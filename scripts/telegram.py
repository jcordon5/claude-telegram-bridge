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
  TELEGRAM_LISTEN_TIMEOUT=21600           # optional; seconds `listen` waits (def 6h)

Commands:
  telegram.py status                      # is it configured? (no secrets shown)
  telegram.py chat-id                     # list recent chats to find your id
  telegram.py notify "text"               # send progress/info (non-blocking)
  telegram.py ask "question" [--timeout N] # ask + WAIT; prints the reply on stdout
  telegram.py listen [--timeout N]        # WAIT for the next user msg (run in background)
  telegram.py test                        # end-to-end check (notify + ask round-trip)

Multi-session (optional): run ONE `broker` (background) and have each session use
`--session NAME` on notify/ask/listen. The broker owns getUpdates and routes each
incoming message to the right session (reply-to a tagged msg, "name: ..." prefix,
/sessions menu, or the active one). Without a broker, the single-session commands
above work exactly as before.
  telegram.py broker                      # single consumer that routes to sessions
  telegram.py sessions                    # list live sessions
  telegram.py notify "text" --session m3  # tagged "[m3] text" (reply routes back)
  telegram.py listen --session m3         # wait for messages routed to "m3"
"""

from __future__ import annotations

import json
import os
import subprocess
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


# ---------------------------------------------------------------------------
# Multi-session state (broker mode). Lives OUTSIDE the skill's git repo.
# A single `broker` process owns getUpdates (no race) and routes each incoming
# message to the right session's local inbox; sessions wait on their inbox.
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("TELEGRAM_STATE_DIR") or (Path.home() / ".telegram-bridge"))
SESS_DIR = STATE_DIR / "sessions"      # heartbeat: <name>.json  → {"ts": ...}
INBOX_DIR = STATE_DIR / "inbox"        # <name>/<seq>.txt        → queued messages
MSGMAP_PATH = STATE_DIR / "msgmap.json"  # {message_id: {"session": str, "ts": float}}
ACTIVE_PATH = STATE_DIR / "active_target"  # plain text: last selected session
BROKER_PID = STATE_DIR / "broker.pid"  # liveness of the broker
OFFSET_PATH = STATE_DIR / "offset"     # last consumed getUpdates offset (survives restarts)
HEARTBEAT_TTL = 90.0                   # a session is "live" if its heartbeat is younger
STOP_TOKEN = "\x00__STOP__"            # enqueued sentinel that tells a session to stop


def _state_init() -> None:
    for d in (STATE_DIR, SESS_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _heartbeat(name: str) -> None:
    _state_init()
    (SESS_DIR / f"{name}.json").write_text(json.dumps({"ts": time.time()}), encoding="utf-8")


def _live_sessions() -> list[str]:
    if not SESS_DIR.exists():
        return []
    out = []
    now = time.time()
    for f in sorted(SESS_DIR.glob("*.json")):
        try:
            ts = json.loads(f.read_text(encoding="utf-8")).get("ts", 0)
        except (OSError, ValueError):
            continue
        if now - ts <= HEARTBEAT_TTL:
            out.append(f.stem)
    return out


def _enqueue(name: str, text: str) -> None:
    box = INBOX_DIR / name
    box.mkdir(parents=True, exist_ok=True)
    # Monotonic-ish name so dequeue order is FIFO even within the same second.
    seq = f"{time.time():.6f}_{os.getpid()}"
    (box / f"{seq}.txt").write_text(text, encoding="utf-8")


def _dequeue(name: str) -> str | None:
    box = INBOX_DIR / name
    if not box.exists():
        return None
    files = sorted(box.glob("*.txt"))
    if not files:
        return None
    f = files[0]
    try:
        text = f.read_text(encoding="utf-8")
    finally:
        f.unlink(missing_ok=True)
    return text


def _record_msgmap(message_id, session: str) -> None:
    if message_id is None:
        return
    _state_init()
    try:
        data = json.loads(MSGMAP_PATH.read_text(encoding="utf-8")) if MSGMAP_PATH.exists() else {}
    except ValueError:
        data = {}
    data[str(message_id)] = {"session": session, "ts": time.time()}
    # Keep the map bounded: drop entries older than 7 days, cap at 1000.
    cutoff = time.time() - 7 * 86400
    data = {k: v for k, v in data.items() if v.get("ts", 0) >= cutoff}
    if len(data) > 1000:
        for k in sorted(data, key=lambda k: data[k]["ts"])[: len(data) - 1000]:
            del data[k]
    MSGMAP_PATH.write_text(json.dumps(data), encoding="utf-8")


def _msg_session(message_id) -> str | None:
    if message_id is None or not MSGMAP_PATH.exists():
        return None
    try:
        return json.loads(MSGMAP_PATH.read_text(encoding="utf-8")).get(str(message_id), {}).get(
            "session"
        )
    except ValueError:
        return None


def _set_active(name: str) -> None:
    _state_init()
    ACTIVE_PATH.write_text(name, encoding="utf-8")


def _get_active() -> str | None:
    if ACTIVE_PATH.exists():
        v = ACTIVE_PATH.read_text(encoding="utf-8").strip()
        return v or None
    return None


def _broker_alive() -> bool:
    if not BROKER_PID.exists():
        return False
    try:
        pid = int(BROKER_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)  # signal 0 = liveness probe
        return True
    except (OSError, ValueError):
        return False


def _broker_enabled() -> bool:
    """Broker mode is the default (plug & play); TELEGRAM_NO_BROKER=1 opts out."""
    return os.environ.get("TELEGRAM_NO_BROKER", "0").strip().lower() not in ("1", "true", "yes")


def _default_session() -> str:
    """Stable per-session name with no user effort: the project directory name.

    One Claude session usually maps to one project dir, so this is stable across
    relaunches and naturally distinct between sessions. Override with --session
    or TELEGRAM_SESSION when two sessions share a directory.
    """
    return (os.environ.get("TELEGRAM_SESSION") or Path.cwd().name or "default").strip()


def _ensure_broker() -> bool:
    """Start a detached broker if none is alive. Idempotent and race-safe.

    The broker is spawned in its own session (``start_new_session``) so it
    outlives the Claude session that triggered it and keeps routing for every
    other session. Returns True once a broker is available.
    """
    if _broker_alive():
        return True
    _state_init()
    lock = STATE_DIR / "broker.starting"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        for _ in range(50):  # another caller is starting it — wait for it
            if _broker_alive():
                return True
            time.sleep(0.1)
        return _broker_alive()
    try:
        logf = open(STATE_DIR / "broker.log", "ab")
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "broker"],
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(50):
            if _broker_alive():
                return True
            time.sleep(0.1)
        return _broker_alive()
    finally:
        lock.unlink(missing_ok=True)


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


def _send_id(token: str, chat: str, text: str, reply_markup: str | None = None):
    """Send a message and return its message_id (for reply→session routing)."""
    params = {"chat_id": chat, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    res = _call(token, "sendMessage", params)
    return (res.get("result") or {}).get("message_id")


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


def cmd_notify(token: str, chat: str, text: str, session: str | None = None) -> int:
    if session:
        # Tag the message and remember its id so a reply routes back here.
        if _broker_enabled():
            _ensure_broker()  # so a reply to this message can be routed
        mid = _send_id(token, chat, f"[{session}] {text}")
        _record_msgmap(mid, session)
    else:
        _send(token, chat, text)
    return 0


def cmd_ask(
    token: str, chat: str, allowed: set[str], question: str, timeout: int, session: str | None = None
) -> int:
    if session:
        # Broker mode: send the (tagged) question, then wait on our inbox — the
        # broker owns getUpdates, so we must not poll it ourselves.
        if not _ensure_broker():
            sys.stderr.write("Could not start the broker.\n")
            return 2
        mid = _send_id(token, chat, f"❓ [{session}] {question}\n\n(Responde a este mensaje.)")
        _record_msgmap(mid, session)
        return _wait_inbox(session, timeout)
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


_STOP_WORDS = {"/stop", "stop", "para", "parar", "detente", "stop listening", "deja de escuchar"}
_STOP_ALL_WORDS = {
    "/stopall", "stop all", "stopall", "para todo", "parar todo", "stop todo",
    "detener todo", "para todas", "parar todas", "stop all sessions",
}


def _save_offset(offset: int) -> None:
    _state_init()
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def _load_offset() -> int | None:
    if OFFSET_PATH.exists():
        try:
            return int(OFFSET_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            return None
    return None


def cmd_listen(
    token: str, chat: str, allowed: set[str], timeout: int, session: str | None = None
) -> int:
    """Wait (silently, no message sent) for the next allow-listed message.

    Designed to run as a BACKGROUND process: the long-poll is pure network, so
    it costs zero model tokens while idle. It exits only when something happens,
    which re-invokes the agent exactly once:
      - prints the message text + returns 0  → a new instruction to act on
      - prints "__STOP__" + returns 4         → user asked to stop the loop
      - returns 3                             → timed out with no message
    """
    if session:
        # Broker mode (default): auto-start the broker if needed, then wait on
        # our local inbox while the broker routes Telegram messages to us.
        if not _ensure_broker():
            sys.stderr.write("Could not start the broker.\n")
            return 2
        _heartbeat(session)
        return _wait_inbox(session, timeout)
    offset = _latest_offset(token)
    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = max(1, min(50, int(deadline - time.time())))
        res = _call(token, "getUpdates", {"timeout": poll, "offset": offset}, timeout=poll + 20)
        for up in res.get("result", []):
            offset = up["update_id"] + 1
            msg = up.get("message") or up.get("edited_message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id")) not in allowed:
                continue  # ignore anyone not on the allowlist
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if text.lower() in _STOP_WORDS:
                _send(token, chat, "🛑 Listener detenido.")
                print("__STOP__")
                return 4
            _send(token, chat, "📥 Recibido, trabajando en ello…")
            print(text)  # the user's instruction → stdout for the agent
            return 0
    sys.stderr.write("No message within timeout.\n")
    return 3


def _wait_inbox(name: str, timeout: int) -> int:
    """Session side of broker mode: heartbeat + wait on our local inbox.

    Same contract as `listen`/`ask`: prints the message on stdout and returns 0,
    prints "__STOP__" and returns 4 on a stop sentinel, returns 3 on timeout.
    Pure local-disk polling — costs zero model tokens while idle.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        _heartbeat(name)
        msg = _dequeue(name)
        if msg is not None:
            if msg == STOP_TOKEN:
                print("__STOP__")
                return 4
            print(msg)
            return 0
        time.sleep(1.0)
    sys.stderr.write("No message within timeout.\n")
    return 3


def cmd_broker(token: str, chat: str, allowed: set[str]) -> int:
    """Single consumer of getUpdates that routes messages to session inboxes.

    Routing precedence for an incoming message:
      1. it is a reply to a tagged message  → that message's session
      2. it starts with "<session>: ..."    → that live session (prefix stripped)
      3. /sessions (or /sesiones)            → show an inline menu of live sessions
      4. a stop word                         → stop the active/target session
      5. otherwise                           → the active session, or the only live
         one; if ambiguous, ask the user to pick (message is not lost-routed).
    """
    if _broker_alive():
        sys.stderr.write("A broker is already running.\n")
        return 1
    _state_init()
    BROKER_PID.write_text(str(os.getpid()), encoding="utf-8")
    # Idle self-shutdown: exit quietly once no session has been live for a while,
    # so the daemon never lingers forever after everyone is done.
    idle_shutdown = float(os.environ.get("TELEGRAM_BROKER_IDLE", "3600"))
    last_active = time.time()
    announced_multi = False
    try:
        # Resume from the saved offset so messages sent while the broker was down
        # are still delivered on restart; skip backlog only on the very first run.
        offset = _load_offset()
        if offset is None:
            offset = _latest_offset(token)
        while True:
            res = _call(
                token,
                "getUpdates",
                {"timeout": 50, "offset": offset, "allowed_updates": '["message","callback_query"]'},
                timeout=70,
            )
            for up in res.get("result", []):
                offset = up["update_id"] + 1
                _handle_update(token, chat, allowed, up)
                _save_offset(offset)
            live = _live_sessions()
            # (a) One-time heads-up the moment a second session shows up.
            if len(live) >= 2 and not announced_multi:
                active = _get_active() or live[-1]
                _send(
                    token,
                    chat,
                    "ℹ️ Ahora hay varias sesiones activas: "
                    + ", ".join(live)
                    + f".\nTus mensajes van a la última con la que hablaste ({active}). "
                    "Para cambiar: responde a un mensaje de la otra sesión, escribe "
                    "'nombre: …' o usa /sessions. Para parar todas: 'para todo'.",
                )
                announced_multi = True
            elif len(live) <= 1:
                announced_multi = False  # re-arm for the next time it grows
            if live:
                last_active = time.time()
            elif time.time() - last_active > idle_shutdown:
                break
    finally:
        BROKER_PID.unlink(missing_ok=True)
    return 0


def _menu(token: str, chat: str) -> None:
    # Show live sessions plus the active one (it may be momentarily down), so the
    # menu isn't empty during a brief listener blip.
    names = list(_live_sessions())
    active = _get_active()
    if active and active not in names:
        names.append(active)
    if not names:
        _send(token, chat, "No hay sesiones todavía.")
        return
    keyboard = [[{"text": n, "callback_data": f"sess:{n}"}] for n in names]
    _send_id(token, chat, "Elige la sesión destino:", json.dumps({"inline_keyboard": keyboard}))


def _handle_update(token: str, chat: str, allowed: set[str], up: dict) -> None:
    cb = up.get("callback_query")
    if cb:
        sender = str((cb.get("from") or {}).get("id"))
        data = cb.get("data") or ""
        if sender in allowed and data.startswith("sess:"):
            name = data[5:]
            _set_active(name)
            _send(token, chat, f"✅ Sesión activa: {name}")
        _call(token, "answerCallbackQuery", {"callback_query_id": cb.get("id")})
        return

    msg = up.get("message") or up.get("edited_message")
    if not msg:
        return
    if str(msg.get("chat", {}).get("id")) not in allowed:
        return  # ignore anyone not on the allowlist
    text = (msg.get("text") or "").strip()
    if not text:
        return

    live = _live_sessions()

    # stop-all → stop every live session at once
    if text.lower() in _STOP_ALL_WORDS:
        for name in live:
            _enqueue(name, STOP_TOKEN)
        _send(token, chat, f"🛑 Detenidas todas las sesiones ({', '.join(live) or '—'}).")
        return

    # 3. menu command
    if text.lower() in ("/sessions", "/sesiones", "/s"):
        _menu(token, chat)
        return

    # 1. reply to a tagged message → its session
    reply = msg.get("reply_to_message") or {}
    target = _msg_session(reply.get("message_id")) if reply else None

    # 2. "<session>: rest" prefix
    if target is None and ":" in text:
        head, rest = text.split(":", 1)
        if head.strip() in live:
            target, text = head.strip(), rest.strip()

    # 4. stop word
    is_stop = text.lower() in _STOP_WORDS

    # 5. fallback: the last session you talked to (even if its listener is
    #    momentarily down — the message queues and is delivered when it relaunches),
    #    else the only live one. Only truly ambiguous when nothing is known.
    if target is None:
        active = _get_active()
        if active:
            target = active
        elif len(live) == 1:
            target = live[0]

    if target is None:
        _send(token, chat, "¿Para qué sesión? Responde a un mensaje suyo, usa 'nombre: ...' o /sessions.")
        return

    if is_stop:
        _enqueue(target, STOP_TOKEN)
        _send(token, chat, f"🛑 [{target}] listener detenido.")
        return

    _set_active(target)
    _enqueue(target, text)
    _send(token, chat, f"📥 [{target}] recibido, trabajando en ello…")


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

    def _pop(args: list[str], flag: str) -> tuple[str | None, list[str]]:
        if flag in args:
            i = args.index(flag)
            return args[i + 1], args[:i] + args[i + 2 :]
        return None, args

    # Broker mode is the default: when enabled and no explicit --session is
    # given, derive a stable one from the project dir so it's plug & play.
    def _eff_session(explicit: str | None) -> str | None:
        if explicit:
            return explicit
        return _default_session() if _broker_enabled() else None

    if cmd == "notify":
        args = argv[1:]
        session, args = _pop(args, "--session")
        if not args:
            sys.stderr.write("Usage: telegram.py notify <text> [--session NAME]\n")
            return 2
        return cmd_notify(token, chat, " ".join(args), _eff_session(session))

    if cmd == "ask":
        args = argv[1:]
        session, args = _pop(args, "--session")
        to, args = _pop(args, "--timeout")
        timeout = int(to) if to else int(os.environ.get("TELEGRAM_ASK_TIMEOUT", "21600"))
        if not args:
            sys.stderr.write("Usage: telegram.py ask <question> [--timeout N] [--session NAME]\n")
            return 2
        return cmd_ask(token, chat, allowed, " ".join(args), timeout, _eff_session(session))

    if cmd == "listen":
        args = argv[1:]
        session, args = _pop(args, "--session")
        to, args = _pop(args, "--timeout")
        timeout = int(to) if to else int(os.environ.get("TELEGRAM_LISTEN_TIMEOUT", "21600"))
        return cmd_listen(token, chat, allowed, timeout, _eff_session(session))

    if cmd == "broker":
        return cmd_broker(token, chat, allowed)

    if cmd == "broker-stop":
        if not _broker_alive():
            print("No broker running.")
            return 0
        import signal

        os.kill(int(BROKER_PID.read_text().strip()), signal.SIGTERM)
        print("Broker stopped.")
        return 0

    if cmd == "sessions":
        print("\n".join(_live_sessions()) or "(no live sessions)")
        return 0

    if cmd == "test":
        return cmd_test(token, chat, allowed)

    sys.stderr.write(f"Unknown command: {cmd}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
