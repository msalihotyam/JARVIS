"""Provider-independent contracts for the JARVIS core."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5


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


def _normalize_url(text: str) -> str | None:
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


def normalize_locator(value: str | Path | None) -> str:
    """Normalize a filesystem/URL locator to an absolute, canonical form."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    url = _normalize_url(text)
    if url is not None:
        return url
    looks_like_path = bool(re.match(r"^[A-Za-z]:[\\/]", text)) or any(
        separator in text for separator in ("/", "\\")
    )
    if looks_like_path:
        resolved = Path(text).expanduser().resolve(strict=False)
        return os.path.normcase(str(resolved)).replace("\\", "/")
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
    url = _normalize_url(text)
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
            "cancellation_requested": self.cancellation_requested,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        effect = data.get("side_effect", {})
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
            stage=LoopStage(data.get("stage", LoopStage.UNDERSTAND.value)),
            plan=list(data.get("plan", [])),
            visible_assumption=data.get("visible_assumption"),
            selected_connector=data.get("selected_connector"),
            result_snapshot=data.get("result_snapshot"),
            # Legacy task JSON without the field loads as an empty snapshot.
            retrieved_context=RetrievedContext.from_dict(
                data.get("retrieved_context")
            ),
            cancellation_requested=bool(data.get("cancellation_requested", False)),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    task_id: str
    session_id: str
    scope: ActionScope
    risk_level: RiskLevel
    side_effect: SideEffectProfile
    reason: str
    outbound_preview: OutboundDataPreview | None = None
    expires_at: str = field(default_factory=lambda: future_time(30))
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", str(uuid5(NAMESPACE_URL, self.fingerprint)))

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
    def fingerprint(self) -> str:
        return hashlib.sha256(
            f"{self.task_id}|{self.scope_fingerprint}".encode("utf-8")
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
    next_step: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        allowed = {"objective", "id", "active_task_id", "next_step", "created_at", "updated_at"}
        return cls(**{key: value for key, value in data.items() if key in allowed})


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
