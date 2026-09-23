"""Content-free planner response timing and sequence boundaries."""

import unittest
from dataclasses import replace
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openclaw_iphone.execution import Budget, Metrics, TaskStopped
from openclaw_iphone.planner import PlannerSession, serve
from openclaw_iphone.tasks import TaskSpec
from openclaw_iphone.observations import ObservationRejected
from test_control_loop import TransportProbe


class PlannerDiagnosticsTests(unittest.TestCase):
    def session(self, request):
        metrics = Metrics()
        connection = SimpleNamespace(metrics=metrics)
        session = SimpleNamespace(executor=SimpleNamespace(connection=connection),
                                  request=request, closed=False)
        return session, metrics

    def test_sequence_and_transport_event_indices(self):
        session, metrics = self.session(Mock(side_effect=lambda data: self._request(metrics, data)))
        replies = []
        with patch("openclaw_iphone.planner.time.monotonic", side_effect=[10.0, 10.2, 11.0, 11.1]):
            self.assertEqual(serve(session, iter([b'{"op":"observe"}', b'{"op":"done"}']), replies.append), 1)
        first, second, end = replies
        self.assertEqual([first["request_sequence"], second["request_sequence"]], [1, 2])
        self.assertEqual(first["timing"], {
            "handling_started_monotonic": 10.0,
            "handling_finished_monotonic": 10.2,
            "handling_seconds": 0.2,
            "transport_event_start": 0,
            "transport_event_end": 1,
        })
        self.assertEqual((second["timing"]["transport_event_start"],
                          second["timing"]["transport_event_end"]), (1, 2))
        self.assertEqual(end["status"], "closed")

    @staticmethod
    def _request(metrics, data):
        metrics.events.append({"operation": "wda GET /source", "seconds": 0.01, "outcome": "returned"})
        return {"status": "observed"}

    def test_invalid_request_still_has_diagnostics_without_echo(self):
        session, _ = self.session(Mock())
        replies = []
        self.assertEqual(serve(session, iter([b'{"op":"observe","op":"PRIVATE"}']), replies.append), 1)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["reason"], "invalid_request")
        self.assertEqual(replies[0]["request_sequence"], 1)
        self.assertEqual(replies[0]["timing"]["transport_event_start"], 0)
        self.assertNotIn("PRIVATE", str(replies))
        session.request.assert_not_called()

    def test_rejections_are_content_free_and_do_not_invent_dispatch_state(self):
        for supplied, expected in (("snapshot_expired", "snapshot_expired"),
                                   ("PRIVATE", "observation_rejected")):
            session, _ = self.session(Mock(side_effect=ObservationRejected("PRIVATE", code=supplied)))
            replies = []
            serve(session, iter([b'{"op":"observe"}']), replies.append)
            self.assertEqual(replies[0]["reason"], expected)
            self.assertNotIn("dispatch", replies[0])
            self.assertNotIn("PRIVATE", str(replies))

    def test_pixel_rejections_explain_exact_pre_dispatch_failure(self):
        cases = {"age": "snapshot_expired", "token": "snapshot_superseded",
                 "pid": "foreground_changed", "geometry": "geometry_changed",
                 "generation": "evidence_unavailable", "consumed": "evidence_unavailable"}
        for cause, expected in cases.items():
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as directory:
                probe = TransportProbe()
                session = PlannerSession(probe.ex, TaskSpec("Inspect", (), (), adaptive=probe.actor.scope),
                                         evidence_base=directory)
                shot = session.request({"op": "screenshot", "redact": []})
                request = {"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4}
                if cause == "age":
                    session.adaptive.visual = replace(session.adaptive.visual,
                                                       started=time.monotonic() - probe.ex.freshness - 1)
                elif cause == "token":
                    request["snapshot_id"] = "not-the-latest-token"
                elif cause == "pid":
                    probe.pid += 1
                elif cause == "geometry":
                    probe.wda.window_size = Mock(return_value=(800, 400))
                elif cause == "generation":
                    probe.ex.connection.generation += 1
                else:
                    session.adaptive.visual = None
                result = session.request(request)
                self.assertEqual((result["dispatch"], result["reason"]), ("not_sent", expected))
                self.assertFalse(any(path.endswith("/actions") for _, path, _ in probe.calls))

    def test_rejection_after_acknowledgement_never_claims_not_sent(self):
        probe = TransportProbe()
        probe.warm()
        with patch.object(probe.ex, "observe", side_effect=ObservationRejected("PRIVATE", code="foreground_changed")):
            result = probe.actor.act({"op": "act", "action": "tap", "instruction": "Next"})
        self.assertEqual((result["dispatch"], result["reason"], result["rejection_code"]),
                         ("acknowledged", "verification_unavailable", "foreground_changed"))
        self.assertEqual(result["acknowledged_substeps"], 1)
        self.assertNotIn("PRIVATE", str(result))

    def test_grant_free_session_skips_offers_and_never_claims_success(self):
        executor = Mock()
        executor.connection.budget = Budget.seconds(10)
        executor.connection.metrics = Metrics()
        executor.offer_blockers = ()
        executor.stopped = False
        session = PlannerSession(executor, TaskSpec("Explore", (), ()))
        observation = Mock()
        observation.compact.return_value = {"snapshot_id": "opaque"}
        self.assertEqual(session.view(observation)["actions"], [])
        executor.offers.assert_not_called()
        self.assertEqual(session.request({"op": "wait"}), {
            "status": "blocked", "verification": "unknown", "reason": "success_conditions_unconfigured"})
        executor.wait.assert_not_called()
        self.assertEqual(session.request({"op": "done"}), {
            "status": "closed", "verification": "unknown", "reason": "caller_finished"})
        self.assertTrue(session.closed)
        executor.verify.assert_not_called()

        session = PlannerSession(executor, TaskSpec("Explore", (), ()))
        replies = []
        self.assertEqual(serve(session, iter([b'{"op":"done"}']), replies.append), 1)
        self.assertEqual(replies[0]["status"], "closed")
        self.assertEqual(replies[0]["verification"], "unknown")

    def test_request_limit_has_timing_without_exception_text(self):
        session, _ = self.session(Mock(side_effect=TaskStopped("PRIVATE deadline")))
        replies = []
        self.assertEqual(serve(session, iter([b'{"op":"observe"}']), replies.append), 1)
        self.assertEqual(replies[0]["reason"], "deadline_or_limit")
        self.assertEqual(replies[0]["request_sequence"], 1)
        self.assertIn("handling_seconds", replies[0]["timing"])
        self.assertNotIn("PRIVATE", str(replies))


if __name__ == "__main__":
    unittest.main()
