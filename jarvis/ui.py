"""Minimal desktop UI (usable-v1): a presentation layer over the CLI's own calls.

Every action here is exactly ONE call the CLI already makes - the same
``JarvisApp`` verbs and ``jarvis.app`` result builders - on configuration
re-read from the store for every action, so the kill switch and the model
selection take effect exactly the way a fresh ``jarvis`` invocation would.

The UI holds no authority. It renders the redacted envelopes the backend
returns and hands back only the exact identifiers those envelopes named: an
approval is answered with the request id and fingerprint the backend printed,
and the backend re-proves both against the persisted request before anything
runs. Nothing here executes a connector, builds a grant or decides a policy.

Launch with ``python -m jarvis.ui``. Text-first; voice is deferred.
"""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Any, Callable

from jarvis.app import (
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_POLICY,
    CommandResult,
    TaskInputError,
    _redact_text,
    build_default_app,
    config_failure_result,
    config_mutation_result,
    config_show_result,
    decode_task_json,
    failure_result,
    job_archive_result,
    job_create_result,
    job_enablement_result,
    job_list_result,
    memory_list_result,
    sessions_result,
    show_result,
    tasks_result,
)
from jarvis.cli import _config_change, _config_reset_change
from jarvis.contracts import ErrorCode
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import CLAUDE_EFFORT_VALUES, ConfigError, ConfigStore, Settings

#: The documented Claude model-answer task a plain-text message becomes.
#: Claude is the only wired model provider; nothing else is offered.
PROVIDER = "claude"
CHAT_CAPABILITY = "code_analysis"
HISTORY_LIMIT = 20
DEFAULT_CHOICE = "(CLI default)"

#: Error codes that the backend raises BEFORE any effect: nothing executed.
REFUSED_CODES = frozenset(
    {
        "unknown_session", "task_not_found", "no_connector",
        "connector_unavailable", "connector_ambiguous", "invalid_grant",
        "stale_grant", "approval_rejected", "cost_cap_exceeded",
        "paid_api_disabled", "billing_route_unknown", "invalid_task",
        "egress_refused", "execution_disabled", "config_invalid",
        "config_io_failed", "task_already_terminal",
    }
)
UNKNOWN_CODE = "execution_unknown_outcome"
IN_PROGRESS_CODE = "execution_in_progress"
WAITING_STATUSES = frozenset(
    {"awaiting_approval", "awaiting_clarification", "awaiting_cost_override"}
)


def task_document(text: str) -> str:
    """Plain text is a question for the model; a JSON object is a task verbatim.

    Either way ``decode_task_json`` stays the only validator: the UI adds no
    field, no capability and no destination of its own.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    return json.dumps(
        {
            "objective": stripped,
            "capability": CHAT_CAPABILITY,
            "scope": {"action": "analyse", "target": "repo"},
        }
    )


@dataclass(frozen=True, slots=True)
class ApprovalTicket:
    """The exact identifiers one rendered approval envelope named."""

    session_id: str
    task_id: str
    request_id: str
    fingerprint: str


def approval_ticket(payload: Any) -> ApprovalTicket | None:
    """A ticket only when every identifier is present and a real string."""
    try:
        values = (
            payload["session"]["id"],
            payload["task"]["id"],
            payload["approval"]["request_id"],
            payload["approval"]["fingerprint"],
        )
    except (KeyError, TypeError):
        return None
    if not all(isinstance(value, str) and value for value in values):
        return None
    return ApprovalTicket(*values)


class Backend:
    """Every UI action as one CLI-equivalent call, on freshly loaded config."""

    def __init__(
        self,
        store: ConfigStore | None = None,
        app_factory: Callable[[Settings], Any] = build_default_app,
    ) -> None:
        self.store = store or ConfigStore()
        self.app_factory = app_factory

    def _call(self, command: str, action: Callable[[Settings], Any]) -> dict:
        loaded = self.store.load()
        mode = loaded.settings.permission_mode
        if not loaded.usable:
            # The CLI's own rule: no ordinary command proceeds on a guess.
            return failure_result(
                command, mode, ErrorCode.CONFIG_INVALID,
                "The stored configuration was refused, so execution is "
                "disabled. Repair it with 'jarvis config reset --all'.",
                EXIT_POLICY,
            ).payload
        try:
            return action(loaded.settings).payload
        except TaskInputError as exc:
            return failure_result(
                command, mode, ErrorCode.INVALID_TASK, str(exc), EXIT_INVALID_INPUT
            ).payload
        except Exception as exc:  # noqa: BLE001 - UI boundary; only the type
            return failure_result(
                command, mode, ErrorCode.CONNECTOR_FAILED,
                f"Unexpected internal failure: {type(exc).__name__}",
                EXIT_INTERNAL,
            ).payload

    def _memory(self, settings: Settings) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(settings.memory_database)

    # -- configuration: the kill switch and the model selection --------------

    def config(self) -> dict:
        return config_show_result(self.store, self.store.load()).payload

    def set_config(self, key: str, raw: str) -> dict:
        return self._mutate("config-set", lambda: _config_change(key, raw))

    def reset_config(self, key: str) -> dict:
        return self._mutate("config-reset", lambda: _config_reset_change(key))

    def _mutate(self, command: str, change: Callable[[], Any]) -> dict:
        mode = self.store.load().settings.permission_mode
        try:
            updated = self.store.mutate(change())
        except ConfigError as exc:
            return config_failure_result(command, mode, exc).payload
        return config_mutation_result(
            command, self.store, updated, self.store.load()
        ).payload

    # -- tasks ----------------------------------------------------------------

    def open_session(self, text: str) -> str | dict:
        """A session for a first task, created by Core the way ``run`` would.

        Created BEFORE the run so the running task can be cancelled by its
        storage id. The document is decoded first so a refused one leaves no
        session behind.
        """
        document = task_document(text)

        def start(settings: Settings) -> CommandResult:
            objective = decode_task_json(document).objective
            session = self.app_factory(settings).core.start_session(objective)
            return CommandResult({"session_id": session.id}, EXIT_OK)

        result = self._call("run", start)
        return result.get("session_id") or result

    def submit(self, text: str, session_id: str | None) -> dict:
        document = task_document(text)
        return self._call("run", lambda s: self.app_factory(s).run(document, session_id))

    def approve(self, ticket: ApprovalTicket) -> dict:
        return self._call("approve", lambda s: self.app_factory(s).approve(
            ticket.session_id, ticket.task_id, ticket.request_id, ticket.fingerprint
        ))

    def reject(self, ticket: ApprovalTicket) -> dict:
        return self._call("reject", lambda s: self.app_factory(s).reject(
            ticket.session_id, ticket.task_id, ticket.request_id, ticket.fingerprint
        ))

    def cancel(self, session_id: str, task_id: str) -> dict:
        return self._call("cancel", lambda s: self.app_factory(s).cancel(session_id, task_id))

    def clarify(self, session_id: str, task_id: str, text: str) -> dict:
        return self._call(
            "clarify", lambda s: self.app_factory(s).clarify(session_id, task_id, text)
        )

    def resume(self, session_id: str, task_id: str) -> dict:
        return self._call("resume", lambda s: self.app_factory(s).resume(session_id, task_id))

    def active_task_id(self, session_id: str) -> str | None:
        """Storage's answer to "which task is this session running now"."""
        loaded = self.store.load()
        if not loaded.usable:
            return None
        summary = self._memory(loaded.settings).session_summary(session_id)
        if summary is None or not summary.readable:
            return None
        return summary.active_task_id

    # -- read-only visibility -------------------------------------------------

    def sessions(self) -> dict:
        return self._call("sessions", lambda s: sessions_result(self._memory(s), s, HISTORY_LIMIT))

    def tasks(self, session_id: str) -> dict:
        return self._call(
            "tasks", lambda s: tasks_result(self._memory(s), s, session_id, HISTORY_LIMIT)
        )

    def show(self, session_id: str, task_id: str) -> dict:
        return self._call("show", lambda s: show_result(self._memory(s), s, session_id, task_id))

    # -- scheduler ------------------------------------------------------------

    def jobs(self, include_archived: bool = False) -> dict:
        return self._call("jobs", lambda s: job_list_result(
            self._memory(s), s, HISTORY_LIMIT, include_archived=include_archived
        ))

    def job_create(self, spec_text: str) -> dict:
        return self._call("job-create", lambda s: job_create_result(self._memory(s), s, spec_text))

    def job_set_enabled(self, job_id: str, revision: int, enabled: bool) -> dict:
        return self._call("job-enable", lambda s: job_enablement_result(
            self._memory(s), s, job_id, revision, enabled
        ))

    def job_archive(self, job_id: str, revision: int) -> dict:
        return self._call(
            "job-archive", lambda s: job_archive_result(self._memory(s), s, job_id, revision)
        )

    def scheduler_once(self) -> dict:
        return self._call("scheduler-run", lambda s: self.app_factory(s).run_scheduler(once=True))

    # -- memory ---------------------------------------------------------------

    def projects(self) -> list[str]:
        loaded = self.store.load()
        if not loaded.usable:
            return []
        return [p.id for p in self._memory(loaded.settings).list_projects() if p.active]

    def memory_list(self, project_id: str, include_inactive: bool = False) -> dict:
        return self._call("memory-list", lambda s: memory_list_result(
            self._memory(s), s, project_id, include_inactive
        ))


# --------------------------------------------------------------------------
# Presentation: payload -> (tag, text) lines. Pure, and never raises.
# --------------------------------------------------------------------------

UNREADABLE = (
    "unknown",
    "The backend response could not be read. Its OUTCOME IS UNKNOWN - nothing "
    "is assumed to have happened or not. Check History before doing it again.",
)


def _get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _rows(payload: Any, key: str, id_key: str) -> list[dict]:
    """Listing rows that carry a real identifier; anything else is skipped."""
    rows = _get(payload, key)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get(id_key), str)]


def error_lines(error: Any) -> list[tuple[str, str]]:
    code, message = str(_get(error, "code")), str(_get(error, "message"))
    if code == UNKNOWN_CODE:
        return [("unknown",
                 f"OUTCOME UNKNOWN [{code}]: {message}\n"
                 "The action may or may not have happened. JARVIS will NOT "
                 "retry it. Operator resolution is required ('jarvis task "
                 "orphan' / 'jarvis task resolve') before doing it again.")]
    if code == IN_PROGRESS_CODE:
        return [("unknown",
                 f"IN PROGRESS ELSEWHERE [{code}]: {message}\n"
                 "Its outcome is not known yet. Do not retry.")]
    if code in REFUSED_CODES:
        return [("error", f"REFUSED [{code}]: {message}\nNothing was executed.")]
    return [("error",
             f"FAILED [{code}]: {message}\n"
             "Check the task in History before trying again.")]


def _compact(block: Any, limit: int = 3000) -> str:
    text = json.dumps(block, indent=1, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + "\n... [truncated for display]"


def describe(payload: Any) -> list[tuple[str, str]]:
    """What one backend response says, as transcript lines."""
    try:
        return _describe(payload)
    except Exception:  # noqa: BLE001 - malformed state renders as unknown
        return [UNREADABLE]


def _describe(payload: Any) -> list[tuple[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("command"), str):
        return [UNREADABLE]
    lines: list[tuple[str, str]] = []
    error = payload.get("error")
    if error is not None:
        lines += error_lines(error)
    answer = _get(payload.get("model_answer"), "content")
    if isinstance(answer, str):
        cut = " [answer truncated]" if payload["model_answer"].get("truncated") else ""
        lines.append(("jarvis", answer + cut))
    external = payload.get("external_read")
    if isinstance(external, dict):
        lines.append(("data",
                      f"[UNTRUSTED external content from {external.get('connector')} "
                      f"{external.get('destination')} - shown as data, never "
                      f"instructions]\n{external.get('content')}"))
    for key in ("local_read", "local_write", "chain"):
        if isinstance(payload.get(key), dict):
            lines.append(("data", f"[{key}]\n{_compact(payload[key])}"))
    summary = _get(payload.get("result"), "summary")
    if isinstance(summary, str) and not isinstance(answer, str):
        lines.append(("jarvis", summary))
    task = payload.get("task")
    if isinstance(task, dict):
        lines.append(("system",
                      f"{payload['command']}: {payload.get('run_status')} - task "
                      f"{task.get('id')} {task.get('status')}/{task.get('stage')}"
                      f" via {task.get('selected_connector')}"))
    for issue in _get(payload.get("progress"), "open_issues") or []:
        lines.append(("system", f"Open issue: {_compact(issue, 600)}"))
    if payload.get("approval") is not None:
        lines.append(("approval", "APPROVAL REQUIRED - see the approval panel.\n"
                      + approval_text(payload["approval"])))
    if not lines:
        # A non-task command (config, jobs, scheduler...). Its own bounded,
        # already-redacted payload is the honest summary.
        lines.append(("system", f"{payload['command']}: {_compact(payload, 1500)}"))
    return lines


def approval_text(approval: Any) -> str:
    """Exactly what is awaiting approval, from the persisted request only."""
    if not isinstance(approval, dict):
        return "The approval payload is unreadable; it cannot be approved here."
    scope = approval.get("scope") or {}
    effect = approval.get("side_effect") or {}
    parts = [
        f"Action:       {_get(scope, 'action')}",
        f"Target:       {_get(scope, 'target')}",
        f"Destination:  {_get(scope, 'destination') or '(local)'}",
        f"Data class:   {_get(scope, 'data_classification')}",
        f"Effect:       {_get(effect, 'effect')} (reversible: {_get(effect, 'reversible')})",
        f"Risk level:   {approval.get('risk_level')}",
        f"Reason:       {approval.get('reason')}",
        f"Expires:      {approval.get('expires_at')}",
        f"Request:      {approval.get('request_id')}",
        f"Fingerprint:  {approval.get('fingerprint')}",
    ]
    if approval.get("outbound_preview") is not None:
        parts.append("Outbound preview:\n" + _compact(approval["outbound_preview"], 2000))
    if approval.get("attestation") is not None:
        parts.append("Attestation:\n" + _compact(approval["attestation"], 2000))
    return "\n".join(parts)


def describe_show(payload: Any) -> list[tuple[str, str]]:
    """One task's operational snapshot (the ``show`` payload)."""
    try:
        if not isinstance(payload, dict):
            return [UNREADABLE]
        if payload.get("error") is not None:
            return error_lines(payload["error"])
        task = payload["task"]
        lines = [("system",
                  f"Task {task.get('task_id')}\nstatus: {task.get('status')}  "
                  f"stage: {task.get('stage')}\ncapability: {task.get('capability')}"
                  f"  action: {task.get('action')}  connector: "
                  f"{task.get('selected_connector')}  readable: {task.get('readable')}")]
        attempt = payload.get("attempt")
        if isinstance(attempt, dict):
            state = attempt.get("state")
            tag = "unknown" if state in ("unknown_outcome", "in_flight", None) else "system"
            note = {
                "unknown_outcome": " - OUTCOME UNKNOWN: never replayed; operator "
                "resolution required",
                "in_flight": " - IN FLIGHT: outcome not established yet",
                None: " - attempt state unreadable: treat as UNKNOWN",
            }.get(state, "")
            lines.append((tag, f"attempt {attempt.get('id')}: {state}{note} "
                               f"(replayable: {attempt.get('replayable')})"))
        if payload.get("pending_approval") is not None:
            lines.append(("approval", "Awaiting approval - Resume this task to "
                                      "review the exact request."))
        for key in ("effect_applied", "deliverable", "latest_event"):
            if payload.get(key) is not None:
                lines.append(("system", f"{key}: {_compact(payload[key], 600)}"))
        return lines
    except Exception:  # noqa: BLE001
        return [UNREADABLE]


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

BG, PANEL, FG, MUTED = "#1e1f22", "#2b2d31", "#e6e6e6", "#9aa0a6"
TAG_COLORS = {
    "user": "#8ab4f8", "jarvis": FG, "system": MUTED, "error": "#f28b82",
    "unknown": "#fdd663", "approval": "#ffab40", "data": "#a8c7a0",
}


class JarvisWindow:
    """The minimal usable-v1 shell. Backend state is always authoritative."""

    def __init__(self, root: tk.Tk, backend: Backend, *, background: bool = True) -> None:
        self.root, self.backend, self.background = root, backend, background
        self.confirm: Callable[[str, str], bool] = messagebox.askyesno
        self.session_id: str | None = None
        self.task_id: str | None = None
        self.task_status: str | None = None
        self.ticket: ApprovalTicket | None = None
        self.config: dict = {}
        self.busy = False
        self.results: queue.Queue = queue.Queue()
        self.jobs_by_id: dict[str, dict] = {}
        self.tasks_by_id: dict[str, dict] = {}
        root.title("JARVIS")
        root.geometry("1180x760")
        root.configure(bg=BG)
        self._style()
        self._build()
        self.refresh_config()
        root.after(100, self._poll)

    # -- layout ---------------------------------------------------------------

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TButton", background=PANEL, foreground=FG, padding=5)
        style.configure("Kill.TButton", background="#b3261e", foreground="white")
        style.configure("Treeview", background=PANEL, foreground=FG, rowheight=22)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(12, 4))
        style.map("TNotebook.Tab", background=[("selected", BG)])
        style.map("TButton", background=[("disabled", BG)], foreground=[("disabled", MUTED)])

    def _text(self, parent: tk.Widget, height: int = 10) -> tk.Text:
        text = tk.Text(parent, bg=PANEL, fg=FG, insertbackground=FG, wrap="word",
                       height=height, relief="flat", padx=8, pady=6)
        for tag, color in TAG_COLORS.items():
            text.tag_configure(tag, foreground=color)
        text.configure(state="disabled")
        return text

    def _tree(self, parent: tk.Widget, columns: tuple[str, ...], height: int = 8) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=height)
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=120, stretch=True)
        return tree

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        self.exec_label = ttk.Label(top, font=("Segoe UI", 10, "bold"))
        self.exec_label.pack(side="left")
        self.kill_button = ttk.Button(top, command=self.toggle_execution)
        self.kill_button.pack(side="left", padx=6)
        ttk.Label(top, text=f"Provider: {PROVIDER}   Model:").pack(side="left", padx=(12, 2))
        self.model_var = tk.StringVar(value=DEFAULT_CHOICE)
        ttk.Combobox(top, textvariable=self.model_var, values=(DEFAULT_CHOICE,),
                     width=22).pack(side="left")
        ttk.Label(top, text="Effort:").pack(side="left", padx=(8, 2))
        self.effort_var = tk.StringVar(value=DEFAULT_CHOICE)
        ttk.Combobox(top, textvariable=self.effort_var, state="readonly", width=12,
                     values=(DEFAULT_CHOICE, *CLAUDE_EFFORT_VALUES)).pack(side="left")
        ttk.Button(top, text="Apply", command=self.apply_model).pack(side="left", padx=4)
        ttk.Button(top, text="New session", command=self.new_session).pack(side="right")
        self.session_label = ttk.Label(top, foreground=MUTED)
        self.session_label.pack(side="right", padx=6)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._build_chat()
        self._build_history()
        self._build_scheduler()
        self._build_memory()

    def _build_chat(self) -> None:
        chat = ttk.Frame(self.notebook)
        self.notebook.add(chat, text="Chat")
        left = ttk.Frame(chat)
        left.pack(side="left", fill="both", expand=True)
        self.transcript = self._text(left, height=30)
        self.transcript.pack(fill="both", expand=True)
        row = ttk.Frame(left, padding=(0, 6))
        row.pack(fill="x")
        self.input_var = tk.StringVar()
        self.input = ttk.Entry(row, textvariable=self.input_var)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", lambda _e: self.submit())
        self.send_button = ttk.Button(row, text="Send", command=self.submit)
        self.send_button.pack(side="left", padx=4)
        ttk.Label(left, foreground=MUTED, text=(
            "Plain text asks Claude (code_analysis). A JSON object is sent as a "
            "task document, validated by the backend.")).pack(anchor="w")

        right = ttk.Frame(chat, padding=(8, 0), width=380)
        right.pack(side="right", fill="y")
        self.approval_frame = tk.Frame(right, bg="#5c3b00", padx=6, pady=6)
        tk.Label(self.approval_frame, text="APPROVAL REQUIRED", bg="#5c3b00",
                 fg="#ffab40", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.approval_text = self._text(self.approval_frame, height=14)
        self.approval_text.configure(width=48)
        self.approval_text.pack(fill="both")
        buttons = tk.Frame(self.approval_frame, bg="#5c3b00")
        buttons.pack(fill="x", pady=(6, 0))
        self.approve_button = ttk.Button(buttons, text="Approve", command=self.approve)
        self.approve_button.pack(side="left")
        self.deny_button = ttk.Button(buttons, text="Deny", command=self.deny)
        self.deny_button.pack(side="left", padx=6)

        status = ttk.LabelFrame(right, text="Current task", padding=6)
        status.pack(fill="x", side="bottom")
        self.status_label = ttk.Label(status, justify="left", wraplength=360)
        self.status_label.pack(anchor="w")
        self.cancel_button = ttk.Button(status, text="Cancel task", command=self.cancel_task)
        self.cancel_button.pack(anchor="w", pady=(6, 0))

    def _build_history(self) -> None:
        tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab, text="History")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh_history).pack(side="left")
        self.use_session_button = ttk.Button(bar, text="Use this session", command=self.use_session)
        self.use_session_button.pack(side="left", padx=4)
        self.resume_button = ttk.Button(bar, text="Resume task", command=self.resume_task)
        self.resume_button.pack(side="left")
        ttk.Label(bar, foreground=MUTED,
                  text=f"Most recent {HISTORY_LIMIT}; read-only.").pack(side="left", padx=8)
        self.sessions_tree = self._tree(tab, ("session", "updated", "tasks", "active task"), 6)
        self.sessions_tree.pack(fill="x", pady=4)
        self.sessions_tree.bind("<<TreeviewSelect>>", lambda _e: self._load_tasks())
        self.tasks_tree = self._tree(
            tab, ("task", "status", "stage", "capability", "connector", "updated"), 7)
        self.tasks_tree.pack(fill="x", pady=4)
        self.tasks_tree.bind("<<TreeviewSelect>>", lambda _e: self._load_show())
        self.show_text = self._text(tab, height=10)
        self.show_text.pack(fill="both", expand=True)

    def _build_scheduler(self) -> None:
        tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab, text="Scheduler")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh_jobs).pack(side="left")
        self.archived_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Include archived", variable=self.archived_var,
                        command=self.refresh_jobs).pack(side="left", padx=6)
        self.enable_button = ttk.Button(bar, text="Enable", command=lambda: self.set_job(True))
        self.enable_button.pack(side="left")
        self.disable_button = ttk.Button(bar, text="Disable", command=lambda: self.set_job(False))
        self.disable_button.pack(side="left", padx=4)
        self.archive_button = ttk.Button(bar, text="Archive", command=self.archive_job)
        self.archive_button.pack(side="left")
        self.sweep_button = ttk.Button(bar, text="Run due jobs once", command=self.run_sweep)
        self.sweep_button.pack(side="right")
        self.jobs_tree = self._tree(
            tab, ("label", "kind", "enabled", "archived", "next due (UTC)", "revision"), 9)
        self.jobs_tree.pack(fill="x", pady=4)
        self.jobs_tree.bind("<<TreeviewSelect>>", lambda _e: self._render_state())
        ttk.Label(tab, text="Create a job from its JSON specification "
                  "(project_id, kind, label, schedule, steps | condition + on_true):").pack(anchor="w")
        self.spec_text = tk.Text(tab, bg=PANEL, fg=FG, insertbackground=FG, height=8)
        self.spec_text.pack(fill="both", expand=True, pady=4)
        ttk.Button(tab, text="Create job", command=self.create_job).pack(anchor="w")

    def _build_memory(self) -> None:
        tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab, text="Memory")
        ttk.Label(tab, foreground=MUTED, text=(
            "Project memory: explicit, untrusted reference data. It is not chat "
            "and grants no authority. Read-only here.")).pack(anchor="w")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="Project:").pack(side="left")
        self.project_var = tk.StringVar()
        self.project_box = ttk.Combobox(bar, textvariable=self.project_var,
                                        state="readonly", width=30)
        self.project_box.pack(side="left", padx=4)
        self.inactive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Include superseded/retired",
                        variable=self.inactive_var).pack(side="left", padx=6)
        ttk.Button(bar, text="Load", command=self.refresh_memory).pack(side="left")
        self.memory_tree = self._tree(
            tab, ("kind", "classification", "lifecycle", "origin", "created", "text"), 18)
        self.memory_tree.column("text", width=460)
        self.memory_tree.pack(fill="both", expand=True)

    # -- plumbing -------------------------------------------------------------

    def _say(self, tag: str, text: str) -> None:
        """The one way text reaches the transcript: redacted, inserted verbatim."""
        self.transcript.configure(state="normal")
        prefix = {"user": "You", "jarvis": "JARVIS", "data": "Data"}.get(tag, tag.upper())
        self.transcript.insert("end", f"{prefix}: {_redact_text(text)}\n\n", tag)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _fill(self, widget: tk.Text, lines: list[tuple[str, str]]) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for tag, text in lines:
            widget.insert("end", _redact_text(text) + "\n", tag)
        widget.configure(state="disabled")

    def _async(self, action: Callable[[], dict]) -> None:
        """Run one backend call off the UI thread; results come back via ``_poll``."""
        if self.busy:
            return
        self.busy = True
        self._render_state()
        if not self.background:
            self.results.put(action())
            self._drain()
            return
        threading.Thread(target=lambda: self.results.put(action()), daemon=True).start()

    def _poll(self) -> None:
        self._drain()
        self.root.after(100, self._poll)

    def _drain(self) -> None:
        while True:
            try:
                payload = self.results.get_nowait()
            except queue.Empty:
                return
            self.busy = False
            self.show(payload)

    def show(self, payload: dict) -> None:
        """Render one task envelope. Replaces the task view and the approval."""
        for tag, text in describe(payload):
            self._say(tag, text)
        if isinstance(payload, dict):
            session, task = payload.get("session"), payload.get("task")
            if isinstance(_get(session, "id"), str):
                self.session_id = session["id"]
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                self.task_id, self.task_status = task["id"], task.get("status")
                usage = _get(payload.get("result"), "usage") or {}
                self.status_label.configure(text=(
                    f"task: {task['id']}\nrun: {payload.get('run_status')}\n"
                    f"status: {task.get('status')}  stage: {task.get('stage')}\n"
                    f"connector: {task.get('selected_connector')}\n"
                    f"provider/model: {usage.get('provider')} / {usage.get('model')}"))
        # The ticket is ONLY ever the one this envelope named; a response with
        # no approval (or an unreadable one) clears whatever was shown before.
        self.ticket = approval_ticket(payload)
        if self.ticket is not None:
            self._fill(self.approval_text, [("approval", approval_text(payload["approval"]))])
            self.notebook.select(0)
            self.root.bell()
        self._render_state()

    def _render_state(self) -> None:
        refused = self.config.get("config_status") not in ("valid", "absent")
        enabled = self.config.get("execution_enabled") is True and not refused
        if refused:
            self.exec_label.configure(text="CONFIG REFUSED - execution off (fail-closed)",
                                      foreground=TAG_COLORS["error"])
        else:
            self.exec_label.configure(
                text="EXECUTION ENABLED" if enabled else "EXECUTION DISABLED (kill switch on)",
                foreground="#81c995" if enabled else TAG_COLORS["error"])
        self.kill_button.configure(
            text="KILL SWITCH" if enabled else "Re-enable execution",
            style="Kill.TButton" if enabled else "TButton")
        self._enable(self.kill_button, not refused)
        self.session_label.configure(text=f"session: {self.session_id or '(new)'}")
        waiting_answer = self.task_status == "awaiting_clarification"
        self.send_button.configure(text="Answer" if waiting_answer else "Send")
        self._enable(self.send_button, not self.busy and not refused and self.ticket is None)
        has_ticket = self.ticket is not None and not self.busy
        self._enable(self.approve_button, has_ticket)
        self._enable(self.deny_button, has_ticket)
        if self.ticket is None:
            self.approval_frame.pack_forget()
        elif not self.approval_frame.winfo_manager():
            self.approval_frame.pack(fill="x", side="top")
        live = self.task_status not in (None, "completed", "failed", "rejected", "cancelled")
        self._enable(self.cancel_button, bool(self.session_id) and (live or self.busy))
        self._enable(self.sweep_button, not self.busy and not refused)
        task = self.tasks_by_id.get(self._selected(self.tasks_tree) or "")
        self._enable(self.resume_button, not self.busy and task is not None
                     and task.get("status") in WAITING_STATUSES)
        self._enable(self.use_session_button, self._selected(self.sessions_tree) is not None)
        job = self.jobs_by_id.get(self._selected(self.jobs_tree) or "")
        actionable = job is not None and job.get("readable") is True and not job.get("archived")
        self._enable(self.enable_button, actionable and not job.get("enabled"))
        self._enable(self.disable_button, actionable and job.get("enabled") is True)
        self._enable(self.archive_button, actionable)

    @staticmethod
    def _enable(widget: ttk.Button, on: bool) -> None:
        widget.state(["!disabled"] if on else ["disabled"])

    @staticmethod
    def _selected(tree: ttk.Treeview) -> str | None:
        selection = tree.selection()
        return selection[0] if selection else None

    # -- actions --------------------------------------------------------------

    def refresh_config(self) -> None:
        self._apply_config(self.backend.config())

    def _apply_config(self, payload: dict) -> None:
        if payload.get("error") is not None:
            for tag, text in describe(payload):
                self._say(tag, text)
            payload = self.backend.config()
        self.config = payload
        self.model_var.set(payload.get("claude_model") or DEFAULT_CHOICE)
        self.effort_var.set(payload.get("claude_effort") or DEFAULT_CHOICE)
        self._render_state()

    def toggle_execution(self) -> None:
        """THE kill switch: the persisted ``execution_enabled`` barrier."""
        enabled = self.config.get("execution_enabled") is True
        if not enabled and not self.confirm(
                "Re-enable execution", "Allow JARVIS to start work again?"):
            return
        payload = self.backend.set_config("execution_enabled", "false" if enabled else "true")
        self._say("system", "Kill switch ON: no new work may start. Running work "
                  "is stopped with Cancel." if enabled else "Execution re-enabled.")
        self._apply_config(payload)

    def apply_model(self) -> None:
        payload: dict = {}
        for key, value in (("claude_model", self.model_var.get().strip()),
                           ("claude_effort", self.effort_var.get())):
            payload = (self.backend.reset_config(key) if value in ("", DEFAULT_CHOICE)
                       else self.backend.set_config(key, value))
            if payload.get("error") is not None:
                break
        self._apply_config(payload)

    def new_session(self) -> None:
        if self.busy:
            return
        self.session_id = self.task_id = self.task_status = None
        self.ticket = None
        self._say("system", "New session: the next message starts a fresh session.")
        self._render_state()

    def submit(self) -> None:
        text = self.input_var.get().strip()
        if self.ticket is not None:
            self._say("system", "Answer the pending approval first (Approve, Deny or Cancel).")
            return
        if not text or self.busy:
            return
        self.input_var.set("")
        self._say("user", text)
        if self.task_status == "awaiting_clarification" and self.session_id and self.task_id:
            session_id, task_id = self.session_id, self.task_id
            self._async(lambda: self.backend.clarify(session_id, task_id, text))
            return
        if self.session_id is None:
            opened = self.backend.open_session(text)
            if isinstance(opened, dict):
                self.show(opened)
                return
            self.session_id = opened
        session_id = self.session_id
        self.task_id = self.task_status = None
        self.status_label.configure(text="running...")
        self._async(lambda: self.backend.submit(text, session_id))

    def approve(self) -> None:
        self._decide(self.backend.approve)

    def deny(self) -> None:
        self._decide(self.backend.reject)

    def _decide(self, verb: Callable[[ApprovalTicket], dict]) -> None:
        ticket = self.ticket
        if ticket is None or self.busy:
            return
        # The backend re-proves request id + fingerprint against the persisted
        # request; the UI only hands back what that request's envelope named.
        self.ticket = None
        self._async(lambda: verb(ticket))

    def cancel_task(self) -> None:
        """Cancel through Core. While running, the id is storage's, not ours."""
        session_id = self.session_id
        task_id = self.backend.active_task_id(session_id) if self.busy and session_id else self.task_id
        if not session_id or not task_id:
            self._say("system", "No cancellable task is registered yet.")
            return
        self.show(self.backend.cancel(session_id, task_id))

    # -- history --------------------------------------------------------------

    def refresh_history(self) -> None:
        payload = self.backend.sessions()
        self.sessions_tree.delete(*self.sessions_tree.get_children())
        self.tasks_tree.delete(*self.tasks_tree.get_children())
        self.tasks_by_id = {}
        if payload.get("error") is not None:
            self._fill(self.show_text, error_lines(payload["error"]))
            return
        for row in _rows(payload, "sessions", "session_id"):
            self.sessions_tree.insert("", "end", iid=row["session_id"], values=(
                row["session_id"], row.get("updated_at"), row.get("task_count"),
                row.get("active_task_id") or ""))
        self._render_state()

    def _load_tasks(self) -> None:
        session_id = self._selected(self.sessions_tree)
        self.tasks_tree.delete(*self.tasks_tree.get_children())
        self.tasks_by_id = {}
        if session_id is None:
            return
        payload = self.backend.tasks(session_id)
        if payload.get("error") is not None:
            self._fill(self.show_text, error_lines(payload["error"]))
            return
        for row in _rows(payload, "tasks", "task_id"):
            self.tasks_by_id[row["task_id"]] = row
            self.tasks_tree.insert("", "end", iid=row["task_id"], values=(
                row["task_id"], row.get("status"), row.get("stage"),
                row.get("capability"), row.get("selected_connector"), row.get("updated_at")))
        self._render_state()

    def _load_show(self) -> None:
        session_id, task_id = self._selected(self.sessions_tree), self._selected(self.tasks_tree)
        if session_id and task_id:
            self._fill(self.show_text, describe_show(self.backend.show(session_id, task_id)))
        self._render_state()

    def use_session(self) -> None:
        session_id = self._selected(self.sessions_tree)
        if session_id and not self.busy:
            self.session_id, self.task_id, self.task_status = session_id, None, None
            self._say("system", f"Using session {session_id}.")
            self._render_state()

    def resume_task(self) -> None:
        """Re-present a parked task through the backend's own ``resume``."""
        session_id, task_id = self._selected(self.sessions_tree), self._selected(self.tasks_tree)
        task = self.tasks_by_id.get(task_id or "")
        if not (session_id and task_id and task) or task.get("status") not in WAITING_STATUSES:
            return
        self.session_id = session_id
        self.notebook.select(0)
        self._async(lambda: self.backend.resume(session_id, task_id))

    # -- scheduler ------------------------------------------------------------

    def refresh_jobs(self) -> None:
        payload = self.backend.jobs(self.archived_var.get())
        self.jobs_tree.delete(*self.jobs_tree.get_children())
        self.jobs_by_id = {}
        if payload.get("error") is not None:
            for tag, text in error_lines(payload["error"]):
                self._say(tag, text)
            return
        for row in _rows(payload, "jobs", "id"):
            self.jobs_by_id[row["id"]] = row
            label = row.get("label") if row.get("readable") else "(unreadable definition)"
            self.jobs_tree.insert("", "end", iid=row["id"], values=(
                label, row.get("kind"), row.get("enabled"), row.get("archived"),
                row.get("next_due_at_utc") or "-", row.get("revision")))
        self._render_state()

    def _job_result(self, payload: dict) -> None:
        for tag, text in describe(payload):
            self._say(tag, text)
        self.refresh_jobs()

    def set_job(self, enabled: bool) -> None:
        job = self.jobs_by_id.get(self._selected(self.jobs_tree) or "")
        if job is not None:
            # The revision this listing printed: storage compares and sets on it.
            self._job_result(self.backend.job_set_enabled(job["id"], job["revision"], enabled))

    def archive_job(self) -> None:
        job = self.jobs_by_id.get(self._selected(self.jobs_tree) or "")
        if job is not None and self.confirm(
                "Archive job", f"Archive '{job.get('label')}'? There is no un-archive."):
            self._job_result(self.backend.job_archive(job["id"], job["revision"]))

    def create_job(self) -> None:
        spec = self.spec_text.get("1.0", "end").strip()
        if spec:
            self._job_result(self.backend.job_create(spec))

    def run_sweep(self) -> None:
        self.notebook.select(0)
        self._say("system", "Running one scheduler sweep...")
        self._async(self.backend.scheduler_once)

    # -- memory ---------------------------------------------------------------

    def refresh_memory(self) -> None:
        projects = self.backend.projects()
        self.project_box.configure(values=projects)
        if self.project_var.get() not in projects:
            self.project_var.set(projects[0] if projects else "")
        self.memory_tree.delete(*self.memory_tree.get_children())
        if not self.project_var.get():
            return
        payload = self.backend.memory_list(self.project_var.get(), self.inactive_var.get())
        if payload.get("error") is not None:
            for tag, text in error_lines(payload["error"]):
                self._say(tag, text)
            return
        for item in _rows(payload, "items", "id"):
            self.memory_tree.insert("", "end", values=(
                item.get("kind"), item.get("classification"), item.get("lifecycle"),
                item.get("origin"), item.get("created_at"),
                _redact_text(str(item.get("text")))))


def main() -> None:
    root = tk.Tk()
    window = JarvisWindow(root, Backend())
    window.refresh_history()
    window.refresh_jobs()
    window.refresh_memory()
    root.mainloop()


if __name__ == "__main__":
    main()
