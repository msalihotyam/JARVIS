import inspect
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jarvis.work_plan as work_plan
from jarvis.work_plan import (SELF_DENY, SHADOW_MODE, Plan, classify,
                              shadow_candidate, validate)
from tests.test_work_plan import example

REFUSED = {"self_target", "protected_path"}


def plan(*targets):
    data = example()
    data["targets"] = list(targets)
    data["slices"][0]["targets"] = list(targets)  # the example's a.py is a root shadow
    return Plan.from_dict(data)


def codes(target, prefix=None):
    return {r.code for r in validate(plan(target), "projects/demo",
                                     jarvis_repo=True, repo_prefix=prefix)}


class SelfDenyProfileTest(unittest.TestCase):
    def test_every_entry_is_refused_whatever_the_spelling(self):
        self.assertIn("scripts/conveyor.py", SELF_DENY)
        self.assertIn(".github/workflows/", SELF_DENY)
        for entry in SELF_DENY:
            path = entry + "x.py" if entry.endswith("/") else entry
            for spelling in (path, path.upper(), path.replace("/", "\\"),
                             "JARVIS/" + path if "/" in entry else "sub/" + path):
                got = codes(spelling)
                self.assertTrue(got and got <= REFUSED, (spelling, got))

    def test_authority_modules_and_tests_are_refused(self):
        for target in ("jarvis/work_plan.py", "jarvis/work_planner.py",
                       "jarvis/work_driver.py", "jarvis/write_scope.py",
                       "jarvis/policy_proxy.py", "jarvis/workers/claude.py",
                       "jarvis/write_stage/__init__.py", "Jarvis/Core.PY",
                       "tests/test_work_selfdev.py", "tests/test_conveyor.py",
                       "AGENTS.override.md", "docs/Agents.md", ".claude/settings.json",
                       "data/jarvis.db", ".env.local", "id_ed25519"):
            got = codes(target)
            self.assertTrue(got and got <= REFUSED, (target, got))

    def test_overlap_spellings_cannot_reach_a_denied_path(self):
        for prefix, target in (("jarvis/", "work_planner.py"), ("JARVIS/", "Write_Scope.py"),
                               ("scripts/", "conveyor.py"), (".github/", "workflows/ci.yml"),
                               ("jarvis/workers/", "codex.py"), ("tests/", "test_work_plan.py"),
                               ("dev/", "x.ps1"), ("build/", "reports/PM31.md")):
            got = codes(target, prefix)
            self.assertTrue(got and got <= REFUSED, (prefix, target, got))
        # trailing dot/space, 8.3 short names and ADS never canonicalise to an allowed path
        for target in ("scripts/conveyor.py.", "scripts/conveyor.py ", "uv.lock.",
                       "SCRIPT~1/CONVEY~1.PY", "jarvis/WORK_P~1.PY", "uv.lock::$DATA"):
            self.assertTrue(codes(target), target)

    def test_benign_targets_stay_allowed(self):
        for target in ("jarvis/router_tr.py", "README.md", "jarvis/work_classify.py",
                       "tests/test_router.py", "jarvis/a.py"):
            self.assertEqual(set(), codes(target), target)
        self.assertEqual(set(), codes("router_tr.py", "jarvis/"))
        # outside the JARVIS repo the profile does not apply
        self.assertEqual([], validate(plan("dev/x.py", "uv.lock"), "projects/demo",
                                      jarvis_repo=False))

    def test_jarvis_repo_is_always_large(self):
        facts = {"target_folders": ["jarvis"], "existing_repo": True,
                 "outgoing_actions": False, "money_actions": False,
                 "manual_checks": False, "jarvis_repo": True}
        small = {"slice_count": 1, "new_dependencies": [], "connector_classes": [],
                 "operation_classes": []}
        size, reasons = classify(facts, small)
        self.assertEqual("large", size)
        self.assertIn("jarvis_repo", reasons)


def refused(test, target, prefix=None):
    got = codes(target, prefix)
    test.assertTrue(got and got <= REFUSED | {"path_escape"}, (prefix, target, got))


def tree(root, extra=None):
    for name in work_plan._SELF_FILES + ("tests/__init__.py",):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("", encoding="utf-8")
    for name, text in (extra or {}).items():
        (root / name).write_text(text, encoding="utf-8")


class ImportClosureTest(unittest.TestCase):
    # PM31FIX F6: hard-coded, not derived from SELF_DENY or the closure
    INVENTORY = (
        "scripts/conveyor.py", "scripts/test_parallel.py", "scripts/ci.ps1",
        "scripts/quality.ps1", ".github/workflows/ci.yml", ".claude/settings.json",
        "CLAUDE.md", "AGENTS.md", "uv.lock", "data/jarvis.db", ".env", "x.pem",
        "jarvis/memory.py", "jarvis/policy_proxy.py", "jarvis/browser.py",
        "jarvis/connectors/claude_code.py", "jarvis/connectors/__init__.py",
        "jarvis/__init__.py", "jarvis/contracts.py", "tests/__init__.py",
        "tests/helpers.py", "tests/fixtures/x.json", "tests/test_execution_journal.py",
        "tests/test_test_parallel_infra.py", "tests/test_clock_audit.py",
        "json.py", "sitecustomize.py", "usercustomize.py", "contracts/__init__.py",
        "jarvis/cli.py", "jarvis/__main__.py", "jarvis/ui/backend.py", "jarvis/ui/bridge.py",
        "tests/test_windows_scripts.py", "tests/dev-helper-tests.ps1", "tests/tempfile.py",
        "TESTS/Shutil.py", "tests/sqlite3/__init__.py")

    def test_independent_inventory_is_refused(self):
        for target in self.INVENTORY:
            refused(self, target)

    def test_spelling_variants_of_closure_members(self):
        for target in ("jarvis\\browser.py", "JARVIS/jarvis/browser.py",
                       "Jarvis/Connectors/Claude_Code.PY", "JSON.PY", "SiteCustomize.py",
                       "TESTS/Helpers.py", "ｊｓｏｎ.py", "jarvis/browser/__init__.py",
                       "jarvis/browser.pyc", "jarvis/connectors/claude_code.cp313-win_amd64.pyd",
                       "json.cp313-win_amd64.pyd", "re/__init__.py", "x/sitecustomize.py"):
            refused(self, target)
        refused(self, "browser.py", "jarvis/")
        refused(self, "helpers.py", "tests/")
        refused(self, "fixtures/a.txt", "tests/")
        refused(self, "tempfile.py", "tests/")
        refused(self, "cli.py", "jarvis/")

    def test_slice_target_only_is_refused(self):
        data = example()
        data["slices"][0]["targets"] = ["jarvis/browser.py"]
        got = {r.code for r in validate(Plan.from_dict(data), "projects/demo",
                                        jarvis_repo=True)}
        self.assertIn("self_target", got)

    def test_prefix_and_jarvis_repo_types(self):
        self.assertEqual(set(), codes("README.md", ""))
        refused(self, "router_tr.py", "")  # "" means repo root: a root module shadow
        for prefix in ("jarvis", 1, b"jarvis/"):
            with self.assertRaises(ValueError, msg=prefix):
                codes("memory.py", prefix)
        for flag in (1, None, "yes"):
            got = {r.code for r in validate(plan("jarvis/browser.py"), "projects/demo",
                                            jarvis_repo=flag)}
            self.assertTrue({"unknown_fact", "self_target"} <= got, (flag, got))
        # the hand list stays prefixed-proof too
        for name in work_plan._SELF_FILES:
            refused(self, name.split("/", 1)[1], "jarvis/")

    def test_closure_follows_imports_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree(root, {"jarvis/memory.py": "from jarvis import extra\nimport jarvis.gone\n",
                        "jarvis/extra.py": "from .deep import x\n",
                        "jarvis/deep.py": "", "jarvis/free.py": "",
                        "tests/test_a.py": "import jarvis.deep\n",
                        "tests/test_b.py": "import jarvis.free\n"})
            stems = work_plan._closure(root)
            for stem in ("jarvis/extra", "jarvis/deep", "jarvis/gone", "tests/test_a",
                         "tests/__init__", "tests/fixtures"):
                self.assertIn(stem, stems)
            self.assertNotIn("jarvis/free", stems)
            self.assertNotIn("tests/test_b", stems)
        for broken in ({"jarvis/memory.py": "def (:\n"}, None):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tree(root, broken)
                if broken is None:
                    (root / work_plan._SELF_FILES[0]).unlink()
                self.assertIsNone(work_plan._closure(root))
                with mock.patch.object(work_plan, "_ROOT", root):
                    self.assertIn("self_target", codes("README.md"))
        self.assertEqual(set(), codes("README.md"))


class ShadowModeTest(unittest.TestCase):
    def test_candidate_is_a_branch_never_main_or_the_checkout(self):
        self.assertIs(True, SHADOW_MODE)
        got = shadow_candidate("run7", "s1", jarvis_repo=True)
        self.assertEqual("pm-shadow/run7/s1", got["branch"])
        self.assertEqual("branch", got["applies_to"])
        self.assertIs(False, got["canonical_checkout"])
        self.assertIs(False, got["push"])
        self.assertTrue(got["report_stub"].startswith("STATUS: PASS\nBRANCH: pm-shadow/run7/s1\n"))
        self.assertIn("PUSHED: no", got["report_stub"])

    def test_bad_ids_and_non_jarvis_runs_are_refused(self):
        for run, sid, jr in (("run7", "s1", False), ("run7", "s1", 1), ("run7", "main", True),
                             ("MASTER", "s1", True), ("run7", "HEAD", True),
                             ("run7", "../main", True), ("run7", "a/b", True),
                             ("run7", "-x", True), ("run7", "", True), (None, "s1", True),
                             ("run7", "s" * 65, True), ("run7", "s1\n", True),
                             ("CON", "s1", True), ("run7", "nul", True),
                             ("COM1", "s1", True), ("run7", "lpt9", True)):
            with self.assertRaises(ValueError, msg=(run, sid, jr)):
                shadow_candidate(run, sid, jarvis_repo=jr)

    def test_no_push_path_is_reachable(self):
        names = [n for n in dir(work_plan) if "push" in n.lower()]
        self.assertEqual([], names)
        source = inspect.getsource(work_plan)
        for needle in ("subprocess", "os.system", "git push", "import os", "Popen"):
            self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
