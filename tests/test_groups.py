"""Offline behavioral regressions for private compatibility and group transport."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GroupBehavior(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = load("bridge_test", "telegram.py")
        self.g = load("group_test", "group.py")
        self.b.STATE_DIR = Path(self.tmp.name)
        self.b.SESS_DIR = self.b.STATE_DIR / "sessions"
        self.b.INBOX_DIR = self.b.STATE_DIR / "inbox"
        self.b.MSGMAP_PATH = self.b.STATE_DIR / "msgmap.json"
        self.b.ACTIVE_PATH = self.b.STATE_DIR / "active_target"
        self.b.CONFIG_PATH = self.b.STATE_DIR / ".env"
        self.b._state_init()
        self.g.CHAT = "-123456"
        self.g.SESSION = "tg-group-test"
        self.g.LABEL = "TestAgent"
        self.g.register(self.b)
        self.calls = []
        self.b._call = lambda token, method, params=None, **kw: self.calls.append((method, params)) or {"ok": True, "result": {}}

    def update(self, text="hello", chat=-123456, mid=1, edited=False, **extra):
        msg = {"chat": {"id": chat}, "from": {"id": 42, "first_name": "Member"},
               "message_id": mid, "date": 1000 + mid, "text": text, **extra}
        return {"edited_message" if edited else "message": msg}

    def route(self, update):
        self.b._handle_update("fake-token", "777", {"777"}, update)

    def take(self):
        return self.g.take_messages(self.b, 100)

    def test_plain_group_conversation_keeps_sender_without_auto_ack(self):
        self.route(self.update("How was dinner?"))
        self.route(self.update("Please update page two", mid=2))
        messages, stopped = self.take()
        self.assertFalse(stopped)
        self.assertEqual([m["text"] for m in messages], ["How was dinner?", "Please update page two"])
        self.assertEqual(messages[0]["sender"]["id"], 42)
        self.assertEqual(self.calls, [])

    def test_private_routing_and_ack_remain_unchanged(self):
        self.b._heartbeat("legacy")
        self.route(self.update("private task", chat=777))
        self.assertEqual(self.b._dequeue("legacy"), "private task")
        self.assertEqual(self.calls, [("sendMessage", {"chat_id": "777", "text": "📥 [legacy] recibido, trabajando en ello…"})])
        self.assertFalse((self.b.STATE_DIR / "group_history" / "777").exists())

    def test_private_stop_words_remain_unchanged(self):
        self.b._heartbeat("legacy")
        self.route(self.update("para", chat=777))
        self.assertEqual(self.b._dequeue("legacy"), self.b.STOP_TOKEN)

    def test_unregistered_groups_and_strangers_cannot_drive_sessions(self):
        self.b._heartbeat("legacy")
        self.route(self.update("intrusion", chat=-999))
        self.route(self.update("intrusion", chat=999))
        self.assertIsNone(self.b._dequeue("legacy"))
        self.assertEqual(self.take(), ([], False))
        self.assertEqual(self.calls, [])

    def test_group_reply_id_collision_cannot_enter_private_session(self):
        self.b._heartbeat("legacy")
        self.b._record_msgmap(10, "legacy")
        self.route(self.update("group feedback", reply_to_message={"message_id": 10}))
        self.assertIsNone(self.b._dequeue("legacy"))
        self.assertEqual(self.take()[0][0]["text"], "group feedback")

    def test_natural_para_is_context_but_explicit_stop_is_transport(self):
        self.route(self.update("para"))
        self.route(self.update("/stop", mid=2))
        messages, stopped = self.take()
        self.assertEqual(messages[0]["text"], "para")
        self.assertFalse(stopped)
        self.assertEqual(self.take(), ([], True))

    def test_attachments_and_edits_survive_receipt_and_history(self):
        self.route(self.update("draft", document={"file_id": "DOC", "file_name": "draft.pdf"}))
        self.route(self.update("revised", edited=True, document={"file_id": "DOC2"}))
        self.route(self.update("", mid=2, sticker={"file_id": "STICKER"}))
        messages, _ = self.take()
        self.assertEqual(messages[1]["attachments"]["document"]["file_id"], "DOC2")
        self.assertTrue(messages[1]["edited"])
        self.assertEqual(messages[2]["attachments"]["sticker"]["file_id"], "STICKER")
        saved = self.g.history(self.b, 50)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["message"]["text"], "revised")

    def test_poll_and_other_message_content_are_not_dropped(self):
        self.route(self.update("", poll={"id": "p", "question": "Which layout?"}))
        self.assertEqual(self.take()[0][0]["message"]["poll"]["question"], "Which layout?")

    def test_strict_reply_is_optional_and_preserves_other_feedback(self):
        self.route(self.update("unrelated"))
        self.route(self.update("seven", mid=2, reply_to_message={"message_id": 99}))
        strict, _ = self.g.take_messages(self.b, 1, 99)
        self.assertEqual(strict[0]["text"], "seven")
        self.assertEqual(self.take()[0][0]["text"], "unrelated")

    def test_history_is_local_and_empty_before_observation(self):
        self.assertEqual(self.g.history(self.b, 50), [])
        self.route(self.update("observed"))
        self.assertEqual(self.g.history(self.b, 50)[0]["direction"], "incoming")

    def test_corrupt_registration_does_not_break_private_dispatch(self):
        path = self.b.STATE_DIR / "group_routes" / "-123456.json"
        for bad in ([], {"chat_id": "-123456", "session": "../../bad"}, "oops"):
            path.write_text(json.dumps(bad))
            self.assertIsNone(self.b._registered_group_route("-123456"))
        self.b._heartbeat("legacy")
        self.route(self.update("still works", chat=777))
        self.assertEqual(self.b._dequeue("legacy"), "still works")

    def test_only_one_listener_can_claim_an_inbox(self):
        with self.g.inbox_lock(self.b):
            with self.assertRaises(RuntimeError):
                with self.g.inbox_lock(self.b):
                    self.fail("Second listener acquired the queue")
        with self.g.inbox_lock(self.b):
            pass

    def test_group_ask_defaults_to_any_message_not_force_reply(self):
        response = {"message_id": 99, "chat": {"id": -123456}, "date": 1100, "text": "question"}
        with patch.object(self.g, "load_bridge", return_value=self.b), \
             patch.object(self.b, "_load_config", return_value=("fake-token", "777", {"777"})), \
             patch.object(self.b, "_ensure_broker", return_value=True), \
             patch.object(self.g, "send", return_value=response) as sent, \
             patch.object(self.g, "wait_message", return_value=0) as waited, \
             patch("sys.argv", ["group.py", "--chat", "-123456", "ask", "Question?"]):
            self.assertEqual(self.g.main(), 0)
            self.assertFalse(sent.call_args.kwargs["question"])
            self.assertIsNone(waited.call_args.args[2])

    def test_outgoing_messages_are_archived_with_their_files(self):
        response = {"ok": True, "result": {"message_id": 20, "chat": {"id": -123456},
                    "date": 1200, "document": {"file_id": "SENT"}}}
        self.b._call = lambda *a, **kw: response
        self.g.send(self.b, "fake-token", "Draft ready")
        saved = self.g.history(self.b, 50)[0]
        self.assertEqual(saved["direction"], "outgoing")
        self.assertEqual(saved["message"]["document"]["file_id"], "SENT")

    def test_upload_transmits_the_actual_file_and_records_confirmation(self):
        source = Path(self.tmp.name) / "result.pdf"
        source.write_bytes(b"%PDF-test-payload")
        response = {"ok": True, "result": {"message_id": 21, "chat": {"id": -123456},
                    "date": 1201, "document": {"file_id": "UPLOADED"}}}
        with patch.object(self.g.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            result = self.g.upload(self.b, "fake-token", source, "Draft")
        request = opened.call_args.args[0]
        self.assertIn(source.read_bytes(), request.data)
        self.assertIn(b"-123456", request.data)
        self.assertIn(b"[TestAgent] Draft", request.data)
        self.assertEqual(result["document"]["file_id"], "UPLOADED")
        self.assertEqual(self.g.history(self.b, 50)[0]["message"]["message_id"], 21)

    def test_download_preserves_bytes_and_does_not_overwrite(self):
        self.b._call = lambda *a, **kw: {"ok": True, "result": {"file_path": "documents/test.pdf", "file_size": 3}}
        dest = Path(self.tmp.name) / "received.pdf"
        with patch.object(self.g.urllib.request, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(b"PDF")):
            self.g.download(self.b, "fake-token", "FILE", dest)
            with self.assertRaises(FileExistsError):
                self.g.download(self.b, "fake-token", "FILE", dest)
        self.assertEqual(dest.read_bytes(), b"PDF")

    def test_migration_service_preserves_group_queue_and_history(self):
        self.route(self.update("before migration"))
        self.route(self.update("", mid=2, migrate_to_chat_id=-100999))
        self.route(self.update("after migration", chat=-100999, mid=1))
        self.assertEqual(self.g.resolve_chat(self.b, "-123456"), "-100999")
        messages, _ = self.take()
        self.assertEqual(messages[-1]["text"], "after migration")
        self.g.CHAT = "-100999"
        archived = self.g.history(self.b, 50)
        self.assertEqual({i["message"]["chat"]["id"] for i in archived}, {-123456, -100999})

    def test_reverse_migration_service_registers_new_group(self):
        self.route(self.update("", chat=-100999, migrate_from_chat_id=-123456))
        route = self.b._registered_group_route("-100999")
        self.assertEqual(route["session"], self.g.SESSION)
        self.assertEqual(route["migrated_from"], ["-123456"])

    def test_api_migration_retries_only_failed_request_at_new_group(self):
        calls = []
        def transport(token, method, params):
            calls.append(dict(params))
            if len(calls) == 1:
                raise self.b.TelegramAPIError(400, json.dumps({"parameters": {"migrate_to_chat_id": -100999}}))
            return {"ok": True, "result": {"chat": {"id": -100999}, "message_id": 3}}
        self.b._call = transport
        result = self.g.api(self.b, "fake-token", "sendMessage", {"chat_id": "-123456", "text": "hello"})
        self.assertEqual(result["chat"]["id"], -100999)
        self.assertEqual([i["chat_id"] for i in calls], ["-123456", "-100999"])
        self.assertEqual(self.g.CHAT, "-100999")

    def test_nonmigration_api_errors_keep_legacy_exit_and_are_not_retried(self):
        error = self.b.TelegramAPIError(403, '{"description":"Forbidden"}')
        self.assertIsInstance(error, SystemExit)
        self.assertEqual(str(error), 'Telegram API error 403: {"description":"Forbidden"}')
        with patch.object(self.b, "_call", side_effect=error) as call:
            with self.assertRaises(self.b.TelegramAPIError):
                self.g.api(self.b, "fake-token", "sendMessage", {"chat_id": self.g.CHAT})
            self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
