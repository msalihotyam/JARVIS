"""V2.2 research b2: how V2.2's skill learning would have treated past runs.

    python scripts/skill_history_count.py --db <data dir>\\jarvis.db

Read-only and in-process, no network: the database is opened ONLY through
``file:<path>?mode=ro`` (never copied, never written, nothing created next to
it). Each finished Core task is replayed through Core's own learning code
(``AgentCore._skill_learn`` with a store whose writes are counted, never
stored): would a draft be offered, is it plannable (the ``skill_run_plan``
dry-run), would a lesson be captured (the a2 allow-list), would an existing
active skill match (FIT / SIMILAR). Prints ONLY aggregate integers under fixed
labels - never text, titles, ids, paths or timestamps.

A WAL-mode database is refused: SQLite creates ``-wal``/``-shm`` next to it on
a ``mode=ro`` open. The 100-byte file header is read first to tell.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import pathname2url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis import skill_lifecycle  # noqa: E402
from jarvis.contracts import (  # noqa: E402
    ChainStatus,
    DataClassification,
    SessionState,
    Task,
    TaskStatus,
)
from jarvis.core import AgentCore  # noqa: E402
from jarvis.intent import NL_CAPABILITIES  # noqa: E402
from jarvis.lifecycle import is_terminal  # noqa: E402
from jarvis.skill_draft import (  # noqa: E402
    CHAT_CAPABILITIES,
    GLOBAL_SUPPRESSION,
    _find_params,
    _without_params,
    should_offer,
    task_kind,
)
from jarvis.skill_filter import check_fields  # noqa: E402
from jarvis.skill_match import MatchKind, SkillWithState, match_skills  # noqa: E402
from jarvis.skill_store import SkillStore, _text_fields  # noqa: E402
from jarvis.skill_types import SkillStatus  # noqa: E402

_SIDE_FILES = ("-wal", "-shm", "-journal")
_SKILL_TABLES = {"skills", "skill_events", "skill_suppressions"}
_FAMILIES = frozenset(capability.split("_")[0] for capability in NL_CAPABILITIES)
_ROUTE_CLASSES = (DataClassification.PUBLIC, DataClassification.INTERNAL)


class NotRun(Exception):
    """The database cannot be read without a side effect (fixed reason text)."""


def read_only_uri(path: Path) -> str:
    return "file:" + pathname2url(str(Path(path).resolve())) + "?mode=ro"


def connect(uri: str) -> sqlite3.Connection:
    """The ONLY way this script opens a database: ``file:<path>?mode=ro``."""
    parts = urlsplit(uri)
    if parts.scheme != "file" or parts.query != "mode=ro" or parts.fragment:
        raise ValueError("refused: only a file:<path>?mode=ro open is allowed")
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def side_files(path: Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in _SIDE_FILES if Path(f"{path}{suffix}").exists()]


def preflight(path: Path) -> None:
    if not path.is_file():
        raise NotRun("no database file")
    if side_files(path):
        raise NotRun("side files present (database in use or not closed cleanly)")
    with path.open("rb") as handle:
        header = handle.read(100)
    if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
        raise NotRun("not a SQLite database")
    if 2 in (header[18], header[19]):
        raise NotRun("WAL mode: a mode=ro open would create -wal/-shm next to the database")


class _ReadOnlySkills(SkillStore):
    """SkillStore's readers over the ro URI; never initialized (no schema write)."""

    def __init__(self, uri: str, present: bool) -> None:
        self.broken_rows = 0
        self._uri = uri
        self._present = present

    def _connect(self) -> sqlite3.Connection:
        return connect(self._uri)

    def list_skills(self, project_id):
        return super().list_skills(project_id) if self._present else ()

    def history(self, skill_id, project_id):
        return super().history(skill_id, project_id) if self._present else None

    def is_suppressed(self, key):
        return super().is_suppressed(key) if self._present else False


class _CountingStore:
    """The store ``_skill_learn`` writes to: reads pass through, a write is
    only remembered (deterministic ids: True new, False already there)."""

    def __init__(self, skills: _ReadOnlySkills) -> None:
        # Every read _skill_learn makes; b8's dedup lists skills and reads a parent's history.
        self.is_suppressed = skills.is_suppressed
        self.list_skills = skills.list_skills
        self.history = skills.history
        self._seen: set[str] = set()

    def _add(self, record_id: str) -> bool:
        new = record_id not in self._seen
        self._seen.add(record_id)
        return new

    def add_draft(self, draft) -> bool:
        return self._add("d:" + draft.draft_id)

    def add_experience(self, experience) -> bool:
        return self._add("e:" + experience.experience_id)


class _Replay(AgentCore):
    """Core's own learning code over the read-only data. No AgentCore.__init__:
    no memory store, registry, connector or loop is built."""

    def __init__(self, skills: _ReadOnlySkills, tasks: dict[str, Task]) -> None:
        self.settings = SimpleNamespace(skills_enabled=True)
        self.skill_store = _CountingStore(skills)
        self.memory = SimpleNamespace(load_task=tasks.get)
        self.plannable: Counter[bool] = Counter()

    def _skill_catalog(self) -> dict[str, str]:
        # ponytail: every NL capability counts as served (as if every connector
        # were configured); the real connector set lives in settings, not the DB.
        return {capability: hint.action for capability, hint in NL_CAPABILITIES.items()}

    def _skill_plannable(self, draft) -> bool:
        plannable = super()._skill_plannable(draft)
        self.plannable[plannable] += 1
        return plannable


def _family(capability: str | None) -> str:
    if capability in CHAT_CAPABILITIES:
        return "chat"
    head = str(capability or "").split("_")[0]
    return head if head in _FAMILIES else "other"


def _load(connection: sqlite3.Connection):
    sessions: dict[str, SessionState] = {}
    tasks: dict[str, Task] = {}
    owner: dict[str, str] = {}
    unreadable = 0
    for (body,) in connection.execute("SELECT state_json FROM sessions"):
        try:
            session = SessionState.from_dict(json.loads(body))
            sessions[session.id] = session
        except Exception:  # noqa: BLE001 - counted, never shown
            unreadable += 1
    for session_id, body in connection.execute("SELECT session_id, state_json FROM tasks"):
        try:
            task = Task.from_dict(json.loads(body))
            tasks[task.id], owner[task.id] = task, str(session_id)
        except Exception:  # noqa: BLE001 - counted, never shown
            unreadable += 1
    return sessions, tasks, owner, unreadable


def _match(skills: _ReadOnlySkills, cache: dict, task: Task, project_id: str | None) -> str:
    """``AgentCore._skill_route``'s matching for a new task, without its effects."""
    if task.scope.data_classification not in _ROUTE_CLASSES:
        return "not_eligible"
    try:
        if project_id not in cache:
            entries = {}
            for listed in skills.list_skills(project_id):
                history = skills.history(listed.skill.skill_id, project_id)
                skill = None if history is None else skill_lifecycle.usable_skill(history)
                if skill is not None:
                    entries[skill.skill_id] = SkillWithState(
                        skill=skill,
                        status=skill_lifecycle.status(history),
                        health=skill_lifecycle.health(history),
                    )
            cache[project_id] = entries
        entries = cache[project_id]
        text = "\n".join((task.objective, task.scope.target))
        text = _without_params(text, _find_params(text))
        # Offline count: the stored objective stands in for the typed request.
        result = match_skills(
            text, entries.values(), project_id, request=task.objective
        )
        if result.kind is MatchKind.FIT and result.best is not None:
            return "fit"
        for candidate in result.needs_revalidation[:1]:
            active = SkillWithState(
                skill=entries[candidate.skill_id].skill,
                status=SkillStatus.ACTIVE,
                health=entries[candidate.skill_id].health,
            )
            if (
                match_skills(text, (active,), project_id, request=task.objective).kind
                is MatchKind.FIT
            ):
                return "stale_fit"
        return result.kind.value
    except Exception:  # noqa: BLE001 - Core fails closed to no skill here too
        return "error"


def count(connection: sqlite3.Connection, uri: str) -> Counter[str]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    sessions, tasks, owner, unreadable = _load(connection)
    skills = _ReadOnlySkills(uri, _SKILL_TABLES <= tables)
    replay = _Replay(skills, tasks)
    cache: dict = {}
    in_chain = {i for s in sessions.values() if s.chain is not None for i in s.chain.step_task_ids}
    counts: Counter[str] = Counter(sessions=len(sessions), tasks=len(tasks), rows_unreadable=unreadable)

    for task in tasks.values():
        family = _family(task.capability)

        def add(metric: str, n: int = 1) -> None:
            if n:
                counts[metric] += n
                counts[f"family.{family}.{metric}"] += n

        def learn(run, **skill) -> None:
            if run is None:
                return add("learn.not_representable")
            add("learn.replayed")
            if not check_fields(_text_fields(run.to_dict())).allowed:
                add("learn.secret_refused")
            suppressed = {k for k in (GLOBAL_SUPPRESSION, task_kind(run)) if skills.is_suppressed(k)}
            add(f"offer_reason.{should_offer(run, suppressed, ()).reason.value}")
            before = Counter(replay.plannable)
            learned = replay._skill_learn(run, **skill)
            add("drafts_plannable", replay.plannable[True] - before[True])
            add("drafts_unplannable", replay.plannable[False] - before[False])
            add("offered", bool(learned["draft_ids"]))
            add("drafts_offered", len(learned["draft_ids"]))
            add("lesson_captured", bool(learned["experience_ids"]))
            add("lessons", len(learned["experience_ids"]))
            add("lessons_dropped", learned["dropped"])
            add("lesson_skipped_external", bool(learned.get("experience_skipped")))

        if not is_terminal(task):
            add("unfinished")
            continue
        add("finished")
        add(f"status.{task.status.value}")
        session = sessions.get(owner[task.id])
        state = AgentCore._skill_state(task)
        if session is None:
            add("no_session")
        elif task.id in in_chain:
            add("chain_step")  # the chain is the run; a step is never learned alone
        elif state.get("state") == "finished":
            # A skill run's anchor (AgentCore._skill_run_finished).
            add("skill_run")
            runner = sessions.get(str(state.get("chain_session_id") or ""))
            if runner is None or runner.chain is None or runner.chain.status not in (
                ChainStatus.COMPLETED,
                ChainStatus.FAILED,
            ):
                add("learn.not_replayable")
                continue
            try:
                history = skills.history(state["skill_id"], session.project_id)
                version = AgentCore._skill_version_of(history, state.get("version"))
            except Exception:  # noqa: BLE001 - an unreadable skill teaches nothing
                version = None
            if version is None:
                add("learn.not_replayable")
                continue
            # ponytail: health=None, the health BEFORE that run is not recorded.
            learn(
                replay._skill_chain_summary(runner, task.objective, state, anchor=task),
                skill=version,
                history=history,
            )
        elif state.get("state") not in (None, "not_used"):
            add("skill_met")  # a declined or stale skill: Core learns nothing from it
        else:
            add("standalone")
            add(f"match.{_match(skills, cache, task, session.project_id)}")
            if task.status is TaskStatus.COMPLETED:  # AgentCore._skill_learn_task
                done = TaskStatus.COMPLETED.value
                learn(
                    replay._skill_run_summary(
                        task.id, task.objective, session.project_id, done, [(task, done)], {}
                    )
                )
    counts["skill_rows_broken"] = skills.broken_rows
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V2.2 skill history counts (read-only, integers only)")
    parser.add_argument("--db", required=True, type=Path)
    path = parser.parse_args(argv).db.resolve()
    try:
        preflight(path)
        uri = read_only_uri(path)
        connection = connect(uri)
        try:
            counts = count(connection, uri)
        finally:
            connection.close()
        if side_files(path):
            raise NotRun("side files appeared during the read")
    except NotRun as exc:
        print(f"not run: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never echo a message that may carry data
        print(f"not run: {type(exc).__name__}", file=sys.stderr)
        return 1
    for key in sorted(counts):
        print(f"{key} {counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
