"""Bounded JSON-lines planner interface over one existing Executor, not a daemon."""
from __future__ import annotations

import os
import select
import io
import json
import time
from typing import Callable, Iterator

from .actions import Executor
from .errors import OpenClawIPhoneError, SessionOutputUnavailable, VerificationExpired
from .execution import Budget, TaskStopped
from .jev import strict_json
from .observations import Observation, ObservationRejected
from .tasks import TaskSpec, object_fields


class PlannerSession:
    def __init__(self, executor: Executor, spec: TaskSpec, *, include_labels: bool = False,
                 driver=None, evidence_base: str | None = None) -> None:
        self.executor, self.spec = executor, spec
        self.include_labels = include_labels
        self.requests = 0
        self.steps = 0
        self.closed = False
        self.adaptive = None
        if spec.adaptive is not None:
            from .adaptive import AdaptiveAct
            self.adaptive = AdaptiveAct(executor, spec.adaptive, driver=driver,
                                        max_decisions=spec.limits.max_decisions, evidence_base=evidence_base)

    def view(self, observation: Observation) -> dict[str, object]:
        offers = self.executor.offers(observation) if self.spec.grants else ()
        view = (self.adaptive.view(observation, labels=self.include_labels) if self.adaptive
                else observation.compact(include_labels=self.include_labels))
        view["actions"] = [{"id": o.id, "snapshot_id": o.snapshot_id, "target_id": o.target_id,
                            "operation": self.spec.grants[o.grant_index].operation,
                            "description": self.spec.grants[o.grant_index].description} for o in offers]
        view["action_blockers"] = list(self.executor.offer_blockers)
        if self.adaptive:
            view["fixed_grant_blockers"] = view.pop("action_blockers")
            view["adaptive_operations"] = [] if self.executor.stopped else list(self.adaptive.scope["operations"])
        view["input_stopped"] = self.executor.stopped
        return view

    def request(self, data: object) -> dict[str, object]:
        """Fixed grants by default; adaptive intent requires explicit task opt-in."""
        if self.closed:
            raise TaskStopped("Planner session is closed.")
        self.executor.connection.budget.remaining()
        self.requests += 1
        if self.requests > 4 * self.spec.limits.max_steps + 8:
            raise TaskStopped("Planner request limit reached.")
        if isinstance(data, dict) and (data.get("op") in ("act", "screenshot", "vision_tap")
                                      or data.get("op") == "reconcile" and self.adaptive is not None):
            if self.adaptive is None:
                raise ValueError("Adaptive requests require task opt-in.")
            if data["op"] in ("act", "vision_tap"):
                if self.steps >= self.spec.limits.max_steps:
                    raise TaskStopped("Planner action limit reached.")
                self.steps += 1
            if data["op"] == "act":
                result = self.adaptive.act(data)
            elif data["op"] == "vision_tap":
                try:
                    result = self.adaptive.vision_tap(data)
                except ObservationRejected as exc:
                    # This operation raises observation rejection only before
                    # its single tap; shared guards elsewhere may follow writes.
                    result = {"status": "blocked", "dispatch": "not_sent", "reason": exc.code}
            elif data["op"] == "screenshot":
                object_fields(data, {"op", "redact"}, {"op"})
                result = self.adaptive.screenshot(redact=data.get("redact"))
            else:
                object_fields(data, {"op"}, {"op"})
                result = self.adaptive.reconcile()
            if isinstance(result.get("observation"), Observation):
                # Do not re-enumerate fixed grants after adaptive dispatch.
                result["observation"] = self.adaptive.view(result["observation"], labels=self.include_labels)
            return result
        fields = object_fields(data, {"op", "id"}, {"op"})
        operation = fields["op"]
        if operation not in {"observe", "execute", "wait", "done", "reconcile", "recover_read", "close"}:
            raise ValueError("Unknown planner operation.")
        if (operation == "execute") != ("id" in fields):
            raise ValueError("Only execute requires an offered action ID.")
        if operation == "close":
            self.closed = True
            return {"status": "closed", "verification": "unknown"}
        if operation == "recover_read":
            # The connection allows one same-UDID read recovery. A stopped
            # executor stays stopped, even if reads become available again.
            self.executor.connection.recover_read()
            return {"status": "recovered", "observation": self.view(self.executor.observe(app_only=True))}
        if operation == "reconcile":
            state, observation = self.executor.wait(self.executor.pending or (), once=True)
            return {"status": "observed", "verification": state, "observation": self.view(observation)}
        if operation in {"wait", "done"}:
            if not self.spec.success:
                if operation == "done":
                    self.closed = True
                    return {"status": "closed", "verification": "unknown", "reason": "caller_finished"}
                return {"status": "blocked", "verification": "unknown",
                        "reason": "success_conditions_unconfigured"}
            if operation == "wait":
                state, observation = self.executor.wait(self.spec.success)
            else:
                observation = self.executor.latest
                if observation is None or time.monotonic() - observation.started > self.executor.freshness:
                    state, observation = self.executor.wait(self.spec.success, once=True)
                else:
                    self.executor._check_snapshot(observation)
                    state = self.executor.verify(observation, self.spec.success)
                    if state == "unknown" and observation.elements is None:
                        state, observation = self.executor.wait(self.spec.success, once=True)
            if state == "satisfied":
                self.closed = True
            return {"status": "completed" if state == "satisfied" else "incomplete", "verification": state,
                    "observation": (self.adaptive.view(observation, labels=self.include_labels) if self.adaptive
                                    else observation.compact(include_labels=self.include_labels))}
        if operation == "observe":
            return {"status": "observed", "observation": self.view(self.executor.observe())}
        action_id = fields["id"]
        if self.adaptive and self.adaptive.pending_input is not None:
            return {"status": "blocked", "dispatch": "not_sent", "reason": "input_requires_reconciliation"}
        if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
            raise ValueError("Invalid offered action ID.")
        if self.steps >= self.spec.limits.max_steps:
            raise TaskStopped("Planner action limit reached.")
        self.steps += 1
        result = self.executor.execute(action_id)
        next_view = None
        if result.observation is not None:
            try:
                next_view = self.view(result.observation)
            except OpenClawIPhoneError:
                # Enumerating the next choices must not erase an acknowledged
                # action if freshness, a predicate read or the budget fails.
                self.executor.stopped = True
                next_view = result.observation.compact(include_labels=self.include_labels)
                next_view.update(actions=[], input_stopped=True,
                                 action_blockers=[{"scope": "all", "reason": "next_choices_unavailable"}])
        return {"status": "step", "dispatch": result.dispatch, "verification": result.verification,
                "reason": result.reason, "acknowledged_substeps": result.acknowledged_substeps,
                "error_type": result.error_type, "input_stopped": self.executor.stopped,
                "observation": next_view}


def read_requests(fd: int, budget: Budget) -> Iterator[bytes]:
    """Read pipe/PTY input without letting an idle or partial line outlive a task.

    os.read avoids TextIO prefetch hiding buffered lines from select. No reader
    thread survives session exit. The protocol bounds each request to 4 KiB.
    """
    pending = b""
    while True:
        budget.remaining()
        if b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line
            continue
        if len(pending) > 4096:
            raise ValueError("Planner request exceeds 4 KiB.")
        ready, _, _ = select.select([fd], [], [], min(0.2, budget.remaining()))
        if not ready:
            continue
        chunk = os.read(fd, 4096 - len(pending) + 1)
        if not chunk:
            if pending:
                raise ValueError("Planner request needs a terminating newline.")
            return
        pending += chunk


def serve(session: PlannerSession, requests: Iterator[bytes], emit: Callable[[dict[str, object]], None]) -> int:
    """Emit safe errors only; never echo requests, UI values or exception bodies."""
    sequence = 0
    for line in requests:
        sequence += 1
        started = time.monotonic()
        events = session.executor.connection.metrics.events
        event_start = len(events)
        invalid = False
        stopped = False
        try:
            result = session.request(strict_json(line))
        except TaskStopped:
            result = {"status": "blocked", "reason": "deadline_or_limit"}
            stopped = True
        except (ValueError, TypeError, UnicodeError, RecursionError):
            # Malformed input ends ownership; no ambiguous framing/retry loop.
            result = {"status": "blocked", "reason": "invalid_request"}
            invalid = True
        except ObservationRejected as exc:
            result = {"status": "blocked", "reason": exc.code}
        except VerificationExpired:
            result = {"status": "incomplete", "verification": "unknown", "reason": "verification_expired"}
        except OSError:
            result = {"status": "blocked", "reason": "local_input_or_evidence_unavailable"}
        except OpenClawIPhoneError:
            result = {"status": "blocked", "reason": "read_or_recovery_unavailable"}
        finished = time.monotonic()
        result["request_sequence"] = sequence
        result["timing"] = {
            "handling_started_monotonic": started,
            "handling_finished_monotonic": finished,
            "handling_seconds": round(finished - started, 6),
            "transport_event_start": event_start,
            "transport_event_end": len(events),
        }
        emit(result)
        if invalid or stopped:
            return 1
        if session.closed:
            return 0 if result["status"] == "completed" else 1
    emit({"status": "closed", "reason": "input_ended", "verification": "unknown"})
    return 1


class JsonLineEmitter:
    """Deadline-aware, bounded stdout writer for a long-lived planner process."""

    def __init__(self, fd: int, budget: Budget, *, max_bytes: int = 65_536) -> None:
        self.fd, self.budget, self.max_bytes = fd, budget, max_bytes

    def __call__(self, value: dict[str, object]) -> None:
        raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        oversized = len(raw) > self.max_bytes
        if oversized:
            # Do not hide a completed/partial mutation behind a generic error.
            summary = {key: value[key] for key in ("status", "dispatch", "verification", "reason",
                       "acknowledged_substeps", "input_stopped", "cleanup", "request_sequence", "timing")
                       if key in value}
            summary["output_warning"] = "response_too_large_session_closed"
            raw = (json.dumps(summary, separators=(",", ":")) + "\n").encode()
        try:
            was_blocking = os.get_blocking(self.fd)
            os.set_blocking(self.fd, False)
        except (OSError, ValueError) as exc:
            raise SessionOutputUnavailable("Planner output is unavailable; task ownership will be released.") from exc
        offset = 0
        try:
            while offset < len(raw):
                try:
                    remaining = self.budget.remaining()
                except TaskStopped as exc:
                    raise SessionOutputUnavailable(
                        "Planner output deadline expired; task ownership will be released."
                    ) from exc
                try:
                    _, ready, _ = select.select([], [self.fd], [], min(0.2, remaining))
                except (OSError, ValueError) as exc:
                    raise SessionOutputUnavailable("Planner output is unavailable.") from exc
                if not ready:
                    continue
                try:
                    written = os.write(self.fd, raw[offset:offset + 4096])
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError) as exc:
                    raise SessionOutputUnavailable("Planner output is unavailable; task ownership will be released.") from exc
                if written <= 0:
                    raise SessionOutputUnavailable("Planner output made no progress; task ownership will be released.")
                offset += written
            if oversized:
                raise SessionOutputUnavailable("Planner response exceeded its bound; session closed.")
        finally:
            try:
                os.set_blocking(self.fd, was_blocking)
            except (OSError, ValueError):
                pass


def json_line_emitter(stream, budget: Budget) -> Callable[[dict[str, object]], None]:
    try:
        fd = stream.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        # Test/capture streams do not expose a file descriptor. Real CLI pipes
        # use JsonLineEmitter, so a full pipe cannot hold device ownership.
        def emit(value: dict[str, object]) -> None:
            stream.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
            stream.flush()
        return emit
    return JsonLineEmitter(fd, budget)
