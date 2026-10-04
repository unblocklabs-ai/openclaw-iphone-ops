"""CLI JSON-lines against real WDA HTTP; no physical device access."""
from contextlib import contextmanager
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

from openclaw_iphone.control_lock import control_lock
from openclaw_iphone.errors import OpenClawIPhoneError, SessionOutputUnavailable
from openclaw_iphone.protocol import JsonLineEmitter, read_requests
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
        self.runner = Runner(timeout=2)
    def select_device(self, selector, *, read_only=False):
        with (base / "selectors").open("a") as stream:
            stream.write(json.dumps([selector, read_only]) + "\\n")
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
        self.element_types = {"field": "XCUIElementTypeTextField", "wheel": "XCUIElementTypePickerWheel"}
        self.picker_values = ["One", "Two", "Three"]
        self.picker_index = 0
        self.reject_text = False
        self.foreground = "test.app"
        self.transition_at = 0
        self.transition_to = "test.app"
        self.fail_picker_readback = False


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
                value = base64.b64encode(self.server.image).decode()
            elif path.endswith("/window/size"):
                value = self.server.size
            elif path.endswith("/elements"):
                value = [{"ELEMENT": ref} for ref in self.server.elements]
            elif path.endswith("/element/active"):
                value = {"ELEMENT": "field"}
            elif "/attribute/" in path:
                ref, attribute = path.split("/element/", 1)[1].split("/attribute/")
                if attribute == "type":
                    value = self.server.element_types.get(ref)
                elif attribute == "value":
                    value = self.server.picker_values[self.server.picker_index] if ref == "wheel" else self.server.text
            elif "/pickerwheel/" in path:
                self.server.picker_index = (self.server.picker_index + (1 if payload["order"] == "next" else -1)) % len(self.server.picker_values)
            elif path.endswith("/wda/apps/activate"):
                self.server.transition_to = payload["bundleId"]
                self.server.transition_at = time.monotonic() + 0.2
            elif path.endswith("/clear"):
                self.server.text = ""
            elif path.endswith("/wda/keys") or path.endswith("/value"):
                text = "".join(payload["value"])
                if "/element/wheel/" in path:
                    if text in self.server.picker_values:
                        self.server.picker_index = self.server.picker_values.index(text)
                    if self.server.fail_picker_readback:
                        self.server.failures["/session/one/element/wheel/attribute/value"] = 1
                elif not self.server.reject_text:
                    self.server.text += text
            body = {"value": value}
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
    def running(self, *, images=False, seconds=2, read_seconds=12):
        with tempfile.TemporaryDirectory() as directory, PhoneServer() as server:
            base = Path(directory)
            (base / "identity").write_text("physical")
            (base / "config.env").write_text("")
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), IPHONE_TEST_DIR=directory,
                       IPHONE_TEST_URL=f"http://127.0.0.1:{server.server_port}",
                       OPENCLAW_IPHONE_CONFIG=str(base / "config.env"),
                       OPENCLAW_IPHONE_WDA_URL="", OPENCLAW_IPHONE_DEVICE="", http_proxy="http://127.0.0.1:1")
            args = [sys.executable, "-c", BOOTSTRAP, "--evidence-dir", directory,
                    "--read-timeout", str(read_seconds),
                    "session", "--device", "physical", "--operation-timeout", str(seconds)]
            if images:
                args.append("--allow-images")
            proc = subprocess.Popen(args, env=env, cwd=base, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertEqual(self.receive(proc)["status"], "ready")
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

    def receive(self, proc):
        ready, _, _ = select.select([proc.stdout], [], [], 5)
        self.assertTrue(ready, "CLI did not respond")
        line = proc.stdout.readline()
        self.assertTrue(line, "CLI ended without a response")
        return json.loads(line)

    def request(self, proc, data):
        proc.stdin.write((json.dumps(data) + "\n").encode())
        proc.stdin.flush()
        return self.receive(proc)

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
        with self.running(seconds=0.5) as (proc, server, base):
            time.sleep(0.7)
            with self.assertRaises(OpenClawIPhoneError) as busy, control_lock(base / ".openclaw/iphone/control.lock"):
                pass
            self.assertEqual(busy.exception.owner["pid"], proc.pid)
            self.assertEqual(busy.exception.owner["requests"], 0)
            for raw in (b'{"op":"close","op":"press"}\n', b'{broken}\n', b'x' * 5000 + b'\n', b'{"op":"tap"}\n', b'{"op":"tap","target":null}\n',
                        b'{"op":"press","button":"home","duration":1e30}\n'):
                proc.stdin.write(raw)
                proc.stdin.flush()
                self.assertEqual(self.receive(proc)["reason"], "invalid_request")
            for _ in range(20):
                self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "acknowledged")
            self.assertEqual(server.sessions, 1)
            self.assertEqual(sum(path.endswith("/appium/settings") for _, path, _ in server.requests), 1)
            self.assertEqual(sum(path.endswith("/wda/pressButton") for _, path, _ in server.requests), 20)
            self.assertFalse(any(path == "/source" for _, path, _ in server.requests))
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
            result = self.request(proc, {"op": "pick", "target": target, "value": "Three"})
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

    def test_private_replace_comparison_and_optional_launch_readiness(self):
        with self.running(images=True) as (proc, server, base):
            for kind, expected in (("readable", "match"), ("mismatch", "mismatch"), ("secure", "unknown")):
                server.reject_text = kind == "mismatch"
                server.element_types["field"] = "XCUIElementTypeSecureTextField" if kind == "secure" else "XCUIElementTypeTextField"
                result = self.request(proc, {"op": "type", "text": "SUPPLIED-PRIVATE", "mode": "replace", "verify": True})
                self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", expected))
                self.assertNotIn("PRIVATE", json.dumps(result))
            result = self.request(proc, {"op": "launch", "bundle_id": "new.app", "wait_seconds": 1, "observe": "image"})
            self.assertEqual((result["dispatch"], result["readiness"]["state"]), ("acknowledged", "ready"))
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
                         f'<XCUIElementTypeButton label="Tab" selected="true" {attrs}/>')
            server.xml = (f'<XCUIElementTypeApplication bundleId="test.app" {attrs}>'
                + f'<XCUIElementTypeOther {attrs}>' * 38 + rows + controls
                + '</XCUIElementTypeOther>' * 38 + '</XCUIElementTypeApplication>')
            result = self.request(proc, {"op": "observe"})
            view = result["observation"]
            self.assertTrue(Path(view["image"]["path"]).is_file())
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
            server.xml = f'<XCUIElementTypeApplication bundleId="test.app" {attrs}>' + ''.join(
                f'<XCUIElementTypeButton label="{label}" {attrs}/>' for label in labels) + '</XCUIElementTypeApplication>'
            first = self.request(proc, {"op": "observe", "limit": 200})
            self.assertTrue(Path(first["observation"]["image"]["path"]).is_file())
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
                page = self.request(proc, {"op": "observe", "offset": page["next_offset"], "limit": 200})["observation"]["accessibility"]
            self.assertEqual(seen, labels)
            self.assertEqual(sum(route == "/source" for _, route, _ in server.requests), 1)
            self.assertEqual(sum(route == "/screenshot" for _, route, _ in server.requests), 1)
            self.assertEqual(self.request(proc, {"op": "tap", "target": first_id})["dispatch"], "acknowledged")
            self.finish(proc, base)

    def test_repeated_read_recovery_stays_pinned_and_refuses_replacement(self):
        with self.running() as (proc, server, base):
            for _ in range(3):
                server.failures["/wda/activeAppInfo"] = 1
                self.assertIn("accessibility", self.request(proc, {"op": "observe"})["observation"])
            selectors = [json.loads(line) for line in (base / "selectors").read_text().splitlines()]
            self.assertEqual(selectors, [["physical", True]] * 4)
            old_sessions = server.sessions
            (base / "identity").write_text("replacement")
            server.failures["/wda/activeAppInfo"] = 1
            self.assertIn("accessibility_error", self.request(proc, {"op": "observe"})["observation"])
            self.assertEqual(self.request(proc, {"op": "press", "button": "home"})["dispatch"], "not_sent")
            self.assertEqual(server.sessions, old_sessions)
            self.assertFalse(any(path.endswith("/wda/pressButton") for _, path, _ in server.requests))
            self.finish(proc, base)

    def test_image_only_masking_reusable_coordinates_and_optional_read_failure(self):
        with self.running(images=True) as (proc, server, base):
            server.failures["/source"] = 20
            server.image = png(16, 32)
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
            server.size = {"width": 16, "height": 8}
            self.assertEqual(self.request(proc, {"op": "tap", "space": "image", "x": 6, "y": 8})["dispatch"], "not_sent")
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
