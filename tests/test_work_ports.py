"""PM26A S3: the judgement ports read only the commit pinned in the JARVIS repo."""

import hashlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis import stage_git, work_driver, work_ports
from jarvis.memory import SQLiteMemoryStore
from jarvis.work_checks import CheckResult
from jarvis.work_ledger import EventKind, RunState, StepState, WorkRun, WorkStep, step_key
from jarvis.work_ports import _changes, build_ports
from jarvis.work_regress import TestResult
from jarvis.write_stage import StageError, StageManager
from jarvis.workers.base import WorkerResult

RUN = "run1"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
ACCEPTED = types.SimpleNamespace(outcome="accepted", logged=(), blocking=(), reason="")


def _check(stdout, *, exit_code=0, timed_out=False, truncated=False):
    return CheckResult(("python", "-m", "unittest"), "unittest", exit_code, 0.1, stdout, "",
                       truncated, timed_out)


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEMP"),
                                           ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.ticks = 0
        self.manager = StageManager(self.home / "stages")
        self.repo = self.manager.create_repo(self.home / "project")
        self.db = self.home / "work.db"
        self.check_outputs = ["Ran 3 tests in 0.1s\n\nOK\n"] * 8
        self.check_effect = None
        self.exported = []
        self.hashed = []
        self.diffs = []
        self.ports = self.build()
        self.step = WorkStep(RUN, step_key(RUN, 1, "one"), 1, "one", StepState.PENDING,
                             self.clock())

    def clock(self):
        self.ticks += 1
        return T0 + timedelta(seconds=self.ticks)

    def git(self, cwd, *args, input=None):
        data = input.encode() if input is not None else None
        return self.manager._git(cwd, *args, input=data).decode("utf-8", "replace")

    def build(self, targets=None, run_id=RUN):
        def fake_run_check(argv, *, stage, jarvis_repo):
            allowed = stage / "allowed.txt"
            self.exported.append((stage, (stage / ".git").exists(),
                                  allowed.read_text() if allowed.exists() else None))
            if self.check_effect:
                self.check_effect(stage)
            return _check(self.check_outputs.pop(0))

        def fixture_hash(directory):
            allowed = directory / "allowed.txt"
            self.hashed.append((directory, (directory / ".git").exists(),
                                allowed.read_text() if allowed.exists() else None))
            return "a" * 64

        def review(stage, diff):
            self.diffs.append(diff)
            return ACCEPTED

        return build_ports(
            store=SQLiteMemoryStore(self.db), manager=self.manager, target=self.repo,
            lease_target="project/", run_id=run_id, boot_id="boot-1", clock=self.clock,
            lease=timedelta(minutes=5), targets=targets or {"allowed.txt": "file"},
            jarvis_repo=False, checks=[("python", "-m", "unittest")],
            base_result=TestResult(3, frozenset()), locked_fixture_hash="a" * 64,
            fixture_hash=fixture_hash, review=review, plan_hash="p" * 64, worker=None,
            route=None, task=None, budget_exhausted=None, needs_input=None, verify_cwd=None,
            link_rescan=None, run_check=fake_run_check)

    def commit_in(self, path, name, text):
        file = Path(path) / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        self.git(path, "add", "--all")
        tree = self.git(path, "write-tree").strip()
        parent = self.git(path, "rev-parse", "HEAD").strip()
        sha = self.git(path, "commit-tree", tree, "-p", parent, input="test change\n").strip()
        self.git(path, "update-ref", "HEAD", sha, parent)
        return sha

    def based_stage(self):
        self.commit_in(self.repo, "keep.txt", "keep\n")
        self.commit_in(self.repo, "sub/x.txt", "x\n")
        return self.ports.stage_create(self.step)

    def jarvis_repo(self, stage):
        return self.manager.root.parent / (self.manager.root.name + "-repos") / RUN

    def pin_ref(self, stage):
        return f"refs/jarvis/judged/{RUN}/{self.step.key}"

    def pinned(self, stage):
        return stage_git.run(self.jarvis_repo(stage), "for-each-ref", "--format=%(objectname)",
                             self.pin_ref(stage)).decode("ascii").strip()

    def common_dir(self, stage):
        common = Path(self.git(stage.path, "rev-parse", "--git-common-dir").strip())
        return common if common.is_absolute() else stage.path / common

    def ref_file(self, stage):
        return self.common_dir(stage) / "refs" / "heads" / stage.branch

    def later_results(self, stage):
        results = self.ports.run_checks(stage)
        verdict = self.ports.regress(results)
        findings = self.ports.review(stage)
        receipt = self.ports.evidence(self.step, stage, results, verdict)
        return (verdict.passed, findings, receipt.diff_sha, receipt.verdict,
                self.ports.tree_hash(stage))


class ScopeTests(Base):
    def test_scope_is_judged_from_the_jarvis_repo(self):
        stage = self.based_stage()
        tip = self.commit_in(stage.path, "allowed.txt", "ok\n")
        (stage.path / "uncommitted.txt").write_text("never judged\n")
        self.assertEqual(self.ports.scope_check(stage), [])
        self.assertEqual(self.pinned(stage), tip)
        repo = self.jarvis_repo(stage)
        self.assertEqual(stage_git.run(repo, "cat-file", "-t", tip).strip(), b"commit")
        self.assertEqual(_changes(repo, stage.base, tip)[0]["path"], "allowed.txt")

    def test_out_of_scope_commit_is_refused_and_pinned_for_later_rejection(self):
        stage = self.based_stage()
        tip = self.commit_in(stage.path, "other.txt", "no\n")
        self.assertEqual([r.code for r in self.ports.scope_check(stage)], ["out_of_scope"])
        self.assertEqual(self.pinned(stage), tip)

    def test_change_dicts_cover_status_mode_size_and_binary(self):
        stage = self.based_stage()
        (stage.path / "keep.txt").unlink()
        (stage.path / "sub" / "x.txt").write_bytes(b"changed\n")
        (stage.path / "blob.bin").write_bytes(b"\0\1\2\3")
        tip = self.commit_in(stage.path, "new.txt", "new\n")
        self.ports.scope_check(stage)
        changes = {c["path"]: c for c in _changes(self.jarvis_repo(stage), stage.base, tip)}
        self.assertEqual(sorted(changes), ["blob.bin", "keep.txt", "new.txt", "sub/x.txt"])
        self.assertEqual(changes["keep.txt"]["status"], "D")
        self.assertEqual(changes["keep.txt"]["new_mode"], "000000")
        self.assertEqual(changes["keep.txt"]["size"], 0)
        self.assertEqual(changes["sub/x.txt"]["status"], "M")
        self.assertEqual(changes["sub/x.txt"]["size"], 8)
        self.assertEqual(changes["new.txt"]["status"], "A")
        self.assertEqual(changes["new.txt"]["old_mode"], "000000")
        self.assertIs(changes["new.txt"]["binary"], False)
        self.assertIs(changes["blob.bin"]["binary"], True)
        self.assertIsNone(changes["new.txt"]["old_path"])
        self.assertIs(changes["new.txt"]["reparse"], False)

    def test_symlink_mode_is_a_reparse_change(self):
        stage = self.based_stage()
        blob = self.git(stage.path, "hash-object", "-w", "--stdin", input="target").strip()
        tree = self.git(stage.path, "mktree",
                        input=f"120000 blob {blob}\tlink\n").strip()
        tip = self.git(stage.path, "commit-tree", tree, "-p", stage.base,
                       input="link\n").strip()
        self.git(stage.path, "update-ref", "HEAD", tip)
        self.ports = self.build(targets={"link": "file", "keep.txt": "file", "sub/x.txt": "file"})
        self.assertIn("symlink", [r.code for r in self.ports.scope_check(stage)])
        change = [c for c in _changes(self.jarvis_repo(stage), stage.base, tip)
                  if c["path"] == "link"][0]
        self.assertIs(change["reparse"], True)

    def test_each_attempt_earns_its_own_pin_and_a_refusal_leaves_none(self):
        stage = self.based_stage()
        first = self.commit_in(stage.path, "allowed.txt", "one\n")
        self.ports.scope_check(stage)
        self.assertEqual(self.pinned(stage), first)
        second = self.commit_in(stage.path, "allowed.txt", "two\n")
        self.ports.scope_check(stage)
        self.assertEqual(self.pinned(stage), second)
        self.ref_file(stage).write_text("not a commit\n")
        with self.assertRaises(StageError):
            self.ports.scope_check(stage)
        self.assertEqual(self.pinned(stage), "")
        with self.assertRaises(StageError):
            self.ports.tree_hash(stage)

    def test_worker_git_config_in_the_stage_never_runs(self):
        stage = self.based_stage()
        marker = self.home / "marker"
        script = self.home / "evil.cmd"
        script.write_text(f"@echo x>{marker}\r\n")
        self.git(stage.path, "config", "core.fsmonitor", str(script))
        self.git(stage.path, "config", "core.sshCommand", str(script))
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        marker.unlink(missing_ok=True)
        self.assertEqual(self.ports.scope_check(stage), [])
        self.later_results(stage)
        self.assertFalse(marker.exists())


class StageRefTests(Base):
    def assert_refused_without_pin(self, stage):
        with self.assertRaises(StageError):
            self.ports.scope_check(stage)
        self.assertEqual(self.pinned(stage), "")

    def test_packed_ref_is_read(self):
        stage = self.based_stage()
        tip = self.commit_in(stage.path, "allowed.txt", "ok\n")
        self.ref_file(stage).unlink()
        (self.common_dir(stage) / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted\n{tip} refs/heads/{stage.branch}\n")
        self.assertEqual(self.ports.scope_check(stage), [])
        self.assertEqual(self.pinned(stage), tip)

    def test_unparseable_ref_is_refused(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        self.ref_file(stage).write_text("garbage\n")
        self.assert_refused_without_pin(stage)

    def test_symbolic_ref_is_refused(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        self.ref_file(stage).write_text("ref: refs/heads/main\n")
        self.assert_refused_without_pin(stage)

    def test_missing_ref_is_refused(self):
        stage = self.based_stage()
        self.ref_file(stage).unlink()
        self.assert_refused_without_pin(stage)

    def test_claim_of_absent_object_is_refused(self):
        stage = self.based_stage()
        self.ref_file(stage).write_text("1" * 40 + "\n")
        self.assert_refused_without_pin(stage)


class PinTests(Base):
    def judged_stage(self, text="ok\n"):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", text)
        self.assertEqual(self.ports.scope_check(stage), [])
        return stage

    def destroy_stage_objects(self, stage):
        for file in (self.common_dir(stage) / "objects").rglob("*"):
            if file.is_file():
                file.chmod(stat.S_IWRITE)
                file.unlink()

    def test_replacing_the_stage_tip_and_objects_changes_no_later_port(self):
        stage = self.judged_stage()
        pinned = self.pinned(stage)
        before = self.later_results(stage)
        self.commit_in(stage.path, "allowed.txt", "replaced\n")
        self.commit_in(stage.path, "other.txt", "planted\n")
        self.assertEqual(self.later_results(stage), before)
        self.destroy_stage_objects(stage)
        self.assertEqual(self.later_results(stage), before)
        self.assertEqual(self.pinned(stage), pinned)
        self.assertEqual(self.exported[-1][2], "ok\n")
        self.assertEqual(self.diffs[-1], self.diffs[0])
        self.assertIn("+ok", self.diffs[0])

    def test_a_garbage_stage_ref_after_the_pin_changes_nothing(self):
        stage = self.judged_stage()
        before = self.later_results(stage)
        self.ref_file(stage).write_text("garbage\n")
        self.assertEqual(self.later_results(stage), before)

    def test_only_scope_check_touches_the_stage(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        with mock.patch.object(stage_git, "import_commit",
                               wraps=stage_git.import_commit) as imported, \
                mock.patch.object(self.manager, "_claim",
                                  wraps=self.manager._claim) as claimed:
            self.ports.scope_check(stage)
            self.assertEqual((imported.call_count, claimed.call_count), (1, 1))
            with mock.patch.object(stage_git, "run", wraps=stage_git.run) as git_run, \
                    mock.patch.object(self.manager, "_git", wraps=self.manager._git) as stage_side:
                self.later_results(stage)
            self.assertEqual((imported.call_count, claimed.call_count), (1, 1))
            self.assertGreater(git_run.call_count, 0)
            self.assertEqual({Path(c.args[0]) for c in git_run.call_args_list},
                             {self.jarvis_repo(stage)})
            self.assertEqual(stage_side.call_count, 0)

    def test_regression_refuses_a_crash_after_a_clean_summary_and_truncated_output(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        self.ports.scope_check(stage)
        self.ports.run_checks(stage)
        clean = "Ran 3 tests in 0.1s\n\nOK\n"
        self.assertIs(self.ports.regress([_check(clean)]).passed, True)
        self.assertIs(self.ports.regress([_check(clean, exit_code=1)]).passed, False)
        self.assertIs(self.ports.regress([_check(clean, truncated=True)]).passed, False)

    def test_every_later_port_refuses_a_missing_pin(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        verdict = types.SimpleNamespace(passed=True)
        for call in (lambda: self.ports.run_checks(stage),
                     lambda: self.ports.review(stage),
                     lambda: self.ports.evidence(self.step, stage, [_check("x")], verdict),
                     lambda: self.ports.tree_hash(stage)):
            with self.assertRaises(StageError):
                call()
        self.assertEqual((self.exported, self.diffs), ([], []))

    def test_regression_refuses_a_pin_that_vanished_after_the_checks(self):
        stage = self.judged_stage()
        results = self.ports.run_checks(stage)
        stage_git.run(self.jarvis_repo(stage), "update-ref", "--no-deref", "--stdin",
                      input=f"delete {self.pin_ref(stage)}\n".encode())
        with self.assertRaises(StageError):
            self.ports.regress(results)


class ExportTests(Base):
    def judged_stage(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "ok\n")
        self.assertEqual(self.ports.scope_check(stage), [])
        (stage.path / "allowed.txt").write_text("edited after the pin\n")
        return stage

    def test_checks_run_on_a_git_free_export(self):
        stage = self.judged_stage()
        self.ports.run_checks(stage)
        directory, has_git, text = self.exported[0]
        self.assertNotEqual(directory, stage.path)
        self.assertFalse(has_git)
        self.assertEqual(text, "ok\n")
        self.assertFalse(directory.exists())

    def test_fixture_hash_is_over_a_fresh_export(self):
        stage = self.judged_stage()
        self.check_effect = lambda tree: (tree / "allowed.txt").write_text("rewritten by a test\n")
        verdict = self.ports.regress(self.ports.run_checks(stage))
        directory, has_git, text = self.hashed[0]
        self.assertIs(verdict.passed, True)
        self.assertNotEqual(directory, stage.path)
        self.assertNotEqual(directory, self.exported[0][0])
        self.assertFalse(has_git)
        self.assertEqual(text, "ok\n")
        self.assertFalse(directory.exists())

    def test_regression_reads_the_check_output(self):
        stage = self.judged_stage()
        self.check_outputs = ["FAIL: test_x (m.T.test_x)\n\nRan 3 tests in 0.1s\n\n"
                              "FAILED (failures=1)\n"]
        self.assertIs(self.ports.regress(self.ports.run_checks(stage)).passed, False)


class WiringTests(Base):
    def test_driver_runs_scope_before_any_other_judgement(self):
        body = inspect.getsource(work_driver._slice_body)
        self.assertLess(body.index("_scope(stage, ports)"), body.index("_accept("))
        scope = inspect.getsource(work_driver._scope)
        self.assertIn("ports.scope_check", scope)
        accept = inspect.getsource(work_driver._accept)
        self.assertNotIn("scope_check", accept)
        self.assertIn("ports.run_checks", accept)

    def test_apply_needs_an_intent_and_a_fresh_stage_has_no_effect(self):
        stage = self.based_stage()
        self.commit_in(stage.path, "allowed.txt", "allowed\n")
        self.assertEqual(self.ports.scope_check(stage), [])
        with self.assertRaisesRegex(StageError, "no apply intent"):
            self.ports.apply(stage, None)
        self.assertIs(self.ports.applied(stage, "key"), False)
        self.assertIsNone(self.ports.undo(stage, "key"))

    def test_ports_never_reach_git_except_through_stage_git(self):
        source = inspect.getsource(work_ports)
        for banned in ("manager._git", "subprocess", "zlib", "manager.apply", "manager.undo",
                       "_integration"):
            self.assertNotIn(banned, source)

    def test_a_restart_reattaches_to_the_recorded_stage(self):
        stage = self.based_stage()
        again = self.build().stage_create(self.step)
        self.assertEqual(again, stage)
        store = SQLiteMemoryStore(self.db)
        gates = [e for e in store.work_events(RUN) if '"gate":"stage"' in e.payload_json
                 or '"gate": "stage"' in e.payload_json]
        self.assertEqual(len(gates), 1)

    def test_lease_is_held_and_released_under_the_run(self):
        self.assertTrue(self.ports.lease_claim(self.step))
        self.ports.lease_release(self.step)
        self.assertTrue(self.ports.lease_claim(self.step))

    def test_apply_intent_reports_key_and_receipt(self):
        self.assertIsNone(self.ports.apply_intent(self.step))
        self.ports.append_event(EventKind.PROGRESS, self.step.key,
                                {"gate": "apply_intent", "idempotency_key": "k"})
        self.assertEqual(self.ports.apply_intent(self.step), ("k", False))
        self.ports.append_event(EventKind.PROGRESS, self.step.key,
                                {"gate": "apply", "passed": True, "idempotency_key": "k"})
        self.assertEqual(self.ports.apply_intent(self.step), ("k", True))


REF = f"refs/heads/jarvis/{RUN}/main"
OTHER = "refs/heads/other"


def _put(path, text):
    Path(path).write_bytes(text.encode())  # no CRLF translation: git reads these bytes


class ApplyBase(Base):
    def setUp(self):
        super().setUp()
        self.stage = self.based_stage()
        self.commit_in(self.stage.path, "allowed.txt", "allowed\n")
        self.assertEqual(self.ports.scope_check(self.stage), [])
        self.B, self.C = self.stage.base, self.pinned(self.stage)
        self.X = self.commit_in(self.repo, "z.txt", "z\n")
        self.intent()

    def intent(self, key="k"):
        self.ports.append_event(EventKind.PROGRESS, self.step.key,
                                {"gate": "apply_intent", "idempotency_key": key}, idempotency_key=key)

    def ref(self, name=REF):
        return self.git(self.repo, "for-each-ref", "--format=%(objectname)", name).strip() or None

    def symref(self):
        return self.git(self.repo, "for-each-ref", "--format=%(symref)", REF).strip()

    def record(self, which, key="k"):
        return f"refs/jarvis/applied/{RUN}/{hashlib.sha256(key.encode()).hexdigest()}/{which}"

    def state(self, key="k"):
        return self.ref(), self.ref(self.record("old", key)), self.ref(self.record("new", key))

    def set_state(self, ref, old, new):
        self.git(self.repo, "update-ref", REF, ref)
        for which, oid in (("old", old), ("new", new)):
            if oid:
                self.git(self.repo, "update-ref", self.record(which), oid)
            elif self.ref(self.record(which)):
                self.git(self.repo, "update-ref", "-d", self.record(which))

    def import_c(self):
        stage_git.import_commit(self.jarvis_repo(self.stage), self.repo, self.C, self.B)

    def linked(self):
        self.git(self.repo, "worktree", "add", "--detach", str(self.home / "wt"))
        return Path(self.repo) / ".git" / "worktrees" / "wt"

    def racing(self, *commands):
        """Run git commands after every pre-check, immediately before the transaction."""
        real = work_ports._transact

        def race(repo, *lines):
            for command in commands:
                self.git(self.repo, *command)
            real(repo, *lines)
        return mock.patch.object(work_ports, "_transact", race)


class ApplyTests(ApplyBase):
    def test_good_apply_moves_the_ref_and_writes_both_records(self):
        self.assertEqual(self.state(), (self.B, None, None))
        self.assertEqual(self.ports.apply(self.stage, None), self.C)
        self.assertEqual(self.state(), (self.C, self.B, self.C))
        self.assertIs(self.ports.applied(self.stage, "k"), True)

    def test_ref_not_at_base_is_refused_with_no_record(self):
        self.git(self.repo, "update-ref", REF, self.X)
        with self.assertRaisesRegex(StageError, "re-run"):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.X, None, None))

    def test_stale_old_refuses_all_with_no_record(self):
        with self.racing(("update-ref", REF, self.X)), self.assertRaises(StageError):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.X, None, None))

    def test_stale_new_refuses_undo_and_keeps_the_records(self):
        self.ports.apply(self.stage, None)
        with self.racing(("update-ref", REF, self.X)), self.assertRaises(StageError):
            self.ports.undo(self.stage, "k")
        self.assertEqual(self.state(), (self.X, self.B, self.C))

    def test_no_deref_replaces_a_late_symref_and_leaves_its_referent(self):
        with self.racing(("update-ref", OTHER, self.B), ("symbolic-ref", REF, OTHER)):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.ref(OTHER), self.B)
        self.assertEqual(self.symref(), "")
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_replay_is_refused(self):
        self.ports.apply(self.stage, None)
        with self.assertRaises(StageError):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.C, self.B, self.C))
        self.git(self.repo, "update-ref", REF, self.B)
        with self.assertRaises(StageError):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.B, self.B, self.C))

    def test_undo_restores_recorded_old_and_removes_records(self):
        self.ports.apply(self.stage, None)
        self.ports.undo(self.stage, "k")
        self.assertEqual(self.state(), (self.B, None, None))
        self.assertIs(self.ports.applied(self.stage, "k"), False)

    def test_undo_with_no_record_is_a_noop_only_at_base(self):
        self.assertIsNone(self.ports.undo(self.stage, "k"))
        self.assertEqual(self.state(), (self.B, None, None))
        self.import_c()
        for moved in (self.X, self.C):
            self.git(self.repo, "update-ref", REF, moved)
            with self.assertRaisesRegex(StageError, "not at the stage base"):
                self.ports.undo(self.stage, "k")
            self.assertEqual(self.state(), (moved, None, None))

    def test_apply_refused_when_main_worktree_has_the_ref_checked_out(self):
        self.git(self.repo, "symbolic-ref", "HEAD", REF)
        with (mock.patch.object(stage_git, "import_commit") as imported,
              self.assertRaisesRegex(StageError, "checked out")):
            self.ports.apply(self.stage, None)
        imported.assert_not_called()  # refused before any object reaches the target
        self.assertEqual(self.state(), (self.B, None, None))

    def test_undo_refused_when_main_worktree_has_the_ref_checked_out(self):
        self.ports.apply(self.stage, None)
        self.git(self.repo, "symbolic-ref", "HEAD", REF)
        with self.assertRaisesRegex(StageError, "checked out"):
            self.ports.undo(self.stage, "k")
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_linked_worktree_checkout_refuses_apply_and_undo(self):
        wt = self.linked()
        _put(wt / "HEAD", f"ref: {REF}\n")
        with self.assertRaisesRegex(StageError, "checked out"):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.B, None, None))
        _put(wt / "HEAD", self.B + "\n")
        self.ports.apply(self.stage, None)
        _put(wt / "HEAD", f"ref: {REF}\n")
        with self.assertRaisesRegex(StageError, "checked out"):
            self.ports.undo(self.stage, "k")
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    # Each unreadable worktree HEAD must refuse apply and undo with nothing moved.
    def linked_refused(self, corrupt, pattern):
        wt = self.linked()
        head, gitdir = (wt / "HEAD").read_bytes(), (wt / "gitdir").read_bytes()
        corrupt(wt)
        with self.assertRaisesRegex(StageError, pattern):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.B, None, None))
        if (wt / "HEAD").is_dir():
            (wt / "HEAD").rmdir()
        (wt / "HEAD").write_bytes(head)
        (wt / "gitdir").write_bytes(gitdir)
        self.ports.apply(self.stage, None)
        corrupt(wt)
        with self.assertRaisesRegex(StageError, pattern):
            self.ports.undo(self.stage, "k")
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_garbage_worktree_head_fails_closed(self):
        self.linked_refused(lambda wt: _put(wt / "HEAD", "not a ref\n"), "unreadable worktree HEAD")

    def test_directory_worktree_head_fails_closed(self):
        def corrupt(wt):
            (wt / "HEAD").unlink()
            (wt / "HEAD").mkdir()
        self.linked_refused(corrupt, "unreadable worktree HEAD")

    def test_missing_worktree_head_fails_closed(self):
        self.linked_refused(lambda wt: (wt / "HEAD").unlink(), "unreadable worktree HEAD")

    def test_over_deep_symref_chain_fails_closed(self):
        heads = Path(self.repo) / ".git" / "refs" / "heads"

        def corrupt(wt):
            for link in range(8):
                _put(heads / f"c{link}", f"ref: refs/heads/c{link + 1}\n")
            _put(heads / "c8", self.B + "\n")
            _put(wt / "HEAD", "ref: refs/heads/c0\n")
        self.linked_refused(corrupt, "unreadable worktree HEAD")

    def test_unborn_zero_worktree_head_fails_closed(self):
        self.linked_refused(lambda wt: _put(wt / "HEAD", "ref: refs/heads/nowhere\n"), "unreadable worktree HEAD")

    def test_unlisted_worktree_record_fails_closed(self):
        self.linked_refused(lambda wt: (wt / "gitdir").unlink(), "do not match")

    def test_case_variant_checked_out_name_is_refused(self):
        variant = REF.replace("jarvis", "JARVIS").replace(RUN, RUN.upper()).replace("main", "MAIN")
        loose = Path(self.repo) / ".git" / variant

        def corrupt(wt):
            if not loose.exists():  # a case-sensitive filesystem needs the variant made
                loose.parent.mkdir(parents=True, exist_ok=True)
                _put(loose, self.ref() + "\n")
            _put(wt / "HEAD", f"ref: {variant}\n")
        self.linked_refused(corrupt, "checked out")

    def test_symbolic_integration_ref_fails_closed_in_apply(self):
        self.git(self.repo, "update-ref", OTHER, self.B)
        self.git(self.repo, "symbolic-ref", REF, OTHER)
        with self.assertRaisesRegex(StageError, "symbolic"):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.symref(), OTHER)
        self.assertEqual(self.state(), (self.B, None, None))

    def test_symbolic_integration_ref_fails_closed_in_undo_and_applied(self):
        self.ports.apply(self.stage, None)
        self.git(self.repo, "update-ref", OTHER, self.C)
        self.git(self.repo, "symbolic-ref", REF, OTHER)
        for call in (self.ports.undo, self.ports.applied):
            with self.assertRaisesRegex(StageError, "symbolic"):
                call(self.stage, "k")
        self.assertEqual(self.symref(), OTHER)
        self.assertEqual(self.ref(OTHER), self.C)
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_dangling_symbolic_ref_fails_closed(self):
        self.git(self.repo, "symbolic-ref", REF, "refs/heads/nowhere")
        for call in (lambda: self.ports.apply(self.stage, None),
                     lambda: self.ports.undo(self.stage, "k"),
                     lambda: self.ports.applied(self.stage, "k")):
            with self.assertRaises(StageError):
                call()

    def test_each_partial_or_mismatched_combination_raises(self):
        self.import_c()
        B, C, X = self.B, self.C, self.X
        for combo in ((B, B, None), (B, None, C), (C, B, None), (C, None, C), (B, B, C),
                      (C, None, None), (X, B, C), (C, X, C)):
            with self.subTest(combo=[oid and oid[:6] for oid in combo]):
                self.set_state(*combo)
                for call in (self.ports.applied, self.ports.undo):
                    with self.assertRaises(StageError):
                        call(self.stage, "k")
                self.assertEqual(self.state(), combo)

    def test_c_reachable_with_no_record_is_not_applied(self):
        self.import_c()
        self.git(self.repo, "update-ref", OTHER, self.C)
        self.assertIs(self.ports.applied(self.stage, "k"), False)

    def test_commit_without_its_objects_in_the_target_is_refused(self):
        # The object transfer "succeeds" but delivers only the commit: its tree is missing.
        def commit_only(*args, **kwargs):
            raw = self.manager._git(self.jarvis_repo(self.stage), "cat-file", "commit", self.C)
            self.manager._git(self.repo, "hash-object", "-t", "commit", "-w", "--stdin", input=raw)

        with mock.patch.object(stage_git, "_pipe", commit_only), self.assertRaises(StageError):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.B, None, None))


class ResumeTests(ApplyBase):
    def drive(self):
        ports = self.build()
        ports.route, ports.worker = (lambda step: "w"), (lambda name: None)
        ports.budget_exhausted = lambda: True
        ports.apply = mock.Mock(side_effect=AssertionError("a resume never applies"))
        step = WorkStep(RUN, self.step.key, 1, "one", StepState.ACCEPTING, self.clock())
        _, steps = work_driver.drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (step,), ports)
        return steps[0]

    def gates(self, name):
        payloads = [json.loads(e.payload_json) for e in SQLiteMemoryStore(self.db).work_events(RUN)]
        return [p for p in payloads if p.get("gate") == name]

    def assert_resume_raises(self):
        with self.assertRaises(StageError):
            self.drive()
        self.assertEqual(self.gates("apply"), [])

    def test_crash_before_commit_is_a_recorded_failed_attempt(self):
        step = self.drive()
        self.assertIsNot(step.state, StepState.ACCEPTED)
        self.assertEqual([g["findings"] for g in self.gates("apply")], [["resume:not_applied"]])
        self.assertEqual(self.state(), (self.B, None, None))

    def test_crash_after_commit_before_receipt_verifies_without_a_second_apply(self):
        self.ports.apply(self.stage, None)
        step = self.drive()
        self.assertIs(step.state, StepState.ACCEPTED)
        self.assertEqual([g["ref"] for g in self.gates("apply")], ["verified"])
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_crash_with_a_partial_record_raises(self):
        self.ports.apply(self.stage, None)
        self.set_state(self.C, self.B, None)
        self.assert_resume_raises()
        self.assertEqual(self.state(), (self.C, self.B, None))

    def test_torn_undo_ref_moved_records_left_raises(self):
        self.ports.apply(self.stage, None)
        self.set_state(self.B, self.B, self.C)
        self.assert_resume_raises()
        self.assertEqual(self.state(), (self.B, self.B, self.C))

    def test_torn_undo_records_removed_ref_not_moved_raises(self):
        self.ports.apply(self.stage, None)
        self.set_state(self.C, None, None)
        self.assert_resume_raises()
        self.assertEqual(self.state(), (self.C, None, None))

    def test_accept_path_applies_and_receipts(self):
        findings, ok = work_driver._accept(self.step, self.stage, self.ports)
        self.assertEqual((findings, ok), ((), True))
        key, receipted = self.ports.apply_intent(self.step)
        self.assertIs(receipted, True)
        self.assertEqual(self.state(key), (self.C, self.B, self.C))
        self.assertEqual([g["ref"] for g in self.gates("apply")], [self.C])

    def test_apply_refused_for_a_moved_ref_stops_for_the_operator(self):
        self.git(self.repo, "update-ref", REF, self.X)
        with self.assertRaises(StageError):
            work_driver._accept(self.step, self.stage, self.ports)
        key, receipted = self.ports.apply_intent(self.step)
        self.assertIs(receipted, False)
        self.assertEqual(self.state(key), (self.X, None, None))
        with self.assertRaises(StageError):
            self.ports.applied(self.stage, key)

    def test_apply_refused_at_base_is_a_failed_attempt(self):
        _put(self.linked() / "HEAD", "not a ref\n")
        findings, ok = work_driver._accept(self.step, self.stage, self.ports)
        self.assertIs(ok, False)
        self.assertEqual(findings[0]["detail"], "gate_error:StageError")
        key, _ = self.ports.apply_intent(self.step)
        self.assertEqual(self.state(key), (self.B, None, None))


BROKEN = {"garbage": "garbage\n", "empty": "", "short": "abcdef0123\n", "zero": "0" * 40 + "\n",
          "dangling": "ref: refs/heads/nowhere\n"}


class ApplyFixTests(ApplyBase):
    def test_commit_equal_to_base_applies_nothing(self):
        self.git(self.jarvis_repo(self.stage), "update-ref", self.pin_ref(self.stage), self.B)
        with (mock.patch.object(work_ports, "_transact") as transact,
              mock.patch.object(stage_git, "import_commit") as imported,
              self.assertRaisesRegex(StageError, "nothing to apply")):
            self.ports.apply(self.stage, None)
        transact.assert_not_called()
        imported.assert_not_called()
        self.assertEqual(self.state(), (self.B, None, None))

    def test_checkout_during_import_refuses_before_the_transaction(self):
        real = stage_git.import_commit

        def checkout_then_import(*args, **kwargs):
            self.git(self.repo, "symbolic-ref", "HEAD", REF)
            return real(*args, **kwargs)
        with (mock.patch.object(stage_git, "import_commit", checkout_then_import),
              self.assertRaisesRegex(StageError, "checked out")):
            self.ports.apply(self.stage, None)
        self.assertEqual(self.state(), (self.B, None, None))

    def test_absent_ref_and_absent_new_is_not_applied(self):
        self.git(self.repo, "update-ref", self.record("old"), self.B)
        self.git(self.repo, "update-ref", "-d", REF)
        with self.assertRaisesRegex(StageError, "does not match"):
            self.ports.applied(self.stage, "k")
        with self.assertRaisesRegex(StageError, "partial"):
            self.ports.undo(self.stage, "k")
        self.assertEqual(self.state(), (None, self.B, None))

    def test_absent_ref_with_no_records_is_not_at_base(self):
        self.git(self.repo, "update-ref", "-d", REF)
        with self.assertRaisesRegex(StageError, "does not match"):
            self.ports.applied(self.stage, "k")
        with self.assertRaisesRegex(StageError, "not at the stage base"):
            self.ports.undo(self.stage, "k")

    def test_absent_ref_with_both_records_is_not_applied(self):
        self.import_c()
        self.git(self.repo, "update-ref", self.record("old"), self.B)
        self.git(self.repo, "update-ref", self.record("new"), self.C)
        self.git(self.repo, "update-ref", "-d", REF)
        for call in (self.ports.applied, self.ports.undo):
            with self.assertRaisesRegex(StageError, "does not match"):
                call(self.stage, "k")
        self.assertEqual(self.state(), (None, self.B, self.C))


class BrokenRefTests(ApplyBase):
    def calls(self):
        return (lambda: self.ports.applied(self.stage, "k"),
                lambda: self.ports.undo(self.stage, "k"))

    def assert_refused(self, ref, *calls):
        path = Path(self.repo) / ".git" / ref
        original = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        for shape, text in BROKEN.items():
            with self.subTest(ref=ref, shape=shape):
                _put(path, text)
                before = path.read_bytes()
                for call in calls:
                    with self.assertRaisesRegex(StageError, "broken ref refused"):
                        call()
                    self.assertEqual(path.read_bytes(), before)
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)

    def test_broken_integration_ref_before_apply(self):
        self.assert_refused(REF, lambda: self.ports.apply(self.stage, None), *self.calls())
        self.assertEqual(self.state(), (self.B, None, None))

    def test_broken_integration_ref_after_apply(self):
        self.ports.apply(self.stage, None)
        self.assert_refused(REF, *self.calls())
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_broken_lone_record_is_not_absent(self):
        for which in ("old", "new"):
            self.assert_refused(self.record(which), *self.calls())
        self.assertEqual(self.state(), (self.B, None, None))

    def test_broken_record_after_apply(self):
        self.ports.apply(self.stage, None)
        for which in ("old", "new"):
            self.assert_refused(self.record(which), *self.calls())
        self.assertEqual(self.state(), (self.C, self.B, self.C))

    def test_broken_record_stops_apply_moving_the_ref(self):
        path = Path(self.repo) / ".git" / self.record("old")
        path.parent.mkdir(parents=True, exist_ok=True)
        for shape in ("garbage", "empty", "short", "dangling"):
            with self.subTest(shape=shape):
                _put(path, BROKEN[shape])
                with self.assertRaises(StageError):
                    self.ports.apply(self.stage, None)
                self.assertEqual(self.ref(), self.B)

    def test_packed_garbage_line_refuses(self):
        packed = Path(self.repo) / ".git" / "packed-refs"
        self.assertFalse(packed.exists())
        _put(packed, f"# pack-refs with: peeled fully-peeled sorted \nzzzz {self.record('old')}\n")
        for call in self.calls():
            with self.assertRaisesRegex(StageError, "broken ref refused"):
                call()
        packed.unlink()

    def test_absent_record_with_a_stale_lock_is_not_proven_absent(self):
        lock = Path(self.repo) / ".git" / (self.record("old") + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        _put(lock, "")
        with self.assertRaisesRegex(StageError, "broken ref refused"):
            self.ports.applied(self.stage, "k")
        lock.unlink()
        self.assertIs(self.ports.applied(self.stage, "k"), False)

    def test_record_that_is_a_directory_with_a_child_refuses(self):
        self.git(self.repo, "update-ref", self.record("old") + "/child", self.B)
        with self.assertRaisesRegex(StageError, "unexpected ref listing"):
            self.ports.applied(self.stage, "k")

    def test_empty_directory_at_a_record_is_absent(self):
        (Path(self.repo) / ".git" / self.record("old")).mkdir(parents=True)
        self.assertIs(self.ports.applied(self.stage, "k"), False)


class SliceChainTests(Base):
    def chain(self, count, after_first=None):
        head = self.commit_in(self.repo, "keep.txt", "keep\n")
        self.check_outputs *= count
        ports = self.build(targets={f"s{n}.txt": "file" for n in range(1, count + 1)})
        tips, stages = [], []

        def start(task):
            step, stage = task
            stages.append(stage)
            tips.append(self.commit_in(stage.path, f"s{step.ordinal}.txt", "s\n"))
            if step.ordinal == 1 and after_first:
                after_first()
            return "id"
        fake = types.SimpleNamespace(
            start=start, stop=lambda run_id: None,
            collect=lambda run_id: WorkerResult("completed", "s", (), ()))
        ports.route = lambda step: "w"
        ports.worker = lambda name: fake
        ports.task = lambda step, stage, findings: (step, stage)
        ports.budget_exhausted = lambda: False
        ports.needs_input = lambda result: False
        ports.verify_cwd = lambda stage: None
        ports.link_rescan = lambda stage: ()
        steps = [WorkStep(RUN, step_key(RUN, n, f"s{n}"), n, f"s{n}", StepState.PENDING, self.clock())
                 for n in range(1, count + 1)]
        _, done = work_driver.drive(WorkRun(RUN, RunState.BUILDING, T0, T0), steps, ports)
        self.assertEqual([step.state for step in done], [StepState.ACCEPTED] * count)
        self.assertEqual(len(stages), count)
        return head, ports, done, tips, stages

    def tip(self):
        return work_ports._ref(self.repo, REF)

    def test_second_slice_applies_on_the_integration_tip_and_undoes(self):
        head, ports, done, tips, stages = self.chain(2)
        self.assertEqual(stages[0].base, head)
        self.assertEqual(stages[1].base, tips[0])
        self.assertEqual(self.tip(), tips[1])
        key, receipted = ports.apply_intent(done[1])
        self.assertIs(receipted, True)
        self.assertIs(ports.applied(stages[1], key), True)
        ports.undo(stages[1], key)
        self.assertEqual(self.tip(), tips[0])
        self.assertIs(ports.applied(stages[1], key), False)
        first, _ = ports.apply_intent(done[0])
        self.assertIs(ports.applied(stages[0], first), True)

    def test_third_slice_builds_on_the_second(self):
        _, _, _, tips, stages = self.chain(3)
        self.assertEqual(stages[2].base, tips[1])
        self.assertEqual(self.tip(), tips[2])

    def test_head_moving_between_slices_does_not_change_the_base(self):
        _, _, _, tips, stages = self.chain(
            2, after_first=lambda: self.commit_in(self.repo, "z.txt", "z\n"))
        self.assertEqual(stages[1].base, tips[0])
        self.assertEqual(self.tip(), tips[1])

    def test_reattach_keeps_the_recorded_base(self):
        _, ports, done, tips, stages = self.chain(2)
        again = ports.stage_create(done[1])
        self.assertEqual((again.base, again.branch), (tips[0], stages[1].branch))

    def test_broken_or_symbolic_integration_ref_stops_stage_create(self):
        head = self.commit_in(self.repo, "keep.txt", "keep\n")
        path = Path(self.repo) / ".git" / REF
        path.parent.mkdir(parents=True, exist_ok=True)
        for shape, text in BROKEN.items():
            with self.subTest(shape=shape):
                _put(path, text)
                before = path.read_bytes()
                with self.assertRaisesRegex(StageError, "broken ref refused"):
                    self.ports.stage_create(self.step)
                self.assertEqual(path.read_bytes(), before)
        path.unlink()
        self.git(self.repo, "update-ref", OTHER, head)
        self.git(self.repo, "symbolic-ref", REF, OTHER)
        with self.assertRaisesRegex(StageError, "symbolic"):
            self.ports.stage_create(self.step)


class RunIdTests(Base):
    def test_bad_run_id_fails_before_any_target_write(self):
        head = self.git(self.repo, "rev-parse", "HEAD").strip()
        victim = "refs/heads/victim"
        injected = f"run1/main\nupdate {victim} {head}\nprepare\ncommit\n"
        before = self.git(self.repo, "for-each-ref")
        for run_id in (injected, "run1\n", "", 7, None, "run/1", "-run", "r" * 65):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(StageError, "invalid run"):
                    self.build(run_id=run_id)
                self.assertEqual(self.git(self.repo, "for-each-ref", victim), "")
                self.assertEqual(self.git(self.repo, "for-each-ref"), before)

    def test_longest_run_id_builds(self):
        self.build(run_id="r" * 64)


class RefExactTests(Base):
    VARIANT = REF.replace("jarvis", "JARVIS")

    def head(self):
        return self.git(self.repo, "rev-parse", "HEAD").strip()

    def put(self, ref, text):
        path = Path(self.repo) / ".git" / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        _put(path, text)

    def test_valid_ref_under_a_case_variant_directory_is_its_value(self):
        self.put(self.VARIANT, self.head() + "\n")
        self.assertEqual(work_ports._ref(self.repo, REF), self.head())

    def test_broken_shapes_under_a_case_variant_directory_refuse(self):
        for shape, text in BROKEN.items():
            with self.subTest(shape=shape):
                self.put(self.VARIANT, text)
                with self.assertRaisesRegex(StageError, "broken ref refused"):
                    work_ports._ref(self.repo, REF)

    def test_symbolic_ref_under_a_case_variant_directory_refuses(self):
        self.git(self.repo, "update-ref", OTHER, self.head())
        self.put(self.VARIANT, f"ref: {OTHER}\n")
        with self.assertRaisesRegex(StageError, "ref refused"):
            work_ports._ref(self.repo, REF)

    def test_case_variant_only_in_packed_refs_is_absent(self):
        _put(Path(self.repo) / ".git" / "packed-refs",
             f"# pack-refs with: peeled fully-peeled sorted \n{self.head()} {self.VARIANT}\n")
        self.assertIsNone(work_ports._ref(self.repo, REF))

    def test_tag_named_like_an_absent_ref_is_absent(self):
        self.git(self.repo, "update-ref", f"refs/tags/{REF}", self.head())
        self.assertIsNone(work_ports._ref(self.repo, REF))
        self.assertEqual(self.ports.stage_create(self.step).base, self.head())

    def test_tag_named_like_a_broken_ref_does_not_hide_it(self):
        self.git(self.repo, "update-ref", f"refs/tags/{REF}", self.head())
        for shape, text in BROKEN.items():
            with self.subTest(shape=shape):
                self.put(REF, text)
                with self.assertRaisesRegex(StageError, "broken ref refused"):
                    work_ports._ref(self.repo, REF)


class RebaseTests(ApplyBase):
    def assert_refused(self, gitdir, state, name, call):
        pause = gitdir / state
        pause.mkdir()
        _put(pause / "head-name", name + "\n")
        before = self.state()
        with self.assertRaisesRegex(StageError, "checked out"):
            call()
        self.assertEqual(self.state(), before)
        shutil.rmtree(pause)

    def test_paused_rebase_of_the_ref_refuses_apply_and_undo(self):
        main, linked = Path(self.repo) / ".git", self.linked()
        for call, setup in ((lambda: self.ports.apply(self.stage, None), None),
                            (lambda: self.ports.undo(self.stage, "k"),
                             lambda: self.ports.apply(self.stage, None))):
            if setup:
                setup()
            for gitdir in (main, linked):
                for state in ("rebase-merge", "rebase-apply"):
                    with self.subTest(gitdir=gitdir.name, state=state):
                        self.assert_refused(gitdir, state, REF, call)

    def test_case_variant_name_refuses(self):
        self.assert_refused(Path(self.repo) / ".git", "rebase-merge", REF.upper(),
                            lambda: self.ports.apply(self.stage, None))

    def test_paused_rebase_of_another_ref_does_not_refuse(self):
        pause = Path(self.repo) / ".git" / "rebase-merge"
        pause.mkdir()
        _put(pause / "head-name", OTHER + "\n")
        self.assertEqual(self.ports.apply(self.stage, None), self.C)
        self.ports.undo(self.stage, "k")
        self.assertEqual(self.ref(), self.B)

    def test_real_paused_rebase_in_a_linked_worktree_refuses(self):
        wt = self.home / "wt"
        # The short name: a full refname would make git detach instead of checking the branch out.
        self.git(self.repo, "worktree", "add", str(wt), REF.removeprefix("refs/heads/"))
        self.commit_in(wt, "clash.txt", "ours\n")
        onto = self.commit_in(self.repo, "clash.txt", "theirs\n")
        rebase = subprocess.run(
            ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "rebase", onto],
            capture_output=True)
        self.assertNotEqual(rebase.returncode, 0)
        self.assertNotIn(REF, self.git(self.repo, "worktree", "list", "--porcelain"))
        with self.assertRaisesRegex(StageError, "checked out"):
            work_ports._refuse_checked_out(self.repo, REF)
