# Task-scoped iPhone control

The task runtime is app-independent and optional. Existing commands and app
recipes keep working; no model, daemon, service changes or new dependencies are
required. This is experimental control infrastructure, not a claim of universal
unattended safety.

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

`WDAClient.type_text` retains its W3C key semantics but reuses one session for
the whole string. `type_text_bulk` uses WDA's native `/wda/keys` route in one
request. It appends to the focused field: trusted callers must validate focus
and read back the field. No fallback, suffix continuation or replay occurs after
an error. Neither transport acknowledgement nor session cleanup proves typing
was correct. Bulk route compatibility must be checked on the deployed WDA.

Low-level Python callers must invalidate the connection on transport failure.
`invalidate(uncertain=True)` forbids recovery after an ambiguous mutation.
`recover_read()` allows one explicit reacquisition following a failed read,
checking the original UDID, current endpoint, readiness and lock again. It
invalidates old generations and never retries the caller's action. It does not
restart services, repair signing or unlock the device.

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

Supported grants: `tap`, explicit named `back`, `append`, `clear`, `replace`,
`scroll`, `activate`, `open_url`. App activation requires an installed bundle;
deep links require an exact destination and expected app postcondition. Back
requires an explicitly named Back/Go Back button; arbitrary top-left controls
are not eligible. Scroll targets a verified container through WDA with fixed
half-container distance; it checks scoped visible-item change, not unrelated
screen activity. Change is progress, not independent task completion.

Text entry requires the exact field to be focused, excludes secure fields and
control/submission characters, and uses one native targeted append request for
up to 4,096 supplied characters. `replace` verifies clear before append. If WDA
reports a placeholder instead of an observable empty value, replacement stops
after clear with uncertain verification; it does not guess that the field is
empty. Chunking and automatic unsupported-route fallback are not enabled.

Predicates are fixed data: expected app, unique element existence/absence,
actionability, focus or exact non-secure editable value. `wait()` polls them
within the earlier operation/task deadline. Conditions are conjoined; no code,
expressions or scripts are evaluated. A missing attribute is not successful
verification. `StepResult` separates dispatch, verification and acknowledged
compound substeps. Failed verification stops the executor; it never replays a
successful tap or partially completed replacement. Inspect/replan explicitly.

Routine operations use accessibility only. No screenshot or raw source is
automatically saved on failures, including private or secure screens. Explicit
local CLI evidence capture remains available under the existing private-file
rules. Cleanup status is read from the connection **after** context exit.
