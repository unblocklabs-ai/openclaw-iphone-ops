"""Direct controls. The caller decides what to do and whether it worked."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path
import stat
import struct
import time
from typing import Callable, TypeVar

from .connection import Connection
from .errors import OpenClawIPhoneError, WDAOutcomeUnknown, WDAUnavailable
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
    def __init__(self, candidates: list[dict[str, object]]) -> None:
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

    def _read(self, read: Callable[[WDAClient], T]) -> T:
        """One safe retry per read; no lifetime recovery allowance."""
        for attempt in range(2):
            try:
                return read(self.connection.require_active())
            except WDAUnavailable:
                self.connection.invalidate()
                if attempt:
                    raise
        raise AssertionError("Unreachable read retry state.")

    def _accessibility(self, *, limit: int = 80, offset: int = 0) -> dict[str, object]:
        if offset:
            if self.snapshot is None:
                raise ValueError("Observe a screen before paging it.")
            return self.snapshot.compact(include_labels=True, limit=limit, offset=offset, redact=self._redact)
        def capture(wda: WDAClient) -> Observation:
            started = time.monotonic()
            app = wda.active_app()
            xml = wda.source(compact=True)
            return parse_observation(xml, generation=0, device_udid=self.connection.device.udid,
                                     app=app["bundleId"], captured_at=datetime.now(timezone.utc).isoformat(),
                                     started=started, finished=time.monotonic(), process_id=app.get("pid"))
        self.snapshot = None
        self.snapshot = self._read(capture)
        return self.snapshot.compact(include_labels=True, limit=limit, offset=offset, redact=self._redact)

    def _image(self, masks: list) -> dict[str, object]:
        def capture(wda: WDAClient):
            size = wda.window_size()
            raw = wda.screenshot()
            if masks:
                raw, pixels = redact_png(raw, size, masks)
            else:
                if len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise WDAUnavailable("Invalid PNG.")
                pixels = struct.unpack_from(">II", raw, 16)
                if not 0 < pixels[0] * pixels[1] <= 12_000_000 or abs(pixels[0] / pixels[1] - size[0] / size[1]) > 0.02:
                    raise WDAUnavailable("Invalid image geometry.")
            return size, pixels, raw
        size, pixels, raw = self._read(capture)
        path = artifact_path("screen", ".png", base=self.evidence_base)
        write_private(path, raw)
        self.image_geometry = size, pixels
        return {"path": str(path), "pixels": list(pixels), "device_size": list(size),
                "masked_regions": len(masks), "privacy": "potentially_private"}

    def _observe(self, mode: str, masks: list, *, limit: int, offset: int) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, capture in (("accessibility", lambda: self._accessibility(limit=limit, offset=offset)), ("image", lambda: self._image(masks))):
            if mode not in (name, "both"):
                continue
            try:
                result[name] = capture()
            except (OpenClawIPhoneError, OSError):
                result[name + "_error"] = "unavailable"
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
            using, query = element.locator()
        else:
            selector = Selector(**target)
            using, query = selector.locator()
        refs = self._read(lambda w: w.find_elements(query, using=using))
        if len(refs) != 1:
            view = self._accessibility()
            candidates = view["elements"]
            if selector:
                ids = {e.id for e in self.snapshot.matches(selector)}
                by_id = {e["id"]: e for e in candidates}
                for candidate in candidates:
                    if candidate["id"] not in ids:
                        continue
                    parent = candidate.get("parent")
                    while parent in by_id:
                        ids.add(parent)
                        parent = by_id[parent].get("parent")
                candidates = [e for e in candidates if e["id"] in ids]
            raise TargetUnavailable(candidates)
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

    def request(self, data: object) -> dict[str, object]:
        if self.closed or not isinstance(data, dict):
            raise ValueError("Expected an open session and request object.")
        op = data.get("op")
        fields = {"observe": {"mode", "masks"}, "tap": {"target", "x", "y", "space"},
                  "swipe": {"from_x", "from_y", "to_x", "to_y", "duration", "space"},
                  "type": {"text", "text_ref", "target", "mode", "strategy", "frequency"},
                  "press": {"button", "duration"}, "launch": {"bundle_id"},
                  "open_url": {"url"}, "close": set()}
        if not isinstance(op, str) or op not in fields or set(data) - (fields[op] | {"op", "observe", "masks", "limit", "offset"}):
            raise ValueError("Unknown operation or field.")
        required = {"swipe": {"from_x", "from_y", "to_x", "to_y"}, "press": {"button"},
                    "launch": {"bundle_id"}, "open_url": {"url"}}
        if required.get(op, set()) - set(data) or op == "tap" and data.get("target") is None and not {"x", "y"} <= set(data):
            raise ValueError("Missing operation argument.")
        default_mode = "both" if self.allow_images and not data.get("offset", 0) else "accessibility"
        mode = data.get("mode", default_mode) if op == "observe" else data.get("observe")
        if mode is not None and mode not in ("image", "accessibility", "both"):
            raise ValueError("Unknown observation mode.")
        masks = data.get("masks", [])
        if not isinstance(masks, list) or len(masks) > 100 or any(not isinstance(r, list) or len(r) != 4 for r in masks):
            raise ValueError("Invalid masks.")
        masks = [[number(v) for v in region] for region in masks]
        limit, offset = data.get("limit", 80), data.get("offset", 0)
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
        try:
            with self.connection.operation():
                if op == "observe":
                    return {"status": "observed", "observation": self._observe(mode, masks, limit=limit, offset=offset)}
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
                if op in ("launch", "open_url", "press"):
                    key = {"launch": "bundle_id", "open_url": "url", "press": "button"}[op]
                    value = data.get(key)
                    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
                        raise ValueError("Invalid operation argument.")
                    if op == "press" and value not in {"home", "volumeUp", "volumeDown", "siri", "back", "enter", "delete", "tab", "escape"}:
                        raise ValueError("Unsupported button.")
                ref = self._target(target) if target is not None else None
                wda = self.connection.require_active()
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
                result = {"status": "action", "dispatch": "acknowledged", "acknowledged_substeps": acknowledged}
                if mode:
                    result["observation"] = self._observe(mode, masks, limit=limit, offset=offset)
                return result
        except TargetUnavailable as exc:
            return {"status": "error", "dispatch": "not_sent", "reason": "target_missing_or_ambiguous", "candidates": exc.candidates}
        except WDAOutcomeUnknown as exc:
            self.connection.invalidate()
            return {"status": "error", "dispatch": "unknown" if action_started else "not_sent", "reason": "inspect_before_retry" if action_started else "connection_unavailable",
                    "acknowledged_substeps": acknowledged, "acknowledged_characters": getattr(exc, "acknowledged_characters", 0)}
        except KeyboardInterrupt:
            self.connection.invalidate()
            self.closed = True
            return {"status": "error", "dispatch": "unknown" if action_started else "not_sent",
                    "reason": "interrupted", "acknowledged_substeps": acknowledged}
        except (OpenClawIPhoneError, OSError):
            return {"status": "error", "dispatch": "partial" if acknowledged else "not_sent",
                    "reason": "operation_unavailable", "acknowledged_substeps": acknowledged}
