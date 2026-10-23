"""SPEEDCODEX: Codex streams its reply as it arrives and stops re-checking the
account and the model catalogue on every message (LATENCYDIAG section 4).

Every process is a fake and every clock is injected: no Codex CLI, model,
network or auth call happens here.
"""
from __future__ import annotations

import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jarvis.codex as codex
import jarvis.intent as intent
from jarvis.contracts import ActionScope, ErrorCode, OutcomeStatus, SessionState, Task

STATE = {"status": "available", "plan": "plus",
         "models": [{"id": "gpt-5.6-luna", "name": "L", "efforts": ["low"], "default_effort": "low"}]}
REPLY = json.dumps({"kind": "chat", "reply": "Merhaba Salih"})
USAGE = {"input_tokens": 120, "cached_input_tokens": 100, "output_tokens": 9, "reasoning_output_tokens": 0}
#: A `codex exec --json` stdout as the CLI writes it: a transient notice, a
#: reasoning item, the agent message, then the usage.
RECORDED = [json.dumps(event) + "\n" for event in (
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "error", "message": "Reconnecting... 1/5"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": REPLY}},
    {"type": "turn.completed", "usage": USAGE},
)]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class FakeProcess:
    """A CLI whose stdout is read line by line; ``ended`` flips when it is exhausted."""

    def __init__(self, lines, during=None, returncode: int = 0) -> None:
        self._lines, self._during = list(lines), during
        self.stdin = io.StringIO()
        self.stdin.close = lambda: None
        self.stderr = io.StringIO("warning: something\n")
        self.args, self.returncode = ["codex.exe"], returncode
        self.ended = self.killed = self.terminated = False
        self.stdout = self._stream()

    def _stream(self):
        for index, line in enumerate(self._lines):
            if self.killed or self.terminated:
                break
            yield line
            if self._during is not None:
                self._during(index)
        self.ended = True

    def wait(self) -> int:
        return self.returncode

    def poll(self):
        return self.returncode if self.ended else None

    def kill(self) -> None:
        self.killed, self.returncode = True, -9

    def terminate(self) -> None:
        self.terminated, self.returncode = True, 1


class ImmediateTimer:
    """``threading.Timer`` whose timeout fires as soon as it starts."""

    def __init__(self, _seconds, function) -> None:
        self.function = function

    def start(self) -> None:
        self.function()

    def cancel(self) -> None:
        pass


def task() -> Task:
    return Task(objective="hi", capability=intent.INTENT_CAPABILITY, scope=ActionScope(action="understand", target=""))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        codex.forget_codex_state()
        codex.forget_codex_catalog()
        self.addCleanup(codex.forget_codex_state)
        self.addCleanup(codex.forget_codex_catalog)
        self.clock = Clock()
        for patcher in (patch.object(codex, "time", SimpleNamespace(monotonic=self.clock.monotonic, time=lambda: 0)),
                        patch.object(codex, "codex_executable", return_value="codex.exe"),
                        patch.object(codex, "prohibited_env_present", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_codex(self, process: FakeProcess, adapter=None, threads=None):
        adapter = adapter or codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: STATE)
        with patch.object(codex.subprocess, "Popen", return_value=process), \
                patch.object(codex, "threading", threads or codex.threading):
            return adapter, adapter.complete(self.task, SessionState(objective="hi"))

    @property
    def task(self) -> Task:
        if not hasattr(self, "_task"):
            self._task = task()
        return self._task


class StreamingTests(Base):
    def test_first_text_reaches_the_listener_before_the_process_ends(self) -> None:
        process = FakeProcess(RECORDED)
        seen = []
        adapter = codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: STATE)
        adapter.stream_listener = lambda text: seen.append((text, process.ended))
        _, result = self.run_codex(process, adapter)
        self.assertEqual(OutcomeStatus.SUCCESS, result.outcome)
        self.assertEqual(("Merhaba Salih", False), seen[0])         # shown while the CLI still runs
        marks = adapter.timings[self.task.id]
        for key in ("started", "spawned", "first_text", "ended"):
            self.assertIn(key, marks)
        self.assertEqual(len(REPLY), marks["output_chars"])

    def test_the_timeout_still_kills_the_process(self) -> None:
        process = FakeProcess(RECORDED)
        threads = SimpleNamespace(Timer=ImmediateTimer, Thread=threading.Thread, Event=threading.Event, Lock=threading.Lock)
        _, result = self.run_codex(process, threads=threads)
        self.assertTrue(process.killed)
        self.assertEqual(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED, result.error.code)
        self.assertIn("timeout", result.summary)

    def test_a_cancel_mid_stream_terminates_the_process(self) -> None:
        adapter = codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: STATE)
        cancelled = []
        process = FakeProcess(RECORDED, during=lambda index: index == 1 and cancelled.append(adapter.cancel(self.task.id)))
        _, result = self.run_codex(process, adapter)
        self.assertEqual([True], cancelled)
        self.assertTrue(process.terminated)
        self.assertEqual(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual({}, adapter._processes)


class FinalResultTests(Base):
    def test_the_recorded_stream_gives_the_same_answer_with_or_without_a_listener(self) -> None:
        results = []
        for listener in (None, lambda _text: None):
            adapter = codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: STATE)
            adapter.stream_listener = listener
            _, result = self.run_codex(FakeProcess(RECORDED), adapter)
            self.assertEqual(USAGE, adapter.reported[self.task.id])
            results.append(result)
        expected = codex.build_model_answer(REPLY)
        for result in results:
            self.assertEqual(OutcomeStatus.SUCCESS, result.outcome)
            self.assertEqual("Codex answered.", result.summary)
            self.assertEqual([codex.MODEL_ANSWER_RESULT_KEY], list(result.data))
            answer = result.data[codex.MODEL_ANSWER_RESULT_KEY]     # produced_at is the wall clock
            self.assertEqual((expected.content, expected.character_count, expected.sha256, expected.truncated),
                             (answer.content, answer.character_count, answer.sha256, answer.truncated))

    def test_a_failed_turn_is_still_refused_with_its_reason(self) -> None:
        lines = [json.dumps({"type": "turn.failed", "error": {"message": "boom"}}) + "\n"]
        _, result = self.run_codex(FakeProcess(lines, returncode=1))
        self.assertEqual(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual(ErrorCode.CONNECTOR_FAILED, result.error.code)


class StateReuseTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.reads = 0
        self.answer = STATE

        def fake_state(**_):
            self.reads += 1
            return self.answer
        patcher = patch.object(codex, "codex_state", side_effect=fake_state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read_twice(self, state) -> int:
        self.reads, self.answer = 0, state
        codex.forget_codex_state()
        codex.reusable_codex_state()
        codex.reusable_codex_state()
        return self.reads

    def test_a_failed_limited_or_unknown_state_is_never_reused(self) -> None:
        for state in ({"status": "unavailable", "models": []},
                      {"status": "not_logged_in", "models": []},
                      {**STATE, "quota": {"has_credits": False, "limit_reached": True}},
                      {**STATE, "quota": {"has_credits": True, "limit_reached": True}},
                      {**STATE, "quota": {"has_credits": None}},
                      dict(STATE)):
            with self.subTest(state=state):
                self.assertEqual(2, self.read_twice(state))

    def test_a_state_without_credits_is_reused_for_the_full_window(self) -> None:
        self.assertEqual(1, self.read_twice({**STATE, "quota": {"has_credits": False}}))
        self.clock.now += codex.CODEX_STATE_REUSE_SECONDS - 1
        codex.reusable_codex_state()
        self.assertEqual(1, self.reads)
        self.clock.now += 2
        codex.reusable_codex_state()
        self.assertEqual(2, self.reads)

    def test_a_state_with_credits_is_re_read_on_every_call(self) -> None:
        self.assertEqual(0.0, codex.CODEX_CREDITS_STATE_REUSE_SECONDS)
        self.assertEqual(2, self.read_twice({**STATE, "quota": {"has_credits": True}}))
        codex.reusable_codex_state()                          # same instant
        self.assertEqual(3, self.reads)
        self.clock.now -= 1                                   # a clock behind the stamp
        codex.reusable_codex_state()
        self.assertEqual(4, self.reads)

    def test_a_slow_read_never_overwrites_a_newer_store(self) -> None:
        newer = {**STATE, "quota": {"has_credits": True}}
        stale = {**STATE, "quota": {"has_credits": False}}

        def slow_read(**_):
            self.reads += 1
            codex.remember_codex_state(newer)                 # the panel stores meanwhile
            return stale
        self.reads = 0
        codex.forget_codex_state()
        with patch.object(codex, "codex_state", side_effect=slow_read):
            codex.reusable_codex_state()
        self.assertIs(newer, codex._state_cache["state"])
        self.answer = stale
        codex.reusable_codex_state()                          # credits: re-read, not the stale False
        self.assertEqual(2, self.reads)

    def test_any_refusal_drops_the_reused_state_and_the_catalogue(self) -> None:
        self.read_twice({**STATE, "quota": {"has_credits": False}})
        codex._catalog_cache["codex.exe"] = (self.clock.now, [{"slug": "x"}])
        limited = {**STATE, "quota": {"has_credits": True, "limit_reached": True}}
        adapter = codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: limited)
        result = adapter.complete(self.task, SessionState(objective="hi"))
        self.assertEqual(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual({}, codex._state_cache)
        self.assertEqual({}, codex._catalog_cache)
        codex.reusable_codex_state()
        self.assertEqual(2, self.reads)                       # re-checked at once


class CatalogCacheTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.models = [{"slug": "gpt-5.6-luna"}]
        patcher = patch.object(codex, "_catalog", side_effect=lambda _executable: list(self.models))
        self.catalog = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_catalogue_is_read_once_per_ttl(self) -> None:
        codex._cached_catalog("codex.exe")
        codex._cached_catalog("codex.exe")
        self.assertEqual(1, self.catalog.call_count)
        self.clock.now += codex.CODEX_CATALOG_TTL_SECONDS + 1
        codex._cached_catalog("codex.exe")
        self.assertEqual(2, self.catalog.call_count)
        codex.forget_codex_catalog()
        codex._cached_catalog("codex.exe")
        self.assertEqual(3, self.catalog.call_count)

    def test_an_empty_read_is_never_kept(self) -> None:
        self.models = []
        codex._cached_catalog("codex.exe")
        codex._cached_catalog("codex.exe")
        self.assertEqual(2, self.catalog.call_count)

    def test_an_unknown_model_refusal_re_reads_the_catalogue(self) -> None:
        codex._cached_catalog("codex.exe")
        adapter = codex.CodexIntentAdapter("system", "gpt-9-unknown", "low", state=lambda: STATE)
        result = adapter.complete(self.task, SessionState(objective="hi"))
        self.assertEqual(ErrorCode.CONNECTOR_UNAVAILABLE, result.error.code)
        codex._cached_catalog("codex.exe")
        self.assertEqual(2, self.catalog.call_count)

    def test_the_chat_path_takes_the_cached_catalogue_and_the_account_fresh(self) -> None:
        answers = {"account/read": {"account": {"type": "chatgpt", "planType": "plus"}},
                   "account/rateLimits/read": {"rateLimits": {"credits": {"hasCredits": False}}}}
        with patch.object(codex, "_app_server", return_value=answers) as app_server:
            codex.codex_state(cached_catalog=True)
            codex.codex_state(cached_catalog=True)
        self.assertEqual(2, app_server.call_count)
        self.assertEqual(1, self.catalog.call_count)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
