---
name: ios-instagram
description: Automate Instagram on the plugged-in physical iPhone using the iphone-control CoreDevice and WebDriverAgent stack. Use for launching Instagram, navigating tabs, searching, reading profile or result screens, capturing screenshot/source evidence, creator verification, and bounded Instagram UI workflows on real iPhone hardware.
---

# iOS Instagram

## Overview

Use this skill for Instagram workflows on the real plugged-in iPhone. It depends on the canonical iPhone control repo configured by `OPENCLAW_IPHONE_REPO_DIR`, normally `~/.openclaw/repos/openclaw-iphone`, and the CoreDevice WDA transport described by `$iphone-control`.

Do not use Simulator assumptions, stale screenshots, `iproxy`, or `localhost:8100`.

## Start With Evidence

From the repo root, verify the phone and WDA before touching Instagram:

```bash
cd "${OPENCLAW_IPHONE_REPO_DIR:-$HOME/.openclaw/repos/openclaw-iphone}"
PYTHONPATH=src python3 -m openclaw_iphone wda status
PYTHONPATH=src python3 -m openclaw_iphone wda locked
PYTHONPATH=src python3 -m openclaw_iphone doctor
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
PYTHONPATH=src python3 -m openclaw_iphone ui source
```

If the device is locked, run `watchdog once` or `wda unlock --verify` once. WDA unlock can recover only when iOS does not require passcode, Face ID, or another secure confirmation. Prevent long workflows from locking by setting Auto-Lock to Never when policy allows. If passcode or Face ID is required, stop and ask the user to unlock it. Continue only after `wda status` is ready and screenshot/source both work.

## Launch Instagram

```bash
PYTHONPATH=src python3 -m openclaw_iphone apps launch Instagram
PYTHONPATH=src python3 -m openclaw_iphone ui elements
PYTHONPATH=src python3 -m openclaw_iphone ui screenshot
```

Use `ui elements` and `ui source` to confirm the current tab or screen before acting.

## Search Flow

Validated flow on the current device:

```bash
PYTHONPATH=src python3 -m openclaw_iphone ui tap-text Explore
PYTHONPATH=src python3 -m openclaw_iphone ui tap-text "Search with Meta AI"
PYTHONPATH=src python3 -m openclaw_iphone ui type "<query>"
```

If `tap-text Explore` does not move the UI, inspect screenshot/source and tap the visible Explore tab by coordinates. To submit a query, tap the visible keyboard Search key or a source-confirmed suggestion, then verify that the accessibility tree or screenshot contains the query and result tabs such as `For you`, `Profiles`, `Audio`, or `Tags`.

For scrolling results:

```bash
PYTHONPATH=src python3 -m openclaw_iphone ui drag --from-x 360 --from-y 780 --to-x 360 --to-y 450 --duration 0.2
```

Capture before/after evidence when scrolling or selecting a result.

## Profiles And Context

For handle or creator verification, prefer project commands when they apply:

```bash
PYTHONPATH=src python3 -m openclaw_iphone instagram verify-handles <handle>
PYTHONPATH=src python3 -m openclaw_iphone instagram capture-context <handle>
```

When using the UI directly, open only source-confirmed results and report what the current screen actually shows. Do not infer profile identity from an old screenshot or partial search text.

## Boundaries

Do not follow, unfollow, like, comment, message, purchase, or change account settings unless the user explicitly asks for that action. For ambiguous UI states, pause and report the exact evidence rather than guessing.
