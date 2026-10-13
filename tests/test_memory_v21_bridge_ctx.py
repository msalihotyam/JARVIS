"""V2.1a M4 bridge: the open conversation's context use and its all-projects
memory toggle. Both are the app's answers over a throwaway TEMP database,
re-read after a turn lands and on a conversation switch; the bridge adds no
number or default of its own. No model or connector is called (a scripted
chat model answers)."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.test_ui import UiTestCase, proposal


class ContextUsageBridgeTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ui = self.bridge(*(proposal(kind="chat", reply=f"Yanıt {n}") for n in range(4)))
        self.app = self.factory(self.store.load().settings)   # the same TEMP database, for checks
        self.usage_changes: list[None] = []
        self.toggle_changes: list[None] = []
        self.ui.contextUsageChanged.connect(lambda: self.usage_changes.append(None))
        self.ui.globalMemoryChanged.connect(lambda: self.toggle_changes.append(None))

    def expected(self, conversation_id: str) -> dict:
        payload = self.app.context_usage_payload(conversation_id)
        self.assertTrue(payload["ok"])
        return payload

    def assertUsageIs(self, payload: dict) -> None:
        usage = self.ui.contextUsage
        self.assertEqual(payload["used_tokens"], usage["used"])
        self.assertEqual(payload["max_tokens"], usage["max"])
        self.assertEqual(payload["ratio"], usage["ratio"])
        self.assertEqual(payload["turns_until_autocompact"], usage["turnsUntilAutoCompact"])
        self.assertIn(usage["state"], ("normal", "high", "full"))

    def test_startup_reads_the_open_conversation_and_toggle_off(self) -> None:
        self.assertTrue(self.ui.conversationId)
        self.assertUsageIs(self.expected(self.ui.conversationId))
        self.assertIs(self.ui.globalMemoryEnabled, False)

    def test_each_turn_append_refreshes_the_usage(self) -> None:
        self.assertTrue(self.ui.submitText("Merhaba"))
        conversation = self.ui.conversationId
        self.assertTrue(conversation)
        first = self.expected(conversation)
        self.assertUsageIs(first)
        changes = len(self.usage_changes)
        self.assertTrue(self.ui.submitText("Bugün hava nasıl olacak, uzun uzun anlatır mısın?"))
        self.assertEqual(conversation, self.ui.conversationId)   # same conversation: only the append re-read it
        second = self.expected(conversation)
        self.assertGreater(second["used_tokens"], first["used_tokens"])
        self.assertUsageIs(second)
        self.assertGreater(len(self.usage_changes), changes)

    def test_a_switch_reads_the_opened_conversation(self) -> None:
        self.assertTrue(self.ui.submitText("Birinci konuşma, biraz daha uzun bir ilk mesaj."))
        first = self.ui.conversationId
        self.ui.setGlobalMemoryEnabled(True)
        self.assertTrue(self.ui.newConversation())
        self.assertTrue(self.ui.submitText("İkinci"))
        second = self.ui.conversationId
        self.assertNotEqual(first, second)
        self.assertUsageIs(self.expected(second))
        self.assertIs(self.ui.globalMemoryEnabled, False)   # the toggle is per conversation
        self.assertTrue(self.ui.openConversation(first))
        self.assertUsageIs(self.expected(first))
        self.assertIs(self.ui.globalMemoryEnabled, True)
        self.assertTrue(self.ui.openConversation(second))
        self.assertIs(self.ui.globalMemoryEnabled, False)

    def test_toggle_round_trip_goes_through_the_app(self) -> None:
        self.assertTrue(self.ui.submitText("Merhaba"))
        conversation = self.ui.conversationId
        self.assertIs(self.ui.globalMemoryEnabled, False)
        self.ui.setGlobalMemoryEnabled(True)
        self.assertIs(self.ui.globalMemoryEnabled, True)
        self.assertIs(self.app.global_memory_toggle_payload(conversation)["enabled"], True)
        self.ui.setGlobalMemoryEnabled(False)
        self.assertIs(self.ui.globalMemoryEnabled, False)
        self.assertIs(self.app.global_memory_toggle_payload(conversation)["enabled"], False)
        self.assertEqual(2, len(self.toggle_changes))

    def test_a_refused_toggle_keeps_the_value_and_says_why(self) -> None:
        self.assertTrue(self.ui.submitText("Merhaba"))
        feed = len(self.ui.feed)
        refusal = {"ok": False, "error": "Unknown conversation: x."}
        with mock.patch.object(self.ui.backend, "set_global_memory_toggle", return_value=refusal):
            self.ui.setGlobalMemoryEnabled(True)
        self.assertIs(self.ui.globalMemoryEnabled, False)
        self.assertEqual(1, len(self.toggle_changes))   # the switch is told to snap back
        self.assertGreater(len(self.ui.feed), feed)
        self.assertEqual("error", self.ui.feed[-1]["kind"])
        self.assertIs(self.app.global_memory_toggle_payload(self.ui.conversationId)["enabled"], False)

    def test_unknown_max_reads_state_unknown(self) -> None:
        self.assertTrue(self.ui.submitText("Merhaba"))
        conversation = self.ui.conversationId
        unknown = {"ok": True, "conversation_id": conversation, "used_tokens": 12, "max_tokens": None,
                   "ratio": None, "turns_until_autocompact": None, "label_key": "bilinmiyor"}
        with mock.patch.object(self.ui.backend, "context_usage", return_value=unknown):
            self.ui._refresh_context_usage()
        self.assertEqual({"used": 12, "max": None, "ratio": None, "turnsUntilAutoCompact": None,
                          "state": "unknown"}, self.ui.contextUsage)
        for answer in ({"ok": False, "error": "Unknown conversation: x."},
                       {**self.expected(conversation), "conversation_id": "another"}):
            with mock.patch.object(self.ui.backend, "context_usage", return_value=answer):
                self.ui._refresh_context_usage()
            self.assertEqual("unknown", self.ui.contextUsage["state"])
            self.assertIsNone(self.ui.contextUsage["max"])


if __name__ == "__main__":
    unittest.main()
