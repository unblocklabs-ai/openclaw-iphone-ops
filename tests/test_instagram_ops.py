from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from openclaw_iphone.instagram_ops import analyze_video, query_field_candidates, verify_handles
from openclaw_iphone.ui import parse_elements


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png"


class FakeWDA:
    def __init__(self, source: str) -> None:
        self.source_text = source
        self.calls: list[tuple[str, tuple, dict]] = []

    def source(self) -> str:
        return self.source_text

    def screenshot(self) -> bytes:
        return PNG_BYTES

    def tap(self, x: float, y: float) -> None:
        self.calls.append(("tap", (x, y), {}))

    def type_text(self, text: str, *, frequency: int | None = None) -> None:
        self.calls.append(("type_text", (text,), {"frequency": frequency}))

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float, *, duration: float = 0.1) -> None:
        self.calls.append(("drag", (from_x, from_y, to_x, to_y), {"duration": duration}))

    def open_url(self, url: str) -> None:
        self.calls.append(("open_url", (url,), {}))


class InstagramOpsTests(unittest.TestCase):
    def test_analyze_video_dry_run_writes_context_and_handoff_manifest(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeOther label="Reel by prenatal.creator." />
        </XCUIElementTypeApplication>"""
        with tempfile.TemporaryDirectory() as tmp:
            result = analyze_video(
                FakeWDA(source),  # type: ignore[arg-type]
                "https://example.com/video.mp4",
                prompt="Analyze pregnancy relevance",
                output_dir=tmp,
                dry_run=True,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["video"], "https://example.com/video.mp4")
            self.assertIn("video-understand", payload["command"][0])
            self.assertTrue(Path(payload["context_manifest"]).exists())

    def test_verify_handles_writes_failure_artifacts_when_search_not_visible(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeStaticText name="Home" label="Home" visible="true" x="1" y="2" width="30" height="10" />
        </XCUIElementTypeApplication>"""
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                FakeWDA(source),  # type: ignore[arg-type]
                ["prenatal.creator"],
                output_dir=tmp,
                max_steps_per_handle=4,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            item = payload["handles"][0]
            self.assertEqual(item["handle"], "prenatal.creator")
            self.assertEqual(item["status"], "failed")
            self.assertTrue(Path(item["artifacts"]["failure_screenshot"]).exists())
            self.assertTrue(Path(item["artifacts"]["failure_elements"]).exists())

    def test_verify_handles_can_use_follow_up_field_when_search_not_visible(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeStaticText name="Ask a follow up..." label="Ask a follow up..." value="Ask a follow up..." visible="true" x="32" y="849" width="309" height="31" />
          <XCUIElementTypeButton name="Clear" label="Clear" visible="true" x="350" y="849" width="40" height="31" />
          <XCUIElementTypeButton name="prenatal.creator" label="prenatal.creator" visible="true" x="40" y="300" width="140" height="40" />
        </XCUIElementTypeApplication>"""
        client = FakeWDA(source)
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["prenatal.creator"],
                output_dir=tmp,
                max_steps_per_handle=10,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            item = payload["handles"][0]
            self.assertEqual(item["query_field"]["label"], "Ask a follow up...")
            self.assertIn(("open_url", ("instagram://user?username=prenatal.creator",), {}), client.calls)
            self.assertIn(("type_text", ("prenatal.creator",), {"frequency": 12}), client.calls)
            self.assertEqual(item["status"], "captured_without_profile_parse")

    def test_verify_handles_accepts_deep_link_profile_context(self) -> None:
        class DeepLinkWDA(FakeWDA):
            def __init__(self) -> None:
                super().__init__(
                    """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="Home" label="Home" visible="true" x="1" y="2" width="30" height="10" />
                    </XCUIElementTypeApplication>"""
                )

            def open_url(self, url: str) -> None:
                super().open_url(url)
                self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                  <XCUIElementTypeStaticText name="prenatal.creator" label="prenatal.creator" />
                  <XCUIElementTypeButton name="user-detail-header-followers" value="9,812 followers" />
                </XCUIElementTypeApplication>"""

        client = DeepLinkWDA()
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["prenatal.creator"],
                output_dir=tmp,
                max_steps_per_handle=8,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            item = payload["handles"][0]
            self.assertEqual(item["status"], "captured_deep_link")
            self.assertEqual(item["profile"]["followers"], "9,812 followers")
            self.assertIn("deep_link_manifest", item["artifacts"])

    def test_query_field_candidates_ignore_keyboard_search_key(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeKey name="Search" label="search" visible="true" x="321" y="806" width="107" height="56" />
          <XCUIElementTypeTextView name="search-bar-text-view" visible="true" x="32" y="556" width="337" height="33" />
        </XCUIElementTypeApplication>"""

        candidates = query_field_candidates(parse_elements(source))

        self.assertEqual(candidates[0].type, "XCUIElementTypeTextView")
        self.assertEqual(candidates[0].name, "search-bar-text-view")

    def test_verify_handles_accepts_current_matching_reel_without_search(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeOther label="Reel by prenatal.creator." />
        </XCUIElementTypeApplication>"""
        client = FakeWDA(source)
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["prenatal.creator"],
                output_dir=tmp,
                deadline_seconds=30,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["deadline_seconds"], 30)
            item = payload["handles"][0]
            self.assertEqual(item["status"], "captured_current_context_match")
            self.assertEqual(item["current_reel"]["creator"], "prenatal.creator")
            self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
