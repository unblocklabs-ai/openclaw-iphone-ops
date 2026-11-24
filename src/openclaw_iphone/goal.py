"""Optional goal navigation: OpenAI, TypeSafe, or Liquid AI (LFM-d1) picks each step; the session performs it.

Off unless configured. Each step's screenshot (and screen text in AX mode) goes to the selected provider.
Secure fields are never sent; an optional local d1-omni guard can screen every screenshot before any cloud upload.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import select
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import __version__, navigator
from .config import IPhoneConfig
from .errors import OpenClawIPhoneError, diagnostic
from .inputs import validate_text

BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
OPENAI_API = "https://api.openai.com/v1"
TYPESAFE_API = "https://api.typesafe.ai/v1"
LIQUID_API = "https://api.liquid.ai/decisions/v1"
MODEL = "gpt-6-luna"
LIQUID_MODEL = "d1.2"
LOCAL_GUARD_MODEL = "d1-omni-600M"
RETRIES = 2
UNEXPANDED = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")  # a $VAR reference whose variable was not set
NO_KEY = ("no OpenAI API key: run `openclaw-iphone goal setup --key-stdin` or `--key-file PATH`, "
          "or export OPENAI_API_KEY")
CHECK = {"input": "The sky is blue.",  # `goal check`: proves key and access without any screen data
         "questions": [{"type": "predicate", "name": "check", "instructions": "Is this sentence about the sky?"}]}
DEFAULT_STEPS, MAX_STEPS = 14, 30
TYPED = "typed the supplied text"
SUBMITTED = "pressed Return/Search to submit"
PROVIDER_ALIASES = {"openai": "openai", "typesafe": "typesafe", "liquid": "liquid", "d1": "liquid", "lfm-d1": "liquid"}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in LOOPBACK_HOSTS


class Decisions:
    """POST /v1/decisions: typed answers to the navigator's questions."""

    endpoint = "decisions"
    model = MODEL
    provider = "OpenAI's Decisions API"
    supports_box = False

    def __init__(self, api_key: str, *, base_url: str = OPENAI_API, model: str | None = None,
                 timeout: float = 30, local_guard: Decisions | None = None) -> None:
        self.url = f"{base_url.rstrip('/')}/{self.endpoint}"
        self.api_key, self.timeout = api_key, timeout
        if model:
            self.model = model
        self.local_guard = local_guard
        self._conn_lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self._conn_key: tuple[str, str, int] | None = None

    def close(self) -> None:
        with self._conn_lock:
            self._close_conn_locked()
        if self.local_guard is not None:
            self.local_guard.close()

    def _close_conn_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
            self._conn_key = None

    def ask(self, body: dict) -> dict:
        return navigator.answers(self._post(dict(body, model=self.model)))

    def _uses_proxy(self, parsed: urllib.parse.SplitResult) -> bool:
        host = parsed.hostname or ""
        if host.lower() in LOOPBACK_HOSTS:
            return False
        proxies = urllib.request.getproxies()
        if parsed.scheme not in proxies:
            return False
        return not urllib.request.proxy_bypass(host)

    def _post_direct(self, parsed: urllib.parse.SplitResult, data: bytes, req_headers: dict[str, str]) -> tuple[int, bytes]:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        key = (parsed.scheme, parsed.hostname or "", port)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        headers = dict(req_headers, Connection="keep-alive")
        with self._conn_lock:
            for stale_attempt in range(2):
                reused = False
                if self._conn is not None and self._conn_key != key:
                    self._close_conn_locked()
                if self._conn is not None and self._conn.sock is not None:
                    try:
                        readable, _, errored = select.select([self._conn.sock], [], [self._conn.sock], 0.0)
                        if readable or errored:
                            self._close_conn_locked()
                        else:
                            reused = True
                    except (OSError, ValueError):
                        self._close_conn_locked()
                if self._conn is None:
                    if parsed.scheme == "https":
                        self._conn = http.client.HTTPSConnection(
                            parsed.hostname, port, timeout=self.timeout, context=ssl.create_default_context()
                        )
                    else:
                        self._conn = http.client.HTTPConnection(parsed.hostname, port, timeout=self.timeout)
                    self._conn_key = key
                try:
                    self._conn.request("POST", path, body=data, headers=headers)
                    resp = self._conn.getresponse()
                    raw = resp.read()
                    status = resp.status
                    if resp.will_close:
                        self._close_conn_locked()
                    return status, raw
                except (http.client.HTTPException, OSError):
                    self._close_conn_locked()
                    if reused and stale_attempt == 0:
                        continue
                    raise
        raise OSError("Decisions connection closed.")

    def _post(self, body: dict, *, headers: dict[str, str] | None = None) -> dict:
        data = json.dumps(body).encode()
        req_headers = {
            "Content-Type": "application/json",
            "User-Agent": f"openclaw-iphone/{__version__}",
            **(headers or {}),
        }
        if self.api_key:
            req_headers["Authorization"] = f"Bearer {self.api_key}"
        parsed = urllib.parse.urlsplit(self.url)
        use_proxy = self._uses_proxy(parsed)
        for attempt in range(RETRIES + 1):  # asking has no side effects: rate limits and server errors are retried
            try:
                if use_proxy:
                    request = urllib.request.Request(self.url, data=data, method="POST", headers=req_headers)
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        return json.load(response)
                status, raw = self._post_direct(parsed, data, req_headers)
                if 200 <= status < 300:
                    return json.loads(raw.decode("utf-8"))
                if (status == 429 or status >= 500) and attempt < RETRIES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise navigator.Unavailable("The Decisions API refused the request.", category=f"http_{status}") from None
            except urllib.error.HTTPError as exc:
                exc.close()
                if (exc.code == 429 or exc.code >= 500) and attempt < RETRIES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise navigator.Unavailable("The Decisions API refused the request.", category=f"http_{exc.code}") from None
            except (http.client.HTTPException, OSError) as exc:
                raise navigator.Unavailable("The Decisions API is unreachable.", category="unreachable") from exc
            except (ValueError, UnicodeDecodeError) as exc:
                raise navigator.Unavailable("The Decisions API reply is not JSON.") from exc


class SystemOneDecisions(Decisions):
    """Shared System One / OpenRouter Decisions wire adapter for TypeSafe Jev and Liquid AI LFM-d1."""

    endpoint = "systemone"
    image_headers: dict[str, str] = {}
    _openrouter_decisions = False

    def ask(self, body: dict) -> dict:
        state, assets, questions = body["input"], {}, {}
        inline_images = bool(getattr(self, "_openrouter_decisions", False))
        if isinstance(state, list):
            state = []
            for message in body["input"]:
                for part in message["content"]:
                    if part["type"] == "input_text":
                        state.append({"text": part["text"]})
                    elif part["type"] == "input_image":
                        prefix, separator, data = part["image_url"].partition(",")
                        if not separator or prefix != "data:image/jpeg;base64":
                            raise navigator.Unavailable("Unsupported navigator image.", category="bad_request")
                        if inline_images:
                            state.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
                        else:
                            ref = str(uuid.uuid4())
                            assets[ref] = {"type": "image", "format": "jpg", "data_base64": data}
                            state.append({"screenshot": ref})
                    else:
                        raise navigator.Unavailable("Unsupported navigator input.", category="bad_request")
        elif inline_images:
            # OpenRouter / codex-lb Decisions rejects bare objects and accepts array-wrapped state.
            state = [state]
        for question in body["questions"]:
            kind = question["type"]
            mapped: dict[str, object] = {
                "type": "noul" if kind == "predicate" else kind,
                "instructions": question["instructions"],
            }
            if kind == "choice":
                mapped["criteria"] = {c["value"]: c["description"] for c in question["choices"]}
            elif kind == "score":
                mapped["criteria"] = [level["description"] for level in question["levels"]]
            elif kind not in {"predicate", "box", "boxes", "text"}:
                raise navigator.Unavailable("Unsupported navigator question.", category="bad_request")
            questions[question["name"]] = mapped
        request: dict[str, object] = {"model": self.model, "state": state, "questions": questions}
        if assets:
            request["assets"] = assets
        headers = dict(self.image_headers) if assets and self.image_headers else None
        reply = self._post(request, headers=headers)

        def number(value: object, maximum: float = 1.0) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError("Invalid decision number.")
            return float(value)

        def parse_box(raw: object) -> dict[str, float]:
            if not isinstance(raw, dict):
                raise ValueError("Invalid box.")
            coords = {k: number(raw[k]) for k in ("top", "bottom", "left", "right")}
            if not (coords["left"] < coords["right"] and coords["top"] < coords["bottom"]):
                raise ValueError("Degenerate box.")
            return coords

        answers: dict[str, object] = {}
        try:
            for name, question in questions.items():
                answer = reply["answers"][name]
                qtype = question["type"]
                if answer["type"] != qtype:
                    raise ValueError("Wrong answer type.")
                if qtype == "noul":
                    answers[name] = {"probability": number(answer["noul"])}
                elif qtype == "choice":
                    probabilities = answer["probabilities"]
                    if set(probabilities) != set(question["criteria"]) or answer["choice"] not in probabilities:
                        raise ValueError("Wrong answer options.")
                    answers[name] = {"choice": answer["choice"],
                                     "probabilities": {k: number(v) for k, v in probabilities.items()}}
                elif qtype == "score":
                    answers[name] = {"score": number(answer["score"], len(question["criteria"]) - 1)}
                elif qtype == "box":
                    raw_box = answer.get("box")
                    parsed = parse_box(raw_box) if raw_box is not None else None
                    conf = number(answer.get("confidence", 1.0 if parsed is not None else 0.0))
                    answers[name] = {"box": parsed, "confidence": conf}
                elif qtype == "boxes":
                    raw_items = answer.get("boxes")
                    if not isinstance(raw_items, list):
                        raise ValueError("Invalid boxes.")
                    parsed_items = []
                    for item in raw_items:
                        if not isinstance(item, dict):
                            raise ValueError("Invalid box item.")
                        coords = parse_box(item.get("box", item))
                        conf = number(item.get("confidence", 1.0))
                        parsed_items.append({"box": coords, "confidence": conf})
                    answers[name] = {"boxes": parsed_items}
                elif qtype == "text":
                    raw_text = answer.get("text")
                    cleaned = None if raw_text is None or str(raw_text).strip().lower() == "not found" else str(raw_text)
                    answers[name] = {"text": cleaned}
                else:
                    raise ValueError("Unsupported answer type.")
        except (KeyError, TypeError, ValueError):
            raise navigator.Unavailable(f"Unexpected {self.provider} reply.") from None
        return answers


class TypeSafeDecisions(SystemOneDecisions):
    """Translate the navigator's questions to TypeSafe's native System One contract."""

    model = "jev-1.14.0"
    provider = "TypeSafe (experimental images)"
    image_headers = {"TypeSafe-Danger-Zone-Images": "true"}
    supports_box = False

    def __init__(self, api_key: str, *, base_url: str = TYPESAFE_API, model: str | None = None,
                 timeout: float = 30, local_guard: Decisions | None = None) -> None:
        super().__init__(api_key, base_url=base_url, model=model, timeout=timeout, local_guard=local_guard)


def _is_codex_decisions_url(base_url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(base_url)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() == "codex.unblocklabs.ai"


def _uses_openrouter_decisions(base_url: str, model: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(base_url)
    except ValueError:
        return False
    path = parsed.path.rstrip("/")
    host = (parsed.hostname or "").lower()
    if path.endswith("/systemone") or is_loopback_url(base_url):
        return False
    return (
        path.endswith("/decisions")
        or host in {"codex.unblocklabs.ai", "openrouter.ai"}
        or model.strip().lower().startswith("liquid/")
    )


class LiquidDecisions(SystemOneDecisions):
    """Liquid AI LFM-d1 via direct System One, OpenRouter/codex-lb Decisions, or local MLX d1-omni."""

    model = LIQUID_MODEL
    provider = "Liquid AI (LFM-d1)"
    image_headers = {}
    supports_box = True

    def __init__(self, api_key: str = "", *, base_url: str = LIQUID_API, model: str | None = None,
                 timeout: float = 30, local_guard: Decisions | None = None,
                 supports_box: bool | None = None) -> None:
        super().__init__(api_key, base_url=base_url, model=model, timeout=timeout, local_guard=local_guard)
        self._openrouter_decisions = _uses_openrouter_decisions(base_url, self.model)
        root = base_url.rstrip("/")
        if self._openrouter_decisions:
            self.url = root if root.endswith("/decisions") else f"{root}/decisions"
        else:
            self.url = root if root.endswith("/systemone") else f"{root}/systemone"
        if supports_box is not None:
            self.supports_box = supports_box
        elif self._openrouter_decisions:
            # OpenRouter/codex-lb Liquid Decisions rejects `box` questions (HTTP 400); allow explicit override.
            self.supports_box = False
        else:
            # Direct hosted d1.2/d1.2-ultra supports native Box grounding; open-weight
            # d1-omni-600M/2B MLX evaluates noul/choice/score natively (override via OPENCLAW_IPHONE_GOAL_VISION_BOX).
            self.supports_box = self.model.strip().lower().startswith("d1.2")


def goal_provider(config: IPhoneConfig) -> str:
    raw = (config.get("OPENCLAW_IPHONE_GOAL_PROVIDER") or "openai").strip().lower()
    provider = PROVIDER_ALIASES.get(raw)
    if provider is None:
        raise ValueError("OPENCLAW_IPHONE_GOAL_PROVIDER must be openai, typesafe, or liquid.")
    return provider


def enabled(config: IPhoneConfig) -> bool:
    return (config.get("OPENCLAW_IPHONE_GOAL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def read_key_file(path: Path, *, provider: str = "openai") -> str | None:
    """A bare key or the selected provider's dotenv field; never use another provider's key."""
    text = path.read_text(encoding="utf-8")
    accepted = {f"{provider.upper()}_API_KEY", f"OPENCLAW_IPHONE_{provider.upper()}_API_KEY"}
    if provider == "liquid":
        accepted.add("CODEX_LB_API_KEY")
    for line in text.splitlines():
        name, equals, value = line.strip().removeprefix("export ").partition("=")
        if equals and name.strip() in accepted:
            return value.strip().strip("'\"") or None
    token = text.strip()
    return token if token and "=" not in token and not any(c.isspace() for c in token) else None


def api_key(config: IPhoneConfig, env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """The selected provider's key and source (never the key itself), without cross-provider fallback."""
    env = os.environ if env is None else env
    provider = goal_provider(config)
    key_name = f"OPENCLAW_IPHONE_{provider.upper()}_API_KEY"
    file_name = key_name + "_FILE"
    env_name = f"{provider.upper()}_API_KEY"

    def loose(path: Path | None) -> str:
        try:
            return " (readable by others: chmod 600)" if path and path.stat().st_mode & 0o077 else ""
        except OSError:
            return ""

    value = (config.get(key_name) or "").strip()
    if value and not UNEXPANDED.fullmatch(value):
        if env.get(key_name):
            return value, f"{key_name} in the environment"
        return value, f"{key_name} in {config.path}{loose(config.path)}"
    if config.get(file_name):
        path = Path(config.get(file_name)).expanduser()
        try:
            key = read_key_file(path, provider=provider)
        except (OSError, UnicodeDecodeError):
            return None, f"key file {path} is unreadable"
        return (key, f"key file {path}{loose(path)}") if key else (None, f"no key found in {path}")
    if (env.get(env_name) or "").strip():
        return env[env_name].strip(), f"{env_name} in the environment"
    if provider == "liquid":
        base_url = (config.get("OPENCLAW_IPHONE_LIQUID_BASE_URL") or "").strip()
        if base_url and is_loopback_url(base_url):
            return "", f"local loopback endpoint {base_url} (no key required)"
        if base_url and _is_codex_decisions_url(base_url) and (env.get("CODEX_LB_API_KEY") or "").strip():
            return env["CODEX_LB_API_KEY"].strip(), "CODEX_LB_API_KEY in the environment"
        return None, "no Liquid API key: use goal setup --provider liquid --key-file PATH or --key-stdin"
    return None, NO_KEY if provider == "openai" else "no TypeSafe API key: use goal setup --provider typesafe --key-file PATH"


def _provider_cls(provider: str) -> type[Decisions]:
    if provider == "typesafe":
        return TypeSafeDecisions
    if provider == "liquid":
        return LiquidDecisions
    return Decisions


def local_guard_from_config(config: IPhoneConfig) -> LiquidDecisions | None:
    url = (config.get("OPENCLAW_IPHONE_LOCAL_GUARD_URL") or "").strip()
    if not url:
        return None
    if not is_loopback_url(url):
        raise ValueError("OPENCLAW_IPHONE_LOCAL_GUARD_URL must point to a local loopback host (127.0.0.1/localhost/::1).")
    model = (config.get("OPENCLAW_IPHONE_LOCAL_GUARD_MODEL") or LOCAL_GUARD_MODEL).strip() or LOCAL_GUARD_MODEL
    return LiquidDecisions("", base_url=url, model=model, timeout=10)


def goal_navigation(config: IPhoneConfig, env: Mapping[str, str] | None = None) -> str:
    """For doctor: off, on (model and where the key comes from), or incomplete (what is missing)."""
    if not enabled(config):
        return "off"
    provider = goal_provider(config)
    key, source = api_key(config, env)
    client_cls = _provider_cls(provider)
    model = (config.get("OPENCLAW_IPHONE_GOAL_MODEL") or client_cls.model).strip() or client_cls.model
    guard_url = (config.get("OPENCLAW_IPHONE_LOCAL_GUARD_URL") or "").strip()
    guard_note = "; local guard: on" if guard_url else ""
    if key is None:
        return f"incomplete ({source})"
    return f"on ({model} via {client_cls.provider}; key: {source}{guard_note})"


def decisions_from_config(config: IPhoneConfig, env: Mapping[str, str] | None = None) -> Decisions | None:
    if not enabled(config):
        return None
    key = api_key(config, env)[0]
    if key is None:
        return None
    provider = goal_provider(config)
    model = (config.get("OPENCLAW_IPHONE_GOAL_MODEL") or "").strip() or None
    local_guard = local_guard_from_config(config)
    if provider == "typesafe":
        return TypeSafeDecisions(key, model=model, local_guard=local_guard)
    if provider == "liquid":
        base_url = (config.get("OPENCLAW_IPHONE_LIQUID_BASE_URL") or LIQUID_API).strip() or LIQUID_API
        vision_box_raw = (config.get("OPENCLAW_IPHONE_GOAL_VISION_BOX") or "").strip().lower()
        vision_box = True if vision_box_raw in {"1", "true", "yes", "on"} else False if vision_box_raw in {"0", "false", "no", "off"} else None
        return LiquidDecisions(key, base_url=base_url, model=model, local_guard=local_guard, supports_box=vision_box)
    base_url = (config.get("OPENCLAW_IPHONE_OPENAI_BASE_URL") or OPENAI_API).strip() or OPENAI_API
    return Decisions(key, base_url=base_url, model=model, local_guard=local_guard)


def jpeg(png: bytes) -> str:
    """The validated request image as a data URL: JPEG quality 80, longest side at most 1024 px.

    Uses in-memory Pillow when installed to avoid subprocess/temp-file churn, falling back to macOS `sips`.
    """
    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Invalid PNG.")
    width, height = struct.unpack_from(">II", png, 16)
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        Image = None
    if Image is not None:
        try:
            with Image.open(io.BytesIO(png)) as img:
                rgb = img.convert("RGB")
                if max(rgb.size) > 1024:
                    rgb.thumbnail((1024, 1024))
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=80)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except OSError:
            pass
    resize = ["-Z", "1024"] if max(width, height) > 1024 else []
    with tempfile.TemporaryDirectory() as directory:
        source, target = Path(directory, "screen.png"), Path(directory, "screen.jpg")
        source.write_bytes(png)
        try:
            subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", *resize,
                            str(source), "--out", str(target)], check=True, capture_output=True, timeout=15)
        except subprocess.SubprocessError as exc:
            raise OSError("Screenshot conversion failed.") from exc
        return "data:image/jpeg;base64," + base64.b64encode(target.read_bytes()).decode()


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack_from(">II", data, 16)
    return None


def same(a: bytes, b: bytes) -> bool:
    """Screenshots of an unchanged screen: identical, or matching dimensions with tight PNG size & body stability."""
    if a == b:
        return True
    if not a or not b:
        return False
    dims_a, dims_b = _png_dimensions(a), _png_dimensions(b)
    if dims_a is not None and dims_b is not None and dims_a != dims_b:
        return False
    if abs(len(a) - len(b)) > len(b) * 0.0005:
        return False
    # Guard against distinct same-dimension screens whose compressed lengths happen to match:
    # if both the midpoint and tail byte slices diverge, treat the screen as changed (conservative).
    if len(a) >= 256 and len(b) >= 256:
        mid_a, mid_b = len(a) // 2, len(b) // 2
        tail_a, tail_b = a[-64:], b[-64:]
        if a[mid_a:mid_a + 64] != b[mid_b:mid_b + 64] and tail_a != tail_b:
            return False
    return True


def settle(shot: Callable[[], bytes], *, max_wait: float = 1.0, poll: float = 0.18, max_shots: int = 3) -> bytes:
    """The first of two screenshots `poll` s apart that match (at most `max_wait` s / `max_shots` captures), else the latest."""
    started, previous, count = time.monotonic(), shot(), 1
    while count < max_shots and time.monotonic() - started < max_wait:
        time.sleep(poll)
        current = shot()
        count += 1
        if same(current, previous):
            return current
        previous = current
    return previous


def _window_size(session, obs, *, png_dims: tuple[int, int] | None = None,
                 size_cache: dict[tuple[int, int] | None, tuple[float, float]] | None = None) -> tuple[float, float]:
    if obs is not None and obs.elements and obs.elements[0].bounds is not None:
        _, _, w, h = obs.elements[0].bounds
        size = (float(w), float(h))
        if size_cache is not None:
            size_cache[png_dims] = size
        return size
    if size_cache is not None and png_dims in size_cache:
        return size_cache[png_dims]
    cached = getattr(session, "image_geometry", None)
    if cached and cached[0] and (png_dims is None or cached[1] == png_dims):
        size = (float(cached[0][0]), float(cached[0][1]))
        if size_cache is not None:
            size_cache[png_dims] = size
        return size
    with session.connection.operation():
        raw = session._read(lambda wda: wda.window_size())
    size = (float(raw[0]), float(raw[1]))
    if size_cache is not None:
        size_cache[png_dims] = size
    return size


def run(session, decisions: Decisions, data: dict, *,
        observe_mode: str | None = None, masks: list | None = None, limit: int = 80) -> dict:
    """Capture, decide, act until done, an escalation, a risky tap needing approval, or the step limit."""
    if "offset" in data:
        raise ValueError("Goal replies cannot page an older snapshot.")
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
    grounding = data.get("grounding", "auto")
    if grounding not in {"auto", "ax", "vision"}:
        raise ValueError("grounding must be auto, ax, or vision.")
    supports_box = bool(getattr(decisions, "supports_box", False))
    if grounding == "vision" and not supports_box:
        raise ValueError("Vision grounding requires a Box-capable provider (such as Liquid AI LFM-d1).")
    app = data.get("app")
    if app is not None and (not isinstance(app, str) or not app.strip() or len(app) > 200):
        raise ValueError("app is an app name or bundle identifier.")
    steps, history, seen = [], [], Counter()
    sent, uncertain, deciding = 0, False, 0.0
    outcome, reason, extra = "step_limit", None, {}
    bundle: str | None = None
    if app is not None:  # the navigator only taps what is on screen; opening the app is the session's job
        try:
            bundle = app if BUNDLE_ID.fullmatch(app) else session.connection.ctl.find_app(
                session.connection.device.identifier, app).bundle_identifier
        except (OpenClawIPhoneError, ValueError) as exc:
            return {"status": "checked", "dispatch": "not_sent", "acknowledged_substeps": 0, "outcome": "escalate",
                    "reason": "app_not_found", "steps": [], "error": diagnostic(exc)}
        reply = session._request({"op": "launch", "bundle_id": bundle})
        if reply.get("dispatch") != "acknowledged":
            return {"status": "error", "dispatch": reply.get("dispatch", "not_sent"), "acknowledged_substeps": 0,
                    "outcome": "escalate", "reason": "app_not_launched", "steps": []}
        sent = 1
    local_guard = getattr(decisions, "local_guard", None)
    size_cache: dict[tuple[int, int] | None, tuple[float, float]] = {}
    last_png: bytes | None = None
    last_obs = None
    last_depth: int | None = None
    last_capture_after_write = False
    try:
        for index in range(max_steps):
            obs = None
            depth = None
            use_vision = grounding == "vision" or (
                grounding == "auto" and supports_box and session.app_hint in session.shallow_apps
            )
            try:
                with session.connection.operation():
                    png = settle(lambda: session._read(lambda wda: wda.screenshot()))
                    if not use_vision:
                        try:
                            obs, depth = session._capture_accessibility()
                        except (OpenClawIPhoneError, ValueError):
                            if grounding == "auto" and supports_box:
                                use_vision = True
                            else:
                                raise
                if not use_vision and app is not None and index == 0 and obs is not None and obs.app != bundle:
                    # still launching: one more settle
                    with session.connection.operation():
                        png = settle(lambda: session._read(lambda wda: wda.screenshot()))
                        try:
                            obs, depth = session._capture_accessibility()
                        except (OpenClawIPhoneError, ValueError):
                            if grounding == "auto" and supports_box:
                                use_vision = True
                                obs = None
                                depth = None
                            else:
                                raise
                if not use_vision:
                    if obs is None or obs.elements[0].bounds is None:
                        if grounding == "auto" and supports_box:
                            use_vision = True
                        else:
                            raise ValueError("Screen frame unavailable.")
                    elif obs.secure:
                        outcome, reason = "escalate", "secure_field"
                        break
                last_png, last_obs, last_depth = png, obs, depth
                last_capture_after_write = True
                encoded_image = jpeg(png)
                png_dims = _png_dimensions(png)
                win_size = _window_size(session, obs, png_dims=png_dims, size_cache=size_cache) if use_vision else None
            except (OpenClawIPhoneError, OSError, ValueError) as exc:
                outcome, reason, extra = "escalate", "capture_failed", {"error": diagnostic(exc)}
                break

            # Optional local d1-omni privacy guard: runs on-device/loopback before any cloud upload.
            if local_guard is not None:
                try:
                    guard_reply = local_guard.ask(
                        navigator.sensitive_guard_request(encoded_image, app=(obs.app if obs else session.app_hint))
                    )
                    if guard_reply["sensitive"]["probability"] >= navigator.THRESHOLDS["sensitive_prob"]:
                        outcome, reason = "escalate", "secure_field"
                        break
                except (navigator.Unavailable, KeyError, TypeError, ValueError) as exc:
                    outcome, reason = "escalate", "local_guard_unavailable"
                    extra = {"error": {"category": getattr(exc, "category", "bad_reply"), "phase": "local_guard"}}
                    break

            step_goal = goal if not history else f"{goal}\nAlready done: " + "; ".join(history[-4:])
            started = time.monotonic()
            try:
                if use_vision:
                    current_app = obs.app if obs is not None else (session.app_hint or bundle or "unknown")
                    assert win_size is not None
                    d = navigator.decide_vision(decisions.ask, encoded_image, step_goal, win_size, app=current_app)
                    step = {"app": current_app, "action": d["action"], "grounding": "vision"}
                    if d["action"] == "dismiss_overlay":
                        d = navigator.decide_vision(decisions.ask, encoded_image, navigator.DISMISS_GOAL, win_size, app=current_app)
                        step["dismiss"] = d["action"]
                else:
                    assert obs is not None
                    screen = navigator.screen(obs, encoded_image)
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
            if "considered" in d:  # an escalation: what the navigator leaned toward, for the agent to take over from
                step["considered"] = [c | {"label": session._redact(c["label"])} if "label" in c else c for c in d["considered"]]
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
                    pending = {"target": target["id"], "label": step["label"], "effect": d["risk"].get("effect")}
                    if target.get("id") is None:
                        pending |= {"x": target["x"], "y": target["y"]}
                    extra = {"pending": pending}
                    break
                try:
                    with session.connection.operation():
                        current_png = session._read(lambda wda: wda.screenshot())
                except (OpenClawIPhoneError, OSError) as exc:
                    outcome, reason, extra = "escalate", "capture_failed", {"error": diagnostic(exc)}
                    break
                if same(current_png, png):
                    reply = session._request({"op": "tap", "x": target["x"], "y": target["y"]})
                elif target.get("id") is not None:
                    reply = session._request({"op": "tap", "target": target["id"]})
                else:
                    outcome, reason = "escalate", "screen_changed"
                    break
                label_note = step["label"] or f"({target['x']},{target['y']})"
                note = f"tapped '{label_note}'" if step["action"] == "tap" else f"closed an overlay via '{label_note}'"
            elif d["action"] == "type_text":
                if text is None or history and history[-1] == TYPED:
                    outcome, reason = "escalate", "text_needed" if text is None else "repeated_type"
                    break
                reply, note = session._request({"op": "set", "value": text}), TYPED
            elif d["action"] == "submit":
                if history and history[-1] == SUBMITTED:
                    outcome, reason = "escalate", "repeated_submit"
                    break
                reply, note = session._request({"op": "press", "button": "enter"}), SUBMITTED
            elif d["action"] == "go_back":
                reply = session._request({"op": "press", "button": "back"})
                note = "went back"
            else:  # scroll_down
                if obs is not None and obs.elements and obs.elements[0].bounds is not None:
                    x, y, w, h = obs.elements[0].bounds
                else:
                    w, h = win_size if win_size is not None else _window_size(session, obs, png_dims=png_dims, size_cache=size_cache)
                    x, y = 0.0, 0.0
                reply = session._request({"op": "swipe", "from_x": round(x + w / 2), "from_y": round(y + h * 0.725),
                                          "to_x": round(x + w / 2), "to_y": round(y + h * 0.335), "duration": 0.3})
                note = "scrolled down"
            last_capture_after_write = False
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
            if index + 1 < max_steps:
                time.sleep(0.2)  # let the transition start before the next settle check
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
    if observe_mode and not session.closed and reason != "secure_field":
        req_masks = masks or []
        observation: dict[str, object] = {}
        try:
            if last_capture_after_write and not req_masks:
                if observe_mode in ("accessibility", "both") and last_obs is not None:
                    observation["accessibility"] = session._render_accessibility(last_obs, last_depth, limit=limit, offset=0)
                if observe_mode in ("image", "both") and last_png is not None:
                    observation["image"] = session._save_image_bytes(last_png, masks=[])
            needed_ax = observe_mode in ("accessibility", "both") and "accessibility" not in observation
            needed_img = observe_mode in ("image", "both") and "image" not in observation
            if needed_ax or needed_img:
                fetch_mode = "both" if needed_ax and needed_img else ("accessibility" if needed_ax else "image")
                with session.connection.operation():
                    observation.update(session._observe(fetch_mode, req_masks, limit=limit, offset=0))
        except (OpenClawIPhoneError, OSError, ValueError) as exc:
            observation["observation_error"] = diagnostic(exc)
        if observation:
            result["observation"] = observation
    return result | extra
