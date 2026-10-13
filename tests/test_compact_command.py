"""V2.1a M4: the manual "Oturumu sıkıştır" command (PreCompact capture first)."""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis import app as app_module
from jarvis.app import JarvisApp
from jarvis.conversation import (ConversationState, PersistedConversation, append_turn, compact_now,
                                 register_precompact_hook)
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import isolated_settings

TEXTS = ("one", "two", "three", "four", "five", "six", "seven")


class CompactCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = isolated_settings(self)
        self.store = SQLiteMemoryStore(self.settings.memory_database)
        self.cid = self.store.create_conversation(self.store.create_project("Alpha").id).id
        self.fired: list[tuple[str, tuple[str, ...]]] = []
        register_precompact_hook(self.store, lambda cid, turns: self.fired.append((cid, tuple(t.text for t in turns))))
        self.addCleanup(register_precompact_hook, self.store, None)
        conversation = PersistedConversation(self.store, self.cid)
        for n, text in enumerate(TEXTS):
            role = "user" if n % 2 == 0 else "assistant"
            conversation.append(role=role, kind=role if role == "user" else "reply", text=text,
                                message=text if role == "user" else "", key=f"k{n}", now=1000 + n)
        self.conversation = conversation

    def raw(self) -> tuple[str, ...]:
        return tuple(t.text for t in self.store.load_conversation_state(self.cid).turns)

    def test_compact_now_equals_append_turn_at_the_threshold_and_fires_the_hook_once(self) -> None:
        expected = ConversationState()
        for n, text in enumerate(TEXTS):
            role = "user" if n % 2 == 0 else "assistant"
            expected = append_turn(expected, role=role, kind=role if role == "user" else "reply", text=text,
                                   message=text if role == "user" else "", key=f"k{n}", now=1000 + n,
                                   raw_turns_max=3)[0]
        self.assertEqual([], self.fired)
        self.assertTrue(compact_now(self.conversation, keep_last=3))
        self.assertEqual(expected, self.store.load_conversation_state(self.cid))
        self.assertEqual([(self.cid, ("one", "two", "three", "four"))], self.fired)
        # Nothing left to compact: no write, no second hook.
        self.assertTrue(compact_now(self.conversation, keep_last=3))
        self.assertEqual(1, len(self.fired))
        with self.assertRaises(ValueError):
            compact_now(self.conversation, keep_last=-1)

    def test_app_compacts_and_returns_the_new_context_usage(self) -> None:
        app = JarvisApp(object(), self.store, self.settings)
        before = app.context_usage_payload(self.cid)
        payload = app.compact_conversation(self.cid, keep_last=2)
        self.assertTrue(payload["ok"])
        self.assertEqual(("six", "seven"), self.raw())
        self.assertEqual(app.context_usage_payload(self.cid), payload)
        self.assertNotEqual(before, payload)
        self.assertEqual(1, len(self.fired))

    def test_app_refuses_while_a_reply_is_streaming(self) -> None:
        app = JarvisApp(object(), self.store, self.settings)
        # A compact asked from inside the reply (the reply is still in flight).
        with mock.patch.object(app, "_submit_text", lambda text, sid, **kw: app.compact_conversation(self.cid)):
            payload = app.submit_text("hi", None, conversation_id=self.cid)
        self.assertFalse(payload["ok"])
        self.assertIn("streaming", payload["error"])
        self.assertEqual(TEXTS, self.raw())
        self.assertEqual([], self.fired)
        self.assertEqual({}, dict(app_module._REPLYING))
        self.assertTrue(app.compact_conversation(self.cid)["ok"])

    def test_app_refuses_unknown_archived_and_invalid(self) -> None:
        app = JarvisApp(object(), self.store, self.settings)
        self.assertFalse(app.compact_conversation("c-" + "0" * 32)["ok"])
        self.assertFalse(app.compact_conversation(self.cid, keep_last=-1)["ok"])
        self.assertFalse(app.compact_conversation(None)["ok"])
        self.store.archive_conversation(self.cid)
        payload = app.compact_conversation(self.cid)
        self.assertFalse(payload["ok"])
        # Archived refuses even when nothing would be compacted.
        self.assertFalse(app.compact_conversation(self.cid, keep_last=100)["ok"])
        self.assertEqual(TEXTS, self.raw())
        self.assertEqual([], self.fired)


if __name__ == "__main__":
    unittest.main()
