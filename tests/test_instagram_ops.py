from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from openclaw_iphone.instagram_ops import (
    analyze_video,
    benchmark_ranking_quality,
    discover_creators,
    parse_follower_count,
    query_to_hashtags,
    triage_shortlist,
    verify_handles,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png"


class FakeWDA:
    def __init__(self, source: str) -> None:
        self.source_text = source or "<XCUIElementTypeApplication/>"
        self.calls: list[tuple[str, tuple, dict]] = []

    def with_deadline(self, seconds):
        return self

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

    def test_verify_handles_reports_uncertain_identity_with_evidence(self) -> None:
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
            self.assertEqual(item["status"], "identity_uncertain")
            self.assertTrue(Path(item["artifacts"]["deep_link_manifest"]).exists())

    def test_verify_handles_does_not_type_into_ai_follow_up_field(self) -> None:
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
            self.assertEqual(client.calls, [("open_url", ("instagram://user?username=prenatal.creator",), {})])
            self.assertEqual(item["status"], "identity_uncertain")

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
                  <XCUIElementTypeStaticText name="prenatal.creator" label="prenatal.creator" visible="true" y="29" />
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

    def test_verify_handles_only_captures_start_for_first_handle(self) -> None:
        def profile_source(handle: str) -> str:
            return f'''<XCUIElementTypeApplication bundleId="com.burbn.instagram">
              <XCUIElementTypeStaticText name="{handle}" label="{handle}" visible="true" y="29" />
              <XCUIElementTypeButton name="user-detail-header-followers" value="42 followers" />
            </XCUIElementTypeApplication>'''

        class CountingWDA(FakeWDA):
            def __init__(self) -> None:
                super().__init__(profile_source("first.creator"))
                self.source_reads = 0
                self.screenshots = 0

            def source(self) -> str:
                self.source_reads += 1
                return super().source()

            def screenshot(self) -> bytes:
                self.screenshots += 1
                return super().screenshot()

            def open_url(self, url: str) -> None:
                super().open_url(url)
                self.source_text = profile_source("second.creator")

        client = CountingWDA()
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["first.creator", "second.creator"],
                output_dir=tmp,
                max_steps_per_handle=2,
            )

            first, second = result.payload["handles"]
            self.assertEqual(first["status"], "captured_current_context_match")
            self.assertEqual([step["name"] for step in first["steps"]], ["capture-start"])
            self.assertTrue(Path(first["artifacts"]["start_manifest"]).exists())
            self.assertEqual(second["status"], "captured_deep_link")
            self.assertTrue(second["identity_verified"])
            self.assertEqual(second["observed_handle"], "second.creator")
            self.assertEqual([step["name"] for step in second["steps"]], ["open-profile-deep-link", "capture-deep-link"])
            self.assertNotIn("start_manifest", second["artifacts"])
            self.assertTrue(Path(second["artifacts"]["deep_link_manifest"]).exists())
            self.assertEqual(client.source_reads, 2)
            self.assertEqual(client.screenshots, 2)
            self.assertEqual(client.calls, [("open_url", ("instagram://user?username=second.creator",), {})])

    def test_verify_handles_later_wrong_profile_is_not_attributed_to_requested_handle(self) -> None:
        first_source = '''<XCUIElementTypeApplication bundleId="com.burbn.instagram">
          <XCUIElementTypeStaticText name="first.creator" label="first.creator" visible="true" y="29" />
          <XCUIElementTypeButton name="user-detail-header-followers" value="42 followers" />
        </XCUIElementTypeApplication>'''
        wrong_source = '''<XCUIElementTypeApplication bundleId="com.burbn.instagram">
          <XCUIElementTypeStaticText name="wrong.creator" label="wrong.creator" visible="true" y="29" />
          <XCUIElementTypeButton name="user-detail-header-followers" value="999 followers" />
        </XCUIElementTypeApplication>'''

        class WrongProfileWDA(FakeWDA):
            def open_url(self, url: str) -> None:
                super().open_url(url)
                self.source_text = wrong_source

        client = WrongProfileWDA(first_source)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "openclaw_iphone.instagram_ops.UIController.wait_source",
            autospec=True,
            side_effect=lambda controller, predicate, *, timeout: controller.client.source(),
        ):
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["first.creator", "wanted.creator"],
                output_dir=tmp,
                max_steps_per_handle=2,
            )

            second = result.payload["handles"][1]
            self.assertEqual(second["status"], "identity_mismatch")
            self.assertFalse(second["identity_verified"])
            self.assertEqual(second["observed_handle"], "wrong.creator")
            self.assertNotIn("profile", second)
            self.assertNotIn("start_manifest", second["artifacts"])
            self.assertTrue(Path(second["artifacts"]["deep_link_manifest"]).exists())

    def test_verify_handles_first_mismatch_still_needs_three_steps(self) -> None:
        source = '<XCUIElementTypeApplication bundleId="com.burbn.instagram" />'
        client = FakeWDA(source)
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["wanted.creator"],
                output_dir=tmp,
                max_steps_per_handle=2,
            )

            item = result.payload["handles"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("before capture-deep-link", item["error"])
            self.assertEqual([step["name"] for step in item["steps"]], ["capture-start", "open-profile-deep-link"])
            self.assertIn("start_manifest", item["artifacts"])
            self.assertIn("failure_screenshot", item["artifacts"])
            self.assertEqual(client.calls, [("open_url", ("instagram://user?username=wanted.creator",), {})])

    def test_verify_handles_later_navigation_failure_still_captures_evidence(self) -> None:
        source = '''<XCUIElementTypeApplication bundleId="com.burbn.instagram">
          <XCUIElementTypeStaticText name="first.creator" label="first.creator" visible="true" y="29" />
          <XCUIElementTypeButton name="user-detail-header-followers" value="42 followers" />
        </XCUIElementTypeApplication>'''

        class FailingWDA(FakeWDA):
            def open_url(self, url: str) -> None:
                super().open_url(url)
                raise RuntimeError("deep link failed")

        client = FailingWDA(source)
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_handles(
                client,  # type: ignore[arg-type]
                ["first.creator", "second.creator"],
                output_dir=tmp,
                max_steps_per_handle=2,
            )

            second = result.payload["handles"][1]
            self.assertEqual(second["status"], "failed")
            self.assertEqual(second["error"], "deep link failed")
            self.assertEqual([step["name"] for step in second["steps"]], ["open-profile-deep-link"])
            self.assertNotIn("start_manifest", second["artifacts"])
            self.assertTrue(Path(second["artifacts"]["failure_screenshot"]).exists())

    def test_verify_handles_does_not_treat_reel_as_profile_verification(self) -> None:
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
            self.assertEqual(item["status"], "identity_uncertain")
            self.assertFalse(item["identity_verified"])
            self.assertEqual(client.calls, [("open_url", ("instagram://user?username=prenatal.creator",), {})])

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

    def test_discover_creators_source_only_skips_profile_deep_links(self) -> None:
        class SourceOnlyWDA(FakeWDA):
            def open_url(self, url: str) -> None:
                super().open_url(url)
                if "tag?name=" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by prenatal.creator media-discovery-cell" visible="true" x="0" y="161" width="215" height="286" />
                    </XCUIElementTypeApplication>"""

        client = SourceOnlyWDA("")
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.instagram_ops.time.sleep", return_value=None):
            result = discover_creators(
                client,  # type: ignore[arg-type]
                "pregnancy journey",
                output_dir=tmp,
                max_candidates=1,
                max_source_scrolls=0,
                deadline_seconds=30,
                verification_mode="source-only",
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertTrue(result.report.exists())
            self.assertEqual(payload["verification_mode"], "source-only")
            self.assertEqual(payload["summary"]["candidates_found"], 1)
            self.assertEqual(payload["summary"]["deep_link_verified"], 0)
            item = payload["partial"][0]
            self.assertEqual(item["handle"], "prenatal.creator")
            self.assertEqual(item["verification_status"], "source_only")
            self.assertIsNone(item["follower_count"])
            self.assertFalse(item["deep_link_verified"])
            self.assertEqual([call for call in client.calls if "user?username=" in call[1][0]], [])

    def test_parse_follower_count_handles_instagram_units(self) -> None:
        self.assertEqual(parse_follower_count("1.6 thousand  "), 1600)
        self.assertEqual(parse_follower_count("9,812 followers"), 9812)
        self.assertEqual(parse_follower_count("2.4K"), 2400)

    def test_query_to_hashtags_prefers_productive_benchmark_aliases(self) -> None:
        self.assertEqual(query_to_hashtags("pregnancy journey")[:2], ["pregnancyjourney", "pregnantmom"])
        self.assertEqual(query_to_hashtags("first trimester pregnancy nausea")[:2], ["trimesterpregnancy", "pregnancy"])
        self.assertEqual(query_to_hashtags("pregnancy after loss")[:2], ["pregnancyafter", "afterloss"])

    def test_triage_shortlist_ranks_source_candidates_and_verifies_top_subset(self) -> None:
        class TriageWDA(FakeWDA):
            def open_url(self, url: str) -> None:
                super().open_url(url)
                if "tag?name=" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by aaa.unrelated media-discovery-cell" visible="true" x="0" y="448" width="215" height="286" />
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by prenatal.creator Pregnancy media-discovery-cell" visible="true" x="0" y="161" width="215" height="286" />
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by nausea.mama Pregnancy media-discovery-cell" visible="true" x="215" y="161" width="215" height="286" />
                    </XCUIElementTypeApplication>"""
                elif "prenatal.creator" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="prenatal.creator" label="prenatal.creator" visible="true" x="52" y="29" width="118" height="104" />
                      <XCUIElementTypeOther name="Prenatal Creator" label="Prenatal Creator" visible="true" x="123" y="121" width="291" height="20" />
                      <XCUIElementTypeButton name="user-detail-header-followers" value="4,812 followers" visible="true" x="203" y="141" width="109" height="70" />
                      <XCUIElementTypeLink name="user-detail-header-info-label" label="Pregnancy and prenatal wellness" visible="true" x="16" y="213" width="398" height="60" />
                      <XCUIElementTypeButton label="Video by prenatal.creator media-thumbnail-cell" visible="true" x="0" y="721" width="143" height="191" />
                    </XCUIElementTypeApplication>"""
                elif "nausea.mama" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="nausea.mama" label="nausea.mama" visible="true" x="52" y="29" width="118" height="104" />
                      <XCUIElementTypeOther name="Nausea Mama" label="Nausea Mama" visible="true" x="123" y="121" width="291" height="20" />
                      <XCUIElementTypeButton name="user-detail-header-followers" value="18.2 thousand followers" visible="true" x="203" y="141" width="109" height="70" />
                      <XCUIElementTypeLink name="user-detail-header-info-label" label="First trimester nausea notes" visible="true" x="16" y="213" width="398" height="60" />
                    </XCUIElementTypeApplication>"""

        client = TriageWDA("")
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.instagram_ops.time.sleep", return_value=None):
            result = triage_shortlist(
                client,  # type: ignore[arg-type]
                output_dir=tmp,
                scenarios=("pregnancy journey",),
                max_candidates_per_scenario=3,
                source_deadline_seconds=30,
                verify_top=2,
                shortlist_size=1,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertTrue(result.report.exists())
            self.assertEqual(payload["summary"]["triage_candidates_found"], 3)
            self.assertEqual(payload["summary"]["verified_count"], 2)
            self.assertEqual(payload["summary"]["shortlist_count"], 1)
            self.assertEqual(payload["shortlisted_verified_creators"][0]["deep_link_verified"], True)
            self.assertEqual(payload["shortlisted_verified_creators"][0]["handle"], "prenatal.creator")
            deep_links = [call[1][0] for call in client.calls if "user?username=" in call[1][0]]
            self.assertEqual(deep_links, ["instagram://user?username=nausea.mama", "instagram://user?username=prenatal.creator"])
            self.assertEqual([candidate["handle"] for candidate in payload["ranked_triage_candidates"]],
                             ["nausea.mama", "prenatal.creator", "aaa.unrelated"])
            self.assertEqual([candidate["handle"] for candidate in payload["unresolved_candidates_needing_manual_review"]],
                             ["aaa.unrelated"])

    def test_benchmark_ranking_quality_compares_top_and_lower_ranked_samples(self) -> None:
        class RankingWDA(FakeWDA):
            def open_url(self, url: str) -> None:
                super().open_url(url)
                if "tag?name=" in url:
                    self.comparison_credible = "trimesterpregnancy" in url
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by prenatal.creator Pregnancy media-discovery-cell" visible="true" x="0" y="161" width="215" height="286" />
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by nausea.mama Pregnancy media-discovery-cell" visible="true" x="215" y="161" width="215" height="286" />
                      <XCUIElementTypeCell name="media-discovery-cell" label="Video by lower.rank media-discovery-cell" visible="true" x="0" y="448" width="215" height="286" />
                    </XCUIElementTypeApplication>"""
                elif "prenatal.creator" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="prenatal.creator" label="prenatal.creator" visible="true" x="52" y="29" width="118" height="104" />
                      <XCUIElementTypeButton name="user-detail-header-followers" value="4,812 followers" visible="true" x="203" y="141" width="109" height="70" />
                      <XCUIElementTypeLink name="user-detail-header-info-label" label="Pregnancy and prenatal wellness" visible="true" x="16" y="213" width="398" height="60" />
                    </XCUIElementTypeApplication>"""
                elif "nausea.mama" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="nausea.mama" label="nausea.mama" visible="true" x="52" y="29" width="118" height="104" />
                      <XCUIElementTypeButton name="user-detail-header-followers" value="18.2 thousand followers" visible="true" x="203" y="141" width="109" height="70" />
                      <XCUIElementTypeLink name="user-detail-header-info-label" label="First trimester nausea notes" visible="true" x="16" y="213" width="398" height="60" />
                    </XCUIElementTypeApplication>"""
                elif "lower.rank" in url:
                    self.source_text = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
                      <XCUIElementTypeStaticText name="lower.rank" label="lower.rank" visible="true" x="52" y="29" width="118" height="104" />
                      <XCUIElementTypeButton name="user-detail-header-followers" value="55 thousand followers" visible="true" x="203" y="141" width="109" height="70" />
                    </XCUIElementTypeApplication>"""
                    if self.comparison_credible:
                        self.source_text = self.source_text.replace('</XCUIElementTypeApplication>',
                            '<XCUIElementTypeLink name="user-detail-header-info-label" label="Pregnancy notes" '
                            'visible="true" x="16" y="213" width="398" height="60" /></XCUIElementTypeApplication>')

        client = RankingWDA("")
        with tempfile.TemporaryDirectory() as tmp, patch("openclaw_iphone.instagram_ops.time.sleep", return_value=None):
            result = benchmark_ranking_quality(
                client,  # type: ignore[arg-type]
                output_dir=tmp,
                themes=("pregnancy journey", "first trimester pregnancy nausea"),
                candidates_per_theme=3,
                verify_top=2,
                comparison_size=1,
                comparison_start_rank=2,
            )

            payload = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertTrue(result.report.exists())
            self.assertEqual(len(payload["runs"]), 2)
            for run, comparison_precision, failures in zip(payload["runs"], (0.0, 1.0), (
                    ["top_yield_below_target"], ["top_yield_below_target", "top_precision_not_above_comparison"])):
                self.assertEqual((run["top_verified_count"], run["top_credible_count"], run["top_precision"]), (2, 2, 1.0))
                self.assertEqual((run["comparison_verified_count"], run["comparison_credible_count"], run["comparison_precision"]),
                                 (1, int(comparison_precision), comparison_precision))
                self.assertEqual(run["failure_modes"], failures)
                self.assertFalse(run["passed_yield_target"])
                self.assertEqual([candidate["handle"] for candidate in run["top_verified"]], ["nausea.mama", "prenatal.creator"])
                self.assertEqual([candidate["handle"] for candidate in run["comparison_verified"]], ["lower.rank"])
            summary = payload["summary"]
            self.assertEqual({key: summary[key] for key in (
                "runs", "top_verified_count", "top_credible_count", "top_precision", "comparison_verified_count",
                "comparison_credible_count", "comparison_precision", "ranking_lift_vs_comparison", "failure_modes",
                "runs_with_at_least_5_credible_top_leads", "pass_rate", "target_passed")}, {
                "runs": 2, "top_verified_count": 4, "top_credible_count": 4, "top_precision": 1.0,
                "comparison_verified_count": 2, "comparison_credible_count": 1, "comparison_precision": 0.5,
                "ranking_lift_vs_comparison": 0.5, "failure_modes": {"top_yield_below_target": 2, "top_precision_not_above_comparison": 1},
                "runs_with_at_least_5_credible_top_leads": 0, "pass_rate": 0.0, "target_passed": False})
            self.assertEqual([call[1][0] for call in client.calls if "user?username=" in call[1][0]], [
                "instagram://user?username=nausea.mama", "instagram://user?username=prenatal.creator",
                "instagram://user?username=lower.rank"] * 2)


if __name__ == "__main__":
    unittest.main()
