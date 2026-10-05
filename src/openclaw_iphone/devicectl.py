from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .errors import AppNotFound, DeviceSelectionError
from .evidence import artifact_path
from .runner import Runner
from .xcode import resolve_developer_dir


@dataclass(frozen=True)
class Device:
    name: str
    identifier: str
    state: str
    model: str = ""
    udid: str = ""

    @property
    def xcode_identifier(self) -> str:
        return self.udid or self.identifier


@dataclass(frozen=True)
class App:
    name: str
    bundle_identifier: str
    version: str = ""
    bundle_version: str = ""


class DeviceCtl:
    def __init__(
        self,
        *,
        developer_dir: str | None = None,
        evidence_base: str | None = None,
        timeout: int = 30,
    ) -> None:
        resolved = resolve_developer_dir(developer_dir)
        env = {"DEVELOPER_DIR": resolved} if resolved else {}
        self.developer_dir = resolved
        self.runner = Runner(env=env, timeout=timeout)
        self.evidence_base = evidence_base

    def list_devices(self) -> tuple[list[Device], Path]:
        data, output = self._json(["list", "devices"], "devices")
        devices = [_device_from_json(item) for item in find_list(data, "devices")]
        return devices, output

    def device_details(self, device_id: str) -> tuple[dict[str, Any], Path]:
        return self._json(["device", "info", "details", "--device", device_id], "device-details")

    def _json(self, arguments: list[str], name: str) -> tuple[dict[str, Any], Path]:
        output = artifact_path(name, base=self.evidence_base)
        self.runner.run(["xcrun", "devicectl", *arguments, "--json-output", str(output)])
        return read_json(output), output

    def coredevice_wda_url(self, device_id: str, *, port: int = 8100) -> tuple[str, Path]:
        data, output = self.device_details(device_id)
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise DeviceSelectionError("CoreDevice details response did not include a result object.")

        connection = result.get("connectionProperties", {})
        if not isinstance(connection, dict):
            raise DeviceSelectionError("CoreDevice details response did not include connection properties.")

        tunnel_state = connection.get("tunnelState")
        tunnel_ip = connection.get("tunnelIPAddress")
        if tunnel_state != "connected" or not tunnel_ip:
            raise DeviceSelectionError(
                "CoreDevice tunnel is not connected for the selected iPhone; "
                "keep the phone plugged in, paired, trusted, and visible to Xcode."
            )

        return f"http://{url_host(str(tunnel_ip))}:{port}", output

    def select_device(self, requested: str | None = None) -> Device:
        requested = requested or None
        devices, _ = self.list_devices()
        connected = [
            device for device in devices
            if device.state.lower() == "connected" and device.identifier
            and device.model.casefold().startswith("iphone")
        ]
        if requested:
            # Explicit identity is not an availability probe. The native
            # command determines whether the selected phone is reachable.
            exact = [d for d in devices if d.identifier and d.model.casefold().startswith("iphone")
                     and requested.casefold() in {d.identifier.casefold(), d.udid.casefold()}]
            if len(exact) > 1:
                raise DeviceSelectionError("Device identity matched multiple records.")
            if exact:
                return exact[0]
            matches = [
                device
                for device in connected
                if requested in {device.identifier, device.name, device.udid}
                or requested.lower() in {device.identifier.lower(), device.name.lower(), device.udid.lower()}
            ]
            if not matches:
                raise DeviceSelectionError(f"No connected iPhone matched {requested!r}.")
            if len(matches) > 1:
                raise DeviceSelectionError(f"Device selector {requested!r} matched multiple devices.")
            return matches[0]

        if len(connected) == 1:
            return connected[0]
        if not connected:
            raise DeviceSelectionError("No connected iPhone was found.")
        names = ", ".join(f"{device.name} ({device.identifier})" for device in connected)
        raise DeviceSelectionError(
            "Multiple devices found; set OPENCLAW_IPHONE_DEVICE in config.env "
            f"or pass --device where supported. Candidates: {names}"
        )

    def lock_state(self, device_id: str) -> tuple[dict[str, Any], Path]:
        return self._json(["device", "info", "lockState", "--device", device_id], "lock-state")

    def list_apps(self, device_id: str, *, include_all: bool = True) -> tuple[list[App], Path]:
        command = [
            "device",
            "info",
            "apps",
            "--device",
            device_id,
        ]
        if include_all:
            command.append("--include-all-apps")
        data, output = self._json(command, "apps")
        result = data.get("result")
        entries = result.get("apps") if isinstance(result, dict) else None
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ValueError("Installed-app inventory is missing or malformed.")
        apps = [_app_from_json(item) for item in entries]
        return apps, output

    def find_app(self, device_id: str, query: str) -> App:
        apps, _ = self.list_apps(device_id, include_all=True)
        return resolve_app(apps, query, device_id)

def resolve_app(apps: list[App], query: str, device_id: str) -> App:
    if not query.strip():
        raise ValueError("App query must be non-empty.")
    query_lower = query.lower()

    exact_bundle = [app for app in apps if app.bundle_identifier == query]
    if exact_bundle:
        return exact_bundle[0]

    exact_name = [app for app in apps if app.name.lower() == query_lower]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_name) > 1:
        raise AppNotFound(f"App name {query!r} matched multiple apps; use an exact bundle identifier.")

    contains = [
        app
        for app in apps
        if query_lower in app.name.lower() or query_lower in app.bundle_identifier.lower()
    ]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        choices = ", ".join(f"{app.name} ({app.bundle_identifier})" for app in contains)
        raise AppNotFound(f"App query {query!r} matched multiple apps: {choices}")
    raise AppNotFound(f"App {query!r} was not found on device {device_id}.")

def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("devicectl JSON response must be an object.")
    return data


def find_list(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        for child in data.values():
            found = find_list(child, key)
            if found:
                return found
    if isinstance(data, list):
        for child in data:
            found = find_list(child, key)
            if found:
                return found
    return []


def _device_from_json(item: dict[str, Any]) -> Device:
    return Device(
        name=first_string(item, ("name", "deviceProperties.name", "properties.name")),
        identifier=first_string(item, ("identifier", "deviceIdentifier", "uuid", "UDID")),
        state=first_string(item, ("state", "connectionState", "connectionProperties.tunnelState")),
        model=first_string(
            item,
            ("model", "hardwareProperties.marketingName", "hardwareProperties.productType", "deviceType"),
        ),
        udid=first_string(item, ("hardwareProperties.udid", "udid", "UDID")),
    )


def _app_from_json(item: dict[str, Any]) -> App:
    return App(
        name=first_string(item, ("name", "localizedName", "displayName")),
        bundle_identifier=first_string(item, ("bundleIdentifier", "bundleID", "identifier")),
        version=first_string(item, ("version", "shortVersionString")),
        bundle_version=first_string(item, ("bundleVersion", "buildVersion")),
    )


def first_string(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = value_at(item, key)
        if value is not None:
            return str(value)
    return ""


def value_at(item: dict[str, Any], dotted_key: str) -> Any:
    current: Any = item
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def url_host(host: str) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host
