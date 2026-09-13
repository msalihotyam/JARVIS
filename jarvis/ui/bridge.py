"""The Python <-> QML bridge: one QObject, properties in, slots out.

The bridge owns UI STATE only. Every action goes to ``Backend`` (which goes
to ``JarvisApp``), off the GUI thread through a bounded single-worker pool,
and every result is applied only if it still belongs to the newest
operation of its kind - a stale worker completion can never overwrite a
newer task, a stale clarification can never bind to another request, and a
stale approval ticket is dropped the moment a newer envelope arrives.

Nothing here decides authority: the backend re-proves every approval,
selection and cancellation it is handed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import Property, QObject, QThreadPool, Signal, Slot

from jarvis.ui.backend import ApprovalTicket, Backend, approval_ticket, redact_text
from jarvis.ui.preferences import (
    CONTEXTUAL_PANELS,
    PAGES,
    PANEL_IDS,
    PROFILES,
    THEMES,
    UiPreferences,
    STATUS_INDICATORS,
)
from jarvis.ui.present import (
    TERMINAL_STATUSES,
    approval_card,
    error_entries,
    outcome_kind,
    rows,
    run_entries,
    show_entries,
    task_status,
    text_entries,
)
from jarvis.settings import CLAUDE_EFFORT_VALUES

FEED_LIMIT = 200
ACTIVITY_LIMIT = 50
CORE_STATES = ("idle", "running", "awaiting_approval", "awaiting_input", "completed",
               "error", "unknown", "disabled")


class _Emitter(QObject):
    """Lives on the GUI thread; the worker emits through it (queued)."""

    finished = Signal(str, int, object)


def _guarded(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the worker never dies silently
        return {"command": "worker", "error": {
            "code": "connector_failed", "message": f"Unexpected internal failure: {type(exc).__name__}",
        }}


class Bridge(QObject):
    """UI state and actions for QML. ``sync=True`` runs work inline (tests)."""

    stateChanged = Signal()
    feedChanged = Signal()
    dataChanged = Signal()
    prefsChanged = Signal()
    approvalChanged = Signal()
    quitRequested = Signal()
    # UI events the core reacts to: they report what the application
    # actually did, never a new backend state.
    inputAccepted = Signal()      # a message or choice was accepted and sent on
    modelApplied = Signal()       # the configured model / effort really changed
    composingChanged = Signal()

    def __init__(
        self, backend: Backend, preferences: UiPreferences, *, sync: bool = False, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.prefs = preferences
        self.sync = sync
        self.started_at = time.monotonic()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._emitter = _Emitter(self)
        self._emitter.finished.connect(self._on_finished)
        self._sequence: dict[str, int] = {}
        self._handlers: dict[tuple[str, int], Callable[[Any], None]] = {}
        self._pending_jobs: dict[tuple[str, int], Callable[[], None]] = {}
        # -- state --
        self._busy = False
        self._executing = False    # the current / last task run is an approved execution
        self._composing = False
        self._core_state = "idle"
        self._session_id = ""
        self._task: dict[str, Any] = {}
        self._feed: list[dict[str, str]] = []
        self._activity: list[dict[str, str]] = []
        self._approval: dict[str, Any] = {}
        self._ticket: ApprovalTicket | None = None
        self._clarification: dict[str, Any] = {}
        self._request_id = ""
        self._config: dict[str, Any] = {}
        self._status: dict[str, Any] = {}
        self._sessions: list[dict] = []
        self._tasks: list[dict] = []
        self._task_detail: list[dict] = []
        self._jobs: list[dict] = []
        self._memory: list[dict] = []
        self._projects: list[str] = []
        self._focus_mode = False
        self._edit_mode = False
        self._last_error = ""

    # -- worker --------------------------------------------------------------

    def _run(self, kind: str, fn: Callable[[], Any], on_done: Callable[[Any], None],
             executing: bool = False) -> None:
        sequence = self._sequence.get(kind, 0) + 1
        self._sequence[kind] = sequence
        self._handlers[(kind, sequence)] = on_done
        if kind == "task":
            self._busy = True
            self._executing = executing
            self._refresh_core_state()
        if self.sync:
            self._on_finished(kind, sequence, _guarded(fn))
            return
        emitter = self._emitter

        def job() -> None:
            emitter.finished.emit(kind, sequence, _guarded(fn))

        # The pool runs the callable on its one worker thread and the result
        # crosses back through the emitter's queued signal. The reference is
        # kept until completion: the pool does not own a Python callable.
        self._pending_jobs[(kind, sequence)] = job
        self._pool.start(job)

    def _on_finished(self, kind: str, sequence: int, result: Any) -> None:
        self._pending_jobs.pop((kind, sequence), None)
        handler = self._handlers.pop((kind, sequence), None)
        if handler is None or self._sequence.get(kind) != sequence:
            # A newer operation of this kind superseded it: the result is
            # dropped whole. Nothing stale ever reaches the screen.
            if kind == "task" and self._sequence.get(kind) == sequence:
                self._busy = False
            return
        if kind == "task":
            self._busy = False
        handler(result)
        self._refresh_core_state()

    # -- feed ---------------------------------------------------------------

    def _say(self, kind: str, text: str) -> None:
        self._feed.append({"kind": kind, "text": redact_text(str(text)),
                           "time": datetime.now().strftime("%I:%M %p").lstrip("0")})
        del self._feed[:-FEED_LIMIT]
        self.feedChanged.emit()

    def _note(self, text: str, kind: str = "system") -> None:
        self._activity.insert(0, {"kind": kind, "text": redact_text(str(text)),
                                  "time": datetime.now().strftime("%H:%M:%S"), "ts": time.time()})
        del self._activity[ACTIVITY_LIMIT:]
        self.dataChanged.emit()

    # -- core state ----------------------------------------------------------

    def _refresh_core_state(self) -> None:
        if self._config_refused() or self._config.get("execution_enabled") is False:
            state = "disabled"
        elif self._busy:
            state = "running"
        elif self._ticket is not None:
            state = "awaiting_approval"
        elif self._clarification or self._task.get("status") == "awaiting_clarification":
            state = "awaiting_input"
        else:
            state = self._last_outcome
        if state != self._core_state:
            self._core_state = state
        self.stateChanged.emit()

    _last_outcome = "idle"

    def _config_refused(self) -> bool:
        return self._config.get("config_status") not in ("valid", "absent")

    # -- envelopes -----------------------------------------------------------

    def _apply_run(self, payload: Any, intent: Any = None) -> None:
        """Render one task envelope: replaces the task block and the approval."""
        for entry in run_entries(payload):
            self._say(entry["kind"], entry["text"])
        if isinstance(payload, dict):
            session_id = (payload.get("session") or {}).get("id") if isinstance(payload.get("session"), dict) else None
            if isinstance(session_id, str):
                self._session_id = session_id
            if isinstance(payload.get("task"), dict) and isinstance(payload["task"].get("id"), str):
                self._task = task_status(payload, intent)
        self._ticket = approval_ticket(payload)
        self._approval = approval_card(payload) if self._ticket is not None else {}
        self._last_outcome = outcome_kind(payload)
        if self._ticket is not None:
            self._note(f"Approval required: {self._approval.get('action')} -> {self._approval.get('destination')}", "approval")
        elif self._last_outcome == "unknown":
            self._note(f"OUTCOME UNKNOWN for task {self._task.get('task_id')}", "unknown")
        elif self._task:
            self._note(f"Task {self._task.get('task_id')[:8]} {self._task.get('run_status')}", "system")
        self.approvalChanged.emit()
        self.dataChanged.emit()

    def _apply_text(self, payload: Any, text: str) -> None:
        intent = payload.get("intent") if isinstance(payload, dict) else None
        if not isinstance(intent, dict):
            intent = None
        status = str((intent or {}).get("status") or "")
        for entry in text_entries(payload):
            self._say(entry["kind"], entry["text"])
        self._clarification = {}
        if status == "structured":
            self._apply_run(payload.get("run"), intent)
            return
        self._ticket, self._approval = None, {}
        if status == "clarification_required" and isinstance(intent, dict):
            choices = [c for c in (intent.get("choices") or []) if isinstance(c, dict)
                       and isinstance(c.get("label"), str) and isinstance(c.get("destination"), str)]
            self._clarification = {
                "request_id": str(intent.get("request_id") or ""),
                "previous_text": text,
                "question": str(intent.get("question") or ""),
                "choices": choices,
            }
        elif status == "scheduled":
            self._note(f"Scheduled job {((payload.get('job') or {}).get('label'))}", "scheduled")
            self._run("jobs", lambda: self.backend.jobs(False), self._set_jobs)
        else:
            self._note(f"Intent {status}", "error" if status != "scheduled" else "system")
        self._task = task_status(None, intent) if status != "structured" else self._task
        self._last_outcome = outcome_kind(None, status)
        self.approvalChanged.emit()
        self.dataChanged.emit()

    # -- properties: state ---------------------------------------------------

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=stateChanged)
    def coreState(self) -> str:
        return self._core_state

    @Property(bool, notify=stateChanged)
    def executing(self) -> bool:
        """The current (or most recent) task run is an approved execution or a
        resume - as opposed to understanding a request, which may or may not
        execute on its own. Read together with ``coreState``."""
        return self._executing

    @Property(bool, notify=composingChanged)
    def composing(self) -> bool:
        """The composer is being typed in right now (UI-only, never persisted)."""
        return self._composing

    @Slot(bool)
    def setComposing(self, composing: bool) -> None:
        if bool(composing) != self._composing:
            self._composing = bool(composing)
            self.composingChanged.emit()

    @Property(bool, notify=stateChanged)
    def executionEnabled(self) -> bool:
        return self._config.get("execution_enabled") is True and not self._config_refused()

    @Property(bool, notify=stateChanged)
    def configRefused(self) -> bool:
        return self._config_refused()

    @Property("QVariantMap", notify=stateChanged)
    def config(self) -> dict:
        return dict(self._config)

    @Property("QVariantMap", notify=stateChanged)
    def statusInfo(self) -> dict:
        return dict(self._status)

    @Property(str, notify=stateChanged)
    def sessionId(self) -> str:
        return self._session_id

    @Property("QVariantMap", notify=stateChanged)
    def task(self) -> dict:
        return dict(self._task)

    @Property(bool, notify=stateChanged)
    def taskLive(self) -> bool:
        return bool(self._session_id) and (self._busy or (
            bool(self._task.get("status")) and self._task.get("status") not in TERMINAL_STATUSES))

    @Property(int, notify=stateChanged)
    def uptimeSeconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    @Property(str, constant=True)
    def operatorName(self) -> str:
        """The person this installation greets. Display only; JARVIS_OPERATOR_NAME overrides."""
        return os.environ.get("JARVIS_OPERATOR_NAME", "").strip() or "Salih"

    # -- development-only reconstruction aids (absent unless the environment
    #    asks for them; nothing here touches authority or persisted state) ----

    @Property(str, constant=True)
    def devReference(self) -> str:
        """file:// URL of the canonical reference image for the DEV overlay, or ""."""
        path = os.environ.get("JARVIS_UI_DEV_REFERENCE", "").strip()
        return Path(path).resolve().as_uri() if path and Path(path).is_file() else ""

    @Property("QVariantList", constant=True)
    def devBackgroundCandidates(self) -> list:
        """file:// URLs of the DEV background candidates, in order, when
        JARVIS_UI_BACKGROUND_AUDITION=1; otherwise empty (audition off)."""
        if os.environ.get("JARVIS_UI_BACKGROUND_AUDITION", "") != "1":
            return []
        folder = Path(__file__).resolve().parents[2] / "dev" / "background-candidates"
        return [p.resolve().as_uri() for p in (folder / f"candidate-{i}.webp" for i in range(1, 5)) if p.is_file()]

    @Property(bool, constant=True)
    def devFixture(self) -> bool:
        """True when the DEV visual fixture (sample feed / activity text) is active."""
        return os.environ.get("JARVIS_UI_DEV_FIXTURE", "") == "1"

    def _seed_dev_fixture(self) -> None:
        """Display-only sample conversation for side-by-side reconstruction
        against the reference. Feed and activity text only: no task, no
        ticket, no config, no persistence. The UI shows a DEV FIXTURE badge."""
        self._say("user", "Can you review my open tasks, check for any important updates, and help me plan the next steps for today?")
        self._say("jarvis", "I’ll review your open tasks, check for relevant updates across your tools, and create a focused plan for today.")
        self._say("jarvis", "Here’s what I found and a recommended plan for today. This takes into account your tasks, recent updates, and key priorities.")
        for kind, text in (("system", "3 relevant documents found"), ("scheduled", "Calendar update"),
                           ("system", "Claude Opus 5 ready"), ("system", "New research insight available")):
            self._note(text, kind)

    @Property("QVariantList", notify=stateChanged)
    def effortChoices(self) -> list:
        return list(CLAUDE_EFFORT_VALUES)

    @Property("QVariantList", notify=feedChanged)
    def feed(self) -> list:
        return list(self._feed)

    @Property("QVariantList", notify=dataChanged)
    def activity(self) -> list:
        return list(self._activity)

    @Property("QVariantMap", notify=approvalChanged)
    def approval(self) -> dict:
        return dict(self._approval)

    @Property(bool, notify=approvalChanged)
    def approvalPending(self) -> bool:
        return self._ticket is not None

    @Property("QVariantMap", notify=approvalChanged)
    def clarification(self) -> dict:
        return dict(self._clarification)

    @Property("QVariantList", notify=dataChanged)
    def sessions(self) -> list:
        return list(self._sessions)

    @Property("QVariantList", notify=dataChanged)
    def tasks(self) -> list:
        return list(self._tasks)

    @Property("QVariantList", notify=dataChanged)
    def taskDetail(self) -> list:
        return list(self._task_detail)

    @Property("QVariantList", notify=dataChanged)
    def jobs(self) -> list:
        return list(self._jobs)

    @Property("QVariantList", notify=dataChanged)
    def memoryItems(self) -> list:
        return list(self._memory)

    @Property("QVariantList", notify=dataChanged)
    def projects(self) -> list:
        return list(self._projects)

    # -- slots: natural language ------------------------------------------

    @Slot(str)
    def submitText(self, text: str) -> None:
        text = str(text)
        if not text.strip() or self._busy:
            return
        if self._ticket is not None:
            self._say("system", "Answer the pending approval first (Approve, Deny or Cancel).")
            return
        self._clarification = {}
        self.approvalChanged.emit()
        self._say("user", text)
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        if self._task.get("status") == "awaiting_clarification" and session_id and self._task.get("task_id"):
            task_id = self._task["task_id"]
            self._run("task", lambda: self.backend.clarify(session_id, task_id, text), self._apply_run)
            return
        self._task = {}
        self._run(
            "task",
            lambda: self.backend.submit_text(text, session_id, request_id=request_id),
            lambda payload: self._apply_text(payload, text),
        )

    @Slot(str)
    def chooseOption(self, label: str) -> None:
        """Send back EXACTLY one backend-offered label; the backend re-proves it."""
        clarification = self._clarification
        if self._busy or not clarification:
            return
        if not any(choice.get("label") == label for choice in clarification["choices"]):
            return
        previous = clarification["previous_text"]
        self._clarification = {}
        self.approvalChanged.emit()
        self._say("user", label)
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        self._run(
            "task",
            lambda: self.backend.submit_text(label, session_id, previous_text=previous, request_id=request_id),
            lambda payload: self._apply_text(payload, label),
        )

    @Slot()
    def dismissClarification(self) -> None:
        self._clarification = {}
        self.approvalChanged.emit()
        self._refresh_core_state()

    @Slot(result=bool)
    def cancelText(self) -> bool:
        """Stop the running understanding call for the CURRENT request only."""
        if not self._busy or not self._request_id:
            return False
        return self.backend.cancel_text(self._request_id)

    # -- slots: approval / task ------------------------------------------

    def _decide(self, verb: Callable[[ApprovalTicket], dict], executing: bool = False) -> None:
        ticket = self._ticket
        if ticket is None or self._busy:
            return
        self._ticket, self._approval = None, {}
        self.approvalChanged.emit()
        self._run("task", lambda: verb(ticket), self._apply_run, executing=executing)

    @Slot()
    def approve(self) -> None:
        self._decide(self.backend.approve, executing=True)

    @Slot()
    def deny(self) -> None:
        self._decide(self.backend.reject)

    @Slot()
    def cancelTask(self) -> None:
        """Cancel through Core; while running the id is storage's, not ours."""
        session_id = self._session_id
        task_id = (self.backend.active_task_id(session_id) if self._busy and session_id
                   else self._task.get("task_id"))
        if not session_id or not task_id:
            self._say("system", "No cancellable task is registered yet.")
            return
        self._ticket, self._approval = None, {}
        self.approvalChanged.emit()
        self._run("cancel", lambda: self.backend.cancel(session_id, task_id), self._apply_run)

    @Slot()
    def newSession(self) -> None:
        if self._busy:
            return
        self._session_id, self._task, self._ticket, self._approval, self._clarification = "", {}, None, {}, {}
        self._last_outcome = "idle"
        self._say("system", "New session: the next message starts a fresh session.")
        self.approvalChanged.emit()
        self._refresh_core_state()

    @Slot(str)
    def useSession(self, session_id: str) -> None:
        if session_id and not self._busy:
            self._session_id, self._task, self._ticket, self._approval = str(session_id), {}, None, {}
            self._say("system", f"Using session {session_id}.")
            self.approvalChanged.emit()
            self._refresh_core_state()

    @Slot(str, str)
    def resumeTask(self, session_id: str, task_id: str) -> None:
        if self._busy:
            return
        self._session_id = str(session_id)
        self._run("task", lambda: self.backend.resume(str(session_id), str(task_id)), self._apply_run, executing=True)

    # -- slots: read-only data ---------------------------------------------

    @Slot()
    def refreshConfig(self) -> None:
        self._config = self.backend.config()
        self._status = self.backend.status()
        self._refresh_core_state()

    @Slot()
    def refreshHistory(self) -> None:
        self._run("sessions", self.backend.sessions, self._set_sessions)

    def _set_sessions(self, payload: Any) -> None:
        self._sessions = rows(payload, "sessions", "session_id")
        if isinstance(payload, dict) and payload.get("error") is not None:
            self._last_error = str(payload["error"].get("message"))
        self.dataChanged.emit()

    @Slot(str)
    def loadTasks(self, session_id: str) -> None:
        self._run("tasks", lambda: self.backend.tasks(str(session_id)), self._set_tasks)

    def _set_tasks(self, payload: Any) -> None:
        self._tasks = rows(payload, "tasks", "task_id")
        self.dataChanged.emit()

    @Slot(str, str)
    def showTask(self, session_id: str, task_id: str) -> None:
        self._run("show", lambda: self.backend.show(str(session_id), str(task_id)), self._set_detail)

    def _set_detail(self, payload: Any) -> None:
        self._task_detail = show_entries(payload)
        self.dataChanged.emit()

    @Slot(bool)
    def refreshJobs(self, include_archived: bool) -> None:
        self._run("jobs", lambda: self.backend.jobs(bool(include_archived)), self._set_jobs)

    def _set_jobs(self, payload: Any) -> None:
        self._jobs = rows(payload, "jobs", "id")
        if isinstance(payload, dict) and payload.get("error") is not None:
            for entry in error_entries(payload["error"]):
                self._say(entry["kind"], entry["text"])
        self.dataChanged.emit()

    def _job_result(self, payload: Any) -> None:
        for entry in run_entries(payload):
            self._say(entry["kind"], entry["text"])
        self._run("jobs", lambda: self.backend.jobs(False), self._set_jobs)

    @Slot(str, int, bool)
    def setJobEnabled(self, job_id: str, revision: int, enabled: bool) -> None:
        self._run("job", lambda: self.backend.job_set_enabled(str(job_id), int(revision), bool(enabled)), self._job_result)

    @Slot(str, int)
    def archiveJob(self, job_id: str, revision: int) -> None:
        self._run("job", lambda: self.backend.job_archive(str(job_id), int(revision)), self._job_result)

    @Slot(str)
    def createJob(self, spec: str) -> None:
        if str(spec).strip():
            self._run("job", lambda: self.backend.job_create(str(spec)), self._job_result)

    @Slot()
    def runSweep(self) -> None:
        if self._busy:
            return
        self._say("system", "Running one scheduler sweep...")
        self._run("task", self.backend.scheduler_once, self._apply_run)

    @Slot(str, bool)
    def refreshMemory(self, project_id: str, include_inactive: bool) -> None:
        self._projects = self.backend.projects()
        project = str(project_id) if project_id in self._projects else (self._projects[0] if self._projects else "")
        if not project:
            self._memory = []
            self.dataChanged.emit()
            return
        self._run("memory", lambda: self.backend.memory_list(project, bool(include_inactive)), self._set_memory)

    def _set_memory(self, payload: Any) -> None:
        self._memory = [
            {**item, "text": redact_text(str(item.get("text")))} for item in rows(payload, "items", "id")
        ]
        if isinstance(payload, dict) and payload.get("error") is not None:
            for entry in error_entries(payload["error"]):
                self._say(entry["kind"], entry["text"])
        self.dataChanged.emit()

    # -- slots: configuration ------------------------------------------------

    def _apply_config(self, payload: dict) -> None:
        if payload.get("error") is not None:
            for entry in error_entries(payload["error"]):
                self._say(entry["kind"], entry["text"])
            payload = self.backend.config()
        self._config = payload
        self._status = self.backend.status()
        self._refresh_core_state()

    @Slot(bool)
    def setExecutionEnabled(self, enabled: bool) -> None:
        """THE kill switch: the persisted ``execution_enabled`` barrier."""
        payload = self.backend.set_config("execution_enabled", "true" if enabled else "false")
        self._say("system", "Execution re-enabled." if enabled else
                  "KILL SWITCH ON: no new work may start. Running work is stopped with Cancel.")
        self._note("Execution enabled" if enabled else "KILL SWITCH engaged", "system")
        self._apply_config(payload)

    @Slot(str, str)
    def applyModel(self, model: str, effort: str) -> None:
        before = (self._config.get("claude_model"), self._config.get("claude_effort"))
        payload: dict = {}
        for key, value in (("claude_model", str(model).strip()), ("claude_effort", str(effort).strip())):
            payload = self.backend.reset_config(key) if not value else self.backend.set_config(key, value)
            if payload.get("error") is not None:
                break
        self._apply_config(payload)
        if (self._config.get("claude_model"), self._config.get("claude_effort")) != before:
            self.modelApplied.emit()

    # -- preferences / layout ------------------------------------------------

    @Property(str, notify=prefsChanged)
    def theme(self) -> str:
        return self.prefs.data["theme"]

    @Slot(str)
    def setTheme(self, theme: str) -> None:
        if theme in THEMES and theme != self.prefs.data["theme"]:
            self.prefs.data["theme"] = theme
            self.prefs.save()
            self.prefsChanged.emit()

    @Property(bool, notify=prefsChanged)
    def ambientEffects(self) -> bool:
        return self.prefs.data["ambient_effects"]

    @Slot(bool)
    def setAmbientEffects(self, enabled: bool) -> None:
        self.prefs.data["ambient_effects"] = bool(enabled)
        self.prefs.save()
        self.prefsChanged.emit()

    @Property(str, notify=prefsChanged)
    def statusIndicator(self) -> str:
        """Skin of the execution-status lamp. Presentation only; never authority."""
        return self.prefs.data["status_indicator"]

    @Slot(str)
    def setStatusIndicator(self, skin: str) -> None:
        if skin in STATUS_INDICATORS:
            self.prefs.data["status_indicator"] = skin
            self.prefs.save()
            self.prefsChanged.emit()

    @Property(str, notify=prefsChanged)
    def profile(self) -> str:
        return self.prefs.package()["profile"]

    @Slot(str)
    def setProfile(self, profile: str) -> None:
        if profile in PROFILES:
            self.prefs.package()["profile"] = profile
            self.prefs.save()
            self.prefsChanged.emit()

    @Property(str, notify=prefsChanged)
    def page(self) -> str:
        return self.prefs.data["page"]

    @Slot(str)
    def setPage(self, page: str) -> None:
        if page in PAGES:
            self.prefs.data["page"] = page
            self.prefs.save()
            self.prefsChanged.emit()

    @Property(bool, notify=prefsChanged)
    def focusMode(self) -> bool:
        return self._focus_mode

    @Slot(bool)
    def setFocusMode(self, enabled: bool) -> None:
        # Temporary: nothing is written, so leaving it restores exactly.
        self._focus_mode = bool(enabled)
        if self._focus_mode:
            self._edit_mode = False
        self.prefsChanged.emit()

    @Property(bool, notify=prefsChanged)
    def editMode(self) -> bool:
        return self._edit_mode

    @Slot(bool)
    def setEditMode(self, enabled: bool) -> None:
        self._edit_mode = bool(enabled) and not self._focus_mode
        self.prefsChanged.emit()

    @Property(float, notify=prefsChanged)
    def columnWidth(self) -> float:
        return float(self.prefs.layout()["column_width"])

    @Property("QVariantList", notify=prefsChanged)
    def panels(self) -> list:
        """The stack as rendered: order, shares, flags, and whether each shows."""
        layout = self.prefs.layout()
        result = []
        for name in layout["order"]:
            panel = layout["panels"][name]
            contextual = name in CONTEXTUAL_PANELS
            has_content = (name != "workflow") or bool(self._task) or self._busy or self._ticket is not None
            shown = panel["visible"] and (panel["pinned"] or has_content or self._edit_mode)
            if self._focus_mode and not panel["pinned"]:
                shown = False
            result.append({"id": name, "share": panel["share"], "visible": panel["visible"],
                           "pinned": panel["pinned"], "contextual": contextual, "shown": shown})
        return result

    @Property("QVariantList", notify=prefsChanged)
    def hiddenPanels(self) -> list:
        layout = self.prefs.layout()
        return [name for name in layout["order"] if not layout["panels"][name]["visible"]]

    def _layout_changed(self) -> None:
        self.prefs.save()
        self.prefsChanged.emit()

    @Slot(str, bool)
    def setPanelVisible(self, panel: str, visible: bool) -> None:
        self.prefs.set_visible(str(panel), bool(visible))
        self._layout_changed()

    @Slot(str, bool)
    def setPanelPinned(self, panel: str, pinned: bool) -> None:
        self.prefs.set_pinned(str(panel), bool(pinned))
        self._layout_changed()

    @Slot(str, int)
    def movePanel(self, panel: str, delta: int) -> None:
        self.prefs.move(str(panel), int(delta))
        self._layout_changed()

    @Slot(str, float)
    def setPanelShare(self, panel: str, share: float) -> None:
        self.prefs.set_share(str(panel), float(share))
        self._layout_changed()

    @Slot(float)
    def setColumnWidth(self, width: float) -> None:
        self.prefs.set_column_width(float(width))
        self._layout_changed()

    @Slot()
    def resetLayout(self) -> None:
        self.prefs.reset_layout()
        self._layout_changed()

    # -- lifecycle ------------------------------------------------------------

    @Slot()
    def start(self) -> None:
        """Initial loads; called once after the QML root exists."""
        self.refreshConfig()
        self.refreshHistory()
        self.refreshJobs(False)
        self.refreshMemory("", False)
        self._note("JARVIS interface started", "system")
        if self.devFixture:
            self._seed_dev_fixture()

    @Slot()
    def waitIdle(self) -> None:
        """Block until the worker pool drains (tests only)."""
        self._pool.waitForDone(30_000)
