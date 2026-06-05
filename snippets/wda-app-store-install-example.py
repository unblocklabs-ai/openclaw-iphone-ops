#!/usr/bin/env python3
"""Example App Store install flow through WebDriverAgent.

This is intentionally a template. It uses only environment variables and
placeholders so the repo can be shared without local device IDs, credentials, or
machine paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


WDA_URL = os.environ.get("WDA_URL", "http://127.0.0.1:8100").rstrip("/")
APP_NAME = os.environ.get("APP_NAME")
EXPECTED_PUBLISHER = os.environ.get("EXPECTED_PUBLISHER", "")
EXPECTED_BUNDLE_ID = os.environ.get("EXPECTED_BUNDLE_ID", "")
DEVICE_ID = os.environ.get("DEVICE_ID", "")

APP_STORE_BUNDLE_ID = "com.apple.AppStore"


def require(value: str | None, name: str) -> str:
    if not value:
        print(f"Set {name}.", file=sys.stderr)
        sys.exit(2)
    return value


def wda(method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{WDA_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"WDA {method} {path} failed: {exc.code} {detail}") from exc


def find_element(using: str, value: str, timeout: float = 15) -> str:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = wda("POST", "/element", {"using": using, "value": value})
            element_id = result.get("value", {}).get("ELEMENT")
            if element_id:
                return element_id
        except Exception as exc:  # keep polling while UI settles
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"Could not find element {using}={value!r}: {last_error}")


def tap(element_id: str) -> None:
    wda("POST", f"/element/{element_id}/click", {})


def type_text(text: str) -> None:
    wda("POST", "/wda/keys", {"value": list(text)})


def devicectl_app_present(bundle_id: str) -> bool:
    if not DEVICE_ID:
        print("Skipping devicectl proof because DEVICE_ID is unset.")
        return False
    proc = subprocess.run(
        [
            "xcrun",
            "devicectl",
            "device",
            "info",
            "apps",
            "--device",
            DEVICE_ID,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return bundle_id in proc.stdout


def main() -> int:
    app_name = require(APP_NAME, "APP_NAME")

    print("Checking WDA status...")
    status = wda("GET", "/status")
    if not status.get("value"):
        raise RuntimeError("WDA responded without a value payload.")

    print("Launching App Store...")
    wda("POST", "/wda/apps/launch", {"bundleId": APP_STORE_BUNDLE_ID})

    print(f"Searching for {app_name!r}...")
    search_tab = find_element("predicate string", "name == 'Search'")
    tap(search_tab)

    search_field = find_element(
        "predicate string",
        "type == 'XCUIElementTypeSearchField' OR name CONTAINS[c] 'Search'",
    )
    tap(search_field)
    type_text(app_name + "\n")

    print("Waiting for exact result...")
    result = find_element("predicate string", f"name CONTAINS[c] {json.dumps(app_name)}", timeout=30)
    tap(result)

    if EXPECTED_PUBLISHER:
        print(f"Verifying publisher contains {EXPECTED_PUBLISHER!r}...")
        find_element(
            "predicate string",
            f"name CONTAINS[c] {json.dumps(EXPECTED_PUBLISHER)}",
            timeout=10,
        )

    print("Tapping install/open action for the verified app page...")
    action = find_element(
        "predicate string",
        "name IN {'GET', 'Get', 'INSTALL', 'Install'} OR name CONTAINS[c] 'cloud'",
        timeout=20,
    )
    tap(action)

    print("Waiting for final App Store state. Handle secure prompts if they appear.")
    find_element("predicate string", "name == 'OPEN' OR name == 'Open'", timeout=180)

    if EXPECTED_BUNDLE_ID:
        print(f"Verifying installed app bundle {EXPECTED_BUNDLE_ID!r}...")
        if not devicectl_app_present(EXPECTED_BUNDLE_ID):
            raise RuntimeError("App Store reached Open, but devicectl did not show the expected bundle id.")

    print("Install flow reached verified success.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
