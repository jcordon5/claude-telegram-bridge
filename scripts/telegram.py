#!/usr/bin/env python3
"""
Telegram bridge for autonomous/long Claude runs — message the user with
progress and ask BLOCKING questions that wait for the user's reply, while they
are away from the computer. No external dependencies (stdlib only).

SECURITY (allowlist):
- The bot only SENDS to your own chat (TELEGRAM_CHAT_ID), so a random person who
  finds the bot can never SEE your messages.
- Inbound messages are accepted ONLY from allow-listed chat ids
  (TELEGRAM_ALLOWED_CHAT_IDS, comma-separated; defaults to TELEGRAM_CHAT_ID).
  Anyone else who writes to the bot is IGNORED — `ask` will not accept their
  reply, so they can't drive the agent.

Config — the skill's OWN .env (this skill dir / .env; gitignored), or OS env:
  TELEGRAM_BOT_TOKEN=123456:ABC...        # from @BotFather
  TELEGRAM_CHAT_ID=123456789              # your chat id (see `chat-id`)
  TELEGRAM_ALLOWED_CHAT_IDS=123456789     # optional; defaults to TELEGRAM_CHAT_ID
  TELEGRAM_ASK_TIMEOUT=21600              # optional; seconds `ask` waits (def 6h)

Commands:
  telegram.py status                      # is it configured? (no secrets shown)
  telegram.py chat-id                     # list recent chats to find your id
  telegram.py notify "text"               # send progress/info (non-blocking)
  telegram.py ask "question" [--timeout N] # ask + WAIT; prints the reply on stdout
  telegram.py test                        # end-to-end check (notify + ask round-trip)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"

# Config lives INSIDE the skill (gitignored), not in the project that uses it.
SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_ROOT / ".env"


def _load_config() -> tuple[str, str, set[str]]:
    # OS env vars take precedence (handy for CI/overrides); otherwise read the
    # skill-local .env so the same config works from any project.
    env = dict(os.environ)
    if CONFIG_PATH.exists():
        for raw in CONFIG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in env or not env[k]:
                env[k] = v.strip().strip('"').strip("'")
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    allowed_raw = (env.get("TELEGRAM_ALLOWED_CHAT_IDS") or chat).strip()
    allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}
    return token, chat, allowed


def _call(token: str, method: str, params: dict | None = None, timeout: int = 70):
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Telegram API error {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Telegram: {exc}")


def _send(token: str, chat: str, text: str) -> None:
    _call(token, "sendMessage", {"chat_id": chat, "text": text})


def _latest_offset(token: str) -> int:
    res = _call(token, "getUpdates", {"timeout": 0})
    ups = res.get("result", [])
    return ups[-1]["update_id"] + 1 if ups else 0


def cmd_status(token: str, chat: str, allowed: set[str]) -> int:
    ok = bool(token and chat)
    print("configured:", "yes" if ok else "no")
    print("bot token:", "set" if token else "MISSING")
    print("chat id:", "set" if chat else "MISSING")
    print("allowlist:", ", ".join(sorted(allowed)) if allowed else "(none → only chat id)")
    print("config file:", CONFIG_PATH, "(exists)" if CONFIG_PATH.exists() else "(create it)")
    return 0 if ok else 1


def cmd_chat_id(token: str) -> int:
    res = _call(token, "getUpdates", {"timeout": 0})
    seen: dict[str, str] = {}
    for up in res.get("result", []):
        msg = up.get("message") or up.get("edited_message") or {}
        c = msg.get("chat") or {}
        if c.get("id") is not None:
            label = c.get("username") or c.get("title") or c.get("first_name") or ""
            seen[str(c["id"])] = label
    if not seen:
        print("No recent chats. Send your bot a message first, then retry.")
        return 1
    print("Recent chats (put the id in TELEGRAM_CHAT_ID):")
    for cid, label in seen.items():
        print(f"  {cid}  {label}")
    return 0


def cmd_notify(token: str, chat: str, text: str) -> int:
    _send(token, chat, text)
    return 0


def cmd_ask(token: str, chat: str, allowed: set[str], question: str, timeout: int) -> int:
    offset = _latest_offset(token)
    _send(token, chat, "❓ " + question + "\n\n(Reply to this message; the agent is waiting.)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _call(token, "getUpdates", {"timeout": 50, "offset": offset}, timeout=70)
        for up in res.get("result", []):
            offset = up["update_id"] + 1
            msg = up.get("message") or up.get("edited_message")
            if not msg:
                continue
            sender = str(msg.get("chat", {}).get("id"))
            if sender not in allowed:
                # Not authorised → ignore silently (don't let randoms drive us).
                continue
            text = msg.get("text")
            if text:
                print(text.strip())  # the user's answer → stdout for the agent
                _send(token, chat, "✅ Got it, continuing.")
                return 0
    sys.stderr.write("No reply within timeout.\n")
    return 3


def cmd_test(token: str, chat: str, allowed: set[str]) -> int:
    _send(token, chat, "🔧 telegram-bridge test: progress messages work.")
    print("Sent a test notification. Now asking a confirmation question…")
    rc = cmd_ask(
        token,
        chat,
        allowed,
        "telegram-bridge test — reply anything to confirm two-way works.",
        timeout=300,
    )
    if rc == 0:
        print("Two-way OK ✅ (notify + ask both work).")
    else:
        print("Did not receive a reply within 5 min. Notify works; check that you replied from an allow-listed chat.")
    return rc


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    cmd = argv[0]
    token, chat, allowed = _load_config()

    if cmd == "status":
        return cmd_status(token, chat, allowed)
    if cmd == "chat-id":
        if not token:
            sys.stderr.write("Missing TELEGRAM_BOT_TOKEN.\n")
            return 2
        return cmd_chat_id(token)

    if not token or not chat:
        sys.stderr.write(
            "Not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in "
            f"{CONFIG_PATH} (or OS env). See the skill's SKILL.md / README.\n"
        )
        return 2

    if cmd == "notify":
        if len(argv) < 2:
            sys.stderr.write("Usage: telegram.py notify <text>\n")
            return 2
        return cmd_notify(token, chat, " ".join(argv[1:]))

    if cmd == "ask":
        args = argv[1:]
        timeout = int(os.environ.get("TELEGRAM_ASK_TIMEOUT", "21600"))
        if "--timeout" in args:
            i = args.index("--timeout")
            timeout = int(args[i + 1])
            args = args[:i] + args[i + 2 :]
        if not args:
            sys.stderr.write("Usage: telegram.py ask <question> [--timeout seconds]\n")
            return 2
        return cmd_ask(token, chat, allowed, " ".join(args), timeout)

    if cmd == "test":
        return cmd_test(token, chat, allowed)

    sys.stderr.write(f"Unknown command: {cmd}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
