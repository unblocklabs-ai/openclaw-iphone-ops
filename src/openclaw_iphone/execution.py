"""Shared task bounds and content-free measurements; no device or model policy."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import time
from typing import Iterator

from .errors import OpenClawIPhoneError


class TaskStopped(OpenClawIPhoneError):
    """The operation deadline expired; no further operation may start."""


@dataclass
class Budget:
    deadline: float

    @classmethod
    def seconds(cls, seconds: float) -> Budget:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Task timeout must be finite and positive.")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TaskStopped("Task deadline expired; no further operation dispatched.")
        return remaining

    def sleep(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Wait must be finite and non-negative.")
        time.sleep(min(seconds, self.remaining()))
        self.remaining()


@dataclass
class Metrics:
    counts: Counter[str] = field(default_factory=Counter)
    seconds: Counter[str] = field(default_factory=Counter)

    @contextmanager
    def measure(self, operation: str) -> Iterator[None]:
        """Callers supply constant names/route templates, never user content."""
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            self.counts[operation] += 1
            self.seconds[operation] += elapsed

    def summary(self) -> dict[str, object]:
        return {"counts": dict(self.counts), "seconds": {key: round(value, 6) for key, value in self.seconds.items()}}
