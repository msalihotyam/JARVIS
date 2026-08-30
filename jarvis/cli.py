"""Executable local CLI entrypoint; it never starts a model call by itself."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from jarvis.app import (
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    MAX_TASK_JSON_BYTES,
    CommandResult,
    JarvisApp,
    TaskInputError,
    build_default_app,
    chain_declare_result,
    chain_show_result,
    failure_result,
    memory_add_result,
    memory_list_result,
    project_create_result,
    sessions_result,
    show_result,
    status_payload,
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
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings


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

    run_command = commands.add_parser("run", help="Start a task from task JSON.")
    run_command.add_argument("--task-json", required=True, metavar="PATH|-")
    run_command.add_argument("--session-id")

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
    project_create.add_argument("--id", required=True, dest="project_id")
    project_create.add_argument("--name", required=True)

    memory_command = commands.add_parser(
        "memory", help="Explicit project-scoped memory (untrusted reference data)."
    )
    memory_actions = memory_command.add_subparsers(dest="memory_action")
    memory_add = memory_actions.add_parser("add")
    memory_add.add_argument("--project", required=True)
    memory_add.add_argument(
        "--kind", required=True, choices=[kind.value for kind in ProjectMemoryKind]
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

    # Sequential task chain (M11 S2): local-only, like ``project`` and
    # ``memory``. Declaring a chain performs no action, so these commands
    # build no registry, connector, probe or model call, and neither of them
    # can advance a chain, create a step task or approve anything.
    chain_command = commands.add_parser(
        "chain", help="Declare or show the session's inert sequential chain."
    )
    chain_actions = chain_command.add_subparsers(dest="chain_action")
    chain_declare = chain_actions.add_parser(
        "declare", help="Declare an inert chain (creates and runs nothing)."
    )
    chain_declare.add_argument("--session-id", required=True)
    chain_declare.add_argument("--chain-json", required=True, metavar="PATH|-")
    chain_show = chain_actions.add_parser("show")
    chain_show.add_argument("--session-id", required=True)
    # ``run`` is the ONE chain verb that executes, so unlike declare/show it
    # deliberately goes through the ordinary application path: it needs the
    # registry, the connectors and AgentCore, exactly as ``run`` does.
    chain_run = chain_actions.add_parser(
        "run", help="Drive the declared chain one step at a time."
    )
    chain_run.add_argument("--session-id", required=True)

    return parser


def _read_task_json(source: str) -> bytes:
    """Read at most one byte more than the limit, so an oversized input is
    refused instead of being loaded into memory first."""
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
                f"Task JSON file could not be read: {source}"
            ) from exc
    if isinstance(payload, str):
        # A text stdin stream is bounded by characters, so the encoded form
        # can only be larger; the limit below still holds.
        payload = payload.encode("utf-8")
    if len(payload) > MAX_TASK_JSON_BYTES:
        raise TaskInputError(
            f"Task JSON is larger than the {MAX_TASK_JSON_BYTES} byte limit."
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
        return failure_result(
            "project",
            settings.permission_mode,
            ErrorCode.INVALID_TASK,
            "'project' requires the 'create' sub-command.",
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
        return memory_list_result(memory, settings, args.project)
    return failure_result(
        "memory",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'memory' requires the 'add' or 'list' sub-command.",
        EXIT_INVALID_INPUT,
    )


def _dispatch_task_command(
    settings: Settings, args: argparse.Namespace
) -> CommandResult:
    """Operator recovery. Constructs a store and nothing else."""
    memory = SQLiteMemoryStore(settings.memory_database)
    if getattr(args, "task_action", None) == "resolve":
        return task_resolve_result(
            memory,
            settings,
            args.session_id,
            args.task_id,
            args.attempt_id,
            args.resolution,
        )
    return failure_result(
        "task",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'task' requires the 'resolve' sub-command.",
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
    return failure_result(
        "chain",
        settings.permission_mode,
        ErrorCode.INVALID_TASK,
        "'chain' requires the 'declare' or 'show' sub-command.",
        EXIT_INVALID_INPUT,
    )


def _dispatch(app: JarvisApp, command: str, args: argparse.Namespace) -> CommandResult:
    if command == "chain":
        return app.run_chain(args.session_id)
    if command == "run":
        return app.run(_read_task_json(args.task_json), args.session_id)
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
    base: Settings = settings or Settings()
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
    if args.permission_mode:
        effective = replace(
            effective, permission_mode=PermissionMode(args.permission_mode)
        )
    command = args.command or "status"

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
