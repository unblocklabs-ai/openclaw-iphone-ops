"""Caller-directed Act inside the existing session, not an autonomous policy agent.

Only trusted local requests may create intents. Jev chooses an observed target;
it cannot supply input, coordinates, operations or additional authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import stat
import time
import unicodedata
import uuid

from .actions import Condition, Executor, scoped_items, scoped_progress
from .errors import OpenClawIPhoneError, VerificationExpired, WDAOutcomeUnknown, WDAUnavailable
from .evidence import artifact_path, write_private
from .jev import DecisionUnavailable, JevDriver, LowConfidenceDecision
from .observations import EDITABLE, SCROLLABLE, Element, Observation, ObservationRejected, Selector

SECURE = "XCUIElementTypeSecureTextField"
INPUTS = EDITABLE | {SECURE, "XCUIElementTypeOther"}
CONTROLS = INPUTS | SCROLLABLE | {"XCUIElementTypeButton", "XCUIElementTypeCell", "XCUIElementTypeLink",
                     "XCUIElementTypeStaticText"}


@dataclass(frozen=True)
class VisualEvidence:
    observation: Observation
    device_size: tuple[float, float]
    image_size: tuple[int, int]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


def normalized(value: str) -> str:
    return "".join(c for c in value if unicodedata.category(c) != "Cf").casefold().strip()


def parse_scope(value: object) -> dict:
    from .tasks import object_fields, text
    scope = object_fields(value, {"apps", "operations", "cloud_labels", "inputs"}, {"apps", "operations"})
    result = {}
    for key in ("apps", "operations", "cloud_labels"):
        values = scope.get(key, [])
        if not isinstance(values, list) or len(values) > 100 or key != "cloud_labels" and not values:
            raise ValueError("Adaptive scope requires bounded app/operation/approved-label lists.")
        result[key] = tuple(text(v) for v in values)
    if not set(result["operations"]) <= {"tap", "input", "keypad", "vision_tap", "relaunch", "scroll"}:
        raise ValueError("Unknown adaptive operation.")
    inputs = scope.get("inputs", {})
    if not isinstance(inputs, dict) or len(inputs) > 32:
        raise ValueError("Input references must be a bounded map of private file paths.")
    result["inputs"] = {text(k, maximum=64): text(v, maximum=2048) for k, v in inputs.items()}
    return result


def read_input(path: str) -> str:
    """Read locally, at dispatch time. Never log a path, secret or exception body."""
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Input must be an owner-only regular file owned by the current user.")
        raw = stream.read(16_385)
    value = raw.decode("utf-8").removesuffix("\n")
    if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Input must contain 1–4096 characters without control keys.")
    return value


class AdaptiveAct:
    def __init__(self, executor: Executor, scope: dict, *, driver: JevDriver | None = None,
                 max_decisions: int = 12, evidence_base: str | None = None) -> None:
        self.ex, self.scope, self.driver = executor, scope, driver
        self.max_decisions, self.decisions = max_decisions, 0
        self.evidence_base = evidence_base
        self.visual: VisualEvidence | None = None
        self.secrets: set[str] = set()
        self.pending_input = None
        self.pending_target = None

    def view(self, observation: Observation, *, labels: bool = False) -> dict:
        # Local opt-in labels are separate from the explicit cloud projection.
        result = observation.compact(limit=200)
        rows = {e.id: e for e in observation.elements or ()}
        # Omit structural wrappers and keyboard keys, not useful controls late
        # in the tree. This is a display projection; targeting uses the full tree.
        relevant = [e for e in observation.elements or () if e.visible is True and
                    e.role in CONTROLS and (e.name or e.label or e.role == SECURE or e.role in SCROLLABLE)]
        result["elements"] = [{"id": e.id, "role": e.role, "enabled": e.enabled,
                                "actionable": e.actionable, "named": bool(e.name or e.label)} for e in relevant[:80]]
        result["omitted_relevant"] = max(0, len(relevant) - 80)
        result["omitted_visible"] = result["counts"]["visible"] - len(result["elements"])
        def safe_label(value: str | None) -> str:
            value = value or ""
            for secret in self.secrets:
                value = value.replace(secret, "[private input]")
            return value[:256]

        for row in result["elements"]:
            element = rows[row["id"]]
            row["bounds"] = element.bounds
            if element.role in EDITABLE:
                # A targeted native recheck may have superseded stale XML for
                # this same snapshot. Never expose the value in the view.
                value = element.value
                if observation is self.ex.latest:
                    for condition, state in self.ex._verified.items():
                        if (state in {"satisfied", "unsatisfied"} and condition.kind == "value" and
                                condition.app == observation.app and element.matches(condition.target) and
                                condition in self.ex._values):
                            value = self.ex._values[condition]
                            break
                # A missing value or a possible placeholder is not evidence of emptiness.
                row["input_state"] = ("unknown" if value is None or value != "" and
                    value in {element.name, element.label} else "empty" if value == "" else "nonempty")
                row["focused"] = element.focused
            if labels and element.role != SECURE:
                row.update(name=safe_label(element.name), label=safe_label(element.label),
                           ancestors=[{"role": role, "name": safe_label(name), "label": safe_label(label)}
                                      for role, name, label in element.ancestors
                                      if role != "XCUIElementTypeApplication" and (name or label)][-2:])
        result.update(labels_included=labels, input_stopped=self.ex.stopped,
                      input_pending=self.pending_input is not None)
        return result

    def _current(self, *, full: bool = True) -> Observation:
        observation = self.ex.current(full=full)
        if observation.app not in self.scope["apps"] or (full and observation.elements is None):
            raise ObservationRejected("Adaptive action is outside the observed app scope.")
        return observation

    def _target(self, data: dict, observation: Observation):
        from .tasks import selector
        action = data["action"]
        roles = INPUTS if action in {"input", "keypad"} else SCROLLABLE if action == "scroll" else CONTROLS - SCROLLABLE
        candidates = [e for e in observation.elements if e.actionable and e.role in roles]
        if "target_id" in data:
            if data.get("snapshot_id") != observation.id:
                raise ObservationRejected("Target ID belongs to a different snapshot.")
            exact = [e for e in candidates if e.id == data["target_id"]]
        elif "target" in data:
            target = selector(data["target"])
            exact = [e for e in candidates if e.matches(target)]
        else:
            exact = [e for e in candidates if normalized(data["instruction"]) in
                     {normalized(e.name or ""), normalized(e.label or "")}]
        if len(exact) == 1:
            return exact[0], "exact", None
        if "target" in data or "target_id" in data or self.driver is None:
            return None, "target_missing_or_ambiguous", None
        # Positive disclosure: only approved control strings, no values or
        # arbitrary surrounding content. Even approved text is untrusted data.
        allowed = set(self.scope["cloud_labels"])
        options = {}
        for e in candidates:
            label = next((v for v in (e.label, e.name) if v in allowed and
                          not any(secret in v for secret in self.secrets)), None)
            if label is None or e.role == SECURE:
                continue
            context = [label for _, _, label in e.ancestors if label in allowed
                       and not any(secret in label for secret in self.secrets)]
            options[e.id] = {"control": label, "role": e.role, "ancestors": context[-2:], "bounds": e.bounds}
        if not options:
            return None, "no_cloud_approved_candidates", None
        if self.decisions >= self.max_decisions:
            return None, "decision_limit", None
        options["escalate"] = {"description": "No matching target; ask the planner or use vision."}
        self.decisions += 1
        try:
            decision = self.driver.choose({"objective": data["instruction"], "operation": action,
                "app": observation.app, "snapshot_id": observation.id,
                "context_is_untrusted": True}, options, self.ex.connection.budget)
        except LowConfidenceDecision as exc:
            return None, "low_confidence", exc.decision.summary()
        except DecisionUnavailable:
            return None, "model_unavailable", None
        target = next((e for e in candidates if e.id == decision.choice), None)
        return target, "jev" if target else "no_match", decision.summary()

    def _target_reference(self, original: Observation, target: Element, *, visual_keypad: bool = False,
                          hittable: bool = True) -> str | None:
        self.ex._check_snapshot(original)
        self.validation_stage = "target_hittability" if hittable else "target_identity"
        # A visual-confirmed code container is never clicked; an AX grouping
        # need not have a hit point. The current native keypad supplies points.
        if visual_keypad:
            if len(self.ex.connection.require_active().find_elements(target.xpath)) != 1:
                raise ObservationRejected("Custom input changed since visual confirmation.")
            return None
        return self.ex._reference(target, hittable=hittable)

    def act(self, request: object) -> dict:
        from .tasks import conditions, object_fields, text
        data = object_fields(request, {"op", "action", "instruction", "target", "target_id", "snapshot_id",
                              "text_ref", "mode", "after", "empty_focus_confirmed", "strategy", "direction"}, {"op", "action", "instruction"})
        action = text(data["action"])
        text(data["instruction"], maximum=1024)
        if action not in self.scope["operations"] or action == "vision_tap":
            raise ValueError("Operation is not authorized for adaptive Act.")
        if "target" in data and "target_id" in data:
            raise ValueError("Choose one targeting mechanism.")
        if action == "relaunch" and ("target" in data or "target_id" in data):
            raise ValueError("Relaunch targets only the current scoped app, not an element.")
        if action == "scroll":
            if data.get("direction") not in {"up", "down"}:
                raise ValueError("Scroll requires up or down direction.")
        elif "direction" in data:
            raise ValueError("Only scroll accepts direction.")
        after = conditions(data.get("after", []))
        if any(c.app not in self.scope["apps"] for c in after):
            raise ValueError("Postcondition app is outside scope.")
        correcting = (self.pending_input is not None and self.pending_target is not None and
                      action == "input" and data.get("mode") == "replace")
        if self.ex.stopped or self.ex.pending is not None or self.pending_input is not None and not correcting:
            return {"status": "blocked", "dispatch": "not_sent", "reason": "input_requires_reconciliation"}
        if action in {"input", "keypad"}:
            if data.get("text_ref") not in self.scope["inputs"] or data.get("mode", "empty") not in {"empty", "replace"}:
                raise ValueError("Input requires an approved local reference and empty/replace mode.")
            if data.get("strategy", "native") not in {"native", "sequential"} or action == "keypad" and "strategy" in data:
                raise ValueError("Input strategy must be native or sequential; keypad is already a strategy.")
        elif any(key in data for key in ("text_ref", "mode", "empty_focus_confirmed", "strategy")):
            raise ValueError("Only input operations accept local text.")
        started = time.monotonic()
        visual, self.visual = self.visual, None
        self.validation_stage = "snapshot"
        acknowledged = 0
        current = None
        resolution, decision = "caller", None
        try:
            original = current = self._current(full=action != "relaunch")
            if action == "relaunch" and "snapshot_id" in data and data["snapshot_id"] != original.id:
                raise ObservationRejected("Relaunch snapshot was superseded; reobserve and choose again.")
            target = None
            if action != "relaunch":
                target, resolution, decision = self._target(data, original)
                if target is None:
                    return {"status": "fallback", "dispatch": "not_sent", "reason": resolution, "decision": decision}
                self.validation_stage = "target_identity"
                visual_keypad = (action == "keypad" and target.role == "XCUIElementTypeOther" and
                                 visual is not None and data.get("empty_focus_confirmed") == visual.id
                                 and original is visual.observation)
                reference = self._target_reference(original, target, visual_keypad=visual_keypad, hittable=action == "tap")
            wda = self.ex.connection.require_active()
            if action in {"input", "keypad"}:
                identity = (current.app, target.role, target.name, target.label)
                if correcting and identity != self.pending_target:
                    raise ObservationRejected("Only explicit replacement of the same readable input can reconcile it.")
                supplied = read_input(self.scope["inputs"][data["text_ref"]])
                self.secrets.add(supplied)
                self.validation_stage = "visual_input_confirmation"
                visual_input = (target.role == "XCUIElementTypeOther" and visual is not None
                                and data.get("empty_focus_confirmed") == visual.id and original is visual.observation
                                and data.get("mode", "empty") == "empty")
                # The native query revalidates the exact field and geometry.
                # The dispatch guard checks app/freshness. Unrelated countdown
                # labels do not prove this field changed. Input is one operation,
                # not an observation/decision/verification cycle per character.
                if "empty_focus_confirmed" in data and not visual_input:
                    raise ObservationRejected("Custom-input visual confirmation is stale or incompatible.")
                if target.role not in EDITABLE and not any(c.kind != "app" for c in after):
                    raise ValueError("Secure/custom input needs an independently observable destination.")
                if after and self.ex.verify(current, after) == "satisfied":
                    raise ObservationRejected("Input destination already satisfied; no input dispatched.")
                if action == "keypad" and (len(supplied) > 32 or any(c not in "0123456789" for c in supplied)):
                    raise ValueError("Keypad input requires 1–32 digits.")
                # Construct every fallible predicate before the first mutation.
                input_conditions = after or ((Condition("value", current.app,
                    Selector(target.role, name=target.name or None, label=target.label or None), supplied),)
                    if target.role in EDITABLE else ())
                # Custom/secure empty evidence is supplied by a just-observed
                # empty native value, not missing AX/OCR. For non-readable
                # fields use explicit replace; clear acknowledgement alone is
                # not treated as proof of emptiness.
                replacing = data.get("mode", "empty") == "replace"
                self.pending_input = input_conditions
                self.pending_target = identity if target.role in EDITABLE and not after else None
                self.ex._dispatch_guard(current)
                self.ex.discard()
                with wda.input_transaction():
                    if replacing:
                        self.ex._check_binding(current)
                        wda.element_action(reference, "clear")
                        acknowledged += 1
                    self.validation_stage = "post_clear" if acknowledged else "input_empty"
                    if not visual_input:
                        value = wda.element_value(reference, allow_null_empty=target.role in EDITABLE)
                        # Empty fields may expose their placeholder after clear.
                        if value and not (replacing and value == wda.element_placeholder(reference)):
                            raise ObservationRejected("Input is not verified empty; authorize replacement if appropriate.")
                        if action == "keypad" or data.get("strategy") == "sequential":
                            if not acknowledged:
                                self.validation_stage = "input_focus"
                            if wda.active_element() != reference:
                                raise ObservationRejected("Focus changed after empty verification.")
                    if action == "keypad":
                        if not acknowledged:
                            self.validation_stage = "keypad_layout"
                        points = self.ex._keypad_points(current, supplied)
                        self.ex._check_binding(current)
                        wda.tap_sequence(points)
                    else:
                        self.ex._check_binding(current)
                        if data.get("strategy", "native") == "sequential":
                            wda.type_text(supplied, frequency=8)
                        else:
                            # Targeted WDA typing does not guarantee keyboard focus.
                            wda.element_action(reference, "value", text=supplied)
                    acknowledged += 1
            else:
                self.ex._dispatch_guard(current, pixels=action == "relaunch")
                self.ex.discard()
                if action == "tap":
                    wda.element_action(reference, "click")
                    acknowledged += 1
                elif action == "scroll":
                    wda.element_scroll(reference, data["direction"])
                    acknowledged += 1
                elif action == "relaunch":
                    wda.terminate_app(current.app)
                    acknowledged += 1
                    self.validation_stage = "post_terminate_activation"
                    wda.activate_app(current.app)
                    acknowledged += 1
            self.validation_stage = "post_action_verification"
            if action == "scroll":
                current = self.ex.observe()
                same = [e for e in current.elements or () if e.path == target.path and e.role == target.role
                        and e.name == target.name and e.label == target.label and e.ancestors == target.ancestors]
                if current.app != original.app or len(same) != 1:
                    state = "unknown"
                else:
                    changed = scoped_progress(scoped_items(original, target), scoped_items(current, same[0]))
                    state = self.ex.verify(current, after) if after else "satisfied" if changed else "unsatisfied"
            elif action in {"input", "keypad"}:
                references = {input_conditions[0].target: reference} if not after and reference and input_conditions else None
                # A normal field's post-input tree is also the next decision's
                # evidence. Verify its value there, rather than reading a narrow
                # value now and immediately fetching the same screen afterward.
                full = not after and target.role in EDITABLE
                state, current = self.ex.wait(input_conditions, once=not after, full=full,
                                              references=references, native_mismatch=action == "input" and full)
                if state == "unknown" or current.app != input_conditions[0].app:
                    self.pending_target = None
                elif full and state == "unsatisfied" and current.unique(input_conditions[0].target) is None:
                    self.pending_target = None
            elif after:
                state, current = self.ex.wait(after)
            else:
                current = self.ex.observe(app_only=action == "relaunch")
                state = "unknown"
            if action in {"input", "keypad"} and state == "satisfied":
                self.pending_input = None
                self.pending_target = None
            reason = ("no_progress" if action == "scroll" and state == "unsatisfied" and not changed
                      else "verified" if state == "satisfied" else "inspect_result")
            result = {"status": "step", "dispatch": "acknowledged", "verification": state, "reason": reason}
            if state != "satisfied":
                result["validation_stage"] = "post_action_verification"
            if action == "input" and not after and state in {"satisfied", "unsatisfied"}:
                readback = self.ex._values.get(input_conditions[0])
                result["readback"] = {"matches": state == "satisfied", "expected_characters": len(supplied),
                                      "observed_characters": len(readback) if readback is not None else None}
        except WDAOutcomeUnknown:
            self.ex.stopped = True
            self.ex.connection.invalidate(uncertain=True)
            result = {"status": "blocked", "dispatch": "unknown", "verification": "unknown", "reason": "mutation_outcome_unknown"}
            current = None
        except VerificationExpired:
            if acknowledged:
                self.pending_target = None  # A missing readback is not a known mismatch to replace.
            result = {"status": "fallback", "dispatch": "acknowledged" if acknowledged else "not_sent",
                      "verification": "unknown", "reason": "verification_expired"}
            if acknowledged:
                result["validation_stage"] = self.validation_stage
            current = None
        except OpenClawIPhoneError as exc:
            if isinstance(exc, WDAUnavailable):
                self.ex.connection.invalidate()
            if not acknowledged and not correcting:
                self.pending_input = None
                self.pending_target = None
            elif acknowledged:
                self.pending_target = None
            result = {"status": "fallback", "dispatch": "acknowledged" if acknowledged else "not_sent",
                      "verification": "unknown", "reason": "verification_unavailable" if acknowledged else "validation_failed",
                      "error_type": type(exc).__name__, "validation_stage": self.validation_stage}
            current = None
        if action == "relaunch" and acknowledged == 1 and result["dispatch"] == "acknowledged":
            self.ex.stopped = True
            result.update(status="blocked", reason="relaunch_partial")
        result.update(resolution=resolution, decision=decision, seconds=time.monotonic() - started,
                      acknowledged_substeps=acknowledged, input_stopped=self.ex.stopped,
                      input_pending=self.pending_input is not None)
        if current is not None:
            result["observation"] = current
        return result

    def reconcile(self) -> dict:
        """Read only. Only the original destination may release pending input."""
        state, observation = self.ex.wait(self.pending_input or self.ex.pending or (), once=True)
        if state == "satisfied" and not self.ex.stopped:
            self.pending_input = None
            self.pending_target = None
        return {"status": "observed", "verification": state, "observation": observation}

    def screenshot(self, *, redact: object = None) -> dict:
        from .image_evidence import redact_png
        self.visual = None
        previous = self.ex.latest
        observation = self.ex.current(full=False)
        if observation.app not in self.scope["apps"]:
            raise ObservationRejected("Screenshot is outside the authorized app scope.")
        if redact is None:
            if observation.elements is None:
                raise ObservationRejected("Screenshot-only capture requires explicit redaction rectangles, or [] for a caller-confirmed non-sensitive screen.")
            bounds = [e.bounds for e in observation.elements if e.bounds and
                      (e.role in EDITABLE | {SECURE} or any(secret in (e.name or "") or secret in (e.label or "")
                                              for secret in self.secrets))]
        else:
            if (not isinstance(redact, list) or len(redact) > 100 or
                    any(not isinstance(b, list) or len(b) != 4 or
                        any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in b)
                        or min(b[2:]) <= 0 for b in redact)):
                raise ValueError("Redaction requires bounded [x,y,width,height] rectangles in device points.")
            bounds = redact
        wda = self.ex.connection.require_active()
        size = wda.window_size()
        raw = wda.screenshot()
        if observation is previous:
            self.ex._guard_app(observation.app, observation.process_id)
        raw, pixels = redact_png(raw, size, bounds)
        self.ex._check_snapshot(observation)
        path = artifact_path("adaptive-screen", suffix=".png", base=self.evidence_base)
        write_private(path, raw)
        self.visual = VisualEvidence(observation, size, pixels)
        return {"status": "screenshot", "path": str(path), "snapshot_id": self.visual.id,
                "image_size": pixels, "device_size": size, "redacted_regions": len(bounds)}

    def vision_tap(self, request: object) -> dict:
        from .tasks import object_fields
        data = object_fields(request, {"op", "snapshot_id", "x", "y"}, {"op", "snapshot_id", "x", "y"})
        if ("vision_tap" not in self.scope["operations"] or self.ex.stopped
                or self.ex.pending is not None or self.pending_input is not None):
            raise ObservationRejected("Vision input is not authorized/available.")
        visual, self.visual = self.visual, None
        if visual is None or data["snapshot_id"] != visual.id:
            raise ObservationRejected("No matching unconsumed screenshot.")
        old, size, pixels = visual.observation, visual.device_size, visual.image_size
        if any(type(data[k]) not in (float, int) or not math.isfinite(data[k]) or not 0 <= data[k] < end
               for k, end in zip(("x", "y"), pixels)):
            raise ValueError("Coordinates must be inside the captured image.")
        self.ex._check_snapshot(old)
        wda = self.ex.connection.require_active()
        if wda.window_size() != size:
            raise ObservationRejected("Screen geometry changed since screenshot; capture again.")
        self.ex._dispatch_guard(old, pixels=True)
        self.ex.discard()
        try:
            wda.tap(data["x"] * size[0] / pixels[0], data["y"] * size[1] / pixels[1])
        except WDAOutcomeUnknown:
            self.ex.stopped = True
            self.ex.connection.invalidate(uncertain=True)
            return {"status": "blocked", "dispatch": "unknown", "reason": "mutation_outcome_unknown"}
        # A visual click acknowledgement is not proof of its effect. Let the
        # next explicit observe/screenshot choose AX or vision; never force AX.
        return {"status": "step", "dispatch": "acknowledged", "verification": "unknown", "reason": "inspect_result"}
