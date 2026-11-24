"""Speech preparation of a written answer (design section 8).

Pure: decides what of an answer is read aloud and what stays text only.
Code blocks, tables, file paths, URLs and hashes are never spoken; each
kind is replaced by one short Turkish cue the first time it appears.
Markdown marks are dropped, invisible/control characters removed, and the
spoken text is capped. Nothing here does I/O; nothing in the app imports it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

MAX_INPUT_CHARS = 20000
SPOKEN_CAP = 600

# Order is the order kinds are reported in when several appear.
CUES = {
    "code": "kod yazıda",
    "table": "tablo yazıda",
    "path": "dosya yolu yazıda",
    "url": "bağlantı yazıda",
    "hash": "özet değeri yazıda",
}
REST = "rest"

_REFUSALS = {
    "wrong_type": "input must be a mapping with a string field 'text'",
    "missing_field": "input has no 'text' field",
    "empty": "text is empty or has nothing to speak",
    "too_long": "text is longer than 20000 characters",
    "too_deep": "input is nested too deeply",
    "malformed": "input could not be prepared for speech",
}


@dataclass(frozen=True)
class Speech:
    spoken: str
    text_only: tuple[str, ...]
    rest_in_text: bool
    ok: bool = True


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str
    ok: bool = False


def _refuse(reason: str) -> Refusal:
    return Refusal(reason, _REFUSALS[reason])


_MARK = "\x00{}\x00"  # NUL never survives _clean, so it is a safe placeholder
_MARK_RE = re.compile(r"\x00(\w+)\x00")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_INLINE_CODE_RE = re.compile(r"(`+)[^`\n]+?\1")
_IMAGE_RE = re.compile(r"!\[([^\]\n]{0,200})\]\([^)\n]{0,2000}\)")
_LINK_RE = re.compile(r"\[([^\]\n]{0,200})\]\([^)\n]{0,2000}\)")
_URL_RE = re.compile(r"(?:\b[a-zA-Z][a-zA-Z0-9+.-]{1,15}://|\bwww\.)[^\s<>\"')\]\x00]+")
_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|\\\\|~[\\/]|\.{1,2}[\\/]|(?<![\w.])/(?=[\w.-]))[^\s<>\"'`|\x00]*"
    r"|\b[\w.-]+(?:[\\/][\w.-]+)+\.[A-Za-z0-9]{1,8}\b"
    r"|\b[\w-]+\.(?:py|md|json|txt|js|ts|qml|toml|ya?ml|ps1|sh|csv|log|db|html|css|cfg|ini|exe|dll)\b"
)
_HASH_RE = re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\b")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]{0,80}>")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_LINE_MARKS = (
    (re.compile(r"^\s*(?:[-*_]\s*){3,}$"), ""),  # horizontal rule
    (re.compile(r"^\s*(?:>\s?)+"), ""),  # blockquote
    (re.compile(r"^\s{0,3}#{1,6}\s+"), ""),  # heading
    (re.compile(r"\s+#+\s*$"), ""),  # closing heading hashes
    (re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?"), ""),  # bullet / task box
)
_INLINE_MARKS = (
    (re.compile(r"[*~`]+"), ""),
    (re.compile(r"(?<!\w)_+|_+(?!\w)"), ""),
    (re.compile(r"\|"), " "),
)


def _clean(text: str) -> str:
    """NFC, unify line ends, drop control, bidi and zero-width characters."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n").replace("\t", " ")
    return "".join(
        ch
        for ch in text
        if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )


def _blocks(lines: list[str]) -> list[str]:
    """Replace fenced code and table lines by one marker line per block."""
    out: list[str] = []
    fence = ""
    for i, line in enumerate(lines):
        if fence:
            m = _FENCE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                if not line.strip().strip(fence[0]):
                    fence = ""
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)  # unclosed fence: the rest of the text is code
            out.append(_MARK.format("code"))
            continue
        stripped = line.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (
            stripped.startswith("|")
            or _TABLE_SEP_RE.match(line) and "|" in line
            or "|" in stripped and _TABLE_SEP_RE.match(nxt) and "|" in nxt
        ):
            if not (out and out[-1] == _MARK.format("table")):
                out.append(_MARK.format("table"))
            continue
        out.append(line)
    return out


def _inline(line: str) -> str:
    line = _INLINE_CODE_RE.sub(_MARK.format("code"), line)
    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1 " + _MARK.format("url"), line)  # label spoken, target not
    line = _URL_RE.sub(_MARK.format("url"), line)
    line = _HTML_TAG_RE.sub(" ", line)  # before paths, or "</b>" reads as a path
    line = _PATH_RE.sub(_MARK.format("path"), line)
    line = _HASH_RE.sub(_MARK.format("hash"), line)
    line = _ESCAPE_RE.sub(r"\1", line)
    for pattern, repl in _LINE_MARKS + _INLINE_MARKS:
        line = pattern.sub(repl, line)
    return " ".join(line.split())


def _cap(spoken: str) -> tuple[str, bool]:
    if len(spoken) <= SPOKEN_CAP:
        return spoken, False
    head = spoken[: SPOKEN_CAP + 1]
    cut = max(head.rfind(p) for p in (". ", "! ", "? ", "\n"))
    if cut < SPOKEN_CAP // 2:
        cut = max(head.rfind(" "), head.rfind("\n"))
    if cut < SPOKEN_CAP // 2:
        cut = SPOKEN_CAP - 1
    return spoken[: cut + 1].rstrip(), True


def _prepare(text: str) -> Speech | Refusal:
    lines = [_inline(line) for line in _blocks(_clean(text).split("\n"))]
    joined = "\n".join(line for line in lines if line)
    seen: list[str] = []

    def cue(m: re.Match) -> str:
        kind = m.group(1)
        if kind in seen:
            return ""
        seen.append(kind)
        return " " + CUES[kind] + " "

    spoken = "\n".join(
        " ".join(line.split()) for line in _MARK_RE.sub(cue, joined).split("\n")
    )
    spoken = "\n".join(line for line in spoken.split("\n") if line)
    if not spoken:
        return _refuse("empty")
    spoken, rest = _cap(spoken)
    kinds = tuple(k for k in CUES if k in seen) + ((REST,) if rest else ())
    return Speech(spoken, kinds, rest)


def prepare_speech(answer: object) -> Speech | Refusal:
    """Split a written answer into what is spoken and what stays text only.

    `answer` is a mapping with a string field "text". Never raises: any bad
    input comes back as a Refusal with an ASCII reason code and message.
    """
    try:
        if not isinstance(answer, Mapping):
            return _refuse("wrong_type")
        if "text" not in answer:
            return _refuse("missing_field")
        text = answer["text"]
        if not isinstance(text, str):
            return _refuse("wrong_type")
        if len(text) > MAX_INPUT_CHARS:
            return _refuse("too_long")
        if not _clean(text).strip():
            return _refuse("empty")
        return _prepare(text)
    except RecursionError:
        return _refuse("too_deep")
    except Exception:  # fail closed: a hostile mapping or a regex surprise never escapes
        return _refuse("malformed")
