# Adaptive Act: proposal and acceptance criteria

Release target: v0.4.0. The dated implementation results below describe staged,
pre-release experiments; see [the input latency audit](input-performance.md)
for the subsequent optimized build and remaining live-validation limits.

## Problem and direction

The v0.3.0 X login attempt stopped after 450.9 seconds without entering an
email, password, or code. Approximately 30 seconds were measured device calls;
most elapsed time was controller adaptation, tool round trips, and inspection.
There were no Jev calls in that attempt. Bill's earlier ~16 minutes included
installation and recovery, so it is not an equivalent login benchmark.

Build on `TaskConnection` and the existing planner session: a trusted caller
supplies a narrow intent; deterministic targeting or Jev resolves contextual
candidates; local code validates, dispatches once, and returns the next state.
App-specific account knowledge stays in workflows, not the runtime.

### Build scope

- Opt-in adaptive requests in the existing JSON-lines session; preserve fixed
  grants and deterministic workflows unchanged.
- Exact observed targets first; contextual Jev choice when needed, with a
  **0.7** acceptance threshold and an explicit no-match/planner fallback.
  Include hittable custom/static controls, not only a role whitelist.
- Separate local execution from cloud disclosure. An unrelated password node
  must not block navigation or email input. Cloud context is explicitly
  approved control text, never field values, credentials, or full XML.
- Same-session screenshots and snapshot-bound vision taps using observed
  device/image geometry. No hardcoded phone dimensions or sidecar controller.
- Local private-file input references; readable-field verification and an
  explicitly selected sequential/native-keypad strategy for custom auth input.
  An auto-submitting code can be verified by its destination state.
- Acknowledged navigation mismatch returns control for correction. Unknown
  writes stop input and are never automatically replayed.
- Useful pre-ready errors, bundled post-action observations, and existing
  transport/model metrics. No new daemon, database, vault, or policy framework.

## Success criteria (record failures as well as passes)

1. **Session usability:** one process handles newly encountered intents and
   custom/static targets without changing its task file or restarting.
2. **Grounding:** exact targets are deterministic; a live Jev contextual choice
   at >=0.7 executes once. Low confidence/no match returns a usable fallback,
   not a permanent task stop or a claim of provider failure.
3. **Local auth/privacy:** unrelated secure nodes do not block allowed local
   actions. Supplied secrets never appear in model payloads, result JSON,
   exception text, or persisted evidence. Private references stay local.
4. **Vision:** a screenshot and image-coordinate action use the same pinned
   session, validate app/process/snapshot/freshness and actual geometry, and
   reject stale or replayed targets.
5. **Outcome handling:** an acknowledged navigation mismatch can be inspected
   and corrected. Unknown or unverified input cannot be blindly repeated.
   Auto-submit success depends on a verified destination, not field readback.
6. **Physical X proof:** authenticated UI **and exact account identity**, not a
   handle appearing on a logged-out account chooser. Restore login before
   attempting another logout. No social actions, purchases, password resets,
   security-setting changes, or destructive operations.
7. **Timing:** prepare the workflow before timing. Record caller-visible total,
   device calls/time, model latency/cost, mailbox wait, fallback count, and all
   blocked attempts. Target <120 seconds excluding separately reported email
   delivery; also report inclusive time. This is an aspiration, not a claim.
   Do not publish p95 conclusions from a handful of exploratory runs.
8. **Generality:** exercise the same core Act on a non-X form/navigation task.
9. **Regression/package:** full offline suite (prior baseline: 224), focused
   tests for new high-risk paths, and packed-install validation pass.

## Experiment boundaries

At most **five build/test iterations**. Each iteration includes a coherent
change set, offline checks, a staged Bill test (or explicit blocker), and a
recorded conclusion. Stop earlier if acceptance criteria are met. Stage code
separately from Bill's installed npm release; do not publish or release here.
Keep credentials/OTP out of CLI arguments and model/tool logs. Use existing
Bill credentials locally. Screenshots are deliberate private checkpoints,
never automatically sent to Jev (which is text-only).

## Iteration ledger

Baseline: main `1f62ed5`, 224 offline tests passed in 13.802s. Bill v0.3.0,
pinned device reachable, unlocked, WDA ready, screen read successful.

### Iteration 1

- Built opt-in adaptive session requests, contextual Jev target selection,
  private input references, same-session redacted screenshots/vision targets,
  explicit outcome handling and startup diagnostics. Fixed a truncated-PNG
  acceptance case found by the new tests before staging.
- Full suite: **235 passed**, 13.102s. Physical test used a separate private
  checkout on Bill; his installed release and WDA service were unchanged.
- Live Jev selected the email field: confidence **0.84**, inference **0.336s**,
  680 input / 118 output tokens, estimated $0.00002856. One acknowledged tap.
  This worked on the auth screen containing the unrelated secure node.
- Same-session screenshot: **1.671s**, 828×1792 pixels / 414×896 points, two
  input regions masked, owner-only file. No screenshot went to Jev.
- Two email-input attempts were **not sent**. Read-only diagnosis established
  that WDA reports a 15-character placeholder as the field value; focus matched,
  and the native value exactly equaled native `placeholderValue`, not the local
  account email. The current empty check cannot distinguish it without an
  explicit clear. No credential or code was typed; X remains logged out.
- Next: support acknowledged clear plus native-placeholder verification,
  identify validation stage in responses, trim structural/keyboard observation
  noise, and support explicit snapshot-bound visual confirmation for custom
  empty/focused code fields that have no readable AX value.

### Iteration 2

- Full suite: **238 passed**, 13.076s. Added placeholder-aware explicit
  replacement, stage-specific validation diagnostics, compact relevant
  controls, and snapshot-bound caller confirmation for custom keypad input.
- Bill: email replacement acknowledged clear and native bulk entry in
  **7.223s**, but exact readback failed. Input was not repeated and Continue
  was not tapped. A read-only native probe confirmed the retained value was
  nine characters, not the expected email and not the placeholder.
- This establishes a second real app issue, not transport failure: native
  bulk input is not reliable on this X form. It does not establish which
  layer lost characters. No code requested, no authenticated state.
- Next: expose the existing slower sequential WDA entry as an explicit
  strategy, retaining exact readback. Allow explicit clear-and-replace of
  the same readable field after an acknowledged mismatch; unknown writes
  remain stopped. Store expected readback for read-only reconciliation.

### Iteration 3

- Full suite: **240 passed**, 13.005s. Added explicit sequential entry and
  read-only reconciliation / explicit same-field replacement after a known
  readback mismatch.
- Live test caught an implementation defect: X's email node has a name and
  an empty-string label. Constructing the readback selector rejected that label
  after the clear, before typing. No sequential input or code was sent.
- Next: normalize empty optional selector attributes, construct predicates
  before mutation, add the exact regression fixture, and verify app/focus
  before each sequential character. This is an unsuccessful iteration, not
  a successful login benchmark.

### Iteration 4

- Full suite: **242 passed**, 13.847s. Fixed empty optional labels before
  mutation and guarded app/focus per sequential character.
- Two local email entries verified exactly (19 characters): **24.236s** and
  **22.652s**. A live Jev selection abstained at 0.33 (**0.346s**); exact-selector
  fallback continued in the same session, proving abstention is not terminal.
- Found the app routing distinction: saved-account “Add another account →
  Continue with Email” uses password login. The welcome email icon opens the
  code route and explicitly returned “Enter the code we sent you to login”
  for the existing email. No new account was created or password reset.
- Same-session screenshot-grounded tap on the unlabeled email icon succeeded,
  using actual 2× geometry. App relaunches and unfamiliar controls did not
  require rebuilding the controller. One fresh code was retrieved locally in
  **1.323s**; no code was printed or sent to Jev.
- Code entry was **not sent**, reporting a stage that covered both hit testing
  and later visual validation. A subsequent read-only probe found one hittable
  container but **no native active element** (WDA 404). Thus the initial hit-test
  diagnosis was not established. The screenshot also showed a changing retry
  countdown; requiring a whole-tree signature match can reject an unchanged
  input for this unrelated change. Final build reports the precise stage and
  revalidates the exact field/app/freshness rather than unrelated labels. Only
  actual digit keys require hit testing in the visual-confirmed keypad path.
- 529.4s session elapsed, including route discovery/inspection; still not a
  completed or comparable login-speed benchmark. X remains at the empty code
  field pending the final iteration.

### Iteration 5 — final; implementation iteration limit reached

- Final offline suite: **242 passed**, 13.665s. Release metadata agrees at
  0.3.0; isolated npm pack/install passed with **60 allowlisted files**. This
  is an unreleased source change, not a republished 0.3.0. `git diff --check`
  passed. A missing task file now emits structured pre-ready failure output.
  Final full `npm run preflight` rerun also passed (242 tests in 13.762s).
  Bill's closing `doctor --check-ui` passed: unlocked, WDA ready, screen read OK,
  installed source/provenance still matching the unchanged npm v0.3.0.
- A fresh screenshot showed all six custom code slots empty and the insertion
  caret/number pad visible. One local-code keypad operation acknowledged six
  native key taps. It took **162.234s**, then destination verification raised a
  read failure and stopped input. **No code was replayed.**
- One same-device read recovery completed in **1.337s**. The resulting screen
  still contained `verifycode` and **“An unexpected error occurred. Please try
  again.”** It was not authenticated. This does not establish whether input,
  code validity, app state or the remote authentication service caused the
  error. Six acknowledged taps do not prove six correct characters arrived.
- The transport trace does establish the dominant latency: **114.394s** in six
  `/element/:id/click` requests (five about 20.6–20.8s, one 11.1s), plus
  **17.349s** in six element queries. There were **zero Jev calls** in this code
  entry phase. Investigate XCTest idle/quiescence behavior and compare the
  native-click path with freshly validated key-coordinate dispatch; the trace
  does not by itself prove the cause of those waits.

#### Non-X validation, same final build

A local-only Safari fixture required focusing a synthetic field, replacing its
text, and selecting Finish. Completion independently checked the fixture's
expected input and visible Completed state. No external form was submitted.
One shared pinned connection, alternating exact / Jev / Jev / exact; timings
below exclude fixture loading and measure the three requests end to end.

| Target resolution for final button | Completed trials | Median total | Device calls/trial | Model latency |
| --- | --- | --- | --- | --- |
| Explicit deterministic selector | 2/2 | 18.718s | 52 | None |
| Jev contextual choice | 2/2 | 19.474s | 52 | 0.334s, 0.394s |

Both Jev decisions had **0.86 confidence**, costing an estimated $0.000037632
and $0.000038052 in input tokens. This demonstrates generality and small model
cost, **not** a latency advantage over known exact selectors. Two trials per
mode do not support a p95 or statistically meaningful speed claim. This is not
a full general-purpose planner comparison or a before/after transport trial.

Two earlier fixture-harness attempts were also retained: an ambiguous label
first caused no dispatch, then a focus tap followed by an ambiguous input label
caused no typing. The harness was corrected to use explicit field/button roles;
**no runtime code changed after staging iteration 5**. These setup failures are
not included in the four correctly configured trials above.

## Acceptance result and remaining work

| Criterion | Result |
| --- | --- |
| 1. Session usability | Demonstrated on X: new intents, static/custom controls, app relaunch and vision fallback without rebuilding that session |
| 2. Grounding/fallback | Live Jev selections at 0.84 and 0.86; 0.33 abstention followed by deterministic fallback in the same session |
| 3. Local auth/privacy | Exact email entry verified; OTP stayed local; credential/cloud projection tests pass. Generic secure-password flows remain unverified |
| 4. Vision | Live geometry-bound unlabeled-icon tap succeeded; stale/replay/format handling covered offline |
| 5. Outcomes/recovery | Native bulk mismatch detected, no automatic replay; read recovery retained input stop. Custom code entry did not complete |
| 6. X authenticated identity | **Not met. X remains logged out** |
| 7. Login latency target | **Not met; no valid comparison to Bill's earlier ~16 minutes** |
| 8. App independence | Safari form completed with exact targeting and live Jev using the same runtime |
| 9. Tests/package | 242 offline tests, metadata and packed-install checks passed |

**Verdict: useful experimental implementation, not ready to claim unattended
X authentication or a successful login-speed improvement.** Stopped after five
implementation iterations as requested. No release, commit, PR or production
installation change was made. Bill's installed CLI remains v0.3.0; staged code
and private reports are separate.

Priority next work (not implemented after the limit):

1. Reproduce and eliminate the measured native keyboard-click waits, then run
   a prepared, equivalent login trial without editing its controller mid-run.
   Do not lower Jev confidence to address a phase that made no Jev calls.
2. Make opaque-input submission/verification usable across ordinary password
   forms that need a separate Submit, not just auto-submitting destinations.
   Test native secure entry and a verified destination with synthetic secrets
   before claiming general authentication support.
3. Tighten failure classification: a failed read-only WDA POST element query
   currently shares `WDAOutcomeUnknown` with mutations; adaptive handling can
   over-stop before dispatch. Also report the actual verification phase rather
   than the most recent pre-input stage after a late read failure.
4. Validate screenshot redaction beyond known input geometry. It is intentionally
   not general image anonymization; new sessions do not know input from a prior
   session, and private text absent from AX can remain in an image. Keep images
   local unless separately approved. Do not treat them as sanitized uploads.

### Evidence and reproducibility

Content-free run reports are on Bill in
`/Users/billjohansson/iphone-adaptive-test.XzsXfF/`: `result-1.json` through
`result-5.json`, and the three `safari-benchmark*.json` reports. Private
screenshots are in that directory's per-capture subdirectories. Test adapters
are there too; the X adapter's historical mailbox lookup is validation-only,
not a production credential-source design. Reproduction requires provisioning
new private input files and explicit device/cloud authorization.

Temporary copies of email/OTP and synthetic input files were deleted after the
runs. Original account/mailbox credentials and TypeSafe key were untouched;
reports contain no input values. Bill's WDA service and npm installation were
not replaced. The OpenClaw skills informed packaging/source provenance and
agent-facing documentation; they do not substitute for physical-device proof.
## Subsequent latency work

The five-iteration history above is retained as recorded. The later user-requested
input/wait optimization has its own [measured results and limitations](input-performance.md).
