# Runtime validation — 2026-09-21

This is a supervised development record, **not unattended acceptance** or a
claim that Jev is faster than a planner. No social actions, purchases, installs,
security changes, destructive device actions or service restarts were performed.
Bill's installed npm package and WDA service configuration were not modified.
Tests used an isolated staged checkout and the existing workflow lock.

## Offline evidence

- Current-main baseline (`347009c`, v0.1.1): **136 tests passed** on Python 3.14.
- Implementation: **173 tests passed both locally and on Bill's Mac**, including existing safety regressions,
  snapshot/focus/identity faults, no unknown-write replay, strict task/provider
  parsing, privacy projection, completion checks and constant bulk-call counts.
- `npm run preflight`: release metadata, full suite and isolated packed npm
  launcher validation, including the task subcommand. No release was published.
- Wheel and source distribution built successfully; the wheel's installed task
  entrypoint was checked outside the source checkout in an isolated environment.
- Generic iPhone skill validation passed (validator dependency installed only
  in a temporary environment, not added to the package).

At the HTTP-opener boundary, a synthetic 30-character request-count comparison
against the unchanged main implementation produced:

| Transport path | Requests |
| --- | ---: |
| v0.1.1 W3C typing | 120 |
| New W3C typing, one session per string | 63 |
| New native bulk typing, standalone | 4 |

This probe uses mocked HTTP responses. It proves reduced request count, **not
live elapsed-time speedup**. The safe executor adds observation, focus,
targeting and readback requests; those are not included in these typing-only
counts. No arbitrary character chunking or high-frequency tuning was enabled.

## Physical iPhone evidence

Host: Bill's Mac, Homebrew Python 3.14.6, existing USB/CoreDevice transport.
WDA checkout revision: `6359d34049e7592371d5ed4846f5ce522bdc4569`.
Device identifiers, addresses, private XML/screenshots and credentials are
intentionally absent from this repository.

- Task ownership: physical UDID present, ready/unlocked checks passed, foreground
  app read and single-session cleanup passed. Initial acquisition used three
  CoreDevice calls; an installed-app check adds one when activation is granted.
- **Calculator / Settings:** authorized app transitions exercised. A full
  Jev-driven Calculator → Settings task completed with independent app
  verification, one acknowledged action and successful cleanup. Total **12.893
  seconds**, inference **0.301 seconds**, **630 input / 101 output tokens**.
  Published-rate estimate: **$0.00002646**, not an authoritative bill.
- **Safari synthetic fixture:** a temporary HTTP server bound only to the Mac's
  CoreDevice tunnel interface served a non-submitting page with one labeled
  input. The server was shut down after each run. Page arrival and field focus
  were independently verified. ASCII append, Unicode/emoji append, replacement
  (verified clear then append), and final clear all reported **acknowledged +
  satisfied**. Three native targeted value writes and two clear writes shared
  one WDA session; cleanup succeeded. No input was submitted or stored remotely.
- Full source reads dominated several trials: Settings source reads were about
  **9.6 seconds** each, while a stable Calculator source read was **0.64 seconds**.
  Defaults were set to 30-second freshness and 15-second verification, with
  fresh target revalidation still required. Per-task tuning remains necessary.

Earlier failed/blocked trials are not counted as successes:

- Short verification bounds expired after acknowledged activation. Safe
  observation retry during app transitions was added; no input was replayed.
- A Settings-back test required General to be visible, but the retained list
  scroll position made that postcondition wrong. The executor stopped; a later
  read-only observation confirmed Settings root. This demonstrates that task
  predicates must encode the intended state, not assumptions about scroll position.
- Native Settings search did not provide reliable focus verification in that
  attempt. Placeholder-valued fields remain conservatively unsupported for
  verified clear/replace when empty cannot be observed.
- The first synthetic empty field omitted XML `value`; typing was refused before
  dispatch. WDA's deployed source confirms explicit `value: null` means empty;
  the runtime now uses that separate endpoint, with regression and live tests.
- A fixture arrival test initially verified only the Safari app, not the page.
  Adding the synthetic field's existence predicate verified actual page arrival.

## TypeSafe evidence and privacy

The existing host credential was loaded in memory from its environment-assignment
file; it was never printed, copied locally or included in task JSON. The initial
probe incorrectly treated the whole assignment as a token and got 401. Correct
loading succeeded; no credential rotation or provider change was needed.

The live API accepted the documented Choice schema with `jev-1.13.0`. A separate
synthetic decision-only probe selected the intended choice in **0.355 seconds**,
392 input tokens, but confidence was **0.12**. That is below the default 0.6
threshold; the real task above passed the default threshold. Confidence policy
has **not** been calibrated on a representative task set.

Only synthetic goals, caller-approved action descriptions, known app identity,
opaque snapshot/action IDs and availability metadata were sent. No field values,
raw accessibility text, screenshots or physical device identity were uploaded.
Usage for failed/unrecorded probes is not claimed to be zero or included in a
fabricated total bill.

## Remaining validation before unattended enablement

1. Run native search/results and bounded list scrolling in at least two real
   apps using approved labels and non-sensitive queries. Validate Back and
   deep-link destination identity, including overlays and duplicate labels.
   Safari fixture success does not replace this native-app coverage.
2. Run equivalent repeated tasks from the same starting states across legacy,
   optimized deterministic, ordinary planner and Jev modes. Save all outcomes
   (including escalations/timeouts), measure cold setup separately, and use
   `task summarize` for median/p95, calls, model latency/usage. Current samples
   are too small and not equivalent enough for a comparative speed claim.
3. Calibrate confidence on development cases, then test held-out unknown,
   ambiguous, malicious-label and no-progress cases. Require independent
   wrong-target/unintended-action/false-success review; missing annotations are
   unknown, not zero. There were no observed forbidden actions in these limited
   supervised trials, which does not establish their general absence.
4. Exercise physical disconnect/reconnect, lock changes and WDA restart during
   reads separately from in-flight writes, with an operator present. Offline
   fault injection is not physical recovery proof. Do not induce passcode,
   signing, destructive or financial operations.
5. Recheck current TypeSafe model/API/account terms and the deployed WDA version
   before rollout. Confirm package CI for Python 3.11/3.14 and inspect any review
   findings on the stacked PRs. No merge, release or default Jev enablement is
   implied by this record.

WDA's validation/input sequence is not atomic. Cancellation/deadlines stop new
dispatch and reject late decisions but cannot retract input already in flight;
socket timeouts are not a hard real-time guarantee. This remains experimental,
explicitly bounded control infrastructure with planner escalation.
