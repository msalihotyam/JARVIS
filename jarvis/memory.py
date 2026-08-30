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
    PROJECT_DECISION_CANDIDATE_WINDOW,
    PROJECT_MEMORY_CANDIDATE_WINDOW,
    PROJECT_MEMORY_TEXT_CAP,
    RETRIEVAL_CANDIDATE_WINDOW,
    ActionScope,
    AttemptState,
    BillingClaim,
    CHAIN_NO_PREDECESSOR,
    LOCAL_READ_CAPABILITY,
    LOCAL_WRITE_CAPABILITY,
    ChainState,
    ChainStatus,
    ChainTaskMode,
    LocalReadSnapshot,
    ModelAnswer,
    BillingClass,
    BillingIntegrityError,
    BillingReconciliation,
    DataClassification,
    DecisionRecord,
    ErrorCode,
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
    ProgressIssueKind,
    ProjectRecord,
    ReconciliationOutcome,
    ReconciliationResult,
    CancellationOutcome,
    ReservationState,
    StructuredError,
    RetrievedContext,
    SessionState,
    ApprovalSummary,
    AttemptRecovery,
    AttemptRecoveryOutcome,
    AttemptSummary,
    ProgressSummary,
    VISIBILITY_DECODE_ERRORS,
    VISIBILITY_MAX_LIMIT,
    VISIBILITY_SESSION_KEYS,
    VISIBILITY_TASK_KEYS,
    canonical_utc_timestamp,
    next_loop_stage,
    normalize_session_next_step,
    visibility_text,
    SessionSummary,
    Task,
    TaskEvent,
    TaskStatus,
    TaskSummary,
    UsageRecord,
    billing_class_from_raw,
    build_retrieved_context,
    canonical_usd,
    chain_binding_refusal,
    local_read_snapshot_refusal,
    chain_id_refusal,
    chain_state_refusal,
    chain_task_refusal,
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
    result_to_dict,
    terminal_failure_result,
    require_monetary_state,
    reservation_state_from_raw,
    usd_from_nano,
    utc_now,
)
from jarvis.lifecycle import is_terminal
from jarvis.progress import NON_FAILURE_ISSUE_KINDS, clear_issues, set_issue
from jarvis.planning import build_task_plan


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


class _CancellationLost(Exception):
    """Roll back a cancellation whose guarded task write lost arbitration.

    Never escapes storage: ``cancel_task`` turns it back into a ``"lost"``
    receipt. It exists so the session pointer, the checkpoint and the event
    cannot survive a task write that did not happen - a partial cancellation
    is a worse lie than a refused one. It carries the rows that WERE rebound,
    so the refusal can still be reported against real state rather than
    against nothing.
    """

    def __init__(self, session: SessionState, task: Task) -> None:
        super().__init__("cancellation lost ownership arbitration")
        self.session = session
        self.task = task


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

    # ----------------------------------------------------------------
    # M11. Chain ownership is STORAGE-OWNED, not session metadata.
    #
    # ``SessionState.chain`` is persisted inside ``state_json``, but it is
    # deliberately NOT ordinary last-writer-wins session state like
    # ``next_step`` or ``latest_task_id``. A caller holds a SessionState it
    # loaded at some earlier instant; by the time it saves, another caller may
    # have declared a chain. Letting that stale object write its own idea of
    # ``chain`` would silently clear or replace a chain it never saw - a lost
    # update on the one field where the product promises exactly one owner.
    #
    # So the authority is split:
    #   * creation goes through ``declare_chain_if_absent`` ONLY, which reads
    #     the incumbent, decides, and writes inside ONE ``BEGIN IMMEDIATE``
    #     transaction, so exactly one concurrent declarer can win;
    #   * every OTHER session write preserves whatever chain is persisted at
    #     that moment, read inside the caller's own write transaction, and
    #     ignores the stale object's value entirely.
    #
    # A caller-held SessionState is therefore never authority over a newer
    # persisted chain. Future chain PROGRESS mutation (advancing a cursor,
    # recording a step task id) needs its own storage-owned compare-and-set
    # operation for the same reason; generic session persistence must never
    # gain that power. No schema change: this is a rule about who may write a
    # field that already exists.
    # ----------------------------------------------------------------

    @staticmethod
    def _stored_chain(
        connection: sqlite3.Connection, session_id: str
    ) -> ChainState | None:
        """The chain persisted for this session right now, read in-transaction.

        Fail-closed in the direction that matters: a row that cannot be read
        as JSON, or whose chain cannot be read as a chain, yields the same
        typed verdict the loader would produce, never a silent ``None`` that
        would read as "this session is free to declare one".
        """
        row = connection.execute(
            "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row[0])
        except (TypeError, ValueError):
            # An unreadable row is not an empty slot. Report the typed INVALID
            # verdict so the one-chain rule keeps refusing.
            return ChainState.from_dict("unreadable")
        if not isinstance(document, dict) or document.get("chain") is None:
            return None
        return ChainState.from_dict(document["chain"])

    @staticmethod
    def _upsert_session(
        connection: sqlite3.Connection,
        session: SessionState,
        *,
        chain_authority: bool = False,
    ) -> None:
        """Write a session, preserving storage-owned chain ownership.

        ``chain_authority`` is set by exactly one caller,
        ``declare_chain_if_absent``, which has already decided the question
        inside this same transaction. Every other path - including
        ``save_session`` and every internal task/approval/billing flow - takes
        the preserving branch, so no stale snapshot can create, clear or
        replace a chain by accident.

        The incumbent is read HERE, inside the caller's write transaction, so
        a declaration cannot land between the preservation read and this
        write. The caller's object is updated to match what was persisted, so
        it stops carrying a value storage just refused to honour.
        """
        session.touch()
        payload = session.to_dict()
        if not chain_authority:
            incumbent = SQLiteMemoryStore._stored_chain(connection, session.id)
            session.chain = incumbent
            payload["chain"] = None if incumbent is None else incumbent.to_dict()
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
                SQLiteMemoryStore._json(payload),
                session.updated_at,
                session.project_id,
            ),
        )

    @staticmethod
    def _attempt_in_doubt(connection: sqlite3.Connection, task_id: str) -> bool:
        """Whether this task's latest attempt has an unresolved outcome.

        Read inside the caller's transaction, and only once the task is
        already terminal, so an ordinary write pays nothing for it.
        """
        row = connection.execute(
            "SELECT state FROM execution_attempts WHERE task_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None and row[0] in (
            AttemptState.IN_FLIGHT.value,
            AttemptState.UNKNOWN_OUTCOME.value,
        )

    @staticmethod
    def _upsert_task(
        connection: sqlite3.Connection,
        session_id: str,
        task: Task,
        *,
        force: bool = False,
        expected_owner: str | None = None,
    ) -> bool:
        """Persist a task row; True when this writer actually won.

        ``expected_owner`` adds ownership to the guard. The upsert writes
        ``session_id`` from the CALLER, so a writer holding an older view of
        who owns a task would quietly take it back - which is precisely what a
        cancellation finalization did after its task legitimately moved on
        during the connector call. Callers that cross a window where ownership
        can change pass the owner they proved, and lose the write instead of
        re-owning the row.

        Terminal immutability at the storage layer: once a row holds a terminal
        status, a stale writer carrying an older non-terminal task object can
        never demote or reopen it - the guarded upsert skips the update and
        reports ``False`` so the caller can drop its dependent writes instead of
        assuming success. Every lifecycle write goes through the guard; only the
        explicit ``save_task`` setup primitive passes ``force`` to stage state.
        """
        task.touch()
        document = task.to_dict()
        # M5 retention. The raw intended write bytes exist in the task row for
        # exactly one reason: ``resume`` rebuilds a task solely from storage,
        # so bytes that are not stored cannot be re-attested or written. Once
        # the task is authoritatively terminal that reason is gone - it can
        # never execute again - and the field is blanked HERE, in the single
        # place every task row is written, so no terminal caller has to
        # remember. Riding inside the guarded write is the point: if a stale
        # writer loses arbitration the whole transaction rolls back and the
        # bytes stay, exactly as they must.
        #
        # An attempt still IN_FLIGHT or UNKNOWN_OUTCOME is the one exception:
        # recovery may still need to re-attest the same bytes, so nothing is
        # dropped while the outcome is in doubt.
        #
        # This is LOGICAL erasure from canonical task state, and nothing
        # more. ``secure_delete`` is off, ``auto_vacuum`` is NONE and no
        # VACUUM runs, so previous bytes may remain in freed pages, the WAL
        # or filesystem slack. No forensic or physical deletion is claimed.
        if document.get("write_content") and is_terminal(task):
            if not SQLiteMemoryStore._attempt_in_doubt(connection, task.id):
                document["write_content"] = ""
        parameters = (
            task.id,
            session_id,
            SQLiteMemoryStore._json(document),
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
              -- Bound, never built: a NULL expected_owner makes this half of
              -- the guard vacuously true, so one statement serves both callers
              -- and no SQL is ever assembled from Python strings.
              AND (? IS NULL OR tasks.session_id = ?)
            """,
            (*parameters, expected_owner, expected_owner),
        )
        # 0 modified rows means the guard blocked the write: a terminal row
        # already holds authority and this writer lost.
        return cursor.rowcount == 1

    def save_session(self, session: SessionState) -> None:
        with self._connection(write=True) as connection:
            self._upsert_session(connection, session)

    @staticmethod
    def _stored_task(
        connection: sqlite3.Connection, task_id: str
    ) -> tuple[str, Task] | None:
        """One task's owning session and rebuilt state, read in-transaction.

        The authoritative row, not a caller's object. Every chain decision
        that depends on a task's status re-reads it through this, inside the
        transaction that will act on the answer.
        """
        row = connection.execute(
            "SELECT session_id, state_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return str(row[0]), Task.from_dict(json.loads(row[1]))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _task_row_exists(connection: sqlite3.Connection, task_id: str) -> bool:
        """Whether ANY row already holds this task id. Decoding is irrelevant.

        A3. ``_stored_task`` answers ``None`` for two completely different
        facts - "no such row" and "a row that cannot be read as a task" - so
        using it to decide whether an id is FREE handed a hostile or corrupted
        row's identity to a new task: the insert would then take the id over,
        re-owning and overwriting a row nobody could read, and the damage
        would look exactly like a fresh materialization. Existence is a
        question about the PRIMARY KEY, so it is asked of the primary key.
        A malformed row is never repaired here and never replaced; it simply
        blocks its own id, whoever owns it.
        """
        return connection.execute(
            "SELECT 1 FROM tasks WHERE id = ? LIMIT 1", (task_id,)
        ).fetchone() is not None

    @staticmethod
    def _attempt_blocks_advance(
        connection: sqlite3.Connection, task_id: str
    ) -> bool:
        """Whether H2 says this step's outcome is not safely settled.

        IN_FLIGHT and UNKNOWN_OUTCOME are the two states the journal refuses
        to replay. A chain must not advance past either, and must never
        fabricate a status for one: the step stays where it is.
        """
        row = connection.execute(
            "SELECT state FROM execution_attempts WHERE task_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        return str(row[0]) in (
            AttemptState.IN_FLIGHT.value,
            AttemptState.UNKNOWN_OUTCOME.value,
        )

    def _chain_claim_authority(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        task_id: str,
    ) -> bool:
        """Whether a chain still authorizes THIS task to claim execution.

        S3B P1-1. Cancellation authority and execution-claim authority are one
        arbitration, decided in the ONE transaction that grants execution
        ownership - not two decisions a runner can be paused between.

        The bug this closes: a runner materialized and bound the current step,
        paused, ``cancel_chain`` committed CANCELLED, and the stale runner
        resumed into ``begin_execution``, which asked only about the TASK. The
        task row is untouched by a cancellation - cancelling a chain
        deliberately does not rewrite the tasks it never started - so the
        claim succeeded and the connector ran. A reload on the driver side
        cannot close that: whatever it read, the cancel can still commit
        between the read and the claim. The question has to be asked where the
        answer is committed.

        Everything the answer depends on is read here, inside that
        transaction: the task's owning SESSION, the session's chain, that the
        chain is ACTIVE, that it is the same chain by id AND by declaration
        digest the task was bound to, that the bound step is the chain's
        CURRENT cursor, and that the cursor's slot still holds exactly this
        task id. A chain cancelled, replaced, redeclared or advanced since the
        binding fails one of them and the claim is LOST - before ownership,
        before any grant is consumed, before a reservation exists and before
        the connector boundary.

        The arbitration is symmetric by construction, because SQLite admits
        one writer: either the cancel commits first, and every later claim
        loses - zero attempts, zero connector calls - or the claim commits
        first, and the cancel finds a chain it may still cancel but an attempt
        it cannot un-start. Cancellation stops what has not begun; it never
        pretends an already-owned attempt never happened.

        CHAIN ASSOCIATION IS NOT THE BINDING ALONE. A task is chain-associated
        when it carries a binding OR when its authoritative owning session's
        chain NAMES that exact task id in any slot, and either alone is
        enough. The binding is a claim the task makes about itself; the slot
        is what the session authoritatively holds. Deciding association from
        the task's own field only would mean a chain step could be turned into
        an ordinary task by DELETING state from it - the persistence boundary
        already degrades a malformed binding to canonical ABSENCE, so
        corrupting one byte of it was enough to make a cancelled chain's step
        executable again through an ordinary resume. Removal must never be a
        grant, so a slot-named task with an absent or malformed binding is
        LOST here rather than waved through as ordinary.

        A task that is neither bound nor named by any slot is not a chain task
        and is unaffected: ordinary single-task authority is exactly what it
        was, including inside a session that separately owns a chain.

        An owning chain that EXISTS but cannot be validated refuses every
        classification below it. An unusable chain cannot say which tasks
        belong to it, and INVALID loads with an empty slot tuple, so asking it
        is worse than not asking: it answers "not mine" about everything.

        OWNERSHIP IS PROVEN FIRST, ahead of every one of those questions. Who
        owns the row is not a chain question and must not be answered inside
        one: an ordinary task is the DEFAULT shape, so an ownership check that
        ran only on the chain path was an ownership check that ran almost
        never. The authoritative owner in the tasks table must equal the
        session the claim is made under before this task is classified as
        ordinary, bound, unbound or chain-associated at all.
        """
        found = self._stored_task(connection, task_id)
        if found is None:
            return False
        owner, task = found
        if owner != session_id:
            # OWNERSHIP FIRST, before this task is CLASSIFIED as anything.
            # The attempt row proves only that a caller once wrote this
            # session id next to this task id; the tasks table is what
            # actually owns the row. Asking later - after the unbound
            # "ordinary task" branch had already returned - meant a session
            # could claim execution of a task belonging to another session
            # purely by having no chain binding, which is the default state
            # of every ordinary task. Nothing downstream may run before this.
            return False
        binding = task.chain_binding
        if chain_binding_refusal(binding) is not None:
            # Tampered chain state on a task is never "no binding".
            return False
        # The AUTHORITATIVE side of the association: the OWNING session's
        # chain, read in this transaction, and what it actually names. Read
        # for every task, because "is this a chain task at all" is exactly the
        # question the task's own field cannot be trusted to answer. It is
        # keyed on ``owner``, never on the caller's session, so a slot in the
        # CALLER's chain can never manufacture ownership of someone else's
        # task - by this point the two are already proven identical.
        chain = self._stored_chain(connection, owner)
        if chain is not None and chain_state_refusal(chain) is not None:
            # A1. The owner HAS a chain and it cannot be validated. An
            # unusable chain does not answer "which tasks do I own" - it
            # answers nothing at all, and INVALID in particular loads with an
            # EMPTY slot tuple, so asking it for membership returns a
            # confident "no". Combined with an erased binding that was enough
            # to reclassify a cancelled chain's step as an ordinary task and
            # execute it through a direct resume. Presence of an unusable
            # chain is therefore refused here, BEFORE any classification: the
            # store cannot prove this task is unrelated to it, and unprovable
            # is not the same fact as unrelated.
            #
            # Only PRESENCE is refused. A session with no chain at all
            # (``None``) is genuinely chainless and is untouched, as is every
            # task under a chain that validates.
            return False
        named = chain is not None and task_id in tuple(chain.step_task_ids)
        if not binding.present:
            # Ordinary ONLY when nothing authoritative names it. A named task
            # whose binding is gone has lost its proof, not its membership.
            return not named
        if chain is None:
            return False
        if chain.status is not ChainStatus.ACTIVE:
            return False
        if binding.chain_id != chain.id or binding.chain_digest != chain.digest:
            return False
        if binding.step_index != chain.cursor:
            return False
        return chain.step_task_ids[chain.cursor] == task_id

    def _chain_for_update(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        chain_id: str,
        chain_digest: str,
    ) -> tuple[ChainState | None, str | None]:
        """The stored chain, or the verdict explaining why it may not be used.

        The CAS precondition every chain-progress primitive shares: the chain
        must exist, be ACTIVE, and be the SAME chain by id AND by declaration
        digest. A stale driver holding an older digest - or one whose chain was
        cancelled, finished or replaced since it read - loses here, before any
        write.
        """
        stored = self._stored_chain(connection, session_id)
        if stored is None:
            return None, "unknown"
        if stored.status is not ChainStatus.ACTIVE:
            return None, "inactive"
        if chain_state_refusal(stored) is not None:
            return None, "inactive"
        if stored.id != chain_id or stored.digest != chain_digest:
            return None, "conflict"
        return stored, None

    def _chain_predecessor_refusal(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        chain: ChainState,
        step_index: int,
        task: Task,
    ) -> str | None:
        """Why this consumer's PREDECESSOR authority is not current, or None.

        B2. ``AgentCore`` proves the predecessor while DERIVING the consumer,
        and that proof describes the predecessor as it was at that instant.
        Between then and the write the predecessor can be tampered - its
        objective, capability, scope, routing or result rewritten - and the
        consumer would still be bound, because storage judged only the
        already-derived consumer. A derivation-time proof is not a
        materialization-time fact, so the predecessor is re-read and re-proved
        HERE, on the caller's connection, inside the transaction that binds.

        The result prerequisite each aperture depends on is part of that: a
        MODEL consumer needs its predecessor's LOCAL READ SNAPSHOT to exist
        and to be usable, exactly as a CREATE consumer needs a model answer.

        Provider-neutral by construction: every question is asked of the
        authoritative row and the frozen declaration. WHICH connector is a
        legitimate external-model producer stays a registry question and stays
        in ``AgentCore`` - storage never learns it, and the M7 resolver
        re-proves it independently before a single write byte is used, so this
        creates no second authority path.
        """
        step = chain.steps[step_index]
        if step.consumes == CHAIN_NO_PREDECESSOR:
            return None
        # The chain's own mapping is the only source of the predecessor id.
        predecessor_id = chain.step_task_ids[step.consumes]
        if not predecessor_id:
            return "the predecessor step has produced no task"
        # ... and the consumer must be routed at exactly that task, through
        # the one sanctioned aperture for what the consumer is.
        if step.capability == LOCAL_WRITE_CAPABILITY:
            declared = task.write_source_task_id
        else:
            sources = task.local_read_source_task_ids
            declared = sources[0] if type(sources) is tuple and sources else ""
        if declared != predecessor_id:
            return "the consumer is not routed at this chain's predecessor"
        found = self._stored_task(connection, predecessor_id)
        if found is None:
            return "the predecessor task could not be read"
        owner, predecessor = found
        if owner != session_id:
            return "the predecessor task belongs to another session"
        if predecessor.status is not TaskStatus.COMPLETED:
            return "the predecessor task did not complete"
        if chain_task_refusal(
            predecessor, chain, step.consumes, mode=ChainTaskMode.STORED
        ) is not None:
            return "the predecessor task is not this chain's step task"
        # The result prerequisites the aperture itself depends on.
        if step.capability == LOCAL_WRITE_CAPABILITY:
            answer = predecessor.model_answer
            if type(answer) is not ModelAnswer or not answer.present:
                return "the predecessor task carries no model answer"
            if (
                type(predecessor.selected_connector) is not str
                or not predecessor.selected_connector
            ):
                return "the predecessor task names no producer"
        elif predecessor.capability != LOCAL_READ_CAPABILITY:
            return "the predecessor task is not a local read"
        else:
            # F5. Being a COMPLETED file_read is not the prerequisite - HAVING
            # THE SNAPSHOT is. A read that completed while producing nothing
            # usable (an absent snapshot, or a row whose snapshot degraded to
            # absence on load, which is how every malformed one arrives) would
            # otherwise bind a MODEL consumer that can never run: the M8 hop
            # refuses it later, after the slot is already spent. The aperture's
            # own precondition is therefore re-proved HERE, on the
            # authoritative row, in the transaction that binds.
            snapshot = predecessor.local_read
            if type(snapshot) is not LocalReadSnapshot:
                return "the predecessor read snapshot is not the exact type"
            if not snapshot.present:
                return "the predecessor read carries no snapshot"
            # THE canonical structural contract, not a restatement of it - the
            # same function the load boundary, the execution boundary, the
            # envelope and the egress payload all call.
            if local_read_snapshot_refusal(snapshot) is not None:
                return "the predecessor read snapshot is not usable"
            authority = predecessor.scope.data_classification
            if type(authority) is not DataClassification:
                return "the predecessor read classification is malformed"
            # M8 rebinds the classification from the SOURCE TASK's own trusted
            # scope, so the object the consumer would actually receive is the
            # one judged, not whatever the stored snapshot says about itself.
            if local_read_snapshot_refusal(
                replace(snapshot, classification=authority)
            ) is not None:
                return "the predecessor read snapshot is not usable as bound"
            if snapshot.start_line != 0 or snapshot.end_line != 0:
                # M8 refuses a bounded region as cross-task context: it
                # describes only the captured lines.
                return "the predecessor read is a bounded region"
        return None

    def materialize_chain_step(
        self,
        session_id: str,
        chain_id: str,
        chain_digest: str,
        step_index: int,
        task: Task,
    ) -> tuple[str, str]:
        """Create AND bind exactly one ordinary task for one chain step.

        Returns ``(verdict, task_id)``; verdict is ``"created"``,
        ``"observed"``, ``"conflict"``, ``"inactive"`` or ``"unknown"``.

        Creation and binding are ONE transaction, and that is the whole point.
        Creating a task and then saving its id into the chain as two
        authoritative writes has a window: a crash between them leaves an
        orphan task nobody can find and a step that will be materialized
        again. Here there is no window - either both effects are durable or
        neither is.

        A step that already has a bound task is OBSERVED, never replaced. Two
        concurrent materializers therefore converge on the same one task id:
        SQLite admits one writer, the loser re-reads inside its own
        transaction, sees the winner's binding and returns it.

        The caller's ``task`` is a proposal. Its binding must name this chain,
        this digest and this index, and the step must be the chain's CURRENT
        cursor with nothing bound beyond it, or nothing is written.
        """
        with self._connection(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                return "unknown", ""
            stored, verdict = self._chain_for_update(
                connection, session_id, chain_id, chain_digest
            )
            if stored is None:
                return verdict or "conflict", ""
            if type(step_index) is not int or step_index != stored.cursor:
                # Only the cursor may be materialized. An earlier index is
                # already settled; a later one would pre-create a step the
                # chain has not reached.
                return "conflict", ""
            existing = stored.step_task_ids[step_index]
            if existing:
                # OBSERVED is a claim about a row, so the row is read and
                # re-proved. A bound id alone says nothing: the task may be
                # missing, owned by another session, or tampered since.
                found = self._stored_task(connection, existing)
                if found is None:
                    return "conflict", ""
                owner, bound_task = found
                if owner != session_id:
                    return "conflict", ""
                if chain_task_refusal(
                    bound_task, stored, step_index, mode=ChainTaskMode.STORED
                ) is not None:
                    return "conflict", ""
                return "observed", existing
            if any(stored.step_task_ids[step_index + 1 :]):
                return "conflict", ""
            # A valid binding is necessary but never sufficient. The WHOLE
            # proposal is judged against the frozen step and the
            # authoritative predecessor mapping before a row exists.
            if chain_task_refusal(
                task, stored, step_index, mode=ChainTaskMode.PROPOSAL
            ) is not None:
                return "conflict", ""
            # A2. The id is judged by the SAME rule the persisted chain
            # applies to every step task id it already holds - exact ``str``,
            # non-empty, no NUL, no surrounding whitespace, within the
            # character ceiling. Reusing ``chain_id_refusal`` rather than
            # restating it is the point: an id accepted here but refused by
            # ``chain_state_refusal`` on the next load would write a chain
            # that can never be read back, which is a self-inflicted INVALID.
            # ``isinstance`` was not enough - a ``str`` subclass with a
            # stateful ``__eq__``/``__hash__`` is not the declared type.
            if chain_id_refusal(task.id) is not None:
                return "conflict", ""
            if self._task_row_exists(connection, task.id):
                # A task id that already exists is never re-bound to a step -
                # whoever owns that row and whether or not it can be decoded.
                return "conflict", ""
            # B2. Predecessor authority is a fact about NOW, re-read and
            # re-proved on this connection, never the caller's older proof.
            if self._chain_predecessor_refusal(
                connection, session_id, stored, step_index, task
            ) is not None:
                return "conflict", ""
            # B1. The proposal is proven, but the proposal is not what will be
            # stored: persistence normalizes the scope before writing. The
            # representation that will actually exist is built here the way
            # the loader builds it, and re-proved in STORED mode BEFORE the
            # slot is bound - so nothing can be bound on pre-serialization
            # proof alone and then fail its own authority on first read.
            try:
                persisted = Task.from_dict(task.to_dict())
            except (TypeError, ValueError):
                return "conflict", ""
            if chain_task_refusal(
                persisted, stored, step_index, mode=ChainTaskMode.STORED
            ) is not None:
                return "conflict", ""
            bound = list(stored.step_task_ids)
            bound[step_index] = task.id
            candidate = replace(stored, step_task_ids=tuple(bound))
            if chain_state_refusal(candidate) is not None:
                # The chain this write WOULD leave behind is judged before
                # anything is written, so no transaction can commit a chain
                # that the next load would refuse. Nothing has been inserted
                # at this point, so returning is the whole rollback.
                return "conflict", ""
            self._upsert_task(connection, session_id, task, force=True)
            session = SessionState.from_dict(
                json.loads(
                    connection.execute(
                        "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()[0]
                )
            )
            session.chain = candidate
            self._upsert_session(connection, session, chain_authority=True)
            return "created", task.id

    def advance_chain_step(
        self, session_id: str, chain_id: str, chain_digest: str, step_index: int
    ) -> str:
        """Move the chain according to the STORED outcome of its current step.

        Returns ``"advanced"``, ``"completed"``, ``"failed"``, ``"cancelled"``,
        ``"blocked"``, ``"conflict"``, ``"inactive"``, ``"unbound"`` or
        ``"unknown"``.

        A caller-held Task object is never proof of anything. Inside one
        transaction this re-reads the chain, re-reads the BOUND task row,
        re-proves that the row is this chain's task for this step, and only
        then reads the authoritative status. "The driver says it completed" is
        not a fact this product has ever accepted, and it is not accepted here.

        The cursor only moves forward, one step, and only off a COMPLETED
        task. A task in doubt - non-terminal, or with an IN_FLIGHT or
        UNKNOWN_OUTCOME attempt - blocks the chain in place: no advance, no
        replay, no fabricated status.
        """
        with self._connection(write=True) as connection:
            stored, verdict = self._chain_for_update(
                connection, session_id, chain_id, chain_digest
            )
            if stored is None:
                return verdict or "conflict"
            if type(step_index) is not int or step_index != stored.cursor:
                return "conflict"
            bound = stored.step_task_ids[step_index]
            if not bound:
                return "unbound"
            found = self._stored_task(connection, bound)
            if found is None:
                return "conflict"
            owner, task = found
            if owner != session_id:
                return "conflict"
            # The COMPLETE proof, not just the binding: a task whose
            # objective, scope, classification, write mode or sanctioned
            # source routing was tampered with has lost chain authority,
            # however plausible its stored status looks.
            if chain_task_refusal(
                task, stored, step_index, mode=ChainTaskMode.STORED
            ) is not None:
                return "conflict"
            if task.status is TaskStatus.FAILED:
                updated = replace(stored, status=ChainStatus.FAILED)
                outcome = "failed"
            elif task.status in (TaskStatus.CANCELLED, TaskStatus.REJECTED):
                updated = replace(stored, status=ChainStatus.CANCELLED)
                outcome = "cancelled"
            elif task.status is not TaskStatus.COMPLETED:
                return "blocked"
            elif self._attempt_blocks_advance(connection, bound):
                return "blocked"
            elif step_index == len(stored.steps) - 1:
                updated = replace(stored, status=ChainStatus.COMPLETED)
                outcome = "completed"
            else:
                updated = replace(stored, cursor=step_index + 1)
                outcome = "advanced"
            session = SessionState.from_dict(
                json.loads(
                    connection.execute(
                        "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()[0]
                )
            )
            session.chain = updated
            self._upsert_session(connection, session, chain_authority=True)
            return outcome

    def cancel_chain(self, session_id: str, chain_id: str) -> str:
        """Mark the chain CANCELLED. The only path to that status.

        Returns ``"cancelled"``, ``"terminal"``, ``"conflict"`` or
        ``"unknown"``. Cursor and every bound task id are preserved exactly:
        cancellation stops what has not started, it does not undo what has.
        Once CANCELLED the chain is no longer ACTIVE, so ``_chain_for_update``
        refuses every later materialize and advance - a cancel cannot be
        raced into a new step task.
        """
        with self._connection(write=True) as connection:
            stored = self._stored_chain(connection, session_id)
            if stored is None:
                return "unknown"
            if stored.id != chain_id:
                return "conflict"
            if stored.status is not ChainStatus.ACTIVE:
                return "terminal"
            session = SessionState.from_dict(
                json.loads(
                    connection.execute(
                        "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()[0]
                )
            )
            session.chain = replace(stored, status=ChainStatus.CANCELLED)
            self._upsert_session(connection, session, chain_authority=True)
            return "cancelled"

    def declare_chain_if_absent(self, session_id: str, chain: ChainState) -> str:
        """Atomically claim the session's one chain slot. THE only creator.

        Returns ``"declared"``, ``"occupied"`` or ``"unknown"`` - a
        deterministic verdict in the shape ``cancel_task`` already uses, so a
        loser is told what happened rather than being handed a silent success.

        The authoritative read, the decision and the write all happen inside
        one ``BEGIN IMMEDIATE`` transaction. SQLite admits one writer at a
        time, so two concurrent declarers cannot both observe an empty slot:
        the second blocks on the write lock, then reads the winner's chain and
        loses. Correctness comes from the database, never from a Python lock,
        which would protect only one process.

        Occupied means occupied in EVERY state. ACTIVE, COMPLETED, FAILED,
        CANCELLED and INVALID all count, because ``None`` is the one spelling
        of absence and a finished or corrupted chain is not it. What may
        replace a terminal chain is a policy this product has not decided, so
        it fails closed here rather than guessing.
        """
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return "unknown"
            if self._stored_chain(connection, session_id) is not None:
                return "occupied"
            session = SessionState.from_dict(json.loads(row[0]))
            session.chain = chain
            self._upsert_session(connection, session, chain_authority=True)
            return "declared"

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
        self, session_id: str, task_id: str
    ) -> CancellationOutcome:
        """Atomically arbitrate cancellation against the execution attempt in
        ONE transaction (P1-B).

        THE REQUESTED ``task_id`` IS THE ONLY ROW SELECTOR. Callers used to
        hand in a decoded ``Task`` and storage used ITS id to write and ITS
        status and stage to decide, so a persisted document could:

        * name another task and get that one cancelled instead - the requested
          task left untouched, and the wrong task's row rewritten;
        * carry a stage the row never had, and see that stage persisted into
          the authoritative column by the cancellation write;
        * carry a terminal status and be reported as terminal.

        None of that is expressible now: the caller passes two ids, and the
        task and session are rebound to their own rows here, inside the write
        transaction, before anything is mutated. The attempt is resolved by the
        requested ``task_id`` too, never by a decoded one.

        Returns a bounded :class:`CancellationOutcome` carrying the verdict and
        the rebound rows. ``"unknown_session"`` and ``"not_found"`` cover every
        identity, ownership and shape refusal alike - which row a document
        really describes is not something a refusal may disclose - and each
        returns having written nothing and raised nothing.

        S2A CLOSEOUT. There is deliberately no ``connector_cancelled``
        parameter. It existed to let a caller report that a connector had
        confirmed the stop, and S2A no longer signals connectors at all, so no
        caller can hold that evidence. Accepting the boolean anyway would have
        let anyone terminalize a running task as CANCELLED by passing ``True``
        - an assertion about the outside world that storage cannot check, about
        work that may still be happening. An authoritative IN_FLIGHT attempt
        therefore always yields ``"cancelling"``, and the execution owner stays
        the only authority that can terminalize it.

        S2A. ``UNKNOWN_OUTCOME`` used to fall into the ``not in_flight``
        branch and terminalize the task as CANCELLED. That is a false
        statement about the world: the attempt reached the connector boundary
        and its effect may already have happened, so "cancelled" would tell
        the operator nothing was done when nobody knows that. Such a task is
        refused here and routed to the explicit abandon recovery instead,
        which records the honest outcome - FAILED, with the uncertainty and
        its monetary exposure preserved.

        Ownership is re-proved and the task write is GUARDED on it, so a
        finalization whose task changed owner returns ``"lost"`` having written
        nothing rather than taking the task back from whoever holds it now.
        """
        try:
            return self._cancel_task_atomically(session_id, task_id)
        except _CancellationLost as lost:
            return CancellationOutcome("lost", lost.session, lost.task)

    def _cancel_task_atomically(
        self, session_id: str, task_id: str
    ) -> CancellationOutcome:
        with self._connection(write=True) as connection:
            # Both rows first, each rebound to its own identity. The session is
            # proved before the task because the task proof is stated in terms
            # of it, and because a cancellation writes the session back.
            session = self._authoritative_session_row(connection, session_id)
            if session is None:
                return CancellationOutcome("unknown_session")
            task = self._authoritative_task_row(connection, session_id, task_id)
            if task is None:
                # ONE verdict for a missing row, a foreign row, a malformed
                # document and a document whose id, status or stage disagrees
                # with its columns. Distinguishing them would leak which of
                # those it was, and which other session owns something is not
                # a fact a refusal may reveal.
                return CancellationOutcome("not_found")
            # The status is the COLUMN's, proved equal to the document's above,
            # so a forged terminal document cannot short-circuit anything.
            if task.status.value in _TERMINAL_STATUS_VALUES:
                return CancellationOutcome("terminal", session, task)
            attempt = connection.execute(
                "SELECT state FROM execution_attempts WHERE task_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if (
                attempt is not None
                and attempt[0] == AttemptState.UNKNOWN_OUTCOME.value
            ):
                # S2A: refused BEFORE any write. The task, the attempt, the
                # reservation and the chain are all left exactly as they are.
                return CancellationOutcome("in_doubt", session, task)
            # Only the rebound objects are mutated from here. A caller's stale
            # lifecycle and progress fields never reach a row, because the
            # caller no longer supplies any.
            task.cancellation_requested = True
            session.latest_task_id = task_id
            clear_issues(task, *NON_FAILURE_ISSUE_KINDS)
            set_issue(task, ProgressIssueKind.CANCELLATION_PENDING)
            if attempt is not None and attempt[0] == AttemptState.IN_FLIGHT.value:
                # FINDING 2. An authoritative IN_FLIGHT attempt is never
                # terminalized here, by anyone. Storage has no evidence the
                # execution stopped - S2A signals no connector, so no such
                # evidence exists anywhere - and writing CANCELLED without it
                # tells the operator the work is over while it may still be
                # running. The intent is durable, the verdict is not final,
                # and the execution owner concludes it.
                task.status = TaskStatus.CANCEL_REQUESTED
                verdict = "cancelling"
            else:
                # Nothing was ever claimed, so there is nothing running to
                # stop: this cancellation IS final and honest.
                task.status = TaskStatus.CANCELLED
                task.stage = LoopStage.CANCELLED
                session.active_task_id = None
                task.progress = replace(task.progress, open_issues=())
                verdict = "cancelled"
            self._upsert_session(connection, session)
            if not self._upsert_task(
                connection, session_id, task, expected_owner=session_id
            ):
                # The guard refused. Roll the whole transaction back rather
                # than leaving a session pointer and an event describing a
                # cancellation that was never written to the task.
                raise _CancellationLost(session, task)
            self._write_checkpoint_and_event(
                connection,
                session,
                task,
                "cancellation_requested",
                {"connector_cancelled": False},
            )
        return CancellationOutcome(verdict, session, task)

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
                # Window A2 (S3B P1-1): for a CHAIN task, the chain's own
                # authority is arbitrated against this claim HERE, in the same
                # transaction that grants ownership, so a cancellation that
                # commits before the claim can never be executed past.
                if not self._chain_claim_authority(connection, session_id, task_id):
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

    @staticmethod
    def _authoritative_task_row(
        connection: sqlite3.Connection, session_id: str, task_id: str
    ) -> Task | None:
        """The task, REBOUND to its authoritative row, or ``None``.

        THE definition of "this task, as storage knows it", shared by every
        mutating path - abandon recovery and ordinary cancellation both. One
        definition on purpose: two would drift, and the weaker one would become
        the way in.

        A decoded document is not a description of the row it was found in, and
        ``task_id`` here is always the id the CALLER asked about, never one
        read out of a document. Every field a mutation will write is tied back
        to the columns storage actually acts on:

        * the row exists and its ``session_id`` column is the caller's
        * the document is an EXACT mapping of the canonical shape - not a
          fragment ``from_dict`` would happily complete with defaults
        * the decoded id is THIS row's id, so a forged identity can never be
          written out as a NEW task
        * the decoded status and stage are the columns' own values, so a
          forged lifecycle cannot ride along into the terminal write

        Returning ``None`` is the single non-disclosing refusal for all of
        them: which row a document really describes is not something a
        refusal may reveal.
        """
        row = connection.execute(
            "SELECT session_id, status, stage, state_json FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["session_id"] != session_id:
            return None
        try:
            document = json.loads(row["state_json"])
        except VISIBILITY_DECODE_ERRORS:
            return None
        # The canonical document shape, shared with the read surface: one
        # definition of "a complete persisted task", not two that can drift.
        if type(document) is not dict or not VISIBILITY_TASK_KEYS <= set(document):
            return None
        try:
            task = Task.from_dict(document)
        except VISIBILITY_DECODE_ERRORS:
            return None
        if task.id != task_id:
            return None
        if task.status.value != row["status"] or task.stage.value != row["stage"]:
            return None
        return task

    @staticmethod
    def _authoritative_session_row(
        connection: sqlite3.Connection, session_id: str
    ) -> SessionState | None:
        """The session, REBOUND to its authoritative row, or ``None``.

        F2. The same rule, and for a sharper reason: recovery writes the
        session back. A document carrying somebody else's id used to be
        written out under THAT id, so a forged field mutated a session the
        operator never named. The decoded identity must be this row's own.
        """
        row = connection.execute(
            "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row[0])
        except VISIBILITY_DECODE_ERRORS:
            return None
        if type(document) is not dict or not VISIBILITY_SESSION_KEYS <= set(document):
            return None
        try:
            session = SessionState.from_dict(document)
        except VISIBILITY_DECODE_ERRORS:
            return None
        if session.id != session_id:
            return None
        return session

    def abandon_unknown_attempt(
        self,
        session_id: str,
        task_id: str,
        attempt_id: str,
        recovery: AttemptRecovery,
    ) -> AttemptRecoveryOutcome:
        """Accept that an UNKNOWN-outcome attempt will never resolve.

        THE only exit from the one H2 state that has none. Everything happens
        in a single ``BEGIN IMMEDIATE`` transaction: the proofs are read and
        the writes committed together, so a concurrent conclusion, a
        cancellation or a second operator cannot interleave between them.

        The task write goes through the ORDINARY guarded upsert, so the
        terminal-immutability rule that protects every other lifecycle write
        protects this one too - and that guard is exactly what makes a double
        recovery, or a race against a real conclusion, resolve to a single
        winner rather than to two terminal statements about one task.

        Nothing here reaches a connector, returns the attempt to a claimable
        state, invents an outcome or touches money except to make an ACTIVE
        reservation UNRESOLVED - which is not a release: the exposure keeps
        counting, because the outcome remains unknown.
        """
        if type(recovery) is not AttemptRecovery:
            # A closed vocabulary, enforced rather than assumed.
            return AttemptRecoveryOutcome.NOT_RECOVERABLE
        with self._connection(write=True) as connection:
            attempt = connection.execute(
                "SELECT session_id, task_id, state FROM execution_attempts "
                "WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            # Identity first, and deliberately ONE verdict for every way it
            # can fail: which other session or task owns an attempt is not
            # something a refusal may disclose.
            if (
                attempt is None
                or attempt["session_id"] != session_id
                or attempt["task_id"] != task_id
            ):
                return AttemptRecoveryOutcome.UNKNOWN
            # F2. The task, REBOUND to its own row: identity, ownership and
            # lifecycle all proven against the columns before anything is
            # written from the document.
            task = self._authoritative_task_row(connection, session_id, task_id)
            if task is None:
                return AttemptRecoveryOutcome.UNKNOWN
            if attempt["state"] != AttemptState.UNKNOWN_OUTCOME.value:
                # PREPARED is still legitimately claimable, IN_FLIGHT may
                # still have a live owner holding connector authority, and
                # FINISHED already has its answer. None of them is this
                # operator's decision to make.
                return AttemptRecoveryOutcome.NOT_RECOVERABLE
            if task.status.value in _TERMINAL_STATUS_VALUES:
                return AttemptRecoveryOutcome.TERMINAL
            # F2. And the session, rebound the same way: recovery writes it
            # back, so a document naming another session must never decide
            # which row that write lands on.
            session = self._authoritative_session_row(connection, session_id)
            if session is None:
                return AttemptRecoveryOutcome.UNKNOWN
            payload = {
                "recovery": recovery.value,
                "attempt_id": attempt_id,
                # The attempt KEEPS this state; it is recorded so the audit
                # trail says what was abandoned, not what it became.
                "attempt_state": AttemptState.UNKNOWN_OUTCOME.value,
                "error_code": ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value,
            }
            task.status = TaskStatus.FAILED
            task.stage = LoopStage.FAILED
            task.cancellation_requested = False
            # F3. The canonical typed failure, made DURABLE. Without it the
            # row is FAILED with no recorded reason, and a restart reports a
            # terminal task that cannot say why - which is precisely the
            # question an operator restarts to ask. Any stale snapshot from a
            # partial run is replaced: the authoritative outcome of this task
            # is now the abandonment, not whatever it was mid-flight.
            task.result_snapshot = result_to_dict(
                terminal_failure_result(
                    StructuredError(
                        ErrorCode.EXECUTION_UNKNOWN_OUTCOME,
                        "A prior execution attempt is in doubt and was "
                        "explicitly abandoned; it was never replayed.",
                        retryable=False,
                    )
                )
            )
            clear_issues(task, *NON_FAILURE_ISSUE_KINDS)
            set_issue(
                task,
                ProgressIssueKind.FAILURE,
                ErrorCode.EXECUTION_UNKNOWN_OUTCOME,
                False,
            )
            session.active_task_id = None
            session.latest_task_id = task.id
            if not self._upsert_task(connection, session_id, task):
                # The guarded upsert refused: a terminal writer won this race.
                # Nothing else in this transaction has written yet.
                return AttemptRecoveryOutcome.TERMINAL
            self._upsert_session(connection, session)
            self._write_checkpoint_and_event(
                connection, session, task, "execution_abandoned", payload
            )
            # H3. UNRESOLVED, never released: the money stays visible because
            # the outcome stays unknown. ACTIVE is the only source state, so a
            # RECONCILED reservation is never reopened and an already
            # UNRESOLVED one is left exactly as it is.
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
        return AttemptRecoveryOutcome.RECOVERED

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
        decision: DecisionRecord | None,
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

        ``decision`` is optional because not every terminal task HAS a
        decision to record. A decision is trusted inherited history: later
        work in the session retrieves it as context. A local file read
        produces evidence, not a judgement, so it finalizes with no decision
        at all rather than with an empty or sentinel one - the row is never
        written, so there is nothing for retrieval to inherit and nothing to
        clean up afterwards. Everything else about the transaction is
        unchanged: the terminal task write, the session update, the usage
        bookkeeping, the terminal event and the guarded arbitration all still
        happen together or not at all.

        The usage row is keyed by its own id and was already appended by the
        execution path, so re-writing it here is idempotent and never
        duplicates legitimately recorded usage.
        """
        try:
            with self._connection(write=True) as connection:
                self._upsert_session(connection, session)
                if not self._upsert_task(connection, session.id, task):
                    raise _StaleWriterLost()
                if decision is not None:
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

        With a project scope (M2) the newest
        ``PROJECT_DECISION_CANDIDATE_WINDOW`` decisions recorded by the SAME
        project's OTHER sessions are additionally examined. The querying
        session is excluded, so the two decision windows are disjoint and a
        decision can never appear twice. Every cross-session decision's
        classification is recovered from its own persisted source task,
        failing closed as SECRET; a decision whose session is missing or not
        bound to exactly this project never enters the window. No source has
        a sub-quota: all candidates compete in the one deterministic ordering
        under the unchanged absolute ceilings.
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
            cross_rows: list[sqlite3.Row] = []
            if query.project_id is not None:
                item_rows = connection.execute(
                    "SELECT id, project_id, kind, text, classification, "
                    "active, superseded_by, created_at, updated_at "
                    "FROM project_memory_items "
                    "WHERE project_id = ? AND active = 1 "
                    "ORDER BY created_at DESC, id ASC LIMIT ?",
                    (query.project_id, PROJECT_MEMORY_CANDIDATE_WINDOW),
                ).fetchall()
                # M2: decisions of the same project's OTHER sessions. The
                # inner JOIN both scopes to exactly this project and drops
                # orphaned decisions whose session row is missing; excluding
                # the querying session keeps the windows disjoint.
                cross_sql = (
                    "SELECT d.decision_json, t.state_json "
                    "FROM decision_log AS d "
                    "JOIN sessions AS s ON s.id = d.session_id "
                    "LEFT JOIN tasks AS t ON t.id = d.task_id "
                    "WHERE s.project_id = ? AND d.session_id != ?"
                )
                cross_parameters: list[Any] = [query.project_id, query.session_id]
                if query.exclude_task_id is not None:
                    cross_sql += " AND d.task_id != ?"
                    cross_parameters.append(query.exclude_task_id)
                cross_sql += " ORDER BY d.created_at DESC, d.id ASC LIMIT ?"
                cross_parameters.append(PROJECT_DECISION_CANDIDATE_WINDOW)
                cross_rows = connection.execute(
                    cross_sql, cross_parameters
                ).fetchall()
        candidates: list[tuple[DecisionRecord, DataClassification]] = []
        for row in list(rows) + list(cross_rows):
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

    # -- M12 S1: read-only operator visibility -----------------------------
    #
    # Pure reads. No ``write=True`` connection is opened, so none of them can
    # create, repair or touch a row even by accident: a listing that healed
    # what it looked at would be a write path wearing a display command's
    # name. Every projection is built from real COLUMNS first and enriched
    # from the persisted document only if that document can be rebuilt, which
    # is what makes a corrupted row reportable instead of fatal.

    @staticmethod
    def _visibility_rows(limit: Any) -> int:
        """How many rows a listing may ask storage for. THE bound.

        F4. Enforced HERE, not by the caller. The command layer separately
        refuses a user-supplied limit above the ceiling - that is input
        validation - but the ceiling itself has to hold for every caller,
        including a future command, a script or another front end that calls
        the store directly. A store that trusted its argument would let any
        such caller turn a display listing into an unbounded export.

        Exact positive ``int`` only: ``bool`` is an ``int`` and ``True`` must
        never silently become "one row". Anything else yields 0, which the
        callers below turn into an empty listing rather than a guess.
        """
        if type(limit) is not int or isinstance(limit, bool) or limit <= 0:
            return 0
        return min(limit, VISIBILITY_MAX_LIMIT)

    @staticmethod
    def _visibility_document(
        raw: Any, required: frozenset[str] | None = None
    ) -> dict[str, Any] | None:
        """One persisted document as an EXACT mapping of the right SHAPE.

        F2. Two separate proofs, both before any loader sees the value.

        The ROOT: a persisted ``"null"``, ``"123"`` or ``"[]"`` is perfectly
        valid JSON, so ``json.loads`` succeeds and the loader then raises
        ``AttributeError`` out of a display command.

        The SHAPE: ``from_dict`` is defensive by design - it fills absent
        fields with typed defaults so a partially-written row still loads.
        That is right for the execution path, which needs to keep going, and
        wrong here: a fragment like ``{"id": ...}`` would rebuild into a
        complete-looking object and be REPORTED as this session's state.
        Requiring the identity and lifecycle keys means a display command
        describes rows that exist, not defaults it invented.
        """
        try:
            document = json.loads(raw)
        except VISIBILITY_DECODE_ERRORS:
            return None
        if type(document) is not dict:
            return None
        if required is not None and not required <= set(document):
            return None
        return document

    def list_sessions(self, limit: int) -> list[SessionSummary]:
        """The most recently touched sessions, newest first.

        Ordered by ``updated_at`` descending with an ASCENDING id tie-break,
        so two rows written in the same instant still have exactly one order
        and repeated calls agree. The row bound is the STORE'S, applied to
        the query itself, so no caller can widen it.

        The task count is a single grouped COUNT, not a per-row query, so a
        listing stays one round trip whatever it returns.
        """
        bound = self._visibility_rows(limit)
        if bound == 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS id,
                       s.updated_at AS updated_at,
                       s.project_id AS project_id,
                       s.state_json AS state_json,
                       (SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id)
                           AS task_count
                FROM sessions AS s
                ORDER BY s.updated_at DESC, s.id ASC
                LIMIT ?
                """,
                (bound,),
            ).fetchall()
        return [self._session_summary(row) for row in rows]

    @staticmethod
    def _session_summary(row: sqlite3.Row) -> SessionSummary:
        """One session row as a display projection, whatever shape it is in."""
        identity = str(row["id"])
        updated_at = str(row["updated_at"])
        count = int(row["task_count"])
        unreadable = SessionSummary(
            id=identity, updated_at=updated_at, readable=False, task_count=count
        )
        document = SQLiteMemoryStore._visibility_document(
            row["state_json"], VISIBILITY_SESSION_KEYS
        )
        if document is None:
            # Unreadable, and said so. The columns are still true.
            return unreadable
        try:
            session = SessionState.from_dict(document)
        except VISIBILITY_DECODE_ERRORS:
            return unreadable
        # F2. The document must AGREE with the authoritative row. A blank or
        # truncated document rebuilds into a perfectly well-formed default
        # SessionState carrying a freshly minted id - which would be reported
        # as readable state this session never had. Corruption is never
        # defaulted into readability.
        if session.id != identity:
            return unreadable
        chain = session.chain
        return SessionSummary(
            id=identity,
            updated_at=updated_at,
            readable=True,
            # The COLUMN is the authoritative project binding, not the
            # document's copy of it.
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            active_task_id=session.active_task_id,
            latest_task_id=session.latest_task_id,
            task_count=count,
            # Through the SAME canonical rule the loader applies, so the
            # visibility copy can never be looser than the persisted one.
            next_step=(
                None
                if session.next_step is None
                else normalize_session_next_step(session.next_step)
            ),
            chain_id=None if chain is None else chain.id,
            chain_digest=None if chain is None else chain.digest,
            chain_status=None if chain is None else chain.status.value,
            chain_cursor=None if chain is None else chain.cursor,
            chain_step_count=None if chain is None else len(chain.steps),
            # The ONE canonical verdict, asked here exactly as every other
            # consumer must ask it - never restated.
            chain_usable=(
                None if chain is None else chain_state_refusal(chain) is None
            ),
        )

    def list_tasks(self, session_id: str, limit: int) -> list[TaskSummary]:
        """The tasks of EXACTLY this session, newest first.

        Ownership is a WHERE clause on the authoritative column, so a caller
        cannot widen a listing into another session's tasks by asking louder.
        An unknown session simply has no tasks; distinguishing "no session"
        from "no tasks" is the command layer's question, and it asks storage
        for the session itself.
        """
        bound = self._visibility_rows(limit)
        if bound == 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, status, stage, updated_at, state_json
                FROM tasks
                WHERE session_id = ?
                ORDER BY updated_at DESC, id ASC
                LIMIT ?
                """,
                (session_id, bound),
            ).fetchall()
        return [self._task_summary(row) for row in rows]

    @staticmethod
    def _task_summary(row: sqlite3.Row) -> TaskSummary:
        """One task row as a display projection. Columns first, document second."""
        summary = TaskSummary(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            status=str(row["status"]),
            stage=str(row["stage"]),
            updated_at=str(row["updated_at"]),
            readable=False,
        )
        document = SQLiteMemoryStore._visibility_document(
            row["state_json"], VISIBILITY_TASK_KEYS
        )
        if document is None:
            return summary
        try:
            task = Task.from_dict(document)
        except VISIBILITY_DECODE_ERRORS:
            return summary
        # F2. Identity AND lifecycle must agree with the authoritative
        # columns. A document that disagrees about which task it is, or about
        # what state that task is in, is not a readable description of this
        # row - it is two contradictory answers, and the columns are the ones
        # the product actually acts on.
        if task.id != summary.id:
            return summary
        if task.status.value != summary.status or task.stage.value != summary.stage:
            return summary
        binding = task.chain_binding
        return replace(
            summary,
            readable=True,
            capability=task.capability,
            selected_connector=visibility_text(task.selected_connector),
            action=task.scope.action if type(task.scope) is ActionScope else None,
            chain_step_index=(
                binding.step_index
                if chain_binding_refusal(binding) is None and binding.present
                else None
            ),
            # P1. FROZEN HERE, from the document that was just proven, so no
            # later render has to reopen the row and race a replacement.
            progress=SQLiteMemoryStore._progress_summary(task),
        )

    @staticmethod
    def _progress_summary(task: Task) -> ProgressSummary:
        """The progress view of an ALREADY-VALIDATED task.

        Called only after identity, status and stage have been proved against
        the authoritative columns, so everything captured here describes this
        row. The values are the ones the ordinary progress contract already
        produces: typed issue and artifact dicts, which have no free-form
        message field, and the CANONICAL REBUILT plan rather than the
        persisted strings.
        """
        successor = next_loop_stage(task.stage)
        snapshot = task.progress.to_dict()
        return ProgressSummary(
            current_stage=task.stage.value,
            next_stage=None if successor is None else successor.value,
            completed_stages=tuple(snapshot["completed_stages"]),
            open_issues=tuple(snapshot["open_issues"]),
            artifacts=tuple(snapshot["artifacts"]),
            plan=tuple(build_task_plan(task)) if task.plan else (),
        )

    def session_summary(self, session_id: str) -> SessionSummary | None:
        """ONE session, through the SAME projection a listing uses.

        F2. The command surface must not fall back to ``load_session`` for a
        single row: that loader is the execution path's, it rebuilds a
        fragment into a plausible default object, and a display command that
        trusted it would report a session state that never existed. Reusing
        ``_session_summary`` means a one-row view can never be looser than a
        listing of the same row, because it is literally the same rule.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.id AS id,
                       s.updated_at AS updated_at,
                       s.project_id AS project_id,
                       s.state_json AS state_json,
                       (SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id)
                           AS task_count
                FROM sessions AS s
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        return None if row is None else self._session_summary(row)

    def task_summary(self, task_id: str) -> TaskSummary | None:
        """ONE task, through the SAME projection a listing uses.

        Identity, status and stage are proved against the authoritative
        COLUMNS inside ``_task_summary``, so a forged document cannot make
        this report a lifecycle the product is not actually in.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, session_id, status, stage, updated_at, state_json
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        return None if row is None else self._task_summary(row)

    def latest_task_event(self, task_id: str) -> TaskEvent | None:
        """The TYPE and time of this task's most recent journal event.

        The payload is deliberately not selected at all: it is arbitrary
        diagnostic state, and a display command has no business widening into
        it. The event TYPE is fixed internal vocabulary and is what answers
        "what happened last".

        F3. Both surviving fields are BOUNDED before they cross. A row edited
        outside the product can hold a megabyte of text in ``event_type`` and
        four hundred digits in ``created_at``; echoing either back would make
        this command the thing that prints them. Anything that is not the
        canonical bounded shape yields an unreadable projection instead.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT event_type, created_at FROM event_log
                WHERE task_id = ? ORDER BY id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        event_type = visibility_text(row[0])
        # The SAME canonical timestamp contract every other consumer uses.
        created_at = canonical_utc_timestamp(row[1])
        if event_type is None or created_at is None:
            return TaskEvent(readable=False)
        return TaskEvent(event_type=event_type, created_at=created_at)

    def latest_attempt_summary(self, task_id: str) -> AttemptSummary | None:
        """This task's latest execution attempt, as bounded display metadata.

        F3. ``AttemptState(row)`` raises on an unrecognized persisted value,
        so the enum is resolved defensively and an unknown state decodes to
        ``None`` - which makes ``replayable`` ``None`` too. That is the whole
        point: a corrupted attempt row must never be DISPLAYED as safe to run
        again, and this projection has no way to say "yes" without a trusted
        enum member to say it from.

        The attempt is selected by this task's own id, so no other task's
        attempt can be shown here.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT state, connector, updated_at, task_id
                FROM execution_attempts
                WHERE task_id = ? ORDER BY rowid DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["task_id"]) != task_id:
            return AttemptSummary(readable=False)
        try:
            state = AttemptState(row["state"])
        except VISIBILITY_DECODE_ERRORS:
            state = None
        connector = visibility_text(row["connector"])
        updated_at = canonical_utc_timestamp(row["updated_at"])
        if state is None or connector is None or updated_at is None:
            # Partial truth is still reported: the state is the one field an
            # operator needs, and withholding a VALID one because a sibling
            # field is corrupt would hide the answer they came for.
            return AttemptSummary(
                state=state,
                connector=connector,
                updated_at=updated_at,
                readable=False,
            )
        return AttemptSummary(
            state=state, connector=connector, updated_at=updated_at
        )

    def pending_approval_summary(
        self, task_id: str, session_id: str
    ) -> ApprovalSummary | None:
        """The open approval for THIS task and session, as bounded metadata.

        F3. Only the three fields that answer a request cross. Identity and
        expiry come from AUTHORITATIVE COLUMNS, and the persisted
        ``request_json`` - which also carries the reason, the scope and the
        outbound preview - is opened for exactly ONE field, the fingerprint,
        which is then bounded like any other auxiliary token. Nothing else in
        that body is read, so a hostile or oversized one can neither raise
        nor be echoed.

        Only a still-PENDING row is reported: a resolved request is not an
        open question and must not be shown as one.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, request_json, expires_at FROM pending_approvals
                WHERE task_id = ? AND session_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (task_id, session_id),
            ).fetchone()
        if row is None:
            return None
        request_id = visibility_text(row["id"])
        expires_at = canonical_utc_timestamp(row["expires_at"])
        # The fingerprint is COMPUTED from the request, never persisted, so
        # the body has to be rebuilt to obtain it. Rebuilding is not
        # exposing: exactly one derived value leaves this block, and the
        # reason, scope, risk and outbound preview the object also carries
        # are simply never read. A body that cannot be rebuilt, or that
        # disagrees with the authoritative columns about which task and
        # session it belongs to, yields no fingerprint at all.
        fingerprint = None
        document = self._visibility_document(row["request_json"])
        if document is not None:
            try:
                request = PermissionRequest.from_dict(document)
            except VISIBILITY_DECODE_ERRORS:
                request = None
            if (
                request is not None
                # F3. The body must agree with the authoritative row about
                # WHICH REQUEST IT IS. Without this, tampering only the
                # embedded id produced a projection carrying the row's id
                # beside a fingerprint derived from a different request -
                # metadata the approval path will later reject, reported here
                # as though it were actionable.
                and request.id == request_id
                and request.task_id == task_id
                and request.session_id == session_id
            ):
                fingerprint = visibility_text(request.fingerprint)
        if request_id is None or fingerprint is None or expires_at is None:
            # The columns that ARE trustworthy still travel; withholding a
            # valid request id because the body is damaged would hide the one
            # handle the operator needs to act on it.
            return ApprovalSummary(
                request_id=request_id,
                fingerprint=fingerprint,
                expires_at=expires_at,
                readable=False,
            )
        return ApprovalSummary(
            request_id=request_id, fingerprint=fingerprint, expires_at=expires_at
        )

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
