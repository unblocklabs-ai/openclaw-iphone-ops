"""Goal navigator: what a screen offers, what OpenAI's Decisions API (gpt-6-luna) is asked, and how its answers
become one step.

Questions, options and cut-offs live at the top for review. Validated on 155 labeled screens from a dedicated iPhone
plus 16 probe cases: 150/155 steps (28/31 held out), 16/16 probes, the tap check flagged 19/19 risky taps with 0/117
false alarms. Pure: `decide` takes an `ask(request) -> answers` callable and never touches the phone.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Callable

from .errors import OpenClawIPhoneError
from .observations import Element, Observation

THRESHOLDS = {
    "consent_prob": 0.5,      # blocked == permission_or_consent        -> escalate
    "done_score": 2.5,        # done: progress Score >= this ...
    "done2_min": 0.9,         # ... and the done2 predicate >= this (progress alone ran high on unfinished screens)
    "done_votes": 0.7,        # ... or next == done with P(done) and the done predicate both >= this
    "done_veto": 0.6,         # never done while next gives one element this much probability
    "act_confidence": 0.35,   # tap-group confidence below -> escalate  (coin-flip cases sat at ~0.30)
    "risky_noul": 0.35,       # tap check: risky predicate >= this -> needs approval
    "covered": 0.5,           # tap check: target covered by an overlay -> dismiss_overlay
    "overlay": 0.5,           # blocked is a menu/sheet or popup with this much probability ...
    "part": 0.5,              # ... and the target is not part of it -> dismiss_overlay
}
RISKY_EFFECTS = frozenset({"social_action", "communication", "purchase_install", "permission_grant"})
OVERLAYS = ("menu_or_sheet", "feature_popup")

NEXT = ("What should happen next to make progress on `goal`? Pick the on-screen element to tap, or one of the "
        "non-element options. The screenshot shows the current screen.")
NON_ELEMENT_OPTIONS = {
    "done": "The goal is already complete on this screen; nothing needs to be tapped.",
    "scroll_down": ("Scroll down (swipe up): reveals more of the page when the needed control is not visible, and moves to "
                    "the next item in a full-screen feed such as Reels, Stories or a video player."),
    "dismiss_overlay": "A popup, menu or sheet covers the needed control and must be closed first.",
    "none": "Nothing on this screen helps with the goal.",
    "type_text": "A text field is already focused (keyboard open) and the goal needs text typed into it now.",
}
DONE = "Does the screenshot show that `goal` is already complete?"
DONE2 = ("Is `goal` already satisfied by what this screen shows, so that no further tap or typing is needed?",
         "The requested screen, information or setting is already displayed or in place.",
         "Something still has to be tapped, typed, opened or changed.")
BLOCKED = "Is something covering the app's normal screen and waiting for a response?"
BLOCKED_OPTIONS = {
    "none": "Nothing is covering the screen.",
    "menu_or_sheet": "A menu, action sheet or popover opened by the user.",
    "feature_popup": "An app announcement or promotional popup.",
    "permission_or_consent": ("A request to grant a permission (location, contacts, notifications, tracking, camera) or to accept, "
                              "agree to or acknowledge terms of service, a privacy policy or data use."),
    "login_or_error": "A login wall, error or rate-limit message.",
}
PROGRESS = "How far along is `goal` on this screen?"
PROGRESS_LEVELS = [
    "This screen has nothing to do with the goal.",
    "The goal is related, but more navigation is needed before its target is visible.",
    "The control needed for the next step of the goal is visible on this screen.",
    "The goal is already accomplished: what it asks for is already shown or set on this screen, no tap needed.",
]
RISKY = ("Would tapping `element` by itself immediately cause a consequential or hard-to-undo effect: sending or posting "
         "something, following/unfollowing/muting someone, liking or saving content, buying or installing an app, starting "
         "a call, deleting something, granting a permission, or changing network connectivity?",
         "The tap itself performs such an action.",
         "The tap only opens, navigates, focuses, reveals options, or changes a harmless display setting.")
EFFECT = "What happens immediately when `element` is tapped?"
EFFECTS = {
    "navigate": "Opens or switches to another screen, tab, list, profile, post, chat thread or app; nothing is changed or sent.",
    "focus_input": "Puts the cursor in a text or search field.",
    "toggle_setting": "Turns a device or app setting on or off.",
    "social_action": "Follows, unfollows, mutes, restricts, likes, saves or reposts on a social account.",
    "communication": ("Sends a message, comment or post, or starts a voice or video call, at the moment of the tap. Opening a "
                      "chat or message screen without sending is navigate."),
    "purchase_install": "Buys, subscribes, downloads or installs something.",
    "permission_grant": "Grants an app access to location, contacts, camera, notifications or similar, or accepts terms.",
    "other": "Something else.",
}
COVERED = ("Is `element` hidden or blocked by something open on top of it, such as a popup, menu, action sheet, dialog or "
           "dimmed backdrop, so that tapping its spot would hit that overlay instead of `element`? An on-screen keyboard "
           "does not count.",
           "A popup, menu, sheet, dialog or backdrop covers or blocks `element`.",
           "`element` is visible and tappable, is part of the popup or menu itself, or nothing covers it.")
PART = ("Is `element` part of a popup, dialog, menu, action sheet or card that is open on top of the app?",
        "`element` is inside that popup, dialog, menu or sheet.",
        "`element` belongs to the app screen underneath, or nothing is open on top.")
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
    """The Decisions API gave no usable answers."""

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
    """Candidates the navigator may choose from: on-screen controls and text without scroll bars, keyboard keys,
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


def describe(e: Element) -> str:
    return line(e).split(" ", 1)[1]


def predicate(name: str, question: str | tuple[str, str, str]) -> dict:
    if isinstance(question, tuple):  # predicates take no criteria: true and false are spelled out in the text
        question = f"{question[0]}\nTrue: {question[1]}\nFalse: {question[2]}"
    return {"type": "predicate", "name": name, "instructions": question}


def choice(name: str, question: str, options: dict) -> dict:
    return {"type": "choice", "name": name, "instructions": question,
            "choices": [{"value": k, "description": v} for k, v in options.items()]}


def message(state: dict, image: str) -> list:
    return [{"role": "user", "content": [{"type": "input_text", "text": json.dumps(state, ensure_ascii=False, separators=(",", ":"))},
                                         {"type": "input_image", "image_url": image}]}]


def request(s: Screen, goal: str) -> dict:
    """One call, five independent questions; next, blocked, progress, done and done2 drive the decision.
    Each element option carries its own line, so the state holds no separate screen listing."""
    options = {element_key(e): describe(e) for e in s.rows} | NON_ELEMENT_OPTIONS
    return {
        "input": message({"goal": goal, "app": s.app, "notes": list(s.notes)}, s.image),
        "questions": [
            choice("next", NEXT, options),
            predicate("done", DONE),
            choice("blocked", BLOCKED, BLOCKED_OPTIONS),
            predicate("done2", DONE2),
            {"type": "score", "name": "progress", "instructions": PROGRESS,
             "levels": [{"label": str(i), "description": d} for i, d in enumerate(PROGRESS_LEVELS)]},
        ],
    }


def tap_check_request(s: Screen, goal: str, e: Element) -> dict:
    """One call per tap: is it risky, what does it do, and is an overlay in the way?"""
    return {
        "input": message({"goal": goal, "app": s.app, "element": line(e), "screen": "\n".join(map(line, s.rows))}, s.image),
        "questions": [predicate("risky", RISKY), choice("effect", EFFECT, EFFECTS),
                      predicate("covered", COVERED), predicate("part", PART)],
    }


def answers(reply: dict) -> dict:
    """Decisions reply -> {name: answer}: predicate {"probability"}, choice {"choice", "probabilities": {value: p}},
    score {"score"}. A refusal or an unexpected shape raises Unavailable."""
    out = {}
    try:
        for a in reply["answers"]:
            kind, name = a["type"], a["name"]
            if kind == "refusal":
                raise Unavailable("The model declined a question.", category="refusal")
            if kind == "predicate":
                out[name] = {"probability": float(a["probability"])}
            elif kind == "choice":
                out[name] = {"choice": a["choice"], "probabilities": {p["value"]: float(p["probability"]) for p in a["probabilities"]}}
            elif kind == "score":
                out[name] = {"score": float(a["score"])}
            else:
                raise KeyError(kind)
    except (KeyError, TypeError, ValueError):
        raise Unavailable("Unexpected Decisions reply.") from None
    return out


def decide(ask: Callable[[dict], dict], s: Screen, goal: str, *, check_tap: bool = True) -> dict:
    """One step: done, escalate, tap (maybe needing approval), type_text, scroll_down or dismiss_overlay."""
    t = THRESHOLDS
    a = ask(request(s, goal))
    n, b, progress = a["next"], a["blocked"], a["progress"]["score"]
    probabilities = n["probabilities"]
    top = sorted(probabilities.items(), key=lambda kv: -kv[1])[:3]
    out = {"signals": {"blocked": b["choice"], "progress": round(progress, 2), "done": round(a["done"]["probability"], 2),
                       "done2": round(a["done2"]["probability"], 2), "top": [[k, round(p, 3)] for k, p in top]}}
    if b["choice"] == "permission_or_consent" and b["probabilities"][b["choice"]] >= t["consent_prob"]:
        return out | {"action": "escalate", "reason": "permission_or_consent_prompt"}
    votes = n["choice"] == "done" and min(probabilities["done"], a["done"]["probability"]) >= t["done_votes"]
    agree = progress >= t["done_score"] and a["done2"]["probability"] >= t["done2_min"]
    element = max((p for k, p in probabilities.items() if k not in NON_ELEMENT_OPTIONS), default=0)
    if (agree or votes) and element < t["done_veto"]:
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
    if confidence < t["act_confidence"]:
        return decision | {"action": "escalate", "reason": "low_confidence"}
    if not check_tap:
        return decision
    try:
        r = ask(tap_check_request(s, goal, e))
        risky, effect = r["risky"]["probability"], r["effect"]["choice"]
        covered, part = r["covered"]["probability"], r["part"]["probability"]
    except (Unavailable, KeyError, TypeError):
        return decision | {"needs_approval": True, "risk": {"error": "risk_check_unavailable"}}
    decision["signals"] |= {"covered": round(covered, 2), "part": round(part, 2)}
    overlay = sum(b["probabilities"].get(k, 0) for k in OVERLAYS)
    if covered >= t["covered"] or overlay >= t["overlay"] and part < t["part"]:
        # The target sits under (or behind) an open overlay: close it first.
        return {k: v for k, v in decision.items() if k != "target"} | {"action": "dismiss_overlay"}
    return decision | {"needs_approval": risky >= t["risky_noul"] or effect in RISKY_EFFECTS,
                       "risk": {"risky": round(risky, 2), "effect": effect}}
