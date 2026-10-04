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
            body, status = {"value": {"error": "unknown error", "message": "SERVER-PRIVATE"}}, 500
        else:
            status = 200
            value = None
            if path == "/status":
                value = {"ready": True}
            elif path == "/wda/locked":
                value = False
            elif path == "/wda/activeAppInfo":
                value = {"bundleId": "test.app", "pid": 1}
            elif path == "/source":
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
            elif path.endswith("/clear"):
                self.server.text = ""
            elif path.endswith("/wda/keys") or path.endswith("/value"):
                self.server.text += "".join(payload["value"])
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
    def running(self, *, images=False, seconds=2):
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

    def test_idle_rejected_frames_and_many_requests_do_not_end_ownership(self):
        with self.running(seconds=0.5) as (proc, server, base):
            time.sleep(0.7)
            with self.assertRaises(OpenClawIPhoneError), control_lock(base / ".openclaw/iphone/control.lock"):
                pass
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

    def test_repeated_read_recovery_stays_pinned_and_refuses_replacement(self):
        with self.running() as (proc, server, base):
            for _ in range(3):
                server.failures["/source"] = 1
                self.assertIn("accessibility", self.request(proc, {"op": "observe"})["observation"])
            selectors = [json.loads(line) for line in (base / "selectors").read_text().splitlines()]
            self.assertEqual(selectors, [["physical", True]] * 4)
            old_sessions = server.sessions
            (base / "identity").write_text("replacement")
            server.failures["/source"] = 1
            self.assertEqual(self.request(proc, {"op": "observe"})["observation"], {"accessibility_error": "unavailable"})
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
            self.assertEqual(result["observation"], {"image_error": "unavailable"})
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
            self.assertEqual(result["observation"], {"image_error": "unavailable"})
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
            JsonLineEmitter(writer)({"status": "still_usable"})
            self.assertEqual(json.loads(os.read(reader, 1024))["status"], "still_usable")
        finally:
            os.close(reader)
            os.close(writer)
