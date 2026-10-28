"""PM20 Claude Code WRITER worker: one project slice, one PM14 stage worktree.

Everything that decides what the child may do is fixed here, before any
process exists: the argv is built from the AgentProfile and then re-checked
against a closed flag allowlist, the working directory must be the stage the
caller handed in (resolved, no ``..``, no link escape), and the environment is
an allowlist with credentials and proxies taken out. Each start writes a
per-run ``--settings`` file (outside the stage) that denies edits to the PM10
protected set, and the stage is re-validated immediately before the spawn.
Any refusal raises ``WorkerRefused`` and the runner is never called.

The child's report is DATA. ``collect`` passes its claims through as a
``WorkerResult`` and never decides acceptance; that stays with the Core checks
(PM15 diff scope, PM16/PM17 check runs).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from jarvis.connectors.claude_code import (
    CLAUDE_ALLOWED_ENV_PREFIXES,
    CLAUDE_ALLOWED_ENV_VARS,
    CLAUDE_FORCED_ENV,
    PROHIBITED_BILLING_ENV_VARS,
    ClaudeCodeConnector,
)
from jarvis.settings import validate_claude_effort, validate_claude_model
from jarvis.trusted_exec import trusted_environment
from jarvis.work_plan import AGENT_CONFIG_NAMES
from jarvis.workers.base import AgentProfile, Worker, WorkerResult

# The only tools a writer gets: edit files. No Bash (no shell, no network
# through it), no WebFetch/WebSearch, no Task (no subagent fan-out).
# No Read/Glob/Grep either (PM20B): permission rules have no negation and deny
# beats allow, so "nothing outside the stage" cannot be written without also
# denying a stage that lives under the denied home; Glob/Grep take any path and
# Read rules reach them only best-effort. Unprovable confinement -> dropped.
WRITER_TOOLS = frozenset({"Edit", "Write"})
# Denied explicitly as well, so a CLI default that re-adds one still loses.
DENIED_TOOLS = ("Bash", "Glob", "Grep", "NotebookEdit", "Read", "Task", "WebFetch", "WebSearch")
EMPTY_MCP_CONFIG = json.dumps({"mcpServers": {}}, separators=(",", ":"))
PERMISSION_MODE = "acceptEdits"
WORKER_FORCED_ENV = {
    **CLAUDE_FORCED_ENV,
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    # Account (claude.ai) connectors off, and no non-essential traffic
    # (telemetry, error reporting, auto-update). That the CLI honours these is
    # runtime proof for PM24/PM33; here only their presence is tested.
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
RUN_DIR_PREFIX = "jarvis-claude-run-"
# PM10 protected set (jarvis/work_plan.py keeps these private; mirrored here).
# Rules match case-sensitively, so each name is also emitted case-folded
# ([gG][iI][tT]); the case-insensitive PM10/PM15 diff gate stays the real check.
_PROTECTED_GLOBS = (
    ".env*", "*.db", "*.db-*", "*.sqlite*",
    "*.key", "*.pem", "*.crt", "*.cer", "*.der", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".jarvis", ".git", ".github", ".ssh", *AGENT_CONFIG_NAMES,
)  # fmt: skip
# Under the user home: never edited.
_HOME_ROOTS = (".jarvis", ".ssh", ".claude", ".codex")
# A path holding one of these cannot be written into a rule literally.
_RULE_UNSAFE = frozenset("*?[]{}!()")
SUMMARY_LIMIT = 2000
UNVERIFIED = "claims are unverified; acceptance is decided by Core checks (PM16/PM17)"

_PROXY_NAMES = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NPM_CONFIG_PROXY", "NPM_CONFIG_HTTPS_PROXY"}
)
_SECRET_SUFFIXES = ("_API_KEY", "_BASE_URL", "_SECRET")
# Through the CLAUDE_ prefix / allowlist but not wanted: provider redirects,
# a shell or env file the CLI would source, and NODE_OPTIONS (--require).
_DROPPED_NAMES = frozenset({"CLAUDE_ENV_FILE", "CLAUDE_CODE_SHELL", "NODE_OPTIONS"})
_DROPPED_PREFIXES = ("CLAUDE_CODE_USE_",)
_TURNS = re.compile(r"[1-9][0-9]{0,3}")
_USD = re.compile(r"[0-9]{1,3}\.[0-9]{4}")
_SUBTYPE = re.compile(r"[a-z_]{1,40}")


class WorkerRefused(Exception):
    """A launch refused before any process start. ``code`` is stable and value-free."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProcessRunner(Protocol):
    """The only way this worker touches a process; tests inject a fake."""

    def start(
        self, argv: list[str], cwd: str, env: dict[str, str], stdin: str, verify_cwd: Callable[[], None]
    ) -> Any:
        """Spawn, calling ``verify_cwd()`` immediately before the OS spawn; if it
        raises, nothing is spawned and the exception propagates."""
        ...

    def poll(self, handle: Any) -> int | None: ...

    def kill(self, handle: Any) -> None: ...

    def output(self, handle: Any) -> str: ...


def _model_ok(value: str) -> bool:
    try:
        return not value.startswith("-") and validate_claude_model(value) == value
    except ValueError:
        return False


def _effort_ok(value: str) -> bool:
    try:
        return validate_claude_effort(value) == value
    except ValueError:
        return False


def _tools_ok(value: str) -> bool:
    names = value.split(",")
    return bool(value) and len(set(names)) == len(names) and set(names) <= WRITER_TOOLS


def _usd_ok(value: str) -> bool:
    return bool(_USD.fullmatch(value)) and float(value) > 0


# Closed allowlist: flag -> value check (None = flag takes no value). Anything
# else - --dangerously-skip-permissions, --add-dir, --allowedTools,
# --append-system-prompt, --resume, an attached ``--flag=value`` - is refused.
# --settings is checked in check_argv: only this run's generated file passes.
_FLAGS: dict[str, Any] = {
    "-p": None,
    "--output-format": lambda v: v == "json",
    "--permission-mode": lambda v: v == PERMISSION_MODE,
    # No user/project/local settings: a settings.json written into the stage
    # (hooks, permissions.allow, env, apiKeyHelper) must not load next session.
    "--safe-mode": None,
    "--settings": "run settings path",
    "--strict-mcp-config": None,
    "--mcp-config": lambda v: v == EMPTY_MCP_CONFIG,
    "--tools": _tools_ok,
    "--disallowedTools": lambda v: v == ",".join(DENIED_TOOLS),
    "--disable-slash-commands": None,
    "--no-chrome": None,
    "--no-session-persistence": None,
    "--max-turns": lambda v: bool(_TURNS.fullmatch(v)),
    "--max-budget-usd": _usd_ok,
    "--model": _model_ok,
    "--effort": _effort_ok,
}
_REQUIRED = frozenset(_FLAGS) - {"--model", "--effort"}


def check_argv(argv: list[str], command: str, settings: str) -> None:
    """Refuse ``argv`` unless every flag is allowlisted, once, with an allowed
    value; ``--settings`` must name exactly ``settings`` (this run's file)."""
    if not argv or argv[0] != command:
        raise WorkerRefused("argv_command")
    seen: set[str] = set()
    index = 1
    while index < len(argv):
        flag = argv[index]
        if flag not in _FLAGS:
            raise WorkerRefused("flag_not_allowed")
        if flag in seen:
            raise WorkerRefused("flag_repeated")
        seen.add(flag)
        check = (lambda v: v == settings) if flag == "--settings" else _FLAGS[flag]
        if check is None:
            index += 1
            continue
        if index + 1 >= len(argv) or not check(argv[index + 1]):
            raise WorkerRefused("flag_value_refused")
        index += 2
    if not _REQUIRED <= seen:
        raise WorkerRefused("flag_missing")


def scrubbed_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's environment: CLI allowlist, minus credentials and proxies.

    The CLI keeps its own login surface (``CLAUDE_*``, config dirs). Billing
    and endpoint overrides (incl. every ``CLAUDE_CODE_USE_*``), any
    ``*_API_KEY``/``*_BASE_URL``/``*_SECRET``, ``NODE_OPTIONS``, the CLI's
    shell/env-file hooks and every proxy variable (they can carry credentials
    in the URL) are dropped.
    No dead proxy is forced: the CLI needs its own model endpoint, and egress
    by the model is closed by the tool allowlist instead.
    """
    child = trusted_environment(
        allow=CLAUDE_ALLOWED_ENV_VARS, prefixes=CLAUDE_ALLOWED_ENV_PREFIXES, environ=environ
    )
    dropped = {
        name.upper()
        for name in (*PROHIBITED_BILLING_ENV_VARS, *_PROXY_NAMES, *_DROPPED_NAMES, *WORKER_FORCED_ENV)
    }
    child = {
        key: value
        for key, value in child.items()
        if key.upper() not in dropped
        and not key.upper().endswith(_SECRET_SUFFIXES)
        and not key.upper().startswith(_DROPPED_PREFIXES)
    }
    child.update(WORKER_FORCED_ENV)
    return child


def stage_directory(stage: Any) -> Path:
    """``stage`` as the resolved directory the child runs in, or refuse.

    Absolute, no ``..`` part, existing directory, and resolving changes
    nothing: a symlink/junction anywhere on the path makes the resolved path
    differ from the literal one and is refused rather than followed.
    """
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
    return resolved


def _inside(path: Any, root: Any) -> bool:
    return Path(os.path.normcase(os.path.abspath(path))).is_relative_to(os.path.normcase(os.path.abspath(root)))


def rule_path(path: Any) -> str:
    """An absolute drive path as a permission-rule path: ``C:\\U\\x`` -> ``//c/U/x``."""
    pure = PureWindowsPath(os.fspath(path))
    if (
        not re.fullmatch(r"[A-Za-z]:", pure.drive)
        or not pure.is_absolute()
        or ".." in pure.parts
        or _RULE_UNSAFE & set(str(pure))
    ):
        raise WorkerRefused("rule_path_unsafe")
    return "//" + pure.drive[0].lower() + "".join("/" + part for part in pure.parts[1:])


def _fold(name: str) -> str:
    """A case-insensitive glob for ``name``: ``.git`` -> ``.[gG][iI][tT]``."""
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in name)


def worker_settings(home: Any, stage: Any, run_dir: Any) -> dict[str, Any]:
    """The per-run ``--settings`` content: deterministic for its inputs.

    Read/Glob/Grep are denied whole (see WRITER_TOOLS). Edit rules cover every
    file-editing tool: the PM10 protected set anywhere under the stage,
    ``data/`` at its root, the home's agent/secret dirs, this run's own dir,
    and the whole home unless the stage lives under it (deny beats allow, so
    that one cannot be written without denying the stage too).
    """
    home_rule = rule_path(home)
    deny = ["Read", "Glob", "Grep"]
    for name in (*_PROTECTED_GLOBS, *map(_fold, _PROTECTED_GLOBS)):
        deny += [f"Edit(**/{name})", f"Edit(**/{name}/**)"]
    for name in ("data", _fold("data")):
        deny += [f"Edit(./{name})", f"Edit(./{name}/**)"]
    for name in (*_HOME_ROOTS, *map(_fold, _HOME_ROOTS)):
        deny += [f"Edit(~/{name})", f"Edit(~/{name}/**)", f"Edit({home_rule}/{name})", f"Edit({home_rule}/{name}/**)"]
    deny.append(f"Edit({rule_path(run_dir)}/**)")
    # Stage equal to the home is not "under" it: the whole home stays denied.
    if not _inside(stage, home) or _inside(home, stage):
        deny.append(f"Edit({home_rule}/**)")
    return {
        "permissions": {
            "defaultMode": PERMISSION_MODE,
            "disableBypassPermissionsMode": "disable",
            "deny": list(dict.fromkeys(deny)),
        },
        "disableAllHooks": True,
        "enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": [],
    }


@dataclass
class _Run:
    handle: Any
    run_dir: str
    stopped: bool = False


class ClaudeWriterWorker(Worker):
    """PM19 Worker over the Claude Code CLI, writing only inside one stage."""

    def __init__(
        self,
        profile: AgentProfile,
        stage: Any,
        *,
        runner: ProcessRunner,
        command: str,
        max_budget_usd: float,
        model: str | None = None,
        effort: str | None = None,
        environ: Mapping[str, str] | None = None,
        home: Any = None,
    ) -> None:
        if not os.path.isabs(command):
            raise WorkerRefused("command_not_absolute")
        # Resolved (8.3 short names, junctions) so rules and _inside see the real path.
        home = os.path.realpath(os.path.expanduser("~") if home is None else os.fspath(home))
        rule_path(home)
        if type(max_budget_usd) not in (int, float) or not math.isfinite(max_budget_usd) or max_budget_usd <= 0:
            raise WorkerRefused("budget_usd_invalid")
        if model is not None and not _model_ok(model):
            raise WorkerRefused("model_invalid")
        if effort is not None and not _effort_ok(effort):
            raise WorkerRefused("effort_invalid")
        self.profile = profile
        self.stage = stage
        self._runner = runner
        self._command = command
        self._max_budget_usd = float(max_budget_usd)
        self._model = model
        self._effort = effort
        self._environ = environ
        self._home = home
        self._runs: dict[str, _Run] = {}

    def build_argv(self, settings: str) -> list[str]:
        """The full argv, deterministic for a given profile, settings file and options."""
        argv = [
            self._command,
            "-p",
            "--output-format", "json",
            "--permission-mode", PERMISSION_MODE,
            "--safe-mode",
            "--settings", settings,
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--tools", ",".join(sorted(self.profile.tools)),
            "--disallowedTools", ",".join(DENIED_TOOLS),
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--max-turns", str(self.profile.max_turns),
            "--max-budget-usd", f"{self._max_budget_usd:.4f}",
        ]  # fmt: skip
        if self._model is not None:
            argv += ["--model", self._model]
        if self._effort is not None:
            argv += ["--effort", self._effort]
        return argv

    def launch_cwd(self) -> Path:
        """Where the child runs: the validated stage, nothing else."""
        return stage_directory(self.stage)

    def _check_profile(self) -> None:
        profile = self.profile
        if profile.network:
            raise WorkerRefused("profile_network")
        if not profile.tools or not profile.tools <= WRITER_TOOLS:
            raise WorkerRefused("profile_tools")
        if len(self._runs) >= profile.budget_sessions:
            raise WorkerRefused("budget_sessions_exhausted")

    def start(self, task: str) -> str:
        if type(task) is not str or not task.strip():
            raise WorkerRefused("task_empty")
        self._check_profile()
        stage = stage_directory(self.stage)
        cwd = self.launch_cwd()
        if os.path.normcase(str(cwd)) != os.path.normcase(str(stage)):
            raise WorkerRefused("cwd_mismatch")

        def verify_cwd() -> None:
            # Runner calls this right before the OS spawn (TOCTOU): the stage
            # must still be the same real directory, not swapped for a link.
            if os.path.normcase(str(stage_directory(self.stage))) != os.path.normcase(str(cwd)):
                raise WorkerRefused("stage_changed")

        run_dir = os.path.realpath(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX))
        try:
            if _inside(run_dir, cwd) or _inside(cwd, run_dir):
                raise WorkerRefused("run_dir_in_stage")
            settings = os.path.join(run_dir, "settings.json")
            with open(settings, "x", encoding="utf-8") as file:
                json.dump(worker_settings(self._home, cwd, run_dir), file, indent=1, sort_keys=True)
            argv = self.build_argv(settings)
            check_argv(argv, self._command, settings)
            env = scrubbed_env(self._environ)
            run_id = f"claude-writer-{len(self._runs) + 1}"
            # The task goes on stdin, never in argv: it is untrusted text and
            # must not be parseable as a flag.
            handle = self._runner.start(argv, str(cwd), env, task, verify_cwd)
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        self._runs[run_id] = _Run(handle, run_dir)
        return run_id

    def status(self, run_id: str) -> str:
        run = self._runs[run_id]
        if run.stopped:
            return "stopped"
        return "running" if self._runner.poll(run.handle) is None else "exited"

    def stop(self, run_id: str) -> None:
        run = self._runs[run_id]
        if run.stopped:
            return
        if self._runner.poll(run.handle) is None:
            self._runner.kill(run.handle)
        run.stopped = True

    def collect(self, run_id: str) -> WorkerResult:
        run = self._runs[run_id]
        code = self._runner.poll(run.handle)
        if code is None and not run.stopped:
            raise WorkerRefused("run_still_running")
        payload = ClaudeCodeConnector._parse_payload(self._runner.output(run.handle)) or {}
        shutil.rmtree(run.run_dir, ignore_errors=True)
        raw_subtype = payload.get("subtype")
        subtype = raw_subtype if type(raw_subtype) is str and _SUBTYPE.fullmatch(raw_subtype) else "unknown"
        result = payload.get("result")
        summary = result[:SUMMARY_LIMIT] if type(result) is str else ""
        if run.stopped:
            status, summary = "failed", "worker was stopped before it finished"
        elif not payload:
            status, summary = "failed", "worker produced no parseable result"
        elif subtype == "error_max_turns":
            status = "partial"
        elif subtype == "error_max_budget_usd":
            status = "blocked_by_gate"
        elif code != 0 or payload.get("is_error") is not False or subtype != "success":
            status = "failed"
        else:
            status = "completed"
        # changed_files stays empty: the real change set is read from the
        # stage by the Core diff gate (PM15), never taken from the worker.
        return WorkerResult(
            status=status,
            summary=summary,
            changed_files=(),
            evidence=(f"exit_code={code}", f"subtype={subtype}", UNVERIFIED),
        )
