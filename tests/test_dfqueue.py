"""DFQUEUE dogfood (Salih 2026-10-12 "Siraya alsin"): a message sent while JARVIS
works in another conversation waits in a queue and starts by itself, in order.

1. A send from any conversation but the running one (the New chat screen too)
   while a run is active is queued: shown there as "Queued" (with its place
   when more than one waits), the draft spent, the sidebar row marked; the New
   chat screen's conversation exists from then on.
2. When the run ends, the oldest queued message starts exactly as if sent then,
   with the choices captured when it was queued (FIFO across conversations).
3. Taken back: it returns to that conversation's draft (text and chips); nothing runs.
4. Freeze and quit return every queued message to its draft; nothing starts
   later or on the next launch. An approval that waits still counts as active.
5. A cap: beyond it the send is refused with a notice and the draft stays.

Every test asserts the gate: no task run ever starts while one is running.
The worker is real (``sync=False``) and paused inside the model call; the
test's own temporary database and preferences only; no live model, auth or
network call.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject

from jarvis.ui.bridge import QUEUE_LIMIT
from tests.test_dfnew import say
from tests.test_intent import proposal, task_proposal
from jarvis.ui.preferences import NEW_CHAT_DRAFT, UiPreferences
from tests.test_ui import UiTestCase, find, pump, tiny_png

PATIENCE = 60


def chat(reply: str):
    return proposal(kind="chat", reply=reply)


class QueueTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.gate, self.started = threading.Event(), threading.Event()
        self.starts: list[bool] = []            # each task run started: was one already running?
        self.sent: list[tuple] = []             # (text, conversation_id, claude) as the backend got them

        def hold(task) -> None:                 # the paused provider call
            self.started.set()
            self.gate.wait(PATIENCE)
        self.model.on_complete = hold

    # -- harness ---------------------------------------------------------------

    def three(self, *answers):
        """A (open), B and C - each with one earlier message; a threaded bridge, gate-watched."""
        bridge = self.bridge(*answers, sync=False)
        self.addCleanup(bridge.waitIdle)
        self.addCleanup(self.gate.set)
        store = bridge._conversation_db()
        ids = []
        for name in ("A", "B", "C"):
            cid = store.create_conversation().id
            store.set_conversation_title(cid, name)
            say(store, cid, f"{name} önce")
            ids.append(cid)
        bridge._refresh_conversations()
        run = bridge._run

        def watched(kind, fn, on_done, executing=False):
            if kind == "task":
                self.starts.append(bridge._busy)
            return run(kind, fn, on_done, executing)
        bridge._run = watched
        submit = bridge.backend.submit_text

        def recorded(text, session_id, **kwargs):
            self.sent.append((text, kwargs.get("conversation_id"), kwargs.get("claude")))
            return submit(text, session_id, **kwargs)
        bridge.backend.submit_text = recorded
        self.assertTrue(bridge.openConversation(ids[0]))
        return (bridge, store, *ids)

    def wait(self, bridge, condition, what: str) -> None:
        deadline = time.monotonic() + PATIENCE
        while not condition() and time.monotonic() < deadline:
            pump(20)
        self.assertTrue(condition(), f"still not {what}: busy={bridge.busy} queue={bridge._queue} sent={self.sent}")

    def start(self, bridge, text: str) -> None:
        self.started.clear()
        self.gate.clear()
        self.assertTrue(bridge.submitText(text))
        self.assertTrue(self.started.wait(PATIENCE), "the worker never reached the provider")
        pump(20)
        self.assertTrue(bridge.busy)

    def release(self, bridge, sent: int) -> None:
        """Let every provider call through and wait until ``sent`` requests ran and all is idle."""
        self.gate.set()
        self.wait(bridge, lambda: len(self.sent) >= sent and not bridge.busy and not bridge._queue
                  and not bridge._queue_timer.isActive(), f"{sent} requests sent and idle")
        bridge.waitIdle()
        pump(50)

    def queue(self, bridge, text: str) -> None:
        bridge.setDraftText(text)
        self.assertTrue(bridge.queueText(text))

    def turns(self, store, cid: str) -> list[tuple]:
        return [(t.kind, t.text) for t in store.load_conversation_state(cid).turns]

    def one_at_a_time(self) -> None:
        """The gate: no task run ever started while another was running."""
        self.assertNotIn(True, self.starts)

    # -- 1 / 2 -----------------------------------------------------------------

    def test_a_queued_in_b_while_a_runs_shows_sirada_then_starts_by_itself(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"), chat("B'nin yanıtı"))
        root = self.load(bridge)
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        composer = root.findChild(QObject, "canonicalComposer")
        sequence, turns_b = bridge._sequence["task"], self.turns(store, b)
        composer.setProperty("text", "B'nin sırası")
        pump(20)
        QMetaObject.invokeMethod(composer, "submit")
        pump(50)
        self.assertEqual("", composer.property("text"))                     # the draft is spent
        self.assertEqual([("B'nin sırası", 1, 1)], [(e["text"], e["position"], e["total"]) for e in bridge.queuedHere])
        self.assertEqual("Queued", find(root.contentItem(), "queuedLabel").property("text"))
        self.assertEqual([b], bridge.queuedIds)                             # the sidebar row's marker
        self.assertEqual((sequence, turns_b), (bridge._sequence["task"], self.turns(store, b)))   # nothing started
        self.assertEqual("", bridge._drafts.get(b, {}).get("text", ""))
        self.release(bridge, 2)
        self.assertEqual([("A'nın sorusu", a), ("B'nin sırası", b)], [(t, cid) for t, cid, _ in self.sent])
        self.assertEqual([("user", "B'nin sırası"), ("reply", "B'nin yanıtı")], self.turns(store, b)[-2:])
        self.assertIn("B'nin yanıtı", [e["text"] for e in bridge.feed])    # B is open: shown there
        self.assertEqual(([], []), (bridge.queuedHere, bridge.queuedIds))
        self.assertIsNone(find(root.contentItem(), "queuedLabel"))
        self.one_at_a_time()

    def test_b_two_queued_start_in_order_across_conversations(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"), chat("B'nin yanıtı"), chat("C'nin yanıtı"))
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.openConversation(b))
        self.queue(bridge, "B'nin sırası")
        self.assertTrue(bridge.openConversation(c))
        self.queue(bridge, "C'nin sırası")
        self.assertEqual([("C'nin sırası", 2, 2)], [(e["text"], e["position"], e["total"]) for e in bridge.queuedHere])
        self.assertEqual(sorted([b, c]), bridge.queuedIds)
        self.assertTrue(bridge.openConversation(a))                         # back on the running one: unchanged
        self.assertEqual((True, []), (bridge.openRunning, bridge.queuedHere))
        feed_a = [e["text"] for e in bridge.feed]
        self.release(bridge, 3)
        self.assertEqual([("A'nın sorusu", a), ("B'nin sırası", b), ("C'nin sırası", c)],
                         [(t, cid) for t, cid, _ in self.sent])
        self.assertEqual([("user", "B'nin sırası"), ("reply", "B'nin yanıtı")], self.turns(store, b)[1:])
        self.assertEqual([("user", "C'nin sırası"), ("reply", "C'nin yanıtı")], self.turns(store, c)[1:])
        self.assertEqual(a, bridge.conversationId)                          # A's screen: nothing of B's or C's
        self.assertNotIn("B'nin", "\n".join(e["text"] for e in bridge.feed if e["text"] not in feed_a))
        self.one_at_a_time()

    def test_c_an_approval_that_waits_holds_the_queue(self) -> None:
        bridge, store, a, b, c = self.three(task_proposal("code_analysis", objective="Bu projeyi incele."),
                                            chat("B'nin yanıtı"))
        self.start(bridge, "Bu projeyi incele.")
        self.assertTrue(bridge.openConversation(b))
        self.queue(bridge, "B'nin sırası")
        self.gate.set()
        self.wait(bridge, lambda: not bridge.busy and a in bridge.needsYouIds, "A waiting for its approval")
        pump(100)
        self.assertEqual((1, [b]), (len(self.sent), bridge.queuedIds))      # the queue waits
        self.assertTrue(bridge.openConversation(a))
        self.assertTrue(bridge.approvalPending)
        bridge.deny()
        self.release(bridge, 2)
        self.assertEqual(("B'nin sırası", b), self.sent[-1][:2])
        self.one_at_a_time()

    # -- 3 ---------------------------------------------------------------------

    def test_d_taken_back_it_returns_to_the_draft_with_its_chips_and_nothing_runs(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"))
        root = self.load(bridge)
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        folder = Path(self.temp.name) / "files"
        folder.mkdir()
        (folder / "not.txt").write_text("not içeriği", encoding="utf-8")
        self.assertTrue(bridge.attachFile((folder / "not.txt").as_uri()))
        self.assertTrue(bridge.attachFile(tiny_png(folder / "shot.png").as_uri()))
        snapshot = Path(next(f["path"] for f in bridge._attachments if f.get("path")))
        composer = root.findChild(QObject, "canonicalComposer")
        composer.setProperty("text", "B'nin sırası")
        pump(20)
        QMetaObject.invokeMethod(composer, "submit")
        pump(50)
        self.assertEqual(("", []), (composer.property("text"), bridge.globalAttachments))
        self.assertTrue(bridge.openConversation(c))                         # switches discard unheld snapshots:
        self.assertTrue(bridge.openConversation(b))                         # a queued one's is held
        pump(50)
        self.assertTrue(snapshot.exists())
        remove = find(root.contentItem(), "queuedRemove_0")
        self.assertIsNotNone(remove)
        QMetaObject.invokeMethod(remove, "activated")
        pump(50)
        self.assertEqual(([], []), (bridge.queuedHere, bridge.queuedIds))
        self.assertEqual("B'nin sırası", composer.property("text"))
        self.assertEqual(["not.txt", "shot.png"], [f["name"] for f in bridge.globalAttachments])
        self.release(bridge, 1)
        pump(100)
        self.assertEqual([("A'nın sorusu", a)], [(t, cid) for t, cid, _ in self.sent])
        self.assertEqual([("user", "B önce")], self.turns(store, b))
        self.one_at_a_time()

    # -- 4 ---------------------------------------------------------------------

    def test_e_freeze_returns_the_queue_to_the_drafts_and_nothing_starts_after_resume(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"), chat("yanlış"), chat("yanlış"))
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.openConversation(b))
        self.queue(bridge, "B'nin sırası")
        self.assertTrue(bridge.openConversation(c))
        self.queue(bridge, "C'nin sırası")
        bridge.setDraftText("C'de yeni")
        bridge.freezeJarvis()
        self.assertEqual([], bridge.queuedIds)
        self.assertEqual("B'nin sırası", bridge._drafts[b]["text"])
        self.assertEqual("C'nin sırası\n\nC'de yeni", bridge.draftText)    # the open one's: in front of what was typed
        self.assertFalse(bridge.queueText("C'de yeni"))                      # frozen: nothing joins either
        self.gate.set()
        self.wait(bridge, lambda: not bridge.busy, "idle")
        bridge.resumeJarvis()
        pump(200)
        bridge.waitIdle()
        self.assertEqual([("A'nın sorusu", a)], [(t, cid) for t, cid, _ in self.sent])
        self.assertEqual(([], []), (self.turns(store, b)[1:], self.turns(store, c)[1:]))
        self.one_at_a_time()

    def test_f_quit_keeps_them_as_drafts_and_nothing_starts_on_the_next_launch(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"), chat("yanlış"))
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.openConversation(b))
        self.queue(bridge, "B'nin sırası")
        self.assertIn("1 queued message(s) will go back to their drafts", bridge.quitWarning)
        bridge.confirmQuit()
        self.assertEqual([], bridge.queuedIds)
        self.gate.set()
        self.wait(bridge, lambda: not bridge.busy, "idle")
        bridge.waitIdle()
        pump(100)
        self.assertEqual([("A'nın sorusu", a)], [(t, cid) for t, cid, _ in self.sent])
        again = self.bridge()                                               # the next launch, same preferences
        pump(100)
        self.assertEqual("B'nin sırası", again._drafts[b]["text"])
        self.assertEqual(([], False), (again.queuedIds, again.busy))
        self.assertEqual([], self.turns(store, b)[1:])
        self.one_at_a_time()

    # -- 5 / New chat ----------------------------------------------------------

    def test_g_beyond_the_cap_the_send_is_refused_and_the_draft_stays(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"))
        root = self.load(bridge)
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        for n in range(QUEUE_LIMIT):
            self.queue(bridge, f"B {n}")
        composer = root.findChild(QObject, "canonicalComposer")
        composer.setProperty("text", "bir fazlası")
        pump(20)
        QMetaObject.invokeMethod(composer, "submit")
        pump(50)
        self.assertEqual("bir fazlası", composer.property("text"))
        self.assertEqual(QUEUE_LIMIT, len(bridge._queue))
        self.assertIn(f"The queue is full ({QUEUE_LIMIT} messages)", bridge.feed[-1]["text"])
        for entry in bridge.queuedHere:                                     # leave nothing to run
            self.assertTrue(bridge.unqueue(entry["id"]))
        self.release(bridge, 1)
        self.one_at_a_time()

    def test_h_the_new_chat_screen_queues_into_a_conversation_that_exists_from_then_on(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"), chat("Yeni sohbetin yanıtı"))
        root = self.load(bridge)
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.startNewChat())
        new = bridge.conversationId
        self.assertNotIn(new, [row["id"] for row in bridge._conversation_list])     # DFNEW: listed nowhere yet
        captured = (bridge._config.get("claude_model") or None, bridge._config.get("claude_effort") or None)
        self.queue(bridge, "Yeni sohbetten")
        self.assertEqual([new], bridge.queuedIds)
        self.assertIn(new, [row["id"] for row in bridge._conversation_list])        # it exists from now on
        pump(100)
        self.assertTrue(find(root.contentItem(), "queuedLabel").isVisible())        # shown there, not the welcome
        bridge.applyModel("claude-opus-5-5", "high")                        # a later choice: not the queued one's
        self.release(bridge, 2)
        self.assertEqual(("Yeni sohbetten", new, captured), self.sent[-1])
        self.assertEqual([("user", "Yeni sohbetten"), ("reply", "Yeni sohbetin yanıtı")], self.turns(store, new))
        self.one_at_a_time()

    def test_i_a_new_chat_queue_comes_back_into_the_new_chat_screen_s_own_draft(self) -> None:
        bridge, store, a, b, c = self.three(chat("A'nın yanıtı"))
        self.start(bridge, "A'nın sorusu")
        self.assertTrue(bridge.startNewChat())
        first = bridge.conversationId
        self.queue(bridge, "İlk yeni sohbetten")
        self.assertTrue(bridge.startNewChat())                              # another New chat screen
        self.assertNotEqual(first, bridge.conversationId)                   # the queued one is not reused
        bridge.setDraftText("ikincide yazılan")
        bridge.freezeJarvis()
        self.assertEqual("İlk yeni sohbetten\n\nikincide yazılan", bridge.draftText)   # the live New chat draft
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual("İlk yeni sohbetten\n\nikincide yazılan",
                         UiPreferences(self.prefs_path).load_drafts()[NEW_CHAT_DRAFT]["text"])
        self.release(bridge, 1)
        self.one_at_a_time()
