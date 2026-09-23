# Bounded exploration prototype

`task session --explore` is an opt-in mode of the existing `TaskConnection` →
`Executor` → `PlannerSession` stack. It does not start a new controller, driver,
transport or service. `task run` and ordinary `task session` keep their strict
version-1 grants and independently observable success conditions.

Use a trusted local version-1 task file with an objective, bounded limits and
an exact adaptive scope. Exploration omits `grants` and `success` (empty lists
are also accepted); nonempty fixed grants, success conditions and inline
`texts` are rejected.
Only the caller decides when its objective is complete. An acknowledged tap,
app identity, changed screen or empty condition list is **not** task success.
`done` closes the exploration session with `verification: unknown`.

```json
{
  "version": 1,
  "objective": "Inspect the next synthetic screen",
  "adaptive": {
    "apps": ["example.test"],
    "operations": ["vision_tap", "tap", "scroll", "input"],
    "inputs": {"query": "/absolute/owner-only/query.txt"}
  },
  "limits": {"seconds": 60, "max_steps": 4, "freshness": 30}
}
```

```sh
openclaw-iphone task session --explore --file /absolute/private/task.json
```

Read each complete JSON-line response before sending the next request; do not
wait for the long-lived process to exit. The deadline includes caller pauses.
The existing request cap, step cap and deadline bound the session. No Jev
driver is used by exploration; `--driver jev` is rejected. The `objective` is
caller context, not an authorization expansion.

## Existing primitives, not renamed wrappers

- `{"op":"screenshot","redact":[[x,y,width,height]]}` captures an app/PID-
  and geometry-bound image. The rectangles are **device points**, explicitly
  reviewed by the caller. The response has a private local PNG path and a
  one-use screenshot `snapshot_id`. An empty list is an explicit decision to
  expose the whole scoped screen to the local caller, not a privacy guarantee.
- `{"op":"vision_tap","snapshot_id":"...","x":10,"y":20}` acts once on
  coordinates in the returned image dimensions. It returns an acknowledged
  dispatch with unknown effect and does not request AX afterward. Inspect the
  result explicitly before another action. It checks current physical session,
  app/PID, geometry, freshness and token use, but cannot make an external UI
  transition atomic with the tap.
- `{"op":"observe"}` requests the existing AX view when semantic controls
  are useful. `act` with scoped `tap`, `scroll`, or `input` uses the existing
  target, focus, empty-field, destination and readback checks. `scroll` is not
  an AX-free pixel swipe in this prototype. Input values come only from
  approved owner-only files; never put credentials in task JSON or requests.
- `recover_read` and `reconcile` retain their existing explicit no-replay
  behavior. A source failure can require read recovery; an uncertain write
  cannot be recovered into permission to replay.

Capture without explicit masks still requires fresh AX to derive automatic masks.
For an AX-unavailable screen, only explicitly reviewed rectangles permit the
app-only pixel path. Unknown future screens cannot be reliably pre-masked;
do not infer that this prototype makes general pixel-first browsing private.
There is no whole-screen model-disclosure permission, auto image upload,
automatic replay or live phone evaluation in this phase. Unexpected app or
OAuth/consent hosts require a newly reviewed scope and session, not a broad
preauthorization. The runtime cannot infer whether a caller-selected control
has a consequential social, financial or account effect.

## What would be retired, if proven useful

This prototype deliberately shares the existing planner. If matched live
evaluation later demonstrates safer or materially more usable exploration,
remove exploration-specific schema/CLI branches only after moving the common
app-scoped, bounded primitives into the normal session contract. Fixed grants
and success verification can remain for deterministic recipes, but their
offer enumeration and `done` machinery should not be mandatory for exploration.
Conversely, if the privacy/review burden or usability does not improve, delete
`--explore`, its parser branch and this document/tests; the existing adaptive
session remains intact. Do not leave a third engine alongside fixed and
adaptive execution.

Offline synthetic tests prove request routing and safety boundaries only.
They do **not** show a speedup, physical WebView/input reliability or objective
completion. A paired, navigable live fixture and explicit authorization are
needed to compare successful effects, caller/device elapsed time, wrong or
uncertain actions and privacy events.
