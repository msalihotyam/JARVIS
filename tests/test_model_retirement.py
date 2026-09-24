"""Provider-driven retirement and migration (jarvis.model_retirement), fake catalogs only."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from jarvis.model_retirement import (
    adopt_claude_lane,
    claude_lane,
    claude_migration,
    codex_migration,
    codex_retirement,
)

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def claude_catalog(opus: str = "claude-opus-5-5[1m]", sonnet: str = "claude-sonnet-5") -> list[dict]:
    """The shape the Claude Code CLI's initialize answer takes (ids, resolved models, efforts)."""
    return [{"id": "default", "resolved": opus, "efforts": EFFORTS},
            {"id": "opus[1m]", "resolved": opus, "efforts": EFFORTS},
            {"id": "claude-fable-5-1[1m]", "resolved": "claude-fable-5-1", "efforts": EFFORTS},
            {"id": "sonnet", "resolved": sonnet, "efforts": EFFORTS},
            {"id": "haiku", "resolved": "claude-haiku-4-5-20251001", "efforts": []}]


class ClaudeLaneTests(unittest.TestCase):
    def test_the_same_lane_resolving_to_a_new_model_moves_the_pin_and_nothing_else_does(self) -> None:
        later = claude_catalog(opus="claude-opus-6[1m]")
        self.assertEqual(("claude-opus-6[1m]", "high"), claude_migration(later, "opus[1m]", "claude-opus-5-5[1m]", "high"))
        self.assertIsNone(claude_migration(claude_catalog(), "opus[1m]", "claude-opus-5-5[1m]", "high"))     # same model
        self.assertIsNone(claude_migration(later, "", "claude-opus-5-5[1m]", "high"))                        # no lane
        self.assertIsNone(claude_migration(later, "opus[1m]", "", ""))                                       # CLI default
        self.assertIsNone(claude_migration([], "opus[1m]", "claude-opus-5-5[1m]", "high"))                   # nothing listed
        self.assertIsNone(claude_migration(later, "default", "claude-opus-5-5[1m]", "high"))                 # not a lane

    def test_effort_is_kept_when_the_new_model_supports_it_and_otherwise_falls_to_default(self) -> None:
        later = claude_catalog(opus="claude-opus-6[1m]")
        later[1]["efforts"] = ["low", "medium", "high"]
        self.assertEqual(("claude-opus-6[1m]", "medium"), claude_migration(later, "opus[1m]", "claude-opus-5-5[1m]", "medium"))
        self.assertEqual(("claude-opus-6[1m]", ""), claude_migration(later, "opus[1m]", "claude-opus-5-5[1m]", "max"))
        self.assertEqual(("claude-opus-6[1m]", ""), claude_migration(later, "opus[1m]", "claude-opus-5-5[1m]", ""))

    def test_a_lane_never_crosses_family_or_context_variant(self) -> None:
        # The alias answering with another family (as "haiku" was once answered by Sonnet) is not a successor.
        crossed = claude_catalog(opus="claude-sonnet-6[1m]")
        self.assertIsNone(claude_migration(crossed, "opus[1m]", "claude-opus-5-5[1m]", "high"))
        # [1m] is the long context: a resolution that drops or adds it never replaces the pin.
        dropped = claude_catalog(opus="claude-opus-6")
        self.assertIsNone(claude_migration(dropped, "opus[1m]", "claude-opus-5-5[1m]", "high"))
        added = claude_catalog(sonnet="claude-sonnet-6[1m]")
        self.assertIsNone(claude_migration(added, "sonnet", "claude-sonnet-5", "high"))
        self.assertEqual(("claude-sonnet-6", "high"), claude_migration(claude_catalog(sonnet="claude-sonnet-6"), "sonnet", "claude-sonnet-5", "high"))
        # A lane is only an alias the discovery listed; an unknown or odd resolution keeps the choice.
        self.assertIsNone(claude_migration(claude_catalog(), "opus", "claude-opus-5-5[1m]", "high"))
        odd = claude_catalog(opus="opus-latest")
        self.assertIsNone(claude_migration(odd, "opus[1m]", "claude-opus-5-5[1m]", "high"))

    def test_a_lane_is_recorded_only_for_an_alias_that_resolves_to_exactly_the_choice(self) -> None:
        models = claude_catalog()
        self.assertEqual("opus[1m]", claude_lane(models, "opus[1m]", "claude-opus-5-5[1m]"))
        self.assertEqual("", claude_lane(models, "opus[1m]", "claude-opus-5[1m]"))                   # not what it resolves to
        self.assertEqual("", claude_lane(models, "claude-fable-5-1[1m]", "claude-fable-5-1[1m]"))    # a full id is a pin, not a lane
        self.assertEqual("", claude_lane(models, "default", "claude-opus-5-5[1m]"))
        self.assertEqual("", claude_lane(models, "nope", "claude-opus-5-5[1m]"))

    def test_a_choice_saved_before_lanes_adopts_one_only_when_unambiguous(self) -> None:
        models = claude_catalog()
        self.assertEqual("opus[1m]", adopt_claude_lane(models, "claude-opus-5-5[1m]"))      # default is not a lane
        self.assertEqual("haiku", adopt_claude_lane(models, "claude-haiku-4-5-20251001"))
        self.assertEqual("", adopt_claude_lane(models, "claude-opus-5[1m]"))                # an obsolete pin: nothing matches it
        self.assertEqual("", adopt_claude_lane(models, "claude-fable-5-1[1m]"))             # no alias resolves to it
        self.assertEqual("", adopt_claude_lane(models, ""))
        twice = models + [{"id": "opus", "resolved": "claude-opus-5-5[1m]", "efforts": EFFORTS}]
        self.assertEqual("", adopt_claude_lane(twice, "claude-opus-5-5[1m]"))              # two aliases: ambiguous


NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def offered(*ids: str, specialty: dict | None = None) -> list[dict]:
    return [{"id": i, "efforts": ["low", "medium", "high"], "default_effort": "medium", "specialty": (specialty or {}).get(i, "")}
            for i in ids]


class CodexRetirementTests(unittest.TestCase):
    def test_the_real_discovery_keeps_every_lane_gpt_6_does_not_retire_5_6(self) -> None:
        # 2026-09-24 live: gpt-5.5 (not code-mode safe, so never offered) declares gpt-5.6-sol.
        models = offered("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-daybreak-blue-latest",
                         specialty={"gpt-daybreak-blue-latest": "cyber"})
        upgrades = {"gpt-5.5": {"model": "gpt-5.6-sol", "retirement_at": "2026-10-14T19:00:00Z", "specialty": ""}}
        kept, retired = codex_retirement(models, upgrades, NOW)
        self.assertEqual(["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-daybreak-blue-latest"], [m["id"] for m in kept])
        self.assertEqual({"gpt-5.5": "gpt-5.6-sol"}, retired)
        self.assertEqual(("gpt-5.6-sol", "medium"), codex_migration("gpt-5.5", "medium", kept, retired))
        self.assertIsNone(codex_migration("gpt-5.6-sol", "medium", kept, retired))

    def test_a_declared_successor_that_is_offered_retires_the_old_model(self) -> None:
        models = offered("gpt-6-sol", "gpt-5.6-sol")
        kept, retired = codex_retirement(models, {"gpt-5.6-sol": {"model": "gpt-6-sol", "retirement_at": "", "specialty": ""}}, NOW)
        self.assertEqual((["gpt-6-sol"], {"gpt-5.6-sol": "gpt-6-sol"}), ([m["id"] for m in kept], retired))
        self.assertEqual(("gpt-6-sol", "high"), codex_migration("gpt-5.6-sol", "high", kept, retired))

    def test_no_successor_is_invented_hidden_unsafe_or_absent_targets_never_count(self) -> None:
        models = offered("gpt-5.6-sol", "gpt-5.6-luna")
        for target in ("gpt-6-sol", "gpt-reserve", "", "gpt-5.6-sol"):      # absent / hidden (never offered) / none / itself
            with self.subTest(target=target):
                kept, retired = codex_retirement(models, {"gpt-5.6-sol": {"model": target, "retirement_at": "", "specialty": ""}}, NOW)
                self.assertEqual(["gpt-5.6-sol", "gpt-5.6-luna"], [m["id"] for m in kept])
                self.assertEqual({}, retired)
                self.assertIsNone(codex_migration("gpt-5.6-sol", "low", kept, retired))
        # A model that vanished with no declaration keeps the stored choice: nothing is guessed.
        self.assertIsNone(codex_migration("gpt-5.4", "low", models, {}))

    def test_a_specialised_model_never_replaces_a_general_one(self) -> None:
        models = offered("gpt-5.6-sol", "gpt-daybreak-blue-latest", "gpt-daybreak-green", specialty={"gpt-daybreak-blue-latest": "cyber",
                                                                                                        "gpt-daybreak-green": "cyber"})
        upgrades = {"gpt-5.6-sol": {"model": "gpt-daybreak-blue-latest", "retirement_at": "", "specialty": ""},
                    "gpt-daybreak-green": {"model": "gpt-daybreak-blue-latest", "retirement_at": "", "specialty": "cyber"}}
        kept, retired = codex_retirement(models, upgrades, NOW)
        self.assertEqual({"gpt-daybreak-green": "gpt-daybreak-blue-latest"}, retired)       # cyber -> cyber is allowed
        self.assertIn("gpt-5.6-sol", [m["id"] for m in kept])
        self.assertIsNone(codex_migration("gpt-5.6-sol", "low", kept, retired))

    def test_a_passed_retirement_time_hides_a_model_but_moves_nothing_without_a_successor(self) -> None:
        models = offered("gpt-5.6-sol", "gpt-5.4")
        past = {"gpt-5.4": {"model": "", "retirement_at": "2026-09-01T00:00:00Z", "specialty": ""}}
        kept, retired = codex_retirement(models, past, NOW)
        self.assertEqual((["gpt-5.6-sol"], {}), ([m["id"] for m in kept], retired))
        self.assertIsNone(codex_migration("gpt-5.4", "low", kept, retired))
        future = {"gpt-5.4": {"model": "", "retirement_at": "2026-12-01T00:00:00Z", "specialty": ""}}
        self.assertEqual(["gpt-5.6-sol", "gpt-5.4"], [m["id"] for m in codex_retirement(models, future, NOW)[0]])
        # A successor whose own retirement has passed is no successor.
        dead_end = {"gpt-5.4": {"model": "gpt-5.6-sol", "retirement_at": "", "specialty": ""},
                    "gpt-5.6-sol": {"model": "", "retirement_at": "2026-09-01T00:00:00Z", "specialty": ""}}
        self.assertEqual({}, codex_retirement(models, dead_end, NOW)[1])

    def test_declared_chains_are_followed_to_an_offered_model_and_cycles_stop(self) -> None:
        models = offered("gpt-7", "gpt-5.6-sol")
        chain = {"gpt-5.5": {"model": "gpt-6", "retirement_at": "", "specialty": ""},
                 "gpt-6": {"model": "gpt-7", "retirement_at": "", "specialty": ""}}
        self.assertEqual({"gpt-5.5": "gpt-7", "gpt-6": "gpt-7"}, codex_retirement(models, chain, NOW)[1])
        loop = {"a": {"model": "b", "retirement_at": "", "specialty": ""}, "b": {"model": "a", "retirement_at": "", "specialty": ""}}
        self.assertEqual({}, codex_retirement(offered("gpt-5.6-sol"), loop, NOW)[1])

    def test_effort_is_kept_when_supported_otherwise_the_successors_default(self) -> None:
        kept = [{"id": "gpt-6-sol", "efforts": ["low", "medium"], "default_effort": "low", "specialty": ""}]
        retired = {"gpt-5.6-sol": "gpt-6-sol"}
        self.assertEqual(("gpt-6-sol", "medium"), codex_migration("gpt-5.6-sol", "medium", kept, retired))
        self.assertEqual(("gpt-6-sol", "low"), codex_migration("gpt-5.6-sol", "ultra", kept, retired))
        kept[0]["default_effort"] = "bogus"
        self.assertEqual(("gpt-6-sol", ""), codex_migration("gpt-5.6-sol", "ultra", kept, retired))

    def test_malformed_or_unknown_metadata_keeps_models_visible(self) -> None:
        models = offered("gpt-5.6-sol", "gpt-5.6-luna")
        for weird in ({"gpt-5.6-sol": {"model": None, "retirement_at": "not a date", "specialty": 7}},
                      {"gpt-5.6-sol": {"retirement_at": "2026-09-01"}},                 # no zone: not trusted
                      {"gpt-5.6-sol": {"model": 42}}, {"gpt-5.6-sol": {}}, {}):
            with self.subTest(weird=weird):
                kept, retired = codex_retirement(models, weird, NOW)
                self.assertEqual((["gpt-5.6-sol", "gpt-5.6-luna"], {}), ([m["id"] for m in kept], retired))


class CodexStateMetadataTests(unittest.TestCase):
    """codex_state exposes Codex's own declarations; the safe-model filter is unchanged."""

    def test_declarations_priority_and_specialty_are_read_defensively(self) -> None:
        import jarvis.codex as codex
        catalog = [
            {"slug": "gpt-5.6-luna", "visibility": "list", "tool_mode": "code_mode_only", "priority": 8, "supported_reasoning_levels": [{"effort": "low"}]},
            {"slug": "gpt-6-astra", "visibility": "list", "tool_mode": "code_mode_only", "priority": 1, "supported_reasoning_levels": [{"effort": "low"}],
             "default_reasoning_level": "low"},
            {"slug": "gpt-daybreak-blue-latest", "visibility": "list", "tool_mode": "code_mode_only", "priority": 10, "model_specialty": "cyber"},
            {"slug": "gpt-5.5", "visibility": "list", "tool_mode": None, "priority": 12,
             "upgrade": {"model": "gpt-5.6-sol", "retirement_at": "2026-10-14T19:00:00Z", "migration_markdown": "x"}},
            {"slug": "gpt-reserve", "visibility": "hide", "tool_mode": "code_mode_only", "priority": "3", "upgrade": {"model": "bad slug!"}},
            {"slug": "gpt-new", "visibility": "list", "tool_mode": "code_mode_only", "priority": None, "brand_new_field": {"x": 1}},
        ]
        answers = {"account/read": {"account": {"type": "chatgpt", "planType": "plus"}}, "account/rateLimits/read": None}
        with patch.object(codex, "codex_executable", return_value="codex.exe"), patch.dict(os.environ, {}, clear=False), \
                patch.object(codex, "prohibited_env_present", return_value=False), \
                patch.object(codex, "_app_server", return_value=answers), patch.object(codex, "_catalog", return_value=catalog):
            state = codex.codex_state()
        self.assertEqual(["gpt-6-astra", "gpt-5.6-luna", "gpt-daybreak-blue-latest", "gpt-new"], [m["id"] for m in state["models"]])
        self.assertEqual(["", "", "cyber", ""], [m["specialty"] for m in state["models"]])
        self.assertEqual({"gpt-5.5": {"model": "gpt-5.6-sol", "retirement_at": "2026-10-14T19:00:00Z", "specialty": ""}}, state["upgrades"])


if __name__ == "__main__":
    unittest.main()
