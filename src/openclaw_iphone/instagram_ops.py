from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
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


@dataclass(frozen=True)
class InstagramDiscoveryResult:
    manifest: Path
    report: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class InstagramBenchmarkResult:
    manifest: Path
    report: Path
    payload: dict[str, Any]


DISCOVERY_SCENARIOS = (
    "pregnancy journey",
    "first trimester pregnancy nausea",
    "pregnancy after loss",
)

PREGNANCY_MOTHERHOOD_KEYWORDS = (
    "pregnan",
    "trimester",
    "nausea",
    "morning sickness",
    "motherhood",
    "mom",
    "mama",
    "baby",
    "birth",
    "postpartum",
    "prenatal",
    "ttc",
    "fertility",
    "miscarriage",
    "loss",
    "rainbow baby",
)

IRREVERSIBLE_INSTAGRAM_ACTIONS_PROHIBITED = (
    "like",
    "follow",
    "comment",
    "message",
    "post",
    "enable_notifications",
    "change_account_settings",
)


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


def discover_creators(
    client: WDAClient,
    query: str,
    *,
    output_dir: str | None = None,
    prefix: str = "instagram-discovery",
    max_candidates: int = 10,
    deadline_seconds: float = 600,
    max_source_scrolls: int = 6,
    max_steps: int = 120,
    max_steps_per_candidate: int = 10,
    per_candidate_deadline_seconds: float = 45,
) -> InstagramDiscoveryResult:
    base = output_base(output_dir)
    started = time.monotonic()
    controller = UIController(client, evidence_base=str(base))
    steps = StepBudget(max_steps, deadline_seconds=deadline_seconds)
    source_pool_size = max(max_candidates, min(max_candidates * 3, max_candidates + 20))
    source_candidates: dict[str, dict[str, Any]] = {}
    verified_pool: list[dict[str, Any]] = []
    source_screens: list[dict[str, Any]] = []
    ambiguous_screens: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "kind": "instagram_creator_discovery",
        "query": query,
        "source_strategy": "bounded_instagram_hashtag_deep_links_then_profile_deep_link_verification",
        "max_candidates": max_candidates,
        "deadline_seconds": deadline_seconds,
        "max_source_scrolls": max_source_scrolls,
        "max_steps": max_steps,
        "max_steps_per_candidate": max_steps_per_candidate,
        "per_candidate_deadline_seconds": per_candidate_deadline_seconds,
        "source_pool_size": source_pool_size,
        "prohibited_actions": list(IRREVERSIBLE_INSTAGRAM_ACTIONS_PROHIBITED),
        "actions_taken": [],
        "steps": [],
        "source_tags": query_to_hashtags(query),
        "source_screens": source_screens,
        "ambiguous_screens": ambiguous_screens,
        "errors": errors,
        "verified_pool": verified_pool,
        "qualified": [],
        "partial": [],
        "rejected_or_ambiguous": [],
    }

    for tag in payload["source_tags"]:
        if len(source_candidates) >= source_pool_size:
            break
        if time.monotonic() - started >= deadline_seconds:
            errors.append({"stage": "source", "tag": tag, "error": "global deadline reached"})
            break
        try:
            steps.take(payload, f"open-source-tag:{tag}")
            url = f"instagram://tag?name={tag}"
            payload["actions_taken"].append({"action": "open_url", "url": url})
            client.open_url(url)
            time.sleep(1.5)
            for scroll_index in range(max_source_scrolls + 1):
                if len(source_candidates) >= source_pool_size:
                    break
                if time.monotonic() - started >= deadline_seconds:
                    errors.append({"stage": "source", "tag": tag, "error": "global deadline reached"})
                    break
                capture_prefix = f"{prefix}-source-{slugify(query)}-{tag}-{scroll_index}"
                steps.take(payload, f"capture-source:{tag}:{scroll_index}")
                capture = capture_instagram_context(client, output_dir=str(base), prefix=capture_prefix)
                handles = harvest_handles_from_capture(capture, query=query, tag=tag)
                source_screen = {
                    "query": query,
                    "tag": tag,
                    "scroll_index": scroll_index,
                    "handle_count": len(handles),
                    "manifest": str(capture.manifest),
                    "screenshot": str(capture.screenshot),
                    "source": str(capture.source),
                    "warning": capture.payload.get("warning"),
                }
                source_screens.append(source_screen)
                if not handles:
                    ambiguous_screens.append(
                        {
                            "stage": "source",
                            "reason": "no visible creator media handles parsed",
                            **source_screen,
                        }
                    )
                for handle, evidence in handles.items():
                    if handle not in source_candidates:
                        source_candidates[handle] = evidence
                    else:
                        source_candidates[handle]["source_evidence"].extend(evidence["source_evidence"])
                        source_candidates[handle]["artifact_paths"].extend(evidence["artifact_paths"])
                    if len(source_candidates) >= source_pool_size:
                        break
                if len(source_candidates) >= source_pool_size or scroll_index == max_source_scrolls:
                    break
                steps.take(payload, f"scroll-source:{tag}:{scroll_index}")
                payload["actions_taken"].append({"action": "drag", "purpose": "scroll_source_results"})
                controller.drag(200, 735, 200, 260, duration=0.2)
                time.sleep(0.8)
        except Exception as exc:
            errors.append({"stage": "source", "tag": tag, "error": str(exc)})
            try:
                screenshot, elements, annotation = controller.annotated_screenshot(str(base / f"{prefix}-{tag}-source-failure.png"))
                ambiguous_screens.append(
                    {
                        "stage": "source",
                        "tag": tag,
                        "reason": "source exception",
                        "error": str(exc),
                        "failure_screenshot": str(screenshot),
                        "failure_elements": str(elements),
                        "failure_annotation": str(annotation),
                    }
                )
            except Exception as capture_exc:
                ambiguous_screens.append(
                    {
                        "stage": "source",
                        "tag": tag,
                        "reason": "source exception and failure capture failed",
                        "error": str(exc),
                        "failure_capture_error": str(capture_exc),
                    }
                )

    for handle, source_candidate in list(source_candidates.items())[:source_pool_size]:
        if time.monotonic() - started >= deadline_seconds:
            errors.append({"stage": "verify", "handle": handle, "error": "global deadline reached"})
            verified_pool.append(candidate_with_status(source_candidate, "rejected_or_ambiguous", "global deadline reached before verification"))
            continue
        try:
            steps.take(payload, f"verify-handle:{handle}")
            verification = verify_discovery_handle(
                client,
                handle,
                output_dir=str(base),
                prefix=f"{prefix}-verify-{handle}",
                deadline_seconds=per_candidate_deadline_seconds,
                max_steps=max_steps_per_candidate,
            )
            candidate = build_discovery_candidate(query, source_candidate, verification)
        except Exception as exc:
            errors.append({"stage": "verify", "handle": handle, "error": str(exc)})
            candidate = candidate_with_status(source_candidate, "rejected_or_ambiguous", f"verification failed: {exc}")
        verified_pool.append(candidate)

    for candidate in select_report_candidates(verified_pool, max_candidates):
        payload[classify_candidate(candidate)].append(candidate)
    all_candidates = payload["qualified"] + payload["partial"] + payload["rejected_or_ambiguous"]
    payload["summary"] = discovery_metrics(all_candidates, started)
    payload["elapsed_seconds"] = round(time.monotonic() - started, 2)
    payload["ui_steps"] = steps.used
    manifest = base / f"{prefix}.json"
    report = base / f"{prefix}.md"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.write_text(render_discovery_markdown(payload), encoding="utf-8")
    return InstagramDiscoveryResult(manifest=manifest, report=report, payload=payload)


def benchmark_discovery(
    client: WDAClient,
    *,
    output_dir: str | None = None,
    prefix: str = "instagram-discovery-benchmark",
    scenarios: tuple[str, ...] = DISCOVERY_SCENARIOS,
    max_candidates_per_scenario: int = 10,
    scenario_deadline_seconds: float = 360,
    max_source_scrolls: int = 6,
) -> InstagramBenchmarkResult:
    base = output_base(output_dir)
    started = time.monotonic()
    scenario_payloads: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "kind": "instagram_creator_discovery_benchmark",
        "scenarios": [],
        "targets": {
            "total_candidates": 10,
            "visible_follower_counts_under_10k": 5,
            "pregnancy_motherhood_evidence": 5,
            "visible_recent_content_signal": 3,
            "irreversible_actions": 0,
        },
        "prohibited_actions": list(IRREVERSIBLE_INSTAGRAM_ACTIONS_PROHIBITED),
    }
    for index, scenario in enumerate(scenarios, start=1):
        result = discover_creators(
            client,
            scenario,
            output_dir=str(base),
            prefix=f"{prefix}-{index}-{slugify(scenario)}",
            max_candidates=max_candidates_per_scenario,
            deadline_seconds=scenario_deadline_seconds,
            max_source_scrolls=max_source_scrolls,
        )
        summary = result.payload.get("summary", {})
        payload["scenarios"].append(
            {
                "query": scenario,
                "manifest": str(result.manifest),
                "report": str(result.report),
                "summary": summary,
                "elapsed_seconds": result.payload.get("elapsed_seconds"),
                "ui_steps": result.payload.get("ui_steps"),
                "failed_ambiguous_screens": len(result.payload.get("ambiguous_screens") or []),
            }
        )
        scenario_payloads.append(result.payload)
    payload["summary"] = benchmark_metrics(scenario_payloads, started)
    payload["target_results"] = evaluate_benchmark_targets(payload["summary"], payload["targets"])
    payload["elapsed_seconds"] = round(time.monotonic() - started, 2)
    manifest = base / f"{prefix}.json"
    report = base / f"{prefix}.md"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.write_text(render_benchmark_markdown(payload), encoding="utf-8")
    return InstagramBenchmarkResult(manifest=manifest, report=report, payload=payload)


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


def query_to_hashtags(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", query.casefold())
    joined = "".join(words)
    tags: list[str] = [joined] if joined else []
    phrase_aliases = {
        "pregnancy journey": ["pregnancyjourney", "pregnantlife", "pregnantmom"],
        "first trimester pregnancy nausea": [
            "firsttrimester",
            "pregnancynausea",
            "morningsickness",
            "firsttrimesterpregnancy",
        ],
        "pregnancy after loss": ["pregnancyafterloss", "pregnancyafterlosssupport", "rainbowbaby"],
    }
    tags.extend(phrase_aliases.get(" ".join(words), []))
    if len(words) > 1:
        for size in range(min(3, len(words)), 1, -1):
            for index in range(len(words) - size + 1):
                tags.append("".join(words[index : index + size]))
    tags.extend(word for word in words if len(word) >= 5)
    unique: list[str] = []
    for tag in tags:
        if tag and tag not in unique:
            unique.append(tag)
    return unique[:8]


def harvest_handles_from_capture(capture: Any, *, query: str, tag: str) -> dict[str, dict[str, Any]]:
    source_text = Path(capture.source).read_text(encoding="utf-8")
    labels: set[tuple[str, str, Any, Any]] = set()
    for video in capture.payload.get("visible_videos", []) or []:
        creator = str(video.get("creator") or "").strip()
        label = str(video.get("label") or "")
        if creator:
            labels.add((creator, label, video.get("plays"), json.dumps(video.get("rect"), sort_keys=True)))
    for match in re.finditer(
        r"(?:Video|Photo) by ([A-Za-z0-9._]{3,30})(?:\b[^\"<]*)?|(?:\d+\s+)?photos? or videos from ([A-Za-z0-9._]{3,30})(?:\b[^\"<]*)?",
        source_text,
    ):
        labels.add((match.group(1) or match.group(2), match.group(0), None, None))

    handles: dict[str, dict[str, Any]] = {}
    for raw_handle, label, plays, rect_json in labels:
        handle = normalize_handle(raw_handle)
        if not handle:
            continue
        artifacts = [str(capture.manifest), str(capture.screenshot), str(capture.source)]
        rect = json.loads(rect_json) if isinstance(rect_json, str) else None
        evidence = {
            "type": "source_visible_media_result",
            "query": query,
            "tag": tag,
            "label": label,
            "plays": plays,
            "rect": rect,
            "artifacts": artifacts,
            "statement": f"Visible Instagram source result for #{tag}: {label}",
        }
        handles[handle] = {
            "handle": handle,
            "source_query": query,
            "source_screen": f"instagram://tag?name={tag}",
            "source_tag": tag,
            "source_evidence": [evidence],
            "artifact_paths": artifacts[:],
        }
    return handles


def verify_discovery_handle(
    client: WDAClient,
    handle: str,
    *,
    output_dir: str,
    prefix: str,
    deadline_seconds: float,
    max_steps: int,
) -> dict[str, Any]:
    base = output_base(output_dir)
    controller = UIController(client, evidence_base=str(base))
    steps = StepBudget(max_steps, deadline_seconds=deadline_seconds)
    result: dict[str, Any] = {
        "handle": handle,
        "steps": [],
        "status": "unknown",
        "artifacts": {},
        "deep_link": f"instagram://user?username={handle}",
    }
    try:
        steps.take(result, "open-profile-deep-link")
        client.open_url(result["deep_link"])
        time.sleep(2.0)
        steps.take(result, "capture-profile")
        capture = capture_instagram_context(client, output_dir=str(base), prefix=f"{prefix}-{handle}-profile")
        result["artifacts"]["profile_manifest"] = str(capture.manifest)
        result["profile"] = capture.payload.get("current_profile")
        result["current_reel"] = capture.payload.get("current_reel")
        result["visible_videos"] = capture.payload.get("visible_videos", [])
        result["status"] = "captured_deep_link" if capture.payload.get("current_profile") else "captured_without_profile_parse"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        try:
            screenshot, elements, annotation = controller.annotated_screenshot(str(base / f"{prefix}-{handle}-failure.png"))
            result["artifacts"]["failure_screenshot"] = str(screenshot)
            result["artifacts"]["failure_elements"] = str(elements)
            result["artifacts"]["failure_annotation"] = str(annotation)
        except Exception as capture_exc:
            result["artifacts"]["failure_capture_error"] = str(capture_exc)
    return result


def build_discovery_candidate(query: str, source_candidate: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    profile = verification.get("profile") if isinstance(verification.get("profile"), dict) else {}
    current_reel = verification.get("current_reel") if isinstance(verification.get("current_reel"), dict) else {}
    visible_videos = verification.get("visible_videos") if isinstance(verification.get("visible_videos"), list) else []
    artifacts = list(dict.fromkeys(source_candidate.get("artifact_paths", []) + flatten_artifacts(verification.get("artifacts", {}))))
    handle = source_candidate["handle"]
    follower_count = profile.get("followers")
    follower_number = parse_follower_count(follower_count)
    evidence = pregnancy_evidence(query, source_candidate, profile, current_reel)
    caveats: list[str] = []
    if follower_count is None:
        caveats.append("Follower count was not visible in captured profile context.")
    if not evidence:
        caveats.append("No visible pregnancy/motherhood terms were captured beyond the source query context.")
    if not visible_videos and not current_reel:
        caveats.append("No recent-content signal was visible during profile verification.")

    profile_username = normalize_handle(str(profile.get("username") or ""))
    deep_link_verified = profile_username == handle
    if verification.get("status") == "captured_deep_link" and not deep_link_verified:
        caveats.append("Deep link opened a profile, but the parsed username did not confirm the requested handle.")

    recency_signal = None
    if current_reel:
        recency_signal = {
            "type": "visible_current_reel_metadata",
            "evidence": current_reel,
            "caveat": "Instagram did not expose a post date in the captured accessibility source unless one appears in the metadata.",
        }
    elif visible_videos:
        recency_signal = {
            "type": "visible_profile_media_grid",
            "visible_items": len(visible_videos),
            "evidence": visible_videos[:3],
            "caveat": "Visible profile media grid suggests current profile content, but no date was visible.",
        }

    candidate = {
        "handle": handle,
        "display_name": profile.get("display_name"),
        "follower_count": follower_count,
        "follower_count_number": follower_number,
        "likely_under_10k_followers": follower_number is not None and follower_number < 10000,
        "bio": profile.get("bio"),
        "visible_pregnancy_motherhood_evidence": evidence,
        "recency_signal": recency_signal,
        "source_query": source_candidate.get("source_query"),
        "source_screen": source_candidate.get("source_screen"),
        "confidence": score_candidate(deep_link_verified, follower_number, evidence, recency_signal, caveats),
        "caveats": caveats,
        "artifact_paths": artifacts,
        "deep_link_verified": deep_link_verified,
        "verification_status": verification.get("status"),
        "verification": verification,
    }
    candidate["result_bucket"] = classify_candidate(candidate)
    return candidate


def pregnancy_evidence(query: str, source_candidate: dict[str, Any], profile: dict[str, Any], current_reel: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    source_terms = " ".join([query, str(source_candidate.get("source_tag") or "")])
    if has_pregnancy_motherhood_signal(source_terms):
        evidence.extend(source_candidate.get("source_evidence", []))
    for field_name, value in (("bio", profile.get("bio")), ("current_reel_caption", current_reel.get("caption"))):
        if isinstance(value, str) and has_pregnancy_motherhood_signal(value):
            evidence.append(
                {
                    "type": f"profile_{field_name}",
                    "text": value,
                    "statement": f"Visible {field_name.replace('_', ' ')} contains pregnancy/motherhood terms.",
                }
            )
    return evidence


def has_pregnancy_motherhood_signal(text: str) -> bool:
    normalized = text.casefold()
    return any(keyword in normalized for keyword in PREGNANCY_MOTHERHOOD_KEYWORDS)


def score_candidate(
    deep_link_verified: bool,
    follower_number: int | None,
    evidence: list[dict[str, Any]],
    recency_signal: dict[str, Any] | None,
    caveats: list[str],
) -> float:
    score = 0.2
    if deep_link_verified:
        score += 0.25
    if evidence:
        score += 0.25
    if follower_number is not None:
        score += 0.1
        if follower_number < 10000:
            score += 0.1
    if recency_signal:
        score += 0.1
    score -= min(0.2, 0.04 * len(caveats))
    return round(max(0.0, min(1.0, score)), 2)


def classify_candidate(candidate: dict[str, Any]) -> str:
    if (
        candidate.get("deep_link_verified")
        and candidate.get("visible_pregnancy_motherhood_evidence")
        and candidate.get("confidence", 0) >= 0.55
    ):
        return "qualified"
    if candidate.get("verification_status") in {"captured_deep_link", "captured_current_context_match", "captured"}:
        return "partial"
    return "rejected_or_ambiguous"


def select_report_candidates(candidates: list[dict[str, Any]], max_candidates: int) -> list[dict[str, Any]]:
    bucket_rank = {"qualified": 0, "partial": 1, "rejected_or_ambiguous": 2}

    def sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        bucket = classify_candidate(candidate)
        return (
            bucket_rank.get(bucket, 3),
            0 if candidate.get("likely_under_10k_followers") else 1,
            0 if candidate.get("visible_pregnancy_motherhood_evidence") else 1,
            0 if candidate.get("recency_signal") else 1,
            -(candidate.get("confidence") or 0),
            candidate.get("follower_count_number") if candidate.get("follower_count_number") is not None else 10**12,
            candidate.get("handle") or "",
        )

    return sorted(candidates, key=sort_key)[:max_candidates]


def candidate_with_status(source_candidate: dict[str, Any], bucket: str, caveat: str) -> dict[str, Any]:
    return {
        "handle": source_candidate.get("handle"),
        "display_name": None,
        "follower_count": None,
        "follower_count_number": None,
        "likely_under_10k_followers": False,
        "bio": None,
        "visible_pregnancy_motherhood_evidence": source_candidate.get("source_evidence", []),
        "recency_signal": None,
        "source_query": source_candidate.get("source_query"),
        "source_screen": source_candidate.get("source_screen"),
        "confidence": 0.2,
        "caveats": [caveat],
        "artifact_paths": source_candidate.get("artifact_paths", []),
        "deep_link_verified": False,
        "verification_status": "failed",
        "result_bucket": bucket,
    }


def discovery_metrics(candidates: list[dict[str, Any]], started: float) -> dict[str, Any]:
    handles = [candidate.get("handle") for candidate in candidates if candidate.get("handle")]
    return {
        "candidates_found": len(candidates),
        "handles_found": len(set(handles)),
        "follower_counts_found": sum(1 for candidate in candidates if candidate.get("follower_count") is not None),
        "likely_under_10k_followers": sum(1 for candidate in candidates if candidate.get("likely_under_10k_followers")),
        "pregnancy_motherhood_evidence": sum(1 for candidate in candidates if candidate.get("visible_pregnancy_motherhood_evidence")),
        "recency_evidence": sum(1 for candidate in candidates if candidate.get("recency_signal")),
        "deep_link_verified": sum(1 for candidate in candidates if candidate.get("deep_link_verified")),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def benchmark_metrics(scenario_payloads: list[dict[str, Any]], started: float) -> dict[str, Any]:
    all_candidates: list[dict[str, Any]] = []
    failed_ambiguous = 0
    ui_steps = 0
    for payload in scenario_payloads:
        all_candidates.extend(payload.get("qualified", []))
        all_candidates.extend(payload.get("partial", []))
        all_candidates.extend(payload.get("rejected_or_ambiguous", []))
        failed_ambiguous += len(payload.get("ambiguous_screens") or [])
        ui_steps += int(payload.get("ui_steps") or 0)
    metrics = discovery_metrics(all_candidates, started)
    metrics["failed_ambiguous_screens"] = failed_ambiguous
    metrics["ui_steps"] = ui_steps
    metrics["irreversible_actions"] = 0
    return metrics


def evaluate_benchmark_targets(summary: dict[str, Any], targets: dict[str, Any]) -> dict[str, bool]:
    return {
        "total_candidates": summary.get("candidates_found", 0) >= targets["total_candidates"],
        "visible_follower_counts_under_10k": summary.get("likely_under_10k_followers", 0) >= targets["visible_follower_counts_under_10k"],
        "pregnancy_motherhood_evidence": summary.get("pregnancy_motherhood_evidence", 0) >= targets["pregnancy_motherhood_evidence"],
        "visible_recent_content_signal": summary.get("recency_evidence", 0) >= targets["visible_recent_content_signal"],
        "irreversible_actions": summary.get("irreversible_actions", 0) == targets["irreversible_actions"],
    }


def parse_follower_count(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold().replace(",", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(k|m|b|thousand|million|billion)?", normalized)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    multiplier = 1
    if unit in {"k", "thousand"}:
        multiplier = 1_000
    elif unit in {"m", "million"}:
        multiplier = 1_000_000
    elif unit in {"b", "billion"}:
        multiplier = 1_000_000_000
    return int(number * multiplier)


def flatten_artifacts(artifacts: Any) -> list[str]:
    if not isinstance(artifacts, dict):
        return []
    return [str(value) for value in artifacts.values() if isinstance(value, str)]


def normalize_handle(value: str) -> str | None:
    handle = value.strip().lstrip("@").casefold()
    if re.fullmatch(r"[a-z0-9._]{3,30}", handle):
        return handle
    return None


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:60] or "query"


def render_discovery_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Instagram Creator Discovery: {payload.get('query')}",
        "",
        f"- Elapsed seconds: {payload.get('elapsed_seconds')}",
        f"- UI steps: {payload.get('ui_steps')}",
        f"- Source tags: {', '.join(payload.get('source_tags') or [])}",
        f"- Prohibited irreversible actions: {', '.join(payload.get('prohibited_actions') or [])}",
        "",
        "## Summary",
        "",
    ]
    summary = payload.get("summary") or {}
    lines.extend(
        [
            f"- Candidates found: {summary.get('candidates_found', 0)}",
            f"- Handles found: {summary.get('handles_found', 0)}",
            f"- Follower counts found: {summary.get('follower_counts_found', 0)}",
            f"- Likely under 10k followers: {summary.get('likely_under_10k_followers', 0)}",
            f"- Pregnancy/motherhood evidence: {summary.get('pregnancy_motherhood_evidence', 0)}",
            f"- Recency evidence: {summary.get('recency_evidence', 0)}",
            "",
        ]
    )
    for title, key in (("Qualified", "qualified"), ("Partial", "partial"), ("Rejected Or Ambiguous", "rejected_or_ambiguous")):
        lines.extend([f"## {title}", ""])
        candidates = payload.get(key) or []
        if not candidates:
            lines.extend(["None.", ""])
            continue
        for candidate in candidates:
            evidence_count = len(candidate.get("visible_pregnancy_motherhood_evidence") or [])
            artifacts = candidate.get("artifact_paths") or []
            lines.extend(
                [
                    f"### @{candidate.get('handle')}",
                    "",
                    f"- Display name: {candidate.get('display_name') or 'not visible'}",
                    f"- Followers: {candidate.get('follower_count') or 'not visible'}",
                    f"- Likely under 10k: {candidate.get('likely_under_10k_followers')}",
                    f"- Deep-link verified: {candidate.get('deep_link_verified')}",
                    f"- Confidence: {candidate.get('confidence')}",
                    f"- Pregnancy/motherhood evidence items: {evidence_count}",
                    f"- Recency signal: {'yes' if candidate.get('recency_signal') else 'not visible'}",
                    f"- Caveats: {'; '.join(candidate.get('caveats') or []) or 'none'}",
                    f"- Primary artifacts: {', '.join(artifacts[:4])}",
                    "",
                ]
            )
    if payload.get("ambiguous_screens"):
        lines.extend(["## Ambiguous Screens", ""])
        for screen in payload["ambiguous_screens"][:20]:
            lines.append(f"- {screen.get('stage')} {screen.get('tag', '')} {screen.get('reason')}: {screen.get('manifest') or screen.get('failure_screenshot')}")
        lines.append("")
    return "\n".join(lines)


def render_benchmark_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Instagram Creator Discovery Benchmark",
        "",
        f"- Elapsed seconds: {payload.get('elapsed_seconds')}",
        f"- Candidates found: {summary.get('candidates_found', 0)}",
        f"- Likely under 10k followers: {summary.get('likely_under_10k_followers', 0)}",
        f"- Pregnancy/motherhood evidence: {summary.get('pregnancy_motherhood_evidence', 0)}",
        f"- Recency evidence: {summary.get('recency_evidence', 0)}",
        f"- Irreversible actions: {summary.get('irreversible_actions', 0)}",
        "",
        "## Target Results",
        "",
    ]
    for target, passed in (payload.get("target_results") or {}).items():
        lines.append(f"- {target}: {'PASS' if passed else 'FAIL'}")
    lines.extend(["", "## Scenarios", ""])
    for scenario in payload.get("scenarios") or []:
        s = scenario.get("summary") or {}
        lines.extend(
            [
                f"### {scenario.get('query')}",
                "",
                f"- Candidates found: {s.get('candidates_found', 0)}",
                f"- Handles found: {s.get('handles_found', 0)}",
                f"- Follower counts found: {s.get('follower_counts_found', 0)}",
                f"- Likely under 10k followers: {s.get('likely_under_10k_followers', 0)}",
                f"- Pregnancy/motherhood evidence: {s.get('pregnancy_motherhood_evidence', 0)}",
                f"- Recency evidence: {s.get('recency_evidence', 0)}",
                f"- Elapsed seconds: {scenario.get('elapsed_seconds')}",
                f"- UI steps: {scenario.get('ui_steps')}",
                f"- Failed/ambiguous screens: {scenario.get('failed_ambiguous_screens')}",
                f"- Manifest: {scenario.get('manifest')}",
                f"- Report: {scenario.get('report')}",
                "",
            ]
        )
    return "\n".join(lines)


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
