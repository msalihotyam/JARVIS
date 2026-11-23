"""scripts/key1_check.py: the read-only verifier for the orchestrator's key1 decision."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "key1_check", Path(__file__).resolve().parents[1] / "scripts" / "key1_check.py")
k1 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(k1)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd, *a):
    return subprocess.run([*GIT, *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(root, name, text):
    (root / name).write_text(text, encoding="utf-8")
    git(root, "add", name)
    git(root, "commit", "-m", f"{name} {text.strip()}")
    return git(root, "rev-parse", "HEAD")


def make(tmp, chain=("CLREV", "CDX", "INT"), status="PASS", findings="none", files=("a.txt",),
         on_main=False, review_commit=None, ci_branch="ci/int-x"):
    """origin/main=base; source commit S on a branch; candidate C = S cherry-picked onto base
    (same patch-id). Reports and backlog live under <root>/build like the conveyor's."""
    root = Path(tmp)
    git(root, "init", "-q", "-b", "main")
    base = commit(root, "a.txt", "1\n")
    git(root, "checkout", "-q", "-b", "other")
    other = commit(root, "b.txt", "other\n")
    git(root, "checkout", "-q", "-b", "src", "main")
    src = commit(root, "a.txt", "2\n")
    git(root, "checkout", "-q", "-b", "cand", "main")
    git(root, "cherry-pick", src)
    cand = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", cand if on_main else base)
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
        json.dumps({"slices": [{"id": "X", "files": list(files), "chain": list(chain)}]}), encoding="utf-8")
    return SimpleNamespace(root=root, base=base, src=src, cand=cand, other=other)


def ci(sha, conclusion="success", status="completed"):
    runs = [{"headSha": sha, "conclusion": conclusion, "status": status, "workflowName": "tests", "url": "u"}]
    return lambda args: (0, json.dumps(runs))


def snapshot(root):
    return {str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file() and ".git" not in p.parts}


class Key1CheckTests(unittest.TestCase):
    def run_lane(self, fx, gh=None, operator_tool=False):
        out, ok = k1.verify("INT-X", fx.root, k1.make_git(fx.root), gh or ci(fx.cand), operator_tool)
        return "\n".join(out), ok

    def fx(self, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return make(d.name, **kw)

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
                    text, ok = self.run_lane(self.fx(findings=findings), operator_tool=op)
                    self.assertFalse(ok, text)
                    self.assertIn("FAIL 3", text)

    def test_operator_tool_warns_only_for_classified_minor(self):
        for findings in ("1\nF1 MINOR: x\nF2 MAJOR: y", "1\nf1 minor: x", "1\nF1 MINΟR: x",
                         "1\nF1 MINOR: x (was MAJOR)", "1\nF1 MINOR: x\nBLOCKER: y", "1\nF1: x", "MINOR: nit",
                         "1\nF1 MINOR: x\n1. F2 МAJOR x", "1\nF2 MINOR (really MEDIUM, data loss)",
                         "1\nF1 MINOR x\nblocking: severe data loss"):
            with self.subTest(findings=findings):
                text, ok = self.run_lane(self.fx(findings=findings), operator_tool=True)
                self.assertFalse(ok, text)
                self.assertIn("FAIL 3", text)

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
        text, ok, p = self.write(fx, write=True, operator_tool=True)
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
