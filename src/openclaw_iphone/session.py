"""Direct controls. The caller decides what to do and whether it worked."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import math
import os
from pathlib import Path
import stat
import struct
import time
from typing import Callable, TypeVar

from .connection import Connection
from .errors import OpenClawIPhoneError, WDAOutcomeUnknown, WDAUnavailable, WDATransportUnavailable, diagnostic
from .execution import Budget
from .evidence import artifact_path, write_private
from .image_evidence import redact_png
from .observations import Observation, Selector, parse_observation
from .wda import WDAClient

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


class TargetUnavailable(OpenClawIPhoneError):
    def __init__(self, candidates: dict[str, object] | list[dict[str, object]]) -> None:
        super().__init__("Target missing or ambiguous.")
        self.candidates = candidates


class Session:
    def __init__(self, connection: Connection, *, allow_images: bool = False,
                 evidence_base: str | None = None) -> None:
        self.connection, self.allow_images, self.evidence_base = connection, allow_images, evidence_base
        self.closed = False
        self.snapshot: Observation | None = None
        self.image_geometry: tuple[tuple[float, float], tuple[int, int]] | None = None
        self.secrets: set[str] = set()
        self.candidate_ids: set[str] | None = None

    def _read(self, read: Callable[[WDAClient], T]) -> T:
        """One safe retry per read; no lifetime recovery allowance."""
        for attempt in range(2):
            try:
                return read(self.connection.require_active())
            except WDATransportUnavailable:
                self.connection.invalidate()
                if attempt:
                    raise
        raise AssertionError("Unreachable read retry state.")

    def _accessibility(self, *, limit: int = 80, offset: int = 0, candidates: bool = False) -> dict[str, object]:
        if offset or candidates:
            if self.snapshot is None:
                raise ValueError("Observe a screen before paging it.")
            if candidates and self.candidate_ids is None:
                raise ValueError("No current ambiguity candidates.")
            return self.snapshot.compact(include_labels=True, limit=limit, offset=offset, redact=self._redact,
                                         ids=self.candidate_ids if candidates else None)
        def capture(wda: WDAClient) -> Observation:
            started = time.monotonic()
            app = wda.active_app()
            xml = wda.source(compact=True, validate=False)
            return parse_observation(xml, generation=0, device_udid=self.connection.device.udid,
                                     app=app["bundleId"], captured_at=datetime.now(timezone.utc).isoformat(),
                                     started=started, finished=time.monotonic(), process_id=app.get("pid"))
        self.snapshot = None
        self.candidate_ids = None
        self.snapshot = self._read(capture)
        return self.snapshot.compact(include_labels=True, limit=limit, offset=offset, redact=self._redact)

    def _image(self, masks: list) -> dict[str, object]:
        started = time.monotonic()
        def capture(wda: WDAClient):
            size = wda.window_size()
            raw = wda.screenshot()
            stamp, captured = datetime.now(timezone.utc).isoformat(), time.monotonic()
            if masks:
                raw, pixels = redact_png(raw, size, masks)
            else:
                if len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise WDAUnavailable("Invalid PNG.")
                pixels = struct.unpack_from(">II", raw, 16)
                if not 0 < pixels[0] * pixels[1] <= 12_000_000 or abs(pixels[0] / pixels[1] - size[0] / size[1]) > 0.02:
                    raise WDAUnavailable("Invalid image geometry.")
            return size, pixels, raw, stamp, captured
        size, pixels, raw, stamp, captured = self._read(capture)
        path = artifact_path("screen", ".png", base=self.evidence_base)
        write_private(path, raw)
        self.image_geometry = size, pixels
        return {"path": str(path), "pixels": list(pixels), "device_size": list(size),
                "masked_regions": len(masks), "privacy": "potentially_private",
                "captured_at": stamp, "capture_seconds": captured - started,
                "processing_seconds": time.monotonic() - captured}

    def _observe(self, mode: str, masks: list, *, limit: int, offset: int, candidates: bool = False) -> dict[str, object]:
        result: dict[str, object] = {}
        # A slow AX lane cannot starve a healthy image fallback. Captures are
        # independent and timestamped, not an atomic screen pair.
        for name, capture in (("image", lambda: self._image(masks)), ("accessibility", lambda: self._accessibility(limit=limit, offset=offset, candidates=candidates))):
            if mode not in (name, "both"):
                continue
            try:
                result[name] = capture()
            except (OpenClawIPhoneError, OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
                result[name + "_error"] = diagnostic(exc)
        return result

    def _target(self, target: str | dict) -> str:
        selector = None
        if isinstance(target, str):
            matches = [e for e in self.snapshot.elements or () if e.id == target] if self.snapshot else []
            if len(matches) != 1:
                raise TargetUnavailable([])
            element = matches[0]
            if element.visible is not True or element.enabled is not True:
                raise TargetUnavailable([])
            if self._read(lambda w: w.active_app())["bundleId"] != self.snapshot.app:
                raise TargetUnavailable([])
            using, query = self.snapshot.locator(element)
        else:
            selector = Selector(**target)
            using, query = selector.locator()
        refs = self._read(lambda w: w.find_elements(query, using=using))
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
            if self._read(lambda w: w.window_size()) != size:
                raise TargetUnavailable([])
            if any(v >= pixels[i % 2] for i, v in enumerate(points)):
                raise ValueError("Coordinates outside image.")
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

    def _compare(self, wda: WDAClient, ref: str | None, expected: str) -> str:
        try:
            with self._within(wda, min(2, self.connection.seconds)):
                ref = ref or wda.active_element()
                if wda.element_type(ref) not in {"XCUIElementTypeTextField", "XCUIElementTypeTextView", "XCUIElementTypeSearchField"}:
                    return "unknown"
                value = wda.element_value(ref, allow_null_empty=False)
                if value == wda.element_placeholder(ref):
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
        fields = {"observe": {"mode", "masks", "candidates"}, "tap": {"target", "x", "y", "space"},
                  "swipe": {"from_x", "from_y", "to_x", "to_y", "duration", "space"},
                  "type": {"text", "text_ref", "target", "mode", "strategy", "frequency", "verify"},
                  "pick": {"target", "value", "order", "max_steps", "offset", "seconds"},
                  "press": {"button", "duration"}, "launch": {"bundle_id", "wait_seconds"},
                  "open_url": {"url"}, "close": set()}
        if not isinstance(op, str) or op not in fields or set(data) - (fields[op] | {"op", "observe", "masks", "limit", "offset"}):
            raise ValueError("Unknown operation or field.")
        required = {"swipe": {"from_x", "from_y", "to_x", "to_y"}, "press": {"button"},
                    "launch": {"bundle_id"}, "open_url": {"url"}, "pick": {"target", "value"}}
        if required.get(op, set()) - set(data) or op == "tap" and data.get("target") is None and not {"x", "y"} <= set(data):
            raise ValueError("Missing operation argument.")
        candidates = data.get("candidates", False)
        if type(candidates) is not bool:
            raise ValueError("Invalid candidates flag.")
        default_mode = "both" if self.allow_images and not data.get("offset", 0) and not candidates else "accessibility"
        mode = data.get("mode", default_mode) if op == "observe" else data.get("observe")
        if mode is not None and mode not in ("image", "accessibility", "both"):
            raise ValueError("Unknown observation mode.")
        masks = data.get("masks", [])
        if not isinstance(masks, list) or len(masks) > 100 or any(not isinstance(r, list) or len(r) != 4 for r in masks):
            raise ValueError("Invalid masks.")
        masks = [[number(v) for v in region] for region in masks]
        limit, offset = data.get("limit", 80), data.get("offset", 0) if op != "pick" else 0
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 2000:
            raise ValueError("Invalid observation page.")
        if offset and op != "observe":
            raise ValueError("Only observations can page a captured screen.")
        if mode in ("image", "both") and not self.allow_images:
            raise ValueError("Image disclosure requires --allow-images.")
        if op == "close":
            self.closed = True
            return {"status": "closed"}
        acknowledged = 0
        action_started = False
        completed = False
        try:
            with self.connection.operation():
                if op == "observe":
                    return {"status": "observed", "observation": self._observe(mode, masks, limit=limit, offset=offset, candidates=candidates)}
                target = data.get("target")
                if target is not None and not isinstance(target, (str, dict)):
                    raise ValueError("Invalid target.")
                # Validate the whole request before any input is dispatched.
                points = self._points(data) if op == "swipe" or op == "tap" and target is None else []
                duration = number(data.get("duration", 0.1))
                if duration > 10:
                    raise ValueError("Gesture/button duration must be at most ten seconds.")
                if op == "tap" and target is not None and any(k in data for k in ("x", "y", "space")):
                    raise ValueError("Choose target or coordinates.")
                if op == "type":
                    if ("text" in data) == ("text_ref" in data):
                        raise ValueError("Choose text or text_ref.")
                    text = read_input(data["text_ref"]) if "text_ref" in data else data["text"]
                    if not isinstance(text, str) or not 1 <= len(text) <= 4096 or any(ord(c) < 32 or 0xE000 <= ord(c) <= 0xF8FF or ord(c) == 127 for c in text):
                        raise ValueError("Invalid text; use press for control keys.")
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
                if op == "pick":
                    value = data["value"]
                    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
                        raise ValueError("Invalid picker value.")
                    self.secrets.add(value)
                    order = data.get("order")
                    steps = data.get("max_steps", 10)
                    picker_offset = number(data.get("offset", 0.15))
                    seconds = number(data.get("seconds", 5))
                    if order not in (None, "next", "previous") or type(steps) is not int or not 1 <= steps <= 50 or not 0 < picker_offset <= 0.5 or not 0 < seconds <= 30:
                        raise ValueError("Invalid picker bounds.")
                wait_seconds = number(data.get("wait_seconds", 0))
                if wait_seconds > 30:
                    raise ValueError("Readiness wait must be at most thirty seconds.")
                if op in ("launch", "open_url", "press"):
                    key = {"launch": "bundle_id", "open_url": "url", "press": "button"}[op]
                    value = data.get(key)
                    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
                        raise ValueError("Invalid operation argument.")
                    if op == "press" and value not in {"home", "volumeUp", "volumeDown", "siri", "back", "enter", "delete", "tab", "escape"}:
                        raise ValueError("Unsupported button.")
                ref = self._target(target) if target is not None else None
                wda = self.connection.require_active()
                if op == "pick":
                    with self._within(wda, seconds), wda.input_transaction():
                        if wda.element_type(ref) != "XCUIElementTypePickerWheel":
                            raise ValueError("Picker target must be a native wheel.")
                        current = wda.element_value(ref, allow_null_empty=False)
                        readback_error = None
                        for step in range(steps if order else 1):
                            if current == value:
                                break
                            action_started = True
                            if order:
                                wda.picker_step(ref, order, offset=picker_offset)
                            else:
                                wda.element_action(ref, "value", text=value)
                            acknowledged += 1
                            try:
                                current = wda.element_value(ref, allow_null_empty=False)
                            except (OpenClawIPhoneError, OSError) as exc:
                                # Every adjustment so far was acknowledged.
                                # Unknown readback stops adjustment, not receipts.
                                readback_error = diagnostic(exc)
                                if isinstance(exc, WDATransportUnavailable):
                                    self.connection.invalidate()
                                break
                        completed = True
                        effect = "unknown" if readback_error else "match" if current == value else "mismatch"
                    result = {"status": "action" if acknowledged else "checked",
                              "dispatch": "acknowledged" if acknowledged else "not_sent",
                              "acknowledged_substeps": acknowledged, "effect": effect}
                    if readback_error:
                        result["error"] = readback_error
                    if mode:
                        result["observation"] = self._observe(mode, masks, limit=limit, offset=offset)
                    return result
                action_started = True
                if op == "tap":
                    wda.element_action(ref, "click") if ref else wda.tap(*points)
                elif op == "swipe":
                    wda.drag(*points, duration=duration)
                elif op == "type":
                    with wda.input_transaction():
                        if ref and strategy == "sequential":
                            wda.element_action(ref, "click")
                            acknowledged += 1
                        if input_mode == "replace":
                            wda.element_action(ref, "clear") if ref else wda.clear_text()
                            acknowledged += 1
                        if strategy == "sequential":
                            wda.type_text(text, frequency=frequency)
                        elif ref:
                            wda.element_action(ref, "value", text=text)
                        else:
                            wda.type_text_bulk(text, frequency=frequency)
                elif op == "press":
                    if value in {"enter", "delete", "tab", "escape"}:
                        wda.type_text({"enter": "\ue007", "delete": "\ue003", "tab": "\ue004", "escape": "\ue00c"}[value])
                    elif value == "back":
                        wda.back()
                    else:
                        wda.press_button(value, duration=data.get("duration"))
                elif op == "launch":
                    wda.activate_app(value)
                elif op == "open_url":
                    wda.open_url(value)
                acknowledged += 1
                completed = True
                result = {"status": "action", "dispatch": "acknowledged", "acknowledged_substeps": acknowledged}
                if op == "type" and verify:
                    result["verification"] = self._compare(wda, ref, text)
                if op == "launch" and wait_seconds:
                    result["readiness"] = self._ready(wda, value, wait_seconds)
                if mode:
                    result["observation"] = self._observe(mode, masks, limit=limit, offset=offset)
                return result
        except TargetUnavailable as exc:
            view = exc.candidates
            return {"status": "error", "dispatch": "not_sent", "reason": "target_missing_or_ambiguous",
                    "candidates": view["elements"] if isinstance(view, dict) else view,
                    "candidate_page": {key: view[key] for key in ("snapshot_id", "next_offset", "offset", "omitted_elements")} if isinstance(view, dict) else None}
        except WDAOutcomeUnknown as exc:
            self.connection.invalidate()
            return {"status": "error", "dispatch": "unknown" if action_started else "not_sent", "reason": "inspect_before_retry" if action_started else "connection_unavailable",
                    "error": diagnostic(exc), "acknowledged_substeps": acknowledged,
                    "acknowledged_characters": getattr(exc, "acknowledged_characters", 0),
                    **({"effect": "unknown"} if op == "pick" else {})}
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
                    **({"effect": "unknown"} if op == "pick" else {})}
