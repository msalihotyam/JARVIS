"""The UI's one door into JARVIS: every action is one call the CLI already makes.

No Qt here. ``Backend`` wraps the application facade exactly the way
``jarvis.cli`` does - configuration is re-read from the store for every
action, so the kill switch and the model selection take effect the way a
fresh ``jarvis`` invocation would, and a refused store fails closed the
CLI's way. It renders nothing and decides nothing: the redacted envelopes
the backend returns are handed to the bridge as plain dicts.

The natural-language entry is ``JarvisApp.submit_text``: plain text in, the
authoritative ``text`` envelope out. The UI never builds task JSON, never
names a capability and never interprets destination grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from jarvis.app import (
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    EXIT_POLICY,
    JarvisApp,
    TaskInputError,
    _redact_text,
    build_default_app,
    config_failure_result,
    config_mutation_result,
    config_show_result,
    failure_result,
    job_archive_result,
    job_create_result,
    job_enablement_result,
    job_list_result,
    memory_list_result,
    sessions_result,
    show_result,
    status_payload,
    tasks_result,
)
from jarvis.cli import _config_change, _config_reset_change
from jarvis.antigravity import antigravity_state
from jarvis.codex import CodexIntentAdapter, codex_state, remember_codex_state
from jarvis.connectors.claude_code import AUTH_NOT_LOGGED_IN_EVIDENCE, ClaudeCodeConnector
from jarvis.intent import INTENT_SYSTEM_PROMPT, claude_model_catalog
from jarvis.model_retirement import codex_retirement
from jarvis.contracts import BillingClass, ErrorCode
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import ConfigError, ConfigStore, Settings

HISTORY_LIMIT = 20

#: Presentation-only redaction, the backend's own rule, applied once more to
#: every string the UI shows so no UI formatting path can re-expose a secret.
redact_text = _redact_text


@dataclass(frozen=True, slots=True)
class ApprovalTicket:
    """The exact identifiers one rendered approval envelope named.

    The backend re-proves all four against the persisted request before
    anything runs; the UI only hands back what the envelope printed.
    """

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


#: What each provider route REALLY accepts as an explicitly attached item,
#: established by a live probe of the same hardened invocation this build
#: uses - not assumed, and never the same answer for both by default:
#:
#: * Claude Code takes images as ``image`` content blocks of one user
#:   message on stdin (``--input-format stream-json``) with the empty tool
#:   grant, safe mode and empty MCP set unchanged.
#: * Codex takes images with ``codex exec -i``, with the read-only sandbox,
#:   ``approval_policy=never`` and every disabled feature unchanged.
#: * Text-like files are read by JARVIS itself and travel as text, so they
#:   need nothing from the provider and grant it no file access.
#: * No provider gets a file tool, a shell or a browser for this, and a
#:   combination that is not supported is refused - never re-routed.
ATTACHMENT_SUPPORT = {
    "claude": {"image": True, "text": True},
    "codex": {"image": True, "text": True},
    "google": {"image": False, "text": False},
    "qwen": {"image": False, "text": False},
}


class Backend:
    """Every UI action as one CLI-equivalent call, on freshly loaded config."""

    def __init__(
        self,
        store: ConfigStore | None = None,
        app_factory: Callable[[Settings], JarvisApp] = build_default_app,
        codex_factory: Callable[..., Any] = CodexIntentAdapter,
        codex_state: Callable[[], dict] = codex_state,
        claude_catalog: Callable[[], list] = claude_model_catalog,
        google_state: Callable[[], dict] = antigravity_state,
    ) -> None:
        self.store = store or ConfigStore()
        self.app_factory = app_factory
        self.codex_factory = codex_factory
        self.codex_state = codex_state
        self.google_state = google_state
        self.claude_catalog = claude_catalog
        #: Codex model -> what the last real call with it showed (this session only).
        self.codex_verified: dict[str, dict[str, str]] = {}
        #: The application instance whose understanding call is running, so
        #: ``cancel_text`` reaches the same adapter that owns the process.
        self._text_app: JarvisApp | None = None

    # -- plumbing -------------------------------------------------------------

    def execution_gate(self) -> bool:
        """The persisted switch as it is NOW (a refused store reads as closed)."""
        loaded = self.store.load()
        return loaded.usable and loaded.settings.execution_enabled is True

    def _app(self, settings: Settings) -> JarvisApp:
        """One application whose Core re-reads the switch at every stage
        boundary, so Freeze reaches work that is already running."""
        app = self.app_factory(settings)
        app.core.execution_gate = self.execution_gate
        return app

    def _call(self, command: str, action: Callable[[Settings], Any]) -> dict:
        loaded = self.store.load()
        mode = loaded.settings.permission_mode
        if not loaded.usable:
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

    @staticmethod
    def _memory(settings: Settings) -> SQLiteMemoryStore:
        return SQLiteMemoryStore(settings.memory_database)

    # -- configuration: the kill switch and the model selection --------------

    def config(self) -> dict:
        return config_show_result(self.store, self.store.load()).payload

    def status(self) -> dict:
        """Local metadata only, exactly like ``jarvis status``."""
        loaded = self.store.load()
        if not loaded.usable:
            return {}
        try:
            return status_payload(loaded.settings, self._memory(loaded.settings))
        except Exception:  # noqa: BLE001 - a broken store reads as unknown
            return {}

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
        return config_mutation_result(command, self.store, updated, self.store.load()).payload

    # -- providers --------------------------------------------------------------

    #: The provider lines a person can choose for General Chat / understanding.
    #: Choosing one changes who INTERPRETS a request - never what JARVIS may do.
    CHAT_PROVIDERS = ("claude", "codex")


    def providers(self) -> list[dict]:
        """What is REALLY known about each provider line, from local checks only
        - no model call, no spend: the Claude auth probe and the CLI's own model
        list; Codex's app-server account, plan limits and model catalog; whether
        the Antigravity CLI is installed (a file check - agy is never started).
        Qwen is deferred past V1."""
        return [self.provider_line(pid) for pid in ("claude", "codex", "google")] + [self.qwen_line()]

    @staticmethod
    def qwen_line() -> dict:
        return {"id": "qwen", "vendor": "Alibaba Cloud", "name": "Qwen", "status": "deferred", "models": [],
                "research": False, "attachments": dict(ATTACHMENT_SUPPORT["qwen"])}

    def provider_line(self, pid: str) -> dict:
        """One provider's line (see ``providers``). Each line depends on no other,
        so different providers may be checked at the same time."""
        if pid == "claude":
            loaded = self.store.load()
            if not loaded.usable:
                status, evidence, models = "unavailable", "config_refused", []
            else:
                try:
                    billing_class, evidence = ClaudeCodeConnector().billing_route()
                except Exception:  # noqa: BLE001 - an unreadable probe establishes nothing
                    billing_class, evidence = BillingClass.UNKNOWN, "claude_auth_status:unavailable"
                status = ("available" if billing_class is BillingClass.SUBSCRIPTION_INCLUDED
                          else "not_logged_in" if evidence == AUTH_NOT_LOGGED_IN_EVIDENCE else "unavailable")
                models = self.claude_catalog() if status == "available" else []
            return {"id": "claude", "vendor": "Anthropic", "name": "Claude", "status": status,
                    "evidence": evidence, "models": models, "research": status == "available",
                    "attachments": dict(ATTACHMENT_SUPPORT["claude"])}
        if pid == "codex":
            try:
                state = self.codex_state()
            except Exception:  # noqa: BLE001
                state = {"status": "unavailable", "models": []}
            if self.codex_state is codex_state:
                remember_codex_state(state)          # the panel's fresh check serves the next chat too
            # Models Codex itself retired (a declared successor it offers, or a
            # passed retirement time) leave normal selection; ``retired`` names
            # each successor for a saved choice (a fresh check only - never cached).
            offered, retired = codex_retirement([m for m in state.get("models", []) if isinstance(m, dict)],
                                                state.get("upgrades") if isinstance(state.get("upgrades"), dict) else {})
            # DISCOVERED (the account lists it) is not USABLE: each model carries what
            # a real call in this session showed - "usable", "failed" + reason, or
            # nothing yet ("" = discovered, not verified).
            models = [{**model, "verified": dict(self.codex_verified.get(model.get("id"), {}))} for model in offered]
            return {"id": "codex", "vendor": "OpenAI", "name": "Codex", "status": state.get("status", "unavailable"),
                    "plan": state.get("plan", ""), "models": models, "quota": state.get("quota") or {},
                    "retired": retired, "research": False, "attachments": dict(ATTACHMENT_SUPPORT["codex"])}
        try:
            google = self.google_state()
        except Exception:  # noqa: BLE001 - an unreadable check establishes nothing
            google = {"id": "google", "vendor": "Google", "name": "Antigravity", "status": "unavailable", "models": [], "research": False}
        # Never selectable in V1: no chat, no research, read-only quota at most.
        return {**google, "attachments": dict(ATTACHMENT_SUPPORT["google"])}

    # -- natural language -----------------------------------------------------

    def submit_text(
        self, text: str, session_id: str | None, *, previous_text: str = "", request_id: str = "",
        response_language: str = "", conversation: tuple = (), provider: str = "claude", model: str = "",
        effort: str = "", context_groups: tuple = (), context_attachments: tuple = (), response_mode: str = "together",
        on_reply_preview: Callable[[str], None] | None = None,
    ) -> dict:
        request_id = request_id or str(uuid4())

        def run(settings: Settings) -> Any:
            app = self._app(settings)
            if provider == "codex":
                # The person chose Codex: it interprets, the same code judges.
                # It has no verified research route, so research is refused, never re-routed.
                app.intent_model = self.codex_factory(INTENT_SYSTEM_PROMPT, model, effort)
                app.research_model = None
            elif provider != "claude":
                app.intent_model, app.research_model = None, None
            self._text_app = app
            try:
                return app.submit_text(
                    text, session_id, previous_text=previous_text, request_id=request_id,
                    response_language=response_language, conversation=conversation,
                    context_groups=context_groups, context_attachments=context_attachments, response_mode=response_mode,
                    on_reply_preview=on_reply_preview,
                )
            finally:
                self._text_app = None

        payload = self._call("text", run)
        if provider == "codex" and model:
            intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
            status = str(intent.get("status") or "")
            broken = ("model_failed", "model_unavailable", "model_timeout", "malformed_model_output")
            if status in broken and not intent.get("reason"):
                self.codex_verified[model] = {"state": "failed", "reason": str(intent.get("message") or "")[:240]}
            elif status and status not in broken + ("model_cancelled", "invalid_input"):
                self.codex_verified[model] = {"state": "usable"}      # the model answered through the safe contract
        return payload

    def cancel_text(self, request_id: str) -> bool:
        app = self._text_app
        return bool(app is not None and app.cancel_text(request_id))

    # -- tasks ----------------------------------------------------------------

    def approve(self, ticket: ApprovalTicket) -> dict:
        return self._call("approve", lambda s: self._app(s).approve(
            ticket.session_id, ticket.task_id, ticket.request_id, ticket.fingerprint
        ))

    def reject(self, ticket: ApprovalTicket) -> dict:
        return self._call("reject", lambda s: self._app(s).reject(
            ticket.session_id, ticket.task_id, ticket.request_id, ticket.fingerprint
        ))

    def cancel(self, session_id: str, task_id: str) -> dict:
        return self._call("cancel", lambda s: self._app(s).cancel(session_id, task_id))

    def clarify(self, session_id: str, task_id: str, text: str) -> dict:
        return self._call("clarify", lambda s: self._app(s).clarify(session_id, task_id, text))

    def resume(self, session_id: str, task_id: str) -> dict:
        return self._call("resume", lambda s: self._app(s).resume(session_id, task_id))

    def active_task_id(self, session_id: str) -> str | None:
        """Storage's answer to "which task is this session running now"."""
        loaded = self.store.load()
        if not loaded.usable:
            return None
        try:
            summary = self._memory(loaded.settings).session_summary(session_id)
        except Exception:  # noqa: BLE001 - unknown is not "none"
            return None
        if summary is None or not summary.readable:
            return None
        return summary.active_task_id

    # -- read-only visibility -------------------------------------------------

    def sessions(self) -> dict:
        return self._call("sessions", lambda s: sessions_result(self._memory(s), s, HISTORY_LIMIT))

    def tasks(self, session_id: str) -> dict:
        return self._call("tasks", lambda s: tasks_result(self._memory(s), s, session_id, HISTORY_LIMIT))

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
        return self._call("job-archive", lambda s: job_archive_result(self._memory(s), s, job_id, revision))

    def scheduler_once(self) -> dict:
        return self._call("scheduler-run", lambda s: self._app(s).run_scheduler(once=True))

    # -- memory ---------------------------------------------------------------

    def projects(self) -> list[str]:
        loaded = self.store.load()
        if not loaded.usable:
            return []
        try:
            return [p.id for p in self._memory(loaded.settings).list_projects() if p.active]
        except Exception:  # noqa: BLE001 - a broken store lists nothing
            return []

    def memory_list(self, project_id: str, include_inactive: bool = False) -> dict:
        return self._call("memory-list", lambda s: memory_list_result(
            self._memory(s), s, project_id, include_inactive
        ))
