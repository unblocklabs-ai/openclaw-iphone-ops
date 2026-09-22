import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from openclaw_iphone.actions import Condition
from openclaw_iphone.adaptive import AdaptiveAct, parse_scope, read_input
from openclaw_iphone.errors import WDAOutcomeUnknown
from openclaw_iphone.image_evidence import redact_png
from openclaw_iphone.jev import Decision, LowConfidenceDecision
from openclaw_iphone.observations import ObservationRejected, Selector
from openclaw_iphone.planner import PlannerSession
from openclaw_iphone.tasks import TaskSpec, parse_task
from test_actions import APP, BUTTON, executor, source


def make(*, extra="", driver=None, inputs=None):
    ex, wda = executor([], xml=source(extra=extra).replace('value=""', ''))
    scope = parse_scope({"apps": [APP], "operations": ["tap", "input", "keypad", "vision_tap", "relaunch"],
                         "cloud_labels": ["Next", "Input", "Continue with Email"], "inputs": inputs or {}})
    actor = AdaptiveAct(ex, scope, driver=driver)
    return actor, wda


def png(width=8, height=16, filter_type=0):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    data = (bytes([filter_type]) + bytes([255]) * width * 4) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b"")


class AdaptiveTests(unittest.TestCase):
    def test_opt_in_preserves_fixed_session_and_scope(self):
        ex, _ = executor([])
        spec = TaskSpec("navigate", (), (Condition("exists", APP, BUTTON),))
        session = PlannerSession(ex, spec)
        with self.assertRaises(ValueError):
            session.request({"op": "act", "action": "tap", "instruction": "Next"})
        with self.assertRaises(ValueError):
            parse_scope({"apps": [APP], "operations": ["shell"]})
        parsed = parse_task({"version": 1, "objective": "test", "grants": [],
            "success": [{"kind": "app", "app": APP}], "adaptive": {"apps": [APP], "operations": ["tap"]}})
        self.assertEqual(parsed.adaptive["apps"], (APP,))

    def test_unrelated_secure_node_does_not_block_static_control(self):
        extra = '<XCUIElementTypeSecureTextField value="SECRET" visible="false"/>'
        extra += '<XCUIElementTypeStaticText label="Continue with Email" visible="true" enabled="true" x="1" y="120" width="200" height="40"/>'
        actor, wda = make(extra=extra)
        result = actor.act({"op": "act", "action": "tap", "instruction": "Continue with Email"})
        self.assertEqual((result["dispatch"], result["resolution"]), ("acknowledged", "exact"))
        self.assertFalse(actor.ex.stopped)
        wda.element_action.assert_called_once_with("ref", "click")
        self.assertNotIn("SECRET", json.dumps(actor.view(result["observation"], labels=True)))

    def test_jev_context_and_low_confidence_fallback_can_continue(self):
        driver = Mock()
        decision = Decision("unknown", 0.69, 0.1, 10, 5)
        driver.choose.side_effect = LowConfidenceDecision(decision, 0.7)
        actor, wda = make(driver=driver, extra='<XCUIElementTypeStaticText label="PRIVATE" visible="true"/>')
        result = actor.act({"op": "act", "action": "tap", "instruction": "Go forward"})
        self.assertEqual(result["reason"], "low_confidence")
        self.assertFalse(actor.ex.stopped)
        view, options, _ = driver.choose.call_args.args
        self.assertIn("Next", json.dumps(options))
        self.assertNotIn("PRIVATE", json.dumps([view, options]))
        self.assertIn("escalate", options)
        self.assertIn("bounds", next(iter(options.values())))
        result = actor.act({"op": "act", "action": "tap", "instruction": "Next"})
        self.assertEqual(result["dispatch"], "acknowledged")
        wda.element_action.assert_called_once()

    def test_jev_selected_custom_target_at_point_seven(self):
        driver = Mock()
        driver.choose.side_effect = lambda v, o, b: Decision(next(iter(o)), 0.7, 0.1, 10, 5)
        actor, wda = make(driver=driver)
        result = actor.act({"op": "act", "action": "tap", "instruction": "Proceed to the following view"})
        self.assertEqual((result["dispatch"], result["resolution"]), ("acknowledged", "jev"))
        wda.element_action.assert_called_once()

    def test_stale_target_wrong_app_and_obscured_never_tap(self):
        for mode in ("moved", "app", "obscured", "snapshot"):
            actor, wda = make()
            observation = actor.ex.observe()
            request = {"op": "act", "action": "tap", "instruction": "Next"}
            if mode == "moved":
                wda.source.return_value = source(button_x=20)
            elif mode == "app":
                wda.active_app.return_value = {"bundleId": "other", "pid": 2}
            elif mode == "obscured":
                wda.element_hittable.return_value = False
            else:
                request.update(target_id=observation.elements[1].id, snapshot_id="wrong")
            self.assertEqual(actor.act(request)["dispatch"], "not_sent")
            wda.element_action.assert_not_called()

    def test_unknown_write_stops_and_is_not_replayed(self):
        actor, wda = make()
        wda.element_action.side_effect = WDAOutcomeUnknown("sensitive provider body")
        request = {"op": "act", "action": "tap", "instruction": "Next"}
        result = actor.act(request)
        self.assertEqual(result["dispatch"], "unknown")
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(actor.act(request)["dispatch"], "not_sent")
        wda.element_action.assert_called_once()
        actor.ex.connection.invalidate.assert_called_once_with(uncertain=True)

    def test_mismatched_navigation_is_not_a_permanent_stop(self):
        actor, wda = make()
        result = actor.act({"op": "act", "action": "tap", "instruction": "Next", "after": [
            {"kind": "exists", "app": APP, "target": {"role": "XCUIElementTypeButton", "label": "Finished"}}]})
        self.assertEqual(result["verification"], "unsatisfied")
        self.assertFalse(actor.ex.stopped)
        self.assertEqual(actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "acknowledged")

    def test_private_input_readback_and_redacted_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            path.write_text("PRIVATE-INPUT")
            path.chmod(0o600)
            actor, wda = make(inputs={"text": str(path)})
            wda.element_value.side_effect = ["", "PRIVATE-INPUT"]
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
            self.assertEqual(result["verification"], "satisfied")
            self.assertIsNone(actor.pending_input)
            wda.element_action.assert_called_once_with("ref", "value", text="PRIVATE-INPUT")
            wda.source.return_value = source(button_label="PRIVATE-INPUT")
            self.assertNotIn("PRIVATE-INPUT", json.dumps(actor.view(actor.ex.observe(), labels=True)))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_input(str(path))
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_input(str(link))

    def test_unverified_input_blocks_all_new_mutations(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.element_value.side_effect = ["", "wrong"]
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["verification"], "unsatisfied")
        self.assertTrue(result["input_pending"])
        self.assertEqual(actor.act({"op": "act", "action": "tap", "instruction": "Next"})["dispatch"], "not_sent")
        wda.element_action.assert_called_once()

    def test_placeholder_is_empty_only_after_explicit_verified_clear(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.element_value.return_value = "Placeholder"
        request = {"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"}
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            result = actor.act(request)
            self.assertEqual((result["dispatch"], result["validation_stage"]), ("not_sent", "input_empty"))
            wda.element_action.assert_not_called()
            wda.element_value.side_effect = ["Placeholder", "exact"]
            wda.element_placeholder.return_value = "Placeholder"
            result = actor.act({**request, "mode": "replace"})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(wda.element_action.call_count, 2)

    def test_acknowledged_wrong_input_allows_explicit_same_field_replacement(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.element_value.side_effect = ["", "wrong", "", "exact"]
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            first = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
            self.assertEqual(first["readback"]["matches"], False)
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text",
                                "mode": "replace", "strategy": "sequential"})
        self.assertEqual(result["verification"], "satisfied")
        wda.type_text.assert_called_once_with("exact", frequency=8)
        self.assertIsNone(actor.pending_input)

    def test_pending_readback_can_be_reconciled_without_input_replay(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.element_value.side_effect = ["", "wrong"]
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        wda.source.return_value = source(value="exact")
        result = actor.reconcile()
        self.assertEqual(result["verification"], "satisfied")
        self.assertIsNone(actor.pending_input)
        wda.element_action.assert_called_once()

    def test_named_field_with_empty_label_constructs_readback_before_clear(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.source.return_value = source().replace('label="Input"', 'name="email" label=""').replace('value=""', '')
        wda.element_value.side_effect = ["", "exact"]
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            result = actor.act({"op": "act", "action": "input", "instruction": "email", "text_ref": "text", "mode": "replace"})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(result["acknowledged_substeps"], 2)

    def test_sequential_focus_change_stops_before_batch(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.active_element.side_effect = ["ref", "other"]
        with patch("openclaw_iphone.adaptive.read_input", return_value="ab"):
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text", "strategy": "sequential"})
        self.assertEqual((result["dispatch"], result["acknowledged_substeps"]), ("not_sent", 0))
        wda.type_text.assert_not_called()

    def test_fresh_xml_value_verifies_without_second_native_lookup(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.source.side_effect = [source(), source(), source(value="exact")]
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(wda.find_elements.call_count, 1)
        # Only the initial emptiness check needs native value access.
        self.assertEqual(wda.element_value.call_count, 1)

    def test_custom_keypad_uses_fresh_visual_confirmation_and_destination(self):
        extra = '<XCUIElementTypeOther name="code" visible="true" enabled="true" x="1" y="120" width="200" height="40"/>'
        extra += '<XCUIElementTypeKeyboard><XCUIElementTypeKey name="1" visible="true" enabled="true" x="1" y="700" width="50" height="40"/></XCUIElementTypeKeyboard>'
        actor, wda = make(extra=extra, inputs={"code": "/unused"})
        observation = actor.ex.observe()
        actor.visual = (observation, (400, 800), (800, 1600))
        # An unrelated countdown/label can change without changing the input.
        wda.source.side_effect = [source(extra=extra, button_label="Countdown"), source(extra=extra), source(button_label="Finished")]
        wda.active_element.return_value = "not-a-readable-input"
        with patch("openclaw_iphone.adaptive.read_input", return_value="1"):
            result = actor.act({"op": "act", "action": "keypad", "instruction": "code", "text_ref": "code",
                "empty_focus_confirmed": observation.id, "after": [{"kind": "exists", "app": APP,
                    "target": {"role": "XCUIElementTypeButton", "label": "Finished"}}]})
        self.assertEqual(result["verification"], "satisfied")
        self.assertIsNone(actor.pending_input)
        wda.element_value.assert_not_called()
        wda.tap_sequence.assert_called_once_with([(26.0, 720.0)])
        wda.element_action.assert_not_called()
        wda.find_elements.assert_not_called()

    def test_secure_input_never_uses_value_readback_for_completion(self):
        extra = '<XCUIElementTypeSecureTextField name="password" visible="true" enabled="true" x="1" y="120" width="200" height="40"/>'
        actor, wda = make(extra=extra, inputs={"password": "/unused"})
        with patch("openclaw_iphone.adaptive.read_input", return_value="secret"):
            with self.assertRaises(ValueError):
                actor.act({"op": "act", "action": "input", "target": {"role": "XCUIElementTypeSecureTextField", "name": "password"},
                           "instruction": "Enter password", "text_ref": "password"})
        wda.element_action.assert_not_called()

    def test_screenshot_geometry_and_consumed_vision_target(self):
        actor, wda = make()
        wda.window_size.return_value = (400, 800)
        wda.screenshot.return_value = png()
        with tempfile.TemporaryDirectory() as directory:
            actor.evidence_base = directory
            shot = actor.screenshot()
            self.assertEqual(os.stat(shot["path"]).st_mode & 0o777, 0o600)
            result = actor.vision_tap({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})
            self.assertEqual(result["dispatch"], "acknowledged")
            wda.tap.assert_called_once_with(100, 200)
            with self.assertRaises(ObservationRejected):
                actor.vision_tap({"op": "vision_tap", "snapshot_id": shot["snapshot_id"], "x": 2, "y": 4})

    def test_redaction_and_invalid_png_never_save_original(self):
        for filtering in range(5):
            data, size = redact_png(png(filter_type=filtering), (8, 16), [(0, 0, 8, 16)])
            self.assertEqual(size, (8, 16))
            # Encoding a fully masked image is independent of the original pixels.
            expected, _ = redact_png(png(), (8, 16), [(0, 0, 8, 16)])
            self.assertEqual(data, expected)
        for raw in (b"invalid", png()[:-10], png(filter_type=6)):
            with self.assertRaises(ObservationRejected):
                redact_png(raw, (8, 16), [])


if __name__ == "__main__":
    unittest.main()
