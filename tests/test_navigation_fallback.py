"""Optional navigation AX may fail without destroying usable read access."""
import tempfile
import unittest
from unittest.mock import patch

from openclaw_iphone.errors import WDAUnavailable
from openclaw_iphone.observations import ObservationRejected
from openclaw_iphone.planner import PlannerSession
from openclaw_iphone.tasks import TaskSpec
from test_actions import APP
from test_control_loop import TransportProbe


class NavigationFallbackTests(unittest.TestCase):
    def test_acknowledged_tap_can_inspect_pixels_without_recovery_or_more_ax(self):
        probe = TransportProbe()
        probe.warm()
        probe.screen_error = True
        with tempfile.TemporaryDirectory() as directory:
            session = PlannerSession(probe.ex, TaskSpec("Navigate", (), (), adaptive=probe.actor.scope),
                                     evidence_base=directory)
            result = session.request({"op": "act", "action": "tap", "instruction": "Next"})
            self.assertEqual((result["dispatch"], result["verification"], result["reason"],
                              result["acknowledged_substeps"]),
                             ("acknowledged", "unknown", "accessibility_unavailable", 1))
            self.assertEqual(result["validation_stage"], "post_action_verification")
            self.assertFalse(result["input_pending"])
            self.assertFalse(result["input_stopped"])
            self.assertFalse(result["observation"]["accessibility_observed"])
            self.assertTrue(probe.ex.connection.valid)
            self.assertEqual(probe.ex.connection.generation, 1)
            shot = session.request({"op": "screenshot", "redact": [[0, 0, 400, 100]]})
            self.assertEqual(shot["status"], "screenshot")
            self.assertEqual(probe.ex.connection.recoveries, 0)
            self.assertEqual(sum(path.startswith("/source?") for _, path, _ in probe.calls), 1)
            self.assertEqual(sum(path.endswith("/click") for _, path, _ in probe.calls), 1)

    def test_failed_identity_still_invalidates_and_preserves_acknowledgement(self):
        for identity in (WDAUnavailable("private identity failure"), {"bundleId": APP, "pid": None}):
            with self.subTest(identity=type(identity).__name__):
                probe = TransportProbe()
                probe.warm()
                probe.screen_error = True
                kwargs = {"side_effect": identity} if isinstance(identity, Exception) else {"return_value": identity}
                with patch.object(probe.wda, "active_app", **kwargs):
                    result = probe.actor.act({"op": "act", "action": "tap", "instruction": "Next"})
                self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "unknown"))
                self.assertEqual(result["reason"], "verification_unavailable")
                self.assertFalse(probe.ex.connection.valid)

    def test_required_reads_and_contradictory_source_do_not_degrade(self):
        probe = TransportProbe()
        probe.screen_error = True
        with self.assertRaises(WDAUnavailable):
            probe.ex.observe()
        self.assertFalse(probe.ex.connection.valid)
        probe = TransportProbe()
        with patch.object(probe.wda, "source", return_value="<invalid>"):
            with self.assertRaises(ObservationRejected):
                probe.ex.observe(optional_source=True)
        self.assertIsNone(probe.ex.latest)

    def test_explicit_postcondition_and_input_keep_required_verification(self):
        for action in ("tap", "input"):
            with self.subTest(action=action):
                probe = TransportProbe()
                probe.warm()
                probe.screen_error = True
                request = {"op": "act", "action": action, "instruction": "Next" if action == "tap" else "Input"}
                if action == "tap":
                    request["after"] = [{"kind": "absent", "app": APP,
                                         "target": {"role": "XCUIElementTypeButton", "label": "Next"}}]
                else:
                    request["text_ref"] = "text"
                with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
                    result = probe.actor.act(request)
                self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "unknown"))
                self.assertEqual(result["reason"], "verification_unavailable")
                self.assertFalse(probe.ex.connection.valid)
                self.assertEqual(result["input_pending"], action == "input")


if __name__ == "__main__":
    unittest.main()
