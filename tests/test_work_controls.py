"""Standalone control tests with fixed caller-owned timestamps."""

import unittest
from dataclasses import FrozenInstanceError

from jarvis.work_controls import (
    PHASES, REVALIDATION_ORDER, Budget, RunControl, Usage, budget_for,
    budget_verdict, cancel, consume, pause, resume, revision_verdict,
    watchdog_verdict,
)


class BudgetTests(unittest.TestCase):
    def test_d3_defaults(self):
        self.assertEqual(budget_for("small", model_calls=50), Budget(10, 7200, 50))
        self.assertEqual(budget_for("medium", model_calls=80), Budget(30, 28800, 80))

    def test_large_requires_explicit_plan(self):
        cap = Budget(40, 40000, 90)
        self.assertEqual(budget_for("large", model_calls=90, large=cap), cap)
        for size in ("large", "unknown"):
            with self.assertRaises(ValueError):
                budget_for(size, model_calls=90)
        with self.assertRaises(ValueError):
            budget_for("small", model_calls=90, large=cap)
        with self.assertRaises(ValueError):
            budget_for("large", model_calls=91, large=cap)

    def test_every_cap_at_and_beyond_boundary(self):
        for size, sessions, seconds in (("small", 10, 7200), ("medium", 30, 28800)):
            cap = budget_for(size, model_calls=100)
            for usage, elapsed, expected in (
                (Usage(sessions - 1, 99), seconds - 1, "continue"),
                (Usage(sessions, 0), 0, "blocked:budget"),
                (Usage(sessions + 1, 0), 0, "blocked:budget"),
                (Usage(), seconds, "blocked:budget"),
                (Usage(), seconds + 1, "blocked:budget"),
                (Usage(0, 100), 0, "blocked:budget"),
                (Usage(0, 101), 0, "blocked:budget"),
            ):
                with self.subTest(size=size, usage=usage, elapsed=elapsed):
                    self.assertEqual(budget_verdict(
                        cap, usage, started_at=100, now=100 + elapsed), expected)

    def test_quota_waits(self):
        self.assertEqual(budget_verdict(Budget(10, 100, 10), Usage(),
                         started_at=0, now=1, quota_available=False), "blocked:budget")

    def test_consumption_is_immutable_and_monotonic(self):
        original = Usage(2, 4)
        self.assertEqual(consume(original, sessions=3, model_calls=5), Usage(5, 9))
        self.assertEqual(original, Usage(2, 4))
        with self.assertRaises(FrozenInstanceError):
            original.sessions = 0
        with self.assertRaises(ValueError):
            consume(original, sessions=-1)

    def test_invalid_counters_and_clocks(self):
        for value in (-1, True, 1.5, "1"):
            for factory in (lambda: Usage(value), lambda: Budget(1, 10, value)):
                with self.assertRaises(ValueError):
                    factory()
        for value in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                Budget(1, value, 1)
        with self.assertRaises(ValueError):
            budget_verdict(Budget(1, 1, 1), Usage(), started_at=2, now=1)

    def test_zero_budget_blocks(self):
        for cap in (Budget(0, 10, 10), Budget(10, 0, 10), Budget(10, 10, 0)):
            self.assertEqual(budget_verdict(cap, Usage(), started_at=0, now=0),
                             "blocked:budget")


class ControlTests(unittest.TestCase):
    def resume_run(self, run, **changes):
        args = dict(explicit=True, revalidated=REVALIDATION_ORDER,
                    budget_status="continue")
        args.update(changes)
        return resume(run, **args)

    def test_pause_resume_preserves_each_phase(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                run = RunControl(phase)
                paused = pause(run)
                self.assertEqual(paused, RunControl("interrupted", phase))
                self.assertEqual(pause(paused), paused)
                self.assertEqual(self.resume_run(paused), run)

    def test_resume_requires_explicit_ordered_complete_checks(self):
        run = pause(RunControl("building"))
        with self.assertRaises(ValueError):
            self.resume_run(run, explicit=False)
        for checks in ((), REVALIDATION_ORDER[::-1], REVALIDATION_ORDER + ("extra",)):
            with self.assertRaises(ValueError):
                self.resume_run(run, revalidated=checks)
        for index in range(5):
            with self.assertRaises(ValueError):
                self.resume_run(run, revalidated=REVALIDATION_ORDER[:index])

    def test_budget_block_survives_resume_until_extended(self):
        run = pause(RunControl("accepting"))
        blocked = self.resume_run(run, budget_status="blocked:budget")
        self.assertEqual(blocked, RunControl("blocked:budget", "accepting"))
        self.assertEqual(pause(blocked), blocked)
        self.assertEqual(self.resume_run(blocked), RunControl("accepting"))
        with self.assertRaises(ValueError):
            self.resume_run(run, budget_status="unknown")

    def test_cancel_active_paused_and_budget_blocked(self):
        for run in (RunControl("planning"), pause(RunControl("building")),
                    RunControl("blocked:budget", "building"), RunControl("interrupted")):
            self.assertEqual(cancel(run), RunControl("cancelled"))
        self.assertEqual(cancel(RunControl("cancelled")), RunControl("cancelled"))

    def test_terminal_controls_refused(self):
        for state in ("done", "failed", "cancelled"):
            with self.assertRaises(ValueError):
                pause(RunControl(state))
            with self.assertRaises(ValueError):
                self.resume_run(RunControl(state))
        for state in ("done", "failed"):
            with self.assertRaises(ValueError):
                cancel(RunControl(state))

    def test_invalid_and_missing_resume_phase(self):
        for args in (("unknown",), ("building", "planning"), ("interrupted", "done")):
            with self.assertRaises(ValueError):
                RunControl(*args)
        for run in (RunControl("interrupted"), RunControl("building")):
            with self.assertRaises(ValueError):
                self.resume_run(run)


class RevisionAndWatchdogTests(unittest.TestCase):
    def test_five_revisions_and_three_failed_slices(self):
        for attempts in range(5):
            self.assertEqual(revision_verdict(attempts=attempts, failed_slices=2),
                             "continue")
        for attempts in (5, 6):
            self.assertEqual(revision_verdict(attempts=attempts, failed_slices=2),
                             "slice_failed_after_cap")
        for failures in (3, 4):
            self.assertEqual(revision_verdict(attempts=5, failed_slices=failures), "pause")
        with self.assertRaises(ValueError):
            revision_verdict(attempts=-1, failed_slices=0)

    def test_watchdog_frozen_boundary_and_progress(self):
        for now, expected in ((109, "continue"), (110, "continue"), (111, "timeout")):
            self.assertEqual(watchdog_verdict(last_progress_at=100, now=now,
                                             threshold=10), expected)
        self.assertEqual(watchdog_verdict(last_progress_at=110, now=111,
                                         threshold=10), "continue")

    def test_watchdog_invalid_inputs(self):
        for last, now, threshold in ((2, 1, 10), (0, 1, 0), (0, 1, -1),
                                      (0, float("nan"), 1), (0, 1, float("inf"))):
            with self.assertRaises(ValueError):
                watchdog_verdict(last_progress_at=last, now=now, threshold=threshold)


if __name__ == "__main__":
    unittest.main()
