# Goal navigation (optional)

Off by default. When enabled, the session's `goal` operation hands navigation to
OpenAI's Decisions API (`gpt-6-luna`): each step captures the screen, the model
picks the next tap, text entry or scroll, and the session performs it, until the
goal looks done or something needs the agent. The agent still owns the task: it supplies the goal,
any text to type and which risky effects are pre-approved, and it checks the result.

## Enable

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
to OpenAI. Text supplied with `text` is typed on the phone, not sent. A screen with
a password field is never sent; the goal stops instead. Enable this only where
sending screen contents to OpenAI is acceptable (OpenAI offers Zero Data Retention
to eligible API organizations).

## Request

```json
{"op":"goal","goal":"Open Instagram and view bill.epsilon's followers"}
{"op":"goal","goal":"Message naughtybek from bill.epsilon's followers list","text":"See you at 5","approve":["communication"],"max_steps":14}
```

- `goal`: what should be on screen when done. Name things the screen shows
  (`bill.epsilon`, not "Bill"). A single goal is optimized for its end state; when
  the route matters, send ordered goals one request at a time.
- `text`: typed with `set` (whole-field replacement) when a focused field needs it.
- `approve`: risky effects the agent pre-approves: `communication`,
  `social_action`, `purchase_install`, `permission_grant`. Default none.
- `max_steps`: 1-30, default 14.

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
- `escalate`: the navigator or the loop stopped (`reason`): `permission_or_consent_prompt`
  (never auto-answered), `low_confidence`, `nothing_on_screen_helps`,
  `secure_field`, `text_needed`, `repeated_type`, `no_progress`,
  `cannot_dismiss_overlay_safely`, `navigator_unavailable` (with a safe
  `error.category`, e.g. `http_401`; rate limits and server errors are retried twice first), `capture_failed`, `action_not_sent`,
  `inspect_before_retry` (a write may have happened), `interrupted`.
- `step_limit`: `max_steps` ran out.

`dispatch` and `acknowledged_substeps` count inputs actually sent, as in every
other operation; each step lists its own `dispatch`. Uncertain writes are never
replayed. Taps use the step's snapshot ID, falling back to the element's center
only when the ID no longer resolves to one control.

## How a step works

1. Wait until two screenshots 0.15 s apart match (at most 3 s), then read the AX source.
2. Build candidates: on-screen controls and text, without scroll bars, keyboard keys,
   empty containers, controls under the tab bar, keyboard or status bar, or text that
   repeats its control's label. Each candidate is a choice described by its own line.
3. One Decisions call asks five independent questions (next option, done, blocked,
   progress, done with criteria). Code decides: a consent/permission prompt escalates;
   the goal is done when progress >= 2.5 and the criteria-based done check agree (or
   next = done with both done signals >= 0.7), unless next still gives one control
   >= 0.6; a tap needs >= 0.35 combined probability on options under the same point.
4. A tap gets one more call, the tap check: would it send, post, follow, buy, call,
   delete or grant something, and is the control covered by (or not part of) an open
   popup or menu? A covered control means close the overlay first; risky taps need
   approval unless their effect was pre-approved.

Questions, thresholds and wording are in `src/openclaw_iphone/navigator.py`.
Validated offline on 155 labeled screens from a dedicated iPhone (Instagram, App
Store, Settings, system prompts) and 16 probe cases: 150/155 steps correct (28/31
on held-out cases), 16/16 probes, 0 consent prompts acted on; the tap check caught
19/19 risky taps with no false alarms on 117 safe ones. A Decisions call takes about
0.25-0.3 s from a home connection (about 0.1 s of it server time); a step's capture
takes about 1 s on an iPhone 11.
