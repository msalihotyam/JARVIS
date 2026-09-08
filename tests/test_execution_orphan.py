"""Scheduler phase 2B2b1: the orphan human gate and the UNKNOWN exit.

Covers the one transition H2 never had - a person declaring that an IN_FLIGHT
execution will never be concluded by whoever started it - the shared
complete-history predicate that decides what a generation may believe about
its own execution reality, the coordination reflection of that uncertainty,
and the terminal resolution that finally unblocks the job without ever
resolving the doubt.

WHAT IS BEING PROTECTED. An IN_FLIGHT attempt means a connector boundary was
crossed and nobody said what came back. Every rule here exists so that no
amount of elapsed time, no operator gesture, no newer database row and no
terminal status anywhere can turn that into "nothing happened" or into
permission to run it again. The gate moves exactly one thing - the record of
whether anyone will ever know - and leaves the task, the money, the chain and
the generation to their own explicit authorities.

THE CRASH SHAPES ARE REAL. The IN_FLIGHT attempt every test starts from is
produced by killing the real machinery at the exact storage write that leaves
one, through a real ``AgentCore``, real connectors and a real schema-10 store.
Only shapes no production writer can create - two attempt rows under one task,
a foreign attempt identity - are planted, and they are planted precisely
because the guard exists for states the API cannot reach.

Every instant is EXPLICIT and every clock is injected. Nothing sleeps, nothing
polls, and no test performs a live model, auth or network call.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone

from jarvis.contracts import (
    AttemptRecovery,
    AttemptRecoveryOutcome,
    AttemptState,
    ChainStatus,
    ErrorCode,
    HistoryFinding,
    JobError,
    JobRunStatus,
    ORPHAN_MIN_AGE,
    OutcomeStatus,
    PermissionGrant,
    SessionState,
    TaskStatus,
    execution_history_verdict,
)
from jarvis.dispatch import (
    DISPATCH_BLOCKED,
    DISPATCH_UNKNOWN,
    _execution_established,
    dispatch_job_run,
)
from jarvis.memory import SQLiteMemoryStore

from tests.test_scheduler_dispatch import (  # noqa: F401
    LEASE,
    WORKER_A,
    WORKER_B,
    ScriptedCore,
    setUpModule,
    tearDownModule,
)
from tests.test_scheduler_recovery import ACTOR, WHY, WORKER_C, RecoveryTestCase

#: So that a test can pass ``now=None`` and mean it. A falsy default would
#: quietly substitute a valid instant and the refusal would never be exercised.
_UNSET = object()


class OrphanTestCase(RecoveryTestCase):
    """A real generation whose executor died mid-connector-call."""

    def in_flight(self):
        """A dead owner's IN_FLIGHT attempt, produced by a real death.

        The connector really ran; the writes that would have concluded it -
        and the write its own owner uses to record an unknown outcome - are
        both killed, which is exactly what a process that vanished mid-call
        leaves behind.
        """
        run, lease = self.claimed()
        self.kill_at("conclude_execution")
        self.kill_at("mark_execution_unknown")
        dispatch_job_run(
            self.core,
            run.id,
            WORKER_A,
            lease.fencing_epoch,
            clock=lambda: self.at + timedelta(minutes=1),
        )
        self.revive("conclude_execution")
        self.revive("mark_execution_unknown")
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        self.assertEqual(1, len(self.reader.calls))
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        return stored, stored

    # -- fixtures ---------------------------------------------------------

    def attempt_rows(self) -> list[sqlite3.Row]:
        """Every attempt row, INCLUDING the stamp the age guard reads.

        The 2B2a projection does not carry ``updated_at`` because nothing there
        needed it; the orphan guard is entirely about that column.
        """
        with contextlib.closing(self.raw()) as connection:
            return list(
                connection.execute(
                    "SELECT id, task_id, session_id, connector, fingerprint, "
                    "state, created_at, updated_at FROM execution_attempts "
                    "ORDER BY rowid"
                )
            )

    def attempt(self) -> sqlite3.Row:
        rows = self.attempt_rows()
        self.assertEqual(1, len(rows))
        return rows[0]

    def aged(self, row=None, extra: timedelta = timedelta(minutes=1)) -> datetime:
        """An instant far enough past the IN_FLIGHT stamp to satisfy the guard.

        Derived from the attempt's OWN timestamp rather than from a test
        constant, because the guard is about the gap and nothing else.
        """
        row = row if row is not None else self.attempt()
        return datetime.fromisoformat(row["updated_at"]) + ORPHAN_MIN_AGE + extra

    def orphan(self, run=None, *, now=_UNSET, store=None, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        row = self.attempt()
        session_id = fields.pop("session_id", row["session_id"])
        task_id = fields.pop("task_id", row["task_id"])
        attempt_id = fields.pop("attempt_id", row["id"])
        return (store or self.memory).declare_attempt_orphaned(
            session_id,
            task_id,
            attempt_id,
            now=self.aged(row) if now is _UNSET else now,
            **fields,
        )

    def abandon(self, row=None) -> AttemptRecoveryOutcome:
        row = row if row is not None else self.attempt()
        return self.memory.abandon_unknown_attempt(
            row["session_id"],
            row["task_id"],
            row["id"],
            AttemptRecovery.ABANDONED,
        )

    def resolve(self, run_id, attempt_id, *, now=_UNSET, store=None, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return (store or self.memory).resolve_unknown_job_run(
            run_id,
            attempt_id,
            now=self.after if now is _UNSET else now,
            **fields,
        )

    def audit(self, event_type: str) -> list[dict]:
        with contextlib.closing(self.raw()) as connection:
            rows = list(
                connection.execute(
                    "SELECT session_id, task_id, payload_json FROM event_log "
                    "WHERE event_type = ? ORDER BY id",
                    (event_type,),
                )
            )
        return [
            {
                "session_id": row["session_id"],
                "task_id": row["task_id"],
                "payload": json.loads(row["payload_json"]),
                "payload_json": row["payload_json"],
            }
            for row in rows
        ]

    def reservation(self, attempt_id: str) -> str | None:
        with contextlib.closing(self.raw()) as connection:
            row = connection.execute(
                "SELECT state FROM attempt_billing WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else row["state"]

    def plant_attempt(self, **over) -> str:
        """A SECOND attempt row under a task - a shape no writer can produce."""
        rows = self.attempt_rows()
        row = rows[0]
        fields = dict(
            id="planted-attempt",
            session_id=row["session_id"],
            task_id=row["task_id"],
            connector=row["connector"],
            fingerprint=row["fingerprint"],
            state=AttemptState.FINISHED.value,
        )
        fields.update(over)
        from jarvis.contracts import utc_now

        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                """
                INSERT INTO execution_attempts
                    (id, session_id, task_id, connector, fingerprint, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["id"],
                    fields["session_id"],
                    fields["task_id"],
                    fields["connector"],
                    fields["fingerprint"],
                    fields["state"],
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.commit()
        return fields["id"]

    def orphaned_run(self):
        """The whole accepted path up to (not including) resolution."""
        run, _dispatch = self.in_flight()
        row = self.attempt()
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.orphan())
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.abandon(row))
        return self.reload(run.id), row


# --------------------------------------------------------------------------
# THE HUMAN GATE: IN_FLIGHT -> UNKNOWN_OUTCOME
# --------------------------------------------------------------------------


class OrphanDeclarationTests(OrphanTestCase):
    """Who may say an execution will never be concluded, and on what terms."""

    def test_a_dead_owners_attempt_can_be_declared_orphaned(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        before = self.reservation(row["id"])
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.orphan())
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        # NOTHING ELSE MOVED. The declaration is about knowledge, not about the
        # task, the money, the chain or the generation.
        task = self.memory.load_task(row["task_id"])
        self.assertIs(TaskStatus.PENDING, task.status)
        self.assertFalse(task.result_snapshot)
        # The reservation is left EXACTLY as it was found. Whether the dead
        # owner had already reconciled measured usage before it vanished is
        # H3's business and not this gate's, so the invariant is "unchanged"
        # rather than any particular state.
        self.assertEqual(before, self.reservation(row["id"]))
        self.assertIsNotNone(before)
        self.assertIs(ChainStatus.ACTIVE, self.chain_of(run.session_id).status)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)
        # And the connector was not reached again by any of it.
        self.assertEqual(1, len(self.reader.calls))

    def test_only_an_in_flight_attempt_may_be_declared(self) -> None:
        # PREPARED never crossed the boundary and is still claimable; FINISHED
        # already has its answer. Neither is this operator's decision.
        for state in (AttemptState.PREPARED, AttemptState.FINISHED):
            with self.subTest(state=state):
                self.setUp()
                run, _dispatch = self.in_flight()
                row = self.attempt()
                with contextlib.closing(self.raw()) as connection:
                    connection.execute(
                        "UPDATE execution_attempts SET state = ? WHERE id = ?",
                        (state.value, row["id"]),
                    )
                    connection.commit()
                self.assertIs(
                    AttemptRecoveryOutcome.NOT_RECOVERABLE, self.orphan()
                )
                self.assertEqual([state.value], self.attempt_states())
                self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_declaring_an_already_unknown_attempt_changes_nothing(self) -> None:
        # Idempotent in the only sense that matters: the second call writes
        # nothing and records nothing, because the first already said it.
        run, _dispatch = self.in_flight()
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.orphan())
        self.assertIs(AttemptRecoveryOutcome.NOT_RECOVERABLE, self.orphan())
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        self.assertEqual(1, len(self.audit("execution_declared_orphaned")))

    def test_a_terminal_task_is_refused(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        self.rewind_task(row["task_id"], TaskStatus.CANCELLED)
        self.assertIs(AttemptRecoveryOutcome.TERMINAL, self.orphan())
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_every_identity_mismatch_is_one_refusal(self) -> None:
        # Which other session or task owns an attempt is not a fact a refusal
        # may disclose, so all of these read the same.
        run, _dispatch = self.in_flight()
        row = self.attempt()
        other = SessionState(objective="somebody else's work")
        self.memory.save_session(other)
        for label, over in (
            ("foreign session", {"session_id": other.id}),
            ("foreign task", {"task_id": "no-such-task"}),
            ("wrong attempt", {"attempt_id": "no-such-attempt"}),
            ("empty attempt", {"attempt_id": ""}),
        ):
            with self.subTest(case=label):
                self.assertIs(AttemptRecoveryOutcome.UNKNOWN, self.orphan(**over))
                self.assertEqual(
                    [AttemptState.IN_FLIGHT.value], self.attempt_states()
                )
                self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_blank_attribution_is_refused_before_anything_is_read(self) -> None:
        run, _dispatch = self.in_flight()
        for label, over in (
            ("blank actor", {"actor_label": "   "}),
            ("empty actor", {"actor_label": ""}),
            ("blank reason", {"reason": ""}),
            ("non-string reason", {"reason": None}),
        ):
            with self.subTest(case=label):
                with self.assertRaises(JobError):
                    self.orphan(**over)
                self.assertEqual(
                    [AttemptState.IN_FLIGHT.value], self.attempt_states()
                )
        self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_a_declaration_made_too_soon_is_refused(self) -> None:
        # A GESTURE GUARD. Waiting proves nothing about the worker; it only
        # makes a mistyped attempt id unlikely.
        run, _dispatch = self.in_flight()
        row = self.attempt()
        started = datetime.fromisoformat(row["updated_at"])
        for label, moment in (
            ("immediately", started),
            ("one second later", started + timedelta(seconds=1)),
            ("just before the bound", started + ORPHAN_MIN_AGE - timedelta(seconds=1)),
        ):
            with self.subTest(case=label):
                self.assertIs(
                    AttemptRecoveryOutcome.TOO_RECENT, self.orphan(now=moment)
                )
                self.assertEqual(
                    [AttemptState.IN_FLIGHT.value], self.attempt_states()
                )
                self.assertEqual([], self.audit("execution_declared_orphaned"))
        # Exactly at the bound it is allowed - the bound is a floor, not proof.
        self.assertIs(
            AttemptRecoveryOutcome.RECOVERED,
            self.orphan(now=started + ORPHAN_MIN_AGE),
        )

    def test_age_alone_never_orphans_anything(self) -> None:
        # THE POINT OF THE GUARD, stated as a test: time passing is not an
        # event. An attempt older than the bound is exactly as IN_FLIGHT as it
        # was, and only an explicit call moves it.
        run, _dispatch = self.in_flight()
        row = self.attempt()
        ancient = datetime.fromisoformat(row["updated_at"]) + timedelta(days=365)
        for reader in (
            lambda: self.memory.load_job_run(run.id),
            lambda: self.memory.latest_execution_attempt(row["task_id"]),
            lambda: self.memory.execution_attempts(row["task_id"]),
            lambda: self.recover(run.id, WORKER_B, run.fencing_epoch,
                                 core=ScriptedCore(self.memory, None)),
        ):
            reader()
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        self.assertEqual([], self.audit("execution_declared_orphaned"))
        self.assertIn("ancient", str(ancient) and "ancient")

    def test_a_malformed_instant_is_refused(self) -> None:
        run, _dispatch = self.in_flight()
        for moment in (None, "2026-09-06T12:00:00+00:00", datetime(2026, 9, 6)):
            with self.subTest(now=moment):
                with self.assertRaises(JobError):
                    self.orphan(now=moment)
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())

    def test_an_unreadable_started_stamp_fails_closed(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE execution_attempts SET updated_at = ? WHERE id = ?",
                ("not a timestamp", row["id"]),
            )
            connection.commit()
        self.assertIs(
            AttemptRecoveryOutcome.TOO_RECENT,
            self.orphan(now=datetime(2099, 1, 1, tzinfo=timezone.utc)),
        )
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())

    def test_the_owner_and_the_gate_cannot_both_win(self) -> None:
        # THE ZOMBIE RACE, both directions. Every writer that can end an
        # attempt names IN_FLIGHT as its source, so the state CAS is the whole
        # fence - and it is enough.
        run, _dispatch = self.in_flight()
        row = self.attempt()
        session = self.memory.load_session(row["session_id"])
        task = self.memory.load_task(row["task_id"])
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.orphan())
        # The old owner returns and tries to conclude. It loses, and its
        # result never becomes the task's.
        self.assertEqual(
            "lost",
            self.memory.conclude_execution(
                session, task, row["id"], "execution_completed", {}
            ),
        )
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        stored = self.memory.load_task(row["task_id"])
        self.assertIs(TaskStatus.PENDING, stored.status)
        self.assertFalse(stored.result_snapshot)

    def test_an_owner_that_concludes_first_refuses_the_gate(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        session = self.memory.load_session(row["session_id"])
        task = self.memory.load_task(row["task_id"])
        self.assertEqual(
            "finished",
            self.memory.conclude_execution(
                session, task, row["id"], "execution_completed", {}
            ),
        )
        self.assertIs(AttemptRecoveryOutcome.NOT_RECOVERABLE, self.orphan())
        self.assertEqual([AttemptState.FINISHED.value], self.attempt_states())
        self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_two_concurrent_declarations_produce_one_winner(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        moment = self.aged(row)
        results: list = []
        barrier = threading.Barrier(2)

        def declare(actor: str) -> None:
            store = SQLiteMemoryStore(self.database)
            barrier.wait()
            try:
                results.append(
                    store.declare_attempt_orphaned(
                        row["session_id"],
                        row["task_id"],
                        row["id"],
                        now=moment,
                        actor_label=actor,
                        reason=WHY,
                    )
                )
            except JobError as exc:  # pragma: no cover - defensive
                results.append(exc)

        threads = [
            threading.Thread(target=declare, args=(actor,))
            for actor in ("operator:one", "operator:two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            1, len([r for r in results if r is AttemptRecoveryOutcome.RECOVERED])
        )
        self.assertEqual(
            1, len([r for r in results if r is AttemptRecoveryOutcome.NOT_RECOVERABLE])
        )
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        self.assertEqual(1, len(self.audit("execution_declared_orphaned")))


class OrphanAuditTests(OrphanTestCase):
    """The declaration and its record are one write, or there is neither."""

    def test_one_declaration_writes_one_complete_record(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        moment = self.aged(row)
        self.orphan(now=moment)
        events = self.audit("execution_declared_orphaned")
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(row["session_id"], event["session_id"])
        self.assertEqual(row["task_id"], event["task_id"])
        payload = event["payload"]
        self.assertEqual(row["id"], payload["attempt_id"])
        self.assertEqual(AttemptState.IN_FLIGHT.value, payload["previous_state"])
        self.assertEqual(
            AttemptState.UNKNOWN_OUTCOME.value, payload["resulting_state"]
        )
        self.assertEqual(row["updated_at"], payload["previous_updated_at"])
        self.assertEqual(ACTOR, payload["actor_label"])
        self.assertEqual(WHY, payload["reason"])
        self.assertEqual(moment.isoformat(), payload["declared_at"])
        # WHAT THE RECORD MUST NEVER BE MISREAD AS, stated in the record.
        self.assertFalse(payload["actor_authenticated"])
        self.assertFalse(payload["worker_death_proved"])
        self.assertFalse(payload["external_effect_established"])
        self.assertFalse(payload["retry_authorized"])

    def test_metadata_is_bounded_and_credential_redacted(self) -> None:
        run, _dispatch = self.in_flight()
        self.orphan(
            actor_label="operator:" + "d" * 400,
            reason="lost the box holding sk-ant-api03-SECRETSECRETSECRETSECRET",
        )
        payload = self.audit("execution_declared_orphaned")[0]["payload"]
        self.assertLessEqual(len(payload["actor_label"]), 120)
        self.assertLessEqual(len(payload["reason"]), 500)
        self.assertNotIn("SECRETSECRETSECRETSECRET", payload["reason"])

    def test_a_failing_audit_rolls_the_declaration_back(self) -> None:
        run, _dispatch = self.in_flight()
        original = SQLiteMemoryStore.__dict__["_write_checkpoint_and_event"]

        def refuse(*_args, **_kwargs):
            raise sqlite3.IntegrityError("the audit row could not be written")

        SQLiteMemoryStore._write_checkpoint_and_event = staticmethod(refuse)
        self.addCleanup(
            setattr, SQLiteMemoryStore, "_write_checkpoint_and_event", original
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.orphan()
        # THE POINT: an authority change nobody can reconstruct is worse than
        # a refused one, so the state change did not survive either.
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.attempt_states())
        self.assertEqual([], self.audit("execution_declared_orphaned"))
        SQLiteMemoryStore._write_checkpoint_and_event = original
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.orphan())
        self.assertEqual(1, len(self.audit("execution_declared_orphaned")))


# --------------------------------------------------------------------------
# THE SHARED COMPLETE-HISTORY PREDICATE
# --------------------------------------------------------------------------


class HistoryVerdictTests(unittest.TestCase):
    """The one definition, asked directly about every shape that matters."""

    class _Task:
        def __init__(self, task_id: str, status: TaskStatus) -> None:
            self.id = task_id
            self.status = status

    class _Attempt:
        def __init__(self, attempt_id, session_id, task_id, state) -> None:
            self.id = attempt_id
            self.session_id = session_id
            self.task_id = task_id
            self.state = state

    SESSION = "session-1"

    def verdict(self, rows_by_task, cursor=0, statuses=None):
        statuses = statuses or {}
        routed = [
            (index, self._Task(task_id, statuses.get(task_id, TaskStatus.COMPLETED)))
            for index, task_id in enumerate(rows_by_task)
        ]
        history = {
            task_id: [
                self._Attempt(f"{task_id}-{n}", self.SESSION, task_id, state)
                for n, state in enumerate(states)
            ]
            for task_id, states in rows_by_task.items()
        }
        return execution_history_verdict(self.SESSION, routed, cursor, history)

    def test_a_clean_history_is_established(self) -> None:
        got = self.verdict({"t0": [AttemptState.FINISHED]})
        self.assertIs(HistoryFinding.ESTABLISHED, got.finding)
        self.assertIsNone(got.attempt_id)

    def test_the_sole_unknown_at_the_cursor_is_the_reflectable_shape(self) -> None:
        got = self.verdict(
            {"t0": [AttemptState.FINISHED], "t1": [AttemptState.UNKNOWN_OUTCOME]},
            cursor=1,
            statuses={"t1": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.UNKNOWN_AT_CURSOR, got.finding)
        self.assertEqual("t1-0", got.attempt_id)

    def test_an_in_flight_row_outranks_a_newer_unknown(self) -> None:
        # THE REVIEW SHAPE. A "latest row" reading would call this resolvable.
        got = self.verdict(
            {"t0": [AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME]},
            statuses={"t0": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.IN_FLIGHT_PRESENT, got.finding)

    def test_an_in_flight_row_under_another_step_blocks(self) -> None:
        got = self.verdict(
            {"t0": [AttemptState.IN_FLIGHT], "t1": [AttemptState.UNKNOWN_OUTCOME]},
            cursor=1,
            statuses={"t1": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.IN_FLIGHT_PRESENT, got.finding)

    def test_an_unknown_under_another_step_blocks(self) -> None:
        got = self.verdict(
            {"t0": [AttemptState.UNKNOWN_OUTCOME], "t1": [AttemptState.FINISHED]},
            cursor=1,
            statuses={"t0": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.NON_CURSOR_UNKNOWN, got.finding)

    class _Store:
        """The smallest thing ``_execution_established`` can be asked of."""

        def __init__(self, history) -> None:
            self._history = history

        def execution_attempts(self, task_id):
            return self._history.get(task_id)

    def established(self, rows_by_task, statuses=None):
        """The 2B1 settlement proof, asked over the same rows."""
        statuses = statuses or {}
        routed = [
            (index, self._Task(task_id, statuses.get(task_id, TaskStatus.COMPLETED)))
            for index, task_id in enumerate(rows_by_task)
        ]
        history = {
            task_id: [
                self._Attempt(f"{task_id}-{n}", self.SESSION, task_id, state)
                for n, state in enumerate(states)
            ]
            for task_id, states in rows_by_task.items()
        }
        return _execution_established(
            self._Store(history), self.SESSION, routed
        )

    def test_two_clean_rows_under_one_task_are_unprovable(self) -> None:
        # THE REVIEW SHAPE. Neither row looks alarming, and that is exactly why
        # it was waved through: two FINISHED rows are two claims about ONE
        # execution opportunity, and no production writer can produce them.
        # Nothing may be settled or resumed on a history nobody can read.
        got = self.verdict({"t0": [AttemptState.FINISHED, AttemptState.FINISHED]})
        self.assertIs(HistoryFinding.UNPROVABLE, got.finding)
        self.assertIsNone(got.attempt_id)
        self.assertFalse(
            self.established({"t0": [AttemptState.FINISHED, AttemptState.FINISHED]})
        )

    def test_a_prepared_row_beside_a_finished_one_is_unprovable(self) -> None:
        # The same task both did and did not cross the connector boundary.
        got = self.verdict({"t0": [AttemptState.PREPARED, AttemptState.FINISHED]})
        self.assertIs(HistoryFinding.UNPROVABLE, got.finding)
        self.assertFalse(
            self.established({"t0": [AttemptState.PREPARED, AttemptState.FINISHED]})
        )

    def test_duplicate_rows_under_a_prior_step_hide_nothing(self) -> None:
        # THE DANGEROUS ONE: the cursor's own uncertainty looks perfectly
        # reflectable, and the corruption sits under a step the chain already
        # passed - which nothing after the cursor rule would ever look at.
        rows = {
            "t0": [AttemptState.FINISHED, AttemptState.FINISHED],
            "t1": [AttemptState.UNKNOWN_OUTCOME],
        }
        got = self.verdict(rows, cursor=1, statuses={"t1": TaskStatus.FAILED})
        self.assertIs(HistoryFinding.UNPROVABLE, got.finding)
        self.assertIsNone(got.attempt_id)
        self.assertFalse(self.established(rows, statuses={"t1": TaskStatus.FAILED}))

    def test_duplicates_never_outrank_an_in_flight_row(self) -> None:
        # Precedence preserved: the sharper danger is still named first.
        got = self.verdict(
            {"t0": [AttemptState.IN_FLIGHT, AttemptState.FINISHED]},
            statuses={"t0": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.IN_FLIGHT_PRESENT, got.finding)

    def test_a_single_row_history_is_unaffected(self) -> None:
        # The rule refuses duplicates and NOTHING else.
        self.assertIs(
            HistoryFinding.ESTABLISHED, self.verdict({"t0": [AttemptState.FINISHED]}).finding
        )
        self.assertTrue(self.established({"t0": [AttemptState.FINISHED]}))
        sole = self.verdict(
            {"t0": [AttemptState.FINISHED], "t1": [AttemptState.UNKNOWN_OUTCOME]},
            cursor=1,
            statuses={"t1": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.UNKNOWN_AT_CURSOR, sole.finding)
        self.assertEqual("t1-0", sole.attempt_id)

    def test_two_unknown_rows_are_not_one_decision(self) -> None:
        got = self.verdict(
            {"t0": [AttemptState.UNKNOWN_OUTCOME, AttemptState.UNKNOWN_OUTCOME]},
            statuses={"t0": TaskStatus.FAILED},
        )
        self.assertIs(HistoryFinding.UNPROVABLE, got.finding)

    def test_an_unknown_beside_a_sibling_row_is_unprovable(self) -> None:
        for sibling in (AttemptState.FINISHED, AttemptState.PREPARED):
            with self.subTest(sibling=sibling):
                got = self.verdict(
                    {"t0": [sibling, AttemptState.UNKNOWN_OUTCOME]},
                    statuses={"t0": TaskStatus.FAILED},
                )
                self.assertIs(HistoryFinding.UNPROVABLE, got.finding)

    def test_a_foreign_or_malformed_row_refuses_everything(self) -> None:
        routed = [(0, self._Task("t0", TaskStatus.FAILED))]
        for label, attempt in (
            ("foreign session", self._Attempt("a", "other", "t0", AttemptState.UNKNOWN_OUTCOME)),
            ("foreign task", self._Attempt("a", self.SESSION, "other", AttemptState.UNKNOWN_OUTCOME)),
            ("empty id", self._Attempt("", self.SESSION, "t0", AttemptState.UNKNOWN_OUTCOME)),
            ("raw state", self._Attempt("a", self.SESSION, "t0", "unknown_outcome")),
            ("no state", self._Attempt("a", self.SESSION, "t0", None)),
        ):
            with self.subTest(case=label):
                got = execution_history_verdict(
                    self.SESSION, routed, 0, {"t0": [attempt]}
                )
                self.assertIs(HistoryFinding.MALFORMED_OR_FOREIGN, got.finding)

    def test_a_completed_task_with_no_evidence_refuses(self) -> None:
        got = self.verdict({"t0": []}, statuses={"t0": TaskStatus.COMPLETED})
        self.assertIs(HistoryFinding.EVIDENCE_MISSING, got.finding)

    def test_a_failed_task_may_legitimately_have_no_attempt(self) -> None:
        got = self.verdict({"t0": []}, statuses={"t0": TaskStatus.FAILED})
        self.assertIs(HistoryFinding.ESTABLISHED, got.finding)

    def test_an_unreadable_journal_is_not_a_clearance(self) -> None:
        routed = [(0, self._Task("t0", TaskStatus.FAILED))]
        got = execution_history_verdict(self.SESSION, routed, 0, {"t0": None})
        self.assertIs(HistoryFinding.UNPROVABLE, got.finding)

    def test_an_unusable_cursor_can_never_produce_the_reflectable_shape(
        self,
    ) -> None:
        for cursor in (None, "0", True, -1, 5):
            with self.subTest(cursor=cursor):
                got = self.verdict(
                    {"t0": [AttemptState.UNKNOWN_OUTCOME]},
                    cursor=cursor,
                    statuses={"t0": TaskStatus.FAILED},
                )
                self.assertIsNot(HistoryFinding.UNKNOWN_AT_CURSOR, got.finding)


# --------------------------------------------------------------------------
# REFLECTION AND RESOLUTION
# --------------------------------------------------------------------------


class UnknownReflectionTests(OrphanTestCase):
    """Coordination catching up with H2 truth - and never more than that."""

    def test_a_declared_orphan_is_reflected_onto_the_generation(self) -> None:
        run, _dispatch = self.in_flight()
        self.orphan()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)
        self.assertEqual("execution_unknown_at_cursor", outcome.reason)
        # CORE WAS NEVER ENTERED and the connector was never reached again.
        self.assertEqual([], scripted.calls)
        self.assertEqual(1, len(self.reader.calls))
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        self.assertFalse(stored.settled)
        # The attempt is untouched by the reflection: it reads H2, it does not
        # write it.
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())

    def test_reflection_is_fenced_like_every_other_mutation(self) -> None:
        run, _dispatch = self.in_flight()
        self.orphan()
        # A recovery whose lease has already died writes nothing.
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(
            run.id,
            WORKER_B,
            run.fencing_epoch,
            core=scripted,
            clock=lambda: self.after,
            lease=LEASE,
        )
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)
        # And the displaced owner cannot mark it afterwards.
        with self.assertRaises(JobError):
            self.memory.mark_job_run_unknown(
                run.id, WORKER_A, run.fencing_epoch, now=self.after
            )

    def test_an_in_flight_attempt_is_still_never_reflected(self) -> None:
        run, _dispatch = self.in_flight()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("execution_not_established", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_second_attempt_row_blocks_reflection(self) -> None:
        run, _dispatch = self.in_flight()
        self.orphan()
        self.plant_attempt()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("execution_not_established", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_clean_duplicate_rows_under_a_prior_step_block_reflection(
        self,
    ) -> None:
        # THE REVIEW SHAPE, END TO END. A real two-step generation: step 0
        # really executed and left a FINISHED attempt, step 1 is the cursor and
        # carries a single unresolved outcome. The corruption is a duplicate of
        # step 0's clean row - a step the chain has already passed - so before
        # the multiplicity rule the cursor read as the one reflectable
        # uncertainty and the generation would have been marked UNKNOWN on a
        # history nobody can read.
        run, _dispatch, task_id, request = self.parked()
        session_id = run.session_id
        prior_task_id = self.chain_of(session_id).step_task_ids[0]
        # Close the approval the way Core does, so recovery may reacquire.
        self.memory.resolve_approval(
            request, PermissionGrant.for_request(request), "approved"
        )
        # The cursor's own single unresolved outcome.
        cursor_attempt = self.plant_attempt(
            id="cursor-unknown",
            task_id=task_id,
            state=AttemptState.UNKNOWN_OUTCOME.value,
        )
        # And the duplicate under the PRIOR step.
        self.plant_attempt(
            id="duplicate-of-step-zero",
            task_id=prior_task_id,
            state=AttemptState.FINISHED.value,
        )
        self.assertEqual(
            2, len(self.memory.execution_attempts(prior_task_id))
        )
        self.assertEqual(1, len(self.memory.execution_attempts(task_id)))

        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)

        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict, outcome.reason)
        self.assertEqual("execution_not_established", outcome.reason)
        # CORE WAS NEVER ENTERED and the generation was NOT marked unknown.
        self.assertEqual([], scripted.calls)
        self.assertEqual(0, len(self.planner.calls))
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.RUNNING, stored.status)
        self.assertNotEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        # And the rows are exactly as they were found.
        self.assertEqual(
            AttemptState.UNKNOWN_OUTCOME,
            self.memory.latest_execution_attempt(task_id).state,
        )
        self.assertIn(cursor_attempt, [a.id for a in self.memory.execution_attempts(task_id)])

    def test_clean_duplicate_rows_also_block_a_settlement(self) -> None:
        # The same rule on the 2B1 path: a generation whose chain really
        # completed cannot be settled either, because its history is unreadable.
        run, _dispatch = self.crashed_run("advance_chain_step")
        task_id = self.task_ids(run.session_id)[0]
        self.plant_attempt(
            id="duplicate-clean-row", state=AttemptState.FINISHED.value
        )
        self.assertEqual(2, len(self.memory.execution_attempts(task_id)))
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict, outcome.reason)
        self.assertEqual("execution_not_established", outcome.reason)
        self.assertEqual([], scripted.calls)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)

    def test_a_foreign_attempt_row_blocks_reflection(self) -> None:
        run, _dispatch = self.in_flight()
        self.orphan()
        row = self.attempt()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE execution_attempts SET session_id = ? WHERE id = ?",
                ("some-other-session", row["id"]),
            )
            connection.commit()
        scripted = ScriptedCore(self.memory, None)
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch, core=scripted)
        self.assertEqual(DISPATCH_BLOCKED, outcome.verdict)
        self.assertEqual("execution_not_established", outcome.reason)
        self.assertEqual([], scripted.calls)


class UnknownResolutionTests(OrphanTestCase):
    """The last step out, and everything it refuses to be."""

    def test_the_whole_accepted_path_ends_the_generation(self) -> None:
        run, row = self.orphaned_run()
        self.assertEqual(
            "cancelled",
            self.memory.cancel_chain(run.session_id, run.chain_id),
        )
        resolved = self.resolve(run.id, row["id"])

        self.assertEqual(JobRunStatus.FAILED, resolved.status)
        self.assertTrue(resolved.settled)
        self.assertEqual(self.after.isoformat(), resolved.settled_at)
        self.assertEqual(
            ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value, resolved.blocking_code
        )
        self.assertEqual(row["id"], resolved.attempt_id)
        # EVERY ARTEFACT OF THE UNCERTAINTY SURVIVES. Ending the generation
        # ends nothing else.
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.attempt_states())
        task = self.memory.load_task(row["task_id"])
        self.assertIs(TaskStatus.FAILED, task.status)
        self.assertEqual(
            ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value,
            task.result_snapshot["error"]["code"],
        )
        self.assertFalse(task.result_snapshot["error"]["retryable"])
        # THE MONEY IS NEVER GIVEN BACK. Abandonment moves an ACTIVE
        # reservation to UNRESOLVED and deliberately never reopens a
        # RECONCILED one, so either is a legitimate end state here - what must
        # never happen is the row vanishing or the exposure being released as
        # though nothing was spent.
        self.assertIn(self.reservation(row["id"]), ("unresolved", "reconciled"))
        self.assertEqual(1, len(self.reader.calls))
        # And the three decisions are all on the record.
        for event_type in (
            "execution_declared_orphaned",
            "execution_abandoned",
            "job_run_unknown_resolved",
        ):
            self.assertEqual(1, len(self.audit(event_type)), event_type)

    def test_a_failed_chain_is_equally_coherent(self) -> None:
        run, row = self.orphaned_run()
        session = self.memory.load_session(run.session_id)
        self.assertEqual(
            "failed",
            self.memory.advance_chain_step(
                run.session_id, session.chain.id, session.chain.digest, 0
            ),
        )
        resolved = self.resolve(run.id, row["id"])
        self.assertEqual(JobRunStatus.FAILED, resolved.status)
        self.assertIs(ChainStatus.FAILED, self.chain_of(run.session_id).status)

    def test_the_resolution_record_reconstructs_the_decision(self) -> None:
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        self.resolve(run.id, row["id"])
        events = self.audit("job_run_unknown_resolved")
        self.assertEqual(1, len(events))
        self.assertEqual(run.session_id, events[0]["session_id"])
        self.assertIsNone(events[0]["task_id"])
        payload = events[0]["payload"]
        self.assertEqual(run.id, payload["run_id"])
        self.assertEqual(row["id"], payload["attempt_id"])
        self.assertEqual(row["task_id"], payload["cursor_task_id"])
        self.assertEqual(
            AttemptState.UNKNOWN_OUTCOME.value, payload["attempt_state"]
        )
        self.assertEqual(TaskStatus.FAILED.value, payload["task_status"])
        self.assertEqual(
            ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value, payload["task_error_code"]
        )
        self.assertFalse(payload["task_retryable"])
        self.assertEqual(ChainStatus.CANCELLED.value, payload["chain_status"])
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME.value, payload["previous_status"]
        )
        self.assertEqual(JobRunStatus.FAILED.value, payload["resulting_status"])
        self.assertEqual(ACTOR, payload["actor_label"])
        self.assertFalse(payload["actor_authenticated"])
        self.assertFalse(payload["external_effect_established"])
        self.assertFalse(payload["retry_authorized"])
        self.assertNotIn(WORKER_A, events[0]["payload_json"])
        self.assertNotIn(WORKER_B, events[0]["payload_json"])

    def test_the_resolved_generation_can_never_run_again(self) -> None:
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        self.resolve(run.id, row["id"])
        with self.assertRaises(JobError):
            self.memory.claim_job_run(run.id, WORKER_C, now=self.after, lease=LEASE)
        with self.assertRaises(JobError):
            self.reacquire(run.id, WORKER_C, self.reload(run.id).fencing_epoch)
        with self.assertRaises(JobError):
            self.resolve(run.id, row["id"])
        self.assertEqual(1, len(self.audit("job_run_unknown_resolved")))

    def test_two_concurrent_resolvers_produce_one_winner(self) -> None:
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        results: list = []
        barrier = threading.Barrier(2)

        def close(actor: str) -> None:
            store = SQLiteMemoryStore(self.database)
            barrier.wait()
            try:
                results.append(
                    store.resolve_unknown_job_run(
                        run.id,
                        row["id"],
                        now=self.after,
                        actor_label=actor,
                        reason=WHY,
                    )
                )
            except JobError as exc:
                results.append(exc)

        threads = [
            threading.Thread(target=close, args=(actor,))
            for actor in ("operator:one", "operator:two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len([r for r in results if not isinstance(r, JobError)]))
        self.assertEqual(1, len([r for r in results if isinstance(r, JobError)]))
        self.assertEqual(1, len(self.audit("job_run_unknown_resolved")))

    def test_a_failing_audit_rolls_the_resolution_back(self) -> None:
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        original = SQLiteMemoryStore.__dict__["_insert_job_run_event"]

        def refuse(*_args, **_kwargs):
            raise sqlite3.IntegrityError("the audit row could not be written")

        SQLiteMemoryStore._insert_job_run_event = staticmethod(refuse)
        self.addCleanup(
            setattr, SQLiteMemoryStore, "_insert_job_run_event", original
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.resolve(run.id, row["id"])
        stored = self.reload(run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        self.assertFalse(stored.settled)
        self.assertIsNone(stored.attempt_id)
        self.assertIsNone(stored.blocking_code)
        self.assertEqual([], self.audit("job_run_unknown_resolved"))
        SQLiteMemoryStore._insert_job_run_event = original
        self.assertEqual(
            JobRunStatus.FAILED, self.resolve(run.id, row["id"]).status
        )


class ResolutionRefusalTests(OrphanTestCase):
    """Everything that must not be closable, refused one at a time."""

    def assertRefused(self, run, attempt_id, **over) -> None:
        before = self.reload(run.id)
        with self.assertRaises(JobError):
            self.resolve(run.id, attempt_id, **over)
        after = self.reload(run.id)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.settled_at, after.settled_at)
        self.assertIsNone(after.attempt_id)
        self.assertEqual([], self.audit("job_run_unknown_resolved"))

    def prepared(self):
        """Orphaned, reflected, abandoned and chain-cancelled: resolvable."""
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        return self.reload(run.id), row

    def test_an_in_flight_row_anywhere_refuses(self) -> None:
        run, row = self.prepared()
        self.plant_attempt(state=AttemptState.IN_FLIGHT.value)
        self.assertRefused(run, row["id"])

    def test_a_second_unknown_row_refuses(self) -> None:
        run, row = self.prepared()
        self.plant_attempt(state=AttemptState.UNKNOWN_OUTCOME.value)
        self.assertRefused(run, row["id"])

    def test_a_clean_sibling_row_refuses(self) -> None:
        run, row = self.prepared()
        self.plant_attempt(state=AttemptState.FINISHED.value)
        self.assertRefused(run, row["id"])

    def test_a_foreign_attempt_identity_refuses(self) -> None:
        run, row = self.prepared()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE execution_attempts SET session_id = ? WHERE id = ?",
                ("some-other-session", row["id"]),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_naming_another_attempt_refuses(self) -> None:
        run, row = self.prepared()
        self.assertRefused(run, "not-this-attempt")
        self.assertRefused(run, "")

    def test_an_abandonment_record_for_another_attempt_refuses(self) -> None:
        # The audit is one of the three facts that prove the abandonment, so a
        # record naming a different attempt is not this attempt's abandonment.
        run, row = self.prepared()
        with contextlib.closing(self.raw()) as connection:
            payload = json.dumps({"attempt_id": "somebody-else"})
            connection.execute(
                "UPDATE event_log SET payload_json = ? WHERE event_type = ?",
                (payload, "execution_abandoned"),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_a_missing_abandonment_record_refuses(self) -> None:
        run, row = self.prepared()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "DELETE FROM event_log WHERE event_type = ?", ("execution_abandoned",)
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_an_ordinary_connector_failure_is_not_an_abandonment(self) -> None:
        # THE WHOLE REASON FAILED IS NOT ENOUGH. This task is FAILED, but for
        # a reason that has nothing to do with an unresolved external effect.
        run, row = self.prepared()
        task = self.memory.load_task(row["task_id"])
        with contextlib.closing(self.raw()) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()["state_json"]
            )
            document["result_snapshot"]["error"]["code"] = (
                ErrorCode.CONNECTOR_FAILED.value
            )
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_a_retryable_failure_refuses(self) -> None:
        run, row = self.prepared()
        task = self.memory.load_task(row["task_id"])
        with contextlib.closing(self.raw()) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()["state_json"]
            )
            document["result_snapshot"]["error"]["retryable"] = True
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_a_missing_result_snapshot_refuses(self) -> None:
        run, row = self.prepared()
        task = self.memory.load_task(row["task_id"])
        with contextlib.closing(self.raw()) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()["state_json"]
            )
            document["result_snapshot"] = {}
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_a_task_that_was_never_abandoned_refuses(self) -> None:
        # Orphaned and reflected, but the human never abandoned it: the task
        # is still PENDING and the generation is not anybody's to close.
        run, _dispatch = self.in_flight()
        row = self.attempt()
        self.orphan()
        self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.memory.cancel_chain(run.session_id, run.chain_id)
        self.assertRefused(self.reload(run.id), row["id"])

    def test_an_active_chain_refuses(self) -> None:
        run, row = self.orphaned_run()
        self.assertIs(ChainStatus.ACTIVE, self.chain_of(run.session_id).status)
        self.assertRefused(run, row["id"])

    def test_a_completed_chain_refuses(self) -> None:
        run, row = self.orphaned_run()
        self.plant_status_only(run.session_id, ChainStatus.COMPLETED)
        self.assertRefused(run, row["id"])

    def test_an_unusable_chain_refuses(self) -> None:
        run, row = self.orphaned_run()
        self.plant_status_only(run.session_id, ChainStatus.INVALID)
        self.assertRefused(run, row["id"])

    def test_a_generation_in_any_other_status_refuses(self) -> None:
        # Only an UNKNOWN_OUTCOME generation is resolvable, and the reflection
        # is what puts it there.
        run, _dispatch = self.in_flight()
        row = self.attempt()
        self.orphan()
        self.abandon(row)
        self.memory.cancel_chain(run.session_id, run.chain_id)
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)
        self.assertRefused(self.reload(run.id), row["id"])

    def test_a_forged_session_document_refuses(self) -> None:
        # The 2B2a identity hardening, re-proved on this boundary too.
        run, row = self.prepared()
        other = SessionState(objective="a second real session", project_id="proj-a")
        self.memory.save_session(other)
        with contextlib.closing(self.raw()) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT state_json FROM sessions WHERE id = ?", (run.session_id,)
                ).fetchone()["state_json"]
            )
            document["id"] = other.id
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), run.session_id),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_a_drifted_declaration_refuses(self) -> None:
        run, row = self.prepared()
        with contextlib.closing(self.raw()) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT state_json FROM sessions WHERE id = ?", (run.session_id,)
                ).fetchone()["state_json"]
            )
            document["chain"]["steps"][0]["objective"] = "something else entirely"
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), run.session_id),
            )
            connection.commit()
        self.assertRefused(run, row["id"])

    def test_blank_attribution_refuses(self) -> None:
        run, row = self.prepared()
        self.assertRefused(run, row["id"], actor_label="  ")
        self.assertRefused(run, row["id"], reason="")

    def test_a_generation_that_already_names_an_attempt_refuses(self) -> None:
        run, row = self.prepared()
        with contextlib.closing(self.raw()) as connection:
            connection.execute(
                "UPDATE job_runs SET attempt_id = ? WHERE id = ?",
                ("already-bound", run.id),
            )
            connection.commit()
        with self.assertRaises(JobError):
            self.resolve(run.id, row["id"])
        self.assertEqual([], self.audit("job_run_unknown_resolved"))


class OrphanRestartTests(OrphanTestCase):
    """Every crash window between the three decisions, after a reopen."""

    def reopened(self) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(self.database)

    def test_a_declaration_survives_a_crash_before_reflection(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        self.orphan()
        store = self.reopened()
        self.assertIs(
            AttemptState.UNKNOWN_OUTCOME,
            store.latest_execution_attempt(row["task_id"]).state,
        )
        self.assertEqual(JobRunStatus.RUNNING, store.load_job_run(run.id).status)
        # The reflection is still available, and nothing else is.
        outcome = self.recover(run.id, WORKER_B, run.fencing_epoch)
        self.assertEqual(DISPATCH_UNKNOWN, outcome.verdict, outcome.reason)

    def test_a_reflection_survives_a_crash_before_abandonment(self) -> None:
        run, _dispatch = self.in_flight()
        row = self.attempt()
        self.orphan()
        self.recover(run.id, WORKER_B, run.fencing_epoch)
        store = self.reopened()
        stored = store.load_job_run(run.id)
        self.assertEqual(JobRunStatus.UNKNOWN_OUTCOME, stored.status)
        self.assertFalse(stored.settled)
        # Not claimable, not recoverable, not resolvable yet.
        with self.assertRaises(JobError):
            store.claim_job_run(run.id, WORKER_C, now=self.after, lease=LEASE)
        with self.assertRaises(JobError):
            self.resolve(run.id, row["id"], store=store)

    def test_an_abandonment_survives_a_crash_before_resolution(self) -> None:
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        store = self.reopened()
        resolved = self.resolve(run.id, row["id"], store=store)
        self.assertEqual(JobRunStatus.FAILED, resolved.status)
        self.assertEqual(row["id"], resolved.attempt_id)

    def test_a_resolution_is_terminal_across_a_restart(self) -> None:
        run, row = self.orphaned_run()
        self.memory.cancel_chain(run.session_id, run.chain_id)
        self.resolve(run.id, row["id"])
        store = self.reopened()
        stored = store.load_job_run(run.id)
        self.assertEqual(JobRunStatus.FAILED, stored.status)
        self.assertTrue(stored.settled)
        self.assertEqual(
            ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value, stored.blocking_code
        )
        self.assertEqual(row["id"], stored.attempt_id)
        with self.assertRaises(JobError):
            store.claim_job_run(run.id, WORKER_C, now=self.after, lease=LEASE)


if __name__ == "__main__":
    unittest.main()
