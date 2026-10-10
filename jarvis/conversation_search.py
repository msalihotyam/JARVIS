"""Gate 6G F8: the sidebar's conversation search - pure, read-only.

A search is an on-demand scan of the stored conversations through the store's
existing read paths (``list_projects``, ``list_conversations``,
``load_conversation_state``): no index, no table, no writer, nothing changed.
What it can find is what the store keeps (S6): the last ``RAW_TURNS_MAX`` turns
in full and the older ones as their digest line (a digest hit says so); turns
counted as omitted are gone and cannot be found.

The scope is the sidebar's (V9): every owner's non-archived conversation, a
project's only while the project is active; a deleted one is never listed by
the store. The scope is context, never authority - nothing here opens,
selects or grants anything.

Matching is case-insensitive and Turkish-aware (S4): ``İ`` is the capital of
``i`` and ``I`` the capital of ``ı``, so neither ``str.lower`` (``"İ"`` becomes
``"i̇"``) nor ``casefold`` (``"I"`` becomes ``"i"``) is enough. A capital ``I``
in the text also matches a typed ``i``: in all-caps or English text
(``ISTANBUL``, ``API``) it cannot be told from a dotless one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

MIN_CHARS = 2
RESULT_CAP = 50
SNIPPET_CHARS = 80

# The letters whose case is Turkish: a typed letter (folded) -> what it matches in folded text.
_LETTERS = {"i": "[iI]", "ı": "[ıI]", "I": "[iıI]"}


def normalized(text: str) -> str:
    """NFC with every whitespace run one space: the form that is matched and shown."""
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def fold(text: str) -> str:
    """Lower case the Turkish way, one character for one (so a match's place is the text's).
    ``I`` stays ``I``: it stands for either ``i`` or ``ı``."""
    out = []
    for ch in text:
        if ch in "İi":
            out.append("i")
        elif ch == "I":
            out.append("I")
        else:
            low = ch.lower()
            out.append(low if len(low) == 1 else ch)
    return "".join(out)


def pattern(query: str) -> re.Pattern[str] | None:
    """The query as a pattern over folded text; ``None`` below ``MIN_CHARS``."""
    query = normalized(query)
    if len(query) < MIN_CHARS:
        return None
    return re.compile("".join(_LETTERS.get(ch) or re.escape(ch) for ch in fold(query)))


def find(compiled: re.Pattern[str], text: str) -> tuple[int, int] | None:
    """Where the query first occurs in ``text`` (already ``normalized``)."""
    hit = compiled.search(fold(text))
    return None if hit is None else hit.span()


def snippet(text: str, span: tuple[int, int]) -> dict[str, str]:
    """About ``SNIPPET_CHARS`` characters around the hit, in three plain pieces
    (the window draws the hit highlighted); a cut end is marked with "…"."""
    start, end = span
    end = min(end, start + SNIPPET_CHARS)
    room = max(0, SNIPPET_CHARS - (end - start))
    left = max(0, start - room // 3)
    right = min(len(text), end + room - (start - left))
    left = max(0, min(left, right - SNIPPET_CHARS))
    return {"before": ("…" if left > 0 else "") + text[left:start].lstrip(),
            "hit": text[start:end],
            "after": text[end:right].rstrip() + ("…" if right < len(text) else "")}


def _shown(turn: Any) -> str:
    """A turn's words as the conversation shows them (bridge._restore_feed)."""
    if turn.kind == "user":
        return turn.message or turn.text
    text = turn.text
    if turn.kind == "result" and text.startswith("[Task result") and "\n" in text:
        return text.split("\n", 1)[1]
    return text


def _content_hit(compiled: re.Pattern[str], state: Any, redact: Callable[[str], str]) -> dict[str, Any] | None:
    """The newest full-text hit, else the newest digest hit (``digest`` True). A hit
    counts only if it is still there once redacted - a secret is never found or shown."""
    sources = [(_shown(t), False) for t in reversed(state.turns)] + [(c.digest, True) for c in reversed(state.compacted)]
    for raw, digest in sources:
        if find(compiled, normalized(raw)) is None:
            continue
        text = normalized(redact(raw))
        span = find(compiled, text)
        if span is not None:
            return {**snippet(text, span), "digest": digest}
    return None


def search(store: Any, query: str, *, redact: Callable[[str], str] = str) -> dict[str, Any]:
    """BAŞLIKLAR (title matches) then İÇERİKLER (content matches of the rest), newest
    first, at most ``RESULT_CAP`` together; ``more`` when at least one more matched."""
    compiled = pattern(query)
    out: dict[str, Any] = {"titles": [], "contents": [], "more": False}
    if compiled is None or store is None:
        return out
    projects = {p.id: p.name for p in store.list_projects() if p.active}
    records = [r for r in store.list_conversations()
               if r.archived_at is None and (r.project_id is None or r.project_id in projects)]

    def row(record: Any) -> dict[str, Any]:
        return {"id": record.id, "title": record.title, "legacy": record.origin == "legacy-import",
                "project_id": record.project_id or "", "project_name": projects.get(record.project_id, ""),
                "updated_at": record.updated_at}

    titled = set()
    for record in records:
        if record.title and find(compiled, normalized(record.title)) is not None:
            if len(out["titles"]) == RESULT_CAP:
                out["more"] = True
                return out
            out["titles"].append(row(record))
            titled.add(record.id)
    for record in records:
        if record.id in titled:
            continue
        try:
            state = store.load_conversation_state(record.id)
        except Exception:  # noqa: BLE001 - deleted meanwhile or unverifiable: never shown, never repaired
            continue
        hit = _content_hit(compiled, state, redact)
        if hit is None:
            continue
        if len(out["titles"]) + len(out["contents"]) == RESULT_CAP:
            out["more"] = True
            return out
        out["contents"].append({**row(record), **hit})
    return out
