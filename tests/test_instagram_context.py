from __future__ import annotations

import unittest

from openclaw_iphone.instagram_context import parse_instagram_source


class InstagramContextTests(unittest.TestCase):
    def test_parse_visible_video_results(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeCell name="media-discovery-cell" label="Video by lifewithsavannahh" visible="true" x="0" y="311" width="215" height="287">
            <XCUIElementTypeStaticText value="11.2K" />
          </XCUIElementTypeCell>
          <XCUIElementTypeCell name="media-discovery-cell" label="Video by chloe.monroe.e" visible="true" x="0" y="598" width="215" height="287">
            <XCUIElementTypeStaticText value="140K" />
          </XCUIElementTypeCell>
        </XCUIElementTypeApplication>"""

        payload = parse_instagram_source(source)

        self.assertEqual(payload["app"]["bundle_id"], "com.burbn.instagram")
        self.assertEqual(
            payload["visible_videos"],
            [
                {
                    "creator": "lifewithsavannahh",
                    "label": "Video by lifewithsavannahh",
                    "plays": "11.2K",
                    "rect": {"x": 0, "y": 311, "width": 215, "height": 287},
                    "visible": True,
                },
                {
                    "creator": "chloe.monroe.e",
                    "label": "Video by chloe.monroe.e",
                    "plays": "140K",
                    "rect": {"x": 0, "y": 598, "width": 215, "height": 287},
                    "visible": True,
                },
            ],
        )

    def test_parse_current_reel_and_profile(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeOther label="Reel by lifewithsavannahh." />
          <XCUIElementTypeButton label="1380 likes" />
          <XCUIElementTypeStaticText label="18 comments" />
          <XCUIElementTypeStaticText label="#reel #teenpregnancy #microinfluencer" />
          <XCUIElementTypeStaticText name="lifewithsavannahh" label="lifewithsavannahh" />
          <XCUIElementTypeButton name="user-detail-header-followers" value="2.1 thousand followers" />
          <XCUIElementTypeButton name="user-detail-header-media-button" value="35 posts" />
          <XCUIElementTypeLink name="user-detail-header-info-label" label="TikTok:@.savspregnancydiary&#10;|| teen mama ||" />
        </XCUIElementTypeApplication>"""

        payload = parse_instagram_source(source)

        self.assertEqual(
            payload["current_reel"],
            {
                "creator": "lifewithsavannahh",
                "label": "Reel by lifewithsavannahh.",
                "likes": "1380 likes",
                "comments": "18 comments",
                "caption": "#reel #teenpregnancy #microinfluencer",
            },
        )
        self.assertEqual(
            payload["current_profile"],
            {
                "username": "lifewithsavannahh",
                "followers": "2.1 thousand followers",
                "posts": "35 posts",
                "bio": "TikTok:@.savspregnancydiary\n|| teen mama ||",
            },
        )

    def test_non_instagram_source_returns_warning(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.apple.springboard" name=" " label=" ">
          <XCUIElementTypeOther name="regular.view" label="Swipe up to unlock" />
        </XCUIElementTypeApplication>"""

        payload = parse_instagram_source(source)

        self.assertEqual(payload["app"]["bundle_id"], "com.apple.springboard")
        self.assertIsNone(payload["current_profile"])
        self.assertEqual(payload["visible_videos"], [])
        self.assertIn("Instagram is not the active application", payload["warning"])

    def test_lone_handle_does_not_create_profile_context(self) -> None:
        source = """<XCUIElementTypeApplication bundleId="com.burbn.instagram" name="Instagram" label="Instagram">
          <XCUIElementTypeStaticText name="hey.its.toree" label="hey.its.toree" />
        </XCUIElementTypeApplication>"""

        payload = parse_instagram_source(source)

        self.assertIsNone(payload["current_profile"])


if __name__ == "__main__":
    unittest.main()
