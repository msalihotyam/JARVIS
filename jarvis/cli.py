"""Executable local CLI entrypoint; it never starts a model call by itself."""

from __future__ import annotations

import argparse
from uuid import uuid4
import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from jarvis.app import (
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    EXIT_POLICY,
    MAX_TASK_JSON_BYTES,
    CommandResult,
    JarvisApp,
    TaskInputError,
    build_default_app,
    chain_cancel_result,
    chain_declare_result,
    chain_show_result,
    config_failure_result,
    config_mutation_result,
    config_show_result,
    failure_result,
    gmail_login_result,
    job_archive_result,
    job_create_result,
    job_enablement_result,
    job_list_result,
    job_run_cancel_result,
    job_run_resolve_result,
    memory_add_result,
    memory_list_result,
    memory_retire_result,
    project_create_result,
    project_lifecycle_result,
    project_instruction_result,
    project_file_result,
    sessions_result,
    show_result,
    status_payload,
    task_orphan_result,
    task_resolve_result,
    tasks_result,
)
from jarvis.contracts import (
    AttemptRecovery,
    DataClassification,
    ErrorCode,
    PermissionMode,
    ProjectMemoryKind,
)
from jarvis.gmail import GmailExecutionUnavailable
from jarvis.gmail_cli import gmail_controlled_login
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import (
    ConfigError,
    ConfigStatus,
    ConfigStore,
    Settings,
    SettingsLoadResult,
    load_settings,
    normalize_database_path,
)


class _ArgumentFailure(Exception):
    """A parser failure, carried to the single JSON envelope in ``main``.

    Deliberately not an ``argparse.ArgumentError``: argparse catches that one
    internally and would route it back into ``error()``.
    """


class _ArgumentParser(argparse.ArgumentParser):
    """Argument-parser failures are invalid input, not the argparse default 2."""

    def error(self, message: str) -> None:
        # argparse would print to stderr and exit here. The failure is raised
        # instead so the caller emits exactly one diagnostic, as JSON.
        raise _ArgumentFailure(message)


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="jarvis")
    parser.add_argument(
        "--permission-mode",
        choices=[mode.value for mode in PermissionMode],
        help="Override the permission mode for this invocation only.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="Show local settings and SQLite schema state.")
    # Persistent operator configuration (M12 S4). Local-only, like ``status``:
    # reading or changing a preference builds no database, no application, no
    # registry and no connector, so configuring JARVIS can never run anything.
    config_command = commands.add_parser(
        "config", help="Show or change persistent operator settings."
    )
    config_actions = config_command.add_subparsers(dest="config_action")
    config_actions.add_parser("show", help="Show the effective configuration.")
    config_set = config_actions.add_parser("set", help="Set one known setting.")
    config_set.add_argument("key", choices=list(CONFIG_SETTABLE_KEYS))
    config_set.add_argument("value")
    config_reset = config_actions.add_parser(
        "reset", help="Reset one setting, or every setting."
    )
    config_reset.add_argument("key", nargs="?", choices=list(CONFIG_SETTABLE_KEYS))
    config_reset.add_argument(
        "--all",
        action="store_true",
        dest="reset_all",
        help="Rewrite the whole document with canonical defaults.",
    )

    # M12 S1: read-only operator visibility. Like ``status`` these build no
    # registry, connector or AgentCore, so they cannot execute, approve, open
    # an attempt or reserve anything even in principle.
    sessions_command = commands.add_parser(
        "sessions", help="List recently touched sessions (read-only)."
    )
    sessions_command.add_argument("--limit", type=int, default=None)

    tasks_command = commands.add_parser(
        "tasks", help="List one session's tasks (read-only)."
    )
    tasks_command.add_argument("--session-id", required=True)
    tasks_command.add_argument("--limit", type=int, default=None)

    show_command = commands.add_parser(
        "show", help="Show one task's operational snapshot (read-only)."
    )
    show_command.add_argument("--session-id", required=True)
    show_command.add_argument("--task-id", required=True)

    # Gmail minimum: the controlled login that establishes and attests the
    # JARVIS-dedicated authorized-user credential. It builds no registry,
    # connector, AgentCore or model; it runs exactly one child,
    # ``gws auth login``, into the dedicated directory and records what it
    # produced. Provenance is the type proof, so the login must be this
    # build's own - see ``jarvis.gmail_cli.gmail_controlled_login``.
    gmail_command = commands.add_parser(
        "gmail", help="Establish the JARVIS-dedicated Gmail credential."
    )
    gmail_actions = gmail_command.add_subparsers(dest="gmail_action")
    gmail_actions.add_parser(
        "login",
        help=(
            "Run the controlled Google Workspace CLI login into the dedicated "
            "directory and attest the resulting authorized-user credential."
        ),
    )

    # M12 S2A: explicit operator recovery. Local-only like the visibility
    # verbs - it builds no registry, connector or AgentCore, so it is
    # structurally incapable of executing or replaying anything.
    task_command = commands.add_parser(
        "task", help="Operator recovery for an execution attempt in doubt."
    )
    task_actions = task_command.add_subparsers(dest="task_action")
    task_resolve = task_actions.add_parser(
        "resolve",
        help="Abandon an attempt whose outcome will never be established.",
    )
    task_resolve.add_argument("--session-id", required=True)
    task_resolve.add_argument("--task-id", required=True)
    # Mandatory on purpose: the operator names the EXACT attempt they read in
    # `jarvis show`, so a blind recovery of whatever happens to be latest is
    # not expressible.
    task_resolve.add_argument("--attempt-id", required=True)
    task_resolve.add_argument(
        "--resolution",
        required=True,
        choices=[recovery.value for recovery in AttemptRecovery],
    )

    # M12 S2B2b1: the explicit human declaration that an execution still
    # marked IN_FLIGHT will never be concluded by whoever started it. A
    # SEPARATE verb from `resolve` on purpose - it acts on a different attempt
    # state, changes only the attempt, and states nothing about the world.
    task_orphan = task_actions.add_parser(
        "orphan",
        help=(
            "Declare an IN_FLIGHT attempt orphaned: nobody will establish its "
            "outcome. Proves nothing about whether the effect happened."
        ),
    )
    task_orphan.add_argument("--session-id", required=True)
    task_orphan.add_argument("--task-id", required=True)
    task_orphan.add_argument("--attempt-id", required=True)
    # Attribution, never authorization: there is no authenticated operator
    # identity in this build, and the record says so.
    task_orphan.add_argument("--actor", required=True)
    task_orphan.add_argument("--reason", required=True)

    # M12 S2B2b1: close a scheduled generation whose single uncertain attempt
    # has already been declared orphaned and abandoned. Local-only, like every
    # other recovery verb.
    job_command = commands.add_parser(
        "job", help="Operator resolution for a blocked scheduled generation."
    )
    job_actions = job_command.add_subparsers(dest="job_action")
    job_resolve = job_actions.add_parser(
        "resolve",
        help=(
            "Fail a generation whose abandoned attempt will never be "
            "established. Ends the generation, never the uncertainty."
        ),
    )
    job_resolve.add_argument("--run-id", required=True)
    job_resolve.add_argument("--attempt-id", required=True)
    job_resolve.add_argument("--actor", required=True)
    job_resolve.add_argument("--reason", required=True)

    # M12 S2B2b2: ask for a scheduled generation to stop. It records ONE
    # sticky intent and lets the chain and task authorities do their own work;
    # it reverses nothing and re-runs nothing.
    job_cancel = job_actions.add_parser(
        "cancel",
        help=(
            "Cancel a scheduled generation. Stops FUTURE work only - an "
            "effect that already happened stands."
        ),
    )
    job_cancel.add_argument("--run-id", required=True)
    job_cancel.add_argument("--actor", required=True)
    job_cancel.add_argument("--reason", required=True)

    # The minimum OPERATOR SURFACE for scheduled work. Local-only like every
    # other 'job' verb: defining, listing and retiring a job builds no
    # registry, connector or AgentCore and starts nothing.
    #
    # The specification is the job's OWN persisted JSON encoding, validated by
    # JobSpec.from_dict - the same default-deny rebuild storage uses. That is
    # deliberately the whole input surface: no wizard, no cron builder, no
    # per-field flags to keep in agreement with the grammar, and a
    # CONDITION_WATCH is created by the same command simply by declaring
    # 'condition' and 'on_true' instead of 'steps'.
    job_create = job_actions.add_parser(
        "create",
        help=(
            "Create one job from a JSON specification "
            "(project_id, kind, label, schedule, steps | condition+on_true)."
        ),
    )
    job_create.add_argument("--spec-json", required=True, metavar="PATH|-")
    job_list = job_actions.add_parser(
        "list", help="List durable job definitions (read-only)."
    )
    job_list.add_argument("--limit", type=int, default=None)
    job_list.add_argument(
        "--all",
        dest="include_archived",
        action="store_true",
        help="Include archived jobs (they have no future occurrences).",
    )
    # Enable / disable / archive all name the revision the operator just read
    # in 'job list': storage compares and sets on it, so a definition that
    # moved in between is refused instead of being acted on blindly.
    for name, summary in (
        ("enable", "Enable FUTURE scheduling; recalculates the next occurrence."),
        ("disable", "Disable FUTURE scheduling; stops nothing already running."),
        ("archive", "Retire a job from scheduling. Deletes nothing; no un-archive."),
    ):
        action = job_actions.add_parser(name, help=summary)
        action.add_argument("--job-id", required=True)
        action.add_argument("--revision", required=True, type=int)

    # Scheduler phase 2D: the foreground runner. Unlike every other scheduler
    # verb above it DOES execute, so - exactly like 'chain run' - it is built
    # through the ordinary application path with a registry, connectors and an
    # AgentCore, and reaches only the claim, dispatch, condition-watch,
    # recovery and cancellation boundaries that already exist.
    scheduler_command = commands.add_parser(
        "scheduler", help="Drive scheduled work in the foreground."
    )
    scheduler_actions = scheduler_command.add_subparsers(dest="scheduler_action")
    scheduler_run = scheduler_actions.add_parser(
        "run",
        help=(
            "Sweep due and unsettled generations sequentially until stopped "
            "with Ctrl+C. Retries nothing and resolves nothing."
        ),
    )
    scheduler_run.add_argument(
        "--once",
        action="store_true",
        help="Perform exactly one bounded sweep and exit.",
    )

    run_command = commands.add_parser("run", help="Start a task from task JSON.")
    run_command.add_argument("--task-json", required=True, metavar="PATH|-")
    run_command.add_argument("--session-id")

    # The natural-language entry. It executes exactly like ``run`` does -
    # through the ordinary application path - after the understanding model
    # has proposed and deterministic code has validated a task document.
    text_command = commands.add_parser(
        "text", help="Start work from one natural-language request."
    )
    text_command.add_argument("--text", required=True)
    text_command.add_argument("--session-id")
    text_command.add_argument(
        "--previous-text",
        default="",
        help="The earlier request this text answers a clarification for.",
    )

    for name in ("resume", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("--session-id", required=True)
        command.add_argument("--task-id", required=True)

    clarify_command = commands.add_parser("clarify")
    clarify_command.add_argument("--session-id", required=True)
    clarify_command.add_argument("--task-id", required=True)
    clarify_command.add_argument("--text", required=True)

    for name in ("approve", "reject"):
        command = commands.add_parser(name)
        command.add_argument("--session-id", required=True)
        command.add_argument("--task-id", required=True)
        command.add_argument("--request-id", required=True)
        command.add_argument("--fingerprint", required=True)
        if name == "approve":
            command.add_argument("--remember-for-session", action="store_true")

    # Project memory (M1): local-only commands. Like ``status`` they build no
    # registry, connector or model call; memory is written only through these
    # explicit user commands, never by a model or a connector.
    project_command = commands.add_parser(
        "project", help="Manage local project identities."
    )
    project_actions = project_command.add_subparsers(dest="project_action")
    project_create = project_actions.add_parser(
        "create", help="Create a project identity (never automatic)."
    )
    project_create.add_argument(
        "--id", dest="project_id", help="Explicit slug; omitted = an opaque id."
    )
    project_create.add_argument("--name", required=True)
    project_rename = project_actions.add_parser("rename", help="Rename a project.")
    project_rename.add_argument("--id", required=True, dest="project_id")
    project_rename.add_argument("--name", required=True)
    for action in ("archive", "unarchive"):
        project_actions.add_parser(action, help=f"{action.capitalize()} a project.").add_argument(
            "--id", required=True, dest="project_id"
        )
    # Gate 4: the project owner's explicit instruction (guidance, never authority).
    instruction_actions = project_actions.add_parser(
        "instruction", help="The project's standing instruction for JARVIS."
    ).add_subparsers(dest="instruction_action")
    instruction_set = instruction_actions.add_parser("set", help="Set a new version; --text '' clears.")
    instruction_set.add_argument("--id", required=True, dest="project_id")
    instruction_set.add_argument("--text", required=True)
    instruction_set.add_argument(
        "--classification",
        required=True,
        choices=[classification.value for classification in DataClassification],
    )
    for action in ("show", "history"):
        instruction_actions.add_parser(action).add_argument("--id", required=True, dest="project_id")
    # Gate 5: explicit-user project files (untrusted data, never authority).
    file_actions = project_actions.add_parser(
        "file", help="The project's text files (untrusted reference data)."
    ).add_subparsers(dest="file_action")
    file_add = file_actions.add_parser("add", help="Add one text file you choose.")
    file_add.add_argument("--id", required=True, dest="project_id")
    file_add.add_argument("--path", required=True)
    file_add.add_argument(
        "--classification",
        required=True,
        choices=[classification.value for classification in DataClassification],
    )
    file_list = file_actions.add_parser("list")
    file_list.add_argument("--id", required=True, dest="project_id")
    file_list.add_argument("--all", dest="include_purged", action="store_true",
                           help="Include purged tombstones.")
    for action in ("retire", "restore", "purge"):
        file_parser = file_actions.add_parser(action)
        file_parser.add_argument("--id", required=True, dest="project_id")
        file_parser.add_argument("--file", required=True, dest="file_id")
        if action == "purge":
            file_parser.add_argument("--confirm", required=True,
                                help="Permanent: repeat the file id to confirm.")

    memory_command = commands.add_parser(
        "memory", help="Explicit project-scoped memory (untrusted reference data)."
    )
    memory_actions = memory_command.add_subparsers(dest="memory_action")
    memory_add = memory_actions.add_parser("add")
    memory_add.add_argument("--project", required=True)
    # Canonical kinds are fact|decision|rule|thread. "preference" is the
    # deprecated M1-era input alias; it is written and persisted as a RULE.
    memory_add.add_argument(
        "--kind",
        required=True,
        choices=[kind.value for kind in ProjectMemoryKind] + ["preference"],
        help="Canonical kind (fact|decision|rule|thread); 'preference' is a "
        "deprecated alias written as 'rule'.",
    )
    memory_add.add_argument(
        "--classification",
        required=True,
        choices=[classification.value for classification in DataClassification],
    )
    memory_add.add_argument("--text", required=True)
    memory_add.add_argument("--supersedes")
    memory_list = memory_actions.add_parser("list")
    memory_list.add_argument("--project", required=True)
    memory_list.add_argument(
        "--all",
        dest="include_inactive",
        action="store_true",
        help="Include superseded and retired items (lifecycle history).",
    )
    memory_retire = memory_actions.add_parser(
        "retire", help="Explicitly retire one ACTIVE memory item."
    )
    memory_retire.add_argument("--project", required=True)
    memory_retire.add_argument("--item", required=True)

    # Sequential task chain (M11 S2): local-only, like ``project`` and
    # ``memory``. Declaring a chain performs no action, so these commands
    # build no registry, connector, probe or model call, and neither of them
    # can advance a chain, create a step task or approve anything.
    chain_command = commands.add_parser(
        "chain",
        help="Declare, show or cancel the session's sequential chain.",
    )
    chain_actions = chain_command.add_subparsers(dest="chain_action")
    chain_declare = chain_actions.add_parser(
        "declare", help="Declare an inert chain (creates and runs nothing)."
    )
    chain_declare.add_argument("--session-id", required=True)
    chain_declare.add_argument("--chain-json", required=True, metavar="PATH|-")
    chain_show = chain_actions.add_parser("show")
    chain_show.add_argument("--session-id", required=True)
    # Cancelling is local-only for the same reason declaring is: it stops
    # future steps by flipping the chain out of ACTIVE, and stopping work
    # needs no connector, no model and no AgentCore. Work already running is
    # not its business - 'cancel' owns a live task.
    chain_cancel = chain_actions.add_parser(
        "cancel", help="Cancel the session's chain (starts and stops nothing)."
    )
    chain_cancel.add_argument("--session-id", required=True)
    # ``run`` is the ONE chain verb that executes, so unlike declare/show it
    # deliberately goes through the ordinary application path: it needs the
    # registry, the connectors and AgentCore, exactly as ``run`` does.
    chain_run = chain_actions.add_parser(
        "run", help="Drive the declared chain one step at a time."
    )
    chain_run.add_argument("--session-id", required=True)

    return parser


def _read_task_json(source: str, *, label: str = "Task JSON") -> bytes:
    """Read at most one byte more than the limit, so an oversized input is
    refused instead of being loaded into memory first.

    ``label`` names the document in the refusal only; every caller gets the
    same bounded read, so there is one reader rather than one per document
    kind."""
    bounded = MAX_TASK_JSON_BYTES + 1
    if source == "-":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        payload = stream.read(bounded)
    else:
        try:
            with Path(source).open("rb") as handle:
                payload = handle.read(bounded)
        except OSError as exc:
            raise TaskInputError(
                f"{label} file could not be read: {source}"
            ) from exc
    if isinstance(payload, str):
        # A text stdin stream is bounded by characters, so the encoded form
        # can only be larger; the limit below still holds.
        payload = payload.encode("utf-8")
    if len(payload) > MAX_TASK_JSON_BYTES:
        raise TaskInputError(
            f"{label} is larger than the {MAX_TASK_JSON_BYTES} byte limit."
        )
    return payload


def _dispatch_memory_command(
    settings: Settings, command: str, args: argparse.Namespace
) -> CommandResult:
    memory = SQLiteMemoryStore(settings.memory_database)
    if command == "project":
        if getattr(args, "project_action", None) == "create":
            return project_create_result(
                memory, settings, args.project_id, args.name
            )
        action = getattr(args, "project_action", None)
        if action == "file" and getattr(args, "file_action", None):
            return project_file_result(
                memory, settings, args.file_action, args.project_id,
                path=getattr(args, "path", None), file_id=getattr(args, "file_id", None),
                classification=getattr(args, "classification", None),
                confirm=getattr(args, "confirm", None),
                include_purged=getattr(args, "include_purged", False),
            )
        if action == "instruction" and getattr(args, "instruction_action", None):
            return project_instruction_result(
                memory, settings, args.instruction_action, args.project_id,
                getattr(args, "text", None), getattr(args, "classification", None),
            )
        if action in ("rename", "archive", "unarchive"):
            return project_lifecycle_result(
                memory, settings, action, args.project_id, getattr(args, "name", None)
            )
        return failure_result(
            "project",
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            "'project' requires a sub-command: create, rename, archive, unarchive, "
            "instruction set|show|history or file add|list|retire|restore|purge.",
            EXIT_INVALID_INPUT,
        )
    if getattr(args, "memory_action", None) == "add":
        return memory_add_result(
            memory,
            settings,
            args.project,
            args.kind,
            args.classification,
            args.text,
            args.supersedes,
        )
    if getattr(args, "memory_action", None) == "list":
        return memory_list_result(
            memory, settings, args.project, args.include_inactive
        )
    if getattr(args, "memory_action", None) == "retire":
        return memory_retire_result(memory, settings, args.project, args.item)
    return failure_result(
        "memory",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'memory' requires the 'add', 'list' or 'retire' sub-command.",
        EXIT_INVALID_INPUT,
    )


def _dispatch_gmail_command(
    settings: Settings,
    args: argparse.Namespace,
    *,
    login: Callable[..., Any] = gmail_controlled_login,
) -> CommandResult:
    """The controlled Gmail login. Constructs nothing but the one child."""
    if getattr(args, "gmail_action", None) != "login":
        return failure_result(
            "gmail",
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            "'gmail' requires an action: login.",
            EXIT_INVALID_INPUT,
        )
    if settings.gmail_principal is None:
        return failure_result(
            "gmail-login",
            settings.permission_mode,
            ErrorCode.CONFIG_INVALID,
            "No Gmail principal is configured; set it first with "
            "'jarvis config set gmail_principal <address>'.",
            EXIT_POLICY,
        )
    try:
        attestation = login(str(settings.gmail_config_dir), settings.gmail_principal)
    except GmailExecutionUnavailable as exc:
        return failure_result(
            "gmail-login",
            settings.permission_mode,
            ErrorCode.CONNECTOR_FAILED,
            str(exc),
            EXIT_POLICY,
        )
    return gmail_login_result(settings, attestation)


def _dispatch_task_command(
    settings: Settings, args: argparse.Namespace
) -> CommandResult:
    """Operator recovery. Constructs a store and nothing else."""
    memory = SQLiteMemoryStore(settings.memory_database)
    action = getattr(args, "task_action", None)
    if action == "resolve":
        return task_resolve_result(
            memory,
            settings,
            args.session_id,
            args.task_id,
            args.attempt_id,
            args.resolution,
        )
    if action == "orphan":
        return task_orphan_result(
            memory,
            settings,
            args.session_id,
            args.task_id,
            args.attempt_id,
            args.actor,
            args.reason,
        )
    return failure_result(
        "task",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'task' requires the 'resolve' or 'orphan' sub-command.",
        EXIT_INVALID_INPUT,
    )


def _dispatch_job_command(
    settings: Settings, args: argparse.Namespace
) -> CommandResult:
    """Operator resolution for a generation. Constructs a store and nothing else."""
    memory = SQLiteMemoryStore(settings.memory_database)
    action = getattr(args, "job_action", None)
    if action == "create":
        try:
            document = _read_task_json(args.spec_json, label="Job specification")
        except TaskInputError as exc:
            # The reader refused (unreadable path, oversized document). Fail
            # closed through the ordinary envelope; nothing was created.
            return failure_result(
                "job-create",
                settings.permission_mode,
                ErrorCode.INVALID_TASK,
                str(exc),
                EXIT_INVALID_INPUT,
            )
        return job_create_result(memory, settings, document)
    if action == "list":
        return job_list_result(
            memory, settings, args.limit, include_archived=args.include_archived
        )
    if action in ("enable", "disable"):
        return job_enablement_result(
            memory, settings, args.job_id, args.revision, action == "enable"
        )
    if action == "archive":
        return job_archive_result(memory, settings, args.job_id, args.revision)
    if action == "resolve":
        return job_run_resolve_result(
            memory,
            settings,
            args.run_id,
            args.attempt_id,
            args.actor,
            args.reason,
        )
    if action == "cancel":
        # The coordination token this one invocation owns. Cancellation needs
        # a lease only to REFLECT the durable outcome, and a fresh token per
        # invocation is what keeps that reflection fenced like every other.
        return job_run_cancel_result(
            memory,
            settings,
            args.run_id,
            f"cancel-{uuid4()}",
            args.actor,
            args.reason,
        )
    return failure_result(
        "job",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'job' requires one of: create, list, enable, disable, archive, "
        "resolve, cancel.",
        EXIT_INVALID_INPUT,
    )


def _dispatch_visibility_command(
    settings: Settings, command: str, args: argparse.Namespace
) -> CommandResult:
    """Read-only visibility. Constructs a store and nothing else."""
    memory = SQLiteMemoryStore(settings.memory_database)
    if command == "sessions":
        return sessions_result(memory, settings, args.limit)
    if command == "tasks":
        return tasks_result(memory, settings, args.session_id, args.limit)
    return show_result(memory, settings, args.session_id, args.task_id)


#: The keys an operator may set or reset. Closed: anything else is refused
#: by the parser before a single byte is read or written.
CONFIG_SETTABLE_KEYS = (
    "execution_enabled",
    "permission_mode",
    "paid_api_enabled",
    "paid_api_approval_id",
    "claude_call_budget_usd",
    "approval_ttl_minutes",
    "memory_database",
    "claude_model",
    "claude_effort",
    "github_principal",
    "github_repositories",
    "gmail_principal",
    "browser_origins",
    "desktop_apps",
)


def _config_bool(key: str, raw: str) -> bool:
    if raw in ("true", "false"):
        return raw == "true"
    raise ConfigError(
        ErrorCode.CONFIG_INVALID, f"'{key}' must be exactly 'true' or 'false'."
    )


def _config_int(key: str, raw: str) -> int:
    try:
        return int(raw, 10)
    except ValueError:
        raise ConfigError(
            ErrorCode.CONFIG_INVALID, f"'{key}' must be a whole number."
        ) from None


def _config_amount(key: str, raw: str) -> float:
    try:
        amount = float(raw)
    except ValueError:
        raise ConfigError(
            ErrorCode.CONFIG_INVALID, f"'{key}' must be a number."
        ) from None
    if not math.isfinite(amount):
        raise ConfigError(
            ErrorCode.CONFIG_INVALID, f"'{key}' must be a finite number."
        )
    return amount


def _config_change(key: str, raw: str):
    """Turn one operator string into a change over Settings.

    Conversion lives here and validation lives in ``settings``: the parser's
    job is to say what the operator typed, and the store's job is to decide
    whether it is allowed. Every change below returns a whole new Settings,
    which the store then re-validates before anything is written.
    """
    if key == "execution_enabled":
        value = _config_bool(key, raw)
        return lambda current: replace(current, execution_enabled=value)
    if key == "permission_mode":
        try:
            mode = PermissionMode(raw)
        except ValueError:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "'permission_mode' must be one of: "
                + ", ".join(mode.value for mode in PermissionMode)
                + ".",
            ) from None
        return lambda current: replace(current, permission_mode=mode)
    if key == "paid_api_enabled":
        value = _config_bool(key, raw)
        return lambda current: replace(current, paid_api_enabled=value)
    if key == "paid_api_approval_id":
        return lambda current: replace(current, paid_api_approval_id=raw)
    if key == "claude_call_budget_usd":
        amount = _config_amount(key, raw)
        return lambda current: replace(current, claude_call_budget_usd=amount)
    # claude_model / claude_effort: the raw operator string is passed through
    # and the shared grammar in ``settings`` is the only validator - both here
    # via the store's re-validation and at every later Settings construction.
    if key == "claude_model":
        return lambda current: replace(current, claude_model=raw)
    if key == "claude_effort":
        return lambda current: replace(current, claude_effort=raw)
    # github_principal / github_repositories: the raw operator string is
    # passed through and the GitHub grammars in ``settings`` are the only
    # validators. The repository list is comma-separated because a list is
    # what the authority IS - one grant per repository - and an empty value
    # clears it, which is how an operator revokes GitHub access.
    if key == "github_principal":
        return lambda current: replace(current, github_principal=raw)
    if key == "github_repositories":
        entries = tuple(
            part.strip() for part in raw.split(",") if part.strip()
        )
        return lambda current: replace(current, github_repositories=entries)
    # gmail_principal: the raw operator string is passed through and the
    # Gmail grammar in ``settings`` is the only validator.
    if key == "gmail_principal":
        return lambda current: replace(current, gmail_principal=raw)
    # browser_origins: comma-separated for the same reason the repository
    # list is - a list is what the authority IS, one grant per origin - and
    # an empty value clears it, which is how an operator revokes browsing.
    if key == "browser_origins":
        origins = tuple(part.strip() for part in raw.split(",") if part.strip())
        return lambda current: replace(current, browser_origins=origins)
    # desktop_apps: the same comma-separated list, one grant per application.
    if key == "desktop_apps":
        apps = tuple(part.strip() for part in raw.split(",") if part.strip())
        return lambda current: replace(current, desktop_apps=apps)
    if key == "approval_ttl_minutes":
        minutes = _config_int(key, raw)
        return lambda current: replace(current, approval_ttl_minutes=minutes)
    # memory_database: canonicalized ONCE here, against the directory this
    # command actually ran in, so the stored absolute path keeps meaning the
    # same database from anywhere later.
    resolved = normalize_database_path(raw)
    return lambda current: replace(current, memory_database=resolved)


def _config_reset_change(key: str):
    """Reset one key to its code default, keeping every relationship."""
    defaults = Settings()
    if key == "paid_api_approval_id":
        def reset_id(current: Settings) -> Settings:
            if current.paid_api_enabled:
                # The relationship is the point: clearing the id while the
                # paid route is on would leave a switch nothing authorized.
                raise ConfigError(
                    ErrorCode.CONFIG_INVALID,
                    "'paid_api_approval_id' cannot be reset while "
                    "'paid_api_enabled' is true. Disable it first.",
                )
            return replace(current, paid_api_approval_id=None)

        return reset_id
    value = getattr(defaults, key)
    return lambda current: replace(current, **{key: value})


def _dispatch_config_command(
    settings_result, args: argparse.Namespace, store: ConfigStore
) -> CommandResult:
    """Local-only configuration commands. Nothing here can execute anything.

    ``show`` reports even a refused store, because that is exactly when an
    operator needs to see it. ``set`` and ``reset <key>`` REFUSE a document
    they could not read, and leave its bytes untouched: deriving a "next"
    document from one this build does not understand would silently discard
    whatever the operator actually had. ``reset --all`` is the way out, and
    is the one mutation that does not require the current document to be
    valid.
    """
    action = getattr(args, "config_action", None)
    mode = settings_result.settings.permission_mode
    if action == "show":
        # Read the store again rather than reporting whatever startup
        # happened to load: this command's whole purpose is to say what is in
        # there NOW, and reporting a value from earlier in the same process
        # would be the one place it could be wrong.
        return config_show_result(store, store.load())
    if action == "reset" and getattr(args, "reset_all", False):
        try:
            updated = store.reset_all()
        except ConfigError as exc:
            return config_failure_result("config-reset-all", mode, exc)
        return config_mutation_result(
            "config-reset-all", store, updated, store.load()
        )
    if action in ("set", "reset"):
        command = "config-set" if action == "set" else "config-reset"
        key = getattr(args, "key", None)
        if not key:
            return failure_result(
                command,
                mode,
                ErrorCode.CONFIG_INVALID,
                "'config reset' requires a key, or '--all'.",
                EXIT_INVALID_INPUT,
            )
        try:
            change = (
                _config_change(key, args.value)
                if action == "set"
                else _config_reset_change(key)
            )
            updated = store.mutate(change)
        except ConfigError as exc:
            return config_failure_result(command, mode, exc)
        return config_mutation_result(command, store, updated, store.load())
    return failure_result(
        "config",
        mode,
        ErrorCode.CONFIG_INVALID,
        "'config' requires the 'show', 'set' or 'reset' sub-command.",
        EXIT_INVALID_INPUT,
    )


def _dispatch_chain_command(
    settings: Settings, args: argparse.Namespace
) -> CommandResult:
    """Local-only chain commands. Nothing here can execute or approve."""
    memory = SQLiteMemoryStore(settings.memory_database)
    action = getattr(args, "chain_action", None)
    if action == "declare":
        return chain_declare_result(
            memory, settings, args.session_id, _read_task_json(args.chain_json)
        )
    if action == "show":
        return chain_show_result(memory, settings, args.session_id)
    if action == "cancel":
        return chain_cancel_result(memory, settings, args.session_id)
    return failure_result(
        "chain",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'chain' requires the 'declare', 'show' or 'cancel' sub-command.",
        EXIT_INVALID_INPUT,
    )


def _dispatch(app: JarvisApp, command: str, args: argparse.Namespace) -> CommandResult:
    if command == "scheduler":
        if getattr(args, "scheduler_action", None) != "run":
            return failure_result(
                "scheduler",
                app.permission_mode,
                ErrorCode.INVALID_TASK,
                "'scheduler' requires the 'run' sub-command.",
                EXIT_INVALID_INPUT,
            )
        return app.run_scheduler(once=args.once)
    if command == "chain":
        return app.run_chain(args.session_id)
    if command == "run":
        return app.run(_read_task_json(args.task_json), args.session_id)
    if command == "text":
        return app.submit_text(
            args.text, args.session_id, previous_text=args.previous_text
        )
    if command == "resume":
        return app.resume(args.session_id, args.task_id)
    if command == "clarify":
        return app.clarify(args.session_id, args.task_id, args.text)
    if command == "approve":
        return app.approve(
            args.session_id,
            args.task_id,
            args.request_id,
            args.fingerprint,
            args.remember_for_session,
        )
    if command == "reject":
        return app.reject(
            args.session_id, args.task_id, args.request_id, args.fingerprint
        )
    return app.cancel(args.session_id, args.task_id)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    app_factory: Callable[[Settings], JarvisApp] = build_default_app,
) -> int:
    parser = _build_parser()
    # S4 PRECEDENCE, and it is deliberate:
    #   1. explicitly injected Settings  - a caller that hands us settings has
    #      already decided; the file is not read at all, and even a transient
    #      flag does not edit their decision
    #   2. transient CLI flags           - this invocation only, never written
    #   3. the persisted document        - the operator's standing choice
    #   4. code defaults
    injected = settings is not None
    settings_result = (
        SettingsLoadResult(settings, ConfigStatus.VALID)
        if settings is not None
        else load_settings()
    )
    base: Settings = settings_result.settings
    try:
        args = parser.parse_args(argv)
    except _ArgumentFailure as exc:
        # No command was resolved and no application is built: the failure is
        # reported through the same envelope every other failure uses.
        parse_failure = failure_result(
            "cli",
            base.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
        print(parse_failure.render())
        return parse_failure.exit_code
    except SystemExit as exc:
        # --help and --version exit through argparse itself and stay 0.
        return int(exc.code or 0)

    # Precedence: explicit CLI flag, then injected settings, then defaults.
    effective: Settings = base
    if args.permission_mode and not injected:
        # Transient, and only where nobody injected a decision of their own.
        effective = replace(
            effective, permission_mode=PermissionMode(args.permission_mode)
        )
    command = args.command or "status"

    if command == "config":
        # Before any store, application, registry, connector or model: a
        # configuration command must be structurally incapable of running
        # anything, exactly like ``status``.
        config_result = _dispatch_config_command(
            SettingsLoadResult(effective, settings_result.status, settings_result.reason),
            args,
            ConfigStore(),
        )
        print(config_result.render())
        return config_result.exit_code

    if not settings_result.usable:
        # The stored configuration was refused. Ordinary commands do not get
        # to proceed on a guess - notably they must not open the DEFAULT
        # database when the operator had selected another one. Repair and
        # inspection stay reachable through ``config`` above.
        refusal = failure_result(
            command,
            effective.permission_mode,
            ErrorCode.CONFIG_INVALID,
            "The stored configuration was refused, so execution is disabled. "
            "Inspect it with 'jarvis config show' or repair it with "
            "'jarvis config reset --all'.",
            EXIT_POLICY,
        )
        print(refusal.render())
        return refusal.exit_code

    if command == "status":
        # Local metadata only: no registry, connector, probe or model call.
        memory = SQLiteMemoryStore(effective.memory_database)
        payload: dict[str, Any] = status_payload(effective, memory)
        print(json.dumps(payload, indent=2))
        return 0

    if command == "task":
        # Local-only, and routed here so that stays STRUCTURAL: recovery must
        # never be able to reach a connector, so no application is built.
        recovery = _dispatch_task_command(effective, args)
        print(recovery.render())
        return recovery.exit_code

    if command == "job":
        # The same structural guarantee, for the same reason: resolving a
        # blocked generation is an operator decision about durable state and
        # must be incapable of running anything.
        resolution = _dispatch_job_command(effective, args)
        print(resolution.render())
        return resolution.exit_code

    if command == "gmail":
        # The controlled login: one child process and one non-secret record,
        # with no application, registry, connector or AgentCore built.
        gmail_result = _dispatch_gmail_command(effective, args)
        print(gmail_result.render())
        return gmail_result.exit_code

    if command in ("sessions", "tasks", "show"):
        # Read-only, and routed here so that stays STRUCTURAL: no application,
        # registry or connector is built for these commands at all.
        visibility = _dispatch_visibility_command(effective, command, args)
        print(visibility.render())
        return visibility.exit_code

    if command in ("project", "memory"):
        # Local-only project memory commands: no registry, connector, probe
        # or model call, exactly like ``status``.
        result = _dispatch_memory_command(effective, command, args)
        print(result.render())
        return result.exit_code

    if command == "chain" and getattr(args, "chain_action", None) != "run":
        # Local-only too, and deliberately so: routing a declaration through
        # the same path as ``status`` is what makes its inertness structural
        # rather than merely intended - no AgentCore, registry or connector
        # is constructed at all. ``run`` is excluded here precisely because it
        # DOES execute, and must be built like any other executing command.
        try:
            chain_result = _dispatch_chain_command(effective, args)
        except TaskInputError as exc:
            chain_result = failure_result(
                command,
                effective.permission_mode,
                ErrorCode.INVALID_TASK,
                str(exc),
                EXIT_INVALID_INPUT,
            )
        print(chain_result.render())
        return chain_result.exit_code

    result: CommandResult
    try:
        result = _dispatch(app_factory(effective), command, args)
    except TaskInputError as exc:
        result = failure_result(
            command,
            effective.permission_mode,
            ErrorCode.INVALID_TASK,
            str(exc),
            EXIT_INVALID_INPUT,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary; nothing may escape
        # Only the exception type is reported: the message can carry paths,
        # environment values or other diagnostics that must not be printed.
        result = failure_result(
            command,
            effective.permission_mode,
            ErrorCode.CONNECTOR_FAILED,
            f"Unexpected internal failure: {type(exc).__name__}",
            EXIT_INTERNAL,
        )
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
