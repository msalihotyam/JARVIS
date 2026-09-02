"""M12 S1 - read-only operator visibility.

What this file pins:

    persisted rows -> bounded OPERATIONAL projections -> three local commands

plus everything that must NOT happen: no connector, registry or AgentCore is
constructed, no row is written, no attempt is opened, no approval is consumed,
no reservation is created, and no objective, context, model answer, file
excerpt or raw persisted document reaches the output.

Every command here is a pure read. That is asserted structurally - by
comparing the ENTIRE database content before and after, and by making the
application factory explode if anything tries to build one - rather than by
inspection.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis.app import (
    EXIT_INVALID_INPUT,
    EXIT_NOT_FOUND,
    EXIT_OK,
    _chain_payload,
    _visibility_chain_payload,
    sessions_result,
    show_result,
    tasks_result,
)
from jarvis.cli import main as cli_main
from jarvis.contracts import (
    APPLIED_EFFECT_LOCAL_WRITE,
    LOCAL_READ_CAPABILITY,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_MAX_BYTES,
    PROGRESS_TEXT_MAX_CHARS,
    VISIBILITY_DECODE_ERRORS,
    VISIBILITY_DEFAULT_LIMIT,
    VISIBILITY_MAX_LIMIT,
    ActionScope,
    AttemptState,
    ChainBinding,
    ChainState,
    ChainStatus,
    DataClassification,
    ExecutionAttempt,
    LocalReadSnapshot,
    LocalWriteRecord,
    LoopStage,
    ModelAnswer,
    PermissionRequest,
    RiskLevel,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    TaskStatus,
    build_model_answer,
    chain_steps_from_declaration,
    local_write_record_refusal,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

#: Strings planted into every user-controlled persisted field. If any of them
#: reaches a visibility payload, the payload is leaking.
SECRETS = {
    "objective": "OBJECTIVE-LEAK-CANARY do not print me",
    "next_step": "NEXT-STEP-LEAK-CANARY",
    "context": "CONTEXT-LEAK-CANARY",
    "model_answer": "MODEL-ANSWER-LEAK-CANARY",
    "excerpt": "FILE-EXCERPT-LEAK-CANARY",
    "target": "TARGET-LEAK-CANARY.txt",
    "write_content": "WRITE-CONTENT-LEAK-CANARY",
    "visible_assumption": "ASSUMPTION-LEAK-CANARY",
}

CHAIN_DECLARATION = [
    {
        "objective": "Read the project notes.",
        "capability": "file_read",
        "action": "read",
        "target": "notes.txt",
        "consumes": -1,
    },
    {
        "objective": "Summarize the notes.",
        "capability": "code_analysis",
        "action": "analyze",
        "destination": "anthropic/claude-code",
        "consumes": 0,
    },
]


class VisibilityTestCase(unittest.TestCase):
    """A real SQLite store. No core, registry, connector or model, ever."""

    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.root)
        self.database = Path(self.root.name) / "jarvis.db"
        self.settings = Settings(memory_database=self.database)
        self.memory = SQLiteMemoryStore(self.database)

    # -- fixtures ------------------------------------------------------

    def make_session(self, objective: str | None = None) -> SessionState:
        session = SessionState(objective=objective or SECRETS["objective"])
        session.next_step = SECRETS["next_step"]
        self.memory.save_session(session)
        return session

    def make_task(
        self, session_id: str, *, capability: str = "file_read", **fields: object
    ) -> Task:
        task = Task(
            objective=SECRETS["objective"],
            capability=capability,
            scope=ActionScope(
                action="read",
                target=SECRETS["target"],
                data_classification=DataClassification.INTERNAL,
            ),
        )
        task.context = {"note": SECRETS["context"]}
        task.visible_assumption = SECRETS["visible_assumption"]
        task.write_content = SECRETS["write_content"]
        task.model_answer = build_model_answer(SECRETS["model_answer"])
        raw = SECRETS["excerpt"].encode("utf-8")
        task.local_read = LocalReadSnapshot(
            relative_path="notes.txt",
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            excerpt=SECRETS["excerpt"],
            truncated=False,
            classification=DataClassification.INTERNAL,
            read_at="2026-08-28T00:00:00+00:00",
        )
        for key, value in fields.items():
            setattr(task, key, value)
        self.memory.save_task(session_id, task)
        return task

    def plant_attempt(self, session_id: str, task_id: str, state: AttemptState) -> str:
        """An attempt row in an exact state, the way a crash leaves one."""
        attempt = ExecutionAttempt(
            session_id=session_id,
            task_id=task_id,
            connector="reader",
            fingerprint="f" * 64,
        )
        self.memory.open_execution_attempt(attempt)
        if state is not AttemptState.PREPARED:
            with contextlib.closing(sqlite3.connect(self.database)) as connection:
                connection.execute(
                    "UPDATE execution_attempts SET state = ? WHERE task_id = ?",
                    (state.value, task_id),
                )
                connection.commit()
        latest = self.memory.latest_execution_attempt(task_id)
        assert latest is not None
        self.assertIs(latest.state, state)
        return latest.id

    def plant_pending_approval(self, session: SessionState, task: Task) -> PermissionRequest:
        request = PermissionRequest(
            session_id=session.id,
            task_id=task.id,
            scope=task.scope,
            risk_level=RiskLevel.HIGH,
            side_effect=SideEffectProfile(
                effect=SideEffect.EXTERNAL_EGRESS,
                reversible=False,
                always_requires_approval=True,
            ),
            reason=SECRETS["objective"],
            expires_at="2099-01-01T00:00:00+00:00",
        )
        self.memory.save_pending_approval(request)
        return request

    def declare_chain(self, session: SessionState) -> ChainState:
        chain = ChainState.declared(chain_steps_from_declaration(CHAIN_DECLARATION))
        assert chain is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(session.id, chain), "declared"
        )
        stored = self.memory.load_session(session.id)
        assert stored is not None and stored.chain is not None
        return stored.chain

    def corrupt(self, table: str, row_id: str) -> None:
        """Make one persisted document unreadable, the way corruption does."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                f"UPDATE {table} SET state_json = ? WHERE id = ?",
                ("{not json at all", row_id),
            )
            connection.commit()

    # -- probes --------------------------------------------------------

    def database_content(self) -> dict[str, list[tuple]]:
        """EVERY row of EVERY table. The strongest available no-write proof."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            tables = sorted(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            )
            return {
                table: connection.execute(
                    f"SELECT * FROM {table}"  # fixed table names only
                ).fetchall()
                for table in tables
            }

    def rendered(self, result: object) -> str:
        return result.render()  # type: ignore[attr-defined]

    def assertNoCanaries(self, text: str, label: str = "") -> None:
        for name, canary in SECRETS.items():
            with self.subTest(canary=name):
                self.assertNotIn(canary, text, f"{label}: {name} leaked")


class SessionListingTests(VisibilityTestCase):
    """`jarvis sessions`: rediscovering work after a restart."""

    def test_an_empty_database_lists_nothing(self) -> None:
        result = sessions_result(self.memory, self.settings)
        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(result.payload["sessions"], [])
        self.assertEqual(result.payload["count"], 0)
        self.assertEqual(result.payload["limit"], VISIBILITY_DEFAULT_LIMIT)

    def test_sessions_are_newest_first_with_a_stable_tie_break(self) -> None:
        """Ties are broken by id, so repeated calls agree exactly."""
        stamp = "2026-08-28T00:00:00+00:00"
        identities = ["c-session", "a-session", "b-session"]
        for identity in identities:
            session = SessionState(objective="x", id=identity)
            self.memory.save_session(session)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE sessions SET updated_at = ?", (stamp,))
            connection.commit()
        first = sessions_result(self.memory, self.settings).payload["sessions"]
        second = sessions_result(self.memory, self.settings).payload["sessions"]
        self.assertEqual(
            [row["session_id"] for row in first], sorted(identities)
        )
        self.assertEqual(first, second, "the order is not stable")

    def test_a_newer_session_sorts_first(self) -> None:
        older = self.make_session()
        newer = self.make_session()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", older.id),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                ("2099-01-01T00:00:00+00:00", newer.id),
            )
            connection.commit()
        rows = sessions_result(self.memory, self.settings).payload["sessions"]
        self.assertEqual([row["session_id"] for row in rows], [newer.id, older.id])

    def test_the_task_count_is_the_authoritative_one(self) -> None:
        session = self.make_session()
        for _ in range(3):
            self.make_task(session.id)
        other = self.make_session()
        self.make_task(other.id)
        rows = {
            row["session_id"]: row
            for row in sessions_result(self.memory, self.settings).payload["sessions"]
        }
        self.assertEqual(rows[session.id]["task_count"], 3)
        self.assertEqual(rows[other.id]["task_count"], 1)

    def test_a_declared_chain_is_located_without_being_executed(self) -> None:
        session = self.make_session()
        chain = self.declare_chain(session)
        row = sessions_result(self.memory, self.settings).payload["sessions"][0]
        self.assertEqual(
            row["chain"],
            {
                "id": chain.id,
                "digest": chain.digest,
                "status": ChainStatus.ACTIVE.value,
                "cursor": 0,
                "step_count": len(chain.steps),
                "usable": True,
            },
        )

    def test_a_session_without_a_chain_reports_none(self) -> None:
        self.make_session()
        row = sessions_result(self.memory, self.settings).payload["sessions"][0]
        self.assertIsNone(row["chain"])


class ListingBoundTests(VisibilityTestCase):
    """A listing is an operator convenience, never a bulk reader."""

    def test_the_default_bound_is_applied(self) -> None:
        for _ in range(VISIBILITY_DEFAULT_LIMIT + 5):
            self.make_session()
        payload = sessions_result(self.memory, self.settings).payload
        self.assertEqual(payload["limit"], VISIBILITY_DEFAULT_LIMIT)
        self.assertEqual(len(payload["sessions"]), VISIBILITY_DEFAULT_LIMIT)

    def test_an_explicit_bound_is_honoured(self) -> None:
        for _ in range(5):
            self.make_session()
        payload = sessions_result(self.memory, self.settings, 2).payload
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(len(payload["sessions"]), 2)

    def test_the_ceiling_is_policy(self) -> None:
        result = sessions_result(self.memory, self.settings, VISIBILITY_MAX_LIMIT + 1)
        self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
        self.assertEqual(result.payload["error"]["code"], "invalid_task")
        # And the ceiling itself is accepted, so the bound is a boundary and
        # not an off-by-one refusal of everything large.
        self.assertEqual(
            sessions_result(self.memory, self.settings, VISIBILITY_MAX_LIMIT).exit_code,
            EXIT_OK,
        )

    def test_every_unusable_bound_is_refused(self) -> None:
        for label, value in (
            ("zero", 0),
            ("negative", -1),
            ("boolean true", True),
            ("boolean false", False),
            ("float", 1.5),
            ("string", "10"),
            ("none-like list", []),
        ):
            with self.subTest(limit=label):
                session_result = sessions_result(self.memory, self.settings, value)
                self.assertEqual(
                    session_result.exit_code, EXIT_INVALID_INPUT, label
                )
                task_result = tasks_result(
                    self.memory, self.settings, "any", value
                )
                self.assertEqual(task_result.exit_code, EXIT_INVALID_INPUT, label)

    def test_the_bound_is_refused_before_the_session_is_read(self) -> None:
        """An invalid bound is invalid input, not a missing session."""
        result = tasks_result(self.memory, self.settings, "no-such-session", 0)
        self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
        self.assertEqual(result.payload["error"]["code"], "invalid_task")


class TaskListingTests(VisibilityTestCase):
    """`jarvis tasks`: one session's work, and only that session's."""

    def test_an_unknown_session_is_not_an_empty_listing(self) -> None:
        result = tasks_result(self.memory, self.settings, "no-such-session")
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertEqual(result.payload["error"]["code"], "unknown_session")

    def test_a_session_with_no_tasks_lists_nothing(self) -> None:
        session = self.make_session()
        payload = tasks_result(self.memory, self.settings, session.id).payload
        self.assertEqual(payload["tasks"], [])
        self.assertEqual(payload["count"], 0)

    def test_only_this_session_s_tasks_are_listed(self) -> None:
        mine = self.make_session()
        theirs = self.make_session()
        my_task = self.make_task(mine.id)
        their_task = self.make_task(theirs.id)
        listed = [
            row["task_id"]
            for row in tasks_result(self.memory, self.settings, mine.id).payload["tasks"]
        ]
        self.assertEqual(listed, [my_task.id])
        self.assertNotIn(their_task.id, listed)

    def test_tasks_are_newest_first_with_a_stable_tie_break(self) -> None:
        session = self.make_session()
        for _ in range(4):
            self.make_task(session.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET updated_at = ?", ("2026-08-28T00:00:00+00:00",)
            )
            connection.commit()
        first = tasks_result(self.memory, self.settings, session.id).payload["tasks"]
        second = tasks_result(self.memory, self.settings, session.id).payload["tasks"]
        identities = [row["task_id"] for row in first]
        self.assertEqual(identities, sorted(identities))
        self.assertEqual(first, second)

    def test_the_listing_carries_lifecycle_position_only(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        row = tasks_result(self.memory, self.settings, session.id).payload["tasks"][0]
        self.assertEqual(
            set(row),
            {
                "task_id",
                "session_id",
                "status",
                "stage",
                "updated_at",
                "readable",
                "capability",
                "action",
                "selected_connector",
                "chain_step_index",
            },
        )
        self.assertEqual(row["task_id"], task.id)
        self.assertEqual(row["capability"], "file_read")
        self.assertEqual(row["action"], "read")
        self.assertEqual(row["status"], TaskStatus.PENDING.value)
        self.assertEqual(row["stage"], LoopStage.UNDERSTAND.value)


class TaskInspectionTests(VisibilityTestCase):
    """`jarvis show`: what is this doing, and what should I look at next?"""

    def test_an_unknown_session_is_refused(self) -> None:
        result = show_result(self.memory, self.settings, "nope", "nope")
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertEqual(result.payload["error"]["code"], "unknown_session")

    def test_an_unknown_task_is_refused(self) -> None:
        session = self.make_session()
        result = show_result(self.memory, self.settings, session.id, "nope")
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertEqual(result.payload["error"]["code"], "task_not_found")

    def test_a_foreign_task_cannot_be_shown_under_this_session(self) -> None:
        """Ownership is proven against storage, never against the caller."""
        mine = self.make_session()
        theirs = self.make_session()
        their_task = self.make_task(theirs.id)
        result = show_result(self.memory, self.settings, mine.id, their_task.id)
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertEqual(result.payload["error"]["code"], "task_not_found")
        # The refusal must not reveal which session DOES own it.
        self.assertNotIn(theirs.id, self.rendered(result))

    def test_the_snapshot_reports_lifecycle_position(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id, selected_connector="reader")
        payload = show_result(self.memory, self.settings, session.id, task.id).payload
        self.assertEqual(payload["task"]["task_id"], task.id)
        self.assertEqual(payload["task"]["status"], TaskStatus.PENDING.value)
        self.assertEqual(payload["task"]["stage"], LoopStage.UNDERSTAND.value)
        self.assertEqual(payload["task"]["capability"], "file_read")
        self.assertEqual(payload["task"]["selected_connector"], "reader")
        self.assertIsNotNone(payload["progress"])
        self.assertEqual(payload["progress"]["task_id"], task.id)

    def test_every_attempt_state_is_reported_with_its_replayability(self) -> None:
        """The one fact an operator needs: may this be run again or not?"""
        expected = {
            AttemptState.PREPARED: True,
            AttemptState.IN_FLIGHT: False,
            AttemptState.UNKNOWN_OUTCOME: False,
            AttemptState.FINISHED: False,
        }
        for state, replayable in expected.items():
            with self.subTest(attempt=state.value):
                session = self.make_session()
                task = self.make_task(session.id)
                self.plant_attempt(session.id, task.id, state)
                payload = show_result(
                    self.memory, self.settings, session.id, task.id
                ).payload
                self.assertEqual(payload["attempt"]["state"], state.value)
                self.assertEqual(payload["attempt"]["connector"], "reader")
                self.assertIs(payload["attempt"]["replayable"], replayable)

    def test_a_task_with_no_attempt_reports_none(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        payload = show_result(self.memory, self.settings, session.id, task.id).payload
        self.assertIsNone(payload["attempt"])

    def test_a_pending_approval_reports_exactly_three_fields(self) -> None:
        """Enough to answer the request, and nothing that belongs elsewhere."""
        session = self.make_session()
        task = self.make_task(session.id)
        request = self.plant_pending_approval(session, task)
        payload = show_result(self.memory, self.settings, session.id, task.id).payload
        self.assertEqual(
            payload["pending_approval"],
            {
                "request_id": request.id,
                "fingerprint": request.fingerprint,
                "expires_at": request.expires_at,
                "readable": True,
            },
        )
        # The reason carries the user's own words and must not travel here.
        self.assertNoCanaries(self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        ), "pending approval")

    def test_a_chain_step_task_reports_where_it_sits(self) -> None:
        session = self.make_session()
        chain = self.declare_chain(session)
        task = Task(
            objective=CHAIN_DECLARATION[0]["objective"],
            capability="file_read",
            scope=ActionScope(
                action="read",
                target="notes.txt",
                data_classification=DataClassification.INTERNAL,
            ),
        )
        verdict, task_id = self.memory.materialize_chain_step(
            session.id,
            chain.id,
            chain.digest,
            0,
            replace_binding(task, chain),
        )
        self.assertEqual(verdict, "created")
        payload = show_result(self.memory, self.settings, session.id, task_id).payload
        self.assertEqual(payload["chain_step_index"], 0)
        self.assertEqual(payload["chain"]["id"], chain.id)
        self.assertEqual(payload["chain"]["status"], ChainStatus.ACTIVE.value)
        self.assertEqual(payload["chain"]["cursor"], 0)
        self.assertTrue(payload["chain"]["usable"])
        listed = tasks_result(self.memory, self.settings, session.id).payload["tasks"]
        self.assertEqual(listed[0]["chain_step_index"], 0)

    def test_an_ordinary_task_has_no_chain_position(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        payload = show_result(self.memory, self.settings, session.id, task.id).payload
        self.assertIsNone(payload["chain_step_index"])
        self.assertIsNone(payload["chain"])

    def test_the_latest_event_reports_its_type_and_never_its_payload(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.memory.persist_checkpoint(
            session, task, "unit_test_event", {"leak": SECRETS["context"]}
        )
        payload = show_result(self.memory, self.settings, session.id, task.id).payload
        self.assertEqual(payload["latest_event"]["event_type"], "unit_test_event")
        self.assertTrue(payload["latest_event"]["created_at"])
        self.assertNoCanaries(
            self.rendered(show_result(self.memory, self.settings, session.id, task.id)),
            "latest event",
        )


def replace_binding(task: Task, chain: ChainState) -> Task:
    """The trusted binding a chain step task carries, built for the fixture."""
    from jarvis.contracts import ChainBinding

    task.chain_binding = ChainBinding(
        chain_id=chain.id, chain_digest=chain.digest, step_index=0
    )
    return task


class MalformedStateTests(VisibilityTestCase):
    """A corrupted row is REPORTED. It never crashes and is never hidden."""

    def test_an_unreadable_session_is_listed_as_unreadable(self) -> None:
        healthy = self.make_session()
        broken = self.make_session()
        self.corrupt("sessions", broken.id)
        rows = {
            row["session_id"]: row
            for row in sessions_result(self.memory, self.settings).payload["sessions"]
        }
        self.assertIn(broken.id, rows, "a corrupted session was hidden")
        self.assertFalse(rows[broken.id]["readable"])
        self.assertIsNone(rows[broken.id]["active_task_id"])
        self.assertIsNone(rows[broken.id]["chain"])
        self.assertTrue(rows[healthy.id]["readable"])

    def test_an_unreadable_task_is_listed_as_unreadable(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.corrupt("tasks", task.id)
        rows = tasks_result(self.memory, self.settings, session.id).payload["tasks"]
        self.assertEqual(len(rows), 1, "a corrupted task was hidden")
        self.assertFalse(rows[0]["readable"])
        # The COLUMNS are still true, so the row remains identifiable.
        self.assertEqual(rows[0]["task_id"], task.id)
        self.assertEqual(rows[0]["status"], TaskStatus.PENDING.value)
        # Everything derived from the unreadable document is absent.
        self.assertIsNone(rows[0]["capability"])
        self.assertIsNone(rows[0]["action"])
        self.assertIsNone(rows[0]["chain_step_index"])

    def test_showing_an_unreadable_task_refuses_deterministically(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.corrupt("tasks", task.id)
        try:
            result = show_result(self.memory, self.settings, session.id, task.id)
        except Exception as exc:  # noqa: BLE001 - that would BE the defect
            self.fail(f"show raised {type(exc).__name__} instead of refusing")
        # The ROW exists, so it is REPORTED as unreadable rather than as
        # missing - that is the more useful and the more honest answer. What
        # must hold is that nothing document-derived is reported.
        self.assertEqual(result.exit_code, EXIT_OK)
        block = result.payload["task"]
        self.assertFalse(block["readable"])
        self.assertEqual(block["task_id"], task.id)
        self.assertIsNone(block["capability"])
        self.assertIsNone(block["action"])
        self.assertIsNone(block["selected_connector"])
        self.assertIsNone(result.payload["progress"])
        self.assertIsNone(result.payload["chain_step_index"])

    def test_a_corrupted_chain_is_reported_as_unusable_not_absent(self) -> None:
        session = self.make_session()
        self.declare_chain(session)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session.id,)
            ).fetchone()
            document = json.loads(row[0])
            document["chain"] = "garbage"
            connection.execute(
                "UPDATE sessions SET state_json = ? WHERE id = ?",
                (json.dumps(document), session.id),
            )
            connection.commit()
        row = sessions_result(self.memory, self.settings).payload["sessions"][0]
        self.assertIsNotNone(row["chain"], "a corrupted chain read as absent")
        self.assertEqual(row["chain"]["status"], ChainStatus.INVALID.value)
        self.assertFalse(row["chain"]["usable"])

    def test_every_command_survives_a_wholly_corrupted_database(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.corrupt("sessions", session.id)
        self.corrupt("tasks", task.id)
        for label, call in (
            ("sessions", lambda: sessions_result(self.memory, self.settings)),
            ("tasks", lambda: tasks_result(self.memory, self.settings, session.id)),
            ("show", lambda: show_result(self.memory, self.settings, session.id, task.id)),
        ):
            with self.subTest(command=label):
                try:
                    result = call()
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__}")
                self.assertIn(result.exit_code, (EXIT_OK, EXIT_NOT_FOUND))


class RedactionTests(VisibilityTestCase):
    """Nothing a user or a model wrote may travel through this surface."""

    def populated(self) -> tuple[SessionState, Task]:
        session = self.make_session()
        task = self.make_task(session.id, selected_connector="reader")
        self.plant_pending_approval(session, task)
        self.plant_attempt(session.id, task.id, AttemptState.UNKNOWN_OUTCOME)
        self.memory.persist_checkpoint(
            session, task, "unit_test_event", {"leak": SECRETS["context"]}
        )
        return session, task

    def test_no_canary_reaches_any_visibility_payload(self) -> None:
        session, task = self.populated()
        for label, result in (
            ("sessions", sessions_result(self.memory, self.settings)),
            ("tasks", tasks_result(self.memory, self.settings, session.id)),
            ("show", show_result(self.memory, self.settings, session.id, task.id)),
        ):
            with self.subTest(command=label):
                self.assertNoCanaries(self.rendered(result), label)

    def test_the_model_answer_and_file_excerpt_are_absent_by_construction(
        self,
    ) -> None:
        session, task = self.populated()
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        stored = self.memory.load_task(task.id)
        assert stored is not None
        # They really ARE persisted, so their absence is the surface's doing.
        self.assertIs(type(stored.model_answer), ModelAnswer)
        self.assertTrue(stored.model_answer.present)
        self.assertTrue(stored.local_read.present)
        for forbidden in ("model_answer", "local_read", "local_write", "context"):
            with self.subTest(field=forbidden):
                self.assertNotIn(f'"{forbidden}"', rendered)

    def test_a_credential_shaped_value_is_redacted_by_the_one_system(self) -> None:
        """The EXISTING redaction, reached the ordinary way - not a second one."""
        session = self.make_session()
        task = self.make_task(session.id, selected_connector="api_key=sk-live-abcdef")
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        self.assertNotIn("sk-live-abcdef", rendered)

    def test_the_session_objective_is_never_a_listing_field(self) -> None:
        self.make_session()
        row = sessions_result(self.memory, self.settings).payload["sessions"][0]
        self.assertEqual(
            set(row),
            {
                "session_id",
                "updated_at",
                "readable",
                "project_id",
                "active_task_id",
                "latest_task_id",
                "task_count",
                "chain",
            },
        )


class InertnessTests(VisibilityTestCase):
    """Read-only, proven structurally rather than by inspection."""

    def test_no_command_writes_a_single_row(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.declare_chain(session)
        self.plant_attempt(session.id, task.id, AttemptState.PREPARED)
        self.plant_pending_approval(session, task)
        before = self.database_content()
        for _ in range(3):
            sessions_result(self.memory, self.settings)
            tasks_result(self.memory, self.settings, session.id)
            show_result(self.memory, self.settings, session.id, task.id)
            sessions_result(self.memory, self.settings, 0)
            tasks_result(self.memory, self.settings, "no-such-session")
            show_result(self.memory, self.settings, session.id, "no-such-task")
        self.assertEqual(
            self.database_content(), before, "a visibility command wrote to storage"
        )

    def test_repeated_calls_are_byte_identical(self) -> None:
        session = self.make_session()
        self.make_task(session.id)
        first = self.rendered(sessions_result(self.memory, self.settings))
        second = self.rendered(sessions_result(self.memory, self.settings))
        self.assertEqual(first, second)

    def test_the_commands_open_no_write_transaction(self) -> None:
        """By source: a read path may never take the writable connection."""
        for name in ("list_sessions", "list_tasks", "latest_task_event"):
            with self.subTest(read=name):
                source = inspect.getsource(getattr(SQLiteMemoryStore, name))
                self.assertIn("self._connection()", source)
                self.assertNotIn("write=True", source)

    def test_the_cli_builds_no_application_registry_or_connector(self) -> None:
        """The strongest inertness proof: the factory explodes if reached."""
        session = self.make_session()
        task = self.make_task(session.id)

        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a visibility command built an application")

        for argv in (
            ["sessions"],
            ["tasks", "--session-id", session.id],
            ["show", "--session-id", session.id, "--task-id", task.id],
        ):
            with self.subTest(argv=argv[0]):
                buffer = io.StringIO()
                with (
                    mock.patch("jarvis.cli.build_default_app", forbidden),
                    contextlib.redirect_stdout(buffer),
                ):
                    code = cli_main(argv, settings=self.settings)
                self.assertEqual(code, EXIT_OK)
                self.assertEqual(json.loads(buffer.getvalue())["command"], argv[0])

    def test_the_schema_is_untouched(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.assertEqual(self.memory.schema_version(), 6)
        sessions_result(self.memory, self.settings)
        tasks_result(self.memory, self.settings, session.id)
        show_result(self.memory, self.settings, session.id, task.id)
        self.assertEqual(self.memory.schema_version(), 6)


class CommandSurfaceTests(VisibilityTestCase):
    """The three verbs, through the real CLI and its exit codes."""

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv, settings=self.settings)
        return code, buffer.getvalue()

    def test_each_verb_prints_one_json_document(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        for argv, command in (
            (["sessions"], "sessions"),
            (["tasks", "--session-id", session.id], "tasks"),
            (["show", "--session-id", session.id, "--task-id", task.id], "show"),
        ):
            with self.subTest(command=command):
                code, output = self.run_cli(argv)
                self.assertEqual(code, EXIT_OK)
                payload = json.loads(output)
                self.assertEqual(payload["command"], command)
                self.assertNoCanaries(output, command)

    def test_a_missing_required_argument_is_invalid_input(self) -> None:
        for argv in (["tasks"], ["show"], ["show", "--session-id", "x"]):
            with self.subTest(argv=" ".join(argv)):
                code, output = self.run_cli(argv)
                self.assertEqual(code, EXIT_INVALID_INPUT)
                self.assertEqual(json.loads(output)["error"]["code"], "invalid_task")

    def test_a_non_integer_limit_is_invalid_input(self) -> None:
        code, output = self.run_cli(["sessions", "--limit", "many"])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertEqual(json.loads(output)["error"]["code"], "invalid_task")

    def test_the_verbs_are_reachable_without_any_existing_database(self) -> None:
        """First run: nothing exists yet, and the answer is an empty listing."""
        fresh = Path(self.root.name) / "brand-new.db"
        self.assertFalse(fresh.exists())
        self.settings = Settings(memory_database=fresh)
        code, output = self.run_cli(["sessions"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["count"], 0)


# --------------------------------------------------------------------------
# M12 S1 hardening. Four ways the visibility surface could still have leaked,
# crashed or been widened, each closed and each pinned here.
# --------------------------------------------------------------------------


#: A chain declaration whose every user-controlled field is a canary. If any
#: of these reaches a visibility payload, the narrow projection has failed.
CANARY_DECLARATION = [
    {
        "objective": "CHAIN-OBJECTIVE-CANARY-zero",
        "capability": "file_read",
        "action": "read",
        "target": "CHAIN-TARGET-CANARY.txt",
        "consumes": -1,
    },
    {
        "objective": "CHAIN-OBJECTIVE-CANARY-one",
        "capability": "code_analysis",
        "action": "analyze",
        "destination": "CHAIN-DESTINATION-CANARY/model",
        "consumes": 0,
    },
]

CHAIN_CANARIES = (
    "CHAIN-OBJECTIVE-CANARY",
    "CHAIN-TARGET-CANARY",
    "CHAIN-DESTINATION-CANARY",
)

#: Persisted documents that are VALID JSON but not the shape their loader
#: expects. ``json.loads`` succeeds for every one of them, so the failure
#: surfaces inside the loader as AttributeError/KeyError rather than as the
#: JSONDecodeError a naive catch would expect.
_MALFORMED_DOCUMENTS = {
    "malformed json text": "{not json at all",
    "null": "null",
    "number": "123",
    "string": '"a string"',
    "list": "[]",
    "populated list": '[{"id": "x"}]',
    "empty object": "{}",
    "boolean": "true",
}


class NarrowChainProjectionTests(VisibilityTestCase):
    """F1. The declaration never crosses the visibility boundary."""

    def declare_canary_chain(self, session: SessionState) -> ChainState:
        chain = ChainState.declared(chain_steps_from_declaration(CANARY_DECLARATION))
        assert chain is not None
        self.assertEqual(
            self.memory.declare_chain_if_absent(session.id, chain), "declared"
        )
        stored = self.memory.load_session(session.id)
        assert stored is not None and stored.chain is not None
        return stored.chain

    def bound_chain_task(self, session: SessionState, chain: ChainState) -> str:
        """A real bound step task, so the chain-associated path is exercised."""
        step = chain.steps[0]
        task = Task(
            objective=step.objective,
            capability=step.capability,
            scope=ActionScope(
                action=step.action,
                target=step.target,
                data_classification=DataClassification.INTERNAL,
            ),
            chain_binding=ChainBinding(
                chain_id=chain.id, chain_digest=chain.digest, step_index=0
            ),
        )
        verdict, task_id = self.memory.materialize_chain_step(
            session.id, chain.id, chain.digest, 0, task
        )
        self.assertEqual(verdict, "created")
        return task_id

    def assertNoChainCanaries(self, text: str, label: str) -> None:
        for canary in CHAIN_CANARIES:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, text, f"{label}: {canary} leaked")

    def test_no_declaration_field_reaches_any_visibility_command(self) -> None:
        """THE F1 REPRODUCTION: objective/target/destination, all three verbs."""
        session = self.make_session()
        chain = self.declare_canary_chain(session)
        ordinary = self.make_task(session.id)
        bound = self.bound_chain_task(session, chain)
        for label, result in (
            ("sessions", sessions_result(self.memory, self.settings)),
            ("tasks", tasks_result(self.memory, self.settings, session.id)),
            ("show ordinary", show_result(
                self.memory, self.settings, session.id, ordinary.id
            )),
            ("show bound chain task", show_result(
                self.memory, self.settings, session.id, bound
            )),
        ):
            with self.subTest(command=label):
                self.assertNoChainCanaries(self.rendered(result), label)

    def test_the_chain_block_carries_orientation_only(self) -> None:
        """Exactly the fields needed to orient, and not one more."""
        session = self.make_session()
        chain = self.declare_canary_chain(session)
        task = self.make_task(session.id)
        block = show_result(
            self.memory, self.settings, session.id, task.id
        ).payload["chain"]
        self.assertEqual(
            set(block), {"id", "digest", "status", "usable", "cursor", "step_count"}
        )
        self.assertEqual(block["id"], chain.id)
        self.assertEqual(block["digest"], chain.digest)
        self.assertEqual(block["status"], ChainStatus.ACTIVE.value)
        self.assertTrue(block["usable"])
        self.assertEqual(block["cursor"], 0)
        self.assertEqual(block["step_count"], 2)
        # Explicitly NOT present: the declaration and the routing table.
        for forbidden in ("steps", "step_task_ids", "verdict"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, block)

    def test_the_wide_envelope_builder_is_untouched(self) -> None:
        """The run/resume contract still renders the declaration, as before."""
        session = self.make_session()
        self.declare_canary_chain(session)
        stored = self.memory.load_session(session.id)
        assert stored is not None
        wide = _chain_payload(stored)
        assert wide is not None
        self.assertIn("steps", wide)
        self.assertIn("step_task_ids", wide)
        self.assertEqual(
            wide["steps"][0]["objective"], CANARY_DECLARATION[0]["objective"]
        )
        # ...and the visibility projection of the SAME session does not.
        summary = self.memory.session_summary(session.id)
        assert summary is not None
        narrow = _visibility_chain_payload(summary)
        assert narrow is not None
        self.assertNotIn("steps", narrow)

    def test_visibility_does_not_call_the_wide_builder(self) -> None:
        """By source: the two projections must not be re-merged by accident."""
        source = inspect.getsource(show_result)
        self.assertIn("_visibility_chain_payload(session)", source)
        # A plain substring test would ALWAYS pass here, because the narrow
        # name ends with the wide one. The boundary is what actually matters.
        self.assertIsNone(
            re.search(r"(?<![\w])_chain_payload\(", source),
            "show_result reached the wide envelope builder",
        )


class MalformedDocumentTests(VisibilityTestCase):
    """F2. Valid JSON of the wrong shape is reported, never crashed or faked."""

    def corrupt_raw(self, table: str, row_id: str, payload: str) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                f"UPDATE {table} SET state_json = ? WHERE id = ?",  # fixed names
                (payload, row_id),
            )
            connection.commit()

    def test_no_malformed_session_document_can_crash_a_listing(self) -> None:
        """THE F2 REPRODUCTION: these used to raise AttributeError/KeyError."""
        for label, payload in _MALFORMED_DOCUMENTS.items():
            with self.subTest(document=label):
                session = self.make_session()
                self.corrupt_raw("sessions", session.id, payload)
                try:
                    rows = sessions_result(self.memory, self.settings).payload[
                        "sessions"
                    ]
                except Exception as exc:  # noqa: BLE001 - that would BE the bug
                    self.fail(f"{label}: sessions raised {type(exc).__name__}")
                row = next(r for r in rows if r["session_id"] == session.id)
                self.assertFalse(row["readable"], label)
                # Corruption is never defaulted into authoritative-looking state.
                self.assertIsNone(row["active_task_id"], label)
                self.assertIsNone(row["latest_task_id"], label)
                self.assertIsNone(row["chain"], label)

    def test_no_malformed_task_document_can_crash_a_listing(self) -> None:
        for label, payload in _MALFORMED_DOCUMENTS.items():
            with self.subTest(document=label):
                session = self.make_session()
                task = self.make_task(session.id)
                self.corrupt_raw("tasks", task.id, payload)
                try:
                    rows = tasks_result(
                        self.memory, self.settings, session.id
                    ).payload["tasks"]
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label}: tasks raised {type(exc).__name__}")
                self.assertEqual(len(rows), 1, label)
                self.assertFalse(rows[0]["readable"], label)
                self.assertIsNone(rows[0]["capability"], label)
                self.assertIsNone(rows[0]["action"], label)
                # The COLUMNS are still true, so the row stays identifiable.
                self.assertEqual(rows[0]["task_id"], task.id, label)

    def test_an_empty_document_is_never_normalized_into_readable(self) -> None:
        """``{}`` rebuilds into a perfectly valid DEFAULT object.

        That is the trap: the loader succeeds, so a naive projection would
        report a freshly-minted identity as readable state this row never
        had. Identity must AGREE with the authoritative column.
        """
        session = self.make_session()
        task = self.make_task(session.id)
        self.corrupt_raw("sessions", session.id, "{}")
        self.corrupt_raw("tasks", task.id, "{}")
        row = next(
            r
            for r in sessions_result(self.memory, self.settings).payload["sessions"]
            if r["session_id"] == session.id
        )
        self.assertFalse(row["readable"], "an empty document read as readable")
        rows = tasks_result(self.memory, self.settings, session.id).payload["tasks"]
        self.assertFalse(rows[0]["readable"], "an empty document read as readable")

    def test_a_document_naming_another_row_is_refused(self) -> None:
        """The document must agree with the column about WHICH row it is."""
        session = self.make_session()
        task = self.make_task(session.id)
        other = self.make_task(session.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()[0]
            )
            document["id"] = other.id
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        row = next(
            r
            for r in tasks_result(self.memory, self.settings, session.id).payload[
                "tasks"
            ]
            if r["task_id"] == task.id
        )
        self.assertFalse(row["readable"], "a mismatched identity read as readable")

    def test_a_document_disagreeing_about_lifecycle_is_refused(self) -> None:
        """Status/stage must agree with the columns the product acts on."""
        for field, value in (
            ("status", TaskStatus.COMPLETED.value),
            ("stage", LoopStage.EXECUTE.value),
        ):
            with self.subTest(field=field):
                session = self.make_session()
                task = self.make_task(session.id)
                with contextlib.closing(
                    sqlite3.connect(self.database)
                ) as connection:
                    document = json.loads(
                        connection.execute(
                            "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                        ).fetchone()[0]
                    )
                    document[field] = value
                    connection.execute(
                        "UPDATE tasks SET state_json = ? WHERE id = ?",
                        (json.dumps(document), task.id),
                    )
                    connection.commit()
                rows = tasks_result(
                    self.memory, self.settings, session.id
                ).payload["tasks"]
                self.assertFalse(rows[0]["readable"], field)
                # The COLUMN, not the document, is what is reported.
                self.assertEqual(rows[0]["status"], TaskStatus.PENDING.value)
                self.assertEqual(rows[0]["stage"], LoopStage.UNDERSTAND.value)

    def test_a_canonical_row_is_completely_unaffected(self) -> None:
        """POSITIVE CONTROL: the strict rules refuse nothing legitimate."""
        session = self.make_session()
        task = self.make_task(session.id)
        row = next(
            r
            for r in sessions_result(self.memory, self.settings).payload["sessions"]
            if r["session_id"] == session.id
        )
        self.assertTrue(row["readable"])
        rows = tasks_result(self.memory, self.settings, session.id).payload["tasks"]
        self.assertTrue(rows[0]["readable"])
        self.assertEqual(rows[0]["task_id"], task.id)
        self.assertEqual(rows[0]["capability"], "file_read")

    def test_the_decode_family_is_named_once(self) -> None:
        """One constant, so no projection can quietly narrow what it catches."""
        self.assertEqual(
            VISIBILITY_DECODE_ERRORS,
            (TypeError, ValueError, AttributeError, KeyError),
        )
        for name in ("_session_summary", "_task_summary", "_visibility_document"):
            with self.subTest(reader=name):
                source = inspect.getsource(getattr(SQLiteMemoryStore, name))
                self.assertNotIn("except (TypeError, ValueError)", source)

    def test_no_malformed_document_causes_a_write(self) -> None:
        """Corruption is REPORTED, never repaired."""
        session = self.make_session()
        task = self.make_task(session.id)
        self.corrupt_raw("sessions", session.id, "null")
        self.corrupt_raw("tasks", task.id, "[]")
        before = self.database_content()
        for _ in range(3):
            sessions_result(self.memory, self.settings)
            tasks_result(self.memory, self.settings, session.id)
            show_result(self.memory, self.settings, session.id, task.id)
        self.assertEqual(
            self.database_content(), before, "a malformed row was repaired"
        )


class AuxiliaryProjectionTests(VisibilityTestCase):
    """F3. Attempt, approval and event rows are bounded, never raw."""

    def prepared(self) -> tuple[SessionState, Task]:
        session = self.make_session()
        task = self.make_task(session.id)
        self.plant_attempt(session.id, task.id, AttemptState.PREPARED)
        return session, task

    def shown(self, session: SessionState, task: Task) -> dict:
        return show_result(self.memory, self.settings, session.id, task.id).payload

    def write_attempt(self, task_id: str, **columns: object) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            for name, value in columns.items():
                connection.execute(
                    f"UPDATE execution_attempts SET {name} = ? WHERE task_id = ?",
                    (value, task_id),
                )
            connection.commit()

    # -- attempts ------------------------------------------------------

    def test_an_unknown_attempt_state_never_raises_and_never_replays(self) -> None:
        """THE F3 REPRODUCTION: ``AttemptState(row)`` used to raise ValueError."""
        session, task = self.prepared()
        self.write_attempt(task.id, state="not_a_real_state")
        try:
            payload = self.shown(session, task)
        except Exception as exc:  # noqa: BLE001 - that would BE the bug
            self.fail(f"show raised {type(exc).__name__} on an unknown state")
        attempt = payload["attempt"]
        self.assertIsNone(attempt["state"])
        self.assertFalse(attempt["readable"])
        # THE security property: unknown is not "yes".
        self.assertIsNone(
            attempt["replayable"], "a corrupted attempt was shown as replayable"
        )

    def test_replayability_is_derived_from_the_trusted_enum_only(self) -> None:
        expected = {
            AttemptState.PREPARED: True,
            AttemptState.IN_FLIGHT: False,
            AttemptState.UNKNOWN_OUTCOME: False,
            AttemptState.FINISHED: False,
        }
        for state, replayable in expected.items():
            with self.subTest(state=state.value):
                session = self.make_session()
                task = self.make_task(session.id)
                self.plant_attempt(session.id, task.id, state)
                attempt = self.shown(session, task)["attempt"]
                self.assertEqual(attempt["state"], state.value)
                self.assertIs(attempt["replayable"], replayable)
                self.assertTrue(attempt["readable"])

    def test_an_oversized_attempt_field_is_bounded_not_echoed(self) -> None:
        session, task = self.prepared()
        self.write_attempt(
            task.id,
            connector="ATTEMPT-CONNECTOR-CANARY-" + "z" * 5000,
            updated_at="9" * 400,
        )
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        self.assertNotIn("ATTEMPT-CONNECTOR-CANARY", rendered)
        self.assertNotIn("9" * 40, rendered)
        attempt = self.shown(session, task)["attempt"]
        self.assertIsNone(attempt["connector"])
        self.assertIsNone(attempt["updated_at"])
        self.assertFalse(attempt["readable"])
        # The state is still VALID, so it is still reported: withholding the
        # answer because a sibling column is corrupt would hide it.
        self.assertEqual(attempt["state"], AttemptState.PREPARED.value)

    def test_an_attempt_for_another_task_is_never_shown(self) -> None:
        session, task = self.prepared()
        other = self.make_task(session.id)
        # The prepared task DOES have one, so the absence below is the query
        # scoping it correctly and not simply an empty table.
        self.assertIsNotNone(self.memory.latest_attempt_summary(task.id))
        self.assertIsNone(
            self.memory.latest_attempt_summary(other.id),
            "another task's attempt was reported",
        )

    # -- P2-1: the attempt's own identity ------------------------------

    def test_the_summary_carries_the_exact_authoritative_attempt_id(self) -> None:
        """The handle 'task resolve --attempt-id' demands.

        Without it that shipped authority is reachable only by reading the
        database by hand, which is not an operator flow.
        """
        _, task = self.prepared()
        planted = self.memory.latest_execution_attempt(task.id)
        assert planted is not None
        summary = self.memory.latest_attempt_summary(task.id)
        assert summary is not None
        self.assertEqual(summary.id, planted.id)
        self.assertTrue(summary.readable)

    def test_show_renders_the_exact_attempt_id(self) -> None:
        session, task = self.prepared()
        planted = self.memory.latest_execution_attempt(task.id)
        assert planted is not None
        attempt = self.shown(session, task)["attempt"]
        # Presence first, so a payload that simply dropped the field FAILS
        # here rather than exploding on a missing key.
        self.assertIn("id", attempt, "the attempt payload carries no id")
        self.assertEqual(attempt.get("id"), planted.id)
        # It is the ATTEMPT's identity, not the task's or the session's.
        self.assertNotEqual(attempt.get("id"), task.id)
        self.assertNotEqual(attempt.get("id"), session.id)

    def test_the_attempt_id_is_shown_in_every_state_including_unknown(self) -> None:
        """UNKNOWN_OUTCOME is the state the operator actually recovers from."""
        for state in AttemptState:
            with self.subTest(state=state.value):
                session = self.make_session()
                task = self.make_task(session.id)
                planted_id = self.plant_attempt(session.id, task.id, state)
                attempt = self.shown(session, task)["attempt"]
                self.assertEqual(attempt.get("id"), planted_id)
                self.assertEqual(attempt["state"], state.value)
                self.assertTrue(attempt["readable"])

    def test_the_payload_gained_no_other_field(self) -> None:
        session, task = self.prepared()
        self.assertEqual(
            set(self.shown(session, task)["attempt"]),
            {"id", "state", "connector", "updated_at", "replayable", "readable"},
        )

    def test_a_task_with_no_attempt_stays_bounded(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.assertIsNone(self.memory.latest_attempt_summary(task.id))
        self.assertIsNone(self.shown(session, task)["attempt"])

    def test_an_attempt_owned_by_another_task_leaks_no_id(self) -> None:
        """The identity travels only with the task that actually owns it.

        The projection is scoped by the task's own id, so an attempt
        reassigned to another task simply stops being this task's - and its
        handle must not appear anywhere in this task's output. Handing the
        operator an id belonging to someone else's attempt is precisely the
        leak the scoping exists to prevent.
        """
        session, task = self.prepared()
        planted = self.memory.latest_execution_attempt(task.id)
        assert planted is not None
        other = self.make_task(session.id)
        # ``task_id`` is the scoping column itself, so it is rewritten
        # directly rather than through ``write_attempt``.
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE execution_attempts SET task_id = ? WHERE id = ?",
                (other.id, planted.id),
            )
            connection.commit()
        self.assertIsNone(
            self.memory.latest_attempt_summary(task.id),
            "an attempt owned by another task was reported",
        )
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        self.assertNotIn(planted.id, rendered, "another task's attempt id leaked")
        # ...and it IS still reachable from the task that now owns it, so the
        # assertion above is scoping and not an emptied table.
        moved = self.memory.latest_attempt_summary(other.id)
        assert moved is not None
        self.assertEqual(moved.id, planted.id)

    def test_an_oversized_attempt_id_is_refused_not_echoed(self) -> None:
        session, task = self.prepared()
        self.write_attempt(task.id, id="ATTEMPT-ID-CANARY-" + "q" * 5000)
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        self.assertNotIn("ATTEMPT-ID-CANARY", rendered)
        attempt = self.shown(session, task)["attempt"]
        self.assertIsNone(attempt["id"])
        self.assertFalse(attempt["readable"])
        # The state is still valid, so it still travels.
        self.assertEqual(attempt["state"], AttemptState.PREPARED.value)

    def test_reading_the_attempt_id_writes_nothing(self) -> None:
        session, task = self.prepared()
        before = self.database_content()
        for _ in range(3):
            self.assertIsNotNone(self.shown(session, task)["attempt"]["id"])
            self.assertIsNotNone(self.memory.latest_attempt_summary(task.id).id)
        self.assertEqual(before, self.database_content(), "visibility wrote a row")

    # -- approvals -----------------------------------------------------

    def write_approval(self, task_id: str, session_id: str, **columns: object) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            for name, value in columns.items():
                connection.execute(
                    f"UPDATE pending_approvals SET {name} = ? "
                    "WHERE task_id = ? AND session_id = ?",
                    (value, task_id, session_id),
                )
            connection.commit()

    def test_a_malformed_approval_body_never_raises_or_leaks(self) -> None:
        """THE F3 REPRODUCTION: ``request_json=[]`` used to raise TypeError."""
        for label, payload in _MALFORMED_DOCUMENTS.items():
            with self.subTest(body=label):
                session = self.make_session()
                task = self.make_task(session.id)
                self.plant_pending_approval(session, task)
                self.write_approval(task.id, session.id, request_json=payload)
                try:
                    approval = self.shown(session, task)["pending_approval"]
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label}: show raised {type(exc).__name__}")
                self.assertFalse(approval["readable"], label)
                self.assertIsNone(approval["fingerprint"], label)
                # The trustworthy COLUMNS still travel.
                self.assertIsNotNone(approval["request_id"], label)

    def test_the_approval_reason_never_travels(self) -> None:
        """The body is rebuilt to compute a fingerprint. Nothing else escapes."""
        session = self.make_session()
        task = self.make_task(session.id)
        self.plant_pending_approval(session, task)
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        # The fixture's reason IS the objective canary, and it is persisted.
        self.assertNoCanaries(rendered, "approval body")
        approval = self.shown(session, task)["pending_approval"]
        self.assertEqual(
            set(approval),
            {"request_id", "fingerprint", "expires_at", "readable"},
        )

    def test_an_approval_body_naming_another_task_yields_no_fingerprint(
        self,
    ) -> None:
        """A body must agree with the columns about what it is approving."""
        session = self.make_session()
        task = self.make_task(session.id)
        other = self.make_task(session.id)
        self.plant_pending_approval(session, task)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT request_json FROM pending_approvals WHERE task_id = ?",
                    (task.id,),
                ).fetchone()[0]
            )
            document["task_id"] = other.id
            connection.execute(
                "UPDATE pending_approvals SET request_json = ? WHERE task_id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        approval = self.shown(session, task)["pending_approval"]
        self.assertIsNone(approval["fingerprint"])
        self.assertFalse(approval["readable"])

    def test_a_resolved_approval_is_not_shown_as_open(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.plant_pending_approval(session, task)
        self.write_approval(task.id, session.id, status="approved")
        self.assertIsNone(self.shown(session, task)["pending_approval"])

    # -- events --------------------------------------------------------

    def test_a_hostile_event_row_is_bounded_not_echoed(self) -> None:
        """THE F3 REPRODUCTION: type and timestamp were echoed verbatim."""
        session = self.make_session()
        task = self.make_task(session.id)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO event_log (session_id, task_id, event_type, "
                "payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    session.id,
                    task.id,
                    "EVENT-TYPE-CANARY-" + "q" * 5000,
                    json.dumps({"leak": "EVENT-PAYLOAD-CANARY"}),
                    "8" * 400,
                ),
            )
            connection.commit()
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        for canary in ("EVENT-TYPE-CANARY", "EVENT-PAYLOAD-CANARY", "8" * 40):
            with self.subTest(canary=canary[:20]):
                self.assertNotIn(canary, rendered)
        event = self.shown(session, task)["latest_event"]
        self.assertFalse(event["readable"])
        self.assertIsNone(event["event_type"])
        self.assertIsNone(event["created_at"])
        self.assertLess(len(rendered), 4000, "the payload was unbounded")

    def test_an_ordinary_event_is_still_reported(self) -> None:
        """POSITIVE CONTROL: real journal rows survive the bounding."""
        session = self.make_session()
        task = self.make_task(session.id)
        self.memory.persist_checkpoint(
            session, task, "unit_test_event", {"leak": SECRETS["context"]}
        )
        event = self.shown(session, task)["latest_event"]
        self.assertTrue(event["readable"])
        self.assertEqual(event["event_type"], "unit_test_event")
        self.assertTrue(event["created_at"])

    def test_the_event_payload_column_is_never_selected(self) -> None:
        """By source: the payload cannot leak from a query that never reads it."""
        source = inspect.getsource(SQLiteMemoryStore.latest_task_event)
        self.assertNotIn("payload_json", source)

    def test_every_malformed_auxiliary_row_together_still_shows_the_task(
        self,
    ) -> None:
        """All three corrupt at once: bounded, deterministic, still useful."""
        session = self.make_session()
        task = self.make_task(session.id)
        self.plant_attempt(session.id, task.id, AttemptState.PREPARED)
        self.plant_pending_approval(session, task)
        self.write_attempt(task.id, state="bogus", connector="x" * 9000)
        self.write_approval(task.id, session.id, request_json="null")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO event_log (session_id, task_id, event_type, "
                "payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (session.id, task.id, "e" * 9000, "{}", "not-a-time"),
            )
            connection.commit()
        try:
            payload = self.shown(session, task)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"show raised {type(exc).__name__}")
        self.assertFalse(payload["attempt"]["readable"])
        self.assertFalse(payload["pending_approval"]["readable"])
        self.assertFalse(payload["latest_event"]["readable"])
        # The task itself is still legible - that is the point of the command.
        self.assertEqual(payload["task"]["task_id"], task.id)
        self.assertEqual(payload["task"]["capability"], "file_read")
        self.assertIsNone(payload["attempt"]["replayable"])


class StorageHardLimitTests(VisibilityTestCase):
    """F4. The ceiling is the STORE'S, not the caller's."""

    def populate(self, count: int) -> SessionState:
        session = self.make_session()
        for _ in range(count):
            self.make_session()
            self.make_task(session.id)
        return session

    def test_no_direct_caller_can_exceed_the_ceiling(self) -> None:
        """THE F4 REPRODUCTION: list_sessions(1000) used to return everything."""
        session = self.populate(VISIBILITY_MAX_LIMIT + 30)
        for limit in (VISIBILITY_MAX_LIMIT + 1, 1_000, 10**9):
            with self.subTest(limit=limit):
                self.assertLessEqual(
                    len(self.memory.list_sessions(limit)), VISIBILITY_MAX_LIMIT
                )
                self.assertLessEqual(
                    len(self.memory.list_tasks(session.id, limit)),
                    VISIBILITY_MAX_LIMIT,
                )

    def test_the_exact_ceiling_still_works(self) -> None:
        session = self.populate(VISIBILITY_MAX_LIMIT + 30)
        self.assertEqual(
            len(self.memory.list_sessions(VISIBILITY_MAX_LIMIT)),
            VISIBILITY_MAX_LIMIT,
        )
        self.assertEqual(
            len(self.memory.list_tasks(session.id, VISIBILITY_MAX_LIMIT)),
            VISIBILITY_MAX_LIMIT,
        )

    def test_a_smaller_bound_is_still_honoured(self) -> None:
        session = self.populate(10)
        self.assertEqual(len(self.memory.list_sessions(3)), 3)
        self.assertEqual(len(self.memory.list_tasks(session.id, 3)), 3)

    def test_no_malformed_direct_limit_becomes_a_row_count(self) -> None:
        """``True`` is an ``int``. It is not one row."""
        session = self.populate(5)
        for label, value in (
            ("true", True),
            ("false", False),
            ("zero", 0),
            ("negative", -1),
            ("float", 1.0),
            ("string", "5"),
            ("none", None),
            ("list", []),
        ):
            with self.subTest(limit=label):
                self.assertEqual(self.memory.list_sessions(value), [], label)
                self.assertEqual(
                    self.memory.list_tasks(session.id, value), [], label
                )

    def test_the_bound_is_applied_to_the_query_not_the_result(self) -> None:
        """By source: a store that fetched everything and sliced would still
        have read everything."""
        for name in ("list_sessions", "list_tasks"):
            with self.subTest(read=name):
                source = inspect.getsource(getattr(SQLiteMemoryStore, name))
                self.assertIn("self._visibility_rows(limit)", source)
                self.assertIn("LIMIT ?", source)
                self.assertRegex(source, r"bound\)?,?\s*\)")

    def test_the_command_layer_still_refuses_an_over_ceiling_request(self) -> None:
        """Storage clamps; the command layer REFUSES. Both, not either."""
        result = sessions_result(
            self.memory, self.settings, VISIBILITY_MAX_LIMIT + 1
        )
        self.assertEqual(result.exit_code, EXIT_INVALID_INPUT)
        self.assertEqual(result.payload["error"]["code"], "invalid_task")


# --------------------------------------------------------------------------
# M12 S1 final corrections. A decoded document is never authoritative until
# it has been proved against the columns storage actually acts on.
# --------------------------------------------------------------------------


class SessionDocumentAuthorityTests(VisibilityTestCase):
    """F2. A session document must describe THIS row, completely."""

    def setj(self, table: str, row_id: str, payload: str) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                f"UPDATE {table} SET state_json = ? WHERE id = ?",  # fixed names
                (payload, row_id),
            )
            connection.commit()

    def document(self, session_id: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return json.loads(
                connection.execute(
                    "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()[0]
            )

    def listed(self, session_id: str) -> dict:
        return next(
            row
            for row in sessions_result(self.memory, self.settings).payload["sessions"]
            if row["session_id"] == session_id
        )

    def test_every_unusable_session_document_is_unreadable_everywhere(self) -> None:
        """THE F2 REPRODUCTION: ``{}`` and a same-id fragment read as valid."""
        session = self.make_session()
        task = self.make_task(session.id)
        canonical = json.dumps(self.document(session.id))
        cases = {
            "empty object": "{}",
            "null": "null",
            "list": "[]",
            "number": "123",
            "correct id but incomplete": json.dumps({"id": session.id}),
            "wrong embedded id": json.dumps(
                {**self.document(session.id), "id": "a-different-session"}
            ),
            "missing objective": json.dumps(
                {k: v for k, v in self.document(session.id).items() if k != "objective"}
            ),
            "missing timestamps": json.dumps(
                {
                    k: v
                    for k, v in self.document(session.id).items()
                    if k not in ("created_at", "updated_at")
                }
            ),
        }
        for label, payload in cases.items():
            with self.subTest(document=label):
                self.setj("sessions", session.id, payload)
                row = self.listed(session.id)
                self.assertFalse(row["readable"], label)
                # Nothing document-derived survives the failed proof.
                self.assertIsNone(row["active_task_id"], label)
                self.assertIsNone(row["latest_task_id"], label)
                self.assertIsNone(row["chain"], label)
                # The single-row views agree with the listing, exactly.
                summary = self.memory.session_summary(session.id)
                assert summary is not None
                self.assertFalse(summary.readable, label)
                try:
                    listing = tasks_result(self.memory, self.settings, session.id)
                    shown = show_result(
                        self.memory, self.settings, session.id, task.id
                    )
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__}")
                # The ROW exists, so the session exists; only its document is
                # unusable. The chain it might have claimed does not travel.
                self.assertEqual(listing.exit_code, EXIT_OK, label)
                self.assertIsNone(shown.payload["chain"], label)
        self.setj("sessions", session.id, canonical)
        self.assertTrue(self.listed(session.id)["readable"])

    def test_a_legacy_session_document_is_still_readable(self) -> None:
        """POSITIVE CONTROL: the documented legacy shape omits only ``chain``."""
        session = self.make_session()
        legacy = {
            k: v for k, v in self.document(session.id).items() if k != "chain"
        }
        self.assertNotIn("chain", legacy)
        self.setj("sessions", session.id, json.dumps(legacy))
        row = self.listed(session.id)
        self.assertTrue(row["readable"], "a legacy session was refused")
        self.assertIsNone(row["chain"])

    def test_an_unreadable_session_cannot_show_a_chain(self) -> None:
        """A failed proof must not leave chain orientation behind."""
        session = self.make_session()
        chain = ChainState.declared(chain_steps_from_declaration(CANARY_DECLARATION))
        assert chain is not None
        self.memory.declare_chain_if_absent(session.id, chain)
        task = self.make_task(session.id)
        self.assertIsNotNone(
            show_result(self.memory, self.settings, session.id, task.id).payload[
                "chain"
            ]
        )
        self.setj("sessions", session.id, json.dumps({"id": session.id}))
        payload = show_result(
            self.memory, self.settings, session.id, task.id
        ).payload
        self.assertIsNone(payload["chain"], "an unreadable session showed a chain")
        # ...and the declaration it was hiding does not appear either.
        for canary in CHAIN_CANARIES:
            self.assertNotIn(canary, self.rendered(
                show_result(self.memory, self.settings, session.id, task.id)
            ))

    def test_the_command_surface_uses_the_validated_projection(self) -> None:
        """By source: no visibility command may fall back to the raw loaders."""
        for function in (tasks_result, show_result):
            with self.subTest(command=function.__name__):
                source = inspect.getsource(function)
                self.assertNotIn("load_session(", source)
                self.assertNotIn("load_task(", source)
        self.assertIn("memory.session_summary(", inspect.getsource(show_result))
        self.assertIn("memory.task_summary(", inspect.getsource(show_result))
        self.assertIn("memory.session_summary(", inspect.getsource(tasks_result))


class TaskDocumentAuthorityTests(VisibilityTestCase):
    """F2. A task document must agree with the columns about what it is."""

    def setj(self, task_id: str, payload: str) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?", (payload, task_id)
            )
            connection.commit()

    def document(self, task_id: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return json.loads(
                connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0]
            )

    def test_no_forged_lifecycle_ever_reaches_the_output(self) -> None:
        """THE F2 REPRODUCTION: show emitted the document's own status/stage."""
        session = self.make_session()
        task = self.make_task(session.id)
        canonical = self.document(task.id)
        cases = {
            "empty object": {},
            "null": None,
            "list": [],
            "wrong embedded id": {**canonical, "id": "a-different-task"},
            "forged status": {**canonical, "status": TaskStatus.COMPLETED.value},
            "forged stage": {**canonical, "stage": LoopStage.EXECUTE.value},
            "forged status and stage": {
                **canonical,
                "status": TaskStatus.FAILED.value,
                "stage": LoopStage.VERIFY.value,
            },
            "missing capability": {
                k: v for k, v in canonical.items() if k != "capability"
            },
        }
        for label, document in cases.items():
            with self.subTest(document=label):
                self.setj(task.id, json.dumps(document))
                try:
                    payload = show_result(
                        self.memory, self.settings, session.id, task.id
                    ).payload
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__}")
                block = payload["task"]
                self.assertFalse(block["readable"], label)
                # The AUTHORITATIVE columns, never the document's claim.
                self.assertEqual(block["task_id"], task.id, label)
                self.assertEqual(block["status"], TaskStatus.PENDING.value, label)
                self.assertEqual(block["stage"], LoopStage.UNDERSTAND.value, label)
                # Nothing document-derived survives.
                self.assertIsNone(block["capability"], label)
                self.assertIsNone(block["action"], label)
                self.assertIsNone(block["selected_connector"], label)
                self.assertIsNone(payload["progress"], label)
                self.assertIsNone(payload["chain_step_index"], label)
                # The listing agrees, exactly.
                rows = tasks_result(
                    self.memory, self.settings, session.id
                ).payload["tasks"]
                self.assertFalse(rows[0]["readable"], label)

    def test_progress_is_withheld_from_an_unproven_document(self) -> None:
        """Progress is wholly document-derived, so it needs the proof first."""
        session = self.make_session()
        task = self.make_task(session.id)
        document = self.document(task.id)
        document["progress"] = {
            "completed_stages": [],
            "open_issues": [],
            "artifacts": [
                {
                    "kind": "file",
                    "change": "created",
                    "label": "FORGED-ARTIFACT-CANARY",
                }
            ],
        }
        document["status"] = TaskStatus.COMPLETED.value
        self.setj(task.id, json.dumps(document))
        rendered = self.rendered(
            show_result(self.memory, self.settings, session.id, task.id)
        )
        self.assertNotIn("FORGED-ARTIFACT-CANARY", rendered)
        self.assertIsNone(
            show_result(self.memory, self.settings, session.id, task.id).payload[
                "progress"
            ]
        )

    def test_a_canonical_task_reports_everything_it_should(self) -> None:
        """POSITIVE CONTROL: the strict proof refuses nothing legitimate."""
        session = self.make_session()
        task = self.make_task(session.id, selected_connector="reader")
        payload = show_result(
            self.memory, self.settings, session.id, task.id
        ).payload
        block = payload["task"]
        self.assertTrue(block["readable"])
        self.assertEqual(block["task_id"], task.id)
        self.assertEqual(block["capability"], "file_read")
        self.assertEqual(block["action"], "read")
        self.assertEqual(block["selected_connector"], "reader")
        self.assertIsNotNone(payload["progress"])
        self.assertEqual(payload["progress"]["task_id"], task.id)

    def test_an_unknown_task_is_still_not_found(self) -> None:
        """No row at all is a different answer from an unreadable row."""
        session = self.make_session()
        self.assertIsNone(self.memory.task_summary("no-such-task"))
        result = show_result(
            self.memory, self.settings, session.id, "no-such-task"
        )
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)

    def test_a_foreign_task_is_still_refused_before_anything_is_read(self) -> None:
        mine = self.make_session()
        theirs = self.make_session()
        foreign = self.make_task(theirs.id)
        result = show_result(self.memory, self.settings, mine.id, foreign.id)
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertNotIn(theirs.id, self.rendered(result))


class ApprovalIdentityTests(VisibilityTestCase):
    """F3. The body must agree with the row about which request it is."""

    def body(self, request_id: str, **overrides: object) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            document = json.loads(
                connection.execute(
                    "SELECT request_json FROM pending_approvals WHERE id = ?",
                    (request_id,),
                ).fetchone()[0]
            )
            document.update(overrides)
            connection.execute(
                "UPDATE pending_approvals SET request_json = ? WHERE id = ?",
                (json.dumps(document), request_id),
            )
            connection.commit()

    def approval(self, session: SessionState, task: Task) -> dict:
        return show_result(
            self.memory, self.settings, session.id, task.id
        ).payload["pending_approval"]

    def test_a_tampered_request_id_is_never_actionable(self) -> None:
        """THE F3 REPRODUCTION: only the embedded id was changed."""
        session = self.make_session()
        task = self.make_task(session.id)
        request = self.plant_pending_approval(session, task)
        self.body(request.id, id="A-DIFFERENT-REQUEST-ID")
        approval = self.approval(session, task)
        self.assertFalse(approval["readable"], "a mismatched body read as usable")
        self.assertIsNone(approval["fingerprint"])
        # The authoritative row id still travels - it is the handle the
        # operator needs - but nothing derived from the conflicting body does.
        self.assertEqual(approval["request_id"], request.id)
        self.assertNoCanaries(
            self.rendered(
                show_result(self.memory, self.settings, session.id, task.id)
            ),
            "tampered approval",
        )

    def test_every_identity_mismatch_stays_closed(self) -> None:
        """The existing task/session controls are retained alongside the id."""
        session = self.make_session()
        task = self.make_task(session.id)
        other = self.make_task(session.id)
        stranger = SessionState(objective="elsewhere")
        self.memory.save_session(stranger)
        request = self.plant_pending_approval(session, task)
        for label, overrides in (
            ("wrong request id", {"id": "not-this-request"}),
            ("wrong task id", {"task_id": other.id}),
            ("wrong session id", {"session_id": stranger.id}),
        ):
            with self.subTest(mismatch=label):
                self.body(request.id, id=request.id, task_id=task.id,
                          session_id=session.id)
                self.body(request.id, **overrides)
                approval = self.approval(session, task)
                self.assertFalse(approval["readable"], label)
                self.assertIsNone(approval["fingerprint"], label)

    def test_a_consistent_request_is_still_readable(self) -> None:
        """POSITIVE CONTROL: an untampered request keeps its fingerprint."""
        session = self.make_session()
        task = self.make_task(session.id)
        request = self.plant_pending_approval(session, task)
        approval = self.approval(session, task)
        self.assertTrue(approval["readable"])
        self.assertEqual(approval["request_id"], request.id)
        self.assertEqual(approval["fingerprint"], request.fingerprint)
        self.assertEqual(approval["expires_at"], request.expires_at)

    def test_a_malformed_body_root_remains_closed(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        request = self.plant_pending_approval(session, task)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE pending_approvals SET request_json = ? WHERE id = ?",
                ("[]", request.id),
            )
            connection.commit()
        approval = self.approval(session, task)
        self.assertFalse(approval["readable"])
        self.assertIsNone(approval["fingerprint"])

    def test_the_identity_proof_is_complete(self) -> None:
        """By source: all three identity fields are compared, not two."""
        source = inspect.getsource(SQLiteMemoryStore.pending_approval_summary)
        for comparison in (
            "request.id == request_id",
            "request.task_id == task_id",
            "request.session_id == session_id",
        ):
            with self.subTest(check=comparison):
                self.assertIn(comparison, source)

    def test_no_approval_check_mutates_anything(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        request = self.plant_pending_approval(session, task)
        self.body(request.id, id="mismatched")
        before = self.database_content()
        for _ in range(3):
            show_result(self.memory, self.settings, session.id, task.id)
        self.assertEqual(self.database_content(), before)


# --------------------------------------------------------------------------
# M12 S1 progress snapshot. Once a document is validated, the render must not
# be able to reach a different one.
# --------------------------------------------------------------------------


class _ForbiddenLoader(AssertionError):
    """Raised the instant an ordinary loader is reached from the show path."""


class FrozenProgressTests(VisibilityTestCase):
    """P1. Validation and render must describe the SAME document."""

    def completed_task(self, session: SessionState) -> Task:
        task = self.make_task(session.id)
        task.status = TaskStatus.COMPLETED
        task.stage = LoopStage.COMPLETED
        self.memory.save_task(session.id, task)
        return task

    def setj(self, table: str, row_id: str, payload: str) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                f"UPDATE {table} SET state_json = ? WHERE id = ?",  # fixed names
                (payload, row_id),
            )
            connection.commit()

    def document(self, table: str, row_id: str) -> dict:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return json.loads(
                connection.execute(
                    f"SELECT state_json FROM {table} WHERE id = ?",  # fixed names
                    (row_id,),
                ).fetchone()[0]
            )

    @contextlib.contextmanager
    def no_ordinary_loaders(self):
        """Make the ordinary loaders EXPLODE for the duration of the block.

        A source-text check proves only what one function spells; this proves
        what the whole call graph actually does, including anything a helper
        reaches indirectly.
        """

        def forbidden_task(_self, _task_id):
            raise _ForbiddenLoader("show reached the ordinary load_task")

        def forbidden_session(_self, _session_id):
            raise _ForbiddenLoader("show reached the ordinary load_session")

        with (
            mock.patch.object(SQLiteMemoryStore, "load_task", forbidden_task),
            mock.patch.object(SQLiteMemoryStore, "load_session", forbidden_session),
        ):
            yield

    # -- the mandatory source guarantee, behaviourally ------------------

    def test_show_never_reaches_an_ordinary_loader(self) -> None:
        """THE P1 GUARANTEE, proven by spies rather than by reading code."""
        session = self.make_session()
        task = self.completed_task(session)
        self.plant_attempt(session.id, task.id, AttemptState.PREPARED)
        self.plant_pending_approval(session, task)
        self.memory.persist_checkpoint(session, task, "unit_test_event", {})
        chain = ChainState.declared(chain_steps_from_declaration(CANARY_DECLARATION))
        assert chain is not None
        self.memory.declare_chain_if_absent(session.id, chain)
        with self.no_ordinary_loaders():
            result = show_result(self.memory, self.settings, session.id, task.id)
        self.assertEqual(result.exit_code, EXIT_OK)
        # ...and it is still a USEFUL answer, not an empty one.
        self.assertIsNotNone(result.payload["progress"])
        self.assertTrue(result.payload["task"]["readable"])
        self.assertIsNotNone(result.payload["chain"])

    def test_every_visibility_command_avoids_the_ordinary_loaders(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        with self.no_ordinary_loaders():
            self.assertEqual(
                sessions_result(self.memory, self.settings).exit_code, EXIT_OK
            )
            self.assertEqual(
                tasks_result(self.memory, self.settings, session.id).exit_code,
                EXIT_OK,
            )
            self.assertEqual(
                show_result(
                    self.memory, self.settings, session.id, task.id
                ).exit_code,
                EXIT_OK,
            )

    def test_the_spy_really_would_catch_a_reload(self) -> None:
        """NON-VACUITY: the spy fires when an ordinary loader IS reached."""
        session = self.make_session()
        with self.no_ordinary_loaders():
            with self.assertRaises(_ForbiddenLoader):
                self.memory.load_session(session.id)
            with self.assertRaises(_ForbiddenLoader):
                self.memory.load_task("anything")

    # -- 1. post-validation task replacement ---------------------------

    def test_a_task_replaced_after_validation_cannot_inject_progress(
        self,
    ) -> None:
        """THE P1 REPRODUCTION: the document swapped between proof and render."""
        session = self.make_session()
        task = self.make_task(session.id)
        forged = dict(self.document("tasks", task.id))
        forged.update(
            id="FORGED-TASK-ID-CANARY",
            status=TaskStatus.COMPLETED.value,
            stage=LoopStage.EXECUTE.value,
        )
        forged["progress"] = {
            "completed_stages": [],
            "open_issues": [],
            "artifacts": [
                {
                    "kind": "file",
                    "change": "created",
                    "label": "TOCTOU-ARTIFACT-CANARY",
                }
            ],
        }
        original = SQLiteMemoryStore.task_summary
        swapped: list[bool] = []

        def validate_then_swap(store, task_id):
            view = original(store, task_id)
            # The row is replaced the instant the projection is handed back.
            self.setj("tasks", task.id, json.dumps(forged))
            swapped.append(True)
            return view

        with mock.patch.object(
            SQLiteMemoryStore, "task_summary", validate_then_swap
        ):
            result = show_result(self.memory, self.settings, session.id, task.id)
        self.assertEqual(swapped, [True], "the fixture never swapped the row")
        rendered = result.render()
        for canary in ("FORGED-TASK-ID-CANARY", "TOCTOU-ARTIFACT-CANARY"):
            with self.subTest(canary=canary):
                self.assertNotIn(canary, rendered)
        block = result.payload["task"]
        self.assertEqual(block["task_id"], task.id)
        self.assertEqual(block["status"], TaskStatus.PENDING.value)
        self.assertEqual(block["stage"], LoopStage.UNDERSTAND.value)
        progress = result.payload["progress"]
        assert progress is not None
        self.assertEqual(progress["current_stage"], LoopStage.UNDERSTAND.value)
        self.assertEqual(progress["artifacts"], [])
        self.assertEqual(progress["task_id"], task.id)

    def test_the_replacement_itself_is_never_written_by_visibility(self) -> None:
        """The swap is the fixture's doing; the command writes nothing."""
        session = self.make_session()
        task = self.make_task(session.id)
        before = self.database_content()
        for _ in range(3):
            show_result(self.memory, self.settings, session.id, task.id)
        self.assertEqual(self.database_content(), before)

    # -- 2. unreadable session + completed task ------------------------

    def test_an_unreadable_session_never_contributes_next_step(self) -> None:
        """THE SECOND P1 REPRODUCTION: the raw session was read regardless."""
        session = self.make_session()
        task = self.completed_task(session)
        # The healthy case DOES surface it, so the absence below is the
        # verdict on the session and not simply an empty field.
        healthy = show_result(
            self.memory, self.settings, session.id, task.id
        ).payload
        self.assertEqual(
            healthy["progress"]["post_completion_next_step"], SECRETS["next_step"]
        )
        document = dict(self.document("sessions", session.id))
        document["id"] = "A-DIFFERENT-SESSION-ID"
        self.setj("sessions", session.id, json.dumps(document))
        summary = self.memory.session_summary(session.id)
        assert summary is not None
        self.assertFalse(summary.readable, "the fixture did not break the session")
        result = show_result(self.memory, self.settings, session.id, task.id)
        payload = result.payload
        # The TASK is still fine, so its own progress is still reported...
        self.assertTrue(payload["task"]["readable"])
        self.assertIsNotNone(payload["progress"])
        # ...but nothing session-derived travels.
        self.assertIsNone(payload["progress"]["post_completion_next_step"])
        self.assertNotIn(SECRETS["next_step"], result.render())
        self.assertIsNone(payload["chain"])

    def test_every_unreadable_session_shape_withholds_next_step(self) -> None:
        for label, payload in _MALFORMED_DOCUMENTS.items():
            with self.subTest(document=label):
                session = self.make_session()
                task = self.completed_task(session)
                self.setj("sessions", session.id, payload)
                result = show_result(
                    self.memory, self.settings, session.id, task.id
                )
                progress = result.payload["progress"]
                assert progress is not None
                self.assertIsNone(
                    progress["post_completion_next_step"], label
                )
                self.assertNotIn(SECRETS["next_step"], result.render(), label)

    # -- 3. task summary mismatches cannot enter progress --------------

    def test_no_task_mismatch_can_enter_the_progress_block(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        canonical = dict(self.document("tasks", task.id))
        cases = {
            "wrong task id": {**canonical, "id": "another-task"},
            "forged status": {**canonical, "status": TaskStatus.COMPLETED.value},
            "forged stage": {**canonical, "stage": LoopStage.EXECUTE.value},
        }
        for label, document in cases.items():
            with self.subTest(document=label):
                document = dict(document)
                document["progress"] = {
                    "completed_stages": [],
                    "open_issues": [],
                    "artifacts": [
                        {
                            "kind": "file",
                            "change": "created",
                            "label": "MISMATCH-ARTIFACT-CANARY",
                        }
                    ],
                }
                self.setj("tasks", task.id, json.dumps(document))
                result = show_result(
                    self.memory, self.settings, session.id, task.id
                )
                self.assertIsNone(result.payload["progress"], label)
                self.assertNotIn("MISMATCH-ARTIFACT-CANARY", result.render(), label)

    # -- 4. the positive control ---------------------------------------

    def test_a_valid_task_still_reports_its_whole_progress_block(self) -> None:
        """POSITIVE CONTROL: the allowed progress UX is not lost."""
        session = self.make_session()
        task = self.make_task(session.id)
        document = dict(self.document("tasks", task.id))
        document["progress"] = {
            "completed_stages": [LoopStage.UNDERSTAND.value],
            "open_issues": [
                {"kind": "approval_required", "error_code": None,
                 "retryable": False}
            ],
            "artifacts": [
                {"kind": "file", "change": "created", "label": "notes.txt"}
            ],
        }
        self.setj("tasks", task.id, json.dumps(document))
        progress = show_result(
            self.memory, self.settings, session.id, task.id
        ).payload["progress"]
        assert progress is not None
        self.assertEqual(
            set(progress),
            {
                "task_id",
                "plan",
                "current_stage",
                "next_stage",
                "post_completion_next_step",
                "completed_stages",
                "open_issues",
                "artifacts",
            },
        )
        self.assertEqual(progress["task_id"], task.id)
        self.assertEqual(progress["current_stage"], LoopStage.UNDERSTAND.value)
        self.assertEqual(
            progress["completed_stages"], [LoopStage.UNDERSTAND.value]
        )
        self.assertEqual(progress["artifacts"][0]["label"], "notes.txt")
        self.assertEqual(progress["open_issues"][0]["kind"], "approval_required")

    def test_the_progress_snapshot_is_frozen_at_validation(self) -> None:
        """The projection already CARRIES its progress; nothing computes it later."""
        session = self.make_session()
        task = self.make_task(session.id)
        view = self.memory.task_summary(task.id)
        assert view is not None
        self.assertIsNotNone(view.progress)
        self.assertEqual(view.progress.current_stage, LoopStage.UNDERSTAND.value)
        # An unreadable projection carries none at all.
        self.setj("tasks", task.id, "{}")
        broken = self.memory.task_summary(task.id)
        assert broken is not None
        self.assertFalse(broken.readable)
        self.assertIsNone(broken.progress)

    def test_the_session_next_step_is_bounded_by_the_canonical_rule(self) -> None:
        """The visibility copy uses the SAME rule the loader applies."""
        session = self.make_session()
        document = dict(self.document("sessions", session.id))
        document["next_step"] = "x" * 5000
        self.setj("sessions", session.id, json.dumps(document))
        summary = self.memory.session_summary(session.id)
        assert summary is not None
        self.assertTrue(summary.readable)
        assert summary.next_step is not None
        self.assertLessEqual(len(summary.next_step), PROGRESS_TEXT_MAX_CHARS)


class AppliedEffectProjectionTests(VisibilityTestCase):
    """P2. The one applied-effect fact, and everything that must not produce it.

    It answers a question about the WORLD - did this irreversible write
    already land - which is exactly why it may not be derived from the task's
    LIFECYCLE. A fact taken from CANCELLED would be manufactured by the very
    cancellation it exists to qualify; a fact suppressed by COMPLETED would
    hide a true one. So the record decides, alone, and only when it survives
    the one canonical structural contract and says exactly ``written``.
    """

    def written_record(self, **overrides: object) -> LocalWriteRecord:
        record = LocalWriteRecord(
            relative_path="notes.txt",
            mode="create",
            pre_present=False,
            pre_sha256="",
            pre_byte_size=0,
            intended_sha256="a" * 64,
            intended_byte_size=5,
            status="written",
            written_at="2026-08-28T00:00:00+00:00",
        )
        self.assertIsNone(
            local_write_record_refusal(record), "the fixture is not a valid record"
        )
        return replace(record, **overrides) if overrides else record

    def task_with(self, record: LocalWriteRecord | None, **fields: object):
        session = self.make_session()
        task = self.make_task(session.id, capability=LOCAL_WRITE_CAPABILITY, **fields)
        if record is not None:
            task.local_write = record
            self.memory.save_task(session.id, task)
        return session, task

    def effect(self, session, task):
        return show_result(
            self.memory, self.settings, session.id, task.id
        ).payload["effect_applied"]

    # -- the one positive case -----------------------------------------

    def test_a_written_record_is_reported_as_applied(self) -> None:
        session, task = self.task_with(self.written_record())
        self.assertEqual(
            self.effect(session, task),
            {"kind": APPLIED_EFFECT_LOCAL_WRITE, "status": "written"},
        )
        summary = self.memory.task_summary(task.id)
        assert summary is not None
        assert summary.effect_applied is not None
        self.assertEqual(summary.effect_applied.kind, APPLIED_EFFECT_LOCAL_WRITE)
        self.assertEqual(summary.effect_applied.status, "written")

    def test_it_is_derived_from_the_record_and_never_from_the_status(self) -> None:
        """The same record under every lifecycle, and one answer throughout."""
        for status, stage in (
            (TaskStatus.CANCELLED, LoopStage.CANCELLED),
            (TaskStatus.COMPLETED, LoopStage.COMPLETED),
            (TaskStatus.FAILED, LoopStage.FAILED),
            (TaskStatus.RUNNING, LoopStage.EXECUTE),
        ):
            with self.subTest(status=status.value):
                session, task = self.task_with(
                    self.written_record(), status=status, stage=stage
                )
                self.assertIsNotNone(
                    self.effect(session, task),
                    "a lifecycle state suppressed a true applied effect",
                )

    # -- negative controls ---------------------------------------------

    def test_a_cancelled_task_that_wrote_nothing_reports_no_effect(self) -> None:
        """CANCELLED alone is not evidence of anything having happened."""
        session, task = self.task_with(
            None, status=TaskStatus.CANCELLED, stage=LoopStage.CANCELLED
        )
        self.assertFalse(task.local_write.present)
        self.assertIsNone(self.effect(session, task))

    def test_a_task_with_no_local_write_reports_no_effect(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id)
        self.assertIsNone(self.effect(session, task))

    def test_a_non_write_capability_reports_no_effect(self) -> None:
        """The weak half: a non-write task that carries no record at all."""
        session = self.make_session()
        task = self.make_task(session.id, capability="echo")
        self.assertIsNone(self.effect(session, task))

    def test_a_non_write_task_cannot_manufacture_an_effect(self) -> None:
        """THE adversarial case: a valid written record on a task that
        could never have written anything.

        A ``LocalWriteRecord`` is a DESCRIPTION. Any task document can carry
        a perfectly canonical one - injected into a row, restored from a
        tampered store, or copied from a real write - and the record's own
        structural contract has no way to notice, because nothing in it says
        which capability produced it. Validating the record alone therefore
        let an ``echo`` or ``file_read`` task assert a durable, irreversible
        effect it has no mechanism to have caused.

        The record is deliberately fully valid here. A malformed one would
        be refused by the structural contract and would prove nothing about
        the capability guard; only a record that passes every other check can
        show that the capability is what does the refusing.
        """
        canary = "INJECTED-WRITE-CANARY.txt"
        record = self.written_record(relative_path=canary)
        self.assertIsNone(
            local_write_record_refusal(record),
            "the injected record must be fully valid for this to be a test",
        )
        for capability in ("echo", LOCAL_READ_CAPABILITY, "code_analysis"):
            with self.subTest(capability=capability):
                session = self.make_session()
                task = self.make_task(session.id, capability=capability)
                # Injected into the PERSISTED document, the way a hostile or
                # tampered row would carry it - not through a write flow,
                # because no write flow could produce this task.
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    raw = db.execute(
                        "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                    ).fetchone()[0]
                    document = json.loads(raw)
                    document["local_write"] = record.to_dict()
                    db.execute(
                        "UPDATE tasks SET state_json = ? WHERE id = ?",
                        (json.dumps(document), task.id),
                    )
                    db.commit()
                # The row stays otherwise VALID: this is not a corrupted
                # document degrading to unreadable, it is a readable one
                # making a false claim.
                summary = self.memory.task_summary(task.id)
                assert summary is not None
                self.assertTrue(summary.readable, "the row must stay readable")
                self.assertEqual(summary.capability, capability)
                self.assertIsNone(
                    summary.effect_applied,
                    f"a {capability!r} task manufactured an applied effect",
                )
                result = show_result(
                    self.memory, self.settings, session.id, task.id
                )
                rendered = self.rendered(result)
                self.assertIsNone(result.payload["effect_applied"])
                self.assertNotIn(canary, rendered, "the injected path leaked")
                self.assertNotIn(record.intended_sha256, rendered)

    def test_the_capability_is_never_inferred_from_the_record(self) -> None:
        """Presence of write-shaped data is not evidence of a write."""
        session = self.make_session()
        task = self.make_task(session.id, capability="echo")
        task.local_write = self.written_record()
        self.memory.save_task(session.id, task)
        summary = self.memory.task_summary(task.id)
        assert summary is not None
        self.assertEqual(summary.capability, "echo")
        self.assertIsNone(summary.effect_applied)

    def test_reading_a_manufactured_claim_writes_nothing(self) -> None:
        session = self.make_session()
        task = self.make_task(session.id, capability="echo")
        task.local_write = self.written_record()
        self.memory.save_task(session.id, task)
        before = self.database_content()
        for _ in range(3):
            self.assertIsNone(self.effect(session, task))
        self.assertEqual(before, self.database_content(), "visibility wrote a row")

    def test_every_non_written_status_reports_no_effect(self) -> None:
        """prepared, refused and unknown are each a different NOT-written."""
        for status in ("prepared", "refused", "unknown"):
            with self.subTest(status=status):
                session, task = self.task_with(self.written_record(status=status))
                self.assertIsNone(
                    self.effect(session, task),
                    f"'{status}' was reported as an applied effect",
                )

    def test_a_status_outside_the_vocabulary_reports_no_effect(self) -> None:
        for status in ("WRITTEN", "written ", "written!", "", "wr"):
            with self.subTest(status=repr(status)):
                session, task = self.task_with(self.written_record(status=status))
                self.assertIsNone(self.effect(session, task))

    def test_a_malformed_record_reports_no_effect_and_echoes_nothing(self) -> None:
        """Structurally invalid, yet claiming ``written``. Refused whole."""
        canary = "WRITE-RECORD-CANARY"
        for label, record in (
            ("bad digest", self.written_record(intended_sha256=canary)),
            ("absolute path", self.written_record(relative_path=f"C:/{canary}.txt")),
            ("traversal", self.written_record(relative_path=f"../{canary}.txt")),
            ("mode contradicts pre-state", self.written_record(pre_present=True)),
            ("bad mode", self.written_record(mode=canary)),
            ("bad timestamp", self.written_record(written_at=canary)),
            (
                "oversized",
                self.written_record(intended_byte_size=LOCAL_WRITE_MAX_BYTES + 1),
            ),
        ):
            with self.subTest(label=label):
                self.assertIsNotNone(
                    local_write_record_refusal(record), "the fixture is not malformed"
                )
                session, task = self.task_with(record)
                rendered = self.rendered(
                    show_result(self.memory, self.settings, session.id, task.id)
                )
                self.assertNotIn(canary, rendered, "a tampered value was echoed")
                self.assertIsNone(json.loads(rendered)["effect_applied"])

    def test_a_document_that_denies_its_own_row_reports_no_effect(self) -> None:
        """No effect from state whose task identity is not proven.

        The projection freezes the effect only after identity and lifecycle
        agree with the authoritative columns, so a document carrying a
        perfectly valid written record while claiming to be a different task -
        or a different lifecycle - contributes nothing at all.
        """
        for label, mutate in (
            ("foreign id", lambda document: {**document, "id": "another-task"}),
            ("forged status", lambda document: {**document, "status": "completed"}),
            ("forged stage", lambda document: {**document, "stage": "completed"}),
        ):
            with self.subTest(label=label):
                session, task = self.task_with(self.written_record())
                with contextlib.closing(sqlite3.connect(self.database)) as connection:
                    raw = connection.execute(
                        "SELECT state_json FROM tasks WHERE id = ?", (task.id,)
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE tasks SET state_json = ? WHERE id = ?",
                        (json.dumps(mutate(json.loads(raw))), task.id),
                    )
                    connection.commit()
                payload = show_result(
                    self.memory, self.settings, session.id, task.id
                ).payload
                self.assertFalse(payload["task"]["readable"])
                self.assertIsNone(
                    payload["effect_applied"], "an unproven document produced an effect"
                )

    def test_another_tasks_written_record_never_travels(self) -> None:
        """The effect belongs to the task that applied it, and to no other."""
        session, applied = self.task_with(self.written_record())
        bystander = self.make_task(session.id, capability=LOCAL_WRITE_CAPABILITY)
        self.assertIsNotNone(self.effect(session, applied))
        self.assertIsNone(
            self.effect(session, bystander), "a sibling task inherited the effect"
        )

    def test_a_foreign_session_cannot_read_the_effect_at_all(self) -> None:
        _, task = self.task_with(self.written_record())
        stranger = self.make_session()
        result = show_result(self.memory, self.settings, stranger.id, task.id)
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND)
        self.assertNotIn("effect_applied", self.rendered(result))

    def test_reporting_the_effect_writes_nothing(self) -> None:
        session, task = self.task_with(self.written_record())
        before = self.database_content()
        for _ in range(3):
            self.assertIsNotNone(self.effect(session, task))
        self.assertEqual(before, self.database_content(), "visibility wrote a row")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
