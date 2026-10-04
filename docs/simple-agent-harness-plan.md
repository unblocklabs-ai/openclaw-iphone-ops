# Simple iPhone agent harness

Status: approved; local implementation, unreleased. Replaces the task/planner API.

## Principle

The harness is a controller, not another agent. The caller owns planning,
recovery decisions, and task completion. One persistent session uses the existing
CoreDevice and WebDriverAgent transports; no new daemon, model, or dependencies.

## Keep and abstract

- One physical device, exclusive control lock, reused WDA session.
- Per-operation timeout/cancellation, bounded JSON-lines requests and responses.
- Device identity checks, honest dispatch/partial/unknown outcomes, private files.
- Existing configuration, packaging, diagnostics, `wda run`, `watchdog once`,
  and launchd wrappers. No service or fleet migration.
- One observation parser and one session implementing `observe`, `tap`, `swipe`,
  `type`, `press`, `launch`, `open_url`, and `close`.

## Remove

- Grants, offers, predicates, automatic verification/readback, global stops.
- Fixed/adaptive/explore modes, task schemas, goals, quotas, no-progress stops,
  reconciliation permissions, image authorization tokens and snapshot-age gates.
- The second model, confidence thresholds, cloud allowlists, task benchmarking.
- Bundled Instagram discovery, ranking, research and video orchestration.
  Keep only app knowledge expressed through ordinary controls.
- Parallel UI controllers and obsolete commands/docs/tests. No permanent legacy
  engine or compatibility framework.

## Behavior

- Agent deliberation does not spend an operation timeout.
- Failed safe reads may reconnect repeatedly to the same pinned physical UDID.
- Never replay a write automatically. An unknown write reports uncertainty;
  inspection and subsequent deliberate requests remain possible.
- Input failure cannot block unrelated actions; app transitions need no new session.
- Screenshots and coordinate swipes do not require accessibility.
- Accessibility includes useful labels, bounds and hierarchy by default; secure
  values and supplied secrets never appear in responses/logs.
- Semantic selection resolves current identity/context. Missing or ambiguous
  targets return candidates without killing the session.
- Images require explicit disclosure opt-in. Optional caller-specified masks
  hide only those regions, not arbitrary private content.
- Coordinates use device points by default; image pixels explicitly request
  image space and use the last image's geometry, without expiring/consuming it.
- Optional observation after an action never erases acknowledged dispatch.
- A malformed framed request fails only that request. Normal close/EOF exits 0,
  meaning lifecycle completion, not achievement of an agent's task.

## Cutover and proof

1. Save existing edits recoverably; introduce thin ownership/session/protocol.
2. Migrate CLI, packaged skills and package checks; remove retired engines.
3. Retire app orchestration and preserve valuable transport, identity, privacy,
   uncertainty and lifecycle proof at their new owners.
4. Run offline/loopback tests and repository preflight. A physical matched
   comparison remains a gate before claiming device reliability or performance.

No release, installation, deployment, commit or physical-phone operation is
authorized by this implementation request.
