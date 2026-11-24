# Goal navigation (optional)

Off by default. When enabled, the session's `goal` operation hands navigation to
OpenAI's Decisions API (`gpt-6-luna`, the default), Liquid AI's LFM-d1 (`d1.2` / `d1.2-ultra` / local `d1-omni-*`),
or TypeSafe (`jev-1.14.0`): each step captures the screen, the model
picks the next tap, text entry or scroll, and the session performs it, until the
goal looks done or something needs the agent. The agent still owns the task: it supplies the goal,
any text to type and which risky effects are pre-approved, and it checks the result.

## Enable

### Liquid AI LFM-d1 (hosted `d1.2` / `d1.2-ultra` or local `d1-omni-*`)

Liquid AI's `d1` decision models evaluate screenshots in a single forward pass with
zero output tokens and support native **`box` visual grounding**. When configured,
`ready.capabilities` includes both `goal` and `goal_vision_box`:

- **Automatic AX-free fallback:** In heavy or shallow apps (such as App Store, X,
  Instagram, or WebViews) where accessibility `/source` reads are slow or capped,
  `goal` automatically switches to single-pass screenshot + `box` grounding
  (combining action, completion, overlay, sensitive-screen guard, target coordinates,
  and tap risk in one pass). You can also force `"grounding": "vision"` per request.
- **Local Apple Silicon MLX `d1-omni-600M` (zero core dependencies):** Install
  `mlx-vlm` into an isolated host venv (`~/.openclaw/iphone/mlx-venv`) and serve
  `LiquidAI/d1-omni-600M-MLX-8bit` (~600 MB RAM) over loopback `127.0.0.1:8781`.
  Run it either as a zero-API-cost local navigator (`--configure-navigator`) or as
  a fast local privacy guard ahead of a cloud provider (`--configure-guard`,
  screening password/OTP/payment/CAPTCHA screens before any cloud call). Note:
  `d1-omni-600M` evaluates `noul`/`choice`/`score` natively (so `goal_vision_box`
  defaults to off for `d1-omni*` models and on for `d1.2*`; override with
  `OPENCLAW_IPHONE_GOAL_VISION_BOX=1|0`).

```sh
# 1) Local Apple Silicon MLX d1-omni-600M (isolated venv, no core runtime deps)
openclaw-iphone d1 install --prefetch --configure-navigator   # or --configure-guard
openclaw-iphone d1 serve                                      # or install snippets/launchd/install-d1-serve-launchagent.sh
openclaw-iphone d1 status
openclaw-iphone goal check

# 2) Hosted Liquid AI d1.2 (or --model d1.2-ultra) with Box visual grounding
openclaw-iphone goal setup --provider liquid --key-file ~/.secrets/liquid.env --model d1.2
openclaw-iphone goal check

# 3) Hosted cloud navigator + local MLX d1-omni-600M privacy guard
openclaw-iphone d1 install --prefetch --configure-guard
openclaw-iphone goal setup --provider liquid --key-file ~/.secrets/liquid.env --model d1.2
```

Equivalent host settings:

```sh
OPENCLAW_IPHONE_GOAL_ENABLED="1"
OPENCLAW_IPHONE_GOAL_PROVIDER="liquid"
OPENCLAW_IPHONE_GOAL_MODEL="d1.2"
OPENCLAW_IPHONE_LIQUID_API_KEY_FILE="~/.secrets/liquid.env"
# OPENCLAW_IPHONE_LIQUID_BASE_URL="https://api.liquid.ai/decisions/v1"
```

### Reuse a TypeSafe/Jev key

```sh
openclaw-iphone goal setup --provider typesafe --key-file ~/.openclaw/secrets/unblock-memory-typesafe.env
openclaw-iphone goal check
```

The referenced file can contain `TYPESAFE_API_KEY=...` or a bare key. The key stays
in that file and is read on each new session; it is not copied to the iPhone config.
Alternatively, use `--key-stdin` or an existing `TYPESAFE_API_KEY` environment variable.
TypeSafe credentials never fall back to an OpenAI key (or vice versa).

**Experimental vision:** TypeSafe labels image support experimental. This adapter
sends the `TypeSafe-Danger-Zone-Images: true` header for screenshot requests only.
It preserves the navigator's consent/risky-tap checks, but the OpenAI benchmark
results below do not establish Jev's accuracy. Validate the actual app workflow
before unattended use. See [TypeSafe images](https://docs.typesafe.ai/images).

Equivalent host settings:

```sh
OPENCLAW_IPHONE_GOAL_ENABLED="1"
OPENCLAW_IPHONE_GOAL_PROVIDER="typesafe"
OPENCLAW_IPHONE_TYPESAFE_API_KEY_FILE="~/.openclaw/secrets/unblock-memory-typesafe.env"
```

### OpenAI (default)

Goal navigation needs an OpenAI API key with Decisions API access. Either store
the key, or point to where it already is:

```sh
# store it in the config file (written owner-only; the key is read from stdin, never echoed)
printf '%s' "$KEY" | openclaw-iphone goal setup --key-stdin
# or point to a file that holds it: a bare key, or a .env file with an OPENAI_API_KEY= line
openclaw-iphone goal setup --key-file ~/.secrets/openai.env
# or only turn it on and use OPENAI_API_KEY from the session's environment
openclaw-iphone goal setup

openclaw-iphone goal check            # one tiny call, no screen data: proves key and access
openclaw-iphone goal setup --disable  # off again; key settings are kept
```

`goal setup` edits the config file the CLI reads (`OPENCLAW_IPHONE_CONFIG`, else
`~/.openclaw/iphone/config.env`), keeps its other settings and leaves it owner-only.
Restart any running `session` afterwards. The same settings by hand:

```sh
OPENCLAW_IPHONE_GOAL_ENABLED="1"                         # the toggle
OPENCLAW_IPHONE_OPENAI_API_KEY="sk-..."                  # 1. the key itself
OPENCLAW_IPHONE_OPENAI_API_KEY_FILE="~/.secrets/openai"  # 2. or where it is
# 3. otherwise OPENAI_API_KEY from the environment
# Optional: an API-compatible proxy instead of https://api.openai.com/v1
# OPENCLAW_IPHONE_OPENAI_BASE_URL="https://proxy.example/v1"
```

The first key found in that order is used. `openclaw-iphone doctor` and `goal check`
report `goal-navigation: off`, `on (gpt-6-luna via OpenAI's Decisions API; key: <where>)`
or `incomplete (<what is missing>)`, never the key. When it is on, the session's
`ready.capabilities` includes `goal`.

**Privacy:** every step sends a screenshot and the screen's text (labels, names)
to the selected provider. Text supplied with `text` is typed on the phone, not sent. A screen with
a password field is never sent; the goal stops instead. Enable this only where
sending screen contents to that provider is acceptable (OpenAI offers Zero Data Retention
to eligible API organizations).

## Request

```json
{"op":"goal","goal":"Show bill.epsilon's followers","app":"Instagram"}
{"op":"goal","goal":"Message naughtybek from bill.epsilon's followers list","app":"com.burbn.instagram","text":"See you at 5","approve":["communication"],"max_steps":14}
```

- `goal`: what should be on screen when done. Name things the screen shows
  (`bill.epsilon`, not "Bill"). A single goal is optimized for its end state; when
  the route matters, send ordered goals one request at a time.
- `app`: optional app name (`"X"`, `"Instagram"`) or bundle identifier to open first.
  The navigator only taps what is on screen: it can't open an app that isn't on the
  current home-screen page, and starting from the home screen sends that screen
  (widgets included) to the configured provider. A name costs one installed-app lookup (~0.3 s, cached for 10 min);
  a bundle identifier skips it.
- `text`: typed with `set` (whole-field replacement) when a focused field needs it.
- `approve`: risky effects the agent pre-approves: `communication`,
  `social_action`, `purchase_install`, `permission_grant`. Default none.
- `max_steps`: 1-30, default 14.
- `grounding`: optional `"auto"` (default), `"ax"`, or `"vision"`. With a
  `Box`-capable provider (`liquid` / LFM-d1), `"auto"` automatically falls back
  to AX-free screenshot + `box` grounding when `/source` fails or the app is in
  `shallow-apps.json`; `"vision"` skips `/source` altogether.

The reply has no observation; observe separately when you need the screen.

## Reply

```json
{"status":"action","dispatch":"acknowledged","acknowledged_substeps":3,"outcome":"needs_approval",
 "reason":"communication","pending":{"target":"SNAPSHOT_ID:42","label":"Send","effect":"communication"},
 "steps":[{"app":"com.burbn.instagram","action":"tap","label":"Message","confidence":0.97,
           "risk":{"risky":0.02,"effect":"navigate"},"dispatch":"acknowledged"}],
 "snapshot_id":"SNAPSHOT_ID","navigator_seconds":0.9}
```

`outcome`:

- `done`: the navigator judges the goal complete on screen. A judgment, not proof: check it.
- `needs_approval`: the next tap's effect (`reason`) was not pre-approved; nothing
  was tapped. `pending.target` is valid until your next observation: ask, then tap it
  with `{"op":"tap","target":...}` or observe and decide yourself.
- `escalate`: the navigator or the loop stopped (`reason`): `app_not_found` or
  `app_not_launched` (for `app`), `permission_or_consent_prompt`
  (never auto-answered), `low_confidence`, `nothing_on_screen_helps`,
  `secure_field`, `text_needed`, `repeated_type`, `no_progress`,
  `cannot_dismiss_overlay_safely`, `local_guard_unavailable`, `screen_changed`,
  `navigator_unavailable` (with a safe
  `error.category`, e.g. `http_401`; rate limits and server errors are retried twice first), `capture_failed`, `action_not_sent`,
  `inspect_before_retry` (a write may have happened), `interrupted`.
- `step_limit`: `max_steps` ran out.

The navigator stops rather than guess, and the session stays open: take over by
observing, then act yourself or send a narrower goal (`"Tap Back to the inbox"`).
When the navigator itself escalated (`permission_or_consent_prompt`, `low_confidence`,
`nothing_on_screen_helps`), the last step lists what it leaned toward:

```json
"considered":[{"option":"none","probability":0.36},
              {"option":"go_back","target":"SNAPSHOT_ID:21","label":"Back","probability":0.14},
              {"target":"SNAPSHOT_ID:19","label":"Bek,","probability":0.06}]
```

Options are the navigator's own (`go_back`, `scroll_down`, `none`, ...); an element's
`target` is valid until your next observation, so `{"op":"tap","target":...}` works
without observing first. Treat these as hints, not decisions.

`dispatch` and `acknowledged_substeps` count inputs actually sent, as in every
other operation; each step lists its own `dispatch`. Uncertain writes are never
replayed. Taps go to the chosen element's center on the screen the navigator just
judged, after one screenshot confirms the screen hasn't moved; if it has, the tap
finds the element by identity instead. Looking the element up by ID is a second
tree read: 0.3 s in light apps, and over 30 s (then a timeout) in X's timeline at
full depth; a coordinate tap took 0.75 s everywhere. Screens of apps measured slow are read only 22 levels deep (see
[session](session.md)), which keeps menus, tabs and buttons above post contents.

## How a step works

1. Wait until two screenshots 0.12 s apart match (at most 2.0 s), then read the AX source.
2. Build candidates: on-screen controls and text, without scroll bars, keyboard keys,
   empty containers, controls under the tab bar, keyboard or status bar, or text that
   repeats its control's label. Each candidate is a choice described by its own line.
3. One Decisions call asks five independent questions (next option, done, blocked,
   progress, done with criteria). Code decides: a consent/permission prompt escalates;
   the goal is done when progress >= 2.5 and the criteria-based done check agree (or
   next = done with both done signals >= 0.7), unless next still gives one control
   >= 0.6; a tap needs >= 0.35 combined probability on options under the same point.
   Going back is one option: next's `go_back` plus the screen's own Back button (a
   top-left button named or labeled back). It taps that button, and is preferred
   over giving up (`none`) when it has >= 0.25, since it only navigates; with no
   back button it escalates like `none`.
4. A tap gets one more call, the tap check: would it send, post, follow, buy, call,
   delete or grant something, and is the control covered by (or not part of) an open
   popup or menu? A covered control means close the overlay first; risky taps need
   approval unless their effect was pre-approved.

Questions, thresholds and wording are in `src/openclaw_iphone/navigator.py`.
Validated offline on 155 labeled screens from a dedicated iPhone (Instagram, App
Store, Settings, system prompts) and 16 probe cases: 150/155 steps correct (28/31
on held-out cases), 16/16 probes, 0 consent prompts acted on; the tap check caught
19/19 risky taps with no false alarms on 117 safe ones. On 42 more screens that start
away from the goal (inside a chat thread, someone else's profile, a deep Settings page,
or another app entirely): 25/27, and 13/15 on screens held out until the end (both
held-out misses go back once when the goal is in a different app). A Decisions call takes about
0.25-0.3 s from a home connection (about 0.1 s of it server time); a step's capture
takes about 1 s on an iPhone 11.
