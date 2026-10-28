"""PM21: Codex WRITER worker (PM19 contract) for one project slice in a PM14 stage.

Ported from the conveyor's CodexWorker (scripts/conveyor_workers.py) with the same
argv and environment rules: ``codex exec --json`` with user config ignored, ChatGPT
login and the mxc Windows sandbox forced, ``--sandbox workspace-write`` (writes only
the stage, no network for the model's commands) and ``-C`` the stage. Unlike the
conveyor, the argv is checked against a closed token allowlist, the stage must hold
no link and no ``.codex`` project config (so no MCP server or other config can ride
in), and it is re-validated by the runner right before the OS spawn. Admission
(fresh quota reading, no credits) stays with the caller (PM22 ``route``). Nothing
the CLI prints is trusted: acceptance is decided by the Core checks.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jarvis import codex as jarvis_codex
from jarvis.trusted_exec import trusted_environment
from jarvis.workers.base import AgentProfile, Worker, WorkerResult

SANDBOX = "workspace-write"  # never danger-full-access; network stays off in it
FORCED_LOGIN = "forced_login_method=chatgpt"
WINDOWS_SANDBOX = "windows.sandbox=mxc"
CONFIGS = (FORCED_LOGIN, WINDOWS_SANDBOX)
PROHIBITED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
                  "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID", "AZURE_OPENAI_API_KEY")
# Native binary: a Node hook is never needed and could load code into every child.
DROPPED_ENV = ("NODE_OPTIONS",)
SANDBOX_DIRS = {"TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp", "UV_CACHE_DIR": "uv",
                "RUFF_CACHE_DIR": "ruff", "PIP_CACHE_DIR": "pip"}
UNVERIFIED = "claims are unverified; acceptance is decided by Core checks (PM16/PM17)"
SUMMARY_LIMIT = 2000
OUTPUT_LIMIT = 1 << 20  # only the stdout tail is parsed for the summary
RUN_ID_PREFIX = "codex-writer"


class WorkerRefused(Exception):
    """A refusal with a stable, value-free code (no path or env value in it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProcessRunner(Protocol):
    """Spawns the child. ``start`` calls ``verify_cwd()`` right before the OS spawn
    and spawns nothing if it raises; ``poll`` is the exit code or None while running."""

    def start(self, argv: list[str], cwd: str, env: dict[str, str], stdin: str,
              verify_cwd: Callable[[], None]) -> Any: ...
    def poll(self, handle: Any) -> int | None: ...
    def kill(self, handle: Any) -> None: ...
    def output(self, handle: Any) -> str: ...


def build_argv(exe: str, stage: Any) -> list[str]:
    """Same argv as the conveyor's CodexWorker.argv(worktree)."""
    return [exe, "exec", "--json", "--ignore-user-config", "-c", FORCED_LOGIN,
            "-c", WINDOWS_SANDBOX, "-C", str(stage), "--sandbox", SANDBOX, "-"]


def check_argv(argv: list[str], exe: str, stage: Any) -> list[str]:
    """Closed allowlist: every token is a known flag with an allowed value, each once,
    all present; the task comes on stdin (``-``). Anything else refuses."""
    argv = list(argv)
    if argv[:2] != [exe, "exec"] or argv[-1:] != ["-"]:
        raise WorkerRefused("argv_command")
    values = {"-c": CONFIGS, "-C": (str(stage),), "--sandbox": (SANDBOX,)}
    required = {"--json", "--ignore-user-config", ("-c", FORCED_LOGIN), ("-c", WINDOWS_SANDBOX),
                ("-C", str(stage)), ("--sandbox", SANDBOX)}
    seen = []
    tokens = iter(argv[2:-1])
    for token in tokens:
        if token in values:
            value = next(tokens, None)
            if value not in values[token]:
                raise WorkerRefused("flag_value_refused")
            token = (token, value)
        elif token not in ("--json", "--ignore-user-config"):
            raise WorkerRefused("flag_not_allowed")
        if token in seen:
            raise WorkerRefused("flag_repeated")
        seen.append(token)
    if set(seen) != required:
        raise WorkerRefused("flag_missing")
    return argv


def route_var(key: str) -> bool:
    """Conveyor route_var: a variable that can pick Codex's route, key or provider."""
    u = key.upper()
    return u != "CODEX_HOME" and (u in PROHIBITED_ENV or u.startswith(("OPENAI_", "AZURE_OPENAI", "CODEX_"))
                                  or u.endswith(("_API_KEY", "_BASE_URL")))


def scrubbed_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Conveyor scrubbed_env (allowlist, sanitized PATH, no route variable), minus DROPPED_ENV.
    Proxy variables are outside the allowlist, so none reaches the child."""
    env = trusted_environment(allow=jarvis_codex.CODEX_ALLOWED_ENV_VARS,
                              prefixes=jarvis_codex.CODEX_ALLOWED_ENV_PREFIXES,
                              environ=os.environ if environ is None else environ)
    return {k: v for k, v in env.items() if not route_var(k) and k.upper() not in DROPPED_ENV}


def sandbox_env(env: Mapping[str, str], stage: Any) -> dict[str, str]:
    """Conveyor sandbox_env: <stage>/.codex-tmp/{tmp,uv,ruff,pip} (self-ignored), with
    SANDBOX_DIRS pointing there; an inherited key of any case is dropped."""
    base = Path(stage).resolve(strict=True) / ".codex-tmp"
    for path in (base, *(base / sub for sub in sorted(set(SANDBOX_DIRS.values())))):
        path.mkdir(exist_ok=True)
        if path.resolve() != path:
            raise OSError("codex sandbox dir is a link")
    ignore = base / ".gitignore"
    try:
        st = os.lstat(ignore)
    except FileNotFoundError:
        st = None
    if st is not None and (not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or _is_link(st)):
        raise OSError("codex sandbox .gitignore is a link or not a plain file")
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".gitignore.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("*\n")
        os.replace(tmp, ignore)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    out = {k: v for k, v in env.items() if k.upper() not in SANDBOX_DIRS}
    out.update({k: str(base / sub) for k, sub in SANDBOX_DIRS.items()})
    return out


def _is_link(st: os.stat_result) -> bool:
    # A junction is not S_ISLNK; on Windows every link kind is a reparse point.
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _inside(path: Any, root: Any) -> bool:
    return Path(os.path.normcase(os.path.abspath(path))).is_relative_to(os.path.normcase(os.path.abspath(root)))


def stage_links(root: Any, scandir: Callable[[str], Any] = os.scandir,
                lstat: Callable[[str], Any] = os.lstat) -> None:
    """Refuse any link, junction, reparse point or hard-linked file below ``root``, and a
    ``.codex`` project config dir/file (it could add MCP servers or widen the sandbox).
    Nothing is followed; an unreadable directory refuses (unscanned is unproven)."""
    pending = [os.fspath(root)]
    try:
        while pending:
            with scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_symlink() or _is_link(entry.stat(follow_symlinks=False)):
                        raise WorkerRefused("stage_has_link")
                    if entry.name.lower().rstrip(". ") == ".codex":
                        raise WorkerRefused("stage_has_codex_config")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif lstat(entry.path).st_nlink > 1:
                        raise WorkerRefused("stage_has_link")
    except OSError:
        raise WorkerRefused("stage_scan_failed") from None


def stage_directory(stage: Any) -> Path:
    """``stage`` as the resolved directory the child runs in, or refuse: absolute, no
    ``..``, an existing directory that resolving does not change, holding no link."""
    raw = os.fspath(stage)
    literal = Path(raw)
    if not literal.is_absolute() or ".." in literal.parts:
        raise WorkerRefused("stage_not_absolute")
    try:
        resolved = literal.resolve(strict=True)
    except OSError:
        raise WorkerRefused("stage_missing") from None
    if not resolved.is_dir():
        raise WorkerRefused("stage_not_directory")
    if os.path.normcase(str(resolved)) != os.path.normcase(os.path.abspath(raw)):
        raise WorkerRefused("stage_escape")
    try:
        root_link = _is_link(os.lstat(resolved))
    except OSError:
        raise WorkerRefused("stage_scan_failed") from None
    if root_link:
        raise WorkerRefused("stage_has_link")
    stage_links(resolved)
    return resolved


def _summary(output: str) -> tuple[str, bool]:
    """Last agent message (bounded, data only) and whether a failure event was seen."""
    summary, failed = "", False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if type(event) is not dict:
            continue
        if event.get("type") in ("turn.failed", "error"):
            failed = True
        item = event.get("item")
        if event.get("type") == "item.completed" and type(item) is dict and item.get("type") == "agent_message" \
                and type(item.get("text")) is str:
            summary = item["text"][:SUMMARY_LIMIT]
    return summary, failed


@dataclass
class _Run:
    handle: Any
    stopped: bool = False


class CodexWriterWorker(Worker):
    """PM19 Worker over ``codex exec``, writing only inside one stage."""

    def __init__(self, profile: AgentProfile, stage: Any, *, runner: ProcessRunner, exe: str | None = None,
                 environ: Mapping[str, str] | None = None, home: Any = None) -> None:
        exe = jarvis_codex.codex_executable() if exe is None else exe
        if exe is None:
            raise WorkerRefused("codex_missing")
        if not os.path.isabs(exe):
            raise WorkerRefused("command_not_absolute")
        self.profile = profile
        self.stage = stage
        self._runner = runner
        self._exe = exe
        self._environ = environ
        self._home = os.path.realpath(os.path.expanduser("~") if home is None else os.fspath(home))
        self._runs: dict[str, _Run] = {}

    def start(self, task: str) -> str:
        if type(task) is not str or not task.strip():
            raise WorkerRefused("task_empty")
        if self.profile.network:
            raise WorkerRefused("profile_network")
        if len(self._runs) >= self.profile.budget_sessions:
            raise WorkerRefused("budget_sessions_exhausted")
        cwd = stage_directory(self.stage)

        def verify_cwd() -> None:
            # Runner calls this right before the OS spawn (TOCTOU): still the same
            # real directory, still no link or .codex config in it.
            if os.path.normcase(str(stage_directory(self.stage))) != os.path.normcase(str(cwd)):
                raise WorkerRefused("stage_changed")

        if _inside(os.path.realpath(self._exe), cwd):
            raise WorkerRefused("exe_in_stage")
        if _inside(self._home, cwd):
            raise WorkerRefused("stage_contains_home")
        env = scrubbed_env(self._environ)
        codex_home = os.path.realpath(env.get("CODEX_HOME") or os.path.join(self._home, ".codex"))
        if _inside(codex_home, cwd) or _inside(cwd, codex_home):
            raise WorkerRefused("stage_codex_home_overlap")
        argv = check_argv(build_argv(self._exe, cwd), self._exe, cwd)
        verify_cwd()  # sandbox_env writes into the stage: re-prove it first
        try:
            env = sandbox_env(env, cwd)
        except OSError:
            raise WorkerRefused("sandbox_env_failed") from None
        run_id = f"{RUN_ID_PREFIX}-{len(self._runs) + 1}"
        # The task goes on stdin, never in argv: untrusted text must not parse as a flag.
        handle = self._runner.start(argv, str(cwd), env, task, verify_cwd)
        self._runs[run_id] = _Run(handle)
        return run_id

    def _run(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise WorkerRefused("unknown_run")
        return run

    def status(self, run_id: str) -> str:
        run = self._run(run_id)
        if run.stopped:
            return "stopped"
        return "running" if self._runner.poll(run.handle) is None else "exited"

    def stop(self, run_id: str) -> None:
        run = self._run(run_id)
        if run.stopped:
            return
        if self._runner.poll(run.handle) is None:
            self._runner.kill(run.handle)
        run.stopped = True

    def collect(self, run_id: str) -> WorkerResult:
        run = self._run(run_id)
        code = self._runner.poll(run.handle)
        if code is None:
            # Even after stop(): a tree still dying can still write the stage the gate reads.
            raise WorkerRefused("run_still_running")
        # ponytail: tail slice after the runner already buffered it; cap in the runner (PM16 style) for memory.
        summary, failed = _summary(self._runner.output(run.handle)[-OUTPUT_LIMIT:])
        if run.stopped:
            status, summary = "failed", "worker was stopped before it finished"
        elif code != 0 or failed:
            status = "failed"
        else:
            status = "completed"
        # changed_files stays empty: the real change set is read from the stage by
        # the Core diff gate (PM15), never taken from the worker.
        return WorkerResult(status=status, summary=summary, changed_files=(),
                            evidence=(f"exit_code={code}", UNVERIFIED))
