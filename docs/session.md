# Direct iPhone session (protocol 2)

Requires v0.6.0 or newer. Start `openclaw-iphone session --device PHYSICAL_UDID`.
From a source checkout, use `PYTHONPATH=src python3 -m openclaw_iphone` instead
of the installed CLI. Add `--allow-images` only when screenshot disclosure is appropriate.
Capture may contain private messages, credentials, or account information.

The process emits `ready`, accepts newline-terminated JSON requests, returns one
response per request, then emits `session_end` after closing and cleanup.
`request_sequence` identifies the response; requests are handled serially.
Hold onto the process across the workflow. Don't recreate it after each step.

## Operations

```json
{"op":"observe","mode":"accessibility"}
{"op":"observe","mode":"image"}
{"op":"observe","mode":"both","masks":[[10,100,250,40]]}
{"op":"tap","x":120,"y":300}
{"op":"tap","space":"image","x":360,"y":900}
{"op":"tap","target":{"role":"XCUIElementTypeButton","label":"Continue","ancestor_label":"Account"}}
{"op":"tap","target":"RETURNED_ELEMENT_ID"}
{"op":"swipe","from_x":200,"from_y":700,"to_x":200,"to_y":250,"duration":0.2}
{"op":"type","text":"hello","mode":"insert"}
{"op":"type","text_ref":"/absolute/private/input.txt","mode":"replace","strategy":"native"}
{"op":"type","text":"hello","target":{"role":"XCUIElementTypeTextField","label":"Search"},"strategy":"sequential"}
{"op":"press","button":"home"}
{"op":"press","button":"enter"}
{"op":"launch","bundle_id":"com.apple.mobilesafari","observe":"image"}
{"op":"open_url","url":"https://example.com","observe":"accessibility"}
{"op":"close"}
```

- `observe` returns a screen: AX plus a screenshot when `--allow-images` is set,
  otherwise AX only. Explicit `mode` still selects either independently.
  `image` and coordinate `swipe`/`tap` do
  not require accessibility. `both` reports each component independently.
- AX shows readable content and controls, including unnamed controls, their
  bounds, enabled/focus/selection state and short snapshot-local IDs. Empty
  layout wrappers and duplicate container text are collapsed. `parent` links
  preserve meaningful groups (for example, which account owns a button).
  Full paths and native ancestors stay internal for locating the target.
- AX pages contain at most 80 screen elements by default (`limit`: 1–200),
  automatically stopping earlier to fit the output budget. `next_offset` tells
  you where to continue with `{"op":"observe","offset":NEXT_OFFSET}`.
  These pages reuse the captured AX snapshot and IDs without another device
  read; their default is AX only. Parent IDs may refer to earlier pages. A fresh
  observation at offset 0 replaces the snapshot. Paging is disclosure of a
  prior screen, not evidence of current state.
- Actions accept optional `observe: accessibility|image|both`, otherwise no
  automatic capture or verification is performed. `masks` applies to images.
- Coordinates are device points, unless `space: image` explicitly selects
  pixels in the most recent successfully captured image. Returned image
  metadata includes both sizes. Image coordinates are reusable, not consumable
  tokens; changed window geometry rejects only that request. Screens may move
  without changing geometry: the caller remains responsible for visual intent.
- A target selector uses exact `role`, optional `name`, `label`, and
  `ancestor_label`. Native lookup must resolve exactly one visible enabled
  element. Ambiguity returns current candidates with their meaningful groups.
  Snapshot IDs come from the last
  AX observation; they resolve current identity and ancestor context, not stored
  coordinates or value. New observations replace the ID map. Cross-app ID use
  is rejected; ordinary app transitions do not change session permissions.
- `type` inserts by default. `replace` explicitly clears first. With no target,
  input goes to the focused field; callers are responsible for focus. Native
  input uses WDA keys or targeted value insertion. Explicit `sequential` uses
  individual key requests, focusing a supplied target first. It never falls
  back, verifies a value, submits Return, or repeats failed input automatically.
- Text is bounded to 4096 characters without control characters; use `press`
  for control keys. Hardware buttons: `home`, `volumeUp`, `volumeDown`, `siri`.
  Keyboard controls: `enter`, `delete` (backspace), `tab`, `escape`. `back` uses
  supported WDA routes only, never guesses a screen control.
- `text_ref` is an owner-only regular UTF-8 file owned by the current user,
  read at request time; symlinks and oversized files are rejected. One trailing
  newline is removed. Paths, text, and exception bodies are never echoed.
  AX text values are omitted; known Boolean checkbox/switch state is returned as
  `checked`, and native selection as `selected`. Secure fields' names/labels are omitted; supplied
  input is suppressed in subsequent label projections. Images require separate
  disclosure approval and explicit masks where needed.

## Outcomes and recovery

`dispatch` describes input delivery, **not achievement of the agent's task**:

- `acknowledged`: WDA acknowledged all requested substeps.
- `not_sent`: no requested input dispatched.
- `partial`: earlier substeps acknowledged, later work could not start.
- `unknown`: a write may have occurred. `acknowledged_substeps` and, for
  sequential input, `acknowledged_characters` preserve known progress.

Inspect before retrying uncertain input. The session never replays writes or
permanently blocks subsequent deliberate requests. A safe read can reconnect
and retry once per request on the same pinned physical UDID; another read may
recover again. It never switches devices, unlocks, restarts WDA or broadens
app/account permissions automatically. A changed physical identity rejects the
request without dispatch.

An optional observation failure is reported under the observation component
(`image_error` / `accessibility_error`); it does not change action dispatch.
Malformed JSON, duplicate keys, nonfinite numbers, invalid fields and oversized
newline-framed requests produce `invalid_request` and leave the session open.

## Ownership, limits and privacy

- One exclusive control lock for the whole session. Existing service wrappers
  remain unchanged. Other tools outside this package aren't coordinated by it.
- `--operation-timeout` defaults to 30 seconds. Global `--timeout` caps individual
  subprocess/WDA calls; `--read-timeout` defaults to 12 seconds. Each operation
  gets a fresh deadline; idle caller deliberation consumes none of it.
- Gesture/button durations are at most ten seconds; long robot-side input is not
  made cancellable merely by limiting the host's HTTP wait.
- Cancellation is process interruption (Ctrl-C/SIGINT), which releases ownership.
  Inspect before retrying any interrupted action.
- Requests are at most 4 KiB; partial/nonterminated frames expire after five
  seconds. Idle between frames is unlimited. Outputs are bounded to 64 KiB;
  oversized responses retain the receipt and a successful image where it fits,
  without closing. Ordinary AX is byte-paged before reaching this fallback;
  callers do not need to guess a smaller limit. Stalled/disconnected output
  ends ownership without replaying input.
- Evidence is a unique owner-only file/directory. Caller-specified mask rectangles
  are `[x,y,width,height]` in device points. Masking hides only these rectangles,
  not arbitrary secrets; it is not anonymization or permission to share images.
- `close` and EOF exit 0 after normal lifecycle cleanup. Neither claims task
  completion. Cleanup failure is an independent warning. Setup, framing,
  interruption, or output failure exits nonzero.

## Migration

Remove task files, grants, goal predicates, Jev settings and `--explore` flags.
Replace `ui`/`task` commands with the operations above. Instagram-specific
research/ranking/video commands are retired; app knowledge now lives in the
Instagram skill and the caller's workflow. No legacy compatibility engine remains.

Offline loopback tests prove protocol behavior, not physical reliability,
WebView accessibility, coordinate accuracy, or performance. Use an approved,
non-destructive matched physical comparison before claiming those improvements.
