"""Shown transcript ("Anladığım: ...") and glossary candidates from a correction (design §8).

Pure and deterministic: no I/O, no network, no model calls. Nothing here writes the
glossary; candidates are returned as records for a caller to review. Every public
function returns {"ok": True, ...} or a typed refusal {"ok": False, "reason": <ASCII code>}.
"""

import difflib
import re
import unicodedata
from collections.abc import Mapping

SHOWN_PREFIX = "Anladığım: "
MAX_TEXT = 4000
MAX_CANDIDATES = 5
MAX_SIDE_WORDS = 4
_EDGE_PUNCT = ".,;:!?\"'()[]{}«»“”‘’…"
_DOMAIN = re.compile(r"[\w-]\.+[^\W\d_]{2,}")  # x.com, x..com, foo-.com: a bare domain counts as a URL


def _refuse(reason):
    # Reasons are fixed ASCII codes; user text is never echoed into a refusal.
    return {"ok": False, "reason": reason}


def _guard(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RecursionError:
            return _refuse("input_too_deep")
        except Exception:
            return _refuse("internal_error")
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _check_text(text, name):
    if not isinstance(text, str):
        return f"{name}_not_text"
    if len(text) > MAX_TEXT:
        return f"{name}_too_long"
    return None


def _replacements(raw, items):
    """Validate corrector spans (indices into raw, start < end, no overlap); None if invalid."""
    if not isinstance(items, (list, tuple)):
        return None
    spans = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        start, end, meant = item.get("start"), item.get("end"), item.get("text")
        if (type(start) is not int or type(end) is not int or not isinstance(meant, str)
                or not 0 <= start < end <= len(raw)):
            return None
        spans.append((start, end, meant))
    spans.sort()
    for (_, prev_end, _), (start, _, _) in zip(spans, spans[1:]):
        if start < prev_end:
            return None
    return [{"start": s, "end": e, "heard": raw[s:e], "meant": m} for s, e, m in spans]


def _apply(raw, records):
    out, pos = [], 0
    for rec in records:
        out.append(raw[pos:rec["start"]])
        out.append(rec["meant"])
        pos = rec["end"]
    out.append(raw[pos:])
    return "".join(out)


@_guard
def build_shown(raw, corrector):
    """Run the corrector over raw speech output and build the shown transcript.

    corrector(raw) -> (text, replacements); each replacement is a mapping
    {"start", "end", "text"} replacing raw[start:end]. A corrector that raises
    leaves the raw text shown and flagged "uncorrected". A corrector result whose
    spans are invalid, or whose text is not raw with those replacements applied,
    is refused.
    """
    problem = _check_text(raw, "raw")
    if problem:
        return _refuse(problem)
    if not raw.strip():
        return _refuse("raw_empty")
    if not callable(corrector):
        return _refuse("corrector_not_callable")
    try:
        result = corrector(raw)
    except Exception:
        return {"ok": True, "shown": raw, "line": SHOWN_PREFIX + raw,
                "replacements": [], "uncorrected": True}
    if not isinstance(result, (tuple, list)) or len(result) != 2:
        return _refuse("corrector_bad_result")
    text, items = result
    problem = _check_text(text, "corrected")
    if problem:
        return _refuse(problem)
    records = _replacements(raw, items)
    if records is None:
        return _refuse("replacement_span_invalid")
    if _apply(raw, records) != text:
        return _refuse("replacements_mismatch")
    return {"ok": True, "shown": text, "line": SHOWN_PREFIX + text,
            "replacements": records, "uncorrected": False}


def _fold(text):
    """Turkish case folding over NFC: I -> ı, İ -> i, then casefold (ß -> ss)."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("I", "ı").replace("İ", "i").casefold()
    return unicodedata.normalize("NFC", text)


def _plain(ch):
    # Allowlist, not a denylist: a separator homoglyph (ː ⧵ ∶ ꓽ), a combining overlay (c̶) or another
    # script's letter is none of ASCII-minus-separators or a Latin letter (Turkish ç ğ ı ö ş ü pass).
    if ch.isascii():
        return ch not in "/\\:@"
    return ch.isalpha() and unicodedata.name(ch, "").startswith("LATIN ")


def _words(text):
    words = []
    for token in unicodedata.normalize("NFC", text).split():
        word = token.strip(_EDGE_PUNCT)
        if word:
            words.append(word)
    return words


def _unsafe(phrase):
    if any(unicodedata.category(ch).startswith("C") for ch in phrase):
        return True  # control, format (bidi, zero-width), private, unassigned
    # Per word, after the comparison fold and NFKC (fullwidth : / . fold to ASCII). Any inner ":"
    # covers every URI scheme (mailto:, javascript:, tel:), drives and times; "@" emails; dotted
    # words bare domains (x.İo folds to x.io first). NFKC on both sides of the fold: ᵂ -> W -> w.
    for word in unicodedata.normalize("NFKC", _fold(unicodedata.normalize("NFKC", phrase))).split():
        if (not all(map(_plain, word)) or word.startswith(("~", "www."))
                or _DOMAIN.search(word)):
            return True
    return False


@_guard
def correction_candidates(shown, corrected):
    """Word-level differences between shown and corrected text as heard -> meant records.

    Words compare by _fold(). If the changed word count (per differing block, the
    larger side) is more than half of the longer text's word count, the correction
    is a new sentence and gives no candidates. Only replaced blocks become
    candidates, each side at most MAX_SIDE_WORDS words; unsafe ones are dropped;
    at most MAX_CANDIDATES are returned, in text order.
    """
    for value, name in ((shown, "shown"), (corrected, "corrected")):
        problem = _check_text(value, name)
        if problem:
            return _refuse(problem)
    heard_words, meant_words = _words(shown), _words(corrected)
    matcher = difflib.SequenceMatcher(None, [_fold(w) for w in heard_words],
                                      [_fold(w) for w in meant_words], autojunk=False)
    ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in ops)
    if changed * 2 > max(len(heard_words), len(meant_words)):
        return {"ok": True, "candidates": [], "note": "new_sentence"}
    candidates, seen = [], set()
    for tag, i1, i2, j1, j2 in ops:
        if tag != "replace" or i2 - i1 > MAX_SIDE_WORDS or j2 - j1 > MAX_SIDE_WORDS:
            continue
        heard, meant = " ".join(heard_words[i1:i2]), " ".join(meant_words[j1:j2])
        key = (_fold(heard), _fold(meant))
        if _unsafe(heard) or _unsafe(meant) or key in seen:
            continue
        seen.add(key)
        candidates.append({"heard": heard, "meant": meant})
    return {"ok": True, "candidates": candidates[:MAX_CANDIDATES]}
