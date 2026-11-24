from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openclaw_iphone.errors import WDASetupError, WDAUnavailable, WDAUnsupportedCommand
from openclaw_iphone.observations import parse_observation
from openclaw_iphone.wda import WDAClient, WDARunConfig, build_xcodebuild_command, find_xcode_container, parse_ready


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

    def _send(self, path: str, *, method: str, payload: dict | None, timeout: float) -> bytes:
        if method == "POST":
            self.posts.append((path, payload))
            if path == "/session":
                return b'{"sessionId":"session-123","value":{}}'
        else:
            self.requests.append((method, path, payload))
        return b'{"value":null}'


class SelectiveFailingBackWDAClient(RecordingWDAClient):
    def __init__(self, *, fail_wda_back: bool, fail_session_back: bool) -> None:
        super().__init__()
        self.fail_wda_back = fail_wda_back
        self.fail_session_back = fail_session_back

    def _json_post(self, path: str, payload: dict) -> dict:
        if path == "/wda/back" and self.fail_wda_back:
            raise WDAUnsupportedCommand("WDA POST /wda/back failed with HTTP 404")
        if path == "/session/session-123/back" and self.fail_session_back:
            raise WDAUnsupportedCommand("WDA POST /session/session-123/back failed with HTTP 404")
        return super()._json_post(path, payload)


class WDATests(unittest.TestCase):
    def test_status_reads_ready_from_value(self) -> None:
        client = FakeWDAClient({"/status": b'{"value":{"ready":true}}'})

        status = client.status()

        self.assertEqual(status.url, "http://wda.test")
        self.assertTrue(status.ready)

    def test_connection_reset_is_unavailable(self) -> None:
        import unittest.mock

        client = WDAClient(url="http://wda.test", timeout=1)
        from openclaw_iphone.transport import TransportFailure
        with unittest.mock.patch("openclaw_iphone.wda.exchange", side_effect=TransportFailure("transport", "connect")):
            with self.assertRaises(WDAUnavailable):
                client.status()

    def test_parse_ready_supports_legacy_status_zero(self) -> None:
        self.assertTrue(parse_ready({"status": 0}))

    def test_source_accepts_json_value(self) -> None:
        client = FakeWDAClient({"/source": b'{"value":"<App />"}'})

        self.assertEqual(client.source(), "<App />")

    def test_source_compact_route_leaves_default_raw(self) -> None:
        from unittest.mock import Mock
        compact = ("/source?format=xml&excluded_attributes=visible,accessible,nativeAccessibilityElement,"
                   "index,placeholderValue,nativeFrame,minValue,maxValue,customActions,type")
        client = WDAClient(url="http://wda.test", timeout=1)
        client._send = Mock(return_value=b'<App />')
        self.assertEqual(client.source(), "<App />")
        self.assertEqual(client.source(compact=True), "<App />")
        self.assertEqual([call.args[0] for call in client._send.call_args_list], ["/source", compact])
        self.assertTrue(all(call.kwargs["method"] == "GET" for call in client._send.call_args_list))
        self.assertEqual(client.metrics.summary()["counts"], {"wda GET /source": 2})

    def test_source_from_a_restarted_or_foreign_wda_session_is_a_lost_session(self) -> None:
        from openclaw_iphone.errors import WDATransportUnavailable
        client = FakeWDAClient({})
        client._session.identifier = "ours"
        for active in (b'"ours"', None):  # Matching, or no sessionId in the response at all.
            client.responses["/source"] = b'{"value":"<App />"' + (b',"sessionId":' + active if active else b"") + b"}"
            self.assertEqual(client.source(), "<App />")
        for active in (b"null", b'"theirs"'):  # WDA restarted, or another client reset the settings.
            client.responses["/source"] = b'{"value":"<App />","sessionId":' + active + b"}"
            with self.assertRaises(WDATransportUnavailable) as lost:
                client.source()
            self.assertEqual(lost.exception.category, "session_lost")
        client._session.identifier = None
        self.assertEqual(client.source(), "<App />")  # Sessionless callers have nothing to compare.

    def test_compact_xml_retains_observation_semantics(self) -> None:
        attributes = ('accessible="true" nativeAccessibilityElement="noop" index="3" '
                      'placeholderValue="hint" traits="0" nativeFrame="none" '
                      'minValue="0" maxValue="1" customActions="none" type="TextField"')
        raw = ('<XCUIElementTypeApplication name="Test" bundleId="test.app" visible="true" enabled="true" '
               'x="0" y="0" width="400" height="800"><XCUIElementTypeTextField '
               'label="Input" visible="true" enabled="true" focused="true" '
               'x="1" y="50" width="100" height="30" ' + attributes + '/>'
               '<XCUIElementTypeSecureTextField value="SECRET" visible="false" '
               'enabled="true" x="2" y="90" width="100" height="30" ' + attributes + '/>'
               '</XCUIElementTypeApplication>')
        reduced = raw.replace(' ' + attributes, '')
        def parse(xml: str):
            return parse_observation(xml, generation=1, device_udid="device",
                                     captured_at="now", started=1, finished=2)
        full, compact = parse(raw), parse(reduced)
        self.assertEqual(full.secure, compact.secure)
        self.assertTrue(compact.secure)  # Hidden secure nodes still constrain projection.
        self.assertEqual([(e.role, e.name, e.label, e.value, e.visible, e.enabled,
                           e.focused, e.bounds, e.path) for e in full.elements],
                         [(e.role, e.name, e.label, e.value, e.visible, e.enabled,
                           e.focused, e.bounds, e.path) for e in compact.elements])
        self.assertIsNone(compact.elements[1].value)
        self.assertEqual(compact.elements[1].bounds, (1.0, 50.0, 100.0, 30.0))
        self.assertTrue(compact.elements[1].focused)

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

        self.assertIsNone(client.tap(12.5, 44)["value"])

        self.assertEqual(
            client.posts[0],
            (
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "shouldWaitForQuiescence": False,
                            "eventloopIdleDelaySec": 0,
                            "disableAutomaticScreenshots": True,
                        },
                        "firstMatch": [{}],
                    }
                },
            ),
        )
        self.assertEqual(client.posts[1][0], "/session/session-123/appium/settings")
        self.assertEqual(client.posts[1][1]["settings"]["accessibilityDeadline"], 5.0)
        self.assertEqual(
            client.posts[2],
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
                                {"type": "pause", "duration": 25},
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

        with patch("openclaw_iphone.wda.time.monotonic", return_value=10), \
             patch("openclaw_iphone.wda.time.sleep") as sleep:
            self.assertIsNone(client.type_text("hé🙂", frequency=15)["value"])
        self.assertEqual([call.args for call in sleep.call_args_list], [(1 / 15,), (1 / 15,)])

        self.assertEqual(
            client.posts[0],
            (
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "shouldWaitForQuiescence": False,
                            "eventloopIdleDelaySec": 0,
                            "disableAutomaticScreenshots": True,
                        },
                        "firstMatch": [{}],
                    }
                },
            ),
        )
        self.assertEqual(
            client.posts[2],
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
        self.assertEqual(client.posts[3][1]["actions"][0]["actions"], [
            {"type": "keyDown", "value": "é"}, {"type": "keyUp", "value": "é"}])
        self.assertEqual(client.posts[4][1]["actions"][0]["actions"], [
            {"type": "keyDown", "value": "🙂"}, {"type": "keyUp", "value": "🙂"}])
        self.assertEqual(len(client.posts), 5)
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_clear_text_clears_only_active_element(self) -> None:
        from unittest.mock import Mock
        client = RecordingWDAClient()
        client._json_request = Mock(return_value={"value": {"ELEMENT": "field-1"}})
        client.clear_text()
        client._json_request.assert_called_once_with("/session/session-123/element/active")
        self.assertEqual(client.posts[2], ("/session/session-123/element/field-1/clear", {}))
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_press_button_posts_name_and_duration(self) -> None:
        client = RecordingWDAClient()

        self.assertIsNone(client.press_button("home", duration=0.2)["value"])

        self.assertEqual(client.posts[2], ("/session/session-123/wda/pressButton", {"name": "home", "duration": 0.2}))
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_back_posts_wda_back(self) -> None:
        client = RecordingWDAClient()

        self.assertIsNone(client.back()["value"])

        self.assertEqual(client.posts, [("/wda/back", {})])
        self.assertEqual(client.requests, [])  # No fallback/source or session after direct success.

    def test_back_falls_back_to_session_back(self) -> None:
        client = SelectiveFailingBackWDAClient(fail_wda_back=True, fail_session_back=False)

        client.back()

        self.assertEqual([path for path, _ in client.posts],
                         ["/session", "/session/session-123/appium/settings", "/session/session-123/back"])
        self.assertEqual(client.posts[-1][1], {})
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_back_raises_clean_error_when_all_wda_routes_fail(self) -> None:
        client = SelectiveFailingBackWDAClient(fail_wda_back=True, fail_session_back=True)

        with self.assertRaisesRegex(WDAUnavailable, "WDA back is unavailable on this runner"):
            client.back()

        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_open_url_posts_webdriver_url_and_deletes_session(self) -> None:
        client = RecordingWDAClient()

        client.open_url("instagram://user?username=creator")

        self.assertEqual(
            client.posts[0],
            (
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "shouldWaitForQuiescence": False,
                            "eventloopIdleDelaySec": 0,
                            "disableAutomaticScreenshots": True,
                        },
                        "firstMatch": [{}],
                    }
                },
            ),
        )
        self.assertEqual(client.posts[2], ("/session/session-123/url", {"url": "instagram://user?username=creator"}))
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_drag_posts_w3c_touch_action_and_deletes_session(self) -> None:
        client = RecordingWDAClient()

        self.assertIsNone(client.drag(10, 20, 30, 40, duration=0.4)["value"])

        self.assertEqual(
            client.posts[0],
            (
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "shouldWaitForQuiescence": False,
                            "eventloopIdleDelaySec": 0,
                            "disableAutomaticScreenshots": True,
                        },
                        "firstMatch": [{}],
                    }
                },
            ),
        )
        self.assertEqual(
            client.posts[2],
            (
                "/session/session-123/actions",
                (
                    {
                        "actions": [
                            {
                                "type": "pointer",
                                "id": "finger1",
                                "parameters": {"pointerType": "touch"},
                                "actions": [
                                    {"type": "pointerMove", "duration": 0, "x": 10, "y": 20},
                                    {"type": "pointerDown", "button": 0},
                                    {"type": "pointerMove", "duration": 400, "x": 30, "y": 40},
                                    {"type": "pointerUp", "button": 0},
                                ],
                            }
                        ]
                    }
                ),
            ),
        )
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

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

    def test_build_xcodebuild_command_build_for_testing_and_test_without_building(self) -> None:
        from openclaw_iphone.wda import find_xctestrun

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "WebDriverAgent.xcodeproj"
            project.mkdir()
            derived = root / "derived"
            products = derived / "Build" / "Products"
            products.mkdir(parents=True)
            xctestrun = products / "WebDriverAgentRunner_iphoneos18.0-arm64.xctestrun"
            xctestrun.write_text("<plist/>", encoding="utf-8")

            self.assertEqual(find_xctestrun(derived), xctestrun)

            build_cmd = build_xcodebuild_command(
                WDARunConfig(
                    device_id="device-id",
                    wda_path=root,
                    destination_timeout=15,
                    development_team="TEAM123",
                    runner_bundle_id="com.example.WDA",
                    allow_provisioning_updates=True,
                    allow_provisioning_device_registration=True,
                    derived_data_path=derived,
                    build_only=True,
                )
            )
            self.assertEqual(build_cmd[:2], ["xcodebuild", "build-for-testing"])
            self.assertIn("-derivedDataPath", build_cmd)
            self.assertIn(str(derived), build_cmd)
            self.assertIn("-allowProvisioningDeviceRegistration", build_cmd)

            run_cmd = build_xcodebuild_command(
                WDARunConfig(
                    device_id="device-id",
                    wda_path=root,
                    destination_timeout=15,
                    xctestrun=xctestrun,
                )
            )
            self.assertEqual(
                run_cmd,
                [
                    "xcodebuild",
                    "test-without-building",
                    "-xctestrun",
                    str(xctestrun),
                    "-destination",
                    "id=device-id",
                    "-destination-timeout",
                    "15",
                ],
            )

    def test_build_wda_failure_includes_keychain_hint(self) -> None:
        from openclaw_iphone.wda import build_wda

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "WebDriverAgent.xcodeproj").mkdir()
            fake = type("R", (), {"returncode": 65})()
            with patch("subprocess.run", return_value=fake):
                with self.assertRaises(WDASetupError) as err:
                    build_wda(WDARunConfig(device_id="device-id", wda_path=root, derived_data_path=root / "derived"))
            self.assertIn("errSecInternalComponent", str(err.exception))

    def test_pasteboard_decodes_base64_and_foreground_swaps_runner(self) -> None:
        from openclaw_iphone.wda import read_only_request

        self.assertTrue(read_only_request("POST", "/session/s1/wda/getPasteboard"))

        class PasteboardWDA(WDAClient):
            def __init__(self) -> None:
                super().__init__(url="http://wda.test", timeout=5)
                self.foreground = "com.burbn.instagram"
                self.activations: list[str] = []
                self.sessions_created = 0
                self.clip = "https://www.instagram.com/reel/abc123/"

            def _send(self, path: str, *, method: str, payload: dict | None, timeout: float) -> bytes:
                if path == "/session" and method == "POST":
                    self.sessions_created += 1
                    return b'{"sessionId":"s1","value":{}}'
                if path == "/session/s1" and method == "DELETE":
                    return b'{"value":null}'
                if path == "/wda/activeAppInfo":
                    return json.dumps({"value": {"bundleId": self.foreground}}).encode()
                if path == "/session/s1/wda/apps/activate":
                    self.foreground = payload["bundleId"]
                    self.activations.append(self.foreground)
                    return b'{"value":null}'
                if path == "/session/s1/wda/getPasteboard":
                    if self.foreground.endswith(".xctrunner"):
                        return json.dumps({"value": base64.b64encode(self.clip.encode()).decode()}).encode()
                    return b'{"value":""}'
                return b'{"value":null}'

        client = PasteboardWDA()
        result = client.read_pasteboard(
            runner_bundle_id="com.example.WebDriverAgentRunner",
            settle_seconds=0.0,
        )
        self.assertEqual(result["text"], "https://www.instagram.com/reel/abc123/")
        self.assertTrue(result["swapped_app"])
        self.assertEqual(result["restored_app"], "com.burbn.instagram")
        self.assertFalse(result["restore_warning"])
        self.assertEqual(
            client.activations,
            ["com.example.WebDriverAgentRunner.xctrunner", "com.burbn.instagram"],
        )
        # Standalone read_pasteboard uses a single WDA session across probe, swap, read, and restore.
        self.assertEqual(client.sessions_created, 1)

        # Invalid base64 raises WDAUnavailable.
        bad = FakeWDAClient({
            "/session": b'{"sessionId":"s1","value":{}}',
            "/session/s1/appium/settings": b'{"value":null}',
            "/session/s1/wda/getPasteboard": b'{"value":"!!!bad!!!"}',
            "/session/s1": b'{"value":null}',
        })
        with self.assertRaises(WDAUnavailable):
            bad.pasteboard()


if __name__ == "__main__":
    unittest.main()
