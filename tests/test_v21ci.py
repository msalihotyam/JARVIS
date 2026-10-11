"""V2.1 C-5: CI's aggregate (scripts/test_parallel.py --aggregate) fails on any skip not on
ALLOWED_SKIPS and prints every skip with its reason; local runs are unchanged.

Synthetic -v output and synthetic shard reports in a temp directory; discovery is
replaced by a two-test fake, so no test module is imported and nothing runs.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "test_parallel", Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py")
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

POSIX = "tests.test_local_file_write.ContainmentTests.test_the_posix_parent_walk_is_no_follow_at_every_step"
LINGUIST = "tests.test_ui.LocalizationTests.test_turkish_catalog_covers_every_marked_string"
GWS = "the Google Workspace CLI is not installed here"
IDS = ["tests.test_fake.C.test_a", "tests.test_fake.C.test_b"]
UNKNOWN = "tests.test_conversation_store.RollbackTests.test_schema_10_release_reads_it"


class SkipParsingTests(unittest.TestCase):
    def test_every_skip_line_gives_its_id_and_reason(self) -> None:
        output = (
            "test_a (tests.m.C.test_a) ... ok\n"
            "test_b (tests.m.C.test_b)\nA docstring line. ... skipped 'no directory-handle traversal'\n"
            "test_c (tests.m.D.test_c) ... skipped \"it's not here\"\n"
            "\nRan 3 tests in 0.1s\n\nOK (skipped=2)\n")
        result = tp.Result(None, [], 0, output, 0.1)
        self.assertEqual(2, result.skipped)
        self.assertEqual([["tests.m.C.test_b", "no directory-handle traversal"],
                          ["tests.m.D.test_c", "it's not here"]], result.skips)

    def test_the_allowlist_names_todays_skips_with_their_reasons(self) -> None:
        self.assertEqual({
            POSIX: "no directory-handle traversal",
            LINGUIST: "the PySide6 Linguist tools are not installed",
            "tests.test_gmail_minimum.LiveProbeTests.test_the_installed_cli_can_read_a_held_credential_but_not_remove_it":
                GWS,
            "tests.test_gmail_minimum.LiveProbeTests.test_the_installed_cli_refuses_an_empty_dedicated_directory":
                GWS,
            "tests.test_gmail_minimum.LiveProbeTests.test_the_installed_cli_rehearses_both_writes_as_bodiless_multipart_uploads":
                GWS,
        }, tp.ALLOWED_SKIPS)


class AggregateSkipGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        discovery = {"modules": {"test_fake": {"split": True, "classes": {"C": IDS}}}, "serial": IDS, "errors": []}
        for target, value in (("discover", lambda selected: ["test_fake"]), ("ui_modules", lambda names: set()),
                              ("discover_tests", lambda python, names, serial: discovery),
                              ("load_durations", lambda *a, **k: {})):
            patcher = mock.patch.object(tp, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def aggregate(self, skips, skipped=None) -> tuple[int, str]:
        report = {"shard": [1, 1], "green": True, "wall": 1.0, "units": ["tests.test_fake"], "dispatched": IDS,
                  "ran": 2, "skipped": len(skips) if skipped is None else skipped, "failures": 0, "errors": 0,
                  "accounted": True, "rule_d": [], "durations": [], "discovered": 2}
        if skips is not None:
            report["skips"] = skips
        (self.dir / "shard-report.json").write_text(json.dumps(report), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tp.aggregate(argparse.Namespace(aggregate=str(self.dir), shards=1, python="python",
                                                   durations=0, write_durations=""))
        return code, out.getvalue()

    def test_no_skip_is_green(self) -> None:
        code, out = self.aggregate([])
        self.assertEqual(0, code, out)
        self.assertNotIn("skipped:", out)

    def test_the_allowlisted_skips_pass_and_are_printed_with_their_reasons(self) -> None:
        code, out = self.aggregate([[POSIX, tp.ALLOWED_SKIPS[POSIX]], [LINGUIST, tp.ALLOWED_SKIPS[LINGUIST]]])
        self.assertEqual(0, code, out)
        self.assertIn(f"  skipped: {POSIX}: no directory-handle traversal", out)
        self.assertIn(f"  skipped: {LINGUIST}: the PySide6 Linguist tools are not installed", out)
        self.assertEqual("OK", out.strip().splitlines()[-1])

    def test_an_unknown_skip_fails_and_is_printed(self) -> None:
        code, out = self.aggregate([[POSIX, tp.ALLOWED_SKIPS[POSIX]], [UNKNOWN, "the rollback release's commit is not in this clone"]])
        self.assertEqual(1, code, out)
        self.assertIn(f"  skipped (NOT ALLOWED): {UNKNOWN}: the rollback release's commit is not in this clone", out)
        self.assertIn(f"PROBLEM: shard 1/1: {UNKNOWN} skipped", out)
        self.assertEqual("FAILED", out.strip().splitlines()[-1])

    def test_an_allowlisted_test_skipping_for_another_reason_fails(self) -> None:
        code, out = self.aggregate([[LINGUIST, "something else"]])
        self.assertEqual(1, code, out)
        self.assertIn(f"  skipped (NOT ALLOWED): {LINGUIST}: something else", out)

    def test_skips_that_cannot_all_be_named_fail(self) -> None:
        # No "skips" in the report (an older shard), or fewer named than the count says.
        for skips, skipped in ((None, 1), ([[POSIX, tp.ALLOWED_SKIPS[POSIX]]], 2)):
            with self.subTest(skips=skips):
                code, out = self.aggregate(skips, skipped)
                self.assertEqual(1, code, out)
                self.assertIn(f"PROBLEM: shard 1/1: {skipped} skipped, but", out)
                if skips:                       # the ones it could name are still printed
                    self.assertIn(f"  skipped: {POSIX}: no directory-handle traversal", out)


if __name__ == "__main__":
    unittest.main()
