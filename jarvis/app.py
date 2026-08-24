"""Composition root, untrusted task-input boundary and redacted presentation.

This module owns three things and nothing else:

* how a JARVIS application is assembled from settings, storage, registry and
  connectors (``create_app`` for injection, ``build_default_app`` for
  production);
* how untrusted task JSON becomes a ``Task``, using a default-deny allowlist;
* how an ``AgentRun`` becomes one deterministic, redacted JSON envelope.

It contains no Agent Loop policy. Permission, billing, fingerprint and
outbound-payload decisions stay in ``jarvis.core`` and the connectors.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Imported only because this module is the production composition root. The
# presentation redaction policy below is owned here, not by any connector.
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.base import BaseConnector
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.contracts import (
    LOCAL_READ_TASK_REFERENCE_KEY,
    ActionScope,
    AgentRun,
    BillingClass,
    BillingIntegrityError,
    CostEstimate,
    DataClassification,
    ErrorCode,
    LocalReadSnapshot,
    MemoryStore,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    ProjectMemoryItem,
    ProjectMemoryKind,
    ProjectRecord,
    ResultVerifier,
    RiskLevel,
    RunStatus,
    SessionState,
    StructuredError,
    Task,
    TaskStatus,
    ToolResult,
    canonical_usd,
    is_expired,
    local_read_snapshot_refusal,
    next_loop_stage,
    normalize_destination,
    require_billing_class,
    validate_project_id,
)
from jarvis.core import AgentCore
from jarvis.memory import SQLiteMemoryStore
from jarvis.planning import build_task_plan
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings


#: Version of the non-status JSON envelope, independent of the SQLite schema.
#: Version 2 added the deterministic, redacted top-level ``progress`` object.
#: Version 3 (M3) added the bounded top-level ``local_read`` object and the
#: ``session.project_id`` field that M1 added without a bump. The SQLite
#: schema is a separate contract and stays at its own version.
OUTPUT_SCHEMA_VERSION = 3

#: Untrusted task JSON is bounded before it is parsed.
MAX_TASK_JSON_BYTES = 256 * 1024

#: The only outbound endpoint a task may name in this build. Task input can
#: confirm it but never redirect it.
ALLOWED_DESTINATION = "anthropic/claude-code"

REDACTED = "[REDACTED]"

EXIT_OK = 0
EXIT_AWAITING_APPROVAL = 10
EXIT_AWAITING_CLARIFICATION = 11
EXIT_REJECTED = 20
EXIT_CANCELLED = 21
EXIT_INVALID_INPUT = 64
EXIT_NOT_FOUND = 66
EXIT_INTERNAL = 70
EXIT_POLICY = 77


_RUN_STATUS_EXIT = {
    RunStatus.COMPLETED: EXIT_OK,
    RunStatus.AWAITING_APPROVAL: EXIT_AWAITING_APPROVAL,
    RunStatus.AWAITING_COST_OVERRIDE: EXIT_AWAITING_APPROVAL,
    RunStatus.AWAITING_CLARIFICATION: EXIT_AWAITING_CLARIFICATION,
    RunStatus.REJECTED: EXIT_REJECTED,
    RunStatus.CANCELLING: EXIT_CANCELLED,
    RunStatus.CANCELLED: EXIT_CANCELLED,
    RunStatus.FAILED: EXIT_INTERNAL,
}

_ERROR_EXIT = {
    ErrorCode.INVALID_TASK: EXIT_INVALID_INPUT,
    ErrorCode.NO_CONNECTOR: EXIT_INVALID_INPUT,
    ErrorCode.UNKNOWN_SESSION: EXIT_NOT_FOUND,
    ErrorCode.TASK_NOT_FOUND: EXIT_NOT_FOUND,
    ErrorCode.INVALID_GRANT: EXIT_POLICY,
    ErrorCode.STALE_GRANT: EXIT_POLICY,
    # Reserved codes (H5): currently produced by no code path, but kept in
    # the persisted ErrorCode contract and mapped here so every member has a
    # stable exit code. The live conditions surface as approval gates.
    ErrorCode.COST_CAP_EXCEEDED: EXIT_POLICY,
    ErrorCode.PAID_API_DISABLED: EXIT_POLICY,
    ErrorCode.BILLING_ROUTE_UNKNOWN: EXIT_POLICY,
    ErrorCode.BILLING_INTEGRITY_FAILED: EXIT_POLICY,
    ErrorCode.CONNECTOR_AMBIGUOUS: EXIT_POLICY,
    ErrorCode.EGRESS_REFUSED: EXIT_POLICY,
    ErrorCode.EXECUTION_UNKNOWN_OUTCOME: EXIT_POLICY,
    ErrorCode.EXECUTION_IN_PROGRESS: EXIT_POLICY,
    # Reserved (H5): currently unproduced; see the contract note.
    ErrorCode.TASK_ALREADY_TERMINAL: EXIT_POLICY,
    ErrorCode.APPROVAL_REJECTED: EXIT_REJECTED,
    ErrorCode.CANCELLED: EXIT_CANCELLED,
    ErrorCode.CONNECTOR_UNAVAILABLE: EXIT_INTERNAL,
    ErrorCode.CONNECTOR_FAILED: EXIT_INTERNAL,
    ErrorCode.VERIFICATION_FAILED: EXIT_INTERNAL,
}


# --------------------------------------------------------------------------
# Untrusted task input
# --------------------------------------------------------------------------

#: Default-deny allowlists. Anything absent here - protected core fields,
#: billing markers, approval state, fingerprints or future preference keys -
#: is rejected rather than ignored.
_TASK_KEYS = frozenset(
    {"objective", "capability", "scope", "context", "preferences", "project_id"}
)
_SCOPE_KEYS = frozenset({"action", "target", "destination", "data_classification"})
#: ``local_read_task_id`` (M3) is a *reference* to an earlier completed local
#: read in the same session. Content is never accepted from input: the
#: snapshot is always re-derived from the referenced task's persisted state
#: and re-validated in ``AgentCore``.
_CONTEXT_KEYS = frozenset(
    {"working_directory", "inputs", LOCAL_READ_TASK_REFERENCE_KEY}
)
_PREFERENCE_KEYS = frozenset(
    {"preferred_connector", "needs_clarification", "clarification_question", "assumption"}
)


class TaskInputError(ValueError):
    """Untrusted task input was refused at the boundary."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise TaskInputError(f"Duplicate JSON key: {key}")
        document[key] = value
    return document


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise TaskInputError(f"Non-finite number is not accepted: {text}")
    return value


def _reject_constant(text: str) -> float:
    raise TaskInputError(f"Non-finite number is not accepted: {text}")


def _load_document(raw: str | bytes) -> dict[str, Any]:
    payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(payload) > MAX_TASK_JSON_BYTES:
        raise TaskInputError(
            f"Task JSON is larger than the {MAX_TASK_JSON_BYTES} byte limit."
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskInputError("Task JSON must be UTF-8 encoded.") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except TaskInputError:
        # A boundary rule already produced a precise message; keep it.
        raise
    except json.JSONDecodeError as exc:
        raise TaskInputError(f"Task JSON is not valid JSON: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        # The parser itself can refuse input - the integer digit limit and the
        # nesting depth limit both land here. That is invalid input, not an
        # internal failure, so it must not surface as a connector error.
        raise TaskInputError(
            f"Task JSON could not be parsed: {type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise TaskInputError("Task JSON root must be an object.")
    return document


def _reject_unknown(mapping: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise TaskInputError(
            f"Rejected field(s) in '{where}': {', '.join(unknown)}"
        )


def _section(document: dict[str, Any], key: str, allowed: frozenset[str]) -> dict[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise TaskInputError(f"'{key}' must be an object.")
    _reject_unknown(value, allowed, key)
    return value


def _string(
    mapping: dict[str, Any], key: str, where: str, *, required: bool = False
) -> str:
    if key not in mapping:
        if required:
            raise TaskInputError(f"'{where}.{key}' is required.")
        return ""
    value = mapping[key]
    if not isinstance(value, str):
        raise TaskInputError(f"'{where}.{key}' must be a string.")
    if required and not value.strip():
        raise TaskInputError(f"'{where}.{key}' must not be empty.")
    if chr(0) in value:
        # A NUL terminates a string at the OS boundary and names nothing on
        # any platform. Refused at the input boundary so it can never become
        # a persisted target, fingerprint input or connector argument; the
        # loop refuses it independently for programmatically built tasks.
        raise TaskInputError(f"'{where}.{key}' must not contain a NUL character.")
    return value


def decode_task_json(raw: str | bytes) -> Task:
    """Build a fresh ``Task`` from untrusted JSON, field by field.

    ``Task.from_dict`` is deliberately not used: it trusts every key, which
    would let input carry its own id, stage, estimate, approval markers or
    risk profile into the loop.
    """
    document = _load_document(raw)
    _reject_unknown(document, _TASK_KEYS, "task")

    objective = _string(document, "objective", "task", required=True)
    capability = _string(document, "capability", "task", required=True)

    scope_input = _section(document, "scope", _SCOPE_KEYS)
    action = _string(scope_input, "action", "scope", required=True)
    target = _string(scope_input, "target", "scope")
    destination = _string(scope_input, "destination", "scope")
    if destination.strip() and normalize_destination(destination) != ALLOWED_DESTINATION:
        raise TaskInputError(
            f"'scope.destination' must be '{ALLOWED_DESTINATION}'."
        )
    classification_text = (
        _string(scope_input, "data_classification", "scope")
        or DataClassification.INTERNAL.value
    )
    try:
        classification = DataClassification(classification_text)
    except ValueError as exc:
        raise TaskInputError(
            f"Unsupported 'scope.data_classification': {classification_text}"
        ) from exc

    context_input = _section(document, "context", _CONTEXT_KEYS)
    context: dict[str, Any] = {}
    working_directory = _string(context_input, "working_directory", "context")
    if working_directory.strip():
        context["working_directory"] = working_directory
    if "inputs" in context_input:
        # Opaque user content. It is never read as a control namespace here;
        # the connector redacts it before anything can leave the machine.
        context["inputs"] = context_input["inputs"]
    # M3: shape-validated here only. Same-session membership, COMPLETED
    # source status, snapshot validity and the classification ladder are all
    # proven later against storage, where any failure fails the task closed.
    local_read_reference = _string(
        context_input, LOCAL_READ_TASK_REFERENCE_KEY, "context"
    )
    if local_read_reference.strip():
        context[LOCAL_READ_TASK_REFERENCE_KEY] = local_read_reference.strip()

    preference_input = _section(document, "preferences", _PREFERENCE_KEYS)
    preferences: dict[str, Any] = {}
    preferred_connector = _string(preference_input, "preferred_connector", "preferences")
    if preferred_connector.strip():
        preferences["preferred_connector"] = preferred_connector
    if "needs_clarification" in preference_input:
        needs_clarification = preference_input["needs_clarification"]
        if not isinstance(needs_clarification, bool):
            raise TaskInputError("'preferences.needs_clarification' must be a boolean.")
        preferences["needs_clarification"] = needs_clarification
    question = _string(preference_input, "clarification_question", "preferences")
    if question.strip():
        preferences["clarification_question"] = question
    assumption = _string(preference_input, "assumption", "preferences")
    if assumption.strip():
        preferences["assumption"] = assumption

    # M1: an optional project reference. Format-validated here; existence and
    # active state are proven later against storage, where an unknown project
    # fails the task closed. Never auto-created.
    project_id: str | None = None
    project_id_text = _string(document, "project_id", "task")
    if project_id_text.strip():
        try:
            project_id = validate_project_id(project_id_text.strip())
        except ValueError as exc:
            raise TaskInputError(str(exc)) from exc

    # Everything else - id, timestamps, status, stage, estimate, risk and side
    # effect - comes from the dataclass defaults and stays connector-owned.
    return Task(
        objective=objective,
        capability=capability,
        scope=ActionScope(
            action=action,
            target=target,
            destination=ALLOWED_DESTINATION,
            data_classification=classification,
        ),
        context=context,
        preferences=preferences,
        project_id=project_id,
    )


# --------------------------------------------------------------------------
# Redacted presentation
# --------------------------------------------------------------------------


#: Secret-key vocabulary, used both for field names and for labels embedded in
#: free-form text. Owned by this module: presentation redaction is a CLI
#: concern and must not depend on any connector's internal helpers.
#: Compound key names first: ``_`` is a word character, so the trailing
#: ``\b`` on the bare terms means ``secret_access_key:`` never matched
#: ``secret``. Kept byte-for-byte in step with the contracts policy so the
#: presentation defence is never the weaker of the two.
_SECRET_LABEL = (
    r"secret[_\-\s]?access[_\-\s]?key(?:[_\-\s]?id)?"
    r"|access[_\-\s]?key[_\-\s]?id|access[_\-\s]?key|secret[_\-\s]?key"
    r"|api[_\-\s]?secret"
    r"|api[_\-\s]?key|token|secret|password|passwd|authorization|cookie"
    r"|private[_\-\s]?key"
)

_SECRET_KEY_PATTERN = re.compile(_SECRET_LABEL, re.IGNORECASE)

#: ``password=hunter2``, ``API_KEY: abc123``, ``authorization: Basic x`` and
#: the like. The value runs to the end of the field so a space-separated
#: credential cannot survive; over-redaction is the safe direction here.
#: The leading boundary is a negative alphanumeric lookbehind rather than
#: ``\b`` so identifier separators still expose the sensitive term
#: (``access_token=``, ``client-secret:``) while a term embedded in an
#: ordinary alphanumeric word (``mytoken=``) stays untouched.
_LABELLED_SECRET_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])({_SECRET_LABEL})(\s*[:=]\s*)([^,;\n]+)", re.IGNORECASE
)

#: Credential shapes that carry no label of their own.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{16}\b"
    ),
)


def _redact_text(text: str) -> str:
    # Shape patterns run first: an "authorization: Bearer <token>" header is
    # then already reduced, so the label rule cannot stop at "Bearer" and
    # leave the token behind.
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _LABELLED_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text
    )


def _redact(value: Any, key: str = "") -> Any:
    """Conservatively redact strings that reach the output envelope.

    Key-based redaction is applied to string values only: numeric telemetry
    such as ``input_tokens`` must survive a key pattern that matches "token".
    """
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if isinstance(value, str):
        if key and _SECRET_KEY_PATTERN.search(key):
            return REDACTED
        return _redact_text(value)
    return value


def _approval_payload(request: PermissionRequest) -> dict[str, Any]:
    return {
        "request_id": request.id,
        "fingerprint": request.fingerprint,
        "scope_fingerprint": request.scope_fingerprint,
        "expires_at": request.expires_at,
        "scope": request.scope.to_dict(),
        "risk_level": int(request.risk_level),
        "side_effect": {
            "effect": request.side_effect.effect.value,
            "reversible": request.side_effect.reversible,
            "always_requires_approval": request.side_effect.always_requires_approval,
        },
        "reason": request.reason,
        # The connector's own redacted preview is the only representation of
        # outbound data; the payload and prompt themselves never appear.
        "outbound_preview": (
            request.outbound_preview.to_dict() if request.outbound_preview else None
        ),
        # The safe egress/resource manifest bound into the fingerprint: the
        # outbound digest, gate state, connector/destination/classification,
        # the resolved working directory, the approved allowed roots and the
        # exact external tool grants. Digests and bounded paths only - no raw
        # prompt, credentials or retrieved-memory internals. What is shown
        # here corresponds exactly to the execution-time attestation state.
        "attestation": (
            request.attestation.display_manifest()
            if request.attestation is not None
            else None
        ),
    }


def _finite_usd(value: Any) -> float | None:
    """Envelope-safe USD amount; anything invalid is reported as ``None``.

    The envelope is rendered with ``allow_nan=False``, so a malformed estimate
    that reached presentation must degrade to a null rather than crash the CLI
    or print a non-JSON number.
    """
    if value is None:
        return None
    try:
        return float(canonical_usd(value))
    except BillingIntegrityError:
        return None


def _billing_class_value(estimate: CostEstimate) -> str:
    """Envelope-safe billing class; anything unvalidated reads as unknown.

    Presentation never dereferences an enum-specific attribute on a value the
    monetary boundary has not already validated.
    """
    try:
        return require_billing_class(estimate.billing_class).value
    except BillingIntegrityError:
        return BillingClass.UNKNOWN.value


def _billing_payload(estimate: CostEstimate) -> dict[str, Any]:
    return {
        "class": _billing_class_value(estimate),
        "monetary_maximum_usd": _finite_usd(estimate.monetary_maximum_usd),
        "resource_ceiling_usd": _finite_usd(estimate.resource_ceiling_usd),
    }


def _progress_payload(task: Task, session: SessionState | None) -> dict[str, Any]:
    """Deterministic, informational progress view of the addressed task.

    ``plan`` is always the safe canonical rebuild — the persisted, possibly
    tampered ``task.plan`` strings are never printed and its emptiness is the
    only thing read. ``next_stage`` is the informational linear successor; it
    never implies the successor may run past an open blocker.
    """
    successor = next_loop_stage(task.stage)
    payload: dict[str, Any] = {
        "task_id": task.id,
        "plan": build_task_plan(task) if task.plan else [],
        "current_stage": task.stage.value,
        "next_stage": successor.value if successor is not None else None,
        "post_completion_next_step": (
            session.next_step
            if session is not None and task.status is TaskStatus.COMPLETED
            else None
        ),
    }
    payload.update(task.progress.to_dict())
    return payload


def _safe_local_read_shape(snapshot: Any) -> bool:
    """Whether this snapshot is safe to render into the public envelope.

    P1-5: this asks the ONE canonical structural contract,
    ``local_read_snapshot_refusal``, rather than restating it. A task row
    edited outside the product can carry an absolute path, a traversal path,
    an unbounded scalar, a malformed digest or a raw-string classification
    into memory; the load boundary, the execution boundary, this envelope and
    the Claude egress boundary now all judge that row by the same function,
    so the boundary the *user* reads can never be the weaker one.

    Masking would not be enough: printing ``[PATH]`` in place of a tampered
    absolute path would still assert that a bounded, trustworthy read
    happened. A snapshot that fails the contract produces NO block at all.
    """
    return local_read_snapshot_refusal(snapshot) is None


def _local_read_payload(snapshot: LocalReadSnapshot) -> dict[str, Any] | None:
    """The bounded local-read view of the addressed task, or ``None``.

    Exactly the fields a user needs to see what was read and how far it was
    bounded. The excerpt is already newline-normalized, credential- and
    absolute-path-redacted and capped by ``bound_local_text``; the envelope's
    own ``_redact`` pass then runs over it like every other string.

    ``None`` means the snapshot did not survive ``_safe_local_read_shape``, so
    the envelope shows no local-read block rather than a tampered one.
    """
    if not _safe_local_read_shape(snapshot):
        return None
    return {
        "relative_path": snapshot.relative_path,
        "byte_size": snapshot.byte_size,
        "sha256": snapshot.sha256,
        "classification": snapshot.classification.value,
        "truncated": snapshot.truncated,
        "excerpt_characters": len(snapshot.excerpt),
        "excerpt": snapshot.excerpt,
        "read_at": snapshot.read_at,
    }


def _result_payload(result: ToolResult) -> dict[str, Any]:
    usage = result.usage
    return {
        "summary": result.summary,
        "usage": {
            "provider": usage.provider,
            "model": usage.model,
            "connector": usage.connector,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "call_count": usage.call_count,
            "billing_class": usage.billing_class.value,
            "monetary_state": usage.monetary_state.value,
            "monetary_cost_usd": _finite_usd(usage.monetary_cost_usd),
            "reference_cost_usd": _finite_usd(usage.reference_cost_usd),
            "billing_evidence": usage.billing_evidence,
        },
        "verification": {
            "status": result.verification.status.value,
            "method": result.verification.method,
            "evidence": list(result.verification.evidence),
        },
    }


def build_envelope(
    command: str,
    permission_mode: PermissionMode,
    run: AgentRun | None = None,
    error: StructuredError | None = None,
) -> dict[str, Any]:
    """One fixed-key, redacted envelope for every non-status command."""
    session = run.session if run is not None else None
    task = run.task if run is not None else None
    request = run.permission_request if run is not None else None
    result = run.result if run is not None else None
    failure = error if error is not None else (run.error if run is not None else None)
    payload = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "command": command,
        "effective_permission_mode": permission_mode.value,
        "run_status": run.status.value if run is not None else None,
        "session": (
            None
            if session is None
            else {
                "id": session.id,
                "active_task_id": session.active_task_id,
                "next_step": session.next_step,
                "project_id": session.project_id,
            }
        ),
        "task": (
            None
            if task is None
            else {
                "id": task.id,
                "status": task.status.value,
                "stage": task.stage.value,
                "selected_connector": task.selected_connector,
            }
        ),
        "approval": None if request is None else _approval_payload(request),
        "billing": None if task is None else _billing_payload(task.estimate),
        "progress": None if task is None else _progress_payload(task, session),
        # ``_local_read_payload`` re-validates the snapshot and returns None
        # for anything it cannot vouch for, so the presence check lives there.
        "local_read": (None if task is None else _local_read_payload(task.local_read)),
        "result": None if result is None else _result_payload(result),
        # ``details`` is deliberately dropped: it carries free-form diagnostics.
        "error": (
            None
            if failure is None
            else {
                "code": failure.code.value,
                "message": failure.message,
                "retryable": failure.retryable,
            }
        ),
    }
    return _redact(payload)


@dataclass(frozen=True, slots=True)
class CommandResult:
    payload: dict[str, Any]
    exit_code: int

    def render(self) -> str:
        return json.dumps(
            self.payload,
            sort_keys=True,
            indent=2,
            allow_nan=False,
            ensure_ascii=True,
        )


def failure_result(
    command: str,
    permission_mode: PermissionMode,
    code: ErrorCode,
    message: str,
    exit_code: int,
) -> CommandResult:
    return CommandResult(
        build_envelope(command, permission_mode, error=StructuredError(code, message)),
        exit_code,
    )


def status_payload(settings: Settings, memory: SQLiteMemoryStore) -> dict[str, Any]:
    """Local-only status. No connector is constructed, probed or selected."""
    return {
        "mode": "local",
        "schema_version": memory.schema_version(),
        "permission_mode": settings.permission_mode.value,
        "monthly_hard_limit_usd": settings.monthly_hard_limit_usd,
        "paid_api_enabled": settings.paid_api_enabled,
    }


# --------------------------------------------------------------------------
# Project memory commands (M1) - local-only, deterministic, no connector,
# no model call. Persisted memory is untrusted reference data and is never
# approval, billing or lifecycle authority.
# --------------------------------------------------------------------------


def _memory_item_payload(item: ProjectMemoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "kind": item.kind.value,
        "classification": item.classification.value,
        # Exactly what was persisted: already normalized, bounded and passed
        # through the canonical credential/path hygiene at the write boundary.
        "text": item.text,
        "active": item.active,
        "superseded_by": item.superseded_by,
        "created_at": item.created_at,
    }


def project_create_result(
    memory: SQLiteMemoryStore, settings: Settings, project_id: str, name: str
) -> CommandResult:
    command = "project-create"
    try:
        project = ProjectRecord(id=validate_project_id(project_id), name=name)
        memory.save_project(project)
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    return CommandResult(
        {"command": command, "project": project.to_dict()}, EXIT_OK
    )


def memory_add_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    project_id: str,
    kind: str,
    classification: str,
    text: str,
    supersedes: str | None,
) -> CommandResult:
    command = "memory-add"
    try:
        # Kind and classification are explicit user input; an unknown value
        # fails closed here and is never defaulted or downgraded.
        item = ProjectMemoryItem(
            project_id=validate_project_id(project_id),
            kind=ProjectMemoryKind(kind),
            text=text,
            classification=DataClassification(classification),
        )
        stored = memory.save_memory_item(item, supersedes=supersedes)
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    return CommandResult(
        {
            "command": command,
            "item": _memory_item_payload(stored),
            "superseded": supersedes,
        },
        EXIT_OK,
    )


def memory_list_result(
    memory: SQLiteMemoryStore, settings: Settings, project_id: str
) -> CommandResult:
    command = "memory-list"
    try:
        valid_id = validate_project_id(project_id)
        if memory.load_project(valid_id) is None:
            raise ValueError(f"Unknown project: {valid_id}")
        items = memory.list_memory_items(valid_id)
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    return CommandResult(
        {
            "command": command,
            "project_id": valid_id,
            "items": [_memory_item_payload(item) for item in items],
        },
        EXIT_OK,
    )


# --------------------------------------------------------------------------
# Command facade
# --------------------------------------------------------------------------


class JarvisApp:
    """Thin command facade over ``AgentCore``; it holds no loop policy."""

    def __init__(
        self, core: AgentCore, memory: MemoryStore, settings: Settings
    ) -> None:
        self.core = core
        self.memory = memory
        self.settings = settings

    @property
    def permission_mode(self) -> PermissionMode:
        return self.settings.permission_mode

    def run(self, raw_task_json: str | bytes, session_id: str | None = None) -> CommandResult:
        try:
            task = decode_task_json(raw_task_json)
        except TaskInputError as exc:
            return self._failure(
                "run", ErrorCode.INVALID_TASK, str(exc), EXIT_INVALID_INPUT
            )
        # No grant is ever accepted here: a first run cannot approve itself.
        return self._present("run", self.core.run(task, session_id))

    def resume(self, session_id: str, task_id: str) -> CommandResult:
        return self._present("resume", self.core.resume(session_id, task_id))

    def clarify(self, session_id: str, task_id: str, text: str) -> CommandResult:
        if not text.strip():
            return self._failure(
                "clarify",
                ErrorCode.INVALID_TASK,
                "Clarification text must not be empty.",
                EXIT_INVALID_INPUT,
            )
        # provide_clarification() mutates the task, so ownership is proven
        # before it is called rather than after.
        owned = self._ownership_failure("clarify", session_id, task_id)
        if owned is not None:
            return owned
        return self._present(
            "clarify", self.core.provide_clarification(session_id, task_id, text)
        )

    def approve(
        self,
        session_id: str,
        task_id: str,
        request_id: str,
        fingerprint: str,
        remember_for_session: bool = False,
    ) -> CommandResult:
        return self._decide(
            "approve",
            session_id,
            task_id,
            request_id,
            fingerprint,
            approved=True,
            remember_for_session=remember_for_session,
        )

    def reject(
        self, session_id: str, task_id: str, request_id: str, fingerprint: str
    ) -> CommandResult:
        return self._decide(
            "reject",
            session_id,
            task_id,
            request_id,
            fingerprint,
            approved=False,
            remember_for_session=False,
        )

    def cancel(self, session_id: str, task_id: str) -> CommandResult:
        owned = self._ownership_failure("cancel", session_id, task_id)
        if owned is not None:
            return owned
        return self._present("cancel", self.core.cancel(session_id, task_id))

    def _decide(
        self,
        command: str,
        session_id: str,
        task_id: str,
        request_id: str,
        fingerprint: str,
        *,
        approved: bool,
        remember_for_session: bool,
    ) -> CommandResult:
        owned = self._ownership_failure(command, session_id, task_id)
        if owned is not None:
            return owned
        request = self.memory.load_pending_approval(task_id, session_id)
        if request is None:
            return self._failure(
                command,
                ErrorCode.TASK_NOT_FOUND,
                "No pending approval request exists for this task and session.",
                EXIT_NOT_FOUND,
            )
        # The persisted request is the only source of truth. A mismatched or
        # expired one is refused, never recomputed or replaced.
        if request.id != request_id or request.fingerprint != fingerprint:
            return self._failure(
                command,
                ErrorCode.INVALID_GRANT,
                "The supplied request id or fingerprint does not match the "
                "persisted approval request.",
                EXIT_POLICY,
            )
        if is_expired(request.expires_at):
            return self._failure(
                command,
                ErrorCode.STALE_GRANT,
                "The persisted approval request has expired.",
                EXIT_POLICY,
            )
        if remember_for_session and not self._remembrance_allowed(request):
            return self._failure(
                command,
                ErrorCode.INVALID_GRANT,
                "This approval may not be remembered for the session.",
                EXIT_POLICY,
            )
        grant = PermissionGrant.for_request(
            request,
            approved=approved,
            remember_for_session=remember_for_session,
        )
        return self._present(command, self.core.resume(session_id, task_id, grant))

    def _remembrance_allowed(self, request: PermissionRequest) -> bool:
        """The same reuse conditions the loop itself applies."""
        return (
            self.settings.permission_mode is PermissionMode.SESSION
            and not request.side_effect.mandatory
            and request.risk_level < RiskLevel.HIGH
            and request.side_effect.reversible
        )

    def _ownership_failure(
        self, command: str, session_id: str, task_id: str
    ) -> CommandResult | None:
        if self.memory.load_session(session_id) is None:
            return self._failure(
                command,
                ErrorCode.UNKNOWN_SESSION,
                f"Unknown session: {session_id}",
                EXIT_NOT_FOUND,
            )
        if (
            self.memory.load_task(task_id) is None
            or self.memory.task_session_id(task_id) != session_id
        ):
            return self._failure(
                command,
                ErrorCode.TASK_NOT_FOUND,
                f"Unknown task for this session: {task_id}",
                EXIT_NOT_FOUND,
            )
        return None

    def _present(self, command: str, run: AgentRun) -> CommandResult:
        return CommandResult(
            build_envelope(command, self.settings.permission_mode, run),
            self._exit_code(run),
        )

    def _failure(
        self, command: str, code: ErrorCode, message: str, exit_code: int
    ) -> CommandResult:
        return failure_result(
            command, self.settings.permission_mode, code, message, exit_code
        )

    @staticmethod
    def _exit_code(run: AgentRun) -> int:
        if run.error is not None:
            return _ERROR_EXIT.get(run.error.code, EXIT_INTERNAL)
        return _RUN_STATUS_EXIT.get(run.status, EXIT_INTERNAL)


# --------------------------------------------------------------------------
# Composition root
# --------------------------------------------------------------------------


def create_app(
    *,
    settings: Settings | None = None,
    memory: MemoryStore | None = None,
    registry: ToolRegistry | None = None,
    connectors: Iterable[BaseConnector] = (),
    verifier: ResultVerifier | None = None,
) -> JarvisApp:
    """Assemble an application from injected parts. Tests use this."""
    settings = settings or Settings()
    memory = memory if memory is not None else SQLiteMemoryStore(settings.memory_database)
    registry = registry if registry is not None else ToolRegistry()
    for connector in connectors:
        registry.register(connector)
    return JarvisApp(AgentCore(registry, memory, settings, verifier), memory, settings)


def build_default_app(settings: Settings | None = None) -> JarvisApp:
    """Production composition. Constructing a connector starts no process."""
    settings = settings or Settings()
    startup_directory = Path.cwd().resolve()
    connector = ClaudeCodeConnector(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
        max_budget_usd=settings.claude_call_budget_usd,
    )
    # M3: the same startup root, so a local read and a Claude plan are bound
    # to exactly one boundary. Constructing it touches no filesystem.
    local_reader = LocalFileReadConnector(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
    )
    return create_app(settings=settings, connectors=(connector, local_reader))
