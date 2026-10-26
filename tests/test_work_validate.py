import unittest

from jarvis.work_plan import Plan, Refusal, validate
from tests.test_work_plan import example


def plan(**changes):
    data = example()
    data.update(changes)
    return Plan.from_dict(data)


def codes(value, root="projects/demo", jarvis_repo=False):
    return [r.code for r in validate(value, root, jarvis_repo=jarvis_repo)]


def with_targets(*targets):
    return plan(targets=list(targets))


class ValidateTests(unittest.TestCase):
    def test_example_plan_is_ok(self):
        self.assertEqual([], validate(plan(), "projects/demo", jarvis_repo=False))
        self.assertEqual([], codes(plan(), root="projects\\demo\\"))

    def test_unknown_plan_and_facts(self):
        self.assertEqual(["unknown_plan"], codes(example()))
        self.assertEqual(["unknown_fact"], codes(plan(), jarvis_repo=None))
        with self.assertRaises(TypeError):
            validate(plan(), "projects/demo")

    def test_root_mismatch(self):
        for root in ("projects/other", "", None, "projects/demo/.."):
            self.assertEqual(["root_mismatch"], codes(plan(), root=root), root)

    def test_protected_project_root(self):
        for root in ("C:/Users/x/.jarvis", "C:\\JARVIS\\data", "repo/.env.d"):
            self.assertEqual(["protected_path"],
                             codes(plan(project_root=root), root=root), root)

    def test_path_escape_posix_and_windows_forms(self):
        for target in ("../x.py", "a/../../x.py", "..\\x.py", "/etc/passwd",
                       "C:\\x.py", "c:x.py", "\\\\server\\share\\x.py",
                       "//server/share/x.py", "~/x.py", "~\\x.py", "a/./b.py",
                       "a//b.py", "a/b.py:stream", "PROGRA~1/x.py", "a\x00b",
                       "a/b\n", "x. /y.py", "x../y.py", "nul", "a/COM1.txt",
                       "[.][.]/x.py", "?/x.py", "CONIN$", "a/conout$.txt"):
            self.assertEqual(["path_escape"], codes(with_targets(target)), target)

    def test_protected_paths(self):
        for target in ("data/jarvis.db", "data", "DATA\\notes.txt", "data/*",
                       ".env", ".env.local", "src/.ENV", "a/.jarvis/x",
                       "certs/server.pem", "k/site.key", "x.pfx",
                       "store.sqlite3", "db/app.db-wal", ".ssh/id_rsa",
                       ".git/config", ".claude/settings.json", "AGENTS.md",
                       "*", "src/*", "src/*.db", "**/x.py", "*/x.py"):
            self.assertEqual(["protected_path"], codes(with_targets(target)),
                             target)

    def test_self_target_only_in_jarvis_repo(self):
        for target in ("jarvis/policy.py", "jarvis/policy_gate.py",
                       "jarvis/memory.py", "scripts/conveyor.py",
                       "jarvis/approvals.py", "jarvis/redaction.py",
                       "jarvis/credentials.py", "tools/push_main.ps1",
                       "jarvis/*.py", "scripts", "*.py", "jarvis/[pm]*",
                       "jar*/memory.py", "j?rvis/memory.py", "[j]arvis/memory.py",
                       "jarvis/contracts.py", "jarvis/memory_filter.py"):
            self.assertEqual(["self_target"],
                             codes(with_targets(target), jarvis_repo=True), target)
            if "*" not in target:
                self.assertEqual([], codes(with_targets(target)), target)
        self.assertEqual([], codes(with_targets("jarvis/work_plan.py"),
                                   jarvis_repo=True))

    def test_slice_targets_are_checked(self):
        data = example()
        data["slices"][0]["targets"] = ["../outside.py"]
        self.assertEqual(["path_escape"], codes(Plan.from_dict(data)))

    def test_checks_outside_allowlist(self):
        for argv in (["sh", "-c", "python -m unittest"], ["cmd", "/c", "dir"],
                     ["powershell", "-Command", "x"], ["python", "x.py"],
                     ["python", "-m", "pip", "install", "x"],
                     ["python", "-m", "unittest", "a;rm"],
                     ["python", "-m", "unittest", "-v"],
                     ["python", "-m", "unittest", "../x"],
                     ["python", "-m", "unittest", "a && b"],
                     ["Python", "-m", "unittest"], ["python", "-m"],
                     ["python", "-m", "unittest", "scripts.push_main"],
                     ["python", "-m", "unittest", "data.x"]):
            data = example()
            data["checks"][0]["argv"] = argv
            self.assertEqual(["check_not_allowlisted"],
                             codes(Plan.from_dict(data)), argv)

    def test_unknown_and_duplicate_check_ids(self):
        data = example()
        data["slices"][0]["check_ids"] = ["missing"]
        self.assertEqual(["unknown_check"], codes(Plan.from_dict(data)))
        data = example()
        data["checks"].append(dict(data["checks"][0]))
        self.assertEqual(["unknown_check", "unknown_check"],
                         codes(Plan.from_dict(data)))

    def test_more_than_five_children(self):
        self.assertEqual(["too_many_children"], codes(plan(child_count=6)))
        data = example()
        data["slices"] = [dict(data["slices"][0], slice_id=f"s{i}")
                          for i in range(6)]
        data["child_count"] = 1
        try:
            many = Plan.from_dict(data)
        except Exception:
            return  # schema already refuses the mismatch
        self.assertIn("too_many_children", codes(many))

    def test_near_misses_pass(self):
        self.assertEqual([], codes(plan(child_count=5)))
        for target in ("src/metadata/x.py", "mydata/file.txt", "data_tools.py",
                       "src/data/x.py", "environment.py", "src\\pkg\\a.py",
                       "docs/keys.md", "src/*.py", "tests/test_*.py",
                       "a.dbx", "monkey.py"):
            self.assertEqual([], codes(with_targets(target)), target)
        data = example()
        data["checks"][0]["argv"] = ["python", "-m", "unittest",
                                     "tests.test_a", "tests.test_b.Case"]
        self.assertEqual([], codes(Plan.from_dict(data)))

    def test_reasons_accumulate_and_are_typed(self):
        result = validate(plan(child_count=9, targets=["../a", ".env"]),
                          "elsewhere", jarvis_repo=False)
        self.assertTrue(all(type(r) is Refusal for r in result))
        self.assertEqual(["root_mismatch", "path_escape", "protected_path",
                          "too_many_children"], [r.code for r in result])


if __name__ == "__main__":
    unittest.main()
