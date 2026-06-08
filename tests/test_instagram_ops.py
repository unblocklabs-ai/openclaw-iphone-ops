from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from openclaw_iphone.instagram_ops import analyze_video, discover_creators, parse_follower_count, query_field_candidates, verify_handles
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

    def test_discover_creators_harvests_source_and_deep_link_verifies_profiles(self) -> None:
        class DiscoveryWDA(FakeWDA):
            def open_url(self, url: str) -> None:
                super().open_url(url)
                if "tag?name=" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by prenatal.creator media-discovery-cell" visible="true" x="0" y="161" width="215" height="286" />
                    </XCUIElementTypeApplication>"""
                elif "prenatal.creator" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="prenatal.creator" label="prenatal.creator" visible="true" x="52" y="29" width="118" height="104" />
                      <XCUIElementTypeOther name="Prenatal Creator" label="Prenatal Creator" visible="true" x="123" y="121" width="291" height="20" />
                      <XCUIElementTypeButton name="user-detail-header-followers" value="9,812 followers" visible="true" x="203" y="141" width="109" height="70" />
                      <XCUIElementTypeLink name="user-detail-header-info-label" label="Pregnancy journey and first trimester nausea support" visible="true" x="16" y="213" width="398" height="60" />
                      <XCUIElementTypeButton label="Video by prenatal.creator media-thumbnail-cell" visible="true" x="0" y="721" width="143" height="191" />
                    </XCUIElementTypeApplication>"""

        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.instagram_ops.time.sleep", return_value=None):
            result = discover_creators(
                DiscoveryWDA(""),  # type: ignore[arg-type]
                "pregnancy journey",
                output_dir=tmp,
                max_candidates=1,
                max_source_scrolls=0,
                deadline_seconds=30,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertTrue(result.report.exists())
            self.assertEqual(payload["summary"]["candidates_found"], 1)
            self.assertEqual(payload["summary"]["likely_under_10k_followers"], 1)
            item = payload["qualified"][0]
            self.assertEqual(item["handle"], "prenatal.creator")
            self.assertEqual(item["display_name"], "Prenatal Creator")
            self.assertTrue(item["deep_link_verified"])
            self.assertTrue(item["visible_pregnancy_motherhood_evidence"])
            self.assertTrue(item["recency_signal"])

    def test_parse_follower_count_handles_instagram_units(self) -> None:
        self.assertEqual(parse_follower_count("1.6 thousand  "), 1600)
        self.assertEqual(parse_follower_count("9,812 followers"), 9812)
        self.assertEqual(parse_follower_count("2.4K"), 2400)


if __name__ == "__main__":
    unittest.main()
