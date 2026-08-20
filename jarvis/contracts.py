"""Provider-independent contracts for the JARVIS core."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def future_time(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def is_expired(timestamp: str | None) -> bool:
    if not timestamp:
        return False
    return datetime.fromisoformat(timestamp) <= datetime.now(timezone.utc)


def normalize_symbol(value: str | Path | None) -> str:
    """Normalize a symbolic identifier without touching the filesystem.

    Used for names that merely *look* like paths, such as
    ``anthropic/claude-code``. The result never depends on the current working
    directory, so a permission fingerprint stays stable wherever JARVIS runs.
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def normalize_url(text: str) -> str | None:
    parsed = urlsplit(text)
    if not (parsed.scheme and parsed.netloc):
        return None
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def looks_like_path(text: str) -> bool:
    """Whether a non-URL locator names a filesystem location."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or any(
        separator in text for separator in ("/", "\\")
    )


def canonical_path(path: str | Path) -> str:
    """Windows-safe canonical form of a resolved filesystem path.

    Symlinks, junctions and ``..`` segments are resolved first; the result is
    case-folded by the platform rules and uses forward slashes only.
    """
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved)).replace("\\", "/")


def normalize_locator(value: str | Path | None) -> str:
    """Normalize a filesystem/URL locator to an absolute, canonical form."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    url = normalize_url(text)
    if url is not None:
        return url
    if looks_like_path(text):
        return canonical_path(text)
    return normalize_symbol(text)


def normalize_destination(value: str | Path | None) -> str:
    """Normalize an outbound destination endpoint.

    A destination is a real URL or a symbolic provider/connector endpoint such
    as ``anthropic/claude-code``. It is never resolved against the working
    directory; a filesystem location belongs in ``ActionScope.target``.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    url = normalize_url(text)
    return url if url is not None else normalize_symbol(text)


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class PermissionMode(str, Enum):
    STRICT = "strict"
    SESSION = "session"
    SMART_AUTO = "smart_auto"


class BillingClass(str, Enum):
    """Provider-neutral billing route of one execution.

    Classification is per execution route/call, never a static connector fact.
    """

    NO_ADDITIONAL_CHARGE = "no_additional_charge"
    SUBSCRIPTION_INCLUDED = "subscription_included"
    METERED_MONETARY = "metered_monetary"
    UNKNOWN = "unknown"


class MonetaryState(str, Enum):
    """Observation state of a monetary amount; 0.0 is not a substitute."""

    MEASURED = "measured"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


#: Only this class can move real money and therefore the monthly ledger.
MONETARY_BILLING_CLASSES = frozenset({BillingClass.METERED_MONETARY})


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class SideEffect(str, Enum):
    NONE = "none"
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_EGRESS = "external_egress"
    BILLING = "billing"
    PUSH_DEPLOY_PUBLISH = "push_deploy_publish"
    FINANCIAL_OR_FORM_SUBMIT = "financial_or_form_submit"


MANDATORY_APPROVAL_EFFECTS = frozenset(
    {
        SideEffect.DESTRUCTIVE,
        SideEffect.EXTERNAL_EGRESS,
        SideEffect.BILLING,
        SideEffect.PUSH_DEPLOY_PUBLISH,
        SideEffect.FINANCIAL_OR_FORM_SUBMIT,
    }
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_COST_OVERRIDE = "awaiting_cost_override"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_COST_OVERRIDE = "awaiting_cost_override"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LoopStage(str, Enum):
    UNDERSTAND = "understand"
    CONTEXT_RETRIEVAL = "context_retrieval"
    CLARIFY_OR_ASSUME = "clarify_or_visible_assumption"
    PLAN = "plan"
    RISK_PERMISSION = "risk_and_permission"
    COST_PREFLIGHT = "cost_preflight"
    TOOL_SELECTION = "tool_selection"
    EXECUTE = "execute"
    VERIFY = "verify"
    PRESENT = "present"
    PERSIST = "persist_decision_reason_next_step"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutcomeStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ErrorCode(str, Enum):
    UNKNOWN_SESSION = "unknown_session"
    TASK_NOT_FOUND = "task_not_found"
    NO_CONNECTOR = "no_connector"
    CONNECTOR_UNAVAILABLE = "connector_unavailable"
    CONNECTOR_AMBIGUOUS = "connector_ambiguous"
    CONNECTOR_FAILED = "connector_failed"
    INVALID_GRANT = "invalid_grant"
    STALE_GRANT = "stale_grant"
    APPROVAL_REJECTED = "approval_rejected"
    COST_CAP_EXCEEDED = "cost_cap_exceeded"
    PAID_API_DISABLED = "paid_api_disabled"
    BILLING_ROUTE_UNKNOWN = "billing_route_unknown"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"
    INVALID_TASK = "invalid_task"
    #: The connector refused to attest an external egress for this task
    #: (SECRET classification, a local-resource boundary it cannot honour).
    EGRESS_REFUSED = "egress_refused"


class AttestationError(Exception):
    """A permission/egress attestation cannot be produced; fail closed.

    Raised at the connector boundary before any approval request exists and
    again before execution. ``code`` is the structured error the loop reports.
    """

    def __init__(self, code: "ErrorCode", message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StructuredError:
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StructuredError | None":
        if data is None:
            return None
        return cls(
            code=ErrorCode(data["code"]),
            message=data["message"],
            details=data.get("details", {}),
            retryable=bool(data.get("retryable", False)),
        )


@dataclass(frozen=True, slots=True)
class ActionScope:
    action: str
    target: str = ""
    connector: str = ""
    destination: str = ""
    data_classification: DataClassification = DataClassification.INTERNAL

    def normalized(self) -> "ActionScope":
        # Field-aware on purpose: only ``target`` is a filesystem/URL locator.
        # ``action`` and ``connector`` are symbolic names, and ``destination``
        # is an outbound endpoint (symbolic or URL) - none of them may be
        # expanded against the current working directory.
        return ActionScope(
            action=normalize_symbol(self.action),
            target=normalize_locator(self.target),
            connector=normalize_symbol(self.connector),
            destination=normalize_destination(self.destination),
            data_classification=self.data_classification,
        )

    def to_dict(self) -> dict[str, Any]:
        normalized = self.normalized()
        return {
            "action": normalized.action,
            "target": normalized.target,
            "connector": normalized.connector,
            "destination": normalized.destination,
            "data_classification": normalized.data_classification.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionScope":
        return cls(
            action=data["action"],
            target=data.get("target", ""),
            connector=data.get("connector", ""),
            destination=data.get("destination", ""),
            data_classification=DataClassification(
                data.get("data_classification", DataClassification.INTERNAL.value)
            ),
        )


@dataclass(frozen=True, slots=True)
class SideEffectProfile:
    effect: SideEffect = SideEffect.NONE
    reversible: bool = True
    always_requires_approval: bool = False

    @property
    def mandatory(self) -> bool:
        return self.always_requires_approval or self.effect in MANDATORY_APPROVAL_EFFECTS


#: The only explicit safe combination rule between two distinct effects: a
#: task may raise a connector's *local* effect up this ladder (a read-only
#: connector asked to do a write is more restricted, never less). Every other
#: pair of distinct non-NONE effects is a conflict and fails closed.
_LOCAL_EFFECT_LADDER = (SideEffect.LOCAL_READ, SideEffect.LOCAL_WRITE, SideEffect.DESTRUCTIVE)


def merge_side_effects(
    manifest: SideEffectProfile, requested: SideEffectProfile
) -> SideEffectProfile:
    """Fail-closed merge: the connector manifest is the non-weakenable floor.

    Task/persisted state may strengthen (irreversible, always-approve, a
    higher local effect) but can never remove the connector's external-egress
    status, mandatory approval, irreversibility or effect. Raises
    ``AttestationError`` for conflicting non-NONE effects.
    """
    if manifest.effect is SideEffect.NONE or manifest.effect is requested.effect:
        effect = requested.effect
    elif requested.effect is SideEffect.NONE:
        effect = manifest.effect
    elif (
        manifest.effect in _LOCAL_EFFECT_LADDER
        and requested.effect in _LOCAL_EFFECT_LADDER
        and _LOCAL_EFFECT_LADDER.index(requested.effect)
        > _LOCAL_EFFECT_LADDER.index(manifest.effect)
    ):
        effect = requested.effect
    else:
        raise AttestationError(
            ErrorCode.INVALID_TASK,
            f"Task side effect '{requested.effect.value}' conflicts with the "
            f"connector side effect '{manifest.effect.value}'.",
        )
    return SideEffectProfile(
        effect=effect,
        reversible=manifest.reversible and requested.reversible,
        always_requires_approval=(
            manifest.always_requires_approval or requested.always_requires_approval
        ),
    )


@dataclass(frozen=True, slots=True)
class OutboundDataPreview:
    destination: str
    data_classification: DataClassification
    field_names: tuple[str, ...] = ()
    approximate_bytes: int = 0
    redacted_items: int = 0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["data_classification"] = self.data_classification.value
        data["field_names"] = list(self.field_names)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OutboundDataPreview | None":
        if data is None:
            return None
        return cls(
            destination=data["destination"],
            data_classification=DataClassification(data["data_classification"]),
            field_names=tuple(data.get("field_names", [])),
            approximate_bytes=int(data.get("approximate_bytes", 0)),
            redacted_items=int(data.get("redacted_items", 0)),
            summary=data.get("summary", ""),
        )


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Pre-execution billing route plus expected *monetary* spend.

    ``minimum_usd``/``maximum_usd`` are real money and are only meaningful for
    ``BillingClass.METERED_MONETARY``. A provider-side resource ceiling (for
    example a CLI ``--max-budget-usd`` safety bound) belongs in
    ``resource_ceiling_usd`` and never in the monthly monetary projection.
    """

    provider: str | None = None
    model: str | None = None
    minimum_usd: float = 0.0
    maximum_usd: float = 0.0
    confidence: str = "unknown"
    billing_class: BillingClass = BillingClass.UNKNOWN
    billing_evidence: str = ""
    resource_ceiling_usd: float | None = None
    #: Whether this route needs the separate ``paid_api_enabled`` user switch.
    #: This is a policy gate, not a billing classification.
    requires_paid_api: bool = False

    @property
    def is_paid(self) -> bool:
        """Deprecated compatibility view derived from ``billing_class``."""
        return self.billing_class in MONETARY_BILLING_CLASSES

    @property
    def monetary_maximum_usd(self) -> float:
        """Money this estimate may add to the monthly ledger."""
        return self.maximum_usd if self.is_paid else 0.0

    @property
    def gate_state(self) -> str:
        """The billing-critical policy inputs, without the evidence string.

        Evidence is deliberately excluded so a reworded, policy-equivalent
        classification cannot bounce a task between stages forever. Bound
        into every permission attestation and compared before execution.
        """
        ceiling = self.resource_ceiling_usd
        return "|".join(
            (
                self.billing_class.value,
                f"{self.monetary_maximum_usd:.9f}",
                str(int(self.requires_paid_api)),
                "none" if ceiling is None else f"{float(ceiling):.9f}",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["billing_class"] = self.billing_class.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CostEstimate":
        payload = dict(data or {})
        legacy_paid = payload.pop("is_paid", None)
        if legacy_paid:
            # A legacy ``is_paid`` flag proves a metered, switch-gated route.
            # A legacy false is ambiguous and must not be read as "free".
            payload.setdefault("billing_class", BillingClass.METERED_MONETARY.value)
            payload.setdefault("requires_paid_api", True)
        payload["billing_class"] = BillingClass(
            payload.get("billing_class", BillingClass.UNKNOWN.value)
        )
        return cls(**{key: value for key, value in payload.items() if key in _COST_FIELDS})


@dataclass(slots=True)
class UsageRecord:
    """Post-execution telemetry.

    ``monetary_cost_usd`` is incremental money attributable to the call and is
    the only value the monthly ledger sums. ``reference_cost_usd`` holds a
    provider-reported USD-equivalent that the user was not incrementally
    billed for (subscription-included usage), and is never money.
    """

    provider: str | None = None
    model: str | None = None
    connector: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0
    estimated_cost_usd: float = 0.0
    billing_class: BillingClass = BillingClass.UNKNOWN
    monetary_state: MonetaryState = MonetaryState.UNKNOWN
    monetary_cost_usd: float | None = None
    reference_cost_usd: float | None = None
    billing_evidence: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    recorded_at: str = field(default_factory=utc_now)

    @property
    def counts_toward_monetary_budget(self) -> bool:
        return (
            self.billing_class in MONETARY_BILLING_CLASSES
            and self.monetary_state is MonetaryState.MEASURED
            and self.monetary_cost_usd is not None
        )

    @property
    def budget_amount_usd(self) -> float:
        return (
            float(self.monetary_cost_usd)
            if self.counts_toward_monetary_budget and self.monetary_cost_usd is not None
            else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["billing_class"] = self.billing_class.value
        data["monetary_state"] = self.monetary_state.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UsageRecord":
        payload = dict(data or {})
        legacy_cost = payload.pop("actual_cost_usd", None)
        payload.pop("is_paid", None)
        if legacy_cost is not None and "monetary_cost_usd" not in payload:
            # Only a positive legacy amount is unambiguous incremental spend.
            # A legacy 0.0 stays UNKNOWN instead of becoming a measured zero.
            if float(legacy_cost) > 0:
                payload.setdefault("monetary_cost_usd", float(legacy_cost))
                payload.setdefault("billing_class", BillingClass.METERED_MONETARY.value)
                payload.setdefault("monetary_state", MonetaryState.MEASURED.value)
        payload["billing_class"] = BillingClass(
            payload.get("billing_class", BillingClass.UNKNOWN.value)
        )
        payload["monetary_state"] = MonetaryState(
            payload.get("monetary_state", MonetaryState.UNKNOWN.value)
        )
        return cls(
            **{key: value for key, value in payload.items() if key in _USAGE_FIELDS}
        )


_COST_FIELDS = frozenset(CostEstimate.__dataclass_fields__)
_USAGE_FIELDS = frozenset(UsageRecord.__dataclass_fields__)


# --------------------------------------------------------------------------
# Retrieved memory (same-session completed decisions)
# --------------------------------------------------------------------------

#: At most this many of the newest same-session decisions are examined per
#: retrieval. An internal safety bound, not the final result limit.
RETRIEVAL_CANDIDATE_WINDOW = 50

#: Absolute retrieval ceilings. Query values below them may narrow a
#: retrieval; values above them are clamped and never expand it.
RETRIEVAL_MAX_ITEMS = 5
RETRIEVAL_MAX_CHARACTERS = 4_000

#: Exact per-field character caps applied to the finally selected records.
RETRIEVAL_SUMMARY_CAP = 480
RETRIEVAL_RATIONALE_CAP = 240
RETRIEVAL_NEXT_STEP_CAP = 80


class RetrievedMemoryKind(str, Enum):
    DECISION = "decision"


@dataclass(frozen=True, slots=True)
class MemoryRetrievalQuery:
    session_id: str
    query_text: str
    exclude_task_id: str | None
    max_items: int = RETRIEVAL_MAX_ITEMS
    max_characters: int = RETRIEVAL_MAX_CHARACTERS


@dataclass(frozen=True, slots=True)
class RetrievedMemoryItem:
    kind: RetrievedMemoryKind
    record_id: str
    source_task_id: str
    summary: str
    rationale: str
    next_step: str | None
    classification: DataClassification
    created_at: str
    relevance_score: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "record_id": self.record_id,
            "source_task_id": self.source_task_id,
            "summary": self.summary,
            "rationale": self.rationale,
            "next_step": self.next_step,
            "classification": self.classification.value,
            "created_at": self.created_at,
            "relevance_score": self.relevance_score,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetrievedMemoryItem":
        return cls(
            kind=RetrievedMemoryKind(data.get("kind", RetrievedMemoryKind.DECISION.value)),
            record_id=data["record_id"],
            source_task_id=data["source_task_id"],
            summary=data.get("summary", ""),
            rationale=data.get("rationale", ""),
            next_step=data.get("next_step"),
            # An unreadable classification fails closed.
            classification=DataClassification(
                data.get("classification", DataClassification.SECRET.value)
            ),
            created_at=data.get("created_at", ""),
            relevance_score=int(data.get("relevance_score", 0)),
            truncated=bool(data.get("truncated", False)),
        )


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    items: tuple[RetrievedMemoryItem, ...] = ()
    candidate_count: int = 0
    character_count: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "candidate_count": self.candidate_count,
            "character_count": self.character_count,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RetrievedContext":
        if not data:
            return cls()
        return cls(
            items=tuple(
                RetrievedMemoryItem.from_dict(item) for item in data.get("items", [])
            ),
            candidate_count=int(data.get("candidate_count", 0)),
            character_count=int(data.get("character_count", 0)),
            truncated=bool(data.get("truncated", False)),
        )


def retrieval_tokens(value: Any) -> frozenset[str]:
    """Exact deterministic retrieval tokenizer.

    ``str`` -> NFKC -> casefold -> ``[^\\W_]+`` -> tokens whose length is at
    least 3, as a set. No stop words, stemming, fuzzy or substring matching,
    synonym expansion, model ranking or language-specific handling.
    """
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return frozenset(
        token
        for token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
        if len(token) >= 3
    )


#: Fixed classification order used only for retrieval eligibility.
_RETRIEVAL_CLASSIFICATION_RANK = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.SENSITIVE: 2,
    DataClassification.SECRET: 3,
}


def retrieval_eligible(
    current: DataClassification | None, candidate: DataClassification
) -> bool:
    """Exact eligibility matrix; an unknown current classification fails closed.

    Shared by the retrieval pipeline and by every egress boundary that must
    independently re-validate a persisted snapshot.
    """
    if current is None:
        return False
    if current is DataClassification.SECRET or candidate is DataClassification.SECRET:
        return False
    return (
        _RETRIEVAL_CLASSIFICATION_RANK[candidate]
        <= _RETRIEVAL_CLASSIFICATION_RANK[current]
    )


def normalize_retrieved_text(value: Any) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def truncate_retrieved_text(value: str, cap: int) -> tuple[str, bool]:
    if len(value) <= cap:
        return value, False
    return value[: cap - 1] + "…", True


def build_retrieved_context(
    query: MemoryRetrievalQuery,
    current_classification: DataClassification | None,
    candidates: Sequence[tuple["DecisionRecord", DataClassification]],
) -> RetrievedContext:
    """Score, rank, filter, select and truncate one bounded candidate window.

    The order is mandatory: relevance is computed on full untruncated content,
    zero-score records are dropped, ranking happens before the classification
    filter so an ineligible record never consumes a result slot, and only the
    finally selected records are normalized and truncated.

    ``RETRIEVAL_MAX_ITEMS`` and ``RETRIEVAL_MAX_CHARACTERS`` are absolute
    ceilings: a query may narrow them but can never expand them.
    """
    effective_max_items = max(0, min(query.max_items, RETRIEVAL_MAX_ITEMS))
    effective_max_characters = max(
        0, min(query.max_characters, RETRIEVAL_MAX_CHARACTERS)
    )
    query_tokens = retrieval_tokens(query.query_text)
    scored: list[tuple[Any, DataClassification, int]] = []
    for record, classification in candidates:
        # Candidate tokens come from the full values of summary, rationale
        # and next_step, in that fixed order; duplicates count once.
        candidate_tokens = retrieval_tokens(record.summary) | retrieval_tokens(
            record.rationale
        )
        if record.next_step is not None:
            candidate_tokens |= retrieval_tokens(record.next_step)
        score = len(query_tokens & candidate_tokens)
        if score > 0:
            scored.append((record, classification, score))
    # Stable triple sort: relevance desc, created_at desc, decision id asc.
    scored.sort(key=lambda entry: entry[0].id)
    scored.sort(key=lambda entry: entry[0].created_at, reverse=True)
    scored.sort(key=lambda entry: entry[2], reverse=True)

    selected: list[RetrievedMemoryItem] = []
    character_count = 0
    overflow = False
    for record, classification, score in scored:
        if not retrieval_eligible(current_classification, classification):
            continue
        if len(selected) >= effective_max_items:
            overflow = True
            break
        summary, summary_truncated = truncate_retrieved_text(
            normalize_retrieved_text(record.summary), RETRIEVAL_SUMMARY_CAP
        )
        rationale, rationale_truncated = truncate_retrieved_text(
            normalize_retrieved_text(record.rationale), RETRIEVAL_RATIONALE_CAP
        )
        if record.next_step is None:
            next_step: str | None = None
            next_step_truncated = False
            next_step_length = 0
        else:
            next_step, next_step_truncated = truncate_retrieved_text(
                normalize_retrieved_text(record.next_step), RETRIEVAL_NEXT_STEP_CAP
            )
            next_step_length = len(next_step)
        contribution = len(summary) + len(rationale) + next_step_length
        if character_count + contribution > effective_max_characters:
            overflow = True
            break
        selected.append(
            RetrievedMemoryItem(
                kind=RetrievedMemoryKind.DECISION,
                record_id=record.id,
                source_task_id=record.task_id,
                summary=summary,
                rationale=rationale,
                next_step=next_step,
                classification=classification,
                created_at=record.created_at,
                relevance_score=score,
                truncated=summary_truncated
                or rationale_truncated
                or next_step_truncated,
            )
        )
        character_count += contribution
    return RetrievedContext(
        items=tuple(selected),
        candidate_count=len(candidates),
        character_count=character_count,
        truncated=overflow or any(item.truncated for item in selected),
    )


# --------------------------------------------------------------------------
# Task-scoped progress (informational display state, never lifecycle authority)
# --------------------------------------------------------------------------

#: Hard ceilings for persisted progress collections.
PROGRESS_MAX_ISSUES = 8
PROGRESS_MAX_ARTIFACTS = 20
PROGRESS_TEXT_MAX_CHARS = 240

#: Terminal states; they never appear in ``completed_stages``.
TERMINAL_LOOP_STAGES = frozenset(
    {LoopStage.COMPLETED, LoopStage.FAILED, LoopStage.CANCELLED}
)

#: The deterministic linear work-stage order, for informational progress only.
LINEAR_LOOP_STAGES: tuple[LoopStage, ...] = (
    LoopStage.UNDERSTAND,
    LoopStage.CONTEXT_RETRIEVAL,
    LoopStage.CLARIFY_OR_ASSUME,
    LoopStage.PLAN,
    LoopStage.RISK_PERMISSION,
    LoopStage.COST_PREFLIGHT,
    LoopStage.TOOL_SELECTION,
    LoopStage.EXECUTE,
    LoopStage.VERIFY,
    LoopStage.PRESENT,
    LoopStage.PERSIST,
)

_NEXT_LOOP_STAGE: dict[LoopStage, LoopStage] = {
    **dict(zip(LINEAR_LOOP_STAGES, LINEAR_LOOP_STAGES[1:])),
    LoopStage.PERSIST: LoopStage.COMPLETED,
}


def next_loop_stage(stage: LoopStage) -> "LoopStage | None":
    """Informational linear successor of a stage, or ``None`` at a terminal.

    Never a transition authority: it does not imply the successor may run
    now, it bypasses no clarification/approval/cost/cancellation blocker and
    it feeds no permission, billing, selection, execution, verification or
    outbound decision.
    """
    if type(stage) is not LoopStage:
        return None
    return _NEXT_LOOP_STAGE.get(stage)


def _legacy_completed_stages(stage: LoopStage) -> tuple[LoopStage, ...]:
    """Conservative derivation for legacy task JSON without progress.

    A completed task traversed every linear work stage; a nonterminal task
    traversed the linear predecessors of its current stage; a failed or
    cancelled task gets nothing invented.
    """
    if stage is LoopStage.COMPLETED:
        return LINEAR_LOOP_STAGES
    if stage in _NEXT_LOOP_STAGE:
        return LINEAR_LOOP_STAGES[: LINEAR_LOOP_STAGES.index(stage)]
    return ()


class ProgressIssueKind(str, Enum):
    CLARIFICATION_REQUIRED = "clarification_required"
    APPROVAL_REQUIRED = "approval_required"
    COST_OVERRIDE_REQUIRED = "cost_override_required"
    INVALID_GRANT = "invalid_grant"
    CANCELLATION_PENDING = "cancellation_pending"
    FAILURE = "failure"


class ProgressArtifactKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    ARTIFACT = "artifact"


class ProgressArtifactChange(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    GENERATED = "generated"


def normalize_progress_text(value: Any) -> str | None:
    """Bounded display-text normalization for progress metadata.

    Only an actual non-empty string survives; arbitrary objects are never
    stringified. NFKC, then CRLF/CR to LF, then whitespace-run collapse,
    then a 240-code-point cap with a final U+2026 on overflow.
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > PROGRESS_TEXT_MAX_CHARS:
        return text[: PROGRESS_TEXT_MAX_CHARS - 1] + "…"
    return text


#: Fallback session next step; also replaces credential-bearing values.
DEFAULT_SESSION_NEXT_STEP = "Review the verified result."

#: Syntax-aware credential screening for persisted progress/session text.
#: Owned here so runtime creation (``jarvis.progress``) and deserialization
#: share one canonical, provider-independent policy. A recognized credential
#: label alone is not enough: credential syntax — a ``:`` or ``=`` delimiter
#: followed by a non-empty value — is required, so ordinary prose and
#: filenames such as "Review token telemetry", "tokenizer.py" or
#: "secret_handling.md" stay valid. The leading boundary is a negative
#: alphanumeric lookbehind rather than ``\b``: identifier separators such as
#: ``_`` and ``-`` still expose the sensitive term (``access_token=``,
#: ``client-secret:``), while a term embedded in an ordinary alphanumeric
#: word (``mytoken=``) does not match. Independent credential shapes are
#: always rejected. Presentation redaction in ``jarvis.app`` stays a
#: separate, deliberately broader second defense.
_CREDENTIAL_LABEL = (
    r"api[_\-\s]?key|token|secret|password|passwd|authorization|cookie"
    r"|private[_\-\s]?key"
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_CREDENTIAL_LABEL})\b\s*[:=]\s*\S", re.IGNORECASE
)
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


#: Redaction form of the assignment rule: the label and delimiter survive, the
#: value runs to the end of the field so a space-separated credential cannot
#: escape; over-redaction is the safe direction at an egress boundary.
_CREDENTIAL_REDACTION_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])({_CREDENTIAL_LABEL})(\s*[:=]\s*)([^,;\n]+)", re.IGNORECASE
)


def credential_bearing(text: str) -> bool:
    """Whether text carries a labelled credential assignment or a raw shape."""
    if _CREDENTIAL_ASSIGNMENT_PATTERN.search(text):
        return True
    return any(pattern.search(text) for pattern in _CREDENTIAL_VALUE_PATTERNS)


def redact_credentials(text: str, replacement: str = "[REDACTED]") -> tuple[str, int]:
    """Redact raw credential shapes and labelled credential values in text.

    Shape patterns run first so an ``authorization: Bearer <token>`` header
    is already reduced before the label rule sees it. Returns the redacted
    text and the number of replacements. Shared by every egress boundary.
    """
    count = 0
    for pattern in _CREDENTIAL_VALUE_PATTERNS:
        text, replaced = pattern.subn(replacement, text)
        count += replaced
    text, replaced = _CREDENTIAL_REDACTION_PATTERN.subn(
        lambda match: f"{match.group(1)}{match.group(2)}{replacement}", text
    )
    return text, count + replaced


def normalize_session_next_step(value: Any) -> str:
    """Bounded, credential-safe session next step.

    Only an actual string is accepted; arbitrary connector data is never
    stringified. A credential-bearing value is replaced entirely. The raw
    value is screened before normalization so content beyond the cap and
    shapes only assembled by NFKC/whitespace folding are both caught. Applied
    both when a connector result produces the value and when persisted
    session JSON is loaded, so unsafe text cannot survive a load/resave.
    """
    if isinstance(value, str) and credential_bearing(value):
        return DEFAULT_SESSION_NEXT_STEP
    text = normalize_progress_text(value)
    if text is None or credential_bearing(text):
        return DEFAULT_SESSION_NEXT_STEP
    return text


@dataclass(frozen=True, slots=True)
class ProgressIssue:
    """One typed open issue. No free-form message field exists on purpose."""

    kind: ProgressIssueKind
    error_code: ErrorCode | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "error_code": self.error_code.value if self.error_code else None,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProgressIssue | None":
        """Fail closed: any malformed field drops the whole issue."""
        if not isinstance(data, dict):
            return None
        kind_value = data.get("kind")
        if not isinstance(kind_value, str):
            return None
        try:
            kind = ProgressIssueKind(kind_value)
        except ValueError:
            return None
        code_value = data.get("error_code")
        error_code: ErrorCode | None = None
        if code_value is not None:
            if not isinstance(code_value, str):
                return None
            try:
                error_code = ErrorCode(code_value)
            except ValueError:
                return None
        retryable = data.get("retryable", False)
        if not isinstance(retryable, bool):
            return None
        return cls(kind=kind, error_code=error_code, retryable=retryable)


@dataclass(frozen=True, slots=True)
class ProgressArtifact:
    """Informational display metadata only.

    A label is never resolved, opened, executed or treated as a filesystem
    path.
    """

    kind: ProgressArtifactKind
    change: ProgressArtifactChange
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "change": self.change.value,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProgressArtifact | None":
        """Fail closed: any malformed or credential-bearing field drops the
        whole artifact, so unsafe persisted labels cannot survive a load."""
        if not isinstance(data, dict):
            return None
        kind_value = data.get("kind")
        change_value = data.get("change")
        if not isinstance(kind_value, str) or not isinstance(change_value, str):
            return None
        try:
            kind = ProgressArtifactKind(kind_value)
            change = ProgressArtifactChange(change_value)
        except ValueError:
            return None
        raw_label = data.get("label")
        # Screen the raw text first (truncation cannot hide a credential),
        # then the normalized form (NFKC/whitespace folding cannot assemble
        # one) — the same double screening the artifact builder applies.
        if not isinstance(raw_label, str) or credential_bearing(raw_label):
            return None
        label = normalize_progress_text(raw_label)
        if label is None or credential_bearing(label):
            return None
        return cls(kind=kind, change=change, label=label)


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Typed, bounded, task-scoped progress snapshot.

    Purely informational: ``Task.status``/``Task.stage``, approvals, billing
    state and verification remain the only lifecycle authorities.
    """

    completed_stages: tuple[LoopStage, ...] = ()
    open_issues: tuple[ProgressIssue, ...] = ()
    artifacts: tuple[ProgressArtifact, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_stages": [stage.value for stage in self.completed_stages],
            "open_issues": [issue.to_dict() for issue in self.open_issues],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TaskProgress":
        """Fail closed per element; ordering and hard ceilings are enforced."""
        if not isinstance(data, dict):
            return cls()
        stages: list[LoopStage] = []
        raw_stages = data.get("completed_stages", [])
        if isinstance(raw_stages, (list, tuple)):
            for value in raw_stages:
                if not isinstance(value, str):
                    continue
                try:
                    stage = LoopStage(value)
                except ValueError:
                    continue
                if stage in TERMINAL_LOOP_STAGES or stage in stages:
                    continue
                stages.append(stage)
        issues: list[ProgressIssue] = []
        raw_issues = data.get("open_issues", [])
        if isinstance(raw_issues, (list, tuple)):
            for value in raw_issues:
                issue = ProgressIssue.from_dict(value)
                if issue is None or issue in issues:
                    continue
                issues.append(issue)
                if len(issues) >= PROGRESS_MAX_ISSUES:
                    break
        artifacts: list[ProgressArtifact] = []
        raw_artifacts = data.get("artifacts", [])
        if isinstance(raw_artifacts, (list, tuple)):
            for value in raw_artifacts:
                artifact = ProgressArtifact.from_dict(value)
                if artifact is None or artifact in artifacts:
                    continue
                artifacts.append(artifact)
                if len(artifacts) >= PROGRESS_MAX_ARTIFACTS:
                    break
        return cls(
            completed_stages=tuple(stages),
            open_issues=tuple(issues),
            artifacts=tuple(artifacts),
        )


@dataclass(slots=True)
class Task:
    objective: str
    capability: str
    scope: ActionScope
    context: dict[str, Any] = field(default_factory=dict)
    estimate: CostEstimate = field(default_factory=CostEstimate)
    preferences: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    side_effect: SideEffectProfile = field(default_factory=SideEffectProfile)
    id: str = field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = TaskStatus.PENDING
    stage: LoopStage = LoopStage.UNDERSTAND
    plan: list[str] = field(default_factory=list)
    visible_assumption: str | None = None
    selected_connector: str | None = None
    result_snapshot: dict[str, Any] | None = None
    #: Internal typed snapshot taken once at CONTEXT_RETRIEVAL. Never accepted
    #: from untrusted task input and never shown in the CLI output envelope.
    retrieved_context: RetrievedContext = field(default_factory=RetrievedContext)
    #: Typed informational progress maintained by AgentCore. Never accepted
    #: from untrusted task input and never consumed as lifecycle authority.
    progress: TaskProgress = field(default_factory=TaskProgress)
    cancellation_requested: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "capability": self.capability,
            "scope": self.scope.to_dict(),
            "context": self.context,
            "estimate": self.estimate.to_dict(),
            "preferences": self.preferences,
            "risk_level": int(self.risk_level),
            "side_effect": {
                "effect": self.side_effect.effect.value,
                "reversible": self.side_effect.reversible,
                "always_requires_approval": self.side_effect.always_requires_approval,
            },
            "id": self.id,
            "status": self.status.value,
            "stage": self.stage.value,
            "plan": self.plan,
            "visible_assumption": self.visible_assumption,
            "selected_connector": self.selected_connector,
            "result_snapshot": self.result_snapshot,
            "retrieved_context": self.retrieved_context.to_dict(),
            "progress": self.progress.to_dict(),
            "cancellation_requested": self.cancellation_requested,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        effect = data.get("side_effect", {})
        stage = LoopStage(data.get("stage", LoopStage.UNDERSTAND.value))
        raw_progress = data.get("progress")
        if isinstance(raw_progress, dict):
            progress = TaskProgress.from_dict(raw_progress)
        else:
            # Legacy or malformed progress: derive only the deterministic
            # linear predecessors; FAILED/CANCELLED never invent stages.
            progress = TaskProgress(
                completed_stages=_legacy_completed_stages(stage)
            )
        return cls(
            objective=data["objective"],
            capability=data["capability"],
            scope=ActionScope.from_dict(data["scope"]),
            context=data.get("context", {}),
            estimate=CostEstimate.from_dict(data.get("estimate")),
            preferences=data.get("preferences", {}),
            risk_level=RiskLevel(data.get("risk_level", RiskLevel.LOW)),
            side_effect=SideEffectProfile(
                effect=SideEffect(effect.get("effect", SideEffect.NONE.value)),
                reversible=bool(effect.get("reversible", True)),
                always_requires_approval=bool(
                    effect.get("always_requires_approval", False)
                ),
            ),
            id=data["id"],
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            stage=stage,
            plan=list(data.get("plan", [])),
            visible_assumption=data.get("visible_assumption"),
            selected_connector=data.get("selected_connector"),
            result_snapshot=data.get("result_snapshot"),
            # Legacy task JSON without the field loads as an empty snapshot.
            retrieved_context=RetrievedContext.from_dict(
                data.get("retrieved_context")
            ),
            progress=progress,
            cancellation_requested=bool(data.get("cancellation_requested", False)),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )


@dataclass(frozen=True, slots=True)
class EgressAttestation:
    """The exact external-effect state one permission grant authorizes.

    Provider-independent: a connector fills in what *it* knows (normalized
    scope incl. connector/destination/classification, effective merged side
    effect, the digest of the exact canonical bytes it will send, the local
    resource boundary the external process can inspect) and AgentCore binds
    the gate state (billing classification/ceiling and, for cost approvals,
    the monetary gate). Equality of two attestations means the same data
    leaves to the same place under the same gates from the same boundary.
    """

    scope: ActionScope
    risk_level: RiskLevel
    side_effect: SideEffectProfile
    #: SHA-256 of the exact sanitized outbound bytes execution will use.
    outbound_digest: str
    #: ``CostEstimate.gate_state`` of the connector-derived estimate.
    billing_gate: str = ""
    #: Monetary gate being approved (``gate|projected|limit|paid_api``), or
    #: empty for a plain egress/permission request.
    cost_gate: str = ""
    #: Canonical resolved working directory the external process runs in.
    working_directory: str = ""
    #: Canonical allowed roots the connector may expose; the approved boundary.
    allowed_roots: tuple[str, ...] = ()
    #: The exact external tool grants the process is launched with - the
    #: local-resource inspection manifest. Empty means the external model can
    #: receive nothing beyond the attested outbound bytes; any tool-mediated
    #: file read would be a channel outside those bytes, so an empty grant set
    #: is the only fully attestable state until a bounded, enforceable read
    #: manifest exists. Bound into the digest so a changed grant set (or a
    #: connector that silently re-enables tools) invalidates the approval.
    tool_grants: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "risk_level": int(self.risk_level),
            "side_effect": {
                "effect": self.side_effect.effect.value,
                "reversible": self.side_effect.reversible,
                "always_requires_approval": self.side_effect.always_requires_approval,
            },
            "outbound_digest": self.outbound_digest,
            "billing_gate": self.billing_gate,
            "cost_gate": self.cost_gate,
            "working_directory": self.working_directory,
            "allowed_roots": list(self.allowed_roots),
            "tool_grants": list(self.tool_grants),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EgressAttestation | None":
        if not data:
            return None
        effect = data["side_effect"]
        return cls(
            scope=ActionScope.from_dict(data["scope"]),
            risk_level=RiskLevel(data["risk_level"]),
            side_effect=SideEffectProfile(
                effect=SideEffect(effect["effect"]),
                reversible=bool(effect["reversible"]),
                always_requires_approval=bool(effect["always_requires_approval"]),
            ),
            outbound_digest=str(data["outbound_digest"]),
            billing_gate=str(data.get("billing_gate", "")),
            cost_gate=str(data.get("cost_gate", "")),
            working_directory=str(data.get("working_directory", "")),
            allowed_roots=tuple(str(root) for root in data.get("allowed_roots", [])),
            tool_grants=tuple(str(tool) for tool in data.get("tool_grants", [])),
        )

    def display_manifest(self) -> dict[str, Any]:
        """Safe, user-facing view of the approved boundary and manifest.

        Only digests, canonical bounded paths and tool-grant names; no raw
        prompt, credentials or retrieved-memory internals.
        """
        return {
            "outbound_digest": self.outbound_digest,
            "billing_gate": self.billing_gate,
            "cost_gate": self.cost_gate,
            "connector": self.scope.connector,
            "destination": self.scope.destination,
            "classification": self.scope.data_classification.value,
            "working_directory": self.working_directory,
            "allowed_roots": list(self.allowed_roots),
            "tool_grants": list(self.tool_grants),
        }

    @property
    def digest(self) -> str:
        """Deterministic SHA-256 over the canonical attestation encoding."""
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    task_id: str
    session_id: str
    scope: ActionScope
    risk_level: RiskLevel
    side_effect: SideEffectProfile
    reason: str
    outbound_preview: OutboundDataPreview | None = None
    #: Absent only for legacy persisted requests, which can then never match
    #: a current attestation and are superseded by a fresh request.
    attestation: EgressAttestation | None = None
    expires_at: str = field(default_factory=lambda: future_time(30))
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            # A fresh, unique issuance identity - never derived from the
            # semantic fingerprint. Two requests with identical scope and
            # attestation (for example a re-issue after an expired one) are
            # distinct issuances, so an old grant can never authorize a newly
            # issued request even when the semantic fingerprint is identical.
            object.__setattr__(self, "id", str(uuid4()))

    def as_monetary_gate(self, cost_gate: str) -> "PermissionRequest":
        """AgentCore's view of this request as a monetary gate approval.

        Money is HIGH risk, irreversible, always approved; the gate state is
        bound into the attestation and the id is re-derived from it.
        """
        profile = SideEffectProfile(
            effect=SideEffect.BILLING, reversible=False, always_requires_approval=True
        )
        risk = max(self.risk_level, RiskLevel.HIGH)
        attestation = (
            None
            if self.attestation is None
            else replace(
                self.attestation, risk_level=risk, side_effect=profile, cost_gate=cost_gate
            )
        )
        return replace(
            self, risk_level=risk, side_effect=profile, attestation=attestation, id=""
        )

    @property
    def scope_fingerprint(self) -> str:
        payload = {
            "session_id": self.session_id,
            "scope": self.scope.to_dict(),
            "risk_level": int(self.risk_level),
            "side_effect": self.side_effect.effect.value,
            "reversible": self.side_effect.reversible,
            "always": self.side_effect.mandatory,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @property
    def display_digest(self) -> str:
        """Digest of the security-relevant fields shown to the user.

        The reason and the redacted outbound preview are what the user reads
        when approving; binding them means a stale request whose displayed
        reason or preview was mutated can never be presented as current.
        """
        payload = {
            "reason": self.reason,
            "outbound_preview": (
                self.outbound_preview.to_dict()
                if self.outbound_preview is not None
                else None
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Binds task, scope, the exact egress attestation and the approval
        display a grant covers. Any change to the outbound digest, gate state,
        resource boundary, tool grants, reason or preview changes it."""
        attested = self.attestation.digest if self.attestation is not None else ""
        return hashlib.sha256(
            f"{self.task_id}|{self.scope_fingerprint}|{attested}|{self.display_digest}".encode(
                "utf-8"
            )
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "scope": self.scope.to_dict(),
            "risk_level": int(self.risk_level),
            "side_effect": {
                "effect": self.side_effect.effect.value,
                "reversible": self.side_effect.reversible,
                "always_requires_approval": self.side_effect.always_requires_approval,
            },
            "reason": self.reason,
            "outbound_preview": (
                self.outbound_preview.to_dict() if self.outbound_preview else None
            ),
            "attestation": (
                self.attestation.to_dict() if self.attestation is not None else None
            ),
            "expires_at": self.expires_at,
            "id": self.id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionRequest":
        effect = data["side_effect"]
        return cls(
            task_id=data["task_id"],
            session_id=data["session_id"],
            scope=ActionScope.from_dict(data["scope"]),
            risk_level=RiskLevel(data["risk_level"]),
            side_effect=SideEffectProfile(
                effect=SideEffect(effect["effect"]),
                reversible=bool(effect["reversible"]),
                always_requires_approval=bool(effect["always_requires_approval"]),
            ),
            reason=data["reason"],
            outbound_preview=OutboundDataPreview.from_dict(
                data.get("outbound_preview")
            ),
            attestation=EgressAttestation.from_dict(data.get("attestation")),
            expires_at=data["expires_at"],
            id=data["id"],
        )


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    request_id: str
    task_id: str
    session_id: str
    fingerprint: str
    approved: bool
    expires_at: str
    remember_for_session: bool = False
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def for_request(
        cls,
        request: PermissionRequest,
        approved: bool = True,
        remember_for_session: bool = False,
        valid_minutes: int = 15,
    ) -> "PermissionGrant":
        return cls(
            request_id=request.id,
            task_id=request.task_id,
            session_id=request.session_id,
            fingerprint=request.fingerprint,
            approved=approved,
            expires_at=future_time(valid_minutes),
            remember_for_session=remember_for_session,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionGrant":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    status: VerificationStatus = VerificationStatus.NOT_RUN
    method: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.status is VerificationStatus.PASSED
            and bool(self.method.strip())
            and bool(self.evidence)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "method": self.method,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "VerificationRecord":
        if not data:
            return cls()
        return cls(
            status=VerificationStatus(data["status"]),
            method=data.get("method", ""),
            evidence=tuple(data.get("evidence", [])),
        )


@dataclass(slots=True)
class ToolResult:
    outcome: OutcomeStatus
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: StructuredError | None = None
    usage: UsageRecord = field(default_factory=UsageRecord)
    verification: VerificationRecord = field(default_factory=VerificationRecord)

    @property
    def ok(self) -> bool:
        return self.outcome is OutcomeStatus.SUCCESS and self.error is None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    session_id: str
    task_id: str
    summary: str
    rationale: str
    next_step: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRecord":
        return cls(**data)


@dataclass(slots=True)
class SessionState:
    objective: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    active_task_id: str | None = None
    #: Navigation pointer to the most recently touched task. Informational
    #: only; it survives terminal completion while ``active_task_id`` clears.
    latest_task_id: str | None = None
    next_step: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        allowed = {
            "objective",
            "id",
            "active_task_id",
            "latest_task_id",
            "next_step",
            "created_at",
            "updated_at",
        }
        payload = {key: value for key, value in data.items() if key in allowed}
        # Legacy session JSON without the pointer defaults to the active task.
        if not isinstance(payload.get("latest_task_id"), str):
            active = payload.get("active_task_id")
            payload["latest_task_id"] = active if isinstance(active, str) else None
        # The persisted next step is untrusted display text: the same
        # canonical policy that bounds newly produced connector results runs
        # again at load, so unsafe text cannot survive a load/resave cycle.
        payload["next_step"] = normalize_session_next_step(payload.get("next_step"))
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LoopCheckpoint:
    task_id: str
    session_id: str
    stage: LoopStage
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AgentRun:
    status: RunStatus
    session: SessionState | None
    task: Task | None
    result: ToolResult | None = None
    permission_request: PermissionRequest | None = None
    error: StructuredError | None = None


@dataclass(frozen=True, slots=True)
class Availability:
    available: bool
    reason: str = ""


@runtime_checkable
class ModelAdapter(Protocol):
    name: str

    def availability(self) -> Availability: ...

    def estimate_cost(self, task: Task) -> CostEstimate: ...

    def complete(self, task: Task, session: SessionState) -> ToolResult: ...

    def cancel(self, task_id: str) -> bool: ...


class MemoryStore(Protocol):
    """Provider-independent persistence surface required by ``AgentCore``."""

    def save_session(self, session: SessionState) -> None: ...

    def load_session(self, session_id: str) -> SessionState | None: ...

    def save_task(self, session_id: str, task: Task) -> None: ...

    def load_task(self, task_id: str) -> Task | None: ...

    def task_session_id(self, task_id: str) -> str | None: ...

    def persist_checkpoint(
        self,
        session: SessionState,
        task: Task,
        event_type: str,
        payload: dict[str, Any] | None = None,
        pending_request: PermissionRequest | None = None,
    ) -> None: ...

    def list_decisions(self, session_id: str) -> list[DecisionRecord]: ...

    def retrieve_context(self, query: MemoryRetrievalQuery) -> RetrievedContext: ...

    def save_pending_approval(self, request: PermissionRequest) -> None: ...

    def load_pending_approval(
        self, task_id: str, session_id: str
    ) -> PermissionRequest | None: ...

    def resolve_approval(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        status: str,
    ) -> None: ...

    def has_remembered_scope_grant(
        self, session_id: str, scope_fingerprint: str
    ) -> bool: ...

    def has_approved_grant(
        self, session_id: str, task_id: str, fingerprint: str
    ) -> bool:
        """Whether an approved grant was recorded for this exact fingerprint.

        The grant record, never persisted task state, is the proof that a
        permission/cost gate was satisfied for the attested request.
        """
        ...

    def append_usage(self, usage: UsageRecord) -> None: ...

    def monthly_monetary_cost(self, month: str | None = None) -> float: ...

    def finalize_task(
        self,
        session: SessionState,
        task: Task,
        decision: DecisionRecord,
        usage: UsageRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> None: ...


@runtime_checkable
class ResultVerifier(Protocol):
    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord: ...
