"""V2.2 research b6 (SKSINGLEWRITER): Core is the single writer.

Pins, with fakes only (an isolated TEMP database, never data\\jarvis.db; no
live CLI, model, connector or network call):

1. no write-capable capability reachable through Core (file_write,
   project_folder_create) writes under the JARVIS data directory (the
   settings database's folder): every spelling of a target there - direct,
   ``..``, a junction, an 8.3 short name, a case variant, ``\\\\?\\``, ``\\\\.\\``
   and UNC - ends refused, nothing written, never a crash. Composition is
   the shipped one (``build_default_app``), started in a folder that holds
   the data directory, as a checkout does.
2. every CLI JARVIS launches (the claude_code connector and its intent and
   research adapters, its auth probe and model catalog probe; the codex
   exec, app-server and model-list calls) is started in a working directory
   outside the data directory: the launcher is mocked and its cwd read (an
   absent cwd is the JARVIS process's own). The Gmail gws CLI runs in the
   data directory by design: pinned as an expected failure (BLOCKED).
3. only jarvis/skill_store.py writes the skill* tables: a whole
   teach / use / correct / decline / reject / suppress / edit / rollback /
   disable / delete cycle through ``open_app`` with every SQL statement
   traced; each INSERT into a skill* table comes from a skill_store.py frame
   and no UPDATE or DELETE on a skill* table ever runs.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jarvis
from jarvis import codex, gmail_cli, intent, project_folders
from jarvis.app import _folder_protected, build_default_app
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.local_files import LocalFileWriteConnector
from jarvis.connectors.project_folder import PROJECT_FOLDER_CAPABILITY, PROJECT_FOLDER_CONTEXT_KEY
from jarvis.contracts import (
    LOCAL_WRITE_CAPABILITY,
    ActionScope,
    DataClassification,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    SessionState,
    Task,
)
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant
from jarvis.intent import ClaudeIntentAdapter, ClaudeResearchAdapter, claude_model_catalog
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, isolated_settings, open_app, safe_cleanup
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, patch_oauth_environment

JARVIS_DIR = Path(jarvis.__file__).resolve().parent
SKILL_STORE = JARVIS_DIR / "skill_store.py"
SKILL_TABLES = ("skills", "skill_events", "skill_drafts", "skill_suppressions", "skill_experiences")
TASK_TEXT = "Haftalik rapor dosyasini oku: notes/week.md"
# A statement's verb and target table, read from the statement's start only:
# a bound value (a skill body, an event payload) can never match.
WRITE = re.compile(
    r'(?:\s|--[^\n]*\n|/\*.*?\*/)*'  # leading comments
    r'(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+'
    r'(?:["`\[]?\w+["`\]]?\.)?["`\[]?(\w+)',  # an optional schema: main.skills
    re.IGNORECASE | re.DOTALL,
)


def task_json(objective: str = TASK_TEXT) -> str:
    return json.dumps({
        "objective": objective,
        "capability": DUMMY_CAPABILITY,
        "scope": {"action": "read", "target": "resource-a"},
    })


def writer_file() -> Path | None:
    """The innermost jarvis/ source frame on the stack: the code that ran
    the statement (sqlite3's own execute is C and has no frame)."""
    frame = sys._getframe(1)
    while frame is not None:
        path = Path(frame.f_code.co_filename).resolve()
        if path.is_relative_to(JARVIS_DIR):
            return path
        frame = frame.f_back
    return None


class DataDirectoryCase(unittest.TestCase):
    """A startup folder holding the data directory, the process started in
    it (the shipped layout: data\\ under the folder JARVIS starts in)."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.root = Path(temp.name).resolve()
        # A long name, so the volume gives it a distinct 8.3 alias.
        self.data = self.root / "jarvis-data-directory"
        self.data.mkdir()
        self.settings = Settings(
            memory_database=self.data / "jarvis.db", permission_mode=PermissionMode.SESSION
        )
        self.addCleanup(os.chdir, os.getcwd())  # runs before the temp cleanup
        os.chdir(self.root)

    def assertOutsideData(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        data = self.data.resolve()
        self.assertFalse(resolved == data or data in resolved.parents, f"{resolved} is in the data directory")


#: The file_write spellings that reach the data directory today (BLOCKED).
FILE_WRITE_OPEN_SPELLINGS = ("direct", "dotdot", "case")


def short_name(path: Path) -> str | None:
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    return buffer.value if buffer.value.lower() != str(path).lower() else None


@unittest.skipUnless(os.name == "nt", "Windows path spellings")
class DataDirectoryWriteRefusalTests(DataDirectoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self.build_app()
        self.core = self.app.core

    def build_app(self):
        return build_default_app(self.settings)

    def spellings(self, leaf: str) -> dict[str, str]:
        data = str(self.data)
        drive, rest = os.path.splitdrive(data)
        junction = self.root / "link"
        if not junction.exists():
            import _winapi

            _winapi.CreateJunction(data, str(junction))
        spelled = {
            "direct": os.path.join(data, leaf),
            "dotdot": os.path.join(str(self.root), "sub", "..", self.data.name, leaf),
            "case": os.path.join(str(self.root), self.data.name.upper(), leaf),
            "junction": os.path.join(str(junction), leaf),
            "extended": "\\\\?\\" + os.path.join(data, leaf),
            "device": "\\\\.\\" + os.path.join(data, leaf),
            "unc": "\\\\localhost\\" + drive.rstrip(":") + "$" + os.path.join(rest, leaf),
        }
        short = short_name(self.data)
        if short is not None:
            spelled["short"] = os.path.join(short, leaf)
        return spelled

    def finish(self, task: Task):
        self.before = self.listing()  # per case: one spelling never masks another
        run = self.core.run(task)
        if run.status is RunStatus.AWAITING_APPROVAL:  # the user approves: still refused
            run = self.core.resume(run.session.id, task.id, grant=PermissionGrant.for_request(run.permission_request))
        return run

    def assertRefusedAndUntouched(self, run) -> None:
        self.assertIs(RunStatus.FAILED, run.status, run.error)
        self.assertIsNotNone(run.error)
        self.assertEqual(self.before, self.listing())

    def listing(self) -> list[str]:
        return sorted(str(path) for path in self.data.rglob("*"))

    def file_write_refused(self, open_case: bool) -> None:
        for name in self.spellings("x"):
            if (name in FILE_WRITE_OPEN_SPELLINGS) is not open_case:
                continue
            target = self.spellings(f"planted-{name}.txt")[name]
            with self.subTest(spelling=name, target=target):
                task = Task(
                    objective="write the file",
                    capability=LOCAL_WRITE_CAPABILITY,
                    scope=ActionScope(action="write", target=target, data_classification=DataClassification.INTERNAL),
                )
                task.write_content, task.write_mode = "planted\n", "create"
                self.assertRefusedAndUntouched(self.finish(task))

    def test_file_write_under_the_data_directory_is_refused_through_a_junction_short_name_and_prefixes(self) -> None:
        self.file_write_refused(open_case=False)

    # BLOCKED (SKSINGLEWRITER): LocalFileWriteConnector has the guard
    # (protected_roots, proven wired below) but build_default_app does not pass
    # it, so with data\ under the startup folder these spellings are written
    # (NEEDS FILES: jarvis/app.py). The asserts are the real ones: once wired
    # this is an unexpected success - drop the decorator.
    @unittest.expectedFailure
    def test_file_write_under_the_data_directory_is_refused_direct_dotdot_and_case(self) -> None:
        self.file_write_refused(open_case=True)

    def folder_run(self, documents: str, label: str):
        """The shell's Documents folder is ``documents``; the task carries
        exactly JARVIS's own proposal (or, if the proposal itself refuses, the
        path it would have been), so a refusal is never a mere mismatch."""
        project = self.app.memory.create_project(f"Alpha {label}")
        with mock.patch.object(project_folders, "_known_path", return_value=documents):
            try:
                path = project_folders.propose_folder(project.name, False, protected=self.protected)
            except ValueError:  # refused at the proposal: Core must refuse it too
                path = os.path.join(documents, project_folders.DEFAULT_PARENT_NAME, project.name)
            task = Task(
                objective="Proje klasorunu kur",
                capability=PROJECT_FOLDER_CAPABILITY,
                scope=ActionScope(action="create", target="", data_classification=DataClassification.INTERNAL),
                context={PROJECT_FOLDER_CONTEXT_KEY: {"path": path, "location": "documents"}},
            )
            task.project_id = project.id
            return self.finish(task), path

    def test_project_folder_create_under_the_data_directory_is_refused_in_every_spelling(self) -> None:
        self.protected = _folder_protected(self.app.memory, self.settings)
        # Control: the same construction outside the data directory is created.
        (self.root / "Documents").mkdir()
        run, path = self.folder_run(str(self.root / "Documents"), "control")
        self.assertIs(RunStatus.COMPLETED, run.status, run.error)
        self.assertTrue(os.path.isdir(path))
        (self.data / "Documents").mkdir()
        for name, documents in self.spellings("Documents").items():
            with self.subTest(spelling=name, documents=documents):
                self.assertRefusedAndUntouched(self.folder_run(documents, name)[0])


@unittest.skipUnless(os.name == "nt", "Windows path spellings")
class WiredDataDirectoryWriteRefusalTests(DataDirectoryWriteRefusalTests):
    """The connector guard, wired as build_default_app must wire it
    (``protected_roots`` = the data directory): every spelling is refused.
    The unwired composition above stays pinned until jarvis/app.py passes it."""

    def build_app(self):
        wired = functools.partial(LocalFileWriteConnector, protected_roots=(self.data,))
        with mock.patch("jarvis.app.LocalFileWriteConnector", wired):
            return build_default_app(self.settings)

    def test_file_write_under_the_data_directory_is_refused_direct_dotdot_and_case(self) -> None:
        self.file_write_refused(open_case=True)

    test_project_folder_create_under_the_data_directory_is_refused_in_every_spelling = None


def launched_cwd(launcher: mock.Mock) -> str:
    """The cwd the launcher was given; none means the JARVIS process's own."""
    return launcher.call_args.kwargs.get("cwd") or os.getcwd()


class CliWorkingDirectoryTests(DataDirectoryCase):
    def setUp(self) -> None:
        super().setUp()
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)

    def claude_task(self, context: dict | None = None) -> Task:
        return Task(
            objective="Analyze without changing files",
            capability="code_planning",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.SENSITIVE,
            ),
            context=context or {},
        )

    def claude_launch(self, kind: type[ClaudeCodeConnector], context: dict | None = None, **wiring):
        """``build_default_app``'s construction; returns (popen mock, result)."""
        connector = kind(allowed_roots=(self.root,), default_working_directory=self.root, max_budget_usd=0.15, **wiring)
        with mock.patch.object(kind, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS), \
                mock.patch.object(kind, "_argv0", return_value="claude.exe"), \
                mock.patch("jarvis.connectors.claude_code.subprocess.Popen", side_effect=OSError("mocked")) as popen:
            result = None
            with contextlib.suppress(OSError):  # the mocked launch failing; only its cwd is read
                result = connector.execute(self.claude_task(context), SessionState())
        return popen, result

    def test_claude_code_connector_and_adapters_start_outside_the_data_directory(self) -> None:
        for kind in (ClaudeCodeConnector, ClaudeIntentAdapter, ClaudeResearchAdapter):
            with self.subTest(kind=kind.__name__):
                popen, _ = self.claude_launch(kind)
                self.assertTrue(popen.called)
                self.assertOutsideData(launched_cwd(popen))

    # BLOCKED (SKSINGLEWRITER): unwired (build_default_app's construction, no
    # protected_roots), _working_directory accepts a caller's
    # context["working_directory"] anywhere under its allowed root, data\
    # included (NEEDS FILES: jarvis/app.py). Real asserts: an unexpected
    # success once wired - drop the decorator then.
    @unittest.expectedFailure
    def test_a_caller_named_working_directory_in_the_data_directory_never_starts_there(self) -> None:
        self.caller_named_data_directory_refused()

    def caller_named_data_directory_refused(self, **wiring) -> None:
        requests = [str(self.data), os.path.join(str(self.root), "x", "..", self.data.name),
                    os.path.join(str(self.root), self.data.name.upper()), os.path.join(str(self.data), "sub")]
        (self.data / "sub").mkdir(exist_ok=True)
        if os.name == "nt" and short_name(self.data) is not None:
            alias = Path(short_name(self.data)).name
            # all-short, and a long root with the data folder's alias under it
            requests += [short_name(self.data), str(self.root / alias), str(self.root / alias / "sub")]
        for requested in requests:
            for kind in (ClaudeCodeConnector, ClaudeIntentAdapter, ClaudeResearchAdapter):
                with self.subTest(requested=requested, kind=kind.__name__):
                    popen, result = self.claude_launch(kind, {"working_directory": requested}, **wiring)
                    if popen.called:
                        self.assertOutsideData(launched_cwd(popen))
                    else:
                        self.assertIsNotNone(result)  # a crash is not a refusal
                        self.assertIsNotNone(result.error)  # refused before the launch

    def test_wired_a_caller_named_working_directory_in_the_data_directory_is_refused(self) -> None:
        """The connector guard, wired as build_default_app must wire it
        (``protected_roots`` = the data directory); the unwired construction
        above stays pinned until jarvis/app.py passes it."""
        self.caller_named_data_directory_refused(protected_roots=(self.data,))
        popen, _ = self.claude_launch(ClaudeCodeConnector, protected_roots=(self.data,))
        self.assertTrue(popen.called)  # control: the default root still launches
        self.assertOutsideData(launched_cwd(popen))

    # BLOCKED (SKSINGLEWRITER): by design (Settings.gmail_config_dir) every gws
    # call - login, status, reads, writes - runs in <data>\gmail-config\run.
    # A V2.1 confidential-transport design decision, not a lane fix. Real
    # assert: an unexpected success once moved - drop the decorator then.
    @unittest.expectedFailure
    def test_the_gmail_cli_starts_outside_the_data_directory(self) -> None:
        self.assertOutsideData(gmail_cli._run_directory(str(self.settings.gmail_config_dir)))

    def test_claude_auth_and_catalog_probes_start_outside_the_data_directory(self) -> None:
        connector = ClaudeCodeConnector(allowed_roots=(self.root,), default_working_directory=self.root)
        with mock.patch.object(ClaudeCodeConnector, "_argv0", return_value="claude.exe"), \
                mock.patch("jarvis.connectors.claude_code.subprocess.run", side_effect=OSError("mocked")) as run:
            connector._auth_status()
        self.assertTrue(run.called)
        self.assertOutsideData(launched_cwd(run))
        located = mock.Mock(argv0=mock.Mock(return_value="claude.exe"))
        with mock.patch.object(intent, "resolve_trusted_executable", return_value=located), \
                mock.patch.object(intent.subprocess, "Popen", side_effect=OSError("mocked")) as popen:
            self.assertEqual([], claude_model_catalog())
        self.assertTrue(popen.called)
        self.assertOutsideData(launched_cwd(popen))

    def test_codex_exec_app_server_and_model_list_start_outside_the_data_directory(self) -> None:
        with mock.patch.object(codex.subprocess, "Popen", side_effect=OSError("mocked")) as popen:
            with contextlib.suppress(OSError):
                codex._app_server("codex.exe", [], 0)
        self.assertOutsideData(launched_cwd(popen))
        with mock.patch.object(codex.subprocess, "run", side_effect=OSError("mocked")) as run:
            self.assertEqual([], codex._catalog("codex.exe"))
        self.assertOutsideData(launched_cwd(run))
        state = {"status": "available", "models": [{"id": "gpt-test", "efforts": []}], "quota": {}}
        adapter = codex.CodexIntentAdapter("system", "gpt-test", state=lambda: state)
        with mock.patch.object(codex, "codex_executable", return_value="codex.exe"), \
                mock.patch.object(codex.subprocess, "Popen", side_effect=OSError("mocked")) as popen:
            adapter.complete(self.claude_task(), SessionState())
        self.assertTrue(popen.called)
        self.assertOutsideData(launched_cwd(popen))
        argv = popen.call_args.args[0]
        self.assertOutsideData(argv[argv.index("-C") + 1])  # the CLI's own workspace root


class SkillTablesSingleWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.statements: list[tuple[str, str, Path | None]] = []
        self.unclear: list[str] = []
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(self.trace)
            return connection

        for patcher in (
            mock.patch.object(sqlite3, "connect", traced),
            mock.patch.dict(
                intent.NL_CAPABILITIES,
                {DUMMY_CAPABILITY: intent.CapabilityHint(None, "read", "echo")},
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = isolated_settings(
            self, skills_enabled=True, permission_mode=PermissionMode.SMART_AUTO
        )

    def trace(self, sql: str) -> None:
        match = WRITE.match(sql)
        if match is not None:
            verb = " ".join(match.group(1).upper().split())
            if match.group(2).lower().startswith("skill"):
                if re.search(r"\bDO\s+UPDATE\b", sql, re.IGNORECASE):
                    verb += " DO UPDATE"  # an upsert is an UPDATE
                self.statements.append((verb, match.group(2).lower(), writer_file()))
        elif sql.lstrip().upper().startswith("WITH") and "skill" in sql.lower():
            self.unclear.append(sql)  # a CTE write would hide its verb; fail closed

    def app(self, connector: DummyConnector):
        policy = {DestinationGrant(connector.manifest.name, DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION)}
        app = open_app(destination_policy=policy, settings=self.settings, connectors=(connector,))
        # create_app does not hand Core a skill store yet; attach the app's
        # own (same database), as the Core skill tests compose it.
        app.core.skill_store = app.skill_store
        return app

    def anchor(self, app, result) -> tuple[str, str]:
        """The user's task and its own session behind a skill-run chain."""
        session = app.core.memory.load_session(result.payload["session"]["id"])
        anchor_id = session.objective.removeprefix("skill-run:")
        return app.core.memory.task_session_id(anchor_id), anchor_id

    def test_a_full_skill_cycle_writes_skill_tables_only_through_skill_store(self) -> None:
        app = self.app(DummyConnector())
        # teach: a repeatable run leaves a pending draft; the user approves it.
        taught = app.run(task_json())
        self.assertEqual(0, taught.exit_code, taught.payload)
        [draft] = app.drafts_pending()["drafts"]
        approved = app.draft_approve(draft["draft_id"])
        self.assertTrue(approved["ok"], approved)
        skill_id = approved["skill_id"]
        # use: the next run is announced and runs the skill as a chain.
        used = app.run(task_json())
        self.assertEqual(0, used.exit_code, used.payload)
        owner, anchor_id = self.anchor(app, used)
        # correct: an avoid lesson and a version draft; the user rejects it.
        learned = app.core.correct_run(owner, anchor_id, "Once yedek dosyayi kontrol et")
        self.assertTrue(learned["experience_ids"], learned)
        [version_draft] = learned["draft_ids"]
        self.assertTrue(app.draft_reject(version_draft)["ok"])
        # decline: a skill run that stops at approval runs the task without it.
        gated = self.app(DummyConnector(always_requires_approval=True))
        paused = gated.run(task_json())
        self.assertEqual(10, paused.exit_code, paused.payload)
        owner, anchor_id = self.anchor(gated, paused)
        declined = gated.skill_decline(owner, anchor_id)
        self.assertIsNone(declined.payload["error"], declined.payload)
        self.assertEqual(10, declined.exit_code, declined.payload)  # the plain task, at its own approval
        # user clean-up: suppress, edit, rollback, disable, delete.
        self.assertTrue(app.suppress(None, "global")["ok"])
        form = app.skill_get(skill_id)["skill"]["edit_draft"]
        self.assertTrue(app.skill_edit(skill_id, dict(form, summary="Ikinci surum"))["ok"])
        self.assertTrue(app.skill_rollback(skill_id, 1)["ok"])
        self.assertTrue(app.skill_set_enabled(skill_id, False)["ok"])
        self.assertTrue(app.skill_delete(skill_id)["ok"])

        self.assertEqual([], self.unclear)
        # Not vacuous: every skill table was written during the cycle.
        self.assertEqual(set(SKILL_TABLES), {table for _, table, _ in self.statements})
        for verb, table, writer in self.statements:
            with self.subTest(verb=verb, table=table, writer=writer):
                # INSERT only: an UPDATE, DELETE or REPLACE rewrites history.
                self.assertTrue(verb.startswith("INSERT") and "REPLACE" not in verb and "UPDATE" not in verb, verb)
                self.assertEqual(SKILL_STORE, writer)


if __name__ == "__main__":
    unittest.main()
