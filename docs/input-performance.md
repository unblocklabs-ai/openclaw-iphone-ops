# Input latency audit — 2026-09-21

Release target: v0.4.0. Measurements below were taken before publication from
staged source; Bill's installed npm v0.3.0 was unchanged during those tests.

## Measured result

On Bill's physical iPhone, six synthetic keypad digits fell from **30.78s to
8.99s median end to end (3.4× faster, 71% less time)**. Device calls fell from
**88 to 22**. The final build's six actual touches take **1.78–1.83s in one request**;
the remaining time is target validation and final observation.

These are Safari fixture measurements, **not a successful X login**. The earlier
X attempt spent 114.394s in six native clicks and 162.234s overall. Bill's WDA
source/logs confirmed repeated 10-second XCTest idle waits there. That historical
X trace is diagnosis evidence, not an equivalent before/after benchmark.

## Method and outcomes

- Baseline: the pre-optimization, uncommitted Adaptive Act build on
  `codex/adaptive-act` (242 passing tests), not the installed npm release.
- Same physical device, same Safari page, same synthetic `121212`, same explicit
  final-value predicate, same task-scoped WDA ownership, no Jev or cloud calls.
- A local HTTP fixture has no accounts, external submissions, credentials or
  social actions. Every trial loads a fresh page and verifies focus first.
- Timings exclude fixture loading/focus; the harness reports that setup time
  separately. Calls include validation, dispatch and verification.
- Three baseline trials per case; too few samples for a credible p95 claim.

| Case | Baseline verified trials | Baseline median / calls | Optimized result |
| --- | --- | --- | --- |
| Adaptive keypad | 3/3 | 30.775s / 88 | Final build 3/3; median 8.987s / 22 |
| Sequential input | 3/3 | 12.627s / 56 | Final build 3/3; median 7.542s / 22 (40% less time) |
| Tap + destination check | 3/3 | 4.010s / 14 | Final build 3/3; median 4.086s / 13 (no demonstrated speed gain) |
| Fixed-grant keypad | 0/1 | Validation rejected before dispatch | Corrected native-null emptiness handling; final build 3/3; median 7.005s / 16; no valid paired timing baseline |

Two earlier corrected builds also passed 3/3 keypad trials each. Their median
times were 9.243s and 9.219s. Final build cleanup succeeded with no warning.

Additional final-build checks passed **4/4**: six identical digits and mixed
digits with local auto-submit, each through both adaptive and fixed-grant
keypads. Auto-submit removes the input after checking the exact expected text;
completion is verified on the destination, not an acknowledgement. Total final
physical coverage: **16/16 completed**. Source hashes in the pattern reports
match the local runtime. Closing `doctor --check-ui` verifies service health
separately; it is not a performance measurement.

Failed trials are retained, not excluded from the implementation history:

1. Initial baseline harness omitted the adaptive keypad's explicit `after`
   condition; it returned unknown. Subsequent comparable trials specify it.
2. First optimized gesture reused a lifted touch path. Bill's WDA synthesized
   duplicate digits. Final-value verification rejected it; no retry occurred.
   The replacement uses separate, nonoverlapping touch paths in one request.
3. Batched text key events entered only the first character. That optimization
   was removed. Sequential compatibility input keeps individual HTTP requests,
   but removes per-character screen/focus reads, sessions and redundant pacing.
4. The next sequential trial acknowledged six key requests, then WDA became
   unreachable during native value readback; cleanup also failed. No replay.
   One explicit runner restart subsequently failed with **“Timed out while
   enabling automation mode.”** Device discovery still reported the correct
   phone and no passcode requirement. Cause is not established; phone-side
   inspection was requested. After the user reported it fixed, `doctor --check-ui`
   passed and the final build completed all twelve comparison trials. This is
   not proof that the cause of that interruption is fixed in code.

## Hot-path audit and changes

| Area | Change / decision |
| --- | --- |
| WDA session | Set `waitForIdleTimeout=0` and `animationCoolOffTimeout=0` once per owned session. Callers verify actual readiness/results instead of also waiting for whole-app idleness. Nested sessions reuse it. |
| Both keypad executors | Resolve all digits against one fresh native keyboard; send one bounded touch sequence; verify final value or explicit auto-submit destination. Removed per-digit XML, XPath, hit-test and prefix loops. |
| Sequential compatibility typing | One session and input-boundary checks; separate key requests because live batching lost input. Cadence subtracts time already spent in the request. Long input rechecks lock every 32 characters; cancellation/deadline checked at request boundaries. |
| Observation | App-only reads issue one app-identity request, not two with nothing between them. Full AX observations retain before/after app/PID checks. |
| Verification | Stop at a decisive unsatisfied predicate. A wait with no predicates returns one observation and unknown immediately, rather than polling the full timeout. |
| Input readback | Reuse a value present in fresh XML; when omitted, read the original native reference instead of reserializing/querying the full tree again. |
| Lock checks | Transport checks before mutation remain. Removed the duplicate lock request inside the app-identity guard; relaunch termination now checks lock at the transport boundary too. |
| Targeted legacy clear | Clear the validated native field directly; removed coordinate focus tap plus a blind 200ms sleep. Raw clear without a selector retains its active-field semantics. |
| Legacy scrolling | Read immediately and poll for visible change; removed the unconditional 400ms sleep. |
| Instagram recipes | Replaced fixed 0.8–2s sleeps with read-only polling for new media or matching profile identity. Reuse the resulting XML for evidence instead of reading again. No social actions were tested. |
| Startup/recovery | Keep UDID selection/reconnect checks, workflow ownership, bounded reads and explicit recovery. They are not repeated per keypad digit and are not the measured bottleneck. |
| Evidence/Jev | No routine screenshots added; no credentials or field values sent to a model. Existing evidence permissions and model limits are unchanged. |

Remaining sleeps are predicate polling/backoff or deliberate input cadence,
not unconditional readiness delays. `wait_source` bounds when polling starts;
an in-flight read remains bounded by the client read timeout and task deadline.
Its returned last snapshot does not itself prove readiness.

## Contracts and limits

- A keypad batch is one stable-field operation, **not a multi-screen macro**.
  It cannot inspect focus, stop or react between digits. A timeout may leave a
  prefix entered. Never replay it automatically.
- Key centers come from one current, visible, unambiguous native keyboard.
  Unknown/unlabeled/custom key layouts still need another explicit strategy.
- Exact native field references, app/PID checks, device pinning, workflow lock,
  freshness, deadlines, private evidence and uncertain-write handling remain.
- Disabling XCTest global idle waits means raw low-level callers must wait for
  their own expected state. An acknowledged tap is not verified completion.
- The final readback path passed the synthetic physical tests. Legacy recipe
  changes have offline coverage only, not a new live performance claim.
  X login, other hosts and unattended soak tests
  remain unverified. Bill's installed npm v0.3.0 was not overwritten.

## Reproduce / complete validation

After Bill's runner is healthy, on its host from the staged checkout:

```sh
PYTHONPATH=src python3 scripts/benchmark_input.py \
  --address <Mac-LAN-address> --output /private/existing-dir/result.json \
  --label candidate --runs 3 --cases keypad sequential tap grant-keypad
```

The script is opt-in physical-device validation, not part of the offline suite.
Use distinct report paths and identical fixture/case parameters for each build.
Reports contain timings/route counts, not input payloads or accessibility text.
Its exact temporary synthetic-input file is deleted on exit.

Use `--digits 111111` for repeated keys and `--digits 123456 --auto-submit`
for a mixed-digit fixture that removes the field and independently verifies
the entered value before showing Completed. Neither fixture contacts a service.

Remaining release validation: verify an authorized fresh X login on the
published package and compatibility on other installed WDA/iOS versions. Compare
same tasks/start state; do not label the earlier 16-minute install/login session
an equivalent timing baseline.

Private run artifacts on Bill: `/Users/billjohansson/iphone-fast-test.a6JdLu/`.
Local copies/check logs: `/tmp/iphone-fast-test.1JOl3l/`. Reports retain baseline,
failed prototypes and corrected runs. Source tarballs identify the tested code.

## Offline validation

- Baseline: 242 tests passed.
- Final runtime: **251 tests passed**; includes batched touch timing, ambiguous
  keyboard rejection, no-replay/unknown outcomes, deadline and lock handling,
  auto-submit completion, settings ownership, immediate waits and XML reuse.
- `npm run preflight`: passed; version checks plus isolated install of the
  actual npm archive, **61 allowlisted files**. No release/publish was performed.
- `compileall` and `git diff --check`: passed.
- OpenClaw development skills guided package/provenance checks and preservation
  of the existing worktree; they are not physical-device proof.
