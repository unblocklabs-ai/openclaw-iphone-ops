from __future__ import annotations

import unittest
import subprocess
from unittest.mock import patch

from openclaw_iphone.devicectl import DeviceCtl
from openclaw_iphone.errors import CommandFailed
from openclaw_iphone.runner import Runner, shell_quote


class RunnerTests(unittest.TestCase):
    def test_shell_quote_leaves_safe_values_unquoted(self) -> None:
        self.assertEqual(shell_quote("com.burbn.instagram"), "com.burbn.instagram")

    def test_shell_quote_quotes_spaces(self) -> None:
        self.assertEqual(shell_quote("Pearl's iPhone"), "'Pearl'\"'\"'s iPhone'")

    def test_coredevice_timeout_reports_fixed_phase_without_private_arguments(self) -> None:
        runner = Runner(timeout=3)
        command = ["xcrun", "devicectl", "device", "info", "details", "--device", "private-id", "--json-output", "/private/path"]
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(command, 3)) as process:
            with self.assertRaises(CommandFailed) as caught:
                runner.run(command)
        self.assertTrue(caught.exception.timed_out)
        self.assertIn("devicectl device details timed out after", str(caught.exception))
        self.assertIn("limit 3s", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("private", str(runner.metrics.summary()))
        self.assertEqual(runner.metrics.counts["devicectl"], 1)
        self.assertEqual(runner.metrics.counts["devicectl device details"], 1)
        process.assert_called_once()

    def test_device_list_timeout_does_not_continue_to_other_phases(self) -> None:
        client = DeviceCtl(timeout=2)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["xcrun", "devicectl"], 2)) as process:
            with self.assertRaises(CommandFailed) as caught:
                client.select_device("private-id")
        self.assertIn("devicectl list devices timed out", str(caught.exception))
        self.assertNotIn("private-id", str(caught.exception))
        self.assertEqual(process.call_count, 1)
        self.assertEqual(client.runner.metrics.counts["devicectl list devices"], 1)

    def test_coredevice_non_timeout_errors_keep_actionable_cause(self) -> None:
        command = ["xcrun", "devicectl", "device", "info", "lockState", "--device", "device-id"]
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(command, 69, "", "You must agree to the Xcode license")):
            with self.assertRaises(CommandFailed) as caught:
                Runner().run(command)
        self.assertIn("You must agree to the Xcode license", str(caught.exception))
        self.assertEqual(caught.exception.returncode, 69)

        with patch("subprocess.run", side_effect=FileNotFoundError("xcrun unavailable")):
            with self.assertRaises(CommandFailed) as caught:
                Runner().run(command)
        self.assertIn("Could not start xcrun: xcrun unavailable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
