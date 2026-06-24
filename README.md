# telegram-bridge

A reusable **Claude skill** that lets your AI agent reach you on **Telegram**
while it works on long or unattended tasks — to send progress updates and,
crucially, to **ask blocking questions and wait for your reply** before
continuing. So you can step away from the computer and the agent still keeps
moving, pinging you when it needs a decision instead of guessing or stalling.

## Why

When an agent runs for hours toward a goal, two things go wrong if you're not at
the keyboard: it either **stops** at the first decision it can't make, or it
**guesses**. This bridge gives it a side channel to you: `notify` for progress,
`ask` for a decision it then follows literally.

## Security (allowlist)

- The bot only **sends** to your own chat, so a stranger who finds the bot never
  sees your messages.
- Inbound replies are accepted **only** from allow-listed chat ids
  (`TELEGRAM_ALLOWED_CHAT_IDS`, defaults to your `TELEGRAM_CHAT_ID`). Anyone else
  who writes to the bot is **ignored** — they can't answer on your behalf or
  drive the agent.

## Install

**Primary (recommended):**

```bash
npx skills add https://github.com/jcordon5/claude-telegram-bridge.git
```

Or clone it into your skills directory manually:

```bash
git clone https://github.com/jcordon5/claude-telegram-bridge.git \
  ~/.claude/skills/telegram-bridge
```

## Setup (one time)

1. In Telegram, talk to **@BotFather**, send `/newbot`, follow the prompts → you
   get a **bot token**.
2. Send your new bot any message (so a chat exists).
3. Find your chat id:
   ```bash
   TELEGRAM_BOT_TOKEN=<token> python3 scripts/telegram.py chat-id
   ```
4. Copy `.env.example` to `.env` (in the skill folder; it's gitignored) and fill
   in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Optionally
   `TELEGRAM_ALLOWED_CHAT_IDS`.
5. Test it:
   ```bash
   python3 scripts/telegram.py test
   ```

Config lives **inside the skill** (`.env` next to `SKILL.md`), so the same setup
works from any project. OS environment variables override the file if set.

## Usage

```bash
python3 scripts/telegram.py status        # is it configured?
python3 scripts/telegram.py chat-id       # discover chat ids
python3 scripts/telegram.py notify "..."  # progress / info (non-blocking)
python3 scripts/telegram.py ask "..."     # ask + WAIT; prints the reply to stdout
python3 scripts/telegram.py listen        # WAIT for your next message (run in background)
python3 scripts/telegram.py test          # end-to-end check
```

`ask`/`listen` exit codes: `0` answered (reply/message on stdout), `3` timed out,
`2` not configured. `listen` also returns `4` when you send a stop word.

## Drive Claude from Telegram (send it prompts from your phone)

You don't only receive updates — you can **send Claude new instructions from
Telegram**, as if you typed them in the app. Claude runs `listen` in the
background; it long-polls Telegram for free (no model tokens while it waits) and
wakes Claude only when your message arrives. Then Claude does it, replies on
Telegram, and listens again. So cost scales with the number of messages you
send, not with idle time.

- **Use it:** ask Claude to "listen on Telegram" (or it offers it for unattended
  work). Then just message the bot whatever you want done.
- **Stop it:** send `stop`, `para`, `/stop`, `parar` or `detente`.
- **Note:** it runs inside your normal Claude session/quota — a Telegram message
  is billed exactly like typing the same prompt in the app. The machine running
  Claude must stay on while you're away.

### Multiple sessions at once (automatic)

One bot drives many Claude sessions with **zero setup** — it's on by default.
Each session just uses `notify`/`listen` as usual; the skill auto-starts a single
router (broker) behind the scenes, names each session after its project folder,
and tags messages `[folder] …` so you always know who's talking.

To target a session from Telegram: **reply** to one of its messages, prefix with
`folder: …`, or send `/sessions` for a tap-to-pick menu. `stop`/`para` stops that
session; `para todo` / `stop all` stops every session. When a second session
appears you get a one-time heads-up explaining how to switch, and messages aren't
lost if the router briefly restarts. (`TELEGRAM_NO_BROKER=1` forces the old
single-session mode.)

Claude picks all this up automatically from `SKILL.md`; you mostly just need the
one-time setup above.

## License

MIT.
