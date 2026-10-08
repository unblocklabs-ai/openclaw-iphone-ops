"""CLI JSON-lines against real WDA HTTP; no physical device access."""
from contextlib import contextmanager
import calendar
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

from openclaw_iphone.connection import Connection
from openclaw_iphone.control_lock import control_lock
from openclaw_iphone.errors import OpenClawIPhoneError, SessionOutputUnavailable
from openclaw_iphone.protocol import JsonLineEmitter, read_requests
from test_goal import DecisionsServer, check, step
from test_image_evidence import png

ROOT = Path(__file__).resolve().parents[1]
XML = '''<XCUIElementTypeApplication bundleId="test.app" visible="true" enabled="true" x="0" y="0" width="8" height="16">
<XCUIElementTypeButton label="Next" visible="true" enabled="true" x="1" y="2" width="4" height="4"/>
<XCUIElementTypeSecureTextField name="SERVER-PRIVATE" label="SERVER-PRIVATE" value="SERVER-PRIVATE" visible="true" enabled="true" x="1" y="7" width="4" height="4"/>
</XCUIElementTypeApplication>'''

BOOTSTRAP = '''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from openclaw_iphone import cli
from openclaw_iphone.devicectl import Device
from openclaw_iphone.runner import Runner
base = Path(os.environ["IPHONE_TEST_DIR"])
class CoreDeviceFixture:
    def __init__(self):
        self.runner = Runner(timeout=float(os.environ.get("IPHONE_TEST_TIMEOUT", "2")))
    def select_device(self, selector):
        with (base / "selectors").open("a") as stream:
            stream.write(json.dumps(selector) + "\\n")
        return Device("fixture", "core", "connected", "iPhone", (base / "identity").read_text())
    def coredevice_wda_url(self, identifier):
        return os.environ["IPHONE_TEST_URL"], None
ctl = CoreDeviceFixture()
previous_metrics = ctl.runner.metrics
cli.client_from_args = lambda args: ctl
with patch("pathlib.Path.home", return_value=base):
    code = cli.main(sys.argv[1:])
assert ctl.runner.budget is None, "Caller budget not restored"
assert ctl.runner.metrics is previous_metrics, "Caller metrics not restored"
sys.exit(code)
'''


GOAL_XML = '''<XCUIElementTypeApplication bundleId="test.app" name="Test" label="Test" enabled="true" x="0" y="0" width="414" height="896">
<XCUIElementTypeButton label="Next" enabled="true" x="20" y="100" width="100" height="44"/>
<XCUIElementTypeStaticText label="Welcome" enabled="true" x="20" y="200" width="200" height="20"/>
</XCUIElementTypeApplication>'''


class PhoneServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        super().__init__(("127.0.0.1", 0), PhoneHandler)
        self.requests = []
        self.failures = {}
        self.xml = XML
        self.elements = ["field"]
        self.size = {"width": 8, "height": 16}
        self.image = png()
        self.sessions = 0
        self.text = ""
        self.delay_action = False
        self.action_entered = threading.Event()
        self.release_action = threading.Event()
        self.source_delay = 0
        self.image_delay = 0
        self.lost_sources = 0
        self.hidden = set()
        self.stale = set()
        self.element_types = {"field": "XCUIElementTypeTextField", "wheel": "XCUIElementTypePickerWheel"}
        self.picker_values = ["One", "Two", "Three"]
        self.picker_index = 0
        self.reject_text = False
        self.reject_picker_value = False
        self.foreground = "test.app"
        self.transition_at = 0
        self.transition_to = "test.app"
        self.fail_picker_readback = False
        self.native_date = date(2023, 3, 31)
        self.year_omitted = False
        self.date_max = None
        self.date_min = None
        self.month_values = list(calendar.month_name)[1:]
        self.date_wheels = ["day", "year", "month"]
        self.element_types.update({"date": "XCUIElementTypeDatePicker", "switch": "XCUIElementTypeSwitch",
                                   **{ref: "XCUIElementTypePickerWheel" for ref in self.date_wheels}})
        self.component_labels = {"day": "", "year": "", "month": ""}
        self.number_suffixes = {"day": "", "year": ""}
        self.checked = True
        self.submit_count = 0
        self.value_delay = 0
        self.mutate_then_fail = set()
        self.fail_read_after = set()

    def wheel_value(self, part):
        if part == "year" and self.year_omitted:
            return "----"
        value = getattr(self.native_date, part)
        return self.month_values[value - 1] if part == "month" else str(value) + self.number_suffixes[part]

    def adjust_date(self, part, value):
        if part == "year":
            self.year_omitted = False
        values = {part: getattr(self.native_date, part) for part in ("year", "month", "day")}
        values[part] = self.month_values.index(value) + 1 if part == "month" else int(value.removesuffix(self.number_suffixes[part]))
        values["day"] = min(values["day"], calendar.monthrange(values["year"], values["month"])[1])
        updated = date(**values)
        if self.date_min:
            updated = max(updated, self.date_min)
        self.native_date = min(updated, self.date_max) if self.date_max else updated


class PhoneHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = self.path.split("?", 1)[0]
        self.server.requests.append((self.command, path, payload))
        if path.endswith("/actions") and self.server.delay_action:
            self.server.action_entered.set()
            self.server.release_action.wait(timeout=5)
        failure = self.server.failures.get(path, 0)
        if failure:
            self.server.failures[path] -= 1
            error = "invalid session id" if path == "/wda/activeAppInfo" else "unknown error"
            body, status = {"value": {"error": error, "message": "SERVER-PRIVATE"}}, 500
        else:
            status = 200
            value = None
            if path == "/status":
                value = {"ready": True}
            elif path == "/wda/locked":
                value = False
            elif path == "/wda/activeAppInfo":
                if self.server.transition_at and time.monotonic() >= self.server.transition_at:
                    self.server.foreground = self.server.transition_to
                value = {"bundleId": self.server.foreground, "pid": 1}
            elif path == "/source":
                time.sleep(self.server.source_delay)
                value = self.server.xml
            elif path == "/screenshot":
                import base64
                time.sleep(self.server.image_delay)
                value = base64.b64encode(self.server.image).decode()
            elif path.endswith("/displayed"):
                value = path.split("/element/")[1].split("/")[0] not in self.server.hidden
            elif path.endswith("/window/size"):
                value = self.server.size
            elif path.endswith("/elements"):
                query = payload["value"]
                refs = self.server.elements
                if "/element/date/" in path or query.startswith("**/XCUIElementTypePickerWheel"):
                    refs = self.server.date_wheels
                elif "XCUIElementTypeDatePicker" in query:
                    refs = ["date"]
                elif "XCUIElementTypeSwitch" in query:
                    refs = ["switch"]
                elif "XCUIElementTypePickerWheel" in query:
                    for part, label in self.server.component_labels.items():
                        if label and (f'"{label}"' in query or f"'{label}'" in query):
                            refs = [part]
                value = [{"ELEMENT": ref, "type": self.server.element_types.get(ref),
                          "label": self.server.component_labels.get(ref),
                          "attribute/value": (self.server.wheel_value(ref) if ref in self.server.date_wheels
                              else str(int(self.server.checked)) if ref == "switch" and self.server.checked is not None
                              else self.server.picker_values[self.server.picker_index] if ref == "wheel"
                              else self.server.text)} for ref in refs]
            elif path.endswith("/element/active"):
                value = {"ELEMENT": "field"}
            elif "/attribute/" in path:
                ref, attribute = path.split("/element/", 1)[1].split("/attribute/")
                if attribute == "type":
                    value = self.server.element_types.get(ref)
                elif attribute == "value":
                    time.sleep(self.server.value_delay)
                    if ref in self.server.date_wheels:
                        value = self.server.wheel_value(ref)
                    elif ref == "switch":
                        value = str(int(self.server.checked)) if self.server.checked is not None else None
                    else:
                        value = self.server.picker_values[self.server.picker_index] if ref == "wheel" else self.server.text
                elif attribute == "label":
                    value = self.server.component_labels.get(ref)
            elif "/pickerwheel/" in path:
                ref = path.split("/pickerwheel/")[1].split("/")[0]
                step = 1 if payload["order"] == "next" else -1
                if ref in self.server.date_wheels:
                    current = getattr(self.server.native_date, ref)
                    if ref == "month":
                        self.server.adjust_date(ref, self.server.month_values[(current - 1 + step) % 12])
                    elif ref == "day":
                        limit = calendar.monthrange(self.server.native_date.year, self.server.native_date.month)[1]
                        self.server.adjust_date(ref, str((current - 1 + step) % limit + 1))
                    else:
                        self.server.adjust_date(ref, str(current + step))
                else:
                    self.server.picker_index = (self.server.picker_index + step) % len(self.server.picker_values)
            elif path.endswith("/element/switch/click"):
                self.server.checked = not self.server.checked
            elif path.endswith("/wda/apps/activate"):
                self.server.transition_to = payload["bundleId"]
                self.server.transition_at = time.monotonic() + 0.2
            elif path.endswith("/clear"):
                self.server.text = ""
            elif path.endswith("/wda/keys") or path.endswith("/value"):
                text = "".join(payload["value"])
                ref = path.split("/element/")[1].split("/")[0] if "/element/" in path else "field"
                if ref in self.server.date_wheels:
                    self.server.adjust_date(ref, text)
                elif ref == "wheel":
                    if text in self.server.picker_values and not self.server.reject_picker_value:
                        self.server.picker_index = self.server.picker_values.index(text)
                    if self.server.fail_picker_readback:
                        self.server.failures["/session/one/element/wheel/attribute/value"] = 1
                elif not self.server.reject_text:
                    self.server.text += text
                if path in self.server.fail_read_after:
                    read_route = ("/session/one/element/date/elements" if ref in self.server.date_wheels
                                  else f"/session/one/element/{ref}/attribute/value")
                    self.server.failures[read_route] = 1
            elif path.endswith("/actions"):
                self.server.submit_count += sum(action.get("value") == "\ue007" and action["type"] == "keyDown"
                    for source in payload["actions"] for action in source["actions"])
            body = {"value": value}
            if path == "/source" and self.server.lost_sources:
                self.server.lost_sources -= 1  # As after a WDA restart: no active session.
                body["sessionId"] = None
            if path.endswith("/displayed") and path.split("/element/")[1].split("/")[0] in self.server.stale:
                body, status = {"value": {"error": "stale element reference", "message": "gone"}}, 404
            if path in self.server.mutate_then_fail:
                body, status = {"value": {"error": "unknown error", "message": "SERVER-PRIVATE"}}, 500
            if path == "/session" and self.command == "POST":
                self.server.sessions += 1
                body["sessionId"] = "one"
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    do_GET = respond
    do_POST = respond
    do_DELETE = respond


class SessionTests(unittest.TestCase):
    @contextmanager
    def running(self, *, images=False, seconds=2, read_seconds=None, timeout=2, status_unavailable=False, config=""):
        with tempfile.TemporaryDirectory() as directory, PhoneServer() as server:
            base = Path(directory)
            (base / "identity").write_text("physical")
            (base / "config.env").write_text(config)
            if status_unavailable:
                server.failures["/status"] = 100
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), IPHONE_TEST_DIR=directory,
                       IPHONE_TEST_TIMEOUT=str(timeout),
                       IPHONE_TEST_URL=f"http://127.0.0.1:{server.server_port}",
                       OPENCLAW_IPHONE_CONFIG=str(base / "config.env"),
                       OPENCLAW_IPHONE_WDA_URL="", OPENCLAW_IPHONE_DEVICE="", http_proxy="http://127.0.0.1:1")
            if config:
                env["no_proxy"] = "127.0.0.1"  # the fake Decisions API
            args = [sys.executable, "-c", BOOTSTRAP, "--evidence-dir", directory]
            if read_seconds is not None:
                args.extend(["--read-timeout", str(read_seconds)])
            args.extend(["session", "--device", "physical", "--operation-timeout", str(seconds)])
            if images:
                args.append("--allow-images")
            proc = subprocess.Popen(args, env=env, cwd=base, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.ready = self.receive(proc)
                self.assertEqual(self.ready["status"], "ready")
                yield proc, server, base
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream:
                        stream.close()
                server.release_action.set()
                server.shutdown()
                thread.join(timeout=5)

    def receive(self, proc, *, timeout=5):
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        self.assertTrue(ready, "CLI did not respond")
        line = proc.stdout.readline()
        self.assertTrue(line, "CLI ended without a response")
        return json.loads(line)

    def request(self, proc, data, *, timeout=5):
        proc.stdin.write((json.dumps(data) + "\n").encode())
        proc.stdin.flush()
        return self.receive(proc, timeout=timeout)

    def finish(self, proc, base):
        self.assertEqual(self.request(proc, {"op": "close"})["status"], "closed")
        # communicate consumes any TextIO/BufferedReader-prefetched session_end.
        proc.stdin.close()
        proc.stdin = None
        output, errors = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, errors.decode())
        self.assertNotIn(b"SERVER-PRIVATE", errors + output)
        with control_lock(base / ".openclaw/iphone/control.lock"):
            pass
        return next(json.loads(line) for line in output.splitlines() if json.loads(line)["status"] == "session_end")

    def test_idle_rejected_frames_and_many_requests_do_not_end_ownership(self):
        with self.running(seconds=0.5, status_unavailable=True) as (proc, server, base):
            time.sleep(0.7)
            with self.assertRaises(OpenClawIPhoneError) as busy, control_lock(base / ".openclaw/iphone/control.lock"):
                pass
            self.assertEqual(busy.exception.owner["pid"], proc.pid)
            self.assertEqual(busy.exception.owner["requests"], 0)
            server.failures["/wda/locked"] = 100
            for raw in (b'{"op":"close","op":"press"}\n', b'{broken}\n', b'x' * 70000 + b'\n', b'{"op":"tap"}\n', b'{"op":"tap","target":null}\n',
                        b'{"op":"press","button":"home","duration":-1}\n'):
                proc.stdin.write(raw)
                proc.stdin.flush()
                self.assertEqual(self.receive(proc)["reason"], "invalid_request")
            for _ in range(20):
                self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.assertEqual(server.sessions, 1)
            self.assertEqual(sum(path.endswith("/appium/settings") for _, path, _ in server.requests), 1)
            self.assertEqual(sum(path.endswith("/wda/pressButton") for _, path, _ in server.requests), 20)
            self.assertFalse(any(path == "/source" for _, path, _ in server.requests))
            self.assertFalse(any(path == "/wda/locked" for _, path, _ in server.requests))
            self.assertFalse(any(path == "/status" for _, path, _ in server.requests))
            result = self.request(proc, {"op": "swipe", "from_x": 1, "from_y": 2,
                "to_x": 3, "to_y": 4, "duration": 11})
            self.assertEqual(result["dispatch"], "acknowledged")
            moves = server.requests[-1][2]["actions"][0]["actions"]
            self.assertEqual(moves[2]["duration"], 11000)
            self.finish(proc, base)

    def test_ambiguity_finds_late_matches_and_pages_only_relevant_candidates(self):
        with self.running() as (proc, server, base):
            attrs = 'visible="true" enabled="true" x="1" y="2" width="4" height="4"'
            noise = ''.join(f'<XCUIElementTypeButton label="Noise {i}" {attrs}/>' for i in range(100))
            rows = ''.join(f'<XCUIElementTypeOther name="Group {i}" {attrs}>'
                f'<XCUIElementTypeButton label="Late" {attrs}/></XCUIElementTypeOther>' for i in range(100))
            server.xml = f'<XCUIElementTypeApplication bundleId="test.app" {attrs}>{noise}{rows}</XCUIElementTypeApplication>'
            server.elements = ["one", "two"]
            result = self.request(proc, {"op": "tap", "target": {"role": "XCUIElementTypeButton", "label": "Late"}})
            self.assertEqual(result["dispatch"], "not_sent")
            self.assertTrue(result["candidates"], "Late matches must not disappear behind the first screen page")
            candidates, next_offset = result["candidates"], result["candidate_page"]["next_offset"]
            while next_offset is not None:
                page = self.request(proc, {"op": "observe", "candidates": True, "offset": next_offset})["observation"]["accessibility"]
                candidates.extend(page["elements"])
                next_offset = page["next_offset"]
            self.assertEqual(sum(e.get("label") == "Late" for e in candidates), 100)
            self.assertEqual(sum(e.get("name", "").startswith("Group ") for e in candidates), 100)
            self.assertFalse(any(e.get("label", "").startswith("Noise") for e in candidates))
            self.assertEqual(sum(path == "/source" for _, path, _ in server.requests), 1)
            self.assertFalse(any(path.endswith("/click") for _, path, _ in server.requests))
            self.finish(proc, base)

    def test_framing_failure_is_not_a_phone_failure_and_releases_owner(self):
        with self.running() as (proc, server, base):
            proc.stdin.write(b'{"op":"press"}')
            proc.stdin.close()
            proc.stdin = None
            result = self.receive(proc)
            self.assertEqual((result["reason"], result["dispatch"]), ("framing_error", "not_sent"))
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(any(path.endswith("/wda/pressButton") for _, path, _ in server.requests))
            with control_lock(base / ".openclaw/iphone/control.lock"):
                pass

    def test_unknown_write_is_not_replayed_and_inspection_then_new_action_work(self):
        with self.running(images=True) as (proc, server, base):
            server.failures["/session/one/actions"] = 1
            result = self.request(proc, {"op": "tap", "x": 2, "y": 3})
            self.assertEqual(result["dispatch"], "unknown")
            self.assertIn("image", self.request(proc, {"op": "observe", "mode": "image"})["observation"])
            self.assertEqual(self.request(proc, {"op": "tap", "x": 4, "y": 5})["dispatch"], "acknowledged")
            actions = [p for _, path, p in server.requests if path.endswith("/actions")]
            self.assertEqual(len(actions), 2)
            self.assertEqual(actions[0]["actions"][0]["actions"][0]["x"], 2)
            self.assertEqual(actions[1]["actions"][0]["actions"][0]["x"], 4)
            self.assertFalse(any(path == "/source" for _, path, _ in server.requests))
            self.finish(proc, base)

    def test_optional_image_arithmetic_failure_cannot_erase_acknowledged_tap(self):
        with self.running(images=True) as (proc, server, base):
            result = self.request(proc, {"op": "tap", "x": 2, "y": 3,
                "observe": "image", "masks": [[1e308, 0, 1e308, 1]]})
            self.assertEqual(result["dispatch"], "acknowledged")
            self.assertIn("image", result["observation"])
            self.assertEqual(sum(path.endswith("/actions") for _, path, _ in server.requests), 1)
            self.finish(proc, base)

    def test_ax_timeout_or_malformed_xml_does_not_recreate_session_or_starve_image(self):
        for kind in ("timeout", "xml"):
            with self.subTest(kind=kind), self.running(images=True, read_seconds=0.08) as (proc, server, base):
                if kind == "timeout":
                    server.source_delay = 0.3
                else:
                    server.xml = "<invalid"
                started = time.monotonic()
                result = self.request(proc, {"op": "tap", "x": 1, "y": 2, "observe": "both"})
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(result["dispatch"], "acknowledged")
                self.assertIn("image", result["observation"])
                self.assertIn("accessibility_error", result["observation"])
                self.assertEqual((server.sessions, result["timing"]["reconnects"]), (1, 0))
                self.assertEqual(sum(path == "/source" for _, path, _ in server.requests), 1)
                self.assertEqual(len((base / "selectors").read_text().splitlines()), 1)
                self.finish(proc, base)

    def test_native_picker_readback_bounded_adjustment_and_no_replay(self):
        target = {"role": "XCUIElementTypePickerWheel", "label": "Month"}
        with self.running() as (proc, server, base):
            server.elements = ["wheel"]
            result = self.request(proc, {"op": "pick", "target": target, "value": "Three",
                "max_steps": 100, "seconds": 60})
            self.assertEqual((result["dispatch"], result["effect"]), ("acknowledged", "match"))
            self.assertEqual(server.picker_index, 2)
            server.picker_index = 0
            result = self.request(proc, {"op": "pick", "target": target, "value": "Three", "order": "next", "max_steps": 2})
            self.assertEqual((result["effect"], result["acknowledged_substeps"]), ("match", 2))
            result = self.request(proc, {"op": "pick", "target": target, "value": "Missing", "order": "previous", "max_steps": 2})
            self.assertEqual((result["effect"], result["acknowledged_substeps"]), ("mismatch", 2))
            server.fail_picker_readback = True
            server.picker_index = 2
            result = self.request(proc, {"op": "pick", "target": target, "value": "One"})
            self.assertEqual((result["dispatch"], result["effect"]), ("acknowledged", "unknown"))
            server.fail_picker_readback = False
            route = "/session/one/element/wheel/value"
            server.failures[route] = 1
            result = self.request(proc, {"op": "pick", "target": target, "value": "Two"})
            self.assertEqual(result["dispatch"], "unknown")
            self.assertEqual(sum(path == route for method, path, _ in server.requests if method == "POST"), 3)
            self.assertNotIn("Two", json.dumps(result))
            self.finish(proc, base)

    def test_set_text_replaces_clears_preserves_literal_input_and_does_not_submit(self):
        with self.running() as (proc, server, base):
            for role, text in (("XCUIElementTypeTextField", "Bék O'Neil 👋"),
                               ("XCUIElementTypeTextField", "007"),
                               ("XCUIElementTypeTextField", "🙂" * 4096),
                               ("XCUIElementTypeTextField", ""),
                               ("XCUIElementTypeTextView", "First line\nSecond line")):
                with self.subTest(role=role, text=text):
                    server.element_types["field"] = role
                    server.text = "old value"
                    result = self.request(proc, {"op": "set", "value": text, "verify": True})
                    self.assertEqual(server.text, text)
                    self.assertEqual((result["dispatch"], result["effect"]), ("acknowledged", "match"))
            server.element_types["field"] = "XCUIElementTypeTextField"
            before = server.text
            result = self.request(proc, {"op": "set", "value": "Do not\nsubmit"})
            self.assertEqual(result["dispatch"], "not_sent")
            self.assertEqual(server.text, before)
            private = base / "input.txt"
            private_text = "SECRET-NAME" + "x" * 5000
            private.write_text(private_text)
            private.chmod(0o600)
            result = self.request(proc, {"op": "set", "target": {"role": "XCUIElementTypeTextField"},
                                         "value_ref": str(private), "verify": True})
            self.assertEqual((server.text, result["effect"]), (private_text, "match"))
            self.assertNotIn("SECRET", json.dumps(result))
            server.element_types["field"] = "XCUIElementTypeSecureTextField"
            result = self.request(proc, {"op": "set", "value_ref": str(private), "verify": True})
            self.assertEqual((server.text, result["effect"]), (private_text, "unknown"))
            self.assertEqual(server.submit_count, 0)
            self.finish(proc, base)

    def test_set_date_is_one_request_handles_clamping_and_checks_complete_date(self):
        target = {"role": "XCUIElementTypeDatePicker", "label": "Birthday"}
        with self.running() as (proc, server, base):
            # Day/month/year are not in semantic order. Month adjustment clamps
            # March 31 to February 29 before the desired day is processed.
            result = self.request(proc, {"op": "set", "target": target, "value": "2024-02-29"})
            self.assertEqual(server.native_date, date(2024, 2, 29))
            self.assertEqual((result["dispatch"], result["effect"]), ("acknowledged", "unknown"))
            self.assertEqual(result["request_sequence"], 1)
            self.assertNotIn("2024-02-29", json.dumps(result))
            # Bound native round trips, not Python calls: lookup, grouped reads,
            # three writes. No preflight or per-component polling.
            routes = sum(result["timing"]["counts"].values())
            self.assertLessEqual(routes, 5, "Date entry must not add preflight or per-wheel reads")
            result = self.request(proc, {"op": "set", "target": target, "value": "2024-02-29"})
            self.assertEqual((result["dispatch"], result["effect"], result["acknowledged_substeps"]),
                             ("not_sent", "match", 0))
            # Native Contacts represents a birthday without a year as ----.
            server.year_omitted = True
            result = self.request(proc, {"op": "set", "target": target, "value": "1990-10-14", "verify": True})
            self.assertEqual((server.native_date, result.get("effect")), (date(1990, 10, 14), "match"))
            self.assertFalse(server.year_omitted)
            server.date_max = date(2024, 2, 28)
            server.native_date = date(2023, 3, 31)
            result = self.request(proc, {"op": "set", "target": target, "value": "2024-02-29", "verify": True})
            self.assertEqual(server.native_date, date(2024, 2, 28))
            self.assertEqual(result["effect"], "mismatch")
            # Below the native minimum, don't march the year farther away.
            server.date_max, server.date_min = None, date(1970, 1, 1)
            server.native_date = date(2023, 3, 31)
            result = self.request(proc, {"op": "set", "target": target, "value": "1900-10-14", "verify": True})
            self.assertEqual((server.native_date, result["effect"]), (date(1970, 10, 14), "mismatch"))
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_set_date_numeric_labels_and_explicit_localized_component_mapping(self):
        with self.running() as (proc, server, base):
            server.month_values = [f"{month:02d}" for month in range(1, 13)]
            server.component_labels = {part: part.title() for part in server.date_wheels}
            result = self.request(proc, {"op": "set", "kind": "date", "value": "1990-10-14", "verify": True,
                                         "target": {"role": "XCUIElementTypeDatePicker"}})
            self.assertEqual((server.native_date, result["effect"]), (date(1990, 10, 14), "match"))
            # Without labels, two small numeric wheels aren't identifiable by
            # their positions. No input is sent; ordinary control remains usable.
            server.component_labels = {part: "" for part in server.date_wheels}
            before = server.native_date
            result = self.request(proc, {"op": "set", "kind": "date", "value": "2001-01-01",
                                         "target": {"role": "XCUIElementTypeDatePicker"}})
            self.assertEqual((result["dispatch"], result["reason"]), ("not_sent", "date_component_mapping_required"))
            self.assertEqual(server.native_date, before)
            # Malformed optional provider labels must not crash the session or
            # turn ambiguous numeric wheels into guessed date components.
            for malformed in (1, {"bad": "label"}):
                server.component_labels = {part: malformed for part in server.date_wheels}
                result = self.request(proc, {"op": "set", "kind": "date", "value": "2001-01-01",
                                             "target": {"role": "XCUIElementTypeDatePicker"}})
                self.assertEqual((result["dispatch"], result["reason"]), ("not_sent", "date_component_mapping_required"))
                self.assertEqual(server.native_date, before)
            months = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                      "septembre", "octobre", "novembre", "décembre"]
            server.month_values = months
            server.component_labels = {"day": "D", "year": "Y", "month": "M"}
            result = self.request(proc, {"op": "set", "kind": "date", "value": "2001-08-15", "verify": True,
                "components": {part: {"role": "XCUIElementTypePickerWheel", "label": server.component_labels[part]}
                               for part in server.date_wheels}, "month_values": months})
            self.assertEqual((server.native_date, result["effect"]), (date(2001, 8, 15), "match"))
            server.month_values = [f"{month}月" for month in range(1, 13)]
            server.number_suffixes = {"year": "年", "day": "日"}
            result = self.request(proc, {"op": "set", "kind": "date", "value": "2024-01-01", "verify": True,
                "components": {part: {"role": "XCUIElementTypePickerWheel", "label": server.component_labels[part]}
                               for part in server.date_wheels}})
            self.assertEqual((server.native_date, result["effect"]), (date(2024, 1, 1), "match"))
            self.finish(proc, base)

    def test_set_date_invalid_input_and_partial_unknown_writes_never_replay_or_end_session(self):
        target = {"role": "XCUIElementTypeDatePicker"}
        with self.running() as (proc, server, base):
            for value in ("2023-02-29", "October 14 1990"):
                result = self.request(proc, {"op": "set", "kind": "date", "target": target, "value": value})
                self.assertEqual(result["dispatch"], "not_sent")
                self.assertEqual(server.native_date, date(2023, 3, 31))
            route = "/session/one/element/month/value"
            server.mutate_then_fail.add(route)
            result = self.request(proc, {"op": "set", "kind": "date", "target": target, "value": "2024-02-20"})
            self.assertEqual((result["dispatch"], result["acknowledged_substeps"]), ("unknown", 1))
            self.assertEqual(server.native_date, date(2024, 2, 29))
            self.assertEqual(sum(path == route for method, path, _ in server.requests if method == "POST"), 1)
            self.assertFalse(any(path.endswith("/element/day/value") for _, path, _ in server.requests))
            self.assertNotIn("PRIVATE", json.dumps(result))
            server.mutate_then_fail.clear()
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            server.fail_read_after.add("/session/one/element/year/value")
            result = self.request(proc, {"op": "set", "kind": "date", "target": target, "value": "1990-10-14", "verify": True})
            self.assertEqual((result["dispatch"], result["acknowledged_substeps"], result["effect"]),
                             ("acknowledged", 3, "unknown"))
            self.assertEqual(server.native_date, date(1990, 10, 14))
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_set_checked_does_not_toggle_correct_or_unreadable_state(self):
        target = {"role": "XCUIElementTypeSwitch", "label": "Notifications"}
        with self.running() as (proc, server, base):
            for desired in (False, False, True, True):
                previous = server.checked
                result = self.request(proc, {"op": "set", "target": target, "value": desired})
                self.assertIs(server.checked, desired)
                self.assertEqual(result["effect"], "match" if previous == desired else "unknown")
            self.assertFalse(any("/attribute/value" in path for _, path, _ in server.requests))
            result = self.request(proc, {"op": "set", "target": target, "value": False, "verify": True})
            self.assertEqual(result["effect"], "match")
            clicks = sum(path.endswith("/element/switch/click") for _, path, _ in server.requests)
            self.assertEqual(clicks, 3)
            server.checked = None
            result = self.request(proc, {"op": "set", "target": target, "value": False})
            self.assertEqual((result["dispatch"], result["effect"]), ("not_sent", "unknown"))
            self.assertEqual(sum(path.endswith("/element/switch/click") for _, path, _ in server.requests), clicks)
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_set_picker_sends_once_with_optional_readback_and_no_hidden_adjustment(self):
        with self.running() as (proc, server, base):
            server.elements = ["wheel"]
            target = {"role": "XCUIElementTypePickerWheel", "label": "Options"}
            result = self.request(proc, {"op": "set", "target": target, "value": "Three"})
            self.assertEqual((server.picker_index, result["effect"]), (2, "unknown"))
            self.assertEqual(sum(result["timing"]["counts"].values()), 2)
            self.assertFalse(any("/attribute/value" in path for _, path, _ in server.requests))
            server.reject_picker_value = True
            result = self.request(proc, {"op": "set", "target": target, "value": "One", "verify": True})
            self.assertEqual(result["effect"], "mismatch")
            self.assertEqual(server.picker_index, 2)
            self.assertFalse(any("/pickerwheel/" in path for _, path, _ in server.requests))
            # Selecting an exact option does not require reading the old value.
            server.reject_picker_value = False
            server.picker_values[server.picker_index] = None
            result = self.request(proc, {"op": "set", "target": target, "value": "One"})
            self.assertEqual((result["dispatch"], result["effect"], server.picker_index), ("acknowledged", "unknown", 0))
            self.assertEqual(sum(result["timing"]["counts"].values()), 2)
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_private_readback_uses_operation_budget_without_hidden_subdeadlines(self):
        with self.running(seconds=8, timeout=8) as (proc, server, base):
            server.value_delay = 2.2
            result = self.request(proc, {"op": "type", "text": "slow-readback", "mode": "replace", "verify": True})
            self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "match"))
            server.elements = ["wheel"]
            server.value_delay = 5.2
            result = self.request(proc, {"op": "pick", "target": {"role": "XCUIElementTypePickerWheel"},
                "value": "Three"}, timeout=8)
            self.assertEqual((result["dispatch"], result["effect"]), ("acknowledged", "match"))
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_private_replace_comparison_and_optional_launch_readiness(self):
        with self.running(images=True) as (proc, server, base):
            for kind, expected in (("readable", "match"), ("mismatch", "mismatch"), ("secure", "unknown")):
                server.reject_text = kind == "mismatch"
                server.element_types["field"] = "XCUIElementTypeSecureTextField" if kind == "secure" else "XCUIElementTypeTextField"
                result = self.request(proc, {"op": "type", "text": "SUPPLIED-PRIVATE", "mode": "replace", "verify": True})
                self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", expected))
                self.assertNotIn("PRIVATE", json.dumps(result))
            result = self.request(proc, {"op": "launch", "bundle_id": "new.app", "wait_seconds": 60, "observe": "image"})
            self.assertEqual(result["dispatch"], "acknowledged")
            self.assertEqual(result["readiness"]["state"], "ready")
            self.assertGreaterEqual(result["readiness"]["checks"], 2)
            self.assertEqual(sum(path.endswith("/wda/apps/activate") for _, path, _ in server.requests), 1)
            self.assertEqual(sum(path == "/screenshot" for _, path, _ in server.requests), 1)
            ending = self.finish(proc, base)
            total = sum(count for route, count in ending["timing"]["counts"].items() if route.startswith("wda "))
            self.assertEqual(total, len(server.requests))
            self.assertNotIn("PRIVATE", json.dumps(ending))

    def test_default_screen_keeps_context_unnamed_controls_and_image_without_layout_noise(self):
        with self.running(images=True) as (proc, server, base):
            attrs = 'visible="true" enabled="true" x="1" y="2" width="4" height="4"'
            rows = ''.join(f'<XCUIElementTypeCell label="{account}" {attrs}>'
                f'<XCUIElementTypeOther {attrs}><XCUIElementTypeButton label="Disconnect" {attrs}/>'
                '</XCUIElementTypeOther></XCUIElementTypeCell>' for account in ('Personal', 'Work'))
            controls = ''.join(f'<XCUIElementTypeButton label="Option {i}" {attrs}/>' for i in range(35))
            controls += (f'<XCUIElementTypeButton {attrs}/><XCUIElementTypeTextField focused="true" {attrs}/>'
                         f'<XCUIElementTypeSwitch label="Airplane" value="1" {attrs}/>'
                         f'<XCUIElementTypeButton label="Tab" traits="Selected, Button" {attrs}/>')
            server.xml = (f'<XCUIElementTypeApplication bundleId="test.app" {attrs}>'
                + f'<XCUIElementTypeOther {attrs}>' * 38 + rows + controls
                + '</XCUIElementTypeOther>' * 38 + '</XCUIElementTypeApplication>')
            result = self.request(proc, {"op": "observe"})
            view = result["observation"]
            self.assertTrue(Path(view["image"]["path"]).is_file())
            self.assertIsNone(view["image"]["device_size"])
            self.assertFalse(any(path.endswith("/window/size") for _, path, _ in server.requests))
            elements = view["accessibility"]["elements"]
            self.assertEqual(len(elements), 43)  # Two groups, two controls, 39 other controls.
            by_id = {e["id"]: e for e in elements}
            disconnects = [e for e in elements if e.get("label") == "Disconnect"]
            self.assertEqual([by_id[e["parent"]]["label"] for e in disconnects], ["Personal", "Work"])
            self.assertTrue(any(e["role"] == "XCUIElementTypeButton" and not e.get("label") for e in elements))
            self.assertTrue(any(e["role"] == "XCUIElementTypeTextField" and not e.get("label") for e in elements))
            self.assertTrue(next(e for e in elements if e.get("label") == "Airplane")["checked"])
            self.assertTrue(next(e for e in elements if e.get("label") == "Tab")["selected"])
            self.assertTrue(next(e for e in elements if e["role"] == "XCUIElementTypeTextField")["focused"])
            self.assertLess(len(json.dumps(result).encode()), 16_384)
            self.assertEqual(self.request(proc, {"op": "tap", "target": disconnects[1]["id"]})["dispatch"], "acknowledged")
            server.elements = ["one", "two"]
            ambiguous = self.request(proc, {"op": "tap", "target": {"role": "XCUIElementTypeButton", "label": "Disconnect"}})
            self.assertEqual(ambiguous["dispatch"], "not_sent")
            by_id = {e["id"]: e for e in ambiguous["candidates"]}
            self.assertEqual([by_id[e["parent"]]["label"] for e in ambiguous["candidates"] if e.get("label") == "Disconnect"], ["Personal", "Work"])
            self.finish(proc, base)

    def test_screen_pages_fit_output_and_keep_snapshot_ids_without_another_capture(self):
        with self.running(images=True) as (proc, server, base):
            attrs = 'visible="true" enabled="true" x="1" y="2" width="4" height="4"'
            labels = [f'{i} ' + '\U0001f680' * 240 for i in range(160)]
            labels.extend(f"Tail {i}" for i in range(2200))
            server.xml = f'<XCUIElementTypeApplication bundleId="test.app" {attrs}>' + ''.join(
                f'<XCUIElementTypeButton label="{label}" {attrs}/>' for label in labels) + '</XCUIElementTypeApplication>'
            first = self.request(proc, {"op": "observe", "limit": 2000})
            self.assertTrue(Path(first["observation"]["image"]["path"]).is_file())
            self.assertIn("accessibility", first["observation"])
            page = first["observation"]["accessibility"]
            snapshot_id = page["snapshot_id"]
            first_id = page["elements"][0]["id"]
            seen = []
            server.xml = XML  # Paging must describe the captured screen, not this new one.
            while True:
                self.assertEqual(page["snapshot_id"], snapshot_id)
                self.assertLess(len(json.dumps(page, ensure_ascii=True).encode()), 60_000)
                seen.extend(e["label"] for e in page["elements"])
                if page["next_offset"] is None:
                    break
                page = self.request(proc, {"op": "observe", "offset": page["next_offset"], "limit": 2000})["observation"]["accessibility"]
            self.assertEqual(seen, labels)
            self.assertEqual(sum(route == "/source" for _, route, _ in server.requests), 1)
            self.assertEqual(sum(route == "/screenshot" for _, route, _ in server.requests), 1)
            self.assertEqual(self.request(proc, {"op": "tap", "target": first_id})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_repeated_read_recovery_stays_pinned_and_refuses_replacement(self):
        with self.running() as (proc, server, base):
            for _ in range(3):
                server.lost_sources = 1
                self.assertIn("accessibility", self.request(proc, {"op": "observe"})["observation"])
            selectors = [json.loads(line) for line in (base / "selectors").read_text().splitlines()]
            self.assertEqual(selectors, ["physical"] * 4)
            # Each new session re-applies the zero idle/animation waits a restarted WDA forgot.
            settings = [p["settings"] for _, path, p in server.requests if path.endswith("/appium/settings")]
            self.assertEqual(len(settings), server.sessions)
            self.assertTrue(all(s["waitForIdleTimeout"] == 0 and s["animationCoolOffTimeout"] == 0 for s in settings))
            self.assertFalse(any(path == "/wda/activeAppInfo" for _, path, _ in server.requests))
            old_sessions = server.sessions
            (base / "identity").write_text("replacement")
            server.lost_sources = 1
            self.assertIn("accessibility_error", self.request(proc, {"op": "observe"})["observation"])
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "not_sent")
            self.assertEqual(server.sessions, old_sessions)
            self.assertFalse(any(path.endswith("/wda/pressButton") for _, path, _ in server.requests))
            self.finish(proc, base)

    def test_screen_capture_runs_both_lanes_at_once_and_xpath_targets_skip_hidden_matches(self):
        with self.running(images=True) as (proc, server, base):
            server.source_delay = server.image_delay = 0.3
            started = time.monotonic()
            view = self.request(proc, {"op": "observe"})["observation"]
            self.assertLess(time.monotonic() - started, 0.55)
            self.assertEqual(set(view), {"image", "accessibility"})
            self.assertEqual(view["accessibility"]["app"], "test.app")
            server.source_delay = server.image_delay = 0
            self.assertEqual(self.request(proc, {"op": "tap", "target": view["accessibility"]["elements"][0]["id"]})["dispatch"], "acknowledged")
            self.assertFalse(any(path == "/wda/activeAppInfo" for _, path, _ in server.requests))
            server.elements, server.hidden, server.stale = ["gone", "one", "two"], {"one"}, {"gone"}
            target = {"role": "XCUIElementTypeButton", "label": "Disconnect", "ancestor_label": "Work"}
            self.assertEqual(self.request(proc, {"op": "tap", "target": target})["dispatch"], "acknowledged")
            query = next(p["value"] for _, path, p in server.requests if path.endswith("/elements") and p["using"] == "xpath")
            self.assertNotIn("@visible", query)
            self.assertEqual(sum(path.endswith("/displayed") for _, path, _ in server.requests), 3)
            self.assertTrue(any(path.endswith("/element/two/click") for _, path, _ in server.requests))
            server.hidden = {"one", "two"}
            self.assertEqual(self.request(proc, {"op": "tap", "target": target})["dispatch"], "not_sent")
            self.finish(proc, base)

    def test_masked_image_is_captured_before_the_source_read(self):
        with self.running(images=True) as (proc, server, base):
            self.request(proc, {"op": "observe", "mode": "both", "masks": [[0, 0, 4, 4]]})
            routes = [path for _, path, _ in server.requests]
            self.assertLess(max(routes.index("/screenshot"), next(i for i, r in enumerate(routes) if r.endswith("/window/size"))),
                            routes.index("/source"))
            self.finish(proc, base)

    def test_image_only_masking_reusable_coordinates_and_optional_read_failure(self):
        with self.running(images=True) as (proc, server, base):
            server.failures["/source"] = 20
            server.image = png(16, 32)
            self.request(proc, {"op": "observe", "mode": "image"})
            self.assertFalse(any(route.endswith("/window/size") for _, route, _ in server.requests))
            for _ in range(2):
                self.assertEqual(self.request(proc, {"op": "tap", "space": "image", "x": 6, "y": 8})["dispatch"], "acknowledged")
            self.assertEqual(sum(route.endswith("/window/size") for _, route, _ in server.requests), 1)
            observed = self.request(proc, {"op": "observe", "mode": "image", "masks": [[0, 0, 8, 16]]})
            path = Path(observed["observation"]["image"]["path"])
            raw = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            compressed, offset = b"", 8
            while offset < len(raw):
                length = struct.unpack_from(">I", raw, offset)[0]
                if raw[offset+4:offset+8] == b"IDAT":
                    compressed += raw[offset+8:offset+8+length]
                offset += length + 12
            self.assertEqual(zlib.decompress(compressed), (b"\0" + bytes([32, 32, 32, 255]) * 16) * 32)
            for _ in range(2):
                self.assertEqual(self.request(proc, {"op": "tap", "space": "image", "x": 6, "y": 8})["dispatch"], "acknowledged")
            actions = [p for _, route, p in server.requests if route.endswith("/actions")]
            self.assertEqual(actions[-1]["actions"][0]["actions"][0]["x"], 3)
            self.assertEqual(sum(route.endswith("/window/size") for _, route, _ in server.requests), 2)
            # A new screenshot resets conversion; explicit caller recapture
            # replaces continuous orientation probes before every tap.
            server.size = {"width": 16, "height": 8}
            server.image = png(32, 16)
            self.request(proc, {"op": "observe", "mode": "image"})
            self.assertEqual(self.request(proc, {"op": "tap", "space": "image", "x": 6, "y": 8})["dispatch"], "acknowledged")
            server.failures["/screenshot"] = 2
            result = self.request(proc, {"op": "swipe", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4, "observe": "image"})
            self.assertEqual(result["dispatch"], "acknowledged")
            self.assertIn("image_error", result["observation"])
            self.assertFalse(any(route == "/source" for _, route, _ in server.requests))
            self.finish(proc, base)

    def test_partial_replace_private_input_and_semantic_ambiguity_are_request_local(self):
        with self.running() as (proc, server, base):
            secret = base / "secret"
            secret.write_text("SUPPLIED-PRIVATE\n")
            secret.chmod(0o600)
            server.failures["/session/one/wda/keys"] = 1
            result = self.request(proc, {"op": "type", "text_ref": str(secret), "mode": "replace"})
            self.assertEqual((result["dispatch"], result["acknowledged_substeps"]), ("unknown", 1))
            self.assertEqual(sum(route.endswith("/clear") for _, route, _ in server.requests), 1)
            self.assertFalse(any(route == "/source" for _, route, _ in server.requests))
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            server.xml = XML.replace('label="Next"', 'label="SUPPLIED-PRIVATE"')
            view = self.request(proc, {"op": "observe"})
            self.assertNotIn("PRIVATE", json.dumps(view))
            self.assertIn("bounds", view["observation"]["accessibility"]["elements"][1])
            server.elements = ["one", "two"]
            result = self.request(proc, {"op": "tap", "target": {"role": "XCUIElementTypeButton", "label": "SUPPLIED-PRIVATE"}})
            self.assertEqual(result["dispatch"], "not_sent")
            self.assertTrue(result["candidates"])
            self.assertNotIn("PRIVATE", json.dumps(result))
            server.elements = ["field"]
            self.assertEqual(self.request(proc, {"op": "type", "text_ref": str(secret), "target": {"role": "XCUIElementTypeTextField", "label": "Input"}})["dispatch"], "acknowledged")
            self.assertEqual(server.text, "SUPPLIED-PRIVATE")
            for kind in ("symlink", "public"):
                if kind == "symlink":
                    bad = base / "link"
                    bad.symlink_to(secret)
                else:
                    secret.chmod(0o644)
                    bad = secret
                result = self.request(proc, {"op": "type", "text_ref": str(bad), "mode": "replace"})
                self.assertEqual(result["dispatch"], "not_sent")
                self.assertEqual(sum(route.endswith("/clear") for _, route, _ in server.requests), 1)
            self.finish(proc, base)

    def test_eof_and_output_disconnect_release_ownership_without_replay(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect), self.running() as (proc, server, base):
                if disconnect:
                    proc.stdout.close()
                    proc.stdin.write(b'{"op":"tap","x":1,"y":2}\n')
                    proc.stdin.flush()
                else:
                    proc.stdin.close()
                proc.wait(timeout=5)
                self.assertEqual(proc.returncode, 1 if disconnect else 0)
                self.assertEqual(sum(route.endswith("/actions") for _, route, _ in server.requests), 1 if disconnect else 0)
                with control_lock(base / ".openclaw/iphone/control.lock"):
                    pass

    def test_interruption_preserves_unknown_receipt_and_releases_ownership(self):
        with self.running() as (proc, server, base):
            server.delay_action = True
            proc.stdin.write(b'{"op":"tap","x":1,"y":2}\n')
            proc.stdin.flush()
            self.assertTrue(server.action_entered.wait(timeout=3))
            proc.send_signal(signal.SIGINT)
            result = self.receive(proc)
            self.assertEqual((result["dispatch"], result["reason"]), ("unknown", "interrupted"))
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(sum(route.endswith("/actions") for _, route, _ in server.requests), 1)
            with control_lock(base / ".openclaw/iphone/control.lock"):
                pass

    def test_image_disclosure_is_explicit_and_bad_png_never_persists(self):
        with self.running() as (proc, server, base):
            self.assertEqual(self.request(proc, {"op": "observe", "mode": "image"})["reason"], "invalid_request")
            self.assertFalse(any(route == "/screenshot" for _, route, _ in server.requests))
            self.finish(proc, base)
        with self.running(images=True) as (proc, server, base):
            server.image = b"invalid"
            result = self.request(proc, {"op": "observe", "mode": "image", "masks": [[0, 0, 8, 16]]})
            self.assertIn("image_error", result["observation"])
            self.assertEqual(list(base.rglob("*.png")), [])
            self.finish(proc, base)


    @contextmanager
    def decisions(self):
        with DecisionsServer() as api:
            thread = threading.Thread(target=api.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                yield api, ("OPENCLAW_IPHONE_GOAL_ENABLED=1\nOPENCLAW_IPHONE_OPENAI_API_KEY=test-key\n"
                            f"OPENCLAW_IPHONE_OPENAI_BASE_URL=http://127.0.0.1:{api.server_port}/v1\n")
            finally:
                api.shutdown()
                thread.join(timeout=5)

    def test_goal_steps_through_the_session_until_the_navigator_judges_it_done(self):
        with self.decisions() as (api, config), self.running(config=config) as (proc, server, base):
            self.assertIn("goal", self.ready["capabilities"])
            server.xml = GOAL_XML
            api.replies = [(200, r) for r in (step("e1"), check(0.05), step("type_text"), step("scroll_down"),
                                              step("done", {"done": 0.9, "e1": 0.1}, progress=2.9, done=0.95))]
            reply = self.request(proc, {"op": "goal", "goal": "Reply on the next page", "text": "see you at 5"}, timeout=20)
            self.assertEqual({k: reply[k] for k in ("status", "dispatch", "acknowledged_substeps", "outcome")},
                             {"status": "action", "dispatch": "acknowledged", "acknowledged_substeps": 3, "outcome": "done"})
            self.assertEqual([(s["action"], s.get("label"), s.get("dispatch")) for s in reply["steps"]],
                             [("tap", "Next", "acknowledged"), ("type_text", None, "acknowledged"),
                              ("scroll_down", None, "acknowledged"), ("done", None, None)])
            self.assertTrue(any(path.endswith("/element/field/click") for _, path, _ in server.requests))
            self.assertEqual(server.text, "see you at 5")
            swipe = next(p for _, path, p in server.requests if path.endswith("/actions"))["actions"][0]["actions"]
            self.assertEqual([(a["x"], a["y"]) for a in swipe if a["type"] == "pointerMove"], [(207, 650), (207, 300)])
            (path, auth, first), *_, (_, _, last) = api.requests
            self.assertEqual((path, auth, first["model"]), ("/v1/decisions", "Bearer test-key", "gpt-6-luna"))
            self.assertTrue(first["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))
            self.assertIn({"value": "e1", "description": 'Button "Next" @(70,122) 100x44'}, first["questions"][0]["choices"])
            goal = json.loads(last["input"][0]["content"][0]["text"])["goal"]
            self.assertTrue(goal.endswith("Already done: tapped 'Next'; typed the supplied text; scrolled down"))
            self.assertNotIn("see you", json.dumps(api.requests))
            self.finish(proc, base)

    def test_goal_stops_before_unapproved_risky_taps_and_never_sends_secure_screens(self):
        with self.decisions() as (api, config), self.running(config=config) as (proc, server, base):
            server.xml = GOAL_XML
            for bad in ({"approve": ["delete"]}, {"max_steps": 0}, {"observe": "accessibility"}, {"goal": ""}):
                self.assertEqual(self.request(proc, {"op": "goal", "goal": "Send it", **bad})["reason"], "invalid_request")
            api.replies = [(200, r) for r in (step("e1"), check(0.9, "communication"))]
            reply = self.request(proc, {"op": "goal", "goal": "Send it", "approve": ["social_action"]}, timeout=20)
            self.assertEqual((reply["outcome"], reply["reason"], reply["dispatch"], reply["pending"]["label"]),
                             ("needs_approval", "communication", "not_sent", "Next"))
            self.assertFalse(any(path.endswith("/click") for _, path, _ in server.requests))
            self.assertEqual(self.request(proc, {"op": "tap", "target": reply["pending"]["target"]})["dispatch"], "acknowledged")
            api.replies = [(401, {"error": {"message": "bad key"}})]
            reply = self.request(proc, {"op": "goal", "goal": "Send it"}, timeout=20)
            self.assertEqual((reply["outcome"], reply["reason"], reply["error"]["category"]),
                             ("escalate", "navigator_unavailable", "http_401"))
            api.replies = [(200, step("none", {"none": 0.6, "e1": 0.3, "done": 0.1}))]
            reply = self.request(proc, {"op": "goal", "goal": "Open the inbox"}, timeout=20)
            self.assertEqual((reply["outcome"], reply["reason"]), ("escalate", "nothing_on_screen_helps"))
            considered = reply["steps"][-1]["considered"]
            self.assertEqual([(c.get("option"), c.get("label"), c["probability"]) for c in considered],
                             [("none", None, 0.6), (None, "Next", 0.3), ("done", None, 0.1)])
            # the agent can take over from there: an element it leaned toward is tappable as is
            self.assertEqual(self.request(proc, {"op": "tap", "target": considered[1]["target"]})["dispatch"], "acknowledged")
            asked, server.xml = len(api.requests), XML  # XML has a secure field
            reply = self.request(proc, {"op": "goal", "goal": "Log in"}, timeout=20)
            self.assertEqual((reply["outcome"], reply["reason"], reply["steps"]), ("escalate", "secure_field", []))
            self.assertEqual(len(api.requests), asked)
            self.finish(proc, base)

    def test_goal_navigation_is_off_by_default(self):
        with self.running() as (proc, server, base):
            self.assertNotIn("goal", self.ready["capabilities"])
            reply = self.request(proc, {"op": "goal", "goal": "Open Settings"})
            self.assertEqual((reply["reason"], reply["dispatch"]), ("goal_navigation_disabled", "not_sent"))
            self.finish(proc, base)


class ConnectionTests(unittest.TestCase):
    def test_invalidate_ignores_a_late_failure_from_a_replaced_client(self):
        connection = Connection(object())
        old, current = object(), object()
        connection.wda, connection.valid = current, True
        connection.invalidate(old)
        self.assertTrue(connection.valid)
        connection.invalidate(current)
        self.assertFalse(connection.valid)
        connection.valid = True
        connection.invalidate()
        self.assertFalse(connection.valid)


class ProtocolTests(unittest.TestCase):
    def test_partial_frame_and_stalled_output_bound_ownership_waits(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b'{"op":')
            with self.assertRaises(ValueError):
                list(read_requests(reader, frame_timeout=0.02))
            os.set_blocking(writer, False)
            while True:
                try:
                    os.write(writer, b"x" * 4096)
                except BlockingIOError:
                    break
            started = time.monotonic()
            with self.assertRaises(SessionOutputUnavailable):
                JsonLineEmitter(writer, seconds=0.03)({"status": "ready"})
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(reader)
            os.close(writer)

    def test_oversized_response_preserves_dispatch_receipt(self):
        reader, writer = os.pipe()
        try:
            JsonLineEmitter(writer, max_bytes=256)({"status": "action", "dispatch": "acknowledged",
                "acknowledged_substeps": 1, "observation": {"private": "x" * 1000}})
            raw = os.read(reader, 1024)
            self.assertLessEqual(len(raw), 256)
            result = json.loads(raw)
            self.assertEqual((result["dispatch"], result["acknowledged_substeps"]), ("acknowledged", 1))
            self.assertNotIn("private", result)
            JsonLineEmitter(writer, max_bytes=512)({"status": "action", "dispatch": "acknowledged",
                "observation": {"accessibility": {"private": "x" * 1000}, "image": {"path": "/private/screen.png"}}})
            result = json.loads(os.read(reader, 1024))
            self.assertEqual(result["observation"]["image"]["path"], "/private/screen.png")
            self.assertNotIn("private", result["observation"].get("accessibility", {}))
            JsonLineEmitter(writer)({"status": "still_usable"})
            self.assertEqual(json.loads(os.read(reader, 1024))["status"], "still_usable")
        finally:
            os.close(reader)
            os.close(writer)
