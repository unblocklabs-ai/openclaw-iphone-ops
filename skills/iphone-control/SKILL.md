---
name: iphone-control
description: Control or diagnose a USB-connected physical iPhone through one persistent WebDriverAgent session. Use for real-device observation, direct taps/swipes/input, app transitions, connection failures and WDA setup; not Simulator work.
---

# iPhone control

Use the installed `openclaw-iphone`; check `--help` before assuming the host has
the new `session` interface. This checkout's interface is unreleased; older
v0.5.3 installs use the retired API. From this checkout, substitute
`PYTHONPATH=src python3 -m openclaw_iphone`.

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
- Inspect unknown/partial outcomes before retrying. The session never replays
  writes; uncertainty does not block subsequent deliberate requests.
- `close`/EOF releases ownership; exit 0 says nothing about task achievement.

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
