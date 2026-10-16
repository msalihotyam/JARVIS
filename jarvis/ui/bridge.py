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

import contextlib
import os
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import Property, QDateTime, QLocale, QObject, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QWindow
import shiboken6

from jarvis.context_items import (
    DEFAULT_RESPONSE_MODE,
    MAX_ITEMS,
    NOTE_MAX_CHARS,
    RESPONSE_MODES,
    TEXT_ATTACHMENT_MAX_CHARS,
    AttachmentRefused,
    attach_bytes,
    attach_file,
    discard_snapshots,
    file_kind,
    group,
    group_label,
    history_text,
    reference,
    result_history,
    safe_name,
    same_reference,
)
from jarvis import conversation_search, project_files
from jarvis.app import project_folder_result
from jarvis.connectors.project_folder import PROJECT_FOLDER_CAPABILITY, PROJECT_FOLDER_CONNECTOR, UNVERIFIED_SUFFIX
from jarvis.destinations import CLAUDE_CODE_DESTINATION
from jarvis.memory import PROJECT_FOLDER_LINK_UNREADABLE
from jarvis.contracts import PROJECT_INSTRUCTION_TEXT_CAP, normalize_retrieved_text, redact_credentials
from jarvis.conversation import (REPLY_MAX_CHARS, CorruptConversation, PersistedConversation, cut_reply, plain_reply,
                                 split_cut, task_title, turn_refs)
from jarvis.model_catalog import ProviderCatalog, default_catalog_path
from jarvis.model_retirement import adopt_claude_lane, claude_lane, claude_migration, codex_migration, newest_per_name
from jarvis.ui.backend import ATTACHMENT_SUPPORT, ApprovalTicket, Backend, approval_ticket, redact_text
from jarvis.ui.i18n import LANGUAGES, QT_TRANSLATE_NOOP, render, tx
from jarvis.ui import weather
from jarvis.ui.telemetry import Sampler
from jarvis.ui.wording import code_word
from jarvis.ui.preferences import (
    CHAT_PROVIDERS,
    CONTEXTUAL_PANELS,
    GLOBAL_SCOPE,
    NEW_CHAT_DRAFT,
    PAGES,
    PIN_LIMIT,
    PROFILES,
    THEMES,
    UiPreferences,
    FREEZE_KEYS,
    STATUS_INDICATORS,
    ordered_projects,
    validate_preferences,
)
from jarvis.ui.present import (
    FOLDER_UNKNOWN,
    LINK_STATE_TEXT,
    REASON_TEXT,
    REFUSED_CODES,
    display_model_name,
    provider_brand,
    TERMINAL_STATUSES,
    approval_card,
    cut_entries,
    cut_reason,
    error_entries,
    failure_message,
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
QUEUE_LIMIT = 5         # DFQUEUE: messages that may wait for the one run at a time
ACTIVITY_LIMIT = 50
CORE_STATES = ("idle", "running", "awaiting_approval", "awaiting_input", "completed",
               "error", "unknown", "disabled")
#: DEV FIXTURE only (JARVIS_UI_DEV_FIXTURE=1): display-only safety cards for
#: visual review. They never create a ticket, a task or a decision.
DEV_SAFETY_SCENARIOS = ("", "approval", "approval_incomplete", "unknown", "failed", "blocked", "completed")


#: What a pasted image is called on the draft (the clipboard names nothing).
CLIPBOARD_IMAGE_NAME = "pasted-image.png"
PASTED_TEXT_NAME = "pasted-text.txt"


class _Emitter(QObject):
    """Lives on the GUI thread; the worker emits through it (queued)."""

    finished = Signal(str, int, object)
    preview = Signal(str, str)        # request id, the reply text visible so far


#: The token fields a provider reports that together are what one call used.
#: Claude counts cache reads and writes apart from ``input_tokens``; Codex counts
#: its cached input INSIDE ``input_tokens`` and its reasoning INSIDE
#: ``output_tokens``, so those two are never added again.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
#: Of those, the ones a cache serves at a fraction of the normal input price.
#: Reported at face value alongside the total - never estimated, never implied
#: for a provider (Codex) that folds its cached share INSIDE input_tokens.
_CACHE_FIELDS = ("cache_creation_input_tokens", "cache_read_input_tokens")


def reported_tokens(*reports: Any) -> int | None:
    """The tokens the providers REPORTED for the calls behind one answer, or None
    when none was reported - never an estimate."""
    values = [report[name] for report in reports if isinstance(report, dict)
              for name in _TOKEN_FIELDS if type(report.get(name)) is int and report[name] >= 0]
    return sum(values) if values else None


def reported_cache_tokens(*reports: Any) -> int | None:
    """Of ``reported_tokens``, the share a cache served - so a total that is
    mostly cheap cache reads never reads as an equally expensive fresh call."""
    values = [report[name] for report in reports if isinstance(report, dict)
              for name in _CACHE_FIELDS if type(report.get(name)) is int and report[name] >= 0]
    return sum(values) if values else None


# Gate 6G R2: the screen slots a conversation's waiting state lives in - live for the open one,
# parked (bridge memory only) for another. ``_decision`` is not one: every activation resets it.
_SLOTS = ("session_id", "task", "ticket", "approval", "clarification", "last_outcome")


def _empty_slots() -> dict[str, Any]:
    return {"session_id": "", "task": {}, "ticket": None, "approval": {}, "clarification": {}, "last_outcome": "idle"}


def _waiting(slots: dict) -> bool:
    """The person is needed (Q2): an approval card (an incomplete one too), a clarification, or a
    task Core reports awaiting clarification (a bare run keeps that only in the task)."""
    return bool(slots["approval"] or slots["clarification"] or slots["task"].get("status") == "awaiting_clarification")


def _draft_merged(base: dict | None, front: dict) -> dict[str, Any]:
    """DFQUEUE: ``front`` (a queued message or a parked draft) put before the draft ``base``."""
    base = base or {"text": "", "groups": [], "attachments": [], "mode": DEFAULT_RESPONSE_MODE}
    text = "\n\n".join(part for part in (front["text"], base["text"]) if part.strip())
    return {"text": text, "groups": [dict(g) for g in (*front["groups"], *base["groups"])],
            "attachments": [dict(a) for a in (*front["attachments"], *base["attachments"])],
            "mode": front["mode"] if front["groups"] else base["mode"], "at": int(time.time())}


def _parks(slots: dict) -> bool:
    """OD1: kept for a conversation that is not open only while it needs the person or Resume."""
    return _waiting(slots) or task_outcome(slots["task"]).get("kind") == "paused"


def _proved_path(run: Any) -> str:
    """F7: the root-relative path a COMPLETED file run's connector proved from
    its open handle (the envelope's local_read / local_write block), or "".
    Never the destination: that is planner text."""
    if not isinstance(run, dict) or run.get("run_status") != "completed":
        return ""
    for key in ("local_read", "local_write"):
        block = run.get(key)
        if isinstance(block, dict) and type(block.get("relative_path")) is str:
            return block["relative_path"]
    return ""


def _run_facts(task: dict, payload: Any, intent: Any = None) -> dict[str, str]:
    """What a task envelope cannot say about its own task: the capability and the destination,
    from the resolved intent, else from the task block its OWN conversation already holds."""
    resolved = task_status(payload, intent) if intent is not None else {}
    return {key: resolved.get(key) or task.get(key, "") for key in ("capability", "destination")}


def _here(facts: dict) -> dict:
    """6H H3 (E4): a K3 task's destination reads "This computer" wherever it is
    shown - the grant row's name is a label here and names no endpoint. Display
    only: the stored task, turn and request keep the grant row's destination."""
    if facts.get("capability") == PROJECT_FOLDER_CAPABILITY and facts.get("destination"):
        return {**facts, "destination": tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "This computer"))}
    return facts


#: 6H H3: the app's E7 notices (app.project_folder_switch), as Bridge catalog sources.
_K3_NOTICES = (QT_TRANSLATE_NOOP("Bridge", "JARVIS is frozen; no new folder request was made."),
               QT_TRANSLATE_NOOP("Bridge", "JARVIS could not propose a folder for this project. Nothing was created."))


def _project_run(prior: dict, payload: Any, intent: Any = None) -> dict[str, Any]:
    """One conversation's slots after a task envelope (a run, a decision, a cancel, a resume, a
    clarify), from that conversation's own prior slots only. A run envelope answers any
    clarification that was waiting (OD6)."""
    slots = {**prior, "clarification": {}}
    if isinstance(payload, dict):
        session_id = (payload.get("session") or {}).get("id") if isinstance(payload.get("session"), dict) else None
        if isinstance(session_id, str):
            slots["session_id"] = session_id
        if isinstance(payload.get("task"), dict) and isinstance(payload["task"].get("id"), str):
            slots["task"] = task_status(payload, intent)
    slots["ticket"] = approval_ticket(payload)
    # Shown whenever Core sent a request - incomplete ones included, so a
    # request missing its binding reads as unavailable, never as absent.
    slots["approval"] = approval_card(payload)
    slots["last_outcome"] = outcome_kind(payload, facts=_run_facts(prior["task"], payload, intent))
    return slots


def _project_text(prior: dict, payload: Any, text: str) -> dict[str, Any]:
    """The slots after an understanding envelope for ``text`` (what the person sent)."""
    intent = payload.get("intent") if isinstance(payload, dict) else None
    if not isinstance(intent, dict):
        intent = None
    status = str((intent or {}).get("status") or "")
    if status == "structured":
        return _project_run(prior, payload.get("run"), intent)
    clarification: dict[str, Any] = {}
    if status == "clarification_required" and intent is not None:
        choices = [c for c in (intent.get("choices") or []) if isinstance(c, dict)
                   and isinstance(c.get("label"), str) and isinstance(c.get("destination"), str)]
        clarification = {"request_id": str(intent.get("request_id") or ""), "previous_text": text,
                         "question": str(intent.get("question") or ""), "choices": choices}
    return {**prior, "ticket": None, "approval": {}, "clarification": clarification,
            "task": task_status(None, intent), "last_outcome": outcome_kind(None, status)}


def _kept_refusal(slots: dict, payload: Any, ticket: ApprovalTicket | None, shown: dict | None) -> dict[str, Any]:
    """Core refused a decision with ``execution_disabled`` before touching the request, which is
    still pending exactly as it was: the decided ticket and card stay. Not when the task moved
    on - an approval Core carried out until Freeze paused it is no request any more (R2)."""
    error = payload.get("error") if isinstance(payload, dict) else None
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    if (code == "execution_disabled" and ticket is not None and slots["ticket"] is None
            and slots["task"].get("status") in ("awaiting_approval", "awaiting_cost_override")):
        return {**slots, "ticket": ticket, "approval": dict(shown or {})}
    return slots


def _kept_partial(text: str) -> str:
    """DFREPLY: a streamed reply that stopped, as it may be kept: at most a reply's
    ceiling, credential-redacted by the same rules as a finished model answer, and
    without a token-shaped last word the stream may have cut (a credential prefix too
    short for any rule to match; the shortest rule needs 8 such characters)."""
    text = redact_credentials(str(text)[:REPLY_MAX_CHARS])[0]
    start = len(text)
    while start and text[start - 1] in _TOKEN_CHARS:
        start -= 1
    return (text[:start] if len(text) - start >= 8 else text).rstrip()


_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~+/=-")


def _cut_feed(seen: str, status: str, intent: Any, payload: Any) -> list[dict[str, Any]]:
    """The cut-short reply and its notice; an established cause the verdict names
    (a sign-in, a billing route) keeps its own card after them, so it stays actionable."""
    entries = cut_entries(seen, cut_reason(status))
    if REASON_TEXT.get(str((intent or {}).get("reason") or "")):
        entries += text_entries(payload)
    return entries


def _waited(payload: Any, queued_ms: int | None) -> Any:
    """DFSPEED: a queued message's wait, added to its turn's ``timing`` - only when
    the envelope carries one (a model was asked); anything else passes unchanged."""
    intent = payload.get("intent") if isinstance(payload, dict) else None
    usage = intent.get("usage") if isinstance(intent, dict) else None
    if queued_ms is not None and isinstance(usage, dict) and isinstance(usage.get("timing"), dict):
        usage["timing"]["queued_ms"] = queued_ms
    return payload


# V2.1a M4: the app's context-use labels as the UI's states; anything else is unknown.
_USAGE_STATES = {"normal": "normal", "yuksek": "high", "dolu": "full"}
_UNKNOWN_USAGE = {"used": None, "max": None, "ratio": None, "turnsUntilAutoCompact": None, "state": "unknown"}


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
    providersChanged = Signal()
    modelApplied = Signal()       # the configured model / effort really changed
    composingChanged = Signal()
    attentionChanged = Signal()   # the safety projection may have changed

    def __init__(
        self, backend: Backend, preferences: UiPreferences, *, sync: bool = False, parent: QObject | None = None,
        weather_key: str = "",
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
        self._emitter.preview.connect(self._on_preview)
        # The General Chat reply while it streams: ONE turn, shown apart from the
        # feed until the final envelope replaces it (display only, never history).
        self._stream_text = ""
        self._stream_ts = 0.0
        # Response telemetry: the monotonic clock, when the running task started,
        # when its first text became visible, and what the finished run took.
        self._clock: Callable[[], float] = time.monotonic
        self._work_started: float | None = None
        self._first_visible: float | None = None
        self._work_ms: int | None = None
        self._work_started_at: float = 0.0
        # The CURRENT draft's context: reference groups (each owning its note and
        # files) and the message's own attachments. Data for that one turn.
        self._groups: list[dict[str, Any]] = []
        self._attachments: list[dict[str, Any]] = []
        self._attach_revision = 0
        self._item_seq = 0
        self._response_mode = DEFAULT_RESPONSE_MODE
        self._snapshots = Path(tempfile.mkdtemp(prefix="jarvis-attachments-"))
        self._entry_seq = 0
        # Local machine load for the System status card: read-only kernel
        # figures, sampled on a slow clock. Nothing until the first reading.
        self._sampler = Sampler()
        self._telemetry: dict[str, Any] = {"available": False}
        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.setInterval(5_000)
        self._telemetry_timer.timeout.connect(self._sample_telemetry)
        QTimer.singleShot(1_500, self._sample_telemetry)
        self._telemetry_timer.start()
        # Current weather for the clock card (jarvis.ui.weather): the operator's
        # fixed place, its own thread (never the chat worker), every ~5 minutes,
        # and no network at all without a key. The key is held here only.
        self._weather_key = weather_key
        self._weather_fetch = weather.fetch_json
        self._weather_cache = self.prefs.path.parent / "weather-location.json"
        self._weather_location: dict[str, Any] | None = None
        self._weather_reading: dict[str, Any] | None = None
        self._weather_error = ""
        self._weather_view: dict[str, Any] = weather.project(None, None)
        self._weather_busy = False
        self._weather_next = 0.0
        # Set once by ``shutdown``; under the lock so a finishing weather thread
        # either delivers before it or never.
        self._shutting_down = False
        self._weather_lock = threading.Lock()
        self._weatherDone.connect(self._weather_finished, Qt.QueuedConnection)
        self._weather_timer = QTimer(self)
        self._weather_timer.setInterval(60_000)
        self._weather_timer.timeout.connect(self._weather_tick)
        QTimer.singleShot(3_000, self._weather_tick)
        self._weather_timer.start()
        self._sequence: dict[str, int] = {}
        self._handlers: dict[tuple[str, int], Callable[[Any], None]] = {}
        self._pending_jobs: dict[tuple[str, int], Callable[[], None]] = {}
        # -- state --
        self._busy = False
        # DFQUEUE: messages sent while JARVIS works elsewhere, oldest first (bridge memory only).
        self._queue: list[dict[str, Any]] = []
        # Gate 6G R1: the run record of the conversation-bound "task" run in flight (bridge memory only,
        # never persisted): origin conversation, request / session / task identity, the attachment
        # snapshots its request references and its latest preview. None for any other run (a sweep).
        self._run_record: dict[str, Any] | None = None
        # Gate 6G R2: per conversation id, the waiting state (``_SLOTS``) of a conversation that is
        # not open - restored only on that conversation (bridge memory only, never persisted, so
        # a restart shows none: Q5) - and the needsYouIds last announced.
        self._parked: dict[str, dict[str, Any]] = {}
        self._needs_you_seen: list[str] = []
        # Gate 6G R3 (D1): conversations a recorded run finished for while another one was open,
        # until opened (bridge memory only, never persisted: a restart shows none).
        self._finished_unseen: set[str] = set()
        self._run_preview = ""        # DFREPLY: the finished run's streamed text while it is delivered
        self._restored_session = ""                  # the session the open screen got back from its parked state
        self._deliveries: dict[str, int] = {}        # per conversation: envelopes delivered so far (newest wins)
        self._executing = False    # the current / last task run is an approved execution
        # The running request began while frozen: it can start no work (Core
        # refuses anything authoritative), so nothing is on its way to a safe point.
        self._started_frozen = False
        # One in-app scheduler sweep at a time (see ``sweepDueWork``).
        self._sweeping = False
        # The selector's provider lines: last-known-good from the local catalog at
        # once, refreshed on the catalog's own threads (never the chat worker).
        self._providers: list[dict] = []
        self._catalog = ProviderCatalog(default_catalog_path(self.prefs.path), self._probe_provider,
                                        self._catalog_changed, sync=sync)
        self._catalogDone.connect(self._show_catalog, Qt.QueuedConnection)
        self._liveLine.connect(self._follow_models, Qt.QueuedConnection)
        self._show_catalog()
        # The last understanding / General Chat call's usage, as its envelope reported it.
        self._chat_usage: dict = {}
        # Plan limits exactly as each provider last REPORTED them, with when that was.
        self._claude_limits: dict = {}
        self._codex_quota: dict = {}
        self._composing = False
        self._core_state = "idle"
        self._session_id = ""
        self._task: dict[str, Any] = {}
        self._feed: list[dict[str, str]] = []
        self._activity: list[dict[str, str]] = []
        # THE canonical conversation (jarvis.conversation), held in the Core
        # store: what later calls are given, durable across a restart, whichever
        # provider answers. The feed above is what is SHOWN; this is what is
        # remembered. Several conversations exist; this is the open one. A record
        # that does not verify is never opened and nothing from it is used.
        self._store_run = uuid4().hex[:8]         # this window's turns, for idempotent appends
        self._conversation_list: list[dict] = []
        self._scope_conversations: list[dict] = []
        self._sidebar_lists: dict[str, list[dict]] = {}
        # Gate 6F F6: the pinned conversations' records (any scope) and their owners' names.
        self._pin_records: dict[str, Any] = {}
        self._pin_projects: dict[str, dict] = {}
        self._show_old_models = False
        self._rename_notice = ""
        # Gate 6D-b: History's archived Global conversations, whether the open
        # one is itself archived (read-only) and its name, the held delete
        # preview (what the confirm will send) and the last lifecycle answer.
        self._archived_conversations: list[dict] = []
        self._conversation_archived = False
        self._open_seen: tuple[str, bool] = ("", False)     # N2: the open one as last read: (id, live)
        self._open_title: Any = ""
        self._delete_preview: dict[str, Any] = {}
        self._lifecycle_notice = ""
        self._project_archived_conversations: list[dict] = []
        # Gate 6A: the open conversation's owner, as the store reports it
        # ({} = Global). Context only: nothing here is authority.
        self._conversation_owner: dict[str, Any] = {}
        self._scope_facts: dict[str, Any] = {}                       # the sidebar's Focus / Memory tiles; presentation only
        self._work_panel: dict[str, Any] = {}                        # G6W3: the project chat's work panel; presentation only
        # The Projects page: every project (names shown, ids internal), the
        # one whose detail is open, its conversations and its jobs, and the
        # last answer Core gave to a lifecycle request (shown as returned).
        self._project_records: list[dict] = []
        self._project_counts: dict[str, tuple[int, int]] = {}      # project id -> (conversations, archived); presentation only
        self._instruction_lines: dict[str, str] = {}                # project id -> its card's instruction line; presentation only
        self._project_folder: dict[str, str] = {}                   # the open project's current 6H folder link
        self._project_folder_notice = ""                            # the last 6H link/unlink refusal, in plain words
        self._k3_folder: dict[str, str] = {}                        # 6H H3: the shown K3 request's exact folder (display)
        self._k3_switched: set[str] = set()                         # 6H H3: K3 request ids already switched once
        self._open_project = ""
        # G6SB3F: the picker's "New project…" in flight - (the new chat's id, the project ids before it).
        self._new_project_chat: tuple[str, frozenset] | None = None
        self._project_conversations: list[dict] = []
        self._project_jobs: list[dict] = []
        self._project_jobs_bounded = False
        self._project_notice = ""
        # Gate 6B: the open project's CURRENT stored instruction version ({} =
        # none), the key of the one the editor last loaded, the last answer to
        # a save, its file listing and the last answer to a file operation.
        self._project_instruction: dict[str, Any] = {}
        self._project_instruction_key: tuple | None = None
        self._project_instruction_notice = ""
        self._project_files: list[dict] = []
        self._project_file_notice = ""
        # Gate 6C: the held attach preview (what the confirm will send) and
        # the last answer to a preview or confirm.
        self._attach_preview: dict[str, Any] = {}
        self._attach_notice = ""
        # The Projects page is window-only: never persisted, so a release that
        # does not know it never reads it (preferences stay rollback-safe).
        self._page_override = ""
        self._conversation_store = self._open_startup_conversation()
        self._refresh_conversations()
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
        self._skills: list[dict] = []
        self._pending_drafts: list[dict] = []
        self._skills_read = False   # the rail's Skills tile says "—" until a listing answered
        # SKUIB (b1, b5, b7): the Skills page's lessons, suppressions, unreadable count and own notice
        self._lessons: list[dict] = []
        self._lesson_detail: dict = {}
        self._suppressions: list[dict] = []
        self._skills_unreadable = 0
        self._skill_notice = ""
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
        # The compact model card reads configuration, preferences AND provider
        # state, so any of the three refreshes it.
        for signal in (self.stateChanged, self.prefsChanged):
            signal.connect(self.providersChanged)
        # The sidebar's pins and groups follow the lists and the project names.
        for signal in (self.conversationChanged, self.projectsChanged):
            signal.connect(self.sidebarChanged)
        # R2: every change of the live slots or of the run says so through one of these.
        for signal in (self.approvalChanged, self.stateChanged):
            signal.connect(self._needs_you_check)
        self._queue_timer = QTimer(self)
        self._queue_timer.setSingleShot(True)
        self._queue_timer.setInterval(0)
        self._queue_timer.timeout.connect(self._drain_queue)
        self.stateChanged.connect(self.queueChanged)
        self.queueChanged.connect(self.attentionChanged)
        # V2.1a M4: the open conversation's context use and all-projects memory
        # toggle, re-read on every switch (and after each turn, ``_remember``).
        self._context_usage: dict[str, Any] = dict(_UNKNOWN_USAGE)
        self._global_memory = False
        self._conversation_memory_off = False   # SEC2: the open conversation's memory-off switch
        self._memory_read_id = ""
        self.conversationChanged.connect(self._refresh_conversation_memory)
        self._refresh_conversation_memory()
        self._read_feed_sources()               # SEC2: the restored replies' sources
        # SECQMLUI: the last session-end / compaction approval batch, as the app sent it
        # (its capture thread calls the listener; the answer is queued to this thread).
        self._memory_batch: dict[str, Any] = {}
        self._memoryBatchArrived.connect(self._set_memory_batch, Qt.QueuedConnection)
        listen = getattr(self.backend, "set_memory_batch_listener", None)
        if callable(listen):
            listen(self._memoryBatchArrived.emit)
        # DFDRAFT: every conversation's own unsent draft - the open one's live
        # (its text as the composer reports it, its context above), the others
        # parked here by key; written to the preferences' drafts file shortly
        # after a change, on a switch and on quit.
        self._draft_text = ""
        self._draft_key = self._draft_key_of(self._conversation_store)
        self._drafts: dict[str, dict[str, Any]] = {}
        self._draft_held = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(800)
        self._draft_timer.timeout.connect(self._save_drafts)
        for signal in (self.groupsChanged, self.attachmentsChanged, self.modeChanged):
            signal.connect(lambda: self._draft_timer.start())
        self._load_drafts()

    # -- worker --------------------------------------------------------------

    def _run(self, kind: str, fn: Callable[[], Any], on_done: Callable[[Any], None],
             executing: bool = False) -> None:
        sequence = self._sequence.get(kind, 0) + 1
        self._sequence[kind] = sequence
        self._handlers[(kind, sequence)] = on_done
        if kind == "task":
            self._busy = True
            self._executing = executing
            self._work_started, self._first_visible, self._work_ms = self._clock(), None, None
            # The wall-clock twin of ``_work_started``: the live turn's running
            # time is drawn from it, so showing a clock never reads ``_clock``.
            self._work_started_at = time.time()
            self._started_frozen = self._config.get("execution_enabled") is False
            # Only a run delivered through ``_delivered`` (a captured origin) is recorded; only it
            # lets the person open other conversations while it runs.
            origin = getattr(on_done, "origin", None)
            # R2: a run for a conversation that is not open (background Resume) names its own identity.
            session_id, task_id = getattr(on_done, "identity", None) or (self._session_id, str(self._task.get("task_id") or ""))
            self._run_record = None if origin is None else {
                "origin": origin.id, "project": self._owner_of(origin.store, origin.id).get("id") or "",
                "request_id": self._request_id, "session_id": session_id,
                "task_id": task_id, "snapshots": getattr(on_done, "snapshots", frozenset()),
                "preview": "", "stream_ts": 0.0, "discard": False}
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
                self._end_run()
            return
        if kind == "task":
            self._busy = False
            origin = self._run_record["origin"] if self._run_record is not None else None
            if origin and origin != self._conversation_store.id:      # R3 (D1): finished while away
                self._finished_unseen.add(origin)
                self.sidebarChanged.emit()
            # DFREPLY: what the run streamed, for a reply that ends cut short in a
            # conversation that is not open (``_remember_elsewhere``).
            self._run_preview = self._run_record["preview"] if self._run_record is not None else ""
            self._end_run()
            if self._work_started is not None:
                self._work_ms = max(0, round((self._clock() - self._work_started) * 1000))
        try:
            handler(result)
        finally:
            self._run_preview = ""
        if kind == "task" and not self._busy:
            self._recheck_open()
        if kind == "task" and self._queue and not self._busy:
            self._queue_timer.start()       # DFQUEUE: any outcome lets the oldest queued message start
        if kind == "task" and self._freeze_waiting and not self._busy:
            # The operation that was running when Freeze arrived has returned
            # (at its next safe point, or finished): now JARVIS is frozen.
            self._freeze_waiting = False
            if not self.executionEnabled:
                # Lifecycle, not conversation: the live feed and the attention strip say it.
                self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen"), ()), "system")
        self._refresh_core_state()

    def _end_run(self) -> None:
        """The run record clears exactly once: finished, failed or cancelled. Snapshots it kept
        through a discard (another conversation opened, a new session) go now, unless the open
        screen's draft holds the same file."""
        record, self._run_record = self._run_record, None
        if record is None or not record["discard"]:
            return
        for path in record["snapshots"] - self._draft_snapshots():
            with contextlib.suppress(OSError):
                Path(path).unlink(missing_ok=True)

    def _draft_snapshots(self) -> frozenset[str]:
        """The snapshot files the open screen's draft, every parked draft (DFDRAFT) and every
        queued message (DFQUEUE) reference."""
        drafts = [{"groups": self._groups, "attachments": self._attachments}, *self._drafts.values(), *self._queue]
        items = [item for d in drafts for item in (*d["attachments"], *(a for g in d["groups"] for a in g["attachments"]))]
        return frozenset(item["path"] for item in items if type(item.get("path")) is str)

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

    def _say(self, kind: str, message: Any, chat: bool = False, refs: Any = None,
             owner: PersistedConversation | None = None, **extra: Any) -> None:
        if isinstance(extra.get("sources"), list):   # untrusted links: redacted like everything shown
            extra["sources"] = [{key: redact_text(str(source.get(key) or "")) for key in ("title", "url")}
                                for source in extra["sources"] if isinstance(source, dict)]
        self._entry_seq += 1
        entry = {"kind": kind, "msg": self._redacted(message), "ts": time.time(),
                 "id": f"m{self._entry_seq}", **extra}
        if chat:
            entry["chat"] = True         # a General Chat reply: part of the conversation window
        if owner is not None and owner.id != self._conversation_store.id:
            # It belongs to ANOTHER conversation: remembered there, never shown
            # in (or added to the history of) the one that is open now.
            self._remember(entry, refs, owner)
            return
        self._feed.append(entry)
        del self._feed[:-FEED_LIMIT]
        self._remember(entry, refs)
        self.feedChanged.emit()

    def _remember(self, entry: dict, refs: Any, owner: PersistedConversation | None = None) -> None:
        """Enter one shown entry into the canonical conversation, if it is one.

        What the person typed, General Chat's replies and the task results they
        were shown - exactly once each - and nothing else: never a notice, an
        approval, an error, a fixture or anything a planning component wrote. A
        reply cut short (DFREPLY) is kept as what was shown plus its marker line. A user turn keeps its own words
        apart (only they can become a pinned decision) and its references by
        identity; a result keeps its provenance as a marker, not as the content.
        """
        kind, message = entry["kind"], entry["msg"]
        store = owner or self._conversation_store
        if kind == "user" and isinstance(message, str):
            first = not any(turn.kind == "user" for turn in store.state.turns)
            turn = store.append(role="user", kind="user", message=message,
                                text=history_text(message, entry.get("groups"), entry.get("attachments")),
                                refs=refs or (), key=f"{self._store_run}:{entry['id']}")
            if first and turn is not None:
                self._name_conversation(store, message)
            if turn is not None:
                entry["turn"] = turn.id     # the stored turn 'Hatırla' names (V2.1a M4)
        elif kind == "jarvis" and entry.get("chat") and isinstance(message, str):
            # DFREPLY: a reply cut short keeps why as its last line (an ordinary reply turn).
            text = cut_reply(message, entry["cut"]) if entry.get("cut") else plain_reply(message)
            turn = store.append(role="assistant", kind="reply", text=text, key=f"{self._store_run}:{entry['id']}")
            if turn is not None:
                entry["turn"] = turn.id
                if store.id:
                    self._read_reply_sources(store.id, [turn.id])
        elif kind == "result":
            shown = render(message)
            # A result's identity is its TASK and the OUTCOME Core reported for it -
            # never its text: a re-delivered envelope (a retry, a resume, a reopen)
            # may render differently (another interface language, a truncation
            # note) and is still the same result, so it adds nothing. A task that
            # failed and later completed has two outcomes, and each is one turn.
            task = str(entry.get("task_id") or "")
            key = (f"result:{task}:{entry.get('run_status') or ''}" if task
                   else f"{self._store_run}:{entry['id']}")
            store.append(role="assistant", kind="result", key=key,
                         text=result_history(shown, entry.get("capability"), entry.get("destination")),
                         capability=str(entry.get("capability") or ""), destination=str(entry.get("destination") or ""),
                         path=str(entry.get("path") or ""))
        if store.store is not None and store.diagnostic:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "The conversation could not be saved: %1"),
                        (store.diagnostic,)), "error")
        if store is self._conversation_store and (kind == "result" or (kind == "jarvis" and entry.get("chat"))):
            self._refresh_context_usage()   # never on the user's turn: it would queue ahead of its own run

    def _conversation(self) -> tuple:
        """The canonical conversation's raw turns as ``{role, text}``.

        The invariant this upholds: everything shown as a completed assistant
        message is here exactly once, as the text that was shown, and nothing
        else is. A task result is remembered by WHAT IT SAID, with its provenance
        as a short marker on the same turn. A turn that carried context items
        sent them in full exactly ONCE, with that turn; here only a short
        provenance line per item remains.
        """
        return tuple({"role": turn.role, "text": turn.text} for turn in self._conversation_store.state.turns)

    def _conversation_state(self) -> Any:
        """The canonical conversation as the call receives it: an immutable
        snapshot (turns, compacted digests, pins) - the provider's budget is
        applied where the call is assembled (jarvis.conversation)."""
        return self._conversation_store.state

    # -- conversations -------------------------------------------------------
    #
    # Several persisted Global conversations, one of them open. Which one is
    # open at start comes from Core state (the most recently updated one), so
    # there is no second record of it. A Core session belongs to exactly one
    # conversation (``conversation_sessions``): whatever a session produces is
    # remembered by THAT conversation, whichever one is open when it arrives.
    # None of this is authority - nothing here reaches an approval, a grant, a
    # destination, a root, an identity or billing.

    conversationChanged = Signal()

    def _conversation_db(self) -> Any:
        getter = getattr(self.backend, "conversations", None)
        return getter() if callable(getter) else None

    def _open_startup_conversation(self) -> PersistedConversation:
        store = self._conversation_db()
        if store is None:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "The conversation store is unavailable, so this "
                                                              "window's conversation will not be saved."), ()), "error")
            return PersistedConversation(None, None)
        # The previous version's single conversation file is only READ: a digest
        # never seen before becomes one more conversation; the file stays as it is.
        try:
            if store.import_legacy_conversation(self.prefs.path.parent / "conversation.json") is not None:
                self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Recovered the conversation saved by the previous version"), ()),
                           "system")
        except CorruptConversation as exc:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "The previous version's conversation could not be read, "
                                                              "so it was left untouched and not used: %1"), (str(exc),)), "error")
        except Exception as exc:  # noqa: BLE001 - storage boundary; only the type
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "The previous version's conversation could not be recovered: %1"),
                        (type(exc).__name__,)), "error")
        opened = None
        try:
            records = store.list_conversations(project_id=None)
        except Exception:  # noqa: BLE001 - storage boundary
            records = []
        for record in records:
            opened = self._load_conversation(store, record.id)
            if opened is not None:
                break
        if opened is None:
            try:
                opened = PersistedConversation(store, store.create_conversation().id)
            except Exception as exc:  # noqa: BLE001 - storage boundary; only the type
                self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A new conversation could not be created: %1"),
                            (type(exc).__name__,)), "error")
                return PersistedConversation(None, None)
        if opened.state.turns:
            count = len(opened.state.turns)
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Continuing the previous conversation (1 turn)") if count == 1
                        else QT_TRANSLATE_NOOP("Bridge", "Continuing the previous conversation (%1 turns)"), (count,)), "system")
        self._restore_feed(opened.state)
        return opened

    def _load_conversation(self, store: Any, conversation_id: str) -> PersistedConversation | None:
        """The conversation, verified; ``None`` (and a note) when it cannot be trusted."""
        try:
            state = store.load_conversation_state(conversation_id)
        except Exception as exc:  # noqa: BLE001 - storage boundary; fail closed
            problem = str(exc) if isinstance(exc, CorruptConversation) else type(exc).__name__
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A saved conversation could not be read, so it was not "
                                                              "opened and nothing from it is used: %1"), (problem,)), "error")
            return None
        return PersistedConversation(store, conversation_id, state)

    # DFNEW (Salih 2026-10-12): "Yeni sohbet" is a screen; a conversation shows only
    # after its first message. A blank one (store.blank_conversation_ids) is listed
    # nowhere, and every new chat reuses its owner's newest blank one, so they never
    # pile up. Nothing is bound to a blank one: a session links only after a turn.

    @staticmethod
    def _blank_ids(store: Any) -> frozenset[str]:
        try:
            return store.blank_conversation_ids() if store is not None else frozenset()
        except Exception:  # noqa: BLE001 - a listing filter only: unreadable hides nothing
            return frozenset()

    def _new_chat_conversation(self, store: Any, project_id: str | None) -> PersistedConversation:
        """The owner's newest non-archived blank conversation, else a new one
        through the store's creation path (which refuses an unknown or archived
        owner, so none is reused for one either). Never the recorded run's
        conversation, nor the open one once its screen shows a message (its
        first turn may have failed to save). Raises what the store raises."""
        try:
            project = store.load_project(project_id) if project_id is not None else None
            if project_id is None or (project is not None and project.active):
                blank = set(store.blank_conversation_ids()) - self._queued_ids()     # DFQUEUE: a queued one exists
                blank.discard(self._run_record["origin"] if self._run_record is not None else None)
                if any(entry.get("kind") == "user" for entry in self._feed):
                    blank.discard(self._conversation_store.id)
                spare = next((r.id for r in store.list_conversations(project_id=project_id) if r.id in blank), None)
                opened = self._load_conversation(store, spare) if spare is not None else None
                if opened is not None:
                    return opened
        except Exception:  # noqa: BLE001 - storage boundary: a new one instead
            pass
        return PersistedConversation(store, store.create_conversation(project_id).id)

    def _restore_feed(self, state: Any) -> None:
        """The opened conversation's remembered turns, shown again - the same
        words that were shown, never re-remembered."""
        feed = []
        for turn in state.turns:
            notice = None
            if turn.kind == "user":
                entry: dict[str, Any] = {"kind": "user", "msg": self._redacted(turn.message or turn.text), "turn": turn.id}
            elif turn.kind == "reply":
                shown, cut = split_cut(turn.text)
                entry = {"kind": "jarvis", "msg": self._redacted(shown), "chat": True, "turn": turn.id}
                if cut:                         # DFREPLY: cut short - and the notice under it again
                    reply, line = cut_entries(shown, cut)
                    entry.update(cut=cut, interrupted=reply["interrupted"])
                    notice = {"kind": line["kind"], "msg": line["text"]}
            else:
                text = turn.text.split("\n", 1)[1] if turn.text.startswith("[Task result") and "\n" in turn.text else turn.text
                entry = {"kind": "result", "msg": self._redacted(text), "capability": turn.capability,
                         "destination": turn.destination}
            for item in (entry, notice) if notice is not None else (entry,):
                self._entry_seq += 1
                item.update(ts=float(turn.ts), id=f"m{self._entry_seq}", restored=True)
                feed.append(item)
        self._feed = feed[-FEED_LIMIT:]
        self.feedChanged.emit()

    def _read_feed_sources(self) -> None:
        """The restored replies' sources; called once the opened conversation is set."""
        replies = [entry["turn"] for entry in self._feed if entry.get("kind") == "jarvis" and entry.get("turn")]
        if replies and self._conversation_store.id:
            self._read_reply_sources(self._conversation_store.id, replies)

    def _read_reply_sources(self, conversation_id: str, turn_ids: list[str]) -> None:
        """SEC2 (I22): each reply's usedMemory / memoryNoneRelevant, as the app
        answers them, set on its feed entry unchanged; the bridge picks nothing."""
        if self._shutting_down:
            return
        self._run(f"reply-sources:{uuid4().hex}",
                  lambda: {turn: self.backend.reply_memory_sources(conversation_id, turn) for turn in turn_ids},
                  lambda answers: self._reply_sources_answer(conversation_id, answers))

    def _reply_sources_answer(self, conversation_id: str, answers: Any) -> None:
        if not isinstance(answers, dict) or conversation_id != (self._conversation_store.id or ""):
            return   # it landed for a conversation no longer open
        changed = False
        for entry in self._feed:
            sources = answers.get(entry.get("turn")) if entry.get("kind") == "jarvis" else None
            if isinstance(sources, dict) and isinstance(sources.get("usedMemory"), list):
                entry.update(usedMemory=sources["usedMemory"], memoryNoneRelevant=sources.get("memoryNoneRelevant") is True)
                changed = True
        if changed:
            self.feedChanged.emit()

    def _name_conversation(self, conversation: PersistedConversation, message: str) -> None:
        """A conversation is named from its first message, once. DFNEW: named
        or not, the lists are read again - the first message makes its row."""
        title = task_title(message)[:120]
        if conversation.store is None:
            return
        try:
            # Never over a name the person already gave (Gate 6D-a).
            record = conversation.store.load_conversation(conversation.id)
            if title and record is not None and not record.title:
                conversation.store.set_conversation_title(conversation.id, title)
        except Exception:  # noqa: BLE001 - a label only; the conversation is unaffected
            pass
        self._refresh_conversations()

    @staticmethod
    def _listed_title(record: Any) -> Any:
        return record.title or (("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ())
                                if record.origin == "legacy-import"
                                else ("Bridge", QT_TRANSLATE_NOOP("Bridge", "New Chat"), ()))

    def _refresh_conversations(self) -> None:
        store = self._conversation_db()
        # ONE store read per refresh, every owner: Global's lists, the open scope's list and the
        # per-project counts are all cut from it (never a query per project).
        listed = store is not None
        try:
            everything = store.list_conversations(include_archived=True) if store is not None else []
        except Exception:  # noqa: BLE001 - storage boundary
            everything, listed = [], False
        # DFNEW: a New chat is listed nowhere until its first message (DFQUEUE: or a queued one).
        blank = self._blank_ids(store) - self._queued_ids()
        everything = [r for r in everything if r.id not in blank]
        records = [r for r in everything if r.project_id is None]
        self._conversation_list = [
            {"id": r.id, "origin": r.origin, "updated_at": r.updated_at, "title": self._listed_title(r)}
            for r in records if r.archived_at is None
        ]
        # Gate 6D-b: History's ARCHIVED section (Global only; a project's are on its page).
        self._archived_conversations = [
            {"id": r.id, "origin": r.origin, "updated_at": r.updated_at, "archived_at": r.archived_at,
             "title": self._listed_title(r)}
            for r in records if r.archived_at is not None
        ]
        open_id = self._conversation_store.id if hasattr(self, "_conversation_store") else None
        self._new_chat_open = bool(open_id) and open_id in blank
        readable = store is not None and bool(open_id)
        try:
            open_record = store.load_conversation(open_id) if readable else None
        except Exception:  # noqa: BLE001 - a label only
            open_record, readable = None, False
        if readable:
            live = open_record is not None and open_record.archived_at is None
            seen_id, was_live = self._open_seen
            if open_id == seen_id and (open_record is None or (was_live and not live)):
                # N2: deleted, or archived since it was opened, by any path (incl. a change
                # in the store): never stay on it. An archived one opened read-only stays.
                # While a run without a record (a sweep) is busy it is left when the run
                # ends (_on_finished); a recorded run leaves navigation open (R1).
                if not self.navigationLocked:
                    self._open_seen = ("", False)
                    self._leave_open_conversation(*self._open_scope())
                    return
            else:
                self._open_seen = (open_id, live) if open_record is not None else ("", False)
        self._conversation_archived = open_record is not None and open_record.archived_at is not None
        self._open_title = "" if open_record is None else (open_record.title or (
            ("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ()) if open_record.origin == "legacy-import" else ""))
        self._conversation_owner = self._owner_of(store, open_id)
        owner_id = self._conversation_owner.get("id") or ""
        project = self._read_project_texts(store, owner_id)       # D-4: read once, used by both
        self._scope_facts = self._read_scope_facts(store, owner_id or None, project)
        self._work_panel = self._read_work_panel(store, owner_id, project)
        scope_owner = self._conversation_owner.get("id") or None
        scope = [r for r in everything if r.project_id == scope_owner and r.archived_at is None]
        self._scope_conversations = [
            {"id": r.id, "updated_at": r.updated_at,
             "title": r.title or (("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ())
                                  if r.origin == "legacy-import" else "")}
            for r in scope
        ]
        # Gate 6G Sidebar v2: every owner's non-archived conversations, newest first ("" = no project).
        self._sidebar_lists = {}
        for r in everything:
            if r.archived_at is None:
                self._sidebar_lists.setdefault(r.project_id or "", []).append(
                    {"id": r.id, "updated_at": r.updated_at,
                     "title": r.title or (("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ())
                                          if r.origin == "legacy-import" else "")})
        # Gate 6F F6: pins of conversations no longer listed (archived, deleted, unknown) are
        # pruned on this read - never on a failed one, which lists nothing.
        if listed:
            self.prefs.prune_sidebar({r.id for r in everything if r.archived_at is None},
                                     {r.project_id for r in everything if r.project_id})
            self._prune_parked()
        self._pin_records = {r.id: r for r in everything if r.archived_at is None and r.id in self.prefs.sidebar["pins"]}
        if any(r.project_id for r in self._pin_records.values()):
            try:
                self._pin_projects = {p.id: {"id": p.id, "name": p.name, "active": bool(p.active)} for p in store.list_projects()}
            except Exception:  # noqa: BLE001 - a label only
                self._pin_projects = {}
        counts: dict[str, list[int]] = {}
        for r in everything:
            if r.project_id is not None:
                counts.setdefault(r.project_id, [0, 0])[0 if r.archived_at is None else 1] += 1
        counts_now = {pid: (live, archived) for pid, (live, archived) in counts.items()}
        counts_changed = counts_now != self._project_counts
        self._project_counts = counts_now
        self.conversationChanged.emit()
        if counts_changed:                       # the project list's badges follow, with no manual refresh
            self.projectsChanged.emit()

    @staticmethod
    def _owner_of(store: Any, conversation_id: str | None) -> dict[str, Any]:
        """The owner project of a conversation as the store reports it:
        ``{}`` for Global (or when it cannot be read)."""
        if store is None or not conversation_id:
            return {}
        try:
            record = store.load_conversation(conversation_id)
            if record is None or record.project_id is None:
                return {}
            project = store.load_project(record.project_id)
        except Exception:  # noqa: BLE001 - a label only
            return {}
        if project is None:
            return {"id": record.project_id, "name": record.project_id, "active": False}
        return {"id": project.id, "name": project.name, "active": bool(project.active)}

    @staticmethod
    def _read_project_texts(store: Any, project_id: str) -> dict[str, Any]:
        """D-4, read only, once per refresh: the project's CURRENT instruction
        text ("text") and its active file names ("files"), for the scope facts
        and the work panel. A key is left out when its read failed (or the one
        before it did); {} for no store or no project."""
        read: dict[str, Any] = {}
        if store is None or not project_id:
            return read
        try:
            current = store.load_project_instruction(project_id)
            read["text"] = current.text if current is not None else ""
            read["files"] = [str(f["display_name"]) for f in store.list_project_files(project_id)
                             if f.get("lifecycle") == project_files.ACTIVE]
        except Exception:  # noqa: BLE001 - a label only: what was read stays
            pass
        return read

    @staticmethod
    def _read_scope_facts(store: Any, project_id: str | None, project: dict[str, Any]) -> dict[str, Any]:
        """Gate 6F (tiles T2, T4), read only: what the model sees in the scope -
        its current instruction and active files (a project) - and the scope's
        active memory records: their count and the newest write. Genel has no
        records of its own, so its newest write is across every active project."""
        facts: dict[str, Any] = {"instruction": False, "file_count": 0, "memory_count": 0, "memory_at": ""}
        if store is None:
            return facts
        try:
            if project_id:
                if "files" not in project:
                    return facts
                facts["instruction"] = bool(project["text"].strip())
                facts["file_count"] = len(project["files"])
                items = store.list_memory_items(project_id)
            else:
                # ponytail: one listing per active project; a store-wide "newest item" query if projects grow many
                items = [i for p in store.list_projects() if p.active for i in store.list_memory_items(p.id)]
        except Exception:  # noqa: BLE001 - a label only
            return facts
        facts["memory_count"] = len(items)
        facts["memory_at"] = max((i.updated_at or i.created_at for i in items), default="")
        return facts

    @Property("QVariantList", notify=conversationChanged)
    def conversations(self) -> list:
        """The Global conversations, newest first, the open one marked."""
        return [{**row, "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                 "active": row["id"] == self._conversation_store.id} for row in self._conversation_list]

    @Property(str, notify=conversationChanged)
    def conversationId(self) -> str:
        return self._conversation_store.id or ""

    @Property(bool, notify=conversationChanged)
    def newChatOpen(self) -> bool:
        """DFSIDE: the open conversation is a New chat screen (blank, listed
        nowhere) - the sidebar shows its New chat row as the current one. Read only."""
        return getattr(self, "_new_chat_open", False)

    # -- Gate 6D-a: the top-bar conversation switcher and its rename ----------
    #
    # A label only: a title is never read by any authority decision. The
    # switcher lists the CURRENT scope's conversations (the open one's owner,
    # Global when Global), newest first; switching is openConversation. A
    # rename goes through the app's explicit-user result; the store alone
    # decides the length limit and the archived refusals.

    renameNoticeChanged = Signal()

    @Property("QVariantList", notify=conversationChanged)
    def scopeConversations(self) -> list:
        """Non-archived conversations of the open conversation's scope; an
        untitled one has title "" (the window says "New Chat")."""
        return [{"id": row["id"], "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                 "updated_at": row["updated_at"], "active": row["id"] == self._conversation_store.id}
                for row in self._scope_conversations]

    @Property(str, notify=conversationChanged)
    def conversationTitle(self) -> str:
        """The open conversation's stored name, "" when it has none (also
        while it is archived and so not in the switcher's list)."""
        title = self._open_title
        return render(title) if isinstance(title, tuple) else title

    @Property(str, notify=renameNoticeChanged)
    def conversationRenameNotice(self) -> str:
        return self._rename_notice

    @staticmethod
    def _normalized_title(text: str) -> str:
        """Newlines and control characters become single spaces, then trimmed."""
        return " ".join("".join(" " if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") else ch
                                for ch in str(text)).split())

    @Slot()
    def clearRenameNotice(self) -> None:
        self._rename_notice = ""
        self.renameNoticeChanged.emit()

    @Slot(str, str, result=bool)
    def renameConversation(self, conversation_id: str, title: str) -> bool:
        """Commit a rename of EXACTLY ``conversation_id`` (the one captured
        when the edit started, never whichever is open now). Refused while it
        runs (R1). Empty after normalizing: no call, the old name stays."""
        self._rename_notice = ""
        normalized = self._normalized_title(title)
        if self._runs_in(str(conversation_id)):
            self._rename_notice = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Wait for the current run to finish."))
        elif not str(conversation_id):
            self._rename_notice = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "This conversation is not saved yet."))
        elif not normalized:
            self._rename_notice = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Type a name; the old name is kept."))
        else:
            payload = self.backend.conversation_rename(str(conversation_id), normalized)
            self._rename_notice = self._error_message(payload)
        ok = self._rename_notice == ""
        if ok:
            self._refresh_conversations()
            self.refreshProjects()
        self.renameNoticeChanged.emit()
        return ok

    def _activate(self, opened: PersistedConversation, park: bool = True) -> None:
        """Open ``opened``: its history on screen, a fresh Core session for it -
        or, back on the conversation a recorded run is working for (R1), that
        run's session and its live stream. R2: the conversation left keeps
        what waits for the person or Resume (``park``: not when it is left
        because it was deleted or archived), and ``opened`` gets back what it
        kept, once proven still its own."""
        left = self._conversation_store.id
        if park and opened.id != left:                            # re-opening the same one (attach) starts fresh
            self._park(left, self._live_slots())
            if left:                                              # SessionEnd; archive fires from the backend, delete never
                self._memory_hook("memory_capture_left", left)
        else:
            self._parked.pop(left, None)
        self._conversation_store = opened
        self._finished_unseen.discard(opened.id)                  # R3 (D1/D2): green clears on open
        record = self._run_record if self.openRunning else None
        restored = self._unpark(opened)
        self._set_live(restored or _empty_slots())
        self._restored_session = restored["session_id"] if restored else ""
        if record is not None:
            self._session_id = record["session_id"]
        self._decision = {}
        self._swap_draft(opened)
        self._end_stream()
        if record is not None and record["preview"]:
            self._stream_text, self._stream_ts = record["preview"], record["stream_ts"]
            self.streamChanged.emit()
        self._restore_feed(opened.state)
        self._read_feed_sources()
        self._refresh_conversations()
        self.approvalChanged.emit()
        self._refresh_core_state()

    @Slot(result=bool)
    def newConversation(self) -> bool:
        """Open a new Global conversation (DFNEW: Global's blank one when there is one)."""
        if self.navigationLocked:
            return False
        store = self._conversation_db()
        if store is None:
            return False
        try:
            opened = self._new_chat_conversation(store, None)
        except Exception as exc:  # noqa: BLE001 - storage boundary; only the type
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A new conversation could not be created: %1"),
                        (type(exc).__name__,)), "error")
            return False
        self._activate(opened)
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "New conversation"), ()), "system")
        return True

    @Slot(str, result=bool)
    def openConversation(self, conversation_id: str) -> bool:
        """Open a saved conversation; its history returns to the screen and its
        sidebar date group opens (F6)."""
        return self._open_conversation(str(conversation_id), expand=True)

    def _open_conversation(self, conversation_id: str, expand: bool) -> bool:
        if self.navigationLocked:
            return False
        if conversation_id == self._conversation_store.id:
            return True
        store = self._conversation_db()
        if store is None:
            return False
        opened = self._load_conversation(store, conversation_id)
        if opened is None:
            if self._parked.pop(conversation_id, None) is not None:   # R2: unreadable keeps nothing
                self._needs_you_check()
            return False
        self._activate(opened)
        if expand:
            self._expand_group_of(conversation_id)
        count = len(opened.state.turns)
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Opened a conversation (1 turn)") if count == 1
                    else QT_TRANSLATE_NOOP("Bridge", "Opened a conversation (%1 turns)"), (count,)), "system")
        return True

    # -- projects (Gate 6A): the window's view of the Gate 1-5 project model --
    #
    # Presentation and existing canonical paths only. Lifecycle changes go
    # through the app-level explicit-user results; a new project conversation
    # goes through the store's own creation path, which refuses an unknown or
    # archived owner by itself. Core's refusals are shown as returned.

    projectsChanged = Signal()

    @Property(dict, notify=conversationChanged)
    def conversationProject(self) -> dict:
        """The open conversation's owner: name and state, or Global; with the
        sidebar's read-only scope facts (instruction, file_count, memory_count,
        memory_at - Gate 6F tiles T2, T4)."""
        owner = self._conversation_owner
        if not owner:
            return {"id": "", "name": tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Global")), "global": True, "archived": False,
                    **self._scope_facts}
        return {"id": owner["id"], "name": owner["name"], "global": False, "archived": not owner["active"], **self._scope_facts}

    @Property(bool, notify=conversationChanged)
    def conversationReadOnly(self) -> bool:
        """The open conversation belongs to an archived project, or is itself
        archived (Gate 6D-b). Presentation only: Core's archive refusals
        remain the enforcement."""
        project_archived = bool(self._conversation_owner) and not self._conversation_owner.get("active", False)
        return project_archived or self._conversation_archived

    @Property(bool, notify=conversationChanged)
    def conversationArchived(self) -> bool:
        """The open conversation itself is archived (read-only until unarchived)."""
        return self._conversation_archived

    # -- G6W3: the work panel of a PROJECT chat (W1/W3), read only -------------
    #
    # What this chat works with, as stored: the owner project's current
    # instruction, its active files, its linked folder's NAME (never the path;
    # the path stays in the project page's Klasör dialog) and the capabilities
    # of the task results this conversation recorded. {} for a plain chat (W1:
    # no panel there). Nothing here reaches a model payload.

    workPanelChanged = Signal()

    def _read_work_panel(self, store: Any, project_id: str, project: dict[str, Any]) -> dict[str, Any]:
        if store is None or not project_id:
            return {}
        panel: dict[str, Any] = {"instruction": "", "instruction_chars": 0, "files": [], "folder": ""}
        text = project.get("text", "")
        if text.strip():
            panel["instruction"], panel["instruction_chars"] = " ".join(text.split())[:300], len(text)
        if "files" not in project:
            return panel
        panel["files"] = list(project["files"])
        try:
            link = self._folder_result("show", project_id).get("folder")
            panel["folder"] = Path(link["path"]).name if link else ""
        except Exception:  # noqa: BLE001 - a label only: what was read stays
            pass
        return panel

    @Property("QVariantMap", notify=conversationChanged)
    def workPanel(self) -> dict:
        """{} in a plain chat; in a project chat: instruction (a short preview,
        "" = none), instruction_chars, files (active names), folder (the linked
        folder's name only, "" = none)."""
        return dict(self._work_panel)

    @Property("QVariantList", notify=feedChanged)
    def workPanelCapabilities(self) -> list:
        """The capabilities of the task results the open conversation recorded
        (raw turns and compacted digests), first use first. Read with the feed
        (a switch restores it before the owner is known), so the panel, never
        shown in a plain chat, is what decides whether it is seen."""
        state = self._conversation_store.state
        seen = [c.capability for c in state.compacted] + [t.capability for t in state.turns]
        return list(dict.fromkeys(c for c in seen if c))

    @Property(bool, notify=workPanelChanged)
    def workPanelCollapsed(self) -> bool:
        return self.prefs.work_panel_collapsed

    @Slot(bool)
    def setWorkPanelCollapsed(self, collapsed: bool) -> None:
        if self.prefs.set_work_panel_collapsed(bool(collapsed)):
            self.workPanelChanged.emit()

    @Property("QVariantList", notify=projectsChanged)
    def projectRecords(self) -> list:
        """Every project with its conversation counts (live, archived) and its
        instruction's first non-blank line ("" = none or cleared) - read-only presentation keys."""
        return [{**record, "conversation_count": self._project_counts.get(record["id"], (0, 0))[0],
                 "archived_count": self._project_counts.get(record["id"], (0, 0))[1],
                 "instruction_line": self._instruction_lines.get(record["id"], "")} for record in self._project_records]

    @Property(dict, notify=projectsChanged)
    def openProject(self) -> dict:
        return next((dict(r) for r in self._project_records if r["id"] == self._open_project), {})

    @Property("QVariantList", notify=projectsChanged)
    def projectConversations(self) -> list:
        """The open project's non-archived conversations. Under an archived
        project each one is deletable at once: the project's archive is the
        reversible first step (Gate 6D-b, Salih decision 2026-09-30)."""
        archived = self.openProject.get("active") is False
        return [{**row, "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                 "active": row["id"] == self._conversation_store.id, "deletable": archived}
                for row in self._project_conversations]

    @Property("QVariantList", notify=projectsChanged)
    def projectArchivedConversations(self) -> list:
        """The open project's archived conversations (its ARCHIVED section)."""
        return [{**row, "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                 "active": row["id"] == self._conversation_store.id} for row in self._project_archived_conversations]

    @Property("QVariantMap", notify=projectsChanged)
    def projectFolder(self) -> dict:
        """Gate 6G P3: the open project's CURRENT folder link (6H H1) as the
        store keeps it - {path, linked_at} - or {} when none. Context for the
        local user, never authority; linkProjectFolder / unlinkProjectFolder
        change it, nothing here creates a folder."""
        return dict(self._project_folder)

    @Property(str, notify=projectsChanged)
    def projectFolderNotice(self) -> str:
        return self._project_folder_notice

    @Property("QVariantList", notify=projectsChanged)
    def projectJobs(self) -> list:
        return list(self._project_jobs)

    @Property(bool, notify=projectsChanged)
    def projectJobsBounded(self) -> bool:
        """The job listing is the store's FIRST N by creation: when it is full,
        this project may have more jobs than it shows."""
        return self._project_jobs_bounded

    @Property(str, notify=projectsChanged)
    def projectNotice(self) -> str:
        return self._project_notice

    @Slot()
    def refreshProjects(self) -> None:
        self._project_records = self.backend.project_records()
        if self._open_project and not any(r["id"] == self._open_project for r in self._project_records):
            self._open_project = ""
        self._instruction_lines = self._read_instruction_lines()
        self._load_project_detail()
        self._refresh_conversations()           # the open one's owner and scope facts; N2 if it is gone
        self.projectsChanged.emit()

    def _read_instruction_lines(self) -> dict[str, str]:
        """Gate 6G P2, read only: each project card's line - the first non-blank
        line of the project's CURRENT stored instruction ("" when none or cleared)."""
        store = self._conversation_db()
        lines: dict[str, str] = {}
        if store is None:
            return lines
        for record in self._project_records:
            try:
                current = store.load_project_instruction(record["id"])
            except Exception:  # noqa: BLE001 - a label only
                continue
            text = current.text if current is not None else ""
            lines[record["id"]] = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return lines

    def _load_project_detail(self) -> None:
        self._project_conversations, self._project_jobs, self._project_jobs_bounded = [], [], False
        self._project_archived_conversations = []
        self._project_folder = {}
        self._load_project_instruction()
        self._load_project_files()
        if not self._open_project:
            return
        store = self._conversation_db()
        try:
            shown = self._folder_result("show", self._open_project)
            link = shown.get("folder")
            if (not self._project_folder_notice                                # a fresher refusal stays
                    and self._error_message(shown) == PROJECT_FOLDER_LINK_UNREADABLE):   # V21K3: said, never "no folder"
                self._project_folder_notice = self._folder_reason(PROJECT_FOLDER_LINK_UNREADABLE)
        except Exception:  # noqa: BLE001 - a label only; an unreadable link is not shown
            link = None
        if link:
            self._project_folder = {"path": link["path"], "linked_at": link["linked_at"]}
        try:
            records = (store.list_conversations(project_id=self._open_project, include_archived=True)
                       if store is not None else [])
        except Exception:  # noqa: BLE001 - storage boundary
            records = []
        blank = self._blank_ids(store) - self._queued_ids()     # DFQUEUE: a queued one is listed
        rows_of = [
            {"id": r.id, "updated_at": r.updated_at, "archived_at": r.archived_at,
             "title": r.title or ("Bridge", QT_TRANSLATE_NOOP("Bridge", "New Chat"), ())}
            for r in records if r.id not in blank
        ]
        self._project_conversations = [row for row in rows_of if row["archived_at"] is None]
        self._project_archived_conversations = [row for row in rows_of if row["archived_at"] is not None]
        payload = self.backend.jobs(True)
        listing = rows(payload, "jobs", "id")
        self._project_jobs = [job for job in listing if job.get("project_id") == self._open_project]
        limit = payload.get("limit") if isinstance(payload, dict) else None
        self._project_jobs_bounded = isinstance(limit, int) and len(listing) >= limit

    @Slot(str)
    def showProject(self, project_id: str) -> None:
        """Open a project's detail on the Projects page ("" = the list)."""
        pending = self._new_project_chat
        if project_id and pending is not None:
            # G6SB3F: the new-project dialog opens the project it created; asked from
            # the picker, that project becomes the still-empty new chat's instead.
            self._new_project_chat = None
            chat, before = pending
            if (str(project_id) not in before and self._conversation_store.id == chat
                    and self.setNewChatProject(str(project_id))):
                self.setPage("home")
                return
        self._open_project = str(project_id)
        self._project_notice = self._project_instruction_notice = self._project_file_notice = ""
        self._project_folder_notice = ""
        self.setPage("projects")
        self.refreshProjects()

    def _project_answer(self, payload: Any) -> None:
        error = payload.get("error") if isinstance(payload, dict) else None
        self._project_notice = str(error.get("message") or "") if isinstance(error, dict) else ""
        self.refreshProjects()

    @Slot(str)
    def createProject(self, name: str) -> None:
        self._run("project", lambda: self.backend.project_create(str(name)), self._project_answer)

    @Slot(str, str)
    def renameProject(self, project_id: str, name: str) -> None:
        self._run("project", lambda: self.backend.project_lifecycle("rename", str(project_id), str(name)),
                  self._project_answer)

    @Slot(str)
    def archiveProject(self, project_id: str) -> None:
        if self._busy and self._run_record is not None and self._run_in_project(str(project_id)):
            # R1: the conversation JARVIS is working for belongs to this project (fail closed).
            self._project_notice = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Wait for the current run to finish."))
            self.projectsChanged.emit()
            return
        self._run("project", lambda: self.backend.project_lifecycle("archive", str(project_id)), self._project_answer)

    def _run_in_project(self, project_id: str) -> bool:
        """The recorded run's conversation belongs to ``project_id`` - read now; a
        failed read says yes (fail closed)."""
        store = self._conversation_db()
        try:
            record = store.load_conversation(self._run_record["origin"]) if store is not None else None
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            return True
        return (record.project_id if record is not None else self._run_record["project"]) == project_id

    @Slot(str)
    def unarchiveProject(self, project_id: str) -> None:
        self._run("project", lambda: self.backend.project_lifecycle("unarchive", str(project_id)), self._project_answer)

    @Slot(str, result=bool)
    def newProjectConversation(self, project_id: str) -> bool:
        """A new conversation OWNED by ``project_id``, opened (DFNEW: that
        owner's blank one when there is one). The store's own creation path
        decides: an unknown or archived owner is refused inside its write
        transaction and nothing is created. The owner is fixed."""
        if self.navigationLocked:
            return False
        store = self._conversation_db()
        if store is None:
            return False
        try:
            opened = self._new_chat_conversation(store, str(project_id))
        except Exception as exc:  # noqa: BLE001 - storage boundary: shown as returned
            self._project_notice = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.projectsChanged.emit()
            return False
        self._project_notice = ""
        self._activate(opened)
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "New conversation"), ()), "system")
        self.refreshProjects()
        return True

    # -- Gate 6B: instruction editor, file panel, top-bar project selector ----
    #
    # Presentation over the existing explicit-user app paths
    # (project_instruction_result / project_file_result); the store decides
    # and its refusals are shown as returned. Nothing here grants, approves
    # or selects a destination or connector.

    projectInstructionLoaded = Signal()

    @staticmethod
    def _error_message(payload: Any) -> str:
        error = payload.get("error") if isinstance(payload, dict) else None
        return str(error.get("message") or "") if isinstance(error, dict) else ""

    def _load_project_instruction(self) -> None:
        current: dict[str, Any] = {}
        if self._open_project:
            payload = self.backend.project_instruction("history", self._open_project)
            versions = payload.get("versions") if isinstance(payload, dict) else None
            current = next((dict(v) for v in versions or () if v.get("superseded_by") is None), {})
        self._project_instruction = current
        key = (self._open_project, current.get("id"))
        if key != self._project_instruction_key:
            # Only a different stored version reloads the editor: a refresh
            # for any other reason never overwrites what is being typed.
            self._project_instruction_key = key
            self.projectInstructionLoaded.emit()

    def _load_project_files(self) -> None:
        self._project_files = []
        if not self._open_project:
            return
        payload = self.backend.project_file("list", self._open_project)
        error = self._error_message(payload)
        if error:
            self._project_file_notice = error
            return
        self._project_files = [dict(row) for row in payload.get("files") or ()]

    @Property(dict, notify=projectsChanged)
    def projectInstruction(self) -> dict:
        """The CURRENT stored version: text (possibly an empty clear) and its
        classification, or {} when the project never had one."""
        return dict(self._project_instruction)

    @Property(int, constant=True)
    def projectInstructionCap(self) -> int:
        return PROJECT_INSTRUCTION_TEXT_CAP

    @Property(str, notify=projectsChanged)
    def projectInstructionNotice(self) -> str:
        return self._project_instruction_notice

    @Slot(str, str, str)
    def saveProjectInstruction(self, project_id: str, text: str, classification: str) -> None:
        """Save (or, with empty text, clear) through the app path. The editor
        then shows the STORED text; when the store's hygiene changed it, that
        is said."""
        typed = str(text)

        def done(payload: Any) -> None:
            error = self._error_message(payload)
            if error:
                # Refused, nothing written: what was typed stays in the editor.
                self._project_instruction_notice = error
            else:
                stored = (payload.get("instruction") or {}).get("text", "")
                self._project_instruction_notice = "" if stored == normalize_retrieved_text(typed) else tx(
                    "Bridge", QT_TRANSLATE_NOOP("Bridge", "Saved. The store redacted credentials or local paths, "
                                                          "so the stored text shown here differs from what you typed."))
                self._project_instruction_key = None
            self.refreshProjects()

        self._run("project-instruction", lambda: self.backend.project_instruction(
            "set", str(project_id), typed, str(classification) or None), done)

    @Property("QVariantList", notify=projectsChanged)
    def projectFiles(self) -> list:
        """The open project's files as the store lists them, oldest first.
        Purge is the second step of retire -> purge: offered on RETIRED rows;
        on an archived project (where retire is refused) also on ACTIVE rows,
        because a privacy deletion is never blocked (ARCHITECTURE 4.24)."""
        archived = self.openProject.get("active") is False
        return [{**row, "purgeable": row.get("lifecycle") == project_files.RETIRED
                 or (archived and row.get("lifecycle") == project_files.ACTIVE)}
                for row in self._project_files]

    @Property(dict, notify=projectsChanged)
    def projectFileCounts(self) -> dict:
        kept = [row for row in self._project_files if row.get("lifecycle") != project_files.PURGED]
        return {"active": sum(row.get("lifecycle") == project_files.ACTIVE for row in kept),
                "active_max": project_files.PROJECT_FILE_ACTIVE_MAX,
                "retained": len(kept), "retained_max": project_files.PROJECT_FILE_RETAINED_MAX,
                "chars": sum(int(row.get("char_count") or 0) for row in kept),
                "chars_max": project_files.PROJECT_FILE_RETAINED_CHARS_MAX}

    @Property(str, notify=projectsChanged)
    def projectFileNotice(self) -> str:
        return self._project_file_notice

    def _file_answer(self, payload: Any) -> None:
        self._project_file_notice = self._error_message(payload)
        self.refreshProjects()

    def _file_action(self, action: str, project_id: str, **options: Any) -> None:
        self._run("project-file", lambda: self.backend.project_file(action, str(project_id), **options),
                  self._file_answer)

    @Slot(str, str, str)
    def addProjectFile(self, project_id: str, url: str, classification: str) -> None:
        """Add the ONE file the person picked, with the classification they chose."""
        local = QUrl(str(url)).toLocalFile() if str(url).startswith("file:") else str(url)
        self._file_action("add", project_id, path=local, classification=str(classification) or None)

    @Slot(str, str)
    def retireProjectFile(self, project_id: str, file_id: str) -> None:
        self._file_action("retire", project_id, file_id=str(file_id))

    @Slot(str, str)
    def restoreProjectFile(self, project_id: str, file_id: str) -> None:
        self._file_action("restore", project_id, file_id=str(file_id))

    @Slot(str, str, str, result=bool)
    def purgeProjectFile(self, project_id: str, file_id: str, typed_name: str) -> bool:
        """Permanent purge of exactly the chosen row. The typed file name is
        the window's gate (display names are not unique, so the ROW's id is
        what the store is given as its own confirmation); a mismatch, or a
        row the panel does not offer purge on, calls nothing."""
        row = next((r for r in self.projectFiles if r.get("id") == str(file_id)), None)
        if row is None or not row["purgeable"] or str(typed_name) != row.get("display_name"):
            return False
        self._file_action("purge", project_id, file_id=row["id"], confirm=row["id"])
        return True

    # -- Gate 6H (G6H2): the project folder link, CONTEXT for the local user --
    #
    # Only through the app's audited project_folder_result (show / link /
    # unlink; Gate 6H A4): the bridge never names the store's link methods.
    # The path is the person's own pick; JARVIS never reads, writes or sends
    # the folder (Salih Q1, 2026-10-10). Nothing here creates or deletes one.

    # The backend's refusals (English, for logs: stable codes) in plain words;
    # any other message is one generic line, never shown raw (D-6).
    _FOLDER_REASONS = {
        "A folder path must be a non-blank string.": QT_TRANSLATE_NOOP("Bridge", "No folder was chosen."),
        "A folder path may not contain control or bidi characters.":
            QT_TRANSLATE_NOOP("Bridge", "The folder path contains characters that cannot be used."),
        "A folder path must already be in normal form.":
            QT_TRANSLATE_NOOP("Bridge", "The folder path is not in plain form (it has . or .. or doubled separators)."),
        "A folder name may not contain < > : \" | ? *.":
            QT_TRANSLATE_NOOP("Bridge", "A folder name in this path cannot be used on Windows."),
        "A folder name may not end with a dot or a space.":
            QT_TRANSLATE_NOOP("Bridge", "A folder name in this path cannot be used on Windows."),
        "A folder name may not be a Windows device name.":
            QT_TRANSLATE_NOOP("Bridge", "A folder name in this path cannot be used on Windows."),
        "A folder path may not use a short (8.3) name.":
            QT_TRANSLATE_NOOP("Bridge", "The folder path uses a short (8.3) name. Pick the folder by its full name."),
        "A drive root cannot be a project folder.": QT_TRANSLATE_NOOP("Bridge", "A whole drive cannot be a project folder."),
        "The user profile folder, or a folder containing it, cannot be a project folder.":
            QT_TRANSLATE_NOOP("Bridge", "Your user folder itself, or a folder around it, cannot be a project folder. Pick a folder inside it."),
        "A Windows or program folder cannot be a project folder.":
            QT_TRANSLATE_NOOP("Bridge", "A Windows or program folder cannot be a project folder."),
        "A JARVIS data, settings, install or backup folder cannot be a project folder.":
            QT_TRANSLATE_NOOP("Bridge", "JARVIS's own data, settings, install or backup folders cannot be a project folder."),
        "Project folders are supported on Windows only.":
            QT_TRANSLATE_NOOP("Bridge", "Project folders are supported on Windows only."),
        "A project folder must be on a local fixed drive.":
            QT_TRANSLATE_NOOP("Bridge", "The folder must be on a local fixed drive. Network, removable and other drives cannot be linked."),
        "The folder does not exist.": QT_TRANSLATE_NOOP("Bridge", "The folder does not exist."),
        "The folder could not be inspected.": QT_TRANSLATE_NOOP("Bridge", "The folder could not be checked."),
        "A folder path may not pass through a link or junction.":
            QT_TRANSLATE_NOOP("Bridge", "This folder is a link or junction, or lies inside one (an online-only OneDrive "
                                        "folder can be one). Such folders are always refused; pick a regular folder."),
        "The path is not a folder.": QT_TRANSLATE_NOOP("Bridge", "The chosen path is not a folder."),
        "The folder could not be proven to be the named one.":
            QT_TRANSLATE_NOOP("Bridge", "The folder could not be verified as the one you picked (a substituted drive or a redirect)."),
        "The project already has a linked folder; unlink it first.":
            QT_TRANSLATE_NOOP("Bridge", "This project already has a linked folder. Unlink it first."),
        "That folder, or one inside or around it, is linked to another project.":
            QT_TRANSLATE_NOOP("Bridge", "That folder, or a folder inside or around it, is linked to another project."),
        PROJECT_FOLDER_LINK_UNREADABLE:
            QT_TRANSLATE_NOOP("Bridge", "A project folder link could not be read, so no folder is shown or offered. "
                                        "Nothing was changed."),
        # 6H K1 / K3: proposing and creating a folder.
        "project name must be a non-blank string": QT_TRANSLATE_NOOP("Bridge", "The project needs a name."),
        "The Desktop or Documents folder could not be found.":
            QT_TRANSLATE_NOOP("Bridge", "The Desktop or Documents folder could not be found."),
        "A proposed folder could not be inspected.":
            QT_TRANSLATE_NOOP("Bridge", "A possible folder name could not be checked."),
        "A folder or file with that name already exists; nothing is reused.":
            QT_TRANSLATE_NOOP("Bridge", "A folder or file with that name already exists. Nothing was reused."),
        "A folder is created only on the Desktop or in Documents\\JARVIS Projeleri.":
            QT_TRANSLATE_NOOP("Bridge", "A folder can be created only on the Desktop or in the JARVIS Projeleri folder in Documents."),
        "The created folder could not be proven to be the approved one.":
            QT_TRANSLATE_NOOP("Bridge", "The created folder could not be verified as the approved one."),
        "The folder path changed; confirm the new path.":
            QT_TRANSLATE_NOOP("Bridge", "The folder path changed. Confirm the new path."),
        # The window's own store check (_folder_result) and the backend's
        # refused-configuration answer (Backend._call), which keeps its repair hint.
        "The project store is unavailable.": QT_TRANSLATE_NOOP("Bridge", "The project store is unavailable."),
        "The stored configuration was refused, so execution is disabled. Repair it with 'jarvis config reset --all'.":
            QT_TRANSLATE_NOOP("Bridge", "The stored configuration was refused, so execution is disabled. "
                                        "Repair it with 'jarvis config reset --all'."),
        # E7 "Masaüstünde oluştur" refusals (app.project_folder_switch).
        "JARVIS is frozen; the folder request was not changed.":
            QT_TRANSLATE_NOOP("Bridge", "JARVIS is frozen; the folder request was not changed."),
        "This folder request is no longer pending here; nothing was changed.":
            QT_TRANSLATE_NOOP("Bridge", "This folder request is no longer pending here. Nothing was changed."),
    }
    _FOLDER_REASON_PREFIXES = (
        # Its example path comes back redacted ("[PATH]").
        ("A folder path must be a local drive path like ",
         QT_TRANSLATE_NOOP("Bridge", "Pick a folder on a local drive. Network and device paths cannot be linked.")),
        ("A folder path is longer than ", QT_TRANSLATE_NOOP("Bridge", "The folder path is too long.")),
        ("Project is archived: ", QT_TRANSLATE_NOOP("Bridge", "The project is archived: its folder link cannot change.")),
        ("Unknown project: ", QT_TRANSLATE_NOOP("Bridge", "The project no longer exists.")),
        ("No folder is linked to this project: ", QT_TRANSLATE_NOOP("Bridge", "No folder is linked to this project.")),
        # Its status code is for the log.
        ("The folder could not be created", QT_TRANSLATE_NOOP("Bridge", "The folder could not be created.")),
        ("No free folder name up to ", QT_TRANSLATE_NOOP("Bridge", "Every possible folder name for this project is already taken.")),
    )

    def _folder_reason(self, message: str) -> str:
        source = self._FOLDER_REASONS.get(message) or next(
            (said for prefix, said in self._FOLDER_REASON_PREFIXES if message.startswith(prefix)), None)
        return tx("Bridge", source) if source else tx("Present", FOLDER_UNKNOWN)

    @staticmethod
    def _folder_partial(folder: dict) -> str:
        """K1's partial creation, from its own fields (the message is a code):
        how many folders were made and kept, the link state read back, and
        each kept folder's path - an unverified one labelled as such."""
        created = [str(path) for path in folder.get("created") or ()]
        state = folder.get("link_state") if folder.get("link_state") in ("linked", "not linked") else "unknown"
        label = " (" + tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "intended path; its location could not be verified")) + ")"
        return "\n".join([tx("Bridge", QT_TRANSLATE_NOOP(
            "Bridge", "Folder creation stopped partway: %1 folder(s) were created and kept (no folder was deleted). "
                      "Project link: %2."), len(created), tx("Present", LINK_STATE_TEXT[state]))]
                         + [path[: -len(UNVERIFIED_SUFFIX)] + label if path.endswith(UNVERIFIED_SUFFIX) else path
                            for path in created])

    def _folder_result(self, action: str, project_id: str, **options: Any) -> dict:
        store = self._conversation_db()
        loaded = self.backend.store.load() if store is not None else None
        if loaded is None or not loaded.usable:
            return {"error": {"message": "The project store is unavailable."}}    # a code: _FOLDER_REASONS says it
        return project_folder_result(store, loaded.settings, action, str(project_id), **options).payload

    def _folder_change(self, action: str, project_id: str, **options: Any) -> bool:
        try:
            message = self._error_message(self._folder_result(action, project_id, **options))
        except Exception as exc:  # noqa: BLE001 - storage boundary: only the type
            message = type(exc).__name__
        self._project_folder_notice = self._folder_reason(message) if message else ""
        self.refreshProjects()
        return not message

    @Slot(str, result=str)
    def folderPath(self, url: str) -> str:
        """The exact local path a picked folder URL links as - what the window
        shows before the person confirms, and then sends unchanged."""
        local = QUrl(str(url)).toLocalFile() if str(url).startswith("file:") else str(url)
        return os.path.normpath(local) if local else ""

    @Slot(str, str, result=bool)
    def linkProjectFolder(self, project_id: str, path: str) -> bool:
        """Link the EXISTING folder the person picked (its exact path, as shown).
        The backend proves it (no reparse point, protected root, overlap); a
        refusal writes nothing and its reason becomes projectFolderNotice."""
        return self._folder_change("link", project_id, path=str(path))

    @Slot(str, result=bool)
    def unlinkProjectFolder(self, project_id: str) -> bool:
        """End the current link. The folder itself is never touched."""
        return self._folder_change("unlink", project_id)

    # 6H K1 "Klasör oluştur": the preview answer (the request's generation, the
    # path or "", a notice) and the combined create answer (the new project's id
    # or "" when no project was created, linked, the notice).
    projectFolderPreviewed = Signal(int, str, str)
    projectFolderCreated = Signal(str, bool, str)

    @Slot(str, bool, int)
    def previewProjectFolder(self, name: str, on_desktop: bool, generation: int) -> None:
        """Where the folder WOULD go for this name (reads only), on the worker."""
        def answer(payload: Any) -> None:
            message = self._error_message(payload)
            path = "" if message else str(payload.get("path") or "")
            self.projectFolderPreviewed.emit(int(generation), path, self._folder_reason(message) if message else "")
        self._run("project-folder-preview",
                  lambda: self.backend.project_folder_preview(str(name), bool(on_desktop)), answer)

    @Slot(str, str, bool)
    def createProjectWithFolder(self, name: str, path: str, on_desktop: bool) -> None:
        """ONE worker job: create the project, then create and link exactly
        ``path`` (the shown one) for the id from THAT create's own answer."""
        def work() -> dict:
            created = self.backend.project_create(str(name))
            project = created.get("project") if isinstance(created, dict) else None
            if not isinstance(project, dict) or not project.get("id"):
                return {"created": created}
            return {"created": created, "project_id": str(project["id"]),
                    "folder": self.backend.project_folder_create(str(project["id"]), str(path), bool(on_desktop))}
        self._run("project-folder-create", work, self._folder_created)

    def _folder_created(self, result: Any) -> None:
        result = result if isinstance(result, dict) else {}
        project_id = str(result.get("project_id") or "")
        if not project_id:
            message = self._error_message(result.get("created", result))
            self._project_notice = message or tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "The project could not be created."))
            self.refreshProjects()
            self.projectFolderCreated.emit("", False, self._project_notice)
            return
        folder = result.get("folder")
        message = self._error_message(folder)
        if message and isinstance(folder, dict) and "link_state" in folder:
            notice = self._folder_partial(folder)
        else:
            notice = self._folder_reason(message) if message else ""
        self._project_notice, self._project_folder_notice = "", notice
        self.refreshProjects()
        self.projectFolderCreated.emit(project_id, not message, notice)

    @Slot(result="QVariantList")
    def projectChoices(self) -> list:
        """The selector's projects: active ones only, by name."""
        return [{"id": r["id"], "name": r["name"]} for r in self.backend.project_records() if r.get("active")]

    @Slot(str, result=bool)
    def openProjectScope(self, project_id: str) -> bool:
        """The top-bar selector ("" = Global): open that owner's newest
        non-archived conversation, or create one through the existing paths.
        Opening a conversation decides nothing: Core binds the session to its
        conversation's owner (ARCHITECTURE 4.22)."""
        project_id = str(project_id)
        if self.navigationLocked:
            return False
        if project_id and project_id not in {choice["id"] for choice in self.projectChoices()}:
            return False
        store = self._conversation_db()
        if store is None:
            return False
        try:
            records = store.list_conversations(project_id=project_id or None)
        except Exception:  # noqa: BLE001 - storage boundary: create instead
            records = []
        if records:
            # A scope pick is not a pick of that conversation: the scope's groups stay as they were left.
            opened = self._open_conversation(records[0].id, expand=False)
        else:
            opened = self.newProjectConversation(project_id) if project_id else self.newConversation()
        if opened:
            self.setPage("home")
        return opened

    @Slot(result=bool)
    def newConversationInScope(self) -> bool:
        """The "+ New conversation" button: in the open conversation's owner
        (Global when Global); never in an archived project."""
        owner = self._conversation_owner
        if not owner:
            return self.newConversation()
        if not owner.get("active"):
            return False
        return self.newProjectConversation(owner["id"])

    # -- Gate 6G Sidebar v2 slice 3 (V4): the New chat screen's project picker --
    #
    # Navigation over the existing creation paths only: the store decides an
    # owner when a conversation is created and the first message fixes it
    # (the picker shows only while the open conversation is empty).

    newProjectDialogRequested = Signal()

    def _active_project(self, project_id: str) -> bool:
        return any(r["id"] == project_id and r.get("active") for r in self._project_records)

    @Slot(result=bool)
    def startNewChat(self) -> bool:
        """V4 "Yeni sohbet": the New chat screen - a new, empty conversation in
        the current project (the open project page's, else the open
        conversation's) when that one is active, else with no project."""
        project = self._open_project if self._page_override == "projects" else ""
        if not project:
            project = self._conversation_owner.get("id") or ""
        opened = self.newProjectConversation(project) if self._active_project(project) else self.newConversation()
        if opened:
            self.setPage("home")
        return opened

    @Slot(str, result=bool)
    def setNewChatProject(self, project_id: str) -> bool:
        """V4: the picker's choice ("" = no project) while the open conversation
        is still empty: that owner's blank conversation when there is one, else
        a new one (DFNEW, in newConversation / newProjectConversation). Refused
        once a message was sent (the owner is fixed) and for an unknown or
        archived project."""
        project_id = str(project_id)
        if (not self._conversation_store.id or self._conversation_store.state.turns or self._conversation_archived
                or self._conversation_store.id in self._queued_ids()):      # DFQUEUE: a queued message fixes it too
            return False
        if (self._conversation_owner.get("id") or "") == project_id:
            return True
        if project_id and not self._active_project(project_id):
            return False
        return self.newProjectConversation(project_id) if project_id else self.newConversation()

    @Slot()
    def requestNewProject(self) -> None:
        """V4 "Yeni proje…": the window opens the existing new-project dialog. The
        project it creates is chosen for the open new chat (G6SB3F, in showProject);
        a dialog closed without one drops the request (dropNewProjectChat)."""
        self._new_project_chat = (self._conversation_store.id, frozenset(r["id"] for r in self._project_records))
        self.newProjectDialogRequested.emit()

    @Slot()
    def dropNewProjectChat(self) -> None:
        """G6SB3F: the new-project dialog closed without creating: the new chat stays as it was."""
        self._new_project_chat = None

    # -- Gate 6F F6: pinned conversations and collapsible date groups ---------
    #
    # Window preferences only (UiPreferences' sidebar file): a pin is an id in
    # an ordered list, a group's open state a flag per scope. Nothing here
    # writes the conversation store or changes a conversation, its owner or its
    # archive state; opening a pinned conversation is openConversation.

    sidebarChanged = Signal()

    @Property(int, constant=True)
    def pinLimit(self) -> int:
        return PIN_LIMIT

    @Property("QVariantList", notify=sidebarChanged)
    def pinnedConversations(self) -> list:
        """The pinned conversations of every scope, in their manual order: the
        scopeConversations keys, plus the owner (project_id "" = Global, its
        name and whether it is archived)."""
        projects = {**self._pin_projects, **{r["id"]: r for r in self._project_records}}
        out = []
        for pin in self.prefs.sidebar["pins"]:
            record = self._pin_records.get(pin)
            if record is None:
                continue
            owner = projects.get(record.project_id or "", {})
            title = record.title or (("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ())
                                     if record.origin == "legacy-import" else "")
            out.append({"id": record.id, "title": render(title) if isinstance(title, tuple) else title,
                        "updated_at": record.updated_at, "active": record.id == self._conversation_store.id,
                        "project_id": record.project_id or "",
                        "project_name": str(owner.get("name") or record.project_id or ""),
                        "project_archived": bool(record.project_id) and not owner.get("active", False)})
        return out

    @Property("QVariantMap", notify=sidebarChanged)
    def sidebarGroups(self) -> dict:
        """Which sidebar sections are open: SABİTLENENLER ("pinned") and the
        no-project list's today / yesterday / earlier (Sidebar v2 V1: one
        fixed list, whatever conversation is open)."""
        return {"pinned": self.prefs.sidebar["pins_open"], **self.prefs.groups_open(self._sidebar_scope())}

    def _sidebar_scope(self) -> str:
        return GLOBAL_SCOPE

    # -- Gate 6G Sidebar v2: the fixed list, the project tree and its states ---
    #
    # Read-only views of the same store read, and window preferences only
    # (UiPreferences' tree file): nothing here writes a conversation or a project.

    @Property("QVariantMap", notify=conversationChanged)
    def sidebarConversations(self) -> dict:
        """Every owner's non-archived conversations, newest first: "" = no
        project, else the project id. Untitled = "" (the window says "New Chat")."""
        open_id = self._conversation_store.id
        return {owner: [{**row, "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                         "active": row["id"] == open_id} for row in rows]
                for owner, rows in self._sidebar_lists.items()}

    @Property("QVariantMap", notify=sidebarChanged)
    def sidebarTree(self) -> dict:
        """projects_open (L2), plain_open (P1), closed (T1: collapsed project
        ids) and pinned_projects (T2, newest pin first)."""
        tree = self.prefs.tree
        return {"projects_open": tree["projects_open"], "plain_open": tree["plain_open"],
                "closed": list(tree["closed"]), "pinned_projects": list(tree["pinned_projects"])}

    def _tree_saved(self, changed: bool) -> bool:
        if changed:
            self.prefs.save_tree()
            self.sidebarChanged.emit()
        return changed

    @Slot(bool)
    def setProjectsOpen(self, open_: bool) -> None:
        self._tree_saved(self.prefs.set_tree_flag("projects_open", bool(open_)))

    @Slot(bool)
    def setPlainOpen(self, open_: bool) -> None:
        self._tree_saved(self.prefs.set_tree_flag("plain_open", bool(open_)))

    @Slot(str, bool)
    def setProjectOpen(self, project_id: str, open_: bool) -> None:
        self._tree_saved(self.prefs.set_tree_member("closed", str(project_id), not open_))

    @Slot(str, bool)
    def setProjectPinned(self, project_id: str, pinned: bool) -> None:
        self._tree_saved(self.prefs.set_tree_member("pinned_projects", str(project_id), bool(pinned)))

    # -- Gate 6G F8: the sidebar's conversation search (read-only) ------------
    #
    # A scan of the stored conversations through the store's read paths
    # (jarvis.conversation_search), on its own thread: the window never waits.
    # The answer carries the query exactly as asked, so the window keeps only
    # the answer to what it shows now; a superseded scan delivers nothing.

    conversationSearchDone = Signal("QVariantMap")

    @Slot(str)
    def searchConversations(self, query: str) -> None:
        query = str(query)
        self._search_seq = seq = getattr(self, "_search_seq", 0) + 1
        store = self._conversation_db()

        def work() -> dict:
            try:
                found = conversation_search.search(store, query, redact=redact_text)
            except Exception:  # noqa: BLE001 - storage boundary: an honest failure, never a partial list
                found = {"titles": [], "contents": [], "more": False, "failed": True}
            return {"failed": store is None, **found, "query": query}

        if self.sync:
            self.conversationSearchDone.emit(work())
            return

        def run() -> None:
            result = work()
            if seq == self._search_seq:
                self._deliver("conversationSearchDone", result)

        threading.Thread(target=run, name="jarvis-search", daemon=True).start()

    # -- Gate 6G Sidebar v2 slice 3: DRAG (project order) and V10 (pin info card)

    @Property("QVariantList", notify=projectsChanged)
    def sidebarProjectOrder(self) -> list:
        """DRAG: every project's id in the sidebar's manual order (a new one on top)."""
        return ordered_projects(self._project_records, self.prefs.project_order)

    @Slot(str, str, result=bool)
    def moveProject(self, project_id: str, before_id: str) -> bool:
        """DRAG: put a project just above ``before_id`` ("" = last). Window
        preferences only (UiPreferences' order file)."""
        project_id, before_id = str(project_id), str(before_id)
        order = self.sidebarProjectOrder
        if project_id not in order or project_id == before_id or (before_id and before_id not in order):
            return False
        order.remove(project_id)
        order.insert(order.index(before_id) if before_id else len(order), project_id)
        if not self.prefs.set_project_order(order):
            return False
        self.projectsChanged.emit()
        return True

    @Slot(str, result="QVariantMap")
    def pinInfo(self, conversation_id: str) -> dict:
        """V10, read only: what a SABİTLENENLER row's info card shows, every
        value as stored (nothing generated, no model call): the owner, the
        title, the project's instruction line (a project's) or the first
        message (a plain one), the project's active file names, the last
        change. A fact that cannot be read is left out; {} for no listed pin."""
        record = self._pin_records.get(str(conversation_id))
        store = self._conversation_db()
        if record is None or store is None:
            return {}
        title = record.title or (("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ())
                                 if record.origin == "legacy-import" else "")
        owner = {**self._pin_projects, **{r["id"]: r for r in self._project_records}}.get(record.project_id or "", {})
        info: dict[str, Any] = {"project_id": record.project_id or "", "project_name": str(owner.get("name") or ""),
                                "title": render(title) if isinstance(title, tuple) else title,
                                "instruction": "", "first_message": "", "files": [], "updated_at": record.updated_at}
        try:
            if record.project_id:
                current = store.load_project_instruction(record.project_id)
                text = current.text if current is not None else ""
                info["instruction"] = next((line.strip() for line in text.splitlines() if line.strip()), "")
                info["files"] = [str(f["display_name"]) for f in store.list_project_files(record.project_id)
                                 if f.get("lifecycle") == "active"]
            else:
                first = next((t for t in store.load_conversation_state(record.id).turns if t.role == "user"), None)
                info["first_message"] = (first.message or first.text).strip() if first is not None else ""
        except Exception:  # noqa: BLE001 - a label only: what was read stays
            pass
        return info

    # -- Gate 6G Sidebar v2 slice 2: hide mode (V5), tiles mode (V6), widths (E1')
    #
    # Window preferences only (UiPreferences' sidebar-layout file); the widths
    # are px at the 1920 layout, scaled by the window.

    sidebarLayoutChanged = Signal()

    @Property("QVariantMap", notify=sidebarLayoutChanged)
    def sidebarLayout(self) -> dict:
        """hide_mode ("" / "grow" / "keep"), tiles, left and right (px at 1920)."""
        layout = self.prefs.sidebar_layout
        return {key: layout[key] for key in ("hide_mode", "tiles", "left", "right")}

    def _layout_saved(self, changes: dict) -> None:
        if self.prefs.set_sidebar_layout(changes):
            self.sidebarLayoutChanged.emit()

    @Slot(str)
    def setSidebarHideMode(self, mode: str) -> None:
        self._layout_saved({"hide_mode": str(mode)})

    @Slot(bool)
    def setSidebarTiles(self, tiles: bool) -> None:
        self._layout_saved({"tiles": bool(tiles)})

    @Slot(int, int)
    def setSidebarWidths(self, left: int, right: int) -> None:
        self._layout_saved({"left": int(left), "right": int(right)})

    def _sidebar_saved(self, changed: bool) -> bool:
        if changed:
            self.prefs.save_sidebar()
            self.sidebarChanged.emit()
        return changed

    @Slot(str, result=bool)
    def pinConversation(self, conversation_id: str) -> bool:
        """Pin a listed, non-archived conversation (to the top); refused at the limit."""
        conversation_id = str(conversation_id)
        store = self._conversation_db()
        try:
            record = store.load_conversation(conversation_id) if store is not None and conversation_id else None
        except Exception:  # noqa: BLE001 - storage boundary: not pinned
            record = None
        if record is None or record.archived_at is not None:
            return False
        changed = self.prefs.pin(conversation_id)
        if changed:
            self._pin_records[conversation_id] = record
            if record.project_id and record.project_id not in self._pin_projects:
                self._refresh_conversations()
        return self._sidebar_saved(changed)

    @Slot(str, result=bool)
    def unpinConversation(self, conversation_id: str) -> bool:
        return self._sidebar_saved(self.prefs.unpin(str(conversation_id)))

    @Slot(str, int, result=bool)
    def movePin(self, conversation_id: str, index: int) -> bool:
        """Put a pin at ``index`` of SABİTLENDİ (drag and drop, Yukarı / Aşağı taşı)."""
        return self._sidebar_saved(self.prefs.move_pin(str(conversation_id), int(index)))

    @staticmethod
    def _date_group(updated_at: str) -> str:
        """The sidebar's date group of a stored timestamp, by the local calendar
        day as the sidebar groups it (unreadable: earlier)."""
        try:
            day = datetime.fromisoformat(str(updated_at)).astimezone().date()
        except ValueError:
            return "earlier"
        today = datetime.now().astimezone().date()
        return "today" if day >= today else "yesterday" if day == today - timedelta(days=1) else "earlier"

    def _expand_group_of(self, conversation_id: str) -> None:
        """Switching to a no-project conversation opens its date group (a pinned
        or a project one has none)."""
        if conversation_id in self.prefs.sidebar["pins"]:
            return
        row = next((r for r in self._sidebar_lists.get("", []) if r["id"] == conversation_id), None)
        if row is not None:
            self.setGroupOpen(self._date_group(row["updated_at"]), True)

    @Slot(str, bool)
    def setGroupOpen(self, key: str, open_: bool) -> None:
        """Open or collapse SABİTLENENLER ("pinned") or one of the no-project list's date groups."""
        self._sidebar_saved(self.prefs.set_group_open(self._sidebar_scope(), str(key), bool(open_)))

    # -- Gate 6C "Projeye bağla": attach the open Global conversation --------
    #
    # Preview, then the person's explicit confirm, through the app paths; the
    # store's shared check decides and its refusals are shown as returned.
    # The confirm carries exactly what was previewed (target + next_seq); a
    # different choice or a changed conversation refreshes the preview and
    # is never retried on its own.

    attachChanged = Signal()

    @Property(bool, notify=conversationChanged)
    def canAttach(self) -> bool:
        """The open conversation is a stored, writable GLOBAL conversation."""
        store, conversation_id = self._conversation_db(), self._conversation_store.id
        if store is None or not conversation_id or self._conversation_owner:
            return False
        try:
            record = store.load_conversation(conversation_id)
        except Exception:  # noqa: BLE001 - storage boundary: not offered
            return False
        return record is not None and record.project_id is None and record.archived_at is None

    @Property(dict, notify=attachChanged)
    def attachPreview(self) -> dict:
        return dict(self._attach_preview)

    @Property(str, notify=attachChanged)
    def attachNotice(self) -> str:
        return self._attach_notice

    @staticmethod
    def _attach_target(project_id: str, new_name: str) -> dict[str, str]:
        return {"project_id": str(project_id)} if project_id else {"new_project_name": str(new_name)}

    @Slot(str, str)
    def previewAttach(self, project_id: str, new_name: str) -> None:
        """Preview attaching the open conversation to ``project_id`` or to a
        new project named ``new_name`` ("" / "" clears the preview)."""
        self._attach_preview, self._attach_notice = {}, ""
        if project_id or new_name:
            target = self._attach_target(project_id, new_name)
            payload = self.backend.conversation_attach_preview(str(self._conversation_store.id or ""), **target)
            self._attach_notice = self._error_message(payload)
            if not self._attach_notice:
                self._attach_preview = {**payload["preview"], "request": target}
        self.attachChanged.emit()

    @Slot(str, str, result=bool)
    def confirmAttach(self, project_id: str, new_name: str) -> bool:
        """The single "Projeye bağla" button: attach exactly what was
        previewed. Afterwards the conversation is re-opened, so the next task
        starts a fresh Core session."""
        held = self._attach_preview
        if not held or held.get("blocked") or self._runs_in(held["conversation_id"]):
            return False
        if held["request"] != self._attach_target(project_id, new_name):
            self.previewAttach(project_id, new_name)
            self._attach_notice = tx("Bridge", QT_TRANSLATE_NOOP(
                "Bridge", "The choice changed after the preview; check the new preview and confirm again."))
            self.attachChanged.emit()
            return False
        conversation_id = held["conversation_id"]
        payload = self.backend.conversation_attach(conversation_id, held["expected_next_seq"], **held["request"])
        error = self._error_message(payload)
        if error:
            self.previewAttach(project_id, new_name)          # shown fresh; never retried
            self._attach_notice = error
            self.attachChanged.emit()
            return False
        store = self._conversation_db()
        self._attach_preview, self._attach_notice = {}, ""
        # Re-opened WITH its verified stored state, so the feed and the next
        # request's history keep the earlier messages.
        opened = self._load_conversation(store, conversation_id)
        self._parked.pop(conversation_id, None)       # R2: moved to a project, it starts a fresh session
        if opened is None:
            self._leave_open_conversation(*self._open_scope())
        else:
            self._activate(opened)
        self.refreshProjects()
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Conversation attached to %1"),
                    (str(payload["attached"]["target"]["name"]),)), "system")
        self.attachChanged.emit()
        return True

    # -- Gate 6D-b: archive, unarchive, permanent delete (Salih 2026-09-30) ----
    #
    # Two steps, like a project file's retire -> purge: archive (reversible),
    # then a permanent delete confirmed from a preview. Every change goes
    # through the explicit-user app results; the store decides and its
    # refusals are shown as returned. Each action carries the conversation id
    # captured when it started. The open conversation is never left open once
    # archived or deleted.

    conversationLifecycleChanged = Signal()

    @Property("QVariantList", notify=conversationChanged)
    def archivedConversations(self) -> list:
        """History's ARCHIVED section: the archived Global conversations."""
        return [{**row, "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                 "active": row["id"] == self._conversation_store.id} for row in self._archived_conversations]

    @Property(str, notify=conversationLifecycleChanged)
    def conversationNotice(self) -> str:
        return self._lifecycle_notice

    @Property(dict, notify=conversationLifecycleChanged)
    def deletePreview(self) -> dict:
        return dict(self._delete_preview)

    def _open_scope(self) -> tuple[str | None, bool]:
        """The open conversation's scope (``None`` = Global) and whether a
        new conversation may be opened in it (Global, or an active project)."""
        owner = self._conversation_owner
        return (owner.get("id") or None, bool(owner.get("active"))) if owner else (None, True)

    def _recheck_open(self) -> None:
        """N2: one read of the open conversation only; deleted or archived since
        it was last read (by any path) -> the full refresh, which leaves it."""
        seen_id, was_live = self._open_seen
        store = self._conversation_db()
        if self.navigationLocked or store is None or not seen_id or seen_id != self._conversation_store.id:
            return
        try:
            record = store.load_conversation(seen_id)
        except Exception:  # noqa: BLE001 - unreadable is not removed
            return
        if record is None or (was_live and record.archived_at is not None):
            self._refresh_conversations()

    def _leave_open_conversation(self, scope: str | None, writable: bool) -> None:
        """The open conversation was archived or deleted: never stay on it.
        N2 (Salih 2026-10-05): the New chat screen - its scope's blank
        conversation (DFNEW; a new one only when it has none) when that scope
        is writable, else Global's (the store refuses an owner archived
        meanwhile), on the chat page."""
        store = self._conversation_db()
        opened, error = None, None
        for owner in ([scope] if writable and scope is not None else []) + [None]:
            try:
                opened = self._new_chat_conversation(store, owner)
                break
            except Exception as exc:  # noqa: BLE001 - storage boundary; only the type
                error = exc
        if opened is None:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A new conversation could not be created: %1"),
                        (type(error).__name__,)), "error")
            opened = PersistedConversation(None, None)
        self._activate(opened, park=False)
        self.refreshProjects()
        self.setPage("home")

    def _lifecycle_done(self, conversation_id: str, payload: Any, leaves: bool) -> bool:
        self._lifecycle_notice = self._error_message(payload)
        if not self._lifecycle_notice:
            if leaves and self.prefs.unpin(conversation_id):      # archived or deleted: its pin goes (F6)
                self.prefs.save_sidebar()
            self.refreshProjects()                                 # the open one archived or deleted: N2
        self.conversationLifecycleChanged.emit()
        return self._lifecycle_notice == ""

    def _lifecycle_refused(self, conversation_id: str) -> bool:
        if self._runs_in(conversation_id):
            self._lifecycle_notice = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "Wait for the current run to finish."))
        elif not conversation_id:
            self._lifecycle_notice = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "This conversation is not saved yet."))
        else:
            return False
        self.conversationLifecycleChanged.emit()
        return True

    @Slot(str, result=bool)
    def archiveConversation(self, conversation_id: str) -> bool:
        """Archive EXACTLY ``conversation_id`` (reversible)."""
        conversation_id = str(conversation_id)
        if self._lifecycle_refused(conversation_id):
            return False
        return self._lifecycle_done(conversation_id, self.backend.conversation_archive(conversation_id), True)

    @Slot(str, result=bool)
    def unarchiveConversation(self, conversation_id: str) -> bool:
        """Undo an archive of EXACTLY ``conversation_id``; the store refuses
        under an archived project (unarchive the project first)."""
        conversation_id = str(conversation_id)
        if self._lifecycle_refused(conversation_id):
            return False
        return self._lifecycle_done(conversation_id, self.backend.conversation_unarchive(conversation_id), False)

    @Slot(str)
    def previewDeleteConversation(self, conversation_id: str) -> None:
        """Hold what a permanent delete of ``conversation_id`` would remove
        ("" clears it); the dialog shows it and the confirm sends it."""
        self._delete_preview, self._lifecycle_notice = {}, ""
        if str(conversation_id):
            payload = self.backend.conversation_delete_preview(str(conversation_id))
            self._lifecycle_notice = self._error_message(payload)
            if not self._lifecycle_notice:
                self._delete_preview = dict(payload["preview"])
        self.conversationLifecycleChanged.emit()

    @Slot(str, result=bool)
    def confirmDeleteConversation(self, conversation_id: str) -> bool:
        """The single "Kalıcı sil" button: delete exactly what was previewed
        (id, next_seq and title). A refusal - e.g. the conversation changed -
        refreshes the preview and is never retried."""
        conversation_id = str(conversation_id)
        held = self._delete_preview
        if self._runs_in(conversation_id) or not held or held.get("blocked") or held.get("conversation_id") != conversation_id:
            return False
        payload = self.backend.conversation_delete(conversation_id, confirm=conversation_id,
                                                   expected_next_seq=held["expected_next_seq"],
                                                   expected_title=held["title"])
        error = self._error_message(payload)
        if error:
            self.previewDeleteConversation(conversation_id)          # shown fresh; never retried
            self._lifecycle_notice = error
            self.conversationLifecycleChanged.emit()
            return False
        self._delete_preview = {}
        self._lifecycle_done(conversation_id, payload, True)
        if self._drafts.pop(conversation_id, None) is not None:     # DFDRAFT: its draft goes with it
            self._save_drafts()
        if any(entry["cid"] == conversation_id for entry in self._queue):     # DFQUEUE: and its queued messages
            self._queue = [entry for entry in self._queue if entry["cid"] != conversation_id]
            self.queueChanged.emit()
        self._set_sessions({"sessions": self._sessions})
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Conversation deleted permanently"), ()), "system")
        return True

    def _session_conversation(self, session_id: str) -> str | None:
        store = self._conversation_db()
        if store is None or not session_id:
            return None
        try:
            return store.conversation_for_session(str(session_id))
        except Exception:  # noqa: BLE001 - storage boundary
            return None

    def _payload_owner(self, payload: Any, origin: PersistedConversation) -> PersistedConversation | None:
        """The conversation a run's envelope belongs to, or ``None``: DROP it.

        A session already linked belongs to ITS conversation. A session seen for
        the first time is linked to ``origin``, the conversation the request came
        from. Without a session the envelope belongs to ``origin``. A linked
        conversation that cannot be loaded - deleted (Gate 6D-b) or corrupt -
        owns it all the same: it is dropped, never handed to the open
        conversation or to any other owner. R1: the owner is re-read at
        delivery on EVERY path, before anything is linked or written."""
        run = payload.get("run") if isinstance(payload, dict) and isinstance(payload.get("run"), dict) else payload
        session = run.get("session") if isinstance(run, dict) else None
        session_id = session.get("id") if isinstance(session, dict) else None
        store = origin.store
        if store is None:
            return origin
        if not isinstance(session_id, str) or not session_id:
            return origin if self._still_there(store, origin.id) else None
        linked = self._session_conversation(session_id)
        if linked is None:
            if not self._still_there(store, origin.id):
                return None
            try:
                store.link_session_conversation(session_id, origin.id)
            except Exception:  # noqa: BLE001 - an unlinkable session stays with its origin
                pass
            return origin
        if linked == self._conversation_store.id:
            return self._conversation_store if self._still_there(store, linked) else None
        try:
            return PersistedConversation(store, linked, store.load_conversation_state(linked))
        except Exception:  # noqa: BLE001 - deleted or unreadable: dropped by the caller
            return None

    @staticmethod
    def _still_there(store: Any, conversation_id: str | None) -> bool:
        """The conversation exists and is not deleted (an unreadable store says no)."""
        try:
            return bool(conversation_id) and store.load_conversation(conversation_id) is not None
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            return False

    def _delivered(self, handler: Callable[[Any], None], snapshots: frozenset[str] = frozenset(),
                   project: Callable[[dict, Any], dict] = _project_run, origin: PersistedConversation | None = None,
                   identity: tuple[str, str] | None = None) -> Callable[[Any], None]:
        """``handler`` for the conversation open NOW; anything that turns out to
        belong to another conversation is remembered there instead, and
        anything whose conversation cannot be loaded is dropped: the only trace
        is a transient window notice, saved nowhere. ``snapshots``: the
        attachment files the request references (kept by its run record).
        R2: ``project`` is the slot projection ``handler`` applies, so another
        conversation's parked state follows its newest envelope - from that
        conversation's own parked state, else from the origin's slots as they
        were at launch, never from the screen open at delivery. ``origin``
        and ``identity`` name a run started for a conversation that is not
        open (a background Resume, whose own state is parked)."""
        captured = None if origin is not None else dict(self._live_slots())
        origin = origin or self._conversation_store
        launched = self._deliveries.get(origin.id or "", 0)     # captured is stale once a newer envelope landed

        def deliver(payload: Any) -> None:
            owner = self._payload_owner(payload, origin)
            if owner is None:
                self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A reply for a conversation that was deleted or cannot "
                                                                  "be read was dropped; it was saved nowhere."), ()), "system")
                self._prune_parked()
                return
            fresh = owner.id == origin.id and self._deliveries.get(owner.id or "", 0) == launched
            self._deliveries[owner.id or ""] = self._deliveries.get(owner.id or "", 0) + 1
            if owner.id == self._conversation_store.id:
                handler(payload)
                return
            prior = self._parked.get(owner.id) or (captured if captured and fresh else _empty_slots())
            self._remember_elsewhere(payload, owner)
            self._park_delivered(owner, project(prior, payload))

        deliver.origin, deliver.snapshots = origin, snapshots    # read by ``_run``: the run record
        if identity is not None:
            deliver.identity = identity
        return deliver

    def _remember_elsewhere(self, payload: Any, owner: PersistedConversation) -> None:
        """What a conversation that is not open would have remembered: its
        completed replies and its task results. Nothing on screen changes."""
        intent = payload.get("intent") if isinstance(payload, dict) and isinstance(payload.get("intent"), dict) else None
        status = str((intent or {}).get("status") or "")
        if intent is not None:
            # DFREPLY: a reply the run streamed that ended cut short is kept there too.
            seen = _kept_partial(self._run_preview) if status not in ("conversation", "research") else ""
            kept = bool(seen)
            for entry in cut_entries(seen, cut_reason(status)) if kept else text_entries(payload):
                if (kept or status in ("conversation", "research")) and entry["kind"] == "jarvis" \
                        and isinstance(entry.get("text"), str):
                    self._say(entry.pop("kind"), entry.pop("text"), chat=True, owner=owner, **entry)
        run = payload.get("run") if intent is not None and isinstance(payload, dict) else payload
        if isinstance(run, dict) and isinstance(run.get("task"), dict):
            facts = {key: task_status(run, intent).get(key, "") for key in ("capability", "destination")}
            task_id = str(run["task"].get("id") or "")
            run_status = str(run.get("run_status") or "")
            for entry in run_entries(run, facts):
                if entry.get("kind") == "result":
                    self._say(entry.pop("kind"), entry.pop("text"), owner=owner, task_id=task_id,
                              run_status=run_status, path=_proved_path(run), **entry)
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A reply for another conversation was saved there"), ()), "system")

    # -- Gate 6G R2: parked waiting state, per conversation (bridge memory only) --

    def _live_slots(self) -> dict[str, Any]:
        return {"session_id": self._session_id, "task": self._task, "ticket": self._ticket, "approval": self._approval,
                "clarification": self._clarification, "last_outcome": self._last_outcome}

    def _set_live(self, slots: dict) -> None:
        (self._session_id, self._task, self._ticket, self._approval, self._clarification,
         self._last_outcome) = (slots[key] for key in _SLOTS)

    def _park(self, conversation_id: str | None, slots: dict) -> None:
        """``slots`` become the parked state of ``conversation_id`` (not open) while they qualify
        (OD1) and are provably its own; otherwise it keeps none. Nothing is parked without a
        conversation store."""
        store = self._conversation_db()
        if (conversation_id and store is not None and _parks(slots)
                and self._parked_valid(store, conversation_id, slots)):
            self._parked[conversation_id] = {key: slots[key] for key in _SLOTS}
        else:
            self._parked.pop(conversation_id, None)
        self._needs_you_check()

    def _park_delivered(self, owner: PersistedConversation, slots: dict) -> None:
        """An envelope for ``owner`` (not open) replaces its parked state; a ticket only ever stays
        when its session is ``owner``'s own."""
        ticket = slots["ticket"]
        if ticket is not None and self._session_conversation(ticket.session_id) != owner.id:
            slots = {**slots, "ticket": None}
        if slots["task"].get("task_id"):
            slots = {**slots, "task": {**slots["task"], "title": owner.title(slots["task"]["task_id"])}}
        self._park(owner.id, slots)

    def _parked_valid(self, store: Any, conversation_id: str, slots: dict) -> bool:
        """Still ``conversation_id``'s own: it exists, reads and is not archived, and every session
        the slots name is linked to it (a failed lookup, no link or another owner: no). Never relinks."""
        try:
            record = store.load_conversation(conversation_id)
            store.load_conversation_state(conversation_id)
        except Exception:  # noqa: BLE001 - storage boundary; fail closed
            return False
        if record is None or record.archived_at is not None:
            return False
        ticket = slots["ticket"]
        sessions = {slots["session_id"], str(slots["task"].get("session_id") or ""),
                    str(slots["approval"].get("session_id") or ""), ticket.session_id if ticket is not None else ""} - {""}
        return all(self._session_conversation(session) == conversation_id for session in sessions)

    def _unpark(self, opened: PersistedConversation) -> dict[str, Any] | None:
        """``opened``'s parked state, taken once and only when still its own."""
        slots = self._parked.pop(opened.id, None) if opened.id else None
        if slots is None or opened.store is None or not self._parked_valid(opened.store, opened.id, slots):
            return None
        return slots

    def _prune_parked(self) -> None:
        """Deleted, archived, unreadable or relinked: dropped, never restored anywhere, nothing written."""
        store = self._conversation_db()
        gone = [cid for cid, slots in self._parked.items() if store is None or not self._parked_valid(store, cid, slots)]
        for cid in gone:
            del self._parked[cid]
        if gone:
            self._needs_you_check()

    def _needs_you(self) -> list[str]:
        ids = {cid for cid, slots in self._parked.items() if _waiting(slots)}
        if self._conversation_store.id and _waiting(self._live_slots()):
            ids.add(self._conversation_store.id)
        if self._busy and self._run_record is not None:
            ids.discard(self._run_record["origin"])     # OD5: its waiting request is being answered
        return sorted(ids)

    def _needs_you_check(self) -> None:
        ids = self._needs_you()
        if ids != self._needs_you_seen:
            self._needs_you_seen = ids
            self.sidebarChanged.emit()
        if self._queue and not self._busy:
            self._queue_timer.start()       # DFQUEUE: an approval answered (or gone) may let the queue go on

    @Property("QVariantList", notify=sidebarChanged)
    def needsYouIds(self) -> list:
        """Q2: the conversations where an approval or a clarification waits for the person - a
        parked one, or the open one's - minus the one a run is answering now (OD5). Truth only:
        the sidebar draws it (R3); never persisted."""
        return list(self._needs_you())

    @Property("QVariantList", notify=stateChanged)
    def runningIds(self) -> list:
        """R3 (D1): the conversation the recorded run in flight works for (purple); none for a
        run without a record. Truth only, never persisted."""
        return [self.runningConversationId] if self.runningConversationId else []

    @Property("QVariantList", notify=sidebarChanged)
    def finishedUnseenIds(self) -> list:
        """R3 (D1/D2): conversations a recorded run finished for while another one was open,
        until opened (green); the open one never. Bridge memory only."""
        return sorted(self._finished_unseen - {self._conversation_store.id})

    @Slot(str, result=bool)
    def markConversationUnread(self, conversation_id: str) -> bool:
        """R3U: back to R3's green "finished, not seen" until it is opened again - a listed
        (non-archived) conversation that is not the open one. Bridge memory only, like R3's own
        green: no conversation, no preference and no schema changes."""
        cid = str(conversation_id)
        listed = any(row["id"] == cid for listed_rows in self._sidebar_lists.values() for row in listed_rows)
        if not cid or cid == self._conversation_store.id or not listed:
            return False
        if cid not in self._finished_unseen:
            self._finished_unseen.add(cid)
            self.sidebarChanged.emit()
        return True

    @Property("QVariantList", notify=sidebarChanged)
    def unreadBell(self) -> list:
        """R3U: the top bar bell's entries, newest first - every listed conversation that waits
        for the person (needsYouIds, amber wins) or finished unseen (green), each once, never the
        open one: id, title ("" = untitled), project ("" = Global) and state."""
        waiting = set(self._needs_you())
        wanted = (waiting | self._finished_unseen) - {self._conversation_store.id}
        names = {**self._pin_projects, **{r["id"]: r for r in self._project_records}}
        out = []
        for owner, listed_rows in self._sidebar_lists.items():
            for row in listed_rows:
                cid = row["id"]
                if cid not in wanted:
                    continue
                title = row["title"]
                out.append({"id": cid, "title": render(title) if isinstance(title, tuple) else title,
                            "project": str(names.get(owner, {}).get("name") or owner),
                            "state": "needsYou" if cid in waiting else "finished", "updated_at": row["updated_at"]})
        return sorted(out, key=lambda entry: entry["updated_at"], reverse=True)

    def _linked_here(self, session_id: str) -> bool:
        """``session_id`` belongs to the OPEN conversation (a failed lookup: no)."""
        return self._session_conversation(session_id) == self._conversation_store.id

    def _restored_unlinked(self) -> bool:
        """The screen acts with the session it got back from its parked state, and that session
        is no longer linked to it (relinked, unlinked, a failed lookup): refuse, never relink."""
        session = self._session_id
        return bool(session) and session == self._restored_session and not self._linked_here(session)

    def _refuse_unlinked(self) -> None:
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "This task's session no longer belongs to this conversation; "
                                                          "nothing was sent."), ()), "error")

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
            out.append(_here(row))
        return out

    # -- core state ----------------------------------------------------------

    def _refresh_core_state(self) -> None:
        # Dormant only when frozen is REAL: the gate is closed and no operation
        # of this application is still running toward its next safe point.
        if self._config_refused() or (self._config.get("execution_enabled") is False and not self._busy):
            state = "disabled"
        elif self.openRunning:
            # R1: "running" is this screen's run; a recorded run working for another
            # conversation leaves this one its own state (``busy`` stays global).
            state = "running"
        elif self._ticket is not None:
            state = "awaiting_approval"
        elif self._clarification or self._task.get("status") == "awaiting_clarification":
            state = "awaiting_input"
        else:
            # A run that ended without achieving what was asked is not a success
            # state for the core: nothing is running, and nothing was completed.
            state = "idle" if self._last_outcome == "unmet" else self._last_outcome
        if state != self._core_state:
            self._core_state = state
        self.stateChanged.emit()

    _last_outcome = "idle"

    def _config_refused(self) -> bool:
        return self._config.get("config_status") not in ("valid", "absent")

    # -- envelopes -----------------------------------------------------------

    def _apply_run(self, payload: Any, intent: Any = None, request: str = "") -> None:
        """Render one task envelope: replaces the task block and the approval.

        ``request`` is the person's message that started this run, when it did:
        a task (and a session) with no name yet is named from it - once, never
        by a model, and a rename of theirs is never replaced."""
        # What the envelope cannot say about its own task. The capability and the
        # destination come from the resolved intent, and on a later envelope of
        # the same task (an approval carried out, say) from the task block this
        # window already holds. The presentation boundary needs them to decide
        # what this task's RESULT may be (present._grounded).
        prior = self._live_slots()
        facts = _run_facts(self._task, payload, intent)
        task_id = str((payload.get("task") or {}).get("id") or "") if isinstance(payload, dict) and isinstance(payload.get("task"), dict) else ""
        run_status = str(payload.get("run_status") or "") if isinstance(payload, dict) else ""
        # A completed result shown for the first time carries what the run took; a
        # re-delivered one (a resume, a retry, a reopen) is not measured again.
        fresh = bool(task_id) and run_status == "completed" and f"result:{task_id}:{run_status}" not in self._conversation_store.state.keys
        result_usage = (payload.get("result") or {}).get("usage") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else None
        for entry in run_entries(payload, facts):
            if fresh and entry.get("kind") == "result":
                measured = self._measured(*self._intent_reports(intent), result_usage)
                if measured:
                    entry["telemetry"] = measured
            if entry.get("kind") == "result":
                entry["path"] = _proved_path(payload)
            self._say(entry.pop("kind"), entry.pop("text"), task_id=task_id, run_status=run_status, **entry)
        self._set_live(_project_run(prior, payload, intent))
        if self._task.get("task_id"):
            if request:
                self._conversation_store.name_auto((self._task["task_id"], self._session_id), task_title(request))
            self._task["title"] = self._conversation_store.title(self._task["task_id"])
        if self._ticket is not None:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Approval required: %1 -> %2"),
                        (self._approval.get("action"), self._k3_card(dict(self._approval)).get("destination"))), "approval")
        elif self._last_outcome == "unknown":
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "OUTCOME UNKNOWN for task %1"), (self._task.get("task_id"),)), "unknown")
        elif self._last_outcome == "unmet" and self._task:
            # The run ended; what was asked did not happen. The live feed says that,
            # not "completed", which would be a claim JARVIS cannot make.
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Task %1 ended without achieving what was asked"),
                        (self._task_label(),)), "unmet")
        elif self._last_outcome == "completed" and self._task:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Task %1 completed"),
                        (self._task_label(),)), "system")
        elif self._task:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Task %1 %2"),
                        (self._task_label(), self._task.get("run_status"))), "system")
        self.approvalChanged.emit()
        self.dataChanged.emit()

    # -- streaming preview ---------------------------------------------------

    def _measured(self, *reports: Any) -> dict:
        """What the finished run took, measured - once: the next answer of the
        same run gets nothing. ``first_ms`` is the first text that became visible;
        ``thinking_ms`` only a provider's OWN reasoning time, never inferred;
        ``tokens`` only what the providers reported."""
        if self._work_ms is None:
            return {}
        measured = {"elapsed_ms": self._work_ms}
        thinking = next((r["thinking_ms"] for r in reports if isinstance(r, dict)
                         and type(r.get("thinking_ms")) is int and r["thinking_ms"] >= 0), None)
        if thinking is not None:
            measured["thinking_ms"] = thinking
        elif self._first_visible is not None and self._work_started is not None:
            measured["first_ms"] = max(0, round((self._first_visible - self._work_started) * 1000))
        tokens = reported_tokens(*reports)
        if tokens:
            measured["tokens"] = tokens
            cached = reported_cache_tokens(*reports)
            if cached:
                measured["cached_tokens"] = cached
        self._work_ms = None
        return measured

    @staticmethod
    def _intent_reports(intent: Any) -> tuple:
        usage = intent.get("usage") if isinstance(intent, dict) and isinstance(intent.get("usage"), dict) else {}
        research = usage.get("research") if isinstance(usage.get("research"), dict) else {}
        return usage.get("reported"), research.get("reported")

    def _task_label(self) -> str:
        """The live task as notes name it: its title, else a short id."""
        return self._task.get("title") or str(self._task.get("task_id") or "")[:8]

    def _preview_relay(self, request_id: str) -> Callable[[str], None]:
        """What the worker calls with each reply preview; it reaches the GUI thread queued."""
        if self.sync:
            return lambda text: self._on_preview(request_id, text)
        emitter = self._emitter
        return lambda text: emitter.preview.emit(request_id, text)

    def _on_preview(self, request_id: str, text: str) -> None:
        # Only the request that is still running may show text, and only on its
        # own conversation's screen; elsewhere it waits in the run record (R1).
        if request_id != self._request_id or not self._busy or not isinstance(text, str):
            return
        record = self._run_record
        if record is not None:
            if not record["preview"]:
                record["stream_ts"] = time.time()
            record["preview"] = redact_text(text)
        if self._first_visible is None and text.strip():
            self._first_visible = self._clock()       # the run's telemetry, wherever it is shown
        if not self.openRunning:
            return
        if not self._stream_text:
            self._stream_ts = time.time()
        self._stream_text = redact_text(text)
        self.streamChanged.emit()

    def _end_stream(self) -> str:
        partial, self._stream_text = self._stream_text, ""
        if partial:
            self.streamChanged.emit()
        return partial

    streamChanged = Signal()

    @Property(str, notify=streamChanged)
    def streamText(self) -> str:
        """The General Chat reply visible so far while it streams, else ""."""
        return self._stream_text

    @Property(str, notify=streamChanged)
    def streamTime(self) -> str:
        """The streaming turn's one stable time."""
        if not self._stream_text:
            return ""
        return QLocale().toString(QDateTime.fromSecsSinceEpoch(int(self._stream_ts)).time(), QLocale.FormatType.ShortFormat)

    def _apply_text(self, payload: Any, text: str, request: str = "") -> None:
        intent = payload.get("intent") if isinstance(payload, dict) else None
        if not isinstance(intent, dict):
            intent = None
        status = str((intent or {}).get("status") or "")
        partial = self._end_stream()
        # DFREPLY: the reply had started and did not finish. What was shown stays as
        # JARVIS's reply - in the history too - marked cut short with a notice saying
        # why, in place of the verdict card. Only that text: nothing of the proposal.
        seen = _kept_partial(partial) if partial and status not in ("conversation", "research") else ""
        kept = bool(seen)
        if isinstance(intent, dict) and isinstance(intent.get("usage"), dict):
            self._chat_usage = dict(intent["usage"])
            for call in (self._chat_usage, self._chat_usage.get("research")):
                if isinstance(call, dict) and isinstance(call.get("rate_limits"), dict):
                    self._claude_limits = {**call["rate_limits"], "seen_at": int(time.time())}
            if isinstance(self._chat_usage.get("quota"), dict):
                quota = self._chat_usage["quota"]
                self._codex_quota = {**quota, "seen_at": int(quota.get("checked_at") or time.time())}
            self.providersChanged.emit()
        verified = getattr(self.backend, "codex_verified", None)
        if isinstance(verified, dict) and verified:
            # What the last Codex call showed reaches the model list without a new provider check.
            for provider in self._providers:
                if provider.get("id") == "codex":
                    provider["models"] = [{**m, "verified": dict(verified.get(m.get("id"), {}))} for m in provider.get("models") or []]
            self.providersChanged.emit()
        entries = _cut_feed(seen, status, intent, payload) if kept else text_entries(payload)
        answered = status in ("conversation", "research")
        for entry in entries:
            # The reply itself; a cut-short reply's notice is JARVIS's own line, not part of it.
            reply = entry["kind"] != "notice" if answered else (kept and entry["kind"] == "jarvis")
            if reply and (answered or kept):        # DFSPEED: a reply cut short keeps its timing too
                measured = self._measured(*self._intent_reports(intent))
                if measured:
                    entry["telemetry"] = measured
            if status == "conversation" and not isinstance(entry.get("text"), str):
                # Separately answered groups: rendered once, in the interface language,
                # so the reply is ordinary text - copyable, quotable, part of the history.
                entry["text"] = render(entry["text"])
            if partial and reply:
                entry["streamed"] = True        # replaces the preview in place: no second entrance
            self._say(entry.pop("kind"), entry.pop("text"), chat=reply, **entry)
        self._clarification = {}
        if status == "structured":
            self._apply_run(payload.get("run"), intent, request=request or text)
            return
        self._set_live(_project_text(self._live_slots(), payload, text))
        if status == "clarification_required":
            pass                                # the clarification card asks
        elif status == "scheduled":
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Scheduled job %1"), ((payload.get("job") or {}).get("label"),)), "scheduled")
            self._run("jobs", lambda: self.backend.jobs(False), self._set_jobs)
        elif status == "conversation":
            pass                                # General Chat: the reply is the whole result
        elif status == "research":
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Web Research answered with %1 sources"),
                        (len((intent or {}).get("sources") or []),)), "system")
        else:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Intent %1"), (status,)), "error" if status != "scheduled" else "system")
        self.approvalChanged.emit()
        self.dataChanged.emit()

    # -- properties: state ---------------------------------------------------

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        """A run is active (the global gate: one run at a time)."""
        return self._busy

    # -- Gate 6G R1: switching conversations while a recorded run continues ----

    @Property(bool, notify=stateChanged)
    def openRunning(self) -> bool:
        """A run is active AND it is this screen's: the open conversation is the
        run record's origin - or the run has no record (a sweep), which pins the
        screen it started on. What the screen shows of a run follows this."""
        record = self._run_record
        return self._busy and (record is None or record["origin"] == self._conversation_store.id)

    @Property(bool, notify=stateChanged)
    def navigationLocked(self) -> bool:
        """A run without a record (the manual sweep) is active: nothing opens, as before R1."""
        return self._busy and self._run_record is None

    @Property(str, notify=stateChanged)
    def runningConversationId(self) -> str:
        """The conversation the recorded run in flight works for ("" when none)."""
        return str(self._run_record["origin"] or "") if self._busy and self._run_record is not None else ""

    @Property(str, notify=stateChanged)
    def runningProjectId(self) -> str:
        """That conversation's project ("" = Global, or no recorded run)."""
        return self._run_record["project"] if self._busy and self._run_record is not None else ""

    def _runs_in(self, conversation_id: str | None) -> bool:
        """The ONE running-only refusal: ``conversation_id`` is the conversation a
        recorded run works for - or a run without a record is active, which
        keeps every refusal it had before R1."""
        return self.navigationLocked or (self._busy and self._run_record is not None
                                         and self._run_record["origin"] == conversation_id)

    @Slot(result=bool)
    def openRunningConversation(self) -> bool:
        """The composer's link on another conversation: open the one JARVIS works for."""
        record = self._run_record
        if not self._busy or record is None or not record["origin"]:
            return False
        opened = self.openConversation(record["origin"])
        if opened:
            self.setPage("home")
        return opened

    @Property(str, notify=stateChanged)
    def coreState(self) -> str:
        return self._core_state

    @Property(float, notify=stateChanged)
    def workStartedAt(self) -> float:
        """When the request being worked on started, in epoch milliseconds; 0 when
        nothing is running here. Display only - the live turn's running time. The ONE
        measured duration of a finished answer is its telemetry (``_measured``)."""
        return self._work_started_at * 1000 if self.openRunning else 0.0

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
        return _here(dict(self._task))

    @Property("QVariantMap", notify=stateChanged)
    def progress(self) -> dict:
        """The header's four lifecycle stages, projected in present.task_progress."""
        return task_progress(self._task, self._core_state, self.openRunning)

    @Property(bool, notify=stateChanged)
    def taskLive(self) -> bool:
        return bool(self._session_id) and (self.openRunning or (
            bool(self._task.get("status")) and self._task.get("status") not in TERMINAL_STATUSES))

    # -- local telemetry: a gauge, never a capability ---------------------------

    telemetryChanged = Signal()

    def _sample_telemetry(self) -> None:
        try:
            sample = self._sampler.sample()
        except Exception:  # noqa: BLE001 - a gauge that cannot read shows nothing
            sample = {"available": False}
        if sample != self._telemetry:
            self._telemetry = sample
            self.telemetryChanged.emit()

    @Property("QVariantMap", notify=telemetryChanged)
    def telemetry(self) -> dict:
        """CPU, memory and disk use of THIS machine as the kernel reports them
        (``available`` False until read, and whenever a figure cannot be read -
        then the card shows nothing rather than a placeholder)."""
        return dict(self._telemetry)

    # -- current weather: read-only, one fixed place ----------------------------

    weatherChanged = Signal()
    _weatherDone = Signal(object)

    def _weather_tick(self) -> None:
        """Re-judge freshness every minute; refresh when due (about every 5 minutes)."""
        if self._shutting_down:
            return
        self._publish_weather()
        if not self._weather_key or self._weather_busy or time.monotonic() < self._weather_next:
            return
        self._weather_busy = True
        self._weather_next = time.monotonic() + weather.REFRESH_SECONDS
        cache, key, fetch, location = self._weather_cache, self._weather_key, self._weather_fetch, self._weather_location

        def work() -> tuple:
            try:
                return weather.refresh(cache, key, fetch, location)
            except Exception:  # noqa: BLE001 - weather never breaks the interface
                return location, None, "network"

        if self.sync:
            self._weather_finished(work())
            return

        def run() -> None:
            result = work()
            with self._weather_lock:
                if self._shutting_down:     # JARVIS is quitting: the result is dropped
                    return
                self._weatherDone.emit(result)

        threading.Thread(target=run, name="jarvis-weather", daemon=True).start()

    def _weather_finished(self, result: tuple) -> None:
        if self._shutting_down:
            return
        location, reading, error = result
        self._weather_busy = False
        if location is not None:
            self._weather_location = location
        if reading is not None:         # a failed refresh keeps the last good reading
            self._weather_reading = reading
        self._weather_error = error
        if error in ("rate_limited", "auth", "not_found", "no_key") or (error and location is None):
            self._weather_next = time.monotonic() + weather.HOLD_SECONDS
        self._publish_weather()

    def _publish_weather(self) -> None:
        view = weather.project(self._weather_location, self._weather_reading)
        if view != self._weather_view:
            self._weather_view = view
            self.weatherChanged.emit()

    @Property("QVariantMap", notify=weatherChanged)
    def weather(self) -> dict:
        """The clock card's weather: ``state`` fresh / stale / unavailable, and only
        real values (district, rounded temperature, condition key, reading and fetch
        times). No key, coordinate or error detail is ever part of it."""
        return dict(self._weather_view)

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
        return self._k3_card(dict(self._approval))

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
        clarifying = self._task.get("status") == "awaiting_clarification" and self._session_id and self._task.get("task_id")
        if clarifying and self._restored_unlinked():
            self._refuse_unlinked()          # R2: before anything changes or is written; the draft stays
            return False
        self._clarification, self._decision = {}, {}
        self.approvalChanged.emit()
        conversation = self._conversation_state()   # before this turn: what came earlier
        groups, attachments, mode = self._take_context()
        # The feed keeps what the turn carried (paths dropped) so later history
        # can name it briefly; the full content travels with this turn only. The
        # canonical turn keeps each reference's identity (and JARVIS's own
        # snapshot path, a local handle) for a later, explicit re-hydration.
        self._say("user", text,
                  groups=[{**g, "attachments": [self._shown(a) for a in g["attachments"]]} for g in groups],
                  attachments=[self._shown(a) for a in attachments], refs=turn_refs(groups, attachments))
        self.clearContext()
        self._draft_sent(text)
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        if clarifying:
            task_id = self._task["task_id"]
            self._run("task", lambda: self.backend.clarify(session_id, task_id, text), self._delivered(self._apply_run))
            return True
        self._start_text(text, session_id, request_id, conversation, groups, attachments, mode, self._provider_route())
        return True

    def _start_text(self, text: str, session_id: str | None, request_id: str, conversation: Any, groups: tuple,
                    attachments: tuple, mode: str, route: dict, origin: PersistedConversation | None = None,
                    claude: tuple | None = None, queued_ms: int | None = None) -> None:
        """Start one new text request: the open conversation's - or (DFQUEUE) a queued
        message's whose conversation ``origin`` is not open, its screen left untouched.
        ``queued_ms`` (DFSPEED) is how long a queued message waited: it joins the turn's
        ``timing`` in the envelope's usage record."""
        if origin is None:
            self._task = {}
            if self._clarification or self._decision:   # DFQUEUE: a queued start leaves no old card above it
                self._clarification, self._decision = {}, {}
                self.approvalChanged.emit()
            self._end_stream()
        language = self.language           # the default reply language for General Chat
        owner = (origin or self._conversation_store).id or ""     # its owner scopes the request ("" = unsaved Global)
        relay = self._preview_relay(request_id)
        # The files this request references: its run record keeps them until the run has finished (R1).
        snapshots = frozenset(item["path"] for item in (*attachments, *(a for g in groups for a in g["attachments"]))
                              if type(item.get("path")) is str)
        extra = {} if claude is None else {"claude": claude}
        self._run(
            "task",
            lambda: _waited(self.backend.submit_text(text, session_id, request_id=request_id, response_language=language,
                                                     conversation=conversation, context_groups=groups,
                                                     context_attachments=attachments, response_mode=mode,
                                                     on_reply_preview=relay, conversation_id=owner, **route, **extra),
                            queued_ms),
            self._delivered(lambda payload: self._apply_text(payload, text), snapshots,
                            project=lambda prior, payload: _project_text(prior, payload, text), origin=origin,
                            identity=None if origin is None else (session_id or "", "")),
        )

    @Slot(str)
    def chooseOption(self, label: str) -> None:
        """Send back EXACTLY one backend-offered label; the backend re-proves it."""
        clarification = self._clarification
        if self._busy or not clarification:
            return
        if not any(choice.get("label") == label for choice in clarification["choices"]):
            return
        if self._restored_unlinked():
            self._refuse_unlinked()          # R2: a sessionless intent clarification stays allowed
            return
        previous = clarification["previous_text"]
        self._clarification = {}
        self.approvalChanged.emit()
        conversation = self._conversation_state()
        self._say("user", label)
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        language = self.language
        route = self._provider_route()
        owner = self._conversation_store.id or ""
        self._end_stream()
        relay = self._preview_relay(request_id)
        self._run(
            "task",
            lambda: self.backend.submit_text(label, session_id, previous_text=previous, request_id=request_id,
                                             response_language=language, conversation=conversation,
                                             on_reply_preview=relay, conversation_id=owner, **route),
            self._delivered(lambda payload: self._apply_text(payload, label, request=previous),
                            project=lambda prior, payload: _project_text(prior, payload, label)),
        )

    @Slot()
    def dismissClarification(self) -> None:
        self._clarification = {}
        self.approvalChanged.emit()
        self._refresh_core_state()

    @Slot(result=bool)
    def cancelText(self) -> bool:
        """Stop the running understanding call for the CURRENT request only -
        from its own conversation's screen, by the run record's request (R1)."""
        record = self._run_record
        request_id = record["request_id"] if record is not None else self._request_id
        if not self.openRunning or not request_id:
            return False
        return self.backend.cancel_text(request_id)

    # -- slots: approval / task ------------------------------------------

    def _decide(self, verb: str, request_id: str | None = None, fingerprint: str | None = None,
                after: tuple[Callable[[], None], Callable[[], None]] | None = None) -> bool:
        """Submit one decision for the request ON SCREEN. With an identity, it
        must still be the current request - a newer revision is never decided
        with an older rendering. Core re-proves the ticket either way.
        ``after`` (6H K3 pick-existing): ``(work, shown)`` - ``work`` runs on
        the worker once Core has answered (or the call failed); ``shown`` runs
        when that answer is delivered, to whichever conversation."""
        ticket = self._ticket
        if ticket is None or self._busy or not self._approval.get("complete"):
            return False
        if verb == "approve" and not self._k3_card(dict(self._approval)).get("complete"):
            return False            # 6H H3: a K3 card is approvable only with its exact path shown
        if not self._linked_here(ticket.session_id):
            return False            # R1: never a ticket of a conversation other than the open one
        if request_id is not None and (str(request_id), str(fingerprint)) != (ticket.request_id, ticket.fingerprint):
            return False
        shown = self._approval
        self._ticket, self._approval = None, {}
        # Labels are kept as catalog sources and rendered by ``safety``.
        self._decision = {"state": "submitting", "verb": verb, "label": QT_TRANSLATE_NOOP("Bridge", "Submitting decision…"),
                          "action": shown.get("action", ""), "destination": self._k3_card(dict(shown)).get("destination", ""),
                          "code": "", "message": "", "time": ""}
        self.approvalChanged.emit()
        call = self.backend.approve if verb == "approve" else self.backend.reject
        work, then = after or (lambda: None, lambda: None)

        def decided() -> Any:
            try:
                return call(ticket)
            finally:
                work()

        deliver = self._delivered(
            lambda payload: self._apply_decision(payload, verb, ticket, shown),
            project=lambda prior, payload: _kept_refusal(_project_run(prior, payload), payload, ticket, shown))

        def delivered(payload: Any) -> None:
            deliver(payload)
            then()                      # whichever conversation the answer went to

        vars(delivered).update(vars(deliver))       # the run record reads the origin and snapshots
        self._run("task", decided, delivered, executing=verb == "approve")
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
        kept = _kept_refusal(self._live_slots(), payload, ticket, shown)
        if kept["ticket"] is not self._ticket:
            # Core refused the decision before touching the request, which is
            # still pending exactly as it was: keep showing it.
            self._ticket, self._approval = kept["ticket"], kept["approval"]
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

    # -- Gate 6H H3 (K3): the project folder approval card --------------------

    def _k3_card(self, approval: dict) -> dict:
        """Display only, for a K3 request: the destination reads "This computer"
        (never the grant row's) and the target is the exact path the request
        binds, read from its task (Core logs the request, so the path is not
        in it). Without that path the card is not approvable."""
        if approval.get("connector") != PROJECT_FOLDER_CONNECTOR:
            return approval
        key = str(approval.get("request_id") or "")
        if self._k3_folder.get("request_id") != key:
            found = self.backend.project_folder_request(
                str(approval.get("session_id") or ""), str(approval.get("task_id") or ""), key)
            # Only a found path is kept: a failed read is asked again next time.
            self._k3_folder = {"request_id": key, **found} if found.get("path") else {}
            folder = found
        else:
            folder = self._k3_folder
        here = tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "This computer"))
        # The grant row's destination is a label here: it names no endpoint.
        shown = {key: str(approval.get(key) or "").replace(CLAUDE_CODE_DESTINATION, here)
                 for key in ("outbound_preview", "attestation")}
        return {**approval, **shown, "destination": here,
                "target": folder.get("path", ""), "folder_location": folder.get("location", ""),
                "folder_project": folder.get("project_id", ""),
                "complete": bool(approval.get("complete") and folder.get("path"))}

    @Slot(str, str, result=bool)
    def switchProjectFolderLocation(self, request_id: str, fingerprint: str) -> bool:
        """E7 "Masaüstünde oluştur" for the K3 request ON SCREEN: once per request
        id, never while frozen or busy. The app rejects it through Core and only
        then asks for the other location (a new card); it never approves."""
        ticket = self._ticket
        if (ticket is None or self._busy or not self.executionEnabled or str(request_id) in self._k3_switched
                or (str(request_id), str(fingerprint)) != (ticket.request_id, ticket.fingerprint)
                or self._approval.get("connector") != PROJECT_FOLDER_CONNECTOR or not self._linked_here(ticket.session_id)):
            return False
        self._k3_switched.add(ticket.request_id)

        def kept(prior: dict, payload: Any) -> dict:
            # A refusal changed nothing: that conversation keeps its card.
            return _project_run(prior, payload) if isinstance(payload, dict) and payload.get("run_status") else prior

        self._run("task", lambda: self.backend.project_folder_switch(ticket), self._delivered(self._k3_switched_to, project=kept))
        return True

    @Slot(str, str, str, str, result=bool)
    def pickExistingFolder(self, project_id: str, request_id: str, fingerprint: str, path: str) -> bool:
        """K3 "Choose an existing folder…" for the project and request the
        picker was bound to. The pick is checked first (reads only): a refused
        folder decides nothing. Then the request ON SCREEN is rejected through
        Core while its project is still folderless, so it ends REJECTED, and
        the folder is linked right after Core's answer, whatever it was. While
        an operation runs, the request on screen is not picked for (nothing
        changes; pick again). A request not on this screen stays pending and
        the folder is linked at once: Core never runs a K3 request for a
        project that has a folder. Nothing is created either way."""
        project_id, path = str(project_id), str(path)
        ticket = self._ticket
        if self._busy and ticket is not None and (ticket.request_id, ticket.fingerprint) == (str(request_id), str(fingerprint)):
            return False
        if not project_id or not self._folder_change("check", project_id, path=path):
            return False
        store, loaded = self._conversation_db(), self.backend.store.load()
        said: list[str] = []

        def link() -> None:
            try:
                said.append(self._error_message(project_folder_result(
                    store, loaded.settings, "link", project_id, path=path).payload))
            except Exception as exc:  # noqa: BLE001 - storage boundary: only the type
                said.append(type(exc).__name__)

        def shown() -> None:
            self._project_folder_notice = self._folder_reason(said[0]) if said and said[0] else ""
            self.refreshProjects()

        if self._decide("reject", request_id, fingerprint, after=(link, shown)):
            return True
        return self._folder_change("link", project_id, path=path)

    def _k3_switched_to(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("run_status"):
            self._apply_run(payload)          # the new request's card (or what Core did with the rejection)
            if payload.get("notice"):
                # The app's two E7 notices, in the interface language (catalog sources).
                notice = str(payload["notice"])
                self._note(("Bridge", notice, ()) if notice in _K3_NOTICES else notice, "error")
        else:
            # A message (re-rendered on a language switch): the app's E7 refusal,
            # Core's refusal of the rejection itself, or one generic line - never raw.
            message = self._error_message(payload)
            code = str(payload["error"].get("code") or "") if message else ""
            self._note(("Bridge", self._FOLDER_REASONS[message], ()) if message in self._FOLDER_REASONS
                       else ("Bridge", QT_TRANSLATE_NOOP("Bridge", "Decision not recorded"), ()) if not message
                       else ("Bridge", QT_TRANSLATE_NOOP("Bridge", "Request changed, expired or was resolved elsewhere — "
                                                                   "decision not recorded"), ())
                       if code in ("invalid_grant", "stale_grant", "task_not_found")
                       else ("Present", FOLDER_UNKNOWN, ()), "error")

    @Slot()
    def cancelTask(self) -> None:
        """Cancel through Core; while running the id is storage's, not ours. On the
        screen of a recorded run (R1) that is the RUN's session - the screen's may
        have been reset by leaving and returning - with its cached task as fallback."""
        record = self._run_record if self.openRunning else None
        if record is not None:
            session_id = record["session_id"]
            task_id = (self.backend.active_task_id(session_id) if session_id else None) or record["task_id"]
        else:
            session_id = self._session_id
            if self._restored_unlinked():
                self._refuse_unlinked()      # R2: only this conversation's own task, before any call
                return
            task_id = (self.backend.active_task_id(session_id) if self.openRunning and session_id
                       else self._task.get("task_id"))
        if not session_id or not task_id:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "No cancellable task is registered yet."))
            return
        self._ticket, self._approval = None, {}
        self.approvalChanged.emit()
        self._run("cancel", lambda: self.backend.cancel(session_id, task_id), self._delivered(self._apply_run))

    def _discard_snapshots(self) -> None:
        """Throw away every attachment snapshot JARVIS copied for this session -
        except those the recorded run in flight still references: a provider
        reads them later (and skips a missing one), so they go when it ends (R1) - and
        those another conversation's parked draft holds (DFDRAFT)."""
        self.clearContext()
        record = self._run_record if self._busy else None
        if record is not None and record["snapshots"]:
            record["discard"] = True
        keep = self._draft_snapshots() | (record["snapshots"] if record is not None else frozenset())
        if not keep:
            discard_snapshots(self._snapshots)
            return
        for path in self._snapshots.glob("*"):
            if str(path) not in keep:
                with contextlib.suppress(OSError):
                    path.unlink()

    @Slot()
    def newSession(self) -> None:
        if self._runs_in(self._conversation_store.id):
            return
        self._session_id, self._task, self._ticket, self._approval, self._clarification = "", {}, None, {}, {}
        self._decision, self._restored_session = {}, ""
        self._discard_snapshots()       # a new session keeps none of the old drafts' files
        self._last_outcome = "idle"
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "New session: the next message starts a fresh session."), ()), "system")
        self.approvalChanged.emit()
        self._refresh_core_state()

    def _open_owner_of(self, session_id: str) -> bool:
        """Open the conversation that owns ``session_id`` (if one does)."""
        owner = self._session_conversation(session_id)
        return owner is None or self.openConversation(owner)

    @Slot(str)
    def useSession(self, session_id: str) -> None:
        session_id = str(session_id)
        if not session_id:
            return
        # The owner is resolved ONCE, before anything changes. While a run is active (R1, FAIL
        # CLOSED) it is refused for a run without a record, an unlinked session or a failed lookup
        # (both None) and the running conversation's own session: each would rebind that conversation.
        owner = self._session_conversation(session_id)
        if self._busy and (self._run_record is None or owner is None or owner == self._run_record["origin"]):
            return
        if owner is None or self.openConversation(owner):
            self._session_id, self._task, self._ticket, self._approval, self._decision = session_id, {}, None, {}, {}
            self._clarification, self._restored_session = {}, ""   # R2 (OD6): no waiting slot outlives a session choice
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Using session %1."), (session_id,)), "system")
            self.approvalChanged.emit()
            self._refresh_core_state()

    @Slot(str, str)
    def resumeTask(self, session_id: str, task_id: str) -> None:
        if self._busy or not self._open_owner_of(str(session_id)):
            return
        self._session_id = str(session_id)
        self._run("task", lambda: self.backend.resume(str(session_id), str(task_id)), self._delivered(self._apply_run),
                  executing=True)

    # -- slots: read-only data ---------------------------------------------

    @Slot()
    def refreshConfig(self) -> None:
        self._config = self.backend.config()
        self._status = self.backend.status()
        self._refresh_core_state()

    @Slot()
    def refreshHistory(self) -> None:
        self._recheck_open()
        self._run("sessions", self.backend.sessions, self._set_sessions)

    def _of_deleted_conversation(self, session_id: str) -> bool:
        """The session belongs to a permanently deleted conversation (Gate 6D-b)."""
        linked, store = self._session_conversation(session_id), self._conversation_db()
        if linked is None or store is None:
            return False
        try:
            return store.is_conversation_deleted(linked)
        except Exception:  # noqa: BLE001 - storage boundary: listed as before
            return False

    def _set_sessions(self, payload: Any) -> None:
        # A deleted conversation's sessions leave the list (it cannot be opened);
        # their tasks and results stay on the Tasks page.
        listing = [row for row in rows(payload, "sessions", "session_id")
                   if not self._of_deleted_conversation(str(row.get("session_id") or ""))]
        self._sessions = self._with_latest_task(self._titled(listing, "session_id"))
        if isinstance(payload, dict) and payload.get("error") is not None:
            self._last_error = str(payload["error"].get("message"))
        self.dataChanged.emit()

    @Slot(str)
    def loadTasks(self, session_id: str) -> None:
        self._run("tasks", lambda: self.backend.tasks(str(session_id)), self._set_tasks)

    def _set_tasks(self, payload: Any) -> None:
        self._tasks = self._titled(rows(payload, "tasks", "task_id"), "task_id")
        self.dataChanged.emit()

    def _owner_store(self, session_id: str, cache: dict) -> PersistedConversation:
        """The conversation holding a session's (and its tasks') titles."""
        owner = self._session_conversation(session_id)
        if owner is None or owner == self._conversation_store.id:
            return self._conversation_store
        if owner not in cache:
            store = self._conversation_db()
            try:
                cache[owner] = PersistedConversation(store, owner, store.load_conversation_state(owner))
            except Exception:  # noqa: BLE001 - a label only: unreadable reads as untitled
                cache[owner] = PersistedConversation(None, None)
        return cache[owner]

    def _with_latest_task(self, sessions: list) -> list:
        """Gate 6F (tile T1), read only: the newest readable session also carries
        its latest task's status, time and name - one task listing, never one
        per session."""
        newest = next((s for s in sessions if s.get("readable") and s.get("latest_task_id")), None)
        if newest is None:
            return sessions
        sid, tid = str(newest["session_id"]), str(newest["latest_task_id"])
        latest = next((t for t in rows(self.backend.tasks(sid), "tasks", "task_id") if t.get("task_id") == tid), None)
        if latest is None:
            return sessions
        newest.update(latest_status=str(latest.get("status") or ""), latest_updated_at=str(latest.get("updated_at") or ""),
                      latest_title=self._owner_store(sid, {}).title(tid))
        return sessions

    def _titled(self, listing: list, key: str) -> list:
        """Each row with its display name ("" when it has none); the id stays."""
        cache: dict = {}
        return [{**row, "title": self._owner_store(str(row.get("session_id") or ""), cache).title(str(row.get(key) or ""))}
                for row in listing]

    @Slot(str, str, result=bool)
    def renameTask(self, item_id: str, title: str) -> bool:
        """The person's own name for a task or session; it always wins over the
        automatic one. An empty name changes nothing (False)."""
        session_id = next((str(row.get("session_id") or "") for row in (*self._sessions, *self._tasks)
                           if item_id in (row.get("session_id"), row.get("task_id"))), "")
        owner = self._owner_store(session_id, {})
        if not owner.rename(str(item_id), str(title)):
            return False
        self._sessions = self._titled(self._sessions, "session_id")
        self._tasks = self._titled(self._tasks, "task_id")
        if self._task.get("task_id") == item_id:
            self._task["title"] = self._conversation_store.title(item_id)
            self.stateChanged.emit()
        self.dataChanged.emit()
        return True

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
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Running one scheduler sweep..."), ()), "scheduled")
        self._run("task", self.backend.scheduler_once, self._apply_manual_sweep)

    def _apply_manual_sweep(self, payload: Any) -> None:
        """The Automations button's sweep: what the scheduler did goes to the
        live feed; only a failure to sweep reaches the conversation."""
        if not isinstance(payload, dict):
            self._apply_run(payload)
            return
        if payload.get("error") is not None:
            for entry in error_entries(payload["error"]):
                self._say(entry["kind"], entry["text"])
        actions = [a for a in payload.get("actions") or [] if isinstance(a, dict)]
        for action in actions[:10]:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Scheduled job %1: %2"),
                        (str(action.get("job_id") or ""), str(action.get("action") or ""))), "scheduled")
        if not actions and payload.get("error") is None:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Scheduler sweep: nothing started - JARVIS is frozen")
                        if payload.get("claims_suspended") else QT_TRANSLATE_NOOP("Bridge", "Scheduler sweep: nothing was due"), ()), "scheduled")
        self._run("jobs", lambda: self.backend.jobs(False), self._set_jobs)

    @Slot()
    def sweepDueWork(self) -> None:
        """The in-app scheduler loop's one sweep: drive the EXISTING scheduler.

        Skipped while a foreground operation runs, while a sweep is already in
        flight and while the configuration is refused, so sweeps never overlap
        and an idle one stays cheap. It adds no authority: Core's claim, lease,
        fencing, approval and execution-gate rules decide everything, exactly
        as they do for the Automations button.
        """
        if self._busy or self._sweeping or self._config_refused():
            return
        self._sweeping = True
        self._run("sweep", self.backend.scheduler_once, self._apply_sweep)

    def _apply_sweep(self, payload: Any) -> None:
        """A background sweep never disturbs what is on screen: it replaces no
        approval, task or outcome. Only a sweep that really acted refreshes the
        job list, so the Automations page keeps projecting real state."""
        self._sweeping = False
        actions = payload.get("actions") if isinstance(payload, dict) else None
        if actions:
            # What the scheduler really did, in the live feed: one line per action.
            for action in actions[:10]:
                if isinstance(action, dict):
                    self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Scheduled job %1: %2"),
                                (str(action.get("job_id") or ""), str(action.get("action") or ""))), "scheduled")
            self._run("jobs", lambda: self.backend.jobs(False), self._set_jobs)

    # -- providers and usage -------------------------------------------------

    @Property("QVariantList", notify=providersChanged)
    def providers(self) -> list:
        """The provider lines and their REAL route state (``Backend.providers``);
        empty until the first local check has answered."""
        return [dict(provider) for provider in self._providers]

    @Property(bool, notify=providersChanged)
    def showOldModels(self) -> bool:
        """Window-only (never written to the preferences): show every model
        the providers listed, not just the newest version per model name."""
        return self._show_old_models

    @Property(dict, notify=providersChanged)
    def modelLists(self) -> dict:
        """DISPLAY ONLY (S5): per provider the ids the selector shows and
        ``{hidden id: keeper id}`` for the older versions of a model name. The
        cache and ``providers`` keep the full lists; nothing here routes,
        verifies or authorizes anything."""
        selected = {"claude": str(self._config.get("claude_model") or ""),
                    "codex": str(self.prefs.data["chat"]["codex_model"] or "")}
        lists: dict[str, dict] = {}
        for provider in self._providers:
            pid = provider.get("id")
            if pid not in selected:
                continue
            models = provider.get("models") or []
            shown, hidden = newest_per_name(models, provider=pid, selected=selected[pid])
            if self._show_old_models:
                shown = models
            lists[pid] = {"shown": [m["id"] for m in shown if isinstance(m, dict) and "id" in m], "hidden": hidden}
        return lists

    _catalogDone = Signal()
    _liveLine = Signal(str, object)

    def _probe_provider(self, pid: str) -> Any:
        """The catalog's live check (its own thread). This fresh answer - never
        the cache - is the only thing that may move a saved model choice."""
        line = self.backend.provider_line(pid)
        if self.sync:
            self._follow_models(pid, line)
        else:
            self._deliver("_liveLine", pid, line)
        return line

    def _deliver(self, signal_name: str, *args: Any) -> None:
        """Emit the signal named ``signal_name`` from a worker thread, only into a live Bridge.

        Never after ``shutdown()``, and never into a Bridge whose C++ object is
        gone: ``shutdown()`` guards a teardown that runs it, but a Bridge
        deleted WITHOUT it (an early return from ``main``, a launch path that
        does not tear down) would otherwise raise "Signal source has been
        deleted" in the probe thread. A late result is dropped here; the
        catalog has already saved it to its own file. Nothing blocks or joins.
        """
        with self._weather_lock:            # the shutdown lock: after quit begins, nothing is delivered
            if self._shutting_down or not shiboken6.isValid(self):
                return
            try:
                getattr(self, signal_name).emit(*args)
            except RuntimeError:            # deleted between the check and the emit
                pass

    def _follow_models(self, pid: str, line: Any) -> None:
        """A saved choice follows its provider-declared successor (see
        jarvis.model_retirement) - only from a successful discovery that listed models."""
        if self._shutting_down or not isinstance(line, dict) or line.get("status") != "available" or not line.get("models"):
            return
        if pid == "claude":
            self._follow_claude(line["models"])
        elif pid == "codex":
            self._follow_codex(line["models"], line.get("retired") if isinstance(line.get("retired"), dict) else {})

    def _follow_claude(self, models: list) -> None:
        loaded = self.backend.store.load()
        if not loaded.usable or not loaded.settings.claude_model:
            return                                  # the CLI default follows the CLI by itself
        model, effort = loaded.settings.claude_model, loaded.settings.claude_effort or ""
        chat = self.prefs.data["chat"]
        lane = chat["claude_lane"] if chat["claude_lane_model"] == model else adopt_claude_lane(models, model)
        if lane and chat["claude_lane_model"] != model:
            self._set_claude_lane(lane, model)      # a choice from before lanes: its one unambiguous alias
        moved = claude_migration(models, lane, model, effort)
        if moved is None:
            return
        payload = self._write_claude(*moved)
        self._apply_config(payload)
        if payload.get("error") is None:
            self._set_claude_lane(lane, moved[0])
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Claude model updated from %1 to %2"), (model, moved[0])), "system")
            self.modelApplied.emit()

    def _follow_codex(self, offered: list, retired: dict) -> None:
        chat = self.prefs.data["chat"]
        moved = codex_migration(chat["codex_model"], chat["codex_effort"], offered, retired)
        if moved is None:
            return
        before = chat["codex_model"]
        self.setCodexModel(*moved)
        if self.prefs.data["chat"]["codex_model"] == moved[0]:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Codex model updated from %1 to %2"), (before, moved[0])), "system")

    def _set_claude_lane(self, lane: str, model: str) -> None:
        chat = dict(self.prefs.data["chat"], claude_lane=lane, claude_lane_model=model)
        if chat != self.prefs.data["chat"] and validate_preferences({**self.prefs.data, "chat": chat})["chat"] == chat:
            self.prefs.data["chat"] = chat
            self.prefs.save()

    @Slot()
    def refreshProviders(self, force: bool = False) -> None:
        """Never waits: re-checks, in the background, only the provider lines
        older than the catalog's TTL (all of them when ``force``); a provider
        already being checked is not checked twice."""
        self._catalog.refresh(force)

    def _catalog_changed(self) -> None:
        if self.sync:
            self._show_catalog()
            return
        self._deliver("_catalogDone")

    def _show_catalog(self) -> None:
        if self._shutting_down:
            return
        lines = self._catalog.snapshot()
        self._set_providers(lines + [self.backend.qwen_line()] if lines else [])

    def _set_providers(self, payload: Any) -> None:
        self._providers = [dict(p) for p in payload if isinstance(p, dict)] if isinstance(payload, list) else []
        verified = getattr(self.backend, "codex_verified", None) or {}
        for provider in self._providers:
            if isinstance(provider.get("quota"), dict) and provider["quota"]:
                quota = provider["quota"]
                provider["quota"] = {**quota, "seen_at": int(quota.get("checked_at") or time.time())}
            if provider.get("id") == "codex":
                # What real calls showed this session (never cached) rides on the listed models.
                provider["models"] = [{**m, "verified": dict(verified.get(m.get("id"), {}))}
                                      for m in provider.get("models") or [] if isinstance(m, dict)]
        self.providersChanged.emit()

    def _provider_route(self) -> dict:
        """The chosen provider and ITS model / effort for the next call."""
        chat = self.prefs.data["chat"]
        if chat["provider"] == "codex":
            return {"provider": "codex", "model": chat["codex_model"], "effort": chat["codex_effort"]}
        return {"provider": "claude"}

    @Property("QVariantMap", notify=dataChanged)
    def chatUsage(self) -> dict:
        """The last understanding / General Chat call's usage exactly as reported
        (``reported``: the provider's own token fields and model; absent fields
        were not reported) plus measured request / conversation / catalogue sizes."""
        return dict(self._chat_usage)

    @Property("QVariantMap", notify=providersChanged)
    def codexQuota(self) -> dict:
        """Codex plan usage windows: from the newest of the provider check and
        the last Codex call (``seen_at``), or {} when none was reported."""
        probed = next((p.get("quota") for p in self._providers if p.get("id") == "codex" and p.get("quota")), None)
        if probed and probed.get("seen_at", 0) >= self._codex_quota.get("seen_at", 0):
            return dict(probed)
        return dict(self._codex_quota)

    # -- the compact model card: one shape for every selectable provider ---------

    @Slot(str, str, result=str)
    def modelLabel(self, model_id: str, fallback: str = "") -> str:
        """The SHORT display name of a model id. Display only: the canonical id
        is what every call still uses, and nothing here changes it."""
        return display_model_name(model_id, fallback)

    def _claude_model_id(self) -> str:
        """The Claude model this installation will really use: what is configured,
        or - for the CLI default - the id the CLI says that default resolves to."""
        configured = str(self._config.get("claude_model") or "")
        if configured:
            return configured
        models = next((p.get("models") or [] for p in self._providers if p.get("id") == "claude"), [])
        entry = next((m for m in models if isinstance(m, dict) and m.get("id") == "default"), None)
        return str((entry or {}).get("resolved") or "")

    def _claude_card_quota(self, model_id: str) -> list:
        windows = (self._claude_limits or {}).get("windows") or {}
        if not isinstance(windows, dict):
            return []
        # The weekly bucket that genuinely belongs to the chosen model, when the
        # plan exposes one; otherwise the all-model week. Any further bucket stays
        # in the detailed panel rather than crowding the card.
        family = model_id.lower()
        weekly = "seven_day"
        for name in ("seven_day_opus", "seven_day_sonnet"):
            if name in windows and name.rsplit("_", 1)[-1] in family:
                weekly = name
        rows = []
        for key, label in (("five_hour", "five_hour"), (weekly, "weekly")):
            window = windows.get(key)
            if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
                rows.append({"window": label, "remaining": max(0, 100 - round(window["utilization"] * 100)),
                             "resets_at": int(window.get("resets_at") or 0)})
        return rows

    def _codex_card_quota(self) -> list:
        quota = self.codexQuota
        rows = []
        for key, label in (("primary", "five_hour"), ("secondary", "weekly")):
            window = quota.get(key)
            if isinstance(window, dict) and isinstance(window.get("used_percent"), (int, float)):
                rows.append({"window": label, "remaining": max(0, 100 - round(window["used_percent"])),
                             "resets_at": int(window.get("resets_at") or 0)})
        return rows

    @Property("QVariantMap", notify=providersChanged)
    def modelCard(self) -> dict:
        """The compact card's content, the same shape for every provider: the
        short model name, ``BRAND | EFFORT`` and at most two quota rows.

        Every value is provider-reported truth. A field no provider reported is
        absent - never estimated, never carried over from another provider.
        """
        provider = self.chatProvider
        if provider == "codex":
            model_id, effort = self.codexModel, self.codexEffort
            quota = self._codex_card_quota()
        else:
            model_id, effort = self._claude_model_id(), str(self._config.get("claude_effort") or "")
            quota = self._claude_card_quota(model_id)
        brand = provider_brand(provider)
        effort = str(effort).upper()
        return {"provider": provider, "model": model_id,
                "name": display_model_name(model_id),
                "brand": brand, "effort": effort,
                "meta": f"{brand}  |  {effort}" if effort else brand,
                "quota": quota}

    @Property("QVariantMap", notify=providersChanged)
    def usageLimits(self) -> dict:
        """The usage popover's content for the chosen provider (G6MP1): every
        window it REPORTED (``rows``, the card's row shape; Claude's per-model
        weeks carry ``model``), and the details only the old Model panel showed
        (route status, the chosen Codex model's verified state, plan facts, Web
        Research, extra paid usage / credits, the last call's tokens).
        A key no provider reported is absent - never estimated."""
        provider = self.chatProvider
        entry = next((p for p in self._providers if p.get("id") == provider), {})
        found: dict = {"provider": provider}
        if entry.get("status"):
            found["status"] = str(entry["status"])
        if entry.get("plan"):
            found["plan"] = str(entry["plan"])
        if type(entry.get("research")) is bool:
            found["research"] = entry["research"]
        if provider == "codex":
            limits = self.codexQuota
            found["rows"] = self._codex_card_quota()
            if type(limits.get("has_credits")) is bool:
                found["credits"] = limits["has_credits"]
            # Listed is not usable: what a real call this session showed for the chosen model.
            listed = next((m for m in entry.get("models") or [] if isinstance(m, dict) and m.get("id") == self.codexModel), None)
            if listed is not None:
                found["verified"] = {**dict(listed.get("verified") or {}), "model": self.codexModel}
            usage_vendor = "openai"
        else:
            limits = dict(self._claude_limits)
            windows = limits.get("windows") if isinstance(limits.get("windows"), dict) else {}
            rows = []
            for key, label, model in (("five_hour", "five_hour", ""), ("seven_day", "weekly", ""),
                                      ("seven_day_opus", "weekly_opus", "Opus"), ("seven_day_sonnet", "weekly_sonnet", "Sonnet")):
                window = windows.get(key)
                if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
                    row = {"window": label, "remaining": max(0, 100 - round(window["utilization"] * 100)),
                           "resets_at": int(window.get("resets_at") or 0)}
                    rows.append({**row, "model": model} if model else row)
            found["rows"] = rows
            if limits.get("using_overage") is True:
                found["overage"] = "in_use"
            elif limits.get("overage_status") == "rejected":
                found["overage"] = "off"
            usage_vendor = "anthropic"
        if limits.get("seen_at"):
            found["seen_at"] = int(limits["seen_at"])
        usage = self._chat_usage
        if usage.get("provider") == usage_vendor and isinstance(usage.get("reported"), dict) and usage["reported"]:
            found["last_call"] = {"reported": dict(usage["reported"]),
                                  "calls": usage["call_count"] if type(usage.get("call_count")) is int else 1}
        return found

    # -- chat provider: who answers and interprets (never authority) ---------------

    @Property(str, notify=prefsChanged)
    def chatProvider(self) -> str:
        return self.prefs.data["chat"]["provider"]

    @Property(str, notify=prefsChanged)
    def codexModel(self) -> str:
        return self.prefs.data["chat"]["codex_model"]

    @Property(str, notify=prefsChanged)
    def codexEffort(self) -> str:
        return self.prefs.data["chat"]["codex_effort"]

    @Slot(str)
    def setChatProvider(self, provider: str) -> None:
        """Manual only; persisted. Each provider keeps its own model and effort."""
        if provider in CHAT_PROVIDERS and provider != self.chatProvider:
            self.prefs.data["chat"]["provider"] = provider
            self.prefs.save()
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Chat provider: %1"), (provider,)), "system")
            self.prefsChanged.emit()

    @Slot(str, str)
    def setCodexModel(self, model: str, effort: str) -> None:
        chat = dict(self.prefs.data["chat"], codex_model=str(model), codex_effort=str(effort))
        if validate_preferences({**self.prefs.data, "chat": chat})["chat"] == chat:
            self.prefs.data["chat"] = chat
            self.prefs.save()
            self.prefsChanged.emit()

    # -- permission mode: Core's own policy, projected and changed through Core ----

    @Property(str, notify=stateChanged)
    def permissionMode(self) -> str:
        """The permission mode the stored configuration holds ("" before any read)."""
        return str(self._config.get("permission_mode") or "") if not self._config_refused() else ""

    @Slot(str, result=bool)
    def setPermissionMode(self, mode: str) -> bool:
        """Write ``permission_mode`` through the same config path the CLI uses;
        Core reads it on the next action. True only when the store confirms it."""
        if self._busy or mode not in ("strict", "smart_auto", "session") or mode == self.permissionMode:
            return mode == self.permissionMode
        self._apply_config(self.backend.set_config("permission_mode", mode))
        confirmed = self.permissionMode == mode
        if confirmed:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Permission mode: %1"), (mode,)), "system")
        return confirmed

    # -- draft context: reference GROUPS and the message's own attachments -------
    #
    # A quoted message or selection starts a CONTEXT GROUP that owns its own
    # note and its own files; files that belong to no quote are the message's
    # global attachments. Every part is addressed by a stable id, never by its
    # position. All of it is DATA for one turn: it selects no destination,
    # grants no capability, approves nothing and changes no permission mode.
    # An attachment is the exact file the person picked, snapshotted here at
    # attach time, so the draft cannot change under them and no path a model
    # produced can ever be read.
    #
    # Three separate signals, because a republished list rebuilds its QML
    # delegates: groups (membership only), attachments (any attachment, global
    # or in a group - group cards re-read theirs without being rebuilt) and the
    # answer mode. A note changes none of them, so its editor is never destroyed
    # while someone types in it.

    groupsChanged = Signal()
    attachmentsChanged = Signal()
    modeChanged = Signal()

    @staticmethod
    def _shown(item: dict) -> dict:
        """An item as the interface shows it. The original path was never kept;
        an image gets a ``preview`` URL to JARVIS's own snapshot copy, the only
        file a thumbnail or the lightbox ever reads."""
        shown = {key: value for key, value in item.items() if key != "path"}
        if item.get("kind") == "image" and type(item.get("path")) is str:
            shown["preview"] = QUrl.fromLocalFile(item["path"]).toString()
        return shown

    # -- attachment preview: display only ------------------------------------------

    #: The interface asked to look at one attachment (the lightbox / text view).
    previewRequested = Signal("QVariant")

    @Slot("QVariant")
    def previewAttachment(self, file: Any) -> None:
        """Open the internal preview for one attachment as the interface holds
        it (name, size, snapshot preview URL, snapshotted text). It reads no
        path: what is shown is exactly what was already taken at attach time."""
        if isinstance(file, dict) and file.get("kind") in ("image", "text"):
            self.previewRequested.emit({key: file[key] for key in ("id", "kind", "name", "bytes", "media_type", "preview", "text", "truncated")
                                        if key in file})

    @Property("QVariantList", notify=groupsChanged)
    def contextGroups(self) -> list:
        """The draft's groups in order: id, the display letter, the reference and
        the note. Attachments are read per group (``groupAttachments``)."""
        return [{"id": group["id"], "label": group_label(index), "reference": dict(group["reference"]),
                 "note": group["note"]} for index, group in enumerate(self._groups)]

    @Property("QVariantList", notify=attachmentsChanged)
    def globalAttachments(self) -> list:
        """Files attached to the message as a whole, not to any group."""
        return [self._shown(item) for item in self._attachments]

    @Property(int, notify=attachmentsChanged)
    def attachmentRevision(self) -> int:
        """Moves whenever any attachment is added or removed (see ``groupAttachments``)."""
        return self._attach_revision

    @Slot(str, result="QVariantList")
    def groupAttachments(self, group_id: str) -> list:
        """The files owned by ONE group, in the order they were attached."""
        group = self._group(group_id)
        return [self._shown(item) for item in group["attachments"]] if group else []

    @Property(str, notify=modeChanged)
    def responseMode(self) -> str:
        """"together" - reason across groups - or "separate" - one isolated answer per group."""
        return self._response_mode

    @Slot(str)
    def setResponseMode(self, mode: str) -> None:
        if mode in RESPONSE_MODES and mode != self._response_mode:
            self._response_mode = mode
            self.modeChanged.emit()

    @Property("QVariantMap", notify=prefsChanged)
    def attachmentSupport(self) -> dict:
        """What the CHOSEN provider really accepts as an explicit attachment.
        A kind it does not accept is refused here - never sent to another provider."""
        return dict(ATTACHMENT_SUPPORT.get(self.chatProvider, {"image": False, "text": False}))

    def _group(self, group_id: str) -> dict | None:
        return next((group for group in self._groups if group["id"] == str(group_id)), None)

    def _entry(self, entry_id: str) -> dict | None:
        """The conversational feed entry with this id: what the person wrote and
        General Chat's replies only - a clarification question is one of them.
        A notice, an approval, an error, external data or a task result is never
        quotable."""
        for entry in self._feed:
            if entry.get("id") == entry_id and isinstance(entry.get("msg"), str):
                return entry if entry["kind"] in ("user", "clarify") or entry.get("chat") else None
        return None

    def _item_count(self) -> int:
        return len(self._groups) + len(self._attachments) + sum(len(g["attachments"]) for g in self._groups)

    def _room(self) -> bool:
        if self._item_count() < MAX_ITEMS:
            return True
        self._tell("notice", QT_TRANSLATE_NOOP("Bridge", "A message can carry at most %1 context items."), MAX_ITEMS)
        return False

    def _new_id(self, prefix: str) -> str:
        self._item_seq += 1
        return f"{prefix}{self._item_seq}"

    def _start_group(self, entry: dict, selection: str, text: str) -> bool:
        primary = reference(self._new_id("ref-"), source_role=entry["kind"], source_id=entry["id"],
                            selection=selection, text=redact_text(text), source_time=str(int(entry.get("ts") or 0)))
        if any(same_reference(primary, group["reference"]) for group in self._groups):
            return True                     # the same quote twice stays one group
        if not self._room():
            return False
        self._groups.append(group(self._new_id("grp-"), primary))
        self.groupsChanged.emit()
        return True

    @Slot(str, result=bool)
    def quoteMessage(self, entry_id: str) -> bool:
        """Quote a WHOLE conversational message: a new group on the draft.
        Nothing is pasted into the text the person is writing, nothing is sent."""
        entry = self._entry(str(entry_id))
        return entry is not None and self._start_group(entry, "full", entry["msg"])

    @Slot(str, str, result=bool)
    def quoteSelection(self, entry_id: str, text: str) -> bool:
        """Quote exactly the selected part of one conversational message: a new group.

        A rendered document separates its blocks with U+2029/U+2028: those are the
        renderer's marks for a line break, never content. They become newlines
        again here, so a quote carries the text and not a document's shape.
        """
        entry = self._entry(str(entry_id))
        selected = str(text).replace(" ", "\n").replace(" ", "\n").strip()
        return entry is not None and bool(selected) and self._start_group(entry, "partial", selected)

    @Slot(str, str)
    def setGroupNote(self, group_id: str, note: str) -> None:
        """The person's instruction for ONE group, written straight to it.

        No list change is announced: republishing would rebuild the cards and
        destroy the editor being typed in. The stored value is always the newest,
        so a turn sent while the editor still has focus carries exactly what was typed.
        """
        found = self._group(group_id)
        if found is not None:
            found["note"] = redact_text(str(note)[:NOTE_MAX_CHARS])
            self._draft_timer.start()

    @Slot(str)
    def removeGroup(self, group_id: str) -> None:
        """Remove one group: its reference, its note and its attachments. Every
        other group and every global attachment is untouched."""
        found = self._group(group_id)
        if found is None:
            return
        self._groups.remove(found)
        self.groupsChanged.emit()
        if found["attachments"]:
            self._attach_revision += 1
            self.attachmentsChanged.emit()

    @Slot(str)
    def removeAttachment(self, attachment_id: str) -> None:
        """Remove exactly one attachment, wherever it is owned."""
        for owner in [self._attachments] + [g["attachments"] for g in self._groups]:
            for item in owner:
                if item["id"] == str(attachment_id):
                    owner.remove(item)
                    self._attach_revision += 1
                    self.attachmentsChanged.emit()
                    return

    def _unsupported_attachment(self, kind: str, name: str) -> None:
        self._tell("notice", QT_TRANSLATE_NOOP(
            "Bridge", "The selected provider cannot take %1 files, so %2 was not attached. "
                      "JARVIS never sends it to another provider instead."), kind, name)

    def _owner(self, group_id: str) -> list | None:
        """Where a new attachment goes: the named group, or the message itself."""
        if not group_id:
            return self._attachments
        found = self._group(group_id)
        return found["attachments"] if found is not None else None

    def _store_attachment(self, item: dict, owner: list) -> bool:
        for key in ("text", "name"):
            if isinstance(item.get(key), str):
                item[key] = redact_text(item[key])
        if not self._room():
            return False
        owner.append(item)
        self._attach_revision += 1
        self.attachmentsChanged.emit()
        return True

    def _attach_path(self, url: str, group_id: str) -> bool:
        owner = self._owner(group_id)
        if owner is None:
            return False
        local = QUrl(str(url)).toLocalFile() if str(url).startswith("file:") else str(url)
        kind = file_kind(safe_name(local))
        if kind and not self.attachmentSupport.get(kind, False):
            self._unsupported_attachment(kind, safe_name(local))
            return False
        try:
            item = attach_file(local, self._snapshots, self._new_id("att-"))
        except AttachmentRefused as refused:
            self._tell("notice", refused.source, *refused.args_tuple)
            return False
        return self._store_attachment(item, owner)

    @Slot(str, result=bool)
    def attachFile(self, url: str) -> bool:
        """Attach exactly the ONE file the person picked, to the message itself.
        A folder, a device, an unreadable path or a format this build cannot read
        is refused and said so."""
        return self._attach_path(url, "")

    @Slot(str, str, result=bool)
    def attachFileToGroup(self, group_id: str, url: str) -> bool:
        """The same, owned by ONE group - and only that group."""
        return self._attach_path(url, group_id)

    def clipboard_data(self) -> Any:
        """What the system clipboard currently holds (the seam tests replace)."""
        from PySide6.QtGui import QGuiApplication
        clipboard = QGuiApplication.clipboard()
        return clipboard.mimeData() if clipboard is not None else None

    @Slot(result=bool)
    def clipboardAttachable(self) -> bool:
        """Whether the clipboard holds something paste would ATTACH (an image or
        local files) rather than paste as text. Reads only; changes nothing."""
        data = self.clipboard_data()
        if data is None:
            return False
        return bool(data.hasImage() or any(url.isLocalFile() for url in (data.urls() if data.hasUrls() else [])))

    def _paste_attachments(self, group_id: str) -> int:
        """-1 when the clipboard is not an attachment payload at all (so it is
        ordinary text); otherwise how many items were attached - 0 included,
        when every file was refused with its reason. A refused file is never
        pasted as its path instead."""
        owner = self._owner(group_id)
        data = self.clipboard_data()
        if data is None:
            return -1
        files = [url for url in (data.urls() if data.hasUrls() else []) if url.isLocalFile()]
        if not data.hasImage() and not files:
            return -1
        if owner is None:
            return 0
        if data.hasImage():
            return int(self._attach_clipboard_image(data.imageData(), owner))
        return sum(1 for url in files if self._attach_path(url.toString(), group_id))

    @Slot(result=int)
    def attachClipboard(self) -> int:
        """THE paste decision for the main composer, used by Ctrl+V and by the
        context menu alike. An image or local files become attachments of the
        message and the count is returned (0 when all were refused, each with
        its reason); -1 means the clipboard holds no attachment, and the editor
        pastes it as ordinary text."""
        return self._paste_attachments("")

    @Slot(str, result=int)
    def attachClipboardToGroup(self, group_id: str) -> int:
        """The same decision, with the result owned by ONE group. Only called
        from that group's own explicit control - JARVIS never guesses an owner."""
        return self._paste_attachments(str(group_id))

    @Slot(result=str)
    def clipboardText(self) -> str:
        """The clipboard's plain text, so the composer can measure a paste (LP3). Reads only."""
        data = self.clipboard_data()
        return str(data.text()) if data is not None and data.hasText() else ""

    @Slot(str, result=str)
    def attachPastedText(self, text: str) -> str:
        """LP1/LP4: a long paste, kept as a text attachment of the message itself
        and marked ``pasted`` - context beside the typed request, never part of it
        and never authority. Its id, or "" when nothing was attached (the provider
        takes no text files, the draft is full, it is too large): the composer then
        pastes it as ordinary text, exactly as before."""
        if not self.attachmentSupport.get("text", False):
            return ""
        try:
            item = attach_bytes(str(text).encode("utf-8"), PASTED_TEXT_NAME, self._snapshots, self._new_id("att-"))
        except AttachmentRefused:
            return ""
        item["pasted"] = True
        return item["id"] if self._store_attachment(item, self._attachments) else ""

    def _attach_clipboard_image(self, image: Any, owner: list) -> bool:
        from PySide6.QtCore import QBuffer, QByteArray
        if image is None or not hasattr(image, "save"):
            return False
        if not self.attachmentSupport.get("image", False):
            self._unsupported_attachment("image", CLIPBOARD_IMAGE_NAME)
            return False
        store = QByteArray()          # the buffer writes into THIS: it must outlive the buffer
        buffer = QBuffer(store)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        saved = bool(image.save(buffer, "PNG"))
        buffer.close()
        if not saved:
            return False
        try:
            item = attach_bytes(bytes(store.data()), CLIPBOARD_IMAGE_NAME, self._snapshots, self._new_id("att-"))
        except AttachmentRefused as refused:
            self._tell("notice", refused.source, *refused.args_tuple)
            return False
        return self._store_attachment(item, owner)

    @Slot()
    def clearContext(self) -> None:
        """Empty the draft's groups and attachments. The answer mode returns to
        its default with them: a mode chosen for one draft never governs the next."""
        had_files = bool(self._attachments) or any(group["attachments"] for group in self._groups)
        if self._groups:
            self._groups = []
            self.groupsChanged.emit()
        if had_files:
            self._attachments = []
            self._attach_revision += 1
            self.attachmentsChanged.emit()
        if self._response_mode != DEFAULT_RESPONSE_MODE:
            self._response_mode = DEFAULT_RESPONSE_MODE
            self.modeChanged.emit()

    def _take_context(self) -> tuple[tuple, tuple, str]:
        """The draft's groups and attachments for the turn being sent, and the mode."""
        groups = tuple({**group, "reference": dict(group["reference"]),
                        "attachments": [dict(item) for item in group["attachments"]]} for group in self._groups)
        attachments = tuple(dict(item) for item in self._attachments)
        return groups, attachments, (self._response_mode if groups else DEFAULT_RESPONSE_MODE)

    # -- DFDRAFT (Salih 2026-10-12): every conversation keeps its own draft --------
    #
    # "Her sohbetin kendi hafizasinda kalmali taslaklar." The open screen's draft
    # is live: its text as the composer reports it (setDraftText) and the
    # context above. A switch parks it under its key - the conversation's id, or
    # NEW_CHAT_DRAFT for the New chat screen, whichever owner its picker shows -
    # and brings back the opened one's. The drafts file (UiPreferences) keeps
    # them over a restart: text, quotes, notes and TEXT files; an image's
    # snapshot belongs to this run, so it is not kept past it. A draft is local
    # data only: nothing here sends, logs or uploads anything.

    draftRestored = Signal()

    @Property(str, notify=draftRestored)
    def draftText(self) -> str:
        """The open screen's draft text as last restored (a switch, a start, a send)."""
        return self._draft_text

    @Slot(str)
    def setDraftText(self, text: str) -> None:
        """What the composer holds now; kept for the open screen only."""
        text = str(text)
        if text != self._draft_text:
            self._draft_text = text
            self._draft_timer.start()

    def _draft_key_of(self, conversation: PersistedConversation) -> str:
        """Its id once it has a turn or a queued message (DFQUEUE), else the New chat key."""
        exists = conversation.state.turns or conversation.id in self._queued_ids()
        return conversation.id if conversation.id and exists else NEW_CHAT_DRAFT

    def _live_draft(self) -> dict[str, Any] | None:
        if not (self._draft_text or self._groups or self._attachments):
            return None
        return {"text": self._draft_text, "groups": self._groups, "attachments": self._attachments,
                "mode": self._response_mode, "at": int(time.time())}

    def _swap_draft(self, opened: PersistedConversation) -> None:
        """Park the open screen's draft, drop the snapshot files nothing holds any
        more (the run in flight's stay, R1), then bring back ``opened``'s own."""
        live = self._live_draft()
        if live is not None:
            self._drafts[self._draft_key] = live
        self._groups, self._attachments, self._draft_text = [], [], ""
        self._response_mode = DEFAULT_RESPONSE_MODE
        self._discard_snapshots()
        self._draft_key = self._draft_key_of(opened)
        self._restore_draft()
        self._save_drafts()
        self.queueChanged.emit()            # DFQUEUE: the opened one's queued messages

    def _restore_draft(self) -> None:
        draft = (None if self._draft_held else self._drafts.pop(self._draft_key, None)) or {}
        self._groups, self._attachments = draft.get("groups", []), draft.get("attachments", [])
        self._response_mode = draft.get("mode", DEFAULT_RESPONSE_MODE)
        self._draft_text = draft.get("text", "")
        self._attach_revision += 1
        for signal in (self.groupsChanged, self.attachmentsChanged, self.modeChanged, self.draftRestored):
            signal.emit()
        self._draft_timer.stop()        # a restore is not an edit

    def _draft_sent(self, text: str) -> None:
        """An accepted send spends the draft it came from (its context went with
        it). Typed text it did not carry stays, parked under the New chat key it
        was typed on when the send gave the screen its first message."""
        if text == self._draft_text:
            self._draft_text = ""
        key = self._draft_key_of(self._conversation_store)
        if key != self._draft_key:
            live = self._live_draft()
            if live is not None:
                self._drafts[self._draft_key] = live
            self._draft_key, self._groups, self._attachments, self._draft_text = key, [], [], ""
            self.draftRestored.emit()
        self._save_drafts()

    @Slot(bool)
    def holdDraft(self, hold: bool) -> None:
        """A composer that is not the open conversation's (a project page's)
        sends: while held, a switch parks the open screen's draft but brings
        none back, so no parked draft - the New chat screen's above all - goes
        out with that message. Released: the open screen's own comes back."""
        self._draft_held = bool(hold)
        if not hold and self._live_draft() is None and self._draft_key in self._drafts:
            self._restore_draft()
            self._save_drafts()

    def _save_drafts(self) -> None:
        if self._quitting:              # the quit saved them before it emptied the open one
            return
        self._draft_timer.stop()
        live = self._live_draft()
        try:
            self.prefs.save_drafts({**self._drafts, **({self._draft_key: live} if live is not None else {})})
        except Exception:  # noqa: BLE001 - a draft that cannot be written never stops a switch or a quit
            pass

    def _load_drafts(self) -> None:
        """The stored drafts, with fresh ids; a conversation deleted meanwhile
        (by any path) leaves its draft behind. Then the open screen's own."""
        store = self._conversation_db()
        for key, stored in self.prefs.load_drafts().items():
            try:
                gone = key != NEW_CHAT_DRAFT and store is not None and store.load_conversation(key) is None
            except ValueError:              # not a conversation id at all
                gone = True
            except Exception:  # noqa: BLE001 - unreadable is not deleted: the draft stays
                gone = False
            if not gone:
                self._drafts[key] = self._revived(stored)
        self._restore_draft()

    def _revived(self, stored: dict[str, Any]) -> dict[str, Any]:
        """A stored draft as live context: this run's ids, the same caps as when attached."""
        count = 0

        def files(raw: list) -> list:
            nonlocal count
            kept = []
            for item in raw:
                if count < MAX_ITEMS:
                    count += 1
                    kept.append({**item, "id": self._new_id("att-"), "text": item["text"][:TEXT_ATTACHMENT_MAX_CHARS]})
            return kept
        groups = []
        for raw in stored["groups"]:
            if count >= MAX_ITEMS:
                break
            count += 1
            ref = raw["reference"]          # its feed entry id was the last run's: it names nothing now
            primary = reference(self._new_id("ref-"), source_role=ref["source_role"], source_id="",
                                selection=ref["selection"], text=ref["text"], source_time=ref["source_time"])
            groups.append({**group(self._new_id("grp-"), primary), "note": raw["note"][:NOTE_MAX_CHARS],
                           "attachments": files(raw["attachments"])})
        return {"text": stored["text"], "groups": groups, "attachments": files(stored["attachments"]),
                "mode": stored["mode"] if stored["mode"] in RESPONSE_MODES else DEFAULT_RESPONSE_MODE, "at": stored["at"]}

    # -- DFQUEUE (Salih 2026-10-12 "Siraya alsin"): a message sent while JARVIS works elsewhere waits --
    #
    # One run at a time stays the rule: submitText never starts a second one. A send
    # from any conversation but the running one (the New chat screen too) while a run
    # is active - or while older messages still wait - joins the queue instead: shown
    # in its conversation as "Queued", its draft spent, the New chat screen's
    # conversation listed from then on. When nothing runs and no approval waits
    # anywhere, the oldest starts exactly as if sent then, with the provider, model
    # and effort chosen when it was queued. Taken back, Freeze or quit: it returns
    # to its conversation's draft and nothing starts. Bridge memory only.

    queueChanged = Signal()

    @Property(bool, notify=queueChanged)
    def queueing(self) -> bool:
        """A send from the open conversation's composer joins the queue now."""
        return (self._busy or bool(self._queue)) and not self.openRunning and not self.navigationLocked

    @Property("QVariantList", notify=queueChanged)
    def queuedHere(self) -> list:
        """The open conversation's queued messages: their place in the one queue, oldest first."""
        cid = self._conversation_store.id
        return [{"id": entry["id"], "text": redact_text(entry["text"]), "position": index + 1, "total": len(self._queue)}
                for index, entry in enumerate(self._queue) if cid and entry["cid"] == cid]

    @Property("QVariantList", notify=queueChanged)
    def queuedIds(self) -> list:
        """The conversations that hold a queued message (the sidebar's marker)."""
        return sorted(self._queued_ids())

    def _queued_ids(self) -> set[str]:
        return {entry["cid"] for entry in self._queue}

    def _approval_waiting(self) -> bool:
        """An approval waits somewhere: its run still counts as active, so the queue waits."""
        return any(slots["ticket"] is not None or bool(slots["approval"])
                   for slots in (self._live_slots(), *self._parked.values()))

    @Slot(str, result=bool)
    def queueText(self, text: str) -> bool:
        """Queue one message from the open conversation. True when queued (the composer
        clears); False when refused - said in the conversation - and the draft stays."""
        text = str(text)
        cid = self._conversation_store.id
        if not text.strip() or not self.queueing or self._ticket is not None or self.conversationReadOnly or not cid:
            return False
        if not self.executionEnabled or self._execution_request:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "JARVIS is frozen, so the message was not queued; it stays in the draft."))
            return False
        if _waiting(self._live_slots()):
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "This conversation is waiting for your answer, so the message was not "
                                                             "queued; send it when JARVIS is free. It stays in the draft."))
            return False
        if len(self._queue) >= QUEUE_LIMIT:
            self._tell("system", QT_TRANSLATE_NOOP("Bridge", "The queue is full (%1 messages), so this message was not queued; "
                                                             "it stays in the draft."), QUEUE_LIMIT)
            return False
        groups, attachments, mode = self._take_context()
        claude = (self._config.get("claude_model") or None, self._config.get("claude_effort") or None)
        self._queue.append({"id": self._new_id("q-"), "cid": cid, "text": text, "groups": groups,
                            "attachments": attachments, "mode": mode, "route": self._provider_route(), "claude": claude,
                            "queued_at": self._clock()})
        self.clearContext()
        self._draft_sent(text)              # the New chat screen's conversation exists from now on
        self._refresh_conversations()
        self.queueChanged.emit()
        self.inputAccepted.emit()
        return True

    @Slot(str, result=bool)
    def unqueue(self, entry_id: str) -> bool:
        """Take one queued message back: it returns to its conversation's draft; nothing runs."""
        entry = next((e for e in self._queue if e["id"] == str(entry_id)), None)
        if entry is None:
            return False
        self._queue.remove(entry)
        self._to_draft(entry)
        self._refresh_conversations()
        self.queueChanged.emit()
        return True

    def _return_queue(self) -> int:
        """Every queued message back to its conversation's draft (Freeze, quit, frozen): none starts."""
        returned, self._queue = self._queue, []
        for entry in returned:
            self._to_draft(entry)
        if returned:
            self._refresh_conversations()
            self.queueChanged.emit()
        return len(returned)

    def _to_draft(self, entry: dict[str, Any]) -> None:
        """``entry`` (already out of the queue) goes in front of its conversation's draft."""
        store = self._conversation_db()
        here = entry["cid"] == self._conversation_store.id
        conversation = self._conversation_store if here else self._load_conversation(store, entry["cid"]) if store else None
        key = self._draft_key_of(conversation) if conversation is not None else entry["cid"]
        if here or key == self._draft_key:  # the open screen's own key (a New chat's): into the live draft
            base = self._live_draft()
            if key != self._draft_key:      # a New chat whose only message came back: the New chat screen again
                parked, self._draft_key = self._drafts.pop(key, None), key
                base = _draft_merged(base, parked) if parked else base
            merged = _draft_merged(base, entry)
            self._groups, self._attachments = merged["groups"], merged["attachments"]
            self._draft_text, self._response_mode = merged["text"], merged["mode"]
            self._attach_revision += 1
            for signal in (self.groupsChanged, self.attachmentsChanged, self.modeChanged, self.draftRestored):
                signal.emit()
        else:
            self._drafts[key] = _draft_merged(self._drafts.get(key), entry)
        self._save_drafts()

    def _drain_queue(self) -> None:
        """Start the oldest queued message once nothing runs and no approval waits; frozen: none
        ever starts, they all return to their drafts."""
        if not self._queue or self._busy or self._quitting:
            return
        if not self.executionEnabled or self._execution_request:
            if self._return_queue():
                self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS is frozen: queued messages went back to their drafts"), ()), "system")
            return
        if self._approval_waiting():
            return
        while self._queue and not self._busy and not self._approval_waiting():
            self._start_queued(self._queue.pop(0))
        self._refresh_conversations()
        self.queueChanged.emit()

    def _start_queued(self, entry: dict[str, Any]) -> None:
        store = self._conversation_db()
        cid = entry["cid"]
        here = cid == self._conversation_store.id
        try:
            deleted = store is not None and store.load_conversation(cid) is None
        except Exception:  # noqa: BLE001 - unreadable is not deleted: it goes back to its draft below
            deleted = False
        if deleted:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A queued message for a conversation that was deleted was dropped."), ()), "system")
            return
        origin = None if here else self._load_conversation(store, cid)
        slots = self._live_slots() if here else self._parked.get(cid) or _empty_slots()
        try:
            archived = store.load_conversation(cid).archived_at is not None
        except Exception:  # noqa: BLE001 - unreadable: fail closed, it is not started
            archived = True
        if (not here and origin is None) or archived or _waiting(slots):
            self._to_draft(entry)
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A queued message went back to its draft: its conversation is "
                                                              "archived, unreadable or waiting for your answer."), ()), "system")
            return
        groups, attachments, text = entry["groups"], entry["attachments"], entry["text"]
        conversation = (self._conversation_store if here else origin).state     # before this turn: what came earlier
        self._say("user", text, owner=origin,
                  groups=[{**g, "attachments": [self._shown(a) for a in g["attachments"]]} for g in groups],
                  attachments=[self._shown(a) for a in attachments], refs=turn_refs(groups, attachments))
        session_id = (self._session_id if here else slots["session_id"]) or None
        request_id = str(uuid4())
        self._request_id = request_id
        self._start_text(text, session_id, request_id, conversation, groups, attachments, entry["mode"], entry["route"],
                         origin=origin, claude=entry["claude"],
                         queued_ms=max(0, round((self._clock() - entry["queued_at"]) * 1000)))

    # -- conversation actions ----------------------------------------------------

    @Slot(str)
    def copyText(self, text: str) -> None:
        """Copy exactly the visible plain text of one conversational message."""
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(str(text))

    @Slot(str, bool)
    def refreshMemory(self, project_id: str, include_inactive: bool) -> None:
        self._projects = self.backend.projects()
        self._project_records = self.backend.project_records()
        self.projectsChanged.emit()
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

    # -- V2.2 S4: skills (pass-through) ------------------------------------------
    #
    # The page's rows and every user action run the app's skill API on the
    # worker. The bridge decides nothing: the flag (S4), the filter (S7), the
    # scope (S8) and every refusal are the app's. Each action is one user
    # click, sent back with the project its row was read in; nothing here
    # approves, trusts or remembers anything on its own (S1/S5). A refusal
    # lands in the feed, then the rows are read again.

    skillsChanged = Signal()

    @Property("QVariantList", notify=skillsChanged)
    def skills(self) -> list:
        return list(self._skills)

    @Property("QVariantList", notify=skillsChanged)
    def pendingDrafts(self) -> list:
        return list(self._pending_drafts)

    @Property(bool, notify=skillsChanged)
    def skillsRead(self) -> bool:
        """True once a skills listing answered; before it the counts are unknown, not zero."""
        return self._skills_read

    @Property(bool, notify=stateChanged)
    def skillsEnabled(self) -> bool:
        return self._config.get("skills_enabled") is True and not self._config_refused()

    @Slot()
    def refreshSkills(self) -> None:
        records = self.backend.project_records()
        names = {str(p.get("id")): str(p.get("name") or "") for p in records}
        active = [str(p.get("id")) for p in records if p.get("active")]
        self._run("skills", lambda: self.backend.skills_overview(active),
                  lambda payload: self._set_skills(payload, names))
        self.refreshLessons()          # SKUIB b1/b7: the page's other two lists
        self.refreshSuppressions()

    @staticmethod
    def _skill_text(record: dict) -> dict:
        # Every field a save keeps reads on the card: a step's capability, inputs and check, an input's required.
        def step(s: dict) -> str:
            extra = [tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "uses %1"), code_word("capability", s["capability"]))
                     if s.get("capability") else "",
                     tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "inputs: %1"), ", ".join(map(str, s["inputs"])))
                     if s.get("inputs") else "",
                     tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "check: %1"), str(s["check"])) if s.get("check") else ""]
            extra = [part for part in extra if part]
            return str(s.get("action")) + (" (" + "; ".join(extra) + ")" if extra else "")

        def need(i: dict) -> str:
            return tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "%1 (required)") if i.get("required") is True
                      else QT_TRANSLATE_NOOP("Bridge", "%1 (optional)"), str(i.get("name")))

        return {"steps": [step(s) for s in record.get("steps") or []],
                "inputs": [need(i) + (": " + str(i["description"]) if i.get("description") else "")
                           for i in record.get("inputs") or []],
                "checks": [str(check) for check in record.get("checks") or []]}

    def _set_skills(self, payload: Any, projects: dict[str, str]) -> None:
        scopes = payload.get("scopes") if isinstance(payload, dict) else None
        self._skills_read = isinstance(scopes, list)
        if not isinstance(scopes, list):
            self._skill_refused(payload)
            scopes = []
        found: list[tuple[str, dict]] = []
        drafts: list[dict] = []
        refused: dict[str, dict] = {}     # one line per distinct refusal, not one per project
        for scope in scopes:
            pid = scope["project_id"] or ""
            for answer in [scope["list"], *scope["skills"]]:
                if answer.get("ok") is not True:
                    refused.setdefault(str(answer.get("error")), answer)
            found += [(pid, a["skill"]) for a in scope["skills"] if a.get("ok") is True]
            seen = {d["id"] for d in drafts}
            drafts += [{"id": d["draft_id"], "project_id": pid, "name": d["name"], "summary": d["summary"],
                        "scope": d["scope"]["kind"], "project_name": projects.get(d["scope"]["project_id"] or "", ""),
                        "reason": d["reason"], "origin": d["provenance"]["origin"],
                        "parent_version": d["provenance"]["parent_version"] or 0,
                        "target_skill_id": d.get("target_skill_id") or "",
                        "parent_id": d["provenance"].get("parent_skill_id") or "", **self._skill_text(d)}
                       for d in scope["drafts"].get("drafts", []) if d["draft_id"] not in seen]
        names = {s["skill_id"]: s["name"] for _, s in found}
        for d in drafts:   # SKUIB b8: the adapted draft's source, by name
            d["parent_name"] = names.get(d["parent_id"], "")
        self._skills_unreadable = max((int(s["list"].get("unreadable") or 0) for s in scopes), default=0)
        self._skills = [
            {"id": s["skill_id"], "project_id": pid, "name": s["name"], "scope": s["scope"]["kind"],
             "project_name": projects.get(s["scope"]["project_id"] or "", ""), "version": s["version"],
             "versions": list(s["versions"]), "status": s["status"], "uses": s["health"]["use_count"],
             "last_success": s["health"]["last_success_at"] or "", "failures": s["health"]["failure_count"],
             "origin": s["provenance"]["origin"], "parent_name": names.get(s["provenance"]["parent_skill_id"] or "", ""),
             "parent_version": s["provenance"]["parent_version"] or 0, **self._skill_text(s)}
            for pid, s in found
        ]
        self._pending_drafts = drafts
        for answer in refused.values():
            self._skill_refused(answer)
        self.skillsChanged.emit()

    def _skill_refused(self, answer: Any) -> None:
        """A refusal is said once in the feed; the flag being off is the page's own note, not an error."""
        if not isinstance(answer, dict) or answer.get("skills_disabled") is True:
            return
        error = answer.get("error")
        if not isinstance(error, dict):   # the app's own refusal words (its plain-dict answers)
            error = {"code": "invalid_task", "message": str(error or "refused")}
        for entry in error_entries(error):
            self._say(entry["kind"], entry["text"])

    def _skill_action(self, method: str, *args: Any) -> None:
        # Its own kind per click (like the memory actions): no answer is dropped behind a later one.
        self._run(f"skill-action:{uuid4().hex}", lambda: self.backend.skill_action(method, *args), self._skill_done)

    def _skill_done(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            self._skill_refused(payload)
        self.refreshSkills()

    @staticmethod
    def _scope_of(rows: list[dict], row_id: str) -> str | None:
        """The project a row was read in; an unknown id goes with none and the app refuses it."""
        return next((row["project_id"] or None for row in rows if row["id"] == row_id), None)

    @Slot(str, "QVariantMap")
    def skillEdit(self, skill_id: str, payload: Any) -> None:
        self._skill_action("skill_edit", str(skill_id), dict(payload or {}), self._scope_of(self._skills, str(skill_id)))

    @Slot(str, bool)
    def skillSetEnabled(self, skill_id: str, on: bool) -> None:
        self._skill_action("skill_set_enabled", str(skill_id), bool(on), self._scope_of(self._skills, str(skill_id)))

    @Slot(str)
    def skillDelete(self, skill_id: str) -> None:
        self._skill_action("skill_delete", str(skill_id), self._scope_of(self._skills, str(skill_id)))

    @Slot(str, int)
    def skillRollback(self, skill_id: str, version: int) -> None:
        self._skill_action("skill_rollback", str(skill_id), int(version), self._scope_of(self._skills, str(skill_id)))

    @Slot(str, str)
    def draftApprove(self, draft_id: str, scope: str) -> None:
        """The user's Save click on one draft card (S5); scope is the card's "same" or "global"."""
        self._skill_action("draft_approve", str(draft_id), str(scope) or None, None,
                           self._scope_of(self._pending_drafts, str(draft_id)))

    @Slot(str)
    def draftReject(self, draft_id: str) -> None:
        self._skill_action("draft_reject", str(draft_id), self._scope_of(self._pending_drafts, str(draft_id)))

    @Slot(str, str)
    def suppressTaskKind(self, task_kind: str, scope: str) -> None:
        self._skill_action("suppress", str(task_kind) or None, str(scope))

    @Slot(bool)
    def enableSkills(self, on: bool) -> None:
        def done(payload: Any) -> None:
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                self._skill_refused(payload)
            self.refreshConfig()
            self.refreshSkills()
        self._run("skills-enable", lambda: self.backend.skills_set_enabled(bool(on)), done)

    @Slot(str, str)
    def declineSkill(self, session_id: str, task_id: str) -> None:
        """D6: the announced skill's 'Kullanma' - the task runs on without it (Core may refuse)."""
        if self._busy or not self._open_owner_of(str(session_id)):
            return
        self._session_id = str(session_id)
        self._run("task", lambda: self.backend.skill_decline(str(session_id), str(task_id)),
                  self._delivered(self._apply_run), executing=True)

    # -- SKUIB: run, lessons and suppressions from the Skills page (b4, b1, b7) --
    #
    # Pass-through like the rest: "Run" is a request to Core (announce, inputs,
    # every step through policy and approval), never an approval. Lesson and
    # suppression refusals are the app's words, shown verbatim on the page.

    skillNoticeChanged = Signal()

    @Property("QVariantList", notify=skillsChanged)
    def lessons(self) -> list:
        return list(self._lessons)

    @Property("QVariantMap", notify=skillsChanged)
    def lessonDetail(self) -> dict:
        return dict(self._lesson_detail)

    @Property("QVariantList", notify=skillsChanged)
    def suppressions(self) -> list:
        return list(self._suppressions)

    @Property(int, notify=skillsChanged)
    def skillsUnreadable(self) -> int:
        return self._skills_unreadable

    @Property(str, notify=skillNoticeChanged)
    def skillNotice(self) -> str:
        return self._skill_notice

    def _set_skill_notice(self, answer: Any) -> None:
        """The answer's own error, verbatim (also when the flag is off); "" on success."""
        ok = isinstance(answer, dict) and answer.get("ok") is True
        error = answer.get("error") if isinstance(answer, dict) else answer
        if isinstance(error, dict):
            error = error.get("message")
        self._skill_notice = "" if ok else str(error or "refused")
        self.skillNoticeChanged.emit()

    @Slot(str)
    def runSkill(self, skill_id: str) -> None:
        if self._busy or self._ticket is not None:
            return
        self._task = {}
        project_id = self._scope_of(self._skills, str(skill_id))
        self._run("task", lambda: self.backend.run_skill(str(skill_id), project_id),
                  self._delivered(self._apply_run), executing=True)

    @Slot()
    def refreshLessons(self) -> None:
        scopes = (None, *[str(p.get("id")) for p in self.backend.project_records() if p.get("active")])

        def fetch() -> list[dict]:
            return [{**self.backend.skill_action("list_lessons", pid), "project_id": pid or ""} for pid in scopes]

        def done(answers: Any) -> None:
            rows: dict[str, dict] = {}
            for answer in answers if isinstance(answers, list) else []:
                for item in answer.get("lessons", []) if answer.get("ok") is True else []:
                    rows.setdefault(str(item["experience_id"]), {**item, "project_id": answer["project_id"]})
            self._lessons = list(rows.values())
            self.skillsChanged.emit()
        self._run("skill-lessons", fetch, done)

    @Slot()
    def refreshSuppressions(self) -> None:
        def done(answer: Any) -> None:
            ok = isinstance(answer, dict) and answer.get("ok") is True
            self._suppressions = list(answer.get("suppressions") or []) if ok else []
            self.skillsChanged.emit()
        self._run("skill-suppressions", lambda: self.backend.skill_action("suppressions", None), done)

    def _lesson_call(self, method: str, experience_id: str, then: Callable[[Any], None]) -> None:
        pid = self._scope_of([{"id": r["experience_id"], "project_id": r["project_id"]} for r in self._lessons],
                             str(experience_id))
        self._run(f"skill-action:{uuid4().hex}",
                  lambda: self.backend.skill_action(method, str(experience_id), pid), then)

    @Slot(str)
    def viewLesson(self, experience_id: str) -> None:
        def done(answer: Any) -> None:
            self._set_skill_notice(answer)
            ok = isinstance(answer, dict) and answer.get("ok") is True
            self._lesson_detail = dict(answer.get("lesson") or {}) if ok else {}
            self.skillsChanged.emit()
        self._lesson_call("lesson", experience_id, done)

    def _lesson_changed(self, answer: Any) -> None:
        self._set_skill_notice(answer)
        self._lesson_detail = {}
        self.refreshLessons()

    @Slot(str)
    def disableLesson(self, experience_id: str) -> None:
        self._lesson_call("disable_lesson", experience_id, self._lesson_changed)

    @Slot(str)
    def deleteLesson(self, experience_id: str) -> None:
        self._lesson_call("delete_lesson", experience_id, self._lesson_changed)

    @Slot(str, str)
    def unsuppress(self, key: str, scope: str) -> None:
        """b7 'Geri al': a task row by its key, the global row by its scope alone."""
        def done(answer: Any) -> None:
            self._set_skill_notice(answer)
            self.refreshSuppressions()
        task_kind = None if scope == "global" else str(key)
        self._run(f"skill-action:{uuid4().hex}",
                  lambda: self.backend.skill_action("unsuppress", task_kind, str(scope)), done)

    # -- V2.1a M4: memory records, approvals and 'Hatırla' (pass-through) -----
    #
    # Each slot runs the app's method on the worker and delivers its answer
    # whole, with the request's ids. The bridge decides nothing: ownership,
    # status moves and the remember filter are the app's. A refusal is never
    # swallowed: the answer keeps "ok": False with the rendered reason in
    # "error", and the same words land in the feed.

    memoryRecordsDone = Signal("QVariantMap")
    memoryRecordActionDone = Signal("QVariantMap")

    @Slot(str, "QVariantMap")
    def memoryRecords(self, project_id: str, filters: Any) -> None:
        request = {"project_id": str(project_id)}
        self._run("memory-records", lambda: self.backend.memory_records(request["project_id"], filters),
                  lambda payload: self._memory_answer(self.memoryRecordsDone, request, payload))

    @Slot(str, str)
    def approveMemoryRecord(self, project_id: str, record_id: str) -> None:
        self._record_action("approve", self.backend.approve_memory_record, project_id, record_id)

    @Slot(str, str)
    def rejectMemoryRecord(self, project_id: str, record_id: str) -> None:
        self._record_action("reject", self.backend.reject_memory_record, project_id, record_id)

    @Slot(str, str)
    def undoMemoryRecord(self, project_id: str, record_id: str) -> None:
        self._record_action("undo", self.backend.undo_memory_record, project_id, record_id)

    @Slot(str, str)
    def rememberMessage(self, conversation_id: str, message_id: str) -> None:
        request = {"action": "remember", "conversation_id": str(conversation_id), "message_id": str(message_id)}
        self._run(f"memory-action:{uuid4().hex}",
                  lambda: self.backend.remember_message(request["conversation_id"], request["message_id"]),
                  lambda payload: self._memory_answer(self.memoryRecordActionDone, request, payload))

    def _record_action(self, action: str, call: Callable[[str, str], dict], project_id: str, record_id: str) -> None:
        request = {"action": action, "project_id": str(project_id), "record_id": str(record_id)}
        # Every click's answer arrives (an approve that landed is never hidden behind a later refusal),
        # so each click is its own kind. ponytail: one tiny _sequence entry per click, prune if it ever matters.
        self._run(f"memory-action:{uuid4().hex}", lambda: call(request["project_id"], request["record_id"]),
                  lambda payload: self._memory_answer(self.memoryRecordActionDone, request, payload))

    def _memory_answer(self, signal: Any, request: dict, payload: Any) -> None:
        answer = {**(payload if isinstance(payload, dict) else {}), **request}
        answer["records"] = [{**item, "text": redact_text(str(item.get("text")))}
                             for item in rows(answer, "records", "record_id")]
        if isinstance(answer.get("record"), dict):
            answer["record"] = {**answer["record"], "text": redact_text(str(answer["record"].get("text")))}
        if answer.get("ok") is not True:
            error = answer.get("error")
            if not isinstance(error, dict):   # the app's own refusal words (its plain-dict answers)
                error = {"code": "invalid_task", "message": str(error or answer.get("refused") or "refused")}
            entries = error_entries(error)
            for entry in entries:
                self._say(entry["kind"], entry["text"])
            answer.update(ok=False, error=render(self._redacted(entries[0]["text"])))
        signal.emit(answer)

    # -- SECQMLUI: the approval batch, Undo and the two-step purge (pass-through)
    #
    # The batch is the app's memory_approval_batch, whole (text redacted for
    # display); its cards act through the record slots above (approve, reject,
    # undo), so Core alone moves a status (I14). Purge: requestMemoryPurge
    # answers the app's set, still_held_by and its Core token; the dialog's
    # confirm click hands that token back to confirmMemoryPurge untouched.

    _memoryBatchArrived = Signal(str, "QVariantMap")
    memoryBatchChanged = Signal()
    memoryPurgeRequested = Signal("QVariantMap")
    memoryPurgeDone = Signal("QVariantMap")

    @Property("QVariantMap", notify=memoryBatchChanged)
    def memoryBatch(self) -> dict:
        return dict(self._memory_batch)

    @Slot(str)
    def refreshMemoryBatch(self, conversation_id: str) -> None:
        conversation_id = str(conversation_id)
        self._run("memory-batch", lambda: self.backend.memory_approval_batch(conversation_id),
                  lambda payload: self._set_memory_batch(conversation_id, payload))

    def _set_memory_batch(self, conversation_id: str, payload: Any) -> None:
        if not (isinstance(payload, dict) and payload.get("ok") is True):
            return   # a batch that could not be read changes nothing; the records stay on the page
        self._memory_batch = {**payload, "conversation_id": str(conversation_id), **{
            key: [{**item, "text": redact_text(str(item.get("text") or ""))}
                  for item in payload.get(key) or [] if isinstance(item, dict)]
            for key in ("pending", "auto_saved")}}
        self.memoryBatchChanged.emit()

    @Slot("QVariantList")
    def requestMemoryPurge(self, record_ids: Any) -> None:
        request = {"requested": [str(record_id) for record_id in record_ids or []]}
        self._run(f"memory-purge:{uuid4().hex}", lambda: self.backend.request_memory_purge(request["requested"]),
                  lambda payload: self._memory_answer(self.memoryPurgeRequested, request, payload))

    @Slot(str)
    def confirmMemoryPurge(self, token: str) -> None:
        token = str(token)
        self._run(f"memory-purge:{uuid4().hex}", lambda: self.backend.confirm_memory_purge(token),
                  lambda payload: self._memory_answer(self.memoryPurgeDone, {}, payload))

    # -- SEC2: undo everything one conversation brought (pass-through) ---------
    #
    # The preview asks the app with confirm=False (nothing changes) and answers
    # its count; only the dialog's confirm click calls with confirm=True. Core
    # retracts through events and tombstones as USER (I23); purge stays apart.

    undoConversationPreviewed = Signal("QVariantMap")
    undoConversationDone = Signal("QVariantMap")

    @Slot(str)
    def undoConversationRecordsPreview(self, conversation_id: str) -> None:
        request = {"conversation_id": str(conversation_id)}
        self._run(f"memory-undo-conversation:{uuid4().hex}",
                  lambda: self.backend.undo_conversation_records(request["conversation_id"], confirm=False),
                  lambda payload: self._memory_answer(self.undoConversationPreviewed, request, payload))

    @Slot(str)
    def undoConversationRecords(self, conversation_id: str) -> None:
        request = {"conversation_id": str(conversation_id)}
        self._run(f"memory-undo-conversation:{uuid4().hex}",
                  lambda: self.backend.undo_conversation_records(request["conversation_id"], confirm=True),
                  lambda payload: self._memory_answer(self.undoConversationDone, request, payload))

    # -- V2.1a M4: context usage and the all-projects memory toggle (pass-through)
    #
    # Both are the app's answers for the open conversation, read on the worker.
    # The bridge computes no window, ratio or default: anything but a clean
    # answer for the open conversation reads as unknown usage and toggle off;
    # a refused toggle change keeps the last confirmed value and says why.

    contextUsageChanged = Signal()
    globalMemoryChanged = Signal()

    @Property("QVariantMap", notify=contextUsageChanged)
    def contextUsage(self) -> dict:
        return dict(self._context_usage)

    @Property(bool, notify=globalMemoryChanged)
    def globalMemoryEnabled(self) -> bool:
        return self._global_memory

    @Slot(bool)
    def setGlobalMemoryEnabled(self, enabled: bool) -> None:
        conversation_id, enabled = self._conversation_store.id or "", bool(enabled)
        # Its own kind per click: a refusal is never dropped behind a later read.
        self._run(f"global-memory-set:{uuid4().hex}",
                  lambda: self.backend.set_global_memory_toggle(conversation_id, enabled),
                  lambda payload: self._global_memory_answer(conversation_id, payload, says=True))

    # SEC2 (I21): the open conversation's memory-off switch, the app's value only.
    conversationMemoryOffChanged = Signal()

    @Property(bool, notify=conversationMemoryOffChanged)
    def conversationMemoryOff(self) -> bool:
        return self._conversation_memory_off

    @Slot(bool)
    def setConversationMemoryOff(self, off: bool) -> None:
        conversation_id, off = self._conversation_store.id or "", bool(off)
        self._run(f"memory-off-set:{uuid4().hex}",
                  lambda: self.backend.set_conversation_memory_off(conversation_id, off),
                  lambda payload: self._memory_off_answer(conversation_id, payload, says=True))

    def _memory_off_answer(self, conversation_id: str, payload: Any, says: bool = False) -> None:
        if says and not (isinstance(payload, dict) and payload.get("ok") is True):
            error = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error, dict):
                error = {"code": "invalid_task", "message": str(error or "refused")}
            if conversation_id == (self._conversation_store.id or ""):
                for entry in error_entries(error):
                    self._say(entry["kind"], entry["text"])
            self.conversationMemoryOffChanged.emit()   # a switch that moved on its own snaps back
            return
        if says and not self._current(conversation_id, payload):
            return
        off = self._current(conversation_id, payload) and payload.get("conversation_memory_off") is True
        if off != self._conversation_memory_off:
            self._conversation_memory_off = off
            self.conversationMemoryOffChanged.emit()

    def _refresh_conversation_memory(self) -> None:
        if self._shutting_down:
            return
        conversation_id = self._conversation_store.id or ""
        if conversation_id != self._memory_read_id:   # a switch: the last one's values never show meanwhile
            self._memory_read_id = conversation_id
            self._context_usage_answer("", None)
            self._global_memory_answer("", None)
            self._memory_off_answer("", None)
        self._refresh_context_usage()
        if not conversation_id:
            self._global_memory_answer("", None)
            self._memory_off_answer("", None)
            return
        self._run("global-memory", lambda: self.backend.global_memory_toggle(conversation_id),
                  lambda payload: self._global_memory_answer(conversation_id, payload))
        self._run("memory-off", lambda: self.backend.conversation_memory_off(conversation_id),
                  lambda payload: self._memory_off_answer(conversation_id, payload))

    def _refresh_context_usage(self) -> None:
        if self._shutting_down:
            return
        conversation_id = self._conversation_store.id or ""
        if not conversation_id:
            self._context_usage_answer("", None)
            return
        self._run("context-usage", lambda: self.backend.context_usage(conversation_id),
                  lambda payload: self._context_usage_answer(conversation_id, payload))

    def _current(self, conversation_id: str, payload: Any) -> bool:
        """A clean answer about the conversation that is still open."""
        return (bool(conversation_id) and conversation_id == (self._conversation_store.id or "")
                and isinstance(payload, dict) and payload.get("ok") is True
                and payload.get("conversation_id") == conversation_id)

    def _context_usage_answer(self, conversation_id: str, payload: Any) -> None:
        usage = dict(_UNKNOWN_USAGE)
        if self._current(conversation_id, payload) and isinstance(payload.get("max_tokens"), int):
            usage.update(used=payload.get("used_tokens"), max=payload["max_tokens"], ratio=payload.get("ratio"),
                         turnsUntilAutoCompact=payload.get("turns_until_autocompact"),
                         state=_USAGE_STATES.get(str(payload.get("label_key")), "unknown"))
        elif self._current(conversation_id, payload):
            usage["used"] = payload.get("used_tokens")
        if usage != self._context_usage:
            self._context_usage = usage
            self.contextUsageChanged.emit()

    def _global_memory_answer(self, conversation_id: str, payload: Any, says: bool = False) -> None:
        current = self._current(conversation_id, payload)
        if says and not (isinstance(payload, dict) and payload.get("ok") is True):
            error = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error, dict):   # the app's own refusal words (its plain-dict answers)
                error = {"code": "invalid_task", "message": str(error or "refused")}
            if conversation_id == (self._conversation_store.id or ""):   # never in another conversation's feed
                for entry in error_entries(error):
                    self._say(entry["kind"], entry["text"])
            self.globalMemoryChanged.emit()   # a switch that moved on its own snaps back
            return
        if says and not current:
            return   # it landed for a conversation no longer open; the switch re-read the open one
        enabled = current and payload.get("enabled") is True
        if enabled != self._global_memory:
            self._global_memory = enabled
            self.globalMemoryChanged.emit()

    # -- V2.1a M4: "Oturumu sıkıştır" and the Memory Doctor (pass-through) -----
    #
    # Same contract as the record slots: the app runs on the worker, its answer
    # arrives whole with the request's ids, a refusal (a reply still streaming,
    # a stale finding) keeps "ok": False and is said in the feed. A compaction's
    # answer is the conversation's new context usage, shown as such.

    compactConversationDone = Signal("QVariantMap")
    memoryDoctorDone = Signal("QVariantMap")
    memoryRepairDone = Signal("QVariantMap")

    @Slot(str)
    def compactConversation(self, conversation_id: str) -> None:
        request = {"conversation_id": str(conversation_id)}

        def done(payload: Any) -> None:
            self._memory_answer(self.compactConversationDone, request, payload)
            if isinstance(payload, dict) and payload.get("ok") is True:
                self._context_usage_answer(request["conversation_id"], payload)

        self._run(f"memory-compact:{uuid4().hex}",
                  lambda: self.backend.compact_conversation(request["conversation_id"]), done)

    @Slot(str)
    def memoryDoctor(self, project_id: str) -> None:
        """One project's report, or the whole store's for "" (all projects)."""
        request = {"project_id": str(project_id)}
        self._run("memory-doctor", lambda: self.backend.memory_doctor(request["project_id"] or None),
                  lambda payload: self._memory_answer(self.memoryDoctorDone, request, payload))

    @Slot(str)
    def applyMemoryRepair(self, finding_id: str) -> None:
        """The window's 'Onar' click, made only after its own confirmation, IS
        the confirmed command: the app re-diagnoses the whole store and repairs
        exactly that finding, or refuses (I2/I3)."""
        request = {"finding_id": str(finding_id)}
        self._run(f"memory-repair:{uuid4().hex}",
                  lambda: self.backend.apply_memory_repair(request["finding_id"], request["finding_id"]),
                  lambda payload: self._memory_answer(self.memoryRepairDone, request, payload))

    # -- V2.1b (IMPUI): history import (pass-through) --------------------------
    #
    # Same contract as the record slots. The app parses, plans, checks every
    # project and keeps each imported record PENDING; the bridge forwards only
    # the groups the person confirmed (the dialog omits the rest) and decides
    # nothing. Imported text is shown as redacted data, never executed.

    importPreviewDone = Signal("QVariantMap")
    importRunDone = Signal("QVariantMap")
    importCancelDone = Signal("QVariantMap")

    @Slot(str)
    def importHistoryPreview(self, path: str) -> None:
        local = QUrl(str(path)).toLocalFile() if str(path).startswith("file:") else str(path)

        def done(payload: Any) -> None:
            if isinstance(payload, dict) and isinstance(payload.get("groups"), list):
                payload = {**payload, "groups": [{**g, "title": redact_text(str(g.get("title") or ""))}
                                                 for g in payload["groups"] if isinstance(g, dict)]}
            self._memory_answer(self.importPreviewDone, {}, payload)

        self._run("import-preview", lambda: self.backend.import_history_preview(local), done)

    @Slot(str, "QVariantMap")
    def importHistoryRun(self, plan_id: str, scopes: Any) -> None:
        """``scopes`` is {group_id: project_id} for the confirmed groups only."""
        request = {"plan_id": str(plan_id)}
        confirmed = {str(group): project for group, project in dict(scopes or {}).items()}
        self._run("import-run", lambda: self.backend.import_history_run(request["plan_id"], confirmed),
                  lambda payload: self._memory_answer(self.importRunDone, request, payload))

    @Slot(str)
    def cancelImport(self, plan_id: str) -> None:
        """Answered here, not on the worker: the one worker is busy with the run it stops."""
        request = {"plan_id": str(plan_id)}
        self._memory_answer(self.importCancelDone, request, self.backend.import_history_cancel(request["plan_id"]))

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
        # Lifecycle is not conversation: the live feed notes it and the attention
        # strip / safety cards project the state itself. An UNCONFIRMED change
        # above stays in the conversation - that is safety truth.
        elif not want and self._freeze_waiting:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Freeze requested"), ()), "system")
        elif not want:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS frozen"), ()), "system")
        self._refresh_core_state()
        return confirmed

    @Slot()
    def freezeJarvis(self) -> None:
        """The red control and the tray: close the execution gate now. No confirmation.
        DFQUEUE: every queued message goes back to its draft first; none runs later."""
        if self._return_queue():
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Freeze: queued messages went back to their drafts"), ()), "system")
        self._set_gate(False)

    @Slot()
    def resumeJarvis(self) -> None:
        """Called after the person confirmed Resume. Opens the gate; a task that
        Freeze paused continues from its persisted stage, nothing else starts."""
        if not self._set_gate(True):
            return
        # R2 (Q4): from ANY screen - the open screen's paused task first, else the ONE parked
        # one (OD3); none while a run is busy (OD4: nothing is queued for later) or when the
        # choice is not unique. "Continuing" is said only for a run that is started.
        here = task_outcome(self._task).get("kind") == "paused"
        parked = [cid for cid, slots in self._parked.items() if task_outcome(slots["task"]).get("kind") == "paused"]
        start = None
        if self._busy:
            # The run in flight may itself be the paused task's continuation: that one is not "another".
            running = self._run_record["origin"] if self._run_record is not None else None
            waiting = (here and running != self._conversation_store.id) or any(cid != running for cid in parked)
            source = (QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed - the paused task was not continued: another run is in progress")
                      if waiting else QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed"))
        elif not here and len(parked) > 1:
            source = QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed - more than one task is paused, so none was continued")
        elif here or parked:
            start = self._paused_continuation(self._conversation_store.id if here else parked[0], here)
            source = (QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed - continuing the paused task") if start
                      else QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed - the paused task could not be continued"))
        else:
            source = QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed")
        self._note(("Bridge", source, ()), "system")
        if start is not None:
            start()

    def _paused_continuation(self, conversation_id: str | None, here: bool) -> Callable[[], None] | None:
        """The start of a paused task's continuation for its OWN conversation, or ``None`` when it
        is not provably that conversation's (identity, existence, readability, archive, session
        link - never relinked); then its carried state is dropped. A parked one continues without
        opening its conversation or touching the open screen: a recorded run whose origin it is."""
        slots = self._live_slots() if here else self._parked[conversation_id]
        session_id, task_id = slots["session_id"], str(slots["task"].get("task_id") or "")
        store = self._conversation_db()
        origin: PersistedConversation | None = self._conversation_store if here else None
        valid = bool(session_id and task_id)
        if valid and store is not None:
            if not here:
                origin = self._load_conversation(store, conversation_id)
            valid = origin is not None and self._parked_valid(store, conversation_id, slots)
        if not valid or origin is None:
            if here:
                self._set_live(_empty_slots())
                self.approvalChanged.emit()
                self._refresh_core_state()
            else:
                self._parked.pop(conversation_id, None)
                self._needs_you_check()
            return None
        if here:
            return lambda: self._run("task", lambda: self.backend.resume(session_id, task_id),
                                     self._delivered(self._apply_run), executing=True)
        return lambda: self._run("task", lambda: self.backend.resume(session_id, task_id),
                                 self._delivered(self._apply_run, origin=origin, identity=(session_id, task_id)),
                                 executing=True)

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

    @Slot(QObject)
    def minimizeWindow(self, window: QObject) -> None:
        """Minimize WITHOUT forgetting full screen / maximized. ``showMinimized``
        replaces the window states, so a restore from the taskbar came back
        windowed; adding the flag keeps them, exactly like the native minimize."""
        if isinstance(window, QWindow):
            window.setWindowStates(window.windowStates() | Qt.WindowState.WindowMinimized)

    @Property(str, notify=attentionChanged)
    def quitWarning(self) -> str:
        """Why quitting now needs a confirmation, from real state - or "". DFQUEUE: queued
        messages are named too."""
        queued = (tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "%1 queued message(s) will go back to their drafts and will not start."),
                     len(self._queue)) if self._queue else "")
        if self._busy:
            return " ".join(part for part in (tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS is running an operation right now. Quitting closes JARVIS without cancelling it: "
                                                            "the operation may keep running until it returns, and anything already sent to its "
                                                            "destination is not stopped or undone.")), queued) if part)
        outcome = task_outcome(self._task)
        if outcome.get("kind") in ("unknown", "in_progress") or outcome.get("run_status") == "cancelling":
            return " ".join(part for part in (tx("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS has work whose outcome is not established. Quitting does not resolve or stop it; "
                                                            "it stays recorded in History, and anything already sent to its destination is not undone.")), queued) if part)
        return queued

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
        self._return_queue()                   # DFQUEUE: queued messages wait in their drafts; none starts on its own
        self._save_drafts()                    # DFDRAFT: before the discard below empties the open one
        self._quitting = True
        self._discard_snapshots()
        discard_snapshots(self._snapshots)     # quitting keeps none, a running request's included (R1)
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
        approval = self._k3_card(dict(self._approval))
        if approval:
            # The connector's outbound sentence, rendered in the interface language
            # when JARVIS knows it (present.OUTBOUND_TEXT) and as written otherwise.
            approval["summary"] = render(approval.get("summary"))
            approval["reason"] = render(approval.get("reason"))
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
        outcome = _here(task_outcome(self._task, self._last_outcome == "unmet"))
        if outcome.get("message"):
            outcome["message"] = render(failure_message(outcome["code"], outcome["message"]))
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
            if decision.get("message"):
                decision["message"] = render(failure_message(decision.get("code"), decision["message"]))
            decision["time"] = (QLocale().toString(QDateTime.fromSecsSinceEpoch(int(decision["ts"])).time(), QLocale.FormatType.ShortFormat)
                                if decision.get("ts") else "")
        return {"fixture": False, "execution": execution, "running": self._busy, "approval": approval,
                "decision": decision, "outcome": outcome, "attention": items,
                "cards": bool(approval or self._decision or outcome
                              or execution in ("frozen", "freeze_requested", "refused", "unconfirmed"))}

    @staticmethod
    def _outcome_attention(kind: str) -> tuple | None:
        """(level, tone, label) for an outcome that needs attention, else None."""
        # ``unmet`` is deliberately absent: nothing failed, nothing is pending and
        # nothing was changed, so it is stated on the outcome card and in the live
        # feed rather than raised as something to act on.
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
                        "project_instruction": ph(QT_TRANSLATE_NOOP("Bridge", "[Project instruction, exactly as attested]")),
                        "project_files": [{"name": "notes.md", "chunk_index": 0, "classification": "internal",
                                           "text": ph(QT_TRANSLATE_NOOP("Bridge", "[Project file excerpt, exactly as attested]"))}],
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

    @Slot(str, str, str)
    def applyModel(self, model: str, effort: str, row: str = "") -> None:
        """``row``: the catalog row the model was chosen from; when it is an alias
        resolving to exactly this model, it is recorded as the model's lane."""
        model, effort = str(model).strip(), str(effort).strip()
        before = (self._config.get("claude_model"), self._config.get("claude_effort"))
        payload = self._write_claude(model, effort)
        self._apply_config(payload)
        if payload.get("error") is None and (row or model != (before[0] or "")):
            claude = next((p for p in self._providers if p.get("id") == "claude"), {})
            self._set_claude_lane(claude_lane(claude.get("models"), str(row), model), model)
        if (self._config.get("claude_model"), self._config.get("claude_effort")) != before:
            self.modelApplied.emit()

    def _write_claude(self, model: str, effort: str) -> dict:
        payload: dict = {}
        for key, value in (("claude_model", model), ("claude_effort", effort)):
            payload = self.backend.reset_config(key) if not value else self.backend.set_config(key, value)
            if payload.get("error") is not None:
                break
        return payload

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

    @Property(str, notify=prefsChanged)
    def coreSkin(self) -> str:
        return self.prefs.core_skin

    @Slot(str)
    def setCoreSkin(self, skin: str) -> None:
        if self.prefs.set_core_skin(skin):
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

    @Property(int, notify=prefsChanged)
    def freezeKeySkin(self) -> int:
        """Skin of the Freeze control (1-4). Presentation only; never authority."""
        return self.prefs.data["freeze_key"]

    @Slot(int)
    def setFreezeKeySkin(self, skin: int) -> None:
        if skin in FREEZE_KEYS:
            self.prefs.data["freeze_key"] = skin
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
        State itself is untouched - only its presentation is read again: the
        labels the Bridge projects (the owner "Global", the untitled name, the
        open project's rows) are rendered in the property getters, so
        re-emitting their signals is all it takes. No store read, no write."""
        self.feedChanged.emit()
        self.dataChanged.emit()
        self.stateChanged.emit()
        self.approvalChanged.emit()
        self.conversationChanged.emit()
        self.projectsChanged.emit()

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
        return self._page_override or self.prefs.data["page"]

    @Slot(str)
    def setPage(self, page: str) -> None:
        self._recheck_open()                    # N2 first; then the page asked for
        if page == "projects":
            if self._page_override != "projects":
                self._page_override = "projects"
                self.refreshProjects()
            self.prefsChanged.emit()
            return
        if page == "skills":    # not persisted: an older build never reads a page it does not know
            self._page_override = "skills"
            self.refreshSkills()
            self.prefsChanged.emit()
            return
        if page in PAGES:
            self._page_override = ""
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
            has_content = (name != "workflow") or bool(self._task) or self.openRunning or self._ticket is not None
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

    @Slot(str, str, bool, result=bool)
    def setCardVisible(self, region: str, card: str, visible: bool) -> bool:
        """Hide a card from the layout or add it back. Presentation only.
        False: refused (a fifth rail tile) - nothing changed or saved."""
        if region == "rail":
            if not self.prefs.set_rail_visible(str(card), bool(visible)):
                return False
        elif region == "stack":
            self.prefs.set_visible(str(card), bool(visible))
        else:
            return False
        self._layout_changed()
        return True

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
        self.refreshProviders(force=True)       # once per launch, in the background
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS interface started"), ()), "system")
        if self.devFixture:
            self._seed_dev_fixture()

    def shutdown(self) -> None:
        """The application is really quitting (never Window X, Freeze or Cancel):
        stop every clock this bridge owns and refuse late weather deliveries, so
        nothing schedules UI work while the QML tree is torn down. The bridge
        itself stays whole until the engine is gone (``jarvis.ui.app.teardown``)."""
        with self._weather_lock:
            self._shutting_down = True
        for timer in self.findChildren(QTimer):
            timer.stop()
        if not self._quitting:
            self._return_queue()               # DFQUEUE: a quit by another path keeps them too, as drafts
        self._save_drafts()                    # DFDRAFT: the last keystrokes too (no-op after confirmQuit's)
        # A turn whose write met a busy file is written now, not lost with the process.
        self._conversation_store.flush()
        self._memory_hook("memory_capture_shutdown", self._conversation_store.id or "")   # bounded (I12)

    def _memory_hook(self, name: str, conversation_id: str) -> None:
        """A backend memory-capture trigger; never raises into the window (I12)."""
        hook = getattr(self.backend, name, None)
        if callable(hook):
            try:
                hook(conversation_id)
            except Exception:  # noqa: BLE001 - capture never blocks the chat
                pass

    @Slot()
    def waitIdle(self) -> None:
        """Block until the worker pool drains (tests only)."""
        self._pool.waitForDone(30_000)
