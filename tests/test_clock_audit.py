"""TESTINFRA: a test reads the wall clock only through an injected clock. Static audit of tests/:
a line calling the real clock is counted per file against ALLOWED, the offenders at the time this
audit landed. The list may only shrink; a new offender (or one more line in a listed file) fails."""
import re
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# Built from pieces so this file never matches itself.
CALLS = re.compile("|".join(re.escape(name + "(") for name in ("datetime.now", "date.today", "time" + ".time")))

#: file -> lines calling the real clock. Only lower a number or drop a file; never add or raise.
ALLOWED = {
    "test_app.py": 1,
    "test_billing_integrity.py": 2,
    "test_conveyor.py": 1,
    "test_convui.py": 1,
    "test_dfsingle.py": 2,
    "test_egress_attestation.py": 2,
    "test_execution_journal.py": 1,
    "test_g6k6.py": 1,
    "test_g6mp1.py": 3,
    "test_g6sb1_sidebar.py": 2,
    "test_g6u1_usage.py": 1,
    "test_g6uipol.py": 1,
    "test_indicators_6g.py": 2,
    "test_intent.py": 5,
    "test_memory_doctor_cli.py": 1,
    "test_memory_doctor_e2e.py": 1,
    "test_memory_doctor_store.py": 1,
    "test_memory_guard_store.py": 1,
    "test_model_list_newest.py": 4,
    "test_permissions.py": 1,
    "test_projects_ui.py": 1,
    "test_projects_ui_6f.py": 8,
    "test_schedules.py": 1,
    "test_ui.py": 7,
}


class ClockAuditTests(unittest.TestCase):
    def test_no_new_wall_clock_reads(self):
        over = []
        for path in sorted(TESTS.rglob("*.py")):
            name = path.relative_to(TESTS).as_posix()
            lines = [n for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if CALLS.search(line)]
            if len(lines) > ALLOWED.get(name, 0):
                over.append(f"{name}: {len(lines)} lines (allowed {ALLOWED.get(name, 0)}) at {lines}")
        self.assertEqual(over, [], "inject a clock instead of reading the wall clock:\n" + "\n".join(over))

    def test_the_pattern_finds_each_call(self):
        for name in ("datetime.now", "date.today", "time" + ".time"):
            self.assertRegex("x = " + name + "()", CALLS)
        self.assertNotRegex("time.monotonic()", CALLS)


if __name__ == "__main__":
    unittest.main()
