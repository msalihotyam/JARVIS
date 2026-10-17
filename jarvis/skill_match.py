"""V2.2 deterministic, scoped skill matcher: fit, similar or none.

``match_skills`` looks for a saved skill that fits a new task. It is word
overlap only (KARAR-2, PLAN D5): no model, no embeddings, no network. Text is
folded with ``skill_types.fold_token`` (the one fold for task words, D13:
NFKC, Turkish dotted/dotless i to ``i``, case fold, accents dropped), then
split into words; Turkish and English stop words and pure numbers are
dropped. Two words match when they are equal or when the shorter one, at
least ``PREFIX_MIN`` letters long, starts the longer one (``rapor`` /
``raporunu``, ``send`` / ``sends``). A skill's words come from its name,
summary, input names and step action text, in that order.

``score`` is the share of the task's words the skill covers, in whole
percent; ``coverage`` is the share of the skill's name and summary words
that the task covers. A skill FITS when the score is at least
``FIT_MIN_SCORE``, at least ``FIT_MIN_MATCHED`` words match, at least one of
them is in the skill's name or summary, and the coverage is at least
``FIT_MIN_COVERAGE``; so a broad skill whose summary names many other things
(a delete step before an email step, say) is not a fit for a narrow task
that shares two of its words. A fit is a near-identical request (SKSIMASK,
Salih 2026-10-17): the request (once Core strips declared inputs) must
equal the skill's name or summary word for word, stop words and question
marks included, and every task word the skill's text does not bind leaves
it SIMILAR; so a paraphrase, a reorder, a dropped or extra word, a
question, an antonym, a swapped qualifier or a retraction is only asked
about, never auto-run. A skill is SIMILAR when the score is at least
``SIMILAR_MIN_SCORE`` with at least ``SIMILAR_MIN_MATCHED`` matched words.
A one-word task is therefore never a fit.

Shared words alone cannot tell "Do not search my email" from "Search my email
for invoices", so a fit also needs the skill's own text to carry every cue
the task carries (V2.2 b3): each negation, with the word it governs ("do not
summarize", "hiçbir", "değil", a Turkish verb of the skill with a negative
ending such as "özetleme"), and each cue word naming another action (delete,
send, merge...), object, destination or scope (repository, main, mailbox,
recipient, file, site, app, every/all...; ``_CUE_GROUPS``), and each stem a
Turkish suffix names after an apostrophe ("GitHub'daki"). Negative
contractions (can't, won't, cannot, any apostrophe) read as "not"; a Turkish
negative ending counts on any word of the skill. The task must also bind its
objects and places as the skill does: "copy beta to alpha" is not "copy
alpha to beta", nor "from" for "to" (``_misordered``). Each negation binds
to its own clause (punctuation, but/ama/fakat/and/ve/then split them), so
"delete alpha but not beta" is not "not alpha but delete beta"; never,
avoid, "under no circumstances", sakın, asla, "hiçbir şekilde" and the
other prohibitions read as "not". Every fixed literal (a number or numeral,
a date or weekday, a quoted string, a path, an email or web address, a
mid-sentence name) must match the skill's one to one; a differing or extra
literal beyond the skill's declared inputs, or a missing one, is no fit. A
task cut anywhere before its cues are read (``MAX_TASK_CHARS``,
``MAX_TASK_WORDS``, a word over ``WORD_MAX``) is never a fit. A task that
misses one is SIMILAR at best: a lost fit is only a hint, a false fit would
propose the wrong procedure.

Scope is an allowlist: a global skill is always considered; a project skill
only when ``project_id`` is that exact project. Another project's skill never
appears in any part of the result, not even as similar or stale. Disabled and
deleted skills never appear. A stale skill is never a fit and never a
candidate: it is listed only in ``needs_revalidation`` (with kind ``similar``
at most), because a stale skill is not used automatically and revalidating it
is a normal, approved run (S12).

Cost is bounded: the task is cut to ``MAX_TASK_CHARS`` and ``MAX_TASK_WORDS``;
each skill field is read up to ``FIELD_TEXT_MAX`` folded characters, a skill
up to ``SKILL_TEXT_MAX`` and ``MAX_SKILL_WORDS`` distinct words; a cut never
leaves a fragment of a word. A skill's words are computed once and kept in a
bounded in-memory cache (``CACHE_MAX`` skill versions, keyed by the whole
immutable skill value, so a different body under the same id and version is
never served stale words). The cache changes speed only, never a result.

A match is advice, never authority: a skill is a procedure. Using it does not
lower a risk tier, skip an approval or pre-approve anything; every step still
goes through Core policy and approval as an ordinary chain step. Nothing in a
result grants, implies or remembers approval.

Pure: stdlib and ``jarvis.skill_types`` only, no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction
from functools import lru_cache

from jarvis.skill_types import (
    ID_PATTERN,
    PROJECT_ID_PATTERN,
    Skill,
    SkillHealth,
    SkillScopeKind,
    SkillStatus,
    fold_token,
)

#: Minimum score (percent of task words covered) for a fit.
FIT_MIN_SCORE = 60
#: Minimum coverage (percent of the skill's name and summary words) for a fit.
FIT_MIN_COVERAGE = 60
#: Minimum number of matched task words for a fit.
FIT_MIN_MATCHED = 2
#: Minimum score for a similar skill (a candidate for adaptation).
SIMILAR_MIN_SCORE = 30
#: Minimum number of matched task words for a similar skill.
SIMILAR_MIN_MATCHED = 1
#: At most this many candidates, and this many ``needs_revalidation`` entries.
MAX_CANDIDATES = 5
#: Only this much of the task text is read (whole words only).
MAX_TASK_CHARS = 2_000
#: Only the first this many distinct task words are scored.
MAX_TASK_WORDS = 48
#: A longer word is cut to this many characters.
WORD_MAX = 40
#: Words shorter than this are dropped.
WORD_MIN = 2
#: A word of at least this many letters also matches its suffixed forms.
PREFIX_MIN = 4
#: Each skill field (name, summary, input name, step action) is read up to
#: this many folded characters (whole words only).
FIELD_TEXT_MAX = 600
#: A skill is read up to this many folded characters in all.
SKILL_TEXT_MAX = 3_000
#: A skill contributes at most this many distinct words.
MAX_SKILL_WORDS = 256
#: At most this many skills per call; more is refused (fail closed). Chosen
#: so that a cold call over this many maximum-size skills stays near 1-2 s
#: (measured and guarded by a test); a warm call (words cached) is far less.
MAX_SKILLS = 1_000
#: The word cache keeps at most this many skill versions (a full library).
CACHE_MAX = MAX_SKILLS
#: At most this many words are named in one reason line.
MAX_REASON_WORDS = 8

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Raw characters folded at a time, and the largest NFKC expansion of one
# character (U+FDFA becomes 18), so a field is never folded much past its cap.
_FOLD_CHUNK = 16
_MAX_EXPANSION = 18
# Runs of letters and digits: exactly the ``str.isalnum`` characters (the
# regex class is "not a non-word character and not an underscore").
_WORD_RUN = re.compile("[^" + chr(0x5C) + "W_]+")
# A "{name}" placeholder (D12: name is an input name, skill_types.ID_PATTERN).
# Placeholders in a skill's name, summary and step actions are not words: the
# value they stand for differs on every run (D18), so they are removed (each
# becomes a space) before the skill's words are read, for matching and for
# coverage. The TASK text is read as it is (its values are SKCORE's job).
_PLACEHOLDER = re.compile("[{]" + ID_PATTERN.pattern + "[}]")
# Above every character a word can hold (U+10FFFF is not alphanumeric).
_AFTER_ALL = chr(0x10FFFF)

# Function words and fillers that say nothing about WHAT the task is. Written
# naturally here; folded with ``fold_token`` below like every other word.
# Entries whose fold is also a Turkish content word are left out on purpose,
# so the stop list never swallows that word: "şu"/"şunu" (fold to su "water"
# and sunu "presentation"), "is" (iş "work") and "can" (can "life").
_STOP_WORDS_TR = (
    "ve veya ile ya da de ki mi mı mu mü bu o bir için gibi kadar daha çok en "
    "ama fakat ancak sonra önce şimdi lütfen bana beni benim bize bizim sen sana "
    "senin siz size ben biz onu bunu buna şuna ona onun bunun olan olarak "
    "her hem ne nasıl neden niye nerede hangi tüm bütün şey şeyi diye ise eğer "
    "hadi rica ederim acaba yine tekrar artık hep"
)
_STOP_WORDS_EN = (
    "a an the and or but nor to of in on at by for with from into onto as are "
    "was were be been it its this that these those my me mine our we us you your "
    "i please could would will should shall do does did then now all any "
    "some so if up out about just hey also after before again there here what how "
    "why when where which who"
)
_ASSISTANT_NAMES = "jarvis"
#: Turkish content words that no stop word may fold to (tested).
KEPT_CONTENT_WORDS = frozenset({"is", "su", "sunu", "can"})

# Fit cues (module docstring). Written naturally, folded like the stop words.
# Words that negate the next content word, and the ones that negate the word
# before them.
_NEGATION_BEFORE_TEXT = (
    "not no never without dont don doesn didn isn aren wasn weren cannot cant "
    "won wouldn shouldn couldn haven hasn hadn mustn needn shan "
    "doesnt didnt isnt arent wasnt werent wont wouldnt shouldnt couldnt "
    "havent hasnt hadnt mustnt neednt shant aint "
    "nothing none neither hiçbir asla sakın hiç katiyen "
    "avoid avoids avoided avoiding refrain refrains refrained refraining "
    "forbid forbids forbidden forbidding prohibit prohibits prohibited prohibiting "
    "prevent prevents prevented preventing skip skips skipped skipping "
    "omit omits omitted omitting except excluding instead"
)
_NEGATION_AFTER_TEXT = (
    "değil değildir değilim değilsin değiliz değilsiniz değiller yok yerine gerekmez bırak "
    "hariç haricinde dışında yasak yasaktır"
)
# Phrases read as "not" before tokenizing ("under no circumstances delete").
_NEGATION_PHRASES = re.compile(
    r"\b(?:under no circumstances?|in no way|by no means|rather than|instead of|"
    r"hicbir sekilde|hicbir zaman|ne olursa olsun)\b"
)
# A verb plus one of these endings is the negated verb ("özetle" + "me"). The
# verb is any content word of the skill's name, summary or a step (a word of
# its own text is never negated by accident) or one of the general verbs
# below. The second list also matches every longer ending it starts
# ("özetle" + "miyorum", "özetle" + "yemem").
_NEGATIVE_ENDINGS_TEXT = (
    "ma me mayın meyin mayınız meyiniz masın mesin masınlar mesinler "
    "madan meden mayalım meyelim"
)
_NEGATIVE_ENDING_STARTS_TEXT = (
    "mıyor miyor muyor müyor madı medi mamış memiş mayacak meyecek mayacağ "
    "meyeceğ maz mez mam mem mayız meyiz yama yeme ama eme meksizin maksızın mesene masana "
    # Colloquial future and progressive ("özetle" + "micem", "miycem", "miyom").
    "mıca mice muca müce mıyc miyc muyc müyc mıyo miyo muyo müyo"
)
# A passive ("silin" + "mesin", "özetlen" + "me") is the verb plus one of these
# and a negative ending starting with "m".
_PASSIVE = ("il", "in", "ul", "un", "l", "n")  # folded: "ıl", "ün" are "il", "un"
_NEGATED_VERBS_TEXT = "yap et ed iste ol"
# A cue only when it opens a clause ("Open a pull request") or an object
# pronoun follows it ("(open it first)"), never as an adjective ("List open
# pull requests", "which are open"); a clause opens at the start, after a
# punctuation mark or after one of the opener words.
_CLAUSE_START_ONLY_TEXT = "open opens opened"
_OBJECT_PRONOUNS_TEXT = "it them this that"
_CLAUSE_OPENERS_TEXT = "and then or please you also just now to ve sonra lütfen"
# Conjunctions that also close a clause: a negation binds within its clause.
_CLAUSE_SPLITS_TEXT = "and but or then ve veya ama fakat ancak sonra"
# Mail words that opening a clause send ("and email it to Bob").
_MAIL_VERBS_TEXT = "email emails emailed mail mails mailed"
# English contraction endings: "don't" and "it's" name no place.
_CONTRACTION_TEXT = "t s m re ve ll d"
# Cue words by group; words of one group are the same cue. A word ending in
# "*" also matches every word it starts (Turkish suffixes, inflections).
_CUE_GROUPS = {
    # Another action than the skill's own.
    "delete": "delete* remove* erase* uninstall* sil* kaldır*",
    "send": (
        "send* sent forward* post posts posted posting share* publish* upload* "
        "gönder* ilet iletip iletir iletin yolla* paylaş*"
    ),
    "merge": "merge* birleştir*",
    "squash": "squash*",
    "close": "close closes closed closing kapat*",
    "revert": "revert* undo* rollback geri",
    "push": "push*",
    "install": "install* kur kurup kurar kurun",
    "update": "update* upgrade* güncelle*",
    "stop": "stop stops stopped stopping cancel* abort* durdur* iptal",
    "rename": "rename*",
    "translate": "translat* çevir*",
    "print": "print* yazdır*",
    "fill": "fill* doldur*",
    "review": "review* incele*",
    "edit": "edit* modify* modifie* düzenle* değiştir*",
    "save": "save* kaydet*",
    "tag": "tag tags tagged tagging etiketle*",
    "amend": "amend*",
    "reply": "reply* replies replied respond* yanıtla* cevapla*",
    "open":"open opens opened launch* başlat* çalıştır* aç açıp açar açın",
    "restore": "restor* recover* undelet*",
    # A question, condition, choice, repeat or age the skill does not carry.
    "question": "mi mı mu mü",
    "condition": "if unless when whenever eğer ise yoksa",
    "choice": "or either veya yahut",
    "repeat": "twice thrice again tekrar yeniden",
    "age": "old older oldest new newer newest latest eski yeni",
    # Another object, destination or scope.
    "email": "email* mail* posta* eposta* inbox*",
    "folder": "folder* klasör* dizin*",
    "people": "everyone everybody team* recipient* herkes* ekip* ekib* alıcı*",
    "file": "file* dosya* pdf path*",
    "web": "web website* site* browser* tarayıcı* tab tabs sekme*",
    "app": "app apps application* uygulama*",
    "repository": "repositor* repo repos depo*",
    "branch": "branch* main master dal dalı dalına dalında dala",
    "github": "github*",
    "gitlab": "gitlab*",
    "slack": "slack*",
    "issue": "issue* sorun*",
    "release": "release* sürüm*",
    "organization": "organization* organisation* org kuruluş* şirket* company companies",
    "archive": "archive* arşiv*",
    "forum": "forum*",
    "startup": "startup açılış*",
    "aloud": "aloud loud sesli*",
    "all": "every* all whole entire* each tüm* bütün* hepsi* her",
}


class SkillMatchError(ValueError):
    """The matcher was called with input it refuses (fail closed)."""


class MatchKind(str, Enum):
    FIT = "fit"
    SIMILAR = "similar"
    NONE = "none"


def _wordish(char: str) -> bool:
    return char.isalnum() or unicodedata.category(char).startswith("M")


def cut_text(text: str, limit: int) -> str:
    """``text[:limit]`` without a fragment of a word at the end.

    When the cut falls inside a word (letters, digits or combining marks on
    both sides), the partial last word is dropped.
    """
    if len(text) <= limit:
        return text
    end = limit
    if _wordish(text[limit]):
        while end > 0 and _wordish(text[end - 1]):
            end -= 1
    return text[:end]


def _fold_capped(text: str, limit: int) -> str:
    """``fold_token`` of ``text``, read only up to ``limit`` folded characters.

    ASCII text never grows when folded, and neither does a text short enough
    that even the largest expansion stays under the cap: both are folded in
    one call. Any other text is folded ``_FOLD_CHUNK`` raw characters at a
    time and reading stops past the cap, so a text that expands under NFKC
    (U+FDFA becomes 18 characters) costs at most one chunk more than the cap.
    A word that straddles a chunk border is checked again as a whole word in
    ``_new_word``.
    """
    if text.isascii() or len(text) * _MAX_EXPANSION <= limit:
        return cut_text(fold_token(text), limit)
    parts: list[str] = []
    size = 0
    for start in range(0, len(text), _FOLD_CHUNK):
        piece = fold_token(text[start : start + _FOLD_CHUNK])
        parts.append(piece)
        size += len(piece)
        if size > limit:
            break
    return cut_text("".join(parts), limit)


def _candidates(folded: str) -> Iterator[str]:
    """Runs of letters and digits in folded text, each cut to ``WORD_MAX``."""
    for run in _WORD_RUN.findall(folded):
        word = run[:WORD_MAX]
        if len(word) >= WORD_MIN and not word.isdigit() and word not in STOP_WORDS:
            yield word


def _new_word(word: str, seen: dict[str, None]) -> bool:
    """Add a content word to ``seen`` if it is new and canonical.

    Every kept word satisfies ``fold_token(word) == word`` (ASCII words do by
    construction). A word that is not (possible only after a cut) is dropped,
    which can only lower a score: it fails toward no match.
    """
    if word in seen or (not word.isascii() and fold_token(word) != word):
        return False
    seen[word] = None
    return True


STOP_WORDS = frozenset(
    _WORD_RUN.findall(fold_token(" ".join((_STOP_WORDS_TR, _STOP_WORDS_EN, _ASSISTANT_NAMES))))
)


def _folded_set(text: str) -> frozenset[str]:
    return frozenset(_WORD_RUN.findall(fold_token(text)))


_NEGATION_BEFORE = _folded_set(_NEGATION_BEFORE_TEXT)
_NEGATION_AFTER = _folded_set(_NEGATION_AFTER_TEXT)
_NEGATIVE_ENDINGS = _folded_set(_NEGATIVE_ENDINGS_TEXT)
_NEGATIVE_ENDING_STARTS = tuple(sorted(_folded_set(_NEGATIVE_ENDING_STARTS_TEXT)))
_NEGATED_VERBS = _folded_set(_NEGATED_VERBS_TEXT)
_CLAUSE_START_ONLY = _folded_set(_CLAUSE_START_ONLY_TEXT)
_CLAUSE_OPENERS = _folded_set(_CLAUSE_OPENERS_TEXT)
_OBJECT_PRONOUNS = _folded_set(_OBJECT_PRONOUNS_TEXT)
_MAIL_VERBS = _folded_set(_MAIL_VERBS_TEXT)
_CONTRACTIONS = _folded_set(_CONTRACTION_TEXT)
_CLAUSE_SPLITS = _folded_set(_CLAUSE_SPLITS_TEXT)
_CUE_EXACT = {
    fold_token(entry): group
    for group, entries in _CUE_GROUPS.items()
    for entry in entries.split()
    if not entry.endswith("*")
}
_CUE_PREFIX = {
    fold_token(entry[:-1]): group
    for group, entries in _CUE_GROUPS.items()
    for entry in entries.split()
    if entry.endswith("*")
}
_CUE_PREFIX_SIZES = tuple(sorted({len(prefix) for prefix in _CUE_PREFIX}))
# Words and the punctuation marks that close a clause.
_TOKEN = re.compile(_WORD_RUN.pattern + "|[,.;:!?]")
# A stem and the suffix written after an apostrophe (ASCII or U+2019).
_NAMED = re.compile("(" + _WORD_RUN.pattern + ")['" + chr(0x2019) + "](" + _WORD_RUN.pattern + ")")
# "n't" after any apostrophe (ASCII, U+2018, U+2019, U+02BC, a backtick, or
# the space an acute accent folds to): read as "not" before tokenizing.
_APOSTROPHES = "'`" + chr(0x2018) + chr(0x2019) + chr(0x2BC)
# Apostrophes that are letters (U+02BB, U+02BC) or a prime split words as "'".
_LETTER_APOSTROPHES = {0x2BB: "'", 0x2BC: "'", 0x2032: "'"}
_NEGATIVE_CONTRACTION = re.compile(
    "(?<![^" + chr(0x5C) + "W_])[^" + chr(0x5C) + "W_]*n[" + _APOSTROPHES + " ]t(?![^"
    + chr(0x5C) + "W_])"
)
# Literals (``_literals``): quoted texts, the marks of a path, mail or web
# address, and the number, day, month and relative date words (folded; "on",
# "one", "bir" and "may" are ordinary words too often to count).
_QUOTED = re.compile(
    '"([^"]*)"|' + chr(0x201C) + "([^" + chr(0x201D) + "]*)" + chr(0x201D)
    + "|" + chr(0xAB) + "([^" + chr(0xBB) + "]*)" + chr(0xBB)
    + "|(?<![^" + chr(0x5C) + "W_])'([^']*)'(?![^" + chr(0x5C) + "W_])"
)
_ADDRESS = re.compile("[/@" + chr(0x5C) * 2 + "]|[^" + chr(0x5C) + "W_][.:][^" + chr(0x5C) + "W_]")
_LITERAL_EDGE = ".,;:!?()[]{}\"'`" + "".join(map(chr, (0x2018, 0x2019, 0x201C, 0x201D, 0xAB, 0xBB)))
_LITERAL_EDGE_AFTER = "\"')]}" + "".join(map(chr, (0x2019, 0x201D, 0xBB)))
_SENTENCE_ENDS = (".", "!", "?", ":")
_LITERAL_SUFFIX = re.compile("[" + _APOSTROPHES + "]")
# English clitics after an apostrophe ("Don't", "It's") mark no proper name.
_CLITICS = frozenset("t s ll re ve d m".split())
_LITERAL_WORDS = tuple(
    fold_token(word)
    for word in (
        "two three four five six seven eight nine ten eleven twelve thirteen fourteen"
        " fifteen sixteen seventeen eighteen nineteen twenty hundred thousand million"
        " iki üç dört beş altı yedi sekiz dokuz yüz bin milyon"
        " today tomorrow yesterday tonight bugün yarın dün"
        " monday tuesday wednesday thursday friday saturday sunday"
        " pazartesi salı çarşamba perşembe cumartesi cuma pazar"
        " january february march april june july august september october november"
        " december ocak şubat mart nisan mayıs haziran temmuz ağustos eylül ekim kasım"
        " aralık"
    ).split()
)
# Slot words and Turkish case suffixes after an apostrophe, read as slot marks
# in a text's word order ("@" is never part of a word).
_SLOT_WORDS = {"to": "@to", "into": "@to", "onto": "@to", "toward": "@to",
               "towards": "@to", "unto": "@to", "from": "@from"}
_SLOT_SUFFIXES = {"a": "@to", "e": "@to", "ya": "@to", "ye": "@to", "na": "@to",
                  "ne": "@to", "i": "@obj", "u": "@obj", "yi": "@obj", "yu": "@obj",
                  "ni": "@obj", "nu": "@obj"}


def _suffix_slot(suffix: str) -> str | None:
    if suffix.startswith(("dan", "den", "tan", "ten", "ndan", "nden")):
        return "@from"
    if suffix.startswith(("da", "de", "ta", "te", "nda", "nde")):
        return "@at"
    return _SLOT_SUFFIXES.get(suffix)


def _cue_group(word: str) -> str | None:
    if word in _CUE_EXACT:
        return _CUE_EXACT[word]
    for size in _CUE_PREFIX_SIZES:
        group = _CUE_PREFIX.get(word[:size]) if size <= len(word) else None
        if group is not None:
            return group
    return None


def _is_content(token: str) -> bool:
    return len(token) >= WORD_MIN and not token.isdigit() and token not in STOP_WORDS


def _same_word(a: str, b: str) -> bool:
    """The matcher's word rule: equal, or a long enough one starts the other."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= PREFIX_MIN and long.startswith(short)


@dataclass(frozen=True, slots=True)
class _CueText:
    """A folded text read for fit cues; everything that needs no verbs.

    ``particles`` are the negation words' governed positions and words ("" when
    there is none); ``endings`` maps a stem to the positions of words that are
    the stem plus a negative ending, a negated verb only when the stem is a
    verb (``_cues``); ``cues`` are the positions and groups of cue words;
    ``named`` the stems written before an apostrophe suffix; ``order`` the
    content words and slot marks (``_SLOT_WORDS``, ``_suffix_slot``) in text
    order. A negative contraction ("can't") is read as "not".
    """

    particles: tuple[tuple[int, str, int], ...]  # and the negation's clause
    endings: dict[str, tuple[int, ...]]
    cues: tuple[tuple[int, str], ...]
    named: tuple[str, ...]
    order: tuple[str, ...]
    clauses: tuple[int, ...]  # the clause of each content word
    clause_words: tuple[frozenset[str], ...]  # each clause's content words

    @classmethod
    def read(cls, folded: str) -> _CueText:
        folded = re.sub(r"\bout of\b", "from", folded.translate(_LETTER_APOSTROPHES))
        folded = _NEGATIVE_CONTRACTION.sub(" not ", folded)
        folded = _NEGATION_PHRASES.sub(" not ", folded)
        found = list(_TOKEN.finditer(folded))
        tokens = [match.group()[:WORD_MAX] for match in found]
        order: list[str] = []
        for match, word in zip(found, tokens):
            start = match.start()
            after = start >= 2 and folded[start - 1] in _APOSTROPHES and folded[start - 2].isalnum()
            slot = _suffix_slot(word) if after else _SLOT_WORDS.get(word)
            if slot is not None or (not after and _is_content(word)):
                order.append(slot or word)
        flags = [_is_content(word) for word in tokens]
        objects = [word in _OBJECT_PRONOUNS for word in tokens[1:]] + [False]
        content: list[str] = []
        clauses: list[int] = []  # the clause of each content word
        cues: list[tuple[int, str]] = []
        opened = True
        clause = 0
        for index, word in enumerate(tokens):
            is_content = flags[index]
            # Stop words are read too: "tüm", "bütün" and "all" are cues. A
            # stop word is never a governed word (position -1).
            at = len(content) if is_content else -1
            group = _cue_group(word) if len(word) >= WORD_MIN else None
            if group is not None and (
                opened or objects[index] or word not in _CLAUSE_START_ONLY
            ):
                cues.append((at, group))
            if opened and word in _MAIL_VERBS:
                cues.append((at, "send"))
            if not is_content:
                # Punctuation and "but", "and", "ama", "ve"... split clauses.
                mark = (len(word) == 1 and not word.isalnum()) or word in _CLAUSE_SPLITS
                clause += mark
                opened = mark or word in _CLAUSE_OPENERS
                continue
            content.append(word)
            clauses.append(clause)
            opened = False
        particles: list[tuple[int, str]] = []
        endings: dict[str, list[int]] = {}
        for at, word in enumerate(content):
            if word in _NEGATION_BEFORE or word in _NEGATION_AFTER:
                # A negation governs a word of its own clause only.
                target = at + 1 if word in _NEGATION_BEFORE else at - 1
                inside = 0 <= target < len(content) and clauses[target] == clauses[at]
                particles.append(
                    (target if inside else -2, content[target] if inside else "", clauses[at])
                )
            for cut in range(1, len(word)):
                ending = word[cut:]
                passive = [ending[len(p):] for p in _PASSIVE if ending[len(p):len(p) + 1] == "m"
                           and ending.startswith(p)]
                if any(e in _NEGATIVE_ENDINGS or e.startswith(_NEGATIVE_ENDING_STARTS)
                       for e in (ending, *passive)):
                    endings.setdefault(word[:cut], []).append(at)
        named = tuple(
            dict.fromkeys(
                stem[:WORD_MAX]
                for stem, suffix in _NAMED.findall(folded)
                if suffix not in _CONTRACTIONS and _is_content(stem[:WORD_MAX])
            )
        )
        clause_words: list[set[str]] = [set() for _ in range(clause + 1)]
        for word, at in zip(content, clauses):
            if word not in _NEGATION_BEFORE and word not in _NEGATION_AFTER:
                clause_words[at].add(word)
        return cls(
            tuple(particles),
            {stem: tuple(at) for stem, at in endings.items()},
            tuple(cues),
            named,
            tuple(order),
            tuple(clauses),
            tuple(frozenset(words) for words in clause_words),
        )


def _cues(
    text: _CueText, verbs: frozenset[str]
) -> tuple[frozenset[str], tuple[str, ...], frozenset[tuple[str, frozenset[str]]]]:
    """The cue groups, negated words and negation bindings of ``text``.

    A negated word names no cue of its own: "without sending" is no "send".
    A binding is a negated word and the content words of its own clause.
    """
    governed = {at for at, _, _ in text.particles}
    negated = [word for _, word, _ in text.particles]
    bindings = {(word, text.clause_words[clause]) for _, word, clause in text.particles}
    for stem in sorted((verbs | _NEGATED_VERBS) & text.endings.keys()):
        governed.update(text.endings[stem])
        negated.extend(stem for _ in text.endings[stem])
        bindings.update((stem, text.clause_words[text.clauses[at]]) for at in text.endings[stem])
    groups = frozenset(group for at, group in text.cues if at not in governed)
    return groups, tuple(negated), frozenset(bindings)


def _same_clause(a: frozenset[str], b: frozenset[str]) -> bool:
    """Every word of each clause is the same word as one of the other's."""
    return all(any(_same_word(x, y) for y in b) for x in a) and all(
        any(_same_word(y, x) for x in a) for y in b
    )


def _literals(text: str) -> tuple[str, ...]:
    """The fixed literals of a raw text, folded, in first-seen order.

    A literal is a quoted text, a token with a digit or a path, mail or web
    address mark, a capitalised word that does not start a sentence, a stem
    written before an apostrophe suffix, or a number, day, month or relative
    date word. A literal names one thing: a different one is a different task.
    """
    found: dict[str, None] = {}
    for parts in _QUOTED.findall(text):
        quoted = " ".join(_WORD_RUN.findall(fold_token("".join(parts))))
        if quoted:
            found['"' + quoted] = None
        # The quoted words are still read below.
    opens = True  # the next word starts a sentence
    for token in text.split():
        bare = token.strip(_LITERAL_EDGE)
        stem, *suffix = _LITERAL_SUFFIX.split(bare, maxsplit=1)
        named = bool(suffix) and fold_token(suffix[0]) not in _CLITICS
        folded = fold_token(stem)
        if stem and (any(c.isdigit() for c in stem) or _ADDRESS.search(stem)):
            found[folded] = None
        elif len(stem) >= WORD_MIN and stem[0].isupper() and (not opens or named):
            found[folded] = None
        else:
            for word in _WORD_RUN.findall(folded):
                base = next(
                    (x for x in _LITERAL_WORDS
                     if word == x or (len(x) >= PREFIX_MIN and word.startswith(x))),
                    None,
                )
                if base is not None:
                    found[base] = None
        opens = token.rstrip(_LITERAL_EDGE_AFTER).endswith(_SENTENCE_ENDS)
    return tuple(found)


def _same_literal(a: str, b: str) -> bool:
    """Words by the matcher's word rule; numbers, paths and quotes exactly."""
    return _same_word(a, b) if a.isalpha() and b.isalpha() else a == b


@lru_cache(maxsize=CACHE_MAX)
def _skill_literals(skill: Skill) -> tuple[str, ...]:
    """The literals of a skill's name, summary and step actions, bounded."""
    fields = [skill.name, skill.summary] + [step.action for step in skill.steps]
    text = cut_text(" . ".join(_without_placeholders(field) for field in fields), SKILL_TEXT_MAX)
    return _literals(text)


def words(text: str, limit: int | None = None) -> tuple[str, ...]:
    """The distinct content words of ``text``, in first-seen order."""
    seen: dict[str, None] = {}
    for word in _candidates(fold_token(text)):
        if _new_word(word, seen) and limit is not None and len(seen) >= limit:
            break
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class _WordIndex:
    exact: frozenset[str]
    ordered: tuple[str, ...]

    @classmethod
    def build(cls, items: Iterable[str]) -> _WordIndex:
        exact = frozenset(items)
        return cls(exact, tuple(sorted(exact)))


@dataclass(frozen=True, slots=True)
class _SkillWords:
    headline: _WordIndex  # name and summary
    body: _WordIndex  # headline, input names and step actions


def _without_placeholders(text: str) -> str:
    """``text`` with every "{name}" placeholder replaced by a space."""
    return _PLACEHOLDER.sub(" ", text)


def _said(text: str, *, stored: bool = False) -> str | None:
    """A text's identity: the whole stripped string through
    ``fold_token(identity=True)`` (case, NFC/NFD, Turkish I/i only). Every
    other difference - punctuation, quote, symbol, order, inner whitespace,
    a word more or less - is a different text, so a request FITs only when it
    is the skill's own text (Salih, 2026-10-17 "Birebir aynıysa"). A stored
    text with any placeholder has no identity (None): no request equals it.
    Nor has a text with a mark stacked on a marked letter (the Turkish fold
    would drop it, so it would vanish)."""
    if stored and _PLACEHOLDER.search(text):
        return None
    for prev, char in zip(text, text[1:]):
        if unicodedata.combining(char) and (
            unicodedata.combining(prev) or unicodedata.decomposition(prev)[:1] not in ("", "<")
        ):
            return None
    # CDXINT54F O2: NFC turns a singleton (Kelvin sign) into a plain letter.
    if any(" " not in d and d[:1] not in ("", "<") for d in map(unicodedata.decomposition, text)):
        return None
    return fold_token(text.strip(), identity=True)


_REFUSED: dict[str, str] = {}  # sentinel: the template aligns but cannot bind (checked by identity)


def _bind_one(template: str, request: str, skill: Skill) -> dict[str, str] | None:
    """Each placeholder bound to the exact span the user typed there, all else the
    template's own up to the identity fold (Salih 2026-10-17, option A); None
    only when a literal of the template is absent from the request (it is not
    this template), ``_REFUSED`` for every other failure: an empty, padded or
    unprintable span, an overlap, an unsupported template, a split that is not
    the only one (CDXINT54H M2)."""
    parts = _PLACEHOLDER.split(template)
    names = [m.group()[1:-1] for m in _PLACEHOLDER.finditer(template)]
    declared = {spec.name: spec.required for spec in skill.inputs}
    edges = [p[-1:] for p in parts[:-1]] + [p[:1] for p in parts[1:]]
    chars = [fold_token(c, identity=True) for c in request]
    key, lits = "".join(chars), [fold_token(p, identity=True) for p in parts]
    whole, at = fold_token(request, identity=True), len(lits[0])
    if not (whole.startswith(lits[0]) and whole.endswith(lits[-1])):
        return None
    for lit in lits[1:-1]:
        at = whole.find(lit, at)
        if at < 0:
            return None
        at += len(lit)
    end = len(key) - len(lits[-1])
    if (
        len(set(names)) != len(names)
        or not set(names) <= declared.keys()
        or {n for n, required in declared.items() if required} - set(names)
        or not all(parts[1:-1])
        or any(c.isalnum() or c == "_" for c in edges)
        or any(len(c) != 1 for c in chars)
        or key != whole
        or len(lits[0]) >= end
    ):
        return _REFUSED
    # Leftmost and rightmost placements of the literals agree only for a unique split.
    lo, at = [], len(lits[0])
    for lit in lits[1:-1]:
        at = key.find(lit, at + 1, end)
        if at < 0:
            return _REFUSED
        lo.append(at)
        at += len(lit)
    hi, stop = [], end
    for lit in reversed(lits[1:-1]):
        hi.insert(0, stop := key.rfind(lit, 0, stop - 1))
    starts = (len(lits[0]), *(a + len(lit) for a, lit in zip(lo, lits[1:-1])))
    values = [request[a:b] for a, b in zip(starts, (*lo, end))]
    # The literals align here, so a bad split is refused, not a non-match. A
    # typed "{name}" is the template's own text, never a value: ask.
    if at >= end or lo != hi or any(v != v.strip() or not _plain(v) or "{" in v for v in values):
        return _REFUSED
    return dict(zip(names, values))


# Printable characters that still show nothing: Hangul and braille fillers.
_BLANKS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800)))


def _plain(value: str) -> bool:
    """True when every character of a bound value is visible on its own: no
    control, format, combining mark or blank filler (CDXINT54G F2 #20)."""
    return value.isprintable() and not any(
        c in _BLANKS or unicodedata.category(c) in ("Mn", "Me") for c in value
    )


def _bind(skill: Skill, request: str | None) -> dict[str, str] | None:
    """The one binding of the user's own ``request`` to the skill's name or
    summary placeholders, never the model's text (INT54-AFX F2 H1)."""
    if not request or not request.strip() or len(request) > MAX_TASK_CHARS:
        return None
    if _said(request) is None:
        return None
    texts = [t.strip() for t in (skill.name, skill.summary) if _PLACEHOLDER.search(t)]
    # Every template the request aligns to must bind it alike; one refused
    # (ambiguous, padded, invisible) alignment asks (CDXINT54G C2/C3).
    bound = [_bind_one(t, request.strip(), skill) if _said(t) else _REFUSED for t in texts]
    bound = [b for b in bound if b is not None]
    if not bound or any(b is _REFUSED for b in bound) or any(b != bound[0] for b in bound):
        return None
    return bound[0]


@lru_cache(maxsize=CACHE_MAX)
def _skill_said(skill: Skill) -> frozenset[str]:
    """The skill's own texts a near-identical request must equal: name and summary."""
    said = {_said(skill.name, stored=True), _said(skill.summary, stored=True)}
    return frozenset(said - {None})


@lru_cache(maxsize=CACHE_MAX)
def _skill_words(skill: Skill) -> _SkillWords:
    """A skill's words, bounded per field, per skill and in number.

    "{name}" placeholders in the name, summary and step actions are removed
    first (``_PLACEHOLDER``): they are never words of the skill.
    """
    fields = (
        [_without_placeholders(skill.name), _without_placeholders(skill.summary)]
        + [item.name for item in skill.inputs]
        + [_without_placeholders(step.action) for step in skill.steps]
    )
    seen: dict[str, None] = {}
    headline: tuple[str, ...] = ()
    budget = SKILL_TEXT_MAX
    for position, field in enumerate(fields):
        if budget > 0 and len(seen) < MAX_SKILL_WORDS:
            folded = _fold_capped(field, min(FIELD_TEXT_MAX, budget))
            budget -= len(folded)
            for word in _candidates(folded):
                if _new_word(word, seen) and len(seen) >= MAX_SKILL_WORDS:
                    break
        if position == 1:
            headline = tuple(seen)
    return _SkillWords(_WordIndex.build(headline), _WordIndex.build(seen))


@lru_cache(maxsize=CACHE_MAX)
def _skill_cues(
    skill: Skill,
) -> tuple[
    frozenset[str],
    frozenset[str],
    tuple[str, ...],
    tuple[tuple[str, ...], ...],
    frozenset[tuple[str, frozenset[str]]],
]:
    """A skill's verbs, cue groups, negated words, field word orders and
    negation bindings.

    Its verbs are every content word of its name, summary and each step
    action: a Turkish verb need not end its clause ("özetle ve göster"). Each
    field opens a clause.
    """
    fields = (
        [_without_placeholders(skill.name), _without_placeholders(skill.summary)]
        + [item.name for item in skill.inputs]
        + [_without_placeholders(step.action) for step in skill.steps]
    )
    folded: list[str] = []
    orders: list[tuple[str, ...]] = []
    verbs: set[str] = set()
    budget = SKILL_TEXT_MAX
    for position, field in enumerate(fields):
        if budget <= 0:
            break
        text = _fold_capped(field, min(FIELD_TEXT_MAX, budget))
        budget -= len(text)
        folded.append(text)
        orders.append(_CueText.read(text).order)
        if position < 2 or position >= 2 + len(skill.inputs):
            verbs.update(word[:WORD_MAX] for word in _WORD_RUN.findall(text) if _is_content(word))
    verbs_set = frozenset(verbs)
    groups, negated, bindings = _cues(_CueText.read(" . ".join(folded)), verbs_set)
    return verbs_set, groups, negated, tuple(orders), bindings


def _misordered(fields: tuple[tuple[str, ...], ...], order: tuple[str, ...]) -> bool:
    """Whether ``order`` binds its words or slots unlike every skill field.

    True when the request has a slot mark no field has ("from" for "to")
    while the skill binds some slot, or two words of the skill swap their
    neighbours (the word or slot mark just before, or just after; a clause
    edge only where the skill has nothing else there), or a shared stem with
    another case ending ("dosyasından" for "dosyasını"): "copy file beta to folder alpha" is not "copy
    file alpha to folder beta", "beta dosyasını alpha klasörüne" is not
    "alpha dosyasını beta klasörüne". Free reordering that keeps each word's
    own neighbours ("faturayı müşteriye" for "müşteriye faturayı") is not a
    swap. More shared words than ``MAX_TASK_WORDS`` cannot be checked (fail
    closed).
    """
    def same(a: str, b: str) -> bool:
        return a == b or (a[0] not in "@^$" and _same_word(a, b))

    own = {word for field in fields for word in field}
    if any(word[0] == "@" for word in own) and any(word[0] == "@" and word not in own for word in order):
        return True
    shared = [
        word
        for word in dict.fromkeys(order)
        if word[0] != "@" and any(same(word, other) for other in own)
    ]
    if len(shared) > MAX_TASK_WORDS:
        return True

    def neighbours(text: tuple[str, ...], word: str) -> list[tuple[str, str]]:
        padded = ("^", *text, "$")
        return [(padded[k - 1], padded[k + 1]) for k in range(1, len(padded) - 1) if same(word, padded[k])]

    def case(text: tuple[str, ...], k: int, size: int) -> str | None:
        if len(text[k]) > size:
            return _suffix_slot(text[k][size:])
        return text[k + 1] if k + 1 < len(text) and text[k + 1][0] == "@" else None

    # A shared stem with another case ending: "dosyasından" for "dosyasını",
    # "betaya" for "beta'yı".
    for k, word in enumerate(order):
        cases = [
            (case(order, k, size), case(field, m, size))
            for field in fields
            for m, other in enumerate(field)
            if word[0] != "@" and other[0] != "@"
            and (size := next((n for n, (x, y) in enumerate(zip(word, other)) if x != y),
                              min(len(word), len(other)))) >= PREFIX_MIN
        ]
        if any(a and b and a != b for a, b in cases) and not any(a == b for a, b in cases):
            return True

    asked = [neighbours(order, word) for word in shared]
    known = [[pair for field in fields for pair in neighbours(field, word)] for word in shared]
    for side in (0, 1):
        moved = [
            (i, pair[side])
            for i in range(len(shared))
            for pair in asked[i]
            if not any(same(pair[side], own[side]) for own in known[i])
        ]
        for i, near in moved:
            for j, other in moved:
                if (
                    i != j
                    and not same(shared[i], shared[j])
                    and (near not in "^$" or other not in "^$")
                    and (near not in "^$" or all(pair[side] in "^$" for pair in known[j]))
                    and (other not in "^$" or all(pair[side] in "^$" for pair in known[i]))
                    and any(same(near, pair[side]) for pair in known[j])
                    and any(same(other, pair[side]) for pair in known[i])
                ):
                    return True
    return False


def _conflicts(
    skill: Skill,
    body: _WordIndex,
    request: _CueText,
    literals: tuple[str, ...],
    task_words: tuple[str, ...],
) -> list[str]:
    """Why ``request`` carries a cue the skill's own text does not (fit rule)."""
    verbs, own_groups, own_negated, orders, own_bindings = _skill_cues(skill)
    groups, negated, bindings = _cues(request, verbs)
    found: list[str] = []
    # A negation the request leaves out counts once the request names that
    # word itself: it then asks for what the skill refuses to do.
    negations = sorted(
        {word for word in negated if not any(_same_word(word, own) for own in own_negated)}
        | {
            own for own in own_negated
            if not any(_same_word(word, own) for word in negated)
            and (not own or any(_same_word(own, word) for word in task_words))
        }
    )
    if negations:
        found.append(
            _named("not a fit: negates what the skill does not", (w or "-" for w in negations))
        )
    elif any(
        not any(
            _same_word(word, own) and _same_clause(clause, own_clause)
            for own, own_clause in own_bindings
        )
        for word, clause in bindings
    ):
        found.append("not a fit: negates in another clause than the skill")
    own_literals = _skill_literals(skill)
    extra = [
        x for x in literals
        if not any(_same_literal(x, own) for own in own_literals)
        and not (x.isalpha() and _TaskWords.build((x,)).hits(body))
    ]
    missing = [
        own for own in own_literals
        if not any(_same_literal(x, own) for x in literals)
        and not (own.isalpha() and any(_same_word(own, word) for word in task_words))
    ]
    if missing or len(extra) > len(skill.inputs):
        found.append(_named("not a fit: names a literal the skill does not", missing + extra))
    others = sorted(groups - own_groups)
    if others:
        found.append(_named("not a fit: names what the skill does not", others))
    places = [stem for stem in request.named if not _TaskWords.build((stem,)).hits(body)]
    if places:
        found.append(_named("not a fit: names a place the skill does not", places))
    if _misordered(orders, request.order):
        found.append("not a fit: binds objects or places unlike the skill")
    return found


@dataclass(frozen=True, slots=True)
class _TaskWords:
    """The task's words and the lookups every skill is compared with."""

    words: tuple[str, ...]
    long: tuple[str, ...]  # words of at least PREFIX_MIN letters
    by_key: dict[str, tuple[str, ...]]  # word or its long proper prefix -> words
    keys: frozenset[str]
    starts: tuple[str, ...]  # merged ranges of words that start with a long word
    ends: tuple[str, ...]

    @classmethod
    def build(cls, task_words: tuple[str, ...]) -> _TaskWords:
        by_key: dict[str, list[str]] = {}
        for word in task_words:
            by_key.setdefault(word, []).append(word)
            for size in range(PREFIX_MIN, len(word)):
                by_key.setdefault(word[:size], []).append(word)
        long = tuple(word for word in task_words if len(word) >= PREFIX_MIN)
        starts: list[str] = []
        ends: list[str] = []
        for word in sorted(long):
            if ends and word < ends[-1]:
                continue  # inside the range of a shorter word it starts with
            starts.append(word)
            ends.append(word + _AFTER_ALL)
        return cls(
            task_words,
            long,
            {key: tuple(value) for key, value in by_key.items()},
            frozenset(by_key),
            tuple(starts),
            tuple(ends),
        )

    def hits(self, index: _WordIndex) -> set[str]:
        """The task words that match some word of ``index``."""
        found: set[str] = set()
        for key in index.exact & self.keys:
            found.update(self.by_key[key])
        ordered = index.ordered
        for word in self.long:
            if word not in found:
                at = bisect_left(ordered, word)
                if at < len(ordered) and ordered[at].startswith(word):
                    found.add(word)
        return found

    def _in_ranges(self, word: str) -> bool:
        at = bisect_right(self.starts, word) - 1
        return at >= 0 and word < self.ends[at]

    def covered(self, index: _WordIndex) -> int:
        """How many words of ``index`` match some task word."""
        ordered = index.ordered
        in_ranges = sum(
            bisect_left(ordered, end) - bisect_left(ordered, start)
            for start, end in zip(self.starts, self.ends)
        )
        by_key = index.exact & self.keys
        return in_ranges + sum(1 for word in by_key if not self._in_ranges(word))


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillWithState:
    """One skill version offered for matching, with its computed state.

    ``status`` and ``health`` are computed from the skill's events elsewhere
    (skill_lifecycle); here they only filter and order, never authorise.
    """

    skill: Skill
    status: SkillStatus
    health: SkillHealth

    def __post_init__(self) -> None:
        if type(self.skill) is not Skill:
            raise SkillMatchError("skill must be a Skill")
        if type(self.status) is not SkillStatus:
            raise SkillMatchError("status must be a SkillStatus")
        if type(self.health) is not SkillHealth:
            raise SkillMatchError("health must be a SkillHealth")


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchCandidate:
    """One matched skill: how well it matched and why. Advice only."""

    skill: Skill
    kind: MatchKind
    score: int
    coverage: int
    reasons: tuple[str, ...]
    inputs: tuple[tuple[str, str], ...] = ()

    @property
    def skill_id(self) -> str:
        return self.skill.skill_id

    @property
    def version(self) -> int:
        return self.skill.version


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchResult:
    """``kind`` is ``best``'s kind, or ``none`` with no ``best``.

    ``best`` is always ``candidates[0]`` when there is one. Candidates list
    fits first, then similar skills; within each kind the order is score,
    coverage, success rate, last success (newest first), then skill_id.
    ``needs_revalidation`` lists in-scope stale skills that would have matched;
    they are never a fit and never ``best``.
    """

    kind: MatchKind
    best: MatchCandidate | None
    candidates: tuple[MatchCandidate, ...]
    needs_revalidation: tuple[MatchCandidate, ...]


def _in_scope(skill: Skill, project_id: str | None) -> bool:
    """Allowlist: global always; project only for the exact same project."""
    scope = skill.scope
    if scope.kind is SkillScopeKind.GLOBAL:
        return True
    if scope.kind is SkillScopeKind.PROJECT:
        return project_id is not None and scope.project_id == project_id
    return False


def _named(label: str, items: Iterable[str]) -> str:
    listed = list(items)
    shown = ", ".join(listed[:MAX_REASON_WORDS])
    more = len(listed) - MAX_REASON_WORDS
    return f"{label}: {shown}" + (f" (+{more})" if more > 0 else "")


def _assess(
    skill: Skill,
    task: _TaskWords,
    request: _CueText,
    literals: tuple[str, ...],
    truncated: bool,
    said: str | None,
    bound: bool,
) -> tuple[MatchKind, int, int, tuple[str, ...]]:
    index = _skill_words(skill)
    body_hits = task.hits(index.body)
    matched = [word for word in task.words if word in body_hits]
    unbound = [word for word in task.words if word not in body_hits]
    score = 100 * len(matched) // len(task.words)
    if score < SIMILAR_MIN_SCORE or len(matched) < SIMILAR_MIN_MATCHED:
        return MatchKind.NONE, score, 0, ()
    head_hits = task.hits(index.headline)
    in_headline = [word for word in matched if word in head_hits]
    head_size = len(index.headline.exact)
    covered = task.covered(index.headline)
    coverage = 100 * covered // head_size if head_size else 0
    # A placeholder skill is its own text only through the user's typed binding
    # (CDXINT54G C1): no request, or its bare name, never FITs it.
    placeholder = any(_PLACEHOLDER.search(t) for t in (skill.name, skill.summary))
    own_text = bound if placeholder else said is not None and said in _skill_said(skill)
    fit = (
        own_text
        and score >= FIT_MIN_SCORE
        and len(matched) >= FIT_MIN_MATCHED
        and bool(in_headline)
        and coverage >= FIT_MIN_COVERAGE
        and not unbound
        and not truncated
    )
    # A bound request is the skill's own text; model-read word gates cannot veto it.
    fit = fit or bound and not truncated
    conflicts = _conflicts(skill, index.body, request, literals, task.words) if fit else []
    kind = MatchKind.FIT if fit and not conflicts else MatchKind.SIMILAR
    reasons = [
        f"scope: {skill.scope.kind.value}",
        f"score {score}: {len(matched)} of {len(task.words)} task words matched",
        _named("matched words", matched),
    ]
    if in_headline:
        reasons.append(_named("in name or summary", in_headline))
    reasons.append(
        f"coverage {coverage}: {covered} of {head_size} name and summary words matched"
    )
    if kind is MatchKind.SIMILAR:
        if not own_text:
            reasons.append("not a fit: request is not the skill's own text (name or summary)")
        if score < FIT_MIN_SCORE:
            reasons.append(f"not a fit: score below {FIT_MIN_SCORE}")
        if len(matched) < FIT_MIN_MATCHED:
            reasons.append(f"not a fit: fewer than {FIT_MIN_MATCHED} matched words")
        if not in_headline:
            reasons.append("not a fit: no task word in its name or summary")
        if coverage < FIT_MIN_COVERAGE:
            reasons.append(f"not a fit: coverage below {FIT_MIN_COVERAGE}")
        if unbound:
            reasons.append(_named("not a fit: request words the skill does not bind", unbound))
        if truncated:
            reasons.append("not a fit: request too long to read whole")
        reasons.extend(conflicts)
    return kind, score, coverage, tuple(reasons)


def _micros(stamp: str | None) -> int | None:
    if stamp is None:
        return None
    return (datetime.fromisoformat(stamp) - _EPOCH) // timedelta(microseconds=1)


def _order_key(kind: MatchKind, score: int, coverage: int, entry: SkillWithState) -> tuple:
    health = entry.health
    # A never-used skill has no success rate: it ranks as 0, below any success.
    rate = (
        Fraction(health.success_count, health.use_count) if health.use_count else Fraction(0)
    )
    last = _micros(health.last_success_at)
    return (
        0 if kind is MatchKind.FIT else 1,
        -score,
        -coverage,
        -rate,
        (1, 0) if last is None else (0, -last),
        entry.skill.skill_id,
    )


def _entries(skills_with_state: Iterable[SkillWithState]) -> tuple[SkillWithState, ...]:
    if isinstance(skills_with_state, (str, bytes)) or not isinstance(
        skills_with_state, Iterable
    ):
        raise SkillMatchError("skills_with_state must be an iterable of SkillWithState")
    entries: list[SkillWithState] = []
    seen: set[str] = set()
    for entry in skills_with_state:
        if len(entries) >= MAX_SKILLS:
            raise SkillMatchError(f"more than {MAX_SKILLS} skills offered")
        if type(entry) is not SkillWithState:
            raise SkillMatchError("skills_with_state items must be SkillWithState")
        if entry.skill.skill_id in seen:
            # Two versions of one skill: which one is active is ambiguous.
            raise SkillMatchError("a skill is offered more than once")
        seen.add(entry.skill.skill_id)
        entries.append(entry)
    return tuple(entries)


def match_skills(
    task_text: str,
    skills_with_state: Iterable[SkillWithState],
    project_id: str | None,
    *,
    request: str | None = None,
) -> MatchResult:
    """Match a task against the offered skills; see the module docstring.

    ``request`` is the user's own typed text, never the model's; a fit's
    inputs are its spans at the skill's placeholders (none without it).
    Offer one entry per skill (its active version). Wrong argument types, a
    malformed ``project_id`` (Core's project id pattern), a skill offered
    twice or more than ``MAX_SKILLS`` skills raise ``SkillMatchError``.
    """
    if type(task_text) is not str:
        raise SkillMatchError("task_text must be a string")
    if request is not None and type(request) is not str:
        raise SkillMatchError("request must be None or a string")
    if project_id is not None and (
        type(project_id) is not str or PROJECT_ID_PATTERN.fullmatch(project_id) is None
    ):
        raise SkillMatchError(
            "project_id must be None or a project id (3-64 of a-z, 0-9 and -, "
            "not starting with -)"
        )
    entries = _entries(skills_with_state)
    # Any part of the request left unread caps every match at SIMILAR.
    truncated = len(task_text) > MAX_TASK_CHARS
    task_text = cut_text(task_text, MAX_TASK_CHARS)
    folded = fold_token(task_text)
    all_words = words(task_text)
    truncated = (
        truncated
        or len(all_words) > MAX_TASK_WORDS
        or any(len(run) > WORD_MAX for run in _WORD_RUN.findall(folded))
    )
    task_words = all_words[:MAX_TASK_WORDS]
    if not task_words:
        return MatchResult(kind=MatchKind.NONE, best=None, candidates=(), needs_revalidation=())
    task = _TaskWords.build(task_words)
    cues = _CueText.read(folded)
    literals = _literals(task_text)
    # Only the user's own typed text can be a skill's own text: no request, or
    # a blank one, never FITs, even a placeholder-free skill (CDXINT54H M1).
    said = _said(request) if request and request.strip() else None

    ranked: list[tuple[tuple, MatchCandidate]] = []
    stale: list[tuple[tuple, MatchCandidate]] = []
    for entry in entries:
        skill = entry.skill
        if not _in_scope(skill, project_id):
            continue
        if entry.status is SkillStatus.ACTIVE:
            target = ranked
        elif entry.status is SkillStatus.STALE:
            target = stale
        else:  # disabled, deleted, or anything unknown: never offered
            continue
        bound = _bind(skill, request)
        kind, score, coverage, reasons = _assess(
            skill, task, cues, literals, truncated, said, bound is not None
        )
        if kind is MatchKind.NONE:
            continue
        if target is stale:
            reasons = reasons + (
                f"stale: needs revalidation before use (would be {kind.value})",
            )
            kind = MatchKind.SIMILAR
        inputs = tuple(sorted(bound.items())) if bound and kind is MatchKind.FIT else ()
        candidate = MatchCandidate(
            skill=skill, kind=kind, score=score, coverage=coverage, reasons=reasons, inputs=inputs
        )
        target.append((_order_key(kind, score, coverage, entry), candidate))

    ranked.sort(key=lambda pair: pair[0])
    stale.sort(key=lambda pair: pair[0])
    candidates = tuple(candidate for _, candidate in ranked[:MAX_CANDIDATES])
    best = candidates[0] if candidates else None
    return MatchResult(
        kind=MatchKind.NONE if best is None else best.kind,
        best=best,
        candidates=candidates,
        needs_revalidation=tuple(candidate for _, candidate in stale[:MAX_CANDIDATES]),
    )
