import unittest

from jarvis import write_scope
from jarvis.work_plan import AGENT_CONFIG_NAMES, Refusal
from jarvis.write_scope import MAX_FILE_BYTES, check

TARGETS = {"src/": "subtree", "README.md": "file", "assets/": "subtree"}


def change(path, status="M", old_path=None, old_mode="100644",
           new_mode="100644", size=10, binary=False, reparse=False):
    if status == "A":
        old_mode = "000000"
    if status == "D":
        new_mode = "000000"
    return {"status": status, "path": path, "old_path": old_path,
            "old_mode": old_mode, "new_mode": new_mode, "size": size,
            "binary": binary, "reparse": reparse}


def codes(changes, targets=TARGETS, jarvis_repo=False, **kw):
    return [r.code for r in check(changes, targets, jarvis_repo=jarvis_repo, **kw)]


class WriteScopeTests(unittest.TestCase):
    def assertRefused(self, code, paths, **kw):
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(codes([change(path, **kw)]), [code])

    def test_accepts_paths_inside_granted_targets(self):
        changes = [change("src/a.py"), change("src/deep/b.py", status="A"),
                   change("README.md", status="D"), change("SRC/C.PY"),
                   change("src/r.py", status="R100", old_path="src/q.py"),
                   change("src/run.sh", old_mode="100755", new_mode="100755")]
        self.assertEqual(check(changes, TARGETS, jarvis_repo=False), [])
        self.assertEqual(check([], TARGETS, jarvis_repo=False), [])

    def test_out_of_scope(self):
        self.assertRefused("out_of_scope", ["other.py", "srcx/a.py", "src",
                                            "README.md.bak", "docs/README.md"])
        # A length-changing case fold must not merge distinct names.
        self.assertEqual(codes([change("MASS.py")], {"maß.py": "file"}),
                         ["out_of_scope"])
        self.assertEqual(codes([change("i̇/x.py")], {"İ/": "subtree"}),
                         ["out_of_scope"])
        self.assertEqual(codes([change("MASS.py")], {"mass.py": "file"}), [])

    def test_traversal(self):
        self.assertRefused("traversal", ["src/../secret.py", "src\\..\\x",
                                         "../outside.py"])

    def test_absolute_drive_and_unc_paths(self):
        self.assertRefused("absolute_path", ["/etc/passwd", "C:/x/src/a.py",
                                             "c:src/a.py", "\\\\srv\\share\\a",
                                             "//srv/share/a", "\\src\\a.py"])

    def test_windows_aliases_ads_and_devices(self):
        self.assertRefused("path_escape", ["src/a.py:stream", "src/LONGNA~1.PY",
                                           "src/a.py.", "src/a.py ", "src/nul",
                                           "src/COM1.txt", "src//a.py",
                                           "src/./a.py", "src/ａ.py"])

    def test_glob_and_unparseable_paths(self):
        self.assertRefused("glob", ["src/*.py", "src/a?.py"])
        self.assertRefused("unparseable", ["src/dir/", ""])
        self.assertEqual(codes([change(None)]), ["unparseable"])

    def test_symlink(self):
        self.assertRefused("symlink", ["src/link"], status="A", new_mode="120000")
        self.assertRefused("symlink", ["src/link"], status="T", new_mode="120000")
        self.assertRefused("symlink", ["src/link"], status="D", old_mode="120000")

    def test_junction_or_reparse_point(self):
        self.assertRefused("reparse_point", ["src/a.py"], reparse=True)

    def test_submodule_gitlink(self):
        self.assertRefused("submodule", ["src/vendor"], status="A",
                           new_mode="160000")
        self.assertRefused("submodule", ["src/vendor"], old_mode="160000",
                           new_mode="160000")

    def test_git_dir(self):
        self.assertRefused("git_dir", ["src/.git/config", "src/.GIT/hooks/x",
                                       ".git/HEAD"])

    def test_config_files(self):
        self.assertRefused("config_file", [
            "src/.gitattributes", "src/.gitmodules", "src/.github/workflows/c.yml",
            "src/.husky/pre-commit", "src/.githooks/x", "src/.claude/settings.json",
            "src/.codex/config.toml", "src/AGENTS.md", "src/CLAUDE.md",
            "src/.gitlab-ci.yml", "src/.pre-commit-config.yaml",
            "src/.vscode/tasks.json", "src/sitecustomize.py", "src/evil.pth",
            "src/Jenkinsfile", "src/.npmrc", "src/.mcp.json", "src/CLAUDE.local.md",
            "src/GEMINI.md", "src/AGENTS.override.md", "src/.cursorrules",
            "src/.windsurfrules", "src/.aider.conf.yml", "src/.gitea/workflows/x.yml",
            "src/.forgejo/workflows/x.yml"])

    def test_agent_config_names_come_from_work_plan(self):
        self.assertTrue(set(AGENT_CONFIG_NAMES) <= set(write_scope._CONFIG_NAMES))
        self.assertIs(write_scope.AGENT_CONFIG_NAMES, AGENT_CONFIG_NAMES)

    BUILD = ["src/conftest.py", "src/setup.py", "src/setup.cfg",
             "src/pyproject.toml", "src/pytest.ini", "src/tox.ini",
             "src/noxfile.py", "src/Makefile", "src/makefile", "src/GNUmakefile",
             "src/package.json", "src/package-lock.json", "src/requirements.txt",
             "src/requirements-dev.txt", "src/requirements/dev.txt",
             "src/ruff.toml", "src/mypy.ini", "src/.coveragerc", "src/uv.lock",
             "src/jest.config.js", "src/__pycache__/a.cpython-313.pyc",
             "src/a.pyc", "src/ext.pyd", "src/rules.mk", "src/constraints.txt",
             "src/vite.config.ts", "src/tsconfig.json", "src/justfile",
             "src/CMakeLists.txt", "src/MANIFEST.in"]

    def test_build_config_needs_a_literal_file_target(self):
        self.assertRefused("build_config", self.BUILD)
        for path in self.BUILD:
            with self.subTest(path=path):
                self.assertEqual(codes([change(path)], dict(TARGETS, **{path: "file"})),
                                 [])

    def test_build_config_case_nesting_and_status(self):
        self.assertRefused("build_config", ["SRC/ConfTest.PY", "src/a/b/conftest.py",
                                            "src/PyProject.Toml", "src/x/setup.cfg"])
        for status in ("A", "D", "T"):
            self.assertRefused("build_config", ["src/conftest.py"], status=status)
        # The literal grant matches case-insensitively, like every target.
        self.assertEqual(codes([change("SRC/CONFTEST.PY")],
                               {"src/conftest.py": "file"}), [])
        # A literal grant covers only that path, not a nested look-alike.
        self.assertEqual(codes([change("src/a/conftest.py")],
                               {"src/": "subtree", "src/conftest.py": "file"}),
                         ["build_config"])

    def test_build_config_rename_or_copy_either_side(self):
        for status in ("R100", "C075"):
            with self.subTest(status=status):
                self.assertEqual(codes([change("src/conftest.py", status=status,
                                               old_path="src/a.py")]),
                                 ["build_config"])
                self.assertEqual(codes([change("src/a.py", status=status,
                                               old_path="src/conftest.py")]),
                                 ["build_config"])
                both = dict(TARGETS, **{"src/conftest.py": "file"})
                self.assertEqual(codes([change("src/a/conftest.py", status=status,
                                               old_path="src/conftest.py")], both),
                                 ["build_config"])
                self.assertEqual(codes([change("src/conftest.py", status=status,
                                               old_path="src/a.py")], both), [])

    def test_top_level_stdlib_shadow(self):
        self.assertEqual(codes([change("unittest/__init__.py")],
                               {"unittest/": "subtree"}), ["build_config"])
        self.assertEqual(codes([change("json.py")], {"json.py": "file"}), [])
        self.assertEqual(codes([change("Json.py")], {"json.py": "file"}), [])
        self.assertEqual(codes([change("json.py")], {"src/": "subtree"}),
                         ["build_config"])
        self.assertEqual(codes([change("tests/test_a.py")], {"tests/": "subtree"}), [])
        self.assertEqual(codes([change("src/json.py")]), [])

    def test_protected_paths(self):
        self.assertRefused("protected_path", ["src/.env", "src/.env.local",
                                              "src/server.key", "src/id_rsa",
                                              "src/app.db", "src/app.db-wal",
                                              "src/.ssh/known_hosts"])

    def test_self_target_in_jarvis_repo(self):
        targets = {"jarvis/x.py": "file", "jarvis/policy.py": "file"}
        self.assertEqual(codes([change("jarvis/x.py")], targets, jarvis_repo=True),
                         ["invalid_target"])
        self.assertEqual(codes([change("jarvis/x.py")], targets), [])
        self.assertEqual(codes([change("jarvis/x.py")], {"jarvis/x.py": "file"},
                               jarvis_repo=True), [])
        self.assertEqual(codes([change("jarvis/policy.py")], {"jarvis/x.py": "file"},
                               jarvis_repo=True), ["self_target"])

    def test_size_cap(self):
        self.assertEqual(codes([change("src/a.py", size=MAX_FILE_BYTES)]), [])
        self.assertRefused("size_cap", ["src/a.py"], size=MAX_FILE_BYTES + 1)
        self.assertEqual(codes([change("src/a.py", size=11)], max_bytes=10),
                         ["size_cap"])
        self.assertEqual(codes([change("src/a.py", status="D", size=1 << 30)]), [])

    def test_binary_unless_target_allows(self):
        self.assertRefused("binary", ["src/logo.png"], binary=True)
        self.assertEqual(codes([change("assets/logo.png", binary=True)],
                               binary_targets=("assets/",)), [])
        self.assertEqual(codes([change("src/logo.png", binary=True)],
                               binary_targets=("assets/",)), ["binary"])
        self.assertEqual(codes([change("src/a.py")], binary_targets=("other/",)),
                         ["invalid_target"])

    def test_rename_or_copy_with_out_of_scope_side(self):
        for status in ("R100", "R", "C075"):
            with self.subTest(status=status):
                self.assertEqual(codes([change("src/a.py", status=status,
                                               old_path="lib/a.py")]),
                                 ["out_of_scope"])
                self.assertEqual(codes([change("lib/a.py", status=status,
                                               old_path="src/a.py")]),
                                 ["out_of_scope"])
                self.assertEqual(codes([change("src/a.py", status=status,
                                               old_path="src/.env")]),
                                 ["protected_path"])

    def test_executable_mode(self):
        self.assertRefused("executable_mode", ["src/run.sh"], new_mode="100755")
        self.assertRefused("executable_mode", ["src/run.sh"], status="A",
                           new_mode="100755")
        self.assertRefused("executable_mode", ["src/run.sh"], status="R100",
                           old_path="src/old.sh", new_mode="100755")

    def test_unknown_status_and_malformed_entries_fail_closed(self):
        for status in ("C000", "R0", "R00", "R099", "R100", "C75"):
            with self.subTest(status=status):
                self.assertEqual(codes([change("src/a.py", status=status,
                                               old_path="src/b.py")]), [])
        for status in ("U", "X", "B", "?", "R1000", "m", "", None, "R101",
                       "C999", "R200", "R-1", "R1.0", "R 1"):
            with self.subTest(status=status):
                self.assertEqual(codes([change("src/a.py", status=status)]),
                                 ["unknown_status"])
        good = change("src/a.py")
        bad = [{k: v for k, v in good.items() if k != "size"},
               dict(good, extra=1), dict(good, size=-1), dict(good, size=True),
               dict(good, size="10"), dict(good, binary=0), dict(good, reparse=None),
               dict(good, old_mode="100664"), dict(good, new_mode=100644),
               dict(good, old_path="src/b.py"), change("src/a.py", status="R100"),
               dict(change("src/a.py", status="A"), old_mode="100644"),
               dict(good, new_mode="000000"), "src/a.py", None]
        for entry in bad:
            with self.subTest(entry=entry):
                self.assertEqual(codes([entry]), ["unparseable"])
        deep = []
        for _ in range(100000):
            deep = [deep]
        for entry in (deep, dict(good, size=deep), dict(good, path=deep)):
            self.assertEqual(codes([entry]), ["unparseable"])

    def test_bad_arguments_and_targets_fail_closed(self):
        ok = [change("src/a.py")]
        self.assertEqual(check(ok, TARGETS, jarvis_repo=None),
                         [Refusal("unknown_fact", "jarvis_repo")])
        self.assertEqual(codes(tuple(ok)), ["unparseable"])
        self.assertEqual(codes(ok, targets=[("src/", "subtree")]), ["unparseable"])
        self.assertEqual(codes(ok, max_bytes=True), ["unparseable"])
        self.assertEqual(codes(ok, max_bytes=MAX_FILE_BYTES + 1), ["unparseable"])
        self.assertEqual(codes(ok, max_bytes=-1), ["unparseable"])
        self.assertEqual(codes(ok, max_bytes=MAX_FILE_BYTES), [])
        self.assertEqual(codes(ok, max_bytes=0), ["size_cap"])
        self.assertEqual(codes(ok, binary_targets="src/"), ["unparseable"])
        for targets in ({"src": "subtree"}, {"src/": "file"}, {"src/": "dir"},
                        {"../src/": "subtree"}, {"data/": "subtree"},
                        {"src/*.py": "file"}, {"": "file"}, {1: "file"}):
            with self.subTest(targets=targets):
                self.assertEqual(codes(ok, targets=targets), ["invalid_target"])
        self.assertEqual(codes(ok, targets={"src/": "subtree"}, jarvis_repo=True),
                         ["invalid_target"])

    def test_every_refused_change_is_reported(self):
        self.assertEqual(codes([change("src/a.py"), change("x.py"),
                                change("src/.git/x")]),
                         ["out_of_scope", "git_dir"])


if __name__ == "__main__":
    unittest.main()
