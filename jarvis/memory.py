"""SQLite state, approval, checkpoint and cost ledger for JARVIS."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import uuid4

from jarvis.contracts import (
    COST_CAP_GATE,
    MAX_NANO_USD,
    MAX_SQLITE_INTEGER,
    MONTHLY_HARD_LIMIT_NANO_USD,
    PROJECT_MEMORY_CANDIDATE_WINDOW,
    PROJECT_MEMORY_TEXT_CAP,
    RETRIEVAL_CANDIDATE_WINDOW,
    AttemptState,
    BillingClaim,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    DataClassification,
    DecisionRecord,
    ExecutionAttempt,
    ExecutionClaimOutcome,
    LedgerSnapshot,
    LoopCheckpoint,
    LoopStage,
    MemoryRetrievalQuery,
    MonetaryState,
    PermissionGrant,
    PermissionRequest,
    ProjectMemoryItem,
    ProjectRecord,
    ReconciliationOutcome,
    ReconciliationResult,
    ReservationState,
    RetrievedContext,
    SessionState,
    Task,
    TaskStatus,
    UsageRecord,
    billing_class_from_raw,
    build_retrieved_context,
    canonical_usd,
    format_usd,
    ledger_month,
    monetary_state_from_raw,
    nano_usd,
    normalize_retrieved_text,
    normalize_symbol,
    persistable_count,
    redact_credentials,
    redact_local_paths,
    require_billing_class,
    require_monetary_state,
    reservation_state_from_raw,
    usd_from_nano,
    utc_now,
)


SCHEMA_VERSION = 6

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


def _exact_nano(value: Any, field: str) -> int:
    """An exact, non-negative, persistable integer nano-USD amount."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BillingIntegrityError(f"{field}: not an exact integer amount")
    if value < 0 or value > MAX_NANO_USD:
        raise BillingIntegrityError(f"{field}: outside the monetary range")
    return value


def validate_ledger_usage(
    billing_class: BillingClass,
    monetary_state: MonetaryState,
    monetary_cost_usd: Any,
    reference_cost_usd: Any,
    *,
    where: str,
) -> None:
    """The authoritative class/state/money matrix for *recorded* usage history.

    This validates what a ledger row is allowed to be, not what a connector may
    be uncertain about before it runs. Pre-execution uncertainty belongs to
    ``jarvis.contracts.validate_cost_estimate``; by the time a row exists in the
    monthly ledger there is nothing left to be uncertain about, so the metered
    route admits exactly one shape:

    * ``METERED_MONETARY`` -> ``MEASURED`` only, with a present, finite,
      non-negative, exactly representable amount. ``UNKNOWN``,
      ``NOT_APPLICABLE``, a missing amount or a malformed amount are all
      invalid; a provider "reference" figure can never be substituted.
    * ``SUBSCRIPTION_INCLUDED`` / ``NO_ADDITIONAL_CHARGE`` -> ``NOT_APPLICABLE``
      with no monetary amount at all; only a finite, non-negative
      ``reference_cost_usd`` may survive, and it is never money.
    * ``UNKNOWN`` -> never measured and never monetary. This is the legacy
      "route could not be established" row; it contributes no spend.

    An unresolved *reservation* is a different thing entirely and keeps its own
    semantics: it is exposure without a usage row, validated separately.
    """
    require_billing_class(billing_class, f"{where}.billing_class")
    require_monetary_state(monetary_state, f"{where}.monetary_state")
    if reference_cost_usd is not None:
        canonical_usd(reference_cost_usd, f"{where}.reference_cost_usd")
    if billing_class is BillingClass.METERED_MONETARY:
        if monetary_state is not MonetaryState.MEASURED:
            # A recorded metered row is settled money or it is corruption; it
            # is never a valid zero-cost monetary history entry.
            raise BillingIntegrityError(f"{where}: metered row is not measured")
        if monetary_cost_usd is None:
            raise BillingIntegrityError(
                f"{where}: measured metered row carries no amount"
            )
        canonical_usd(monetary_cost_usd, f"{where}.monetary_cost_usd")
        return
    if billing_class in (
        BillingClass.SUBSCRIPTION_INCLUDED,
        BillingClass.NO_ADDITIONAL_CHARGE,
    ):
        if monetary_state is not MonetaryState.NOT_APPLICABLE:
            raise BillingIntegrityError(f"{where}: unbilled route is not not_applicable")
        if monetary_cost_usd is not None:
            raise BillingIntegrityError(f"{where}: unbilled route carries an amount")
        return
    # UNKNOWN: never a measured monetary row.
    if monetary_state is MonetaryState.MEASURED or monetary_cost_usd is not None:
        raise BillingIntegrityError(f"{where}: unknown route carries measured money")


def validate_usage_record(usage: UsageRecord) -> None:
    """Fail closed on data that must never reach the ledger.

    Exact enum members are required at this runtime boundary, the class/state/
    money matrix is enforced, and every persisted integer must fit SQLite's
    INTEGER range so an oversized Python int fails as policy rather than as a
    driver ``OverflowError``.
    """
    validate_ledger_usage(
        usage.billing_class,
        usage.monetary_state,
        usage.monetary_cost_usd,
        usage.reference_cost_usd,
        where="usage",
    )
    canonical_usd(usage.estimated_cost_usd, "estimated_cost_usd")
    for field in ("input_tokens", "output_tokens", "call_count"):
        value = getattr(usage, field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_SQLITE_INTEGER
        ):
            raise BillingIntegrityError(f"{field}: not a persistable integer count")


#: The ledger-relevant identity of a usage row. ``recorded_at`` is deliberately
#: excluded: it is storage-owned and legitimately differs between a first write
#: and an identical replay of the same attempt.
_USAGE_IDENTITY_FIELDS = (
    "id",
    "session_id",
    "task_id",
    "provider",
    "model",
    "connector",
    "input_tokens",
    "output_tokens",
    "call_count",
    "estimated_cost_usd",
    "billing_class",
    "monetary_state",
    "monetary_cost_usd",
    "reference_cost_usd",
    "billing_evidence",
)


def _same_normalized_usage(left: UsageRecord, right: UsageRecord) -> bool:
    """Whether two usage rows are the same normalized ledger record."""
    return all(
        getattr(left, name) == getattr(right, name)
        for name in _USAGE_IDENTITY_FIELDS
    )


#: Exact shape of a persisted UTC ledger month key (``YYYY-MM``).
_LEDGER_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def validated_ledger_month(value: Any) -> str:
    """Fail-closed decoding of a persisted ledger month key."""
    if not isinstance(value, str) or not _LEDGER_MONTH_PATTERN.match(value):
        raise BillingIntegrityError("ledger_month: not a valid UTC month key")
    return value


def ledger_timestamp(month: str, claimed_at: Any) -> str:
    """The authoritative monetary timestamp for an attempt's ledger month.

    Monthly accounting keys on ``substr(recorded_at, 1, 7)``, so the usage row
    that replaces a reservation must carry a timestamp inside the month the
    reservation was fixed to at claim time. The storage-owned claim time is
    reused when it already falls in that month - the normal case, and it keeps
    ledger ordering meaningful - otherwise the first instant of the month is
    used so the row can never land outside it.

    Reconciliation wall-clock time, completion time and connector-supplied
    timestamps are never consulted: an execution claimed in month A stays
    accounted in month A even when it concludes in month B, so its reservation
    is replaced in place and neither the reservation nor the actual spend can
    migrate between monthly caps.
    """
    key = validated_ledger_month(month)
    if isinstance(claimed_at, str) and claimed_at[:7] == key:
        return claimed_at
    return f"{key}-01T00:00:00+00:00"


class _ClaimAborted(Exception):
    """Internal: roll back an execution claim and report a typed outcome.

    Raised for a vanished one-shot grant, a ledger that moved after approval,
    an unreadable ledger or a failed reservation insert. Every write in the
    claim transaction rolls back together.
    """

    def __init__(self, outcome: ExecutionClaimOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


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
        if target_version == 5:
            self._migrate_execution_billing(connection)
            return
        if target_version == 6:
            self._migrate_project_memory(connection)
            return
        raise RuntimeError(f"No migration is defined for schema {target_version}")

    @staticmethod
    def _migrate_project_memory(connection: sqlite3.Connection) -> None:
        """Add project identity and explicit project-scoped memory (M1).

        Strictly additive and idempotent: two new tables, one nullable column
        on ``sessions``. No existing row is rewritten, so every schema-5
        database keeps its full history and every legacy session stays a
        project-less session (NULL) with byte-identical behaviour.
        """
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_memory_items (
                -- M1: INSERT-only rows; content columns are never rewritten.
                -- Supersede flips only the lifecycle flags below.
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                -- Explicit, user-supplied classification; never defaulted or
                -- downgraded by code.
                classification TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                superseded_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_pmi_project_active
                ON project_memory_items(project_id, active);
            """
        )
        existing = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        if existing and "project_id" not in existing:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN project_id TEXT"
            )

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
    def _migrate_execution_billing(connection: sqlite3.Connection) -> None:
        """Add the attempt-bound execution-billing/reservation journal (H3).

        Strictly additive and idempotent: no task, grant, attempt or usage row
        is rewritten, so every schema-4 database keeps its full history.
        """
        connection.executescript(
"""
            CREATE TABLE IF NOT EXISTS attempt_billing (
                -- H3: at most one billing record per execution attempt.
                attempt_id TEXT PRIMARY KEY,
                -- Storage-owned ledger identity; a connector never supplies it.
                usage_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                -- Fixed at claim time; a later write cannot move the month.
                ledger_month TEXT NOT NULL,
                -- The authoritative execution-time route the claim was made on.
                billing_class TEXT NOT NULL,
                -- Exact integer nano-USD; never a binary float.
                reserved_nano_usd INTEGER NOT NULL,
                state TEXT NOT NULL,
                actual_nano_usd INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES execution_attempts(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_attempt_billing_month
                ON attempt_billing(ledger_month, state);
"""
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
                updated_at TEXT NOT NULL,
                project_id TEXT
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_memory_items (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                classification TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                superseded_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_pmi_project_active
                ON project_memory_items(project_id, active);

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

            CREATE TABLE IF NOT EXISTS attempt_billing (
                -- H3: at most one billing record per execution attempt.
                attempt_id TEXT PRIMARY KEY,
                -- Storage-owned ledger identity; a connector never supplies it.
                usage_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                -- Fixed at claim time; a later write cannot move the month.
                ledger_month TEXT NOT NULL,
                -- The authoritative execution-time route the claim was made on.
                billing_class TEXT NOT NULL,
                -- Exact integer nano-USD; never a binary float.
                reserved_nano_usd INTEGER NOT NULL,
                state TEXT NOT NULL,
                actual_nano_usd INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES execution_attempts(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_attempt_billing_month
                ON attempt_billing(ledger_month, state);
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
            INSERT INTO sessions (id, state_json, updated_at, project_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at,
                project_id = excluded.project_id
            """,
            (
                session.id,
                SQLiteMemoryStore._json(session.to_dict()),
                session.updated_at,
                session.project_id,
            ),
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
        billing: BillingClaim,
    ) -> ExecutionClaimOutcome:
        """Atomic single-winner claim in ONE transaction: check authoritative
        lifecycle and attempt ownership, verify the billing claim against the
        re-derived ledger, transition PREPARED -> IN_FLIGHT, consume every
        required one-shot grant and create the attempt-bound reservation - all
        or nothing. Durable before the connector boundary.

        Ordering is part of the contract (H3 P1-D): whether this caller can
        still own the exact attempt is settled *first*. A contender that lost
        execution ownership gets ``LOST`` immediately and never triggers a
        ledger recheck, a grant consumption, a reservation or a bounded regate
        back to COST_PREFLIGHT - losing a race is not a billing change. Only an
        otherwise-claimable candidate reaches the authoritative cap check.

        ``billing`` is mandatory and carries a real reservation, including for
        zero-monetary routes. Billing is never inferred from absence: there is
        no implicit route and no synthetic fallback claim, so no execution can
        be claimed without an explicit, validated monetary position.

        Raises ``BillingIntegrityError`` for a missing or structurally invalid
        claim (wrong type, malformed amounts, or a hard limit that is not the
        canonical policy value); nothing is written and the connector boundary
        is never reached.
        """
        claim = self._validated_claim(billing)
        try:
            with self._connection(write=True) as connection:
                # Window A: an authoritative cancellation or terminal decision
                # that won before this claim forbids crossing the boundary.
                row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None or row[0] in _EXECUTION_BLOCKING_STATUSES:
                    return ExecutionClaimOutcome.LOST
                # P1-D: ownership before money. The attempt must exist, belong
                # to this exact session/task and still be claimable.
                attempt = connection.execute(
                    "SELECT session_id, task_id, state FROM execution_attempts "
                    "WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
                if (
                    attempt is None
                    or attempt["session_id"] != session_id
                    or attempt["task_id"] != task_id
                    or attempt["state"] != AttemptState.PREPARED.value
                ):
                    return ExecutionClaimOutcome.LOST
                self._verify_billing_claim(
                    connection, session_id, task_id, one_shot_fingerprints, claim
                )
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
                    return ExecutionClaimOutcome.LOST
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
                        raise _ClaimAborted(ExecutionClaimOutcome.LOST)
                self._create_reservation(
                    connection, attempt_id, session_id, task_id, claim
                )
        except _ClaimAborted as aborted:
            return aborted.outcome
        return ExecutionClaimOutcome.CLAIMED

    @staticmethod
    def _validated_claim(billing: BillingClaim) -> BillingClaim:
        """Exact-type, exact-value validation of the caller's billing claim.

        ``None``, a raw dict, a string, a subclass or a structurally impossible
        claim is refused before any transaction opens, so a malformed claim can
        never be persisted and an absent one is never turned into a synthetic
        zero-cost route. The hard limit is storage-owned policy: a
        caller-supplied larger ceiling is never trusted, it must equal the
        canonical value.
        """
        if type(billing) is not BillingClaim:
            raise BillingIntegrityError("billing claim: exact BillingClaim required")
        billing_class = require_billing_class(billing.billing_class)
        if billing_class is BillingClass.UNKNOWN:
            raise BillingIntegrityError("billing claim: unknown route cannot be claimed")
        validated_ledger_month(billing.ledger_month)
        reservation = _exact_nano(billing.reservation_nano_usd, "reservation_nano_usd")
        _exact_nano(billing.expected_exposure_nano_usd, "expected_exposure_nano_usd")
        if billing_class is BillingClass.METERED_MONETARY:
            if reservation <= 0:
                raise BillingIntegrityError(
                    "billing claim: a metered reservation must be > 0"
                )
        elif reservation != 0:
            raise BillingIntegrityError(
                "billing claim: a non-metered reservation must be exactly 0"
            )
        if billing.limit_nano_usd != MONTHLY_HARD_LIMIT_NANO_USD:
            # Storage-owned cap. A caller cannot raise its own ceiling.
            raise BillingIntegrityError(
                "billing claim: hard limit is not the canonical policy value"
            )
        if not isinstance(billing.gate_state, str) or not billing.gate_state:
            raise BillingIntegrityError("billing claim: gate state is missing")
        if not isinstance(billing.cost_override_fingerprint, str):
            raise BillingIntegrityError("billing claim: malformed override fingerprint")
        return billing

    @classmethod
    def _verify_billing_claim(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
        task_id: str,
        one_shot_fingerprints: Sequence[str],
        billing: BillingClaim,
    ) -> None:
        """Authoritative cap check inside the claim transaction (H3).

        The ledger is rebuilt here, not trusted from preflight: two concurrent
        metered tasks cannot both clear the cap against the same balance,
        because the loser's expected exposure no longer matches.
        """
        snapshot = cls._snapshot_from(connection, billing.ledger_month)
        if not snapshot.valid:
            raise _ClaimAborted(ExecutionClaimOutcome.LEDGER_INVALID)
        if billing.ledger_month != ledger_month():
            # The month rolled over between preflight and the claim; the
            # approved projection describes a month that is no longer current.
            raise _ClaimAborted(ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED)
        if snapshot.total_nano_usd != billing.expected_exposure_nano_usd:
            raise _ClaimAborted(ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED)
        # The cap compared against is storage-owned policy, never the claim.
        limit = MONTHLY_HARD_LIMIT_NANO_USD
        projected = snapshot.total_nano_usd + billing.reservation_nano_usd
        if projected <= limit:
            return
        # Crossing the hard limit needs the exact approved override for THIS
        # projection. A grant approved for a lower/stale projection carries a
        # different fingerprint and can never authorize a higher one.
        fingerprint = billing.cost_override_fingerprint
        if not fingerprint or fingerprint not in tuple(one_shot_fingerprints):
            raise _ClaimAborted(ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED)
        override = connection.execute(
            """
            SELECT p.request_json FROM permission_grants AS g
              JOIN pending_approvals AS p ON p.id = g.request_id
             WHERE g.session_id = ? AND g.task_id = ? AND g.fingerprint = ?
               AND g.approved = 1 AND g.remember_for_session = 0
               AND g.expires_at > ? AND g.consumed_by_attempt IS NULL
             LIMIT 1
            """,
            (session_id, task_id, fingerprint, utc_now()),
        ).fetchone()
        if override is None or not cls._is_exact_cost_override(
            override["request_json"], projected, limit
        ):
            raise _ClaimAborted(ExecutionClaimOutcome.BILLING_RECHECK_REQUIRED)

    @staticmethod
    def _is_exact_cost_override(
        request_json: Any, projected: int, limit: int
    ) -> bool:
        """Whether the persisted approved request really is *this* cost override.

        Not "some valid grant fingerprint": the persisted request must name the
        monthly-cost-cap action and its attested cost gate must bind exactly the
        projection and the limit recomputed inside this transaction. A grant
        approved for a different action, a stale projection or a different cap
        can never authorize the claim.
        """
        try:
            request = json.loads(request_json)
            action = request["scope"]["action"]
            cost_gate = request["attestation"]["cost_gate"]
        except (TypeError, ValueError, KeyError):
            return False
        if not isinstance(action, str) or not isinstance(cost_gate, str):
            return False
        if normalize_symbol(action) != COST_CAP_GATE:
            return False
        expected = "|".join(
            (
                COST_CAP_GATE,
                format_usd(usd_from_nano(projected)),
                format_usd(usd_from_nano(limit)),
            )
        )
        # The trailing component is the paid-API switch state at approval time;
        # it must be a legal flag, but the money-bearing components are exact.
        return cost_gate.startswith(f"{expected}|") and cost_gate[
            len(expected) + 1 :
        ] in ("0", "1")

    @staticmethod
    def _create_reservation(
        connection: sqlite3.Connection,
        attempt_id: str,
        session_id: str,
        task_id: str,
        billing: BillingClaim,
    ) -> None:
        """Bind the reservation to the attempt in the same claim transaction.

        A subscription/no-additional-charge route still gets a zero-valued
        record, so the route and the ledger identity are bound for idempotent
        reconciliation without adding monetary exposure. Every field was already
        validated by ``_validated_claim``.
        """
        stamp = utc_now()
        try:
            connection.execute(
                """
                INSERT INTO attempt_billing
                    (attempt_id, usage_id, session_id, task_id, ledger_month,
                     billing_class, reserved_nano_usd, state, actual_nano_usd,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    attempt_id,
                    str(uuid4()),
                    session_id,
                    task_id,
                    billing.ledger_month,
                    billing.billing_class.value,
                    int(billing.reservation_nano_usd),
                    ReservationState.ACTIVE.value,
                    stamp,
                    stamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise _ClaimAborted(ExecutionClaimOutcome.LOST) from exc

    def mark_reservation_unresolved(self, attempt_id: str) -> None:
        """Retain the exposure of an attempt whose usage could not be accepted.

        ACTIVE -> UNRESOLVED only. A reconciled reservation is never reopened,
        and no reservation is ever released here: H3 implements no automatic
        recovery, so the money stays visible until explicit recovery work.
        """
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE attempt_billing SET state = ?, updated_at = ? "
                "WHERE attempt_id = ? AND state = ?",
                (
                    ReservationState.UNRESOLVED.value,
                    utc_now(),
                    attempt_id,
                    ReservationState.ACTIVE.value,
                ),
            )

    def attempt_billing_record(self, attempt_id: str) -> dict[str, Any] | None:
        """The attempt's billing/reservation row (diagnostics and tests)."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM attempt_billing WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def reconcile_usage(
        self, reconciliation: BillingReconciliation
    ) -> ReconciliationResult:
        """Authoritative attempt-bound usage reconciliation (H3).

        Storage owns the usage id, the recorded timestamp and the ledger month;
        the connector supplies telemetry only. The usage insert and the
        reservation transition commit together, so measured spend replaces the
        reserved exposure exactly once and never double counts.
        """
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM attempt_billing WHERE attempt_id = ?",
                (reconciliation.attempt_id,),
            ).fetchone()
            if row is None:
                return ReconciliationResult(
                    ReconciliationOutcome.MISSING, reason="no_reservation"
                )
            try:
                authoritative = billing_class_from_raw(row["billing_class"])
                state = reservation_state_from_raw(row["state"])
                usage = self._authoritative_usage(row, reconciliation)
            except BillingIntegrityError as exc:
                return ReconciliationResult(
                    ReconciliationOutcome.CONFLICT, reason=str(exc)
                )
            if reconciliation.billing_class is not authoritative:
                # The connector may never downgrade or replace the route the
                # execution claim was authorized against.
                return ReconciliationResult(
                    ReconciliationOutcome.CONFLICT, reason="route_mismatch"
                )
            existing = connection.execute(
                "SELECT * FROM usage_ledger WHERE id = ?", (row["usage_id"],)
            ).fetchone()
            if existing is not None or state is ReservationState.RECONCILED:
                if existing is None:
                    return ReconciliationResult(
                        ReconciliationOutcome.CONFLICT, reason="missing_usage_row"
                    )
                stored = self._usage_from_row(existing)
                if _same_normalized_usage(stored, usage):
                    return ReconciliationResult(
                        ReconciliationOutcome.IDEMPOTENT, usage=stored
                    )
                return ReconciliationResult(
                    ReconciliationOutcome.CONFLICT, reason="conflicting_replay"
                )
            try:
                self._insert_usage(connection, usage)
            except (BillingIntegrityError, sqlite3.IntegrityError):
                return ReconciliationResult(
                    ReconciliationOutcome.CONFLICT, reason="usage_id_collision"
                )
            connection.execute(
                """
                UPDATE attempt_billing
                   SET state = ?, actual_nano_usd = ?, updated_at = ?
                 WHERE attempt_id = ? AND state IN (?, ?)
                """,
                (
                    ReservationState.RECONCILED.value,
                    int(reconciliation.actual_nano_usd or 0),
                    utc_now(),
                    reconciliation.attempt_id,
                    ReservationState.ACTIVE.value,
                    ReservationState.UNRESOLVED.value,
                ),
            )
        return ReconciliationResult(ReconciliationOutcome.RECONCILED, usage=usage)

    @staticmethod
    def _authoritative_usage(
        row: sqlite3.Row, reconciliation: BillingReconciliation
    ) -> UsageRecord:
        """Build the ledger row from storage-owned identity plus telemetry.

        Raises ``BillingIntegrityError`` for a route/state mismatch, a malformed
        monetary amount or an unknown metered actual - never a silent zero.
        """
        billing_class = require_billing_class(reconciliation.billing_class)
        monetary_state = require_monetary_state(reconciliation.monetary_state)
        actual = reconciliation.actual_nano_usd
        reference: float | None = None
        if billing_class is BillingClass.METERED_MONETARY:
            if monetary_state is not MonetaryState.MEASURED:
                raise BillingIntegrityError("metered actual is not measured")
            monetary_cost: float | None = float(
                usd_from_nano(_exact_nano(actual, "actual_nano_usd"))
            )
        elif billing_class in (
            BillingClass.SUBSCRIPTION_INCLUDED,
            BillingClass.NO_ADDITIONAL_CHARGE,
        ):
            if monetary_state is not MonetaryState.NOT_APPLICABLE:
                raise BillingIntegrityError("unbilled route is not not_applicable")
            if actual not in (None, 0):
                raise BillingIntegrityError("unbilled route carries a monetary amount")
            monetary_cost = None
            if reconciliation.reference_cost_usd is not None:
                reference = float(
                    canonical_usd(
                        reconciliation.reference_cost_usd, "reference_cost_usd"
                    )
                )
        else:
            raise BillingIntegrityError("unknown route cannot be reconciled")
        validate_ledger_usage(
            billing_class, monetary_state, monetary_cost, reference, where="usage"
        )
        return UsageRecord(
            provider=reconciliation.provider,
            model=reconciliation.model,
            connector=reconciliation.connector,
            # Session and task identity come from the claim, not the connector.
            session_id=row["session_id"],
            task_id=row["task_id"],
            # Connector-reported counts are normalized to the legacy-safe zero
            # when malformed, negative, non-integral or beyond SQLite's INTEGER
            # range; no billing is ever inferred from them.
            input_tokens=persistable_count(reconciliation.input_tokens),
            output_tokens=persistable_count(reconciliation.output_tokens),
            # One claimed execution is exactly one external call.
            call_count=1,
            estimated_cost_usd=float(
                canonical_usd(reconciliation.estimated_cost_usd, "estimated_cost_usd")
            ),
            billing_class=billing_class,
            monetary_state=monetary_state,
            monetary_cost_usd=monetary_cost,
            reference_cost_usd=reference,
            billing_evidence=reconciliation.billing_evidence,
            # Storage-owned ledger identity and time. A connector-supplied id
            # can never overwrite a row, and neither a connector timestamp nor
            # the wall clock at reconciliation decides the monetary month: the
            # row inherits the ledger month fixed by the claim transaction, so
            # a metered execution that spans a UTC month boundary still
            # replaces its own reservation instead of migrating to the next
            # month's cap. ``attempt_billing.created_at``/``updated_at`` remain
            # the operational/audit timestamps and never drive accounting.
            id=row["usage_id"],
            recorded_at=ledger_timestamp(row["ledger_month"], row["created_at"]),
        )

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
        """Compatibility insertion for direct/legacy writers.

        Kept because the storage abstraction and its contract tests still use
        it, but no longer a bypass: invalid monetary data is rejected and an
        existing usage id is never overwritten by a conflicting record. The
        product execution path uses ``reconcile_usage`` instead, so connector
        identity and time can never become ledger authority.
        """
        validate_usage_record(usage)
        with self._connection(write=True) as connection:
            self._insert_usage(connection, usage)

    @classmethod
    def _insert_usage(
        cls, connection: sqlite3.Connection, usage: UsageRecord
    ) -> None:
        """Immutable insert. A repeat of the byte-identical row is a no-op; a
        conflicting duplicate id fails closed instead of silently replacing a
        prior ledger record (the old ``INSERT OR REPLACE`` behaviour)."""
        existing = connection.execute(
            "SELECT * FROM usage_ledger WHERE id = ?", (usage.id,)
        ).fetchone()
        if existing is not None:
            if _same_normalized_usage(cls._usage_from_row(existing), usage):
                return
            raise BillingIntegrityError("usage id collides with a different record")
        connection.execute(
            """
            INSERT INTO usage_ledger
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

    @staticmethod
    def _snapshot_from(
        connection: sqlite3.Connection, month_key: str
    ) -> LedgerSnapshot:
        """Rebuild the authoritative monetary position from persisted rows.

        Validity is decided by validating the *complete* relevant monthly
        accounting dataset, never by selecting only the rows that already look
        well formed (H3 P1-B). Every usage row recorded in the month and every
        reservation bound to the month is inspected: enum exactness, the
        class/state/money matrix, exact fixed-point non-negative amounts,
        required/null combinations, and the reservation <-> authoritative usage
        relationship. Every amount is converted exactly to integer nano-USD, so
        no binary float arithmetic decides an enforcement question.

        Any integrity violation invalidates the whole snapshot with a bounded,
        non-secret reason. A malformed or tampered row is never silently
        excluded from exposure, and no paid execution may proceed on it.
        """
        try:
            # A malformed month key would match no row and return a
            # valid-looking empty position; refuse it instead.
            validated_ledger_month(month_key)
        except BillingIntegrityError:
            return LedgerSnapshot.invalid(str(month_key)[:32], "malformed_ledger_month")
        try:
            usage_rows = connection.execute(
                """
                SELECT id, billing_class, monetary_state, monetary_cost_usd,
                       reference_cost_usd, input_tokens, output_tokens, call_count
                  FROM usage_ledger
                 WHERE substr(recorded_at, 1, 7) = ?
                """,
                (month_key,),
            ).fetchall()
            reservation_rows = connection.execute(
                """
                SELECT attempt_id, usage_id, billing_class, reserved_nano_usd,
                       state, actual_nano_usd
                  FROM attempt_billing WHERE ledger_month = ?
                """,
                (month_key,),
            ).fetchall()
        except sqlite3.Error:
            return LedgerSnapshot.invalid(month_key, "ledger_unreadable")

        measured = 0
        settled: dict[str, tuple[BillingClass, int | None]] = {}
        for row in usage_rows:
            try:
                # A legacy pre-migration NULL reads as UNKNOWN; anything else
                # unparseable is tampered data and fails the whole month.
                billing_class = billing_class_from_raw(
                    row["billing_class"] or BillingClass.UNKNOWN.value
                )
                monetary_state = monetary_state_from_raw(
                    row["monetary_state"] or MonetaryState.UNKNOWN.value
                )
                validate_ledger_usage(
                    billing_class,
                    monetary_state,
                    row["monetary_cost_usd"],
                    row["reference_cost_usd"],
                    where="usage",
                )
                for field in ("input_tokens", "output_tokens", "call_count"):
                    value = row[field]
                    if value is None:
                        continue
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        or value > MAX_SQLITE_INTEGER
                    ):
                        raise BillingIntegrityError("usage: malformed integer telemetry")
                amount: int | None = None
                if (
                    billing_class is BillingClass.METERED_MONETARY
                    and monetary_state is MonetaryState.MEASURED
                ):
                    amount = nano_usd(row["monetary_cost_usd"], "monetary_cost_usd")
                    measured += amount
            except BillingIntegrityError:
                return LedgerSnapshot.invalid(month_key, "malformed_usage_row")
            # ``usage_ledger.id`` is a TEXT primary key, so a non-text value is
            # coerced and a duplicate is impossible - but SQLite still permits
            # NULL in a non-INTEGER primary key, which would detach a row from
            # its reservation.
            if not isinstance(row["id"], str) or not row["id"]:
                return LedgerSnapshot.invalid(month_key, "malformed_usage_identity")
            settled[row["id"]] = (billing_class, amount)

        reserved = 0
        unresolved = 0
        for row in reservation_rows:
            try:
                billing_class = billing_class_from_raw(row["billing_class"])
                state = reservation_state_from_raw(row["state"])
                amount = _exact_nano(row["reserved_nano_usd"], "reserved_nano_usd")
            except BillingIntegrityError:
                return LedgerSnapshot.invalid(month_key, "malformed_reservation_row")
            if billing_class is BillingClass.METERED_MONETARY:
                if amount <= 0:
                    return LedgerSnapshot.invalid(
                        month_key, "metered_reservation_without_amount"
                    )
            elif billing_class is BillingClass.UNKNOWN or amount != 0:
                return LedgerSnapshot.invalid(
                    month_key, "impossible_reservation_route"
                )
            usage_id = row["usage_id"]
            if not isinstance(usage_id, str) or not usage_id:
                return LedgerSnapshot.invalid(month_key, "malformed_reservation_row")
            if state is ReservationState.RECONCILED:
                # A reconciled reservation must have its immutable authoritative
                # usage row, and the two must agree on the money.
                try:
                    actual = _exact_nano(row["actual_nano_usd"], "actual_nano_usd")
                except BillingIntegrityError:
                    return LedgerSnapshot.invalid(
                        month_key, "reconciled_without_actual"
                    )
                recorded = settled.get(usage_id)
                if recorded is None:
                    return LedgerSnapshot.invalid(
                        month_key, "reconciled_without_usage_row"
                    )
                recorded_class, recorded_amount = recorded
                if recorded_class is not billing_class:
                    return LedgerSnapshot.invalid(month_key, "reconciled_route_conflict")
                expected = actual if billing_class is BillingClass.METERED_MONETARY else None
                if recorded_amount != expected:
                    return LedgerSnapshot.invalid(month_key, "reconciled_amount_conflict")
                continue
            # ACTIVE / UNRESOLVED: nothing may be settled for it yet.
            if row["actual_nano_usd"] is not None or usage_id in settled:
                return LedgerSnapshot.invalid(month_key, "unsettled_reservation_conflict")
            reserved += amount
            if state is ReservationState.UNRESOLVED:
                unresolved += amount

        if measured > MAX_NANO_USD or measured + reserved > MAX_NANO_USD:
            return LedgerSnapshot.invalid(month_key, "ledger_exposure_out_of_range")
        return LedgerSnapshot(
            month=month_key,
            measured_nano_usd=measured,
            reserved_nano_usd=reserved,
            unresolved_nano_usd=unresolved,
        )

    def ledger_snapshot(self, month: str | None = None) -> LedgerSnapshot:
        """The authoritative monetary position used for every paid decision."""
        month_key = month or ledger_month()
        with self._connection() as connection:
            return self._snapshot_from(connection, month_key)

    def monthly_monetary_cost(self, month: str | None = None) -> float:
        """Measured, genuinely metered spend of the month (compatibility view).

        Derived from the authoritative snapshot rather than a bare float SUM, so
        a malformed row fails closed here too instead of quietly reducing the
        total. Enforcement uses ``ledger_snapshot`` because measured spend alone
        ignores exposure already reserved by a claimed execution.
        """
        snapshot = self.ledger_snapshot(month)
        if not snapshot.valid:
            raise BillingIntegrityError(f"ledger invalid: {snapshot.reason}")
        return float(snapshot.measured_usd)

    @staticmethod
    def _usage_from_row(row: sqlite3.Row) -> UsageRecord:
        return UsageRecord(
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

    def usage_records(self) -> list[UsageRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_ledger ORDER BY recorded_at, id"
            ).fetchall()
        return [self._usage_from_row(row) for row in rows]

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
                validate_usage_record(usage)
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

    def save_project(self, project: ProjectRecord) -> None:
        """Create one project identity. Explicit and INSERT-only: an existing
        id is refused loudly, never silently renamed or reactivated."""
        with self._connection(write=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO projects (id, name, active, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        project.id,
                        project.name,
                        int(bool(project.active)),
                        project.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Project already exists: {project.id}") from exc

    def load_project(self, project_id: str) -> ProjectRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, name, active, created_at FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return ProjectRecord(
                id=row["id"],
                name=row["name"],
                active=bool(row["active"]),
                created_at=str(row["created_at"]),
            )
        except (TypeError, ValueError):
            # A malformed persisted project fails closed as missing: it can
            # never become a binding or retrieval scope.
            return None

    def list_projects(self) -> list[ProjectRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, name, active, created_at FROM projects "
                "ORDER BY created_at, id"
            ).fetchall()
        projects: list[ProjectRecord] = []
        for row in rows:
            try:
                projects.append(
                    ProjectRecord(
                        id=row["id"],
                        name=row["name"],
                        active=bool(row["active"]),
                        created_at=str(row["created_at"]),
                    )
                )
            except (TypeError, ValueError):
                continue
        return projects

    def save_memory_item(
        self, item: ProjectMemoryItem, *, supersedes: str | None = None
    ) -> ProjectMemoryItem:
        """Persist one explicit memory item (INSERT-only + supersede).

        The write boundary applies exactly the hygiene the persistence
        contract needs and nothing more: newline normalization, the hard text
        ceiling (REJECT, never truncate - memory text is semantic user data,
        not diagnostics), and the canonical credential/path redaction
        helpers. Content columns are never rewritten; ``supersedes`` only
        flips the referenced item's lifecycle flags, in the same transaction.
        """
        text = normalize_retrieved_text(item.text)
        if not text:
            raise ValueError("memory text must be a non-blank string")
        if len(text) > PROJECT_MEMORY_TEXT_CAP:
            raise ValueError(
                f"memory text is longer than {PROJECT_MEMORY_TEXT_CAP} characters"
            )
        text, _ = redact_credentials(text)
        text = redact_local_paths(text)
        stored = replace(item, text=text, active=True, superseded_by=None)
        with self._connection(write=True) as connection:
            project_row = connection.execute(
                "SELECT active FROM projects WHERE id = ?",
                (stored.project_id,),
            ).fetchone()
            if project_row is None or not project_row["active"]:
                raise ValueError(
                    f"Unknown or inactive project: {stored.project_id}"
                )
            if supersedes is not None:
                old_row = connection.execute(
                    "SELECT project_id, active FROM project_memory_items "
                    "WHERE id = ?",
                    (supersedes,),
                ).fetchone()
                if (
                    old_row is None
                    or old_row["project_id"] != stored.project_id
                    or not old_row["active"]
                ):
                    raise ValueError(
                        "supersedes must reference an active memory item "
                        "of the same project"
                    )
            try:
                connection.execute(
                    """
                    INSERT INTO project_memory_items
                        (id, project_id, kind, text, classification, active,
                         superseded_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?)
                    """,
                    (
                        stored.id,
                        stored.project_id,
                        stored.kind.value,
                        stored.text,
                        stored.classification.value,
                        stored.created_at,
                        stored.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Memory item already exists: {stored.id}"
                ) from exc
            if supersedes is not None:
                connection.execute(
                    """
                    UPDATE project_memory_items
                       SET active = 0, superseded_by = ?, updated_at = ?
                     WHERE id = ? AND active = 1
                    """,
                    (stored.id, utc_now(), supersedes),
                )
        return stored

    def list_memory_items(
        self, project_id: str, *, include_inactive: bool = False
    ) -> list[ProjectMemoryItem]:
        sql = (
            "SELECT id, project_id, kind, text, classification, active, "
            "superseded_by, created_at, updated_at FROM project_memory_items "
            "WHERE project_id = ?"
        )
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY created_at, id"
        with self._connection() as connection:
            rows = connection.execute(sql, (project_id,)).fetchall()
        items: list[ProjectMemoryItem] = []
        for row in rows:
            item = self._memory_item_from_row(row)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _memory_item_from_row(row: sqlite3.Row) -> ProjectMemoryItem | None:
        """Rebuild one persisted item defensively.

        An unknown kind, blank/oversized text or broken identity makes the
        row unusable and it is skipped (fail closed); an unreadable
        classification alone degrades to SECRET inside ``from_dict`` so the
        item stays listable but never egress-eligible.
        """
        try:
            return ProjectMemoryItem.from_dict(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "kind": row["kind"],
                    "text": row["text"],
                    "classification": row["classification"],
                    "active": bool(row["active"]),
                    "superseded_by": row["superseded_by"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        except (TypeError, KeyError, ValueError):
            return None

    def retrieve_context(self, query: MemoryRetrievalQuery) -> RetrievedContext:
        """Bounded, deterministic retrieval over same-session decisions.

        Only the newest ``RETRIEVAL_CANDIDATE_WINDOW`` decisions of the query
        session are examined, the current task is excluded, and every
        classification is recovered from the persisted source task, failing
        closed as SECRET when that recovery is impossible.

        With a project scope (M1) the newest active
        ``PROJECT_MEMORY_CANDIDATE_WINDOW`` items of exactly that project are
        additionally examined; they compete inside the same absolute item and
        character ceilings and the same eligibility ladder. Without a project
        scope the behaviour is byte-identical to the project-less pipeline.
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
            item_rows: list[sqlite3.Row] = []
            if query.project_id is not None:
                item_rows = connection.execute(
                    "SELECT id, project_id, kind, text, classification, "
                    "active, superseded_by, created_at, updated_at "
                    "FROM project_memory_items "
                    "WHERE project_id = ? AND active = 1 "
                    "ORDER BY created_at DESC, id ASC LIMIT ?",
                    (query.project_id, PROJECT_MEMORY_CANDIDATE_WINDOW),
                ).fetchall()
        candidates: list[tuple[DecisionRecord, DataClassification]] = []
        for row in rows:
            try:
                record = DecisionRecord.from_dict(json.loads(row[0]))
            except (TypeError, KeyError, ValueError):
                # An unreadable decision row cannot be scored or shown.
                continue
            candidates.append((record, self._classification_from_state(row[1])))
        project_candidates: list[ProjectMemoryItem] = []
        for row in item_rows:
            item = self._memory_item_from_row(row)
            if item is not None:
                project_candidates.append(item)
        return build_retrieved_context(
            query,
            current_classification,
            candidates,
            project_candidates=tuple(project_candidates),
        )

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
