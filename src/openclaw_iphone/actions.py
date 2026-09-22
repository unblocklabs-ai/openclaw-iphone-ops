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
                           ObservationRejected, Selector, keypad_points, parse_observation)


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
        targeted = {"tap", "back", "append", "replace", "clear", "scroll", "keypad"}
        if self.operation not in targeted | {"activate", "open_url"} or not self.app:
            raise ValueError("Invalid grant operation or source app.")
        if type(self.max_uses) is not int or not 1 <= self.max_uses <= 10:
            raise ValueError("Grant max_uses must be an integer from 1 to 10.")
        if not self.description or len(self.description) > 256:
            raise ValueError("A short caller-approved action description is required.")
        if (self.operation in targeted) != (self.target is not None):
            raise ValueError("Target is required only for element operations.")
        if (self.operation in {"append", "replace", "keypad"}) != (self.text_id is not None):
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
        if self.operation == "keypad" and (self.target.role not in EDITABLE | {"XCUIElementTypeOther"}
                                           or not (self.target.name or self.target.label)):
            raise ValueError("Keypad requires a named non-secure input target.")


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
                if grant.operation == "keypad" and (len(text) > 32 or any(c not in "0123456789" for c in text)):
                    raise ValueError("Keypad accepts 1–32 supplied ASCII digits only.")
        self.freshness, self.verification_seconds = freshness, verification_seconds
        self.latest: Observation | None = None
        self._offers: dict[str, Offer] = {}
        self._offer_blockers: tuple[dict[str, object], ...] = ()
        self._uses = [0] * len(grants)
        self.stopped = False
        destinations = {g.destination for g in grants if g.operation == "activate"}
        if destinations:
            connection.require_active()
            installed, _ = connection.ctl.list_apps(connection.device.identifier)
            if not destinations <= {a.bundle_identifier for a in installed}:
                raise ObservationRejected("An authorized destination app is not installed.")

    def observe(self, *, app_only: bool = False) -> Observation:
        """Read a full screen by default; app-only evidence cannot authorize input."""
        wda = self.connection.require_active()
        self._offers.clear()
        self.latest = None
        started = time.monotonic()
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            wda.require_unlocked()
            before = wda.active_app()
            source = None if app_only else wda.source()
            after = before if app_only else wda.active_app()
        except OpenClawIPhoneError:
            self.connection.invalidate()
            self._offers.clear()
            raise
        if (type(after.get("pid")) is not int or after["pid"] <= 0 or
                (before.get("bundleId"), before.get("pid")) != (after.get("bundleId"), after.get("pid"))):
            self._offers.clear()
            raise ObservationRejected("Foreground app changed during observation.")
        if source is None:
            self.latest = Observation(uuid.uuid4().hex, self.connection.generation,
                self.connection.device.udid, after["bundleId"], stamp, started,
                time.monotonic(), None, None, f"app:{after['bundleId']}:{after['pid']}", after["pid"])
        else:
            self.latest = parse_observation(source, generation=self.connection.generation,
                device_udid=self.connection.device.udid, app=after["bundleId"],
                captured_at=stamp, started=started, finished=time.monotonic(), process_id=after["pid"])
        return self.latest

    def evaluate(self, observation: Observation, condition: Condition) -> str:
        if observation.app != condition.app:
            return "unsatisfied"
        if condition.kind == "app":
            return "satisfied"
        if observation.elements is None:
            return "unknown"
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
        if element.role not in EDITABLE | {"XCUIElementTypeOther"}:
            return "unknown"
        value = element.value
        if value is None or element.role == "XCUIElementTypeOther":
            try:
                reference = self._reference(element)
                self._guard_app(observation.app, observation.process_id)
                wda = self.connection.require_active()
                value = (wda.element_value(reference, allow_null_empty=False)
                         if element.role == "XCUIElementTypeOther" else wda.element_value(reference))
            except OpenClawIPhoneError:
                return "unknown"
        return "satisfied" if value == condition.value else "unsatisfied"

    def verify(self, observation: Observation, conditions: tuple[Condition, ...]) -> str:
        if not conditions:
            return "unknown"
        state = "satisfied"
        for condition in conditions:
            current = self.evaluate(observation, condition)
            if current == "unsatisfied":
                return current
            if current == "unknown":
                state = current
        return state

    def wait(self, conditions: tuple[Condition, ...], *, seconds: float | None = None) -> tuple[str, Observation]:
        duration = self.verification_seconds if seconds is None else seconds
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Wait timeout must be finite and positive.")
        if not conditions:
            return "unknown", self.observe()
        end = min(time.monotonic() + duration, self.connection.budget.deadline)
        wda = self.connection.require_active()
        previous = wda.deadline
        wda.deadline = min(previous, end) if previous is not None else end
        app_only = bool(conditions) and all(c.kind == "app" for c in conditions)
        try:
            while True:
                try:
                    observation = self.observe(app_only=app_only)
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
        blockers: list[dict[str, object]] = []
        self._offer_blockers = ()
        self._check_snapshot(observation)
        if self.stopped:
            self._offer_blockers = ({"scope": "all", "reason": "executor_stopped"},)
            return ()
        if observation.elements is None:
            self._offer_blockers = ({"scope": "all", "reason": "accessibility_unavailable"},)
            return ()
        if observation.secure is not False:
            self._offer_blockers = ({"scope": "all", "reason": "secure_screen"},)
            return ()
        for index, grant in enumerate(self.grants):
            if self._uses[index] >= grant.max_uses:
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "grant_exhausted"})
                continue
            if grant.app != observation.app:
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "foreground_app_mismatch"})
                continue
            if grant.before:
                before_state = self.verify(observation, grant.before)
                if before_state != "satisfied":
                    blockers.append({"grant_index": index, "operation": grant.operation,
                                     "reason": "precondition_unknown" if before_state == "unknown" else "precondition_unsatisfied"})
                    continue
            if grant.target and observation.unique(grant.target) is None:
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "target_missing_or_ambiguous"})
                continue
            target = observation.unique(grant.target) if grant.target else None
            if grant.target and target is not None and not target.actionable:
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "target_not_actionable"})
                continue
            if grant.operation in {"append", "keypad"} and target.value not in (None, ""):
                # WDA inserts at the current caret/selection, not necessarily
                # the end. Never offer an append to existing text.
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "field_not_verified_empty"})
                continue
            if grant.after and self.verify(observation, grant.after) == "satisfied":
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "postcondition_already_satisfied"})
                continue
            if target and grant.operation in {"tap", "back", "append", "replace", "clear", "keypad"} and not (target.name or target.label):
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "target_unnamed"})
                continue
            offer = Offer(uuid.uuid4().hex, observation.id, index, target.id if target else None)
            self._offers[offer.id] = offer
        self._offer_blockers = tuple(blockers)
        return tuple(self._offers.values())

    @property
    def offer_blockers(self) -> tuple[dict[str, object], ...]:
        return self._offer_blockers

    def _check_snapshot(self, observation: Observation) -> None:
        self.connection.require_active()
        if (self.latest is not observation or observation.generation != self.connection.generation
                or observation.device_udid != self.connection.device.udid
                or time.monotonic() - observation.started > self.freshness):
            raise ObservationRejected("Snapshot is stale, foreign or superseded; reobserve and choose again.")

    def _guard_app(self, app: str, process_id: int | None) -> None:
        wda = self.connection.require_active()
        # Observations and each transport mutation check lock state. This guard
        # only closes the foreground-identity gap immediately before dispatch.
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
        if (self.stopped or offer is None or original is None or original.elements is None
                or offer.snapshot_id != original.id):
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
            if operation == "keypad":
                supplied = self.texts[grant.text_id]
                if (wda.active_element() != reference
                        or wda.element_value(reference, allow_null_empty=target.role in EDITABLE) != ""):
                    raise ObservationRejected("Keypad requires verified focus and an empty field.")
                points = keypad_points(current, supplied)
                self._check_snapshot(current)
                self._guard_app(grant.app, current.process_id)
                dispatching = True
                wda.tap_sequence(points)
                acknowledged += 1
                dispatching = False
                # Auto-submit may replace the field. Verify an explicit
                # destination if supplied, otherwise verify the complete value.
                conditions = conditions or (Condition("value", grant.app, grant.target, supplied),)
            elif operation in {"append", "replace", "clear"}:
                if wda.active_element() != reference:
                    raise ObservationRejected("Intended editable field is not focused.")
                if operation == "append" and (target.value not in (None, "") or wda.element_value(reference) != ""):
                    raise ObservationRejected("Append requires a verified empty field; caret position is unknown. Use an explicit replace grant for whole-field entry.")
                expected = self.texts[grant.text_id] if operation in {"append", "replace"} else ""
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
    if observation.elements is None:
        raise ObservationRejected("App-only observation cannot verify scroll progress.")
    return tuple((e.role, e.name, e.label, e.value, e.bounds) for e in observation.elements
                 if e.visible is True and e.path.startswith(container.path + "/"))
