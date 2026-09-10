"""Gmail minimum - THE process seam over the Google Workspace CLI.

The same seam ``jarvis.github_cli`` is, for a different executable: exactly
one program, ``gws``, found on PATH, started once per step with an argument
vector that is RE-DERIVED from a validated typed resource by ``jarvis.gmail``
and re-proved here before the process starts. No shell, no command string,
no subcommand parameter, no stdin, no way for a caller to add, reorder or
override an argument.

ONE CREDENTIAL ROUTE, PROVED BEFORE EVERY CALL. gws 0.22.5 can authenticate
from many places: a pre-obtained token in the environment, a credentials
file named by the environment, plaintext or encrypted credentials in its
config directory, Application Default Credentials (an env-named file, the
gcloud well-known file, or the metadata server) and a service account. This
build allows exactly one: OAuth2 user credentials stored ENCRYPTED in a
JARVIS-dedicated config directory whose encryption key is held by the OS
keyring. It is enforced three ways, measured against gws 0.22.5 on
2026-09-10:

- the child environment has the whole ``GOOGLE_``/``GWS_``/``CLOUDSDK_``/
  ``GCLOUD_``/``OAUTH_``/``GMAIL_`` families removed, and then the config
  directory and keyring backend are SET explicitly - an explicitly set
  variable beats a ``.env`` (dotenvy never overrides an existing variable),
  and the dedicated directory is the only place the CLI is told to look;
- ``gws auth status`` is run under that pinned environment before every
  operation and its JSON must describe exactly the allowed route
  (``jarvis.gmail.gmail_route_refusal``): OAuth2, storage ``encrypted``,
  keyring backend, no environment source, no plaintext file, the OAuth
  client configuration present, and every path inside the dedicated
  directory. ADC is invisible to ``auth status`` but is only consulted when
  the config directory holds no credentials, and the env-named ADC route is
  stripped - so requiring the dedicated credentials to exist is what keeps
  ADC and a service account from ever being selected;
- a ``.env`` in the child's working directory or any ancestor is a refusal,
  so nothing that would only work by side-loading a variable can run.

``users.getProfile`` then proves the EFFECTIVE ACCOUNT only - which mailbox
the credential can act as - never the credential's source.

NO MESSAGE CONTENT IN ARGV. A draft or a send carries its RFC 2822 bytes in a
uniquely named transport file inside a JARVIS-owned directory that is the
child's working directory, uploaded as ``message/rfc822`` through the CLI's
multipart upload (``--upload``). The command line names the file and the
non-secret routing parameters and nothing else; a local ``--dry-run`` under
the same vector is required to report a multipart upload with no JSON body
before the real call is made. The transport file is removed on every path -
success, refusal, timeout, exception and unknown outcome - and stale
transport directories are swept before each write.

A WRITE THAT STARTED AND DID NOT SUCCEED IS UNKNOWN. Once the child exists,
a nonzero exit, a timeout, an unreadable acknowledgement or any other failure
may sit on either side of the request leaving the machine; none of them is
proof that nothing was sent. They escape as ``GmailOutcomeUnknown`` and Core
records an in-doubt attempt that is never replayed. Only what provably
precedes the request - the plan refusal, the route check, the ``.env``
refusal, a missing executable, a failed spawn and the local dry run - is a
plain, non-retryable failure.

WHAT IT DECIDES. Nothing. It observes and returns bytes; Core compares the
identity against its own binding, Core owns the destination grant and Core
owns the approval. A refusal here never falls back, retries,
re-authenticates, prompts, opens a browser or widens anything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, Mapping

from jarvis.gmail import (
    GMAIL_AUTH_STATUS_ARGS,
    GMAIL_CREDENTIAL_ROUTE,
    GMAIL_UPLOAD_NAME,
    GWS_API_ARGS,
    GWS_ARGUMENT_MAX_CHARS,
    GWS_DRY_RUN_FLAG,
    GWS_MAX_ARGUMENTS,
    GmailExecutionUnavailable,
    GmailOutcomeUnknown,
    gmail_address_refusal,
    gmail_config_dir_refusal,
    gmail_dry_run_refusal,
    gmail_operation_plan_refusal,
    gmail_outbound_message,
    gmail_principal_refusal,
    gmail_route_refusal,
    sanitized_gmail_environment,
)

#: The one executable name this build will ever start, resolved through PATH.
GWS_EXECUTABLE_NAME = "gws"

#: Wall-clock ceilings on one child process. A read that cannot answer inside
#: its bound is a refusal; a write that cannot is an UNKNOWN outcome.
GWS_READ_TIMEOUT_SECONDS = 30
GWS_WRITE_TIMEOUT_SECONDS = 60

#: Ceiling on captured child output, applied to the BYTES before any decode.
GWS_OUTPUT_MAX_BYTES = 4 * 1024 * 1024

#: Inside the dedicated config directory: where reads run, and where each
#: write's transport directory is created. Both are JARVIS-owned.
GWS_RUN_DIRNAME = "run"
GWS_TRANSPORT_DIRNAME = "transport"

#: The file the CLI's dotenv support would read. Its presence anywhere up the
#: working directory's chain is a refusal, not a variable.
DOTENV_NAME = ".env"

#: How the transport file's removal is retried on Windows, where a handle can
#: outlive the process by a moment.
_REMOVE_ATTEMPTS = 5
_REMOVE_DELAY_SECONDS = 0.1


def gws_profile_arguments(principal: Any) -> tuple[str, ...]:
    """The vector that asks which account the session can act as. Constant
    apart from the bound principal it names in the path."""
    reason = gmail_principal_refusal(principal)
    if reason is not None:
        raise GmailCommandFailed(f"no usable account to observe: {reason}")
    params = json.dumps({"userId": principal}, separators=(",", ":"), ensure_ascii=True)
    return (*GWS_API_ARGS, "getProfile", "--params", params)


class GmailCommandFailed(GmailExecutionUnavailable):
    """A Gmail call did not happen: refused or not started. Nothing left
    the machine."""


class GmailCommandExited(GmailCommandFailed):
    """The child STARTED and exited nonzero. For a read that is a refusal;
    for a write it is ``GmailOutcomeUnknown``, never proof of no effect."""

    def __init__(self, returncode: int) -> None:
        super().__init__(f"the Google Workspace CLI refused the request (exit {returncode})")
        self.returncode = returncode


class GmailCommandTimedOut(GmailCommandFailed):
    """The child STARTED and did not answer inside its bound."""


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


def dotenv_refusal(cwd: Any) -> str | None:
    """Why this working directory could side-load variables, or ``None``.

    ``HOME``-style lookups are not the concern - the CLI's dotenv support is:
    it reads ``.env`` from the working directory chain. An explicitly set
    variable is never overridden by one, and every variable this seam cares
    about IS set explicitly, but a directory chain that carries one is
    refused outright rather than reasoned about.
    """
    try:
        current = Path(cwd).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return "the working directory cannot be resolved"
    for directory in (current, *current.parents):
        if (directory / DOTENV_NAME).exists():
            return "a .env file is present in the working directory or an ancestor"
    return None


def _child_environment(
    environ: Mapping[str, str] | None, config_dir: str
) -> dict[str, str]:
    """The sanitized, PINNED environment: the Google families removed, then
    the dedicated config directory and the keyring backend set explicitly."""
    return sanitized_gmail_environment(
        os.environ if environ is None else environ, config_dir=config_dir
    )


def _run(
    arguments: tuple[str, ...],
    *,
    config_dir: str,
    cwd: str,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
    timeout: int = GWS_READ_TIMEOUT_SECONDS,
) -> GmailCommandOutput:
    """Start ``gws`` once with exactly these arguments, or fail closed.

    Everything before ``runner`` is a pre-spawn refusal (``GmailCommandFailed``
    proper): the bounds, the executable, the ``.env`` chain, the pinned
    environment. A spawn that raises ``OSError`` never started either. Once
    the child exists the failures are typed differently - ``GmailCommandExited``
    and ``GmailCommandTimedOut`` - so a caller performing a side effect can
    tell "nothing ran" from "something may have".

    ``shell=False`` and a real argument LIST; stdin closed with empty bytes;
    output captured as bounded bytes and decoded with replacement. Nothing
    from ``stderr`` is returned or re-raised.
    """
    if type(arguments) is not tuple or not arguments:
        raise GmailCommandFailed("no argument vector was derived")
    if len(arguments) > GWS_MAX_ARGUMENTS:
        raise GmailCommandFailed("the argument list is over the bound")
    for argument in arguments:
        if type(argument) is not str or len(argument) > GWS_ARGUMENT_MAX_CHARS:
            raise GmailCommandFailed("an argument is not bounded text")
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    reason = dotenv_refusal(cwd)
    if reason is not None:
        raise GmailCommandFailed(reason)
    executable = gws_executable(path_lookup=path_lookup)
    try:
        completed = runner(
            [executable, *arguments],
            input=b"",
            capture_output=True,
            shell=False,
            env=_child_environment(environ, config_dir),
            cwd=cwd,
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
        raise GmailCommandExited(int(completed.returncode))
    raw = completed.stdout or b""
    if type(raw) is str:
        raw = raw.encode("utf-8", errors="replace")
    truncated = len(raw) > GWS_OUTPUT_MAX_BYTES
    return GmailCommandOutput(
        stdout=raw[:GWS_OUTPUT_MAX_BYTES].decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def _run_directory(config_dir: str) -> str:
    """Where reads and status calls run: a JARVIS-owned directory with nothing
    in it, inside the dedicated config directory."""
    directory = Path(config_dir) / GWS_RUN_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def _one_json_object(stdout: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    return payload if type(payload) is dict else None


def gmail_route_check(
    config_dir: str,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> None:
    """Require the dedicated credential route, or fail closed.

    Runs ``gws auth status`` under the pinned environment and judges the
    answer with the pure ``gmail_route_refusal``. Local, no network, no
    credential read by this process - the CLI reports where its credential
    would come from and how it is stored, and only one answer passes.
    """
    try:
        output = _run(
            GMAIL_AUTH_STATUS_ARGS,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            runner=runner,
        )
    except GmailCommandFailed as exc:
        raise GmailCommandFailed(
            "the Google Workspace CLI could not report its authentication state"
        ) from exc
    reason = gmail_route_refusal(_one_json_object(output.stdout), config_dir=config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the credential route is not the dedicated one: {reason}")


def observed_gmail_identity(
    principal: Any,
    config_dir: Any,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> tuple[str, str]:
    """The ``(principal, credential_route)`` the dedicated session acts as.

    AN OBSERVATION, NEVER A DECISION, in two halves that must both pass: the
    ROUTE (``gmail_route_check``) and then the EFFECTIVE ACCOUNT - the profile
    of the bound account, asked for by name, which the API answers only if
    the credential belongs to that account. The returned route is the one
    constant this build allows, and it is returned only after the route
    check proved the CLI is on it; it is never inferred from the profile.
    """
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    arguments = gws_profile_arguments(principal)
    gmail_route_check(config_dir, environ=environ, path_lookup=path_lookup, runner=runner)
    try:
        output = _run(
            arguments,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            runner=runner,
        )
    except GmailCommandFailed as exc:
        raise GmailCommandFailed(
            "the Google Workspace CLI reports no usable authenticated session "
            "for the expected account"
        ) from exc
    payload = _one_json_object(output.stdout)
    if payload is None or "error" in payload:
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


# --------------------------------------------------------------------------
# The write transport
# --------------------------------------------------------------------------


def _transport_root(config_dir: str) -> Path:
    root = Path(config_dir) / GWS_TRANSPORT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _remove_transport(directory: str) -> bool:
    """Remove one transport directory and its file. Best effort, retried:
    on Windows a just-killed child can hold the handle for a moment."""
    for attempt in range(_REMOVE_ATTEMPTS):
        shutil.rmtree(directory, ignore_errors=True)
        if not os.path.exists(directory):
            return True
        if attempt + 1 < _REMOVE_ATTEMPTS:
            time.sleep(_REMOVE_DELAY_SECONDS)
    return False


def _sweep_transport(root: Path) -> None:
    """Remove every leftover transport directory from an earlier interrupted
    write, so a crash can never leave message bytes behind for long."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            _remove_transport(str(entry))


def _write_transport_file(directory: str, message: bytes) -> str:
    """The message, in a file only this process created, inside a directory
    only this process named. Exclusive create: an existing file is a refusal,
    never overwritten. On POSIX the mode is owner-only.

    ponytail: on Windows the file inherits the JARVIS data directory's ACL
    (the operator's own profile); add an explicit per-file ACL if this ever
    runs on a shared machine.
    """
    path = os.path.join(directory, GMAIL_UPLOAD_NAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        handle = os.fdopen(descriptor, "wb")
    except Exception:
        os.close(descriptor)
        raise
    with handle:
        handle.write(message)
    return path


def run_operation_plan(
    plan: Any,
    *,
    config_dir: str,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> GmailCommandOutput:
    """Execute ONE re-proved plan, or fail closed - or, for a write that
    started, refuse to claim it did not happen.

    Order, and what each step means:

    1. the plan is re-derived (``gmail_operation_plan_refusal``) - refusal;
    2. the credential route is proved (``gmail_route_check``) - refusal;
    3. a READ runs once in the run directory; every failure is a refusal;
    4. a WRITE gets a fresh transport directory holding the message bytes
       derived from the plan's own validated resource (never handed in), then
       a local ``--dry-run`` under the exact vector, which must report a
       multipart upload with no JSON body - any dry-run failure is a refusal,
       because a dry run makes no request; then the real call, whose every
       non-success is ``GmailOutcomeUnknown``;
    5. the transport directory is removed in ``finally`` on every path.
    """
    reason = gmail_operation_plan_refusal(plan)
    if reason is not None:
        raise GmailCommandFailed(reason)
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    gmail_route_check(config_dir, environ=environ, path_lookup=path_lookup, runner=runner)
    if not plan.is_write:
        return _run(
            plan.arguments,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            runner=runner,
            timeout=GWS_READ_TIMEOUT_SECONDS,
        )

    message = gmail_outbound_message(plan.resource)
    root = _transport_root(config_dir)
    _sweep_transport(root)
    directory = tempfile.mkdtemp(prefix="msg-", dir=str(root))
    try:
        _write_transport_file(directory, message)
        try:
            rehearsal = _run(
                (*plan.arguments, GWS_DRY_RUN_FLAG),
                config_dir=config_dir,
                cwd=directory,
                environ=environ,
                path_lookup=path_lookup,
                runner=runner,
                timeout=GWS_READ_TIMEOUT_SECONDS,
            )
        except GmailCommandFailed as exc:
            # A dry run makes no request, so its failure precedes the boundary.
            raise GmailCommandFailed(
                "the write was refused by the local dry run before any request "
                f"could leave the machine: {exc}"
            ) from exc
        reason = gmail_dry_run_refusal(_one_json_object(rehearsal.stdout))
        if reason is not None:
            raise GmailCommandFailed(
                f"the local dry run did not describe a multipart upload: {reason}"
            )
        try:
            return _run(
                plan.arguments,
                config_dir=config_dir,
                cwd=directory,
                environ=environ,
                path_lookup=path_lookup,
                runner=runner,
                timeout=GWS_WRITE_TIMEOUT_SECONDS,
            )
        except (GmailCommandExited, GmailCommandTimedOut) as exc:
            raise GmailOutcomeUnknown(
                f"the Gmail write started and did not succeed; the request may "
                f"have reached Gmail ({exc})"
            ) from exc
        except GmailCommandFailed:
            # Pre-spawn: the executable vanished or the spawn itself failed.
            raise
        except Exception as exc:  # noqa: BLE001 - after the child may have started
            raise GmailOutcomeUnknown(
                "the Gmail write did not complete and its outcome is unknown"
            ) from exc
    finally:
        _remove_transport(directory)


__all__ = [
    "DOTENV_NAME",
    "GWS_EXECUTABLE_NAME",
    "GWS_OUTPUT_MAX_BYTES",
    "GWS_READ_TIMEOUT_SECONDS",
    "GWS_RUN_DIRNAME",
    "GWS_TRANSPORT_DIRNAME",
    "GWS_WRITE_TIMEOUT_SECONDS",
    "GmailCommandExited",
    "GmailCommandFailed",
    "GmailCommandOutput",
    "GmailCommandTimedOut",
    "dotenv_refusal",
    "gmail_route_check",
    "gws_executable",
    "gws_profile_arguments",
    "observed_gmail_identity",
    "run_operation_plan",
]
