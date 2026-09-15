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

from PySide6.QtCore import Property, QDateTime, QLocale, QObject, QThreadPool, Signal, Slot

from jarvis.ui.backend import ApprovalTicket, Backend, approval_ticket, redact_text
from jarvis.ui.i18n import LANGUAGES, QT_TRANSLATE_NOOP, render, tx
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
    REFUSED_CODES,
    TERMINAL_STATUSES,
    approval_card,
    error_entries,
    outcome_kind,
    rows,
    run_entries,
    show_entries,
    task_outcome,
    task_progress,
    task_status,
    text_entries,
)
from jarvis.settings import CLAUDE_EFFORT_VALUES

FEED_LIMIT = 200
ACTIVITY_LIMIT = 50
CORE_STATES = ("idle", "running", "awaiting_approval", "awaiting_input", "completed",
               "error", "unknown", "disabled")
#: DEV FIXTURE only (JARVIS_UI_DEV_FIXTURE=1): display-only safety cards for
#: visual review. They never create a ticket, a task or a decision.
DEV_SAFETY_SCENARIOS = ("", "approval", "approval_incomplete", "unknown", "failed", "blocked", "completed")


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
    attentionChanged = Signal()   # the safety projection may have changed

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
        # The running request began while frozen: it can start no work (Core
        # refuses anything authoritative), so nothing is on its way to a safe point.
        self._started_frozen = False
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
        self._layout_save = ""     # "", "saved" or "failed": this session's last layout write
        self._last_error = ""
        # The operator's last approval decision, as Core answered it ({} when none).
        self._decision: dict[str, str] = {}
        # "", "disable_requested", "enable_requested" or "unconfirmed": an
        # execution change the UI asked for that Core has not (yet) confirmed.
        self._execution_request = ""
        self._freeze_waiting = False      # Freeze confirmed while an operation was still running
        self._quitting = False            # the one orderly quit is under way
        self._tray_available = False      # set by the application when a tray icon exists
        self._dev_safety = ""
        for signal in (self.stateChanged, self.approvalChanged, self.dataChanged):
            signal.connect(self.attentionChanged)

    # -- worker --------------------------------------------------------------

    def _run(self, kind: str, fn: Callable[[], Any], on_done: Callable[[Any], None],
             executing: bool = False) -> None:
        sequence = self._sequence.get(kind, 0) + 1
        self._sequence[kind] = sequence
        self._handlers[(kind, sequence)] = on_done
        if kind == "task":
            self._busy = True
            self._executing = executing
            self._started_frozen = self._config.get("execution_enabled") is False
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
        if kind == "task" and self._freeze_waiting and not self._busy:
            # The operation that was running when Freeze arrived has returned
            # (at its next safe point, or finished): now JARVIS is frozen.
            self._freeze_waiting = False
            if not self.executionEnabled:
                self._tell("system", QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen. The operation that was running has returned; nothing new can start."))
                self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen"), ()), "system")
        self._refresh_core_state()

    # -- feed ---------------------------------------------------------------

    # Entries keep a MESSAGE: plain text for what people and models wrote, or a
    # JARVIS-owned (context, source, args) rendered in the current language
    # whenever it is read - see jarvis.ui.i18n. Everything is redacted on entry.

    @staticmethod
    def _redacted(message: Any) -> Any:
        if isinstance(message, tuple) and len(message) == 3:
            context, source, args = message
            return (context, source, tuple(Bridge._redacted(a) for a in args))
        return redact_text(str(message))

    def _say(self, kind: str, message: Any, chat: bool = False) -> None:
        entry = {"kind": kind, "msg": self._redacted(message), "ts": time.time()}
        if chat:
            entry["chat"] = True         # a General Chat reply: part of the conversation window
        self._feed.append(entry)
        del self._feed[:-FEED_LIMIT]
        self.feedChanged.emit()

    def _conversation(self) -> tuple:
        """The recent chat of this window for a follow-up: what the person typed
        and General Chat's replies, as written - never notices, approvals,
        errors, task results or fixtures. Core bounds it (intent.conversation_window)."""
        return tuple({"role": "user" if entry["kind"] == "user" else "assistant", "text": entry["msg"]}
                     for entry in self._feed
                     if isinstance(entry["msg"], str) and (entry["kind"] == "user" or entry.get("chat")))

    def _tell(self, kind: str, source: str, *args: Any) -> None:
        """Say a JARVIS-owned notice (a ``Bridge`` catalog source)."""
        self._say(kind, ("Bridge", source, tuple(args)))

    def _note(self, message: Any, kind: str = "system") -> None:
        self._activity.insert(0, {"kind": kind, "msg": self._redacted(message),
                                  "time": datetime.now().strftime("%H:%M:%S"), "ts": time.time()})
        del self._activity[ACTIVITY_LIMIT:]
        self.dataChanged.emit()

    @staticmethod
    def _rendered(entries: list[dict], clock: bool = False) -> list[dict]:
        out = []
        for entry in entries:
            row = {k: v for k, v in entry.items() if k != "msg"}
            row["text"] = render(entry["msg"])
            if clock:
                row["time"] = QLocale().toString(QDateTime.fromSecsSinceEpoch(int(entry["ts"])).time(), QLocale.FormatType.ShortFormat)
            out.append(row)
        return out

    # -- core state ----------------------------------------------------------

    def _refresh_core_state(self) -> None:
        # Dormant only when frozen is REAL: the gate is closed and no operation
        # of this application is still running toward its next safe point.
        if self._config_refused() or (self._config.get("execution_enabled") is False and not self._busy):
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
        # Shown whenever Core sent a request - incomplete ones included, so a
        # request missing its binding reads as unavailable, never as absent.
        self._approval = approval_card(payload)
        self._last_outcome = outcome_kind(payload)
        if self._ticket is not None:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Approval required: %1 -> %2"),
                        (self._approval.get("action"), self._approval.get("destination"))), "approval")
        elif self._last_outcome == "unknown":
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "OUTCOME UNKNOWN for task %1"), (self._task.get("task_id"),)), "unknown")
        elif self._task:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Task %1 %2"),
                        (self._task.get("task_id")[:8], self._task.get("run_status"))), "system")
        self.approvalChanged.emit()
        self.dataChanged.emit()

    def _apply_text(self, payload: Any, text: str) -> None:
        intent = payload.get("intent") if isinstance(payload, dict) else None
        if not isinstance(intent, dict):
            intent = None
        status = str((intent or {}).get("status") or "")
        for entry in text_entries(payload):
            self._say(entry["kind"], entry["text"], chat=status == "conversation")
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
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Scheduled job %1"), ((payload.get("job") or {}).get("label"),)), "scheduled")
            self._run("jobs", lambda: self.backend.jobs(False), self._set_jobs)
        elif status == "conversation":
            pass                                # General Chat: the reply is the whole result
        else:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Intent %1"), (status,)), "error" if status != "scheduled" else "system")
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
    def memoryStatus(self) -> str:
        """The Memory card's status, from what the backend actually reported.

        "Unavailable" when the configuration was refused (every memory call is
        refused with it); "Available" only when the local memory store opened
        and reported its schema; otherwise "Status unavailable". Nothing here
        claims saved information exists or is empty - no such signal exists.
        """
        if self._config and self._config_refused():
            return "Unavailable"
        if type(self._status.get("schema_version")) is int:
            return "Available"
        return "Status unavailable"

    @Property(str, notify=stateChanged)
    def sessionId(self) -> str:
        return self._session_id

    @Property("QVariantMap", notify=stateChanged)
    def task(self) -> dict:
        return dict(self._task)

    @Property("QVariantMap", notify=stateChanged)
    def progress(self) -> dict:
        """The header's four lifecycle stages, projected in present.task_progress."""
        return task_progress(self._task, self._core_state, self._busy)

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
        # Sample turns are fixture copy, not anyone's words: they follow the language.
        self._tell("user", QT_TRANSLATE_NOOP("Bridge", "Can you review my open tasks, check for any important updates, and help me plan the next steps for today?"))
        self._tell("jarvis", QT_TRANSLATE_NOOP("Bridge", "I’ll review your open tasks, check for relevant updates across your tools, and create a focused plan for today."))
        self._tell("jarvis", QT_TRANSLATE_NOOP("Bridge", "Here’s what I found and a recommended plan for today. This takes into account your tasks, recent updates, and key priorities."))
        for kind, text in (("system", QT_TRANSLATE_NOOP("Bridge", "3 relevant documents found")),
                           ("scheduled", QT_TRANSLATE_NOOP("Bridge", "Calendar update")),
                           ("system", QT_TRANSLATE_NOOP("Bridge", "Claude Opus 5 ready")),
                           ("system", QT_TRANSLATE_NOOP("Bridge", "New research insight available"))):
            self._note(("Bridge", text, ()), kind)

    @Property("QVariantList", notify=stateChanged)
    def effortChoices(self) -> list:
        return list(CLAUDE_EFFORT_VALUES)

    @Property("QVariantList", notify=feedChanged)
    def feed(self) -> list:
        return self._rendered(self._feed, clock=True)

    @Property("QVariantList", notify=dataChanged)
    def activity(self) -> list:
        return self._rendered(self._activity)

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
        return [{**entry, "text": render(entry["text"])} for entry in self._task_detail]

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

    @Slot(str, result=bool)
    def submitText(self, text: str) -> bool:
        """Send one natural-language message. True when it was accepted and
        is on its way; False when nothing was sent, so a draft is kept."""
        text = str(text)
        if not text.strip() or self._busy:
            return False
        if self._ticket is not None:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "Answer the pending approval first (Approve, Deny or Cancel)."))
            return False
        self._clarification, self._decision = {}, {}
        self.approvalChanged.emit()
        conversation = self._conversation()   # before this turn: the window is what came earlier
        self._say("user", text)
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        if self._task.get("status") == "awaiting_clarification" and session_id and self._task.get("task_id"):
            task_id = self._task["task_id"]
            self._run("task", lambda: self.backend.clarify(session_id, task_id, text), self._apply_run)
            return True
        self._task = {}
        language = self.language           # the default reply language for General Chat
        self._run(
            "task",
            lambda: self.backend.submit_text(text, session_id, request_id=request_id, response_language=language,
                                             conversation=conversation),
            lambda payload: self._apply_text(payload, text),
        )
        return True

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
        conversation = self._conversation()
        self._say("user", label)
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        language = self.language
        self._run(
            "task",
            lambda: self.backend.submit_text(label, session_id, previous_text=previous, request_id=request_id,
                                             response_language=language, conversation=conversation),
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

    def _decide(self, verb: str, request_id: str | None = None, fingerprint: str | None = None) -> bool:
        """Submit one decision for the request ON SCREEN. With an identity, it
        must still be the current request - a newer revision is never decided
        with an older rendering. Core re-proves the ticket either way."""
        ticket = self._ticket
        if ticket is None or self._busy or not self._approval.get("complete"):
            return False
        if request_id is not None and (str(request_id), str(fingerprint)) != (ticket.request_id, ticket.fingerprint):
            return False
        shown = self._approval
        self._ticket, self._approval = None, {}
        # Labels are kept as catalog sources and rendered by ``safety``.
        self._decision = {"state": "submitting", "verb": verb, "label": QT_TRANSLATE_NOOP("Bridge", "Submitting decision…"),
                          "action": shown.get("action", ""), "destination": shown.get("destination", ""),
                          "code": "", "message": "", "time": ""}
        self.approvalChanged.emit()
        call = self.backend.approve if verb == "approve" else self.backend.reject
        self._run("task", lambda: call(ticket), lambda payload: self._apply_decision(payload, verb, ticket, shown),
                  executing=verb == "approve")
        return True

    def _apply_decision(self, payload: Any, verb: str, ticket: ApprovalTicket | None = None,
                        shown: dict | None = None) -> None:
        """What Core did with the decision - recorded only when it said so."""
        error = payload.get("error") if isinstance(payload, dict) else None
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if not isinstance(payload, dict):
            state, label = "unknown", QT_TRANSLATE_NOOP("Bridge", "Decision status unknown")
        elif code in ("invalid_grant", "stale_grant", "task_not_found"):
            state, label = "invalidated", QT_TRANSLATE_NOOP("Bridge", "Request changed, expired or was resolved elsewhere — decision not recorded")
        elif code == "execution_disabled":
            state, label = "not_recorded", QT_TRANSLATE_NOOP("Bridge", "JARVIS is frozen — approval not recorded; the request stays pending")
        elif code in REFUSED_CODES and code != "approval_rejected":
            state, label = "not_recorded", QT_TRANSLATE_NOOP("Bridge", "Decision not recorded")
        elif verb == "reject":
            state, label = ("rejected", QT_TRANSLATE_NOOP("Bridge", "Request rejected")) if payload.get("run_status") == "rejected" \
                else ("unknown", QT_TRANSLATE_NOOP("Bridge", "Decision status unknown"))
        else:
            state, label = "recorded", QT_TRANSLATE_NOOP("Bridge", "Approval recorded")
        self._decision = {**self._decision, "state": state, "label": label, "code": code,
                          "message": str(error.get("message") or "") if isinstance(error, dict) else "",
                          "ts": time.time()}
        self._apply_run(payload)
        if code == "execution_disabled" and ticket is not None and self._ticket is None:
            # Core refused the decision before touching the request, which is
            # still pending exactly as it was: keep showing it.
            self._ticket, self._approval = ticket, dict(shown or {})
            self.approvalChanged.emit()

    @Slot()
    def approve(self) -> None:
        self._decide("approve")

    @Slot()
    def deny(self) -> None:
        self._decide("reject")

    @Slot(str, str, result=bool)
    def approveRequest(self, request_id: str, fingerprint: str) -> bool:
        """Approve exactly the displayed request (its id and fingerprint). Not
        while frozen: approving starts work (Core refuses it too)."""
        if not self.executionEnabled:
            return False
        return self._decide("approve", request_id, fingerprint)

    @Slot(str, str, result=bool)
    def rejectRequest(self, request_id: str, fingerprint: str) -> bool:
        """Reject exactly the displayed request. Nothing else is cancelled or disabled."""
        return self._decide("reject", request_id, fingerprint)

    @Slot()
    def cancelTask(self) -> None:
        """Cancel through Core; while running the id is storage's, not ours."""
        session_id = self._session_id
        task_id = (self.backend.active_task_id(session_id) if self._busy and session_id
                   else self._task.get("task_id"))
        if not session_id or not task_id:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "No cancellable task is registered yet."))
            return
        self._ticket, self._approval = None, {}
        self.approvalChanged.emit()
        self._run("cancel", lambda: self.backend.cancel(session_id, task_id), self._apply_run)

    @Slot()
    def newSession(self) -> None:
        if self._busy:
            return
        self._session_id, self._task, self._ticket, self._approval, self._clarification = "", {}, None, {}, {}
        self._decision = {}
        self._last_outcome = "idle"
        self._tell("system", QT_TRANSLATE_NOOP("Bridge", "New session: the next message starts a fresh session."))
        self.approvalChanged.emit()
        self._refresh_core_state()

    @Slot(str)
    def useSession(self, session_id: str) -> None:
        if session_id and not self._busy:
            self._session_id, self._task, self._ticket, self._approval, self._decision = str(session_id), {}, None, {}, {}
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "Using session %1."), session_id)
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
        self._tell("system", QT_TRANSLATE_NOOP("Bridge", "Running one scheduler sweep..."))
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

    # -- Freeze / Resume JARVIS -------------------------------------------------
    #
    # Freeze closes the persisted execution gate AT ONCE, on this thread (never
    # queued behind running work). Core re-reads that gate at every stage
    # boundary, so an operation already running stops at its next safe point
    # with its task persisted where it was; an atomic step (a connector call)
    # is never interrupted. JARVIS is FROZEN only once the gate is confirmed
    # closed AND nothing of this application is still running. Resume opens
    # the gate again and continues only a task Freeze paused - never a failed,
    # cancelled, rejected or unknown one; Core's journal never replays.

    def _set_gate(self, want: bool) -> bool:
        self._execution_request = "resume" if want else "freeze"
        self.stateChanged.emit()
        payload = self.backend.set_config("execution_enabled", "true" if want else "false")
        self._apply_config(payload)
        confirmed = bool(self._config) and self.executionEnabled == want
        self._execution_request = "" if confirmed else "unconfirmed"
        self._freeze_waiting = confirmed and not want and self._busy
        if not confirmed:
            if want:
                source = (QT_TRANSLATE_NOOP("Bridge", "JARVIS state unconfirmed: the request to resume JARVIS was not confirmed. The store reports execution running.")
                          if self.executionEnabled else
                          QT_TRANSLATE_NOOP("Bridge", "JARVIS state unconfirmed: the request to resume JARVIS was not confirmed. The store reports execution frozen."))
            else:
                source = (QT_TRANSLATE_NOOP("Bridge", "JARVIS state unconfirmed: the request to freeze JARVIS was not confirmed. The store reports execution running.")
                          if self.executionEnabled else
                          QT_TRANSLATE_NOOP("Bridge", "JARVIS state unconfirmed: the request to freeze JARVIS was not confirmed. The store reports execution frozen."))
            self._tell("error", source)
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS state unconfirmed"), ()), "error")
        elif not want and self._freeze_waiting:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "Freeze requested. Nothing new can start. The operation that is running now "
                                                             "stops at its next safe point; a step already sent to its destination is not "
                                                             "stopped or undone."))
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Freeze requested"), ()), "system")
        elif not want:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen. Nothing new can start; JARVIS stays open. "
                                                             "Resume JARVIS from the red control or the tray when ready."))
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen"), ()), "system")
        self._refresh_core_state()
        return confirmed

    @Slot()
    def freezeJarvis(self) -> None:
        """The red control and the tray: close the execution gate now. No confirmation."""
        self._set_gate(False)

    @Slot()
    def resumeJarvis(self) -> None:
        """Called after the person confirmed Resume. Opens the gate; a task that
        Freeze paused continues from its persisted stage, nothing else starts."""
        if not self._set_gate(True):
            return
        paused = task_outcome(self._task).get("kind") == "paused"
        self._tell("system", QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed. Continuing the paused task from where it stopped.")
                   if paused else QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed. Nothing else restarts by itself."))
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed"), ()), "system")
        if paused and self._session_id:
            self.resumeTask(self._session_id, self._task["task_id"])

    @Slot(bool)
    def setExecutionEnabled(self, enabled: bool) -> None:
        """The persisted execution gate: False freezes, True resumes."""
        if enabled:
            self.resumeJarvis()
        else:
            self.freezeJarvis()

    @Property(str, notify=stateChanged)
    def executionState(self) -> str:
        """JARVIS lifecycle as established: running / freeze_requested (gate
        closed, an operation still on its way to a safe point) / frozen,
        resuming while the gate write is under way, unconfirmed when a request
        was not confirmed, refused, or unknown before any read."""
        if self._execution_request == "unconfirmed":
            return "unconfirmed"
        if self._execution_request == "resume":
            return "resuming"
        if self._execution_request == "freeze":
            return "freeze_requested"
        if not self._config:
            return "unknown"
        if self._config_refused():
            return "refused"
        if self.executionEnabled:
            return "running"
        return "freeze_requested" if self._busy and not self._started_frozen else "frozen"

    # -- window, tray and quit ---------------------------------------------------

    trayChanged = Signal()
    hiddenToTray = Signal()           # the window went to the tray (the tray may say so once)
    resumeConfirmRequested = Signal() # the tray asks the window to confirm Resume
    quitConfirmRequested = Signal()   # the tray asks the window to confirm Quit

    @Property(bool, notify=trayChanged)
    def trayAvailable(self) -> bool:
        return self._tray_available

    def setTrayAvailable(self, available: bool) -> None:
        self._tray_available = bool(available)
        self.trayChanged.emit()

    @Property(bool, notify=trayChanged)
    def quitting(self) -> bool:
        return self._quitting

    @Slot()
    def noteHiddenToTray(self) -> None:
        self.hiddenToTray.emit()

    @Property(str, notify=attentionChanged)
    def quitWarning(self) -> str:
        """Why quitting now needs a confirmation, from real state - or ""."""
        if self._busy:
            return tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS is running an operation right now. Quitting closes JARVIS without cancelling it: "
                                                            "the operation may keep running until it returns, and anything already sent to its "
                                                            "destination is not stopped or undone."))
        outcome = task_outcome(self._task)
        if outcome.get("kind") in ("unknown", "in_progress") or outcome.get("run_status") == "cancelling":
            return tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS has work whose outcome is not established. Quitting does not resolve or stop it; "
                                                            "it stays recorded in History, and anything already sent to its destination is not undone."))
        return ""

    @Slot()
    def requestQuit(self) -> None:
        """Quit JARVIS (tray): at once when nothing is active, otherwise ask first."""
        if self.quitWarning:
            self.quitConfirmRequested.emit()
        else:
            self.confirmQuit()

    @Slot()
    def confirmQuit(self) -> None:
        """The orderly quit. The window accepts its close from here on."""
        self._quitting = True
        self.trayChanged.emit()
        self.quitRequested.emit()

    # -- the safety projection: cards + the one attention strip ---------------

    @Property("QVariantMap", notify=attentionChanged)
    def safety(self) -> dict:
        """Current safety state for the conversation cards and the attention
        strip, projected only from Core-reported state. Priority orders the
        attention items; it never removes one. History plays no part."""
        if self._dev_safety:
            return self._dev_safety_projection(self._dev_safety)
        execution = self.executionState
        approval = dict(self._approval)
        if approval:
            decidable = self._ticket is not None and bool(approval.get("complete")) and not self._busy
            approval["capability"] = str(self._task.get("capability") or "")
            # Frozen: approving would start work, so it waits for Resume; rejecting
            # executes nothing, so Core accepts it while frozen.
            approval["actionable"] = decidable and self.executionEnabled
            approval["rejectable"] = decidable
            approval["hold"] = (tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Approval details unavailable"))
                                if not approval.get("complete") or self._ticket is None
                                else tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS is frozen. Approving is unavailable until you resume JARVIS; Reject still works."))
                                if not self.executionEnabled else "")
        outcome = task_outcome(self._task)
        items: list[dict[str, Any]] = []
        if execution in ("frozen", "freeze_requested", "resuming", "refused", "unconfirmed"):
            items.append({"key": "execution", "level": 1, "tone": "danger", "label": tx("Bridge", {
                "frozen": QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen"),
                "refused": QT_TRANSLATE_NOOP("Bridge", "Configuration refused — execution off"),
                "unconfirmed": QT_TRANSLATE_NOOP("Bridge", "JARVIS state unconfirmed"),
                "resuming": QT_TRANSLATE_NOOP("Bridge", "Resuming JARVIS…"),
                "freeze_requested": QT_TRANSLATE_NOOP("Bridge", "Freeze requested — waiting for the current operation")}[execution])})
        if approval:
            items.append({"key": "approval", "level": 2, "tone": "warning", "label": tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Your approval is required"))})
        if self._clarification or self._task.get("status") == "awaiting_clarification":
            items.append({"key": "decision", "level": 2, "tone": "warning", "label": tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Your decision is needed"))})
        attention = self._outcome_attention(outcome.get("kind", ""))
        if attention:
            items.append({"key": "outcome", "level": attention[0], "tone": attention[1], "label": attention[2]})
        items.sort(key=lambda item: item["level"])       # stable: equal levels keep their order
        decision = dict(self._decision)
        if decision:
            decision["label"] = tx("Bridge", decision["label"])
            decision["time"] = (QLocale().toString(QDateTime.fromSecsSinceEpoch(int(decision["ts"])).time(), QLocale.FormatType.ShortFormat)
                                if decision.get("ts") else "")
        return {"fixture": False, "execution": execution, "running": self._busy, "approval": approval,
                "decision": decision, "outcome": outcome, "attention": items,
                "cards": bool(approval or self._decision or outcome
                              or execution in ("frozen", "freeze_requested", "refused", "unconfirmed"))}

    @staticmethod
    def _outcome_attention(kind: str) -> tuple | None:
        """(level, tone, label) for an outcome that needs attention, else None."""
        entry = {"unknown": (3, "unknown", QT_TRANSLATE_NOOP("Bridge", "Outcome unknown")),
                 "in_progress": (3, "unknown", QT_TRANSLATE_NOOP("Bridge", "Outcome not established yet")),
                 "blocked": (4, "warning", QT_TRANSLATE_NOOP("Bridge", "Action blocked")),
                 "failed": (4, "danger", QT_TRANSLATE_NOOP("Bridge", "Action failed"))}.get(kind)
        return (entry[0], entry[1], tx("Bridge", entry[2])) if entry else None

    @Property(str, notify=attentionChanged)
    def devSafetyScenario(self) -> str:
        return self._dev_safety

    @Slot()
    def nextDevSafetyScenario(self) -> None:
        """DEV FIXTURE only: cycle the display-only safety scenarios."""
        if not self.devFixture:
            return
        self._dev_safety = DEV_SAFETY_SCENARIOS[(DEV_SAFETY_SCENARIOS.index(self._dev_safety) + 1) % len(DEV_SAFETY_SCENARIOS)]
        self.attentionChanged.emit()

    @Slot(str)
    def setDevSafetyScenario(self, name: str) -> None:
        if self.devFixture and name in DEV_SAFETY_SCENARIOS:
            self._dev_safety = name
            self.attentionChanged.emit()

    @staticmethod
    def _dev_safety_projection(name: str) -> dict:
        """Placeholder fields only, flagged ``fixture``: nothing here can be approved."""
        def ph(source: str) -> str:
            return tx("Bridge", source)
        sample = {"task_id": "DEV-FIXTURE", "connector": ph(QT_TRANSLATE_NOOP("Bridge", "[Connector]")),
                  "capability": ph(QT_TRANSLATE_NOOP("Bridge", "[Capability]")),
                  "destination": ph(QT_TRANSLATE_NOOP("Bridge", "[Exact destination]")), "run_status": "", "code": "",
                  "message": ph(QT_TRANSLATE_NOOP("Bridge", "[Core-reported detail]"))}
        approval, outcome, items = {}, {}, []
        if name.startswith("approval"):
            complete = name == "approval"
            approval = {"summary": ph(QT_TRANSLATE_NOOP("Bridge", "[Action summary]")),
                        "action": ph(QT_TRANSLATE_NOOP("Bridge", "[Action]")) if complete else "",
                        "target": ph(QT_TRANSLATE_NOOP("Bridge", "[Target]")),
                        "connector": sample["connector"] if complete else "", "capability": sample["capability"],
                        "destination": sample["destination"], "data_classification": ph(QT_TRANSLATE_NOOP("Bridge", "[classification]")),
                        "effect": ph(QT_TRANSLATE_NOOP("Bridge", "[effect]")), "reversible": False,
                        "reason": ph(QT_TRANSLATE_NOOP("Bridge", "[Why approval is required]")),
                        "expires_at": ph(QT_TRANSLATE_NOOP("Bridge", "[expiry]")), "request_id": "DEV-FIXTURE", "fingerprint": "DEV-FIXTURE",
                        "outbound_preview": ph(QT_TRANSLATE_NOOP("Bridge", "[Outbound preview]")), "complete": complete, "actionable": False,
                        "hold": ph(QT_TRANSLATE_NOOP("Bridge", "DEV FIXTURE · not a live approval request")) if complete
                                else ph(QT_TRANSLATE_NOOP("Bridge", "Approval details unavailable"))}
            items.append({"key": "approval", "level": 2, "tone": "warning", "label": ph(QT_TRANSLATE_NOOP("Bridge", "Your approval is required"))})
        else:
            outcome = {**sample, "kind": name, "run_status": "completed" if name == "completed" else "failed"}
            level = Bridge._outcome_attention(name)
            if level:
                items.append({"key": "outcome", "level": level[0], "tone": level[1], "label": level[2]})
        return {"fixture": True, "execution": "running", "running": False, "approval": approval, "decision": {},
                "outcome": outcome, "attention": items, "cards": True}

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

    # -- interface language ---------------------------------------------------

    languageChanged = Signal()

    @Property(str, notify=languageChanged)
    def language(self) -> str:
        """The chosen interface language as a stable locale id (en-US / tr-TR).

        Also the seam for later conversation features: it is the DEFAULT
        response language (an explicit language in the request still wins)."""
        return self.prefs.data["language"]

    @Slot(str)
    def setLanguage(self, language: str) -> None:
        """Persist the choice; the application's Localization applies it live."""
        if language in LANGUAGES and language != self.prefs.data["language"]:
            self.prefs.data["language"] = language
            self.prefs.save()
            self.languageChanged.emit()

    @Slot()
    def retranslated(self) -> None:
        """The running language changed: every projected text re-renders.
        State itself is untouched - only its presentation is read again."""
        self.feedChanged.emit()
        self.dataChanged.emit()
        self.stateChanged.emit()
        self.approvalChanged.emit()

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

    @Property("QVariantList", notify=prefsChanged)
    def railCards(self) -> list:
        """The left rail's cards in stored order, each with its visible flag."""
        rail = self.prefs.data["rail"]
        return [{"id": name, "visible": rail["visible"][name]} for name in rail["order"]]

    @Property(str, notify=prefsChanged)
    def layoutSaveState(self) -> str:
        """"saved" once a layout change is on disk, "failed" when the last one
        could not be written (the previous layout is back), "" before any."""
        return self._layout_save

    def _layout_changed(self) -> None:
        if self.prefs.save():
            self._layout_save = "saved"
        else:
            self.prefs.restore_last_layout()
            self._layout_save = "failed"
        self.prefsChanged.emit()

    @Slot(str, str, bool)
    def setCardVisible(self, region: str, card: str, visible: bool) -> None:
        """Hide a card from the layout or add it back. Presentation only."""
        if region == "rail":
            self.prefs.set_rail_visible(str(card), bool(visible))
        elif region == "stack":
            self.prefs.set_visible(str(card), bool(visible))
        else:
            return
        self._layout_changed()

    @Slot(str, str, int)
    def moveCard(self, region: str, card: str, step: int) -> None:
        self.prefs.move_card(str(region), str(card), int(step))
        self._layout_changed()

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
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS interface started"), ()), "system")
        if self.devFixture:
            self._seed_dev_fixture()

    @Slot()
    def waitIdle(self) -> None:
        """Block until the worker pool drains (tests only)."""
        self._pool.waitForDone(30_000)
