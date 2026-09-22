"""Strict task files and a bounded loop shared by deterministic and Jev callers."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import time

from .actions import Condition, Executor, Grant, Offer
from .errors import OpenClawIPhoneError, VerificationExpired
from .execution import TaskStopped
from .jev import DecisionUnavailable, JevDriver, LowConfidenceDecision, strict_json
from .observations import Observation, Selector


@dataclass(frozen=True)
class Limits:
    seconds: float = 60
    max_steps: int = 12
    max_decisions: int = 12
    max_no_progress: int = 3
    freshness: float = 30
    verification_seconds: float = 15


@dataclass(frozen=True)
class TaskSpec:
    objective: str = field(repr=False)
    grants: tuple[Grant, ...] = field(repr=False)
    success: tuple[Condition, ...] = field(repr=False)
    texts: dict[str, str] = field(default_factory=dict, repr=False)
    limits: Limits = field(default_factory=Limits)
    adaptive: dict | None = field(default=None, repr=False)


def object_fields(value: object, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed:
        raise ValueError("Task object has missing or unknown fields.")
    return value


def text(value: object, *, maximum: int = 256, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not value and not empty):
        raise ValueError("Task string is missing, invalid or too long.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Task strings must be valid Unicode.") from None
    return value


def selector(value: object) -> Selector:
    data = object_fields(value, {"role", "name", "label", "ancestor_label"}, {"role"})
    return Selector(**{key: text(item) for key, item in data.items()})


def conditions(value: object) -> tuple[Condition, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Conditions must be a list of at most 32 predicates.")
    result = []
    for item in value:
        data = object_fields(item, {"kind", "app", "target", "value"}, {"kind", "app"})
        result.append(Condition(text(data["kind"]), text(data["app"]),
            selector(data["target"]) if "target" in data else None,
            text(data["value"], maximum=4096, empty=True) if "value" in data else None))
    return tuple(result)


def parse_task(data: object) -> TaskSpec:
    data = object_fields(data, {"version", "objective", "grants", "success", "texts", "limits", "adaptive"},
                         {"version", "objective", "grants", "success"})
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Only task schema version 1 is supported.")
    raw_grants = data["grants"]
    if not isinstance(raw_grants, list) or len(raw_grants) > 252:
        raise ValueError("Task grants must be a list with at most 252 entries.")
    grants = []
    for item in raw_grants:
        grant = object_fields(item, {"operation", "app", "description", "target", "text_id", "direction", "destination", "after", "before", "max_uses"},
                              {"operation", "app", "description"})
        grants.append(Grant(operation=text(grant["operation"]), app=text(grant["app"]),
            description=text(grant["description"]),
            target=selector(grant["target"]) if "target" in grant else None,
            text_id=text(grant["text_id"], maximum=64) if "text_id" in grant else None,
            direction=text(grant["direction"]) if "direction" in grant else None,
            destination=text(grant["destination"], maximum=2048) if "destination" in grant else None,
            after=conditions(grant.get("after", [])), before=conditions(grant.get("before", [])),
            max_uses=grant.get("max_uses", 1)))
    success = conditions(data["success"])
    if not success:
        raise ValueError("At least one independently observable success condition is required.")
    supplied = data.get("texts", {})
    if not isinstance(supplied, dict) or len(supplied) > 32:
        raise ValueError("Task texts must be a map with at most 32 entries.")
    texts = {text(key, maximum=64): text(value, maximum=4096) for key, value in supplied.items()}
    for value in texts.values():
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Autonomous typing does not accept submission/control keys.")
    if any(g.text_id is not None and g.text_id not in texts for g in grants):
        raise ValueError("Grant references a missing supplied text ID.")
    limits = object_fields(data.get("limits", {}), set(Limits.__dataclass_fields__), set())
    for key, value in limits.items():
        maximum = 100 if key.startswith("max_") else 3600 if key == "seconds" else 60
        valid_type = type(value) is int if key.startswith("max_") else type(value) in (float, int)
        if not valid_type or not 0 < value <= maximum or not math.isfinite(value):
            raise ValueError("Invalid task limit.")
    adaptive = None
    if "adaptive" in data:
        from .adaptive import parse_scope
        adaptive = parse_scope(data["adaptive"])
    return TaskSpec(text(data["objective"], maximum=1024), tuple(grants), success, texts, Limits(**limits), adaptive)


def load_task(path: Path) -> TaskSpec:
    with path.open("rb") as stream:
        raw = stream.read(262_145)
    if len(raw) > 262_144:
        raise ValueError("Task file exceeds 256 KiB.")
    try:
        data = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Task file is not valid bounded JSON.") from None
    return parse_task(data)


def cloud_view(spec: TaskSpec, observation: Observation, offers: tuple[Offer, ...],
               *, step: int) -> tuple[dict[str, object], dict[str, object]]:
    """Positive projection. Never include raw labels, values, selectors or UDID.

    Only caller-authored objective/descriptions, known app identity, roles and
    availability leave the host. An unknown or secure screen never reaches Jev.
    """
    apps = {g.app for g in spec.grants} | {c.app for c in spec.success}
    apps |= {c.app for g in spec.grants for c in g.after}
    if observation.secure is not False or observation.elements is None or observation.app not in apps:
        raise DecisionUnavailable("Screen is unobserved, secure or outside approved app scope.")
    options: dict[str, object] = {
        offer.id: {
            "kind": "device_action",
            "operation": spec.grants[offer.grant_index].operation,
            "description": spec.grants[offer.grant_index].description,
            "boundary": "Only this caller-approved action from this exact snapshot.",
        }
        for offer in offers
    }
    options.update({
        "wait": {
            "kind": "wait",
            "description": "Wait briefly for an expected state change; send no device input.",
        },
        "done": {
            "kind": "verification",
            "description": "Request an independent completion check; do not claim success.",
        },
        "escalate": {
            "kind": "escalation",
            "description": "Stop and return control to the planner without device input.",
        },
    })
    view = {"objective": spec.objective,
            "snapshot": {"id": observation.id, "captured_at": observation.captured_at, "app": observation.app},
            "available_actions": [{"id": offer.id, "operation": spec.grants[offer.grant_index].operation,
                "target_id": offer.target_id,
                "role": spec.grants[offer.grant_index].target.role if spec.grants[offer.grant_index].target else None,
                "supplied_text_available": spec.grants[offer.grant_index].text_id is not None} for offer in offers],
            "completion_verified": False, "steps_remaining": spec.limits.max_steps - step}
    return view, options


def run_task(executor: Executor, spec: TaskSpec, *, driver: JevDriver | None = None,
             decision_only: bool = False) -> dict[str, object]:
    started = time.monotonic()
    events: list[dict[str, object]] = []
    observation = None
    previous_signature = None
    no_progress = 0
    decisions = 0
    last_decision = None

    def finish(status: str, reason: str, verification: str = "unknown") -> dict[str, object]:
        return {"status": status, "reason": reason, "verification": verification,
                "loop_seconds": time.monotonic() - started, "steps": len(events), "events": events,
                "snapshot": {"id": observation.id, "captured_at": observation.captured_at, "app": observation.app} if observation else None,
                "model": driver.summary() if driver else None, "last_decision": last_decision}

    try:
        for step in range(spec.limits.max_steps):
            observation = executor.current()
            state = executor.verify(observation, spec.success)
            if state == "satisfied":
                return finish("completed", "conditions_verified", state)
            if observation.secure is not False:
                return finish("blocked", "secure_screen")
            no_progress = no_progress + 1 if observation.signature == previous_signature else 0
            previous_signature = observation.signature
            if no_progress >= spec.limits.max_no_progress:
                return finish("escalated", "no_progress")
            offers = executor.offers(observation)
            if driver:
                if decisions >= spec.limits.max_decisions:
                    return finish("escalated", "decision_limit")
                view, options = cloud_view(spec, observation, offers, step=step)
                decisions += 1
                decision = driver.choose(view, options, executor.connection.budget)
                choice = decision.choice
                last_decision = decision.summary()
            else:
                if len(offers) != 1:
                    return finish("escalated", "no_unique_deterministic_action")
                choice = offers[0].id
            executor.connection.budget.remaining()
            if decision_only:
                index = next((offer.grant_index for offer in offers if offer.id == choice), None)
                events.append({"decision": "offered_action" if index is not None else choice, "grant_index": index})
                return finish("decision_only", "no_device_action_dispatched")
            if choice == "escalate":
                return finish("escalated", "driver_escalated")
            if choice in {"done", "wait"}:
                state, observation = executor.wait(spec.success)
                events.append({"operation": choice, "dispatch": "not_sent", "verification": state})
                if state == "satisfied":
                    return finish("completed", "conditions_verified", state)
                if choice == "done":
                    return finish("escalated", "completion_not_verified", state)
                continue
            index = next((offer.grant_index for offer in offers if offer.id == choice), None)
            # A final known postcondition needs only its predicate, not another
            # tree of controls. Intermediate navigation still returns the next
            # screen once, reused by the following iteration.
            final = index is not None and set(spec.success) <= set(spec.grants[index].after)
            result = executor.execute(choice, observe_next=not final)
            events.append({"grant_index": index, "dispatch": result.dispatch, "verification": result.verification,
                           "reason": result.reason, "acknowledged_substeps": result.acknowledged_substeps,
                           "error_type": result.error_type})
            observation = result.observation or observation
            if result.verification != "satisfied":
                return finish("escalated", result.reason, result.verification)
            # Even the final allowed action gets an independent success check.
            state = executor.verify(observation, spec.success)
            if state == "unknown" and observation.elements is None:
                # App-only postconditions cannot prove element-based task success.
                # Read under the remaining task budget; never replay the action.
                observation = executor.observe()
                state = executor.verify(observation, spec.success)
            if state == "satisfied":
                return finish("completed", "conditions_verified", "satisfied")
        return finish("escalated", "step_limit")
    except TaskStopped:
        return finish("blocked", "deadline_or_cancelled")
    except LowConfidenceDecision as exc:
        last_decision = {**exc.decision.summary(), "min_confidence": exc.min_confidence}
        return finish("escalated", "low_confidence")
    except DecisionUnavailable:
        return finish("escalated", "model_unavailable")
    except VerificationExpired:
        return finish("escalated", "verification_expired")
    except OpenClawIPhoneError:
        return finish("blocked", "observation_unavailable")
    except KeyboardInterrupt:
        executor.connection.invalidate(uncertain=True)
        return finish("escalated", "interrupted_outcome_unknown")
