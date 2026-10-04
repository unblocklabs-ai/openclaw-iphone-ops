---
name: iphone-control
description: Control or diagnose a USB-connected physical iPhone through one persistent WebDriverAgent session. Use for real-device observation, direct taps/swipes/input, app transitions, connection failures and WDA setup; not Simulator work.
---

# iPhone control

Use the installed `openclaw-iphone` and retain one `session` process.
Read `ready.capabilities` for supported optional features. Use checkout
commands only for explicitly requested development/testing, never as fallback
when an installed operator workflow is inconvenient.

Read [the session protocol](../../docs/session.md), then retain **one** `session`
process and its pipes across requests. The agent chooses actions and judges
completion. No task file, grants, second model, quota or reconciliation step.

- `observe` returns AX and a screenshot when `--allow-images` is enabled,
  otherwise AX only. Choose an explicit mode when only one is needed. AX shows
  text, controls, bounds, state and short IDs; `parent` links meaningful groups,
  without native layout noise. Text values are omitted. If `next_offset` is
  present, page that same snapshot with `observe` + `offset`; earlier IDs stay
  valid. A fresh offset-0 observation replaces the snapshot.
- Tap a semantic selector/returned ID or coordinates. Coordinate swipes and
  images do not need AX. Device points are default; image pixels explicitly
  use `space: image` and the returned image geometry.
- Type inserts by default; `mode: replace` explicitly clears first. Prefer
  native input; select sequential only for field compatibility. Use private
  owner-only `text_ref` files for credentials, never CLI arguments or logs.
- Use `press`, `launch`, and `open_url` for ordinary transitions. Optional
  `observe` attaches the next view without changing acknowledged dispatch.
- Where supported, use native `pick` for picker wheels rather than repeated
  screenshot/swipe decisions. Supply exact locale-specific values; adjust
  dependent date components deliberately. Native readback is separate from
  acknowledgement, and bounds stop adjustment—not the agent's whole task.
- Optional `launch.wait_seconds` checks foreground before capture. Optional
  `type.verify` compares whole-field replacement privately, returning only
  match/mismatch/unknown. Neither adds mandatory verification or blind retries.
- Inspect unknown/partial outcomes before retrying. The session never replays
  writes; uncertainty does not block subsequent deliberate requests.
- `close`/EOF releases ownership; exit 0 says nothing about task achievement.

Read one complete reply promptly per request; avoid empty polling. Reuse its
attached view and display image results directly when your tool supports it,
instead of an extra inspection step. AX and image are independent, timestamped
captures; select the useful modality. Route timing and safe phases explain
failures without exposing text. Don't add a planner wrapper.

Do not add status/doctor/screenshot/source chains before every action. Use
`doctor --check-ui` after connection failures. Read recovery stays pinned to the
same physical UDID. A locked/passcode-required phone or failed signing needs
appropriate operator recovery, not another phone or automatic service cycling.

Images and AX labels may disclose private content. Masks hide only supplied
rectangles, not all secrets. App/account policy belongs to caller instructions;
the controller does not authorize purchases, messages, deletion or consent.
Close the session before separate mutation commands. See
[troubleshooting](../../docs/troubleshooting.md) and
[service setup](../../docs/launchagent-service.md) only when those boundaries matter.
