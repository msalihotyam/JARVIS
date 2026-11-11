"""N6-SHADOW1: the shadow runner (switch, cursor, KPI rows) and the live window report."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
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

    def judge(self, head=lambda lane: SHA) -> int:
        ticks = iter([0.0, 2.0])
        return run.judge(self.root, clock=lambda: T0, timer=lambda: next(ticks), head=head)

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


if __name__ == "__main__":
    unittest.main()
