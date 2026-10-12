"""DFREPLY (Salih 2026-10-12): long replies, cut-short replies kept, "siz" and "mesaj".

1. A General Chat reply may be up to ``REPLY_MAX_CHARS`` (50,000); the model is told.
2. A reply over it - or any reply cut short (cancel, timeout, error) - keeps the text
   the person saw as JARVIS's reply, marked with why, under a notice (not an error
   card). Nothing of a cut or partial proposal is ever executed.
3. That reply is saved in its conversation - also when the run ends while another
   one is open - as an ordinary reply turn, so the schema-10 rollback reads it.
4. The prompt fixes "siz" and the meaning of "mesaj"; the Gmail catalog says e-mail.

Fake adapters and temporary databases only: no model, network or connector is
reached and the live database is never opened.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess  # nosec B404 - git show of a tracked file, fixed argv
import sys
import threading
import time
from pathlib import Path

from jarvis.connectors.claude_code import TIMEOUT_MESSAGE
from jarvis.contracts import MODEL_ANSWER_MAX_CHARS, ErrorCode, StructuredError, build_model_answer
from jarvis.conversation import REPLY_MAX_CHARS, append_turn, ConversationState, cut_reply, split_cut
from jarvis.intent import (
    CHAT_REPLY_MAX_CHARS,
    CHAT_REPLY_TOO_LONG,
    INTENT_SYSTEM_PROMPT,
    NL_CAPABILITIES,
    IntentStatus,
    too_long_reply,
)
from jarvis.ui.present import CUT_TEXT, cut_entries, group_sections, text_entries
from jarvis.ui.i18n import render
from tests.test_intent import IntentTestCase, ScriptedModel
from tests.test_ui import StreamingModel, UiTestCase, pump, window_ready

ROOT = Path(__file__).resolve().parent.parent
#: The schema-10 release a rollback returns to (1.0.0-c3df707).
ROLLBACK = "c3df707"
PATIENCE = 60


def lines(chars: int) -> str:
    """A long plain answer of exactly ``chars`` characters, many short lines."""
    text = "".join(f"{n}. {n}² = {n * n} — bu satır uzun bir yanıtın bir parçasıdır.\n" for n in range(1, chars))
    return text[:chars]


def chat(reply: str) -> str:
    """A chat proposal as a model writes it: raw UTF-8, non-ASCII letters not escaped."""
    return json.dumps({"kind": "chat", "reply": reply}, ensure_ascii=False)


def acted_reply(reply: str, padding: int) -> str:
    """A chat-shaped answer that then tries to act - truncated by its own size."""
    return json.dumps({"kind": "chat", "reply": reply, "capability": "gmail_send", "destination": "gmail/a@example.com",
                       "arguments": {"to": ["b@example.com"], "subject": "x", "body": "y" * padding}}, ensure_ascii=False)


class CapTests(IntentTestCase):
    """Item 1 and 2 at the understanding boundary (``JarvisApp.submit_text``)."""

    def test_the_caps(self) -> None:
        self.assertEqual((50_000, 50_000, 64_000), (REPLY_MAX_CHARS, CHAT_REPLY_MAX_CHARS, MODEL_ANSWER_MAX_CHARS))
        envelope = chat(lines(REPLY_MAX_CHARS))
        self.assertLess(len(envelope), MODEL_ANSWER_MAX_CHARS)          # a full reply plus its envelope fits
        self.assertFalse(build_model_answer(envelope).truncated)

    def test_a_30000_character_reply_is_an_ordinary_answer(self) -> None:
        reply = lines(30_000)
        payload = self.build(ScriptedModel(chat(reply))).submit_text("Uzun yaz.").payload
        self.assertNothingHappened(payload, IntentStatus.CONVERSATION)
        self.assertEqual((reply, None), (payload["intent"]["reply"], payload["intent"]["reason"]))

    def test_a_60000_character_reply_is_kept_up_to_the_cap_and_marked(self) -> None:
        reply = lines(60_000)
        payload = self.build(ScriptedModel(chat(reply))).submit_text("Uzun yaz.").payload
        self.assertNothingHappened(payload, IntentStatus.CONVERSATION)
        self.assertEqual((reply[:REPLY_MAX_CHARS], CHAT_REPLY_TOO_LONG),
                         (payload["intent"]["reply"], payload["intent"]["reason"]))
        self.assertIn("about 50,000 characters", payload["intent"]["message"])

    def test_an_answer_past_the_json_cap_is_kept_up_to_the_cap(self) -> None:
        reply = lines(70_000)
        self.assertTrue(build_model_answer(chat(reply)).truncated)
        payload = self.build(ScriptedModel(chat(reply))).submit_text("Uzun yaz.").payload
        self.assertNothingHappened(payload, IntentStatus.CONVERSATION)
        self.assertEqual((reply[:REPLY_MAX_CHARS], CHAT_REPLY_TOO_LONG),
                         (payload["intent"]["reply"], payload["intent"]["reason"]))

    def test_a_truncated_proposal_with_an_action_drops_the_action_and_keeps_the_text(self) -> None:
        answer = acted_reply("Taslak hazır, gönderiyorum.", 70_000)
        self.assertTrue(build_model_answer(answer).truncated)
        app = self.build(ScriptedModel(answer), gmail=("a@example.com",))
        payload = app.submit_text("Ali'ye mesaj at.").payload
        self.assertNothingHappened(payload, IntentStatus.CONVERSATION)        # no task, approval or grant; nothing ran
        self.assertEqual(0, self.gmail.execute_calls)
        self.assertEqual(("Taslak hazır, gönderiyorum.", CHAT_REPLY_TOO_LONG, None, None),
                         (payload["intent"]["reply"], payload["intent"]["reason"],
                          payload["intent"]["capability"], payload["intent"]["destination"]))

    def test_a_cut_answer_with_no_visible_reply_is_still_refused(self) -> None:
        task = json.dumps({"kind": "task", "objective": "x" * 70_000, "capability": "code_analysis",
                           "destination": "", "arguments": {}})
        payload = self.build(ScriptedModel(task)).submit_text("İncele.").payload
        self.assertNothingHappened(payload, IntentStatus.MALFORMED_MODEL_OUTPUT)
        self.assertIsNone(payload["intent"]["reply"])

    def test_only_the_reply_ever_leaves_a_cut_answer(self) -> None:
        self.assertIsNone(too_long_reply(chat("kısa"), False))          # whole: parsed as before
        self.assertEqual("ok", too_long_reply(acted_reply("ok", 10), True))                   # cut: the reply only
        self.assertIsNone(too_long_reply('{"capability":"gmail_send","kind":"chat","reply":"x"}', True))
        self.assertIsNone(too_long_reply(chat("a\u0000b" + "c" * 60_000), False))
        self.assertIsNone(too_long_reply(chat(" " * 60_000), False))

    def test_a_separately_answered_group_is_cut_the_same_way(self) -> None:
        from jarvis.app import JarvisApp
        from jarvis.contracts import MODEL_ANSWER_RESULT_KEY, OutcomeStatus, ToolResult
        result = ToolResult(outcome=OutcomeStatus.SUCCESS, summary="proposal",
                            data={MODEL_ANSWER_RESULT_KEY: build_model_answer(acted_reply("A için yanıt", 70_000))})
        section = JarvisApp._group_section("A", result, False)
        self.assertEqual({"label": "A", "status": "answered", "kind": "chat", "text": "A için yanıt", "cut": "too_long"},
                         section)
        self.assertIn("Cut short: the reply was longer", render(group_sections([section])))


class WordsTests(IntentTestCase):
    """Item 4: the prompt and the catalog, pinned as text (no call is made)."""

    def test_the_prompt_fixes_siz_mesaj_several_messages_and_the_length(self) -> None:
        for text in (
            "in Turkish ALWAYS address the user formally as 'siz'",
            "never 'sen'",
            "keep it the same within a reply and across replies",
            "'mesaj' / 'message' means a chat message in this conversation",
            "It means an e-mail only when the user says e-posta, e-mail, mail or Gmail or gives an e-mail address",
            "write them all inside ONE chat reply (for example numbered 1., 2., 3.) - never explain a format limit instead",
            "a chat reply may be up to 50,000 characters",
        ):
            with self.subTest(text=text):
                self.assertIn(text, INTENT_SYSTEM_PROMPT)

    def test_the_gmail_catalog_says_e_mail(self) -> None:
        self.assertEqual("Send ONE e-mail (Gmail) to the given addresses - never a chat message. "
                         "A side effect: the user still approves it.", NL_CAPABILITIES["gmail_send"].description)
        for name, hint in NL_CAPABILITIES.items():
            if name.startswith("gmail_"):
                with self.subTest(name=name):
                    self.assertIn("e-mail", hint.description)


class MarkerTests(UiTestCase):
    """Item 3's storage form: an ordinary reply turn, readable by the rollback release."""

    def test_the_marker_is_a_last_line_of_an_ordinary_reply(self) -> None:
        stored = cut_reply("x" * 60_000, "too_long")
        self.assertEqual(("x" * REPLY_MAX_CHARS, "too_long"), split_cut(stored))
        self.assertEqual(("Tam bir yanıt.", ""), split_cut("Tam bir yanıt."))
        state, turn = append_turn(ConversationState(), role="assistant", kind="reply", text=stored)
        self.assertEqual((stored, "reply"), (turn.text, turn.kind))                             # kept whole, not clipped
        with self.assertRaises(ValueError):
            cut_reply("x", "unknown")


class ReplyFlowTests(UiTestCase):
    """Items 2 and 3 in the window: shown, saved, reopened."""

    def streaming(self, *answers, previews=(), error=None, during=None):
        self.model = StreamingModel(previews=previews, error=error, during=during)
        return self.bridge(*answers)

    def table_count(self, table: str) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608 - constant table names

    def turns(self, bridge, conversation_id: str | None = None) -> list[tuple[str, str]]:
        store = bridge._conversation_db()
        state = store.load_conversation_state(conversation_id or bridge.conversationId)
        return [(t.kind, t.text) for t in state.turns]

    def test_a_30000_character_reply_is_shown_and_saved_with_no_refusal(self) -> None:
        reply = lines(30_000)
        bridge = self.streaming(chat(reply), previews=(reply[:900], reply))
        self.assertTrue(bridge.submitText("Uzun yaz."))
        self.assertEqual(["user", "jarvis"], self.kinds(bridge))
        self.assertEqual((reply, True), (bridge.feed[-1]["text"], bridge.feed[-1].get("chat")))
        self.assertNotIn("interrupted", bridge.feed[-1])
        self.assertEqual("idle", bridge.coreState)
        self.assertEqual([("user", "Uzun yaz."), ("reply", reply)], self.turns(bridge))

    def test_a_60000_character_reply_is_kept_with_a_notice_never_an_error(self) -> None:
        reply = lines(60_000)
        bridge = self.streaming(chat(reply), previews=(reply[:900], reply))
        self.assertTrue(bridge.submitText("Uzun yaz."))
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        shown, notice = bridge.feed[1], bridge.feed[2]
        self.assertEqual((reply[:REPLY_MAX_CHARS], True, "too_long", True),
                         (shown["text"], shown["chat"], shown["cut"], shown["interrupted"]))
        self.assertEqual(render(("Present", CUT_TEXT["too_long"], ())), notice["text"])
        self.assertNotIn("error", self.kinds(bridge))
        self.assertEqual("idle", bridge.coreState)                                               # the lamp is not "Hata"
        self.assertEqual((0, 0), (self.execute_calls(), self.table_count("pending_approvals")))
        self.assertEqual([("user", "Uzun yaz."), ("reply", cut_reply(reply, "too_long"))], self.turns(bridge))

    def test_a_cut_proposal_that_tried_to_act_keeps_only_its_text(self) -> None:
        self.gmail_accounts = ("a@example.com",)
        answer = acted_reply("Taslak hazır.", 70_000)
        bridge = self.streaming(answer, previews=("Taslak", "Taslak hazır."))
        self.assertTrue(bridge.submitText("Ali'ye mesaj at."))
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        self.assertEqual(("Taslak hazır.", "too_long"), (bridge.feed[1]["text"], bridge.feed[1]["cut"]))
        self.assertFalse(bridge.approvalPending)
        self.assertEqual(0, self.gmail.execute_calls)
        self.assertEqual((0, 0), (self.execute_calls(), self.table_count("tasks")))

    def test_cancel_mid_stream_keeps_what_was_seen_marked_cancelled(self) -> None:
        holder: dict = {}
        bridge = self.streaming(chat("Kısmi yanıt ve devamı"),
                                previews=("Kısmi", "Kısmi yanıt"),
                                during=lambda text: holder["bridge"].cancelText() if text == "Kısmi yanıt" else None)
        holder["bridge"] = bridge
        self.assertTrue(bridge.submitText("Anlat."))
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        self.assertEqual(("Kısmi yanıt", "cancelled"), (bridge.feed[1]["text"], bridge.feed[1]["cut"]))
        self.assertEqual(render(("Present", CUT_TEXT["cancelled"], ())), bridge.feed[2]["text"])
        self.assertEqual([("user", "Anlat."), ("reply", cut_reply("Kısmi yanıt", "cancelled"))], self.turns(bridge))

    def test_a_timeout_and_an_error_mid_stream_are_kept_with_their_reason(self) -> None:
        for message, reason in ((TIMEOUT_MESSAGE, "timed_out"), ("stopped", "error")):
            with self.subTest(reason=reason):
                bridge = self.streaming(previews=("Yarım kalan",), error=StructuredError(ErrorCode.CONNECTOR_FAILED, message))
                self.assertTrue(bridge.submitText("Anlat."))
                self.assertEqual(["jarvis", "notice"], self.kinds(bridge)[-2:])
                self.assertEqual(("Yarım kalan", reason), (bridge.feed[-2]["text"], bridge.feed[-2]["cut"]))
                self.assertEqual(("reply", cut_reply("Yarım kalan", reason)), self.turns(bridge)[-1])

    def test_reopened_the_reply_and_its_notice_are_there(self) -> None:
        reply = lines(60_000)
        bridge = self.streaming(chat(reply), previews=(reply,))
        store = bridge._conversation_db()
        a = bridge.conversationId
        self.assertTrue(bridge.submitText("Uzun yaz."))
        b = store.create_conversation().id
        self.assertTrue(bridge.openConversation(b))
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        self.assertEqual((reply[:REPLY_MAX_CHARS], "too_long", True, True),
                         (bridge.feed[1]["text"], bridge.feed[1]["cut"], bridge.feed[1]["interrupted"], bridge.feed[1]["chat"]))
        self.assertEqual(render(("Present", CUT_TEXT["too_long"], ())), bridge.feed[2]["text"])

    def test_the_rollback_release_reads_the_stored_conversation(self) -> None:
        reply = lines(60_000)
        bridge = self.streaming(chat(reply), previews=(reply,))
        self.assertTrue(bridge.submitText("Uzun yaz."))
        old_store, old = rollback_modules(self.root)
        # The rollback release's read path: the row's two columns, then its decode_state.
        self.assertIn('return decode_state(row["state_json"], row["integrity"])', old_store)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            row = db.execute("SELECT state_json, integrity FROM conversations WHERE id = ?",  # nosec B608 - constant SQL
                             (bridge.conversationId,)).fetchone()
        state = old.decode_state(row[0], row[1])
        self.assertEqual([("user", "user"), ("assistant", "reply")], [(t.role, t.kind) for t in state.turns])
        self.assertEqual(cut_reply(reply, "too_long"), state.turns[-1].text)
        self.assertTrue(state.turns[-1].text.startswith(reply[:REPLY_MAX_CHARS]))

    def test_a_kept_partial_is_redacted_and_loses_a_cut_token(self) -> None:
        from jarvis.ui.bridge import _kept_partial
        self.assertEqual("Komut: tool --api-key [REDACTED] ve", _kept_partial("Komut: tool --api-key Zq9xW2pL7vK4mN8r ve"))
        self.assertEqual("Anahtar şu:", _kept_partial("Anahtar şu: ghp_abcdef12"))          # a token the stream cut
        self.assertEqual("Kısmi yanıt", _kept_partial("Kısmi yanıt"))                         # an ordinary last word stays
        self.assertEqual("", _kept_partial("sk-abcdefgh"))
        self.assertEqual(REPLY_MAX_CHARS, len(_kept_partial("ç" * 60_000)))

    def test_a_reply_that_writes_the_marker_itself_is_not_cut(self) -> None:
        reply = "Tam bir yanıt.\n\n[reply cut short: error]"
        bridge = self.streaming(chat(reply), previews=(reply,))
        self.assertTrue(bridge.submitText("Yaz."))
        self.assertEqual(("reply", reply + "\n"), self.turns(bridge)[-1])
        a = bridge.conversationId
        self.assertTrue(bridge.openConversation(bridge._conversation_db().create_conversation().id))
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual(["user", "jarvis"], self.kinds(bridge))
        self.assertNotIn("cut", bridge.feed[-1])

    def test_an_established_cause_keeps_its_card_after_the_cut_notice(self) -> None:
        from jarvis.connectors.claude_code import AUTH_NOT_LOGGED_IN_EVIDENCE
        error = StructuredError(ErrorCode.CONNECTOR_FAILED, "stopped", details={"billing_evidence": AUTH_NOT_LOGGED_IN_EVIDENCE})
        bridge = self.streaming(previews=("Yarım kalan",), error=error)
        self.assertTrue(bridge.submitText("Anlat."))
        self.assertEqual(["user", "jarvis", "notice", "error"], self.kinds(bridge))
        self.assertIn("claude auth login", bridge.feed[-1]["text"])

    def test_the_too_long_notice_text_and_the_entries(self) -> None:
        entries = text_entries({"intent": {"status": "conversation", "reply": "kısmi", "reason": CHAT_REPLY_TOO_LONG}})
        self.assertEqual(cut_entries("kısmi", "too_long"), entries)
        self.assertEqual(["jarvis", "notice"], [e["kind"] for e in entries])


def rollback_modules(folder: Path):
    """The rollback release's memory.py source and its conversation module, from git."""
    git = shutil.which("git")
    assert git, "git is needed to read the rollback release"

    def show(path: str) -> bytes:
        return subprocess.run([git, "show", f"{ROLLBACK}:{path}"], cwd=ROOT, capture_output=True,  # nosec B603
                              check=True).stdout
    target = folder / f"conversation_{ROLLBACK}.py"
    target.write_bytes(show("jarvis/conversation.py"))
    name = f"conversation_{ROLLBACK}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return show("jarvis/memory.py").decode("utf-8"), module


class AwayTests(UiTestCase):
    """Item 3: the run ends while another conversation is open."""

    def setUp(self) -> None:
        super().setUp()
        self.gate, self.started = threading.Event(), threading.Event()

    def hold(self, task) -> None:
        self.started.set()
        self.gate.wait(PATIENCE)

    def wait(self, condition) -> None:
        deadline = time.monotonic() + PATIENCE
        while not condition() and time.monotonic() < deadline:
            pump(20)
        self.assertTrue(condition())

    def run_away(self, answers, previews, error=None):
        self.model = StreamingModel(previews=previews, error=error)
        self.model.on_complete = self.hold
        bridge = self.bridge(*answers, sync=False)
        self.addCleanup(bridge.waitIdle)
        self.addCleanup(self.gate.set)
        store = bridge._conversation_db()
        a, b = store.create_conversation().id, store.create_conversation().id
        bridge._refresh_conversations()
        self.assertTrue(bridge.openConversation(a))
        self.assertTrue(bridge.submitText("Anlat."))
        self.assertTrue(self.started.wait(PATIENCE))
        self.wait(lambda: bridge.streamText != "")
        self.assertTrue(bridge.openConversation(b))
        before = list(bridge.feed)
        self.gate.set()
        self.wait(lambda: not bridge.busy)
        bridge.waitIdle()
        pump(50)
        self.assertEqual(before, list(bridge.feed))                       # nothing of A's on B's screen
        turns = [(t.kind, t.text) for t in store.load_conversation_state(a).turns]
        self.assertTrue(bridge.openConversation(a))
        return bridge, turns

    def test_an_error_after_the_switch_keeps_the_seen_text_there(self) -> None:
        bridge, turns = self.run_away((), ("Görülen kısım",), StructuredError(ErrorCode.CONNECTOR_FAILED, "stopped"))
        self.assertEqual([("user", "Anlat."), ("reply", cut_reply("Görülen kısım", "error"))], turns)
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        self.assertEqual(("Görülen kısım", "error"), (bridge.feed[1]["text"], bridge.feed[1]["cut"]))

    def test_a_too_long_reply_after_the_switch_is_kept_there(self) -> None:
        reply = lines(60_000)
        bridge, turns = self.run_away((chat(reply),), (reply[:900],))
        self.assertEqual([("user", "Anlat."), ("reply", cut_reply(reply, "too_long"))], turns)
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))


class LongMarkdownTests(UiTestCase):
    """Item 1: the window renders a 50,000-character Markdown reply (and its stream) whole."""

    def window(self, bridge):
        root = self.load(bridge)
        root.showNormal()
        root.resize(1920, 1080)
        window_ready(self, root, 1920, 1080, 350)
        return root

    def find(self, root, name):
        stack = [root.contentItem()]
        while stack:
            node = stack.pop()
            if node.objectName() == name:
                return node
            stack.extend(node.childItems())
        return None

    def test_a_50000_character_markdown_reply_renders_whole(self) -> None:
        """The whole document is rendered and streamed; the bounds are hang guards only.
        Measured here 1.9-2.8 s to show and about 1.4 s per 50,000-character preview, almost
        all in ``Typography.setLineHeight`` (one relayout per block format; a single
        ``beginEditBlock`` around it measured 0.28 s / 0.10 s). That file is outside DFREPLY's
        owned list, so the no-stall bound waits for it (see build/reports/DFREPLY.md)."""
        block = ("## Bölüm\n\n1. **Kalın** bir madde ve `kod` parçası.\n- Bir liste satırı, biraz daha uzun yazılmış.\n\n"
                 "```python\nprint('merhaba')\n```\n\n")
        text = (block * (REPLY_MAX_CHARS // len(block) + 1))[:REPLY_MAX_CHARS]
        bridge = self.bridge()
        root = self.window(bridge)
        started = time.monotonic()
        bridge._say("jarvis", text, chat=True)
        pump(50)
        message = self.find(root, "feedMessage_jarvis")
        shown = time.monotonic() - started
        self.assertIsNotNone(message)
        self.assertGreater(message.property("length"), REPLY_MAX_CHARS // 2)              # the document is there
        self.assertLess(shown, 15.0, f"a 50,000-character reply took {shown:.1f} s to show")
        # Streaming the same length: every growing preview is shown, whole.
        bridge._busy, bridge._request_id = True, "live"
        started = time.monotonic()
        for size in range(10_000, REPLY_MAX_CHARS + 1, 10_000):
            bridge._on_preview("live", text[:size])
            pump(10)
        streamed = time.monotonic() - started
        self.assertEqual(text, bridge.streamText)
        self.assertLess(streamed, 20.0, f"five previews up to 50,000 characters took {streamed:.1f} s")
        bridge._busy = False
