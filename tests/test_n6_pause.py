"""N6-09 durable pause (R03, G12): negative tests first, each with its control."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.work_controls import N6_PAUSE_FILE, read_pause, write_pause
from jarvis.work_supervisor import Policy, Situation, supervise

THEN = Policy(90.0, 3.0, (2, 8), False, 3.0, 1.5)
NEUTRAL = {"operator_pause": False, "policy": THEN,
           "memory": {"free_gb": 8.0, "held": False},
           "quota": {"used_pct": 10, "line_pct": 10, "hour": 12}}
APPROVAL = {"approval": {"id": "N6-09"}}
PUSH = {"push": {"id": "main"}}


def _sit(**kw):
    return Situation.from_dict({**NEUTRAL, **kw})


class N6PauseTests(unittest.TestCase):
    dir = ""
    path = ""
    connect = mock.MagicMock()  # replaced in setUp by the sqlite3.connect patch

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "n6", "PAUSE")
        connect = mock.patch.object(sqlite3, "connect",
                                    side_effect=AssertionError("tests never open a DB"))
        self.connect = connect.start()
        self.addCleanup(connect.stop)

    def _raw(self, data: bytes):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(data)

    def _held(self, sit, path=None):
        d = supervise(sit, path or self.path)
        self.assertEqual((d.action, d.rule_id, d.constraint), ("pause", "operator.pause", "none"))

    # --- NEG: restart while paused stays paused; nothing is cached ---------------
    def test_neg_restart_while_paused_stays_paused(self):
        write_pause(self.path, paused=True)
        for _ in range(3):  # each "restart": new path object, new Situation, same file
            self._held(_sit(**APPROVAL), Path(self.path))
            self.assertTrue(read_pause(Path(self.path)))

    def test_state_is_read_fresh_on_every_decision(self):
        write_pause(self.path, paused=True)
        self._held(_sit())
        write_pause(self.path, paused=False)
        self.assertEqual(supervise(_sit(), self.path).action, "dispatch")
        write_pause(self.path, paused=True)
        self._held(_sit())

    # --- NEG: approvals and pushes are held during PAUSE --------------------------
    def test_neg_approval_during_pause_is_held(self):
        write_pause(self.path, paused=True)
        self._held(_sit(**APPROVAL))
        self._held(_sit(approval={"id": ""}))      # even a malformed one is held, not run
        write_pause(self.path, paused=False)       # control
        d = supervise(_sit(**APPROVAL), self.path)
        self.assertEqual((d.action, d.rule_id, d.constraint), ("approval", "gate.approval", "none"))

    def test_neg_push_during_pause_is_held(self):
        write_pause(self.path, paused=True)
        self._held(_sit(**PUSH))
        self._held(_sit(**PUSH, **APPROVAL, proposal="requeue"))
        write_pause(self.path, paused=False)       # control
        d = supervise(_sit(**PUSH), self.path)
        self.assertEqual((d.action, d.rule_id, d.constraint), ("push", "gate.push", "none"))

    def test_neg_caller_false_never_lifts_a_paused_file(self):
        write_pause(self.path, paused=True)
        self._held(_sit(operator_pause=False, **PUSH))
        write_pause(self.path, paused=False)       # running file never lifts caller's True
        self._held(_sit(operator_pause=True, **PUSH))

    def test_neg_malformed_approval_or_push_raises_when_running(self):
        for item in ({"approval": {}}, {"push": "main"}, {"approval": {"id": 7}}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                supervise(_sit(**item), self.path)

    # --- NEG: bad pause file -> paused (fail closed) -----------------------------
    def test_neg_bad_content_is_paused(self):
        for data in (b"", b"\x00\xff\xfe", b"garbage", b"running\n", b"\xef\xbb\xbfrunning",
                     b"RUNNING", b" running", b"runnin", b"runningX", b"paused",
                     "runnıng".encode()):
            with self.subTest(data=data):
                self._raw(data)
                self.assertTrue(read_pause(self.path))
                self._held(_sit(**PUSH))

    def test_neg_directory_instead_of_file_is_paused(self):
        os.makedirs(self.path)
        self.assertTrue(read_pause(self.path))
        self._held(_sit(**APPROVAL))

    def test_neg_unreadable_is_paused(self):
        write_pause(self.path, paused=False)
        with mock.patch("jarvis.work_controls.open", create=True,
                        side_effect=PermissionError("denied")):
            self.assertTrue(read_pause(self.path))
            self._held(_sit(**PUSH))

    def test_neg_present_but_not_found_is_paused(self):
        self._raw(b"running")  # e.g. a dangling link: open says not found, the path exists
        with mock.patch("jarvis.work_controls.open", create=True,
                        side_effect=FileNotFoundError("dangling")):
            self.assertTrue(read_pause(self.path))

    def test_neg_pause_write_retries_while_a_reader_holds_the_file(self):
        write_pause(self.path, paused=False)
        real, held = os.replace, [PermissionError("held"), PermissionError("held")]

        def flaky(src, dst):
            if held:
                raise held.pop()
            real(src, dst)
        with mock.patch("jarvis.work_controls.time.sleep"), mock.patch(
                "jarvis.work_controls.os.replace", side_effect=flaky) as rep:
            write_pause(self.path, paused=True)
        self.assertEqual(rep.call_count, 3)
        self.assertTrue(read_pause(self.path))

    def test_neg_empty_or_missing_path_raises(self):
        for bad in ("", None, b"x", 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                read_pause(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                write_pause(bad, paused=True)
        with self.assertRaises(ValueError):
            write_pause(self.path, paused=1)  # pyright: ignore[reportArgumentType]
        with self.assertRaises(ValueError):
            supervise(_sit(), "")

    # --- resume only by the explicit running form --------------------------------
    def test_absent_file_or_exact_running_form_is_running(self):
        self.assertFalse(read_pause(self.path))
        self._raw(b"running")
        self.assertFalse(read_pause(self.path))
        self.assertEqual(supervise(_sit(), self.path).action, "dispatch")

    def test_write_is_atomic_and_leaves_no_temp(self):
        write_pause(self.path, paused=True)
        with mock.patch("jarvis.work_controls.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                write_pause(self.path, paused=False)
        self.assertTrue(read_pause(self.path))     # old state intact
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["PAUSE"])

    def test_production_path_is_outside_the_conveyor(self):
        parts = Path(N6_PAUSE_FILE).parts
        self.assertEqual(parts, ("build", "n6", "PAUSE"))
        self.assertNotIn("conveyor", parts)

    # --- NEG: tests never open data/jarvis.db ------------------------------------
    def test_neg_never_opens_the_live_db(self):
        write_pause(self.path, paused=True)
        self._held(_sit(**APPROVAL, **PUSH))
        self.connect.assert_not_called()
        self.assertNotIn("jarvis.db", self.path)
        self.assertTrue(os.path.abspath(self.path).startswith(os.path.abspath(self.dir)))


if __name__ == "__main__":
    unittest.main()
