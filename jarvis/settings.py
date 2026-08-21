"""Safe local settings and cost policy."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from jarvis.contracts import (
    MONTHLY_HARD_LIMIT_NANO_USD,
    BillingIntegrityError,
    PermissionMode,
    nano_usd,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: The fixed monthly monetary hard limit. Changing it is a user decision, not
#: a configuration knob, so it is validated exactly rather than by range. The
#: canonical value lives in ``jarvis.contracts`` as exact nano-USD, because the
#: storage boundary enforces the same cap and never trusts a caller-supplied
#: larger limit.
MONTHLY_HARD_LIMIT_USD = float(MONTHLY_HARD_LIMIT_NANO_USD) / 1_000_000_000


def _finite(name: str, value: float) -> float:
    """Reject a monetary/limit setting that is not an ordinary finite number.

    Booleans, NaN and both infinities are refused here: every ordinary range
    comparison below silently passes NaN, so a NaN limit would otherwise look
    like a satisfied policy.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class Settings:
    memory_database: Path = PROJECT_ROOT / "data" / "jarvis.db"
    permission_mode: PermissionMode = PermissionMode.SESSION
    monthly_target_usd: float = MONTHLY_HARD_LIMIT_USD
    monthly_hard_limit_usd: float = MONTHLY_HARD_LIMIT_USD
    paid_api_enabled: bool = False
    paid_api_approval_id: str | None = None
    approval_ttl_minutes: int = 30
    claude_call_budget_usd: float = 0.25

    def __post_init__(self) -> None:
        # Finiteness first: NaN passes every ordinary comparison below.
        target = _finite("monthly_target_usd", self.monthly_target_usd)
        limit = _finite("monthly_hard_limit_usd", self.monthly_hard_limit_usd)
        _finite("claude_call_budget_usd", self.claude_call_budget_usd)
        if target < 0:
            raise ValueError("monthly_target_usd cannot be negative")
        try:
            limit_nano = nano_usd(limit, "monthly_hard_limit_usd")
        except BillingIntegrityError as exc:
            raise ValueError(str(exc)) from exc
        if limit_nano != MONTHLY_HARD_LIMIT_NANO_USD:
            raise ValueError("The hard limit is fixed at 20 USD without a new user decision")
        if target > limit:
            raise ValueError("monthly_target_usd cannot exceed the hard limit")
        if self.paid_api_enabled and not self.paid_api_approval_id:
            raise ValueError("Enabling paid APIs requires an explicit approval id")
        if self.approval_ttl_minutes <= 0:
            raise ValueError("approval_ttl_minutes must be positive")
        if self.claude_call_budget_usd <= 0:
            raise ValueError("claude_call_budget_usd must be positive")

    def enable_paid_api(self, approval_id: str) -> "Settings":
        if not approval_id.strip():
            raise ValueError("approval_id is required")
        return replace(
            self, paid_api_enabled=True, paid_api_approval_id=approval_id.strip()
        )

