"""PM03: work lease store (claim / renew / release over the append-only work_lease table)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.memory import SQLiteMemoryStore
from jarvis.work_ledger import WorkLedgerError

T = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
BOOT = "boot-1"


class WorkLeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.db = Path(self._temp.name) / "jarvis.db"
        self.store = SQLiteMemoryStore(self.db)

    def claim(self, target, run="run-a", boot=BOOT, now=T):
        return self.store.claim_work_lease(target, run, boot, now=now, lease=LEASE)

    def history(self):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute("SELECT * FROM work_lease ORDER BY lease_id").fetchall()
        finally:
            connection.close()

    def test_overlapping_claims_are_refused(self) -> None:
        cases = [
            ("src/a.py", "src/a.py"),  # same file
            ("src/a.py", "SRC\\A.PY"),  # same file, case/backslash variant
            ("src/", "src/deep/a.py"),  # file inside a claimed subtree
            ("src/deep/a.py", "src/"),  # subtree containing a claimed file
            ("src/deep/", "Src/"),  # subtree containing a claimed subtree
            ("src/", "src/deep/"),  # subtree inside a claimed subtree
        ]
        for held, wanted in cases:
            with self.subTest(held=held, wanted=wanted):
                self.setUp()
                self.claim(held)
                with self.assertRaises(WorkLedgerError):
                    self.claim(wanted, run="run-b")
                with self.assertRaises(WorkLedgerError):
                    self.claim(wanted)  # the holder itself renews instead

    def test_disjoint_targets_coexist(self) -> None:
        self.claim("src/a.py")
        self.claim("src/a.pyc", run="run-b")
        self.claim("src/ab/", run="run-c")
        self.claim("srcx/", run="run-d")
        self.assertEqual(len(self.history()), 4)

    def test_claim_normalises_and_returns_the_row(self) -> None:
        row = self.claim("Docs\\Guide\\")
        self.assertEqual(row["target"], "docs/guide/")
        self.assertEqual(row["expires_at"], T + LEASE)
        self.assertEqual(self.store.work_leases("docs/guide/")[0]["lease_id"], row["lease_id"])

    def test_non_literal_targets_are_refused(self) -> None:
        for target in ("src/*.py", "../x", "/abs", "C:/x", "", ".", "data/jarvis.db", "a/./b", 7):
            with self.subTest(target=target), self.assertRaises(WorkLedgerError):
                self.claim(target)
        self.assertEqual(self.history(), [])

    def test_bad_arguments_are_refused(self) -> None:
        with self.assertRaises(WorkLedgerError):
            self.store.claim_work_lease("a.py", "r", BOOT, now=T.replace(tzinfo=None), lease=LEASE)
        for lease in (timedelta(seconds=1), timedelta(days=1), 300):
            with self.subTest(lease=lease), self.assertRaises(WorkLedgerError):
                self.store.claim_work_lease("a.py", "r", BOOT, now=T, lease=lease)
        with self.assertRaises(WorkLedgerError):
            self.claim("a.py", run="")

    def test_expiry_frees_the_target(self) -> None:
        self.claim("src/")
        with self.assertRaises(WorkLedgerError):
            self.claim("src/a.py", run="run-b", now=T + LEASE - timedelta(seconds=1))
        self.claim("src/a.py", run="run-b", now=T + LEASE)

    def test_other_boot_is_reaped(self) -> None:
        self.claim("src/a.py", boot="boot-0")
        row = self.claim("src/a.py", run="run-b", boot="boot-2")
        self.assertEqual(row["run_id"], "run-b")
        with self.assertRaises(WorkLedgerError):
            self.store.renew_work_lease("src/a.py", "run-a", "boot-0", now=T, lease=LEASE)

    def test_renew_by_holder_only(self) -> None:
        self.claim("a.py")
        later = T + timedelta(minutes=4)
        with self.assertRaises(WorkLedgerError):
            self.store.renew_work_lease("a.py", "run-b", BOOT, now=later, lease=LEASE)
        with self.assertRaises(WorkLedgerError):
            self.store.renew_work_lease("a.py", "run-a", "boot-2", now=later, lease=LEASE)
        row = self.store.renew_work_lease("A.PY", "run-a", BOOT, now=later, lease=LEASE)
        self.assertEqual(row["expires_at"], later + LEASE)
        with self.assertRaises(WorkLedgerError):
            self.claim("a.py", run="run-b", now=T + LEASE)  # the renewal still holds it

    def test_renew_after_expiry_is_refused(self) -> None:
        self.claim("a.py")
        with self.assertRaises(WorkLedgerError):
            self.store.renew_work_lease("a.py", "run-a", BOOT, now=T + LEASE, lease=LEASE)

    def test_release_by_holder_only_and_frees_target(self) -> None:
        self.claim("src/")
        with self.assertRaises(WorkLedgerError):
            self.store.release_work_lease("src/", "run-b", BOOT, now=T)
        self.store.release_work_lease("src/", "run-a", BOOT, now=T)
        with self.assertRaises(WorkLedgerError):
            self.store.release_work_lease("src/", "run-a", BOOT, now=T)
        with self.assertRaises(WorkLedgerError):
            self.store.renew_work_lease("src/", "run-a", BOOT, now=T, lease=LEASE)
        self.claim("src/x.py", run="run-b")

    def test_history_is_append_only(self) -> None:
        self.claim("a.py")
        before = self.history()
        self.store.renew_work_lease("a.py", "run-a", BOOT, now=T + timedelta(minutes=1), lease=LEASE)
        self.store.release_work_lease("a.py", "run-a", BOOT, now=T + timedelta(minutes=2))
        with self.assertRaises(WorkLedgerError):
            self.store.release_work_lease("a.py", "run-a", BOOT, now=T + timedelta(minutes=3))
        after = self.history()
        self.assertEqual(after[: len(before)], before)
        self.assertEqual(len(after), 3)


if __name__ == "__main__":
    unittest.main()
