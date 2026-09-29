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
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import Property, QDateTime, QLocale, QObject, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QWindow

from jarvis.context_items import (
    DEFAULT_RESPONSE_MODE,
    MAX_ITEMS,
    NOTE_MAX_CHARS,
    RESPONSE_MODES,
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
from jarvis.conversation import CorruptConversation, PersistedConversation, task_title, turn_refs
from jarvis.model_catalog import ProviderCatalog, default_catalog_path
from jarvis.model_retirement import adopt_claude_lane, claude_lane, claude_migration, codex_migration
from jarvis.ui.backend import ATTACHMENT_SUPPORT, ApprovalTicket, Backend, approval_ticket, redact_text
from jarvis.ui.i18n import LANGUAGES, QT_TRANSLATE_NOOP, render, tx
from jarvis.ui import weather
from jarvis.ui.telemetry import Sampler
from jarvis.ui.preferences import (
    CHAT_PROVIDERS,
    CONTEXTUAL_PANELS,
    PAGES,
    PANEL_IDS,
    PROFILES,
    THEMES,
    UiPreferences,
    STATUS_INDICATORS,
    validate_preferences,
)
from jarvis.ui.present import (
    REFUSED_CODES,
    display_model_name,
    provider_brand,
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


#: What a pasted image is called on the draft (the clipboard names nothing).
CLIPBOARD_IMAGE_NAME = "pasted-image.png"


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
            if self._work_started is not None:
                self._work_ms = max(0, round((self._clock() - self._work_started) * 1000))
        handler(result)
        if kind == "task" and self._freeze_waiting and not self._busy:
            # The operation that was running when Freeze arrived has returned
            # (at its next safe point, or finished): now JARVIS is frozen.
            self._freeze_waiting = False
            if not self.executionEnabled:
                # Lifecycle, not conversation: the live feed and the attention strip say it.
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

        What the person typed, General Chat's completed replies and the task
        results they were shown - exactly once each - and nothing else: never a
        notice, an approval, an error, a fixture, a partial or interrupted reply,
        or anything a planning component wrote. A user turn keeps its own words
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
        elif kind == "jarvis" and entry.get("chat") and isinstance(message, str):
            store.append(role="assistant", kind="reply", text=message, key=f"{self._store_run}:{entry['id']}")
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
                         capability=str(entry.get("capability") or ""), destination=str(entry.get("destination") or ""))
        if store.store is not None and store.diagnostic:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "The conversation could not be saved: %1"),
                        (store.diagnostic,)), "error")

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
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Continuing the previous conversation (%1 turns)"),
                        (len(opened.state.turns),)), "system")
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

    def _restore_feed(self, state: Any) -> None:
        """The opened conversation's remembered turns, shown again - the same
        words that were shown, never re-remembered."""
        feed = []
        for turn in state.turns:
            if turn.kind == "user":
                entry: dict[str, Any] = {"kind": "user", "msg": self._redacted(turn.message or turn.text)}
            elif turn.kind == "reply":
                entry = {"kind": "jarvis", "msg": self._redacted(turn.text), "chat": True}
            else:
                text = turn.text.split("\n", 1)[1] if turn.text.startswith("[Task result") and "\n" in turn.text else turn.text
                entry = {"kind": "result", "msg": self._redacted(text), "capability": turn.capability,
                         "destination": turn.destination}
            self._entry_seq += 1
            entry.update(ts=float(turn.ts), id=f"m{self._entry_seq}", restored=True)
            feed.append(entry)
        self._feed = feed[-FEED_LIMIT:]
        self.feedChanged.emit()

    def _name_conversation(self, conversation: PersistedConversation, message: str) -> None:
        """A conversation is named from its first message, once."""
        title = task_title(message)[:120]
        if not title or conversation.store is None:
            return
        try:
            conversation.store.set_conversation_title(conversation.id, title)
        except Exception:  # noqa: BLE001 - a label only; the conversation is unaffected
            return
        self._refresh_conversations()

    def _refresh_conversations(self) -> None:
        store = self._conversation_db()
        try:
            records = store.list_conversations(project_id=None) if store is not None else []
        except Exception:  # noqa: BLE001 - storage boundary
            records = []
        self._conversation_list = [
            {"id": r.id, "origin": r.origin, "updated_at": r.updated_at,
             "title": r.title or (("Bridge", QT_TRANSLATE_NOOP("Bridge", "Previous conversation"), ())
                                  if r.origin == "legacy-import"
                                  else ("Bridge", QT_TRANSLATE_NOOP("Bridge", "New conversation"), ()))}
            for r in records
        ]
        self.conversationChanged.emit()

    @Property("QVariantList", notify=conversationChanged)
    def conversations(self) -> list:
        """The Global conversations, newest first, the open one marked."""
        return [{**row, "title": render(row["title"]) if isinstance(row["title"], tuple) else row["title"],
                 "active": row["id"] == self._conversation_store.id} for row in self._conversation_list]

    @Property(str, notify=conversationChanged)
    def conversationId(self) -> str:
        return self._conversation_store.id or ""

    def _activate(self, opened: PersistedConversation) -> None:
        """Open ``opened``: its history on screen, a fresh Core session for it."""
        self._conversation_store = opened
        self._session_id, self._task, self._ticket, self._approval, self._clarification = "", {}, None, {}, {}
        self._decision = {}
        self._discard_snapshots()
        self._last_outcome = "idle"
        self._end_stream()
        self._restore_feed(opened.state)
        self._refresh_conversations()
        self.approvalChanged.emit()
        self._refresh_core_state()

    @Slot(result=bool)
    def newConversation(self) -> bool:
        """Start a new Global conversation and open it."""
        if self._busy:
            return False
        store = self._conversation_db()
        if store is None:
            return False
        try:
            record = store.create_conversation()
        except Exception as exc:  # noqa: BLE001 - storage boundary; only the type
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A new conversation could not be created: %1"),
                        (type(exc).__name__,)), "error")
            return False
        self._activate(PersistedConversation(store, record.id))
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "New conversation"), ()), "system")
        return True

    @Slot(str, result=bool)
    def openConversation(self, conversation_id: str) -> bool:
        """Open a saved conversation; its history returns to the screen."""
        conversation_id = str(conversation_id)
        if self._busy:
            return False
        if conversation_id == self._conversation_store.id:
            return True
        store = self._conversation_db()
        if store is None:
            return False
        opened = self._load_conversation(store, conversation_id)
        if opened is None:
            return False
        self._activate(opened)
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Opened a conversation (%1 turns)"),
                    (len(opened.state.turns),)), "system")
        return True

    def _session_conversation(self, session_id: str) -> str | None:
        store = self._conversation_db()
        if store is None or not session_id:
            return None
        try:
            return store.conversation_for_session(str(session_id))
        except Exception:  # noqa: BLE001 - storage boundary
            return None

    def _payload_owner(self, payload: Any, origin: PersistedConversation) -> PersistedConversation:
        """The conversation a run's envelope belongs to.

        A session already linked belongs to ITS conversation. A session seen for
        the first time is linked to ``origin``, the conversation the request came
        from. Without a session the envelope belongs to ``origin``."""
        run = payload.get("run") if isinstance(payload, dict) and isinstance(payload.get("run"), dict) else payload
        session = run.get("session") if isinstance(run, dict) else None
        session_id = session.get("id") if isinstance(session, dict) else None
        store = origin.store
        if not isinstance(session_id, str) or not session_id or store is None:
            return origin
        linked = self._session_conversation(session_id)
        if linked is None:
            try:
                store.link_session_conversation(session_id, origin.id)
            except Exception:  # noqa: BLE001 - an unlinkable session stays with its origin
                pass
            return origin
        if linked == origin.id:
            return origin
        if linked == self._conversation_store.id:
            return self._conversation_store
        return self._load_conversation(store, linked) or origin

    def _delivered(self, handler: Callable[[Any], None]) -> Callable[[Any], None]:
        """``handler`` for the conversation open NOW; anything that turns out to
        belong to another conversation is remembered there instead."""
        origin = self._conversation_store

        def deliver(payload: Any) -> None:
            owner = self._payload_owner(payload, origin)
            if owner.id == self._conversation_store.id:
                handler(payload)
                return
            self._remember_elsewhere(payload, owner)

        return deliver

    def _remember_elsewhere(self, payload: Any, owner: PersistedConversation) -> None:
        """What a conversation that is not open would have remembered: its
        completed replies and its task results. Nothing on screen changes."""
        intent = payload.get("intent") if isinstance(payload, dict) and isinstance(payload.get("intent"), dict) else None
        status = str((intent or {}).get("status") or "")
        if intent is not None:
            for entry in text_entries(payload):
                if status in ("conversation", "research") and isinstance(entry.get("text"), str):
                    self._say(entry.pop("kind"), entry.pop("text"), chat=True, owner=owner)
        run = payload.get("run") if intent is not None and isinstance(payload, dict) else payload
        if isinstance(run, dict) and isinstance(run.get("task"), dict):
            facts = {key: task_status(run, intent).get(key, "") for key in ("capability", "destination")}
            task_id = str(run["task"].get("id") or "")
            run_status = str(run.get("run_status") or "")
            for entry in run_entries(run, facts):
                if entry.get("kind") == "result":
                    self._say(entry.pop("kind"), entry.pop("text"), owner=owner, task_id=task_id,
                              run_status=run_status, **entry)
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "A reply for another conversation was saved there"), ()), "system")

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
        resolved = task_status(payload, intent) if intent is not None else {}
        facts = {key: resolved.get(key) or self._task.get(key, "") for key in ("capability", "destination")}
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
            self._say(entry.pop("kind"), entry.pop("text"), task_id=task_id, run_status=run_status, **entry)
        if isinstance(payload, dict):
            session_id = (payload.get("session") or {}).get("id") if isinstance(payload.get("session"), dict) else None
            if isinstance(session_id, str):
                self._session_id = session_id
            if isinstance(payload.get("task"), dict) and isinstance(payload["task"].get("id"), str):
                self._task = task_status(payload, intent)
        if self._task.get("task_id"):
            if request:
                self._conversation_store.name_auto((self._task["task_id"], self._session_id), task_title(request))
            self._task["title"] = self._conversation_store.title(self._task["task_id"])
        self._ticket = approval_ticket(payload)
        # Shown whenever Core sent a request - incomplete ones included, so a
        # request missing its binding reads as unavailable, never as absent.
        self._approval = approval_card(payload)
        self._last_outcome = outcome_kind(payload, facts=facts)
        if self._ticket is not None:
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Approval required: %1 -> %2"),
                        (self._approval.get("action"), self._approval.get("destination"))), "approval")
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
        # Only the request that is still running may show text.
        if request_id != self._request_id or not self._busy or not isinstance(text, str):
            return
        if not self._stream_text:
            self._stream_ts = time.time()
            if self._first_visible is None and text.strip():
                self._first_visible = self._clock()
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
        if partial and status not in ("conversation", "research"):
            # The reply had started and did not finish: what was shown stays, marked
            # incomplete - never passed off as an answer, never part of the history.
            self._say("jarvis", partial, interrupted=True)
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
        entries = text_entries(payload)
        answered = status in ("conversation", "research")
        for index, entry in enumerate(entries):
            if answered and index == len(entries) - 1:
                measured = self._measured(*self._intent_reports(intent))
                if measured:
                    entry["telemetry"] = measured
            if status == "conversation" and not isinstance(entry.get("text"), str):
                # Separately answered groups: rendered once, in the interface language,
                # so the reply is ordinary text - copyable, quotable, part of the history.
                entry["text"] = render(entry["text"])
            if partial and status == "conversation":
                entry["streamed"] = True        # replaces the preview in place: no second entrance
            self._say(entry.pop("kind"), entry.pop("text"), chat=status in ("conversation", "research"), **entry)
        self._clarification = {}
        if status == "structured":
            self._apply_run(payload.get("run"), intent, request=request or text)
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
        elif status == "research":
            self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "Web Research answered with %1 sources"),
                        (len((intent or {}).get("sources") or []),)), "system")
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

    @Property(float, notify=stateChanged)
    def workStartedAt(self) -> float:
        """When the request being worked on started, in epoch milliseconds; 0 when
        nothing is running. Display only - the live turn's running time. The ONE
        measured duration of a finished answer is its telemetry (``_measured``)."""
        return self._work_started_at * 1000 if self._busy else 0.0

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
        self.inputAccepted.emit()
        session_id = self._session_id or None
        request_id = str(uuid4())
        self._request_id = request_id
        if self._task.get("status") == "awaiting_clarification" and session_id and self._task.get("task_id"):
            task_id = self._task["task_id"]
            self._run("task", lambda: self.backend.clarify(session_id, task_id, text), self._delivered(self._apply_run))
            return True
        self._task = {}
        language = self.language           # the default reply language for General Chat
        route = self._provider_route()
        owner = self._conversation_store.id or ""     # its owner scopes the request ("" = unsaved Global)
        self._end_stream()
        relay = self._preview_relay(request_id)
        self._run(
            "task",
            lambda: self.backend.submit_text(text, session_id, request_id=request_id, response_language=language,
                                             conversation=conversation, context_groups=groups,
                                             context_attachments=attachments, response_mode=mode,
                                             on_reply_preview=relay, conversation_id=owner, **route),
            self._delivered(lambda payload: self._apply_text(payload, text)),
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
            self._delivered(lambda payload: self._apply_text(payload, label, request=previous)),
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
        self._run("task", lambda: call(ticket), self._delivered(lambda payload: self._apply_decision(payload, verb, ticket, shown)),
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
        self._run("cancel", lambda: self.backend.cancel(session_id, task_id), self._delivered(self._apply_run))

    def _discard_snapshots(self) -> None:
        """Throw away every attachment snapshot JARVIS copied for this session."""
        self.clearContext()
        discard_snapshots(self._snapshots)

    @Slot()
    def newSession(self) -> None:
        if self._busy:
            return
        self._session_id, self._task, self._ticket, self._approval, self._clarification = "", {}, None, {}, {}
        self._decision = {}
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
        if session_id and not self._busy and self._open_owner_of(str(session_id)):
            self._session_id, self._task, self._ticket, self._approval, self._decision = str(session_id), {}, None, {}, {}
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
        self._run("sessions", self.backend.sessions, self._set_sessions)

    def _set_sessions(self, payload: Any) -> None:
        self._sessions = self._titled(rows(payload, "sessions", "session_id"), "session_id")
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

    _catalogDone = Signal()
    _liveLine = Signal(str, object)

    def _probe_provider(self, pid: str) -> Any:
        """The catalog's live check (its own thread). This fresh answer - never
        the cache - is the only thing that may move a saved model choice."""
        line = self.backend.provider_line(pid)
        if self.sync:
            self._follow_models(pid, line)
        else:
            with self._weather_lock:
                if not self._shutting_down:
                    self._liveLine.emit(pid, line)
        return line

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
        with self._weather_lock:            # the shutdown lock: after quit begins, nothing is delivered
            if not self._shutting_down:
                self._catalogDone.emit()

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
    def claudeLimits(self) -> dict:
        """Claude plan limits as Claude Code last REPORTED them on a real call
        (``windows``: utilization 0-1 and reset time; ``seen_at``), or {}."""
        return dict(self._claude_limits)

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
        General Chat's replies only. A notice, an approval, an error, external
        data or a task result is never quotable."""
        for entry in self._feed:
            if entry.get("id") == entry_id and isinstance(entry.get("msg"), str):
                return entry if entry["kind"] == "user" or entry.get("chat") else None
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
        for owner in [self._attachments] + [group["attachments"] for group in self._groups]:
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

    # -- conversation actions ----------------------------------------------------

    @Slot(str)
    def copyText(self, text: str) -> None:
        """Copy exactly the visible plain text of one conversational message."""
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(str(text))

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
        """The red control and the tray: close the execution gate now. No confirmation."""
        self._set_gate(False)

    @Slot()
    def resumeJarvis(self) -> None:
        """Called after the person confirmed Resume. Opens the gate; a task that
        Freeze paused continues from its persisted stage, nothing else starts."""
        if not self._set_gate(True):
            return
        paused = task_outcome(self._task).get("kind") == "paused"
        self._note(("Bridge", QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed - continuing the paused task")
                    if paused else QT_TRANSLATE_NOOP("Bridge", "JARVIS resumed"), ()), "system")
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

    @Slot(QObject)
    def minimizeWindow(self, window: QObject) -> None:
        """Minimize WITHOUT forgetting full screen / maximized. ``showMinimized``
        replaces the window states, so a restore from the taskbar came back
        windowed; adding the flag keeps them, exactly like the native minimize."""
        if isinstance(window, QWindow):
            window.setWindowStates(window.windowStates() | Qt.WindowState.WindowMinimized)

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
        self._discard_snapshots()
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
        outcome = task_outcome(self._task, self._last_outcome == "unmet")
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
        # A turn whose write met a busy file is written now, not lost with the process.
        self._conversation_store.flush()

    @Slot()
    def waitIdle(self) -> None:
        """Block until the worker pool drains (tests only)."""
        self._pool.waitForDone(30_000)
