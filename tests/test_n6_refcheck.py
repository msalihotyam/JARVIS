"""N6-15: acceptance verifies run_id, a stable candidate ref and per-pick patch-ids (fakes only, no git)."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from jarvis import n6_refcheck
from jarvis.n6_refcheck import RefCheckError, accept, parse_picks, read_evidence

REF = "refs/heads/ci/INTN6"
HEAD = "a" * 40
MOVED = "b" * 40
SRC1, NEW1 = "1111111" + "0" * 33, "2222222" + "0" * 33
SRC2, NEW2 = "3333333" + "0" * 33, "4444444" + "0" * 33
PID1, PID2 = "c" * 40, "d" * 40

GOOD = (
    "STATUS: CI_PENDING\n"
    "RUN_ID: run-2026-11-08.1\n"
    "PICKS old -> new (all clean, no conflicts):\n"
    "  1111111 -> 2222222 N6-14\n"
    "  3333333 -> 4444444 N6-13\n"
    "NEW MAIN SHA: " + HEAD + "\n"
)


class FakeGit:
    """Scripted ``rev_parse`` for the ref; a fixed table for picks and patch-ids."""

    def __init__(self, ref_reads: list[object], patch: dict[str, object] | None = None) -> None:
        self.ref_reads = list(ref_reads)
        self.shas = {s[:7]: s for s in (SRC1, NEW1, SRC2, NEW2)}
        self.patch: dict[str, object] = patch if patch is not None else {SRC1: PID1, NEW1: PID1, SRC2: PID2, NEW2: PID2}

    def rev_parse(self, rev: str) -> object:
        if rev == REF:
            return self.ref_reads.pop(0)
        return self.shas[rev]

    def patch_id(self, sha: str) -> object:
        return self.patch[sha]


def _read(git: FakeGit, report: object = GOOD, run_id: str = "run-2026-11-08.1"):
    return read_evidence(git, REF, lambda: report, run_id)


class N6RefcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        # NEG: nothing in this module may open a database (data/jarvis.db least of all).
        patcher = mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened"))
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- torn read ---------------------------------------------------------------------
    def test_neg_ref_changes_during_evidence_read_raises(self):
        with self.assertRaisesRegex(RefCheckError, "torn read"):
            _read(FakeGit([HEAD, MOVED]))

    def test_neg_partial_or_malformed_sha_raises(self):
        for bad in (HEAD[:39], HEAD[:7], HEAD.upper(), HEAD + "\n", " " + HEAD, "", None, b"a" * 40, 1, "g" * 40):
            for reads in ([bad, HEAD], [HEAD, bad]):
                with self.subTest(bad=repr(bad)[:20], first=reads[0] is bad), self.assertRaisesRegex(RefCheckError, "torn or malformed"):
                    _read(FakeGit(reads))

    def test_neg_report_read_non_str_or_empty_raises(self):
        for bad in (None, b"RUN_ID: x", "", 0):
            with self.subTest(bad=bad), self.assertRaisesRegex(RefCheckError, "torn or empty report"):
                _read(FakeGit([HEAD, HEAD]), report=bad)

    # --- run_id ------------------------------------------------------------------------
    def test_neg_mismatched_run_id_refused(self):
        for other in ("run-2026-11-08.2", "RUN-2026-11-08.1", "run-2026-11-08"):
            with self.subTest(other=other), self.assertRaisesRegex(RefCheckError, "run_id mismatch"):
                _read(FakeGit([HEAD, HEAD]), run_id=other)

    def test_neg_missing_duplicate_or_malformed_run_id_line_refused(self):
        cases = {
            "missing": GOOD.replace("RUN_ID: run-2026-11-08.1\n", ""),
            "duplicate": GOOD.replace("RUN_ID: run-2026-11-08.1\n", "RUN_ID: run-2026-11-08.1\nRUN_ID: run-2026-11-08.1\n"),
            "conflicting": GOOD + "RUN_ID: run-other\n",
            "indented conflicting": "  RUN_ID: run-other\n" + GOOD,
        }
        for name, report in cases.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "exactly one RUN_ID"):
                _read(FakeGit([HEAD, HEAD]), report=report)
        for line in ("RUN_ID:", "RUN_ID: a b", "RUN_ID run-2026-11-08.1", "RUN_ID: -x"):
            report = GOOD.replace("RUN_ID: run-2026-11-08.1", line)
            with self.subTest(line), self.assertRaisesRegex(RefCheckError, "malformed RUN_ID"):
                _read(FakeGit([HEAD, HEAD]), report=report)

    def test_neg_malformed_expected_run_id_or_ref_raises(self):
        for run_id in ("", "a b", "-x", "x" * 200):
            with self.subTest(run_id=run_id[:10]), self.assertRaisesRegex(RefCheckError, "malformed run_id"):
                _read(FakeGit([HEAD, HEAD]), run_id=run_id)
        for ref in ("", "-h", "a..b", "ref with space", "refs/heads/x\n"):
            with self.subTest(ref=ref), self.assertRaisesRegex(RefCheckError, "malformed ref"):
                read_evidence(FakeGit([HEAD, HEAD]), ref, lambda: GOOD, "run-2026-11-08.1")

    # --- compare-before-accept ---------------------------------------------------------
    def test_neg_ref_moved_between_read_and_accept_refused(self):
        git = FakeGit([HEAD, HEAD, MOVED])
        ev = _read(git)
        with self.assertRaisesRegex(RefCheckError, "between read and accept"):
            accept(git, ev)

    def test_neg_torn_read_at_accept_raises(self):
        git = FakeGit([HEAD, HEAD, HEAD[:12]])
        ev = _read(git)
        with self.assertRaisesRegex(RefCheckError, "torn or malformed"):
            accept(git, ev)

    # --- PICKS cleanup evidence --------------------------------------------------------
    def test_neg_missing_picks_block_refused(self):
        report = GOOD.split("PICKS")[0] + "NEW MAIN SHA: " + HEAD + "\n"
        with self.assertRaisesRegex(RefCheckError, "exactly one PICKS block, found 0"):
            _read(FakeGit([HEAD, HEAD]), report=report)

    def test_neg_two_or_empty_picks_blocks_refused(self):
        with self.assertRaisesRegex(RefCheckError, "exactly one PICKS block, found 2"):
            parse_picks(GOOD + "PICKS:\n  5555555 -> 6666666 N6-12\n")
        with self.assertRaisesRegex(RefCheckError, "empty PICKS block"):
            parse_picks("PICKS:\nNEW MAIN SHA: x\n")

    def test_neg_pick_lines_orchbot_would_count_are_strict(self):
        bad = {
            "blank line hides a pick": "PICKS:\n  1111111 -> 2222222 N6-14\n\n  3333333 N6-13\n",
            "lane only": "PICKS:\n  N6-14\n",
            "semicolon joined": "PICKS:\n  1111111 -> 2222222 N6-14; 3333333 -> 4444444 N6-13\n",
            "header colon content": "PICKS: N6-14\n  1111111 -> 2222222 N6-14\n",
            "short sha": "PICKS:\n  111111 -> 2222222 N6-14\n",
            "uppercase sha": "PICKS:\n  ABCDEF1 -> 2222222 N6-14\n",
        }
        for name, report in bad.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                parse_picks(report)

    def test_neg_duplicate_pick_refused(self):
        for line in ("  1111111 -> 5555555 N6-12", "  5555555 -> 2222222 N6-12", "  5555555 -> 6666666 N6-14"):
            with self.subTest(line), self.assertRaisesRegex(RefCheckError, "duplicate pick"):
                parse_picks("PICKS:\n  1111111 -> 2222222 N6-14\n" + line + "\n")

    def test_neg_pick_patch_id_differs_refused(self):
        git = FakeGit([HEAD, HEAD], {SRC1: PID1, NEW1: PID2, SRC2: PID2, NEW2: PID2})
        with self.assertRaisesRegex(RefCheckError, "N6-14 1111111->2222222: patch-id differs"):
            _read(git)

    def test_neg_missing_or_malformed_patch_id_raises(self):
        for bad in ("", None, PID1[:39], PID1.upper()):
            git = FakeGit([HEAD, HEAD], {SRC1: PID1, NEW1: bad, SRC2: PID2, NEW2: PID2})
            with self.subTest(bad=bad), self.assertRaisesRegex(RefCheckError, "patch-id of"):
                _read(git)

    def test_neg_abbreviation_resolving_elsewhere_raises(self):
        git = FakeGit([HEAD, HEAD])
        git.shas["2222222"] = "9" * 40
        with self.assertRaisesRegex(RefCheckError, "resolved to a different commit"):
            _read(git)

    def test_neg_pick_reusing_a_commit_refused(self):
        self_pick = GOOD.replace("1111111 -> 2222222", "1111111 -> 11111110")  # the source itself: patch-ids trivially equal
        git = FakeGit([HEAD, HEAD])
        git.shas["11111110"] = SRC1
        with self.assertRaisesRegex(RefCheckError, "commit reused"):
            _read(git, report=self_pick)
        git = FakeGit([HEAD, HEAD])
        report = GOOD.replace("3333333 -> 4444444", "1111111000 -> 4444444")  # same source, longer abbrev
        git.shas["1111111000"] = SRC1
        with self.assertRaisesRegex(RefCheckError, "commit reused"):
            _read(git, report=report)

    def test_neg_non_str_ref_or_run_id_raises_refcheck_error(self):
        for ref, run_id in ((None, "run-2026-11-08.1"), (REF, None), (b"refs/x", "r"), (REF, 7)):
            with self.subTest(ref=ref, run_id=run_id), self.assertRaisesRegex(RefCheckError, "malformed"):
                read_evidence(FakeGit([HEAD, HEAD]), ref, lambda: GOOD, run_id)  # pyright: ignore[reportArgumentType]
        with self.assertRaisesRegex(RefCheckError, "not text"):
            parse_picks(None)  # pyright: ignore[reportArgumentType]
        sub = type("S", (str,), {})
        with self.assertRaisesRegex(RefCheckError, "torn or malformed"):
            _read(FakeGit([sub(HEAD), HEAD]))
        with self.assertRaisesRegex(RefCheckError, "torn or empty report"):
            _read(FakeGit([HEAD, HEAD]), report=sub(GOOD))

    # --- OK ----------------------------------------------------------------------------
    def test_ok_consistent_evidence_accepted(self):
        git = FakeGit([HEAD, HEAD, HEAD])
        ev = _read(git)
        self.assertEqual((ev.run_id, ev.ref, ev.sha), ("run-2026-11-08.1", REF, HEAD))
        self.assertEqual(
            [(p.source, p.new, p.lane) for p in ev.picks], [("1111111", "2222222", "N6-14"), ("3333333", "4444444", "N6-13")]
        )
        self.assertEqual(accept(git, ev), HEAD)
        self.assertEqual(git.ref_reads, [])

    def test_ok_crlf_report_accepted(self):
        git = FakeGit([HEAD, HEAD, HEAD])
        self.assertEqual(accept(git, _read(git, report=GOOD.replace("\n", "\r\n"))), HEAD)

    def test_neg_module_runs_no_process_and_opens_no_db(self):
        src = Path(n6_refcheck.__file__).read_text(encoding="utf-8")
        for word in ("sqlite3", "jarvis.db", "subprocess", "open("):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
