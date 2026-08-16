"""Safe local settings and cost policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from jarvis.contracts import PermissionMode


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Settings:
    memory_database: Path = PROJECT_ROOT / "data" / "jarvis.db"
    permission_mode: PermissionMode = PermissionMode.SESSION
    monthly_target_usd: float = 20.0
    monthly_hard_limit_usd: float = 20.0
    paid_api_enabled: bool = False
    paid_api_approval_id: str | None = None
    approval_ttl_minutes: int = 30
    claude_call_budget_usd: float = 0.25

    def __post_init__(self) -> None:
        if self.monthly_target_usd < 0:
            raise ValueError("monthly_target_usd cannot be negative")
        if self.monthly_hard_limit_usd != 20.0:
            raise ValueError("The hard limit is fixed at 20 USD without a new user decision")
        if self.monthly_target_usd > self.monthly_hard_limit_usd:
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

