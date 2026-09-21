"""Shared task bounds and content-free measurements; no device or model policy."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Iterator

from .errors import OpenClawIPhoneError


class TaskStopped(OpenClawIPhoneError):
    """No further operation may start: the task was cancelled or expired."""


@dataclass
class Budget:
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def seconds(cls, seconds: float) -> Budget:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Task timeout must be finite and positive.")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        if self.cancelled.is_set():
            raise TaskStopped("Task cancelled; no further operation dispatched.")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TaskStopped("Task deadline expired; no further operation dispatched.")
        return remaining

    def sleep(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Wait must be finite and non-negative.")
        self.cancelled.wait(min(seconds, self.remaining()))
        self.remaining()


@dataclass
class Metrics:
    counts: Counter[str] = field(default_factory=Counter)
    seconds: Counter[str] = field(default_factory=Counter)
    events: list[dict[str, object]] = field(default_factory=list)

    @contextmanager
    def measure(self, operation: str) -> Iterator[None]:
        """Callers supply constant names/route templates, never user content."""
        started = time.monotonic()
        outcome = "returned"
        try:
            yield
        except BaseException:
            outcome = "raised"
            raise
        finally:
            elapsed = time.monotonic() - started
            self.counts[operation] += 1
            self.seconds[operation] += elapsed
            if len(self.events) < 2000:
                self.events.append({"operation": operation, "seconds": round(elapsed, 6), "outcome": outcome})

    def summary(self) -> dict[str, object]:
        return {"counts": dict(self.counts), "seconds": dict(self.seconds), "events": list(self.events),
                "events_truncated": sum(self.counts.values()) > len(self.events)}
