# App-only observation benchmark — 2026-09-21

**Status: complete supervised before/after comparison. All 15 measured tests per
arm passed after operator PIN unlock. The Calculator → Settings task improved
from 12.623 to 3.039 seconds median (4.15×); this is not a general iPhone or
unattended-readiness claim. At measurement time the candidate was staged, not
released or globally installed.**

These measurements describe the pre-review candidate identified by the hashes
below. Subsequent fixes to final-step completion and benchmark source validation
are offline-tested separately; they have not been re-benchmarked on the phone.

## Scope and method

This pass optimizes app-identity-only waits, not typing, target selection or Jev.
Tests only foreground Calculator/Settings and perform read-only verification.
No screenshots, UI text, model calls, social actions, purchases or device settings
changes are involved. The installed npm release and WDA configuration are untouched.

The baseline is the installed v0.2.0 package on Bill's physical USB-connected
iPhone, corresponding to main `a85b2b16c6465087c5ec93042c7c1b7e7966bae8`.
Its source-file hashes are recorded with each series. The candidate is staged
separately, not installed over the release.

Both arms used the pre-review version of
[`scripts/benchmark_app_observation.py`](../scripts/benchmark_app_observation.py).
Its frozen SHA-256 was:
`edab49a9ca1c73a93fc6821ba5a87a9029f671429d06181a1a88edd17758512e`.
The current script additionally rejects missing source packages or imports from
a different checkout before reading device configuration; the measured workflow
and timing boundaries are unchanged.

- Five intended repetitions per test: Calculator → Settings task, settled Settings
  app wait, settled Calculator app wait. Unexpected state or failure stops the
  series; no automatic input replay.
- Same explicit configured device, task lock, 90-second task budget, 20-second
  request/verification bounds and 30-second freshness.
- Transition totals include connection acquisition, installed-app checks, execution,
  verification and session cleanup. Reset setup is recorded but excluded. A new
  connection independently checks expected foreground identity after each success.
- Settled-wait timing isolates `Executor.wait`; setup, session ownership and
  independent before/after identity checks are excluded from that metric.
- Setup checks a full screen, refuses unexpected/secure apps, activates an approved
  app at most once, and uses bounded read-only identity polling to settle.
- Evidence is created in new owner-only directories/files. Public summaries omit
  physical device identifiers, addresses, accessibility content and credentials.

## Results on the same recovered service

Baseline and candidate used the identical harness, request/verification bounds,
physical phone, retained app states and running WDA process. The baseline ran
first (19:39–19:42 UTC), followed by the candidate (19:43–19:44 UTC); there was no
service restart or configuration change between arms. Each test has five samples
per arm, and all transitions also passed a separate-connection foreground check.

| Test | Baseline median | Candidate median | Median speedup | Baseline / candidate exploratory p95 |
| --- | ---: | ---: | ---: | ---: |
| Calculator → Settings, full task | 12.623 s | 3.039 s | 4.15× | 12.873 / 3.135 s |
| Settled Settings app-only wait | 10.223 s | 0.574 s | 17.81× | 10.268 / 0.583 s |
| Settled Calculator app-only wait | 0.789 s | 0.192 s | 4.10× | 0.817 / 0.197 s |

| Request measure, per sample | Baseline | Candidate |
| --- | ---: | ---: |
| Transition WDA calls | 20 | 19 |
| Transition full source reads | 3 | 2 |
| Transition CoreDevice calls | 4 | 4 |
| Settled-wait WDA calls (either app) | 4 | 3 |
| Settled-wait full source reads (either app) | 1 | 0 |

The transition's median source-read time fell from 10.813 to 1.233 seconds.
The two remaining source reads inspect Calculator for initial selection and
fresh pre-dispatch validation. The removed Settings source read cost about
9.6 seconds; lock and matching foreground bundle/PID checks remain. The runtime
does **not** make source acquisition itself faster or remove target safeguards.

There were no failed samples or session-cleanup warnings in either recovered
arm. A final independent full-screen check confirmed unlocked Calculator with no
secure field, successful session cleanup and the same WDA process. Installed
release sources still match the original baseline manifest. LaunchAgent/device
configuration checksums remain unchanged.

### Recorded samples

Seconds in trial order; rounded only for presentation:

| Test | Baseline | Candidate |
| --- | --- | --- |
| Transition | 12.873089, 12.625800, 12.586517, 12.623453, 12.607824 | 3.062176, 3.134932, 3.039339, 2.989841, 2.985487 |
| Settings wait | 10.222513, 10.268415, 10.225917, 10.156413, 10.116013 | 0.573944, 0.582937, 0.577947, 0.572015, 0.567505 |
| Calculator wait | 0.778925, 0.771277, 0.815820, 0.816570, 0.788914 | 0.157586, 0.196800, 0.191036, 0.197219, 0.192348 |

The changed runtime files matched the staged candidate before and after testing:

- `actions.py`: `351dc26cfaff28471020e9eed4645512adbafb3bb227abfdf3c0f5d7b6a9526b`
- `observations.py`: `5869188b04b7aa2e926c64c5de4d7aa59527278667a0c75c923cfa1790ab33ff`
- `tasks.py`: `bebe5db589c6e81f07b4757f279ffb36555d9e2c4bcacfe1f4237d88e777051d`

Full-precision records, manifests and private CoreDevice diagnostics are retained
outside the repository. Raw device evidence is not packaged or published.

## Original pre-edit baseline (before recovery)

| Test | Verified samples | Median | Exploratory p95 | WDA calls per sample | Full source reads |
| --- | ---: | ---: | ---: | ---: | ---: |
| Calculator → Settings, full task | 5/5 | 12.638 s | 12.650 s | 20 | 3 |
| Settled Settings app-only wait | 5/5 | 10.230 s | 10.442 s | 4 | 1 |
| Settled Calculator app-only wait | 1/2 attempted | 0.804 s successful sample | Not meaningful | 4 successful sample | 1 successful sample |

The remaining three Calculator samples were not attempted. The median total
source-read time was 10.761 seconds per transition and 9.656 seconds per settled
Settings wait. Nearest-rank p95 with five samples is just the slowest sample;
this is exploratory, not a production latency estimate.

### Failures retained separately

1. An initial harness attempt completed one transition in 12.713 seconds, then
   stopped when its immediate reset identity check failed after acknowledged
   activation. No activation was replayed. Independent reads later confirmed
   unlocked Calculator. Setup was corrected to bounded read-only polling, and
   the five-sample baseline above was captured with that frozen corrected harness
   before editing runtime code. The initial attempt is not pooled with it.
2. At the second settled Calculator sample, a `WDAUnavailable` error and session
   cleanup failure stopped the baseline. Subsequent independent `/status` reads
   closed without an HTTP response; the existing launchd runner process was still
   alive. These observations do not identify the root cause.
3. After explicit approval, the existing WDA LaunchAgent was restarted once under
   the workflow lock. The launch plist and device-config SHA-256 checksums remained
   unchanged, and every installed-package Python source still matched the baseline
   manifest. The existing launch command performed its normal build/sign/start
   pipeline; no signing configuration was edited. A 90-second read-only readiness
   wait did not succeed. Two Xcode startup attempts reported: `The test runner
   failed to initialize for UI testing. ... Timed out while enabling automation
   mode.` The second attempt was the service's existing automatic retry, not an
   additional manual restart. Fresh CoreDevice checks reported the pinned phone
   detected, unlocked and tunnel-connected. WDA remained unavailable;
   no candidate benchmark or input replay occurred. The LaunchAgent was left with
   its existing retry policy, not reconfigured.
4. The operator subsequently reported that the phone required its PIN and unlocked
   it. WDA then passed readiness, unlocked-state, foreground identity and full
   Calculator accessibility checks without another manual service restart. The
   service's automatic retries had continued during the blocked period. Earlier
   CoreDevice reports were therefore not sufficient evidence of UI-automation
   readiness or the absence of a PIN barrier. The exact onset/cause of the lock
   and its relationship to the initial transport failure are not established.

## Implementation and offline validation

App-only waits now read lock state and matching foreground bundle/PID twice,
without full source acquisition. They return explicitly incomplete screen
evidence (`elements=None`, `secure=None`). That evidence cannot authorize any
action or model input, prove element absence, or verify field/scroll state.
Full observation before selection and fresh target validation before dispatch
remain unchanged. Mixed and element predicates still use full accessibility.

- Baseline: **173 tests passed** locally.
- Candidate: **182 tests passed locally and on Bill** (Python 3.14).
- Post-review fixes: **187 tests passed locally**, including final-step completion
  for deterministic/mocked Jev drivers, failed completion reads without replay,
  and rejection of missing or mismatched benchmark sources. These fixes were not
  rerun on Bill; the live hashes/results above remain the pre-review record.
- Local `npm run preflight` passed: version alignment, full suite and isolated
  packed npm installation. Publish-related test messages are mocked; nothing
  was published.
- Focused coverage includes skipped source requests for app-only waits, full
  source for mixed predicates, unknown element/secure state, rejected stale and
  incomplete offers, cloud exclusion, PID changes, lock/read failures, restored
  deadlines/cancellation and acknowledged actions never replayed on failed reads.

The OpenClaw packaging/development skills guided isolated staging and preflight;
this is a Python CLI/skill bundle, so native plugin-inspector checks do not apply.

Offline tests establish the new request shape and failure handling; the live
results above establish latency for these specific tasks. Neither establishes
unattended readiness or improves WDA service recovery.

## Limitations and reproduction

- Five samples per test; nearest-rank p95 is the slowest sample, not a robust tail
  estimate. Arm order was sequential, not randomized. The recovered baseline
  closely reproduced the earlier baseline, and request traces isolate the omitted
  source read, but these timings should not be generalized to all apps/devices.
- An app condition verifies foreground identity only, not rendered-page readiness,
  deep-link destination content or screen safety. Such requirements still need
  element/value conditions and full observation. Reset/setup time is excluded
  and still includes a full source read; it did not receive this optimization.
- No Jev, planner, typing, scrolling or visual-targeting speedup was tested in this
  pass. No lock/disconnect fault was intentionally induced. Existing unit tests
  cover failure paths, but the real PIN barrier still required human intervention.
- No forbidden device operations were requested. Transport/identity evidence is
  not an independent audit of every possible external side effect; missing
  wrong-target/unintended-action annotations are not inferred zero counts.
- No phone restart, reset or security-setting change was performed. The original
  failed series and recovery attempts above are retained rather than pooled into
  the successful recovered comparison or omitted from the record.

Example invocation (the source tree must already exist; output must not exist):

```sh
python3 scripts/benchmark_app_observation.py \
  --source /absolute/path/to/baseline-or-candidate \
  --output /absolute/path/to/new-private-run \
  --arm baseline --repeats 5
```

Use `--arm candidate` for the candidate, changing no timing/task parameters.
Retain all failures and compare verified completion, median/p95 totals, source
time/counts and total WDA calls. Stop on a failed/uncertain mutation; independent
read-only diagnosis is required before any new workflow, never blind input replay.
