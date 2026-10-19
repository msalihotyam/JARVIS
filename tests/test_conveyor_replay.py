"""Replay net for scripts/conveyor.py: EVTAUDIT section 4 incidents (R1 R2 R4 R7 R8)
replayed through pass_once with a fake clock, fake runner and fake git.

Each test asserts TODAY's behaviour, bad outcomes included. Its docstring carries the
incident window and a TARGET: line (EVTAUDIT "What the conveyor should have done"); the
fix slice for that seed flips the assertions to the TARGET.

Fixtures are inline, anonymised and live in a tmp dir (tests.test_conveyor.Base).
"""
import json
import os
import unittest
from unittest import mock

import tests.test_conveyor as tc

conveyor = tc.conveyor
T0 = 1760000000.0  # fixed replay clock (C3); EVTAUDIT wall times map onto offsets from it


class Replay(tc.AutopilotBase):
    def setUp(self):
        super().setUp()
        self.t = T0  # Base's now() reads self.t live
        (self.base / "pace.json").write_text(json.dumps({"max_lanes": 6}), encoding="utf-8")

    def dispatch(self, *names):
        for n in names:
            self.drop(f"{n}.md", tc.HDR.format(n))
        self.c.pass_once()
        self.assertTrue(set(names) <= set(self.fake.bg_names()))
        self.t += 60


class TestR1StallBatchCollect(Replay):
    def test_lanes_finished_during_a_dead_loop_are_collected_in_one_batch_unpaged(self):
        """R1 2026-10-09T01:30:58 -> 15:23:10: the loop stopped passing for ~14 h while lanes
        finished; the next pass closed all of them under one timestamp (20 finishes, one ts).

        TODAY: no page while no heartbeat; the first pass after the gap collects every
        finished lane at once, every collect event shares that pass's ts.
        TARGET: page once after 30 min without a heartbeat and close each finished lane
        when it finishes, not in one batch.
        """
        self.dispatch("A1", "A2", "A3")
        for i, n in enumerate(("A1", "A2", "A3")):  # finished an hour apart, nobody passing
            self.report(n, "PASS", mtime=self.t + 3600 * (i + 1))
        self.t += 14 * 3600
        self.c.pass_once()
        collect = [e for e in self.ev("collect") if e.get("lane") in ("A1", "A2", "A3")]
        self.assertEqual(sorted(e["lane"] for e in collect), ["A1", "A2", "A3"])
        self.assertEqual(len({e["ts"] for e in collect}), 1)
        self.assertEqual(self.pushes, [])  # nothing paged the 14 h gap


class TestR2BreakerStorm(Replay):
    def blocked(self, *names):
        for n in names:
            self.dispatch(n)
            self.finish(n, "BLOCKED")

    def test_same_root_blocked_lanes_open_the_breaker_and_a_pass_does_not_close_it(self):
        """R2 2026-10-13T02:45:42 -> 13:15:27: the breaker stayed open 10.5 h after three
        BLOCKED lanes that shared one root cause, though later lanes passed.

        TODAY: three BLOCKED lanes within 2 h open the breaker whatever their root; a CDX
        review lane's BLOCKED is not counted; a later PASS lane leaves it open.
        TARGET: count only distinct-root BLOCKED (CDX/DRY review findings excluded) and
        auto-reset when the next lane passes.
        """
        self.blocked("CDX1")
        self.assertEqual(self.status()["breaker"], "closed")
        self.blocked("F1", "F2")  # F1..F3 share one root cause
        self.dispatch("F3", "G1")  # G1 is already running when the breaker opens
        self.finish("F3", "BLOCKED")
        self.assertEqual(self.status()["breaker"], "open")
        (opened,) = self.ev("breaker")
        self.assertEqual(opened["state"], "open")
        self.assertIn("F1, F2, F3", opened["reason"])
        self.assertNotIn("CDX1", opened["reason"])
        self.finish("G1", "PASS")
        self.t += 3600
        self.c.pass_once()
        self.assertEqual(self.status()["breaker"], "open")
        self.assertIn(conveyor.BREAKER_ID, self.attn())
        self.assertEqual([e["state"] for e in self.ev("breaker")], ["open"])


class TestR4CIRedFlaky(Replay):
    def setUp(self):
        super().setUp()
        self.git = self.c.cmd = tc.StackGit([tc.ORIGIN, tc.SHA_A, tc.SHA_B])

    def test_red_ci_shard_goes_straight_to_auto_fix_without_a_rerun(self):
        """R4 INT45 2026-10-13T23:24:46, INT50 2026-10-14T04:56:01: a CI shard went red on
        a load-sensitive test and the conveyor queued an AFX fix lane instead of a rerun.

        TODAY: a CI failure marks the lane BLOCKED, never reruns the run, and queues
        INT1-AFX then INT1-R.
        TARGET: rerun the CI shard once (R16), then decide GREEN or AFX.
        """
        self.drop("INT1.md", tc.INT_HDR.format("INT1") + "INTEGRATE\n")
        self.c.pass_once()
        self.assertIn("INT1", self.fake.bg_names())
        self.git.main = tc.SHA_A
        self.t += 60
        self.finish("INT1", "CI_PENDING", f"NEW MAIN SHA: {tc.SHA_A}\nCI: pushed ci/int1, run in progress\n")
        self.git.ci(tc.SHA_A, "failure")
        self.c.pass_once()
        self.assertEqual(self.lanes()["INT1"]["status"], "BLOCKED")
        self.assertEqual(self.ev("ci_rerun"), [])
        self.assertFalse([c for c in self.git.calls if c[:3] == ["gh", "run", "rerun"]])
        (afx,) = self.ev("auto_fix")
        self.assertEqual((afx["lane"], afx["fix"], afx["rerun"]), ("INT1", "INT1-AFX", "INT1-R"))


class TestR7PushBlockedLocalMain(tc.Base):
    def test_approval_for_a_ci_pushed_sha_is_refused_while_local_main_lags(self):
        """R7 2026-10-18T15:49:51 and 19:01:43: an approved sha that CI had already tested
        on ci/* was refused because local main had not moved to it.

        TODAY: push_blocked with reason "local main <12> is not <sha>", no git push, the
        approval stays put and is raised as an attention item.
        TARGET: compare against origin and the ci/* branch, and let the push gate
        fast-forward local main.
        """
        self.t = T0
        want = "d" * 40  # the sha the lane pushed to ci/*; local main is still MAIN
        d = self.root / "build" / "approvals"
        d.mkdir(parents=True)
        approval = d / f"push-{want}-p2b"
        approval.write_text("integration report read, ok\n", encoding="utf-8")
        self.c.pass_once()
        (ev,) = [e for e in self.events() if e["event"] == "push_blocked"]
        self.assertEqual(ev["approval"], approval.name)
        self.assertEqual(ev["reason"], f"local main {tc.MAIN[:12]} is not {want}")
        self.assertEqual(self.git.pushes(), [])
        self.assertTrue(approval.exists())
        self.assertIn(approval.name, " ".join(self.attn()))


class TestR8PermissionErrorOnReplace(tc.Base):
    def test_a_locked_status_file_raises_a_pass_error_on_every_tick(self):
        """R8 2026-10-18T15:52:04 -> 2026-10-19T01:13:51: os.replace of status.json.tmp hit
        PermissionError(13) (a reader held status.json) 11 times; status.json went stale.

        TODAY: every pass raises, logs an "error" event "pass: PermissionError(13, ...)",
        and status.json keeps the last good snapshot (or never appears).
        TARGET: retry the replace with backoff and skip status for this pass; do not
        raise a pass error on every tick.
        """
        self.t = T0
        (self.base / "PAUSE").write_text("")
        real = os.replace

        def locked(src, dst):
            if str(src).endswith("status.json.tmp"):
                raise PermissionError(13, "Access is denied")
            return real(src, dst)

        self.c.pass_once()
        good = self.status()["updated_at"]
        with mock.patch.object(conveyor.os, "replace", locked):
            for _ in range(2):
                self.t += 60
                self.c.loop(once=True)
        errors = [e for e in self.events() if e["event"] == "error"]
        self.assertEqual(len(errors), 2)
        for e in errors:
            self.assertTrue(e["message"].startswith("pass: PermissionError(13"), e)
        self.assertEqual(self.status()["updated_at"], good)


if __name__ == "__main__":
    unittest.main()
