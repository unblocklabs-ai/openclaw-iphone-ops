from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import Mock, patch

from openclaw_iphone import cli
from openclaw_iphone.control_lock import control_lock
from openclaw_iphone.devicectl import App, Device, DeviceCtl
from openclaw_iphone.errors import AppNotFound, CommandFailed, DeviceLocked, DeviceSelectionError, WDAOutcomeUnknown, WDAUnavailable, WDAUnsupportedCommand
from openclaw_iphone.evidence import artifact_path, write_private
from openclaw_iphone.instagram_context import capture_instagram_context, parse_instagram_source
from openclaw_iphone.instagram_ops import build_discovery_candidate, pregnancy_evidence, verify_discovery_handle, verify_handles
from openclaw_iphone.runner import Runner
from openclaw_iphone.ui import UIController
from openclaw_iphone.wda import WDAClient, find_xcode_container


def profile_source(handle: str, *, visible: str = "true", y: int = 29) -> str:
    return f'''<XCUIElementTypeApplication bundleId="com.burbn.instagram">
      <XCUIElementTypeStaticText name="{handle}" label="{handle}" visible="{visible}" y="{y}" />
      <XCUIElementTypeButton name="user-detail-header-followers" value="123 followers" />
    </XCUIElementTypeApplication>'''


class EvidenceTests(unittest.TestCase):
    def test_private_unique_files_even_with_permissive_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.umask(0)
            try:
                first, second = artifact_path("screen", base=tmp), artifact_path("screen", base=tmp)
                write_private(first, "private")
                write_private(second, "private")
            finally:
                os.umask(previous)
            self.assertNotEqual(first.parent, second.parent)
            self.assertEqual(first.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)

    def test_existing_file_and_symlink_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "existing"
            target.write_text("keep")
            link = Path(tmp) / "link"
            link.symlink_to(target)
            for path in (target, link):
                with self.assertRaises(FileExistsError):
                    write_private(path, "replace")
            self.assertEqual(target.read_text(), "keep")

    def test_context_capture_is_private_and_unique(self):
        client = Mock()
        client.source.return_value = profile_source("creator")
        client.screenshot.return_value = b"png"
        with tempfile.TemporaryDirectory() as tmp:
            captures = [capture_instagram_context(client, output_dir=tmp) for _ in range(2)]
            self.assertNotEqual(captures[0].manifest, captures[1].manifest)
            for capture in captures:
                for path in (capture.screenshot, capture.source, capture.manifest):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for prefix in ("../escape", "/absolute", "", "a/b"):
                with self.assertRaises(ValueError):
                    capture_instagram_context(client, output_dir=tmp, prefix=prefix)


class ActionSafetyTests(unittest.TestCase):
    def client(self, *, cleanup_error=True, action_error=None):
        client = WDAClient(url="http://wda.test")
        client.locked = Mock(return_value=False)
        client._create_session = Mock(return_value="one")
        client._delete_session = Mock(side_effect=WDAUnavailable("cleanup") if cleanup_error else None)
        client._json_post = Mock(return_value={"value": None}, side_effect=action_error)
        return client

    def test_cleanup_cannot_mask_success_or_primary_error(self):
        for action in (lambda c: c.tap(1, 2), lambda c: c.open_url("instagram://user?username=a"), lambda c: c.type_text("hi")):
            with self.subTest(action=action):
                client = self.client()
                with self.assertLogs("openclaw_iphone.wda", level="WARNING"):
                    self.assertEqual(action(client), {"value": None})
                client = self.client(action_error=WDAOutcomeUnknown("primary"))
                with self.assertLogs("openclaw_iphone.wda", level="WARNING"), self.assertRaises(WDAOutcomeUnknown):
                    action(client)

    def test_session_back_cleanup_does_not_trigger_ui_fallback(self):
        client = self.client()
        client._json_post.side_effect = [WDAUnsupportedCommand("unsupported"), {"value": None}]
        with self.assertLogs("openclaw_iphone.wda", level="WARNING"):
            UIController(client).back()
        self.assertEqual(client._json_post.call_count, 2)

    def test_ambiguous_back_failure_never_retries_or_taps(self):
        client = self.client(action_error=WDAOutcomeUnknown("timeout"))
        with self.assertRaises(WDAOutcomeUnknown):
            UIController(client).back()
        client._create_session.assert_not_called()
        self.assertEqual(client._json_post.call_count, 1)

    def test_back_rejects_unsafe_or_ambiguous_controls(self):
        for controls in (
            '<XCUIElementTypeButton name="Back up now" visible="true" x="1" y="2" width="20" height="20"/>',
            '<XCUIElementTypeButton name="Close account" visible="true" x="1" y="2" width="20" height="20"/>',
            '<XCUIElementTypeStaticText name="Back" visible="true" x="1" y="2" width="20" height="20"/>',
            '<XCUIElementTypeButton name="Back" visible="true" enabled="false" x="1" y="2" width="20" height="20"/>',
            '<XCUIElementTypeButton name="Back" visible="true" x="1" y="2" width="20" height="20"/>' * 2,
        ):
            client = Mock()
            client.back.side_effect = WDAUnsupportedCommand("missing")
            client.source.return_value = f"<App>{controls}</App>"
            with self.subTest(controls=controls), self.assertRaises(WDAUnavailable):
                UIController(client).back()
            client.tap.assert_not_called()

    def test_locked_or_unknown_screen_blocks_mutations(self):
        for locked in (True, None):
            for action in (lambda c: c.tap(1, 2), lambda c: c.open_url("test:"), lambda c: c.back(), lambda c: c.clear_text()):
                client = self.client()
                client.locked.return_value = locked
                with self.assertRaises(DeviceLocked):
                    action(client)
                client._json_post.assert_not_called()

    def test_protocol_error_at_http_200_is_not_success(self):
        client = WDAClient(url="http://wda.test")
        client._request = Mock(return_value=b'{"value":{"error":"unknown error","message":"sensitive text"}}')
        with self.assertRaises(WDAOutcomeUnknown) as raised:
            client._json_post("/actions", {})
        self.assertNotIn("sensitive text", str(raised.exception))
        client._request.return_value = b'{"status":9,"value":{}}'
        with self.assertRaises(WDAUnsupportedCommand):
            client._json_post("/back", {})

    def test_transport_failure_exposes_unknown_action_outcome(self):
        client = WDAClient(url="http://wda.test")
        with patch.object(client.opener, "open", side_effect=TimeoutError()):
            with self.assertRaises(WDAOutcomeUnknown):
                client._json_post("/actions", {})

    def test_workflow_deadline_caps_requests_and_blocks_expired_request(self):
        client = WDAClient(url="http://wda.test", timeout=30)
        with patch("openclaw_iphone.wda.time.monotonic", return_value=100):
            bounded = client.with_deadline(5)
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"value":true}')))
        response.__exit__ = Mock(return_value=False)
        with patch.object(client.opener, "open", return_value=response) as request:
            with patch("openclaw_iphone.wda.time.monotonic", return_value=103):
                bounded.locked()
                self.assertEqual(request.call_args.kwargs["timeout"], 2)
            with patch("openclaw_iphone.wda.time.monotonic", return_value=105), self.assertRaises(WDAUnavailable):
                bounded.locked()
            self.assertEqual(request.call_count, 1)
        self.assertIsNone(client.deadline)


class IdentityTests(unittest.TestCase):
    def test_search_query_alone_is_not_personal_topical_evidence(self):
        self.assertEqual(pregnancy_evidence("pregnancy", {"source_tag": "pregnancy", "source_evidence": [{"label": "Video by unrelated.creator"}]}, {}, {}), [])

    def test_parser_requires_visible_unambiguous_header(self):
        for visible, y in (("false", 29), ("true", 500)):
            profile = parse_instagram_source(profile_source("wrong", visible=visible, y=y))["current_profile"]
            self.assertNotIn("username", profile)
        source = profile_source("first").replace("</XCUIElementTypeApplication>", '<XCUIElementTypeStaticText name="second" label="second" visible="true" y="30"/></XCUIElementTypeApplication>')
        self.assertNotIn("username", parse_instagram_source(source)["current_profile"])
        wrapped = "<AppiumAUT>" + profile_source("creator") + "</AppiumAUT>"
        self.assertEqual(parse_instagram_source(wrapped)["current_profile"]["username"], "creator")

    def test_wrong_profile_is_reported_and_never_attributed(self):
        client = Mock()
        client.with_deadline.return_value = client
        client.screenshot.return_value = b"png"
        client.source.return_value = profile_source("wrong.creator")
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.instagram_ops.time.sleep"):
            result = verify_discovery_handle(client, "wanted.creator", output_dir=tmp, prefix="test", deadline_seconds=5, max_steps=4)
            handles = verify_handles(client, ["wanted.creator"], output_dir=tmp)
        self.assertEqual(result["status"], "identity_mismatch")
        self.assertEqual(handles.payload["handles"][0]["status"], "identity_mismatch")
        self.assertEqual(result["observed_handle"], "wrong.creator")
        candidate = build_discovery_candidate("pregnancy", {"handle": "wanted.creator"}, {**result, "profile": {"username": "wrong.creator", "followers": "123", "bio": "pregnancy"}})
        self.assertFalse(candidate["deep_link_verified"])
        self.assertIsNone(candidate["follower_count"])
        self.assertIsNone(candidate["bio"])
        self.assertEqual(candidate["result_bucket"], "rejected_or_ambiguous")
        client.tap.assert_not_called()
        client.type_text.assert_not_called()


class DeviceAndSetupTests(unittest.TestCase):
    def test_wda_override_cannot_select_different_device(self):
        from openclaw_iphone.config import IPhoneConfig
        args = argparse.Namespace(url="http://wrong.test", device="dedicated")
        client = Mock()
        client.select_device.return_value = Device("Phone", "id", "connected")
        client.coredevice_wda_url.return_value = ("http://right.test", Path("unused"))
        with patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})), patch("openclaw_iphone.cli.client_from_args", return_value=client):
            with self.assertRaisesRegex(ValueError, "does not match"):
                cli.resolve_wda_url_from_args(args)

    def test_unlock_does_not_claim_success_for_unknown_or_locked_screen(self):
        for locked in (True, None):
            client = Mock()
            client.locked.return_value = locked
            with patch("openclaw_iphone.cli.wda_client_from_args", return_value=client), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.handle_wda_unlock(argparse.Namespace(verify=False)), 1)

    def test_status_returns_failure_for_unknown_or_not_ready(self):
        from openclaw_iphone.wda import WDAStatus
        with tempfile.TemporaryDirectory() as tmp:
            for ready in (False, None):
                client = Mock()
                client.status.return_value = WDAStatus("http://test", {}, ready)
                args = argparse.Namespace(output=None, evidence_dir=tmp)
                with patch("openclaw_iphone.cli.wda_client_from_args", return_value=client), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.handle_wda_status(args), 1)

    def test_concurrent_workflow_fails_without_actions_then_lock_releases(self):
        from openclaw_iphone.errors import OpenClawIPhoneError
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.lock"
            with control_lock(path):
                with self.assertRaisesRegex(OpenClawIPhoneError, "Another iPhone workflow"):
                    with control_lock(path):
                        self.fail("A concurrent workflow must not enter.")
            with control_lock(path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_non_iphone_and_unknown_models_are_not_auto_selected(self):
        client = DeviceCtl()
        for model in ("iPad Pro", "", "Apple Watch"):
            client.list_devices = Mock(return_value=([Device("device", "id", "connected", model=model)], Path("unused")))
            with self.assertRaises(DeviceSelectionError):
                client.select_device()

    def test_disconnected_device_never_selected(self):
        client = DeviceCtl()
        client.list_devices = Mock(return_value=([Device("Phone", "id", "disconnected")], Path("unused")))
        for selector in (None, "Phone"):
            with self.assertRaises(DeviceSelectionError):
                client.select_device(selector)

    def test_duplicate_app_names_require_bundle_id(self):
        client = DeviceCtl()
        client.list_apps = Mock(return_value=([App("App", "one"), App("App", "two")], Path("unused")))
        with self.assertRaises(AppNotFound):
            client.find_app("phone", "App")
        self.assertEqual(client.find_app("phone", "two").bundle_identifier, "two")

    def test_selected_device_is_pinned_for_entire_cli_command(self):
        args = argparse.Namespace(device="phone")
        client = Mock()
        first = Device("phone", "first", "connected")
        client.select_device.side_effect = [first, Device("phone", "second", "connected")]
        self.assertIs(cli.selected_device(args, client), first)
        self.assertIs(cli.selected_device(args, client), first)
        client.select_device.assert_called_once()

    def test_explicit_project_directory_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "WebDriverAgent.xcodeproj"
            project.mkdir()
            self.assertEqual(find_xcode_container(project), ("-project", project))

    def test_runner_normalizes_timeout_output_and_missing_binary(self):
        error = subprocess.TimeoutExpired(["tool"], 1, output=b"partial", stderr=b"error")
        with patch("subprocess.run", side_effect=error), self.assertRaises(CommandFailed) as raised:
            Runner().run(["tool"])
        self.assertEqual(raised.exception.stdout, "partial")
        with patch("subprocess.run", side_effect=FileNotFoundError()), self.assertRaises(CommandFailed):
            Runner().run(["missing"])

    def test_cli_rejects_unbounded_numeric_values_before_actions(self):
        for timeout in ("nan", "inf", "0", "-1"):
            with contextlib.redirect_stderr(io.StringIO()), patch("openclaw_iphone.cli.wda_client_from_args") as client:
                self.assertEqual(cli.main(["ui", "wait-text", "Search", "--timeout", timeout]), 1)
                client.assert_not_called()


class ProtocolAndSnippetTests(unittest.TestCase):
    def test_real_http_cleanup_failure_preserves_action_and_no_proxy(self):
        requests = []
        release_cleanup = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, payload, *, incomplete=False):
                requests.append((self.command, self.path))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body) + (100 if incomplete else 0)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self):
                self.respond(200, {"value": False})

            def do_POST(self):
                if action_fails and self.path.endswith("/actions"):
                    self.respond(500, {"value": {"error": "unknown error"}})
                else:
                    self.respond(200, {"sessionId": "one", "value": None})

            def do_DELETE(self):
                self.respond(500, {"value": {"error": "unknown error"}}, incomplete=cleanup_body != "complete")
                if cleanup_body == "stalled":
                    release_cleanup.wait(timeout=5)

        for cleanup_body in ("complete", "truncated", "stalled"):
            for action_fails in (False, True):
                with self.subTest(cleanup_body=cleanup_body, action_fails=action_fails):
                    requests.clear()
                    release_cleanup.clear()
                    with HTTPServer(("127.0.0.1", 0), Handler) as server:
                        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                        thread.start()
                        try:
                            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                                client = WDAClient(url=f"http://127.0.0.1:{server.server_port}", timeout=1)
                                with self.assertLogs("openclaw_iphone.wda", level="WARNING"):
                                    if action_fails:
                                        with self.assertRaisesRegex(WDAOutcomeUnknown, "/session/one/actions"):
                                            client.tap(1, 2)
                                    else:
                                        self.assertEqual(client.tap(1, 2)["value"], None)
                        finally:
                            release_cleanup.set()
                            server.shutdown()
                            thread.join()
                    self.assertEqual(requests, [("GET", "/wda/locked"), ("POST", "/session"), ("POST", "/session/one/actions"), ("DELETE", "/session/one")])

    def test_app_store_requires_authorization_before_device_access(self):
        path = Path(__file__).resolve().parents[1] / "snippets/wda-app-store-install-example.py"
        spec = importlib.util.spec_from_file_location("app_store_example", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {"APP_NAME": "Example", "EXPECTED_PUBLISHER": "Publisher", "EXPECTED_BUNDLE_ID": "com.example"}, clear=True):
            with patch.object(module, "DeviceCtl") as device, self.assertRaisesRegex(ValueError, "ALLOW_INSTALL"):
                module.main()
            device.assert_not_called()
        client = Mock()
        client.with_deadline.return_value = client
        client._json_post.return_value = {"value": [{"ELEMENT": "one"}, {"ELEMENT": "two"}]}
        with self.assertRaisesRegex(WDAUnavailable, "Ambiguous"):
            module.find_element(client, "session", "name == 'Get'")
        self.assertEqual(client._json_post.call_args.args[0], "/session/session/elements")


if __name__ == "__main__":
    unittest.main()
