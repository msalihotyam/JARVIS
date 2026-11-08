"""N6-15: acceptance verifies run_id, a stable candidate ref and per-pick patch-ids (fakes only, no git)."""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from jarvis import n6_refcheck
from jarvis.n6_refcheck import Evidence, Pick, RefCheckError, accept, parse_picks, read_evidence

RUN = "run-2026-11-08.1"

REF = "refs/heads/ci/INTN6"
HEAD = "a" * 40
MOVED = "b" * 40
SRC1, NEW1 = "1111111" + "0" * 33, "2222222" + "0" * 33
SRC2, NEW2 = "3333333" + "0" * 33, "4444444" + "0" * 33
S5, S6 = "5555555" + "0" * 33, "6666666" + "0" * 33
PID1, PID2 = "c" * 40, "d" * 40

GOOD = (
    "STATUS: CI_PENDING\n"
    "RUN_ID: run-2026-11-08.1\n"
    "PICKS old -> new (all clean, no conflicts):\n"
    f"  {SRC1} -> {NEW1} N6-14\n"
    f"  {SRC2} -> {NEW2} N6-13\n"
    "NEW MAIN SHA: " + HEAD + "\n"
)

# Verbatim PICKS lines from real INT reports in build/reports (read-only copies).
REAL_OK = {
    "INT118": (
        "  5646e8f3cb9807a2fcfb996ef4d5b5d0d8a12a40 -> 0a40aa463aac2627fd6017ce2bf9587dffc4808b (N6-10)",
        ("5646e8f3cb9807a2fcfb996ef4d5b5d0d8a12a40", "0a40aa463aac2627fd6017ce2bf9587dffc4808b", "N6-10"),
    ),
    "INT118 fix lane": (
        "  da006444e175b282e05b44db816f603eee50f4bd -> 10a475728879c6dcafd670b099a748bb068f8432 (N6-10FIX)",
        ("da006444e175b282e05b44db816f603eee50f4bd", "10a475728879c6dcafd670b099a748bb068f8432", "N6-10FIX"),
    ),
    "INT-N6-11 lane between": (
        "  a2fd7079378ec55c0d17423b30991efb91959d3a (N6-11) -> e2b281ff3f8a3dc4900b054b38429c458162d946",
        ("a2fd7079378ec55c0d17423b30991efb91959d3a", "e2b281ff3f8a3dc4900b054b38429c458162d946", "N6-11"),
    ),
    "INT-N6-09 lane first, notes after": (
        "  N6-09 fbae69e6f06bb6717ee6e9927bb91fc5b67ddb5a -> b7e8429e034e146c61d071f643550b84c82d50a6 (clean, no conflict)",
        ("fbae69e6f06bb6717ee6e9927bb91fc5b67ddb5a", "b7e8429e034e146c61d071f643550b84c82d50a6", "N6-09"),
    ),
    "INT-N6-HARD1 lane between, word after": (
        "  3b050c8f8b265811366756ccdd3231c1d1fa5689 (N6-HARD1) -> 2eb193c82adc81357c573dd0ef780714130895f6 clean",
        ("3b050c8f8b265811366756ccdd3231c1d1fa5689", "2eb193c82adc81357c573dd0ef780714130895f6", "N6-HARD1"),
    ),
    "INT47 lane first, notes after": (
        f"  INT47-AFX {SRC1} -> {NEW1} (tests only, 2 files)",
        (SRC1, NEW1, "INT47-AFX"),
    ),
}
# INT117 wrote abbreviated candidates: refused, a full sha is required on both sides.
REAL_ABBREVIATED = (
    "  1f573977dc8f9fbbeb228a67a8b4f89d1dae8c62 N6-08 -> 37378abd",
    "  542984da329e01c5b2ab69d7af423fce1594a510 N6-08FIX -> 1a1ca721",
    "  fbae69e6f06bb6717ee6e9927bb91fc5b67ddb5a N6-09 -> 77ec711f",
)


class FakeGit:
    """Scripted ``rev_parse`` for the ref; a fixed table for picks and patch-ids."""

    def __init__(self, ref_reads: list[object], patch: dict[str, object] | None = None) -> None:
        self.ref_reads = list(ref_reads)
        self.shas: dict[str, object] = {s: s for s in (SRC1, NEW1, SRC2, NEW2)}
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
            accept(git, ev, RUN)

    def test_neg_torn_read_at_accept_raises(self):
        git = FakeGit([HEAD, HEAD, HEAD[:12]])
        ev = _read(git)
        with self.assertRaisesRegex(RefCheckError, "torn or malformed"):
            accept(git, ev, RUN)

    # --- PICKS cleanup evidence --------------------------------------------------------
    def test_neg_missing_picks_block_refused(self):
        report = GOOD.split("PICKS")[0] + "NEW MAIN SHA: " + HEAD + "\n"
        with self.assertRaisesRegex(RefCheckError, "exactly one PICKS block, found 0"):
            _read(FakeGit([HEAD, HEAD]), report=report)

    def test_neg_two_or_empty_picks_blocks_refused(self):
        with self.assertRaisesRegex(RefCheckError, "exactly one PICKS block, found 2"):
            parse_picks(GOOD + f"PICKS:\n  {S5} -> {S6} N6-12\n")
        with self.assertRaisesRegex(RefCheckError, "empty PICKS block"):
            parse_picks("PICKS:\nNEW MAIN SHA: x\n")

    def test_ok_real_int_report_pick_lines_parse(self):
        for name, (line, want) in REAL_OK.items():
            with self.subTest(name):
                (p,) = parse_picks(f"STATUS: CI_PENDING\nPICKS:\n{line}\nFILES: x\n")
                self.assertEqual((p.source, p.new, p.lane), want)

    def test_ok_verbatim_int118_block_parses_and_ends_at_unindented_line(self):
        block = "PICKS:\n" + "\n".join(REAL_OK[k][0] for k in ("INT118", "INT118 fix lane")) + "\n"
        block += "  a2fd7079378ec55c0d17423b30991efb91959d3a -> b9058e7d4b3da2a40242f10e80ecb11d97c27064 (N6-11)\n"
        block += "RESOLVED HUNKS: scripts/test_durations.json (N6-11 pick): both entries kept\n"
        self.assertEqual([p.lane for p in parse_picks(block)], ["N6-10", "N6-10FIX", "N6-11"])

    def test_neg_real_int117_abbreviated_candidate_refused(self):
        for line in REAL_ABBREVIATED:
            with self.subTest(line), self.assertRaisesRegex(RefCheckError, "exactly two full 40-hex shas"):
                parse_picks(f"PICKS:\n{line}\n")

    def test_neg_pick_line_without_exactly_two_full_shas_refused(self):
        bad = {
            "one sha": f"  {SRC1} -> N6-14",
            "one sha, lane only": f"  {SRC1} N6-14",
            "three shas": f"  {SRC1} -> {NEW1} N6-14 (was {S5})",
            "three shas, third glued": f"  {SRC1} -> {NEW1} N6-14 {S5}-x",
            "abbreviated source": f"  {SRC1[:7]} -> {NEW1} N6-14",
            "abbreviated note": f"  {SRC1} -> {NEW1} N6-14 (was 37378abd)",
            "39 hex": f"  {SRC1[:39]} -> {NEW1} N6-14",
            "41 hex": f"  {SRC1}0 -> {NEW1} N6-14",
            "uppercase sha": f"  {'AB' * 20} -> {NEW1} N6-14",
            "no sha": "  N6-14",
            "semicolon joined": f"  {SRC1} -> {NEW1} N6-14; {SRC2} -> {NEW2} N6-13",
        }
        for name, line in bad.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "exactly two full 40-hex shas"):
                parse_picks(f"PICKS:\n{line}\n")

    def test_neg_pick_line_arrow_or_lane_wrong_refused(self):
        arrow = {
            "no arrow": f"  {SRC1} {NEW1} N6-14",
            "reversed arrow": f"  {SRC1} <- {NEW1} N6-14",
            "two arrows": f"  {SRC1} -> {NEW1} -> N6-14",
            "arrow before both": f"  -> {SRC1} {NEW1} N6-14",
        }
        for name, line in arrow.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "one -> from source"):
                parse_picks(f"PICKS:\n{line}\n")
        lane = {
            "no lane (real shape)": f"  {SRC1} -> {NEW1}",
            "lowercase lane": f"  {SRC1} -> {NEW1} (n6-14)",
            "two lanes": f"  {SRC1} -> {NEW1} N6-14 N6-13",
            "second uppercase word": f"  {SRC1} -> {NEW1} (N6-14) OK",
        }
        for name, line in lane.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "exactly one lane label"):
                parse_picks(f"PICKS:\n{line}\n")

    def test_neg_pick_lines_orchbot_would_count_are_strict(self):
        bad = {
            "blank line hides a pick": f"PICKS:\n  {SRC1} -> {NEW1} N6-14\n\n  {SRC2} N6-13\n",
            "lane only": "PICKS:\n  N6-14\n",
            "header colon content": f"PICKS: N6-14\n  {SRC1} -> {NEW1} N6-14\n",
        }
        for name, report in bad.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                parse_picks(report)

    def test_neg_duplicate_pick_refused(self):
        for line in (f"  {SRC1} -> {S5} N6-12", f"  {S5} -> {NEW1} N6-12", f"  {S5} -> {S6} N6-14"):
            with self.subTest(line), self.assertRaisesRegex(RefCheckError, "duplicate pick"):
                parse_picks(f"PICKS:\n  {SRC1} -> {NEW1} N6-14\n" + line + "\n")

    def test_neg_pick_patch_id_differs_refused(self):
        git = FakeGit([HEAD, HEAD], {SRC1: PID1, NEW1: PID2, SRC2: PID2, NEW2: PID2})
        with self.assertRaisesRegex(RefCheckError, f"N6-14 {SRC1}->{NEW1}: patch-id differs"):
            _read(git)

    def test_neg_missing_or_malformed_patch_id_raises(self):
        for bad in ("", None, PID1[:39], PID1.upper()):
            git = FakeGit([HEAD, HEAD], {SRC1: PID1, NEW1: bad, SRC2: PID2, NEW2: PID2})
            with self.subTest(bad=bad), self.assertRaisesRegex(RefCheckError, "patch-id of"):
                _read(git)

    def test_neg_abbreviation_resolving_elsewhere_raises(self):
        git = FakeGit([HEAD, HEAD])
        git.shas[NEW1] = "9" * 40
        with self.assertRaisesRegex(RefCheckError, "resolved to a different commit"):
            _read(git)

    def test_neg_pick_reusing_a_commit_refused(self):
        self_pick = GOOD.replace(f"{SRC1} -> {NEW1}", f"{SRC1} -> {SRC1}")  # the source itself: patch-ids trivially equal
        with self.assertRaisesRegex(RefCheckError, "commit reused"):
            _read(FakeGit([HEAD, HEAD]), report=self_pick)
        reused = GOOD.replace(f"{SRC2} -> {NEW2}", f"{NEW1} -> {NEW2}")  # a candidate reused as a source
        with self.assertRaisesRegex(RefCheckError, "commit reused"):
            _read(FakeGit([HEAD, HEAD]), report=reused)

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
            [(p.source, p.new, p.lane) for p in ev.picks], [(SRC1, NEW1, "N6-14"), (SRC2, NEW2, "N6-13")]
        )
        self.assertEqual(accept(git, ev, RUN), HEAD)
        self.assertEqual(git.ref_reads, [])

    def test_ok_crlf_report_accepted(self):
        git = FakeGit([HEAD, HEAD, HEAD])
        self.assertEqual(accept(git, _read(git, report=GOOD.replace("\n", "\r\n")), RUN), HEAD)

    # --- N6-15FIX2 ---------------------------------------------------------------------
    def test_neg_glued_partial_or_suffixed_lane_label_refused(self):
        # F1: one case per variant in the defect class; never trimmed back to a valid lane.
        bad = {
            "glued suffix": "N6-14fix",
            "glued suffix, real lane": "N6-10FIXx",
            "glued prefix": "xN6-14",
            "partial": "N6-",
            "underscore suffix": "N6-14_",
            "single letter": "N",
            "trailing comma": "N6-14,",
            "trailing dot": "(N6-14.)",
            "colon joined": "N6-14:N6-13",
            "slash joined": "N6-14/x",
            "mixed case": "n6-14FIX",
            "non-ascii suffix": "N6-14é",
            "zero-width suffix": "N6-14​",
            "nbsp joined": "N6-14 fix",
            "fullwidth": "Ｎ6-14",
            "cyrillic homoglyph": "Н6-14",
            "titlecase digraph": "ǅ6-14",
            "lane plus glued word": "N6-14 Clean",
        }
        for name, label in bad.items():
            for line in (f"  {SRC1} -> {NEW1} {label}", f"  {label} {SRC1} -> {NEW1}", f"  {SRC1} ({label}) -> {NEW1}"):
                with self.subTest(name, line=line), self.assertRaisesRegex(RefCheckError, "malformed pick line.*lane label"):
                    parse_picks(f"PICKS:\n{line}\n")

    def test_neg_sha_glued_to_non_ascii_refused(self):
        # F2: a letter/digit/mark of any script, or a zero-width / NBSP, glued to a sha.
        for glue in ("é", "١", "д", "́", "‍", " ", "０", "²"):
            for line in (f"  {SRC1}{glue} -> {NEW1} N6-14", f"  {SRC1} -> {glue}{NEW1} N6-14", f"  N6-14 {glue}{SRC1} -> {NEW1}"):
                with self.subTest(glue=hex(ord(glue)), line=line), self.assertRaisesRegex(RefCheckError, "exactly two full 40-hex shas"):
                    parse_picks(f"PICKS:\n{line}\n")

    def test_neg_caller_built_or_altered_evidence_refused_at_accept(self):
        # F3: only sealed read_evidence output; the git port is never consulted for a forgery.
        git = FakeGit([HEAD, HEAD])
        ev = _read(git)
        pick = Pick(SRC1, NEW1, "N6-14")
        forged = {
            "caller built": Evidence(RUN, REF, HEAD, (pick,)),
            "caller built, real picks": Evidence(RUN, REF, HEAD, ev.picks),
            "replace sha": dataclasses.replace(ev, sha=MOVED),
            "replace picks": dataclasses.replace(ev, picks=()),
            "replace nothing": dataclasses.replace(ev),
            "subclass": type("E", (Evidence,), {})(RUN, REF, HEAD, ev.picks),
            "not evidence": (RUN, REF, HEAD, ev.picks),
        }
        for name, fake in forged.items():
            with self.subTest(name), self.assertRaisesRegex(RefCheckError, "not produced by read_evidence"):
                accept(FakeGit([]), fake, RUN)  # pyright: ignore[reportArgumentType]
        stolen = Evidence(RUN, REF, MOVED, ev.picks)
        object.__setattr__(stolen, "seal", ev.seal)
        with self.assertRaisesRegex(RefCheckError, "not produced by read_evidence"):
            accept(FakeGit([]), stolen, RUN)

    def test_neg_accept_with_other_run_id_refused(self):
        ev = _read(FakeGit([HEAD, HEAD]))
        sub = type("S", (str,), {"__eq__": lambda s, o: True, "__ne__": lambda s, o: False, "__hash__": str.__hash__})
        for other in ("run-2026-11-08.2", "RUN-2026-11-08.1", "", None, sub("x")):
            with self.subTest(other=other), self.assertRaisesRegex(RefCheckError, "run_id mismatch"):
                accept(FakeGit([]), ev, other)  # pyright: ignore[reportArgumentType]

    def test_neg_module_runs_no_process_and_opens_no_db(self):
        src = Path(n6_refcheck.__file__).read_text(encoding="utf-8")
        for word in ("sqlite3", "jarvis.db", "subprocess", "open("):
            self.assertNotIn(word, src)

    # --- N6-15FIX3 ---------------------------------------------------------------------
    def test_neg_cdxn615r2_crlf_blank_line_cannot_hide_a_pick(self):
        # F1 (CDXN615R2), verbatim shape: a CRLF blank line used to end the block early.
        for eol in ("\r\n", "\r", "\r\r\n", " \t\r\n"):
            report = f"RUN_ID: r1\r\nPICKS:\r\n  {SRC1} -> {NEW1} N6-12\r\n{eol}  {SRC2} N6-13 garbage\r\n"
            with self.subTest(eol=eol), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                parse_picks(report)
            with self.subTest(eol=eol, via="read_evidence"), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                _read(FakeGit([HEAD, HEAD]), report=report, run_id="r1")

    def test_neg_crlf_pick_after_blank_line_still_patch_id_checked(self):
        report = GOOD.replace(f"\n  {SRC2}", f"\n\n  {SRC2}").replace("\n", "\r\n")
        git = FakeGit([HEAD, HEAD], {SRC1: PID1, NEW1: PID1, SRC2: PID2, NEW2: PID1})
        with self.assertRaisesRegex(RefCheckError, f"N6-13 {SRC2}->{NEW2}: patch-id differs"):
            _read(git, report=report)

    def test_neg_other_line_separators_refused(self):
        # str.splitlines() breaks on these but PICKS_RE does not: at a line start one would
        # end the block and hide an indented-looking pick, so the boundary refuses them all.
        for sep in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            report = GOOD.replace(f"\n  {SRC2}", f"{sep}  {SRC2}")
            with self.subTest(sep=sep), self.assertRaisesRegex(RefCheckError, "line separator other than CR/LF"):
                _read(FakeGit([HEAD, HEAD]), report=report)
            with self.subTest(sep=sep, via="parse_picks"), self.assertRaisesRegex(RefCheckError, "other than CR/LF"):
                parse_picks(report)

    def test_neg_cr_split_second_run_id_line_counted(self):
        report = GOOD.replace("RUN_ID: run-2026-11-08.1\n", "RUN_ID: run-2026-11-08.1\rRUN_ID: run-other\n")
        with self.assertRaisesRegex(RefCheckError, "exactly one RUN_ID"):
            _read(FakeGit([HEAD, HEAD]), report=report)

    # --- N6-15FIX4: the PICKS block is a closed grammar under Unicode blank rules ------
    BLANKS = {
        "NBSP": " ", "BOM": "﻿", "ZWSP": "​", "ZWNJ": "‌", "WJ": "⁠",
        "U+3000": "　", "U+202F": " ", "tab-only": "\t", "CR-only": "\r",
        "mixed": "  ﻿​\t　 ",
    }
    PREFIXES = ("", "  ", "\t", " ", "﻿", "​", "‌", "　")

    def test_neg_cdxn615r3_blank_line_of_any_class_cannot_hide_a_pick(self):
        # F1 (CDXN615R3), verbatim shape: an NBSP/BOM/ZWSP-only line used to end the block.
        for name, blank in self.BLANKS.items():
            report = f"RUN_ID: r1\nPICKS:\n  {SRC1} -> {NEW1} N6-12\n{blank}\n  {SRC2} N6-13 garbage\n"
            with self.subTest(blank=name), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                parse_picks(report)
            with self.subTest(blank=name, via="read_evidence"), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                _read(FakeGit([HEAD, HEAD]), report=report, run_id="r1")

    def test_ok_pick_after_blank_line_of_any_class_is_parsed_and_patch_id_checked(self):
        for name, blank in self.BLANKS.items():
            report = GOOD.replace(f"\n  {SRC2}", f"\n{blank}\n  {SRC2}")
            with self.subTest(blank=name):
                self.assertEqual(parse_picks(report), (Pick(SRC1, NEW1, "N6-14"), Pick(SRC2, NEW2, "N6-13")))
            git = FakeGit([HEAD, HEAD], {SRC1: PID1, NEW1: PID1, SRC2: PID2, NEW2: PID1})
            with self.subTest(blank=name, via="read_evidence"), self.assertRaisesRegex(RefCheckError, "N6-13 .*patch-id differs"):
                _read(git, report=report)

    def test_neg_anything_but_blank_or_pick_inside_the_block_refused(self):
        for name, blank in self.BLANKS.items():
            for junk in ("garbage", "  garbage", "done.", "Note: x", "⠀", "ㅤ", "́", f"​NOTE: {SRC2}"):
                report = GOOD.replace(f"\n  {SRC2}", f"\n{blank}\n{junk}\n  {SRC2}")
                with self.subTest(blank=name, junk=junk), self.assertRaisesRegex(RefCheckError, "malformed pick line"):
                    parse_picks(report)

    def test_neg_line_or_paragraph_separator_line_between_picks_refused(self):
        for sep in (" ", " ", "  "):
            report = GOOD.replace(f"\n  {SRC2}", f"\n{sep}\n  {SRC2}")
            with self.subTest(sep=sep), self.assertRaisesRegex(RefCheckError, "other than CR/LF"):
                parse_picks(report)

    def test_neg_hidden_second_picks_block_refused(self):
        for prefix in self.PREFIXES:
            after = GOOD + f"{prefix}PICKS:\n  {S5} -> {S6} N6-15\n"
            inside = GOOD.replace(f"\n  {SRC2}", f"\n\n{prefix}PICKS more:\n  {SRC2}")
            for where, report in (("after", after), ("inside", inside)):
                with self.subTest(prefix=prefix, where=where), self.assertRaisesRegex(RefCheckError, "exactly one PICKS block, found 2"):
                    parse_picks(report)

    def test_neg_sole_picks_header_behind_a_prefix_refused(self):
        for prefix in self.PREFIXES[1:]:
            with self.subTest(prefix=prefix), self.assertRaisesRegex(RefCheckError, "PICKS header not at line start"):
                parse_picks(GOOD.replace("PICKS old", f"{prefix}PICKS old"))

    def test_neg_hidden_second_run_id_line_counted(self):
        for prefix in self.PREFIXES:
            report = GOOD + f"{prefix}RUN_ID: run-other\n"
            with self.subTest(prefix=prefix), self.assertRaisesRegex(RefCheckError, "exactly one RUN_ID"):
                _read(FakeGit([HEAD, HEAD]), report=report)

    def test_ok_real_int_block_lf_and_crlf_still_parses(self):
        lines = [line for line, _ in REAL_OK.values()]
        want = tuple(Pick(*exp) for _, exp in REAL_OK.values())
        report = "STATUS: CI_PENDING\nPICKS old -> new (all clean, no conflicts):\n" + "\n".join(lines)
        report += "\n\nRESOLVED HUNKS: none\nCI: pending\n"
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=eol):
                self.assertEqual(parse_picks(report.replace("\n", eol)), want)


def _eol(text: object, ends: tuple[str, ...]) -> object:
    """``text`` with its LF line ends replaced by ``ends`` in turn; non-str (and str
    subclasses) unchanged so the type checks see exactly what the base test passes."""
    if type(text) is not str:
        return text
    parts = text.split("\n")
    return parts[0] + "".join(ends[i % len(ends)] + p for i, p in enumerate(parts[1:]))


class _EolVariant(N6RefcheckTests):
    """Every test above, rerun with each report's line ends rewritten: same verdicts."""

    ENDS: tuple[str, ...] = ()

    def setUp(self) -> None:
        super().setUp()
        real_parse, real_read = parse_picks, read_evidence

        def parse(report: object) -> object:
            return real_parse(_eol(report, self.ENDS))  # pyright: ignore[reportArgumentType]

        def read(git: object, ref: object, read_report, run_id: object) -> object:
            return real_read(git, ref, lambda: _eol(read_report(), self.ENDS), run_id)  # pyright: ignore[reportArgumentType]

        for name, fake in (("parse_picks", parse), ("read_evidence", read)):
            patcher = mock.patch(f"{__name__}.{name}", fake)
            patcher.start()
            self.addCleanup(patcher.stop)


class N6RefcheckCrlfTests(_EolVariant):
    ENDS = ("\r\n",)


class N6RefcheckCrTests(_EolVariant):
    ENDS = ("\r",)


class N6RefcheckMixedEolTests(_EolVariant):
    ENDS = ("\r\n", "\r", "\n")


del _EolVariant  # the base runs only through its subclasses


if __name__ == "__main__":
    unittest.main()
