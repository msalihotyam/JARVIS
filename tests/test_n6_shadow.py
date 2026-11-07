"""N6-03 shadow comparator: known verdicts on sanitized past lanes, and the read-only negatives."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from jarvis import n6_shadow
from jarvis.n6_root import _guard  # pyright: ignore[reportPrivateUsage]

FIXTURES = Path(__file__).parent / "fixtures" / "n6"
FIX_SHA = "5d6f096ca07f06a046bc38bf9f3d3b55aeed1a6d"


def guarded(path: Path) -> str:
    """The N6-01 sight read: guarded, read-only."""
    return _guard(path).read_text(encoding="utf-8")


def clock() -> datetime:
    return datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


class N6ShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.conveyor = self.root / "build" / "conveyor"
        self.conveyor.mkdir(parents=True)
        shutil.copy(FIXTURES / "lanes.json", self.conveyor / "lanes.json")
        shutil.copy(FIXTURES / "events.jsonl", self.conveyor / "events.jsonl")
        shutil.copytree(FIXTURES / "reports", self.root / "build" / "reports")

    def by_lane(self, **kwargs: object) -> dict[str, dict[str, object]]:
        result = n6_shadow.compare(self.root, clock=clock, read=guarded, **kwargs)  # type: ignore[arg-type]
        return {r["lane"]: r for r in result["records"]}

    def conveyor_digest(self) -> list[tuple[str, str]]:
        return sorted((p.relative_to(self.conveyor).as_posix(),
                       hashlib.sha256(p.read_bytes()).hexdigest())
                      for p in self.conveyor.rglob("*") if p.is_file())

    def test_past_lanes_give_known_verdicts(self) -> None:
        result = n6_shadow.compare(self.root, clock=clock, read=guarded)
        got = {r["lane"]: (r["verdict"], r["conveyor"]["verdict"], r["n6"]["verdict"])
               for r in result["records"]}
        self.assertEqual(got, {
            "N6-01": ("agree", "block", "block"),      # its CDX check blocked it
            "CDX-N6-01": ("agree", "block", "block"),
            "N6-01FIX": ("disagree", "pass", "block"),  # no sha besides the report
            "N6-02": ("agree", "pass", "pass"),
            "INT100B": ("agree", "block", "block"),
        })
        records = {r["lane"]: r for r in result["records"]}
        self.assertEqual(records["N6-01FIX"]["n6"]["reason"], "no sha evidence besides the report")
        self.assertIn("CDX-N6-01 BLOCKED", records["N6-01"]["conveyor"]["reason"])
        self.assertEqual(result["totals"],
                         {"agree": 4, "disagree": 1, "false_pass": 0, "excluded": 0})
        self.assertEqual(result["ts"], clock().isoformat())
        self.assertTrue(all(r["n6"]["reason"] and r["conveyor"]["reason"]
                            for r in result["records"]))

    def test_head_evidence_closes_the_disagreement(self) -> None:
        records = self.by_lane(head=lambda lane: FIX_SHA if lane == "N6-01FIX" else None)
        self.assertEqual(records["N6-01FIX"]["verdict"], "agree")
        wrong = self.by_lane(head=lambda lane: "0" * 40)
        self.assertEqual(wrong["N6-01FIX"]["n6"]["verdict"], "block")

    def test_neg_torn_snapshot_raises(self) -> None:
        lanes = (self.conveyor / "lanes.json").read_text(encoding="utf-8")
        reads: list[Path] = []

        def moving(path: Path) -> str:
            reads.append(path)
            if path.name == "lanes.json" and len(reads) > 1:
                return lanes.replace('"PASS"', '"BLOCKED"', 1)
            return path.read_text(encoding="utf-8")

        with self.assertRaises(n6_shadow.TornSnapshot):
            n6_shadow.compare(self.root, clock=clock, read=moving)
        (self.conveyor / "lanes.json").write_text(lanes[: len(lanes) // 2], encoding="utf-8")
        with self.assertRaises(n6_shadow.TornSnapshot):
            n6_shadow.load(self.root, read=guarded)
        (self.conveyor / "lanes.json").write_text(lanes, encoding="utf-8")
        with (self.conveyor / "events.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write('{"ts": "x", "event": "fin')
        with self.assertRaises(n6_shadow.TornSnapshot):
            n6_shadow.load(self.root, read=guarded)

    def test_neg_report_text_alone_never_passes(self) -> None:
        lanes = json.loads((self.conveyor / "lanes.json").read_text(encoding="utf-8"))
        lanes["N6-02"]["status"] = "BLOCKED"
        (self.conveyor / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        (self.root / "build" / "reports" / "ROGUE.md").write_text(
            f"STATUS: PASS\nCOMMIT: {FIX_SHA}\n", encoding="utf-8")
        records = self.by_lane()
        self.assertEqual(records["N6-02"]["n6"]["verdict"], "block")
        self.assertNotIn("ROGUE", records)
        self.assertEqual(records["N6-01FIX"]["n6"]["verdict"], "block")
        self.assertFalse(any(r["verdict"] == "false_pass" for r in records.values()))

    def test_neg_never_writes_under_build_conveyor(self) -> None:
        before = self.conveyor_digest()
        refuse = mock.Mock(side_effect=AssertionError("shadow wrote"))
        with mock.patch.object(Path, "write_text", refuse),                 mock.patch.object(Path, "write_bytes", refuse),                 mock.patch.object(Path, "mkdir", refuse),                 mock.patch.object(Path, "touch", refuse):
            result = n6_shadow.compare(self.root, clock=clock, read=guarded)
        self.assertEqual(result["totals"]["agree"], 4)
        self.assertEqual(self.conveyor_digest(), before)
        self.assertFalse(hasattr(n6_shadow, "write"))

    def test_neg_never_opens_the_live_db(self) -> None:
        paths: list[Path] = []

        def tracing(path: Path) -> str:
            paths.append(path)
            return path.read_text(encoding="utf-8")

        with mock.patch("sqlite3.connect", side_effect=AssertionError("db opened")):
            n6_shadow.compare(self.root, clock=clock, read=tracing)
        self.assertTrue(paths)
        self.assertFalse(any("jarvis.db" in str(p) for p in paths))


if __name__ == "__main__":
    unittest.main()
