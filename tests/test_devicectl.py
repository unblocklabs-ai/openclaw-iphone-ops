from __future__ import annotations

import unittest

from pathlib import Path
from unittest.mock import Mock

from openclaw_iphone.devicectl import App, Device, DeviceCtl, _app_from_json, _device_from_json, find_list
from openclaw_iphone.errors import DeviceLocked


class DeviceCtlJsonTests(unittest.TestCase):
    def test_find_list_finds_nested_devicectl_result(self) -> None:
        data = {"info": {"outcome": "success"}, "result": {"apps": [{"name": "Instagram"}]}}

        self.assertEqual(find_list(data, "apps"), [{"name": "Instagram"}])

    def test_device_from_common_json_keys(self) -> None:
        device = _device_from_json(
            {
                "name": "Pearl's iPhone",
                "identifier": "EEDD57B5-2EF9-52F6-BA8D-A8063C842901",
                "state": "connected",
                "model": "iPhone 15 Pro Max",
            }
        )

        self.assertEqual(
            device,
            Device(
                name="Pearl's iPhone",
                identifier="EEDD57B5-2EF9-52F6-BA8D-A8063C842901",
                state="connected",
                model="iPhone 15 Pro Max",
            ),
        )

    def test_device_from_nested_coredevice_json_keys(self) -> None:
        device = _device_from_json(
            {
                "identifier": "EEDD57B5-2EF9-52F6-BA8D-A8063C842901",
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
                identifier="EEDD57B5-2EF9-52F6-BA8D-A8063C842901",
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

    def test_require_unlocked_returns_artifact_when_passcode_not_required(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({"result": {"passcodeRequired": False}}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        self.assertEqual(client.require_unlocked("device-id"), Path("/tmp/lock.json"))

    def test_require_unlocked_raises_when_passcode_required(self) -> None:
        client = DeviceCtl()
        client.lock_state = Mock(return_value=({"result": {"passcodeRequired": True}}, Path("/tmp/lock.json")))  # type: ignore[method-assign]

        with self.assertRaises(DeviceLocked):
            client.require_unlocked("device-id")


if __name__ == "__main__":
    unittest.main()
