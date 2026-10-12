"""V2.1 typed memory record contracts (pure; no I/O, no database)."""

from __future__ import annotations

import dataclasses
import inspect
import unittest

from jarvis.contracts import (
    MEMORY_RECORD_TEXT_CAP,
    MEMORY_RECORD_TURN_IDS_CAP,
    MEMORY_SUBJECT_KEY_CAP,
    DataClassification,
    MemoryOrigin,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordCandidate,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordScopeKind,
    MemoryRecordStatus,
    ProjectMemoryKind,
    needs_user_approval,
    normalize_memory_subject_key,
)

CREATED = "2026-10-12T10:00:00+00:00"


def _scope(**kwargs) -> MemoryRecordScope:
    return MemoryRecordScope(project_id=kwargs.pop("project_id", "jarvis"), **kwargs)


def _record(**overrides) -> MemoryRecord:
    values = dict(
        kind=MemoryRecordKind.FACT,
        text="The build uses Python 3.12.",
        subject_key="Build Python version",
        scope=_scope(),
        origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
        actor=MemoryRecordActor.MODEL,
        confidence=0.8,
        classification=DataClassification.INTERNAL,
        created_at=CREATED,
    )
    values.update(overrides)
    return MemoryRecord(**values)


def _candidate(**overrides) -> MemoryRecordCandidate:
    values = dict(
        kind=MemoryRecordKind.LESSON,
        text="Run the targeted tests first.",
        subject_key="test order",
        confidence=0.5,
    )
    values.update(overrides)
    return MemoryRecordCandidate(**values)


class EnumVocabularyTests(unittest.TestCase):
    def test_every_enum_value_round_trips(self) -> None:
        for enum in (
            MemoryRecordKind,
            MemoryRecordStatus,
            MemoryRecordOrigin,
            MemoryRecordActor,
            MemoryRecordScopeKind,
        ):
            for member in enum:
                with self.subTest(member=member):
                    self.assertIs(enum(member.value), member)
                    self.assertIs(enum[member.name], member)

    def test_closed_vocabularies(self) -> None:
        self.assertEqual(
            {m.name for m in MemoryRecordKind},
            {"FACT", "DECISION", "RULE", "LESSON", "PREFERENCE"},
        )
        self.assertEqual(
            {m.name for m in MemoryRecordStatus},
            {"PENDING", "APPROVED", "REJECTED", "RETRACTED", "SUPERSEDED", "EXPIRED"},
        )
        self.assertEqual(
            {m.name for m in MemoryRecordOrigin},
            {
                "EXPLICIT_USER",
                "CAPTURE_SESSION_END",
                "CAPTURE_PRECOMPACT",
                "CONSOLIDATION",
                "IMPORT",
                "LEGACY_M1",
            },
        )
        for enum, bad in (
            (MemoryRecordKind, "thread"),
            (MemoryRecordStatus, "deleted"),
            (MemoryRecordOrigin, "inferred"),
        ):
            with self.subTest(enum=enum), self.assertRaises(ValueError):
                enum(bad)

    def test_m1_vocabularies_are_unchanged(self) -> None:
        self.assertEqual(
            [(m.name, m.value) for m in ProjectMemoryKind],
            [("FACT", "fact"), ("DECISION", "decision"), ("RULE", "rule"), ("THREAD", "thread")],
        )
        self.assertEqual(
            [(m.name, m.value) for m in MemoryOrigin],
            [
                ("EXPLICIT_USER", "explicit_user"),
                ("SYSTEM_DECISION", "system_decision"),
                ("INFERRED", "inferred"),
                ("IMPORTED", "imported"),
            ],
        )


class ApprovalPolicyTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        expected = {
            MemoryRecordKind.FACT: False,
            MemoryRecordKind.LESSON: False,
            MemoryRecordKind.DECISION: True,
            MemoryRecordKind.RULE: True,
            MemoryRecordKind.PREFERENCE: True,  # fail-closed until Q1 decided
        }
        self.assertEqual(set(expected), set(MemoryRecordKind))
        for kind, needs in expected.items():
            with self.subTest(kind=kind):
                self.assertIs(needs_user_approval(kind), needs)

    def test_non_kind_raises(self) -> None:
        for bad in ("rule", ProjectMemoryKind.RULE, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                needs_user_approval(bad)


class ScopeTests(unittest.TestCase):
    def test_project_scope_requires_valid_project(self) -> None:
        self.assertEqual(_scope().project_id, "jarvis")
        self.assertEqual(_scope(conversation_id="c-abc123").conversation_id, "c-abc123")
        for bad in (None, "", "X", "../etc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MemoryRecordScope(project_id=bad)

    def test_global_readonly_is_project_less(self) -> None:
        scope = MemoryRecordScope(None, kind=MemoryRecordScopeKind.GLOBAL_READONLY)
        self.assertIsNone(scope.project_id)
        with self.assertRaises(ValueError):
            MemoryRecordScope("jarvis", kind=MemoryRecordScopeKind.GLOBAL_READONLY)

    def test_bad_scope_kind_and_conversation_raise(self) -> None:
        with self.assertRaises(ValueError):
            MemoryRecordScope("jarvis", kind="project")
        with self.assertRaises(ValueError):
            MemoryRecordScope("jarvis", conversation_id="has space")


class MemoryRecordTests(unittest.TestCase):
    def test_valid_record_is_frozen_and_normalized(self) -> None:
        record = _record(confidence=1)
        self.assertEqual(record.subject_key, "build-python-version")
        self.assertIsInstance(record.confidence, float)
        self.assertTrue(record.record_id)
        self.assertFalse(hasattr(record, "status"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.text = "changed"

    def test_invalid_enums_raise(self) -> None:
        for field_name, bad in (
            ("kind", "fact"),
            ("kind", ProjectMemoryKind.FACT),
            ("origin", "explicit_user"),
            ("origin", MemoryOrigin.EXPLICIT_USER),
            ("actor", "user"),
            ("classification", "internal"),
            ("scope", "jarvis"),
        ):
            with self.subTest(field=field_name, bad=bad), self.assertRaises(ValueError):
                _record(**{field_name: bad})

    def test_invalid_confidence_raises(self) -> None:
        for bad in (-0.01, 1.01, float("nan"), float("inf"), 10**400, True, "0.5", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _record(confidence=bad)

    def test_invalid_text_raises(self) -> None:
        for bad in ("", "   \n", None, "x" * (MEMORY_RECORD_TEXT_CAP + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _record(text=bad)
        self.assertEqual(len(_record(text="x" * MEMORY_RECORD_TEXT_CAP).text), MEMORY_RECORD_TEXT_CAP)

    def test_secret_classification_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _record(classification=DataClassification.SECRET)

    def test_global_scope_is_refused_on_a_record(self) -> None:
        with self.assertRaises(ValueError):
            _record(scope=MemoryRecordScope(None, kind=MemoryRecordScopeKind.GLOBAL_READONLY))

    def test_explicit_user_origin_requires_user_actor(self) -> None:
        _record(origin=MemoryRecordOrigin.EXPLICIT_USER, actor=MemoryRecordActor.USER)
        for actor in (MemoryRecordActor.MODEL, MemoryRecordActor.TOOL):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                _record(origin=MemoryRecordOrigin.EXPLICIT_USER, actor=actor, tool="t")

    def test_tool_actor_must_name_tool(self) -> None:
        self.assertEqual(_record(actor=MemoryRecordActor.TOOL, tool="importer").tool, "importer")
        for bad in (None, "", "  ", "t" * 65):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _record(actor=MemoryRecordActor.TOOL, tool=bad)
        with self.assertRaises(ValueError):
            _record(actor=MemoryRecordActor.MODEL, tool="importer")

    def test_timestamps(self) -> None:
        later = "2026-10-13T10:00:00+00:00"
        record = _record(last_verified_at=later, expires_at=later)
        self.assertEqual(record.expires_at, later)
        self.assertEqual(_record(last_verified_at=CREATED).last_verified_at, CREATED)
        for overrides in (
            {"created_at": "yesterday"},
            {"created_at": "2026-10-12T10:00:00"},  # naive
            {"created_at": "2026-10-12T10:00:00+03:00"},  # not UTC
            {"last_verified_at": "2026-10-11T10:00:00+00:00"},
            {"expires_at": CREATED},
            {"expires_at": "2026-10-11T10:00:00+00:00"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _record(**overrides)

    def test_supersession_and_provenance(self) -> None:
        record = _record(
            record_id="r-2",
            supersedes="r-1",
            source_conversation_id="c-0a1b",
            source_turn_ids=("t-1", "t-2"),
        )
        self.assertEqual(record.supersedes, "r-1")
        for overrides in (
            {"record_id": "r-1", "supersedes": "r-1"},
            {"record_id": ""},
            {"supersedes": "bad id"},
            {"source_conversation_id": "x" * 129},
            {"source_turn_ids": ["t-1"]},
            {"source_turn_ids": ("",)},
            {"source_turn_ids": tuple(f"t-{i}" for i in range(MEMORY_RECORD_TURN_IDS_CAP + 1))},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _record(**overrides)


class SubjectKeyTests(unittest.TestCase):
    def test_normalization(self) -> None:
        self.assertEqual(normalize_memory_subject_key("  Build  PYTHON_version! "), "build-python-version")
        self.assertEqual(normalize_memory_subject_key("ＢＵＩＬＤ"), "build")
        for spelling in ("İzmir ofisi", "IZMIR ofisi", "izmir ofisi"):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_memory_subject_key(spelling), "izmir-ofisi")

    def test_invalid_keys_raise(self) -> None:
        for bad in ("", "  -- !! ", None, 5, "a" * (MEMORY_SUBJECT_KEY_CAP + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_memory_subject_key(bad)


class CandidateTests(unittest.TestCase):
    def test_candidate_carries_only_proposal_fields(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(MemoryRecordCandidate)],
            ["kind", "text", "subject_key", "confidence", "evidence_turn_ids"],
        )
        params = set(inspect.signature(MemoryRecordCandidate).parameters)
        for authority in ("origin", "status", "scope", "actor", "classification", "record_id"):
            with self.subTest(field=authority):
                self.assertNotIn(authority, params)
                with self.assertRaises(TypeError):
                    _candidate(**{authority: None})

    def test_valid_candidate_is_frozen_and_normalized(self) -> None:
        candidate = _candidate(evidence_turn_ids=("t-1",), confidence=0)
        self.assertEqual(candidate.subject_key, "test-order")
        self.assertEqual(candidate.confidence, 0.0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candidate.kind = MemoryRecordKind.RULE

    def test_invalid_candidate_raises(self) -> None:
        for overrides in (
            {"kind": "lesson"},
            {"text": " "},
            {"text": "x" * (MEMORY_RECORD_TEXT_CAP + 1)},
            {"subject_key": "!!"},
            {"confidence": 2},
            {"confidence": False},
            {"evidence_turn_ids": ["t-1"]},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _candidate(**overrides)


if __name__ == "__main__":
    unittest.main()
