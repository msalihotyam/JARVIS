"""Scheduler phase 2B2b1: store parity and the shape of the operator surface.

Two things are proved here that the behavioural suite cannot prove on its own.

PARITY. The non-SQLite double must refuse exactly what SQLite refuses, in the
same order and for the same reasons, or a contract test would certify a hole
instead of closing it. The part of the decision that reads execution history is
parity BY CONSTRUCTION - ``execution_history_verdict`` and
``chain_lifecycle_coherent`` are pure and both stores call the same functions -
so what is exercised here is each store's own proof ordering around them. The
double owns no chain materializer, so the routing-dependent half of the
resolution proof is covered by the SQLite suite and by the direct predicate
tests; every refusal the double CAN represent is exercised.

SURFACE. Recovery must be structurally incapable of executing anything, and
that is a property of what the code CONSTRUCTS, not of what it intends. These
guards read the real source: the operator paths may build a store and nothing
else, the scheduler may not name any H2 or human-gate authority, and none of
the phase-2B2b2 cancellation surface may exist in this build at all.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import threading
import unittest
from datetime import datetime, timedelta, timezone

from jarvis.contracts import (
    AttemptRecoveryOutcome,
    AttemptState,
    ChainStep,
    ErrorCode,
    ExecutionAttempt,
    JobError,
    JobKind,
    JobRunStatus,
    JobSpec,
    ORPHAN_MIN_AGE,
    ProjectRecord,
    SessionState,
    TaskStatus,
    result_to_dict,
    terminal_failure_result,
    StructuredError,
)
from jarvis.schedules import Interval

from tests.helpers import make_task
from tests.test_scheduler_dispatch import LEASE, WORKER_A, WORKER_B
from tests.test_scheduler_recovery import ACTOR, WHY
from tests.test_storage_abstraction import InMemoryStore

_UNSET = object()


class OrphanParityTests(unittest.TestCase):
    """The double declares, refuses and records on exactly the same terms."""

    NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.memory = InMemoryStore()
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))
        spec = JobSpec(
            project_id="proj-a",
            kind=JobKind.RECURRING,
            label="hourly digest",
            schedule=Interval(anchor=self.NOW, every=timedelta(hours=1)),
            steps=(
                ChainStep(
                    objective="summarize the log",
                    capability="echo",
                    action="read",
                    target="notes.txt",
                ),
            ),
        )
        record = self.memory.create_job(spec, now=self.NOW)
        self.at = self.NOW + timedelta(hours=1)
        run = self.memory.materialize_due_job_run(
            record.id, record.revision, now=self.at
        )
        lease = self.memory.claim_job_run(run.id, WORKER_A, now=self.at, lease=LEASE)
        self.dispatch = self.memory.bind_job_run_dispatch(
            run.id, WORKER_A, lease.fencing_epoch, now=self.at
        )
        self.run = self.memory.load_job_run(run.id)
        self.after = self.at + timedelta(minutes=10)
        # A task in the bound session, and one attempt of it that crossed the
        # connector boundary and was never concluded.
        self.session_id = self.dispatch.session_id
        self.task = make_task(target="notes.txt")
        self.memory.save_task(self.session_id, self.task)
        self.started = self.at
        self.attempt = ExecutionAttempt(
            session_id=self.session_id,
            task_id=self.task.id,
            connector="reader",
            fingerprint="fingerprint",
            state=AttemptState.IN_FLIGHT,
            created_at=self.started.isoformat(),
            updated_at=self.started.isoformat(),
        )
        self.memory.attempts[self.task.id] = [self.attempt]
        self.aged = self.started + ORPHAN_MIN_AGE + timedelta(minutes=1)

    def orphan(self, *, now=_UNSET, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        session_id = fields.pop("session_id", self.session_id)
        task_id = fields.pop("task_id", self.task.id)
        attempt_id = fields.pop("attempt_id", self.attempt.id)
        return self.memory.declare_attempt_orphaned(
            session_id,
            task_id,
            attempt_id,
            now=self.aged if now is _UNSET else now,
            **fields,
        )

    def states(self) -> list[str]:
        return [a.state.value for a in self.memory.attempts[self.task.id]]

    def audit(self, event_type: str) -> list[dict]:
        return [e for e in self.memory.events if e["event_type"] == event_type]

    # -- the declaration --------------------------------------------------

    def test_the_double_declares_on_the_same_terms(self) -> None:
        self.assertIs(AttemptRecoveryOutcome.RECOVERED, self.orphan())
        self.assertEqual([AttemptState.UNKNOWN_OUTCOME.value], self.states())
        # Nothing else moved.
        self.assertIs(TaskStatus.PENDING, self.memory.load_task(self.task.id).status)
        events = self.audit("execution_declared_orphaned")
        self.assertEqual(1, len(events))
        self.assertEqual(self.session_id, events[0]["session_id"])
        self.assertEqual(self.task.id, events[0]["task_id"])
        payload = events[0]["payload"]
        self.assertEqual(self.attempt.id, payload["attempt_id"])
        self.assertEqual(AttemptState.IN_FLIGHT.value, payload["previous_state"])
        self.assertFalse(payload["actor_authenticated"])
        self.assertFalse(payload["worker_death_proved"])
        self.assertFalse(payload["external_effect_established"])
        self.assertFalse(payload["retry_authorized"])

    def test_the_double_refuses_every_non_in_flight_state(self) -> None:
        for state in (
            AttemptState.PREPARED,
            AttemptState.FINISHED,
            AttemptState.UNKNOWN_OUTCOME,
        ):
            with self.subTest(state=state):
                self.setUp()
                self.memory.attempts[self.task.id] = [
                    ExecutionAttempt(
                        session_id=self.session_id,
                        task_id=self.task.id,
                        connector="reader",
                        fingerprint="fingerprint",
                        state=state,
                        updated_at=self.started.isoformat(),
                        id=self.attempt.id,
                    )
                ]
                self.assertIs(AttemptRecoveryOutcome.NOT_RECOVERABLE, self.orphan())
                self.assertEqual([state.value], self.states())
                self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_the_double_refuses_a_terminal_task(self) -> None:
        terminal = make_task(target="notes.txt")
        object.__setattr__(terminal, "id", self.task.id)
        terminal.status = TaskStatus.CANCELLED
        self.memory.save_task(self.session_id, terminal)
        self.assertIs(AttemptRecoveryOutcome.TERMINAL, self.orphan())
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.states())

    def test_the_double_gives_one_verdict_for_every_identity_mismatch(self) -> None:
        other = SessionState(objective="somebody else's work")
        self.memory.save_session(other)
        for label, over in (
            ("foreign session", {"session_id": other.id}),
            ("foreign task", {"task_id": "no-such-task"}),
            ("wrong attempt", {"attempt_id": "no-such-attempt"}),
        ):
            with self.subTest(case=label):
                self.assertIs(AttemptRecoveryOutcome.UNKNOWN, self.orphan(**over))
                self.assertEqual([AttemptState.IN_FLIGHT.value], self.states())
                self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_the_double_applies_the_same_age_guard(self) -> None:
        for label, moment in (
            ("immediately", self.started),
            ("just before", self.started + ORPHAN_MIN_AGE - timedelta(seconds=1)),
        ):
            with self.subTest(case=label):
                self.assertIs(AttemptRecoveryOutcome.TOO_RECENT, self.orphan(now=moment))
                self.assertEqual([AttemptState.IN_FLIGHT.value], self.states())
        self.assertIs(
            AttemptRecoveryOutcome.RECOVERED,
            self.orphan(now=self.started + ORPHAN_MIN_AGE),
        )

    def test_the_double_refuses_blank_attribution_and_bad_instants(self) -> None:
        for over in ({"actor_label": " "}, {"reason": ""}):
            with self.subTest(over=over):
                with self.assertRaises(JobError):
                    self.orphan(**over)
        for moment in (None, "2026-09-06T12:00:00+00:00", datetime(2026, 9, 6)):
            with self.subTest(now=moment):
                with self.assertRaises(JobError):
                    self.orphan(now=moment)
        self.assertEqual([AttemptState.IN_FLIGHT.value], self.states())
        self.assertEqual([], self.audit("execution_declared_orphaned"))

    def test_the_double_admits_one_concurrent_declaration(self) -> None:
        results: list = []
        barrier = threading.Barrier(2)

        def declare(actor: str) -> None:
            barrier.wait()
            try:
                results.append(self.orphan(actor_label=actor))
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
        self.assertEqual(1, len(self.audit("execution_declared_orphaned")))

    # -- the resolution ---------------------------------------------------

    def resolve(self, *, attempt_id=None, now=_UNSET, **over):
        fields = dict(actor_label=ACTOR, reason=WHY)
        fields.update(over)
        return self.memory.resolve_unknown_job_run(
            self.run.id,
            attempt_id or self.attempt.id,
            now=self.after if now is _UNSET else now,
            **fields,
        )

    def park_unknown(self) -> None:
        """Put the generation where a resolution would start from."""
        self.orphan()
        import dataclasses

        self.memory.job_runs[self.run.id] = dataclasses.replace(
            self.memory.job_runs[self.run.id],
            status=JobRunStatus.UNKNOWN_OUTCOME,
            owner_token=None,
            lease_expires_at_utc=None,
        )

    def test_the_double_refuses_a_generation_in_any_other_status(self) -> None:
        # RUNNING, straight from the binding: only UNKNOWN_OUTCOME resolves.
        with self.assertRaises(JobError):
            self.resolve()
        self.assertEqual(
            JobRunStatus.RUNNING, self.memory.load_job_run(self.run.id).status
        )
        self.assertEqual([], self.audit("job_run_unknown_resolved"))

    def test_the_double_refuses_blank_attribution_and_bad_identifiers(self) -> None:
        self.park_unknown()
        for over in ({"actor_label": ""}, {"reason": "   "}):
            with self.subTest(over=over):
                with self.assertRaises(JobError):
                    self.resolve(**over)
        for bad in ("", "   "):
            with self.subTest(attempt=bad):
                with self.assertRaises(JobError):
                    self.resolve(attempt_id=bad)
        with self.assertRaises(JobError):
            self.resolve(now=None)
        self.assertEqual([], self.audit("job_run_unknown_resolved"))

    def test_the_double_refuses_a_generation_that_already_names_an_attempt(
        self,
    ) -> None:
        import dataclasses

        self.park_unknown()
        self.memory.job_runs[self.run.id] = dataclasses.replace(
            self.memory.job_runs[self.run.id], attempt_id="already-bound"
        )
        with self.assertRaises(JobError):
            self.resolve()
        self.assertEqual([], self.audit("job_run_unknown_resolved"))

    def test_the_double_refuses_a_chain_that_routes_to_nothing_proved(self) -> None:
        # The bound chain names no task for its step, so no routed history can
        # be rebuilt and nothing may be closed on it.
        self.park_unknown()
        with self.assertRaises(JobError):
            self.resolve()
        self.assertEqual(
            JobRunStatus.UNKNOWN_OUTCOME,
            self.memory.load_job_run(self.run.id).status,
        )
        self.assertEqual([], self.audit("job_run_unknown_resolved"))

    def test_the_double_refuses_a_forged_session_document(self) -> None:
        import dataclasses

        self.park_unknown()
        stored = self.memory.sessions[self.session_id]
        self.memory.sessions[self.session_id] = dataclasses.replace(
            stored, id="some-other-session"
        )
        with self.assertRaises(JobError):
            self.resolve()
        self.assertEqual([], self.audit("job_run_unknown_resolved"))


class OrphanSurfaceTests(unittest.TestCase):
    """What the new code may construct, name and expose - proved from source."""

    @staticmethod
    def identifiers(source: str) -> set[str]:
        """Every NAME the code actually uses, docstrings and comments excluded.

        A substring scan cannot do this job: the very docstrings that promise
        "no registry, no connector, no AgentCore" contain those words, so a
        text guard would fail on prose and could be silenced by editing prose.
        Only real identifiers count.
        """
        tree = ast.parse(source)
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    found.add(alias.asname or alias.name.split(".")[0])
        return found

    def names_of(self, obj) -> set[str]:
        return self.identifiers(textwrap.dedent(inspect.getsource(obj)))

    def test_the_operator_paths_construct_no_execution_machinery(self) -> None:
        # STRUCTURAL, not intentional: a recovery verb that cannot build a
        # connector cannot reach one however it is called.
        import jarvis.app as app
        import jarvis.cli as cli

        for obj in (
            app.task_orphan_result,
            app.job_run_resolve_result,
            cli._dispatch_task_command,
            cli._dispatch_job_command,
        ):
            names = self.names_of(obj)
            for forbidden in (
                "AgentCore",
                "ToolRegistry",
                "build_default_app",
                "JarvisApp",
                "run_chain",
                "resume",
                "connector",
                "registry",
                "execute",
            ):
                with self.subTest(obj=obj.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, names)
        # And what they DO build is a store, and only a store.
        for obj in (cli._dispatch_task_command, cli._dispatch_job_command):
            self.assertIn("SQLiteMemoryStore", self.names_of(obj))

    def test_the_operator_paths_expose_no_retry_or_force_verb(self) -> None:
        import jarvis.app as app
        import jarvis.cli as cli

        for module in (app, cli):
            names = self.identifiers(inspect.getsource(module))
            for forbidden in (
                "force_resolve",
                "force_complete",
                "mark_success",
                "effect_happened",
                "effect_absent",
                "retry_job_run",
                "retry_attempt",
            ):
                with self.subTest(module=module.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, names)

    def test_the_scheduler_holds_no_h2_or_human_gate_authority(self) -> None:
        import jarvis.dispatch as dispatch

        names = self.identifiers(inspect.getsource(dispatch))
        for forbidden in (
            "declare_attempt_orphaned",
            "abandon_unknown_attempt",
            "resolve_unknown_job_run",
            "mark_execution_unknown",
            "open_execution_attempt",
            "begin_execution",
            "conclude_execution",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, names)

    def test_the_h2_gate_is_never_reachable_from_the_scheduler_module(self) -> None:
        # The reflection reads H2 and writes only the fenced JobRun transition.
        import jarvis.dispatch as dispatch

        tree = ast.parse(inspect.getsource(dispatch))
        recover = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "recover_job_run"
        )
        attrs = {
            node.func.attr
            for node in ast.walk(recover)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("mark_job_run_unknown", attrs)
        self.assertNotIn("declare_attempt_orphaned", attrs)
        self.assertNotIn("abandon_unknown_attempt", attrs)
        self.assertNotIn("resolve_unknown_job_run", attrs)

    def test_phase_2b2b2_cancellation_added_exactly_one_intent_writer(self) -> None:
        # Phase 2B2b2 arrived, so "absent" became "exactly this". What the
        # guard protects is unchanged: cancellation may record ONE sticky
        # intent and may never grow into a second settlement authority, a
        # retry, or a way to reopen a generation.
        from jarvis.memory import SQLiteMemoryStore
        from tests.test_storage_abstraction import InMemoryStore as Double

        for store in (SQLiteMemoryStore, Double):
            # The one operation that exists.
            self.assertTrue(hasattr(store, "request_job_run_cancel"), store.__name__)
            for absent in (
                "cancel_job_run",          # orchestration is NOT a store authority
                "resolve_job_run",
                "retry_job_run",
                "uncancel_job_run",
                "clear_cancel_requested",
            ):
                with self.subTest(store=store.__name__, absent=absent):
                    self.assertFalse(hasattr(store, absent), absent)

        # THE FLAG IS STICKY: exactly one writer sets it, and nothing clears
        # it. A statement that would return it to 0 is what this forbids.
        import re

        import jarvis.memory as memory

        # Flattened so implicit string concatenation cannot hide a statement,
        # and each occurrence judged by the clause it sits in: a ``0`` may only
        # ever appear as a compare-and-set PREDICATE, never as an assignment.
        # Docstrings and comments are stripped first: this module's own prose
        # quotes the predicate it is describing, and a text scan would fail on
        # documentation rather than on code.
        tree = ast.parse(inspect.getsource(memory))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    body.pop(0)
        flat = " ".join(re.sub(r"[\"']", " ", ast.unparse(tree)).split())
        occurrences = list(re.finditer(r"cancel_requested\s*=\s*([01])", flat))
        self.assertTrue(occurrences, "the intent flag is never referenced at all")
        assignments = []
        for match in occurrences:
            prefix = flat[max(0, match.start() - 40) : match.start()]
            predicate = "AND" in prefix or "WHERE" in prefix
            if not predicate:
                assignments.append(match.group(0))
            if match.group(1) == "0":
                # Every zero is a guard against a flagged generation, never a
                # write that would return one to un-cancelled.
                self.assertTrue(predicate, flat[match.start() - 60 : match.end()])
        # Exactly one thing is ever assigned, and it is the flag going up.
        self.assertTrue(assignments, "nothing ever sets the flag")
        for assignment in assignments:
            self.assertRegex(assignment, r"cancel_requested\s*=\s*1")

    def test_every_gate_that_must_refuse_a_flagged_generation_names_it(
        self,
    ) -> None:
        # DEFENCE IN DEPTH, PINNED. Each of these writes reads the run and then
        # updates it inside ONE ``BEGIN IMMEDIATE`` transaction, so today the
        # Python check alone already decides the answer and the ``WHERE``
        # clause can never be the one that refuses. That is exactly why it
        # needs a guard rather than a test: no behavioural test can reach it,
        # and dropping it would be invisible until the day the transaction mode
        # or the read/write ordering changes and the window reopens.
        #
        # The predicate is stated where the row is actually written, in every
        # place that could hand a flagged generation a future.
        import inspect

        from jarvis.memory import SQLiteMemoryStore

        for name in (
            "claim_job_run",            # no new owner
            "bind_job_run_dispatch",    # no session, no chain, no RUNNING
            "request_job_run_cancel",   # the flag itself, set exactly once
        ):
            with self.subTest(gate=name):
                self.assertIn(
                    "cancel_requested = 0",
                    inspect.getsource(getattr(SQLiteMemoryStore, name)),
                    name,
                )
        # And the execution claim - the last instant before the connector
        # boundary - asks the question in the transaction that crosses it.
        claim = inspect.getsource(SQLiteMemoryStore.begin_execution)
        self.assertIn("_scheduled_claim_refused", claim)
        self.assertLess(
            claim.index("_scheduled_claim_refused"),
            claim.index("UPDATE execution_attempts"),
            "the arbitration must precede the PREPARED -> IN_FLIGHT crossing",
        )

    def test_cancellation_reuses_the_existing_approval_vocabulary(self) -> None:
        # The cancelled task's own question leaves 'pending' as SUPERSEDED -
        # the word the store already writes when a newer request displaces an
        # older one. A NEW durable status would be a schema change in all but
        # name: every reader that enumerates approval outcomes would have to
        # learn it, and none of them were taught.
        import inspect
        import re

        from jarvis.memory import SQLiteMemoryStore

        source = inspect.getsource(SQLiteMemoryStore._cancel_task_atomically)
        self.assertIn("status = 'superseded'", source)
        # Statuses this store may ever write, across every writer it has.
        written = set(
            re.findall(
                r"pending_approvals SET status = '([a-z_]+)'",
                inspect.getsource(SQLiteMemoryStore),
            )
        )
        self.assertEqual({"superseded", "rejected"}, written, written)

    def test_the_schema_did_not_move(self) -> None:
        from jarvis.app import OUTPUT_SCHEMA_VERSION
        from jarvis.memory import SCHEMA_VERSION

        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)

    def test_the_typed_abandonment_result_is_what_resolution_requires(self) -> None:
        # The exact contract the resolution proof reads, asserted against the
        # canonical writer rather than against a hand-written dict.
        snapshot = result_to_dict(
            terminal_failure_result(
                StructuredError(
                    ErrorCode.EXECUTION_UNKNOWN_OUTCOME,
                    "in doubt",
                    retryable=False,
                )
            )
        )
        self.assertEqual("error", snapshot["outcome"])
        self.assertEqual(
            ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value, snapshot["error"]["code"]
        )
        self.assertIs(False, snapshot["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
