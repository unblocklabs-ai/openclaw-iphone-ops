"""App-independent, snapshot-bound execution for trusted caller-owned grants."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
import time
import uuid
from urllib.parse import urlsplit

from .connection import TaskConnection
from .errors import OpenClawIPhoneError, VerificationExpired, WDAOutcomeUnknown, WDAStaleElement, WDAUnavailable
from .observations import (EDITABLE, SCROLLABLE, TAPPABLE, Element, Observation,
                           ObservationRejected, Selector, keypad_points, parse_observation, xpath_literal)


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


@dataclass
class PredicateReads:
    """Facts shared only within one synchronous predicate evaluation pass."""
    references: dict[Selector, list[str]] = field(default_factory=dict)
    values: dict[Selector, str] = field(default_factory=dict)
    focused: str | None = None
    native_mismatch: bool = False


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
        self._verified: dict[Condition, str] = {}
        self._values: dict[Condition, str] = {}
        self._offers: dict[str, Offer] = {}
        self._offer_blockers: tuple[dict[str, object], ...] = ()
        self._uses = [0] * len(grants)
        self.stopped = False
        self.pending: tuple[Condition, ...] | None = None

    def discard(self) -> None:
        """A mutation or failed read consumes every prior choice and proof."""
        self.latest = None
        self._offers.clear()
        self._verified.clear()
        self._values.clear()

    def current(self, *, full: bool = True) -> Observation:
        observation = self.latest
        if (observation is None or full and observation.elements is None
                or time.monotonic() - observation.started > self.freshness):
            return self.observe(app_only=not full)
        self._check_snapshot(observation)
        return observation

    def observe(self, *, app_only: bool = False, invalidate_on_error: bool = True) -> Observation:
        """Read a full screen by default; app-only evidence cannot authorize input."""
        wda = self.connection.require_active()
        self.discard()
        started = time.monotonic()
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            source = None if app_only else wda.source(compact=True)
            after = wda.active_app()
        except OpenClawIPhoneError:
            if invalidate_on_error:
                self.connection.invalidate()
            raise
        if type(after.get("pid")) is not int or after["pid"] <= 0:
            self._offers.clear()
            raise ObservationRejected("Foreground process identity is unavailable.")
        if source is None:
            self.latest = Observation(uuid.uuid4().hex, self.connection.generation,
                self.connection.device.udid, after["bundleId"], stamp, started,
                time.monotonic(), None, None, f"app:{after['bundleId']}:{after['pid']}", after["pid"])
        else:
            self.latest = parse_observation(source, generation=self.connection.generation,
                device_udid=self.connection.device.udid, app=after["bundleId"],
                captured_at=stamp, started=started, finished=time.monotonic(), process_id=after["pid"])
        return self.latest

    def _find(self, target: Selector | Element) -> list[str]:
        using, query = target.locator()
        return self.connection.require_active().find_elements(query, using=using)

    def _read_condition(self, condition: Condition, reads: PredicateReads) -> str:
        target = condition.target
        if condition.kind == "value" and target.role not in EDITABLE | {"XCUIElementTypeOther"}:
            return "unknown"
        wda = self.connection.require_active()
        if target not in reads.references:
            reads.references[target] = self._find(target)
        refs = reads.references[target]
        if len(refs) != 1:
            return "unsatisfied" if not refs else "unknown"
        if condition.kind == "exists":
            return "satisfied"
        if condition.kind == "focused":
            if reads.focused is None:
                reads.focused = wda.active_element()
            return "satisfied" if reads.focused == refs[0] else "unsatisfied"
        if target not in reads.values:
            try:
                value = wda.element_value(refs[0], allow_null_empty=target.role in EDITABLE)
            except WDAStaleElement:
                # Only an explicit stale READ permits re-resolution. Transport
                # failures are not a reason to replay a write or guess a value.
                reads.references[target] = self._find(target)
                if len(reads.references[target]) != 1:
                    return "unknown"
                value = wda.element_value(reads.references[target][0], allow_null_empty=target.role in EDITABLE)
            reads.values[target] = value
        self._values[condition] = reads.values[target]
        return "satisfied" if reads.values[target] == condition.value else "unsatisfied"

    def evaluate(self, observation: Observation, condition: Condition, *, reads: PredicateReads | None = None) -> str:
        if observation is self.latest and condition in self._verified:
            return self._verified[condition]
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
        if condition.kind == "focused" or element.value is None or element.role == "XCUIElementTypeOther":
            try:
                return self._read_condition(condition, reads if reads is not None else PredicateReads())
            except OpenClawIPhoneError:
                return "unknown"
        if element.role not in EDITABLE:
            return "unknown"
        if (condition.kind == "value" and element.value != condition.value and reads is not None
                and reads.native_mismatch and condition.target in reads.references):
            try:
                # This is only a supplied post-input field. Re-resolve it on
                # the current screen before one native read; never trust a
                # previous write reference when the XML disagrees.
                reads.references[condition.target] = self._find(condition.target)
                self._guard_app(observation.app, observation.process_id)
                state = self._read_condition(condition, reads)
                self._guard_app(observation.app, observation.process_id)
            except OpenClawIPhoneError:
                self._values.pop(condition, None)
                state = "unknown"
            if len(reads.references.get(condition.target, [])) != 1:
                self._values.pop(condition, None)
                state = "unknown"
            self._verified[condition] = state
            return state
        self._values[condition] = element.value
        return "satisfied" if element.value == condition.value else "unsatisfied"

    def verify(self, observation: Observation, conditions: tuple[Condition, ...], *,
               cache: dict[Condition, str] | None = None, reads: PredicateReads | None = None) -> str:
        if not conditions:
            return "unknown"
        state = "satisfied"
        reads = reads if reads is not None else PredicateReads()
        for condition in conditions:
            if cache is not None and condition in cache:
                current = cache[condition]
            else:
                current = self.evaluate(observation, condition, reads=reads)
                if cache is not None:
                    cache[condition] = current
            if current == "unsatisfied":
                return current
            if current == "unknown":
                state = current
        return state

    def _observe_conditions(self, conditions: tuple[Condition, ...], *, full: bool = False,
                            references: dict[Selector, str] | None = None) -> Observation:
        # App identity alone never needs a tree, even if the caller could use
        # next-screen evidence. Element predicates with full=True do.
        narrow = (all(c.kind == "app" for c in conditions)
                  or not full and all(c.kind in {"app", "value", "focused", "exists"} for c in conditions))
        observation = self.observe(app_only=narrow, invalidate_on_error=False)
        if not narrow:
            return observation
        reads = PredicateReads({target: [ref] for target, ref in (references or {}).items()})
        states: dict[Condition, str] = {}
        for condition in dict.fromkeys(conditions):
            if condition.kind == "app" or condition.app != observation.app:
                states[condition] = self.evaluate(observation, condition)
                continue
            states[condition] = self._read_condition(condition, reads)
        if references is not None:
            # Keep a replacement handle only for this wait's existing hints;
            # the next poll should not deliberately retry the stale handle.
            for target in references:
                refs = reads.references[target]
                if len(refs) == 1:
                    references[target] = refs[0]
        # This is predicate evidence, NOT a full tree that could prove absence
        # or authorize another action. A later decision acquires its tree once.
        self._verified = states
        self.latest = replace(observation, finished=time.monotonic())
        return self.latest

    def wait(self, conditions: tuple[Condition, ...], *, seconds: float | None = None,
             once: bool = False, full: bool = False,
             references: dict[Selector, str] | None = None,
             native_mismatch: bool = False) -> tuple[str, Observation]:
        duration = self.verification_seconds if seconds is None else seconds
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Wait timeout must be finite and positive.")
        if not conditions:
            return "unknown", self.observe()
        end = min(time.monotonic() + duration, self.connection.budget.deadline)
        wda = self.connection.require_active()
        previous = wda.deadline
        end = min(previous, end) if previous is not None else end
        wda.deadline = end
        try:
            while True:
                try:
                    observation = self._observe_conditions(conditions, full=full, references=references)
                except ObservationRejected:
                    # A transitioning app may change during a read. Retrying
                    # observation is safe; never repeat the preceding input.
                    if time.monotonic() >= end:
                        raise VerificationExpired("Verification window expired; inspect without replaying input.")
                    self.connection.budget.sleep(min(0.2, end - time.monotonic()))
                    continue
                reads = PredicateReads({target: [ref] for target, ref in (references or {}).items()},
                                       native_mismatch=native_mismatch) if full else None
                state = self.verify(observation, conditions, reads=reads)
                if full and references is not None and reads is not None:
                    for target in references:
                        refs = reads.references[target]
                        if len(refs) == 1:
                            references[target] = refs[0]
                if time.monotonic() > end:
                    raise VerificationExpired("Verification read exceeded its deadline; input is not replayed.")
                if state == "satisfied" and self.pending == conditions:
                    self.pending = None
                if state == "satisfied" or once or time.monotonic() >= end:
                    return state, observation
                self.connection.budget.sleep(min(0.2, max(0, end - time.monotonic())))
                if time.monotonic() >= end:
                    return state, observation
        except OpenClawIPhoneError as exc:
            self.discard()
            self.connection.budget.remaining()
            if isinstance(exc, VerificationExpired) or isinstance(exc, WDAUnavailable) and time.monotonic() >= end:
                raise VerificationExpired("Verification window expired; dispatch is not retried.") from exc
            self.connection.invalidate()
            raise
        finally:
            wda.deadline = previous

    def offers(self, observation: Observation) -> tuple[Offer, ...]:
        self._offers.clear()
        blockers: list[dict[str, object]] = []
        self._offer_blockers = ()
        self._check_snapshot(observation)
        if self.stopped or self.pending is not None:
            self._offer_blockers = ({"scope": "all", "reason": "executor_stopped" if self.stopped else "verification_pending"},)
            return ()
        if observation.elements is None:
            self._offer_blockers = ({"scope": "all", "reason": "accessibility_unavailable"},)
            return ()
        if observation.secure is not False:
            self._offer_blockers = ({"scope": "all", "reason": "secure_screen"},)
            return ()
        predicates: dict[Condition, str] = {}
        reads = PredicateReads()
        for index, grant in enumerate(self.grants):
            if self._uses[index] >= grant.max_uses:
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "grant_exhausted"})
                continue
            if grant.app != observation.app:
                blockers.append({"grant_index": index, "operation": grant.operation, "reason": "foreground_app_mismatch"})
                continue
            if grant.before:
                before_state = self.verify(observation, grant.before, cache=predicates, reads=reads)
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
            if grant.after and self.verify(observation, grant.after, cache=predicates, reads=reads) == "satisfied":
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
        if self.latest is not observation:
            raise ObservationRejected("Snapshot was superseded; reobserve and choose again.")
        self._check_binding(observation)

    def _check_binding(self, observation: Observation) -> None:
        """Local only: still enforced after slow validation/clear reads."""
        self.connection.require_active()
        if (observation.generation != self.connection.generation
                or observation.device_udid != self.connection.device.udid
                or time.monotonic() - observation.started > self.freshness):
            raise ObservationRejected("Snapshot is stale or foreign; reobserve and choose again.")

    def _guard_app(self, app: str, process_id: int | None = None) -> None:
        wda = self.connection.require_active()
        if process_id is None:
            matches = wda.app_state(app) == 4  # XCUIApplicationStateRunningForeground
        else:
            # Pixel evidence is tied to this exact process, unlike a freshly
            # resolved native target. Preserve the strict guard for vision.
            active = wda.active_app()
            matches = active["bundleId"] == app and active.get("pid") == process_id
        if not matches:
            raise ObservationRejected("Foreground app changed before dispatch.")

    def _reference(self, element: Element, *, hittable: bool = True) -> str:
        refs = self._find(element)
        if len(refs) != 1:
            raise ObservationRejected("Target no longer resolves uniquely; no coordinate fallback.")
        if hittable and not self.connection.require_active().element_hittable(refs[0]):
            raise ObservationRejected("Target is obscured or hit testing is unavailable.")
        return refs[0]

    def _keypad_points(self, observation: Observation, digits: str) -> list[tuple[float, float]]:
        points = keypad_points(observation, digits)
        wanted = set(digits)
        keys = [e for e in observation.elements or () if e.role == "XCUIElementTypeKey"
                and e.actionable and (e.name in wanted or e.label in wanted)
                and e.bounds and (e.bounds[0] + e.bounds[2] / 2, e.bounds[1] + e.bounds[3] / 2) in points]
        # One query validates every used key's identity AND geometry, without
        # rereading the entire screen or checking each digit in the batch.
        queries = []
        for key in keys:
            digit = key.name if key.name in wanted else key.label
            label = xpath_literal(digit)
            unique = ("//XCUIElementTypeKey[ancestor::XCUIElementTypeKeyboard and not(ancestor::*[@visible='false']) and @visible='true' "
                      f"and @enabled='true' and (@name={label} or @label={label})]")
            queries.append(f"{key.xpath}[count({unique})=1 and not(ancestor::*[@visible='false'])]")
        refs = self.connection.require_active().find_elements(" | ".join(queries))
        if len(set(refs)) != len(keys):
            raise ObservationRejected("Native keypad changed; no touches sent.")
        return points

    def _dispatch_guard(self, observation: Observation, *, pixels: bool = False) -> None:
        self._guard_app(observation.app, observation.process_id if pixels else None)
        self._check_binding(observation)

    def execute(self, offer_id: str, *, observe_next: bool = True) -> StepResult:
        original = self.latest
        offer = self._offers.get(offer_id)
        if (self.stopped or self.pending is not None or offer is None or original is None or original.elements is None
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
            target = old
            reference = self._reference(target, hittable=grant.operation not in {"append", "replace", "clear", "keypad"}) if target else None
            references = {grant.target: reference} if target else {}
            reads = PredicateReads({key: [ref] for key, ref in references.items()})
            if grant.before:
                # Read live preconditions using the selected target's existing
                # reference, not a separate observation/dispatch pipeline.
                for condition in grant.before:
                    if condition.app != original.app:
                        state = "unsatisfied"
                    elif condition.kind == "app":
                        state = "satisfied"  # The dispatch guard checks it once.
                    elif condition.kind in {"focused", "value", "exists"}:
                        state = self._read_condition(condition, reads)
                    else:
                        state, _ = self.wait((condition,), once=True)
                    if state != "satisfied":
                        raise ObservationRejected("Action precondition no longer holds.")
            wda = self.connection.require_active()
            operation = grant.operation
            conditions = grant.after
            if operation == "keypad":
                supplied = self.texts[grant.text_id]
                if (self._read_condition(Condition("focused", grant.app, grant.target), reads) != "satisfied"
                        or self._read_condition(Condition("value", grant.app, grant.target, ""), reads) != "satisfied"):
                    raise ObservationRejected("Keypad requires verified focus and an empty field.")
                points = self._keypad_points(original, supplied)
                self._dispatch_guard(original)
                self.discard()
                dispatching = True
                self._uses[offer.grant_index] += 1
                wda.tap_sequence(points)
                acknowledged += 1
                dispatching = False
                # Auto-submit may replace the field. Verify an explicit
                # destination if supplied, otherwise verify the complete value.
                conditions = conditions or (Condition("value", grant.app, grant.target, supplied),)
            elif operation in {"append", "replace", "clear"}:
                if operation == "append" and (target.value not in (None, "") or
                        self._read_condition(Condition("value", grant.app, grant.target, ""), reads) != "satisfied"):
                    raise ObservationRejected("Append requires a verified empty field; caret position is unknown. Use an explicit replace grant for whole-field entry.")
                expected = self.texts[grant.text_id] if operation in {"append", "replace"} else ""
                self._dispatch_guard(original)
                self.discard()
                with wda.input_transaction():
                    if operation in {"replace", "clear"}:
                        self._check_binding(original)
                        dispatching = True
                        self._uses[offer.grant_index] += 1
                        wda.element_action(reference, "clear")
                        acknowledged += 1
                        dispatching = False
                        if operation == "replace" and wda.element_value(reference):
                            self.stopped = True
                            return StepResult("acknowledged", "unsatisfied", "clear_not_verified", acknowledged_substeps=acknowledged)
                    if operation != "clear":
                        self._check_binding(original)
                        dispatching = True
                        if operation == "append":
                            self._uses[offer.grant_index] += 1
                        wda.element_action(reference, "value", text=self.texts[grant.text_id])
                        acknowledged += 1
                        dispatching = False
                conditions = (Condition("value", grant.app, grant.target, expected),) + conditions
            else:
                if operation == "activate":
                    installed, _ = self.connection.ctl.list_apps(self.connection.device.identifier)
                    if grant.destination not in {app.bundle_identifier for app in installed}:
                        raise ObservationRejected("The selected destination app is not installed.")
                self._dispatch_guard(original)
                self.discard()
                dispatching = True
                self._uses[offer.grant_index] += 1
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
                           scoped_progress(scoped_items(original, old), scoped_items(current, current_target)))
                state = "satisfied" if current.app == grant.app and changed else "unsatisfied"
            else:
                state, current = self.wait(conditions, full=observe_next, references=references)
            if state != "satisfied":
                self.stopped = True  # An acknowledged action is never replayed on failed readback.
            return StepResult("acknowledged", state, "verified" if state == "satisfied" else "postcondition_not_verified", current, acknowledged)
        except VerificationExpired:
            # Only a completely acknowledged operation reaches postcondition
            # polling. Preserve its proof obligation, not a replayable offer.
            if acknowledged:
                self.pending = conditions
            return StepResult("acknowledged" if acknowledged else "not_sent", "unknown", "verification_expired",
                              acknowledged_substeps=acknowledged)
        except ObservationRejected as exc:
            if not acknowledged and not dispatching:
                self.discard()
                return StepResult("not_sent", "unknown", "validation_failed", error_type=type(exc).__name__)
            self.stopped = True
            return StepResult("acknowledged" if acknowledged else "not_sent", "unknown",
                              "verification_unavailable" if acknowledged else "validation_failed",
                              acknowledged_substeps=acknowledged, error_type=type(exc).__name__)
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
                 if e.visible is True and e.path.startswith(container.path + "/")
                 and (e.label or e.name and e.role not in {"XCUIElementTypeOther", "XCUIElementTypeScrollView"}))


def scoped_progress(before: tuple[object, ...], after: tuple[object, ...]) -> bool:
    # Compare visible content, not anonymous wrappers or bounce/rounding jitter.
    if len(before) != len(after):
        return True
    for old, new in zip(before, after):
        if old[:4] != new[:4]:
            return True
        if old[4] is not None and new[4] is not None and any(abs(a - b) > 4 for a, b in zip(old[4], new[4])):
            return True
    return False
