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
| Inspect an unfamiliar screen | `ui observe`; add `--include-labels` only when local private labels are appropriate |
| Wait for a known condition | Runtime `wait()`/session `wait`; one-shot `ui wait-text` for manual work |
| Diagnose the control lane | `doctor --check-ui`, not repeated status/source/screenshot commands |
| Accessibility misses a control | Explicit local `ui screenshot` and, if needed, `ui source`; neither makes a guessed action safe |
| Find an installed app | `apps find` with exact display name or bundle ID, not shell greps of old brand names |

Before constructing grants or using a planner session, read
[task-runtime.md](../../docs/task-runtime.md) and
[planner-session.md](../../docs/planner-session.md). They contain task schema,
request examples and verification contracts. One task owns the physical device,
workflow lock, WDA session, deadline and metrics. Separate CLI invocations do
**not** share that ownership. Close a session before one-shot mutation commands.

Keep app labels, deep links, success conditions and login-specific knowledge in
caller workflows/app skills. Core control remains app-independent.

## Input and completion

- Authorize effects, exact supplied text and expected state before acting.
  Snapshot action IDs expire; element list positions are not durable targets.
- Prefer `replace` for explicitly authorized whole-field entry: it verifies
  clear, then input. `append` requires a freshly verified **empty** field.
- `keypad` is an explicitly chosen digits-only strategy, not an automatic bulk
  retry. It requires accessible keys, readable empty/prefix values and verified
  focus. Secure/unreadable custom fields remain unsupported. See runtime docs.
- `ui type`/low-level bulk input insert at the current caret; they do not verify
  the final text. Never put credentials/codes into command arguments, logs or
  cloud prompts. Do not equate “OCR found no digits” with an empty field.
- Acknowledgement, cleanup success, model `done` and app foreground identity
  alone do not prove the desired page/action completed. Use the relevant
  element/value/identity postcondition. Stop on uncertain or partial input;
  observe and replan, never replay it blindly.

## Evidence, recovery and optional Jev

Routine observations use accessibility without screenshots. Compact output omits
values and, by default, labels. Opt-in labels, full XML and screenshots can still
contain private data; keep them local unless disclosure is separately approved.
Save visual evidence only at meaningful checkpoints/failures. Missing or
unlabeled controls require explicit planner/vision fallback, not Jev guessing.

Jev is optional: `task run --driver jev --allow-cloud`, with `TYPESAFE_API_KEY`
securely available in the process. It receives reviewed objective/action aliases,
not raw labels, supplied input or screenshots. Use `--decision-only` for initial
calibration. Low confidence is an escalation, not an API outage.

For disconnected/locked devices, stalled reads or service failures, read
[troubleshooting.md](../../docs/troubleshooting.md). Exact UDID/CoreDevice pins
get one bounded read-only reconnect probe. Session `recover_read` permits one
same-device reacquisition; it does not restart services or revive stopped input.
Never switch phones to get past an error. Passcode-required means human unlock.

Only for a new/broken host, read [launchagent-service.md](../../docs/launchagent-service.md)
and the setup sections in [agent-automation-guide.md](../../docs/agent-automation-guide.md).
Use CoreDevice, not `iproxy`, `localhost:8100`, debug URL overrides or the removed
legacy tunnel service. Signing/trust prompts require operator setup. Do not use
fake taps as keep-alives. Check [runtime-validation.md](../../docs/runtime-validation.md)
for limits; offline tests are not proof of unattended readiness.
