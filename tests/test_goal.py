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
from openclaw_iphone.config import IPhoneConfig, load_config
from openclaw_iphone.goal import (CHECK, Decisions, LiquidDecisions, TypeSafeDecisions, api_key, decisions_from_config,
                                  goal_navigation, jpeg, local_guard_from_config, read_key_file, same, settle)
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
                         ["e3", "e4", "e6", "e8", "e13", "done", "scroll_down", "go_back", "dismiss_overlay", "none",
                          "type_text", "submit"])
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
            (step("submit"), "submit", None),
            (step("e4", {"e4": 0.3, "e6": 0.3, "none": 0.4}), "escalate", "low_confidence"),
        ]
        for reply, action, reason in cases:
            ask, _ = scripted(reply, check(0.05))
            d = navigator.decide(ask, s, "Message bek")
            self.assertEqual((d["action"], d.get("reason")), (action, reason), reply)

    def test_go_back_taps_the_back_button(self):
        xml = SCREEN.replace('<XCUIElementTypeOther label="Profile"', '<XCUIElementTypeButton name="BackButton" label="Back" '
                             'enabled="true" x="16" y="50" width="36" height="44"/>\n<XCUIElementTypeOther label="Profile"')
        s = navigator.screen(observe(xml), "")
        back = navigator.back_control(s)
        self.assertEqual((back.label, back.name), ("Back", "BackButton"))
        self.assertIsNone(navigator.back_control(navigator.screen(observe(), "")))
        key = navigator.element_key(back)
        # go_back and the Back button count together; Back beats none at 0.25, and a back tap needs only that much
        for reply, action, confidence in ((step("go_back", {"go_back": 0.5, key: 0.1, "none": 0.4}), "tap", 0.6),
                                          (step("none", {"none": 0.38, "go_back": 0.2, key: 0.06, "e5": 0.36}), "tap", 0.26),
                                          (step("e5", {"e5": 0.3, "go_back": 0.25, key: 0.05, "none": 0.4}), "tap", 0.3),
                                          (step("none", {"none": 0.6, "go_back": 0.2, key: 0.02, "e5": 0.18}), "escalate", None)):
            ask, bodies = scripted(reply, check(0.05))
            d = navigator.decide(ask, s, "Open the home feed")
            self.assertEqual((d["action"], d.get("confidence")), (action, confidence), reply)
            if action == "tap":
                self.assertEqual((d["target"]["label"], d["target"]["key"]), ("Back", key))
                self.assertEqual(len(bodies), 2)  # the tap check still runs
            else:  # an escalation lists what next leaned toward: options by name, elements by target and label
                self.assertEqual(d["considered"], [{"option": "none", "probability": 0.6},
                                                   {"option": "go_back", "target": back.id, "label": "Back", "probability": 0.22},
                                                   {"target": s.rows[2].id, "label": "Message", "probability": 0.18}])
        ask, _ = scripted(step("go_back", {"go_back": 0.6, "none": 0.4}))
        d = navigator.decide(ask, navigator.screen(observe(), ""), "Open the home feed")
        self.assertEqual((d["action"], d["reason"]), ("escalate", "nothing_on_screen_helps"))
        self.assertEqual(d["considered"], [{"option": "go_back", "probability": 0.6}, {"option": "none", "probability": 0.4}])

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


class ShallowAppsTests(unittest.TestCase):
    def test_slow_apps_are_remembered_owner_only_and_expire(self):
        from openclaw_iphone import shallow
        with tempfile.TemporaryDirectory() as home, mock.patch("pathlib.Path.home", return_value=Path(home)):
            self.assertEqual(shallow.load(), {"com.atebits.Tweetie2"})  # X is known slow on every host
            shallow.remember("com.example.slow")
            self.assertEqual(shallow.load(), {"com.atebits.Tweetie2", "com.example.slow"})
            self.assertEqual(os.stat(shallow.path()).st_mode & 0o777, 0o600)
            with mock.patch("time.time", return_value=__import__("time").time() + shallow.KEEP_SECONDS + 1):
                self.assertEqual(shallow.load(), {"com.atebits.Tweetie2"})  # learned apps are re-measured after two weeks
            shallow.path().write_text("not json")
            self.assertEqual(shallow.load(), {"com.atebits.Tweetie2"})


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
        self.server.image_opt_in = self.headers.get("TypeSafe-Danger-Zone-Images")
        status, reply = self.server.replies.pop(0) if self.server.replies else (200, check(0.1))
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class DecisionsTests(unittest.TestCase):
    def test_typesafe_translates_screen_questions_images_and_answers_over_http(self):
        # Native TypeSafe response contract, independent of the adapter's output.
        with DecisionsServer() as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                client = TypeSafeDecisions("secret-key", base_url=f"http://127.0.0.1:{server.server_port}/v1")
                s = navigator.screen(observe(), "data:image/jpeg;base64,AA==")
                request = navigator.request(s, "Open the profile")
                options = request["questions"][0]["choices"]
                next_probabilities = {c["value"]: 1.0 if c["value"] == "done" else 0.0 for c in options}
                blocked_probabilities = {k: 1.0 if k == "none" else 0.0 for k in navigator.BLOCKED_OPTIONS}
                answer = {"answers": {
                    "next": {"type": "choice", "choice": "done", "probabilities": next_probabilities},
                    "done": {"type": "noul", "noul": 0.99},
                    "blocked": {"type": "choice", "choice": "none", "probabilities": blocked_probabilities},
                    "done2": {"type": "noul", "noul": 0.98},
                    "progress": {"type": "score", "score": 2.9},
                }}
                server.replies = [(200, answer)]
                self.assertEqual(navigator.decide(client.ask, s, "Open the profile")["action"], "done")
                path, auth, body = server.requests[-1]
                self.assertEqual((path, auth, body["model"]), ("/v1/systemone", "Bearer secret-key", "jev-1.14.0"))
                ref, asset = next(iter(body["assets"].items()))
                self.assertEqual(asset, {"type": "image", "format": "jpg", "data_base64": "AA=="})
                self.assertIn({"screenshot": ref}, body["state"])
                self.assertEqual(server.image_opt_in, "true")
                self.assertEqual(body["questions"]["done"]["type"], "noul")
                self.assertEqual(body["questions"]["next"]["criteria"]["done"], navigator.NON_ELEMENT_OPTIONS["done"])
                self.assertEqual(body["questions"]["progress"]["criteria"], navigator.PROGRESS_LEVELS)
                # A missing safety judgment or non-finite probability must not become an action.
                for bad in ({"answers": {}}, {"answers": {"risky": {"type": "noul", "noul": float("nan")}}}):
                    server.replies = [(200, bad)]
                    with self.assertRaises(navigator.Unavailable):
                        client.ask({"input": "screen", "questions": [navigator.predicate("risky", "Is this risky?")]})
                self.assertIsNone(server.image_opt_in)  # text-only calls do not opt into images
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_typesafe_setup_reuses_existing_secret_file_without_copying_or_cross_provider_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, secret = Path(tmp, "config.env"), Path(tmp, "memory.env")
            config.write_text('OPENCLAW_IPHONE_DEVICE="phone"\n')
            secret.write_text("TYPESAFE_API_KEY=secret-typesafe\n")
            secret.chmod(0o600)
            env = {"HOME": tmp, "OPENCLAW_IPHONE_CONFIG": str(config), "OPENAI_API_KEY": "wrong-provider"}
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["goal", "setup", "--provider", "typesafe", "--key-file", str(secret)]), 0)
                settings = load_config()
                client = decisions_from_config(settings)
                self.assertIsInstance(client, TypeSafeDecisions)
                self.assertEqual(client.api_key, "secret-typesafe")
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("secret-typesafe", config.read_text() + out.getvalue())
                self.assertEqual(secret.read_text(), "TYPESAFE_API_KEY=secret-typesafe\n")
                self.assertIsNone(read_key_file(secret))  # not an OpenAI credential
                secret.write_text("OPENAI_API_KEY=wrong-provider\n")
                self.assertIsNone(api_key(settings)[0])

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

    def test_liquid_d1_single_pass_box_grounding_and_text_extraction(self):
        with DecisionsServer() as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                client = LiquidDecisions("liq-secret", base_url=f"http://127.0.0.1:{server.server_port}/decisions/v1", model="d1.2")
                self.assertTrue(client.supports_box)
                action_probs = {k: (0.9 if k == "tap" else 0.02) for k in navigator.VISION_ACTIONS}
                blocked_probs = {k: (0.96 if k == "none" else 0.01) for k in navigator.BLOCKED_OPTIONS}
                effect_probs = {k: (0.93 if k == "navigate" else 0.01) for k in navigator.EFFECTS}
                server.replies = [(200, {"answers": {
                    "action": {"type": "choice", "choice": "tap", "probabilities": action_probs},
                    "done": {"type": "noul", "noul": 0.05},
                    "blocked": {"type": "choice", "choice": "none", "probabilities": blocked_probs},
                    "done2": {"type": "noul", "noul": 0.05},
                    "progress": {"type": "score", "score": 2.0},
                    "sensitive": {"type": "noul", "noul": 0.01},
                    "target_box": {"type": "box", "box": {"left": 0.2, "top": 0.4, "right": 0.6, "bottom": 0.6}, "confidence": 0.92},
                    "risky": {"type": "noul", "noul": 0.04},
                    "effect": {"type": "choice", "choice": "navigate", "probabilities": effect_probs},
                }})]
                d = navigator.decide_vision(client.ask, "data:image/jpeg;base64,AA==", "Open Search", (400.0, 800.0), app="com.apple.AppStore")
                self.assertEqual((d["action"], d["grounding"], d["needs_approval"]), ("tap", "vision", False))
                self.assertEqual((d["target"]["x"], d["target"]["y"]), (160, 400))
                self.assertEqual(d["confidence"], 0.9)
                path, auth, body = server.requests[-1]
                self.assertEqual((path, auth, body["model"]), ("/decisions/v1/systemone", "Bearer liq-secret", "d1.2"))
                self.assertIsNone(server.image_opt_in)  # Liquid d1 accepts images natively without TypeSafe danger header
                self.assertEqual(body["questions"]["target_box"]["type"], "box")
                # Sensitive screen guard stops immediately without tapping.
                server.replies = [(200, {"answers": {
                    "action": {"type": "choice", "choice": "tap", "probabilities": action_probs},
                    "done": {"type": "noul", "noul": 0.0},
                    "blocked": {"type": "choice", "choice": "none", "probabilities": blocked_probs},
                    "done2": {"type": "noul", "noul": 0.0},
                    "progress": {"type": "score", "score": 1.0},
                    "sensitive": {"type": "noul", "noul": 0.91},
                    "target_box": {"type": "box", "box": {"left": 0.2, "top": 0.4, "right": 0.6, "bottom": 0.6}, "confidence": 0.92},
                    "risky": {"type": "noul", "noul": 0.04},
                    "effect": {"type": "choice", "choice": "navigate", "probabilities": effect_probs},
                }})]
                d_sec = navigator.decide_vision(client.ask, "data:image/jpeg;base64,AA==", "Log in", (400.0, 800.0))
                self.assertEqual((d_sec["action"], d_sec["reason"]), ("escalate", "secure_field"))
                # Hallucination-free text extraction treats "not found" as None.
                server.replies = [
                    (200, {"answers": {"extracted": {"type": "text", "text": "@pear_lmiller"}}}),
                    (200, {"answers": {"extracted": {"type": "text", "text": "not found"}}}),
                ]
                self.assertEqual(navigator.extract_text(client.ask, "data:image/jpeg;base64,AA==", "Read handle"), "@pear_lmiller")
                self.assertIsNone(navigator.extract_text(client.ask, "data:image/jpeg;base64,AA==", "Read missing"))
                # Degenerate or out-of-range boxes are rejected cleanly.
                server.replies = [(200, {"answers": {"b": {"type": "box", "box": {"left": 0.8, "right": 0.2, "top": 0.1, "bottom": 0.5}}}})]
                with self.assertRaises(navigator.Unavailable):
                    client.ask({"input": "s", "questions": [navigator.box("b", "Find it")]})
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_liquid_setup_hosted_local_loopback_and_local_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, secret = Path(tmp, "config.env"), Path(tmp, "liquid.env")
            config.write_text('OPENCLAW_IPHONE_DEVICE="phone"\n')
            secret.write_text("LIQUID_API_KEY=liq-key-123\n")
            secret.chmod(0o600)
            env = {"HOME": tmp, "OPENCLAW_IPHONE_CONFIG": str(config), "OPENAI_API_KEY": "wrong-provider"}
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main([
                    "goal", "setup", "--provider", "liquid", "--key-file", str(secret),
                    "--model", "d1.2-ultra", "--local-guard-url", "http://127.0.0.1:8080/v1",
                ]), 0)
                settings = load_config()
                client = decisions_from_config(settings)
                self.assertIsInstance(client, LiquidDecisions)
                self.assertEqual((client.api_key, client.model), ("liq-key-123", "d1.2-ultra"))
                self.assertIsInstance(client.local_guard, LiquidDecisions)
                self.assertEqual(client.local_guard.url, "http://127.0.0.1:8080/v1/systemone")
                self.assertIn("d1.2-ultra via Liquid AI (LFM-d1)", out.getvalue())
                self.assertIn("local guard: on", out.getvalue())
                self.assertNotIn("liq-key-123", config.read_text() + out.getvalue())
            # Local loopback Liquid d1-omni endpoint works with no API key.
            local_cfg = IPhoneConfig({
                "OPENCLAW_IPHONE_GOAL_ENABLED": "1",
                "OPENCLAW_IPHONE_GOAL_PROVIDER": "d1",
                "OPENCLAW_IPHONE_GOAL_MODEL": "d1-omni-2B",
                "OPENCLAW_IPHONE_LIQUID_BASE_URL": "http://127.0.0.1:8090/v1",
            })
            local_client = decisions_from_config(local_cfg, env={})
            self.assertIsInstance(local_client, LiquidDecisions)
            self.assertEqual((local_client.url, local_client.model, local_client.api_key),
                             ("http://127.0.0.1:8090/v1/systemone", "d1-omni-2B", ""))
            # Non-loopback local guard URL is rejected to prevent accidental remote leakage.
            with self.assertRaises(ValueError):
                local_guard_from_config(IPhoneConfig({"OPENCLAW_IPHONE_LOCAL_GUARD_URL": "https://evil.example/v1"}))

    def test_liquid_openrouter_codex_lb_decisions_contract(self):
        # OpenRouter / codex-lb Decisions endpoint uses /decisions, array-wrapped state, inline image_url, and supports_box=False.
        with DecisionsServer() as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}/v1/decisions"
                # Force non-loopback detection by testing explicit /decisions path via proxy host + direct HTTP test:
                client = LiquidDecisions("codex-key", base_url="https://codex.unblocklabs.ai/v1/decisions", model="liquid/d1.2-ultra")
                self.assertEqual(client.url, "https://codex.unblocklabs.ai/v1/decisions")
                self.assertTrue(client._openrouter_decisions)
                self.assertFalse(client.supports_box)

                # Route through local test server while keeping _openrouter_decisions=True to verify exact JSON wire body.
                client.url = base_url
                s = navigator.screen(observe(), "data:image/jpeg;base64,AA==")
                req = navigator.request(s, "Open the profile")
                opts = req["questions"][0]["choices"]
                next_probs = {c["value"]: 1.0 if c["value"] == "done" else 0.0 for c in opts}
                blocked_probs = {k: 1.0 if k == "none" else 0.0 for k in navigator.BLOCKED_OPTIONS}
                server.replies = [(200, {
                    "model": "liquid/d1.2-ultra-20261116",
                    "answers": {
                        "next": {"type": "choice", "choice": "done", "probabilities": next_probs, "confidence": 0.99},
                        "done": {"type": "noul", "noul": 0.98},
                        "blocked": {"type": "choice", "choice": "none", "probabilities": blocked_probs, "confidence": 0.99},
                        "done2": {"type": "noul", "noul": 0.97},
                        "progress": {"type": "score", "score": 2.95, "legend": {"0": "a", "1": "b", "2": "c", "3": "d"}, "probabilities": {"0": 0.0, "1": 0.0, "2": 0.05, "3": 0.95}, "confidence": 0.95},
                    },
                })]
                self.assertEqual(navigator.decide(client.ask, s, "Open the profile")["action"], "done")
                path, auth, body = server.requests[-1]
                self.assertEqual((path, auth, body["model"]), ("/v1/decisions", "Bearer codex-key", "liquid/d1.2-ultra"))
                self.assertNotIn("assets", body)
                self.assertIn({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}, body["state"])

                # Bare string input (like CHECK) is also wrapped in a list for codex-lb/OpenRouter Decisions.
                server.replies = [(200, {"model": "liquid/d1.2-ultra-20261116", "answers": {"check": {"type": "noul", "noul": 0.99}}})]
                out = client.ask(CHECK)
                self.assertAlmostEqual(out["check"]["probability"], 0.99)
                _, _, check_body = server.requests[-1]
                self.assertEqual(check_body["state"], ["The sky is blue."])

                # CODEX_LB_API_KEY env fallback works for codex.unblocklabs.ai liquid configs.
                codex_cfg = IPhoneConfig({
                    "OPENCLAW_IPHONE_GOAL_ENABLED": "1",
                    "OPENCLAW_IPHONE_GOAL_PROVIDER": "liquid",
                    "OPENCLAW_IPHONE_GOAL_MODEL": "liquid/d1.2-ultra",
                    "OPENCLAW_IPHONE_LIQUID_BASE_URL": "https://codex.unblocklabs.ai/v1/decisions",
                })
                resolved = decisions_from_config(codex_cfg, env={"CODEX_LB_API_KEY": "codex-env-key"})
                self.assertIsInstance(resolved, LiquidDecisions)
                self.assertEqual((resolved.api_key, resolved.url, resolved.model),
                                 ("codex-env-key", "https://codex.unblocklabs.ai/v1/decisions", "liquid/d1.2-ultra"))
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_same_distinguishes_different_screens_with_identical_byte_length(self):
        a = png(16, 16)
        self.assertTrue(same(a, a))
        self.assertFalse(same(png(16, 16), png(32, 32)))
        # Same length synthetic PNG-sized buffers that diverge across middle and tail must not collide.
        buf_a = b"\x89PNG\r\n\x1a\n" + struct.pack(">II", 0, 16) + struct.pack(">II", 100, 200) + (b"A" * 512)
        buf_b = b"\x89PNG\r\n\x1a\n" + struct.pack(">II", 0, 16) + struct.pack(">II", 100, 200) + (b"B" * 512)
        self.assertFalse(same(buf_a, buf_b))

    def test_settle_caps_captures_on_animated_screens_and_stops_early_when_stable(self):
        # Stable screen settles in 2 captures.
        stable_shot = png(16, 16)
        calls = 0
        def stable():
            nonlocal calls
            calls += 1
            return stable_shot
        self.assertEqual(settle(stable, max_wait=1.0, poll=0.0, max_shots=3), stable_shot)
        self.assertEqual(calls, 2)
        # Continuously changing frames (video/blinking cursor) stop at max_shots rather than spinning.
        frames = [png(16 + i, 16) for i in range(6)]
        idx = 0
        def animated():
            nonlocal idx
            frame = frames[idx]
            idx += 1
            return frame
        self.assertEqual(settle(animated, max_wait=5.0, poll=0.0, max_shots=3), frames[2])
        self.assertEqual(idx, 3)

    def test_decide_vision_supports_submit_action(self):
        action_probs = {k: (0.92 if k == "submit" else 0.01) for k in navigator.VISION_ACTIONS}
        blocked_probs = {k: (0.96 if k == "none" else 0.01) for k in navigator.BLOCKED_OPTIONS}
        ask = lambda _req: {
            "action": {"choice": "submit", "probabilities": action_probs},
            "done": {"probability": 0.05},
            "blocked": {"choice": "none", "probabilities": blocked_probs},
            "done2": {"probability": 0.05},
            "progress": {"score": 2.0},
            "sensitive": {"probability": 0.01},
        }
        d = navigator.decide_vision(ask, "data:image/jpeg;base64,AA==", "Submit search", (390.0, 844.0))
        self.assertEqual((d["action"], d["confidence"]), ("submit", 0.92))


if __name__ == "__main__":
    unittest.main()
