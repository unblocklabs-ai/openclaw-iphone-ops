"""Goal navigation offline: candidate rules, decision rules, the Decisions API client and config gating."""
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest import mock

from openclaw_iphone import cli, navigator
from openclaw_iphone.config import IPhoneConfig
from openclaw_iphone.goal import Decisions, api_key, decisions_from_config, goal_navigation, jpeg
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


def distribution(probabilities):
    return [{"value": k, "probability": p} for k, p in probabilities.items()]


def step(choice, probabilities=None, *, progress=1.0, done=0.1, done2=None, blocked="none", blocked_p=0.9):
    """A Decisions reply to the step request, as the API sends it."""
    blocked_probabilities = {blocked: blocked_p} | ({"none": round(1 - blocked_p, 2)} if blocked != "none" else {})
    return {"answers": [
        {"type": "choice", "name": "next", "choice": choice, "probabilities": distribution(probabilities or {choice: 0.9}),
         "confidence": 0.8},
        {"type": "predicate", "name": "done", "probability": done},
        {"type": "choice", "name": "blocked", "choice": blocked, "probabilities": distribution(blocked_probabilities),
         "confidence": 0.8},
        {"type": "predicate", "name": "done2", "probability": done if done2 is None else done2},
        {"type": "score", "name": "progress", "score": progress,
         "probabilities": [{"value": 1, "label": "1", "probability": 1.0}], "confidence": 0.5}]}


def check(risky, effect="navigate", *, covered=0.0, part=0.0):
    """A Decisions reply to the tap check."""
    return {"answers": [
        {"type": "predicate", "name": "risky", "probability": risky},
        {"type": "choice", "name": "effect", "choice": effect, "probabilities": distribution({effect: 0.9}), "confidence": 0.8},
        {"type": "predicate", "name": "covered", "probability": covered},
        {"type": "predicate", "name": "part", "probability": part}]}


def scripted(*replies):
    bodies, queue = [], list(replies)
    def ask(body):
        bodies.append(body)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return navigator.answers(reply)
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
        text, image = body["input"][0]["content"]
        self.assertEqual(json.loads(text["text"]), {"goal": "Message bek", "app": "com.burbn.instagram", "notes": list(s.notes)})
        self.assertEqual(image, {"type": "input_image", "image_url": "data:image/jpeg;base64,AA=="})
        self.assertEqual([(q["type"], q["name"]) for q in body["questions"]],
                         [("choice", "next"), ("predicate", "done"), ("choice", "blocked"), ("predicate", "done2"),
                          ("score", "progress")])
        choices = body["questions"][0]["choices"]
        self.assertEqual([c["value"] for c in choices],
                         ["e3", "e4", "e6", "e8", "e13", "done", "scroll_down", "dismiss_overlay", "none", "type_text"])
        self.assertEqual(choices[1]["description"], 'Button "Message" @(70,122) 100x44 in "Profile"')
        self.assertIn("\nTrue: ", body["questions"][3]["instructions"])
        self.assertEqual([lv["label"] for lv in body["questions"][4]["levels"]], ["0", "1", "2", "3"])

    def test_home_screen_keeps_controls_near_the_top(self):
        xml = SCREEN.replace("com.burbn.instagram", "com.apple.springboard")
        s = navigator.screen(observe(xml), "")
        self.assertEqual(navigator.line(s.rows[0]), 'e2 StaticText "9:41" @(40,20) 40x20')

    def test_decision_rules(self):
        s = navigator.screen(observe(), "")
        cases = [
            (step("e4", blocked="permission_or_consent"), "escalate", "permission_or_consent_prompt"),
            (step("e4", blocked="permission_or_consent", blocked_p=0.4), "tap", None),
            (step("done", {"done": 0.8, "e4": 0.2}, progress=2.9, done=0.95), "done", None),
            # progress alone does not end the goal: done2 must agree, and a confident tap pick vetoes it
            (step("e4", {"e4": 0.5, "done": 0.5}, progress=2.9, done=0.95, done2=0.2), "tap", None),
            (step("e4", {"e4": 0.9, "done": 0.1}, progress=2.9, done=0.95), "tap", None),
            (step("done", {"done": 0.8, "scroll_down": 0.2}, progress=2.0, done=0.75), "done", None),
            (step("done", {"done": 0.6, "scroll_down": 0.3, "e4": 0.1}, progress=2.45, done=0.9), "scroll_down", None),
            (step("none"), "escalate", "nothing_on_screen_helps"),
            (step("type_text"), "type_text", None),
            (step("e4", {"e4": 0.3, "e6": 0.3, "none": 0.4}), "escalate", "low_confidence"),
        ]
        for reply, action, reason in cases:
            ask, _ = scripted(reply, check(0.05))
            d = navigator.decide(ask, s, "Message bek")
            self.assertEqual((d["action"], d.get("reason")), (action, reason), reply)

    def test_tap_check_flags_risky_taps_and_closes_overlays_in_the_way(self):
        s = navigator.screen(observe(), "")
        # e3 (the profile area) contains the Message button's center: both land the same tap.
        ask, bodies = scripted(step("e4", {"e4": 0.2, "e6": 0.8}))
        self.assertEqual(navigator.decide(ask, s, "Open Profile")["action"], "escalate")
        ask, bodies = scripted(step("e4", {"e4": 0.3, "e3": 0.2, "e6": 0.5}), check(0.05))
        d = navigator.decide(ask, s, "Message bek")
        self.assertEqual((d["action"], d["confidence"], d["needs_approval"]), ("tap", 0.5, False))
        self.assertEqual(d["target"], {"id": s.rows[1].id, "key": "e4", "role": "Button", "label": "Message", "x": 70, "y": 122})
        state = json.loads(bodies[1]["input"][0]["content"][0]["text"])
        self.assertEqual(state["element"], 'e4 Button "Message" @(70,122) 100x44 in "Profile"')
        self.assertEqual([q["name"] for q in bodies[1]["questions"]], ["risky", "effect", "covered", "part"])
        for reply in (check(0.5), check(0.1, "communication"), navigator.Unavailable("down"), {"answers": []}):
            ask, _ = scripted(step("e4"), reply)
            self.assertTrue(navigator.decide(ask, s, "Message bek")["needs_approval"], reply)
        for first, tap_check, action in ((step("e4"), check(0.05, covered=0.9), "dismiss_overlay"),
                                         (step("e4", blocked="menu_or_sheet"), check(0.05, part=0.1), "dismiss_overlay"),
                                         (step("e4", blocked="menu_or_sheet"), check(0.05, part=0.9), "tap"),
                                         (step("e4"), check(0.05, part=0.1), "tap")):
            ask, _ = scripted(first, tap_check)
            d = navigator.decide(ask, s, "Message bek")
            self.assertEqual(d["action"], action, (first, tap_check))
            self.assertEqual("target" in d, action == "tap")
        ask, bodies = scripted(step("e4"))
        self.assertNotIn("needs_approval", navigator.decide(ask, s, "Message bek", check_tap=False))
        self.assertEqual(len(bodies), 1)

    def test_answers_reject_refusals_and_unexpected_replies(self):
        self.assertEqual(navigator.answers(check(0.2))["effect"], {"choice": "navigate", "probabilities": {"navigate": 0.9}})
        for reply, category in (({"answers": [{"type": "refusal", "name": "risky"}]}, "refusal"),
                                ({"error": "x"}, "bad_reply"), ({"answers": [{"type": "predicate"}]}, "bad_reply")):
            with self.assertRaises(navigator.Unavailable) as failure:
                navigator.answers(reply)
            self.assertEqual(failure.exception.category, category)


class DecisionsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), DecisionsHandler)
        self.requests, self.replies = [], []


class DecisionsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, self.headers.get("Authorization"), body))
        status, reply = self.server.replies.pop(0) if self.server.replies else (200, check(0.1))
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class DecisionsTests(unittest.TestCase):
    def test_client_posts_the_body_and_reports_failures_without_details(self):
        with DecisionsServer() as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                client = Decisions("secret-key", base_url=f"http://127.0.0.1:{server.server_port}/v1/")
                self.assertEqual(client.ask({"input": "s", "questions": []}), navigator.answers(check(0.1)))
                path, auth, body = server.requests[0]
                self.assertEqual(path, "/v1/decisions")
                self.assertEqual((auth, body), ("Bearer secret-key", {"input": "s", "questions": [], "model": "gpt-6-luna"}))
                server.replies = [(429, {}), (503, {})]  # retried, then answered
                self.assertEqual(client.ask({}), navigator.answers(check(0.1)))
                boom = (500, {"error": {"message": "boom"}})
                server.replies = [boom, boom, boom, (401, {}), (200, {"error": "x"}), (200, b"not json"),
                                  (200, {"answers": [{"type": "refusal", "name": "risky"}]})]
                for category in ("http_500", "http_401", "bad_reply", "bad_reply", "refusal"):
                    with self.assertRaises(navigator.Unavailable) as failure:
                        client.ask({})
                    self.assertEqual(failure.exception.category, category)
                    self.assertNotIn("secret-key", str(failure.exception))
                self.assertEqual(len(server.requests), 1 + 3 + 3 + 4)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_goal_navigation_is_off_unless_enabled_with_a_key(self):
        on = {"OPENCLAW_IPHONE_GOAL_ENABLED": "1"}
        self.assertEqual(goal_navigation(IPhoneConfig({}), env={"OPENAI_API_KEY": "k"}), "off")
        self.assertEqual(goal_navigation(IPhoneConfig({"OPENCLAW_IPHONE_GOAL_ENABLED": "0",
                                                       "OPENCLAW_IPHONE_OPENAI_API_KEY": "k"}), env={}), "off")
        self.assertTrue(goal_navigation(IPhoneConfig(on), env={}).startswith("incomplete (no OpenAI API key"))
        self.assertIsNone(decisions_from_config(IPhoneConfig(on), env={}))
        # an unset $VAR reference left in the config file is not a key
        self.assertIsNone(decisions_from_config(IPhoneConfig(on | {"OPENCLAW_IPHONE_OPENAI_API_KEY": "$OPENAI_API_KEY"}), env={}))
        client = decisions_from_config(IPhoneConfig({"OPENCLAW_IPHONE_GOAL_ENABLED": "true",
                                                     "OPENCLAW_IPHONE_OPENAI_API_KEY": "k"}), env={})
        self.assertEqual((client.url, client.api_key), ("https://api.openai.com/v1/decisions", "k"))
        self.assertEqual(goal_navigation(IPhoneConfig(on), env={"OPENAI_API_KEY": "env-key"}),
                         "on (gpt-6-luna via OpenAI's Decisions API; key: OPENAI_API_KEY in the environment)")

    def test_the_key_can_live_in_config_in_a_key_file_or_in_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare, dotenv, empty = Path(tmp, "openai.key"), Path(tmp, ".env"), Path(tmp, "empty")
            bare.write_text("sk-file\n")
            dotenv.write_text("OTHER=1\nexport OPENAI_API_KEY='sk-dotenv'\n")
            empty.write_text("")
            for path in (bare, dotenv):
                path.chmod(0o600)
            env = {"OPENAI_API_KEY": "sk-env"}
            def key(values, env=env):
                return api_key(IPhoneConfig(values, path=Path(tmp, "config.env")), env)
            self.assertEqual(key({"OPENCLAW_IPHONE_OPENAI_API_KEY": "sk-inline", "OPENCLAW_IPHONE_OPENAI_API_KEY_FILE": str(bare)}),
                             ("sk-inline", f"OPENCLAW_IPHONE_OPENAI_API_KEY in {tmp}/config.env"))
            self.assertEqual(key({"OPENCLAW_IPHONE_OPENAI_API_KEY_FILE": str(bare)}), ("sk-file", f"key file {bare}"))
            self.assertEqual(key({"OPENCLAW_IPHONE_OPENAI_API_KEY_FILE": str(dotenv)})[0], "sk-dotenv")
            self.assertEqual(key({"OPENCLAW_IPHONE_OPENAI_API_KEY_FILE": str(empty)}), (None, f"no key found in {empty}"))
            self.assertEqual(key({"OPENCLAW_IPHONE_OPENAI_API_KEY_FILE": f"{tmp}/missing"})[0], None)
            self.assertEqual(key({}), ("sk-env", "OPENAI_API_KEY in the environment"))
            bare.chmod(0o644)
            self.assertTrue(key({"OPENCLAW_IPHONE_OPENAI_API_KEY_FILE": str(bare)})[1].endswith("(readable by others: chmod 600)"))

    def test_goal_setup_records_the_key_owner_only_and_goal_check_proves_it(self):
        with tempfile.TemporaryDirectory() as tmp, DecisionsServer() as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            config = Path(tmp, "config.env")
            config.write_text('OPENCLAW_IPHONE_DEVICE="Bill iPhone"\nOPENCLAW_IPHONE_GOAL_ENABLED="0"\n'
                              f'OPENCLAW_IPHONE_OPENAI_BASE_URL=http://127.0.0.1:{server.server_port}/v1\n')
            key_file = Path(tmp, "openai.key")
            key_file.write_text("sk-from-file\n")
            env = {"HOME": tmp, "OPENCLAW_IPHONE_CONFIG": str(config), "no_proxy": "127.0.0.1"}
            def run(*argv, stdin=""):
                out = io.StringIO()
                with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdin", io.StringIO(stdin)), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    return cli.main(list(argv)), out.getvalue()
            try:
                code, out = run("goal", "setup", "--key-stdin", stdin="sk-secret-123\n")
                self.assertEqual(code, 0)
                self.assertNotIn("sk-secret-123", out)
                self.assertIn(f"goal-navigation: on (gpt-6-luna via OpenAI's Decisions API; key: "
                              f"OPENCLAW_IPHONE_OPENAI_API_KEY in {config})", out)
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                text = config.read_text()
                self.assertIn('OPENCLAW_IPHONE_DEVICE="Bill iPhone"', text)
                self.assertIn("OPENCLAW_IPHONE_GOAL_ENABLED=1\nOPENCLAW_IPHONE_OPENAI_BASE_URL", text)  # replaced in place
                self.assertIn("OPENCLAW_IPHONE_OPENAI_API_KEY=sk-secret-123", text)
                self.assertEqual(run("goal", "check")[0], 0)
                path, auth, body = server.requests[-1]
                self.assertEqual((path, auth, body["input"]), ("/v1/decisions", "Bearer sk-secret-123", "The sky is blue."))
                code, out = run("goal", "setup", "--key-file", str(key_file))
                self.assertIn(f"key: key file {key_file.resolve()}", out)
                self.assertNotIn("sk-secret-123", config.read_text())  # the stored key gives way to the file
                server.replies = [(401, {})]
                code, out = run("goal", "check")
                self.assertEqual((code, out.splitlines()[-1]), (1, "goal-check: failed (http_401)"))
                self.assertEqual(server.requests[-1][1], "Bearer sk-from-file")
                code, out = run("goal", "setup", "--disable")
                self.assertIn("goal-navigation: off", out)
                self.assertIn("OPENCLAW_IPHONE_OPENAI_API_KEY_FILE=", config.read_text())
                self.assertEqual(run("goal", "check")[0], 1)
                self.assertEqual(run("goal", "setup", "--key-stdin", stdin="two words")[0], 1)
            finally:
                server.shutdown()
                thread.join(timeout=5)

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
