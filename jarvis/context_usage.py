"""How full a conversation's context is: a pure calculator, no I/O.

Feeds the V2.1a context indicator (K3: honest). ``used_tokens`` comes from the
same conservative estimator ``assemble_context`` budgets with; ``max_tokens``
is what the provider's ``ContextProfile`` leaves after the output reserve. An
unknown limit yields ``ratio=None`` and ``label_key="bilinmiyor"``, never a
made-up number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .conversation import ESTIMATOR, RAW_TURNS_MAX, ContextProfile

#: At or above this share the indicator warns.
HIGH_RATIO = 0.8


@dataclass(frozen=True)
class ContextUsage:
    used_tokens: int
    max_tokens: int | None
    ratio: float | None
    turns_until_autocompact: int
    label_key: str  # "bilinmiyor" | "normal" | "yuksek" | "dolu"


def context_usage(raw_turn_count: int, assembled_chars: int, profile: ContextProfile | None, *,
                  assembled_tokens: int | None = None) -> ContextUsage:
    """``assembled_tokens`` (an exact count, when a provider reports one) wins
    over the character estimate of ``assembled_chars``."""
    if assembled_tokens is None:
        assembled_tokens = math.ceil(max(0, assembled_chars) / ESTIMATOR.chars_per_token)
    used = max(0, assembled_tokens)
    turns_left = max(0, RAW_TURNS_MAX - max(0, raw_turn_count))
    usable = None
    if profile is not None and profile.context_window_tokens > 0:
        usable = max(0, profile.context_window_tokens - profile.output_reserve_tokens)
    if not usable:
        return ContextUsage(used, None, None, turns_left, "bilinmiyor")
    ratio = min(1.0, used / usable)
    label = "dolu" if ratio >= 1.0 else "yuksek" if ratio >= HIGH_RATIO else "normal"
    return ContextUsage(used, usable, ratio, turns_left, label)


__all__ = ["HIGH_RATIO", "ContextUsage", "context_usage"]
