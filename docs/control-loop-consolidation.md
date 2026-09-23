# Control-loop consolidation (v0.4.1)

Offline validation recorded 2026-09-22 against v0.4.0 (`c983109`). This report
covers pre-release tests, not deployment, phone interaction or live Jev calls.
See the v0.4.1 release notes for subsequent installed-release validation.

**Current-behavior note (2026-09-22):** The table below is the historical
v0.4.1 consolidation baseline, not current request counts. Ordinary no-`after`
editable input now captures a reusable post-input full observation, with
targeted fallback only when its value is missing. The warm input → tap path is
14 requests rather than 16. Eligible named-ancestor targets now use WDA class
chain; other ancestor cases retain XPath. See
[`phoneagent-refactor-plan.md`](phoneagent-refactor-plan.md) for physical timings.

## Before / after

These are **offline WDA request counts**, not physical-device latency. Both
versions ran the same synthetic transport and goals with the real WDAClient
dispatch/session plumbing. Warm actions exclude their initial observation,
connection acquisition and cleanup. Verification succeeds on its first read;
polling on a slow real app adds work. Screenshot comparison starts without an
initial observation and includes explicit caller-reviewed masks on the new path.

| Operation | v0.4.0 | First consolidation | Simplified |
| --- | ---: | ---: | ---: |
| Adaptive tap | 13 | 9 | 7 |
| Adaptive empty-field input | 17 | 12 | 7 |
| Adaptive replace | 19 | 15 | 8 |
| Adaptive keypad | 18 | 13 | 9 |
| Fixed append | 15 | 12 | 7 |
| Fixed replace | 24 | 16 | 8 |
| Fixed keypad | 16 | 13 | 9 |
| Screenshot → vision tap | 19 | 9 | 7 |
| Two-step deterministic task | 34 | 22 | 16 |
| Ten offers sharing focus/value predicates | 60 | 6 | 3 |

In this historical measurement, the simplified warm tap captured one full
screen; the two-step task captured two (initial and intermediate). The other
cases issued no `/source` requests.
**No `/source` does not mean no accessibility work:** XPath queries build XML
inside WDA. These simple tap/input/predicate cases now use native predicates,
not XPath. The keypad cases retain one union XPath to validate keyboard layout.
At the time, named-ancestor targeting retained XPath rather than weakening context.
Native lookup/attributes can still take accessibility snapshots internally.

The adaptive tap has no explicit postcondition and reports verification
`unknown`; the vision tap reports acknowledgment, not completion. Input and the
two-step task verify their specified results. Setup/cleanup remain excluded.

Historical composition check: input followed by a tap took **16 requests (7 + 9)**,
down from 25 (12 + 13) after the first consolidation, excluding the initial
observation. The following tap needs two observation requests to acquire controls
not present in field-only readback; its isolated warm count was seven. Current
post-input full observation supplies those controls, making this path 14 requests.

A screenshot with an existing fresh full observation uses three requests;
without one it also uses three. Vision dispatch uses four. Each image has a distinct,
one-use ID even when captures reuse the same accessibility observation.

## All six audit items

1. **Unconditional reobservation:** fixed/adaptive actions resolve the exact
   observed native target rather than recapture XML. Simple controls use native
   identity/geometry predicates; eligible named ancestors now use native class
   chain, with XPath for other ancestor cases. Foreground state
   and local freshness/device identity guard native dispatch. Pixel evidence
   retains the original process/PID guard. Keypad layout uses one union query.
2. **Overbroad verification:** value/focus/existence predicates use targeted
   reads. Replace verifies clear and final value through the selected reference.
   An explicit stale read can re-resolve the field; a failed write cannot replay.
   Final task existence proof needs no full screen. Partial evidence cannot
   masquerade as a complete tree or prove unrelated absence.
3. **Discarded evidence:** the task loop reuses post-action observations. `done`
   independently evaluates success against fresh sufficient evidence; it does
   not always capture another screen. Mutations discard all prior choices/proofs.
4. **AX-dependent vision:** screenshot/vision dispatch do not read XML. Recovery
   returns app-only evidence. Without a current AX mask source, the caller must
   provide reviewed redaction rectangles (or explicitly confirm no masks are
   needed). Vision dispatch acknowledges the click; it does not claim completion
   or force an AX capture afterward.
5. **Repeated predicates:** identical conditions and their underlying field
   references, focus and values are shared per pass. Selected preconditions
   share the dispatch reference and empty/focus reads. Read-only predicates do
   not check hittability. These caches do not survive into later passes.
6. **Instruction overhead:** Instagram/general skills, README and operational
   docs now route multi-step work through one session and reusable evidence.
   Diagnostic CLI commands are alternatives, not mandatory preflight chains.

## Reliability fixes

- The synthetic pair of 20-second observations previously expired a 30-second
  selection before any action, without planner delay. The new path captures
  once, validates the selected target and dispatches before that deadline. Real
  stale selections still fail; the age of old evidence is never reset.
- Expiry of the read-only verification window reports `verification_expired`
  separately from an unknown mutation. Acknowledged input remains pending and
  blocks unrelated writes until read-only reconciliation succeeds. A missing
  readback does not authorize replacement. Unknown writes still invalidate the
  connection, stop input, and cannot be reconciled into permission to retry.
- App/focus/value/query reads now share the configured read timeout, not just
  XML/PNG. POST element lookup and app-state queries are classified as reads,
  not unknown writes. Polling never starts an extra read after its sleep has
  consumed the verification window.

## Deliberate simplification of the checking policy

- Read-only observations do not require an unlock preflight. Full captures use
  source plus one app-info read, not app/source/app brackets.
- Native dispatch uses WDA's known-app state endpoint rather than the heavier
  `activeAppInfo` accessibility identifier lookup. Acquisition/reconnect still
  pins the physical UDID. Same-bundle restarts can proceed after fresh native
  target resolution; visual evidence still rejects changed PID/geometry.
- Clear/type is one foreground/lock transaction. Native element typing prepares
  focus; keypad/sequential input still requires independent keyboard focus.
  An explicit caller `focused` precondition is still enforced for native input.
- Native click hittability remains. It is not imposed on value reads or native
  typing. Vision retains both current window geometry and process identity;
  there is no unproven assumption that portrait orientation never changes.
- Known emptiness, exact input readback, private evidence, task ownership,
  deadlines and no-blind-replay rules remain. Checks are not atomic with input;
  the runtime does not promise to detect every transient switch/overlay during
  a capture or an uninterrupted clear/type transaction.

## Validation

- v0.4.0 baseline: **251 offline tests passed**. First consolidation: **265**.
- Simplified candidate: **271 offline tests passed**; `npm run preflight` passed release
  metadata checks and isolated npm archive installation (62 allowlisted files).
- Python compilation, shell syntax and `git diff --check` passed.
- Both changed skills passed the skill frontmatter validator. Its PyYAML
  dependency was installed only in a temporary validation directory, not added
  to the application or global Python environment.
- Ten equivalent before/after transport scenarios passed. Ten native Foundation
  NSPredicate evaluations checked Unicode, quotes/backslashes/control characters,
  injection-like labels and moved geometry. This checks predicate syntax and
  matching, not the installed WDA/XCTest integration or device latency.
- OpenClaw development/packaging guidance informed the preflight; skill-creator
  guidance informed removal of mandatory diagnostic sequences from agent skills.

Reproduce the current request-budget tests with
`PYTHONPATH=src:tests python3 -m unittest test_control_loop -v`.

`tests/test_control_loop.py` protects the request budgets above and covers
predicate reuse, two-step observation reuse, partial-proof boundaries, slow
capture, verification expiry/reconciliation, unknown-write stops, AX-free vision,
distinct screenshot IDs, read timeouts and recovery/reporting.

The existing suites retain device/reconnect identity, ownership, stale/foreign
targets, lock state, secure input, exact readback and no-replay coverage. Test
fixtures were updated to respond to native queries instead of expecting the
removed XML reads.

The initial consolidation also checked XPath semantics locally. The current
path keeps XPath for ineligible ancestor cases and the keypad union, but no
longer adds a whole-screen secure-field XPath predicate before each fixed action:
observed secure screens still withhold fixed offers, and fixed input targets
remain non-secure editable roles.

## Remaining work and intentional checks

Absence, actionability and scroll progress still require screen evidence. A
full next-screen capture still includes one potentially expensive app-info read;
simple targeted dispatch uses the cheaper app-state route instead. Native click
hit testing and keypad layout validation remain intentional costs. There are
no speculative WDA server modifications or new runtime dependencies.

Ordinary no-`after` input captures a full next screen even if the caller stops
there; other actions capture it when the subsequent decision needs its controls.
Screenshot fallback checks app/process, geometry, age and ownership, but cannot guarantee
arbitrary pixels stayed unchanged; use it only on an inspected stable target.
Explicit masks remain the trusted local caller's responsibility.

The [narrow physical comparison](phoneagent-refactor-plan.md) now measures a
synthetic input/navigation gain on Bill. Before generalizing, measure verified
completion, total latency, route counts, tails, and reconciliation on broader
screens; test an AX-broken screen and custom keypad separately. No X login,
unattended-readiness, or model-performance gain is established.
