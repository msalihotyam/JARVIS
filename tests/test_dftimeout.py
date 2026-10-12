"""DFTIMEOUT (Salih 2026-10-12, "180 sn yap"): the reply timeout is 180 s.

A ~50,000-character reply needs longer than the old 90 s to stream. The real
adapter's own timer bounds the call: past 90 s and under 180 s the reply
completes as an ordinary answer; past 180 s it is cut and what the person saw is
kept with DFREPLY's "timed out" notice.

A fake clock drives the adapter's timer: no real sleeping, no process, no model,
network or connector call, and the live database is never opened.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - only subprocess.TimeoutExpired is used
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import jarvis.intent as intent
from jarvis.connectors.claude_code import TIMEOUT_MESSAGE
from jarvis.contracts import ErrorCode, OutcomeStatus, StructuredError, ToolResult
from jarvis.conversation import cut_reply
from jarvis.intent import INTENT_TIMEOUT_SECONDS, ClaudeIntentAdapter
from jarvis.ui.i18n import render
from jarvis.ui.present import CUT_TEXT
from tests.test_dfreply import chat, lines
from tests.test_intent import ScriptedModel, stream_line
from tests.test_ui import UiTestCase

REPLY = lines(50_000)


class FakeClock:
    """Seconds that pass only when a test says so; due timers fire on the advance."""

    def __init__(self) -> None:
        self.now = 0.0
        self.timers: list[FakeTimer] = []

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for timer in list(self.timers):
            if timer.live and self.now >= timer.due:
                timer.live = False
                timer.function()

    def timer(self, interval, function):
        return FakeTimer(self, interval, function)


class FakeTimer:
    """``threading.Timer`` on the fake clock."""

    def __init__(self, clock: FakeClock, interval: float, function) -> None:
        self.clock, self.due, self.function, self.live = clock, clock.now + interval, function, False

    def start(self) -> None:
        self.live = True
        self.clock.timers.append(self)

    def cancel(self) -> None:
        self.live = False


class ClockedProcess:
    """The CLI streaming ``REPLY`` over ``seconds`` of fake time; ``kill`` ends its stdout."""

    def __init__(self, clock: FakeClock, seconds: float, reply: str = REPLY, steps: int = 100) -> None:
        answer = chat(reply)
        size = -(-len(answer) // steps)
        self.chunks = [stream_line(answer[i:i + size]) for i in range(0, len(answer), size)]
        self.chunks.append(json.dumps({"type": "result", "result": answer, "usage": {}}) + "\n")
        self.clock, self.step = clock, seconds / len(self.chunks)
        self.stdout = self._lines()
        self.stderr = types.SimpleNamespace(read=lambda: "")
        self.args = ["claude"]
        self.returncode = 0
        self.killed = False

    def _lines(self):
        for line in self.chunks:
            self.clock.advance(self.step)
            if self.killed:
                return
            yield line

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.killed = True


def collect(seconds: float, listener) -> tuple[str, str]:
    """The real adapter's streaming read of a process lasting ``seconds`` of fake time."""
    clock = FakeClock()
    adapter = ClaudeIntentAdapter(allowed_roots=(Path.cwd(),), default_working_directory=Path.cwd())
    adapter.stream_listener = listener
    fake = types.SimpleNamespace(Timer=clock.timer, Event=threading.Event, Thread=threading.Thread)
    with mock.patch.object(intent, "threading", fake):
        return adapter._collect_output(ClockedProcess(clock, seconds))


class TimeoutTests(unittest.TestCase):
    def test_the_reply_timeout_is_180_seconds(self) -> None:
        self.assertEqual(180, INTENT_TIMEOUT_SECONDS)
        adapter = ClaudeIntentAdapter(allowed_roots=(Path.cwd(),), default_working_directory=Path.cwd())
        self.assertEqual(180, adapter.timeout_seconds)
        self.assertEqual(180, adapter.manifest.timeout_seconds)

    def test_a_reply_past_90_and_under_180_seconds_completes(self) -> None:
        previews: list[str] = []
        stdout, _ = collect(170, previews.append)
        self.assertIn('"type": "result"', stdout.splitlines()[-1])
        self.assertEqual(REPLY, previews[-1])

    def test_a_reply_past_180_seconds_times_out_after_showing_part(self) -> None:
        previews: list[str] = []
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            collect(250, previews.append)
        self.assertEqual(180, caught.exception.timeout)
        self.assertTrue(previews)
        self.assertTrue(REPLY.startswith(previews[-1]))
        self.assertLess(len(previews[-1]), len(REPLY))


class ClockedModel(ScriptedModel):
    """An understanding model that streams through the real adapter's read on the fake clock,
    answering as the connector does: a timeout is ``TIMEOUT_MESSAGE``."""

    stream_listener = None

    def __init__(self, *answers, seconds: float, **kwargs) -> None:
        super().__init__(*answers, **kwargs)
        self.seconds = seconds

    def complete(self, task, session):
        try:
            collect(self.seconds, self.stream_listener or (lambda text: None))
        except subprocess.TimeoutExpired:
            self.calls.append(task)
            error = StructuredError(ErrorCode.CONNECTOR_FAILED, TIMEOUT_MESSAGE)
            return ToolResult(outcome=OutcomeStatus.ERROR, summary=error.message, error=error)
        return super().complete(task, session)


class WindowTests(UiTestCase):
    def turns(self, bridge) -> list[tuple[str, str]]:
        state = bridge._conversation_db().load_conversation_state(bridge.conversationId)
        return [(t.kind, t.text) for t in state.turns]

    def test_a_long_reply_under_180_seconds_is_an_ordinary_reply(self) -> None:
        self.model = ClockedModel(seconds=170)
        bridge = self.bridge(chat(REPLY))
        self.assertTrue(bridge.submitText("Uzun anlat."))
        self.assertEqual(["user", "jarvis"], self.kinds(bridge))
        self.assertEqual(REPLY, bridge.feed[-1]["text"])
        self.assertFalse(bridge.feed[-1].get("cut"))
        self.assertEqual(("reply", REPLY), self.turns(bridge)[-1])

    def test_a_reply_past_180_seconds_is_kept_with_the_timed_out_notice(self) -> None:
        self.model = ClockedModel(seconds=250)
        bridge = self.bridge()
        self.assertTrue(bridge.submitText("Uzun anlat."))
        self.assertEqual(["user", "jarvis", "notice"], self.kinds(bridge))
        seen = bridge.feed[1]["text"]
        self.assertTrue(seen and REPLY.startswith(seen) and len(seen) < len(REPLY))
        self.assertEqual("timed_out", bridge.feed[1]["cut"])
        self.assertEqual(render(("Present", CUT_TEXT["timed_out"], ())), bridge.feed[2]["text"])
        self.assertEqual(("reply", cut_reply(seen, "timed_out")), self.turns(bridge)[-1])


if __name__ == "__main__":
    unittest.main()
