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

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import AbstractSet, Any, Callable, Iterable, Mapping
from uuid import uuid4

from jarvis.connectors.base import BaseConnector

# Imported only because this module is the production composition root. The
# presentation redaction policy below is owned here, not by any connector.
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.connectors.github_cli import GitHubCliConnector
from jarvis.connectors.browser_cdp import BrowserCdpConnector
from jarvis.connectors.desktop_uia import DesktopUiaConnector
from jarvis.connectors.gmail_cli import GmailCliConnector
from jarvis.connectors.local_files import (
    LocalFileReadConnector,
    LocalFileWriteConnector,
)
from jarvis.connectors.project_folder import (
    PROJECT_FOLDER_CAPABILITY,
    PROJECT_FOLDER_CONNECTOR,
    PROJECT_FOLDER_CONTEXT_KEY,
    PROJECT_FOLDER_TARGETS,
    ProjectFolderCreateConnector,
)
from jarvis import project_files
from jarvis import project_folders
from jarvis.contracts import (
    MemoryRecordOrigin,
    utc_now,
    PROJECT_INSTRUCTION_KEY,
    project_instruction_block,
    project_instruction_delivery,
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_CONTEXT_REFERENCE_KEY,
    LOCAL_READ_MAX_REGION_LINES,
    LOCAL_READ_MAX_SOURCES,
    LOCAL_READ_MAX_START_LINE,
    LOCAL_READ_REGION_SECTION,
    LOCAL_READ_TASK_REFERENCE_KEY,
    LOCAL_WRITE_CAPABILITY,
    LOCAL_WRITE_MODES,
    LOCAL_WRITE_SOURCE_KEY,
    VISIBILITY_DECODE_ERRORS,
    VISIBILITY_DEFAULT_LIMIT,
    VISIBILITY_MAX_LIMIT,
    ActionScope,
    AgentRun,
    ApprovalSummary,
    AttemptRecovery,
    AttemptRecoveryOutcome,
    AttemptState,
    ConditionError,
    JobError,
    JobSpec,
    JobSummary,
    ScheduleError,
    AttemptSummary,
    BillingClass,
    BillingIntegrityError,
    ChainState,
    ChainStatus,
    ChainStep,
    CostEstimate,
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordScope,
    MemoryRecordStatus,
    normalize_memory_subject_key,
    redact_credentials,
    redact_local_paths,
    ErrorCode,
    LocalReadContextKind,
    LocalReadKind,
    LocalReadSnapshot,
    LocalWriteRecord,
    MEMORY_ORIGIN_DETAIL_CAP,
    MODEL_ANSWER_RESULT_KEY,
    MemoryStore,
    ModelAdapter,
    ModelAnswer,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    ProjectMemoryItem,
    ProjectRecord,
    ResultVerifier,
    RunStatus,
    SessionState,
    SessionSummary,
    StructuredError,
    Task,
    TaskStatus,
    TaskSummary,
    ToolResult,
    WriteSource,
    canonical_usd,
    chain_declaration_refusal,
    chain_state_refusal,
    chain_steps_from_declaration,
    is_expired,
    local_read_reference_refusal,
    local_read_region_refusal,
    local_read_snapshot_refusal,
    local_write_content_refusal,
    local_write_mode_unsupported,
    local_write_record_refusal,
    local_write_source_refusal,
    model_answer_refusal,
    next_loop_stage,
    require_billing_class,
    sanitize_diagnostic_text,
    validate_project_id,
)
from jarvis.context_items import (
    DEFAULT_RESPONSE_MODE,
    context_window,
    image_files,
)
from jarvis.context_items import response_mode as items_response_mode
from jarvis.core import (
    AgentCore,
    CaptureStop,
    MemoryCaptureOutcome,
    capture_gate_open,
    _capture_run_connection,
    _claim_capture_run,
    _finish_capture_run,
    _validated_capture_candidate,
)
from jarvis import memory_capture, memory_import
from jarvis.memory_guard import Provenance, RecordGuard, SessionTaint, content_hash
from jarvis.memory_import_plan import ImportPlan, plan_import
from jarvis.dispatch import DISPATCH_REFUSED, cancel_job_run
from jarvis.destinations import (
    CONNECTOR_DESTINATION_POLICY,
    DEFAULT_TASK_DESTINATION,
    DestinationGrant,
    canonical_destination,
    destination_refusal,
)
from jarvis.desktop import (
    DESKTOP_CAPABILITIES,
    DESKTOP_CONNECTOR,
    DESKTOP_ENABLED_CAPABILITIES,
    DesktopExecutionUnavailable,
    desktop_external_read_refusal,
    desktop_resource_binding_refusal,
    desktop_resource_from_dict,
)
from jarvis.browser import (
    BROWSER_CAPABILITIES,
    BROWSER_CONNECTOR,
    BROWSER_ENABLED_CAPABILITIES,
    BrowserExecutionUnavailable,
    browser_external_read_refusal,
    browser_resource_binding_refusal,
    browser_resource_from_dict,
)
from jarvis.gmail import (
    GMAIL_CAPABILITIES,
    GMAIL_CONNECTOR,
    GMAIL_ENABLED_CAPABILITIES,
    GmailCredentialAttestation,
    GmailExecutionUnavailable,
    GmailIdentityBinding,
    gmail_external_read_refusal,
    gmail_resource_binding_refusal,
    gmail_resource_from_dict,
)
from jarvis.github import (
    GITHUB_CONNECTOR,
    ExternalReadSnapshot,
    GITHUB_READ_CAPABILITIES,
    GitHubExecutionUnavailable,
    GitHubIdentityBinding,
    github_resource_binding_refusal,
    github_resource_from_dict,
    external_read_refusal,
)
from jarvis.intent import (
    CHAT_REPLY_TOO_LONG,
    INTENT_ACTION,
    INTENT_CAPABILITY,
    ClaudeIntentAdapter,
    ClaudeResearchAdapter,
    IntentStatus,
    ProposalError,
    build_catalog,
    intent_text_refusal,
    model_failure_status,
    parse_proposal,
    parse_research,
    model_failure_reason,
    resolve_proposal,
    response_language_hint,
    too_long_reply,
)
from jarvis.context_usage import context_usage
from jarvis.conversation import (
    COMPACT_KEEP_LAST,
    RAW_TURNS_MAX,
    ConversationState,
    PersistedConversation,
    argv_chars,
    assemble_context,
    compact_now,
    context_fields,
    context_profile_of,
    json_text,
    register_precompact_hook,
    rehydrate,
)
from jarvis.memory import _RECORD_CURRENT_SQL, MemoryRecordApproval, MemoryRecordRefused, SQLiteMemoryStore
from jarvis.memory_doctor import Finding, RepairAction, RepairProposal, diagnose
from jarvis.memory_filter import classify_candidate_text
from jarvis.planning import build_task_plan
from jarvis.registry import ToolRegistry
from jarvis.scheduler_runner import SchedulerRunner
from jarvis.settings import (
    CONFIG_VERSION,
    MONTHLY_HARD_LIMIT_USD,
    ConfigError,
    ConfigStore,
    Settings,
)
from jarvis.skill_draft import GLOBAL_SUPPRESSION, parse_user_edited_draft
from jarvis.skill_lifecycle import (
    CHANGEABLE_FIELDS,
    SkillEvent,
    SkillHistory,
    active_skill,
    active_version,
    health,
    next_version,
    rollback_event,
    status,
)
from jarvis.skill_store import SkillStore, SkillStoreError
from jarvis.skill_types import (
    Skill,
    SkillDraft,
    SkillEventKind,
    SkillHealth,
    SkillOrigin,
    SkillProvenance,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
)

#: Version of the non-status JSON envelope, independent of the SQLite schema.
#: Version 2 added the deterministic, redacted top-level ``progress`` object.
#: Version 3 (M3) added the bounded top-level ``local_read`` object and the
#: ``session.project_id`` field that M1 added without a bump.
#: Version 4 (M5) added the bounded top-level ``local_write`` object.
#: Version 5 (M6) added the bounded top-level ``model_answer`` object.
#: Version 6 (M11) added the top-level ``chain`` object: the session's inert
#: sequential declaration, presentation only. The SQLite schema is a separate
#: contract and stays at its own version.
#: Version 7 (GitHub v1) added the bounded top-level ``external_read`` object:
#: the untrusted content one authorized GitHub read returned. A consumer must
#: be able to tell a build that can carry inbound external content from one
#: that cannot, which is what this field is for - so the block is a bump
#: rather than a silent key. The SQLite schema is untouched by it and stays
#: at 10.
#: Gmail minimum (no bump): the same ``external_read`` block now also carries
#: one authorized Gmail read, with ``connector`` naming ``gmail-cli``; the
#: shape of the block is unchanged, so the version is not.
OUTPUT_SCHEMA_VERSION = 7

#: Untrusted task JSON is bounded before it is parsed.
MAX_TASK_JSON_BYTES = 256 * 1024

#: M11. The only key a chain declaration document may carry. Default-deny,
#: and the ABSENCES are the contract: ``id``, ``cursor``, ``status`` and
#: ``step_task_ids`` are runtime state JARVIS owns, so a caller naming any of
#: them is refused BY NAME rather than having it quietly ignored. A caller
#: declares intent; it never declares identity or progress.
_CHAIN_DOCUMENT_KEYS = frozenset({"steps"})

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
    # A policy refusal, and the strongest one: not a gate somebody can
    # satisfy, so it shares the policy exit rather than an input one.
    ErrorCode.EXECUTION_DISABLED: EXIT_POLICY,
    ErrorCode.CONFIG_INVALID: EXIT_POLICY,
    ErrorCode.CONFIG_IO_FAILED: EXIT_INTERNAL,
    # A confidentiality fault after an acknowledged effect: a policy exit,
    # because it blocks further effects until a person removes the residue.
    ErrorCode.CONFIDENTIAL_CLEANUP_FAILED: EXIT_POLICY,
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
    {
        "objective",
        "capability",
        "scope",
        "context",
        "preferences",
        "project_id",
        # M5. The intended write bytes and mode. A TOP-LEVEL section, not
        # a ``context`` key: the Claude connector serializes ``context``
        # verbatim into its payload, so a context key would have been an
        # egress channel for up to 64 KiB of file content.
        "write",
        # GitHub v1. A TOP-LEVEL section for the same reason ``write`` is
        # one: the Claude connector serializes ``context`` verbatim into its
        # payload, so a context key would be an egress channel. Nothing adds
        # this section to an outbound key set.
        "github",
        # Gmail minimum. A TOP-LEVEL section for the same reason: recipients,
        # a subject and a body must never ride in ``context``.
        "gmail",
        # Browser basic. A TOP-LEVEL section for the same reason: a URL, an
        # element ref and typed text must never ride in ``context``.
        "browser",
        # Windows Desktop basic. Top-level for the same reason: a window id,
        # an element ref and typed text must never ride in ``context``.
        "desktop",
        # M9. The bounded LINE REGION of the read target. A top-level section
        # for the same reason ``write`` is: ``context`` egresses verbatim.
        LOCAL_READ_REGION_SECTION,
    }
)
#: M7: ``model_answer_task_id`` is a REFERENCE to a completed same-session
#: model task whose answer becomes the write's bytes. Exactly one of
#: ``content`` and the reference is mandatory; the durable source kind is
#: derived here from that shape and is never accepted from input.
_WRITE_KEYS = frozenset({"content", "mode", LOCAL_WRITE_SOURCE_KEY})
#: GitHub v1. The typed resource fields, and nothing else. Default-deny like
#: every other section: a URL, an endpoint, an argv, a shell fragment or a
#: search-qualifier string has no key here, so none of them is expressible.
#: ``kind``, ``owner`` and ``repository`` are required; the rest are the
#: optional fields whose presence each kind decides.
_GITHUB_KEYS = frozenset(
    {"kind", "owner", "repository", "number", "ref", "path", "state", "limit", "query"}
)
#: Gmail minimum. The typed resource fields, and nothing else: no raw
#: message, no header, no argv and no attachment content is expressible.
#: ``kind`` and ``account`` are required; the rest are the optional fields
#: whose presence each kind decides.
_GMAIL_KEYS = frozenset(
    {
        "kind", "account", "query", "limit", "message_id", "thread_id",
        "attachment_id", "to", "cc", "bcc", "subject", "body",
    }
)
#: Browser basic. The typed target fields, and nothing else. Default-deny like
#: every other section: a selector, a script, a coordinate, an argv and a
#: header have no key here, so none of them is expressible. ``kind`` and
#: ``origin`` are required; the rest are the optional fields whose presence
#: each kind decides.
_BROWSER_KEYS = frozenset(
    {"kind", "origin", "url", "seconds", "tab_id", "ref", "page_digest", "text"}
)
#: Windows Desktop basic. The typed target fields, and nothing else: no
#: coordinate, class name, automation id, process id or command line is
#: expressible. ``kind`` and ``app`` are required.
_DESKTOP_KEYS = frozenset({"kind", "app", "window_id", "ref", "digest", "text"})
#: M9. The only two read-window parameters. Default-deny like every other
#: section: anything else is rejected, never ignored.
_READ_KEYS = frozenset({"start_line", "max_lines"})
_SCOPE_KEYS = frozenset({"action", "target", "destination", "data_classification"})
#: ``local_read_task_id`` (M3) is a *reference* to an earlier completed local
#: read in the same session. Content is never accepted from input: the
#: snapshot is always re-derived from the referenced task's persisted state
#: and re-validated in ``AgentCore``.
#:
#: ``local_read_task_ids`` (M8) is the plural form naming 1..3 such reads.
#: It is decoded into typed ``Task`` fields and deliberately NOT written back
#: into ``context``, which the Claude connector serializes verbatim. Exactly
#: one of the two keys may be supplied.
_CONTEXT_KEYS = frozenset(
    {
        "working_directory",
        "inputs",
        LOCAL_READ_TASK_REFERENCE_KEY,
        LOCAL_READ_CONTEXT_REFERENCE_KEY,
    }
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


def decode_chain_declaration(raw: str | bytes) -> tuple[ChainStep, ...]:
    """The trusted boundary for one inert chain declaration.

    Deliberately NOT a second grammar: the document shell is checked here -
    same byte ceiling, same duplicate-key and non-finite refusals every task
    document gets - and the steps themselves are judged by the ONE S1
    contract, ``chain_declaration_refusal``, then built by the same
    parse-once constructor that judged them. Nothing about a step is
    re-implemented, so the boundary and the persistence loader can never
    drift apart.

    Accepting a declaration performs no action and needs no approval: the
    result is inert data. Every side effect a step may eventually cause still
    belongs to an ordinary single-action task that must pass every existing
    gate by itself.
    """
    document = _load_document(raw)
    _reject_unknown(document, _CHAIN_DOCUMENT_KEYS, "chain")
    if "steps" not in document:
        raise TaskInputError("'chain.steps' is required.")
    reason = chain_declaration_refusal(document["steps"])
    if reason is not None:
        raise TaskInputError(f"Chain declaration refused: {reason}")
    return chain_steps_from_declaration(document["steps"])


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
    # The REQUESTED destination is untrusted, and it is the one field of the
    # scope that names somewhere outside this machine. Input may CONFIRM a
    # destination Core already owns; it may never introduce one. The decision
    # is ``jarvis.destinations``' - this boundary holds no endpoint of its own
    # any more - and the canonical spelling it accepts is the one the task
    # carries, so the value checked here is the value the fingerprint binds.
    requested_destination = _string(scope_input, "destination", "scope")
    destination = (
        canonical_destination(requested_destination)
        if requested_destination.strip()
        else DEFAULT_TASK_DESTINATION
    )
    if destination is None or destination_refusal(destination) is not None:
        raise TaskInputError(
            "'scope.destination' is not a destination this build permits."
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
    # M8: the plural form. Shape-validated here only; same-session
    # membership, COMPLETED source status, the producer gate, snapshot
    # validity and the classification ladder are all proven later against
    # storage, where any failure fails the task closed.
    #
    # The decoded references become TYPED fields and are deliberately never
    # written into ``context``: the Claude connector serializes ``context``
    # verbatim, so a context key would egress the task ids for no benefit.
    local_read_context_kind = LocalReadContextKind.NONE
    local_read_source_task_ids: tuple[str, ...] = ()
    if LOCAL_READ_CONTEXT_REFERENCE_KEY in context_input:
        # Exactly one of the two reference forms. Checked on KEY PRESENCE,
        # not on truthiness, so an empty singular alongside a plural is
        # still refused rather than silently ignored.
        if LOCAL_READ_TASK_REFERENCE_KEY in context_input:
            raise TaskInputError(
                "'context' accepts exactly one of "
                f"'{LOCAL_READ_TASK_REFERENCE_KEY}' or "
                f"'{LOCAL_READ_CONTEXT_REFERENCE_KEY}'."
            )
        references = context_input[LOCAL_READ_CONTEXT_REFERENCE_KEY]
        # ``type() is not list`` and not ``isinstance``: a list subclass is
        # not this contract, and a bare string must be refused rather than
        # iterated into one reference per character.
        if type(references) is not list:
            raise TaskInputError(
                f"'context.{LOCAL_READ_CONTEXT_REFERENCE_KEY}' must be a list."
            )
        if not 1 <= len(references) <= LOCAL_READ_MAX_SOURCES:
            raise TaskInputError(
                f"'context.{LOCAL_READ_CONTEXT_REFERENCE_KEY}' must name "
                f"between 1 and {LOCAL_READ_MAX_SOURCES} tasks."
            )
        for index, reference in enumerate(references):
            # The same grammar the persistence load boundary and the
            # resolver apply. Nothing is stripped or coerced, so a padded
            # reference is refused rather than quietly canonicalized.
            reason = local_read_reference_refusal(reference)
            if reason is not None:
                raise TaskInputError(
                    f"'context.{LOCAL_READ_CONTEXT_REFERENCE_KEY}[{index}]' "
                    f"is refused: {reason}."
                )
        if len(set(references)) != len(references):
            raise TaskInputError(
                f"'context.{LOCAL_READ_CONTEXT_REFERENCE_KEY}' names the "
                "same task twice."
            )
        # Order is the user's and is preserved verbatim: it decides the
        # frozen bundle order, the outbound order and the approval identity.
        local_read_source_task_ids = tuple(references)
        local_read_context_kind = LocalReadContextKind.REFERENCES

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

    # M5: the intended write bytes and the declared mode. Both are validated
    # here against the SAME canonical grammars the persistence load boundary
    # applies, so a row edited outside the product can never carry a payload
    # this boundary would have refused. The content is NOT transformed - no
    # newline normalization, no redaction, no truncation: it is the
    # deliverable, and quietly altering it would corrupt the file while the
    # product still claimed an attested write.
    write_input = _section(document, "write", _WRITE_KEYS)
    write_content = ""
    write_mode = ""
    # M7: the durable source kind is derived from the validated shape below
    # and is never read from input. A task without a ``write`` section is a
    # direct write of nothing, exactly as before.
    write_source = WriteSource.DIRECT
    write_source_task_id = ""
    if write_input:
        # Mode first, unchanged: a recognized mode is not necessarily a
        # supported one. Overwrite is refused here rather than deep in
        # execution, because no primitive available to this product can make
        # a replacement conditional on the destination still being the
        # approved file - see ``LOCAL_WRITE_ENABLED_MODES``.
        if "mode" not in write_input:
            raise TaskInputError("'write' requires 'mode'.")
        write_mode = write_input["mode"]
        if type(write_mode) is not str or write_mode not in LOCAL_WRITE_MODES:
            raise TaskInputError(
                "'write.mode' must be exactly one of: "
                + ", ".join(LOCAL_WRITE_MODES)
                + "."
            )
        reason = local_write_mode_unsupported(write_mode)
        if reason is not None:
            raise TaskInputError(f"'write.mode' is refused: {reason}.")
        # Exactly ONE content source: the bytes themselves, or a reference to
        # a completed model task whose answer will become the bytes. Never
        # both and never neither.
        has_content = "content" in write_input
        has_reference = LOCAL_WRITE_SOURCE_KEY in write_input
        if has_content == has_reference:
            raise TaskInputError(
                "'write' requires exactly one of 'content' or "
                f"'{LOCAL_WRITE_SOURCE_KEY}'."
            )
        # M10. A model answer may CREATE a file; it may not REPLACE one.
        # Nothing durable proves that the exact bounded read context reached
        # the actual model invocation that produced an answer -
        # ``canonical_outbound`` is opaque bytes and the core only checks that
        # its digest is non-empty - so "the model saw the file it is about to
        # overwrite" is not a fact this product can establish for ANY
        # connector. An irreversible in-place replacement will not be
        # authorized on an unprovable premise. Refused here so no such task is
        # ever persisted; ``_write_identity_failure`` refuses the
        # programmatically built form independently.
        if has_reference and write_mode == "overwrite":
            raise TaskInputError(
                "'write.mode' 'overwrite' cannot be combined with "
                f"'{LOCAL_WRITE_SOURCE_KEY}': a model answer may create a "
                "file but may not replace one."
            )
        if has_content:
            write_content = write_input["content"]
            reason = local_write_content_refusal(write_content)
            if reason is not None:
                raise TaskInputError(f"'write.content' is refused: {reason}.")
        else:
            # M7. The same grammar the persistence load boundary and the
            # resolver apply; nothing is stripped or coerced, so a padded
            # reference is refused rather than quietly canonicalized. The
            # reference is only meaningful for a local write, and refusing it
            # here keeps a doomed task out of persistence altogether; the
            # loop refuses it independently for programmatically built tasks.
            reference = write_input[LOCAL_WRITE_SOURCE_KEY]
            reason = local_write_source_refusal(reference)
            if reason is not None:
                raise TaskInputError(
                    f"'write.{LOCAL_WRITE_SOURCE_KEY}' is refused: {reason}."
                )
            if capability != LOCAL_WRITE_CAPABILITY:
                raise TaskInputError(
                    f"'write.{LOCAL_WRITE_SOURCE_KEY}' is only valid for "
                    f"capability '{LOCAL_WRITE_CAPABILITY}'."
                )
            write_source = WriteSource.MODEL_ANSWER
            write_source_task_id = reference

    # M9: the optional bounded read window. Absence of the section is the
    # existing HEAD read, byte-identical. The durable kind is derived here
    # from the validated shape and is never read from input.
    read_input = _section(document, LOCAL_READ_REGION_SECTION, _READ_KEYS)
    local_read_kind = LocalReadKind.HEAD
    read_start_line = 0
    read_max_lines = 0
    # Presence is asked of the DOCUMENT, not of the decoded section: an empty
    # ``"read": {}`` is a region request that names no window, and is refused
    # rather than silently becoming a head read.
    if LOCAL_READ_REGION_SECTION in document:
        for field in ("start_line", "max_lines"):
            if field not in read_input:
                raise TaskInputError(
                    f"'{LOCAL_READ_REGION_SECTION}' requires '{field}'."
                )
        start_value = read_input["start_line"]
        max_value = read_input["max_lines"]
        # ``type() is int`` and not ``isinstance``: ``True`` is an ``int``,
        # and a boolean must never be accepted as a line number.
        for name, value in (("start_line", start_value), ("max_lines", max_value)):
            if type(value) is not int:
                raise TaskInputError(
                    f"'{LOCAL_READ_REGION_SECTION}.{name}' must be an integer."
                )
        if not 1 <= start_value <= LOCAL_READ_MAX_START_LINE:
            raise TaskInputError(
                f"'{LOCAL_READ_REGION_SECTION}.start_line' must be between 1 "
                f"and {LOCAL_READ_MAX_START_LINE}."
            )
        if not 1 <= max_value <= LOCAL_READ_MAX_REGION_LINES:
            raise TaskInputError(
                f"'{LOCAL_READ_REGION_SECTION}.max_lines' must be between 1 "
                f"and {LOCAL_READ_MAX_REGION_LINES}."
            )
        if capability != LOCAL_READ_CAPABILITY:
            raise TaskInputError(
                f"'{LOCAL_READ_REGION_SECTION}' is only valid for capability "
                f"'{LOCAL_READ_CAPABILITY}'."
            )
        local_read_kind = LocalReadKind.REGION
        read_start_line = start_value
        read_max_lines = max_value
        # The same contract the load boundary and the connector apply, asked
        # once more here so a shape this boundary would refuse can never be
        # constructed at all.
        reason = local_read_region_refusal(
            local_read_kind, read_start_line, read_max_lines
        )
        if reason is not None:  # pragma: no cover - the checks above fired
            raise TaskInputError(
                f"'{LOCAL_READ_REGION_SECTION}' is refused: {reason}."
            )

    # GitHub v1. The typed resource, judged HERE by the GitHub contracts
    # themselves rather than by a second grammar living in this module. What
    # is stored on the task is the CANONICAL serialization of the object that
    # passed - never the raw input - so a task can only ever carry a resource
    # this build would construct today.
    github_input = _section(document, "github", _GITHUB_KEYS)
    github_resource: dict[str, Any] = {}
    if github_input:
        if capability not in GITHUB_READ_CAPABILITIES:
            raise TaskInputError(
                "'github' may only be named by a GitHub read capability."
            )
        for key in ("kind", "owner", "repository"):
            if key not in github_input:
                raise TaskInputError(f"'github' requires '{key}'.")
        candidate = {
            "kind": github_input["kind"],
            "owner": github_input["owner"],
            "repository": github_input["repository"],
            "number": github_input.get("number", 0),
            "ref": github_input.get("ref", ""),
            "path": github_input.get("path", ""),
            "state": github_input.get("state", ""),
            "limit": github_input.get("limit", 0),
            "query": github_input.get("query", ""),
        }
        try:
            resource = github_resource_from_dict(candidate)
        except GitHubExecutionUnavailable as exc:
            raise TaskInputError(f"'github' is refused: {exc}.") from exc
        reason = github_resource_binding_refusal(
            resource, capability=capability, destination=destination
        )
        if reason is not None:
            # The scope's destination and the resource's repository are two
            # statements about the same thing, and an approval binds both. A
            # task whose destination does not match the repository it names is
            # refused here rather than reconciled into one of them.
            raise TaskInputError(f"'github' is refused: {reason}.")
        github_resource = resource.to_dict()
    elif capability in GITHUB_READ_CAPABILITIES:
        raise TaskInputError("a GitHub read capability requires a 'github' section.")

    # Gmail minimum. Judged by the Gmail contracts themselves, stored in the
    # canonical serialization of the object that passed. Recipients are
    # typed lists here and nowhere else: a model answer, a body or a
    # ``context`` value cannot become one.
    gmail_input = _section(document, "gmail", _GMAIL_KEYS)
    gmail_resource: dict[str, Any] = {}
    if gmail_input:
        if capability not in GMAIL_ENABLED_CAPABILITIES:
            raise TaskInputError(
                "'gmail' may only be named by an enabled Gmail capability."
            )
        for key in ("kind", "account"):
            if key not in gmail_input:
                raise TaskInputError(f"'gmail' requires '{key}'.")
        candidate = {
            "kind": gmail_input["kind"],
            "account": gmail_input["account"],
            "query": gmail_input.get("query", ""),
            "limit": gmail_input.get("limit", 0),
            "message_id": gmail_input.get("message_id", ""),
            "thread_id": gmail_input.get("thread_id", ""),
            "attachment_id": gmail_input.get("attachment_id", ""),
            "to": gmail_input.get("to", []),
            "cc": gmail_input.get("cc", []),
            "bcc": gmail_input.get("bcc", []),
            "subject": gmail_input.get("subject", ""),
            "body": gmail_input.get("body", ""),
        }
        try:
            resource = gmail_resource_from_dict(candidate)
        except GmailExecutionUnavailable as exc:
            raise TaskInputError(f"'gmail' is refused: {exc}.") from exc
        reason = gmail_resource_binding_refusal(
            resource, capability=capability, destination=destination
        )
        if reason is not None:
            raise TaskInputError(f"'gmail' is refused: {reason}.")
        gmail_resource = resource.to_dict()
    elif capability in GMAIL_CAPABILITIES:
        raise TaskInputError("a Gmail capability requires a 'gmail' section.")

    # Browser basic. Judged by the browser contracts themselves and stored in
    # the canonical serialization of the object that passed. The element ref
    # and the page digest are typed fields here and nowhere else: page
    # content, a model answer or a ``context`` value cannot become one.
    browser_input = _section(document, "browser", _BROWSER_KEYS)
    browser_resource: dict[str, Any] = {}
    if browser_input:
        if capability not in BROWSER_ENABLED_CAPABILITIES:
            raise TaskInputError(
                "'browser' may only be named by an enabled browser capability."
            )
        for key in ("kind", "origin"):
            if key not in browser_input:
                raise TaskInputError(f"'browser' requires '{key}'.")
        candidate = {
            "kind": browser_input["kind"],
            "origin": browser_input["origin"],
            "url": browser_input.get("url", ""),
            "seconds": browser_input.get("seconds", 0),
            "tab_id": browser_input.get("tab_id", ""),
            "ref": browser_input.get("ref", ""),
            "page_digest": browser_input.get("page_digest", ""),
            "text": browser_input.get("text", ""),
        }
        try:
            resource = browser_resource_from_dict(candidate)
        except BrowserExecutionUnavailable as exc:
            raise TaskInputError(f"'browser' is refused: {exc}.") from exc
        reason = browser_resource_binding_refusal(
            resource, capability=capability, destination=destination
        )
        if reason is not None:
            raise TaskInputError(f"'browser' is refused: {reason}.")
        browser_resource = resource.to_dict()
    elif capability in BROWSER_CAPABILITIES:
        raise TaskInputError("a browser capability requires a 'browser' section.")

    # Windows Desktop basic. Judged by the desktop contracts themselves and
    # stored in the canonical serialization of the object that passed. The
    # window id, element ref and digest are typed fields here and nowhere
    # else: application content or a model answer cannot become one.
    desktop_input = _section(document, "desktop", _DESKTOP_KEYS)
    desktop_resource: dict[str, Any] = {}
    if desktop_input:
        if capability not in DESKTOP_ENABLED_CAPABILITIES:
            raise TaskInputError(
                "'desktop' may only be named by an enabled desktop capability."
            )
        for key in ("kind", "app"):
            if key not in desktop_input:
                raise TaskInputError(f"'desktop' requires '{key}'.")
        try:
            resource = desktop_resource_from_dict(dict(desktop_input))
        except DesktopExecutionUnavailable as exc:
            raise TaskInputError(f"'desktop' is refused: {exc}.") from exc
        reason = desktop_resource_binding_refusal(
            resource, capability=capability, destination=destination
        )
        if reason is not None:
            raise TaskInputError(f"'desktop' is refused: {reason}.")
        desktop_resource = resource.to_dict()
    elif capability in DESKTOP_CAPABILITIES:
        raise TaskInputError("a desktop capability requires a 'desktop' section.")

    # Everything else - id, timestamps, status, stage, estimate, risk and side
    # effect - comes from the dataclass defaults and stays connector-owned.
    return Task(
        objective=objective,
        capability=capability,
        scope=ActionScope(
            action=action,
            target=target,
            destination=destination,
            data_classification=classification,
        ),
        context=context,
        preferences=preferences,
        project_id=project_id,
        github=github_resource,
        gmail=gmail_resource,
        browser=browser_resource,
        desktop=desktop_resource,
        write_content=write_content,
        write_mode=write_mode,
        write_source=write_source,
        write_source_task_id=write_source_task_id,
        local_read_context_kind=local_read_context_kind,
        local_read_source_task_ids=local_read_source_task_ids,
        local_read_kind=local_read_kind,
        read_start_line=read_start_line,
        read_max_lines=read_max_lines,
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
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # linear (DFREPLY)
    re.compile(
        r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{16}\b"
    ),
    # GitHub token families, kept byte-for-byte in step with the
    # contracts policy so the presentation defence is never the weaker
    # of the two.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # Google OAuth access tokens (``ya29.``), refresh tokens (``1//0``) and
    # API keys (``AIza``) carry documented prefixes too. Gmail minimum never
    # handles one, and this is what keeps a token that leaks into a captured
    # stream or a message body from ever being shown.
    re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}"),
    re.compile(r"\b1//0[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
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
    payload: dict[str, Any] = {
        "relative_path": snapshot.relative_path,
        "byte_size": snapshot.byte_size,
        "sha256": snapshot.sha256,
        "classification": snapshot.classification.value,
        "truncated": snapshot.truncated,
        "excerpt_characters": len(snapshot.excerpt),
        "excerpt": snapshot.excerpt,
        "read_at": snapshot.read_at,
    }
    # M9. Which source lines the excerpt actually is, for a REGION read only.
    # ``0/0`` is the head sentinel the contract validates against the excerpt,
    # so a head envelope keeps byte-for-byte the shape it has always had - no
    # new top-level key, no schema bump, and no span asserted about a read
    # that never requested one.
    if snapshot.start_line > 0:
        payload["start_line"] = snapshot.start_line
        payload["end_line"] = snapshot.end_line
    return payload


def _local_write_payload(record: LocalWriteRecord) -> dict[str, Any] | None:
    """The bounded local-write view of the addressed task, or ``None``.

    Metadata only, by construction: the record itself carries no bytes, so
    there is nothing here to redact and nothing that could leak the file's
    content or a pre-image. What the user gets is what was authorized and
    what happened - the root-relative name, the mode, both digests and both
    sizes.

    ``None`` means the record did not survive the one canonical structural
    contract, so the envelope shows no local-write block rather than a
    tampered one.
    """
    if local_write_record_refusal(record) is not None:
        return None
    return {
        "relative_path": record.relative_path,
        "mode": record.mode,
        "pre_present": record.pre_present,
        "pre_sha256": record.pre_sha256,
        "pre_byte_size": record.pre_byte_size,
        "intended_sha256": record.intended_sha256,
        "intended_byte_size": record.intended_byte_size,
        "status": record.status,
        "written_at": record.written_at,
    }


def _model_answer_payload(answer: ModelAnswer) -> dict[str, Any] | None:
    """The bounded model-answer view of the addressed task, or ``None``.

    The deliverable itself plus the metadata that lets a user judge it: how
    long it is, whether it was cut, when it was produced and the digest of
    exactly these bytes. No provider internals - no response type, subtype,
    stop reason, error status or redaction counter - and no raw payload of
    any kind; those are provider diagnostics, not the answer.

    The content keeps its newlines, its indentation and any absolute paths it
    mentions, which is the entire reason M6 exists: the H4 diagnostic
    pipeline is correct for a diagnostic and destroys a deliverable. It has
    already been credential-redacted and bounded by ``bound_model_text``; the
    envelope's own ``_redact`` pass then runs over it like every other string,
    and that pass is credential-only - it neither truncates nor touches paths.

    ``None`` means the record did not survive the one canonical structural
    contract, so the envelope shows no model-answer block rather than a
    tampered one. Masking would not be enough: rendering a repaired answer
    would still assert that a bounded, intact model answer is being shown.
    """
    if model_answer_refusal(answer) is not None:
        return None
    return {
        "content": answer.content,
        "character_count": answer.character_count,
        "sha256": answer.sha256,
        "truncated": answer.truncated,
        "produced_at": answer.produced_at,
    }


def _chain_payload(
    session: SessionState | None, verdict: str | None = None
) -> dict[str, Any] | None:
    """The session's chain as descriptive state, or ``None`` for no chain.

    Presentation only: it creates no authority, and nothing reads it back.

    Unlike ``_local_read_payload`` this does NOT return ``None`` for a chain
    it cannot vouch for. A corrupted chain is rendered as itself, with
    ``usable`` false, because ``None`` is the one spelling of ABSENCE and a
    tampered declaration must never be shown as "this session has no chain".
    ``usable`` is the single canonical verdict, asked here exactly as every
    other consumer must ask it.

    ``step_task_ids`` is descriptive ROUTING state - which ordinary task was
    created for which step - and never a claim that a step succeeded. The
    storage-owned task row remains the only answer to that question.
    """
    chain = session.chain if session is not None else None
    if chain is None:
        return None
    return {
        "id": chain.id,
        "digest": chain.digest,
        "status": chain.status.value,
        "usable": chain_state_refusal(chain) is None,
        "cursor": chain.cursor,
        "steps": [step.to_dict() for step in chain.steps],
        "step_task_ids": list(chain.step_task_ids),
        # M11 S3B. Why the driver stopped, when a driver ran. ``None`` for
        # every other command, because nothing was driven. A field inside an
        # existing block, not a new top-level key, so the envelope version is
        # unchanged - the same shape M9 used when it added a span to the read
        # block.
        "verdict": verdict,
    }


def _external_read_payload(stored: Any) -> dict[str, Any] | None:
    """The external read as a rendered block, or ``None``.

    RE-VALIDATED HERE, from the stored mapping, through the same
    ``external_read_refusal`` the producer had to satisfy: a row edited
    outside the product renders no block rather than a plausible one. The
    digest and byte size are shown so the content can be checked against what
    was attested, and the classification is shown because it is what governs
    where the content may go next.
    """
    if not isinstance(stored, dict) or not stored:
        return None
    try:
        snapshot = ExternalReadSnapshot(
            source_task_id=str(stored.get("source_task_id", "")),
            connector=str(stored.get("connector", "")),
            capability=str(stored.get("capability", "")),
            destination=str(stored.get("destination", "")),
            resource_kind=str(stored.get("resource_kind", "")),
            resource_id=str(stored.get("resource_id", "")),
            content=str(stored.get("content", "")),
            truncated=bool(stored.get("truncated", False)),
            byte_size=int(stored.get("byte_size", 0)),
            sha256=str(stored.get("sha256", "")),
            classification=DataClassification(
                str(stored.get("classification", DataClassification.SECRET.value))
            ),
            captured_at=str(stored.get("captured_at", "")),
        )
    except Exception:  # noqa: BLE001 - an unrenderable snapshot renders nothing
        return None
    # The producer decides which vocabulary judges it; both refuse anything
    # that is not self-consistent, and neither grants the block authority.
    refusal = {
        GMAIL_CONNECTOR: gmail_external_read_refusal,
        BROWSER_CONNECTOR: browser_external_read_refusal,
        DESKTOP_CONNECTOR: desktop_external_read_refusal,
    }.get(snapshot.connector, external_read_refusal)
    if refusal(snapshot) is not None:
        return None
    return {
        "connector": snapshot.connector,
        "capability": snapshot.capability,
        "destination": snapshot.destination,
        "resource_kind": snapshot.resource_kind,
        "content": snapshot.content,
        "truncated": snapshot.truncated,
        "byte_size": snapshot.byte_size,
        "sha256": snapshot.sha256,
        "classification": snapshot.classification.value,
        "captured_at": snapshot.captured_at,
        # Stated rather than implied, like every other bounded fact this
        # envelope prints: external content is reference material.
        "trusted": False,
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
    *,
    session: SessionState | None = None,
    chain_verdict: str | None = None,
) -> dict[str, Any]:
    """One fixed-key, redacted envelope for every non-status command.

    ``session`` is keyword-only and is used ONLY when there is no run: the
    M11 chain commands touch a session without running a task, and they must
    report through this same envelope rather than growing a second shape. A
    run always wins, so no existing caller changes behaviour.
    """
    session = run.session if run is not None else session
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
        # M5. Metadata only and re-validated the same way, so a tampered row
        # renders no block instead of a plausible one. The intended bytes are
        # deliberately absent: this is the changed-file record, not the file.
        "local_write": (
            None if task is None else _local_write_payload(task.local_write)
        ),
        # M6. The bounded model deliverable, re-validated the same way. This
        # is untrusted inbound model data shown to the user, never authority:
        # nothing downstream reads it back, and it never returns to the model.
        "model_answer": (
            None if task is None else _model_answer_payload(task.model_answer)
        ),
        # GitHub v1. The bounded external read, re-validated the same way and
        # shown for the same reason the model answer is: it is the operator's
        # deliverable. It is UNTRUSTED inbound content - it grants nothing,
        # nothing downstream reads it back, and it never travels onward.
        "external_read": (
            None if task is None else _external_read_payload(task.external_read)
        ),
        # M11. The session's inert sequential declaration. Presentation only:
        # it grants nothing, and declaring a chain never creates a task,
        # requests an approval, opens an attempt or reserves a cent.
        "chain": _chain_payload(session, chain_verdict),
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
        # Provenance is rendered data about the record, never authority.
        "origin": item.origin.value,
        # Re-sanitized defensively for presentation (save and load already
        # apply the same canonical pipeline, so this is a no-op on any value
        # that passed those boundaries).
        "origin_detail": sanitize_diagnostic_text(
            item.origin_detail, MEMORY_ORIGIN_DETAIL_CAP
        ),
        # Exactly what was persisted: already normalized, bounded and passed
        # through the canonical credential/path hygiene at the write boundary.
        "text": item.text,
        "lifecycle": item.lifecycle.value,
        "active": item.active,
        "superseded_by": item.superseded_by,
        "created_at": item.created_at,
    }


def project_create_result(
    memory: SQLiteMemoryStore, settings: Settings, project_id: str | None, name: str
) -> CommandResult:
    """Create a project. Without ``project_id`` it gets an opaque id (Gate 3);
    an explicit slug keeps the original M1 path."""
    command = "project-create"
    try:
        if project_id is None:
            project = memory.create_project(name)
        else:
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


def project_lifecycle_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    action: str,
    project_id: str,
    name: str | None = None,
) -> CommandResult:
    """``rename`` / ``archive`` / ``unarchive`` one project. No delete."""
    command = f"project-{action}"
    try:
        if action == "rename":
            project = memory.rename_project(project_id, name)
        elif action in ("archive", "unarchive"):
            project = memory.set_project_active(project_id, action == "unarchive")
        else:
            raise ValueError(f"Unknown project action: {action}")
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


def project_instruction_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    action: str,
    project_id: str,
    text: str | None = None,
    classification: str | None = None,
) -> CommandResult:
    """``set`` / ``show`` / ``history`` of one project's instruction (Gate 4).

    ``set`` is THE audited production caller of the store's explicit-user
    instruction boundary: the text and classification are the person's own
    CLI/app input. ``--text ""`` clears (a new empty version)."""
    command = f"project-instruction-{action}"
    try:
        if action == "set":
            stored = memory.save_explicit_user_project_instruction(
                project_id, text, classification
            )
            return CommandResult({"command": command, "instruction": stored.to_dict()}, EXIT_OK)
        validate_project_id(project_id)
        if memory.load_project(project_id) is None:
            raise ValueError(f"Unknown project: {project_id}")
        if action == "show":
            current = memory.load_project_instruction(project_id)
            shown = None if current is None or not current.text else current.to_dict()
            return CommandResult({"command": command, "project_id": project_id, "instruction": shown}, EXIT_OK)
        if action == "history":
            versions = memory.list_project_instruction_history(project_id)
            return CommandResult(
                {"command": command, "project_id": project_id,
                 "versions": [version.to_dict() for version in versions]},
                EXIT_OK,
            )
        raise ValueError(f"Unknown instruction action: {action}")
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )


def project_file_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    action: str,
    project_id: str,
    *,
    path: str | None = None,
    file_id: str | None = None,
    classification: str | None = None,
    confirm: str | None = None,
    include_purged: bool = False,
) -> CommandResult:
    """``add`` / ``list`` / ``retire`` / ``restore`` / ``purge`` one project
    file (Gate 5). ``add`` is THE audited production caller of the store's
    explicit-user file boundary: the path and classification are the
    person's own CLI/app input. ``purge`` is permanent and needs ``confirm``
    equal to the file id; the store verifies it before any mutation."""
    command = f"project-file-{action}"
    try:
        if action == "add":
            content = project_files.read_project_file(path)
            stored = memory.save_explicit_user_project_file(project_id, content, classification)
            return CommandResult({"command": command, "file": stored}, EXIT_OK)
        if action == "list":
            validate_project_id(project_id)
            if memory.load_project(project_id) is None:
                raise ValueError(f"Unknown project: {project_id}")
            files = memory.list_project_files(project_id, include_purged=include_purged)
            return CommandResult(
                {"command": command, "project_id": project_id, "files": files}, EXIT_OK
            )
        if action in ("retire", "restore"):
            stored = memory.set_project_file_active(project_id, file_id, action == "restore")
            return CommandResult({"command": command, "file": stored}, EXIT_OK)
        if action == "purge":
            stored = memory.purge_project_file(project_id, file_id, confirm=confirm)
            return CommandResult({"command": command, "file": stored}, EXIT_OK)
        raise ValueError(f"Unknown project file action: {action}")
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )


def _folder_protected(memory: SQLiteMemoryStore, settings: Settings) -> tuple[Path, ...]:
    """JARVIS's own folders: no project folder may be one of them or inside one."""
    home = Path.home()
    own = (
        memory.database_path.parent,
        settings.memory_database.parent,
        home / ".jarvis",
        home / "Apps" / "JARVIS",
        home / "JARVIS-backups",
    )
    # JARVIS's own folders as spelled AND as resolved, so an 8.3, SUBST or
    # junction spelling of them cannot hide them from the proven pick.
    return own + tuple(path.resolve() for path in own)


def project_folder_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    action: str,
    project_id: str,
    *,
    path: str | None = None,
    on_desktop: bool = False,
) -> CommandResult:
    """``show`` / ``propose`` / ``check`` / ``link`` / ``unlink`` / ``create`` one
    project's folder (Gate 6H H1, H1b). ``link`` is THE audited production caller of the
    store's explicit-user folder boundary: the path is the person's own pick.
    ``create`` (K1) creates the folder at ``path`` only when it EQUALS what
    ``propose_folder`` gives NOW for the stored name (D1): the person's click
    on that exact shown path is the approval (Salih Q5), JARVIS decides the
    path. The link is CONTEXT for the LOCAL USER only - this result is the one
    place the path appears; no root, working directory, task, event or model
    payload ever gets it. Nothing here moves or deletes a folder."""
    command = f"project-folder-{action}"
    protected = _folder_protected(memory, settings)
    try:
        validate_project_id(project_id)
        if action == "link":
            proven = project_folders.validate_folder(path, protected=protected)
            link = memory.link_explicit_user_project_folder(project_id, proven, "picked")
            return CommandResult({"command": command, "project_id": project_id, "folder": link.to_dict()}, EXIT_OK)
        if action == "unlink":
            memory.unlink_project_folder(project_id)
            return CommandResult({"command": command, "project_id": project_id, "folder": None}, EXIT_OK)
        project = memory.load_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        if action == "show":
            link = memory.load_project_folder(project_id)
            shown = None if link is None else link.to_dict()
            return CommandResult({"command": command, "project_id": project_id, "folder": shown}, EXIT_OK)
        if action == "check":
            # K3 "Choose an existing folder…": the link's own refusals, asked
            # BEFORE the pending request is rejected, so a refused pick decides
            # nothing. Reads only; ``link`` proves the folder again.
            if not project.active:
                raise ValueError(f"Project is archived: {project_id}")
            if memory.load_project_folder(project_id) is not None:
                raise ValueError("The project already has a linked folder; unlink it first.")
            proven = project_folders.validate_folder(path, protected=protected)
            for other in memory.list_projects():
                link = None if other.id == project_id else memory.load_project_folder(other.id)
                if link is not None and project_folders.folders_overlap(proven, link.path):
                    raise ValueError("That folder, or one inside or around it, is linked to another project.")
            return CommandResult({"command": command, "project_id": project_id, "folder": None}, EXIT_OK)
        if action == "propose":
            if not project.active:
                raise ValueError(f"Project is archived: {project_id}")
            proposed = project_folders.propose_folder(project.name, on_desktop, protected=protected)
            return CommandResult({"command": command, "project_id": project_id, "path": proposed}, EXIT_OK)
        if action == "create":
            if not project.active:
                raise ValueError(f"Project is archived: {project_id}")
            if project_folders.propose_folder(project.name, on_desktop, protected=protected) != path:
                raise ValueError("The folder path changed; confirm the new path.")
            try:
                link, creation = project_folders.create_and_link(memory, project_id, path, protected=protected)
            except project_folders.FolderCreateError as exc:
                # The paths travel as their own field: a diagnostic message is path-redacted.
                # A stable code (D-6): the window says it from ``created`` and ``link_state``.
                state = exc.link_state if exc.link_state in ("linked", "not linked") else "unknown"
                failed = failure_result(
                    command, settings.permission_mode, ErrorCode.INVALID_TASK,
                    f"Folder creation stopped partway: {len(exc.created)} folder(s) created and kept "
                    f"(nothing was deleted). Project link: {state}.",
                    EXIT_INVALID_INPUT,
                )
                failed.payload.update(created=list(exc.created), link_state=exc.link_state)
                return failed
            return CommandResult({"command": command, "project_id": project_id, "folder": link.to_dict(),
                                  "created": list(creation.created)}, EXIT_OK)
        raise ValueError(f"Unknown project folder action: {action}")
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )


def project_folder_preview_result(
    memory: SQLiteMemoryStore, settings: Settings, name: str, on_desktop: bool
) -> CommandResult:
    """Where "Klasör oluştur" WOULD create the folder of a project not stored
    yet: ``propose_folder`` for the name as ``create_project`` stores it.
    Reads only; creates and stores nothing."""
    command = "project-folder-preview"
    try:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("project name must be a non-blank string")
        path = project_folders.propose_folder(name.strip(), on_desktop, protected=_folder_protected(memory, settings))
    except ValueError as exc:
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK, str(exc), EXIT_INVALID_INPUT
        )
    return CommandResult({"command": command, "path": path}, EXIT_OK)


def conversation_attach_preview_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
    *,
    project_id: str | None = None,
    new_project_name: str | None = None,
) -> CommandResult:
    """What attaching a Global conversation to a project would do (Gate 6C),
    read-only; ``blocked`` carries the refusal the confirm would meet."""
    command = "conversation-attach-preview"
    try:
        preview = memory.conversation_attach_preview(
            conversation_id, project_id=project_id, new_project_name=new_project_name
        )
    except ValueError as exc:
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK, str(exc), EXIT_INVALID_INPUT
        )
    return CommandResult({"command": command, "preview": preview}, EXIT_OK)


def conversation_attach_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
    *,
    expected_next_seq: int,
    project_id: str | None = None,
    new_project_name: str | None = None,
) -> CommandResult:
    """Attach ONE Global conversation to a project, one-way (Gate 6C, Salih
    decision 2026-09-29). THE audited production caller of the store's
    writer: the conversation, the target and the previewed ``next_seq`` are
    the person's own confirmed app input. No model, connector, intent,
    scheduler or Core path reaches it."""
    command = "conversation-attach"
    try:
        attached = memory.attach_global_conversation_to_project(
            conversation_id,
            expected_next_seq=expected_next_seq,
            project_id=project_id,
            new_project_name=new_project_name,
        )
    except ValueError as exc:
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK, str(exc), EXIT_INVALID_INPUT
        )
    return CommandResult({"command": command, "attached": attached}, EXIT_OK)


def conversation_rename_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
    title: str,
) -> CommandResult:
    """A manual rename of one conversation (Gate 6D-a): the person's own
    window input, a label only - never read by any authority decision. The
    store alone decides the length limit and the archived refusals."""
    command = "conversation-rename"
    try:
        memory.set_conversation_title(conversation_id, title)
    except KeyError:
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK,
            f"Unknown or archived conversation: {conversation_id}", EXIT_INVALID_INPUT,
        )
    except ValueError as exc:
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK, str(exc), EXIT_INVALID_INPUT
        )
    return CommandResult({"command": command, "conversation_id": conversation_id, "title": title}, EXIT_OK)


def _conversation_lifecycle_failure(command: str, settings: Settings, conversation_id: str, exc: Exception) -> CommandResult:
    message = f"Unknown or deleted conversation: {conversation_id}" if isinstance(exc, KeyError) else str(exc)
    return failure_result(command, settings.permission_mode, ErrorCode.INVALID_TASK, message, EXIT_INVALID_INPUT)


def conversation_archive_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
) -> CommandResult:
    """Archive ONE conversation (Gate 6D-b, Salih decision 2026-09-30): the
    reversible first step. THE audited production caller of the store's
    archive; the conversation is the person's own window choice. No model,
    connector, intent, scheduler or Core path reaches it."""
    command = "conversation-archive"
    try:
        record = memory.archive_conversation(conversation_id)
    except (KeyError, ValueError) as exc:
        return _conversation_lifecycle_failure(command, settings, conversation_id, exc)
    return CommandResult({"command": command, "conversation_id": record.id, "archived_at": record.archived_at}, EXIT_OK)


def conversation_unarchive_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
) -> CommandResult:
    """Undo an archive exactly (Gate 6D-b). THE audited production caller of
    the store's unarchive; explicit window input only."""
    command = "conversation-unarchive"
    try:
        record = memory.unarchive_conversation(conversation_id)
    except (KeyError, ValueError) as exc:
        return _conversation_lifecycle_failure(command, settings, conversation_id, exc)
    return CommandResult({"command": command, "conversation_id": record.id}, EXIT_OK)


def conversation_delete_preview_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
) -> CommandResult:
    """What a permanent delete would remove (Gate 6D-b), read-only;
    ``blocked`` carries the refusal the confirm would meet."""
    command = "conversation-delete-preview"
    try:
        preview = memory.conversation_delete_preview(conversation_id)
    except (KeyError, ValueError) as exc:
        return _conversation_lifecycle_failure(command, settings, conversation_id, exc)
    return CommandResult({"command": command, "preview": preview}, EXIT_OK)


def conversation_delete_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    conversation_id: str,
    *,
    confirm: str,
    expected_next_seq: int,
    expected_title: str,
) -> CommandResult:
    """Permanently delete ONE conversation (Gate 6D-b, Salih decision
    2026-09-30): the second step after archive. THE audited production
    caller of the store's writer: the conversation, the confirmation and
    the previewed ``next_seq`` and title are the person's own confirmed
    window input. No model, connector, intent, scheduler or Core path
    reaches it, and there is no CLI for it."""
    command = "conversation-delete"
    try:
        deleted = memory.delete_conversation(
            conversation_id, confirm=confirm, expected_next_seq=expected_next_seq, expected_title=expected_title
        )
    except (KeyError, ValueError) as exc:
        return _conversation_lifecycle_failure(command, settings, conversation_id, exc)
    return CommandResult({"command": command, "deleted": deleted}, EXIT_OK)


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
        # THE trusted explicit-human write boundary (Memory V2). Kind and
        # classification are explicit user input; an unknown value fails
        # closed and is never defaulted or downgraded. The one deterministic
        # mapping is the deprecated M1-era "preference" input: it is an
        # explicit human CLI action, so the trusted boundary writes it as a
        # RULE. The boundary establishes EXPLICIT_USER provenance internally
        # - this composition path is its single audited production caller,
        # and generic/programmatic writers (which cannot self-assert
        # explicit-user provenance) never inherit the alias.
        stored = memory.save_explicit_user_memory_item(
            project_id,
            kind,
            classification,
            text,
            supersedes=supersedes,
        )
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
    memory: SQLiteMemoryStore,
    settings: Settings,
    project_id: str,
    include_inactive: bool = False,
) -> CommandResult:
    command = "memory-list"
    try:
        valid_id = validate_project_id(project_id)
        if memory.load_project(valid_id) is None:
            raise ValueError(f"Unknown project: {valid_id}")
        items = memory.list_memory_items(
            valid_id, include_inactive=bool(include_inactive)
        )
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


def memory_retire_result(
    memory: SQLiteMemoryStore, settings: Settings, project_id: str, item_id: str
) -> CommandResult:
    """Explicitly retire one ACTIVE memory item (V2 lifecycle transition)."""
    command = "memory-retire"
    try:
        valid_id = validate_project_id(project_id)
        if memory.load_project(valid_id) is None:
            raise ValueError(f"Unknown project: {valid_id}")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("retire requires the id of the item to retire")
        retired = memory.retire_memory_item(valid_id, item_id)
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
            "item": _memory_item_payload(retired),
        },
        EXIT_OK,
    )


# --------------------------------------------------------------------------
# V2.1b Memory Doctor (DOCREPAIR). The report is read-only and carries ids
# and summary keys, never record text (a FILTER_VIOLATION finding is about
# text that must not be shown). A repair is the user's explicit, confirmed
# command, one finding at a time; the store re-checks everything.
# --------------------------------------------------------------------------


def _doctor_findings(memory: SQLiteMemoryStore, project_id: str | None):
    if project_id is not None:
        validate_project_id(project_id)
        if memory.load_project(project_id) is None:
            raise ValueError(f"Unknown project: {project_id}")
    snapshot = memory.doctor_snapshot(project_id)
    return snapshot, diagnose(
        snapshot, is_storable=lambda text: classify_candidate_text(text).allowed
    )


def _finding_payload(finding: Finding) -> dict[str, Any]:
    repair = finding.repair
    return {
        "finding_id": finding.finding_id,
        "code": finding.code.value,
        "severity": finding.severity.value,
        "record_ids": list(finding.record_ids),
        "summary_key": finding.summary_key,
        "repair": None if repair is None else {
            "action": repair.action.value, "record_ids": list(repair.record_ids),
        },
    }


def memory_doctor_payload(memory: SQLiteMemoryStore, project_id: str | None = None) -> dict[str, Any]:
    """Read-only Memory Doctor report for one project, or the whole store."""
    try:
        snapshot, findings = _doctor_findings(memory, project_id)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "project_id": project_id,
        "taken_at": snapshot.taken_at,
        "record_count": len(snapshot.records),
        "findings": [_finding_payload(finding) for finding in findings],
    }


def apply_memory_repair(
    memory: SQLiteMemoryStore,
    finding_id: str,
    *,
    project_id: str | None = None,
    confirm: str | None = None,
) -> dict[str, Any]:
    """Apply the repair of ONE current finding the user confirmed.

    ``confirm`` must repeat ``finding_id`` (the purge confirmation style).
    The finding is diagnosed again from current state, so a stale or made-up
    id is refused. The explicit-user proof is built HERE, from the confirmed
    command (I2/I3). A SUPERSEDE of a larger conflict applies one
    supersession per confirmation; the rest stays a finding."""
    if type(finding_id) is not str or not finding_id:
        return {"ok": False, "error": "A repair names one finding id."}
    if confirm != finding_id:
        return {"ok": False, "error": "Repair not confirmed: repeat the finding id to confirm."}
    try:
        snapshot, findings = _doctor_findings(memory, project_id)
        finding = next((f for f in findings if f.finding_id == finding_id), None)
        if finding is None or finding.repair is None:
            raise ValueError(f"No current repairable finding {finding_id}.")
        action = finding.repair.action
        if action is RepairAction.REBUILD_FTS:
            return {"ok": True, "finding_id": finding_id, "action": action.value,
                    "indexed": memory.rebuild_fts()}
        records = {state.record.record_id: state.record for state in snapshot.records}
        ids = finding.repair.record_ids
        if action is RepairAction.SUPERSEDE:
            # The store supersedes only a record its successor already names;
            # oldest link first, so a chain A <- B <- C resolves step by step.
            ids = next(
                ((keep, old) for old in reversed(ids) for keep in ids if records[keep].supersedes == old),
                ids[:2],
            )
        target = ids[-1]  # the record whose status changes
        status = memory.apply_doctor_repair(
            RepairProposal(action, ids),
            MemoryRecordApproval(records[target].scope.project_id, target, "user-doctor-repair"),
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "finding_id": finding_id,
        "action": action.value,
        "record_id": target,
        "status": status.value,
        "remaining": len(finding.repair.record_ids) - len(ids),
    }


def memory_doctor_result(
    memory: SQLiteMemoryStore,
    settings: Settings,
    project_id: str | None = None,
    *,
    repair: str | None = None,
    confirm: str | None = None,
    rebuild_fts: bool = False,
) -> CommandResult:
    """CLI ``memory doctor``: the report, one confirmed repair, or an FTS
    rebuild (index only, idempotent, so the command itself is the consent)."""
    command = "memory-doctor"
    if (confirm is not None and repair is None) or (rebuild_fts and project_id is not None):
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK,
            "--confirm needs --repair; --rebuild-fts rebuilds the whole index and takes no --project.",
            EXIT_INVALID_INPUT,
        )
    if rebuild_fts:
        payload = {"ok": True, "action": RepairAction.REBUILD_FTS.value, "indexed": memory.rebuild_fts()}
    elif repair is not None:
        payload = apply_memory_repair(memory, repair, project_id=project_id, confirm=confirm)
    else:
        payload = memory_doctor_payload(memory, project_id)
    if not payload.pop("ok"):
        return failure_result(
            command, settings.permission_mode, ErrorCode.INVALID_TASK, payload["error"], EXIT_INVALID_INPUT
        )
    return CommandResult({"command": command, **payload}, EXIT_OK)


# --------------------------------------------------------------------------
# M12 S1. Read-only operator visibility - local, deterministic and inert.
#
# Like ``status``, ``project`` and ``memory``, these build no registry, no
# connector and no AgentCore, so their inertness is STRUCTURAL rather than
# merely intended: there is nothing here that could execute, approve, open an
# attempt or reserve a cent even if it tried. They read persisted rows and
# render OPERATIONAL METADATA - identity, lifecycle position, timestamps,
# counts and the small set of already-bounded payloads the envelope already
# knows how to render. No objective, no context, no model answer, no file
# bytes and no raw persisted document is rendered by any of them.
# --------------------------------------------------------------------------


def _bounded_limit(value: Any) -> int:
    """The row bound a listing may use, or a refusal.

    Exact ``int`` and not ``isinstance``: ``True`` is an ``int`` and a boolean
    is not a row count. The ceiling is policy, not a suggestion - a listing is
    an operator convenience, and an unbounded one would quietly become a bulk
    reader of task state.
    """
    if value is None:
        return VISIBILITY_DEFAULT_LIMIT
    if type(value) is not int or isinstance(value, bool):
        raise ValueError("'--limit' must be an integer.")
    if value < 1:
        raise ValueError("'--limit' must be at least 1.")
    if value > VISIBILITY_MAX_LIMIT:
        raise ValueError(f"'--limit' may not exceed {VISIBILITY_MAX_LIMIT}.")
    return value


def _visibility_chain_payload(summary: SessionSummary) -> dict[str, Any] | None:
    """The session's chain as ORIENTATION ONLY, for the visibility surface.

    F1. Deliberately NOT ``_chain_payload``. That builder serializes every
    ``ChainStep`` - objective, target, destination, write mode - because the
    commands it serves are the ones the user just drove, and seeing their own
    declaration echoed back is the point there. A visibility command has a
    different contract: operational metadata only. Reusing the wide builder
    turned ``show`` into a reader of declaration text for any task in a
    chain-owning session, which is exactly what this slice promised not to be.

    So this is a separate, narrower projection - the wide one is untouched and
    keeps serving run/resume/approve/chain-run unchanged. What crosses here is
    only what an operator needs to ORIENT: which chain, whether it is usable,
    what status it is in, where the cursor is and how many steps exist. Not
    one field of the declaration, and not the bound task ids either: those are
    routing state a listing does not need to answer "where am I".

    F2. It takes the VALIDATED summary, never a raw ``SessionState``, so both
    the listing and the single-session view render this block from exactly
    the same proven fields. A document that failed validation carries no
    chain, so an unreadable row cannot show one.

    ``digest`` is a fixed-size hex fingerprint of the declaration, not its
    content: it identifies WHICH declaration without revealing any of it, and
    it is what makes two chains distinguishable across a redeclaration.
    """
    if not summary.readable or summary.chain_status is None:
        return None
    return {
        "id": summary.chain_id,
        "digest": summary.chain_digest,
        "status": summary.chain_status,
        "usable": summary.chain_usable,
        "cursor": summary.chain_cursor,
        "step_count": summary.chain_step_count,
    }


def _visibility_progress(
    view: TaskSummary, session: SessionSummary
) -> dict[str, Any] | None:
    """The progress block, composed ONLY from frozen validated projections.

    P1. This used to reopen the task and the session through the ordinary
    loaders to build the block. That reintroduced everything the validation
    above had just established: the persisted document could be REPLACED
    between the proof and this render, so a forged id, a forged lifecycle and
    a forged artifact label reached the output under a projection that had
    already been approved - and an UNREADABLE session still contributed its
    ``next_step`` text, because the raw row was read again regardless of the
    verdict on it.

    Nothing is re-read now. The task-derived half was frozen at the instant
    the document was validated, and the session-derived half comes from the
    session projection, which is ``None`` unless that document validated too.
    There is no window left, because there is no second read.
    """
    if not view.readable or view.progress is None:
        return None
    progress = view.progress
    return {
        "task_id": view.id,
        "plan": list(progress.plan),
        "current_stage": progress.current_stage,
        "next_stage": progress.next_stage,
        # SESSION-derived, and therefore gated on the SESSION's own verdict:
        # an unreadable session contributes no display text at all.
        "post_completion_next_step": (
            session.next_step
            if session.readable and view.status == TaskStatus.COMPLETED.value
            else None
        ),
        "completed_stages": list(progress.completed_stages),
        "open_issues": [dict(issue) for issue in progress.open_issues],
        "artifacts": [dict(artifact) for artifact in progress.artifacts],
    }


def _attempt_summary_payload(summary: AttemptSummary) -> dict[str, Any]:
    return {
        # The handle 'task resolve --attempt-id' asks for. Naming an attempt
        # is not authority over it: recovery re-proves the attempt it is
        # given against the authoritative rows before it changes anything.
        "id": summary.id,
        "state": None if summary.state is None else summary.state.value,
        "connector": summary.connector,
        "updated_at": summary.updated_at,
        # ``None`` where the state could not be trusted: unknown is not "yes".
        "replayable": summary.replayable,
        "readable": summary.readable,
    }


def _approval_summary_payload(summary: ApprovalSummary) -> dict[str, Any]:
    return {
        "request_id": summary.request_id,
        "fingerprint": summary.fingerprint,
        "expires_at": summary.expires_at,
        "readable": summary.readable,
    }


def _session_summary_payload(summary: SessionSummary) -> dict[str, Any]:
    return {
        "session_id": summary.id,
        "updated_at": summary.updated_at,
        "readable": summary.readable,
        "project_id": summary.project_id,
        "active_task_id": summary.active_task_id,
        "latest_task_id": summary.latest_task_id,
        "task_count": summary.task_count,
        "chain": _visibility_chain_payload(summary),
    }


def _task_summary_payload(summary: TaskSummary) -> dict[str, Any]:
    return {
        "task_id": summary.id,
        "session_id": summary.session_id,
        "status": summary.status,
        "stage": summary.stage,
        "updated_at": summary.updated_at,
        "readable": summary.readable,
        "capability": summary.capability,
        "action": summary.action,
        "selected_connector": summary.selected_connector,
        "chain_step_index": summary.chain_step_index,
    }


def sessions_result(
    memory: MemoryStore, settings: Settings, limit: Any = None
) -> CommandResult:
    """Every recently touched session, newest first. Reads only."""
    command = "sessions"
    try:
        bound = _bounded_limit(limit)
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    summaries = memory.list_sessions(bound)
    return CommandResult(
        _redact(
            {
                "command": command,
                "limit": bound,
                "count": len(summaries),
                "sessions": [
                    _session_summary_payload(summary) for summary in summaries
                ],
            }
        ),
        EXIT_OK,
    )


def tasks_result(
    memory: MemoryStore, settings: Settings, session_id: str, limit: Any = None
) -> CommandResult:
    """Every task of ONE session, newest first. Reads only."""
    command = "tasks"
    try:
        bound = _bounded_limit(limit)
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    if memory.session_summary(session_id) is None:
        # An unknown session is NOT an empty listing: reporting "no tasks"
        # for a session that does not exist would send an operator looking
        # for work in the wrong place. Asked through the VALIDATED projection
        # (F2), not ``load_session``: existence is a question about the ROW,
        # and the ordinary loader would also answer questions about a
        # document this command has no business trusting.
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.UNKNOWN_SESSION,
            f"Unknown session: {session_id}",
            EXIT_NOT_FOUND,
        )
    summaries = memory.list_tasks(session_id, bound)
    return CommandResult(
        _redact(
            {
                "command": command,
                "session_id": session_id,
                "limit": bound,
                "count": len(summaries),
                "tasks": [_task_summary_payload(summary) for summary in summaries],
            }
        ),
        EXIT_OK,
    )


def show_result(
    memory: MemoryStore, settings: Settings, session_id: str, task_id: str
) -> CommandResult:
    """One task's operational snapshot: what it is doing, and what to look at.

    Ownership is proven against the AUTHORITATIVE owning session before
    anything is rendered, so a task cannot be inspected under a session that
    does not own it - the same rule every executing command already applies,
    asked here of storage rather than of the caller.

    Everything rendered is either a small identifier/enum or one of the
    EXISTING bounded payload builders. There is no new redaction system and no
    new free-text channel: the persisted objective, context, model-answer
    CONTENT, file excerpt and raw document are all deliberately absent. The
    durable deliverable itself is reported as bounded metadata only - that a
    completed answer exists, and the size, digest, truncation flag and
    timestamp to judge it by.
    """
    command = "show"
    session = memory.session_summary(session_id)
    if session is None:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.UNKNOWN_SESSION,
            f"Unknown session: {session_id}",
            EXIT_NOT_FOUND,
        )
    if memory.task_session_id(task_id) != session_id:
        # Covers both "no such task" and "belongs to another session", and
        # deliberately says the same thing for both: which other session owns
        # a task is not this caller's business.
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.TASK_NOT_FOUND,
            f"Unknown task for this session: {task_id}",
            EXIT_NOT_FOUND,
        )
    # F2. The VALIDATED projection, never ``load_task``. Identity, status and
    # stage have been proven against the authoritative columns inside it, so
    # a forged document cannot make this command report a lifecycle the
    # product is not actually in.
    view = memory.task_summary(task_id)
    if view is None:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            f"Task state could not be read: {task_id}",
            EXIT_NOT_FOUND,
        )
    # F3. The BOUNDED projections, never the raw rows. A malformed attempt,
    # approval or journal row is a thing an operator runs this command to
    # find out about, so each one degrades to bounded "unreadable" metadata
    # instead of raising or echoing whatever the row happened to hold.
    attempt = memory.latest_attempt_summary(task_id)
    pending = memory.pending_approval_summary(task_id, session_id)
    event = memory.latest_task_event(task_id)
    payload = {
        "command": command,
        "session_id": session_id,
        "task": {
            "task_id": view.id,
            # The authoritative COLUMNS, which is what the product acts on.
            "status": view.status,
            "stage": view.stage,
            # Document-derived, and therefore ``None`` unless the document
            # was proven consistent with those columns.
            "capability": view.capability,
            "action": view.action,
            "selected_connector": view.selected_connector,
            "readable": view.readable,
        },
        # The ordinary bounded progress view - rendered ONLY for a document
        # that passed validation. It is entirely document-derived, so showing
        # it for a row whose identity or lifecycle disagrees with storage
        # would be exactly the forged metadata F2 exists to refuse.
        "progress": _visibility_progress(view, session),
        "latest_event": (
            None
            if event is None
            else {
                "event_type": event.event_type,
                "created_at": event.created_at,
                "readable": event.readable,
            }
        ),
        # H2 position. The attempt's own identity is enough to decide what to
        # do next; nothing it wrapped is rendered. An attempt in doubt must
        # never be replayed automatically, so the operator is told plainly
        # which state that is - and told ``null`` when the row cannot say.
        "attempt": None if attempt is None else _attempt_summary_payload(attempt),
        # Approval METADATA only - exactly the three fields a caller needs to
        # answer an open request. The scope, risk, reason and side-effect
        # detail stay with the approval-issuing envelope that renders them
        # under its own contract; here they would be an unbounded channel.
        "pending_approval": (
            None if pending is None else _approval_summary_payload(pending)
        ),
        # The NARROW visibility projection, not the wide envelope builder:
        # orientation without one field of the declaration (F1).
        "chain": _visibility_chain_payload(session),
        # Already validated inside the projection.
        "chain_step_index": view.chain_step_index,
        # P2. What the lifecycle alone cannot say. A task that ends CANCELLED
        # because the operator's request beat a returned result is reported
        # as CANCELLED - correctly - but an approved local write that had
        # already landed is not undone by that, and this surface used to
        # imply it was. Two constant-vocabulary values: no path, no bytes, no
        # digest. ``None`` whenever no such effect is KNOWN.
        "effect_applied": (
            None
            if view.effect_applied is None
            else {
                "kind": view.effect_applied.kind,
                "status": view.effect_applied.status,
            }
        ),
        # The task's durable deliverable (the M6 model answer), as bounded
        # METADATA only: how long it is, whether it was cut, when it was
        # produced, and the digest of exactly the bytes the envelope's own
        # ``model_answer`` block shows. The content itself is deliberately
        # still absent - this surface has no free-text channel, and the
        # answer text is untrusted inbound model data whose reader-facing
        # home is the envelope. ``None`` means no deliverable is KNOWN: no
        # answer was produced, the row's document cannot be read, or the
        # answer did not survive its own structural contract.
        "deliverable": (
            None
            if view.deliverable is None
            else {
                "character_count": view.deliverable.character_count,
                "sha256": view.deliverable.sha256,
                "truncated": view.deliverable.truncated,
                "produced_at": view.deliverable.produced_at,
            }
        ),
    }
    return CommandResult(_redact(payload), EXIT_OK)


#: What each recovery verdict means to the operator, and the exit code that
#: carries it. Every refusal wrote nothing at all.
_RECOVERY_REFUSALS: dict[AttemptRecoveryOutcome, tuple[ErrorCode, str, int]] = {
    AttemptRecoveryOutcome.UNKNOWN: (
        ErrorCode.TASK_NOT_FOUND,
        "No such execution attempt for this session and task.",
        EXIT_NOT_FOUND,
    ),
    AttemptRecoveryOutcome.NOT_RECOVERABLE: (
        ErrorCode.INVALID_TASK,
        (
            "Only an attempt whose outcome is UNKNOWN may be abandoned; this "
            "one is in another state and nothing was changed."
        ),
        EXIT_INVALID_INPUT,
    ),
    AttemptRecoveryOutcome.TERMINAL: (
        ErrorCode.INVALID_TASK,
        "The task already reached a terminal state; nothing was changed.",
        EXIT_INVALID_INPUT,
    ),
}


#: Phase 2B2b1. One message per refusal the orphan declaration can return.
#: Deliberately separate from ``_RECOVERY_REFUSALS``: abandoning an UNKNOWN
#: attempt and declaring an IN_FLIGHT one orphaned are different acts with
#: different preconditions, and one shared message would have to be vague
#: about both.
_ORPHAN_REFUSALS: dict[AttemptRecoveryOutcome, tuple[ErrorCode, str, int]] = {
    AttemptRecoveryOutcome.UNKNOWN: (
        ErrorCode.TASK_NOT_FOUND,
        "No such execution attempt for this session and task.",
        EXIT_NOT_FOUND,
    ),
    AttemptRecoveryOutcome.NOT_RECOVERABLE: (
        ErrorCode.INVALID_TASK,
        (
            "Only an attempt that is still IN_FLIGHT may be declared orphaned; "
            "this one is in another state and nothing was changed."
        ),
        EXIT_INVALID_INPUT,
    ),
    AttemptRecoveryOutcome.TERMINAL: (
        ErrorCode.INVALID_TASK,
        "The task already reached a terminal state; nothing was changed.",
        EXIT_INVALID_INPUT,
    ),
    AttemptRecoveryOutcome.TOO_RECENT: (
        ErrorCode.INVALID_TASK,
        (
            "This attempt started too recently to be declared orphaned. The "
            "wait proves nothing about the worker - it only makes a mistyped "
            "attempt id unlikely. Nothing was changed."
        ),
        EXIT_INVALID_INPUT,
    ),
}


def task_orphan_result(
    memory: MemoryStore,
    settings: Settings,
    session_id: str,
    task_id: str,
    attempt_id: str,
    actor_label: str,
    reason: str,
) -> CommandResult:
    """Declare that an IN_FLIGHT execution will never be concluded by its owner.

    Local and deterministic like the other recovery verbs: it builds no
    registry, no connector and no ``AgentCore``, so it is structurally
    incapable of executing, replaying or resuming anything. The only authority
    it exercises is the single storage transaction, which decides everything.

    The response states the bounded facts an operator needs in order NOT to
    misread what they just did: the attempt is now recorded as unresolved, that
    is a statement about knowledge and not about the world, the task and the
    money are untouched, and the next step is a separate explicit decision.
    """
    command = "task-orphan"
    try:
        outcome = memory.declare_attempt_orphaned(
            session_id,
            task_id,
            attempt_id,
            now=datetime.now(timezone.utc),
            actor_label=actor_label,
            reason=reason,
        )
    except JobError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    if outcome is not AttemptRecoveryOutcome.RECOVERED:
        code, message, exit_code = _ORPHAN_REFUSALS[outcome]
        return failure_result(
            command, settings.permission_mode, code, message, exit_code
        )
    return CommandResult(
        _redact(
            {
                "command": command,
                "session_id": session_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "outcome": outcome.value,
                # The bounded facts, stated rather than implied.
                "attempt_state": AttemptState.UNKNOWN_OUTCOME.value,
                "worker_death_proved": False,
                "external_effect_established": False,
                "retry_authorized": False,
                "connector_replay": False,
                "task_modified": False,
                "chain_modified": False,
                "monetary_exposure_released": False,
                "actor_authenticated": False,
                "next_step": (
                    "The attempt is now recorded as UNKNOWN_OUTCOME and will "
                    "never be replayed. This records that nobody will "
                    "establish the outcome - it does NOT establish that the "
                    "external effect did or did not happen. The task is "
                    "unchanged and still blocks its chain; abandon the attempt "
                    "with 'jarvis task resolve' to fail the task, then resolve "
                    "the scheduled generation if one owns it."
                ),
            }
        ),
        EXIT_OK,
    )


def job_run_cancel_result(
    memory: MemoryStore,
    settings: Settings,
    run_id: str,
    owner_token: str,
    actor_label: str,
    reason: str,
) -> CommandResult:
    """Ask for a scheduled generation to stop.

    Local and deterministic like every other operator verb: it builds a store
    and nothing else - no registry, no connector, no ``AgentCore`` - so it is
    structurally incapable of executing, replaying or resuming anything. What
    it does is record one sticky intent and then let the authorities that
    already own the chain and the task do their own work.

    The response is written so that a cancelled generation is never mistaken
    for an undone one: it says plainly whether anything was still running, that
    no external effect was reversed, and that nothing will be retried.
    """
    command = "job-cancel"
    try:
        outcome = cancel_job_run(
            memory,
            run_id,
            owner_token,
            actor_label=actor_label,
            reason=reason,
        )
    except JobError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    if outcome.verdict == DISPATCH_REFUSED:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            outcome.reason,
            EXIT_INVALID_INPUT,
        )
    run = outcome.run
    settled = run is not None and run.settled
    return CommandResult(
        _redact(
            {
                "command": command,
                "run_id": run_id,
                "outcome": outcome.verdict,
                "detail": outcome.reason,
                "run_status": None if run is None else run.status.value,
                "cancel_requested": True,
                "settled": settled,
                # The bounded facts, stated rather than implied. Every one of
                # them is what an operator must not assume the opposite of.
                "external_effect_reversed": False,
                "external_effect_established": False,
                "connector_stopped": False,
                "retry_authorized": False,
                "actor_authenticated": False,
                "next_step": (
                    "The cancellation is recorded and this generation will "
                    "never be claimed, bound or executed again. Cancelling "
                    "stops FUTURE work only: any effect that already happened "
                    "stands, and nothing here reverses or re-runs anything. If "
                    "the generation is still running, an execution already in "
                    "flight finishes under its own owner; if its outcome can "
                    "never be established, resolve it with 'jarvis task "
                    "orphan' and 'jarvis job resolve'."
                ),
            }
        ),
        EXIT_OK,
    )


def job_run_resolve_result(
    memory: MemoryStore,
    settings: Settings,
    run_id: str,
    attempt_id: str,
    actor_label: str,
    reason: str,
) -> CommandResult:
    """Close a scheduled generation whose one uncertain attempt was abandoned.

    The last step of the only path out of an unresolved external outcome, and
    like every other recovery verb it constructs a store and nothing else - no
    registry, no connector, no ``AgentCore``.

    It ends the generation; it does not end the uncertainty. Everything that
    records the doubt stays exactly where it is, and the response says so
    plainly so that a settled generation is never mistaken for a settled
    question.
    """
    command = "job-resolve"
    try:
        run = memory.resolve_unknown_job_run(
            run_id,
            attempt_id,
            now=datetime.now(timezone.utc),
            actor_label=actor_label,
            reason=reason,
        )
    except JobError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    return CommandResult(
        _redact(
            {
                "command": command,
                "run_id": run.id,
                "job_id": run.job_id,
                "generation": run.generation,
                "attempt_id": attempt_id,
                "run_status": run.status.value,
                "blocking_code": run.blocking_code,
                # The bounded facts. Every one of them is what the operator
                # must not assume the opposite of.
                "attempt_state": AttemptState.UNKNOWN_OUTCOME.value,
                "external_effect_established": False,
                "retry_authorized": False,
                "connector_replay": False,
                "monetary_exposure_released": False,
                "actor_authenticated": False,
                "next_step": (
                    "The generation is FAILED and its job is unblocked. The "
                    "attempt stays UNKNOWN_OUTCOME, the task stays failed with "
                    "the same typed uncertainty, and any monetary exposure "
                    "stays recorded: none of that is resolved by this, and "
                    "this generation will never run again."
                ),
            }
        ),
        EXIT_OK,
    )


#: Everything a malformed job definition can raise on its way in. All three
#: are the EXISTING validators refusing - the job grammar, the schedule engine
#: and the condition vocabulary - and this surface adds no fourth one of its
#: own. ``ValueError`` is here because ``validate_project_id`` raises it.
_JOB_DEFINITION_ERRORS = (JobError, ScheduleError, ConditionError, ValueError)


def _job_listing_payload(summary: JobSummary) -> dict[str, Any]:
    """One job as the store already redacted it. Adds no field of its own.

    Named for the LISTING rather than after its argument, unlike its session
    and task neighbours: the phase-boundary guards scan this module's SOURCE
    for the names of the scheduler storage operations app.py must never be
    able to call, and the store's own single-job read is one of them. The name
    stays out of the way of that proof on purpose - do not "fix" it back.

    A row that could not be rebuilt arrives with ``readable`` false and every
    derived field absent; it is printed exactly that way rather than hidden or
    guessed at, because a corrupt definition must stay VISIBLE to an operator
    while staying unrunnable to everything else.
    """
    return {
        "id": summary.id,
        "readable": summary.readable,
        "kind": summary.kind,
        "label": summary.label,
        "project_id": summary.project_id,
        "enabled": summary.enabled,
        "archived": summary.archived,
        # The revision an enable/disable/archive must name. It is printed
        # because those verbs REQUIRE it: the operator acts on the definition
        # they just read, never on whatever is latest.
        "revision": summary.revision,
        "timezone": summary.timezone,
        "next_due_at_utc": summary.next_due_at_utc,
        "last_run_id": summary.last_run_id,
        "updated_at": summary.updated_at,
    }


def job_create_result(
    memory: MemoryStore, settings: Settings, raw_spec_json: str | bytes
) -> CommandResult:
    """Create ONE durable job definition from an operator's JSON document.

    The document is the job specification's OWN persisted encoding, decoded by
    ``JobSpec.from_dict`` - the same default-deny rebuild storage uses when it
    reads a row back. That is the whole validation story: unknown or missing
    keys, an unknown kind, a naive instant, an interval under the floor, a
    condition on a non-watch, a watch carrying unconditional steps, a
    malformed step declaration and an oversized specification are all refused
    by the EXISTING contracts, and this command adds no grammar, no wizard, no
    schedule builder and no defaults of its own.

    CREATING IS NOT AUTHORITY. It materializes no task, requests no approval,
    consumes no grant, selects no connector, reserves no money and starts
    nothing; the response says so rather than leaving it to be assumed.
    """
    command = "job-create"

    def refused(message: str) -> CommandResult:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            message,
            EXIT_INVALID_INPUT,
        )

    try:
        document = json.loads(raw_spec_json)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        # The decoder's own message can echo the document; only the shape of
        # the failure is reported.
        return refused("The job specification is not valid JSON.")
    try:
        spec = JobSpec.from_dict(document)
        record = memory.create_job(spec, now=datetime.now(timezone.utc))
    except _JOB_DEFINITION_ERRORS as exc:
        return refused(str(exc))
    return CommandResult(
        _redact(
            {
                "command": command,
                "job_id": record.id,
                "revision": record.revision,
                "kind": record.spec.kind.value,
                "label": record.spec.label,
                "project_id": record.project_id,
                "enabled": record.enabled,
                "timezone": record.spec.timezone,
                # NULL is an honest answer: a one-time instant already past,
                # or a recurrence past its end, names no next occurrence.
                "next_due_at_utc": record.next_due_at_utc,
                "declared_steps": len(record.spec.steps),
                "declared_on_true_steps": len(record.spec.on_true),
                "condition_kind": (
                    None if record.spec.condition is None
                    else record.spec.condition.kind.value
                ),
                # The bounded facts, stated rather than implied.
                "execution_authorized": False,
                "approval_granted": False,
                "money_reserved": False,
                "next_step": (
                    "The definition is recorded and nothing has run. A "
                    "generation is created and executed only when the "
                    "scheduler is run explicitly ('jarvis scheduler run'), "
                    "and every permission, approval and billing gate still "
                    "applies to it then, unchanged."
                ),
            }
        ),
        EXIT_OK,
    )


def job_list_result(
    memory: MemoryStore,
    settings: Settings,
    limit: Any = None,
    *,
    include_archived: bool = False,
) -> CommandResult:
    """The durable job catalogue, oldest first. Reads only.

    Bounded by the same visibility ceiling every other listing uses, and by
    the store's own bound underneath it. Archived jobs are excluded unless
    asked for: they have no future, and a default listing is about what will
    happen next.
    """
    command = "jobs"
    try:
        bound = _bounded_limit(limit)
    except ValueError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    summaries = memory.list_jobs(bound, include_archived=bool(include_archived))
    return CommandResult(
        _redact(
            {
                "command": command,
                "limit": bound,
                "include_archived": bool(include_archived),
                "count": len(summaries),
                "jobs": [_job_listing_payload(summary) for summary in summaries],
            }
        ),
        EXIT_OK,
    )


def job_enablement_result(
    memory: MemoryStore,
    settings: Settings,
    job_id: str,
    revision: Any,
    enabled: bool,
) -> CommandResult:
    """Turn a job's FUTURE scheduling eligibility on or off.

    ``revision`` is the one the operator read in ``jarvis job list`` and is
    passed straight to storage's compare-and-set: a definition that moved in
    between is refused rather than acted on blindly. Enablement is STATE
    rather than definition, so it does not move the revision - the number that
    was read stays the number the next verb names.

    IT STOPS NOTHING THAT IS ALREADY RUNNING. Disabling clears the next due
    instant and nothing else - an open generation keeps its own lifecycle, and
    cancelling one is 'jarvis job cancel'. Enabling recalculates the next
    occurrence from now forward; occurrences missed while the job was off are
    not recovered.
    """
    command = "job-enable" if enabled else "job-disable"
    try:
        record = memory.set_job_enabled(
            job_id, revision, enabled, now=datetime.now(timezone.utc)
        )
    except _JOB_DEFINITION_ERRORS as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    return CommandResult(
        _redact(
            {
                "command": command,
                "job_id": record.id,
                "revision": record.revision,
                "enabled": record.enabled,
                "archived": record.archived,
                "next_due_at_utc": record.next_due_at_utc,
                "open_generation_stopped": False,
                "missed_occurrences_recovered": False,
                "next_step": (
                    "Future eligibility only. A generation that is already "
                    "open keeps its own lifecycle - stop one with 'jarvis job "
                    "cancel' - and occurrences the schedule named while the "
                    "job was disabled are never replayed."
                ),
            }
        ),
        EXIT_OK,
    )


def job_archive_result(
    memory: MemoryStore, settings: Settings, job_id: str, revision: Any
) -> CommandResult:
    """Retire a job from future scheduling, keeping every trace of it.

    Same compare-and-set on the revision the operator read. Archiving deletes
    nothing: no generation, no snapshot, no history. There is deliberately no
    un-archive and no delete - this ends a job's future, not its past.
    """
    command = "job-archive"
    try:
        record = memory.archive_job(job_id, revision)
    except _JOB_DEFINITION_ERRORS as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    return CommandResult(
        _redact(
            {
                "command": command,
                "job_id": record.id,
                "revision": record.revision,
                "archived": record.archived,
                "enabled": record.enabled,
                "next_due_at_utc": record.next_due_at_utc,
                "history_deleted": False,
                "open_generation_stopped": False,
                "reversible": False,
                "next_step": (
                    "The job has no future occurrences. Its definition and "
                    "every past generation stay readable, an already-open "
                    "generation keeps its own lifecycle, and there is no "
                    "un-archive."
                ),
            }
        ),
        EXIT_OK,
    )


def task_resolve_result(
    memory: MemoryStore,
    settings: Settings,
    session_id: str,
    task_id: str,
    attempt_id: str,
    resolution: str,
) -> CommandResult:
    """Abandon an attempt whose outcome will never be established.

    Local and deterministic like the visibility commands: it builds no
    registry, no connector and no ``AgentCore``, because recovery must be
    structurally incapable of executing anything. The only authority it
    exercises is the single storage transaction, which decides everything.

    The response states the bounded facts an operator needs to know they were
    not sold a retry: what the attempt still is, that no connector will run,
    what the task became, that the money stays exposed, and that the chain was
    not touched.
    """
    command = "task-resolve"
    try:
        recovery = AttemptRecovery(resolution)
    except ValueError:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            f"Unsupported resolution: {resolution}",
            EXIT_INVALID_INPUT,
        )
    outcome = memory.abandon_unknown_attempt(
        session_id, task_id, attempt_id, recovery
    )
    if outcome is not AttemptRecoveryOutcome.RECOVERED:
        code, message, exit_code = _RECOVERY_REFUSALS[outcome]
        return failure_result(
            command, settings.permission_mode, code, message, exit_code
        )
    return CommandResult(
        _redact(
            {
                "command": command,
                "session_id": session_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "resolution": recovery.value,
                "outcome": outcome.value,
                # The bounded facts, stated rather than implied.
                "attempt_state": AttemptState.UNKNOWN_OUTCOME.value,
                "connector_replay": False,
                "task_status": TaskStatus.FAILED.value,
                "error_code": ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value,
                # F3. Only what is UNIVERSALLY true. Storage returns a closed
                # recovery verdict, not a reservation receipt, so this surface
                # has no proof of what happened to THIS attempt's reservation
                # row - it may have had none at all, or an already RECONCILED
                # one that recovery correctly refused to reopen. Naming the
                # ACTIVE -> UNRESOLVED transition here asserted a fact about a
                # row nothing on this path read, so the field is gone rather
                # than made conditional on a guess. What remains holds in
                # every case, including the case of no reservation.
                "monetary_exposure_released": False,
                "new_reservation_created": False,
                "reconciled_reservation_reopened": False,
                "chain_modified": False,
                "next_step": (
                    "The attempt stays UNKNOWN_OUTCOME and will never be "
                    "replayed. The task is FAILED. No monetary exposure was "
                    "released and no reservation was created; recovery never "
                    "reopens a reconciled one. This command does not report "
                    "this attempt's reservation state - read the billing "
                    "position for that. A chain owning this task terminalizes "
                    "on its next chain run."
                ),
            }
        ),
        EXIT_OK,
    )


def chain_declare_result(
    memory: MemoryStore,
    settings: Settings,
    session_id: str,
    raw_declaration: str | bytes,
) -> CommandResult:
    """Declare ONE inert chain on an existing session.

    Order is the contract: the session is found, the declaration is fully
    parsed and validated, the one-chain rule is applied, the chain is built
    and judged - and only then is the session written, exactly once. A
    refusal at any point returns before ``save_session`` is reached, so a
    rejected declaration leaves the persisted session byte-identical.
    """
    command = "chain-declare"
    unknown = failure_result(
        command,
        settings.permission_mode,
        ErrorCode.UNKNOWN_SESSION,
        f"Unknown session: {session_id}",
        EXIT_NOT_FOUND,
    )
    occupied = failure_result(
        command,
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "This session already owns a chain.",
        EXIT_INVALID_INPUT,
    )
    session = memory.load_session(session_id)
    if session is None:
        return unknown
    # An EARLY REPORT, never the authority. It saves the caller a parse when
    # the answer is already obvious, but the real decision - and the only one
    # that is safe against a concurrent declarer - is made by storage, inside
    # one transaction, below. A check here could always be stale by the time
    # the write lands, which is exactly the lost update this no longer has.
    if session.chain is not None:
        return occupied
    try:
        steps = decode_chain_declaration(raw_declaration)
    except TaskInputError as exc:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    chain = ChainState.declared(steps)
    if chain is None:
        # The trusted constructor refused the steps. Unreachable through the
        # grammar above; kept because a chain is never persisted on the
        # strength of how it was built.
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            "Chain declaration refused: the declared steps do not form a chain.",
            EXIT_INVALID_INPUT,
        )
    verdict = memory.declare_chain_if_absent(session_id, chain)
    if verdict == "unknown":
        return unknown
    if verdict != "declared":
        # A concurrent declarer won. Report the same one-chain refusal, and
        # touch nothing: the winner's chain is already persisted and this
        # caller has no authority over it.
        return occupied
    stored = memory.load_session(session_id)
    return CommandResult(
        build_envelope(command, settings.permission_mode, session=stored), EXIT_OK
    )


def _display_database(path: Path) -> str:
    """The database path, home-relative where that is safe, else bounded.

    A configuration report is not a place to hand out an operator's full
    filesystem layout. Anything under the home directory is shown relative to
    it, which is both shorter and less revealing; anything else is reduced to
    a marker, because the exact location is not what the operator is checking
    here.
    """
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return "[PATH]"
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return "[PATH]"


def _config_payload(command: str, settings: Settings, result) -> dict[str, Any]:
    """The bounded, effective view of configuration. Never the raw document.

    Reports what the runtime WILL use, not what the file happens to contain -
    on a refused store those differ, and the effective posture is the fact an
    operator needs. No raw file content, no path beyond the bounded form
    above, no credential-shaped value, and no key outside the closed set.
    """
    return {
        "command": command,
        "config_status": result.status.value,
        "config_version": CONFIG_VERSION,
        "config_reason": result.reason,
        "execution_enabled": settings.execution_enabled,
        "permission_mode": settings.permission_mode.value,
        "paid_api_enabled": settings.paid_api_enabled,
        "paid_api_approval_id_present": settings.paid_api_approval_id is not None,
        "claude_call_budget_usd": settings.claude_call_budget_usd,
        "approval_ttl_minutes": settings.approval_ttl_minutes,
        "skills_enabled": settings.skills_enabled,
        # Manual Claude selection. Both are validated, non-secret data in
        # their own bounded grammar, so the values themselves are shown;
        # null means the CLI's own default stands.
        "claude_model": settings.claude_model,
        "claude_effort": settings.claude_effort,
        "memory_extract_model": settings.memory_extract_model,
        "memory_database": _display_database(settings.memory_database),
        # Stated because it is the question this surface most invites, and
        # the answer is permanent: the monthly cap is not configuration.
        "monthly_hard_limit_usd": MONTHLY_HARD_LIMIT_USD,
        "monthly_hard_limit_configurable": False,
    }


def config_show_result(store: ConfigStore, settings_result) -> CommandResult:
    """Report the effective configuration and the store's health.

    Available even when the document is refused - especially then. An
    operator whose store is broken needs to be told so, shown what the
    runtime is actually doing instead, and pointed at the repair; a command
    that simply failed would tell them none of that.
    """
    payload = _config_payload("config-show", settings_result.settings, settings_result)
    if settings_result.usable:
        return CommandResult(_redact(payload), EXIT_OK)
    payload["next_step"] = (
        "The stored configuration was refused, so a fail-closed posture is "
        "in effect: execution is off and approvals are strict. Repair it "
        "with 'jarvis config reset --all'."
    )
    return CommandResult(_redact(payload), EXIT_POLICY)


def config_mutation_result(
    command: str, store: ConfigStore, settings: Settings, settings_result
) -> CommandResult:
    """Report a completed mutation as the new effective configuration."""
    payload = _config_payload(command, settings, settings_result)
    if command == "config-reset-all" and settings.execution_enabled:
        # Said plainly, because a repair that silently switches execution
        # back on is a surprise an operator should never get from a command
        # they ran to fix a file.
        payload["next_step"] = (
            "The configuration was reset to defaults. Execution is now "
            "ENABLED and the permission mode is 'session'."
        )
    return CommandResult(_redact(payload), EXIT_OK)


def config_failure_result(
    command: str, permission_mode: PermissionMode, error: ConfigError
) -> CommandResult:
    """A bounded configuration refusal: a code and a short reason, no more."""
    return failure_result(
        command,
        permission_mode,
        error.code,
        error.message,
        _ERROR_EXIT.get(error.code, EXIT_POLICY),
    )


def chain_cancel_result(
    memory: MemoryStore, settings: Settings, session_id: str
) -> CommandResult:
    """Cancel the session's chain through the EXISTING chain authority.

    This surface decides nothing. It resolves which chain the session owns,
    hands both ids to ``cancel_chain``, and reports the verdict that came
    back. Every rule that matters already lives in that one transaction: the
    chain must exist, must be the one named, and must still be ACTIVE, and
    the cursor and every bound task id are preserved because cancellation
    stops what has not started rather than undoing what has.

    Two things it deliberately does NOT do, because the existing authority
    does not:

    * it does not touch the currently live task. A chain cancellation is not
      a task cancellation; ordinary ``cancel`` owns that, with the H2 rules
      S2A and S2B established. Terminalizing a running task from here would
      be exactly the false CANCELLED those milestones exist to prevent.
    * it does not suppress the successor by writing anything. It does not
      need to: once the chain is no longer ACTIVE, every materialize and
      advance refuses on its own precondition, so no step can be raced into
      existence behind this call.

    Local-only by construction. Routed beside ``declare`` and ``show``, so no
    registry, connector or ``AgentCore`` is built to cancel a chain - the
    isolation is structural rather than merely intended.
    """
    command = "chain-cancel"
    unknown = failure_result(
        command,
        settings.permission_mode,
        ErrorCode.UNKNOWN_SESSION,
        f"Unknown session: {session_id}",
        EXIT_NOT_FOUND,
    )
    inactive = failure_result(
        command,
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "This session owns no active chain; nothing was cancelled.",
        EXIT_INVALID_INPUT,
    )
    try:
        session = memory.load_session(session_id)
    except VISIBILITY_DECODE_ERRORS:
        # An unreadable session row is not an invitation to guess which chain
        # it meant. It reads as the same bounded refusal a missing one does:
        # which of the two it was is not a fact this surface may disclose,
        # and no traceback reaches the operator either way.
        return unknown
    if session is None:
        return unknown
    chain = session.chain
    if chain is None:
        return inactive
    # The id comes from the session and is handed straight back to storage,
    # which re-reads it inside the write transaction. A chain replaced between
    # the two loses there as a conflict rather than being cancelled by
    # mistake, so this read is a lookup and never an authority.
    verdict = memory.cancel_chain(session_id, chain.id)
    if verdict == "unknown":
        return unknown
    if verdict != "cancelled":
        # "terminal" and "conflict" alike: the chain named is not the ACTIVE
        # one any more, and storage wrote nothing. One message for both -
        # whether it finished, was already cancelled or was replaced is not
        # something a refusal needs to say.
        return inactive
    return CommandResult(
        _redact(
            {
                "command": command,
                "session_id": session_id,
                # The bounded facts an operator needs, and no declaration:
                # the steps are their own input and are echoed by declare and
                # show, but a cancellation has no reason to repeat them.
                "chain_status": ChainStatus.CANCELLED.value,
                "chain_cancelled": True,
                "tasks_modified": False,
                "connector_called": False,
                "successor_started": False,
                "billing_modified": False,
                "next_step": (
                    "The chain is CANCELLED, so no further step will be "
                    "materialized or advanced. Work already started is "
                    "untouched: cancel a running task with 'cancel', and "
                    "resolve an attempt whose outcome is unknown with "
                    "'task resolve'."
                ),
            }
        ),
        EXIT_OK,
    )


def chain_show_result(
    memory: MemoryStore, settings: Settings, session_id: str
) -> CommandResult:
    """Report the session's chain. Reads only; it advances nothing."""
    command = "chain-show"
    session = memory.load_session(session_id)
    if session is None:
        return failure_result(
            command,
            settings.permission_mode,
            ErrorCode.UNKNOWN_SESSION,
            f"Unknown session: {session_id}",
            EXIT_NOT_FOUND,
        )
    return CommandResult(
        build_envelope(command, settings.permission_mode, session=session), EXIT_OK
    )


# --------------------------------------------------------------------------
# Command facade
# --------------------------------------------------------------------------


#: Exit code and error code of each pre-task understanding outcome. Every
#: one of them means NOTHING external ran; the run envelope, when there is
#: one, carries Core's own verdict instead.
_INTENT_EXIT: dict[IntentStatus, tuple[int, ErrorCode | None]] = {
    IntentStatus.CLARIFICATION_REQUIRED: (EXIT_AWAITING_CLARIFICATION, None),
    IntentStatus.UNSUPPORTED: (EXIT_INVALID_INPUT, ErrorCode.NO_CONNECTOR),
    IntentStatus.AUTHORITY_UNAVAILABLE: (EXIT_POLICY, ErrorCode.INVALID_TASK),
    IntentStatus.MALFORMED_MODEL_OUTPUT: (EXIT_INTERNAL, ErrorCode.CONNECTOR_FAILED),
    IntentStatus.MODEL_UNAVAILABLE: (EXIT_INTERNAL, ErrorCode.CONNECTOR_UNAVAILABLE),
    IntentStatus.MODEL_TIMEOUT: (EXIT_INTERNAL, ErrorCode.CONNECTOR_FAILED),
    IntentStatus.MODEL_CANCELLED: (EXIT_CANCELLED, ErrorCode.CANCELLED),
    IntentStatus.MODEL_FAILED: (EXIT_INTERNAL, ErrorCode.CONNECTOR_FAILED),
    IntentStatus.EXECUTION_DISABLED: (EXIT_POLICY, ErrorCode.EXECUTION_DISABLED),
    IntentStatus.INVALID_INPUT: (EXIT_INVALID_INPUT, ErrorCode.INVALID_TASK),
    IntentStatus.RESEARCH_UNAVAILABLE: (EXIT_INTERNAL, ErrorCode.CONNECTOR_UNAVAILABLE),
    IntentStatus.RESEARCH_FAILED: (EXIT_INTERNAL, ErrorCode.CONNECTOR_FAILED),
}

#: Why Web Research could not run, as stable non-secret codes for the interface.
RESEARCH_PROVIDER_UNSUPPORTED = "research_provider_unsupported"
#: The ``chat_reply_too_long`` verdict's sentence (the interface says it in its own words).
_TOO_LONG_MESSAGE = ("The reply was longer than JARVIS keeps in one message (about 50,000 characters), so it was "
                     "cut there; the part shown was kept. Nothing was executed.")

#: The execution barrier on the free-text entry: the request was understood,
#: and nothing authoritative followed.
_FROZEN_UNDERSTOOD_MESSAGE = (
    "JARVIS is frozen, so this request was understood but not started: no "
    "task or job was created and nothing was queued. Resume JARVIS and send "
    "it again to run it."
)
#: K3: the proposal could not be computed. Path-free: the cause is never said.
_NO_FOLDER_PROPOSAL = "JARVIS could not propose a folder for this project. Nothing was created."


def _project_folder_document(document: Any) -> bool:
    """K3 (E1): exactly objective, capability and a scope whose target is
    "" or "desktop" - no read window, no other field."""
    scope = document.get("scope") if isinstance(document, dict) else None
    return (set(document or ()) == {"objective", "capability", "scope"} and isinstance(scope, dict)
            and set(scope) == {"action", "target", "destination"} and scope["target"] in PROJECT_FOLDER_TARGETS)


def _instruction_chars(instruction: str | None) -> int:
    """What the instruction block adds to one understanding call's payload."""
    if not instruction:
        return 0
    return argv_chars(json_text({PROJECT_INSTRUCTION_KEY: project_instruction_block(instruction)}))


#: The understanding call's context key for Core's typed memory (V2.1a M3b).
CHAT_MEMORY_KEY = "retrieved_memory"


def _memory_chars(memory: Mapping[str, Any]) -> int:
    """What the typed memory block adds to one understanding call's payload."""
    return argv_chars(json_text(memory)) if memory else 0


def text_result(
    permission_mode: PermissionMode,
    status: IntentStatus,
    request_id: str,
    *,
    message: str = "",
    question: str = "",
    capability: str = "",
    destination: str = "",
    objective: str = "",
    reply: str = "",
    reason: str = "",
    usage: dict[str, Any] | None = None,
    sources: Iterable[dict[str, str]] = (),
    sources_cited: bool = False,
    choices: Iterable[dict[str, str]] = (),
    sections: Iterable[dict[str, Any]] = (),
    run: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    exit_code: int | None = None,
) -> CommandResult:
    """The ONE envelope of the free-text entry.

    Its shape is fixed: the understanding verdict in ``intent``, and then
    EXACTLY the existing run envelope (unchanged, its own version) under
    ``run`` when a task was created, or the existing job-create payload under
    ``job`` when a job was. A consumer therefore reads one new block and
    everything it already knew how to read; no existing envelope changes.
    """
    code, error_code = _INTENT_EXIT.get(status, (EXIT_OK, None))
    if exit_code is not None:
        code = exit_code
    error = None
    if error_code is not None:
        error = {"code": error_code.value, "message": message, "retryable": False}
    sections = [dict(section) for section in sections]
    payload = {
        "command": "text",
        "effective_permission_mode": permission_mode.value,
        "intent": {
            "status": status.value,
            "request_id": request_id,
            "message": message or None,
            "question": question or None,
            "capability": capability or None,
            "destination": destination or None,
            "objective": objective or None,
            # General Chat: the model's conversational text, when the verdict
            # is ``conversation``. Something to show, never something done.
            "reply": reply or None,
            # A stable, non-secret code for an ESTABLISHED failure cause, so the
            # interface can explain it in the person's language. Never prose.
            "reason": reason or None,
            # The understanding call's own usage: the token fields the provider
            # REPORTED (none invented) and measured character sizes of the
            # request, the conversation window and the catalogue it carried.
            "usage": usage or None,
            # Web Research: the public pages the search REALLY returned that
            # back the reply (``sources_cited``), or - when the reply named
            # none of them - the returned results themselves. Untrusted links.
            "sources": [dict(source) for source in sources],
            "sources_cited": bool(sources_cited),
            # The configured destinations a clarification asks the user to
            # choose between, each with the exact text that selects it. Built
            # from the grant rows only; empty when no choice is pending.
            "choices": [dict(choice) for choice in choices],
        },
        "run": run,
        "job": job,
        "error": error,
    }
    if sections:
        # "Reply to each group separately": one section per group, in order, each
        # from its OWN call - the answer, or why it has none. Only present then,
        # so every other envelope keeps its exact shape.
        payload["intent"]["sections"] = sections
    return CommandResult(_redact(payload), code)


def _model_usage(model: Any, request_id: str) -> dict[str, Any]:
    """What a model adapter REPORTED about one call: the provider's own token
    fields and answering model (``reported``), the plan limits it stated
    (``rate_limits``) and which provider answered. Nothing is estimated."""
    reported = getattr(model, "reported", None)
    observed = getattr(model, "observed", None)
    found: dict[str, Any] = {
        "provider": getattr(model, "provider", None) or None,
        "reported": dict(reported.get(request_id) or {}) if isinstance(reported, dict) else {},
    }
    seen = observed.get(request_id) if isinstance(observed, dict) else None
    for key in ("rate_limits", "quota", "requested_model"):
        value = seen.get(key) if isinstance(seen, dict) else None
        if value:
            found[key] = dict(value) if isinstance(value, dict) else value
    return found


def _turn_timing(turn: Mapping[str, Any], now: float) -> dict[str, Any]:
    """DFSPEED: one text turn's phases in whole milliseconds, from ``time.monotonic()``
    marks - measured, never estimated; a phase that did not happen is absent.
    ``spawn_ms`` / ``first_visible_ms`` count from the turn's start, ``auth_ms`` is the
    auth check, ``first_text_ms`` the CLI spawn to the first streamed text, ``text_ms``
    the first to the last, ``parse_ms`` the model's return to the validated verdict."""
    def ms(a: float, b: float) -> int:
        return max(0, round((b - a) * 1000))

    start = turn["started"]
    timings = getattr(turn["model"], "timings", None)
    call = timings.get(turn["request_id"]) if isinstance(timings, dict) else None
    call = call if isinstance(call, dict) else {}
    timing: dict[str, Any] = {"total_ms": ms(start, now)}
    if "auth_started" in call and "auth_ended" in call:
        timing["auth_ms"] = ms(call["auth_started"], call["auth_ended"])
    if "spawned" in call:
        timing["spawn_ms"] = ms(start, call["spawned"])
        if "first_text" in call:
            timing["first_text_ms"] = ms(call["spawned"], call["first_text"])
            timing["first_visible_ms"] = ms(start, call["first_text"])
            timing["text_ms"] = ms(call["first_text"], call["last_text"])
    if "returned" in turn:
        timing["parse_ms"] = ms(turn["returned"], turn.get("validated", now))
    chars = call.get("output_chars", turn.get("output_chars"))
    if type(chars) is int:
        timing["output_chars"] = chars
    return timing


class MemoryTriggers:
    """Process-wide SessionEnd / PreCompact capture worker (V2.1a M2).

    It only schedules: Core decides every record. Each conversation has at
    most one draining thread; jobs that arrive meanwhile queue behind it. A
    capture failure is swallowed here - it never reaches the chat or the
    compaction that triggered it (I12). Process-wide because the UI backend
    builds a fresh ``JarvisApp`` per call.

    A job holds the app, or a zero-argument callable resolving it when the
    job runs (``None``: skip), so a model or budget change applies to the
    next capture. Every capture write goes through one ``CaptureStop``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, list[tuple[Any, MemoryRecordOrigin, tuple[Any, ...] | None]]] = {}
        self._threads: dict[str, threading.Thread] = {}
        # ponytail: process-local high-water mark of seqs handed to capture;
        # a restart re-offers turns and Core's run-id ledger dedupes them.
        self._captured: dict[str, int] = {}
        self._closed = False
        self._stop = CaptureStop()
        #: The app each conversation's capture last ran with: shutdown aborts
        #: through the store that run writes to, without reading config.
        self._apps: dict[str, Any] = {}
        #: The last PreCompact batch Core skipped as too short or reply-only
        #: (or failed on before any write): those turns are folded, so
        #: SessionEnd never re-reads them. It rides in front of the next
        #: PreCompact or SessionEnd batch, up to Core's turn cap (a skipped
        #: batch is within it). Any other outcome drops it, so no turn is
        #: offered again after a run that could have written records.
        self._carry: dict[str, tuple[Any, ...]] = {}

    def schedule(self, app: Any, conversation_id: str, trigger: MemoryRecordOrigin,
                 turns: Iterable[Any] | None = None) -> bool:
        with self._lock:
            if self._closed or not conversation_id:
                return False
            self._jobs.setdefault(conversation_id, []).append(
                (app, trigger, None if turns is None else tuple(turns)))
            if conversation_id in self._threads:
                return True
            thread = threading.Thread(target=self._drain, args=(conversation_id,),
                                      name=f"jarvis-capture-{conversation_id}", daemon=True)
            self._threads[conversation_id] = thread
            thread.start()  # under the lock: shutdown never sees an unstarted thread
        return True

    def _drain(self, conversation_id: str) -> None:
        while True:
            with self._lock:
                jobs = self._jobs.pop(conversation_id, [])
                if not jobs:
                    self._threads.pop(conversation_id, None)
                    return
            for app, trigger, turns in jobs:
                try:
                    self._run(app, conversation_id, trigger, turns)
                except Exception:  # noqa: BLE001 - capture never blocks the chat (I12)
                    pass

    def _mark(self, conversation_id: str, turns: Iterable[Any], outcome: Any) -> None:
        # Only a run Core finished (or already had) moves the mark; a
        # provider/run-store refusal leaves the turns for the next leave.
        if getattr(outcome, "outcome", None) not in ("ok", "duplicate"):
            return
        top = max((turn.seq for turn in turns), default=0)
        with self._lock:
            self._captured[conversation_id] = max(self._captured.get(conversation_id, 0), top)

    def _with_carry(self, conversation_id: str, turns: tuple[Any, ...]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """(the carried turns + the new ones, the new ones). A re-offered turn
        is not new; a carry the new turns do not continue (seqs) is dropped;
        the oldest carried turns give way to Core's cap, the new ones never."""
        with self._lock:
            carry = self._carry.get(conversation_id, ())
        new = tuple(turn for turn in turns if not carry or turn.seq > carry[-1].seq)
        if new and carry and new[0].seq != carry[-1].seq + 1:
            carry = ()
        room = RAW_TURNS_MAX - len(new)
        return (carry[-room:] if room > 0 else ()) + new, new

    def _carry_on(self, conversation_id: str, batch: tuple[Any, ...], outcome: Any) -> None:
        with self._lock:
            # A provider or run-store failure wrote nothing either: the folded
            # turns ride on rather than being lost.
            if getattr(outcome, "outcome", None) in (
                "skipped:too_short", "skipped:empty", "provider_error", "run_store_error"
            ):
                self._carry[conversation_id] = batch
            else:
                self._carry.pop(conversation_id, None)

    def _run(self, app: Any, conversation_id: str, trigger: MemoryRecordOrigin,
             turns: tuple[Any, ...] | None) -> None:
        if self._stop.stopped:  # halted: not even a resolver's store opens
            return
        app = app() if callable(app) else app
        if app is None:
            return
        with self._lock:
            self._apps[conversation_id] = app
        model = app.intent_model
        if model is None:
            return
        stop = self._stop
        if trigger is MemoryRecordOrigin.CAPTURE_PRECOMPACT:
            batch, new = self._with_carry(conversation_id, turns or ())
            if new:
                outcome = app.core.capture_memory(conversation_id, trigger, model, turns=batch, stop=stop)
                self._mark(conversation_id, batch, outcome)
                self._carry_on(conversation_id, batch, outcome)
            return
        raw = app.memory.load_conversation_state(conversation_id).turns
        with self._lock:
            mark = self._captured.get(conversation_id, 0)
        fresh = tuple(turn for turn in raw if turn.seq > mark)
        batch, new = self._with_carry(conversation_id, fresh)
        if batch:
            if batch == raw:
                outcome = app.core.capture_memory(conversation_id, trigger, model, stop=stop)
            else:
                outcome = app.core.capture_memory(conversation_id, trigger, model, turns=batch, stop=stop)
            self._mark(conversation_id, batch, outcome)
            # Skipped: the raw turns stay raw (the next leave re-reads them);
            # only the folded ones keep riding.
            self._carry_on(conversation_id, batch[:len(batch) - len(new)], outcome)
        record = app.memory.load_conversation(conversation_id)
        if record is not None and record.project_id:
            with stop.write():
                app.memory.consolidate_scope(record.project_id, utc_now())

    def halt(self, timeout: float = 0.0) -> bool:
        """Close for good: once this returns no capture write starts (one in
        progress gets ``timeout``; ``False`` if it outlived that) and announced
        model calls are cancelled."""
        with self._lock:
            self._closed = True
        return self._stop.stop(timeout)

    def shutdown(self, app: Any, open_ids: Iterable[str] = (), timeout: float = 5.0) -> tuple[str, ...]:
        """Session-end the open conversations, wait for captures, halt, then
        have Core record every run still open aborted - all within ``timeout``
        seconds; no capture writes after this returns. ``app`` (or what it
        resolves to; ``None`` is fine) only aborts a conversation whose
        capture never resolved an app. Returns the conversations whose open
        runs Core recorded aborted. Already halted (a quit that returned):
        records nothing."""
        if self._stop.stopped:
            return ()
        deadline = time.monotonic() + max(0.0, timeout)
        reserve = min(0.5, max(0.0, timeout) / 5)  # for the halt: a write in progress finishes
        for conversation_id in open_ids:
            self.schedule(app, conversation_id, MemoryRecordOrigin.CAPTURE_SESSION_END)
        with self._lock:
            self._closed = True
            threads = dict(self._threads)
        for thread in threads.values():
            thread.join(max(0.0, deadline - reserve - time.monotonic()))
        self.halt(max(0.0, deadline - time.monotonic()))
        with self._lock:
            apps = {cid: self._apps.get(cid) for cid in threads}
        aborted: list[str] = []
        for conversation_id, ran_with in apps.items():
            try:
                target = ran_with if ran_with is not None else (app() if callable(app) else app)
                if target is not None and target.core.abort_capture_runs(conversation_id):
                    aborted.append(conversation_id)
            except Exception:  # noqa: BLE001 - shutdown proceeds regardless
                continue
        return tuple(aborted)


MEMORY_TRIGGERS = MemoryTriggers()

#: Conversations with a reply in flight in this process (per-call ``JarvisApp``s
#: share it), so "Oturumu sıkıştır" never compacts under a streaming reply.
_REPLYING: Counter[str] = Counter()
_REPLYING_LOCK = threading.Lock()


@contextmanager
def _replying(conversation_id: str | None):
    if not conversation_id:
        yield
        return
    with _REPLYING_LOCK:
        _REPLYING[conversation_id] += 1
    try:
        yield
    finally:
        with _REPLYING_LOCK:
            _REPLYING[conversation_id] -= 1
            if _REPLYING[conversation_id] <= 0:
                del _REPLYING[conversation_id]


#: V2.1b history import. Previewed plans and the cancel flag of each running
#: import are process-wide: the UI backend builds a fresh ``JarvisApp`` per call.
IMPORT_BATCH_MAX_ITEMS = 20
IMPORT_BATCH_MAX_CHARS = 24_000
_IMPORT_PLANS_MAX = 4
#: Ledger rows one batch may claim: the first run plus retries of a failed one.
_IMPORT_RUN_ATTEMPTS = 3
#: The adapter-facing capability, the same one Core's capture uses.
_IMPORT_CAPABILITY = "memory_capture"
_IMPORT_TOOL = "memory_import"
_IMPORT_PLANS: dict[str, ImportPlan] = {}
_IMPORT_CANCEL: dict[str, threading.Event] = {}
_IMPORT_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class _ImportTurn:
    """One imported item shown to the capture model as DATA; never a stored turn."""

    id: str
    role: str
    kind: str
    text: str


def _import_batches(items: Iterable[Any]) -> list[tuple[_ImportTurn, ...]]:
    """Items in batches of at most ``IMPORT_BATCH_MAX_ITEMS`` items and
    ``IMPORT_BATCH_MAX_CHARS`` characters, in order. Ids come from the content
    hash, so the same export gives the same batches (the run ledger dedupes)."""
    batches: list[tuple[_ImportTurn, ...]] = []
    batch: list[_ImportTurn] = []
    size = 0
    for item in items:
        # ponytail: an item over the batch cap is clipped, not split; split it
        # into several turns if long imported messages lose memories.
        text = item.text[:IMPORT_BATCH_MAX_CHARS]
        if batch and (len(batch) >= IMPORT_BATCH_MAX_ITEMS or size + len(text) > IMPORT_BATCH_MAX_CHARS):
            batches.append(tuple(batch))
            batch, size = [], 0
        batch.append(_ImportTurn(f"i{len(batch)}-{item.content_hash[:16]}", item.role, "imported", text))
        size += len(text)
    if batch:
        batches.append(tuple(batch))
    return batches


class JarvisApp:
    """Thin command facade over ``AgentCore``; it holds no loop policy."""

    def __init__(
        self,
        core: AgentCore,
        memory: MemoryStore,
        settings: Settings,
        intent_model: ModelAdapter | None = None,
        research_model: ModelAdapter | None = None,
    ) -> None:
        self.core = core
        self.memory = memory
        self.settings = settings
        #: The understanding model, or ``None`` when none is composed. Never
        #: a registered connector: it interprets, it does not execute.
        self.intent_model = intent_model
        #: The read-only public Web Research model, or ``None`` when the
        #: composed provider has no verified research route. Never registered.
        self.research_model = research_model
        self._cancelled_text: set[str] = set()
        self.memory_triggers = MEMORY_TRIGGERS

    def watch_conversations(self, store: Any, resolve: Callable[[], Any] | None = None) -> None:
        """PreCompact: capture exactly the turns ``store`` is about to compact,
        with the app ``resolve()`` returns when the capture runs (default this
        one). Gated like Core: no capture thread for a conversation Core would
        refuse (``capture_gate_open``)."""
        triggers, target = self.memory_triggers, resolve or self

        def hook(conversation_id: str, turns: tuple[Any, ...]) -> None:
            if capture_gate_open(store, conversation_id):
                triggers.schedule(target, conversation_id, MemoryRecordOrigin.CAPTURE_PRECOMPACT, turns)
        register_precompact_hook(store, hook)

    def conversation_left(self, conversation_id: str) -> bool:
        """SessionEnd: switch, new conversation or archive. Delete never calls this."""
        return self.memory_triggers.schedule(self, conversation_id, MemoryRecordOrigin.CAPTURE_SESSION_END)

    def shutdown_memory_capture(self, open_ids: Iterable[str] = (), timeout: float = 5.0) -> tuple[str, ...]:
        return self.memory_triggers.shutdown(self, open_ids, timeout)

    @property
    def permission_mode(self) -> PermissionMode:
        return self.settings.permission_mode

    def submit_text(self, text: str, session_id: str | None = None, **kwargs: Any) -> CommandResult:
        """``_submit_text``, with its conversation marked as replying meanwhile."""
        with _replying(kwargs.get("conversation_id")):
            return self._submit_text(text, session_id, **kwargs)

    def _submit_text(
        self,
        text: str,
        session_id: str | None = None,
        *,
        previous_text: str = "",
        request_id: str | None = None,
        response_language: str = "",
        conversation: Iterable[Mapping[str, str]] = (),
        context_groups: Iterable[Mapping[str, Any]] = (),
        context_attachments: Iterable[Mapping[str, Any]] = (),
        response_mode: str = DEFAULT_RESPONSE_MODE,
        on_reply_preview: Callable[[str], None] | None = None,
        conversation_id: str | None = None,
    ) -> CommandResult:
        """Free text in, the authoritative envelope out.

        The pre-task understanding boundary. The model proposes, this method
        validates the proposal against the catalog built from the composed
        registry and grant table, ``decode_task_json`` judges the resulting
        document exactly as typed JSON, and ``AgentCore.run`` decides every
        approval, attestation and billing question as it always has. No
        outcome of the understanding step is ever an unknown outcome: no
        connector is reached before the task exists.

        ``previous_text`` carries the earlier turn when the caller is
        answering a clarification; the seam is stateless on purpose, so a
        stale clarification can never mutate a later task or session.
        ``request_id`` is the caller's handle for ``cancel_text``.

        The same call is General Chat: a message that asks for nothing to be
        done comes back as a ``conversation`` verdict carrying the model's
        reply - no task, job, grant or connector, and nothing to approve.
        ``response_language`` (a language tag, e.g. the interface language)
        is only the default for that reply; the user's own language wins.
        ``conversation`` is the caller's canonical conversation - a
        ``jarvis.conversation.ConversationState`` (turns, compacted digests,
        the person's active pins) or, from older callers, a plain list of
        ``{role, text}`` turns. It is assembled here, inside the answering
        provider's reported budget, into a checkpoint, the active pins and the
        recent turns verbatim (``jarvis.conversation.assemble_context``). It is
        data: only the CURRENT turn can select a destination.

        ``context_groups`` are the quotes the person attached to THIS turn,
        each owning its own note and files; ``context_attachments`` are files
        that belong to the message as a whole (``jarvis.context_items``).
        ``response_mode`` says whether the groups are evaluated together or
        answered separately. All of it is context: it never selects a
        destination, never grants anything and never changes what JARVIS may
        do - the person's own message does that.

        ``conversation_id`` names the persisted conversation the request came
        from. Its owner is then the ONLY source of project scope: the task's
        ``project_id`` (hence session binding and project memory) and the
        catalog's job project are the owner, Global = none. An unknown
        conversation, an archived owner or a session bound to another scope
        is refused before anything runs. Scope is context, never authority.
        """
        request_id = request_id or str(uuid4())
        mode = self.settings.permission_mode

        # What the call really cost in tokens, as the provider reported it, and
        # the measured sizes of what was sent - filled only after the call.
        usage: dict[str, Any] = {}
        # DFSPEED: when this turn's phases happened; ``timing`` in ``usage`` once a model is asked.
        turn: dict[str, Any] = {"started": time.monotonic(), "request_id": request_id}

        def respond(*args: Any, **kwargs: Any) -> CommandResult:
            if "model" in turn:
                usage["timing"] = _turn_timing(turn, time.monotonic())
            return text_result(*args, usage=usage, **kwargs)

        def refused(status: IntentStatus, message: str, reason: str = "") -> CommandResult:
            return respond(mode, status, request_id, message=message, reason=reason)

        reason = intent_text_refusal(text)
        if reason is None and previous_text:
            reason = intent_text_refusal(previous_text)
        if reason is not None:
            return refused(IntentStatus.INVALID_INPUT, f"The request was refused: {reason}.")
        # Freeze gates AUTHORITATIVE work, not conversation: the understanding
        # call may run while execution is disabled (it has no tools and no
        # authority), and the barrier stands after the verdict, ahead of
        # anything that would create a task or a job.
        session = None
        if session_id is not None:
            session = self.memory.load_session(session_id)
            if session is None:
                return respond(
                    mode, IntentStatus.INVALID_INPUT, request_id,
                    message=f"Unknown session: {session_id}", exit_code=EXIT_NOT_FOUND,
                )
        scoped = conversation_id is not None
        owner = session.project_id if session is not None else None
        if scoped:
            owner, reason = self._conversation_scope(conversation_id, session)
            if reason is not None:
                return refused(IntentStatus.INVALID_INPUT, f"{reason} Nothing was executed.")
        model = self.intent_model
        if model is None:
            return refused(
                IntentStatus.MODEL_UNAVAILABLE,
                "No understanding model is composed into this build.",
            )
        availability = model.availability()
        if not availability.available:
            return refused(
                IntentStatus.MODEL_UNAVAILABLE,
                availability.reason or "The understanding model is unavailable.",
            )
        groups, attachments = list(context_groups), list(context_attachments)
        # One canonical conversation, read more than once below: never a one-shot iterator.
        if not isinstance(conversation, ConversationState):
            conversation = list(conversation)
        items = context_window(groups, attachments)
        carried = bool(items["groups"] or items["global_attachments"])
        now = datetime.now().astimezone()
        catalog = build_catalog(
            self.core.registry,
            self.core.destination_policy,
            self.memory.list_projects(),
            owner,
            now,
            conversation_scoped=scoped,
        )
        # Gate 6H H3 (K3): a project folder is offered only to the chat of an
        # active project that has none - never Global, the CLI or a linked one.
        if catalog.entry(PROJECT_FOLDER_CAPABILITY) is not None and not (scoped and self._folderless(owner)):
            catalog = replace(catalog, entries=tuple(
                entry for entry in catalog.entries if entry.capability != PROJECT_FOLDER_CAPABILITY))
        # Gate 4: the scoped project's current instruction, read from the
        # store NOW (never from conversation text or model output), delivered
        # iff eligible for this call's own classification. Guidance only.
        intent_scope = ActionScope(action=INTENT_ACTION, destination=DEFAULT_TASK_DESTINATION)
        instruction = self._intent_instruction(owner, intent_scope, usage)
        memory = self._chat_memory(owner, session, conversation_id, text, intent_scope)
        turn["model"] = model
        if items_response_mode(response_mode) == "separate" and len(items["groups"]) >= 2:
            # Strict isolation: one call per group, merged into one reply.
            return self._answer_groups_separately(
                text, request_id, mode, previous_text, response_language, conversation,
                groups, attachments, items, catalog, on_reply_preview, usage, respond, refused,
                instruction, memory,
            )
        # A file named again from an earlier turn comes back as data with its
        # provenance - never merged into this turn's groups.
        present = [str(a.get("name") or "") for g in items["groups"] for a in g["attachments"]]
        present += [str(a.get("name") or "") for a in items["global_attachments"]]
        rehydrated = rehydrate(conversation, text, present)
        turn_context = {
            "previous_request": previous_text,
            "response_language": response_language_hint(response_language),
            "context_groups": items["groups"],
            "global_attachments": items["global_attachments"],
            "response_mode": items_response_mode(response_mode) if items["groups"] else "",
            "local_time": catalog.local_time,
        }
        if rehydrated:
            turn_context["rehydrated_references"] = rehydrated
        plan = assemble_context(conversation, context_profile_of(model), text,
                                extra_chars=argv_chars(json_text(turn_context)) + argv_chars(json_text(catalog.to_dict()))
                                + _instruction_chars(instruction) + _memory_chars(memory))
        intent_task = Task(
            objective=text,
            capability=INTENT_CAPABILITY,
            scope=intent_scope,
            context={
                "previous_request": previous_text,
                "response_language": response_language_hint(response_language),
                **context_fields(plan),
                **({"rehydrated_references": rehydrated} if rehydrated else {}),
                "context_groups": items["groups"],
                "global_attachments": items["global_attachments"],
                "response_mode": items_response_mode(response_mode) if items["groups"] else "",
                # JARVIS's own snapshots of the images the person attached. The
                # adapter attaches exactly these files and strips the key from
                # the payload: a local path is a file handle, never model context.
                "attachment_files": image_files(groups, attachments),
                "local_time": catalog.local_time,
                "catalog": catalog.to_dict(),
                **memory,
            },
            id=request_id,
            project_instruction=instruction,
        )
        self._cancelled_text.discard(request_id)
        # ``on_reply_preview`` sees a General Chat reply while it streams, when
        # the adapter can stream. Display only: the envelope below - built from
        # the validated proposal - stays the one result.
        streams = on_reply_preview is not None and hasattr(model, "stream_listener")
        if streams:
            model.stream_listener = on_reply_preview
        try:
            result = model.complete(intent_task, SessionState(objective=text))
        except Exception as exc:  # noqa: BLE001 - adapter boundary; type only
            self._cancelled_text.discard(request_id)
            return refused(
                IntentStatus.MODEL_FAILED,
                f"The understanding model failed: {type(exc).__name__}.",
            )
        finally:
            if streams:
                model.stream_listener = None
        turn["returned"] = time.monotonic()
        cancelled = request_id in self._cancelled_text
        self._cancelled_text.discard(request_id)
        usage.update({
            **_model_usage(model, request_id),
            "request_chars": len(text),
            "conversation_chars": sum(len(turn["text"]) for turn in intent_task.context["conversation"]),
            "context_plan": {**plan.stats, "budget_tokens": plan.budget_tokens, "used_tokens": plan.used_tokens},
            "items_chars": len(json.dumps(items, ensure_ascii=False)) if carried else 0,
            "items": len(items["groups"]) + sum(len(g["attachments"]) for g in items["groups"]) + len(items["global_attachments"]),
            "context_chars": len(json.dumps(intent_task.context["catalog"], sort_keys=True)),
        })
        if not result.ok:
            status = model_failure_status(result, cancelled)
            detail = result.error.message if result.error is not None else result.summary
            return refused(status, sanitize_diagnostic_text(detail, 240), model_failure_reason(result))
        if cancelled:
            return refused(IntentStatus.MODEL_CANCELLED, "The understanding call was cancelled.")
        answer = result.data.get(MODEL_ANSWER_RESULT_KEY) if isinstance(result.data, dict) else None
        if type(answer) is ModelAnswer and answer.present:
            turn["output_chars"] = len(answer.content)      # when nothing streamed
        # DFREPLY: a chat reply too long to keep whole is kept up to the ceiling as
        # what it is - text to show, cut short. Nothing else of the answer is read,
        # so no proposal is resolved and nothing can be executed from it.
        kept = too_long_reply(answer.content, answer.truncated) if type(answer) is ModelAnswer and answer.present else None
        if kept is not None:
            return respond(mode, IntentStatus.CONVERSATION, request_id, reply=kept, reason=CHAT_REPLY_TOO_LONG,
                           message=_TOO_LONG_MESSAGE)
        if type(answer) is not ModelAnswer or not answer.present or answer.truncated:
            return refused(
                IntentStatus.MALFORMED_MODEL_OUTPUT,
                "The understanding model returned no usable proposal. Nothing was executed.",
            )
        try:
            proposal = parse_proposal(answer.content)
        except ProposalError as exc:
            return refused(
                IntentStatus.MALFORMED_MODEL_OUTPUT,
                f"The proposal was refused: {exc}. Nothing was executed.",
            )
        # Only the user's CURRENT turn may choose among several configured
        # destinations - never the model, and never an earlier turn.
        resolution = resolve_proposal(proposal, catalog, now, text)
        turn["validated"] = time.monotonic()
        # K3 (E1): the model chooses only "" or "desktop"; a path, a read window
        # or any other field is refused before a task can exist.
        if (resolution.status is IntentStatus.STRUCTURED and resolution.capability == PROJECT_FOLDER_CAPABILITY
                and not _project_folder_document(resolution.task_document)):
            return refused(
                IntentStatus.MALFORMED_MODEL_OUTPUT,
                "The project folder proposal was refused: it may only say whether the folder goes on the "
                "Desktop. Nothing was executed.",
            )
        if resolution.status in (IntentStatus.STRUCTURED, IntentStatus.SCHEDULED) and not self.core.execution_allowed():
            # THE barrier: understood, but nothing is created, queued or started.
            return respond(
                mode, IntentStatus.EXECUTION_DISABLED, request_id,
                message=_FROZEN_UNDERSTOOD_MESSAGE,
                capability=resolution.capability,
                destination=resolution.destination,
                objective=resolution.objective,
            )
        if resolution.status is IntentStatus.STRUCTURED:
            try:
                task = decode_task_json(json.dumps(resolution.task_document))
            except TaskInputError as exc:
                return refused(
                    IntentStatus.MALFORMED_MODEL_OUTPUT,
                    f"The proposal was refused by the task boundary: {exc} Nothing was executed.",
                )
            if scoped:
                task.project_id = owner
            if task.capability == PROJECT_FOLDER_CAPABILITY:
                # K3 (E3): JARVIS computes the path ONCE, from the stored
                # project name; it travels only in the connector's context.
                if not self._propose_project_folder(task):
                    return refused(IntentStatus.UNSUPPORTED, _NO_FOLDER_PROPOSAL)
            # From here on the ordinary lifecycle owns everything. No grant
            # is passed: a natural-language request cannot approve itself.
            run = self.core.run(task, session_id)
            return respond(
                mode, IntentStatus.STRUCTURED, request_id,
                capability=resolution.capability,
                destination=resolution.destination,
                objective=resolution.objective,
                run=build_envelope("run", mode, run),
                exit_code=self._exit_code(run),
            )
        if resolution.status is IntentStatus.CONVERSATION:
            # Text to show. Nothing was created and nothing can run from it.
            return respond(mode, IntentStatus.CONVERSATION, request_id, reply=resolution.reply)
        if resolution.status is IntentStatus.RESEARCH:
            # Like conversation, research creates nothing and needs no approval:
            # one read-only public search answers a question. It is not gated by
            # Freeze for the same reason chat is not - nothing authoritative runs.
            return self._research(resolution.objective, text, request_id, response_language, usage, respond)
        if resolution.status is IntentStatus.SCHEDULED:
            created = job_create_result(
                self.memory, self.settings, json.dumps(resolution.job_document)
            )
            if created.exit_code != EXIT_OK:
                error = created.payload.get("error") or {}
                return refused(
                    IntentStatus.MALFORMED_MODEL_OUTPUT,
                    f"The proposed job was refused: {error.get('message')} Nothing was created.",
                )
            return respond(
                mode, IntentStatus.SCHEDULED, request_id,
                capability=resolution.capability,
                destination=resolution.destination,
                objective=resolution.objective,
                job=created.payload,
            )
        return respond(
            mode, resolution.status, request_id,
            message=resolution.message,
            question=resolution.question,
            capability=resolution.capability,
            destination=resolution.destination,
            choices=resolution.choices,
        )

    def _intent_instruction(
        self, owner: str | None, scope: ActionScope, usage: dict[str, Any]
    ) -> str | None:
        """The owner project's instruction text for the understanding call,
        or ``None``. Global gets nothing (no single-project fallback). The
        delivery status is LOCAL telemetry in ``usage``, never payload."""
        if owner is None:
            return None
        try:
            current = self.memory.load_project_instruction(owner)
        except Exception:  # noqa: BLE001 - storage boundary; deliver nothing
            current = None
        text, status = project_instruction_delivery(current, scope.data_classification)
        if status:
            usage["project_instruction"] = status
        return text

    def _chat_memory(
        self, owner: str | None, session: SessionState | None, conversation_id: str | None,
        text: str, scope: ActionScope,
    ) -> dict[str, Any]:
        """V2.1a M3b: the chat turn's typed memory, ``{}`` or
        ``{"retrieved_memory": block}`` for the understanding call's context.

        Core decides everything (``AgentCore.typed_memory_for``: project
        scope, the conversation's global toggle, approved records only,
        provenance labels); the block is the connector's re-validated egress
        view, labelled untrusted data. The app only names the scope: the
        owner and this conversation (a conversation-less CLI turn names its
        session's linked one). Global and any failure get nothing."""
        if owner is None:
            return {}
        try:
            if conversation_id is None and session is not None:
                conversation_id = self.memory.conversation_for_session(session.id)
            probe = Task(objective=text, capability=INTENT_CAPABILITY, scope=scope)
            probe.retrieved_context = self.core.typed_memory_for(owner, conversation_id, probe)
            block, _ = ClaudeCodeConnector._retrieved_memory(probe)
        except Exception:  # noqa: BLE001 - informational context; deliver nothing
            return {}
        return {} if block is None else {CHAT_MEMORY_KEY: block}

    def _conversation_scope(
        self, conversation_id: str, session: SessionState | None
    ) -> tuple[str | None, str | None]:
        """``(owner, None)`` or ``(None, refusal)`` for a conversation request.

        Fails closed: an unknown conversation, an archived owner project, or
        a session bound to a different project or linked to a conversation of
        a different owner. ``""`` is the window's UNSAVED conversation (no
        store): Global by construction."""
        owner = None
        if conversation_id != "":
            try:
                record = self.memory.load_conversation(conversation_id)
            except (TypeError, ValueError):
                record = None
            if record is None:
                return None, f"Unknown conversation: {conversation_id}."
            if record.archived_at is not None:
                return None, "This conversation is archived; it is read-only."
            owner = record.project_id
        if owner is not None:
            project = self.memory.load_project(owner)
            if project is None or not project.active:
                return None, "This conversation's project is archived; it is read-only."
        if session is None:
            return owner, None
        if session.project_id is not None and session.project_id != owner:
            return None, "The session is bound to another project scope."
        linked = self.memory.conversation_for_session(session.id)
        if linked is not None and linked != conversation_id:
            other = self.memory.load_conversation(linked)
            if other is None or other.project_id != owner:
                return None, "The session belongs to a conversation of another project scope."
        return owner, None

    # -- V2.1a M4 (MEMAPPAPI): memory app API ----------------------------------
    # Plain payload dicts for the bridge: ``{"ok": True, ...}`` or
    # ``{"ok": False, "error": ...}``. Approve/reject/undo/Hatırla are USER
    # actions: this is the trusted boundary that builds the explicit-user
    # proof (I2/I3); nothing a model returns ever reaches these methods.

    @staticmethod
    def _record_payload(view: Any) -> dict[str, Any]:
        record = view.record
        return {
            "record_id": record.record_id,
            "kind": record.kind.value,
            "text": record.text,
            "subject_key": record.subject_key,
            "status": view.status.value,
            "origin": record.origin.value,
            "actor": record.actor.value,
            "classification": record.classification.value,
            "project_id": record.scope.project_id,
            "conversation_id": record.scope.conversation_id,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "supersedes": record.supersedes,
            "source_conversation_id": record.source_conversation_id,
        }

    def _active_project_refusal(self, project_id: Any) -> str | None:
        try:
            validate_project_id(project_id)
            project = self.memory.load_project(project_id)
        except (TypeError, ValueError):
            project = None
        if project is None or not project.active:
            return f"Unknown or inactive project: {project_id}."
        return None

    def memory_records_payload(self, project_id: str, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        refusal = self._active_project_refusal(project_id)
        if refusal:
            return {"ok": False, "error": refusal}
        filters = {} if filters is None else filters
        if not isinstance(filters, Mapping):
            return {"ok": False, "error": "filters must be a mapping."}
        try:
            statuses = (
                {MemoryRecordStatus(value) for value in filters["statuses"]}
                if filters.get("statuses") else None
            )
            kinds = {MemoryRecordKind(value) for value in filters["kinds"]} if filters.get("kinds") else None
            limit = filters.get("limit", 100)
            query = filters.get("query") or ""
            if type(query) is not str:
                raise ValueError("query must be text")
            scope = MemoryRecordScope(project_id)
            if query.strip():
                views = [
                    view for view, _rank in self.memory.search_records(
                        query, scope, kinds=kinds, statuses=statuses, limit=limit
                    )
                ]
            else:
                views = self.memory.list_records(scope, statuses=statuses, kinds=kinds, limit=limit)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"Invalid memory filter: {exc}"}
        return {
            "ok": True,
            "project_id": project_id,
            "records": [self._record_payload(view) for view in views],
        }

    def _owned_record_refusal(self, project_id: Any, record_id: Any, status: MemoryRecordStatus) -> str | None:
        """None when ``record_id`` is a project-wide record of ``project_id``
        in ``status``; otherwise a refusal (unknown, other project, wrong
        state - indistinguishable on purpose).

        ponytail: one read-only row lookup on the store's own connection
        because ``record_event``/``undo`` do not check the project; a
        store-side project-checked transition replaces this."""
        refusal = self._active_project_refusal(project_id)
        if refusal:
            return refusal
        if type(record_id) is not str:
            return "Unknown memory record."
        with self.memory._connection() as connection:
            row = connection.execute(
                f"{_RECORD_CURRENT_SQL} WHERE r.record_id = ? AND r.project_id = ? AND r.conversation_id IS NULL",
                (record_id, project_id),
            ).fetchone()
        if row is None or row["current_status"] != status.value:
            return f"No {status.value} memory record {record_id} in this project."
        return None

    def _record_action(self, project_id: str, record_id: str, expected: MemoryRecordStatus, action) -> dict[str, Any]:
        refusal = self._owned_record_refusal(project_id, record_id, expected)
        if refusal:
            return {"ok": False, "error": refusal}
        try:
            action()
            status = self.memory.current_status(record_id)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "record_id": record_id, "status": status.value}

    def approve_memory_record(self, project_id: str, record_id: str) -> dict[str, Any]:
        # The explicit-user proof is built HERE, from the user's click (I3).
        return self._record_action(
            project_id,
            record_id,
            MemoryRecordStatus.PENDING,
            lambda: self.memory.approve_record(
                record_id, MemoryRecordApproval(project_id=project_id, record_id=record_id)
            ),
        )

    def reject_memory_record(self, project_id: str, record_id: str) -> dict[str, Any]:
        return self._record_action(
            project_id,
            record_id,
            MemoryRecordStatus.PENDING,
            lambda: self.memory.record_event(
                record_id, MemoryRecordStatus.REJECTED, MemoryRecordActor.USER, "user-reject"
            ),
        )

    def undo_memory_record(self, project_id: str, record_id: str) -> dict[str, Any]:
        return self._record_action(
            project_id,
            record_id,
            MemoryRecordStatus.APPROVED,
            lambda: self.memory.undo(record_id, MemoryRecordActor.USER),
        )

    def memory_doctor_payload(self, project_id: str | None = None) -> dict[str, Any]:
        return memory_doctor_payload(self.memory, project_id)

    def apply_memory_repair(
        self, finding_id: str, *, project_id: str | None = None, confirm: str | None = None
    ) -> dict[str, Any]:
        return apply_memory_repair(self.memory, finding_id, project_id=project_id, confirm=confirm)

    def remember_message(self, conversation_id: str, message_id: str, kind: str = "fact") -> dict[str, Any]:
        """'Hatırla': the user's click stores one message as an EXPLICIT_USER
        record of the conversation's project (project-wide, so it is listed
        and undoable). FACT/LESSON land approved; DECISION/RULE/PREFERENCE
        land PENDING and still need the approval card (I4). Connector output
        (``result`` turns) is untrusted and never remembered (I11)."""
        try:
            record = self.memory.load_conversation(conversation_id)
        except (TypeError, ValueError):
            record = None
        if record is None:
            return {"ok": False, "error": f"Unknown conversation: {conversation_id}."}
        if record.archived_at is not None:
            return {"ok": False, "error": "This conversation is archived; it is read-only."}
        if record.project_id is None:
            return {"ok": False, "error": "Only a project conversation can remember a message."}
        refusal = self._active_project_refusal(record.project_id)
        if refusal:
            return {"ok": False, "error": refusal}
        try:
            state = self.memory.load_conversation_state(conversation_id)
        except (KeyError, ValueError):
            return {"ok": False, "error": f"Unknown conversation: {conversation_id}."}
        turn = next((turn for turn in state.turns if turn.id == message_id), None)
        if turn is None:
            return {"ok": False, "error": f"Unknown message: {message_id}."}
        if turn.kind == "user":
            text = turn.message or turn.text
        elif turn.kind == "reply":
            text = turn.text
        else:
            return {"ok": False, "error": "Tool results cannot be remembered."}
        try:
            # 60 characters keep the key under its 120 cap even after casefold
            # expansion; a key that still cannot be derived refuses. Derived
            # from the redacted text: the store redacts only ``text``.
            redacted = redact_local_paths(redact_credentials(text)[0])
            subject_key = normalize_memory_subject_key(redacted.strip()[:60])
            view = self.memory.insert_explicit_user_record(
                record.project_id, kind, DataClassification.INTERNAL.value, text, subject_key
            )
        except MemoryRecordRefused as exc:
            return {"ok": False, "refused": exc.category, "error": "This message cannot be stored in memory."}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "record": self._record_payload(view)}

    # -- V2.1b (IMPEXTRACT): history import pipeline ----------------------------
    # import -> dedup -> proposed scope (preview) -> the user confirms a project
    # per group -> extraction -> every record PENDING until the user approves
    # it. Imported text is untrusted DATA: never executed, never instructions.

    def import_history_preview(self, path: Any) -> dict[str, Any]:
        """Parse one export file and plan it. Writes nothing - no record, no
        run row; the plan lives in this process under a fresh ``plan_id``.
        Protected paths (operator data, secrets, the memory database's
        folder), links and malformed or oversized exports are refused."""
        if not isinstance(path, (str, Path)) or not str(path).strip():
            return {"ok": False, "error": "path must be a file path."}
        try:
            batch = memory_import.parse_export(
                path, protected_roots=(Path(self.settings.memory_database).parent,)
            )
            projects = {project.id: project.name for project in self.memory.list_projects() if project.active}
            # ponytail: dedup is within the export; a re-import is deduped per
            # batch by the run ledger. Pass stored item hashes if they get a home.
            plan = plan_import(batch, (), projects)
        except memory_import.ProtectedPathError:
            return {"ok": False, "refused": "protected_path", "error": "This path is protected and is never read."}
        except (memory_import.ImportRefused, OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        plan_id = uuid4().hex
        with _IMPORT_LOCK:
            _IMPORT_PLANS[plan_id] = plan
            while len(_IMPORT_PLANS) > _IMPORT_PLANS_MAX:
                _IMPORT_PLANS.pop(next(iter(_IMPORT_PLANS)))
        return {
            "ok": True,
            "plan_id": plan_id,
            "groups": [
                {
                    "group_id": str(index),
                    "title": group.source_conversation_title,
                    "source_format": group.source_format,
                    "items": len(group.items),
                    "proposed_project_id": group.proposed_project_id,
                    "scope_status": group.scope_status,
                    "estimated_chars": group.estimated_chars,
                    "estimated_tokens": group.estimated_tokens,
                }
                for index, group in enumerate(plan.groups)
            ],
            "duplicates_dropped": plan.duplicates_dropped,
            "estimated_chars": plan.estimated_chars,
            "estimated_tokens": plan.estimated_tokens,
            "warnings": list(batch.warnings),
        }

    def import_history_run(self, plan_id: str, confirmed_scopes: Mapping[str, str]) -> dict[str, Any]:
        """Extract the groups the user confirmed (``group_id -> project_id``).

        A group missing from ``confirmed_scopes`` is skipped; every confirmed
        project must be active before any model call. Each batch is one run
        in ``memory_capture_runs`` (trigger ``import``); a batch already run
        is a ``duplicate`` and is not asked again. Every stored record has
        origin IMPORT, so the store keeps it PENDING whatever its kind.
        ``import_history_cancel`` stops before the next batch."""
        with _IMPORT_LOCK:
            plan = _IMPORT_PLANS.get(plan_id) if isinstance(plan_id, str) else None
        if plan is None:
            return {"ok": False, "error": "Unknown or expired import plan."}
        if not isinstance(confirmed_scopes, Mapping):
            return {"ok": False, "error": "confirmed_scopes must map group id -> project id."}
        groups = {str(index): group for index, group in enumerate(plan.groups)}
        if not set(confirmed_scopes) <= set(groups):
            return {"ok": False, "error": "confirmed_scopes names an unknown group."}
        for project_id in confirmed_scopes.values():
            refusal = self._active_project_refusal(project_id)
            if refusal:
                return {"ok": False, "error": refusal}
        model = self.intent_model
        if model is None:
            return {"ok": False, "error": "No model is available for import extraction."}
        cancel = threading.Event()
        with _IMPORT_LOCK:
            if plan_id in _IMPORT_CANCEL:
                return {"ok": False, "error": "This import is already running."}
            _IMPORT_CANCEL[plan_id] = cancel
        runs: list[dict[str, Any]] = []
        try:
            for group_id, group in groups.items():
                if group_id not in confirmed_scopes:
                    continue
                for turns in _import_batches(group.items):
                    if cancel.is_set():
                        break
                    outcome = self._import_batch(confirmed_scopes[group_id], plan_id, turns, model)
                    runs.append({
                        "group_id": group_id,
                        "project_id": confirmed_scopes[group_id],
                        "run_id": outcome.run_id,
                        "outcome": outcome.outcome,
                        "record_ids": list(outcome.record_ids),
                        **outcome.counts(),
                    })
        finally:
            with _IMPORT_LOCK:
                if _IMPORT_CANCEL.get(plan_id) is cancel:
                    del _IMPORT_CANCEL[plan_id]
        return {
            "ok": True,
            "plan_id": plan_id,
            "cancelled": cancel.is_set(),
            "skipped_groups": [group_id for group_id in groups if group_id not in confirmed_scopes],
            "runs": runs,
            "pending": sum(run["pending"] for run in runs),
            "approved": sum(run["approved"] for run in runs),
        }

    def import_history_cancel(self, plan_id: str) -> dict[str, Any]:
        with _IMPORT_LOCK:
            cancel = _IMPORT_CANCEL.get(plan_id) if isinstance(plan_id, str) else None
        if cancel is None:
            return {"ok": False, "error": "No import is running for this plan."}
        cancel.set()
        return {"ok": True, "plan_id": plan_id}

    def _import_batch(
        self, project_id: str, plan_id: str, turns: tuple[_ImportTurn, ...], model: ModelAdapter
    ) -> MemoryCaptureOutcome:
        """One ledgered extraction run; never raises (mirrors ``capture_memory``)."""
        key = "|".join((project_id, "import", *(turn.id for turn in turns)))
        base = "imp-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        for attempt in range(_IMPORT_RUN_ATTEMPTS):
            run_id = f"{base}-{attempt}" if attempt else base
            try:
                if _claim_capture_run(self.memory, run_id, f"import-{plan_id}", "import"):
                    break
                with _capture_run_connection(self.memory) as connection:
                    row = connection.execute(
                        "SELECT outcome FROM memory_capture_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
            except Exception:  # noqa: BLE001 - no run store means no model call
                return MemoryCaptureOutcome(run_id=run_id, outcome="run_store_error")
            # Done, or unfinished (maybe still running): never asked twice.
            # A finished failure is retried under the next attempt id.
            if row is None or row[0] in (None, "ok"):
                return MemoryCaptureOutcome(run_id=run_id, outcome="duplicate", duplicate=True)
        else:
            return MemoryCaptureOutcome(run_id=base, outcome="retries_exhausted")
        try:
            result = self._run_import_capture(run_id, project_id, turns, model)
        except Exception:  # noqa: BLE001 - one batch never breaks the import
            result = MemoryCaptureOutcome(run_id=run_id, outcome="internal_error")
        try:
            _finish_capture_run(self.memory, result)
        except Exception:  # noqa: BLE001 - the run row stays unfinished
            pass
        return result

    def _run_import_capture(
        self, run_id: str, project_id: str, turns: tuple[_ImportTurn, ...], model: ModelAdapter
    ) -> MemoryCaptureOutcome:
        # ponytail: Core's capture path for imports, reusing Core's own
        # validators; AgentCore.capture_memory is conversation-bound and
        # jarvis/core.py belongs to another lane. Upgrade: AgentCore.capture_import.
        prompt = memory_capture.build_capture_prompt(turns, MemoryRecordOrigin.IMPORT, "")
        task = Task(
            objective=prompt,
            capability=_IMPORT_CAPABILITY,
            scope=ActionScope(action=_IMPORT_CAPABILITY),
            project_id=project_id,
        )
        try:
            response = model.complete(task, SessionState(objective=prompt))
        except Exception:  # noqa: BLE001 - provider boundary
            return MemoryCaptureOutcome(run_id=run_id, outcome="provider_error")
        if type(response) is not ToolResult or not response.ok or type(response.data) is not dict:
            return MemoryCaptureOutcome(run_id=run_id, outcome="provider_error")
        try:
            answer = AgentCore._validated_model_answer(response.data.get(MODEL_ANSWER_RESULT_KEY))
        except TypeError:
            return MemoryCaptureOutcome(run_id=run_id, outcome="malformed_answer")
        if not answer.present or answer.truncated:
            return MemoryCaptureOutcome(run_id=run_id, outcome="malformed_answer")
        parsed = memory_capture.parse_candidates(answer.content, turns)
        if type(parsed) is not memory_capture.CaptureResult:
            return MemoryCaptureOutcome(run_id=run_id, outcome="parse_error")
        known = frozenset(turn.id for turn in turns)
        # Evidence must be the person's side of the export (an "unknown" turn
        # is a marker-less note); an assistant/system/tool line alone is not.
        own = frozenset(turn.id for turn in turns if turn.role in ("user", "unknown"))
        record_ids: list[str] = []
        approved = pending = failed = 0
        refused = parsed.refused
        for value in parsed.candidates:
            candidate = _validated_capture_candidate(value, known, own)
            if candidate is None:
                refused += 1
                continue
            try:
                view = self.memory.insert_record(
                    MemoryRecord(
                        kind=candidate.kind,
                        text=candidate.text,
                        subject_key=candidate.subject_key,
                        scope=MemoryRecordScope(project_id=project_id),
                        origin=MemoryRecordOrigin.IMPORT,
                        actor=MemoryRecordActor.TOOL,
                        tool=_IMPORT_TOOL,
                        confidence=candidate.confidence,
                        classification=DataClassification.INTERNAL,
                    ),
                    # The store re-runs admission (import taint -> PENDING RULE,
                    # proposed kind kept in the guard row) and the tombstone gate.
                    guard=RecordGuard(
                        Provenance.IMPORT,
                        SessionTaint.IMPORT,
                        (),
                        candidate.kind,
                        content_hash(candidate.text),
                    ),
                )
            except ValueError:  # includes MemoryRecordRefused/MemoryTombstoned: the store said no
                refused += 1
                continue
            except Exception:  # noqa: BLE001 - one bad write never stops the run
                failed += 1
                continue
            record_ids.append(view.record.record_id)
            if view.status is MemoryRecordStatus.PENDING:
                pending += 1
            else:
                approved += 1
        return MemoryCaptureOutcome(
            run_id=run_id,
            outcome="ok",
            record_ids=tuple(record_ids),
            approved=approved,
            pending=pending,
            refused=refused,
            dropped=len(parsed.dropped),
            failed=failed,
        )

    def global_memory_toggle_payload(self, conversation_id: str) -> dict[str, Any]:
        try:
            enabled = self.memory.global_memory_enabled(conversation_id)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "conversation_id": conversation_id, "enabled": enabled}

    def set_global_memory_toggle(self, conversation_id: str, enabled: bool) -> dict[str, Any]:
        try:
            self.memory.set_global_memory_toggle(conversation_id, enabled, MemoryRecordActor.USER)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return self.global_memory_toggle_payload(conversation_id)

    def context_usage_payload(self, conversation_id: str) -> dict[str, Any]:
        """Context use of the stored conversation. Without a chat model the
        window is unknown and says so ("bilinmiyor") instead of guessing."""
        try:
            state = self.memory.load_conversation_state(conversation_id)
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": f"Unknown conversation: {conversation_id}."}
        profile = None if self.intent_model is None else context_profile_of(self.intent_model)
        plan = assemble_context(state, profile) if profile is not None else assemble_context(state)
        usage = context_usage(len(state.turns), plan.used_chars, profile, assembled_tokens=plan.used_tokens)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "used_tokens": usage.used_tokens,
            "max_tokens": usage.max_tokens,
            "ratio": usage.ratio,
            "turns_until_autocompact": usage.turns_until_autocompact,
            "label_key": usage.label_key,
        }

    def compact_conversation(self, conversation_id: str, keep_last: int = COMPACT_KEEP_LAST) -> dict[str, Any]:
        """"Oturumu sıkıştır": compact the stored conversation now (PreCompact
        capture first, see ``compact_now``) and return its new context usage.
        Refused while a reply for it is streaming, and when the store refuses."""
        if type(conversation_id) is not str:
            return {"ok": False, "error": "Unknown conversation."}
        # ponytail: one process-wide lock held through the compaction write, so no reply
        # (of any conversation) starts mid-compaction; per-conversation marks if that stalls.
        with _REPLYING_LOCK:
            if _REPLYING.get(conversation_id):
                return {"ok": False, "error": "A reply is still streaming; compact after it finishes."}
            try:
                record = self.memory.load_conversation(conversation_id)
                state = self.memory.load_conversation_state(conversation_id)
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "error": f"Unknown conversation: {conversation_id}."}
            if record is None or record.archived_at is not None:
                return {"ok": False, "error": f"Conversation is archived or unknown: {conversation_id}."}
            conversation = PersistedConversation(self.memory, conversation_id, state)
            try:
                written = compact_now(conversation, keep_last)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        if not written:
            return {"ok": False, "error": conversation.diagnostic or "The conversation could not be compacted."}
        return self.context_usage_payload(conversation_id)

    # -- Gate 6H H3 (K3): the project folder from chat ------------------------

    def _folderless(self, project_id: str | None) -> bool:
        """An active project with no current folder link (a failed read: no;
        an unreadable link row raises in the store, so it is never folderless)."""
        try:
            project = None if project_id is None else self.memory.load_project(project_id)
            return project is not None and project.active and self.memory.load_project_folder(project_id) is None
        except Exception:  # noqa: BLE001 - storage boundary; offer nothing
            return False

    def _propose_project_folder(self, task: Task) -> bool:
        """E3: put JARVIS's own proposal for ``task.project_id`` into the
        connector's context. Any failure is ``False``: nothing about it is
        kept, logged or chained (the cause may name the path)."""
        try:
            connector = self.core.registry.get(PROJECT_FOLDER_CONNECTOR)
            if not isinstance(connector, ProjectFolderCreateConnector):
                return False
            task.context[PROJECT_FOLDER_CONTEXT_KEY] = connector.proposal(
                task.project_id, PROJECT_FOLDER_TARGETS[task.scope.target])
        except Exception:  # noqa: BLE001 - path-free refusal, nothing logged
            return False
        return True

    def project_folder_switch(
        self, session_id: str, task_id: str, request_id: str, fingerprint: str
    ) -> CommandResult:
        """E7 "Masaüstünde oluştur": the pending K3 request moves to the other
        location. Deterministic, no model call. Refused unless the request is
        still pending, of the K3 connector, and its session's conversation is
        owned by the task's project. It is REJECTED through Core first; only
        then (and not while frozen) does a new task for the other location of
        the SAME project and session start, from the rejected task's own
        stored fields - never from what the window shows - with a new card."""
        command = "project-folder-switch"
        if not self.core.execution_allowed():
            return self._failure(command, ErrorCode.EXECUTION_DISABLED,
                                 "JARVIS is frozen; the folder request was not changed.", EXIT_POLICY)
        try:
            request = self.memory.load_pending_approval(task_id, session_id)
            task = self.memory.load_task(task_id)
            linked = self.memory.conversation_for_session(session_id)
            conversation = None if linked is None else self.memory.load_conversation(linked)
        except Exception:  # noqa: BLE001 - storage boundary
            request = task = conversation = None
        if (request is None or (request.id, request.fingerprint) != (request_id, fingerprint)
                or request.scope.connector != PROJECT_FOLDER_CONNECTOR
                or task is None or task.capability != PROJECT_FOLDER_CAPABILITY
                or task.scope.target not in PROJECT_FOLDER_TARGETS
                or conversation is None or task.project_id is None or conversation.project_id != task.project_id):
            return self._failure(command, ErrorCode.INVALID_GRANT,
                                 "This folder request is no longer pending here; nothing was changed.", EXIT_POLICY)
        rejected = self.reject(session_id, task_id, request_id, fingerprint)
        if rejected.payload.get("run_status") != "rejected":
            return rejected
        # After a successful rejection a refusal still reports the rejection's
        # own envelope (the card is gone), with why no new request followed.
        if not self.core.execution_allowed():
            return CommandResult({**rejected.payload, "notice": "JARVIS is frozen; no new folder request was made."},
                                 rejected.exit_code)
        other = "" if task.scope.target == "desktop" else "desktop"
        fresh = Task(
            objective=task.objective,
            capability=PROJECT_FOLDER_CAPABILITY,
            scope=ActionScope(action=task.scope.action, target=other, destination=task.scope.destination,
                              data_classification=task.scope.data_classification),
        )
        fresh.project_id = task.project_id
        if not self._propose_project_folder(fresh):
            return CommandResult({**rejected.payload, "notice": _NO_FOLDER_PROPOSAL}, rejected.exit_code)
        return self._present(command, self.core.run(fresh, session_id))

    def _research(
        self, query: str, text: str, request_id: str, response_language: str,
        usage: dict[str, Any], respond: Callable[..., CommandResult],
    ) -> CommandResult:
        """One read-only public Web Research call for an understood question."""
        mode = self.settings.permission_mode
        model = self.research_model
        if model is None:
            return respond(mode, IntentStatus.RESEARCH_UNAVAILABLE, request_id, objective=query,
                           message="Web Research has no verified route with the selected provider. Nothing was searched.",
                           reason=RESEARCH_PROVIDER_UNSUPPORTED)
        availability = model.availability()
        if not availability.available:
            return respond(mode, IntentStatus.RESEARCH_UNAVAILABLE, request_id, objective=query,
                           message=availability.reason or "Web Research is unavailable.")
        task = Task(
            objective=query,
            capability="web_research",
            scope=ActionScope(action="research", destination=DEFAULT_TASK_DESTINATION),
            context={"request": text, "local_time": datetime.now().astimezone().isoformat(timespec="minutes"),
                     "response_language": response_language_hint(response_language)},
            id=request_id,
        )
        try:
            result = model.complete(task, SessionState(objective=query))
        except Exception as exc:  # noqa: BLE001 - adapter boundary; type only
            return respond(mode, IntentStatus.RESEARCH_FAILED, request_id, objective=query,
                           message=f"Web Research failed: {type(exc).__name__}.")
        cancelled = request_id in self._cancelled_text
        self._cancelled_text.discard(request_id)
        usage["research"] = _model_usage(model, request_id)
        if cancelled:
            return respond(mode, IntentStatus.MODEL_CANCELLED, request_id, objective=query,
                           message="Web Research was cancelled.")
        if not result.ok:
            detail = result.error.message if result.error is not None else result.summary
            details = result.error.details if result.error is not None and isinstance(result.error.details, dict) else {}
            if "redacted_items" in details:
                # A failed turn carries the model's own text as its message: never shown.
                detail = "The web search did not finish with an answer."
            return respond(mode, IntentStatus.RESEARCH_FAILED, request_id, objective=query,
                           message=sanitize_diagnostic_text(detail, 240), reason=model_failure_reason(result))
        data = result.data if isinstance(result.data, dict) else {}
        answer = data.get(MODEL_ANSWER_RESULT_KEY)
        # The CLI's schema-validated object first; the answer text only as its carrier.
        structured = data.get("structured") if isinstance(data.get("structured"), dict) else None
        if structured is None and (type(answer) is not ModelAnswer or not answer.present):
            return respond(mode, IntentStatus.RESEARCH_FAILED, request_id, objective=query,
                           message="Web Research returned no answer.")
        links = [link for link in data.get("search_links") or [] if isinstance(link, dict) and type(link.get("url")) is str]
        reply, sources, cited = parse_research(structured if structured is not None else answer.content, links)
        if not reply:
            # Never show what could not be read: no raw output reaches the conversation.
            return respond(mode, IntentStatus.RESEARCH_FAILED, request_id, objective=query,
                           message="Web Research returned a result JARVIS could not read, so nothing from it is shown.")
        return respond(mode, IntentStatus.RESEARCH, request_id, objective=query, reply=reply,
                       sources=sources, sources_cited=cited)

    def _answer_groups_separately(
        self,
        text: str,
        request_id: str,
        mode: PermissionMode,
        previous_text: str,
        response_language: str,
        conversation: Any,
        groups: list[Mapping[str, Any]],
        attachments: list[Mapping[str, Any]],
        items: dict[str, Any],
        catalog: Any,
        on_reply_preview: Callable[[str], None] | None,
        usage: dict[str, Any],
        respond: Callable[..., CommandResult],
        refused: Callable[..., CommandResult],
        instruction: str | None = None,
        memory: Mapping[str, Any] | None = None,
    ) -> CommandResult:
        """"Reply to each group separately": ONE understanding call PER GROUP.

        A real context boundary, not an instruction: group B's call is built
        without group A's reference, note or files, so no later call can see an
        earlier group. Every call gets the same global message, the same global
        attachments, the same conversation history and the same adapter - one
        provider / model / effort snapshot for the whole turn.

        Calls run one after another, so a preview never mixes two groups and a
        cancellation stops before the next call starts (an already dispatched
        call is cancelled the ordinary way). Each group's result is judged on
        its own: a reply is shown under its label; a proposal to DO something
        is never carried out from a grouped answer; a failure is said, never
        filled in. The turn is one conversation reply with one section per
        group, in order.
        """
        model = self.intent_model
        memory = memory or {}
        owned = {str(group.get("id")): group for group in groups if isinstance(group, Mapping)}
        largest = max((json_text(context_window([g], attachments)) for g in owned.values()), key=len, default="")
        plan = assemble_context(conversation, context_profile_of(model), text,
                                extra_chars=argv_chars(largest) + argv_chars(json_text(catalog.to_dict()))
                                + _instruction_chars(instruction) + _memory_chars(memory))
        history = context_fields(plan)
        sections: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        streams = on_reply_preview is not None and hasattr(model, "stream_listener")
        shown = ""                                  # the answered sections, as the preview shows them
        self._cancelled_text.discard(request_id)
        try:
            for entry in items["groups"]:
                label = entry["label"]
                if request_id in self._cancelled_text:
                    sections.append({"label": label, "status": "cancelled"})
                    continue
                source = owned.get(entry["id"])
                own = context_window([source], attachments)
                if not own["groups"]:
                    sections.append({"label": label, "status": "failed",
                                     "message": "The group could not be read. Nothing was sent for it."})
                    continue
                own["groups"][0]["label"] = label
                task = Task(
                    objective=text,
                    capability=INTENT_CAPABILITY,
                    scope=ActionScope(action=INTENT_ACTION, destination=DEFAULT_TASK_DESTINATION),
                    context={
                        "previous_request": previous_text,
                        "response_language": response_language_hint(response_language),
                        **history,
                        "context_groups": own["groups"],
                        "global_attachments": own["global_attachments"],
                        "response_mode": "separate",
                        "attachment_files": image_files([source], attachments),
                        "local_time": catalog.local_time,
                        "catalog": catalog.to_dict(),
                        **memory,
                    },
                    # The turn's request id for every call: they run one at a time,
                    # and cancel_text reaches whichever call is running.
                    id=request_id,
                    project_instruction=instruction,
                )
                if streams:
                    head = f"{shown}{label}\n"
                    model.stream_listener = lambda partial, head=head: on_reply_preview(head + partial)
                try:
                    result = model.complete(task, SessionState(objective=text))
                except Exception as exc:  # noqa: BLE001 - adapter boundary; type only
                    result = exc
                calls.append({"label": label, **_model_usage(model, request_id)})
                section = self._group_section(label, result, request_id in self._cancelled_text)
                sections.append(section)
                if section["status"] == "answered":
                    shown += f"{label}\n{section['text']}\n\n"
        finally:
            if streams:
                model.stream_listener = None
        cancelled = request_id in self._cancelled_text
        self._cancelled_text.discard(request_id)
        # N groups, N calls: the totals are the SUM of what each call reported,
        # and every call is kept on its own - nothing is presented as one call.
        totals: dict[str, Any] = {}
        for call in calls:
            for key, value in call["reported"].items():
                if type(value) is int:
                    totals[key] = totals.get(key, 0) + value
        answering = [call["reported"]["model"] for call in calls if call["reported"].get("model")]
        if answering:
            totals["model"] = answering[-1]
        usage.update({
            "provider": getattr(model, "provider", None) or None,
            "reported": totals,
            "call_count": len(calls),
            "calls": calls,
            "request_chars": len(text),
            "conversation_chars": sum(len(turn["text"]) for turn in history["conversation"]),
            "items_chars": len(json.dumps(items, ensure_ascii=False)),
            "items": len(items["groups"]) + sum(len(g["attachments"]) for g in items["groups"]) + len(items["global_attachments"]),
        })
        for call in reversed(calls):                 # the newest plan state any call reported
            for key in ("rate_limits", "quota", "requested_model"):
                if call.get(key) and key not in usage:
                    usage[key] = call[key]
        if not any(section["status"] == "answered" for section in sections):
            if cancelled:
                return refused(IntentStatus.MODEL_CANCELLED, "The understanding call was cancelled.")
            failed = next((s for s in sections if s["status"] == "failed"), None)
            if failed is not None:
                return refused(IntentStatus(failed.get("intent_status") or IntentStatus.MODEL_FAILED.value),
                               "No group could be answered: " + str(failed.get("message") or ""))
        # A plain rendition for consumers that read ``reply``; the interface
        # composes the sections itself in the person's language.
        plain = []
        for section in sections:
            body = section.get("text") or {
                "failed": "[not answered: %s]" % (section.get("message") or "the call failed"),
                "cancelled": "[not answered: stopped before this group]",
                "not_answered": "[not answered: this group asked for something to be done; nothing was started]",
            }.get(section["status"], "")
            if section.get("cut"):
                body += "\n\n[reply cut short: too_long]"
            plain.append(f"{section['label']}\n{body}")
        return respond(mode, IntentStatus.CONVERSATION, request_id, reply="\n\n".join(plain), sections=sections)

    @staticmethod
    def _group_section(label: str, result: Any, cancelled: bool) -> dict[str, Any]:
        """What ONE group's call produced, judged on its own and never filled in."""
        if isinstance(result, Exception):
            return {"label": label, "status": "failed", "intent_status": IntentStatus.MODEL_FAILED.value,
                    "message": f"The understanding model failed: {type(result).__name__}."}
        if not result.ok:
            status = model_failure_status(result, cancelled)
            if status is IntentStatus.MODEL_CANCELLED:
                return {"label": label, "status": "cancelled"}
            detail = result.error.message if result.error is not None else result.summary
            return {"label": label, "status": "failed", "intent_status": status.value,
                    "message": sanitize_diagnostic_text(detail, 240)}
        if cancelled:
            return {"label": label, "status": "cancelled"}
        answer = result.data.get(MODEL_ANSWER_RESULT_KEY) if isinstance(result.data, dict) else None
        kept = too_long_reply(answer.content, answer.truncated) if type(answer) is ModelAnswer and answer.present else None
        if kept is not None:            # DFREPLY: shown up to the ceiling, marked cut short
            return {"label": label, "status": "answered", "kind": "chat", "text": kept, "cut": "too_long"}
        if type(answer) is not ModelAnswer or not answer.present or answer.truncated:
            return {"label": label, "status": "failed", "intent_status": IntentStatus.MALFORMED_MODEL_OUTPUT.value,
                    "message": "The model returned no usable answer."}
        try:
            proposal = parse_proposal(answer.content)
        except ProposalError as exc:
            return {"label": label, "status": "failed", "intent_status": IntentStatus.MALFORMED_MODEL_OUTPUT.value,
                    "message": f"The answer was refused: {exc}."}
        spoken = {"chat": proposal.reply, "clarify": proposal.question, "unsupported": proposal.reason}.get(proposal.kind)
        if spoken:
            return {"label": label, "status": "answered", "kind": proposal.kind, "text": spoken}
        # A task, a job or a web search proposed from inside one group's answer
        # is not started: a grouped answer only replies.
        return {"label": label, "status": "not_answered", "kind": proposal.kind}

    def cancel_text(self, request_id: str) -> bool:
        """Ask the running understanding or research call for ``request_id`` to stop.

        Advisory and best-effort, exactly like connector cancellation: the
        return value says a signal was delivered, never that nothing left
        the machine. A cancelled call reports ``model_cancelled`` and
        creates nothing.
        """
        if type(request_id) is not str:
            return False
        models = [m for m in (self.intent_model, self.research_model) if m is not None]
        if not models:
            return False
        self._cancelled_text.add(request_id)
        delivered = False
        for model in models:
            try:
                delivered = bool(model.cancel(request_id)) or delivered
            except Exception:  # noqa: BLE001 - advisory signal; never raises
                pass
        return delivered

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

    # -- V2.2 S3 (SKAPPAPI): skill app API -------------------------------------
    # Payload dicts like the memory API: ``{"ok": True, ...}`` or
    # ``{"ok": False, "error": ...}``. Every method is a USER action: the
    # writes record actor "user" and no method takes an actor, so a model, a
    # skill or Core can never approve a draft through here (S5). A skill is a
    # procedure, never an authority (S1): nothing here grants, implies or
    # remembers an approval. Flag off (S4): list, get, disable, delete and
    # decline still work so the user can always clean up; the rest refuse.

    @cached_property
    def skill_store(self) -> SkillStore | None:
        """Over the app's own database; None (every call refused) otherwise."""
        if isinstance(self.memory, SQLiteMemoryStore):
            return SkillStore(self.memory.database_path)
        return None

    def _skill_call(
        self, action: Callable[[SkillStore], dict[str, Any]], project_id: str | None, *, cleanup: bool = False
    ) -> dict[str, Any]:
        if not cleanup and self.settings.skills_enabled is not True:
            return {"ok": False, "error": "skills disabled", "skills_disabled": True}
        try:
            store = self.skill_store
            if store is None:
                return {"ok": False, "error": "No skill store in this build."}
            if project_id is not None:
                validate_project_id(project_id)
            return {"ok": True, **action(store)}
        except (TypeError, ValueError) as exc:  # SkillRecordError is a ValueError
            return {"ok": False, "error": _redact_text(str(exc))}
        except sqlite3.Error:
            return {"ok": False, "error": "The skill store could not be read or written."}

    @staticmethod
    def _skill_history(store: SkillStore, skill_id: Any, project_id: str | None) -> SkillHistory:
        history = store.history(skill_id, project_id) if type(skill_id) is str else None
        if history is None:  # unknown, deleted or another project's (S8)
            raise SkillStoreError("no such skill")
        return history

    @staticmethod
    def _skill_payload(skill: Skill, state: SkillStatus, counters: SkillHealth) -> dict[str, Any]:
        return {**skill.to_dict(), "status": state.value, "health": counters.to_dict()}

    @staticmethod
    def _edit_draft(history: SkillHistory) -> SkillDraft:
        """The active version as the draft the edit card sends back. Its id
        names the latest version, so an edit made before another one lands is
        refused (lineage mismatch) instead of silently overwriting it."""
        base = active_skill(history)
        seed = f"edit:{base.skill_id}:{base.version}:{history.latest_version}"
        return SkillDraft(
            draft_id="draft-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32],
            name=base.name,
            summary=base.summary,
            scope=base.scope,
            inputs=base.inputs,
            steps=base.steps,
            checks=base.checks,
            provenance=SkillProvenance(
                origin=SkillOrigin.USER_EDIT,
                source_run_id=None,
                parent_skill_id=base.skill_id,
                parent_version=base.version,
                created_at=base.provenance.created_at,
            ),
            reason="user edit",
            target_skill_id=base.skill_id,
        )

    def skills_list(self, project_id: str | None = None) -> dict[str, Any]:
        def listed(store: SkillStore) -> dict[str, Any]:
            return {"skills": [
                self._skill_payload(item.skill, item.status, item.health)
                for item in store.list_skills(project_id)
            ]}
        return self._skill_call(listed, project_id, cleanup=True)

    def skill_get(self, skill_id: str, version: int | None = None, project_id: str | None = None) -> dict[str, Any]:
        def get(store: SkillStore) -> dict[str, Any]:
            if version is not None and type(version) is not int:
                raise SkillStoreError("version must be an int")
            history = self._skill_history(store, skill_id, project_id)
            skill = active_skill(history) if version is None else history.get(version)
            payload = self._skill_payload(skill, status(history), health(history))
            payload["active_version"] = active_version(history)
            payload["versions"] = [item.version for item in history.versions]
            payload["edit_draft"] = self._edit_draft(history).to_dict()
            return {"skill": payload}
        return self._skill_call(get, project_id, cleanup=True)

    def skill_edit(self, skill_id: str, payload: Any, project_id: str | None = None) -> dict[str, Any]:
        """The user's edit of the active version, saved as the next version.
        Saving it IS the user's approval; skill_filter re-runs on the save (S7)."""
        def edit(store: SkillStore) -> dict[str, Any]:
            history = self._skill_history(store, skill_id, project_id)
            edited = parse_user_edited_draft(payload, original=self._edit_draft(history))
            base = active_skill(history)
            changes = {
                name: getattr(edited, name)
                for name in CHANGEABLE_FIELDS
                if getattr(edited, name) != getattr(base, name)
            }
            skill = next_version(base, changes, SkillOrigin.USER_EDIT, utc_now(), history=history)
            store.add_version(skill, actor="user")
            return {"skill_id": skill.skill_id, "version": skill.version}
        return self._skill_call(edit, project_id)

    def _skill_event(
        self,
        skill_id: str,
        project_id: str | None,
        make: Callable[[SkillHistory], SkillEvent],
        *,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        def append(store: SkillStore) -> dict[str, Any]:
            event = make(self._skill_history(store, skill_id, project_id))
            store.append_event(event, actor="user")
            return {"skill_id": event.skill_id, "event": event.kind.value, "version": event.version}
        return self._skill_call(append, project_id, cleanup=cleanup)

    def skill_set_enabled(self, skill_id: str, on: bool, project_id: str | None = None) -> dict[str, Any]:
        if type(on) is not bool:
            return {"ok": False, "error": "on must be a bool."}
        kind = SkillEventKind.ENABLED if on else SkillEventKind.DISABLED
        return self._skill_event(
            skill_id, project_id,
            lambda history: SkillEvent(skill_id=skill_id, kind=kind, version=None, at=utc_now()),
            cleanup=not on,
        )

    def skill_delete(self, skill_id: str, project_id: str | None = None) -> dict[str, Any]:
        """A final tombstone event (D4); nothing is removed or rewritten (S9)."""
        return self._skill_event(
            skill_id, project_id,
            lambda history: SkillEvent(
                skill_id=skill_id, kind=SkillEventKind.DELETED, version=None, at=utc_now()
            ),
            cleanup=True,
        )

    def skill_rollback(self, skill_id: str, version: int, project_id: str | None = None) -> dict[str, Any]:
        """Only the active pointer moves; every version stays (S6)."""
        return self._skill_event(
            skill_id, project_id, lambda history: rollback_event(history, version, utc_now())
        )

    def drafts_pending(self, project_id: str | None = None) -> dict[str, Any]:
        return self._skill_call(
            lambda store: {"drafts": [item.to_dict() for item in store.pending_drafts(project_id)]},
            project_id,
        )

    def draft_approve(
        self, draft_id: str, scope: str | None = None, edits: Any = None, project_id: str | None = None
    ) -> dict[str, Any]:
        """The user's explicit approval of one pending draft (S5). ``scope`` is
        the card's choice: ``None``/"same" keeps the draft's, "global" makes a
        project draft global (D15); ``edits`` is the edited draft payload."""
        def approve(store: SkillStore) -> dict[str, Any]:
            if type(draft_id) is not str:
                raise SkillStoreError("no such draft")
            if scope not in (None, "same", "global"):
                raise SkillStoreError("scope is 'same' or 'global'")
            chosen = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None) if scope == "global" else None
            skill = store.approve_draft(draft_id, chosen, edits, "user", project_id=project_id)
            return {"skill_id": skill.skill_id, "version": skill.version}
        return self._skill_call(approve, project_id)

    def draft_reject(self, draft_id: str, project_id: str | None = None) -> dict[str, Any]:
        def reject(store: SkillStore) -> dict[str, Any]:
            if type(draft_id) is not str:
                raise SkillStoreError("no such draft")
            store.reject_draft(draft_id, "user", project_id=project_id)
            return {"draft_id": draft_id}
        return self._skill_call(reject, project_id)

    def _suppression(self, task_kind: str | None, scope: str, suppressed: bool) -> dict[str, Any]:
        def write(store: SkillStore) -> dict[str, Any]:
            if scope not in ("task", "global"):
                raise SkillStoreError("scope is 'task' or 'global'")
            key = GLOBAL_SUPPRESSION if scope == "global" else task_kind
            store.set_suppression(key, suppressed, actor="user")
            return {"key": key, "suppressed": suppressed}
        return self._skill_call(write, None)

    def suppress(self, task_kind: str | None, scope: str = "task") -> dict[str, Any]:
        """Stop offering drafts for one task kind (``tk-...``) or at all."""
        return self._suppression(task_kind, scope, True)

    def unsuppress(self, task_kind: str | None, scope: str = "task") -> dict[str, Any]:
        return self._suppression(task_kind, scope, False)

    def skills_set_enabled(self, on: bool, store: ConfigStore | None = None) -> dict[str, Any]:
        """The skills switch, through the same ConfigStore.mutate path as
        every other configuration write. Takes effect for the next composed
        app (Core reads its own settings), like a permission-mode change."""
        if type(on) is not bool:
            return {"ok": False, "error": "on must be a bool."}
        try:
            updated = (store or ConfigStore()).mutate(lambda current: replace(current, skills_enabled=on))
        except ConfigError as exc:
            return {"ok": False, "error": exc.message}
        if updated.skills_enabled is not True:  # off is fail-closed at once here
            self.settings = replace(self.settings, skills_enabled=False)
        return {"ok": True, "skills_enabled": updated.skills_enabled}

    def skill_decline(self, session_id: str, task_id: str) -> CommandResult:
        """D6: run the anchor task without the announced skill. Works with
        the flag off too (stopping a skill is clean-up); Core records no use
        event (D16) and may refuse when the step can no longer be stopped."""
        if not hasattr(self.core, "decline_skill"):
            return self._failure("skill-decline", ErrorCode.INVALID_TASK, "skills are not wired into Core", EXIT_POLICY)
        owned = self._ownership_failure("skill-decline", session_id, task_id)
        if owned is not None:
            return owned
        return self._present("skill-decline", self.core.decline_skill(session_id, task_id))

    def skill_use(self, session_id: str, task_id: str) -> CommandResult:
        """The user's explicit 'use it' for a stale (revalidation) skill (S12)."""
        if self.settings.skills_enabled is not True:
            return self._failure("skill-use", ErrorCode.INVALID_TASK, "skills disabled", EXIT_POLICY)
        if not hasattr(self.core, "use_skill"):
            return self._failure("skill-use", ErrorCode.INVALID_TASK, "skills are not wired into Core", EXIT_POLICY)
        owned = self._ownership_failure("skill-use", session_id, task_id)
        if owned is not None:
            return owned
        return self._present("skill-use", self.core.use_skill(session_id, task_id))

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

    def declare_chain(
        self, session_id: str, raw_declaration: str | bytes
    ) -> CommandResult:
        """Declare one inert chain. Creates no task and executes nothing."""
        return chain_declare_result(
            self.memory, self.settings, session_id, raw_declaration
        )

    def run_chain(self, session_id: str) -> CommandResult:
        """Drive the session's chain. Every step runs the ordinary way.

        The driver reaches ``AgentCore.run_chain``, which reaches ``resume``,
        which is the same entry point a standalone task uses - so approval,
        billing and the attempt journal are the existing authorities and this
        command adds none of its own.
        """
        run, verdict = self.core.run_chain(session_id)
        return CommandResult(
            build_envelope(
                "chain-run",
                self.settings.permission_mode,
                run,
                chain_verdict=verdict,
            ),
            self._exit_code(run),
        )

    def run_scheduler(self, *, once: bool = False) -> CommandResult:
        """Drive the foreground scheduler: one bounded sweep, or until Ctrl+C.

        It adds no authority of its own. Every occurrence it touches goes
        through the same claim, dispatch, condition-watch, recovery and
        cancellation boundaries an operator command reaches, so this is a
        DRIVER for the existing machinery and never a second copy of it.

        The response states the bounded facts an operator must not have to
        infer: how many sweeps ran, whether the kill switch stopped new claims,
        and what each generation's own boundary answered.
        """
        runner = SchedulerRunner(self.core)
        reports = (runner.run_once(),) if once else runner.run_until_stopped()
        counts: dict[str, int] = {}
        for report in reports:
            for action in report.actions:
                counts[action.action] = counts.get(action.action, 0) + 1
        last = reports[-1] if reports else None
        return CommandResult(
            _redact(
                {
                    "command": "scheduler-run",
                    "mode": "once" if once else "until_stopped",
                    "sweeps": len(reports),
                    "jobs_considered": 0 if last is None else last.jobs_considered,
                    # Stated rather than implied: a sweep that did nothing
                    # because execution is off must not read as "nothing due".
                    "claims_suspended": (
                        not self.core.execution_allowed()
                        if last is None
                        else last.claims_suspended
                    ),
                    "counts": counts,
                    "actions": [
                        {
                            "job_id": action.job_id,
                            "run_id": action.run_id,
                            "action": action.action,
                            "reason": action.reason,
                            "verdict": action.verdict,
                        }
                        for action in (() if last is None else last.actions)
                    ],
                    # The runner retries nothing. A generation left blocked or
                    # in doubt stays that way until a person resolves it.
                    "retry_authorized": False,
                    "next_step": (
                        "Generations reported 'parked' are waiting for a human "
                        "approval and 'skipped: unknown_outcome' ones for an "
                        "operator resolution ('jarvis task orphan' then 'jarvis "
                        "job resolve'). The scheduler never resumes, retries or "
                        "settles either on its own."
                    ),
                }
            ),
            EXIT_OK,
        )

    def show_chain(self, session_id: str) -> CommandResult:
        return chain_show_result(self.memory, self.settings, session_id)

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
        """The ONE reuse predicate the loop itself applies (never a copy)."""
        return self.core._remembered_permission_allowed(request)

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
    destination_policy: AbstractSet[DestinationGrant] | None = None,
    github_identity: GitHubIdentityBinding | None = None,
    gmail_identity: GmailIdentityBinding | None = None,
    intent_model: ModelAdapter | None = None,
    research_model: ModelAdapter | None = None,
) -> JarvisApp:
    """Assemble an application from injected parts. Tests use this.

    ``intent_model`` is the natural-language understanding adapter. It is
    held by the application, never registered as a connector, and ``None``
    means the free-text entry reports itself unavailable.

    ``destination_policy`` is the closed (connector, capability, canonical
    destination) authorization table this build runs under. It belongs to
    whoever composes the build; ``None`` means the shipped Core policy, which
    is what production uses.

    ``github_identity`` is the expected GitHub identity Core will hold - the
    other half of the same trusted composition boundary, and the ONLY way one
    can be established. ``None`` means the shipped default, which owns no
    identity at all and therefore refuses every repository destination rather
    than believing a connector's account of itself.
    """
    settings = settings or Settings()
    memory = memory if memory is not None else SQLiteMemoryStore(settings.memory_database)
    registry = registry if registry is not None else ToolRegistry()
    for connector in connectors:
        registry.register(connector)
    return JarvisApp(
        AgentCore(
            registry,
            memory,
            settings,
            verifier,
            destination_policy=destination_policy,
            github_identity=github_identity,
            gmail_identity=gmail_identity,
        ),
        memory,
        settings,
        intent_model=intent_model,
        research_model=research_model,
    )


def build_default_app(settings: Settings | None = None) -> JarvisApp:
    """Production composition. Constructing a connector starts no process."""
    settings = settings or Settings()
    startup_directory = Path.cwd().resolve()
    connector = ClaudeCodeConnector(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
        max_budget_usd=settings.claude_call_budget_usd,
        model=settings.claude_model,
        effort=settings.claude_effort,
    )
    # The natural-language understanding call: the same hardened CLI seam
    # and the same operator model/effort/budget, held by the app and NOT
    # registered - it interprets a request, it never executes one.
    intent_model = ClaudeIntentAdapter(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
        max_budget_usd=settings.claude_call_budget_usd,
        model=settings.claude_model,
        effort=settings.claude_effort,
    )
    # Web Research: the same seam, the same operator model/effort, one tool
    # (WebSearch). Held by the app and NOT registered, like the understanding call.
    research_model = ClaudeResearchAdapter(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
        max_budget_usd=settings.claude_call_budget_usd,
        model=settings.claude_model,
        effort=settings.claude_effort,
    )
    # M3: the same startup root, so a local read and a Claude plan are bound
    # to exactly one boundary. Constructing it touches no filesystem.
    local_reader = LocalFileReadConnector(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
    )
    # M5: the same startup root again, so read, write and the Claude plan are
    # all bound to exactly one boundary. Constructing it touches no
    # filesystem and grants the model nothing.
    local_writer = LocalFileWriteConnector(
        allowed_roots=(startup_directory,),
        default_working_directory=startup_directory,
    )
    # GitHub v1. The authority is CONFIGURATION, resolved here and nowhere
    # else: an operator's stated login becomes the Core-owned identity
    # binding, and each stated repository becomes exact
    # ``DestinationGrant`` rows - one per read capability, per repository.
    # Nothing is inferred and nothing is defaulted, so an installation that
    # configured no GitHub account composes with no GitHub connector, no
    # GitHub grant and no expected identity, and therefore cannot reach a
    # repository even if a task names one.
    # Gate 6H H3 (K3): ONE store, shared by the app and the project folder
    # connector, which gets the same protected roots project_folder_result
    # uses. It adds no root and no working directory to any connector.
    memory = SQLiteMemoryStore(settings.memory_database)
    project_folder = ProjectFolderCreateConnector(memory, _folder_protected(memory, settings))
    connectors: tuple[BaseConnector, ...] = (connector, local_reader, local_writer, project_folder)
    github_identity, github_policy = github_authority(settings)
    if github_identity is not None:
        connectors += (GitHubCliConnector(github_identity),)
    # Gmail minimum: the same rule, from the same kind of statement. An
    # installation that configured no mailbox composes with no Gmail
    # connector, no Gmail grant and no expected identity.
    gmail_identity, gmail_policy = gmail_authority(settings)
    if gmail_identity is not None:
        connectors += (GmailCliConnector(gmail_identity),)
    # Browser basic: the same rule from the same kind of statement, with one
    # difference - there is no identity half, because JARVIS holds no browser
    # credential. An installation that configured no origin composes with no
    # browser connector and no browser grant, so no origin is reachable.
    browser_policy = browser_authority(settings)
    if browser_policy:
        connectors += (BrowserCdpConnector(str(settings.browser_profile_dir)),)
    # Windows Desktop basic: the same rule. No configured application means
    # no desktop connector and no desktop grant.
    desktop_policy = desktop_authority(settings)
    if desktop_policy:
        connectors += (DesktopUiaConnector(),)
    configured = github_policy | gmail_policy | browser_policy | desktop_policy
    return create_app(
        settings=settings,
        memory=memory,
        connectors=connectors,
        destination_policy=(
            CONNECTOR_DESTINATION_POLICY | configured if configured else None
        ),
        github_identity=github_identity,
        gmail_identity=gmail_identity,
        intent_model=intent_model,
        research_model=research_model,
    )


def github_authority(
    settings: Settings,
) -> tuple[GitHubIdentityBinding | None, frozenset[DestinationGrant]]:
    """The identity and the exact grants this installation configured.

    Two values because Core needs two, and they are deliberately produced
    together from ONE source: an installation cannot end up authorized for a
    repository it has no identity for, or hold an identity that authorizes
    nothing. Both halves stay Core's - this function only reads configuration
    the operator wrote and hands the result to the composition root.

    An unconfigured installation returns ``(None, frozenset())``, which is the
    shipped default: no connector, no grant, no expected identity.
    """
    principal = settings.github_principal
    if principal is None or not settings.github_repositories:
        return (None, frozenset())
    grants = {
        DestinationGrant(GITHUB_CONNECTOR, capability, destination)
        for destination in settings.github_repositories
        for capability in GITHUB_READ_CAPABILITIES
    }
    return (GitHubIdentityBinding(principal=principal), frozenset(grants))


def gmail_authority(
    settings: Settings,
) -> tuple[GmailIdentityBinding | None, frozenset[DestinationGrant]]:
    """The Gmail identity and the exact grants this installation configured.

    One configured mailbox address becomes the Core-owned identity binding
    and one exact grant per ENABLED Gmail capability for ``gmail/<account>``
    - reads, the draft and the send alike, because every one of them is
    still behind mandatory approval and the send additionally binds its
    exact bytes. The deferred capability gets no row. An unconfigured
    installation returns ``(None, frozenset())``.
    """
    principal = settings.gmail_principal
    if principal is None:
        return (None, frozenset())
    destination = f"gmail/{principal}"
    grants = {
        DestinationGrant(GMAIL_CONNECTOR, capability, destination)
        for capability in GMAIL_ENABLED_CAPABILITIES
    }
    return (
        GmailIdentityBinding(
            principal=principal, config_dir=str(settings.gmail_config_dir)
        ),
        frozenset(grants),
    )


def browser_authority(settings: Settings) -> frozenset[DestinationGrant]:
    """The exact browser grants this installation configured.

    One row per enabled capability per configured origin - reads and
    interactions alike, because every one of them is still behind mandatory
    approval and an interaction additionally binds its exact target and page
    state. An unconfigured installation returns an empty set, which is the
    shipped default: no connector and no grant.

    No identity half, deliberately. GitHub and Gmail each act AS somebody and
    Core must own that binding; the browser acts as nobody - it drives a
    dedicated profile JARVIS created, with no session of the operator's in
    it - so there is no identity to bind and nothing here reads a credential.
    """
    return frozenset(
        DestinationGrant(BROWSER_CONNECTOR, capability, destination)
        for destination in settings.browser_origins
        for capability in BROWSER_ENABLED_CAPABILITIES
    )


def desktop_authority(settings: Settings) -> frozenset[DestinationGrant]:
    """The exact desktop grants this installation configured: one row per
    enabled capability per configured application, every one still behind
    mandatory approval. Unconfigured means an empty set - no connector and no
    grant. No identity half: the desktop acts as the operator's own session,
    and the boundary is WHICH application, never who."""
    return frozenset(
        DestinationGrant(DESKTOP_CONNECTOR, capability, destination)
        for destination in settings.desktop_apps
        for capability in DESKTOP_ENABLED_CAPABILITIES
    )


def gmail_login_result(
    settings: Settings, attestation: GmailCredentialAttestation
) -> CommandResult:
    """Report the controlled login's attestation: non-secret identifiers of
    the exact credential instance every Gmail operation will now bind to."""
    payload = build_envelope("gmail-login", settings.permission_mode)
    payload["gmail_credential"] = attestation.to_dict()
    payload["next_step"] = (
        "The dedicated authorized-user credential is attested. Every Gmail "
        "operation binds to this exact credential instance; a replaced or "
        "re-created credential is refused until 'jarvis gmail login' runs again."
    )
    return CommandResult(_redact(payload), EXIT_OK)
