from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..devicectl import Device, DeviceCtl


INSTAGRAM_BUNDLE_ID = "com.burbn.instagram"


@dataclass(frozen=True)
class InstagramSmokeResult:
    device: Device
    bundle_identifier: str
    app_name: str
    lock_state_artifact: Path
    apps_artifact: Path


def smoke(
    client: DeviceCtl,
    *,
    device_selector: str | None = None,
    app_query: str = "Instagram",
) -> InstagramSmokeResult:
    device = client.select_device(device_selector)
    lock_state_artifact = client.require_unlocked(device.identifier)

    app = client.find_app(device.identifier, app_query)
    if app.bundle_identifier != INSTAGRAM_BUNDLE_ID:
        # Keep this as a guardrail, not a dependency of the generic app resolver.
        raise ValueError(
            f"{app_query!r} resolved to {app.name} ({app.bundle_identifier}), "
            f"not expected Instagram bundle {INSTAGRAM_BUNDLE_ID}."
        )

    _, apps_artifact = client.list_apps(device.identifier, include_all=True)
    client.launch_app(device.identifier, app.bundle_identifier)
    return InstagramSmokeResult(
        device=device,
        bundle_identifier=app.bundle_identifier,
        app_name=app.name,
        lock_state_artifact=lock_state_artifact,
        apps_artifact=apps_artifact,
    )
