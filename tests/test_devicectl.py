from __future__ import annotations

import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock

from openclaw_iphone.devicectl import App, Device, DeviceCtl, _app_from_json, _device_from_json, find_list, url_host
from openclaw_iphone.errors import AppNotFound, DeviceSelectionError


class DeviceCtlJsonTests(unittest.TestCase):
    def test_find_list_finds_nested_devicectl_result(self) -> None:
        data = {"info": {"outcome": "success"}, "result": {"apps": [{"name": "Instagram"}]}}

        self.assertEqual(find_list(data, "apps"), [{"name": "Instagram"}])

    def test_device_from_common_json_keys(self) -> None:
        device = _device_from_json(
            {
                "name": "Pearl's iPhone",
                "identifier": "coredevice-id",
                "state": "connected",
                "model": "iPhone 15 Pro Max",
            }
        )

        self.assertEqual(
            device,
            Device(
                name="Pearl's iPhone",
                identifier="coredevice-id",
                state="connected",
                model="iPhone 15 Pro Max",
            ),
        )

    def test_device_from_nested_coredevice_json_keys(self) -> None:
        device = _device_from_json(
            {
                "identifier": "coredevice-id",
                "connectionProperties": {"tunnelState": "connected"},
                "deviceProperties": {"name": "Pearl's iPhone"},
                "hardwareProperties": {
                    "marketingName": "iPhone 15 Pro Max",
                    "productType": "iPhone16,2",
                    "udid": "00008130-00067DDE0C43001C",
                },
            }
        )

        self.assertEqual(
            device,
            Device(
                name="Pearl's iPhone",
                identifier="coredevice-id",
                state="connected",
                model="iPhone 15 Pro Max",
                udid="00008130-00067DDE0C43001C",
            ),
        )
        self.assertEqual(device.xcode_identifier, "00008130-00067DDE0C43001C")

    def test_app_from_common_json_keys(self) -> None:
        app = _app_from_json(
            {
                "name": "Instagram",
                "bundleIdentifier": "com.burbn.instagram",
                "version": "432.0.0",
                "bundleVersion": "983743279",
            }
        )

        self.assertEqual(
            app,
            App(
                name="Instagram",
                bundle_identifier="com.burbn.instagram",
                version="432.0.0",
                bundle_version="983743279",
            ),
        )

    def test_resolve_app_preserves_bundle_name_and_substring_precedence(self) -> None:
        apps = [App("Instagram", "com.burbn.instagram"), App("Instagram", "example.duplicate"),
                App("Other", "example.instagrampreview")]
        client = DeviceCtl()
        client.list_apps = Mock(return_value=(apps, Path("unused")))
        self.assertEqual(client.find_app("phone", "com.burbn.instagram"), apps[0])
        with self.assertRaisesRegex(AppNotFound, "matched multiple apps"):
            client.find_app("phone", "Instagram")
        self.assertEqual(client.find_app("phone", "preview"), apps[2])
        with self.assertRaisesRegex(AppNotFound, "matched multiple apps"):
            client.find_app("phone", "insta")
        for call in client.list_apps.call_args_list:
            self.assertEqual(call.args, ("phone",))

    def test_devicectl_json_is_kept_only_when_the_caller_shows_it(self) -> None:
        class Runner:
            def __init__(self) -> None:
                self.outputs: list[Path] = []

            def run(self, command: list[str]) -> None:
                output = Path(command[command.index("--json-output") + 1])
                output.write_text(json.dumps({"result": {"devices": [{"identifier": "core", "deviceProperties": {"name": "phone"}}]}}))
                self.outputs.append(output)

        with tempfile.TemporaryDirectory() as evidence:
            client = DeviceCtl(evidence_base=evidence)
            client.runner = Runner()
            devices, output = client.list_devices()
            self.assertEqual(([d.identifier for d in devices], output), (["core"], None))
            self.assertFalse(client.runner.outputs[-1].exists())
            self.assertFalse(client.runner.outputs[-1].parent.exists())  # routine checks leave no folder behind
            self.assertEqual(list(Path(evidence).iterdir()), [])
            devices, output = client.list_devices(keep=True)
            self.assertEqual(output, client.runner.outputs[-1])
            self.assertTrue(output.exists() and output.is_relative_to(evidence))

    def test_url_host_wraps_ipv6_for_urls(self) -> None:
        self.assertEqual(url_host("fdaa:8372:5daf::1"), "[fdaa:8372:5daf::1]")
        self.assertEqual(url_host("192.168.1.202"), "192.168.1.202")

    def test_coredevice_wda_url_uses_tunnel_ip(self) -> None:
        client = DeviceCtl()
        client.device_details = Mock(  # type: ignore[method-assign]
            return_value=(
                {
                    "result": {
                        "connectionProperties": {
                            "tunnelState": "connected",
                            "tunnelIPAddress": "fdaa:8372:5daf::1",
                        }
                    }
                },
                Path("/tmp/details.json"),
            )
        )

        self.assertEqual(
            client.coredevice_wda_url("device-id"),
            ("http://[fdaa:8372:5daf::1]:8100", Path("/tmp/details.json")),
        )

    def test_select_device_matches_physical_udid(self) -> None:
        client = DeviceCtl()
        client.device_details = Mock(side_effect=AssertionError("Selection must not probe availability."))
        for state in ("connected", "disconnected"):
            device = Device("phone", "core", state, "iPhone 15", "physical")
            client.list_devices = Mock(return_value=([device], Path("unused")))
            for selector in ("core", "physical", "PHYSICAL"):
                with self.subTest(state=state, selector=selector):
                    self.assertEqual(client.select_device(selector), device)
            client.list_devices.return_value = ([device, device], Path("unused"))
            with self.assertRaisesRegex(DeviceSelectionError, "multiple records"):
                client.select_device("physical")

        client.list_devices.return_value = ([Device("other", "other-core", "connected", "iPhone 15", "other-physical")], Path("unused"))
        with self.assertRaises(DeviceSelectionError):
            client.select_device("physical")


if __name__ == "__main__":
    unittest.main()
