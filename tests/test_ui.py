from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openclaw_iphone.ui import UIController


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png"


class FakeClient:
    def source(self) -> str:
        return "<App />"

    def screenshot(self) -> bytes:
        return PNG_BYTES


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


if __name__ == "__main__":
    unittest.main()

