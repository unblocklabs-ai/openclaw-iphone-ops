#!/usr/bin/env python3
"""Supervised App Store template; never an unattended purchase/credential handler."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

REPO_DIR = Path(os.environ.get("OPENCLAW_IPHONE_REPO_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO_DIR / "src"))

from openclaw_iphone.config import load_config
from openclaw_iphone.control_lock import control_lock
from openclaw_iphone.devicectl import DeviceCtl
from openclaw_iphone.errors import OpenClawIPhoneError, WDAUnavailable
from openclaw_iphone.ui import UIController
from openclaw_iphone.wda import WDAClient


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name}.")
    return value


def find_element(client: WDAClient, session: str, predicate: str, *, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    bounded = client.with_deadline(timeout)
    while time.monotonic() < deadline:
        response = bounded._json_post(f"/session/{session}/elements", {"using": "predicate string", "value": predicate})
        elements = response.get("value")
        if not isinstance(elements, list):
            raise WDAUnavailable("Element lookup did not return a list.")
        if len(elements) > 1:
            raise WDAUnavailable("Ambiguous App Store controls; inspect the screen manually.")
        if elements:
            element_id = elements[0].get("element-6066-11e4-a52e-4f735466cecf") or elements[0].get("ELEMENT")
            if isinstance(element_id, str) and element_id:
                return element_id
            raise WDAUnavailable("Element response did not include an id.")
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise WDAUnavailable("Timed out waiting for the expected App Store control.")


def tap(client: WDAClient, session: str, element_id: str) -> None:
    client.require_unlocked()
    client._json_post(f"/session/{session}/element/{element_id}/click", {})


def main() -> int:
    app_name = require("APP_NAME")
    publisher = require("EXPECTED_PUBLISHER")
    bundle_id = require("EXPECTED_BUNDLE_ID")
    if os.environ.get("ALLOW_INSTALL") != "1":
        raise ValueError("Supervised installation requires explicit ALLOW_INSTALL=1. No device actions performed.")
    config = load_config(cwd=REPO_DIR)
    device_client = DeviceCtl()
    device = device_client.select_device(os.environ.get("DEVICE_ID") or config.device)
    device_client.require_unlocked(device.identifier)
    url, _ = device_client.coredevice_wda_url(device.identifier)
    if os.environ.get("WDA_URL") or config.wda_url:
        raise ValueError("Remove WDA URL overrides; this template uses the selected device's CoreDevice endpoint.")
    client = WDAClient(url=url)
    if not client.is_ready():
        raise WDAUnavailable("WDA is not ready.")
    client.require_unlocked()
    device_client.launch_app(device.identifier, "com.apple.AppStore")
    controller = UIController(client)

    with client.session() as session:
        visible = "visible == 1 AND enabled == 1"
        search_tab = find_element(client, session, f"{visible} AND type == 'XCUIElementTypeButton' AND name == 'Search'")
        tap(client, session, search_tab)
        field = find_element(client, session, f"{visible} AND type == 'XCUIElementTypeSearchField'")
        tap(client, session, field)
        client.require_unlocked()
        client._json_post(f"/session/{session}/element/{field}/clear", {})
        client.require_unlocked()
        client._json_post(f"/session/{session}/wda/keys", {"value": list(app_name + "\n")})
        result = find_element(client, session, f"{visible} AND name == {json.dumps(app_name)}", timeout=30)
        tap(client, session, result)
        find_element(client, session, f"{visible} AND name == {json.dumps(publisher)}")
        controller.annotated_screenshot()
        # Recommendations may still be on screen. A human confirms this one
        # install action; never infer the intended app from a generic cloud icon.
        if input("Verify the exact app and publisher on the phone. Type INSTALL to continue: ") != "INSTALL":
            raise ValueError("Installation not confirmed.")
        action = find_element(client, session, f"{visible} AND type == 'XCUIElementTypeButton' AND name IN {{'GET', 'Get', 'INSTALL', 'Install'}}")
        tap(client, session, action)
        find_element(client, session, f"{visible} AND name IN {{'OPEN', 'Open'}}", timeout=180)

    apps, _ = device_client.list_apps(device.identifier, include_all=True)
    if not any(app.bundle_identifier == bundle_id for app in apps):
        raise WDAUnavailable("Open was visible, but the expected bundle is not installed. Success unverified.")
    controller.annotated_screenshot()
    print("Expected bundle is installed. No credential or secure-confirmation automation was attempted.")
    return 0


if __name__ == "__main__":
    try:
        with control_lock():
            raise SystemExit(main())
    except (OpenClawIPhoneError, ValueError, OSError, EOFError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
