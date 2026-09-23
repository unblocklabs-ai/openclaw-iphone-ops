"""Bounded exploration is a mode of the existing planner, not a second controller."""
import json
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openclaw_iphone.cli import build_parser
from openclaw_iphone.errors import WDAUnavailable
from openclaw_iphone.execution import TaskStopped
from openclaw_iphone.observations import ObservationRejected
from openclaw_iphone.planner import PlannerSession
from openclaw_iphone.tasks import load_task, parse_task
from test_actions import APP
from test_control_loop import TransportProbe


def exploratory_task():
    return {"version": 1, "objective": "Inspect a synthetic screen",
            "adaptive": {"apps": [APP], "operations": ["vision_tap", "input"],
                         "inputs": {"text": "/synthetic-not-read"}},
            "limits": {"seconds": 60, "max_steps": 2}}


class ExplorationTests(unittest.TestCase):
    def test_schema_is_opt_in_and_run_stays_strict(self):
        data = exploratory_task()
        with self.assertRaises(ValueError):
            parse_task(data)
        spec = parse_task(data, explore=True)
        self.assertEqual((spec.grants, spec.success), ((), ()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            path.write_text(json.dumps(data))
            self.assertEqual(load_task(path, explore=True).success, ())
            with self.assertRaises(ValueError):
                load_task(path)
        args = build_parser().parse_args(["task", "session", "--file", "task.json", "--explore"])
        self.assertTrue(args.explore)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["task", "run", "--file", "task.json", "--explore"])

    def test_exploration_requires_scope_and_no_fixed_completion(self):
        data = exploratory_task()
        for changed in ({"adaptive": None}, {"adaptive": {"apps": [APP], "operations": []}},
                        {"grants": [{}]}, {"success": [{"kind": "app", "app": APP}]},
                        {"texts": {"secret": "inline"}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parse_task({**data, **changed}, explore=True)

    def test_masked_pixel_navigation_never_reads_ax_or_claims_completion(self):
        probe = TransportProbe()
        probe.screen_error = True
        spec = parse_task(exploratory_task(), explore=True)
        with tempfile.TemporaryDirectory() as directory:
            session = PlannerSession(probe.ex, spec, evidence_base=directory)
            shot = session.request({"op": "screenshot", "redact": [[0, 0, 400, 100]]})
            result = session.request({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})
            self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "unknown"))
            self.assertEqual(sum(path.startswith("/source?") for _, path, _ in probe.calls), 0)
            self.assertEqual(sum(path.endswith("/actions") for _, path, _ in probe.calls), 1)
            self.assertEqual(session.steps, 1)
            rejected = session.request({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})
            self.assertEqual((rejected["dispatch"], rejected["reason"]), ("not_sent", "evidence_unavailable"))

    def test_unmasked_pixels_need_ax_and_scope_still_applies(self):
        probe = TransportProbe()
        probe.screen_error = True
        session = PlannerSession(probe.ex, parse_task(exploratory_task(), explore=True))
        with self.assertRaises(WDAUnavailable):
            session.request({"op": "screenshot"})
        self.assertFalse(any(path == "/screenshot" for _, path, _ in probe.calls))
        outside = TransportProbe()
        outside.app = "unapproved.app"
        with self.assertRaises(ObservationRejected):
            PlannerSession(outside.ex, parse_task(exploratory_task(), explore=True)).request(
                {"op": "screenshot", "redact": []})
        self.assertFalse(any(path == "/screenshot" for _, path, _ in outside.calls))

    def test_semantic_input_keeps_native_empty_and_readback_path(self):
        probe = TransportProbe()
        data = exploratory_task()
        data["adaptive"]["operations"] = ["input"]
        session = PlannerSession(probe.ex, parse_task(data, explore=True))
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = session.request({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "satisfied"))
        self.assertEqual(result["readback"]["observed_characters"], 6)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in probe.calls), 1)
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in probe.calls), 1)
        self.assertEqual(session.steps, 1)
        with self.assertRaises(ValueError):
            session.request({"op": "act", "action": "tap", "instruction": "Next"})

    def test_caller_completion_is_unknown_and_step_limit_is_shared(self):
        probe = TransportProbe()
        data = exploratory_task()
        data["limits"]["max_steps"] = 1
        spec = parse_task(data, explore=True)
        with tempfile.TemporaryDirectory() as directory:
            session = PlannerSession(probe.ex, spec, evidence_base=directory)
            self.assertEqual(session.request({"op": "wait"})["reason"], "success_conditions_unconfigured")
            shot = session.request({"op": "screenshot", "redact": []})
            self.assertEqual(session.request({"op": "vision_tap", "snapshot_id": shot["snapshot_id"],
                                              "x": 2, "y": 4})["verification"], "unknown")
            with self.assertRaises(TaskStopped):
                session.request({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
            self.assertEqual(session.request({"op": "done"}),
                             {"status": "closed", "verification": "unknown", "reason": "caller_finished"})
            self.assertEqual(session.steps, 1)


if __name__ == "__main__":
    unittest.main()
