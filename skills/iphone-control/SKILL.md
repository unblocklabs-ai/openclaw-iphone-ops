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
completion. No task file, grants, quota or reconciliation step, and no second
model unless the operator enabled goal navigation.

- `observe` returns AX and a screenshot when `--allow-images` is enabled,
  otherwise AX only. Choose an explicit mode when only one is needed. AX shows
  text, controls, bounds, state and short IDs; `parent` links meaningful groups,
  without native layout noise. Text values are omitted. If `next_offset` is
  present, page that same snapshot with `observe` + `offset`; earlier IDs stay
  valid. A fresh offset-0 observation replaces the snapshot.
- Tap a semantic selector/returned ID or coordinates. Coordinate swipes and
  images do not need AX. Device points are default; image pixels explicitly
  use `space: image`. Conversion fetches dimensions once per image; recapture
  after rotation or layout changes.
- Use `set` with a complete `value` for text, picker options and Boolean states.
  Text replaces; an empty string clears. Native input handles keyboard focus;
  no letter-by-letter agent calls are needed. Literal newlines require TextView.
  `type` is for intentional insertion. Native input is preferred; sequential
  is explicit field compatibility. Credentials use private owner-only
  `value_ref`/`text_ref` files, never CLI arguments or logs.
- Use `press`, `launch`, and `open_url` for ordinary transitions. Optional
  `observe` attaches the next view without changing acknowledged dispatch.
- Set a wheel-style Gregorian date in one request: `set`, picker target,
  `kind: date`, and `value: YYYY-MM-DD`. The controller identifies supported
  components and handles dependencies. `verify: true` optionally reads the final
  date; otherwise native acknowledgement is not proof of the final value. Ambiguous identity
  uses explicit `components`; localized months use `month_values` in calendar
  order. Do not assume screen order or non-Gregorian conversion. For an ordinary
  wheel, `set` sends an exact native option once without hidden adjustments;
  `pick` exposes explicit low-level control when needed. Boolean `set` avoids
  toggling a correct state. Readback uncertainty is not input failure or a
  permanent stop; ordinary controls remain available.
- Optional `launch.wait_seconds` checks foreground before capture. Optional
  `set.verify` reads back the requested value; `type.verify` compares whole-field replacement privately, returning only
  match/mismatch/unknown. Neither adds mandatory verification or blind retries.
- If `ready.capabilities` includes `goal`, the operator enabled goal
  navigation ([goal navigation](../../docs/goal.md)): `goal` runs several steps
  toward a goal you name and returns `done`, `escalate` or `needs_approval`.
  Pre-approve only the effects the user asked for (`approve`), check `done`
  yourself, and handle approvals and escalations directly. It sends screenshots and
  screen text to OpenAI. Without the capability, you choose every action.
- Enable goal navigation only when the operator asks, since it sends screens to
  OpenAI. If they give you an OpenAI API key, store it with
  `printf '%s' "$KEY" | openclaw-iphone goal setup --key-stdin` (never as an
  argument, in logs or in replies); if they say where it is, use
  `openclaw-iphone goal setup --key-file PATH`; if `OPENAI_API_KEY` is already
  in the session's environment, plain `openclaw-iphone goal setup`. Then run
  `openclaw-iphone goal check` and restart the session. `goal setup --disable`
  turns it off.
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
