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
  the dedicated directory, encrypted storage with the keyring backend, no
  plaintext file, a refresh token, a credential ``client_id`` equal to the
  configured client's, and the CLI's own successful token check. That is
  the ROUTE, not the TYPE: the report never carries the stored ``type``,
  and a service-account file dressed with those three fields passes it
  while the CLI's own loader would select the service-account
  authenticator. ADC is invisible to ``auth status`` but is consulted only
  when the config directory holds no credential, and its env route is
  stripped, so requiring the dedicated credential is what excludes it;
- the TYPE is proved by PROVENANCE and the INSTANCE by a HOLD
  (``attested_credential``). ``gws auth login`` is the only writer of
  ``credentials.enc`` inside gws 0.22.5 and it always writes
  ``type: authorized_user``; ``gmail_controlled_login`` runs that login
  itself, into a directory it first proved held no credential and no token
  cache, and records a non-secret attestation of the exact file it produced
  (principal, directory, SHA-256 and size of the ciphertext). Before every
  operation the file is opened with a Windows share mode that lets the
  child READ it but denies every writer, deleter and renamer for as long as
  the handle is open, its bytes are read through that very handle and
  digested against the attestation, and the handle stays open until the
  child has exited - so the credential the child decrypted is the attested
  one: a credential replaced or re-created since the attestation is refused
  before any child starts, and none can be swapped in while one runs.
  Nothing decrypts anything: the digest is of ciphertext;
- JARVIS's own steps never race each other: the controlled login and every
  operation bracket take a cooperative exclusive lock on one file in the
  dedicated directory (``gmail_step_lock``: share mode 0, released when
  the handle closes - on return, on exception and on a crash alike), and a
  second JARVIS step is refused before any child starts, never interleaved;
- a ``.env`` in the child's working directory or any ancestor is a refusal.

THE TRUSTED LOCAL BOUNDARY (usable-v1). The OS user JARVIS runs as, and the
JARVIS-owned data directory - the dedicated directory with its
``client_secret.json``, ``credentials.enc``, attestation, lock, run and
transport directories - are TRUSTED, exactly as the database beside them
is. Everything above defends the route against ambient and foreign
credentials; against a stored credential that JARVIS's controlled login
did not create (a service account dressed as a user, an operator's own
``gws auth login``, a file copied in); against a credential changed since
the attestation; against a swap while a child runs; and against JARVIS's
own concurrent steps. It does NOT defend against, and does not claim to
defend against, a process already running with this Windows user's
authority that deliberately rewrites the trusted directory: such a process
can rewrite the attestation and ``client_secret.json`` as readily as the
credential, and a cooperative lock does not bind it. That is out of scope
for usable-v1.

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

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from jarvis.trusted_exec import NO_CONSOLE_WINDOW, resolve_trusted_executable
from jarvis.gmail import (
    GMAIL_ATTESTATION_MAX_BYTES,
    GMAIL_ATTESTATION_NAME,
    GMAIL_AUTH_STATUS_ARGS,
    GMAIL_CREDENTIAL_MAX_BYTES,
    GMAIL_CREDENTIAL_ROUTE,
    GMAIL_ENCRYPTED_CREDENTIALS_NAME,
    GMAIL_LOGIN_ARGS,
    GMAIL_LOGIN_PRECLUDING_FILES,
    GMAIL_UPLOAD_NAME,
    GWS_API_ARGS,
    GWS_ARGUMENT_MAX_CHARS,
    GWS_DRY_RUN_FLAG,
    GWS_MAX_ARGUMENTS,
    GmailCredentialAttestation,
    GmailExecutionUnavailable,
    GmailOutcomeUnknown,
    GmailResourceKind,
    _same_path,
    gmail_address_refusal,
    gmail_listing_thread_ids,
    gws_thread_metadata_arguments,
    gmail_attestation_from_dict,
    gmail_attestation_refusal,
    gmail_config_dir_refusal,
    gmail_credential_attestation,
    gmail_dry_run_refusal,
    gmail_login_refusal,
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
#: The controlled login waits for a person to finish the browser consent.
GWS_LOGIN_TIMEOUT_SECONDS = 300
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
    #: A listing only: ``(thread_id, raw metadata response or None)`` for each
    #: listed thread the seam asked for its From / Subject / Date.
    metadata: tuple[tuple[str, str | None], ...] = ()


def trusted_gws_lookup(name: str) -> str | None:
    """``gws`` by provenance, never by a search that can reach the current
    directory: a ``gws.exe`` in the workspace JARVIS was pointed at is not the
    Google Workspace CLI, and it would be started against a real, logged-in
    Google session (DB-001)."""
    found = resolve_trusted_executable(name)
    return None if found is None else found.path


def gws_executable(*, path_lookup: Any = trusted_gws_lookup) -> str:
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
    path_lookup: Any = trusted_gws_lookup,
    popen: Any = subprocess.Popen,
    timeout: int = GWS_READ_TIMEOUT_SECONDS,
    inherit_stderr: bool = False,
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
    Nothing from ``stderr`` is returned or re-raised; the controlled login
    alone lets the child inherit it, so the operator sees the consent URL.
    Every other spawn is created with ``NO_CONSOLE_WINDOW``: a call from the
    window process must not open a console window per ``gws`` child.
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
            stderr=None if inherit_stderr else subprocess.PIPE,
            shell=False,
            env=environment,
            cwd=cwd,
            **({} if inherit_stderr else {"creationflags": NO_CONSOLE_WINDOW}),
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


# --------------------------------------------------------------------------
# The credential instance: hold, attest, bind
# --------------------------------------------------------------------------

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_NONE = 0
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
#: ``ERROR_SHARING_VIOLATION`` and ``ERROR_LOCK_VIOLATION``: the file is
#: open elsewhere in a way that excludes this open.
_SHARING_ERRORS = (32, 33)
#: The cooperative lock every JARVIS Gmail step takes in the dedicated
#: directory. An empty file; the exclusion is the open handle's share mode.
GMAIL_STEP_LOCK_NAME = "jarvis-gmail.lock"


def _win32_open(path: str, access: int, share: int, disposition: int) -> Any:
    """One ``CreateFileW``: an open handle, or ``OSError``.

    ponytail: Windows-only by construction - the mandatory share modes this
    seam relies on have no POSIX equivalent - and the seam fails closed on
    any other platform rather than pretend.
    """
    if sys.platform != "win32":
        raise GmailCommandFailed(
            "the Windows share semantics this seam relies on are unavailable on this platform"
        )
    import ctypes  # noqa: PLC0415 - platform-conditional
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = create_file(path, access, share, None, disposition, 0, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _win32_close(handle: Any) -> None:
    import ctypes  # noqa: PLC0415 - platform-conditional

    ctypes.WinDLL("kernel32").CloseHandle(handle)


@contextlib.contextmanager
def gmail_step_lock(config_dir: str) -> Iterator[None]:
    """Exclude every other JARVIS Gmail step for the block's duration.

    COOPERATIVE and JARVIS-INTERNAL. The lock file is opened with share
    mode 0, so a second open by any other JARVIS process - a concurrent
    ``jarvis gmail login``, an operation from another session - fails at
    once with a sharing violation and that step is refused before any
    child starts, never interleaved: two logins cannot both attest, a
    write's residue sweep cannot remove another write's in-flight transport
    directory, and no attestation is read while a login is replacing it.
    The handle closes when the block ends or the process dies, so no stale
    lock survives a crash. It binds JARVIS's own processes only: a process
    that ignores it is outside the trusted local boundary (module doc).
    """
    path = os.path.join(config_dir, GMAIL_STEP_LOCK_NAME)
    try:
        handle = _win32_open(
            path, _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_NONE, _OPEN_ALWAYS
        )
    except OSError as exc:
        if exc.winerror in _SHARING_ERRORS:
            raise GmailCommandFailed(
                "another JARVIS Gmail step is in progress in the dedicated directory; "
                "retry when it has finished"
            ) from exc
        raise GmailCommandFailed(
            "the dedicated directory's step lock cannot be created"
        ) from exc
    try:
        yield
    finally:
        _win32_close(handle)


@contextlib.contextmanager
def held_credential(config_dir: str) -> Iterator[bytes]:
    """Hold the dedicated ``credentials.enc`` against replacement for the
    block's duration and yield its exact bytes, read through the held handle.

    Windows share semantics: the file is opened for reading with
    ``FILE_SHARE_READ`` only, so while the handle is open any other open
    for write or delete - a rewrite, an unlink, a rename over it, a rename
    of it or of any ancestor directory - fails with a sharing violation,
    while the child's own read (``GENERIC_READ`` with every share flag, as
    Rust's ``std::fs::read`` opens) succeeds. The bytes are read through
    THIS handle, never through the path, so they are the held instance's.
    The ciphertext is digested by the caller and never decrypted here.
    """
    path = os.path.join(config_dir, GMAIL_ENCRYPTED_CREDENTIALS_NAME)
    try:
        handle = _win32_open(path, _GENERIC_READ, _FILE_SHARE_READ, _OPEN_EXISTING)
    except OSError as exc:
        raise GmailCommandFailed(
            "the dedicated credential file cannot be held: it is absent or "
            "another process holds it open for writing"
        ) from exc
    import msvcrt  # noqa: PLC0415 - platform-conditional, after _win32_open proved win32

    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError as exc:
        _win32_close(handle)
        raise GmailCommandFailed("the dedicated credential file cannot be held") from exc
    with os.fdopen(descriptor, "rb") as held:
        try:
            content = held.read(GMAIL_CREDENTIAL_MAX_BYTES + 1)
        except OSError as exc:
            raise GmailCommandFailed("the dedicated credential file cannot be read") from exc
        if not content or len(content) > GMAIL_CREDENTIAL_MAX_BYTES:
            raise GmailCommandFailed("the dedicated credential file is empty or over its bound")
        yield content


def _attestation_path(config_dir: str) -> str:
    return os.path.join(config_dir, GMAIL_ATTESTATION_NAME)


def read_credential_attestation(config_dir: str) -> GmailCredentialAttestation:
    """The stored attestation, or fail closed. Bounded read, closed record."""
    try:
        with open(_attestation_path(config_dir), "rb") as stored:
            raw = stored.read(GMAIL_ATTESTATION_MAX_BYTES + 1)
    except OSError as exc:
        raise GmailCommandFailed(
            "no credential attestation exists in the dedicated directory; "
            "establish the credential with 'jarvis gmail login'"
        ) from exc
    if len(raw) > GMAIL_ATTESTATION_MAX_BYTES:
        raise GmailCommandFailed("the credential attestation is over its bound")
    try:
        return gmail_attestation_from_dict(json.loads(raw.decode("utf-8")))
    except (GmailExecutionUnavailable, ValueError) as exc:
        raise GmailCommandFailed(f"the credential attestation is unusable: {exc}") from exc


def write_credential_attestation(config_dir: str, attestation: GmailCredentialAttestation) -> None:
    """Write the attestation atomically (sibling temp file, then replace)."""
    path = _attestation_path(config_dir)
    temp = path + ".tmp"
    encoded = json.dumps(attestation.to_dict(), sort_keys=True, indent=2, ensure_ascii=True)
    try:
        with open(temp, "w", encoding="utf-8") as out:
            out.write(encoded + "\n")
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise GmailCommandFailed("the credential attestation could not be written") from exc


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def migrate_gmail_config_dir(legacy: str | Path, current: str | Path) -> str | None:
    """The one-time move of the dedicated directory out of the data directory.

    ``None`` when there is nothing to report (no legacy directory - the
    idempotent steady state - or the move just succeeded); otherwise a bounded
    warning naming no path. Only a same-volume rename of the whole directory:
    nothing is copied, merged or read except the attestation record. A legacy
    entry that is a link/junction/reparse point or not a directory is refused
    untouched; when both directories exist the current one wins and the
    legacy one is left as it is (fail closed, no merge); a failed rename
    leaves the legacy directory where it was, still usable.
    """
    try:
        info = os.lstat(legacy)
    except FileNotFoundError:
        return _follow_moved_attestation(legacy, current)
    except OSError:
        return "the legacy Gmail directory inside the data directory cannot be inspected; it was not moved"
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        or not stat.S_ISDIR(info.st_mode)
    ):
        return "the legacy Gmail directory inside the data directory is a link or not a directory; it was not moved"
    try:
        os.lstat(current)
    except FileNotFoundError:
        pass
    except OSError:
        return "the Gmail directory beside the data directory cannot be inspected; the legacy one was not moved"
    else:
        return (
            "a legacy Gmail directory is still inside the data directory beside the current one; "
            "the current one is used and the legacy one is untouched - remove it once checked"
        )
    try:
        os.rename(legacy, current)
    except OSError:
        return "the Gmail directory could not be moved out of the data directory; the legacy one stays in use"
    return _follow_moved_attestation(legacy, current)


def _follow_moved_attestation(legacy: str | Path, current: str | Path) -> str | None:
    """Point the attestation at the moved directory. Runs at every start, not
    only right after the rename, so a crash between the two is repaired on the
    next start. A ``current`` that is a link/junction is never read through."""
    try:
        info = os.lstat(current)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return None
    try:
        attestation = read_credential_attestation(str(current))
    except (GmailCommandFailed, RecursionError):
        return None
    if _same_path(attestation.config_dir, str(legacy)):
        try:
            write_credential_attestation(
                str(current), replace(attestation, config_dir=str(current))
            )
        except GmailCommandFailed:
            return "the Gmail directory moved but its attestation was not updated; run 'jarvis gmail login'"
    return None


@contextlib.contextmanager
def attested_credential(config_dir: str, principal: Any) -> Iterator[GmailCredentialAttestation]:
    """THE instance bracket: for the block's duration the dedicated
    credential file is held against replacement and is the exact instance
    the attestation names for this principal - or the block never runs.

    Order: the JARVIS step lock is taken (another JARVIS step in progress
    is a refusal), the attestation is read (a missing or malformed record
    is a refusal before any file is touched), the credential is held and
    its bytes are digested against the record, the block runs with the
    hold and the lock still in place, then both are released.
    """
    with gmail_step_lock(config_dir):
        attestation = read_credential_attestation(config_dir)
        with held_credential(config_dir) as credentials:
            reason = gmail_attestation_refusal(
                attestation, principal=principal, config_dir=config_dir, credentials=credentials
            )
            if reason is not None:
                raise GmailCommandFailed(
                    "the credential instance is not the attested authorized-user "
                    f"credential: {reason}"
                )
            yield attestation


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gmail_controlled_login(
    config_dir: Any,
    principal: Any,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = trusted_gws_lookup,
    popen: Any = subprocess.Popen,
    now: Any = _utc_now_text,
) -> GmailCredentialAttestation:
    """Run ``gws auth login`` into the dedicated directory and attest the
    credential it created, or fail closed.

    Provenance, step by step, under the JARVIS step lock so no second login
    or operation interleaves: the directory must hold NO credential and no
    token cache (an existing one is refused, never removed - the operator
    removes it with ``gws auth logout`` under the dedicated directory); any
    stale attestation is removed first, so a failed login can never leave a
    record for a file it did not produce; the login runs under the pinned
    environment with the constant scope vector, stderr inherited so the
    consent URL reaches the operator and stdout captured for the report;
    the report must say success, name the bound principal as the account
    and name the dedicated credential file; then the new file is held, its
    ciphertext digested and the attestation written. The login's own JSON
    carries no secret and none is kept.
    """
    reason = gmail_principal_refusal(principal)
    if reason is not None:
        raise GmailCommandFailed(f"no usable principal to log in as: {reason}")
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    with gmail_step_lock(config_dir):
        return _login_under_lock(
            config_dir, principal, environ=environ, path_lookup=path_lookup, popen=popen, now=now
        )


def _login_under_lock(
    config_dir: str,
    principal: str,
    *,
    environ: Mapping[str, str] | None,
    path_lookup: Any,
    popen: Any,
    now: Any,
) -> GmailCredentialAttestation:
    present = [
        name for name in GMAIL_LOGIN_PRECLUDING_FILES
        if os.path.lexists(os.path.join(config_dir, name))
    ]
    if present:
        raise GmailCommandFailed(
            "the dedicated directory already holds a credential or token cache "
            f"({', '.join(present)}); provenance needs an empty start, so remove it "
            "with 'gws auth logout' run with GOOGLE_WORKSPACE_CLI_CONFIG_DIR set to "
            "that directory, then run the login again"
        )
    try:
        with contextlib.suppress(FileNotFoundError):
            os.remove(_attestation_path(config_dir))
    except OSError as exc:
        raise GmailCommandFailed("a stale credential attestation could not be removed") from exc
    try:
        output = _run(
            GMAIL_LOGIN_ARGS,
            config_dir=config_dir,
            cwd=_run_directory(config_dir),
            environ=environ,
            path_lookup=path_lookup,
            popen=popen,
            timeout=GWS_LOGIN_TIMEOUT_SECONDS,
            inherit_stderr=True,
        )
    except GmailCommandFailed as exc:
        raise GmailCommandFailed(f"the controlled login did not complete: {exc}") from exc
    reason = gmail_login_refusal(
        _one_json_object(output.stdout), principal=principal, config_dir=config_dir
    )
    if reason is not None:
        raise GmailCommandFailed(
            f"the controlled login did not establish the expected credential: {reason}; "
            "whatever it wrote is unattested and every Gmail operation stays refused - "
            "remove it with 'gws auth logout' under the dedicated directory"
        )
    with held_credential(config_dir) as credentials:
        try:
            attestation = gmail_credential_attestation(
                principal=principal, config_dir=config_dir,
                credentials=credentials, established_at=now(),
            )
        except GmailExecutionUnavailable as exc:
            raise GmailCommandFailed(f"the new credential could not be attested: {exc}") from exc
        write_credential_attestation(config_dir, attestation)
    return attestation


def gmail_route_check(
    config_dir: str,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = trusted_gws_lookup,
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
    path_lookup: Any = trusted_gws_lookup,
    popen: Any = subprocess.Popen,
) -> tuple[str, str]:
    """The ``(principal, credential_route)`` the dedicated session acts as.

    AN OBSERVATION, NEVER A DECISION, in two halves that must both pass
    inside the instance bracket (``attested_credential``: the attested
    authorized-user credential, held for the whole observation): the ROUTE
    (``gmail_route_check``) and then the EFFECTIVE ACCOUNT - the profile of
    the bound account, asked for by name. The returned route is the one
    constant this build allows, returned only after the route check proved
    the CLI is on it; it is never inferred from the profile.
    """
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailCommandFailed(f"the dedicated configuration directory is unusable: {reason}")
    arguments = gws_profile_arguments(principal)
    with attested_credential(config_dir, principal):
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
    path_lookup: Any = trusted_gws_lookup,
    popen: Any = subprocess.Popen,
) -> GmailCommandOutput:
    """Execute ONE re-proved plan, or fail closed - or, for a write that
    started, refuse to claim it did not happen.

    Order, and what each step means:

    1. the plan is re-derived - refusal;
    2. the JARVIS step lock is taken and the credential INSTANCE is proved
       and held, both for every later step (``attested_credential``), then
       the credential route is proved - refusal;
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
    # The frame already proved the resource account IS Core's principal.
    with attested_credential(config_dir, plan.resource.account):
        gmail_route_check(config_dir, environ=environ, path_lookup=path_lookup, popen=popen)
        if not plan.is_write:
            output = _run(
                plan.arguments,
                config_dir=config_dir,
                cwd=_run_directory(config_dir),
                environ=environ,
                path_lookup=path_lookup,
                popen=popen,
                timeout=GWS_READ_TIMEOUT_SECONDS,
            )
            if plan.resource.kind not in (GmailResourceKind.THREAD_LIST, GmailResourceKind.SEARCH):
                return output
            # Enrichment, under the SAME held credential and proved route: one
            # metadata-only request per listed thread, never beyond the
            # request's own limit. A failed one is recorded as unavailable -
            # the listing is still true without it.
            metadata: list[tuple[str, str | None]] = []
            for thread_id in gmail_listing_thread_ids(output.stdout, limit=plan.resource.limit):
                try:
                    detail = _run(
                        gws_thread_metadata_arguments(plan.resource.account, thread_id),
                        config_dir=config_dir,
                        cwd=_run_directory(config_dir),
                        environ=environ,
                        path_lookup=path_lookup,
                        popen=popen,
                        timeout=GWS_READ_TIMEOUT_SECONDS,
                    )
                    metadata.append((thread_id, detail.stdout))
                except GmailExecutionUnavailable:
                    metadata.append((thread_id, None))
            return GmailCommandOutput(output.stdout, output.truncated, tuple(metadata))
        return _write_under_hold(
            plan, config_dir=config_dir, environ=environ, path_lookup=path_lookup, popen=popen
        )


def _write_under_hold(
    plan: Any,
    *,
    config_dir: str,
    environ: Mapping[str, str] | None,
    path_lookup: Any,
    popen: Any,
) -> GmailCommandOutput:
    """Steps 4 and 5, unchanged, run while the credential hold is in place."""
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
    "GMAIL_STEP_LOCK_NAME",
    "GWS_LOGIN_TIMEOUT_SECONDS",
    "GWS_TRANSPORT_DIRNAME",
    "GWS_WRITE_TIMEOUT_SECONDS",
    "GmailCommandExited",
    "GmailCommandFailed",
    "GmailCommandInterrupted",
    "GmailCommandOutput",
    "GmailCommandStarted",
    "GmailCommandTimedOut",
    "GmailTransportResidue",
    "attested_credential",
    "dotenv_refusal",
    "gmail_controlled_login",
    "gmail_route_check",
    "gmail_step_lock",
    "held_credential",
    "migrate_gmail_config_dir",
    "read_credential_attestation",
    "write_credential_attestation",
    "gws_executable",
    "gws_profile_arguments",
    "observed_gmail_identity",
    "run_operation_plan",
    "transport_residue_present",
]
