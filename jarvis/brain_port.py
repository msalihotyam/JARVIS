"""Brain port: the one ask(packet) the talk core calls.

One call runs one turn of the brain session through an injected runner,
with the injected tool server attached, and returns a validated Reply or a
typed Failure; no exception leaves ask(). This module starts no process
and opens no file or socket: the runner is the only thing that starts a
process, the injected argv builder (the brain session) is the only thing
that knows the command line, and proposals reach disk only through the tool
server's propose tool. The packet travels as stdin bytes, never in argv.

Runner contract: runner(argv, stdin=bytes, timeout=seconds) returns the
exact tuple (exit_code: int, stdout: bytes, session_id: str | None,
needs_you: bool) and raises TimeoutError after ending the run itself.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass

from jarvis import talk_rotate

MAX_OUTPUT_BYTES = 16_000
MAX_REPLY_CHARS = 4000
# ASCII only, no leading "-" so a stored id can never turn into an argv flag.
SESSION_ID = r"[A-Za-z0-9][A-Za-z0-9-]{0,63}"
# Same names as the Claude Code connector's billing guard; presence alone refuses.
METERED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

FAILURES = {
    "busy": "Another brain call is running.",
    "bad_packet": "The packet must be non-empty text.",
    "metered_env": "A metered API credential is set; the brain call was refused.",
    "bad_limit": "The brain time limit must be a finite number of seconds above zero.",
    "timeout": "The brain call ran past its time limit.",
    "exit": "The brain call ended with an error.",
    "runner_failed": "The brain runner failed.",
    "oversized": "The brain output was over the size limit.",
    "unreadable": "The brain output was not readable text.",
    "empty": "The brain output was empty.",
    "internal": "The brain port failed internally.",
}


@dataclass(frozen=True)
class Counters:
    turns: int
    seconds: float
    rotations: int
    failures: int


@dataclass(frozen=True)
class Reply:
    text: str
    needs_you: bool
    counters: Counters


@dataclass(frozen=True)
class Failure:
    code: str
    text: str
    counters: Counters


class _Bad(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BrainPort:
    def __init__(self, runner, argv_for, tool_server, env, clock, limit_seconds, context_limit):
        self._runner = runner
        self._argv_for = argv_for  # (session_id | None, tool_server) -> argv
        self._tool_server = tool_server
        self._env = env
        self._clock = clock
        self._limit = limit_seconds
        self._context_limit = context_limit
        self._lock = threading.Lock()
        self._session = None
        self._used = 0  # characters sent and received in the current session
        self._turns = self._rotations = self._failures = 0
        self._seconds = 0.0

    def counters(self) -> Counters:
        return Counters(self._turns, self._seconds, self._rotations, self._failures)

    def ask(self, packet):
        if not self._lock.acquire(blocking=False):
            return Failure("busy", FAILURES["busy"], self.counters())
        try:
            try:
                return self._ask(packet)
            except _Bad as bad:
                # An injected callable could raise _Bad with an unknown code: never let KeyError out.
                code = bad.code if bad.code in FAILURES else "internal"
            except Exception:  # noqa: BLE001 - nothing may escape ask()
                code = "internal"
            self._failures += 1
            return Failure(code, FAILURES[code], self.counters())
        finally:
            self._lock.release()

    def _ask(self, packet):
        if type(packet) is not str or not packet.strip():
            raise _Bad("bad_packet")
        try:
            stdin = packet.encode("utf-8")
        except UnicodeEncodeError:
            raise _Bad("bad_packet") from None
        # A non-str key (bytes, subclass) cannot be checked by name: refuse it as metered.
        if any(type(key) is not str or key.upper() in METERED_ENV for key in self._env):
            raise _Bad("metered_env")
        limit = self._limit
        # Finite, above zero, not bool; "not 0 < x" refuses NaN; an int past float range is refused too.
        if type(limit) not in (int, float) or not 0 < limit <= sys.float_info.max:
            raise _Bad("bad_limit")
        if self._session is not None:
            verdict = talk_rotate.decide(self._used, self._context_limit)
            # A refusal from the rotation rule rotates too: fail closed.
            if type(verdict) is not talk_rotate.Decision or verdict.verdict == talk_rotate.ROTATE:
                self._session, self._used = None, 0
                self._rotations += 1
        start = self._clock()
        deadline = start + limit
        resumed, used = self._session, self._used
        # Any failed turn leaves no session behind, so the next ask starts fresh.
        self._session, self._used = None, 0
        try:
            result = self._once(stdin, resumed, deadline, limit)
            if result is None and resumed is not None:
                # A failed resume starts a fresh session once, inside the same deadline.
                used = 0
                result = self._once(stdin, None, deadline, limit)
            if result is None:
                raise _Bad("exit")
        finally:
            self._turns += 1
            self._seconds += max(0.0, self._clock() - start)
        text, needs_you, session_id = result
        if type(session_id) is str and re.fullmatch(SESSION_ID, session_id, re.ASCII) is not None:
            # A resumed turn keeps its count even if the id changes, so rotation still fires.
            self._session, self._used = session_id, used + len(packet) + len(text)
        return Reply(text, needs_you, self.counters())

    def _once(self, stdin, session, deadline, limit):
        argv = self._argv_for(session, self._tool_server)
        # After argv_for, so builder time counts; the bounds refuse a NaN, inf or backwards clock.
        remaining = deadline - self._clock()
        if not 0 < remaining <= limit:
            raise _Bad("timeout")
        try:
            result = self._runner(argv, stdin=stdin, timeout=remaining)
        except TimeoutError:
            raise _Bad("timeout") from None
        except Exception:  # noqa: BLE001 - a broken runner is a typed failure
            raise _Bad("runner_failed") from None
        if not self._clock() <= deadline:
            raise _Bad("timeout")  # nothing partial is returned
        if type(result) is not tuple or len(result) != 4:
            raise _Bad("runner_failed")
        code, out, session_id, needs_you = result
        if type(code) is not int or type(out) is not bytes:
            raise _Bad("runner_failed")
        if code != 0:
            return None
        if len(out) > MAX_OUTPUT_BYTES:
            raise _Bad("oversized")
        try:
            text = out.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _Bad("unreadable") from None
        if len(text) > MAX_REPLY_CHARS:
            raise _Bad("oversized")
        if not text.strip():
            raise _Bad("empty")
        # Output is text only: tool-call or control structures in it are never acted on.
        return text, needs_you is True, session_id
