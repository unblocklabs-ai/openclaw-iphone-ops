"""Offline request budgets and safety contracts for the consolidated loop.

Real WDAClient routing/session/timeout plumbing; synthetic responses only.
These are request counts, not a physical-device latency benchmark.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openclaw_iphone.actions import Condition, Executor, Grant
from openclaw_iphone.adaptive import AdaptiveAct, parse_scope
from openclaw_iphone.connection import TaskConnection
from openclaw_iphone.errors import WDAOutcomeUnknown, WDAUnavailable
from openclaw_iphone.execution import TaskStopped
from openclaw_iphone.observations import ObservationRejected
from openclaw_iphone.planner import PlannerSession, serve
from openclaw_iphone.tasks import TaskSpec, run_task
from openclaw_iphone.wda import WDAClient
from test_actions import APP, BUTTON, FIELD, executor, snapshot, source
from test_adaptive import png


class TransportProbe:
    def __init__(self, grants=(), *, missing_value=False):
        ex, _ = executor(())
        connection = TaskConnection(Mock(), seconds=60)
        connection.device = ex.connection.device
        connection.generation, connection.valid = 1, True
        self.wda = WDAClient(url="http://never-called.invalid")
        self.wda.budget = connection.budget
        self.wda._session.identifier = "audit"
        connection.wda = self.wda
        self.ex = Executor(connection, tuple(grants), texts={"text": "121212"}, verification_seconds=1)
        self.actor = AdaptiveAct(self.ex, parse_scope({"apps": [APP],
            "operations": ["tap", "input", "keypad", "vision_tap"], "inputs": {"text": "/synthetic-not-read"}}))
        self.value, self.button = "", "Next"
        self.app, self.pid = APP, 1
        self.calls = []
        self.locators = []
        self.missing_value = missing_value
        self.screen_error = False
        self.wda._send = self.send

    def xml(self):
        keys = '<XCUIElementTypeKeyboard>' + ''.join(
            f'<XCUIElementTypeKey name="{d}" visible="true" enabled="true" x="{i * 40}" y="600" width="30" height="30"/>'
            for i, d in enumerate("12")) + '</XCUIElementTypeKeyboard>'
        xml = source(value=self.value, button_label=self.button, extra=keys)
        return xml.replace(f'value="{self.value}"', '') if self.missing_value else xml

    def send(self, path, *, method, payload, timeout):
        self.calls.append((method, path, timeout))
        if path.startswith("/source?"):
            if self.screen_error:
                raise WDAUnavailable("synthetic AX failure")
            value = self.xml()
        elif path == "/wda/locked": value = False
        elif path == "/wda/activeAppInfo": value = {"bundleId": self.app, "pid": self.pid}
        elif path.endswith("/wda/apps/state"): value = 4 if payload["bundleId"] == self.app else 3
        elif path.endswith("/elements"):
            self.locators.append(payload["using"])
            query = payload["value"]
            elements = snapshot(self.xml()).elements
            if payload["using"] == "predicate string":
                from openclaw_iphone.observations import Selector
                selected = [e for e in elements if e.visible is True and
                    (e.locator() == ("predicate string", query) or any(
                        Selector(e.role, name=name, label=label).locator() == ("predicate string", query)
                        for name in (None, e.name or None) for label in (None, e.label or None)))]
            elif query.startswith("//XCUIElementTypeTextField[@visible="):
                selected = [e for e in elements if e.matches(FIELD)]
            else:
                paths = [q.split("[count(")[0].split("[not(")[0] for q in query.split(" | ")]
                selected = [e for e in elements if e.xpath in paths]
            value = [{"ELEMENT": "ref" if e.role != "XCUIElementTypeKey" else f"key{e.name}"} for e in selected]
        elif path.endswith("/attribute/hittable"): value = True
        elif path.endswith("/element/active"): value = {"ELEMENT": "ref"}
        elif path.endswith("/attribute/value"): value = self.value
        elif path.endswith("/attribute/placeholderValue"): value = "Input"
        elif path.endswith("/window/size"): value = {"width": 400, "height": 800}
        elif path == "/screenshot": return png()
        else:
            value = None
            if path.endswith("/clear"): self.value = ""
            elif path.endswith("/value"): self.value = payload["value"][0]
            elif path.endswith("/click"):
                self.button = "Finished" if self.button == "Next" else "Done"
            elif path.endswith("/actions"): self.value = "121212"
        return json.dumps({"value": value}).encode()

    def warm(self):
        self.ex.observe()
        self.calls.clear()


class ControlLoopTests(unittest.TestCase):
    def test_warm_adaptive_request_budgets(self):
        for action, mode, count in (("tap", None, 7), ("input", None, 7),
                                    ("input", "replace", 8), ("keypad", None, 9)):
            with self.subTest(action=action, mode=mode):
                p = TransportProbe()
                p.warm()
                request = {"op": "act", "action": action, "instruction": "Next" if action == "tap" else "Input"}
                if action != "tap": request["text_ref"] = "text"
                if mode: request["mode"] = mode
                with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
                    result = p.actor.act(request)
                self.assertEqual(result["dispatch"], "acknowledged")
                self.assertEqual(result["verification"], "unknown" if action == "tap" else "satisfied")
                self.assertEqual(len(p.calls), count)
                self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 1)
                if action != "tap":
                    self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 1)
                    self.assertIsNotNone(p.ex.latest.elements)
                self.assertEqual(p.locators.count("xpath"), int(action == "keypad"))

    def test_input_tree_verifies_and_is_reused_by_next_action(self):
        p = TransportProbe()
        p.warm()
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(result["readback"]["observed_characters"], 6)
        self.assertEqual([path.split("?", 1)[0] for _, path, _ in p.calls[-2:]], ["/source", "/wda/activeAppInfo"])
        p.calls.clear()
        self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "acknowledged")
        self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 1)  # After tap only.
        self.assertEqual(p.calls[0][1], "/session/audit/elements")

    def test_missing_tree_value_falls_back_to_selected_reference(self):
        p = TransportProbe(missing_value=True)
        p.warm()
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 1)
        self.assertEqual(sum(path.endswith("/elements") for _, path, _ in p.calls), 1)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 2)

    def test_tree_mismatch_keeps_input_pending_until_read_only_reconcile(self):
        p = TransportProbe()
        p.warm()
        def partial(path, **kwargs):
            result = p.send(path, **kwargs)
            if path.endswith("/value") and kwargs["method"] == "POST":
                p.value = "121"
            return result
        p.wda._send = partial
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["dispatch"], result["verification"], result["input_pending"]),
                         ("acknowledged", "unsatisfied", True))
        self.assertEqual(result["readback"]["observed_characters"], 3)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 2)
        self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")
        p.value = "121212"
        p.calls.clear()
        self.assertEqual(p.actor.reconcile()["verification"], "satisfied")
        self.assertIsNone(p.actor.pending_input)
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 0)

    def test_stale_empty_tree_native_exact_proof_is_reused_by_done(self):
        p = TransportProbe()
        p.warm()
        original_xml = p.xml
        p.xml = lambda: original_xml().replace(f'value="{p.value}"', 'value=""') if p.value else original_xml()
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["dispatch"], result["verification"], result["input_pending"]),
                         ("acknowledged", "satisfied", False))
        self.assertEqual(result["readback"]["observed_characters"], 6)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 2)
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)
        view = p.actor.view(result["observation"])
        self.assertEqual(next(row["input_state"] for row in view["elements"] if row["role"] == FIELD.role), "nonempty")
        spec = TaskSpec("Synthetic exact field", (), (Condition("value", APP, FIELD, "121212"),))
        p.calls.clear()
        self.assertEqual(PlannerSession(p.ex, spec).request({"op": "done"})["status"], "completed")
        self.assertEqual(p.calls, [])

    def test_native_mismatch_recheck_rejects_post_xml_app_or_pid_switch(self):
        for switch_at in ("lookup", "value", "pid"):
            with self.subTest(switch_at=switch_at):
                p = TransportProbe()
                p.warm()
                original_xml = p.xml
                p.xml = lambda: original_xml().replace(f'value="{p.value}"', 'value=""') if p.value else original_xml()
                def switched(path, **kwargs):
                    response = p.send(path, **kwargs)
                    if p.value and ((switch_at in {"lookup", "pid"} and path.endswith("/elements"))
                                    or switch_at == "value" and path.endswith("/attribute/value")):
                        if switch_at == "pid":
                            p.pid = 2
                        else:
                            p.app, p.pid = "unapproved.app", 2
                    return response
                p.wda._send = switched
                with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
                    result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
                self.assertEqual((result["dispatch"], result["verification"], result["input_pending"]),
                                 ("acknowledged", "unknown", True))
                self.assertEqual(result["observation"].app, APP)
                self.assertIsNone(p.actor.pending_target)
                self.assertNotIn(Condition("value", APP, FIELD, "121212"), p.ex._values)
                spec = TaskSpec("Synthetic exact field", (), (Condition("value", APP, FIELD, "121212"),))
                self.assertEqual(PlannerSession(p.ex, spec).request({"op": "done"})["status"], "incomplete")
                self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")
                self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)

    def test_cross_app_tree_match_cannot_promote_native_input_proof(self):
        p = TransportProbe()
        p.warm()
        def changed_app(path, **kwargs):
            result = p.send(path, **kwargs)
            if path.endswith("/value") and kwargs["method"] == "POST":
                p.app, p.pid = "other.app", 2
            return result
        p.wda._send = changed_app
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["verification"], result["input_pending"]), ("unsatisfied", True))
        self.assertIsNone(p.actor.pending_target)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 1)
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)

    def test_missing_post_input_field_does_not_trigger_native_recheck(self):
        p = TransportProbe()
        p.warm()
        original_xml = p.xml
        p.xml = lambda: original_xml().replace("XCUIElementTypeTextField", "XCUIElementTypeOther") if p.value else original_xml()
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["verification"], result["input_pending"]), ("unsatisfied", True))
        self.assertIsNone(p.actor.pending_target)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 1)
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)

    def test_unknown_native_mismatch_read_requires_reconciliation(self):
        p = TransportProbe()
        p.warm()
        original_xml = p.xml
        p.xml = lambda: original_xml().replace(f'value="{p.value}"', 'value=""') if p.value else original_xml()
        def unreadable(path, **kwargs):
            result = p.send(path, **kwargs)
            if path.endswith("/attribute/value") and p.value:
                raise WDAUnavailable("synthetic read failure")
            return result
        p.wda._send = unreadable
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["verification"], result["input_pending"]), ("unknown", True))
        self.assertIsNone(p.actor.pending_target)
        self.assertEqual(p.actor.act({"op": "act", "action": "input", "instruction": "Input",
                                      "text_ref": "text", "mode": "replace"})["dispatch"], "not_sent")
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)
        p.wda._send = p.send
        self.assertEqual(p.actor.reconcile()["verification"], "satisfied")

    def test_duplicate_post_input_target_does_not_verify_via_old_reference(self):
        p = TransportProbe()
        p.warm()
        original_xml = p.xml
        def xml():
            tree = original_xml()
            if p.value:
                duplicate = '<XCUIElementTypeTextField label="Input" value="121212" visible="true" enabled="true" x="150" y="50" width="100" height="30"/>'
                return tree.replace('</XCUIElementTypeApplication>', duplicate + '</XCUIElementTypeApplication>')
            return tree
        p.xml = xml
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["dispatch"], result["verification"], result["input_pending"]),
                         ("acknowledged", "unknown", True))
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 1)
        self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")

    def test_post_input_source_failure_keeps_acknowledgement_and_pending(self):
        p = TransportProbe()
        p.warm()
        def failed_source(path, **kwargs):
            if path.startswith("/source?") and p.value:
                raise WDAUnavailable("synthetic AX failure")
            return p.send(path, **kwargs)
        p.wda._send = failed_source
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["dispatch"], result["verification"], result["input_pending"]),
                         ("acknowledged", "unknown", True))
        self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)
        self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")

    def test_explicit_after_remains_narrow(self):
        p = TransportProbe()
        p.warm()
        after = [{"kind": "value", "app": APP,
                  "target": {"role": "XCUIElementTypeTextField", "label": "Input"}, "value": "121212"}]
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input",
                                  "text_ref": "text", "after": after})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 0)
        self.assertIsNone(p.ex.latest.elements)

    def test_fixed_input_budgets_and_no_replay(self):
        for operation, count in (("append", 7), ("replace", 8), ("keypad", 9)):
            with self.subTest(operation=operation):
                grant = Grant(operation, APP, "Input", FIELD, text_id="text")
                p = TransportProbe((grant,))
                offer, = p.ex.offers(p.ex.observe())
                p.calls.clear()
                result = p.ex.execute(offer.id)
                self.assertEqual(result.verification, "satisfied")
                self.assertEqual(len(p.calls), count)
                self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 1)
                self.assertIsNotNone(p.ex.latest.elements)
                self.assertEqual(p.ex.execute(offer.id).dispatch, "not_sent")

    def test_two_steps_reuse_post_observation_and_done_reuses_success(self):
        middle = Condition("exists", APP, replace(BUTTON, label="Finished"))
        done = Condition("exists", APP, replace(BUTTON, label="Done"))
        grants = (Grant("tap", APP, "First", BUTTON, after=(middle,)),
                  Grant("tap", APP, "Second", middle.target, after=(done,)))
        p = TransportProbe(grants)
        spec = TaskSpec("Two screens", grants, (done,))
        self.assertEqual(run_task(p.ex, spec)["status"], "completed")
        self.assertLessEqual(len(p.calls), 16)
        self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 2)
        self.assertNotIn("xpath", p.locators)
        p.calls.clear()
        self.assertEqual(PlannerSession(p.ex, spec).request({"op": "done"})["status"], "completed")
        self.assertEqual(p.calls, [])

    def test_offer_predicates_cached_only_within_each_pass(self):
        grants = tuple(Grant("tap", APP, f"Choice {i}", BUTTON,
            before=(Condition("focused", APP, FIELD),), after=(Condition("value", APP, FIELD, "other"),)) for i in range(10))
        p = TransportProbe(grants, missing_value=True)
        p.wda.element_hittable = Mock(side_effect=AssertionError("A value read is not a hit test"))
        p.warm()
        self.assertEqual(len(p.ex.offers(p.ex.latest)), 10)
        self.assertEqual(len(p.calls), 3)
        p.value = "other"
        self.assertEqual(p.ex.offers(p.ex.latest), ())
        self.assertEqual(len(p.calls), 6)

    def test_selected_preconditions_share_reference_focus_and_empty_read(self):
        grant = Grant("append", APP, "Input", FIELD, text_id="text", before=(
            Condition("focused", APP, FIELD), Condition("value", APP, FIELD, "")))
        p = TransportProbe((grant,))
        offer, = p.ex.offers(p.ex.observe())
        p.calls.clear()
        self.assertEqual(p.ex.execute(offer.id).verification, "satisfied")
        self.assertEqual(sum(path.endswith("/elements") for _, path, _ in p.calls), 1)
        self.assertEqual(sum(path.endswith("/element/active") for _, path, _ in p.calls), 1)
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in p.calls), 1)
        self.assertLessEqual(len(p.calls), 8)

    def test_only_explicit_stale_read_re_resolves_without_replaying_input(self):
        for failure in ("stale element reference", "unknown error"):
            with self.subTest(failure=failure):
                p = TransportProbe(missing_value=True)
                p.warm()
                failed = False
                def send(path, **kwargs):
                    nonlocal failed
                    result = p.send(path, **kwargs)
                    if path.endswith("/attribute/value") and p.value and not failed:
                        failed = True
                        return json.dumps({"value": {"error": failure}}).encode()
                    return result
                p.wda._send = send
                with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
                    result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
                self.assertEqual(result["dispatch"], "acknowledged")
                stale = failure == "stale element reference"
                self.assertEqual(result["verification"], "satisfied" if stale else "unknown")
                self.assertEqual(sum(path.endswith("/elements") for _, path, _ in p.calls), 2 if stale else 1)
                self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)
                if not stale:
                    self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")

    def test_compound_input_checks_lock_once_and_restores_guard_on_failure(self):
        p = TransportProbe()
        p.warm()
        def send(path, **kwargs):
            result = p.send(path, **kwargs)
            if path.endswith("/value") and kwargs["method"] == "POST":
                return b'{"value":{"error":"unknown error"}}'
            return result
        p.wda._send = send
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "mode": "replace", "instruction": "Input", "text_ref": "text"})
        self.assertEqual((result["dispatch"], result["acknowledged_substeps"]), ("unknown", 1))
        self.assertEqual(sum(path == "/wda/locked" for _, path, _ in p.calls), 1)
        self.assertFalse(p.wda._input_checked)
        p.wda.require_unlocked()
        self.assertEqual(sum(path == "/wda/locked" for _, path, _ in p.calls), 2)

    def test_locked_device_remains_readable_but_cannot_receive_input(self):
        for lock_response in (b'{"value":true}', b'{"value":null}'):
            with self.subTest(lock_response=lock_response):
                p = TransportProbe()
                def send(path, **kwargs):
                    response = p.send(path, **kwargs)
                    return lock_response if path == "/wda/locked" else response
                p.wda._send = send
                p.warm()
                result = p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})
                self.assertEqual((result["dispatch"], result["error_type"]), ("not_sent", "DeviceLocked"))
                self.assertFalse(any(path.endswith("/click") for _, path, _ in p.calls))

    def test_native_target_is_resolved_again_after_same_app_restart(self):
        p = TransportProbe()
        p.warm()
        p.pid = 2
        result = p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})
        self.assertEqual(result["dispatch"], "acknowledged")
        self.assertEqual(result["observation"].process_id, 2)
        self.assertEqual(p.calls[0][1], "/session/audit/elements")

    def test_narrow_proof_cannot_be_used_as_a_tree_or_survive_mutation(self):
        p = TransportProbe()
        condition = Condition("value", APP, FIELD, "")
        state, obs = p.ex.wait((condition,))
        self.assertEqual(state, "satisfied")
        p.calls.clear()
        self.assertEqual(p.ex.verify(obs, (condition,)), "satisfied")
        self.assertEqual(p.calls, [])
        self.assertIsNone(obs.elements)
        self.assertEqual(p.ex.verify(obs, (Condition("absent", APP, BUTTON),)), "unknown")
        self.assertEqual(p.ex.offers(obs), ())
        p.ex.discard()
        self.assertEqual(p.ex.verify(obs, (condition,)), "unknown")

    def test_slow_capture_no_longer_repeats_and_expires_itself(self):
        p = TransportProbe()
        tick = [0.0]
        def slow(path, **kwargs):
            value = p.send(path, **kwargs)
            tick[0] += 8 if path == "/wda/activeAppInfo" else 4 if path.startswith("/source?") else 0
            return value
        p.wda._send = slow
        with patch("time.monotonic", side_effect=lambda: tick[0]):
            p.warm()  # Capture takes 12s; no duplicate app read or planner delay.
            result = p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})
        self.assertEqual(result["dispatch"], "acknowledged")
        self.assertEqual(len(p.calls), 7)

        p = TransportProbe()
        tick = [0.0]
        def late_value(path, **kwargs):
            value = p.send(path, **kwargs)
            if path.endswith("/attribute/value"):
                tick[0] += 31
            return value
        p.wda._send = late_value
        with patch("time.monotonic", side_effect=lambda: tick[0]), patch(
                "openclaw_iphone.adaptive.read_input", return_value="121212"):
            p.warm()
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["dispatch"], "not_sent")
        self.assertFalse(any(method == "POST" and path.endswith("/value") for method, path, _ in p.calls))

    def test_verification_expiry_preserves_ack_and_allows_read_only_reconciliation(self):
        for adaptive in (True, False):
            with self.subTest(adaptive=adaptive):
                grants = () if adaptive else (Grant("append", APP, "Input", FIELD, text_id="text"),)
                p = TransportProbe(grants, missing_value=True)
                p.warm()
                offer = None if adaptive else p.ex.offers(p.ex.latest)[0]
                tick = [0.0]
                def late(path, **kwargs):
                    value = p.send(path, **kwargs)
                    if path.endswith("/attribute/value") and p.value:
                        tick[0] += 1.1
                    return value
                p.wda._send = late
                with patch("time.monotonic", side_effect=lambda: tick[0]), patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
                    if adaptive:
                        result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
                    else:
                        result = vars(p.ex.execute(offer.id))
                    self.assertEqual((result["dispatch"], result["reason"]), ("acknowledged", "verification_expired"))
                    self.assertFalse(p.ex.stopped)
                    self.assertTrue(p.ex.connection.valid)
                    self.assertEqual(p.actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")
                    p.wda._send = p.send
                    self.assertEqual(p.actor.reconcile()["verification"], "satisfied")
                    self.assertIsNone(p.actor.pending_input)
                    self.assertIsNone(p.ex.pending)
                self.assertEqual(sum(path.endswith("/value") and method == "POST" for method, path, _ in p.calls), 1)

    def test_unknown_write_cannot_be_reconciled_into_permission(self):
        p = TransportProbe()
        p.warm()
        def unknown(path, **kwargs):
            if path.endswith("/value") and kwargs["method"] == "POST":
                raise WDAOutcomeUnknown("synthetic unknown write")
            return p.send(path, **kwargs)
        p.wda._send = unknown
        with patch("openclaw_iphone.adaptive.read_input", return_value="121212"):
            result = p.actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["dispatch"], "unknown")
        with self.assertRaises(TaskStopped): p.actor.reconcile()
        self.assertTrue(p.ex.stopped)
        self.assertTrue(p.ex.connection.uncertain)

    def test_screenshot_only_fallback_never_requires_ax(self):
        p = TransportProbe()
        p.screen_error = True
        with tempfile.TemporaryDirectory() as directory:
            p.actor.evidence_base = directory
            shot = p.actor.screenshot(redact=[])
            self.assertEqual(len(p.calls), 3)
            self.assertEqual(Path(shot["path"]).stat().st_mode & 0o777, 0o600)
            result = p.actor.vision_tap({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})
            self.assertEqual(result["dispatch"], "acknowledged")
            self.assertEqual(len(p.calls), 7)
            self.assertFalse(any(path.startswith("/source?") for _, path, _ in p.calls))
            with self.assertRaises(ObservationRejected):
                p.actor.vision_tap({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})

    def test_failed_source_recover_then_masked_vision_tap_without_more_xml(self):
        p = TransportProbe()
        spec = TaskSpec("Visual fallback", (), (Condition("app", APP),), adaptive=p.actor.scope)
        p.screen_error = True
        with tempfile.TemporaryDirectory() as directory:
            session = PlannerSession(p.ex, spec, evidence_base=directory)
            with self.assertRaises(WDAUnavailable):
                session.request({"op": "observe"})
            self.assertFalse(p.ex.connection.valid)
            def reacquire():
                p.ex.connection.valid = True
                p.ex.connection.generation += 1
            p.ex.connection.recover_read = Mock(side_effect=reacquire)
            recovered = session.request({"op": "recover_read"})
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(recovered["observation"]["app"], APP)
            shot = session.request({"op": "screenshot", "redact": [[0, 0, 400, 100]]})
            result = session.request({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})
            self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "unknown"))
            p.ex.connection.recover_read.assert_called_once_with()
            self.assertEqual(sum(path.startswith("/source?") for _, path, _ in p.calls), 1)
            self.assertEqual(sum(path.endswith("/actions") for _, path, _ in p.calls), 1)

    def test_visual_fallback_requires_explicit_masks_without_ax_and_rejects_identity_change(self):
        p = TransportProbe()
        with tempfile.TemporaryDirectory() as directory:
            p.actor.evidence_base = directory
            with self.assertRaises(ObservationRejected): p.actor.screenshot()
            self.assertEqual(list(Path(directory).iterdir()), [])
            shot = p.actor.screenshot(redact=[[0, 0, 400, 100]])
            p.pid = 2
            with self.assertRaises(ObservationRejected):
                p.actor.vision_tap({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})
            self.assertFalse(any(path.endswith("/actions") for _, path, _ in p.calls))

    def test_target_query_keeps_ancestor_geometry_and_key_uniqueness(self):
        extra = '<XCUIElementTypeCell label="First"><XCUIElementTypeButton label="Next" visible="true" enabled="true" x="2" y="3" width="4" height="5"/></XCUIElementTypeCell>'
        element = snapshot(source(extra=extra)).elements[-1]
        self.assertIn("XCUIElementTypeCell[1][@label='First']/", element.xpath)
        self.assertIn("@x='2'", element.xpath)
        using, query = element.locator()
        self.assertEqual(using, "class chain")
        self.assertIn('XCUIElementTypeCell[`label == "First"`]/XCUIElementTypeButton[', query)
        self.assertIn('rect.x == 2.0', query)
        self.assertNotIn('[1]', query)  # Anonymous-sibling reorder is not a positional identity check.
        using, query = snapshot(source()).elements[1].locator()
        self.assertEqual(using, "predicate string")
        self.assertIn('label == "Next"', query)
        self.assertIn("rect.x == 1.0", query)
        p = TransportProbe()
        p.warm()
        queries = []
        original = p.wda.find_elements
        def query(xpath):
            queries.append(xpath)
            return original(xpath)
        p.wda.find_elements = query
        p.ex._keypad_points(p.ex.latest, "121212")
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].count("[count("), 2)

    def test_repeated_screenshots_get_distinct_one_use_ids(self):
        p = TransportProbe()
        p.warm()
        with tempfile.TemporaryDirectory() as directory:
            p.actor.evidence_base = directory
            first, second = p.actor.screenshot(), p.actor.screenshot()
            self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual(len(p.calls), 6)  # Each warm capture uses three reads, no XML.
            with self.assertRaises(ObservationRejected):
                p.actor.vision_tap({"op": "vision_tap", "snapshot_id": first["snapshot_id"], "x": 1, "y": 1})
            self.assertFalse(any(path.endswith("/actions") for _, path, _ in p.calls))

    def test_planner_recovery_avoids_ax_and_timeout_is_not_reported_as_outage(self):
        from openclaw_iphone.errors import VerificationExpired
        ex, wda = executor(())
        spec = TaskSpec("Read", (), (Condition("app", APP),))
        session = PlannerSession(ex, spec)
        wda.source.side_effect = WDAUnavailable("broken AX")
        reply = session.request({"op": "recover_read"})
        self.assertEqual(reply["status"], "recovered")
        wda.source.assert_not_called()
        ex.wait = Mock(side_effect=VerificationExpired("private"))
        replies = []
        serve(session, iter([b'{"op":"wait"}']), replies.append)
        self.assertEqual(replies[0], {"status": "incomplete", "verification": "unknown", "reason": "verification_expired"})

    def test_read_timeout_covers_identity_and_post_queries_without_unknown_write(self):
        p = TransportProbe()
        p.wda.read_timeout = 0.5
        p.wda.active_app()
        self.assertEqual(p.wda.app_state(APP), 4)
        p.wda.find_elements(FIELD.xpath())
        self.assertTrue(all(timeout <= 0.5 for _, _, timeout in p.calls))
        p.wda._send = Mock(side_effect=WDAOutcomeUnknown("synthetic POST read failure"))
        with self.assertRaises(WDAUnavailable) as raised: p.wda.find_elements(FIELD.xpath())
        self.assertNotIsInstance(raised.exception, WDAOutcomeUnknown)

    def test_app_state_transport_and_protocol_failures_are_read_failures(self):
        p = TransportProbe()
        p.wda._send = WDAClient._send.__get__(p.wda)
        with patch.object(p.wda.opener, "open", side_effect=TimeoutError("private")):
            with self.assertRaises(WDAUnavailable) as raised:
                p.wda.app_state(APP)
        self.assertNotIsInstance(raised.exception, WDAOutcomeUnknown)
        for response in (b"malformed", b'{"value":{"error":"unknown error"}}', b'{"value":true}'):
            p.wda._send = Mock(return_value=response)
            with self.assertRaises(WDAUnavailable) as raised:
                p.wda.app_state(APP)
            self.assertNotIsInstance(raised.exception, WDAOutcomeUnknown)
