# telegram-bridge

A reusable skill for **Claude, Codex, and other agents** that lets your AI agent reach you on **Telegram**
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

- Private-chat commands only **send** to your own chat, so a stranger who finds the bot never
  sees your messages.
- Inbound replies are accepted **only** from allow-listed chat ids
  (`TELEGRAM_ALLOWED_CHAT_IDS`, defaults to your `TELEGRAM_CHAT_ID`). Anyone else
  who writes to the bot is **ignored** — they can't answer on your behalf or
  drive the agent.
- Optional group mode explicitly registers a separate group destination and
  queue. Members of that group can contribute messages and attachments; this
  does not change the private-chat configuration or allowlist.

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

## Natural group conversations and attachments (optional)

Keep the existing `.env` and private commands unchanged. Use the additional
stdlib helper with a group ID supplied explicitly or via
`TELEGRAM_GROUP_CHAT_ID`. Replace the example ID with your own:

```bash
python3 scripts/group.py --chat -123456789 --label Codex status
python3 scripts/group.py --chat -123456789 --label Codex notify 'The first draft is ready.'
python3 scripts/group.py --chat -123456789 --label Codex listen --batch --timeout 21600
python3 scripts/group.py --chat -123456789 --label Codex ask 'How many pages do you need?'
python3 scripts/group.py --chat -123456789 --label Codex send-file /path/to/draft.pdf --caption 'Draft'
python3 scripts/group.py --chat -123456789 download FILE_ID /path/to/reference.pdf
python3 scripts/group.py --chat -123456789 history --limit 50
```

One shared broker consumes Telegram updates for both modes. A registered group
has one active inbox, separate from private sessions; `--session NAME` selects
its owner. `drain` retrieves queued messages without waiting, and `listen
--batch` returns a JSON array of currently queued messages once the first
arrives. Sender identity, edit state and attachment metadata are retained.
Text, files, photos, voice messages, stickers and other content can be delivered
without requiring a reply-to, mention or command. The **agent** decides whether
a message is casual conversation, feedback, a task, or needs no answer. Receipt
does not automatically send a "working" acknowledgement. Plain words such as
`para` remain available for contextual interpretation; `/stop` stops the group
listener explicitly. The original private-session stop words remain unchanged.

**Telegram must deliver those messages first:** make the bot an administrator
in the selected group, or disable its privacy mode through BotFather and re-add
it. The helper cannot change those permissions itself. `status` reports the
effective group access. See [Telegram privacy mode](https://core.telegram.org/bots/features#privacy-mode).

Group `ask` accepts any next delivered message by default. `--strict-reply`
opts into exact reply-to matching. The caller interprets the response in context
and can keep listening if it is unrelated to the pending question.

When Telegram upgrades a registered group to a supergroup, the bridge follows
its migration service message or structured migration error, preserving the
queue and local history. Existing group IDs continue to resolve to the new ID.

`history` reads a local archive of group messages actually observed by the
bridge, including files it sent. It is **not** a Telegram history API and cannot
recover messages from before the bot joined or updates already consumed before
archiving was enabled. Runtime state stays under `~/.telegram-bridge/`, outside
the repository. No bot tokens or real group IDs belong in commits.

The waiting process uses no model tokens while idle. Continuing the agent when
it exits depends on the host delivering tool completion to the agent; a skill
alone does not establish a permanent wakeup service after a completed turn.
Keep the host awake and test that lifecycle in the chosen agent environment.

Run the offline regression suite with `python3 -m unittest discover -s tests -v`.

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
