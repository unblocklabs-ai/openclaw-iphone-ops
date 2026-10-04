# Fast decision models for the iPhone harness

Independent research recommendation — **2026-10-04**.

## Recommendation in one minute

**Yes: a fast decision model can plausibly make this project substantially better. But it should replace repeated expensive agent decisions, not become another prerequisite for controlling the phone.**

Keep the direct session as a model-free controller. Establish the compact, useful screen observation baseline. Then evaluate a **small, optional, caller-owned fast navigation loop** that uses the same session: the main agent supplies a narrow intent and any text; Jev or Clef chooses among current screen controls; the main agent resumes when interpretation is needed and independently checks the outcome.

I would fund that experiment. I would **not** restore the old planner, grants, confidence blockers, global stop state, or a mandatory model check before every tap. Nor would I buy into a new framework, fine-tune a model, or claim Clef's advertised milliseconds on Bill's Mac without measuring its actual endpoint.

There are real browser implementations of this split, including one from Browser Use. Clef's vision is real and interesting. Neither proves general iPhone control, and there is fresh contrary evidence about Clef's hosted latency. The best first experiment is **text-based Jev vs Clef-flash on the new AX projection**, with a separate **Clef + screenshot candidate-selection arm**. Keep the main-agent-only path as the baseline and fallback.

## What I actually reviewed

- This repository at checkpoint **4cc5dcc**, its prior Jev/adaptive implementation in `4cc5dcc^`, `session.py`, `observations.py`, `wda.py`, protocol documentation, and the approved removal plan. The parent is changing the projection concurrently; this is not a review of its final patch.
- Bill's prior real-phone report: `/tmp/iphone-live-bill.pDvHNV/report.md`. That proves direct control on an actual iPhone and AX capture around **0.92–1.40 seconds**, but not a model-driven speedup.
- Actual local Jev integrations in **unblock-memory** and **QMD**, plus their relevant evaluation results. **Layla's Jev mission is a plan/pilot document in the inspected checkout, not proof that all those features shipped.** Scrat's `research/jevify-2026-09-22` is similarly research, not a production integration.
- Current TypeSafe model/API/limitations/confidence docs, Cloudflare's October 1 Clef announcement and hosted schemas/model cards, public Jev browser projects, native Cua-S1 examples, and recent GUI-agent research.

This was read-only research. I made no model calls, used no credentials, incurred no inference charges, installed nothing, and performed no phone operations. Public source copies are retained under `/tmp/iphone-model-research.f0bNVm/`. The Jev skill and Cloudflare skill guided API retrieval and source interpretation; current public documentation took precedence over the September skill snapshot.

**Concurrent implementation update from the parent:** it reports live verification passed for the compact default screen (4,125 bytes, or 4.1 KB decimal, on the previously overflowing synthetic screen), Work-row-only disambiguated clicking, stable paged IDs, an unnamed-button ID click, and boolean checkbox state. These are parent-reported results, not a second physical run by this researcher. They establish the intended baseline for a future decision-model comparison; no model integration occurred this turn.

## 1. Start with the work, not the model

The parent's final [physical validation](physical-validation-2026-10-04.md)
records the implemented baseline and its limits. This recommendation does not
attribute that deterministic observation improvement to a decision model.

An agent controlling a phone does four different jobs:

1. **Understand the user's task and its boundaries.** Often requires conversation, app knowledge, judgment, and possibly generation.
2. **Find the current control that advances a known next intent.** Often a quick, bounded judgment.
3. **Deliver input accurately.** Mostly deterministic transport, geometry, identity, ownership, and uncertain-write accounting.
4. **Decide whether the task actually succeeded.** Sometimes a simple visible condition; sometimes a complex, independent check.

Jev/Clef are compelling for **#2**, sometimes for a narrowly defined part of #4. They do not automatically replace #1, and putting them in #3 would add latency and probabilistic failure to something code already handles.

The key unit is not a tool call or a token: it is **a verified useful action per unit of wall time and human/agent attention**. If the main agent still reads the same screen and deliberates after every fast-model answer, we have bought another round trip, not a fast lane.

Conversely, a main agent need not personally reason through every “open menu → choose search → focus search field” interaction. A short delegated loop can amortize one planning turn over several mundane decisions. This is the positive case that removing the old second-model engine should not cause us to overlook.

## 2. What Jev and Clef can actually do today

| | Jev 1.13.0 | Clef | Clef-flash |
|---|---|---|---|
| Input | Text / JSON | Text / JSON + images | Text / JSON + images |
| Output | Choice, Score, Noul distributions | Same decision types | Same decision types |
| Generate arbitrary typing text or pixel coordinates | No | No | No |
| Published input price | $0.042/M tokens | $0.24/M tokens | $0.09/M tokens |
| Hosting | TypeSafe / provider wrappers | Workers AI; Apache-2.0 open weights | Workers AI; Apache-2.0 open weights |
| Primary advantage here | Cheap selection over AX candidates; existing integrations | Vision-assisted decisions; larger model | Potential low-latency vision-assisted decisions |

Prices and supported modalities are from current primary docs, not our own measurements [1–4].

### Bounded answers are useful, not magic

You provide a state and questions. A Choice selects one supplied option; a Noul estimates yes/no; a Score rates defined levels. The API cannot invent a missing element, a novel action, a typed query, or a coordinate that you did not offer. That eliminates parsing and invalid-option invention, **not wrong decisions**.

Jev currently accepts text only. It has a 64k total request budget, but `state` plus the longest question is limited to 32k. Its current docs list dynamically adjusting limits of 100k tokens/s and 80 requests/s. The older local skill's “about eight concurrent requests” is a historical operational observation, not the current published contract [1].

Clef uses a 27B Qwen-derived vision backbone; Clef-flash uses a 9B one. The release model uses a prefill-only backbone pass and a schema-scoring head rather than autoregressive answer generation [2–4].

**The hosted API accepts up to four embedded PNG/JPEG/WebP images**, 4 MiB and 16 megapixels each, 8 MiB total decoded, and a 13 MiB request body. It does not fetch image URLs. It accepts 1–64 questions; Choice supports 2–255 options and Score 2–10 levels. The model card describes video/frame support, but the inspected hosted request schema has `images`, **not a video-upload field**. For this project, a screenshot as embedded image is the relevant supported path [3].

“Fully Jev-API compatible” is a request/answer-shape claim, not a guarantee of identical calibration, decision quality, independence, latency, or provider envelopes. Cloudflare REST wraps results; some gateways use `boolean` instead of `noul`. Clef's joint cross-field attention also means we should not inherit Jev's question-independence promise without testing it [2, 3, 10].

### Image understanding is not coordinate grounding

Clef can inspect a screen and choose “keyboard visible,” “loading,” or one of the supplied numbered controls. It is not, by that fact alone, a proven click-coordinate predictor. A sensible visual pilot supplies the screenshot plus the current candidate IDs, labels, and bounds. The answer names an original candidate; code resolves it through the existing session.

An important unlabeled icon can remain a candidate. An icon omitted by AX is different: a choice model cannot select an absent candidate. Then use the main vision-capable agent's coordinate path, or separately evaluate a specialist grounding/perception model. A grid of thousands of coordinate choices, or repeated coarse-to-fine queries, is not the simplest default solution.

### Probability is not a certificate

Jev documents literal-reading errors, numeric/counting limitations, indirection, irrelevant-context degradation, adversarial-state susceptibility, and option-order sensitivity [5]. Its current Choice `confidence` is a normalized top probability:

`(p_max - 1/n) / (1 - 1/n)`.

It is not a separately verified probability that the task is safe or complete. A fixed confidence threshold also changes its effective top-probability requirement with candidate count [6]. Our old iPhone driver had a default 0.7 confidence policy; resurrecting that as a universal gate would be an unjustified regression. Return useful distributions; tune any fast-lane abstention on our actual task/error costs. None of this should block a subsequent deliberate direct action.

## 3. What the existing local Jev work teaches us

### Unblock-memory: a good boundary

`src/typesafe-client.ts` makes one request and leaves response schema and failure policy to the feature. `typesafe.ts` performs skill selection, independent memory-passage usefulness judgments, and quality judgments over explicitly bounded inputs. `evidence-review.ts` calls its result **advisory support against cited excerpts**, not truth or authorization to write. These are useful architectural examples: a narrow decision can improve retrieval without owning everything downstream.

The local Skill Whisperer experiment found:

- vector-only selection: **24/40** correct;
- vector top-three + Jev + no-match: **40/40**, warm p50 **203 ms**;
- Jev over the entire 16-skill test roster: **40/40**, p50 **183 ms**.

This was 40 synthetic labeled cases, one completed pass, not a general production claim. It also observed HTTP 529 and a **3.56 s** retry-inclusive tail. An especially relevant lesson: Jev fixed semantic distinctions (“translate ‘create a ticket’” is not “create a ticket”) that a rigid relevance threshold missed. It worked best **instead of** a restrictive gate, not after it [L1].

### QMD: judgment over code-found candidates

`src/typesafe-query.ts` defines a four-level evidence-usefulness rubric and versioned query policy. Source retrieval and candidate text remain deterministic. This is close to phone control: find actual controls in code, then ask a semantic question over them. It does not demonstrate that Jev should plan complete tasks or manage the connection [L2].

### Layla and Scrat: do not count plans as successes

Layla's mission proposes part facets, role ranking, identity matching, docs routing, and evidence checks. It stresses explicit inferred labels, independent evidence, current candidates, and richer state. Those are useful proposals, but the inspected Jev mission is not proof of live deployment or performance for all six features. Scrat's research explicitly reports **no paid inference** [L3, L4].

### Prior iPhone code: the problem was the ownership boundary

The removed `jev.py` was itself a fairly narrow REST choice adapter. Its surrounding adaptive/task/planner system introduced scopes, grants, approved-label projections, separate evidence/permission state, quotas, confidence escalation, and completion/verification machinery. The adapter's existence did not logically require that machinery.

So I do **not** conclude “we tried Jev; Jev is bad here.” I conclude: **we intertwined decision assistance with authority and lifecycle. Keep those separate on the next attempt.** The direct session's unknown-write accounting and no-auto-replay behavior remain valuable whether the caller is a frontier model, Jev, a script, or Bek.

## 4. Real Jev browser work — what is transferable

### Browser Use: `jev-ultrafast`

This is the strongest direct precedent [7]. It presents a dynamic indexed control table; Jev picks operation and compatible target in one request using speculative heads; a small LLM generates free text only for `TYPE_TEXT`. It reuses a browser and keeps DOM observation/execution outside the model.

The published Google Flights recording takes **7.073 s**, with **17 Jev requests**, ten interactions plus WAIT, and two actual text-generation calls. Median Jev request latency was **178 ms**. A matched runtime optimization had three alternating pairs: **9.450 s → 7.092 s** median, both **3/3** verified. Browser protocol calls fell **1,092 → 101**.

Important qualification: that comparison is **two implementations using Jev**, not Jev vs a frontier agent. Timing starts after the initial observation and ends at DONE; setup, initial navigation, and independent post-run verification are outside that recorded clock. It is one task on one profile, not a broad speed/reliability benchmark. Its supported DOM/ARIA subset excludes frames, shadow DOM, canvas, uploads, popups, nested scrolling, and arbitrary keyboard widgets.

**Transfer:** one persistent session, visible useful controls, compatible options, one inference per decision, short fresh context, generated text only when necessary, fewer transport calls. **Do not copy:** its broad agent loop, screenshot recording defaults, implementation-specific DOM freshness policy, or all its prompt heuristics into our core controller.

### `wy-coliney/jev-browser-use`

“Jev clicks; Codex thinks and verifies” is exactly the delegation split [8]. It operates within an existing computer-use connection, avoids a host turn for each click, and hands visual interpretation and typing back to Codex. Its “~5–10×” is an approximate claim about the authors' EZCollegeApp browser operations; server processing is excluded and the chart is illustrative. Useful workflow evidence, not a portable numeric promise.

### `Ying-Kai-Liao/jev-browser`

This project reports **40/42** correct tasks, **202** Jev calls averaging **286 ms**, with independent ground-truth checks [9]. It includes forms, navigation, common widgets, large pages, frames, and shadow DOM. Known failures include counting repeated actions, open-ended scrolling, ordered subgoals bundled together, and sorting verification. Older completion loops got stuck when “done” oscillated.

A two-sided cost ledger is unusually honest: small pages can spend more Jev tokens than caller tokens saved. Token price differences may still favor it, but pruning a tiny page can cost latency for no real benefit. Its reported browser capabilities rely heavily on Playwright and custom DOM extraction. A number of its pause/settle/global-budget policies are precisely what we should avoid copying wholesale.

### `jasonduncan/jev-browser`

An experimental existing-tools bridge with a continuous runner [11]. Historical two-trial workflow aggregates report Hacker News **12.44 s → 5.43 s**, and Wikipedia **19.64 s → 14.56 s**, both complete. The authors explicitly separate fixed planning from timing and say the package's current runtime is not what those old results measured. Good proof of feasibility and measurement discipline, not broad proof.

### ThinkFlowLab `system1-agents`

Multiple runnable environments use the same decision-model slot [12]. Its Allrecipes example reports **35.7 s vs 138.7 s** and lower cost; text generation and final answers still use a chat model. These are small task-specific comparisons, and some older comparison rows lack recorded baseline model identities.

Its detailed Google Flights profiling is more relevant than the headline ratios: identical decision policies varied greatly with browser-driver settle behavior, page/network response, and machine load. One stock MCP path paid about a second of extra settle work on each probe/click; dropping that overhead mattered enormously. **A fast decision model cannot rescue a slow or over-checking transport path.**

### Native Cua Driver / Cua-S1

This is the closest nonbrowser analogy [13]. The driver stays independent; example-level adapters choose immutable native controls from a current observation. Cua's published native fixture runs report **1,680/1,680** correct decisions across roughly 4/12/24 candidates. Those are four controlled harnesses and declared task steps, not general apps or iOS.

Cua-S1-4B has separate text and multimodal adapters and a local inference path. Its native examples cap candidate sets to 24 plus handoff options. The adapter docs note >9.3 GB memory for the 4B base and **10–35 s loading**, recommending a persistently loaded model. This is not a drop-in, zero-dependency improvement to Bill's host; hardware and true warm latency need proof.

Cua-S1 Nano's own model card is refreshingly blunt: very strong same-distribution synthetic multimodal results, but weak cross-dataset results, no goal string in per-element text context, no general-decision benchmark result, and no canonical live desktop integration for Nano [14]. “Tiny and milliseconds” is insufficient evidence of useful phone control.

## 5. Related GUI-agent work: the strongest counterarguments

### Specialist grounding is a distinct capability

UI-TARS is trained for GUI perception, action grounding, reasoning, and multi-step trajectories [15]. OmniParser identifies interactable regions and describes icons from screenshots [16]. These are relevant if our important failures are missing AX controls or inaccurate pixel clicks—not if the problem is merely slow selection among well-labeled buttons.

Adding them means more deployment/runtime complexity. Do not introduce OCR, detection, captioning, and then Clef as a default four-stage pipeline unless the actual failure data demands it. Existing images + main agent coordinates are a simpler fallback.

### A flat strong agent is still a serious baseline

Agent S3 deliberately removes manager/worker hierarchical planning in favor of a **flat policy that can replan at any time**, arguing that separate high-level planning can become stale and counterproductive [17]. That supports the simplification already underway. It does not rule out narrow fast decisions; it warns against building another elaborate hierarchy just because an extra model is available.

Its best-of-N rollout research depends on duplicable/resettable environments. We cannot run several conflicting rollouts on the same physical iPhone and pick the winner afterward. WDA receipts likewise are not task completion.

### Clef is only three days old

Cloudflare's domain-classification example renders and classifies a website in **2.2 s**, versus **4.7 s** for its comparison LLM [2]. That is classification, not navigating a website. The launch suite covers tool selection, routing, classification, NLI, and other decisions—not ScreenSpot/WDA/iPhone end-to-end control. Vision support is verified; generalized phone control is not.

Cloudflare reports median latencies **209.3 ms** (Clef) and **38.8 ms** (flash), p95 **238.6/122.4 ms**, across its benchmark runs. A fresh public third-party October 3 comparison with raw evidence instead measured **18.7/18.6 seconds p50** for Clef/flash, versus **0.3 seconds** for Jev, in one environment [10]. Four-way parallelism did not remove it. The authors attribute that to launch-time queueing/scale-from-zero; **their measurements establish slow requests, not that causal diagnosis**. Their code records individual HTTP attempt time. This is a material reason not to promise Clef in the hot path yet.

## 6. The latency economics for this phone

Let `O` be observation time, `A` input delivery time, `H` main-agent decision/turn time, and `D` fast-model decision time. Ignoring identical setup and final verification:

`main-agent cycle ≈ O + H + A`

`fast delegated cycle ≈ O + D + A + handoff cost amortized across the run`

The opportunity is mostly **H − D**. Replacing a 200 ms model with a 39 ms model saves 161 ms per cycle; it does not eliminate our roughly one-second AX observation or app animations. Browser demos can read DOM state much faster and more atomically than WDA's app/source calls.

Illustration only: if O=1.2 s, A=0.3 s, H=3 s, D=0.2 s, the decision-loop floor improves from 4.5 s to 1.7 s per action, about 2.6×. H and A here are assumptions, not measured Bill data. If H is already 0.3 s, the improvement is marginal. If D is 18 s, it is a disaster.

**A suggestion helper that returns to the main agent each step does not realize that gain.** It can improve difficult semantic selection, but should be sold as accuracy/ergonomics assistance, not speed. To measure speed, evaluate a short continuous delegation that avoids those host turns.

Use optional action+observation responses and reuse the existing SSH/process/session connection. Do not add multiple captures, model reviewers, or state predicates before every input. Do not cache a target decision across a changed screen to chase milliseconds.

## 7. The smallest useful integration

### Keep the controller boring

The shipped session remains `observe/tap/swipe/type/press/launch/open_url/close`, with the same persistent lock, pinning, action receipts, image opt-in, and no write replay. No model credentials, confidence gates, planner, or cloud dependency belong in its transport or observation projection.

Start the pilot as **one experiment script or caller-side skill**, not a provider framework or a new controller. It opens/uses the same protocol-2 session and composes ordinary operations. Only promote it after live benefits are measured.

### Two capabilities, evaluated separately

**A. `choose_control(intent, observation)`**

Returns a current candidate ID or `handoff`, with probabilities. Useful when the phrase “open account options” must map to an unlabeled button or a row-scoped control. It dispatches nothing. If only one exact valid control already matches, use code; do not pay a model to rediscover it.

**B. A short fast-navigation delegation**

The main agent supplies one bounded intent, allowed operation kinds, and any exact text or local text reference. A loop observes → chooses a compatible action/control → dispatches once → observes. It returns control on completion *candidate*, ambiguity, unexpected state, unsupported visuals, cancellation, or uncertainty. It may navigate through several routine controls without a main-agent turn for each one.

Those bounds apply to **that optional delegation**, not to the underlying session or every possible agent task. The main agent can immediately act directly, revise the intent, or resume the delegation. A low model score must never put the phone/session into a blocked state.

### One meaningful question, not a safety bureaucracy

Offer original candidate IDs mapped locally to complete operations, or use operation-specific target heads so incompatible pairs cannot execute. Include `handoff`/`none`; otherwise softmax always has a winner even when no control fits. Ask the one narrow semantic question needed. Do not prepend universal “is it safe / is it correct / is it complete / is it authorized” model batteries.

If multiple independent questions genuinely share the same state, batch them. If none applies, avoid the call. Free text comes from the main agent or approved input, not a second generator by default. Supplied secrets stay in owner-only local files; the model gets their opaque purpose, not their values.

Fast-lane abstention is useful if it hands back to the capable agent. Always requiring a human for a fuzzy score is not. Any threshold should be specific to the pilot's actual candidate-selection errors, not transplanted from vendor examples or the old driver.

### What the new observation needs

- Current controls, short IDs, names, relevant grouping, bounds, enabled/focused state, and useful readable content.
- Unlabeled actionable icons/fields retained; structural wrappers removed without losing identity context.
- Complete candidate coverage or explicit pagination/truncation. A model cannot select a useful control cut out before it sees it.
- **Safe control-state facts**, when WDA reliably exposes them: parsed boolean switch/checkbox state and explicit selected state, without arbitrary field values. The checkpoint baseline drops all values; that also drops toggle-state facts. Both a main agent and a fast controller need to avoid toggling the same setting twice. The parent reports adding boolean checked/selected state in the concurrent projection change; I have not independently validated that final patch. Do not broadly export raw text values to solve this.
- Screenshot independently usable when AX is poor. AX and image observations are sequential captures, not proof of an atomic scene.

A model-free, screen-oriented projection improves every caller. Do not use Jev/Clef to “clean up” the AX tree on every observation: deterministic projection is faster, private, stable, and does not silently misclassify useful controls away.

## 8. Privacy and reliability boundaries

Existing screenshot permission is **not automatically consent to send the screenshot to a new cloud provider**. Phone labels can contain private messages; screenshots can contain account strips, notifications, typed secrets, and keyboards. Value omission does not make an AX tree anonymous.

Jev says no customer-data training and offers enterprise ZDR; that is not a universal default retention guarantee [1]. Cloudflare's Clef announcement says it does not read/store/train on requests or responses absent fine-tuning; Workers AI data docs explain no training/improvement without consent and note storage when used with other services [2, 18]. Those April platform docs still say Cloudflare does not train models, now stale relative to Clef; check service-specific/account terms rather than claiming every paragraph is current. AI Gateway logging, our own traces, R2, or fine-tuning capture are separate choices.

Use synthetic screens for the first model experiment. Keep raw labels/images in private artifacts and credentials host-side. Do not send entire task history or unrelated app content. If privacy blocks cloud use, return to the main-agent/direct path. Local open weights are a future option, not an excuse to silently install several gigabytes or launch an inference daemon on Bill.

The fast loop uses the existing action outcome contract. On `unknown`/partial write, return the receipt and screen to the main agent; **do not reissue input automatically**. A model saying “not done” is not permission to replay an uncertain write. Target freshness belongs to actual identity resolution/scene intent, not a new global age-token regime.

## 9. How to decide whether to ship it

### Stage 0 — finish the baseline UX

Run the new compact projection on Bill and independently prove controls/text/grouping are useful, image evidence survives AX truncation/failure, and no default tiny page explodes. Record baseline observation/action/host-turn timing. Otherwise the fast-model experiment will conflate UX fixes with model benefits.

At research handoff, the parent reports the compact-screen physical checks above have passed. Use its saved final report/checkpoint as the baseline rather than redoing the old overflowing projection or attributing this deterministic UX improvement to a model.

### Stage 1 — inexpensive offline selection pilot

With explicit approval for paid API tests, use about 60–100 synthetic or consented labeled screen-intent pairs, including repeated labels in different rows, unlabeled controls, disabled controls, missing controls, keyboard-covered elements, loading, unfamiliar screens, and legitimate handoff cases. Compare Jev, Clef-flash text, Clef-flash image+text, and Clef image+text. Include option-order permutations and fixed state-format checks. Measure accuracy **and the proportion of cases handed back**, not just accuracy on accepted cases.

This screens models; it does not establish end-to-end speed. It also checks actual request RTT from Bill, including cold/warm/tail behavior. A cheaper/faster reported inference number is irrelevant if the network endpoint is slow.

### Stage 2 — matched physical tasks

Use the same session/phone/fixture/reset and the same narrow tasks in randomized or alternating order:

1. main agent on the compact observation, one deliberate action at a time;
2. main agent gives one short intent; Jev fast-navigation loop;
3. same loop and intent; fastest viable Clef variant;
4. image-assisted arm where AX genuinely needs visual context.

Choose 6–10 small reversible flows with several steps, plus missing-control, duplicate-control, unexpected-screen, and uncertainty cases. No messages, purchases, account/security changes, or social effects. Include a synthetic Safari flow and a native-navigation flow; a perfect Safari fixture is not general iOS evidence.

Time **before the first observation through independently verified outcome**, with setup/reset reported separately. Measure O/A/H/D, total task p50 and tail, actual provider RTT, main-agent turns, tool calls, wrong actions, fallback/handoff frequency, completion, and all failures. Do not use dispatch acknowledgment or the fast model's DONE as the oracle. Keep the main-agent task parameters identical and do not reveal expected action IDs to the selector.

Set criteria before running. A reasonable pilot bar: clearly lower median task time/agent turns without lower task success or extra wrong actions; endpoint delays/fallback should not worsen tail task experience materially. A small exploratory cohort supports a decision to gather more evidence, not claims of universal safety or reliability parity.

### Ship / do not ship

- **Ship optional fast navigation** if it removes repeated main-agent turns and produces a real measured improvement with useful handoff.
- **Ship only choose-control assistance** if it improves difficult targeting but continuous execution is not reliable. Do not claim a speedup for that.
- **Ship neither** if the compact direct session is already fast enough, privacy excludes useful state, or fallbacks absorb the gain.
- **Prefer Jev** for an initial text pilot if current Bill endpoint measurements resemble the public launch data. **Prefer Clef-flash** only if actual hosted latency and accuracy support it; **prefer Clef** for vision only if its extra quality earns its cost/time. No model winner can be selected from general vendor benchmarks alone.

## Bottom line

**The thin controller and a fast decision model are not opposing ideas.** A thin controller lets different callers work well. A decision model can be one optional caller that handles cheap screen-level decisions, while a strong agent owns task judgment and recovery. This is worth testing—but only in a way that makes the direct path remain complete and effortless.

The biggest avoidable mistake is to rebuild the old restrictions around a promising new model. The second biggest is to dismiss a real fast-navigation opportunity because the last integration put that model in the wrong layer.

## Sources

Primary public sources retrieved 2026-10-04 unless a historical result date is noted. Model/vendor/project benchmark claims are attributed above and were not rerun.

1. [TypeSafe current models](https://docs.typesafe.ai/models), [state](https://docs.typesafe.ai/concepts/state), [API](https://docs.typesafe.ai/api).
2. [Cloudflare Clef launch, 2026-10-01](https://blog.cloudflare.com/clef-decision-models/).
3. [Clef hosted model/API](https://developers.cloudflare.com/workers-ai/models/clef/), [raw input schema](https://developers.cloudflare.com/workers-ai/models/clef/schema-input.json).
4. [Clef-flash hosted model/API](https://developers.cloudflare.com/workers-ai/models/clef-flash/), [Clef open model card](https://huggingface.co/Cloudflare/clef), [flash model card](https://huggingface.co/Cloudflare/clef-flash).
5. [Jev 1.13 jaggedness, reviewed 2026-10-02](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
6. [TypeSafe confidence definition and formula](https://docs.typesafe.ai/confidence).
7. [Browser Use Jev Ultrafast](https://github.com/browser-use/jev-ultrafast), [measurements](https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md), [actual loop](https://github.com/browser-use/jev-ultrafast/blob/main/jev_ultrafast/agent.py), [questions](https://github.com/browser-use/jev-ultrafast/blob/main/jev_ultrafast/questions.py).
8. [wy-coliney Jev Browser Use](https://github.com/wy-coliney/jev-browser-use).
9. [Ying-Kai-Liao Jev Browser](https://github.com/Ying-Kai-Liao/jev-browser), [results and two-sided ledger](https://github.com/Ying-Kai-Liao/jev-browser/blob/main/RESULTS.md), [execution loop](https://github.com/Ying-Kai-Liao/jev-browser/blob/main/src/session.mjs).
10. [rmax-ai launch-window Jev/Clef comparison](https://github.com/rmax-ai/jev-vs-clef), [Oct 3 report](https://github.com/rmax-ai/jev-vs-clef/blob/main/results/run-20261003-main/report.md), [HTTP client and timing](https://github.com/rmax-ai/jev-vs-clef/blob/main/harness/clef_client.py).
11. [jasonduncan Jev Browser](https://github.com/jasonduncan/jev-browser), [historical prototype evaluation](https://github.com/jasonduncan/jev-browser/blob/main/evals/README.md).
12. [ThinkFlowLab system1-agents](https://github.com/ThinkFlowLab/system1-agents), [detailed comparisons, drivers, reruns, and limitations](https://github.com/ThinkFlowLab/system1-agents/blob/main/docs/benchmarks.md).
13. [Cua Driver jev-use browser/native example](https://github.com/trycua/cua/tree/main/libs/cua-driver/examples/jev-use), [model adapter boundary](https://github.com/trycua/cua/blob/main/libs/cua-driver/examples/jev-use/decision-models.md), [Cua-S1 4B adapter model card](https://huggingface.co/cua-ai/cua-s1-4b-0.2).
14. [Cua-S1 Nano model card and limitations](https://huggingface.co/cua-ai/cua-s1-nano-0.1).
15. [UI-TARS project and latest linked models/report](https://github.com/bytedance/UI-TARS), [UI-TARS-2 paper](https://arxiv.org/abs/2509.02544).
16. [Microsoft OmniParser](https://github.com/microsoft/OmniParser).
17. [Agent S project](https://github.com/simular-ai/Agent-S), [Agent S3 paper, flat-policy design](https://arxiv.org/abs/2510.02250). Consult the paper and README revision dates before comparing their different OSWorld scores.
18. [Workers AI customer data usage](https://developers.cloudflare.com/workers-ai/platform/data-usage/).
19. [Additional exploratory Jev/Clef pixel-control experiments](https://github.com/edumntg/jev-clef-experiments). Its Mario success uses several hand-designed perception/action policies and emulator state for cropping/airborne gating; it is not evidence of a universal direct image-to-action policy.

Local evidence inspected:

- L1: `/Users/bek/Desktop/openclaw-plugins/unblock-memory/src/typesafe-client.ts`, `src/typesafe.ts`, `src/evidence-review.ts`, `eval/skill-whisperer/RESULTS.md` (2026-09-17).
- L2: `/Users/bek/Desktop/unblocked_agents/qmd/src/typesafe-query.ts`.
- L3: `/Users/bek/Desktop/lai/layla/dev/orchestration-jev.md`.
- L4: `/Users/bek/Desktop/unblocked/repo-review-bot/research/jevify-2026-09-22/README.md` and `MEASUREMENTS.md`.
- L5: `/tmp/iphone-live-bill.pDvHNV/report.md`; repository `docs/session.md`, `docs/simple-agent-harness-plan.md`; historical `src/openclaw_iphone/jev.py` and `adaptive.py` at `4cc5dcc^`.

Memory was used only to locate related local project context; current file/public-source inspection supports the recommendations. No historical memory claim substitutes for current provider/device state.
