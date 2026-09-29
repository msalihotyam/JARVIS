"""V2 Projects Gate 3: project lifecycle and conversation scope propagation.

A. create / rename / archive / unarchive, deterministic refusals, opaque ids
   that schema-10 releases still load, schema version untouched.
B. A chat request's project scope comes ONLY from its conversation owner:
   Task.project_id, session binding, project memory and the job project.
C. No cross-project memory leakage; an owner/binding mismatch fails closed.
D. Archive: conversations kept and read-only, no new task (even through an
   already-bound session), no memory write, scheduled work refused visibly;
   unarchive restores exactly.
E. Scope is never authority: the same action under Global, A and B yields the
   same approval, destination, attestation and grant.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

from jarvis.app import EXIT_OK, create_app, job_create_result, project_create_result, project_lifecycle_result
from jarvis.contracts import (
    PROJECT_ID_PATTERN,
    DataClassification,
    ErrorCode,
    JobRunStatus,
    ProjectMemoryItem,
    ProjectMemoryKind,
    ProjectRecord,
    RunStatus,
    SessionState,
)
from jarvis.dispatch import DISPATCH_REFUSED, DISPATCH_SETTLED
from jarvis.intent import IntentStatus
from jarvis.memory import SCHEMA_VERSION, SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.scheduler_runner import ACTION_DISPATCHED, ACTION_RECOVERED
from jarvis.settings import Settings
from tests.helpers import make_task, safe_cleanup
from tests.test_intent import IntentTestCase, ScriptedModel, proposal, task_proposal, tomorrow_at_ten
from tests.test_scheduler_runner import (  # noqa: F401
    NOW,
    RunnerTestCase,
    setUpModule,
    tearDownModule,
)

OBJECTIVE = "alpha deploy notes"


def fact(project_id: str, text: str) -> ProjectMemoryItem:
    return ProjectMemoryItem(
        project_id=project_id, kind=ProjectMemoryKind.FACT, text=text,
        classification=DataClassification.INTERNAL,
    )


def job_proposal() -> str:
    return proposal(
        kind="job", label="notes", schedule={"kind": "one_time", "at": tomorrow_at_ten()},
        step={"objective": "read the notes", "capability": "code_analysis", "target": ""},
    )


class ScopeTestCase(IntentTestCase):
    def rows(self, sql: str) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return [tuple(row) for row in connection.execute(sql).fetchall()]

    def projects(self) -> list[tuple]:
        return self.rows("SELECT id, name, active, created_at FROM projects ORDER BY id")

    def schema(self) -> tuple:
        return (
            self.rows("SELECT version FROM schema_version"),
            self.rows("SELECT version FROM schema_migrations ORDER BY version"),
        )

    def read(self, conversation_id: str, target: str = "notes.txt", **kwargs):
        """A LOW local read (runs without approval) from ``conversation_id``."""
        model = ScriptedModel(task_proposal("file_read", objective=OBJECTIVE, target=target))
        app = self.build(model)
        return model, app.submit_text(OBJECTIVE, conversation_id=conversation_id, **kwargs).payload

    def retrieved_texts(self, task_id: str) -> set[str]:
        return {item.summary for item in self.memory.load_task(task_id).retrieved_context.items}


# --------------------------------------------------------------------------
# A. Lifecycle
# --------------------------------------------------------------------------


class LifecycleTests(ScopeTestCase):
    def test_create_gives_an_opaque_id_that_schema_10_loads(self) -> None:
        before = self.schema()
        first = self.memory.create_project("  Evim  ")
        second = self.memory.create_project("Evim 2")
        for project in (first, second):
            self.assertRegex(project.id, r"^p-[0-9a-f]{32}$")
            self.assertTrue(PROJECT_ID_PATTERN.fullmatch(project.id))
            self.assertNotIn("evim", project.id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual("Evim", first.name)
        # Exactly what a schema-10 release reads: the raw row through ProjectRecord.
        row = self.rows(f"SELECT id, name, active, created_at FROM projects WHERE id = '{first.id}'")[0]
        self.assertEqual(first, ProjectRecord(id=row[0], name=row[1], active=bool(row[2]), created_at=row[3]))
        self.assertEqual(first, self.memory.load_project(first.id))
        self.assertEqual(before, self.schema())
        self.assertEqual([(SCHEMA_VERSION,)], self.schema()[0])

    def test_rename_archive_unarchive_change_exactly_one_column(self) -> None:
        slug = ProjectRecord(id="evim", name="Evim")
        self.memory.save_project(slug)
        project = self.memory.create_project("Iş")
        renamed = self.memory.rename_project(project.id, "İş")
        self.assertEqual((project.id, "İş", True, project.created_at),
                         (renamed.id, renamed.name, renamed.active, renamed.created_at))
        archived = self.memory.set_project_active(project.id, False)
        self.assertFalse(archived.active)
        self.assertEqual(archived, self.memory.load_project(project.id))
        self.assertEqual(renamed, self.memory.set_project_active(project.id, True))
        self.assertEqual(slug, self.memory.load_project("evim"))              # existing slug untouched

    def test_refusals_are_deterministic_and_write_nothing(self) -> None:
        project = self.memory.create_project("Evim")
        self.memory.create_project("Iş")
        before, schema = self.projects(), self.schema()
        refusals = [
            (lambda: self.memory.create_project("evim "), "already exists"),            # duplicate
            (lambda: self.memory.create_project("   "), "non-blank"),
            (lambda: self.memory.create_project("x" * 121), "longer than"),
            (lambda: self.memory.create_project(None), "non-blank"),
            (lambda: self.memory.rename_project("p-" + "0" * 32, "Yeni"), "Unknown project"),
            (lambda: self.memory.rename_project("../etc", "Yeni"), "project_id"),
            (lambda: self.memory.rename_project(project.id, "Evim"), "already has that name"),
            (lambda: self.memory.rename_project(project.id, "iş"), "already exists"),
            (lambda: self.memory.rename_project(project.id, ""), "non-blank"),
            (lambda: self.memory.rename_project(project.id, "y" * 121), "longer than"),
            (lambda: self.memory.set_project_active(project.id, True), "already active"),
            (lambda: self.memory.set_project_active("p-" + "1" * 32, False), "Unknown project"),
            (lambda: self.memory.set_project_active(project.id, 0), "bool"),
        ]
        for call, message in refusals:
            for _ in range(2):                                   # same refusal every time
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    call()
        self.memory.set_project_active(project.id, False)
        with self.assertRaisesRegex(ValueError, "already archived"):
            self.memory.set_project_active(project.id, False)
        self.memory.set_project_active(project.id, True)
        self.assertEqual(before, self.projects())
        self.assertEqual(schema, self.schema())

    def test_app_and_cli_surface(self) -> None:
        created = project_create_result(self.memory, self.settings, None, "Evim")
        self.assertEqual(EXIT_OK, created.exit_code, created.payload)
        project_id = created.payload["project"]["id"]
        self.assertTrue(project_id.startswith("p-"))
        slug = project_create_result(self.memory, self.settings, "isim", "Isim")
        self.assertEqual("isim", slug.payload["project"]["id"])
        for action, name, active in (("rename", "Ev", True), ("archive", None, False), ("unarchive", None, True)):
            result = project_lifecycle_result(self.memory, self.settings, action, project_id, name)
            self.assertEqual(EXIT_OK, result.exit_code, result.payload)
            self.assertEqual(active, result.payload["project"]["active"])
        refused = project_lifecycle_result(self.memory, self.settings, "unarchive", project_id)
        self.assertNotEqual(EXIT_OK, refused.exit_code)
        self.assertIn("already active", refused.payload["error"]["message"])
        self.assertNotEqual(EXIT_OK, project_lifecycle_result(self.memory, self.settings, "delete", project_id).exit_code)
        self.assertIsNotNone(self.memory.load_project(project_id))           # never deleted


# --------------------------------------------------------------------------
# B / C. Scope propagation and isolation
# --------------------------------------------------------------------------


class ScopePropagationTests(ScopeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.memory.create_project("A")
        self.memory.save_memory_item(fact(self.a.id, "alpha deploy notes fact of project A"))

    def test_project_conversation_scopes_task_session_and_memory(self) -> None:
        b = self.memory.create_project("B")
        self.memory.save_memory_item(fact(b.id, "alpha deploy notes fact of project B"))
        conversation = self.memory.create_conversation(self.a.id)
        model, payload = self.read(conversation.id)
        self.assertEqual("completed", payload["run"]["run_status"], payload)
        task_id, session_id = payload["run"]["task"]["id"], payload["run"]["session"]["id"]
        self.assertEqual(self.a.id, self.memory.load_task(task_id).project_id)
        self.assertEqual(self.a.id, self.memory.load_session(session_id).project_id)
        texts = self.retrieved_texts(task_id)
        self.assertIn("alpha deploy notes fact of project A", texts)
        self.assertNotIn("alpha deploy notes fact of project B", texts)        # C: no B leakage
        self.assertEqual(self.a.id, model.calls[0].context["catalog"]["scheduler"]["project"])

    def test_b_conversation_never_sees_a_memory(self) -> None:
        b = self.memory.create_project("B")
        _model, payload = self.read(self.memory.create_conversation(b.id).id)
        task_id = payload["run"]["task"]["id"]
        self.assertEqual(b.id, self.memory.load_task(task_id).project_id)
        self.assertFalse(any("project A" in text for text in self.retrieved_texts(task_id)))

    def test_global_is_none_end_to_end_even_with_one_active_project(self) -> None:
        self.assertEqual(1, len([p for p in self.memory.list_projects() if p.active]))
        model, payload = self.read(self.memory.create_conversation().id)
        task_id, session_id = payload["run"]["task"]["id"], payload["run"]["session"]["id"]
        self.assertIsNone(self.memory.load_task(task_id).project_id)
        self.assertIsNone(self.memory.load_session(session_id).project_id)
        self.assertFalse(any("project A" in text for text in self.retrieved_texts(task_id)))
        self.assertIsNone(model.calls[0].context["catalog"]["scheduler"]["project"])
        # The unsaved window conversation ("") is Global too.
        _model, payload = self.read("")
        self.assertIsNone(self.memory.load_task(payload["run"]["task"]["id"]).project_id)

    def test_job_project_is_the_conversation_owner(self) -> None:
        app = self.build(ScriptedModel(job_proposal()))
        payload = app.submit_text("yarın notları oku", conversation_id=self.memory.create_conversation().id).payload
        self.assertNothingHappened(payload, IntentStatus.UNSUPPORTED)          # Global: no fallback to A
        self.assertIn("Global conversation", payload["intent"]["message"])
        self.assertEqual(0, self.table_count("jobs"))
        app = self.build(ScriptedModel(job_proposal()))
        payload = app.submit_text("yarın notları oku", conversation_id=self.memory.create_conversation(self.a.id).id).payload
        self.assertEqual(IntentStatus.SCHEDULED.value, payload["intent"]["status"], payload)
        self.assertEqual(self.a.id, payload["job"]["project_id"])
        # The CLI path (no conversation) keeps the single-active-project fallback.
        app = self.build(ScriptedModel(job_proposal()))
        payload = app.submit_text("yarın notları oku").payload
        self.assertEqual(self.a.id, payload["job"]["project_id"])

    def test_owner_binding_mismatch_fails_closed(self) -> None:
        b = self.memory.create_project("B")
        in_a = self.memory.create_conversation(self.a.id)
        _model, payload = self.read(in_a.id)
        session_a = payload["run"]["session"]["id"]
        self.memory.link_session_conversation(session_a, in_a.id)
        global_conversation = self.memory.create_conversation()
        _model, payload = self.read(global_conversation.id)
        session_global = payload["run"]["session"]["id"]
        self.memory.link_session_conversation(session_global, global_conversation.id)
        tasks = self.table_count("tasks")
        cases = (
            (session_a, self.memory.create_conversation(b.id).id),               # bound A, owner B
            (session_a, self.memory.create_conversation().id),                   # bound A, owner Global
            (session_global, self.memory.create_conversation(self.a.id).id),     # linked Global, owner A
            (None, "c-" + "0" * 32),                                             # unknown conversation
            (None, "not-a-conversation"),
        )
        for session_id, conversation_id in cases:
            with self.subTest(conversation=conversation_id):
                model, payload = self.read(conversation_id, session_id=session_id)
                self.assertEqual(IntentStatus.INVALID_INPUT.value, payload["intent"]["status"], payload)
                self.assertEqual([], model.calls)
        self.assertEqual(tasks, self.table_count("tasks"))
        # Core itself refuses a task that names another project than its session.
        task = make_task()
        task.project_id = b.id
        core_run = self.app.core.run(task, session_a)
        self.assertEqual(RunStatus.FAILED, core_run.status)
        self.assertEqual(ErrorCode.INVALID_TASK, core_run.error.code)


# --------------------------------------------------------------------------
# D. Archive semantics
# --------------------------------------------------------------------------


class ArchiveTests(ScopeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.memory.create_project("A")
        self.item = self.memory.save_memory_item(fact(self.a.id, "alpha deploy notes fact of project A"))
        self.conversation = self.memory.create_conversation(self.a.id, title="A1")

    def snapshot(self) -> tuple:
        return (
            self.projects(),
            self.rows("SELECT * FROM conversations ORDER BY id"),
            self.rows("SELECT * FROM project_memory_items ORDER BY id"),
            self.rows("SELECT * FROM conversation_sessions ORDER BY session_id"),
        )

    def test_conversations_are_kept_readable_and_read_only(self) -> None:
        state = self.memory.load_conversation_state(self.conversation.id)
        before = self.snapshot()
        self.memory.set_project_active(self.a.id, False)
        self.assertEqual(state, self.memory.load_conversation_state(self.conversation.id))
        self.assertIn(self.conversation.id, [c.id for c in self.memory.list_conversations(project_id=self.a.id)])
        session = SessionState(objective="x")
        self.memory.save_session(session)
        for call in (
            lambda: self.memory.update_conversation_state(self.conversation.id, lambda s: s),
            lambda: self.memory.set_conversation_title(self.conversation.id, "B"),
            lambda: self.memory.archive_conversation(self.conversation.id),
            lambda: self.memory.link_session_conversation(session.id, self.conversation.id),
            lambda: self.memory.create_conversation(self.a.id),
        ):
            with self.assertRaises(ValueError):
                call()
        model, payload = self.read(self.conversation.id)
        self.assertNothingHappened(payload, IntentStatus.INVALID_INPUT)
        self.assertIn("archived", payload["intent"]["message"])
        self.assertEqual([], model.calls)
        self.memory.set_project_active(self.a.id, True)
        self.assertEqual(before, self.snapshot())                            # unarchive restores exactly
        self.memory.set_conversation_title(self.conversation.id, "A1")

    def test_no_memory_write_while_archived(self) -> None:
        self.memory.set_project_active(self.a.id, False)
        with self.assertRaisesRegex(ValueError, "inactive project"):
            self.memory.save_memory_item(fact(self.a.id, "another fact"))
        with self.assertRaisesRegex(ValueError, "inactive project"):
            self.memory.retire_memory_item(self.a.id, self.item.id)
        self.memory.set_project_active(self.a.id, True)
        self.memory.retire_memory_item(self.a.id, self.item.id)

    def test_no_new_task_even_through_an_already_bound_session(self) -> None:
        _model, payload = self.read(self.conversation.id)
        session_id = payload["run"]["session"]["id"]
        self.assertEqual(self.a.id, self.memory.load_session(session_id).project_id)
        self.memory.set_project_active(self.a.id, False)
        core = self.app.core
        tasks, calls = self.table_count("tasks"), self.execute_calls()
        explicit = make_task()
        explicit.project_id = self.a.id
        for task, session in ((make_task(), session_id), (explicit, session_id), (explicit, None)):
            run = core.run(task, session)                    # bound session, or a new binding
            self.assertEqual(RunStatus.FAILED, run.status)
            self.assertIn("inactive project", run.error.message)
        self.assertEqual((tasks, calls), (self.table_count("tasks"), self.execute_calls()))
        self.memory.set_project_active(self.a.id, True)
        _model, payload = self.read(self.conversation.id, session_id=session_id)
        self.assertEqual("completed", payload["run"]["run_status"], payload)

    def test_a_parked_task_does_not_resume_until_unarchived(self) -> None:
        app = self.build(ScriptedModel(task_proposal("code_analysis", objective=OBJECTIVE)))
        run = app.submit_text(OBJECTIVE, conversation_id=self.conversation.id).payload["run"]
        self.assertEqual("awaiting_approval", run["run_status"], run)
        self.memory.set_project_active(self.a.id, False)
        args = (run["session"]["id"], run["task"]["id"], run["approval"]["request_id"], run["approval"]["fingerprint"])
        refused = app.approve(*args)
        self.assertNotEqual(EXIT_OK, refused.exit_code)
        self.assertEqual(0, self.claude.execute_calls)
        self.memory.set_project_active(self.a.id, True)
        approved = app.approve(*args)
        self.assertEqual(EXIT_OK, approved.exit_code, approved.payload)
        self.assertEqual(1, self.claude.execute_calls)


class ArchivedJobTests(RunnerTestCase):
    """Scheduled work of an archived project does not execute, visibly."""

    def test_a_due_job_is_refused_visibly_and_runs_after_unarchive(self) -> None:
        self.memory.create_job(self.spec(steps=1), now=NOW)
        self.memory.set_project_active("proj-a", False)
        action = self.only_action(self.runner(at=self.at).run_once())
        self.assertEqual((ACTION_DISPATCHED, DISPATCH_REFUSED), (action.action, action.verdict))
        self.assertIn("inactive project", action.reason)
        self.assertEqual(0, len(self.reader.calls))
        self.assertFalse(self.reload(action.run_id).settled)
        self.memory.set_project_active("proj-a", True)
        action = self.only_action(self.runner(at=self.after).run_once())
        self.assertEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        self.assertEqual(1, len(self.reader.calls))

    def test_an_already_bound_generation_is_blocked_not_settled(self) -> None:
        run, _dispatch = self.bound()
        self.memory.set_project_active("proj-a", False)
        action = self.only_action(self.runner().run_once())
        self.assertEqual(ACTION_RECOVERED, action.action)
        self.assertNotEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        self.assertEqual(0, len(self.reader.calls))
        self.assertEqual(JobRunStatus.RUNNING, self.reload(run.id).status)
        self.memory.set_project_active("proj-a", True)
        later = self.after + timedelta(hours=1)
        action = self.only_action(self.runner(at=later).run_once())
        self.assertEqual(DISPATCH_SETTLED, action.verdict, action.reason)
        self.assertEqual(1, len(self.reader.calls))


# --------------------------------------------------------------------------
# E. The hard invariant: Global <-> Project never changes authority
# --------------------------------------------------------------------------


class ScopeIsNotAuthorityTests(IntentTestCase):
    """Three isolated worlds, identical except for the conversation owner."""

    SCOPE_FREE = ("request_id", "fingerprint", "scope_fingerprint", "expires_at")

    def world(self, owner: str | None) -> dict:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.database = Path(temp.name) / "jarvis.db"
        self.memory = SQLiteMemoryStore(self.database)
        self.settings = Settings(memory_database=self.database)
        for project_id in ("proj-a", "proj-b"):
            self.memory.save_project(ProjectRecord(id=project_id, name=project_id, created_at="t"))
        conversation = self.memory.create_conversation(owner)
        model = ScriptedModel(task_proposal("code_analysis", objective=OBJECTIVE))
        app = self.build(model)
        result = app.submit_text(OBJECTIVE, conversation_id=conversation.id)
        run = result.payload["run"]
        self.assertEqual("awaiting_approval", run["run_status"], run)
        approval = {k: v for k, v in run["approval"].items() if k not in self.SCOPE_FREE}
        catalog = dict(model.calls[0].context["catalog"])
        catalog["scheduler"] = {k: v for k, v in catalog["scheduler"].items() if k != "project"}
        approved = app.approve(
            run["session"]["id"], run["task"]["id"], run["approval"]["request_id"],
            run["approval"]["fingerprint"],
        )
        self.assertEqual(EXIT_OK, approved.exit_code, approved.payload)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            grants = connection.execute("SELECT COUNT(*) FROM permission_grants").fetchone()[0]
        return {
            "exit": result.exit_code,
            "approval": approval,
            "catalog": catalog,
            "execution_allowed": app.core.execution_allowed(),
            "destination_policy": sorted(map(repr, app.core.destination_policy)),
            "executed": self.claude.execute_calls,
            "grants": grants,
            "project": self.memory.load_session(run["session"]["id"]).project_id,
        }

    def test_global_a_and_b_have_identical_authority(self) -> None:
        worlds = {owner: self.world(owner) for owner in (None, "proj-a", "proj-b")}
        self.assertEqual({None: None, "proj-a": "proj-a", "proj-b": "proj-b"},
                         {owner: world.pop("project") for owner, world in worlds.items()})
        self.assertIsNotNone(worlds[None]["approval"]["attestation"])
        self.assertEqual(worlds[None], worlds["proj-a"])
        self.assertEqual(worlds[None], worlds["proj-b"])
