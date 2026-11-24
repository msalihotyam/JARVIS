"""N6-SHADOW1: the shadow runner (switch, cursor, KPI rows) and the live window report."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("n6_shadow_run", ROOT / "scripts" / "n6_shadow_run.py")
assert _spec and _spec.loader
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

from jarvis.n6_kpi import KpiRow, record, rows  # noqa: E402

SHA = "a" * 40
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


class ShadowRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.lanes = {"A": {"finished": True, "status": "PASS", "started_ts": "100", "finished_ts": "160"},
                      "B": {"finished": True, "status": "BLOCKED", "started_ts": "200", "finished_ts": "230"}}
        self.write("build/conveyor/lanes.json", json.dumps(self.lanes))
        self.write("build/conveyor/events.jsonl", json.dumps(
            {"event": "reader", "lane": "A", "reading": {"kind": "needs_salih", "summary": "Salih karar vermeli"}}) + "\n")
        self.write("build/reports/A.md", f"STATUS: PASS\nCOMMIT: {SHA}\n")
        self.write("build/reports/B.md", "STATUS: BLOCKED\n")
        self.write("build/ledger/usage.json", json.dumps({"by_lane": {"A": {"total_tokens": 1000},
                                                                     "B": {"total_tokens": 500}}}))
        self.db = self.root / "build" / "n6" / "kpi.db"
        self.notes = self.root / "build" / "n6" / "notes.jsonl"

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def on(self) -> None:
        self.write("build/n6/SHADOW_ON", "")

    def judge(self, head=lambda lane: SHA, **kw) -> int:
        ticks = iter([0.0, 2.0])
        return run.judge(self.root, clock=lambda: T0, timer=lambda: next(ticks), head=head, **kw)

    def test_origin_is_live_by_default_and_carried_to_notes_and_rows(self) -> None:
        self.on()
        self.judge()  # the conveyor spawns the runner with no arguments
        lines = [json.loads(x)["text"] for x in self.notes.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(t.endswith(" [origin=live]") for t in lines), lines)
        self.assertEqual({r.origin for r in rows(self.db)}, {"live"})

    def test_main_says_origin_live_on_stderr_only_when_origin_is_not_given(self) -> None:
        import contextlib
        import io
        for argv, expected in (([], "origin=live (default)\n"),(["--origin", "sweep"], "")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(run.main(["--root", str(self.root), *argv]), 0)
            self.assertEqual(err.getvalue(), expected)

    def test_explicit_sweep_origin_is_carried(self) -> None:
        self.on()
        self.judge(origin="sweep")
        text = self.notes.read_text(encoding="utf-8")
        self.assertEqual(text.count(" [origin=sweep]"), 2)
        self.assertNotIn("origin=live", text)
        self.assertEqual({r.origin for r in rows(self.db)}, {"sweep"})
        with self.assertRaises(ValueError):
            self.judge(origin="manual")

    def test_switch_off_writes_nothing(self) -> None:
        self.assertEqual(self.judge(), 0)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.notes.exists())

    def test_two_rows_per_judged_slice_with_the_fields(self) -> None:
        self.on()
        self.assertEqual(self.judge(), 4)
        got = {(r.slice_id, r.source): r for r in rows(self.db)}
        self.assertEqual(set(got), {("A", "conveyor"), ("A", "n6"), ("B", "conveyor"), ("B", "n6")})
        a = got["A", "conveyor"]
        self.assertEqual((a.passed, a.post_accept_defects, a.salih_interventions, a.quota_tokens,
                          a.finish_seconds, a.security_incidents), (True, 0, 1, 1000, 60.0, 0))
        n = got["A", "n6"]
        self.assertEqual((n.passed, n.salih_interventions, n.quota_tokens, n.finish_seconds,
                          n.security_incidents), (True, 0, 0, 1.0, 0))
        self.assertEqual((got["B", "conveyor"].passed, got["B", "n6"].passed), (False, False))
        self.assertEqual(a.recorded_at, T0)
        text = self.notes.read_text(encoding="utf-8")
        self.assertIn("A agree: conveyor pass", text)
        self.assertIn("B agree: conveyor block", text)

    def test_rerun_is_idempotent(self) -> None:
        self.on()
        self.assertEqual(self.judge(), 4)
        notes = self.notes.read_text(encoding="utf-8")
        self.assertEqual(self.judge(), 0)
        self.assertEqual(len(rows(self.db)), 4)
        self.assertEqual(self.notes.read_text(encoding="utf-8"), notes)

    def test_unknown_head_and_missing_tokens_write_no_rows(self) -> None:
        self.on()
        self.write("build/ledger/usage.json", json.dumps({"by_lane": {"A": {"total_tokens": 1000}}}))
        self.assertEqual(self.judge(head=lambda lane: None), 0)  # A unknown, B has no tokens
        self.assertFalse(self.db.exists())

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
             "-c", "core.autocrlf=false", *args],
            cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def real_repo(self) -> str:
        """self.root as a git repo whose worktree-A branch holds the commit A's report names."""
        self.git("init", "-q")
        self.git("add", "build/reports")
        self.git("commit", "-q", "-m", "x")
        sha = self.git("rev-parse", "HEAD")
        self.git("branch", "worktree-A")
        self.write("build/reports/A.md", f"STATUS: PASS\nCOMMIT: {sha}\n")
        return sha

    def test_default_head_on_a_plain_repo_writes_rows(self) -> None:
        self.real_repo()
        self.on()
        self.assertEqual(run.judge(self.root, clock=lambda: T0), 4)
        got = {(r.slice_id, r.source): r.passed for r in rows(self.db)}
        self.assertEqual((got["A", "conveyor"], got["A", "n6"]), (True, True))

    def test_default_head_with_a_multi_pack_index_and_packed_refs(self) -> None:
        sha = self.real_repo()
        self.git("repack", "-a", "-d", "-q")
        self.git("multi-pack-index", "write")
        self.git("pack-refs", "--all")
        pack = self.root / ".git" / "objects" / "pack"
        self.assertTrue(any(p.name.startswith("multi-pack-index") for p in pack.iterdir()))
        self.assertFalse((self.root / ".git" / "refs" / "heads" / "worktree-A").exists())
        self.on()
        self.assertEqual(run.judge(self.root, clock=lambda: T0), 4)
        self.assertIn("A agree",self.notes.read_text(encoding="utf-8"))
        self.assertEqual(run.branch_tip(self.n6(), self.root, "A"), sha)

    def n6(self):
        return run.N6(run.SimpleNamespace(clock=lambda: T0), store=None, notes=self.notes, clock=lambda: T0)

    def tip(self, lane: str = "A"):
        return run.branch_tip(self.n6(), self.root, lane)

    def test_branch_tip_variants(self) -> None:
        loose, packed = ".git/refs/heads/worktree-A", ".git/packed-refs"
        self.write(".git/HEAD", "ref: refs/heads/main\n")
        self.assertIsNone(self.tip())  # neither loose nor packed: branch deleted
        self.write(packed, f"# pack-refs with: peeled\n{'b' * 40} refs/heads/worktree-AB\n"
                           f"{'c' * 40} refs/heads/worktree-A\n^{'d' * 40}\n")
        self.assertEqual(self.tip(), "c" * 40)  # packed, exact name, peeled line skipped
        self.assertEqual(self.tip("AB"), "b" * 40)
        self.write(loose, "e" * 64 + "\r\n")
        self.assertEqual(self.tip(), "e" * 64)  # loose wins; sha-256 and CRLF accepted
        (self.root / loose).unlink()
        (self.root / loose / "x").mkdir(parents=True)
        self.assertEqual(self.tip(), "c" * 40)  # a directory there is not this ref
        for bad in ("../A", "A/B", "A\\B", "", ".A", "A..B", "A.lock", "C:A", "A\nB", "A B", "Ａ"):
            self.assertIsNone(self.tip(bad), bad)  # never a path outside refs/heads

    def test_branch_tip_fails_loudly_on_a_broken_store(self) -> None:
        loose = ".git/refs/heads/worktree-A"
        for content in ("", "abc", "A" * 40, "g" * 40, "ref: refs/heads/main", "a" * 41):
            self.write(loose, content)
            with self.assertRaises(RuntimeError, msg=content):
                self.tip()
        (self.root / loose).unlink()
        self.write(".git/packed-refs", "zz refs/heads/worktree-A\n")
        with self.assertRaises(RuntimeError):
            self.tip()
        (self.root / ".git" / "reftable").mkdir()
        with self.assertRaises(RuntimeError):
            self.tip()

    def test_a_nul_padded_ledger_line_and_one_bad_ref_do_not_stop_the_pass(self) -> None:
        self.on()
        with (self.root / "build/conveyor/events.jsonl").open("a", encoding="utf-8", newline="") as f:
            f.write("\x00" * 359 + json.dumps({"ts": "2026-11-19T22:34:21", "event": "lock_takeover"}) + "\n")
        self.write(".git/refs/heads/worktree-A", "")
        (self.root / ".git/refs/heads/worktree-A").write_bytes(b"\xff\xfe garbage\n")  # one lane's ref is garbage
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = self.judge(head=None)
        self.assertEqual(written, 2)  # B judged; A has no independent sha, so unknown and no rows
        self.assertEqual({r.slice_id for r in rows(self.db)}, {"B"})
        self.assertIn("events.jsonl line 2", err.getvalue())
        self.assertIn("N6 A: N6 head: refs/heads/worktree-A is not text", err.getvalue())

    def test_a_report_that_is_not_utf8_makes_only_that_lane_unknown(self) -> None:
        self.on()
        (self.root / "build/reports/A.md").write_bytes(b"\xff\xfe STATUS: PASS\n")  # A malformed, B valid
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = self.judge()
        self.assertEqual(written, 2)  # B judged; A unknown, so no rows
        self.assertEqual({r.slice_id for r in rows(self.db)}, {"B"})
        self.assertIn("N6 A: report unreadable: UnicodeDecodeError", err.getvalue())
        self.assertNotIn("N6 B:", err.getvalue())

    def test_no_git_dir_fails_the_run_instead_of_writing_nothing(self) -> None:
        self.on()
        with self.assertRaises(RuntimeError):
            run.judge(self.root, clock=lambda: T0)
        self.write(".git", "gitdir: elsewhere\n")
        with self.assertRaises(RuntimeError):
            run.judge(self.root, clock=lambda: T0)
        self.assertFalse(self.db.exists())

    def test_streak_rule(self) -> None:
        self.assertEqual(run.streak([("A", False, True), ("X", True, False), ("B", False, True)], 15),
                         (["A", "B"], None))
        self.assertEqual(run.streak([("A", False, True), ("M", False, False), ("B", False, True)], 15),
                         (["A"], "M"))
        self.assertEqual(run.streak([("A", False, True), ("B", False, True)], 1), (["A"], None))

    def test_live_counts_a_false_pass(self) -> None:
        self.db.parent.mkdir(parents=True)
        for source, passed in (("conveyor", False), ("n6", True)):  # n6 would PASS a BLOCKED lane
            record(self.db, KpiRow("B", source, passed, 0, 0, 10, 1.0, 0, T0))
        out = run.live(self.root, 15)
        self.assertIn("FALSE PASS: 1 B", out)
        self.assertIn("AGREEMENT: 0.0%", out)
        self.assertIn("STREAK: 1 (broken by A)", out)  # A finished, judged by nobody yet

    def test_live_after_judge(self) -> None:
        self.on()
        self.judge()
        out = run.live(self.root, 15)
        self.assertIn("STREAK: 2\n", out)
        self.assertIn("AGREEMENT: 100.0%", out)
        self.assertIn("FALSE PASS: 0\n", out)
        self.assertIn("OVERHEAD QUOTA: 0.0%", out)

    # N6DAILY1: the daily diff after a shadow pass, once per local day, never failing the pass.
    NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone(timedelta(hours=3)))

    def files(self) -> set[Path]:
        return {p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file()}

    def test_missing_yesterday_runs_the_real_diff_once_and_writes_only_daily(self) -> None:
        self.on()
        self.judge()  # kpi.db + notes from this pass, read by the real diff below
        before = self.files()
        self.assertTrue(run.daily(self.root, clock=lambda: self.NOW))
        daily = Path("build/n6/daily")
        self.assertEqual(self.files() - before, {daily / "2026-11-11.json", daily / "2026-11-11.md"})
        rep = json.loads((self.root / daily / "2026-11-11.json").read_text(encoding="utf-8"))
        self.assertEqual((rep["date"], rep["notes"]["agree"]), ("2026-11-11", 2))
        self.assertFalse(run.daily(self.root, clock=lambda: self.NOW))  # never twice for one day
        self.assertEqual(self.files() - before, {daily / "2026-11-11.json", daily / "2026-11-11.md"})

    def test_present_report_is_not_rebuilt_and_switch_off_does_nothing(self) -> None:
        calls: list[object] = []
        self.assertFalse(run.daily(self.root, clock=lambda: self.NOW, writer=lambda *a, **k: calls.append(a)))
        self.on()
        self.write("build/n6/daily/2026-11-11.json", "{}")
        self.write("build/n6/daily/2026-11-11.md", "x")
        self.assertFalse(run.daily(self.root, clock=lambda: self.NOW, writer=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])

    def test_until_is_local_midnight_of_the_clock_zone(self) -> None:
        self.on()
        seen: list[tuple] = []
        run.daily(self.root, clock=lambda: self.NOW, writer=lambda root, day, until: seen.append((day, until)))
        self.assertEqual(seen, [(self.NOW.date() - timedelta(days=1), self.NOW.replace(hour=0))])

    def test_diff_error_is_logged_and_the_pass_stands(self) -> None:
        self.on()
        self.judge()
        notes = self.notes.read_text(encoding="utf-8")

        def boom(*a: object, **k: object) -> None:
            raise RuntimeError("kpi.db is in WAL mode")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(run.daily(self.root, clock=lambda: self.NOW, writer=boom))
        self.assertIn("kpi.db is in WAL mode", err.getvalue())
        log = (self.root / "build/n6/daily/errors.log").read_text(encoding="utf-8")
        self.assertIn("day=2026-11-11 RuntimeError('kpi.db is in WAL mode')", log)
        self.assertEqual(self.notes.read_text(encoding="utf-8"), notes)
        self.assertEqual(len(rows(self.db)), 4)
        self.assertFalse((self.root / "build/n6/daily/2026-11-11.json").exists())

    def main_at(self, local: datetime) -> int:
        """main() with its wall clock frozen: naive now() is local time, so .astimezone() keeps the day."""
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return local if tz is None else local.astimezone(tz)
        err = io.StringIO()
        with mock.patch.object(run, "datetime", Frozen), contextlib.redirect_stderr(err):
            return run.main(["--root", str(self.root)])

    def test_main_writes_the_previous_day_once_after_a_shadow_pass(self) -> None:
        self.real_repo()  # judge needs a .git to read branch tips
        self.on()
        local = datetime(2026, 11, 12, 0, 30)  # just past local midnight: a UTC clock east of UTC names the wrong day
        self.assertEqual(self.main_at(local), 0)
        daily = self.root / "build/n6/daily"
        got = {p.name for p in daily.iterdir()}
        self.assertEqual(got, {"2026-11-11.json", "2026-11-11.md"})
        rep = json.loads((daily / "2026-11-11.json").read_text(encoding="utf-8"))
        self.assertEqual(rep["date"], "2026-11-11")
        stamp = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in daily.iterdir()}
        self.assertEqual(self.main_at(local.replace(hour=23)), 0)  # same local day, second pass
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in daily.iterdir()}, stamp)
        self.assertEqual(self.main_at(local + timedelta(days=1)), 0)  # next day adds only its own report
        self.assertEqual({p.name for p in daily.iterdir()} - got, {"2026-11-12.json", "2026-11-12.md"})

    def test_main_calls_the_writer_once_after_judge_and_a_failure_keeps_rc_0(self) -> None:
        self.real_repo()
        self.on()
        calls: list[tuple] = []

        def boom(root: Path, day: object, until: object) -> None:
            calls.append((day, self.db.exists()))  # judge already wrote its KPI rows
            raise RuntimeError("diff failed")
        with mock.patch.dict(run.daily.__kwdefaults__, {"writer": boom}):
            self.assertEqual(self.main_at(datetime(2026, 11, 12, 9, 0)), 0)
        self.assertEqual(calls, [(datetime(2026, 11, 11).date(), True)])
        log = (self.root / "build/n6/daily/errors.log").read_text(encoding="utf-8")
        self.assertIn("day=2026-11-11 RuntimeError('diff failed')", log)

    def test_main_without_the_switch_writes_no_daily_report(self) -> None:
        self.assertEqual(self.main_at(datetime(2026, 11, 12, 9, 0)), 0)
        self.assertFalse((self.root / "build/n6/daily").exists())


if __name__ == "__main__":
    unittest.main()
