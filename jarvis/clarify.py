"""Project-mode clarification contracts (PM06): ProjectBrief and the exit check.

Pure and deterministic; AgentCore.clarify_turn (PM07) drives it from chat, and
every model proposal (questions, slot values) passes through the parsers here
as data before it can touch a brief. The exit check
is fail-closed: every required slot filled, no open question, every acceptance
example convertible to a check (or marked manual, which forces the large
class), and a read-back of the *current* brief confirmed by the user in a
turn after it was shown. A model claiming it understood is not an input.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace

SLOTS = (
    "goal",
    "users",
    "platform",
    "target_location",
    "definition_of_done",
    "constraints",
    "out_of_scope",
    "data_sensitivity",
    "outgoing_or_money",
)

# Rounds are counted in total, initial round included: asking round 4 is
# refused and the run goes to needs_input.
MAX_ROUNDS = 3

NEEDS_INPUT = "needs_input"
CLARIFYING = "clarifying"
PLANNING = "planning"


@dataclass(frozen=True)
class AcceptanceExample:
    text: str
    check: str = ""  # how it becomes a deterministic check
    manual: bool = False  # "manual check by Salih"; forces the large class


@dataclass(frozen=True)
class ProjectBrief:
    goal: str = ""
    users: str = ""
    platform: str = ""
    target_location: str = ""
    definition_of_done: str = ""
    acceptance_examples: tuple[AcceptanceExample, ...] = ()
    constraints: str = ""
    out_of_scope: str = ""
    data_sensitivity: str = ""
    outgoing_or_money: bool | None = None  # None = not answered


def _example_ok(example: AcceptanceExample) -> bool:
    return bool(example.text.strip()) and (bool(example.check.strip()) or example.manual)


def empty_slots(brief: ProjectBrief) -> tuple[str, ...]:
    empty = []
    for slot in SLOTS:
        if slot == "outgoing_or_money":
            filled = isinstance(brief.outgoing_or_money, bool)
        elif slot == "definition_of_done":
            examples = brief.acceptance_examples
            filled = (
                bool(brief.definition_of_done.strip())
                and bool(examples)
                and all(_example_ok(e) for e in examples)
            )
        else:
            value = getattr(brief, slot)
            filled = isinstance(value, str) and bool(value.strip())
        if not filled:
            empty.append(slot)
    return tuple(empty)


def brief_hash(brief: ProjectBrief) -> str:
    payload = json.dumps(asdict(brief), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReadBack:
    brief_hash: str
    shown_turn: int


@dataclass(frozen=True)
class ClarifyState:
    brief: ProjectBrief
    rounds_asked: int = 0
    open_questions: tuple[str, ...] = ()
    readback: ReadBack | None = None
    confirmed_turn: int | None = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = ()
    large_forced: bool = False


def check_understood(state: ClarifyState) -> Verdict:
    reasons = [f"empty slot: {slot}" for slot in empty_slots(state.brief)]
    reasons += [f"open question: {q}" for q in state.open_questions]
    readback = state.readback
    if readback is None:
        reasons.append("read-back not shown")
    elif readback.brief_hash != brief_hash(state.brief):
        reasons.append("brief changed after read-back")
    elif state.confirmed_turn is None:
        reasons.append("read-back not confirmed")
    elif state.confirmed_turn <= readback.shown_turn:
        reasons.append("confirmation not in a later turn than the read-back")
    large = any(e.manual for e in state.brief.acceptance_examples)
    return Verdict(ok=not reasons, reasons=tuple(reasons), large_forced=large)


def next_step(state: ClarifyState) -> str:
    """planning once understood; needs_input when another round would pass the cap."""
    if check_understood(state).ok:
        return PLANNING
    if empty_slots(state.brief) or state.open_questions:
        if state.rounds_asked >= MAX_ROUNDS:
            return NEEDS_INPUT
    return CLARIFYING


@dataclass(frozen=True)
class Question:
    slot: str
    text: str


@dataclass(frozen=True)
class QuestionSet:
    round: int
    questions: tuple[Question, ...]

    def render(self) -> str:
        return "\n".join(f"{i}. {q.text.strip()}" for i, q in enumerate(self.questions, 1))


def validate_question_set(qs: QuestionSet, brief: ProjectBrief) -> None:
    """All questions go in one numbered message; follow-ups target empty slots only."""
    if not 1 <= qs.round <= MAX_ROUNDS:
        raise ValueError(f"round {qs.round} outside 1..{MAX_ROUNDS}: {NEEDS_INPUT}")
    if not qs.questions:
        raise ValueError("question set is empty")
    empty = set(empty_slots(brief))
    for q in qs.questions:
        if q.slot not in SLOTS:
            raise ValueError(f"unknown slot: {q.slot!r}")
        if not q.text.strip():
            raise ValueError(f"blank question for slot {q.slot}")
        if qs.round > 1 and q.slot not in empty:
            raise ValueError(f"follow-up round asks filled slot {q.slot}")


# PM07: the chat side. Everything below is deterministic; a model's proposal
# is only ever an argument to these parsers, never trusted as-is.

CONFIRM_PROMPT = "Bunu bir proje olarak başlatayım mı? (evet / hayır)"
MAX_TEXT = 2000  # one slot value or question, in characters
MAX_EXAMPLES = 20

_YES = frozenset({"evet", "yes", "olur", "tamam", "onaylıyorum", "onayla", "başlat"})
_NO = frozenset({"hayır", "no", "vazgeç", "iptal", "istemiyorum"})
_BUILD = re.compile(r"\b(yap|oluştur|geliştir|kur|yaz|başlat|build|create|make|develop)")
_THING = re.compile(r"\b(proje|uygulama|site|oyun|bot|program|app|website)")
_LABELS = {
    "goal": "Amaç",
    "users": "Kullanıcılar",
    "platform": "Platform",
    "target_location": "Hedef konum",
    "definition_of_done": "Bitti sayılması için",
    "constraints": "Kısıtlar",
    "out_of_scope": "Kapsam dışı",
    "data_sensitivity": "Veri hassasiyeti",
    "outgoing_or_money": "Dışarıya gönderim veya para",
}


def _fold(text: str) -> str:
    """Turkish-aware lower case (I -> ı, İ -> i), collapsed whitespace."""
    return " ".join(text.replace("İ", "i").replace("I", "ı").lower().split())


def looks_like_project(text: object) -> bool:
    """A build verb and a buildable thing in one message. Only ever earns the
    confirm question; misses fall through to ordinary chat (fail closed)."""
    if not isinstance(text, str):
        return False
    folded = _fold(text)
    return bool(_BUILD.search(folded) and _THING.search(folded))


def parse_yes_no(text: object) -> str | None:
    """'yes' / 'no' only for an exact short answer; anything else is None."""
    if not isinstance(text, str):
        return None
    folded = _fold(text).strip(" .!?,;:")
    if folded in _YES:
        return "yes"
    if folded in _NO:
        return "no"
    return None


# PM07B: leaving the dialog. A closed set of whole-message answers, like the
# router's L0 rule; "iptal etme" or a sentence around the word is not one.
_CANCEL = frozenset({
    "vazgeç", "vazgec", "vazgeçtim", "vazgectim", "vazgeçelim", "vazgecelim",
    "iptal", "iptal et", "iptal edin", "boşver", "bosver", "boş ver", "bos ver", "cancel",
})


def parse_cancel(text: object) -> bool:
    """True only when the whole message is a cancel word (case, I/İ and
    trailing punctuation tolerated; full-width forms folded by NFKC)."""
    if not isinstance(text, str):
        return False
    # "?" is kept: "iptal?" asks, it does not cancel.
    folded = _fold(unicodedata.normalize("NFKC", text)).strip(" .!,;:")
    return folded.replace("ı", "i") in _CANCEL


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(f"{what} must be a string of at most {MAX_TEXT} characters")
    return value


def fill_brief(brief: ProjectBrief, values: object) -> ProjectBrief:
    """Apply proposed slot values; any unknown key or wrong type refuses all."""
    if not isinstance(values, dict):
        raise ValueError("slot values must be a mapping")
    changes: dict[str, object] = {}
    for key, value in values.items():
        if key == "outgoing_or_money":
            if value is not None and not isinstance(value, bool):
                raise ValueError("outgoing_or_money must be true, false or null")
            changes[key] = value
        elif key == "acceptance_examples":
            if not isinstance(value, (list, tuple)) or len(value) > MAX_EXAMPLES:
                raise ValueError(f"acceptance_examples must be a list of at most {MAX_EXAMPLES}")
            examples = []
            for item in value:
                if not isinstance(item, dict) or not set(item) <= {"text", "check", "manual"}:
                    raise ValueError("acceptance example must be {text, check, manual}")
                manual = item.get("manual", False)
                if not isinstance(manual, bool):
                    raise ValueError("acceptance example manual must be a bool")
                examples.append(AcceptanceExample(
                    _text(item.get("text"), "example text"),
                    _text(item.get("check", ""), "example check"), manual,
                ))
            changes[key] = tuple(examples)
        elif key in SLOTS:
            changes[key] = _text(value, key)
        else:
            raise ValueError(f"unknown slot: {key!r}")
    return replace(brief, **changes)


def reopen_slots(brief: ProjectBrief, values: object) -> tuple[ProjectBrief, tuple[str, ...]]:
    """A read-back correction: the slots the proposal touches are emptied so
    they are asked again; the proposed values themselves are not applied."""
    fill_brief(brief, values)  # same validation as an answer
    touched = {"definition_of_done" if k == "acceptance_examples" else k for k in values}
    changes: dict[str, object] = {}
    for slot in touched:
        if slot == "outgoing_or_money":
            changes[slot] = None
        elif slot == "definition_of_done":
            changes.update(definition_of_done="", acceptance_examples=())
        else:
            changes[slot] = ""
    return replace(brief, **changes), tuple(s for s in SLOTS if s in touched)


def question_set(round_: int, proposed: object, brief: ProjectBrief) -> QuestionSet:
    """Parse and validate a proposed question list ([{slot, text}, ...])."""
    if not isinstance(proposed, (list, tuple)) or len(proposed) > len(SLOTS):
        raise ValueError(f"questions must be a list of at most {len(SLOTS)}")
    questions = []
    for item in proposed:
        if not isinstance(item, dict) or set(item) != {"slot", "text"}:
            raise ValueError("question must be {slot, text}")
        questions.append(Question(_text(item["slot"], "slot"), _text(item["text"], "question")))
    qs = QuestionSet(round_, tuple(questions))
    validate_question_set(qs, brief)
    return qs


def render_readback(brief: ProjectBrief) -> str:
    lines = ["Anladığım kadarıyla:"]
    for slot in SLOTS:
        if slot == "outgoing_or_money":
            value = "Evet" if brief.outgoing_or_money else "Hayır"
        else:
            value = getattr(brief, slot).strip()
        lines.append(f"- {_LABELS[slot]}: {value}")
        if slot == "definition_of_done":
            for example in brief.acceptance_examples:
                how = "elle kontrol" if example.manual else example.check.strip()
                lines.append(f"  - Örnek: {example.text.strip()} ({how})")
    lines.append(
        "Doğruysa ayrı bir mesajla \"evet\" yazın; düzeltmek istediğiniz bir şey "
        "varsa onu yazın."
    )
    return "\n".join(lines)


def state_to_dict(state: ClarifyState) -> dict:
    return asdict(state)


def state_from_dict(data: object) -> ClarifyState:
    """Rebuild a persisted state; anything malformed raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("clarify state must be a mapping")
    try:
        brief = fill_brief(ProjectBrief(), data["brief"])
        rounds = data["rounds_asked"]
        questions = tuple(_text(q, "open question") for q in data["open_questions"])
        readback = data["readback"]
        confirmed = data["confirmed_turn"]
        if readback is not None:
            readback = ReadBack(readback["brief_hash"], readback["shown_turn"])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed clarify state: {exc}") from exc
    for number in (rounds, confirmed, getattr(readback, "shown_turn", 0)):
        if number is not None and (isinstance(number, bool) or not isinstance(number, int)):
            raise ValueError("clarify state counters must be integers")
    if readback is not None and not isinstance(readback.brief_hash, str):
        raise ValueError("read-back hash must be a string")
    return ClarifyState(brief, rounds, questions, readback, confirmed)


@dataclass(frozen=True)
class MidBuildQuestion:
    """D8: ask mid-build only for a decision that changes the result."""

    decision: str
    options: tuple[str, ...]
    default: str  # JARVIS's default if nobody answers
    blocks: tuple[str, ...]  # slice ids that cannot proceed without the answer
    changes_result: bool

    def __post_init__(self) -> None:
        if not self.changes_result:
            raise ValueError("mid-build question must name a decision that changes the result")
        if not self.decision.strip():
            raise ValueError("mid-build question has no decision")
        opts = [o.strip() for o in self.options]
        if len(opts) < 2 or len(set(opts)) != len(opts) or not all(opts):
            raise ValueError("mid-build question needs at least two distinct options")
        if self.default.strip() not in opts:
            raise ValueError("JARVIS default must be one of the options")
        if not self.blocks or not all(b.strip() for b in self.blocks):
            raise ValueError("mid-build question must name the steps it blocks")


@dataclass(frozen=True)
class MidBuildBatch:
    questions: tuple[MidBuildQuestion, ...]
    message: str
    blocked: frozenset[str]
    runnable: tuple[str, ...] = field(default=())


def batch_mid_build(questions, slices) -> MidBuildBatch:
    """Merge by decision, number them in one message; only dependent slices block."""
    slices = tuple(slices)
    merged: dict[str, MidBuildQuestion] = {}
    for q in questions:
        key = q.decision.strip()
        prior = merged.get(key)
        if prior is None:
            merged[key] = q
            continue
        if (prior.options, prior.default) != (q.options, q.default):
            raise ValueError(f"conflicting questions for decision {key!r}")
        blocks = prior.blocks + tuple(b for b in q.blocks if b not in prior.blocks)
        merged[key] = MidBuildQuestion(key, q.options, q.default, blocks, True)
    if not merged:
        raise ValueError("no mid-build questions to batch")
    blocked = frozenset(b for q in merged.values() for b in q.blocks)
    unknown = blocked - set(slices)
    if unknown:
        raise ValueError(f"blocks unknown slices: {sorted(unknown)}")
    lines = []
    for i, q in enumerate(merged.values(), 1):
        lines.append(
            f"{i}. {q.decision} Options: {' / '.join(q.options)}. "
            f"Default: {q.default}. Blocks: {', '.join(q.blocks)}."
        )
    return MidBuildBatch(
        questions=tuple(merged.values()),
        message="\n".join(lines),
        blocked=blocked,
        runnable=tuple(s for s in slices if s not in blocked),
    )
