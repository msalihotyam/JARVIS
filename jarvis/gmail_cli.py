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
config directory, a ``.env`` in the working directory, Application Default
Credentials and a service account - and its encrypted store can hold a
service-account credential as readily as a user one. This build allows
exactly one: an AUTHORIZED-USER OAuth2 credential, issued for the OAuth
client configured in a JARVIS-dedicated config directory, stored ENCRYPTED
there with the key in the OS keyring. Enforced three ways, measured against
gws 0.22.5 on 2026-09-10:

- the child environment has the ``GOOGLE_``/``GWS_``/``CLOUDSDK_``/
  ``GCLOUD_``/``OAUTH_``/``GMAIL_`` families removed and then the config
  directory and keyring backend SET explicitly - an explicit variable beats
  a ``.env`` (dotenvy never overrides one), and the dedicated directory is
  the only place the CLI is told to look;
- ``gws auth status`` is run under that pinned environment before every
  operation and its JSON must describe exactly the allowed route
  (``jarvis.gmail.gmail_route_refusal``): a valid ``client_secret.json`` in
  the dedicated directory (``credential_source`` names it and no
  ``client_config_error``), encrypted storage with the keyring backend, no
  plaintext file, and the AUTHORIZED-USER evidence - a refresh token, a
  credential ``client_id`` equal to the configured client's, and the CLI's
  own successful token check. A service account carries no refresh token
  and a numeric client id that never equals the configured OAuth client's,
  so it cannot pass even from the encrypted store. ADC is invisible to
  ``auth status`` but is consulted only when the config directory holds no
  credential, and its env route is stripped, so requiring the dedicated
  credential is what excludes it. No secret is read to prove any of this:
  the status report masks client ids and carries no token;
- a ``.env`` in the child's working directory or any ancestor is a refusal.

``users.getProfile`` then proves the EFFECTIVE ACCOUNT only.

THE BOUNDARY IS PROCESS CREATION. Everything before ``Popen`` returns - the
bounds, the executable, the ``.env`` chain, the pinned environment, the
spawn itself - is deterministic and, when it fails, nothing left the
machine. From the moment the child exists, every failure is typed as
STARTED (``GmailCommandExited``, ``GmailCommandTimedOut``,
``GmailCommandInterrupted``): a nonzero exit, a timeout, an OSError on the
pipes, a wait failure, anything. For a write those all become
``GmailOutcomeUnknown`` and Core records a non-replayable in-doubt attempt,
because none of them is proof that the request did not cross.

NO MESSAGE CONTENT IN ARGV. A draft or a send carries its RFC 2822 bytes in a
uniquely named transport file inside a JARVIS-owned directory that is the
child's working directory, uploaded as ``message/rfc822`` through the CLI's
multipart upload. A local ``--dry-run`` under the same vector must report a
bodiless multipart upload before the real call.

NO RESIDUE IS SILENT. The transport file is removed on every path; if it
cannot be removed the fact is never swallowed: an acknowledged write becomes
``GmailTransportResidue`` (the remote outcome is known and reported, the
local cleanup fault is reported beside it, and it is not a reason to retry),
any other outcome carries the residue in its own refusal or unknown, and
every later write is refused before it starts until the transport directory
can be swept clean. Nothing ever reads the residue's contents.

WHAT IT DECIDES. Nothing. It observes and returns bytes; Core compares the
identity against its own binding, Core owns the destination grant and Core
owns the approval.
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
#: How long a killed child is given to actually exit before it is abandoned.
_KILL_WAIT_SECONDS = 5

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
#: outlive the process by a moment. Bounded: five tries, half a second.
_REMOVE_ATTEMPTS = 5
_REMOVE_DELAY_SECONDS = 0.1

#: The refusal every write gets while an earlier message file is still on
#: disk. Names the transport DIRECTORY (JARVIS-owned, constant), never the
#: file's contents or the unique subdirectory.
_RESIDUE_REFUSAL = (
    "an earlier Gmail message transport file could not be removed and is "
    "quarantined in the dedicated transport directory; Gmail writes are refused "
    "until it has been securely removed"
)


def gws_profile_arguments(principal: Any) -> tuple[str, ...]:
    """The vector that asks which account the session can act as. Constant
    apart from the bound principal it names in the path."""
    reason = gmail_principal_refusal(principal)
    if reason is not None:
        raise GmailCommandFailed(f"no usable account to observe: {reason}")
    params = json.dumps({"userId": principal}, separators=(",", ":"), ensure_ascii=True)
    return (*GWS_API_ARGS, "getProfile", "--params", params)


class GmailCommandFailed(GmailExecutionUnavailable):
    """A Gmail call did not happen: refused, or the child was never created.
    Nothing left the machine."""


class GmailCommandStarted(GmailCommandFailed):
    """The child WAS created and the call did not succeed. Never proof that
    nothing happened: for a write it is ``GmailOutcomeUnknown``."""


class GmailCommandExited(GmailCommandStarted):
    """The child exited nonzero."""

    def __init__(self, returncode: int) -> None:
        super().__init__(f"the Google Workspace CLI refused the request (exit {returncode})")
        self.returncode = returncode


class GmailCommandTimedOut(GmailCommandStarted):
    """The child did not answer inside its bound."""


class GmailCommandInterrupted(GmailCommandStarted):
    """The child was created and then the call failed on this side - a pipe,
    an output read, a wait - before an outcome could be read."""


class GmailTransportResidue(GmailExecutionUnavailable):
    """A write was ACKNOWLEDGED and the transport file could not be removed.

    Carries the acknowledgement so the caller can report the remote outcome
    truthfully; the local fault is a confidentiality fault, not a reason to
    doubt or repeat the send.
    """

    def __init__(self, output: "GmailCommandOutput") -> None:
        super().__init__(_RESIDUE_REFUSAL)
        self.output = output


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
    """Why this working directory could side-load variables, or ``None``."""
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
    """The sanitized, PINNED environment."""
    return sanitized_gmail_environment(
        os.environ if environ is None else environ, config_dir=config_dir
    )


def _abandon(process: Any) -> None:
    """Best effort to stop a child that will not be waited for any further."""
    try:
        process.kill()
    except Exception:  # noqa: BLE001 - it may already be gone
        pass
    try:
        process.wait(timeout=_KILL_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 - abandoned either way
        pass


def _run(
    arguments: tuple[str, ...],
    *,
    config_dir: str,
    cwd: str,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    popen: Any = subprocess.Popen,
    timeout: int = GWS_READ_TIMEOUT_SECONDS,
) -> GmailCommandOutput:
    """Start ``gws`` once with exactly these arguments, or fail closed.

    Two regimes, split at process creation. Before ``popen`` returns every
    failure is ``GmailCommandFailed`` proper and nothing ran: the bounds, the
    executable, the ``.env`` chain, the pinned environment, and the spawn's
    own ``OSError``. After it returns the child exists and every failure is
    a ``GmailCommandStarted``: the timeout, a nonzero exit, and ANY exception
    from feeding stdin, reading the pipes or waiting - an ``OSError`` there
    is not a spawn failure and is never reported as one.

    ``shell=False`` and a real argument LIST; stdin fed empty bytes and
    closed; output captured as bounded bytes and decoded with replacement.
    Nothing from ``stderr`` is returned or re-raised.
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
    environment = _child_environment(environ, config_dir)
    try:
        process = popen(
            [executable, *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=environment,
            cwd=cwd,
        )
    except OSError as exc:
        raise GmailCommandFailed("the Google Workspace CLI could not be started") from exc
    # THE BOUNDARY. The child exists; from here on nothing is a spawn failure.
    try:
        stdout, _stderr = process.communicate(input=b"", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _abandon(process)
        raise GmailCommandTimedOut(
            "the Google Workspace CLI did not answer inside its time bound"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - OSError, ValueError, anything post-start
        _abandon(process)
        raise GmailCommandInterrupted(
            "the Google Workspace CLI call failed after the process started"
        ) from exc
    returncode = process.returncode
    if type(returncode) is not int:
        raise GmailCommandInterrupted("the Google Workspace CLI exit status could not be read")
    if returncode != 0:
        raise GmailCommandExited(returncode)
    raw = stdout or b""
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
    popen: Any = subprocess.Popen,
) -> None:
    """Require the dedicated authorized-user route, or fail closed.

    Runs ``gws auth status`` under the pinned environment and judges the
    answer with the pure ``gmail_route_refusal``. No credential is read by
    this process: the CLI reports where its client configuration and
    credential come from, how the credential is stored, whether it carries a
    refresh token for the configured client and whether its own token check
    passed - and only one answer passes.
    """
    try:
        output = _run(
            GMAIL_AUTH_STATUS_ARGS,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            popen=popen,
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
    popen: Any = subprocess.Popen,
) -> tuple[str, str]:
    """The ``(principal, credential_route)`` the dedicated session acts as.

    AN OBSERVATION, NEVER A DECISION, in two halves that must both pass: the
    ROUTE (``gmail_route_check``) and then the EFFECTIVE ACCOUNT - the profile
    of the bound account, asked for by name. The returned route is the one
    constant this build allows, returned only after the route check proved
    the CLI is on it; it is never inferred from the profile.
    """
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    arguments = gws_profile_arguments(principal)
    gmail_route_check(config_dir, environ=environ, path_lookup=path_lookup, popen=popen)
    try:
        output = _run(
            arguments,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            popen=popen,
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
    """Remove one transport directory and its file. Retried, bounded: on
    Windows a just-killed child can hold the handle for a moment. ``False``
    is a confidentiality fault the caller must surface, never swallow."""
    for attempt in range(_REMOVE_ATTEMPTS):
        shutil.rmtree(directory, ignore_errors=True)
        if not os.path.exists(directory):
            return True
        if attempt + 1 < _REMOVE_ATTEMPTS:
            time.sleep(_REMOVE_DELAY_SECONDS)
    return False


def transport_residue_present(config_dir: str) -> bool:
    """Whether an earlier write's transport directory is still on disk after
    a sweep. The sweep IS the later cleanup attempt: when it succeeds the
    block lifts; while anything remains, writes are refused."""
    root = Path(config_dir) / GWS_TRANSPORT_DIRNAME
    if not root.exists():
        return False
    try:
        entries = list(root.iterdir())
    except OSError:
        return True
    remaining = False
    for entry in entries:
        if entry.is_dir():
            if not _remove_transport(str(entry)):
                remaining = True
        else:
            try:
                entry.unlink()
            except OSError:
                remaining = True
    return remaining


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
    popen: Any = subprocess.Popen,
) -> GmailCommandOutput:
    """Execute ONE re-proved plan, or fail closed - or, for a write that
    started, refuse to claim it did not happen.

    Order, and what each step means:

    1. the plan is re-derived - refusal;
    2. the credential route is proved - refusal;
    3. a READ runs once in the run directory; every failure is a refusal;
    4. a WRITE first requires the transport directory to sweep clean (an
       earlier residue is a refusal before anything starts), gets a fresh
       transport directory holding the message bytes derived from the plan's
       own validated resource, then a local ``--dry-run`` under the exact
       vector which must describe a bodiless multipart upload (any dry-run
       failure is a refusal: a dry run makes no request), then the real
       call, whose every non-success is ``GmailOutcomeUnknown``;
    5. the transport directory is removed on every path, and a removal that
       fails is reported on that path: ``GmailTransportResidue`` when the
       write was acknowledged, otherwise folded into the refusal or unknown.
    """
    reason = gmail_operation_plan_refusal(plan)
    if reason is not None:
        raise GmailCommandFailed(reason)
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    gmail_route_check(config_dir, environ=environ, path_lookup=path_lookup, popen=popen)
    if not plan.is_write:
        return _run(
            plan.arguments,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            popen=popen,
            timeout=GWS_READ_TIMEOUT_SECONDS,
        )

    if transport_residue_present(config_dir):
        raise GmailCommandFailed(_RESIDUE_REFUSAL)
    message = gmail_outbound_message(plan.resource)
    root = _transport_root(config_dir)
    directory = tempfile.mkdtemp(prefix="msg-", dir=str(root))
    outcome: BaseException | None = None
    output: GmailCommandOutput | None = None
    try:
        output = _write_and_send(
            plan, message, directory,
            config_dir=config_dir, environ=environ, path_lookup=path_lookup, popen=popen,
        )
    except BaseException as exc:  # noqa: BLE001 - re-raised below, after cleanup
        outcome = exc
    removed = _remove_transport(directory)
    if removed:
        if outcome is not None:
            raise outcome
        assert output is not None
        return output
    # The transport file is still on disk. Never silent, never a retry.
    if outcome is None:
        assert output is not None
        raise GmailTransportResidue(output)
    if isinstance(outcome, GmailOutcomeUnknown):
        raise GmailOutcomeUnknown(f"{outcome}; and {_RESIDUE_REFUSAL}") from outcome
    if isinstance(outcome, GmailCommandFailed):
        raise GmailCommandFailed(f"{outcome}; and {_RESIDUE_REFUSAL}") from outcome
    raise outcome


def _write_and_send(
    plan: Any,
    message: bytes,
    directory: str,
    *,
    config_dir: str,
    environ: Mapping[str, str] | None,
    path_lookup: Any,
    popen: Any,
) -> GmailCommandOutput:
    """Steps 4's body: transport file, dry run, real call."""
    _write_transport_file(directory, message)
    try:
        rehearsal = _run(
            (*plan.arguments, GWS_DRY_RUN_FLAG),
            config_dir=config_dir,
            cwd=directory,
            environ=environ,
            path_lookup=path_lookup,
            popen=popen,
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
            popen=popen,
            timeout=GWS_WRITE_TIMEOUT_SECONDS,
        )
    except GmailCommandStarted as exc:
        raise GmailOutcomeUnknown(
            f"the Gmail write started and did not succeed; the request may "
            f"have reached Gmail ({exc})"
        ) from exc
    except GmailCommandFailed:
        # The child was never created: the executable vanished or the spawn
        # itself failed.
        raise
    except Exception as exc:  # noqa: BLE001 - unclassified, after the child may exist
        raise GmailOutcomeUnknown(
            "the Gmail write did not complete and its outcome is unknown"
        ) from exc


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
    "GmailCommandInterrupted",
    "GmailCommandOutput",
    "GmailCommandStarted",
    "GmailCommandTimedOut",
    "GmailTransportResidue",
    "dotenv_refusal",
    "gmail_route_check",
    "gws_executable",
    "gws_profile_arguments",
    "observed_gmail_identity",
    "run_operation_plan",
    "transport_residue_present",
]
