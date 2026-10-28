"""PM13 scout: one read-only Claude Code exploration of an existing target.

Core runs it once, before the planner call, only when the target folder
already holds something. The child is the PM20 CLI in plan mode with Read as
its only tool, cwd = the target, no Bash/Edit/Write/Glob/Grep/web, an empty
strict MCP config, max turns and a budget; its Read denies are the PM20C set
plus the JARVIS checkout's protected set. Its summary is DATA for the planner
(work_planner quotes and caps it). Any failure or timeout returns None and the
planner runs without it: never retried, never blocking.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from typing import Any

from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.work_planner import JARVIS_ROOT, checkout_relation
from jarvis.workers.claude import (
    _PROTECTED_GLOBS,
    EMPTY_MCP_CONFIG,
    RUN_DIR_PREFIX,
    ProcessRunner,
    WorkerRefused,
    _fold,
    _inside,
    _rule_forms,
    rule_path,
    scrubbed_env,
    stage_directory,
    worker_settings,
)

log = logging.getLogger(__name__)

PERMISSION_MODE = "plan"
SCOUT_TOOLS = ("Read",)
SCOUT_DENIED_TOOLS = ("Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Task", "WebFetch", "WebSearch", "Write")
MAX_TURNS = 8
MAX_BUDGET_USD = 0.5
TIMEOUT_S = 300.0
POLL_S = 0.5
# Fixed: nothing from the brief or the folder goes on stdin or argv.
PROMPT = (
    "Explore the current folder read-only. Summarise its layout, languages, build and test "
    "commands and conventions in at most 300 words. Do not change anything."
)


def should_scout(root: Any) -> bool:
    """True only for an existing, non-empty directory; unreadable -> False.

    A root the planner would refuse unasked (UNC/relative, or above the
    JARVIS checkout) is judged first and never listed or scouted."""
    try:
        if checkout_relation(root)[0] == "ancestor":
            return False
        with os.scandir(root) as entries:
            return any(True for _ in entries)
    except (OSError, TypeError, ValueError):
        return False


def build_argv(command: str, settings: str, max_budget_usd: float = MAX_BUDGET_USD) -> list[str]:
    return [
        command,
        "-p",
        "--output-format", "json",
        "--permission-mode", PERMISSION_MODE,
        "--safe-mode",
        "--settings", settings,
        "--strict-mcp-config",
        "--mcp-config", EMPTY_MCP_CONFIG,
        "--tools", ",".join(SCOUT_TOOLS),
        "--disallowedTools", ",".join(SCOUT_DENIED_TOOLS),
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--max-turns", str(MAX_TURNS),
        "--max-budget-usd", f"{max_budget_usd:.4f}",
    ]  # fmt: skip


def scout_settings(home: Any, stage: Any, run_dir: Any) -> dict[str, Any]:
    """PM20C settings in plan mode, plus Read/Edit denies for the JARVIS
    checkout's ``data/`` and protected set wherever the target is."""
    settings = worker_settings(home, stage, run_dir)
    paths = []
    for root in _rule_forms(JARVIS_ROOT):
        for name in ("data", _fold("data")):
            paths += [f"{root}/{name}", f"{root}/{name}/**"]
        for name in (*_PROTECTED_GLOBS, *map(_fold, _PROTECTED_GLOBS)):
            paths += [f"{root}/**/{name}", f"{root}/**/{name}/**"]
    deny = settings["permissions"]["deny"]
    for tool in ("Edit", "Read"):
        deny += [f"{tool}({path})" for path in dict.fromkeys(paths) if f"{tool}({path})" not in deny]
    settings["permissions"]["defaultMode"] = PERMISSION_MODE
    return settings


def run_scout(
    root: Any,
    *,
    runner: ProcessRunner,
    command: str,
    home: Any = None,
    environ: Mapping[str, str] | None = None,
    timeout_s: float = TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_budget_usd: float = MAX_BUDGET_USD,
) -> str | None:
    """One scout process; its ``result`` text, or None on any refusal/failure."""
    if not os.path.isabs(command):
        raise WorkerRefused("command_not_absolute")
    home = os.path.realpath(os.path.expanduser("~") if home is None else os.fspath(home))
    rule_path(home)
    cwd = stage_directory(root)
    if _inside(home, cwd):
        raise WorkerRefused("stage_contains_home")
    if _inside(os.path.realpath(tempfile.gettempdir()), cwd):
        raise WorkerRefused("run_dir_in_stage")

    def verify_cwd() -> None:
        if os.path.normcase(str(stage_directory(root))) != os.path.normcase(str(cwd)):
            raise WorkerRefused("stage_changed")

    run_dir = os.path.realpath(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX))
    try:
        if _inside(run_dir, cwd) or _inside(cwd, run_dir):
            raise WorkerRefused("run_dir_in_stage")
        settings = os.path.join(run_dir, "settings.json")
        with open(settings, "x", encoding="utf-8") as file:
            json.dump(scout_settings(home, cwd, run_dir), file, indent=1, sort_keys=True)
        argv = build_argv(command, settings, max_budget_usd)
        handle = runner.start(argv, str(cwd), scrubbed_env(environ), PROMPT, verify_cwd)
        try:
            deadline = clock() + timeout_s
            while (code := runner.poll(handle)) is None:
                if clock() >= deadline:
                    raise WorkerRefused("scout_timeout")
                sleep(POLL_S)
        except BaseException:  # timeout or a failing poll: never leave the child running
            runner.kill(handle)
            raise
        payload = ClaudeCodeConnector._parse_payload(runner.output(handle)) or {}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    result = payload.get("result")
    if code != 0 or payload.get("is_error") is not False or payload.get("subtype") != "success" or type(result) is not str:
        raise WorkerRefused("scout_failed")
    return result


def explore(scout: Callable[[str], str | None], root: str) -> str | None:
    """Core's single scout step: skipped for a new/empty target, one call
    otherwise; any failure is logged (code only) and yields None."""
    if not should_scout(root):
        return None
    try:
        notes = scout(root)
    except WorkerRefused as exc:
        log.warning("scout skipped: %s", exc.code)
        return None
    except Exception as exc:  # a scout never blocks the run
        log.warning("scout failed: %s", type(exc).__name__)
        return None
    return notes if type(notes) is str and notes.strip() else None
