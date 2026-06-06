# OpenClaw iPhone Operations

This repo documents a reusable lane for controlling a USB-connected physical iPhone from an OpenClaw/Codex agent host.

It is intentionally local-only for now. It contains no secrets, no machine-specific credential values, and no workstation-specific paths.

## What This Enables

- Discovering a connected iPhone
- Checking lock state before foreground automation
- Launching and terminating apps
- Inspecting processes and installed apps
- Installing apps when device state and signing allow it
- Driving real UI interactions through XCUITest or WebDriverAgent
- Capturing durable evidence for install and troubleshooting flows

## Operating Model

Use the physical iPhone first. Do not default to the iOS Simulator for tasks that ask for "the phone" or require App Store behavior.

The control stack is:

1. `xcrun devicectl` for device discovery, lock state, app/process inspection, installs, launches, and terminations.
2. A signed XCUITest runner for taps, typing, scrolling, and App Store navigation on the real device.
3. WebDriverAgent when a reusable remote-control API is useful.

## Required State

The iPhone must be:

- Connected over USB
- Trusted by the host
- Awake and unlocked for foreground UI work
- Developer Mode enabled
- Available to Xcode/CoreDevice tooling

If the phone is locked, foreground app launches and UI tests are blocked until a human unlocks it.

## Quick Start

Run the reusable Python CLI directly from a checkout:

```sh
PYTHONPATH=src python3 -m openclaw_iphone devices list
```

Launch an app by display name or bundle id:

```sh
PYTHONPATH=src python3 -m openclaw_iphone apps launch Instagram
PYTHONPATH=src python3 -m openclaw_iphone apps launch com.burbn.instagram
```

Run the first optional app recipe:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram smoke
```

Check WebDriverAgent and capture the current UI when WDA is already running:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
```

The WDA URL defaults to `OPENCLAW_IPHONE_WDA_URL`, then
`http://127.0.0.1:8100`. Pass `--url` to target a different endpoint.

Run WebDriverAgentRunner from a local WDA checkout:

```sh
OPENCLAW_IPHONE_WDA_PATH="/path/to/WebDriverAgent" \
PYTHONPATH=src python3 -m openclaw_iphone wda run
```

This must be an actual WebDriverAgent Xcode project, such as Appium's maintained
fork, with signing/provisioning configured for the physical iPhone. Cache marker
files are not enough.

When signing is not already configured in the project, pass Xcode build settings
through the CLI:

```sh
OPENCLAW_IPHONE_WDA_PATH="/path/to/WebDriverAgent" \
PYTHONPATH=src python3 -m openclaw_iphone wda run \
  --development-team "<team-id>" \
  --runner-bundle-id "com.example.WebDriverAgentRunner" \
  --allow-provisioning-updates
```

In a second process, forward the device WDA port when `iproxy` is installed:

```sh
brew install libimobiledevice
PYTHONPATH=src python3 -m openclaw_iphone wda tunnel
```

Both commands stay in the foreground while their underlying process is alive. If
either exits, UI control breaks and needs to be restarted or supervised.
After the runner and tunnel are up, `wda status`, `ui screenshot`, and
`ui source` should talk to `http://127.0.0.1:8100`.

The CLI resolves full Xcode through `DEVELOPER_DIR`, the current
`xcode-select` path, or common Xcode install locations. It does not change the
host's global Xcode selection.

Run a basic device check:

```sh
./snippets/iphone-doctor.sh
```

Check a specific device:

```sh
DEVICE_ID="<udid-or-device-name>" ./snippets/iphone-lock-state.sh
```

Launch an app by bundle id:

```sh
DEVICE_ID="<udid-or-device-name>" BUNDLE_ID="com.apple.mobilesafari" ./snippets/iphone-launch-app.sh
```

Check WebDriverAgent if it is already running:

```sh
WDA_URL="http://127.0.0.1:8100" ./snippets/wda-smoke.sh
```

Run the App Store install example after WDA is already reachable:

```sh
WDA_URL="http://127.0.0.1:8100" \
APP_NAME="Example App" \
EXPECTED_PUBLISHER="Example Publisher" \
EXPECTED_BUNDLE_ID="com.example.app" \
DEVICE_ID="<udid-or-device-name>" \
python3 ./snippets/wda-app-store-install-example.py
```

## Human Intervention Boundaries

Escalate only for concrete boundaries:

- Unlocking the phone
- Face ID or passcode prompts
- Apple enrollment, trust, or secure confirmation prompts that automation cannot complete
- Xcode signing team setup
- Provisioning profile or codesign failures
- Rejected stored credentials or missing credential material

Do not call the iPhone unavailable just because a first automation path fails. Read the error, verify the device state, and try the next appropriate lane.

## Evidence

Keep task-specific artifacts under a local temp/evidence directory. For repeatable work, capture only the proof needed:

- Live WDA proof when WDA is used
- Exact target screen before risky taps, such as App Store install actions
- Final success or blocker screen
- Fresh `devicectl` proof when installed-app state matters

Do not share screenshots or local evidence paths into user-facing chat unless explicitly asked.

## Files

- `docs/mechanics.md`: detailed operating notes
- `docs/app-store-installs.md`: App Store-specific flow and proof rules
- `docs/troubleshooting.md`: common failures and exact escalation language
- `docs/agent-automation-guide.md`: guidance for writing reusable app recipes
- `src/openclaw_iphone/`: reusable Python primitives and CLI
- `snippets/`: small reusable shell snippets
