# Screen observation: physical validation

2026-10-04, `billsmacmini`, its connected physical iPhone, Python 3.14.6.

## Change and proof

Local checkpoint: `4cc5dcc` (direct session simplification). The subsequent
screen-projection implementation is unreleased. This repository is still a
thin controller: no model, planner, daemon, completion gate or write replay.

Observations now project content and controls into a compact hierarchy with
short IDs and meaningful parent links. Full native paths and ancestor identity
stay internal. Unnamed controls and opaque leaves remain; empty layout
containers and repeated container text are collapsed. Native Boolean
checked/selected state is available without exposing text-field values.

Default `observe` includes the screenshot when image disclosure is enabled;
otherwise it is AX only. Large screens automatically stop at a byte-aware page
boundary. Additional pages retain the same captured screen and IDs, without a
fresh WDA read. See [the protocol](session.md).

### Offline evidence

- The two new CLI/WDA-loopback regressions failed on the checkpoint code:
  oversized responses had dropped their observations.
- The strengthened output-boundary regression also failed on the checkpoint:
  its successful screenshot was dropped with the oversized AX.
- The repaired focused suite passed (17 tests); the final full preflight passed
  **134 tests** and the real **47-file packed installation**.
- The regression surface protects useful controls/grouping, private-input
  suppression, known Boolean state, output size, complete paging, ID continuity
  and independent image preservation—not source layout or exact XML paths.

### Physical evidence

A unique owner-only copy of the current source was staged on Bill's host. Local
and remote source archive SHA-256 matched:
`09adc7eeb6fc6b46e5849d808a0c445f41d19047eede55241b2d337d93d182de`.
The new module was imported from that copy, not the installed CLI.

One persistent session handled **21 requests**: 11 acknowledged actions, eight
standalone observations, one deliberate ambiguity rejection, and a normal
close. A disposable LAN-served Safari fixture supplied independent synthetic
state and screenshots; no account operations were performed.

| Check | Observed result |
|---|---|
| Previously overflowing screen | Same 91 native nodes / 68 visible: full default AX + screenshot now **4,125 bytes**, with 24 projected screen entries and no pagination needed |
| Named element ID and replacement typing | Page counter changed to 1; fixture received exactly `screen-test` |
| Duplicate “Disconnect” buttons | Broad selector returned both with Personal/Work parent groups; Work's returned ID changed **Work only**, Personal stayed 0 |
| Paging | Three pages retained identical snapshot ID and capture time; an ID from the first page still delivered `paged-id` after later pages |
| Unnamed button | Present in the projection; its returned ID increased its independent counter to 1 |
| Checkbox state | AX reported `checked: false`, then `checked: true` after a semantic tap; fixture state independently agreed |
| Images independent of paging | Default fresh observations included images; paging default returned the retained AX only |
| Lifecycle | `closed`, `session_end`, cleanup `completed`, process exit 0, control lock released |

The grouped fixture's final state was:

```json
{"text":"paged-id","taps":0,"bottom":0,"personal":0,"work":1,"checked":true,"blank":1}
```

## Preservation and limits

Bill's global **v0.5.2** package was not replaced. All **67** hashed installed,
configuration and service files were unchanged; WDA xcodebuild remained PID
854. No service restart, unlock, publication, message, purchase or consent
change occurred. The test server was stopped and its port verified closed.

Settings was restored and confirmed by a separate read-only observation after
the app transition. The immediate observation attached to the launch still
showed Safari: dispatch acknowledgment is not proof that a transition has
finished. The harness does not introduce an automatic settling/verification
loop. Screenshots and AX are captured independently, not atomically.

The synthetic tabs and private test artifacts remain. The local worktree's
existing implementation was preserved and `git diff --check` passed.

This establishes the exercised physical behavior, not general app reliability,
a model-quality improvement, cloud-model latency or a whole-task speedup.
Automatic byte-based paging was exercised at the real CLI/HTTP boundary using
large Unicode labels; physical paging used an explicit small page size. Native
selected-state projection has offline coverage; the live state exercise was a
checkbox, not every control type. Opaque leaf entries are deliberately retained
even when they may be decoration; silently hiding an unnamed control is worse
than a small amount of residual noise.

Private evidence (not packaged): `/tmp/iphone-screen-bill.hjBtRp/` locally,
`/tmp/iphone-screen-bill.pGecjg/` on Bill's host. The first directory contains
the transcript, manifests, complete remote evidence archive, screenshots and
preflight/baseline-regression logs. Test-audit and the iPhone/OpenClaw skills
guided the owner-boundary regression and isolated physical validation.
