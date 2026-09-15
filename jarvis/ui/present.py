"""Envelope -> display records. Pure functions, no Qt, never raise.

Every function here turns one redacted backend envelope into bounded display
data for the QML layer: feed entries ``{kind, text}``, an approval card, a
task status block. Nothing is interpreted as authority; external content is
tagged as data; a malformed envelope renders as UNKNOWN rather than as
anything more reassuring.

JARVIS-owned wording is returned as a MESSAGE - ``(context, source, args)`` -
not as finished text: the bridge renders it through ``jarvis.ui.i18n`` each
time it is shown, so the interface language applies to it (and to it again
after a switch). Codes, ids, Core messages and model or external content stay
exactly as they came, as arguments.
"""

from __future__ import annotations

import json
from typing import Any


def QT_TRANSLATE_NOOP(context: str, source: str) -> str:  # noqa: N802 - the marker lupdate extracts
    return source


def _msg(source: str, *args: Any) -> tuple:
    """A JARVIS-owned message in the ``Present`` context, rendered later."""
    return ("Present", source, tuple(args))

#: Error codes the backend raises BEFORE any effect: nothing executed.
REFUSED_CODES = frozenset({
    "unknown_session", "task_not_found", "no_connector", "connector_unavailable",
    "connector_ambiguous", "invalid_grant", "stale_grant", "approval_rejected",
    "cost_cap_exceeded", "paid_api_disabled", "billing_route_unknown", "invalid_task",
    "egress_refused", "execution_disabled", "config_invalid", "config_io_failed",
    "task_already_terminal",
})
UNKNOWN_CODE = "execution_unknown_outcome"
IN_PROGRESS_CODE = "execution_in_progress"
WAITING_STATUSES = frozenset({"awaiting_approval", "awaiting_clarification", "awaiting_cost_override"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected", "cancelled"})

#: What each pre-task intent verdict means to the person reading it.
INTENT_TEXT = {
    "structured": None,
    "scheduled": None,
    "clarification_required": None,
    "unsupported_capability": QT_TRANSLATE_NOOP("Present", "UNSUPPORTED: %1"),
    "authority_unavailable": QT_TRANSLATE_NOOP("Present", "NOT AUTHORIZED: %1"),
    "malformed_model_output": QT_TRANSLATE_NOOP("Present", "REFUSED: %1"),
    "model_unavailable": QT_TRANSLATE_NOOP("Present", "UNDERSTANDING MODEL UNAVAILABLE: %1"),
    "model_timeout": QT_TRANSLATE_NOOP("Present", "UNDERSTANDING TIMED OUT: %1"),
    "model_cancelled": QT_TRANSLATE_NOOP("Present", "UNDERSTANDING CANCELLED: %1"),
    "model_failed": QT_TRANSLATE_NOOP("Present", "UNDERSTANDING FAILED: %1"),
    "execution_disabled": QT_TRANSLATE_NOOP("Present", "EXECUTION DISABLED: %1"),
    "invalid_input": QT_TRANSLATE_NOOP("Present", "INVALID REQUEST: %1"),
}

#: JARVIS's own explanation of an ESTABLISHED failure cause, keyed by the
#: stable reason code the envelope carries. Shown instead of the provider's
#: generic diagnostic; every other failure keeps its truthful generic text.
#: The key is ``jarvis.intent.CLAUDE_NOT_LOGGED_IN``; this module imports nothing.
REASON_TEXT = {
    "claude_not_logged_in": QT_TRANSLATE_NOOP("Present", "Claude Code CLI is not logged in. Run `claude auth login` in a normal "
                                                       "PowerShell, complete the Claude subscription login, then restart JARVIS."),
}

UNREADABLE = {
    "kind": "unknown",
    "text": _msg(QT_TRANSLATE_NOOP("Present", "The backend response could not be read. Its OUTCOME IS UNKNOWN - nothing is "
                                              "assumed to have happened or not. Check History before doing it again.")),
}


def _get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _compact(block: Any, limit: int = 3000) -> str:
    text = json.dumps(block, indent=1, sort_keys=True, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n... [truncated for display]"


def error_entries(error: Any) -> list[dict[str, str]]:
    code, message = str(_get(error, "code")), str(_get(error, "message"))
    if code == UNKNOWN_CODE:
        return [{"kind": "unknown", "text": _msg(QT_TRANSLATE_NOOP("Present",
            "OUTCOME UNKNOWN [%1]: %2\nThe action may or may not have happened. "
            "JARVIS will NOT retry it. Operator resolution is required ('jarvis task orphan' / "
            "'jarvis task resolve') before doing it again."), code, message)}]
    if code == IN_PROGRESS_CODE:
        return [{"kind": "unknown", "text": _msg(QT_TRANSLATE_NOOP("Present",
            "IN PROGRESS ELSEWHERE [%1]: %2\nIts outcome is not known yet. Do not retry."), code, message)}]
    if code in REFUSED_CODES:
        return [{"kind": "error", "text": _msg(QT_TRANSLATE_NOOP("Present", "REFUSED [%1]: %2\nNothing was executed."), code, message)}]
    return [{"kind": "error", "text": _msg(QT_TRANSLATE_NOOP("Present",
        "FAILED [%1]: %2\nCheck the task in History before trying again."), code, message)}]


def run_entries(payload: Any) -> list[dict[str, str]]:
    """What one run/approve/reject/cancel/resume envelope says."""
    try:
        return _run_entries(payload)
    except Exception:  # noqa: BLE001 - malformed state renders as unknown
        return [dict(UNREADABLE)]


def _run_entries(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("command"), str):
        return [dict(UNREADABLE)]
    entries: list[dict[str, str]] = []
    if payload.get("error") is not None:
        entries += error_entries(payload["error"])
    answer = _get(payload.get("model_answer"), "content")
    if isinstance(answer, str):
        # The model's words stay as written; only the truncation note is JARVIS's.
        entries.append({"kind": "jarvis", "text": _msg(QT_TRANSLATE_NOOP("Present", "%1 [answer truncated]"), answer)
                        if payload["model_answer"].get("truncated") else answer})
    external = payload.get("external_read")
    if isinstance(external, dict):
        entries.append({"kind": "data", "text": _msg(QT_TRANSLATE_NOOP("Present",
            "[UNTRUSTED external content from %1 %2 - shown as data, never instructions]\n%3"),
            external.get("connector"), external.get("destination"), external.get("content"))})
    for key in ("local_read", "local_write", "chain"):
        if isinstance(payload.get(key), dict):
            entries.append({"kind": "data", "text": f"[{key}]\n{_compact(payload[key])}"})
    summary = _get(payload.get("result"), "summary")
    if isinstance(summary, str) and not isinstance(answer, str):
        entries.append({"kind": "jarvis", "text": summary})
    task = payload.get("task")
    if isinstance(task, dict):
        entries.append({"kind": "system", "text": _msg(QT_TRANSLATE_NOOP("Present", "%1: %2 - task %3 %4/%5 via %6"),
            payload["command"], payload.get("run_status"), task.get("id"), task.get("status"),
            task.get("stage"), task.get("selected_connector"))})
    for issue in _get(payload.get("progress"), "open_issues") or []:
        if _get(issue, "kind") == "approval_required":
            continue  # the authorization card says it
        entries.append({"kind": "system", "text": _msg(QT_TRANSLATE_NOOP("Present", "Open issue: %1"), _compact(issue, 600))})
    if payload.get("approval") is not None:
        entries.append({"kind": "approval", "text": _msg(QT_TRANSLATE_NOOP("Present", "APPROVAL REQUIRED - review the authorization card."))})
    if not entries:
        entries.append({"kind": "system", "text": f"{payload['command']}: {_compact(payload, 1500)}"})
    return entries


def text_entries(payload: Any) -> list[dict[str, str]]:
    """What one ``text`` (natural-language) envelope says, run included."""
    try:
        intent = payload["intent"]
        status = str(intent.get("status"))
    except (KeyError, TypeError, AttributeError):
        return [dict(UNREADABLE)]
    entries: list[dict[str, str]] = []
    if status == "structured":
        if intent.get("objective"):
            text = _msg(QT_TRANSLATE_NOOP("Present", "Understood as %1 -> %2: %3"),
                        intent.get("capability"), intent.get("destination"), intent.get("objective"))
        else:
            text = _msg(QT_TRANSLATE_NOOP("Present", "Understood as %1 -> %2"), intent.get("capability"), intent.get("destination"))
        entries.append({"kind": "system", "text": text})
        # The run envelope itself is rendered by the caller's run path.
    elif status == "scheduled":
        job = payload.get("job") or {}
        due = job.get("next_due_at_utc") or _msg(QT_TRANSLATE_NOOP("Present", "never"))
        entries.append({"kind": "scheduled", "text": _msg(QT_TRANSLATE_NOOP("Present",
            "SCHEDULED: '%1' (%2) as job %3 - next due %4 (UTC). Nothing has run; the scheduler "
            "runs it and every approval still applies."), job.get("label"), job.get("kind"), job.get("job_id"), due)})
    elif status == "conversation":
        # General Chat: the model's words, shown as JARVIS's turn, as written.
        reply = intent.get("reply")
        entries.append({"kind": "jarvis", "text": str(reply)} if reply else dict(UNREADABLE))
    elif status == "clarification_required":
        question = intent.get("question")
        entries.append({"kind": "clarify", "text": str(question) if question else _msg(QT_TRANSLATE_NOOP("Present", "JARVIS needs one detail."))})
    elif REASON_TEXT.get(str(intent.get("reason") or "")):
        # An established cause: JARVIS explains it in the interface language.
        entries.append({"kind": "error", "text": _msg(REASON_TEXT[str(intent["reason"])])})
    else:
        template = INTENT_TEXT.get(status)
        message = str(intent.get("message") or "")
        if template is None:
            entries.append({"kind": "unknown", "text": _msg(QT_TRANSLATE_NOOP("Present", "UNKNOWN INTENT STATUS [%1]: %2"), status, message)})
        else:
            entries.append({"kind": "error", "text": _msg(template, message)})
    return entries


def approval_card(payload: Any) -> dict[str, Any]:
    """Exactly what is awaiting approval, from the persisted request only."""
    approval = _get(payload, "approval")
    if not isinstance(approval, dict):
        return {}
    scope = approval.get("scope") or {}
    effect = approval.get("side_effect") or {}
    preview = approval.get("outbound_preview")
    card = {
        "summary": str(_get(preview, "summary") or ""),
        "action": str(_get(scope, "action") or ""),
        "target": str(_get(scope, "target") or ""),
        "destination": str(_get(scope, "destination") or "(local)"),
        "connector": str(_get(scope, "connector") or ""),
        "data_classification": str(_get(scope, "data_classification") or ""),
        "effect": str(_get(effect, "effect") or ""),
        "reversible": bool(_get(effect, "reversible")),
        "risk_level": str(approval.get("risk_level")),
        "reason": str(approval.get("reason") or ""),
        "expires_at": str(approval.get("expires_at") or ""),
        "request_id": str(approval.get("request_id") or ""),
        "fingerprint": str(approval.get("fingerprint") or ""),
        "outbound_preview": (
            _compact(approval["outbound_preview"], 2000) if approval.get("outbound_preview") else ""
        ),
        "attestation": _compact(approval["attestation"], 2000) if approval.get("attestation") else "",
        "task_id": str(_get(_get(payload, "task"), "id") or ""),
        "session_id": str(_get(_get(payload, "session"), "id") or ""),
    }
    # Approvable only with everything that binds and explains the request:
    # what is done, through which connector, and the exact request identity.
    card["complete"] = all(card[k] for k in ("action", "connector", "request_id", "fingerprint", "task_id", "session_id"))
    return card


def task_outcome(task: Any) -> dict[str, str]:
    """The current task's outcome as Core reported it, or ``{}`` while there
    is none. Kinds stay distinct: ``blocked`` (refused before any effect -
    this attempt did not proceed), ``failed`` (Core reported failure),
    ``unknown`` (completion cannot be verified), ``in_progress`` (running
    elsewhere, not established yet), ``completed``. Nothing is inferred from
    history or from the execution switch."""
    if not isinstance(task, dict) or not task.get("task_id"):
        return {}
    code, status = str(task.get("error_code") or ""), str(task.get("run_status") or "")
    task_state = str(task.get("status") or "")
    # Paused by Freeze: Core's barrier stopped a REGISTERED task at a stage
    # boundary and left it exactly where it was - not terminal, not waiting on
    # a person, past the first stage (a refusal before registration persists
    # nothing, so there is nothing to continue).
    paused = (code == "execution_disabled" and task_state not in TERMINAL_STATUSES
              and task_state not in WAITING_STATUSES and str(task.get("stage") or "") not in ("", "understand"))
    kind = ("unknown" if code == UNKNOWN_CODE else "in_progress" if code == IN_PROGRESS_CODE
            else "paused" if paused
            else "" if status == "rejected"          # a decision, shown by its receipt
            else "cancelled" if status in ("cancelled", "cancelling")
            else "blocked" if code in REFUSED_CODES
            else "failed" if code or status == "failed"
            else "completed" if status == "completed" else "")
    if not kind:
        return {}
    return {"kind": kind, "run_status": status, "code": code, "message": str(task.get("error_message") or ""),
            "task_id": str(task["task_id"]), "connector": str(task.get("connector") or ""),
            "capability": str(task.get("capability") or ""), "destination": str(task.get("destination") or "")}


def task_progress(task: Any, core_state: str, busy: bool) -> dict[str, Any]:
    """The four lifecycle stages (understanding, authorization, execution,
    outcome) for the header, projected from the bridge's authoritative state:
    Core's task, the core state and whether an operation is running. Active
    only once Core has registered a task, while it runs or is not terminal. A
    message still being understood - General Chat included - has no task and
    projects nothing (the status group says REASONING). Each stage is pending
    / running / waiting / done / error / unknown / denied / skipped."""
    task = task if isinstance(task, dict) else {}
    status, cs = str(task.get("status") or ""), core_state
    if not task.get("task_id") or not (busy or status not in TERMINAL_STATUSES):
        return {"active": False, "stages": []}
    if status == "rejected":
        stages = ["done", "denied", "skipped", "skipped"]
    elif status == "cancelled":
        stages = ["done", "skipped", "skipped", "skipped"]
    else:
        ended = {"completed": "done", "error": "error", "unknown": "unknown"}.get(cs)
        stages = [
            "done",
            "waiting" if cs == "awaiting_approval"
            else "done" if task.get("run_status") or cs in ("completed", "error", "unknown") else "pending",
            "running" if cs == "running" else ended or "pending",
            ended or "pending",
        ]
    return {"active": True, "stages": stages}


def task_status(run: Any, intent: Any = None) -> dict[str, Any]:
    """The bounded current-task block for the Active Workflow panel."""
    task = _get(run, "task") or {}
    usage = _get(_get(run, "result"), "usage") or {}
    error = _get(run, "error") or {}
    return {
        "task_id": str(task.get("id") or ""),
        "session_id": str(_get(_get(run, "session"), "id") or ""),
        "status": str(task.get("status") or ""),
        "stage": str(task.get("stage") or ""),
        "connector": str(task.get("selected_connector") or ""),
        "run_status": str(_get(run, "run_status") or ""),
        "provider": str(usage.get("provider") or ""),
        "model": str(usage.get("model") or ""),
        "error_code": str(error.get("code") or ""),
        "error_message": str(error.get("message") or ""),
        "intent_status": str(_get(intent, "status") or ""),
        "capability": str(_get(intent, "capability") or ""),
        "destination": str(_get(intent, "destination") or ""),
    }


def outcome_kind(run: Any, intent_status: str = "") -> str:
    """The core-state word one envelope earns: completed / error / unknown / waiting / idle."""
    if intent_status and intent_status not in ("structured",):
        return "idle" if intent_status in ("scheduled", "clarification_required", "conversation") else "error"
    code = str(_get(_get(run, "error"), "code") or "")
    if code in (UNKNOWN_CODE, IN_PROGRESS_CODE):
        return "unknown"
    status = str(_get(run, "run_status") or "")
    if status in WAITING_STATUSES:
        return "waiting"
    if status == "completed":
        return "completed"
    if status in ("cancelled", "cancelling", "rejected"):
        return "idle"
    return "error" if code or status == "failed" else "idle"


def show_entries(payload: Any) -> list[dict[str, str]]:
    """One task's operational snapshot (the ``show`` payload)."""
    try:
        if not isinstance(payload, dict):
            return [dict(UNREADABLE)]
        if payload.get("error") is not None:
            return error_entries(payload["error"])
        task = payload["task"]
        entries = [{"kind": "system", "text": _msg(QT_TRANSLATE_NOOP("Present",
            "Task %1\nstatus: %2  stage: %3\ncapability: %4  action: %5  connector: %6  readable: %7"),
            task.get("task_id"), task.get("status"), task.get("stage"), task.get("capability"),
            task.get("action"), task.get("selected_connector"), task.get("readable"))}]
        attempt = payload.get("attempt")
        if isinstance(attempt, dict):
            state = attempt.get("state")
            note = {
                "unknown_outcome": _msg(QT_TRANSLATE_NOOP("Present", " - OUTCOME UNKNOWN: never replayed; operator resolution required")),
                "in_flight": _msg(QT_TRANSLATE_NOOP("Present", " - IN FLIGHT: outcome not established yet")),
                None: _msg(QT_TRANSLATE_NOOP("Present", " - attempt state unreadable: treat as UNKNOWN")),
            }.get(state, "")
            kind = "unknown" if state in ("unknown_outcome", "in_flight", None) else "system"
            entries.append({"kind": kind, "text": _msg(QT_TRANSLATE_NOOP("Present", "attempt %1: %2%3 (replayable: %4)"),
                            attempt.get("id"), state, note, attempt.get("replayable"))})
        if payload.get("pending_approval") is not None:
            entries.append({"kind": "approval", "text": _msg(QT_TRANSLATE_NOOP("Present", "Awaiting approval - resume this task to review the exact request."))})
        for key in ("effect_applied", "deliverable", "latest_event"):
            if payload.get(key) is not None:
                entries.append({"kind": "system", "text": f"{key}: {_compact(payload[key], 600)}"})
        return entries
    except Exception:  # noqa: BLE001
        return [dict(UNREADABLE)]


def rows(payload: Any, key: str, id_key: str) -> list[dict]:
    """Listing rows that carry a real identifier; anything else is skipped."""
    items = _get(payload, key)
    if not isinstance(items, list):
        return []
    return [r for r in items if isinstance(r, dict) and isinstance(r.get(id_key), str)]
