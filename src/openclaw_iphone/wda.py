from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from copy import copy
import base64
import binascii
import json
import http.client
import logging
import math
import os
from pathlib import Path
import socket
import time
from typing import Any, Iterator
from xml.etree import ElementTree as ET
import urllib.error
import urllib.parse
import urllib.request

from .errors import DeviceLocked, WDAOutcomeUnknown, WDASetupError, WDAUnavailable, WDAUnsupportedCommand
from .xcode import resolve_developer_dir


DEFAULT_WDA_PORT = 8100
DEFAULT_WDA_SCHEME = "WebDriverAgentRunner"
DEFAULT_WDA_CONFIGURATION = "Debug"


@dataclass(frozen=True)
class WDAStatus:
    url: str
    payload: dict[str, Any]
    ready: bool | None

    @property
    def reachable(self) -> bool:
        return True


class WDAClient:
    def __init__(self, *, url: str | None = None, timeout: int = 30) -> None:
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
        self.deadline: float | None = None
        # Device control must not traverse a host HTTP proxy or follow redirects.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def with_deadline(self, seconds: float | None) -> WDAClient:
        client = copy(self)
        if seconds is not None:
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("Workflow deadline must be finite and positive.")
            deadline = time.monotonic() + seconds
            client.deadline = min(self.deadline, deadline) if self.deadline is not None else deadline
        return client

    def status(self) -> WDAStatus:
        payload = self._json_request("/status")
        return WDAStatus(url=self.url, payload=payload, ready=parse_ready(payload))

    def is_ready(self) -> bool:
        return self.status().ready is True

    def source(self) -> str:
        body = self._request("/source")
        parsed = parse_json_bytes(body)
        if isinstance(parsed, dict):
            check_response(parsed, "/source")
            value = parsed.get("value")
            if isinstance(value, str):
                body = value.encode("utf-8")
        source = body.decode("utf-8", errors="replace")
        try:
            ET.fromstring(source)
        except ET.ParseError as exc:
            raise WDAUnavailable("WDA source was not valid accessibility XML.") from exc
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

    def require_unlocked(self) -> None:
        if self.locked() is not False:
            raise DeviceLocked("WDA screen lock state is locked or unknown; verify unlock before UI actions.")

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
        response: dict[str, Any] = {"value": None}
        for index, char in enumerate(text):
            try:
                response = self._perform_key_press(char)
            except (WDAUnavailable, DeviceLocked) as exc:
                raise WDAOutcomeUnknown(
                    f"Typing stopped after {index} confirmed characters; the next character may have been entered. "
                    "Inspect the field before retrying; do not replay the full text."
                ) from exc
            if frequency is not None and frequency > 0:
                time.sleep(1 / frequency)
        return response

    def clear_text(self) -> dict[str, Any]:
        self.require_unlocked()
        with self.session() as session_id:
            active = self._json_request(f"/session/{session_id}/element/active")
            value = active.get("value")
            element_id = (value.get("element-6066-11e4-a52e-4f735466cecf") or value.get("ELEMENT")) if isinstance(value, dict) else None
            if not isinstance(element_id, str) or not element_id:
                raise WDAUnavailable("No active editable element was identified; focus the intended field first.")
            return self._json_post(f"/session/{session_id}/element/{element_id}/clear", {})

    def _perform_key_press(self, value: str) -> dict[str, Any]:
        return self._perform_session_actions(
            [
                {
                    "type": "key",
                    "id": "keyboard1",
                    "actions": [
                        {"type": "keyDown", "value": value},
                        {"type": "keyUp", "value": value},
                    ],
                }
            ]
        )

    def _perform_session_actions(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        self.require_unlocked()
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/actions", {"actions": actions})

    @contextmanager
    def session(self) -> Iterator[str]:
        session_id = self._create_session()
        try:
            yield session_id
        finally:
            try:
                self._delete_session(session_id)
            except WDAUnavailable:
                logging.getLogger(__name__).warning(
                    "WDA session cleanup failed; action outcome is unchanged. "
                    "Do not replay completed actions. Check WDA before the next workflow."
                )

    def press_button(self, name: str, *, duration: float | None = None) -> dict[str, Any]:
        self.require_unlocked()
        payload: dict[str, Any] = {"name": name}
        if duration is not None:
            payload["duration"] = duration
        with self.session() as session_id:
            return self._json_post(f"/session/{session_id}/wda/pressButton", payload)

    def back(self) -> dict[str, Any]:
        self.require_unlocked()
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
        self.require_unlocked()
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
                        {"type": "pause", "duration": duration_ms},
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
            raise WDAUnavailable(f"WDA {path} response was not a JSON object.")
        check_response(parsed, path)
        return parsed

    def _json_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._request(path, method="POST", payload=payload)
        parsed = parse_json_bytes(body)
        if not isinstance(parsed, dict):
            raise WDAOutcomeUnknown(f"WDA {path} response was not a JSON object. Outcome unknown; inspect state before retrying.")
        check_response(parsed, path, mutating=True)
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
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise WDAUnavailable("Workflow deadline expired before sending a WDA request.")
            timeout = min(timeout, remaining)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            try:
                parsed = parse_json_bytes(exc.read())
            finally:
                exc.close()
            if isinstance(parsed, dict):
                check_response(parsed, path, mutating=method != "GET")
            error = WDAOutcomeUnknown if method != "GET" else WDAUnavailable
            raise error(f"WDA {method} {path} failed with HTTP {exc.code}. Inspect state before retrying.") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException) as exc:
            error = WDAOutcomeUnknown if method != "GET" else WDAUnavailable
            raise error(f"WDA {method} {path} transport failed. Outcome unknown; inspect state before retrying.") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_response(payload: dict[str, Any], path: str, *, mutating: bool = False) -> None:
    value = payload.get("value")
    error = value.get("error") if isinstance(value, dict) else None
    status = payload.get("status")
    if isinstance(error, str) and error in {"unknown command", "unknown method", "unsupported operation"} or status == 9:
        raise WDAUnsupportedCommand(f"WDA {path}: command unsupported by this runner.")
    if error or status not in (None, 0):
        # Server messages can echo typed text, URLs or accessibility content.
        failure = WDAOutcomeUnknown if mutating else WDAUnavailable
        raise failure(f"WDA {path} returned a protocol error. Inspect state before retrying.")


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
