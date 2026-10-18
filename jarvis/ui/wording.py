"""Gate 6G K3: honest indicators - the window's ONE date formatter and its
words for Core's state codes. Presentation only: no code, id or stored value
changes; QML reads both through ``i18n`` (``i18n.when``, ``i18n.word``).

* ``human_date`` - a stored moment as a person says it: today / yesterday /
  tomorrow, else "3 Oct" ("3 Eki"), with the year only when it is not this
  year. ``now`` is passed in, so a fixed clock decides midnight; the moment is
  read in ``now``'s timezone. Unreadable: "" (the caller decides what shows).
* ``code_word`` - a Core code as a word. A code with no word yet never shows
  bare and never as a guess: "Unrecognized (<code>)" keeps it exact; an
  unknown capability reads "Other capability" (no raw id in the work panel).
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
    # V2.1a M4: a typed record's kind (contracts.MemoryRecordKind).
    "memory_record_kind": {
        "fact": QT_TRANSLATE_NOOP("Wording", "Fact"),
        "decision": QT_TRANSLATE_NOOP("Wording", "Decision"),
        "rule": QT_TRANSLATE_NOOP("Wording", "Rule"),
        "lesson": QT_TRANSLATE_NOOP("Wording", "Lesson"),
        "preference": QT_TRANSLATE_NOOP("Wording", "Preference"),
    },
    # V2.1a M4: a typed record's current status (contracts.MemoryRecordStatus).
    "memory_record_status": {
        "pending": QT_TRANSLATE_NOOP("Wording", "Awaiting your approval"),
        "approved": QT_TRANSLATE_NOOP("Wording", "Approved"),
        "rejected": QT_TRANSLATE_NOOP("Wording", "Rejected"),
        "retracted": QT_TRANSLATE_NOOP("Wording", "Taken back"),
        "superseded": QT_TRANSLATE_NOOP("Wording", "Replaced by a newer record"),
        "expired": QT_TRANSLATE_NOOP("Wording", "Expired"),
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
        "pause_requested": QT_TRANSLATE_NOOP("Wording", "Pausing"),
        "paused": QT_TRANSLATE_NOOP("Wording", "Paused"),
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
    # G6CAPWORD (Gate 6G A-8): the work panel's "Used in this chat" - every capability a connector
    # declares (intent.NL_CAPABILITIES, the connectors' manifests, web_research, intent_understanding).
    "capability": {
        "code_analysis": QT_TRANSLATE_NOOP("Wording", "Code analysis"),
        "code_planning": QT_TRANSLATE_NOOP("Wording", "Code planning"),
        "file_read": QT_TRANSLATE_NOOP("Wording", "File reading"),
        "file_write": QT_TRANSLATE_NOOP("Wording", "File writing"),
        "web_research": QT_TRANSLATE_NOOP("Wording", "Web research"),
        "intent_understanding": QT_TRANSLATE_NOOP("Wording", "Understanding the request"),
        "project_folder_create": QT_TRANSLATE_NOOP("Wording", "Project folder setup"),
        "gmail_thread_list": QT_TRANSLATE_NOOP("Wording", "Mail list"),
        "gmail_search": QT_TRANSLATE_NOOP("Wording", "Mail search"),
        "gmail_message_read": QT_TRANSLATE_NOOP("Wording", "Mail reading"),
        "gmail_thread_read": QT_TRANSLATE_NOOP("Wording", "Mail thread reading"),
        "gmail_draft_create": QT_TRANSLATE_NOOP("Wording", "Mail draft"),
        "gmail_send": QT_TRANSLATE_NOOP("Wording", "Mail sending"),
        "github_repository_read": QT_TRANSLATE_NOOP("Wording", "GitHub repository reading"),
        "github_file_read": QT_TRANSLATE_NOOP("Wording", "GitHub file reading"),
        "github_issue_list": QT_TRANSLATE_NOOP("Wording", "GitHub issue list"),
        "github_issue_read": QT_TRANSLATE_NOOP("Wording", "GitHub issue reading"),
        "github_pull_request_list": QT_TRANSLATE_NOOP("Wording", "GitHub pull request list"),
        "github_pull_request_read": QT_TRANSLATE_NOOP("Wording", "GitHub pull request reading"),
        "github_pull_request_files": QT_TRANSLATE_NOOP("Wording", "GitHub pull request files"),
        "github_commit_list": QT_TRANSLATE_NOOP("Wording", "GitHub commit list"),
        "github_commit_read": QT_TRANSLATE_NOOP("Wording", "GitHub commit reading"),
        "browser_navigate": QT_TRANSLATE_NOOP("Wording", "Browser navigation"),
        "browser_page_read": QT_TRANSLATE_NOOP("Wording", "Browser page reading"),
        "browser_wait": QT_TRANSLATE_NOOP("Wording", "Browser wait"),
        "browser_click": QT_TRANSLATE_NOOP("Wording", "Browser click"),
        "browser_type": QT_TRANSLATE_NOOP("Wording", "Browser typing"),
        "browser_tab_list": QT_TRANSLATE_NOOP("Wording", "Browser tab list"),
        "browser_tab_open": QT_TRANSLATE_NOOP("Wording", "Browser tab opening"),
        "browser_tab_switch": QT_TRANSLATE_NOOP("Wording", "Browser tab switch"),
        "browser_tab_close": QT_TRANSLATE_NOOP("Wording", "Browser tab closing"),
        "desktop_app_launch": QT_TRANSLATE_NOOP("Wording", "App launch"),
        "desktop_window_list": QT_TRANSLATE_NOOP("Wording", "Window list"),
        "desktop_window_inspect": QT_TRANSLATE_NOOP("Wording", "Window inspection"),
        "desktop_window_focus": QT_TRANSLATE_NOOP("Wording", "Window focus"),
        "desktop_click": QT_TRANSLATE_NOOP("Wording", "Desktop click"),
        "desktop_type": QT_TRANSLATE_NOOP("Wording", "Desktop typing"),
    },
}


UNRECOGNIZED = QT_TRANSLATE_NOOP("Wording", "Unrecognized (%1)")

#: A vocabulary whose unknown code reads as one plain word, never the raw id.
FALLBACK_WORDS = {"capability": QT_TRANSLATE_NOOP("Wording", "Other capability")}


def code_word(kind: str, code: str) -> str:
    """The word for ``code`` in the ``kind`` vocabulary; empty stays empty."""
    code = str(code or "")
    if not code:
        return ""
    word = CODE_WORDS.get(kind, {}).get(code) or FALLBACK_WORDS.get(kind)
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
