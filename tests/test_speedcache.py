"""SPEEDCACHE: two turns of one conversation share a byte-identical prompt
prefix - the system prompt, the catalog and the stable context - and differ
only in the tail (the new conversation lines, then the per-turn keys, then the
request), so the provider's prompt cache can serve the prefix (LATENCYDIAG
section 4, fix 5).

Every transport is a fake: the "provider" below reports as cached the prefix
it saw on the previous call. No CLI, model, network or auth call happens here.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import jarvis.intent as intent
from jarvis.contracts import ActionScope, Task
from tests.test_codex_speed import RECORDED, STATE, Base, FakeProcess, codex

CATALOG = {"local_time": "2026-10-24T10:00+03:00",
           "entries": [{"capability": "chat.reply", "summary": "Answer in chat"},
                       {"capability": "reminder.create", "summary": "Set a reminder"}]}
CONVERSATION = [{"role": "user", "text": "merhaba"}, {"role": "assistant", "text": "Merhaba Salih"}]
TURNS = (
    {"request": "nasılsın", "local_time": "2026-10-24T10:00+03:00", "previous_request": "merhaba",
     "retrieved_memory": [{"label": "m1", "text": "çay sever"}], "conversation": CONVERSATION},
    {"request": "hava nasıl", "local_time": "2026-10-24T10:01+03:00", "previous_request": "nasılsın",
     "retrieved_memory": [{"label": "m2", "text": "Beşiktaş"}],
     "conversation": CONVERSATION + [{"role": "user", "text": "nasılsın"}, {"role": "assistant", "text": "İyiyim"}]},
)
VOLATILE = ("local_time", "previous_request", "retrieved_memory")


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))


def turn_task(turn: dict, reverse: bool = False) -> Task:
    context = {"checkpoint": "özet: selamlaştılar", "pinned": [{"text": "Salih"}],
               "catalog": dict(CATALOG, local_time=turn["local_time"]),
               **{key: turn[key] for key in ("conversation", *VOLATILE)}}
    if reverse:
        context = dict(reversed(list(context.items())))
    return Task(objective=turn["request"], capability=intent.INTENT_CAPABILITY,
                scope=ActionScope(action="understand", target=""), context=context)


class FakeCache:
    """A provider prompt cache: an input token is ~4 bytes, and the bytes a
    call shares with the previous call's prompt are read from the cache."""

    def __init__(self) -> None:
        self.previous = ""

    def usage(self, prompt: str) -> dict[str, int]:
        shared = len(os.path.commonprefix([self.previous, prompt]).encode("utf-8"))
        self.previous = prompt
        return {"input_tokens": len(prompt.encode("utf-8")) // 4, "cached_input_tokens": shared // 4,
                "cache_read_input_tokens": shared // 4, "output_tokens": 9}


def claude_turn(turn: dict, reverse: bool = False) -> tuple[str, str, Task, intent.ClaudeIntentAdapter]:
    adapter = intent.ClaudeIntentAdapter(allowed_roots=(Path.cwd(),), default_working_directory=Path.cwd())
    task = turn_task(turn, reverse)
    prompt, _ = adapter._build_prompt(task)
    command = adapter._command(prompt)
    return command[command.index("--system-prompt") + 1], prompt, task, adapter


class ClaudePrefixTests(unittest.TestCase):
    def test_two_turns_share_the_system_prompt_and_differ_only_in_the_tail(self) -> None:
        (system1, prompt1, _, _), (system2, prompt2, _, _) = claude_turn(TURNS[0]), claude_turn(TURNS[1])
        self.assertEqual(system1, system2)                     # catalog in, its local_time out
        self.assertIn(intent.CATALOG_SECTION, system1)
        self.assertNotIn("2026-10-24", system1)
        prefix = os.path.commonprefix([prompt1, prompt2])
        # Turn 1 is a prefix of turn 2 up to its last conversation line.
        self.assertTrue(prefix.endswith(dump(CONVERSATION)[:-1]), prefix[-80:])
        self.assertTrue(prefix.startswith(intent.ClaudeIntentAdapter.prompt_prefix))
        self.assertIn('"checkpoint":', prefix)
        self.assertIn('"pinned":', prefix)
        for prompt, turn in ((prompt1, TURNS[0]), (prompt2, TURNS[1])):
            tail = prompt[len(prefix):]
            for key in VOLATILE:
                self.assertNotIn(f'"{key}":', prefix)
                self.assertIn(f'"{key}":{dump(turn[key])}', tail)
            self.assertTrue(prompt.endswith('},"request":' + dump(turn["request"]) + "}"))

    def test_the_cache_order_changes_no_content_and_no_byte_count(self) -> None:
        for turn in TURNS:
            _, prompt, task, adapter = claude_turn(turn)
            payload, _ = adapter._sanitized_payload(task)
            sorted_text = dump(payload)                          # the encoding before SPEEDCACHE
            body = prompt[len(adapter.prompt_prefix):]
            self.assertEqual(json.loads(body), json.loads(sorted_text))
            self.assertEqual(len(body), len(sorted_text))
            self.assertEqual(sorted(body), sorted(sorted_text))

    def test_the_attested_bytes_are_the_prompt_and_do_not_depend_on_insertion_order(self) -> None:
        for turn in TURNS:
            system, prompt, task, adapter = claude_turn(turn)
            reversed_system, reversed_prompt, _, _ = claude_turn(turn, reverse=True)
            self.assertEqual((system, prompt), (reversed_system, reversed_prompt))
            self.assertEqual(adapter.canonical_outbound(task), prompt.encode("utf-8"))
            self.assertEqual(adapter.canonical_outbound(task), adapter.canonical_outbound(task))

    def test_a_key_that_is_not_a_string_falls_back_to_the_sorted_dump(self) -> None:
        payload = {"request": "x", "context": {"conversation": [], "n": {1: "a", 2: "b"}}}
        self.assertEqual(json.loads(intent.cache_ordered_json(payload)), json.loads(dump(payload)))
        self.assertEqual(intent.cache_ordered_json({1: "a"}), dump({1: "a"}))

    def test_the_reported_cache_read_grows_with_the_stable_prefix(self) -> None:
        cache = FakeCache()
        reported = []
        for turn in TURNS:
            system, prompt, _, _ = claude_turn(turn)
            reported.append(intent.reported_usage({"usage": cache.usage(system + prompt)}))
        first, second = reported
        self.assertEqual(first["cache_read_input_tokens"], 0)
        # The whole system prompt and the stable context come from the cache.
        self.assertGreater(second["cache_read_input_tokens"], len(claude_turn(TURNS[1])[0]) // 4)
        old = FakeCache()
        for turn in TURNS:
            system, _, task, adapter = claude_turn(turn)
            old_usage = old.usage(system + adapter.prompt_prefix + dump(adapter._sanitized_payload(task)[0]))
        self.assertGreater(second["cache_read_input_tokens"], old_usage["cache_read_input_tokens"])


class CachingProcess(FakeProcess):
    """A ``codex exec --json`` whose turn.completed usage comes from ``cache``."""

    def __init__(self, cache: FakeCache) -> None:
        self.cache = cache
        super().__init__(RECORDED[:-1])

    def _stream(self):
        yield from super()._stream()
        usage = self.cache.usage(self.stdin.getvalue())
        usage.pop("cache_read_input_tokens")
        yield json.dumps({"type": "turn.completed", "usage": dict(usage, reasoning_output_tokens=0)}) + "\n"


class CodexPrefixTests(Base):
    def run_turn(self, turn: dict, cache: FakeCache) -> tuple[str, dict]:
        process = CachingProcess(cache)
        self._task = turn_task(turn)
        adapter = codex.CodexIntentAdapter("system", "gpt-5.6-luna", "low", state=lambda: STATE)
        adapter, _ = self.run_codex(process, adapter)
        return process.stdin.getvalue(), adapter.reported[self._task.id]

    def test_two_turns_share_the_prefix_and_report_it_as_cached(self) -> None:
        cache = FakeCache()
        (prompt1, usage1), (prompt2, usage2) = (self.run_turn(turn, cache) for turn in TURNS)
        prefix = os.path.commonprefix([prompt1, prompt2])
        self.assertTrue(prefix.startswith("system" + intent.CATALOG_SECTION))
        self.assertTrue(prefix.endswith(dump(CONVERSATION)[:-1]), prefix[-80:])
        catalog_section = prompt1[:prompt1.index("\n\nJARVIS intent understanding request.")]
        self.assertNotIn("2026-10-24", catalog_section)
        self.assertEqual(json.loads(catalog_section[len("system" + intent.CATALOG_SECTION):]),
                         {"entries": CATALOG["entries"]})
        for prompt, turn in ((prompt1, TURNS[0]), (prompt2, TURNS[1])):
            for key in VOLATILE:
                self.assertIn(f'"{key}":{dump(turn[key])}', prompt[len(prefix):])
            self.assertTrue(prompt.endswith('},"request":' + dump(turn["request"]) + "}"))
        self.assertEqual(usage1["cached_input_tokens"], 0)
        self.assertGreater(usage2["cached_input_tokens"], len(prefix.encode("utf-8")) // 4 - 1)
        self.assertLessEqual(usage2["cached_input_tokens"], usage2["input_tokens"])

    def test_a_catalog_that_is_not_a_dict_stays_in_the_payload(self) -> None:
        task = Task(objective="hi", capability=intent.INTENT_CAPABILITY,
                    scope=ActionScope(action="understand", target=""), context={"catalog": "raw"})
        self._task = task
        process = FakeProcess(RECORDED)
        self.run_codex(process)
        prompt = process.stdin.getvalue()
        self.assertNotIn(intent.CATALOG_SECTION, prompt)
        self.assertTrue(prompt.endswith('{"context":{"catalog":"raw"},"request":"hi"}'))


if __name__ == "__main__":
    unittest.main()
