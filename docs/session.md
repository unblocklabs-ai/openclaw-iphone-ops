# Direct iPhone session (protocol 2)

Start `openclaw-iphone session --device PHYSICAL_UDID`.
Read `ready.capabilities` for supported optional operations.
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
{"op":"set","target":"NAME_FIELD_ID","value":"Bek"}
{"op":"set","target":"NAME_FIELD_ID","value":""}
{"op":"set","target":"TEXT_VIEW_ID","value":"First line\nSecond line"}
{"op":"set","target":"PASSWORD_FIELD_ID","value_ref":"/absolute/private/input.txt"}
{"op":"set","target":"DATE_PICKER_ID","kind":"date","value":"1990-10-14"}
{"op":"set","target":"WHEEL_ID","value":"October"}
{"op":"set","target":"SWITCH_ID","value":false}
{"op":"type","text":"hello","mode":"insert"}
{"op":"type","text_ref":"/absolute/private/input.txt","mode":"replace","strategy":"native"}
{"op":"type","text_ref":"/absolute/private/input.txt","mode":"replace","verify":true}
{"op":"pick","target":{"role":"XCUIElementTypePickerWheel","label":"Month"},"value":"October"}
{"op":"pick","target":"RETURNED_WHEEL_ID","value":"October","order":"next","max_steps":12,"seconds":5}
{"op":"type","text":"hello","target":{"role":"XCUIElementTypeTextField","label":"Search"},"strategy":"sequential"}
{"op":"press","button":"home"}
{"op":"press","button":"enter"}
{"op":"launch","bundle_id":"com.apple.mobilesafari","observe":"image"}
{"op":"launch","bundle_id":"com.apple.mobilesafari","wait_seconds":2,"observe":"image"}
{"op":"open_url","url":"https://example.com","observe":"accessibility"}
{"op":"goal","goal":"Open Settings and show Wi-Fi","approve":[]}
{"op":"close"}
```

`goal` exists only when `ready.capabilities` includes it: optional Clef goal
navigation, enabled in host config. See [goal navigation](goal.md).

- `observe` returns a screen: AX plus a screenshot when `--allow-images` is set,
  otherwise AX only. Explicit `mode` still selects either independently.
  `image` and coordinate `swipe`/`tap` do
  not require accessibility. `both` reports each component independently.
  The image and AX captures run concurrently (masked images are captured first,
  since masking reads `/window/size` on the source queue), so failed/slow AX
  cannot starve the image. Each capture has its own timestamp; they are not an atomic pair.
- AX shows readable content and controls, including unnamed controls, their
  bounds, enabled/selection state and short snapshot-local IDs. On-screen means
  the bounds overlap the screen: WDA's per-element visibility verdict costs about
  20 ms per element, so it is not read. Content covered by a sheet or a stacked
  screen is therefore listed; the screenshot shows what is on top. The app and
  process come from the source's application root. Empty
  layout wrappers and duplicate container text are collapsed. `parent` links
  preserve meaningful groups (for example, which account owns a button).
  Full paths and native ancestors stay internal for locating the target.
- AX pages contain at most 80 screen elements by default (`limit`: positive integer),
  automatically stopping earlier to fit the output budget. `next_offset` tells
  you where to continue with `{"op":"observe","offset":NEXT_OFFSET}`.
  These pages reuse the captured AX snapshot and IDs without another device
  read; their default is AX only. Parent IDs may refer to earlier pages. A fresh
  observation at offset 0 replaces the snapshot. Paging is disclosure of a
  prior screen, not evidence of current state.
- Actions accept optional `observe: accessibility|image|both`, otherwise no
  automatic capture is performed. Native input readback is described below;
  task verification belongs to the caller. `masks` applies to images.
- Coordinates are device points, unless `space: image` explicitly selects
  pixels in the most recent successfully captured image. Returned image
  metadata includes pixel size; `device_size` is null unless masking needed it.
  Conversion fetches device dimensions on first use and reuses them for that
  image. Capture a new image after rotation or layout changes. There is no
  geometry probe before each tap; the caller owns visual intent.
- A target selector uses exact `role`, optional `name`, `label`, and
  `ancestor_label`. Native lookup must resolve exactly one visible enabled
  element. Ambiguity returns current candidates with their meaningful groups.
  Snapshot IDs come from the last
  AX observation; they resolve current identity and ancestor context, not stored
  coordinates or value. New observations replace the ID map. IDs are not
  checked against the foreground app (that read costs 0.12 s per tap); after an
  app switch, observe again, because a control with the same role, names and
  ancestors in the new app would match.
  Otherwise indistinguishable IDs use their exact captured hierarchy position,
  not geometry. A same-app reorder can change positional intent: observe again
  when the list changes. Named, unique controls still use semantic identity.
  Ambiguity selects relevant matches and groups from the full snapshot before
  paging. `candidate_page.next_offset` continues with
  `{"op":"observe","candidates":true,"offset":NEXT_OFFSET}` without recapture.
- `type` inserts by default. `replace` explicitly clears first. With no target,
  input goes to the focused field; callers are responsible for focus. Native
  input uses WDA keys or targeted value insertion. Explicit `sequential` uses
  individual key requests, focusing a supplied target first. It never falls
  back, submits Return, or repeats failed input automatically.
- `set` supplies the desired value in **one request**. Native target type selects
  text, wheel, date or Boolean behavior; `kind: date` explicitly identifies a
  date group. Without a target, ordinary input uses the focused native element.
  Text replaces the field; `""` clears it. WDA handles native keyboard focus
  without an extra host tap. Literal newlines require a native TextView. Names,
  emails, numbers and codes are strings: spelling and leading zeros are preserved.
  No Return, Next, Done or submission action is added. Supplied newlines can
  still invoke an app's own input handlers. `strategy: sequential` is an explicit
  compatibility option, not an automatic retry. `value_ref` has the same private
  file rules as `text_ref`. Optional `verify: true` compares text privately;
  `effect: match|mismatch|unknown` is separate from delivery. Unverified text and
  secure/unreadable values return unknown, not a failed action.
- Date `set` takes a Gregorian `YYYY-MM-DD` value, scopes discovery to the
  identified group, and coordinates year, month, then day. One grouped native
  lookup supplies the wheel identities and values; three native selections
  set the date. Optional `verify: true` adds one grouped read of the complete
  result; otherwise `effect: unknown` leaves verification to the caller. There is no
  per-wheel polling, host-side swipe correction or input replay. Already-correct
  dates send no input. Later wheels may clamp earlier components. Recognizable
  English named/abbreviated months and native Year/Month/Day labels identify
  components without assuming their screen order. Native birthday wheels with
  an omitted year (`----`) accept the supplied year. Numeric formats preserve
  digit style, leading-zero conventions and native unit text. If component
  identity is ambiguous, supply `components` mapping
  `year`, `month`, `day` to the usual targets; the group target is then optional.
  Localized month strings use an explicit twelve-item `month_values` list in
  calendar order. This is wheel-style Gregorian entry, not arbitrary calendar,
  compact/inline picker navigation or non-Gregorian conversion. Unsupported
  identity/format returns a specific reason without input or session termination.
- Wheel `set` selects the native value once. Optional `verify: true` reads back;
  otherwise the effect is unknown. No automatic adjustment or fallback follows
  a mismatch, unsupported route or uncertain write. Use explicit `pick` for
  bounded wheel adjustments when needed.
- Boolean `set` uses readable native switch/checkbox/radio/toggle state. Already
  matching means no input; otherwise it clicks once. Optional `verify: true`
  reads back; otherwise the effect is unknown. Unknown
  initial state sends no guessed toggle. An app may not permit a state change;
  the result reports mismatch/unknown rather than repeatedly clicking.
- Inputs dispatch directly without a lock-status preflight. Use explicit
  lock/unlock/status commands for diagnosis or recovery; native failures are
  reported without replaying input.
- For `type`, optional `verify: true` requires `mode: replace`: the expected value
  is the supplied whole field, not an inferred append/caret position. Local
  readback returns only `verification: match|mismatch|unknown`. Secure fields,
  placeholders and unreadable custom fields return unknown. Readback failure
  never changes acknowledged dispatch and never retypes the input.
- `pick` requires one explicit native PickerWheel target and exact locale-specific
  `value`. By default it tries native value selection once and reads back.
  Optional `order: next|previous` instead performs at most `max_steps` native
  wheel adjustments (`max_steps`: positive integer, default 10), checking after each. `offset` is the native
  wheel offset (greater than 0, at most 0.5, default 0.15), NOT observation paging.
  Optional positive `seconds` bounds picker work within the operation deadline;
  otherwise the operation deadline applies. `effect: match|mismatch|unknown` is separate from
  dispatch. An already matching wheel sends no input. Unsupported routes are
  reported; uncertain writes are never replayed or followed by blind swipes.
  Set dependent day/month/year wheels deliberately; no date/locale guessing.
- `launch.wait_seconds` optionally checks foreground identity for the supplied
  nonnegative duration, bounded by the operation deadline,
  after exactly one launch, then captures requested evidence. It returns
  `readiness.state: ready|unknown` with checks/time. This proves foreground only,
  not a fully loaded in-app screen. No blind sleep, repeated launch or global
  idle gate. Unready/unknown leaves dispatch acknowledged.
- Input size is bounded by request framing or private-file size. `type` excludes control
  characters; `set` permits literal newlines in TextView only. Use `press`
  for control keys. Hardware buttons: `home`, `volumeUp`, `volumeDown`, `siri`.
  Keyboard controls: `enter`, `delete` (backspace), `tab`, `escape`. `back` uses
  supported WDA routes only, never guesses a screen control.
- `text_ref` is an owner-only regular UTF-8 file owned by the current user,
  read at request time; symlinks and oversized files are rejected. One trailing
  newline is removed. Paths, text, and exception bodies are never echoed.
  AX text values are omitted; known Boolean checkbox/switch state is returned as
  `checked`, and the native Selected trait as `selected: true`. Secure fields' names/labels are omitted; supplied
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
recover again. Source-specific deadlines, malformed AX and read protocol errors
do not recreate the session. Genuine transport/session loss may reconnect. An AX
read whose reply shows WDA no longer holds this session (WDA restarted, or another
client opened one and reset WDA's idle/animation waits to 10 s/2 s) counts as
session loss: the new session re-applies zero waits. It never switches devices, unlocks, restarts WDA or broadens
app/account permissions automatically. A changed physical identity rejects the
request without dispatch.

An optional observation failure is reported under the observation component
(`image_error` / `accessibility_error`); it does not change action dispatch.
Errors contain safe `category`/`phase`, never raw server or exception messages.
Malformed JSON, duplicate keys, nonfinite numbers, invalid fields and oversized
newline-framed requests produce `invalid_request` and leave the session open.
Incomplete frames produce `framing_error` and end ownership safely.

Every reply includes content-free `timing`: start/end, elapsed, route counts/time
and reconnects. `session_end` includes final acquisition/request/cleanup totals.
No payloads, element/session IDs, raw errors, labels or URLs enter telemetry.
Use these to distinguish device costs from caller decision/approval gaps;
they do not measure model reasoning. Consume complete replies promptly, avoid
empty polling, and render returned images directly if the host tool permits it.

## Ownership, limits and privacy

- One exclusive control lock for the whole session. The watchdog and foreground
  mutation commands use the same lock. Other tools outside this package aren't
  coordinated by it.
  A busy session reports advisory owner PID/start/last-activity/request count.
  Live idle owners are never evicted; process exit releases the OS lock.
- `--operation-timeout` defaults to 30 seconds. Global `--timeout` caps individual
  subprocess/WDA calls; optional `--read-timeout` additionally caps safe reads. Each operation
  gets a fresh deadline; idle caller deliberation consumes none of it.
- Gesture/button durations have no additional host-side ceiling; long robot-side
  input is not made cancellable merely by limiting the host's HTTP wait.
  Swipe duration is movement time (minimum 100ms), with no equal-length hold.
- Cancellation is process interruption (Ctrl-C/SIGINT), which releases ownership.
  Inspect before retrying any interrupted action.
- Requests are at most 64 KiB; partial/nonterminated frames expire after five
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

## Validation boundaries

Offline loopback tests prove protocol behavior, not physical reliability,
WebView accessibility, coordinate accuracy, or performance. Use an approved,
non-destructive matched physical comparison before claiming those improvements.
