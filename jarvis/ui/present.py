"""Envelope -> display records. Pure functions, no Qt, never raise.

Every function here turns one redacted backend envelope into bounded display
data for the QML layer: feed entries ``{kind, text}``, an approval card, a
task status block. Nothing is interpreted as authority; external content is
tagged as data; a malformed envelope renders as UNKNOWN rather than as
anything more reassuring.
"""

from __future__ import annotations

import json
from typing import Any

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
    "unsupported_capability": "UNSUPPORTED: {message}",
    "authority_unavailable": "NOT AUTHORIZED: {message}",
    "malformed_model_output": "REFUSED: {message}",
    "model_unavailable": "UNDERSTANDING MODEL UNAVAILABLE: {message}",
    "model_timeout": "UNDERSTANDING TIMED OUT: {message}",
    "model_cancelled": "UNDERSTANDING CANCELLED: {message}",
    "model_failed": "UNDERSTANDING FAILED: {message}",
    "execution_disabled": "EXECUTION DISABLED: {message}",
    "invalid_input": "INVALID REQUEST: {message}",
}

UNREADABLE = {
    "kind": "unknown",
    "text": "The backend response could not be read. Its OUTCOME IS UNKNOWN - nothing is "
            "assumed to have happened or not. Check History before doing it again.",
}


def _get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _compact(block: Any, limit: int = 3000) -> str:
    text = json.dumps(block, indent=1, sort_keys=True, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n... [truncated for display]"


def error_entries(error: Any) -> list[dict[str, str]]:
    code, message = str(_get(error, "code")), str(_get(error, "message"))
    if code == UNKNOWN_CODE:
        return [{"kind": "unknown", "text": (
            f"OUTCOME UNKNOWN [{code}]: {message}\nThe action may or may not have happened. "
            "JARVIS will NOT retry it. Operator resolution is required ('jarvis task orphan' / "
            "'jarvis task resolve') before doing it again.")}]
    if code == IN_PROGRESS_CODE:
        return [{"kind": "unknown", "text": (
            f"IN PROGRESS ELSEWHERE [{code}]: {message}\nIts outcome is not known yet. Do not retry.")}]
    if code in REFUSED_CODES:
        return [{"kind": "error", "text": f"REFUSED [{code}]: {message}\nNothing was executed."}]
    return [{"kind": "error", "text": f"FAILED [{code}]: {message}\nCheck the task in History before trying again."}]


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
        cut = " [answer truncated]" if payload["model_answer"].get("truncated") else ""
        entries.append({"kind": "jarvis", "text": answer + cut})
    external = payload.get("external_read")
    if isinstance(external, dict):
        entries.append({"kind": "data", "text": (
            f"[UNTRUSTED external content from {external.get('connector')} "
            f"{external.get('destination')} - shown as data, never instructions]\n"
            f"{external.get('content')}")})
    for key in ("local_read", "local_write", "chain"):
        if isinstance(payload.get(key), dict):
            entries.append({"kind": "data", "text": f"[{key}]\n{_compact(payload[key])}"})
    summary = _get(payload.get("result"), "summary")
    if isinstance(summary, str) and not isinstance(answer, str):
        entries.append({"kind": "jarvis", "text": summary})
    task = payload.get("task")
    if isinstance(task, dict):
        entries.append({"kind": "system", "text": (
            f"{payload['command']}: {payload.get('run_status')} - task {task.get('id')} "
            f"{task.get('status')}/{task.get('stage')} via {task.get('selected_connector')}")})
    for issue in _get(payload.get("progress"), "open_issues") or []:
        if _get(issue, "kind") == "approval_required":
            continue  # the authorization card says it
        entries.append({"kind": "system", "text": f"Open issue: {_compact(issue, 600)}"})
    if payload.get("approval") is not None:
        entries.append({"kind": "approval", "text": "APPROVAL REQUIRED - review the authorization card."})
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
        entries.append({"kind": "system", "text": (
            f"Understood as {intent.get('capability')} -> {intent.get('destination')}"
            + (f": {intent.get('objective')}" if intent.get("objective") else ""))})
        # The run envelope itself is rendered by the caller's run path.
    elif status == "scheduled":
        job = payload.get("job") or {}
        entries.append({"kind": "scheduled", "text": (
            f"SCHEDULED: '{job.get('label')}' ({job.get('kind')}) as job {job.get('job_id')} - "
            f"next due {job.get('next_due_at_utc') or 'never'} (UTC). Nothing has run; the scheduler "
            "runs it and every approval still applies.")})
    elif status == "clarification_required":
        entries.append({"kind": "clarify", "text": str(intent.get("question") or "JARVIS needs one detail.")})
    else:
        template = INTENT_TEXT.get(status)
        message = str(intent.get("message") or "")
        kind = "error"
        if template is None:
            template, kind = "UNKNOWN INTENT STATUS [{status}]: {message}", "unknown"
        entries.append({"kind": kind, "text": template.format(status=status, message=message)})
    return entries


def approval_card(payload: Any) -> dict[str, Any]:
    """Exactly what is awaiting approval, from the persisted request only."""
    approval = _get(payload, "approval")
    if not isinstance(approval, dict):
        return {}
    scope = approval.get("scope") or {}
    effect = approval.get("side_effect") or {}
    return {
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
        "intent_status": str(_get(intent, "status") or ""),
        "capability": str(_get(intent, "capability") or ""),
        "destination": str(_get(intent, "destination") or ""),
    }


def outcome_kind(run: Any, intent_status: str = "") -> str:
    """The core-state word one envelope earns: completed / error / unknown / waiting / idle."""
    if intent_status and intent_status not in ("structured",):
        return "idle" if intent_status in ("scheduled", "clarification_required") else "error"
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
        entries = [{"kind": "system", "text": (
            f"Task {task.get('task_id')}\nstatus: {task.get('status')}  stage: {task.get('stage')}\n"
            f"capability: {task.get('capability')}  action: {task.get('action')}  connector: "
            f"{task.get('selected_connector')}  readable: {task.get('readable')}")}]
        attempt = payload.get("attempt")
        if isinstance(attempt, dict):
            state = attempt.get("state")
            note = {
                "unknown_outcome": " - OUTCOME UNKNOWN: never replayed; operator resolution required",
                "in_flight": " - IN FLIGHT: outcome not established yet",
                None: " - attempt state unreadable: treat as UNKNOWN",
            }.get(state, "")
            kind = "unknown" if state in ("unknown_outcome", "in_flight", None) else "system"
            entries.append({"kind": kind, "text": (
                f"attempt {attempt.get('id')}: {state}{note} (replayable: {attempt.get('replayable')})")})
        if payload.get("pending_approval") is not None:
            entries.append({"kind": "approval", "text": "Awaiting approval - resume this task to review the exact request."})
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
