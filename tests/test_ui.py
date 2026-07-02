from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openclaw_iphone.errors import WDAUnavailable
from openclaw_iphone.ui import UIController, parse_elements


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png"


class FakeClient:
    def __init__(self, source: str = "<App />") -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.source_text = source

    def source(self) -> str:
        return self.source_text

    def screenshot(self) -> bytes:
        return PNG_BYTES

    def tap(self, x: float, y: float) -> None:
        self.calls.append(("tap", (x, y), {}))

    def type_text(self, text: str, *, frequency: int | None = None) -> None:
        self.calls.append(("type_text", (text,), {"frequency": frequency}))

    def clear_text(self) -> None:
        self.calls.append(("clear_text", (), {}))

    def press_button(self, name: str, *, duration: float | None = None) -> None:
        self.calls.append(("press_button", (name,), {"duration": duration}))

    def back(self) -> None:
        self.calls.append(("back", (), {}))

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float, *, duration: float = 0.1) -> None:
        self.calls.append(("drag", (from_x, from_y, to_x, to_y), {"duration": duration}))


class FailingBackClient(FakeClient):
    def back(self) -> None:
        self.calls.append(("back", (), {}))
        raise WDAUnavailable("WDA back routes are missing")


class UITests(unittest.TestCase):
    def test_capture_source_writes_explicit_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xml"

            result = UIController(FakeClient()).capture_source(str(path))  # type: ignore[arg-type]

            self.assertEqual(result, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "<App />")

    def test_capture_screenshot_writes_explicit_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen.png"

            result = UIController(FakeClient()).capture_screenshot(str(path))  # type: ignore[arg-type]

            self.assertEqual(result, path)
            self.assertEqual(path.read_bytes(), PNG_BYTES)

    def test_capture_source_uses_evidence_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = UIController(FakeClient(), evidence_base=tmp).capture_source()  # type: ignore[arg-type]

            self.assertTrue(result.name.endswith("-wda-source.xml"))
            self.assertEqual(result.parent.name, "openclaw-iphone-ops")
            self.assertEqual(result.read_text(encoding="utf-8"), "<App />")

    def test_tap_delegates_to_client(self) -> None:
        client = FakeClient()

        UIController(client).tap(12, 34)  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("tap", (12, 34), {})])

    def test_type_text_delegates_to_client(self) -> None:
        client = FakeClient()

        UIController(client).type_text("hello", frequency=15)  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("type_text", ("hello",), {"frequency": 15})])

    def test_clear_field_delegates_to_focused_client_when_no_clear_button_exists(self) -> None:
        client = FakeClient()

        result = UIController(client).clear_field()  # type: ignore[arg-type]

        self.assertIsNone(result)
        self.assertEqual(client.calls, [("clear_text", (), {})])

    def test_clear_field_prefers_visible_clear_button(self) -> None:
        source = """<XCUIElementTypeApplication>
          <XCUIElementTypeButton name="Clear" label="Clear" visible="true" x="10" y="20" width="100" height="40" />
        </XCUIElementTypeApplication>"""
        client = FakeClient(source)

        element = UIController(client).clear_field()  # type: ignore[arg-type]

        self.assertEqual(element.name, "Clear")
        self.assertEqual(client.calls, [("tap", (60.0, 40.0), {})])

    def test_clear_field_taps_target_then_visible_clear_button(self) -> None:
        source = """<XCUIElementTypeApplication>
          <XCUIElementTypeTextView name="Search field" label="Search field" visible="true" x="10" y="20" width="100" height="40" />
          <XCUIElementTypeButton name="Clear" label="Clear" visible="true" x="120" y="20" width="80" height="40" />
        </XCUIElementTypeApplication>"""
        client = FakeClient(source)

        element = UIController(client).clear_field("Search field", exact=True)  # type: ignore[arg-type]

        self.assertEqual(element.name, "Clear")
        self.assertEqual(client.calls, [("tap", (60.0, 40.0), {}), ("tap", (160.0, 40.0), {})])

    def test_press_button_delegates_to_client(self) -> None:
        client = FakeClient()

        UIController(client).press_button("home", duration=0.2)  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("press_button", ("home",), {"duration": 0.2})])

    def test_drag_delegates_to_client(self) -> None:
        client = FakeClient()

        UIController(client).drag(1, 2, 3, 4, duration=0.3)  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("drag", (1, 2, 3, 4), {"duration": 0.3})])

    def test_back_delegates_to_client(self) -> None:
        client = FakeClient()

        UIController(client).back()  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("back", (), {})])

    def test_back_falls_back_to_visible_back_button(self) -> None:
        source = """<XCUIElementTypeApplication>
          <XCUIElementTypeButton name="Back" label="Back" visible="true" x="16" y="71" width="24" height="25" />
        </XCUIElementTypeApplication>"""
        client = FailingBackClient(source)

        UIController(client).back()  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("back", (), {}), ("tap", (28.0, 83.5), {})])

    def test_back_falls_back_to_top_left_button_without_label(self) -> None:
        source = """<XCUIElementTypeApplication>
          <XCUIElementTypeButton name="profile-header-left-button" visible="true" x="18" y="75" width="24" height="24" />
        </XCUIElementTypeApplication>"""
        client = FailingBackClient(source)

        UIController(client).back()  # type: ignore[arg-type]

        self.assertEqual(client.calls, [("back", (), {}), ("tap", (30.0, 87.0), {})])

    def test_back_reports_clean_error_when_no_route_or_visible_control_exists(self) -> None:
        client = FailingBackClient("<XCUIElementTypeApplication />")

        with self.assertRaisesRegex(WDAUnavailable, "No WDA back route or visible back/close control"):
            UIController(client).back()  # type: ignore[arg-type]

    def test_parse_elements_extracts_visible_text_and_center(self) -> None:
        source = """<XCUIElementTypeApplication name="Instagram" label="Instagram">
          <XCUIElementTypeButton name="Search" label="Search" visible="true" enabled="true" x="10" y="20" width="100" height="40" />
          <XCUIElementTypeStaticText name="Hidden" label="Hidden" visible="false" x="1" y="1" width="10" height="10" />
        </XCUIElementTypeApplication>"""

        elements = parse_elements(source)

        self.assertEqual(len(elements), 2)
        self.assertEqual(elements[1].text, "Search Search")
        self.assertEqual(elements[1].center, (60.0, 40.0))
        self.assertTrue(elements[1].enabled)

    def test_tap_text_taps_matching_element_center(self) -> None:
        source = """<XCUIElementTypeApplication>
          <XCUIElementTypeButton name="Search" label="Search" visible="true" x="10" y="20" width="100" height="40" />
        </XCUIElementTypeApplication>"""
        client = FakeClient(source)

        element = UIController(client).tap_text("search")  # type: ignore[arg-type]

        self.assertEqual(element.name, "Search")
        self.assertEqual(client.calls, [("tap", (60.0, 40.0), {})])

    def test_scroll_until_text_drags_until_match(self) -> None:
        class ChangingClient(FakeClient):
            def source(self) -> str:
                if not self.calls:
                    return """<XCUIElementTypeApplication>
                      <XCUIElementTypeStaticText name="Top" visible="true" x="0" y="0" width="10" height="10" />
                    </XCUIElementTypeApplication>"""
                return """<XCUIElementTypeApplication>
                  <XCUIElementTypeButton name="Followers" visible="true" x="20" y="30" width="80" height="20" />
                </XCUIElementTypeApplication>"""

        client = ChangingClient()

        element = UIController(client).scroll_until_text("followers", max_scrolls=2)  # type: ignore[arg-type]

        self.assertEqual(element.name, "Followers")
        self.assertEqual(client.calls, [("drag", (200, 720, 200, 260), {"duration": 0.2})])

    def test_annotated_screenshot_writes_png_json_and_html(self) -> None:
        source = """<XCUIElementTypeApplication>
          <XCUIElementTypeButton name="Search" label="Search" visible="true" x="10" y="20" width="100" height="40" />
        </XCUIElementTypeApplication>"""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "annotated.png"

            screenshot, elements, html = UIController(FakeClient(source)).annotated_screenshot(str(output))  # type: ignore[arg-type]

            self.assertEqual(screenshot.read_bytes(), PNG_BYTES)
            self.assertIn('"text": "Search Search"', elements.read_text(encoding="utf-8"))
            self.assertIn("data:image/png;base64", html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
