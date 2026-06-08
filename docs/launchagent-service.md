# WDA LaunchAgent Service

This is the proposed always-on service shape for OpenClaw iPhone control.

WebDriverAgent requires two long-lived host processes:

- `wda run`: runs `xcodebuild test` and hosts `WebDriverAgentRunner` on the
  phone.
- `wda tunnel`: runs `iproxy` and forwards `127.0.0.1:8100` to the phone.

Use two LaunchAgents rather than one wrapper. They fail and restart
independently: the tunnel can restart when USB hiccups, and the WDA runner can
restart when Xcode, signing, or the phone state changes.

## Preconditions

Validate these manually before installing launchd services:

```sh
xcode-select -p
security find-identity -v -p codesigning
PYTHONPATH=src python3 -m openclaw_iphone devices list
PYTHONPATH=src python3 -m openclaw_iphone wda status
```

Required host state:

- `xcode-select -p` points to `/Applications/Xcode.app/Contents/Developer`.
- The Apple Development private key is available to `codesign`.
- Any keychain prompt has been approved with Always Allow.
- The iPhone is paired, trusted, unlocked after boot, and has trusted the
  developer profile.
- `iproxy` is installed.

Do not store keychain passwords or Apple credentials in these plist templates.
If the host needs keychain unlock at login, keep that in the host-local
OpenClaw keychain unlock agent.

## Files

Templates and wrappers live under `snippets/launchd/`:

- `openclaw-iphone-wda-run.sh`
- `openclaw-iphone-wda-tunnel.sh`
- `com.openclaw.iphone-wda-run.plist.template`
- `com.openclaw.iphone-wda-tunnel.plist.template`

The wrappers set `PATH`, `PYTHONPATH`, and the WDA path before executing the
repo CLI. The plists log to `~/Library/Logs/openclaw/`.

On this Mac, launchd services should not execute directly from `~/Desktop`
because macOS privacy controls can block non-interactive agents from that
location. Stage a service runtime under `~/.openclaw/service-env/iphone-wda`
and point the installed plists there.

## Install

From this repo:

```sh
chmod +x snippets/launchd/openclaw-iphone-wda-run.sh
chmod +x snippets/launchd/openclaw-iphone-wda-tunnel.sh
mkdir -p ~/Library/Logs/openclaw
mkdir -p ~/.openclaw/service-env/iphone-wda/src

cp -R src/openclaw_iphone ~/.openclaw/service-env/iphone-wda/src/
cp snippets/launchd/openclaw-iphone-wda-run.sh \
  ~/.openclaw/service-env/iphone-wda/openclaw-iphone-wda-run.sh
cp snippets/launchd/openclaw-iphone-wda-tunnel.sh \
  ~/.openclaw/service-env/iphone-wda/openclaw-iphone-wda-tunnel.sh
chmod +x ~/.openclaw/service-env/iphone-wda/openclaw-iphone-wda-run.sh
chmod +x ~/.openclaw/service-env/iphone-wda/openclaw-iphone-wda-tunnel.sh

cp snippets/launchd/com.openclaw.iphone-wda-run.plist.template \
  ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
cp snippets/launchd/com.openclaw.iphone-wda-tunnel.plist.template \
  ~/Library/LaunchAgents/com.openclaw.iphone-wda-tunnel.plist

plutil -lint ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
plutil -lint ~/Library/LaunchAgents/com.openclaw.iphone-wda-tunnel.plist

launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-tunnel.plist
launchctl enable "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl enable "gui/$(id -u)/com.openclaw.iphone-wda-tunnel"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-tunnel"
```

If multiple iPhones are connected, add `OPENCLAW_IPHONE_DEVICE` to both plist
`EnvironmentVariables` dictionaries. The value can be the device name,
CoreDevice identifier, or physical UDID accepted by the CLI.

## Verify

```sh
launchctl print "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl print "gui/$(id -u)/com.openclaw.iphone-wda-tunnel"
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-run.log
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-run.err.log
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-tunnel.log
tail -n 80 ~/Library/Logs/openclaw/iphone-wda-tunnel.err.log
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
```

Healthy state:

- `wda status` prints `reachable: true` and `ready: true`.
- Screenshot and source capture both succeed.
- `launchctl print` shows each service as running or recently restarted without
  rapid repeated failures.

## Restart

```sh
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-run"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.iphone-wda-tunnel"
```

Restart the runner first when signing, trust, or Xcode state changed. Restart
the tunnel first when only localhost forwarding is broken.

## Uninstall

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-tunnel.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
rm ~/Library/LaunchAgents/com.openclaw.iphone-wda-tunnel.plist
rm ~/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist
```

## OpenClaw Prompt

Once the LaunchAgents are installed, ask OpenClaw to treat WDA as a service:

```text
Use the plugged-in physical iPhone via the WDA LaunchAgents from
/Users/pearlperelel/Desktop/iphone. First run `PYTHONPATH=src python3 -m
openclaw_iphone wda status`. If WDA is not ready, inspect `launchctl print`
and `~/Library/Logs/openclaw/iphone-wda-*.log`, restart the WDA run/tunnel
LaunchAgents as needed, then verify with status, screenshot, and source before
interacting with apps.
```
