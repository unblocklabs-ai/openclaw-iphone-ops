from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from openclaw_iphone import cli
from openclaw_iphone.config import IPhoneConfig
from openclaw_iphone.errors import OpenClawIPhoneError
from openclaw_iphone.media import discover_cookies_file, fetch_video, normalize_social_url


class MediaTests(unittest.TestCase):
    def test_normalize_social_url_extracts_and_strips_tracking(self) -> None:
        platform, url = normalize_social_url(
            "Check this reel https://www.instagram.com/reel/DgA1FrdKylY/?knvt=abc&igsh=xyz&utm_source=ig_web_copy_link!"
        )
        self.assertEqual(platform, "instagram")
        self.assertEqual(url, "https://www.instagram.com/reel/DgA1FrdKylY/")

        platform2, url2 = normalize_social_url(
            "https://www.tiktok.com/@creator/video/7401234567890?is_from_webapp=1&sender_device=pc&_r=1"
        )
        self.assertEqual(platform2, "tiktok")
        self.assertEqual(url2, "https://www.tiktok.com/@creator/video/7401234567890")

        with self.assertRaises(ValueError):
            normalize_social_url("https://example.com/video.mp4")

        platform3, url3 = normalize_social_url("https://example.com/video.mp4", allow_any_host=True)
        self.assertEqual(platform3, "example.com")
        self.assertEqual(url3, "https://example.com/video.mp4")

        with self.assertRaises(ValueError):
            normalize_social_url("https://user:pass@www.instagram.com/reel/abc/")

    def test_cookies_file_enforces_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            cookies.chmod(0o644)
            with self.assertRaises(ValueError):
                discover_cookies_file("instagram", explicit=str(cookies))
            cookies.chmod(0o600)
            self.assertEqual(discover_cookies_file("instagram", explicit=str(cookies)), cookies)

    def test_fetch_video_downloads_sets_0600_and_retries_browser_on_login_wall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            calls: list[list[str]] = []

            def fake_runner(cmd: list[str], *, capture_output: bool, text: bool, timeout: int, check: bool):
                calls.append(cmd)
                if "--cookies-from-browser" not in cmd:
                    return mock.Mock(returncode=1, stdout="", stderr="ERROR: [Instagram] login required. Use --cookies")
                target = out_dir / "instagram-video.mp4"
                target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
                target.chmod(0o644)
                meta = {"_filename": str(target), "duration": 12.5, "title": "Robot Mold", "uploader": "bill.epsilon"}
                return mock.Mock(returncode=0, stdout=json.dumps(meta) + "\n", stderr="")

            with mock.patch("openclaw_iphone.media.shutil.which", return_value="/opt/homebrew/bin/yt-dlp"):
                res = fetch_video(
                    "https://www.instagram.com/reel/DgA1FrdKylY/?knvt=abc",
                    output_dir=out_dir,
                    cookies_from_browser="chrome",
                    runner=fake_runner,
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual(res["platform"], "instagram")
            self.assertEqual(res["canonical_url"], "https://www.instagram.com/reel/DgA1FrdKylY/")
            self.assertEqual(res["auth"], "cookies_from_browser")
            self.assertEqual(res["duration_seconds"], 12.5)
            self.assertEqual(res["uploader"], "bill.epsilon")
            downloaded = Path(res["path"])
            self.assertTrue(downloaded.is_file())
            self.assertEqual(downloaded.stat().st_mode & 0o777, 0o600)

    def test_fetch_video_reports_missing_yt_dlp(self) -> None:
        with mock.patch("openclaw_iphone.media.shutil.which", return_value=None):
            with self.assertRaises(OpenClawIPhoneError) as err:
                fetch_video("https://www.instagram.com/reel/DgA1FrdKylY/")
        self.assertIn("brew install yt-dlp", str(err.exception))

    def test_cli_media_fetch_from_pasteboard_and_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.env"
            cfg.write_text('OPENCLAW_IPHONE_RUNNER_BUNDLE_ID="com.example.WDA"\n', encoding="utf-8")
            env = {**os.environ, "HOME": tmp, "OPENCLAW_IPHONE_CONFIG": str(cfg)}

            class FakePasteWDA:
                def read_pasteboard(self, **kwargs):
                    return {"text": "https://www.instagram.com/reel/DgA1FrdKylY/?knvt=1", "swapped_app": True, "restored_app": "com.burbn.instagram", "restore_warning": False}

            fake_result = {
                "status": "ok",
                "platform": "instagram",
                "canonical_url": "https://www.instagram.com/reel/DgA1FrdKylY/",
                "path": str(Path(tmp) / "reel.mp4"),
                "size_bytes": 269649,
                "duration_seconds": 14.0,
                "title": "Mold",
                "uploader": "bill.epsilon",
                "auth": "none",
            }
            with mock.patch.dict("os.environ", env, clear=True), \
                 mock.patch("openclaw_iphone.cli.control_lock"), \
                 mock.patch("openclaw_iphone.cli.wda_client_from_args", return_value=FakePasteWDA()), \
                 mock.patch("openclaw_iphone.cli.fetch_video", return_value=fake_result) as fetch_mock:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = cli.main(["media", "fetch", "--from-pasteboard", "--json"])
            self.assertEqual(rc, 0)
            fetch_mock.assert_called_once()
            self.assertEqual(json.loads(out.getvalue())["path"], fake_result["path"])


if __name__ == "__main__":
    unittest.main()
