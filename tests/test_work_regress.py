"""Inline captured runner output excerpts; no runner processes are started."""

import unittest

from jarvis.work_regress import TestResult, compare_results, fixture_list_hash, parse_output


UNITTEST = """.FE
======================================================================
FAIL: test_add (tests.test_math.MathTests.test_add)
----------------------------------------------------------------------
AssertionError: 3 != 4
======================================================================
ERROR: test_read (tests.test_io.IOTests.test_read)
----------------------------------------------------------------------
FileNotFoundError: missing
----------------------------------------------------------------------
Ran 3 tests in 0.002s

FAILED (failures=1, errors=1)
"""
PYTEST = """============================= test session starts ==============================
tests/test_math.py .F                                                 [100%]
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add - assert 3 == 4
==================== 1 failed, 1 passed, 1 skipped in 0.03s =====================
"""
NODE = """TAP version 13
# Subtest: adds numbers
ok 1 - adds numbers
# Subtest: rejects invalid input
not ok 2 - rejects invalid input
  ---
  duration_ms: 1.2
  failureType: 'testCodeFailure'
  error: 'Missing expected exception.'
  ...
1..2
# tests 2
# suites 0
# pass 1
# fail 1
# cancelled 0
# skipped 0
# todo 0
# duration_ms 54.132
"""
JEST = """FAIL tests/math.test.js
  ● arithmetic › adds numbers

    expect(received).toBe(expected) // Object.is equality

    Expected: 4
    Received: 3

PASS tests/io.test.js
Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 2 passed, 3 total
Snapshots:   0 total
Time:        0.521 s
Ran all test suites.
"""


class ParsingTests(unittest.TestCase):
    def test_unittest(self):
        result = parse_output(UNITTEST, "unittest")
        self.assertEqual(result.total, 3)
        self.assertEqual(result.failed_ids, {
            "test_add (tests.test_math.MathTests.test_add)",
            "test_read (tests.test_io.IOTests.test_read)",
        })

    def test_pytest(self):
        self.assertEqual(parse_output(PYTEST, "pytest"), TestResult(3, {"tests/test_math.py::test_add"}))

    def test_node(self):
        self.assertEqual(parse_output(NODE, "node"), TestResult(2, {"rejects invalid input"}))

    def test_jest(self):
        self.assertEqual(parse_output(JEST, "jest"),
                         TestResult(3, {"tests/math.test.js::arithmetic › adds numbers"}))

    def test_clean_runs(self):
        samples = {
            "unittest": "Ran 2 tests in 0.001s\n\nOK (skipped=1)\n",
            "pytest": "==================== 2 passed in 0.01s ====================\n",
            "node": "# tests 2\n# fail 0\n# duration_ms 1.2\n",
            "jest": "Test Suites: 1 passed, 1 total\nTests: 2 passed, 2 total\n",
        }
        for runner, output in samples.items():
            with self.subTest(runner=runner):
                self.assertEqual(parse_output(output, runner), TestResult(2, frozenset()))

    def test_color_and_windows_newlines(self):
        self.assertEqual(parse_output("\x1b[31m" + PYTEST.replace("\n", "\r\n") + "\x1b[0m", "pytest"),
                         parse_output(PYTEST, "pytest"))

    def test_missing_or_duplicate_summary(self):
        for output in ("", "Ran 2 tests in 0.01s\n", UNITTEST + UNITTEST):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_output(output, "unittest")

    def test_unidentified_failures_rejected(self):
        samples = [(UNITTEST.replace("failures=1", "failures=2"), "unittest"),
                   (PYTEST.replace("FAILED tests/test_math.py::test_add - assert 3 == 4", ""), "pytest"),
                   (NODE.replace("# fail 1", "# fail 2"), "node"),
                   (JEST.replace("  ● arithmetic › adds numbers", ""), "jest")]
        for output, runner in samples:
            with self.subTest(runner=runner), self.assertRaises(ValueError):
                parse_output(output, runner)

    def test_jest_suite_crash(self):
        with self.assertRaises(ValueError):
            parse_output("Test Suites: 1 failed, 1 total\nTests: 0 total\n", "jest")

    def test_unknown_runner(self):
        with self.assertRaises(ValueError):
            parse_output(UNITTEST, "other")

    def test_pytest_collection_error(self):
        output = "ERROR tests/test_io.py - ImportError\n==================== 1 error in 0.1s ====================\n"
        self.assertEqual(parse_output(output, "pytest"), TestResult(1, {"tests/test_io.py"}))

    def test_quiet_pytest(self):
        output = "FAILED tests/test_math.py::test_add - assert 3 == 4\n1 failed, 1 passed in 0.03s\n"
        self.assertEqual(parse_output(output, "pytest"), TestResult(2, {"tests/test_math.py::test_add"}))

    def test_cancelled_node_tests(self):
        with self.assertRaises(ValueError):
            parse_output(NODE.replace("# cancelled 0", "# cancelled 1"), "node")

    def test_nested_node_failure_identity(self):
        output = "# Subtest: suite\n    # Subtest: child\n    not ok 1 - child\nnot ok 1 - suite\n# tests 1\n# fail 2\n# duration_ms 1\n"
        # Parent failure counts can exceed leaf test totals; refuse rather
        # than accepting ambiguous container failures as comparable tests.
        with self.assertRaises(ValueError):
            parse_output(output, "node")

    def test_unexpected_success_rejected(self):
        with self.assertRaises(ValueError):
            parse_output("Ran 1 test in 0.01s\nFAILED (unexpected successes=1)\n", "unittest")


class GateTests(unittest.TestCase):
    def verdict(self, base, stage, fixtures=None):
        return compare_results(base, stage, locked_fixture_hash="a" * 64,
                               stage_fixture_hash=fixtures or "a" * 64)

    def test_preexisting_failure_passes(self):
        result = parse_output(UNITTEST, "unittest")
        self.assertTrue(self.verdict(result, result).passed)

    def test_new_failure_fails_even_with_same_failure_count(self):
        result = self.verdict(TestResult(3, {"old"}), TestResult(3, {"new"}))
        self.assertFalse(result.passed)
        self.assertEqual(result.new_failures, {"new"})
        self.assertEqual(result.reasons, ("new_failures",))

    def test_fixed_failure_and_increased_total_pass(self):
        self.assertTrue(self.verdict(TestResult(2, {"old"}), TestResult(3, set())).passed)

    def test_dropped_total_fails(self):
        self.assertEqual(self.verdict(TestResult(3, set()), TestResult(2, set())).reasons,
                         ("test_count_dropped",))

    def test_fixture_change_fails(self):
        self.assertEqual(self.verdict(TestResult(2, set()), TestResult(2, set()), "b" * 64).reasons,
                         ("locked_fixtures_changed",))

    def test_all_reasons_retained(self):
        result = self.verdict(TestResult(3, set()), TestResult(2, {"new"}), "b" * 64)
        self.assertEqual(len(result.reasons), 3)

    def test_manifest_is_order_independent_and_content_bound(self):
        a = {"a.py": "a" * 64, "b.py": "b" * 64}
        self.assertEqual(fixture_list_hash(a), fixture_list_hash(dict(reversed(list(a.items())))))
        for changed in ({"a.py": "b" * 64, "b.py": "b" * 64}, {"a.py": "a" * 64}, {}):
            self.assertNotEqual(fixture_list_hash(a), fixture_list_hash(changed))

    def test_invalid_evidence_rejected(self):
        with self.assertRaises(ValueError):
            fixture_list_hash({"a.py": ""})
        with self.assertRaises(ValueError):
            compare_results(TestResult(0, set()), TestResult(0, set()),
                            locked_fixture_hash="", stage_fixture_hash="")
        for total in (-1, True, 1.5):
            with self.subTest(total=total), self.assertRaises(ValueError):
                TestResult(total, set())


if __name__ == "__main__":
    unittest.main()
