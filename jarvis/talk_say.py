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
# A fence-ish line: ``` or ~~~ after any mix of spaces, quote (">") and list
# ("-", "*", "+", "•", "1.", "1)", task box "[ ]") markers. It ends speech for the
# rest of the answer; no closer is ever matched, so nested code is never read aloud.
_FENCE_RE = re.compile(r"(?:[\s>*+•-]|\d{1,9}[.)]|\[[ xX]\])*(?:```|~~~)")
# Patterns below avoid two adjacent runs over the same characters and start
# only at the beginning of a run: a 20000-char line must stay linear.
_TABLE_SEP_RE = re.compile(r"\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?")  # fullmatch on a stripped line
_QUOTE_RE = re.compile(r"^ {0,3}(?:> ?)+")  # blockquote prefix, at least one ">"
_BACKTICKS_RE = re.compile(r"`+")
# HTML code: up to its closer, or to the end of the answer (the rest stays in text).
_HTML_CODE_RE = re.compile(r"<(code|pre)\b[^<>]{0,80}>.*?(?:(</\1\s*>)|\Z)", re.S | re.I)
_TARGET = r"\((?:[^()\n]|\([^()\n]{0,2000}\)){0,2000}\)"  # one level of balanced parentheses
_LABEL = r"\[((?:[^\[\]\n]|\[[^\[\]\n]{0,200}\]){0,200})\]"  # one level of nested brackets
_IMAGE_RE = re.compile("!" + _LABEL + _TARGET)
_LINK_RE = re.compile(_LABEL + _TARGET)
_URL_CH = r"[^\s<>\"'()\]\x00]"
_URL_RE = re.compile(
    r"(?:\b[a-zA-Z][a-zA-Z0-9+.-]{1,15}://|\bwww\."
    r"|\b(?i:mailto|tel|sms|callto|xmpp|data|javascript|file|magnet):(?=[^\s\x00]))"
    rf"(?:{_URL_CH}|\((?:{_URL_CH}|\({_URL_CH}*\))*\)?)+"  # two levels of balanced parentheses
)
# A path start: drive (with "Program Files" kept whole), UNC, home, relative or absolute.
_DRIVE = r"(?:\b[A-Za-z]:[\\/](?:[Pp]rogram [Ff]iles(?: \(x86\))?)?|\\\\)"
_ANCHOR = r"(?:" + _DRIVE + r"|~[\\/]|\.{1,2}[\\/]|(?<![\w.])/(?=[\w.-]))"
_RUN = r"[^\s<>\"'`|\x00]"
_WORD = r"[^\s<>\"'`|\x00\\/]"
_PATH_RE = re.compile(
    "".join(q + _ANCHOR + "[^" + c + r"\n]{0,260}" + c + "|" for q, c in ('""', "''", "“”", "«»"))
    # Unquoted: later space-joined words belong to the path while a later word holds a
    # separator: any distance to a "\" for drive/UNC ("OneDrive - Acme Corp\x"), else
    # within three words ("/" is common in prose: "km/saat", "ve/veya").
    + _DRIVE + _RUN + rf"*(?:(?: {_WORD}+)* {_WORD}*\\{_RUN}*|(?: {_WORD}+){{0,2}} {_WORD}*/{_RUN}*)*|"
    + _ANCHOR + _RUN + rf"*(?:(?: {_WORD}+){{0,2}} {_WORD}*[\\/]{_RUN}*)*"
    + r"|(?<![\w.\\/-])[\w.-]+(?:[\\/][\w.-]+)+\.[A-Za-z0-9]{1,8}\b"
    r"|(?<![\w.\\/-])[\w.-]+(?:[\\/][\w.-]+)*[\\/]\.\w[\w.-]*"  # dotfile last segment: secrets/.env
    r"|(?<![\w-])[\w-]+\.(?:py|md|json|txt|js|ts|qml|toml|ya?ml|ps1|sh|csv|log|db|html|css|cfg|ini|exe|dll)\b"
)
_HASH_RE = re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\b")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]{0,80}>")
_WRAPPED_MARK_RE = re.compile(r"<\s*(\x00\w+\x00)\s*>")  # autolink "<https://x>"
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_LINE_MARKS = (
    (re.compile(r"^\s*(?:[-*_]\s*){3,}$"), ""),  # horizontal rule
    (re.compile(r"^\s*=+\s*$"), ""),  # setext heading underline
    (re.compile(r"^\s*(?:>\s?)+"), ""),  # blockquote
    (re.compile(r"^\s{0,3}#{1,6}\s+"), ""),  # heading
    (re.compile(r"(?<=\s)#+\s*$"), ""),  # closing heading hashes
    (re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?"), ""),  # bullet / task box
)
_INLINE_MARKS = (
    (re.compile(r"[*~`]+"), ""),
    (re.compile(r"(?<!\w)_+|(?<!_)_+(?!\w)"), ""),
    (re.compile(r"\|"), " "),
)


def _clean(text: str) -> str:
    """NFC, unify line ends, drop control, bidi and zero-width characters."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n").replace("\t", "    ")  # a tab indent is a code indent
    return "".join(
        ch
        for ch in text
        if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
    )


def _blocks(lines: list[str]) -> tuple[list[str], bool]:
    """Replace indented code and table lines by one marker line per block.

    The first fence-ish line becomes a code marker and cuts the rest of the
    answer (the second value is True): fail closed, never read code aloud.
    """
    out: list[str] = []
    table = indented = prev_quoted = False
    prev_blank = True
    for i, line in enumerate(lines):
        # Table and indent checks read the line inside its blockquote prefix.
        quote = _QUOTE_RE.match(line)
        body = line[quote.end():] if quote else line
        stripped = body.strip()
        # A quote starting or ending opens a new block, like a blank line.
        blank_before = prev_blank or bool(quote) != prev_quoted
        prev_blank, prev_quoted = not stripped, bool(quote)
        if _FENCE_RE.match(line):
            out.append(_MARK.format("code"))
            return out, True
        # Indented code runs through blank lines; it cannot interrupt a paragraph.
        if indented and (not stripped or body.startswith("    ")):
            continue
        indented = False
        if blank_before and stripped and body.startswith("    "):
            indented = True
            out.append(_MARK.format("code"))
            continue
        if table and "|" in stripped:
            continue  # body rows, with or without leading pipes
        table = False
        nxt = _QUOTE_RE.sub("", lines[i + 1], count=1).strip() if i + 1 < len(lines) else ""
        if (
            stripped.startswith("|")
            or "|" in stripped and _TABLE_SEP_RE.fullmatch(stripped)
            or "|" in stripped and "|" in nxt and _TABLE_SEP_RE.fullmatch(nxt)
        ):
            table = True  # ends at a blank line or a line without "|"
            if not (out and out[-1] == _MARK.format("table")):
                out.append(_MARK.format("table"))
            continue
        out.append(line)
    return out, False


def _tag(m: re.Match) -> str:
    """Drop a tag but keep the kinds inside it ("<a href=https://x>")."""
    inner = _PATH_RE.sub(_MARK.format("path"), m.group(0)[1:-1].lstrip("/"))
    inner = _HASH_RE.sub(_MARK.format("hash"), inner)
    return " " + "".join(_MARK.format(k) for k in _MARK_RE.findall(inner)) + " "


def _code_spans(line: str) -> str:
    """CommonMark code spans in one linear pass: a run closes only on the next equal-length run."""
    runs = [(m.start(), m.end()) for m in _BACKTICKS_RE.finditer(line)]
    nxt: list[int | None] = [None] * len(runs)
    last: dict[int, int] = {}
    for i in range(len(runs) - 1, -1, -1):
        size = runs[i][1] - runs[i][0]
        nxt[i], last[size] = last.get(size), i
    out, pos, i = [], 0, 0
    while i < len(runs):
        j = nxt[i]
        if j is None:
            i += 1  # no closer: a literal run, dropped later with the other marks
            continue
        out += [line[pos:runs[i][0]], _MARK.format("code")]
        pos, i = runs[j][1], j + 1
    return "".join(out) + line[pos:]


def _paragraph_spans(blocks: list[str]) -> list[str]:
    """Code spans per paragraph (a span may cross lines); blank, quote-only and marker lines end one."""
    out: list[str] = []
    para: list[str] = []
    for line in blocks + [""]:
        if line.strip(" >") and not _MARK_RE.fullmatch(line):
            para.append(line)
            continue
        if para:
            out += _code_spans("\n".join(para)).split("\n")
        out.append(line)
        para = []
    return out[:-1]


def _inline(line: str) -> str:
    line = _IMAGE_RE.sub(r"\1 " + _MARK.format("url"), line)  # alt text spoken, target not
    line = _LINK_RE.sub(r"\1 " + _MARK.format("url"), line)  # label spoken, target not
    line = _URL_RE.sub(_MARK.format("url"), line)
    line = _HTML_TAG_RE.sub(_tag, line)  # before paths, or "</b>" reads as a path
    line = _PATH_RE.sub(_MARK.format("path"), line)
    line = _HASH_RE.sub(_MARK.format("hash"), line)
    line = _WRAPPED_MARK_RE.sub(r"\1", line)
    line = _ESCAPE_RE.sub(r"\1", line)
    for pattern, repl in _LINE_MARKS + _INLINE_MARKS:
        line = pattern.sub(repl, line)
    return " ".join(line.split())


def _cap(spoken: str) -> tuple[str, bool]:
    if len(spoken) <= SPOKEN_CAP:
        return spoken, False
    head = spoken[: SPOKEN_CAP + 1]
    end = max(head.rfind(p) for p in (". ", "! ", "? ", "\n"))
    space = max(head.rfind(" "), head.rfind("\n"))
    # Sentence end, then word end in the second half; else any sentence or word end; else mid-word.
    cut = next((c for c in (end, space) if c >= SPOKEN_CAP // 2), end if end > 0 else space)
    if cut < 1:
        cut = SPOKEN_CAP - 1
    return spoken[: cut + 1].rstrip(), True


def _prepare(text: str) -> Speech | Refusal:
    blocks, cut = _blocks(_clean(text).split("\n"))

    def html_code(m: re.Match) -> str:
        nonlocal cut
        cut = cut or m.group(2) is None
        return _MARK.format("code")

    body = _HTML_CODE_RE.sub(html_code, "\n".join(_paragraph_spans(blocks)))
    lines = [_inline(line) for line in body.split("\n")]
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
    spoken, capped = _cap(spoken)
    rest = capped or cut
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
