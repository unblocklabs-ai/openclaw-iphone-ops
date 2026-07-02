from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from openclaw_iphone import cli
from openclaw_iphone.config import IPhoneConfig
from openclaw_iphone.devicectl import Device
from openclaw_iphone.errors import DeviceSelectionError, WDASetupError, WDAUnavailable
from openclaw_iphone.wda import WDAStatus


class FakeDeviceCtl:
    def __init__(self, *, lock_state_payload: dict | None = None, lock_state_error: Exception | None = None) -> None:
        self.selected: str | None = None
        self.required_unlocked: str | None = None
        self.lock_state_payload = lock_state_payload or {"result": {"passcodeRequired": False}}
        self.lock_state_error = lock_state_error

    def select_device(self, requested: str | None = None) -> Device:
        self.selected = requested
        return Device(
            name="Configured iPhone",
            identifier="coredevice-id",
            state="connected",
            udid="physical-udid",
        )

    def coredevice_wda_url(self, device_id: str, *, port: int = 8100) -> tuple[str, Path]:
        return (f"http://[fdaa::1]:{port}", Path("/tmp/device-details.json"))

    def require_unlocked(self, device_id: str) -> Path:
        self.required_unlocked = device_id
        return Path("/tmp/lock-state.json")

    def lock_state(self, device_id: str) -> tuple[dict, Path]:
        if self.lock_state_error:
            raise self.lock_state_error
        return self.lock_state_payload, Path("/tmp/lock-state.json")


class FakeWDA:
    def __init__(self, *, ready: bool | None = True, locked_values: list[bool | None] | None = None) -> None:
        self.ready = ready
        self.locked_values = list(locked_values or [False])
        self.unlock_count = 0
        self.url = "http://[fdaa::1]:8100"

    def status(self) -> WDAStatus:
        return WDAStatus(url=self.url, payload={"value": {"ready": self.ready}}, ready=self.ready)

    def locked(self) -> bool | None:
        if len(self.locked_values) > 1:
            return self.locked_values.pop(0)
        return self.locked_values[0]

    def unlock(self) -> dict:
        self.unlock_count += 1
        return {"value": None}


class StatusFailingWDA:
    url = "http://[fdaa::1]:8100"

    def status(self) -> WDAStatus:
        raise WDAUnavailable("connection refused")


class LockFailingWDA(FakeWDA):
    def locked(self) -> bool | None:
        raise WDAUnavailable("lock endpoint failed")


class CLIConfigTests(unittest.TestCase):
    def test_wda_backed_commands_accept_device_override(self) -> None:
        parser = cli.build_parser()

        commands = [
            ["instagram", "capture-context"],
            ["instagram", "analyze-video", "--video", "file.mp4"],
            ["wda", "status"],
            ["wda", "locked"],
            ["wda", "unlock"],
            ["wda", "lock"],
            ["ui", "screenshot"],
            ["ui", "source"],
            ["ui", "elements"],
            ["ui", "annotated-screenshot"],
            ["ui", "tap", "--x", "1", "--y", "2"],
            ["ui", "tap-text", "Search"],
            ["ui", "wait-text", "Search"],
            ["ui", "scroll-until-text", "Search"],
            ["ui", "type", "hello"],
            ["ui", "clear-field"],
            ["ui", "drag", "--from-x", "1", "--from-y", "2", "--to-x", "3", "--to-y", "4"],
            ["ui", "press-button", "home"],
            ["ui", "back"],
        ]

        for command in commands:
            with self.subTest(command=command):
                args = parser.parse_args(command + ["--device", "Configured iPhone"])
                self.assertEqual(args.device, "Configured iPhone")

    def test_resolve_wda_url_uses_configured_device(self) -> None:
        fake = FakeDeviceCtl()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            config_path.write_text('OPENCLAW_IPHONE_DEVICE="Configured iPhone"\n', encoding="utf-8")
            args = argparse.Namespace(url=None, device=None, developer_dir=None, evidence_dir=None, timeout=30)

            with mock.patch.dict("os.environ", {"OPENCLAW_IPHONE_CONFIG": str(config_path)}, clear=True):
                with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake):
                    url = cli.resolve_wda_url_from_args(args)

        self.assertEqual(url, "http://[fdaa::1]:8100")
        self.assertEqual(fake.selected, "Configured iPhone")

    def test_explicit_wda_url_skips_coredevice_resolution(self) -> None:
        args = argparse.Namespace(url="http://wda.example:8100")

        self.assertEqual(cli.resolve_wda_url_from_args(args), "http://wda.example:8100")

    def test_wda_run_uses_host_config_for_runner_settings(self) -> None:
        fake = FakeDeviceCtl()
        with tempfile.TemporaryDirectory() as tmp:
            wda_path = Path(tmp) / "WebDriverAgent"
            wda_path.mkdir()
            config_path = Path(tmp) / "config.env"
            config_path.write_text(
                "\n".join(
                    [
                        'OPENCLAW_IPHONE_DEVICE="Configured iPhone"',
                        f'OPENCLAW_IPHONE_WDA_PATH="{wda_path}"',
                        'OPENCLAW_IPHONE_RUNNER_BUNDLE_ID="ai.example.WebDriverAgentRunner"',
                        'OPENCLAW_IPHONE_DEVELOPMENT_TEAM="TEAM123"',
                        'OPENCLAW_IPHONE_DESTINATION_TIMEOUT="60"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                wda_path=None,
                device=None,
                developer_dir=None,
                evidence_dir=None,
                timeout=30,
                scheme="WebDriverAgentRunner",
                configuration="Debug",
                destination_timeout=None,
                development_team=None,
                runner_bundle_id=None,
                allow_provisioning_updates=True,
            )

            captured = []

            def record_run(config: object) -> int:
                captured.append(config)
                return 0

            with mock.patch.dict("os.environ", {"OPENCLAW_IPHONE_CONFIG": str(config_path)}, clear=True):
                with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake):
                    with mock.patch("openclaw_iphone.cli.run_wda", side_effect=record_run):
                        with contextlib.redirect_stdout(io.StringIO()):
                            result = cli.handle_wda_run(args)

        self.assertEqual(result, 0)
        self.assertEqual(fake.selected, "Configured iPhone")
        self.assertEqual(fake.required_unlocked, "coredevice-id")
        self.assertEqual(len(captured), 1)
        config = captured[0]
        self.assertEqual(config.device_id, "physical-udid")
        self.assertEqual(config.wda_path, wda_path)
        self.assertEqual(config.destination_timeout, 60)
        self.assertEqual(config.development_team, "TEAM123")
        self.assertEqual(config.runner_bundle_id, "ai.example.WebDriverAgentRunner")
        self.assertTrue(config.allow_provisioning_updates)

    def test_watchdog_once_unlocks_locked_phone_and_verifies(self) -> None:
        fake_device = FakeDeviceCtl()
        fake_wda = FakeWDA(locked_values=[True, False])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
                with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                    with mock.patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})):
                        with contextlib.redirect_stdout(io.StringIO()):
                            result = cli.handle_watchdog_once(args)

        self.assertEqual(result, 0)
        self.assertEqual(fake_wda.unlock_count, 1)

    def test_watchdog_once_reports_structured_url_resolution_failure(self) -> None:
        fake_device = FakeDeviceCtl()
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch(
                "openclaw_iphone.cli.wda_client_from_args",
                side_effect=WDASetupError("CoreDevice tunnel is not connected"),
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_watchdog_once(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("wda-url: unknown", output)
        self.assertIn("result: wda-url-resolution-failed", output)
        self.assertIn("blocker: CoreDevice tunnel is not connected", output)

    def test_watchdog_once_reports_ready_unknown(self) -> None:
        fake_device = FakeDeviceCtl()
        fake_wda = FakeWDA(ready=None)
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_watchdog_once(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("wda-ready: unknown", output)
        self.assertIn("result: wda-ready-unknown", output)

    def test_watchdog_once_reports_structured_lock_check_failure(self) -> None:
        fake_device = FakeDeviceCtl()
        fake_wda = LockFailingWDA()
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
                with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                    with mock.patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})):
                        stdout = io.StringIO()
                        with contextlib.redirect_stdout(stdout):
                            result = cli.handle_watchdog_once(args)

        self.assertEqual(result, 1)
        self.assertIn("result: lock-check-failed", stdout.getvalue())
        self.assertIn("blocker: lock endpoint failed", stdout.getvalue())

    def test_watchdog_once_accepts_coredevice_verified_unlock_when_wda_unknown(self) -> None:
        fake_device = FakeDeviceCtl()
        fake_wda = FakeWDA(locked_values=[True, None])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_watchdog_once(args)

        self.assertEqual(result, 0)
        self.assertIn("result: verified-unlocked", stdout.getvalue())

    def test_watchdog_once_reports_conflicting_lock_signals(self) -> None:
        fake_device = FakeDeviceCtl()
        fake_wda = FakeWDA(locked_values=[True, True])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_watchdog_once(args)

        self.assertEqual(result, 1)
        self.assertIn("result: lock-state-conflict", stdout.getvalue())

    def test_watchdog_once_reports_structured_verification_failure(self) -> None:
        fake_device = FakeDeviceCtl(lock_state_error=ValueError("bad lock-state json"))
        fake_wda = FakeWDA(locked_values=[True, None])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_watchdog_once(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("passcode-required: unknown", output)
        self.assertIn("result: lock-state-failed", output)
        self.assertIn("blocker: bad lock-state json", output)

    def test_watchdog_once_rejects_unknown_verification_payload(self) -> None:
        fake_device = FakeDeviceCtl(lock_state_payload={"result": {"passcodeRequired": "unknown"}})
        fake_wda = FakeWDA(locked_values=[True, False])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
            no_verify=False,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=fake_wda):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_watchdog_once(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("passcode-required: unknown", output)
        self.assertIn("result: lock-state-unknown", output)

    def test_doctor_reports_healthy_when_unlocked_and_ready(self) -> None:
        fake_device = FakeDeviceCtl()
        fake_wda = FakeWDA(ready=True, locked_values=[False])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
        )

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
                with mock.patch("openclaw_iphone.cli.WDAClient", return_value=fake_wda):
                    with mock.patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})):
                        with contextlib.redirect_stdout(io.StringIO()):
                            result = cli.handle_doctor(args)

        self.assertEqual(result, 0)

    def test_doctor_reports_attention_required_for_unknown_lock_state(self) -> None:
        fake_device = FakeDeviceCtl(lock_state_payload={"result": {"passcodeRequired": "unknown"}})
        fake_wda = FakeWDA(ready=True, locked_values=[False])
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
        )

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
                with mock.patch("openclaw_iphone.cli.WDAClient", return_value=fake_wda):
                    with mock.patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})):
                        stdout = io.StringIO()
                        with contextlib.redirect_stdout(stdout):
                            result = cli.handle_doctor(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("passcode-required: unknown", output)
        self.assertIn("result: attention-required", output)

    def test_doctor_reports_structured_url_resolution_failure(self) -> None:
        fake_device = FakeDeviceCtl()
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
        )

        with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
            with mock.patch(
                "openclaw_iphone.cli.resolve_wda_url_from_args",
                side_effect=DeviceSelectionError("CoreDevice tunnel is not connected"),
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = cli.handle_doctor(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("wda-url: unknown", output)
        self.assertIn("result: wda-url-resolution-failed", output)
        self.assertIn("blocker: CoreDevice tunnel is not connected", output)

    def test_doctor_reports_structured_unreachable_wda(self) -> None:
        fake_device = FakeDeviceCtl()
        args = argparse.Namespace(
            device=None,
            url=None,
            developer_dir=None,
            evidence_dir=None,
            timeout=30,
        )

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("openclaw_iphone.cli.client_from_args", return_value=fake_device):
                with mock.patch("openclaw_iphone.cli.WDAClient", return_value=StatusFailingWDA()):
                    with mock.patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})):
                        stdout = io.StringIO()
                        with contextlib.redirect_stdout(stdout):
                            result = cli.handle_doctor(args)

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("wda-reachable: false", output)
        self.assertIn("wda-ready: false", output)
        self.assertIn("result: attention-required", output)
        self.assertIn("blocker: connection refused", output)


if __name__ == "__main__":
    unittest.main()
