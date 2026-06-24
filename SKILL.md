---
name: telegram-bridge
description: >-
  Lets Claude reach the user on Telegram during long or unattended runs — to
  send progress updates and, crucially, to ask BLOCKING questions and WAIT for
  the user's reply before continuing. Use this skill whenever you're working
  autonomously toward a goal for a while and the user may be away from the
  computer, OR whenever you hit a decision/confirmation/missing-context that
  blocks progress and you'd otherwise stall or guess. Triggers on: "ping me on
  Telegram", "message me when…", "ask me if you get stuck while I'm out", "let
  me know the progress", "work on this for a few hours and tell me if you need
  anything", or any setup where you'll run unattended and need a human in the
  loop. Also use it proactively (even if not asked) when a long task you're
  already running needs a human decision and nobody is watching the terminal.
  If it isn't configured yet, walk the user through the one-time setup here.
---

# Telegram bridge

Talk to the user on Telegram while they're away: send progress, and ask
questions that **block until they answer**, then do exactly what they say. This
keeps long autonomous runs moving without the user babysitting the terminal, and
without you guessing on decisions that are theirs to make.

All logic is in `scripts/telegram.py` (stdlib only, no installs).

## Golden rule — once this skill is in use, ask ONLY via Telegram

While you are using this skill (the user asked you to work via Telegram, or you
sent a Telegram `notify`/`ask` this run), **every question, confirmation, choice,
or missing-context request MUST go through `ask` on Telegram — never through the
Claude chat UI** (no in-chat questions, no AskUserQuestion-style prompts, no
"stopping to ask in the response"). The whole point is that the user may be away
from the computer with **only Telegram access**: a question posted in the chat
would block the run forever because they can't see or answer it there.

Concretely:
- Need a decision? → `ask` on Telegram and act on the reply. Do **not** also ask
  in chat, and do **not** pause expecting a chat answer.
- `ask` timed out (exit code 3)? → don't assume; park that piece as blocked and
  continue with unblocked work, or stop and report **via `notify`**.
- Before ending a turn during an unattended run, send a `notify` so the user can
  redirect from Telegram if they want.

## Install

Primary (recommended):

```bash
npx skills add https://github.com/jcordon5/claude-telegram-bridge.git
```

Or clone into your skills dir manually:

```bash
git clone https://github.com/jcordon5/claude-telegram-bridge.git ~/.claude/skills/telegram-bridge
```

## When to use it

- **Progress / info** — at meaningful milestones of a long run, send a short
  `notify` so the user can follow along from their phone. Don't spam; surface
  the things a human would want to know.
- **Blocking question** — whenever you need a **decision, confirmation, or extra
  context** you can't safely assume (e.g. "rename this model? (yes/no)", "which
  of these two approaches?", a missing credential or value), use `ask`. It posts
  the question and **waits for the reply**, which you then follow literally.
- **Don't assume on no-answer** — if `ask` times out, do NOT guess. Park that
  piece of work (mark it blocked) and move on to something unblocked, or stop
  and report.

## First: is it configured?

Run a quick check before relying on it:

```bash
python3 ~/.claude/skills/telegram-bridge/scripts/telegram.py status
```

- If it prints `configured: yes` → it's ready; go to **Usage**.
- If `configured: no` → walk the user through **Setup** below, then test it.

(Adjust the path if the skill lives elsewhere, e.g. a project-local `.claude/skills/`.)

## Setup (one time — guide the user through this)

The user has to create the bot in their own Telegram; you can't do it for them.
Give them these steps, then run the commands you can:

1. **Create the bot**: in Telegram, open a chat with **@BotFather**, send
   `/newbot`, follow the prompts. BotFather returns a **bot token** like
   `123456:ABC-DEF...`.
2. **Start the chat**: the user sends any message (e.g. "hola") to their new bot
   so a chat exists.
3. **Find the chat id** — you run:
   ```bash
   TELEGRAM_BOT_TOKEN=<token> python3 .../scripts/telegram.py chat-id
   ```
   It prints the recent chats' ids. The user confirms which is theirs.
4. **Save the config inside the skill** — copy `.env.example` to `.env` in the
   skill folder (it's gitignored) and fill it in. Living in the skill means the
   same config works from *any* project; OS env vars override it if set.
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=<the user's chat id>
   TELEGRAM_ALLOWED_CHAT_IDS=<the user's chat id>   # optional; defaults to CHAT_ID
   TELEGRAM_ASK_TIMEOUT=21600                        # optional; seconds ask() waits
   ```
   Never commit the token (the skill's `.env` is gitignored).
5. **Test it** (you run this):
   ```bash
   python3 .../scripts/telegram.py test
   ```
   It sends a progress message and then a confirmation question; ask the user to
   reply in Telegram. If you see `Two-way OK` and the reply printed, it works.

## Security — allowlist (why a random person can't hijack it)

The user's worry: "what if some random person finds the bot and messages it?"

- **They can't see your messages.** A Telegram bot's messages go to a specific
  chat; `notify`/`ask` only send to `TELEGRAM_CHAT_ID` (the user). Strangers who
  open the bot get their *own* empty chat and never see the user's updates.
- **They can't drive the agent.** `ask` only accepts replies from chats in
  `TELEGRAM_ALLOWED_CHAT_IDS` (defaults to just `TELEGRAM_CHAT_ID`). Any message
  from a non-allow-listed chat is **ignored** — it won't be taken as the answer.
- To authorise more people (e.g. a teammate), add their chat ids to
  `TELEGRAM_ALLOWED_CHAT_IDS`, comma-separated.

So: a stranger messaging the bot has **no effect** — they see nothing useful and
can't answer questions on the user's behalf.

## Usage

Config is read from the skill's own `.env`, so these work from any directory:

```bash
# progress / info (non-blocking)
python3 .../scripts/telegram.py notify "Done with step 3/7: M3 endpoints. Running tests."

# blocking question — prints the user's reply to stdout, then you act on it
ANSWER=$(python3 .../scripts/telegram.py ask "Rename Gap → NonConformity now? (yes/no)")
# → use $ANSWER literally to decide what to do; exit code 3 means no reply (don't assume)

# helpers
python3 .../scripts/telegram.py status     # configured?
python3 .../scripts/telegram.py chat-id     # discover chat ids
python3 .../scripts/telegram.py test        # end-to-end check
```

Notes:
- `ask` blocks (long-polling) up to `TELEGRAM_ASK_TIMEOUT` (default 6h) so it
  survives the user being away. Exit code `0` = answered (reply on stdout),
  `3` = timed out (don't assume — park the work), `2` = not configured.
- Keep messages short and human; the user reads them on a phone.
- This is a side channel, not a log — send what a human actually wants to know.

## Driving the agent FROM Telegram (`listen`) — token-efficient loop

`listen` lets the **user send new instructions from Telegram** without the
Claude app open in front of them. The trick that keeps it cheap: the polling is
done by the **background shell process**, not by the model.

**Tell the user this exists.** The first time you set up or use the bridge in a
run (and whenever you start a `listen` loop), briefly inform the user — in chat
and/or via `notify` — that they can now **send you prompts from Telegram** (it
behaves like typing in the app, runs on their normal session/quota) and that
they **stop the loop** with `stop`/`para`. Don't assume they know the feature is
there; surface it once, concisely.

```bash
# Run in the BACKGROUND (e.g. Claude Code's run_in_background). It long-polls
# Telegram and blocks — pure network, ZERO model tokens while idle.
python3 .../scripts/telegram.py listen
```

Exit codes drive the loop:
- `0` → a new message arrived; its text is on **stdout**. The agent is
  re-invoked **once**, acts on the instruction, then **relaunches `listen`** in
  the background.
- `4` → the user sent a stop word (`stop`, `para`, `/stop`, `parar`, `detente`,
  "deja de escuchar"). The agent **does not relaunch** → the loop ends.
- `3` → timed out (`TELEGRAM_LISTEN_TIMEOUT`, default 6h) with no message; the
  agent may relaunch (one cheap turn) or stop.

### Why this barely costs tokens

- **While waiting:** the background process is just an HTTP long-poll to
  Telegram. The model is not running, so **0 tokens** are spent — for minutes or
  hours.
- **Per message:** the model wakes up exactly once (when `listen` exits),
  handles that one instruction, relaunches `listen`, and goes idle again. So
  token cost scales with the **number of messages you send**, not with elapsed
  time. (A naive "model polls every N seconds" loop, by contrast, re-bills the
  whole conversation context every tick — that's the expensive thing to avoid.)

### Is keeping the session alive a problem?

Mostly no, with three caveats:
- The host machine must stay awake and online (the background process lives in
  that session).
- Each handled message adds to the transcript, so a very long-lived loop slowly
  grows context (later turns cost a bit more until the harness compacts). Fine
  for hours; for days, restart the loop occasionally.
- Always relaunch with a long `--timeout` so idle time doesn't trigger needless
  model wake-ups.

### How to stop it

- From Telegram: send `stop` / `para` / `/stop` → clean end (exit 4).
- Locally: stop/kill the background `listen` task (e.g. Claude Code's task-stop),
  or just don't relaunch it after the next message.

### Multiple sessions — automatic (plug & play, on by default)

You **don't manage any of this**. Multi-session routing is the default: just use
`notify` / `ask` / `listen` exactly as above and the skill takes care of it.

Under the hood, the first command that needs it **auto-starts a detached broker**
(a single `getUpdates` consumer, so N sessions never race the consume-once
queue), each session **auto-names itself** after its project directory, and
outbound messages are **auto-tagged** `[name]`. The broker survives the session
that spawned it and **shuts itself down** once no session has been live for a
while. State lives in `~/.telegram-bridge/` (outside any repo).

So in practice, per session, this is all you run — same as single-session:

```bash
python3 .../scripts/telegram.py listen            # auto: broker + session = dir name
python3 .../scripts/telegram.py notify "tests ok"  # auto-tagged "[DCIBERID] tests ok"
```

The user targets a session from Telegram by (in priority order):
1. **Reply** to one of its tagged messages → that session (cleanest, zero typing).
2. **`name: ...`** prefix where `name` is a live session.
3. **`/sessions`** → inline-keyboard menu to pick the active session.
4. **stop word** → stops the targeted/active session.
5. otherwise → the active session, or the only live one; if ambiguous it asks.

Overrides (rarely needed): `--session NAME` (or `TELEGRAM_SESSION`) to set the
name explicitly; `TELEGRAM_NO_BROKER=1` to force the old direct single-session
mode; `telegram.py broker-stop` to stop the daemon now; `telegram.py sessions`
to list live ones.
