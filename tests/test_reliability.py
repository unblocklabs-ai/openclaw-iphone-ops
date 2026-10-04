from dataclasses import replace
import contextlib
import io
import unittest
from unittest.mock import Mock, patch
from openclaw_iphone import cli
from openclaw_iphone.devicectl import Device, DeviceCtl
from openclaw_iphone.errors import DeviceLocked, DeviceSelectionError, WDAUnavailable
from openclaw_iphone.execution import Budget
from openclaw_iphone.wda import WDAClient
from test_cli_config import FakeDeviceCtl, FakeWDA

class ReconnectTests(unittest.TestCase):
    def client(self):
        ctl = DeviceCtl()
        device = Device("phone", "core", "disconnected", "iPhone 15", "physical")
        ctl.list_devices = Mock(side_effect=[([device], None), ([replace(device, state="connected")], None)])
        ctl.device_details = Mock(return_value=({"result": {"hardwareProperties": {"udid": "physical"}}}, None))
        ctl.require_unlocked = Mock()
        return ctl, device

    def test_exact_dormant_identity_probed_once_and_rechecked_under_budget(self):
        for selector in ("core", "physical", "PHYSICAL"):
            ctl, _ = self.client()
            previous = Budget.seconds(2)
            ctl.runner.budget = previous
            def probe(identifier):
                self.assertLessEqual(ctl.runner.budget.deadline, previous.deadline)
                self.assertIs(ctl.runner.budget.cancelled, previous.cancelled)
                return {"result": {"hardwareProperties": {"udid": "physical"}}}, None
            ctl.device_details.side_effect = probe
            self.assertEqual(ctl.select_device(selector).udid, "physical")
            ctl.device_details.assert_called_once_with("core")
            ctl.require_unlocked.assert_called_once_with("core")
            self.assertEqual(ctl.list_devices.call_count, 2)
            self.assertIs(ctl.runner.budget, previous)

    def test_read_only_dormant_recheck_keeps_identity_without_unlock_probe(self):
        ctl, _ = self.client()
        self.assertEqual(ctl.select_device("physical", read_only=True).udid, "physical")
        ctl.device_details.assert_called_once_with("core")
        self.assertEqual(ctl.list_devices.call_count, 2)
        ctl.require_unlocked.assert_not_called()

    def test_never_wakes_name_auto_missing_udid_or_ambiguous_identity(self):
        for kind in ("name", "auto", "missing_udid", "duplicate"):
            ctl, device = self.client()
            selector = {"name": "phone", "auto": None}.get(kind, "core")
            devices = [replace(device, udid="")] if kind == "missing_udid" else [device, device] if kind == "duplicate" else [device]
            ctl.list_devices.side_effect = None
            ctl.list_devices.return_value = devices, None
            with self.assertRaises(DeviceSelectionError):
                ctl.select_device(selector)
            ctl.device_details.assert_not_called()

    def test_changed_disconnected_or_locked_identity_never_returns_alternative(self):
        for kind in ("details", "replacement", "disconnected", "locked", "unknown_details"):
            ctl, device = self.client()
            if kind in {"details", "unknown_details"}:
                ctl.device_details.return_value = {"result": {"hardwareProperties": {"udid": "other"}}} if kind == "details" else {}, None
            elif kind == "replacement":
                ctl.list_devices.side_effect = [([device], None), ([replace(device, state="connected", udid="other")], None)]
            elif kind == "disconnected":
                ctl.list_devices.side_effect = None
                ctl.list_devices.return_value = [device], None
            else:
                ctl.require_unlocked.side_effect = DeviceLocked("locked")
            with self.assertRaises((DeviceSelectionError, DeviceLocked)):
                ctl.select_device("physical")
            self.assertIsNone(ctl.runner.budget)


class ReadHealthTests(unittest.TestCase):
    def test_screen_reads_capped_without_shortening_mutation_timeout(self):
        wda = WDAClient(url="http://wda.test", timeout=30, read_timeout=3)
        with patch.object(wda, "_send", return_value=b"{}") as send:
            for path in ("/source", "/screenshot"):
                wda._request(path)
                self.assertEqual(send.call_args.kwargs["timeout"], 3)
            wda._request("/session/one/element/ref/click", method="POST", payload={})
            self.assertEqual(send.call_args.kwargs["timeout"], 30)
            wda.budget = Budget.seconds(0.5)
            wda._request("/source")
            self.assertLessEqual(send.call_args.kwargs["timeout"], 0.5)
        for value in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                WDAClient(url="http://wda.test", read_timeout=value)

    def test_doctor_does_not_equate_ready_with_usable_source(self):
        args = cli.build_parser().parse_args(["doctor", "--check-ui"])
        wda = FakeWDA()
        wda.source = Mock(side_effect=WDAUnavailable("PRIVATE"))
        with patch("openclaw_iphone.cli.client_from_args", return_value=FakeDeviceCtl()), \
             patch("openclaw_iphone.cli.resolve_wda_url_from_args", return_value="http://wda.test"), \
             patch("openclaw_iphone.cli.WDAClient", return_value=wda), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.handle_doctor(args), 1)
        self.assertIn("screen-read-failed", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(wda.unlock_count, 0)
