from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from typing import Any
import urllib.error
import urllib.request

from .errors import WDASetupError, WDAUnavailable
from .xcode import resolve_developer_dir


DEFAULT_WDA_URL = "http://127.0.0.1:8100"
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
        self.url = normalize_url(url or os.environ.get("OPENCLAW_IPHONE_WDA_URL") or DEFAULT_WDA_URL)
        self.timeout = timeout

    def status(self) -> WDAStatus:
        payload = self._json_request("/status")
        return WDAStatus(url=self.url, payload=payload, ready=parse_ready(payload))

    def is_ready(self) -> bool:
        return self.status().ready is True

    def source(self) -> str:
        body = self._request("/source")
        parsed = parse_json_bytes(body)
        if isinstance(parsed, dict):
            value = parsed.get("value")
            if isinstance(value, str):
                return value
        return body.decode("utf-8", errors="replace")

    def screenshot(self) -> bytes:
        body = self._request("/screenshot")
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return body

        parsed = parse_json_bytes(body)
        if isinstance(parsed, dict):
            value = parsed.get("value")
            if isinstance(value, str):
                try:
                    return base64.b64decode(value, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise WDAUnavailable("WDA screenshot response contained invalid base64 data.") from exc

        raise WDAUnavailable("WDA screenshot response was neither raw PNG nor JSON base64.")

    def _json_request(self, path: str) -> dict[str, Any]:
        body = self._request(path)
        parsed = parse_json_bytes(body)
        if not isinstance(parsed, dict):
            raise WDAUnavailable(f"WDA {path} response was not a JSON object.")
        return parsed

    def _request(self, path: str) -> bytes:
        req = urllib.request.Request(f"{self.url}{path}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WDAUnavailable(f"WDA GET {path} failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            raise WDAUnavailable(f"WDA is not reachable at {self.url}: {exc}") from exc


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
    if os.environ.get("OPENCLAW_IPHONE_WDA_PATH"):
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
    if wda_path.is_file() and wda_path.suffix in {".xcodeproj", ".xcworkspace"}:
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
    proc = subprocess.Popen(command, env=env)
    return proc.wait()


def find_iproxy() -> str:
    iproxy = shutil.which("iproxy")
    if not iproxy:
        raise WDASetupError(
            "iproxy is not installed or not on PATH. Install it with "
            "`brew install libimobiledevice`, or use another tunnel provider such as go-ios, then retry."
        )
    return iproxy


def iproxy_command(device_id: str, *, local_port: int = 8100, device_port: int = 8100) -> list[str]:
    iproxy = find_iproxy()
    return [iproxy, "--udid", device_id, f"{local_port}:{device_port}"]


def run_iproxy(device_id: str, *, local_port: int = 8100, device_port: int = 8100) -> int:
    proc = subprocess.Popen(iproxy_command(device_id, local_port=local_port, device_port=device_port))
    return proc.wait()


def normalize_url(url: str) -> str:
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
