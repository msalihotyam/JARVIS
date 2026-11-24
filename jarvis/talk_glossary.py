"""Local speech hints and deterministic Turkish transcript correction."""

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "dev" / "talk" / "glossary_tr.json"
# Every combining mark (Mn/Mc/Me), format joiner (Cf, e.g. ZWNJ), connector (Pc, e.g. U+203F)
# and emoji skin-tone modifier (Word_Break=Extend) stays inside a word,
# so an alias never matches the start of a longer word. Python re has no \p{M}; scan once.
_JOINER_CHARS = frozenset(
    chr(code)
    for code in range(sys.maxunicode + 1)
    if unicodedata.category(chr(code)) in {"Mn", "Mc", "Me", "Cf", "Pc"} or 0x1F3FB <= code <= 0x1F3FF
)
_JOINERS = "".join(map(re.escape, sorted(_JOINER_CHARS)))
_WORD = rf"\w[\w{_JOINERS}]*"
_TOKEN = re.compile(rf"{_WORD}|\s+|[^\w\s]")
_TURKISH_I = str.maketrans({"I": "ı", "İ": "i"})


def _fold(token: str) -> str:
    if token.isspace():
        return " "
    if not re.fullmatch(_WORD, token):
        # A non-word token is never casefolded (U+0345 folds to a letter) and never equals a word key.
        return "\0" + token
    folded = token.translate(_TURKISH_I).casefold()
    return unicodedata.normalize("NFC", folded)


def _nfc_with_offsets(text: str) -> tuple[str, dict[int, int]]:
    """NFC the text in independently normalizable chunks; map each NFC chunk start to its original offset."""
    bounds = [0]
    for index in range(1, len(text)):
        char, chunk = text[index], text[bounds[-1]:index]
        if unicodedata.combining(unicodedata.normalize("NFD", char)[0]) == 0 and unicodedata.normalize(
            "NFC", chunk + char
        ) == unicodedata.normalize("NFC", chunk) + unicodedata.normalize("NFC", char):
            bounds.append(index)
    parts, offsets, position = [], {}, 0
    for left, right in zip(bounds, bounds[1:] + [len(text)]):
        offsets[position] = left
        parts.append(unicodedata.normalize("NFC", text[left:right]))
        position += len(parts[-1])
    offsets[position] = len(text)
    return "".join(parts), offsets


def _term_key(term: str) -> tuple[str, ...]:
    tokens = _TOKEN.findall(unicodedata.normalize("NFC", term))
    if not tokens or not all(re.fullmatch(_WORD, token) for token in (tokens[0], tokens[-1])):
        raise ValueError(f"glossary term must start and end with a word: {term!r}")
    return tuple(map(_fold, tokens))


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"duplicate glossary field: {key}")
        fields[key] = value
    return fields


@dataclass(frozen=True)
class GlossaryRow:
    canonical: str
    aliases: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True)
class Replacement:
    original: str
    canonical: str

    def candidate_row(self, note: str | None = None) -> GlossaryRow:
        """Return an in-memory row for review."""
        return GlossaryRow(self.canonical, (self.original,), note)


class Glossary:
    def __init__(self, rows: tuple[GlossaryRow, ...]) -> None:
        canonicals = {}
        for row in rows:
            key = _term_key(row.canonical)
            if key in canonicals:
                raise ValueError(f"duplicate canonical term: {row.canonical!r}")
            canonicals[key] = row.canonical

        aliases = {}
        for row in rows:
            for alias in row.aliases:
                key = _term_key(alias)
                previous = canonicals.get(key, aliases.get(key))
                if previous is not None and previous != row.canonical:
                    raise ValueError(f"ambiguous glossary alias: {alias!r}")
                # Canonical spans are protected, so such an alias could never fire.
                if any(key[start:end] in canonicals for start in range(len(key)) for end in range(start + 1, len(key) + 1)):
                    raise ValueError(f"glossary alias contains a canonical term: {alias!r}")
                aliases[key] = row.canonical

        self._rows = rows
        self._canonicals = canonicals
        self._aliases = aliases
        self._sizes = sorted({len(key) for key in canonicals.keys() | aliases.keys()}, reverse=True)

    @property
    def rows(self) -> tuple[GlossaryRow, ...]:
        return self._rows

    @classmethod
    def load(cls, path: str | Path = DEFAULT_GLOSSARY_PATH) -> "Glossary":
        """Read and validate the entire JSON glossary before returning it."""
        document = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_fields)
        if not isinstance(document, list):
            raise ValueError("glossary must be an array")
        rows = []
        for index, row in enumerate(document, start=1):
            if not isinstance(row, dict) or row.keys() - {"canonical", "aliases", "note"}:
                raise ValueError(f"invalid glossary row {index}")
            canonical, aliases, note = row.get("canonical"), row.get("aliases"), row.get("note")
            if (
                not isinstance(canonical, str)
                or not isinstance(aliases, list)
                or not all(isinstance(alias, str) for alias in aliases)
                or ("note" in row and not isinstance(note, str))
            ):
                raise ValueError(f"invalid glossary row {index}")
            rows.append(GlossaryRow(canonical, tuple(aliases), note))
        return cls(tuple(rows))

    def build_hint(self, max_chars: int = 256) -> str:
        """Join a prefix of the canonical terms within the character budget."""
        terms = []
        remaining = max_chars
        for row in self.rows:
            cost = len(row.canonical) + (2 if terms else 0)
            if cost > remaining:
                break
            terms.append(row.canonical)
            remaining -= cost
        return ", ".join(terms)

    def correct(self, transcript: str) -> tuple[str, list[Replacement]]:
        """Apply leftmost, longest aliases and report edits in transcript order."""
        normalized, offsets = _nfc_with_offsets(transcript)
        tokens = list(_TOKEN.finditer(normalized))
        keys = tuple(_fold(token.group()) for token in tokens)
        protected_until = 0
        protected = []
        # Protect every canonical span, including overlaps. A replacement's
        # own canonical span will then prevent new aliases from crossing it.
        for start in range(len(tokens)):
            for size in self._sizes:
                end = start + size
                if end <= len(tokens) and keys[start:end] in self._canonicals:
                    protected_until = max(protected_until, end)
                    break
            protected.append(protected_until > start)

        pieces = []
        changes = []
        cursor = start = 0
        while start < len(tokens):
            # An alias right after an orphan mark or joiner (no word base) is joined to it: no edit.
            joined = start > 0 and tokens[start - 1].group()[-1] in _JOINER_CHARS
            for size in [] if joined else self._sizes:
                end = start + size
                canonical = self._aliases.get(keys[start:end]) if end <= len(tokens) else None
                left = offsets.get(tokens[start].start())
                right = offsets.get(tokens[end - 1].end()) if end <= len(tokens) else None
                # A span edge inside one NFC chunk has no original offset: fail closed, no edit.
                if canonical is not None and left is not None and right is not None and not any(protected[start:end]):
                    pieces.extend((transcript[cursor:left], canonical))
                    changes.append(Replacement(transcript[left:right], canonical))
                    cursor, start = right, end
                    break
            else:
                start += 1
        pieces.append(transcript[cursor:])
        return "".join(pieces), changes
