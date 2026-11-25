"""Brain session command builder (BRAINRUN1): no built-in tools, one tool server.

The brain is a Claude Code session that sees the repo read-only through our
own tool server (``read_file``, ``list_dir``, ``search``, ``write_proposal``).
This module only builds the argv / env / tool-server config and runs it
through an injected runner; nothing in the running app imports it, and it
has no default runner, so it can never spawn a real process by itself.

Every public call returns a value: a result or ``Refused`` / ``Failure``
with an ASCII code. No exception escapes and no input is echoed.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess  # nosec B404 - only for TimeoutExpired; spawning is the injected runner's job
from pathlib import Path
from typing import Any, Callable, NamedTuple

from jarvis.connectors.claude_code import PROHIBITED_BILLING_ENV_VARS
from jarvis.project_folders import _DRIVE_FIXED, _drive_type, _reserved
from jarvis.settings import validate_claude_effort, validate_claude_model
from jarvis.workers.claude import scrubbed_env

TOOL_SERVER_NAME = "jarvis_brain"
TOOL_NAMES = ("read_file", "list_dir", "search", "write_proposal")
# Claude Code's permission name for an MCP tool is mcp__<server>__<tool>.
ALLOWED_TOOLS = tuple(f"mcp__{TOOL_SERVER_NAME}__{name}" for name in TOOL_NAMES)
MAX_PACKET_BYTES = 1024 * 1024
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PROTECTED_PARTS = frozenset(("data", ".jarvis"))


class BrainConfig(NamedTuple):
    command: str  # absolute path of the claude CLI
    root: Any  # the repo root the session runs in
    model: str
    tool_server_command: str
    tool_server_args: tuple = ()
    effort: str | None = None
    max_turns: int = 8
    max_budget_usd: float = 1.0
    timeout_seconds: int = 600
    max_output_bytes: int = 4 * 1024 * 1024
    # False: the CLI keeps no transcript (--no-session-persistence) and resume() is refused.
    # True: the first session is stored so resume() can continue it; the caller opts in knowingly.
    resumable: bool = False
    # Asking for any of these is refused: the brain gets none of them.
    builtin_tools: tuple = ()
    extra_tool_servers: tuple = ()
    add_dirs: tuple = ()
    extra_flags: tuple = ()


class BrainCommand(NamedTuple):
    argv: tuple
    env: dict
    mcp_config: str
    cwd: Path
    timeout_seconds: int
    max_output_bytes: int


class BrainReply(NamedTuple):
    text: str
    session_id: str


class Refused(NamedTuple):
    code: str


class Failure(NamedTuple):
    code: str


Runner = Callable[..., Any]  # subprocess.run-shaped: (argv, *, cwd, env, input, timeout) -> .returncode/.stdout


class _Refuse(Exception):
    pass


def build(config: Any, environ: Any = None) -> BrainCommand | Refused:
    return _guard(lambda: _build(config, environ, None))


def resume(config: Any, session_id: Any, environ: Any = None) -> BrainCommand | Refused:
    return _guard(lambda: _build(config, environ, _session_id(session_id)))


def _guard(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except _Refuse as exc:
        return Refused(str(exc))
    except Exception as exc:  # fail closed: any helper error is a refusal, never an escape
        return Refused("internal_" + type(exc).__name__.encode("ascii", "replace").decode("ascii"))


def _session_id(value: Any) -> str:
    if type(value) is not str or not _SESSION_ID.fullmatch(value):
        raise _Refuse("session_id_invalid")
    return value


def _build(config: Any, environ: Any, session_id: str | None) -> BrainCommand:
    if type(config) is not BrainConfig:
        raise _Refuse("config_invalid")
    if config.builtin_tools:
        raise _Refuse("builtin_tools_refused")
    if config.extra_tool_servers:
        raise _Refuse("extra_tool_server_refused")
    if config.add_dirs:
        raise _Refuse("add_dirs_refused")
    if config.extra_flags:  # closed argv: no extra flag at all, bypass or otherwise
        raise _Refuse("extra_flags_refused")
    if type(config.resumable) is not bool:
        raise _Refuse("resumable_invalid")
    if session_id and not config.resumable:
        raise _Refuse("resume_not_enabled")
    root = _root(config.root)
    command = _program(config.command, "command_invalid")
    server = _program(config.tool_server_command, "tool_server_invalid")
    args = config.tool_server_args  # inside the JSON config, not argv: a leading dash ("-m") is fine
    if type(args) is not tuple or not all(type(a) is str and a and "\x00" not in a for a in args):
        raise _Refuse("tool_server_args_invalid")
    model, effort = _model(config.model, config.effort)
    turns = _int(config.max_turns, 1, 100, "max_turns_invalid")
    timeout = _int(config.timeout_seconds, 1, 3600, "timeout_invalid")
    max_output = _int(config.max_output_bytes, 1, 64 * 1024 * 1024, "max_output_invalid")
    budget = config.max_budget_usd
    if type(budget) not in (int, float) or not math.isfinite(budget) or not 0.01 <= budget <= 100:
        raise _Refuse("max_budget_invalid")
    env = _env(environ)
    mcp_config = json.dumps(
        {"mcpServers": {TOOL_SERVER_NAME: {"command": server, "args": list(config.tool_server_args)}}},
        separators=(",", ":"),
    )
    if session_id:
        session = ["--resume", session_id]
    else:  # a non-resumable first session leaves no transcript in the CLI session store
        session = [] if config.resumable else ["--no-session-persistence"]
    argv = [
        command, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--tools", "", "--safe-mode", "--strict-mcp-config", "--mcp-config", mcp_config,
        "--permission-mode", "dontAsk", "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disable-slash-commands", "--no-chrome", *session,
        "--max-turns", str(turns), "--max-budget-usd", f"{float(budget):.4f}", "--model", model,
    ]
    if effort is not None:
        argv += ["--effort", effort]
    return BrainCommand(tuple(argv), env, mcp_config, root, timeout, max_output)


def _plain(value: Any) -> bool:
    return type(value) is str and bool(value) and "\x00" not in value and not value.startswith("-")


def _program(value: Any, code: str) -> str:
    if not _plain(value) or not Path(value).is_absolute():
        raise _Refuse(code)
    return value


def _int(value: Any, low: int, high: int, code: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _Refuse(code)
    return value


def _model(model: Any, effort: Any) -> tuple[str, str | None]:
    try:
        if not _plain(model) or validate_claude_model(model) != model:
            raise _Refuse("model_invalid")
        if effort is not None and (not _plain(effort) or validate_claude_effort(effort) != effort):
            raise _Refuse("effort_invalid")
    except ValueError:
        raise _Refuse("model_invalid") from None
    return model, effort


def _root(value: Any) -> Path:
    if type(value) not in (str, type(Path())):  # exact types: a subclass's __fspath__ would run caller code
        raise _Refuse("root_invalid")
    text = os.fspath(value)
    # UNC / device anchors would make a build do network or device I/O: refused lexically.
    if not text or "\x00" in text or text[:1] in ("\\", "/") or not Path(text).is_absolute():
        raise _Refuse("root_invalid")
    path = Path(text)
    if any(_reserved(part) for part in path.parts[1:]):  # C:\NUL, C:\x\com1.txt open a device, not a folder
        raise _Refuse("root_invalid")
    try:  # a mapped (remote), removable or unknown drive is refused before any stat on it
        fixed = _drive_type(path.anchor) == _DRIVE_FIXED
    except Exception:  # noqa: BLE001 - an unanswered question refuses
        fixed = False
    if not fixed:
        raise _Refuse("root_not_local")
    # lstat each component before resolve: a link or junction could point at a share, and resolve would follow it.
    for part in (*reversed(path.parents), path):
        try:
            info = os.lstat(part)
        except OSError:
            raise _Refuse("root_missing") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise _Refuse("root_not_canonical")
    if not stat.S_ISDIR(info.st_mode):
        raise _Refuse("root_not_dir")
    # shortcut: a component swapped to a link between this walk and resolve is followed; the UNC check below
    # still refuses the result. Hold the folder handle (project_folders._hold_folder) if a caller ever needs it.
    try:
        root = path.resolve(strict=True)
    except OSError:
        raise _Refuse("root_missing") from None
    if str(root).startswith(("\\", "/")) or not root.is_dir():  # a mapped drive resolves to UNC
        raise _Refuse("root_not_dir")
    if os.path.normcase(text) != os.path.normcase(str(root)):  # ".", "..", link, trailing separator
        raise _Refuse("root_not_canonical")
    # Every path under data/ or ~/.jarvis has one of these parts, whatever the home dir is.
    if any(part.casefold() in _PROTECTED_PARTS for part in root.parts):
        raise _Refuse("root_protected")
    return root


def _env(environ: Any) -> dict[str, str]:
    source = os.environ if environ is None else environ
    billing = {name.upper() for name in PROHIBITED_BILLING_ENV_VARS}
    if any(str(key).upper() in billing for key in source):  # present even when empty
        raise _Refuse("billing_env_present")
    return scrubbed_env(dict(source))


def run(command: Any, packet: Any, runner: Runner) -> BrainReply | Failure:
    """Run ``command`` with ``packet`` on stdin; a reply, or a typed failure and no partial reply."""
    try:
        return _run(command, packet, runner)
    except Exception as exc:  # fail closed
        return Failure("internal_" + type(exc).__name__.encode("ascii", "replace").decode("ascii"))


def _run(command: Any, packet: Any, runner: Runner) -> BrainReply | Failure:
    if type(command) is not BrainCommand or not callable(runner):
        return Failure("command_invalid")
    if type(packet) is not str or not packet or len(packet.encode("utf-8", "surrogatepass")) > MAX_PACKET_BYTES:
        return Failure("packet_invalid")
    stdin = json.dumps({"type": "user", "message": {"role": "user", "content": packet}}, ensure_ascii=False) + "\n"
    try:
        done = runner(
            list(command.argv), cwd=str(command.cwd), env=dict(command.env), input=stdin,
            timeout=command.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return Failure("timeout")
    except Exception:
        return Failure("runner_error")
    if type(getattr(done, "returncode", None)) is not int or done.returncode != 0:
        return Failure("exit_nonzero")
    stdout = getattr(done, "stdout", None)
    # shortcut: size is checked after the runner buffered it; the real runner must cap its own read.
    if type(stdout) is not str or len(stdout.encode("utf-8", "surrogatepass")) > command.max_output_bytes:
        return Failure("output_too_large" if type(stdout) is str else "output_invalid")
    return _reply(stdout)


def _reply(stdout: str) -> BrainReply | Failure:
    for line in reversed(stdout.split("\n")):  # not splitlines: U+2028 may sit unescaped inside a JSON string
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "result":
            continue
        text, sid = payload.get("result"), payload.get("session_id")
        if payload.get("is_error") is not False or type(text) is not str:
            return Failure("reply_error")
        if type(sid) is not str or not _SESSION_ID.fullmatch(sid):
            return Failure("reply_session_invalid")
        return BrainReply(text, sid)
    return Failure("reply_malformed")
