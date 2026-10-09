"""Gate 6G K3: honest indicators - the window's ONE date formatter and its
words for Core's state codes. Presentation only: no code, id or stored value
changes; QML reads both through ``i18n`` (``i18n.when``, ``i18n.word``).

* ``human_date`` - a stored moment as a person says it: today / yesterday /
  tomorrow, else "3 Oct" ("3 Eki"), with the year only when it is not this
  year. ``now`` is passed in, so a fixed clock decides midnight; the moment is
  read in ``now``'s timezone. Unreadable: "" (the caller decides what shows).
* ``code_word`` - a Core code as a word. A code with no word yet never shows
  bare and never as a guess: "Unrecognized (<code>)" keeps it exact.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from PySide6.QtCore import QDate, QLocale

from jarvis.ui.i18n import QT_TRANSLATE_NOOP, tx

#: Calendar-day distance -> its word.
DAY_WORDS = {
    -1: QT_TRANSLATE_NOOP("Wording", "yesterday"),
    0: QT_TRANSLATE_NOOP("Wording", "today"),
    1: QT_TRANSLATE_NOOP("Wording", "tomorrow"),
}

#: Core vocabularies the window shows (jarvis.contracts LoopStage, JobKind, ProjectMemoryKind, ...).
CODE_WORDS = {
    "stage": {
        "understand": QT_TRANSLATE_NOOP("Wording", "Understanding"),
        "context_retrieval": QT_TRANSLATE_NOOP("Wording", "Gathering context"),
        "clarify_or_visible_assumption": QT_TRANSLATE_NOOP("Wording", "Clarifying"),
        "plan": QT_TRANSLATE_NOOP("Wording", "Planning"),
        "risk_and_permission": QT_TRANSLATE_NOOP("Wording", "Checking permission"),
        "cost_preflight": QT_TRANSLATE_NOOP("Wording", "Checking cost"),
        "tool_selection": QT_TRANSLATE_NOOP("Wording", "Choosing a tool"),
        "execute": QT_TRANSLATE_NOOP("Wording", "Executing"),
        "verify": QT_TRANSLATE_NOOP("Wording", "Verifying"),
        "present": QT_TRANSLATE_NOOP("Wording", "Presenting"),
        "persist_decision_reason_next_step": QT_TRANSLATE_NOOP("Wording", "Recording"),
        "completed": QT_TRANSLATE_NOOP("Wording", "Done"),
        "failed": QT_TRANSLATE_NOOP("Wording", "Failed"),
        "cancelled": QT_TRANSLATE_NOOP("Wording", "Cancelled"),
    },
    "job_kind": {
        "one_time": QT_TRANSLATE_NOOP("Wording", "One-time"),
        "recurring": QT_TRANSLATE_NOOP("Wording", "Recurring"),
        "condition_watch": QT_TRANSLATE_NOOP("Wording", "Condition watch"),
    },
    "memory_kind": {
        "fact": QT_TRANSLATE_NOOP("Wording", "Fact"),
        "decision": QT_TRANSLATE_NOOP("Wording", "Decision"),
        "rule": QT_TRANSLATE_NOOP("Wording", "Rule"),
        "thread": QT_TRANSLATE_NOOP("Wording", "Thread"),
    },
    # K6: the pages' remaining codes (TaskStatus, DataClassification, MemoryLifecycle, MemoryOrigin).
    "task_status": {
        "pending": QT_TRANSLATE_NOOP("Wording", "Pending"),
        "awaiting_clarification": QT_TRANSLATE_NOOP("Wording", "Needs a detail"),
        "awaiting_approval": QT_TRANSLATE_NOOP("Wording", "Awaiting your approval"),
        "awaiting_cost_override": QT_TRANSLATE_NOOP("Wording", "Awaiting a cost decision"),
        "running": QT_TRANSLATE_NOOP("Wording", "Running"),
        "verifying": QT_TRANSLATE_NOOP("Wording", "Verifying"),
        "completed": QT_TRANSLATE_NOOP("Wording", "Completed"),
        "failed": QT_TRANSLATE_NOOP("Wording", "Failed"),
        "rejected": QT_TRANSLATE_NOOP("Wording", "Denied"),
        "cancel_requested": QT_TRANSLATE_NOOP("Wording", "Cancelling"),
        "cancelled": QT_TRANSLATE_NOOP("Wording", "Cancelled"),
    },
    "memory_classification": {
        "public": QT_TRANSLATE_NOOP("Wording", "Public"),
        "internal": QT_TRANSLATE_NOOP("Wording", "Internal"),
        "sensitive": QT_TRANSLATE_NOOP("Wording", "Sensitive"),
        "secret": QT_TRANSLATE_NOOP("Wording", "Secret"),
    },
    "memory_lifecycle": {
        "active": QT_TRANSLATE_NOOP("Wording", "In use"),
        "superseded": QT_TRANSLATE_NOOP("Wording", "Replaced by a newer record"),
        "retired": QT_TRANSLATE_NOOP("Wording", "Retired"),
    },
    "memory_origin": {
        "explicit_user": QT_TRANSLATE_NOOP("Wording", "Written by you"),
        "system_decision": QT_TRANSLATE_NOOP("Wording", "JARVIS decision"),
        "inferred": QT_TRANSLATE_NOOP("Wording", "Inferred"),
        "imported": QT_TRANSLATE_NOOP("Wording", "Imported"),
    },
}


UNRECOGNIZED = QT_TRANSLATE_NOOP("Wording", "Unrecognized (%1)")


def code_word(kind: str, code: str) -> str:
    """The word for ``code`` in the ``kind`` vocabulary; empty stays empty."""
    code = str(code or "")
    if not code:
        return ""
    word = CODE_WORDS.get(kind, {}).get(code)
    return tx("Wording", word) if word else tx("Wording", UNRECOGNIZED, code)


def _moment(value, zone: tzinfo | None) -> datetime | None:
    """An ISO text (naive = already local) or epoch seconds, in ``zone``."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, zone) if value > 0 else None
        text = str(value or "").strip()
        if not text:
            return None
        moment = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


def human_date(value, now: datetime, locale: str = "en_US", style: str = "day") -> str:
    """``value`` as a person reads it, relative to ``now`` (an aware moment).

    ``day``: "today" / "yesterday" / "tomorrow" / "3 Oct" / "3 Oct 2025";
    ``time``: the same, then " 14:05"; ``short``: "14:05" today, else ``day``.
    """
    moment = _moment(value, now.tzinfo)
    if moment is None:
        return ""
    clock = f"{moment:%H:%M}"
    days = (moment.date() - now.date()).days
    if style == "short" and days == 0:
        return clock
    word = DAY_WORDS.get(days)
    day = tx("Wording", word) if word else QLocale(locale).toString(
        QDate(moment.year, moment.month, moment.day), "d MMM" if moment.year == now.year else "d MMM yyyy")
    return f"{day} {clock}" if style == "time" else day
