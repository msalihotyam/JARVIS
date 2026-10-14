"""SECCAPWIRE: memory_extract_model is settable from the CLI.

The CLI refuses what the settings layer would only silently clear: a value
is empty (no override) or a Claude model id in the shared grammar.
"""

from __future__ import annotations

import unittest

from jarvis import cli
from tests.test_config import EXIT_OK, ConfigTestCase


class MemoryExtractModelCliTests(ConfigTestCase):
    def stored(self) -> str:
        return self.load().settings.memory_extract_model

    def test_the_key_is_settable(self) -> None:
        self.assertIn("memory_extract_model", cli.CONFIG_SETTABLE_KEYS)

    def test_set_then_get(self) -> None:
        code, _ = self.payload(["config", "set", "memory_extract_model", "claude-haiku-5-5"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.stored(), "claude-haiku-5-5")

    def test_empty_clears_and_reset_restores_the_default(self) -> None:
        self.payload(["config", "set", "memory_extract_model", "claude-haiku-5-5"])
        code, _ = self.payload(["config", "set", "memory_extract_model", ""])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.stored(), "")
        self.payload(["config", "set", "memory_extract_model", "claude-haiku-5-5"])
        code, _ = self.payload(["config", "reset", "memory_extract_model"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.stored(), "")

    def test_malformed_values_are_refused_and_nothing_is_written(self) -> None:
        self.payload(["config", "set", "memory_extract_model", "claude-haiku-5-5"])
        before = self.path.read_bytes()
        for bad in ("claude haiku", ".dot-first", "x" * 65, "a/b", "model[UPPER]", "m\n"):
            with self.subTest(bad=bad):
                code, payload = self.payload(["config", "set", "memory_extract_model", bad])
                self.assertNotEqual(code, EXIT_OK)
                self.assertEqual(payload["error"]["code"], "config_invalid")
                self.assertEqual(self.path.read_bytes(), before, "a refused value was written")
        self.assertEqual(self.stored(), "claude-haiku-5-5")

    def test_unknown_keys_stay_refused(self) -> None:
        code, _ = self.payload(["config", "set", "memory_extract_modelx", "claude-haiku-5-5"])
        self.assertNotEqual(code, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
