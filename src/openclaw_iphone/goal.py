"""Optional goal navigation: OpenAI's Decisions API (gpt-6-luna) picks each step, the session performs it. Off unless
configured.

Each step's screenshot and on-screen text go to OpenAI. Screens with a secure (password) field are never sent.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.request

from . import navigator
from .config import IPhoneConfig
from .errors import OpenClawIPhoneError, diagnostic
from .inputs import validate_text

OPENAI_API = "https://api.openai.com/v1"
MODEL = "gpt-6-luna"
RETRIES = 2
DEFAULT_STEPS, MAX_STEPS = 14, 30
TYPED = "typed the supplied text"


class Decisions:
    """POST /v1/decisions: typed answers to the navigator's questions."""

    def __init__(self, api_key: str, *, base_url: str = OPENAI_API, timeout: float = 30) -> None:
        self.url = f"{base_url.rstrip('/')}/decisions"
        self.api_key, self.timeout = api_key, timeout

    def ask(self, body: dict) -> dict:
        data = json.dumps(dict(body, model=MODEL)).encode()
        for attempt in range(RETRIES + 1):  # asking has no side effects: rate limits and server errors are retried
            request = urllib.request.Request(self.url, data=data, method="POST",
                                             headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return navigator.answers(json.load(response))
            except urllib.error.HTTPError as exc:
                exc.close()
                if (exc.code == 429 or exc.code >= 500) and attempt < RETRIES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise navigator.Unavailable("The Decisions API refused the request.", category=f"http_{exc.code}") from None
            except OSError as exc:
                raise navigator.Unavailable("The Decisions API is unreachable.", category="unreachable") from exc
            except ValueError as exc:
                raise navigator.Unavailable("The Decisions API reply is not JSON.") from exc


def goal_navigation(config: IPhoneConfig) -> str:
    """off, on, or incomplete (enabled without an OpenAI API key)."""
    if (config.get("OPENCLAW_IPHONE_GOAL_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return "off"
    return "on" if config.get("OPENCLAW_IPHONE_OPENAI_API_KEY") else "incomplete"


def decisions_from_config(config: IPhoneConfig) -> Decisions | None:
    if goal_navigation(config) != "on":
        return None
    return Decisions(config.get("OPENCLAW_IPHONE_OPENAI_API_KEY"),
                     base_url=config.get("OPENCLAW_IPHONE_OPENAI_BASE_URL") or OPENAI_API)


def jpeg(png: bytes) -> str:
    """The validated request image as a data URL: JPEG quality 80, longest side at most 1024 px."""
    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Invalid PNG.")
    resize = ["-Z", "1024"] if max(struct.unpack_from(">II", png, 16)) > 1024 else []
    with tempfile.TemporaryDirectory() as directory:
        source, target = Path(directory, "screen.png"), Path(directory, "screen.jpg")
        source.write_bytes(png)
        try:
            subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", *resize,
                            str(source), "--out", str(target)], check=True, capture_output=True, timeout=15)
        except subprocess.SubprocessError as exc:
            raise OSError("Screenshot conversion failed.") from exc
        return "data:image/jpeg;base64," + base64.b64encode(target.read_bytes()).decode()


def settle(shot: Callable[[], bytes]) -> bytes:
    """The first of two screenshots 0.15 s apart that match (at most 3 s), else the latest."""
    started, previous = time.monotonic(), shot()
    while time.monotonic() - started < 3:
        time.sleep(0.15)
        current = shot()
        if current == previous or abs(len(current) - len(previous)) <= len(previous) * 0.0005:
            return current
        previous = current
    return previous


def run(session, decisions: Decisions, data: dict) -> dict:
    """Capture, decide, act until done, an escalation, a risky tap needing approval, or the step limit."""
    if any(key in data for key in ("observe", "masks", "limit", "offset")):
        raise ValueError("Goal replies carry no observation; observe separately.")
    goal = validate_text(data.get("goal"))
    text = data.get("text")
    if text is not None:
        text = validate_text(text, multiline=True)
    approve = data.get("approve", [])
    if not isinstance(approve, list) or not set(approve) <= navigator.RISKY_EFFECTS:
        raise ValueError(f"approve lists effects among {sorted(navigator.RISKY_EFFECTS)}.")
    max_steps = data.get("max_steps", DEFAULT_STEPS)
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
        raise ValueError(f"max_steps must be 1-{MAX_STEPS}.")
    steps, history, seen = [], [], Counter()
    sent, uncertain, deciding = 0, False, 0.0
    outcome, reason, extra = "step_limit", None, {}
    try:
        for _ in range(max_steps):
            try:
                with session.connection.operation():
                    png = settle(lambda: session._read(lambda wda: wda.screenshot()))
                    session._accessibility(limit=1)
                obs = session.snapshot
                if obs.elements[0].bounds is None:
                    raise ValueError("Screen frame unavailable.")
                if obs.secure:
                    outcome, reason = "escalate", "secure_field"
                    break
                screen = navigator.screen(obs, jpeg(png))
            except (OpenClawIPhoneError, OSError, ValueError) as exc:
                outcome, reason, extra = "escalate", "capture_failed", {"error": diagnostic(exc)}
                break
            step_goal = goal if not history else f"{goal}\nAlready done: " + "; ".join(history[-4:])
            started = time.monotonic()
            try:
                d = navigator.decide(decisions.ask, screen, step_goal)
                step = {"app": screen.app, "action": d["action"]}
                if d["action"] == "dismiss_overlay":
                    d = navigator.decide(decisions.ask, screen, navigator.DISMISS_GOAL)
                    step["dismiss"] = d["action"]
            except (navigator.Unavailable, KeyError, TypeError, ValueError) as exc:
                outcome, reason = "escalate", "navigator_unavailable"
                extra = {"error": {"category": getattr(exc, "category", "bad_reply"), "phase": "navigator"}}
                break
            finally:
                deciding += time.monotonic() - started
            target = d.get("target")
            if target:
                step["label"] = session._redact(target["label"] or "")
            step.update({k: d[k] for k in ("confidence", "reason", "risk") if k in d})
            steps.append(step)
            fingerprint = hashlib.sha256(png).digest(), d["action"], target and target["key"]
            seen[fingerprint] += 1
            if seen[fingerprint] > 2:
                outcome, reason = "escalate", "no_progress"
                break
            if step["action"] in ("done", "escalate"):
                outcome, reason = step["action"], d.get("reason")
                break
            if step["action"] == "dismiss_overlay" and (d["action"] != "tap" or d.get("needs_approval")):
                outcome, reason = "escalate", "cannot_dismiss_overlay_safely"
                break
            if d["action"] == "tap":
                if d.get("needs_approval") and d["risk"].get("effect") not in approve:
                    outcome, reason = "needs_approval", d["risk"].get("effect", "risk_check_unavailable")
                    extra = {"pending": {"target": target["id"], "label": step["label"], "effect": d["risk"].get("effect")}}
                    break
                reply = session._request({"op": "tap", "target": target["id"]})
                if reply.get("dispatch") == "not_sent" and reply.get("reason") == "target_missing_or_ambiguous":
                    reply = session._request({"op": "tap", "x": target["x"], "y": target["y"]})
                note = f"tapped '{step['label']}'" if step["action"] == "tap" else f"closed an overlay via '{step['label']}'"
            elif d["action"] == "type_text":
                if text is None or history and history[-1] == TYPED:
                    outcome, reason = "escalate", "text_needed" if text is None else "repeated_type"
                    break
                reply, note = session._request({"op": "set", "value": text}), TYPED
            else:  # scroll_down
                x, y, w, h = obs.elements[0].bounds
                reply = session._request({"op": "swipe", "from_x": round(x + w / 2), "from_y": round(y + h * 0.725),
                                          "to_x": round(x + w / 2), "to_y": round(y + h * 0.335), "duration": 0.3})
                note = "scrolled down"
            step["dispatch"] = reply.get("dispatch")
            if step["dispatch"] != "acknowledged":
                uncertain = step["dispatch"] in ("unknown", "partial")
                outcome, reason = "escalate", "inspect_before_retry" if uncertain else "action_not_sent"
                break
            sent += 1
            history.append(note)
            if session.closed:
                outcome, reason = "escalate", "interrupted"
                break
            time.sleep(0.4)  # let the transition start before the next settle check
    except KeyboardInterrupt:  # between writes: a write in flight reports through its own step
        session.connection.invalidate()
        session.closed = True
        outcome, reason = "escalate", "interrupted"
    result = {"status": "action" if sent or uncertain else "checked",
              "dispatch": "unknown" if uncertain else "acknowledged" if sent else "not_sent",
              "acknowledged_substeps": sent, "outcome": outcome, "steps": steps,
              "snapshot_id": session.snapshot.id if session.snapshot else None,
              "navigator_seconds": round(deciding, 3)}
    if reason:
        result["reason"] = reason
    return result | extra
