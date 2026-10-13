"""V2.2 S1 (SKFLAG): the ``skills_enabled`` switch.

One flag covers skill matching and use, save offers, and experience capture
and injection (PLAN_V22 D7). It is off by default, off in
``fail_closed_settings`` (invariant S4), and it is not authority: turning it
on changes no other setting.

Rollback: every older build refuses a document with a key it does not know,
so the key is written only when it is on. A document with skills off keeps
exactly the shape the previous build wrote and accepts.

No test here reads or writes the operator's real configuration or database:
every store lives in a temporary directory.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from jarvis.settings import (
    CONFIG_KEYS,
    LEGACY_CONFIG_KEYS,
    PRE_DESKTOP_CONFIG_KEYS,
    PRE_GITHUB_CONFIG_KEYS,
    SKILLS_CONFIG_KEYS,
    SKILLS_ENABLED_KEY,
    ConfigStatus,
    ConfigStore,
    Settings,
    _document_from_settings,
    fail_closed_settings,
)
from tests.helpers import safe_cleanup


class SkillsFlagTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.root = Path(temp.name)
        self.path = self.root / "config.json"
        self.store = ConfigStore(self.path)
        self.database = self.root / "jarvis.db"

    def settings(self, **fields: object) -> Settings:
        return Settings(memory_database=self.database, **fields)

    def document(self, **fields: object) -> dict:
        return _document_from_settings(self.settings(**fields))

    def write_raw(self, document: dict) -> None:
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class DefaultsTests(SkillsFlagTestCase):
    def test_default_is_off(self) -> None:
        self.assertIs(Settings().skills_enabled, False)
        self.assertIs(self.settings().skills_enabled, False)

    def test_fail_closed_settings_is_off(self) -> None:
        self.assertIs(fail_closed_settings().skills_enabled, False)

    def test_an_absent_store_runs_with_skills_off(self) -> None:
        result = self.store.load()
        self.assertIs(result.status, ConfigStatus.ABSENT)
        self.assertIs(result.settings.skills_enabled, False)

    def test_a_refused_document_runs_with_skills_off(self) -> None:
        # Even a document that asked for skills is refused whole when any
        # other part of it is wrong; the fallback is the fail-closed posture.
        self.write_raw(self.document(skills_enabled=True) | {"extra": 1})
        result = self.store.load()
        self.assertIs(result.status, ConfigStatus.INVALID)
        self.assertIs(result.settings.skills_enabled, False)
        self.assertEqual(result.settings, fail_closed_settings())

    def test_the_flag_is_not_authority(self) -> None:
        # Turning skills on changes nothing else: no permission mode, no
        # paid API, no execution switch, no grant-bearing configuration.
        on = self.settings(skills_enabled=True)
        off = self.settings()
        for field in dataclasses.fields(Settings):
            if field.name == "skills_enabled":
                continue
            with self.subTest(field=field.name):
                self.assertEqual(getattr(on, field.name), getattr(off, field.name))
        self.assertIs(fail_closed_settings().skills_enabled, False)

    def test_enable_paid_api_keeps_the_flag_as_it_was(self) -> None:
        self.assertIs(
            self.settings().enable_paid_api("approval-1").skills_enabled, False
        )
        self.assertIs(
            self.settings(skills_enabled=True)
            .enable_paid_api("approval-1")
            .skills_enabled,
            True,
        )


class TypeTests(SkillsFlagTestCase):
    def test_the_constructor_requires_an_exact_bool(self) -> None:
        for value in ("true", "false", 1, 0, None, 1.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.settings(skills_enabled=value)

    def test_a_non_bool_stored_value_is_refused(self) -> None:
        for value in ("true", "True", 1, 0, None, [], {}):
            with self.subTest(value=value):
                document = self.document()
                document[SKILLS_ENABLED_KEY] = value
                self.write_raw(document)
                result = self.store.load()
                self.assertIs(result.status, ConfigStatus.INVALID)
                self.assertIs(result.settings.skills_enabled, False)
                self.assertIn(SKILLS_ENABLED_KEY, result.reason or "")


class PersistenceTests(SkillsFlagTestCase):
    def test_round_trip_on(self) -> None:
        self.store.write(self.settings(skills_enabled=True))
        self.assertIs(self.stored()[SKILLS_ENABLED_KEY], True)
        result = self.store.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(result.settings.skills_enabled, True)

    def test_round_trip_off(self) -> None:
        self.store.write(self.settings(skills_enabled=True))
        self.store.write(self.settings(skills_enabled=False))
        result = self.store.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(result.settings.skills_enabled, False)

    def test_mutate_toggles_and_keeps_the_flag(self) -> None:
        self.store.write(self.settings())
        updated = self.store.mutate(
            lambda current: dataclasses.replace(current, skills_enabled=True)
        )
        self.assertIs(updated.skills_enabled, True)
        # An unrelated change must not drop the flag on rewrite.
        updated = self.store.mutate(
            lambda current: dataclasses.replace(current, approval_ttl_minutes=45)
        )
        self.assertIs(updated.skills_enabled, True)
        self.assertIs(self.store.load().settings.skills_enabled, True)
        updated = self.store.mutate(
            lambda current: dataclasses.replace(current, skills_enabled=False)
        )
        self.assertIs(updated.skills_enabled, False)
        self.assertNotIn(SKILLS_ENABLED_KEY, self.stored())

    def test_an_explicit_false_is_accepted(self) -> None:
        document = self.document()
        document[SKILLS_ENABLED_KEY] = False
        self.write_raw(document)
        result = self.store.load()
        self.assertIs(result.status, ConfigStatus.VALID)
        self.assertIs(result.settings.skills_enabled, False)

    def test_reset_all_turns_skills_off(self) -> None:
        self.store.write(self.settings(skills_enabled=True))
        self.assertIs(self.store.reset_all().skills_enabled, False)
        self.assertNotIn(SKILLS_ENABLED_KEY, self.stored())
        self.assertIs(self.store.load().settings.skills_enabled, False)

    def test_read_document_reports_the_flag_only_when_on(self) -> None:
        self.store.write(self.settings(skills_enabled=True))
        self.assertIs(self.store.read_document()[SKILLS_ENABLED_KEY], True)
        self.store.write(self.settings())
        self.assertNotIn(SKILLS_ENABLED_KEY, self.store.read_document())


class BackwardCompatibilityTests(SkillsFlagTestCase):
    def test_older_documents_without_the_key_load_off(self) -> None:
        full = self.document()
        for name, keys in (
            ("current", CONFIG_KEYS),
            ("pre-desktop", PRE_DESKTOP_CONFIG_KEYS),
            ("pre-github", PRE_GITHUB_CONFIG_KEYS),
            ("legacy", LEGACY_CONFIG_KEYS),
        ):
            with self.subTest(shape=name):
                self.write_raw({key: full[key] for key in keys})
                result = self.store.load()
                self.assertIs(result.status, ConfigStatus.VALID)
                self.assertIs(result.settings.skills_enabled, False)

    def test_an_off_document_keeps_the_previous_build_shape(self) -> None:
        # The rollback guarantee: with skills off, the written document has
        # exactly the key set the previous (schema-10) build writes and
        # accepts, so rolling back loads it VALID instead of refusing it.
        self.assertNotIn(SKILLS_ENABLED_KEY, CONFIG_KEYS)
        self.assertEqual(set(self.document()), set(CONFIG_KEYS))
        self.store.write(self.settings())
        self.assertEqual(set(self.stored()), set(CONFIG_KEYS))
        self.store.reset_all()
        self.assertEqual(set(self.stored()), set(CONFIG_KEYS))

    def test_an_on_document_is_exactly_the_skills_shape(self) -> None:
        self.assertEqual(SKILLS_CONFIG_KEYS, frozenset(CONFIG_KEYS) | {"skills_enabled"})
        self.assertEqual(set(self.document(skills_enabled=True)), set(SKILLS_CONFIG_KEYS))

    def test_the_key_on_an_older_shape_is_refused(self) -> None:
        # One more CLOSED shape, never an optional-keys hole: only the
        # current shape may carry the switch, because only this build
        # writes it and it always writes the full current shape.
        full = self.document(skills_enabled=True)
        for name, keys in (
            ("pre-desktop", PRE_DESKTOP_CONFIG_KEYS),
            ("pre-github", PRE_GITHUB_CONFIG_KEYS),
            ("legacy", LEGACY_CONFIG_KEYS),
        ):
            with self.subTest(shape=name):
                document = {key: full[key] for key in keys}
                document[SKILLS_ENABLED_KEY] = True
                self.write_raw(document)
                result = self.store.load()
                self.assertIs(result.status, ConfigStatus.INVALID)
                self.assertIs(result.settings.skills_enabled, False)

    def test_a_misspelled_key_is_still_refused(self) -> None:
        for typo in ("skill_enabled", "skills_enable", "Skills_enabled"):
            with self.subTest(key=typo):
                self.write_raw(self.document() | {typo: True})
                result = self.store.load()
                self.assertIs(result.status, ConfigStatus.INVALID)
                self.assertIs(result.settings.skills_enabled, False)


if __name__ == "__main__":
    unittest.main()
