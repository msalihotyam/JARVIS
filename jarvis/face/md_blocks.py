"""FACEMD1: a long answer's markdown -> typed blocks for the reading view.

Pure: no rendering, no HTML, no network. Never raises; non-str input is refused.
Streaming: Doc.stable counts the leading blocks that no appended text can change, so a renderer
may freeze blocks[:stable] while the answer is still arriving. An unclosed fence is code to the end.
Links: only http(s) targets become links (scheme matched lowercase only); everything else is text.
Raw HTML is never interpreted: it stays visible text.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_INPUT_CHARS = 200_000
MAX_BLOCKS = 2_000
MAX_BLOCK_CHARS = 50_000
MAX_ITEMS = 500
MAX_TABLE_COLS = 64
MAX_TABLE_ROWS = 500
MAX_CELL_CHARS = 1_000
MAX_HREF_CHARS = 2_048
MAX_LANG_CHARS = 32
NOTE_LABELS = ("note", "tip", "important", "warning", "caution")

_PUNCT = frozenset(string.punctuation)
_LANG_CHARS = frozenset(string.ascii_letters + string.digits + "_+#.-")
_DELIM_CELL = re.compile(r":?-+:?")


@dataclass(frozen=True)
class Inline:
    kind: str  # text | bold | italic | code | link | image (a link chip; nothing is loaded)
    text: str
    href: str = ""  # link/image only: an http(s) URL with invisible (Cf + default-ignorable) characters removed


@dataclass(frozen=True)
class Item:
    inlines: tuple[Inline, ...]
    # second (last) nesting level: one list Block per child run, each with its own ordered/start; a marker type
    # change starts a new run; child Blocks' items have no lists
    lists: tuple[Block, ...] = ()


@dataclass(frozen=True)
class Block:
    kind: str  # heading | paragraph | list | table | code | quote | rule
    inlines: tuple[Inline, ...] = ()  # heading, paragraph, quote
    level: int = 0  # heading: 1-3
    ordered: bool = False  # list
    start: int = 1  # ordered list: first number
    items: tuple[Item, ...] = ()  # list
    header: tuple[tuple[Inline, ...], ...] = ()  # table: one inline tuple per cell
    rows: tuple[tuple[tuple[Inline, ...], ...], ...] = ()
    align: tuple[str, ...] = ()  # table: left | center | right | "" per column
    language: str = ""  # code
    text: str = ""  # code
    note: str = ""  # quote: callout label from NOTE_LABELS, or ""


@dataclass(frozen=True)
class Doc:
    blocks: tuple[Block, ...] = ()
    stable: int = 0  # blocks[:stable] are final: more text cannot change them
    truncated: bool = False  # a size cap dropped input, blocks, items, rows, columns or text
    refused: str = ""  # "" | not_text | internal


def parse(text: object) -> Doc:
    """Markdown text -> Doc; never raises."""
    if type(text) is not str:  # identity: a str subclass may override split/replace
        return Doc(refused="not_text")
    try:
        return _Parser().run(text)
    except Exception:  # fail closed
        return Doc(refused="internal")


class _Parser:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.truncated = False
        self.open = ""  # "" | para | quote | list
        self.lines: list[str] = []  # para / quote content lines
        self.items: list[list] = []  # list: [lines, runs, child count]; run = [ordered, start, [child lines]]
        self.ordered = False
        self.start = 1
        self.root = 0  # indent of the latest root-level list marker; nesting is measured from it
        self.stable = 0
        self.trigger = 0  # index of the line whose processing emits the next block
        self.complete = 0  # lines[:complete] end in a newline; only those can close a block for good

    def run(self, text: str) -> Doc:
        text = text.replace("\r\n", "\n").replace("\r", "\n")  # before the cap: CRLF and LF cut alike
        if len(text) > MAX_INPUT_CHARS:
            text, self.truncated = text[:MAX_INPUT_CHARS], True
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
            self.complete = len(lines)
        else:
            self.complete = len(lines) - 1  # the last line may still grow ("#" -> "#x", "---" -> "---x")
        i = 0
        while i < len(lines):
            if len(self.blocks) >= MAX_BLOCKS:
                self.truncated = True
                break
            self.trigger = i
            raw = lines[i]
            i += 1
            line = raw.expandtabs(4) if "\t" in raw else raw
            body = line.lstrip(" ")
            if not body.strip():
                self.close()  # a blank line closes every open block, lists included (streaming stability)
                continue
            indent = len(line) - len(body)
            fence = _fence(body)
            if fence:
                self.close()
                char, size, language = fence
                code, closer = [], len(lines)  # unclosed: code to the end, never final
                while i < len(lines):
                    raw = lines[i]
                    i += 1
                    end = raw.strip()
                    if len(end) >= size and end == char * len(end):
                        closer = i - 1
                        break
                    code.append(raw[min(indent, len(raw) - len(raw.lstrip(" "))):])
                self.trigger = closer
                self.emit(Block("code", language=language, text=self.cap("\n".join(code), MAX_BLOCK_CHARS)))
                continue
            lazy = self.open == "para" or self.open == "list"
            if indent <= 3 and not lazy and _is_rule(body):
                self.close()
                self.emit(Block("rule"))
                continue
            level, heading = _heading(body) if indent <= 3 else (0, "")
            if level:
                self.close()
                self.emit(Block("heading", inlines=self.inl(heading), level=level))
                continue
            if indent <= 3 and body[0] == ">":
                if self.open != "quote":
                    self.close()
                    self.open = "quote"
                self.lines.append(_unquote(body))
                continue
            marker = _marker(body)
            if marker:
                self.item(indent, *marker)
                continue
            if self.open == "list":
                last = self.items[-1]
                (last[1][-1][2][-1] if last[1] else last[0]).append(body.strip())
                continue
            if self.open != "para":
                self.close()
                self.open = "para"
            self.lines.append(body.strip())
        self.trigger = len(lines)  # the end of input closes nothing for good
        self.close()
        return Doc(tuple(self.blocks), self.stable, self.truncated)

    def emit(self, block: Block) -> None:
        if len(self.blocks) >= MAX_BLOCKS:
            self.truncated = True
            return
        self.blocks.append(block)
        if self.trigger < self.complete:
            self.stable = len(self.blocks)

    def cap(self, text: str, limit: int) -> str:
        if len(text) > limit:
            self.truncated = True
            return text[:limit]
        return text

    def inl(self, text: str, limit: int = MAX_BLOCK_CHARS) -> tuple[Inline, ...]:
        return _inlines(self.cap(text, limit))

    def item(self, indent: int, ordered: bool, number: int, text: str) -> None:
        last = self.items[-1] if self.open == "list" else None
        if last and indent >= self.root + 2:  # deeper markers all land on level 2
            if last[2] >= MAX_ITEMS:
                self.truncated = True  # drop child overflow; promoting it would invent a root item
                return
            if not last[1] or last[1][-1][0] != ordered:
                last[1].append([ordered, number, []])
            last[1][-1][2].append([text])
            last[2] += 1
            return
        if self.open != "list" or self.ordered != ordered or len(self.items) >= MAX_ITEMS:
            self.close()  # a full list continues as a new list block: nothing is dropped
            self.open, self.ordered, self.start, self.items, self.root = "list", ordered, number, [], indent
        elif indent < self.root + 2:
            self.root = indent  # roots may drift (3 -> 0): children nest relative to the latest root
        self.items.append([[text], [], 0])

    def close(self) -> None:
        kind, lines, self.open, self.lines = self.open, self.lines, "", []
        if kind == "para":
            self.emit(_table(lines, self) or Block("paragraph", inlines=self.inl("\n".join(lines))))
        elif kind == "quote":
            note = ""
            if lines and lines[0].strip().lower() in {f"[!{label}]" for label in NOTE_LABELS}:
                note, lines = lines[0].strip()[2:-1].lower(), lines[1:]
            self.emit(Block("quote", inlines=self.inl("\n".join(lines)), note=note))
        elif kind == "list":
            items = tuple(
                Item(self.inl("\n".join(lines)), tuple(
                    Block("list", ordered=o, start=s, items=tuple(Item(self.inl("\n".join(c))) for c in run))
                    for o, s, run in runs))
                for lines, runs, _ in self.items
            )
            self.emit(Block("list", ordered=self.ordered, start=self.start, items=items))
            self.items = []


_ORDERED = re.compile(r"(\d{1,9})[.)] ")
_PIPE = re.compile(r"(?<!\\)\|")
_SPECIAL = re.compile(r"[\\`*_\[!]")
_ALIGN = {(True, True): "center", (True, False): "left", (False, True): "right", (False, False): ""}
# Default_Ignorable_Code_Point outside Cf (Cf is stripped by category; the Cn ones make _href refuse the link)
_IGNORABLE = re.compile("[͏ᅟᅠ឴឵᠋-᠍᠏ㅤ︀-️ﾠ\U000e0100-\U000e01ef]")


def _fence(body: str) -> tuple[str, int, str] | None:
    char = body[0]
    if char not in "`~":
        return None
    size = len(body) - len(body.lstrip(char))
    info = body[size:].strip()
    if size < 3 or (char == "`" and "`" in info):
        return None
    word = info.split()[0] if info else ""
    language = word if len(word) <= MAX_LANG_CHARS and set(word) <= _LANG_CHARS else ""
    return char, size, language


def _is_rule(body: str) -> bool:
    s = body.replace(" ", "")
    return len(s) >= 3 and s[0] in "-*_" and s == s[0] * len(s)


def _heading(body: str) -> tuple[int, str]:
    n = len(body) - len(body.lstrip("#"))
    if not 1 <= n <= 6 or (len(body) > n and body[n] != " "):
        return 0, ""  # 7+ hashes or "#x": paragraph text
    text = body[n:].strip()
    bare = text.rstrip("#")
    if not bare or bare.endswith(" "):
        text = bare.strip()  # closing "#" run
    return min(n, 3), text


def _marker(body: str) -> tuple[bool, int, str] | None:
    if body[0] in "-*+" and body[1:2] == " ":
        return False, 1, body[2:].strip()
    m = _ORDERED.match(body)
    return (True, int(m.group(1)), body[m.end():].strip()) if m else None


def _unquote(body: str) -> str:
    return body[2:] if body[1:2] == " " else body[1:]


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _PIPE.split(s)]


def _table(lines: list[str], p: _Parser) -> Block | None:
    """Header + delimiter row + same-width rows -> table; anything else -> None (paragraph)."""
    if len(lines) < 2 or "|" not in lines[0] or "|" not in lines[1]:
        return None
    header, delim = _cells(lines[0]), _cells(lines[1])
    if len(delim) != len(header) or not all(_DELIM_CELL.fullmatch(c) for c in delim):
        return None
    body = [_cells(line) for line in lines[2:]]
    if any(len(row) != len(header) for row in body):
        return None  # ragged
    cols = min(len(header), MAX_TABLE_COLS)
    if cols < len(header) or len(body) > MAX_TABLE_ROWS:
        p.truncated = True
    return Block(
        "table",
        header=tuple(p.inl(c, MAX_CELL_CHARS) for c in header[:cols]),
        rows=tuple(tuple(p.inl(c, MAX_CELL_CHARS) for c in row[:cols]) for row in body[:MAX_TABLE_ROWS]),
        align=tuple(_ALIGN[c.startswith(":"), c.endswith(":")] for c in delim[:cols]),
    )


def _href(raw: str) -> str:
    """An http(s) link target with invisible (Cf + default-ignorable) characters removed, or "" (stays text)."""
    if len(raw) > 4 * MAX_HREF_CHARS:
        return ""
    url = _IGNORABLE.sub("", "".join(ch for ch in raw.strip() if unicodedata.category(ch) != "Cf"))
    if not url.startswith(("http://", "https://")) or len(url) > MAX_HREF_CHARS:
        return ""
    if any(ch == "\\" or ch.isspace() or unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn") for ch in url):
        return ""
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return ""
    return url if netloc and "@" not in netloc else ""


class _Cursor:
    """First position >= pos; queries never go backwards (the scan index only grows), so the total walk is linear."""

    def __init__(self, positions: list[int]) -> None:
        self.positions, self.k = positions, 0

    def after(self, pos: int) -> int:
        while self.k < len(self.positions) and self.positions[self.k] < pos:
            self.k += 1
        return self.positions[self.k] if self.k < len(self.positions) else -1


def _push(out: list[Inline], buf: list[str], inline: Inline | None = None) -> None:
    text = "".join(buf)
    buf.clear()
    if text:
        out.append(Inline("text", text))
    if inline is not None:
        out.append(inline)


def _inlines(s: str) -> tuple[Inline, ...]:
    """One left-to-right scan; every closer lookup is a find or a forward-only cursor, so time stays linear."""
    # shortcut: bold/italic/link text is flat (no nested inlines), upgrade if the reading view needs nesting
    out: list[Inline] = []
    buf: list[str] = []
    n = len(s)
    runs: dict[int, list[int]] = {}  # backtick run length -> run starts
    for m in re.finditer(r"`+", s):
        runs.setdefault(m.end() - m.start(), []).append(m.start())
    ticks = {k: _Cursor(v) for k, v in runs.items()}
    closes = _Cursor([m.start() for m in re.finditer(r"\]", s)])
    opens = _Cursor([m.start() for m in re.finditer(r"\[", s)])
    parens = _Cursor([m.start() for m in re.finditer(r"\)", s)])
    failed: set[str] = set()  # delimiters with no occurrence left: never search again
    i = 0
    while i < n:
        m = _SPECIAL.search(s, i)
        if not m:
            buf.append(s[i:])
            break
        buf.append(s[i:m.start()])
        i = m.start()
        c = s[i]
        if c == "\\":
            escaped = i + 1 < n and s[i + 1] in _PUNCT
            buf.append(s[i + 1] if escaped else c)
            i += 2 if escaped else 1
            continue
        if c in "`*_":
            e = i
            while e < n and s[e] == c:
                e += 1
            if c == "`":
                k = e - i
                q = ticks[k].after(e) if k in ticks else -1
                if q < 0:
                    buf.append(s[i:e])
                    i = e
                    continue
                code = s[e:q]
                if len(code) > 1 and code[0] == code[-1] == " " and code.strip(" "):
                    code = code[1:-1]
                _push(out, buf, Inline("code", code))
                i = q + k
                continue
            k = min(e - i, 2)
            d = c * k
            q = -1
            if e < n and not s[e].isspace() and (c == "*" or i == 0 or not s[i - 1].isalnum()) and d not in failed:
                q = s.find(d, e)
                if q < 0:
                    failed.add(d)
            if q > e and not s[q - 1].isspace() and (c == "*" or q + k >= n or not s[q + k].isalnum()):
                buf.append(s[i:e - k])
                _push(out, buf, Inline("bold" if k == 2 else "italic", s[e:q]))
                i = q + k
            else:
                buf.append(s[i:e])
                i = e
            continue
        image = c == "!"
        if image and s[i + 1:i + 2] != "[":
            buf.append(c)
            i += 1
            continue
        start = i + 2 if image else i + 1
        r = closes.after(start)
        p = parens.after(r + 2) if r >= 0 and s[r + 1:r + 2] == "(" else -1
        o = opens.after(start)
        if p < 0 or 0 <= o < r:
            buf.append(s[i:start])
            i = start
            continue
        href, label = _href(s[r + 2:p]), s[start:r]
        if href:
            _push(out, buf, Inline("image" if image else "link", label or href, href))
        else:
            buf.append(s[i:p + 1])  # rejected target: the whole construct stays visible text
        i = p + 1
    _push(out, buf)
    return tuple(out)
