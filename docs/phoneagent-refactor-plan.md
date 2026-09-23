# Action and next evidence: iPhone runtime

**Status (2026-09-22):** Three small Python changes are implemented: reuse the
post-input observation, request compact XML, and select eligible named-ancestor
targets with WDA class chain instead of XPath. These use standard WDA routes
and queries; they do not require a native WDA fork, backend, or controller.
The complete combination and final archived-HEAD control were physically
measured on Bill. This is a bounded synthetic-fixture result, not a release.

## Evidence and limits

The paired synthetic-Safari comparison that motivated this work had five warm
runs per block. Our medians were 10.836 s before and 11.011 s after the
PhoneAgent block; PhoneAgent's was 5.776 s. Our trace included three `/source`
calls (~3.6 s combined) and four `/elements` queries (~3.9 s combined).
Different setup and safety contracts prevent attributing the full gap to those
routes or claiming an end-to-end reliability difference.

Subsequent physical tests on Bill used the same synthetic input → tap fixture,
five runs per block:

| Variant | Median | Status |
| --- | ---: | --- |
| Archived controls, before/after | 10.863 / 10.888 s | Physical controls |
| Stage 1 exact code: reuse post-input tree | 10.587 s | Physically validated |
| Compact XML exact code | 10.138 s | Physically validated, 5/5 runs; range 10.088–10.464 s |
| Class-chain prototype | 8.529 s | Experimental precursor, not checked-in code |
| Combined prototype | 8.176 s | Experimental precursor, not checked-in code |
| Combined exact code | 8.083 s | Physically validated, 5/5 runs; range 7.979–8.315 s |
| Final archived-HEAD control | 11.054 s | Physically validated, 5/5 runs; range 10.727–11.372 s |

Stage 1 and the combined exact-code run each used 23 requests in the matched
fixture versus 25 in the archived control. All five combined runs reached
one correct independent fixture finish without pending input or cleanup failure;
the final control did likewise at 25 requests per run.
The combined median is ~25.6% below the 10.863 s starting control; this is a
small matched-fixture result, not proof of X login, model latency, other apps,
reliability tails, or unattended operation. Compact XML and class chain target
different expensive reads; request count alone does not explain their latency.

On a static Bill screen, five alternating XPath/class-chain pairs resolved the
same unique native element; median query times were 0.985 versus 0.264 s.
Changed name, label, value, geometry, ancestor, and five escaped/injection-like
labels returned no match, without parse errors or mutation. Raw and compact
source parsed to the same signature and secure flag on that screen. This does
not prove dynamic collision or sibling-reorder behavior; the offline duplicate
dispatch test guards no-write on ambiguous results.

## Current implementation and safety boundary

- Ordinary no-`after` editable input uses one fresh full observation to verify
  its unique readable value and supply the next decision. Missing tree values
  fall back to the selected native reference. Explicit-`after`, final,
  secure/custom cases retain their required targeted or destination proof.
  Standalone input can be slower when no next decision needs a tree.
- Full observations request compact XML via WDA's existing `/source` route;
  malformed, oversized, deep, or secure trees retain the existing rejection
  and projection rules.
- For an actionable target beneath the Application root with a named ancestor
  and short observed identity, the locator uses a direct-child class chain of
  every ancestor role and exact available ancestor names/labels, plus target
  identity, state, and geometry. Simple global predicates, long/rootless or
  nonactionable XPath cases, `Selector` queries, and keypad union XPath remain.
  Unlike positional XPath, **anonymous sibling reorder is explicitly allowed**
  when hierarchy, identity, and geometry still produce one global match.
  Duplicate or missing matches stop before writing; foreground checks,
  hittability, freshness, and no-blind-replay remain unchanged.

`npm run preflight` passed 281 tests, release metadata checks, and an isolated
63-file npm package check for the combined source. The measured source snapshot
has SHA-256 `5d78dd16017b00679ac4013a34387d19024b3c09de48c357558a6dd9b7a235fb`.
Python compilation, skill validation, and final device doctor checks passed;
Bill was ready, unlocked, and screen-readable. The synthetic fixture process
was stopped; installed services and package were unchanged. Representative
dynamic hierarchy/collision and native-app checks still precede broader
performance claims. Keep raw device traces outside this portable repository. No deployment or
release is authorized by this report.

## Follow-up: local context and bounded scrolling

The fresh comparison led to focused changes in the existing runtime:
captured XML now rejects explicit app/PID contradictions; the opt-in local
adaptive view includes bounded ancestor context and value-free editable-state
hints; adaptive sessions can scroll one observed container and reuse its
returned tree. The cloud projection is unchanged. See
[planner-session.md](planner-session.md) for the request and evidence contracts.

Physical testing found WDA's old scroll route selected Safari text instead of
moving the page. The existing helper now uses one element-targeted native
swipe, preserving direction semantics and dispatch checks. Named-content
comparison ignores anonymous wrappers and up to four points of bounce/rounding
jitter; no waits or device reads were added. On Bill's synthetic page, duplicate
row labels were resolved from ancestor context, scrolling revealed the final
marker, and the session reported `no_progress` at the bottom. Scroll plus its
next observation took about 2.54 s (six WDA requests); the native gesture itself
was about 0.88 s rather than the old route's 4.80 s failed gesture. These timings
describe the tested fixture, not every scroll view.

Fresh five-run warm workflow blocks measured 8.102 s before these follow-ups,
8.129 s for the initial candidate, and 8.096 s for the final candidate, all
23 WDA requests and 5/5 independently verified completions. The changes did
not demonstrate a meaningful latency change for ordinary input/tap workflows.

A separate PhoneAgent benchmark copy removed only its automatic Return and
added exact post-input value checking in the harness. That variant completed
5/5 equivalent synthetic tasks at median 5.935 s, with zero Return events;
65.418 s cold startup was excluded. This is **not stock PhoneAgent performance**
or identical safety semantics: native current-target validation and uncertain
outcome handling still differ. Four compound PhoneAgent RPCs cannot be compared
directly with four WDA calls. The remaining warm advantage is real on this task,
but does not by itself justify a second production runner or weaker checks.

Final validation passed 294 offline tests and the 63-file isolated npm install
check. No live Jev, X login, general-agent planning latency or reliability-tail
claim follows from these deterministic synthetic tests. At validation time no
commit or release had been performed; the source was tested separately from
Bill's installed package. Subsequent publication is recorded in GitHub Releases.
