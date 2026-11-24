"""scripts/key1_check.py: the read-only verifier for the orchestrator's key1 decision."""
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "key1_check", Path(__file__).resolve().parents[1] / "scripts" / "key1_check.py")
k1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(k1)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd, *a):
    return subprocess.run([*GIT, *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(root, name, text):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")
    git(root, "add", name)
    git(root, "commit", "-m", f"{name} {text.strip()}")
    return git(root, "rev-parse", "HEAD")


def make_history(tmp, src_file="a.txt"):
    """Build the source and candidate histories once for the class (src_file: the file the source commit adds)."""
    root = Path(tmp)
    git(root, "init", "-q", "-b", "main")
    base = commit(root, "a.txt", "1\n")
    git(root, "checkout", "-q", "-b", "other")
    other = commit(root, "b.txt", "other\n")
    git(root, "checkout", "-q", "-b", "src", "main")
    src = commit(root, src_file, "2\n")
    git(root, "checkout", "-q", "-b", "cand", "main")
    git(root, "cherry-pick", src)
    cand = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    return SimpleNamespace(root=root, base=base, src=src, cand=cand, other=other)


def make(tmp, history, chain=("CLREV", "CDX", "INT"), status="PASS", findings="none",
         files=("a.txt",), on_main=False, review_commit=None, ci_branch="ci/int-x",
         shared_git=None, tier=None, src_file="a.txt"):
    """Write fresh reports and backlog and give mutable cases their own repo."""
    root = Path(tmp)
    if src_file != "a.txt":  # a different source file needs its own history
        history, shared_git = make_history(tmp, src_file), None
    elif shared_git is None:
        shutil.copytree(history.root, root, dirs_exist_ok=True)
    src, cand = history.src, history.cand
    if on_main:
        git(root, "update-ref", "refs/remotes/origin/main", cand)
    rep = root / "build" / "reports"
    rep.mkdir(parents=True)
    (rep / "INT-X.md").write_text(
        f"STATUS: CI_PENDING\nCANDIDATE SHA: {cand}\nCI BRANCH: {ci_branch}\n"
        f"PICKS:\n  {src} -> {cand} (patch-id equal)\n", encoding="utf-8")
    for r in ("CLREV", "CDX")[: 2 if "CDX" in chain else 1]:
        (rep / f"{r}-X.md").write_text(
            f"STATUS: {status}\nCOMMIT: {review_commit or src}\nFINDINGS: {findings}\n", encoding="utf-8")
    (root / "build" / "conveyor").mkdir()
    (root / "build" / "conveyor" / "backlog.json").write_text(
        json.dumps({"slices": [{"id": "X", "files": list(files), "chain": list(chain),
                                **({"tier": tier} if tier else {})}]}), encoding="utf-8")
    return SimpleNamespace(root=root, base=history.base, src=src, cand=cand,
                           other=history.other, git=shared_git or k1.make_git(root))


def ci(sha, conclusion="success", status="completed"):
    runs = [{"headSha": sha, "conclusion": conclusion, "status": status, "workflowName": "tests", "url": "u"}]
    return lambda args: (0, json.dumps(runs))


def snapshot(root):
    return {str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file() and ".git" not in p.parts}


class Key1CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.history = make_history(directory.name)
        run_git = k1.make_git(cls.history.root)

        @lru_cache(maxsize=None)
        def cached_git(args, stdin):
            return run_git(args, stdin)

        # Findings cases vary only the reports and reuse Git reads of the template.
        cls.shared_git = staticmethod(lambda args, stdin=None: cached_git(tuple(args), stdin))
        cls.addClassCleanup(cached_git.cache_clear)

    def run_lane(self, fx, gh=None, operator_tool=False):
        out, ok = k1.verify("INT-X", fx.root, fx.git, gh or ci(fx.cand), operator_tool)
        return "\n".join(out), ok

    def fx(self, *, share_history=False, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return make(d.name, self.history, shared_git=self.shared_git if share_history else None, **kw)

    def test_all_pass_prints_approval_name_and_writes_nothing(self):
        fx = self.fx()
        before = snapshot(fx.root)
        text, ok = self.run_lane(fx)
        self.assertTrue(ok, text)
        self.assertNotIn("FAIL", text)
        self.assertIn(f"push-{fx.cand}-int-x", text)
        self.assertEqual(snapshot(fx.root), before)
        self.assertFalse((fx.root / "build" / "approvals").exists())

    def test_already_on_main_and_clrev_only_chain_passes(self):
        text, ok = self.run_lane(self.fx(chain=("CLREV", "INT"), on_main=True))
        self.assertTrue(ok, text)

    def test_candidate_already_landed_below_origin_main_passes(self):
        fx = self.fx()
        git(fx.root, "checkout", "-q", "cand")
        later = commit(fx.root, "c.txt", "later\n")
        git(fx.root, "update-ref", "refs/remotes/origin/main", later)
        text, ok = self.run_lane(fx)
        self.assertTrue(ok, text)
        self.assertIn("already contained", text)

    def test_not_fast_forward_fails(self):
        fx = self.fx()
        git(fx.root, "update-ref", "refs/remotes/origin/main", fx.other)
        text, ok = self.run_lane(fx)
        self.assertFalse(ok)
        self.assertIn("FAIL 1", text)

    def test_review_commit_with_other_patch_id_fails(self):
        fx = self.fx()
        for r in ("CLREV", "CDX"):
            p = fx.root / "build" / "reports" / f"{r}-X.md"
            p.write_text(p.read_text().replace(fx.src, fx.other), encoding="utf-8")
        text, ok = self.run_lane(fx)
        self.assertFalse(ok)
        self.assertIn("FAIL 2", text)

    def test_missing_cdx_report_fails_but_clrev_only_chain_does_not_need_it(self):
        fx = self.fx()
        (fx.root / "build" / "reports" / "CDX-X.md").unlink()
        text, ok = self.run_lane(fx)
        self.assertFalse(ok)
        self.assertIn("FAIL 3", text)

    def test_findings_and_status_gate(self):
        # real MINOR format: a count plus F-lines; WARN only with --operator-tool
        minor = "1\nF1 MINOR: a.py:1 - nit - fix\nF2 NIT: b.py:2 - style"
        text, ok = self.run_lane(self.fx(findings=minor))
        self.assertFalse(ok)
        self.assertIn("FAIL 3", text)
        text, ok = self.run_lane(self.fx(findings=minor), operator_tool=True)
        self.assertTrue(ok, text)
        self.assertIn("WARN 3", text)
        text, ok = self.run_lane(self.fx(status="FAIL"), operator_tool=True)
        self.assertFalse(ok)
        text, ok = self.run_lane(self.fx(findings="none\nSTATUS: BLOCKED"))
        self.assertIn("FAIL 3", text)

    def test_findings_none_with_a_finding_line_fails(self):
        for findings in ("none\nF1 MAJOR: x", "none\n- F1 BLOCKER: x", "none\n* MINOR: x","none\n> MAJOR: x", "none\n  P0 crash",
                         "none\nFINDINGS: 2", "none\nfindings: none", "nonesuch", "", "none.", "NONE",
                         # decorated variants (F2 probe): numbering, zero-width, spaced/fullwidth colon, bullets, bold
                         "none\n1. F1 MAJOR bug", "none\nF1​ MAJOR bug", "none\nFINDINGS : F1 MAJOR",
                         "none\n- FINDINGS: F1 BLOCKER", "none\nFINDINGS： F1 MAJOR", "none\n**STATUS: BLOCKED**",
                         "none\nSTATUS : BLOCKED"):
            for op in (False, True):
                with self.subTest(findings=findings, operator_tool=op):
                    text, ok = self.run_lane(self.fx(findings=findings, share_history=True),
                                             operator_tool=op)
                    self.assertFalse(ok, text)
                    self.assertIn("FAIL 3", text)

    def test_operator_tool_warns_only_for_classified_minor(self):
        for findings in ("1\nF1 MINOR: x\nF2 MAJOR: y", "1\nf1 minor: x", "1\nF1 MINΟR: x",
                         "1\nF1 MINOR: x (was MAJOR)", "1\nF1 MINOR: x\nBLOCKER: y", "1\nF1: x", "MINOR: nit",
                         "1\nF1 MINOR: x\n1. F2 МAJOR x", "1\nF2 MINOR (really MEDIUM, data loss)",
                         "1\nF1 MINOR x\nblocking: severe data loss"):
            with self.subTest(findings=findings):
                text, ok = self.run_lane(self.fx(findings=findings, share_history=True),
                                         operator_tool=True)
                self.assertFalse(ok, text)
                self.assertIn("FAIL 3", text)

    def test_u1rule1_t1_product_minor_warns_without_operator_tool(self):
        """U1RULE1 (Salih 11-22): a T1 slice whose diff is product code passes check 3 with MINOR/NIT as WARN."""
        minor = "1\nF1 MINOR: a.txt:1 - nit - fix\nF2 NIT: a.txt:2 - style"
        text, ok = self.run_lane(self.fx(findings=minor, tier="T1"))
        self.assertTrue(ok, text)
        self.assertIn("WARN 3", text)
        self.assertIn("POLISH.md", text)

    def test_u1rule1fix_backlog_build_reports_entry_is_ignored_like_chain_verify(self):
        """U1RULE1FIX: backlog files list build/reports/<id>.md by convention; chain_verify drops it, so key1 does too."""
        minor = "1\nF1 MINOR: a.txt:1 - nit - fix"
        for files in (("a.txt", "build/reports/X.md"), ("a.txt", "build\\reports\\X.md")):
            with self.subTest(files=files):
                text, ok = self.run_lane(self.fx(findings=minor, tier="T1", files=files))
                self.assertTrue(ok, text)
                self.assertIn("WARN 3", text)
        # only the exact build/reports/ prefix without ".." is dropped; anything else keeps the chain strict
        for files in (("a.txt", "build/reports/../scripts/x.py"), ("a.txt", "Build/Reports/X.md"),
                      ("a.txt", "build/reports-old/X.md"), ("a.txt", "build/reports/X.md", "jarvis/auth.py"),
                      ("a.txt", "build/reports/a..b"), ("a.txt", "build/reports/x y"), ("a.txt", "build/reports/x "),
                      ("a.txt", "build/reports/а.md"), ("a.txt", "build/reports/x\x00y"),
                      ("a.txt", "build/reports/x\n")):
            with self.subTest(files=files):
                text, ok = self.run_lane(self.fx(findings=minor, tier="T1", files=files))
                self.assertFalse(ok, text)
                self.assertIn("FAIL 3", text)

    def test_u1rule1fix2_same_report_table_as_chain_verify(self):
        """U1RULE1FIX2: polish_ok drops exactly what chain_verify drops: build/reports/<NAME>.md, nothing nested."""
        from tests.test_conveyor import REPORT_COUNTED, REPORT_EXEMPT
        minor = "1\nF1 MINOR: a.txt:1 - nit - fix"
        for p in REPORT_EXEMPT:
            with self.subTest(p=p):
                self.assertTrue(k1.conveyor.chain_report_file(p))
                text, ok = self.run_lane(self.fx(findings=minor, tier="T1", files=("a.txt", p)))
                self.assertTrue(ok, text)
                self.assertIn("WARN 3", text)
        for p in REPORT_COUNTED:
            with self.subTest(p=p):
                self.assertFalse(k1.conveyor.chain_report_file(p))
                text, ok = self.run_lane(self.fx(findings=minor, tier="T1", files=("a.txt", p)))
                self.assertFalse(ok, text)
                self.assertIn("FAIL 3", text)

    def test_u1rule1_strict_classes_and_major_still_fail(self):
        minor = "1\nF1 MINOR: a.txt:1 - nit - fix"
        for kw in ({"tier": "T2"}, {}, {"tier": "T1", "src_file": "scripts/x.py", "files": ("a.txt", "scripts/x.py")},
                   {"tier": "T1", "files": ("a.txt", "jarvis/auth.py")},
                   {"tier": "T1", "findings": "1\nF1 MAJOR: a.txt:1 - bug"},
                   {"tier": "T1", "findings": "1\nF1 MINOR: x (really major)"}):
            with self.subTest(**kw):
                text, ok = self.run_lane(self.fx(**{"findings": minor, **kw}))
                self.assertFalse(ok, text)
                self.assertIn("FAIL 3", text)

    def test_u1rule1fix3_touched_names_reach_the_gates_unquoted_and_unsplit(self):
        """U1RULE1FIX3: git quotes non-ASCII names and .split() cut names at a space; both read NUL-separated now."""
        minor = "1\nF1 MINOR: a.txt:1 - nit - fix"
        # a bare star grant: a quoted name used to hide a protected path from chain_strict (polish_ok True -> WARN)
        for name in ("scripts/çonveyor.py", "data/a b.txt"):
            with self.subTest(name=name):
                text, ok = self.run_lane(self.fx(findings=minor, tier="T1", src_file=name, files=("*",)))
                self.assertFalse(ok, text)
                self.assertIn("FAIL 3", text)
                self.assertIn("PASS 5", text)
        # an exact grant of a space or non-ASCII product name: no stray half-names, the chain warns
        for name in ("jarvis/a b.py", "jarvis/ça va.py"):
            with self.subTest(name=name):
                text, ok = self.run_lane(self.fx(findings=minor, tier="T1", src_file=name, files=("a.txt", name)))
                self.assertTrue(ok, text)
                self.assertIn("WARN 3", text)
                self.assertIn("PASS 5", text)

    def test_ci_must_be_success_on_exactly_the_candidate(self):
        fx = self.fx()
        for gh in (ci(fx.cand, "failure"), ci(fx.cand, None, "in_progress"), ci("0" * 40),
                   lambda args: (1, "boom")):
            text, ok = self.run_lane(fx, gh)
            self.assertFalse(ok)
            self.assertIn("FAIL 4", text)

    def test_file_outside_source_slice_files_fails(self):
        text, ok = self.run_lane(self.fx(files=("other.txt",)))
        self.assertFalse(ok)
        self.assertIn("FAIL 5", text)

    def test_bad_ci_branch_fails_approval_name(self):
        text, ok = self.run_lane(self.fx(ci_branch="main"))
        self.assertFalse(ok)
        self.assertIn("FAIL 6", text)

    # KEY1PATH1: --write / --manual are the only key-1 writers
    def write(self, fx, gh=None, **kw):
        out, ok = k1.run("INT-X", fx.root, k1.make_git(fx.root), gh or ci(fx.cand), **kw)
        return "\n".join(out), ok, fx.root / "build" / "approvals" / f"push-{fx.cand}-int-x"

    def test_write_on_pass_writes_header_the_conveyor_accepts(self):
        fx = self.fx()
        text, ok, p = self.write(fx, write=True)
        self.assertTrue(ok, text)
        self.assertIsNone(k1.conveyor.key1_refusal(p))
        self.assertTrue(p.read_text(encoding="ascii").startswith(
            f"KEY1 v1 sha={fx.cand} ci=ci/int-x lane=INT-X by=key1_check "
            "checks=ci,ff,files,name,patch-id,reviews at="))
        self.assertEqual([q.name for q in p.parent.iterdir()], [p.name])  # no temp left behind
        text, ok, _ = self.write(fx, write=True)  # never overwrites
        self.assertFalse(ok, text)

    def test_write_refused_on_warn_or_fail(self):
        minor = "1\nF1 MINOR: a.py:1 - nit - fix"
        fx = self.fx(findings=minor)
        real = k1.verify  # KEY1PATH1FIX2: run() never asks verify for WARN on --write; force a real WARN verdict
        with mock.patch.object(k1, "verify", lambda lane, root, git, gh, operator_tool=False, rep=None:
                               real(lane, root, git, gh, True, rep)):
            text, ok, p = self.write(fx, write=True)
        self.assertIn("WARN", text)
        self.assertFalse(ok, text)
        self.assertFalse(p.exists())
        fx = self.fx()
        text, ok, p = self.write(fx, ci(fx.cand, "failure"), write=True)
        self.assertFalse(ok, text)
        self.assertFalse(p.exists())

    def test_manual_skips_reviews_but_not_ci(self):
        fx = self.fx(findings="1\nF1 MINOR: a.py:1 - nit - fix")
        text, ok, p = self.write(fx, manual="stack review ok")
        self.assertTrue(ok, text)
        self.assertIsNone(k1.conveyor.key1_refusal(p))
        self.assertIn("by=manual reason=stack review ok checks=ci,ff,files,name,patch-id at=", p.read_text())
        fx = self.fx()
        text, ok, p = self.write(fx, ci(fx.cand, "failure"), manual="stack review ok")
        self.assertFalse(ok, text)
        self.assertFalse(p.exists())

    def test_manual_bad_reason_refused(self):
        fx = self.fx()
        for reason in ("", "a=b", "x" * 121, " lead", "trail ", "ünï", "a\nb"):
            with self.subTest(reason=reason):
                text, ok, p = self.write(fx, manual=reason)
                self.assertFalse(ok, text)
                self.assertFalse(p.exists())

    # KEY1PATH1FIX negative tests (V7-V9)
    def test_write_uses_the_verified_report_not_a_second_read(self):  # V7 / F2 TOCTOU
        fx = self.fx()
        rp = fx.root / "build" / "reports" / "INT-X.md"

        def gh(args):  # the report changes while verify() waits on the network
            rp.write_text(rp.read_text(encoding="utf-8").replace(fx.cand, fx.other), encoding="utf-8")
            return ci(fx.cand)(args)
        text, ok, p = self.write(fx, gh, write=True)
        self.assertTrue(ok, text)
        self.assertIn(f"sha={fx.cand} ", p.read_text(encoding="ascii"))
        self.assertFalse((p.parent / f"push-{fx.other}-int-x").exists())

    def test_writer_ignores_planted_temp_and_never_overwrites(self):  # V8 / F3
        import os
        import threading
        from unittest import mock
        d = Path(self.fx().root) / "build" / "approvals"
        d.mkdir()
        planted = d / f".key1-{os.getpid()}-{threading.get_ident()}.tmp"  # the old predictable name
        planted.write_text("planted", encoding="ascii")
        sha, ci_ = "a" * 40, "ci/int-x"
        p = k1.conveyor.write_key1(d, sha, ci_, "INT-X", "key1_check", ("ci",))
        self.assertEqual(planted.read_text(encoding="ascii"), "planted")
        self.assertIsNone(k1.conveyor.key1_refusal(p))
        before = p.read_bytes()
        # a target appearing after the exists() check is still never replaced
        with mock.patch.object(k1.conveyor.Path, "exists", return_value=False):
            with self.assertRaises(FileExistsError):
                k1.conveyor.write_key1(d, sha, ci_, "INT-X", "conveyor", ("approval-for",))
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(sorted(q.name for q in d.iterdir()), sorted([p.name, planted.name]))
        # a locked temp (AV) after a successful link still reports the key that is on disk
        with mock.patch.object(k1.conveyor.Path, "unlink", side_effect=PermissionError):
            q = k1.conveyor.write_key1(d, "c" * 40, ci_, "INT-X", "key1_check", ("ci",))
        self.assertIsNone(k1.conveyor.key1_refusal(q))

    def test_refusal_checks_the_whole_first_line(self):  # V9 / F4
        d = Path(self.fx().root) / "build" / "approvals"
        p = k1.conveyor.write_key1(d, "b" * 40, "ci/int-x", "INT-X", "key1_check", ("ci",))
        h = p.read_text(encoding="ascii").rstrip("\n")
        for body in (h + " " + "x" * 600 + "\n", h + "x" * 2000, h + "x" * 2000 + "\n"):
            with self.subTest(n=len(body)):
                p.write_text(body, encoding="ascii", newline="\n")
                self.assertIsNotNone(k1.conveyor.key1_refusal(p))
        p.write_text(h, encoding="ascii")  # no trailing newline is still a whole line
        self.assertIsNone(k1.conveyor.key1_refusal(p))


if __name__ == "__main__":
    unittest.main()
