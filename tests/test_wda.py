from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest

from openclaw_iphone.errors import WDASetupError, WDAUnavailable
from openclaw_iphone.wda import WDAClient, WDARunConfig, build_xcodebuild_command, find_xcode_container, iproxy_command, parse_ready


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png"


class FakeWDAClient(WDAClient):
    def __init__(self, responses: dict[str, bytes]) -> None:
        super().__init__(url="http://wda.test", timeout=1)
        self.responses = responses

    def _request(self, path: str, *, method: str = "GET", payload: dict | None = None) -> bytes:
        return self.responses[path]


class RecordingWDAClient(WDAClient):
    def __init__(self) -> None:
        super().__init__(url="http://wda.test", timeout=1)
        self.posts: list[tuple[str, dict]] = []
        self.requests: list[tuple[str, str, dict | None]] = []

    def _json_post(self, path: str, payload: dict) -> dict:
        self.posts.append((path, payload))
        if path == "/session":
            return {"sessionId": "session-123", "value": {}}
        return {"value": None}

    def _request(self, path: str, *, method: str = "GET", payload: dict | None = None) -> bytes:
        self.requests.append((method, path, payload))
        return b'{"value": null}'


class SelectiveFailingBackWDAClient(RecordingWDAClient):
    def __init__(self, *, fail_wda_back: bool, fail_session_back: bool) -> None:
        super().__init__()
        self.fail_wda_back = fail_wda_back
        self.fail_session_back = fail_session_back

    def _json_post(self, path: str, payload: dict) -> dict:
        if path == "/wda/back" and self.fail_wda_back:
            raise WDAUnavailable("WDA POST /wda/back failed with HTTP 404")
        if path == "/session/session-123/back" and self.fail_session_back:
            raise WDAUnavailable("WDA POST /session/session-123/back failed with HTTP 404")
        return super()._json_post(path, payload)


class WDATests(unittest.TestCase):
    def test_status_reads_ready_from_value(self) -> None:
        client = FakeWDAClient({"/status": b'{"value":{"ready":true}}'})

        status = client.status()

        self.assertEqual(status.url, "http://wda.test")
        self.assertTrue(status.ready)
        self.assertTrue(status.reachable)

    def test_connection_reset_is_unavailable(self) -> None:
        import unittest.mock

        client = WDAClient(url="http://wda.test", timeout=1)
        with unittest.mock.patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")):
            with self.assertRaises(WDAUnavailable):
                client.status()

    def test_parse_ready_supports_legacy_status_zero(self) -> None:
        self.assertTrue(parse_ready({"status": 0}))

    def test_source_accepts_json_value(self) -> None:
        client = FakeWDAClient({"/source": b'{"value":"<App />"}'})

        self.assertEqual(client.source(), "<App />")

    def test_source_accepts_raw_text(self) -> None:
        client = FakeWDAClient({"/source": b"<App />"})

        self.assertEqual(client.source(), "<App />")

    def test_screenshot_accepts_json_base64_value(self) -> None:
        payload = json.dumps({"value": base64.b64encode(PNG_BYTES).decode("ascii")}).encode("utf-8")
        client = FakeWDAClient({"/screenshot": payload})

        self.assertEqual(client.screenshot(), PNG_BYTES)

    def test_screenshot_accepts_raw_png(self) -> None:
        client = FakeWDAClient({"/screenshot": PNG_BYTES})

        self.assertEqual(client.screenshot(), PNG_BYTES)

    def test_screenshot_rejects_unknown_shape(self) -> None:
        client = FakeWDAClient({"/screenshot": b'{"value":{"not":"image"}}'})

        with self.assertRaises(WDAUnavailable):
            client.screenshot()

    def test_screenshot_rejects_invalid_base64(self) -> None:
        client = FakeWDAClient({"/screenshot": b'{"value":"not base64"}'})

        with self.assertRaises(WDAUnavailable):
            client.screenshot()

    def test_locked_reads_boolean_value(self) -> None:
        client = FakeWDAClient({"/wda/locked": b'{"value":true}'})

        self.assertTrue(client.locked())

    def test_locked_returns_none_for_unknown_shape(self) -> None:
        client = FakeWDAClient({"/wda/locked": b'{"value":"unknown"}'})

        self.assertIsNone(client.locked())

    def test_unlock_posts_wda_unlock(self) -> None:
        client = RecordingWDAClient()

        client.unlock()

        self.assertEqual(client.posts, [("/wda/unlock", {})])

    def test_lock_posts_wda_lock(self) -> None:
        client = RecordingWDAClient()

        client.lock()

        self.assertEqual(client.posts, [("/wda/lock", {})])

    def test_tap_posts_w3c_touch_action_and_deletes_session(self) -> None:
        client = RecordingWDAClient()

        client.tap(12.5, 44)

        self.assertEqual(client.posts[0], ("/session", {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}))
        self.assertEqual(
            client.posts[1],
            (
                "/session/session-123/actions",
                {
                    "actions": [
                        {
                            "type": "pointer",
                            "id": "finger1",
                            "parameters": {"pointerType": "touch"},
                            "actions": [
                                {"type": "pointerMove", "duration": 0, "x": 12.5, "y": 44},
                                {"type": "pointerDown", "button": 0},
                                {"type": "pause", "duration": 100},
                                {"type": "pointerUp", "button": 0},
                            ],
                        }
                    ]
                },
            ),
        )
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_type_text_posts_w3c_key_actions_and_deletes_session(self) -> None:
        client = RecordingWDAClient()

        client.type_text("hi")

        self.assertEqual(client.posts[0], ("/session", {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}))
        self.assertEqual(
            client.posts[1],
            (
                "/session/session-123/actions",
                {
                    "actions": [
                        {
                            "type": "key",
                            "id": "keyboard1",
                            "actions": [
                                {"type": "keyDown", "value": "h"},
                                {"type": "keyUp", "value": "h"},
                            ],
                        }
                    ]
                },
            ),
        )
        self.assertEqual(
            client.posts[3],
            (
                "/session/session-123/actions",
                {
                    "actions": [
                        {
                            "type": "key",
                            "id": "keyboard1",
                            "actions": [
                                {"type": "keyDown", "value": "i"},
                                {"type": "keyUp", "value": "i"},
                            ],
                        }
                    ]
                },
            ),
        )
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None), ("DELETE", "/session/session-123", None)])

    def test_clear_text_posts_repeated_backspace_actions(self) -> None:
        client = RecordingWDAClient()

        client.clear_text(max_chars=2)

        self.assertEqual(client.posts[0], ("/session", {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}))
        self.assertEqual(
            client.posts[1],
            (
                "/session/session-123/actions",
                {
                    "actions": [
                        {
                            "type": "key",
                            "id": "keyboard1",
                            "actions": [
                                {"type": "keyDown", "value": "\ue003"},
                                {"type": "keyUp", "value": "\ue003"},
                            ],
                        }
                    ]
                },
            ),
        )
        self.assertEqual(len(client.requests), 2)

    def test_press_button_posts_name_and_duration(self) -> None:
        client = RecordingWDAClient()

        client.press_button("home", duration=0.2)

        self.assertEqual(client.posts, [("/wda/pressButton", {"name": "home", "duration": 0.2})])

    def test_back_posts_wda_back(self) -> None:
        client = RecordingWDAClient()

        client.back()

        self.assertEqual(client.posts, [("/wda/back", {})])

    def test_back_falls_back_to_session_back(self) -> None:
        client = SelectiveFailingBackWDAClient(fail_wda_back=True, fail_session_back=False)

        client.back()

        self.assertEqual(
            client.posts,
            [
                ("/session", {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}),
                ("/session/session-123/back", {}),
            ],
        )
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_back_raises_clean_error_when_all_wda_routes_fail(self) -> None:
        client = SelectiveFailingBackWDAClient(fail_wda_back=True, fail_session_back=True)

        with self.assertRaisesRegex(WDAUnavailable, "WDA back is unavailable on this runner"):
            client.back()

        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_open_url_posts_webdriver_url_and_deletes_session(self) -> None:
        client = RecordingWDAClient()

        client.open_url("instagram://user?username=creator")

        self.assertEqual(client.posts[0], ("/session", {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}))
        self.assertEqual(client.posts[1], ("/session/session-123/url", {"url": "instagram://user?username=creator"}))
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_drag_posts_absolute_coordinates(self) -> None:
        client = RecordingWDAClient()

        client.drag(10, 20, 30, 40, duration=0.4)

        self.assertEqual(
            client.posts,
            [
                (
                    "/wda/dragfromtoforduration",
                    {"fromX": 10, "fromY": 20, "toX": 30, "toY": 40, "duration": 0.4},
                )
            ],
        )

    def test_find_xcode_container_prefers_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "WebDriverAgent.xcworkspace"
            project = root / "WebDriverAgent.xcodeproj"
            workspace.mkdir()
            project.mkdir()

            self.assertEqual(find_xcode_container(root), ("-workspace", workspace))

    def test_build_xcodebuild_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "WebDriverAgent.xcodeproj"
            project.mkdir()

            command = build_xcodebuild_command(
                WDARunConfig(
                    device_id="device-id",
                    wda_path=root,
                    destination_timeout=12,
                    development_team="TEAM123",
                    runner_bundle_id="com.example.WebDriverAgentRunner",
                    allow_provisioning_updates=True,
                )
            )

            self.assertEqual(
                command,
                [
                    "xcodebuild",
                    "test",
                    "-project",
                    str(project),
                    "-scheme",
                    "WebDriverAgentRunner",
                    "-configuration",
                    "Debug",
                    "-destination",
                    "id=device-id",
                    "-destination-timeout",
                    "12",
                    "-allowProvisioningUpdates",
                    "DEVELOPMENT_TEAM=TEAM123",
                    "PRODUCT_BUNDLE_IDENTIFIER=com.example.WebDriverAgentRunner",
                ],
            )

    def test_find_xcode_container_requires_project_or_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(WDASetupError):
                find_xcode_container(Path(tmp))

    def test_iproxy_command_fails_cleanly_when_missing(self) -> None:
        # This host currently has no iproxy; if future hosts do, the command shape
        # remains covered by integration use.
        import shutil

        if shutil.which("iproxy"):
            self.skipTest("iproxy is installed on this host")

        with self.assertRaises(WDASetupError):
            iproxy_command("device-id")

    def test_iproxy_command_shape_when_binary_exists(self) -> None:
        import unittest.mock

        with unittest.mock.patch("shutil.which", return_value="/usr/bin/iproxy"):
            self.assertEqual(
                iproxy_command("device-id", local_port=8101, device_port=8100),
                ["/usr/bin/iproxy", "--udid", "device-id", "8101:8100"],
            )


if __name__ == "__main__":
    unittest.main()
