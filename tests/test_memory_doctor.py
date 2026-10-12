"""Memory Doctor diagnostics (pure; snapshots are built inline, no database)."""

from __future__ import annotations

import unittest

from jarvis.contracts import (
    DataClassification,
    MemoryRecord,
    MemoryRecordActor,
    MemoryRecordKind,
    MemoryRecordOrigin,
    MemoryRecordScope,
    MemoryRecordStatus,
)
from jarvis.memory_doctor import (
    DoctorRecordState,
    Finding,
    FindingCode,
    LegacyM1Item,
    MemoryDoctorSnapshot,
    RepairAction,
    RepairProposal,
    Severity,
    diagnose,
)

NOW = "2026-10-13T00:00:00+00:00"
RECENT = "2026-10-12T10:00:00+00:00"
OLD = "2026-01-01T00:00:00+00:00"
APPROVED = MemoryRecordStatus.APPROVED
PENDING = MemoryRecordStatus.PENDING


def _record(record_id: str, **overrides) -> MemoryRecord:
    values = dict(
        record_id=record_id,
        kind=MemoryRecordKind.FACT,
        text=f"Fact number {record_id}.",
        subject_key=f"subject {record_id}",
        scope=MemoryRecordScope(project_id="jarvis"),
        origin=MemoryRecordOrigin.CAPTURE_SESSION_END,
        actor=MemoryRecordActor.MODEL,
        confidence=0.8,
        classification=DataClassification.INTERNAL,
        created_at=RECENT,
    )
    values.update(overrides)
    return MemoryRecord(**values)


def _state(record_id: str, status=APPROVED, **overrides) -> DoctorRecordState:
    return DoctorRecordState(record=_record(record_id, **overrides), status=status)


def _snapshot(*states: DoctorRecordState, fts=None, legacy=()) -> MemoryDoctorSnapshot:
    return MemoryDoctorSnapshot(
        taken_at=NOW,
        records=states,
        known_project_ids=frozenset({"jarvis", "other"}),
        known_conversation_ids=frozenset({"conv-1"}),
        fts_row_count=len(states) if fts is None else fts,
        legacy_items=tuple(legacy),
    )


def _allow(text: str) -> bool:
    return True


def _run(snapshot, is_storable=_allow) -> tuple[Finding, ...]:
    return diagnose(snapshot, is_storable=is_storable)


def _codes(findings) -> list[FindingCode]:
    return [f.code for f in findings]


class HealthySnapshotTests(unittest.TestCase):
    def test_healthy_snapshot_has_no_findings(self) -> None:
        snapshot = _snapshot(
            _state("a", status=MemoryRecordStatus.SUPERSEDED),
            _state("b", supersedes="a", subject_key="subject a"),
            _state("p", status=PENDING),
            _state("c", scope=MemoryRecordScope(project_id="jarvis", conversation_id="conv-1")),
            _state("r", status=MemoryRecordStatus.RETRACTED, created_at=OLD),
            legacy=[LegacyM1Item("m1", "jarvis", "fact"), LegacyM1Item("m2", "jarvis", "rule")],
        )
        self.assertEqual(_run(snapshot), ())

    def test_filter_is_required(self) -> None:
        with self.assertRaises(TypeError):
            diagnose(_snapshot())  # type: ignore[call-arg]

    def test_malformed_snapshot_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            MemoryDoctorSnapshot("garbage", (), frozenset(), frozenset(), 0)
        with self.assertRaises(ValueError):
            DoctorRecordState(record=_record("a"), status="approved")  # type: ignore[arg-type]


class OrphanScopeTests(unittest.TestCase):
    def test_unknown_project_is_orphan(self) -> None:
        state = _state("x", scope=MemoryRecordScope(project_id="gone"))
        (finding,) = _run(_snapshot(state))
        self.assertEqual(finding.code, FindingCode.ORPHAN_SCOPE)
        self.assertEqual(finding.record_ids, ("x",))
        self.assertEqual(finding.repair, RepairProposal(RepairAction.RETRACT, ("x",)))

    def test_unknown_conversation_is_orphan(self) -> None:
        state = _state("x", scope=MemoryRecordScope(project_id="jarvis", conversation_id="gone"))
        self.assertEqual(_codes(_run(_snapshot(state))), [FindingCode.ORPHAN_SCOPE])

    def test_known_scope_or_inactive_record_is_not_orphan(self) -> None:
        retracted = _state("x", status=MemoryRecordStatus.RETRACTED,
                           scope=MemoryRecordScope(project_id="gone"))
        self.assertEqual(_run(_snapshot(_state("y", scope=MemoryRecordScope(project_id="other")), retracted)), ())


class StaleTests(unittest.TestCase):
    def test_expired_approved_record_is_stale_with_expire_repair(self) -> None:
        state = _state("x", expires_at="2026-10-12T12:00:00+00:00")
        (finding,) = _run(_snapshot(state))
        self.assertEqual(finding.code, FindingCode.STALE)
        self.assertEqual(finding.summary_key, "memory_doctor.stale.expired")
        self.assertEqual(finding.repair, RepairProposal(RepairAction.EXPIRE, ("x",)))

    def test_long_unverified_record_is_stale(self) -> None:
        (finding,) = _run(_snapshot(_state("x", created_at=OLD)))
        self.assertEqual(finding.code, FindingCode.STALE)
        self.assertEqual(finding.summary_key, "memory_doctor.stale.unverified")
        self.assertIsNone(finding.repair)

    def test_recently_verified_or_future_expiry_is_not_stale(self) -> None:
        verified = _state("x", created_at=OLD, last_verified_at=RECENT)
        future = _state("y", expires_at="2026-12-01T00:00:00+00:00")
        self.assertEqual(_run(_snapshot(verified, future)), ())


class ConflictTests(unittest.TestCase):
    def test_two_approved_same_subject_conflict(self) -> None:
        older = _state("a", subject_key="Deploy target", created_at="2026-10-10T00:00:00+00:00")
        newer = _state("b", subject_key="deploy target")
        (finding,) = _run(_snapshot(older, newer))
        self.assertEqual(finding.code, FindingCode.CONFLICT_UNRESOLVED)
        self.assertEqual(finding.record_ids, ("a", "b"))
        self.assertEqual(finding.repair, RepairProposal(RepairAction.SUPERSEDE, ("b", "a")))

    def test_superseded_or_other_kind_or_project_is_no_conflict(self) -> None:
        old = _state("a", status=MemoryRecordStatus.SUPERSEDED, subject_key="k")
        new = _state("b", subject_key="k", supersedes="a")
        decision = _state("c", subject_key="k", kind=MemoryRecordKind.DECISION)
        elsewhere = _state("d", subject_key="k", scope=MemoryRecordScope(project_id="other"))
        self.assertEqual(_run(_snapshot(old, new, decision, elsewhere)), ())


class SupersessionCycleTests(unittest.TestCase):
    def test_three_cycle_terminates_and_is_reported_once(self) -> None:
        states = (
            _state("a", status=MemoryRecordStatus.SUPERSEDED, supersedes="c"),
            _state("b", status=MemoryRecordStatus.SUPERSEDED, supersedes="a"),
            _state("c", supersedes="b"),
            # Sorts first, so the walk enters the cycle from outside it.
            _state("0tail", status=MemoryRecordStatus.SUPERSEDED, supersedes="a"),
        )
        findings = _run(_snapshot(*states))
        self.assertEqual(
            findings,
            (Finding(FindingCode.SUPERSESSION_CYCLE, Severity.ERROR, ("a", "b", "c"),
                     "memory_doctor.supersession_cycle"),),
        )

    def test_chain_without_cycle_is_clean(self) -> None:
        states = (
            _state("a", status=MemoryRecordStatus.SUPERSEDED),
            _state("b", status=MemoryRecordStatus.SUPERSEDED, supersedes="a"),
            _state("c", supersedes="b"),
        )
        self.assertEqual(_run(_snapshot(*states)), ())


class DanglingSupersessionTests(unittest.TestCase):
    def test_missing_target_is_dangling(self) -> None:
        (finding,) = _run(_snapshot(_state("b", supersedes="ghost")))
        self.assertEqual(finding.code, FindingCode.DANGLING_SUPERSESSION)
        self.assertEqual(finding.record_ids, ("b",))

    def test_target_in_other_project_is_dangling(self) -> None:
        target = _state("a", status=MemoryRecordStatus.SUPERSEDED,
                        scope=MemoryRecordScope(project_id="other"))
        findings = _run(_snapshot(target, _state("b", supersedes="a")))
        self.assertEqual(_codes(findings), [FindingCode.DANGLING_SUPERSESSION])

    def test_existing_same_project_target_is_not_dangling(self) -> None:
        target = _state("a", status=MemoryRecordStatus.SUPERSEDED)
        self.assertEqual(_run(_snapshot(target, _state("b", supersedes="a"))), ())


class FtsDriftTests(unittest.TestCase):
    def test_count_mismatch_proposes_rebuild(self) -> None:
        (finding,) = _run(_snapshot(_state("a"), _state("b"), fts=1))
        self.assertEqual(finding.code, FindingCode.FTS_DRIFT)
        self.assertEqual(finding.repair, RepairProposal(RepairAction.REBUILD_FTS, ()))

    def test_matching_count_is_clean(self) -> None:
        self.assertEqual(_run(_snapshot(_state("a"), _state("b"), fts=2)), ())


class PendingTooOldTests(unittest.TestCase):
    def test_old_pending_is_reported(self) -> None:
        (finding,) = _run(_snapshot(_state("p", status=PENDING, created_at=OLD)))
        self.assertEqual(finding.code, FindingCode.PENDING_TOO_OLD)
        self.assertEqual(finding.repair, RepairProposal(RepairAction.EXPIRE, ("p",)))

    def test_fresh_pending_or_old_rejected_is_clean(self) -> None:
        fresh = _state("p", status=PENDING)
        rejected = _state("r", status=MemoryRecordStatus.REJECTED, created_at=OLD)
        self.assertEqual(_run(_snapshot(fresh, rejected)), ())


class FilterViolationTests(unittest.TestCase):
    def test_refused_text_is_violation(self) -> None:
        findings = _run(_snapshot(_state("a"), _state("b")), is_storable=lambda t: "b." not in t)
        (finding,) = findings
        self.assertEqual(finding.code, FindingCode.FILTER_VIOLATION)
        self.assertEqual(finding.severity, Severity.ERROR)
        self.assertEqual(finding.repair, RepairProposal(RepairAction.RETRACT, ("b",)))

    def test_filter_error_fails_closed(self) -> None:
        def broken(text: str) -> bool:
            raise RuntimeError("filter down")

        self.assertEqual(_codes(_run(_snapshot(_state("a")), is_storable=broken)),
                         [FindingCode.FILTER_VIOLATION])

    def test_non_bool_verdict_fails_closed(self) -> None:
        self.assertEqual(_codes(_run(_snapshot(_state("a")), is_storable=lambda t: "yes")),
                         [FindingCode.FILTER_VIOLATION])

    def test_inactive_record_is_not_checked(self) -> None:
        retracted = _state("a", status=MemoryRecordStatus.RETRACTED)
        self.assertEqual(_run(_snapshot(retracted), is_storable=lambda t: False), ())


class LegacyM1Tests(unittest.TestCase):
    def test_thread_and_unknown_kinds_are_unmapped(self) -> None:
        legacy = [LegacyM1Item("m2", "jarvis", "thread"), LegacyM1Item("m1", "jarvis", "weird")]
        findings = _run(_snapshot(legacy=legacy))
        self.assertEqual(_codes(findings), [FindingCode.LEGACY_M1_UNMAPPED] * 2)
        self.assertEqual([f.record_ids for f in findings], [("m1",), ("m2",)])

    def test_mapped_kinds_are_clean(self) -> None:
        legacy = [LegacyM1Item(f"m-{k}", "jarvis", k) for k in ("fact", "decision", "rule")]
        self.assertEqual(_run(_snapshot(legacy=legacy)), ())


class OrderingTests(unittest.TestCase):
    def test_output_is_deterministic_and_finding_id_stable(self) -> None:
        snapshot = _snapshot(_state("b", supersedes="ghost"), _state("a", created_at=OLD), fts=0)
        findings = _run(snapshot)
        self.assertEqual(_codes(findings), [FindingCode.STALE, FindingCode.DANGLING_SUPERSESSION,
                                            FindingCode.FTS_DRIFT])
        self.assertEqual(findings, _run(snapshot))
        self.assertEqual(findings[0].finding_id, "STALE:a")


if __name__ == "__main__":
    unittest.main()
