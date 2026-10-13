"""Import plan for V2.1b: dedup + proposed project scope (pure, no side effects).

``plan_import`` takes a parsed ``ImportBatch`` (untrusted data), the content
hashes already stored and the projects the user may choose from, and returns
an ``ImportPlan``: items grouped by source conversation, duplicates dropped by
content hash, a PROPOSED project per group and an extraction size estimate.

The plan has no authority. A proposal is never a scope: every group carries
``scope_status == SCOPE_UNCONFIRMED`` and nothing in this module can produce a
confirmed scope; the user confirms each group in a later step (Core decides).
Titles and texts are never interpreted; a title only ever selects among the
caller's own project choices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from jarvis.contracts import PROJECT_NAME_CAP, validate_project_id
from jarvis.memory_import import (
    MAX_ITEMS,
    ImportBatch,
    ImportedItem,
    ImportTooLargeError,
    content_hash,
)

#: The only scope status a plan ever carries.
SCOPE_UNCONFIRMED = "unconfirmed"
MAX_PROJECT_CHOICES = 1_000
MAX_EXISTING_HASHES = 1_000_000
#: Rough chars-per-token ratio for the extraction size estimate.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class ImportGroup:
    source_format: str
    source_conversation_title: str
    items: tuple[ImportedItem, ...]
    proposed_project_id: str | None
    scope_status: str
    estimated_chars: int
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ImportPlan:
    groups: tuple[ImportGroup, ...]
    duplicates_dropped: int
    estimated_chars: int
    estimated_tokens: int


def plan_import(
    batch: ImportBatch,
    existing_hashes: Iterable[str],
    project_choices: Mapping[str, str],
) -> ImportPlan:
    """Group ``batch`` by (format, conversation title) in first-seen order,
    drop items whose content hash is already stored or seen earlier in the
    batch, and propose (never assign) one project per group.

    ``project_choices`` maps project id -> display name. A group's proposal is
    the single project whose name occurs in its title, else the only choice
    when there is exactly one, else ``None``."""
    if not isinstance(batch, ImportBatch):
        raise TypeError("plan_import takes an ImportBatch")
    if len(batch.items) > MAX_ITEMS:
        raise ImportTooLargeError(f"batch has more than {MAX_ITEMS} items")
    choices = _choices(project_choices)
    seen: set[str] = set()
    for count, value in enumerate(existing_hashes):
        if count >= MAX_EXISTING_HASHES:
            raise ImportTooLargeError(f"more than {MAX_EXISTING_HASHES} existing hashes")
        if not isinstance(value, str):
            raise TypeError("existing hashes must be strings")
        seen.add(value)

    grouped: dict[tuple[str, str], list[ImportedItem]] = {}
    dropped = 0
    for item in batch.items:
        # Recomputed, not trusted: a forged hash on the batch cannot dodge dedup.
        digest = content_hash(item.text)
        if digest in seen:
            dropped += 1
            continue
        seen.add(digest)
        key = (item.source_format, item.source_conversation_title)
        grouped.setdefault(key, []).append(item)

    groups = []
    for (fmt, title), items in grouped.items():
        chars = sum(len(item.text) for item in items)
        groups.append(ImportGroup(
            fmt, title, tuple(items), _propose(title, choices), SCOPE_UNCONFIRMED,
            chars, _tokens(chars),
        ))
    total = sum(group.estimated_chars for group in groups)
    return ImportPlan(tuple(groups), dropped, total, _tokens(total))


def _choices(project_choices: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if not isinstance(project_choices, Mapping):
        raise TypeError("project_choices must map project id -> name")
    if len(project_choices) > MAX_PROJECT_CHOICES:
        raise ValueError(f"more than {MAX_PROJECT_CHOICES} project choices")
    result = []
    for project_id, name in project_choices.items():
        validate_project_id(project_id)
        if not isinstance(name, str) or len(name) > PROJECT_NAME_CAP:
            raise ValueError("project name must be a string of at most "
                             f"{PROJECT_NAME_CAP} characters")
        result.append((project_id, name.strip().casefold()))
    return tuple(sorted(result))


def _propose(title: str, choices: tuple[tuple[str, str], ...]) -> str | None:
    folded = title.casefold()
    matches = [pid for pid, name in choices if name and name in folded]
    if len(matches) == 1:
        return matches[0]
    if not matches and len(choices) == 1:
        return choices[0][0]
    return None


def _tokens(chars: int) -> int:
    return -(-chars // CHARS_PER_TOKEN)
