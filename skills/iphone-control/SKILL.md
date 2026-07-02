---
name: iphone-control
description: Control, diagnose, and automate a plugged-in physical iPhone from an OpenClaw Mac via Apple CoreDevice, WebDriverAgent, devicectl, launchd runner service, screenshots/source, taps, typing, drags, and app launch flows. Use for real iPhone control, WDA setup or troubleshooting, lock-state checks, and live UI automation; do not use for iOS Simulator work.
---

# iPhone Control

## Overview

Use this skill when an OpenClaw needs to control the physically connected iPhone from the Mac. The canonical code lives in the host's `OPENCLAW_IPHONE_REPO_DIR`, normally `~/.openclaw/repos/openclaw-iphone`; make local changes there and treat that repo as the source to push and reuse on other OpenClaws.

The supported transport is Apple CoreDevice plus WebDriverAgent. Do not start `iproxy`, do not rely on `localhost:8100`, and do not restore the old `iphone-wda-tunnel` launchd service.

Host-specific settings live in `~/.openclaw/iphone/config.env` or a repo-local `.env`, copied from `.env.example`. Do not set `OPENCLAW_IPHONE_WDA_URL` for normal USB-connected operation; the CLI resolves the live WDA URL dynamically.

## First-Time Host Setup

Before expecting live control on a new OpenClaw, verify the host setup in this order:

1. Install full Xcode and make sure `xcode-select -p` can reach `/Applications/Xcode.app/Contents/Developer`, or set `DEVELOPER_DIR` for the command/service context.
2. Create `~/.openclaw/iphone/config.env` from `.env.example`, then set `OPENCLAW_IPHONE_DEVICE`, `OPENCLAW_IPHONE_REPO_DIR`, `OPENCLAW_IPHONE_WDA_PATH`, `OPENCLAW_IPHONE_DEVELOPMENT_TEAM`, and a unique `OPENCLAW_IPHONE_RUNNER_BUNDLE_ID`.
3. Pair and trust the phone, enable Developer Mode, unlock it after boot, and set Auto-Lock to Never when policy allows.
4. In Xcode, add the Apple ID, create or select an Apple Development certificate, select `WebDriverAgentRunner`, enable automatic signing, choose the team, set the unique runner bundle id, select the physical iPhone, and run `Product -> Test`.
5. Trust the developer profile on the phone if prompted, and approve keychain/codesign private-key prompts with Always Allow.
6. If headless signing fails with `errSecInternalComponent`, stop and report that the host-local keychain/private-key ACL must be fixed. Do not print, request, or commit Apple credentials or keychain passwords.

Only move to the baseline check after WDA has been run successfully at least once or the launchd runner has been installed and started. If any Apple/Xcode/phone-side trust or signing prompt appears, stop for human setup instead of looping on `wda status`.

## Baseline Check

Start from the repo root:

```bash
cd "${OPENCLAW_IPHONE_REPO_DIR:-$HOME/.openclaw/repos/openclaw-iphone}"
PYTHONPATH=src python3 -m openclaw_iphone wda url
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone wda locked
PYTHONPATH=src python3 -m openclaw_iphone doctor
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
```

`wda url` should resolve a CoreDevice tunnel URL such as `http://[...]:8100` from `devicectl device info details` for the configured iPhone. The normal status path should report `reachable: true` and `ready: true`.

Prevent locking by setting the agent phone to `Settings -> Display & Brightness -> Auto-Lock -> Never` when device policy allows it. If the phone is passcode-locked, ask the user to unlock it. WDA can report lock state, but do not assume the agent can enter the device passcode.

## Runtime Service

The WDA runner launch agent is `com.openclaw.iphone-wda-run`. It builds and runs WebDriverAgent from the canonical repo and should be pointed at this repo, not a staged copy.

The optional watchdog launch agent is `com.openclaw.iphone-watchdog`. It runs `watchdog once` periodically, attempts one WDA unlock only if WDA reports the phone is locked, and exits. It must not send fake taps or gestures as a heartbeat.

Useful commands:

```bash
launchctl print "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl print "gui/$(id -u)/com.openclaw.iphone-watchdog"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-watchdog"
tail -n 120 ~/Library/Logs/openclaw/iphone-wda-run.log
tail -n 120 ~/Library/Logs/openclaw/iphone-wda-run.err.log
tail -n 120 ~/Library/Logs/openclaw/iphone-watchdog.log
tail -n 120 ~/Library/Logs/openclaw/iphone-watchdog.err.log
```

The removed legacy service was `com.openclaw.iphone-wda-tunnel`. If it appears again, stop and remove it rather than treating it as a backup path.

## UI Control

Prefer source-backed or screenshot-backed commands. Capture current evidence before raw-coordinate actions.

Common commands:

```bash
PYTHONPATH=src python3 -m openclaw_iphone ui elements
PYTHONPATH=src python3 -m openclaw_iphone ui annotated-screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui tap-text "Settings"
PYTHONPATH=src python3 -m openclaw_iphone ui tap --x 120 --y 700
PYTHONPATH=src python3 -m openclaw_iphone ui type "search text"
PYTHONPATH=src python3 -m openclaw_iphone ui drag --from-x 360 --from-y 780 --to-x 360 --to-y 450 --duration 0.2
PYTHONPATH=src python3 -m openclaw_iphone apps launch Instagram
```

Use semantic labels first (`tap-text`, `elements`, `source`). Use coordinates only after checking the current screenshot or accessibility tree. Verify every meaningful action with another screenshot, source dump, or elements listing.

Some hardware-button endpoints are unavailable on the current WDA build. If `press-button home` fails, use app launches, visible UI controls, or documented project commands instead of assuming the endpoint exists.

## Troubleshooting

If WDA is not reachable:

1. Confirm the iPhone is unlocked and trusted by the Mac.
2. Confirm the launch agents are running and inspect their logs.
3. Run `wda url` and verify CoreDevice returns a tunnel address.
4. Restart only the runner service with `launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"`.
5. If Xcode signing or trust prompts appear, ask the user to handle the iPhone or Xcode prompt and then retry.

When reporting blockers, include the exact command, stderr/stdout summary, whether the device was locked, and whether screenshot/source retrieval worked.
