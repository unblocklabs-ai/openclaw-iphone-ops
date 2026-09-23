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


def scroll_actor(*, item="First"):
    xml = source(extra=f'<XCUIElementTypeScrollView name="Feed" visible="true" enabled="true" x="0" y="100" width="300" height="500">'
                 f'<XCUIElementTypeCell label="{item}" visible="true" x="0" y="100" width="300" height="50"/>'
                 '</XCUIElementTypeScrollView>')
    ex, wda = executor([], xml=xml)
    actor = AdaptiveAct(ex, parse_scope({"apps": [APP], "operations": ["scroll"], "cloud_labels": []}))
    return actor, wda


def png(width=8, height=16, filter_type=0):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    data = (bytes([filter_type]) + bytes([255]) * width * 4) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b"")


class AdaptiveTests(unittest.TestCase):
    def test_scroll_one_dispatch_and_reusable_full_observation(self):
        actor, wda = scroll_actor()
        before = actor.ex.observe()
        view = actor.view(before)
        container = next(row for row in view["elements"] if row["role"] == "XCUIElementTypeScrollView")
        wda.source.return_value = source(extra='<XCUIElementTypeScrollView name="Feed" visible="true" enabled="true" x="0" y="100" width="300" height="500">'
            '<XCUIElementTypeCell label="Second" visible="true" x="0" y="100" width="300" height="50"/>'
            '</XCUIElementTypeScrollView>')
        result = actor.act({"op": "act", "action": "scroll", "instruction": "Feed", "target_id": container["id"],
                            "snapshot_id": before.id, "direction": "down"})
        self.assertEqual((result["dispatch"], result["verification"], result["reason"]),
                         ("acknowledged", "satisfied", "verified"))
        self.assertIs(actor.ex.latest, result["observation"])
        wda.element_scroll.assert_called_once_with("ref", "down")
        self.assertEqual(wda.source.call_count, 2)

    def test_scroll_rejects_bad_direction_stale_ambiguous_and_wrong_app(self):
        for mode in ("direction", "stale", "ambiguous", "app"):
            actor, wda = scroll_actor()
            before = actor.ex.observe()
            request = {"op": "act", "action": "scroll", "instruction": "Feed", "direction": "up",
                       "target_id": before.elements[-2].id, "snapshot_id": before.id}
            if mode == "direction":
                request["direction"] = "left"
                with self.assertRaises(ValueError):
                    actor.act(request)
            else:
                if mode == "stale":
                    request["snapshot_id"] = "old"
                elif mode == "ambiguous":
                    wda.find_elements.return_value = ["one", "two"]
                else:
                    wda.app_state.return_value = 3
                self.assertEqual(actor.act(request)["dispatch"], "not_sent")
            wda.element_scroll.assert_not_called()

    def test_scroll_no_progress_and_unknown_write_are_not_replayed(self):
        actor, wda = scroll_actor()
        result = actor.act({"op": "act", "action": "scroll", "instruction": "Feed", "direction": "down"})
        self.assertEqual((result["dispatch"], result["verification"], result["reason"]),
                         ("acknowledged", "unsatisfied", "no_progress"))
        self.assertFalse(actor.ex.stopped)
        wda.element_scroll.assert_called_once()
        actor, wda = scroll_actor()
        wda.element_scroll.side_effect = WDAOutcomeUnknown("PRIVATE")
        request = {"op": "act", "action": "scroll", "instruction": "Feed", "direction": "down"}
        self.assertEqual(actor.act(request)["dispatch"], "unknown")
        self.assertEqual(actor.act(request)["dispatch"], "not_sent")
        wda.element_scroll.assert_called_once()

    def test_scroll_ignores_bounce_jitter_but_accepts_material_content_movement(self):
        for offset, expected in ((2, "unsatisfied"), (12, "satisfied")):
            actor, wda = scroll_actor()
            wda.source.side_effect = [wda.source.return_value,
                source(extra='<XCUIElementTypeScrollView name="Feed" visible="true" enabled="true" x="0" y="100" width="300" height="500">'
                    f'<XCUIElementTypeCell label="First" visible="true" x="0" y="{100 + offset}" width="300" height="49"/>'
                    '<XCUIElementTypeOther visible="true" x="10" y="10" width="4" height="10"/>'
                    '</XCUIElementTypeScrollView>')]
            result = actor.act({"op": "act", "action": "scroll", "instruction": "Feed", "direction": "down"})
            self.assertEqual(result["verification"], expected)
            self.assertEqual(result["reason"], "no_progress" if offset == 2 else "verified")
            wda.element_scroll.assert_called_once()
            self.assertEqual(wda.source.call_count, 2)

    def test_scroll_explicit_after_is_checked_in_reused_tree(self):
        actor, wda = scroll_actor()
        wda.source.side_effect = [wda.source.return_value,
            source(extra='<XCUIElementTypeScrollView name="Feed" visible="true" enabled="true" x="0" y="100" width="300" height="500">'
            '<XCUIElementTypeCell label="Second" visible="true" x="0" y="100" width="300" height="50"/>'
            '</XCUIElementTypeScrollView>')]
        result = actor.act({"op": "act", "action": "scroll", "instruction": "Feed", "direction": "down",
            "after": [{"kind": "exists", "app": APP, "target": {"role": "XCUIElementTypeCell", "label": "Second"}}]})
        self.assertEqual((result["verification"], result["reason"]), ("satisfied", "verified"))
        wda.source.assert_called()
        self.assertEqual(wda.source.call_count, 2)
        wda.element_scroll.assert_called_once()

    def test_scroll_lost_container_is_unknown_not_no_progress(self):
        actor, wda = scroll_actor()
        wda.source.side_effect = [wda.source.return_value,
            source(extra='<XCUIElementTypeScrollView name="Other" visible="true" enabled="true" x="0" y="100" width="300" height="500"/>')]
        result = actor.act({"op": "act", "action": "scroll", "instruction": "Feed", "direction": "down"})
        self.assertEqual((result["dispatch"], result["verification"], result["reason"]),
                         ("acknowledged", "unknown", "inspect_result"))
        wda.element_scroll.assert_called_once()

    def test_pending_input_blocks_scroll(self):
        actor, wda = scroll_actor()
        actor.pending_input = (Condition("exists", APP, BUTTON),)
        result = actor.act({"op": "act", "action": "scroll", "instruction": "Feed", "direction": "down"})
        self.assertEqual((result["dispatch"], result["reason"]), ("not_sent", "input_requires_reconciliation"))
        wda.element_scroll.assert_not_called()

    def test_local_context_distinguishes_duplicate_rows_without_values(self):
        extra = ''.join(
            f'<XCUIElementTypeCell label="{name}" visible="true" enabled="true" x="1" y="{y}" width="200" height="40">'
            f'<XCUIElementTypeButton label="Edit" visible="true" enabled="true" x="160" y="{y}" width="40" height="40"/>'
            '</XCUIElementTypeCell>' for name, y in (("Alice", 100), ("Bob", 150)))
        actor, _ = make(extra=extra)
        observed = actor.ex.observe()
        plain = actor.view(observed)
        self.assertNotIn("Alice", json.dumps(plain))
        local = actor.view(observed, labels=True)
        edits = [row for row in local["elements"] if row.get("label") == "Edit"]
        self.assertEqual([row["ancestors"][0]["label"] for row in edits], ["Alice", "Bob"])
        self.assertNotEqual(edits[0]["id"], edits[1]["id"])

    def test_local_editable_state_is_coarse_and_private(self):
        extra = ('<XCUIElementTypeTextField label="Empty" value="" focused="true" visible="true"/>'
                 '<XCUIElementTypeTextField label="Placeholder" value="Placeholder" focused="false" visible="true"/>'
                 '<XCUIElementTypeTextField label="Private" value="SECRET" visible="true"/>'
                 '<XCUIElementTypeSecureTextField label="Password" value="SECRET" visible="true"/>')
        actor, _ = make(extra=extra)
        actor.ex.connection.require_active().source.return_value = source(extra=extra).replace('label="Input" value=""', 'label="Input"')
        view = actor.view(actor.ex.observe(), labels=True)
        rows = {row.get("label"): row for row in view["elements"]}
        self.assertEqual(rows["Input"]["input_state"], "unknown")
        self.assertEqual((rows["Empty"]["input_state"], rows["Empty"]["focused"]), ("empty", True))
        self.assertEqual(rows["Placeholder"]["input_state"], "unknown")
        self.assertEqual(rows["Private"]["input_state"], "nonempty")
        self.assertNotIn("input_state", next(row for row in view["elements"] if row["role"] == "XCUIElementTypeSecureTextField"))
        self.assertNotIn("SECRET", json.dumps(view))

    def test_ancestor_context_and_projection_remain_bounded(self):
        label = "L" * 400
        extra = '<XCUIElementTypeCell label="' + label + '"><XCUIElementTypeButton label="Go" visible="true"/></XCUIElementTypeCell>'
        actor, _ = make(extra=extra)
        view = actor.view(actor.ex.observe(), labels=True)
        row = next(row for row in view["elements"] if row.get("label") == "Go")
        self.assertEqual(len(row["ancestors"][0]["label"]), 256)
        self.assertLessEqual(len(view["elements"]), 80)

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
                wda.find_elements.return_value = []
            elif mode == "app":
                wda.app_state.return_value = 3
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
        wda.find_elements.side_effect = lambda query, **kwargs: [] if '"Finished"' in query else ["ref"]
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
        wda.element_value.side_effect = None
        wda.element_value.return_value = "exact"
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
        wda.active_element.return_value = "other"
        with patch("openclaw_iphone.adaptive.read_input", return_value="ab"):
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text", "strategy": "sequential"})
        self.assertEqual((result["dispatch"], result["acknowledged_substeps"]), ("not_sent", 0))
        wda.type_text.assert_not_called()

    def test_input_falls_back_to_native_value_when_xml_omits_it(self):
        actor, wda = make(inputs={"text": "/unused"})
        wda.element_value.side_effect = ["", "exact"]
        with patch("openclaw_iphone.adaptive.read_input", return_value="exact"):
            result = actor.act({"op": "act", "action": "input", "instruction": "Input", "text_ref": "text"})
        self.assertEqual(result["verification"], "satisfied")
        self.assertEqual(wda.find_elements.call_count, 1)  # Readback reuses the selected field.
        self.assertEqual(wda.element_value.call_count, 2)  # Empty and exact final value.
        self.assertEqual(wda.source.call_count, 2)  # Fresh reusable tree cannot prove its omitted value.

    def test_custom_keypad_uses_fresh_visual_confirmation_and_destination(self):
        from openclaw_iphone.adaptive import VisualEvidence
        extra = '<XCUIElementTypeOther name="code" visible="true" enabled="true" x="1" y="120" width="200" height="40"/>'
        extra += '<XCUIElementTypeKeyboard><XCUIElementTypeKey name="1" visible="true" enabled="true" x="1" y="700" width="50" height="40"/></XCUIElementTypeKeyboard>'
        actor, wda = make(extra=extra, inputs={"code": "/unused"})
        observation = actor.ex.observe()
        actor.visual = VisualEvidence(observation, (400, 800), (800, 1600))
        # An unrelated countdown/label can change without changing the input.
        wda.source.return_value = source(button_label="Finished")
        wda.active_element.return_value = "not-a-readable-input"
        with patch("openclaw_iphone.adaptive.read_input", return_value="1"):
            result = actor.act({"op": "act", "action": "keypad", "instruction": "code", "text_ref": "code",
                "empty_focus_confirmed": actor.visual.id, "after": [{"kind": "exists", "app": APP,
                    "target": {"role": "XCUIElementTypeButton", "label": "Finished"}}]})
        self.assertEqual(result["verification"], "satisfied")
        self.assertIsNone(actor.pending_input)
        wda.element_value.assert_not_called()
        wda.tap_sequence.assert_called_once_with([(26.0, 720.0)])
        wda.element_action.assert_not_called()
        self.assertEqual(wda.find_elements.call_count, 3)  # Container, keypad geometry, destination.

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
            shot = actor.screenshot(redact=[])
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
