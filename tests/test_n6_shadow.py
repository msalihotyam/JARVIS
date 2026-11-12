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
            "N6-01": ("excluded", "excluded", "block"),   # CLREV-N6-01 is not in lanes.json
            "CDX-N6-01": ("agree", "block", "block"),
            "N6-01FIX": ("unknown", "pass", "unknown"),   # no git head injected
            "N6-02": ("excluded", "excluded", "unknown"),  # CLREV/INT children pending
            "INT100B": ("agree", "block", "block"),
        })
        records = {r["lane"]: r for r in result["records"]}
        self.assertEqual(records["N6-01FIX"]["n6"]["reason"], "no independent sha (git head)")
        self.assertIn("CLREV-N6-01 pending", records["N6-01"]["conveyor"]["reason"])
        self.assertEqual(result["totals"], {"agree": 2, "disagree": 0, "false_pass": 0,
                                            "excluded": 2, "unknown": 1})
        self.assertEqual(result["ts"], clock().isoformat())
        self.assertTrue(all(r["n6"]["reason"] and r["conveyor"]["reason"]
                            for r in result["records"]))

    def test_head_evidence_closes_the_disagreement(self) -> None:
        records = self.by_lane(head=lambda lane: FIX_SHA if lane == "N6-01FIX" else None)
        self.assertEqual(records["N6-01FIX"]["verdict"], "agree")
        wrong = self.by_lane(head=lambda lane: "0" * 40)
        self.assertEqual(wrong["N6-01FIX"]["n6"]["verdict"], "unknown")

    def test_neg_report_sha_not_the_head_is_unknown(self) -> None:
        # The ledger's chain/ci sha is copied from the report, so only the git head counts.
        for head in (lambda lane: "0" * 40, lambda lane: None):
            records = self.by_lane(head=head)
            self.assertEqual(records["N6-01FIX"]["verdict"], "unknown")
            self.assertEqual(records["N6-01FIX"]["n6"]["verdict"], "unknown")
            self.assertFalse(any(r["verdict"] in ("agree", "false_pass")
                                 and r["n6"]["verdict"] == "pass" for r in records.values()))

    def test_neg_pending_chain_child_excludes_the_lane(self) -> None:
        records = self.by_lane(head=lambda lane: FIX_SHA)
        self.assertEqual(records["N6-02"]["verdict"], "excluded")
        self.assertIn("pending", records["N6-02"]["conveyor"]["reason"])
        lanes = json.loads((self.conveyor / "lanes.json").read_text(encoding="utf-8"))
        lanes["CLREV-N6-02"] = {"status": "PASS", "finished": False}
        lanes["INT-N6-02"] = {"status": "PASS", "finished": True}
        (self.conveyor / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        self.assertEqual(self.by_lane()["N6-02"]["verdict"], "excluded")

    def test_finished_chain_children_are_judged(self) -> None:
        lanes = json.loads((self.conveyor / "lanes.json").read_text(encoding="utf-8"))
        for child in ("CLREV-N6-01", "CLREV-N6-02", "INT-N6-02"):
            lanes[child] = {"status": "PASS", "finished": True}
        (self.conveyor / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        heads = {"N6-02": "ff2a698a09b572c8c55574e6fb6293b744f8f2e0", "N6-01FIX": FIX_SHA,
                 "N6-01": "83f131420162740a1b123b258d0c0ce50a32f02a"}
        records = self.by_lane(head=heads.get)
        self.assertEqual((records["N6-02"]["verdict"], records["N6-02"]["n6"]["verdict"]),
                         ("agree", "pass"))
        self.assertEqual((records["N6-01"]["verdict"], records["N6-01"]["n6"]["verdict"]),
                         ("agree", "block"))
        self.assertIn("CDX-N6-01 BLOCKED", records["N6-01"]["conveyor"]["reason"])

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
        self.assertEqual(records["N6-01FIX"]["n6"]["verdict"], "unknown")
        self.assertFalse(any(r["verdict"] == "false_pass" for r in records.values()))

    def test_neg_never_writes_under_build_conveyor(self) -> None:
        before = self.conveyor_digest()
        refuse = mock.Mock(side_effect=AssertionError("shadow wrote"))
        with mock.patch.object(Path, "write_text", refuse),                 mock.patch.object(Path, "write_bytes", refuse),                 mock.patch.object(Path, "mkdir", refuse),                 mock.patch.object(Path, "touch", refuse):
            result = n6_shadow.compare(self.root, clock=clock, read=guarded)
        self.assertEqual(result["totals"]["agree"], 2)
        self.assertEqual(self.conveyor_digest(), before)
        self.assertFalse(hasattr(n6_shadow, "write"))

    def no_commit_lane(self, report: str, files: list[str] | None) -> dict[str, object]:
        """A finished PASS lane RO-1 with ``report``; ``files`` None leaves the grant absent."""
        lanes = json.loads((self.conveyor / "lanes.json").read_text(encoding="utf-8"))
        lanes["RO-1"] = {"finished": True, "status": "PASS"}
        if files is not None:
            lanes["RO-1"]["files"] = files
        (self.conveyor / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        (self.root / "build" / "reports" / "RO-1.md").write_text(report, encoding="utf-8")
        return self.by_lane(head=lambda lane: None)["RO-1"]

    def test_commit_none_on_a_report_only_lane_passes(self) -> None:
        for files in (["build/reports/RO-1.md"], ["build\\reports\\RO-1.md"], [], None):
            record = self.no_commit_lane("STATUS: PASS\nCOMMIT: none\nFILES: report\n", files)
            self.assertEqual((record["verdict"], record["n6"]["verdict"]), ("agree", "pass"))

    def test_neg_malformed_commit_still_blocks(self) -> None:
        sha = FIX_SHA
        for report, reason in (
                (f"STATUS: PASS\nCOMMIT: {sha[:7]}\n", "report needs exactly one 40-hex COMMIT"),
                (f"STATUS: PASS\nCOMMIT: {sha} (main)\n", "report needs exactly one 40-hex COMMIT"),
                (f"STATUS: PASS\nBRANCH: x COMMIT: {sha}\n", "report needs exactly one 40-hex COMMIT"),
                (f"```\nSTATUS: PASS\nCOMMIT: {sha}\n```\n", "report says ```"),
                (f"STATUS: PASS\nCOMMIT: {sha}\nCOMMIT: {sha}\n", "report needs exactly one 40-hex COMMIT"),
                (f"STATUS: PASS\nCOMMIT: none\nCOMMIT: {sha}\n", "report needs exactly one 40-hex COMMIT"),
                ("STATUS: PASS\nCOMMIT: none\nCOMMIT: none\n", "report needs exactly one 40-hex COMMIT"),
                ("STATUS: PASS\nCOMMIT: none (read-only)\n", "report needs exactly one 40-hex COMMIT"),
                ("STATUS: PASS\nCOMMIT:none\n", "report needs exactly one 40-hex COMMIT"),
                ("STATUS: PASS\nCOMMIT: None\n", "report needs exactly one 40-hex COMMIT"),
                ("```\nSTATUS: PASS\nCOMMIT: none\n", "report says ```")):
            with self.subTest(report=report):
                record = self.no_commit_lane(report, ["build/reports/RO-1.md"])
                self.assertEqual(record["n6"], {"verdict": "block", "reason": reason})

    def test_neg_commit_none_on_a_code_lane_blocks(self) -> None:
        record = self.no_commit_lane("STATUS: PASS\nCOMMIT: none\n",
                                     ["build/reports/RO-1.md", "jarvis/n6_shadow.py"])
        self.assertEqual(record["n6"], {"verdict": "block",
                                        "reason": "COMMIT: none but the slice grants jarvis/n6_shadow.py"})
        self.assertEqual(record["verdict"], "disagree")

    def test_neg_commit_none_still_needs_state_and_chain(self) -> None:
        lanes = json.loads((self.conveyor / "lanes.json").read_text(encoding="utf-8"))
        lanes["CDX-N6-01"]["files"] = []
        (self.conveyor / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        for lane in ("CDX-N6-01", "INT100B", "N6-01"):
            (self.root / "build" / "reports" / f"{lane}.md").write_text(
                "STATUS: PASS\nCOMMIT: none\n", encoding="utf-8")
        records = self.by_lane()
        self.assertEqual(records["CDX-N6-01"]["n6"]["verdict"], "block")  # lane state BLOCKED
        self.assertEqual(records["INT100B"]["n6"]["verdict"], "block")    # lane state DROPPED
        self.assertEqual(records["N6-01"]["n6"]["verdict"], "block")      # CDX-N6-01 chained, BLOCKED
        self.assertIn("chained check CDX-N6-01", records["N6-01"]["n6"]["reason"])

    def test_default_verdicts_unchanged_without_commit_none(self) -> None:
        got = {lane: (r["verdict"], r["n6"]["verdict"], r["n6"]["reason"])
               for lane, r in self.by_lane().items()}
        self.assertEqual(got["N6-01FIX"], ("unknown", "unknown", "no independent sha (git head)"))
        self.assertEqual(got["CDX-N6-01"][:2], ("agree", "block"))
        self.assertEqual(got["INT100B"][:2], ("agree", "block"))

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
