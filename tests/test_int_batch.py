"""INTBATCH1: scripts/int_batch.py against a throwaway git repo (no network)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "int_batch.py"
FIXED = "2026-01-01T00:00:00+00:00"
GIT_ENV = dict(
    os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_AUTHOR_DATE=FIXED,
    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
    GIT_COMMITTER_DATE=FIXED, PYTHONIOENCODING="utf-8",
)
CONFLICT = (
    "CONFLICT: the EXPECTED files only. Resolve by keeping BOTH sides' behaviour "
    "(never drop a main-side guard, test or duration record; never drop a pick's "
    "change); scripts/test_durations.json conflicts: keep every record from both "
    "sides. List every resolved hunk under RESOLVED. Any conflict outside these "
    "files -> BLOCKED. Never add a commit of your own."
)
MISSING_SHA = "0123456789abcdef0123456789abcdef01234567"


def ev(src, step, sha, ts="2026-11-20T10:00:00"):
    return json.dumps({"ts": ts, "event": "chain", "src": src, "step": step, "sha": sha})


class IntBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.repo = Path(cls._tmp.name) / "repo"
        cls.repo.mkdir()
        cls.git("init", "-q", "-b", "main")
        cls.git("config", "core.autocrlf", "false")
        s = {}
        s["base"] = cls.commit("base", {
            "jarvis/__init__.py": "",
            "jarvis/core.py": "X = 1\n",
            "scripts/tool.py": "print('tool')\n",
            "scripts/other.py": "A = 1\n",
            "tests/test_core.py": "from jarvis.core import X\n",
            "tests/test_tool.py": "TOOL = 'scripts/tool.py'\n",
            "tests/test_unrelated.py": "import os\n",
            "scripts/test_durations.json": json.dumps({"classes": {
                "tests.test_core.CoreTest": 1.0, "tests.test_unrelated.U": 1.0}}),
        })
        cls.git("checkout", "-q", "-b", "a")
        s["a1"] = cls.commit("a1", {"jarvis/core.py": "X = 2\n"})
        s["a2"] = cls.commit("a2", {
            "tests/test_new.py": "import unittest\n",
            "scripts/test_durations.d/A.json": json.dumps(
                {"version": 1, "classes": {"tests.test_new.NewTest": 1.0}}),
            "build/junk.txt": "ignored\n",
        })
        cls.git("checkout", "-q", "-b", "b", s["base"])
        s["b1"] = cls.commit("b1", {"scripts/other.py": "A = 2\n"})
        s["b2"] = cls.commit("b2", {"jarvis/core.py": "X = 1\nY = 1\n",
                                    "scripts/tool.py": "print('tool2')\n"})
        cls.git("checkout", "-q", "-b", "c", s["base"])
        s["c1"] = cls.commit("c1", {".github/workflows/ci.yml": "on: push\n"})
        cls.git("checkout", "-q", "main")
        cls.commit("m0", {"README.md": "main\n"})
        cls.git("cherry-pick", s["b1"])
        s["b1_main"] = cls.git("rev-parse", "HEAD")
        cls.git("update-ref", "refs/remotes/origin/main", "HEAD")
        cls.shas = s
        events = cls.repo / "build/conveyor/events.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text("\n".join([
            ev("OLD", "TRAIN-READY", s["c1"], ts="2026-01-01T00:00:00"),
            "this is not json",
            "[1, 2]",
            json.dumps({"event": "chain", "step": "TRAIN-READY", "ts": "2026-11-20T10:00:00"}),
            json.dumps({"event": "chain", "src": "Q", "step": "TRAIN-READY", "ts": "nope"}),
            json.dumps({"event": "lane", "src": "A", "step": "x"}),
            ev("A", "REVIEW", s["a1"], ts="2026-11-20T09:00:00"),
            ev("A", "TRAIN-READY", s["a2"]),
            ev("B", "TRAIN-READY", s["b2"]),
            ev("E", "TRAIN-READY", s["a1"]),
            ev("E", "TRAIN-LANDED", s["a1"], ts="2026-11-21T10:00:00"),
            ev("F", "TRAIN-READY", s["base"]),
            ev("D", "TRAIN-READY", MISSING_SHA),
            ev("G", "TRAIN-READY", "not-a-sha"),
        ]) + "\n", encoding="utf-8")

    @classmethod
    def git(cls, *args):
        r = subprocess.run(["git", *args], cwd=cls.repo, env=GIT_ENV,
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()

    @classmethod
    def commit(cls, msg, files):
        for rel, body in files.items():
            p = cls.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8", newline="\n")
        cls.git("add", "--", *files)
        cls.git("commit", "-q", "-m", msg)
        return cls.git("rev-parse", "HEAD")

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), "--since", "2026-11-01T00:00:00",
                               *args], cwd=self.repo, env=GIT_ENV, capture_output=True,
                              text=True, encoding="utf-8")

    def stderr_line(self, r, label):
        lines = [ln for ln in r.stderr.splitlines() if ln.startswith(label + ":")]
        self.assertEqual(len(lines), 1, r.stderr)
        return lines[0][len(label) + 1:].strip()

    def tree_digest(self):
        h = hashlib.sha256()
        for p in sorted(self.repo.rglob("*")):
            h.update(str(p.relative_to(self.repo)).encode())
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()

    # acceptance

    def test_prompt_format_stacks_in_order_patch_id_dedupe(self):
        out = Path(self._tmp.name) / "out" / "INT7.md"
        out.parent.mkdir(exist_ok=True)
        r = self.run_tool("--name", "INT7", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.shas
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 9)
        self.assertEqual(lines[0], "@jarvis lane=INT7 model=sonnet effort=medium base=main")
        self.assertEqual(lines[1], "LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE + INT)")
        self.assertIn("Stacks in pick order: A (2 commits); B (1 commit).", lines[2])
        self.assertIn(f"expected {s['b1_main'][:7]} or a descendant", lines[2])
        # b1 is on main as a cherry-pick with another sha: dropped by patch-id
        self.assertNotEqual(s["b1"], s["b1_main"])
        self.assertEqual(lines[3], f"PICKS: {s['a1']} {s['a2']} {s['b2']}")
        self.assertEqual(lines[4], "EXPECTED: jarvis/core.py, scripts/test_durations.d/A.json, "
                                   "scripts/test_durations.json, scripts/tool.py, "
                                   "tests/test_new.py")
        self.assertEqual(lines[5], CONFLICT)
        self.assertEqual(lines[6], "MODULES: tests.test_core tests.test_new tests.test_tool")
        self.assertTrue(lines[7].startswith(
            "Also run python scripts/test_parallel.py --require-durations <MODULES>"))
        self.assertEqual(lines[8], "CI BRANCH: ci/int7")
        self.assertIn("STACK: B picks=1 on_main=1", r.stderr)
        entry = json.loads(r.stdout.strip())
        self.assertEqual(entry["id"], "INT7")
        self.assertEqual(entry["tier"], "T1")
        self.assertFalse(entry["approved"])
        self.assertEqual(entry["prompt"], "build/conveyor/prompts/INT7.md")
        self.assertEqual(entry["files"], ["@main-checkout"])

    def test_overlap_listed_with_stacks(self):
        r = self.run_tool("--check-only")
        self.assertEqual(self.stderr_line(r, "OVERLAP"), "jarvis/core.py (A, B)")

    def test_landed_and_all_on_main_skipped(self):
        r = self.run_tool("--check-only")
        skipped = self.stderr_line(r, "SKIPPED")
        self.assertIn("E (last step TRAIN-LANDED)", skipped)
        self.assertIn("F (all on main)", skipped)
        self.assertNotIn("OLD", r.stderr)

    def test_missing_duration_record_exit_3_and_d_file_counts(self):
        r = self.run_tool("--check-only")
        self.assertEqual(r.returncode, 3, r.stderr)
        # tests.test_new is recorded only in a .d file added by pick a2
        self.assertEqual(self.stderr_line(r, "MISSING DURATIONS"), "tests.test_tool")
        r = self.run_tool("--check-only", "--exclude", "B")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stderr_line(r, "MISSING DURATIONS"), "none")

    def test_importers_from_import_and_path_literal(self):
        out = self.run_tool("--exclude", "B").stdout  # A: jarvis/core.py via from-import
        self.assertIn("MODULES: tests.test_core tests.test_new\n", out)
        out = self.run_tool("--exclude", "A", "--name", "INT8").stdout
        # B: jarvis/core.py (from-import) + scripts/tool.py ('scripts/tool.py' literal)
        self.assertIn("MODULES: tests.test_core tests.test_tool\n", out)
        self.assertNotIn("test_unrelated", out)

    def test_tier_t2_for_workflow_path(self):
        r = self.run_tool("--since", "2099-01-01T00:00:00", "--include",
                          f"C={self.shas['c1']}", "--name", "INT9")
        self.assertEqual(r.returncode, 0, r.stderr)
        entry = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(entry["tier"], "T2")
        self.assertIn("EXPECTED: .github/workflows/ci.yml, scripts/test_durations.json", r.stdout)

    # negative

    def test_malformed_lines_skipped_and_counted(self):
        r = self.run_tool("--check-only")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.stderr_line(r, "MALFORMED"), "4")

    def test_unresolvable_sha_reported_not_picked(self):
        r = self.run_tool("--name", "INT10")
        self.assertEqual(r.returncode, 0, r.stderr)
        unresolved = self.stderr_line(r, "UNRESOLVED")
        self.assertIn(f"D ({MISSING_SHA})", unresolved)
        self.assertIn("G (not-a-sha)", unresolved)
        self.assertNotIn(MISSING_SHA, r.stdout.splitlines()[3])

    def test_include_non_hex_sha_exit_2(self):
        for bad in ("X=zzzz1234", "X", "=abcdef1", "X=12"):
            r = self.run_tool("--include", bad)
            self.assertEqual(r.returncode, 2, (bad, r.stderr))

    def test_never_writes_outside_out(self):
        before = self.tree_digest()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.md"
            self.run_tool("--out", str(out), "--name", "INT11")
            self.run_tool("--check-only")
            self.run_tool("--name", "INT12")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["p.md"])
        self.assertEqual(self.tree_digest(), before)


if __name__ == "__main__":
    unittest.main()
