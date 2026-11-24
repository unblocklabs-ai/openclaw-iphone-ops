"""Direct controls. The caller decides what to do and whether it worked."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
from itertools import islice
import json
import math
import os
from pathlib import Path
import stat
import struct
import time
from typing import Callable, TypeVar

from .connection import Connection
from .errors import OpenClawIPhoneError, WDAOutcomeUnknown, WDAStaleElement, WDAUnavailable, WDATransportUnavailable, diagnostic
from .execution import Budget
from .evidence import artifact_path, write_private
from . import cache, goal, shallow
from .image_evidence import redact_png
from .inputs import (InputReadbackUnavailable, InputUnavailable, date_components, input_kind,
                     iso_date, pick_value, set_checked, set_date, validate_text, write_text)
from .observations import Observation, Selector, parse_observation
from .wda import NativeElement, WDAClient

T = TypeVar("T")


def number(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite nonnegative number.")
    return float(value)


def read_input(path: str) -> str:
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Input must be an owner-only regular file.")
        raw = stream.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("Input too large.")
    return raw.decode("utf-8").removesuffix("\n")


def _truncate_json_text(text: str, *, max_bytes: int = 32_768) -> tuple[str, bool]:
    """Truncate text so its ASCII-escaped JSON encoding stays within `max_bytes`."""
    if len(json.dumps(text, ensure_ascii=True).encode("ascii")) <= max_bytes:
        return text, False
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(json.dumps(text[:mid], ensure_ascii=True).encode("ascii")) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], True


OP_FIELDS: dict[str, set[str]] = {
    "observe": {"mode", "masks", "candidates"},
    "tap": {"target", "x", "y", "space"},
    "swipe": {"from_x", "from_y", "to_x", "to_y", "duration", "space"},
    "type": {"text", "text_ref", "target", "mode", "strategy", "frequency", "verify"},
    "pick": {"target", "value", "order", "max_steps", "offset", "seconds"},
    "set": {"target", "value", "value_ref", "kind", "components", "month_values", "strategy", "verify", "submit"},
    "press": {"button", "duration"},
    "launch": {"bundle_id", "wait_seconds"},
    "open_url": {"url"},
    "pasteboard": {"content_type", "foreground_runner", "foreground_first", "runner_bundle_id", "return_app"},
    "goal": {"goal", "app", "text", "approve", "max_steps", "grounding"},
    "sequence": {"steps"},
    "close": set(),
}
OP_REQUIRED: dict[str, set[str]] = {
    "swipe": {"from_x", "from_y", "to_x", "to_y"},
    "press": {"button"},
    "launch": {"bundle_id"},
    "open_url": {"url"},
    "pick": {"target", "value"},
    "sequence": {"steps"},
}
SEQUENCE_MAX_STEPS = 10
SEQUENCE_DISALLOWED_OPS = frozenset({"sequence", "goal", "close"})


class TargetUnavailable(OpenClawIPhoneError):
    def __init__(self, candidates: dict[str, object] | list[dict[str, object]]) -> None:
        super().__init__("Target missing or ambiguous.")
        self.candidates = candidates


class Session:
    def __init__(self, connection: Connection, *, allow_images: bool = False,
                 evidence_base: str | None = None, decisions: goal.Decisions | None = None,
                 runner_bundle_id: str | None = None) -> None:
        self.connection, self.allow_images, self.evidence_base = connection, allow_images, evidence_base
        self.decisions = decisions
        self.runner_bundle_id = runner_bundle_id
        self.closed = False
        self.snapshot: Observation | None = None
        self.image_geometry: tuple[tuple[float, float] | None, tuple[int, int]] | None = None
        self.secrets: set[str] = set()
        self.candidate_ids: set[str] | None = None
        self.shallow_apps = shallow.load()  # apps too slow to read in full (shallow.py)
        device_udid = connection.device.udid if connection.device is not None else None
        self.app_hint: str | None = (
            cache.get_cached_last_app(device_udid)
            if device_udid and os.environ.get("OPENCLAW_IPHONE_DISABLE_CACHE") != "1"
            else None
        )
        self._pasteboard_needs_swap = False

    def _set_app_hint(self, bundle_id: str | None) -> None:
        if not bundle_id or bundle_id == self.app_hint:
            self.app_hint = bundle_id or self.app_hint
            return
        self.app_hint = bundle_id
        if os.environ.get("OPENCLAW_IPHONE_DISABLE_CACHE") != "1" and self.connection.device is not None:
            cache.set_cached_last_app(self.connection.device.udid, bundle_id)

    def _read(self, read: Callable[[WDAClient], T]) -> T:
        """One safe retry per read; no lifetime recovery allowance."""
        for attempt in range(2):
            wda = self.connection.require_active()
            try:
                return read(wda)
            except WDATransportUnavailable:
                self.connection.invalidate(wda)
                if attempt:
                    raise
        raise AssertionError("Unreachable read retry state.")

    def _capture_accessibility(self) -> tuple[Observation, int]:
        def capture(depth: int, *, timeout: float | None = None) -> Observation:
            def read(wda: WDAClient) -> Observation:
                wda.snapshot_depth(depth)
                started = time.monotonic()
                scope = self._within(wda, timeout) if timeout is not None else nullcontext()
                with scope:
                    xml = wda.source(compact=True, validate=False)
                # The application root names the app and process; no separate activeAppInfo read.
                return parse_observation(xml, generation=0, device_udid=self.connection.device.udid,
                                         captured_at=datetime.now(timezone.utc).isoformat(),
                                         started=started, finished=time.monotonic())
            return self._read(read)
        self.snapshot = None
        self.candidate_ids = None
        depth = shallow.DEPTH if self.app_hint in self.shallow_apps else shallow.DEFAULT_DEPTH
        # Cap an uncached full read when the operation budget leaves enough room for a shallow fallback.
        budget = getattr(self.connection.ctl.runner, "budget", None)
        remaining = budget.remaining() if budget is not None else None
        full_timeout = (shallow.FULL_READ_TIMEOUT
                        if depth == shallow.DEFAULT_DEPTH and self.app_hint is not None
                        and (remaining is None or remaining > shallow.FULL_READ_TIMEOUT + 2.0)
                        else None)
        started = time.monotonic()
        try:
            observation = capture(depth, timeout=full_timeout)
        except (WDAStaleElement, WDAUnavailable) as exc:
            # A full read that ran long and then failed because the tree kept changing or the deadline passed is
            # the slow-app signature: remember the app and read it shallow instead of repeating the full read.
            # Lost sessions and reconnects are not: they re-raise as before.
            slow = isinstance(exc, WDAStaleElement) or getattr(exc, "category", None) == "deadline"
            if not slow or depth == shallow.DEPTH or self.app_hint is None or time.monotonic() - started < shallow.SLOW_SECONDS:
                raise
            self._remember_shallow(self.app_hint)
            observation = capture(depth := shallow.DEPTH)
        if depth == shallow.DEFAULT_DEPTH and observation.finished - observation.started > shallow.SLOW_SECONDS:
            self._remember_shallow(observation.app)  # this read already has the full tree; the next ones are capped
        elif depth == shallow.DEPTH and observation.app not in self.shallow_apps:
            observation = capture(depth := shallow.DEFAULT_DEPTH)  # another app is in front now: read it in full
        self.snapshot = observation
        self._set_app_hint(observation.app)
        return observation, depth

    def _render_accessibility(self, observation: Observation, depth: int | None, *,
                              limit: int = 80, offset: int = 0) -> dict[str, object]:
        view = observation.compact(include_labels=True, limit=limit, offset=offset, redact=self._redact)
        if depth == shallow.DEPTH:
            view["tree_depth_limit"] = depth  # deeper controls are missing from `elements`; the screenshot shows all
        return view

    def _accessibility(self, *, limit: int = 80, offset: int = 0, candidates: bool = False) -> dict[str, object]:
        if offset or candidates:
            if self.snapshot is None:
                raise ValueError("Observe a screen before paging it.")
            if candidates and self.candidate_ids is None:
                raise ValueError("No current ambiguity candidates.")
            return self.snapshot.compact(include_labels=True, limit=limit, offset=offset, redact=self._redact,
                                         ids=self.candidate_ids if candidates else None)
        observation, depth = self._capture_accessibility()
        return self._render_accessibility(observation, depth, limit=limit, offset=offset)

    def _remember_shallow(self, app: str | None) -> None:
        if app and app not in self.shallow_apps:
            self.shallow_apps.add(app)
            shallow.remember(app)

    def _save_image_bytes(self, raw: bytes, *, masks: list, size: tuple[float, float] | None = None,
                          stamp: str | None = None, capture_seconds: float = 0.0) -> dict[str, object]:
        captured = time.monotonic()
        stamp = stamp or datetime.now(timezone.utc).isoformat()
        if masks:
            assert size is not None
            raw, pixels = redact_png(raw, size, masks)
        else:
            if len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise WDAUnavailable("Invalid PNG.")
            pixels = struct.unpack_from(">II", raw, 16)
            if not 0 < pixels[0] * pixels[1] <= 12_000_000:
                raise WDAUnavailable("Invalid image geometry.")
        path = artifact_path("screen", ".png", base=self.evidence_base)
        write_private(path, raw)
        self.image_geometry = size, pixels
        return {"path": str(path), "pixels": list(pixels), "device_size": list(size) if size else None,
                "masked_regions": len(masks), "privacy": "potentially_private",
                "captured_at": stamp, "capture_seconds": capture_seconds,
                "processing_seconds": time.monotonic() - captured}

    def _image(self, masks: list) -> dict[str, object]:
        started = time.monotonic()
        def capture(wda: WDAClient):
            size = wda.window_size() if masks else None
            raw = wda.screenshot()
            return size, raw, datetime.now(timezone.utc).isoformat(), time.monotonic() - started
        size, raw, stamp, capture_seconds = self._read(capture)
        return self._save_image_bytes(raw, masks=masks, size=size, stamp=stamp, capture_seconds=capture_seconds)

    def _observe(self, mode: str, masks: list, *, limit: int, offset: int, candidates: bool = False) -> dict[str, object]:
        lanes = {name: capture for name, capture in (("image", lambda: self._image(masks)), ("accessibility", lambda: self._accessibility(limit=limit, offset=offset, candidates=candidates)))
                 if mode in (name, "both")}
        # A slow AX lane cannot starve a healthy image fallback. Captures are
        # independent and timestamped, not an atomic screen pair, so both run at once.
        # Masking reads /window/size, which shares /source's queue, so masked images go first.
        with ThreadPoolExecutor(max_workers=1 if masks else len(lanes)) as pool:
            futures = {name: pool.submit(capture) for name, capture in lanes.items()}
        result: dict[str, object] = {}
        for name, future in futures.items():
            try:
                result[name] = future.result()
            except (OpenClawIPhoneError, OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
                result[name + "_error"] = diagnostic(exc)
        return result

    def _target(self, target: str | dict) -> NativeElement:
        selector = None
        if isinstance(target, str):
            matches = [e for e in self.snapshot.elements or () if e.id == target] if self.snapshot else []
            if len(matches) != 1:
                raise TargetUnavailable([])
            using, query = self.snapshot.locator(matches[0])
        else:
            selector = Selector(**target)
            using, query = selector.locator()
        def lookup(wda: WDAClient) -> list[NativeElement]:
            refs = wda.find_elements(query, using=using)
            if using != "xpath":
                return refs
            # XPath queries leave out @visible (a full visibility pass); ask about the matches only.
            def displayed(ref: NativeElement) -> bool:
                try:
                    return wda.element_displayed(ref.ref)
                except WDAStaleElement:
                    return False
            return list(islice(filter(displayed, refs), 2))
        refs = self._read(lookup)
        if len(refs) != 1:
            self._accessibility()
            ids = None
            if selector:
                ids = {e.id for e in self.snapshot.matches(selector)}
            self.candidate_ids = ids if ids is not None else {e.id for e in self.snapshot.elements}
            view = self.snapshot.compact(include_labels=True, redact=self._redact, ids=ids)
            raise TargetUnavailable(view)
        return refs[0]

    def _points(self, data: dict) -> list[float]:
        keys = ("x", "y") if data["op"] == "tap" else ("from_x", "from_y", "to_x", "to_y")
        points = [number(data[k]) for k in keys]
        space = data.get("space", "device")
        if space not in ("device", "image"):
            raise ValueError("Unknown coordinate space.")
        if space == "image":
            if self.image_geometry is None:
                raise ValueError("Observe an image first.")
            size, pixels = self.image_geometry
            if any(v >= pixels[i % 2] for i, v in enumerate(points)):
                raise ValueError("Coordinates outside image.")
            if size is None:
                size = self._read(lambda w: w.window_size())
                if abs(pixels[0] / pixels[1] - size[0] / size[1]) > 0.02:
                    raise ValueError("Image and window orientations differ; capture a new image.")
                self.image_geometry = size, pixels
            points = [v * size[i % 2] / pixels[i % 2] for i, v in enumerate(points)]
        return points

    def _redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[supplied input]")
        return value

    @contextmanager
    def _within(self, wda: WDAClient, seconds: float):
        previous = wda.budget
        budget = Budget.seconds(seconds)
        if previous:
            budget.deadline = min(budget.deadline, previous.deadline)
        wda.budget = budget
        try:
            yield budget
        finally:
            wda.budget = previous

    def _compare(self, wda: WDAClient, ref: str | None, expected: str, *, role: str | None = None) -> str:
        try:
            ref = ref or wda.active_element()
            if (role or wda.element_type(ref)) not in {"XCUIElementTypeTextField", "XCUIElementTypeTextView", "XCUIElementTypeSearchField"}:
                return "unknown"
            value = wda.element_value(ref)
            placeholder = wda.element_placeholder(ref)
            if placeholder is not None and value == placeholder:
                return "unknown"
            return "match" if value == expected else "mismatch"
        except (OpenClawIPhoneError, OSError):
            return "unknown"

    def _ready(self, wda: WDAClient, bundle_id: str, seconds: float) -> dict[str, object]:
        started, checks = time.monotonic(), 0
        try:
            with self._within(wda, seconds) as budget:
                while True:
                    checks += 1
                    if wda.active_app()["bundleId"] == bundle_id:
                        return {"state": "ready", "checks": checks, "seconds": time.monotonic() - started}
                    budget.sleep(0.1)
        except (OpenClawIPhoneError, OSError) as exc:
            return {"state": "unknown", "checks": checks, "seconds": time.monotonic() - started,
                    "error": diagnostic(exc)}

    def request(self, data: object) -> dict[str, object]:
        started, stamp = time.monotonic(), datetime.now(timezone.utc).isoformat()
        counts, durations = self.connection.metrics.counts.copy(), self.connection.metrics.seconds.copy()
        reconnects = self.connection.reconnects
        self.connection.touch(request=True)
        try:
            result = self._request(data)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
            result = {"status": "error", "dispatch": "not_sent", "reason": "invalid_request", "error": diagnostic(exc)}
        metrics = self.connection.metrics
        result["timing"] = {"started_at": stamp, "finished_at": datetime.now(timezone.utc).isoformat(),
            "seconds": round(time.monotonic() - started, 6),
            "reconnects": self.connection.reconnects - reconnects,
            "counts": {key: value - counts[key] for key, value in metrics.counts.items() if value != counts[key]},
            "seconds_by_route": {key: round(value - durations[key], 6) for key, value in metrics.seconds.items() if value != durations[key]}}
        self.connection.touch()
        return result

    def _request(self, data: object) -> dict[str, object]:
        if self.closed or not isinstance(data, dict):
            raise ValueError("Expected an open session and request object.")
        op = data.get("op")
        if not isinstance(op, str) or op not in OP_FIELDS or set(data) - (OP_FIELDS[op] | {"op", "observe", "masks", "limit", "offset"}):
            raise ValueError("Unknown operation or field.")
        if OP_REQUIRED.get(op, set()) - set(data) or op == "tap" and data.get("target") is None and not {"x", "y"} <= set(data):
            raise ValueError("Missing operation argument.")
        candidates = data.get("candidates", False)
        if type(candidates) is not bool:
            raise ValueError("Invalid candidates flag.")
        default_mode = "both" if self.allow_images and not data.get("offset", 0) and not candidates else "accessibility"
        mode = data.get("mode", default_mode) if op == "observe" else data.get("observe")
        if mode is not None and mode not in ("image", "accessibility", "both"):
            raise ValueError("Unknown observation mode.")
        masks = data.get("masks", [])
        if not isinstance(masks, list) or any(not isinstance(r, list) or len(r) != 4 for r in masks):
            raise ValueError("Invalid masks.")
        masks = [[number(v) for v in region] for region in masks]
        limit, offset = data.get("limit", 80), data.get("offset", 0) if op != "pick" else 0
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid observation page.")
        if offset and op != "observe":
            raise ValueError("Only observations can page a captured screen.")
        if mode in ("image", "both") and not self.allow_images:
            raise ValueError("Image disclosure requires --allow-images.")
        if op == "close":
            self.closed = True
            if self.decisions is not None:
                self.decisions.close()
            return {"status": "closed"}
        if op == "goal":
            # Many operations, each with its own deadline; the navigator's calls run between them.
            if self.decisions is None:
                return {"status": "error", "dispatch": "not_sent", "reason": "goal_navigation_disabled"}
            return goal.run(self, self.decisions, data, observe_mode=mode, masks=masks, limit=limit)
        if op == "sequence":
            return self._sequence(data["steps"], mode=mode, masks=masks, limit=limit)
        acknowledged = 0
        action_started = False
        completed = False
        def send(action: Callable[[], object]) -> None:
            nonlocal acknowledged, action_started
            action_started = True
            action()
            acknowledged += 1
        def input_reply(run: Callable[[], str]) -> dict[str, object]:
            nonlocal completed
            error = None
            try:
                effect = run()
                completed = True
            except InputReadbackUnavailable as exc:
                if isinstance(exc.error, WDATransportUnavailable):
                    self.connection.invalidate()
                effect, error, completed = "unknown", diagnostic(exc.error), True
            result = {"status": "action" if acknowledged else "checked",
                      "dispatch": "acknowledged" if acknowledged and completed else "partial" if acknowledged else "not_sent",
                      "acknowledged_substeps": acknowledged, "effect": effect}
            if error:
                result["error"] = error
            if mode:
                result["observation"] = self._observe(mode, masks, limit=limit, offset=offset)
            return result
        try:
            with self.connection.operation():
                if op == "observe":
                    return {"status": "observed", "observation": self._observe(mode, masks, limit=limit, offset=offset, candidates=candidates)}
                if op == "pasteboard":
                    content_type = data.get("content_type", "plaintext")
                    foreground_runner = data.get("foreground_runner", True)
                    foreground_first = data.get("foreground_first")
                    runner_override = data.get("runner_bundle_id")
                    return_app = data.get("return_app")
                    if (content_type not in {"plaintext", "url"} or type(foreground_runner) is not bool
                            or foreground_first is not None and type(foreground_first) is not bool):
                        raise ValueError("Invalid pasteboard options.")
                    for opt in (runner_override, return_app):
                        if opt is not None and (not isinstance(opt, str) or not opt or any(ord(c) < 32 for c in opt)):
                            raise ValueError("Invalid bundle identifier.")
                    runner_id = runner_override or self.runner_bundle_id
                    skip_bg = bool(
                        foreground_runner and runner_id and (
                            foreground_first is True
                            or (foreground_first is None and self._pasteboard_needs_swap)
                        )
                    )
                    # Safe initial read reconnects if the WDA session was lost.
                    text = "" if skip_bg else self._read(lambda wda: wda.pasteboard(content_type=content_type))
                    swapped = False
                    restored_app = None
                    restore_warning = False
                    if not text and foreground_runner and runner_id:
                        wda = self.connection.require_active()
                        read = wda.read_pasteboard(
                            content_type=content_type,
                            runner_bundle_id=runner_id,
                            return_app=return_app,
                            fallback_app=self.app_hint,
                            foreground_runner=True,
                            skip_initial_read=True,
                            send=send,
                        )
                        text = read["text"]
                        swapped = bool(read["swapped_app"])
                        restored_app = read["restored_app"]
                        restore_warning = bool(read["restore_warning"])
                        if swapped and text:
                            self._pasteboard_needs_swap = True
                        if restored_app:
                            self._set_app_hint(restored_app)
                    redacted = self._redact(text)
                    clipped, truncated = _truncate_json_text(redacted, max_bytes=32_768)
                    completed = True
                    result: dict[str, object] = {
                        "status": "checked",
                        "text": clipped,
                        "truncated": truncated,
                        "swapped_app": swapped,
                    }
                    if restored_app:
                        result["restored_app"] = restored_app
                    if restore_warning:
                        result["restore_warning"] = True
                    if mode:
                        result["observation"] = self._observe(mode, masks, limit=limit, offset=offset)
                    return result
                target = data.get("target")
                if target is not None and not isinstance(target, (str, dict)):
                    raise ValueError("Invalid target.")
                # Validate the whole request before any input is dispatched.
                points = self._points(data) if op == "swipe" or op == "tap" and target is None else []
                duration = number(data.get("duration", 0.1))
                if op == "tap" and target is not None and any(k in data for k in ("x", "y", "space")):
                    raise ValueError("Choose target or coordinates.")
                if op == "type":
                    if ("text" in data) == ("text_ref" in data):
                        raise ValueError("Choose text or text_ref.")
                    text = read_input(data["text_ref"]) if "text_ref" in data else data["text"]
                    text = validate_text(text)
                    self.secrets.add(text)
                    strategy, input_mode = data.get("strategy", "native"), data.get("mode", "insert")
                    if strategy not in ("native", "sequential") or input_mode not in ("insert", "replace"):
                        raise ValueError("Invalid input strategy or mode.")
                    frequency = data.get("frequency")
                    if frequency is not None and (type(frequency) is not int or frequency <= 0):
                        raise ValueError("Invalid frequency.")
                    verify = data.get("verify", False)
                    if type(verify) is not bool or verify and input_mode != "replace":
                        raise ValueError("Private verification requires explicit whole-field replacement.")
                if op == "set":
                    if ("value" in data) == ("value_ref" in data):
                        raise ValueError("Choose value or value_ref.")
                    value = read_input(data["value_ref"]) if "value_ref" in data else data["value"]
                    if isinstance(value, str):
                        value = validate_text(value, multiline=True, empty=True)
                        if value:
                            self.secrets.add(value)
                    elif type(value) is not bool:
                        raise ValueError("Input values are strings or Boolean states.")
                    kind = data.get("kind")
                    if kind not in (None, "date"):
                        raise ValueError("Only dates require an explicit kind.")
                    desired = iso_date(value) if kind == "date" else None
                    components = data.get("components")
                    if components is not None and (not isinstance(components, dict)
                            or set(components) != {"year", "month", "day"}
                            or any(not isinstance(t, (str, dict)) for t in components.values())):
                        raise ValueError("Provide year, month and day targets.")
                    month_values = data.get("month_values")
                    if month_values is not None and (not isinstance(month_values, list) or len(month_values) != 12
                            or any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in month_values)
                            or len({v.casefold() for v in month_values}) != 12):
                        raise ValueError("Provide twelve distinct native month values.")
                    strategy, verify, submit = data.get("strategy", "native"), data.get("verify", False), data.get("submit", False)
                    if strategy not in ("native", "sequential") or type(verify) is not bool or type(submit) is not bool:
                        raise ValueError("Invalid input options.")
                if op == "pick":
                    value = data["value"]
                    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                        raise ValueError("Invalid picker value.")
                    self.secrets.add(value)
                    order = data.get("order")
                    steps = data.get("max_steps", 10)
                    picker_offset = number(data.get("offset", 0.15))
                    seconds = number(data["seconds"]) if "seconds" in data else None
                    if (order not in (None, "next", "previous") or type(steps) is not int or steps < 1
                            or not 0 < picker_offset <= 0.5 or seconds is not None and seconds <= 0):
                        raise ValueError("Invalid picker bounds.")
                wait_seconds = number(data.get("wait_seconds", 0))
                if op in ("launch", "open_url", "press"):
                    key = {"launch": "bundle_id", "open_url": "url", "press": "button"}[op]
                    value = data.get(key)
                    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                        raise ValueError("Invalid operation argument.")
                    if op == "press" and value not in {"home", "volumeUp", "volumeDown", "siri", "back", "enter", "delete", "tab", "escape"}:
                        raise ValueError("Unsupported button.")
                element = self._target(target) if target is not None else None
                ref = element.ref if element else None
                wda = self.connection.require_active()
                if op == "set":
                    role = None
                    active_value: str | None = None
                    if kind == "date" or components is not None:
                        kind = "date"
                    else:
                        if ref is None:
                            ref, active_role, active_value = wda.active_element_info()
                            role = active_role or wda.element_type(ref)
                        else:
                            role = element.role if element and element.role else wda.element_type(ref)
                        kind = input_kind(role, value, kind)
                    if kind != "date" and (components is not None or month_values is not None):
                        raise ValueError("Date options require a date picker.")
                    if submit and kind != "text":
                        raise ValueError("Submit requires a text field.")
                    def perform_set() -> str:
                        if kind == "date":
                            if ref is None and components is None:
                                raise ValueError("Identify the date picker or its components.")
                            desired_date = desired or iso_date(value)
                            wheels = date_components(wda, ref, components, self._target, month_values)
                            return set_date(wda, ref, wheels, desired_date, send, month_values, self.secrets, verify=verify)
                        if kind == "text":
                            if "\n" in value and role != "XCUIElementTypeTextView":
                                raise InputUnavailable("multiline_requires_text_view")
                            current_text = element.value if element is not None else active_value
                            write_text(wda, ref, value, send, replace=True, strategy=strategy,
                                       current_value=current_text, role=role)
                            if submit:
                                send(lambda: wda.type_text("\ue007"))
                            return self._compare(wda, ref, value, role=role) if verify else "unknown"
                        if kind == "picker" and (not value or "\n" in value):
                            raise ValueError("Picker options must be non-empty single-line strings.")
                        current = element.value if element else active_value
                        if kind == "checked":
                            if current is None:
                                current = wda.element_value(ref, allow_null_empty=False)
                            return set_checked(wda, ref, value, send, current=current, verify=verify)
                        return pick_value(wda, ref, value, send, current=current, verify=verify)
                    return input_reply(perform_set)
                if op == "pick":
                    def perform_pick() -> str:
                        with self._within(wda, seconds) if seconds is not None else nullcontext():
                            if element.role != "XCUIElementTypePickerWheel":
                                raise ValueError("Picker target must be a native wheel.")
                            return pick_value(wda, ref, value, send, order=order, max_steps=steps,
                                              offset=picker_offset, current=element.value)
                    return input_reply(perform_pick)
                if op == "tap":
                    send(lambda: wda.element_action(ref, "click") if ref else wda.tap(*points))
                elif op == "swipe":
                    send(lambda: wda.drag(*points, duration=duration))
                elif op == "type":
                    write_text(wda, ref, text, send, replace=input_mode == "replace",
                               strategy=strategy, frequency=frequency)
                elif op == "press":
                    if value in {"enter", "delete", "tab", "escape"}:
                        send(lambda: wda.type_text({"enter": "\ue007", "delete": "\ue003", "tab": "\ue004", "escape": "\ue00c"}[value]))
                    elif value == "back":
                        send(wda.back)
                    else:
                        send(lambda: wda.press_button(value, duration=data.get("duration")))
                elif op == "launch":
                    send(lambda: wda.activate_app(value))
                    self._set_app_hint(value)  # the next read expects this app (and its depth)
                elif op == "open_url":
                    send(lambda: wda.open_url(value))
                completed = True
                result = {"status": "action", "dispatch": "acknowledged", "acknowledged_substeps": acknowledged}
                if op == "type" and verify:
                    result["verification"] = self._compare(wda, ref, text, role=element.role if element else None)
                if op == "launch" and wait_seconds:
                    result["readiness"] = self._ready(wda, value, wait_seconds)
                if mode:
                    result["observation"] = self._observe(mode, masks, limit=limit, offset=offset)
                return result
        except TargetUnavailable as exc:
            view = exc.candidates
            return {"status": "error", "dispatch": "partial" if acknowledged else "not_sent", "reason": "target_missing_or_ambiguous",
                    "acknowledged_substeps": acknowledged,
                    "candidates": view["elements"] if isinstance(view, dict) else view,
                    "candidate_page": {key: view[key] for key in ("snapshot_id", "next_offset", "offset", "omitted_elements")} if isinstance(view, dict) else None}
        except InputUnavailable as exc:
            return {"status": "error", "dispatch": "partial" if acknowledged else "not_sent",
                    "acknowledged_substeps": acknowledged, "effect": "unknown", "reason": exc.reason}
        except WDAOutcomeUnknown as exc:
            self.connection.invalidate()
            return {"status": "error", "dispatch": "unknown" if action_started else "not_sent", "reason": "inspect_before_retry" if action_started else "connection_unavailable",
                    "error": diagnostic(exc), "acknowledged_substeps": acknowledged,
                    "acknowledged_characters": getattr(exc, "acknowledged_characters", 0),
                    **({"effect": "unknown"} if op in ("pick", "set") else {})}
        except KeyboardInterrupt:
            self.connection.invalidate()
            self.closed = True
            return {"status": "error", "dispatch": "acknowledged" if completed else "unknown" if action_started else "not_sent",
                    "reason": "interrupted", "acknowledged_substeps": acknowledged}
        except (OpenClawIPhoneError, OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
            if completed:
                return {"status": "action", "dispatch": "acknowledged", "effect": "unknown",
                        "error": diagnostic(exc), "acknowledged_substeps": acknowledged}
            return {"status": "error", "dispatch": "partial" if acknowledged else "not_sent",
                    "reason": "invalid_request" if isinstance(exc, (ValueError, TypeError, OverflowError)) else "operation_unavailable",
                    "error": diagnostic(exc), "acknowledged_substeps": acknowledged,
                    **({"effect": "unknown"} if op in ("pick", "set") else {})}

    def _sequence(self, steps: object, *, mode: str | None, masks: list, limit: int) -> dict[str, object]:
        """Run 1..10 ordinary ops in order, stopping immediately on the first error or uncertain dispatch."""
        if not isinstance(steps, list) or not 1 <= len(steps) <= SEQUENCE_MAX_STEPS:
            raise ValueError(f"Sequence requires 1-{SEQUENCE_MAX_STEPS} steps.")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("Each sequence step must be a request object.")
            step_op = step.get("op")
            if not isinstance(step_op, str) or step_op not in OP_FIELDS or step_op in SEQUENCE_DISALLOWED_OPS:
                raise ValueError("Unsupported operation in sequence.")
            if "observe" in step or "offset" in step:
                raise ValueError("Attach observe to the sequence itself, not individual steps.")
            if set(step) - (OP_FIELDS[step_op] | {"op", "masks", "limit"}):
                raise ValueError("Unknown field in sequence step.")
            if OP_REQUIRED.get(step_op, set()) - set(step) or (
                step_op == "tap" and step.get("target") is None and not {"x", "y"} <= set(step)
            ):
                raise ValueError("Missing argument in sequence step.")
        results: list[dict[str, object]] = []
        acknowledged = 0
        any_action = False
        failed: dict[str, object] | None = None
        for step in steps:
            try:
                reply = self._request(step)
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
                reply = {"status": "error", "dispatch": "not_sent", "reason": "invalid_request", "error": diagnostic(exc)}
            results.append(reply)
            acknowledged += int(reply.get("acknowledged_substeps") or 0)
            if reply.get("status") == "action" or reply.get("swapped_app"):
                any_action = True
            step_dispatch = reply.get("dispatch")
            if reply.get("status") == "error" or step_dispatch in ("unknown", "partial") or self.closed:
                failed = reply
                break
        if failed is not None:
            fail_dispatch = failed.get("dispatch")
            dispatch = "unknown" if fail_dispatch == "unknown" else ("partial" if acknowledged else "not_sent")
            out: dict[str, object] = {
                "status": "error",
                "dispatch": dispatch,
                "reason": failed.get("reason", "sequence_step_failed"),
                "acknowledged_substeps": acknowledged,
                "completed_steps": len(results) - 1,
                "total_steps": len(steps),
                "steps": results,
            }
            if "error" in failed:
                out["error"] = failed["error"]
            return out
        out = {
            "status": "action" if any_action else "checked",
            "dispatch": "acknowledged" if acknowledged else "not_sent",
            "acknowledged_substeps": acknowledged,
            "completed_steps": len(results),
            "total_steps": len(steps),
            "steps": results,
        }
        if mode:
            try:
                with self.connection.operation():
                    out["observation"] = self._observe(mode, masks, limit=limit, offset=0)
            except (OpenClawIPhoneError, OSError, ValueError) as exc:
                out["error"] = diagnostic(exc)
        return out
