"""V2.1 extraction skip policy and the memory_extract_model setting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.memory_capture import extraction_skip_reason
from jarvis.settings import (
    CONFIG_KEYS,
    PRE_MEMORY_EXTRACT_CONFIG_KEYS,
    ConfigStatus,
    ConfigStore,
    Settings,
)


def user(message: str) -> SimpleNamespace:
    return SimpleNamespace(role="user", kind="user", text=message, message=message)


def reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(role="assistant", kind="reply", text=text, message="")


def session(*sizes: int) -> list[SimpleNamespace]:
    turns: list[SimpleNamespace] = []
    for size in sizes:
        turns += [user("x" * size), reply("y" * 500)]
    return turns


def skip(turns, *, temporary=False, quota=None):
    return extraction_skip_reason(turns, temporary=temporary, quota_remaining_pct=quota)


class SkipPolicyTest(unittest.TestCase):
    def test_a_long_enough_session_is_extracted(self) -> None:
        self.assertIsNone(skip(session(100, 100), quota=50.0))

    def test_temporary(self) -> None:
        self.assertEqual(skip(session(500, 500), temporary=True), "temporary")

    def test_temporary_wins_over_every_other_code(self) -> None:
        self.assertEqual(skip([], temporary=True, quota=0.0), "temporary")

    def test_empty(self) -> None:
        self.assertEqual(skip([]), "empty")
        self.assertEqual(skip([reply("y" * 1000)]), "empty")

    def test_too_short_by_turns(self) -> None:
        self.assertEqual(skip(session(1000)), "too_short")

    def test_too_short_by_chars(self) -> None:
        self.assertEqual(skip(session(99, 100)), "too_short")

    def test_assistant_text_does_not_count_toward_chars(self) -> None:
        self.assertEqual(skip(session(10, 10)), "too_short")

    def test_only_the_persons_own_words_count(self) -> None:
        padded = [SimpleNamespace(role="user", kind="user", text="z" * 500, message="hi")] * 2
        self.assertEqual(skip(padded), "too_short")
        self.assertEqual(skip([user(" " * 300), user(" " * 300)]), "too_short")

    def test_quota_floor(self) -> None:
        self.assertEqual(skip(session(100, 100), quota=9.99), "quota_floor")
        self.assertEqual(skip(session(100, 100), quota=0), "quota_floor")

    def test_none_quota_never_skips(self) -> None:
        self.assertIsNone(skip(session(100, 100), quota=None))

    def test_boundaries_do_not_skip(self) -> None:
        # Exactly 2 user turns, exactly 200 user chars, exactly 10 %.
        self.assertIsNone(skip(session(100, 100), quota=10))
        self.assertIsNone(skip(session(100, 100), quota=10.0))

    def test_one_below_each_boundary_skips(self) -> None:
        self.assertEqual(skip(session(200)), "too_short")
        self.assertEqual(skip(session(100, 99)), "too_short")
        self.assertEqual(skip(session(100, 100), quota=9.999), "quota_floor")

    def test_thresholds_are_parameters(self) -> None:
        turns = session(5)
        self.assertIsNone(
            extraction_skip_reason(
                turns,
                temporary=False,
                quota_remaining_pct=3,
                min_user_turns=1,
                min_user_chars=5,
                quota_floor_pct=3,
            )
        )

    def test_garbage_inputs_skip_fail_closed(self) -> None:
        good = session(100, 100)
        for temporary in (None, 1, "no", 0):
            with self.subTest(temporary=temporary):
                self.assertEqual(skip(good, temporary=temporary), "temporary")
        for quota in (float("nan"), float("inf"), "50", True, False, [50], object()):
            with self.subTest(quota=quota):
                self.assertEqual(skip(good, quota=quota), "quota_floor")
        for turns in (None, 7, [SimpleNamespace(role="user")], [object()]):
            with self.subTest(turns=turns):
                self.assertEqual(skip(turns), "empty")
        for name in ("min_user_turns", "min_user_chars"):
            for bad in (None, "2", float("nan")):
                with self.subTest(**{name: bad}):
                    self.assertEqual(
                        extraction_skip_reason(
                            good, temporary=False, quota_remaining_pct=50, **{name: bad}
                        ),
                        "too_short",
                    )
        for bad in (None, "10", float("nan")):
            with self.subTest(quota_floor_pct=bad):
                self.assertEqual(
                    extraction_skip_reason(
                        good, temporary=False, quota_remaining_pct=50, quota_floor_pct=bad
                    ),
                    "quota_floor",
                )
        odd = [SimpleNamespace(role="user", message=None)] * 2 + good
        self.assertIsNone(skip(odd))  # a non-text message counts as 0 chars
        self.assertEqual(
            skip([SimpleNamespace(role="user", message=b"x" * 500)] * 2), "too_short"
        )

    def test_a_generator_is_read_once(self) -> None:
        self.assertIsNone(skip(iter(session(100, 100))))


class ExtractModelSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "config.json"

    def settings(self, **fields) -> Settings:
        return Settings(memory_database=self.root / "memory.db", **fields)

    def test_default_is_the_current_model(self) -> None:
        self.assertEqual(Settings().memory_extract_model, "")

    def test_round_trip(self) -> None:
        for model in ("", "claude-haiku-5-5", "opus[1m]"):
            with self.subTest(model=model):
                ConfigStore(self.path).write(self.settings(memory_extract_model=model))
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(set(stored), set(CONFIG_KEYS))
                self.assertEqual(stored["memory_extract_model"], model)
                result = ConfigStore(self.path).load()
                self.assertIs(result.status, ConfigStatus.VALID)
                self.assertEqual(result.settings.memory_extract_model, model)

    def test_a_pre_memory_extract_document_loads_with_the_current_model(self) -> None:
        ConfigStore(self.path).write(self.settings(memory_extract_model="sonnet"))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        del document["memory_extract_model"]
        self.assertEqual(set(document), set(PRE_MEMORY_EXTRACT_CONFIG_KEYS))
        self.path.write_text(json.dumps(document), encoding="utf-8")
        result = ConfigStore(self.path).load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertEqual(result.settings.memory_extract_model, "")

    def test_unknown_types_and_bad_names_fall_back_to_empty(self) -> None:
        for value in (None, 7, True, ["sonnet"], {"m": 1}, "--model", "a b", "x" * 200):
            with self.subTest(value=value):
                self.assertEqual(self.settings(memory_extract_model=value).memory_extract_model, "")
                ConfigStore(self.path).write(self.settings())
                document = json.loads(self.path.read_text(encoding="utf-8"))
                document["memory_extract_model"] = value
                self.path.write_text(json.dumps(document), encoding="utf-8")
                result = ConfigStore(self.path).load()
                self.assertIs(result.status, ConfigStatus.VALID)
                self.assertEqual(result.settings.memory_extract_model, "")


if __name__ == "__main__":
    unittest.main()
