"""Pure memory admission guard: taint, provenance, instruction gate (V2.1 SEC).

Core is the authority (I1, I13, I14, I17): this module decides, without any
model, I/O, database or clock, what may happen to a memory candidate. Every
unknown input takes the dirtier or stricter branch: an unknown capability is
``web``, an unknown taint is not clean, an unknown provenance or scope or
classification never reaches ``APPROVED``.

Text checks run on folded text: Turkish I/İ/ı -> i, lowercase, NFKD without
marks (the fold of ``memory._fold_search_text``), plus invisible format
characters (zero-width, soft hyphen, BOM) dropped so they cannot split a
keyword. Every pattern is a fixed word-boundary regex, linear in the input.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .contracts import (
    DataClassification,
    MemoryRecordKind,
    MemoryRecordScopeKind,
    MemoryRecordStatus,
)


class SessionTaint(str, Enum):
    """The dirtiest input a session saw, cleanest first (I13)."""

    USER_TYPED = "user_typed"
    LOCAL_TRUSTED_FILE = "local_trusted_file"
    LEGACY = "legacy"
    IMPORT = "import"
    CONNECTOR = "connector"
    WEB = "web"


_TAINT_ORDER = tuple(SessionTaint)
_CLEAN_TAINTS = frozenset({SessionTaint.USER_TYPED, SessionTaint.LOCAL_TRUSTED_FILE})


def _as_taint(value: object) -> SessionTaint:
    try:
        return SessionTaint(value)
    except (ValueError, TypeError):
        return SessionTaint.WEB  # unknown counts as web (I13)


def dirtiest(*taints: object) -> SessionTaint:
    """The dirtiest of ``taints``; unknown values and an empty call are ``web``."""
    if not taints:
        return SessionTaint.WEB
    return max((_as_taint(t) for t in taints), key=_TAINT_ORDER.index)


def is_clean(taint: object) -> bool:
    """True only for the ``user_typed`` and ``local_trusted_file`` members
    themselves; their plain-string values do not count."""
    return type(taint) is SessionTaint and taint in _CLEAN_TAINTS


#: Every capability id in jarvis/ (gmail.py, github.py, browser.py, desktop.py,
#: intent.py, connectors/, contracts.py, core.py) and the taint its result
#: carries. Anything not listed - including None and "" - is ``web``.
_CAPABILITY_TAINT = {
    # local file reads/writes and Core-made local results
    "file_read": SessionTaint.LOCAL_TRUSTED_FILE,
    "file_write": SessionTaint.LOCAL_TRUSTED_FILE,
    "project_folder_create": SessionTaint.LOCAL_TRUSTED_FILE,
    # import
    "import": SessionTaint.IMPORT,
    "memory_import": SessionTaint.IMPORT,
    # connectors, mail, model/agent calls
    "intent_understanding": SessionTaint.CONNECTOR,
    "code_analysis": SessionTaint.CONNECTOR,
    "code_planning": SessionTaint.CONNECTOR,
    "memory_capture": SessionTaint.CONNECTOR,
    "gmail_thread_list": SessionTaint.CONNECTOR,
    "gmail_search": SessionTaint.CONNECTOR,
    "gmail_message_read": SessionTaint.CONNECTOR,
    "gmail_thread_read": SessionTaint.CONNECTOR,
    "gmail_attachment_read": SessionTaint.CONNECTOR,
    "gmail_draft_create": SessionTaint.CONNECTOR,
    "gmail_send": SessionTaint.CONNECTOR,
    "github_repository_read": SessionTaint.CONNECTOR,
    "github_file_read": SessionTaint.CONNECTOR,
    "github_issue_list": SessionTaint.CONNECTOR,
    "github_issue_read": SessionTaint.CONNECTOR,
    "github_pull_request_list": SessionTaint.CONNECTOR,
    "github_pull_request_read": SessionTaint.CONNECTOR,
    "github_pull_request_files": SessionTaint.CONNECTOR,
    "github_commit_list": SessionTaint.CONNECTOR,
    "github_commit_read": SessionTaint.CONNECTOR,
    "github_code_search": SessionTaint.CONNECTOR,
    "github_issue_create": SessionTaint.CONNECTOR,
    "github_issue_comment_create": SessionTaint.CONNECTOR,
    "github_pull_request_comment_create": SessionTaint.CONNECTOR,
    # web, browser, and desktop (screen and clipboard text can be anything)
    "web_research": SessionTaint.WEB,
    "browser_page_read": SessionTaint.WEB,
    "browser_tab_list": SessionTaint.WEB,
    "browser_wait": SessionTaint.WEB,
    "browser_navigate": SessionTaint.WEB,
    "browser_click": SessionTaint.WEB,
    "browser_type": SessionTaint.WEB,
    "browser_tab_open": SessionTaint.WEB,
    "browser_tab_switch": SessionTaint.WEB,
    "browser_tab_close": SessionTaint.WEB,
    "browser_script_evaluate": SessionTaint.WEB,
    "browser_download": SessionTaint.WEB,
    "browser_upload": SessionTaint.WEB,
    "browser_screenshot": SessionTaint.WEB,
    "browser_cookie_read": SessionTaint.WEB,
    "desktop_window_list": SessionTaint.WEB,
    "desktop_window_inspect": SessionTaint.WEB,
    "desktop_app_launch": SessionTaint.WEB,
    "desktop_window_focus": SessionTaint.WEB,
    "desktop_click": SessionTaint.WEB,
    "desktop_type": SessionTaint.WEB,
    "desktop_screenshot": SessionTaint.WEB,
    "desktop_clipboard_read": SessionTaint.WEB,
    "desktop_clipboard_write": SessionTaint.WEB,
    "desktop_window_close": SessionTaint.WEB,
    "desktop_window_move": SessionTaint.WEB,
    "desktop_drag": SessionTaint.WEB,
    "desktop_field_value_read": SessionTaint.WEB,
    "desktop_command_run": SessionTaint.WEB,
    "desktop_key": SessionTaint.WEB,
}


def taint_for_capability(capability: str | None) -> SessionTaint:
    """The taint of one tool result by its capability id; unknown -> ``web``.

    A user's own typed turn is ``user_typed`` by its turn kind, never by a
    capability: an empty capability is unknown here.
    """
    if type(capability) is not str:
        return SessionTaint.WEB
    return _CAPABILITY_TAINT.get(capability, SessionTaint.WEB)


class Provenance(str, Enum):
    USER_STATED = "user_stated"
    MODEL_INFERRED = "model_inferred"
    EXTERNAL = "external"
    IMPORT = "import"
    LEGACY = "legacy"


_TURKISH_I_FOLD = {ord("I"): "i", ord("İ"): "i", ord("ı"): "i"}
_MARKS = frozenset({"Mn", "Me"})
#: Letters that render blank (Hangul and halfwidth fillers, braille blank).
_FILLERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")


def _fold(text: str, gap: str = "") -> str:
    """``memory._fold_search_text`` hardened: NFKD before lowercasing (so math
    or fullwidth capitals fold), every mark dropped (also class-0 ones such as
    U+034F and U+FE0F), format chars (Cf) and blank fillers -> ``gap``, every
    line break -> \\n."""
    decomposed = unicodedata.normalize(
        "NFKD", unicodedata.normalize("NFKD", text).translate(_TURKISH_I_FOLD).lower()
    )
    folded = "".join(
        gap if ch in _FILLERS or unicodedata.category(ch) == "Cf" else ch
        for ch in decomposed
        if unicodedata.category(ch) not in _MARKS
    )
    return "\n".join(folded.splitlines())


#: (pattern id, regex on folded text). Turkish verbs take suffixes, so
#: 'hatirla' and 'guvenilir kaynak' accept them ('hatırlayın', 'kaynaktır');
#: 'ileride' and 'her zaman' are whole words on purpose: 'ilerideki toplantı'
#: (the upcoming meeting) and 'her zamanki gibi' (as usual) are not orders.
_INSTRUCTION_PATTERNS = tuple(
    (pattern_id, re.compile(regex, re.MULTILINE))
    for pattern_id, regex in (
        ("en_remember", r"\bremember\b"),
        ("en_from_now_on", r"\bfrom\s+now\s+on\b"),
        ("en_always", r"\balways\b"),
        ("en_trusted_source", r"\btrusted\s+sources?\b"),
        ("en_in_future_conversations", r"\bin\s+future\s+conversations?\b"),
        ("tr_hatirla", r"\bhatirla\w*"),
        ("tr_bundan_sonra", r"\bbundan\s+sonra\b"),
        ("tr_her_zaman", r"\bher\s+zaman\b"),
        ("tr_guvenilir_kaynak", r"\bguvenilir\s+kaynak\w*"),
        ("tr_ileride", r"\bileride\b"),
        ("marker_conversation", r"<\s*/?\s*conversation\b"),
        ("marker_system", r"<\s*/?\s*system\b"),
        ("marker_human", r"^[^\S\n]*human\s*:"),
        ("marker_assistant", r"^[^\S\n]*assistant\s*:"),
    )
)


def instruction_likeness(text: str) -> tuple[str, ...]:
    """Ids of every instruction pattern ``text`` matches, in table order (I14).

    Invisible chars are tried both dropped ('re\\u200bmember') and as a space
    ('her\\u180ezaman'); a hit in either counts.
    """
    variants = (_fold(text), _fold(text, " "))
    return tuple(
        pid for pid, pattern in _INSTRUCTION_PATTERNS
        if any(pattern.search(v) for v in variants)
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", _fold(text)))


#: Tokens that flip or pin a statement's meaning: each one in the candidate
#: must also be in the turn, so 80% overlap cannot drop a 'not' or change a
#: number. ponytail: Turkish verb negation (-me/-ma) is inside the word and
#: only caught by the 80% overlap; a morphology check if that matters.
_MEANING_TOKENS = frozenset({
    "not", "no", "never", "cannot", "dont", "don", "t",
    "degil", "asla", "yok", "hic", "hicbir",
})


def verify_user_stated(candidate_text: str, user_turn_text: str) -> bool:
    """True when >= 80% of the candidate's folded tokens are in this ONE user
    turn and every negation or number token of the candidate is too.

    A negation in the turn must be in the candidate as well: dropping the
    user's 'don't' flips the meaning (an unrelated negation in the turn only
    costs user_stated, so the row stays PENDING)."""
    if type(candidate_text) is not str or type(user_turn_text) is not str:
        return False
    candidate, turn = _tokens(candidate_text), _tokens(user_turn_text)
    if not candidate:
        return False
    if any(
        (tok in _MEANING_TOKENS or any(ch.isdigit() for ch in tok)) and tok not in turn
        for tok in candidate
    ):
        return False
    if (turn & _MEANING_TOKENS) - candidate:
        return False
    return len(candidate & turn) >= 0.8 * len(candidate)


#: Folded tokens of the allowed operational topics: reply language, output
#: format, and tools. Exact tokens (with the common Turkish inflections
#: listed); words with an everyday meaning ('kisa' short, 'tablo' painting,
#: 'table', 'shell', 'python', 'arac' vehicle, 'dil' tongue) are left out, a
#: missed preference is refused, never auto-saved. ponytail: 'english' and
#: 'turkish' still hit 'Turkish coffee'; rule 9 keeps those PENDING unless
#: autosave is on.
_OPERATIONAL_TOKENS = frozenset({
    # language
    "language", "english", "turkish", "dilde", "dilinde",
    "turkce", "turkcesi", "ingilizce", "ingilizcesi",
    # format
    "format", "formatting", "markdown", "json", "concise", "verbose",
    "headings", "emoji", "emojis", "bicim", "bicimde", "bicimi", "maddeler",
    "maddeli", "kisaca", "ayrintili", "detayli",
    # tools
    "powershell", "bash", "pip", "unittest", "pytest", "ruff", "vscode",
})


def is_operational_preference(text: str) -> bool:
    """True only when ``text`` names a language, format or tool topic (I17)."""
    if type(text) is not str:
        return False
    return not _tokens(text).isdisjoint(_OPERATIONAL_TOKENS)


def content_hash(text: str) -> str:
    """Hex sha256 of the folded text with whitespace collapsed."""
    return hashlib.sha256(" ".join(_fold(text).split()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordGuard:
    """The guard facts Core stores beside one record (memory_record_guard)."""

    provenance: Provenance
    session_taint: SessionTaint
    instruction_like: tuple[str, ...]
    proposed_kind: MemoryRecordKind
    content_hash: str

    def __post_init__(self) -> None:
        if type(self.provenance) is not Provenance:
            raise ValueError("provenance must be a Provenance")
        if type(self.session_taint) is not SessionTaint:
            raise ValueError("session_taint must be a SessionTaint")
        if type(self.instruction_like) is not tuple or not all(
            type(p) is str for p in self.instruction_like
        ):
            raise ValueError("instruction_like must be a tuple of pattern ids")
        if type(self.proposed_kind) is not MemoryRecordKind:
            raise ValueError("proposed_kind must be a MemoryRecordKind")
        if type(self.content_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise ValueError("content_hash must be a hex sha256")


#: The admission verdict that stores nothing.
REFUSE = "refuse"
LESSON_EXPIRES_IN_DAYS = 90
_NON_SENSITIVE = frozenset({DataClassification.PUBLIC, DataClassification.INTERNAL})


@dataclass(frozen=True)
class Admission:
    final_kind: MemoryRecordKind
    status: MemoryRecordStatus | str  # a MemoryRecordStatus or REFUSE
    reason: str
    expires_in_days: int | None = None


def decide_admission(
    kind: MemoryRecordKind,
    provenance: Provenance,
    taint: SessionTaint,
    instruction_like: tuple[str, ...],
    scope_kind: MemoryRecordScopeKind,
    classification: DataClassification,
    *,
    operational: bool = False,
    preference_autosave: bool = False,
) -> Admission:
    """Core's admission decision for one candidate; the first matching rule wins.

    ``operational`` is ``is_operational_preference(text)`` for a PREFERENCE;
    left False, every PREFERENCE is refused. Nothing unknown is APPROVED.
    """
    if type(kind) is not MemoryRecordKind:
        raise ValueError("kind must be a MemoryRecordKind")
    pending, approved = MemoryRecordStatus.PENDING, MemoryRecordStatus.APPROVED
    if instruction_like:
        return Admission(MemoryRecordKind.RULE, pending, "instruction_like")
    if not is_clean(taint):
        return Admission(MemoryRecordKind.RULE, pending, "tainted")
    # Members are compared by identity: a plain or str-subclass value equal
    # to a member is unknown and never reaches APPROVED.
    if provenance is Provenance.MODEL_INFERRED and kind is MemoryRecordKind.FACT:
        return Admission(kind, REFUSE, "model_inferred_fact")
    if kind is MemoryRecordKind.PREFERENCE and operational is not True:
        return Admission(kind, REFUSE, "preference_not_operational")
    if scope_kind is not MemoryRecordScopeKind.PROJECT:
        return Admission(kind, pending, "not_project_scope")
    if not any(classification is c for c in _NON_SENSITIVE):
        return Admission(kind, pending, "sensitive")
    if provenance is not Provenance.USER_STATED:
        # model_inferred, and external/import/legacy (I11: imports stay PENDING)
        return Admission(kind, pending, "not_user_stated")
    if kind is MemoryRecordKind.FACT:
        return Admission(kind, approved, "user_stated_fact")
    if kind is MemoryRecordKind.LESSON:
        return Admission(kind, approved, "user_stated_lesson", LESSON_EXPIRES_IN_DAYS)
    if kind is MemoryRecordKind.PREFERENCE and preference_autosave is True:
        return Admission(kind, approved, "operational_preference_autosave")
    return Admission(kind, pending, "needs_approval")
