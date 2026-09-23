from dataclasses import replace
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from openclaw_iphone import cli
from openclaw_iphone.actions import Condition, Grant, StepResult
from openclaw_iphone.config import IPhoneConfig
from openclaw_iphone.connection import TaskConnection
from openclaw_iphone.control_lock import control_lock
from openclaw_iphone.devicectl import Device, DeviceCtl
from openclaw_iphone.errors import (DeviceLocked, DeviceSelectionError, SessionOutputUnavailable,
                                    WDAOutcomeUnknown, WDAUnavailable)
from openclaw_iphone.execution import Budget, TaskStopped
from openclaw_iphone.planner import JsonLineEmitter, PlannerSession, read_requests, serve
from openclaw_iphone.observations import ObservationRejected
from openclaw_iphone.runner import Runner
from openclaw_iphone.tasks import Limits, TaskSpec, parse_task
from openclaw_iphone.wda import WDAClient
from test_actions import APP, BUTTON, FIELD, executor, snapshot, source, tap_grant
from test_cli_config import FakeDeviceCtl, FakeWDA
from test_ui import FakeClient
from openclaw_iphone.ui import UIController


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

    def test_compact_reports_filtering_without_values_or_implicit_labels(self):
        xml = source(value="PRIVATE", extra='<XCUIElementTypeOther visible="true"/>')
        obs = snapshot(xml)
        view = obs.compact(limit=2)
        self.assertEqual(view["counts"]["source_nodes"], 4)
        self.assertEqual(view["counts"]["unnamed_visible"], 1)
        self.assertEqual(view["omitted_visible"], 2)
        self.assertNotIn("PRIVATE", json.dumps(view))
        self.assertNotIn("Input", json.dumps(view))
        self.assertIn("Input", json.dumps(obs.compact(include_labels=True)))
        secure = snapshot(source(extra='<XCUIElementTypeSecureTextField name="SECRET" label="SECRET" value="SECRET" visible="true"/>'))
        self.assertNotIn("SECRET", json.dumps(secure.compact(include_labels=True)))

    def test_failed_parse_supersedes_old_offers(self):
        ex, wda = executor([tap_grant()])
        offer, = ex.offers(ex.observe())
        wda.source.return_value = "<App/>"
        with self.assertRaises(ObservationRejected):
            ex.observe()
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
        wda.element_action.assert_not_called()

    def test_nested_static_label_is_not_a_second_button_but_peer_still_is(self):
        button = '<XCUIElementTypeButton label="Confirm" visible="true" x="10" y="450" width="100" height="52"><XCUIElementTypeStaticText label="Confirm" visible="true" x="20" y="463" width="80" height="27"/></XCUIElementTypeButton>'
        client = FakeClient(f"<App>{button}</App>")
        self.assertEqual(UIController(client).tap_text("Confirm", exact=True).type, "XCUIElementTypeButton")
        self.assertEqual(client.calls, [("tap", (60.0, 476.0), {})])
        for peer in (button.replace('x="10"', 'x="200"'), '<XCUIElementTypeStaticText label="Confirm" visible="true" x="20" y="463" width="80" height="27"/>'):
            client = FakeClient(f"<App>{button}{peer}</App>")
            with self.assertRaises(WDAUnavailable):
                UIController(client).tap_text("Confirm", exact=True)
            self.assertEqual(client.calls, [])


class KeypadTests(unittest.TestCase):
    def make(self, *, custom=False):
        target = replace(FIELD, role="XCUIElementTypeOther") if custom else FIELD
        grant = Grant("keypad", APP, "Enter supplied digits", target, text_id="digits")
        ex, wda = executor([grant], texts={"digits": "12"})
        state = {"value": ""}
        keys = '<XCUIElementTypeKeyboard visible="true">' + ''.join(
            f'<XCUIElementTypeKey name="{digit}" visible="true" enabled="true" x="{index * 40}" y="600" width="30" height="30"/>'
            for index, digit in enumerate("12")) + '</XCUIElementTypeKeyboard>'
        def xml():
            text = source(value=state["value"], extra=keys)
            return text.replace("XCUIElementTypeTextField", "XCUIElementTypeOther") if custom else text
        wda.source.side_effect = lambda **kwargs: xml()
        wda.find_elements.side_effect = lambda xpath, **kwargs: ([f"key{digit}" for digit in "12" if f"@name='{digit}'" in xpath]
                                                      if "XCUIElementTypeKey[" in xpath else ["field"])
        wda.active_element.return_value = "field"
        wda.element_value.side_effect = lambda *args, **kwargs: state["value"]
        wda.tap_sequence.side_effect = lambda points: state.update(value="12")
        return ex, wda, state

    def test_batches_keys_and_verifies_final_value_locally(self):
        for custom in (False, True):
            ex, wda, state = self.make(custom=custom)
            offer, = ex.offers(ex.observe())
            result = ex.execute(offer.id)
            self.assertEqual((result.dispatch, result.verification, result.acknowledged_substeps), ("acknowledged", "satisfied", 1))
            self.assertEqual(state["value"], "12")
            wda.tap_sequence.assert_called_once_with([(15.0, 615.0), (55.0, 615.0)])
            wda.element_action.assert_not_called()
            # The final tree verifies a readable field and is available to the next action.
            self.assertEqual(wda.source.call_count, 2)
            wda.element_value.assert_any_call("field", allow_null_empty=not custom)
            self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")

    def test_unreadable_nonempty_wrong_focus_or_missing_key_never_types(self):
        for kind in ("unreadable", "nonempty", "focus", "key"):
            ex, wda, state = self.make(custom=True)
            offer, = ex.offers(ex.observe())
            if kind == "unreadable":
                wda.element_value.side_effect = WDAUnavailable("PRIVATE")
            elif kind == "nonempty":
                state["value"] = "9"
            elif kind == "focus":
                wda.active_element.return_value = "other"
            else:
                wda.find_elements.side_effect = lambda xpath, **kwargs: [] if "XCUIElementTypeKey[" in xpath else ["field"]
            self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
            wda.element_action.assert_not_called()
            wda.tap_sequence.assert_not_called()

    def test_dropped_key_or_unknown_outcome_stops_without_fallback_or_replay(self):
        for kind in ("dropped", "unknown", "readback"):
            ex, wda, state = self.make(custom=True)
            offer, = ex.offers(ex.observe())
            if kind == "dropped":
                wda.tap_sequence.side_effect = None
            elif kind == "unknown":
                wda.tap_sequence.side_effect = WDAOutcomeUnknown("PRIVATE")
            else:
                def click(*args):
                    wda.element_value.side_effect = WDAUnavailable("PRIVATE")
                wda.tap_sequence.side_effect = click
            result = ex.execute(offer.id)
            self.assertNotEqual(result.verification, "satisfied")
            self.assertTrue(ex.stopped)
            self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
            self.assertEqual(wda.tap_sequence.call_count, 1)
            wda.type_text_bulk.assert_not_called()
            self.assertNotIn("PRIVATE", repr(result))

    def test_custom_null_is_not_empty_and_text_is_bounded(self):
        wda = WDAClient(url="http://wda.test")
        wda._session.identifier = "test"
        wda._json_request = Mock(return_value={"value": None})
        with self.assertRaises(WDAUnavailable):
            wda.element_value("custom", allow_null_empty=False)
        for text in ("abc", "１２", "1" * 33):
            with self.assertRaises(ValueError):
                executor([Grant("keypad", APP, "Digits", FIELD, text_id="x")], texts={"x": text})

    def test_auto_submit_verifies_destination_without_requiring_disappeared_field(self):
        ex, wda, state = self.make()
        ex.grants = (replace(ex.grants[0], after=(Condition("exists", APP, BUTTON),)),)
        wda.source.side_effect = lambda **kwargs: source(button_label="Waiting", extra=(
            '<XCUIElementTypeKeyboard visible="true">' + ''.join(
                f'<XCUIElementTypeKey name="{d}" visible="true" enabled="true" x="{i * 40}" y="600" width="30" height="30"/>'
                for i, d in enumerate("12")) + '</XCUIElementTypeKeyboard>'))
        def submit(points):
            wda.source.side_effect = None
            wda.source.return_value = source().replace('XCUIElementTypeTextField', 'XCUIElementTypeOther')
        wda.tap_sequence.side_effect = submit
        offer, = ex.offers(ex.observe())
        result = ex.execute(offer.id)
        self.assertEqual(result.verification, "satisfied")
        wda.tap_sequence.assert_called_once()

    def test_focus_change_duplicate_keys_changed_target_or_foreign_app_stop_before_batch(self):
        for kind in ("focus", "duplicate", "target", "app"):
            ex, wda, state = self.make(custom=True)
            offer, = ex.offers(ex.observe())
            if kind == "focus":
                wda.active_element.return_value = "different"
            elif kind in {"duplicate", "target"}:
                wda.find_elements.side_effect = lambda query, **kwargs: [] if (
                    "count(" in query if kind == "duplicate" else '"XCUIElementTypeOther"' in query) else ["field"]
            else:
                wda.app_state.return_value = 3
            result = ex.execute(offer.id)
            self.assertEqual(result.dispatch, "not_sent")
            self.assertNotEqual(result.verification, "satisfied")
            wda.tap_sequence.assert_not_called()


class PlannerTests(unittest.TestCase):
    def make(self, **limits):
        ex, wda = executor([tap_grant()])
        spec = TaskSpec("Synthetic navigation", ex.grants, (Condition("exists", APP, BUTTON),), limits=Limits(**limits))
        return PlannerSession(ex, spec), wda

    def test_opaque_offers_consumed_and_new_observation_required(self):
        session, wda = self.make()
        one = session.request({"op": "observe"})
        old = one["observation"]["actions"][0]["id"]
        two = session.request({"op": "observe"})
        action = two["observation"]["actions"][0]["id"]
        self.assertEqual(session.request({"op": "execute", "id": old})["dispatch"], "not_sent")
        wda.source.return_value = source(button_label="Finished")
        result = session.request({"op": "execute", "id": action})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(session.request({"op": "execute", "id": action})["dispatch"], "not_sent")
        wda.element_action.assert_called_once()

    def test_execute_response_contains_fresh_choices_and_blocker_reasons(self):
        session, wda = self.make()
        observed = session.request({"op": "observe"})["observation"]
        action = observed["actions"][0]["id"]
        wda.source.return_value = source(button_label="Finished")
        result = session.request({"op": "execute", "id": action})
        self.assertIn("observation", result)
        self.assertIn("actions", result["observation"])
        self.assertEqual(result["observation"]["action_blockers"],
                         [{"grant_index": 0, "operation": "tap", "reason": "grant_exhausted"}])
        self.assertEqual(wda.source.call_count, 2)

    def test_next_choices_failure_preserves_acknowledged_result(self):
        session, wda = self.make()
        observation = session.executor.observe()
        session.executor.execute = Mock(return_value=StepResult("acknowledged", "satisfied", "verified", observation, 1))
        session.executor.offers = Mock(side_effect=ObservationRejected("stale"))
        result = session.request({"op": "execute", "id": "offered"})
        self.assertEqual((result["dispatch"], result["verification"], result["acknowledged_substeps"]),
                         ("acknowledged", "satisfied", 1))
        self.assertTrue(result["input_stopped"])
        self.assertEqual(result["observation"]["actions"], [])
        self.assertEqual(result["observation"]["action_blockers"][0]["reason"], "next_choices_unavailable")

    def test_secure_screen_and_stopped_executor_explain_empty_choices(self):
        session, wda = self.make()
        wda.source.return_value = source(extra='<XCUIElementTypeSecureTextField value="SECRET" visible="true"/>')
        secure = session.request({"op": "observe"})["observation"]
        self.assertEqual(secure["actions"], [])
        self.assertEqual(secure["action_blockers"], [{"scope": "all", "reason": "secure_screen"}])
        session.executor.stopped = True
        stopped = session.request({"op": "observe"})["observation"]
        self.assertEqual(stopped["action_blockers"], [{"scope": "all", "reason": "executor_stopped"}])

    def test_done_is_independent_and_close_does_not_claim_completion(self):
        session, wda = self.make()
        wda.source.return_value = source(button_label="Other")
        wda.find_elements.return_value = []
        self.assertEqual(session.request({"op": "done"})["status"], "incomplete")
        self.assertFalse(session.closed)
        wda.source.return_value = source()
        session.request({"op": "observe"})  # A changed screen needs new evidence, not repeated done.
        self.assertEqual(session.request({"op": "done"})["status"], "completed")
        with self.assertRaises(TaskStopped):
            session.request({"op": "observe"})
        session, _ = self.make()
        self.assertEqual(session.request({"op": "close"})["verification"], "unknown")

    def test_stream_rejects_commands_duplicate_keys_and_never_echoes_input(self):
        for raw in (b'{"op":"shell","command":"SECRET"}', b'{"op":"observe","op":"close"}', b'{"op":[]}'):
            session, wda = self.make()
            replies = []
            self.assertEqual(serve(session, iter([raw]), replies.append), 1)
            self.assertEqual(replies, [{"status": "blocked", "reason": "invalid_request"}])
            wda.source.assert_not_called()

    def test_idle_partial_line_and_action_request_limits(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b'{"op":')
            started = time.monotonic()
            with self.assertRaises(TaskStopped):
                list(read_requests(reader, Budget.seconds(0.02)))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(reader)
            os.close(writer)
        session, wda = self.make(max_steps=1)
        session.request({"op": "execute", "id": "invalid"})
        with self.assertRaises(TaskStopped):
            session.request({"op": "execute", "id": "invalid"})
        wda.element_action.assert_not_called()
        session, _ = self.make(max_steps=1)
        session.requests = 12
        with self.assertRaises(TaskStopped):
            session.request({"op": "observe"})

    def test_pipe_reads_multiple_lines_and_rejects_oversized_or_unterminated_input(self):
        for data, valid in ((b'{}\n{}\n', True), (b'x' * 4097, False), (b'{}', False)):
            reader, writer = os.pipe()
            try:
                os.write(writer, data)
                os.close(writer)
                if valid:
                    self.assertEqual(list(read_requests(reader, Budget.seconds(1))), [b'{}', b'{}'])
                else:
                    with self.assertRaises(ValueError):
                        list(read_requests(reader, Budget.seconds(1)))
            finally:
                os.close(reader)

    def test_full_output_pipe_fails_within_budget_and_never_replays(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(writer, False)
            while True:
                try:
                    os.write(writer, b"x" * 4096)
                except BlockingIOError:
                    break
            started = time.monotonic()
            with self.assertRaises(SessionOutputUnavailable):
                JsonLineEmitter(writer, Budget.seconds(0.03))({"status": "ready"})
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(reader)
            os.close(writer)

    def test_oversized_output_is_replaced_by_bounded_safe_response(self):
        reader, writer = os.pipe()
        try:
            with self.assertRaises(SessionOutputUnavailable):
                JsonLineEmitter(writer, Budget.seconds(1), max_bytes=256)({
                    "status": "step", "dispatch": "acknowledged", "verification": "satisfied",
                    "acknowledged_substeps": 1, "observation": {"secret": "x" * 1000}})
            raw = os.read(reader, 1024)
            self.assertLessEqual(len(raw), 256)
            result = json.loads(raw)
            self.assertEqual((result["dispatch"], result["verification"]), ("acknowledged", "satisfied"))
            self.assertEqual(result["output_warning"], "response_too_large_session_closed")
            self.assertNotIn("secret", result)
        finally:
            os.close(reader)
            os.close(writer)


class SessionIntegrationTests(unittest.TestCase):
    def transport(self):
        ctl = Mock()
        ctl.runner = Runner()
        ctl.select_device.return_value = Device("phone", "core", "connected", "iPhone", "physical")
        ctl.coredevice_wda_url.return_value = "http://wda.test", None
        wda = WDAClient(url="http://wda.test")
        def send(path, *, method, payload, timeout):
            if path == "/session":
                return b'{"sessionId":"one","value":{}}'
            if path == "/status":
                return b'{"value":{"ready":true}}'
            if path == "/wda/activeAppInfo":
                return json.dumps({"value": {"bundleId": APP, "pid": 1}}).encode()
            if path.startswith("/source?"):
                return json.dumps({"value": source(value="PRIVATE")}).encode()
            if method == "DELETE":
                self.assertLessEqual(timeout, 2)
            return b'{"value":false}' if path == "/wda/locked" else b'{"value":null}'
        wda._send = Mock(side_effect=send)
        return ctl, wda

    def spec(self):
        return parse_task({"version": 1, "objective": "Synthetic observation", "grants": [],
                           "success": [{"kind": "app", "app": APP}]})

    def test_unknown_cleanup_does_not_mask_unknown_action_outcome(self):
        wda = WDAClient(url="http://wda.test")
        wda._create_session = Mock(return_value="one")
        wda._delete_session = Mock(side_effect=WDAOutcomeUnknown("cleanup transport failed"))
        wda._json_post = Mock(return_value={"value": None})
        with self.assertRaises(WDAOutcomeUnknown) as action:
            with wda.session():
                raise WDAOutcomeUnknown("action transport failed")
        self.assertEqual(str(action.exception), "action transport failed")
        self.assertTrue(wda._session.cleanup_failed)

    def test_three_observations_share_setup_session_and_cleanup(self):
        counts = []
        for shared in (False, True):
            ctl, wda = self.transport()
            with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.connection.WDAClient", return_value=wda):
                for _ in range(1 if shared else 3):
                    with TaskConnection(ctl, lock_path=Path(tmp) / "lock") as connection:
                        from openclaw_iphone.actions import Executor
                        session = PlannerSession(Executor(connection, ()), self.spec())
                        for _ in range(3 if shared else 1):
                            result = session.request({"op": "observe"})
                            self.assertNotIn("PRIVATE", json.dumps(result))
                            self.assertNotIn("physical", json.dumps(result))
                    self.assertFalse(connection.cleanup_failed)
                    with control_lock(Path(tmp) / "lock"):
                        pass
            sessions = sum(c.args[0] == "/session" for c in wda._send.call_args_list)
            counts.append((ctl.select_device.call_count, sessions, wda._send.call_count))
        self.assertEqual(counts, [(3, 3, 21), (1, 1, 11)])

    def test_cli_protocol_exit_and_cleanup_warning_preserve_completion(self):
        ctl, wda = self.transport()
        send = wda._send.side_effect
        def fail_cleanup(path, **kwargs):
            if kwargs["method"] == "DELETE":
                raise WDAUnavailable("PRIVATE")
            return send(path, **kwargs)
        wda._send.side_effect = fail_cleanup
        reader, writer = os.pipe()
        os.write(writer, b'{"op":"observe"}\n{"op":"done"}\n')
        os.close(writer)
        args = cli.build_parser().parse_args(["task", "session", "--file", "/unused-task.json"])
        stdin = Mock()
        stdin.fileno.return_value = reader
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch("openclaw_iphone.cli.client_from_args", return_value=ctl), \
                 patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})), \
                 patch("openclaw_iphone.tasks.load_task", return_value=self.spec()), \
                 patch("openclaw_iphone.connection.WDAClient", return_value=wda), \
                 patch("openclaw_iphone.connection.control_lock", side_effect=lambda _: control_lock(Path(tmp) / "lock")), \
                 patch("sys.stdin", stdin), contextlib.redirect_stdout(io.StringIO()) as output, \
                 self.assertLogs("openclaw_iphone.wda"):
                self.assertEqual(cli.handle_task_session(args), 0)
                with control_lock(Path(tmp) / "lock"):
                    pass
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([r["status"] for r in responses], ["ready", "observed", "completed", "session_end"])
            self.assertEqual(responses[-1]["cleanup"], "warning")
            self.assertNotIn("PRIVATE", output.getvalue())
        finally:
            os.close(reader)

    def test_cli_output_disconnect_releases_device_ownership_without_replay(self):
        ctl, wda = self.transport()
        reader, writer = os.pipe()
        os.close(reader)  # Force the first response to fail immediately.
        stdin = Mock()
        stdin.fileno.return_value = os.open(os.devnull, os.O_RDONLY)
        stdout = Mock()
        stdout.fileno.return_value = writer
        args = cli.build_parser().parse_args(["task", "session", "--file", "/unused-task.json"])
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch("openclaw_iphone.cli.client_from_args", return_value=ctl), \
                 patch("openclaw_iphone.cli.load_config", return_value=IPhoneConfig({})), \
                 patch("openclaw_iphone.tasks.load_task", return_value=self.spec()), \
                 patch("openclaw_iphone.connection.WDAClient", return_value=wda), \
                 patch("openclaw_iphone.connection.control_lock", side_effect=lambda _: control_lock(Path(tmp) / "lock")), \
                 patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                self.assertEqual(cli.handle_task_session(args), 1)
                with control_lock(Path(tmp) / "lock"):
                    pass
        finally:
            os.close(stdin.fileno.return_value)
            os.close(writer)

    def test_read_recovery_preserves_stop_and_refuses_uncertain_mutation(self):
        from openclaw_iphone.actions import Executor
        ctl, wda = self.transport()
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.connection.WDAClient", return_value=wda):
            with TaskConnection(ctl, lock_path=Path(tmp) / "lock") as connection:
                ex = Executor(connection, ())
                session = PlannerSession(ex, self.spec())
                connection.invalidate()
                ex.stopped = True
                response = session.request({"op": "recover_read"})
                self.assertTrue(response["observation"]["input_stopped"])
                self.assertEqual(response["observation"]["actions"], [])
                ctl.select_device.assert_called_with("physical")
                connection.invalidate()
                with self.assertRaises(TaskStopped):
                    session.request({"op": "recover_read"})
            with TaskConnection(ctl, lock_path=Path(tmp) / "lock") as connection:
                session = PlannerSession(Executor(connection, ()), self.spec())
                connection.invalidate(uncertain=True)
                with self.assertRaises(TaskStopped):
                    session.request({"op": "recover_read"})


if __name__ == "__main__":
    unittest.main()
