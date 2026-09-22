from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from openclaw_iphone.connection import TaskConnection
from openclaw_iphone.control_lock import control_lock
from openclaw_iphone.devicectl import Device
from openclaw_iphone.errors import DeviceLocked, DeviceSelectionError, OpenClawIPhoneError, WDAOutcomeUnknown, WDAUnavailable
from openclaw_iphone.execution import Budget, TaskStopped
from openclaw_iphone.runner import Runner
from openclaw_iphone.wda import WDAClient


class TransportTests(unittest.TestCase):
    def test_bulk_has_constant_request_count_at_http_boundary(self):
        for text in ("a", "hé🙂" * 10):
            client = WDAClient(url="http://wda.test")
            routes = []
            def respond(request, timeout):
                path = urlsplit(request.full_url).path
                routes.append((request.method, path))
                payload = {"sessionId": "one", "value": {}} if path == "/session" else {"value": False if path == "/wda/locked" else None}
                return io.BytesIO(json.dumps(payload).encode())
            with patch.object(client.opener, "open", side_effect=respond):
                client.type_text_bulk(text)
            self.assertEqual(routes, [("GET", "/wda/locked"), ("POST", "/session"),
                                      ("POST", "/session/one/appium/settings"),
                                      ("POST", "/session/one/wda/keys"), ("DELETE", "/session/one")])

    def test_explicit_null_value_is_empty_but_missing_value_is_unknown(self):
        client = self.client()
        client._json_request = Mock(return_value={"value": None})
        self.assertEqual(client.element_value("ref"), "")
        client._json_request.return_value = {}
        with self.assertRaises(WDAUnavailable):
            client.element_value("ref")

    def client(self):
        client = WDAClient(url="http://wda.test")
        client.locked = Mock(return_value=False)
        client._create_session = Mock(return_value="one")
        client._delete_session = Mock()
        client._json_post = Mock(return_value={"value": None})
        return client

    def test_nested_and_child_deadline_borrow_one_session(self):
        client = self.client()
        with client.session():
            child = client.with_deadline(5)
            self.assertLessEqual(child.with_deadline(50).deadline, child.deadline)
            child.tap(1, 2)
            client.type_text_bulk("hé🙂")
            client.type_text("ab")
            client._delete_session.assert_not_called()
        client._create_session.assert_called_once()
        client._delete_session.assert_called_once_with("one")
        client._json_post.assert_any_call("/session/one/wda/keys", {"value": ["hé🙂"]})

    def test_bulk_unknown_never_falls_back_and_cleanup_keeps_primary(self):
        client = self.client()
        client._json_post.side_effect = [{"value": None}, WDAOutcomeUnknown("primary")]
        client._delete_session.side_effect = TaskStopped("expired")
        with self.assertLogs("openclaw_iphone.wda"), self.assertRaisesRegex(WDAOutcomeUnknown, "primary"):
            client.type_text_bulk("private")
        client._json_post.assert_called_with("/session/one/wda/keys", {"value": ["private"]})
        self.assertEqual(client._json_post.call_count, 2)
        self.assertTrue(client._session.cleanup_failed)

    def test_cancel_between_characters_reports_partial_typing_and_never_replays(self):
        client = self.client()
        client.budget = Budget.seconds(3)
        def post(path, payload):
            if path.endswith("/actions"):
                client.budget.cancelled.set()
            return {"value": None}
        client._json_post.side_effect = post
        with self.assertRaisesRegex(WDAOutcomeUnknown, "1 acknowledged characters"):
            client.type_text("ab", frequency=100)
        self.assertEqual(client._json_post.call_count, 2)

    def test_typing_checks_lock_before_session_creation(self):
        for method in ("type_text", "type_text_bulk"):
            client = self.client()
            client.locked.return_value = None
            with self.assertRaises(DeviceLocked):
                getattr(client, method)("text")
            client._create_session.assert_not_called()

    def test_cancelled_budget_starts_no_network_or_subprocess_and_no_count(self):
        budget = Budget.seconds(10)
        budget.cancelled.set()
        client = WDAClient(url="http://wda.test")
        runner = Runner()
        client.budget = runner.budget = budget
        with patch.object(client.opener, "open") as network, patch("subprocess.run") as process:
            for action in (client.status, lambda: runner.run(["private-argument"]), lambda: budget.sleep(1)):
                with self.assertRaises(TaskStopped):
                    action()
            network.assert_not_called()
            process.assert_not_called()
        self.assertFalse(client.metrics.counts)
        self.assertFalse(runner.metrics.counts)

    def test_metrics_strip_ids_and_never_record_payloads(self):
        client = WDAClient(url="http://wda.test")
        with patch.object(client, "_send", return_value=b'{"value":null}'):
            client._json_post("/session/private-session/element/private-element/value", {"value": ["secret"]})
        summary = str(client.metrics.summary())
        self.assertNotIn("private", summary)
        self.assertNotIn("secret", summary)
        self.assertEqual(client.metrics.counts["wda POST /session/:id/element/:id/value"], 1)


class ConnectionTests(unittest.TestCase):
    def test_ownership_cached_setup_and_same_udid_recovery(self):
        ctl = Mock()
        ctl.runner = Runner()
        original_metrics = ctl.runner.metrics
        ctl.select_device.return_value = Device("phone", "core", "connected", "iPhone", "physical")
        ctl.coredevice_wda_url.return_value = ("http://wda.test", None)
        wda = TransportTests().client()
        wda.is_ready = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.connection.WDAClient", return_value=wda):
            lock = Path(tmp) / "control.lock"
            task = TaskConnection(ctl, seconds=10, lock_path=lock)
            with task:
                for _ in range(3):
                    self.assertIs(task.require_active(), wda)
                ctl.select_device.assert_called_once()
                with self.assertRaises(OpenClawIPhoneError), control_lock(lock):
                    pass
                task.invalidate()
                task.recover_read()
                ctl.select_device.assert_called_with("physical")
                task.invalidate(uncertain=True)
                with self.assertRaises(TaskStopped):
                    task.recover_read()
                with self.assertRaises(TaskStopped):
                    task.require_active()
            with control_lock(lock):
                pass
            self.assertIsNone(ctl.runner.budget)
            self.assertIs(ctl.runner.metrics, original_metrics)
            with self.assertRaises(TaskStopped):
                task.__enter__()

    def test_recovery_refuses_changed_physical_device(self):
        ctl = Mock()
        ctl.runner = Runner()
        ctl.select_device.side_effect = [Device("one", "core", "connected", "iPhone", "one"),
                                         Device("two", "core", "connected", "iPhone", "two")]
        ctl.coredevice_wda_url.return_value = ("http://wda.test", None)
        wda = TransportTests().client()
        wda.is_ready = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.connection.WDAClient", return_value=wda):
            with TaskConnection(ctl, lock_path=Path(tmp) / "lock") as task:
                task.invalidate()
                with self.assertRaises(DeviceSelectionError):
                    task.recover_read()
                self.assertFalse(task.valid)
                self.assertEqual(ctl.coredevice_wda_url.call_count, 1)


if __name__ == "__main__":
    unittest.main()
