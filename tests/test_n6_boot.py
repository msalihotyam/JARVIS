"""N6-12 boot scan and crash recovery: temp dirs only, injected facts, no git, wall clock or jarvis.db."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jarvis import n6_lease
from jarvis.memory import SQLiteMemoryStore
from jarvis.n6_boot import Action, Facts, act, apply_once, recorder, scan
from jarvis.notify import NotificationCore
from jarvis.work_controls import write_pause
from jarvis.work_evidence import EvidenceReceipt

NOW = 1000.0
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
SHA = "c0" * 20
RECEIPT = EvidenceReceipt("p", "d", ("x",), 0, "o", {}, "pass").to_json()


class N6BootTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.lease_path, self.pause = self.dir / "n6.lease", self.dir / "pause"
        connect, self.opened = sqlite3.connect, []

        def spy(path, *args, **kwargs):  # every DB a test opens is recorded
            self.opened.append(str(path))
            return connect(path, *args, **kwargs)
        patcher = mock.patch("sqlite3.connect", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._no_live_db)

    def _no_live_db(self):
        for path in self.opened:
            self.assertNotIn("jarvis.db", Path(path).name)
            if path == ":memory:":
                continue
            self.assertTrue(Path(path).resolve().is_relative_to(self.dir.resolve()), path)

    def own(self, owner="me", ttl=60.0, now=NOW):
        lease = n6_lease.acquire(self.lease_path, owner, "s1", ttl, now)
        assert lease is not None
        return lease

    def facts(self, **kw):
        base = {"known": frozenset({"s1"}), "worktrees": frozenset({"s1"}),
                "commits": {"s1": SHA}, "receipts": {"s1": RECEIPT}}
        return Facts(**{**base, **kw})

    def run_boot(self, facts, owner="me", now=NOW):
        report = scan(self.lease_path, self.pause, owner, now, facts)
        log = []
        acted = act(report, self.lease_path, now, lambda i: log.append(("record", i)),
                    lambda i: log.append(("resume", i)))
        return report, acted, log

    def store(self):
        store = SQLiteMemoryStore(self.dir / "store.db")
        seq = iter(range(1, 100))
        core = NotificationCore(store.append_work_event, store.work_events)
        return store, recorder(core.notify, "boot-run", AT, lambda: next(seq))

    def test_neg_crash_between_commit_and_receipt_never_applies_twice(self):
        lease, git, applies, receipts = self.own(), set(), [], []

        def apply():
            applies.append(SHA)
            git.add(SHA)  # the commit landed

        def crash():
            raise RuntimeError("crash before receipt")
        with self.assertRaises(RuntimeError):
            apply_once(self.lease_path, lease, SHA, NOW, frozenset(git), apply, crash)
        report, acted, log = self.run_boot(self.facts(applied=frozenset(git)))
        self.assertTrue(acted)
        self.assertEqual([(e[0], e[1].action, e[1].sha) for e in log], [("resume", Action.RESUME, SHA)])
        for _ in range(2):  # the resumed apply and a further replay
            out = apply_once(self.lease_path, lease, SHA, NOW, frozenset(git), apply,
                             lambda: receipts.append(1))
            self.assertEqual(out, "receipt")
        self.assertEqual(applies, [SHA])

    def test_neg_crash_after_intent_before_apply_is_attention_not_apply(self):
        lease, applies = self.own(), []
        with self.assertRaises(RuntimeError):
            apply_once(self.lease_path, lease, SHA, NOW, frozenset(),
                       lambda: (_ for _ in ()).throw(RuntimeError("crash")), lambda: None)
        out = apply_once(self.lease_path, lease, SHA, NOW, frozenset(), lambda: applies.append(1), lambda: None)
        self.assertEqual((out, applies), ("attention", []))

    def test_neg_crash_mid_event_writes_event_and_outbox_together_or_not_at_all(self):
        store, record = self.store()
        report = scan(self.lease_path, self.pause, "me", NOW, Facts(worktrees=frozenset({"ghost"})))
        (item,) = report.items
        db = sqlite3.connect(self.dir / "store.db")
        db.execute("CREATE TRIGGER crash BEFORE INSERT ON notify_outbox BEGIN SELECT RAISE(ABORT, 'crash'); END")
        db.commit()
        with self.assertRaises(sqlite3.Error):
            record(item)
        self.assertEqual((store.work_events("boot-run"), store.notify_outbox_rows()), ([], []))
        db.execute("DROP TRIGGER crash")
        db.commit()
        db.close()
        self.assertTrue(record(item))
        self.assertEqual(len(store.work_events("boot-run")), 1)
        self.assertEqual(len(store.notify_outbox_rows()), 1)
        self.assertFalse(record(item))  # a second boot collapses the replay
        self.assertEqual(len(store.notify_outbox_rows()), 1)

    def test_neg_unknown_leftover_is_attention_not_resumed(self):
        lease = self.own()
        facts = self.facts(worktrees=frozenset({"s1", "ghost"}), commits={"s1": SHA, "ghost": SHA})
        report, _, log = self.run_boot(facts)
        ghost = [i for i in report.items if i.ref == "ghost"]
        self.assertEqual([(i.action, i.reason) for i in ghost], [(Action.ATTENTION, "unknown leftover")])
        self.assertIn(("record", ghost[0]), log)
        self.assertNotIn("ghost", [e[1].ref for e in log if e[0] == "resume"])
        self.assertTrue(n6_lease.check(self.lease_path, lease, NOW))  # our own lease untouched

    def test_neg_malformed_lease_holds_all_and_resumes_nothing(self):
        self.lease_path.write_bytes(b"{not json")
        report, acted, log = self.run_boot(self.facts())
        self.assertEqual((report.hold, acted, log), ("held_all", False, []))
        self.assertEqual([i.action for i in report.items], [Action.ATTENTION])
        self.assertEqual(self.lease_path.read_bytes(), b"{not json")
        self.assertFalse(Path(f"{self.lease_path}.lock").exists())

    def test_neg_expired_lease_of_another_owner_is_not_resumed_without_new_epoch(self):
        self.own(owner="other", ttl=10.0, now=0.0)
        before = self.lease_path.read_bytes()
        report, acted, log = self.run_boot(self.facts())
        self.assertTrue(acted)
        self.assertEqual([(e[0], e[1].action) for e in log], [("record", Action.HAND_BACK)])
        self.assertEqual(self.lease_path.read_bytes(), before)  # not released, not re-epoched

    def test_neg_paused_scan_reports_but_acts_on_nothing(self):
        self.own()
        write_pause(self.pause, paused=True)
        before = self.lease_path.read_bytes()
        report, acted, log = self.run_boot(self.facts())
        self.assertEqual((report.hold, acted, log), ("paused", False, []))
        self.assertEqual([i.action for i in report.items], [Action.RESUME])  # reported only
        self.assertEqual(self.lease_path.read_bytes(), before)

    def test_ok_consistent_half_done_slice_is_resumed_once(self):
        lease, applies = self.own(), []
        report, acted, log = self.run_boot(self.facts())
        self.assertEqual([(e[0], e[1].ref, e[1].sha) for e in log], [("resume", "s1", SHA)])
        args = (self.lease_path, lease, SHA, NOW, frozenset(), lambda: applies.append(1), lambda: None)
        self.assertEqual(apply_once(*args), "applied")
        self.assertEqual(apply_once(*args), "attention")  # replay without git proof never re-applies
        self.assertEqual(applies, [1])

    def test_neg_outbox_row_without_its_event_is_attention(self):
        report = scan(self.lease_path, self.pause, "me", NOW,
                      Facts(outbox=(("r", 1), ("r", 2)), events=frozenset({("r", 1)})))
        self.assertEqual([i.action for i in report.items], [Action.RESUME, Action.ATTENTION])

    def test_neg_tests_never_open_live_db(self):
        self.store()
        self.assertTrue(self.opened)
        self.assertFalse(any(p.replace("\\", "/").endswith("data/jarvis.db") for p in self.opened))


if __name__ == "__main__":
    unittest.main()
