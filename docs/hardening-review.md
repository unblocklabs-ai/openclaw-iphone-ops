# Unattended iPhone hardening review

Reviewed from remote `main` at `7e8b0c4` on 2026-09-21. The starting worktree
was clean. All tracked source, callers, tests, shell/Python examples, launchd
templates/installers, configuration, packaging, skills, docs, and historical
build notes were inspected. No social actions, purchases, installs, device
deletions, reboots, signing changes, or service installations were performed.

## Prioritized findings and fixes

### P1 — Unsafe navigation and ambiguous retries

- `src/openclaw_iphone/ui.py`, `UIController.back`: any top-left button and
  substring labels such as “Back up” could be tapped. Now only one visible,
  non-disabled Back/Go Back button is eligible; ambiguous/missing targets stop.
- `src/openclaw_iphone/wda.py`, `back`: every failure triggered another route,
  potentially navigating twice after a timeout. Fallback now requires an explicit
  WDA unsupported-command response; transport failures never trigger fallback.
- `clear_field` could tap an unrelated Clear button, while `clear_text` silently
  deleted at most 80 characters. Clearing now uses the session's active element,
  with no global Clear-button guess. Text taps refuse multiple distinct targets.

### P1 — Completed actions reported as failures

- `wda.py`, session actions/open URL/Back: DELETE failure replaced a successful
  action result or its original exception. Session cleanup now emits a separate
  warning and preserves the primary outcome. Typing reports confirmed character
  progress and warns against replaying the whole string after partial failure.
- HTTP-200 WDA error payloads were accepted as success. Legacy and W3C errors
  are now rejected; ambiguous mutating failures have `WDAOutcomeUnknown` semantics.
  Raw server messages are not echoed because they can contain sensitive input.

### P1 — Sensitive evidence disclosure and overwrites

- `evidence.py`, UI/context/report writers and shell snippets used shared or
  fixed filenames and default permissions. Every run/capture now uses a private
  unique directory. Python writers use exclusive creation with mode 0600;
  subprocesses inherit umask 077. Prefix traversal and explicit-file overwrite
  are rejected, including existing symlinks/hardlinks.
- WDA requests bypass HTTP proxies and refuse redirects to avoid forwarding
  device-control traffic/evidence to unrelated services.

### P1 — Wrong Instagram identity accepted as verification

- `instagram_ops.py`, both verifiers: any parsed profile could satisfy the
  deep-link path; matching reels/grid tiles could satisfy “profile” verification.
- `instagram_context.py` selected the first handle-shaped static text, including
  hidden/body text. It now requires an unambiguous visible top-header identity,
  excludes hidden subtrees, and supports standard AppiumAUT wrappers.
- Requested/observed identities and mismatch/uncertainty are explicit. Unverified
  profile fields never contribute follower counts/bios to the requested account.
  Failed deep links do not trigger speculative typing into search/AI fields.
- Search query/tag alone no longer establishes topical evidence about a creator.
  The historical ranking/precision claims are explicitly superseded.

### P1 — Lock-state and concurrent-workflow hazards

- UI mutations previously skipped screen-lock checks; unlock could return success
  while the screen was still locked or unknown. Mutations now require WDA's
  explicit unlocked state. `passcodeRequired=false` is not sufficient by itself.
- Normal app launch verifies both signals, with at most one recovery attempt for
  a known non-passcode screen lock. Watchdog refuses passcode/unknown recovery.
- Mutating CLI workflows and watchdog recovery take one nonblocking per-user
  lock so concurrent workflows cannot interleave taps/typing. Python library
  consumers must coordinate their own entire workflows.

### P2 — Device, setup and service correctness

- Device selection no longer falls back to disconnected cached devices or
  selects non-iPhones/unknown models. A selected device is pinned for the CLI
  invocation. Conflicting WDA URL/device selections fail instead of checking one
  phone while controlling another. Duplicate app display names require bundle IDs.
- `apps terminate` passed a bundle ID to `devicectl process terminate`, whose
  installed help requires `--pid`. It now uses WDA's bundle-specific terminate
  route; hardware buttons are also correctly session-scoped.
- Explicit `.xcodeproj`/`.xcworkspace` directories now work, and invalid explicit
  WDA/Xcode paths cannot silently fall back to another checkout/toolchain.
- LaunchAgent wrappers require a configured device, preserve custom config
  paths, and create private logs. The WDA runner execs xcodebuild so launchd
  supervises the actual runner rather than an intermediate Python process.
- Legacy snippets now reuse the CLI's checks/private evidence instead of
  bypassing lock/readiness/timeout contracts. The App Store example uses scoped,
  exact, unambiguous selectors and mandatory supervised installation confirmation
  plus installed-bundle proof. It remains a template, not a certified installer.

### P2 — Bounds, failure reporting and documentation

- Numeric CLI bounds reject negative/non-finite timeouts/deadlines. WDA requests
  and sleeps respect remaining workflow budgets; discovery child verification
  cannot acquire a fresh full budget beyond its parent.
- Missing executables and subprocess timeout byte output are normalized into
  operational errors. Video-analysis timeout/failure still writes a manifest.
- WDA status readiness and handle-verification failures now return nonzero.
  Documentation corrects Back behavior, context command syntax, private evidence,
  service recovery limits, and historical benchmark claims.

## PR review follow-up

- HTTP error-body reads now share the transport-error guard. Truncated/stalled
  cleanup responses cannot replace a successful action or its original failure.
- `clear_field` selects one visible, non-disabled editable target from one source
  snapshot and taps that same target. Same-label buttons cannot replace it;
  missing, disabled, frameless, and ambiguous editable targets fail before taps.
- Removed the unused Instagram query-field fallback helpers and their obsolete
  helper test. The workflow regression forbidding speculative typing remains.

## Validation

- Baseline: `PYTHONPATH=src python3 -m unittest discover -s tests -q` — **98 passed**.
- Hardened, including PR review fixes: same full suite on Python
  **3.12 and 3.14 — 126 passed each**. Python 3.14 also passes with
  `ResourceWarning` treated as an error.
- Regressions exercise cleanup vs action failure, no retry after ambiguous Back,
  unsafe Back/Clear controls, private paths/permissions, identity mismatch and
  unknown identity, device pinning, non-iPhones/disconnected devices, screen-lock
  failures, deadline clipping, concurrency, launchd config/log permissions, and
  App Store authorization/ambiguous selectors.
- Loopback HTTP test uses the real urllib transport with a fake WDA server:
  complete, truncated, and stalled HTTP-500 cleanup preserves successful actions
  and primary action errors, with no proxy use. Clear-field regressions cover
  disabled fields sharing a button label and single-snapshot target selection.
- `python3 -m compileall -q src snippets/wda-app-store-install-example.py`,
  `sh -n` for every shell script, and `git diff --check` pass.
- `uv build` produces sdist and wheel; isolated wheel installation and
  `openclaw-iphone --help` pass. No runtime dependencies were added.
- Added macOS CI for Python 3.11 and 3.14. CI is distinct from physical-device
  coverage; consult the PR checks for its current status.

## Physical-device evidence and remaining limitations

- `xcrun devicectl list devices --timeout 10` exits 69 because this host has not
  accepted the Xcode license. Calling the installed devicectl binary directly
  also prints that warning and reports **No devices found**. The license was not
  accepted. Live signing, trust, taps, typing, unlock, launchd recovery and
  Instagram UI behavior are therefore **unverified** by this change.
- Current Appium WDA source was checked for session-scoped active-element clear,
  hardware buttons and element routes. Installed devicectl help confirmed the
  terminate PID contract. Protocol/unit coverage does not prove compatibility
  with a particular deployed WDA/iOS/Instagram version.
- CoreDevice connected state/tunnel selection does not prove USB-only routing.
  Operators must keep the dedicated cable connected and disable wireless device
  connections if required. Use a physical UDID, not a mutable device name.
- WDA is unauthenticated. Keep its endpoint on trusted transport, never publicly
  exposed. Explicit URL-only debugging trusts the operator's endpoint selection.
- Signing accounts/certificates, provisioning expiry, phone trust, Developer
  Mode, passcode-after-reboot and login-keychain access remain human setup gates.
  No passwords/certificates are added or changed by this PR.
- launchd KeepAlive restarts exited runners, not hung runners. The watchdog is
  a conservative lock check/recovery pass, not an autonomous signing/USB/XCTest
  repair system. A logged-in user is required for these GUI LaunchAgents.
- Timeouts cannot revoke actions already dispatched to iOS. Socket timeouts are
  not hard real-time wall-clock cancellation of slow-streaming responses.
  Expired workflows may skip cleanup/evidence. Observe before retrying.
- Profile-header parsing remains English/UI-layout dependent and deliberately
  rejects unrecognized layouts. Screenshots and XML are sequential, not atomic.
  `recency_signal` means visible media, not proof of a post date; rounded/localized
  follower counts and heuristic topical relevance need human judgment.
- Existing evidence permissions are not migrated and old artifacts are not
  deleted. Owner-only files are not encryption; same-user processes, backups,
  root and stdout/log exports remain privacy boundaries. Retention/rotation is
  operator-managed. The wheel contains the Python CLI; use a checkout for skills,
  docs and LaunchAgent scripts.

## Safe live acceptance pass after host setup

On an unlocked dedicated test phone with known signing/WDA setup: verify the UDID,
`doctor`, readiness, screenshot/source, and private artifact modes. Use a benign
test app to validate one Back, one text clear/type, lock-state refusal and
disconnect/reconnect recovery. Stop on ambiguous outcomes; do not repeat actions.
Test Instagram identity mismatch only with read-only navigation if authorized.
Keep social actions, purchases, app deletions and device resets out of this pass.
