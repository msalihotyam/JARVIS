"""N6-01: the N6 root wires PM26A through build_ports; N6 itself only has read-only sight."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jarvis import memory, n6_root, stage_git, work_driver
from jarvis.memory import SQLiteMemoryStore
from jarvis.work_ledger import RunState, StepState, WorkRun, WorkStep, step_key
from jarvis.work_regress import TestResult
from jarvis.write_stage import StageManager
from jarvis.workers.base import WorkerResult
from tests.test_work_ports import ACCEPTED, RUN, T0, _check, _commit

ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = ROOT / "data" / "jarvis.db"
WRITES = {"append_event", "notify", "lease_claim", "lease_release", "stage_create", "worker",
          "task", "scope_check", "run_checks", "evidence", "apply", "undo", "link_rescan", "applied"}


class N6RootTests(unittest.TestCase):
    def setUp(self):
        connect = mock.patch("sqlite3.connect", wraps=sqlite3.connect)
        self.connect = connect.start()
        self.addCleanup(connect.stop)
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEMP"), ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.ticks = 0
        self.manager = StageManager(self.home / "stages")
        self.repo = self.manager.create_repo(self.home / "project")
        self.head = _commit(self.manager, self.repo, "keep.txt", "keep\n")
        self.store = SQLiteMemoryStore(self.home / "work.db")
        self.notes = self.home / "n6" / "notes.jsonl"
        self.step = WorkStep(RUN, step_key(RUN, 1, "one"), 1, "one", StepState.PENDING, self.clock())

    def clock(self):
        self.ticks += 1
        return T0 + timedelta(seconds=self.ticks)

    def wiring(self):
        def start(task):
            step, stage = task
            _commit(self.manager, stage.path, "allowed.txt", "done\n")
            return "id"
        fake = types.SimpleNamespace(start=start, stop=lambda run_id: None,
                                     collect=lambda run_id: WorkerResult("completed", "s", (), ()))
        return dict(
            store=self.store, manager=self.manager, target=self.repo, lease_target="project/",
            run_id=RUN, boot_id="boot-1", clock=self.clock, lease=timedelta(minutes=5),
            targets={"allowed.txt": "file"}, jarvis_repo=False,
            checks=[("python", "-m", "unittest")], base_result=TestResult(3, frozenset()),
            locked_fixture_hash="a" * 64, fixture_hash=lambda directory: "a" * 64,
            review=lambda stage, diff: ACCEPTED, plan_hash="p" * 64, worker=lambda name: fake,
            route=lambda step: "fake", task=lambda step, stage, findings: (step, stage),
            budget_exhausted=lambda: False, needs_input=lambda result: False,
            verify_cwd=lambda stage: None, link_rescan=lambda stage: (),
            run_check=lambda argv, *, stage, jarvis_repo: _check("Ran 3 tests in 0.1s\n\nOK\n"))

    def n6(self):
        return n6_root.start(notes=self.notes, **self.wiring())

    def assert_never_opened_live_db(self):
        paths = [Path(call.args[0]).resolve() for call in self.connect.call_args_list
                 if call.args and call.args[0] != ":memory:"]
        self.assertTrue(paths)
        self.assertNotIn(LIVE_DB.resolve(), paths)
        self.assertTrue(all(path.is_relative_to(self.home.resolve()) for path in paths), paths)

    def test_fake_worker_runs_one_slice_end_to_end_through_the_composed_ports(self):
        ports = n6_root.compose(**self.wiring())
        run, steps = work_driver.drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (self.step,), ports)
        self.assertEqual(run.state, RunState.ACCEPTING)
        self.assertEqual([step.state for step in steps], [StepState.ACCEPTED])
        logged = self.store.work_events(RUN)
        self.assertTrue(logged)
        self.assertEqual(len(self.n6().events(RUN)), len(logged))
        self.assert_never_opened_live_db()

    def test_every_port_outside_the_sight_allowlist_raises_and_never_runs(self):
        called = []
        full = types.SimpleNamespace(**{
            name: (lambda *args, _name=name, **kwargs: called.append(_name))
            for name in vars(n6_root.compose(**self.wiring()))})
        seen = n6_root.sight(full)
        refused = set(vars(full)) - n6_root.SIGHT_PORTS
        self.assertLessEqual(WRITES, refused)
        self.assertEqual(set(vars(seen)), set(vars(full)))
        for name in sorted(refused):
            with self.subTest(port=name), self.assertRaisesRegex(PermissionError, f"read-only: {name}$"):
                getattr(seen, name)(self.step)
        self.assertEqual(called, [])

    def test_sight_mode_cannot_drive_a_slice_or_write_an_event(self):
        n6 = self.n6()
        with self.assertRaisesRegex(PermissionError, "N6 sight mode is read-only"):
            work_driver.drive(WorkRun(RUN, RunState.BUILDING, T0, T0), (self.step,), n6.ports)
        self.assertEqual(n6.events(RUN), ())
        self.assertFalse((self.home / "stages").exists() and any((self.home / "stages").iterdir()))
        self.assert_never_opened_live_db()

    def test_there_is_no_write_mode(self):
        for mode in ("write", "act", "full", "Sight"):
            with self.subTest(mode=mode), self.assertRaisesRegex(PermissionError, "no write path"):
                n6_root.start(notes=self.notes, mode=mode, **self.wiring())

    def test_sight_reads_files_and_git_and_refuses_git_writes(self):
        n6 = self.n6()
        report = self.home / "reports" / "N6-01.md"
        report.parent.mkdir()
        report.write_text("STATUS: PASS\n", encoding="utf-8")
        self.assertEqual(n6.read(report), "STATUS: PASS\n")
        self.assertEqual(n6.git(self.repo, "rev-parse", "HEAD").decode().strip(), self.head)
        for args in (("update-ref", "HEAD", self.head), ("worktree", "list", "--porcelain", "-z"),
                     ("index-pack",), ("commit-tree", "x"), ()):
            with self.subTest(args=args), self.assertRaises(PermissionError):
                n6.git(self.repo, *args)
        self.assertEqual(n6.git(self.repo, "rev-parse", "HEAD").decode().strip(), self.head)

    def test_sight_refuses_protected_paths(self):
        n6 = self.n6()
        for path in (LIVE_DB, ROOT / ".env", self.home / ".env.local"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                n6.read(path)
        with self.assertRaises(PermissionError):
            n6_root.start(notes=ROOT / "data" / "n6-notes.jsonl", **self.wiring())

    def test_notes_sink_appends_free_text(self):
        n6 = self.n6()
        n6.note("first thought")
        n6.note("line one\nline two")
        lines = [json.loads(line) for line in self.notes.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["text"] for line in lines], ["first thought", "line one\nline two"])
        self.assertLess(lines[0]["at"], lines[1]["at"])
        with self.assertRaises(ValueError):
            n6.note("   ")

    def snapshot(self):
        names = sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*")
                       if not path.name.startswith(("work.db", "notes.jsonl")))
        return names, stage_git.run(self.repo, "for-each-ref")

    def test_f1_no_sight_call_leaves_a_ref_a_lock_or_a_dir_under_git(self):
        stage = n6_root.compose(**self.wiring()).stage_create(self.step)  # the write path, first
        n6 = self.n6()
        before = self.snapshot()
        calls = (lambda: n6.ports.clock(), lambda: n6.ports.apply_intent(self.step),
                 lambda: n6.ports.tree_hash(stage), lambda: n6.git(self.repo, "rev-parse", "HEAD"),
                 lambda: n6.git(self.repo, "for-each-ref"), lambda: n6.git(stage.path, "rev-list", "HEAD"),
                 lambda: n6.git(self.repo, "ls-tree", "-r", "HEAD"))
        for index, call in enumerate(calls):
            with self.subTest(call=index):
                try:
                    call()
                except Exception:  # tree_hash has no judged pin here; a refusal is not a write
                    pass
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(list(self.home.rglob("*.lock")), [])
        self.assertNotIn("applied", n6_root.SIGHT_PORTS)
        with self.assertRaisesRegex(PermissionError, "read-only: applied$"):
            n6.ports.applied(stage, "key")
        self.assertEqual(self.snapshot(), before)

    def test_f2_git_refuses_a_committed_protected_file_by_any_name(self):
        _commit(self.manager, self.repo, ".env", "SECRET=1\n")
        _commit(self.manager, self.repo, "copy.txt", "SECRET=1\n")  # the same blob, safe name
        _commit(self.manager, self.repo, "secret.pem", "KEY\n")
        n6 = self.n6()
        blob = stage_git.run(self.repo, "rev-parse", "HEAD:.env").decode().strip()
        for args in (("cat-file", "-p", "HEAD:.env"), ("cat-file", "-p", "HEAD:.ENV"),
                     ("cat-file", "-p", "HEAD:./.env"), ("cat-file", "-s", "HEAD:.env"),
                     ("cat-file", "-p", "HEAD:secret.pem"), ("ls-tree", "HEAD", "--", ".env"),
                     ("rev-parse", "HEAD:.env"), ("cat-file", "-p", blob),
                     ("cat-file", "blob", "HEAD:keep.txt"), ("diff-tree", "-p", "HEAD~1", "HEAD"),
                     ("cat-file", "-p", "HEAD:secret.pem^{blob}"), ("for-each-ref", "--format=%(raw)"),
                     ("cat-file", "--batch"), ("cat-file", "-p", "HEAD:keep.txt", "HEAD:.env"),
                     ("cat-file", "-s", blob), ("cat-file", "-t", blob), ("cat-file", "-e", blob),
                     ("cat-file", "-p", "HEAD:copy.txt"), ("cat-file", "-s", "HEAD:copy.txt")):
            with self.subTest(args=args), self.assertRaises(PermissionError):
                n6.git(self.repo, *args)
        self.assertEqual(n6.git(self.repo, "cat-file", "-p", "HEAD:keep.txt").strip(), b"keep")
        self.assertIn(b".env", n6.git(self.repo, "ls-tree", "--name-only", "HEAD"))
        self.assertEqual(n6.git(self.repo, "diff-tree", "--name-only", "--no-commit-id", "-r",
                                "HEAD~1", "HEAD").strip(), b"secret.pem")

    def swap(self, parent):
        parent.rename(parent.with_name("moved"))
        elsewhere = parent.with_name("elsewhere")
        elsewhere.mkdir()
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(elsewhere), str(parent))
        else:
            os.symlink(elsewhere, parent, target_is_directory=True)
        return elsewhere / "notes.jsonl"

    def test_f3_a_junction_swapped_in_before_the_note_is_refused(self):
        n6 = self.n6()
        n6.note("first")
        target = self.swap(n6._notes.parent)
        with self.assertRaisesRegex(PermissionError, "moved"):
            n6.note("redirected")
        self.assertFalse(target.exists())

    def test_f3_a_junction_swapped_in_after_the_name_check_is_refused_on_the_opened_handle(self):
        n6 = self.n6()
        n6.note("first")
        parent, real, swapped = n6._notes.parent, Path.mkdir, []

        def mkdir(path, *args, **kwargs):
            real(path, *args, **kwargs)
            if path == parent and not swapped:
                swapped.append(self.swap(parent))
        with mock.patch.object(Path, "mkdir", mkdir), \
                self.assertRaisesRegex(PermissionError, "changed while it was opened"):
            n6.note("redirected")
        self.assertTrue(swapped)
        self.assertEqual(swapped[0].read_text(encoding="utf-8") if swapped[0].exists() else "", "")

    def test_the_app_does_not_import_n6_root(self):
        code = "import sys, jarvis.core; assert 'jarvis.n6_root' not in sys.modules"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        users = [path.name for path in (ROOT / "jarvis").rglob("*.py")
                 if path.name != "n6_root.py" and "n6_root" in path.read_text(encoding="utf-8")]
        self.assertEqual(users, [])

    def test_schema_version_stays_10(self):
        self.assertEqual(memory.SCHEMA_VERSION, 10)


if __name__ == "__main__":
    unittest.main()
