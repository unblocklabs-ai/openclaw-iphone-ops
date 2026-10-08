from dataclasses import replace
import json
import time
import unittest
from openclaw_iphone.observations import ObservationRejected, Selector, parse_observation, predicate_literal, xpath_literal
APP = "test.app"
BUTTON = Selector("XCUIElementTypeButton", label="Next")

def source(*, button_label="Next", value="", extra="", button_x=1, visible="true", enabled="true"):
    return f'''<XCUIElementTypeApplication name="Test" bundleId="test.app" visible="true" enabled="true" x="0" y="0" width="400" height="800">
      <XCUIElementTypeButton label="{button_label}" visible="{visible}" enabled="{enabled}" x="{button_x}" y="2" width="30" height="30"/>
      <XCUIElementTypeTextField label="Input" value="{value}" visible="true" enabled="true" x="1" y="50" width="100" height="30"/>
      {extra}</XCUIElementTypeApplication>'''


def snapshot(xml):
    return parse_observation(xml, generation=1, device_udid="device",
                             captured_at="now", started=time.monotonic(), finished=time.monotonic())


class ObservationTests(unittest.TestCase):
    def test_application_identity_comes_from_the_root(self):
        xml = source().replace('name="Test"', 'name="Test" processId="1"', 1)
        for candidate in (xml, f"<AppiumAUT>{xml}</AppiumAUT>"):
            obs = parse_observation(candidate, generation=1, device_udid="device", captured_at="now", started=0, finished=1)
            self.assertEqual((obs.app, obs.process_id), (APP, 1))
        self.assertIsNone(snapshot(source()).process_id)
        for candidate in (xml.replace('processId="1"', 'processId="invalid"'), xml.replace('processId="1"', 'processId="0"'),
                          xml.replace(' bundleId="test.app"', '')):
            with self.assertRaises(ObservationRejected):
                snapshot(candidate)

    def test_fast_source_visibility_comes_from_geometry_and_selection_from_traits(self):
        xml = ('<XCUIElementTypeApplication bundleId="test.app" x="0" y="0" width="400" height="800" traits="">'
               '<XCUIElementTypeButton label="Shown" traits="Selected, Button" x="1" y="2" width="30" height="30"/>'
               '<XCUIElementTypeButton label="Edge" traits="Button" x="390" y="790" width="30" height="30"/>'
               '<XCUIElementTypeButton label="Below" traits="Button" x="1" y="800" width="30" height="30"/>'
               '<XCUIElementTypeButton label="Empty" traits="Button" x="1" y="2" width="0" height="30"/>'
               '<XCUIElementTypeButton label="Hidden" visible="false" x="1" y="2" width="30" height="30"/>'
               '</XCUIElementTypeApplication>')
        obs = parse_observation(xml, generation=1, device_udid="device", captured_at="now", started=0, finished=1)
        self.assertEqual({e.label: e.visible for e in obs.elements[1:]},
                         {"Shown": True, "Edge": True, "Below": False, "Empty": False, "Hidden": False})
        self.assertEqual([e.label for e in obs.elements if e.selected], ["Shown"])
        rows = obs.compact()["elements"]
        self.assertEqual([(row["label"], row.get("selected")) for row in rows], [("Shown", True), ("Edge", None)])
        self.assertEqual(obs.counts["unknown_visibility"], 0)


    def test_ids_are_snapshot_local_and_hierarchy_is_retained(self):
        one, two = snapshot(source()), snapshot(source())
        self.assertNotEqual(one.elements[1].id, two.elements[1].id)
        self.assertEqual(one.elements[1].ancestors[0][0], "XCUIElementTypeApplication")
        self.assertEqual(one.elements[1].path, "//XCUIElementTypeApplication[1]/XCUIElementTypeButton[1]")

    def test_unknown_attributes_duplicates_and_secure_values(self):
        secure = '<XCUIElementTypeSecureTextField value="SECRET" />'
        obs = snapshot(source(extra=secure, enabled="unknown"))
        self.assertTrue(obs.secure)
        self.assertIsNone(obs.elements[-1].value)
        self.assertIsNone(obs.elements[1].enabled)
        self.assertNotIn("SECRET", repr(obs))
        duplicate = '<XCUIElementTypeButton label="Next" visible="true" />'
        self.assertEqual(len(snapshot(source(extra=duplicate)).matches(BUTTON)), 2)

    def test_rejects_incomplete_trees_and_escapes_locator_text(self):
        for xml in ("<App />", '<!DOCTYPE a [<!ENTITY b "x">]><App/>'):
            with self.assertRaises(ObservationRejected):
                snapshot(xml)
        self.assertEqual(xpath_literal("a'b\"c"), 'concat(\'a\', "\'", \'b"c\')')
        label = 'long native label ' * 20 + '" OR TRUEPREDICATE OR label == "é🙂\\\n'
        self.assertEqual(json.loads(predicate_literal(label)), label)
        using, query = Selector("XCUIElementTypeButton", label=label).locator()
        self.assertEqual(using, "predicate string")
        self.assertIn("label == " + predicate_literal(label), query)
        self.assertTrue(query.endswith(" AND visible == 1"))  # Visibility is asked only of matches.
        self.assertNotIn("@visible", Selector("XCUIElementTypeButton", label="Go", ancestor_label="Row").xpath())

    def test_named_ancestor_identity_survives_movement_without_unescaped_queries(self):
        xml = source(extra=('<XCUIElementTypeCell name="row-id" label="Row ` A">'
                            '<XCUIElementTypeOther><XCUIElementTypeButton name="go-id" label="Go ` now" '
                            'value="private" visible="true" enabled="true" x="2" y="3" width="4" height="5"/>'
                            '</XCUIElementTypeOther></XCUIElementTypeCell>'))
        element = snapshot(xml).elements[-1]
        expected = ('XCUIElementTypeCell[`name == "row-id" AND label == "Row `` A"`]/'
                    'XCUIElementTypeOther/XCUIElementTypeButton[`name == "go-id" AND label == "Go `` now" '
                    'AND enabled == 1 AND visible == 1`]')
        self.assertEqual(element.locator(), ("class chain", expected))
        self.assertEqual(replace(element, bounds=(100, 200, 8, 10), value="changed").locator(), ("class chain", expected))
