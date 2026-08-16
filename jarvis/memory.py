"""SQLite state, approval, checkpoint and cost ledger for JARVIS."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from jarvis.contracts import (
    DecisionRecord,
    LoopCheckpoint,
    PermissionGrant,
    PermissionRequest,
    SessionState,
    Task,
    UsageRecord,
    utc_now,
)


SCHEMA_VERSION = 2


class SQLiteMemoryStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    # Another reader may briefly hold the WAL. The committed
                    # transaction remains valid and a later writer retries.
                    pass
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        wal_connection = self._connect()
        try:
            wal_connection.execute("PRAGMA journal_mode = WAL")
        finally:
            wal_connection.close()
        with self._connection(write=True) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            legacy_version = 1 if "sessions" in tables and "schema_version" not in tables else 0
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM schema_version ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            current = int(row[0]) if row else legacy_version
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if row is None:
                connection.execute("DELETE FROM schema_version")
                connection.execute("INSERT INTO schema_version (version) VALUES (?)", (current,))
            while current < SCHEMA_VERSION:
                target = current + 1
                self._apply_migration(connection, target)
                connection.execute("UPDATE schema_version SET version = ?", (target,))
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (target, utc_now()),
                )
                current = target
            self._create_current_schema(connection)

    def _apply_migration(
        self, connection: sqlite3.Connection, target_version: int
    ) -> None:
        if target_version == 1:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            return
        if target_version == 2:
            self._create_current_schema(connection)
            return
        raise RuntimeError(f"No migration is defined for schema {target_version}")

    def _create_current_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pending_approvals (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_pending_task
                ON pending_approvals(task_id, session_id, status);

            CREATE TABLE IF NOT EXISTS permission_grants (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                scope_fingerprint TEXT NOT NULL,
                grant_json TEXT NOT NULL,
                approved INTEGER NOT NULL,
                remember_for_session INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES pending_approvals(id),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_grant_scope
                ON permission_grants(session_id, scope_fingerprint, approved, expires_at);

            CREATE TABLE IF NOT EXISTS decision_log (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                task_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS loop_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS usage_ledger (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                task_id TEXT,
                provider TEXT,
                model TEXT,
                connector TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                actual_cost_usd REAL NOT NULL,
                is_paid INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
            );
            """
        )

    def schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version FROM schema_version ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _upsert_session(connection: sqlite3.Connection, session: SessionState) -> None:
        session.touch()
        connection.execute(
            """
            INSERT INTO sessions (id, state_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (session.id, SQLiteMemoryStore._json(session.to_dict()), session.updated_at),
        )

    @staticmethod
    def _upsert_task(
        connection: sqlite3.Connection, session_id: str, task: Task
    ) -> None:
        task.touch()
        connection.execute(
            """
            INSERT INTO tasks (id, session_id, state_json, status, stage, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                state_json = excluded.state_json,
                status = excluded.status,
                stage = excluded.stage,
                updated_at = excluded.updated_at
            """,
            (
                task.id,
                session_id,
                SQLiteMemoryStore._json(task.to_dict()),
                task.status.value,
                task.stage.value,
                task.updated_at,
            ),
        )

    def save_session(self, session: SessionState) -> None:
        with self._connection(write=True) as connection:
            self._upsert_session(connection, session)

    def load_session(self, session_id: str) -> SessionState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return SessionState.from_dict(json.loads(row[0])) if row else None

    def save_task(self, session_id: str, task: Task) -> None:
        with self._connection(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                raise KeyError(f"Unknown session: {session_id}")
            self._upsert_task(connection, session_id, task)

    def load_task(self, task_id: str) -> Task | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return Task.from_dict(json.loads(row[0])) if row else None

    def task_session_id(self, task_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def persist_checkpoint(
        self,
        session: SessionState,
        task: Task,
        event_type: str,
        payload: dict[str, Any] | None = None,
        pending_request: PermissionRequest | None = None,
    ) -> None:
        checkpoint = LoopCheckpoint(
            task_id=task.id,
            session_id=session.id,
            stage=task.stage,
            payload=payload or {},
        )
        with self._connection(write=True) as connection:
            self._upsert_session(connection, session)
            self._upsert_task(connection, session.id, task)
            connection.execute(
                """
                INSERT INTO loop_checkpoints
                    (task_id, session_id, stage, checkpoint_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    session.id,
                    task.stage.value,
                    self._json(
                        {
                            "task_id": checkpoint.task_id,
                            "session_id": checkpoint.session_id,
                            "stage": checkpoint.stage.value,
                            "payload": checkpoint.payload,
                            "created_at": checkpoint.created_at,
                        }
                    ),
                    checkpoint.created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO event_log
                    (session_id, task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session.id, task.id, event_type, self._json(payload or {}), utc_now()),
            )
            if pending_request is not None:
                connection.execute(
                    """
                    INSERT INTO pending_approvals
                        (id, task_id, session_id, request_json, status, created_at, expires_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        request_json = excluded.request_json,
                        status = 'pending',
                        expires_at = excluded.expires_at,
                        resolved_at = NULL
                    """,
                    (
                        pending_request.id,
                        pending_request.task_id,
                        pending_request.session_id,
                        self._json(pending_request.to_dict()),
                        utc_now(),
                        pending_request.expires_at,
                    ),
                )

    def save_pending_approval(self, request: PermissionRequest) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO pending_approvals
                    (id, task_id, session_id, request_json, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(id) DO UPDATE SET request_json = excluded.request_json,
                    status = 'pending', expires_at = excluded.expires_at, resolved_at = NULL
                """,
                (
                    request.id,
                    request.task_id,
                    request.session_id,
                    self._json(request.to_dict()),
                    utc_now(),
                    request.expires_at,
                ),
            )

    def load_pending_approval(
        self, task_id: str, session_id: str
    ) -> PermissionRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT request_json FROM pending_approvals
                WHERE task_id = ? AND session_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (task_id, session_id),
            ).fetchone()
        return PermissionRequest.from_dict(json.loads(row[0])) if row else None

    def resolve_approval(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        status: str,
    ) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE pending_approvals SET status = ?, resolved_at = ? WHERE id = ?",
                (status, utc_now(), request.id),
            )
            connection.execute(
                """
                INSERT INTO permission_grants
                    (id, request_id, task_id, session_id, fingerprint,
                     scope_fingerprint, grant_json, approved,
                     remember_for_session, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant.id,
                    request.id,
                    grant.task_id,
                    grant.session_id,
                    grant.fingerprint,
                    request.scope_fingerprint,
                    self._json(grant.to_dict()),
                    int(grant.approved),
                    int(grant.remember_for_session),
                    grant.expires_at,
                    grant.created_at,
                ),
            )

    def has_remembered_scope_grant(
        self, session_id: str, scope_fingerprint: str
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM permission_grants
                WHERE session_id = ? AND scope_fingerprint = ?
                  AND approved = 1 AND remember_for_session = 1
                  AND expires_at > ?
                LIMIT 1
                """,
                (session_id, scope_fingerprint, utc_now()),
            ).fetchone()
        return row is not None

    def append_usage(self, usage: UsageRecord) -> None:
        with self._connection(write=True) as connection:
            self._insert_usage(connection, usage)

    @staticmethod
    def _insert_usage(connection: sqlite3.Connection, usage: UsageRecord) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO usage_ledger
                (id, session_id, task_id, provider, model, connector,
                 input_tokens, output_tokens, estimated_cost_usd,
                 actual_cost_usd, is_paid, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                usage.id,
                usage.session_id,
                usage.task_id,
                usage.provider,
                usage.model,
                usage.connector,
                usage.input_tokens,
                usage.output_tokens,
                usage.estimated_cost_usd,
                usage.actual_cost_usd,
                int(usage.is_paid),
                usage.recorded_at,
            ),
        )

    def monthly_actual_cost(self, month: str | None = None) -> float:
        month_key = month or datetime.now(timezone.utc).strftime("%Y-%m")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(actual_cost_usd), 0) FROM usage_ledger
                WHERE substr(recorded_at, 1, 7) = ?
                """,
                (month_key,),
            ).fetchone()
        return float(row[0])

    def usage_records(self) -> list[UsageRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, task_id, provider, model, connector,
                       input_tokens, output_tokens, estimated_cost_usd,
                       actual_cost_usd, is_paid, recorded_at
                FROM usage_ledger ORDER BY recorded_at, id
                """
            ).fetchall()
        return [
            UsageRecord(
                id=row["id"],
                session_id=row["session_id"],
                task_id=row["task_id"],
                provider=row["provider"],
                model=row["model"],
                connector=row["connector"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                estimated_cost_usd=row["estimated_cost_usd"],
                actual_cost_usd=row["actual_cost_usd"],
                is_paid=bool(row["is_paid"]),
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def finalize_task(
        self,
        session: SessionState,
        task: Task,
        decision: DecisionRecord,
        usage: UsageRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection(write=True) as connection:
            self._upsert_session(connection, session)
            self._upsert_task(connection, session.id, task)
            connection.execute(
                """
                INSERT OR REPLACE INTO decision_log
                    (id, session_id, task_id, decision_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.session_id,
                    decision.task_id,
                    self._json(decision.to_dict()),
                    decision.created_at,
                ),
            )
            self._insert_usage(connection, usage)
            connection.execute(
                """
                INSERT INTO event_log
                    (session_id, task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session.id, task.id, event_type, self._json(payload), utc_now()),
            )

    def list_decisions(self, session_id: str) -> list[DecisionRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT decision_json FROM decision_log
                WHERE session_id = ? ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()
        return [DecisionRecord.from_dict(json.loads(row[0])) for row in rows]

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT task_id, event_type, payload_json, created_at FROM event_log
                WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT checkpoint_json FROM loop_checkpoints
                WHERE task_id = ? ORDER BY id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None
