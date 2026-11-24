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
- `openclaw-iphone-d1-serve.sh` (optional local Apple Silicon MLX `d1-omni-600M` server)
- `install-d1-serve-launchagent.sh`
- `com.openclaw.iphone-d1-serve.plist.template`

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

snippets/launchd/install-wda-run-launchagent.sh

launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
launchctl enable "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"
```

### Fast WDA restarts (`build-for-testing` / `test-without-building`)

To avoid recompiling `WebDriverAgentRunner` on every launchd restart (20-45 s -> ~3 s),
prebuild once and save the `.xctestrun` path into `~/.openclaw/iphone/config.env`:

```sh
openclaw-iphone wda build --allow-provisioning-updates --save-config
```

Once `OPENCLAW_IPHONE_WDA_XCTESTRUN` is set, `wda run` (including the LaunchAgent)
automatically uses `xcodebuild test-without-building`. Re-run `wda build` after Xcode
or WDA updates. Run `wda build` from the logged-in macOS GUI session (or after
unlocking the login keychain); non-GUI SSH sessions can fail `codesign` with
`errSecInternalComponent`.

### Optional watchdog

Install only when you want between-session lock recovery. It deliberately skips
while a persistent session owns the phone, including caller idle time.

```sh
snippets/launchd/install-watchdog-launchagent.sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist
launchctl enable "gui/$(id -u)/com.openclaw.iphone-watchdog"
```

The watchdog interval defaults to 120 seconds. Override at install time with
`OPENCLAW_IPHONE_WATCHDOG_INTERVAL=300 snippets/launchd/install-watchdog-launchagent.sh`.
To let the watchdog automatically kickstart `com.openclaw.iphone-wda-run` after
consecutive WDA failures (e.g. a hung runner), set `OPENCLAW_IPHONE_WATCHDOG_RESTART_AFTER=3`
in `~/.openclaw/iphone/config.env` (disabled by default: `0`).

### Optional local MLX `d1-omni-600M` service

On Apple Silicon hosts, run `d1-omni-600M` as an always-on loopback service on `127.0.0.1:8781`:

```sh
openclaw-iphone d1 install --prefetch --configure-navigator   # or --configure-guard
snippets/launchd/install-d1-serve-launchagent.sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-d1-serve.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-d1-serve.plist
launchctl enable "gui/$(id -u)/com.openclaw.iphone-d1-serve"
openclaw-iphone d1 status
```

For every unattended host, set `OPENCLAW_IPHONE_DEVICE` in
`~/.openclaw/iphone/config.env`. The value can be the device name, CoreDevice
identifier, or physical UDID accepted by the CLI.
Prefer the physical UDID; names can change or collide. Both wrappers refuse to
start without a selector. Installation preserves a custom config file's absolute
path in the plist, so launchd does not silently switch to a different config.

Set `OPENCLAW_IPHONE_DEVELOPMENT_TEAM` and
`OPENCLAW_IPHONE_RUNNER_BUNDLE_ID` in the host config. These are host settings
and should not be hard-coded into the WDA checkout or committed repo files.

## Verify

Run once after setup, or when diagnosing the service—not before each task.

```sh
launchctl print "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl print "gui/$(id -u)/com.openclaw.iphone-watchdog"
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-run.log
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-run.err.log
tail -n 80 ~/Library/Logs/openclaw/iphone-watchdog.log
tail -n 80 ~/Library/Logs/openclaw/iphone-watchdog.err.log
PYTHONPATH=src python3 -m openclaw_iphone doctor --check-ui
```

Healthy state:

- Doctor reports the pinned device, a ready/unlocked WDA and a usable UI read.
- Capture a screenshot separately only if diagnosing the visual path.
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

The runner replaces its Python process with `xcodebuild`, so launchd supervises
the actual long-lived process. KeepAlive restarts exited processes, **not hung
ones**. By default (`OPENCLAW_IPHONE_WATCHDOG_RESTART_AFTER=0`), the watchdog
checks lock state without restarting WDA; when opted in (`>= 1`), it kickstarts
`com.openclaw.iphone-wda-run` after that many consecutive WDA failures. It never
repairs trust/signing, unlocks a passcode, or guarantees recovery after reboot.
These are per-user GUI LaunchAgents: the user must be logged in. Provisioning
and signing expiry still require operator attention.

The installers pre-create owner-only logs and all plists set umask `077`.
Existing logs are preserved and restricted, not erased; the `wda-run` and
`d1-serve` wrappers rotate logs over 20 MiB on startup (keeping one `.1` backup),
while evidence files are not automatically pruned. Routine
device checks (each watchdog pass, each session start) keep no files; only
commands that print an `evidence:` path, and session screenshots, write evidence. The
watchdog and foreground CLI mutations use one per-user control lock; busy
watchdog passes exit without device actions.

## Uninstall

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-d1-serve.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist \
      ~/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist \
      ~/Library/LaunchAgents/com.openclaw.iphone-d1-serve.plist
```

## OpenClaw Prompt

Once the LaunchAgent is installed, ask OpenClaw to treat WDA as a service:

```text
Use the plugged-in physical iPhone via the WDA LaunchAgent from
the installed openclaw-iphone interface and host config at
~/.openclaw/iphone/config.env. Use one direct session; acquisition establishes the
native session and physical device identity. If acquisition fails, use `doctor --check-ui`,
then inspect `launchctl print` and
`~/Library/Logs/openclaw/iphone-wda-run*.log`. If lock recovery is failing,
inspect `~/Library/Logs/openclaw/iphone-watchdog*.log`. Restart the relevant
LaunchAgent only after authorization and diagnosis, then reacquire the session.
Reuse action results; do not add a status/screenshot/source preflight chain.
```
