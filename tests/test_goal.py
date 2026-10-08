"""Goal navigation offline: candidate rules, decision rules, the Clef client and config gating."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import struct
import threading
import unittest

from openclaw_iphone import navigator
from openclaw_iphone.config import IPhoneConfig
from openclaw_iphone.goal import Clef, clef_from_config, goal_navigation, jpeg
from openclaw_iphone.observations import parse_observation
from test_image_evidence import png

SCREEN = '''<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram" enabled="true" x="0" y="0" width="414" height="896">
<XCUIElementTypeWindow enabled="true" x="0" y="0" width="414" height="896">
<XCUIElementTypeStaticText label="9:41" enabled="true" x="20" y="10" width="40" height="20"/>
<XCUIElementTypeOther label="Profile" enabled="true" x="0" y="50" width="414" height="700">
<XCUIElementTypeButton label="Message" enabled="true" x="20" y="100" width="100" height="44">
<XCUIElementTypeStaticText label="Message" enabled="true" x="30" y="110" width="80" height="20"/>
</XCUIElementTypeButton>
<XCUIElementTypeCell label="bek, Bek" enabled="true" x="0" y="200" width="414" height="60">
<XCUIElementTypeStaticText label="bek" enabled="true" x="70" y="210" width="40" height="20"/>
</XCUIElementTypeCell>
<XCUIElementTypeSwitch label="Private" value="1" enabled="false" traits="Selected" x="300" y="300" width="51" height="31"/>
<XCUIElementTypeOther label="Vertical scroll bar, 2 pages" enabled="true" x="408" y="50" width="3" height="700"/>
<XCUIElementTypeButton label="Hidden by tabs" enabled="true" x="20" y="830" width="100" height="40"/>
<XCUIElementTypeButton label="Below" enabled="true" x="20" y="1000" width="100" height="40"/>
</XCUIElementTypeOther>
<XCUIElementTypeTabBar enabled="true" x="0" y="813" width="414" height="83">
<XCUIElementTypeButton label="Home" enabled="true" x="0" y="813" width="82" height="49"/>
</XCUIElementTypeTabBar>
<XCUIElementTypeKeyboard enabled="true" x="0" y="600" width="414" height="200">
<XCUIElementTypeKey label="q" enabled="true" x="0" y="600" width="40" height="40"/>
</XCUIElementTypeKeyboard>
</XCUIElementTypeWindow>
</XCUIElementTypeApplication>'''


def observe(xml=SCREEN):
    return parse_observation(xml, generation=0, device_udid="test", captured_at="", started=0, finished=0)


def answers(choice, probabilities=None, *, progress=1.0, done=0.1, blocked="none", blocked_p=0.9):
    return {"answers": {"next": {"choice": choice, "probabilities": probabilities or {choice: 0.9}},
                        "done": {"noul": done}, "done2": {"noul": done}, "progress": {"score": progress},
                        "blocked": {"choice": blocked, "probabilities": {blocked: blocked_p}}}}


def risk(noul, effect="navigate"):
    return {"answers": {"risky": {"noul": noul}, "effect": {"choice": effect}}}


def scripted(*replies):
    bodies, queue = [], list(replies)
    def ask(body):
        bodies.append(body)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    return ask, bodies


class NavigatorTests(unittest.TestCase):
    def test_candidates_keep_what_the_validated_rules_keep(self):
        s = navigator.screen(observe(), "data:image/jpeg;base64,AA==")
        self.assertEqual([navigator.line(e) for e in s.rows], [
            'e3 Other "Profile" @(207,400) 414x700',
            'e4 Button "Message" @(70,122) 100x44 in "Profile"',
            'e6 Cell "bek, Bek" @(207,230) 414x60 in "Profile"',
            'e8 Switch "Private" on selected disabled @(326,316) 51x31 in "Profile"',
            'e13 Button "Home" @(41,838) 82x49',
        ])
        self.assertEqual(s.notes, ("An on-screen keyboard is open (individual keys omitted).",))
        body = navigator.request(s, "Message bek")
        self.assertEqual(list(body["questions"]["next"]["criteria"]),
                         ["e3", "e4", "e6", "e8", "e13", "done", "scroll_down", "dismiss_overlay", "none", "type_text"])
        self.assertEqual(body["state"]["app"], "com.burbn.instagram")
        self.assertEqual(body["images"], ["data:image/jpeg;base64,AA=="])

    def test_home_screen_keeps_controls_near_the_top(self):
        xml = SCREEN.replace("com.burbn.instagram", "com.apple.springboard")
        s = navigator.screen(observe(xml), "")
        self.assertEqual(navigator.line(s.rows[0]), 'e2 StaticText "9:41" @(40,20) 40x20')

    def test_decision_rules(self):
        s = navigator.screen(observe(), "")
        cases = [
            (answers("e4", blocked="permission_or_consent"), "escalate", "permission_or_consent_prompt"),
            (answers("e4", blocked="permission_or_consent", blocked_p=0.4), "tap", None),
            (answers("e4", progress=2.5), "done", None),
            (answers("done", {"done": 0.8, "e4": 0.2}, progress=2.45, done=0.75), "done", None),
            (answers("done", {"done": 0.6, "scroll_down": 0.3, "e4": 0.1}, progress=2.45, done=0.9), "scroll_down", None),
            (answers("none"), "escalate", "nothing_on_screen_helps"),
            (answers("type_text"), "type_text", None),
            (answers("e4", {"e4": 0.3, "e6": 0.3, "none": 0.4}), "escalate", "low_confidence"),
        ]
        for reply, action, reason in cases:
            ask, _ = scripted(reply, risk(0.05))
            d = navigator.decide(ask, s, "Message bek")
            self.assertEqual((d["action"], d.get("reason")), (action, reason), reply)

    def test_tap_confidence_counts_options_under_the_same_point_and_risky_taps_need_approval(self):
        s = navigator.screen(observe(), "")
        # e3 (the profile area) contains the Message button's center: both land the same tap.
        ask, bodies = scripted(answers("e4", {"e4": 0.2, "e6": 0.8}), risk(0.05))
        d = navigator.decide(ask, s, "Open Profile")
        self.assertEqual((d["action"], d["confidence"]), ("escalate", 0.2))
        ask, bodies = scripted(answers("e4", {"e4": 0.3, "e3": 0.2, "e6": 0.5}), risk(0.05))
        d = navigator.decide(ask, s, "Message bek")
        self.assertEqual((d["action"], d["confidence"], d["needs_approval"]), ("tap", 0.5, False))
        self.assertEqual(d["target"], {"id": s.rows[1].id, "key": "e4", "role": "Button", "label": "Message", "x": 70, "y": 122})
        self.assertEqual(bodies[1]["state"]["element"], 'e4 Button "Message" @(70,122) 100x44 in "Profile"')
        for reply in (risk(0.5), risk(0.1, "communication"), navigator.Unavailable("down"), {"answers": {}}):
            ask, _ = scripted(answers("e4"), reply)
            self.assertTrue(navigator.decide(ask, s, "Message bek")["needs_approval"], reply)
        ask, bodies = scripted(answers("e4"))
        self.assertNotIn("needs_approval", navigator.decide(ask, s, "Message bek", check_risk=False))
        self.assertEqual(len(bodies), 1)


class ClefServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ClefHandler)
        self.requests, self.replies = [], []


class ClefHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, self.headers.get("Authorization"), body))
        status, reply = self.server.replies.pop(0) if self.server.replies else (200, {"success": True, "result": risk(0.1)})
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ClefTests(unittest.TestCase):
    def test_client_posts_the_body_and_reports_failures_without_details(self):
        with ClefServer() as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                clef = Clef("acct", "secret-token", base_url=f"http://127.0.0.1:{server.server_port}/client/v4/")
                self.assertEqual(clef.ask({"state": "s"}), risk(0.1))
                path, auth, body = server.requests[0]
                self.assertEqual(path, "/client/v4/accounts/acct/ai/run/@cf/cloudflare/clef")
                self.assertEqual((auth, body), ("Bearer secret-token", {"state": "s", "model": "clef"}))
                server.replies = [(500, {"success": False}), (200, {"success": False, "errors": ["x"]}), (200, b"not json")]
                for category in ("http_500", "bad_reply", "bad_reply"):
                    with self.assertRaises(navigator.Unavailable) as failure:
                        clef.ask({})
                    self.assertEqual(failure.exception.category, category)
                    self.assertNotIn("secret-token", str(failure.exception))
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_goal_navigation_is_off_unless_enabled_with_credentials(self):
        self.assertEqual(goal_navigation(IPhoneConfig({})), "off")
        self.assertEqual(goal_navigation(IPhoneConfig({"OPENCLAW_IPHONE_CLEF_ENABLED": "0", "OPENCLAW_IPHONE_CLEF_ACCOUNT_ID": "a",
                                                       "OPENCLAW_IPHONE_CLEF_API_TOKEN": "t"})), "off")
        self.assertEqual(goal_navigation(IPhoneConfig({"OPENCLAW_IPHONE_CLEF_ENABLED": "1"})), "incomplete")
        self.assertIsNone(clef_from_config(IPhoneConfig({"OPENCLAW_IPHONE_CLEF_ENABLED": "1"})))
        clef = clef_from_config(IPhoneConfig({"OPENCLAW_IPHONE_CLEF_ENABLED": "true", "OPENCLAW_IPHONE_CLEF_ACCOUNT_ID": "a",
                                              "OPENCLAW_IPHONE_CLEF_API_TOKEN": "t"}))
        self.assertEqual(clef.url, "https://api.cloudflare.com/client/v4/accounts/a/ai/run/@cf/cloudflare/clef")

    def test_screenshot_is_sent_as_jpeg_no_larger_than_1024(self):
        def size(url):
            raw = base64.b64decode(url.removeprefix("data:image/jpeg;base64,"))
            self.assertTrue(raw.startswith(b"\xff\xd8"))
            sof = raw.index(b"\xff\xc0")
            return struct.unpack_from(">HH", raw, sof + 5)[::-1]
        self.assertEqual(size(jpeg(png(64, 2048))), (32, 1024))
        self.assertEqual(size(jpeg(png(8, 16))), (8, 16))
        with self.assertRaises(ValueError):
            jpeg(b"not a png")


if __name__ == "__main__":
    unittest.main()
