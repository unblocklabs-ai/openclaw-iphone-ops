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

    def _request(self, path: str) -> bytes:
        return self.responses[path]


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
