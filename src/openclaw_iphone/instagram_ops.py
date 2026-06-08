from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from .instagram_context import capture_instagram_context
from .ui import UIController, UIElement
from .wda import WDAClient


DEFAULT_ANALYSIS_PROMPT = (
    "Analyze this Instagram video for creator research. Identify whether the creator discusses pregnancy, "
    "prenatal health, motherhood, fertility, postpartum, or adjacent topics. Return timestamped evidence, "
    "visible claims, product/category mentions, and any uncertainty."
)


@dataclass(frozen=True)
class InstagramVerifyResult:
    manifest: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class InstagramVideoAnalysisResult:
    manifest: Path
    payload: dict[str, Any]


def verify_handles(
    client: WDAClient,
    handles: list[str],
    *,
    output_dir: str | None = None,
    prefix: str = "instagram-verify",
    max_steps_per_handle: int = 12,
    deadline_seconds: float | None = None,
) -> InstagramVerifyResult:
    base = output_base(output_dir)
    controller = UIController(client, evidence_base=str(base))
    payload: dict[str, Any] = {
        "kind": "instagram_handle_verification",
        "handles": [],
        "max_steps_per_handle": max_steps_per_handle,
        "deadline_seconds": deadline_seconds,
    }

    for raw_handle in handles:
        handle = raw_handle.strip().lstrip("@")
        if not handle:
            continue
        handle_prefix = f"{prefix}-{handle}"
        result: dict[str, Any] = {
            "handle": handle,
            "steps": [],
            "status": "unknown",
            "artifacts": {},
        }
        steps = StepBudget(max_steps_per_handle, deadline_seconds=deadline_seconds)
        try:
            steps.take(result, "capture-start")
            start = capture_instagram_context(client, output_dir=str(base), prefix=f"{handle_prefix}-start")
            result["artifacts"]["start_manifest"] = str(start.manifest)
            if context_matches_handle(start.payload, handle):
                result["profile"] = start.payload.get("current_profile")
                result["current_reel"] = start.payload.get("current_reel")
                result["visible_videos"] = start.payload.get("visible_videos", [])
                result["status"] = "captured_current_context_match"
                payload["handles"].append(result)
                continue

            steps.take(result, "open-profile-deep-link")
            deep_link = f"instagram://user?username={handle}"
            result["deep_link"] = deep_link
            client.open_url(deep_link)
            time.sleep(2.0)

            steps.take(result, "capture-deep-link")
            linked = capture_instagram_context(client, output_dir=str(base), prefix=f"{handle_prefix}-deep-link")
            result["artifacts"]["deep_link_manifest"] = str(linked.manifest)
            if context_matches_handle(linked.payload, handle) or linked.payload.get("current_profile"):
                result["profile"] = linked.payload.get("current_profile")
                result["current_reel"] = linked.payload.get("current_reel")
                result["visible_videos"] = linked.payload.get("visible_videos", [])
                result["status"] = "captured_deep_link"
                payload["handles"].append(result)
                continue

            steps.take(result, "focus-query-field")
            field = focus_query_field(controller)
            result["query_field"] = field.to_dict()
            time.sleep(0.8)

            steps.take(result, "clear-query-field")
            controller.clear_field()
            time.sleep(0.3)

            steps.take(result, "type-handle")
            controller.type_text(handle, frequency=12)
            time.sleep(1.5)

            steps.take(result, "tap-result")
            controller.tap_text(handle)
            time.sleep(2.0)

            steps.take(result, "capture-profile")
            profile = capture_instagram_context(client, output_dir=str(base), prefix=f"{handle_prefix}-profile")
            result["artifacts"]["profile_manifest"] = str(profile.manifest)
            result["profile"] = profile.payload.get("current_profile")
            result["current_reel"] = profile.payload.get("current_reel")
            result["visible_videos"] = profile.payload.get("visible_videos", [])
            result["status"] = "captured" if profile.payload.get("current_profile") else "captured_without_profile_parse"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
            try:
                screenshot, elements, annotation = controller.annotated_screenshot(str(base / f"{handle_prefix}-failure.png"))
                result["artifacts"]["failure_screenshot"] = str(screenshot)
                result["artifacts"]["failure_elements"] = str(elements)
                result["artifacts"]["failure_annotation"] = str(annotation)
            except Exception as capture_exc:
                result["artifacts"]["failure_capture_error"] = str(capture_exc)
        payload["handles"].append(result)

    manifest = base / f"{prefix}.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return InstagramVerifyResult(manifest=manifest, payload=payload)


def analyze_video(
    client: WDAClient,
    video: str,
    *,
    prompt: str = DEFAULT_ANALYSIS_PROMPT,
    output_dir: str | None = None,
    prefix: str = "instagram-video-analysis",
    dry_run: bool = False,
    timeout: int = 300,
) -> InstagramVideoAnalysisResult:
    base = output_base(output_dir)
    context = capture_instagram_context(client, output_dir=str(base), prefix=f"{prefix}-context")
    analysis_output = base / f"{prefix}-gemini.json"
    command = [
        "video-understand",
        "analyze",
        video,
        prompt,
        "--timestamps",
        "--json",
        "-o",
        str(analysis_output),
    ]
    payload: dict[str, Any] = {
        "kind": "instagram_video_analysis_handoff",
        "video": video,
        "prompt": prompt,
        "context_manifest": str(context.manifest),
        "context_screenshot": str(context.screenshot),
        "context_source": str(context.source),
        "analysis_output": str(analysis_output),
        "command": command,
        "dry_run": dry_run,
    }

    if dry_run:
        payload["status"] = "dry_run"
    elif shutil.which("video-understand") is None:
        payload["status"] = "blocked"
        payload["blocker"] = "video-understand CLI was not found on PATH."
    else:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        payload["returncode"] = completed.returncode
        payload["stdout"] = completed.stdout
        payload["stderr"] = completed.stderr
        payload["status"] = "analyzed" if completed.returncode == 0 else "failed"

    manifest = base / f"{prefix}.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return InstagramVideoAnalysisResult(manifest=manifest, payload=payload)


class StepBudget:
    def __init__(self, max_steps: int, *, deadline_seconds: float | None = None) -> None:
        self.max_steps = max_steps
        self.deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        self.used = 0

    def take(self, result: dict[str, Any], name: str) -> None:
        if self.used >= self.max_steps:
            raise RuntimeError(f"Step budget exhausted before {name}.")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError(f"Deadline expired before {name}.")
        self.used += 1
        result["steps"].append({"index": self.used, "name": name})


def output_base(output_dir: str | None) -> Path:
    if output_dir:
        base = Path(output_dir).expanduser()
    else:
        base = Path.home() / ".openclaw" / "tmp" / "openclaw-iphone-ops"
    base.mkdir(parents=True, exist_ok=True)
    return base


def context_matches_handle(payload: dict[str, Any], handle: str) -> bool:
    normalized = handle.casefold().lstrip("@")
    profile = payload.get("current_profile")
    if isinstance(profile, dict) and str(profile.get("username", "")).casefold().lstrip("@") == normalized:
        return True
    reel = payload.get("current_reel")
    if isinstance(reel, dict) and str(reel.get("creator", "")).casefold().lstrip("@") == normalized:
        return True
    for video in payload.get("visible_videos", []) or []:
        if isinstance(video, dict) and str(video.get("creator", "")).casefold().lstrip("@") == normalized:
            return True
    return False


def focus_query_field(controller: UIController) -> UIElement:
    candidates = query_field_candidates(controller.elements())
    if not candidates:
        raise RuntimeError("No usable Instagram search/follow-up field was visible.")
    element = candidates[0]
    center = element.center
    if center is None:
        raise RuntimeError(f"Matched query field has no tappable frame: {element.text}")
    controller.tap(center[0], center[1])
    return element


def query_field_candidates(elements: list[UIElement]) -> list[UIElement]:
    def y(element: UIElement) -> int:
        return element.rect.get("y") or 0

    priorities: list[tuple[int, UIElement]] = []
    for element in elements:
        name = (element.name or "").casefold()
        label = (element.label or "").casefold()
        value = (element.value or "").casefold()
        text = element.text.casefold()
        if element.type == "XCUIElementTypeKey":
            continue
        if element.center is None:
            continue
        if "search-bar-text-view" in name and element.type in {"XCUIElementTypeTextView", "XCUIElementTypeOther"}:
            priorities.append((0, element))
        elif value == "ask a follow up..." or label == "ask a follow up...":
            priorities.append((1, element))
        elif "search-bar" in name:
            priorities.append((2, element))
        elif "search" in {name, label} and y(element) < 780:
            priorities.append((3, element))
        elif "search" in text and y(element) < 780:
            priorities.append((4, element))
    return [element for _, element in sorted(priorities, key=lambda item: (item[0], y(item[1])))]
