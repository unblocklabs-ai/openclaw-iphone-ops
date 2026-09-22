import unittest
from unittest.mock import Mock, patch

from openclaw_iphone.actions import Condition
from openclaw_iphone.errors import DeviceLocked, WDAOutcomeUnknown
from openclaw_iphone.execution import TaskStopped
from openclaw_iphone.observations import ObservationRejected, keypad_points
from openclaw_iphone.ui import UIController
from test_actions import APP, BUTTON, executor, snapshot, source
from test_wda import RecordingWDAClient


class FastInputTests(unittest.TestCase):
    def test_one_settings_update_per_owned_session_and_cleanup_on_setup_error(self):
        client = RecordingWDAClient()
        with client.session():
            client.tap(10, 20)
            with client.session():
                client.tap(30, 40)
        settings = [payload for path, payload in client.posts if path.endswith("/appium/settings")]
        self.assertEqual(settings, [{"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}}])
        client = RecordingWDAClient()
        original = client._json_post
        def post(path, payload):
            if path.endswith("/settings"):
                raise WDAOutcomeUnknown("settings failed")
            return original(path, payload)
        client._json_post = post
        with self.assertRaises(WDAOutcomeUnknown):
            client.tap(10, 20)
        self.assertFalse(any(path.endswith("/actions") for path, _ in client.posts))
        self.assertEqual(client.requests, [("DELETE", "/session/session-123", None)])

    def test_six_touches_are_nonoverlapping_paths_in_one_request(self):
        client = RecordingWDAClient()
        client.locked = Mock(return_value=False)
        points = [(10, 20), (30, 40)] * 3
        client.tap_sequence(points)
        requests = [payload for path, payload in client.posts if path.endswith("/actions")]
        self.assertEqual(len(requests), 1)
        sources = requests[0]["actions"]
        self.assertEqual(len({s["id"] for s in sources}), 6)
        for i, (s, point) in enumerate(zip(sources, points)):
            actions = s["actions"]
            offset = actions.pop(0)["duration"] if i else 0
            self.assertEqual(offset, i * 125)
            self.assertEqual(actions, [
                {"type": "pointerMove", "duration": 0, "x": point[0], "y": point[1]},
                {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 50},
                {"type": "pointerUp", "button": 0}])
        client.locked.assert_called_once()

    def test_invalid_expired_locked_or_unknown_sequence_never_replays(self):
        for points in ([], [(1, 2)] * 33, [(float("nan"), 2)], [(-1, 2)]):
            client = RecordingWDAClient()
            with self.assertRaises(ValueError):
                client.tap_sequence(points)
            self.assertEqual(client.posts, [])
        client = RecordingWDAClient()
        client.locked = Mock(return_value=True)
        with self.assertRaises(DeviceLocked):
            client.tap_sequence([(1, 2)])
        self.assertEqual(client.posts, [])
        client = RecordingWDAClient()
        client.timeout = 0.1
        with self.assertRaises(TaskStopped):
            client.tap_sequence([(1, 2)] * 6)
        self.assertFalse(any(path.endswith("/actions") for path, _ in client.posts))
        client = RecordingWDAClient()
        original = client._json_post
        calls = []
        def post(path, payload):
            if path.endswith("/actions"):
                calls.append(path)
                raise WDAOutcomeUnknown("unknown")
            return original(path, payload)
        client._json_post = post
        with self.assertRaises(WDAOutcomeUnknown):
            client.tap_sequence([(1, 2)] * 6)
        self.assertEqual(len(calls), 1)

    def test_keypad_requires_all_digits_from_one_unambiguous_keyboard(self):
        key = '<XCUIElementTypeKey name="{}" visible="true" enabled="true" x="{}" y="600" width="30" height="30"/>'
        first, second = key.format("1", 0), key.format("2", 40)
        valid = snapshot(source(extra=f'<XCUIElementTypeKeyboard>{first}{second}</XCUIElementTypeKeyboard>'))
        self.assertEqual(keypad_points(valid, "121"), [(15, 615), (55, 615), (15, 615)])
        for xml in (first + second,
                    f'<XCUIElementTypeKeyboard visible="false">{first}{second}</XCUIElementTypeKeyboard>',
                    f'<XCUIElementTypeKeyboard>{first}{second}{first}</XCUIElementTypeKeyboard>',
                    f'<XCUIElementTypeKeyboard>{first}</XCUIElementTypeKeyboard><XCUIElementTypeKeyboard>{second}</XCUIElementTypeKeyboard>'):
            with self.assertRaises(ObservationRejected):
                keypad_points(snapshot(source(extra=xml)), "12")

    def test_no_condition_wait_returns_one_observation_without_polling(self):
        ex, wda = executor([])
        ex.connection.budget.sleep = Mock()
        state, _ = ex.wait(())
        self.assertEqual(state, "unknown")
        wda.source.assert_called_once()
        ex.connection.budget.sleep.assert_not_called()

    def test_verification_short_circuits_before_expensive_irrelevant_read(self):
        ex, wda = executor([])
        current = ex.observe()
        state = ex.verify(current, (Condition("app", "other.app"), Condition("focused", APP, BUTTON)))
        self.assertEqual(state, "unsatisfied")
        wda.find_elements.assert_not_called()

    def test_source_wait_reads_immediately_and_reuses_last_snapshot(self):
        client = Mock()
        client.with_deadline.return_value = client
        client.source.side_effect = ["ready", "loading", "ready"]
        ui = UIController(client)
        with patch("openclaw_iphone.ui.time.sleep") as sleep:
            self.assertEqual(ui.wait_source(lambda s: s == "ready", timeout=2), "ready")
            sleep.assert_not_called()
            self.assertEqual(ui.wait_source(lambda s: s == "ready", timeout=2), "ready")
            sleep.assert_called_once()
        client.source.return_value = "not ready"
        client.source.side_effect = None
        self.assertEqual(ui.wait_source(lambda s: False, timeout=0), "not ready")
