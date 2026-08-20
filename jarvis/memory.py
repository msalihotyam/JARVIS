"""SQLite state, approval, checkpoint and cost ledger for JARVIS."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from jarvis.contracts import (
    RETRIEVAL_CANDIDATE_WINDOW,
    AttemptState,
    BillingClass,
    DataClassification,
    DecisionRecord,
    ExecutionAttempt,
    LoopCheckpoint,
    LoopStage,
    MemoryRetrievalQuery,
    MonetaryState,
    PermissionGrant,
    PermissionRequest,
    RetrievedContext,
    SessionState,
    Task,
    TaskStatus,
    UsageRecord,
    build_retrieved_context,
    utc_now,
)


SCHEMA_VERSION = 4

#: Persisted task statuses that forbid *starting* an execution: a cancellation
#: intent or any terminal decision that won first is authoritative (H2-F).
_EXECUTION_BLOCKING_STATUSES = frozenset(
    {
        TaskStatus.CANCEL_REQUESTED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.REJECTED.value,
    }
)

#: Persisted statuses that forbid *concluding* an execution as a clean
#: success/failure: an authoritative cancellation must not be overwritten.
_CANCELLATION_STATUSES = frozenset(
    {TaskStatus.CANCEL_REQUESTED.value, TaskStatus.CANCELLED.value}
)

#: Immutable terminal statuses; a lifecycle-arbitration transaction refuses to
#: rewrite a task already in one of these (CANCEL_REQUESTED is not terminal).
_TERMINAL_STATUS_VALUES = frozenset(
    {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.REJECTED.value,
        TaskStatus.CANCELLED.value,
    }
)


class _ClaimAborted(Exception):
    """Internal: roll back an execution claim whose one-shot grant vanished."""


class _StaleWriterLost(Exception):
    """Internal: roll back a lifecycle write a terminal writer already won."""

#: Columns added by migration 3. Applied additively so existing rows survive.
_USAGE_BILLING_COLUMNS = (
    ("billing_class", "TEXT"),
    ("monetary_state", "TEXT"),
    ("monetary_cost_usd", "REAL"),
    ("reference_cost_usd", "REAL"),
    ("call_count", "INTEGER"),
    ("billing_evidence", "TEXT"),
)


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
        if target_version == 3:
            self._migrate_usage_billing(connection)
            return
        if target_version == 4:
            self._migrate_execution_journal(connection)
            return
        raise RuntimeError(f"No migration is defined for schema {target_version}")

    @staticmethod
    def _migrate_execution_journal(connection: sqlite3.Connection) -> None:
        """Add the durable execution-attempt journal and the grant-consumption
        column additively, without touching existing rows (H2)."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_attempts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                connector TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempt_task "
            "ON execution_attempts(task_id)"
        )
        existing = {
            row[1] for row in connection.execute("PRAGMA table_info(permission_grants)")
        }
        if existing and "consumed_by_attempt" not in existing:
            connection.execute(
                "ALTER TABLE permission_grants ADD COLUMN consumed_by_attempt TEXT"
            )

    @staticmethod
    def _migrate_usage_billing(connection: sqlite3.Connection) -> None:
        """Add provider-neutral billing columns without touching existing rows."""
        existing = {
            row[1] for row in connection.execute("PRAGMA table_info(usage_ledger)")
        }
        if not existing:
            # Table not created yet (fresh database); the current schema below
            # already declares the columns.
            return
        for name, column_type in _USAGE_BILLING_COLUMNS:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE usage_ledger ADD COLUMN {name} {column_type}"
                )
        # A positive legacy amount is unambiguous incremental spend and must
        # keep counting against the monthly hard limit.
        connection.execute(
            """
            UPDATE usage_ledger
               SET billing_class = ?, monetary_state = ?, monetary_cost_usd = actual_cost_usd
             WHERE billing_class IS NULL AND actual_cost_usd > 0
            """,
            (BillingClass.METERED_MONETARY.value, MonetaryState.MEASURED.value),
        )
        # A legacy zero is ambiguous: it may be an unbilled subscription call
        # or a missing measurement. It stays unknown, never a measured zero.
        connection.execute(
            """
            UPDATE usage_ledger
               SET billing_class = ?, monetary_state = ?, monetary_cost_usd = NULL
             WHERE billing_class IS NULL
            """,
            (BillingClass.UNKNOWN.value, MonetaryState.UNKNOWN.value),
        )

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
                -- H2: the one-shot execution attempt that consumed this grant,
                -- NULL while unconsumed. Reusable (remembered) grants stay NULL.
                consumed_by_attempt TEXT,
                FOREIGN KEY(request_id) REFERENCES pending_approvals(id),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_grant_scope
                ON permission_grants(session_id, scope_fingerprint, approved, expires_at);

            CREATE TABLE IF NOT EXISTS execution_attempts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                connector TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_attempt_task
                ON execution_attempts(task_id);

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
                -- actual_cost_usd and is_paid are deprecated compatibility
                -- columns kept for older readers. Monetary accounting reads
                -- billing_class/monetary_state/monetary_cost_usd only.
                actual_cost_usd REAL NOT NULL,
                is_paid INTEGER NOT NULL,
                billing_class TEXT,
                monetary_state TEXT,
                monetary_cost_usd REAL,
                reference_cost_usd REAL,
                call_count INTEGER,
                billing_evidence TEXT,
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
        connection: sqlite3.Connection,
        session_id: str,
        task: Task,
        *,
        force: bool = False,
    ) -> bool:
        """Persist a task row; True when this writer actually won.

        Terminal immutability at the storage layer: once a row holds a terminal
        status, a stale writer carrying an older non-terminal task object can
        never demote or reopen it - the guarded upsert skips the update and
        reports ``False`` so the caller can drop its dependent writes instead of
        assuming success. Every lifecycle write goes through the guard; only the
        explicit ``save_task`` setup primitive passes ``force`` to stage state.
        """
        task.touch()
        parameters = (
            task.id,
            session_id,
            SQLiteMemoryStore._json(task.to_dict()),
            task.status.value,
            task.stage.value,
            task.updated_at,
        )
        if force:
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
                parameters,
            )
            return True
        cursor = connection.execute(
            """
            INSERT INTO tasks (id, session_id, state_json, status, stage, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id = excluded.session_id,
                state_json = excluded.state_json,
                status = excluded.status,
                stage = excluded.stage,
                updated_at = excluded.updated_at
            WHERE tasks.status NOT IN ('completed', 'failed', 'rejected', 'cancelled')
            """,
            parameters,
        )
        # 0 modified rows means the guard blocked the write: a terminal row
        # already holds authority and this writer lost.
        return cursor.rowcount == 1

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
        """Explicit direct write; no product flow calls this. Staging setup
        state deliberately bypasses the terminal guard the loop writes obey."""
        with self._connection(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                raise KeyError(f"Unknown session: {session_id}")
            self._upsert_task(connection, session_id, task, force=True)

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
    ) -> bool:
        """Persist one lifecycle checkpoint; True when this writer won.

        The guarded task write and every dependent write (session, checkpoint,
        audit event, pending approval) are one transactionally consistent
        operation: if a terminal writer already holds authority, the whole
        transaction rolls back and ``False`` is returned. A stale writer
        therefore leaves behind no checkpoint, no lifecycle event and no
        phantom pending approval.
        """
        checkpoint = LoopCheckpoint(
            task_id=task.id,
            session_id=session.id,
            stage=task.stage,
            payload=payload or {},
        )
        try:
            with self._connection(write=True) as connection:
                self._upsert_session(connection, session)
                if not self._upsert_task(connection, session.id, task):
                    raise _StaleWriterLost()
                self._write_checkpoint_and_event(
                    connection, session, task, event_type, payload, checkpoint.created_at
                )
                if pending_request is not None:
                    self._upsert_pending_approval(connection, pending_request)
        except _StaleWriterLost:
            return False
        return True

    @staticmethod
    def _write_checkpoint_and_event(
        connection: sqlite3.Connection,
        session: SessionState,
        task: Task,
        event_type: str,
        payload: dict[str, Any] | None,
        created_at: str | None = None,
    ) -> None:
        stamp = created_at or utc_now()
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
                SQLiteMemoryStore._json(
                    {
                        "task_id": task.id,
                        "session_id": session.id,
                        "stage": task.stage.value,
                        "payload": payload or {},
                        "created_at": stamp,
                    }
                ),
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO event_log
                (session_id, task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session.id, task.id, event_type, SQLiteMemoryStore._json(payload or {}), utc_now()),
        )

    @staticmethod
    def _upsert_pending_approval(
        connection: sqlite3.Connection, request: PermissionRequest
    ) -> None:
        """Exactly one pending request per task/session.

        A request whose attestation changed gets a new id; the superseded
        pending row is closed in the same transaction so stale evidence can
        never be loaded as the current request again.

        Terminal immutability extends to approval issuance: a stale writer
        cannot open a fresh approval opportunity on a task that has already
        reached a terminal decision, so a rejected/cancelled/completed task
        never leaves an orphan pending request behind.
        """
        row = connection.execute(
            "SELECT status FROM tasks WHERE id = ?", (request.task_id,)
        ).fetchone()
        if row is not None and row[0] in _TERMINAL_STATUS_VALUES:
            return
        connection.execute(
            """
            INSERT INTO pending_approvals
                (id, task_id, session_id, request_json, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                request_json = excluded.request_json,
                status = 'pending',
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                resolved_at = NULL
            """,
            (
                request.id,
                request.task_id,
                request.session_id,
                SQLiteMemoryStore._json(request.to_dict()),
                utc_now(),
                request.expires_at,
            ),
        )
        connection.execute(
            """
            UPDATE pending_approvals SET status = 'superseded', resolved_at = ?
             WHERE task_id = ? AND session_id = ? AND status = 'pending' AND id != ?
            """,
            (utc_now(), request.task_id, request.session_id, request.id),
        )

    def save_pending_approval(self, request: PermissionRequest) -> None:
        with self._connection(write=True) as connection:
            self._upsert_pending_approval(connection, request)

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
    ) -> bool:
        """Atomically resolve a still-pending request; exactly one winner.

        The CAS requires the request to still be ``pending``, so concurrent
        approve and reject cannot both proceed: the loser makes no grant and
        must reload authoritative state. Returns True only for the winner.
        """
        with self._connection(write=True) as connection:
            cas = connection.execute(
                "UPDATE pending_approvals SET status = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, utc_now(), request.id),
            )
            if cas.rowcount != 1:
                # Already resolved by another contender; create no grant.
                return False
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
        return True

    def reject_task(
        self,
        request: PermissionRequest,
        grant: PermissionGrant,
        session: SessionState,
        task: Task,
        payload: dict[str, Any],
    ) -> bool:
        """Atomically resolve a pending request REJECTED and finalize the task
        as terminal rejected in ONE transaction (P1-A).

        There is no observable persisted state where the request is REJECTED
        but the task is still resumable / eligible for a fresh approval, so a
        concurrent resume can never reissue or reopen it. Returns False (no
        writes) if the request was already resolved or the task is terminal.
        """
        with self._connection(write=True) as connection:
            cas = connection.execute(
                "UPDATE pending_approvals SET status = 'rejected', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (utc_now(), request.id),
            )
            if cas.rowcount != 1:
                return False
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()
            if row is None or row[0] in _TERMINAL_STATUS_VALUES:
                return False
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
            self._upsert_session(connection, session)
            self._upsert_task(connection, session.id, task)
            self._write_checkpoint_and_event(
                connection, session, task, "approval_rejected", payload
            )
        return True

    def cancel_task(
        self,
        session: SessionState,
        task: Task,
        *,
        connector_cancelled: bool,
    ) -> str:
        """Atomically arbitrate cancellation against the execution attempt in
        ONE transaction (P1-B).

        Reads the authoritative task lifecycle and the latest attempt state
        together, then writes the decision, so a claim and a cancel cannot both
        succeed on stale observations. Returns ``"terminal"`` (already
        immutable, no write), ``"cancelled"`` (finalized terminal cancellation:
        no execution was claimed, or the connector confirmed the stop), or
        ``"cancelling"`` (an IN_FLIGHT attempt exists: intent recorded but not
        final; the executing owner finalizes at conclusion).
        """
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if row[0] in _TERMINAL_STATUS_VALUES:
                return "terminal"
            attempt = connection.execute(
                "SELECT state FROM execution_attempts WHERE task_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            in_flight = attempt is not None and attempt[0] == AttemptState.IN_FLIGHT.value
            if not in_flight or connector_cancelled:
                task.status = TaskStatus.CANCELLED
                task.stage = LoopStage.CANCELLED
                session.active_task_id = None
                task.progress = replace(task.progress, open_issues=())
                outcome = "cancelled"
            else:
                task.status = TaskStatus.CANCEL_REQUESTED
                outcome = "cancelling"
            self._upsert_session(connection, session)
            self._upsert_task(connection, session.id, task)
            self._write_checkpoint_and_event(
                connection,
                session,
                task,
                "cancellation_requested",
                {"connector_cancelled": connector_cancelled},
            )
        return outcome

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

    def has_approved_grant(
        self, session_id: str, task_id: str, fingerprint: str
    ) -> bool:
        """The grant record is the only proof a gate was satisfied.

        Expiry is enforced here: a recorded grant whose window has closed can
        never re-authorize execution, so an expired grant returns the task to
        a fresh gate rather than silently clearing it.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM permission_grants
                WHERE session_id = ? AND task_id = ? AND fingerprint = ?
                  AND approved = 1 AND expires_at > ?
                LIMIT 1
                """,
                (session_id, task_id, fingerprint, utc_now()),
            ).fetchone()
        return row is not None

    # -- H2: durable execution-attempt journal and atomic ownership --------

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row | None) -> ExecutionAttempt | None:
        if row is None:
            return None
        return ExecutionAttempt(
            id=row["id"],
            session_id=row["session_id"],
            task_id=row["task_id"],
            connector=row["connector"],
            fingerprint=row["fingerprint"],
            state=AttemptState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def latest_execution_attempt(self, task_id: str) -> ExecutionAttempt | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, session_id, task_id, connector, fingerprint, state,
                       created_at, updated_at
                FROM execution_attempts WHERE task_id = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._attempt_from_row(row)

    def open_execution_attempt(self, attempt: ExecutionAttempt) -> ExecutionAttempt:
        """Create-or-observe the active attempt for the task in one IMMEDIATE
        transaction, so concurrent openers converge on a single attempt."""
        with self._connection(write=True) as connection:
            row = connection.execute(
                """
                SELECT id, session_id, task_id, connector, fingerprint, state,
                       created_at, updated_at
                FROM execution_attempts WHERE task_id = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (attempt.task_id,),
            ).fetchone()
            if row is not None:
                return self._attempt_from_row(row)
            connection.execute(
                """
                INSERT INTO execution_attempts
                    (id, session_id, task_id, connector, fingerprint, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.id,
                    attempt.session_id,
                    attempt.task_id,
                    attempt.connector,
                    attempt.fingerprint,
                    AttemptState.PREPARED.value,
                    attempt.created_at,
                    attempt.updated_at,
                ),
            )
        return replace(attempt, state=AttemptState.PREPARED)

    def begin_execution(
        self,
        attempt_id: str,
        session_id: str,
        task_id: str,
        one_shot_fingerprints: Sequence[str],
    ) -> bool:
        """Atomic single-winner claim in ONE transaction: check authoritative
        lifecycle, transition PREPARED -> IN_FLIGHT, and consume every required
        one-shot grant, all-or-nothing. Durable before the connector boundary.

        The whole claim fails closed (rolls back, nobody executes) if the task
        was cancelled/terminal first (H2-F window A), the attempt CAS is lost,
        or any required one-shot grant is missing/expired/already consumed.
        """
        try:
            with self._connection(write=True) as connection:
                # Window A: an authoritative cancellation or terminal decision
                # that won before this claim forbids crossing the boundary.
                row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None or row[0] in _EXECUTION_BLOCKING_STATUSES:
                    return False
                cas = connection.execute(
                    "UPDATE execution_attempts SET state = ?, updated_at = ? "
                    "WHERE id = ? AND state = ?",
                    (
                        AttemptState.IN_FLIGHT.value,
                        utc_now(),
                        attempt_id,
                        AttemptState.PREPARED.value,
                    ),
                )
                if cas.rowcount != 1:
                    # Lost the race (already IN_FLIGHT/concluded). Nothing changed.
                    return False
                for fingerprint in one_shot_fingerprints:
                    consumed = connection.execute(
                        """
                        UPDATE permission_grants SET consumed_by_attempt = ?
                         WHERE session_id = ? AND task_id = ? AND fingerprint = ?
                           AND approved = 1 AND remember_for_session = 0
                           AND expires_at > ? AND consumed_by_attempt IS NULL
                        """,
                        (attempt_id, session_id, task_id, fingerprint, utc_now()),
                    )
                    if consumed.rowcount < 1:
                        # A required one-shot grant is gone; abort the whole
                        # claim so every write here rolls back atomically.
                        raise _ClaimAborted()
        except _ClaimAborted:
            return False
        return True

    def mark_execution_unknown(self, attempt_id: str) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE execution_attempts SET state = ?, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    AttemptState.UNKNOWN_OUTCOME.value,
                    utc_now(),
                    attempt_id,
                    AttemptState.IN_FLIGHT.value,
                ),
            )

    def conclude_execution(
        self,
        session: SessionState,
        task: Task,
        attempt_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        finalizing_cancellation: bool = False,
    ) -> str:
        """Atomically finish an IN_FLIGHT attempt together with the task's
        post-execution lifecycle write and audit checkpoint.

        Returns ``"finished"`` on success, ``"cancelled"`` when an authoritative
        cancellation was recorded in the conclude window and this is a normal
        (non-cancellation) conclusion - so a stale success/failure never
        overwrites it (H2-F window B) - or ``"lost"`` when the attempt was no
        longer IN_FLIGHT. ``finalizing_cancellation`` skips the cancellation
        re-check because the task being written *is* the cancellation.
        """
        with self._connection(write=True) as connection:
            if not finalizing_cancellation:
                row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()
                if row is not None and row[0] in _CANCELLATION_STATUSES:
                    return "cancelled"
            cas = connection.execute(
                "UPDATE execution_attempts SET state = ?, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    AttemptState.FINISHED.value,
                    utc_now(),
                    attempt_id,
                    AttemptState.IN_FLIGHT.value,
                ),
            )
            if cas.rowcount != 1:
                # The attempt was no longer IN_FLIGHT; do not claim the result.
                return "lost"
            self._upsert_session(connection, session)
            self._upsert_task(connection, session.id, task)
            self._write_checkpoint_and_event(
                connection, session, task, event_type, payload
            )
        return "finished"

    def execution_attempts(self, task_id: str) -> list[ExecutionAttempt]:
        """All attempts for a task in creation order (diagnostics/tests)."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, task_id, connector, fingerprint, state,
                       created_at, updated_at
                FROM execution_attempts WHERE task_id = ? ORDER BY rowid
                """,
                (task_id,),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

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
                 actual_cost_usd, is_paid, billing_class, monetary_state,
                 monetary_cost_usd, reference_cost_usd, call_count,
                 billing_evidence, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # Deprecated compatibility columns, never read for accounting.
                usage.budget_amount_usd,
                int(usage.billing_class is BillingClass.METERED_MONETARY),
                usage.billing_class.value,
                usage.monetary_state.value,
                usage.monetary_cost_usd,
                usage.reference_cost_usd,
                usage.call_count,
                usage.billing_evidence,
                usage.recorded_at,
            ),
        )

    def monthly_monetary_cost(self, month: str | None = None) -> float:
        """Only measured, genuinely metered spend counts against the cap."""
        month_key = month or datetime.now(timezone.utc).strftime("%Y-%m")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(monetary_cost_usd), 0) FROM usage_ledger
                WHERE substr(recorded_at, 1, 7) = ?
                  AND billing_class = ?
                  AND monetary_state = ?
                  AND monetary_cost_usd IS NOT NULL
                """,
                (
                    month_key,
                    BillingClass.METERED_MONETARY.value,
                    MonetaryState.MEASURED.value,
                ),
            ).fetchone()
        return float(row[0])

    def usage_records(self) -> list[UsageRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, task_id, provider, model, connector,
                       input_tokens, output_tokens, estimated_cost_usd,
                       billing_class, monetary_state, monetary_cost_usd,
                       reference_cost_usd, call_count, billing_evidence,
                       recorded_at
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
                billing_class=BillingClass(
                    row["billing_class"] or BillingClass.UNKNOWN.value
                ),
                monetary_state=MonetaryState(
                    row["monetary_state"] or MonetaryState.UNKNOWN.value
                ),
                monetary_cost_usd=row["monetary_cost_usd"],
                reference_cost_usd=row["reference_cost_usd"],
                call_count=row["call_count"] or 0,
                billing_evidence=row["billing_evidence"] or "",
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
    ) -> bool:
        """Finalize a task; True only when this writer owned the transition.

        All-or-nothing with lifecycle authority: the guarded terminal task
        write happens in the same transaction as the session update, the
        decision record, the final usage bookkeeping and the terminal event.
        If another terminal writer (a cancellation) already won, the whole
        transaction rolls back and ``False`` is returned - no decision row, no
        terminal event, no stale session overwrite.

        The usage row is keyed by its own id and was already appended by the
        execution path, so re-writing it here is idempotent and never
        duplicates legitimately recorded usage.
        """
        try:
            with self._connection(write=True) as connection:
                self._upsert_session(connection, session)
                if not self._upsert_task(connection, session.id, task):
                    raise _StaleWriterLost()
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
        except _StaleWriterLost:
            return False
        return True

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

    def retrieve_context(self, query: MemoryRetrievalQuery) -> RetrievedContext:
        """Bounded, deterministic retrieval over same-session decisions.

        Only the newest ``RETRIEVAL_CANDIDATE_WINDOW`` decisions of the query
        session are examined, the current task is excluded, and every
        classification is recovered from the persisted source task, failing
        closed as SECRET when that recovery is impossible.
        """
        sql = (
            "SELECT d.decision_json, t.state_json FROM decision_log AS d "
            "LEFT JOIN tasks AS t ON t.id = d.task_id "
            "WHERE d.session_id = ?"
        )
        parameters: list[Any] = [query.session_id]
        if query.exclude_task_id is not None:
            sql += " AND d.task_id != ?"
            parameters.append(query.exclude_task_id)
        sql += " ORDER BY d.created_at DESC, d.id ASC LIMIT ?"
        parameters.append(RETRIEVAL_CANDIDATE_WINDOW)
        with self._connection() as connection:
            current_classification: DataClassification | None = None
            if query.exclude_task_id is not None:
                current_row = connection.execute(
                    "SELECT state_json FROM tasks WHERE id = ?",
                    (query.exclude_task_id,),
                ).fetchone()
                if current_row is not None:
                    current_classification = self._classification_from_state(
                        current_row[0]
                    )
            rows = connection.execute(sql, parameters).fetchall()
        candidates: list[tuple[DecisionRecord, DataClassification]] = []
        for row in rows:
            try:
                record = DecisionRecord.from_dict(json.loads(row[0]))
            except (TypeError, KeyError, ValueError):
                # An unreadable decision row cannot be scored or shown.
                continue
            candidates.append((record, self._classification_from_state(row[1])))
        return build_retrieved_context(query, current_classification, candidates)

    @staticmethod
    def _classification_from_state(state_json: Any) -> DataClassification:
        """Recover a task's classification from its persisted JSON.

        A missing, unparseable or incomplete source task fails closed as
        SECRET so its decision can never become externally eligible.
        """
        try:
            return DataClassification(
                json.loads(state_json)["scope"]["data_classification"]
            )
        except (TypeError, KeyError, ValueError):
            return DataClassification.SECRET

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
