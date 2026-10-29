import unittest

from jarvis.work_plan import Plan, Refusal, scope, validate
from tests.test_work_plan import example


def plan(**changes):
    data = example()
    # PM31FIX F3: a root a.py is a JARVIS root shadow
    data["targets"] = data["slices"][0]["targets"] = ["src/a.py"]
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
                       "CONIN$", "a/conout$.txt"):
            self.assertEqual(["path_escape"], codes(with_targets(target)), target)

    def test_protected_paths(self):
        for target in ("data/jarvis.db", "data", "DATA\\notes.txt",
                       ".env", ".env.local", "src/.ENV", "a/.jarvis/x",
                       "certs/server.pem", "k/site.key", "x.pfx",
                       "store.sqlite3", "db/app.db-wal", ".ssh/id_rsa",
                       ".git/config", ".claude/settings.json", "AGENTS.md",
                       "store.sqlite3-wal", "x.sqlite-shm", "a.sqlite3-journal",
                       ".GIT/x", "DATA/x", ".ssh/authorized_keys", ".SSH/config",
                       "x.db-mj0A1B2C3D", "x.DB-WAL",
                       # Agent/tool/IDE config shared with PM15 write_scope.
                       ".mcp.json", "src/CLAUDE.local.md", ".vscode/", "GEMINI.md",
                       ".cursorrules", "AGENTS.override.md", ".idea/workspace.xml",
                       ".cursor/", ".codex/config.toml", "CLAUDE.md",
                       ".windsurfrules", ".aider.conf.yml", ".devcontainer/x.json",
                       ".gemini/settings.json"):
            for jr in (False, True):
                self.assertEqual(["protected_path"],
                                 codes(with_targets(target), jarvis_repo=jr),
                                 (target, jr))

    def test_self_target_only_in_jarvis_repo(self):
        for target in ("jarvis/policy.py", "jarvis/policy_gate.py",
                       "jarvis/memory.py", "scripts/conveyor.py",
                       "jarvis/approvals.py", "jarvis/redaction.py",
                       "jarvis/credentials.py", "tools/push_main.ps1",
                       "scripts", "Jarvis/Memory.py",
                       "jarvis/contracts.py", "jarvis/memory_filter.py",
                       "jarvis/work_plan.py", "jarvis/trusted_exec.py",
                       "jarvis/owned_process.py", "jarvis/memory_guard.py",
                       "jarvis/memory/__init__.py", "jarvis/work_plan.pyd",
                       "jarvis/settings.cp313-win_amd64.pyd", "jarvis/__init__.py",
                       "jarvis/__pycache__/work_plan.cpython-313.pyc", "jarvis/app"):
            self.assertEqual(["self_target"],
                             codes(with_targets(target), jarvis_repo=True), target)
            self.assertEqual([], codes(with_targets(target)), target)
        self.assertEqual([], codes(with_targets("jarvis/work_classify.py"),
                                   jarvis_repo=True))

    def test_repo_prefix_checks_targets_as_the_checkout_sees_them(self):
        # CDXINT109 F1: under root=<checkout>/jarvis "memory.py" is jarvis/memory.py.
        for prefix, target, code in (("jarvis/", "memory.py", "self_target"),
                                     ("JARVIS/", "Settings.py", "self_target"),
                                     ("scripts/", "conveyor.py", "self_target"),
                                     ("jarvis/memory/", "__init__.py", "self_target"),
                                     ("data/", "x.txt", "protected_path"),
                                     ("data/sub/", "x.txt", "protected_path"),
                                     (".claude/", "settings.json", "protected_path")):
            refusals = validate(with_targets(target), "projects/demo",
                                jarvis_repo=True, repo_prefix=prefix)
            # example()'s own slice target is checked under the prefix too
            self.assertEqual({code}, {r.code for r in refusals}, (prefix, target))
            # the same name under a benign folder passes: the prefix is what refuses it
            # (PM31FIX F3: with no prefix a root *.py is itself a shadow and refused)
            self.assertEqual([], codes(with_targets("pkg/sub/" + target), jarvis_repo=True),
                             (prefix, target))
        self.assertEqual([], validate(with_targets("work_classify.py"), "projects/demo",
                                      jarvis_repo=True, repo_prefix="jarvis/"))

    def test_unknown_action_classes_are_refused(self):
        for name, value in (("connector_classes", "network"),
                            ("connector_classes", "LOCAL"),
                            ("operation_classes", "push"),
                            ("operation_classes", "stage_write ")):
            refusals = validate(plan(**{name: [value]}), "projects/demo", jarvis_repo=False)
            self.assertEqual([Refusal("unknown_class", value)], refusals, (name, value))
        self.assertEqual([], codes(plan(connector_classes=[], operation_classes=[])))

    def test_any_glob_is_refused(self):
        # RULE A: every glob from CDXINT91/93/94 and the old near-misses.
        for target in (
                "[.][.]/x.py", "?/x.py", "data/*", "*", "src/*", "src/*.db",
                "**/x.py", "*/x.py", "jarvis/*.py", "*.py", "jarvis/[pm]*",
                "jar*/memory.py", "j?rvis/memory.py", "[j]arvis/memory.py",
                "src/.en[v].production", "db/customer.d[b]", "src/customer.d?",
                "src/.en?.production", "src/.e*v.production", "src/{a}.py",
                "src/a!.py", "SRC\\.EN[V]", "src/.en[!x]", "keys/id_r?a",
                "src/**/*.py", "src/*.py", "jarvis/polic[y]_engine.py",
                "jarvis/polic?_engine.py", "jarvis/pol*cy_x.py",
                "jarvis/{policy}.py", "jarvis/aut?.py", "nu?", "src/co?.txt",
                "lpt?", "?ux", "conin?", "co? .x", "src/customer.db?", ".git?/x",
                "x.db?", "SRC/Customer.DB?", "x.db-wal?", "src/pkg/mod_?.py",
                "x.sqlite3-journal*", ".git.*/x", ".GIT*/x", "data.*/x",
                "git*/x", "src/env*", "src/x.db*", "nul\u3000*", "con*",
                "src/en1*.loc", "src/gi0*1", "nu?\xa0.txt", "nu?\u3000.txt",
                "con.*", "nul\u3000*.txt", "tests/test_*.py", "src/app_*.py",
                "docs/guide*.md", "src/id0*1", "src/id0*1.pub",
                "src/customer.db*stream", "src/\\*.py", "src/\\[a].py",
                "src/}", "src/]x", "src/%2A.py", "src/%2a", "src/x%3F",
                "src/\uff0a.py", "src/\ufe61", "src/x\uff1f", "src/\uff3ba]",
                "src/*/", "data/?/"):
            for jr in (False, True):
                self.assertEqual(["glob"],
                                 codes(with_targets(target), jarvis_repo=jr),
                                 (target, jr))

    def test_literal_windows_aliases_refused(self):
        # CDXINT93: trailing dot/space, ADS, 8.3, DOS wildcards, device padding.
        for jr in (False, True):
            for target in ("NUL .txt", "a/con  .py", "a/b\t", "x.db.", "x.db ",
                           "a/b.db:stream", "GIT~1/x", "NUL\xa0.txt",
                           'src/x"db', "src/.en<", "src/.gi>", "a|b",
                           "src/%2e%2e/x", "src/a%5cb", "\uff44\uff41\uff54\uff41/x",
                           "\uff0e\uff0e/x", "src/a\xa0b", "src/../data/",
                           " data/x", "src/ x.py", "data\u200b/x", ".git\u200c/x",
                           "src/a\x85b"):
                self.assertEqual(["path_escape"],
                                 codes(with_targets(target), jarvis_repo=jr),
                                 (target, jr))

    def test_folder_targets_are_subtree_scopes(self):
        targets = ["src/", "src\\", "src\\pkg/", "src/app.py"]
        self.assertEqual({"src/": "subtree", "src\\": "subtree",
                          "src\\pkg/": "subtree", "src/app.py": "file",
                          "src/a.py": "file"},
                         scope(with_targets(*targets), "projects/demo",
                               jarvis_repo=False))
        self.assertIsNone(scope(with_targets("src/", "data/"), "projects/demo",
                                jarvis_repo=False))
        for target in ("/", "\\", ".", "./", ".\\"):
            self.assertEqual(["repo_root"], codes(with_targets(target)), target)
        for target in ("src//", "src\\\\", "../", "src/./"):
            self.assertEqual(["path_escape"], codes(with_targets(target)), target)
        for target in (".git/", "data/", "DATA\\", "a/.jarvis/", "certs.pem/",
                       ".claude/", ".ssh/"):
            self.assertEqual(["protected_path"], codes(with_targets(target)),
                             target)
        # In the JARVIS repo every subtree is refused: its files are unseen.
        for target in ("jarvis/", "JARVIS\\", "scripts/", "src/policy/",
                       "jarvis/auth/", "tests/", "jarvis/ui/", "src/",
                       "jarvis/memory/", "jarvis/__pycache__/"):
            self.assertEqual(["self_target"],
                             codes(with_targets(target), jarvis_repo=True), target)
            self.assertEqual([], codes(with_targets(target)), target)

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
        self.assertEqual(["too_many_children"], codes(Plan.from_dict(data)))
        data["slices"] = data["slices"][:5]
        data["child_count"] = 5
        self.assertEqual([], codes(Plan.from_dict(data)))

    def test_near_misses_pass(self):
        self.assertEqual([], codes(plan(child_count=5)))
        for target in ("src/metadata/x.py", "mydata/file.txt", "data_tools.py",
                       "src/data/x.py", "environment.py", "src\\pkg\\a.py",
                       "docs/keys.md", "a.dbx", "100%.txt",
                       "monkey.py", "store.sqlite3.bak.txt"):
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
