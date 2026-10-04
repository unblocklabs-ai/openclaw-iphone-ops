"""Bounded JSON-lines framing. Idle caller deliberation has no deadline."""
from __future__ import annotations

import io
from datetime import datetime, timezone
import json
import os
import select
import time
from typing import Callable, Iterator

from .errors import SessionOutputUnavailable
from .execution import Budget, TaskStopped


class FramingError(ValueError):
    """Incomplete frame, distinct from a malformed complete request."""


def strict_json(raw: bytes | str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Non-finite JSON number.")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def read_requests(fd: int, *, frame_timeout: float = 5) -> Iterator[bytes]:
    pending = b""
    started = None
    oversized = False
    while True:
        if started is not None and time.monotonic() - started >= frame_timeout:
            raise FramingError("Incomplete request frame timed out.")
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            if pending or oversized:
                raise FramingError("Request needs a terminating newline.")
            return
        for byte in chunk:
            if started is None:
                started = time.monotonic()
            if byte == 10:
                yield b"" if oversized else pending
                pending, oversized, started = b"", False, None
            elif not oversized:
                pending += bytes([byte])
                if len(pending) > 4096:
                    pending, oversized = b"", True


class JsonLineEmitter:
    def __init__(self, fd: int, *, seconds: float = 5, max_bytes: int = 65_536) -> None:
        self.fd, self.seconds, self.max_bytes = fd, seconds, max_bytes

    def __call__(self, value: dict[str, object]) -> None:
        raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        oversized = len(raw) > self.max_bytes
        if oversized:
            summary = {key: value[key] for key in ("status", "dispatch", "reason", "acknowledged_substeps",
                       "acknowledged_characters", "cleanup", "request_sequence") if key in value}
            summary["output_warning"] = "response_too_large_reduce_observation_limit"
            observation = value.get("observation")
            if isinstance(observation, dict) and "image" in observation:
                preserved = dict(summary, observation={"image": observation["image"], "accessibility_error": "response_too_large"})
                if len((json.dumps(preserved) + "\n").encode()) <= self.max_bytes:
                    summary = preserved
            raw = (json.dumps(summary) + "\n").encode()
        budget = Budget.seconds(self.seconds)
        try:
            was_blocking = os.get_blocking(self.fd)
            os.set_blocking(self.fd, False)
        except (OSError, ValueError) as exc:
            raise SessionOutputUnavailable("Output unavailable.") from exc
        offset = 0
        try:
            while offset < len(raw):
                _, ready, _ = select.select([], [self.fd], [], min(0.2, budget.remaining()))
                if not ready:
                    continue
                try:
                    written = os.write(self.fd, raw[offset:offset + 4096])
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise SessionOutputUnavailable("Output made no progress.")
                offset += written
        except (OSError, ValueError, TaskStopped) as exc:
            raise SessionOutputUnavailable("Output unavailable or stalled.") from exc
        finally:
            try:
                os.set_blocking(self.fd, was_blocking)
            except (OSError, ValueError):
                pass


def json_line_emitter(stream) -> Callable[[dict[str, object]], None]:
    try:
        return JsonLineEmitter(stream.fileno())
    except (AttributeError, io.UnsupportedOperation):
        def emit(value: dict[str, object]) -> None:
            stream.write(json.dumps(value, ensure_ascii=True) + "\n")
            stream.flush()
        return emit


def serve(session, requests: Iterator[bytes], emit: Callable[[dict[str, object]], None]) -> int:
    iterator = iter(requests)
    sequence = 0
    while True:
        try:
            line = next(iterator)
        except StopIteration:
            break
        except FramingError:
            emit({"status": "error", "dispatch": "not_sent", "reason": "framing_error",
                  "error": {"category": "incomplete_frame", "phase": "framing"}})
            return 1
        sequence += 1
        started, stamp = time.monotonic(), datetime.now(timezone.utc).isoformat()
        try:
            result = session.request(strict_json(line))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            result = {"status": "error", "dispatch": "not_sent", "reason": "invalid_request"}
        result["request_sequence"] = sequence
        result.setdefault("timing", {"started_at": stamp, "finished_at": datetime.now(timezone.utc).isoformat(),
                                    "seconds": round(time.monotonic() - started, 6), "reconnects": 0,
                                    "counts": {}, "seconds_by_route": {}})
        emit(result)
        if session.closed:
            return 1 if result["status"] == "error" else 0
    emit({"status": "closed", "reason": "input_ended"})
    return 0
