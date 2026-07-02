# WDA LaunchAgent Service

This is the always-on service shape for OpenClaw iPhone control.

WebDriverAgent needs one long-lived host process and one optional periodic
recovery check:

- `wda run`: runs `xcodebuild test` and hosts `WebDriverAgentRunner` on the
  plugged-in iPhone.
- `watchdog once`: checks WDA and lock state, attempts one WDA unlock only if
  the screen is locked, and exits.

The CLI reaches WDA through Apple CoreDevice's USB tunnel. Do not run a
separate localhost forwarding LaunchAgent; that path is not part of the
supported runtime for this repo.

The watchdog is not a replacement for iOS setup. Set the agent phone to
`Settings -> Display & Brightness -> Auto-Lock -> Never` when device policy
allows it. The watchdog does not send fake taps or other keep-alive gestures.

## Preflight

Validate these manually before installing launchd services:

```sh
xcode-select -p
security find-identity -v -p codesigning
mkdir -p ~/.openclaw/iphone
cp .env.example ~/.openclaw/iphone/config.env
$EDITOR ~/.openclaw/iphone/config.env
PYTHONPATH=src python3 -m openclaw_iphone devices list
PYTHONPATH=src python3 -m openclaw_iphone wda url
```

Required host state:

- `xcode-select -p` points to `/Applications/Xcode.app/Contents/Developer`.
- The Apple Development private key is available to `codesign`.
- Any keychain prompt has been approved with Always Allow.
- The iPhone is paired, trusted, plugged in, unlocked after boot, and has
  trusted the developer profile.
- `devicectl device info details` reports `connectionProperties.tunnelState` as
  `connected`.
- `~/.openclaw/iphone/config.env` points to this host's iPhone, WDA checkout,
  signing team, runner bundle id, and destination timeout.

Before relying on launchd, run a supervised WDA pass from the repo or Xcode at
least once so Apple Developer signing, profile trust, and keychain prompts are
resolved:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda run --allow-provisioning-updates
```

Stop and handle any Xcode account, Apple Developer certificate, unique runner
bundle id, keychain, or phone-side developer-profile trust prompt. `wda status`
is a post-start verification command; it is expected to fail until WDA is
running.

Do not store keychain passwords or Apple credentials in plist templates. If the
host needs keychain unlock at login, keep that in the host-local OpenClaw
keychain unlock agent.

## Files

Templates and wrappers live under `snippets/launchd/`:

- `openclaw-iphone-wda-run.sh`
- `install-wda-run-launchagent.sh`
- `com.openclaw.iphone-wda-run.plist.template`
- `openclaw-iphone-watchdog.sh`
- `install-watchdog-launchagent.sh`
- `com.openclaw.iphone-watchdog.plist.template`

The wrappers set `PATH` and `PYTHONPATH` before executing the repo CLI. The CLI
reads `~/.openclaw/iphone/config.env` or a repo-local `.env`. The install
scripts read the same config, prefer `OPENCLAW_IPHONE_REPO_DIR` from it, verify
the target repo and wrapper exist, render plist files with structured plist
serialization, and write logs to `~/Library/Logs/openclaw/`.

On managed OpenClaw Macs, launchd services should not execute directly from
`~/Desktop` because macOS privacy controls can block non-interactive agents from
that location. Use the canonical git checkout at
`~/.openclaw/repos/openclaw-iphone` for both local development and launchd
runtime so the service does not drift from the code being edited.

## Install

From this repo:

```sh
chmod +x snippets/launchd/openclaw-iphone-wda-run.sh
chmod +x snippets/launchd/install-wda-run-launchagent.sh
chmod +x snippets/launchd/openclaw-iphone-watchdog.sh
chmod +x snippets/launchd/install-watchdog-launchagent.sh

snippets/launchd/install-wda-run-launchagent.sh
snippets/launchd/install-watchdog-launchagent.sh

launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist 2>/dev/null || true
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist
launchctl enable "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl enable "gui/$(id -u)/com.openclaw.iphone-watchdog"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"
```

The watchdog interval defaults to 120 seconds. Override at install time with
`OPENCLAW_IPHONE_WATCHDOG_INTERVAL=300 snippets/launchd/install-watchdog-launchagent.sh`.

If multiple iPhones are connected, set `OPENCLAW_IPHONE_DEVICE` in
`~/.openclaw/iphone/config.env`. The value can be the device name, CoreDevice
identifier, or physical UDID accepted by the CLI.

Set `OPENCLAW_IPHONE_DEVELOPMENT_TEAM` and
`OPENCLAW_IPHONE_RUNNER_BUNDLE_ID` in the host config. These are host settings
and should not be hard-coded into the WDA checkout or committed repo files.

## Verify

```sh
launchctl print "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl print "gui/$(id -u)/com.openclaw.iphone-watchdog"
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-run.log
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-run.err.log
tail -n 80 ~/Library/Logs/openclaw/iphone-watchdog.log
tail -n 80 ~/Library/Logs/openclaw/iphone-watchdog.err.log
PYTHONPATH=src python3 -m openclaw_iphone wda url
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone doctor
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
```

Healthy state:

- `wda url` prints a CoreDevice tunnel URL, usually an IPv6 URL like
  `http://[...]:8100`.
- `wda status` prints `reachable: true` and `ready: true`.
- Screenshot and source capture both succeed.
- `launchctl print` shows the runner service as running or recently restarted
  without rapid repeated failures.
- `iphone-watchdog.log` shows `result: ok` or `result: unlocked`. If it shows
  `result: human-unlock-required`, a passcode/Face ID boundary needs human input.

## Restart

```sh
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-watchdog"
```

Restart the runner when signing, trust, Xcode state, or phone state changes.

## Uninstall

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist
rm ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
rm ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist
```

## OpenClaw Prompt

Once the LaunchAgent is installed, ask OpenClaw to treat WDA as a service:

```text
Use the plugged-in physical iPhone via the WDA LaunchAgent from
the canonical openclaw-iphone checkout and host config at
~/.openclaw/iphone/config.env. First run `PYTHONPATH=src python3 -m
openclaw_iphone doctor`. If WDA is not ready, inspect `launchctl print` and
`~/Library/Logs/openclaw/iphone-wda-run*.log`. If lock recovery is failing,
inspect `~/Library/Logs/openclaw/iphone-watchdog*.log`. Restart the relevant
LaunchAgent as needed, then verify with status, screenshot, and source before
interacting with apps.
```
