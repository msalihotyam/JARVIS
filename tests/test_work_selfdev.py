import inspect
import unittest

import jarvis.work_plan as work_plan
from jarvis.work_plan import (SELF_DENY, SHADOW_MODE, Plan, classify,
                              shadow_candidate, validate)
from tests.test_work_plan import example

REFUSED = {"self_target", "protected_path"}


def plan(*targets):
    data = example()
    data["targets"] = list(targets)
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
                             ("run7", "s" * 65, True), ("run7", "s1\n", True)):
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
