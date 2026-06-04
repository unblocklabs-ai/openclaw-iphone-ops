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
- `snippets/`: small reusable shell snippets

