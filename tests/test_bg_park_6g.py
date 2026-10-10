"""Gate 6G R2 - parked approvals and clarifications, Stop binding, Resume of a
background run, and the amber "needs you" truth (Salih 2026-10-06 Q2/Q4/Q5).

A conversation that is not open keeps, in bridge memory only, what waits for
the person (an approval card, a clarification) or for Resume (a task Freeze
paused); it comes back only on that conversation, once proven still its own.
Core stays the authority: nothing is rebuilt from history, nothing replays.

Ordinary cases run the sync bridge and switch conversations from INSIDE the
backend / connector call. Ordered (S1) and in-flight (F1) cases run the real
asynchronous bridge on ``StepPool``: its jobs wait until the test runs them, in
order, so the run is really busy while it is inspected. Nothing reaches a
model, a network or the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace

from jarvis.contracts import SideEffect, utc_now
from jarvis.memory import SQLiteMemoryStore
from jarvis.ui.bridge import Bridge, _empty_slots, _kept_refusal, _project_run
from jarvis.ui.preferences import UiPreferences
from tests.test_intent import ACCOUNT, OTHER_ACCOUNT, StubConnector, proposal, task_proposal
from tests.test_ui import UiTestCase

TABLES = ("conversations", "conversation_sessions", "conversation_deletions")
ANALYSE = "Bu projeyi incele."


class StepPool:
    """A deterministic single-worker stand-in for the bridge's pool: each job waits
    until the test runs it, in order, on this thread."""

    def __init__(self) -> None:
        self.jobs: list = []

    def start(self, job) -> None:
        self.jobs.append(job)

    def step(self) -> None:
        self.jobs.pop(0)()

    def waitForDone(self, _ms: int = 0) -> bool:
        while self.jobs:
            self.step()
        return True


class ParkedStateTests(UiTestCase):
    # -- helpers ---------------------------------------------------------------

    def two(self, *answers, sync: bool = True):
        """A (open) and B ("Tatil"), Global; ``sync=False`` runs on a StepPool."""
        bridge = self.bridge(*answers, sync=sync)
        if not sync:
            bridge._pool = StepPool()
        store = bridge._conversation_db()
        a = store.create_conversation().id
        b = store.create_conversation().id
        store.set_conversation_title(b, "Tatil")
        bridge._refresh_conversations()
        self.assertTrue(bridge.openConversation(a))
        return bridge, store, a, b

    def away(self, bridge, b: str) -> None:
        """The next model call opens B from inside it (the run's record lets it)."""
        def switch(task) -> None:
            self.model.on_complete = None
            self.assertTrue(bridge.openConversation(b))
        self.model.on_complete = switch

    def own_session(self, bridge, store, conversation: str) -> str:
        """``conversation`` (open) gets its own linked session; its task rejected (no open work)."""
        self.assertTrue(bridge.submitText(ANALYSE))
        bridge.waitIdle()
        self.assertTrue(bridge.approvalPending)
        bridge.deny()
        bridge.waitIdle()
        session = bridge.sessionId
        self.assertEqual(conversation, store.conversation_for_session(session))
        return session

    def deliver(self, bridge, b: str, run: dict, text: str = "Devam et.") -> None:
        """A structured envelope carrying ``run`` arrives for the open conversation's request
        while B has been opened inside the call (a scripted Core answer, no model)."""
        def submit(*_args, **_kwargs):
            self.assertTrue(bridge.openConversation(b))
            return {"command": "text", "intent": {"status": "structured", "request_id": "r", "capability": "code_analysis",
                                                  "destination": "anthropic/claude-code"}, "run": run, "job": None, "error": None}
        real, bridge.backend.submit_text = bridge.backend.submit_text, submit
        try:
            self.assertTrue(bridge.submitText(text))
        finally:
            bridge.backend.submit_text = real

    @staticmethod
    def waiting_run(session: str, task: str = "t-clar", status: str = "awaiting_clarification") -> dict:
        return {"command": "run", "session": {"id": session}, "task": {"id": task, "status": status, "stage": "plan"},
                "run_status": status}

    @staticmethod
    def paused_run(session: str | None, task: str = "t-paused", stage: str = "execute") -> dict:
        run = {"command": "run", "task": {"id": task, "status": "running", "stage": stage}, "run_status": "running",
               "error": {"code": "execution_disabled", "message": "JARVIS is frozen"}}
        if session is not None:
            run["session"] = {"id": session}
        return run

    def rows(self) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            return {table: sorted(map(tuple, db.execute(f"SELECT * FROM {table}"))) for table in TABLES}  # nosec B608 - constant names

    def sql(self, statement: str, *args) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute(statement, args)
            db.commit()

    def delete_oob(self, conversation: str) -> None:
        self.sql("INSERT INTO conversation_deletions (conversation_id, deleted_at) VALUES (?, ?)", conversation, utc_now())

    def archive_oob(self, conversation: str) -> None:
        self.sql("UPDATE conversations SET archived_at = ? WHERE id = ?", utc_now(), conversation)

    def corrupt_oob(self, conversation: str) -> None:
        self.sql("UPDATE conversations SET state_json = '{}' WHERE id = ?", conversation)

    def turns(self, store, conversation: str) -> list[tuple]:
        return [(t.kind, t.text) for t in store.load_conversation_state(conversation).turns]

    @staticmethod
    def screen(bridge) -> tuple:
        """B's own screen: its slots and feed (never A-derived)."""
        return (bridge.conversationId, bridge._session_id, dict(bridge._task), bridge._ticket, dict(bridge._approval),
                dict(bridge._clarification), [(e["kind"], e["text"]) for e in bridge.feed])

    @staticmethod
    def keys(bridge) -> list[str]:
        return [item["key"] for item in bridge.safety["attention"] if item["key"] != "execution"]

    @staticmethod
    def notes(bridge) -> str:
        return "\n".join(entry["text"] for entry in bridge.activity)

    def spy(self, bridge, name: str, answer=None) -> list:
        """Record every call of ``backend.<name>``; pass it on (or answer ``answer``)."""
        calls, real = [], getattr(bridge.backend, name)

        def recorded(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs) if answer is None else answer
        setattr(bridge.backend, name, recorded)
        return calls

    def pausing_connector(self, then) -> None:
        """Freeze arrives inside the atomic step, and ``then`` (a switch) with it."""
        test = self

        class FreezesMidRun(StubConnector):
            def execute(self, task, session):
                test.current.freezeJarvis()
                then()
                return super().execute(task, session)
        self.claude = FreezesMidRun("claude-code-plan", ("code_analysis", "code_planning"),
                                    side_effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True)

    def paused_in_background(self, sync: bool = True):
        """A's approved task paused by Freeze while B is open (real Core)."""
        held: dict = {}
        self.pausing_connector(lambda: held["bridge"].openConversation(held["b"]))
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       proposal(kind="chat", reply="Tamam"), sync=sync)
        held.update(bridge=bridge, b=b)
        self.current = bridge
        self.assertTrue(bridge.submitText(ANALYSE))
        bridge.waitIdle()
        ticket = bridge._ticket
        bridge.approve()
        bridge.waitIdle()
        self.assertEqual((b, "frozen"), (bridge.conversationId, bridge.executionState))
        self.assertIn(a, bridge._parked)
        self.assertEqual([], bridge.needsYouIds)                  # paused alone is never amber
        return bridge, store, a, b, ticket

    # -- P1 / P2 / P3 ---------------------------------------------------------------

    def test_p1_an_approval_finished_elsewhere_is_parked_and_restored_only_on_its_conversation(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        self.away(bridge, b)
        self.assertTrue(bridge.submitText(ANALYSE))
        self.assertEqual(b, bridge.conversationId)
        self.assertEqual(({}, False, []), (bridge.approval, bridge.approvalPending, self.keys(bridge)))
        self.assertEqual([a], bridge.needsYouIds)
        self.assertTrue(bridge.openConversation(a))
        ticket = bridge._ticket
        pending = SQLiteMemoryStore(self.database).load_pending_approval(ticket.task_id, ticket.session_id)
        self.assertEqual((pending.id, pending.fingerprint), (ticket.request_id, ticket.fingerprint))
        self.assertEqual((ticket.request_id, ticket.fingerprint, True),
                         (bridge.approval["request_id"], bridge.approval["fingerprint"], bridge.safety["approval"]["actionable"]))
        self.assertEqual(a, store.conversation_for_session(ticket.session_id))
        self.assertEqual(["approval"], self.keys(bridge))
        self.assertEqual([a], bridge.needsYouIds)
        approvals = self.spy(bridge, "approve")
        bridge.approve()
        self.assertEqual([(ticket,)], approvals)
        self.assertEqual(1, self.claude.execute_calls)
        self.assertEqual([], bridge.needsYouIds)

    def test_p2_a_parked_approval_is_never_shown_or_decidable_on_another_conversation(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        self.assertTrue(bridge.submitText(ANALYSE))
        ticket = bridge._ticket
        self.assertTrue(bridge.openConversation(b))
        before = self.screen(bridge)
        approvals, rejects = self.spy(bridge, "approve"), self.spy(bridge, "reject")
        self.assertFalse(bridge.approveRequest(ticket.request_id, ticket.fingerprint))
        self.assertFalse(bridge.rejectRequest(ticket.request_id, ticket.fingerprint))
        self.assertIsNone(bridge.approve())
        self.assertIsNone(bridge.deny())
        self.assertEqual(([], []), (approvals, rejects))
        self.assertEqual(before, self.screen(bridge))
        self.assertEqual({}, bridge.safety["decision"])
        self.assertEqual(0, self.execute_calls())
        self.assertEqual([a], bridge.needsYouIds)

    def test_p3_a_card_left_open_is_parked_on_leave_and_nothing_is_parked_when_n2_leaves(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE))
        self.assertTrue(bridge.submitText(ANALYSE))
        card, ticket = bridge.approval, bridge._ticket
        self.assertTrue(bridge.openConversation(b))
        self.assertEqual((b, {}, [a]), (bridge.conversationId, bridge.approval, bridge.needsYouIds))
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual((card, ticket, {}), (bridge.approval, bridge._ticket, bridge._parked))
        for gone in (self.archive_oob, self.delete_oob):
            with self.subTest(gone=gone.__name__):
                c = store.create_conversation().id
                bridge._refresh_conversations()
                self.assertTrue(bridge.openConversation(c))
                self.assertTrue(bridge.submitText(ANALYSE))
                self.assertTrue(bridge.approvalPending)
                gone(c)
                bridge.refreshHistory()                                  # N2 leaves it: nothing is parked
                self.assertNotIn(bridge.conversationId, (a, b, c))
                self.assertNotIn(c, bridge._parked)
                self.assertNotIn(c, bridge.needsYouIds)
        self.assertEqual([a], bridge.needsYouIds)                        # A's own, untouched

    # -- P4 / P4b / P10 -------------------------------------------------------------

    def test_p4_a_parked_clarification_round_trip_uses_its_own_session(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("gmail_thread_list", limit=5), task_proposal("gmail_thread_list", limit=5),
                                       task_proposal("gmail_thread_list", limit=5))
        session = self.own_session(bridge, store, a)
        self.away(bridge, b)
        self.assertTrue(bridge.submitText("Son maillerimi kontrol et."))
        self.assertEqual((b, {}, []), (bridge.conversationId, bridge.clarification, self.keys(bridge)))
        self.assertEqual([a], bridge.needsYouIds)
        slots_b = (bridge._session_id, dict(bridge._task))
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual([ACCOUNT, OTHER_ACCOUNT], [c["label"] for c in bridge.clarification["choices"]])
        self.assertEqual(("Son maillerimi kontrol et.", session, "awaiting_input"),
                         (bridge.clarification["previous_text"], bridge.sessionId, bridge.coreState))
        sent = self.spy(bridge, "submit_text")
        bridge.chooseOption(OTHER_ACCOUNT)
        self.assertEqual([(OTHER_ACCOUNT, session)], [args[:2] for args in sent])
        self.assertEqual("Son maillerimi kontrol et.", self.model.calls[-1].context["previous_request"])
        bridge.deny()
        self.assertTrue(bridge.openConversation(b))
        self.assertEqual(slots_b, (bridge._session_id, dict(bridge._task)))
        self.assertTrue(bridge.openConversation(a))
        self.assertTrue(bridge.submitText("Son maillerimi kontrol et."))
        self.assertEqual([a], bridge.needsYouIds)
        bridge.dismissClarification()
        self.assertEqual([], bridge.needsYouIds)

    def test_p4b_a_bare_run_task_clarification_parks_restores_and_answers_with_its_session(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        session = self.own_session(bridge, store, a)
        self.deliver(bridge, b, self.waiting_run(session))
        self.assertEqual((b, [], {}), (bridge.conversationId, self.keys(bridge), bridge.clarification))
        self.assertEqual([a], bridge.needsYouIds)
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual(("awaiting_clarification", session, "awaiting_input", ["decision"]),
                         (bridge.task["status"], bridge.sessionId, bridge.coreState, self.keys(bridge)))
        seen = []

        def clarify(session_id, task_id, text):
            # In flight: the answered conversation is not amber (OD5); the answer comes back elsewhere.
            seen.append(((session_id, task_id, text), bridge.busy, bridge.needsYouIds, bridge.runningConversationId))
            self.assertTrue(bridge.openConversation(b))
            return self.waiting_run(session)
        bridge.backend.clarify = clarify
        self.assertTrue(bridge.submitText("Rapor Türkçe olsun."))
        self.assertEqual([((session, "t-clar", "Rapor Türkçe olsun."), True, [], a)], seen)
        self.assertEqual((b, [a]), (bridge.conversationId, bridge.needsYouIds))      # the repeat parks again
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual(("awaiting_clarification", session), (bridge.task["status"], bridge.sessionId))

    def test_p10_a_session_relinked_or_unlinked_after_restore_refuses_every_action(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        for how in ("unlink", "relink"):
            with self.subTest(how=how):
                bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                               task_proposal("gmail_thread_list", limit=5))
                session = self.own_session(bridge, store, a)

                def broken() -> None:
                    if how == "unlink":
                        self.sql("DELETE FROM conversation_sessions WHERE session_id = ?", session)
                    else:
                        self.sql("UPDATE conversation_sessions SET conversation_id = ? WHERE session_id = ?", b, session)

                def mended() -> None:
                    self.sql("DELETE FROM conversation_sessions WHERE session_id = ?", session)
                    self.sql("INSERT INTO conversation_sessions (session_id, conversation_id, created_at) VALUES (?, ?, ?)",
                             session, a, utc_now())
                clarified, cancelled = self.spy(bridge, "clarify", answer={}), self.spy(bridge, "cancel", answer={})
                # The bare-run task clarification: submitText and cancelTask.
                self.deliver(bridge, b, self.waiting_run(session))
                self.assertTrue(bridge.openConversation(a))
                self.assertEqual("awaiting_clarification", bridge.task["status"])
                broken()
                turns, sequence = self.turns(store, a), dict(bridge._sequence)
                self.assertFalse(bridge.submitText("Rapor Türkçe olsun."))           # refused: the draft stays
                bridge.cancelTask()
                self.assertEqual((turns, sequence), (self.turns(store, a), dict(bridge._sequence)))
                self.assertEqual("awaiting_clarification", bridge.task["status"])
                mended()
                # An intent clarification asked within that session: chooseOption (and cancelTask).
                bridge.newSession()
                bridge.useSession(session)
                self.away(bridge, b)
                self.assertTrue(bridge.submitText("Son maillerimi kontrol et."))
                self.assertTrue(bridge.openConversation(a))
                self.assertEqual(session, bridge.sessionId)
                broken()
                sent = self.spy(bridge, "submit_text", answer={})
                turns, sequence = self.turns(store, a), dict(bridge._sequence)
                bridge.chooseOption(OTHER_ACCOUNT)
                bridge.cancelTask()
                self.assertEqual((turns, sequence), (self.turns(store, a), dict(bridge._sequence)))
                self.assertEqual(([], [], []), (sent, clarified, cancelled))
                self.assertTrue(bridge.clarification)                           # nothing was taken from it
                self.assertIn("no longer belongs", self.notes(bridge))

    def test_p10_a_session_that_was_never_parked_keeps_todays_path(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("gmail_thread_list", limit=5), task_proposal("gmail_thread_list", limit=5))
        session = self.own_session(bridge, store, a)
        self.sql("DELETE FROM conversation_sessions WHERE session_id = ?", session)
        bridge.newSession()
        bridge.useSession(session)                                            # History: an unlinked session stays here
        self.assertTrue(bridge.submitText("Son maillerimi kontrol et."))
        sent = self.spy(bridge, "submit_text")
        bridge.chooseOption(OTHER_ACCOUNT)
        self.assertEqual([(OTHER_ACCOUNT, session)], [args[:2] for args in sent])

    def test_p10_a_conversation_attached_to_a_project_starts_fresh(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("gmail_thread_list", limit=5))
        self.own_session(bridge, store, a)
        self.away(bridge, b)
        self.assertTrue(bridge.submitText("Son maillerimi kontrol et."))
        self.assertTrue(bridge.openConversation(a))
        self.assertTrue(bridge.clarification)
        bridge.previewAttach("", "Yeni proje")
        self.assertTrue(bridge.confirmAttach("", "Yeni proje"))
        self.assertEqual((a, "", {}, [], {}), (bridge.conversationId, bridge.sessionId, bridge.clarification,
                                               bridge.needsYouIds, bridge._parked))

    def test_k_the_frozen_keep_rule_keeps_only_a_request_core_still_holds(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        self.assertTrue(bridge.submitText(ANALYSE))
        ticket, shown = bridge._ticket, dict(bridge._approval)
        frozen = {"code": "execution_disabled", "message": "JARVIS is frozen"}
        for status, kept in (("awaiting_approval", True), ("awaiting_cost_override", True), ("verifying", False)):
            with self.subTest(status=status):
                envelope = {"command": "approve", "run_status": "failed", "session": {"id": ticket.session_id},
                            "task": {"id": ticket.task_id, "status": status, "stage": "verify"}, "error": frozen}
                bridge._apply_decision(envelope, "approve", ticket, shown)          # live
                self.assertEqual((kept, [a] if kept else []), (bridge.approvalPending, bridge.needsYouIds))
                parked = _kept_refusal(_project_run(_empty_slots(), envelope), envelope, ticket, shown)   # elsewhere
                self.assertEqual(ticket if kept else None, parked["ticket"])
                bridge._ticket, bridge._approval = ticket, dict(shown)

    # -- P5 / P6 / P7 / P8 / P9 ---------------------------------------------------

    def test_p5_a_restored_ticket_core_no_longer_holds_reads_invalidated_and_nothing_runs(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        self.away(bridge, b)
        self.assertTrue(bridge.submitText(ANALYSE))
        ticket = bridge._parked[a]["ticket"]
        bridge.backend.cancel(ticket.session_id, ticket.task_id)            # resolved outside this window
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual(ticket, bridge._ticket)
        self.assertTrue(bridge.approveRequest(ticket.request_id, ticket.fingerprint))
        self.assertEqual("invalidated", bridge.safety["decision"]["state"])
        self.assertEqual(0, self.execute_calls())

    def test_p6_parked_state_is_memory_only_and_gone_after_a_restart(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        self.assertTrue(bridge.submitText(ANALYSE))
        ticket = bridge._ticket
        before = self.rows()
        self.assertTrue(bridge.openConversation(b))                         # parks A
        self.assertEqual(([a], before), (bridge.needsYouIds, self.rows()))  # the parking wrote nothing
        saved = self.prefs_path.read_text(encoding="utf-8") if self.prefs_path.exists() else ""
        for trace in (ticket.request_id, ticket.fingerprint, "needsYou", "parked"):
            self.assertNotIn(trace, saved)
        again = Bridge(self.backend, UiPreferences(self.prefs_path), sync=True)
        again.refreshConfig()
        self.assertEqual([], again.needsYouIds)
        self.assertTrue(again.openConversation(a))
        self.assertEqual(({}, False, []), (again.approval, again.approvalPending, again.needsYouIds))
        self.assertIsNotNone(SQLiteMemoryStore(self.database).load_pending_approval(ticket.task_id, ticket.session_id))

    def test_p7_a_parked_entry_whose_conversation_is_gone_or_relinked_is_dropped_never_restored(self) -> None:
        for gone in ("delete", "archive", "corrupt", "relink", "unlink"):
            with self.subTest(gone=gone):
                bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
                self.away(bridge, b)
                self.assertTrue(bridge.submitText(ANALYSE))
                session = bridge._parked[a]["session_id"]
                self.assertEqual([a], bridge.needsYouIds)
                {"delete": lambda: self.delete_oob(a), "archive": lambda: self.archive_oob(a),
                 "corrupt": lambda: self.corrupt_oob(a),
                 "relink": lambda: self.sql("UPDATE conversation_sessions SET conversation_id = ? WHERE session_id = ?", b, session),
                 "unlink": lambda: self.sql("DELETE FROM conversation_sessions WHERE session_id = ?", session)}[gone]()
                before, screen_b = self.rows(), self.screen(bridge)
                bridge._refresh_conversations()
                self.assertEqual(({}, []), (bridge._parked, bridge.needsYouIds))
                self.assertEqual(screen_b, self.screen(bridge))
                bridge.openConversation(a)                                       # refused, or opened with nothing
                self.assertEqual(({}, None, {}), (bridge.approval, bridge._ticket, bridge._parked))
                self.assertEqual(before, self.rows())                          # zero writes, no relink

    def test_p8_new_and_chosen_sessions_clear_every_waiting_slot(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("gmail_thread_list", limit=5), task_proposal("gmail_thread_list", limit=5))
        self.away(bridge, b)
        self.assertTrue(bridge.submitText(ANALYSE))
        self.assertTrue(bridge.openConversation(a))
        ticket = bridge._ticket
        bridge.newSession()
        self.assertEqual((None, {}, [], ""), (bridge._ticket, bridge.approval, bridge.needsYouIds, bridge.sessionId))
        other = self.own_session(bridge, store, a)                            # A's second session
        for chosen in (ticket.session_id, other):
            with self.subTest(chosen="same" if chosen == ticket.session_id else "other"):
                bridge.useSession(ticket.session_id)
                self.away(bridge, b)
                self.assertTrue(bridge.submitText("Son maillerimi kontrol et."))
                self.assertTrue(bridge.openConversation(a))
                self.assertTrue(bridge.clarification)
                calls = len(self.model.calls)
                bridge.useSession(chosen)
                self.assertEqual(({}, None, [], chosen), (bridge.clarification, bridge._ticket, bridge.needsYouIds, bridge.sessionId))
                bridge.chooseOption(ACCOUNT)                                   # the stale clarification is gone
                self.assertEqual(calls, len(self.model.calls))
        self.assertIsNotNone(SQLiteMemoryStore(self.database).load_pending_approval(ticket.task_id, ticket.session_id))

    def test_p9_an_incomplete_core_approval_is_amber_but_never_decidable(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        session = self.own_session(bridge, store, a)
        incomplete = {"command": "run", "session": {"id": session}, "task": {"id": "t-inc", "status": "awaiting_approval"},
                      "run_status": "awaiting_approval", "approval": {"scope": {"action": "analyse"}, "reason": "r"}}
        self.deliver(bridge, b, incomplete)
        self.assertEqual([a], bridge.needsYouIds)
        self.assertTrue(bridge.openConversation(a))
        approval = bridge.safety["approval"]
        self.assertEqual((None, False, False, "Approval details unavailable"),
                         (bridge._ticket, approval["complete"], approval["actionable"], approval["hold"]))
        approvals = self.spy(bridge, "approve")
        self.assertFalse(bridge.approveRequest("", ""))
        bridge.approve()
        self.assertEqual(([], [a]), (approvals, bridge.needsYouIds))

    # -- S1 / S1b / S2 ----------------------------------------------------------------

    def test_s1_a_queued_cancel_landing_after_the_parked_approval_removes_it(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective="Yeniden incele."), sync=False)
        session = self.own_session(bridge, store, a)
        self.assertTrue(bridge.submitText("Yeniden incele."))                 # queued: the run is in flight
        self.assertEqual((True, a), (bridge.busy, bridge.runningConversationId))
        held, cancels, real = {}, [], bridge.backend.cancel
        bridge.backend.active_task_id = lambda s: "t-not-yet"                 # Core registers it inside the run
        bridge.backend.cancel = lambda s, t: cancels.append((s, t)) or real(s, held["task"])
        bridge.cancelTask()                                                    # Durdur on A, queued behind the run
        self.assertTrue(bridge.openConversation(b))
        before, turns_b = self.screen(bridge), self.turns(store, b)
        bridge._pool.step()                                                    # the task returns awaiting approval
        self.assertEqual([a], bridge.needsYouIds)
        held["task"] = bridge._parked[a]["ticket"].task_id
        bridge._pool.step()                                                    # then the cancel lands
        self.assertEqual([(session, "t-not-yet")], cancels)
        self.assertNotIn(a, bridge._parked)
        self.assertEqual([], bridge.needsYouIds)
        self.assertEqual("cancelled", SQLiteMemoryStore(self.database).load_task(held["task"]).status.value)
        self.assertEqual((before, turns_b), (self.screen(bridge), self.turns(store, b)))

    def test_s1b_a_cancel_from_a_real_clarification_state_leaves_no_clarification_live_or_parked(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        session = self.own_session(bridge, store, a)
        cancelled = {"command": "cancel", "session": {"id": session}, "task": {"id": "t-clar", "status": "cancelled", "stage": "plan"},
                     "run_status": "cancelled"}
        for where in ("live", "parked"):
            with self.subTest(where=where):
                self.deliver(bridge, b, self.waiting_run(session))
                self.assertTrue(bridge.openConversation(a))
                self.assertEqual(("awaiting_clarification", [a]), (bridge.task["status"], bridge.needsYouIds))
                # OD6: a run envelope answers an intent clarification held alongside, too.
                bridge._clarification = {"request_id": "r", "previous_text": "x", "question": "?", "choices": []}

                def cancel(s, t):
                    if where == "parked":
                        self.assertTrue(bridge.openConversation(b))
                        self.assertEqual("awaiting_clarification", bridge._parked[a]["task"]["status"])
                    return cancelled
                bridge.backend.cancel = cancel
                bridge.cancelTask()
                if where == "parked":
                    self.assertNotIn(a, bridge._parked)
                    self.assertEqual([], bridge.needsYouIds)
                    self.assertTrue(bridge.openConversation(a))
                self.assertEqual(({}, "cancelled" if where == "live" else "", [], []),
                                 (bridge.clarification, bridge.task.get("status", ""), bridge.needsYouIds, self.keys(bridge)))

    def test_s1c_the_newest_envelope_wins_over_what_a_queued_cancel_captured_at_launch(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE), sync=False)
        session = self.own_session(bridge, store, a)
        bridge._apply_run(self.waiting_run(session))                          # A's task waits for an answer
        bridge.backend.clarify = lambda s, t, text: self.waiting_run(session, status="completed")
        self.assertTrue(bridge.submitText("Rapor Türkçe olsun."))            # the answer: queued, in flight
        bridge.backend.active_task_id = lambda s: "t-clar"
        bridge.backend.cancel = lambda s, t: {"command": "cancel", "error": {"code": "task_not_found", "message": "gone"}}
        bridge.cancelTask()                                                    # Durdur: queued behind the answer
        self.assertTrue(bridge.openConversation(b))
        bridge._pool.step()                                                    # the answer lands: completed
        self.assertNotIn(a, bridge._parked)
        bridge._pool.step()                                                    # then the cancel, with no task block
        self.assertEqual(({}, []), (bridge._parked, bridge.needsYouIds))      # never the stale launch snapshot

    def test_s2_cancel_of_a_restored_task_targets_only_its_conversation(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        self.away(bridge, b)
        self.assertTrue(bridge.submitText(ANALYSE))
        ticket = bridge._parked[a]["ticket"]
        cancels = self.spy(bridge, "cancel")
        bridge.cancelTask()                                                    # on B: B has none
        self.assertIn("No cancellable task", bridge.feed[-1]["text"])
        self.assertEqual(([], [a]), (cancels, bridge.needsYouIds))
        self.assertTrue(bridge.openConversation(a))
        bridge.cancelTask()
        self.assertEqual([(ticket.session_id, ticket.task_id)], cancels)
        self.assertEqual("cancelled", SQLiteMemoryStore(self.database).load_task(ticket.task_id).status.value)
        self.assertEqual([], bridge.needsYouIds)

    # -- F1 - F5 --------------------------------------------------------------------

    def test_f1_resume_on_another_conversation_continues_the_paused_background_task_in_place(self) -> None:
        bridge, store, a, b, ticket = self.paused_in_background(sync=False)
        before, turns_b, sequence = self.screen(bridge), self.turns(store, b), bridge._sequence["task"]
        inside = []

        def resume(session_id, task_id):
            inside.append(((session_id, task_id), dict(bridge._run_record), bridge.openRunning, bridge.conversationId,
                           bridge.submitText("B'de bir taslak"), bridge._sequence["task"]))
            return real(session_id, task_id)
        real, bridge.backend.resume = bridge.backend.resume, resume
        bridge.resumeJarvis()
        self.assertEqual((True, a, False, b), (bridge.busy, bridge.runningConversationId, bridge.openRunning, bridge.conversationId))
        self.assertIn("continuing the paused task", bridge.activity[0]["text"])
        self.assertFalse(bridge.submitText("B'de bir taslak"))               # refused, not queued
        self.assertEqual(sequence + 1, bridge._sequence["task"])
        bridge.freezeJarvis()                                                # a second Resume while it is busy:
        bridge.resumeJarvis()                                                # the run IS that task, not "another"
        self.assertEqual("JARVIS resumed", bridge.activity[0]["text"])
        bridge.waitIdle()
        (identity, record, open_running, open_id, submitted, seq), = inside
        self.assertEqual(((ticket.session_id, ticket.task_id), a, ticket.session_id, ticket.task_id),
                         (identity, record["origin"], record["session_id"], record["task_id"]))
        self.assertEqual((False, b, False, sequence + 1), (open_running, open_id, submitted, seq))
        self.assertEqual((b, before, turns_b), (bridge.conversationId, self.screen(bridge), self.turns(store, b)))
        self.assertEqual(1, [kind for kind, _ in self.turns(store, a)].count("result"))
        self.assertEqual(1, self.claude.execute_calls)                        # the executed step never again
        self.assertEqual("completed", SQLiteMemoryStore(self.database).load_task(ticket.task_id).status.value)
        self.assertEqual(({}, None), (bridge._parked, bridge._run_record))

    def test_f2_resume_continues_nothing_the_paused_conversation_cannot_prove(self) -> None:
        for gone in ("deleted", "unlinked"):
            with self.subTest(gone=gone):
                bridge, store, a, b, ticket = self.paused_in_background()
                if gone == "deleted":
                    self.delete_oob(a)
                else:
                    self.sql("DELETE FROM conversation_sessions WHERE session_id = ?", ticket.session_id)
                before, screen_b, resumes = self.rows(), self.screen(bridge), self.spy(bridge, "resume")
                bridge.resumeJarvis()
                self.assertEqual("running", bridge.executionState)
                self.assertEqual(([], {}, before, screen_b), (resumes, bridge._parked, self.rows(), self.screen(bridge)))
                self.assertNotIn("continuing", self.notes(bridge))
                self.assertEqual(1, self.claude.execute_calls)
        # Never a candidate: what Freeze did not pause, or a paused task without its identity.
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
        session = self.own_session(bridge, store, a)
        failed = {"command": "run", "session": {"id": session}, "task": {"id": "t-f", "status": "failed", "stage": "execute"},
                  "run_status": "failed", "error": {"code": "connector_failed", "message": "x"}}
        kinds = {"failed": failed,
                 "cancelled": {**failed, "task": {"id": "t-f", "status": "cancelled"}, "run_status": "cancelled", "error": None},
                 "rejected": {**failed, "task": {"id": "t-f", "status": "rejected"}, "run_status": "rejected", "error": None},
                 "unknown": {**failed, "error": {"code": "execution_unknown_outcome", "message": "x"}},
                 "blocked before registration": self.paused_run(session, stage="understand"),
                 "paused without a session": self.paused_run(None)}
        for kind, run in kinds.items():
            with self.subTest(kind=kind):
                bridge.freezeJarvis()
                self.assertTrue(bridge.openConversation(a))
                bridge.newSession()                                            # no session to inherit
                self.deliver(bridge, b, run)
                before, screen_b, resumes = self.rows(), self.screen(bridge), self.spy(bridge, "resume")
                bridge.resumeJarvis()
                self.assertEqual(([], {}, before, screen_b), (resumes, bridge._parked, self.rows(), self.screen(bridge)))
                self.assertNotIn("continuing", bridge.activity[0]["text"])

    def test_f2u_an_unconfirmed_resume_processes_no_candidate(self) -> None:
        bridge, store, a, b, ticket = self.paused_in_background()
        parked, before, turns = dict(bridge._parked), self.rows(), (self.turns(store, a), self.turns(store, b))
        resumes = self.spy(bridge, "resume")
        bridge.backend.set_config = lambda key, raw: {"command": "config-set", "error": {"code": "config_io_failed", "message": "busy"}}
        bridge.resumeJarvis()
        self.assertEqual("unconfirmed", bridge.executionState)
        self.assertEqual(("error", "JARVIS state unconfirmed: the request to resume JARVIS was not confirmed. The store reports execution frozen."),
                         (bridge.feed[-1]["kind"], bridge.feed[-1]["text"]))
        self.assertEqual(([], parked, before), (resumes, bridge._parked, self.rows()))
        self.assertEqual(turns, (self.turns(store, a), self.turns(store, b)))   # the error is screen feed only

    def test_f3_resume_while_another_run_is_busy_continues_nothing_now_or_later(self) -> None:
        bridge, store, a, b, ticket = self.paused_in_background()
        core = SQLiteMemoryStore(self.database)
        task_before = core.load_task(ticket.task_id)
        resumes, seen = self.spy(bridge, "resume"), []

        def resume_while_chatting(task) -> None:
            self.model.on_complete = None
            bridge.resumeJarvis()                                              # B's chat is the busy run
            seen.append((bridge.busy, bridge.executionState, bridge.activity[0]["text"]))
        self.model.on_complete = resume_while_chatting
        self.assertTrue(bridge.submitText("Merhaba"))                          # frozen JARVIS still chats
        self.assertEqual([(True, "running", "JARVIS resumed - the paused task was not continued: another run is in progress")], seen)
        self.assertFalse(bridge.busy)
        task_after = core.load_task(ticket.task_id)
        self.assertEqual((task_before.status, task_before.stage), (task_after.status, task_after.stage))
        self.assertEqual(([], 1), (resumes, self.claude.execute_calls))
        self.assertIn(a, bridge._parked)                                       # still there for a later Resume

    def test_f4_the_open_screens_paused_task_wins_and_two_parked_ones_continue_nothing(self) -> None:
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE))
        c = store.create_conversation().id
        bridge._refresh_conversations()
        session_a = self.own_session(bridge, store, a)
        self.assertTrue(bridge.openConversation(c))
        session_c = self.own_session(bridge, store, c)
        bridge.freezeJarvis()
        self.deliver(bridge, b, self.paused_run(session_c, task="t-c"))
        self.assertTrue(bridge.openConversation(a))
        self.deliver(bridge, b, self.paused_run(session_a, task="t-a"))
        self.assertEqual({a, c}, set(bridge._parked))
        resumes = self.spy(bridge, "resume", answer={"command": "resume", "error": {"code": "task_not_found", "message": "x"}})
        bridge.resumeJarvis()
        self.assertEqual([], resumes)
        self.assertIn("more than one task is paused", bridge.activity[0]["text"])
        self.assertTrue(bridge.openConversation(a))                            # A's paused task on screen now
        bridge.resumeJarvis()
        self.assertEqual([(session_a, "t-a")], resumes)
        self.assertIn(c, bridge._parked)

    def test_f5_a_background_resume_whose_conversation_goes_away_mid_run_is_dropped(self) -> None:
        for gone in ("delete", "corrupt"):
            for shape in ("sessionless", "sessionful"):
                with self.subTest(gone=gone, shape=shape):
                    bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE))
                    session = self.own_session(bridge, store, a)
                    bridge.freezeJarvis()
                    self.deliver(bridge, b, self.paused_run(session))
                    self.assertIn(a, bridge._parked)
                    before, screen_b = {}, self.screen(bridge)

                    def resume(session_id, task_id):
                        (self.delete_oob if gone == "delete" else self.corrupt_oob)(a)
                        before.update(self.rows())
                        if shape == "sessionless":
                            return {"command": "worker", "error": {"code": "connector_failed", "message": "Unexpected internal failure"}}
                        return {"command": "resume", "session": {"id": session}, "task": {"id": task_id, "status": "completed"},
                                "run_status": "completed", "result": {"summary": "bitti"}}
                    bridge.backend.resume = resume
                    bridge.resumeJarvis()
                    self.assertEqual((before, screen_b), (self.rows(), self.screen(bridge)))
                    self.assertEqual(({}, None), (bridge._parked, bridge._run_record))
                    self.assertNotIn("bitti", "\n".join(e["text"] for e in bridge.feed))

    # -- N1 -------------------------------------------------------------------------

    def test_n1_needs_you_ids_follow_real_waiting_state_and_say_every_change(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge, store, a, b = self.two(task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("gmail_thread_list", limit=5),
                                       task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE),
                                       task_proposal("code_analysis", objective=ANALYSE))
        said: list = []
        bridge.sidebarChanged.connect(lambda: said.append(bridge.needsYouIds))

        def step(action, expected) -> None:
            said.clear()
            action()
            self.assertEqual(expected, bridge.needsYouIds)
            self.assertTrue(said and said[-1] == expected, said)

        self.away(bridge, b)
        step(lambda: bridge.submitText(ANALYSE), [a])                         # parked on delivery
        step(lambda: bridge.openConversation(a), [a])                         # restored: the open one's
        step(bridge.approve, [])                                              # decided
        step(lambda: bridge.submitText(ANALYSE), [a])
        step(bridge.deny, [])                                                 # rejected
        step(lambda: bridge.submitText(ANALYSE), [a])
        step(bridge.cancelTask, [])                                           # cancelled
        step(lambda: bridge.submitText("Son maillerimi kontrol et."), [a])
        step(bridge.dismissClarification, [])                                 # dismissed
        step(lambda: bridge.submitText(ANALYSE), [a])
        step(bridge.newSession, [])                                           # reset
        # A paused task alone is never amber; a finished, failed or unknown one neither.
        session = self.own_session(bridge, store, a)
        for run in (self.paused_run(session), {**self.waiting_run(session, status="completed"), "run_status": "completed"},
                    {**self.paused_run(session), "error": {"code": "execution_unknown_outcome", "message": "x"}}):
            self.assertTrue(bridge.openConversation(a))
            self.deliver(bridge, b, run)
            self.assertEqual([], bridge.needsYouIds)
        # OD5: the conversation a run is answering is not amber; another's waiting request stays.
        c = store.create_conversation().id
        bridge._refresh_conversations()
        self.assertTrue(bridge.openConversation(c))
        self.away(bridge, a)
        step(lambda: bridge.submitText(ANALYSE), [c])                         # C's approval parked; A open
        bridge.newSession()
        bridge.useSession(session)
        self.deliver(bridge, b, self.waiting_run(session))
        self.assertTrue(bridge.openConversation(a))
        self.assertEqual([a, c] if a < c else [c, a], bridge.needsYouIds)
        inside = []
        bridge.backend.clarify = lambda s, t, text: inside.append(bridge.needsYouIds) or self.waiting_run(session, status="completed")
        step(lambda: bridge.submitText("Cevap"), [c])                         # run start / end of the origin
        self.assertEqual([[c]], inside)
        step(lambda: self.delete_oob(c) or bridge._refresh_conversations(), [])   # dropped
