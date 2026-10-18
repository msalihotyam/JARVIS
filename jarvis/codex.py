"""OpenAI Codex as a General Chat / understanding provider - ChatGPT subscription only.

The official standalone Codex CLI (``@openai/codex``) is used in exactly two
ways, both without any JARVIS credential:

* ``codex app-server`` JSON-RPC, read-only: ``account/read`` proves the
  route is a ChatGPT sign-in (never an API key), ``account/rateLimits/read``
  reports the plan's usage windows, and ``codex debug models`` lists the
  models the account is offered. None of these is a model call.
* ``codex exec`` for one understanding call, with every tool surface this
  build can name turned off. The call is refused before any process starts
  unless the route is a ChatGPT sign-in, the model is one whose tools all run
  through the code-mode host (which is disabled here, so any tool call fails
  closed at the CLI), and no API-key variable is present in the environment.

It proposes exactly what the Claude understanding call proposes, and the
proposal is judged by the same deterministic code: choosing this provider
changes who INTERPRETS a request, never what JARVIS may do.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.trusted_exec import NO_CONSOLE_WINDOW, resolve_trusted_executable, trusted_environment
from jarvis.conversation import ContextProfile
from jarvis.contracts import (
    PROJECT_INSTRUCTION_KEY,
    MODEL_ANSWER_RESULT_KEY,
    Availability,
    ErrorCode,
    OutcomeStatus,
    SessionState,
    StructuredError,
    Task,
    ToolResult,
    build_model_answer,
    redact_credentials,
)

#: Variables that could route the CLI to metered API billing. Only presence is
#: ever checked; values are never read.
CODEX_PROHIBITED_ENV_VARS = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID", "AZURE_OPENAI_API_KEY",
)
CODEX_EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_MODEL_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
#: Every feature that can put a tool, an app, a plugin, memory or a hook in
#: front of the model. Disabled on every call.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "image_generation", "multi_agent", "view_image", "hooks", "plugins", "memories", "goals",
    "sleep_tool", "skill_search", "tool_suggest", "in_app_browser", "code_mode_host", "workspace_dependencies",
    "shell_snapshot", "remote_plugin", "skill_mcp_dependency_install", "tool_call_mcp_elicitation",
    "guardian_approval",
)
CODEX_NO_MEMORIES = ("--disable", "memories")
#: The catalog tool mode under which EVERY tool goes through the code-mode host.
CODEX_SAFE_TOOL_MODE = "code_mode_only"
CODEX_TIMEOUT_SECONDS = 120
CODEX_PROBE_TIMEOUT_SECONDS = 20
CODEX_NOT_LOGGED_IN = "codex_not_logged_in"
CODEX_API_KEY_ROUTE = "codex_api_key_route"


def codex_executable() -> str | None:
    """The native Codex binary. The npm launcher is a script; the binary it
    starts is run directly so no shell ever parses an argument.

    Located by provenance (``jarvis.trusted_exec``), never by a search that can
    reach the current directory: a ``codex.exe`` committed to the workspace
    JARVIS was pointed at is not the Codex CLI (DB-001). The vendored binary
    reached from an npm launcher is validated the same way before it is
    returned - it is found by walking the launcher's own install tree, but it
    still has to be a real file outside every workspace."""
    resolved = resolve_trusted_executable("codex")
    if resolved is None:
        return None
    found = resolved.path
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".ps1", "") and not path.name.lower().endswith(".exe"):
        vendor = path.parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor"
        binaries = sorted(vendor.glob("*/bin/codex.exe")) if vendor.is_dir() else []
        vendored = resolve_trusted_executable("codex", configured=binaries[0]) if binaries else None
        if vendored is not None:
            return vendored.path
        return None if os.name == "nt" else found
    return found


def cli_reason(message: Any) -> str:
    """A Codex CLI error message fit to show: one line, bounded, credential-redacted."""
    if type(message) is not str:
        return ""
    line = " ".join("".join(ch for ch in message if ch >= " " or ch == "\n").split())
    if len(line) > 200:
        line = line[:199] + "…"
    return str(redact_credentials(line)[0])


#: What the Codex CLI legitimately reads: its own configuration/session home
#: and the OS basics ``trusted_exec`` allows every child. An ALLOWLIST, so an
#: unrelated credential JARVIS happens to hold is not present to be leaked
#: (DB-001) - and the metered routes in ``CODEX_PROHIBITED_ENV_VARS`` are
#: excluded by construction rather than by remembering to subtract them.
CODEX_ALLOWED_ENV_PREFIXES = ("CODEX_",)
CODEX_ALLOWED_ENV_VARS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "NODE_OPTIONS")


def _environment() -> dict[str, str]:
    prohibited = {name.upper() for name in CODEX_PROHIBITED_ENV_VARS}
    child = trusted_environment(allow=CODEX_ALLOWED_ENV_VARS, prefixes=CODEX_ALLOWED_ENV_PREFIXES)
    # The prefix allowance must never re-admit a prohibited metered route.
    return {key: value for key, value in child.items() if key.upper() not in prohibited}


def prohibited_env_present() -> bool:
    present = {key.upper() for key in os.environ}
    return any(name in present for name in CODEX_PROHIBITED_ENV_VARS)


def _app_server(executable: str, calls: list[tuple[str, dict | None]], timeout: float) -> dict[str, Any]:
    """Answers of a short read-only app-server session, keyed by method."""
    # app-server loads the user's config.toml (exec runs ignore it), so Codex
    # memories are switched off here explicitly: no JARVIS run reads or writes them.
    process = subprocess.Popen([executable, "app-server", *CODEX_NO_MEMORIES], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                               env=_environment(), shell=False, creationflags=NO_CONSOLE_WINDOW)
    replies: dict[int, dict] = {}
    done = threading.Event()

    def reader() -> None:
        for line in process.stdout:  # type: ignore[union-attr]
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and type(message.get("id")) is int:
                replies[message["id"]] = message
                if all(i in replies for i in range(1, len(calls) + 1)):
                    done.set()
                    return

    threading.Thread(target=reader, daemon=True).start()
    try:
        messages = [{"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "jarvis", "version": "1"}}},
                    {"method": "initialized"}]
        messages += [{"id": i, "method": method, **({"params": params} if params is not None else {})}
                     for i, (method, params) in enumerate(calls, 1)]
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        process.stdin.flush()  # type: ignore[union-attr]
        done.wait(timeout)
    except OSError:
        pass
    finally:
        process.kill()
    return {method: (replies.get(i) or {}).get("result") for i, (method, _) in enumerate(calls, 1)}


def _catalog(executable: str) -> list[dict[str, Any]]:
    try:
        completed = subprocess.run([executable, "debug", "models"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=CODEX_PROBE_TIMEOUT_SECONDS, env=_environment(), shell=False,
                                   creationflags=NO_CONSOLE_WINDOW)
        models = json.loads(completed.stdout).get("models")
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return []
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


def _window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used, minutes, resets = value.get("usedPercent"), value.get("windowDurationMins"), value.get("resetsAt")
    if type(used) not in (int, float) or type(minutes) is not int or type(resets) is not int:
        return None
    return {"used_percent": float(used), "window_minutes": minutes, "resets_at": resets}


def codex_state() -> dict[str, Any]:
    """What is REALLY known about the Codex route, from local non-model calls.

    ``status``: available / not_installed / not_logged_in / api_key_route /
    unavailable. ``models``: the account's listed models whose tools all fail
    closed here (id, name, efforts, default effort, priority, specialty), in
    Codex's own priority order. ``upgrades``: what Codex declares about
    replacing ANY model it returned, listed or not - ``{id: {"model",
    "retirement_at", "specialty"}}``. ``quota``: the plan's usage windows as
    the service reported them, or absent."""
    executable = codex_executable()
    if executable is None:
        return {"status": "not_installed", "models": []}
    if prohibited_env_present():
        return {"status": "api_key_route", "models": []}
    answers = _app_server(executable, [("account/read", {}), ("account/rateLimits/read", None)], CODEX_PROBE_TIMEOUT_SECONDS)
    account = (answers.get("account/read") or {}).get("account")
    if not isinstance(account, dict):
        return {"status": "not_logged_in" if answers.get("account/read") is not None else "unavailable", "models": []}
    if account.get("type") != "chatgpt":
        return {"status": "api_key_route", "models": []}
    state: dict[str, Any] = {"status": "available", "plan": str(account.get("planType") or "")[:32], "models": [], "upgrades": {}}
    limits = (answers.get("account/rateLimits/read") or {}).get("rateLimits")
    if isinstance(limits, dict):
        quota = {name: window for name in ("primary", "secondary") if (window := _window(limits.get(name)))}
        credits = limits.get("credits") if isinstance(limits.get("credits"), dict) else {}
        if type(credits.get("hasCredits")) is bool:
            quota["has_credits"] = credits["hasCredits"]
        if limits.get("rateLimitReachedType") is not None:
            quota["limit_reached"] = True
        if quota:
            quota["checked_at"] = int(time.time())       # when Codex reported it (a reused state keeps this time)
            state["quota"] = quota
    for model in _catalog(executable):
        slug = model.get("slug")
        if type(slug) is not str or not CODEX_MODEL_PATTERN.match(slug):
            continue
        specialty = model.get("model_specialty") if type(model.get("model_specialty")) is str else ""
        upgrade = model.get("upgrade") if isinstance(model.get("upgrade"), dict) else {}
        target = upgrade.get("model") if type(upgrade.get("model")) is str and CODEX_MODEL_PATTERN.match(upgrade["model"]) else ""
        retirement = upgrade.get("retirement_at") if type(upgrade.get("retirement_at")) is str else ""
        if target or retirement:
            state["upgrades"][slug] = {"model": target, "retirement_at": retirement[:40], "specialty": specialty[:32]}
        if model.get("visibility") != "list" or model.get("tool_mode") != CODEX_SAFE_TOOL_MODE:
            continue
        efforts = [level.get("effort") for level in model.get("supported_reasoning_levels") or []
                   if isinstance(level, dict) and level.get("effort") in CODEX_EFFORT_VALUES]
        state["models"].append({"id": slug, "name": str(model.get("display_name") or slug)[:64], "efforts": efforts,
                                "default_effort": model.get("default_reasoning_level") if model.get("default_reasoning_level") in efforts else "",
                                "priority": model.get("priority") if type(model.get("priority")) is int else None,
                                "specialty": specialty[:32]})
    # Codex's own order; a model without a priority goes last, ties by id.
    state["models"].sort(key=lambda m: (m["priority"] is None, m["priority"] or 0, m["id"]))
    return state


#: How long a verified Codex state may be reused for chat. The check costs a
#: separate app-server + catalogue process per message (about 0.7 s measured);
#: the call itself still enforces the route on every message (ChatGPT login
#: forced, API-key variables stripped), so a reused state can never open a
#: metered route.
CODEX_STATE_REUSE_SECONDS = 300.0
_state_cache: dict[str, Any] = {}
_state_lock = threading.Lock()


def remember_codex_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep a state for reuse only when it is fully safe to reuse: a verified
    ChatGPT route whose plan has NO purchased credits (so a stale 'limit not
    reached' can never let credits be spent)."""
    with _state_lock:
        _state_cache.clear()
        quota = state.get("quota") if isinstance(state.get("quota"), dict) else {}
        if state.get("status") == "available" and quota.get("has_credits") is False and not quota.get("limit_reached"):
            _state_cache.update(state=state, at=time.monotonic())
    return state


def forget_codex_state() -> None:
    with _state_lock:
        _state_cache.clear()


def reusable_codex_state() -> dict[str, Any]:
    """A recently verified state, or a fresh check. Never reused when an
    API-key variable has appeared since."""
    if prohibited_env_present():
        forget_codex_state()
        return codex_state()
    with _state_lock:
        cached = _state_cache.get("state")
        if cached is not None and time.monotonic() - _state_cache["at"] < CODEX_STATE_REUSE_SECONDS:
            return cached
    return remember_codex_state(codex_state())


class CodexIntentAdapter:
    """The understanding / General Chat call through Codex. A ``ModelAdapter``."""

    name = "codex-intent"
    provider = "openai"

    def __init__(self, system_prompt: str, model: str, effort: str = "", state: Any = reusable_codex_state) -> None:
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        self._state = state
        self.reported: dict[str, dict[str, Any]] = {}
        self.observed: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()
        #: Task ids cancelled before their CLI spawned; ``complete`` refuses them.
        self._cancelled: set[str] = set()

    def context_profile(self) -> ContextProfile:
        """What one call can carry, REPORTED to the Conversation Context Core,
        which owns the conversation. The request reaches ``codex exec`` on stdin,
        so no command line bounds it; 128k tokens is the conservative window."""
        return ContextProfile(context_window_tokens=128_000, output_reserve_tokens=8_000,
                              transport_chars=None, fixed_chars=len(self.system_prompt))

    def availability(self) -> Availability:
        if codex_executable() is None:
            return Availability(False, "The Codex CLI is not installed.")
        return Availability(True)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            process = self._processes.get(task_id)
            if process is None:
                self._cancelled.add(task_id)  # not spawned yet: never will be
                return False
        if process.poll() is not None:
            return False
        process.terminate()
        return True

    def _refused(self, message: str, code: ErrorCode = ErrorCode.BILLING_ROUTE_UNKNOWN, reason: str = "") -> ToolResult:
        # Any refusal or failure re-verifies the route next time: nothing stale is trusted after a problem.
        forget_codex_state()
        # Every failure names the provider and the model the person chose.
        message = f"Codex · {self.model or 'no model'}: {message}"
        details = {"billing_evidence": reason} if reason else {}
        return ToolResult(outcome=OutcomeStatus.ERROR, summary=message, error=StructuredError(code, message, details))

    def complete(self, task: Task, session: SessionState) -> ToolResult:
        executable = codex_executable()
        if executable is None:
            return self._refused("The Codex CLI is not installed.", ErrorCode.CONNECTOR_UNAVAILABLE)
        state = self._state()
        if state.get("status") != "available":
            reason = CODEX_NOT_LOGGED_IN if state.get("status") == "not_logged_in" else CODEX_API_KEY_ROUTE if state.get("status") == "api_key_route" else ""
            return self._refused("Codex has no verified ChatGPT subscription route; the call was not started.", reason=reason)
        models = {m["id"]: m for m in state.get("models") or []}
        chosen = models.get(self.model)
        if chosen is None:
            return self._refused(f"The Codex model '{self.model}' is not offered to this account with tools disabled; nothing was sent.",
                                 ErrorCode.CONNECTOR_UNAVAILABLE)
        if self.effort and self.effort not in chosen["efforts"]:
            return self._refused(f"The Codex model '{self.model}' does not support effort '{self.effort}'; nothing was sent.",
                                 ErrorCode.CONNECTOR_UNAVAILABLE)
        if (state.get("quota") or {}).get("limit_reached"):
            return self._refused("The Codex plan limit is reached; nothing was sent so no credits can be used.",
                                 ErrorCode.CONNECTOR_UNAVAILABLE)
        # JARVIS's own snapshots of the images the person attached: a local file
        # handle for `-i`, never model context, so they leave the payload here.
        context = dict(task.context)
        images = [str(path) for _, _, path in (context.pop("attachment_files", None) or [])]
        payload, _ = ClaudeCodeConnector._redact({"request": task.objective, "context": context})
        # Gate 4: one concatenated prompt (no system role): the fixed prefix,
        # then the payload, whose own key carries the project instruction.
        instruction, _ = ClaudeCodeConnector._project_instruction(task)
        if instruction is not None:
            payload[PROJECT_INSTRUCTION_KEY] = instruction
        prompt = (self.system_prompt + "\n\nJARVIS intent understanding request. Reply with exactly one JSON object.\n"
                  + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")))
        workdir = tempfile.mkdtemp(prefix="jarvis-codex-")
        command = [executable, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                   "--skip-git-repo-check", "-s", "read-only", "-C", workdir, "-m", self.model,
                   "-c", "approval_policy=never", "-c", "forced_login_method=chatgpt", "-c", "web_search_mode=disabled"]
        if self.effort:
            command += ["-c", f"model_reasoning_effort={self.effort}"]
        for feature in CODEX_DISABLED_FEATURES:
            command += ["--disable", feature]
        if images:
            # Explicit image input, exactly these files. `-i` takes a list, so
            # `--` closes it and `-` still means "the request is on stdin"; the
            # sandbox, the approval policy and the disabled features are unchanged.
            command += ["-i", *images, "--"]
        command.append("-")
        # Spawn and registration share the lock with ``cancel``: a cancel either
        # finds the process or lands first and the CLI is never started.
        with self._lock:
            if task.id in self._cancelled:
                self._cancelled.discard(task.id)
                shutil.rmtree(workdir, ignore_errors=True)
                return self._refused("The call was cancelled before Codex started; nothing was sent.", ErrorCode.CANCELLED)
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding="utf-8", errors="replace", env=_environment(), shell=False,
                                           creationflags=NO_CONSOLE_WINDOW)
            except OSError:
                return self._refused("The Codex CLI could not be started.", ErrorCode.CONNECTOR_UNAVAILABLE)
            self._processes[task.id] = process
        try:
            stdout, _ = process.communicate(prompt, timeout=CODEX_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return self._refused("Codex did not finish before the timeout.", ErrorCode.CONNECTOR_FAILED)
        finally:
            with self._lock:
                self._processes.pop(task.id, None)
                # ponytail: a cancel after this pop (or before an early refusal)
                # leaves one uuid in _cancelled; ids are never reused, so no false refusal.
                self._cancelled.discard(task.id)
            shutil.rmtree(workdir, ignore_errors=True)
        text, usage, failed, notes = "", {}, False, []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message" and type(item.get("text")) is str:
                text = item["text"]
            elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
            elif event.get("type") == "turn.failed":
                failed = True
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                notes.append(error.get("message"))
            elif event.get("type") == "error":
                # Also transient notices (a reconnect that then succeeds): a reason, not a verdict.
                notes.append(event.get("message"))
        # The CLI names no answering model in its event stream: only the model
        # it was asked to use is known, and it is reported as requested.
        self.reported[task.id] = {**{key: usage[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens")
                                     if type(usage.get(key)) is int and usage[key] >= 0}}
        self.observed[task.id] = {"requested_model": self.model, "quota": state.get("quota") or {}}
        if process.returncode != 0 or failed or not text.strip():
            reason = next((cli_reason(note) for note in reversed(notes) if cli_reason(note)), "")
            return self._refused(f"Codex reported: {reason}" if reason else "Codex returned no answer.", ErrorCode.CONNECTOR_FAILED)
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary="Codex answered.",
                          data={MODEL_ANSWER_RESULT_KEY: build_model_answer(text)})
