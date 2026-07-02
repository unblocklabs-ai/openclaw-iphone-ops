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

For the OpenClaw device lane, set `Settings -> Display & Brightness -> Auto-Lock`
to `Never` when the device policy allows it. This is the preferred fix; unlock
automation is only a fallback.

Run a full health check before agent workflows:

```sh
PYTHONPATH=src python3 -m openclaw_iphone doctor
```

If the phone is locked, agents should try a best-effort WDA unlock once:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda unlock --verify
```

WDA unlock can recover only when iOS does not require passcode, Face ID, or
another secure confirmation. If verification reports `passcode-required: true`,
foreground app launches and UI tests are blocked until a human unlocks it.

For unattended hosts, install the watchdog LaunchAgent from
`docs/launchagent-service.md`. It runs `watchdog once` on a schedule, attempts
one WDA unlock only when WDA reports the screen is locked, and never sends
synthetic keep-alive taps that could interfere with the foreground app.

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

Capture current Instagram context for creator/content analysis:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram capture-context \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops \
  --prefix instagram-current
```

This writes a screenshot, accessibility source, and JSON manifest. The manifest
parses visible recommended video tiles, opened reel metadata, and profile
follower counts when Instagram is active. If the phone is locked or another app
is foregrounded, it writes a warning instead of returning stale context. Use the
manifest with the `video-understand` skill after extracting or spoofing the
corresponding Instagram video asset URL.

Check WebDriverAgent and capture the current UI when WDA is already running:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone wda locked
PYTHONPATH=src python3 -m openclaw_iphone wda unlock --verify
PYTHONPATH=src python3 -m openclaw_iphone watchdog once
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
```

After a screenshot/source confirms the UI state, prefer semantic accessibility
primitives over raw coordinates:

```sh
PYTHONPATH=src python3 -m openclaw_iphone ui elements
PYTHONPATH=src python3 -m openclaw_iphone ui annotated-screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui tap-text "Search"
PYTHONPATH=src python3 -m openclaw_iphone ui wait-text "Followers" --timeout 10
PYTHONPATH=src python3 -m openclaw_iphone ui scroll-until-text "pregnancy" --max-scrolls 6
PYTHONPATH=src python3 -m openclaw_iphone ui clear-field "Search"
PYTHONPATH=src python3 -m openclaw_iphone ui back
```

`ui back` is best-effort across WDA builds. It tries WDA back routes first, then
visible back/close/cancel/top-left controls. If none exist, it exits with a
clean actionable error instead of a raw WDA 404.

Raw WDA interaction primitives are still available when source labels are not
usable:

```sh
PYTHONPATH=src python3 -m openclaw_iphone ui tap --x 180 --y 420
PYTHONPATH=src python3 -m openclaw_iphone ui type "hello from the phone"
PYTHONPATH=src python3 -m openclaw_iphone ui drag --from-x 200 --from-y 700 --to-x 200 --to-y 250 --duration 0.2
PYTHONPATH=src python3 -m openclaw_iphone ui press-button home
```

Coordinates are absolute WDA screen coordinates. Capture a fresh screenshot
before tapping or dragging, and prefer accessibility source when it can identify
the intended target.

For bounded Instagram verification, source candidate handles outside the phone,
then use the logged-in app for evidence:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram verify-handles prenatal.creator another.creator \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops \
  --max-steps-per-handle 12 \
  --deadline-seconds 45
```

For bounded broad Instagram creator discovery on the physical phone:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram discover-creators \
  --query "pregnancy journey" \
  --max-candidates 10 \
  --deadline-seconds 600 \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops
```

Discovery opens bounded Instagram hashtag result screens, harvests visible media
handles, deep-links each sourced handle to capture profile evidence, and writes
both JSON and Markdown reports. Reports include handle, display name, follower
count when visible, likely-under-10k status, bio, visible pregnancy/motherhood
evidence, recency signal when visible, confidence, caveats, artifact paths, and
whether the profile was deep-link verified. The workflow records only URL opens,
captures, and scroll drags; it must not like, follow, comment, message, post,
enable notifications, or change account settings.

Run the benchmark suite for the three Perelel discovery scenarios:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram benchmark-discovery \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops
```

The benchmark writes aggregate JSON/Markdown reports with per-scenario candidate
counts, handles found, follower counts found, likely-under-10k counts, evidence
counts, recency counts, elapsed time, UI steps, ambiguous screen counts, and
artifact paths.

For a fast source-only pass that skips profile deep-links and completes the
three-scenario benchmark in about 30 seconds on the physical phone:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram benchmark-discovery \
  --verification-mode source-only \
  --max-source-scrolls 0 \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops
```

Source-only mode returns partial candidates from visible result screens. It does
not claim follower counts, likely-under-10k status, bio, display name, or
deep-link verification; use the default profile mode when those fields are
required.

For the fastest usable Perelel sourcing workflow, run source triage followed by
top-candidate verification in one command:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram triage-shortlist \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops
```

`triage-shortlist` gathers a source-only pool across the Perelel benchmark
scenarios, ranks and deduplicates candidates by visible pregnancy/motherhood
evidence, source recency, duplicate appearances, and evidence quality, then
deep-link verifies only the top candidates. The final JSON and Markdown reports
separate shortlisted verified creators, rejected/low-confidence verified
candidates, and unresolved source-only candidates needing manual review.

To validate ranking quality across broader pregnancy/motherhood themes and
compare top-ranked candidates with a lower-ranked verification sample:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram benchmark-ranking-quality \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops
```

The ranking benchmark writes aggregate JSON/Markdown reports plus per-theme
triage reports. It tracks top-10 credible-lead yield, top precision, comparison
precision, speed, duplicate rate, unresolved count, and failure modes such as
source shortfalls or missing lower-ranked comparison samples.

To pair current Instagram context with a direct video URL or local video file:

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram analyze-video \
  --video "https://example.com/video.mp4" \
  --output-dir ~/.openclaw/tmp/openclaw-iphone-ops
```

Use `--dry-run` to validate the context/handoff artifacts without invoking
`video-understand`.

Host-specific iPhone settings live in `~/.openclaw/iphone/config.env` or a
repo-local `.env` during development. Start from the committed example:

```sh
mkdir -p ~/.openclaw/iphone
cp .env.example ~/.openclaw/iphone/config.env
$EDITOR ~/.openclaw/iphone/config.env
```

Set the iPhone selector, WDA checkout path, runner bundle id, team id, and
destination timeout there. Do not set a WDA URL for normal USB-connected
operation; the CLI resolves the live Apple CoreDevice tunnel URL dynamically.
Inspect the resolved endpoint with:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda url
```

Run WebDriverAgentRunner from a local WDA checkout:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda run
```

This must be an actual WebDriverAgent Xcode project, such as Appium's maintained
fork, with signing/provisioning configured for the physical iPhone. Cache marker
files are not enough.

The Mac must have full Xcode installed, not just Command Line Tools. CLI
commands resolve full Xcode per command through `DEVELOPER_DIR`, the current
`xcode-select` path, or common Xcode install locations. On managed OpenClaw
hosts, switching the global selection is acceptable when launchd and manual
shells should share the same full-Xcode default:

```sh
xcode-select -p
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
```

The first successful setup may require a manual Xcode pass:

1. Add the Apple ID in Xcode settings and create an Apple Development
   certificate.
2. Select the `WebDriverAgentRunner` target.
3. Enable automatic signing, choose the team, and set a unique runner bundle id.
4. Select the physical iPhone and run `Product -> Test`.
5. On the phone, trust the developer profile if prompted.

If Keychain prompts for codesign/private-key access, use the login keychain
password and choose Always Allow. On OpenClaw-managed hosts, check the local
keychain unlock launch agent or operator notes for the login keychain password;
do not commit that password to this repo.

For unattended hosts, unlocking the keychain is not always enough. If
`codesign` fails with `errSecInternalComponent`, grant Apple tools access to the
private key with `security set-key-partition-list` using the host-local keychain
password. Keep that password in host-local service configuration only.

When signing is not already configured in the project, set
`OPENCLAW_IPHONE_DEVELOPMENT_TEAM` and `OPENCLAW_IPHONE_RUNNER_BUNDLE_ID` in the
host config. CLI flags are still available as debug overrides:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda run \
  --development-team "<team-id>" \
  --runner-bundle-id "com.example.WebDriverAgentRunner" \
  --allow-provisioning-updates
```

When running WDA through launchd, keep host-specific values in
`~/.openclaw/iphone/config.env`, not in the plist template.

Keep the WDA runner process alive. The CLI reaches WDA through Apple
CoreDevice's USB tunnel and does not require a separate localhost forwarding
process.

For always-on agent use, see `docs/launchagent-service.md`. It scopes the
LaunchAgent wrapper for running the WDA runner as a restartable per-user service
with logs.

Validated local example:

```sh
PYTHONPATH=src python3 -m openclaw_iphone wda run \
  --allow-provisioning-updates

PYTHONPATH=src python3 -m openclaw_iphone wda url
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
PYTHONPATH=src python3 -m openclaw_iphone ui tap --x 180 --y 420
```

The CLI does not change the host's global Xcode selection by itself.

## Asking OpenClaw To Use The iPhone

Ask for the phone explicitly and include the repo/path context when useful:

```text
Use the plugged-in physical iPhone via WebDriverAgent from
the canonical openclaw-iphone checkout. Use the host config at
~/.openclaw/iphone/config.env. First verify WDA with
PYTHONPATH=src python3 -m openclaw_iphone wda status. If WDA is not running,
start the WDA runner, then verify with status, screenshot, and source before
interacting with apps.
```

For task-specific work, name the app and desired end state:

```text
Use the physical iPhone, not a simulator. Launch Instagram on the plugged-in
iPhone, verify the UI through WDA screenshot/source, then perform [task].
Stop and report the exact blocker if the phone is locked, WDA is not ready, or
a secure confirmation requires human input.
```

Run a basic device check:

```sh
./snippets/iphone-doctor.sh
```

Check a specific device:

```sh
./snippets/iphone-lock-state.sh
```

Launch an app by bundle id:

```sh
BUNDLE_ID="com.apple.mobilesafari" ./snippets/iphone-launch-app.sh
```

The low-level snippets resolve `OPENCLAW_IPHONE_DEVICE` from
`~/.openclaw/iphone/config.env` when `DEVICE_ID` is not explicitly provided.

Check WebDriverAgent if it is already running:

```sh
./snippets/wda-smoke.sh
```

Run the App Store install example after WDA is already reachable:

```sh
APP_NAME="Example App" \
EXPECTED_PUBLISHER="Example Publisher" \
EXPECTED_BUNDLE_ID="com.example.app" \
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
