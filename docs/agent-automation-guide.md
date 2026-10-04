# Agent control

Keep one [direct session](session.md) open. Observe what you need, choose the
next action, inspect uncertainty, and decide when the user task is complete.
The harness does not plan, judge completion, impose progress quotas or demand
reconciliation permission.

Prefer semantic targets when they identify the intended control clearly. Use
images and coordinates for visual controls or unavailable AX. Neither mode
requires the other. Add `observe` to an action only when the next decision needs
fresh evidence; don't prepend a diagnosis/capture chain to every action.

Use private `text_ref` files for supplied credentials and choose explicit
`mode: replace` when whole-field replacement is intended. Native input is the
default; choose sequential only for a field that needs that compatibility path.
Inspect actual state before retrying unknown/partial input.

App/account knowledge and permission to perform consequential actions belong
to the caller. A reusable controller is not authorization to send, buy, delete,
change credentials, or clear consent prompts. Use `doctor --check-ui` for
diagnosis after a control-lane failure, not as a routine pre-action blocker.
