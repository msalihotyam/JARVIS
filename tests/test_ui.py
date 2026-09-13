"""The PySide6 / QML interface: the bridge drives the REAL application path.

Every test composes the ordinary application from fake connectors, a
scripted understanding model and temporary config / database / preference
files; the QML loads on the offscreen platform. No model, network or
subprocess. ``sync=True`` runs the worker inline except where the threaded
path itself is under test.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
os.environ.pop("ANTHROPIC_BASE_URL", None)

from PySide6.QtCore import QMetaObject, Q_ARG, Q_RETURN_ARG, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402,F401
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.app import create_app  # noqa: E402
from jarvis.contracts import ProjectRecord, SideEffect  # noqa: E402
from jarvis.destinations import CLAUDE_CODE_DESTINATION, DestinationGrant  # noqa: E402
from jarvis.gmail import GMAIL_CONNECTOR, GMAIL_ENABLED_CAPABILITIES, GmailIdentityBinding  # noqa: E402
from jarvis.memory import SQLiteMemoryStore  # noqa: E402
from jarvis.registry import ToolRegistry  # noqa: E402
from jarvis.settings import ConfigStore  # noqa: E402
from jarvis.ui.app import create_engine  # noqa: E402
from jarvis.ui.backend import ApprovalTicket, Backend  # noqa: E402
from jarvis.ui.bridge import Bridge  # noqa: E402
from jarvis.ui.preferences import (  # noqa: E402
    MAX_COLUMN_WIDTH,
    MAX_SHARE,
    MIN_SHARE,
    UiPreferences,
    canonical_preferences,
    validate_preferences,
)
from tests.helpers import safe_cleanup  # noqa: E402
from tests.test_intent import (  # noqa: E402
    ACCOUNT,
    OTHER_ACCOUNT,
    SECRET,
    GmailStub,
    ScriptedModel,
    StubConnector,
    proposal,
    task_proposal,
)

_APP = QGuiApplication.instance() or QGuiApplication([])


def pump(ms: int = 0) -> None:
    """Turn the GUI loop for ``ms`` while RELEASING the GIL between turns.

    ``QTest.qWait`` spins without releasing the GIL, which starves a Python
    worker thread; the real ``app.exec()`` releases it. ``time.sleep`` does
    the same here.
    """
    deadline = time.monotonic() + ms / 1000
    while True:
        _APP.processEvents()
        _APP.sendPostedEvents()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)


class UiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = self.root / "jarvis.db"
        self.store = ConfigStore(self.root / "config.json")
        self.store.mutate(lambda s: replace(s, memory_database=self.database))
        self.prefs_path = self.root / "prefs.json"
        self.model = ScriptedModel()
        self.gmail_accounts: tuple[str, ...] = ()
        self.claude = StubConnector("claude-code-plan", ("code_analysis", "code_planning"),
                                    side_effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True)
        self.reader = StubConnector("local-file-read", ("file_read",))
        self.gmail = None
        self.engine = None

    def factory(self, settings):
        connectors = [self.claude, self.reader]
        policy = {
            DestinationGrant("claude-code-plan", "code_analysis", CLAUDE_CODE_DESTINATION),
            DestinationGrant("claude-code-plan", "code_planning", CLAUDE_CODE_DESTINATION),
            DestinationGrant("local-file-read", "file_read", CLAUDE_CODE_DESTINATION),
        }
        identity = None
        if self.gmail_accounts:
            if self.gmail is None:
                self.gmail = GmailStub(self.gmail_accounts[0])
            connectors.append(self.gmail)
            policy |= {DestinationGrant(GMAIL_CONNECTOR, cap, f"gmail/{a}")
                       for a in self.gmail_accounts for cap in GMAIL_ENABLED_CAPABILITIES}
            identity = GmailIdentityBinding(principal=self.gmail_accounts[0], config_dir=str(self.root))
        return create_app(
            settings=settings, memory=SQLiteMemoryStore(settings.memory_database), registry=ToolRegistry(),
            connectors=connectors, destination_policy=frozenset(policy), gmail_identity=identity,
            intent_model=self.model,
        )

    def bridge(self, *answers, sync: bool = True) -> Bridge:
        self.model.answers = list(answers)
        self.backend = Backend(self.store, self.factory)
        bridge = Bridge(self.backend, UiPreferences(self.prefs_path), sync=sync)
        bridge.refreshConfig()
        return bridge

    def load(self, bridge: Bridge):
        self.engine = create_engine(bridge)
        self.addCleanup(self.engine.deleteLater)
        roots = self.engine.rootObjects()
        self.assertEqual(1, len(roots), "Main.qml did not load")
        pump(50)
        return roots[0]

    def kinds(self, bridge: Bridge) -> list[str]:
        return [entry["kind"] for entry in bridge.feed]

    def feed_text(self, bridge: Bridge) -> str:
        return "\n".join(entry["text"] for entry in bridge.feed)

    def execute_calls(self) -> int:
        return self.claude.execute_calls + self.reader.execute_calls + (self.gmail.execute_calls if self.gmail else 0)


# --------------------------------------------------------------------------
# Bootstrap, themes, preferences
# --------------------------------------------------------------------------


class BootstrapTests(UiTestCase):
    def test_app_bootstraps_and_qml_loads_full_screen(self) -> None:
        bridge = self.bridge()
        root = self.load(bridge)
        self.assertEqual("JARVIS", root.title())
        self.assertEqual("FullScreen", root.visibility().name)
        self.assertEqual("idle", bridge.coreState)
        self.assertEqual("home", root.property("loadedPage"))
        self.assertTrue(bridge.executionEnabled)
        self.assertIn("JARVIS interface started", bridge.activity[0]["text"])

    def test_canonical_theme_is_default_and_dark_theme_loads(self) -> None:
        bridge = self.bridge()
        root = self.load(bridge)
        self.assertEqual("space", root.property("themeName"))
        space_accent = QColor(root.property("accentToken"))
        space_bg = QColor(root.property("bgToken"))
        bridge.setTheme("dark")
        pump(20)
        self.assertEqual("dark", root.property("themeName"))
        # Mid-transition: the tokens are moving, not cut.
        pump(120)
        mid_bg = QColor(root.property("bgToken"))
        self.assertNotEqual(space_bg.name(), mid_bg.name())
        pump(500)
        dark_accent = QColor(root.property("accentToken"))
        dark_bg = QColor(root.property("bgToken"))
        self.assertEqual("#1bbaff", dark_accent.name())
        self.assertEqual("#030709", dark_bg.name())
        self.assertNotEqual(space_accent.name(), dark_accent.name())
        # Both themes reach a stable state; the transition completes cleanly.
        bridge.setTheme("space")
        pump(600)
        self.assertEqual(space_accent.name(), QColor(root.property("accentToken")).name())

    def test_theme_and_ambient_are_persisted_and_validated(self) -> None:
        bridge = self.bridge()
        bridge.setTheme("dark")
        bridge.setAmbientEffects(False)
        bridge.setTheme("neon")  # not a theme: ignored
        saved = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(("dark", False, 2), (saved["theme"], saved["ambient_effects"], saved["version"]))
        again = Bridge(self.backend, UiPreferences(self.prefs_path), sync=True)
        self.assertEqual("dark", again.theme)
        self.assertFalse(again.ambientEffects)

    def test_malformed_preferences_fall_back_to_canonical(self) -> None:
        for raw in ("{not json", "[]", json.dumps({"theme": "dark"}),
                    json.dumps({**canonical_preferences(), "version": 99}),
                    json.dumps({**canonical_preferences(), "theme": "neon"})):
            with self.subTest(raw=raw[:20]):
                self.prefs_path.write_text(raw, encoding="utf-8")
                self.assertEqual(canonical_preferences(), UiPreferences(self.prefs_path).data)
        broken = canonical_preferences()
        broken["profiles"]["normal"]["panels"]["system"]["share"] = 5.0
        self.assertEqual(canonical_preferences(), validate_preferences(broken))
        broken = canonical_preferences()
        broken["profiles"]["normal"]["column_width"] = 0.9
        self.assertEqual(canonical_preferences(), validate_preferences(broken))
        broken = canonical_preferences()
        broken["profiles"]["normal"]["order"] = ["system"]
        self.assertEqual(canonical_preferences(), validate_preferences(broken))


# --------------------------------------------------------------------------
# The natural-language composer over JarvisApp.submit_text
# --------------------------------------------------------------------------


class ComposerTests(UiTestCase):
    def test_plain_text_goes_through_submit_text_to_the_ordinary_gate(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis", objective="Bu projeyi incele."))
        bridge.submitText("Bu projeyi incele.")
        self.assertEqual("Bu projeyi incele.", self.model.calls[0].objective)
        self.assertEqual(["user", "system", "system", "approval"], self.kinds(bridge), self.feed_text(bridge))
        self.assertIn("Understood as code_analysis -> anthropic/claude-code", self.feed_text(bridge))
        self.assertTrue(bridge.approvalPending)
        self.assertEqual("awaiting_approval", bridge.coreState)
        self.assertEqual("awaiting_approval", bridge.task["run_status"])
        self.assertEqual(CLAUDE_CODE_DESTINATION, bridge.approval["destination"])
        self.assertEqual(0, self.execute_calls())
        self.assertNotEqual("", bridge.sessionId)

    def test_a_scheduled_result_is_rendered_and_listed(self) -> None:
        SQLiteMemoryStore(self.database).save_project(ProjectRecord(id="evim", name="Evim"))
        at = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        bridge = self.bridge(proposal(kind="job", label="fatura", schedule={"kind": "one_time", "at": at.isoformat(timespec="minutes")},
                                      step={"objective": "Faturayı hatırlat", "capability": "code_analysis", "target": ""}))
        bridge.submitText("Yarın saat 10'da faturayı ödemeyi hatırlat.")
        self.assertIn("scheduled", self.kinds(bridge))
        self.assertIn("SCHEDULED: 'fatura' (one_time)", self.feed_text(bridge))
        self.assertEqual(1, len(bridge.jobs))
        self.assertEqual("idle", bridge.coreState)

    def test_clarification_choices_render_and_exact_choice_submits(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge = self.bridge(task_proposal("gmail_thread_list", limit=5), task_proposal("gmail_thread_list", limit=5))
        bridge.submitText("Son maillerimi kontrol et.")
        self.assertEqual("clarify", self.kinds(bridge)[-1])
        labels = [c["label"] for c in bridge.clarification["choices"]]
        self.assertEqual([ACCOUNT, OTHER_ACCOUNT], labels)
        self.assertEqual("awaiting_input", bridge.coreState)
        self.assertEqual(0, self.execute_calls())
        bridge.chooseOption(OTHER_ACCOUNT)
        # The exact label became the current text; the original request rode along.
        self.assertEqual(OTHER_ACCOUNT, self.model.calls[1].objective)
        self.assertEqual("Son maillerimi kontrol et.", self.model.calls[1].context["previous_request"])
        self.assertEqual({}, bridge.clarification)
        self.assertEqual(f"gmail/{OTHER_ACCOUNT}", bridge.task["destination"])
        self.assertTrue(bridge.approvalPending)

    def test_stale_or_foreign_choices_never_apply(self) -> None:
        self.gmail_accounts = (ACCOUNT, OTHER_ACCOUNT)
        bridge = self.bridge(task_proposal("gmail_thread_list", limit=5), task_proposal("code_analysis"))
        bridge.submitText("Son maillerimi kontrol et.")
        self.assertTrue(bridge.clarification["choices"])
        bridge.chooseOption("attacker@example.com")  # not an offered label
        self.assertEqual(1, len(self.model.calls))
        bridge.submitText("Bu projeyi incele.")  # a new request invalidates the old choices
        self.assertEqual({}, bridge.clarification)
        bridge.deny()
        bridge.chooseOption(ACCOUNT)  # stale: nothing happens
        self.assertEqual(2, len(self.model.calls))

    def test_running_completed_and_error_states(self) -> None:
        bridge = self.bridge(task_proposal("file_read", target="notes.txt"), task_proposal("shell_exec"))
        (self.root / "notes.txt").write_text("hello", encoding="utf-8")
        bridge.submitText("notes.txt dosyasını oku")
        self.assertEqual("completed", bridge.task["run_status"], self.feed_text(bridge))
        self.assertEqual("completed", bridge.coreState)
        self.assertEqual(1, self.reader.execute_calls)
        bridge.submitText("yap")
        self.assertEqual("error", bridge.coreState)
        self.assertIn("UNSUPPORTED", self.feed_text(bridge))
        self.assertEqual(1, self.execute_calls())

    def test_every_intent_status_renders_without_executing(self) -> None:
        bridge = self.bridge()
        for status, kind in (("unsupported_capability", "error"), ("authority_unavailable", "error"),
                             ("malformed_model_output", "error"), ("model_unavailable", "error"),
                             ("model_timeout", "error"), ("model_cancelled", "error"), ("model_failed", "error"),
                             ("execution_disabled", "error"), ("invalid_input", "error"), ("bogus", "unknown")):
            with self.subTest(status=status):
                bridge._apply_text({"command": "text", "intent": {"status": status, "message": "m", "request_id": "r", "choices": []},
                                    "run": None, "job": None, "error": None}, "t")
                self.assertEqual(kind, bridge.feed[-1]["kind"])
                self.assertFalse(bridge.approvalPending)
        self.assertEqual(0, self.execute_calls())


# --------------------------------------------------------------------------
# Approval, cancellation, kill switch, stale isolation
# --------------------------------------------------------------------------


class AuthorityTests(UiTestCase):
    def pending(self) -> Bridge:
        bridge = self.bridge(task_proposal("code_analysis"))
        bridge.submitText("Bu projeyi incele.")
        self.assertTrue(bridge.approvalPending)
        return bridge

    def test_approve_routes_the_exact_ticket(self) -> None:
        bridge = self.pending()
        ticket = bridge._ticket
        pending = SQLiteMemoryStore(self.database).load_pending_approval(ticket.task_id, ticket.session_id)
        self.assertEqual((pending.id, pending.fingerprint), (ticket.request_id, ticket.fingerprint))
        bridge.approve()
        self.assertEqual(1, self.claude.execute_calls)
        self.assertEqual("completed", bridge.coreState)
        self.assertFalse(bridge.approvalPending)

    def test_deny_routes_the_exact_ticket(self) -> None:
        bridge = self.pending()
        task_id = bridge._ticket.task_id
        bridge.deny()
        self.assertEqual(0, self.claude.execute_calls)
        self.assertEqual("rejected", SQLiteMemoryStore(self.database).load_task(task_id).status.value)
        self.assertFalse(bridge.approvalPending)

    def test_a_stale_or_forged_ticket_cannot_act(self) -> None:
        bridge = self.pending()
        real = bridge._ticket
        bridge._ticket = replace(real, fingerprint="0" * 64)
        bridge.approve()
        self.assertEqual(0, self.claude.execute_calls)
        self.assertIn("REFUSED [invalid_grant]", self.feed_text(bridge))
        self.assertFalse(bridge.approvalPending)
        # A ticket for a task that has since been cancelled is refused too.
        bridge._ticket = real
        bridge.cancelTask()
        bridge._ticket = real
        bridge.approve()
        self.assertEqual(0, self.claude.execute_calls)

    def test_cancel_calls_the_real_mechanism(self) -> None:
        bridge = self.pending()
        task_id = bridge._ticket.task_id
        self.assertTrue(bridge.taskLive)
        bridge.cancelTask()
        self.assertEqual("cancelled", SQLiteMemoryStore(self.database).load_task(task_id).status.value)
        self.assertFalse(bridge.approvalPending)
        self.assertFalse(bridge.taskLive)

    def test_cancel_text_stops_the_running_understanding_call(self) -> None:
        bridge = self.bridge()
        self.model.error = __import__("jarvis.contracts", fromlist=["StructuredError"]).StructuredError(
            __import__("jarvis.contracts", fromlist=["ErrorCode"]).ErrorCode.CONNECTOR_FAILED, "killed")
        seen = {}
        self.model.on_complete = lambda task: seen.update(cancelled=bridge.cancelText(), request=task.id)
        bridge.submitText("incele")
        self.assertTrue(seen["cancelled"])
        self.assertEqual([seen["request"]], self.model.cancelled)
        self.assertIn("UNDERSTANDING CANCELLED", self.feed_text(bridge))
        self.assertFalse(bridge.cancelText())  # nothing running now

    def test_unknown_outcome_is_first_class(self) -> None:
        class Raising(StubConnector):
            def execute(self, task, session):
                self.execute_calls += 1
                raise RuntimeError("socket died")

        self.claude = Raising("claude-code-plan", ("code_analysis", "code_planning"),
                              side_effect=SideEffect.EXTERNAL_EGRESS, always_requires_approval=True)
        bridge = self.pending()
        bridge.approve()
        self.assertEqual("unknown", bridge.coreState)
        self.assertEqual("execution_unknown_outcome", bridge.task["error_code"])
        self.assertIn("unknown", self.kinds(bridge))
        self.assertIn("will NOT retry", self.feed_text(bridge))
        self.assertEqual("unknown", bridge.activity[0]["kind"])

    def test_kill_switch_is_the_persisted_barrier(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis"))
        bridge.setExecutionEnabled(False)
        self.assertFalse(self.store.load().settings.execution_enabled)
        self.assertEqual("disabled", bridge.coreState)
        self.assertFalse(bridge.executionEnabled)
        bridge.submitText("Bu projeyi incele.")
        self.assertIn("EXECUTION DISABLED", self.feed_text(bridge))
        self.assertEqual([], self.model.calls)
        bridge.setExecutionEnabled(True)
        self.assertTrue(self.store.load().settings.execution_enabled)
        bridge.submitText("Bu projeyi incele.")
        self.assertEqual(1, len(self.model.calls))

    def test_stale_worker_completion_never_overwrites_a_newer_operation(self) -> None:
        bridge = self.bridge()
        bridge._run("task", lambda: {"command": "run", "task": {"id": "old"}}, bridge._apply_run)
        before = list(bridge.feed)
        # A completion carrying an OLD sequence number arrives after a newer op.
        bridge._sequence["task"] = 99
        bridge._handlers[("task", 1)] = bridge._apply_run
        bridge._on_finished("task", 1, {"command": "run", "task": {"id": "stale", "status": "completed"}})
        self.assertEqual(before, bridge.feed)
        self.assertNotEqual("stale", bridge.task.get("task_id"))

    def test_background_work_does_not_block_the_gui_thread(self) -> None:
        release = threading.Event()
        started = threading.Event()

        class Slow(ScriptedModel):
            def complete(self, task, session):
                started.set()
                release.wait(10)
                return super().complete(task, session)

        self.model = Slow()
        bridge = self.bridge(task_proposal("code_analysis"), sync=False)
        t0 = time.monotonic()
        bridge.submitText("Bu projeyi incele.")
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(bridge.busy)
        self.assertTrue(started.wait(5))
        pump(50)  # the GUI loop keeps turning while the worker blocks
        self.assertTrue(bridge.busy)
        release.set()
        deadline = time.monotonic() + 10
        while bridge.busy and time.monotonic() < deadline:
            pump(20)
        self.assertFalse(bridge.busy)
        self.assertTrue(bridge.approvalPending)
        bridge.waitIdle()

    def test_secrets_never_reach_the_feed(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis", objective=f"incele {SECRET}"))
        bridge.submitText(f"incele api_key={SECRET}")
        text = self.feed_text(bridge) + json.dumps(bridge.activity) + json.dumps(bridge.task)
        self.assertNotIn(SECRET, text)
        self.assertIn("[REDACTED]", text)

    def test_malformed_backend_state_fails_safe(self) -> None:
        bridge = self.bridge()
        for payload in (None, "x", 5, {}, {"command": "run", "task": "bogus", "approval": 5},
                        {"command": "run", "session": {"id": 1}, "task": {"id": "t"}, "approval": {"request_id": "r"}}):
            with self.subTest(payload=payload):
                bridge._apply_run(payload)
                self.assertFalse(bridge.approvalPending)
        bridge._apply_text("nope", "t")
        bridge._apply_text({"intent": 5}, "t")
        self.assertEqual("unknown", bridge.feed[-1]["kind"])
        self.assertEqual({}, bridge.clarification)


# --------------------------------------------------------------------------
# Data pages
# --------------------------------------------------------------------------


class DataTests(UiTestCase):
    def test_provider_model_effort_settings(self) -> None:
        bridge = self.bridge()
        bridge.applyModel("claude-opus-5", "high")
        settings = self.store.load().settings
        self.assertEqual(("claude-opus-5", "high"), (settings.claude_model, settings.claude_effort))
        self.assertEqual("claude-opus-5", bridge.config["claude_model"])
        bridge.applyModel("--dangerous", "high")
        self.assertEqual("claude-opus-5", self.store.load().settings.claude_model)
        self.assertIn("REFUSED [config_invalid]", self.feed_text(bridge))
        bridge.applyModel("", "")
        self.assertEqual((None, None), (self.store.load().settings.claude_model, self.store.load().settings.claude_effort))
        self.assertEqual(["low", "medium", "high", "xhigh", "max"], bridge.effortChoices)

    def test_scheduler_page_reads_and_manages_jobs(self) -> None:
        SQLiteMemoryStore(self.database).save_project(ProjectRecord(id="evim", name="Evim"))
        bridge = self.bridge()
        bridge.createJob(json.dumps({
            "project_id": "evim", "kind": "recurring", "label": "nightly notes",
            "schedule": {"kind": "interval", "anchor": "2026-09-06T12:00:00+00:00", "every_seconds": 3600, "end": None},
            "steps": [{"objective": "Read notes.", "capability": "file_read", "action": "read", "target": "notes.txt"}],
            "condition": None, "on_true": []}))
        self.assertEqual(1, len(bridge.jobs))
        job = bridge.jobs[0]
        self.assertEqual("nightly notes", job["label"])
        bridge.setJobEnabled(job["id"], job["revision"], False)
        self.assertFalse(bridge.jobs[0]["enabled"])
        bridge.archiveJob(job["id"], bridge.jobs[0]["revision"])
        self.assertEqual([], bridge.jobs)
        bridge.refreshJobs(True)
        self.assertTrue(bridge.jobs[0]["archived"])

    def test_memory_page_reads_existing_memory(self) -> None:
        memory = SQLiteMemoryStore(self.database)
        memory.save_project(ProjectRecord(id="evim", name="Evim"))
        memory.save_explicit_user_memory_item("evim", "fact", "internal", "tabs are 4 spaces")
        bridge = self.bridge()
        bridge.refreshMemory("", False)
        self.assertEqual(["evim"], bridge.projects)
        self.assertEqual("tabs are 4 spaces", bridge.memoryItems[0]["text"])

    def test_history_and_task_snapshot(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis"))
        bridge.submitText("Bu projeyi incele.")
        session_id, task_id = bridge.sessionId, bridge.task["task_id"]
        bridge.refreshHistory()
        self.assertEqual(session_id, bridge.sessions[0]["session_id"])
        bridge.loadTasks(session_id)
        self.assertEqual(task_id, bridge.tasks[0]["task_id"])
        bridge.showTask(session_id, task_id)
        self.assertIn("Awaiting approval", "\n".join(e["text"] for e in bridge.taskDetail))
        bridge.newSession()
        self.assertEqual("", bridge.sessionId)
        bridge.useSession(session_id)
        self.assertEqual(session_id, bridge.sessionId)


# --------------------------------------------------------------------------
# Layout: edit mode, bounds, profiles, focus, pinning, navigation, palette
# --------------------------------------------------------------------------


class LayoutTests(UiTestCase):
    def shown(self, bridge: Bridge) -> list[str]:
        return [p["id"] for p in bridge.panels if p["shown"]]

    def test_edit_mode_show_hide_move_within_bounds(self) -> None:
        bridge = self.bridge()
        self.assertEqual(["system", "workflow", "activity"], self.shown(bridge))
        bridge.setPanelPinned("workflow", False)
        self.assertEqual(["system", "activity"], self.shown(bridge))  # unpinned + no task: contextual, hidden
        bridge.setEditMode(True)
        self.assertTrue(bridge.editMode)
        self.assertEqual(["system", "workflow", "activity"], self.shown(bridge))  # edit mode reveals contextual panels
        bridge.setPanelVisible("system", False)
        self.assertEqual(["workflow", "activity"], self.shown(bridge))
        self.assertIn("system", bridge.hiddenPanels)
        bridge.setPanelVisible("system", True)
        bridge.movePanel("system", -5)  # cannot leave the top
        self.assertEqual("system", bridge.prefs.layout()["order"][0])
        bridge.movePanel("system", 99)  # cannot leave the bottom
        self.assertEqual("system", bridge.prefs.layout()["order"][-1])
        bridge.setPanelVisible("bogus", True)  # unknown panel: ignored
        self.assertEqual(4, len(bridge.panels))

    def test_resize_bounds_snap_and_off_screen_prevention(self) -> None:
        bridge = self.bridge()
        bridge.setPanelShare("system", 5.0)
        self.assertEqual(MAX_SHARE, bridge.prefs.layout()["panels"]["system"]["share"])
        bridge.setPanelShare("system", -1.0)
        self.assertEqual(MIN_SHARE, bridge.prefs.layout()["panels"]["system"]["share"])
        bridge.setPanelShare("system", 0.333)
        self.assertEqual(0.34, bridge.prefs.layout()["panels"]["system"]["share"])
        bridge.setColumnWidth(0.95)
        self.assertEqual(MAX_COLUMN_WIDTH, bridge.columnWidth)
        bridge.setColumnWidth(-3)
        self.assertEqual(0.22, bridge.columnWidth)
        root = self.load(bridge)
        from PySide6.QtQuick import QQuickItem
        column = root.findChild(QQuickItem, "panelColumn")
        self.assertIsNotNone(column)
        for _ in range(3):
            bounds = QMetaObject.invokeMethod(column, "slotBounds", Q_RETURN_ARG("QVariant")).toVariant()
            for slot in bounds:
                self.assertGreaterEqual(slot["y"], 0)
                self.assertGreaterEqual(slot["height"], 120)
                self.assertLessEqual(slot["y"] + slot["height"], column.height() + 1)
                self.assertLessEqual(slot["width"], column.width() + 1)
            bridge.setPanelShare("activity", MAX_SHARE)
            bridge.setEditMode(True)

    def test_reset_layout_restores_canonical(self) -> None:
        bridge = self.bridge()
        bridge.setPanelVisible("system", False)
        bridge.setPanelShare("activity", 0.6)
        bridge.movePanel("automations", -3)
        bridge.resetLayout()
        self.assertEqual(canonical_preferences()["profiles"]["normal"], bridge.prefs.layout())
        self.assertEqual(["system", "workflow", "activity"], self.shown(bridge))

    def test_profiles_keep_their_own_customizations(self) -> None:
        bridge = self.bridge()
        bridge.setPanelVisible("automations", True)
        bridge.setProfile("monitoring")
        self.assertEqual(0.42, bridge.columnWidth)
        self.assertEqual(["system", "workflow", "activity", "automations"], self.shown(bridge))
        bridge.setProfile("focus")
        self.assertEqual(0.24, bridge.columnWidth)
        self.assertEqual(["workflow"], self.shown(bridge))
        bridge.setProfile("normal")
        self.assertEqual(["system", "workflow", "activity", "automations"], self.shown(bridge))
        self.assertEqual("normal", UiPreferences(self.prefs_path).data["profile"])

    def test_focus_mode_is_temporary_and_restores_exactly(self) -> None:
        bridge = self.bridge()
        bridge.setPanelPinned("activity", True)
        before_shown = self.shown(bridge)
        before_file = self.prefs_path.read_text(encoding="utf-8")
        bridge.setEditMode(True)
        bridge.setFocusMode(True)
        self.assertTrue(bridge.focusMode)
        self.assertFalse(bridge.editMode)
        self.assertEqual(["workflow", "activity"], self.shown(bridge))  # only the pinned panels stay
        self.assertEqual(before_file, self.prefs_path.read_text(encoding="utf-8"))
        bridge.setFocusMode(False)
        self.assertEqual(before_shown, self.shown(bridge))
        self.assertEqual(before_file, self.prefs_path.read_text(encoding="utf-8"))

    def test_pinned_contextual_panel_stays_visible(self) -> None:
        bridge = self.bridge(task_proposal("code_analysis"))
        self.assertIn("workflow", self.shown(bridge))  # canonical: pinned, shown with its empty state
        bridge.setPanelPinned("workflow", False)
        self.assertNotIn("workflow", self.shown(bridge))
        bridge.setPanelPinned("workflow", True)
        self.assertIn("workflow", self.shown(bridge))
        bridge.setPanelPinned("workflow", False)
        self.assertNotIn("workflow", self.shown(bridge))
        bridge.submitText("Bu projeyi incele.")
        self.assertIn("workflow", self.shown(bridge))  # context: a task exists

    def test_navigation_pages_and_command_palette(self) -> None:
        bridge = self.bridge()
        root = self.load(bridge)
        for page in ("tasks", "automations", "memory", "history", "home"):
            bridge.setPage(page)
            pump(30)
            self.assertEqual(page, root.property("loadedPage"))
        bridge.setPage("nowhere")
        self.assertEqual("home", bridge.page)
        bridge.setPage("settings")  # no longer a page: a drawer over the current one
        self.assertEqual("home", bridge.page)
        self.assertFalse(root.property("paletteOpen"))
        QTest.keyClick(root, Qt.Key_K, Qt.ControlModifier)
        pump(30)
        self.assertTrue(root.property("paletteOpen"))
        QTest.keyClick(root, Qt.Key_Escape)
        pump(30)
        self.assertFalse(root.property("paletteOpen"))
        QTest.keyClick(root, Qt.Key_M, Qt.ControlModifier)
        pump(30)
        self.assertTrue(root.property("navOpen"))
        QTest.keyClick(root, Qt.Key_3, Qt.ControlModifier)
        pump(30)
        self.assertEqual("automations", bridge.page)

    def test_settings_is_a_drawer_that_never_traps(self) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtQuick import QQuickItem
        bridge = self.bridge()
        root = self.load(bridge)
        bridge.setPage("tasks")
        pump(30)
        self.assertFalse(root.property("settingsOpen"))

        def click(object_name: str) -> None:
            item = root.findChild(QQuickItem, object_name)
            self.assertIsNotNone(item, object_name)
            center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
            pump(60)

        # The orb left of the wordmark opens it, over the current page.
        click("settingsOrb")
        self.assertTrue(root.property("settingsOpen"))
        self.assertEqual("tasks", root.property("loadedPage"))
        # Esc closes it; the page and its state are exactly as before.
        QTest.keyClick(root, Qt.Key_Escape)
        pump(60)
        self.assertFalse(root.property("settingsOpen"))
        self.assertEqual("tasks", bridge.page)
        # The orb toggles: open, then the same click closes.
        click("settingsOrb")
        self.assertTrue(root.property("settingsOpen"))
        click("settingsOrb")
        self.assertFalse(root.property("settingsOpen"))
        # Ctrl+6 toggles both ways; the close control closes.
        QTest.keyClick(root, Qt.Key_6, Qt.ControlModifier)
        pump(40)
        self.assertTrue(root.property("settingsOpen"))
        QTest.keyClick(root, Qt.Key_6, Qt.ControlModifier)
        pump(40)
        self.assertFalse(root.property("settingsOpen"))
        QMetaObject.invokeMethod(root, "toggleSettings")
        pump(40)
        self.assertTrue(root.property("settingsOpen"))
        click("settingsSurfaceClose")
        self.assertFalse(root.property("settingsOpen"))
        # No stuck state: normal interaction resumes on the page underneath.
        bridge.setPage("home")
        pump(30)
        bridge.submitText("Bu projeyi incele.")
        self.assertEqual("user", self.kinds(bridge)[0])
        # A document written by the earlier build (page "settings") loads as home,
        # keeping the rest of the user's choices.
        legacy = {**canonical_preferences(), "theme": "dark", "page": "settings"}
        self.assertEqual({**canonical_preferences(), "theme": "dark", "page": "home"}, validate_preferences(legacy))


class PresentationTests(UiTestCase):
    """Real QML pointer/keyboard events, not source-string assertions."""

    def item(self, root, name):
        from PySide6.QtQuick import QQuickItem
        def visual_find(node):
            if node.objectName() == name:
                return node
            for child in node.childItems():
                found = visual_find(child)
                if found is not None:
                    return found
            return None
        item = visual_find(root.contentItem())
        self.assertIsNotNone(item, name)
        return item

    def click(self, root, name):
        from PySide6.QtCore import QPointF
        item = self.item(root, name)
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(260)

    def window(self, bridge):
        root = self.load(bridge)
        root.showNormal()
        root.resize(1920, 1080)
        pump(350)
        return root

    def test_distinct_anchored_contexts_toggle_escape_outside_and_selection(self):
        from PySide6.QtCore import QPoint
        bridge = self.bridge()
        root = self.window(bridge)
        for trigger, state, surface in (("settingsOrb", "settingsOpen", "settingsSurface"),
                                        ("modelTrigger", "modelOpen", "modelSurface"),
                                        ("navigationTrigger", "navOpen", "navSurface")):
            with self.subTest(trigger=trigger):
                self.click(root, trigger)
                self.assertTrue(root.property(state))
                self.assertEqual(1, sum(bool(root.property(key)) for key in ("settingsOpen", "modelOpen", "navOpen")))
                panel = self.item(root, surface)
                if trigger == "settingsOrb":
                    self.assertLess(panel.x(), 80)
                else:
                    self.assertGreater(panel.x(), root.width() / 2)
                self.assertGreaterEqual(panel.y(), 80)
                self.click(root, trigger)
                self.assertFalse(root.property(state))
                self.click(root, trigger)
                QTest.keyClick(root, Qt.Key_Escape)
                pump(250)
                self.assertFalse(root.property(state))
                self.click(root, trigger)
                QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(850, 920))
                pump(250)
                self.assertFalse(root.property(state))
        self.click(root, "settingsOrb")
        self.click(root, "modelTrigger")
        self.assertFalse(root.property("settingsOpen"))
        self.assertTrue(root.property("modelOpen"))
        self.click(root, "navigationTrigger")
        self.assertFalse(root.property("modelOpen"))
        self.click(root, "navigate_tasks")
        self.assertEqual("tasks", bridge.page)
        self.assertFalse(root.property("navOpen"))

    def test_context_and_protected_composer_bounds_at_all_required_sizes(self):
        from PySide6.QtCore import QPointF
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in ("space", "dark", "command"):
            bridge.setTheme(theme)
            for width, height in ((1920, 1080), (1280, 760), (1100, 700)):
                with self.subTest(theme=theme, size=(width, height)):
                    root.resize(width, height)
                    pump(500)
                    composer = self.item(root, "commandComposer" if theme == "command" else "canonicalComposer")
                    corner = composer.mapToScene(QPointF(composer.width(), composer.height()))
                    self.assertLessEqual(corner.x(), width)
                    self.assertLessEqual(corner.y(), height)
                    self.assertGreater(composer.width(), 360)
                    for trigger, surface in (("settingsOrb", "settingsSurface"), ("modelTrigger", "modelSurface"), ("navigationTrigger", "navSurface")):
                        self.click(root, trigger)
                        panel = self.item(root, surface)
                        self.assertGreaterEqual(panel.x(), 0)
                        self.assertGreaterEqual(panel.y(), 80)
                        self.assertLessEqual(panel.x() + panel.width(), width)
                        self.assertLessEqual(panel.y() + panel.height(), height)
                        QTest.keyClick(root, Qt.Key_Escape)
                        pump(220)

    def test_package_selection_restores_each_custom_layout_and_focus(self):
        from copy import deepcopy
        bridge = self.bridge()
        root = self.window(bridge)
        self.click(root, "settingsOrb")
        bridge.setPanelShare("system", 0.52)
        bridge.setColumnWidth(0.38)
        canonical = deepcopy(bridge.prefs.layout())
        self.click(root, "package_command")
        self.assertEqual("command", bridge.theme)
        self.assertIsNotNone(self.item(root, "commandPreset"))
        self.assertNotEqual(canonical, bridge.prefs.layout())
        bridge.movePanel("system", 2)
        bridge.setPanelVisible("activity", False)
        bridge.setColumnWidth(0.28)
        command = deepcopy(bridge.prefs.layout())
        saved = self.prefs_path.read_bytes()
        bridge.setFocusMode(True)
        bridge.setFocusMode(False)
        self.assertEqual(saved, self.prefs_path.read_bytes())
        self.assertEqual(command, bridge.prefs.layout())
        self.click(root, "package_dark")
        self.assertEqual("dark", bridge.theme)
        bridge.setPanelShare("workflow", 0.64)
        dark = deepcopy(bridge.prefs.layout())
        self.click(root, "package_space")
        self.assertEqual(canonical, bridge.prefs.layout())
        bridge.setTheme("command")
        self.assertEqual(command, bridge.prefs.layout())
        bridge.setTheme("dark")
        self.assertEqual(dark, bridge.prefs.layout())
        self.assertEqual(bridge.prefs.data, UiPreferences(self.prefs_path).data)

    def test_v1_migrates_without_losing_layout_and_invalid_packages_refuse(self):
        from copy import deepcopy
        legacy = canonical_preferences()
        legacy.pop("packages")
        legacy["version"] = 1
        legacy["profiles"]["normal"]["panels"]["system"]["share"] = 0.54
        migrated = validate_preferences(legacy)
        self.assertEqual(2, migrated["version"])
        self.assertEqual(legacy["profiles"], migrated["profiles"])
        self.assertEqual({"dark", "command"}, set(migrated["packages"]))
        for key, value in (("profile", "foreign"), ("profiles", {})):
            bad = deepcopy(migrated)
            bad["packages"]["command"][key] = value
            self.assertEqual(canonical_preferences(), validate_preferences(bad))

    def test_approval_fixture_cannot_create_authority_or_persist_product_state(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"JARVIS_UI_DEV_FIXTURE": "1"}):
            bridge = self.bridge()
            before = self.store.path.read_bytes() if hasattr(self.store, "path") else None
            root = self.window(bridge)
            QTest.keyClick(root, Qt.Key_A, Qt.ControlModifier | Qt.ShiftModifier)
            pump(300)
            self.assertTrue(root.property("devApprovalVisible"))
            self.assertFalse(bridge.approvalPending)
            self.assertIsNone(bridge._ticket)
            self.assertEqual({}, bridge.task)
            self.assertEqual("", bridge.sessionId)
            self.assertFalse(self.item(root, "approvalApprove").isEnabled())
            self.click(root, "approvalApprove")
            self.assertEqual(0, self.execute_calls())
            self.assertIsNone(bridge._ticket)
            self.assertEqual([], bridge.sessions)
            if before is not None:
                self.assertEqual(before, self.store.path.read_bytes())

    def test_actual_approval_card_still_uses_exact_backend_ticket(self):
        bridge = self.bridge(task_proposal("code_analysis"))
        root = self.window(bridge)
        bridge.submitText("Bu projeyi incele.")
        pump(300)
        ticket = bridge._ticket
        self.assertIsNotNone(ticket)
        for name in ("session_id", "task_id", "request_id", "fingerprint", "expires_at"):
            self.assertTrue(bridge.approval[name], name)
        self.click(root, "approvalApprove")
        self.assertFalse(bridge.approvalPending)
        self.assertEqual(1, self.claude.execute_calls)
        self.assertEqual("completed", bridge.coreState)


class SecondaryPageTests(UiTestCase):
    item = PresentationTests.item
    click = PresentationTests.click
    window = PresentationTests.window

    def test_all_secondary_pages_close_to_same_primary_package_by_x_and_escape(self):
        from copy import deepcopy
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in ("space", "dark", "command"):
            bridge.setTheme(theme)
            pump(450)
            layout = deepcopy(bridge.prefs.layout())
            for page in ("tasks", "automations", "memory", "history"):
                for action in ("x", "escape"):
                    with self.subTest(theme=theme, page=page, action=action):
                        self.click(root, "navigationTrigger")
                        self.click(root, "navigate_" + page)
                        self.assertEqual(page, bridge.page)
                        close = self.item(root, "secondaryPageClose")
                        self.assertTrue(close.isVisible())
                        self.assertTrue(close.isEnabled())
                        if action == "x":
                            self.click(root, "secondaryPageClose")
                        else:
                            QTest.keyClick(root, Qt.Key_Escape)
                            pump(300)
                        self.assertEqual("home", bridge.page)
                        self.assertTrue(root.isVisible())
                        self.assertEqual(theme, bridge.theme)
                        self.assertEqual(layout, bridge.prefs.layout())
        self.assertEqual(0, self.execute_calls())

    def test_escape_dismisses_context_first_then_page_even_with_spec_focus(self):
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("automations")
        pump(300)
        self.click(root, "jobSpecInput")
        QTest.keyClick(root, Qt.Key_Escape)
        pump(300)
        self.assertEqual("home", bridge.page)
        bridge.setPage("automations")
        pump(300)
        self.click(root, "navigationTrigger")
        QTest.keyClick(root, Qt.Key_Escape)
        pump(250)
        self.assertFalse(root.property("navOpen"))
        self.assertEqual("automations", bridge.page)
        QTest.keyClick(root, Qt.Key_Escape)
        pump(300)
        self.assertEqual("home", bridge.page)

    def test_automations_creation_bounds_empty_and_populated_at_supported_sizes(self):
        from PySide6.QtCore import QPointF
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("automations")
        pump(300)
        viewport = self.item(root, "automationsViewport")
        button = self.item(root, "createJobButton")
        spec = self.item(root, "jobSpecification")
        for count in (0, 8):
            bridge._jobs = [dict(id=f"fixture-{i}", label=f"Display fixture {i}", kind="interval", readable=True, archived=False, enabled=False, revision=1, next_due_at_utc="") for i in range(count)]
            bridge.dataChanged.emit()
            for width, height in ((1920, 1080), (1280, 760), (1100, 700)):
                with self.subTest(jobs=count, size=(width, height)):
                    root.resize(width, height)
                    pump(700)   # listing / editor heights rebind after the resize
                    viewport.setProperty("contentY", 0)
                    self.assertGreaterEqual(spec.height(), 180)
                    self.assertGreater(button.height(), 30)
                    bottom = max(0, viewport.property("contentHeight") - viewport.height())
                    if width == 1920:
                        self.assertLessEqual(bottom, 1, "master layout must show the whole form without scrolling")
                    viewport.setProperty("contentY", bottom)
                    pump(120)
                    top_left = button.mapToItem(viewport, QPointF(0, 0))
                    bottom_right = button.mapToItem(viewport, QPointF(button.width(), button.height()))
                    self.assertGreaterEqual(top_left.y(), 0)
                    self.assertLessEqual(bottom_right.y(), viewport.height() - 16)
                    self.assertLessEqual(bottom_right.x(), viewport.width())
                    window_bottom = viewport.mapToScene(QPointF(viewport.width(), viewport.height()))
                    self.assertLessEqual(window_bottom.y(), height - 40)
                    self.assertTrue(button.isEnabled())
                    self.assertEqual(count == 0, self.item(root, "automationsEmpty").isVisible())
        self.assertEqual(0, self.execute_calls())


class StatusIndicatorTests(UiTestCase):
    item = PresentationTests.item
    window = PresentationTests.window

    def test_lamp_binds_to_real_execution_state_and_skin_persists(self) -> None:
        bridge = self.bridge()
        self.assertEqual("orb", bridge.statusIndicator)
        root = self.window(bridge)
        lamp = self.item(root, "statusOrb")
        self.assertTrue(lamp.property("lit"))
        self.assertEqual("orb", lamp.property("skin"))
        # The kill switch changes the persisted barrier; the lamp only follows it.
        bridge.setExecutionEnabled(False)
        pump(50)
        self.assertFalse(lamp.property("lit"))
        self.assertFalse(self.store.load().settings.execution_enabled)
        bridge.setExecutionEnabled(True)
        pump(50)
        self.assertTrue(lamp.property("lit"))
        # Separate hit targets: the lamp and the kill switch never overlap.
        from PySide6.QtCore import QPointF
        kill = self.item(root, "killSwitchControl")
        for width, height in ((1920, 1080), (1280, 760)):
            root.resize(width, height)
            pump(300)
            l0 = lamp.mapToScene(QPointF(0, 0)); l1 = lamp.mapToScene(QPointF(lamp.width(), lamp.height()))
            k0 = kill.mapToScene(QPointF(0, 0)); k1 = kill.mapToScene(QPointF(kill.width(), kill.height()))
            self.assertLessEqual(l1.x(), k0.x() + 1, f"lamp overlaps kill switch at {width}x{height}")
            self.assertLessEqual(l1.x(), width); self.assertLessEqual(k1.x(), width)
        # Skin choice persists, survives a fresh load, is reversible and never touches execution.
        bridge.setStatusIndicator("hud")
        pump(50)
        self.assertEqual("hud", lamp.property("skin"))
        self.assertTrue(lamp.property("lit"))
        self.assertEqual("hud", json.loads(self.prefs_path.read_text(encoding="utf-8"))["status_indicator"])
        self.assertEqual("hud", UiPreferences(self.prefs_path).data["status_indicator"])
        self.assertTrue(self.store.load().settings.execution_enabled)
        bridge.setStatusIndicator("neon")  # not a skin: ignored
        self.assertEqual("hud", bridge.statusIndicator)
        bridge.setStatusIndicator("orb")
        self.assertEqual("orb", UiPreferences(self.prefs_path).data["status_indicator"])
        # A document written before the lamp existed keeps its choices and gains the default.
        legacy = canonical_preferences(); del legacy["status_indicator"]; legacy["theme"] = "dark"
        self.assertEqual({**canonical_preferences(), "theme": "dark"}, validate_preferences(legacy))
        self.assertEqual(canonical_preferences(), validate_preferences({**canonical_preferences(), "status_indicator": "neon"}))
        self.assertEqual(0, self.execute_calls())


class MacroGeometryTests(UiTestCase):
    """The Canonical Space macro composition, normalized to the viewport.

    Targets measured from the approved 1536x864 reference; the same ratios
    must hold at every supported size. Major boundaries +-1.5 pp, the core
    centre +-1.0 pp.
    """
    item = PresentationTests.item

    REFERENCE = {
        "central left": 12.83, "central width": 56.70, "central right": 69.53,
        "column gap": 0.91, "stack left": 70.44, "stack width": 27.60, "stack right": 98.05,
        "panel start Y": 18.98, "central bottom Y": 95.14, "stack bottom Y": 92.13,
        "core centre X": 48.96, "core centre Y": 7.99,
    }

    def test_canonical_macro_composition_holds_at_every_supported_size(self) -> None:
        from PySide6.QtCore import QPointF
        bridge = self.bridge()
        root = self.load(bridge)
        root.showNormal()
        for width, height in ((1920, 1080), (1536, 864), (1280, 760)):
            root.resize(width, height)
            pump(760)   # the work surface width and the column reflow both animate
            self.assertEqual((width, height), (root.width(), root.height()))

            def box(name):
                it = self.item(root, name)
                a = it.mapToScene(QPointF(0, 0)); b = it.mapToScene(QPointF(it.width(), it.height()))
                return a.x(), a.y(), b.x(), b.y()

            work = box("canonicalWorkspace"); stack = box("panelColumn"); core = box("jarvisCore")
            measured = {
                "central left": 100 * work[0] / width,
                "central width": 100 * (work[2] - work[0]) / width,
                "central right": 100 * work[2] / width,
                "column gap": 100 * (stack[0] - work[2]) / width,
                "stack left": 100 * stack[0] / width,
                "stack width": 100 * (stack[2] - stack[0]) / width,
                "stack right": 100 * stack[2] / width,
                "panel start Y": 100 * work[1] / height,
                "central bottom Y": 100 * work[3] / height,
                "stack bottom Y": 100 * stack[3] / height,
                "core centre X": 100 * (core[0] + core[2]) / 2 / width,
                "core centre Y": 100 * (core[1] + core[3]) / 2 / height,
            }
            for key, target in self.REFERENCE.items():
                tolerance = 1.0 if key.startswith("core centre") else 1.5
                with self.subTest(size=(width, height), boundary=key):
                    self.assertAlmostEqual(target, measured[key], delta=tolerance,
                                           msg=f"{key} {measured[key]:.2f}% vs reference {target:.2f}%")
            with self.subTest(size=(width, height), boundary="no overlap"):
                # The work surface never reaches into the stack, and nothing leaves the viewport.
                self.assertLessEqual(work[2], stack[0])
                self.assertLessEqual(stack[2], width)
                self.assertLessEqual(work[3], height)
                self.assertGreater(work[2] - work[0], stack[2] - stack[0],
                                   "the central work surface must dominate the right stack")
        self.assertEqual(0, self.execute_calls())

    def test_command_preset_keeps_its_own_outer_geometry(self) -> None:
        from PySide6.QtCore import QPointF
        bridge = self.bridge()
        root = self.load(bridge)
        root.showNormal(); root.resize(1920, 1080); pump(300)
        canonical_left = self.item(root, "canonicalWorkspace").mapToScene(QPointF(0, 0)).x()
        bridge.setTheme("command")
        pump(600)
        # The command preset is a different layout family: it must not inherit
        # the canonical ambient rail inset.
        self.assertEqual("command", root.property("themeName"))
        bridge.setTheme("space")
        pump(600)
        self.assertAlmostEqual(canonical_left, self.item(root, "canonicalWorkspace").mapToScene(QPointF(0, 0)).x(), delta=1.0)


class CommandHeaderTests(UiTestCase):
    """The canonical header: the core dominates, the command strip is one
    right-anchored assembly in the required order, and the selector reads
    real provider state without inventing metadata."""

    item = PresentationTests.item
    click = PresentationTests.click
    window = PresentationTests.window

    def boxes(self, root, *names):
        from PySide6.QtCore import QPointF
        out = []
        for name in names:
            it = self.item(root, name)
            a = it.mapToScene(QPointF(0, 0)); b = it.mapToScene(QPointF(it.width(), it.height()))
            out.append((a.x(), a.y(), b.x(), b.y()))
        return out

    def test_header_composition_holds_and_the_core_dominates(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in ((1920, 1080), (1536, 864), (1280, 760)):
            root.resize(width, height)
            pump(600)
            core, strip, chip, lamp, kill, nav = self.boxes(
                root, "jarvisCore", "commandStrip", "modelTrigger", "statusOrb",
                "killSwitchControl", "navigationTrigger")
            with self.subTest(size=(width, height)):
                # The core is the dominant header element and keeps its centre.
                self.assertAlmostEqual(48.90, 100 * (core[0] + core[2]) / 2 / width, delta=1.0)
                self.assertGreater(core[2] - core[0], chip[2] - chip[0])
                self.assertGreater(core[3] - core[1], 3.5 * (chip[3] - chip[1]))
                self.assertGreater(core[2] - core[0], 3 * (lamp[2] - lamp[0]))
                self.assertGreater(core[2] - core[0], 3 * (kill[2] - kill[0]))
                # The strip is right-anchored, inside the viewport, and never
                # reaches into the core.
                self.assertLess(width - strip[2], 0.02 * width)
                self.assertLessEqual(strip[2], width)
                self.assertGreaterEqual(strip[0], core[2] - 1)
                # Required order, with separate hit targets throughout.
                order = [chip, lamp, kill, nav]
                for left, right in zip(order, order[1:]):
                    self.assertLessEqual(left[2], right[0] + 1)
                # Degradation never shrinks a control or the selector below a
                # usable width.
                self.assertGreaterEqual(chip[2] - chip[0], 188)
                self.assertGreaterEqual(lamp[2] - lamp[0], 56)
                self.assertGreaterEqual(kill[2] - kill[0], 44)
                self.assertGreaterEqual(nav[2] - nav[0], 34)
                # The core-side status group is present whenever it fits, and
                # is what gives way when it does not - not the controls.
                group = self.item(root, "coreStatusGroup")
                if group.isVisible():
                    g = self.boxes(root, "coreStatusGroup")[0]
                    self.assertGreaterEqual(g[0], core[2] - 1)
                    self.assertLessEqual(g[2], strip[0] + 1)
        self.assertEqual(0, self.execute_calls())

    def test_selector_shows_real_model_state_and_opens_its_own_panel(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        chip = self.item(root, "modelTrigger")
        # Nothing configured: the CLI's own default, never an invented name.
        self.assertFalse(bridge.config["claude_model"])
        self.assertEqual("CLI default", chip.property("modelName"))
        self.assertIn("ANTHROPIC", chip.property("metadata"))
        self.assertFalse(chip.property("open"))

        self.click(root, "modelTrigger")
        pump(300)
        self.assertTrue(root.property("modelOpen"))
        self.assertTrue(chip.property("open"))
        # The dropdown offers the real choices and routes through the one
        # authoritative call.
        self.click(root, "modelRowDefault")
        pump(200)
        self.assertFalse(self.store.load().settings.claude_model)

        QMetaObject.invokeMethod(self.item(root, "modelField"), "insert",
                                 Q_ARG(int, 0), Q_ARG(str, "claude-opus-5"))
        self.click(root, "applyModelButton")
        pump(250)
        self.assertEqual("claude-opus-5", self.store.load().settings.claude_model)
        self.assertEqual("claude-opus-5", chip.property("modelName"))
        self.assertEqual(0, self.execute_calls())

class LeftContextTests(UiTestCase):
    item = PresentationTests.item
    window = PresentationTests.window

    def test_clock_date_and_timezone_share_the_live_system_clock(self) -> None:
        from PySide6.QtCore import QObject, QDate, QDateTime, QTime
        root = self.window(self.bridge())
        source = root.findChild(QObject, "systemClock")
        context = self.item(root, "leftContextCard")
        self.assertIsNotNone(source)
        original = source.property("now")
        pump(1150)
        self.assertNotEqual(original, source.property("now"), "the system clock timer stopped")
        for date, clock in ((QDate(2032, 12, 31), QTime(23, 59, 59)),
                            (QDate(2033, 1, 1), QTime(0, 0, 0))):
            instant = QDateTime(date, clock)
            source.setProperty("now", instant)
            pump(20)
            self.assertEqual(instant.toString("HH:mm"), context.property("timeText"))
            self.assertEqual(instant.toString("ddd, d MMM"), context.property("dateText"))
            offset = instant.offsetFromUtc() // 60
            zone = f"UTC{'-' if offset < 0 else '+'}{abs(offset) // 60:02}:{abs(offset) % 60:02}"
            self.assertEqual(zone, context.property("zoneText"))
        self.assertEqual(0, self.execute_calls())

    def test_context_is_aligned_and_unclipped_at_supported_sizes(self) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QFontDatabase
        # Windows' offscreen plugin may expose no fonts at all and measure
        # missing-glyph boxes. Real ink bounds are additionally exercised on
        # the native Windows renderer; layout bounds remain portable.
        native_fonts = bool(QFontDatabase.families())
        root = self.window(self.bridge())
        context = self.item(root, "leftContextCard")
        for width, height in ((1920, 1080), (1536, 864), (1280, 760)):
            root.resize(width, height); pump(500)
            task = self.item(root, "railTasks")
            a = context.mapToScene(QPointF(0, 0))
            b = task.mapToScene(QPointF(0, 0))
            with self.subTest(size=(width, height)):
                self.assertTrue(context.isVisible())
                self.assertEqual(a.x(), b.x())
                self.assertEqual(context.width(), task.width())
                self.assertAlmostEqual(12, b.y() - a.y() - context.height(), delta=1)
                self.assertGreaterEqual(a.y(), 84)
                self.assertGreaterEqual(context.height(), 130)
                for name in ("contextClock", "contextDate", "contextTimeZone", "contextTemperature", "contextWeatherCondition"):
                    text = self.item(root, name)
                    pos = text.mapToItem(context, QPointF(0, 0))
                    self.assertGreaterEqual(pos.x(), 0); self.assertGreaterEqual(pos.y(), 0)
                    ink_width = text.property("contentWidth") if native_fonts else text.width()
                    self.assertLessEqual(pos.x() + ink_width, context.width(), name)
                    self.assertLessEqual(pos.y() + text.property("contentHeight"), context.height(), name)
                clock = self.item(root, "contextClock")
                weather = self.item(root, "contextWeather")
                end = clock.mapToItem(context, QPointF(clock.property("contentWidth") if native_fonts else clock.width(), 0)).x()
                self.assertLessEqual(end, weather.x())
        self.assertEqual(0, self.execute_calls())

    def test_weather_remains_explicitly_unavailable_without_provider_or_city(self) -> None:
        root = self.window(self.bridge())
        self.assertEqual("--°", self.item(root, "contextTemperature").property("text"))
        self.assertEqual("Not\nconnected", self.item(root, "contextWeatherCondition").property("text"))
        context = self.item(root, "leftContextCard")
        self.assertTrue(context.property("zoneText").startswith("UTC"))
        self.assertEqual(0, self.execute_calls())


class ConversationSurfaceTests(UiTestCase):
    """Phase 3: the summary modules leave the conversation for the left
    ambient rail, open anchored quick-look peeks, and the conversation and
    composer keep their place inside the Phase 1 work surface."""

    item = PresentationTests.item
    click = PresentationTests.click
    window = PresentationTests.window
    SUMMARY = ("Tasks", "Updates", "Focus", "Plan")

    def box(self, root, name):
        from PySide6.QtCore import QPointF
        it = self.item(root, name)
        a = it.mapToScene(QPointF(0, 0)); b = it.mapToScene(QPointF(it.width(), it.height()))
        return a.x(), a.y(), b.x(), b.y()

    def texts(self, node, acc):
        value = node.property("text")
        if isinstance(value, str) and value:
            acc.append(value)
        for child in node.childItems():
            self.texts(child, acc)
        return acc

    def test_summary_modules_live_in_the_rail_not_the_conversation(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        inside = self.texts(self.item(root, "canonicalWorkspace"), [])
        for title in self.SUMMARY + ("Automations", "Memory", "History"):
            self.assertNotIn(title, inside, f"{title} is still inside the conversation surface")
        for width, height in ((1920, 1080), (1536, 864), (1280, 760)):
            root.resize(width, height)
            pump(700)
            work = self.box(root, "canonicalWorkspace"); rail = self.box(root, "summaryRail")
            composer = self.box(root, "canonicalComposer")
            with self.subTest(size=(width, height)):
                previous_bottom = rail[1]
                for key in self.SUMMARY:
                    card = self.box(root, "rail" + key)
                    # Phase 3.3b: substantial modules within the frozen rail,
                    # not the former 76 px navigation-sized cards.
                    self.assertGreaterEqual(card[3] - card[1], 84)
                    if width == 1920:
                        self.assertGreaterEqual(card[2] - card[0], work[0] * 0.90)
                        self.assertGreaterEqual(card[3] - card[1], 108)
                        self.assertLessEqual(card[3] - card[1], 124)
                    if key != "Tasks":
                        self.assertGreaterEqual(card[1] - previous_bottom, 10)
                        self.assertLessEqual(card[1] - previous_bottom, 14)
                    # In order, inside the Phase 1 rail, never over the work surface.
                    self.assertGreaterEqual(card[1], previous_bottom - 1)
                    self.assertLessEqual(card[2], work[0])
                    self.assertGreaterEqual(card[0], 0)
                    self.assertLessEqual(card[3], height)
                    previous_bottom = card[3]
                # The composer stays inside the work surface and on screen.
                self.assertGreaterEqual(composer[0], work[0]); self.assertLessEqual(composer[2], work[2])
                self.assertLessEqual(composer[3], work[3]); self.assertLessEqual(work[3], height)
        # Ambient, not navigation: the rail belongs to the Command Center only.
        bridge.setPage("tasks"); pump(400)
        self.assertFalse(self.item(root, "summaryRail").isVisible())
        bridge.setPage("home"); pump(400)
        self.assertTrue(self.item(root, "summaryRail").isVisible())
        self.assertEqual(0, self.execute_calls())

    def test_conversation_surface_holds_the_reference_proportions(self) -> None:
        """Phase 3.2: inside the fixed Phase 1 bounds the conversation takes the
        approved helper's measured proportions - composer 94 % of the surface
        and centred, the avatar column centred at 8.2 %, and the reference
        layout at both desktop sizes."""
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in ((1920, 1080), (1536, 864)):
            root.resize(width, height)
            pump(700)
            work = self.box(root, "canonicalWorkspace"); composer = self.box(root, "canonicalComposer")
            feed = self.box(root, "conversationFeed")
            surface = work[2] - work[0]
            with self.subTest(size=(width, height)):
                self.assertTrue(self.item(root, "conversationFeed").property("referenceLayout"))
                self.assertAlmostEqual(94.0, 100 * (composer[2] - composer[0]) / surface, delta=1.0)
                self.assertAlmostEqual(composer[0] - work[0], work[2] - composer[2], delta=2.0)
                avatar_centre = feed[0] + self.item(root, "conversationFeed").property("avatarSize") / 2
                self.assertAlmostEqual(8.2, 100 * (avatar_centre - work[0]) / surface, delta=1.0)
                self.assertLessEqual(composer[3], work[3])
        self.assertEqual(0, self.execute_calls())

    def test_peek_opens_anchored_switches_and_closes_every_way(self) -> None:
        from PySide6.QtCore import QPoint
        bridge = self.bridge()
        root = self.window(bridge)
        pump(700)   # the work surface width animates in; read it settled
        work = self.box(root, "canonicalWorkspace")
        composer = self.box(root, "canonicalComposer")

        self.click(root, "railTasks"); pump(300)
        self.assertTrue(root.property("peekOpen")); self.assertEqual("Tasks", root.property("peekKey"))
        self.assertTrue(self.item(root, "railTasks").property("active"))
        peek = self.box(root, "summaryPeek"); rail = self.box(root, "summaryRail")
        self.assertGreaterEqual(peek[0], rail[2], "the peek opens to the right of the rail")
        # Quick look, not a reflow: the conversation does not move.
        self.assertEqual(work, self.box(root, "canonicalWorkspace"))
        self.assertEqual(composer, self.box(root, "canonicalComposer"))

        # Another card switches in place; exactly one card is active.
        self.click(root, "railUpdates"); pump(300)
        self.assertTrue(root.property("peekOpen")); self.assertEqual("Updates", root.property("peekKey"))
        self.assertEqual(["Updates"], [k for k in self.SUMMARY if self.item(root, "rail" + k).property("active")])
        # The same card again closes it.
        self.click(root, "railUpdates"); pump(300)
        self.assertFalse(root.property("peekOpen"))
        # X, Escape and an outside click close it too.
        self.click(root, "railFocus"); pump(300)
        self.click(root, "summaryPeekClose"); pump(300)
        self.assertFalse(root.property("peekOpen"))
        self.click(root, "railPlan"); pump(300)
        QTest.keyClick(root, Qt.Key_Escape); pump(300)
        self.assertFalse(root.property("peekOpen"))
        self.click(root, "railTasks"); pump(300)
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, QPoint(1100, 620)); pump(300)
        self.assertFalse(root.property("peekOpen"))
        # Only one context at a time: a peek closes the model panel.
        self.click(root, "modelTrigger"); pump(300)
        self.assertTrue(root.property("modelOpen"))
        self.click(root, "railTasks"); pump(300)
        self.assertTrue(root.property("peekOpen")); self.assertFalse(root.property("modelOpen"))
        # The deep action is the existing full page.
        self.click(root, "summaryPeekAction"); pump(500)
        self.assertEqual("tasks", bridge.page); self.assertFalse(root.property("peekOpen"))
        self.assertEqual(0, self.execute_calls())

    def test_peek_stays_inside_the_viewport_and_shows_only_real_state(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for width, height in ((1920, 1080), (1536, 864), (1280, 760)):
            root.resize(width, height)
            pump(600)
            for key in self.SUMMARY:
                with self.subTest(size=(width, height), module=key):
                    self.click(root, "rail" + key); pump(300)
                    peek = self.box(root, "summaryPeek"); close = self.box(root, "summaryPeekClose")
                    self.assertGreaterEqual(peek[0], 0); self.assertGreaterEqual(peek[1], 0)
                    self.assertLessEqual(peek[2], width); self.assertLessEqual(peek[3], height)
                    self.assertLessEqual(close[2], width); self.assertGreaterEqual(close[1], 0)
                    self.click(root, "rail" + key); pump(300)
                    self.assertFalse(root.property("peekOpen"))
        # An empty installation: no tasks, projects or jobs exist, so those
        # peeks show their truthful empty state rather than invented rows.
        root.resize(1920, 1080); pump(400)
        for key in ("Tasks", "Focus", "Plan"):
            self.click(root, "rail" + key); pump(300)
            self.assertTrue(self.item(root, "summaryPeekEmpty").isVisible(), f"{key} should be empty")
            self.click(root, "rail" + key); pump(300)
        # Updates is real too: this session's own activity note is listed.
        self.click(root, "railUpdates"); pump(300)
        self.assertFalse(self.item(root, "summaryPeekEmpty").isVisible())
        self.assertIn("JARVIS interface started", self.texts(self.item(root, "summaryPeek"), []))
        self.assertEqual(0, self.execute_calls())

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
