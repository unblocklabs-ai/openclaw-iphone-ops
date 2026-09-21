"""App-independent, snapshot-bound execution for trusted caller-owned grants."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import time
import uuid
from urllib.parse import urlsplit

from .connection import TaskConnection
from .errors import OpenClawIPhoneError, WDAOutcomeUnknown, WDAUnavailable
from .observations import (EDITABLE, SCROLLABLE, TAPPABLE, Element, Observation,
                           ObservationRejected, Selector, parse_observation)


@dataclass(frozen=True)
class Condition:
    kind: str
    app: str
    target: Selector | None = None
    value: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in {"app", "exists", "absent", "actionable", "value", "focused"} or not self.app:
            raise ValueError("Invalid condition kind or app.")
        if (self.kind == "app") != (self.target is None):
            raise ValueError("Element conditions require a target; app conditions do not.")
        if (self.kind == "value") != (self.value is not None):
            raise ValueError("Only value conditions require an exact value.")


@dataclass(frozen=True)
class Grant:
    """Trusted policy, never constructed from model output or screen text.

    description is a caller-written, cloud-approved alias, not raw UI content.
    Authorizing a target does not prove its app-specific effect is harmless.
    """
    operation: str
    app: str
    description: str
    target: Selector | None = None
    text_id: str | None = None
    direction: str | None = None
    destination: str | None = field(default=None, repr=False)
    after: tuple[Condition, ...] = ()
    before: tuple[Condition, ...] = ()
    max_uses: int = 1

    def __post_init__(self) -> None:
        targeted = {"tap", "back", "append", "replace", "clear", "scroll"}
        if self.operation not in targeted | {"activate", "open_url"} or not self.app:
            raise ValueError("Invalid grant operation or source app.")
        if type(self.max_uses) is not int or not 1 <= self.max_uses <= 10:
            raise ValueError("Grant max_uses must be an integer from 1 to 10.")
        if not self.description or len(self.description) > 256:
            raise ValueError("A short caller-approved action description is required.")
        if (self.operation in targeted) != (self.target is not None):
            raise ValueError("Target is required only for element operations.")
        if (self.operation in {"append", "replace"}) != (self.text_id is not None):
            raise ValueError("Typing must reference exact caller-supplied text.")
        if self.operation == "scroll":
            if self.direction not in {"up", "down"} or self.target.role not in SCROLLABLE:
                raise ValueError("Scroll requires a bounded direction and scrollable container.")
        elif self.direction is not None:
            raise ValueError("Only scroll accepts direction.")
        if self.operation in {"activate", "open_url"}:
            if not self.destination or not any(c.kind == "app" for c in self.after):
                raise ValueError("App transitions require an exact destination and app postcondition.")
            if self.operation == "open_url":
                parsed = urlsplit(self.destination)
                if not parsed.scheme or parsed.scheme in {"file", "javascript", "data"} or parsed.username or parsed.password:
                    raise ValueError("Deep links require an explicit non-file scheme without credentials.")
        elif self.destination is not None:
            raise ValueError("Only app transitions accept destination.")
        if self.operation in {"tap", "back"}:
            if self.target.role not in TAPPABLE or not (self.target.name or self.target.label) or not self.after:
                raise ValueError("Tap requires a named compatible control and explicit postcondition.")
        if self.operation == "back" and (self.target.name or self.target.label or "").casefold() not in {"back", "go back"}:
            raise ValueError("Back requires an explicit Back control, never a top-left guess.")
        if self.operation in {"append", "replace", "clear"} and (self.target.role not in EDITABLE or not (self.target.name or self.target.label)):
            raise ValueError("Typing requires a named non-secure editable target.")


@dataclass(frozen=True)
class Offer:
    id: str
    snapshot_id: str
    grant_index: int
    target_id: str | None


@dataclass(frozen=True)
class StepResult:
    dispatch: str  # not_sent, acknowledged, unknown (compound actions can be partial)
    verification: str  # satisfied, unsatisfied, unknown
    reason: str
    observation: Observation | None = field(default=None, repr=False)
    acknowledged_substeps: int = 0
    error_type: str | None = None


class Executor:
    def __init__(self, connection: TaskConnection, grants: tuple[Grant, ...], *,
                 texts: dict[str, str] | None = None, freshness: float = 30,
                 verification_seconds: float = 15) -> None:
        if any(not math.isfinite(v) or v <= 0 for v in (freshness, verification_seconds)):
            raise ValueError("Freshness and verification bounds must be finite and positive.")
        if len(grants) > 252:
            raise ValueError("At most 252 action grants are allowed.")
        self.connection, self.grants = connection, tuple(grants)
        self.texts = dict(texts or {})
        for grant in grants:
            if grant.text_id is not None:
                text = self.texts.get(grant.text_id)
                if not isinstance(text, str) or not text or len(text) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                    raise ValueError("Typing requires 1–4096 supplied characters, without submission/control keys.")
        self.freshness, self.verification_seconds = freshness, verification_seconds
        self.latest: Observation | None = None
        self._offers: dict[str, Offer] = {}
        self._uses = [0] * len(grants)
        self.stopped = False
        destinations = {g.destination for g in grants if g.operation == "activate"}
        if destinations:
            connection.require_active()
            installed, _ = connection.ctl.list_apps(connection.device.identifier)
            if not destinations <= {a.bundle_identifier for a in installed}:
                raise ObservationRejected("An authorized destination app is not installed.")

    def observe(self) -> Observation:
        wda = self.connection.require_active()
        started = time.monotonic()
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            wda.require_unlocked()
            before = wda.active_app()
            source = wda.source()
            after = wda.active_app()
        except OpenClawIPhoneError:
            self.connection.invalidate()
            self._offers.clear()
            raise
        if (type(after.get("pid")) is not int or after["pid"] <= 0 or
                (before.get("bundleId"), before.get("pid")) != (after.get("bundleId"), after.get("pid"))):
            self._offers.clear()
            raise ObservationRejected("Foreground app changed during observation.")
        self.latest = parse_observation(source, generation=self.connection.generation,
            device_udid=self.connection.device.udid, app=after["bundleId"],
            captured_at=stamp, started=started, finished=time.monotonic(), process_id=after["pid"])
        return self.latest

    def evaluate(self, observation: Observation, condition: Condition) -> str:
        if observation.app != condition.app:
            return "unsatisfied"
        if condition.kind == "app":
            return "satisfied"
        matches = observation.matches(condition.target)
        if condition.kind == "absent":
            # Unknown visibility is not proof of absence.
            possible = [e for e in observation.elements if e.matches(condition.target) and e.visible is not False]
            return "unsatisfied" if matches else "unknown" if possible else "satisfied"
        if len(matches) != 1:
            possible = any(e.matches(condition.target) and e.visible is None for e in observation.elements)
            return "unsatisfied" if not matches and not possible else "unknown"
        element = matches[0]
        if condition.kind == "exists":
            return "satisfied"
        if condition.kind == "actionable":
            return "satisfied" if element.actionable else "unknown"
        if condition.kind == "focused":
            # iOS XML often omits focus. Ask WDA, never assume from a prior tap.
            try:
                wda = self.connection.require_active()
                refs = wda.find_elements(element.xpath)
                return "satisfied" if len(refs) == 1 and wda.active_element() == refs[0] else "unsatisfied"
            except WDAUnavailable:
                return "unknown"
        if element.role not in EDITABLE:
            return "unknown"
        value = element.value
        if value is None:
            try:
                reference = self._reference(element)
                self._guard_app(observation.app, observation.process_id)
                value = self.connection.require_active().element_value(reference)
            except OpenClawIPhoneError:
                return "unknown"
        return "satisfied" if value == condition.value else "unsatisfied"

    def verify(self, observation: Observation, conditions: tuple[Condition, ...]) -> str:
        if not conditions:
            return "unknown"
        states = [self.evaluate(observation, c) for c in conditions]
        return "unsatisfied" if "unsatisfied" in states else "unknown" if "unknown" in states else "satisfied"

    def wait(self, conditions: tuple[Condition, ...], *, seconds: float | None = None) -> tuple[str, Observation]:
        duration = self.verification_seconds if seconds is None else seconds
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Wait timeout must be finite and positive.")
        end = min(time.monotonic() + duration, self.connection.budget.deadline)
        wda = self.connection.require_active()
        previous = wda.deadline
        wda.deadline = min(previous, end) if previous is not None else end
        try:
            while True:
                try:
                    observation = self.observe()
                except ObservationRejected:
                    # A transitioning app may change during a read. Retrying
                    # observation is safe; never repeat the preceding input.
                    if time.monotonic() >= end:
                        raise
                    self.connection.budget.sleep(min(0.2, end - time.monotonic()))
                    continue
                state = self.verify(observation, conditions)
                if state == "satisfied" or time.monotonic() >= end:
                    return state, observation
                self.connection.budget.sleep(min(0.2, max(0, end - time.monotonic())))
        finally:
            wda.deadline = previous

    def offers(self, observation: Observation) -> tuple[Offer, ...]:
        self._offers.clear()
        self._check_snapshot(observation)
        if self.stopped or observation.secure:
            return ()
        for index, grant in enumerate(self.grants):
            if self._uses[index] >= grant.max_uses:
                continue
            if grant.app != observation.app or grant.before and self.verify(observation, grant.before) != "satisfied":
                continue
            target = observation.unique(grant.target) if grant.target else None
            if grant.target and (target is None or not target.actionable):
                continue
            if grant.after and self.verify(observation, grant.after) == "satisfied":
                continue
            if target and grant.operation in {"tap", "back", "append", "replace", "clear"} and not (target.name or target.label):
                continue
            offer = Offer(uuid.uuid4().hex, observation.id, index, target.id if target else None)
            self._offers[offer.id] = offer
        return tuple(self._offers.values())

    def _check_snapshot(self, observation: Observation) -> None:
        self.connection.require_active()
        if (self.latest is not observation or observation.generation != self.connection.generation
                or observation.device_udid != self.connection.device.udid
                or time.monotonic() - observation.started > self.freshness):
            raise ObservationRejected("Snapshot is stale, foreign or superseded; reobserve and choose again.")

    def _guard_app(self, app: str, process_id: int | None) -> None:
        wda = self.connection.require_active()
        wda.require_unlocked()
        active = wda.active_app()
        if active["bundleId"] != app or active.get("pid") != process_id:
            raise ObservationRejected("Foreground app changed before dispatch.")

    def _reference(self, element: Element) -> str:
        refs = self.connection.require_active().find_elements(element.xpath)
        if len(refs) != 1:
            raise ObservationRejected("Target no longer resolves uniquely; no coordinate fallback.")
        if not self.connection.require_active().element_hittable(refs[0]):
            raise ObservationRejected("Target is obscured or hit testing is unavailable.")
        return refs[0]

    def execute(self, offer_id: str) -> StepResult:
        original = self.latest
        offer = self._offers.get(offer_id)
        if self.stopped or offer is None or original is None or offer.snapshot_id != original.id:
            return StepResult("not_sent", "unknown", "invalid_or_consumed_offer")
        # Consume the entire observation's choices before any dispatch.
        self._offers.clear()
        acknowledged = 0
        dispatching = False
        try:
            self._check_snapshot(original)
            grant = self.grants[offer.grant_index]
            old = next((e for e in original.elements if e.id == offer.target_id), None)
            current = self.observe()
            target = current.unique(grant.target) if grant.target else None
            if (current.secure or (current.app, current.process_id) != (original.app, original.process_id)
                    or grant.target and (old is None or target is None or target.fingerprint() != old.fingerprint())):
                raise ObservationRejected("App or target changed since selection.")
            if time.monotonic() - original.started > self.freshness:
                raise ObservationRejected("Choice expired during validation.")
            if grant.before and self.verify(current, grant.before) != "satisfied":
                raise ObservationRejected("Action precondition no longer holds.")
            reference = self._reference(target) if target else None
            self._guard_app(grant.app, current.process_id)
            wda = self.connection.require_active()
            operation = grant.operation
            conditions = grant.after
            self._uses[offer.grant_index] += 1
            if operation in {"append", "replace", "clear"}:
                if wda.active_element() != reference:
                    raise ObservationRejected("Intended editable field is not focused.")
                if operation == "append" and target.value is None and wda.element_value(reference) != "":
                    raise ObservationRejected("Initial field value changed or is unknown; cannot verify append.")
                if operation == "append" and target.value and target.value in {target.name, target.label}:
                    raise ObservationRejected("Value may be a placeholder; cannot infer the initial text.")
                expected = (target.value or "") + self.texts[grant.text_id] if operation == "append" else self.texts[grant.text_id] if operation == "replace" else ""
                if operation in {"replace", "clear"}:
                    dispatching = True
                    wda.element_action(reference, "clear")
                    acknowledged += 1
                    dispatching = False
                    state, current = self.wait((Condition("value", grant.app, grant.target, ""),))
                    if state != "satisfied":
                        self.stopped = True
                        return StepResult("acknowledged", state, "clear_not_verified", current, acknowledged)
                    if operation == "replace":
                        target = current.unique(grant.target)
                        reference = self._reference(target)
                        self._guard_app(grant.app, current.process_id)
                        if wda.active_element() != reference:
                            raise ObservationRejected("Focus changed after clearing; text not sent.")
                if operation != "clear":
                    dispatching = True
                    wda.element_action(reference, "value", text=self.texts[grant.text_id])
                    acknowledged += 1
                    dispatching = False
                conditions = (Condition("value", grant.app, grant.target, expected),) + conditions
            else:
                dispatching = True
                if operation in {"tap", "back"}:
                    wda.element_action(reference, "click")
                elif operation == "activate":
                    wda.activate_app(grant.destination)
                elif operation == "open_url":
                    wda.open_url(grant.destination)
                elif operation == "scroll":
                    wda.element_scroll(reference, grant.direction)
                acknowledged += 1
                dispatching = False
            if operation == "scroll" and not conditions:
                current = self.observe()
                current_target = current.unique(grant.target)
                changed = (current_target is not None and
                           scoped_items(current, current_target) != scoped_items(original, old))
                state = "satisfied" if current.app == grant.app and changed else "unsatisfied"
            else:
                state, current = self.wait(conditions)
            if state != "satisfied":
                self.stopped = True  # An acknowledged action is never replayed on failed readback.
            return StepResult("acknowledged", state, "verified" if state == "satisfied" else "postcondition_not_verified", current, acknowledged)
        except OpenClawIPhoneError as exc:
            self.stopped = True
            if dispatching and isinstance(exc, WDAOutcomeUnknown):
                self.connection.invalidate(uncertain=True)
                return StepResult("unknown", "unknown", "mutation_outcome_unknown", acknowledged_substeps=acknowledged)
            if isinstance(exc, WDAUnavailable):
                self.connection.invalidate()
            return StepResult("acknowledged" if acknowledged else "not_sent", "unknown",
                              "verification_unavailable" if acknowledged else "validation_failed",
                              acknowledged_substeps=acknowledged, error_type=type(exc).__name__)


def scoped_items(observation: Observation, container: Element) -> tuple[object, ...]:
    return tuple((e.role, e.name, e.label, e.value, e.bounds) for e in observation.elements
                 if e.visible is True and e.path.startswith(container.path + "/"))
