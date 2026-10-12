"""Gate 6G R1 - switching conversations while JARVIS runs (Salih 2026-10-06).

A conversation-bound run carries a run record (bridge memory only): while it
is in flight the person may open, create and return to conversations; it
delivers only into its origin, re-checked at delivery time. Its own
conversation's mutations and session rebinding stay refused; a run without a
record (the manual sweep) keeps every refusal it had. One run at a time.

The worker is real (``sync=False``) and paused inside the model call, so every
switch happens mid-run; nothing reaches a model, a network or the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject

from tests.test_intent import proposal, task_proposal
from tests.test_ui import UiTestCase, pump, tiny_png

TABLES = ("conversations", "conversation_sessions", "conversation_deletions")
# Sized for a starved CI runner: the module ran 74 s there (7 s locally) and a
# 10 s wait for the approval card timed out (run 38109324177). A healthy run
# never comes near it; the waits end as soon as their condition holds.
PATIENCE = 60


def state(bridge) -> str:
    """What the bridge showed when a wait gave up, for the failure message."""
    feed = bridge.feed
    return (f"busy={bridge.busy} approvalPending={bridge.approvalPending} "
            f"conversation={bridge.conversationId!r} running={bridge.runningConversationId!r} "
            f"coreState={bridge.coreState!r} task={bridge.task.get('status')!r} "
            f"lastError={bridge._last_error!r} lastFeed={feed[-1]['text'] if feed else None!r}")


class BackgroundRunTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.gate, self.started = threading.Event(), threading.Event()
        self.provider_saw: list[bool] = []
        self.watch: list[Path] = []

        def hold(task) -> None:            # the paused provider call
            self.started.set()
            self.gate.wait(PATIENCE)
            self.provider_saw.extend(path.exists() for path in self.watch)
        self.model.on_complete = hold

    # -- helpers ---------------------------------------------------------------

    def wait(self, bridge, condition, what: str, seconds: float = PATIENCE) -> None:
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            pump(20)
        self.assertTrue(condition(), f"after {seconds} s still not {what}: {state(bridge)}")

    def two(self, *answers, project: bool = False):
        """A (open) and B, Global unless ``project``; a threaded bridge."""
        bridge = self.bridge(*answers, sync=False)
        self.addCleanup(bridge.waitIdle)
        self.addCleanup(self.gate.set)
        store = bridge._conversation_db()
        owner = store.create_project("Ev").id if project else None
        a = store.create_conversation(owner).id
        b = store.create_conversation().id
        store.set_conversation_title(b, "Tatil")
        bridge._refresh_conversations()
        self.assertTrue(bridge.openConversation(a))
        return bridge, store, a, b

    def start(self, bridge, text: str = "Merhaba") -> None:
        self.started.clear()
        self.gate.clear()
        self.assertTrue(bridge.submitText(text))
        self.assertTrue(self.started.wait(PATIENCE),
                        f"after {PATIENCE} s the worker still had not reached the provider: {state(bridge)}")
        pump(20)
        self.assertTrue(bridge.busy)

    def finish(self, bridge) -> None:
        self.gate.set()
        self.wait(bridge, lambda: not bridge.busy, "idle")
        bridge.waitIdle()
        pump(50)

    def rows(self) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            return {table: sorted(map(tuple, db.execute(f"SELECT * FROM {table}"))) for table in TABLES}  # nosec B608 - constant names

    def turns(self, store, conversation_id: str) -> list[tuple]:
        return [(t.kind, t.text) for t in store.load_conversation_state(conversation_id).turns]

    def linked_session(self, bridge, store, a: str) -> str:
        """A's own session, linked to A, its task rejected (no open work)."""
        self.gate.set()
        self.assertTrue(bridge.submitText("Bu projeyi incele."))
        self.wait(bridge, lambda: not bridge.busy and bridge.approvalPending, "idle with the approval card")
        bridge.deny()
        self.wait(bridge, lambda: not bridge.busy and bridge.task.get("status") == "rejected", "idle with the task rejected")
        bridge.waitIdle()
        session = bridge.sessionId
        self.assertEqual(a, store.conversation_for_session(session))
        return session

    # -- T1 / I2 ---------------------------------------------------------------

    def test_t1_i2_a_reply_lands_once_in_its_own_conversation_and_never_on_the_open_one(self) -> None:
        bridge, store, a, b = self.two(proposal(kind="chat", reply="A'nın yanıtı"))
        self.start(bridge)
        self.assertTrue(bridge.openRunning)
        self.assertTrue(bridge.openConversation(b))                        # lifted while a recorded run is in flight
        self.assertEqual((b, False, a), (bridge.conversationId, bridge.openRunning, bridge.runningConversationId))
        feed_b, turns_b = list(bridge.feed), self.turns(store, b)
        bridge._on_preview(bridge._request_id, "A'nın akışı")               # a preview emitted AFTER the switch
        self.assertEqual("", bridge.streamText)
        self.finish(bridge)
        self.assertIsNone(bridge._run_record)
        self.assertEqual(b, bridge.conversationId)
        self.assertEqual(feed_b, list(bridge.feed))                         # B's screen: nothing of A's
        self.assertNotIn("A'nın", "\n".join(e["text"] for e in bridge.feed))
        self.assertEqual(turns_b, self.turns(store, b))
        self.assertEqual(1, [text for _, text in self.turns(store, a)].count("A'nın yanıtı"))
        self.assertIn("saved there", bridge.activity[0]["text"])
        self.assertTrue(bridge.openConversation(a))                         # remembered there, shown when opened
        self.assertIn("A'nın yanıtı", [e["text"] for e in bridge.feed])

    # -- T2 / T2b / T8 ---------------------------------------------------------

    def test_t2_t8_leaving_and_returning_keeps_durdur_the_stream_and_the_draft(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective="Bu projeyi incele."),
                                       task_proposal("code_analysis", objective="Yeniden incele."))
        root = self.load(bridge)
        session = self.linked_session(bridge, store, a)
        self.start(bridge, "Yeniden incele.")
        record = dict(bridge._run_record)
        self.assertEqual((a, session, ""), (record["origin"], record["session_id"], record["task_id"]))
        bridge._on_preview(bridge._request_id, "inceliyorum")
        send = root.findChild(QObject, "composerSend")
        elsewhere = root.findChild(QObject, "composerElsewhere")
        pump(50)
        self.assertEqual((True, False), (send.property("stopping"), elsewhere.property("visible")))
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        # B: no Durdur, no stream, no clock - the reason and the link instead.
        self.assertEqual((False, "", 0.0), (bridge.openRunning, bridge.streamText, bridge.workStartedAt))
        self.assertEqual((False, True), (send.property("stopping"), elsewhere.property("visible")))
        live = root.findChild(QObject, "streamingReply")
        self.assertEqual(("idle", False), (bridge.coreState, live.property("visible")))   # no live turn of A's on B
        bridge._on_preview(bridge._request_id, "inceliyorum, dosyalar")     # arrives while B is open
        self.assertEqual("", bridge.streamText)
        self.assertEqual("", bridge.sessionId)
        # T8: Send in B is refused, the draft stays and no second run is queued.
        sequence = bridge._sequence["task"]
        composer = root.findChild(QObject, "canonicalComposer")
        composer.setProperty("text", "B'de bir taslak")
        QMetaObject.invokeMethod(composer, "submit")
        self.assertFalse(bridge.submitText("B'de bir taslak"))
        self.assertEqual(("B'de bir taslak", sequence), (composer.property("text"), bridge._sequence["task"]))
        self.assertTrue(bridge.openRunningConversation())                  # the composer's link: back to A
        pump(50)
        self.assertEqual((a, True, "inceliyorum, dosyalar"), (bridge.conversationId, bridge.openRunning, bridge.streamText))
        self.assertGreater(bridge.workStartedAt, 0)
        self.assertEqual(("running", True), (bridge.coreState, live.property("visible")))
        self.assertEqual(session, bridge.sessionId)
        self.assertEqual((True, False), (send.property("stopping"), elsewhere.property("visible")))
        # Durdur after A -> B -> A: cancelText by the RECORD's request ...
        texts, cancels = [], []
        bridge.backend.cancel_text = lambda request_id: texts.append(request_id) or True
        bridge.backend.cancel = lambda s, t: cancels.append((s, t)) or {}
        QMetaObject.invokeMethod(send, "act")
        self.assertEqual([record["request_id"]], texts)
        # ... and the Composer fallback: cancelTask by the record's session; T2b: no cached task id,
        # the authoritative lookup from the record's session answers.
        bridge.backend.cancel_text = lambda request_id: texts.append(request_id) or False
        looked = []
        bridge.backend.active_task_id = lambda s: looked.append(s) or "t-live"
        bridge._session_id = ""                                             # the screen's identity is not used
        QMetaObject.invokeMethod(send, "act")
        self.assertEqual([session], looked)
        self.finish(bridge)
        self.assertEqual([(session, "t-live")], cancels)

    def test_t2b_the_cached_task_is_only_the_fallback_and_nothing_known_says_so(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective="Bu projeyi incele."),
                                       task_proposal("code_analysis", objective="Yeniden incele."))
        session = self.linked_session(bridge, store, a)
        self.start(bridge, "Yeniden incele.")
        cancels = []
        bridge.backend.cancel = lambda s, t: cancels.append((s, t)) or {}
        bridge.backend.active_task_id = lambda s: None
        bridge._run_record["task_id"] = "t-cached"
        self.assertTrue(bridge.openConversation(b))
        bridge.cancelTask()                                                 # from B: B's own (none), never A's run
        self.assertTrue(bridge.openConversation(a))
        bridge.cancelTask()
        bridge._run_record["session_id"] = ""
        bridge.cancelTask()
        self.assertIn("No cancellable task", bridge.feed[-1]["text"])
        self.finish(bridge)
        self.assertEqual([(session, "t-cached")], cancels)

    # -- T3s -------------------------------------------------------------------

    def test_t3s_the_running_request_keeps_its_image_until_the_run_ends(self) -> None:
        bridge, store, a, b = self.two(proposal(kind="chat", reply="Gördüm"))
        folder = Path(tempfile.mkdtemp(prefix="jarvis-r1-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, True))
        self.assertTrue(bridge.attachFile(tiny_png(folder / "shot.png").as_uri()))
        snapshot = Path(bridge._attachments[0]["path"])
        self.watch.append(snapshot)
        self.start(bridge, "Bu resme bak")
        self.assertTrue(bridge.openConversation(b))
        self.assertTrue(bridge.attachFile(tiny_png(folder / "other.png", rgb=(1, 2, 3)).as_uri()))   # B's own draft
        bridge.newSession()
        draft = Path(bridge._attachments[0]["path"]) if bridge._attachments else None
        self.assertTrue(snapshot.exists())                                  # kept through the switch and the new session
        self.finish(bridge)
        self.assertEqual([True], self.provider_saw)                         # the provider read it after both
        self.assertFalse(snapshot.exists())                                 # discarded once the run ended
        self.assertIsNone(draft)                                            # newSession emptied B's draft as before

    def test_t3s_a_draft_holding_the_same_file_keeps_it(self) -> None:
        bridge, store, a, b = self.two(proposal(kind="chat", reply="Gördüm"))
        folder = Path(tempfile.mkdtemp(prefix="jarvis-r1-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, True))
        shot = tiny_png(folder / "shot.png")
        bridge.attachFile(shot.as_uri())
        snapshot = Path(bridge._attachments[0]["path"])
        self.start(bridge, "Bu resme bak")
        self.assertTrue(bridge.openConversation(b))
        bridge.attachFile(shot.as_uri())                                    # the same bytes, the same snapshot
        self.finish(bridge)
        self.assertTrue(snapshot.exists())

    # -- T4 --------------------------------------------------------------------

    def test_t4_the_running_conversation_refuses_its_mutations_with_zero_writes(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective="Bu projeyi incele."),
                                       proposal(kind="chat", reply="Tamam"))
        session = self.linked_session(bridge, store, a)
        self.start(bridge, "Selam")
        before, title = self.rows(), store.load_conversation(a).title
        self.assertFalse(bridge.renameConversation(a, "Yeni ad"))
        self.assertFalse(bridge.archiveConversation(a))
        self.assertFalse(bridge.unarchiveConversation(a))
        bridge._delete_preview = {**store.conversation_delete_preview(a), "blocked": ""}
        self.assertFalse(bridge.confirmDeleteConversation(a))
        bridge.previewAttach("", "Yeni proje")
        self.assertFalse(bridge.confirmAttach("", "Yeni proje"))
        bridge.newSession()
        bridge.useSession(session)
        self.assertEqual((a, session), (bridge.conversationId, bridge.sessionId))
        self.assertEqual(before, self.rows())
        # On B, while A runs: no longer refused because of the run; their own checks still apply.
        self.assertTrue(bridge.openConversation(b))
        self.assertTrue(bridge.renameConversation(b, "Tatil planı"))
        self.assertTrue(bridge.archiveConversation(b))
        self.assertNotIn(bridge.conversationId, (a, b))                     # N2 left it at once, not into A
        self.assertTrue(bridge.unarchiveConversation(b))
        self.assertFalse(bridge.renameConversation(b + "x", "yok"))         # an unknown one: the store refuses
        self.finish(bridge)
        self.assertEqual("Tatil planı", store.load_conversation(b).title)
        self.assertEqual(title, store.load_conversation(a).title)

    def test_t4_archiving_the_running_conversations_project_is_refused(self) -> None:
        bridge, store, a, b = self.two(proposal(kind="chat", reply="Tamam"), project=True)
        other = store.create_project("Başka").id
        project = store.load_conversation(a).project_id
        self.start(bridge)
        self.assertEqual(project, bridge.runningProjectId)
        bridge.archiveProject(project)
        self.assertIn("Wait for the current run", bridge.projectNotice)
        bridge.archiveProject(other)                                        # queued behind the run, then done
        self.finish(bridge)
        self.assertTrue(store.load_project(project).active)
        self.assertFalse(store.load_project(other).active)

    # -- T6s -------------------------------------------------------------------

    def test_t6s_use_session_while_busy_fails_closed(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective="Bu projeyi incele."),
                                       task_proposal("code_analysis", objective="Bu projeyi incele."),
                                       proposal(kind="chat", reply="Tamam"))
        self.assertTrue(bridge.openConversation(b))
        of_b = self.linked_session(bridge, store, b)
        self.assertTrue(bridge.openConversation(a))
        session = self.linked_session(bridge, store, a)
        self.start(bridge, "Selam")
        c = store.create_conversation().id
        bridge._refresh_conversations()
        self.assertTrue(bridge.openConversation(c))
        state = lambda: (bridge.conversationId, bridge.sessionId, list(bridge.feed), bridge.task)  # noqa: E731
        before = state()
        bridge.useSession("not-linked-anywhere")                            # unlinked: no owner
        self.assertEqual(before, state())
        bridge.useSession(session)                                          # the running conversation's
        self.assertEqual(before, state())
        broken = bridge._conversation_db()
        original = broken.conversation_for_session
        broken.conversation_for_session = lambda s: (_ for _ in ()).throw(OSError("store gone"))
        try:
            bridge.useSession(session)                                      # the lookup fails
        finally:
            broken.conversation_for_session = original
        self.assertEqual(before, state())
        bridge.useSession(of_b)                                             # another, non-running one's: allowed
        self.assertEqual((b, of_b), (bridge.conversationId, bridge.sessionId))
        self.finish(bridge)

    # -- T9 --------------------------------------------------------------------

    def deleted_mid_run(self, answers, *, linked: bool, b_open: bool):
        bridge, store, a, b = self.two(*answers)
        if linked:
            self.linked_session(bridge, store, a)
        self.start(bridge, "Yeniden incele." if linked else "Merhaba")
        if b_open:
            self.assertTrue(bridge.openConversation(b))
        held = store.conversation_delete_preview(a)
        store.delete_conversation(a, confirm=a, expected_next_seq=held["expected_next_seq"], expected_title=held["title"])
        before, feed = self.rows(), list(bridge.feed)
        self.finish(bridge)
        after = self.rows()
        self.assertEqual(before["conversation_sessions"], after["conversation_sessions"])
        self.assertEqual(before["conversation_deletions"], after["conversation_deletions"])
        old = {row[0]: row for row in before["conversations"]}
        kept = [row for row in after["conversations"] if row[0] in old]
        self.assertEqual(sorted(old.values()), kept)                        # no write to any existing conversation
        self.assertTrue(any("was dropped" in row["text"] for row in bridge.activity[:3]))
        self.assertIsNone(bridge._run_record)
        if b_open:
            self.assertEqual((b, feed), (bridge.conversationId, list(bridge.feed)))
        else:
            self.assertNotEqual(a, bridge.conversationId)                   # N2 left the deleted one
            # DFNEW: for Global's blank one (the startup one), reused - nothing new is stored.
            self.assertEqual(0, len(after["conversations"]) - len(before["conversations"]))
            self.assertIn(bridge.conversationId, store.blank_conversation_ids())
        return bridge

    def test_t9a_a_sessionless_reply_for_a_deleted_conversation_is_dropped(self) -> None:
        self.deleted_mid_run([proposal(kind="chat", reply="Kayıp")], linked=False, b_open=True)

    def test_t9b_a_first_seen_session_is_never_linked_to_a_deleted_conversation(self) -> None:
        bridge = self.deleted_mid_run([task_proposal("code_analysis", objective="Merhaba")], linked=False, b_open=True)
        self.assertFalse(bridge.approvalPending)

    def test_t9c_a_linked_reply_while_the_deleted_one_is_still_open_is_dropped(self) -> None:
        self.deleted_mid_run([task_proposal("code_analysis", objective="Bu projeyi incele."),
                              task_proposal("code_analysis", objective="Yeniden incele.")], linked=True, b_open=False)

    def test_t9d_a_linked_reply_while_another_one_is_open_is_dropped(self) -> None:
        self.deleted_mid_run([task_proposal("code_analysis", objective="Bu projeyi incele."),
                              task_proposal("code_analysis", objective="Yeniden incele.")], linked=True, b_open=True)

    # -- T10 -------------------------------------------------------------------

    def test_t10_a_manual_sweep_keeps_every_refusal_and_answers_where_it_started(self) -> None:
        bridge, store, a, b = self.two()
        for answer, marker in (({"command": "scheduler", "error": {"code": "store_failed", "message": "süpürme olmadı"}},
                                "süpürme olmadı"), ("garbled", None)):
            self.gate.clear()

            def sweep(answer=answer):
                self.gate.wait(PATIENCE)
                return answer
            bridge.backend.scheduler_once = sweep
            feed = len(bridge.feed)
            bridge.runSweep()
            pump(20)
            self.assertEqual((True, True, ""), (bridge.navigationLocked, bridge.openRunning, bridge.runningConversationId))
            self.assertFalse(bridge.openConversation(b))
            self.assertFalse(bridge.newConversation())
            self.assertFalse(bridge.renameConversation(b, "x"))
            self.assertFalse(bridge.archiveConversation(b))
            bridge.useSession("any")
            self.assertEqual(a, bridge.conversationId)
            self.finish(bridge)
            self.assertGreater(len(bridge.feed), feed)                     # it answered on A, where it started
            if marker:
                self.assertIn(marker, bridge.feed[-1]["text"])
        self.assertTrue(bridge.openConversation(b))
        self.assertNotIn("süpürme", "\n".join(e["text"] for e in bridge.feed))

    # -- approvals -------------------------------------------------------------

    def test_an_approval_is_never_decidable_on_another_conversation(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis", objective="Bu projeyi incele."))
        store = bridge._conversation_db()
        b = store.create_conversation().id
        self.gate.set()
        bridge.submitText("Bu projeyi incele.")
        a, ticket, card = bridge.conversationId, bridge._ticket, dict(bridge._approval)
        self.assertIsNotNone(ticket)
        self.assertTrue(bridge.openConversation(b))
        bridge._ticket, bridge._approval = ticket, card                    # even if A's card were on B's screen
        approvals = []
        bridge.backend.approve = lambda t: approvals.append(t) or {}
        self.assertFalse(bridge.approveRequest(ticket.request_id, ticket.fingerprint))
        self.assertTrue(bridge.openConversation(a))
        bridge._ticket, bridge._approval = ticket, card
        self.assertTrue(bridge.approveRequest(ticket.request_id, ticket.fingerprint))
        self.assertEqual([ticket], approvals)
