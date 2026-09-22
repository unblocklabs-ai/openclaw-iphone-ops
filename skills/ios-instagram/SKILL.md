---
name: ios-instagram
description: Automate Instagram on the plugged-in physical iPhone using the iphone-control CoreDevice and WebDriverAgent stack. Use for launching Instagram, navigating tabs, searching, reading profile or result screens, capturing screenshot/source evidence, creator verification, and bounded Instagram UI workflows on real iPhone hardware.
---

# iOS Instagram

## Overview

Use this skill for Instagram workflows on the real plugged-in iPhone. It depends on the canonical iPhone control repo configured by `OPENCLAW_IPHONE_REPO_DIR`, normally `~/.openclaw/repos/openclaw-iphone`, and the CoreDevice WDA transport described by `$iphone-control`.

Do not use Simulator assumptions, stale screenshots, `iproxy`, or `localhost:8100`.

## Use one control session

Follow [iphone-control](../iphone-control/SKILL.md) and the
[planner-session contract](../../docs/planner-session.md). Start one `task session`
for an interactive workflow; acquisition already checks the pinned device,
readiness and lock state. Do not prepend status/locked/doctor/screenshot/source
chains. Use `doctor --check-ui` only to diagnose a failure.

Launch or deep-link through an authorized task action, then use its returned
observation. Reobserve only when evidence is missing, stale or insufficient for
the next decision. Capture a screenshot for a genuinely visual question, not
after every action. Passcode-required means ask for human unlock; do not switch
phones or repeatedly restart the runner.

## Search Flow

Previously observed labels include `Explore` and `Search with Meta AI`; labels
can change. Select only controls in current evidence, enter the exact supplied
query through verified input, and verify a relevant result tab/query before
choosing a result. Use a bounded scroll action on the observed results container.
Reuse returned observations instead of issuing separate capture commands.

When accessibility misses a control, use the session's explicit screenshot/vision
fallback after inspecting it. A stalled or uncertain action is not permission to
tap again. Do not substitute guessed coordinates or an AI follow-up field.

## Profiles And Context

For handle or creator verification, prefer project commands when they apply:

```bash
PYTHONPATH=src python3 -m openclaw_iphone instagram verify-handles <handle>
PYTHONPATH=src python3 -m openclaw_iphone instagram capture-context
```

When using the UI directly, open only source-confirmed results and report what the current screen actually shows. Do not infer profile identity from an old screenshot or partial search text.

`verify-handles` requires an unambiguous matching profile header; a reel or
result tile is not profile verification. Identity mismatch/uncertainty is a
nonzero result with evidence, not permission to type into guessed search or AI
follow-up fields. Use returned private evidence paths; repeated captures never
overwrite prior runs.

## Boundaries

Do not follow, unfollow, like, comment, message, purchase, or change account settings unless the user explicitly asks for that action. For ambiguous UI states, pause and report the exact evidence rather than guessing.
