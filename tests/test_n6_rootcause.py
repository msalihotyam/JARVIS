"""N6-13 root-cause gate and retry cap (G17, R23): fake ports, no database or process."""

import unittest
from unittest import mock

from jarvis.work_driver import drive
from jarvis.work_ledger import RunState, StepState, WorkRun
from jarvis.work_regress import RETRY_CAP, retriable, root_cause_ok
from jarvis.write_stage import StageError
from tests.test_work_driver import FAIL, KEY_A, PASS, RUN, T0, FakePorts, result, run_drive

CAP_POINT = "slice_failed_after_cap"


class Weird(Exception):
    pass


class RootCauseTest(unittest.TestCase):
    def setUp(self):
        # The NEG paths must never reach a real database (data/jarvis.db included).
        patcher = mock.patch("sqlite3.connect", side_effect=AssertionError("database opened"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def notified(self, ports, dedupe):
        return ("notify", CAP_POINT, dedupe) in ports.log

    def hold_detail(self, ports, gate):
        return [e[3]["findings"] for e in ports.log if e[0] == "event" and e[3].get("gate") == gate]

    def test_neg_first_retry_without_root_cause_is_refused(self):
        # N6-13FIX: every retry needs a record, the first one included.
        ports = FakePorts({"a": [result()]}, regress=[FAIL])
        ports.root_cause = mock.Mock(return_value=None)
        run, steps = run_drive(ports)
        self.assertIs(run.state, RunState.PAUSED)
        self.assertEqual((steps["a"].state, steps["a"].attempt), (StepState.ACCEPTING, 1))
        self.assertEqual(len(ports.workers["a"].tasks), 1)
        self.assertTrue(self.notified(ports, f"{KEY_A}:root_cause:1"))
        self.assertEqual(self.hold_detail(ports, "root_cause"), [[f"missing_or_invalid:{KEY_A}:1"]])
        ports.root_cause.assert_called_once()
        self.assertEqual(ports.count("apply"), 0)

    def test_neg_root_cause_port_error_is_refused(self):
        ports = FakePorts({"a": [result()]}, regress=[FAIL])
        ports.root_cause = mock.Mock(side_effect=OSError("unreadable"))
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, steps["a"].attempt), (StepState.ACCEPTING, 1))
        self.assertEqual(len(ports.workers["a"].tasks), 1)

    def test_neg_root_cause_repeating_the_error_text_is_refused(self):
        ports = FakePorts({"a": [TimeoutError("slow")]})
        ports.root_cause = lambda step: f"ROOT CAUSE {step.key}:{step.attempt}: gate_error:TimeoutError"
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, steps["a"].attempt), (StepState.BLOCKED_BY_GATE, 1))
        self.assertEqual(len(ports.workers["a"].tasks), 1)
        self.assertTrue(self.notified(ports, f"{KEY_A}:root_cause:1"))

    def test_neg_non_retriable_stage_error_ends_the_slice(self):
        ports = FakePorts({"a": [StageError("stage gone")]})
        run, steps = run_drive(ports)
        self.assertIs(steps["a"].state, StepState.CANCELLED)
        self.assertEqual(len(ports.workers["a"].tasks), 1)
        self.assertTrue(self.notified(ports, f"{KEY_A}:non_retriable"))
        ports = FakePorts({"a": [result()]}, apply=[StageError("apply")])
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, len(ports.workers["a"].tasks)), (StepState.CANCELLED, 1))

    def test_neg_unknown_error_class_is_not_retried(self):
        for exc in (Weird("?"), RuntimeError("?"), ValueError("?")):
            with self.subTest(exc=type(exc).__name__):
                ports = FakePorts({"a": [exc]})
                run, steps = run_drive(ports)
                self.assertIs(steps["a"].state, StepState.CANCELLED)
                self.assertEqual(len(ports.workers["a"].tasks), 1)

    def test_neg_valid_root_cause_cannot_pass_the_cap(self):
        # The cap counts attempts and fails closed to the lower number: attempt RETRY_CAP is the last.
        ports = FakePorts({"a": [result()]}, regress=[FAIL, FAIL, PASS])
        run, steps = run_drive(ports)
        step = steps["a"]
        self.assertEqual((run.state, step.state, step.attempt), (RunState.PAUSED, StepState.ACCEPTING, RETRY_CAP))
        self.assertTrue(self.notified(ports, f"{KEY_A}:retry_cap:{RETRY_CAP}"))
        self.assertEqual(self.hold_detail(ports, "retry_cap"), [[f"attempt_after_cap:{KEY_A}:{RETRY_CAP}"]])
        run, (step,) = drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (step,), ports)
        self.assertEqual((run.state, step.state, step.attempt), (RunState.PAUSED, StepState.ACCEPTING, RETRY_CAP))
        self.assertEqual(len(ports.workers["a"].tasks), RETRY_CAP)
        self.assertEqual(ports.count("apply"), 0)

    def test_neg_resume_checks_the_record_against_the_recorded_failure(self):
        # CLREV-N6-13 F2 probe: on resume an error-text-only record is refused; unreadable findings hold.
        ports = FakePorts({"a": [TimeoutError("slow"), result()]})
        ports.root_cause = lambda step: None
        run, steps = run_drive(ports)
        step = steps["a"]
        self.assertEqual((step.state, step.attempt), (StepState.BLOCKED_BY_GATE, 1))

        def resume():
            return drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (step,), ports)

        ports.root_cause = lambda s: f"ROOT CAUSE {s.key}:1: gate_error:TimeoutError"
        run, (step,) = resume()
        self.assertEqual((run.state, step.state, step.attempt), (RunState.PAUSED, StepState.BLOCKED_BY_GATE, 1))
        self.assertEqual(len(ports.workers["a"].tasks), 1)
        ports.root_cause = lambda s: f"ROOT CAUSE {s.key}:1: proxy dropped idle sockets"
        for broken in (mock.Mock(side_effect=OSError("ledger")), mock.Mock(return_value=[])):
            ports.gate_findings = broken
            run, (step,) = resume()
            self.assertEqual((run.state, step.attempt, len(ports.workers["a"].tasks)), (RunState.PAUSED, 1, 1))
        self.assertIn([f"findings_unreadable:{KEY_A}:1"], self.hold_detail(ports, "root_cause"))
        # OK: the readable failure plus a record that explains it lets the retry run.
        del ports.gate_findings
        run, (step,) = resume()
        self.assertEqual((step.state, step.attempt, len(ports.workers["a"].tasks)), (StepState.ACCEPTED, 2, 2))

    def test_ok_transient_retry_under_the_cap_proceeds(self):
        ports = FakePorts({"a": [TimeoutError("slow"), result()]})
        ports.root_cause = mock.Mock(return_value=f"ROOT CAUSE {KEY_A}:1: proxy dropped idle sockets")
        run, steps = run_drive(ports)
        self.assertEqual((steps["a"].state, steps["a"].attempt), (StepState.ACCEPTED, 2))
        ports.root_cause.assert_called_once()

    def test_root_cause_record_rules(self):
        fid, err = f"{KEY_A}:2", "gate_error:TimeoutError"
        self.assertTrue(root_cause_ok(f"ROOT CAUSE {fid}: proxy dropped idle sockets", fid, err))
        for bad in (None, "", "   ", f"ROOT CAUSE {fid}:", f"ROOT CAUSE {fid}: {err}",
                    f"ROOT CAUSE {fid}: {err.upper()}!", f"ROOT CAUSE {fid}: {fid}",
                    f"ROOT CAUSE {KEY_A}:1: proxy dropped idle sockets", "proxy dropped idle sockets",
                    f"ROOT CAUSE {fid}: Gate error, TimeoutError.", f"ROOT CAUSE {fid}: gate_error {fid}",
                    f"ROOT CAUSE {fid}: timeout"):
            with self.subTest(bad=bad):
                self.assertFalse(root_cause_ok(bad, fid, err))

    def test_retriable_allowlist(self):
        self.assertTrue(retriable(TimeoutError()))
        self.assertTrue(retriable(ConnectionError()))
        for exc in (StageError(), RuntimeError(), ValueError(), Weird(), OSError()):
            self.assertFalse(retriable(exc))
        self.assertEqual(RETRY_CAP, 2)


if __name__ == "__main__":
    unittest.main()
