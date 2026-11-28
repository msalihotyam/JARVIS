"""PRECHECK1: plain-code slice precheck. Fake git runner only; no network, no live DB, no clock."""

import ast
import contextlib
import dataclasses
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import slice_precheck as sp
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, deep_json, each, \
    narrow_stdout

ROOT = Path(__file__).resolve().parents[1]
BASE, HEAD, OTHER = "a" * 40, "b" * 40, "c" * 40
PF = "project_" + "folders"
BEAT = "def " + "heart" + "beat"
SPEC = ("@jarvis lane=DEMO1 model=opus effort=high base=" + BASE + "\nT1. BASE AT " + BASE + " (main).\n"
        "Run: python scripts/test_parallel.py tests.test_demo\n"
        "Owned files: jarvis/demo.py (new), jarvis/face/panel.py (new), tests/test_demo.py (new), "
        "scripts/test_durations.d/DEMO1.json (new). Anything else -> STOP (C2).\n")
REPORT = ("STATUS: PASS\nBRANCH: demo1\nCOMMIT: " + HEAD + "\nPARENT: " + BASE + "\n"
          "RUNS: python scripts/test_parallel.py tests.test_demo -> 4 tests, 0 failures, exit 0\n")
FACE_SRC = 'SHELL_IMPORTS = {"sys", "json"}\nPURE_IMPORTS = {"re", "urllib.parse"}\nDENY = r"^jarvis\\.(ui|core)"\n'
ISO_SRC = ("import unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n    def test_iso(self):\n"
           '        for p in Path("jarvis").rglob("*.py"):\n            if p.name != "secret_mod.py":\n'
           '                self.assertNotIn("secret_mod", p.read_text())\n')
PF_SRC = 'allowed = {"connectors/folder.py"}\n'
CONV_SRC = 'PROTECTED_DIRS = (".github/workflows/", ".claude/")\nPROTECTED_FILES = {"scripts/conveyor.py"}\n'


def clean():
    """A clean slice shaped like FACEDEV1: a face module, a jarvis module, its test and its duration record."""
    return {
        "spec": SPEC,
        "changed_files": [("A", "jarvis/demo.py"), ("A", "jarvis/face/panel.py"), ("A", "tests/test_demo.py"),
                          ("A", "scripts/test_durations.d/DEMO1.json")],
        "texts": {"jarvis/demo.py": b'"""Demo values."""\nVALUE = 1\n',
                  "jarvis/face/panel.py": b"import json\nimport re\nfrom urllib.parse import quote\n",
                  "tests/test_demo.py": b"import unittest\n",
                  "scripts/test_durations.d/DEMO1.json": b'{"version": 1, "classes": {"tests.test_demo.T": 1.0}}'},
        "report": REPORT,
        "facts": {"head": HEAD, "parent": BASE, "main": BASE, "old": {},
                  "head_files": ["jarvis/demo.py", "jarvis/secret_mod.py", "jarvis/face/panel.py",
                                 "tests/test_demo.py", "tests/test_iso.py", "scripts/test_durations.d/DEMO1.json"],
                  "sources": {sp.FACE_GUARD: FACE_SRC, "tests/test_iso.py": ISO_SRC, sp.PF_GUARD: PF_SRC,
                              sp.CONVEYOR: CONV_SRC}},
    }


def run(case=None, **over):
    case = case or clean()
    case.update(over)
    return sp.check(case["spec"], case["changed_files"], case["texts"], case["report"], case["facts"])


def codes(case=None, **over):
    return [f.code for f in run(case, **over)]


def with_text(path, data, status="A"):
    case = clean()
    case["texts"][path] = data
    if all(p != path for _, p in case["changed_files"]):
        case["changed_files"].append((status, path))
    return case


class CheckTests(unittest.TestCase):
    def test_clean_synthetic_slice_has_no_findings(self):
        self.assertEqual(run(), [])

    def test_finding_is_frozen(self):
        f = sp.Finding("SCOPE", "x", 1, "m")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            f.code = "OTHER"

    def test_check_never_raises_on_garbage(self):
        for args in [(None,) * 5, (SPEC, 5, 5, 5, 5), (SPEC, [("M", None), (1, 2), ()], {1: 2}, b"\xff", [])]:
            out = sp.check(*args)
            self.assertIsInstance(out, list)
            self.assertTrue(all(isinstance(f, sp.Finding) for f in out))

    def test_stacked_base_is_accepted_and_foreign_parent_is_not(self):
        case = clean()
        case["facts"]["main"] = OTHER
        self.assertEqual(codes(case), [])
        case["facts"]["parent"] = "d" * 40
        self.assertEqual(codes(case), ["BASE"])

    def test_guard_word_heartbeat_and_pf_import(self):
        self.assertEqual(codes(with_text("jarvis/demo.py", BEAT + "():\n    pass\n")), ["GUARD_WORD"])
        self.assertEqual(codes(with_text("jarvis/demo.py", "from jarvis import " + PF + "\n")), ["GUARD_WORD"])
        case = with_text("jarvis/demo.py", "from jarvis import " + PF + "\n")
        del case["facts"]["sources"][sp.PF_GUARD]
        self.assertEqual(codes(case), ["CHECKER_ERROR"])

    def test_run_line_module_must_exist(self):
        self.assertEqual(codes(spec=SPEC.replace("tests.test_demo", "tests.test_missing")), ["RUN_LINE"])

    def test_report_shapes(self):
        self.assertEqual(codes(report=REPORT + "COMMIT: " + HEAD + "\n"), ["REPORT"])
        self.assertEqual(codes(report=REPORT.replace(HEAD, OTHER)), ["REPORT"])
        self.assertEqual(codes(report=REPORT.replace("0 failures", "2 failures")), ["REPORT"])
        self.assertEqual(codes(report=REPORT.replace("STATUS: PASS", "status ok")), ["REPORT"])
        blocked = REPORT.replace("STATUS: PASS", "STATUS: BLOCKED").replace("COMMIT: " + HEAD, "COMMIT: none")
        self.assertEqual(codes(report=blocked), [])

    def test_face_module_reads_deny_regex_and_relative_imports(self):
        self.assertEqual(codes(with_text("jarvis/face/panel.py", "from jarvis.core import x\n")), ["FACE_ALLOW"])
        self.assertEqual(codes(with_text("jarvis/face/panel.py", "from . import x\n")), ["FACE_ALLOW"])
        self.assertEqual(codes(with_text("jarvis/face/panel.py", "import urllib.parse\n")), ["FACE_ALLOW"])


class NegativeTests(unittest.TestCase):
    def test_protected_files_are_scope_findings(self):
        for path in ("data/jarvis.db", ".env", "uv.lock", "scripts/test_durations.json", sp.CONVEYOR,
                     ".claude/settings.json", "dev/dashboard/x.py", "certs/server.pem"):
            with self.subTest(path=path):
                case = clean()
                case["changed_files"].append(("M", path))
                self.assertEqual(codes(case), ["SCOPE"])

    def test_owning_a_protected_path_does_not_exempt_it(self):
        for path in (sp.CONVEYOR, "sub/uv.lock", "assets/riot/x.py", ".claude/settings.json"):
            with self.subTest(path=path):
                case = clean()
                case["spec"] = SPEC.replace("jarvis/demo.py (new)", f"jarvis/demo.py (new), {path}")
                case["changed_files"].append(("M", path))
                self.assertEqual(codes(case), ["SCOPE"])

    def test_missing_conveyor_protected_set_is_a_checker_error(self):
        for src in (None, "x = 1\n", "PROTECTED_DIRS = ()\nPROTECTED_FILES = set()\n"):
            with self.subTest(src=src):
                case = clean()
                case["facts"]["sources"][sp.CONVEYOR] = src
                self.assertEqual(codes(case), ["CHECKER_ERROR"])

    def test_pass_with_nonzero_exit_or_a_later_failing_run(self):
        self.assertEqual(codes(report=REPORT.replace("exit 0", "exit 1")), ["REPORT"])
        later = REPORT + "RUNS: python scripts/test_parallel.py tests.test_x -> 3 tests, 1 failures, exit 1\n"
        self.assertEqual(codes(report=later), ["REPORT"])
        self.assertEqual(codes(report=REPORT + "Note: the COMMIT: line above is final\n"), [])

    def test_class_qualified_run_line_and_trailing_period(self):
        self.assertEqual(codes(spec=SPEC.replace("tests.test_demo", "tests.test_demo.T.test_a.")), [])

    def test_outside_owned_rename_into_scope_and_deleted_guard_test(self):
        for change in (("M", "jarvis/other.py"), ("R100", "jarvis/demo.py", "jarvis/elsewhere.py"),
                       ("D", "tests/test_iso.py"), ("R090", "tests/test_demo.py", "tests/test_iso.py")):
            with self.subTest(change=change):
                case = clean()
                case["changed_files"] = [c for c in case["changed_files"] if c[1] != change[1]] + [change]
                self.assertEqual(codes(case), ["SCOPE"])

    def test_banned_name_in_comment_or_docstring(self):
        for text in ("# reads secret_mod later\nVALUE = 1\n", '"""Talks to secret_mod for data."""\nVALUE = 1\n'):
            with self.subTest(text=text):
                self.assertEqual(codes(with_text("jarvis/demo.py", text)), ["BANNED_NAME"])

    def test_malformed_spec_and_missing_report(self):
        for spec in (None, "", "no header\nOwned files: a/b", b"\xff\xfe", "@jarvis lane=X\nRun: tests.x\n"):
            with self.subTest(spec=spec):
                self.assertEqual(codes(spec=spec), ["SPEC"])
        self.assertEqual(codes(report=None), ["REPORT"])

    def test_binary_invalid_utf8_and_oversized_text(self):
        class Huge:
            def __len__(self):
                return 50 * 1024 * 1024
        for data in (b"\x00\x01\x02", b"\xff\xfe bad", Huge()):
            with self.subTest(data=type(data).__name__):
                self.assertEqual(codes(with_text("jarvis/demo.py", data)), ["INPUT"])
        self.assertEqual(codes(report=b"\xffSTATUS"), ["INPUT"])


class FixFixtureTests(unittest.TestCase):
    """This week's FIX reasons rebuilt as fixtures; each gives exactly its own code."""

    def test_docstring_names_a_guarded_module(self):
        text = '"""Panel values; mirrors what secret_mod keeps."""\nVALUE = 1\n'
        self.assertEqual(codes(with_text("jarvis/demo.py", text)), ["BANNED_NAME"])

    def test_missing_duration_record(self):
        case, record = clean(), "scripts/test_durations.d/DEMO1.json"
        case["changed_files"] = [c for c in case["changed_files"] if c[1] != record]
        del case["texts"][record]
        case["facts"]["head_files"].remove(record)
        self.assertEqual(codes(case), ["DURATION"])

    def test_malformed_duration_record(self):
        self.assertEqual(codes(with_text("scripts/test_durations.d/DEMO1.json", b'{"version": 1, "classes": [')),
                         ["DURATION"])

    def test_face_module_not_in_allow_list_until_the_same_diff_names_it(self):
        case = with_text("jarvis/face/panel.py", b"import os\n")
        self.assertEqual(codes(case), ["FACE_ALLOW"])
        case["spec"] = SPEC.replace("tests/test_demo.py (new)", "tests/test_demo.py (new), " + sp.FACE_GUARD)
        case["changed_files"].append(("M", sp.FACE_GUARD))
        case["texts"][sp.FACE_GUARD] = FACE_SRC + 'SHELL_IMPORTS |= {"os"}  # panel.py reads its folder\n'
        self.assertEqual(codes(case), [])

    def test_commit_line_malformed(self):
        for bad in ("COMMIT: `" + HEAD + "`", "COMMIT: " + HEAD[:7], "COMMIT " + HEAD, "COMMIT: " + HEAD.upper()):
            with self.subTest(bad=bad):
                self.assertEqual(codes(report=REPORT.replace("COMMIT: " + HEAD, bad)), ["REPORT"])


class HostileTests(unittest.TestCase):
    def test_hostile_paths_are_scope_findings(self):
        def probe(part):
            for path in (part, "jarvis/" + part + "/x.py"):
                case = clean()
                case["changed_files"].append(("M", path))
                found = codes(case)
                self.assertIn("SCOPE", found)
                self.assertNotIn("CHECKER_ERROR", found)
        each(self, HOSTILE_PATH_PARTS, probe)

    def test_hostile_text_never_raises(self):
        def probe(text):
            for found in (codes(report=text), codes(with_text("jarvis/demo.py", text))):
                self.assertNotIn("CHECKER_ERROR", found)
        each(self, HOSTILE_TEXT, probe)

    def test_hostile_json_duration_record(self):
        def probe(text):
            found = codes(with_text("scripts/test_durations.d/DEMO1.json", text))
            self.assertIn(found, ([], ["DURATION"]))
        each(self, HOSTILE_JSON + (deep_json(),), probe)

    def test_hostile_numbers_in_duration_record(self):
        def probe(value):
            text = '{"version": 1, "classes": {"tests.test_demo.T": ' + sp.json.dumps(value) + "}}"
            found = codes(with_text("scripts/test_durations.d/DEMO1.json", text))
            self.assertEqual(found, [] if value == 0 and not isinstance(value, bool) else ["DURATION"])
        each(self, HOSTILE_NUMBERS, probe)

    def test_render_survives_a_narrow_console(self):
        findings = [sp.Finding("SCOPE", t[:200], 0, t[:200]) for t in HOSTILE_TEXT]
        with narrow_stdout() as stream:
            print(sp.render(findings))
            stream.flush()


class RealGuardTests(unittest.TestCase):
    """The lists come from the guard modules on disk; nothing is copied by hand."""

    def test_reads_the_real_guard_lists(self):
        allowed, deny = sp.face_allowed((ROOT / sp.FACE_GUARD).read_text(encoding="utf-8"))
        self.assertIn("urllib.parse", allowed)
        self.assertTrue(deny)
        tree = ast.parse((ROOT / sp.CONVEYOR).read_text(encoding="utf-8"))
        self.assertIn("scripts/conveyor.py", sp._literal(tree, "PROTECTED_FILES"))
        self.assertTrue(sp._literal(ast.parse((ROOT / sp.PF_GUARD).read_text(encoding="utf-8")), "allowed"))


class StaticTests(unittest.TestCase):
    def test_no_write_network_or_process_call_outside_the_runner(self):
        tree = ast.parse((ROOT / "scripts" / "slice_precheck.py").read_text(encoding="utf-8"))
        runner = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_default_runner")
        inside = {id(n) for n in ast.walk(runner)}
        allowed = {"argparse", "ast", "contextlib", "io", "json", "posixpath", "re", "sys", "dataclasses", "pathlib"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in inside:
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                self.assertLessEqual(set(names), allowed)
            if isinstance(node, ast.Name) and node.id == "subprocess":
                self.assertIn(id(node), inside)
            if isinstance(node, ast.Call):
                self.assertFalse(banned_call(node.func), ast.unparse(node))
            if isinstance(node, ast.FunctionDef):
                self.assertFalse(any(w in node.name for w in ("write", "review", "queue", "save", "dump", "receipt")))

    def test_attribute_open_is_banned(self):  # F19
        self.assertTrue(banned_call(ast.parse('Path("queue").open("w")').body[0].value.func))


def banned_call(fn):
    return (isinstance(fn, ast.Name) and fn.id in ("open", "exec", "eval", "__import__")) or (
        isinstance(fn, ast.Attribute) and fn.attr in (
            "open", "write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "rename", "rmtree", "remove"))


def fake_runner(case, fail=False, calls=None):
    files = {**{k: v.encode() for k, v in case["facts"]["sources"].items()}, **case["texts"]}
    shas = {"HEAD": case["facts"]["head"], "HEAD^": case["facts"]["parent"], "main": case["facts"]["main"]}

    def runner(args):
        if calls is not None:
            calls.append(args)
        if fail:
            return "not a tuple"
        if args[0] == "rev-parse":
            ref = args[-1][:-len("^{commit}")]
            sha = ref if sp.SHA_RE.fullmatch(ref) else shas.get(ref)
            return (0, sha.encode() + b"\n") if sha else (1, b"")
        if args[0] == "merge-base":
            return (1, b"") if case["facts"].get("ancestor") is False else (0, b"")
        if args[0] == "show":
            data = files.get(args[1].split(":", 1)[1])
            return (0, data) if data is not None else (128, b"")
        if "--name-status" in args:
            return 0, b"".join("".join(f"{t}\0" for t in (c[0], *c[2:], c[1])).encode() for c in case["changed_files"])
        if "-Grglob" in args:
            return 0, b"tests/test_iso.py\0"
        return 0, "\0".join(case["facts"]["head_files"]).encode()
    return runner


class CliTests(unittest.TestCase):
    def cli(self, argv, runner, spec=SPEC, report=REPORT, size=None):
        with tempfile.TemporaryDirectory() as tmp:
            sp_path, rp_path = os.path.join(tmp, "spec.md"), os.path.join(tmp, "report.md")
            Path(sp_path).write_text(spec, encoding="utf-8")
            Path(rp_path).write_text(report, encoding="utf-8")
            if size:
                os.truncate(sp_path, size)  # sparse; C11: no real large file
            argv = [a.replace("{spec}", sp_path).replace("{report}", rp_path) for a in argv]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sp.main(argv, runner=runner)
        out = buf.getvalue()
        self.assertEqual(out.splitlines()[0], sp.NOTICE)
        return rc, out

    def test_exit_codes(self):
        argv = ["--spec", "{spec}", "--base", BASE, "--report", "{report}"]
        self.assertEqual(self.cli(argv, fake_runner(clean()))[0], 0)
        foreign = clean()
        foreign["facts"]["parent"] = OTHER
        rc, out = self.cli(argv, fake_runner(foreign))
        self.assertEqual((rc, '"BASE"' in out), (1, True))
        self.assertEqual(self.cli(argv, fake_runner(clean(), fail=True))[0], 2)
        self.assertEqual(self.cli(argv[:3] + ["../x"] + argv[4:], fake_runner(clean()))[0], 2)
        self.assertEqual(self.cli(["--spec", "missing.md", "--base", BASE, "--report", "x"], fake_runner(clean()))[0], 2)
        self.assertEqual(self.cli(["--spec"], fake_runner(clean()))[0], 2)
        self.assertEqual(self.cli(["--help"], fake_runner(clean()))[0], 0)

    def test_fifty_megabyte_spec_is_an_input_finding(self):
        rc, out = self.cli(["--spec", "{spec}", "--base", BASE, "--report", "{report}"], fake_runner(clean()),
                           size=50 * 1024 * 1024)
        self.assertEqual((rc, '"INPUT"' in out), (1, True))

    def test_git_runner_admits_read_commands_only(self):
        ok = fake_runner(clean())
        for args in (("push",), ("diff", "--output=x"), ("log", "-c", "x"), ("show", "--ext-diff"), (), ("diff", 5)):
            with self.subTest(args=args), self.assertRaises(PermissionError):
                sp._git(ok, *args)
        with self.assertRaises(RuntimeError):
            sp._git(fake_runner(clean(), fail=True), "log")

    def test_guard_discovery_diff_disables_textconv(self):
        calls = []
        self.cli(["--spec", "{spec}", "--base", BASE, "--report", "{report}"], fake_runner(clean(), calls=calls))
        grep = [c for c in calls if "-Grglob" in c]
        self.assertTrue(grep and all("--no-textconv" in c and "--no-ext-diff" in c for c in grep), calls)

    def test_base_head_or_non_ancestor_is_a_base_finding(self):
        rc, out = self.cli(["--spec", "{spec}", "--base", "HEAD", "--report", "{report}"], fake_runner(clean()))
        self.assertEqual((rc, '"BASE"' in out), (1, True))
        case = clean()
        case["facts"]["ancestor"] = False
        rc, out = self.cli(["--spec", "{spec}", "--base", BASE, "--report", "{report}"], fake_runner(case))
        self.assertEqual((rc, '"BASE"' in out), (1, True))

    def test_unreadable_changed_python_is_a_checker_error(self):
        case = clean()
        del case["texts"]["jarvis/demo.py"]
        rc, out = self.cli(["--spec", "{spec}", "--base", BASE, "--report", "{report}"], fake_runner(case))
        self.assertEqual((rc, "CHECKER_ERROR" in out), (2, True))

    def test_usage_error_renders_json(self):
        rc, out = self.cli(["--spec"], fake_runner(clean()))
        self.assertEqual((rc, "```json" in out, '"CHECKER_ERROR"' in out), (2, True, True))
        self.assertEqual(self.cli(["--help"], fake_runner(clean()))[0], 0)


class FixRoundTests(unittest.TestCase):
    def base_codes(self, base, ancestor=True):
        case = clean()
        case["facts"].update(base=base, base_is_ancestor=ancestor)
        return codes(case)

    def test_base_must_be_an_ancestor_parent_not_head(self):
        self.assertEqual(self.base_codes(BASE), [])
        self.assertEqual(self.base_codes(HEAD), ["BASE"])
        self.assertEqual(self.base_codes(OTHER), ["BASE"])
        self.assertEqual(self.base_codes(BASE, ancestor=False), ["BASE"])

    def test_exception_with_raising_str_stays_a_finding(self):
        class Boom(Exception):
            def __str__(self):
                raise ValueError("no str")
        with mock.patch.object(sp, "_check", side_effect=Boom()):
            self.assertEqual(codes(), ["CHECKER_ERROR"])

    def test_ownership_is_case_exact(self):
        case = clean()
        case["changed_files"].append(("M", "jarvis/DEMO.py"))
        self.assertEqual(codes(case), ["SCOPE"])

    def test_rename_or_copy_needs_a_source_path(self):
        for entry in (("R100", "jarvis/demo.py"), ("R100", "jarvis/demo.py", ""), ("C100", "jarvis/demo.py")):
            with self.subTest(entry=entry):
                case = clean()
                case["changed_files"][0] = entry
                self.assertIn("SCOPE", codes(case))

    def test_only_the_lanes_own_duration_record_counts(self):
        lane, other = "scripts/test_durations.d/DEMO1.json", "scripts/test_durations.d/OTHER.json"
        case = clean()
        case["spec"] = SPEC.replace("DEMO1.json (new)", "DEMO1.json (new), " + other + " (new)")
        case["changed_files"] = [c for c in case["changed_files"] if c[1] != lane] + [("A", other)]
        case["texts"][other] = case["texts"].pop(lane)
        case["facts"]["head_files"] = [p for p in case["facts"]["head_files"] if p != lane] + [other]
        self.assertEqual(codes(case), ["DURATION"])

    def test_lane_record_at_head_is_validated(self):
        lane = "scripts/test_durations.d/DEMO1.json"
        for record, want in ((b'{"version": 1, "classes": [', ["DURATION"]), (b"\xff\xfe", ["DURATION"]),
                             (b'{"version": 1, "classes": {"tests.test_demo.T": 1.0}}', [])):
            with self.subTest(record=record):
                case = clean()
                case["changed_files"] = [c for c in case["changed_files"] if c[1] != lane]
                del case["texts"][lane]
                case["facts"]["lane_record"] = record
                self.assertEqual(codes(case), want)

    def test_negative_count_is_not_a_count(self):
        self.assertEqual(codes(report=REPORT.replace("4 tests", "-1 tests")), ["REPORT"])

    def test_allow_list_name_is_bounded(self):
        case = with_text("jarvis/face/panel.py", b"import os\n")
        case["spec"] = SPEC.replace("tests/test_demo.py (new)", "tests/test_demo.py (new), " + sp.FACE_GUARD)
        case["changed_files"].append(("M", sp.FACE_GUARD))
        case["texts"][sp.FACE_GUARD] = FACE_SRC + 'SHELL_IMPORTS |= {"os"}  # panel.py.tmp\n'
        self.assertEqual(codes(case), ["FACE_ALLOW"])


ADV_HEAD, ADV_BASE = "7b1401c5a3a2faf5b8a7dc396d8cd0e517ecc559", "7fcb07703e9efc530f744ee00371eeb0cb5c9670"
ADV_FILES = ("tests/adversarial.py", "tests/test_adversarial.py", "scripts/test_durations.d/ADVCORPUS1.json")
ADV_SPEC = ("@jarvis lane=ADVCORPUS1 model=opus effort=high base=" + ADV_BASE + "\n"
            "Run: python scripts/test_parallel.py tests.test_adversarial\n"
            "Owned files: tests/adversarial.py (new), tests/test_adversarial.py (new), "
            "scripts/test_durations.d/ADVCORPUS1.json (new) [classes map for tests.test_adversarial], "
            "build/reports/ADVCORPUS1.md (new). Anything else -> STOP (C2). Do NOT edit scripts/test_durations.json.\n")
ADV_REPORT = ("STATUS: PASS\nBRANCH: worktree-ADVCORPUS1\nCOMMIT: " + ADV_HEAD + "\nPARENT: " + ADV_BASE + "\n"
              "FILES: tests/adversarial.py, tests/test_adversarial.py, scripts/test_durations.d/ADVCORPUS1.json, "
              "build/reports/ADVCORPUS1.md\n"
              "RUNS: python scripts/test_parallel.py tests.test_adversarial -> 10 tests, 0 failures, exit 0; "
              "ruff clean; git diff --check clean\n"
              "CHANGED ASSERTS: none\nADJACENT FILES: none\nPRE-EXISTING: none\nGIT STATUS --SHORT: clean\nPUSHED: no\n"
              "INTEGRATION LANE NEEDS: tests.test_adversarial\n")


class RealSliceTests(unittest.TestCase):
    def test_clean_historical_slice_has_no_findings(self):
        """ADVCORPUS1 (7b1401c, integrated as f98113f): its spec, report and diff frozen; blobs unchanged at HEAD."""
        tree = [p.relative_to(ROOT).as_posix() for d in ("jarvis", "scripts", "tests") for p in (ROOT / d).rglob("*.py")]
        guards = [p for p in tree if p.startswith("tests/test_") and "rglob" in (ROOT / p).read_text(encoding="utf-8")]
        sources = {p: (ROOT / p).read_text(encoding="utf-8") for p in {sp.FACE_GUARD, sp.PF_GUARD, sp.CONVEYOR, *guards}}
        facts = {"head": ADV_HEAD, "parent": ADV_BASE, "main": ADV_BASE, "base": ADV_BASE, "base_is_ancestor": True,
                 "old": {}, "head_files": sorted({*tree, *ADV_FILES}), "sources": sources}
        texts = {p: (ROOT / p).read_bytes() for p in ADV_FILES}
        self.assertEqual(sp.check(ADV_SPEC, [("A", p) for p in ADV_FILES], texts, ADV_REPORT, facts), [])
