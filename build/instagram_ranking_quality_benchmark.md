# Instagram Ranking Quality Benchmark

Goal: validate whether Instagram creator triage ranking is measurably useful,
not just fast. Across at least five varied pregnancy/motherhood themes, the
verified top 10 should produce at least five credible leads in at least 80% of
runs, with evidence, caveats, JSON/Markdown artifacts, and zero irreversible
Instagram actions.

## Benchmark Command

```sh
PYTHONPATH=src python3 -m openclaw_iphone instagram benchmark-ranking-quality \
  --output-dir /Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops \
  --prefix ranking-quality-final-v1
```

## Artifacts

- Aggregate JSON: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/ranking-quality-final-v1.json`
- Aggregate Markdown: `/Users/pearlperelel/.openclaw/tmp/openclaw-iphone-ops/ranking-quality-final-v1.md`
- Per-theme triage JSON/Markdown:
  - `ranking-quality-final-v1-1-pregnancy-journey.json`
  - `ranking-quality-final-v1-2-first-trimester-pregnancy-nausea.json`
  - `ranking-quality-final-v1-3-pregnancy-after-loss.json`
  - `ranking-quality-final-v1-4-postpartum-mom-life.json`
  - `ranking-quality-final-v1-5-ttc-fertility-journey.json`

## Results

| Theme | Ranked | Source sec | Verify sec | Total sec | Unresolved | Duplicate rate | Top credible / verified | Top precision | Comparison credible / verified | Comparison precision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pregnancy journey | 21 | 16.81 | 39.23 | 75.27 | 11 | 0.0 | 10 / 10 | 1.0 | 5 / 5 | 1.0 |
| first trimester pregnancy nausea | 24 | 29.67 | 39.74 | 90.0 | 14 | 0.0 | 10 / 10 | 1.0 | 5 / 5 | 1.0 |
| pregnancy after loss | 18 | 23.4 | 38.76 | 82.33 | 8 | 0.0 | 10 / 10 | 1.0 | 5 / 5 | 1.0 |
| postpartum mom life | 16 | 19.67 | 36.57 | 75.61 | 6 | 0.0 | 10 / 10 | 1.0 | 5 / 5 | 1.0 |
| ttc fertility journey | 29 | 27.85 | 38.39 | 86.41 | 19 | 0.0 | 10 / 10 | 1.0 | 5 / 5 | 1.0 |

Aggregate:

- Runs: 5
- Runs with at least 5 credible top-10 leads: 5
- Pass rate: 1.0
- Target passed: true
- Top precision: 1.0 (50 credible / 50 verified)
- Lower-ranked comparison precision: 1.0 (25 credible / 25 verified)
- Ranking lift over comparison: 0.0
- Average duplicate rate: 0.0
- Total elapsed seconds: 409.64
- Irreversible Instagram actions: 0
- Failure modes: `source_candidate_shortfall` in 5 runs,
  `top_precision_not_above_comparison` in 5 runs.

## Baseline Versus Final

Baseline before this validation was the previous triage-shortlist workflow run:
`triage-shortlist-v6` found 30 unique source candidates in 23.8 seconds,
verified the top 10 in 41.03 seconds, and produced 5 shortlisted verified leads
across the original three Perelel scenarios.

Final validation broadens that evidence to five themes and adds a lower-ranked
comparison sample. The current ranking meets the yield target on all five runs:
50 of 50 top-ranked verified candidates were credible. The lower-ranked sample
was also strong at 25 of 25 credible, so the benchmark proves high yield but
does not prove top-ranked precision is better than lower-ranked precision.

Because the target already passed and the lower-ranked comparison was equally
precise, this benchmark does not support changing ranking weights yet. Adding
screenshot/text quality signals now would risk overfitting to one benchmark
surface without a demonstrated yield problem or a discriminating precision gap.

## Failure Modes

- All five themes produced fewer than the requested 30 ranked candidates within
  the source-stage bounds, so source breadth is the clearest remaining failure
  mode.
- Top-ranked precision did not exceed lower-ranked comparison precision in any
  run because both groups were fully credible in the final benchmark.
- Most unresolved counts are source-only candidates left unverified by design;
  unknown profile fields remain unknown unless the candidate was profile
  verified.
- The benchmark found broad topical relevance reliably, but many credible leads
  are above 10k followers. If strict micro-creator sourcing is required, the
  next benchmark should measure under-10k yield separately.

## Recommendation

Do not add new ranking weights from this evidence. The next best experiment is
broader source collection for themes that produce fewer than 30 ranked
candidates, followed by the same top-vs-comparison benchmark. Optional
second-pass verification is useful for shortlist breadth, but better ranking
signals should wait until a benchmark shows top precision or top yield falling
below the lower-ranked/random comparison.
