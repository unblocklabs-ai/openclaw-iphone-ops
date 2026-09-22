# Task-scoped iPhone control

The task runtime is app-independent and optional. Existing commands and app
recipes keep working; no model, daemon, service changes or new dependencies are
required. This is experimental control infrastructure, not a claim of universal
unattended safety.

For interactive planner control, use [one JSON-lines session](planner-session.md)
with `task session --file TASK.json`; `ui observe` is the one-shot compact read.

## Transport ownership

`TaskConnection(DeviceCtl(...), device=selector, seconds=60)` is a single-use,
single-threaded Python context manager. It takes the existing workflow lock,
resolves one physical UDID and CoreDevice endpoint, checks lock/readiness, and
owns one WDA session. Child WDA operations borrow that session. Device discovery
is not repeated for every tap. Debug URL overrides are deliberately unsupported
by this context: an arbitrary endpoint cannot establish physical device identity.

The shared monotonic budget covers setup, reads, writes and waits. Set
`connection.budget.cancelled` to stop new requests. In-flight device actions
cannot be retracted. Cleanup uses only the remaining budget; failure is reported
separately by `cleanup_failed` after context exit and never changes the action
result. An expired task may leave a server session for the next owner to replace.
Task session cleanup is additionally capped at two seconds so a stalled reader
does not add a full command timeout merely releasing ownership.

Source/screenshot requests have a separate 12-second socket timeout, capped by
the command timeout and remaining task/operation deadline. Override with global
`--read-timeout SECONDS` (before the subcommand), or Python `read_timeout=`.
The default allows the previously measured ~9.6-second Settings source capture;
it is not a claim that every healthy screen is faster than 12 seconds. Socket
timeouts are not hard real-time guarantees. Mutation timeouts are unchanged.

`WDAClient.type_text` retains its W3C key semantics but reuses one session for
the whole string. `type_text_bulk` uses WDA's native `/wda/keys` route in one
request. It inserts at the focused field's current caret/selection, not
necessarily the end: trusted callers must validate focus and read back the
field. No fallback, suffix continuation or replay occurs after
an error. Neither transport acknowledgement nor session cleanup proves typing
was correct. Bulk route compatibility must be checked on the deployed WDA.

Low-level Python callers must invalidate the connection on transport failure.
`invalidate(uncertain=True)` forbids recovery after an ambiguous mutation.
`recover_read()` allows one explicit reacquisition following a failed read,
checking the original UDID, current endpoint, readiness and lock again. It
invalidates old generations and never retries the caller's action. It does not
restart services, repair signing or unlock the device.
Exact UDID/CoreDevice selectors may first perform one read-only details probe
of a disconnected device, then re-list and verify physical identity and lock
state, within ten seconds or the smaller remaining budget. Name/automatic
selection does not wake a disconnected device or choose an alternative phone.

`connection.metrics.summary()` contains content-free transport attempt counts,
elapsed time and up to 2,000 event timings. Names use route templates, not session
IDs, input text, URLs, device IDs or exception messages. A timeout counts as one
attempt; operations rejected by the budget before dispatch do not. `returned`
means the HTTP exchange returned, not that the WDA protocol or task succeeded.
The existing private CoreDevice diagnostic files remain separate from metrics.

## Snapshot-bound execution

`Executor(connection, grants, texts=...)` is the optional safe targeting layer.
App knowledge belongs in the trusted caller or a skill, not in the runtime.
`Grant` fixes the source bundle ID, operation, exact `Selector`, caller-supplied
text ID/destination/direction, preconditions and expected postconditions. Grant
descriptions must be caller-written, non-sensitive aliases suitable for cloud
use. A label does not prove an action is harmless: callers authorize **effects**,
not just matching words. No actions are authorized by default.
Each grant defaults to one use for the entire executor lifetime. Repetition
requires an explicit `max_uses` (1–10), including scrolling. Reobserving does
not replenish typing or tap permissions.

```python
from openclaw_iphone.actions import Condition, Executor, Grant
from openclaw_iphone.connection import TaskConnection
from openclaw_iphone.devicectl import DeviceCtl
from openclaw_iphone.observations import Selector

field = Selector("XCUIElementTypeSearchField", label="Search")
with TaskConnection(DeviceCtl(), device="EXACT_PHYSICAL_UDID", seconds=60) as task:
    executor = Executor(task, (
        Grant("tap", "your.app.bundle", "Focus search", field,
              after=(Condition("focused", "your.app.bundle", field),)),
        Grant("append", "your.app.bundle", "Enter supplied query", field,
              text_id="query", before=(Condition("focused", "your.app.bundle", field),)),
    ), texts={"query": "synthetic example"})
    observation = executor.observe()
    offers = executor.offers(observation)
    # Trusted deterministic code (or a bounded driver) chooses an offered ID.
    # Never assume the first offer is the right one for an arbitrary task.
```

An observation carries random snapshot-local IDs, capture start/end times,
physical device/generation and foreground bundle/PID, elements, hierarchy,
geometry and local progress signature. Unknown visibility/enabled/value stays
unknown. Oversized/deep/malformed sources are rejected rather than truncated
into a misleading actionable snapshot. Secure values are discarded; the
presence of any secure field disables offers. Raw local observations still
contain private non-secure text; do not serialize them into logs or model input.

`execute(offer.id)` consumes the observation's choices once. It captures fresh
source, compares app/PID, target identity, ancestor context, location and value,
resolves a unique WDA element reference, checks hittability, lock and foreground
identity, then dispatches. Superseded/expired offers never remap to new indices.
No guessed coordinates or icon semantics are used. WDA cannot make all these
checks atomic with input; external clients, animations or human touches can
still race. This is a safety improvement, not an isolation guarantee.

Supported grants: `tap`, explicit named `back`, `append`, `clear`, `replace`, `keypad`,
`scroll`, `activate`, `open_url`. App activation requires an installed bundle;
deep links require an exact destination and expected app postcondition. Back
requires an explicitly named Back/Go Back button; arbitrary top-left controls
are not eligible. Scroll targets a verified container through WDA with fixed
half-container distance; it checks scoped visible-item change, not unrelated
screen activity. Change is progress, not independent task completion.

Text entry requires the exact field to be focused, excludes secure fields and
control/submission characters, and uses one native targeted input request for
up to 4,096 supplied characters. WDA inserts at the current caret/selection;
the runtime cannot establish an end-of-text caret. Consequently, `append` is
only supported for empty fields: known nonempty values (including placeholders)
are not offered, and a fresh value-endpoint read must confirm empty before input,
even when XML says empty. For whole-field entry, explicitly authorize `replace`
and supply the complete desired text. Append never silently clears or replaces
existing text. `replace` verifies clear before typing. If WDA
reports a placeholder instead of an observable empty value, replacement stops
after clear with uncertain verification; it does not guess that the field is
empty. Missing XML values require a separate successful value-endpoint read:
WDA's explicit `value: null` confirms empty for an otherwise validated editable
field; a missing response key/error does not. Placeholder-like values are not
treated as empty. Chunking and automatic unsupported-route fallback are not enabled.

### Explicit keypad input

`keypad` references a supplied `text_id` containing 1–32 ASCII digits. It is a
deliberately selected input strategy, **not** a retry on bulk failure. Target a
named non-secure editable field, or a custom `XCUIElementTypeOther` field that
actually exposes focus and a readable string value. Before the first key, a
fresh value-endpoint read must equal `""` (native editable fields also accept
WDA's explicit `null`; custom fields do not). Placeholders, missing attributes
and empty OCR output are insufficient. It does not clear.
If existing content must be reset, do so only in a separately authorized,
verified workflow; generic custom-field clearing is not implemented.

The whole input uses one fresh app/PID, field/focus check and native keyboard
layout. Every requested digit must resolve to one visible enabled key in that
same keyboard. The observed key centers become separately timed touches in
**one WDA request**, not six screen reads and native clicks. Coordinates are
derived locally from accessibility, never guessed by the model.

Verify the full value afterward, or an explicit `after` destination for fields
that auto-submit. Acknowledgement counts describe requests, not confirmed
characters. A failed/partial batch is never replayed. The batch cannot be
interrupted between digits; use it only for a stable keypad, not a multi-screen
macro. Session setup disables XCTest global-idle/animation waits; readiness
and completion belong to explicit predicates. See [input benchmarks](input-performance.md).

Value predicates on custom `Other` fields always use a separate strict-string
value read, never treat WDA `null` as empty. These are assertions about the
observed value, not proof a control is an input or permission to mutate it.
Keypad actions also require independent focus and field/keyboard validation.

Predicates are fixed data: expected app, unique element existence/absence,
actionability, focus or exact non-secure editable value. `wait()` polls them
within the earlier operation/task deadline. Conditions are conjoined; no code,
expressions or scripts are evaluated. A missing attribute is not successful
verification. `StepResult` separates dispatch, verification and acknowledged
compound substeps. Failed verification stops the executor; it never replays a
successful tap or partially completed replacement. Inspect/replan explicitly.

App-only waits use lock-state and one foreground bundle/PID read, not
the accessibility tree. Their returned `Observation` has `elements=None` and
`secure=None`: screen contents and secure-field presence were **not observed**.
It can verify app identity only; element conditions return `unknown`, direct
element matching is rejected, and it cannot produce action offers or Jev input.
Call `observe()` again for a full screen before targeting. Full observation and
fresh target validation before dispatch remain unchanged. Mixed/element waits
(including text readback) still acquire the full tree.

If an action verifies only app identity but task completion needs element evidence,
the task loop takes a full read-only observation under the remaining task budget,
even after the final allowed action. It never repeats that action. App-only task
completion does not incur this extra read.

An `app` condition proves only which application is foreground, not that its UI
has loaded or a deep link reached the intended page. Include an element or value
condition when readiness, destination content or field state is required.

Routine operations use accessibility only. No screenshot or raw source is
automatically saved on failures, including private or secure screens. Explicit
local CLI evidence capture remains available under the existing private-file
rules. Cleanup status is read from the connection **after** context exit.

## Optional Jev task driver

Jev selects from the same offers used by deterministic Python callers. It does
not generate text, coordinates, destinations, selectors, commands or code. The
planner/skill must supply all permissions and observable completion conditions.
Keep known deterministic recipes/deep links as the first choice; this driver is
for bounded routine decisions, not a universal iPhone agent or vision model.

The dependency-free adapter uses TypeSafe's documented
[`POST /v1/systemone`](https://docs.typesafe.ai/api), Bearer authentication and
Choice questions, pinned to `jev-1.13.0`. It does not load an invented SDK, honor
endpoint overrides, forward credentials through proxies/redirects or retry
provider failures. HTTP/transport failures or invalid responses escalate as
`model_unavailable`. A valid answer below the configured threshold escalates as
`low_confidence`, with confidence, `min_confidence`, latency and token usage in
`last_decision`; it dispatches no action, including in decision-only mode.
Known usage is still counted. Replan explicitly rather than rerunning an
unknown device mutation. Model confidence is never authorization or completion proof.

The Choice request uses structured JSON objects for both `instructions` and
each `criteria` entry (the documented TypeSafe shape), rather than making the
model infer policy from vague strings. Device-action criteria contain only the
opaque choice key, operation, caller-written description and a snapshot
boundary; `wait`, `done` and `escalate` are explicitly typed non-device
alternatives. This improves decision context without granting Jev new
authority: it still cannot emit selectors, coordinates, destinations, text,
commands or completion claims.

Make `TYPESAFE_API_KEY` available in the invoking process using your existing
secure credential mechanism. Do not pass it as a CLI argument, add it to the
task file or commit it. The driver expects the **value**, not the contents of an
`env` assignment file. The runtime does not read another plugin's credentials.
No TypeSafe key is required for any existing command or deterministic task.

Create an owner-only task JSON file. Example (replace app identities and targets
with ones actually observed and authorized; this example only activates an app):

```json
{
  "version": 1,
  "objective": "Open Settings from Calculator; finish when Settings is foreground.",
  "grants": [{
    "operation": "activate",
    "app": "com.apple.calculator",
    "description": "Open the installed Settings application.",
    "destination": "com.apple.Preferences",
    "after": [{"kind": "app", "app": "com.apple.Preferences"}]
  }],
  "success": [{"kind": "app", "app": "com.apple.Preferences"}],
  "limits": {"seconds": 60, "max_steps": 6, "max_decisions": 6}
}
```

```sh
# No model or cloud; proceeds only when there is one eligible grant.
openclaw-iphone task run --file /private/task.json --driver deterministic

# Inspect the approved choice without UI input. Still a paid cloud request.
openclaw-iphone task run --file /private/task.json --driver jev --allow-cloud --decision-only

# Real bounded execution; requires authorized effects as well as cloud consent.
openclaw-iphone task run --file /private/task.json --driver jev --allow-cloud
```

`--min-confidence` defaults to 0.7 (an exploratory starting point, not calibrated
probability of safe execution or task completion).
Changing it does not relax target validation, grants or verification. Evaluate
held-out decision cases before changing it for autonomous use. The `done`
choice runs the independent verifier; it cannot declare success. `wait` polls
without UI mutation; repeated no-progress terminates. Failures return stable
status/reason codes and safe metadata, not exception bodies or model prompts.

Task files reject unknown fields, duplicate JSON keys, non-finite numbers,
unknown operations, missing text IDs and empty success conditions. Schema v1:

- `version`, `objective`, `grants`, `success` are required.
- `texts` optionally maps IDs to exact supplied strings. Text stays local even
  when Jev is used; grant descriptions should explain intent without repeating
  the input. Control/submission characters are rejected.
- A `target` has exact `role`, optional exact `name`/`label` and optional
  `ancestor_label`. Matching is not substring-based. Roles use full
  `XCUIElementType…` names. No XPath/predicate/code from the task is executed.
- Grants accept `before`/`after` condition lists and `max_uses` (default 1, max
  10). Typing uses `text_id`; scroll uses `direction` (`up`/`down`); app
  transitions use `destination`. Incompatible parameters are rejected.
- Conditions have `kind` and `app`; element conditions add `target`; `value`
  conditions also require an exact string `value`. See supported kinds above.
- Optional `limits`: `seconds` (60), `max_steps` (12), `max_decisions` (12),
  `max_no_progress` (3), `freshness` (30 seconds), `verification_seconds` (15).
  Counts are 1–100, total time ≤3,600 seconds, per-observation/wait bounds ≤60.
  Limits include model attempts; requests are additionally capped at 16 KiB.

Task result JSON is printed and saved in a unique owner-only evidence directory.
Exit 0 means verified completion or successful **decision-only** mode, not that
every result with exit 0 mutated/completed the device task. Noncompletion exits
1. `dispatch`, `verification`, acknowledged substeps, last decision metadata,
cleanup and telemetry remain separate. Evidence-write failure does not erase a
successful action result. The task connection owns the workflow lock for the
whole invocation, including model inference.

### Cloud privacy boundary

Only the caller-written objective/action descriptions, approved bundle ID,
snapshot ID/time, eligible operations/roles, opaque target IDs and availability
are sent. Raw accessibility labels, field values, supplied input, selectors,
device UDID/PID, source XML and screenshots are **not** sent. Unknown apps and
secure-field screens block cloud inference. This strict projection deliberately
limits what Jev can understand; ambiguity goes back to the planner rather than
uploading more of the screen. Caller-written objectives/descriptions must also
be reviewed for private data. No-training does not mean zero retention; review
[TypeSafe's account/privacy terms](https://docs.typesafe.ai/legal) before use.

### Measurements and remaining acceptance

`task summarize RESULT.json ...` aggregates saved task results offline, grouping
drivers and including failures in total-time median/p95. It reports transport
counts, missing telemetry, inference latency/tokens and estimated known cost
using the published $0.042/M input-token rate (2026-09-21). Missing usage is
unknown cost, not free. Limits bound attempts/bytes/time, not exact dollar cost.
Optional independent annotations `wrong_target_actions`, `unintended_actions`
and `false_successes` remain null unless supplied; transport logs cannot prove
the absence of external effects.

Use equivalent tasks and starting state to compare `legacy`, `planner`,
`deterministic` and `jev`; annotate externally timed legacy/planner runs rather
than inventing model timings. Separate cold setup from warm actions using
transport event timings. Small-sample p95 is exploratory. No automatic live
benchmark runs, social actions, purchases or destructive operations are bundled.
Live acceptance evidence and exact remaining steps are recorded in
[runtime-validation.md](runtime-validation.md).
