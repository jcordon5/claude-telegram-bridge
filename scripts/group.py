#!/usr/bin/env python3
"""Group transport sharing the installed telegram-bridge broker (stdlib only)."""
import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import mimetypes
import os
import re
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

CHAT = None
SESSION = None
LABEL = "Agent"
BRIDGE = Path(__file__).resolve().with_name("telegram.py")


def load_bridge():
    spec = importlib.util.spec_from_file_location("shared_telegram_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "_route_registered_group"):
        raise RuntimeError("The shared broker is missing its group-routing extension.")
    return module


def setting(bridge, name, default=None):
    if os.environ.get(name):
        return os.environ[name]
    if bridge.CONFIG_PATH.exists():
        for raw in bridge.CONFIG_PATH.read_text().splitlines():
            if raw.lstrip().startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key.strip() == name and value.strip():
                return value.strip().strip("\"").strip("'")
    return default


def register(bridge, heartbeat=False):
    root = bridge.STATE_DIR / "group_routes"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = root / f"{CHAT}.json"
    old = {}
    if dest.exists():
        old = json.loads(dest.read_text())
    data = {**old, "chat_id": CHAT, "session": SESSION, "label": LABEL,
            "heartbeat": time.time() if heartbeat else old.get("heartbeat", 0)}
    tmp = root / f".{CHAT}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, dest)


def resolve_chat(bridge, chat):
    seen = set()
    while True:
        if not re.fullmatch(r"-\d+", chat) or chat in seen:
            raise ValueError("Invalid or cyclic group migration")
        seen.add(chat)
        route = bridge._registered_group_route(chat) or {}
        if not route.get("migrated_to"):
            return chat
        chat = str(route["migrated_to"])


def follow_migration(bridge, error):
    global CHAT
    target = error.parameters.get("migrate_to_chat_id")
    if error.status != 400 or not target:
        return False
    bridge._register_group_migration(CHAT, str(target))
    CHAT = str(target)
    register(bridge)
    return True


def api(bridge, token, method, params):
    try:
        result = bridge._call(token, method, params)
    except bridge.TelegramAPIError as exc:
        if str(params.get("chat_id")) != CHAT or not follow_migration(bridge, exc):
            raise
        # A migration error is a failed request, so retrying at the new chat
        # cannot duplicate a successfully delivered message.
        result = bridge._call(token, method, {**params, "chat_id": CHAT})
    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm the operation.")
    return result["result"]


def send(bridge, token, text, question=False):
    params = {"chat_id": CHAT, "text": f"[{LABEL}] " + text}
    if question:
        params["reply_markup"] = json.dumps({"force_reply": True, "selective": False})
    result = api(bridge, token, "sendMessage", params)
    bridge._journal_group_message(result, "outgoing")
    return result


def upload(bridge, token, path, caption, migrated=False):
    path = path.resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
        raise RuntimeError("Upload requires a regular file up to 50 MB.")
    boundary = "TelegramGroup" + uuid.uuid4().hex
    chunks = []
    fields = {"chat_id": CHAT, "caption": f"[{LABEL}] " + caption if caption else f"[{LABEL}]"}
    for key, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
    safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n').encode())
    chunks.extend([path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
    req = urllib.request.Request(bridge.API.format(token=token, method="sendDocument"),
                                 data=b"".join(chunks),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        error = bridge.TelegramAPIError(exc.code, exc.read().decode("utf-8", "replace"))
        if not migrated and follow_migration(bridge, error):
            return upload(bridge, token, path, caption, migrated=True)
        raise error from None
    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm the document upload.")
    bridge._journal_group_message(result["result"], "outgoing")
    return result["result"]


def download(bridge, token, file_id, dest):
    item = api(bridge, token, "getFile", {"file_id": file_id})
    if item.get("file_size", 0) > 20 * 1024 * 1024:
        raise RuntimeError("The Bot API download exceeds this helper's 20 MB limit.")
    remote = item.get("file_path", "")
    if not remote or ".." in Path(remote).parts or remote.startswith("/"):
        raise RuntimeError("Unexpected file path from Telegram.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting an existing local deliverable.
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{remote}", timeout=90) as response:
        payload = response.read(20 * 1024 * 1024 + 1)
    if len(payload) > 20 * 1024 * 1024:
        raise RuntimeError("File exceeds the download limit.")
    with dest.open("xb") as output:
        output.write(payload)
    return {"saved": str(dest.resolve()), "bytes": len(payload)}


@contextmanager
def inbox_lock(bridge):
    box = bridge.INBOX_DIR / SESSION
    box.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (box / ".listener.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another listener already owns this group inbox.")
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def take_messages(bridge, limit=1, question_id=None):
    messages = []
    box = bridge.INBOX_DIR / SESSION
    for path in sorted(box.glob("*.txt")):
        raw = path.read_text()
        if raw == bridge.STOP_TOKEN:
            if messages:
                break  # deliver earlier messages first, preserving the stop
            path.unlink()
            return [], True
        msg = json.loads(raw)
        if question_id is not None and msg.get("reply_to_message_id") != question_id:
            continue
        messages.append(msg)
        path.unlink()
        if len(messages) >= limit:
            break
    return messages, False


def history(bridge, limit):
    items = []
    chats = {CHAT}
    for path in (bridge.STATE_DIR / "group_routes").glob("-*.json"):
        try:
            if resolve_chat(bridge, path.stem) == CHAT:
                chats.add(path.stem)
        except ValueError:
            continue
    for chat in chats:
        for path in (bridge.STATE_DIR / "group_history" / chat).glob("*.json"):
            item = json.loads(path.read_text())
            items.append(item)
    items.sort(key=lambda item: (item["message"].get("date", 0), item.get("observed_at", 0), item["message"]["message_id"]))
    return items[-limit:]


def wait_message(bridge, timeout, question_id=None, batch=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        register(bridge, heartbeat=True)
        # Also recovers from an idle broker shutdown or an exited process.
        if not bridge._ensure_broker():
            raise RuntimeError("Could not start the shared broker.")
        messages, stopped = take_messages(bridge, 100 if batch else 1, question_id)
        if stopped:
            print("__STOP__", flush=True)
            return 4
        if messages:
            print(json.dumps(messages if batch else messages[0], ensure_ascii=False), flush=True)
            return 0
        time.sleep(1)
    print("No message within timeout.", file=sys.stderr)
    return 3


def main():
    global CHAT, SESSION, LABEL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", help="Group ID; defaults to TELEGRAM_GROUP_CHAT_ID")
    parser.add_argument("--session", help="Group queue name (one active route per group)")
    parser.add_argument("--label", help="Outbound name, e.g. Claude or ChatGPT")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("status")
    subs.add_parser("register")
    sub = subs.add_parser("history")
    sub.add_argument("--limit", type=int, default=50)
    subs.add_parser("drain")
    for command in ("notify", "ask"):
        sub = subs.add_parser(command)
        sub.add_argument("text")
        if command == "ask":
            sub.add_argument("--timeout", type=int)
            sub.add_argument("--strict-reply", action="store_true", help="Opt in to exact reply-to matching")
    sub = subs.add_parser("listen")
    sub.add_argument("--timeout", type=int)
    sub.add_argument("--reply-to", type=int)
    sub.add_argument("--batch", action="store_true")
    sub = subs.add_parser("send-file")
    sub.add_argument("path", type=Path)
    sub.add_argument("--caption", default="")
    sub = subs.add_parser("download")
    sub.add_argument("file_id")
    sub.add_argument("destination", type=Path)
    args = parser.parse_args()
    bridge = load_bridge()
    if args.command in ("ask", "listen"):
        key = "TELEGRAM_ASK_TIMEOUT" if args.command == "ask" else "TELEGRAM_LISTEN_TIMEOUT"
        args.timeout = args.timeout if args.timeout is not None else int(setting(bridge, key, "21600"))
        if args.timeout <= 0:
            raise ValueError("Timeout must be positive.")
    CHAT = args.chat or setting(bridge, "TELEGRAM_GROUP_CHAT_ID")
    if not CHAT or not re.fullmatch(r"-\d+", CHAT):
        raise ValueError("Set --chat or TELEGRAM_GROUP_CHAT_ID to a negative group ID.")
    CHAT = resolve_chat(bridge, CHAT)
    route = bridge._registered_group_route(CHAT) or {}
    SESSION = args.session or setting(bridge, "TELEGRAM_GROUP_SESSION", route.get("session", f"tg-group-{CHAT[1:]}"))
    if not SESSION.startswith("tg-group-"):
        SESSION = "tg-group-" + SESSION
    if not re.fullmatch(r"tg-group-[a-zA-Z0-9_-]{1,96}", SESSION):
        raise ValueError("Invalid group session name.")
    LABEL = args.label or setting(bridge, "TELEGRAM_AGENT_NAME", "Agent")
    if args.command == "history":
        if not 1 <= args.limit <= 1000:
            raise ValueError("History limit must be 1..1000.")
        print(json.dumps(history(bridge, args.limit), ensure_ascii=False))
        return 0
    if args.command == "drain":
        with inbox_lock(bridge):
            messages, stopped = take_messages(bridge, 100)
        print("__STOP__" if stopped else json.dumps(messages, ensure_ascii=False))
        return 4 if stopped else 0
    token, private_chat, _ = bridge._load_config()
    if not token:
        raise RuntimeError("No token configured in the existing bridge.")
    if args.command == "status":
        me = api(bridge, token, "getMe", {})
        chat = api(bridge, token, "getChat", {"chat_id": CHAT})
        member = api(bridge, token, "getChatMember", {"chat_id": CHAT, "user_id": me["id"]})
        full_access = member.get("status") in ("creator", "administrator") or bool(me.get("can_read_all_group_messages"))
        print(json.dumps({"bot": me["username"], "group": chat["id"],
                          "title": chat.get("title"), "private_chat_preserved": private_chat,
                          "privacy_mode": not me.get("can_read_all_group_messages", False),
                          "group_role": member.get("status"),
                          "receives_all_human_messages": full_access,
                          "history_scope": "locally_observed_messages_only",
                          "broker_alive": bridge._broker_alive(),
                          "group_registered": bool(bridge._registered_group_route(CHAT))}, ensure_ascii=False))
        return 0
    register(bridge)
    if args.command == "register":
        print(json.dumps({"registered_chat": CHAT, "session": SESSION}))
        return 0
    if args.command in ("notify", "send-file", "ask", "listen") and not bridge._ensure_broker():
        raise RuntimeError("Could not start the shared broker.")
    if args.command == "download":
        result = download(bridge, token, args.file_id, args.destination)
    elif args.command == "send-file":
        result = upload(bridge, token, args.path, args.caption)
    elif args.command == "notify":
        result = send(bridge, token, args.text)
    elif args.command == "ask":
        with inbox_lock(bridge):
            question = send(bridge, token, args.text, question=args.strict_reply)
            return wait_message(bridge, args.timeout, question["message_id"] if args.strict_reply else None)
    else:
        with inbox_lock(bridge):
            return wait_message(bridge, args.timeout, args.reply_to, args.batch)
    print(json.dumps({"ok": True, "message_id": result.get("message_id"),
                      "file_id": result.get("document", {}).get("file_id"),
                      "chat_id": result.get("chat", {}).get("id"), **(
                          result if args.command == "download" else {})}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(f"Telegram HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}", file=sys.stderr)
        raise SystemExit(2)
    except (OSError, RuntimeError, ValueError) as exc:
        # URLs can contain the token: do not print raw network exceptions.
        detail = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "Check network and file paths."
        print(f"Bridge operation failed ({type(exc).__name__}): {detail}", file=sys.stderr)
        raise SystemExit(2)
