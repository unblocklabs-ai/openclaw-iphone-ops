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
openclaw-iphone ui elements
openclaw-iphone ui annotated-screenshot
openclaw-iphone ui tap-text "Search"
openclaw-iphone ui wait-text "Followers" --timeout 10
openclaw-iphone ui scroll-until-text "pregnancy" --max-scrolls 6
openclaw-iphone ui clear-field "Search"
openclaw-iphone ui back
openclaw-iphone ui tap --x 180 --y 420
openclaw-iphone ui type "text to enter"
openclaw-iphone ui drag --from-x 200 --from-y 700 --to-x 200 --to-y 250 --duration 0.2
openclaw-iphone ui press-button home
openclaw-iphone instagram capture-context --output-dir /tmp/iphone --prefix instagram-current
```

Avoid:

- Committed device identifiers
- Committed credentials
- Local-only paths
- Screen coordinates without screenshot-backed context
- Claims of success without a second verification signal

## UI Control

The reusable UI layer is backed by WebDriverAgent:

```sh
openclaw-iphone wda run --wda-path /path/to/WebDriverAgent
openclaw-iphone wda tunnel
openclaw-iphone wda status
openclaw-iphone ui screenshot
openclaw-iphone ui source
openclaw-iphone ui tap --x 180 --y 420
openclaw-iphone ui type "text to enter"
openclaw-iphone ui drag --from-x 200 --from-y 700 --to-x 200 --to-y 250 --duration 0.2
openclaw-iphone ui press-button home
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

The host must use full Xcode for CoreDevice operations. If `xcode-select -p`
prints `/Library/Developer/CommandLineTools`, ask for or perform the supervised
host setup step:

```sh
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
```

For first-time signing on a host, a human may need to add the Apple ID in Xcode,
create the Apple Development certificate, set automatic signing on
`WebDriverAgentRunner`, pick the physical iPhone, run `Product -> Test`, and
trust the developer profile on the phone. If codesign prompts for private-key
access, the login keychain password may differ from the Mac login password; use
the locally configured keychain password and choose Always Allow. Never print or
commit that password.

`wda tunnel` uses `iproxy` to forward local port 8100 to device port 8100.
Install it with `brew install libimobiledevice` when it is missing. Run `wda
run` and `wda tunnel` in separate foreground or supervised background processes.
If either process exits, UI control breaks. Then verify with `wda status`.

Use UI capture after launching the target app. `wda status` should be the first
check when WebDriverAgent is involved. If WDA is not reachable, report that
exact runtime boundary instead of inventing app-specific workarounds.

For interaction, prefer the accessibility-backed commands first. `ui elements`
returns compact visible labels/values/types/rects. `ui annotated-screenshot`
writes a PNG, element JSON, and HTML overlay. `ui tap-text`, `ui wait-text`, and
`ui scroll-until-text` reduce coordinate guessing. `ui clear-field` clears the
focused field or taps a matched field before clearing. Use absolute WDA
coordinates for `ui tap` and `ui drag` only when source labels are missing or
ambiguous. Use `ui type` only after the intended field is focused. Use
`ui press-button` for hardware buttons such as `home`, `volumeUp`, and
`volumeDown`.

`ui back` is best-effort. Some WDA runners do not expose `/wda/back` or WebDriver
session back routes, so the command falls back to visible back/close/cancel or
top-left controls. If no such control is visible, treat the clean error as a
navigation boundary and choose an app-specific route instead of retrying.

For Instagram search/recommendation work, run `instagram capture-context` after
opening a result grid, reel, or creator profile. The command writes screenshot,
source, and a manifest containing visible video creators, current reel metadata,
and profile follower counts when available. If the phone is locked or Instagram
is not active, treat the manifest warning as a hard stop. To analyze a reel's
content, pair the manifest with a spoofed/extracted Instagram asset URL or local
video file and hand that file/URL to the `video-understand` skill.

For broad creator research on the physical phone, use bounded discovery:

```sh
openclaw-iphone instagram discover-creators \
  --query "pregnancy journey" \
  --max-candidates 10 \
  --deadline-seconds 600 \
  --output-dir /tmp/iphone
```

Discovery opens bounded Instagram hashtag result screens, harvests visible media
handles, deep-links sourced handles to capture profile evidence, and writes JSON
plus Markdown reports. Each candidate includes handle, display name, follower
count when visible, bio, visible pregnancy/motherhood evidence, recency signal
when visible, confidence, caveats, artifact paths, and deep-link verification
status. It records only URL opens, screenshot/source captures, and scroll drags.
It must not like, follow, comment, message, post, enable notifications, or change
account settings.

Run the repeatable benchmark suite with:

```sh
openclaw-iphone instagram benchmark-discovery --output-dir /tmp/iphone
```

The benchmark covers `pregnancy journey`, `first trimester pregnancy nausea`,
and `pregnancy after loss`, then reports candidates found, handles found,
follower counts found, likely-under-10k counts, evidence counts, recency counts,
elapsed time, UI steps, ambiguous screens, and artifact paths.

For a fast source-only pass around the 30-second budget:

```sh
openclaw-iphone instagram benchmark-discovery \
  --verification-mode source-only \
  --max-source-scrolls 0 \
  --output-dir /tmp/iphone
```

Source-only mode returns partial candidates from visible result screens and does
not open profiles. Treat missing follower count, likely-under-10k status, bio,
display name, and deep-link verification as caveats, not negative evidence. Use
the default profile mode when those fields are required.

For the triage-to-shortlist workflow, prefer:

```sh
openclaw-iphone instagram triage-shortlist --output-dir /tmp/iphone
```

This runs source-only triage first, ranks and deduplicates the broad pool, then
profile-verifies only the top candidates. The report separates shortlisted
verified creators, rejected/low-confidence verified candidates, and unresolved
source-only candidates needing manual review. Treat `deep_link_verified:
false` and missing profile fields as unverified, not disqualified.

For known-handle verification, prefer:

```sh
openclaw-iphone instagram verify-handles creator1 creator2 \
  --output-dir /tmp/iphone \
  --max-steps-per-handle 12 \
  --deadline-seconds 45
```

This bounded recipe launches Instagram, attempts a semantic search/open flow for
each known handle, and writes per-handle profile/context/failure artifacts. Use
the step and deadline options to prevent open-ended UI driving. The recipe first
accepts matching current context, then tries the Instagram profile deep link
`instagram://user?username=<handle>`, then falls back to visible Search controls
or visible Instagram follow-up/search-bar fields. The manifest records the deep
link or matched query field so failures are easier to diagnose.

For video analysis handoff:

```sh
openclaw-iphone instagram analyze-video --video "https://example.com/video.mp4" --output-dir /tmp/iphone
```

The command captures current Instagram context, then invokes `video-understand
analyze` with timestamps and JSON output. Use `--dry-run` when Gemini credentials,
network, or a real asset URL are not available; the dry run still validates the
evidence bundle shape.

Screenshot/source artifacts belong in the local evidence directory. Do not
commit them or paste local paths into user-facing chat unless explicitly asked.
