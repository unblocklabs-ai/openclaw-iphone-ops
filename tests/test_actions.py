from dataclasses import replace
from contextlib import nullcontext
import json
import time
import unittest
from unittest.mock import Mock, call

from openclaw_iphone.actions import Condition, Executor, Grant
from openclaw_iphone.devicectl import Device
from openclaw_iphone.errors import DeviceLocked, WDAOutcomeUnknown, WDAUnavailable
from openclaw_iphone.execution import Budget, TaskStopped
from openclaw_iphone.observations import ObservationRejected, Selector, parse_observation, predicate_literal, xpath_literal


APP = "test.app"
BUTTON = Selector("XCUIElementTypeButton", label="Next")
FIELD = Selector("XCUIElementTypeTextField", label="Input")


def source(*, button_label="Next", value="", extra="", button_x=1, visible="true", enabled="true"):
    return f'''<XCUIElementTypeApplication name="Test" visible="true" enabled="true" x="0" y="0" width="400" height="800">
      <XCUIElementTypeButton label="{button_label}" visible="{visible}" enabled="{enabled}" x="{button_x}" y="2" width="30" height="30"/>
      <XCUIElementTypeTextField label="Input" value="{value}" visible="true" enabled="true" x="1" y="50" width="100" height="30"/>
      {extra}</XCUIElementTypeApplication>'''


def snapshot(xml):
    return parse_observation(xml, generation=1, device_udid="device", app=APP,
                             captured_at="now", started=time.monotonic(), finished=time.monotonic())


def executor(grants, *, xml=None, texts=None):
    wda = Mock()
    wda.deadline = None
    wda.active_app.return_value = {"bundleId": APP, "pid": 1}
    wda.app_state.return_value = 4
    wda.input_transaction.side_effect = nullcontext
    wda.source.return_value = xml or source()
    wda.find_elements.return_value = ["ref"]
    wda.element_hittable.return_value = True
    wda.element_value.return_value = ""
    wda.active_element.return_value = "ref"
    connection = Mock()
    connection.wda = wda
    connection.require_active.return_value = wda
    connection.device = Device("test", "core", "connected", "iPhone", "device")
    connection.generation = 1
    connection.budget = Budget.seconds(30)
    return Executor(connection, tuple(grants), texts=texts, verification_seconds=0.01), wda


def tap_grant():
    return Grant("tap", APP, "Go to the next view", BUTTON,
                 after=(Condition("exists", APP, Selector("XCUIElementTypeButton", label="Finished")),))


class ObservationTests(unittest.TestCase):
    def test_explicit_application_identity_must_match_foreground(self):
        xml = source().replace('name="Test"', 'name="Test" bundleId="test.app" processId="1"', 1)
        for candidate in (xml, f"<AppiumAUT>{xml}</AppiumAUT>"):
            self.assertEqual(parse_observation(candidate, generation=1, device_udid="device", app=APP,
                captured_at="now", started=0, finished=1, process_id=1).process_id, 1)
        for candidate in (xml.replace('bundleId="test.app"', 'bundleId="other.app"'),
                          xml.replace('processId="1"', 'processId="2"'),
                          xml.replace('processId="1"', 'processId="invalid"'),
                          "<AppiumAUT>" + xml.replace('bundleId="test.app"', 'bundleId="other.app"') + "</AppiumAUT>"):
            with self.assertRaises(ObservationRejected):
                parse_observation(candidate, generation=1, device_udid="device", app=APP,
                    captured_at="now", started=0, finished=1, process_id=1)
        self.assertEqual(snapshot(source()).app, APP)  # Older sources omit metadata.
        self.assertEqual(parse_observation(xml, generation=1, device_udid="device", app=APP,
            captured_at="now", started=0, finished=1).app, APP)  # Optional PID caller has nothing to compare.

    def test_contradictory_source_cannot_complete_foreign_app_condition(self):
        ex, wda = executor([])
        wda.source.return_value = source(button_label="Finished").replace(
            'name="Test"', 'name="Test" bundleId="other.app" processId="999"', 1)
        with self.assertRaises(ObservationRejected):
            ex.observe()
        wda.source.assert_called_once()
        wda.active_app.assert_called_once()
        wda.find_elements.assert_not_called()

    def test_ids_are_snapshot_local_and_hierarchy_is_retained(self):
        one, two = snapshot(source()), snapshot(source())
        self.assertNotEqual(one.elements[1].id, two.elements[1].id)
        self.assertEqual(one.signature, two.signature)
        self.assertEqual(one.elements[1].ancestors[0][0], "XCUIElementTypeApplication")
        self.assertEqual(one.elements[1].path, "//XCUIElementTypeApplication[1]/XCUIElementTypeButton[1]")

    def test_unknown_attributes_duplicates_and_secure_values(self):
        secure = '<XCUIElementTypeSecureTextField value="SECRET" />'
        obs = snapshot(source(extra=secure, enabled="unknown"))
        self.assertTrue(obs.secure)
        self.assertIsNone(obs.elements[-1].value)
        self.assertFalse(obs.elements[1].actionable)
        self.assertNotIn("SECRET", repr(obs))
        duplicate = '<XCUIElementTypeButton label="Next" visible="true" />'
        self.assertIsNone(snapshot(source(extra=duplicate)).unique(BUTTON))

    def test_rejects_incomplete_trees_and_escapes_locator_text(self):
        for xml in ("<App />", '<!DOCTYPE a [<!ENTITY b "x">]><App/>',
                    "<XCUIElementTypeApplication>" + "<XCUIElementTypeOther/>" * 2001 + "</XCUIElementTypeApplication>"):
            with self.assertRaises(ObservationRejected):
                snapshot(xml)
        self.assertEqual(xpath_literal("a'b\"c"), 'concat(\'a\', "\'", \'b"c\')')
        label = '" OR TRUEPREDICATE OR label == "é🙂\\\n'
        self.assertEqual(json.loads(predicate_literal(label)), label)
        using, query = Selector("XCUIElementTypeButton", label=label).locator()
        self.assertEqual(using, "predicate string")
        self.assertTrue(query.endswith("label == " + predicate_literal(label)))

    def test_named_ancestor_class_chain_escapes_literals_and_keeps_xpath_fallback(self):
        extra = ('<XCUIElementTypeCell name="row-id" label="Row ` A">'
                 '<XCUIElementTypeOther>'
                 '<XCUIElementTypeButton name="go-id" label="Go ` now" value="ready" visible="true" '
                 'enabled="true" x="2" y="3" width="4" height="5"/>'
                 '</XCUIElementTypeOther></XCUIElementTypeCell>')
        element = snapshot(source(extra=extra)).elements[-1]
        using, query = element.locator()
        self.assertEqual(using, "class chain")
        self.assertEqual(query,
            'XCUIElementTypeCell[`name == "row-id" AND label == "Row `` A"`]/'
            'XCUIElementTypeOther/'
            'XCUIElementTypeButton[`name == "go-id" AND label == "Go `` now" AND '
            'value == "ready" AND visible == 1 AND enabled == 1 AND rect.x == 2.0 AND '
            'rect.y == 3.0 AND rect.width == 4.0 AND rect.height == 5.0`]')
        self.assertEqual(replace(element, bounds=None).locator(), ("xpath", element.xpath))
        self.assertEqual(replace(element, enabled=False).locator(), ("xpath", element.xpath))
        self.assertEqual(replace(element, ancestors=element.ancestors[1:]).locator(), ("xpath", element.xpath))
        self.assertEqual(replace(element, label="x" * 257).locator(), ("xpath", element.xpath))
        ancestors = element.ancestors[:1] + (("XCUIElementTypeCell", "x" * 257, "Row ` A"),) + element.ancestors[2:]
        self.assertEqual(replace(element, ancestors=ancestors).locator(), ("xpath", element.xpath))
        self.assertEqual(snapshot(source()).elements[1].locator()[0], "predicate string")
        self.assertEqual(Selector("XCUIElementTypeButton", ancestor_label="Row ` A").locator()[0], "xpath")


class ExecutorTests(unittest.TestCase):
    def test_named_ancestor_ambiguous_class_chain_never_writes(self):
        extra = ('<XCUIElementTypeCell label="Row">'
                 '<XCUIElementTypeButton label="Go" visible="true" enabled="true" '
                 'x="2" y="3" width="4" height="5"/>'
                 '</XCUIElementTypeCell>')
        grant = Grant("tap", APP, "Tap the row button",
                      Selector("XCUIElementTypeButton", label="Go", ancestor_label="Row"),
                      after=(Condition("exists", APP, Selector("XCUIElementTypeButton", label="Finished")),))
        ex, wda = executor([grant], xml=source(extra=extra))
        offer, = ex.offers(ex.observe())
        wda.find_elements.return_value = ["one", "two"]
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
        query = wda.find_elements.call_args.args[0]
        self.assertEqual(wda.find_elements.call_args.kwargs["using"], "class chain")
        self.assertIn('XCUIElementTypeCell[`label == "Row"`]/XCUIElementTypeButton[`', query)
        wda.element_action.assert_not_called()

    def test_app_wait_reads_identity_without_source_or_lock_preflight(self):
        ex, wda = executor([])
        state, obs = ex.wait((Condition("app", APP),))
        self.assertEqual(state, "satisfied")
        self.assertEqual(wda.method_calls, [call.active_app()])
        self.assertEqual((obs.app, obs.process_id, obs.device_udid, obs.generation), (APP, 1, "device", 1))
        self.assertIsNone(obs.elements)
        self.assertIsNone(obs.secure)
        self.assertEqual(ex.offers(obs), ())
        for kind in ("exists", "absent", "actionable", "focused", "value"):
            with self.subTest(kind=kind):
                condition = Condition(kind, APP, FIELD, "" if kind == "value" else None)
                self.assertEqual(ex.verify(obs, (condition,)), "unknown")
        with self.assertRaises(ObservationRejected):
            obs.unique(BUTTON)

    def test_app_wait_supersedes_offers_and_needs_full_observation_for_input(self):
        ex, wda = executor([tap_grant()])
        offer, = ex.offers(ex.observe())
        _, obs = ex.wait((Condition("app", APP),))
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
        self.assertEqual(ex.offers(obs), ())
        self.assertEqual(len(ex.offers(ex.observe())), 1)
        wda.element_action.assert_not_called()
        self.assertEqual(wda.source.call_count, 2)

    def test_app_wait_cannot_offer_even_untargeted_transitions(self):
        grant = Grant("open_url", APP, "Open approved URL", destination="https://example.com",
                      after=(Condition("app", "next.app"),))
        ex, wda = executor([grant])
        self.assertEqual(len(ex.offers(ex.observe())), 1)
        _, obs = ex.wait((Condition("app", APP),))
        self.assertEqual(ex.offers(obs), ())
        wda.open_url.assert_not_called()

    def test_existence_wait_reads_only_the_target_unless_controls_requested(self):
        for conditions in ((Condition("exists", APP, BUTTON),),
                           (Condition("app", APP), Condition("exists", APP, BUTTON))):
            with self.subTest(conditions=conditions):
                ex, wda = executor([])
                state, obs = ex.wait(conditions)
                self.assertEqual(state, "satisfied")
                self.assertIsNone(obs.elements)
                wda.source.assert_not_called()
                state, obs = ex.wait(conditions, full=True)
                self.assertEqual(state, "satisfied")
                self.assertIsNotNone(obs.elements)
                wda.source.assert_called_once()

    def test_read_only_wait_does_not_need_unlock_but_invalidates_on_read_failure(self):
        ex, wda = executor([tap_grant()])
        wda.require_unlocked.side_effect = DeviceLocked("locked or unknown")
        self.assertEqual(ex.wait((Condition("app", APP),))[0], "satisfied")
        wda.require_unlocked.assert_not_called()
        wda.active_app.side_effect = WDAUnavailable("read failed")
        with self.assertRaises(WDAUnavailable):
            ex.wait((Condition("app", APP),))
        ex.connection.invalidate.assert_called_once()
        self.assertEqual(ex._offers, {})
        wda.source.assert_not_called()
        self.assertIsNone(wda.deadline)

    def test_app_only_observation_rejects_missing_or_invalid_process(self):
        for pid in (None, True, 0, -1, "1"):
            with self.subTest(pid=pid):
                ex, wda = executor([])
                wda.active_app.return_value = {"bundleId": APP, "pid": pid}
                with self.assertRaises(ObservationRejected):
                    ex.observe(app_only=True)
                wda.source.assert_not_called()

    def test_app_wait_preserves_outer_deadline_on_timeout_and_cancellation(self):
        ex, wda = executor([])
        outer = time.monotonic() + 0.001
        wda.deadline = outer
        def timed_out():
            self.assertLessEqual(wda.deadline, outer)
            raise WDAUnavailable("deadline expired")
        wda.active_app.side_effect = timed_out
        with self.assertRaises(WDAUnavailable):
            ex.wait((Condition("app", APP),), seconds=10)
        self.assertEqual(wda.deadline, outer)
        wda.source.assert_not_called()

        ex, wda = executor([])
        ex.connection.require_active.side_effect = TaskStopped("cancelled")
        with self.assertRaises(TaskStopped):
            ex.wait((Condition("app", APP),))
        self.assertEqual(wda.method_calls, [])

    def test_wait_reobserves_transient_app_change_without_repeating_input(self):
        ex, wda = executor([])
        ex.verification_seconds = 1
        wda.active_app.side_effect = [{"bundleId": "old", "pid": 2}, {"bundleId": APP, "pid": 1},
                                     {"bundleId": APP, "pid": 1}, {"bundleId": APP, "pid": 1}]
        state, _ = ex.wait((Condition("app", APP),))
        self.assertEqual(state, "satisfied")
        wda.element_action.assert_not_called()
        wda.source.assert_not_called()
        self.assertIsNone(wda.deadline)

    def test_offer_consumed_and_fresh_target_used_with_postcondition(self):
        ex, wda = executor([tap_grant()])
        observed = ex.observe()
        offer, = ex.offers(observed)
        wda.source.return_value = source(button_label="Finished")
        result = ex.execute(offer.id)
        self.assertEqual((result.dispatch, result.verification), ("acknowledged", "satisfied"))
        wda.element_action.assert_called_once_with("ref", "click")
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")

    def test_stale_moved_changed_app_and_duplicate_never_mutate(self):
        for kind in ("old", "moved", "app", "duplicate", "generation", "foreign", "superseded", "obscured"):
            with self.subTest(kind=kind):
                ex, wda = executor([tap_grant()])
                observed = ex.observe()
                offer, = ex.offers(observed)
                if kind == "old":
                    ex.freshness = 0.000001
                elif kind == "moved":
                    wda.find_elements.return_value = []  # Exact old geometry no longer matches.
                elif kind == "app":
                    wda.app_state.return_value = 3
                elif kind == "duplicate":
                    wda.find_elements.return_value = ["one", "two"]
                elif kind == "generation":
                    ex.connection.generation = 2
                elif kind == "superseded":
                    ex.observe()
                elif kind == "obscured":
                    wda.element_hittable.return_value = False
                else:
                    ex.connection.device = replace(ex.connection.device, udid="other")
                self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
                wda.element_action.assert_not_called()

    def test_unknown_mutation_stops_all_future_dispatch(self):
        ex, wda = executor([tap_grant()])
        offer, = ex.offers(ex.observe())
        wda.element_action.side_effect = WDAOutcomeUnknown("private transport detail")
        result = ex.execute(offer.id)
        self.assertEqual((result.dispatch, result.reason), ("unknown", "mutation_outcome_unknown"))
        ex.connection.invalidate.assert_called_with(uncertain=True)
        self.assertNotIn("private", repr(result))
        self.assertEqual(ex.offers(ex.observe()), ())
        wda.element_action.assert_called_once()

    def test_acknowledged_action_readback_failure_is_not_action_failure(self):
        ex, wda = executor([tap_grant()])
        offer, = ex.offers(ex.observe())
        wda.source.side_effect = WDAUnavailable("private")
        result = ex.execute(offer.id)
        self.assertEqual((result.dispatch, result.verification, result.reason),
                         ("acknowledged", "unknown", "verification_unavailable"))
        self.assertTrue(ex.stopped)
        self.assertIsNone(wda.deadline)

    def test_app_readback_failure_preserves_acknowledgement_without_replay(self):
        grant = replace(tap_grant(), after=(Condition("app", "next.app"),))
        ex, wda = executor([grant])
        offer, = ex.offers(ex.observe())
        def active_app():
            if wda.element_action.called:
                raise WDAUnavailable("post-dispatch read failed")
            return {"bundleId": APP, "pid": 1}
        wda.active_app.side_effect = active_app
        result = ex.execute(offer.id)
        self.assertEqual((result.dispatch, result.verification, result.reason),
                         ("acknowledged", "unknown", "verification_unavailable"))
        self.assertTrue(ex.stopped)
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
        wda.element_action.assert_called_once_with("ref", "click")
        self.assertEqual(wda.source.call_count, 1)  # App-only postcondition stays narrow.

    def test_native_append_prepares_focus_and_verifies_exact_unicode(self):
        grant = Grant("append", APP, "Enter supplied text", FIELD, text_id="query")
        ex, wda = executor([grant], texts={"query": "hé🙂"})
        offer, = ex.offers(ex.observe())
        wda.element_value.side_effect = ["", "hé🙂"]
        wda.source.side_effect = lambda **kwargs: source(value="hé🙂" if wda.element_action.called else "")
        self.assertEqual(ex.execute(offer.id).verification, "satisfied")
        wda.element_action.assert_called_once_with("ref", "value", text="hé🙂")
        wda.source.side_effect = None
        wda.source.return_value = source(value="hé🙂")
        self.assertEqual(ex.offers(ex.observe()), ())  # Cannot repeat the same input grant.
        ex, wda = executor([grant], texts={"query": "hi"})
        offer, = ex.offers(ex.observe())
        wda.active_element.return_value = "different"
        wda.element_value.side_effect = ["", "hi"]
        wda.source.side_effect = lambda **kwargs: source(value="hi" if wda.element_action.called else "")
        self.assertEqual(ex.execute(offer.id).verification, "satisfied")
        wda.active_element.assert_not_called()
        wda.element_action.assert_called_once_with("ref", "value", text="hi")

    def test_missing_xml_value_requires_explicit_empty_read_before_append(self):
        grant = Grant("append", APP, "Enter supplied text", FIELD, text_id="query")
        missing = source().replace('value=""', '')
        ex, wda = executor([grant], xml=missing, texts={"query": "hello"})
        offer, = ex.offers(ex.observe())
        wda.element_value.side_effect = ["", "hello"]
        self.assertEqual(ex.execute(offer.id).verification, "satisfied")
        self.assertEqual(wda.element_value.call_args_list, [call("ref", allow_null_empty=True)] * 2)
        ex, wda = executor([grant], xml=missing, texts={"query": "hello"})
        offer, = ex.offers(ex.observe())
        wda.element_value.side_effect = WDAUnavailable("unknown")
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
        wda.element_action.assert_not_called()

    def test_nonempty_append_is_not_offered_or_silently_replaced(self):
        grant = Grant("append", APP, "Enter supplied text", FIELD, text_id="query")
        for value in ("abCD", "Input"):
            with self.subTest(value=value):
                ex, wda = executor([grant], xml=source(value=value), texts={"query": "X"})
                self.assertEqual(ex.offers(ex.observe()), ())
                wda.element_action.assert_not_called()

    def test_append_requires_fresh_empty_value_even_when_xml_says_empty(self):
        grant = Grant("append", APP, "Enter supplied text", FIELD, text_id="query")
        for xml in (source(), source().replace('value=""', '')):
            for value in ("abCD", WDAUnavailable("private read error")):
                with self.subTest(xml=xml, value=value):
                    ex, wda = executor([grant], xml=xml, texts={"query": "X"})
                    offer, = ex.offers(ex.observe())
                    wda.element_value.side_effect = [value]
                    result = ex.execute(offer.id)
                    self.assertEqual((result.dispatch, result.reason), ("not_sent", "validation_failed"))
                    self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
                    wda.element_action.assert_not_called()

    def test_append_bad_readback_never_replays_or_repairs_text(self):
        grant = Grant("append", APP, "Enter supplied text", FIELD, text_id="query")
        ex, wda = executor([grant], texts={"query": "X"})
        offer, = ex.offers(ex.observe())
        wda.source.side_effect = lambda **kwargs: source(value="abXCD" if wda.element_action.called else "")
        result = ex.execute(offer.id)
        self.assertEqual((result.dispatch, result.verification), ("acknowledged", "unsatisfied"))
        self.assertTrue(ex.stopped)
        self.assertEqual(ex.execute(offer.id).dispatch, "not_sent")
        self.assertEqual(ex.offers(ex.observe()), ())
        wda.element_action.assert_called_once_with("ref", "value", text="X")

    def test_replace_stops_after_clear_if_empty_cannot_be_verified(self):
        grant = Grant("replace", APP, "Replace supplied text", FIELD, text_id="query")
        ex, wda = executor([grant], xml=source(value="placeholder"), texts={"query": "hello"})
        wda.element_value.return_value = "placeholder"
        offer, = ex.offers(ex.observe())
        result = ex.execute(offer.id)
        self.assertEqual(result.dispatch, "acknowledged")
        self.assertNotEqual(result.verification, "satisfied")
        wda.element_action.assert_called_once_with("ref", "clear")
        self.assertTrue(ex.stopped)

    def test_replace_verifies_clear_then_types_and_verifies(self):
        grant = Grant("replace", APP, "Replace supplied text", FIELD, text_id="query")
        ex, wda = executor([grant], xml=source(value="old"), texts={"query": "new"})
        offer, = ex.offers(ex.observe())
        wda.element_value.side_effect = ["", "new"]
        wda.source.side_effect = lambda **kwargs: source(value="new" if wda.element_action.called else "old")
        result = ex.execute(offer.id)
        self.assertEqual((result.verification, result.acknowledged_substeps), ("satisfied", 2))
        self.assertEqual(wda.element_action.call_count, 2)

    def test_no_actions_for_unknown_disabled_duplicate_or_secure(self):
        for xml in (source(visible="unknown"), source(enabled="false"),
                    source(extra='<XCUIElementTypeButton label="Next" visible="true"/>'),
                    source(extra='<XCUIElementTypeSecureTextField/>')):
            ex, wda = executor([tap_grant()], xml=xml)
            self.assertEqual(ex.offers(ex.observe()), ())
            wda.element_action.assert_not_called()

    def test_absence_and_field_value_do_not_invent_certainty(self):
        ex, _ = executor([])
        obs = snapshot(source(visible="unknown"))
        self.assertEqual(ex.evaluate(obs, Condition("absent", APP, BUTTON)), "unknown")
        self.assertEqual(ex.evaluate(obs, Condition("value", APP, BUTTON, "")), "unknown")
        self.assertEqual(ex.verify(obs, ()), "unknown")

    def test_scroll_no_progress_ignores_unrelated_changes(self):
        container = '<XCUIElementTypeScrollView visible="true" enabled="true" x="0" y="40" width="300" height="500"><XCUIElementTypeStaticText label="item" visible="true"/></XCUIElementTypeScrollView>'
        grant = Grant("scroll", APP, "Scroll the list", Selector("XCUIElementTypeScrollView"), direction="down")
        ex, wda = executor([grant], xml=source(extra=container))
        offer, = ex.offers(ex.observe())
        wda.source.side_effect = [source(extra=container), source(button_label="Clock changed", extra=container)]
        result = ex.execute(offer.id)
        self.assertEqual((result.dispatch, result.verification), ("acknowledged", "unsatisfied"))
        wda.element_scroll.assert_called_once_with("ref", "down")
        self.assertTrue(ex.stopped)


if __name__ == "__main__":
    unittest.main()
