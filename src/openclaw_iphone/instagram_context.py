from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree as ET

from .evidence import evidence_dir, validate_prefix, write_private
from .wda import WDAClient


@dataclass(frozen=True)
class InstagramContextCapture:
    screenshot: Path
    source: Path
    manifest: Path
    payload: dict[str, Any]


def capture_instagram_context(
    client: WDAClient,
    *,
    output_dir: str | None = None,
    prefix: str = "instagram-context",
    source_text: str | None = None,
) -> InstagramContextCapture:
    validate_prefix(prefix)
    base = evidence_dir(output_dir)
    screenshot = base / f"{prefix}.png"
    source = base / f"{prefix}.xml"
    manifest = base / f"{prefix}.json"

    write_private(screenshot, client.screenshot())
    if source_text is None:
        source_text = client.source()
    write_private(source, source_text)

    payload = parse_instagram_source(source_text)
    payload["artifacts"] = {
        "screenshot": str(screenshot),
        "source": str(source),
    }
    payload["video_understand"] = {
        "note": (
            "Use the screenshot/source as current-screen evidence. If a local video file or URL is obtained "
            "for the same reel, run `video-understand analyze <video> <prompt> --timestamps` and include "
            "this manifest for creator/profile context."
        )
    }
    write_private(manifest, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return InstagramContextCapture(screenshot=screenshot, source=source, manifest=manifest, payload=payload)


def parse_instagram_source(source_text: str) -> dict[str, Any]:
    root = ET.fromstring(source_text)
    # Standard WDA XML may wrap the application in AppiumAUT.
    if root.tag != "XCUIElementTypeApplication":
        applications = list(root.iter("XCUIElementTypeApplication"))
        if len(applications) == 1:
            root = applications[0]
    elements = visible_elements(root)
    app = {
        "bundle_id": attr(root, "bundleId"),
        "name": attr(root, "name"),
        "label": attr(root, "label"),
    }
    if app["bundle_id"] != "com.burbn.instagram":
        return {
            "app": app,
            "current_reel": None,
            "current_profile": None,
            "visible_videos": [],
            "warning": "Instagram is not the active application. Unlock the phone and launch Instagram before parsing context.",
        }
    return {
        "app": app,
        "current_reel": parse_current_reel(elements),
        "current_profile": parse_current_profile(elements),
        "visible_videos": parse_visible_videos(elements),
    }


def parse_visible_videos(elements: list[ET.Element]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for element in elements:
        label = attr(element, "label") or attr(element, "name")
        if not label:
            continue
        match = re.fullmatch(r"Video by ([A-Za-z0-9._]+)(?:\b.*)?", label)
        if not match:
            continue
        rect = rect_from_element(element)
        plays = first_descendant_value(element, "XCUIElementTypeStaticText")
        videos.append(
            {
                "creator": match.group(1),
                "label": label,
                "plays": plays,
                "rect": rect,
                "visible": attr(element, "visible") == "true",
            }
        )
    return videos


def parse_current_reel(elements: list[ET.Element]) -> dict[str, Any] | None:
    reel: dict[str, Any] = {}
    for element in elements:
        label = attr(element, "label") or attr(element, "name")
        if not label:
            continue
        match = re.fullmatch(r"Reel by ([A-Za-z0-9._]+)\.?", label)
        if match:
            reel["creator"] = match.group(1).rstrip(".")
            reel["label"] = label
        elif " likes" in label and "likes" not in reel:
            reel["likes"] = label
        elif " comments" in label and "comments" not in reel:
            reel["comments"] = label
        elif label.startswith("#") and "caption" not in reel:
            reel["caption"] = label

    return reel or None


def parse_current_profile(elements: list[ET.Element]) -> dict[str, Any] | None:
    profile: dict[str, Any] = {}
    usernames: set[str] = set()
    for element in elements:
        name = attr(element, "name")
        label = attr(element, "label")
        value = attr(element, "value")
        if name == "user-detail-header-followers" and value:
            profile["followers"] = value
        elif name == "user-detail-header-following-button" and value:
            profile["following"] = value
        elif name == "user-detail-header-media-button" and value:
            profile["posts"] = value
        elif name == "user-detail-header-info-label" and label:
            profile["bio"] = label
        elif (
            "display_name" not in profile
            and element.tag in {"XCUIElementTypeOther", "XCUIElementTypeStaticText"}
            and label
            and name == label
            and not is_probable_instagram_handle(label)
            and not label.casefold().startswith(("posts", "followers", "following"))
            and 90 <= (parse_int(attr(element, "y")) or 0) <= 220
        ):
            profile["display_name"] = label
        elif (
            element.tag == "XCUIElementTypeStaticText"
            and name
            and is_probable_instagram_handle(name)
            and name == label
            and attr(element, "visible") == "true"
            and (parse_int(attr(element, "y")) is not None)
            and 0 <= parse_int(attr(element, "y")) < 110
        ):
            # Only an unambiguous visible header, never arbitrary body/reel text.
            usernames.add(name.casefold())

    if len(usernames) == 1:
        profile["username"] = next(iter(usernames))

    profile_signals = {"followers", "following", "posts", "bio"}
    if profile_signals.intersection(profile):
        return profile
    return None


def visible_elements(root: ET.Element) -> list[ET.Element]:
    """Exclude hidden subtrees, not just individual nodes."""
    if attr(root, "visible") == "false":
        return []
    return [root] + [node for child in root for node in visible_elements(child)]


def first_descendant_value(element: ET.Element, tag: str) -> str | None:
    for child in element.iter():
        if child.tag == tag:
            return attr(child, "value") or attr(child, "label") or attr(child, "name")
    return None


def rect_from_element(element: ET.Element) -> dict[str, int | None]:
    return {
        key: parse_int(attr(element, key))
        for key in ("x", "y", "width", "height")
    }


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def attr(element: ET.Element, name: str) -> str | None:
    value = element.attrib.get(name)
    return value if value else None


def is_probable_instagram_handle(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._]{1,30}", value)) and not value.startswith("user-detail")
