"""SQLite state, approval, checkpoint and cost ledger for JARVIS."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from jarvis.conditions import ConditionResult
from jarvis.contracts import (
    APPLIED_EFFECT_LOCAL_WRITE,
    LOCAL_WRITE_APPLIED_STATUS,
    AppliedEffectSummary,
    DeliverableSummary,
    COST_CAP_GATE,
    MAX_NANO_USD,
    MAX_SQLITE_INTEGER,
    MEMORY_ORIGIN_DETAIL_CAP,
    MONTHLY_HARD_LIMIT_NANO_USD,
    PROJECT_DECISION_CANDIDATE_WINDOW,
    PROJECT_MEMORY_CANDIDATE_WINDOW,
    PROJECT_INSTRUCTION_CLASSIFICATIONS,
    PROJECT_INSTRUCTION_TEXT_CAP,
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
    JOB_RECOVERY_ACTOR_MAX_CHARS,
    JOB_RECOVERY_REASON_MAX_CHARS,
    JOB_SPEC_MAX_BYTES,
    ORPHAN_MIN_AGE,
    APPROVAL_PENDABLE_JOB_RUN_STATUSES,
    CLAIMABLE_JOB_RUN_STATUSES,
    DISPATCHABLE_JOB_RUN_STATUSES,
    REACQUIRABLE_JOB_RUN_STATUSES,
    MAX_JOB_LEASE,
    MIN_JOB_LEASE,
    JobError,
    JobKind,
    JobRecord,
    JobRun,
    JobRunDispatch,
    JobRunLease,
    JobRunStatus,
    JobRunSummary,
    JobSpec,
    JobSummary,
    HistoryFinding,
    chain_lifecycle_coherent,
    execution_history_verdict,
    job_recovery_note,
    SETTLEABLE_JOB_RUN_STATUSES,
    SETTLED_JOB_RUN_STATUSES,
    UNKNOWNABLE_JOB_RUN_STATUSES,
    UNSETTLED_JOB_RUN_STATUSES,
    ExecutionClaimOutcome,
    LedgerSnapshot,
    LoopCheckpoint,
    LoopStage,
    MemoryLifecycle,
    MemoryRetrievalQuery,
    OutcomeStatus,
    MemoryOrigin,
    MonetaryState,
    PermissionGrant,
    PermissionRequest,
    ProjectMemoryItem,
    ProjectMemoryKind,
    ProgressIssueKind,
    ProjectFileRef,
    PROJECT_FILE_ID,
    ProjectInstruction,
    ProjectRecord,
    ReconciliationOutcome,
    ReconciliationResult,
    CancellationOutcome,
    ReservationState,
    StructuredError,
    RetrievedContext,
    RetrievedMemoryItem,
    RetrievedMemoryKind,
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
    memory_lifecycle_refusal,
    normalize_session_next_step,
    normalize_retrieved_text,
    retrieval_eligible,
    retrieval_tokens,
    truncate_retrieved_text,
    RETRIEVAL_SUMMARY_CAP,
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
    local_write_record_refusal,
    model_answer_refusal,
    chain_id_refusal,
    chain_state_refusal,
    chain_task_refusal,
    format_usd,
    ledger_month,
    monetary_state_from_raw,
    nano_usd,
    normalize_symbol,
    persistable_count,
    redact_credentials,
    redact_local_paths,
    require_billing_class,
    result_to_dict,
    terminal_failure_result,
    require_monetary_state,
    reservation_state_from_raw,
    sanitize_diagnostic_text,
    usd_from_nano,
    utc_now,
    validate_project_id,
)
from jarvis.schedules import ScheduleError, latest_occurrence, next_occurrence
from jarvis.conversation import (
    CONVERSATION_ID,
    CONVERSATION_ORIGINS,
    CONVERSATION_TITLE_MAX,
    ConversationRecord,
    ConversationState,
    CorruptConversation,
    decode_state,
    encode_state,
    new_conversation_id,
    read_verified,
)
from jarvis.lifecycle import is_terminal
from jarvis.progress import NON_FAILURE_ISSUE_KINDS, clear_issues, set_issue
from jarvis.planning import build_task_plan
from jarvis import project_files


SCHEMA_VERSION = 10

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

#: Phase 2C. ``settle_job_run``'s default for ``condition_result`` means
#: PRESERVE the run's own existing value, never "set it to None" - ``None``
#: is itself a legitimate value a caller might one day need to write, so it
#: cannot double as "unspecified". A watch's ``condition_result`` is written
#: exactly once, by the fenced TRUE binding or by this method's own FALSE/
#: UNKNOWN settle of an unbound generation; every LATER settlement of a
#: bound watch - COMPLETED, FAILED, CANCELLED, through the same shared
#: reflection every job kind uses - must leave that record exactly as it
#: found it, not silently null it back out because the caller settling a
#: chain's durable status had no reason to know a predicate was ever asked.
_PRESERVE_CONDITION_RESULT = object()


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


def _sql_values(values: Any) -> str:
    """A closed vocabulary as a SQL literal list, derived from the enum.

    The CHECK constraints below must agree with the Python vocabularies
    exactly and forever. Writing the spellings out twice is how they stop
    agreeing, so the SQL is generated from the same enums the loader uses and
    a member added later cannot be forgotten in one of the two places.
    """
    return ", ".join(f"'{value}'" for value in sorted(values))


#: Scheduler phase 1B tables. ONE definition, used by both the additive 7 -> 8
#: migration and the current-schema creation, so a migrated database and a
#: fresh one are structurally identical.
#:
#: The invariants are HERE rather than only in Python:
#:
#: * ``UNIQUE (job_id, job_revision, nominal_due_at_utc)`` is occurrence
#:   identity - one generation per (job, revision, nominal instant);
#:   ``UNIQUE (job_id, generation)`` makes the monotonic counter a key, so a
#:   settled generation's number can never be reissued;
#: * the PARTIAL unique index on ``settled_at IS NULL`` is the
#:   one-unsettled-generation-per-job rule, enforced by the database rather
#:   than by whoever remembers to check;
#: * the status/settlement CHECK is the settled-shape rule, so a row cannot
#:   be counted as open by one reader and closed by another;
#: * ``next_due_at_utc IS NULL OR (enabled = 1 AND archived_at IS NULL)``
#:   makes DISABLE and ARCHIVE durable rather than advisory, and
#:   ``archived_at IS NULL OR enabled = 0`` refuses the impossible
#:   archived-and-enabled combination outright.
_SCHEDULER_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS jobs (
        -- Scheduler phase 1B: a durable DEFINITION, never task authority.
        id TEXT PRIMARY KEY,
        -- The operator-established root this job's future work belongs to.
        project_id TEXT NOT NULL,
        definition_version INTEGER NOT NULL,
        -- Monotonic compare-and-set token for every definition change.
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        -- The whole validated specification, canonically encoded. Bounded by
        -- bytes, not characters, so a multi-byte payload cannot exceed the
        -- stated ceiling by being spelled in one code point per character.
        spec_json TEXT NOT NULL,
        definition_digest TEXT NOT NULL,
        next_due_at_utc TEXT,
        enabled INTEGER NOT NULL,
        archived_at TEXT,
        last_run_id TEXT,
        next_generation INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (kind IN ({_sql_values(kind.value for kind in JobKind)})),
        CHECK (definition_version >= 1),
        CHECK (revision >= 1),
        CHECK (next_generation >= 1),
        CHECK (enabled IN (0, 1)),
        CHECK (archived_at IS NULL OR enabled = 0),
        CHECK (
            next_due_at_utc IS NULL
            OR (enabled = 1 AND archived_at IS NULL)
        ),
        CHECK (length(CAST(spec_json AS BLOB)) <= {JOB_SPEC_MAX_BYTES}),
        FOREIGN KEY(project_id) REFERENCES projects(id)
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_due
        ON jobs(enabled, next_due_at_utc);

    CREATE TABLE IF NOT EXISTS job_runs (
        -- ONE occurrence of one job. NOT an execution journal: when execution
        -- exists it binds to the existing H2 execution_attempts row through
        -- attempt_id. There is no second attempt history here.
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        job_revision INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        nominal_due_at_utc TEXT NOT NULL,
        -- Immutable snapshot of the definition at the recorded revision. A
        -- later edit never rewrites it.
        project_id TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        snapshot_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        -- Materialization associations; NULL until something materializes.
        session_id TEXT,
        task_id TEXT,
        chain_id TEXT,
        attempt_id TEXT,
        -- Coordination ownership, live since phase 2A. Which worker owns
        -- this generation and until when - and nothing else: no approval, no
        -- grant, no connector authority, no permission to execute. The
        -- phase-2A ``lease_expires_at_utc`` column is added by
        -- ``_SCHEDULER_LEASE_SQL`` below rather than declared here, so a
        -- migrated database and a fresh one end up with the same job_runs
        -- SHAPE - same columns in the same order, same constraints, same
        -- indexes. Not the same stored DDL TEXT: SQLite keeps whatever
        -- CREATE text the release that made the table wrote, comments and
        -- all, so an older database's sqlite_master SQL still reads the way
        -- that release wrote it.
        owner_token TEXT,
        fencing_epoch INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        blocking_code TEXT,
        condition_result TEXT,
        created_at TEXT NOT NULL,
        claimed_at TEXT,
        settled_at TEXT,
        CHECK (job_revision >= 1),
        CHECK (generation >= 1),
        CHECK (fencing_epoch >= 0),
        CHECK (cancel_requested IN (0, 1)),
        CHECK (length(CAST(spec_json AS BLOB)) <= {JOB_SPEC_MAX_BYTES}),
        CHECK (
            (settled_at IS NULL
             AND status IN (
                 {_sql_values(s.value for s in UNSETTLED_JOB_RUN_STATUSES)}))
            OR
            (settled_at IS NOT NULL
             AND status IN (
                 {_sql_values(s.value for s in SETTLED_JOB_RUN_STATUSES)}))
        ),
        UNIQUE (job_id, generation),
        UNIQUE (job_id, job_revision, nominal_due_at_utc),
        FOREIGN KEY(job_id) REFERENCES jobs(id)
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_job_runs_unsettled
        ON job_runs(job_id) WHERE settled_at IS NULL;
"""

#: Scheduler phase 2A. The ONE field a bounded lease needs that schema 8 did
#: not have, applied by the additive 8 -> 9 migration and, identically, on
#: every fresh creation.
#:
#: An ``ALTER TABLE`` on both paths rather than a column inside the ``CREATE``
#: above, deliberately: SQLite appends an added column to the stored DDL text,
#: so declaring it in place would put it in a different POSITION on the two
#: paths - fresh databases would carry it mid-table, migrated ones last. The
#: ALTER on both paths keeps the column order, and with it the whole job_runs
#: shape, the same everywhere. Stored DDL text still differs between an old
#: database and a fresh one, because SQLite preserves the CREATE text - including
#: its comments - of whichever release built the table; structure is what is
#: held identical, and structure is what every read and write depends on.
#:
#: There is no accompanying CHECK because ``ALTER TABLE ADD COLUMN`` cannot
#: add a table constraint, and rebuilding ``job_runs`` under live durable data
#: to gain one would be the most dangerous possible way to express it. The
#: owner/expiry pairing is instead refused by ``JobRun`` at construction,
#: which every read path goes through and which fails CLOSED: a row carrying
#: half a lease loads as ``None`` and can never be claimed, renewed or
#: reclaimed. The load-bearing atomicity - one winner per acquisition - is not
#: a constraint at all; it is the compare-and-set in each ``UPDATE``'s
#: ``WHERE`` clause under ``BEGIN IMMEDIATE``.
_SCHEDULER_LEASE_SQL = "ALTER TABLE job_runs ADD COLUMN lease_expires_at_utc TEXT"

#: Scheduler phase 2B1. The ONE structural fact a dispatch binding needs that
#: application code cannot hold: an AgentCore session belongs to at most one
#: generation. PARTIAL, because the overwhelming majority of generations never
#: get a session and NULLs must stay free to repeat.
#:
#: This index is not what makes a binding immutable - that is the
#: compare-and-set on ``session_id IS NULL AND chain_id IS NULL``. It is what
#: makes the binding EXCLUSIVE: even a writer that bypassed the method could
#: not point two occurrences at one session and have both believe they own its
#: chain.
_SCHEDULER_SESSION_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_runs_session "
    "ON job_runs(session_id) WHERE session_id IS NOT NULL"
)


def _add_scheduler_session_index(connection: sqlite3.Connection) -> None:
    """Add the phase-2B1 unique session index, or FAIL CLOSED.

    A database that already contains two generations naming one session is a
    database whose dispatch history is ambiguous: one AgentCore session, two
    occurrences that each believe they own its chain and its outcome. There is
    no honest automatic answer to which one is real, so the upgrade REFUSES.

    Deliberately no repair. Deleting a binding would strand a session nobody
    owns; keeping one and clearing the other would pick a winner on no
    evidence, and the loser's generation would then look free to dispatch a
    second time - a second connector crossing for one occurrence, which is the
    exact duplicate-effect this phase exists to prevent. A human resolves it.
    """
    duplicates = connection.execute(
        """
        SELECT session_id, COUNT(*) AS bound FROM job_runs
         WHERE session_id IS NOT NULL
         GROUP BY session_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    if duplicates:
        shared = ", ".join(sorted(str(row[0]) for row in duplicates))
        raise RuntimeError(
            "Refusing to upgrade to schema 10: these AgentCore sessions are "
            f"bound to more than one job run: {shared}. Two generations "
            "claiming one session is ambiguous dispatch history; resolve it "
            "explicitly rather than letting the upgrade choose a winner."
        )
    connection.execute(_SCHEDULER_SESSION_INDEX_SQL)


def _add_scheduler_lease_column(connection: sqlite3.Connection) -> None:
    """Add the phase-2A lease column if it is not already there.

    Idempotent by inspection rather than by exception: ``ALTER TABLE`` has no
    ``IF NOT EXISTS``, and catching ``OperationalError`` would also swallow a
    genuinely broken table.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(job_runs)").fetchall()
    }
    if "lease_expires_at_utc" not in columns:
        connection.execute(_SCHEDULER_LEASE_SQL)


# --------------------------------------------------------------------------
# The additive compatibility layer (V2 Projects foundation)
# --------------------------------------------------------------------------
#
# NOT schema migration 11, on purpose. Releases built for schema 10 are kept
# as rollback targets and share this database, and every one of them REFUSES
# a database whose ``schema_version`` is newer than 10. What they do tolerate
# is an extra, isolated table: they name their columns on every read and
# write and never enumerate the schema. So while such a release remains a
# supported rollback target, V2 adds only NEW tables and indexes here -
# created ``IF NOT EXISTS`` on every open, never an ALTER of a schema-10
# table, never a change to what schema-10 code reads or writes - and
# ``schema_version`` / ``schema_migrations`` stay exactly at 10. Once those
# releases are retired these objects can be adopted by an ordinary numbered
# migration.
#
# ``conversation_sessions.session_id`` deliberately has NO foreign key to
# ``sessions``: a schema-10 release keeps creating sessions without knowing
# this table exists.

COMPATIBILITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT NULL REFERENCES projects(id),
    title TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL,
    integrity TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_owner
    ON conversations(project_id, archived_at, updated_at);
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_legacy_imports (
    digest TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_moves (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
    project_id TEXT NOT NULL,
    boundary_seq INTEGER NOT NULL,
    moved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_deletions (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
    deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_instructions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    text TEXT NOT NULL,
    classification TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_by TEXT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_instructions_current
    ON project_instructions(project_id) WHERE superseded_by IS NULL;
CREATE TABLE IF NOT EXISTS project_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    display_name TEXT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NULL,
    char_count INTEGER NULL,
    classification TEXT NULL,
    added_at TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    purged_at TEXT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_files_active_sha
    ON project_files(project_id, sha256) WHERE lifecycle = 'active';
CREATE TABLE IF NOT EXISTS project_file_chunks (
    file_id TEXT NOT NULL REFERENCES project_files(id),
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    PRIMARY KEY (file_id, chunk_index)
);
"""

def _create_compatibility_schema(connection: sqlite3.Connection) -> None:
    """Idempotent on every open. Statement by statement, NOT ``executescript``
    (which would commit the open transaction first), so the layer is created
    inside the same write transaction as the rest of the open."""
    for statement in COMPATIBILITY_SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


#: Every object the compatibility layer owns - kept apart from the numbered
#: schema's objects so a migration test can still say exactly what ITS step
#: added.
COMPATIBILITY_SCHEMA_OBJECTS = frozenset(
    {
        "conversations",
        "idx_conversations_owner",
        "conversation_sessions",
        "conversation_legacy_imports",
        "conversation_moves",
        "conversation_deletions",
        "project_instructions",
        "idx_project_instructions_current",
        "project_files",
        "idx_project_files_active_sha",
        "project_file_chunks",
    }
)

#: The one pre-V2 copy, beside the database: ``<stem>.pre-v2<suffix>``.
PRE_V2_BACKUP_TAG = ".pre-v2"

#: A caller that does not filter conversations by owner.
ANY_OWNER = object()

#: RESERVED for V2.1 (future compatibility contract; never created here): the
#: per-conversation "all projects memory" record. A conversation with any row
#: in it can never be attached to a project (Gate 6C, Salih decision).
CONVERSATION_GLOBAL_MEMORY_TOGGLES = "conversation_global_memory_toggles"


def pre_v2_backup_path(database_path: Path) -> Path:
    return database_path.with_name(
        f"{database_path.stem}{PRE_V2_BACKUP_TAG}{database_path.suffix}"
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

    def _ensure_pre_v2_backup(self) -> None:
        """One consistent copy of a real pre-V2 database, taken before the V2
        compatibility tables are first established in it.

        WHEN: only if the database already holds JARVIS data (a ``sessions``
        table) and the compatibility layer does not exist yet. A brand-new
        database has nothing to preserve, and once ``conversations`` exists
        the layer is established and nothing is copied again - so this is
        one read of ``sqlite_master`` on every later open.

        HOW: SQLite's online backup API (consistent under WAL and concurrent
        readers, unlike a file copy) into a private temporary file, verified
        with ``integrity_check``, then ``os.rename`` into place. A rename
        never replaces an existing file, so an existing backup is never
        overwritten and a concurrent opener that loses the race discards its
        own copy. The winner renames BEFORE it creates any V2 table, so a copy
        that already contains V2 tables can never be the one that lands.
        A failure raises: the V2 layer is never established without it.
        """
        target = pre_v2_backup_path(self.database_path)
        probe = self._connect()
        try:
            tables = {
                row[0]
                for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            probe.close()
        if "sessions" not in tables or "conversations" in tables or target.exists():
            return
        temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        source = self._connect()
        try:
            copy = sqlite3.connect(temporary)
            try:
                source.backup(copy)
                copy.execute("PRAGMA journal_mode = DELETE")
                verdict = copy.execute("PRAGMA integrity_check").fetchone()
            finally:
                copy.close()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            source.close()
        if verdict is None or verdict[0] != "ok":
            temporary.unlink(missing_ok=True)
            raise RuntimeError("The pre-V2 database backup did not verify; nothing was changed.")
        try:
            os.rename(temporary, target)
        except FileExistsError:
            temporary.unlink(missing_ok=True)

    def _initialize(self) -> None:
        wal_connection = self._connect()
        try:
            wal_connection.execute("PRAGMA journal_mode = WAL")
        finally:
            wal_connection.close()
        self._ensure_pre_v2_backup()
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
            _create_compatibility_schema(connection)
            # Provenance hygiene that must hold on EVERY open, not only while
            # a migration fires: a database migrated by the pre-correction
            # build is already at the current schema version and still
            # carries invented explicit-user provenance on its legacy rows.
            self._repair_invented_provenance(connection)

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
        if target_version == 7:
            self._migrate_typed_memory(connection)
            return
        if target_version == 8:
            self._migrate_scheduler_jobs(connection)
            return
        if target_version == 9:
            self._migrate_scheduler_lease(connection)
            return
        if target_version == 10:
            self._migrate_scheduler_dispatch(connection)
            return
        raise RuntimeError(f"No migration is defined for schema {target_version}")

    @staticmethod
    def _migrate_scheduler_jobs(connection: sqlite3.Connection) -> None:
        """Add the durable scheduler job/generation tables additively (1B).

        Strictly additive and idempotent: two new tables and their indexes,
        and nothing else. No existing table is altered, no existing row is
        read, rewritten, reclassified or deleted, so every schema-7 database
        keeps its full history byte-for-byte and every session, task, grant,
        attempt, reservation and memory record behaves identically after the
        upgrade.

        No authority is invented either. The upgrade creates EMPTY tables: a
        migrated database has no jobs, so nothing becomes schedulable, nothing
        gains an approval and nothing gains a monetary authorization by having
        been upgraded.
        """
        connection.executescript(_SCHEDULER_SCHEMA_SQL)

    @staticmethod
    def _migrate_scheduler_lease(connection: sqlite3.Connection) -> None:
        """Add the bounded-lease expiry column additively (2A).

        ONE nullable column on ONE table. No existing table is altered
        otherwise, no index is added or dropped, no existing row is read,
        rewritten, reclassified or deleted - every schema-8 database keeps its
        jobs, its generations and its full non-scheduler history byte-for-byte,
        and every session, task, grant, attempt, reservation and memory record
        behaves identically after the upgrade.

        No authority is invented. Existing generations acquire the column with
        the value NULL, which is precisely "no lease" - the upgrade hands no
        run to any worker, and a schema-8 database that was blocked on an
        uncertain outcome stays blocked, because expiry alone never unblocks
        one.
        """
        _add_scheduler_lease_column(connection)

    @staticmethod
    def _migrate_scheduler_dispatch(connection: sqlite3.Connection) -> None:
        """Add the unique partial session index additively (2B1).

        ONE index on ONE existing table. No table is created, altered or
        rebuilt, no column is added, and no existing row is read for repair,
        rewritten, reclassified or deleted - every schema-9 database keeps its
        jobs, its generations and its whole non-scheduler history
        byte-for-byte.

        No authority is invented: an index grants nothing. It refuses instead,
        and it fails the upgrade closed on legacy ambiguity rather than
        inventing an answer.
        """
        _add_scheduler_session_index(connection)

    @staticmethod
    def _migrate_typed_memory(connection: sqlite3.Connection) -> None:
        """Typed memory records: provenance columns + canonical kinds (V2).

        Strictly additive and idempotent; no row is deleted and no text,
        identity, classification or lifecycle flag is rewritten, so every
        schema-6 database keeps its full history:

        * ``origin`` / ``origin_detail`` provenance columns are added. The
          SQL-level default is ``inferred`` (the LEAST trusted class) and is
          NOT backfilled upwards: a schema-6 row carries no proof of which
          write path produced it, so its provenance stays
          uncertain/untrusted. Stronger history is never invented here; only
          the trusted explicit-human write boundary establishes
          ``explicit_user``, and only for records written through it.
        * The M1-era persisted kind ``preference`` has NO proven provenance,
          so it is NOT promoted to RULE. It is preserved as NON-authoritative
          compatibility memory: canonically remapped to ``fact`` (the
          smallest non-authoritative kind in the closed four-kind
          vocabulary) with a ``legacy-v6-preference`` ``origin_detail``
          marker recording where the row came from, keeping its text,
          identity, classification, flags and timestamps untouched. It stays
          retrievable as ordinary untrusted memory and only becomes a RULE
          again if a human re-adds it through the trusted explicit-human
          write path.
        * Rows stamped by the intermediate (pre-correction) migration that
          invented ``explicit_user``/``legacy-v6-cli-add`` provenance for
          every legacy row are repaired by ``_repair_invented_provenance``,
          which runs on EVERY open for the same reason.
        """
        existing = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(project_memory_items)"
            )
        }
        if not existing:
            # Table not created yet (fresh database); the current schema below
            # already declares the columns.
            return
        # Columns first, so the compatibility mapping below can always write
        # its origin_detail marker.
        if "origin" not in existing:
            connection.execute(
                "ALTER TABLE project_memory_items ADD COLUMN origin TEXT "
                "NOT NULL DEFAULT 'inferred'"
            )
        if "origin_detail" not in existing:
            connection.execute(
                "ALTER TABLE project_memory_items ADD COLUMN origin_detail TEXT "
                "NOT NULL DEFAULT ''"
            )
        # Unproven legacy preference compatibility mapping: non-authoritative
        # canonical FACT, with a marker that records the legacy source.
        connection.execute(
            """
            UPDATE project_memory_items
               SET kind = 'fact', origin_detail = 'legacy-v6-preference'
             WHERE kind = 'preference'
            """
        )

    @staticmethod
    def _repair_invented_provenance(connection: sqlite3.Connection) -> None:
        """Demote every row carrying the invented legacy provenance stamp.

        Runs on EVERY open, not only inside migration 7: a database migrated
        by the pre-correction build is already at schema 7 and no migration
        will ever fire for it again, yet its rows still carry the invented
        ``explicit_user``/``legacy-v6-cli-add`` provenance. That mass-applied
        stamp is proof of the OPPOSITE of what it claims - the trusted
        explicit-human boundary writes ``cli-memory-add`` - so the rows fall
        back to the least-trusted origin, and the ones the intermediate build
        had promoted to RULE (legacy preferences) fall back to the same
        non-authoritative compatibility mapping as the migration applies.
        Idempotent by construction: healthy databases match no row and
        nothing is rewritten.
        """
        # First remap the promoted legacy preferences (kind rule + the
        # invented stamp) to the non-authoritative compatibility shape.
        connection.execute(
            """
            UPDATE project_memory_items
               SET kind = 'fact', origin_detail = 'legacy-v6-preference'
             WHERE kind = 'rule' AND origin_detail = 'legacy-v6-cli-add'
            """
        )
        # Then demote the invented explicit-user origin everywhere it was
        # stamped, keeping the compatibility marker on the remapped rows and
        # clearing the stamp from all the others.
        connection.execute(
            """
            UPDATE project_memory_items
               SET origin = 'inferred',
                   origin_detail = CASE
                       WHEN origin_detail = 'legacy-v6-preference'
                           THEN 'legacy-v6-preference'
                       ELSE ''
                   END
             WHERE origin = 'explicit_user'
               AND origin_detail IN ('legacy-v6-cli-add', 'legacy-v6-preference')
            """
        )

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
                -- V2 provenance: closed origin class + bounded source/event
                -- detail. The SQL default is the LEAST trusted class so a
                -- row written without provenance can never load as an
                -- explicit-user record; the write boundary always supplies
                -- both explicitly.
                origin TEXT NOT NULL DEFAULT 'inferred',
                origin_detail TEXT NOT NULL DEFAULT '',
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
        # Scheduler tables, from the SAME definitions the 7 -> 8 and 8 -> 9
        # migrations apply and in the same order, so a migrated database and a
        # fresh one cannot diverge in a constraint or in their stored DDL.
        connection.executescript(_SCHEDULER_SCHEMA_SQL)
        _add_scheduler_lease_column(connection)
        _add_scheduler_session_index(connection)

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
        """Whether ANY attempt of this task has an unresolved outcome.

        Read inside the caller's transaction, and only once the task is
        already terminal, so an ordinary write pays nothing for it.

        THE COMPLETE HISTORY, never the newest row. The bytes this guards are
        the ones recovery may still need to re-attest for an attempt nobody
        concluded, and that attempt does not stop existing because a later
        clean one was written above it. ``_attempt_rows`` is the store's one
        complete-history read - the same one the recovery verdict and the
        cancellation verdict are built from - so there is a single history
        model here and not three.
        """
        states = {
            attempt.state
            for attempt in SQLiteMemoryStore._attempt_rows(connection, task_id)
        }
        return bool(
            states & {AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME}
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

        THE COMPLETE HISTORY, never the newest row. Advancing is a claim that
        this step's execution is over; an older IN_FLIGHT or UNKNOWN_OUTCOME
        under a newer FINISHED one is an execution nobody concluded, and a
        chain that walked past it would carry that unresolved effect forward
        as settled. Same ``_attempt_rows`` read as every other verdict.
        """
        states = {
            attempt.state
            for attempt in SQLiteMemoryStore._attempt_rows(connection, task_id)
        }
        return bool(
            states & {AttemptState.IN_FLIGHT, AttemptState.UNKNOWN_OUTCOME}
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

    def execution_cancellation_requested(
        self, session_id: str, task_id: str
    ) -> bool:
        """Whether this exact task is authoritatively CANCEL_REQUESTED.

        S2B. The owner-local stop watcher's ONLY view of storage, and it is
        two columns wide on purpose. The watcher polls this while a connector
        call is in flight, so every byte it can see is a byte that could
        steer a live external operation - and a persisted document is exactly
        the thing S2A proved must not steer anything. ``state_json`` is never
        read here, never decoded, and could be arbitrarily corrupt without
        changing this answer.

        Ownership is proved from the ``session_id`` column, so a foreign task
        reads as ``False`` rather than as a fact about somebody else's work.
        A missing row and every non-CANCEL_REQUESTED status read the same
        way: one bit, no disclosure.

        Reads only, one short statement, no transaction held across polls.
        It reports INTENT. It is never evidence that anything stopped.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT session_id, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None or row["session_id"] != session_id:
            return False
        return row["status"] == TaskStatus.CANCEL_REQUESTED.value

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
            # THE COMPLETE HISTORY, never the newest row. A cancellation
            # decides whether a task may be written TERMINAL, and that question
            # is about everything that ever crossed the connector boundary for
            # it - not about whichever attempt happens to sit on top. An older
            # IN_FLIGHT or UNKNOWN_OUTCOME under a newer FINISHED one is still
            # an execution nobody concluded, and a top-of-stack read would
            # write CANCELLED over it: "nothing happened" asserted about a call
            # that did.
            #
            # ``_attempt_rows`` is the store's existing complete-history read,
            # the same one the recovery verdict is built from, so there is one
            # history model here and not two.
            states = {
                attempt.state
                for attempt in self._attempt_rows(connection, task_id)
            }
            if AttemptState.UNKNOWN_OUTCOME in states:
                # S2A: refused BEFORE any write. The task, the attempt, the
                # reservation and the chain are all left exactly as they are.
                # Uncertainty ANYWHERE in the history outranks everything
                # after it; only the 2B2b1 human gate may end it.
                return CancellationOutcome("in_doubt", session, task)
            # Only the rebound objects are mutated from here. A caller's stale
            # lifecycle and progress fields never reach a row, because the
            # caller no longer supplies any.
            task.cancellation_requested = True
            session.latest_task_id = task_id
            clear_issues(task, *NON_FAILURE_ISSUE_KINDS)
            set_issue(task, ProgressIssueKind.CANCELLATION_PENDING)
            if AttemptState.IN_FLIGHT in states:
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
            if verdict == "cancelled":
                # THE TASK'S OWN QUESTION, superseded by the task's own
                # terminal write. A cancelled task can never execute, so a
                # request still marked pending against it would be a question
                # whose answer could not be acted on either way - and an
                # approver resolving it would be granting permission to a task
                # that no longer exists to use it.
                #
                # The word is the store's EXISTING one: this is exactly what
                # ``_upsert_pending_approval`` already writes when a newer
                # request displaces an older one - the question stopped being
                # answerable, and nobody decided it. No new status value, and
                # therefore no reader to teach and no schema to migrate.
                #
                # This is SUPERSESSION, not denial. Nothing here approves,
                # rejects or judges the request: its row, its id, its
                # ``request_json`` and the fingerprint derived from it are
                # preserved exactly, and only its status leaves 'pending'. The
                # CAS on that status is what makes a concurrent approver and
                # this cancellation resolve to a single winner - an approval
                # that already won keeps its own verdict, and this write
                # touches nothing.
                connection.execute(
                    "UPDATE pending_approvals SET status = 'superseded', "
                    "resolved_at = ? "
                    "WHERE task_id = ? AND session_id = ? AND status = 'pending'",
                    (utc_now(), task_id, session_id),
                )
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

        Only an unconsumed ONE-SHOT grant counts - exactly the grant the
        execution claim can consume (``begin_execution``). A remembered grant
        proves reuse only through ``has_remembered_scope_grant``, which Core
        consults solely for a rememberable request; counting it here would
        pass the gate with a grant the claim can never consume, leaving the
        task stuck instead of back at approval.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM permission_grants
                WHERE session_id = ? AND task_id = ? AND fingerprint = ?
                  AND approved = 1 AND remember_for_session = 0
                  AND consumed_by_attempt IS NULL AND expires_at > ?
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

    @staticmethod
    def _scheduled_claim_refused(
        connection: sqlite3.Connection, session_id: str
    ) -> bool:
        """Whether a bound generation forbids this execution claim (2B2b2 F1).

        Asked inside the claim transaction, immediately before
        ``PREPARED -> IN_FLIGHT``, so the answer cannot go stale between the
        question and the crossing it authorizes.

        INTERACTIVE WORK IS UNTOUCHED. A session with no generation is the
        ordinary case - every hand-driven task, every chain declared by a
        person - and it takes exactly the path it always did. Nothing here
        invents a generation for a session that has none, and the lookup is by
        the unique ``session_id`` index, so at most one row can ever answer.

        A generation that DOES claim this session must still be a live reason
        to run: unsettled, not asked to stop, and naming the very chain the
        session holds. Anything else - a settled generation, a cancelled one,
        a binding that names no chain or another chain - is refused rather than
        reasoned about. A claim is the last moment before an external effect;
        it is the wrong place to be generous about evidence.
        """
        row = connection.execute(
            "SELECT settled_at, cancel_requested, chain_id FROM job_runs "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            # Not a scheduled session. Ordinary execution, unchanged.
            return False
        if row["settled_at"] is not None:
            # The occurrence is over; it starts nothing further.
            return True
        if row["cancel_requested"] != 0:
            # THE CANCELLATION, seen in the transaction that would otherwise
            # have crossed the boundary. Anything that is not exactly 0 -
            # including a corrupted value - reads as "stop".
            return True
        chain_id = row["chain_id"]
        if not chain_id:
            return True
        stored = SQLiteMemoryStore._stored_chain(connection, session_id)
        if stored is None or stored.id != chain_id:
            # The generation's binding and the session's own chain disagree.
            return True
        return False

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
                # Window A3 (2B2b2 F1): the SCHEDULED generation's own
                # authority, arbitrated in this same transaction and for
                # exactly the same reason. A cancellation can commit after a
                # dispatcher has bound, checked and materialized everything and
                # still before this claim; without the question being asked
                # HERE, that stale dispatcher would cross the connector
                # boundary on a generation somebody already stopped. No
                # pre-transaction check can close that window.
                if self._scheduled_claim_refused(connection, session_id):
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

    @staticmethod
    def _attempt_rows(
        connection: sqlite3.Connection, task_id: str
    ) -> list[ExecutionAttempt]:
        """EVERY attempt of one task, in creation order, read in-transaction.

        The complete history, never the newest row: an unresolved attempt can
        sit under a later clean one, and a top-of-stack read would report a
        settled effect for a connector call nobody concluded.
        """
        rows = connection.execute(
            """
            SELECT id, session_id, task_id, connector, fingerprint, state,
                   created_at, updated_at
            FROM execution_attempts WHERE task_id = ? ORDER BY rowid
            """,
            (task_id,),
        ).fetchall()
        return [SQLiteMemoryStore._attempt_from_row(row) for row in rows]

    @staticmethod
    def _orphan_age_reached(updated_at: Any, moment: datetime) -> bool:
        """Whether this attempt has been IN_FLIGHT long enough to be declared.

        A GESTURE GUARD, not evidence: see ``ORPHAN_MIN_AGE``. An unreadable
        or future-dated stamp fails closed, because a bound that cannot be
        evaluated has not been satisfied.
        """
        canonical = canonical_utc_timestamp(updated_at)
        if canonical is None:
            return False
        return moment - datetime.fromisoformat(canonical) >= ORPHAN_MIN_AGE

    def request_job_run_cancel(
        self,
        run_id: str,
        *,
        now: datetime,
        actor_label: str,
        reason: str,
    ) -> JobRun:
        """Record that a person wants this generation stopped.

        Phase 2B2b2, and deliberately the smallest thing that can be true:
        ONE sticky bit meaning "somebody asked for this to stop". It is not a
        statement that a connector stopped, that a task was cancelled, that an
        external effect did not happen, or that no execution exists. Each of
        those belongs to an authority that already owns it - the executing
        owner, Core's task cancellation, the chain - and cancellation may not
        borrow any of them.

        WHAT THE BIT ACTUALLY DOES is close futures. ``claim_job_run``,
        ``bind_job_run_dispatch`` and the execution claim itself all name
        ``cancel_requested = 0`` inside their own transactions, so a request
        that commits first can never be executed past, and one that commits
        second is left as advisory over a reality it cannot rewrite. That is
        the whole mechanism; there is no stop signal here and no orchestration.

        IMMEDIATE SETTLEMENT IS THE NARROW CASE, and its proof is the complete
        binding shape rather than any single column. A generation may be
        settled CANCELLED in this same transaction only when ``session_id``,
        ``chain_id``, ``task_id`` and ``attempt_id`` are ALL NULL and no live
        owner holds it - that is a queue position and demonstrably nothing
        else, because the only writer that creates a session for a generation
        binds it in one transaction with the RUNNING transition. A partially
        populated binding is corrupted or racing state and falls through to
        intent: "no session" alone is never read as "nothing ran".

        REFUSED OUTRIGHT: a settled generation, and one whose status is
        UNKNOWN_OUTCOME. Uncertainty has exactly one exit - the human gate that
        ends in FAILED - and letting a cancellation close it would turn "nobody
        knows" into "nothing happened", which is the one sentence this whole
        subsystem exists to refuse.

        Idempotent: the first request flips the bit and writes one record; a
        later one finds it set, writes nothing and reports the same row.
        """
        actor = job_recovery_note(
            actor_label, "actor_label", JOB_RECOVERY_ACTOR_MAX_CHARS
        )
        why = job_recovery_note(reason, "reason", JOB_RECOVERY_REASON_MAX_CHARS)
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        stamp = moment.isoformat()
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            if run.settled:
                raise JobError("the generation is already settled")
            if run.status is JobRunStatus.UNKNOWN_OUTCOME:
                # Its only exit is the human gate, and that gate ends FAILED.
                raise JobError(
                    "the generation's outcome is in doubt and cannot be cancelled"
                )
            if run.cancel_requested:
                # Already asked for. Sticky, so there is nothing to write and
                # nothing new to record.
                return run

            # THE COMPLETE UNBOUND SHAPE (F3). Every binding column, not one.
            unbound = (
                run.session_id is None
                and run.chain_id is None
                and run.task_id is None
                and run.attempt_id is None
                and not run.lease_is_live(moment)
            )
            if unbound:
                cancelled = replace(
                    run,
                    status=JobRunStatus.CANCELLED,
                    cancel_requested=True,
                    owner_token=None,
                    lease_expires_at_utc=None,
                    settled_at=stamp,
                )
                cursor = connection.execute(
                    """
                    UPDATE job_runs
                       SET status = ?, cancel_requested = 1, settled_at = ?,
                           owner_token = NULL, lease_expires_at_utc = NULL
                     WHERE id = ? AND settled_at IS NULL AND cancel_requested = 0
                       AND status = ?
                       AND session_id IS NULL AND chain_id IS NULL
                       AND task_id IS NULL AND attempt_id IS NULL
                       AND owner_token IS ? AND lease_expires_at_utc IS ?
                    """,
                    (
                        cancelled.status.value,
                        stamp,
                        run_id,
                        run.status.value,
                        # The exact ownership observed to be absent or expired,
                        # PINNED rather than re-derived - a second definition
                        # of expiry in SQL is a second place for it to drift.
                        run.owner_token,
                        run.lease_expires_at_utc,
                    ),
                )
                if cursor.rowcount != 1:
                    raise JobError("the generation is not cancellable")
                # No audit event: an unbound generation has no session, and
                # event_log rows are session-scoped. The row itself - CANCELLED,
                # settled, flagged - is the record, and nothing external can
                # have happened for it to be incomplete about.
                return cancelled

            flagged = replace(run, cancel_requested=True)
            cursor = connection.execute(
                """
                UPDATE job_runs SET cancel_requested = 1
                 WHERE id = ? AND settled_at IS NULL AND cancel_requested = 0
                """,
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise JobError("the generation is not cancellable")
            bound_session = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (run.session_id,)
            ).fetchone() if run.session_id is not None else None
            if bound_session is not None:
                # Bound generations get the record; the event log is
                # session-scoped and this one has a session to bind to.
                #
                # A generation naming a session that does not exist is
                # corrupted binding state. The INTENT is still recorded - a
                # person must always be able to stop a run, and the flag is
                # what actually refuses the claim, the binding and the
                # execution - but the audit is skipped rather than allowed to
                # fail the whole write on a foreign key. The row itself is then
                # the record, exactly as it is for an unbound cancellation.
                self._insert_job_run_event(
                    connection,
                    run.session_id,
                    "job_run_cancel_requested",
                    {
                        "run_id": run.id,
                        "job_id": run.job_id,
                        "generation": run.generation,
                        "chain_id": run.chain_id,
                        "previous_status": run.status.value,
                        # Stated so the record cannot later be misread as a
                        # finding about the world.
                        "settled_immediately": False,
                        "connector_stopped": False,
                        "external_effect_established": False,
                        "retry_authorized": False,
                        "actor_label": actor,
                        "actor_authenticated": False,
                        "reason": why,
                        "requested_at": stamp,
                    },
                    stamp,
                )
        return flagged

    def declare_attempt_orphaned(
        self,
        session_id: str,
        task_id: str,
        attempt_id: str,
        *,
        now: datetime,
        actor_label: str,
        reason: str,
    ) -> AttemptRecoveryOutcome:
        """Record that a person believes an IN_FLIGHT execution is orphaned.

        Phase 2B2b1, and the ONE exit from the single H2 state that had none.
        An attempt becomes IN_FLIGHT durably before the connector boundary and
        leaves it only when its owner concludes it, so a process that died
        holding one blocks its task, its chain and every later generation of
        its job forever - nothing may replay it and nothing may pretend it
        ended.

        WHAT THIS RECORDS is a person's statement that the outcome will never
        be established by the party that started it. It is NOT proof the
        worker died, NOT proof the effect happened, NOT proof it did not, and
        NOT permission to retry: the attempt moves from "nobody has said yet"
        to "nobody ever will", which is a loss of hope rather than a gain of
        knowledge. Everything else - the task, its result, its chain, its
        reservation, its generation - is left exactly as it stands, because
        each of those has its own explicit authority elsewhere.

        THE STATE CAS IS THE WHOLE CONCURRENCY STORY, and it is enough. Every
        writer that can end an attempt names ``IN_FLIGHT`` as its source, so a
        live owner concluding first makes this refuse and this committing
        first makes that owner's conclusion lose - ``conclude_execution``
        returns "lost" and never writes the task. No owner token, epoch, lease
        or heartbeat exists in H2 and none is added here: inferring death from
        a timer is exactly what an explicit human declaration replaces.
        """
        actor = job_recovery_note(
            actor_label, "actor_label", JOB_RECOVERY_ACTOR_MAX_CHARS
        )
        why = job_recovery_note(reason, "reason", JOB_RECOVERY_REASON_MAX_CHARS)
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        stamp = moment.isoformat()
        with self._connection(write=True) as connection:
            attempt = connection.execute(
                "SELECT id, session_id, task_id, connector, state, updated_at "
                "FROM execution_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            # Identity first, and ONE verdict for every way it can fail: which
            # other session or task owns an attempt is not something a refusal
            # may disclose.
            if (
                attempt is None
                or attempt["session_id"] != session_id
                or attempt["task_id"] != task_id
            ):
                return AttemptRecoveryOutcome.UNKNOWN
            task = self._authoritative_task_row(connection, session_id, task_id)
            if task is None:
                return AttemptRecoveryOutcome.UNKNOWN
            session = self._authoritative_session_row(connection, session_id)
            if session is None:
                return AttemptRecoveryOutcome.UNKNOWN
            if attempt["state"] != AttemptState.IN_FLIGHT.value:
                # PREPARED never crossed the boundary and is still legitimately
                # claimable; FINISHED already has its answer; UNKNOWN_OUTCOME
                # has already been declared - by its owner or by this gate -
                # and saying it twice changes nothing.
                return AttemptRecoveryOutcome.NOT_RECOVERABLE
            if task.status.value in _TERMINAL_STATUS_VALUES:
                return AttemptRecoveryOutcome.TERMINAL
            if not self._orphan_age_reached(attempt["updated_at"], moment):
                # A gesture guard only. Waiting longer proves nothing about
                # the worker; it just makes a slip less likely than a decision.
                return AttemptRecoveryOutcome.TOO_RECENT
            cas = connection.execute(
                """
                UPDATE execution_attempts SET state = ?, updated_at = ?
                 WHERE id = ? AND session_id = ? AND task_id = ? AND state = ?
                """,
                (
                    AttemptState.UNKNOWN_OUTCOME.value,
                    stamp,
                    attempt_id,
                    session_id,
                    task_id,
                    AttemptState.IN_FLIGHT.value,
                ),
            )
            if cas.rowcount != 1:
                # The owner concluded it inside this window. Its answer stands.
                return AttemptRecoveryOutcome.NOT_RECOVERABLE
            # SAME TRANSACTION. The declaration and the record of who made it
            # become durable together or neither does.
            self._write_checkpoint_and_event(
                connection,
                session,
                task,
                "execution_declared_orphaned",
                {
                    "attempt_id": attempt_id,
                    "connector": attempt["connector"],
                    "previous_state": AttemptState.IN_FLIGHT.value,
                    "resulting_state": AttemptState.UNKNOWN_OUTCOME.value,
                    "previous_updated_at": attempt["updated_at"],
                    # UNAUTHENTICATED, and named so IN the record: local
                    # operator invocation is the authorization boundary, and a
                    # caller-supplied string is attribution, never a principal.
                    "actor_label": actor,
                    "actor_authenticated": False,
                    "reason": why,
                    "declared_at": stamp,
                    # Stated so the record cannot be misread later as a finding
                    # about the world or as a licence to run anything again.
                    "worker_death_proved": False,
                    "external_effect_established": False,
                    "retry_authorized": False,
                },
                created_at=stamp,
            )
        return AttemptRecoveryOutcome.RECOVERED

    def resolve_unknown_job_run(
        self,
        run_id: str,
        attempt_id: str,
        *,
        now: datetime,
        actor_label: str,
        reason: str,
    ) -> JobRun:
        """Close a generation whose one uncertain attempt was abandoned.

        Phase 2B2b1, and the LAST step of the only path out of an unresolved
        external outcome: a person declared the attempt orphaned (or its owner
        raised), a person abandoned it, the chain was cancelled or failed, and
        this records that the occurrence is over.

        IT ESTABLISHES NOTHING ABOUT THE WORLD. The effect is exactly as
        unknown afterwards as before; what changes is that the job stops being
        blocked behind a question nobody can answer. Every artefact of the
        uncertainty stays readable - the attempt is still UNKNOWN_OUTCOME, the
        task is still the typed non-retryable failure, the reservation is
        still exposed, and ``blocking_code`` says why this generation ended -
        and no path anywhere retries it.

        THE PROOF IS THE OPERATION, and all of it runs in THIS transaction,
        because every fact it rests on has an authoritative writer that could
        invalidate it a moment later. Reading the history, proving the
        abandonment and settling the generation in three separate transactions
        would leave three windows in which the thing being terminalized stops
        being the thing that was proved.
        """
        if chain_id_refusal(attempt_id) is not None:
            raise JobError("attempt_id is not a usable identifier")
        actor = job_recovery_note(
            actor_label, "actor_label", JOB_RECOVERY_ACTOR_MAX_CHARS
        )
        why = job_recovery_note(reason, "reason", JOB_RECOVERY_REASON_MAX_CHARS)
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        stamp = moment.isoformat()
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            if run.status is not JobRunStatus.UNKNOWN_OUTCOME or run.settled:
                raise JobError("the generation has no unresolved unknown outcome")
            if run.session_id is None or run.chain_id is None:
                raise JobError("the generation is not bound to a session")
            if run.attempt_id is not None:
                # The binding is written exactly once, by this operation.
                raise JobError("the generation already names an attempt")
            # The bound session, proved to BE the bound session (2B2a): row
            # key, decoded identity and project record must all agree before
            # anything decoded from it is believed.
            session = self._recovery_session(connection, run)
            chain = session.chain
            if chain is None or chain.id != run.chain_id:
                raise JobError("the bound chain does not match the generation")
            if chain_state_refusal(chain) is not None:
                raise JobError("the bound chain is unusable")
            if tuple(chain.steps) != tuple(run.spec.steps):
                # The DECLARATION itself: still the exact ordered steps the
                # snapshot froze when the generation was recorded.
                raise JobError("the bound chain is no longer the declaration")
            cursor = chain.cursor
            if type(cursor) is not int or not 0 <= cursor < len(chain.step_task_ids):
                raise JobError("the bound chain has no usable cursor")
            # Every routed task, rebuilt from its OWN row and re-proved to be
            # this chain's task for its own step.
            routed: list = []
            for index, step_task_id in enumerate(chain.step_task_ids):
                if not step_task_id:
                    continue
                found = self._stored_task(connection, step_task_id)
                if found is None:
                    raise JobError("the chain routes to a task that cannot be read")
                owner, task = found
                if owner != run.session_id:
                    raise JobError("the chain routes to another session's task")
                if (
                    chain_task_refusal(
                        task, chain, index, mode=ChainTaskMode.STORED
                    )
                    is not None
                ):
                    raise JobError("the chain routes to a task it no longer owns")
                routed.append((index, task))
            # THE COMPLETE HISTORY of every routed task, through the one
            # shared definition the scheduler also reads.
            history = {task.id: self._attempt_rows(connection, task.id) for _index, task in routed}
            verdict = execution_history_verdict(
                run.session_id, routed, cursor, history
            )
            if verdict.finding is not HistoryFinding.UNKNOWN_AT_CURSOR:
                # An in-flight attempt anywhere, an unresolved one under
                # another step, more than one row under one task, a foreign or
                # malformed row, or nothing in doubt at all. ONE refusal: none
                # of them is a generation a single human decision may close.
                raise JobError(
                    "the execution history is not one unresolved outcome at the "
                    "current step"
                )
            if verdict.attempt_id != attempt_id:
                # The operator named a different attempt than the one this
                # chain is actually unresolved on. Never substituted.
                raise JobError("that attempt is not this generation's unresolved outcome")
            cursor_task_id = chain.step_task_ids[cursor]
            cursor_task = self._authoritative_task_row(
                connection, run.session_id, cursor_task_id
            )
            if cursor_task is None:
                raise JobError("the current step's task cannot be read")
            # THE ABANDONMENT, proved by three durable facts rather than
            # inferred from one. FAILED alone is what an ordinary connector
            # failure also writes, so it proves nothing by itself.
            if cursor_task.status is not TaskStatus.FAILED:
                raise JobError("the current step's task was not abandoned")
            snapshot = cursor_task.result_snapshot
            error = snapshot.get("error") if isinstance(snapshot, dict) else None
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("outcome") != OutcomeStatus.ERROR.value
                or not isinstance(error, dict)
                or error.get("code") != ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value
                or error.get("retryable") is not False
            ):
                raise JobError("the current step carries no abandonment result")
            events = connection.execute(
                """
                SELECT payload_json FROM event_log
                 WHERE session_id = ? AND task_id = ? AND event_type = ?
                """,
                (run.session_id, cursor_task_id, "execution_abandoned"),
            ).fetchall()
            if len(events) != 1:
                raise JobError("the current step has no single abandonment record")
            try:
                recorded = json.loads(events[0]["payload_json"])
            except (TypeError, ValueError) as exc:
                raise JobError("the abandonment record is unreadable") from exc
            if (
                not isinstance(recorded, dict)
                or recorded.get("attempt_id") != attempt_id
            ):
                raise JobError("the abandonment record names another attempt")
            # THE CHAIN, in a form its canonical writers could have produced.
            # "Not ACTIVE" is not enough: COMPLETED over an abandoned step is
            # corruption, and INVALID was already refused above.
            if chain.status not in (ChainStatus.CANCELLED, ChainStatus.FAILED):
                raise JobError("the chain has not been terminalized")
            if not chain_lifecycle_coherent(chain, routed):
                raise JobError("the chain status disagrees with its own steps")
            resolved = replace(
                run,
                status=JobRunStatus.FAILED,
                attempt_id=attempt_id,
                blocking_code=ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value,
                owner_token=None,
                lease_expires_at_utc=None,
                settled_at=stamp,
            )
            cas = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, settled_at = ?, blocking_code = ?,
                       attempt_id = ?
                 WHERE id = ? AND status = ? AND settled_at IS NULL
                   AND session_id = ? AND chain_id = ? AND attempt_id IS NULL
                """,
                (
                    resolved.status.value,
                    resolved.settled_at,
                    resolved.blocking_code,
                    attempt_id,
                    run_id,
                    JobRunStatus.UNKNOWN_OUTCOME.value,
                    run.session_id,
                    run.chain_id,
                ),
            )
            if cas.rowcount != 1:
                raise JobError("the generation is not resolvable")
            # SAME TRANSACTION. The settlement and the evidence for it become
            # durable together or neither does.
            self._insert_job_run_event(
                connection,
                run.session_id,
                "job_run_unknown_resolved",
                {
                    "run_id": run.id,
                    "job_id": run.job_id,
                    "generation": run.generation,
                    "chain_id": run.chain_id,
                    "cursor_task_id": cursor_task_id,
                    "attempt_id": attempt_id,
                    # The attempt KEEPS this state; it is recorded so the trail
                    # says what was closed over, not what it became.
                    "attempt_state": AttemptState.UNKNOWN_OUTCOME.value,
                    "task_status": cursor_task.status.value,
                    "task_error_code": ErrorCode.EXECUTION_UNKNOWN_OUTCOME.value,
                    "task_retryable": False,
                    "chain_status": chain.status.value,
                    "previous_status": run.status.value,
                    "resulting_status": resolved.status.value,
                    "blocking_code": resolved.blocking_code,
                    "actor_label": actor,
                    "actor_authenticated": False,
                    "reason": why,
                    "resolved_at": stamp,
                    "external_effect_established": False,
                    "retry_authorized": False,
                },
                stamp,
            )
        return resolved

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

    # ------------------------------------------------------------------
    # Project lifecycle (V2 Projects Gate 3): create / rename / archive /
    # unarchive. No delete. Each operation writes exactly one schema-10
    # column of one row (``name`` or ``active``) with its schema-10 meaning;
    # every refusal is deterministic and writes nothing.
    # ------------------------------------------------------------------

    @staticmethod
    def _project_for_update(
        connection: sqlite3.Connection, project_id: Any
    ) -> ProjectRecord:
        validate_project_id(project_id)
        row = connection.execute(
            "SELECT id, name, active, created_at FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        try:
            if row is None:
                raise ValueError
            return ProjectRecord(
                id=row["id"],
                name=row["name"],
                active=bool(row["active"]),
                created_at=str(row["created_at"]),
            )
        except (TypeError, ValueError):
            # A malformed row is refused exactly like a missing one.
            raise ValueError(f"Unknown project: {project_id}") from None

    @staticmethod
    def _refuse_duplicate_project_name(
        connection: sqlite3.Connection, name: str, *, exclude: str | None = None
    ) -> None:
        wanted = name.casefold()
        for row in connection.execute("SELECT id, name FROM projects"):
            if (
                row["id"] != exclude
                and isinstance(row["name"], str)
                and row["name"].strip().casefold() == wanted
            ):
                raise ValueError(f"A project with that name already exists: {name}")

    def create_project(self, name: str) -> ProjectRecord:
        """A new ACTIVE project with an opaque id (``p-`` + uuid4 hex).

        The id is never derived from the name, a time, a provider or a model,
        and it matches ``PROJECT_ID_PATTERN``, so schema-10 releases load the
        row unchanged."""
        project = self._new_project_record(name)
        with self._connection(write=True) as connection:
            self._insert_new_project(connection, project)
        return project

    @staticmethod
    def _new_project_record(name: Any) -> ProjectRecord:
        if not isinstance(name, str):
            raise ValueError("project name must be a non-blank string")
        return ProjectRecord(id="p-" + uuid4().hex, name=name.strip())

    def _insert_new_project(self, connection: sqlite3.Connection, project: ProjectRecord) -> None:
        self._refuse_duplicate_project_name(connection, project.name)
        connection.execute(
            "INSERT INTO projects (id, name, active, created_at) VALUES (?, ?, 1, ?)",
            (project.id, project.name, project.created_at),
        )

    def rename_project(self, project_id: str, name: str) -> ProjectRecord:
        """Change only ``name``. The id, owner links and ``active`` stay."""
        if not isinstance(name, str):
            raise ValueError("project name must be a non-blank string")
        with self._connection(write=True) as connection:
            current = self._project_for_update(connection, project_id)
            renamed = replace(current, name=name.strip())
            if renamed.name == current.name:
                raise ValueError(f"Project already has that name: {project_id}")
            self._refuse_duplicate_project_name(
                connection, renamed.name, exclude=project_id
            )
            connection.execute(
                "UPDATE projects SET name = ? WHERE id = ?", (renamed.name, project_id)
            )
        return renamed

    def set_project_active(self, project_id: str, active: bool) -> ProjectRecord:
        """Archive (``False``) or unarchive (``True``): only ``active`` changes.

        Archiving deletes nothing; unarchiving restores exactly what was
        there. Repeating the current state is refused, not ignored."""
        if type(active) is not bool:
            raise ValueError("active must be a bool")
        with self._connection(write=True) as connection:
            current = self._project_for_update(connection, project_id)
            if current.active is active:
                state = "active" if active else "archived"
                raise ValueError(f"Project is already {state}: {project_id}")
            connection.execute(
                "UPDATE projects SET active = ? WHERE id = ?", (int(active), project_id)
            )
        return replace(current, active=active)

    # ------------------------------------------------------------------
    # Project instructions (Gate 4): one CURRENT version per project,
    # INSERT-only with supersession (only ``superseded_by`` of the previous
    # current row is ever set). Guidance for the model, never authority.
    # ------------------------------------------------------------------

    _INSTRUCTION_COLUMNS = "id, project_id, text, classification, created_at, superseded_by"

    @staticmethod
    def _instruction_from_row(row: sqlite3.Row | None) -> ProjectInstruction | None:
        if row is None:
            return None
        try:
            classification = DataClassification(row["classification"])
            if (
                classification not in PROJECT_INSTRUCTION_CLASSIFICATIONS
                or type(row["text"]) is not str
                or len(row["text"]) > PROJECT_INSTRUCTION_TEXT_CAP
            ):
                return None
            return ProjectInstruction(
                id=row["id"],
                project_id=validate_project_id(row["project_id"]),
                text=row["text"],
                classification=classification,
                created_at=str(row["created_at"]),
                superseded_by=row["superseded_by"],
            )
        except (TypeError, ValueError):
            # A malformed row is never delivered.
            return None

    def save_explicit_user_project_instruction(
        self, project_id: str, text: str, classification: DataClassification | str
    ) -> ProjectInstruction:
        """THE trusted explicit-user write boundary for project instructions,
        reached only from the local CLI/app composition path. Model output,
        connectors, intent results, memory and imported content have no path
        here.

        A new version supersedes the current one; empty text is a clear (a
        version meaning "no active instruction"). Hygiene is exactly project
        memory's: newline normalization, a hard cap (REJECT, never truncate),
        credential and local-path redaction. Only PUBLIC/INTERNAL may be
        stored. Refused while the project is archived. Every refusal writes
        nothing."""
        validate_project_id(project_id)
        try:
            classification = DataClassification(classification)
        except (TypeError, ValueError):
            raise ValueError("an instruction classification is required (public or internal)") from None
        if classification not in PROJECT_INSTRUCTION_CLASSIFICATIONS:
            raise ValueError("an instruction may only be PUBLIC or INTERNAL")
        if not isinstance(text, str):
            raise ValueError("instruction text must be a string")
        text = normalize_retrieved_text(text)
        if len(text) > PROJECT_INSTRUCTION_TEXT_CAP:
            raise ValueError(
                f"instruction text is longer than {PROJECT_INSTRUCTION_TEXT_CAP} characters"
            )
        text, _ = redact_credentials(text)
        text = redact_local_paths(text)
        record = ProjectInstruction(
            id="pi-" + uuid4().hex,
            project_id=project_id,
            text=text,
            classification=classification,
            created_at=utc_now(),
        )
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT active FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None or not row["active"]:
                raise ValueError(f"Unknown or inactive project: {project_id}")
            connection.execute(
                "UPDATE project_instructions SET superseded_by = ? "
                "WHERE project_id = ? AND superseded_by IS NULL",
                (record.id, project_id),
            )
            connection.execute(
                f"INSERT INTO project_instructions ({self._INSTRUCTION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (record.id, project_id, record.text, classification.value, record.created_at),
            )
        return record

    def load_project_instruction(self, project_id: str) -> ProjectInstruction | None:
        """The CURRENT version (possibly an empty clear), or ``None``."""
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {self._INSTRUCTION_COLUMNS} FROM project_instructions "
                "WHERE project_id = ? AND superseded_by IS NULL",
                (project_id,),
            ).fetchone()
        return self._instruction_from_row(row)

    def list_project_instruction_history(self, project_id: str) -> list[ProjectInstruction]:
        """Every version, oldest first. Unreadable rows are skipped."""
        validate_project_id(project_id)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {self._INSTRUCTION_COLUMNS} FROM project_instructions "
                "WHERE project_id = ? ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
        records = (self._instruction_from_row(row) for row in rows)
        return [record for record in records if record is not None]

    # ------------------------------------------------------------------
    # Project files (Gate 5): explicit-user text files, untrusted DATA.
    # Lifecycle ACTIVE <-> RETIRED, plus a separate explicit, permanent PURGE
    # (Salih decision 2026-09-29) that leaves a metadata tombstone. Only the
    # display name is kept, never the path; the original bytes are never
    # kept, only the normalized text in chunks.
    # ------------------------------------------------------------------

    _FILE_COLUMNS = (
        "id, project_id, display_name, sha256, byte_size, char_count, "
        "classification, added_at, lifecycle, purged_at"
    )

    @staticmethod
    def _file_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else {key: row[key] for key in row.keys()}

    @staticmethod
    def _project_row_for_files(
        connection: sqlite3.Connection, project_id: Any, *, need_active: bool
    ) -> None:
        validate_project_id(project_id)
        row = connection.execute(
            "SELECT active FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown project: {project_id}")
        if need_active and not row["active"]:
            raise ValueError(f"Project is archived: {project_id}")

    def _file_for_update(
        self, connection: sqlite3.Connection, project_id: str, file_id: Any
    ) -> dict[str, Any]:
        if type(file_id) is not str or not PROJECT_FILE_ID.fullmatch(file_id):
            raise ValueError("file id must be 'pf-' followed by 32 lowercase hex digits")
        row = self._file_row(connection.execute(
            f"SELECT {self._FILE_COLUMNS} FROM project_files WHERE id = ? AND project_id = ?",
            (file_id, project_id),
        ).fetchone())
        if row is None or row["lifecycle"] not in project_files.LIFECYCLES:
            raise ValueError(f"Unknown project file: {file_id}")
        return row

    @staticmethod
    def _require_file_quota(
        connection: sqlite3.Connection,
        project_id: str,
        *,
        active_add: int,
        files_add: int,
        chars_add: int,
    ) -> None:
        active, retained, chars = connection.execute(
            "SELECT COALESCE(SUM(lifecycle = 'active'), 0), "
            "COALESCE(SUM(lifecycle != 'purged'), 0), "
            "COALESCE(SUM(CASE WHEN lifecycle != 'purged' THEN char_count ELSE 0 END), 0) "
            "FROM project_files WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if active + active_add > project_files.PROJECT_FILE_ACTIVE_MAX:
            raise ValueError(
                f"A project can have at most {project_files.PROJECT_FILE_ACTIVE_MAX} active files."
            )
        if retained + files_add > project_files.PROJECT_FILE_RETAINED_MAX:
            raise ValueError(
                f"A project can keep at most {project_files.PROJECT_FILE_RETAINED_MAX} files; "
                "purge a retired file first."
            )
        if chars + chars_add > project_files.PROJECT_FILE_RETAINED_CHARS_MAX:
            raise ValueError(
                f"A project can keep at most {project_files.PROJECT_FILE_RETAINED_CHARS_MAX} "
                "characters of files; purge a retired file first."
            )

    @staticmethod
    def _require_unique_active_sha(
        connection: sqlite3.Connection, project_id: str, sha256: str
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM project_files WHERE project_id = ? AND sha256 = ? AND lifecycle = 'active'",
            (project_id, sha256),
        ).fetchone() is not None:
            raise ValueError("An identical file is already active in this project.")

    def save_explicit_user_project_file(
        self,
        project_id: str,
        content: "project_files.ProjectFileContent",
        classification: DataClassification | str,
    ) -> dict[str, Any]:
        """THE explicit-user add boundary, reached only from the local
        CLI/app composition path with a file the person chose
        (``project_files.read_project_file``). Classification is required;
        SECRET may be stored but is never retrieval-eligible. Refused on an
        archived project. Every refusal writes nothing."""
        try:
            classification = DataClassification(classification)
        except (TypeError, ValueError):
            raise ValueError("a file classification is required") from None
        if type(content) is not project_files.ProjectFileContent:
            raise ValueError("a project file must be read through read_project_file")
        chunks = project_files.chunk_text(content.text)
        record = {
            "id": "pf-" + uuid4().hex,
            "project_id": project_id,
            "display_name": content.display_name,
            "sha256": content.sha256,
            "byte_size": content.byte_size,
            "char_count": len(content.text),
            "classification": classification.value,
            "added_at": utc_now(),
            "lifecycle": project_files.ACTIVE,
            "purged_at": None,
        }
        with self._connection(write=True) as connection:
            self._project_row_for_files(connection, project_id, need_active=True)
            self._require_unique_active_sha(connection, project_id, content.sha256)
            self._require_file_quota(
                connection, project_id, active_add=1, files_add=1, chars_add=record["char_count"]
            )
            connection.execute(
                f"INSERT INTO project_files ({self._FILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(record[key] for key in self._FILE_COLUMNS.split(", ")),
            )
            connection.executemany(
                "INSERT INTO project_file_chunks (file_id, chunk_index, text, text_sha256) "
                "VALUES (?, ?, ?, ?)",
                [
                    (record["id"], index, text, project_files.text_digest(text))
                    for index, text in enumerate(chunks)
                ],
            )
        return record

    def list_project_files(
        self, project_id: str, *, include_purged: bool = False
    ) -> list[dict[str, Any]]:
        """Oldest first. Works on an archived project."""
        validate_project_id(project_id)
        where = "" if include_purged else " AND lifecycle != 'purged'"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {self._FILE_COLUMNS} FROM project_files WHERE project_id = ?{where} "
                "ORDER BY added_at, id",
                (project_id,),
            ).fetchall()
        return [self._file_row(row) for row in rows]

    def set_project_file_active(
        self, project_id: str, file_id: str, active: bool
    ) -> dict[str, Any]:
        """retire (``False``) / restore (``True``). Refused on an archived
        project; restore is subject to the active cap and active-SHA
        uniqueness; a purged file never changes again."""
        if type(active) is not bool:
            raise ValueError("active must be a bool")
        wanted = project_files.ACTIVE if active else project_files.RETIRED
        with self._connection(write=True) as connection:
            self._project_row_for_files(connection, project_id, need_active=True)
            row = self._file_for_update(connection, project_id, file_id)
            if row["lifecycle"] == project_files.PURGED:
                raise ValueError(f"Project file is purged: {file_id}")
            if row["lifecycle"] == wanted:
                raise ValueError(f"Project file is already {wanted}: {file_id}")
            if active:
                self._require_unique_active_sha(connection, project_id, row["sha256"])
                self._require_file_quota(
                    connection, project_id, active_add=1, files_add=0, chars_add=0
                )
            connection.execute(
                "UPDATE project_files SET lifecycle = ? WHERE id = ?", (wanted, file_id)
            )
        return {**row, "lifecycle": wanted}

    def purge_project_file(
        self, project_id: str, file_id: str, *, confirm: Any
    ) -> dict[str, Any]:
        """Permanent, explicit removal (Salih decision 2026-09-29).

        ``confirm`` must equal ``file_id``, checked before anything is
        touched. ONE transaction deletes every chunk and turns the metadata
        row into a tombstone (PURGED; name, sizes and classification NULL),
        which also releases the quota; any failure rolls all of it back.
        Allowed on an archived project. ``secure_delete`` is enabled for the
        transaction as best-effort defense in depth and the connection's
        previous setting is restored either way; nothing here claims physical
        erasure from WAL, filesystem or storage."""
        if type(confirm) is not str or confirm != file_id:
            raise ValueError("Purge needs a confirmation equal to the file id.")
        connection = self._connect()
        try:
            previous = int(connection.execute("PRAGMA secure_delete").fetchone()[0])
            connection.execute("PRAGMA secure_delete = ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._project_row_for_files(connection, project_id, need_active=False)
                    row = self._file_for_update(connection, project_id, file_id)
                    if row["lifecycle"] == project_files.PURGED:
                        raise ValueError(f"Project file is already purged: {file_id}")
                    connection.execute(
                        "DELETE FROM project_file_chunks WHERE file_id = ?", (file_id,)
                    )
                    purged_at = self._purge_file_metadata(connection, file_id)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                connection.execute(f"PRAGMA secure_delete = {previous}")
        finally:
            connection.close()
        return {
            **row,
            "display_name": None,
            "byte_size": None,
            "char_count": None,
            "classification": None,
            "lifecycle": project_files.PURGED,
            "purged_at": purged_at,
        }

    @staticmethod
    def _purge_file_metadata(connection: sqlite3.Connection, file_id: str) -> str:
        purged_at = utc_now()
        connection.execute(
            "UPDATE project_files SET lifecycle = 'purged', purged_at = ?, display_name = NULL, "
            "byte_size = NULL, char_count = NULL, classification = NULL WHERE id = ?",
            (purged_at, file_id),
        )
        return purged_at

    def retrieve_project_file_refs(
        self, project_id: str, query_text: str, classification: DataClassification
    ) -> tuple[tuple[ProjectFileRef, ...], int]:
        """Deterministic excerpt selection for one Core task (a pure read).

        Keyword overlap (``retrieval_tokens``) over the project's ACTIVE
        chunks whose classification is eligible for the task; score 0 is
        never selected; ties by file ``added_at``, file id, chunk index; at
        most 3 excerpts and 4,000 characters. Returns the references and how
        many matching chunks were skipped as ineligible (LOCAL metadata)."""
        query = retrieval_tokens(query_text)
        if not query:
            return (), 0
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT f.id, f.sha256, f.classification, f.added_at, c.chunk_index, "
                "c.text, c.text_sha256 "
                "FROM project_files f JOIN project_file_chunks c ON c.file_id = f.id "
                "WHERE f.project_id = ? AND f.lifecycle = 'active'",
                (project_id,),
            ).fetchall()
        scored = []
        ineligible = 0
        for row in rows:
            score = len(query & retrieval_tokens(row["text"]))
            if score == 0:
                continue
            try:
                eligible = retrieval_eligible(
                    classification, DataClassification(row["classification"])
                )
            except (TypeError, ValueError):
                eligible = False
            if not eligible:
                ineligible += 1
                continue
            scored.append((-score, row["added_at"], row["id"], row["chunk_index"], row))
        scored.sort(key=lambda item: item[:4])
        refs: list[ProjectFileRef] = []
        used = 0
        for *_, row in scored:
            if len(refs) >= project_files.PROJECT_FILE_EXCERPT_MAX_ITEMS:
                break
            if used + len(row["text"]) > project_files.PROJECT_FILE_EXCERPT_MAX_CHARS:
                continue
            used += len(row["text"])
            refs.append(
                ProjectFileRef(row["id"], row["sha256"], row["chunk_index"], row["text_sha256"])
            )
        return tuple(refs), ineligible

    def load_project_file_chunk(
        self, project_id: str, file_id: str, chunk_index: int
    ) -> dict[str, Any] | None:
        """One chunk and its file's CURRENT state, for re-proof (a pure read)."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT f.id, f.display_name, f.sha256, f.classification, f.lifecycle, "
                "c.chunk_index, c.text, c.text_sha256 "
                "FROM project_files f JOIN project_file_chunks c ON c.file_id = f.id "
                "WHERE f.project_id = ? AND f.id = ? AND c.chunk_index = ?",
                (project_id, file_id, chunk_index),
            ).fetchone()
        return self._file_row(row)


    @staticmethod
    def _owner_archived(connection: sqlite3.Connection, conversation_id: str) -> bool:
        """The conversation belongs to a project that is not active."""
        row = connection.execute(
            "SELECT p.active FROM conversations c JOIN projects p ON p.id = c.project_id "
            "WHERE c.id = ?",
            (conversation_id,),
        ).fetchone()
        return row is not None and row["active"] != 1

    @classmethod
    def _require_writable_owner(
        cls, connection: sqlite3.Connection, conversation_id: str
    ) -> None:
        """A conversation of an archived project is kept and readable, but
        read-only until the project is unarchived."""
        if cls._owner_archived(connection, conversation_id):
            raise ValueError(f"Conversation belongs to an archived project: {conversation_id}")

    # ------------------------------------------------------------------
    # Conversations (V2 Projects foundation; the additive compatibility layer)
    #
    # A conversation is CONTEXT: who owns it (Global = ``project_id`` NULL, or
    # one existing project) organizes history and later scopes retrieval. No
    # authority reads any of this - not a grant, fingerprint, destination,
    # root, identity, billing decision or the execution barrier.
    # ------------------------------------------------------------------

    _CONVERSATION_COLUMNS = (
        "id, project_id, title, origin, created_at, updated_at, archived_at"
    )
    #: Gate 6D-b: a permanently deleted conversation is gone for every reader
    #: and writer. Its row stays only as a tombstone (empty title and state),
    #: so its session links, its move and its legacy digest keep their meaning:
    #: the links keep Core's owner checks failing closed, and the digest keeps
    #: ``conversation.json`` from being imported again.
    _NOT_DELETED = "id NOT IN (SELECT conversation_id FROM conversation_deletions)"

    @staticmethod
    def _conversation_record(row: sqlite3.Row) -> ConversationRecord | None:
        """A row's metadata, or ``None`` when the row is not one this build
        could have written (it is then never used)."""
        record = ConversationRecord(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            origin=row["origin"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
        )
        if (
            type(record.id) is not str
            or not CONVERSATION_ID.fullmatch(record.id)
            or record.origin not in CONVERSATION_ORIGINS
            or type(record.title) is not str
            or len(record.title) > CONVERSATION_TITLE_MAX
        ):
            return None
        if record.project_id is not None:
            try:
                validate_project_id(record.project_id)
            except ValueError:
                return None
        return record

    @staticmethod
    def _require_conversation_id(conversation_id: Any) -> str:
        if type(conversation_id) is not str or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("conversation id must be 'c-' followed by 32 lowercase hex digits")
        return conversation_id

    @staticmethod
    def _insert_conversation(
        connection: sqlite3.Connection,
        *,
        project_id: str | None,
        title: str,
        origin: str,
        state: ConversationState,
    ) -> ConversationRecord:
        state_json, integrity = encode_state(state)
        now = utc_now()
        record = ConversationRecord(
            id=new_conversation_id(),
            project_id=project_id,
            title=title,
            origin=origin,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, project_id, title, state_json, integrity, origin,
                created_at, updated_at, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (record.id, project_id, title, state_json, integrity, origin, now, now),
        )
        return record

    def create_conversation(
        self, project_id: str | None = None, *, title: str = ""
    ) -> ConversationRecord:
        """A new, empty conversation owned by ``project_id`` (``None`` =
        Global). The owner must be an existing ACTIVE project - nothing here
        creates one - and it can never be changed afterwards, except by the one
        named one-way exception: a Global conversation attached to a project
        (``attach_global_conversation_to_project``, Gate 6C)."""
        if type(title) is not str or len(title) > CONVERSATION_TITLE_MAX:
            raise ValueError(
                f"conversation title must be text of at most {CONVERSATION_TITLE_MAX} characters"
            )
        if project_id is not None:
            validate_project_id(project_id)
        with self._connection(write=True) as connection:
            if project_id is not None:
                row = connection.execute(
                    "SELECT active FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if row is None or row["active"] != 1:
                    raise ValueError(f"Unknown or inactive project: {project_id}")
            return self._insert_conversation(
                connection,
                project_id=project_id,
                title=title,
                origin="jarvis",
                state=ConversationState(),
            )

    def load_conversation(self, conversation_id: str) -> ConversationRecord | None:
        self._require_conversation_id(conversation_id)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {self._CONVERSATION_COLUMNS} FROM conversations WHERE id = ? AND {self._NOT_DELETED}",
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._conversation_record(row)

    def load_conversation_state(self, conversation_id: str) -> ConversationState:
        """The conversation's state, integrity-verified before it is returned.

        Raises ``KeyError`` for an unknown (or deleted) conversation and
        ``CorruptConversation`` for a row that does not verify - nothing is
        repaired or partially used."""
        self._require_conversation_id(conversation_id)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT state_json, integrity FROM conversations WHERE id = ? AND {self._NOT_DELETED}",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return decode_state(row["state_json"], row["integrity"])

    def list_conversations(
        self, *, project_id: Any = ANY_OWNER, include_archived: bool = False
    ) -> list[ConversationRecord]:
        """Newest first. ``project_id=None`` lists Global conversations, a
        project id lists that project's, and the default lists every owner.
        A row this build could not have written is skipped, never shown, and
        a deleted conversation is never listed, whatever the mode."""
        clauses: list[str] = [self._NOT_DELETED]
        params: list[Any] = []
        if project_id is None:
            clauses.append("project_id IS NULL")
        elif project_id is not ANY_OWNER:
            validate_project_id(project_id)
            clauses.append("project_id = ?")
            params.append(project_id)
        if not include_archived:
            clauses.append("archived_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {self._CONVERSATION_COLUMNS} FROM conversations{where} "
                "ORDER BY updated_at DESC, id",
                params,
            ).fetchall()
        records = (self._conversation_record(row) for row in rows)
        return [record for record in records if record is not None]

    def update_conversation_state(
        self,
        conversation_id: str,
        change: Callable[[ConversationState], ConversationState],
    ) -> ConversationState:
        """Read, verify, change and write one conversation in ONE write
        transaction, so two writers can never lose each other's turns.

        ``change`` receives the verified current state and returns the next
        one. An archived conversation is read-only; an unknown (or deleted)
        one raises ``KeyError``; one that does not verify raises
        ``CorruptConversation`` and is left exactly as it was."""
        self._require_conversation_id(conversation_id)
        with self._connection(write=True) as connection:
            row = connection.execute(
                f"SELECT state_json, integrity, archived_at FROM conversations WHERE id = ? AND {self._NOT_DELETED}",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if row["archived_at"] is not None:
                raise ValueError(f"Conversation is archived: {conversation_id}")
            self._require_writable_owner(connection, conversation_id)
            current = decode_state(row["state_json"], row["integrity"])
            updated = change(current)
            if not isinstance(updated, ConversationState):
                raise TypeError("a conversation change must return a ConversationState")
            state_json, integrity = encode_state(updated)
            connection.execute(
                "UPDATE conversations SET state_json = ?, integrity = ?, updated_at = ? "
                "WHERE id = ?",
                (state_json, integrity, utc_now(), conversation_id),
            )
        return updated

    def set_conversation_title(self, conversation_id: str, title: str) -> None:
        """A conversation's display name (a label only; the id stays the
        identity). Refused for an archived or unknown conversation."""
        self._require_conversation_id(conversation_id)
        if type(title) is not str or len(title) > CONVERSATION_TITLE_MAX:
            raise ValueError(
                f"conversation title must be text of at most {CONVERSATION_TITLE_MAX} characters"
            )
        with self._connection(write=True) as connection:
            self._require_writable_owner(connection, conversation_id)
            changed = connection.execute(
                f"UPDATE conversations SET title = ? WHERE id = ? AND archived_at IS NULL AND {self._NOT_DELETED}",
                (title, conversation_id),
            ).rowcount
        if changed != 1:
            raise KeyError(conversation_id)

    def _live_conversation_row(self, connection: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        """The row of a conversation that exists and is not deleted, else ``KeyError``."""
        row = connection.execute(
            f"SELECT {self._CONVERSATION_COLUMNS}, state_json, integrity FROM conversations "
            f"WHERE id = ? AND {self._NOT_DELETED}",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return row

    def _require_record(self, row: sqlite3.Row, conversation_id: str) -> ConversationRecord:
        record = self._conversation_record(row)
        if record is None:
            raise CorruptConversation(f"conversation row is malformed: {conversation_id}")
        return record

    def archive_conversation(self, conversation_id: str) -> ConversationRecord:
        """Mark a conversation archived: kept, read-only, reversible
        (``unarchive_conversation``, Gate 6D-b). Idempotent - an archived
        conversation keeps its first archive time. Refused for an unknown or
        deleted conversation, under an archived project, and while a linked
        session has open work (the same check the 6C attach uses). Nothing is
        deleted here."""
        self._require_conversation_id(conversation_id)
        with self._connection(write=True) as connection:
            row = self._live_conversation_row(connection, conversation_id)
            self._require_writable_owner(connection, conversation_id)
            if row["archived_at"] is None:
                refusal = self._conversation_open_work(connection, conversation_id, "archived")
                if refusal is not None:
                    raise ValueError(refusal)
                connection.execute(
                    "UPDATE conversations SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
                    (utc_now(), conversation_id),
                )
                row = self._live_conversation_row(connection, conversation_id)
        return self._require_record(row, conversation_id)

    def unarchive_conversation(self, conversation_id: str) -> ConversationRecord:
        """Undo an archive exactly (Gate 6D-b; roadmap: no irreversible
        archive): only ``archived_at`` changes - state, title and times stay
        as they were. Refused for an unknown or deleted conversation, for one
        that is not archived, and under an archived project."""
        self._require_conversation_id(conversation_id)
        with self._connection(write=True) as connection:
            row = self._live_conversation_row(connection, conversation_id)
            if self._owner_archived(connection, conversation_id):
                raise ValueError(
                    "Conversation belongs to an archived project; unarchive the project first."
                )
            if row["archived_at"] is None:
                raise ValueError(f"Conversation is not archived: {conversation_id}")
            connection.execute(
                "UPDATE conversations SET archived_at = NULL WHERE id = ?", (conversation_id,)
            )
            row = self._live_conversation_row(connection, conversation_id)
        return self._require_record(row, conversation_id)

    # ------------------------------------------------------------------
    # Gate 6D-b permanent delete (Salih decision 2026-09-30): the SECOND step
    # after archive (or after the owner project's archive), confirmed from a
    # preview. Gate 6F F7 (Salih 2026-10-05): a Global conversation (no
    # project) needs no archive first; a project's still does. The row
    # becomes a tombstone - never a hard DELETE: the links,
    # the move and the legacy digest reference it, and nothing tied to a
    # session (tasks, approvals, checkpoints, audit, billing) is touched.
    # ------------------------------------------------------------------

    def _delete_check(
        self, connection: sqlite3.Connection, conversation_id: Any
    ) -> tuple[dict[str, Any], str | None]:
        """``(preview, refusal or None)`` from one read. An unknown, deleted
        or malformed conversation raises; a well-formed one that may not be
        deleted returns its refusal, so the preview can show it."""
        self._require_conversation_id(conversation_id)
        try:
            row = self._live_conversation_row(connection, conversation_id)
        except KeyError:
            raise ValueError(f"Unknown conversation: {conversation_id}") from None
        record = self._conversation_record(row)
        if record is None:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        state = decode_state(row["state_json"], row["integrity"])
        owner = None
        if record.project_id is not None:
            project = connection.execute(
                "SELECT name, active FROM projects WHERE id = ?", (record.project_id,)
            ).fetchone()
            owner = {
                "project_id": record.project_id,
                "name": project["name"] if project is not None else record.project_id,
                "active": project is not None and project["active"] == 1,
            }
        preview: dict[str, Any] = {
            "conversation_id": record.id,
            "title": record.title,
            "turns": len(state.turns),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "archived_at": record.archived_at,
            "owner": owner,
            "origin": record.origin,
            "expected_next_seq": state.next_seq,
        }
        refusal: str | None = None
        if record.archived_at is None and owner is not None and owner["active"]:
            refusal = "Archive this conversation first; a project's conversation can be deleted only once archived."
        refusal = refusal or self._conversation_open_work(connection, record.id, "deleted")
        preview["blocked"] = refusal or ""
        return preview, refusal

    def conversation_delete_preview(self, conversation_id: str) -> dict[str, Any]:
        """What deleting would remove, from ONE consistent read: title, turn
        count, dates, owner, origin, ``expected_next_seq`` (which the confirm
        must carry with the title) and the refusal it would meet (``blocked``)."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                preview, _ = self._delete_check(connection, conversation_id)
            finally:
                connection.rollback()
        return preview

    @staticmethod
    def _record_conversation_deletion(
        connection: sqlite3.Connection, conversation_id: str, deleted_at: str
    ) -> None:
        connection.execute(
            "INSERT INTO conversation_deletions (conversation_id, deleted_at) VALUES (?, ?)",
            (conversation_id, deleted_at),
        )

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        confirm: Any,
        expected_next_seq: Any,
        expected_title: Any,
    ) -> dict[str, Any]:
        """Permanent, explicit deletion of ONE conversation, reached only from
        the local app path (``conversation_delete_result``) after a preview
        and the person's confirm.

        ``confirm`` must equal ``conversation_id`` (checked before anything
        is touched). ONE transaction: the shared check (Global, archived, or
        its owner project archived; no open work in a linked session), the
        preview binding (``next_seq`` AND title unchanged), then the
        tombstone - title emptied, state replaced by the verified empty
        state, ``updated_at`` = the deletion time, ``archived_at`` kept or
        set - and the ``conversation_deletions`` row. Any failure rolls all
        of it back. ``secure_delete`` is on for the transaction as
        best-effort defense in depth and the connection's previous setting
        is restored either way; nothing here claims physical erasure from
        the WAL, free pages, the file system, storage or backups."""
        if type(confirm) is not str or confirm != conversation_id:
            raise ValueError("Delete needs a confirmation equal to the conversation id.")
        if type(expected_next_seq) is not int:
            raise ValueError("expected_next_seq must be the integer the preview returned")
        if type(expected_title) is not str:
            raise ValueError("expected_title must be the title the preview returned")
        empty_json, empty_integrity = encode_state(ConversationState())
        connection = self._connect()
        try:
            previous = int(connection.execute("PRAGMA secure_delete").fetchone()[0])
            connection.execute("PRAGMA secure_delete = ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    preview, refusal = self._delete_check(connection, conversation_id)
                    if refusal is None and (
                        preview["expected_next_seq"] != expected_next_seq
                        or preview["title"] != expected_title
                    ):
                        refusal = "The conversation changed after the preview; preview again."
                    if refusal is not None:
                        raise ValueError(refusal)
                    deleted_at = utc_now()
                    changed = connection.execute(
                        "UPDATE conversations SET title = '', state_json = ?, integrity = ?, "
                        "updated_at = ?, archived_at = COALESCE(archived_at, ?) WHERE id = ?",
                        (empty_json, empty_integrity, deleted_at, deleted_at, conversation_id),
                    ).rowcount
                    if changed != 1:
                        raise ValueError(f"Unknown conversation: {conversation_id}")
                    self._record_conversation_deletion(connection, conversation_id, deleted_at)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    pass                                 # a reader holds the WAL; a later writer retries
            finally:
                connection.execute(f"PRAGMA secure_delete = {previous}")
        finally:
            connection.close()
        return {**preview, "blocked": "", "deleted_at": deleted_at}

    def link_session_conversation(self, session_id: str, conversation_id: str) -> None:
        """Record, once and permanently, that ``session_id`` belongs to
        ``conversation_id``: the same link again is a no-op, another link is
        refused. The session and the conversation must exist, the
        conversation must not be archived, and a session already bound to a
        project may only belong to a conversation of THAT project - a
        context-consistency rule, never an authorization."""
        self._require_conversation_id(conversation_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session id must be a non-empty string")
        with self._connection(write=True) as connection:
            conversation = connection.execute(
                f"SELECT project_id, archived_at FROM conversations WHERE id = ? AND {self._NOT_DELETED}",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            session = connection.execute(
                "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            existing = connection.execute(
                "SELECT conversation_id FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if existing["conversation_id"] != conversation_id:
                    raise ValueError(
                        f"Session {session_id} already belongs to another conversation"
                    )
                return
            if conversation["archived_at"] is not None:
                raise ValueError(f"Conversation is archived: {conversation_id}")
            self._require_writable_owner(connection, conversation_id)
            if (
                session["project_id"] is not None
                and session["project_id"] != conversation["project_id"]
            ):
                raise ValueError("Session project does not match the conversation's owner")
            connection.execute(
                "INSERT INTO conversation_sessions (session_id, conversation_id, created_at) "
                "VALUES (?, ?, ?)",
                (session_id, conversation_id, utc_now()),
            )

    def is_conversation_deleted(self, conversation_id: str) -> bool:
        """The conversation is a Gate 6D-b tombstone (read-only query)."""
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM conversation_deletions WHERE conversation_id = ?", (conversation_id,)
            ).fetchone() is not None

    def conversation_for_session(self, session_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT conversation_id FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return None if row is None else row["conversation_id"]

    # ------------------------------------------------------------------
    # Gate 6C "Projeye bağla" (Salih decision 2026-09-29): the ONE named
    # exception to "the owner is fixed". A GLOBAL conversation may be
    # attached to a project once, one-way; nothing else ever changes an
    # owner (P -> Q and P -> Global are impossible: the UPDATE only matches
    # a NULL owner and ``conversation_moves`` allows one row per
    # conversation). Context, never authority. The preview and the writer
    # share one check, so what the person is shown is what is enforced.
    # ------------------------------------------------------------------

    def _attach_check(
        self,
        connection: sqlite3.Connection,
        conversation_id: Any,
        project_id: Any,
        new_project_name: Any,
    ) -> tuple[dict[str, Any], ProjectRecord | None, str | None]:
        """``(preview, new project record or None, refusal or None)``.

        Malformed arguments raise; a well-formed request that may not proceed
        returns its refusal, so the preview can show it."""
        self._require_conversation_id(conversation_id)
        if (project_id is None) == (new_project_name is None):
            raise ValueError("Name exactly one target: an existing project or a new project name.")
        row = connection.execute(
            f"SELECT {self._CONVERSATION_COLUMNS}, state_json, integrity FROM conversations "
            f"WHERE id = ? AND {self._NOT_DELETED}",
            (conversation_id,),
        ).fetchone()
        record = None if row is None else self._conversation_record(row)
        if record is None:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        state = decode_state(row["state_json"], row["integrity"])
        preview: dict[str, Any] = {
            "conversation_id": record.id,
            "title": record.title,
            "turns": len(state.turns),
            "created_at": record.created_at,
            "expected_next_seq": state.next_seq,
        }
        new_project: ProjectRecord | None = None
        refusal: str | None = None
        if project_id is not None:
            validate_project_id(project_id)
            target = connection.execute(
                "SELECT name, active FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            preview["target"] = {"project_id": project_id, "new": False,
                                 "name": target["name"] if target is not None else project_id}
            if target is None:
                refusal = f"Unknown project: {project_id}"
            elif target["active"] != 1:
                refusal = f"Project is archived: {project_id}"
        else:
            name = new_project_name.strip() if isinstance(new_project_name, str) else new_project_name
            preview["target"] = {"project_id": None, "new": True, "name": name if isinstance(name, str) else ""}
            try:
                new_project = self._new_project_record(new_project_name)
                self._refuse_duplicate_project_name(connection, new_project.name)
            except ValueError as exc:
                new_project, refusal = None, str(exc)
        if record.project_id is not None:
            refusal = "Only a Global conversation can be attached to a project."
        elif record.archived_at is not None:
            refusal = "This conversation is archived; it is read-only."
        refusal = refusal or self._conversation_open_work(connection, record.id, "attached") or self._attach_toggle_refusal(
            connection, record.id
        )
        preview["blocked"] = refusal or ""
        return preview, new_project, refusal

    @staticmethod
    def _conversation_open_work(
        connection: sqlite3.Connection, conversation_id: str, action: str
    ) -> str | None:
        """A linked session with unfinished work - a non-terminal task, a
        pending approval or a non-terminal chain - blocks the move (6C), the
        archive and the delete (6D-b), and so does a linked session that
        cannot be read (fail closed). ``action`` only words the refusal."""
        terminal_chain = {ChainStatus.COMPLETED, ChainStatus.FAILED, ChainStatus.CANCELLED}
        for linked in connection.execute(
            "SELECT s.state_json FROM conversation_sessions cs "
            "LEFT JOIN sessions s ON s.id = cs.session_id WHERE cs.conversation_id = ?",
            (conversation_id,),
        ).fetchall():
            try:
                session = SessionState.from_dict(json.loads(linked["state_json"]))
            except Exception:  # noqa: BLE001 - an unreadable session fails closed
                return f"A session of this conversation cannot be read, so it cannot be {action}."
            if session.chain is not None and session.chain.status not in terminal_chain:
                return "A chain in this conversation is not finished; finish or cancel it first."
        terminal = sorted(_TERMINAL_STATUS_VALUES)
        if connection.execute(
            "SELECT 1 FROM tasks t JOIN conversation_sessions cs ON cs.session_id = t.session_id "
            f"WHERE cs.conversation_id = ? AND t.status NOT IN ({', '.join('?' * len(terminal))}) LIMIT 1",
            (conversation_id, *terminal),
        ).fetchone() is not None:
            return "A task in this conversation is not finished; finish or cancel it first."
        if connection.execute(
            "SELECT 1 FROM pending_approvals pa JOIN conversation_sessions cs ON cs.session_id = pa.session_id "
            "WHERE cs.conversation_id = ? AND pa.status = 'pending' LIMIT 1",
            (conversation_id,),
        ).fetchone() is not None:
            return "An approval in this conversation is still pending; answer it first."
        return None

    @staticmethod
    def _attach_toggle_refusal(connection: sqlite3.Connection, conversation_id: str) -> str | None:
        """FUTURE COMPATIBILITY CONTRACT (V2.1): a conversation that EVER had
        "all projects memory" on can never be attached (Salih decision). V2.1
        owns the reserved table ``conversation_global_memory_toggles``
        (append-only, explicit-user writes only, keyed by conversation_id);
        6C never creates it. Evaluated on every call: absent -> no refusal; a
        row for the conversation -> refused; present but unreadable -> refused."""
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (CONVERSATION_GLOBAL_MEMORY_TOGGLES,),
        ).fetchone() is None:
            return None
        try:
            used = connection.execute(
                f"SELECT 1 FROM {CONVERSATION_GLOBAL_MEMORY_TOGGLES} WHERE conversation_id = ? LIMIT 1",
                (conversation_id,),
            ).fetchone() is not None
        except sqlite3.Error:
            return "This conversation's \"all projects memory\" record cannot be read, so it cannot be attached."
        return "This conversation used \"all projects memory\"; it can never be attached to a project." if used else None

    def conversation_attach_preview(
        self, conversation_id: str, *, project_id: str | None = None, new_project_name: str | None = None
    ) -> dict[str, Any]:
        """What attaching would do, from ONE consistent read: title, turn
        count, created date, the target, ``expected_next_seq`` (which the
        confirm must carry) and the refusal it would meet (``blocked``)."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                preview, _, _ = self._attach_check(connection, conversation_id, project_id, new_project_name)
            finally:
                connection.rollback()
        return preview

    def attach_global_conversation_to_project(
        self,
        conversation_id: str,
        *,
        expected_next_seq: int,
        project_id: str | None = None,
        new_project_name: str | None = None,
    ) -> dict[str, Any]:
        """THE one owner change there is, reached only from the local app
        composition path (``conversation_attach_result``) after a preview and
        the person's explicit confirm. ONE write transaction: the shared
        check, the preview binding (the conversation's ``next_seq`` must be
        the previewed one), an optional new project, the guarded owner UPDATE
        and the ``conversation_moves`` row. Every refusal writes nothing."""
        if type(expected_next_seq) is not int:
            raise ValueError("expected_next_seq must be the integer the preview returned")
        with self._connection(write=True) as connection:
            preview, new_project, refusal = self._attach_check(
                connection, conversation_id, project_id, new_project_name
            )
            if refusal is None and preview["expected_next_seq"] != expected_next_seq:
                refusal = "The conversation changed after the preview; preview again."
            if refusal is not None:
                raise ValueError(refusal)
            if new_project is not None:
                self._insert_new_project(connection, new_project)
                project_id = new_project.id
            changed = connection.execute(
                "UPDATE conversations SET project_id = ? "
                "WHERE id = ? AND project_id IS NULL AND archived_at IS NULL",
                (project_id, conversation_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Only a Global conversation can be attached to a project.")
            moved_at = utc_now()
            connection.execute(
                "INSERT INTO conversation_moves (conversation_id, project_id, boundary_seq, moved_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, project_id, expected_next_seq, moved_at),
            )
        return {**preview, "target": {**preview["target"], "project_id": project_id},
                "boundary_seq": expected_next_seq, "moved_at": moved_at}

    def import_legacy_conversation(self, path: str | Path) -> ConversationRecord | None:
        """Preserve the legacy ``conversation.json`` as a Global conversation.

        The file is only READ (``read_verified``): never renamed, locked,
        truncated or rewritten, because a schema-10 rollback release may
        still be using it. Idempotent BY CONTENT DIGEST, enforced by the
        primary key of ``conversation_legacy_imports`` inside one write
        transaction: a digest already imported adds nothing, while a valid
        digest never seen before - e.g. what a rollback release wrote in the
        meantime - becomes one more recovered Global conversation. Histories
        are never merged.

        Returns the new record, or ``None`` when the file is absent or its
        digest was already imported. A file that does not verify raises
        ``CorruptConversation`` and nothing is created."""
        path = Path(path)
        if not path.exists():
            return None
        state, digest = read_verified(path)
        with self._connection(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM conversation_legacy_imports WHERE digest = ?", (digest,)
            ).fetchone() is not None:
                return None
            record = self._insert_conversation(
                connection,
                project_id=None,
                title="",
                origin="legacy-import",
                state=state,
            )
            connection.execute(
                "INSERT INTO conversation_legacy_imports (digest, conversation_id, imported_at) "
                "VALUES (?, ?, ?)",
                (digest, record.id, record.created_at),
            )
            return record

    def save_memory_item(
        self, item: ProjectMemoryItem, *, supersedes: str | None = None
    ) -> ProjectMemoryItem:
        """Persist one programmatic memory item (INSERT-only + supersede).

        The GENERIC write surface: untrusted/non-human provenance only. It
        refuses an ``EXPLICIT_USER`` origin outright, so no arbitrary caller
        can create a durable RULE - or any explicit-user record - merely by
        supplying the origin enum. ``EXPLICIT_USER`` is established only by
        ``save_explicit_user_memory_item``, the trusted explicit-human write
        boundary whose audited production caller is the local CLI/app
        ``memory add`` composition path.

        The write boundary applies exactly the hygiene the persistence
        contract needs and nothing more: newline normalization, the hard text
        ceiling (REJECT, never truncate - memory text is semantic user data,
        not diagnostics), and the canonical credential/path redaction
        helpers. Content columns are never rewritten; ``supersedes`` only
        flips the referenced item's lifecycle flags, in the same transaction,
        and only from a lifecycle shape the canonical validator proves is
        exactly ACTIVE.
        """
        if item.origin is MemoryOrigin.EXPLICIT_USER:
            raise ValueError(
                "explicit-user provenance requires the trusted "
                "explicit-human memory add path"
            )
        if item.kind is ProjectMemoryKind.RULE:
            # Unreachable through a valid record (the contract refuses a
            # RULE without explicit-user provenance), restated here as
            # defense in depth against a record mutated after construction.
            raise ValueError("a RULE requires explicit-user provenance")
        return self._persist_memory_item(item, supersedes=supersedes)

    def save_explicit_user_memory_item(
        self,
        project_id: str,
        kind: str,
        classification: str,
        text: str,
        *,
        supersedes: str | None = None,
    ) -> ProjectMemoryItem:
        """THE trusted explicit-human write boundary (Memory V2 foundation).

        The caller supplies RAW command inputs and never a provenance value:
        this method establishes ``EXPLICIT_USER`` internally, after the local
        human command boundary has been identified, so trusted provenance can
        neither be passed in nor forged through a generic boolean/enum
        argument. Audited production callers: exactly one -
        ``jarvis.app.memory_add_result``, the local CLI/app ``memory add``
        composition path. No model, connector, import or other programmatic
        caller is sanctioned; generic writers use ``save_memory_item``.

        The deprecated M1-era ``preference`` kind input maps to RULE here -
        and ONLY here - because this path is an explicit human CLI action.
        An unknown kind or classification, a bad project id or oversized text
        fails closed with ``ValueError``.
        """
        project_kind = (
            ProjectMemoryKind.RULE
            if kind == "preference"
            else ProjectMemoryKind(kind)
        )
        item = ProjectMemoryItem(
            project_id=validate_project_id(project_id),
            kind=project_kind,
            text=text,
            classification=DataClassification(classification),
            origin=MemoryOrigin.EXPLICIT_USER,
            origin_detail="cli-memory-add",
        )
        return self._persist_memory_item(item, supersedes=supersedes)

    def _persist_memory_item(
        self, item: ProjectMemoryItem, *, supersedes: str | None
    ) -> ProjectMemoryItem:
        """The one INSERT-only persistence path shared by both write surfaces.

        RULE eligibility is re-checked here even though the record contract
        already refuses it: a durable RULE carries explicit-user provenance
        established by the trusted boundary, and the storage boundary must
        stay fail-closed independently of how the record object was built.
        """
        if item.kind is ProjectMemoryKind.RULE and (
            item.origin is not MemoryOrigin.EXPLICIT_USER
        ):
            raise ValueError("a RULE requires explicit-user provenance")
        text = normalize_retrieved_text(item.text)
        if not text:
            raise ValueError("memory text must be a non-blank string")
        if len(text) > PROJECT_MEMORY_TEXT_CAP:
            raise ValueError(
                f"memory text is longer than {PROJECT_MEMORY_TEXT_CAP} characters"
            )
        text, _ = redact_credentials(text)
        text = redact_local_paths(text)
        # Defense in depth on the provenance detail: the record contract
        # already ran the one canonical hygiene pipeline, and the storage
        # boundary re-runs it rather than trusting the object it was handed.
        origin_detail = sanitize_diagnostic_text(
            item.origin_detail, MEMORY_ORIGIN_DETAIL_CAP
        )
        stored = replace(
            item,
            text=text,
            origin=item.origin,
            origin_detail=origin_detail,
            active=True,
            superseded_by=None,
        )
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
                    "SELECT project_id, active, superseded_by FROM "
                    "project_memory_items WHERE id = ?",
                    (supersedes,),
                ).fetchone()
                if (
                    old_row is None
                    or old_row["project_id"] != stored.project_id
                    # The predecessor must be a VALID lifecycle shape that is
                    # exactly ACTIVE - a corrupt (e.g. active=2) row is never
                    # transitionable, so it can never be superseded either.
                    or memory_lifecycle_refusal(
                        old_row["active"], old_row["superseded_by"]
                    ) is not None
                    or old_row["active"] != 1
                ):
                    raise ValueError(
                        "supersedes must reference an active memory item "
                        "of the same project"
                    )
            try:
                connection.execute(
                    """
                    INSERT INTO project_memory_items
                        (id, project_id, kind, text, classification, origin,
                         origin_detail, active, superseded_by, created_at,
                         updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                    """,
                    (
                        stored.id,
                        stored.project_id,
                        stored.kind.value,
                        stored.text,
                        stored.classification.value,
                        stored.origin.value,
                        stored.origin_detail,
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
        """List one project's memory items in stable created_at, id order.

        With ``include_inactive=True`` (the CLI's ``memory list --all``) the
        listing includes SUPERSEDED and RETIRED lifecycle history.

        Deterministic corrupt-row behavior (Memory V2 correction): a row
        whose shape the fail-closed loader refuses - an unknown kind, a
        lifecycle shape the canonical validator rejects, a broken identity -
        is SKIPPED. It is never silently normalized into a valid trusted
        state, never listed as an active item and never repaired by a
        display path; it stays physically stored exactly as it is. The
        listing therefore shows the rows that ARE validly loadable, in the
        same deterministic order on every call.
        """
        sql = (
            "SELECT id, project_id, kind, text, classification, origin, "
            "origin_detail, active, superseded_by, created_at, updated_at "
            "FROM project_memory_items WHERE project_id = ?"
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

    def retire_memory_item(self, project_id: str, item_id: str) -> ProjectMemoryItem:
        """Explicitly retire one ACTIVE memory item of exactly this project.

        The only lifecycle transition besides supersede, and like supersede it
        flips nothing but the lifecycle flags of the referenced row in ONE
        transaction: ``active -> 0`` with ``superseded_by`` left NULL, which
        the closed lifecycle vocabulary reads as RETIRED. The row itself stays
        durably stored and loadable with ``include_inactive=True``; it simply
        stops being an active retrieval candidate. A missing id, an unknown
        project, an item that is not an ACTIVE record of exactly this
        project, or a row whose lifecycle shape the canonical validator
        refuses (a corrupt row is never transitionable) is refused loudly -
        there is no un-retire and no silent state rewrite.
        """
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT project_id, active, superseded_by FROM "
                "project_memory_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None or row["project_id"] != project_id:
                raise ValueError(
                    "retire must reference a memory item of exactly this project"
                )
            project_row = connection.execute(
                "SELECT active FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project_row is None or not project_row["active"]:
                raise ValueError(f"Unknown or inactive project: {project_id}")
            lifecycle_refusal = memory_lifecycle_refusal(
                row["active"], row["superseded_by"]
            )
            if lifecycle_refusal is not None:
                raise ValueError(
                    "refusing to retire a record with an invalid lifecycle "
                    f"shape: {lifecycle_refusal}"
                )
            if row["active"] != 1:
                raise ValueError(
                    "only an ACTIVE memory item can be retired"
                )
            connection.execute(
                """
                UPDATE project_memory_items
                   SET active = 0, updated_at = ?
                 WHERE id = ? AND active = 1
                """,
                (utc_now(), item_id),
            )
            updated = connection.execute(
                "SELECT id, project_id, kind, text, classification, origin, "
                "origin_detail, active, superseded_by, created_at, updated_at "
                "FROM project_memory_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        item = self._memory_item_from_row(updated)
        if item is None:
            raise ValueError("retired item could not be reloaded")
        return item

    @staticmethod
    def _memory_item_from_row(row: sqlite3.Row) -> ProjectMemoryItem | None:
        """Rebuild one persisted item defensively, fail-closed on bad shape.

        An unknown kind, blank/oversized text, broken identity or a lifecycle
        shape the ONE canonical validator refuses (``active=2``/``-1``/``"yes"``,
        a blank successor id, an ACTIVE row carrying a successor) makes the
        row unusable and it is skipped (fail closed): corruption is never
        silently normalized into a valid trusted state, and the row stays
        physically stored. An unreadable classification alone degrades to
        SECRET inside ``from_dict`` so the item stays listable but never
        egress-eligible, and an unreadable provenance degrades to the LEAST
        trusted origin. A persisted RULE whose provenance is not
        explicit-user therefore refuses to build and the row is skipped: a
        tampered rule can never load as trusted.
        """
        try:
            return ProjectMemoryItem.from_dict(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "kind": row["kind"],
                    "text": row["text"],
                    "classification": row["classification"],
                    "origin": row["origin"],
                    "origin_detail": row["origin_detail"],
                    # Raw persisted value - deliberately NOT bool(...)-coerced.
                    # An impossible flag shape raises inside the canonical
                    # lifecycle validation and the row is skipped fail-closed.
                    "active": row["active"],
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
        ``PROJECT_MEMORY_CANDIDATE_WINDOW`` items PER CANONICAL KIND of
        exactly that project are additionally examined (Memory V2 correction:
        candidate acquisition is partitioned by kind BEFORE any truncation,
        so a flood of newer records in one category can no longer push the
        other categories out of the candidate set before fairness
        interleaving runs; the total stays strictly bounded by
        ``PROJECT_MEMORY_CANDIDATE_WINDOW_TOTAL``); they compete inside the
        same absolute item and character ceilings and the same eligibility
        ladder. Without a project scope the behaviour is byte-identical to
        the project-less pipeline.

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
                # Memory V2 correction: candidate acquisition is PARTITIONED
                # per canonical kind before any truncation or interleaving.
                # The previous single global newest-N window let 25+ newer
                # RULE rows exclude every DECISION/THREAD/FACT from the
                # candidate set entirely, defeating category fairness before
                # it could run. One bounded query per kind keeps every
                # category represented, and the total candidate set stays
                # strictly bounded (one window per kind, never the whole
                # table).
                for kind_value in (
                    kind.value for kind in ProjectMemoryKind
                ):
                    item_rows.extend(
                        connection.execute(
                            "SELECT id, project_id, kind, text, classification, "
                            "origin, origin_detail, active, superseded_by, "
                            "created_at, updated_at "
                            "FROM project_memory_items "
                            "WHERE project_id = ? AND active = 1 AND kind = ? "
                            "ORDER BY created_at DESC, id ASC LIMIT ?",
                            (
                                query.project_id,
                                kind_value,
                                PROJECT_MEMORY_CANDIDATE_WINDOW,
                            ),
                        ).fetchall()
                    )
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

    def reprove_retrieved_rules(
        self,
        project_id: str | None,
        retrieved: RetrievedContext,
        query_text: str = "",
    ) -> RetrievedContext:
        """Re-prove persisted snapshot RULE semantics against the store.

        THE one authoritative re-proof for RULE claims that cross a
        persisted/resumed boundary (Memory V2 correction). A task snapshot is
        untrusted data: editing its serialized fields must not manufacture
        trusted RULE semantics, and - critically - an untrusted snapshot must
        never be able to ATTACH SEMANTIC TEXT to a trusted human RULE
        identity. A surviving RULE is therefore not a checked copy of the
        snapshot item: it is RECONSTRUCTED entirely from authoritative state.

        Every ``RetrievedMemoryItem`` field is classified explicitly:

        * AUTHORITATIVE PROJECTION (taken only from the proven record):
          ``kind`` (RULE), ``record_id``, ``source_task_id`` (the record's
          project binding - a project memory record has NO authoritative
          originating task, so the snapshot can never invent one), ``summary``
          (the canonical text projection of the record's stored text),
          ``rationale`` (canonical empty - no human RULE rationale is stored),
          ``next_step`` (canonical None - same), ``classification``,
          ``created_at`` and ``origin`` (EXPLICIT_USER, verified).
        * DETERMINISTIC RETRIEVAL METADATA (recomputed by the same rules the
          current retrieval pipeline uses): ``relevance_score`` (the shared
          token-intersection score over the authoritative text and the current
          retrieval query) and ``truncated`` (the canonical projection's own
          truncation outcome).
        * NON-AUTHORITATIVE SNAPSHOT METADATA: nothing at item level is
          copied from the snapshot. Context-level ``candidate_count`` /
          ``character_count`` are accounting metadata and never item
          semantics; they are left as decoded data.

        The proof reads the CURRENT SQLite row by the snapshot's stable source
        identifier (``record_id``) and requires ALL of: the row exists, the
        same source identity under exactly this project binding, authoritative
        ``kind == RULE``, authoritative ``origin == EXPLICIT_USER``, a
        lifecycle the canonical validator reads as exactly ACTIVE, and a valid
        authoritative classification. Anything else - a missing row, changed
        kind/provenance, retired or superseded state, a changed project
        binding, a corrupt row, a malformed lifecycle or any storage failure -
        FAILS CLOSED: the item is demoted to non-authoritative data
        (``kind=FACT``, ``origin=INFERRED``) rather than trusted. The proof is
        storage-owned and never delegated to a connector or to the snapshot
        itself; provenance on non-RULE items is left as decoded data - it
        grants no authority.
        """
        if not any(
            type(item) is RetrievedMemoryItem
            and item.kind is RetrievedMemoryKind.RULE
            for item in retrieved.items
        ):
            return retrieved
        query_tokens = retrieval_tokens(query_text)
        items: list[RetrievedMemoryItem] = []
        for item in retrieved.items:
            if (
                type(item) is not RetrievedMemoryItem
                or item.kind is not RetrievedMemoryKind.RULE
            ):
                items.append(item)
                continue
            proof = (
                self._explicit_rule_proof(project_id, item.record_id)
                if project_id is not None
                else None
            )
            if proof is None:
                # Unprovable claim: demote to ordinary non-authoritative
                # memory. The content stays as untrusted data; the trust does
                # not.
                items.append(
                    replace(
                        item,
                        kind=RetrievedMemoryKind.FACT,
                        origin=MemoryOrigin.INFERRED,
                    )
                )
                continue
            # Canonical RULE projection, built from AUTHORITATIVE state only.
            # No snapshot field survives into a trusted RULE - not the
            # summary, not the rationale, not the next step, not the source
            # binding.
            summary, summary_truncated = truncate_retrieved_text(
                normalize_retrieved_text(proof.text), RETRIEVAL_SUMMARY_CAP
            )
            items.append(
                RetrievedMemoryItem(
                    kind=RetrievedMemoryKind.RULE,
                    record_id=proof.id,
                    # The project memory record's only authoritative binding
                    # is its project scope; an originating task id does not
                    # exist for it and is never taken from the snapshot.
                    source_task_id=proof.project_id,
                    summary=summary,
                    rationale="",
                    next_step=None,
                    classification=proof.classification,
                    created_at=proof.created_at,
                    # Deterministic recomputation under the same scoring rule
                    # the retrieval pipeline applies to project records.
                    relevance_score=len(
                        query_tokens & retrieval_tokens(proof.text)
                    ),
                    truncated=summary_truncated,
                    origin=proof.origin,
                )
            )
        return replace(retrieved, items=tuple(items))

    def _explicit_rule_proof(
        self, project_id: str | None, item_id: str
    ) -> ProjectMemoryItem | None:
        """The authoritative current row that proves one explicit-user RULE.

        Reads the CURRENT SQLite row by the snapshot's stable source
        identifier and re-establishes every eligibility condition from it.
        The row is loaded through the fail-closed loader, so a tampered or
        corrupt row yields ``None`` instead of trust. Anything less than a
        same-project, explicit-user, ACTIVE RULE is no proof at all.
        """
        if project_id is None or not isinstance(item_id, str) or not item_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, project_id, kind, text, classification, "
                "origin, origin_detail, active, superseded_by, "
                "created_at, updated_at "
                "FROM project_memory_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        item = self._memory_item_from_row(row) if row is not None else None
        if item is None:
            return None
        if item.project_id != project_id:
            return None
        if item.kind is not ProjectMemoryKind.RULE:
            return None
        if item.origin is not MemoryOrigin.EXPLICIT_USER:
            return None
        if item.lifecycle is not MemoryLifecycle.ACTIVE:
            return None
        return item

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
            # P2. Frozen at the same instant and for the same reason. It is
            # derived from THIS proven document and nothing else - no second
            # read, no auxiliary row, no reopened task - so there is no window
            # in which the record could be swapped after the proof.
            effect_applied=SQLiteMemoryStore._applied_effect(task),
            # Frozen at the same instant and for the same reason: the durable
            # deliverable this task keeps, as bounded metadata, from THIS
            # proven document and nothing else.
            deliverable=SQLiteMemoryStore._deliverable_summary(task),
        )

    @staticmethod
    def _applied_effect(task: Task) -> AppliedEffectSummary | None:
        """The irreversible effect an ALREADY-VALIDATED task has applied.

        P2. Reached only after identity, status and stage have been proved
        against the authoritative columns, so the record examined here belongs
        to this row. Four things must hold before anything is reported, and
        the order is the point:

        * the TASK must hold the one capability that can perform a local
          write. A record describes an operation; it is not evidence that
          this task could ever have performed one;
        * the record must survive the ONE canonical structural contract, the
          same ``local_write_record_refusal`` the load boundary, the execution
          boundary and the envelope all ask. A tampered or half-written record
          answers nothing here rather than answering approximately;
        * its status must be EXACTLY ``written``. "prepared" is an authorized
          intent that may never have run, "refused" is a write that did not
          happen, and "unknown" is the state whose whole meaning is that
          nobody can say - reporting any of them as an applied effect would be
          the same false claim in the opposite direction; and
        * nothing about the TASK's lifecycle is consulted at all. That is
          deliberate: a fact derived from CANCELLED would be manufactured by
          the very cancellation this exists to qualify, and a fact suppressed
          by COMPLETED would hide a truthful one. The effect either happened
          or it did not, and only the record knows.

        Nothing from the record crosses. Both reported values are constants
        from a closed vocabulary, so this projection cannot echo a persisted
        string even if one were tampered into the row.
        """
        # The CAPABILITY first, and it is not a formality. A record is a
        # description, and a description is not evidence that the task it
        # rides on ever had the authority to write anything: any task
        # document can carry a perfectly canonical LocalWriteRecord, so
        # validating the record alone let an "echo" or "file_read" task
        # manufacture a durable claim that an irreversible write had been
        # applied. Only the ONE capability that can actually perform a local
        # write may report having performed one, and it is asked of the
        # task's own authoritative capability rather than inferred from the
        # presence of write-shaped data - inferring it from the data would be
        # the same mistake in a different place.
        if task.capability != LOCAL_WRITE_CAPABILITY:
            return None
        record = task.local_write
        if local_write_record_refusal(record) is not None:
            return None
        # Exact, not a prefix and not a truthiness test: the refusal above
        # already proved the field is a ``str`` from the closed status
        # vocabulary, so this comparison decides between known spellings.
        if record.status != LOCAL_WRITE_APPLIED_STATUS:
            return None
        return AppliedEffectSummary(
            kind=APPLIED_EFFECT_LOCAL_WRITE, status=LOCAL_WRITE_APPLIED_STATUS
        )

    @staticmethod
    def _deliverable_summary(task: Task) -> DeliverableSummary | None:
        """The durable deliverable of an ALREADY-VALIDATED task, bounded.

        Reached only after identity, status and stage have been proved against
        the authoritative columns, so the answer examined here belongs to this
        row. The M6 ``ModelAnswer`` is the deliverable a completed task keeps
        at terminal state, and this projection exposes exactly its persisted
        metadata - never its content, which has no channel through this
        surface. Three guards, in the same order every other consumer asks:

        * the object must be EXACTLY a ``ModelAnswer`` - a dict, a subclass
          or a structurally impossible value answers nothing here;
        * it must survive the ONE canonical structural contract, the same
          ``model_answer_refusal`` the load boundary, the execution boundary
          and the envelope all ask; and
        * it must be PRESENT. The empty answer is not a small deliverable;
          ``None`` is the one spelling of "this task produced none".

        The load boundary has already degraded a tampered answer to the empty
        one, so the refusal check is defence in depth - asked here exactly as
        every other consumer must ask it, never restated.
        """
        answer = task.model_answer
        if type(answer) is not ModelAnswer or not answer.present:
            return None
        if model_answer_refusal(answer) is not None:
            return None
        return DeliverableSummary(
            character_count=answer.character_count,
            sha256=answer.sha256,
            truncated=answer.truncated,
            produced_at=answer.produced_at,
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
        attempt can be shown here. Its own identity travels with it, from
        the SAME authoritative row and the same single query - the operator
        needs that handle to name the attempt to ``task resolve``, and a
        second lookup to fetch it could return a different attempt than the
        one being described.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, state, connector, updated_at, task_id
                FROM execution_attempts
                WHERE task_id = ? ORDER BY rowid DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["task_id"]) != task_id:
            # The row does not agree that it belongs to this task, so nothing
            # from it is this task's to show - the id least of all, since
            # that would hand the operator a handle to another task's
            # attempt.
            return AttemptSummary(readable=False)
        # Bounded like every other auxiliary token: an id that is not a plain
        # single-line string is refused outright rather than echoed.
        attempt_id = visibility_text(row["id"])
        try:
            state = AttemptState(row["state"])
        except VISIBILITY_DECODE_ERRORS:
            state = None
        connector = visibility_text(row["connector"])
        updated_at = canonical_utc_timestamp(row["updated_at"])
        if (
            attempt_id is None
            or state is None
            or connector is None
            or updated_at is None
        ):
            # Partial truth is still reported: the state is the one field an
            # operator needs, and withholding a VALID one because a sibling
            # field is corrupt would hide the answer they came for. The id
            # travels under the same rule, for the same reason the approval
            # projection lets a valid request id through a damaged body.
            return AttemptSummary(
                id=attempt_id,
                state=state,
                connector=connector,
                updated_at=updated_at,
                readable=False,
            )
        return AttemptSummary(
            id=attempt_id,
            state=state,
            connector=connector,
            updated_at=updated_at,
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

    # ----------------------------------------------------------------
    # Scheduler phase 1B. DURABLE JOB / GENERATION PERSISTENCE.
    #
    # Storage and management ONLY. Nothing below wakes on a due job, claims a
    # generation, materializes an AgentCore task, creates an approval, invokes
    # a connector, evaluates a predicate, performs an on_true action, starts a
    # worker, takes over a lease or retries an unknown outcome. Every method
    # here runs because a caller called it, and the only caller in this build
    # is a test.
    #
    # The invariants live in SQL, not in Python. Occurrence identity, the
    # one-unsettled-generation rule and the impossible-state refusals are
    # UNIQUE and CHECK constraints on the tables themselves, so a writer that
    # bypassed these methods - a future dispatcher, a script, a hand-edited
    # database - still cannot create a second live generation or a row whose
    # settled status and settlement instant disagree. The Python checks below
    # exist to produce a clear refusal, never as the guarantee.
    # ----------------------------------------------------------------

    @staticmethod
    def _job_flag(value: Any, field_name: str) -> bool:
        """One persisted 0/1 flag as an exact bool, or ``JobError``.

        Deliberately not ``bool(...)``: ``2``, ``-1`` and ``"yes"`` are each
        tampered state that a coercion would turn into a confident ``True``.
        """
        if type(value) is not int or value not in (0, 1):
            raise JobError(f"{field_name} is not an exact persisted flag")
        return value == 1

    @staticmethod
    def _job_instant(value: Any, field_name: str) -> datetime:
        """One canonical persisted UTC timestamp as a datetime, or ``JobError``."""
        if canonical_utc_timestamp(value) is None:
            raise JobError(f"{field_name} is not a canonical UTC timestamp")
        return datetime.fromisoformat(value)

    @staticmethod
    def _job_moment(value: Any, field_name: str) -> datetime:
        """One caller-supplied EXPLICIT instant, or ``JobError``.

        There is no default and no ``utc_now()`` fallback here on purpose. The
        pure schedule engine refuses a naive datetime rather than guessing a
        zone, and scheduling arithmetic that quietly read the machine clock
        would be untestable and, on a machine whose clock is wrong, silently
        wrong too. The caller states the instant it is planning from.
        """
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise JobError(f"{field_name} is not an aware datetime")
        return value

    @staticmethod
    def _next_due(spec: JobSpec, after: datetime) -> str | None:
        """The next instant this schedule names after ``after``, or ``None``.

        Delegated in full to the reviewed pure kernel. No schedule arithmetic
        is reimplemented in storage - a second implementation would be a
        second answer, and the one that fires is always the wrong one.
        """
        occurrence = next_occurrence(spec.schedule, after)
        return None if occurrence is None else occurrence.isoformat()

    @staticmethod
    def _named_occurrence(spec: JobSpec, moment: datetime) -> str | None:
        """The occurrence this instant IS, in the kernel's own spelling.

        Two jobs at once. First, whether the schedule really names the instant
        at all: asked with the same pure kernel and no new arithmetic, an
        instant is named iff the latest occurrence at or before it IS it. That
        refuses an arbitrary caller-chosen moment becoming a durable
        occurrence, and it refuses an instant past a schedule's end, without
        storage ever enumerating anything.

        Second, the SPELLING. The occurrence key is a string, so two different
        renderings of the same instant - a trailing ``.000000``, a non-UTC
        offset - would be two rows for one occurrence, and the uniqueness
        index would never see them collide. So the value that gets persisted
        is the ENGINE'S rendering of the occurrence, never the caller's, and
        every occurrence therefore has exactly one durable spelling.
        """
        try:
            occurrence = latest_occurrence(spec.schedule, moment)
        except ScheduleError:
            return None
        if occurrence is None or occurrence != moment:
            return None
        return occurrence.isoformat()

    @staticmethod
    def _job_from_row(row: sqlite3.Row | None) -> JobRecord | None:
        """Rebuild one persisted job defensively, fail-closed on bad shape.

        A corrupt job is NEVER repaired into a runnable default. An unknown
        kind, a malformed schedule, an unresolvable timezone, an invalid
        revision or generation, an impossible enabled/archived combination, a
        specification that no longer matches its digest, or a scope that
        disagrees with the column the foreign key is on - each one makes the
        row unusable and it loads as ``None``. The row stays physically stored
        exactly as it is; it simply can never be scheduled.
        """
        if row is None:
            return None
        try:
            document = json.loads(row["spec_json"])
            spec = JobSpec.from_dict(document)
            # Three cross-checks between the document and the COLUMNS that
            # storage indexes and constrains on. A document that disagrees
            # with them describes a different job than the one the foreign
            # key, the kind constraint and the digest were written for.
            if spec.project_id != row["project_id"]:
                raise JobError("the persisted scope disagrees with its column")
            if spec.kind.value != row["kind"]:
                raise JobError("the persisted kind disagrees with its column")
            if spec.digest != row["definition_digest"]:
                raise JobError("the persisted specification digest does not match")
            return JobRecord(
                spec=spec,
                id=row["id"],
                definition_version=row["definition_version"],
                revision=row["revision"],
                enabled=SQLiteMemoryStore._job_flag(row["enabled"], "enabled"),
                archived_at=row["archived_at"],
                next_due_at_utc=row["next_due_at_utc"],
                last_run_id=row["last_run_id"],
                next_generation=row["next_generation"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except (TypeError, ValueError, AttributeError, KeyError, IndexError):
            return None

    @staticmethod
    def _job_run_from_row(
        row: sqlite3.Row | None, job_revision: Any = None
    ) -> JobRun | None:
        """Rebuild one persisted generation defensively, fail-closed.

        ``job_revision`` is the OWNING job's current revision when the caller
        read it in the same query. A run claiming a revision the job has never
        reached is inconsistent durable state - the one revision relationship
        a row cannot check about itself - and loads as ``None`` rather than as
        a generation of a definition that does not exist. An OLDER revision is
        ordinary history and is kept.
        """
        if row is None:
            return None
        try:
            spec = JobSpec.from_dict(json.loads(row["spec_json"]))
            if spec.digest != row["snapshot_digest"]:
                raise JobError("the snapshot digest does not match its snapshot")
            if spec.project_id != row["project_id"]:
                raise JobError("the snapshot scope disagrees with its column")
            revision = row["job_revision"]
            if job_revision is not None and (
                type(revision) is not int
                or type(job_revision) is not int
                or revision > job_revision
            ):
                raise JobError("the generation names a revision the job never had")
            return JobRun(
                job_id=row["job_id"],
                job_revision=revision,
                generation=row["generation"],
                nominal_due_at_utc=row["nominal_due_at_utc"],
                spec=spec,
                id=row["id"],
                status=JobRunStatus(row["status"]),
                session_id=row["session_id"],
                task_id=row["task_id"],
                chain_id=row["chain_id"],
                attempt_id=row["attempt_id"],
                owner_token=row["owner_token"],
                fencing_epoch=row["fencing_epoch"],
                lease_expires_at_utc=row["lease_expires_at_utc"],
                cancel_requested=SQLiteMemoryStore._job_flag(
                    row["cancel_requested"], "cancel_requested"
                ),
                blocking_code=row["blocking_code"],
                condition_result=row["condition_result"],
                created_at=row["created_at"],
                claimed_at=row["claimed_at"],
                settled_at=row["settled_at"],
            )
        except (TypeError, ValueError, AttributeError, KeyError, IndexError):
            return None

    #: Every job column a rebuild needs, named once so the four read paths
    #: cannot drift into selecting different subsets.
    _JOB_COLUMNS = (
        "id, project_id, definition_version, revision, kind, spec_json, "
        "definition_digest, next_due_at_utc, enabled, archived_at, "
        "last_run_id, next_generation, created_at, updated_at"
    )

    #: A generation plus the ONE fact it cannot check about itself: its
    #: owning job's current revision. Named once so the five paths that need
    #: the fail-closed rebuild cannot drift into asking different questions.
    _JOB_RUN_WITH_OWNER = (
        "SELECT r.*, j.revision AS owner_revision "
        "FROM job_runs AS r JOIN jobs AS j ON j.id = r.job_id WHERE r.id = ?"
    )

    _JOB_RUN_COLUMNS = (
        "id, job_id, job_revision, generation, nominal_due_at_utc, project_id, "
        "spec_json, snapshot_digest, status, session_id, task_id, chain_id, "
        "attempt_id, owner_token, fencing_epoch, lease_expires_at_utc, "
        "cancel_requested, blocking_code, condition_result, created_at, "
        "claimed_at, settled_at"
    )

    @staticmethod
    def _insert_job(connection: sqlite3.Connection, record: JobRecord) -> None:
        """INSERT one new job row from a validated record.

        The record has already passed its own contract, so every impossible
        combination is gone before SQL sees it; the table's CHECK constraints
        then hold the same line for any writer that did not come through here.
        """
        connection.execute(
            """
            INSERT INTO jobs
                (id, project_id, definition_version, revision, kind, spec_json,
                 definition_digest, next_due_at_utc, enabled, archived_at,
                 last_run_id, next_generation, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.project_id,
                record.definition_version,
                record.revision,
                record.spec.kind.value,
                record.spec.encoded(),
                record.spec.digest,
                record.next_due_at_utc,
                int(record.enabled),
                record.archived_at,
                record.last_run_id,
                record.next_generation,
                record.created_at,
                record.updated_at,
            ),
        )

    @staticmethod
    def _insert_job_run(connection: sqlite3.Connection, run: JobRun) -> None:
        """INSERT one new generation row from a validated run.

        Every association and every coordination field is written as an
        explicit NULL / 0 rather than left to a column default: a generation
        is born unowned, unleased and unmaterialized, and saying so in the
        statement is what keeps a later default change from quietly handing a
        new run an owner.
        """
        try:
            connection.execute(
                """
                INSERT INTO job_runs
                    (id, job_id, job_revision, generation, nominal_due_at_utc,
                     project_id, spec_json, snapshot_digest, status, session_id,
                     task_id, chain_id, attempt_id, owner_token, fencing_epoch,
                     lease_expires_at_utc, cancel_requested, blocking_code,
                     condition_result, created_at, claimed_at, settled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                        NULL, ?, NULL, 0, NULL, NULL, ?, NULL, NULL)
                """,
                (
                    run.id,
                    run.job_id,
                    run.job_revision,
                    run.generation,
                    run.nominal_due_at_utc,
                    run.spec.project_id,
                    run.spec.encoded(),
                    run.snapshot_digest,
                    run.status.value,
                    run.fencing_epoch,
                    run.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise JobError("that generation could not be recorded") from exc

    @staticmethod
    def _update_job(
        connection: sqlite3.Connection, record: JobRecord, *, expected_revision: int
    ) -> None:
        """UPDATE one existing job row, guarded on the revision it replaces.

        An UPDATE and deliberately not an ``INSERT OR REPLACE``: replace is a
        DELETE plus an INSERT, and deleting a job row that generations
        reference would put a foreign key at the mercy of when SQLite happens
        to check it. Nothing about editing a definition should be able to
        touch its history.

        The ``WHERE`` clause carries the compare-and-set a second time, in
        SQL. The Python gate above has already re-read the row inside this
        transaction, so this cannot normally fail - which is the point: if it
        ever does, the write is refused rather than silently applied to a row
        that moved.
        """
        cursor = connection.execute(
            """
            UPDATE jobs
               SET project_id = ?, definition_version = ?, revision = ?,
                   kind = ?, spec_json = ?, definition_digest = ?,
                   next_due_at_utc = ?, enabled = ?, archived_at = ?,
                   last_run_id = ?, next_generation = ?, updated_at = ?
             WHERE id = ? AND revision = ?
            """,
            (
                record.project_id,
                record.definition_version,
                record.revision,
                record.spec.kind.value,
                record.spec.encoded(),
                record.spec.digest,
                record.next_due_at_utc,
                int(record.enabled),
                record.archived_at,
                record.last_run_id,
                record.next_generation,
                record.updated_at,
                record.id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise JobError("stale job revision")

    @staticmethod
    def _require_active_project(
        connection: sqlite3.Connection, project_id: str
    ) -> None:
        row = connection.execute(
            "SELECT active FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None or not row["active"]:
            raise JobError(f"Unknown or inactive project: {project_id}")

    def _job_for_update(
        self, connection: sqlite3.Connection, job_id: str, expected_revision: Any
    ) -> JobRecord:
        """The job this operation may act on, or a deterministic refusal.

        THE compare-and-set gate every definition and state change goes
        through. A caller holds a job it read at some earlier instant; by the
        time it writes, another caller may have edited the definition. Letting
        a stale caller write would be a lost update on the one field that says
        what the job DOES, so the revision it is presenting is re-proved
        against the row inside this transaction, and a mismatch is refused
        having written nothing.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise JobError("expected_revision is not an exact positive integer")
        row = connection.execute(
            f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        record = self._job_from_row(row)
        if record is None:
            # One refusal for a missing job and for an unusable one alike: a
            # corrupted row must not be reported as editable, and which of the
            # two it is is not something this refusal needs to disclose.
            raise JobError("no usable job with that identity")
        if record.revision != expected_revision:
            raise JobError("stale job revision")
        return record

    def create_job(self, spec: JobSpec, *, now: datetime) -> JobRecord:
        """Create ONE durable job definition. Creation is not authority.

        Creating a job materializes no task, requests no approval, consumes no
        grant, selects no connector, reserves no money and starts nothing. It
        records what someone would like done later, and every gate that work
        must pass is still ahead of it, unchanged and re-proved on the trusted
        path when it happens.

        ``now`` is the EXPLICIT instant the first occurrence is calculated
        from, through the pure schedule engine. A schedule that names nothing
        after it - a one-time instant already past, a recurrence past its end
        - yields no next due; the job is created and is simply never
        eligible, which is the honest record of what was asked for.
        """
        if type(spec) is not JobSpec:
            raise JobError("spec is not an exact JobSpec")
        moment = self._job_moment(now, "now")
        record = JobRecord(spec=spec, next_due_at_utc=self._next_due(spec, moment))
        with self._connection(write=True) as connection:
            self._require_active_project(connection, spec.project_id)
            try:
                self._insert_job(connection, record)
            except sqlite3.IntegrityError as exc:
                raise JobError(f"Job already exists: {record.id}") from exc
        return record

    def load_job(self, job_id: str) -> JobRecord | None:
        """One job, or ``None`` when it is missing OR unusable.

        The two collapse deliberately. Everything that would act on a job must
        treat a corrupted definition exactly as it treats an absent one -
        there is nothing to run - and a loader that distinguished them would
        invite a caller to "repair" the difference.
        """
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job_from_row(row)

    def edit_job(
        self, job_id: str, expected_revision: int, spec: JobSpec, *, now: datetime
    ) -> JobRecord:
        """Replace a job's definition under compare-and-set. Revision advances.

        Every execution-relevant field lives in the specification - scope,
        kind, label, schedule, predicate, declaration - so any change to any
        of them produces a new revision, and a caller presenting an older one
        is refused deterministically having written nothing.

        Generations already recorded are NOT touched. Their snapshots stay
        byte-identical at the revision that produced them, because what an
        open occurrence was for is a fact about the past; rewriting it to
        resemble the new definition would make the durable record lie.
        """
        if type(spec) is not JobSpec:
            raise JobError("spec is not an exact JobSpec")
        moment = self._job_moment(now, "now")
        with self._connection(write=True) as connection:
            record = self._job_for_update(connection, job_id, expected_revision)
            if record.archived:
                raise JobError("an archived job cannot be edited")
            self._require_active_project(connection, spec.project_id)
            updated = replace(
                record,
                spec=spec,
                revision=record.revision + 1,
                next_due_at_utc=(
                    self._next_due(spec, moment) if record.enabled else None
                ),
                updated_at=utc_now(),
            )
            self._update_job(
                connection, updated, expected_revision=record.revision
            )
        return updated

    def set_job_enabled(
        self, job_id: str, expected_revision: int, enabled: bool, *, now: datetime
    ) -> JobRecord:
        """Turn future scheduling eligibility on or off. State, not definition.

        DISABLE clears the next due instant, which is what makes it durable
        rather than advisory: a disabled job carries no eligible occurrence at
        all, and the table's CHECK constraint refuses one. It says nothing
        about work already in flight - storage has not cancelled anything and
        does not claim to have.

        ENABLE is an explicit transition that RECALCULATES the next occurrence
        from ``now`` forward. Occurrences the schedule named while the job was
        off are not recovered, and no settled generation is revived: the
        generation counter is monotonic and never reissues a number, and the
        occurrence uniqueness key refuses a second run at an instant this
        revision already recorded.
        """
        if type(enabled) is not bool:
            raise JobError("enabled is not an exact boolean")
        moment = self._job_moment(now, "now")
        with self._connection(write=True) as connection:
            record = self._job_for_update(connection, job_id, expected_revision)
            if record.archived:
                raise JobError("an archived job cannot change enablement")
            updated = replace(
                record,
                enabled=enabled,
                next_due_at_utc=(
                    self._next_due(record.spec, moment) if enabled else None
                ),
                updated_at=utc_now(),
            )
            self._update_job(
                connection, updated, expected_revision=record.revision
            )
        return updated

    def archive_job(self, job_id: str, expected_revision: int) -> JobRecord:
        """Retire a job from future scheduling, keeping every trace of it.

        Archiving disables future eligibility and nothing else. No generation
        is deleted, no snapshot is rewritten, no history is compacted and no
        evidence is removed - the job and all of its runs stay exactly where
        they are and stay readable. There is deliberately no un-archive and no
        delete: this is the end of a job's future, not the erasure of its
        past.
        """
        with self._connection(write=True) as connection:
            record = self._job_for_update(connection, job_id, expected_revision)
            if record.archived:
                raise JobError("the job is already archived")
            updated = replace(
                record,
                enabled=False,
                next_due_at_utc=None,
                archived_at=utc_now(),
                updated_at=utc_now(),
            )
            self._update_job(
                connection, updated, expected_revision=record.revision
            )
        return updated

    def record_job_run(self, job_id: str, nominal_due_at_utc: str) -> JobRun:
        """Record ONE durable generation of a job. Recording is not claiming.

        This creates the occurrence row and nothing else: no ownership is
        taken, no lease is written, no task is materialized, no approval
        exists and no connector is reached. The generation is born
        ``PENDING`` and stays there until something that does not exist yet
        settles it.

        Three things are proved in the one transaction before the row exists:
        the job is usable and schedulable (a disabled or archived job records
        nothing); the instant really is one the schedule NAMES, asked of the
        pure kernel rather than trusted from the caller; and no unsettled
        generation is already open. The last two are also UNIQUE indexes, so
        the guarantee survives a writer that never came through here.
        """
        moment = self._job_instant(nominal_due_at_utc, "nominal_due_at_utc")
        with self._connection(write=True) as connection:
            row = connection.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            record = self._job_from_row(row)
            if record is None:
                raise JobError("no usable job with that identity")
            if not record.schedulable:
                raise JobError("a job that is not schedulable records no generation")
            # The occurrence is stored in the ENGINE'S spelling, so one instant
            # can never become two rows by being rendered two ways.
            nominal = self._named_occurrence(record.spec, moment)
            if nominal is None:
                raise JobError("the schedule does not name that instant")
            open_run = connection.execute(
                "SELECT id FROM job_runs WHERE job_id = ? AND settled_at IS NULL",
                (job_id,),
            ).fetchone()
            if open_run is not None:
                raise JobError("an unsettled generation of this job is already open")
            duplicate = connection.execute(
                "SELECT id FROM job_runs WHERE job_id = ? AND job_revision = ? "
                "AND nominal_due_at_utc = ?",
                (job_id, record.revision, nominal),
            ).fetchone()
            if duplicate is not None:
                raise JobError("that occurrence has already been recorded")
            run = JobRun(
                job_id=record.id,
                job_revision=record.revision,
                generation=record.next_generation,
                nominal_due_at_utc=nominal,
                spec=record.spec,
            )
            self._insert_job_run(connection, run)
            # Bookkeeping only: the monotonic counter advances, the last-run
            # pointer moves, and the next due instant becomes the next one
            # this schedule names AFTER the occurrence just recorded - all
            # from the pure kernel, none of it a decision to run anything.
            self._update_job(
                connection,
                replace(
                    record,
                    last_run_id=run.id,
                    next_generation=record.next_generation + 1,
                    next_due_at_utc=self._next_due(record.spec, moment),
                    updated_at=utc_now(),
                ),
                expected_revision=record.revision,
            )
        return run

    # ----------------------------------------------------------------
    # Scheduler phase 2A. DUE GENERATION + CLAIM / LEASE / FENCING.
    #
    # Coordination, and only coordination. Everything below decides what is
    # due, which durable generation exists and which worker currently owns a
    # bounded lease on it. NONE of it materializes an AgentCore task, creates
    # an approval, consumes a grant, opens an execution attempt, creates a
    # reservation, reaches a connector, evaluates a predicate, performs an
    # on_true action, starts a thread or retries an unknown outcome. A worker
    # holding a valid lease has taken a place in a queue; it has been granted
    # nothing, and every authority gate real work must pass is still ahead of
    # it, unchanged.
    #
    # Atomicity comes from two things together, not from either alone. Each
    # method runs inside BEGIN IMMEDIATE, so writers serialize; and each
    # UPDATE carries the state it read in its own WHERE clause, so a write
    # applied to a row that moved is refused rather than silently landing.
    # That is what makes two competing workers resolve to exactly one winner
    # with exactly one fencing epoch.
    # ----------------------------------------------------------------

    @staticmethod
    def _lease_expiry(lease: Any, now: datetime) -> str:
        """The canonical UTC instant a lease of this length would stop.

        The bounds are REFUSED, never clamped. A caller that asked for a lease
        the architecture does not offer has a bug, and quietly substituting a
        different expiry would leave it believing it owns a generation for a
        window it does not - which is exactly the belief a fencing epoch
        exists to make unfalsifiable.
        """
        if type(lease) is not timedelta:
            raise JobError("lease is not a timedelta")
        if lease < MIN_JOB_LEASE or lease > MAX_JOB_LEASE:
            raise JobError(
                f"lease is outside the approved {MIN_JOB_LEASE} .. "
                f"{MAX_JOB_LEASE} window"
            )
        return (now + lease).isoformat()

    def _owned_run(self, connection: sqlite3.Connection, run_id: str) -> JobRun:
        """One generation rebuilt fail-closed, or a deterministic refusal.

        A corrupted generation - including one carrying half a lease - is
        refused exactly as a missing one is. It stays physically stored and
        stays unclaimable, which is the safe direction: an unreadable row is
        one whose external history nobody can establish.
        """
        row = connection.execute(self._JOB_RUN_WITH_OWNER, (run_id,)).fetchone()
        run = (
            None if row is None else self._job_run_from_row(row, row["owner_revision"])
        )
        if run is None:
            raise JobError("no usable generation with that identity")
        return run

    @staticmethod
    def _held_lease(
        run: JobRun, owner_token: Any, fencing_epoch: Any, now: datetime
    ) -> None:
        """Refuse unless the caller can PROVE it is the current owner NOW.

        Three independent proofs, all required.

        The TOKEN must match exactly - not a prefix, not a normalization. The
        FENCING EPOCH must match too, which is the whole point of having one:
        a worker displaced by a takeover still remembers its token, and a
        token alone would let it write over its successor. And the lease must
        still be LIVE, because once it has expired another worker may
        legitimately have taken the run, and no store can un-expire it after
        the fact.
        """
        if type(fencing_epoch) is not int or isinstance(fencing_epoch, bool):
            raise JobError("fencing_epoch is not an exact integer")
        if run.settled:
            raise JobError("the generation is already settled")
        if run.owner_token is None:
            raise JobError("the generation carries no lease")
        if run.owner_token != owner_token or run.fencing_epoch != fencing_epoch:
            raise JobError("the caller does not hold this lease")
        if not run.lease_is_live(now):
            raise JobError("the lease has expired")

    @staticmethod
    def _lease_result(run: JobRun) -> JobRunLease:
        """The immutable receipt for a run whose lease was just written."""
        return JobRunLease(
            run_id=run.id,
            job_id=run.job_id,
            job_revision=run.job_revision,
            generation=run.generation,
            owner_token=run.owner_token,
            fencing_epoch=run.fencing_epoch,
            lease_expires_at_utc=run.lease_expires_at_utc,
            claimed_at=run.claimed_at,
        )

    def materialize_due_job_run(
        self, job_id: str, expected_revision: int, *, now: datetime
    ) -> JobRun | None:
        """Create the next durable generation IF this job is due at ``now``.

        MISSED OCCURRENCES ARE COALESCED, and the coalescing is what keeps the
        work bounded. The occurrence created is ``latest_occurrence`` - the
        single latest instant the schedule names at or before ``now`` - asked
        ONCE of the pure kernel. A machine offline for a year therefore
        creates exactly one generation and costs exactly what a machine
        offline for a minute costs; nothing here enumerates a missed history,
        so no backlog can exist to be unbounded.

        The three kinds fall out of that one rule rather than needing three:

        * ONE_TIME materializes at most once EVER - after its single instant
          the schedule names nothing later, the stored next due becomes NULL,
          and the occurrence key would refuse a repeat even if it did not;
        * RECURRING skips straight to the newest due occurrence;
        * CONDITION_WATCH gets at most one fresh CHECK OPPORTUNITY, and
          nothing here evaluates its predicate, reads its target, produces a
          TRUE/FALSE/UNKNOWN or performs its on_true declaration.

        ``None`` is the ordinary "nothing to do" answer: not schedulable, not
        yet due, a generation already open, or this occurrence already
        recorded. ``JobError`` is reserved for a caller whose own premise is
        wrong - an unusable job, or a revision the definition has moved past.
        """
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        with self._connection(write=True) as connection:
            # The same compare-and-set gate every definition change goes
            # through. A caller decided this job was due from a revision it
            # read earlier; if the definition moved in between, materializing
            # would record an occurrence of a job that no longer exists in
            # that form.
            record = self._job_for_update(connection, job_id, expected_revision)
            if not record.schedulable or record.next_due_at_utc is None:
                return None
            due = datetime.fromisoformat(record.next_due_at_utc)
            if moment < due:
                return None
            try:
                occurrence = latest_occurrence(record.spec.schedule, moment)
            except ScheduleError as exc:
                # A schedule that cannot answer must not read as "not due".
                raise JobError("the schedule could not name an occurrence") from exc
            if occurrence is None or occurrence < due:
                return None
            # The engine own spelling, so one instant can never become two
            # rows by being rendered two ways.
            nominal = occurrence.isoformat()
            open_run = connection.execute(
                "SELECT id FROM job_runs WHERE job_id = ? AND settled_at IS NULL",
                (job_id,),
            ).fetchone()
            if open_run is not None:
                # The one-unsettled-generation rule stays authoritative, and
                # it is not an error here: a job whose previous occurrence is
                # still open simply has nothing new to materialize. An
                # UNSETTLED uncertain outcome therefore blocks every later
                # generation, which is the intended fail-closed behaviour.
                return None
            duplicate = connection.execute(
                "SELECT id FROM job_runs WHERE job_id = ? AND job_revision = ? "
                "AND nominal_due_at_utc = ?",
                (job_id, record.revision, nominal),
            ).fetchone()
            if duplicate is not None:
                return None
            run = JobRun(
                job_id=record.id,
                job_revision=record.revision,
                generation=record.next_generation,
                nominal_due_at_utc=nominal,
                spec=record.spec,
            )
            self._insert_job_run(connection, run)
            # Advancing the anchor is TRANSACTIONAL with creating the
            # generation - the same immediate transaction - so a crash can
            # never leave a job whose occurrence exists but whose next due
            # still points at it.
            self._update_job(
                connection,
                replace(
                    record,
                    last_run_id=run.id,
                    next_generation=record.next_generation + 1,
                    next_due_at_utc=self._next_due(record.spec, occurrence),
                    updated_at=utc_now(),
                ),
                expected_revision=record.revision,
            )
        return run

    def claim_job_run(
        self,
        run_id: str,
        owner_token: str,
        *,
        now: datetime,
        lease: timedelta,
    ) -> JobRunLease:
        """Take exclusive coordination ownership of ONE generation, atomically.

        CLAIMING IS NOT AUTHORITY. It creates no task, no approval, no grant
        consumption, no execution attempt and no reservation, and it reaches
        no connector. It answers one question - which worker coordinates this
        occurrence - and the answer expires.

        Two competing workers cannot both win. The row is re-read inside the
        immediate transaction and the UPDATE names the fencing epoch it
        expects, so the second writer finds either a live lease or an epoch
        that has already moved, and is refused. The winner epoch is strictly
        greater than every epoch this run has ever had.

        A generation whose external outcome could be in doubt is NOT claimable
        - see ``CLAIMABLE_JOB_RUN_STATUSES``. An expired lease is evidence
        about coordination, never evidence that the previous owner external
        action did not happen.
        """
        if chain_id_refusal(owner_token) is not None:
            raise JobError("owner_token is not a usable identifier")
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        expires = self._lease_expiry(lease, moment)
        claimed_at = moment.isoformat()
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            if not run.claimable_at(moment):
                # ONE refusal for every unclaimable shape - settled, already
                # live-leased, RUNNING, UNKNOWN_OUTCOME, AWAITING_APPROVAL -
                # so a caller cannot probe a run coordination state through
                # the differences between refusals.
                raise JobError("the generation is not claimable")
            claimed = replace(
                run,
                status=JobRunStatus.CLAIMED,
                owner_token=owner_token,
                fencing_epoch=run.fencing_epoch + 1,
                lease_expires_at_utc=expires,
                claimed_at=claimed_at,
            )
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, owner_token = ?, fencing_epoch = ?,
                       lease_expires_at_utc = ?, claimed_at = ?
                 WHERE id = ? AND fencing_epoch = ? AND settled_at IS NULL
                   AND cancel_requested = 0
                """,
                (
                    claimed.status.value,
                    claimed.owner_token,
                    claimed.fencing_epoch,
                    claimed.lease_expires_at_utc,
                    claimed.claimed_at,
                    run_id,
                    run.fencing_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("the generation is not claimable")
        return self._lease_result(claimed)

    def renew_job_run_lease(
        self,
        run_id: str,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
        lease: timedelta,
    ) -> JobRunLease:
        """Extend a lease the caller can still PROVE it currently holds.

        The renewal moves the EXPIRY and nothing else. The generation, the job
        revision, the immutable snapshot, the nominal due instant and the
        fencing epoch are all left exactly as they are - the epoch marks an
        ACQUISITION of ownership, and renewing is not acquiring, so an epoch
        that advanced on renewal would fence out the very worker renewing it.
        """
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        expires = self._lease_expiry(lease, moment)
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            self._held_lease(run, owner_token, fencing_epoch, moment)
            renewed = replace(run, lease_expires_at_utc=expires)
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET lease_expires_at_utc = ?
                 WHERE id = ? AND owner_token = ? AND fencing_epoch = ?
                   AND settled_at IS NULL
                """,
                (expires, run_id, owner_token, fencing_epoch),
            )
            if cursor.rowcount != 1:
                raise JobError("the caller does not hold this lease")
        return self._lease_result(renewed)

    def release_job_run(
        self,
        run_id: str,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
    ) -> JobRun:
        """Voluntarily give up a lease, so the next worker need not wait it out.

        RELEASING REWRITES NO EXTERNAL REALITY. It is not a cancellation, not
        a settlement and not a claim that anything was undone; the generation
        goes back to ``PENDING`` and stays exactly as unfinished as it was. It
        is permitted only from the same narrow coordination states a claim is
        permitted from, so a run that may have started something outside this
        database can never be released into looking untouched.

        The fencing epoch is DELIBERATELY not rolled back. It is the durable
        record that an ownership happened, the released owner token is
        unusable afterwards, and the next claimant epoch is strictly greater
        than this one - a rollback would let a stale owner proof match again.
        """
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            self._held_lease(run, owner_token, fencing_epoch, moment)
            if run.status not in CLAIMABLE_JOB_RUN_STATUSES:
                raise JobError("the generation is not releasable")
            released = replace(
                run,
                status=JobRunStatus.PENDING,
                owner_token=None,
                lease_expires_at_utc=None,
            )
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, owner_token = NULL,
                       lease_expires_at_utc = NULL
                 WHERE id = ? AND owner_token = ? AND fencing_epoch = ?
                   AND settled_at IS NULL
                """,
                (released.status.value, run_id, owner_token, fencing_epoch),
            )
            if cursor.rowcount != 1:
                raise JobError("the caller does not hold this lease")
        return released

    def settle_job_run(
        self,
        run_id: str,
        status: JobRunStatus,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
        blocking_code: str | None = None,
        condition_result: str | None = _PRESERVE_CONDITION_RESULT,
    ) -> JobRun:
        """Close ONE open generation with a settled outcome, FENCED.

        Settling is the storage transition that lets the NEXT generation
        exist, so it is the last place an unfenced write could do damage: a
        worker whose lease expired while its successor took over would, with
        no proof required, write an ending over a run somebody else is working
        and unblock the job on evidence nobody holds. It therefore demands
        exactly what every other ownership mutation demands - the exact token,
        the exact fencing epoch, and a lease still LIVE at the caller's
        explicit ``now`` - and there is deliberately no path around it.

        A settled generation is never reopened: the compare-and-set names the
        owner, the epoch and ``settled_at IS NULL``, so two settlers cannot
        both win and the settled half of the vocabulary has no way back out.

        ``condition_result`` defaults to PRESERVING whatever the run already
        holds - see ``_PRESERVE_CONDITION_RESULT`` - rather than to ``None``.
        A phase-2C watch's TRUE record is written once, at bind time, and this
        is the SAME method every job kind's later COMPLETED/FAILED/CANCELLED
        settlement reaches through the shared reflection; a caller settling an
        ordinary job's chain status has no reason to know a predicate was ever
        asked, and must not silently erase the record of one by omission. Pass
        an explicit value only to WRITE the result for the first time, from an
        unbound FALSE/UNKNOWN watch.
        """
        if type(status) is not JobRunStatus:
            raise JobError("status is not an exact JobRunStatus")
        if status not in SETTLED_JOB_RUN_STATUSES:
            raise JobError("a generation settles only into a settled status")
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        with self._connection(write=True) as connection:
            # ``_owned_run`` joins to the owning job for exactly the reason the
            # loader does: a generation naming a revision its job never had is
            # inconsistent durable state, and settling it would be writing an
            # outcome for something that was never a real occurrence.
            run = self._owned_run(connection, run_id)
            self._held_lease(run, owner_token, fencing_epoch, moment)
            if run.status not in SETTLEABLE_JOB_RUN_STATUSES:
                raise JobError("the generation is not settleable from its state")
            resolved_condition_result = (
                run.condition_result
                if condition_result is _PRESERVE_CONDITION_RESULT
                else condition_result
            )
            settled = replace(
                run,
                status=status,
                blocking_code=blocking_code,
                condition_result=resolved_condition_result,
                settled_at=moment.isoformat(),
            )
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, blocking_code = ?, condition_result = ?,
                       settled_at = ?
                 WHERE id = ? AND owner_token = ? AND fencing_epoch = ?
                   AND settled_at IS NULL
                """,
                (
                    settled.status.value,
                    settled.blocking_code,
                    settled.condition_result,
                    settled.settled_at,
                    run_id,
                    owner_token,
                    fencing_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("the caller does not hold this lease")
        return settled

    def _fenced_transition(
        self,
        run_id: str,
        owner_token: Any,
        fencing_epoch: Any,
        now: datetime,
        *,
        sources: frozenset,
        target: JobRunStatus,
        refusal: str,
    ) -> JobRun:
        """ONE fenced UNSETTLED state change that also drops the lease.

        Shared by the two phase-2B0 transitions because they differ only in
        their source set and their target, and a second hand-written copy of
        the proof-plus-CAS would be a second place for the fencing to drift.

        Both leave the generation UNSETTLED, so the one-unsettled-generation
        rule keeps every later generation of the job blocked behind it, and
        both leave the fencing epoch and the claim instant exactly as they
        are: the epoch is the durable evidence that an ownership happened, and
        rolling it back would let the very proof this call just consumed match
        again.
        """
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            self._held_lease(run, owner_token, fencing_epoch, moment)
            if run.status not in sources:
                raise JobError(refusal)
            moved = replace(
                run,
                status=target,
                owner_token=None,
                lease_expires_at_utc=None,
            )
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, owner_token = NULL,
                       lease_expires_at_utc = NULL
                 WHERE id = ? AND owner_token = ? AND fencing_epoch = ?
                   AND status = ? AND settled_at IS NULL
                """,
                (
                    target.value,
                    run_id,
                    owner_token,
                    fencing_epoch,
                    run.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("the caller does not hold this lease")
        return moved

    def mark_job_run_unknown(
        self,
        run_id: str,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
    ) -> JobRun:
        """Record that this generation's external outcome is UNESTABLISHABLE.

        UNKNOWN_OUTCOME IS NOT AN ENDING. It stays unsettled, so this job's
        every later generation stays blocked behind it; it is not claimable,
        so no worker reclaims it and no expiry turns it into a second attempt.
        That is the whole point - a lease running out is evidence about
        coordination, never evidence that the external action did not happen,
        and retrying on it is exactly how a duplicate real-world effect gets
        made.

        Resolving one is a human authority gate that does not exist in this
        build. Nothing here settles, retries or resumes it.
        """
        return self._fenced_transition(
            run_id,
            owner_token,
            fencing_epoch,
            now,
            sources=UNKNOWNABLE_JOB_RUN_STATUSES,
            target=JobRunStatus.UNKNOWN_OUTCOME,
            refusal="the generation has no attempt whose outcome could be unknown",
        )

    def await_job_run_approval(
        self,
        run_id: str,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
    ) -> JobRun:
        """Park this generation pending a human answer, releasing the lease.

        THIS CREATES NO APPROVAL AND CONSUMES NO GRANT. It writes one
        coordination state and clears one lease; it requests nothing, answers
        nothing, reserves nothing and reaches no connector. The waiting is for
        a person, so holding an expiring lease across it would mean either an
        expiry that means nothing or a renewal loop that never ends.

        The source is ``RUNNING``: an attempt DISCOVERS that it needs a human,
        it does not know beforehand. A future caller therefore reaches this
        after its chain has already returned an awaiting-approval answer, and
        the ``now`` it passes must be read at THAT point - never a timestamp
        captured before dispatch, which would judge lease liveness against an
        instant that has since passed.

        ``AWAITING_APPROVAL`` is unsettled and unclaimable, so the generation
        stays blocked - as does every later generation of the job - until the
        approval surface a later phase builds answers it.
        """
        return self._fenced_transition(
            run_id,
            owner_token,
            fencing_epoch,
            now,
            sources=APPROVAL_PENDABLE_JOB_RUN_STATUSES,
            target=JobRunStatus.AWAITING_APPROVAL,
            refusal="the generation is not awaiting-approval from its state",
        )

    def bind_job_run_dispatch(
        self,
        run_id: str,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
    ) -> JobRunDispatch:
        """Bind ONE claimed generation to a fresh session/chain, atomically.

        The FIRST and ONLY binding this generation ever gets, and the whole of
        it is one transaction: the pristine session, the chain declared from the
        immutable snapshot, both binding columns and the ``CLAIMED -> RUNNING``
        transition either all become durable together or none of them does. A
        crash anywhere inside leaves no orphan session, no orphan chain, no half
        binding and no generation that says RUNNING with nothing to run.

        THE BINDING IS IMMUTABLE. The compare-and-set names both slots as NULL,
        so a second binder finds them filled and is refused - it never replaces
        either identity, and it never gets a second session for one occurrence.
        The unique partial index on ``session_id`` holds the other direction:
        no two generations can name one session even if a writer skipped this
        method.

        The session row is a STRICT INSERT. An upsert would silently adopt an
        existing session and hand this generation someone else's chain, which
        is the one way a scheduler could reach into a conversation it has no
        business in; a colliding id therefore raises rather than merges.

        A ``CONDITION_WATCH`` is refused. Its declaration is what it would do IF
        its predicate held, nothing here evaluates one, and dispatching it would
        run unconditionally what the operator declared conditionally.
        ``bind_condition_true_dispatch`` is the one path that may bind a watch,
        and only after its predicate was found TRUE outside this store.

        Binding is NOT authority. It creates no approval, consumes no grant,
        opens no execution attempt, reserves no money and reaches no connector.
        Everything real still has to pass Core's own gates afterwards.
        """
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            self._held_lease(run, owner_token, fencing_epoch, moment)
            if run.spec.kind is JobKind.CONDITION_WATCH:
                raise JobError(
                    "a condition watch is not dispatchable in this phase"
                )
            return self._bind_dispatch(
                connection,
                run,
                owner_token,
                fencing_epoch,
                moment,
                declared=run.spec.steps,
                condition_result=None,
            )

    def bind_condition_true_dispatch(
        self,
        run_id: str,
        owner_token: str,
        fencing_epoch: int,
        *,
        now: datetime,
    ) -> JobRunDispatch:
        """Phase 2C. Bind ONE claimed watch to its ``on_true`` declaration.

        The mirror image of ``bind_job_run_dispatch``: identical proof,
        identical source-state and empty-slot requirements, identical one-
        transaction session-plus-chain-plus-binding atomicity, built by the
        SAME shared core - ``_bind_dispatch`` - so there is exactly one
        implementation of what binding a generation means. The only two
        differences are what it refuses and what it declares: this path
        requires ``CONDITION_WATCH`` rather than refusing it, and the chain it
        declares comes from ``JobSpec.on_true`` rather than ``JobSpec.steps``,
        with ``condition_result = "true"`` persisted in the same write.

        Whether the predicate really was TRUE is entirely the CALLER'S proof,
        made before this is ever reached, over a hardened observation this
        method never sees and never re-derives. Calling it is itself the only
        authority a TRUE evaluation ever needed to carry.
        """
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            self._held_lease(run, owner_token, fencing_epoch, moment)
            if run.spec.kind is not JobKind.CONDITION_WATCH:
                raise JobError(
                    "only a condition watch may bind through this path"
                )
            return self._bind_dispatch(
                connection,
                run,
                owner_token,
                fencing_epoch,
                moment,
                declared=run.spec.on_true,
                condition_result=ConditionResult.TRUE.value,
            )

    def _bind_dispatch(
        self,
        connection: sqlite3.Connection,
        run: JobRun,
        owner_token: str,
        fencing_epoch: int,
        moment: datetime,
        *,
        declared: tuple,
        condition_result: str | None,
    ) -> JobRunDispatch:
        """The one binding implementation both dispatch paths share.

        Callers have already proved the lease and decided which declaration
        and which ``condition_result`` this generation's kind permits; this
        does the actual atomic create-session / declare-chain / bind / CAS,
        identically either way.
        """
        run_id = run.id
        if run.status not in DISPATCHABLE_JOB_RUN_STATUSES:
            raise JobError("the generation is not dispatchable from its state")
        if run.cancel_requested:
            # Somebody asked for this occurrence to stop. Binding it would
            # create the session, the chain and the RUNNING state whose
            # only purpose would be to be cancelled again.
            raise JobError("the generation has been asked to cancel")
        if run.session_id is not None or run.chain_id is not None:
            # Already bound. Refused rather than re-bound: a second session
            # for one occurrence would be a second crossing of a connector
            # boundary whose first crossing nobody has disproved.
            raise JobError("the generation is already bound to a session")
        # The project is re-proved HERE, inside the write transaction: a
        # snapshot names the project the operator scheduled under, and a
        # project archived since then must not gain a fresh session.
        self._require_active_project(connection, run.spec.project_id)
        chain = ChainState.declared(declared)
        if chain is None:
            # Unreachable through JobSpec, which already refuses a
            # malformed declaration. Fail closed anyway rather than bind a
            # session to a chain no authority stage would accept.
            raise JobError("the snapshot declaration cannot form a chain")
        session = SessionState(
            objective=run.spec.label,
            project_id=run.spec.project_id,
            chain=chain,
        )
        payload = session.to_dict()
        try:
            # STRICT INSERT, no ON CONFLICT: a colliding session id is a
            # session that already exists and is not ours to write into.
            connection.execute(
                """
                INSERT INTO sessions (id, state_json, updated_at, project_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    session.id,
                    self._json(payload),
                    session.updated_at,
                    session.project_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise JobError("the session identity is already taken") from exc
        bound = replace(
            run,
            status=JobRunStatus.RUNNING,
            session_id=session.id,
            chain_id=chain.id,
            condition_result=condition_result,
        )
        try:
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, session_id = ?, chain_id = ?,
                       condition_result = ?
                 WHERE id = ? AND owner_token = ? AND fencing_epoch = ?
                   AND status = ? AND settled_at IS NULL
                   AND session_id IS NULL AND chain_id IS NULL
                   AND cancel_requested = 0
                """,
                (
                    bound.status.value,
                    session.id,
                    chain.id,
                    condition_result,
                    run_id,
                    owner_token,
                    fencing_epoch,
                    run.status.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # The unique partial index: this session already belongs to
            # another generation. The whole transaction unwinds, so the
            # session row inserted above never becomes durable.
            raise JobError("that session is already bound to a generation") from exc
        if cursor.rowcount != 1:
            raise JobError("the caller does not hold this lease")
        return JobRunDispatch(
            run_id=bound.id,
            job_id=bound.job_id,
            generation=bound.generation,
            project_id=bound.spec.project_id,
            session_id=session.id,
            chain_id=chain.id,
            owner_token=owner_token,
            fencing_epoch=fencing_epoch,
            lease_expires_at_utc=bound.lease_expires_at_utc,
        )

    @staticmethod
    def _insert_job_run_event(
        connection: sqlite3.Connection,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        """One generation-scoped audit row, on the CALLER'S transaction.

        PRIVATE, and deliberately so. It takes an already-open connection
        rather than opening one, which is what lets it write inside the very
        transaction performing the mutation it describes - and which is also
        why it is not, and must not become, a public "record an event"
        operation. A public one would be an audit authority callable without
        the mutation, or worse, after it.

        There is no loop checkpoint here, unlike the task-scoped writer: a
        generation has no ``LoopStage``, and inventing one would put a stage
        that never happened into the task journal. ``task_id`` is NULL because
        the subject is the occurrence itself; the schema has always allowed it.
        """
        connection.execute(
            """
            INSERT INTO event_log
                (session_id, task_id, event_type, payload_json, created_at)
            VALUES (?, NULL, ?, ?, ?)
            """,
            (session_id, event_type, SQLiteMemoryStore._json(payload), created_at),
        )

    @staticmethod
    def _recovery_session(
        connection: sqlite3.Connection, run: JobRun
    ) -> SessionState:
        """The bound session, proved to BE the bound session - or raise.

        Three separate things have to name one session before anything decoded
        from it may be believed, and only the first is what the lookup already
        guarantees:

        * the GENERATION says which session it is bound to;
        * the sessions ROW KEY is what the row was found under;
        * the DOCUMENT inside that row asserts its own identity.

        A row written by the ordinary path always agrees with itself - every
        writer keys the row by ``session.id`` and stores the project alongside
        it - so a disagreement is corrupted or forged durable state, and it is
        the dangerous direction: Core works from the DECODED object, so a
        document stored under this generation's session but claiming to be
        another one would materialize tasks, open attempts and cross connector
        boundaries inside a session this generation was never bound to, while
        the generation still read as bound here.

        The PROJECT is proved the same way and for the same reason. The column
        is what every project-scoped query answers from; the document carries
        its own copy; a generation whose two answers differ has no provable
        project, and this boundary is already re-proving project identity.

        Nothing is repaired, substituted or followed. The identity the document
        names is never loaded, neither identity is ever rewritten, and a
        mismatch refuses the whole recovery before any mutation - so no
        ownership moves and no success is recorded.
        """
        row = connection.execute(
            "SELECT id, state_json, project_id FROM sessions WHERE id = ?",
            (run.session_id,),
        ).fetchone()
        if row is None:
            raise JobError("the bound session is unreadable")
        try:
            session = SessionState.from_dict(json.loads(row["state_json"]))
        except Exception as exc:  # noqa: BLE001 - unparseable is unusable
            raise JobError("the bound session is unreadable") from exc
        if type(session) is not SessionState:
            raise JobError("the bound session is unreadable")
        # The row key is what it was found under; the document says what it is.
        if session.id != row["id"] or session.id != run.session_id:
            raise JobError("the persisted session is not the bound session")
        if session.project_id != row["project_id"]:
            raise JobError(
                "the persisted session and its project record disagree"
            )
        return session

    @staticmethod
    def _recovery_cursor_task(
        connection: sqlite3.Connection, session: SessionState, run: JobRun
    ) -> str:
        """The task this generation's chain is CURRENTLY on, proved - or raise.

        Takes the session ``_recovery_session`` has ALREADY proved to be the
        bound one, so nothing here is derived from an identity that was merely
        asserted. Every remaining link is checked rather than assumed: the
        chain is the exact chain this generation is bound to, its cursor is a
        real index into its own routing, that step routes to a task id, and
        that task row exists and belongs to THIS session. A break anywhere
        raises, which fails the recovery closed.

        Nothing here reads a task's status, a grant, or an approval outcome. It
        answers one question - which task is the chain on - so that the
        approval WAIT rule has something exact to ask about.
        """
        chain = session.chain
        if chain is None or chain.id != run.chain_id:
            raise JobError("the bound chain does not match the generation")
        cursor = chain.cursor
        if type(cursor) is not int or not 0 <= cursor < len(chain.step_task_ids):
            raise JobError("the bound chain has no usable cursor")
        task_id = chain.step_task_ids[cursor]
        if not isinstance(task_id, str) or not task_id:
            raise JobError("the chain's current step routes to no task")
        owner = connection.execute(
            "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if owner is None or owner[0] != run.session_id:
            raise JobError("the chain's current step is not this session's task")
        return task_id

    def reacquire_job_run(
        self,
        run_id: str,
        owner_token: str,
        expected_fencing_epoch: int,
        *,
        now: datetime,
        lease: timedelta,
        actor_label: str,
        reason: str,
    ) -> JobRunLease:
        """Recover coordination ownership of an ALREADY-BOUND generation.

        Phase 2B2a, and the narrowest thing that unsticks a crashed or parked
        occurrence without ever becoming a retry. ONE transaction: prove, move
        the lease, record why, commit.

        THE OBSERVED EPOCH IS THE FENCE. ``expected_fencing_epoch`` is what the
        caller actually looked at, and the compare-and-set names it, so the
        stale-observer race closes: A reads epoch N; B recovers to N+1 and then
        expires; A - whose lease check would now pass, because the lease it
        would be taking over really is expired - is refused anyway, because the
        run it holds evidence about is not the run in front of it. An expiry
        can never be that fence; only the epoch can.

        THE BINDING IS UNTOUCHABLE. Both binding columns must already be set,
        and the UPDATE both requires and re-states their exact values, so no
        path through here creates a session, creates a chain, replaces either
        identity, or gives one occurrence a second session. Recovery hands the
        SAME session back; that is the whole reason it is safe where a claim
        would not be, and why ``CLAIMABLE_JOB_RUN_STATUSES`` is untouched and
        this has a source set of its own.

        THE PARKED CASE WAITS, IT DOES NOT INTERPRET. For an
        ``AWAITING_APPROVAL`` generation the chain's current routed task is
        resolved and its pending request is looked for. Still pending means a
        person is mid-answer, and recovery is REFUSED. Not pending means only
        that nobody is being asked right now - it is NOT an approval, it is not
        read as one, no grant is consulted, and the task is deliberately NOT
        required to have left ``AWAITING_APPROVAL``: Core resolves an approval
        and checkpoints the task in two steps, so a worker that died between
        them legitimately leaves exactly that shape. What the approval came to
        is Core's answer, given when the session is re-entered, and Core asking
        again is a correct outcome rather than a degraded one.

        THE RECORD IS PART OF THE MOVE. The audit row is inserted on this same
        transaction, so ownership and the evidence for it commit together: a
        failed insert rolls the recovery back, and a failed compare-and-set
        never reaches it. Two racing recoverers produce one winner and exactly
        one event, because ``BEGIN IMMEDIATE`` serializes them and the loser's
        expected epoch has moved.

        Recovering is NOT authority. It creates no approval and answers none,
        mints and consumes no grant, opens, advances, abandons or concludes no
        execution attempt, reserves no money, reaches no connector and starts
        nothing. It writes one lease and one audit row.
        """
        if chain_id_refusal(owner_token) is not None:
            raise JobError("owner_token is not a usable identifier")
        if (
            type(expected_fencing_epoch) is not int
            or isinstance(expected_fencing_epoch, bool)
        ):
            raise JobError("fencing_epoch is not an exact integer")
        # Bounded, sanitized and credential-redacted BEFORE anything is
        # written, and refused when blank: an authority action whose record
        # says nothing about who asked or why is not worth committing.
        actor = job_recovery_note(
            actor_label, "actor_label", JOB_RECOVERY_ACTOR_MAX_CHARS
        )
        why = job_recovery_note(reason, "reason", JOB_RECOVERY_REASON_MAX_CHARS)
        moment = self._job_moment(now, "now").astimezone(timezone.utc)
        expires = self._lease_expiry(lease, moment)
        claimed_at = moment.isoformat()
        with self._connection(write=True) as connection:
            run = self._owned_run(connection, run_id)
            if run.settled:
                raise JobError("the generation is already settled")
            if run.status not in REACQUIRABLE_JOB_RUN_STATUSES:
                raise JobError("the generation is not recoverable from its state")
            if run.session_id is None or run.chain_id is None:
                # Unbound. Recovery exists only because the session already
                # does; an unbound generation is the generic claim path's
                # business, and taking it here would be a dispatch in disguise.
                raise JobError("the generation is not bound to a session")
            if run.lease_is_live(moment):
                # Half-open, the house's one definition: the instant of expiry
                # is already expired, and a live owner is never displaced.
                raise JobError("the generation still has a live owner")
            # THE BOUND SESSION IS PROVED FIRST, and on EVERY recovery
            # rather than only the parked one: the identity question is not a
            # side effect of the approval rule, and a RUNNING generation is
            # handed to Core through exactly the same decoded document.
            session = self._recovery_session(connection, run)
            cursor_task_id: str | None = None
            if run.status is JobRunStatus.AWAITING_APPROVAL:
                cursor_task_id = self._recovery_cursor_task(
                    connection, session, run
                )
                pending = connection.execute(
                    """
                    SELECT 1 FROM pending_approvals
                     WHERE task_id = ? AND session_id = ? AND status = 'pending'
                     LIMIT 1
                    """,
                    (cursor_task_id, run.session_id),
                ).fetchone()
                if pending is not None:
                    # A person is being asked RIGHT NOW. Taking the generation
                    # back mid-question would be the scheduler answering it.
                    raise JobError("an approval for this generation is pending")
            recovered = replace(
                run,
                status=JobRunStatus.RUNNING,
                owner_token=owner_token,
                fencing_epoch=run.fencing_epoch + 1,
                lease_expires_at_utc=expires,
                claimed_at=claimed_at,
            )
            cursor = connection.execute(
                """
                UPDATE job_runs
                   SET status = ?, owner_token = ?, fencing_epoch = ?,
                       lease_expires_at_utc = ?, claimed_at = ?
                 WHERE id = ? AND fencing_epoch = ? AND settled_at IS NULL
                   AND status = ?
                   AND session_id IS NOT NULL AND chain_id IS NOT NULL
                   AND session_id = ? AND chain_id = ?
                   AND owner_token IS ? AND lease_expires_at_utc IS ?
                """,
                (
                    recovered.status.value,
                    recovered.owner_token,
                    recovered.fencing_epoch,
                    recovered.lease_expires_at_utc,
                    recovered.claimed_at,
                    run_id,
                    # F1: the epoch the CALLER observed, not the one just read.
                    # The two are equal here only because nothing moved between
                    # the read and the write; naming the caller's value is what
                    # refuses a stale observer whose run has since advanced.
                    expected_fencing_epoch,
                    run.status.value,
                    # The binding, re-stated so it is provably not rewritten.
                    run.session_id,
                    run.chain_id,
                    # The exact ownership observed to be absent or expired,
                    # PINNED rather than re-derived: a second definition of
                    # expiry written in SQL is a second place for it to drift
                    # from ``lease_is_live``.
                    run.owner_token,
                    run.lease_expires_at_utc,
                ),
            )
            if cursor.rowcount != 1:
                raise JobError("the generation is not recoverable")
            # SAME TRANSACTION. Authority moved above; the evidence for it is
            # written here, and either both become durable or neither does.
            self._insert_job_run_event(
                connection,
                run.session_id,
                "job_run_reacquired",
                {
                    "run_id": run.id,
                    "job_id": run.job_id,
                    "generation": run.generation,
                    "chain_id": run.chain_id,
                    "previous_status": run.status.value,
                    "resulting_status": recovered.status.value,
                    "previous_fencing_epoch": run.fencing_epoch,
                    "fencing_epoch": recovered.fencing_epoch,
                    "previous_claimed_at": run.claimed_at,
                    "claimed_at": recovered.claimed_at,
                    "lease_expires_at_utc": recovered.lease_expires_at_utc,
                    # A BOOLEAN, never the token. Whether somebody held an
                    # expired lease is the operational fact; which token it
                    # was is a credential-shaped value with no audit value.
                    "previous_owner_present": run.owner_token is not None,
                    # UNAUTHENTICATED, and named so IN the record, because a
                    # later reader must not mistake it for a proven principal.
                    "actor_label": actor,
                    "actor_authenticated": False,
                    "reason": why,
                    # Present only where it was actually PROVED - the parked
                    # case. An unproved guess in an audit trail is worse than
                    # an absent field.
                    "cursor_task_id": cursor_task_id,
                    "recovered_at": claimed_at,
                },
                claimed_at,
            )
        return self._lease_result(recovered)

    def load_job_run(self, run_id: str) -> JobRun | None:
        """One generation, or ``None`` when it is missing OR unusable."""
        with self._connection() as connection:
            row = connection.execute(
                self._JOB_RUN_WITH_OWNER, (run_id,)
            ).fetchone()
        if row is None:
            return None
        return self._job_run_from_row(row, row["owner_revision"])

    def list_jobs(
        self, limit: int, *, include_archived: bool = False
    ) -> list[JobSummary]:
        """The durable job catalogue as BOUNDED operational metadata.

        Ordered by creation with an ascending id tie-break, so the listing is
        totally ordered and does not reshuffle itself when a job is toggled.
        The row bound is the STORE'S, applied to the query, so no caller -
        including a future command or script - can turn a listing into an
        export.

        A row that cannot be rebuilt is reported with ``readable`` false and
        every derived field absent. It is neither hidden nor guessed at: a
        corrupted job must stay visible to an operator and must stay
        unrunnable to everything else.
        """
        bound = self._visibility_rows(limit)
        if bound == 0:
            return []
        sql = f"SELECT {self._JOB_COLUMNS} FROM jobs"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        with self._connection() as connection:
            rows = connection.execute(sql, (bound,)).fetchall()
        return [self._job_summary(row) for row in rows]

    def job_summary(self, job_id: str) -> JobSummary | None:
        """ONE job as the same validated projection a listing returns."""
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return None if row is None else self._job_summary(row)

    @classmethod
    def _job_summary(cls, row: sqlite3.Row) -> JobSummary:
        record = cls._job_from_row(row)
        identity = row["id"] if isinstance(row["id"], str) else ""
        updated = canonical_utc_timestamp(row["updated_at"]) or ""
        if record is None:
            return JobSummary(id=identity, updated_at=updated, readable=False)
        return JobSummary(
            id=record.id,
            updated_at=record.updated_at,
            kind=record.spec.kind.value,
            # Already through the canonical credential/local-path redaction
            # pipeline at construction; the declaration itself never crosses.
            label=record.spec.label,
            project_id=record.project_id,
            enabled=record.enabled,
            archived=record.archived,
            revision=record.revision,
            timezone=record.spec.timezone,
            next_due_at_utc=record.next_due_at_utc,
            last_run_id=record.last_run_id,
        )

    def list_job_runs(self, job_id: str, limit: int) -> list[JobRunSummary]:
        """The generations of EXACTLY this job, newest generation first.

        Totally ordered by the generation counter, which is unique per job, so
        repeated calls agree without a tie-break. Same store-side bound and
        the same honest treatment of a row that cannot be rebuilt.
        """
        bound = self._visibility_rows(limit)
        if bound == 0:
            return []
        with self._connection() as connection:
            revision_row = connection.execute(
                "SELECT revision FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            rows = connection.execute(
                f"SELECT {self._JOB_RUN_COLUMNS} FROM job_runs WHERE job_id = ? "
                "ORDER BY generation DESC LIMIT ?",
                (job_id, bound),
            ).fetchall()
        owner_revision = None if revision_row is None else revision_row["revision"]
        return [self._job_run_summary(row, owner_revision) for row in rows]

    @classmethod
    def _job_run_summary(
        cls, row: sqlite3.Row, owner_revision: Any
    ) -> JobRunSummary:
        run = cls._job_run_from_row(row, owner_revision)
        identity = row["id"] if isinstance(row["id"], str) else ""
        owner = row["job_id"] if isinstance(row["job_id"], str) else ""
        if run is None:
            return JobRunSummary(id=identity, job_id=owner, readable=False)
        return JobRunSummary(
            id=run.id,
            job_id=run.job_id,
            job_revision=run.job_revision,
            generation=run.generation,
            nominal_due_at_utc=run.nominal_due_at_utc,
            status=run.status.value,
            settled=run.settled,
            snapshot_digest=run.snapshot_digest,
            session_id=run.session_id,
            task_id=run.task_id,
            attempt_id=run.attempt_id,
            blocking_code=run.blocking_code,
            condition_result=run.condition_result,
            # Coordination lifecycle, and deliberately not the owner token:
            # it is an opaque caller-supplied value this store never inspects,
            # so it cannot be asserted non-secret and never crosses.
            claimed=run.owner_token is not None,
            fencing_epoch=run.fencing_epoch,
            lease_expires_at_utc=run.lease_expires_at_utc,
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
