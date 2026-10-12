"""LOADLIST2: rule D in scripts/test_parallel.py takes ``module.Class.test`` ids of any tests module
from the "Load-sensitive list" item of JARVIS_ACTIVE_STATE.md, and only from that item. No test
module runs: the state file is a temp copy and the rule-D rerun is a fake subprocess."""

import argparse
import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "test_parallel", Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py")
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

STATE = """# State

Notes mention `test_ui.Elsewhere.test_before_the_list` here.

## Decisions

- Load-sensitive list (RUNBOOK rule D; only the orchestrator edits):
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b`,
  `test_dfreply.LongMarkdownTests.test_a_50000_character_markdown_reply_renders_whole`
  (reason), `test_bg_run_6g` (whole module; `test_t4_alone` named in the reason).
- Next bullet names `test_dfwin.Other.test_after_the_list` and is not listed.

## Next
`test_ui.Later.test_in_another_section`
"""


def failed_run(test_id: str):
    module, cls, name = test_id.split(".")
    output = (f"FAIL: {name} (tests.{test_id})\n" + "-" * 70 + "\nAssertionError: slow\n\n"
              + "-" * 70 + "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n")
    return tp.Result(None, [sys.executable, "-m", "unittest", f"tests.{module}"], 1, output, 0.1)


class LoadSensitiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name) / "JARVIS_ACTIVE_STATE.md"
        self.state.write_text(STATE, encoding="utf-8")
        patch = mock.patch.object(tp, "STATE", self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_full_ids_inside_the_list_item_count_for_any_module(self):
        self.assertEqual({"test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b",
                          "test_dfreply.LongMarkdownTests.test_a_50000_character_markdown_reply_renders_whole"},
                         tp.load_sensitive())

    def test_no_list_item_or_no_file_lists_nothing(self):
        self.state.write_text(STATE.replace("Load-sensitive list", "Other list"), encoding="utf-8")
        self.assertEqual(set(), tp.load_sensitive())
        self.state.unlink()
        self.assertEqual(set(), tp.load_sensitive())

    def report(self, test_id: str, returncode: int = 0):
        rule_d = []
        done = subprocess.CompletedProcess([], returncode, "", "")
        with mock.patch.object(tp.subprocess, "run", return_value=done) as run, \
                mock.patch.object(tp, "worker_env", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            green = tp._report_failures([failed_run(test_id)], argparse.Namespace(no_rerun=False, python="py"),
                                        [], rule_d)
        return green, run, rule_d

    def test_a_listed_test_of_another_module_is_rerun_alone_from_its_own_module(self):
        test_id = "test_dfreply.LongMarkdownTests.test_a_50000_character_markdown_reply_renders_whole"
        green, run, rule_d = self.report(test_id)
        self.assertTrue(green)
        self.assertEqual(["py", "-m", "unittest", f"tests.{test_id}"], run.call_args.args[0])
        self.assertEqual([{"test": test_id, "alone": "PASS"}], rule_d)
        self.assertFalse(self.report(test_id, returncode=1)[0])   # fails alone too: red

    def test_a_listed_test_ui_id_still_qualifies(self):
        green, run, _ = self.report("test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b")
        self.assertTrue(green)
        self.assertEqual(1, run.call_count)

    def test_an_unlisted_or_merely_mentioned_test_blocks_without_a_rerun(self):
        for test_id in ("test_dfwin.Other.test_after_the_list", "test_ui.Elsewhere.test_before_the_list",
                        "test_dfreply.RailSwitchTests.test_switching_cards_closes_a_then_opens_b"):
            with self.subTest(test_id=test_id):
                green, run, rule_d = self.report(test_id)
                self.assertFalse(green)
                run.assert_not_called()
                self.assertEqual([], rule_d)


class RealStateListTests(unittest.TestCase):
    def test_the_state_file_lists_the_timing_tests_of_other_modules(self):
        listed = tp.load_sensitive()
        for test_id in ("test_browser_lifecycle.FailClosedTests.test_a_refusal_the_browser_will_not_acknowledge_destroys_the_tab",
                        "test_dfreply.LongMarkdownTests.test_a_50000_character_markdown_reply_renders_whole",
                        "test_dfwin.WindowPowerV2Tests.test_the_power_up_ignites_the_core_then_unfolds_the_content",
                        "test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b"):
            self.assertIn(test_id, listed)


if __name__ == "__main__":
    unittest.main()
