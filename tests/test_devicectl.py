from __future__ import annotations

import unittest

from pathlib import Path
from unittest.mock import Mock

from openclaw_iphone.devicectl import App, Device, DeviceCtl, _app_from_json, _device_from_json, find_list, url_host
from openclaw_iphone.errors import DeviceLocked


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
        client.list_devices = Mock(  # type: ignore[method-assign]
            return_value=(
                [
                    Device(
                        name="Pearl's iPhone",
                        identifier="coredevice-id",
                        state="connected",
                        udid="physical-udid",
                    )
                ],
                Path("/tmp/devices.json"),
            )
        )

        self.assertEqual(client.select_device("physical-udid").identifier, "coredevice-id")

    def test_require_unlocked_returns_artifact_when_passcode_not_required(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({"result": {"passcodeRequired": False}}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        self.assertEqual(client.require_unlocked("device-id"), Path("/tmp/lock.json"))

    def test_require_unlocked_raises_when_passcode_required(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({"result": {"passcodeRequired": True}}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        with self.assertRaises(DeviceLocked):
            client.require_unlocked("device-id")

    def test_require_unlocked_raises_when_lock_state_missing(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        with self.assertRaisesRegex(DeviceLocked, "unknown lock-state response"):
            client.require_unlocked("device-id")

    def test_require_unlocked_raises_when_result_is_not_object(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({"result": "unknown"}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        with self.assertRaisesRegex(DeviceLocked, "unknown lock-state response"):
            client.require_unlocked("device-id")

    def test_require_unlocked_raises_when_passcode_required_not_boolean(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({"result": {"passcodeRequired": "false"}}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        with self.assertRaisesRegex(DeviceLocked, "boolean passcodeRequired"):
            client.require_unlocked("device-id")


if __name__ == "__main__":
    unittest.main()
