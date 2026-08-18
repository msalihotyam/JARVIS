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
    failure_result,
    status_payload,
)
from jarvis.contracts import ErrorCode, PermissionMode
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


def _dispatch(app: JarvisApp, command: str, args: argparse.Namespace) -> CommandResult:
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
