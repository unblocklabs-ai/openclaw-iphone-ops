# OpenClaw iPhone Operations

A small Python controller for a dedicated USB-connected physical iPhone.
The agent plans and judges results; this package owns the phone connection and
executes direct WebDriverAgent controls. No second model or runtime dependencies.

Use one persistent `session` for observation, taps, swipes, typing, picker
selection and app transitions. Optional private input comparison, foreground
readiness and route timing help inspect results. The session's
`ready.capabilities` reports supported optional operations.

## Use this checkout

Requires macOS, Python 3.11+, full Xcode, and a signed, running WDA runner on a
trusted USB-connected iPhone with Developer Mode enabled. UI mutations require
an unlocked screen. Configure the dedicated phone's physical UDID in
`~/.openclaw/iphone/config.env` (see `.env.example`).

```sh
PYTHONPATH=src python3 -m openclaw_iphone session --device PHYSICAL_UDID --allow-images
```

Keep the process and its stdin/stdout pipes open. Send one JSON object per line;
read each response before making the next decision. For example:

```json
{"op":"observe"}
{"op":"tap","target":{"role":"XCUIElementTypeButton","label":"Search"}}
{"op":"type","text":"a search query"}
{"op":"press","button":"enter","observe":"image"}
{"op":"swipe","from_x":200,"from_y":700,"to_x":200,"to_y":250}
{"op":"close"}
```

Coordinates default to device points. Image-pixel coordinates explicitly use
`"space":"image"`. See [the session protocol](docs/session.md) for units,
target IDs, input replacement, private input files, masking and failure outcomes.

Observation is a screen, not a native view-tree dump: readable content and
controls, short IDs, meaningful parent groups, bounds and state. Empty layout
wrappers stay internal. With image disclosure enabled, the default also returns
a screenshot. Large screens page automatically within the output budget;
follow `next_offset` without recapturing the screen or invalidating earlier IDs.

The session pins one physical phone and holds exclusive control. Timeouts apply
to operations, not agent deliberation. There are no grants, task files, quotas,
completion verdicts, mandatory readbacks, or reconciliation permissions. Unknown
writes are never replayed automatically. The caller may inspect and choose a
subsequent action. Optional observation failure does not erase acknowledgment.

## Diagnosis and setup

```sh
PYTHONPATH=src python3 -m openclaw_iphone devices list
PYTHONPATH=src python3 -m openclaw_iphone doctor --check-ui
PYTHONPATH=src python3 -m openclaw_iphone apps find Safari
```

Use diagnosis after a failure, not as a ritual before every action. Use
`wda run`, `watchdog once`, app inspection/control, and launchd tooling for setup
and recovery.
Close the session before separate mutating commands.

- [Service setup](docs/launchagent-service.md)
- [Mechanics](docs/mechanics.md) and [troubleshooting](docs/troubleshooting.md)
- [Supervised App Store installation](docs/app-store-installs.md)

## Distribution

This is a CLI/skills bundle, not a native OpenClaw plugin. Install v0.6.0 with
`npm install -g @unblocklabs/openclaw-iphone-ops@0.6.0`.
The package ships the CLI, skills, docs and snippets; register its `skills/` directory with
the agent explicitly. The launcher supports `OPENCLAW_IPHONE_PYTHON` to select
an absolute Python interpreter. Configuration/evidence belong outside the package.
No installation sets up Xcode, WDA, signing or launchd services automatically.

## Validation

```sh
npm run preflight
```

Preflight checks shared versions, offline/loopback tests, and the real packed
npm installation outside the checkout. Physical-device reliability and speed
still require a matched on-device comparison; these checks do not establish it.
See [release procedure](RELEASING.md) before publishing a new version.
