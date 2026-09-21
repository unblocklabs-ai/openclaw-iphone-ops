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
