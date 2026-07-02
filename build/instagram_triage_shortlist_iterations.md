# Instagram Triage Shortlist Iterations

Goal: turn broad Instagram discovery into a fast triage-to-shortlist workflow:
source-only triage finds at least 30 candidates in under 30 seconds, then only
the top 10 ranked candidates are profile-verified in under 3 minutes, producing
at least 5 credible leads with evidence, caveats, and artifacts.

## Baseline: Current Source-Only Triage

- Command: `instagram benchmark-discovery --verification-mode source-only
  --max-source-scrolls 0 --prefix triage-baseline-source-only-current`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/triage-baseline-source-only-current.json`
- Result: 29.59 seconds, 30 source-only candidates, 28 unique handles, 30
  pregnancy/motherhood evidence items, 30 source recency signals, 0 irreversible
  actions.
- Interpretation: source-only mode meets the 30-second target, but barely.

## Iteration 1: Add Triage-To-Shortlist Command

- Change: add `instagram triage-shortlist`. It runs source-only triage across
  the three Perelel scenarios, ranks/dedupes candidates, verifies the top 10
  with profile deep-links, and writes a combined JSON/Markdown shortlist report.
- Ranking signals: pregnancy/motherhood source evidence, source recency signal,
  duplicate appearances, cross-scenario appearances, and specific visible
  source-label evidence.
- Command: `instagram triage-shortlist --prefix triage-shortlist-v1`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/triage-shortlist-v1.json`
- Result: 79.68 seconds total, 39.1 seconds source triage, 40.58 seconds
  verification, 30 triage candidates, 29 unique handles, 10 verified, 5
  shortlisted, 0 irreversible actions.
- Failure mode: verification target passed, but source triage missed the hard
  under-30s target. Source stage opened 12 source screens; each tag open waited
  1.5 seconds before capture.
- Next experiment: reduce the tag/source open wait for source screens while
  preserving the 2.0 second profile deep-link wait.

## Iteration 2: Shorter Source Open Wait

- Change: make source-result open wait configurable and default it to 0.7
  seconds for discovery/benchmark/triage source screens. Profile verification
  waits are unchanged.
- Expected benefit: save about 0.8 seconds per source screen, enough to bring
  the combined workflow's source stage back under 30 seconds.
- Next benchmark: run `instagram triage-shortlist --prefix triage-shortlist-v2`.

### Result

- Command: `instagram triage-shortlist --prefix triage-shortlist-v2`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/triage-shortlist-v2.json`
- Result: 39.65 seconds, 0 candidates, 0 verified, 0 shortlisted.
- Tests: `PYTHONPATH=src python3 -m unittest discover -s tests` passed 68 tests with 1 skipped.
- Interpretation: 0.7 seconds is too short for Instagram hashtag deep-links on the physical phone. The source capture happened before result cells populated, so the workflow opened fallback tags and still missed the source target.
- Next experiment: try a less aggressive 1.1 second source open wait using the CLI option before changing the default.

## Iteration 3: Moderate Source Open Wait

- Change: keep the configurable source wait, but benchmark with `--source-open-wait-seconds 1.1`.
- Expected benefit: enough wait for result cells to populate while still saving about 0.4 seconds per source screen versus the original 1.5 second wait.

### Result

- Command: `instagram triage-shortlist --source-open-wait-seconds 1.1
  --prefix triage-shortlist-v3`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/triage-shortlist-v3.json`
- Result: 87.86 seconds total, 50.7 seconds source triage, 37.16 seconds
  verification, 23 triage candidates, 21 unique handles, 10 verified, 5
  shortlisted.
- Interpretation: 1.1 seconds was more reliable than 0.7 seconds but slower
  than the original because several productive tags still captured zero handles,
  forcing fallback source screens.
- Next experiment: keep the original 1.5 second source wait and test whether
  allowing one source scroll gives a faster, more reliable 30-candidate source
  stage than opening more fallback tags.

## Iteration 4: Source Depth Over More Tags

- Change: benchmark source-only discovery with `--max-source-scrolls 1` and
  `--source-open-wait-seconds 1.5`.
- Command: `instagram benchmark-discovery --verification-mode source-only
  --max-source-scrolls 1 --source-open-wait-seconds 1.5
  --prefix triage-source-scroll-v1`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/triage-source-scroll-v1.json`
- Result: 24.56 seconds, 30 candidates, 30 unique handles, 30 evidence items,
  30 source recency signals, 0 irreversible actions.
- Interpretation: this meets the source-only hard target with more headroom than
  the current triage run. In this run it collected enough candidates from the
  first two tuned tags per scenario; the one-scroll allowance remains useful as
  fallback when a tag underproduces.
- Next experiment: make the combined triage-shortlist command default to this
  source strategy and rerun the full workflow.

## Iteration 5: Global Round-Robin Source Pool

- Change: `triage-shortlist` now collects one global source pool and stops when
  it has the target number of unique handles. Source screens are opened in
  round-robin order across the Perelel scenarios so the source stage samples the
  broad scenario set instead of exhausting all tags for an early scenario.
- Command: `instagram triage-shortlist --prefix triage-shortlist-v6`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/triage-shortlist-v6.json`
- Result: 64.83 seconds total, 23.8 seconds source triage, 41.03 seconds
  verification, 30 triage candidates, 30 unique handles, 10 verified, 5
  shortlisted, 0 irreversible actions.
- Source coverage: sampled `pregnancy journey`, `first trimester pregnancy
  nausea`, and `pregnancy after loss`.
- Tests: `PYTHONPATH=src python3 -m unittest discover -s tests` passed 68 tests
  with 1 skipped before this benchmark.
- Interpretation: hard targets are met. Source triage is under 30 seconds,
  top-10 verification is under 3 minutes, and the final report contains 5
  verified leads with evidence, caveats, and artifact paths.
- Recommended next experiment: improve triage ranking with visual/text quality
  from source screenshots or add a second optional verification pass for
  unresolved source-only candidates that have high triage scores.
