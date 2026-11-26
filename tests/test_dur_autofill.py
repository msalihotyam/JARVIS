"""DURAUTO1: scripts/dur_autofill.py finds a missing or broken duration record before INT and
proposes the DUR slice that adds it. Temp directories only; never data/jarvis.db."""
import ast
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, deep_json, each, narrow_stdout

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dur_autofill.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


da = _load("dur_autofill_under_test", SCRIPT)

TWO_CLASSES = (
    "import unittest\n\n"
    "class AlphaTests(unittest.TestCase):\n    def test_a(self):\n        pass\n\n"
    "class _Base(unittest.TestCase):\n    def helper(self):\n        pass\n\n"
    "class BetaTests(_Base):\n    def test_b(self):\n        pass\n\n"
    "class Mixin:\n    def test_not_a_case(self):\n        pass\n"
)


class Tree:
    """A fake repo root: tests/, scripts/test_durations.json, scripts/test_durations.d/, build/."""

    def __init__(self, base: str):
        self.root = Path(base)
        self.d = self.root / "scripts" / "test_durations.d"
        self.d.mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "build" / "conveyor" / "prompts").mkdir(parents=True)
        self.main({})

    def main(self, classes: dict):
        (self.root / "scripts" / "test_durations.json").write_text(json.dumps({"classes": classes}), encoding="utf-8")

    def module(self, name: str, text: str = TWO_CLASSES):
        (self.root / "tests" / f"{name}.py").write_text(text, encoding="utf-8")

    def record(self, stem: str, text) -> Path:
        path = self.d / f"{stem}.json"
        path.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
        return path

    def scan(self, changed, known=()):
        return da.scan(changed, self.d, known, root=self.root)


class _TreeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Tree(tmp.name)


class ScanTests(_TreeCase):
    def test_absent_when_no_class_is_recorded(self):
        self.t.module("test_new")
        self.assertEqual(self.t.scan({"tests.test_new": "SLICEA"}), [da.Missing("tests.test_new", "SLICEA", "absent")])

    def test_module_not_listed_when_one_class_is_missing(self):
        self.t.module("test_new")
        self.t.record("DUROLD", {"classes": {"tests.test_new.AlphaTests": 1.0}})
        self.assertEqual(self.t.scan([("tests.test_new", "SLICEA")]),
                         [da.Missing("tests.test_new", "SLICEA", "module_not_listed")])

    def test_complete_record_in_main_or_d_is_clean(self):
        self.t.module("test_new")
        self.t.main({"tests.test_new.AlphaTests": 1.0})
        self.t.record("DURX", {"version": 1, "classes": {"tests.test_new.BetaTests": 2.5}})
        self.assertEqual(self.t.scan({"tests.test_new": "SLICEA"}), [])

    def test_test_classes_is_static_and_follows_local_bases(self):
        self.t.module("test_new")
        self.assertEqual(da.test_classes(self.t.root / "tests" / "test_new.py"), ["AlphaTests", "BetaTests"])
        self.t.module("test_broken", "class (:\n")
        self.assertIsNone(da.test_classes(self.t.root / "tests" / "test_broken.py"))

    def test_unparseable_record_is_reported_and_not_taken_as_a_record(self):
        self.t.module("test_new")
        self.t.record("DURBAD", "{not json")
        self.assertEqual(self.t.scan({"tests.test_new": "SLICEA"}),
                         [da.Missing("", "DURBAD", "unparseable"), da.Missing("tests.test_new", "SLICEA", "absent")])

    def test_known_dur_slice_or_deleted_module_is_not_reported(self):
        self.t.module("test_new")
        self.assertEqual(self.t.scan({"tests.test_new": "SLICEA"}, known={"DURSLICEA"}), [])
        self.t.record("DURSLICEB", {"classes": {}})        # landed record file counts as known too
        self.assertEqual(self.t.scan({"tests.test_new": "SLICEB"}), [])
        self.assertEqual(self.t.scan({"tests.test_gone": "SLICEA"}), [])

    def test_missing_durations_dir_never_raises(self):
        self.assertEqual(da.scan({"tests.test_x": "A"}, self.t.root / "nope", (), root=self.t.root), [])
        self.assertEqual(da.scan(None, self.t.d, None, root=self.t.root), [])


class ProposeTests(_TreeCase):
    def test_one_spec_per_slice_with_exactly_the_one_record(self):
        self.t.module("test_one")
        self.t.module("test_two")
        missing = self.t.scan({"tests.test_one": "SLICEA", "tests.test_two": "SLICEA"})
        specs = da.propose(missing, "a" * 40, self.t.root)
        self.assertEqual(list(specs), ["DURSLICEA"])
        text = specs["DURSLICEA"]
        self.assertTrue(text.startswith("@jarvis lane=DURSLICEA model=sonnet effort=medium base=" + "a" * 40 + "\n"))
        self.assertIn("\nT1. ", text)
        self.assertIn("Owned files: scripts/test_durations.d/DURSLICEA.json (new), build/reports/DURSLICEA.md (new). "
                      "Anything else -> STOP (C2).", text)
        self.assertIn("tests.test_one.AlphaTests, tests.test_one.BetaTests, tests.test_two.AlphaTests", text)
        self.assertNotIn("Mixin", text)

    def test_deterministic_same_input_same_bytes(self):
        self.t.module("test_one")
        self.t.module("test_two")
        a = da.propose(self.t.scan({"tests.test_one": "B", "tests.test_two": "A"}), root=self.t.root)
        b = da.propose(list(reversed(self.t.scan([("tests.test_two", "A"), ("tests.test_one", "B")]))), root=self.t.root)
        self.assertEqual(json.dumps(a).encode(), json.dumps(b).encode())
        self.assertEqual(list(a), ["DURA", "DURB"])
        self.assertIn("base=<BASE>", a["DURA"])

    def test_nothing_for_orphans_unparseable_or_hostile_entries(self):
        missing = [da.Missing("tests.test_x", "A", "orphan"), da.Missing("", "DURBAD", "unparseable"),
                   da.Missing("tests.test_x", "a/../B", "absent"), ("tests.test_x", "A", "absent"), None]
        self.assertEqual(da.propose(missing, "x; rm", self.t.root), {})


class AdversarialTests(_TreeCase):
    def test_hostile_ids_and_paths_are_reported_never_raised(self):
        self.t.module("test_new")

        def check(value):
            valid = isinstance(value, str) and da.SLICE_RE.fullmatch(value) is not None   # CON, COM1, DATA: plain ids
            for pair, reason in (((value, "SLICEA"), "unparseable"), ((value, value), "unparseable"),
                                 (("tests.test_new", value), "absent" if valid else "unparseable")):
                got = self.t.scan([pair])
                self.assertEqual([m.reason for m in got], [reason])
                self.assertEqual(list(da.propose(got, root=self.t.root)), [f"DUR{value}"] if reason == "absent" else [])
                with narrow_stdout():
                    print(got)
        each(self, HOSTILE_PATH_PARTS + HOSTILE_TEXT + HOSTILE_NUMBERS, check)
        self.assertEqual([m.reason for m in self.t.scan([("x",), 5, "ab"])], ["unparseable"] * 3)

    def test_hostile_record_keys_are_reported(self):
        def check(value):
            self.t.record("DURK", json.dumps({"classes": {value: 1.0}}))
            got = self.t.scan({})
            self.assertEqual([m.reason for m in got], ["unparseable"])
            with narrow_stdout():
                print(got)
        each(self, HOSTILE_PATH_PARTS + HOSTILE_TEXT, check)

    def test_duplicate_key_non_json_deep_and_huge_records_are_unparseable(self):
        self.t.record("DURDUP", '{"classes": {"tests.test_a.X": 1.0, "tests.test_a.X": 2.0}}')
        self.t.record("DURTXT", "not json at all")
        self.t.record("DURDEEP", deep_json())
        huge = self.t.record("DURHUGE", "{}")
        os.truncate(huge, da.MAX_RECORD_BYTES + 1)
        self.t.record("DURNUL", b"\xff\xfe\x00".decode("latin-1"))
        got = self.t.scan({})
        self.assertEqual(got, [da.Missing("", s, "unparseable") for s in ("DURDEEP", "DURDUP", "DURHUGE", "DURNUL", "DURTXT")])

    def test_duplicate_record_across_files_is_accepted_like_int(self):
        self.t.module("test_a", "import unittest\nclass X(unittest.TestCase):\n    def test_x(self):\n        pass\n")
        self.t.record("DURONE", {"classes": {"tests.test_a.X": 1.0}})
        self.t.record("DURTWO", {"classes": {"tests.test_a.X": 3.0}})
        self.assertEqual(self.t.scan({"tests.test_a": "SLICEA"}), [])

    def test_parity_with_int_reader(self):
        tp = _load("test_parallel_for_dur_autofill", ROOT / "scripts" / "test_parallel.py")
        texts = [t for t in HOSTILE_JSON if '"value": "a", "value"' not in t]  # duplicates: INT keeps the last
        texts += [json.dumps({"classes": {"tests.test_a.X": v}}, allow_nan=True) for v in HOSTILE_NUMBERS]
        texts += ['{"classes": []}', '{"tests": 1}', '﻿{"classes": {"tests.test_a.X": 1.5}}',
                  '{"classes": {"tests.test_a.X": 0}}', '{"tests": {"tests.test_a.X.test_x": 2}}']

        def check(text):
            path = self.t.record("DURP", text)
            try:
                tp._read_slice_durations(path)
                int_ok = True
            except SystemExit:
                int_ok = False
            self.assertEqual(da.read_record(path) is not None, int_ok)
        each(self, texts, check)


class OrphanTests(_TreeCase):
    def test_record_for_a_deleted_module_is_orphan_without_proposal(self):
        self.t.module("test_alive", "import unittest\nclass X(unittest.TestCase):\n    def test_x(self):\n        pass\n")
        self.t.record("DURGONE", {"classes": {"tests.test_gone.X": 1.0, "tests.test_alive.X": 1.0},
                                  "tests": {"tests.test_gone2.X.test_x": 1.0}})
        got = self.t.scan({"tests.test_alive": "SLICEA"})
        self.assertEqual(got, [da.Missing("tests.test_gone", "DURGONE", "orphan"),
                               da.Missing("tests.test_gone2", "DURGONE", "orphan")])
        self.assertEqual(da.propose(got, root=self.t.root), {})


def _run(argv, root):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = da.main(argv, root=root)
    return code, out.getvalue(), err.getvalue()


class CliTests(_TreeCase):
    def test_prints_proposals_and_writes_nothing_without_out(self):
        self.t.module("test_new")
        before = sorted(self.t.root.rglob("*"))
        code, out, _ = _run(["--changed", "SLICEA:tests/test_new.py"], self.t.root)
        self.assertEqual(code, 1)
        self.assertIn("MISSING absent module=tests.test_new slice=SLICEA", out)
        self.assertIn("@jarvis lane=DURSLICEA ", out)
        self.assertEqual(sorted(self.t.root.rglob("*")), before)

    def test_clean_exit_zero_and_known_prompt_skips(self):
        self.t.module("test_new")
        (self.t.root / "build" / "conveyor" / "prompts" / "DURSLICEA.md").write_text("x", encoding="utf-8")
        self.assertEqual(_run(["--changed", "SLICEA:tests.test_new"], self.t.root)[0], 0)

    def test_out_inside_build_writes_one_spec_per_slice(self):
        self.t.module("test_new")
        code, _, _ = _run(["--changed", "SLICEA:tests.test_new", "--out", "build/dur"], self.t.root)
        self.assertEqual(code, 1)
        want = da.propose(self.t.scan({"tests.test_new": "SLICEA"}), root=self.t.root)["DURSLICEA"]
        self.assertEqual((self.t.root / "build" / "dur" / "DURSLICEA.md").read_bytes(), want.encode())

    def test_out_outside_build_is_refused(self):
        self.t.module("test_new")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        for value in (outside.name, "build/../tests", "..", "build", "scripts", str(self.t.root / "tests")):
            with self.subTest(value=value):
                code, _, err = _run(["--changed", "SLICEA:tests.test_new", "--out", value], self.t.root)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)
        self.assertEqual(os.listdir(outside.name), [])

    def test_out_through_a_link_leaving_build_is_refused(self):
        link, tests = str(self.t.root / "build" / "link"), str(self.t.root / "tests")
        real = os.path.realpath
        try:
            os.symlink(tests, link, target_is_directory=True)
            patch = contextlib.nullcontext()
        except OSError:                                   # no symlink privilege: simulate what it resolves to
            patch = mock.patch.object(da.os.path, "realpath", lambda p: real(p).replace(real(link), real(tests)))
        with patch:
            code, _, err = _run(["--out", "build/link/x"], self.t.root)
        self.assertEqual((code, "refused" in err), (2, True))


class StaticTests(unittest.TestCase):
    def test_no_network_no_subprocess_no_writes_beyond_out(self):
        source = SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, {"__future__", "argparse", "ast", "json", "os", "re", "sys", "pathlib", "typing"})
        for banned in ("subprocess", "socket", "urllib", "http", "def heartbeat", "project_folders", "sqlite",
                       "write_text", "write_bytes", "unlink", "rmtree", "os.remove", "rename"):
            self.assertNotIn(banned, source)
        self.assertEqual(source.count('open(file, "w"'), 1)   # the single write, behind --out


if __name__ == "__main__":
    unittest.main()
