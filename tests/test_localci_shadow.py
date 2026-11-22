"""LOCALCI0: scripts/localci_shadow.py - temp git repos, stubbed suite and gh; no real suite, no network."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("localci_shadow", ROOT / "scripts" / "localci_shadow.py")
shadow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shadow)

NOW = "2026-11-22T23:30:00+00:00"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def shard(index: int, ids: list[str], green: bool = True, wall: float = 10.0) -> dict:
    return {"shard": [index, 4], "green": green, "wall": wall, "dispatched": ids,
            "ran": len(ids), "skipped": 0, "failures": 0 if green else 1, "errors": 0}


class Fake(shadow.Shadow):
    """The real git; the suite and gh stubbed."""

    def __init__(self, *args, report=None, rc=0, gh=None, **kwargs):
        super().__init__(*args, clock=lambda: NOW, python="py", **kwargs)
        self.report, self.rc, self.gh = report, rc, gh or (lambda argv: (1, "gh: offline"))
        self.suite_calls: list = []
        self.gh_calls: list = []

    def cmd(self, argv, cwd=None, timeout=600):
        if argv[0] == "gh":
            self.gh_calls.append(argv)
            return self.gh(argv)
        return super().cmd(argv, cwd, timeout)

    def suite(self, argv, cwd):
        self.suite_calls.append((argv, Path(cwd), (Path(cwd) / "f.txt").read_text(encoding="utf-8")))
        if isinstance(self.report, Exception):
            raise self.report
        if self.report is not None:
            Path(argv[argv.index("--report") + 1]).write_text(json.dumps(self.report), encoding="utf-8")
        return self.rc, "suite output"


def gh_complete(shards: list[dict], conclusion: str = "success"):
    """gh run list -> one completed tests run of the sha; gh run download -> the shard reports."""
    def answer(argv):
        if argv[1:3] == ["run", "list"]:
            return 0, json.dumps([{"databaseId": 7, "headSha": answer.sha, "status": "completed",
                                   "conclusion": conclusion, "workflowName": "tests"},
                                  {"databaseId": 6, "headSha": answer.sha, "status": "completed",
                                   "conclusion": "success", "workflowName": "other"}])
        target = Path(argv[argv.index("--dir") + 1])
        for data in shards:
            folder = target / f"ci-shard-{data['shard'][0]}"
            folder.mkdir()
            (folder / "shard-report.json").write_text(json.dumps(data), encoding="utf-8")
        return 0, ""
    return answer


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.scratch = self.tmp / "repo", self.tmp / "scratch"
        self.repo.mkdir()
        self.scratch.mkdir()
        self.out, self.events = self.tmp / "out", self.tmp / "events.jsonl"
        git(self.repo, "init", "-q")
        self.shas = []
        for n in (1, 2):
            (self.repo / "f.txt").write_text(str(n), encoding="utf-8")
            git(self.repo, "add", "f.txt")
            git(self.repo, "commit", "-q", "-m", f"c{n}")
            self.shas.append(git(self.repo, "rev-parse", "HEAD"))

    def make(self, **kwargs) -> Fake:
        return Fake(root=self.repo, out=self.out, events=self.events, temp=self.scratch, **kwargs)

    def pend(self, *shas: str, branch: str = "ci/x") -> None:
        with self.events.open("ab") as fh:
            for sha in shas:
                fh.write(json.dumps({"event": "ci_pending", "lane": "L", "sha": sha, "branch": branch}).encode()
                         + b"\n")

    def worktrees(self) -> int:
        return git(self.repo, "worktree", "list", "--porcelain").count("worktree ")


class TestPick(Base):
    def test_newest_first_one_per_sha_bad_lines_skipped(self):
        a, b = self.shas
        self.pend(a)
        with self.events.open("ab") as fh:
            fh.write(b"\xff\xfe not json\n" + json.dumps({"event": "ci_done", "sha": a}).encode() + b"\n")
            fh.write(json.dumps({"event": "ci_pending", "sha": "nothex"}).encode() + b"\n[1]\n")
        self.pend(b, a)
        self.assertEqual([e["sha"] for e in shadow.pending_shas(self.events)], [a, b])
        self.assertEqual(shadow.pending_shas(self.tmp / "missing.jsonl"), [])

    def test_a_sha_with_a_json_is_not_run_again(self):
        a, b = self.shas
        self.pend(a, b)
        self.out.mkdir()
        (self.out / f"{b}.json").write_text(json.dumps({"sha": b, "verdict": "error"}), encoding="utf-8")
        fake = self.make(report={"green": True, "dispatched": ["t.a"], "wall": 1.0})
        self.assertEqual(fake.run_once(), f"ran {a}: green")
        self.assertEqual(fake.run_once(), "idle: no new ci_pending sha")
        self.assertEqual(len(fake.suite_calls), 1)

    def test_lock_held_means_busy_and_nothing_runs(self):
        self.pend(self.shas[0])
        fake = self.make(report={"green": True})
        self.out.mkdir()
        with shadow.file_lock(self.out / ".lock") as held:
            self.assertTrue(held)
            self.assertTrue(fake.run_once().startswith("busy"))
        self.assertEqual(fake.suite_calls, [])
        self.assertTrue(fake.run_once().startswith("ran "))

    def test_suite_python_maps_pythonw_to_python(self):
        (self.tmp / "python.exe").write_text("", encoding="utf-8")
        self.assertEqual(shadow.suite_python(str(self.tmp / "pythonw.exe")), str(self.tmp / "python.exe"))
        self.assertEqual(shadow.suite_python(str(self.tmp / "python.exe")), str(self.tmp / "python.exe"))
        self.assertEqual(shadow.suite_python(str(self.scratch / "pythonw.exe")), str(self.scratch / "pythonw.exe"))


class TestLocalRun(Base):
    REPORT = {"green": True, "wall": 12.5, "dispatched": ["t.b", "t.a", "t.a"], "ran": 3, "skipped": [["t.c", "x"]],
              "failures": 0, "errors": 0}

    def run_one(self, **kwargs) -> tuple[Fake, dict]:
        sha = self.shas[0]
        self.pend(sha)
        fake = self.make(**kwargs)
        fake.run_once()
        return fake, json.loads((self.out / f"{sha}.json").read_text(encoding="utf-8"))

    def test_green_run_writes_the_record_and_removes_the_worktree(self):
        fake, rec = self.run_one(report=self.REPORT)
        argv, cwd, content = fake.suite_calls[0]
        self.assertEqual(argv[:5], ["py", "scripts/test_parallel.py", "--durations", "30", "--report"])
        self.assertNotIn("--shard", argv)
        self.assertEqual(content, "1")  # the event's sha, not HEAD
        self.assertFalse(cwd.exists())  # removed after the run
        self.assertEqual(rec["verdict"], "green")
        self.assertEqual(rec["counts"], {"run": 3, "skipped": 1, "failures": 0, "errors": 0})
        self.assertEqual(rec["ids"], ["t.a", "t.b"])
        self.assertEqual((rec["ids_count"], rec["ids_hash"]), (2, shadow.ids_hash(["t.b", "t.a"])))
        self.assertEqual((rec["duration"], rec["started"], rec["finished"]), (12.5, NOW, NOW))
        self.assertEqual((rec["lane"], rec["branch"], rec["errors"]), ("L", "ci/x", []))
        self.assertEqual(rec["github"]["status"], "error")  # gh offline: recorded, never raised
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_red_run_keeps_the_output_tail(self):
        _, rec = self.run_one(report={**self.REPORT, "green": False, "failures": 1}, rc=1)
        self.assertEqual((rec["verdict"], rec["returncode"], rec["tail"]), ("red", 1, "suite output"))
        self.assertEqual(self.worktrees(), 1)

    def test_a_zero_exit_without_green_is_red(self):
        _, rec = self.run_one(report={**self.REPORT, "green": False})
        self.assertEqual(rec["verdict"], "red")

    def test_no_report_is_an_error_and_never_compared(self):
        fake, rec = self.run_one()
        self.assertEqual((rec["verdict"], rec["errors"]), ("error", ["no report written"]))
        self.assertNotIn("github", rec)
        self.assertEqual(fake.gh_calls, [])
        self.assertEqual(self.worktrees(), 1)

    def test_a_crashing_suite_is_recorded_and_the_worktree_removed(self):
        _, rec = self.run_one(report=RuntimeError("boom"))
        self.assertEqual((rec["verdict"], rec["errors"]), ("error", ["RuntimeError: boom"]))
        self.assertEqual(rec["finished"], NOW)
        self.assertEqual(self.worktrees(), 1)

    def test_an_unknown_sha_is_recorded_without_a_worktree(self):
        self.pend("f" * 40)
        fake = self.make(report=self.REPORT)
        self.assertEqual(fake.run_once(), f"ran {'f' * 40}: error")
        rec = json.loads((self.out / f"{'f' * 40}.json").read_text(encoding="utf-8"))
        self.assertTrue(rec["errors"][0].startswith("commit not found"))
        self.assertEqual(fake.suite_calls, [])


class TestGithubCompare(Base):
    def record(self, ids: list[str], verdict: str = "green") -> dict:
        return {"sha": self.shas[0], "branch": "ci/x", "verdict": verdict, "ids": ids, "duration": 50.0}

    def compare(self, rec: dict, gh) -> Fake:
        gh.sha = rec["sha"]
        fake = self.make(gh=gh)
        fake.compare(rec)
        return fake

    def test_equal_verdicts_and_id_sets(self):
        ids = [f"t.{i}" for i in range(8)]
        rec = self.record(ids)
        self.compare(rec, gh_complete([shard(i, ids[2 * i - 2:2 * i], wall=float(i)) for i in (1, 2, 3, 4)]))
        g = rec["github"]
        self.assertEqual((g["status"], g["run"], g["verdict"], g["verdict_equal"]), ("complete", 7, "green", True))
        self.assertEqual((g["ids_equal"], g["ids_hash"], g["ids_count"]), (True, shadow.ids_hash(ids), 8))
        self.assertEqual((g["duration"], g["counts"]["run"], g["checked"]), (4.0, 8, NOW))
        self.assertEqual(json.loads((self.out / f"{rec['sha']}.json").read_text(encoding="utf-8")), rec)

    def test_differing_ids_are_listed_at_most_fifty(self):
        local = [f"t.local{i:02}" for i in range(60)]
        rec = self.record(local + ["t.both"], verdict="red")
        self.compare(rec, gh_complete([shard(i, ["t.both", f"t.cloud{i}"]) for i in (1, 2, 3, 4)]))
        g = rec["github"]
        self.assertEqual((g["verdict_equal"], g["ids_equal"]), (False, False))
        self.assertEqual(g["ids_only_local"], local[:50])
        self.assertEqual(g["ids_only_github"], [])
        rec = self.record(["t.both", "t.local"])
        self.compare(rec, gh_complete([shard(i, ["t.both", f"t.cloud{i}"]) for i in (1, 2, 3, 4)]))
        self.assertEqual(rec["github"]["ids_only_local"], ["t.local"])
        self.assertEqual(rec["github"]["ids_only_github"], [f"t.cloud{i}" for i in (1, 2, 3, 4)])

    def test_a_missing_or_red_shard_makes_github_red(self):
        rec = self.record(["t.a"])
        self.compare(rec, gh_complete([shard(i, ["t.a"]) for i in (1, 2, 4)]))
        self.assertEqual((rec["github"]["verdict"], rec["github"]["verdict_equal"]), ("red", False))
        rec = self.record(["t.a"])
        self.compare(rec, gh_complete([shard(i, ["t.a"], green=i != 3) for i in (1, 2, 3, 4)]))
        self.assertEqual(rec["github"]["verdict"], "red")

    def test_pending_until_the_run_completes_then_compared_on_a_later_run(self):
        sha = self.shas[0]
        self.pend(sha)
        state = {"status": "in_progress"}

        def gh(argv):
            if argv[1:3] == ["run", "list"]:
                return 0, json.dumps([{"databaseId": 9, "headSha": sha, "status": state["status"],
                                       "conclusion": "", "workflowName": "tests"}])
            return gh_complete([shard(i, ["t.a"]) for i in (1, 2, 3, 4)])(argv)
        fake = self.make(report={"green": True, "dispatched": ["t.a"], "wall": 3.0}, gh=gh)
        fake.run_once()
        rec = json.loads((self.out / f"{sha}.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["github"], {"status": "pending", "run": 9, "checked": NOW})
        state["status"] = "completed"
        self.assertEqual(fake.run_once(), "idle: no new ci_pending sha")
        rec = json.loads((self.out / f"{sha}.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["github"]["status"], rec["github"]["verdict_equal"]), ("complete", True))
        calls = len(fake.gh_calls)
        fake.run_once()  # complete: never asked again
        self.assertEqual(len(fake.gh_calls), calls)
        self.assertEqual(len(fake.suite_calls), 1)

    def test_no_run_cancelled_and_gh_errors_are_recorded(self):
        rec = self.record(["t.a"])
        self.compare(rec, lambda argv: (0, "[]"))
        self.assertEqual(rec["github"]["status"], "pending")
        fake = self.compare(rec, lambda argv: (4, "gh: authentication required"))
        self.assertEqual(rec["github"]["error"], "gh: authentication required")
        self.assertEqual(len(fake.gh_calls), 1)  # list failed: no download tried
        self.compare(rec, lambda argv: (0, "not json"))
        self.assertTrue(rec["github"]["error"].startswith("JSONDecodeError"))
        fake = self.compare(rec, gh_complete([], conclusion="cancelled"))
        self.assertEqual(rec["github"], {"status": "cancelled", "run": 7, "checked": NOW})
        self.assertEqual(len(fake.gh_calls), 1)
        fake = self.compare(rec, gh_complete([]))
        self.assertEqual((rec["github"]["status"], fake.gh_calls), ("cancelled", []))

    def test_an_error_record_or_no_branch_is_not_sent_to_gh(self):
        rec = self.record(["t.a"], verdict="error")
        fake = self.compare(rec, gh_complete([]))
        self.assertEqual((fake.gh_calls, "github" in rec), ([], False))
        rec = {**self.record(["t.a"]), "branch": None}
        fake = self.compare(rec, gh_complete([]))
        self.assertEqual((rec["github"]["status"], fake.gh_calls), ("error", []))


class TestSummary(Base):
    def test_summary_counts_and_medians(self):
        self.out.mkdir()
        rows = [("a", 100.0, {"status": "complete", "verdict_equal": True, "ids_equal": True, "duration": 30.0}),
                ("b", 200.0, {"status": "complete", "verdict_equal": True, "ids_equal": False, "duration": 50.0}),
                ("c", 300.0, {"status": "complete", "verdict_equal": False, "ids_equal": False, "duration": 40.0}),
                ("d", 999.0, {"status": "pending"})]
        for name, duration, github in rows:
            (self.out / f"{name * 40}.json").write_text(
                json.dumps({"sha": name * 40, "duration": duration, "github": github}), encoding="utf-8")
        (self.out / "e.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(shadow.summary(self.out).splitlines(), [
            "local runs: 4", "SHAs compared: 3", "equal verdicts: 2/3", "equal id sets: 1/3",
            "median duration: local 200.0s vs GitHub 40.0s (slowest shard)"])

    def test_empty_summary(self):
        self.assertIn("median duration: local n/a vs GitHub n/a", shadow.summary(self.out))


if __name__ == "__main__":
    unittest.main()
