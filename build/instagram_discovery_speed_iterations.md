# Instagram Discovery Speed Iterations

Goal: reduce physical-iPhone Instagram creator discovery from the verified
431.86 second benchmark toward 30 seconds while keeping correctness tests green.

## Baseline

- Command: `instagram benchmark-discovery --prefix live-benchmark-v1`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/live-benchmark-v1.json`
- Result: 431.86 seconds total across three scenarios.
- Output quality: 30 candidates, 30 follower counts, 27 likely under 10k, 30
  pregnancy/motherhood evidence items, 25 recency signals, 0 irreversible
  actions.
- Runtime diagnosis: benchmark verified 90 source-pool profiles. The profile
  verifier has a 2.0 second post-deep-link wait, so the sleep floor alone is
  about 180 seconds before WDA source/screenshot capture time.

## Small Baseline

- Command: `instagram discover-creators --query "pregnancy journey"
  --max-candidates 1 --deadline-seconds 90 --max-source-scrolls 0
  --prefix speed-baseline-profile-small`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/speed-baseline-profile-small.json`
- Result: 18.19 seconds, 1 reported candidate, 3 verified profiles, 1 likely
  under 10k, 1 evidence-backed candidate, 1 recency signal.
- Interpretation: even one reported profile-mode candidate is too slow because
  discovery verifies an expanded pool to rank toward lower follower counts.

## Iteration 1: Source-Only Verification Mode

- Change: add explicit `--verification-mode source-only` for discovery and
  benchmark commands. Source-only mode harvests visible media handles from
  bounded hashtag result screens and skips profile deep-links.
- Expected benefit: avoid profile open waits and profile screenshot/source
  captures, making the benchmark limited mostly by three hashtag opens and
  source captures.
- Expected tradeoff: candidates are partial, not qualified. Follower count, bio,
  display name, and deep-link verification are caveated as missing because the
  profile is not opened.
- Next benchmark: run the three-scenario benchmark with
  `--verification-mode source-only` and compare elapsed time plus candidate
  counts.

### Result

- Command: `instagram benchmark-discovery --verification-mode source-only
  --max-source-scrolls 0 --prefix speed-source-only-v1`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/speed-source-only-v1.json`
- Result: 49.78 seconds, 30 candidates, 30 handles, 30 evidence items, 30
  source-media recency signals, 0 irreversible actions. Follower-count targets
  failed because source-only mode does not open profiles.
- Tests: `PYTHONPATH=src python3 -m unittest discover -s tests` passed 66 tests
  with 1 skipped.
- Interpretation: profile verification is no longer the bottleneck. The run
  opened 16 source screens; 8 were ambiguous or had zero parsed handles.
- Next experiment: reorder query-derived hashtag aliases using the productive
  tags observed in this benchmark so each scenario can collect enough source
  handles from about two screens instead of five to eight.

## Iteration 2: Productive Tag Ordering

- Change: reorder built-in scenario aliases toward tags that produced visible
  media handles in Iteration 1.
- Expected benefit: reduce source screens from 16 to roughly 6, keeping
  source-only benchmark runtime under 30 seconds.
- Expected tradeoff: tag ordering is empirical and Instagram result surfaces may
  drift, so ambiguous-screen reporting remains necessary.

### Result

- Command: `instagram benchmark-discovery --verification-mode source-only
  --max-source-scrolls 0 --prefix speed-source-only-v2`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/speed-source-only-v2.json`
- Result: 26.45 seconds, 30 candidates, 30 handles, 30 evidence items, 30
  source-media recency signals, 0 irreversible actions.
- Tests: `PYTHONPATH=src python3 -m unittest discover -s tests` passed 67 tests
  with 1 skipped.
- Interpretation: runtime target was met, but the first-trimester scenario still
  opened one zero-handle source screen and took 13.27 seconds.
- Next experiment: move a broader productive `pregnancy` tag earlier for the
  first-trimester scenario to reduce that scenario to roughly two source
  screens and add runtime headroom.

## Iteration 3: First-Trimester Headroom

- Change: prefer `pregnancy` immediately after `trimesterpregnancy` for the
  first-trimester/nausea benchmark scenario.
- Expected benefit: avoid the zero-handle `firsttrimester` screen and keep the
  source-only benchmark comfortably below 30 seconds.
- Expected tradeoff: the second source screen is broader than the query, so the
  report caveats and source query/tag fields remain important.

### Result

- Command: `instagram benchmark-discovery --verification-mode source-only
  --max-source-scrolls 0 --prefix speed-source-only-v3`
- Artifact: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/speed-source-only-v3.json`
- Result: 21.0 seconds internal elapsed, 21.36 seconds wall time, 30 candidates,
  27 unique handles, 30 evidence items, 30 source-media recency signals, 0
  ambiguous screens, 0 irreversible actions.
- Tests: `PYTHONPATH=src python3 -m unittest discover -s tests` passed 67 tests
  with 1 skipped.
- Interpretation: the 30-second runtime target is met for source-only discovery.
  Profile/follower correctness is intentionally not claimed in this mode:
  follower counts, likely-under-10k counts, bios, display names, and deep-link
  verification require the slower profile mode.
- Next best experiment if profile evidence must also fit 30 seconds: add a
  two-phase workflow that first returns source-only candidates in under 30
  seconds, then verifies a small selected subset of handles asynchronously or in
  a separate command. The previous profile-mode benchmark shows full 30-profile
  verification cannot fit 30 seconds on this WDA path.
