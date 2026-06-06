# Agent Automation Guide

## Design Rule

Build app-specific automation on top of generic primitives. Do not put device
IDs, local paths, Apple credentials, or one-off screen coordinates into shared
code.

## Configuration Order

Prefer:

1. CLI flags
2. Environment variables
3. Auto-detection when the result is unambiguous

For Xcode, prefer per-command `DEVELOPER_DIR` resolution instead of mutating the
host's global `xcode-select` setting.

## App Automation Pattern

1. Select the connected physical iPhone.
2. Check lock state before foreground actions.
3. Resolve the target app by exact bundle id or exact display name.
4. Launch the app through `devicectl`.
5. Use a UI backend such as WebDriverAgent or XCUITest for taps, typing,
   scrolling, screenshots, and source inspection.
6. Prefer accessibility selectors and visible text over coordinates.
7. Use coordinates only after capturing screenshot evidence for the current
   screen.
8. Keep reusable app flows in optional recipe modules.

## When Writing A New App Recipe

Recipes should be thin wrappers over primitives. A good recipe can hardcode a
known public bundle id, but it should still verify the installed app before
acting.

Good:

```sh
openclaw-iphone apps launch Instagram
openclaw-iphone apps launch com.burbn.instagram
openclaw-iphone wda status
openclaw-iphone ui screenshot
openclaw-iphone ui source
```

Avoid:

- Committed device identifiers
- Committed credentials
- Local-only paths
- Screen coordinates without screenshot-backed context
- Claims of success without a second verification signal

## UI Capture

The first reusable UI layer is intentionally read-only:

```sh
openclaw-iphone wda run --wda-path /path/to/WebDriverAgent
openclaw-iphone wda tunnel
openclaw-iphone wda status
openclaw-iphone ui screenshot
openclaw-iphone ui source
```

Mechanically, `wda run` executes `xcodebuild test` against the
`WebDriverAgentRunner` scheme on the selected physical iPhone. It installs and
keeps the XCTest-hosted WDA process alive on the device. The WDA path must point
to an actual WebDriverAgent Xcode project, usually Appium's maintained fork, and
the project must already be signed/provisioned for the phone. Marker files or
cache directories are not sufficient. If signing is not stored in the project,
use `--development-team`, `--runner-bundle-id`, and optionally
`--allow-provisioning-updates`. If no Apple Development identity or Xcode
account exists on the host, report signing as the blocker.

`wda tunnel` uses `iproxy` to forward local port 8100 to device port 8100.
Install it with `brew install libimobiledevice` when it is missing. Run `wda
run` and `wda tunnel` in separate foreground or supervised background processes.
If either process exits, UI control breaks. Then verify with `wda status`.

Use UI capture after launching the target app. `wda status` should be the first
check when WebDriverAgent is involved. If WDA is not reachable, report that
exact runtime boundary instead of inventing app-specific workarounds.

Screenshot/source artifacts belong in the local evidence directory. Do not
commit them or paste local paths into user-facing chat unless explicitly asked.
