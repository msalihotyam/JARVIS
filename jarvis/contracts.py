"""Provider-independent contracts for the JARVIS core."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
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
    #: Cancellation intent is durable but the task was still executing and the
    #: transition to a final CANCELLED could not be confirmed. Never reported
    #: as final CANCELLED (H2-F): the executing owner finalizes on return.
    CANCELLING = "cancelling"
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
    #: Reserved, currently unproduced (H5): a projected-cost breach surfaces
    #: as a COST_CAP_GATE approval request, never as this terminal error.
    #: Kept for persisted-contract compatibility; do not remove or repurpose.
    COST_CAP_EXCEEDED = "cost_cap_exceeded"
    #: Reserved, currently unproduced (H5): the disabled paid-API switch
    #: surfaces as a PAID_API_GATE approval request, never as this terminal
    #: error. Kept for persisted-contract compatibility.
    PAID_API_DISABLED = "paid_api_disabled"
    BILLING_ROUTE_UNKNOWN = "billing_route_unknown"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"
    INVALID_TASK = "invalid_task"
    #: The connector refused to attest an external egress for this task
    #: (SECRET classification, a local-resource boundary it cannot honour).
    EGRESS_REFUSED = "egress_refused"
    #: A prior execution attempt is in doubt (IN_FLIGHT after restart, or an
    #: outcome the owner could not establish). Non-replayable; needs deferred
    #: recovery. JARVIS refuses to automatically execute it again.
    EXECUTION_UNKNOWN_OUTCOME = "execution_unknown_outcome"
    #: Another local execution owner already holds this task's attempt; a
    #: concurrent resume observed the live claim and did not execute.
    EXECUTION_IN_PROGRESS = "execution_in_progress"
    #: A command targeted a task that is already in a terminal state.
    #: Reserved, currently unproduced (H5): terminal-state refusals surface
    #: through the lifecycle arbitration codes above today. Kept for
    #: persisted-contract compatibility.
    TASK_ALREADY_TERMINAL = "task_already_terminal"
    #: H3: canonical monetary validation, ledger integrity or authoritative
    #: usage reconciliation failed. Always fail closed; never a retryable
    #: "probably fine" state, and never accompanied by a completed decision.
    BILLING_INTEGRITY_FAILED = "billing_integrity_failed"


class AttestationError(Exception):
    """A permission/egress attestation cannot be produced; fail closed.

    Raised at the connector boundary before any approval request exists and
    again before execution. ``code`` is the structured error the loop reports.
    """

    def __init__(self, code: "ErrorCode", message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# H3 - canonical, provider-independent monetary validation
# --------------------------------------------------------------------------

#: Canonical monetary precision: exactly nine decimal places (nano-USD).
MONETARY_DECIMAL_PLACES = 9
_NANO_EXPONENT = Decimal(1).scaleb(-MONETARY_DECIMAL_PLACES)
NANO_USD_SCALE = 10**MONETARY_DECIMAL_PLACES
#: Widest exactly persistable nano-USD amount (SQLite signed 64-bit INTEGER).
MAX_NANO_USD = 2**63 - 1
#: Working precision for exact conversions; 19 integral digits plus 9 decimal
#: places always fit, so no authoritative amount is silently rounded away.
_MONETARY_PRECISION = 40
#: Bounded, non-secret reason text carried by an invalid ledger snapshot.
LEDGER_REASON_MAX_CHARS = 120


class BillingIntegrityError(Exception):
    """Canonical monetary validation or ledger integrity failed; fail closed.

    The message is a short, non-secret field/rule tag. Raw connector telemetry,
    payloads, prompts or credentials are never carried here.
    """

    code = ErrorCode.BILLING_INTEGRITY_FAILED


def canonical_usd(value: Any, field: str = "amount") -> Decimal:
    """Exact, finite, non-negative USD at the canonical nine-decimal precision.

    Booleans and non-numeric objects are refused outright; NaN, positive and
    negative infinity and negative amounts are refused. Conversion goes through
    ``Decimal(str(value))`` so no binary float artefact reaches the ledger. The
    result is quantized with ``ROUND_CEILING`` - the only rounding direction
    that can never under-count money, and then by at most one nano-USD.
    Anything outside the exactly persistable fixed-width nano-USD range is
    refused rather than truncated.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BillingIntegrityError(f"{field}: not a monetary number")
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BillingIntegrityError(f"{field}: not a monetary number") from exc
    if not candidate.is_finite():
        raise BillingIntegrityError(f"{field}: must be finite")
    if candidate < 0:
        raise BillingIntegrityError(f"{field}: must not be negative")
    with localcontext() as context:
        context.prec = _MONETARY_PRECISION
        try:
            amount = candidate.quantize(_NANO_EXPONENT, rounding=ROUND_CEILING)
            nano = int(amount * NANO_USD_SCALE)
        except (InvalidOperation, OverflowError, ValueError) as exc:
            raise BillingIntegrityError(f"{field}: outside the monetary range") from exc
    if nano > MAX_NANO_USD:
        raise BillingIntegrityError(f"{field}: outside the monetary range")
    return amount


def nano_usd(value: Any, field: str = "amount") -> int:
    """Exact integer nano-USD; the only persisted monetary representation."""
    with localcontext() as context:
        context.prec = _MONETARY_PRECISION
        return int(canonical_usd(value, field) * NANO_USD_SCALE)


def usd_from_nano(nano: Any) -> Decimal:
    """Exact canonical USD for a persisted nano-USD integer."""
    if isinstance(nano, bool) or not isinstance(nano, int):
        raise BillingIntegrityError("nano_usd: not an exact integer amount")
    if nano < 0 or nano > MAX_NANO_USD:
        raise BillingIntegrityError("nano_usd: outside the monetary range")
    return Decimal(nano).scaleb(-MONETARY_DECIMAL_PLACES)


def format_usd(value: Any) -> str:
    """Canonical nine-decimal text form used inside gate/claim identities."""
    return format(canonical_usd(value), "f")


def gate_amount(value: Any) -> str:
    """Gate-identity form of an amount; an invalid one becomes ``invalid``.

    Fail-closed by construction: an ``invalid`` component can never equal a
    valid one, so a malformed amount can never satisfy a gate comparison.
    """
    try:
        return format_usd(value)
    except BillingIntegrityError:
        return "invalid"


def validated_metered_maximum(value: Any) -> Decimal:
    """A metered pre-execution maximum: finite and strictly greater than zero."""
    amount = canonical_usd(value, "maximum_usd")
    if amount <= 0:
        raise BillingIntegrityError("maximum_usd: metered maximum must be > 0")
    return amount


def validated_metered_minimum(value: Any, maximum: Decimal) -> Decimal:
    """A metered minimum: finite, non-negative and no greater than the maximum."""
    amount = canonical_usd(value, "minimum_usd")
    if amount > maximum:
        raise BillingIntegrityError("minimum_usd: must not exceed maximum_usd")
    return amount


def validated_resource_ceiling(value: Any) -> Decimal:
    """A provider-side resource ceiling: finite and strictly positive.

    A containment bound, never monetary spend; validated here so a malformed
    ceiling cannot ride into a gate identity or an external invocation.
    """
    amount = canonical_usd(value, "resource_ceiling_usd")
    if amount <= 0:
        raise BillingIntegrityError("resource_ceiling_usd: must be > 0")
    return amount


def validated_measured_cost(value: Any) -> Decimal:
    """A measured metered actual cost: finite and non-negative; zero is legal."""
    return canonical_usd(value, "monetary_cost_usd")


#: Widest value SQLite can persist in an INTEGER column. A larger Python int
#: would raise ``OverflowError`` at the driver instead of failing as policy.
MAX_SQLITE_INTEGER = 2**63 - 1

#: The fixed monthly monetary hard limit as exact nano-USD. Storage-owned
#: policy: a caller-supplied larger limit is never trusted (H3 P1-C).
MONTHLY_HARD_LIMIT_NANO_USD = 20 * NANO_USD_SCALE

#: Monetary gate actions. Provider-neutral policy identifiers, owned here so
#: the loop that issues the request and the storage boundary that re-verifies
#: the persisted grant compare exactly the same action name.
PAID_API_GATE = "enable_paid_api_for_task"
COST_CAP_GATE = "override_monthly_cost_cap"


def _enum_from_raw(enum_type: Any, value: Any, field: str) -> Any:
    """Fail-closed enum decoding for persisted/raw classification values."""
    if not isinstance(value, str):
        raise BillingIntegrityError(f"{field}: not a classification value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise BillingIntegrityError(f"{field}: unknown classification value") from exc


def _require_enum(enum_type: Any, value: Any, field: str) -> Any:
    """Require an exact enum member at a runtime authority boundary.

    A raw string is deliberately *not* accepted even though these enums inherit
    from ``str``: an externally constructed record carrying
    ``billing_class="metered_monetary"`` would otherwise survive validation and
    then crash on ``.value`` deep inside persistence. Failing here turns that
    into a structured billing-integrity failure instead.
    """
    if type(value) is not enum_type:
        raise BillingIntegrityError(f"{field}: exact {enum_type.__name__} required")
    return value


def require_billing_class(value: Any, field: str = "billing_class") -> BillingClass:
    return _require_enum(BillingClass, value, field)


def require_monetary_state(
    value: Any, field: str = "monetary_state"
) -> MonetaryState:
    return _require_enum(MonetaryState, value, field)


def persistable_count(value: Any) -> int:
    """Legacy-safe non-negative integer count bounded to SQLite's INTEGER range.

    The deferred nullable-telemetry decision stands: a missing, malformed,
    negative, non-integral or non-representable count uses the existing zero
    representation rather than raising or inventing a measurement. No billing
    is ever inferred from it, and no oversized Python int reaches the driver.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    if value < 0 or value > MAX_SQLITE_INTEGER or int(value) != value:
        return 0
    return int(value)


def billing_class_from_raw(value: Any) -> BillingClass:
    return _enum_from_raw(BillingClass, value, "billing_class")


def monetary_state_from_raw(value: Any) -> MonetaryState:
    return _enum_from_raw(MonetaryState, value, "monetary_state")


def ledger_month(moment: datetime | None = None) -> str:
    """The UTC ledger month key. Never derived from connector-supplied time."""
    stamp = moment or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m")


@dataclass(frozen=True, slots=True)
class StructuredError:
    """The one structured diagnostic the product persists.

    H4: construction is the sanitization boundary. ``message`` is bounded and
    credential/path-scrubbed and ``details`` is reduced to bounded allowlisted
    scalars *here*, so no code path - a stage boundary, a connector exception,
    a legacy row reloaded from SQLite - can put raw text into task JSON, a
    result snapshot, a checkpoint or an audit event. A diagnostic is
    informational only and is never execution, billing or lifecycle authority.
    """

    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", sanitize_diagnostic_text(self.message))
        object.__setattr__(self, "details", sanitize_diagnostic_details(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StructuredError | None":
        """Load a persisted diagnostic conservatively.

        A malformed or adversarial row never becomes active state again: an
        unknown code falls back to ``INVALID_TASK``, a non-boolean retryable
        flag falls back to ``False``, and the message/details are re-sanitized
        so unsafe legacy text cannot be written back on the next checkpoint.
        """
        if not isinstance(data, dict):
            return None
        raw_code = data.get("code")
        try:
            code = ErrorCode(raw_code) if isinstance(raw_code, str) else None
        except ValueError:
            code = None
        retryable = data.get("retryable", False)
        return cls(
            code=code if code is not None else ErrorCode.INVALID_TASK,
            message=data.get("message", ""),
            details=data.get("details", {}),
            retryable=retryable if isinstance(retryable, bool) else False,
        )


def diagnostic_from_exception(
    code: ErrorCode,
    exc: BaseException,
    *,
    retryable: bool = False,
    stage: Any = None,
) -> StructuredError:
    """Normalize an exception into the safe persisted diagnostic contract.

    The exception *object* never reaches storage. Its message is sanitized like
    any other free-form diagnostic, and the only structural metadata kept is the
    exception class name and the stage tag - both bounded, non-secret
    identifiers. Tracebacks, environment values, command lines, raw payloads and
    connector objects are never carried.
    """
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 - a pathological __str__ is not a message
        message = ""
    details: dict[str, Any] = {"exception_type": type(exc).__name__}
    if stage is not None:
        details["stage"] = stage.value if isinstance(stage, Enum) else str(stage)
    return StructuredError(code, message, details=details, retryable=retryable)


#: The only keys a persisted result snapshot may carry, and the only free-form
#: ``data`` key the loop itself consumes.
_RESULT_SNAPSHOT_KEYS = frozenset(
    {"outcome", "summary", "data", "error", "usage", "verification"}
)
_RESULT_DATA_KEYS = frozenset({"next_step"})
#: Hard ceiling on persisted verification evidence items.
VERIFICATION_MAX_EVIDENCE = 8
def _canonical_enum_value(value: Any, allowed: frozenset[str]) -> str | None:
    """An exact canonical enum value, or ``None``.

    A key being allowlisted is never enough: an enum-like field must carry one
    of the exact values its enum declares. A raw arbitrary string - including a
    credential-bearing or oversized one - is not a value, it is tampered data,
    and it is dropped so restoration keeps failing closed.
    """
    return value if isinstance(value, str) and value in allowed else None


def _snapshot_count(value: Any) -> int:
    """A persisted count field: real int, not bool, non-negative, bounded.

    Reuses the established H3 zero-normalization policy, so ``True``, ``"7"``,
    a float, a negative or an oversized integer all become the legacy-safe
    zero rather than becoming active ``UsageRecord`` state.
    """
    return persistable_count(value)


def _snapshot_amount(value: Any, *, nullable: bool) -> Any:
    """A persisted monetary field under the canonical H3 numeric semantics."""
    if value is None:
        return None if nullable else 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None if nullable else 0.0
    try:
        return float(canonical_usd(value))
    except BillingIntegrityError:
        return None if nullable else 0.0


def _snapshot_text(value: Any) -> str | None:
    """A persisted free-form usage string: bounded, scrubbed, or dropped."""
    if not isinstance(value, str):
        return None
    return sanitize_diagnostic_text(value, DIAGNOSTIC_DETAIL_MAX_CHARS)


def _sanitize_snapshot_usage(usage: Any) -> dict[str, Any]:
    """Exactly the canonical typed usage fields, each validated by its own type.

    Default-deny at the nested boundary *and* field-aware inside it: a field is
    never kept merely because its key is allowlisted. Enum fields must carry an
    exact canonical value, count fields must be real non-negative bounded ints
    (``True`` and ``"7"`` are not ints), monetary fields must satisfy the
    canonical H3 numeric semantics, and free-form strings are bounded and
    scrubbed. Anything else is dropped and the field falls back to its typed
    default, so a tampered snapshot can never become active state.

    The class/state/amount triple is additionally reconciled: a *measured*
    metered row with no amount is impossible, so its state degrades to the
    fail-closed ``UNKNOWN`` rather than persisting a combination that would
    raise out of a later ledger write.
    """
    if not isinstance(usage, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in sorted(_USAGE_SNAPSHOT_KEYS & set(usage)):
        value = usage[key]
        if key in _USAGE_ENUM_VALUES:
            resolved = _canonical_enum_value(value, _USAGE_ENUM_VALUES[key])
        elif key in _USAGE_COUNT_FIELDS:
            resolved = _snapshot_count(value)
        elif key in _USAGE_NULLABLE_AMOUNT_FIELDS:
            resolved = _snapshot_amount(value, nullable=True)
        elif key in _USAGE_AMOUNT_FIELDS:
            resolved = _snapshot_amount(value, nullable=False)
        else:
            # Free-form and storage-owned identifier strings alike. These are
            # echoes of the authoritative ledger row, never connector authority:
            # H3 owns the usage id, the recorded time and the ledger month.
            resolved = _snapshot_text(value)
        if resolved is None and key not in _USAGE_NULLABLE_AMOUNT_FIELDS:
            continue
        safe[key] = resolved
    if (
        safe.get("billing_class") == BillingClass.METERED_MONETARY.value
        and safe.get("monetary_state") == MonetaryState.MEASURED.value
        and safe.get("monetary_cost_usd") is None
    ):
        # A measured metered row with no amount is not a valid ledger shape and
        # would raise on a later write; degrade to the fail-closed state.
        safe["monetary_state"] = MonetaryState.UNKNOWN.value
    return safe


def _sanitize_snapshot_verification(verification: Any) -> dict[str, Any]:
    """Exactly the canonical typed verification fields, bounded and scrubbed.

    Default-deny: ``status``, ``method`` and ``evidence`` are built explicitly,
    so ``payload``, ``raw``, ``metadata`` and every other connector-authored
    field is dropped rather than carried through. ``status`` must be an exact
    ``VerificationStatus`` value - a raw arbitrary string, however
    credential-bearing or oversized, is never accepted as a status and is
    dropped so restoration fails closed.
    """
    if not isinstance(verification, dict):
        return {}
    evidence = verification.get("evidence")
    return {
        "status": _canonical_enum_value(
            verification.get("status"), _VERIFICATION_STATUS_VALUES
        ),
        "method": sanitize_diagnostic_text(
            verification.get("method", ""), DIAGNOSTIC_DETAIL_MAX_CHARS
        ),
        "evidence": [
            sanitize_diagnostic_text(item, DIAGNOSTIC_DETAIL_MAX_CHARS)
            for item in (evidence if isinstance(evidence, (list, tuple)) else [])
        ][:VERIFICATION_MAX_EVIDENCE],
    }


def sanitize_result_snapshot(snapshot: Any) -> dict[str, Any] | None:
    """Bound and scrub a persisted result snapshot, on write and on load.

    Applied by ``Task.to_dict``/``Task.from_dict`` so the same policy governs
    freshly produced results, legacy rows and tampered rows alike, and so a
    load/resave cycle can never write unsafe text back. Idempotent: sanitizing
    an already-sanitized snapshot is byte-identical.
    """
    if not isinstance(snapshot, dict):
        return None
    safe: dict[str, Any] = {
        key: value for key, value in snapshot.items() if key in _RESULT_SNAPSHOT_KEYS
    }
    if "outcome" in safe:
        # Enum-like in the result contract: an exact canonical value or nothing.
        # A credential-bearing or oversized "outcome" is tampered data, and
        # dropping it makes restoration fail closed deterministically.
        safe["outcome"] = _canonical_enum_value(safe["outcome"], _OUTCOME_VALUES)
    if "summary" in safe:
        safe["summary"] = sanitize_diagnostic_text(safe["summary"])
    raw_data = safe.get("data")
    if isinstance(raw_data, dict):
        # Arbitrary connector ``data`` is never persisted; only the one key the
        # loop actually consumes survives, bounded and credential-safe.
        safe["data"] = {
            key: sanitize_diagnostic_text(raw_data[key])
            for key in sorted(_RESULT_DATA_KEYS & set(raw_data))
            # Only an actual string survives; arbitrary connector data is never
            # stringified into persisted state.
            if isinstance(raw_data[key], str)
        }
    elif "data" in safe:
        safe["data"] = {}
    error = StructuredError.from_dict(safe.get("error"))
    if error is not None:
        safe["error"] = error.to_dict()
    elif "error" in safe:
        safe["error"] = None
    if "usage" in safe:
        safe["usage"] = _sanitize_snapshot_usage(safe["usage"])
    if "verification" in safe:
        safe["verification"] = _sanitize_snapshot_verification(safe["verification"])
    return safe


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
                gate_amount(self.monetary_maximum_usd),
                str(int(self.requires_paid_api)),
                "none" if ceiling is None else gate_amount(ceiling),
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
#: Exact persisted usage fields: the canonical typed contract, nothing else.
_USAGE_SNAPSHOT_KEYS = _USAGE_FIELDS
#: Per-field semantic types inside that contract. A key alone never qualifies a
#: value; each field is validated against the exact type it declares.
_USAGE_ENUM_VALUES = {
    "billing_class": frozenset(item.value for item in BillingClass),
    "monetary_state": frozenset(item.value for item in MonetaryState),
}
_USAGE_COUNT_FIELDS = frozenset({"input_tokens", "output_tokens", "call_count"})
_USAGE_NULLABLE_AMOUNT_FIELDS = frozenset(
    {"monetary_cost_usd", "reference_cost_usd"}
)
_USAGE_AMOUNT_FIELDS = frozenset({"estimated_cost_usd"})
#: Exact canonical values for the enum-like result-snapshot fields.
_OUTCOME_VALUES = frozenset(item.value for item in OutcomeStatus)
_VERIFICATION_STATUS_VALUES = frozenset(item.value for item in VerificationStatus)


def validate_cost_estimate(estimate: CostEstimate) -> None:
    """Fail closed on a pre-execution estimate that cannot be reasoned about.

    This is the *pre-execution* half of the monetary contract: it validates the
    projected route and bounds a connector declares before anything runs. Its
    authoritative counterpart for already-recorded history is
    ``jarvis.memory.validate_ledger_usage``, which is deliberately stricter
    because a persisted ledger row has no remaining uncertainty to express.

    The billing class must be an exact ``BillingClass`` member: a raw string, a
    number, a foreign enum or a duck-typed ``.value`` stand-in is refused here,
    at the earliest provider-neutral boundary, rather than crashing later on
    enum-specific access. A metered route must project a finite maximum strictly
    greater than zero with a finite, non-negative minimum no greater than it, so
    a NaN, infinite, negative or zero/unknown maximum can never slip past a
    meaningful preflight. A supplied resource ceiling must be finite and
    strictly positive; it stays a containment bound and never enters the
    monetary projection.
    """
    if not isinstance(estimate, CostEstimate):
        raise BillingIntegrityError("cost estimate: not a CostEstimate")
    require_billing_class(estimate.billing_class)
    if estimate.resource_ceiling_usd is not None:
        validated_resource_ceiling(estimate.resource_ceiling_usd)
    if estimate.billing_class is BillingClass.METERED_MONETARY:
        maximum = validated_metered_maximum(estimate.maximum_usd)
        validated_metered_minimum(estimate.minimum_usd, maximum)
        return
    # A non-metered route projects no money, but a malformed amount still must
    # not survive into a gate identity or a persisted estimate.
    canonical_usd(estimate.maximum_usd, "maximum_usd")
    canonical_usd(estimate.minimum_usd, "minimum_usd")


# --------------------------------------------------------------------------
# H3 - typed, provider/storage-independent ledger state
# --------------------------------------------------------------------------


class LedgerValidity(str, Enum):
    """Whether the persisted monetary state could be read as authoritative."""

    VALID = "valid"
    INVALID = "invalid"


class ReservationState(str, Enum):
    """Durable state of one attempt-bound monetary reservation.

    ACTIVE: money is committed to a claimed execution and not yet reconciled.
    RECONCILED: authoritative measured usage replaced the reservation.
    UNRESOLVED: the execution returned something the ledger could not accept;
    the exposure stays until explicit future recovery work.
    """

    ACTIVE = "active"
    RECONCILED = "reconciled"
    UNRESOLVED = "unresolved"


#: Reservation states that still contribute monetary exposure.
OPEN_RESERVATION_STATES = frozenset(
    {ReservationState.ACTIVE, ReservationState.UNRESOLVED}
)


def reservation_state_from_raw(value: Any) -> ReservationState:
    return _enum_from_raw(ReservationState, value, "reservation_state")


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """The authoritative monetary position of one UTC ledger month.

    Provider- and storage-independent. Every amount is exact integer nano-USD;
    no binary float arithmetic participates in an enforcement decision. An
    invalid snapshot carries a bounded, non-secret reason and must never be
    used to authorize paid execution - a malformed persisted row can neither
    subtract from spend nor be silently ignored.
    """

    month: str
    measured_nano_usd: int = 0
    reserved_nano_usd: int = 0
    unresolved_nano_usd: int = 0
    validity: LedgerValidity = LedgerValidity.VALID
    reason: str = ""

    @classmethod
    def invalid(cls, month: str, reason: str) -> LedgerSnapshot:
        return cls(
            month=month,
            validity=LedgerValidity.INVALID,
            reason=str(reason)[:LEDGER_REASON_MAX_CHARS],
        )

    @property
    def valid(self) -> bool:
        return self.validity is LedgerValidity.VALID

    @property
    def total_nano_usd(self) -> int:
        """Measured spend plus every still-open reservation, in nano-USD."""
        return self.measured_nano_usd + self.reserved_nano_usd

    @property
    def measured_usd(self) -> Decimal:
        return usd_from_nano(self.measured_nano_usd)

    @property
    def reserved_usd(self) -> Decimal:
        return usd_from_nano(self.reserved_nano_usd)

    @property
    def total_usd(self) -> Decimal:
        return usd_from_nano(self.total_nano_usd)

    @property
    def has_unresolved_liability(self) -> bool:
        """Whether money is committed but not yet reconciled to measured spend."""
        return self.reserved_nano_usd > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "measured_usd": format_usd(self.measured_usd),
            "reserved_usd": format_usd(self.reserved_usd),
            "total_usd": format_usd(self.total_usd),
            "has_unresolved_liability": self.has_unresolved_liability,
            "validity": self.validity.value,
            "reason": self.reason,
        }


class ExecutionClaimOutcome(str, Enum):
    """Typed result of the atomic execution claim (attempt + cap + reservation).

    CLAIMED: this owner holds the attempt, every one-shot grant was consumed
    and the reservation is durable. LOST: another owner, an authoritative
    cancellation or a missing grant won; nothing was written.
    BILLING_RECHECK_REQUIRED: the ledger moved after preflight/approval, so the
    approved projection no longer describes reality; nothing was written and the
    task must return to COST_PREFLIGHT. LEDGER_INVALID: persisted monetary state
    could not be read authoritatively; refuse to execute.
    """

    CLAIMED = "claimed"
    LOST = "lost"
    BILLING_RECHECK_REQUIRED = "billing_recheck_required"
    LEDGER_INVALID = "ledger_invalid"


@dataclass(frozen=True, slots=True)
class BillingClaim:
    """The exact monetary state an execution claim is authorized against.

    Immutable and fully explicit: storage re-derives the ledger inside the claim
    transaction and refuses the claim unless the month and the total exposure
    match this snapshot exactly, so two concurrent metered tasks can never both
    clear preflight against the same balance.
    """

    ledger_month: str
    expected_exposure_nano_usd: int
    reservation_nano_usd: int
    limit_nano_usd: int
    billing_class: BillingClass
    gate_state: str
    #: The exact approved cost-override grant fingerprint, when the projection
    #: crosses the hard limit. Empty when no override is required.
    cost_override_fingerprint: str = ""


class ReconciliationOutcome(str, Enum):
    """Result of binding one connector result to its attempt reservation."""

    RECONCILED = "reconciled"
    #: The same attempt replayed the exact same normalized record.
    IDEMPOTENT = "idempotent"
    #: A conflicting duplicate, a route/state mismatch or malformed money.
    CONFLICT = "conflict"
    #: No reservation exists for this attempt.
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class BillingReconciliation:
    """Authoritative post-execution usage bound to one execution attempt.

    Identity (usage id), recorded time, ledger month and call count are
    storage-owned and deliberately absent or storage-written here: a connector
    may never supply ledger authority. ``billing_class`` is the execution-time
    route bound into the claim, ``connector`` is the selected connector
    manifest name and ``billing_evidence`` is the execution-time estimate's
    evidence - none of the three is ever the class, name or evidence string the
    connector reported in its own telemetry.
    """

    attempt_id: str
    session_id: str
    task_id: str
    connector: str
    billing_class: BillingClass
    monetary_state: MonetaryState
    #: Exact measured nano-USD for a metered route; ``None`` otherwise.
    actual_nano_usd: int | None = None
    #: Provider-reported USD for unbilled usage. Telemetry, never money.
    reference_cost_usd: float | None = None
    #: ``call_count`` is deliberately absent: one claimed execution is exactly
    #: one external call, so storage writes 1 rather than trusting telemetry.
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    billing_evidence: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    outcome: ReconciliationOutcome
    #: The authoritative persisted record, for the first insert and for a
    #: byte-identical replay. ``None`` for a conflict or a missing reservation.
    usage: UsageRecord | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (
            ReconciliationOutcome.RECONCILED,
            ReconciliationOutcome.IDEMPOTENT,
        )


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


# --------------------------------------------------------------------------
# Project identity and project-scoped memory (M1)
# --------------------------------------------------------------------------

#: Strict project identity: a lowercase slug, never a filesystem path, URL or
#: free text, so project ids can never collide with the H4 path-redaction
#: contract or smuggle location details into persisted rows.
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
PROJECT_NAME_CAP = 120
#: Hard ceiling for one stored memory text. Oversized text is REJECTED at the
#: write boundary, never silently truncated: memory content is semantic user
#: data, not diagnostics.
PROJECT_MEMORY_TEXT_CAP = 480
#: Newest active project items examined per retrieval, before scoring. An
#: internal safety window like ``RETRIEVAL_CANDIDATE_WINDOW``, not a result
#: limit; the absolute retrieval ceilings above stay unchanged.
PROJECT_MEMORY_CANDIDATE_WINDOW = 25


def validate_project_id(value: Any) -> str:
    """Validate one project id; unknown shapes fail closed with ValueError."""
    if not isinstance(value, str) or not PROJECT_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "project_id must be a 3-64 character lowercase slug "
            "([a-z0-9][a-z0-9-]{2,63})"
        )
    return value


class ProjectMemoryKind(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """One explicit local project identity. Created only by the user through
    the CLI; nothing auto-creates a project."""

    id: str
    name: str
    active: bool = True
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_project_id(self.id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("project name must be a non-blank string")
        if len(self.name) > PROJECT_NAME_CAP:
            raise ValueError(
                f"project name is longer than {PROJECT_NAME_CAP} characters"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "active": self.active,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRecord":
        return cls(
            id=data["id"],
            name=data["name"],
            active=bool(data.get("active", True)),
            created_at=str(data.get("created_at", "")),
        )


@dataclass(frozen=True, slots=True)
class ProjectMemoryItem:
    """One explicit, user-written project memory entry (fact or preference).

    Untrusted reference data with NO execution authority: it can never
    satisfy, create or influence approvals, grants, billing or lifecycle
    state, and its text is data, not instructions. The classification is
    mandatory at write time and is never downgraded automatically; a
    malformed persisted classification loads closed as SECRET through
    ``from_dict``, keeping the item permanently ineligible for egress.
    """

    project_id: str
    kind: ProjectMemoryKind
    text: str
    classification: DataClassification
    id: str = field(default_factory=lambda: str(uuid4()))
    active: bool = True
    superseded_by: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if type(self.kind) is not ProjectMemoryKind:
            raise ValueError("kind must be a ProjectMemoryKind")
        if not isinstance(self.classification, DataClassification):
            raise ValueError("classification must be a DataClassification")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("memory text must be a non-blank string")
        if len(self.text) > PROJECT_MEMORY_TEXT_CAP:
            raise ValueError(
                f"memory text is longer than {PROJECT_MEMORY_TEXT_CAP} characters"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "text": self.text,
            "classification": self.classification.value,
            "active": self.active,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectMemoryItem":
        # Kind is strict: an unknown kind raises and the caller skips the row.
        kind = ProjectMemoryKind(data["kind"])
        # Classification fails closed: an unreadable value becomes SECRET and
        # the item stays retrievable-but-never-egressable, not permissive.
        try:
            classification = DataClassification(data.get("classification"))
        except ValueError:
            classification = DataClassification.SECRET
        return cls(
            project_id=data["project_id"],
            kind=kind,
            text=data["text"],
            classification=classification,
            id=str(data.get("id", "")) or str(uuid4()),
            active=bool(data.get("active", True)),
            superseded_by=(
                data.get("superseded_by")
                if isinstance(data.get("superseded_by"), str)
                else None
            ),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


class RetrievedMemoryKind(str, Enum):
    DECISION = "decision"
    FACT = "fact"
    PREFERENCE = "preference"


@dataclass(frozen=True, slots=True)
class MemoryRetrievalQuery:
    session_id: str
    query_text: str
    exclude_task_id: str | None
    max_items: int = RETRIEVAL_MAX_ITEMS
    max_characters: int = RETRIEVAL_MAX_CHARACTERS
    #: Optional project scope (M1). ``None`` keeps retrieval byte-identical
    #: to the project-less behaviour: same-session decisions only.
    project_id: str | None = None


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
    project_candidates: Sequence[ProjectMemoryItem] = (),
) -> RetrievedContext:
    """Score, rank, filter, select and truncate one bounded candidate window.

    The order is mandatory: relevance is computed on full untruncated content,
    zero-score records are dropped, ranking happens before the classification
    filter so an ineligible record never consumes a result slot, and only the
    finally selected records are normalized and truncated.

    ``RETRIEVAL_MAX_ITEMS`` and ``RETRIEVAL_MAX_CHARACTERS`` are absolute
    ceilings: a query may narrow them but can never expand them. Project
    memory items (M1) compete for the SAME slots and characters under the
    same tokenizer, ordering and eligibility ladder — they never extend the
    quota, and with ``project_candidates`` empty the result is byte-identical
    to the project-less behaviour.
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
    for item in project_candidates:
        # A project item scores on its full stored text; its classification is
        # the item's own explicit stored classification.
        score = len(query_tokens & retrieval_tokens(item.text))
        if score > 0:
            scored.append((item, item.classification, score))
    # Stable triple sort: relevance desc, created_at desc, record id asc.
    # One merged ordering across kinds; ids are unique so ties resolve
    # deterministically.
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
        if isinstance(record, ProjectMemoryItem):
            summary, summary_truncated = truncate_retrieved_text(
                normalize_retrieved_text(record.text), RETRIEVAL_SUMMARY_CAP
            )
            rationale = ""
            rationale_truncated = False
            next_step: str | None = None
            next_step_truncated = False
            next_step_length = 0
            kind = RetrievedMemoryKind(record.kind.value)
            source_id = record.project_id
        else:
            summary, summary_truncated = truncate_retrieved_text(
                normalize_retrieved_text(record.summary), RETRIEVAL_SUMMARY_CAP
            )
            rationale, rationale_truncated = truncate_retrieved_text(
                normalize_retrieved_text(record.rationale), RETRIEVAL_RATIONALE_CAP
            )
            if record.next_step is None:
                next_step = None
                next_step_truncated = False
                next_step_length = 0
            else:
                next_step, next_step_truncated = truncate_retrieved_text(
                    normalize_retrieved_text(record.next_step),
                    RETRIEVAL_NEXT_STEP_CAP,
                )
                next_step_length = len(next_step)
            kind = RetrievedMemoryKind.DECISION
            source_id = record.task_id
        contribution = len(summary) + len(rationale) + next_step_length
        if character_count + contribution > effective_max_characters:
            overflow = True
            break
        selected.append(
            RetrievedMemoryItem(
                kind=kind,
                record_id=record.id,
                source_task_id=source_id,
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
        candidate_count=len(candidates) + len(project_candidates),
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
#: Command-line credential syntax: ``--api-key VALUE``, ``--token=VALUE`` and
#: friends. The flag prefix is required, so ordinary prose mentioning a token or
#: a secret - and file names such as ``tokenizer.py`` - never matches.
_CLI_CREDENTIAL_FLAG = (
    r"api[_\-]?key|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token|token"
    r"|password|passwd|client[_\-]?secret|secret|authorization|bearer"
    r"|private[_\-]?key|session[_\-]?cookie|cookie|credential"
)
#: The flag and its ``=``/whitespace joiner. The value is NOT matched here: a
#: character class such as ``[^"]*`` treats an escaped quote as the closing one
#: and hands back the rest of the secret (``--password "HUNTER2 foo\"bar TAIL"``
#: leaking ``bar TAIL``). The lookahead only asserts that a value exists, so
#: ``--token`` with nothing after it is still not a credential assignment.
_CLI_CREDENTIAL_PATTERN = re.compile(
    rf"(--?(?:{_CLI_CREDENTIAL_FLAG}))(=|\s+)(?=\S)", re.IGNORECASE
)


def _credential_value_end(text: str, start: int) -> int:
    """End index of the one credential value beginning at ``start``.

    A deterministic single-pass scanner, because quoting has state a regex
    cannot carry:

    * unquoted - one whitespace-delimited token, so an ordinary sentence is
      never swallowed;
    * quoted - everything up to the first **unescaped** matching quote. A
      backslash escapes the next character, so an escaped quote stays *inside*
      the secret instead of ending it;
    * quoted but never closed - the rest of the bounded diagnostic field. Once
      a credential flag opens a quote, everything after it is secret; falling
      back to one token would publish the tail.

    Escapes are honoured in single quotes too. A POSIX shell would not, but
    this reads diagnostics from any source, and treating ``\\'`` as an escape
    only ever consumes *more*, which is the safe direction.
    """
    length = len(text)
    if start >= length:
        return start
    quote = text[start]
    if quote not in "\"'":
        index = start
        while index < length and not text[index].isspace():
            index += 1
        return index
    index = start + 1
    while index < length:
        char = text[index]
        if char == "\\":
            # The escaped character belongs to the secret, whatever it is.
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return length


def _redact_cli_credentials(text: str, replacement: str) -> tuple[str, int]:
    """Replace every command-line credential value, keeping its flag.

    Scans left to right and resumes *after* each consumed value, so a flag
    mentioned inside a quoted secret is never treated as a fresh assignment.
    """
    parts: list[str] = []
    count = 0
    index = 0
    while True:
        match = _CLI_CREDENTIAL_PATTERN.search(text, index)
        if match is None:
            break
        end = _credential_value_end(text, match.end())
        parts.append(text[index : match.start()])
        parts.append(f"{match.group(1)}{match.group(2)}{replacement}")
        count += 1
        index = end
    parts.append(text[index:])
    return "".join(parts), count

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
    """Whether text carries a labelled, command-line or raw-shape credential."""
    if _CREDENTIAL_ASSIGNMENT_PATTERN.search(text):
        return True
    if _CLI_CREDENTIAL_PATTERN.search(text):
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
    # Command-line syntax runs before the label rule so ``--token VALUE`` (no
    # ``:``/``=`` delimiter) is caught, and so the flag itself survives.
    text, replaced = _redact_cli_credentials(text, replacement)
    count += replaced
    text, replaced = _CREDENTIAL_REDACTION_PATTERN.subn(
        lambda match: f"{match.group(1)}{match.group(2)}{replacement}", text
    )
    return text, count + replaced


# --------------------------------------------------------------------------
# H4 - persisted diagnostic hygiene
# --------------------------------------------------------------------------
#
# Presentation redaction is not enough: sensitive or unbounded diagnostic text
# must never become authoritative persisted state in the first place. Every
# free-form string that can reach task JSON, a result snapshot, a checkpoint or
# an audit event passes through ``sanitize_diagnostic_text`` first. The policy
# is provider-independent, deterministic and byte-stable across restarts.

#: The single explicit bound for any persisted free-form diagnostic string.
#: Matches the existing progress-text ceiling so one number governs every
#: bounded human-readable field the product persists.
DIAGNOSTIC_MESSAGE_MAX_CHARS = 240
#: Tighter bound for the scalar values carried in structured error details.
DIAGNOSTIC_DETAIL_MAX_CHARS = 120
#: Hard ceilings on structured error details, so a diagnostic can never grow
#: into an arbitrary nested blob.
DIAGNOSTIC_MAX_DETAILS = 8
DIAGNOSTIC_DETAIL_KEY_MAX_CHARS = 48

#: How much text the redaction passes examine, as a multiple of the final
#: bound. Anything beyond is discarded before it could be persisted.
DIAGNOSTIC_WORKING_WINDOW_FACTOR = 8

#: Widest integer a diagnostic detail may carry. Signed 64-bit keeps every
#: persisted value inside SQLite's INTEGER range and well inside Python's JSON
#: integer-digit limit, so an arbitrary huge int can never reach persistence.
DIAGNOSTIC_INT_MIN = -(2**63)
DIAGNOSTIC_INT_MAX = 2**63 - 1

#: Detail keys that are unsafe to persist whatever their value: a traceback, a
#: command line, an environment dump, a raw prompt/payload or a captured
#: stream is a blob by construction, and no value-level scrub makes it a tag.
#: Matched case-insensitively, including suffixed forms such as
#: ``connector_traceback``.
FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "argv",
        "body",
        "cmd",
        "command",
        "commandline",
        "cookies",
        "env",
        "environ",
        "environment",
        "headers",
        "payload",
        "prompt",
        "request",
        "response",
        "stack",
        "stacktrace",
        "stderr",
        "stdout",
        "traceback",
    }
)

#: Replacement for a redacted local filesystem location.
REDACTED_PATH = "[PATH]"
#: Replacement for a diagnostic value that is not usable text at all.
UNAVAILABLE_DIAGNOSTIC = "[UNAVAILABLE]"

#: Filesystem shapes that carry machine-local detail (user names, temp
#: directories, interpreter locations, repository roots). URLs are deliberately
#: NOT matched: an outbound endpoint is an attested, typed value elsewhere, and
#: destroying it would hide the very thing a diagnostic is for.
#:
#: This is a deterministic single-pass scanner rather than a regex, because a
#: regex needs bounded repetition to stay safe and those bounds silently became
#: the *visible* end of a path: a per-segment word ceiling left the tenth word
#: of a directory name next to the marker, and a segment-count ceiling left the
#: tail of a deep path (``[PATH]seg17/seg18/TAIL``). A safety bound must bound
#: the work, never define where a recognized path stops. The scanner has no
#: such ceiling: once a root is recognized, it consumes to a character a path
#: cannot cross, or to the end of the bounded diagnostic field.
#:
#: Characters no local path can contain, so reaching one PROVES the path ended.
#: A plain space is deliberately absent: real directories contain spaces
#: ("Program Files", "My Documents"), so a space is ambiguous and the scanner
#: keeps consuming - over-redacting the rest of the field rather than exposing
#: a suffix. Other whitespace ends it, since the field itself has ended.
_PATH_STOP_CHARS = frozenset("\"'<>|")


def _ascii_alnum(char: str) -> bool:
    """Exactly ``[A-Za-z0-9]``; the empty string (start of text) is not."""
    return char.isascii() and char.isalnum()


def _path_root(text: str, index: int) -> int:
    """Length of the absolute-path root starting at ``index``, else ``0``.

    Recognizes UNC (``\\\\server``), Windows drive (``C:\\``, ``C:/``) and
    POSIX (``/``) roots. The preceding character matters: a separator glued to
    a word or to a URL scheme is not a root, which is what keeps
    ``read/write``, ``and/or`` and ``https://host/path`` intact.
    """
    char = text[index]
    previous = text[index - 1] if index else ""
    if char == "\\":
        return 2 if text.startswith("\\\\", index) else 0
    if char == "/":
        return 0 if (_ascii_alnum(previous) or previous in (":", "/")) else 1
    if (
        char.isascii()
        and char.isalpha()
        and text[index + 1 : index + 2] == ":"
        and text[index + 2 : index + 3] in ("\\", "/")
        and not _ascii_alnum(previous)
    ):
        return 3
    return 0


def _path_end(text: str, index: int) -> int:
    """First index at or after ``index`` that a local path cannot cross."""
    length = len(text)
    while index < length:
        char = text[index]
        if char in _PATH_STOP_CHARS or (char.isspace() and char != " "):
            break
        index += 1
    return index


def redact_local_paths(text: str) -> str:
    """Replace absolute filesystem locations with a bounded safe marker.

    Applies to *accidental diagnostic leakage* only. A typed resource contract
    - ``ActionScope.target``, an attestation's working directory or allowed
    roots - is authoritative state under H1 and never routed through here.

    No basename or terminal segment is ever preserved: free diagnostic text
    cannot prove that ``/home/john.doe`` names a file rather than a user, so
    every recognized absolute path collapses to the fixed marker.

    Linear in the length of the text. Each character is examined a constant
    number of times: a recognized path advances the cursor past its whole
    extent, and a rejected single-separator candidate advances past the extent
    too - which is sound precisely because that extent was rejected for
    containing no further separator, so no root can begin inside it either.
    """
    parts: list[str] = []
    index = 0
    emitted = 0
    length = len(text)
    while index < length:
        root = _path_root(text, index)
        if root:
            body = index + root
            start = index
            # A path written as an argument keeps its quotes; consuming through
            # the matching quote leaves no dangling fragment behind.
            quote = (
                text[index - 1]
                if index > emitted and text[index - 1] in "\"'"
                else ""
            )
            if quote:
                closing = text.find(quote, body)
                end = length if closing < 0 else closing + 1
                start = index - 1
            else:
                end = _path_end(text, body)
            # A lone separator is not an absolute path in free prose, so a
            # POSIX root must show a second one ("/tmp" is a word, "/tmp/x"
            # is a location). A drive or UNC root is already unambiguous.
            if root != 1 or any(char in "\\/" for char in text[body:end]):
                parts.append(text[emitted:start])
                parts.append(REDACTED_PATH)
                index = emitted = end
                continue
            index = end
            continue
        index += 1
    parts.append(text[emitted:])
    return "".join(parts)


def sanitize_diagnostic_text(
    value: Any, cap: int = DIAGNOSTIC_MESSAGE_MAX_CHARS
) -> str:
    """The one canonical pipeline for persisted free-form diagnostic text.

    Deterministic and ordered on purpose:

    1. only an actual ``str`` survives; arbitrary objects are never stringified
       into persisted state, they become ``UNAVAILABLE_DIAGNOSTIC``;
    2. NFKC, then CRLF/CR to LF, then whitespace-run collapse, so a pathological
       or multi-line message cannot smuggle structure into a stored field;
    3. credential redaction (the same canonical policy every egress boundary
       uses), before truncation, so a secret beyond the cap is still removed;
    4. local filesystem redaction;
    5. a hard character bound with a final U+2026 on overflow.

    Running it twice is a no-op, so a value that survived an earlier load is
    byte-identical after a resave.
    """
    if not isinstance(value, str):
        return UNAVAILABLE_DIAGNOSTIC
    bound = max(1, int(cap))
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(text.split())
    if not text:
        return ""
    # Work on a bounded window: everything past it is discarded rather than
    # persisted anyway, and a fixed window keeps the redaction passes linear
    # on adversarially large input.
    text = text[: bound * DIAGNOSTIC_WORKING_WINDOW_FACTOR]
    text, _ = redact_credentials(text)
    text = redact_local_paths(text)
    if len(text) > bound:
        text = text[: bound - 1] + "…"
    return text


def _detail_key_parts(key: str) -> set[str]:
    """The identifier words in a detail key, for forbidden-key matching."""
    lowered = key.casefold()
    return {lowered, *re.split(r"[^a-z0-9]+", lowered)}


#: Sentinel meaning "this detail value cannot be persisted safely".
_DROP_DETAIL = object()


def _safe_detail_value(value: Any) -> Any:
    """One allowlisted, persistence-safe scalar, or ``_DROP_DETAIL``.

    Deliberately narrow: a diagnostic detail is a tag, never a blob. Nested
    collections and objects are dropped rather than stringified, an integer
    outside the signed 64-bit range is dropped rather than persisted (an
    arbitrary huge Python int would otherwise hit the JSON integer-digit limit
    or overflow the SQLite column), and a non-finite float is dropped rather
    than turned into a fabricated value.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if DIAGNOSTIC_INT_MIN <= value <= DIAGNOSTIC_INT_MAX else _DROP_DETAIL
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP_DETAIL
    if isinstance(value, str):
        return sanitize_diagnostic_text(value, DIAGNOSTIC_DETAIL_MAX_CHARS)
    return _DROP_DETAIL


def sanitize_diagnostic_details(details: Any) -> dict[str, Any]:
    """Bounded, allowlisted scalar metadata for a structured error.

    Total by construction: no input raises. Only a flat mapping of *actual
    string* keys to allowlisted, persistence-safe scalars survives. A non-string
    key is dropped fail-closed rather than coerced - the mapping is iterated by
    item, so nothing ever assumes a key is a string or looks one up again.
    Nested dicts and sequences, tracebacks, environment values, raw payloads and
    connector objects are dropped rather than stringified. Keys are sorted and
    the count is capped, so the persisted form is deterministic and bounded.
    """
    if not isinstance(details, dict) or not details:
        return {}
    try:
        items = sorted(
            (key, value) for key, value in details.items() if isinstance(key, str)
        )
    except TypeError:  # pragma: no cover - unorderable values keep insertion order
        items = [(key, value) for key, value in details.items() if isinstance(key, str)]
    safe: dict[str, Any] = {}
    for raw_key, raw_value in items:
        if len(safe) >= DIAGNOSTIC_MAX_DETAILS:
            break
        key = sanitize_diagnostic_text(raw_key, DIAGNOSTIC_DETAIL_KEY_MAX_CHARS)
        if not key or key == UNAVAILABLE_DIAGNOSTIC:
            continue
        if any(part in FORBIDDEN_DETAIL_KEYS for part in _detail_key_parts(key)):
            # Structurally unsafe regardless of value; never persisted.
            continue
        value = _safe_detail_value(raw_value)
        if value is _DROP_DETAIL:
            continue
        safe[key] = value
    return safe


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
    #: Optional project reference (M1), validated at the input boundary.
    #: Binding-only: it selects an EXISTING project for the session and is
    #: never authority; an unknown project fails the task closed.
    project_id: str | None = None
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
            "project_id": self.project_id,
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
            "result_snapshot": sanitize_result_snapshot(self.result_snapshot),
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
            # A malformed persisted project reference fails closed to None;
            # legacy task JSON without the field loads as project-less.
            project_id=(
                data.get("project_id")
                if isinstance(data.get("project_id"), str)
                and PROJECT_ID_PATTERN.fullmatch(data.get("project_id"))
                else None
            ),
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
            # H4: unsafe legacy diagnostic text is scrubbed on load, so it can
            # never be written back on the next checkpoint.
            result_snapshot=sanitize_result_snapshot(data.get("result_snapshot")),
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
    #: Project binding (M1). Set once, at the first project-carrying task of
    #: the session, only to an existing active project. Scopes retrieval
    #: only; it is never approval, billing or lifecycle authority.
    project_id: str | None = None
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
            "project_id",
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
        # A malformed persisted project binding fails closed to no project:
        # it must never become a retrieval scope key. Legacy JSON loads None.
        raw_project = payload.get("project_id")
        payload["project_id"] = (
            raw_project
            if isinstance(raw_project, str)
            and PROJECT_ID_PATTERN.fullmatch(raw_project)
            else None
        )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LoopCheckpoint:
    task_id: str
    session_id: str
    stage: LoopStage
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


class AttemptState(str, Enum):
    """Durable lifecycle of one local execution attempt (H2).

    PREPARED: bound and gate-validated, the connector boundary has NOT been
    crossed. Safe to reclaim/retry after restart. IN_FLIGHT: the connector
    boundary was (about to be) crossed; the external outcome is unknown until
    the owner concludes it - never replayed after restart. FINISHED: the
    attempt concluded with a known outcome (success or clean failure); never
    executed again. UNKNOWN_OUTCOME: the owner could not establish the outcome
    (an egress call that raised, a timeout); never automatically executed
    again, needs explicit deferred recovery.
    """

    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    FINISHED = "finished"
    UNKNOWN_OUTCOME = "unknown_outcome"


#: Attempt states that must never trigger an automatic re-execution.
ATTEMPT_NON_REPLAYABLE = frozenset(
    {AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME, AttemptState.FINISHED}
)


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """One durable local execution attempt bound to an approved opportunity."""

    session_id: str
    task_id: str
    connector: str
    #: The approved permission/attestation identity this attempt runs under.
    fingerprint: str
    state: AttemptState = AttemptState.PREPARED
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionAttempt":
        return cls(
            session_id=data["session_id"],
            task_id=data["task_id"],
            connector=data["connector"],
            fingerprint=data["fingerprint"],
            state=AttemptState(data.get("state", AttemptState.PREPARED.value)),
            id=data["id"],
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )


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
    ) -> bool:
        """Persist one lifecycle checkpoint; True when this writer won.

        The task write and its dependent checkpoint/event/pending-approval
        writes are one consistent operation. False means a terminal writer
        already holds authority: nothing was written and the caller must
        reload authoritative state instead of reporting its own stale view.
        """
        ...

    def list_decisions(self, session_id: str) -> list[DecisionRecord]: ...

    def retrieve_context(self, query: MemoryRetrievalQuery) -> RetrievedContext: ...

    def save_project(self, project: ProjectRecord) -> None: ...

    def load_project(self, project_id: str) -> ProjectRecord | None: ...

    def list_projects(self) -> list[ProjectRecord]: ...

    def save_memory_item(
        self, item: ProjectMemoryItem, *, supersedes: str | None = None
    ) -> ProjectMemoryItem:
        """Persist one explicit memory item (INSERT-only).

        The write boundary applies canonical credential/path hygiene and the
        text ceiling, verifies the target project exists and is active, and —
        when ``supersedes`` names an active item of the SAME project — marks
        that item inactive/superseded in the same transaction. Row content is
        never rewritten; there is no destructive update or delete."""
        ...

    def list_memory_items(
        self, project_id: str, *, include_inactive: bool = False
    ) -> list[ProjectMemoryItem]: ...

    def save_pending_approval(self, request: PermissionRequest) -> None: ...

    def load_pending_approval(
        self, task_id: str, session_id: str
    ) -> PermissionRequest | None: ...

    def resolve_approval(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        status: str,
    ) -> bool:
        """Atomically resolve a still-pending request; True only for the single
        winner. A losing contender creates no grant and must reload state."""
        ...

    def reject_task(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        session: SessionState,
        task: Task,
        payload: dict[str, Any],
    ) -> bool:
        """Atomically resolve a pending request REJECTED and finalize the task
        as terminal rejected in one transaction, so no observable state has a
        rejected request with a still-resumable task. False if already resolved
        or terminal (no writes)."""
        ...

    def cancel_task(
        self,
        session: SessionState,
        task: Task,
        *,
        connector_cancelled: bool,
    ) -> str:
        """Atomically arbitrate cancellation against the execution attempt in
        one transaction. Returns ``"terminal"`` (already immutable, no write),
        ``"cancelled"`` (finalized), or ``"cancelling"`` (IN_FLIGHT: intent
        recorded, not final)."""
        ...

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

    # -- H2: durable execution-attempt journal and atomic ownership --------

    def latest_execution_attempt(self, task_id: str) -> "ExecutionAttempt | None":
        """The most recent execution attempt for a task, or None."""
        ...

    def open_execution_attempt(
        self, attempt: "ExecutionAttempt"
    ) -> "ExecutionAttempt":
        """Atomically create-or-observe the active attempt for the task.

        Inserts ``attempt`` (PREPARED) only when the task has no attempt yet;
        otherwise returns the existing latest attempt unchanged. The caller
        proceeds only when the returned attempt is still PREPARED.
        """
        ...

    def begin_execution(
        self,
        attempt_id: str,
        session_id: str,
        task_id: str,
        one_shot_fingerprints: Sequence[str],
        billing: BillingClaim,
    ) -> ExecutionClaimOutcome:
        """Atomically claim ownership in ONE transaction: verify authoritative
        task lifecycle (reject if cancelled/terminal), rebuild the authoritative
        ledger snapshot, verify month/exposure against ``billing``, recompute
        the projection, require the exact approved cost-override grant when it
        crosses the hard limit, transition PREPARED -> IN_FLIGHT, consume every
        required one-shot grant and create the attempt-bound reservation - all
        or nothing.

        Exactly one concurrent contender gets ``CLAIMED``; every other outcome
        wrote nothing and must not cross the connector boundary. Durable before
        ``connector.execute`` is invoked.

        ``billing`` is mandatory. There is no implicit billing route and no
        synthetic fallback claim: every execution claim states its route,
        month, expected exposure and reservation explicitly, including
        zero-monetary routes. A missing or invalid claim fails closed before
        the attempt is claimed, before any grant is consumed, before any
        reservation exists and before the connector boundary is crossed.
        """
        ...

    def mark_reservation_unresolved(self, attempt_id: str) -> None:
        """Move an ACTIVE reservation to UNRESOLVED; exposure is retained."""
        ...

    def reconcile_usage(
        self, reconciliation: BillingReconciliation
    ) -> ReconciliationResult:
        """Atomically insert the authoritative usage row for one attempt and
        reconcile its reservation, so measured spend replaces reserved exposure
        without double counting.

        Storage owns the usage id, the recorded timestamp and the ledger month.
        A first record is inserted immutably; an identical replay of the same
        attempt is idempotent; a conflicting duplicate, a route/state mismatch
        or malformed monetary data fails closed and leaves the reservation open.
        """
        ...

    def mark_execution_unknown(self, attempt_id: str) -> None:
        """Owner-only transition IN_FLIGHT -> UNKNOWN_OUTCOME (outcome in doubt)."""
        ...

    def conclude_execution(
        self,
        session: SessionState,
        task: Task,
        attempt_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        finalizing_cancellation: bool = False,
    ) -> str:
        """Atomically conclude an IN_FLIGHT attempt as FINISHED together with
        the task's post-execution lifecycle write and audit checkpoint.

        Returns ``"finished"`` on success, ``"cancelled"`` when an authoritative
        cancellation was recorded in the conclude window (so a stale
        success/failure never overwrites it), or ``"lost"`` if the attempt was
        no longer IN_FLIGHT. ``finalizing_cancellation`` skips the re-check
        because the write itself is the cancellation.
        """
        ...

    def append_usage(self, usage: UsageRecord) -> None:
        """Compatibility insertion. Rejects invalid monetary data and refuses to
        overwrite an existing usage id with a conflicting record."""
        ...

    def monthly_monetary_cost(self, month: str | None = None) -> float:
        """Measured metered spend of the month; the compatibility view.

        Enforcement uses ``ledger_snapshot`` instead: measured spend alone
        ignores reserved exposure that is already committed to an execution.
        """
        ...

    def ledger_snapshot(self, month: str | None = None) -> LedgerSnapshot:
        """The authoritative monetary position used for every paid decision."""
        ...

    def finalize_task(
        self,
        session: SessionState,
        task: Task,
        decision: DecisionRecord,
        usage: UsageRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Finalize a task; True only when this writer owned the transition.

        The guarded terminal task write, the session update, the decision
        record, the final usage bookkeeping and the terminal event are one
        all-or-nothing operation. False means another terminal writer already
        won: nothing was written and the caller must report authoritative state.
        """
        ...


@runtime_checkable
class ResultVerifier(Protocol):
    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord: ...
