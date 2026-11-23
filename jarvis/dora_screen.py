"""DORAGATE1: the one Dora/company deny + secret screen every egress adapter calls.

``screen(text)`` -> ``(ok, reason)``. It refuses on a deny-list hit, on an API-key-shaped
secret and on any doubt (unreadable or malformed list, non-text input, a screen error).
``reason`` is a fixed label, never the matched text; nothing here logs or prints.
The deny list is ``scripts/lesson_deny_patterns.json`` (moved from lesson_extract,
LESSONS1..FIX4): shapes are regexes, words are matched in a letters skeleton that folds
case, accents, Turkish letters, homoglyphs, leet digits and separators.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

DENY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "lesson_deny_patterns.json"
Deny = tuple[int, list[re.Pattern[str]], list[tuple[str, bool, bool]]]

_TR = str.maketrans("çğıöşüâîû", "cgiosuaiu")
# sk-ant keys, GitHub tokens and any key/token/secret/password = 32+ char value.
# DORAGATE1FIX: linear time. The name branch starts only at the start of a [\w-] run,
# checks the keyword by lookahead and takes the run possessively (same matches as before).
_SECRET = re.compile(
    r"sk-ant-[\w-]{8,}"
    r"|(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})"
    r"|(?<![\w-])(?i:(?=[\w-]*?(?:key|token|secret|passw(?:or)?d|pwd))[\w-]++)"
    r"[\"']?\s*[=:]\s*[\"']?[^\s\"',;]{32,}")


def load_deny(path: Path | None = None) -> Deny:
    """(version, shapes, words). A word is (letters, needs_start, needs_end): '^' / '$'
    in the JSON ask for a word boundary in the letters skeleton (see _skeleton).
    ``path`` None reads DENY_PATH at call time (tests patch it)."""
    d = json.loads((DENY_PATH if path is None else path).read_text(encoding="utf-8"))
    if not (isinstance(d, dict) and type(d.get("shapes")) is list and type(d.get("words")) is list
            and all(type(x) is str for x in d["shapes"] + d["words"])
            and (d["shapes"] or d["words"])):  # an empty list would allow everything
        raise ValueError("deny list needs lists of strings, not both empty")
    words = [(w.strip("^$"), w.startswith("^"), w.endswith("$")) for w in d["words"]]
    if not all(w and w.isalnum() for w, _, _ in words):
        raise ValueError("deny words must be letters/digits with optional ^ $")
    return int(d["version"]), [re.compile(p, re.I) for p in d["shapes"]], words


_DATE = re.compile(r"(?<!\d)(?:19|20)\d\d-[01]\d-[0-3]\d(?!\d)")
_GLUED = re.compile(r"(?<=[^\W\d_])\d{1,3}(?!\d)")  # O52, UIRED5: ids glued to letters
_MARK = ("Mn", "Mc", "Me", "Cf")
# Cyrillic / Greek lookalikes (after casefold) -> Latin, for the letters skeleton only.
_HOMO = str.maketrans("аеорсухіјѕԁкмнтвοαειкνρτυχβημ", "aeopcyxijsdkmhtboaeiknptuxbhm")
# LESSONS1FIX4: leet digits ('d0ra') and Unicode small capitals ('ᴅora'), skeleton only.
_LEET = str.maketrans(  # LATIN LETTER SMALL CAPITAL A..Z (Unicode has no X)
    "013457ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍ"
    "ɴᴏᴘꞯʀꜱᴛᴜᴠᴡʏᴢ",
    "oieastabcdefghijklmnopqrstuvwyz")


def _norm(text: str):
    """Whole-string NFKC (each base char composed with its combining marks, so decomposed
    mü / ş compose), Cf (zero-width, soft hyphen) dropped, any script's digits -> ASCII.
    Returns (normalized, origin index in text per char)."""
    out, org, i, n = [], [], 0, len(text)
    while i < n:
        j = i + 1
        while j < n and unicodedata.category(text[j]) in _MARK:
            j += 1
        unit = "".join(c for c in text[i:j] if unicodedata.category(c) != "Cf")
        for c in unicodedata.normalize("NFKC", unit):
            out.append(str(unicodedata.decimal(c)) if c.isdecimal() else c)
            org.append(i)
        i = j
    return "".join(out), org


def _skeleton(n: str):
    """Letters/digits only, casefolded, accents and Turkish letters folded, lookalikes
    mapped: 'Garanti-BBVA', 'YapıKredi', 'dora_notes', 'D.O.R.A', 'yapi\\nkredi' all read
    as joined letters. Returns (skeleton, index in n per char, boundary-before per char);
    a boundary is a removed separator, a lower->Upper step or a letter<->digit step."""
    sk, so, sb, gap, prev = [], [], [], True, ""
    # hex ids (commit shas) skip leet folding, else 'a7eb1f0c' reads as 'teb'
    hexrun = {i for m in re.finditer(r"(?<![^\W_])[0-9a-fA-F]{7,}(?![^\W_])", n)
              for i in range(*m.span())}
    for i, ch in enumerate(n):
        if unicodedata.category(ch) in _MARK:
            continue
        if not ch.isalnum():
            gap = True
            continue
        b = gap or (prev.islower() and ch.isupper()) or prev.isdigit() != ch.isdigit()
        for c in unicodedata.normalize("NFD", ch.casefold()):
            if c.isalnum():
                c = c.translate(_TR).translate(_HOMO)
                sk.append(c if i in hexrun else c.translate(_LEET))
                so.append(i)
                sb.append(b)
                b = False
        gap, prev = False, ch
    sb.append(True)
    return "".join(sk), so, sb


def _at_collapse(n: str):
    """n with every non-letter/digit run on either side of each '@' removed
    ('salih @example.com', 'salih@ example.com'). Returns (text, index in n per char)."""
    drop = set()
    for i, ch in enumerate(n):
        if ch == "@":
            for step in (-1, 1):
                j = i + step
                while 0 <= j < len(n) and j not in drop and n[j] != "@" and not n[j].isalnum():
                    drop.add(j)
                    j += step
    keep = [i for i in range(len(n)) if i not in drop]
    return "".join(n[i] for i in keep), keep


def shape_hits(text: str, shapes, n=None, org=None) -> list:
    """(start, end) offsets in text of every shape hit. Views: raw, normalized and the
    '@'-collapsed normalized text; each seen unmasked, with ISO dates blanked and with
    letter-glued 1-3 digit ids hidden (LESSONS1FIX3: the unmasked pass catches
    'Tel53 2026-11-14')."""
    if n is None:
        n, org = _norm(text)
    a, ao = _at_collapse(n)
    views = [(n, org)] + [(text, None)] * (text != n) + [(a, [org[k] for k in ao])] * (a != n)
    out = []
    for t, o in views:  # identical views skipped: same hits, a third of the time
        for v in (t, _DATE.sub(lambda m: " " * len(m.group()), t),
                  _GLUED.sub(lambda m: "#" * len(m.group()), t)):
            for p in shapes:
                for m in p.finditer(v):
                    s, e = m.span()
                    out.append((s, e) if o is None else (o[s], o[max(e - 1, s)] + 1))
    return out


def _screen(text: str, deny) -> list:
    """(start, end) offsets in text of every shape and word hit."""
    _, shapes, words = deny
    if not (shapes or words):
        raise ValueError("empty deny list")  # fail closed: denied() drops, screen() doubts
    n, org = _norm(text)
    out = shape_hits(text, shapes, n, org)
    sk, so, sb = _skeleton(n)
    for w, st, en in words:
        i = sk.find(w)
        while i >= 0:
            e = i + len(w)
            if (not st or sb[i]) and (not en or sb[e]):
                out.append((org[so[i]], org[so[e - 1]] + 1))
            i = sk.find(w, i + 1)
    return out


def denied(text: str, deny) -> bool:
    """True = drop. Any doubt (error, non-text) also drops."""
    try:
        return bool(_screen(text, deny))
    except Exception:  # fail closed
        return True


def screen(text: object, deny: Deny | None = None) -> tuple[bool, str]:
    """(True, "") = may leave; else (False, label). ``deny`` None loads DENY_PATH now."""
    if type(text) is not str:
        return False, "doubt:type"
    try:
        deny = load_deny() if deny is None else deny
        if not (deny[1] or deny[2]):
            raise ValueError("empty deny list")
    except Exception:  # fail closed: unreadable or malformed list
        return False, "doubt:list"
    try:
        if _SECRET.search(text) or _SECRET.search(_norm(text)[0]):
            return False, "secret"
        return (False, "deny") if _screen(text, deny) else (True, "")
    except Exception:  # fail closed
        return False, "doubt:screen"
