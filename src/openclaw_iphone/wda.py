from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import base64
import binascii
import json
import logging
import math
import os
import re
from pathlib import Path
import time
from typing import Any, Iterator
from xml.etree import ElementTree as ET
import urllib.parse

from .errors import WDAOutcomeUnknown, WDASetupError, WDAStaleElement, WDAUnavailable, WDAUnsupportedCommand, WDAReadUnavailable, WDATransportUnavailable
from .execution import Budget, Metrics, TaskStopped
from .transport import exchange, TransportFailure
from .xcode import resolve_developer_dir


DEFAULT_WDA_PORT = 8100
DEFAULT_WDA_SCHEME = "WebDriverAgentRunner"
DEFAULT_WDA_CONFIGURATION = "Debug"


@dataclass(frozen=True)
class WDAStatus:
    url: str
    payload: dict[str, Any]
    ready: bool | None

@dataclass
class SessionState:
    identifier: str | None = None
    cleanup_failed: bool = False


@dataclass(frozen=True)
class NativeElement:
    ref: str
    role: str | None
    label: str | None
    value: str | None


class WDAClient:
    def __init__(self, *, url: str | None = None, timeout: int = 30,
                 read_timeout: float | None = None) -> None:
        if url is None and not os.environ.get("OPENCLAW_IPHONE_WDA_URL"):
            raise WDASetupError(
                "No WebDriverAgent URL was provided. Use the CLI so it can resolve the "
                "CoreDevice tunnel URL, pass --url for debugging, or set OPENCLAW_IPHONE_WDA_URL "
                "as a debug override."
            )
        self.url = normalize_url(url or os.environ["OPENCLAW_IPHONE_WDA_URL"])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("WDA timeout must be finite and positive.")
        self.timeout = timeout
        if read_timeout is not None and (not math.isfinite(read_timeout) or read_timeout <= 0):
            raise ValueError("Screen read timeout must be finite and positive.")
        self.read_timeout = read_timeout
        self.budget: Budget | None = None
        self.metrics = Metrics()
        self._session = SessionState()

    def status(self) -> WDAStatus:
        payload = self._json_request("/status")
        return WDAStatus(url=self.url, payload=payload, ready=parse_ready(payload))

    def source(self, *, compact: bool = False, validate: bool = True) -> str:
        # `visible` makes WDA ask the app about every node (~20 ms each, ~90% of a full read);
        # compact callers judge visibility from bounds. `traits` is free and carries Selected.
        path = ("/source?format=xml&excluded_attributes="
                "visible,accessible,nativeAccessibilityElement,index,placeholderValue,"
                "nativeFrame,minValue,maxValue,customActions,type") if compact else "/source"
        body = self._request(path)
        parsed = parse_json_bytes(body)
        if isinstance(parsed, dict):
            check_response(parsed, "/source")
            own = self._session.identifier
            if own is not None and parsed.get("sessionId", own) != own:
                # WDA restarted or another client opened a session: either way it reset the
                # idle/animation waits to 10s/2s. Reconnecting re-applies the zero settings.
                raise WDATransportUnavailable("Native session expired.", category="session_lost", phase="response")
            value = parsed.get("value")
            if isinstance(value, str):
                body = value.encode("utf-8")
        source = body.decode("utf-8", errors="replace")
        if len(body) > 2_000_000 or "<!DOCTYPE" in source or "<!ENTITY" in source:
            raise WDAReadUnavailable("Accessibility source exceeds parsing limits.", category="source_limits", phase="accessibility")
        if validate:
            try:
                ET.fromstring(source)
            except ET.ParseError as exc:
                raise WDAReadUnavailable("WDA source was not valid accessibility XML.", category="invalid_xml", phase="accessibility") from exc
        return source

    def screenshot(self) -> bytes:
        body = self._request("/screenshot")
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return body

        parsed = parse_json_bytes(body)
        if isinstance(parsed, dict):
            check_response(parsed, "/screenshot")
            value = parsed.get("value")
            if isinstance(value, str):
                try:
                    decoded = base64.b64decode(value, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise WDAUnavailable("WDA screenshot response contained invalid base64 data.") from exc
                if decoded.startswith(b"\x89PNG\r\n\x1a\n"):
                    return decoded

        raise WDAUnavailable("WDA screenshot response was neither raw PNG nor JSON base64.")

    def locked(self) -> bool | None:
        payload = self._json_request("/wda/locked")
        value = payload.get("value")
        return value if isinstance(value, bool) else None

    def unlock(self) -> dict[str, Any]:
        return self._json_post("/wda/unlock", {})

    def lock(self) -> dict[str, Any]:
        return self._json_post("/wda/lock", {})

    def tap(self, x: float, y: float) -> dict[str, Any]:
        return self._perform_session_actions(
            [
                {
                    "type": "pointer",
                    "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pause", "duration": 100},
                        {"type": "pointerUp", "button": 0},
                    ],
                }
            ]
        )

    def type_text(self, text: str, *, frequency: int | None = None) -> dict[str, Any]:
        """Individual key events in one session, without per-key screen reads.

        iOS can drop all but the first text event in a synthesized multi-key
        batch. Keep separate requests for this explicit compatibility strategy;
        use type_text_bulk for normal fields.
        """
        if not text:
            return {"value": None}
        if frequency is not None and (type(frequency) is not int or frequency <= 0):
            raise ValueError("Typing frequency must be a positive integer.")
        with self.session() as session_id:
            response: dict[str, Any] = {"value": None}
            last_sent = 0.0
            for index, char in enumerate(text):
                try:
                    if index and frequency:
                        delay = max(0, 1 / frequency - (time.monotonic() - last_sent))
                        delay = min(delay, self._request_timeout())
                        if self.budget:
                            self.budget.sleep(delay)
                        elif delay:
                            time.sleep(delay)
                    last_sent = time.monotonic()
                    response = self._json_post(f"/session/{session_id}/actions", {"actions": [{
                        "type": "key", "id": "keyboard1", "actions": [
                            {"type": "keyDown", "value": char}, {"type": "keyUp", "value": char},
                        ],
                    }]})
                except (WDAUnavailable, TaskStopped) as exc:
                    failure = WDAOutcomeUnknown(
                        f"Typing stopped after {index} acknowledged characters; the next may have been entered. "
                        "Inspect the field before retrying; do not replay the full text."
                    )
                    failure.acknowledged_characters = index
                    raise failure from exc
            return response


    def type_text_bulk(self, text: str, *, frequency: int | None = None) -> dict[str, Any]:
        """Append to the focused field. No automatic fallback or replay on failure.

        The caller owns focus and optional readback. A failed request
        may have entered any prefix, including the entire string.
        """
        if not text:
            return {"value": None}
        if frequency is not None and (type(frequency) is not int or frequency <= 0):
            raise ValueError("Typing frequency must be a positive integer.")
        with self.session() as session_id:
            payload: dict[str, Any] = {"value": [text]}
            if frequency is not None:
                payload["frequency"] = frequency
            return self._json_post(f"/session/{session_id}/wda/keys", payload)

    def active_app(self) -> dict[str, Any]:
        value = self._json_request("/wda/activeAppInfo").get("value")
        if not isinstance(value, dict) or not isinstance(value.get("bundleId"), str):
            raise WDAUnavailable("Foreground app identity is unavailable.")
        return value

    def activate_app(self, bundle_id: str) -> dict[str, Any]:
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/wda/apps/activate", {"bundleId": bundle_id})

    def find_elements(self, query: str, *, using: str = "xpath", element_id: str | None = None) -> list[NativeElement]:
        """Read-only query (WDA uses POST); no implicit retries."""
        if using not in {"xpath", "predicate string", "class chain", "accessibility id"}:
            raise ValueError("Unsupported native locator strategy.")
        with self.session() as session_id:
            scope = f"/element/{urllib.parse.quote(element_id, safe='')}" if element_id else ""
            value = self._json_post(f"/session/{session_id}{scope}/elements", {"using": using, "value": query}).get("value")
        if not isinstance(value, list):
            raise WDAUnavailable("Invalid WDA element query response.")
        return [NativeElement(element_identifier(item),
                              item.get("type") if isinstance(item.get("type"), str) else None,
                              item.get("label") if isinstance(item.get("label"), str) else None,
                              item.get("attribute/value") if isinstance(item.get("attribute/value"), str) else None)
                for item in value]

    def active_element(self) -> str:
        with self.session() as session_id:
            return element_identifier(self._json_request(f"/session/{session_id}/element/active").get("value"))

    def window_size(self) -> tuple[float, float]:
        # Sessionless, so a concurrent observation lane never touches the WDA session.
        value = self._json_request("/window/size").get("value")
        if (not isinstance(value, dict) or any(type(value.get(k)) not in (int, float)
                or not math.isfinite(value[k]) or value[k] <= 0 for k in ("width", "height"))):
            raise WDAUnavailable("Device window geometry is unavailable.")
        return value["width"], value["height"]

    def element_value(self, element_id: str, *, allow_null_empty: bool = True) -> str:
        """Explicit WDA value read: null means empty; a missing key is unknown.

        Call only for an independently validated non-secure editable element.
        Custom keypad fields require allow_null_empty=False: null on an Other
        element does not prove it exposes a readable input value.
        WDA may return a placeholder instead of empty; do not erase that fact.
        """
        with self.session() as session_id:
            path = f"/session/{session_id}/element/{urllib.parse.quote(element_id, safe='')}/attribute/value"
            payload = self._json_request(path)
        if "value" not in payload or payload["value"] is not None and not isinstance(payload["value"], str):
            raise WDAUnavailable("Editable value is unavailable.")
        if payload["value"] is None and not allow_null_empty:
            raise WDAUnavailable("Custom field must expose an explicit string value.")
        return payload["value"] or ""

    def element_placeholder(self, element_id: str) -> str | None:
        """WDA's value may be placeholder text after a successful native clear."""
        with self.session() as session_id:
            path = f"/session/{session_id}/element/{urllib.parse.quote(element_id, safe='')}/attribute/placeholderValue"
            value = self._json_request(path).get("value")
        return value if isinstance(value, str) and value else None

    def element_displayed(self, element_id: str) -> bool:
        """WDA's visibility verdict for one element (one AX query, unlike @visible in XPath)."""
        with self.session() as session_id:
            value = self._json_request(f"/session/{session_id}/element/{urllib.parse.quote(element_id, safe='')}/displayed").get("value")
        if not isinstance(value, bool):
            raise WDAReadUnavailable("Native element visibility unavailable.")
        return value

    def element_type(self, element_id: str) -> str:
        with self.session() as session_id:
            path = f"/session/{session_id}/element/{urllib.parse.quote(element_id, safe='')}/attribute/type"
            value = self._json_request(path).get("value")
        if not isinstance(value, str):
            raise WDAReadUnavailable("Native element type unavailable.")
        return value

    def picker_step(self, element_id: str, order: str, *, offset: float) -> dict[str, Any]:
        with self.session() as session_id:
            path = f"/session/{session_id}/wda/pickerwheel/{urllib.parse.quote(element_id, safe='')}/select"
            # One native adjustment, never WDA's unbounded/default 25-attempt loop.
            return self._json_post(path, {"order": order, "offset": offset, "maxAttempts": 1})

    def element_action(self, element_id: str, action: str, *, text: str = "") -> dict[str, Any]:
        """Targeted click, clear or caret-based input; callers own authorization."""
        if action not in {"click", "clear", "value"}:
            raise ValueError("Unsupported element action.")
        with self.session() as session_id:
            path = f"/session/{session_id}/element/{urllib.parse.quote(element_id, safe='')}/{action}"
            return self._json_post(path, {"value": [text]} if action == "value" else {})

    def clear_text(self) -> dict[str, Any]:
        with self.session() as session_id:
            element_id = self.active_element()
            return self._json_post(f"/session/{session_id}/element/{urllib.parse.quote(element_id, safe='')}/clear", {})

    def _perform_session_actions(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/actions", {"actions": actions})

    @contextmanager
    def session(self) -> Iterator[str]:
        if self._session.identifier is not None:
            yield self._session.identifier
            return
        session_id = self._create_session()
        self._session.identifier = session_id
        self._session.cleanup_failed = False
        try:
            # XCTest's global idle heuristics can wait 10s before AND after
            # each click in animated apps. Callers own explicit readiness and
            # postconditions; do not also wait for the entire app to be idle.
            self._json_post(f"/session/{session_id}/appium/settings", {"settings": {
                "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
                "shouldUseCompactResponses": False,
                "elementResponseAttributes": "type,label,attribute/value",
            }})
            yield session_id
        finally:
            self._session.identifier = None
            try:
                self._delete_session(session_id)
            except (WDAUnavailable, TaskStopped):
                self._session.cleanup_failed = True
                logging.getLogger(__name__).warning(
                    "WDA session cleanup failed; action outcome is unchanged. "
                    "Do not replay completed actions. Check WDA before the next workflow."
                )

    def press_button(self, name: str, *, duration: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if duration is not None:
            payload["duration"] = duration
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/wda/pressButton", payload)

    def back(self) -> dict[str, Any]:
        try:
            return self._json_post("/wda/back", {})
        except WDAUnsupportedCommand:
            pass

        with self.session() as session_id:
            try:
                return self._json_post(f"/session/{session_id}/back", {})
            except WDAUnsupportedCommand as exc:
                raise WDAUnsupportedCommand("WDA back is unavailable on this runner.") from exc

    def open_url(self, url: str) -> dict[str, Any]:
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/url", {"url": url})

    def terminate_app(self, bundle_id: str) -> dict[str, Any]:
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/wda/apps/terminate", {"bundleId": bundle_id})

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float, *, duration: float = 0.1) -> dict[str, Any]:
        duration_ms = max(0, int(duration * 1000))
        move_ms = max(100, duration_ms)
        return self._perform_session_actions(
            [
                {
                    "type": "pointer",
                    "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": from_x, "y": from_y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerMove", "duration": move_ms, "x": to_x, "y": to_y},
                        {"type": "pointerUp", "button": 0},
                    ],
                }
            ]
        )

    def _json_request(self, path: str) -> dict[str, Any]:
        body = self._request(path)
        parsed = parse_json_bytes(body)
        if not isinstance(parsed, dict):
            raise WDAReadUnavailable(f"WDA {path} response was not a JSON object.", category="invalid_json", phase="response")
        check_response(parsed, path)
        return parsed

    def _json_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._request(path, method="POST", payload=payload)
        parsed = parse_json_bytes(body)
        if not isinstance(parsed, dict):
            error = WDAUnavailable if read_only_request("POST", path) else WDAOutcomeUnknown
            raise error(f"WDA {path} response was not a JSON object; no automatic replay.")
        check_response(parsed, path, mutating=not read_only_request("POST", path))
        return parsed

    def _create_session(self) -> str:
        payload = {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}
        response = self._json_post("/session", payload)
        session_id = response.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id

        value = response.get("value")
        if isinstance(value, dict):
            nested_session_id = value.get("sessionId")
            if isinstance(nested_session_id, str) and nested_session_id:
                return nested_session_id

        raise WDAUnavailable("WDA session response did not include a session id.")

    def _delete_session(self, session_id: str) -> None:
        body = self._request(f"/session/{session_id}", method="DELETE")
        parsed = parse_json_bytes(body)
        if not isinstance(parsed, dict):
            raise WDAUnavailable("WDA cleanup response was not a JSON object.")
        check_response(parsed, "session cleanup")

    def _request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> bytes:
        timeout = self._request_timeout()
        if self.read_timeout is not None and read_only_request(method, path):
            timeout = min(timeout, self.read_timeout)
        route = re.sub(r"/(session|element|pickerwheel)/(?!active(?:/|$))[^/]+", r"/\1/:id", path.split("?", 1)[0])
        with self.metrics.measure(f"wda {method} {route}"):
            return self._send(path, method=method, payload=payload, timeout=timeout)

    def _request_timeout(self) -> float:
        timeout = self.timeout
        if self.budget is not None:
            timeout = min(timeout, self.budget.remaining())
        return timeout

    def _send(self, path: str, *, method: str, payload: dict[str, Any] | None, timeout: float) -> bytes:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        read = read_only_request(method, path)
        try:
            status, body = exchange(self.url, path, method, data, timeout,
                max_bytes=40_000_000 if path == "/screenshot" else 2_100_000)
        except TransportFailure as exc:
            # A source deadline is lane-specific. A refused/reset socket is not.
            failure = (WDAReadUnavailable if path.startswith("/source") and exc.category != "transport"
                       else WDATransportUnavailable) if read else WDAOutcomeUnknown
            raise failure(f"WDA {method} {path} failed; no automatic input replay.",
                          category=exc.category, phase=exc.phase) from exc
        if not 200 <= status < 300:
            parsed = parse_json_bytes(body)
            if isinstance(parsed, dict):
                check_response(parsed, path, mutating=not read)
            failure = WDAReadUnavailable if read else WDAOutcomeUnknown
            raise failure(f"WDA {method} {path} returned HTTP {status}.", category="http_error", phase="response")
        return body


def read_only_request(method: str, path: str) -> bool:
    return method == "GET" or method == "POST" and path.endswith("/elements")


def element_identifier(value: object) -> str:
    identifier = (value.get("element-6066-11e4-a52e-4f735466cecf") or value.get("ELEMENT")) if isinstance(value, dict) else None
    if not isinstance(identifier, str) or not identifier:
        raise WDAUnavailable("No valid WDA element reference returned.")
    return identifier


def check_response(payload: dict[str, Any], path: str, *, mutating: bool = False) -> None:
    value = payload.get("value")
    error = value.get("error") if isinstance(value, dict) else None
    status = payload.get("status")
    if isinstance(error, str) and error in {"unknown command", "unknown method", "unsupported operation"} or status == 9:
        raise WDAUnsupportedCommand(f"WDA {path}: command unsupported by this runner.")
    if not mutating and (error == "stale element reference" or status == 10):
        raise WDAStaleElement("Native element reference expired; only the read may be repeated.")
    if not mutating and error == "invalid session id":
        raise WDATransportUnavailable("Native session expired.", category="session_lost", phase="response")
    if error or status not in (None, 0):
        # Server messages can echo typed text, URLs or accessibility content.
        failure = WDAOutcomeUnknown if mutating else WDAReadUnavailable
        raise failure(f"WDA {path} returned a protocol error. Inspect state before retrying.", category="protocol_error", phase="response")


@dataclass(frozen=True)
class WDARunConfig:
    device_id: str
    wda_path: Path
    scheme: str = DEFAULT_WDA_SCHEME
    configuration: str = DEFAULT_WDA_CONFIGURATION
    developer_dir: str | None = None
    destination_timeout: int = 30
    development_team: str | None = None
    runner_bundle_id: str | None = None
    allow_provisioning_updates: bool = False


def resolve_wda_path(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    elif os.environ.get("OPENCLAW_IPHONE_WDA_PATH"):
        candidates.append(Path(os.environ["OPENCLAW_IPHONE_WDA_PATH"]).expanduser())

    for candidate in candidates:
        if candidate.exists():
            return candidate

    if candidates:
        raise WDASetupError(f"WDA path does not exist: {candidates[0]}")

    raise WDASetupError(
        "No WebDriverAgent checkout was found. Pass --wda-path or set OPENCLAW_IPHONE_WDA_PATH "
        "to an actual WebDriverAgent checkout containing WebDriverAgent.xcodeproj or "
        "WebDriverAgent.xcworkspace. Marker/cache files are not enough."
    )


def find_xcode_container(wda_path: Path) -> tuple[str, Path]:
    if wda_path.is_dir() and wda_path.suffix in {".xcodeproj", ".xcworkspace"}:
        return ("-workspace" if wda_path.suffix == ".xcworkspace" else "-project", wda_path)

    workspaces = sorted(wda_path.glob("*.xcworkspace"))
    if workspaces:
        return "-workspace", workspaces[0]

    projects = sorted(wda_path.glob("*.xcodeproj"))
    if projects:
        return "-project", projects[0]

    raise WDASetupError(
        f"No .xcodeproj or .xcworkspace found in {wda_path}. "
        "Pass --wda-path to the WebDriverAgent project/workspace directory."
    )


def build_xcodebuild_command(config: WDARunConfig) -> list[str]:
    if config.destination_timeout <= 0:
        raise WDASetupError("Xcode destination timeout must be positive.")
    container_flag, container_path = find_xcode_container(config.wda_path)
    command = [
        "xcodebuild",
        "test",
        container_flag,
        str(container_path),
        "-scheme",
        config.scheme,
        "-configuration",
        config.configuration,
        "-destination",
        f"id={config.device_id}",
        "-destination-timeout",
        str(config.destination_timeout),
    ]
    if config.allow_provisioning_updates:
        command.append("-allowProvisioningUpdates")
    if config.development_team:
        command.append(f"DEVELOPMENT_TEAM={config.development_team}")
    if config.runner_bundle_id:
        command.append(f"PRODUCT_BUNDLE_IDENTIFIER={config.runner_bundle_id}")
    return command


def run_wda(config: WDARunConfig) -> int:
    developer_dir = resolve_developer_dir(config.developer_dir)
    env = os.environ.copy()
    if developer_dir:
        env["DEVELOPER_DIR"] = developer_dir

    command = build_xcodebuild_command(config)
    # launchd must supervise xcodebuild itself, not an intermediate Python parent.
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(command[0], command, env)


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WDASetupError("WDA URL must be an HTTP(S) endpoint without credentials, query or fragment.")
    return url.rstrip("/")


def parse_json_bytes(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_ready(payload: dict[str, Any]) -> bool | None:
    value = payload.get("value")
    if isinstance(value, dict):
        ready = value.get("ready")
        if isinstance(ready, bool):
            return ready
        state = value.get("state")
        if isinstance(state, str) and state.lower() in {"success", "ready"}:
            return True

    ready = payload.get("ready")
    if isinstance(ready, bool):
        return ready

    if payload.get("status") == 0:
        return True

    return None
