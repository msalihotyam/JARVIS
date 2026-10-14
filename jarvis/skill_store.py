"""V2.2 S2: the skill store over the compatibility-layer skill* tables.

A skill is a procedure, never an authority: nothing stored or returned here
grants, implies or remembers an approval, and no row has an approval, trust
or risk field (skill_types refuses such keys). Every table is insert- or
append-only (no UPDATE, no DELETE): a skill's state is its events in
event_id order, a draft's decision is the one event carrying its draft_id,
a deletion is a final ``deleted`` tombstone event (PLAN D4).

Every read dispatches on the row's ``body_format`` and then goes through
skill_types/skill_lifecycle ``from_dict``. A row that does not parse, has an
unknown (e.g. newer) body format or disagrees with its own columns is skipped
and counted in ``broken_rows`` - never used, matched, offered or shown, never
a crash - and listed by ``unreadable_rows``; a skill with any such row, or a
history skill_lifecycle refuses, is left out whole (a dropped event could be
its tombstone). Every write re-runs skill_filter (S7) and the D15 scope-move
rule.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
import unicodedata
from dataclasses import replace
from typing import Any, Callable, NamedTuple

from jarvis.contracts import utc_now
from jarvis.experience import visible_in
from jarvis.memory import SQLiteMemoryStore
from jarvis.skill_draft import GLOBAL_SUPPRESSION, TASK_KIND_PREFIX, parse_user_edited_draft
from jarvis.skill_filter import MAX_FIELDS, check_fields
from jarvis.skill_lifecycle import (
    SkillEvent,
    SkillHistory,
    _check_scope_move,
    _active,
    active_skill,
    approved_versions,
    health,
    status,
)
from jarvis.skill_match import SkillWithState
from jarvis.skill_types import (
    Experience,
    Skill,
    SkillDraft,
    SkillEventKind,
    SkillRecordError,
    SkillScope,
    SkillStatus,
    fold_token,
)

_log = logging.getLogger(__name__)

#: Layout version of every new ``body``: an envelope {"body_format", "unicode",
#: "python", "record"} around the record's to_dict, naming the Unicode database
#: and the Python major.minor it was normalized and written with. Format 1 (the
#: bare to_dict, written before the envelope) still reads; any other format is
#: unreadable - a newer one "needs a newer JARVIS" - until a build knows it.
BODY_FORMAT = 2
_BARE_FORMAT = 1
_ENVELOPE_KEYS = frozenset({"body_format", "unicode", "python", "record"})
NEEDS_NEWER = "needs a newer JARVIS"

_ACTORS = frozenset({"user", "core"})
_SUPPRESSION_KEY = re.compile(re.escape(TASK_KIND_PREFIX) + "[0-9a-f]{32}")
#: Draft decisions are written only by approve_draft / reject_draft.
_DECISION_KINDS = frozenset({SkillEventKind.DRAFT_APPROVED, SkillEventKind.DRAFT_REJECTED})
#: Ids, timestamps and enum values: shape-checked by skill_types, not free text.
_NOT_TEXT = frozenset(
    {
        "skill_id", "draft_id", "experience_id", "source_run_id", "parent_skill_id",
        "target_skill_id", "project_id", "created_at", "kind", "origin",
    }
)

_Columns = tuple[tuple[str, Callable[[Any], Any]], ...]
_SCOPE: _Columns = (
    ("scope_kind", lambda r: r.scope.kind.value),
    ("project_id", lambda r: r.scope.project_id),
)
_SKILL: _Columns = (("skill_id", lambda r: r.skill_id), ("version", lambda r: r.version)) + _SCOPE
_EVENT: _Columns = (("skill_id", lambda r: r.skill_id), ("kind", lambda r: r.kind.value))
_DRAFT: _Columns = (("draft_id", lambda r: r.draft_id),) + _SCOPE
_EXPERIENCE: _Columns = (
    ("experience_id", lambda r: r.experience_id),
    ("skill_id", lambda r: r.skill_id),
) + _SCOPE


#: b1 lesson control (SKLESSONS): two more append-only tables, created on
#: every SkillStore open (no ALTER; SCHEMA_VERSION unchanged). A lesson's
#: state is its latest skill_experience_events row, ``deleted`` final (a
#: tombstone); a use row holds ids only, never lesson text.
LESSON_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_experience_events (
    event_id INTEGER PRIMARY KEY,
    experience_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('enabled', 'disabled', 'deleted')),
    actor TEXT NOT NULL CHECK (actor IN ('user', 'core')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_experience_events_experience
    ON skill_experience_events(experience_id, event_id);
CREATE TABLE IF NOT EXISTS skill_experience_uses (
    use_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    experience_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_experience_uses_run
    ON skill_experience_uses(run_id, use_id)
"""
LESSON_SCHEMA_OBJECTS = frozenset(
    {
        "skill_experience_events",
        "idx_skill_experience_events_experience",
        "skill_experience_uses",
        "idx_skill_experience_uses_run",
    }
)
LESSON_ACTIVE = "active"
LESSON_DISABLED = "disabled"
_LESSON_KINDS = frozenset({"enabled", LESSON_DISABLED, "deleted"})


class SkillStoreError(SkillRecordError):
    """A refused store write (callers catch ``SkillRecordError``)."""


class UnreadableRow(NamedTuple):
    """A stored row no read uses: its table, its key and why (never its text)."""

    table: str
    key: str
    reason: str


def _renormalized(data: Any) -> Any:
    """An Experience body written under another Unicode database, its
    task_signature re-folded with this one so it matches the words this build
    folds - in memory only; the stored bytes stay as written (append-only)."""
    if type(data) is dict and type(data.get("task_signature")) is list:
        folded = dict.fromkeys(fold_token(token) for token in data["task_signature"])
        return {**data, "task_signature": list(folded)}
    return data


def _text_fields(data: Any, name: str = "") -> list[tuple[str, str]]:
    """Every free-text value of a to_dict tree, named in skill_filter's grammar."""
    if isinstance(data, dict):
        return [
            pair
            for key, value in data.items()
            if key not in _NOT_TEXT
            for pair in _text_fields(value, f"{name}-{key}" if name else key)
        ]
    if isinstance(data, list):
        return [
            pair
            for number, value in enumerate(data, start=1)
            for pair in _text_fields(value, f"{name}-{number}")
        ]
    return [(name, data)] if isinstance(data, str) else []


def _filter(record: Skill | SkillDraft | Experience) -> None:
    """S7, on every write. The refusal names the field and category, never the text."""
    fields = _text_fields(record.to_dict())
    for start in range(0, len(fields), MAX_FIELDS):
        verdict = check_fields(fields[start:start + MAX_FIELDS])
        if not verdict.allowed:
            raise SkillStoreError(
                f"refused by the skill filter: {verdict.category} in {verdict.field}"
            )


def _skill_id_for(draft_id: str) -> str:
    """The new skill an approved new-skill draft becomes (deterministic)."""
    return "sk-" + hashlib.sha256(draft_id.encode("utf-8")).hexdigest()[:32]


def _check_actor(actor: object) -> None:
    if actor not in _ACTORS:
        raise SkillStoreError("actor is 'user' or 'core'")


class SkillStore(SQLiteMemoryStore):
    """Skills, their events, drafts, suppressions and experiences."""

    def __init__(self, database_path: Any) -> None:
        self.broken_rows = 0
        super().__init__(database_path)
        with self._connection(write=True) as connection:
            for statement in LESSON_SCHEMA_SQL.split(";"):
                connection.execute(statement)

    def _body(self, record: Any) -> str:
        return json.dumps(
            {
                "body_format": BODY_FORMAT,
                "unicode": unicodedata.unidata_version,
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "record": record.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _parse_row(self, row: sqlite3.Row, parse: Callable[[Any], Any], columns: _Columns) -> Any:
        """The strictly parsed record; raises for any row no read may use."""
        body_format = row["body_format"]
        if type(body_format) is not int or body_format not in (_BARE_FORMAT, BODY_FORMAT):
            raise SkillRecordError(
                NEEDS_NEWER if type(body_format) is int and body_format > BODY_FORMAT
                else "unknown body format"
            )
        data = json.loads(row["body"])
        unicode = unicodedata.unidata_version  # format 1 parses strictly, as before
        if body_format == BODY_FORMAT:
            if (
                type(data) is not dict
                or data.keys() != _ENVELOPE_KEYS
                or type(data["body_format"]) is not int
                or data["body_format"] != BODY_FORMAT
                or type(data["unicode"]) is not str
                or type(data["python"]) is not str
            ):
                raise SkillRecordError("the body envelope is malformed")
            unicode, data = data["unicode"], data["record"]
        if parse == Experience.from_dict and unicode != unicodedata.unidata_version:
            data = _renormalized(data)
        record = parse(data)
        if any(row[column] != value(record) for column, value in columns):
            raise SkillRecordError("the row's columns disagree with its body")
        return record

    def _read(self, row: sqlite3.Row, parse: Callable[[Any], Any], columns: _Columns) -> Any:
        """The parsed record, or None (counted) for a broken row."""
        try:
            return self._parse_row(row, parse, columns)
        except (ValueError, TypeError, RecursionError):
            self.broken_rows += 1
            _log.warning("skill store: skipped a broken row")
            return None

    def unreadable_rows(self) -> tuple[UnreadableRow, ...]:
        """Every stored row no read uses (a newer format, a corrupted body,
        columns that disagree), listed instead of silently hidden."""
        tables = (
            ("skills", "SELECT skill_id || ':' || version AS row_key, * FROM skills",
             Skill.from_dict, _SKILL),
            ("skill_events", "SELECT event_id AS row_key, * FROM skill_events",
             SkillEvent.from_dict, _EVENT),
            ("skill_drafts", "SELECT draft_id AS row_key, * FROM skill_drafts",
             SkillDraft.from_dict, _DRAFT),
            ("skill_experiences", "SELECT experience_id AS row_key, * FROM skill_experiences",
             Experience.from_dict, _EXPERIENCE),
        )
        found = []
        with self._connection() as connection:
            for table, query, parse, columns in tables:
                for row in connection.execute(query):
                    try:
                        self._parse_row(row, parse, columns)
                    except (ValueError, TypeError, RecursionError) as error:
                        reason = NEEDS_NEWER if str(error) == NEEDS_NEWER else "unreadable"
                        found.append(UnreadableRow(table, str(row["row_key"]), reason))
        return tuple(found)

    # -- skills and events ---------------------------------------------------

    def _histories(
        self, connection: sqlite3.Connection, skill_id: str | None = None
    ) -> dict[str, SkillHistory]:
        """Every intact skill history (only ``skill_id``'s when given).

        A skill with any broken row, or a history skill_lifecycle refuses
        (e.g. a D15 move), is left out whole: fail closed.
        """
        # ponytail: full scan per read; index by scope when a user has > 1k skills.
        if not connection.in_transaction:
            connection.execute("BEGIN")  # one snapshot: versions and events agree
        versions: dict[str, list[Skill]] = {}
        events: dict[str, list[SkillEvent]] = {}
        broken: set[str] = set()
        for row in connection.execute(
            "SELECT * FROM skills WHERE (? IS NULL OR skill_id = ?) ORDER BY skill_id, version",
            (skill_id, skill_id),
        ):
            skill = self._read(row, Skill.from_dict, _SKILL)
            if skill is None:
                broken.add(row["skill_id"])
            else:
                versions.setdefault(skill.skill_id, []).append(skill)
        for row in connection.execute(
            "SELECT * FROM skill_events WHERE (? IS NULL OR skill_id = ?) ORDER BY event_id",
            (skill_id, skill_id),
        ):
            event = self._read(row, SkillEvent.from_dict, _EVENT)
            if event is None:
                broken.add(row["skill_id"])
            else:
                events.setdefault(event.skill_id, []).append(event)
        histories = {}
        for key, items in versions.items():
            if key in broken:
                continue
            try:
                histories[key] = SkillHistory(
                    versions=tuple(items), events=tuple(events.get(key, ()))
                )
            except SkillRecordError:
                self.broken_rows += 1
                _log.warning("skill store: skipped a refused skill history")
        return histories

    def _intact_history(self, connection: sqlite3.Connection, skill_id: str) -> SkillHistory | None:
        """The skill's history; None when it has no rows; refused when broken."""
        history = self._histories(connection, skill_id).get(skill_id)
        if history is None and connection.execute(
            "SELECT 1 FROM skills WHERE skill_id = ? LIMIT 1", (skill_id,)
        ).fetchone():
            raise SkillStoreError("the skill's stored history is broken; nothing is written")
        return history

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        event: SkillEvent,
        actor: str,
        draft_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO skill_events (skill_id, kind, body, body_format, draft_id, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.skill_id, event.kind.value, self._body(event), BODY_FORMAT, draft_id, actor,
             utc_now()),
        )

    def _save(
        self, connection: sqlite3.Connection, skill: Skill, draft_id: str | None, actor: str, now: str
    ) -> None:
        """Insert one version and the draft_approved event that makes it active."""
        _filter(skill)
        history = self._intact_history(connection, skill.skill_id)
        if history is None:
            versions, events, expected = (), (), 1
        elif status(history) is SkillStatus.DELETED:
            raise SkillStoreError("a deleted skill is final; it gets no new version (D4)")
        else:
            versions, events, expected = history.versions, history.events, history.latest_version + 1
        if skill.version != expected:
            raise SkillStoreError(f"the next version of {skill.skill_id} is {expected}")
        parent_id = skill.provenance.parent_skill_id
        if parent_id is not None and parent_id != skill.skill_id:
            parent = self._intact_history(connection, parent_id)
            if parent is None:
                raise SkillStoreError("the parent skill is not stored")
            if status(parent) is SkillStatus.DELETED:
                raise SkillStoreError("a deleted skill is final; nothing is adapted from it (D4)")
            _check_scope_move(parent.get(skill.provenance.parent_version).scope, skill.scope)
        approved = SkillEvent(
            skill_id=skill.skill_id, kind=SkillEventKind.DRAFT_APPROVED, version=skill.version, at=now
        )
        # The read side's rules (D15 within the skill, known versions) before any insert.
        SkillHistory(versions=versions + (skill,), events=events + (approved,))
        connection.execute(
            "INSERT INTO skills (skill_id, version, body, body_format, scope_kind, project_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (skill.skill_id, skill.version, self._body(skill), BODY_FORMAT,
             skill.scope.kind.value, skill.scope.project_id, utc_now()),
        )
        self._insert_event(connection, approved, actor, draft_id)

    def add_version(self, skill: Skill, *, actor: str = "user", now: str | None = None) -> None:
        """Save a version made by skill_lifecycle (next_version / adapt with
        ``history=``) on the user's explicit save (S5); it becomes active."""
        if actor != "user":
            raise SkillStoreError("only the user saves a skill version (S5)")
        if type(skill) is not Skill:
            raise SkillStoreError("add_version needs a Skill")
        with self._connection(write=True) as connection:
            self._save(connection, skill, None, actor, now or utc_now())

    def append_event(self, event: SkillEvent, *, actor: str = "core") -> None:
        """Append a use / enable / disable / delete / stale / rollback event.

        A deleted skill is final: nothing is appended after its tombstone.
        """
        _check_actor(actor)
        if type(event) is not SkillEvent:
            raise SkillStoreError("append_event needs a SkillEvent")
        if event.kind in _DECISION_KINDS:
            raise SkillStoreError("draft decisions are written by approve_draft / reject_draft")
        with self._connection(write=True) as connection:
            history = self._intact_history(connection, event.skill_id)
            if history is None:
                raise SkillStoreError("no such skill")
            if status(history) is SkillStatus.DELETED:
                raise SkillStoreError("a deleted skill is final (D4)")
            if (
                event.kind is SkillEventKind.ROLLED_BACK
                and event.version not in approved_versions(history)
            ):
                raise SkillStoreError("a rollback target needs its own draft_approved event")
            SkillHistory(versions=history.versions, events=history.events + (event,))
            self._insert_event(connection, event, actor)

    def list_skills(self, project_id: str | None) -> tuple[SkillWithState, ...]:
        """Every non-deleted skill whose active version is visible in
        ``project_id`` (global everywhere, a project skill only there), with
        its status and health. Disabled and stale skills are listed with that
        status; skill_match drops / caps them."""
        with self._connection() as connection:
            histories = self._histories(connection)
        listed = []
        for history in histories.values():
            state = status(history)
            if _active(history) is None:
                continue  # no approved active version: nothing to show or run
            skill = active_skill(history)
            if state is not SkillStatus.DELETED and visible_in(skill.scope, project_id):
                listed.append(SkillWithState(skill=skill, status=state, health=health(history)))
        return tuple(listed)

    def history(self, skill_id: str, project_id: str | None) -> SkillHistory | None:
        """The whole history, or None when the skill is unknown, broken,
        deleted, or holds any version not visible in ``project_id``."""
        with self._connection() as connection:
            history = self._histories(connection, skill_id).get(skill_id)
        if (
            history is None
            or status(history) is SkillStatus.DELETED
            or not all(visible_in(item.scope, project_id) for item in history.versions)
        ):
            return None
        return history

    def get_skill(
        self, skill_id: str, project_id: str | None, version: int | None = None
    ) -> Skill | None:
        """One version (the active one by default), or None when unknown,
        deleted or not visible in ``project_id``. Display lookup: running a
        skill goes through skill_lifecycle.usable_skill(history)."""
        with self._connection() as connection:
            history = self._histories(connection, skill_id).get(skill_id)
        if history is None or status(history) is SkillStatus.DELETED:
            return None
        try:
            skill = active_skill(history) if version is None else history.get(version)
        except SkillRecordError:
            return None
        return skill if visible_in(skill.scope, project_id) else None

    # -- drafts ----------------------------------------------------------------

    def add_draft(self, draft: SkillDraft) -> bool:
        """Store an offered draft; idempotent on draft_id (False = already there)."""
        if type(draft) is not SkillDraft:
            raise SkillStoreError("add_draft needs a SkillDraft")
        _filter(draft)
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO skill_drafts "
                "(draft_id, body, body_format, scope_kind, project_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (draft.draft_id, self._body(draft), BODY_FORMAT, draft.scope.kind.value,
                 draft.scope.project_id, utc_now()),
            )
            return cursor.rowcount == 1

    def pending_drafts(self, project_id: str | None) -> tuple[SkillDraft, ...]:
        """Undecided drafts visible in ``project_id``."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_drafts d WHERE NOT EXISTS "
                "(SELECT 1 FROM skill_events e WHERE e.draft_id = d.draft_id) "
                "ORDER BY created_at, draft_id"
            ).fetchall()
        drafts = (self._read(row, SkillDraft.from_dict, _DRAFT) for row in rows)
        return tuple(
            draft for draft in drafts if draft is not None and visible_in(draft.scope, project_id)
        )

    def _pending(
        self, connection: sqlite3.Connection, draft_id: str, project_id: str | None
    ) -> SkillDraft:
        row = connection.execute(
            "SELECT * FROM skill_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise SkillStoreError("no such draft")
        if connection.execute(
            "SELECT 1 FROM skill_events WHERE draft_id = ?", (draft_id,)
        ).fetchone():
            raise SkillStoreError("the draft is not pending; it was already decided")
        draft = self._read(row, SkillDraft.from_dict, _DRAFT)
        if draft is None:
            raise SkillStoreError("the stored draft is broken")
        if not visible_in(draft.scope, project_id):
            raise SkillStoreError("no such draft")  # another project's draft (S8)
        return draft

    def approve_draft(
        self,
        draft_id: str,
        scope: SkillScope | None = None,
        edits: Any = None,
        actor: str = "user",
        *,
        project_id: str | None,
        now: str | None = None,
    ) -> Skill:
        """The user's explicit approval (S5) saves the draft as a skill version.

        ``edits`` is the user's edited draft payload (skill_draft's parser,
        against the stored original); ``scope`` the card's choice - the
        draft's own or, for a project draft, global (D15). Refused unless the
        draft is pending, visible in ``project_id`` (the context the card is
        shown in) and every field passes skill_filter again.
        """
        if actor != "user":
            raise SkillStoreError("only the user approves a draft (S5)")
        now = now or utc_now()
        with self._connection(write=True) as connection:
            draft = self._pending(connection, draft_id, project_id)
            if edits is not None:
                draft = parse_user_edited_draft(edits, original=draft)
            if scope is not None and scope != draft.scope:
                if type(scope) is not SkillScope:
                    raise SkillStoreError("scope must be a SkillScope")
                _check_scope_move(draft.scope, scope)
                draft = replace(draft, scope=scope)
            _filter(draft)
            skill_id = draft.target_skill_id or _skill_id_for(draft.draft_id)
            history = self._intact_history(connection, skill_id)
            skill = Skill(
                skill_id=skill_id,
                version=1 if history is None else history.latest_version + 1,
                name=draft.name,
                summary=draft.summary,
                scope=draft.scope,
                inputs=draft.inputs,
                steps=draft.steps,
                checks=draft.checks,
                provenance=draft.provenance,
            )
            self._save(connection, skill, draft.draft_id, actor, now)
        return skill

    def reject_draft(
        self,
        draft_id: str,
        actor: str = "user",
        *,
        project_id: str | None,
        now: str | None = None,
    ) -> None:
        """Decide a pending draft as rejected; it is never offered again."""
        _check_actor(actor)
        with self._connection(write=True) as connection:
            draft = self._pending(connection, draft_id, project_id)
            target = draft.target_skill_id and self._intact_history(connection, draft.target_skill_id)
            if target and status(target) is SkillStatus.DELETED:
                raise SkillStoreError("a deleted skill is final (D4)")
            event = SkillEvent(
                skill_id=draft.target_skill_id or _skill_id_for(draft.draft_id),
                kind=SkillEventKind.DRAFT_REJECTED,
                version=None,
                at=now or utc_now(),
            )
            self._insert_event(connection, event, actor, draft.draft_id)

    # -- suppressions ----------------------------------------------------------

    def set_suppression(
        self, key: str, suppressed: bool, *, actor: str = "user", now: str | None = None
    ) -> None:
        """Append an on/off row for a task kind (``tk-...``) or ``global``."""
        _check_actor(actor)
        if type(key) is not str or not (
            key == GLOBAL_SUPPRESSION or _SUPPRESSION_KEY.fullmatch(key)
        ):
            raise SkillStoreError("a suppression key is 'global' or a skill_draft task kind")
        if type(suppressed) is not bool:
            raise SkillStoreError("suppressed is a bool")
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO skill_suppressions (suppression_key, suppressed, actor, at) "
                "VALUES (?, ?, ?, ?)",
                (key, int(suppressed), actor, now or utc_now()),
            )

    def is_suppressed(self, key: str) -> bool:
        """The latest row for ``key`` decides; no row = not suppressed."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT suppressed FROM skill_suppressions WHERE suppression_key = ? "
                "ORDER BY suppression_id DESC LIMIT 1",
                (key,),
            ).fetchone()
        return row is not None and row[0] != 0

    def active_suppressions(self, scope: str | None = None) -> tuple[dict[str, str], ...]:
        """Keys whose latest row is on, oldest first; ``scope`` "global" or
        "task" filters (b7)."""
        if scope not in (None, "global", "task"):
            raise SkillStoreError("scope is 'task' or 'global'")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT s.suppression_key, s.actor, s.at FROM skill_suppressions s "
                "WHERE s.suppressed = 1 AND s.suppression_id = (SELECT MAX(suppression_id) "
                "FROM skill_suppressions WHERE suppression_key = s.suppression_key) "
                "ORDER BY s.suppression_id"
            ).fetchall()
        listed = (
            {"key": key, "scope": "global" if key == GLOBAL_SUPPRESSION else "task", "actor": actor, "at": at}
            for key, actor, at in rows
        )
        return tuple(item for item in listed if scope in (None, item["scope"]))

    def undo_suppression(self, key: str, *, actor: str = "user", now: str | None = None) -> bool:
        """b7: append an off row for a key that has ever been suppressed
        (True); already off appends nothing (False). A key with no row is
        refused (fail closed). Never UPDATE/DELETE: latest row wins."""
        _check_actor(actor)
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT suppressed FROM skill_suppressions WHERE suppression_key = ? "
                "ORDER BY suppression_id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is None:
                raise SkillStoreError("no such suppression")
            if row[0] == 0:
                return False
            connection.execute(
                "INSERT INTO skill_suppressions (suppression_key, suppressed, actor, at) "
                "VALUES (?, 0, ?, ?)",
                (key, actor, now or utc_now()),
            )
        return True

    # -- experiences -----------------------------------------------------------

    def add_experience(self, experience: Experience) -> bool:
        """Store a lesson; idempotent on experience_id (False = already there)."""
        if type(experience) is not Experience:
            raise SkillStoreError("add_experience needs an Experience")
        _filter(experience)
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO skill_experiences "
                "(experience_id, body, body_format, scope_kind, project_id, skill_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (experience.experience_id, self._body(experience), BODY_FORMAT,
                 experience.scope.kind.value, experience.scope.project_id, experience.skill_id,
                 utc_now()),
            )
            return cursor.rowcount == 1

    def experiences(self, project_id: str | None) -> tuple[Experience, ...]:
        """Every ACTIVE lesson visible in ``project_id`` - the only lessons a
        task may be given (disabled and deleted ones never are)."""
        return tuple(item for item, state in self.lessons(project_id) if state == LESSON_ACTIVE)

    def _lessons(
        self, connection: sqlite3.Connection, project_id: str | None
    ) -> tuple[tuple[Experience, str], ...]:
        states: dict[str, str] = {}
        for experience_id, kind in connection.execute(
            "SELECT experience_id, kind FROM skill_experience_events ORDER BY event_id"
        ):
            if states.get(experience_id) != "deleted":  # a tombstone is final
                states[experience_id] = kind
        rows = connection.execute(
            "SELECT * FROM skill_experiences ORDER BY created_at, experience_id"
        ).fetchall()
        found = (self._read(row, Experience.from_dict, _EXPERIENCE) for row in rows)
        return tuple(
            (item, LESSON_DISABLED if states.get(item.experience_id) == LESSON_DISABLED else LESSON_ACTIVE)
            for item in found
            if item is not None
            and visible_in(item.scope, project_id)
            and states.get(item.experience_id) != "deleted"
        )

    def lessons(self, project_id: str | None) -> tuple[tuple[Experience, str], ...]:
        """Every non-deleted lesson visible in ``project_id`` with its state
        (``active`` or ``disabled``); a deleted lesson is never returned."""
        with self._connection() as connection:
            connection.execute("BEGIN")  # one snapshot: lessons and their states agree
            return self._lessons(connection, project_id)

    def set_lesson_state(
        self, experience_id: str, kind: str, project_id: str | None, *, actor: str = "user"
    ) -> None:
        """Append an ``enabled`` / ``disabled`` / ``deleted`` row for a lesson
        visible in ``project_id``; an unknown, deleted or other project's
        lesson is refused, the same answer for each."""
        _check_actor(actor)
        if kind not in _LESSON_KINDS:
            raise SkillStoreError("a lesson state is 'enabled', 'disabled' or 'deleted'")
        with self._connection(write=True) as connection:
            if not any(item.experience_id == experience_id for item, _ in self._lessons(connection, project_id)):
                raise SkillStoreError("no such lesson")
            connection.execute(
                "INSERT INTO skill_experience_events (experience_id, kind, actor, created_at) "
                "VALUES (?, ?, ?, ?)",
                (experience_id, kind, actor, utc_now()),
            )

    def record_lesson_uses(self, run_id: str, task_id: str, experience_ids: Any) -> None:
        """Append which lessons a run's task was given: ids only, never text."""
        ids = tuple(experience_ids)
        if not all(type(value) is str and value for value in (run_id, task_id, *ids)):
            raise SkillStoreError("lesson uses are non-empty string ids")
        at = utc_now()
        with self._connection(write=True) as connection:
            connection.executemany(
                "INSERT INTO skill_experience_uses (run_id, task_id, experience_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                [(run_id, task_id, experience_id, at) for experience_id in ids],
            )

    def lessons_used(self, run_id: str) -> int:
        """How many distinct lessons the run's tasks were given."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT experience_id) FROM skill_experience_uses WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])
