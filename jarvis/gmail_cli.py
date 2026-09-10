"""Gmail minimum - THE process seam over the Google Workspace CLI.

The same seam ``jarvis.github_cli`` is, for a different executable: exactly
one program, ``gws``, found on PATH, started once per operation with an
argument vector that is RE-DERIVED from a validated typed resource by
``jarvis.gmail`` and re-proved here before the process starts. No shell, no
command string, no subcommand parameter, no stdin, no way for a caller to
add, reorder or override an argument.

WHAT IT NEVER TOUCHES. The credential. JARVIS does not request, read, store,
pass, log or display an OAuth token: authentication is the CLI's own
already-established session, and the only thing this module learns about it
is the NON-SECRET account address it can act as. The child environment has
the ``GOOGLE_``/``GWS_``/``CLOUDSDK_``/``GCLOUD_`` families stripped before it
is handed over, so a token or a credential file path sitting in the parent
environment cannot reach the child - which is also what makes the observed
route provably the CLI session rather than an environment override.

WHICH ACCOUNT SERVED A CALL is not decided by observation. Every derived
vector names the bound account as ``userId`` in the request path, and the
Gmail API refuses to serve ``users/<account>/...`` under another account's
credential, so a read or a write cannot be served for an account other than
the one named. The observation below - ``users.getProfile`` for the bound
account - is Core's session gate and nothing more.

A WRITE THAT DOES NOT ANSWER IS NOT A FAILURE. A draft creation or a send
whose process times out may have reached the API; reporting it as "nothing
happened" would invite a retry that sends the message twice. Such a call
escapes as ``GmailOutcomeUnknown`` and Core records an in-doubt attempt.

LIVE VERIFICATION. The argument grammar below (``gws <api> <resource...>
<method> --params <json> [--json <body>]``) is pinned from the CLI's
published shape and has NOT been exercised against a live ``gws`` in the
session that wrote it. It is a closed table; if the installed CLI spells a
flag differently the fix is one row here, never a widening anywhere else.

WHAT IT DECIDES. Nothing. It observes an identity and returns bytes; Core
compares the identity against its own binding, Core owns the destination
grant and Core owns the approval. A refusal here never falls back, retries,
re-authenticates, prompts, opens a browser or widens anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from shutil import which
from typing import Any, Mapping

from jarvis.gmail import (
    GMAIL_CREDENTIAL_ROUTE,
    GWS_API_ARGS,
    GWS_ARGUMENT_MAX_CHARS,
    GWS_MAX_ARGUMENTS,
    GmailExecutionUnavailable,
    GmailOutcomeUnknown,
    gmail_address_refusal,
    gmail_operation_plan_refusal,
    gmail_principal_refusal,
    sanitized_gmail_environment,
)

#: The one executable name this build will ever start, resolved through PATH.
GWS_EXECUTABLE_NAME = "gws"

#: Wall-clock ceilings on one child process. A read that cannot answer inside
#: its bound is a refusal; a write that cannot is an UNKNOWN outcome.
GWS_READ_TIMEOUT_SECONDS = 30
GWS_WRITE_TIMEOUT_SECONDS = 60

#: Ceiling on captured child output, applied to the BYTES before any decode.
#: A full-format thread with inline parts can be large; anything past this is
#: truncated rather than loaded.
GWS_OUTPUT_MAX_BYTES = 4 * 1024 * 1024


def gws_profile_arguments(principal: Any) -> tuple[str, ...]:
    """The vector that asks which account the session can act as. Constant
    apart from the bound principal it names in the path."""
    reason = gmail_principal_refusal(principal)
    if reason is not None:
        raise GmailCommandFailed(f"no usable account to observe: {reason}")
    params = json.dumps({"userId": principal}, separators=(",", ":"), ensure_ascii=True)
    return (*GWS_API_ARGS, "getProfile", "--params", params)


class GmailCommandFailed(GmailExecutionUnavailable):
    """A Gmail call did not happen. Never a partial or assumed result."""


class GmailCommandTimedOut(GmailCommandFailed):
    """The child did not answer inside its bound. For a read that is a
    refusal; for a write it becomes ``GmailOutcomeUnknown``."""


@dataclass(frozen=True, slots=True)
class GmailCommandOutput:
    """What one completed child process produced. Data, with no authority."""

    stdout: str
    truncated: bool


def gws_executable(*, path_lookup: Any = which) -> str:
    """The resolved ``gws`` path, or fail closed. Resolved fresh every call."""
    resolved = path_lookup(GWS_EXECUTABLE_NAME)
    if not resolved or type(resolved) is not str:
        raise GmailCommandFailed(
            "the Google Workspace CLI is not available on this machine"
        )
    return resolved


def _child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The sanitized environment: ``HOME``/``APPDATA``/``USERPROFILE`` survive
    (the CLI finds its own session through them), every Google configuration
    family, proxy and XDG redirection does not."""
    return sanitized_gmail_environment(os.environ if environ is None else environ)


def _neutral_directory() -> str:
    """A working directory with nothing of the operator's in it."""
    return tempfile.gettempdir()


def _run(
    arguments: tuple[str, ...],
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
    timeout: int = GWS_READ_TIMEOUT_SECONDS,
) -> GmailCommandOutput:
    """Start ``gws`` once with exactly these arguments, or fail closed.

    ``shell=False`` and a real argument LIST; stdin closed with empty bytes;
    output captured as bounded bytes and decoded with replacement. Nothing
    from ``stderr`` is returned or re-raised: a diagnostic can carry
    addresses, paths and token fragments, so the refusal states the shape of
    the failure and the exit status only.
    """
    if type(arguments) is not tuple or not arguments:
        raise GmailCommandFailed("no argument vector was derived")
    if len(arguments) > GWS_MAX_ARGUMENTS:
        raise GmailCommandFailed("the argument list is over the bound")
    for argument in arguments:
        if type(argument) is not str or len(argument) > GWS_ARGUMENT_MAX_CHARS:
            raise GmailCommandFailed("an argument is not bounded text")
    executable = gws_executable(path_lookup=path_lookup)
    try:
        completed = runner(
            [executable, *arguments],
            input=b"",
            capture_output=True,
            shell=False,
            env=_child_environment(environ),
            cwd=_neutral_directory(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GmailCommandTimedOut(
            "the Google Workspace CLI did not answer inside its time bound"
        ) from exc
    except OSError as exc:
        raise GmailCommandFailed("the Google Workspace CLI could not be started") from exc
    if completed.returncode != 0:
        raise GmailCommandFailed(
            f"the Google Workspace CLI refused the request (exit {completed.returncode})"
        )
    raw = completed.stdout or b""
    if type(raw) is str:
        raw = raw.encode("utf-8", errors="replace")
    truncated = len(raw) > GWS_OUTPUT_MAX_BYTES
    return GmailCommandOutput(
        stdout=raw[:GWS_OUTPUT_MAX_BYTES].decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def observed_gmail_identity(
    principal: Any,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> tuple[str, str]:
    """The ``(principal, credential_route)`` the local session can act as.

    AN OBSERVATION, NEVER A DECISION. The profile of the BOUND account is
    asked for by name; the API answers only if the session's credential
    belongs to that account, and the answer must name it back. No session,
    an unreadable answer, an error, or an address that is not the expected
    one all fail closed. The token itself is never parsed, returned or
    logged - only the address.
    """
    arguments = gws_profile_arguments(principal)
    try:
        output = _run(arguments, environ=environ, path_lookup=path_lookup, runner=runner)
    except GmailCommandFailed as exc:
        raise GmailCommandFailed(
            "the Google Workspace CLI reports no usable authenticated session "
            "for the expected account"
        ) from exc
    try:
        payload = json.loads(output.stdout)
    except ValueError:
        payload = None
    if type(payload) is not dict or "error" in payload:
        raise GmailCommandFailed(
            "the Google Workspace CLI did not identify the account its session belongs to"
        )
    address = payload.get("emailAddress")
    if gmail_address_refusal(address) is not None:
        raise GmailCommandFailed(
            "the Google Workspace CLI did not identify the account its session belongs to"
        )
    if address.lower() != principal:
        raise GmailCommandFailed(
            "the credential in use does not belong to the account Core expects"
        )
    return (address.lower(), GMAIL_CREDENTIAL_ROUTE)


def run_operation_plan(
    plan: Any,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> GmailCommandOutput:
    """Execute ONE re-proved plan, or fail closed.

    The plan is checked by RE-DERIVATION first: its capability must be a
    closed enabled operation, its resource must bind to that capability and
    its destination, and its arguments must equal - element for element -
    what the closed builder produces. Passing is still not authorization;
    the grant and the identity comparison are Core's and have already
    happened.
    """
    reason = gmail_operation_plan_refusal(plan)
    if reason is not None:
        raise GmailCommandFailed(reason)
    timeout = GWS_WRITE_TIMEOUT_SECONDS if plan.is_write else GWS_READ_TIMEOUT_SECONDS
    try:
        return _run(
            plan.arguments,
            environ=environ,
            path_lookup=path_lookup,
            runner=runner,
            timeout=timeout,
        )
    except GmailCommandTimedOut as exc:
        if plan.is_write:
            raise GmailOutcomeUnknown(
                "the Gmail write did not answer inside its time bound; the "
                "request may have reached Gmail"
            ) from exc
        raise


__all__ = [
    "GWS_EXECUTABLE_NAME",
    "GWS_OUTPUT_MAX_BYTES",
    "GWS_READ_TIMEOUT_SECONDS",
    "GWS_WRITE_TIMEOUT_SECONDS",
    "GmailCommandFailed",
    "GmailCommandOutput",
    "GmailCommandTimedOut",
    "gws_executable",
    "gws_profile_arguments",
    "observed_gmail_identity",
    "run_operation_plan",
]
