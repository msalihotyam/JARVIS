"""V2 Projects Gate 6H H3 (K3): the project folder from a project's chat.

H3-1 catalog, H3-2 model input, H3-3 mandatory approval, H3-4 exact binding
(restart, Unicode), H3-5 changes before / after the final attestation, H3-6
replay, H3-7 refusals write nothing, H3-8 partial failures, H3-9 no leak,
H3-10 authority unchanged, H3-11 the card controls, H3-12 freeze; E4 the card
says "This computer".

Desktop and Documents are temp folders (the shell lookup is patched); fake
models only; no test opens the live DB or makes a live model, auth or network
call.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Q_ARG, QMetaObject, QObject

from jarvis import project_folders
from jarvis.app import (
    CommandResult,
    EXIT_POLICY,
    JarvisApp,
    _folder_protected,
    build_default_app,
    create_app,
    failure_result,
)
from jarvis.connectors import claude_code
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.project_folder import (
    PROJECT_FOLDER_CAPABILITY,
    PROJECT_FOLDER_CONNECTOR,
    PROJECT_FOLDER_CONTEXT_KEY,
    STALE_PROPOSAL,
    ProjectFolderCreateConnector,
)
from jarvis.contracts import (
    ActionScope,
    DataClassification,
    ErrorCode,
    PermissionGrant,
    PermissionMode,
    RunStatus,
    Task,
)
from jarvis.destinations import CLAUDE_CODE_DESTINATION, CONNECTOR_DESTINATION_POLICY
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from jarvis.ui.bridge import _here
from jarvis.ui.i18n import localization
from tests import test_g6h1b_ui as h1b
from tests import test_project_folders_6h as h6
from tests.helpers import installed_cli, settled
from tests.test_claude_connector import OAUTH_MAX_AUTH_STATUS, patch_oauth_environment
from tests.test_intent import ScriptedModel, proposal, task_proposal
from tests.test_ui import _APP, pump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

JP = project_folders.DEFAULT_PARENT_NAME
ASK = "bu proje için bir klasör kur"
#: A project name the user never types: its folder name is a DERIVED canary.
CANARY_NAME = "Zümrüt|Kanarya Qx7"
CANARY_LEAF = "Zümrüt-Kanarya"


def ids(payload: dict) -> tuple[str, str, str, str]:
    run = payload.get("run", payload)
    return run["session"]["id"], run["task"]["id"], run["approval"]["request_id"], run["approval"]["fingerprint"]


class K3Case(h6.CreateCase):
    """The real app (fake model) with the K3 connector over the test's store."""

    mode = PermissionMode.STRICT

    def setUp(self) -> None:
        super().setUp()
        self.compose()

    def compose(self, *, mode: PermissionMode | None = None, register: bool = True,
                extra: tuple = ()) -> JarvisApp:
        self.settings = Settings(memory_database=self.database, permission_mode=mode or self.mode)
        self.model = ScriptedModel()
        self.connector = ProjectFolderCreateConnector(self.memory, _folder_protected(self.memory, self.settings))
        self.k3 = create_app(settings=self.settings, memory=self.memory, registry=ToolRegistry(),
                             connectors=([self.connector] if register else []) + list(extra), intent_model=self.model)
        return self.k3

    def ask(self, project, target: str = "", *, text: str = ASK, conversation=None, **arguments):
        conversation = conversation or self.memory.create_conversation(project.id)
        self.model.answers.append(task_proposal(PROJECT_FOLDER_CAPABILITY, target=target, **arguments))
        payload = self.k3.submit_text(text, conversation_id=conversation.id).payload
        run = payload.get("run") or {}
        if isinstance(run.get("session"), dict):
            self.memory.link_session_conversation(run["session"]["id"], conversation.id)
        return payload, conversation

    def proposed(self, project, desktop: bool = False) -> str:
        return self.connector.proposal(project.id, "desktop" if desktop else "documents")["path"]

    def approve(self, payload: dict, app: JarvisApp | None = None, **kwargs) -> dict:
        return (app or self.k3).approve(*ids(payload), **kwargs).payload

    def count(self, table: str) -> int:
        return self.rows(f"SELECT COUNT(*) FROM {table}")[0][0]

    def everything(self):
        """H3-7: FULL links and projects contents and every listing."""
        return h6.tree(self.folders), self.snapshot()


# --------------------------------------------------------------------------
# H3-1 catalog
# --------------------------------------------------------------------------


class CatalogTests(K3Case):
    def offered(self, conversation_id=None, *, app=None) -> bool:
        app = app or self.k3
        before = len(self.model.calls)
        self.model.answers.append(proposal(kind="chat", reply="tamam"))
        app.submit_text("merhaba", conversation_id=conversation_id)
        if len(self.model.calls) == before:
            return False                                    # refused before the model: nothing offered
        names = [c["name"] for c in self.model.calls[-1].context["catalog"]["capabilities"]]
        return PROJECT_FOLDER_CAPABILITY in names

    def test_h3_1_only_an_active_folderless_project_chat_is_offered_it(self) -> None:
        own = self.memory.create_conversation(self.p.id)
        self.assertTrue(self.offered(own.id))
        self.assertFalse(self.offered(self.memory.create_conversation(None).id))       # Global
        self.assertFalse(self.offered(""))                                              # the window's unsaved Global
        self.assertFalse(self.offered(None))                                            # the CLI text verb
        linked = self.memory.create_conversation(self.q.id)
        self.assertEqual(0, self.app("link", self.q.id, path=self.folder("var")).exit_code)
        self.assertFalse(self.offered(linked.id))                                       # a linked project
        archived = self.memory.create_conversation(self.p.id)
        self.memory.set_project_active(self.p.id, False)
        self.assertFalse(self.offered(archived.id))                                     # archived: refused
        self.assertFalse(self.k3._folderless(self.p.id))
        self.memory.set_project_active(self.p.id, True)
        self.compose(register=False)
        self.assertFalse(self.offered(own.id))                                          # not registered

    def test_h3_1_build_catalog_has_exactly_one_caller(self) -> None:
        root = Path(__file__).resolve().parents[1] / "jarvis"
        calls = {str(p.relative_to(root)): p.read_text(encoding="utf-8").count("build_catalog(")
                 - p.read_text(encoding="utf-8").count("def build_catalog(") for p in root.rglob("*.py")}
        self.assertEqual({"app.py": 1}, {name: n for name, n in calls.items() if n})


# --------------------------------------------------------------------------
# H3-2 model input, H3-7 refusals write nothing
# --------------------------------------------------------------------------


class ModelInputTests(K3Case):
    def test_h3_2_only_empty_or_desktop_and_nothing_else_reaches_a_task(self) -> None:
        cases = {
            "path": {"target": str(self.desktop) + "\\x"},
            "relative": {"target": "..\\x"},
            "capital": {"target": "Desktop"},
            "turkish": {"target": "masaüstü"},
            "documents": {"target": "documents"},
            "read window": {"target": "", "start_line": 1},
            "extra field": {"target": "", "path": "C:\\x"},
            "not text": {"target": 5},
        }
        for name, arguments in cases.items():
            with self.subTest(name), \
                    mock.patch("jarvis.app.decode_task_json", side_effect=AssertionError("decoded")), \
                    mock.patch.object(self.k3.core, "run", side_effect=AssertionError("run")):
                before = self.everything()
                payload, _ = self.ask(self.p, **arguments)
                self.assertEqual("malformed_model_output", payload["intent"]["status"], payload)
                self.assertEqual(before, self.everything())                             # H3-7
                self.assertEqual((0, 0), (self.count("tasks"), self.count("pending_approvals")))


# --------------------------------------------------------------------------
# H3-3 mandatory approval
# --------------------------------------------------------------------------


class MandatoryApprovalTests(K3Case):
    def test_h3_3_every_mode_asks_and_no_remembered_grant_covers_it(self) -> None:
        for mode in (PermissionMode.STRICT, PermissionMode.SESSION, PermissionMode.SMART_AUTO):
            with self.subTest(mode=mode.value):
                self.compose(mode=mode)
                project = self.memory.create_project(f"Proje {mode.value}")
                before = self.everything()
                payload, _ = self.ask(project)
                self.assertEqual("awaiting_approval", payload["run"]["run_status"])
                request = self.memory.load_pending_approval(payload["run"]["task"]["id"], payload["run"]["session"]["id"])
                self.assertTrue(request.side_effect.mandatory)
                self.assertFalse(self.k3.core._remembered_permission_allowed(request))
                refused = self.approve(payload, remember_for_session=True)
                self.assertEqual("invalid_grant", refused["error"]["code"])
                self.assertIsNotNone(self.memory.load_pending_approval(request.task_id, request.session_id))
                self.assertEqual(before[0], self.everything()[0])                       # nothing created
                self.assertIsNone(self.memory.load_project_folder(project.id))


# --------------------------------------------------------------------------
# H3-4 exact binding, restart, Unicode
# --------------------------------------------------------------------------


class ExactBindingTests(K3Case):
    def test_h3_4_the_shown_path_is_created_linked_and_survives_a_restart(self) -> None:
        project = self.memory.create_project("Masaüstü ışığı ğ")
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                if desktop:
                    project = self.memory.create_project("Masaüstü ışığı ğ 2")
                path = self.proposed(project, desktop)
                payload, _ = self.ask(project, "desktop" if desktop else "")
                session_id, task_id, request_id, fingerprint = ids(payload)
                task = self.memory.load_task(task_id)
                self.assertEqual({"path": path, "location": "desktop" if desktop else "documents"},
                                 task.context[PROJECT_FOLDER_CONTEXT_KEY])
                frame = self.connector.canonical_outbound(task)
                header = frame.split(b"\n", 1)[1].decode("ascii")                      # lossless \u escapes
                self.assertEqual({"project_id": project.id, "path": path, "location": "desktop" if desktop else "documents"},
                                 json.loads(header))
                # A restart: a new store, connector and app over the same database.
                memory = SQLiteMemoryStore(self.database)
                again = create_app(settings=self.settings, memory=memory, registry=ToolRegistry(),
                                   connectors=[ProjectFolderCreateConnector(memory, _folder_protected(memory, self.settings))])
                request = memory.load_pending_approval(task_id, session_id)
                self.assertEqual((request_id, fingerprint), (request.id, request.fingerprint))
                self.assertEqual(path, memory.load_task(task_id).context[PROJECT_FOLDER_CONTEXT_KEY]["path"])
                before = h6.dirs(self.folders)
                done = self.approve(payload, again)
                self.assertEqual("completed", done["run_status"], done)
                link = memory.load_project_folder(project.id)
                self.assertEqual((path, "created"), (link.path, link.origin))
                self.assertEqual({path} | ({os.path.dirname(path)} - before), h6.dirs(self.folders) - before)


# --------------------------------------------------------------------------
# H3-5 changes, H3-6 replay
# --------------------------------------------------------------------------


class ChangeTests(K3Case):
    def pending(self):
        payload, _ = self.ask(self.p)
        return payload, self.proposed(self.p)

    def test_h3_5_a_change_before_the_final_attestation_ends_the_task(self) -> None:
        other = self.folder("elsewhere")
        changes = {
            "sibling takes the path": lambda path: os.makedirs(path),
            "project renamed": lambda path: self.memory.rename_project(self.p.id, "Gamma"),
            "a link added": lambda path: self.app("link", self.p.id, path=other),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.p = self.memory.create_project(f"Alpha {len(name)}")
                payload, path = self.pending()
                change(path)
                before = self.everything()
                with self.removes_nothing():
                    done = self.approve(payload)
                self.assertEqual(("failed", STALE_PROPOSAL), (done["run_status"], done["error"]["message"]))
                self.assertEqual("failed", self.memory.load_task(payload["run"]["task"]["id"]).status.value)
                self.assertIsNone(done.get("approval"))                                 # no replacement card
                left = self.memory.load_pending_approval(*reversed(ids(payload)[:2]))
                self.assertIn(getattr(left, "id", None), (None, ids(payload)[2]))       # no NEW request
                self.assertEqual(before, self.everything())                             # H3-7

    def test_h3_5_archived_before_the_final_attestation_is_held_by_core(self) -> None:
        # Accepted as Core designs it (Salih 2026-10-10, "6H3 A"): Core's
        # project gate refuses BEFORE the connector is asked; nothing is
        # created, no card replaces it and the task is held, not failed.
        payload, path = self.pending()
        self.memory.set_project_active(self.p.id, False)
        before = self.everything()
        done = self.approve(payload)
        self.assertEqual(("invalid_task", None), (done["error"]["code"], done.get("approval")))
        self.assertIn("inactive project", done["error"]["message"])
        self.assertEqual(before, self.everything())
        self.assertFalse(os.path.exists(path))

    def test_h3_5_after_an_unarchive_the_old_card_creates_only_its_reattested_path(self) -> None:
        # "6H3 A": unarchived within the approval TTL, the old card may create
        # the folder - exactly the path it showed, after the connector's own
        # re-attestation; a proposal that changed meanwhile still ends the task.
        payload, path = self.pending()
        task_id = payload["run"]["task"]["id"]
        self.memory.set_project_active(self.p.id, False)
        self.assertEqual("invalid_task", self.approve(payload)["error"]["code"])
        self.assertEqual("awaiting_approval", self.memory.load_task(task_id).status.value)   # held
        self.memory.set_project_active(self.p.id, True)
        with self.removes_nothing():
            done = self.approve(payload)
        self.assertEqual("completed", done["run_status"], done)
        link = self.memory.load_project_folder(self.p.id)
        self.assertEqual((path, "created"), (link.path, link.origin))
        changes = {
            "sibling takes the path": lambda path: os.makedirs(path),
            "project renamed": lambda path: self.memory.rename_project(self.p.id, f"Gamma {self.p.id}"),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.p = self.memory.create_project(f"Alpha {len(name)}")
                payload, path = self.pending()
                self.memory.set_project_active(self.p.id, False)
                self.assertEqual("invalid_task", self.approve(payload)["error"]["code"])
                change(path)
                self.memory.set_project_active(self.p.id, True)
                before = self.everything()
                with self.removes_nothing():
                    done = self.approve(payload)
                self.assertEqual(("failed", STALE_PROPOSAL), (done["run_status"], done["error"]["message"]))
                self.assertEqual("failed", self.memory.load_task(payload["run"]["task"]["id"]).status.value)
                self.assertIsNone(done.get("approval"))                                 # no replacement card
                self.assertIsNone(self.memory.load_project_folder(self.p.id))
                self.assertEqual(before, self.everything())

    def late(self, change) -> tuple[dict, str]:
        payload, path = self.pending()
        real = self.connector.execute

        def hooked(task, session):
            change(path)
            return real(task, session)

        with mock.patch.object(self.connector, "execute", side_effect=hooked), self.removes_nothing():
            return self.approve(payload), path

    def test_h3_5_after_the_final_attestation_only_the_transaction_decides(self) -> None:
        done, path = self.late(lambda path: os.makedirs(path))
        self.assertEqual("failed", done["run_status"])
        self.assertIn("hiçbir klasör oluşturulmadı", done["error"]["message"])
        self.assertIsNone(self.memory.load_project_folder(self.p.id))
        other = self.folder("elsewhere")
        for name, change in (("link", lambda path: self.app("link", self.p.id, path=other)),
                             ("archive", lambda path: self.memory.set_project_active(self.p.id, False))):
            with self.subTest(name):
                self.p = self.memory.create_project(f"Alpha {name}")
                done, path = self.late(change)
                self.assertEqual("failed", done["run_status"])
                self.assertFalse(os.path.exists(path))
                link = self.memory.load_project_folder(self.p.id)
                self.assertTrue(link is None or link.origin == "picked")
        self.p = self.memory.create_project("Alpha rename")
        done, path = self.late(lambda path: self.memory.rename_project(self.p.id, "Delta"))
        self.assertEqual("completed", done["run_status"], done)
        self.assertEqual((path, "created"), (self.memory.load_project_folder(self.p.id).path,
                                             self.memory.load_project_folder(self.p.id).origin))
        self.assertTrue(path.endswith("\\Alpha rename"))

    def test_h3_6_nothing_replays(self) -> None:
        # After an E7 switch, the old request.
        payload, path = self.pending()
        switched = self.k3.project_folder_switch(*ids(payload)).payload
        self.assertEqual("awaiting_approval", switched["run_status"], switched)
        self.assertEqual("task_not_found", self.approve(payload)["error"]["code"])      # no longer pending
        self.assertFalse(os.path.exists(path))
        # Twice after success, and again after a restart.
        self.p = self.memory.create_project("Alpha 2")
        payload, path = self.pending()
        self.assertEqual("completed", self.approve(payload)["run_status"])
        before = self.everything()
        self.assertEqual("task_not_found", self.approve(payload)["error"]["code"])
        memory = SQLiteMemoryStore(self.database)
        again = create_app(settings=self.settings, memory=memory, registry=ToolRegistry(),
                           connectors=[ProjectFolderCreateConnector(memory, ())])
        self.assertEqual("task_not_found", self.approve(payload, again)["error"]["code"])
        self.assertEqual(before, self.everything())
        # The original request once its fingerprint is current again.
        self.p = self.memory.create_project("Alpha 3")
        payload, path = self.pending()
        os.makedirs(path)
        self.assertEqual(STALE_PROPOSAL, self.approve(payload)["error"]["message"])
        os.rmdir(path)                                                                   # the test's own folder
        self.assertEqual(path, self.proposed(self.p))                                    # current again
        before = self.everything()
        self.assertNotEqual("completed", self.approve(payload)["run_status"])
        self.assertEqual(before, self.everything())
        self.assertFalse(os.path.exists(path))


# --------------------------------------------------------------------------
# H3-8 partial failures
# --------------------------------------------------------------------------


class PartialFailureTests(K3Case):
    def run_failing(self, *patches):
        payload, _ = self.ask(self.p)
        path = self.proposed(self.p)
        with contextlib.ExitStack() as stack:
            for target, name, value in patches:
                stack.enter_context(mock.patch.object(target, name, **value))
            stack.enter_context(self.removes_nothing())
            done = self.approve(payload)
        self.assertEqual("failed", done["run_status"])
        message = done["error"]["message"]
        for secret in (path, "Alpha", str(self.documents)):
            self.assertNotIn(secret, json.dumps(done, ensure_ascii=False))
        return path, message

    def test_h3_8_only_the_shared_parent(self) -> None:
        real = project_folders._create_child

        def child(parent, leaf):
            if leaf != JP:
                raise OSError(f"no {self.documents}")
            return real(parent, leaf)

        path, message = self.run_failing((project_folders, "_create_child", {"side_effect": child}))
        self.assertIn('Yalnızca ortak "JARVIS Projeleri" klasörü oluşturuldu', message)
        self.assertIn("bağlı değil", message)
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        self.assertFalse(os.path.exists(path))

    def test_h3_8_the_folder_created_but_not_linked(self) -> None:
        h6.CreatePartialFailureTests.insert_fails(self)
        path, message = self.run_failing()
        self.assertIn("Proje klasörü oluşturuldu ama projeye bağlanamadı", message)
        self.assertIn("yolu onay kartındaydı", message)
        self.assertIn("Proje bağlantısı: bağlı değil", message)
        self.assertTrue(os.path.isdir(path))                                             # not deleted
        self.assertEqual([], self.rows("SELECT * FROM project_folder_links"))

    def test_h3_8_the_link_state_is_read_back(self) -> None:
        real = self.memory.link_explicit_user_project_folder

        def then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("after the commit")

        _, message = self.run_failing((self.memory, "link_explicit_user_project_folder", {"side_effect": then_fail}))
        self.assertIn("Proje bağlantısı: bağlı.", message)
        self.p = self.memory.create_project("Alpha 2")
        h6.CreatePartialFailureTests.insert_fails(self)
        _, message = self.run_failing((project_folders, "_read_back", {"return_value": "unknown"}))
        self.assertIn("Proje bağlantısı: bilinmiyor", message)


# --------------------------------------------------------------------------
# H3-9 no leak
# --------------------------------------------------------------------------


def assert_absent(case: unittest.TestCase, sinks: dict, secrets) -> None:
    """No secret in any sink: as written, and escaped once or twice (a JSON
    column inside the rendered JSON), both ways, ignoring case."""
    for name, sink in sinks.items():
        rendered = json.dumps(sink, ensure_ascii=False, default=str)
        for secret in secrets:
            forms = {secret}
            for _ in range(2):
                forms |= {json.dumps(f, ensure_ascii=ascii)[1:-1] for f in forms for ascii in (False, True)}
            for form in forms:
                with case.subTest(sink=name, secret=form):
                    case.assertNotIn(form.casefold(), rendered.casefold())
            # The detector itself: a planted path in a JSON column is found.
            planted = json.dumps([[json.dumps({"p": secret})]], ensure_ascii=False)
            case.assertTrue(any(form.casefold() in planted.casefold() for form in forms))


class _Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(self.format(record))


@unittest.skipUnless(h6.WINDOWS, "Windows path rules")
class NoLeakTests(K3Case):
    def setUp(self) -> None:
        super().setUp()
        self.startup = Path(self.folder("startup-root"))
        self.claude = ClaudeCodeConnector(allowed_roots=(self.startup,), default_working_directory=self.startup,
                                          max_budget_usd=0.15)
        self.compose(extra=(self.claude,))
        environment = patch_oauth_environment()
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(ClaudeCodeConnector, "_auth_status", return_value=OAUTH_MAX_AUTH_STATUS),
            mock.patch.object(claude_code, "resolve_trusted_executable", return_value=installed_cli(self)),
            mock.patch.object(claude_code.subprocess, "Popen", side_effect=AssertionError("Popen")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = _Captured()
        self.log.setFormatter(logging.Formatter("%(name)s %(message)s"))
        root = logging.getLogger()
        level = root.level
        root.addHandler(self.log)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, self.log)
        self.serial = 0

    def canary(self):
        self.serial += 1
        return self.memory.create_project(f"{CANARY_NAME} {self.serial}")

    def oserror(self, path: str):
        return {"side_effect": OSError(f"cannot use {path}")}

    def flows(self) -> str:
        """Every flow once; returns the session of the completed one."""
        project = self.canary()
        payload, conversation = self.ask(project)                                        # complete
        self.assertEqual("completed", self.approve(payload)["run_status"])
        session = payload["run"]["session"]["id"]
        self.ask(self.canary(), target=str(self.documents))                              # refused (E1)
        project = self.canary()
        payload, _ = self.ask(project)                                                   # stale
        os.makedirs(self.proposed(project))
        self.approve(payload)
        payload, _ = self.ask(self.canary())                                             # rejected
        self.k3.reject(*ids(payload))
        h6.CreatePartialFailureTests.insert_fails(self)                                  # partial
        payload, _ = self.ask(self.canary())
        self.approve(payload)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TRIGGER fail_link")
            connection.commit()
        # An OSError carrying the path, in the proposal of both app entries ...
        project = self.canary()
        path = self.proposed(project)
        with mock.patch.object(project_folders, "propose_folder", **self.oserror(path)):
            refused, _ = self.ask(project)
        self.assertEqual("unsupported_capability", refused["intent"]["status"])
        payload, _ = self.ask(project)
        real = project_folders.propose_folder

        def desktop_fails(name, on_desktop, **kwargs):
            if on_desktop:                                    # the switch's new proposal only
                raise OSError(f"x {real(name, on_desktop, **kwargs)}")
            return real(name, on_desktop, **kwargs)

        with mock.patch.object(project_folders, "propose_folder", side_effect=desktop_fails):
            switched = self.k3.project_folder_switch(*ids(payload)).payload
        self.assertEqual(("rejected", "JARVIS could not propose a folder for this project. Nothing was created."),
                         (switched["run_status"], switched["notice"]))
        # ... and in every connector method's own work.
        isdir = os.path.isdir

        def isdir_fails(path: str):
            def check(candidate):
                if candidate == path:                         # verify's own read-back of the created folder
                    raise OSError(candidate)
                return isdir(candidate)
            return {"side_effect": check}

        for target, name in ((project_folders, "check_lexical"), (project_folders, "create_and_link"),
                             (ProjectFolderCreateConnector, "_bound"), (os.path, "isdir")):
            project = self.canary()
            path = self.proposed(project)
            payload, _ = self.ask(project)
            value = isdir_fails(path) if name == "isdir" else self.oserror(path)
            with mock.patch.object(target, name, **value):
                self.assertNotEqual("completed", self.approve(payload)["run_status"])
        return session

    def test_h3_9_no_derived_path_or_folder_name_reaches_a_forbidden_sink(self) -> None:
        session = self.flows()
        # A Claude task in the completed flow's session (its decision is retrieved).
        task = Task(objective="Summarize the module layout", capability="code_planning",
                    scope=ActionScope(action="analyze", target=str(self.startup), destination=CLAUDE_CODE_DESTINATION,
                                      data_classification=DataClassification.INTERNAL),
                    context={"working_directory": str(self.startup)})
        waiting = self.k3.core.run(task, session)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status, waiting.error)
        claude = {"preview": waiting.permission_request.outbound_preview.to_dict(),
                  "payload": self.claude.canonical_outbound(self.memory.load_task(task.id)).decode("utf-8")}
        # The next understanding call.
        self.model.answers.append(proposal(kind="chat", reply="tamam"))
        self.k3.submit_text("merhaba", conversation_id=self.memory.create_conversation(self.p.id).id)
        intents = [{"objective": t.objective, "context": t.context, "instruction": t.project_instruction}
                   for t in self.model.calls]
        sinks = {
            "events": self.rows("SELECT * FROM event_log"),
            "checkpoints": self.rows("SELECT * FROM loop_checkpoints"),
            "decisions": self.rows("SELECT * FROM decision_log"),
            "approvals": self.rows("SELECT * FROM pending_approvals"),
            "intent payloads": intents,
            "claude": claude,
            "logs": self.log.text,
        }
        self.assertGreater(len(sinks["decisions"]), 0)
        assert_absent(self, sinks, (CANARY_LEAF, str(self.documents) + "\\" + JP, str(self.desktop) + "\\Z"))
        # The derived paths ARE in the tasks (connector-private context): the canary is real.
        self.assertIn(CANARY_LEAF, json.dumps(self.rows("SELECT state_json FROM tasks"), ensure_ascii=False))

    def test_h3_9_what_the_user_typed_is_kept_as_typed(self) -> None:
        typed = f"{CANARY_LEAF} adında bir klasör kur, {self.documents}\\{JP} altında"
        self.ask(self.canary(), text=typed)
        self.assertEqual(typed, self.model.calls[-1].objective)


# --------------------------------------------------------------------------
# H3-10 authority unchanged
# --------------------------------------------------------------------------


class CompositionTests(h6.FolderCase):
    def test_h3_10_one_store_and_no_other_connector_changes(self) -> None:
        settings = Settings(memory_database=self.database)
        app = build_default_app(settings)
        connector = app.core.registry.get(PROJECT_FOLDER_CONNECTOR)
        self.assertIsInstance(connector, ProjectFolderCreateConnector)
        self.assertIs(app.memory, connector.store)
        self.assertIs(app.memory, app.core.memory)
        self.assertEqual(_folder_protected(app.memory, settings), connector.protected)
        startup = Path.cwd().resolve()
        others = [app.core.registry.get(name) for name in app.core.registry.list_names() if name != PROJECT_FOLDER_CONNECTOR]
        self.assertEqual({"claude-code-plan", "local-file-read", "local-file-write"}, {c.manifest.name for c in others})
        for other in others + [app.intent_model, app.research_model]:
            with self.subTest(other=type(other).__name__):
                self.assertEqual(((startup,), startup), (tuple(other.allowed_roots), other.default_working_directory))
        task = Task(objective="o", capability=PROJECT_FOLDER_CAPABILITY, scope=ActionScope(action="create"))
        self.assertEqual(("", ()), connector.resource_boundary(task))
        self.assertIn(("project-folder-create", "project_folder_create", CLAUDE_CODE_DESTINATION),
                      {(g.connector, g.capability, g.destination) for g in CONNECTOR_DESTINATION_POLICY})


class K3LinkOrigin:
    """H1 A1-A3 again, with q's folder created and linked through K3. The K3
    task itself leaves one path-free decision record in its project (Core's
    finalization), which later Claude payloads of that project retrieve; so p
    gets the same K3 history first, its link removed, and only the LINK differs."""

    origin = "created"

    def setUp(self) -> None:
        super().setUp()
        self.k3_link(self.p.id)
        self.memory.unlink_project_folder(self.p.id)

    def link_q(self, folder: Path) -> None:
        self.k3_link(self.q.id)
        self.assertIsNone(self.memory.load_project_folder(self.p.id))

    def k3_link(self, project_id: str) -> None:
        known = {project_folders._KNOWN_FOLDERS["desktop"]: str(self.folder("k3-desktop")),
                 project_folders._KNOWN_FOLDERS["documents"]: str(self.folder("k3-documents"))}
        with mock.patch.object(project_folders, "_shell_known_folder", side_effect=known.get):
            connector = ProjectFolderCreateConnector(self.memory, (self.database.parent,))
            core = create_app(settings=self.settings, memory=self.memory, registry=ToolRegistry(),
                              connectors=[connector]).core
            task = Task(objective="klasör", capability=PROJECT_FOLDER_CAPABILITY,
                        scope=ActionScope(action="create", destination=CLAUDE_CODE_DESTINATION),
                        context={PROJECT_FOLDER_CONTEXT_KEY: connector.proposal(project_id, "documents")})
            task.project_id = project_id
            waiting = core.run(task)
            done = core.resume(waiting.session.id, task.id, PermissionGrant.for_request(waiting.permission_request))
        self.assertEqual(RunStatus.COMPLETED, done.status, done.error)
        self.assertEqual("created", self.memory.load_project_folder(project_id).origin)


class K3LocalFileAuthorityTests(K3LinkOrigin, h6.LocalFileAuthorityTests):
    """H3-10: A1 with a K3-created link."""


class K3ClaudeAuthorityTests(K3LinkOrigin, h6.ClaudeAuthorityTests):
    """H3-10: A2/A3 with a K3-created link."""


# --------------------------------------------------------------------------
# H3-12 freeze (the app entries)
# --------------------------------------------------------------------------


class FreezeTests(K3Case):
    def test_h3_12_frozen_nothing_is_created_and_the_switch_refuses(self) -> None:
        payload, _ = self.ask(self.p)
        self.k3.core.execution_gate = lambda: False
        before = self.everything()
        frozen, _ = self.ask(self.q)
        self.assertEqual("execution_disabled", frozen["intent"]["status"])
        switched = self.k3.project_folder_switch(*ids(payload)).payload
        self.assertEqual("execution_disabled", switched["error"]["code"])
        self.assertIsNotNone(self.memory.load_pending_approval(payload["run"]["task"]["id"], payload["run"]["session"]["id"]))
        self.assertEqual(1, self.count("tasks"))
        self.assertEqual(before, self.everything())


class SwitchEntryTests(K3Case):
    def test_h3_11_the_entry_refuses_foreign_and_unowned_requests(self) -> None:
        payload, _ = self.ask(self.p)
        session_id, task_id, request_id, fingerprint = ids(payload)
        before = self.everything()
        for name, args in (("other id", (session_id, task_id, "x", fingerprint)),
                           ("other fingerprint", (session_id, task_id, request_id, "0" * 64)),
                           ("other session", ("nope", task_id, request_id, fingerprint))):
            with self.subTest(name):
                self.assertEqual("invalid_grant", self.k3.project_folder_switch(*args).payload["error"]["code"])
        # A session whose conversation another project owns is never switched.
        other = self.memory.create_conversation(self.q.id)
        bare, _ = self.ask(self.q, conversation=other)
        with mock.patch.object(self.memory, "conversation_for_session", return_value=other.id):
            self.assertEqual("invalid_grant", self.k3.project_folder_switch(*ids(payload)).payload["error"]["code"])
        self.assertIsNotNone(self.memory.load_pending_approval(task_id, session_id))
        self.assertEqual(before[0], self.everything()[0])
        self.assertEqual("awaiting_approval", bare["run"]["run_status"])

    def test_h3_11_a_failed_rejection_issues_nothing(self) -> None:
        payload, _ = self.ask(self.p)
        failed = CommandResult(failure_result("reject", self.settings.permission_mode, ErrorCode.STALE_GRANT,
                                              "expired", EXIT_POLICY).payload, EXIT_POLICY)
        with mock.patch.object(JarvisApp, "reject", return_value=failed):
            self.assertEqual("stale_grant", self.k3.project_folder_switch(*ids(payload)).payload["error"]["code"])
        self.assertEqual(1, self.count("tasks"))

    def test_h3_11_the_switch_rejects_then_issues_one_request_without_a_model_call(self) -> None:
        payload, _ = self.ask(self.p)
        calls = len(self.model.calls)
        switched = self.k3.project_folder_switch(*ids(payload)).payload
        self.assertEqual("rejected", self.memory.load_task(payload["run"]["task"]["id"]).status.value)
        self.assertEqual("awaiting_approval", switched["run_status"])
        new = self.memory.load_task(switched["task"]["id"])
        self.assertEqual(({"path": self.proposed(self.p, True), "location": "desktop"}, self.p.id, "desktop"),
                         (new.context[PROJECT_FOLDER_CONTEXT_KEY], new.project_id, new.scope.target))
        self.assertEqual((calls, 2), (len(self.model.calls), self.count("tasks")))
        self.assertEqual(0, self.rows("SELECT COUNT(*) FROM permission_grants WHERE approved = 1")[0][0])


# --------------------------------------------------------------------------
# The window: E4, H3-4 through the bridge, H3-11 the card controls
# --------------------------------------------------------------------------


@unittest.skipUnless(h6.WINDOWS, "Windows folder creation")
class CardTests(h1b.Case):
    def factory(self, settings):
        memory = SQLiteMemoryStore(settings.memory_database)
        self.k3_connector = ProjectFolderCreateConnector(memory, _folder_protected(memory, settings))
        return create_app(settings=settings, memory=memory, registry=ToolRegistry(),
                          connectors=[self.claude, self.reader, self.k3_connector],
                          destination_policy=CONNECTOR_DESTINATION_POLICY, intent_model=self.model)

    def pending(self, target: str = ""):
        bridge = self.bridge(task_proposal(PROJECT_FOLDER_CAPABILITY, target=target))
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        bridge.submitText(ASK)
        self.assertTrue(bridge.approvalPending)
        return bridge

    def path(self, desktop: bool = False) -> str:
        return str((self.desktop if desktop else self.documents / JP) / self.project.name)

    def test_e4_the_card_says_this_computer_and_shows_the_exact_path(self) -> None:
        bridge = self.pending()
        for card in (bridge.approval, bridge.safety["approval"]):
            self.assertEqual(("This computer", self.path(), "documents", self.project.id),
                             (card["destination"], card["target"], card["folder_location"], card["folder_project"]))
            self.assertNotIn(CLAUDE_CODE_DESTINATION, json.dumps(card, ensure_ascii=False))
        self.assertTrue(bridge.safety["approval"]["actionable"])
        localization(_APP).apply("tr-TR")
        self.addCleanup(localization(_APP).apply, "en-US")
        self.assertEqual("Bu bilgisayar", bridge.approval["destination"])
        # The request itself is unchanged: the grant row's destination, no path.
        ticket = bridge._ticket
        request = self.db().load_pending_approval(ticket.task_id, ticket.session_id)
        self.assertEqual(CLAUDE_CODE_DESTINATION, request.scope.destination)
        self.assertNotIn(self.project.name, json.dumps(request.to_dict(), ensure_ascii=False))

    def test_e4_without_its_shown_path_a_k3_card_cannot_be_approved(self) -> None:
        bridge = self.pending()
        shown = bridge._ticket
        bridge._k3_folder = {}
        with mock.patch.object(bridge.backend, "project_folder_request", return_value={}):
            self.assertEqual("", bridge.approval["target"])
            self.assertFalse(bridge.safety["approval"]["actionable"])
            self.assertFalse(bridge.approveRequest(shown.request_id, shown.fingerprint))
            bridge.approve()
        self.assertTrue(bridge.approvalPending)
        self.assertEqual([], self.made())
        self.assertEqual(self.path(), bridge.approval["target"])                         # a failed read is retried

    def test_h3_4_approving_the_card_creates_exactly_its_path(self) -> None:
        bridge = self.pending()
        shown = bridge.approval
        self.assertTrue(bridge.approveRequest(shown["request_id"], shown["fingerprint"]))
        link = self.db().load_project_folder(self.project.id)
        self.assertEqual((shown["target"], "created"), (link.path, link.origin))
        self.assertEqual([f"Documents\\{JP}", f"Documents\\{JP}\\{self.project.name}"], self.made())
        self.assertFalse(bridge.approvalPending)

    def test_h3_11_the_switch_once_never_approves_and_targets_the_requests_project(self) -> None:
        bridge = self.pending()
        old = bridge._ticket
        calls = len(self.model.calls)
        bridge.showProject(self.other.id)                                               # moved elsewhere
        self.assertFalse(bridge.switchProjectFolderLocation(old.request_id, "0" * 64))
        self.assertTrue(bridge.switchProjectFolderLocation(old.request_id, old.fingerprint))
        self.assertFalse(bridge.switchProjectFolderLocation(old.request_id, old.fingerprint))   # once
        store = self.db()
        self.assertEqual("rejected", store.load_task(old.task_id).status.value)
        new = bridge.approval
        self.assertNotEqual(old.request_id, new["request_id"])
        self.assertEqual((self.path(True), "desktop", self.project.id),
                         (new["target"], new["folder_location"], new["folder_project"]))
        self.assertEqual(calls, len(self.model.calls))                                  # no model call
        self.assertEqual([], self.made())

    def test_h3_11_refusals_issue_nothing(self) -> None:
        bridge = self.pending()
        old = bridge._ticket
        failed = CommandResult(failure_result("reject", bridge.backend.store.load().settings.permission_mode,
                                              ErrorCode.STALE_GRANT, "expired", EXIT_POLICY).payload, EXIT_POLICY)
        with mock.patch.object(JarvisApp, "reject", return_value=failed):
            self.assertTrue(bridge.switchProjectFolderLocation(old.request_id, old.fingerprint))
        self.assertEqual((old.request_id, 1), (bridge.approval["request_id"], self.rows_count()))   # card kept
        self.assertFalse(bridge.switchProjectFolderLocation(old.request_id, old.fingerprint))  # its one transition
        # Frozen: the control is off.
        other = self.pending()
        self.store.mutate(lambda s: replace(s, execution_enabled=False))
        other.refreshConfig()
        ticket = other._ticket
        self.assertFalse(other.switchProjectFolderLocation(ticket.request_id, ticket.fingerprint))

    def rows_count(self) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def card(self, bridge):
        bridge.setTheme("command")
        root = self.window(bridge)
        cards = [c for c in self.all_items(root, "approvalCard") if c.isVisible()]
        self.assertTrue(cards)
        return root, cards[0]

    def test_h3_11_choose_an_existing_folder_links_first_then_rejects(self) -> None:
        bridge = self.pending()
        old = bridge._ticket
        root, card = self.card(bridge)
        self.assertTrue(self.under(card, "k3FolderControls")[0].isVisible())
        picker = card.findChild(QObject, "k3FolderPicker")
        bridge.showProject(self.other.id)                                               # bound at the click
        QMetaObject.invokeMethod(card, "bindPicker")
        self.assertEqual((self.project.id, old.request_id), (picker.property("projectId"), picker.property("requestId")))
        QMetaObject.invokeMethod(picker, "rejected")                                    # cancel
        pump(30)
        self.assertTrue(bridge.approvalPending)
        self.assertIsNone(self.db().load_project_folder(self.project.id))
        existing = self.folder("Var olan")
        QMetaObject.invokeMethod(card, "linkPicked", Q_ARG("QVariant", str(existing)))
        pump(30)
        store = self.db()
        self.assertEqual((str(existing), "picked"), (store.load_project_folder(self.project.id).path,
                                                     store.load_project_folder(self.project.id).origin))
        # Rejected after the link: Core re-attests first, so the now linked
        # project ends it (terminal either way); nothing was created by K3.
        self.assertIn(store.load_task(old.task_id).status.value, ("rejected", "failed"))
        self.assertFalse(bridge.approveRequest(old.request_id, old.fingerprint))       # nothing left to approve
        self.assertEqual(["Var olan"], self.made())

    def test_h3_11_a_failed_rejection_after_the_link_still_cannot_run(self) -> None:
        bridge = self.pending()
        old = bridge._ticket
        root, card = self.card(bridge)
        QMetaObject.invokeMethod(card, "bindPicker")
        existing = self.folder("Var olan")
        bridge._busy = True                                   # the rejection is refused
        QMetaObject.invokeMethod(card, "linkPicked", Q_ARG("QVariant", str(existing)))
        bridge._busy = False
        pump(30)
        store = self.db()
        self.assertEqual("picked", store.load_project_folder(self.project.id).origin)
        self.assertIsNotNone(store.load_pending_approval(old.task_id, old.session_id))
        self.assertTrue(bridge.approveRequest(old.request_id, old.fingerprint))
        self.assertEqual("failed", self.db().load_task(old.task_id).status.value)
        self.assertEqual(["Var olan"], self.made())

    # -- E4 every K3 label, the main card's controls, the real conversation switch --

    def test_e4_every_k3_label_says_this_computer(self) -> None:
        bridge = self.pending()
        here = "This computer"
        self.assertIn(f"-> {here}", " ".join(row["text"] for row in bridge.activity))     # activity note
        self.assertEqual(here, bridge.task["destination"])                                # task block / work panel
        shown = bridge.approval
        self.assertTrue(bridge.approveRequest(shown["request_id"], shown["fingerprint"]))
        safety = bridge.safety
        self.assertEqual(("recorded", here), (safety["decision"]["state"], safety["decision"]["destination"]))
        self.assertEqual("completed", safety["outcome"]["kind"])
        self.assertIn(safety["outcome"]["destination"], (here, ""))                       # "" = "Not reported"
        # The one mapping every label goes through: K3 only, and display only.
        self.assertEqual(here, _here({"capability": PROJECT_FOLDER_CAPABILITY, "destination": CLAUDE_CODE_DESTINATION})["destination"])
        self.assertEqual(CLAUDE_CODE_DESTINATION, _here({"capability": "code_analysis",
                                                         "destination": CLAUDE_CODE_DESTINATION})["destination"])
        rows = [row for row in bridge.feed if row.get("capability") == PROJECT_FOLDER_CAPABILITY]
        self.assertEqual([here] * len(rows), [row["destination"] for row in rows])
        self.assertTrue(rows)                                                             # the result row
        self.assertNotIn(CLAUDE_CODE_DESTINATION, json.dumps([bridge.activity, bridge.feed, bridge.task, safety],
                                                             ensure_ascii=False, default=str))
        # Display only: the stored turn keeps the grant row's destination.
        turns = [t for t in bridge._conversation_store.state.turns if t.capability == PROJECT_FOLDER_CAPABILITY]
        self.assertEqual([CLAUDE_CODE_DESTINATION], [t.destination for t in turns])

    def main_card(self, bridge):
        root = self.window(bridge)
        card = self.item(root, "safetyCard_approval")
        settled(900, lambda: self.assertTrue(card.isVisible()))
        return root, card

    def test_h3_11_the_main_card_has_the_same_controls(self) -> None:
        bridge = self.pending()
        old = bridge._ticket
        calls = len(self.model.calls)
        root, card = self.main_card(bridge)
        self.assertTrue(self.under(card, "k3FolderControls")[0].isVisible())
        switch = self.under(card, "k3OnDesktop")[0]
        self.assertEqual((True, False), (switch.isEnabled(), switch.property("checked")))
        QMetaObject.invokeMethod(switch, "flip")
        pump(30)
        self.assertEqual("rejected", self.db().load_task(old.task_id).status.value)
        new = bridge.approval
        self.assertNotEqual(old.request_id, new["request_id"])
        self.assertEqual((self.path(True), "desktop", self.project.id),
                         (new["target"], new["folder_location"], new["folder_project"]))
        settled(300, lambda: self.assertIs(True, self.under(card, "k3OnDesktop")[0].property("checked")))
        self.assertEqual((calls, []), (len(self.model.calls), self.made()))                # no model call, nothing made
        # "Var olan klasörü seç…" on the main card: links first, then rejects.
        picker = card.findChild(QObject, "k3FolderPicker")
        QMetaObject.invokeMethod(card, "bindPicker")
        self.assertEqual((self.project.id, new["request_id"]), (picker.property("projectId"), picker.property("requestId")))
        QMetaObject.invokeMethod(picker, "rejected")                                    # cancel
        pump(30)
        self.assertTrue(bridge.approvalPending)
        self.assertIsNone(self.db().load_project_folder(self.project.id))
        existing = self.folder("Var olan")
        QMetaObject.invokeMethod(card, "linkPicked", Q_ARG("QVariant", str(existing)))
        pump(30)
        store = self.db()
        self.assertEqual((str(existing), "picked"), (store.load_project_folder(self.project.id).path,
                                                     store.load_project_folder(self.project.id).origin))
        self.assertIn(store.load_task(new["task_id"]).status.value, ("rejected", "failed"))
        self.assertFalse(bridge.approvalPending)
        self.assertEqual(["Var olan"], self.made())

    def test_h3_11_a_conversation_switch_keeps_the_switch_on_the_requests_project(self) -> None:
        bridge = self.pending()
        old, home = bridge._ticket, bridge._conversation_store.id
        self.assertTrue(bridge.newProjectConversation(self.other.id))                    # the real switch
        self.assertFalse(bridge.approvalPending)
        self.assertFalse(bridge.switchProjectFolderLocation(old.request_id, old.fingerprint))   # not on screen
        self.assertEqual("awaiting_approval", self.db().load_task(old.task_id).status.value)
        self.assertTrue(bridge.openConversation(home))
        self.assertEqual(old.request_id, bridge._ticket.request_id)
        self.assertTrue(bridge.switchProjectFolderLocation(old.request_id, old.fingerprint))
        new = bridge.approval
        self.assertEqual((self.path(True), self.project.id), (new["target"], new["folder_project"]))
        self.assertEqual("rejected", self.db().load_task(old.task_id).status.value)
        self.assertEqual([], self.made())

    def test_h3_11_choose_an_existing_folder_across_a_conversation_switch(self) -> None:
        bridge = self.pending()
        old, home = bridge._ticket, bridge._conversation_store.id
        root, card = self.main_card(bridge)
        picker = card.findChild(QObject, "k3FolderPicker")
        QMetaObject.invokeMethod(card, "bindPicker")                                    # bound at the click
        self.assertTrue(bridge.newProjectConversation(self.other.id))                    # then moved away
        self.assertEqual((self.project.id, old.request_id), (picker.property("projectId"), picker.property("requestId")))
        existing = self.folder("Var olan")
        QMetaObject.invokeMethod(card, "linkPicked", Q_ARG("QVariant", str(existing)))
        pump(30)
        store = self.db()
        self.assertEqual("picked", store.load_project_folder(self.project.id).origin)
        self.assertIsNone(store.load_project_folder(self.other.id))
        # Not on this screen, so not rejected here - and it still cannot run.
        self.assertIsNotNone(store.load_pending_approval(old.task_id, old.session_id))
        self.assertTrue(bridge.openConversation(home))
        self.assertTrue(bridge.approveRequest(old.request_id, old.fingerprint))
        self.assertEqual("failed", self.db().load_task(old.task_id).status.value)
        self.assertEqual(["Var olan"], self.made())

    # -- H3-9 through the window: the stored conversation turns ------------------

    def test_h3_9_the_window_stores_no_derived_path_or_folder_name(self) -> None:
        store = self.db()
        first, second, third = (store.create_project(f"{CANARY_NAME} {n}") for n in (1, 2, 3))
        folder = task_proposal(PROJECT_FOLDER_CAPABILITY, target="")
        bridge = self.bridge(folder, folder, folder, task_proposal(PROJECT_FOLDER_CAPABILITY, target=str(self.documents)),
                             folder, proposal(kind="chat", reply="tamam"))
        # Switched, then rejected; asked again and completed.
        self.assertTrue(bridge.newProjectConversation(first.id))
        bridge.submitText(ASK)
        ticket = bridge._ticket
        self.assertTrue(bridge.switchProjectFolderLocation(ticket.request_id, ticket.fingerprint))
        shown = bridge.approval
        self.assertTrue(bridge.rejectRequest(shown["request_id"], shown["fingerprint"]))
        bridge.submitText(ASK)
        shown = bridge.approval
        self.assertTrue(bridge.approveRequest(shown["request_id"], shown["fingerprint"]))
        self.assertEqual("created", self.db().load_project_folder(first.id).origin)
        # Stale: a sibling takes the path before the approval.
        self.assertTrue(bridge.newProjectConversation(second.id))
        bridge.submitText(ASK)
        shown = bridge.approval
        os.makedirs(shown["target"])
        self.assertTrue(bridge.approveRequest(shown["request_id"], shown["fingerprint"]))
        self.assertEqual("failed", self.db().load_task(shown["task_id"]).status.value)
        # Refused (E1: a path from the model), then a partial failure (the leaf made, not linked).
        self.assertTrue(bridge.newProjectConversation(third.id))
        bridge.submitText(ASK)
        self.assertFalse(bridge.approvalPending)
        bridge.submitText(ASK)
        shown = bridge.approval
        h6.CreatePartialFailureTests.insert_fails(self)
        self.assertTrue(bridge.approveRequest(shown["request_id"], shown["fingerprint"]))
        self.assertEqual("failed", self.db().load_task(shown["task_id"]).status.value)
        self.assertTrue(os.path.isdir(shown["target"]))                                  # made, kept, not linked
        # The next understanding call of that chat.
        bridge.submitText("merhaba")
        call = self.model.calls[-1]
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            conversations = connection.execute("SELECT title, state_json FROM conversations").fetchall()
        sinks = {"conversation turns": conversations,
                 "intent payload": {"objective": call.objective, "context": call.context}}
        self.assertTrue(any("merhaba" in state for _, state in conversations))           # the turns are there
        assert_absent(self, sinks, (CANARY_LEAF, str(self.documents) + "\\" + JP, str(self.desktop) + "\\Z"))


if __name__ == "__main__":
    unittest.main()
