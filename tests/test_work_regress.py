"""Inline captured runner output; no runner processes are started.

UNITTEST, UNITTEST_CLEAN, NODE and NODE_NESTED are real output captured from
throwaway suites (Python 3 unittest, node v24.14.0 --test-reporter=tap).
PYTEST and JEST are SYNTHETIC: neither runner was installed offline.
"""

import unittest

from jarvis.work_regress import TestResult, compare_results, fixture_list_hash, parse_output


# Real: ok, expectedFailure, failure, two subTests (one failing), skip.
UNITTEST = r"""F.sFx
======================================================================
FAIL: test_fail (test_u.T.test_fail)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\realk\.claude\jobs\aa424da9\tmp\cap\test_u.py", line 6, in test_fail
    def test_fail(self): self.assertEqual(3, 4)
                         ~~~~~~~~~~~~~~~~^^^^^^
AssertionError: 3 != 4

======================================================================
FAIL: test_sub (test_u.T.test_sub) (i=2)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\realk\.claude\jobs\aa424da9\tmp\cap\test_u.py", line 9, in test_sub
    with self.subTest(i=i): self.assertEqual(i, 1)
                            ~~~~~~~~~~~~~~~~^^^^^^
AssertionError: 2 != 1

----------------------------------------------------------------------
Ran 5 tests in 0.001s

FAILED (failures=2, skipped=1, expected failures=1)
"""
# Real: ok plus expectedFailure.
UNITTEST_CLEAN = """.x
----------------------------------------------------------------------
Ran 2 tests in 0.001s

OK (expected failures=1)
"""
# Real: describe('outer') { it passes; describe('inner') { it fails } },
# a todo test that throws, a skipped test and a failing top-level test.
NODE = r"""TAP version 13
# Subtest: outer
    # Subtest: passes
    ok 1 - passes
      ---
      duration_ms: 0.4011
      type: 'test'
      ...
    # Subtest: inner
        # Subtest: fails
        not ok 1 - fails
          ---
          duration_ms: 0.6641
          type: 'test'
          location: 'C:\\Users\\realk\\.claude\\jobs\\aa424da9\\tmp\\cap\\n.test.mjs:5:29'
          failureType: 'testCodeFailure'
          error: |-
            Expected values to be strictly equal:

            1 !== 2

          code: 'ERR_ASSERTION'
          name: 'AssertionError'
          expected: 2
          actual: 1
          operator: 'strictEqual'
          stack: |-
            TestContext.<anonymous> (file:///C:/Users/realk/.claude/jobs/aa424da9/tmp/cap/n.test.mjs:5:56)
            Test.runInAsyncScope (node:async_hooks:228:14)
            Test.run (node:internal/test_runner/test:1118:25)
            Test.start (node:internal/test_runner/test:1015:17)
            node:internal/test_runner/test:1531:71
            node:internal/per_context/primordials:466:82
            new Promise (<anonymous>)
            new SafePromise (node:internal/per_context/primordials:435:3)
            node:internal/per_context/primordials:466:9
            Array.map (<anonymous>)
          ...
        1..1
    not ok 2 - inner
      ---
      duration_ms: 1.4767
      type: 'suite'
      location: 'C:\\Users\\realk\\.claude\\jobs\\aa424da9\\tmp\\cap\\n.test.mjs:5:3'
      failureType: 'subtestsFailed'
      error: '1 subtest failed'
      code: 'ERR_TEST_FAILURE'
      ...
    1..2
not ok 1 - outer
  ---
  duration_ms: 2.5529
  type: 'suite'
  location: 'C:\\Users\\realk\\.claude\\jobs\\aa424da9\\tmp\\cap\\n.test.mjs:3:1'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  code: 'ERR_TEST_FAILURE'
  ...
# Subtest: todo case
not ok 2 - todo case # TODO
  ---
  duration_ms: 0.2112
  type: 'test'
  location: 'C:\\Users\\realk\\.claude\\jobs\\aa424da9\\tmp\\cap\\n.test.mjs:7:1'
  failureType: 'testCodeFailure'
  error: 'x'
  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  operator: 'fail'
  stack: |-
    TestContext.<anonymous> (file:///C:/Users/realk/.claude/jobs/aa424da9/tmp/cap/n.test.mjs:7:50)
    Test.runInAsyncScope (node:async_hooks:228:14)
    Test.run (node:internal/test_runner/test:1118:25)
    Test.processPendingSubtests (node:internal/test_runner/test:787:18)
    Suite.postRun (node:internal/test_runner/test:1247:19)
    Suite.run (node:internal/test_runner/test:1548:10)
    async startSubtestAfterBootstrap (node:internal/test_runner/harness:358:3)
  ...
# Subtest: skip case
ok 3 - skip case # SKIP
  ---
  duration_ms: 0.0642
  type: 'test'
  ...
# Subtest: top fail
not ok 4 - top fail
  ---
  duration_ms: 2.9571
  type: 'test'
  location: 'C:\\Users\\realk\\.claude\\jobs\\aa424da9\\tmp\\cap\\n.test.mjs:9:1'
  failureType: 'testCodeFailure'
  error: |-
    The expression evaluated to a falsy value:

      assert.ok(false)

  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  expected: true
  actual: false
  operator: '=='
  stack: |-
    TestContext.<anonymous> (file:///C:/Users/realk/.claude/jobs/aa424da9/tmp/cap/n.test.mjs:9:33)
    Test.runInAsyncScope (node:async_hooks:228:14)
    Test.run (node:internal/test_runner/test:1118:25)
    Test.processPendingSubtests (node:internal/test_runner/test:787:18)
    Test.postRun (node:internal/test_runner/test:1247:19)
    Test.run (node:internal/test_runner/test:1175:12)
    async Test.processPendingSubtests (node:internal/test_runner/test:787:7)
  ...
1..4
# tests 5
# suites 2
# pass 1
# fail 2
# cancelled 0
# skipped 1
# todo 1
# duration_ms 79.2712
"""
# Real (location/stack lines removed): test('parent') with t.test children.
# Unlike describe(), a failing parent test IS counted in "# fail".
NODE_NESTED = """TAP version 13
# Subtest: parent
    # Subtest: child ok
    ok 1 - child ok
      ---
      type: 'test'
      ...
    # Subtest: child bad
    not ok 2 - child bad
      ---
      type: 'test'
      failureType: 'testCodeFailure'
      error: |-
        The expression evaluated to a falsy value:

          assert.ok(false)

      ...
    1..2
not ok 1 - parent
  ---
  type: 'test'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  ...
1..1
# tests 3
# suites 0
# pass 1
# fail 2
# cancelled 0
# skipped 0
# todo 0
# duration_ms 86.1735
"""
# SYNTHETIC (pytest not installed): failure, xfail, non-strict XPASS (no
# FAILED line) and a parametrized id containing a space.
PYTEST = """============================= test session starts =============================
collected 6 items

test_p.py .FxXF.                                                         [100%]

=========================== short test summary info ===========================
FAILED test_p.py::test_fail - assert 3 == 4
FAILED test_p.py::test_param[a b] - AssertionError: assert 'a b' == 'c'
=============== 2 failed, 2 passed, 1 xfailed, 1 xpassed in 0.05s ===============
"""
# SYNTHETIC (jest not installed): colored " FAIL " badge, a console.log
# block, two failing suites and the repeated multi-suite failure summary.
_FAIL = "\x1b[0m\x1b[7m\x1b[1m\x1b[31m FAIL \x1b[39m\x1b[22m\x1b[27m\x1b[0m"
JEST = f"""\x1b[1m● \x1b[22mValidation Warning:

  Unknown option "foo" with value 1 was found.

{_FAIL} \x1b[2mtests/\x1b[22m\x1b[1mmath.test.js\x1b[22m
  \x1b[1m● \x1b[22mConsole

    console.log
      FAIL bogus.js
      ● not a failure

      at Object.log (tests/math.test.js:3:11)

\x1b[1m\x1b[31m  \x1b[1m● \x1b[22m\x1b[1marithmetic › adds numbers\x1b[39m\x1b[22m

    expect(received).toBe(expected) // Object.is equality

\x1b[0m\x1b[7m\x1b[1m\x1b[32m PASS \x1b[39m\x1b[22m\x1b[27m\x1b[0m tests/io.test.js
{_FAIL} tests/str.test.js (5.123 s)
  ● trims

    expect(received).toBe(expected)

\x1b[1mSummary of all failing tests\x1b[22m
{_FAIL} tests/math.test.js
  ● arithmetic › adds numbers

{_FAIL} tests/str.test.js (5.123 s)
  ● trims


Test Suites: 2 failed, 1 passed, 3 total
Tests:       2 failed, 3 passed, 5 total
Snapshots:   0 total
Time:        6.021 s
Ran all test suites.
"""


class ParsingTests(unittest.TestCase):
    def test_unittest(self):
        self.assertEqual(parse_output(UNITTEST, "unittest"), TestResult(5, {
            "test_fail (test_u.T.test_fail)",
            "test_sub (test_u.T.test_sub) (i=2)",
        }))

    def test_unittest_expected_failure_is_ok(self):
        self.assertEqual(parse_output(UNITTEST_CLEAN, "unittest"), TestResult(2, frozenset()))

    def test_pytest(self):
        self.assertEqual(parse_output(PYTEST, "pytest"), TestResult(6, {
            "test_p.py::test_fail", "test_p.py::test_param[a b]"}))

    def test_node_describe_suites_todo_and_skip(self):
        self.assertEqual(parse_output(NODE, "node"),
                         TestResult(5, {"outer > inner > fails", "top fail"}))

    def test_node_parent_test_counts(self):
        self.assertEqual(parse_output(NODE_NESTED, "node"),
                         TestResult(3, {"parent > child bad", "parent"}))

    def test_jest_colored_multi_suite(self):
        self.assertEqual(parse_output(JEST, "jest"), TestResult(5, {
            "tests/math.test.js::arithmetic › adds numbers", "tests/str.test.js::trims"}))

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
        samples = [(UNITTEST.replace("failures=2,", "failures=3,"), "unittest"),
                   (PYTEST.replace("FAILED test_p.py::test_fail - assert 3 == 4", ""), "pytest"),
                   (NODE.replace("# fail 2", "# fail 3"), "node"),
                   (JEST.replace("  ● trims\n\n    expect", "\n    expect"), "jest")]
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

    def test_pytest_param_dash_and_captured_log(self):
        output = ("FAILED t.py::test[a - b] - assert 1\nERROR    root:t.py:5 boom\n"
                  "FAILED t.py::test[a - c]\n2 failed in 0.03s\n")
        self.assertEqual(parse_output(output, "pytest"),
                         TestResult(2, {"t.py::test[a - b]", "t.py::test[a - c]"}))

    def test_pytest_strict_xpass_is_a_failure(self):
        output = "FAILED test_p.py::test_xp\n1 failed, 1 passed in 0.03s\n"
        self.assertEqual(parse_output(output, "pytest"), TestResult(2, {"test_p.py::test_xp"}))

    def test_cancelled_node_tests(self):
        with self.assertRaises(ValueError):
            parse_output(NODE.replace("# cancelled 0", "# cancelled 1"), "node")

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
