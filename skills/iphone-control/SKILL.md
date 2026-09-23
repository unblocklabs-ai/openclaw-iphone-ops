---
name: iphone-control
description: Control or diagnose a USB-connected physical iPhone from its Mac using task-scoped WebDriverAgent sessions, verified input, accessibility observations and explicit local screenshot fallback. Use for real-device UI workflows, reconnect/read failures and WDA setup; not iOS Simulator work.
---

# iPhone Control

## Choose the existing tool first

Use the installed `openclaw-iphone` CLI. Check `--version` and subcommand
`--help` before assuming a capability exists on this host. If running a source
checkout, substitute `PYTHONPATH=src python3 -m openclaw_iphone` from its root.
Do not edit an npm-managed installation; upgrades replace its files.

| Need | Preferred path |
| --- | --- |
| Known reliable workflow | Existing deterministic recipe/deep link with destination verification |
| Bounded multi-step task with one eligible action at each step | `task run --file TASK.json --driver deterministic` |
| Planner needs to choose each step | **One** `task session --file TASK.json` process; retain its process handle across JSON-line requests |
| Reveal more content | Adaptive `act` with `action: scroll`, an observed container and `direction: up|down`; inspect the returned tree, stop on `no_progress` |
| Unfamiliar controls/auth forms (experimental, v0.4.0+) | Opt-in `adaptive` task scope; session `act`, local input references and same-session `screenshot`/`vision_tap`; read planner-session docs first |
| Inspect an unfamiliar screen | `ui observe`; add `--include-labels` only when local private labels are appropriate |
| Wait for a known condition | Runtime `wait()`/session `wait`; one-shot `ui wait-text` for manual work |
| Diagnose the control lane | `doctor --check-ui`, not repeated status/source/screenshot commands |
| Accessibility misses a control | Same-session `screenshot`/`vision_tap` with explicit masking when AX is unavailable; do not require another XML read first |
| Find an installed app | `apps find` with exact display name or bundle ID, not shell greps of old brand names |

Before constructing grants or using a planner session, read
[task-runtime.md](../../docs/task-runtime.md) and
[planner-session.md](../../docs/planner-session.md). They contain task schema,
request examples and verification contracts. One task owns the physical device,
workflow lock, WDA session, deadline and metrics. Separate CLI invocations do
**not** share that ownership. Close a session before one-shot mutation commands.

Keep app labels, deep links, success conditions and login-specific knowledge in
caller workflows/app skills. Core control remains app-independent.

Use returned evidence for the next decision and completion check. Acquisition
already validates readiness/device/lock; do not add a status/doctor/capture chain.
Ordinary no-`after` input returns a fresh tree that verifies a unique readable
field value and can supply the next decision. A missing tree value falls back
to a targeted read; an ambiguous target is not verified. Explicit-`after` and
final actions retain targeted verification when their conditions permit it.
Ask for a new observation only when evidence is insufficient or expired.

## Input and completion

- Authorize effects, exact supplied text and expected state before acting.
  Snapshot action IDs expire; element list positions are not durable targets.
- Prefer `replace` for explicitly authorized whole-field entry: it verifies
  clear, then input. `append` requires a freshly verified **empty** field.
- Fixed-grant `keypad` requires accessible keys, a readable empty value
  and verified focus. Adaptive Act also supports a trusted caller's fresh
  screenshot confirmation for an empty/focused custom code field, with a
  destination postcondition. Neither is an automatic retry of failed bulk input.
  Keypad input is one batch from the current keyboard layout, then one final
  verification—not an observe/click/wait cycle per digit. No mid-batch decisions.
- Adaptive `input` reads approved local private-file references, not inline
  credentials. Prefer native input; explicitly choose `strategy: sequential`
  for forms that drop bulk characters. Acknowledged mismatch permits explicit
  same-readable-field clear-and-replace; unknown input still stops the session.
- `ui type`/low-level bulk input insert at the current caret; they do not verify
  the final text. Never put credentials/codes into command arguments, logs or
  cloud prompts. Do not equate “OCR found no digits” with an empty field.
- Acknowledgement, cleanup success, model `done` and app foreground identity
  alone do not prove the desired page/action completed. Use the relevant
  element/value/identity postcondition. Stop on uncertain or partial input;
  observe and replan, never replay it blindly.
- `verification_expired` after acknowledged input leaves verification pending,
  not an unknown write. `reconcile` checks the original result without typing
  again. An actual unknown write remains stopped and cannot be revived this way.

## Evidence, recovery and optional Jev

Routine observations use accessibility without screenshots. Compact output omits
values and, by default, labels. Opt-in labels, full XML and screenshots can still
contain private data; keep them local unless disclosure is separately approved.
Save visual evidence only at meaningful checkpoints/failures. Missing or
unlabeled controls require explicit planner/vision fallback, not Jev guessing.

Jev is optional, with `TYPESAFE_API_KEY` securely available in the process.
`task run --driver jev --allow-cloud` sends reviewed objective/action aliases;
use `--decision-only` for an initial read-only decision. Adaptive `task session`
can additionally send explicitly approved `cloud_labels` and their observed
role/ancestor/bounds context. It never sends supplied input or screenshots.
The default threshold is 0.7, not proof of correctness. Low confidence routes
to planner/vision fallback; it is not an API outage or a reason to rebuild the
session. Routine observations remain accessibility-only.

For disconnected/locked devices, stalled reads or service failures, read
[troubleshooting.md](../../docs/troubleshooting.md). Exact UDID/CoreDevice pins
get one bounded read-only reconnect probe. Session `recover_read` permits one
same-device reacquisition; it does not restart services or revive stopped input.
Recovery returns app-only evidence so broken AX does not block visual fallback.
Never switch phones to get past an error. Passcode-required means human unlock.

Only for a new/broken host, read [launchagent-service.md](../../docs/launchagent-service.md)
and the setup sections in [agent-automation-guide.md](../../docs/agent-automation-guide.md).
Use CoreDevice, not `iproxy`, `localhost:8100`, debug URL overrides or the removed
legacy tunnel service. Signing/trust prompts require operator setup. Do not use
fake taps as keep-alives. Check [runtime-validation.md](../../docs/runtime-validation.md)
for limits; offline tests are not proof of unattended readiness.
