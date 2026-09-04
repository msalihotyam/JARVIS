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

    ``..`` segments are collapsed LEXICALLY; the result is case-folded by the
    platform rules and uses forward slashes only. Links are NOT dereferenced.

    P2. Lexical and only lexical: ``os.path.abspath`` anchors a relative name
    to the working directory and normalizes it as a string - on Windows
    through ``GetFullPathNameW``, which never touches the filesystem.
    ``resolve`` cannot be used here. It DEREFERENCES the chain (on Windows by
    opening it and asking ``GetFinalPathNameByHandleW``), and this function is
    the FIRST thing in the loop to touch a local-file target - so a junction
    planted below an allowed root would be followed here, before any no-follow
    root-identity/reparse-chain policy has accepted a single component. That
    is an inspection aimed by a path this product has not yet agreed to look
    at, and on Windows it is an ``open``: a junction aimed at a UNC share
    turns canonicalizing a NAME into an outbound network round-trip carrying
    the user's credentials. Naming a resource must never be an inspection of
    the filesystem it names.

    Nothing is weakened by refusing to dereference, and one thing is fixed. A
    reparse point in the chain now reaches the connector's no-follow scan AS
    ITSELF and is refused there, where dereferencing would have laundered an
    in-root junction into a clean-looking path the policy never saw.
    Containment, the secret-file policy and the stored root-relative name are
    all re-asked afterwards of the path the OPEN HANDLE proves, never of this
    one, so the authoritative identity is unchanged. What does change is the
    accepted contract: an in-root symlink is refused rather than read through,
    and a repoint is refused at the open/policy boundary rather than by
    changing this fingerprint. Two spellings that only meet by traversing a
    link no longer share an identity - each keeps the one it was approved
    under.

    TOTAL by contract: this feeds ``ActionScope.normalized`` and therefore
    task serialization, permission fingerprints and attestation, all of which
    run inside lifecycle stages that turn a REFUSAL into deterministic
    terminal state but have no answer for an EXCEPTION. A target the platform
    refuses to resolve at all - the practical case is an embedded NUL, which
    Windows rejects in ``GetFullPathNameW`` while POSIX tolerates it here and
    rejects it later in ``lstat``/``open`` - must therefore degrade to a
    deterministic normalized spelling rather than raise out of the loop and
    strand a registered task with no terminal state. The value is
    identity-preserving (nothing is stripped or substituted), so it can never
    be confused with a real resolved path, and every containment, policy and
    open check downstream still refuses it.
    """
    try:
        resolved: str | Path = Path(os.path.abspath(Path(path).expanduser()))
    except (ValueError, OSError):
        resolved = str(path)
    return os.path.normcase(str(resolved)).replace("\\", "/")


def unusable_filesystem_target(value: Any) -> str | None:
    """Why no filesystem can ever name this target, or ``None``.

    Deliberately narrow: only what is impossible on EVERY platform, so this
    stays a structural refusal and never a policy one. A NUL terminates a
    path at the OS boundary in both the POSIX and the Win32 APIs, so a target
    carrying one names nothing at all - it is refused as an invalid task, in
    the loop, instead of being carried to a connector that would raise.
    """
    if not isinstance(value, str):
        return None
    if chr(0) in value:
        return "the target contains a NUL character"
    return None


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
        # M5. A local write is the first side-effecting capability, and it is
        # not reversible: JARVIS keeps no pre-image bytes, so nothing can put
        # the previous file back. ARCHITECTURE §5 requires explicit approval
        # for hard-to-reverse data change in EVERY mode, and no policy here
        # can tell a harmless create from an overwrite of something that
        # matters. Membership here also closes the remembered-SESSION path,
        # because ``_remembered_permission_allowed`` excludes mandatory
        # effects - one rule, both surfaces.
        SideEffect.LOCAL_WRITE,
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
    #: The operator has switched execution off globally. A BARRIER, not a
    #: gate: no permission mode, approval, grant or paid-API decision can
    #: satisfy it, because it is not asking a question. It is refused before
    #: any authority that could start work - connector selection, estimation,
    #: attestation, approval creation, grant consumption, attempt opening or
    #: a billing reservation - so a disabled run costs nothing and changes
    #: nothing. Existing in-doubt work keeps its own H2 verdict.
    EXECUTION_DISABLED = "execution_disabled"
    #: The persisted configuration document could not be accepted. Every
    #: refusal shape - malformed JSON, an unknown key, a wrong version, an
    #: invalid value - reads the same way, and none of them applies a single
    #: field. The runtime falls back to a fail-closed posture instead.
    CONFIG_INVALID = "config_invalid"
    #: The configuration store could not be read or written. Never carries
    #: the underlying OS error, the path or a traceback.
    CONFIG_IO_FAILED = "config_io_failed"


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
#: Newest decisions of the SAME project's OTHER sessions examined per
#: retrieval (M2). The querying session is always excluded, so a decision can
#: never enter through two windows. Like every window above this is an
#: internal safety bound, never a result quota: the absolute 5-item /
#: 4,000-character ceilings stay unchanged and no per-source sub-quota
#: exists - all sources compete in the one deterministic ordering.
PROJECT_DECISION_CANDIDATE_WINDOW = 25


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
# Task-scoped local file reads (M3)
# --------------------------------------------------------------------------

#: Physical read ceiling. A larger file is REFUSED; it is never silently
#: truncated, so a bounded excerpt can never misrepresent a file JARVIS was
#: not allowed to read whole.
#: The one capability string that may produce a local-read snapshot. Shared
#: so the connector manifest, the execution-boundary binding and the
#: cross-task reference all name the same thing.
LOCAL_READ_CAPABILITY = "file_read"

#: The single ``ToolResult.data`` key a local-read connector uses to hand its
#: typed snapshot to ``AgentCore``. It lives here rather than in the connector
#: module so the execution boundary does not have to import a connector
#: implementation to know the contract. ``_sanitize_result`` reduces ``data``
#: to ``next_step``, so this transport is read once and never persisted.
LOCAL_READ_RESULT_KEY = "local_read"

#: The task ``context`` key that references an earlier completed local read.
#: A reference only: content is always re-derived from the referenced task's
#: own persisted state and re-validated.
LOCAL_READ_TASK_REFERENCE_KEY = "local_read_task_id"

LOCAL_READ_MAX_BYTES = 64 * 1024
#: Ceiling for the bounded representation that may reach a model or an
#: output envelope. Independent of the physical ceiling above.
LOCAL_READ_EXCERPT_MAX_CHARS = 4_000
#: Hard ceiling on the stored ROOT-RELATIVE path representation. Bounded for
#: the same reason every other persisted string is: an unbounded scalar in a
#: task row reaches the envelope, the approval preview and the outbound
#: payload. A real in-root source path never approaches this.
LOCAL_READ_PATH_MAX_CHARS = 512
#: M9. Hard ceiling on how many source lines ONE bounded region read may
#: request. The excerpt character cap almost always binds first; this refuses
#: an absurd request outright rather than silently narrowing it.
LOCAL_READ_MAX_REGION_LINES = 200
#: M9. Hard ceiling on the first line a region read may name.
LOCAL_READ_MAX_START_LINE = 1_000_000
#: M9. Hard ceiling on the bytes a region read may SCAN while locating its
#: first line. Deliberately a SEPARATE policy surface from
#: ``LOCAL_READ_MAX_BYTES``, which bounds what is CAPTURED: the two answer
#: different questions and must stay free to diverge, so neither may alias
#: the other. Scanned bytes are counted, checked for NUL and discarded; they
#: are never retained, decoded, persisted or egressed.
LOCAL_READ_REGION_SCAN_MAX_BYTES = 4 * 1024 * 1024
#: How many canonical passes ``bound_local_text`` may take to reach a fixed
#: point. Two suffice for every reachable input; the third is the margin that
#: keeps the bound explicit rather than assumed.
_LOCAL_TEXT_STABILIZATION_PASSES = 3
#: Hard ceiling on any canonical timestamp string this module validates.
CANONICAL_TIMESTAMP_MAX_CHARS = 64

#: Deliberately NARROW secret-file deny policy. Every entry is an exact name,
#: an exact extension or an exact directory - never a broad glob - so ordinary
#: project files keep working. ``id_*`` in particular is NOT a pattern here:
#: only the four OpenSSH private-key defaults are denied, so ``id_generator.py``
#: and ``id_rsa.pub`` stay readable.
_DENIED_LOCAL_NAMES = frozenset(
    {
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
    }
)
#: Private-key / PKCS container extensions.
_DENIED_LOCAL_SUFFIXES = frozenset({".pem", ".key", ".pfx", ".p12"})


def local_read_denial_reason(path: Path) -> str | None:
    """Why this canonical path may never be read, or ``None``.

    Evaluated LEXICALLY, on the canonical path exactly as spelled: nothing
    here dereferences a link to discover where it points. A symlink,
    junction or other reparse point is refused as itself by the connector's
    no-follow chain policy, never resolved through to learn its
    destination; and after a secure open the authoritative opened path is
    rechecked against this rule where applicable, so a link cannot smuggle
    a denied file past it. The reason names the policy rule only - never
    the file's content and never an absolute path.
    """
    # Named streams FIRST, because they defeat every rule below: the deny
    # policy matches on the exact name and the exact suffix, and ``.env:s``
    # has neither name ``.env`` nor suffix ``.pem`` - while
    # ``secret.pem:s:$DATA`` resolves to a stream of a denied file and reads
    # its bytes. A stream is also a second data channel inside one directory
    # entry, which is simply outside M3's single-file contract, so it is
    # refused rather than parsed. The anchor is skipped so ordinary Windows
    # drive handling (``C:\...``) is untouched; the rule applies to every
    # component below it and is deliberately uniform across platforms, so a
    # colon can never mean two different things in one stored identity.
    components = path.parts[1:] if path.anchor else path.parts
    if any(":" in component for component in components):
        return "windows alternate data stream"
    name = path.name.casefold()
    parents = {part.casefold() for part in path.parts[:-1]}
    if ".ssh" in parents:
        return "ssh key directory"
    if ".aws" in parents and name == "credentials":
        return "cloud credential file"
    if name in _DENIED_LOCAL_NAMES:
        return "credential-bearing file name"
    if name.startswith(".env."):
        # The dotenv family is secret-bearing by definition. ``.env.example``
        # is refused too: a documented, accepted false positive, because a
        # template carries no value worth widening the rule for.
        return "dotenv family file"
    if path.suffix.casefold() in _DENIED_LOCAL_SUFFIXES:
        return "private key or certificate container"
    return None


def normalize_local_text(value: str) -> str:
    """Newline normalization ONLY.

    Deliberately not ``normalize_retrieved_text``: that one strips leading and
    trailing whitespace, which is right for a display snippet and wrong for
    file content, where indentation and trailing newlines are part of the
    data. No Unicode normalization either: the bytes are evidence.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def bound_local_text(value: str) -> tuple[str, bool]:
    """Canonical, deterministic bounded representation of file content.

    The order is mandatory and is the whole contract: newline normalization,
    then credential redaction, then local-path redaction, and only then the
    ``LOCAL_READ_EXCERPT_MAX_CHARS`` head truncation. Truncating LAST means a
    redaction that expands the text can never push the result past the
    ceiling. No summarization, no chunk selection, no heuristics - the same
    input always produces the same output.

    The result is also a FIXED POINT: applying this function to its own
    output returns that output unchanged. That is what lets every downstream
    boundary state excerpt canonicality as ``bound_local_text(excerpt) ==
    excerpt`` and refuse anything else. It needs a loop because the last step
    can, in principle, create work for the first: truncation cuts inside an
    already-redacted marker, and re-redacting that fragment rewrites it. The
    loop is bounded and each pass is a full canonical pass, so a value that
    has not converged is still fully redacted - it is simply refused as
    non-canonical downstream rather than silently accepted.
    """
    text = normalize_local_text(str(value))
    truncated = False
    for _ in range(_LOCAL_TEXT_STABILIZATION_PASSES):
        bounded, _ = redact_credentials(text)
        bounded = redact_local_paths(bounded)
        bounded, over = truncate_retrieved_text(
            bounded, LOCAL_READ_EXCERPT_MAX_CHARS
        )
        truncated = truncated or over
        if bounded == text:
            return text, truncated
        text = bounded
    return text, truncated


def excerpt_line_span(excerpt: str, truncated: bool) -> int:
    """How many SOURCE lines a bounded excerpt represents, at least partially.

    THE canonical answer, used both by the region reader to derive
    ``end_line`` and by ``local_read_snapshot_refusal`` to prove a persisted
    span against the excerpt it claims to describe.

    The subtlety this function exists for: ``truncate_retrieved_text`` builds
    ``value[: cap - 1] + ELLIPSIS``, so when the cut lands immediately after
    a newline the excerpt ends with a newline followed by the ellipsis, which
    then sits ALONE on a trailing line carrying no source content. Counting
    newlines naively over-reports the span by exactly one. Measured on real
    regions of this repo that happens on roughly one 90-line region in a
    hundred - rare enough to survive review, common enough to be wrong in
    production.

    The synthetic marker is stripped ONLY when ``truncated`` is true, which
    is what keeps a file whose real content ends in an ellipsis counted
    correctly: nothing was cut, so nothing is removed. After the strip both
    cases collapse into one rule - a mid-line cut leaves text not ending in a
    newline (the partial line counts, because the reader can see part of it),
    and a post-newline cut leaves text ending in one (the phantom line does
    not count).

    Line counting is the canonical rule for text this module has already
    newline-normalized: no lines in empty text, otherwise one per newline
    plus one more for a final unterminated line.
    """
    if not excerpt:
        return 0
    text = excerpt
    if truncated and text.endswith("…"):
        text = text[:-1]
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def canonical_utc_timestamp(value: Any) -> str | None:
    """The value itself if it is the canonical UTC timestamp grammar, else ``None``.

    The grammar is exactly what ``utc_now`` writes: a bounded, timezone-aware
    ISO-8601 string whose offset is UTC. Nothing is coerced and nothing is
    repaired - ``str(value)`` on an arbitrary object would turn a tampered row
    into a plausible timestamp, which is the failure this exists to prevent.
    """
    if type(value) is not str or not value:
        return None
    if len(value) > CANONICAL_TIMESTAMP_MAX_CHARS:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return value


def local_read_path_refusal(value: Any) -> str | None:
    """Why this ROOT-RELATIVE local-read path may never be represented, or ``None``.

    The single grammar for the stored path representation, reused verbatim by
    the connector (before any byte is read), by the ``AgentCore`` execution
    boundary, by the public envelope and by the Claude egress boundary - so
    there is one contract rather than four that drift.

    The last rule is P1-4 and it is deliberately FAIL CLOSED rather than a
    redaction: a path is a resource identifier, and rewriting
    ``api_key=HUNTER2.txt`` into a redacted pseudo-path would make the
    approved identity and the displayed identity diverge while still
    asserting that a bounded, trustworthy read happened. A credential-bearing
    file name is refused instead, so approval identity and display identity
    stay the same string. Ordinary names such as ``apikey.md``,
    ``tokenizer.py`` and ``secret_handling.md`` carry no credential VALUE and
    stay readable - the shared ``credential_bearing`` policy, not a
    name-shaped guess, is what decides.
    """
    if type(value) is not str:
        return "path is not a string"
    if not value:
        return "path is empty"
    if len(value) > LOCAL_READ_PATH_MAX_CHARS:
        return "path exceeds its character ceiling"
    if "\\" in value or chr(0) in value:
        # Also the UNC rejection: a ``\\server\share`` spelling cannot
        # survive the backslash rule, so no UNC form is ever stored.
        return "path is not a root-relative POSIX path"
    if value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        return "path is absolute"
    if ":" in value:
        # Anything left carrying a colon is NTFS named-stream syntax; the
        # drive-letter form was already handled above.
        return "path carries an alternate-data-stream separator"
    if any(part in ("", ".", "..") for part in value.split("/")):
        return "path has an empty or traversal component"
    if credential_bearing(value):
        return "path is credential-bearing"
    return None


def local_read_snapshot_refusal(value: Any) -> str | None:
    """Why this object is not a usable local-read snapshot, or ``None``.

    THE canonical structural contract for ``LocalReadSnapshot`` (P1-5). Every
    authority boundary calls this exact function: the load boundary
    (``from_dict``), the execution boundary (``AgentCore``), the public
    envelope and the Claude egress payload. Nothing here coerces, and every
    field is checked against its exact type, so a tampered task row can never
    widen a ceiling, smuggle a raw string past an enum check or carry an
    unbounded scalar into anything a user or a model reads.

    An absent snapshot is refused too ("path is empty"); presence is the
    caller's own question, asked with ``present`` before this one.
    """
    if type(value) is not LocalReadSnapshot:
        return "not a LocalReadSnapshot"
    reason = local_read_path_refusal(value.relative_path)
    if reason is not None:
        return reason
    # ``type() is int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value.byte_size) is not int:
        return "byte size is not an integer"
    if not 0 <= value.byte_size <= LOCAL_READ_MAX_BYTES:
        return "byte size is outside the physical read ceiling"
    digest = value.sha256
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return "digest is not a lowercase sha256 hex digest"
    if type(value.excerpt) is not str:
        return "excerpt is not a string"
    if len(value.excerpt) > LOCAL_READ_EXCERPT_MAX_CHARS:
        return "excerpt exceeds its character ceiling"
    # Length and type are not hygiene. A row edited outside the product can
    # sit comfortably under the ceiling while carrying raw CRLF, an absolute
    # local path or a live credential - none of which any real read could
    # ever have produced, because ``bound_local_text`` removed them. Since
    # that function is a fixed point, "is this excerpt what a real read would
    # have stored?" is decidable exactly, with no heuristic: canonical form
    # is accepted, anything else is refused rather than re-derived. Refusing
    # is the point - repairing it here would let a tampered excerpt become
    # active state wearing a clean face.
    if bound_local_text(value.excerpt)[0] != value.excerpt:
        return "excerpt is not the canonical bounded representation"
    if type(value.truncated) is not bool:
        return "truncated flag is not a boolean"
    if type(value.classification) is not DataClassification:
        return "classification is not an exact DataClassification"
    if canonical_utc_timestamp(value.read_at) is None:
        return "read timestamp is not a canonical UTC timestamp"
    # M9. The region span, proved against the excerpt it claims to describe.
    # ``type() is int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value.start_line) is not int or type(value.end_line) is not int:
        return "region line span is not an exact integer pair"
    if value.start_line == 0 and value.end_line == 0:
        return None
    if not 1 <= value.start_line <= LOCAL_READ_MAX_START_LINE:
        return "region start line is outside its ceiling"
    if value.end_line < value.start_line:
        return "region end line precedes its start line"
    span = value.end_line - value.start_line + 1
    if span > LOCAL_READ_MAX_REGION_LINES:
        return "region span exceeds its line ceiling"
    # The check that makes a tampered span detectable from the snapshot
    # ALONE, with no filesystem access: the claimed span must be exactly the
    # span the excerpt actually shows.
    if span != excerpt_line_span(value.excerpt, value.truncated):
        return "region span does not match the excerpt it describes"
    return None


@dataclass(frozen=True, slots=True)
class LocalReadSnapshot:
    """One bounded, redacted, task-scoped view of a local file.

    Untrusted reference data with NO authority: it never becomes an
    instruction, a scope change, an approval, a billing input or memory. It
    is task-scoped by construction - it lives on the task that produced it,
    never in project memory and never in the decision log.

    ``classification`` is NOT connector metadata: ``AgentCore`` binds it from
    the reading task's own trusted scope classification, so a connector can
    neither lower it nor establish authority with a malformed value. A
    malformed persisted value loads closed as SECRET.

    ``relative_path`` is always relative to an allowed root, so no absolute
    path or user directory layout can travel with the snapshot.
    """

    relative_path: str = ""
    byte_size: int = 0
    sha256: str = ""
    excerpt: str = ""
    truncated: bool = False
    classification: DataClassification = DataClassification.SECRET
    read_at: str = ""
    #: M9. The SOURCE-LINE SPAN the bounded ``excerpt`` represents - not the
    #: span of the raw capture. Both 0 for a head read. For a region read
    #: ``start_line`` is the requested first line and ``end_line`` is the last
    #: source line shown at least partially, so a line cut in half by the
    #: character cap still counts: the reader can see part of it.
    #:
    #: ``byte_size``/``sha256`` describe the COMPLETE RAW CAPTURE; these two
    #: and ``excerpt``/``truncated`` describe the VISIBLE VIEW of it. That is
    #: exactly the relationship a head read already has, where the capture is
    #: the whole file, and ``truncated`` means only "the view shows less than
    #: the capture" - never that the requested region itself was cut.
    start_line: int = 0
    end_line: int = 0

    @property
    def present(self) -> bool:
        return bool(self.relative_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "excerpt": self.excerpt,
            "truncated": self.truncated,
            "classification": self.classification.value,
            "read_at": self.read_at,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LocalReadSnapshot":
        """Rebuild defensively; malformed persisted state fails closed.

        The ONLY repair applied is re-bounding an oversized excerpt, because
        a ceiling that a tampered row widened must not survive a load. Every
        other field is taken verbatim and then judged by the single canonical
        contract, ``local_read_snapshot_refusal``. A classification that is
        not an exact recognized value is not repaired either - not even to
        SECRET - and discards the whole snapshot. Nothing is coerced:
        ``persistable_count`` on a byte size, ``bool()`` on a truncation flag
        and ``str()`` on a timestamp would each turn tampered persisted state
        into plausible ACTIVE state, which is exactly the P1-5 failure. A row
        that does not satisfy the contract degrades to the empty snapshot -
        no path, no excerpt, no authority - rather than raising.
        """
        if not isinstance(data, dict):
            return cls()
        # An exact recognized classification string, or NOTHING. A SECRET
        # fallback looked fail-closed and was not: it turned a row whose
        # classification could not be read into a STRUCTURALLY VALID,
        # PRESENT snapshot - one that passes the whole contract, renders a
        # block in the envelope, and is a real snapshot for every gate that
        # asks ``present`` before it asks anything else. Malformed persisted
        # state must not survive as a usable object wearing a safe label; the
        # snapshot is discarded instead. ``type() is str`` rather than
        # ``isinstance``: a foreign ``str`` enum carrying the same value is
        # not this contract's classification.
        raw_classification = data.get("classification")
        if type(raw_classification) is not str:
            return cls()
        try:
            classification = DataClassification(raw_classification)
        except ValueError:
            return cls()
        excerpt = data.get("excerpt")
        excerpt, over = truncate_retrieved_text(
            excerpt if isinstance(excerpt, str) else "",
            LOCAL_READ_EXCERPT_MAX_CHARS,
        )
        truncated = data.get("truncated", False)
        if over and type(truncated) is bool:
            # Re-bounding is the one repair, so its flag must follow it. A
            # non-boolean is left alone and refused below.
            truncated = True
        candidate = cls(
            relative_path=data.get("relative_path", ""),
            byte_size=data.get("byte_size", 0),
            sha256=data.get("sha256", ""),
            excerpt=excerpt,
            truncated=truncated,
            classification=classification,
            read_at=data.get("read_at", ""),
            # M9. Taken verbatim; a legacy pre-M9 row has neither key and
            # loads as the head span. Nothing is coerced - a non-integer or an
            # inconsistent span is judged by the one contract below and
            # discards the whole snapshot rather than being repaired.
            start_line=data.get("start_line", 0),
            end_line=data.get("end_line", 0),
        )
        return candidate if local_read_snapshot_refusal(candidate) is None else cls()


# --------------------------------------------------------------------------
# M5. Bounded, task-scoped local WRITE
# --------------------------------------------------------------------------

#: Capability name of the bounded single-file local write, so the connector
#: manifest, the canonical-target binding and the decision gate all name the
#: same thing.
LOCAL_WRITE_CAPABILITY = "file_write"

#: The EXACT capabilities whose ordinary trusted lifecycle rewrites
#: ``ActionScope.target`` through ``canonical_path``. THE single list, named
#: once: ``AgentCore._bind_canonical_local_target`` decides which tasks it
#: canonicalizes from it, and ``chain_task_refusal`` decides from the same
#: constant which STORED tasks may legitimately carry the canonical spelling
#: instead of the declared one. Two lists would drift, and a drift here is an
#: authority widening: a capability the runtime never canonicalizes must not
#: be able to hold chain authority under a rewritten target.
CANONICAL_TARGET_CAPABILITIES = (LOCAL_READ_CAPABILITY, LOCAL_WRITE_CAPABILITY)

#: The single ``ToolResult.data`` key a local-write connector uses to hand its
#: typed record to ``AgentCore``. ``_sanitize_result`` reduces ``data`` to
#: ``next_step``, so this transport is read once and never persisted.
LOCAL_WRITE_RESULT_KEY = "local_write"

#: Physical ceiling on the bytes one local write may produce. Deliberately a
#: SEPARATE policy surface from ``LOCAL_READ_MAX_BYTES``: the two are equal
#: today and must stay free to diverge, so neither may alias the other.
LOCAL_WRITE_MAX_BYTES = 64 * 1024

#: The write modes this contract can DESCRIBE. They are not a hint:
#: ``create`` requires the target to be ABSENT and ``overwrite`` requires it
#: to be PRESENT with the attested digest and size, so "I meant to make a new
#: file" can never silently become "I replaced an existing one".
LOCAL_WRITE_MODES = ("create", "overwrite")

#: The write modes POLICY allows to be PERFORMED, which is a strictly
#: narrower question than what the grammar can express - and a separate one
#: again from whether the PLATFORM can perform them safely.
#:
#: ``create`` is enabled because both platforms can enforce it at the
#: mutation itself: the object is installed from an open handle, and the
#: kernel refuses to install it over an existing name. Nothing is checked and
#: then hoped for.
#:
#: ``overwrite`` is enabled by M10, and the primitive it needed was never a
#: rename. M5 looked for "replace this destination if and only if it is still
#: the approved object" among the rename family and correctly found nothing:
#: Windows' ``FILE_RENAME_INFO`` carries a bare ``ReplaceIfExists`` boolean
#: with no destination precondition, and Linux' ``renameat2`` offers
#: ``RENAME_NOREPLACE`` (absence only), ``RENAME_EXCHANGE`` and
#: ``RENAME_WHITEOUT``, none of which is a compare-and-swap. An EDIT does not
#: need one. The object already exists, so it can be HELD: M10 opens the
#: existing leaf beneath the verified parent, pins both the object AND its
#: directory entry through the OS sharing contract, digests the pre-state
#: through that same descriptor and rewrites in place. No name is re-resolved
#: at the mutation, so there is no window to race.
#:
#: This tuple stays PLATFORM-NEUTRAL on purpose. Whether a given host can
#: actually pin the directory entry is a connector question, asked and
#: refused there - exactly as the handle-sourced install primitive already
#: is. Policy does not learn about platforms.
LOCAL_WRITE_ENABLED_MODES = ("create", "overwrite")

#: The exact lifecycle labels a persisted write record may carry.
LOCAL_WRITE_STATUSES = ("prepared", "written", "refused", "unknown")

#: Version tag of the canonical outbound frame the write authority is bound
#: through (H1). Bumping it invalidates every outstanding write approval,
#: which is the point: a changed frame is a changed authority.
LOCAL_WRITE_FRAME_HEADER = b"jarvis.local_write.v1"

#: Same-directory temporary file prefix. Orphans are reported, never deleted:
#: deletion is a DESTRUCTIVE authority this capability does not hold.
LOCAL_WRITE_TEMP_PREFIX = ".jarvis-write-"

#: The three outcomes of an in-doubt write diagnosis. Local writes are, unlike
#: an external egress, DECIDABLE after the fact: the destination is compared
#: against the two attested states and answers for itself.
LOCAL_WRITE_DID_NOT_LAND = "write_did_not_land"
LOCAL_WRITE_LANDED = "write_landed"
LOCAL_WRITE_AMBIGUOUS = "ambiguous_third_party_change"


def _sha256_hex_refusal(value: Any, label: str) -> str | None:
    """One digest grammar for every write-record digest field."""
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        return f"{label} is not a lowercase sha256 hex digest"
    return None


def local_write_content_refusal(value: Any) -> str | None:
    """Why this text may never be written verbatim to a local file, or ``None``.

    THE single grammar for the intended bytes, applied identically at the
    untrusted input boundary and at the persistence load boundary, so a task
    row edited outside the product cannot carry a payload the input boundary
    would have refused.

    Nothing here transforms the text. The bytes a user asked to write are
    written exactly: newline normalization, credential redaction or path
    redaction would silently corrupt the file while the product still claimed
    a trustworthy, attested write. Bounding and refusing is the only safe
    handling of content that is itself the deliverable.
    """
    if type(value) is not str:
        return "write content is not a string"
    if chr(0) in value:
        # A NUL is the binary tell this capability's UTF-8 text contract
        # excludes, and it terminates a string at several OS boundaries.
        return "write content contains a NUL character"
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate survives JSON decoding and cannot be encoded; it
        # is refused rather than replaced, because a replacement character
        # would be bytes the user never asked for.
        return "write content is not encodable as UTF-8"
    if len(encoded) > LOCAL_WRITE_MAX_BYTES:
        return "write content exceeds the local write ceiling"
    return None


def local_write_mode_refusal(value: Any) -> str | None:
    """Why this is not an exact local write mode, or ``None``.

    Grammar only - this is what a persisted record may DESCRIBE. Whether the
    mode may be performed is a separate, narrower question that
    ``local_write_mode_unsupported`` answers.
    """
    if type(value) is not str or value not in LOCAL_WRITE_MODES:
        return "write mode is not an exact local write mode"
    return None


def local_write_mode_unsupported(value: Any) -> str | None:
    """Why POLICY forbids performing this mode, or ``None``.

    Deliberately separate from the grammar, and equally deliberately separate
    from the PLATFORM question. A record must still be able to DESCRIBE a
    mode this host cannot perform - a row written elsewhere has to load and
    validate - and whether the running platform can offer the primitive a
    mode needs is asked by the connector, not here. Policy never learns about
    platforms.
    """
    reason = local_write_mode_refusal(value)
    if reason is not None:
        return reason
    if value not in LOCAL_WRITE_ENABLED_MODES:
        return (
            f"write mode '{value}' is not enabled by policy, so it fails "
            "closed rather than being performed"
        )
    return None


def local_write_record_refusal(value: Any) -> str | None:
    """Why this object is not a usable local-write record, or ``None``.

    THE canonical structural contract for ``LocalWriteRecord``. Every
    boundary calls this exact function: the load boundary (``from_dict``),
    the execution boundary (``AgentCore``) and the public envelope. Nothing
    coerces and every field is checked against its exact type, so a tampered
    task row can never widen a ceiling, smuggle a raw string past a bounded
    vocabulary or carry an unbounded scalar into anything a user reads.

    An absent record is refused too ("path is empty"); presence is the
    caller's own question, asked with ``present`` before this one.

    Note what is deliberately NOT here: the bytes. A write record is
    metadata, so no pre-image and no intended content can ever reach it.
    """
    if type(value) is not LocalWriteRecord:
        return "not a LocalWriteRecord"
    # The same root-relative grammar the read snapshot stores, reused verbatim
    # rather than restated: one path contract for both local capabilities, so
    # a credential-bearing, absolute, traversal or ADS name is refused here
    # for exactly the reasons it is refused there.
    reason = local_read_path_refusal(value.relative_path)
    if reason is not None:
        return reason
    reason = local_write_mode_refusal(value.mode)
    if reason is not None:
        return reason
    # ``type() is bool`` and not ``isinstance``: an ``int`` is not this flag.
    if type(value.pre_present) is not bool:
        return "pre-state presence flag is not a boolean"
    if type(value.pre_byte_size) is not int:
        return "pre-state byte size is not an integer"
    if type(value.intended_byte_size) is not int:
        return "intended byte size is not an integer"
    if not 0 <= value.intended_byte_size <= LOCAL_WRITE_MAX_BYTES:
        return "intended byte size is outside the local write ceiling"
    reason = _sha256_hex_refusal(value.intended_sha256, "intended digest")
    if reason is not None:
        return reason
    if value.pre_present:
        if not 0 <= value.pre_byte_size <= LOCAL_WRITE_MAX_BYTES:
            return "pre-state byte size is outside the local write ceiling"
        reason = _sha256_hex_refusal(value.pre_sha256, "pre-state digest")
        if reason is not None:
            return reason
    elif value.pre_sha256 != "" or value.pre_byte_size != 0:
        # An absent pre-state has NOTHING to describe. A record claiming
        # absence while carrying a digest or a size is not one this product
        # ever wrote, and is refused rather than partially believed.
        return "absent pre-state carries a digest or a size"
    # Mode and pre-state are ONE authority, not two independent fields: a
    # record saying "create" about a file that was present, or "overwrite"
    # about one that was not, describes an operation that could not have been
    # approved. ``is`` compares the two booleans exactly.
    if (value.mode == "create") is value.pre_present:
        return "write mode contradicts the recorded pre-state"
    if type(value.status) is not str or value.status not in LOCAL_WRITE_STATUSES:
        return "status is not an exact local write status"
    if canonical_utc_timestamp(value.written_at) is None:
        return "write timestamp is not a canonical UTC timestamp"
    return None


@dataclass(frozen=True, slots=True)
class LocalWriteRecord:
    """Metadata of ONE bounded local write, scoped to the task that did it.

    Carries no bytes at all - not the intended content and not a pre-image.
    That is the whole design: M5 keeps no backup, so it promises no rollback,
    and a record that cannot restore anything must not look like one that
    can. What it does carry is exactly enough to answer the two questions
    that matter after the fact: what was authorized, and what is on disk now.

    Untrusted reference data with NO authority: it never becomes an
    instruction, a scope change, an approval or a billing input, it is never
    written to project memory, and a local write produces no decision record.
    """

    relative_path: str = ""
    mode: str = ""
    pre_present: bool = False
    pre_sha256: str = ""
    pre_byte_size: int = 0
    intended_sha256: str = ""
    intended_byte_size: int = 0
    status: str = ""
    written_at: str = ""

    @property
    def present(self) -> bool:
        return bool(self.relative_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "mode": self.mode,
            "pre_present": self.pre_present,
            "pre_sha256": self.pre_sha256,
            "pre_byte_size": self.pre_byte_size,
            "intended_sha256": self.intended_sha256,
            "intended_byte_size": self.intended_byte_size,
            "status": self.status,
            "written_at": self.written_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LocalWriteRecord":
        """Rebuild defensively; malformed persisted state fails closed.

        NOTHING is repaired here - there is no analogue of the read
        snapshot's excerpt re-bounding, because every field is either exact
        or meaningless. Each value is taken verbatim and then judged by the
        single canonical contract; a row that does not satisfy it degrades to
        the empty record rather than raising or becoming a plausible-looking
        valid one.
        """
        if not isinstance(data, dict):
            return cls()
        candidate = cls(
            relative_path=data.get("relative_path", ""),
            mode=data.get("mode", ""),
            pre_present=data.get("pre_present", False),
            pre_sha256=data.get("pre_sha256", ""),
            pre_byte_size=data.get("pre_byte_size", 0),
            intended_sha256=data.get("intended_sha256", ""),
            intended_byte_size=data.get("intended_byte_size", 0),
            status=data.get("status", ""),
            written_at=data.get("written_at", ""),
        )
        return candidate if local_write_record_refusal(candidate) is None else cls()


# --------------------------------------------------------------------------
# M6. Bounded, task-scoped MODEL ANSWER
# --------------------------------------------------------------------------

#: The single ``ToolResult.data`` key an external-model connector uses to hand
#: its typed answer to ``AgentCore``. ``_sanitize_result`` reduces ``data`` to
#: ``next_step``, so this transport is read once and never persisted as data.
MODEL_ANSWER_RESULT_KEY = "model_answer"

#: Ceiling on the user-facing model deliverable. Its OWN policy surface: it
#: is not, and may never become, an alias of ``LOCAL_READ_EXCERPT_MAX_CHARS``.
#: A read excerpt is a bounded view of a local file this machine already owns;
#: this is inbound text from an external model, and the two ceilings answer
#: different questions and must stay free to diverge.
MODEL_ANSWER_MAX_CHARS = 8_000

#: Bounded working window applied to the RAW model text before any redaction
#: pass runs over it. Everything past it is discarded rather than processed,
#: which keeps the credential pass linear on adversarially large output. The
#: same technique ``sanitize_diagnostic_text`` uses for the same reason.
MODEL_ANSWER_SOURCE_MAX_CHARS = 8 * MODEL_ANSWER_MAX_CHARS

#: Convergence budget for ``bound_model_text``; the same bounded-loop shape as
#: ``bound_local_text`` and for the same reason (see that function).
_MODEL_TEXT_STABILIZATION_PASSES = 3


def normalize_model_text(value: str) -> str:
    """Newline normalization ONLY, for a model answer.

    Deliberately NOT ``normalize_local_text``'s twin by accident and NOT
    ``normalize_retrieved_text``: no NFKC, no strip, no whitespace collapse.
    Indentation, blank lines and a trailing newline are part of the
    deliverable a user reads and may copy, not formatting noise, and NFKC
    would rewrite characters the model actually produced.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def bound_model_text(value: str) -> tuple[str, bool]:
    """Canonical, deterministic bounded representation of a model answer.

    The order is mandatory and IS the contract:

    1. exact ``str`` only - no coercion anywhere on this path;
    2. newline normalization;
    3. a bounded source window, cut at a REDACTION-SAFE boundary (below), so
       redaction never runs over unbounded input and never inherits a token
       that the cut itself sliced in half;
    4. shared credential redaction - BEFORE truncation, so a credential past
       the eventual display boundary is still removed rather than surviving
       in an intermediate persisted form;
    5. head truncation to ``MODEL_ANSWER_MAX_CHARS``.

    Step 3's boundary is a security control, not tidiness. A raw
    ``text[:MODEL_ANSWER_SOURCE_MAX_CHARS]`` is the one cut in this function
    that would precede a redaction pass, so a credential straddling it would
    be reduced to a prefix too short for its own pattern to recognize - and a
    prefix is never redacted, because there is nothing left to match. Heavy
    redaction earlier in the window can then compress the retained text far
    enough that the fragment lands inside the 8000-character deliverable.

    The boundary is therefore the last NEWLINE inside the window, and the
    entire boundary-crossing final line is discarded. Newline is not chosen
    because it terminates every pattern - it does not: ``Bearer\\s+``, the
    label's ``[_\\-\\s]?``, the assignment's ``\\s*`` delimiter and the CLI
    flag's ``\\s+`` joiner all admit one. It is chosen because of a strictly
    weaker property that IS true of every recognizer in the shared policy:

        no recognized credential can carry SECRET MATERIAL on both sides of
        a newline.

    * ``sk-``, JWT and the AWS key-id patterns contain no newline at all, so
      no match can span one.
    * ``Bearer`` admits a newline only in the gap between the keyword and the
      token, so the part before it is the public keyword and whitespace.
    * The labelled form's value class is ``[^,;\\n]+``: a newline can only
      fall inside the label or the delimiter, leaving no value behind - and a
      cut inside the value still matches, because the class is ``+``.
    * The CLI form's value scanner runs to whitespace, to the closing quote,
      or to the END of the text, so a cut inside a value leaves something the
      scanner still consumes.

    If the window holds no newline at all, there is no boundary this function
    can prove safe, so it FAILS CLOSED and yields no text rather than risk
    publishing half a token. An empty answer is refused by the connector's
    verifier, which is loud; a leaked credential prefix would not be.

    What it deliberately does NOT do, and the reason ``bound_local_text``
    could not simply be reused: it never calls ``redact_local_paths``. In a
    diagnostic string an absolute path is accidental leakage; in a model
    answer about this project's code it is the answer. Redacting it would
    destroy the deliverable while still claiming to present one.

    It also appends no ellipsis or any other in-band truncation marker. This
    text is meant to be read and copied, so a character the model never wrote
    must not appear inside it; the fact of truncation is carried out of band
    by the typed ``truncated`` flag instead, which is strictly more honest.

    The result is a FIXED POINT: applying this function to its own output
    returns that output unchanged. That is what lets every downstream
    boundary state canonicality as ``bound_model_text(content)[0] == content``
    and refuse anything else instead of re-deriving it. The loop exists
    because truncation can, in principle, create work for redaction (a cut
    inside an already-redacted marker); it is bounded, each pass is a full
    canonical pass, and a value that has not converged is still fully
    redacted - it is simply refused downstream as non-canonical.
    """
    if type(value) is not str:
        # End to end, not merely at one caller: a coercion here would hand a
        # provider's list or dict to the redaction passes and then present the
        # result as something a model wrote.
        raise TypeError("model answer text must be an exact str")
    text = normalize_model_text(value)
    truncated = len(text) > MODEL_ANSWER_SOURCE_MAX_CHARS
    if truncated:
        window = text[:MODEL_ANSWER_SOURCE_MAX_CHARS]
        # Keep through the last complete line; drop the line the window cut.
        boundary = window.rfind("\n")
        text = window[: boundary + 1] if boundary >= 0 else ""
    for _ in range(_MODEL_TEXT_STABILIZATION_PASSES):
        bounded, _ = redact_credentials(text)
        if len(bounded) > MODEL_ANSWER_MAX_CHARS:
            bounded = bounded[:MODEL_ANSWER_MAX_CHARS]
            truncated = True
        if bounded == text:
            return text, truncated
        text = bounded
    return text, truncated


def model_answer_refusal(value: Any) -> str | None:
    """Why this object is not a usable model answer, or ``None``.

    THE canonical structural contract for ``ModelAnswer``. Every boundary
    calls this exact function - the load boundary (``from_dict``), the
    execution boundary (``AgentCore``), the connector's own verifier and the
    public envelope - so four subtly different validators cannot drift apart.
    That drift is the failure M3 had to close once already; it is not going
    to be reintroduced here.

    Nothing is coerced and every field is checked against its exact type, so
    a task row edited outside the product can neither widen the ceiling nor
    present tampered text as a trustworthy answer.

    An absent answer is refused too; presence is the caller's own question,
    asked with ``present`` before this one.
    """
    if type(value) is not ModelAnswer:
        return "not a ModelAnswer"
    if type(value.content) is not str:
        return "content is not a string"
    try:
        encoded = value.content.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate survives JSON decoding of a tampered row and cannot
        # be encoded. Refusing here mirrors ``local_write_content_refusal``:
        # the row loads closed as the empty answer instead of raising out of
        # ``Task.from_dict`` and every loader above it.
        return "content is not encodable as UTF-8"
    if len(value.content) > MODEL_ANSWER_MAX_CHARS:
        return "content exceeds its character ceiling"
    # Length and type are not hygiene. A row edited outside the product can
    # sit under the ceiling while carrying raw CRLF or a live credential -
    # neither of which any real answer could have stored, because
    # ``bound_model_text`` removed them. Since that function is a fixed
    # point, "is this what a real answer would have stored?" is decidable
    # exactly. Refusing is the point: repairing here would let tampered text
    # become active state wearing a clean face.
    if bound_model_text(value.content)[0] != value.content:
        return "content is not the canonical bounded representation"
    # ``type() is int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value.character_count) is not int:
        return "character count is not an integer"
    if value.character_count != len(value.content):
        return "character count does not match the content"
    digest = value.sha256
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return "digest is not a lowercase sha256 hex digest"
    # Unlike the read snapshot's digest - which describes the whole file and
    # therefore cannot be checked against the excerpt - this one describes
    # exactly the persisted text, so the check is unconditional and a
    # tampered content field cannot survive it.
    if hashlib.sha256(encoded).hexdigest() != digest:
        return "digest does not match the content"
    if type(value.truncated) is not bool:
        return "truncated flag is not a boolean"
    # Exact type is ALL that is checkable here, and deliberately so. An
    # earlier version also required ``truncated`` to imply a content length of
    # exactly the ceiling. That rule was simply false: loss has two
    # independent sources - the source window and the output cap - and a
    # window-truncated answer that redaction then compresses legitimately
    # ends up well under the ceiling. The rule rejected honest records, and
    # the truth it claimed to check is not reconstructible anyway once the
    # discarded text is gone. ``truncated`` is producer-set metadata whose
    # integrity is bound to the record by the digest; it is not, and must not
    # be presented as, a provenance claim this validator can re-derive.
    if canonical_utc_timestamp(value.produced_at) is None:
        return "produced timestamp is not a canonical UTC timestamp"
    return None


def build_model_answer(value: Any) -> "ModelAnswer":
    """The canonical constructor: raw model text in, typed record out.

    The ONLY place a ``ModelAnswer`` is built from untrusted text. Only an
    actual ``str`` produces an answer at all - an arbitrary object is never
    stringified into one - and everything else follows from
    ``bound_model_text``, so the record is canonical by construction rather
    than by inspection afterwards.
    """
    if type(value) is not str:
        return ModelAnswer()
    content, truncated = bound_model_text(value)
    return ModelAnswer(
        content=content,
        character_count=len(content),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        truncated=truncated,
        produced_at=utc_now(),
    )


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    """One bounded, structure-preserving answer from an external model.

    UNTRUSTED INBOUND MODEL DATA with NO authority whatsoever. It never
    becomes an instruction, a scope change, an approval, a billing input, a
    plan, a connector choice or a verification judgement; it is never written
    to project memory; it is never a retrieval candidate; and nothing feeds
    it back to the model. It is task-scoped by construction - it lives on the
    task that produced it and nowhere else.

    ``sha256`` is INTEGRITY EVIDENCE for the persisted, displayed text: it is
    the digest of exactly ``content``. It is deliberately NOT provenance and
    NOT an attestation. H1 attestation binds the OUTBOUND prompt bytes; this
    record is what came back, and no digest of inbound text can say anything
    about how it was produced.

    This contract also makes no claim about the answer's truth, correctness,
    relevance or freedom from hallucination, and prompt injection is not
    solved by it: a poisoned local file can influence what a model says. What
    is guaranteed is narrower and real - the text is bounded, canonical,
    credential-redacted, tamper-evident and inert.

    ``truncated`` says only that SOME source content was discarded, by either
    bounded stage: the source window or the output cap. It is a single public
    boolean on purpose, and it is producer-set. Its integrity travels with the
    record - the digest covers the content it describes - but its historical
    truth is NOT re-derivable afterwards, because the discarded text is gone.
    No boundary claims otherwise, and in particular a truncated answer may be
    far shorter than the ceiling: redaction can compress heavily.
    """

    content: str = ""
    character_count: int = 0
    sha256: str = ""
    truncated: bool = False
    produced_at: str = ""

    @property
    def present(self) -> bool:
        return bool(self.sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "character_count": self.character_count,
            "sha256": self.sha256,
            "truncated": self.truncated,
            "produced_at": self.produced_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ModelAnswer":
        """Rebuild defensively; malformed persisted state fails closed.

        NOTHING is repaired and nothing is coerced. ``str()`` on content,
        ``persistable_count`` on the character count, ``bool()`` on the
        truncation flag or ``str()`` on the timestamp would each turn a
        tampered row into plausible ACTIVE state - the exact failure the read
        snapshot had to close. There is no re-bounding either: an oversized
        content field would produce a digest that no longer matches, so
        repairing it could only manufacture a record nothing ever wrote.

        A row that does not satisfy the single canonical contract degrades to
        the empty, non-present answer rather than raising.
        """
        if not isinstance(data, dict):
            return cls()
        candidate = cls(
            content=data.get("content", ""),
            character_count=data.get("character_count", 0),
            sha256=data.get("sha256", ""),
            truncated=data.get("truncated", False),
            produced_at=data.get("produced_at", ""),
        )
        return candidate if model_answer_refusal(candidate) is None else cls()


# --------------------------------------------------------------------------
# M7. Explicit model answer handoff (durable write-source identity)
# --------------------------------------------------------------------------

#: The ``write`` input key that names ONE completed same-session external-model
#: task whose answer is the source of a create-only write's bytes. A REFERENCE,
#: never content: the bytes are always re-derived from the source task's own
#: persisted state and re-validated in ``AgentCore``. It lives in the ``write``
#: section - the one input section that never egresses - and never in
#: ``context``, which the Claude connector serializes verbatim.
LOCAL_WRITE_SOURCE_KEY = "model_answer_task_id"

#: Hard ceiling on a persisted source reference. A task id is a 36-character
#: uuid4; the bound keeps the persisted field and the approval reason it
#: feeds bounded, exactly like every other persisted scalar.
LOCAL_WRITE_SOURCE_MAX_CHARS = 64


class WriteSource(str, Enum):
    """Durable semantic identity of WHERE a local write's bytes come from.

    Internal persisted state, never user-supplied authority: the input
    boundary derives it from the validated ``write`` shape and nothing else
    ever sets it. It exists so that a task created as an explicit model
    answer handoff can never silently turn into an ordinary direct write
    merely because persisted provenance was corrupted. A malformed or
    inconsistent identity loads as ``INVALID`` - a typed verdict that every
    authority-building stage refuses - rather than as either active kind.
    ``INVALID`` is never produced by the input boundary; it is accepted on
    load only so that load -> resave -> load stays invalid.

    The identity carries no filesystem authority of its own. The M5 frame
    keeps binding the exact bytes, the destination, the mode and the
    pre-state; this only says how the bytes got onto the task.
    """

    DIRECT = "direct"
    MODEL_ANSWER = "model_answer"
    INVALID = "invalid"


_WRITE_SOURCE_VALUES = frozenset(item.value for item in WriteSource)


def local_write_source_refusal(value: Any) -> str | None:
    """Why this is not a usable source-task reference, or ``None``.

    THE single grammar for the reference, applied at the input boundary, at
    the persistence load boundary and again by the resolver. Nothing is
    coerced or canonicalized: a padded reference is refused, not stripped,
    so the identity that was approved is the identity that was typed.
    """
    if type(value) is not str:
        return "source reference is not a string"
    if not value:
        return "source reference is empty"
    if chr(0) in value:
        return "source reference contains a NUL character"
    if value != value.strip():
        return "source reference carries surrounding whitespace"
    if len(value) > LOCAL_WRITE_SOURCE_MAX_CHARS:
        return "source reference exceeds its character ceiling"
    return None


def local_write_identity_refusal(source: Any, reference: Any) -> str | None:
    """Why this (source kind, reference) pair is not a usable write identity.

    THE single pair contract. ``DIRECT`` must carry no reference, so a
    reference that appears on a direct task through tampering can never be
    consumed; ``MODEL_ANSWER`` must carry a reference that satisfies the
    grammar; ``INVALID`` is refused unconditionally. Exact types only - a
    raw ``"direct"`` string is not this contract's kind.
    """
    if type(source) is not WriteSource:
        return "write source is not an exact WriteSource"
    if source is WriteSource.INVALID:
        return "write source identity is invalid"
    if type(reference) is not str:
        return "source reference is not a string"
    if source is WriteSource.DIRECT:
        if reference != "":
            return "a direct write carries a source reference"
        return None
    reason = local_write_source_refusal(reference)
    if reason is not None:
        return f"handoff reference is malformed: {reason}"
    return None


def handoff_eligible(source: Any, target: Any) -> bool:
    """Whether a source task's classification may flow into a write task's.

    Provider-neutral and deliberately NOT ``retrieval_eligible``: that matrix
    encodes egress eligibility, where a SECRET current task never egresses.
    A local write cannot egress, so the only rule is that content may never
    move into a LESS restrictive classification than its source task's:
    equal or more restrictive targets - SECRET included - are allowed. A
    SECRET source is never eligible: the product can never produce one (a
    SECRET task never reaches an external model), so one on a persisted row
    is tampered data. Exact types on both sides; a raw string or a foreign
    ``str`` enum compares equal to a member and is refused precisely because
    of that.
    """
    if type(source) is not DataClassification or type(target) is not DataClassification:
        return False
    if source is DataClassification.SECRET:
        return False
    return (
        _RETRIEVAL_CLASSIFICATION_RANK[source] <= _RETRIEVAL_CLASSIFICATION_RANK[target]
    )


# --------------------------------------------------------------------------
# M8. Bounded multi-file read context (durable read-context identity)
# --------------------------------------------------------------------------

#: The task ``context`` key naming 1..3 completed same-session local-read
#: tasks whose bounded excerpts become ONE ordered frozen bundle on this
#: task. A reference list only: every excerpt is re-derived from the
#: referenced tasks' own persisted state and re-validated in ``AgentCore``.
#:
#: Unlike the M3 singular key, the decoded value does NOT stay in
#: ``task.context``. The Claude connector serializes ``context`` verbatim
#: into its payload, so a context key is an egress channel; the references
#: live in typed ``Task`` fields instead, exactly as M7's write reference
#: does.
LOCAL_READ_CONTEXT_REFERENCE_KEY = "local_read_task_ids"

#: Hard ceiling on how many completed reads one task may reference.
LOCAL_READ_MAX_SOURCES = 3

#: Hard ceiling on the COMBINED excerpt characters of one frozen bundle,
#: counted with ``len(str)`` - Unicode code points, not UTF-8 bytes, exactly
#: like every other character ceiling here. Deliberately a SEPARATE policy
#: surface from ``LOCAL_READ_EXCERPT_MAX_CHARS``: three canonical snapshots
#: already satisfy it by construction, so this is the defensive bound that
#: stops a tampered row from widening the bundle.
LOCAL_READ_CONTEXT_MAX_CHARS = 12_000

#: Hard ceiling on one persisted source reference. Deliberately a SEPARATE
#: policy surface from ``LOCAL_WRITE_SOURCE_MAX_CHARS``: the two are equal
#: today and must stay free to diverge, so neither may alias the other.
LOCAL_READ_SOURCE_MAX_CHARS = 64


class LocalReadContextKind(str, Enum):
    """Durable semantic identity of THIS task's bounded local-read context.

    Internal persisted state, never user-supplied authority: the input
    boundary derives it from the validated ``context`` shape and nothing else
    ever sets it. It exists so that a task created as a multi-file read
    consumer can never silently become a context-LESS external model request
    merely because its persisted references or frozen bundle were corrupted.
    A malformed or inconsistent identity loads as ``INVALID`` - a typed
    verdict every authority-building stage refuses - rather than as either
    active kind. ``INVALID`` is never produced by the input boundary; it is
    accepted on load only so that load -> resave -> load stays invalid.

    The identity carries no filesystem authority of its own: the frozen
    snapshots stay untrusted reference data, and the existing external-egress
    approval keeps binding the exact outbound bytes.
    """

    NONE = "none"
    REFERENCES = "references"
    INVALID = "invalid"


_LOCAL_READ_CONTEXT_KIND_VALUES = frozenset(
    item.value for item in LocalReadContextKind
)


def local_read_reference_refusal(value: Any) -> str | None:
    """Why this is not a usable local-read source reference, or ``None``.

    THE single grammar for one plural reference, applied at the input
    boundary, at the persistence load boundary and again by the resolver.
    Nothing is coerced or canonicalized: a padded reference is REFUSED, not
    stripped, so the identity that was approved is the identity that was
    typed. Deliberately independent of the M7 write-reference grammar; the
    two are equal today and must stay free to diverge.
    """
    if type(value) is not str:
        return "source reference is not a string"
    if not value:
        return "source reference is empty"
    if chr(0) in value:
        return "source reference contains a NUL character"
    if value != value.strip():
        return "source reference carries surrounding whitespace"
    if len(value) > LOCAL_READ_SOURCE_MAX_CHARS:
        return "source reference exceeds its character ceiling"
    return None


def local_read_context_refusal(kind: Any, ids: Any, sources: Any) -> str | None:
    """Why this (kind, references, frozen bundle) triple is not usable.

    THE single stage-INDEPENDENT contract for the M8 read context, asked by
    ``Task.from_dict``, by ``AgentCore`` at every stage that builds authority,
    and by the Claude egress boundary. Exact types only - a raw
    ``"references"`` string is not this contract's kind, and a list is not
    this contract's tuple.

    ``NONE`` must carry NO state at all. That clause is what stops a tampered
    row whose kind key was deleted from loading as an ordinary no-context
    task while it still holds references and excerpts: the kind defaults to
    ``NONE``, the leftover state is seen here, and the triple is refused.
    Only the COMPLETE absence of M8 state is a legacy row.

    ``REFERENCES`` accepts BOTH the pre-resolution shape (no frozen sources
    yet) and the post-resolution shape (exactly one snapshot per reference).
    Which of the two is correct right now depends on the loop stage, which
    this function deliberately does not know; ``AgentCore`` decides that.

    One malformed frozen snapshot refuses the WHOLE bundle. It is never
    dropped: a silently shortened bundle would send less than what was
    approved, which is exactly the partial send M8 forbids.
    """
    if type(kind) is not LocalReadContextKind:
        return "read context kind is not an exact LocalReadContextKind"
    if kind is LocalReadContextKind.INVALID:
        return "read context identity is invalid"
    if type(ids) is not tuple:
        return "source references are not a tuple"
    if type(sources) is not tuple:
        return "frozen sources are not a tuple"
    if kind is LocalReadContextKind.NONE:
        if ids:
            return "a task with no read context carries source references"
        if sources:
            return "a task with no read context carries frozen sources"
        return None
    if not ids:
        return "a read context names no source"
    if len(ids) > LOCAL_READ_MAX_SOURCES:
        return "read context exceeds its source ceiling"
    for reference in ids:
        reason = local_read_reference_refusal(reference)
        if reason is not None:
            return f"source reference is malformed: {reason}"
    if len(set(ids)) != len(ids):
        return "read context names the same source twice"
    if len(sources) not in (0, len(ids)):
        return "frozen sources do not match the source references"
    for snapshot in sources:
        reason = local_read_snapshot_refusal(snapshot)
        if reason is not None:
            return f"a frozen source is malformed: {reason}"
    total = sum(len(snapshot.excerpt) for snapshot in sources)
    if total > LOCAL_READ_CONTEXT_MAX_CHARS:
        return "frozen sources exceed the bundle character ceiling"
    return None


# --------------------------------------------------------------------------
# M9. Bounded region read (durable read-window identity)
# --------------------------------------------------------------------------

#: The task section naming a bounded LINE REGION of the read target. A
#: TOP-LEVEL section, not a ``context`` key, for the same reason ``write``
#: is: the Claude connector serializes ``context`` verbatim, so parameters
#: kept there would egress with every request. The decoded values live in
#: typed ``Task`` fields instead.
LOCAL_READ_REGION_SECTION = "read"


class LocalReadKind(str, Enum):
    """Durable identity of WHICH WINDOW of a file a read task asks for.

    Internal persisted state, never user-supplied authority: the input
    boundary derives it from the validated ``read`` shape and nothing else
    ever sets it. It exists so that a task created as a bounded REGION read
    can never silently become a HEAD read merely because its persisted
    parameters were corrupted - which would not just lose the window, it
    would MISLABEL the result, handing back the first lines of a file under
    metadata claiming lines 1900 onward.

    A malformed or inconsistent identity loads as ``INVALID`` - a typed
    verdict the connector refuses - rather than as either active kind.
    ``INVALID`` is never produced by the input boundary; it is accepted on
    load only so that load -> resave -> load stays invalid.

    The identity carries no filesystem authority of its own: a region read is
    still LOCAL_READ, still bounded by the same allowed roots, and still
    subject to every existing containment, symlink, hardlink, ADS and
    secret-file refusal.
    """

    HEAD = "head"
    REGION = "region"
    INVALID = "invalid"


_LOCAL_READ_KIND_VALUES = frozenset(item.value for item in LocalReadKind)


def local_read_region_refusal(kind: Any, start_line: Any, max_lines: Any) -> str | None:
    """Why this (kind, start_line, max_lines) triple is not a usable window.

    THE single contract for the read window, asked by the input boundary, by
    ``Task.from_dict`` and by the connector before a descriptor is opened.
    Exact types only - ``type() is int`` and not ``isinstance``, because
    ``True`` is an ``int`` and a boolean must never be read as a line number.

    ``HEAD`` must carry NO parameters at all. That clause is what stops a
    tampered row whose kind key was deleted from loading as an ordinary head
    read while still holding a window: the kind defaults to ``HEAD``, the
    leftover parameters are seen here, and the triple is refused. Only the
    COMPLETE absence of M9 state is a legacy row.
    """
    if type(kind) is not LocalReadKind:
        return "read kind is not an exact LocalReadKind"
    if kind is LocalReadKind.INVALID:
        return "read window identity is invalid"
    if type(start_line) is not int or type(max_lines) is not int:
        return "read window bounds are not exact integers"
    if kind is LocalReadKind.HEAD:
        if start_line != 0 or max_lines != 0:
            return "a head read carries a region window"
        return None
    if not 1 <= start_line <= LOCAL_READ_MAX_START_LINE:
        return "region start line is outside its ceiling"
    if not 1 <= max_lines <= LOCAL_READ_MAX_REGION_LINES:
        return "region line count is outside its ceiling"
    return None


# --------------------------------------------------------------------------
# M11. Sequential task chain (inert declaration, never authority)
# --------------------------------------------------------------------------

#: Hard ceiling on how many steps ONE chain may declare. Deliberately a
#: SEPARATE policy surface from ``PLAN_STEP_COUNT`` (six advisory prose steps
#: that describe ONE action) and from ``LOCAL_READ_MAX_SOURCES`` (how many
#: completed reads ONE task may consume): the three answer different
#: questions and must stay free to diverge, so none may alias another.
MAX_CHAIN_STEPS = 3

#: Hard ceiling on one persisted chain identifier or step task id.
#: Deliberately a SEPARATE policy surface from ``LOCAL_READ_SOURCE_MAX_CHARS``
#: and ``LOCAL_WRITE_SOURCE_MAX_CHARS``: all three are equal today and must
#: stay free to diverge, so none may alias another.
CHAIN_ID_MAX_CHARS = 64

#: Hard ceiling on one declared symbolic name (capability, action, write
#: mode). Separate policy surface from ``PLAN_SYMBOL_MAX_CHARS``.
CHAIN_SYMBOL_MAX_CHARS = 64

#: Hard ceiling on one declared locator (target, destination). Separate
#: policy surface from ``LOCAL_READ_PATH_MAX_CHARS``, which bounds a
#: PERSISTED ROOT-RELATIVE result path rather than a DECLARED request.
CHAIN_LOCATOR_MAX_CHARS = 512

#: Hard ceiling on one declared step objective. The ordinary task boundary
#: bounds an objective only by the whole-document byte limit, so there is no
#: existing per-field rule to reuse and this is a NEW policy surface rather
#: than an alias of one. It is deliberately its own number: a step objective
#: is USER INTENT that becomes part of the outbound payload for a model step,
#: so bounding it bounds egress, and it must stay free to diverge from
#: ``PLAN_STEP_MAX_CHARS`` (display prose) and ``PROJECT_MEMORY_TEXT_CAP``
#: (stored reference text), which answer entirely different questions.
CHAIN_OBJECTIVE_MAX_CHARS = 480

#: Which write modes a CHAIN step may declare. Deliberately a SEPARATE policy
#: surface from ``LOCAL_WRITE_ENABLED_MODES``: an ordinary standalone task may
#: still overwrite, and M10 is untouched. A chain may not, because a chain
#: step carries no literal content - by design - and an overwrite requires
#: direct bytes: ``write.model_answer_task_id`` is refused for overwrite, so a
#: declared chain overwrite could never be materialized into a real task. It
#: is refused HERE, at declaration, rather than accepted and then found
#: unrunnable much later.
CHAIN_ENABLED_WRITE_MODES = ("create",)

#: The ``consumes`` value meaning "this step consumes no earlier step". A
#: real predecessor index is always >= 0, so a sentinel outside that range
#: can never be confused with step 0.
CHAIN_NO_PREDECESSOR = -1

#: The only keys one declared chain step may carry, at the trusted input
#: boundary and at the persistence load boundary alike. Default-deny, and
#: the ABSENCES are the contract: no ``task_id``, no ``local_read_task_id``,
#: no ``model_answer_task_id``, no ``steps``, no ``chain`` and - deliberately
#: - no ``classification``. A predecessor is named by INDEX and by nothing
#: else; a model answer, a retrieved memory item, a nested chain and a data
#: classification have no spelling here at all, so none of them can author a
#: step. Classification in particular is POLICY, not intent: letting a caller
#: declare it would let a declaration claim PUBLIC for a step whose real
#: scope is SECRET, and the trusted core must derive it instead - exactly as
#: ``AgentCore`` already binds a read snapshot's classification from the
#: reading task's own scope rather than from what the producer claimed.
_CHAIN_STEP_KEYS = frozenset(
    {
        "objective",
        "capability",
        "action",
        "target",
        "destination",
        "write_mode",
        "consumes",
    }
)

#: The only keys one persisted chain may carry.
_CHAIN_STATE_KEYS = frozenset({"id", "steps", "step_task_ids", "cursor", "status"})


class ChainStatus(str, Enum):
    """Durable status of the ONE sequential chain a session may own.

    Internal persisted state, never user-supplied authority: a caller
    declares steps, never a status. ``ACTIVE`` means the chain still has work
    at its cursor; ``COMPLETED`` means every declared step produced a task and
    the last one finished; ``FAILED`` and ``CANCELLED`` are the terminal
    outcomes of a chain that stopped early, and neither implies any undo -
    completed steps stay completed, because no rollback exists.

    A malformed or inconsistent chain loads as ``INVALID`` - a typed verdict
    every later authority stage refuses - rather than as an active chain or as
    no chain at all. ``INVALID`` is never produced by the input boundary; it is
    accepted on load only so that load -> resave -> load stays invalid.
    Degrading a corrupted chain to ABSENCE would be the more dangerous repair:
    absence reads as "this session may start a chain", so a tampered
    declaration would quietly clear the way for a new one.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALID = "invalid"


def chain_symbol_refusal(value: Any) -> str | None:
    """Why this is not a usable declared symbol, or ``None``.

    THE single grammar for a chain-declared symbolic name. Nothing is coerced
    or canonicalized: a padded name is REFUSED, not stripped, so the
    declaration that will be shown at approval is the declaration that was
    typed. Deliberately independent of ``normalize_symbol``, which exists to
    keep a fingerprint stable, not to accept sloppy input here.
    """
    if type(value) is not str:
        return "symbol is not a string"
    if not value:
        return "symbol is empty"
    if chr(0) in value:
        return "symbol contains a NUL character"
    if value != value.strip():
        return "symbol carries surrounding whitespace"
    if len(value) > CHAIN_SYMBOL_MAX_CHARS:
        return "symbol exceeds its character ceiling"
    return None


def chain_locator_refusal(value: Any) -> str | None:
    """Why this is not a usable declared locator, or ``None``.

    Empty is allowed and meaningful: a step that names no filesystem target or
    no outbound destination declares the empty string, exactly as
    ``ActionScope`` does. Everything else is the symbol grammar with a wider
    ceiling. GRAMMAR only - containment, allowed roots, the secret-file policy
    and the whole no-follow chain are asked far later, by the connector that
    would actually open something.
    """
    if type(value) is not str:
        return "locator is not a string"
    if chr(0) in value:
        return "locator contains a NUL character"
    if value != value.strip():
        return "locator carries surrounding whitespace"
    if len(value) > CHAIN_LOCATOR_MAX_CHARS:
        return "locator exceeds its character ceiling"
    return None


def chain_objective_refusal(value: Any) -> str | None:
    """Why this is not a usable declared objective, or ``None``.

    Deliberately NOT the symbol grammar. An objective is USER INTENT in prose,
    and it is the one declared field whose text a person wrote to be read, so
    surrounding whitespace is PRESERVED rather than refused or stripped -
    exactly as the ordinary task boundary preserves it. Blank is still
    refused, because a task with no objective is refused by the loop itself.

    Nothing is normalized: the objective a chain step declares is the
    objective a later task will carry and, for a model step, the objective
    that reaches the outbound payload verbatim. Quietly rewriting it would
    change what the user asked for.
    """
    if type(value) is not str:
        return "objective is not a string"
    if not value.strip():
        return "objective is blank"
    if chr(0) in value:
        return "objective contains a NUL character"
    if len(value) > CHAIN_OBJECTIVE_MAX_CHARS:
        return "objective exceeds its character ceiling"
    return None


def chain_id_refusal(value: Any) -> str | None:
    """Why this is not a usable chain or step-task identifier, or ``None``."""
    if type(value) is not str:
        return "identifier is not a string"
    if not value:
        return "identifier is empty"
    if chr(0) in value:
        return "identifier contains a NUL character"
    if value != value.strip():
        return "identifier carries surrounding whitespace"
    if len(value) > CHAIN_ID_MAX_CHARS:
        return "identifier exceeds its character ceiling"
    return None


@dataclass(frozen=True, slots=True)
class ChainStep:
    """One ordered DECLARATION of a future ordinary single-action task.

    Inert data with NO authority: a step is not an approval, not a scope, not
    an execution opportunity and not a billing input. It describes what one
    later ordinary ``Task`` would ask for, and every gate that task meets -
    its own permission request, its own ``EgressAttestation``, its own
    execution attempt, its own reservation, its own result - stays exactly as
    it is today, unshared with any other step.

    ``consumes`` names a predecessor BY INDEX, never by task id. An index
    cannot address a task in another session, cannot address an unrelated
    completed row, and cannot be forged into a reference the existing
    resolvers would accept - the three things a caller-supplied task id could
    attempt. The concrete id of a predecessor is not even knowable when the
    chain is declared, because that task does not exist yet; it is discovered
    later, from storage, and the existing resolvers still prove it.

    A step expresses INTENT AND ROUTING ONLY, plus the one thing intent cannot
    be derived from: its ``objective``. That field is the user's own words and
    is deliberately caller-controlled, because it is INTENT, not policy - and
    because a later task requires one, which no amount of capability/action/
    target could honestly synthesize. For a model step the objective IS the
    request and reaches the outbound payload verbatim, so it is bounded and
    never rewritten. Model output, retrieved memory and file excerpts still
    cannot author or mutate it: it arrives only through the trusted boundary.

    ``classification`` is NOT declarable: data classification is policy the
    trusted core owns. A declaration that could name a classification could
    name PUBLIC for a SECRET target, and the digest below would then have a
    caller-controlled policy surface in it. Whatever creates the ordinary task
    for a step must set the classification EXPLICITLY on the trusted side -
    for M11 that is ``DataClassification.INTERNAL``, matching what the
    ordinary task boundary already applies when the field is absent - and must
    never derive it from the objective text, from model output, from the
    target string or from a predecessor's content. SECRET-classified chain
    execution is deferred until a genuine trusted derivation policy exists.
    """

    objective: str = ""
    capability: str = ""
    action: str = ""
    target: str = ""
    destination: str = ""
    #: Empty for every non-write step; an exact ``LOCAL_WRITE_MODES`` value for
    #: a write step. GRAMMAR only - whether the running platform and policy may
    #: PERFORM the mode stays ``local_write_mode_unsupported``'s question,
    #: asked far later and by someone else.
    write_mode: str = ""
    consumes: int = CHAIN_NO_PREDECESSOR

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "capability": self.capability,
            "action": self.action,
            "target": self.target,
            "destination": self.destination,
            "write_mode": self.write_mode,
            "consumes": self.consumes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ChainStep":
        """Rebuild defensively; anything malformed degrades to the empty step.

        No repair and no coercion of any kind: ``str()`` on a capability,
        ``int()`` on a predecessor index or ``bool()`` on anything would each
        turn tampered persisted state into a plausible ACTIVE declaration. An
        unknown key is REFUSED rather than filtered, because a key this
        contract does not know is either a spelling from a future contract or
        an attempt to smuggle authority - a task id, a model answer, a nested
        chain - and neither may be silently dropped into a step that then
        looks well-formed.

        The empty step returned here carries an empty capability, which the one
        canonical contract below always refuses. That is deliberate: one
        malformed step must refuse the WHOLE chain, never be dropped from it.
        """
        if not isinstance(data, dict):
            return cls()
        if not set(data) <= _CHAIN_STEP_KEYS:
            return cls()
        # Every field is read exactly ONCE here, so this object is a snapshot
        # of the mapping at one instant. An OMITTED key falls back to its
        # typed default; a PRESENT value of the wrong type is tampered state
        # and is judged - never coerced - by the one contract below.
        return cls(
            objective=data.get("objective", ""),
            capability=data.get("capability", ""),
            action=data.get("action", ""),
            target=data.get("target", ""),
            destination=data.get("destination", ""),
            write_mode=data.get("write_mode", ""),
            consumes=data.get("consumes", CHAIN_NO_PREDECESSOR),
        )


def _canonical_step_scope_field(value: Any, *, destination: bool = False) -> Any:
    """One declared scope field in the EXACT spelling persistence will hold.

    B1. ``ActionScope.to_dict`` normalizes before writing, so the row never
    holds what the caller typed - it holds the normalized form. A declaration
    that froze the RAW spelling therefore accepted a proposal whose own stored
    representation could no longer prove it belonged to the step: the task was
    bound and then permanently failed STORED authority. The declaration is
    frozen in the persisted spelling instead, so the round trip is the
    identity and the digest is over the representation that actually exists.

    The SAME normalizers ``ActionScope`` uses, never a second spelling of
    them. Applied only to a real ``str``: a non-string is tampered state and
    stays untouched so the grammar below still refuses it by type, and
    ``str()`` here would be exactly the coercion this contract forbids.
    """
    if type(value) is not str:
        return value
    return normalize_destination(value) if destination else normalize_symbol(value)


def _canonical_step_target(value: Any) -> Any:
    """A declared target in its persisted spelling, WITHOUT resolving a path.

    A symbolic target converges here, because that is all
    ``normalize_locator`` does to it. A PATH-SHAPED target deliberately does
    not: ``normalize_locator`` would resolve it against the current working
    directory, so the declaration - and its digest - would carry an absolute
    machine path and would differ between machines and between runs. That
    rewrite belongs to the trusted runtime, which performs it later, and
    STORED already sanctions exactly that one canonical form for exactly the
    capabilities whose lifecycle produces it.
    """
    if type(value) is not str or looks_like_path(value):
        return value
    return normalize_symbol(value)


def _canonical_step(step: ChainStep) -> ChainStep:
    """An ACCEPTED step, respelled exactly as persistence will hold it.

    Called only after ``chain_steps_refusal`` has already accepted the step,
    so this can never repair a value the grammar refuses.
    """
    return replace(
        step,
        action=_canonical_step_scope_field(step.action),
        target=_canonical_step_target(step.target),
        destination=_canonical_step_scope_field(step.destination, destination=True),
    )


def chain_step_refusal(step: Any, index: Any, total: Any) -> str | None:
    """Why this step is not usable AT THIS POSITION, or ``None``.

    THE single contract for one declared step, asked by the trusted input
    boundary, by the persistence load boundary and by the whole-chain contract
    below. Position is a parameter because two of the rules are positional and
    cannot be judged by a step alone: a step may not consume ITSELF, and a step
    may not consume a LATER step. A sequential chain that could look forward
    would be declaring a dependency no execution order can satisfy.

    Exact types only - ``type() is int`` and not ``isinstance``, because
    ``True`` is an ``int`` and a boolean must never be read as a step index.
    """
    if type(step) is not ChainStep:
        return "step is not an exact ChainStep"
    if type(index) is not int or type(total) is not int:
        return "step position is not an exact integer"
    if not 0 <= index < total:
        return "step position is outside the chain"
    reason = chain_objective_refusal(step.objective)
    if reason is not None:
        return f"objective is malformed: {reason}"
    reason = chain_symbol_refusal(step.capability)
    if reason is not None:
        return f"capability is malformed: {reason}"
    reason = chain_symbol_refusal(step.action)
    if reason is not None:
        return f"action is malformed: {reason}"
    reason = chain_locator_refusal(step.target)
    if reason is not None:
        return f"target is malformed: {reason}"
    reason = chain_locator_refusal(step.destination)
    if reason is not None:
        return f"destination is malformed: {reason}"
    # Exact type FIRST, ungated by truthiness. ``""`` is the legitimate
    # non-write default; every OTHER falsy value - ``None``, ``0``, ``False``,
    # ``[]``, ``b""`` - is tampered state wearing the default's clothes. A
    # truthiness gate would admit all of them: the declared ``str`` type would
    # be violated in a chain this contract calls valid, one displayed
    # declaration would have several distinct digests, and a non-encodable
    # value would leave a chain whose digest cannot be computed at all.
    if type(step.write_mode) is not str:
        return "write mode is not an exact string"
    if step.write_mode:
        reason = local_write_mode_refusal(step.write_mode)
        if reason is not None:
            return f"write mode is malformed: {reason}"
        if step.write_mode not in CHAIN_ENABLED_WRITE_MODES:
            # Grammar recognizes the mode; CHAIN policy refuses it. Standalone
            # M10 overwrite is untouched - this is only about what a chain,
            # which declares no bytes, could ever actually perform.
            return "a chain step may not declare this write mode"
        if step.capability != LOCAL_WRITE_CAPABILITY:
            return "a step declares a write mode without the write capability"
    elif step.capability == LOCAL_WRITE_CAPABILITY:
        return "a write step declares no write mode"
    if type(step.consumes) is not int:
        return "predecessor is not an exact integer"
    if step.consumes == CHAIN_NO_PREDECESSOR:
        return None
    if step.consumes < 0:
        return "predecessor is not a real step index"
    if step.consumes == index:
        return "a step consumes itself"
    if step.consumes > index:
        return "a step consumes a later step"
    return None


#: The ``step_index`` value meaning "this task was not created for a chain".
#: A real index is always >= 0, so the sentinel can never be step 0.
CHAIN_NO_STEP = -1

#: Hard ceiling on a persisted chain digest string. A SHA-256 hex digest is
#: exactly this long; the constant exists so the check is a stated policy
#: rather than a magic number, and so it cannot drift from the digest that
#: produces it.
CHAIN_DIGEST_CHARS = 64


@dataclass(frozen=True, slots=True)
class ChainBinding:
    """Trusted provenance proving a Task was created FOR one chain step.

    Internal state only. It is deliberately absent from the task input
    allowlist, so untrusted task JSON naming it is refused BY NAME and no
    caller can mint a task that claims to belong to a chain. Only the trusted
    derivation that materializes a step sets it.

    It is a CLAIM that must still be checked, never a permission. A consumer
    proves chain ownership by re-asking storage: the task's session must be
    the chain's session, this binding must name the chain's id, the chain's
    CURRENT declaration digest and the step index whose slot holds this task's
    id, and the task's own capability and scope must still match the frozen
    step. A forged ordinary task fails every one of those, and a genuine
    chain task whose declaration was edited afterwards fails the digest.
    """

    chain_id: str = ""
    chain_digest: str = ""
    step_index: int = CHAIN_NO_STEP

    @property
    def present(self) -> bool:
        return bool(self.chain_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "chain_digest": self.chain_digest,
            "step_index": self.step_index,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ChainBinding":
        """Rebuild defensively; anything malformed loads as NO binding.

        Absence is the safe degradation here, and only here: a task with no
        binding simply cannot prove chain ownership, so every chain consumer
        refuses it. Nothing is coerced - a tampered binding does not become a
        plausible one wearing a valid shape.
        """
        if not isinstance(data, dict):
            return cls()
        candidate = cls(
            chain_id=data.get("chain_id", ""),
            chain_digest=data.get("chain_digest", ""),
            step_index=data.get("step_index", CHAIN_NO_STEP),
        )
        return candidate if chain_binding_refusal(candidate) is None else cls()


def chain_binding_refusal(binding: Any) -> str | None:
    """Why this is not a usable chain binding, or ``None``.

    An ABSENT binding is usable: most tasks are not chain steps, and the empty
    binding is how they say so. A PARTIAL one never is - a binding that names
    a chain but no digest, or a digest but no chain, is tampered state.
    """
    if type(binding) is not ChainBinding:
        return "binding is not an exact ChainBinding"
    if type(binding.step_index) is not int:
        return "step index is not an exact integer"
    if type(binding.chain_id) is not str or type(binding.chain_digest) is not str:
        return "binding identifiers are not strings"
    if not binding.present:
        if binding.chain_digest or binding.step_index != CHAIN_NO_STEP:
            return "an unbound task carries chain state"
        return None
    reason = chain_id_refusal(binding.chain_id)
    if reason is not None:
        return f"chain id is malformed: {reason}"
    if len(binding.chain_digest) != CHAIN_DIGEST_CHARS:
        return "chain digest is not a digest"
    if any(character not in "0123456789abcdef" for character in binding.chain_digest):
        return "chain digest is not lowercase hexadecimal"
    if not 0 <= binding.step_index < MAX_CHAIN_STEPS:
        return "step index is outside the chain"
    return None


#: Loop stages during which a chain step task may not yet name a selected
#: connector. Selection HAPPENS AT PLAN, and the successful choice is
#: persisted only as the task leaves PLAN for RISK_PERMISSION - so a task
#: whose stored stage is still PLAN has not selected anything yet, and PLAN
#: belongs in this set. Storage cannot prove WHICH connector is legitimate;
#: that needs the registry, which is AgentCore's. It can prove that a task
#: which has not got past PLAN has no business naming one at all.
_PRE_SELECTION_STAGES = frozenset(
    {
        LoopStage.UNDERSTAND,
        LoopStage.CONTEXT_RETRIEVAL,
        LoopStage.CLARIFY_OR_ASSUME,
        LoopStage.PLAN,
    }
)


class ChainTaskMode(str, Enum):
    """Which chain-task question is being asked.

    ``PROPOSAL``: a task that has not been persisted yet, offered for
    materialization. It must be PRISTINE - the exact shape trusted derivation
    produces and nothing more - because after this moment its identity is
    fixed and nobody re-derives it.

    ``STORED``: a task that already exists and may have progressed through the
    ordinary loop. Its chain-derived AUTHORITY must still be untouched, while
    its ordinary lifecycle state is allowed to have moved on.
    """

    PROPOSAL = "proposal"
    STORED = "stored"


def chain_task_refusal(
    task: Any, chain: Any, index: Any, *, mode: Any
) -> str | None:
    """Why this task is NOT the chain's task for this step, or ``None``.

    THE single ownership contract. A valid ``ChainBinding`` is NECESSARY but
    never SUFFICIENT: a binding is a claim, and this function is where the
    claim is checked against the frozen declaration, the authoritative chain
    mapping and trusted M11 policy. What it proves is one sentence - this
    stored task is exactly the ordinary task the frozen step plus trusted
    derivation plus authoritative predecessor routing would produce - so a
    task cannot keep chain authority after ANY material change to its user
    intent, its scope, its policy or its sanctioned source routing.

    A. IMMUTABLE CHAIN-DERIVED AUTHORITY - checked in BOTH modes, because
    changing any of them changes what the user asked for or what the task is
    allowed to do:
        the binding (chain id, CURRENT declaration digest, step index);
        ``objective`` verbatim; ``capability``; ``scope.action``,
        ``scope.target``, ``scope.destination``;
        ``scope.data_classification`` == INTERNAL;
        ``write_mode``; the M7/M8 predecessor routing fields
        (``write_source``, ``write_source_task_id``,
        ``local_read_context_kind``, ``local_read_source_task_ids``);
        the absence of the M3 singular reference; the HEAD-only read window;
        ``project_id``.

    B. LEGITIMATE ORDINARY-TASK RUNTIME PROGRESSION - pinned pristine in
    PROPOSAL mode and then left alone, because the ordinary loop owns them:
        ``status``, ``stage``, ``plan``, ``estimate``, ``preferences``,
        ``visible_assumption``, ``result_snapshot``, ``retrieved_context``,
        ``local_read``, ``local_read_sources``, ``local_write``,
        ``model_answer``, ``write_content`` (frozen by the M7 resolver),
        ``progress``, ``cancellation_requested``, ``selected_connector``.

    ``selected_connector`` is the one field in group B that still gets a rule
    in STORED mode, and only the rule storage can honestly enforce: a task
    that has not reached PLAN may not name one. WHICH connector is legitimate
    is a registry question and stays in ``AgentCore``; duplicating that
    authority into storage would create a second source of truth for it.
    """
    if type(mode) is not ChainTaskMode:
        return "chain task mode is not an exact ChainTaskMode"
    if type(task) is not Task:
        return "task is not an exact Task"
    if type(chain) is not ChainState:
        return "chain is not an exact ChainState"
    if chain_state_refusal(chain) is not None:
        return "chain is not usable"
    if type(index) is not int or not 0 <= index < len(chain.steps):
        return "step index is outside the chain"
    step = chain.steps[index]

    binding = task.chain_binding
    if chain_binding_refusal(binding) is not None:
        return "task binding is malformed"
    if not binding.present:
        return "task carries no chain binding"
    if binding.chain_id != chain.id:
        return "task belongs to another chain"
    if binding.chain_digest != chain.digest:
        return "chain declaration changed after the task was created"
    if binding.step_index != index:
        return "task belongs to another step of this chain"

    if task.objective != step.objective:
        return "task objective does not match the declared step"
    if task.capability != step.capability:
        return "task capability does not match the declared step"
    if type(task.scope) is not ActionScope:
        return "task scope is not an exact ActionScope"
    if task.scope.action != step.action:
        return "task action does not match the declared step"
    if task.scope.target != step.target and not (
        mode is ChainTaskMode.STORED
        and step.capability in CANONICAL_TARGET_CAPABILITIES
        and task.scope.target == canonical_path(step.target)
    ):
        # The ONE transformation the ordinary loop performs on a target, and
        # only where it actually performs it. ``_bind_canonical_local_target``
        # canonicalizes the target of a ``CANONICAL_TARGET_CAPABILITIES``
        # task at the top of the loop, so such a task, once it has actually
        # RUN, legitimately carries the canonical spelling of what the step
        # declared. Exactly that value is accepted, for exactly those
        # capabilities, in STORED mode, and nothing else is.
        #
        # S3B P1-2. Allowing the canonical alternative for EVERY capability
        # granted target-transformation authority the trusted lifecycle never
        # grants: no ordinary run rewrites a model step's symbolic target, so
        # a stored model task whose target had become
        # ``canonical_path(target)`` was a task nothing sanctioned produced,
        # and it kept chain authority anyway. It is gated on the shared
        # predicate now, so the validator and the runtime cannot disagree
        # about which capabilities are transformed. A PROPOSAL has not run at
        # all, so it must still carry the declared spelling verbatim,
        # whatever its capability.
        return "task target does not match the declared step"
    if task.scope.destination != step.destination:
        return "task destination does not match the declared step"
    if task.scope.connector != "":
        # FROZEN SCOPE AUTHORITY, and deliberately not the same thing as
        # ``selected_connector``. Trusted derivation leaves this empty, so
        # the scope - and therefore the approval fingerprint built from it -
        # names no connector. A chain task may never acquire authority under
        # an arbitrary one. Which connector RUNS the task is the separate,
        # later, registry-owned question ``selected_connector`` answers.
        return "task scope names a connector"
    if task.scope.data_classification is not DataClassification.INTERNAL:
        return "task classification is not the trusted chain classification"
    if task.project_id is not None:
        return "a chain task carries a project binding"

    expected_mode = step.write_mode if step.capability == LOCAL_WRITE_CAPABILITY else ""
    if expected_mode and expected_mode not in CHAIN_ENABLED_WRITE_MODES:
        return "the declared step names a write mode a chain may not perform"
    if task.write_mode != expected_mode:
        return "task write mode does not match the declared step"

    # A chain declares no region window, so a task carrying one was not
    # derived from this declaration. Checked by EXACT TYPE and EXACT VALUE:
    # ``type() is int`` and not ``isinstance``, because ``bool`` is an ``int``
    # subclass and ``False`` would otherwise read as the canonical 0 - and a
    # truthiness test would admit ``False``, ``0.0``, ``""`` and ``None``
    # alike, each of which is a different fact from "no window".
    if task.local_read_kind is not LocalReadKind.HEAD:
        return "a chain task carries a read window"
    if type(task.read_start_line) is not int or task.read_start_line != 0:
        return "a chain task carries a read window start line"
    if type(task.read_max_lines) is not int or task.read_max_lines != 0:
        return "a chain task carries a read window line count"
    # A4. SHAPE BEFORE MEMBERSHIP. ``in`` is not a safe question to ask of an
    # unproven value: a persisted ``"context": null`` rebuilds as ``None`` -
    # nothing coerces it - and the membership test below would raise
    # ``TypeError`` out of a validator whose entire contract is to RETURN a
    # refusal. A raise here escapes into materialization and the driver, where
    # a deterministic refusal was the only thing keeping the boundary closed.
    # Exact type, so a ``dict`` subclass with a stateful ``__contains__``
    # cannot answer this question either.
    if type(task.context) is not dict:
        return "task context is not an exact mapping"
    if LOCAL_READ_TASK_REFERENCE_KEY in task.context:
        return "a chain task carries an unsanctioned read reference"

    reason = _chain_routing_refusal(task, chain, step)
    if reason is not None:
        return reason

    if mode is ChainTaskMode.STORED:
        if task.selected_connector is not None and (
            type(task.selected_connector) is not str
            or task.selected_connector == ""
        ):
            return "task names a malformed connector"
        if (
            task.stage in _PRE_SELECTION_STAGES
            and task.selected_connector is not None
        ):
            return "task named a connector before it was planned"
        return None

    # PROPOSAL: pristine, exactly what trusted derivation produces.
    if task.status is not TaskStatus.PENDING or task.stage is not LoopStage.UNDERSTAND:
        return "a proposed chain task is not at the start of the loop"
    if task.selected_connector is not None:
        return "a proposed chain task already names a connector"
    if task.result_snapshot is not None:
        return "a proposed chain task already carries a result"
    # Every remaining POLICY and RUNTIME field trusted derivation leaves at
    # its canonical default, checked by EXACT TYPE and EXACT VALUE.
    #
    # Truthiness and ``.present`` are deliberately NOT used here. An
    # empty-LOOKING value is not the same fact as the canonical default,
    # and this is the last boundary before a task identity is fixed:
    # ``LocalReadSnapshot(classification=PUBLIC)`` is not ``present`` and is
    # not the default either; ``LocalWriteRecord(pre_present=True)`` and
    # ``ModelAnswer(content=...)`` are the same trick; ``plan=()`` and
    # ``context=[]`` are falsy and are not the declared types;
    # ``cancellation_requested=0`` is falsy and is not ``False``.
    #
    # Pinned HERE rather than left to downstream recomputation: a boundary
    # that admits hostile state and hopes a later stage overwrites it is not
    # a boundary. A proposal must BE the trusted initial state, and the only
    # fields allowed to differ are the ones construction varies by nature -
    # ``id``, ``created_at`` and ``updated_at``.
    if type(task.write_content) is not str or task.write_content != "":
        return "a proposed chain task already carries write content"
    if type(task.plan) is not list or task.plan != []:
        return "a proposed chain task already carries a plan"
    if task.visible_assumption is not None:
        return "a proposed chain task already carries an assumption"
    if type(task.context) is not dict or task.context != {}:
        return "a proposed chain task carries context"
    if type(task.preferences) is not dict or task.preferences != {}:
        return "a proposed chain task carries preferences"
    if (
        type(task.local_read) is not LocalReadSnapshot
        or task.local_read != LocalReadSnapshot()
    ):
        return "a proposed chain task already carries a read snapshot"
    if (
        type(task.local_write) is not LocalWriteRecord
        or task.local_write != LocalWriteRecord()
    ):
        return "a proposed chain task already carries a write record"
    if (
        type(task.model_answer) is not ModelAnswer
        or task.model_answer != ModelAnswer()
    ):
        return "a proposed chain task already carries a model answer"
    if type(task.local_read_sources) is not tuple or task.local_read_sources != ():
        return "a proposed chain task already carries a frozen bundle"
    if task.cancellation_requested is not False:
        return "a proposed chain task is already cancelled"
    if task.risk_level is not RiskLevel.LOW:
        return "a proposed chain task carries a risk level"
    if (
        type(task.side_effect) is not SideEffectProfile
        or task.side_effect != SideEffectProfile()
    ):
        return "a proposed chain task carries a side-effect profile"
    if type(task.estimate) is not CostEstimate or task.estimate != CostEstimate():
        return "a proposed chain task carries a cost estimate"
    if (
        type(task.retrieved_context) is not RetrievedContext
        or task.retrieved_context != RetrievedContext()
    ):
        return "a proposed chain task carries retrieved context"
    if type(task.progress) is not TaskProgress or task.progress != TaskProgress():
        return "a proposed chain task carries progress"
    return None


def _chain_routing_refusal(task: Task, chain: ChainState, step: ChainStep) -> str | None:
    """Whether the task's source routing is EXACTLY what this step sanctions.

    The predecessor id is taken from the chain's own authoritative mapping and
    from nowhere else, so a task naming any other source - a transitive one, a
    caller-chosen one, an extra one alongside the right one - is refused. Each
    aperture also excludes the other: a model consumer may not carry a write
    source, and a create consumer may not carry read references.
    """
    # Every shape below is checked by EXACT TYPE and EXACT VALUE. Truthiness
    # would let a wrong-type falsy value masquerade as the canonical empty
    # default - ``write_source_task_id=0``, ``local_read_source_task_ids=[]`` -
    # and be materialized as though trusted derivation had produced it.
    if step.consumes == CHAIN_NO_PREDECESSOR:
        if task.write_source is not WriteSource.DIRECT:
            return "a step with no predecessor names a write source"
        if _not_empty_reference(task.write_source_task_id):
            return "a step with no predecessor carries a write source id"
        if task.local_read_context_kind is not LocalReadContextKind.NONE:
            return "a step with no predecessor names a read context"
        if _not_empty_references(task.local_read_source_task_ids):
            return "a step with no predecessor carries read references"
        return None
    predecessor = chain.step_task_ids[step.consumes]
    if type(predecessor) is not str or predecessor == "":
        return "the predecessor step has produced no task"
    if step.capability == LOCAL_WRITE_CAPABILITY:
        if task.write_source is not WriteSource.MODEL_ANSWER:
            return "a chain write step does not name the model-answer source"
        if (
            type(task.write_source_task_id) is not str
            or task.write_source_task_id != predecessor
        ):
            return "task write source is not the authoritative predecessor"
        if task.local_read_context_kind is not LocalReadContextKind.NONE:
            return "a chain write step names a read context"
        if _not_empty_references(task.local_read_source_task_ids):
            return "a chain write step carries read references"
        return None
    if task.local_read_context_kind is not LocalReadContextKind.REFERENCES:
        return "a chain consumer does not name a read context"
    if (
        type(task.local_read_source_task_ids) is not tuple
        or task.local_read_source_task_ids != (predecessor,)
    ):
        return "task read references are not exactly the authoritative predecessor"
    if task.write_source is not WriteSource.DIRECT:
        return "a chain read consumer names a write source"
    if _not_empty_reference(task.write_source_task_id):
        return "a chain read consumer carries a write source id"
    return None


def _not_empty_reference(value: Any) -> bool:
    """Whether this is NOT the canonical empty reference, an exact ``""``.

    ``0``, ``False``, ``None`` and ``b""`` are each falsy and each a different
    fact from "no reference", so none of them may pass as the default.
    """
    return type(value) is not str or value != ""


def _not_empty_references(value: Any) -> bool:
    """Whether this is NOT the canonical empty reference tuple, an exact ``()``.

    A list, a string or ``None`` is not an empty tuple, however falsy it looks.
    """
    return type(value) is not tuple or value != ()


def chain_steps_refusal(steps: Any) -> str | None:
    """Why this ordered step tuple is not a usable declaration, or ``None``.

    THE single grammar for a WHOLE declaration: every per-step rule, plus the
    rules no step can answer about itself. It exists so the trusted input
    boundary and the persistence load boundary ask the same question once,
    instead of each growing its own idea of what a chain may say.

    The cross-step rule is the write aperture. A chain step carries no literal
    content - deliberately, so a declaration can never be a payload - so the
    only way a chain write can obtain bytes is the existing M7 ModelAnswer ->
    create hop. A create step must therefore consume an earlier step that
    declares an outbound destination, which is the declarative shape of a step
    that can produce a model answer.

    That check is a STATIC PRE-CHECK and nothing more: it refuses early a
    declaration that could never be materialized. The AUTHORITATIVE proof that
    a predecessor really produced a model answer stays exactly where it is
    today - the existing resolver, which requires a same-session COMPLETED
    task whose selected connector's own manifest declares external egress.
    Nothing here invents a generic "consume an arbitrary task result" channel.
    """
    if type(steps) is not tuple:
        return "chain steps are not a tuple"
    if not steps:
        return "a chain declares no step"
    if len(steps) > MAX_CHAIN_STEPS:
        return "chain exceeds its step ceiling"
    total = len(steps)
    for index, step in enumerate(steps):
        reason = chain_step_refusal(step, index, total)
        if reason is not None:
            return f"chain step {index} is malformed: {reason}"
    # Every step above is now known to be an exact ChainStep whose predecessor
    # index, when present, is a real EARLIER position, so the lookup below is
    # safe and can never reach forward.
    for index, step in enumerate(steps):
        if step.capability != LOCAL_WRITE_CAPABILITY:
            continue
        if step.consumes == CHAIN_NO_PREDECESSOR:
            return (
                f"chain step {index} writes without consuming a producer step"
            )
        if not steps[step.consumes].destination:
            return (
                f"chain step {index} consumes a step that declares no "
                "outbound destination"
            )
    return None


@dataclass(frozen=True, slots=True)
class ChainState:
    """The ONE sequential chain a session may own. Inert until orchestrated.

    A chain is a DECLARATION plus a cursor plus the ids of the ordinary tasks
    already created for it. It holds no approval, no attestation, no attempt
    and no reservation of its own, and it grants none: declaring a chain causes
    nothing to happen, because every side effect still belongs to an ordinary
    single-action task that must pass every existing gate by itself.

    ``step_task_ids`` is a ROUTING record, never a proof. It says which task
    was created for which step so orchestration can find it again; it never
    says that a step SUCCEEDED. That question is answered only by the
    storage-owned task row and the execution journal, re-asked every time,
    exactly as the billing preflight marker routes while the grant proves. For
    the same reason no per-step status is persisted here: a status field would
    be a forgeable second opinion about a fact storage already owns.

    Sequential and singular by construction: one chain per session, one cursor,
    at most ``MAX_CHAIN_STEPS`` steps, no nesting and no branch.
    """

    id: str = ""
    steps: tuple[ChainStep, ...] = ()
    #: One entry per declared step, in declaration order. An empty string means
    #: "no task created for this step yet"; created ids form a LEADING RUN,
    #: because a sequential chain cannot have created a later task before an
    #: earlier one.
    step_task_ids: tuple[str, ...] = ()
    cursor: int = 0
    status: ChainStatus = ChainStatus.ACTIVE

    @classmethod
    def declared(cls, steps: Any) -> "ChainState | None":
        """A freshly declared chain, or ``None`` if these steps cannot make one.

        THE single constructor for a new chain, so the four runtime fields
        can only ever start one way: a JARVIS-generated identity, one
        uncreated marker per declared step, the cursor on step 0, and
        ``ACTIVE``. None of them is reachable from input - the id in
        particular is minted HERE, from ``uuid4``, so no caller can choose,
        collide with or replay a chain identity, and no boundary has to
        remember to strip one.

        The steps are FROZEN ONCE and then the candidate built from that exact
        tuple is judged by ``chain_state_refusal`` - the one grammar, not a
        second competing one - before it is returned. A trusted constructor
        that could hand back a state its own contract refuses would make every
        downstream ``chain_state_refusal`` call optional in practice, which is
        how a malformed chain reaches storage. Callers already validate their
        input; this is the guarantee that holds even when one forgets.

        The digest is computed here too, for the same reason: a chain whose
        canonical encoding raises is a chain no approval could ever be bound
        to, so it must not exist rather than fail later at the display.
        """
        try:
            frozen = tuple(steps)
        except TypeError:
            return None
        candidate = cls(
            id=str(uuid4()),
            steps=frozen,
            step_task_ids=("",) * len(frozen),
            cursor=0,
            status=ChainStatus.ACTIVE,
        )
        if chain_state_refusal(candidate) is not None:
            return None
        try:
            candidate.digest
        except (TypeError, ValueError):
            return None
        return candidate

    @property
    def present(self) -> bool:
        """Always ``True``: a chain OBJECT is a chain, whatever its status.

        Existence, never validity. Absence has exactly one spelling in this
        product - ``SessionState.chain is None`` - and nothing else may be
        read as it. The INVALID verdict is therefore PRESENT: a corrupted
        declaration is a chain this session already has, not a blank slate.

        This deliberately answers ``True`` for the INVALID sentinel, whose
        declaration fields are empty. Answering ``False`` there would hand the
        house's ordinary first-gate idiom - ``if chain is None or not
        chain.present`` - the exact degradation ``ChainStatus`` exists to
        prevent, letting a tampered chain read as "free to start a new one".
        Whether a present chain may be ACTED ON is a different question with a
        different answer: ``chain_state_refusal`` is the only admissible
        validity gate, and it refuses the sentinel unconditionally.
        """
        return True

    @property
    def digest(self) -> str:
        """Deterministic SHA-256 over the canonical DECLARATION encoding.

        Covers the chain identity and the ordered declaration, and nothing that
        moves while the chain runs: not the cursor, not the created task ids,
        not the status. That is precisely what makes it usable later as an
        approval-display binding - the one digest can be shown with every step,
        it does not drift as steps complete, and any edit to the declaration
        between two steps changes it.

        ``sort_keys`` orders the mapping keys and deliberately does NOT reorder
        the step LIST: for a sequential chain the order IS the contract, so a
        reordered declaration is a different identity.
        """
        encoded = json.dumps(
            {"id": self.id, "steps": [step.to_dict() for step in self.steps]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "steps": [step.to_dict() for step in self.steps],
            "step_task_ids": list(self.step_task_ids),
            "cursor": self.cursor,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ChainState":
        """Rebuild defensively; anything malformed loads as the INVALID chain.

        Default-deny on keys and exact types throughout, with no coercion and
        no partial acceptance: a chain whose cursor, status, step list or task
        id mapping cannot be read is not shortened, not repaired and not
        dropped - it becomes the one typed INVALID verdict, which every later
        authority stage refuses and which survives its own resave.
        """
        if not isinstance(data, dict):
            return _INVALID_CHAIN
        if not set(data) <= _CHAIN_STATE_KEYS:
            return _INVALID_CHAIN
        raw_status = data.get("status")
        if type(raw_status) is not str:
            return _INVALID_CHAIN
        try:
            status = ChainStatus(raw_status)
        except ValueError:
            return _INVALID_CHAIN
        raw_steps = data.get("steps")
        raw_ids = data.get("step_task_ids")
        if not isinstance(raw_steps, list) or not isinstance(raw_ids, list):
            return _INVALID_CHAIN
        candidate = cls(
            id=data.get("id", ""),
            steps=tuple(ChainStep.from_dict(item) for item in raw_steps),
            step_task_ids=tuple(raw_ids),
            cursor=data.get("cursor", 0),
            status=status,
        )
        return candidate if chain_state_refusal(candidate) is None else _INVALID_CHAIN


#: The one typed verdict a malformed persisted chain degrades to. Empty of
#: every declaration field, so it can neither be executed nor mistaken for a
#: shorter chain, and it reloads as itself.
_INVALID_CHAIN = ChainState(status=ChainStatus.INVALID)


def chain_state_refusal(chain: Any) -> str | None:
    """Why this chain is not usable, or ``None``.

    THE single stage-INDEPENDENT contract for one sequential chain, asked by
    the persistence load boundary and by every later stage that would act on a
    chain. It judges the DECLARATION, the cursor and the created-task mapping
    together, because each is meaningless alone: a cursor is only valid against
    a step count, and a task id is only valid at a position the chain could
    already have reached.

    It deliberately does not know, and must never learn, whether any step
    SUCCEEDED. That is storage's answer, re-asked at the moment it matters.
    """
    if type(chain) is not ChainState:
        return "chain is not an exact ChainState"
    if type(chain.status) is not ChainStatus:
        return "chain status is not an exact ChainStatus"
    if chain.status is ChainStatus.INVALID:
        return "chain identity is invalid"
    reason = chain_id_refusal(chain.id)
    if reason is not None:
        return f"chain id is malformed: {reason}"
    reason = chain_steps_refusal(chain.steps)
    if reason is not None:
        return reason
    total = len(chain.steps)
    if type(chain.step_task_ids) is not tuple:
        return "chain step task ids are not a tuple"
    if len(chain.step_task_ids) != total:
        return "chain step task ids do not match the declared steps"
    created = 0
    uncreated_seen = False
    for task_id in chain.step_task_ids:
        if type(task_id) is not str:
            return "a chain step task id is not a string"
        if not task_id:
            uncreated_seen = True
            continue
        if uncreated_seen:
            return "chain step task ids skip an earlier step"
        reason = chain_id_refusal(task_id)
        if reason is not None:
            return f"a chain step task id is malformed: {reason}"
        created += 1
    named = [task_id for task_id in chain.step_task_ids if task_id]
    if len(set(named)) != len(named):
        return "chain names the same step task twice"
    if type(chain.cursor) is not int:
        return "chain cursor is not an exact integer"
    if not 0 <= chain.cursor < total:
        return "chain cursor is outside the chain"
    # At most the step AT the cursor may already have a task; anything beyond
    # it would mean a later step was created before the chain reached it.
    if created > chain.cursor + 1:
        return "chain created a task beyond its cursor"
    if chain.status is ChainStatus.ACTIVE and created < chain.cursor:
        return "chain advanced past a step it never created"
    # The cursor is not re-checked for a completed chain: "created beyond
    # the cursor" above already forces cursor == total - 1 once every step
    # has a task, and a second unreachable rule would read as live policy.
    if chain.status is ChainStatus.COMPLETED and created != total:
        return "a completed chain left a step uncreated"
    return None


def _parsed_chain_declaration(
    value: Any,
) -> tuple[tuple[ChainStep, ...], str | None]:
    """The steps a declaration yields AND why it is unusable, decided once.

    PARSE ONCE, VALIDATE ONCE: each item is read into a ``ChainStep`` exactly
    one time, and the object that is JUDGED is the object that is RETURNED.
    Validating one parse and then re-parsing for the caller would mean the
    grammar's verdict describes an object nobody ever uses - and for an input
    whose reads are not referentially transparent, such as a mapping subclass
    with a stateful ``get``, the accepted step and the returned step could
    differ. That is the same law ``ChainState.from_dict`` already follows:
    build the candidate, judge THAT candidate, return THAT candidate.

    A declaration is an ordered list of step objects and nothing else: no
    identity, no cursor, no status and no task ids, because a caller declares
    intent and never declares progress. Unknown keys are refused rather than
    filtered, so a step carrying a task id, a model answer reference, a data
    classification or a nested chain is rejected by name instead of quietly
    becoming a step that happens to look well-formed.
    """
    if not isinstance(value, list):
        return (), "chain declaration is not a list"
    if not value:
        return (), "chain declaration names no step"
    if len(value) > MAX_CHAIN_STEPS:
        return (), "chain declaration exceeds its step ceiling"
    steps: list[ChainStep] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return (), f"chain step {index} is not an object"
        if not set(item) <= _CHAIN_STEP_KEYS:
            return (), f"chain step {index} carries an unsupported key"
        steps.append(ChainStep.from_dict(item))
    frozen = tuple(steps)
    reason = chain_steps_refusal(frozen)
    if reason is not None:
        return (), reason
    # B1. ONLY NOW, on steps the grammar has already ACCEPTED, are the three
    # SCOPE fields frozen in the spelling persistence will hold. Order is the
    # whole point: canonicalizing first would REPAIR values the grammar exists
    # to refuse - a padded locator would silently become a clean one - which
    # is precisely the silent widening this must not do. Nothing here changes
    # a capability, a routing decision or a predecessor index; it changes only
    # how one accepted value is spelled.
    #
    # And the result is judged again, by the same grammar, because a
    # transformation whose output nobody validates is a second boundary.
    canonical = tuple(_canonical_step(step) for step in frozen)
    reason = chain_steps_refusal(canonical)
    if reason is not None:
        return (), reason
    return canonical, None


def chain_declaration_refusal(value: Any) -> str | None:
    """Why this caller-supplied declaration is not a usable chain, or ``None``.

    THE single grammar for what the TRUSTED input boundary may accept.
    """
    return _parsed_chain_declaration(value)[1]


def chain_steps_from_declaration(value: Any) -> tuple[ChainStep, ...]:
    """The typed ordered steps of an ACCEPTED declaration, or ``()``.

    These are the very objects the grammar judged, not a second parse of the
    same input, so a caller can never receive a step the grammar did not
    accept.
    """
    return _parsed_chain_declaration(value)[0]


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
#: COMPOUND credential key names, matched whole and tried FIRST.
#: The bare terms below are anchored with a trailing ``\b``, and ``_`` is a
#: word character, so ``secret_access_key=`` never matched ``secret`` (no
#: boundary between ``t`` and ``_``) and ``access_key_id=`` matched nothing at
#: all - ``key`` alone is deliberately not a label, because ``key.py`` and
#: ``keys.json`` must stay readable. That left the two most common cloud
#: credentials in the world, ``AWS_ACCESS_KEY_ID`` and
#: ``AWS_SECRET_ACCESS_KEY``, completely outside the policy. Alternation is
#: ordered, so listing the compound forms first also makes the redaction keep
#: the whole key name visible instead of stopping at ``secret``.
_COMPOUND_CREDENTIAL_LABEL = (
    r"secret[_\-\s]?access[_\-\s]?key(?:[_\-\s]?id)?"
    r"|access[_\-\s]?key[_\-\s]?id"
    r"|access[_\-\s]?key"
    r"|secret[_\-\s]?key"
    r"|api[_\-\s]?secret"
)
_CREDENTIAL_LABEL = (
    _COMPOUND_CREDENTIAL_LABEL
    + r"|api[_\-\s]?key|token|secret|password|passwd|authorization|cookie"
    r"|private[_\-\s]?key"
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_CREDENTIAL_LABEL})\b\s*[:=]\s*\S", re.IGNORECASE
)
#: Command-line credential syntax: ``--api-key VALUE``, ``--token=VALUE`` and
#: friends. The flag prefix is required, so ordinary prose mentioning a token or
#: a secret - and file names such as ``tokenizer.py`` - never matches.
_CLI_CREDENTIAL_FLAG = (
    # Compound forms first, for the same ordering reason as the label above.
    r"secret[_\-]?access[_\-]?key(?:[_\-]?id)?|access[_\-]?key[_\-]?id"
    r"|access[_\-]?key|secret[_\-]?key|api[_\-]?secret"
    r"|api[_\-]?key|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token|token"
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
    # AWS access key ids carry their own documented unique-id prefix, so they
    # are recognizable with no label at all - which matters because a bare
    # ``AKIA...`` in file content has no ``key=`` next to it to catch.
    re.compile(
        r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{16}\b"
    ),
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
    #: Typed bounded view of one local file read by THIS task (M3). Written
    #: only by AgentCore from a validated connector result; never accepted
    #: from untrusted task input, never persisted into project memory and
    #: never authority of any kind.
    local_read: LocalReadSnapshot = field(default_factory=LocalReadSnapshot)
    #: M5. The EXACT bytes a local write will produce, as text. Unlike every
    #: other content field in this contract this one IS accepted from
    #: untrusted task input - it is the deliverable, and authority over it
    #: comes from approval, not from provenance. It lives here, typed and at
    #: the top level, rather than in ``context``: the Claude connector
    #: serializes ``context`` verbatim into its payload, so a context key
    #: would have been an egress channel for up to 64 KiB. Nothing adds this
    #: field to an outbound key set, so it cannot leave the machine.
    #:
    #: It persists only for the task's EXECUTABLE lifetime: ``resume``
    #: rebuilds a task solely from storage, so bytes that are not stored
    #: cannot be re-attested. At authoritative terminal state the storage
    #: layer blanks it in the same guarded transaction.
    write_content: str = ""
    #: M5. The declared write mode, part of the authority rather than a hint.
    #: Separate from ``local_write`` because the record is the post-execution
    #: metadata and must satisfy its full structural contract, while the mode
    #: has to survive a reload from the moment the task is created.
    write_mode: str = ""
    #: M5. Typed metadata of the write THIS task performed. Written only by
    #: AgentCore from a validated connector result; never accepted from
    #: untrusted task input, never persisted into project memory, never a
    #: decision, and it carries no bytes of any kind.
    local_write: LocalWriteRecord = field(default_factory=LocalWriteRecord)
    #: M6. The bounded, structure-preserving answer an EXTERNAL MODEL returned
    #: for THIS task. Written only by AgentCore from a validated connector
    #: result; never accepted from untrusted task input, never written to
    #: project memory, never a retrieval candidate, never a decision record
    #: and never authority of any kind. Nothing adds it to an outbound key
    #: set, so it cannot travel back to the model.
    #:
    #: Unlike ``write_content`` it is NOT erased at terminal state: it is the
    #: completed task's user-facing deliverable and has to stay readable.
    model_answer: ModelAnswer = field(default_factory=ModelAnswer)
    #: M7. Durable identity of where THIS task's write bytes come from. Derived
    #: by the input boundary from the validated ``write`` shape, never accepted
    #: from input as such, never erased at terminal state, and never
    #: filesystem authority: the M5 frame keeps binding the bytes themselves.
    #: A malformed or inconsistent persisted identity loads as ``INVALID`` and
    #: is refused by every stage that builds write authority, so a handoff
    #: task can never silently degrade into a direct write.
    write_source: WriteSource = WriteSource.DIRECT
    #: M7. The completed same-session model task whose answer was (or is to
    #: be) resolved into ``write_content``. A reference, never content; kept
    #: at terminal state as provenance after the bytes are erased.
    write_source_task_id: str = ""
    #: M8. Durable identity of THIS task's bounded multi-file read context.
    #: Derived by the input boundary from the validated ``context`` shape,
    #: never accepted from input as such, and never erased at terminal state.
    #: A malformed or inconsistent persisted identity loads as ``INVALID``
    #: and is refused by every stage that builds authority, so a multi-file
    #: consumer can never silently become a context-less model request.
    local_read_context_kind: LocalReadContextKind = LocalReadContextKind.NONE
    #: M8. The completed same-session local-read tasks whose excerpts were
    #: (or are to be) frozen onto this task, in the user's declared order.
    #: References, never content; kept at terminal state as provenance.
    local_read_source_task_ids: tuple[str, ...] = ()
    #: M8. The ORDERED frozen bundle, resolved exactly once at
    #: CONTEXT_RETRIEVAL and never re-derived afterwards. Written only by
    #: AgentCore from fully validated source state, never accepted from
    #: untrusted task input, never written to project memory, never a
    #: retrieval candidate and never authority of any kind. Retained at
    #: terminal state exactly like the M3 singular snapshot.
    local_read_sources: tuple[LocalReadSnapshot, ...] = ()
    #: M9. Durable identity of the read WINDOW this task asks for, derived by
    #: the input boundary from the validated ``read`` section and never
    #: accepted from input as such. A malformed or inconsistent persisted
    #: window loads as ``INVALID`` and is refused by the connector, so a
    #: region read can never silently degrade into a head read wearing a
    #: region label.
    local_read_kind: LocalReadKind = LocalReadKind.HEAD
    #: M9. The 1-based inclusive first source line, and how many lines to
    #: read. Both 0 for a head read. This is the REQUEST; what was actually
    #: produced is described by the snapshot's own span.
    read_start_line: int = 0
    read_max_lines: int = 0
    #: M11. Trusted provenance proving this task was created FOR one chain
    #: step. Absent from the task input allowlist, so untrusted JSON naming it
    #: is refused by name; only the trusted chain derivation sets it. It is a
    #: claim a consumer must still check against the stored chain, never a
    #: permission, and it grants nothing on its own.
    chain_binding: ChainBinding = field(default_factory=ChainBinding)
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
            "local_read": self.local_read.to_dict(),
            "write_content": self.write_content,
            "write_mode": self.write_mode,
            "local_write": self.local_write.to_dict(),
            "model_answer": self.model_answer.to_dict(),
            # M7. Persisted verbatim and never erased. A kind that is not an
            # exact member is serialized as the typed INVALID verdict rather
            # than repaired into an active kind.
            "write_source": (
                self.write_source.value
                if type(self.write_source) is WriteSource
                else WriteSource.INVALID.value
            ),
            "write_source_task_id": self.write_source_task_id,
            "chain_binding": self.chain_binding.to_dict(),
            # M8. Persisted verbatim and never erased. The kind is defended
            # here for the same reason the M7 one is: ``.value`` on a raw
            # string would raise, and that raw string would then satisfy a
            # later membership test as if it were a member.
            "local_read_context_kind": (
                self.local_read_context_kind.value
                if type(self.local_read_context_kind) is LocalReadContextKind
                else LocalReadContextKind.INVALID.value
            ),
            "local_read_source_task_ids": list(self.local_read_source_task_ids),
            "local_read_sources": [
                snapshot.to_dict() for snapshot in self.local_read_sources
            ],
            # M9. Persisted verbatim and never erased. The kind is defended
            # here exactly as the M7 and M8 ones are: ``.value`` on a raw
            # string would raise, and that raw string would then satisfy a
            # later membership test as if it were a member.
            "local_read_kind": (
                self.local_read_kind.value
                if type(self.local_read_kind) is LocalReadKind
                else LocalReadKind.INVALID.value
            ),
            "read_start_line": self.read_start_line,
            "read_max_lines": self.read_max_lines,
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
        # M7. The durable write-source identity loads FAIL CLOSED. A missing
        # key is a legacy pre-M7 row and therefore a direct write by
        # construction; a present value that is not exactly one of the
        # canonical strings is the typed INVALID verdict, never an active
        # kind. A malformed reference blanks the reference AND invalidates
        # the identity, and an inconsistent (kind, reference) pair - a
        # direct write carrying a reference, a handoff carrying none - is
        # INVALID too, so no single corrupted field can turn a handoff into a
        # direct write. Nothing is coerced: no ``str()``, no ``bool()``, no
        # ``Enum()`` on a non-string, no repair into another active kind.
        raw_source = data.get("write_source", WriteSource.DIRECT.value)
        if type(raw_source) is str and raw_source in _WRITE_SOURCE_VALUES:
            write_source = WriteSource(raw_source)
        else:
            write_source = WriteSource.INVALID
        raw_reference = data.get("write_source_task_id", "")
        if type(raw_reference) is str and raw_reference == "":
            write_source_task_id = ""
        elif local_write_source_refusal(raw_reference) is None:
            write_source_task_id = raw_reference
        else:
            write_source_task_id = ""
            write_source = WriteSource.INVALID
        if (
            write_source is not WriteSource.INVALID
            and local_write_identity_refusal(write_source, write_source_task_id)
            is not None
        ):
            write_source = WriteSource.INVALID
        # M8. The durable read-context identity loads FAIL CLOSED, and only
        # the COMPLETE absence of M8 state is a legacy pre-M8 row. A missing
        # kind key defaults to NONE, but the pair contract below then refuses
        # a NONE that still carries references or frozen sources - so
        # deleting the kind key on a real bundle yields INVALID, never a
        # silently context-less task. Nothing is coerced: no ``str()``, no
        # ``bool()``, no ``Enum()`` on a non-string, and one malformed frozen
        # snapshot refuses the whole bundle rather than being dropped from it.
        raw_kind = data.get(
            "local_read_context_kind", LocalReadContextKind.NONE.value
        )
        if type(raw_kind) is str and raw_kind in _LOCAL_READ_CONTEXT_KIND_VALUES:
            local_read_context_kind = LocalReadContextKind(raw_kind)
        else:
            local_read_context_kind = LocalReadContextKind.INVALID
        raw_ids = data.get("local_read_source_task_ids", [])
        if type(raw_ids) is list and all(type(item) is str for item in raw_ids):
            local_read_source_task_ids = tuple(raw_ids)
        else:
            local_read_source_task_ids = ()
            local_read_context_kind = LocalReadContextKind.INVALID
        raw_sources = data.get("local_read_sources", [])
        if type(raw_sources) is list:
            local_read_sources = tuple(
                LocalReadSnapshot.from_dict(item)
                if type(item) is dict
                else LocalReadSnapshot()
                for item in raw_sources
            )
        else:
            local_read_sources = ()
            local_read_context_kind = LocalReadContextKind.INVALID
        if (
            local_read_context_kind is not LocalReadContextKind.INVALID
            and local_read_context_refusal(
                local_read_context_kind,
                local_read_source_task_ids,
                local_read_sources,
            )
            is not None
        ):
            local_read_context_kind = LocalReadContextKind.INVALID
        if local_read_context_kind is LocalReadContextKind.INVALID:
            local_read_source_task_ids = ()
            local_read_sources = ()
        # M9. The durable read window loads FAIL CLOSED, and only the
        # COMPLETE absence of M9 state is a legacy pre-M9 row. A missing kind
        # key defaults to HEAD, but the contract below then refuses a HEAD
        # that still carries a window - so deleting the kind key on a real
        # region read yields INVALID, never a silently mislabelled head read.
        raw_read_kind = data.get("local_read_kind", LocalReadKind.HEAD.value)
        if type(raw_read_kind) is str and raw_read_kind in _LOCAL_READ_KIND_VALUES:
            local_read_kind = LocalReadKind(raw_read_kind)
        else:
            local_read_kind = LocalReadKind.INVALID
        raw_start = data.get("read_start_line", 0)
        raw_max = data.get("read_max_lines", 0)
        if type(raw_start) is not int or type(raw_max) is not int:
            raw_start, raw_max = 0, 0
            local_read_kind = LocalReadKind.INVALID
        if (
            local_read_kind is not LocalReadKind.INVALID
            and local_read_region_refusal(local_read_kind, raw_start, raw_max)
            is not None
        ):
            local_read_kind = LocalReadKind.INVALID
        if local_read_kind is LocalReadKind.INVALID:
            raw_start, raw_max = 0, 0
        return cls(
            objective=data["objective"],
            capability=data["capability"],
            # M11. A tampered or absent binding loads as NO binding, which
            # simply cannot prove chain ownership to any consumer.
            chain_binding=ChainBinding.from_dict(data.get("chain_binding")),
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
            # Legacy task JSON without the field loads as an empty snapshot;
            # a malformed one loads closed (SECRET, re-bounded excerpt).
            local_read=LocalReadSnapshot.from_dict(data.get("local_read")),
            # M5. The same grammar the input boundary applies, so a row edited
            # outside the product cannot carry a payload the boundary would
            # have refused. An empty value is legitimate twice over: a task
            # that writes nothing, and a terminal task whose bytes the storage
            # layer already blanked.
            write_content=(
                data.get("write_content", "")
                if local_write_content_refusal(data.get("write_content", "")) is None
                else ""
            ),
            write_mode=(
                data.get("write_mode", "")
                if local_write_mode_refusal(data.get("write_mode")) is None
                else ""
            ),
            # Legacy task JSON without the field loads as an empty record; a
            # malformed one loads closed as empty rather than partly believed.
            local_write=LocalWriteRecord.from_dict(data.get("local_write")),
            # M6. Legacy task JSON without the field loads as the empty
            # answer; a malformed one loads closed as empty rather than
            # partly believed. Nothing is re-bounded or repaired.
            model_answer=ModelAnswer.from_dict(data.get("model_answer")),
            write_source=write_source,
            write_source_task_id=write_source_task_id,
            local_read_context_kind=local_read_context_kind,
            local_read_source_task_ids=local_read_source_task_ids,
            local_read_sources=local_read_sources,
            local_read_kind=local_read_kind,
            read_start_line=raw_start,
            read_max_lines=raw_max,
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
    #: M11. The ONE sequential chain this session owns, or ``None``. Absence
    #: means no chain, and a legacy session row is indistinguishable from a
    #: session that simply never declared one - correctly, because both have
    #: nothing to orchestrate. A MALFORMED persisted chain deliberately does
    #: NOT join that absence: it loads as the typed INVALID chain, so a
    #: tampered declaration can never read as "this session is free to start
    #: one" and be quietly overwritten. The field is inert here: it is a
    #: declaration and a cursor, never an approval, a grant or an attempt.
    chain: "ChainState | None" = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Written explicitly rather than left to ``asdict``: the chain owns a
        # canonical encoding (enum values, lists, fixed key order) and the
        # session must persist THAT, not a recursive field dump that happens
        # to serialize today.
        data["chain"] = None if self.chain is None else self.chain.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        allowed = {
            "objective",
            "id",
            "active_task_id",
            "latest_task_id",
            "next_step",
            "project_id",
            "chain",
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
        # M11. Absent or explicitly null means no chain; a legacy session row
        # takes this path. Anything else is judged by the one canonical chain
        # contract, which degrades a malformed chain to the typed INVALID
        # verdict rather than to absence.
        raw_chain = payload.get("chain")
        payload["chain"] = None if raw_chain is None else ChainState.from_dict(raw_chain)
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


def result_to_dict(result: ToolResult) -> dict[str, Any]:
    """THE canonical serialization of a persisted execution result.

    F3. It lives here, on the provider-independent surface, because two
    things now need to write one: the ordinary loop, which persists what a
    connector returned, and storage-side recovery, which persists the typed
    failure of an outcome that will never be known. A second serializer would
    mean a second failure schema, and a task restored from the wrong one
    would report a different diagnostic depending on who wrote it.
    """
    return {
        "outcome": result.outcome.value,
        "summary": result.summary,
        "data": result.data,
        "error": result.error.to_dict() if result.error else None,
        "usage": result.usage.to_dict(),
        "verification": result.verification.to_dict(),
    }


def terminal_failure_result(error: StructuredError) -> ToolResult:
    """The canonical bounded result a typed terminal failure persists.

    The snapshot a restart reads back, so the same task reports the same
    diagnostic however it got there. It describes ITS OWN failure - the error
    rides inside the result, not merely beside it - and carries no connector,
    model, file or task content: the summary is the typed error message that
    the diagnostic contract has already bounded.

    Usage is the explicit no-charge record rather than an absent one. A
    result that reported nothing about money would be indistinguishable from
    an unrecorded route, and H3 refuses to infer a route from absence.
    """
    return ToolResult(
        outcome=OutcomeStatus.ERROR,
        summary=error.message,
        error=error,
        usage=UsageRecord(
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            monetary_state=MonetaryState.NOT_APPLICABLE,
        ),
    )


@dataclass(frozen=True)
class CancellationOutcome:
    """One cancellation verdict, with the AUTHORITATIVE state behind it.

    A caller used to bring its own decoded ``Task`` to a cancellation and get a
    verdict about it. That made a persisted document the thing being cancelled
    rather than a row: a document carrying another task's id cancelled THAT
    task, and a document carrying a forged status let the caller report a
    terminal outcome storage never agreed to.

    So the request is now two ids, and everything that comes back was rebound
    to its own row inside the deciding transaction. ``session`` and ``task``
    are present for every verdict that identified a task at all, and they are
    the rows - not what anybody claimed about them.

    Frozen, because a receipt that could be edited afterwards would be back to
    being a claim.
    """

    verdict: str
    session: SessionState | None = None
    task: Task | None = None


class AttemptRecovery(str, Enum):
    """The CLOSED set of operator recoveries an execution attempt admits.

    Exactly one member, and that is the design rather than a placeholder.
    An attempt whose outcome is UNKNOWN may be ABANDONED - the operator
    states that the result will never be established and accepts the task as
    failed. Nothing else is expressible here: there is deliberately no
    ``retry``, no ``succeeded`` and no ``release``, because storage cannot
    know whether the external effect happened, and a vocabulary that offered
    those words would invite a caller to assert something no evidence
    supports.
    """

    ABANDONED = "abandoned"


class AttemptRecoveryOutcome(str, Enum):
    """The CLOSED verdict of one attempted operator recovery.

    Only ``RECOVERED`` wrote anything. Every other member describes a refusal
    that left storage exactly as it was, so a caller can report what happened
    without inspecting rows itself.

    ``NOT_RECOVERABLE`` deliberately does not say WHICH state the attempt was
    in, and ``UNKNOWN`` covers a missing attempt, a foreign session, a foreign
    task and a mismatched pair alike: which other session owns something is
    not a fact this verdict may reveal.
    """

    #: The task is now terminally FAILED and the reservation is unresolved.
    RECOVERED = "recovered"
    #: No such attempt for this exact session and task.
    UNKNOWN = "unknown"
    #: The attempt exists but is not in a recoverable state.
    NOT_RECOVERABLE = "not_recoverable"
    #: The task already reached a terminal state; nothing was rewritten.
    TERMINAL = "terminal"


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


# --------------------------------------------------------------------------
# M12 S1. Read-only operator visibility.
#
# These are PROJECTIONS, not state: they are built for display, nothing reads
# them back, and no lifecycle, approval, billing or chain decision may ever be
# taken from one. They carry OPERATIONAL METADATA ONLY - identifiers,
# lifecycle position, timestamps and counts. No objective, no context, no
# model answer, no file bytes and no persisted free text crosses this
# boundary, so the visibility surface cannot become a second, unbounded read
# path into task state.
# --------------------------------------------------------------------------

#: How many rows a listing returns when the caller asks for no bound, and the
#: ceiling it may never exceed. A listing is an operator convenience, not an
#: export: an unbounded one would turn a display command into a bulk reader.
VISIBILITY_DEFAULT_LIMIT = 20
VISIBILITY_MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One session, as an operator needs to rediscover it after a restart.

    ``readable`` is the honest answer to a row whose persisted state cannot be
    rebuilt. Such a row is REPORTED, never skipped and never guessed at: the
    identity and timestamp come from real columns, everything derived from the
    unreadable document is ``None``, and the operator is told which it is.
    Hiding a corrupted row would be the one outcome that leaves someone
    hunting for work the product can see and will not mention.
    """

    id: str
    updated_at: str
    readable: bool = True
    project_id: str | None = None
    active_task_id: str | None = None
    latest_task_id: str | None = None
    task_count: int = 0
    #: The session's bounded next-step hint, through the SAME canonical rule
    #: the loader applies. ``None`` whenever the document did not validate,
    #: so an unreadable session cannot contribute display text.
    next_step: str | None = None
    chain_id: str | None = None
    chain_digest: str | None = None
    chain_status: str | None = None
    chain_cursor: int | None = None
    chain_step_count: int | None = None
    chain_usable: bool | None = None


@dataclass(frozen=True, slots=True)
class ProgressSummary:
    """One task's progress view, FROZEN at the moment it was validated.

    P1. This exists so the visibility surface never has to reopen a task
    document after proving it. The previous shape validated a projection and
    then, later in the same command, loaded the raw row again to build
    progress - a window in which the persisted document could be replaced,
    and one through which a forged id, a forged lifecycle and a forged
    artifact label all reached the output. A value captured at validation
    time cannot be swapped afterwards, because there is nothing left to
    re-read.

    Every field is either a fixed enum spelling or a value the existing
    bounded diagnostic contracts already produce. Nothing raw is carried.
    """

    current_stage: str | None = None
    next_stage: str | None = None
    completed_stages: tuple[str, ...] = ()
    #: Already-bounded typed dicts from ``ProgressIssue.to_dict`` /
    #: ``ProgressArtifact.to_dict`` - neither has a free-form message field,
    #: and the artifact label passes the ordinary progress-text rule.
    open_issues: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    #: The canonical rebuilt plan, never the persisted strings.
    plan: tuple[str, ...] = ()


#: The one effect kind this projection can report. A closed vocabulary of
#: exactly one, because exactly one capability in this product produces an
#: irreversible local effect whose occurrence outlives the task that asked
#: for it.
APPLIED_EFFECT_LOCAL_WRITE = "local_write"

#: The exact ``LocalWriteRecord`` status meaning the bytes reached the disk.
#: "prepared", "refused" and "unknown" are all NOT this, and none of them may
#: ever be reported as an applied effect.
LOCAL_WRITE_APPLIED_STATUS = "written"


@dataclass(frozen=True, slots=True)
class AppliedEffectSummary:
    """An irreversible effect this task is KNOWN to have already applied.

    P2. A cancellation that arrives while an approved local write is already
    IN_FLIGHT is honoured as a cancellation - the task really does end
    CANCELLED, and H2 is right to keep it that way, because the operator's
    request beat the result that came back. But the write itself may already
    have landed, and it cannot be undone: M5 keeps no backup and promises no
    rollback. A later ``show`` that reported only CANCELLED therefore stated
    something true about the WORKFLOW while implying something false about
    the WORLD - that nothing happened.

    This says the other half. It is a fact about an effect, not about a
    lifecycle: it is derived from the validated write record alone and never
    from the task's status, so it can neither be manufactured by a
    cancellation nor suppressed by one.

    Two constant-vocabulary fields and nothing else. There is no path, no
    digest, no size and no timestamp here - not because they would be
    unbounded, but because none of them is needed to answer the one question
    this exists for, and every field that is not needed is a field that could
    later be widened. The bounded metadata view stays where it already lives,
    in the local-write envelope block.

    Untrusted-free and authority-free: possessing or reading it permits
    nothing. It is never consulted by execution, recovery, approval, billing
    or the chain, and it is produced only inside a read-only projection.
    """

    kind: str
    status: str


@dataclass(frozen=True, slots=True)
class DeliverableSummary:
    """A completed task's durable deliverable, as bounded METADATA.

    The M6 ``ModelAnswer`` is the deliverable a task persists and keeps at
    terminal state. Its CONTENT is deliberately absent here, exactly as it is
    absent from every other visibility projection: this surface carries no
    free-text channel, and the answer text is untrusted inbound model data
    whose one reader-facing home is the envelope's own ``model_answer`` block.
    What crosses instead are the four persisted fields that let an operator
    SEE that a deliverable exists and judge it without reading it - how long
    it is, whether it was cut, when it was produced, and the digest of exactly
    the bytes the envelope would show (the same digest an M7 handoff write
    cites as its source).

    Every field is re-validated state from the ONE canonical structural
    contract, frozen from the same proven document as ``progress`` and
    ``effect_applied``. Untrusted-free and authority-free: knowing a digest
    permits nothing, decides nothing and is consumed by no executing stage.
    """

    character_count: int
    sha256: str
    truncated: bool
    produced_at: str


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """One task's lifecycle position. Never its content.

    ``capability`` and ``action`` are declared vocabulary the product routes
    on, not free text, and they are what makes a listing legible at all. The
    objective - the user's own words - is deliberately absent.
    """

    id: str
    session_id: str
    status: str
    stage: str
    updated_at: str
    readable: bool = True
    capability: str | None = None
    action: str | None = None
    selected_connector: str | None = None
    chain_step_index: int | None = None
    #: Present only for a document that PASSED validation. ``None`` is the
    #: honest answer for a row whose identity or lifecycle disagrees with
    #: storage: there is no progress to report about a description that is
    #: not this task's.
    progress: ProgressSummary | None = None
    #: P2. An irreversible effect this task already applied, frozen at the
    #: same moment and under the same proof as ``progress``. ``None`` means
    #: no such effect is KNOWN - which is the honest answer for a task that
    #: applied none, and equally for a row whose record cannot be trusted.
    effect_applied: AppliedEffectSummary | None = None
    #: The task's durable deliverable (the M6 model answer) as bounded
    #: metadata, frozen by the same proof as ``progress``. ``None`` means no
    #: deliverable is KNOWN - the honest answer for a task that produced
    #: none, for a row whose document cannot be read, and for an answer that
    #: did not survive its own structural contract (which the load boundary
    #: has already degraded to the empty answer).
    deliverable: DeliverableSummary | None = None


#: The exception family a persisted document raises when it is not the shape
#: its loader expects. ``json.loads`` raises ``ValueError`` for malformed
#: text; a loader handed VALID json of the wrong ROOT SHAPE raises
#: ``AttributeError`` (``None.items``, ``int.get``) or ``KeyError`` (a
#: required field that is simply absent); ``TypeError`` covers the rest.
#: Named ONCE so every visibility projection degrades on exactly the same
#: set and none of them can quietly narrow it.
VISIBILITY_DECODE_ERRORS = (TypeError, ValueError, AttributeError, KeyError)

#: The keys a persisted SESSION document must carry to be a description of a
#: real session rather than a fragment that ``from_dict`` will happily fill
#: with defaults. ``chain`` is deliberately ABSENT from the set: a session
#: that never declared one legitimately omits the key, and the documented
#: legacy shape does exactly that.
VISIBILITY_SESSION_KEYS = frozenset({"id", "objective", "created_at", "updated_at"})

#: The same for a TASK document: identity, lifecycle and the scope every
#: projection reads. Everything else a task carries is optional runtime state
#: that a legitimate row may genuinely lack.
VISIBILITY_TASK_KEYS = frozenset(
    {"id", "objective", "capability", "scope", "status", "stage",
     "created_at", "updated_at"}
)

#: Ceiling for every free-ish string a visibility projection may echo back
#: from an auxiliary row. A persisted row edited outside the product can hold
#: megabytes; a display command must not become the thing that prints them.
VISIBILITY_TEXT_MAX_CHARS = 120


def visibility_text(value: Any) -> str | None:
    """One auxiliary field, bounded, or ``None`` when it is not usable.

    The single normalizer every auxiliary projection uses. It is deliberately
    STRICTER than the progress text rule: this is machine metadata - an
    attempt's connector name, an event's type - not prose, so anything that
    is not a plain bounded single-line token is refused outright rather than
    truncated into something that looks legitimate.
    """
    if type(value) is not str:
        return None
    text = value.strip()
    if not text or len(text) > VISIBILITY_TEXT_MAX_CHARS:
        return None
    if any(character.isspace() for character in text):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None
    return text


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """The TYPE and time of one journal event. Never its payload.

    The payload is arbitrary diagnostic state; the type is a fixed internal
    vocabulary. Only the type crosses, so "what happened last" is answerable
    without opening a channel for whatever a checkpoint happened to record.

    ``readable`` false is a row whose type or timestamp is not the bounded
    canonical shape. Its fields are then ``None``: a malformed journal row is
    reported as unreadable, never echoed back verbatim.
    """

    event_type: str | None = None
    created_at: str | None = None
    readable: bool = True


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    """One execution attempt's H2 position, bounded. Never a raw row.

    ``state`` is an exact ``AttemptState`` or ``None``: an unrecognized
    persisted state is NOT coerced into a known one and never becomes
    ``replayable``. ``replayable`` is derived from the trusted enum alone, so
    a corrupted row can never be displayed as safe to run again.

    ``id`` is the attempt's authoritative identity, and it is here for one
    reason: ``task resolve`` requires the operator to name the EXACT attempt
    they are recovering, and without it that shipped authority is only
    reachable by reading the database by hand. It is a handle, not a
    permission - recovery re-reads and re-proves the attempt it is given,
    so knowing an id grants nothing.
    """

    id: str | None = None
    state: AttemptState | None = None
    connector: str | None = None
    updated_at: str | None = None
    readable: bool = True

    @property
    def replayable(self) -> bool | None:
        """Whether H2 would allow another attempt. ``None`` when unknown.

        Unknown is NOT "yes". A row whose state could not be decoded has no
        replayability answer, and inventing one here would be the display
        surface making an execution claim.
        """
        if self.state is None:
            return None
        return self.state not in ATTEMPT_NON_REPLAYABLE


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    """The three fields needed to answer an open approval. Nothing else.

    The request's reason, scope, risk and outbound preview are deliberately
    absent - they belong to the approval-issuing envelope, which already
    renders them under its own contract. Here they would be an unbounded
    channel carrying the user's own words into a listing.
    """

    request_id: str | None = None
    fingerprint: str | None = None
    expires_at: str | None = None
    readable: bool = True




class MemoryStore(Protocol):
    """Provider-independent persistence surface required by ``AgentCore``."""

    def save_session(self, session: SessionState) -> None: ...

    def load_session(self, session_id: str) -> SessionState | None: ...

    # -- M12 S1: read-only operator visibility -----------------------------
    #
    # Declared HERE so the command surface depends on this contract and never
    # on one storage implementation. Every one of them is a pure read: a
    # conforming store must not write, must not create a session, task,
    # attempt, approval or reservation, and must not repair a row it cannot
    # read. A row whose persisted document is unreadable is REPORTED with
    # ``readable`` false and its derived fields absent - never skipped, never
    # rebuilt from a guess, and never raised out of the call.

    #: Newest first by ``updated_at``, ties broken by ascending id so two
    #: rows written in the same instant still have ONE stable order.
    #:
    #: THE BOUND IS THE STORE'S, NOT THE CALLER'S. A conforming store returns
    #: at most ``min(limit, VISIBILITY_MAX_LIMIT)`` rows and asks its backing
    #: storage for no more than that, whoever is calling and whatever they
    #: pass. The command layer separately REFUSES a user-supplied limit above
    #: the ceiling, but that refusal is input validation, not the bound: a
    #: store that trusted its caller would let any direct caller - a future
    #: command, a script, a different front end - turn a display listing into
    #: an unbounded export of task state. A limit that is not an exact
    #: positive ``int`` (``bool`` included, since ``True`` is an ``int``)
    #: yields an EMPTY list, never a row count.
    def list_sessions(self, limit: int) -> list[SessionSummary]: ...

    #: The tasks of EXACTLY this session, newest first with the same stable
    #: tie-break and the same store-side bound. A task owned by another
    #: session is never returned, whatever the caller passes.
    def list_tasks(self, session_id: str, limit: int) -> list[TaskSummary]: ...

    #: The TYPE and time of this task's most recent journal event, or None.
    #: The payload never crosses, and a row whose type or timestamp is not
    #: the bounded canonical shape is returned as ``readable`` false rather
    #: than echoed back.
    def latest_task_event(self, task_id: str) -> TaskEvent | None: ...

    #: ONE session as the same validated projection a listing returns, or
    #: ``None`` when no such row exists. The command surface uses THIS rather
    #: than ``load_session`` so a single-session view cannot be looser than a
    #: listing of the same row: the ordinary loader rebuilds a fragment into
    #: a plausible default object, and a display command that trusted it
    #: would report state the session never had.
    def session_summary(self, session_id: str) -> SessionSummary | None: ...

    #: ONE task as the same validated projection, or ``None``. Identity,
    #: status and stage must agree with the authoritative COLUMNS before any
    #: document-derived field is reported; a document that disagrees is
    #: ``readable`` false and contributes nothing.
    #:
    #: The returned summary is COMPLETE and FROZEN: it already carries every
    #: progress field a display surface may render, captured at the instant
    #: the document was validated. A conforming store must not require - and
    #: a caller must not perform - a second read of the same row afterwards.
    #: That second read is a TOCTOU window: the document can be replaced
    #: between the proof and the render, and a forged identity, lifecycle or
    #: artifact would then reach the output under a projection that had
    #: already been approved.
    def task_summary(self, task_id: str) -> TaskSummary | None: ...

    #: This task's latest execution attempt as BOUNDED display metadata, or
    #: ``None`` when it has none. An unrecognized persisted state must decode
    #: to ``None`` and must never be reported replayable.
    def latest_attempt_summary(self, task_id: str) -> AttemptSummary | None: ...

    #: The open approval for this exact task and session as BOUNDED display
    #: metadata, or ``None``. Only request id, fingerprint and expiry; the
    #: reason and scope never cross.
    def pending_approval_summary(
        self, task_id: str, session_id: str
    ) -> ApprovalSummary | None: ...

    # M11. The storage-owned chain authority family. Each is a single-
    # transaction decision: the caller proposes, storage decides, and a
    # caller-held SessionState or Task is never proof of anything. They are
    # declared HERE, on the provider-independent surface, so AgentCore can
    # depend on this contract rather than on one storage implementation.
    # Verdicts are plain strings, so no storage type crosses the interface.

    #: The ONLY way a chain is created. Generic ``save_session`` may never
    #: create, clear or replace one.
    #: Returns "declared" | "occupied" | "unknown".
    def declare_chain_if_absent(
        self, session_id: str, chain: ChainState
    ) -> str: ...

    #: The ONLY way a step task is created. Creation and binding commit
    #: together, and the COMPLETE proposal is re-validated against the
    #: frozen step first. An already-bound step is OBSERVED - after its
    #: stored task is re-proved - and never replaced.
    #: Returns ("created" | "observed" | "conflict" | "inactive" |
    #: "unknown", task_id); task_id is empty unless a task is named.
    def materialize_chain_step(
        self,
        session_id: str,
        chain_id: str,
        chain_digest: str,
        step_index: int,
        task: Task,
    ) -> tuple[str, str]: ...

    #: The ONLY way chain progress changes. The stored task status is
    #: re-read and its chain authority re-proved inside the transaction.
    #: Returns "advanced" | "completed" | "failed" | "cancelled" |
    #: "blocked" | "conflict" | "inactive" | "unbound" | "unknown".
    def advance_chain_step(
        self, session_id: str, chain_id: str, chain_digest: str,
        step_index: int,
    ) -> str: ...

    #: The ONLY way a chain reaches CANCELLED. Completed steps are kept.
    #: Returns "cancelled" | "terminal" | "conflict" | "unknown".
    def cancel_chain(self, session_id: str, chain_id: str) -> str: ...

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

    def execution_cancellation_requested(
        self, session_id: str, task_id: str
    ) -> bool:
        """Whether this exact task is authoritatively CANCEL_REQUESTED.

        The NARROWEST possible read, and deliberately so. Its only caller is
        the owner-local stop watcher: a thread that runs beside a blocking
        ``connector.execute`` and needs one bit - has the operator asked for
        this to stop - polled repeatedly while an external call is in
        progress. Everything a richer read would carry (the objective, the
        progress, the persisted document, the connector metadata) is both
        useless to that decision and a way for a corrupted document to reach
        a code path that touches a live external operation.

        So it answers from the task row's own columns and nothing else. No
        ``state_json`` is decoded, no summary is projected, and no
        transaction is held open across polls.

        ``True`` only when the row exists, its ``session_id`` column is the
        caller's, and its ``status`` column is exactly ``CANCEL_REQUESTED``.
        A missing task, a foreign task and every other status are all
        ``False`` - one answer, disclosing nothing about what else might be
        true.

        Reads only. It may never mutate anything, and a caller may never
        treat it as authority over lifecycle: it reports intent, not outcome.
        """
        ...

    def cancel_task(
        self, session_id: str, task_id: str
    ) -> CancellationOutcome:
        """Atomically finalize a cancellation in one transaction.

        THE REQUESTED ``task_id`` IS THE ONLY THING THAT SELECTS A ROW. Nothing
        decoded from a persisted document - not an embedded task id, not a
        status, not a stage, not an embedded session id - may choose or
        redefine which row is read or written. A caller-held ``Task`` cannot be
        passed at all, because being able to pass one was the defect.

        Inside the write transaction, before any mutation, the requested task
        and the supplied session are each rebound to their own rows: the row
        exists, its ``session_id`` is the caller's, its document decodes to an
        exact dict of the complete canonical shape, and the embedded id, status
        and stage agree with the columns. Any mismatch, malformed document or
        foreign ownership returns ONE bounded, non-disclosing refusal having
        written nothing and raised nothing.

        Verdicts: ``"unknown_session"``, ``"not_found"`` (also every identity
        and shape refusal - which row a document really describes is not
        something a refusal may reveal), ``"terminal"`` (already immutable, no
        write), ``"cancelled"`` (finalized: nothing was ever claimed, so there
        is nothing running to stop), ``"cancelling"`` (an authoritative
        IN_FLIGHT attempt exists: the intent is durable, the cancellation is
        NOT final, and the execution owner concludes it), ``"in_doubt"`` (the
        attempt is UNKNOWN_OUTCOME, so no cancellation verdict is truthful), or
        ``"lost"`` (the task changed owner; nothing was written).

        There is deliberately no argument for "the connector confirmed the
        stop": nothing in S2A can hold that evidence, because S2A signals no
        connector. Ownership is re-proved and the task write is guarded on it,
        so a finalization racing an ownership change loses rather than taking
        the task back.
        """
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

    def abandon_unknown_attempt(
        self,
        session_id: str,
        task_id: str,
        attempt_id: str,
        recovery: AttemptRecovery,
    ) -> AttemptRecoveryOutcome:
        """Atomically accept that an UNKNOWN-outcome attempt never resolved.

        THE operator recovery for the one state H2 leaves with no exit. An
        attempt marked ``UNKNOWN_OUTCOME`` may never be replayed and never
        concludes on its own, so without this the task, and any chain waiting
        on it, stay blocked forever. This is the human saying "that result
        will never be established"; it is not a retry and not a verdict about
        what the external world did.

        What it must prove, in ONE transaction, before writing anything: the
        attempt exists and its own ``session_id``/``task_id`` columns match
        the caller's; the task's authoritative owner is ``session_id``; the
        attempt's state is EXACTLY ``UNKNOWN_OUTCOME``; and the task is not
        already terminal. Any failure returns a refusal verdict having
        written nothing at all.

        What it then does, atomically: the task becomes terminally FAILED
        carrying the ``EXECUTION_UNKNOWN_OUTCOME`` diagnostic, a bounded
        recovery checkpoint and event are recorded, and an ACTIVE monetary
        reservation for that attempt becomes UNRESOLVED.

        What it must NEVER do: reach a connector, replay, return the attempt
        to a claimable state, invent a COMPLETED outcome, consume a grant,
        create a reservation, or release monetary exposure. The attempt stays
        ``UNKNOWN_OUTCOME`` permanently - that is the durable record that
        something may have happened out there - and an UNRESOLVED reservation
        keeps counting against the monthly position, because abandoning an
        unknown outcome is an admission of uncertainty, not its resolution.

        A RECONCILED reservation is never reopened. The chain is never
        written: the ordinary chain authority observes the FAILED task on its
        next pass and terminalizes itself, exactly as it does for any other
        failure.
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
        task lifecycle (reject if cancelled/terminal), arbitrate CHAIN
        authority against this claim, rebuild the authoritative ledger
        snapshot, verify month/exposure against ``billing``, recompute the
        projection, require the exact approved cost-override grant when it
        crosses the hard limit, transition PREPARED -> IN_FLIGHT, consume every
        required one-shot grant and create the attempt-bound reservation - all
        or nothing.

        AUTHORITATIVE OWNERSHIP is the first thing this transaction must
        settle, and every store must settle it the same way: the owner the
        store itself records for ``task_id`` must equal ``session_id``, or the
        outcome is ``LOST``. It is proven BEFORE the task is classified as
        ordinary, bound, unbound or chain-associated, before any chain is
        read, before ownership is granted, before a grant is consumed and
        before a reservation exists. An attempt row is not evidence of this:
        its session id is whatever a caller wrote next to the task, so a store
        that trusted it - or that asked about ownership only on its chain path
        - would let one session claim execution of another session's ordinary
        task, which is the default shape of every task.

        CHAIN ARBITRATION is likewise part of the contract, not an
        implementation detail, and every store must honour it EXACTLY.

        A task is CHAIN-ASSOCIATED when EITHER it carries a chain binding OR
        the chain of its authoritative owning session names that exact
        ``task_id`` in any step slot. Association is never decided from the
        task's own binding alone: a store that did so would let a chain step
        become an ordinary task by having its binding removed or corrupted -
        and since a malformed binding loads as canonical ABSENCE, that is a
        fail-open a single damaged byte can reach.

        Chain association is read from the OWNING session's chain, never from
        the caller's, so a slot in a caller-controlled chain can never
        manufacture ownership of a task it does not own.

        For a chain-associated task this same transaction must prove: the
        binding is PRESENT and well formed; that owning session's chain is
        ACTIVE and usable; the binding
        names that chain's id AND its current declaration digest; the bound
        step index is the chain's CURRENT cursor; and the cursor's slot holds
        exactly ``task_id``. Any failure is ``LOST``, written nothing - in
        particular a slot-named task whose binding is absent or malformed is
        ``LOST``, never treated as ordinary. A task that is neither bound nor
        named by any slot is unaffected.

        This is what makes cancellation and execution ONE decision. A store
        that checks the chain outside this transaction leaves the window a
        chain cancellation can be executed through: cancelling never rewrites
        the task rows of steps it stopped, so a task-only check cannot see it.
        Cancellation-first means no attempt is ever claimed and no connector
        is ever reached; claim-first means the attempt stands and a later
        cancellation stops the chain advancing without pretending the claimed
        attempt never started.

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
        decision: DecisionRecord | None,
        usage: UsageRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Finalize a task; True only when this writer owned the transition.

        The guarded terminal task write, the session update, the decision
        record, the final usage bookkeeping and the terminal event are one
        all-or-nothing operation. False means another terminal writer already
        won: nothing was written and the caller must report authoritative state.

        ``decision`` is ``None`` for a terminal task that records no
        judgement for later work to inherit; no decision row is written and
        every other part of the transaction is unchanged.
        """
        ...


@runtime_checkable
class ResultVerifier(Protocol):
    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord: ...
