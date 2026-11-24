"""Download Instagram / TikTok videos into local .mp4 files via host `yt-dlp` for video analysis."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Callable
import urllib.parse

from .config import IPhoneConfig
from .errors import OpenClawIPhoneError

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"})
TIKTOK_HOSTS = frozenset({"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"})

TRACKING_PARAMS = frozenset({
    "igsh", "igshid", "lqvp", "knvt", "fbclid", "is_from_webapp", "sender_device",
    "sender_web_id", "_r", "_t", "u_code", "preview_pb", "sharer_language",
    "share_item_id", "share_app_id", "share_link_id", "social_sharing",
    "tt_from", "checksum", "sec_uid", "timestamp",
})

MP4_FORMAT_SPEC = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"


def extract_url(text: str) -> str:
    """Extract the first HTTP(S) URL from raw clipboard or share text."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("No URL provided (clipboard or argument was empty).")
    match = URL_PATTERN.search(text.strip())
    if not match:
        raise ValueError("Input did not contain an http(s) URL.")
    return match.group(0).rstrip(".,);]>\"'")


def normalize_social_url(raw_text: str, *, allow_any_host: bool = False) -> tuple[str, str]:
    """Validate and canonicalize an Instagram or TikTok URL, stripping tracking query params."""
    url = extract_url(raw_text)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be a valid http(s) address.")
    if parsed.username or parsed.password:
        raise ValueError("URL must not contain embedded credentials.")

    host = parsed.hostname.lower()
    if host in INSTAGRAM_HOSTS:
        platform = "instagram"
    elif host in TIKTOK_HOSTS:
        platform = "tiktok"
    elif allow_any_host:
        platform = host
    else:
        raise ValueError(
            f"Unsupported host {host!r}; expected Instagram or TikTok (pass --allow-any-host to override)."
        )

    kept_query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    canonical = urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urllib.parse.urlencode(kept_query),
        "",
    ))
    return platform, canonical


def validate_cookies_file(path: Path) -> Path:
    resolved = path.expanduser()
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"Cookies file must be a regular non-symlink file: {resolved}")
    mode = resolved.stat().st_mode
    if mode & 0o077:
        raise ValueError(f"Cookies file must be owner-only (chmod 600): {resolved}")
    return resolved


def discover_cookies_file(platform: str, config: IPhoneConfig | None = None,
                          explicit: str | None = None) -> Path | None:
    if explicit:
        return validate_cookies_file(Path(explicit))
    configured = config.get("OPENCLAW_IPHONE_MEDIA_COOKIES") if config else None
    if configured:
        return validate_cookies_file(Path(configured))
    home = Path(os.environ.get("HOME") or Path.home())
    for name in (f"{platform}-cookies.txt", "social-cookies.txt"):
        candidate = home / ".openclaw" / "secrets" / name
        if candidate.is_file() and not candidate.is_symlink():
            return validate_cookies_file(candidate)
    return None


def _build_yt_dlp_command(yt_dlp: str, url: str, target_template: str, *,
                          cookies_file: Path | None = None,
                          cookies_from_browser: str | None = None) -> list[str]:
    command = [
        yt_dlp,
        "--no-playlist",
        "--no-warnings",
        "-f", MP4_FORMAT_SPEC,
        "--merge-output-format", "mp4",
        "--print-json",
        "--no-simulate",
        "-o", target_template,
    ]
    if cookies_file is not None:
        command.extend(["--cookies", str(cookies_file)])
    elif cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    command.append(url)
    return command


def _parse_yt_dlp_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue
    return {}


def _find_downloaded_file(directory: Path, stem: str, meta: dict[str, Any]) -> Path | None:
    for key in ("_filename", "filename"):
        candidate_raw = meta.get(key)
        if isinstance(candidate_raw, str) and candidate_raw:
            candidate = Path(candidate_raw)
            if candidate.is_file():
                return candidate
            mp4 = candidate.with_suffix(".mp4")
            if mp4.is_file():
                return mp4
    for ext in (".mp4", ".mov", ".mkv", ".webm"):
        candidate = directory / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    matches = sorted(
        (p for p in directory.glob(f"{stem}.*") if p.is_file() and not p.name.endswith(".part")),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return matches[0] if matches else None


def _is_auth_or_rate_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        token in lowered
        for token in ("login required", "cookies", "rate-limit", "rate limit", "not available", "403", "429")
    )


def fetch_video(raw_url: str, *, config: IPhoneConfig | None = None,
                output_path: Path | None = None, output_dir: Path | None = None,
                cookies_file: str | None = None, cookies_from_browser: str | None = None,
                allow_any_host: bool = False, timeout: int = 120,
                runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Download a social video to a local .mp4 file using `yt-dlp`."""
    platform, canonical_url = normalize_social_url(raw_url, allow_any_host=allow_any_host)
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise OpenClawIPhoneError("`yt-dlp` is not installed. Install it with `brew install yt-dlp`.")

    resolved_cookies = discover_cookies_file(platform, config=config, explicit=cookies_file)
    browser = cookies_from_browser or (config.get("OPENCLAW_IPHONE_MEDIA_COOKIES_BROWSER") if config else None)

    created_temp_dir = False
    if output_path is not None:
        target = output_path.expanduser()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        work_dir = target.parent
        stem = target.stem
        target_template = str(work_dir / f"{stem}.%(ext)s")
    else:
        if output_dir is not None:
            work_dir = output_dir.expanduser()
            work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="openclaw-iphone-media-"))
            os.chmod(work_dir, 0o700)
            created_temp_dir = True
        stem = f"{platform}-video"
        target_template = str(work_dir / f"{stem}.%(ext)s")

    attempts: list[tuple[str, Path | None, str | None]] = []
    if resolved_cookies is not None:
        attempts.append(("cookies_file", resolved_cookies, None))
    else:
        attempts.append(("none", None, None))
    if browser:
        attempts.append(("cookies_from_browser", None, browser))

    last_stderr = ""
    auth_used = "none"
    meta: dict[str, Any] = {}
    downloaded: Path | None = None

    for idx, (auth_label, ck_file, ck_browser) in enumerate(attempts):
        command = _build_yt_dlp_command(
            yt_dlp, canonical_url, target_template,
            cookies_file=ck_file, cookies_from_browser=ck_browser,
        )
        try:
            proc = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            if created_temp_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            raise OpenClawIPhoneError(f"yt-dlp timed out after {timeout}s.") from exc
        if proc.returncode == 0:
            meta = _parse_yt_dlp_json(proc.stdout or "")
            downloaded = _find_downloaded_file(work_dir, stem, meta)
            if downloaded is not None:
                auth_used = auth_label
                break
        last_stderr = (proc.stderr or "").strip()
        if idx + 1 < len(attempts) and _is_auth_or_rate_error(last_stderr):
            continue
        break

    if downloaded is None:
        if created_temp_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        hint = ""
        if _is_auth_or_rate_error(last_stderr):
            hint = (
                " Try upgrading yt-dlp (`brew upgrade yt-dlp`) or providing an owner-only cookies file "
                "via --cookies / OPENCLAW_IPHONE_MEDIA_COOKIES (~/.openclaw/secrets/instagram-cookies.txt)."
            )
        detail = last_stderr.splitlines()[-1] if last_stderr else "unknown yt-dlp failure"
        raise OpenClawIPhoneError(f"Failed to download {platform} video: {detail}.{hint}")

    if output_path is not None and downloaded != output_path.expanduser():
        final_path = output_path.expanduser()
        downloaded.replace(final_path)
        downloaded = final_path

    try:
        if stat.S_ISREG(downloaded.stat().st_mode):
            os.chmod(downloaded, 0o600)
    except OSError:
        pass

    duration = meta.get("duration")
    return {
        "status": "ok",
        "platform": platform,
        "canonical_url": canonical_url,
        "path": str(downloaded),
        "size_bytes": downloaded.stat().st_size,
        "duration_seconds": float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        "title": meta.get("title") if isinstance(meta.get("title"), str) else None,
        "uploader": (
            meta.get("uploader") or meta.get("channel")
            if isinstance(meta.get("uploader") or meta.get("channel"), str)
            else None
        ),
        "auth": auth_used,
    }
