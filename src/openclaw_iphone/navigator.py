"""Clef goal navigator: what a screen offers, what Clef is asked, and how its answers become one step.

Questions, options and cut-offs live at the top for review. They were validated on 155 labeled
screens (98.7% top-1; the safety gate flagged 14/14 risky taps with 0/94 false alarms).
Pure: `decide` takes an `ask(body) -> result` callable and never touches the phone.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Callable

from .errors import OpenClawIPhoneError
from .observations import Element, Observation

THRESHOLDS = {
    "done_score": 2.5,        # progress Score >= this -> done          (tuning: done >= 2.55, non-done <= 2.45)
    "done_votes": 0.7,        # ...or next == done and P(done), done Noul both >= this (live 2.50 tie; 0/155 label changes)
    "consent_prob": 0.5,      # blocked == permission_or_consent        (consent screens scored 0.94-0.98)
    "act_confidence": 0.35,   # tap-group confidence below -> escalate  (coin-flip cases sat at ~0.30)
    "risky_noul": 0.35,       # safety gate Noul                        (risky >= 0.47 except one, safe <= 0.26)
}
RISKY_EFFECTS = frozenset({"social_action", "communication", "purchase_install", "permission_grant"})

NON_ELEMENT_OPTIONS = {
    "done": "The goal is already complete on this screen; nothing needs to be tapped.",
    "scroll_down": "The needed control is not visible or is hidden; scroll to find it.",
    "dismiss_overlay": "A popup, menu or sheet covers the needed control and must be closed first.",
    "none": "Nothing on this screen helps with the goal.",
    "type_text": "A text field is already focused (keyboard open) and the goal needs text typed into it now.",
}
PROGRESS_LEVELS = [
    "This screen has nothing to do with the goal.",
    "The goal is related, but more navigation is needed before its target is visible.",
    "The control needed for the next step of the goal is visible on this screen.",
    "The goal is already accomplished: what it asks for is already shown or set on this screen, no tap needed.",
]
BLOCKED_OPTIONS = {
    "none": "Nothing is covering the screen.",
    "menu_or_sheet": "A menu, action sheet or popover opened by the user.",
    "feature_popup": "An app announcement or promotional popup.",
    "permission_or_consent": ("A request to grant a permission (location, contacts, notifications, tracking, camera) or to accept, "
                              "agree to or acknowledge terms of service, a privacy policy or data use."),
    "login_or_error": "A login wall, error or rate-limit message.",
}
EFFECTS = {
    "navigate": "Opens or switches to another screen, tab, list, profile, post or app; nothing is changed or sent.",
    "focus_input": "Puts the cursor in a text or search field.",
    "toggle_setting": "Turns a device or app setting on or off.",
    "social_action": "Follows, unfollows, mutes, restricts, likes, saves or reposts on a social account.",
    "communication": "Sends a message or starts a voice or video call.",
    "purchase_install": "Buys, subscribes, downloads or installs something.",
    "permission_grant": "Grants an app access to location, contacts, camera, notifications or similar, or accepts terms.",
    "other": "Something else.",
}
RISKY_INSTRUCTIONS = ("Would tapping `element` by itself immediately cause a consequential or hard-to-undo effect: sending or "
                      "posting something, following/unfollowing/muting someone, liking or saving content, buying or installing an "
                      "app, starting a call, deleting something, granting a permission, or changing network connectivity?")
DISMISS_GOAL = ("Close the popup, menu or sheet that covers the screen without accepting terms, granting permissions, "
                "following, buying or sending anything")

SCROLLBAR = re.compile(r"^(Vertical|Horizontal) scroll bar")
KEYBOARD = "XCUIElementTypeKeyboard"
CONTAINERS = frozenset({"XCUIElementTypeOther", "XCUIElementTypeCollectionView", "XCUIElementTypeScrollView",
                        "XCUIElementTypeTable", "XCUIElementTypeWebView", "XCUIElementTypeGroup", "XCUIElementTypeCell",
                        "XCUIElementTypeNavigationBar", "XCUIElementTypeTabBar", "XCUIElementTypeToolbar", KEYBOARD,
                        "XCUIElementTypeWindow", "XCUIElementTypeApplication"})
INTERACTIVE = frozenset({"XCUIElementTypeButton", "XCUIElementTypeCell", "XCUIElementTypeLink", "XCUIElementTypeKey",
                         "XCUIElementTypeIcon", "XCUIElementTypeSwitch", "XCUIElementTypeTextField",
                         "XCUIElementTypeSearchField", "XCUIElementTypeTextView", "XCUIElementTypeSecureTextField",
                         "XCUIElementTypeSlider", "XCUIElementTypeTab", "XCUIElementTypeSegmentedControl"})
COVERS = frozenset({"XCUIElementTypeTabBar", KEYBOARD})
STATUS_BAR = 40  # points; controls centered above this sit under the status bar (except on the home screen)


class Unavailable(OpenClawIPhoneError):
    """Clef gave no usable answers."""

    def __init__(self, message: str, *, category: str = "bad_reply") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Screen:
    app: str
    rows: tuple[Element, ...]
    notes: tuple[str, ...]
    image: str  # data URL


def element_key(e: Element) -> str:
    return "e" + e.id.rsplit(":", 1)[1]


def order(e: Element) -> int:
    return int(e.id.rsplit(":", 1)[1])


def center(e: Element) -> tuple[float, float]:
    x, y, w, h = e.bounds
    return x + w / 2, y + h / 2


def inside(point: tuple[float, float], e: Element) -> bool:
    x, y, w, h = e.bounds
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def contains(ancestor: Element, e: Element) -> bool:
    return e.path.startswith(ancestor.path + "/")


def screen(obs: Observation, image: str) -> Screen:
    """Candidates Clef may choose from: on-screen controls and text without scroll bars, keyboard keys,
    empty containers, controls under the tab bar/keyboard/status bar, or text repeating its control's label."""
    fx, fy, fw, fh = obs.elements[0].bounds  # the application frame
    def shown(e: Element) -> bool:
        if e.bounds is None:
            return False
        x, y, w, h = e.bounds
        return x < fx + fw and fx < x + w and y < fy + fh and fy < y + h and w * h > 1
    obs = replace(obs, elements=tuple(replace(e, visible=shown(e)) for e in obs.elements))
    by_id = {e.id: e for e in obs.elements}
    rows, keyboard = [], False
    for e, parent_id in obs.projection:
        parent = by_id.get(parent_id)
        if SCROLLBAR.match(e.label or "") or parent is not None and SCROLLBAR.match(parent.label or ""):
            continue
        if e.role == KEYBOARD or any(role == KEYBOARD for role, _, _ in e.ancestors):
            keyboard = True
            continue
        if e.role in CONTAINERS and not (e.name or e.label):
            continue
        rows.append(e)
    covers = [c for c in obs.elements if c.bounds and (c.role in COVERS or c.name == "tab-bar-container")]
    def covered(e: Element) -> bool:
        point = center(e)
        return (point[1] < STATUS_BAR and obs.app != "com.apple.springboard" or any(
            inside(point, c) and order(c) > order(e) and not contains(c, e) and not contains(e, c) for c in covers))
    rows = [e for e in rows if not covered(e)]
    controls = [e for e in rows if e.role in INTERACTIVE]
    def repeats_control(e: Element) -> bool:
        owners = [a for a in controls if contains(a, e) and inside(center(e), a)]
        if e.role in INTERACTIVE or not owners:
            return False
        owner = max(owners, key=lambda a: len(a.path))
        return not e.label or e.label.casefold() in (owner.label or "").casefold()
    rows = [e for e in rows if not repeats_control(e)]
    notes = ("An on-screen keyboard is open (individual keys omitted).",) if keyboard else ()
    return Screen(obs.app, tuple(rows), notes, image)


def line(e: Element) -> str:
    parts = [element_key(e), e.role.removeprefix("XCUIElementType")]
    if e.label:
        parts.append(json.dumps(e.label[:120], ensure_ascii=False))
    if e.name and e.name != e.label and not re.fullmatch(r"[0-9A-F-]{36}", e.name):
        parts.append(f"id={e.name[:60]}")
    if e.role in {"XCUIElementTypeSwitch", "XCUIElementTypeToggle"} and e.value in {"0", "1"}:
        parts.append("on" if e.value == "1" else "off")
    if e.selected:
        parts.append("selected")
    if e.enabled is False:
        parts.append("disabled")
    if e.bounds:
        x, y, w, h = e.bounds
        parts.append(f"@({x + w / 2:.0f},{y + h / 2:.0f}) {w:.0f}x{h:.0f}")
    context = next((label for role, _, label in reversed(e.ancestors) if label and not SCROLLBAR.match(label)
                    and role not in {"XCUIElementTypeApplication", "XCUIElementTypeWindow"}), None)
    if context and context != e.label:
        parts.append(f"in {json.dumps(context[:60], ensure_ascii=False)}")
    return " ".join(parts)


def request(s: Screen, goal: str) -> dict:
    """One call, five independent questions; next, blocked, progress and done drive the decision."""
    criteria = {element_key(e): None for e in s.rows}
    criteria.update({k: NON_ELEMENT_OPTIONS[k] for k in ("done", "scroll_down", "dismiss_overlay", "none", "type_text")})
    return {
        "state": {"goal": goal, "app": s.app, "screen": "\n".join(map(line, s.rows)), "notes": list(s.notes)},
        "images": [s.image],
        "questions": {
            "next": {"type": "choice", "criteria": criteria,
                     "instructions": "What should happen next to make progress on `goal`? Pick the element in `screen` to tap, "
                                     "or one of the non-element options. The screenshot shows the current screen."},
            "done": {"type": "noul", "instructions": "Does the screenshot show that `goal` is already complete?"},
            "blocked": {"type": "choice", "criteria": BLOCKED_OPTIONS,
                        "instructions": "Is something covering the app's normal screen and waiting for a response?"},
            "done2": {"type": "noul",
                      "instructions": "Is `goal` already satisfied by what this screen shows, so that no further tap or typing is needed?",
                      "criteria": {"true": "The requested screen, information or setting is already displayed or in place.",
                                   "false": "Something still has to be tapped, typed, opened or changed."}},
            "progress": {"type": "score", "criteria": PROGRESS_LEVELS, "instructions": "How far along is `goal` on this screen?"},
        },
    }


def risk_request(s: Screen, goal: str, e: Element) -> dict:
    return {
        "state": {"goal": goal, "app": s.app, "element": line(e), "screen": "\n".join(map(line, s.rows))},
        "images": [s.image],
        "questions": {
            "risky": {"type": "noul", "instructions": RISKY_INSTRUCTIONS,
                      "criteria": {"true": "The tap itself performs such an action.",
                                   "false": "The tap only opens, navigates, focuses, reveals options, or changes a harmless display setting."}},
            "effect": {"type": "choice", "criteria": EFFECTS, "instructions": "What happens immediately when `element` is tapped?"},
        },
    }


def decide(ask: Callable[[dict], dict], s: Screen, goal: str, *, check_risk: bool = True) -> dict:
    """One step: done, escalate, tap (maybe needing approval), type_text, scroll_down or dismiss_overlay."""
    a = ask(request(s, goal))["answers"]
    n, b, progress = a["next"], a["blocked"], a["progress"]["score"]
    probabilities = n["probabilities"]
    top = sorted(probabilities.items(), key=lambda kv: -kv[1])[:3]
    out = {"signals": {"blocked": b["choice"], "progress": round(progress, 2), "done": round(a["done"]["noul"], 2),
                       "top": [[k, round(p, 3)] for k, p in top]}}
    if b["choice"] == "permission_or_consent" and b["probabilities"][b["choice"]] >= THRESHOLDS["consent_prob"]:
        return out | {"action": "escalate", "reason": "permission_or_consent_prompt"}
    votes = n["choice"] == "done" and min(probabilities["done"], a["done"]["noul"]) >= THRESHOLDS["done_votes"]
    if progress >= THRESHOLDS["done_score"] or votes:
        return out | {"action": "done", "confidence": round(progress / 3, 2)}
    choice = n["choice"] if n["choice"] != "done" else max((k for k in probabilities if k != "done"), key=probabilities.get)
    if choice == "none":
        return out | {"action": "escalate", "reason": "nothing_on_screen_helps"}
    if choice in NON_ELEMENT_OPTIONS:
        return out | {"action": choice, "confidence": round(probabilities[choice], 2)}
    by_key = {element_key(o): o for o in s.rows}
    e = by_key[choice]
    # Options whose centers fall inside the chosen element (or vice versa) land the same tap.
    confidence = sum(p for k, p in probabilities.items()
                     if k in by_key and (inside(center(by_key[k]), e) or inside(center(e), by_key[k])))
    x, y = center(e)
    decision = out | {"action": "tap", "confidence": round(confidence, 2),
                      "target": {"id": e.id, "key": choice, "role": e.role.removeprefix("XCUIElementType"),
                                 "label": e.label or e.name, "x": round(x), "y": round(y)}}
    if confidence < THRESHOLDS["act_confidence"]:
        return decision | {"action": "escalate", "reason": "low_confidence"}
    if not check_risk:
        return decision
    try:
        r = ask(risk_request(s, goal, e))["answers"]
        risky, effect = r["risky"]["noul"], r["effect"]["choice"]
    except (Unavailable, KeyError, TypeError):
        return decision | {"needs_approval": True, "risk": {"error": "risk_check_unavailable"}}
    return decision | {"needs_approval": risky >= THRESHOLDS["risky_noul"] or effect in RISKY_EFFECTS,
                       "risk": {"risky": round(risky, 2), "effect": effect}}
