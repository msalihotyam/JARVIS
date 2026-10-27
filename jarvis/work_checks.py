"""Project-mode check runner (PM16): run ONE allowlisted check, offline.

A check is the only code project mode executes on its own authority, and it
runs code the worker just wrote. So the runner trusts nothing it is handed:

  - the argv must start with an allowlisted template, and every argument after
    the template is a placeholder that must be a validated literal target
    (``work_plan._target_refusal``, D10) or, for unittest, a test module name;
    an option (``-p plugin``), a shell metacharacter or a control character
    never reaches the child;
  - there is no shell and no string command: the argv is a list, ``argv[0]``
    is an absolute path re-validated by ``trusted_exec`` immediately before
    the spawn, and it must lie outside the stage;
  - the environment is the ``trusted_exec`` allowlist, with every per-user
    root (USERPROFILE, HOME, APPDATA, ...) pointed at a throwaway directory so
    no ``.npmrc``, pip or git config with a token rides along, and every proxy
    variable pointed at a dead local port so a network attempt fails fast (D1;
    dependency installs are a separate, approved outgoing step);
  - the cwd is the stage worktree or a directory inside it;
  - the child starts suspended inside a kill-on-close job (``owned_process``)
    and is only resumed once the job is verified to hold it, so a timeout
    kills the whole tree, grandchildren included;
  - stdout/stderr are capped in bytes and returned as data with a truncated
    flag; PM17 (``work_regress``) refuses truncated output.

Every refusal raises ``CheckRefused`` with a stable code; nothing is started.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis import owned_process
from jarvis.trusted_exec import (
    NO_CONSOLE_WINDOW,
    TrustedExecutableError,
    _canonical,
    _inside,
    resolve_trusted_executable,
    trusted_environment,
)
from jarvis.work_plan import _CHECK_ARG, _target_refusal

__all__ = ["CHECK_TEMPLATES", "CheckRefused", "CheckResult", "DEAD_PROXY", "run_check"]

#: template -> (placeholder kind, PM17 runner label). ``module``: a dotted
#: test module under tests/ or test/; ``path``: a literal stage path; ``None``:
#: no arguments at all (``npm test`` runs a project script, never one of ours).
CHECK_TEMPLATES: dict[tuple[str, ...], tuple[str | None, str | None]] = {
    ("python", "-m", "unittest"): ("module", "unittest"),
    ("python", "-m", "pytest"): ("path", "pytest"),
    ("ruff", "check"): ("path", None),
    ("npm", "test"): (None, None),
}

#: Port 9 (discard) on loopback: nothing listens, a connect is refused at once.
DEAD_PROXY = "http://127.0.0.1:9"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "NPM_CONFIG_PROXY", "NPM_CONFIG_HTTPS_PROXY")
#: Characters cmd.exe, a batch shim (npm.cmd) or a POSIX shell would interpret.
#: No shell is used; this is defence in depth against a ``.cmd`` resolution.
_METACHARACTERS = frozenset("&|<>^%;`$()\"'!*?[]{}\n\r\t ")
_DEFAULT_CAP = 1 << 20
_CHUNK = 1 << 16


class CheckRefused(Exception):
    """Nothing was started. ``code`` is the stable machine reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CheckResult:
    argv: tuple[str, ...]
    #: ``work_regress.parse_output`` runner name, ``None`` when unparseable.
    runner: str | None
    #: ``None`` when the run was killed for overrunning ``timeout_s``.
    exit_code: int | None
    duration_s: float
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool


def _template(argv: Sequence[Any]) -> tuple[tuple[str, ...], list[str]]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        raise CheckRefused("not_argv", "a check is an argv list, never a command string")
    if not argv or any(type(a) is not str for a in argv):
        raise CheckRefused("not_argv", "every argv element must be a string")
    for prefix in sorted(CHECK_TEMPLATES, key=len, reverse=True):
        if tuple(argv[:len(prefix)]) == prefix:
            return prefix, list(argv[len(prefix):])
    raise CheckRefused("argv_not_allowlisted", " ".join(argv[:3]))


def _validate_placeholder(arg: str, kind: str | None, stage: Path, workdir: Path,
                          jarvis_repo: bool) -> None:
    if kind is None:
        raise CheckRefused("placeholder_not_allowed", "this template takes no arguments")
    if any(c in _METACHARACTERS or ord(c) < 32 or ord(c) == 127 for c in arg):
        raise CheckRefused("shell_metacharacter", repr(arg))
    # "@file" is an argument file to pytest (>= 8.2): options by another door.
    if arg.startswith(("-", "@")):
        raise CheckRefused("not_a_target", "options are not placeholders")
    if kind == "module":
        if not (_CHECK_ARG.fullmatch(arg) and arg.split(".")[0] in ("tests", "test")):
            raise CheckRefused("not_a_target", repr(arg))
        return
    refusal = _target_refusal(arg, jarvis_repo)
    if refusal is not None:
        raise CheckRefused("not_a_target", f"{refusal.code}: {arg!r}")
    # Resolved from the child's cwd, which is where the tool will read it.
    resolved = _canonical(workdir / arg)
    if resolved is None or not _inside(resolved, stage):
        raise CheckRefused("not_a_target", f"not an existing path inside the stage: {arg!r}")


def _environment(scratch: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    home = str(scratch)
    forced = {name: DEAD_PROXY for name in _PROXY_VARS}
    forced.update(
        NO_PROXY="", USERPROFILE=home, HOME=home, APPDATA=home, LOCALAPPDATA=home,
        TEMP=home, TMP=home, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
        PIP_NO_INDEX="1", NPM_CONFIG_OFFLINE="true", NPM_CONFIG_NOPROXY="",
        # A stage ruff config could point its cache-dir outside the stage.
        RUFF_NO_CACHE="true",
    )
    env = trusted_environment(forced=forced, environ=environ)
    # HOMEDRIVE+HOMEPATH is the other spelling of the real home directory.
    for name in ("HOMEDRIVE", "HOMEPATH"):
        env.pop(name, None)
    return env


def _drain(stream: Any, cap: int, sink: bytearray, over: list[bool]) -> None:
    # Keep reading past the cap so the child never blocks on a full pipe.
    while chunk := stream.read(_CHUNK):
        room = cap - len(sink)
        if len(chunk) > room:
            over[0] = True
        sink += chunk[:max(room, 0)]


def _spawn_owned(argv: list[str], env: dict[str, str], cwd: Path) -> tuple[subprocess.Popen, int]:
    """Start ``argv`` suspended, put it in a kill-on-close job, verify, resume.

    ``owned_process.launch_owned`` cannot set env, cwd or pipes, so its exact
    ordering is mirrored here with its own primitives. Returns the job handle,
    which the caller MUST close: closing it is what kills the tree.
    """
    if not owned_process.ownership_is_available():
        raise CheckRefused("ownership_unavailable", "no job object on this platform")
    from ctypes import wintypes

    k32 = owned_process._kernel32
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise CheckRefused("ownership_failed", owned_process._last_error())
    limits = owned_process._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = owned_process._JOB_KILL_ON_CLOSE
    if not k32.SetInformationJobObject(wintypes.HANDLE(job), owned_process._JOB_EXTENDED_LIMIT,
                                       ctypes.byref(limits), ctypes.sizeof(limits)):
        error = owned_process._last_error()
        k32.CloseHandle(wintypes.HANDLE(job))
        raise CheckRefused("ownership_failed", error)
    try:
        popen = subprocess.Popen(  # noqa: S603 - list argv, trusted argv[0], no shell
            argv, shell=False, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=owned_process._CREATE_SUSPENDED | NO_CONSOLE_WINDOW,
        )
    except Exception as exc:  # OSError, or ValueError on a NUL in env
        k32.CloseHandle(wintypes.HANDLE(job))
        raise CheckRefused("spawn_failed", type(exc).__name__) from exc
    handle = int(popen._handle)  # type: ignore[attr-defined]
    # Liveness/containment view only; never .close()d, Popen owns the process handle.
    owned = owned_process.OwnedProcess(pid=popen.pid, job=int(job), process=handle)
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = wintypes.LONG
    if not (owned_process._assign_to_job(int(job), handle) and owned.contained()
            and not owned.job_is_inheritable()
            and ntdll.NtResumeProcess(wintypes.HANDLE(handle)) == 0):
        error = owned_process._last_error()
        popen.kill()
        k32.CloseHandle(wintypes.HANDLE(job))
        popen.communicate()
        raise CheckRefused("ownership_failed", error)
    return popen, int(job)


def run_check(
    argv: Sequence[str],
    *,
    stage: Any,
    cwd: Any = None,
    timeout_s: float = 900.0,
    output_cap: int = _DEFAULT_CAP,
    jarvis_repo: bool = False,
    executables: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Run one allowlisted check in ``stage``; refuse with ``CheckRefused``.

    ``executables`` maps a template's tool name (``python``, ``ruff``, ``npm``)
    to a configured absolute path; otherwise the tool is found on PATH by
    ``trusted_exec``, never inside the stage.
    """
    prefix, placeholders = _template(argv)
    kind, runner = CHECK_TEMPLATES[prefix]
    root = _canonical(stage)
    if root is None or not root.is_dir():
        raise CheckRefused("stage_missing", "the stage is not an existing directory")
    workdir = root if cwd is None else _canonical(cwd)
    if workdir is None or not workdir.is_dir() or not _inside(workdir, root):
        raise CheckRefused("cwd_outside_stage", "the cwd must be the stage or inside it")
    for arg in placeholders:
        _validate_placeholder(arg, kind, root, workdir, jarvis_repo)
    tool = prefix[0]
    trusted = resolve_trusted_executable(
        tool, configured=(executables or {}).get(tool), environ=environ, workspace=[root])
    if trusted is None:
        raise CheckRefused("missing_executable", tool)

    scratch = Path(tempfile.mkdtemp(prefix="jarvis-check-"))
    try:
        env = _environment(scratch, environ)
        try:
            argv0 = trusted.argv0(workspace=[root])
        except TrustedExecutableError as exc:
            raise CheckRefused("untrusted_executable", str(exc)) from exc
        command = [argv0, *prefix[1:], *placeholders]
        started = clock()
        popen, job = _spawn_owned(command, env, workdir)
        out, err = bytearray(), bytearray()
        over = [False]
        timed_out, exit_code = False, None
        readers = [threading.Thread(target=_drain, args=(stream, output_cap, sink, over), daemon=True)
                   for stream, sink in ((popen.stdout, out), (popen.stderr, err))]
        try:
            for reader in readers:
                reader.start()
            exit_code = popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Terminate explicitly (a leaked job handle would defeat kill-on-close),
            # then close: the whole tree goes, so the pipes reach EOF.
            k32 = owned_process._kernel32
            k32.TerminateJobObject(ctypes.c_void_p(job), 1)
            k32.CloseHandle(ctypes.c_void_p(job))
            try:
                popen.wait(timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
        duration = clock() - started
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=30)
        for reader, stream in zip(readers, (popen.stdout, popen.stderr)):
            # A reader still blocked holds the stream lock; close() would hang.
            if reader.is_alive():
                over[0] = True
            else:
                stream.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return CheckResult(
        argv=tuple(command), runner=runner, exit_code=exit_code, duration_s=duration,
        stdout=out.decode("utf-8", "replace"), stderr=err.decode("utf-8", "replace"),
        truncated=over[0], timed_out=timed_out,
    )
