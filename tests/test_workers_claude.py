"""PM20 Claude writer worker: argv allowlist, stage cwd, env scrub, lifecycle;
PM20B per-run settings (deny rules) and the pre-spawn stage re-check.

No real process is ever started: every test injects FakeRunner.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path

from jarvis.workers.base import AgentProfile, Worker
from jarvis.workers.claude import (
    DENIED_TOOLS,
    EMPTY_MCP_CONFIG,
    RUN_DIR_PREFIX,
    ClaudeWriterWorker,
    WorkerRefused,
    _fold,
    check_argv,
    rule_path,
    scrubbed_env,
    stage_links,
    worker_settings,
)

COMMAND = str(Path(tempfile.gettempdir()).resolve() / "claude-cli-not-run.exe")
SETTINGS = "C:\\run\\settings.json"


class FakeRunner:
    def __init__(self, stdout: str = "", exit_code: int | None = 0) -> None:
        self.started: list[tuple] = []
        self.kills = 0
        self.exit_code = exit_code
        self.stdout = stdout
        self.before = None

    def start(self, argv, cwd, env, stdin, verify_cwd):
        if self.before is not None:
            self.before(argv)
        verify_cwd()
        self.started.append((list(argv), cwd, dict(env), stdin))
        return len(self.started)

    def poll(self, handle):
        return self.exit_code

    def kill(self, handle):
        self.kills += 1
        self.exit_code = 1

    def output(self, handle):
        return self.stdout


def profile(**overrides) -> AgentProfile:
    values = dict(
        name="writer",
        tools=frozenset({"Edit", "Write"}),
        paths=frozenset({"jarvis/x.py"}),
        network=False,
        max_turns=12,
        budget_sessions=2,
    )
    values.update(overrides)
    return AgentProfile(**values)


def payload(**fields) -> str:
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    base.update(fields)
    return "log line\n" + json.dumps(base)


def settings_path(argv: list[str]) -> Path:
    return Path(argv[argv.index("--settings") + 1])


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pm20-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.runner = FakeRunner(stdout=payload())

    def worker(self, stage=None, *, prof=None, environ=None, **kwargs) -> ClaudeWriterWorker:
        kwargs.setdefault("max_budget_usd", 0.5)
        kwargs.setdefault("home", self.root / "home")
        return ClaudeWriterWorker(
            prof or profile(),
            self.stage if stage is None else stage,
            runner=self.runner,
            command=COMMAND,
            environ=environ if environ is not None else {"PATH": ""},
            **kwargs,
        )


class ArgvTests(WorkerTestCase):
    def test_golden_argv(self) -> None:
        self.assertEqual(
            self.worker().build_argv(SETTINGS),
            [
                COMMAND, "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits", "--safe-mode",
                "--settings", SETTINGS,
                "--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG,
                "--tools", "Edit,Write",
                "--disallowedTools", "Bash,Glob,Grep,NotebookEdit,Task,WebFetch,WebSearch",
                "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
                "--max-turns", "12", "--max-budget-usd", "0.5000",
            ],
        )  # fmt: skip

        self.assertEqual(json.loads(EMPTY_MCP_CONFIG), {"mcpServers": {}})

    def test_golden_argv_with_model_and_effort(self) -> None:
        argv = self.worker(model="claude-opus-5-5", effort="high").build_argv(SETTINGS)
        self.assertEqual(argv[-4:], ["--model", "claude-opus-5-5", "--effort", "high"])
        check_argv(argv, COMMAND, SETTINGS)

    def test_turns_and_budget_come_from_profile_and_settings(self) -> None:
        argv = self.worker(prof=profile(max_turns=7), max_budget_usd=2).build_argv(SETTINGS)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.0000")
        for denied in ("Bash", "Glob", "Grep", "WebFetch", "WebSearch"):
            self.assertNotIn(denied, argv[argv.index("--tools") + 1].split(","))
            self.assertIn(denied, DENIED_TOOLS)

    def test_banned_and_malformed_flags_are_refused(self) -> None:
        good = self.worker().build_argv(SETTINGS)
        servers = json.dumps({"mcpServers": {"x": {"command": "evil"}}})
        at = good.index("--settings")
        cases = {
            "skip_permissions": good + ["--dangerously-skip-permissions"],
            "add_dir": good + ["--add-dir", "C:\\"],
            "settings_inline": _replace(good, "--settings", "{}"),
            "settings_other_file": _replace(good, "--settings", "C:\\run\\other.json"),
            "settings_twice": good + ["--settings", SETTINGS],
            "missing_settings": good[:at] + good[at + 2 :],
            "allowed_tools": good + ["--allowedTools", "Bash"],
            "system_prompt": good + ["--append-system-prompt", "x"],
            "resume": good + ["--resume", "abc"],
            "attached_form": good + ["--add-dir=C:\\"],
            "repeated": good + ["--no-chrome"],
            "positional": good + ["do things"],
            "mcp_servers": _replace(good, "--mcp-config", servers),
            "bypass_mode": _replace(good, "--permission-mode", "bypassPermissions"),
            "default_mode": _replace(good, "--permission-mode", "dontAsk"),
            "bash_tool": _replace(good, "--tools", "Edit,Bash"),
            "web_tool": _replace(good, "--tools", "Edit,WebFetch"),
            "glob_tool": _replace(good, "--tools", "Edit,Glob"),
            "grep_tool": _replace(good, "--tools", "Edit,Grep"),
            "dup_tool": _replace(good, "--tools", "Edit,Edit"),
            "empty_tools": _replace(good, "--tools", ""),
            "weak_deny": _replace(good, "--disallowedTools", "Bash"),
            "deny_without_glob": _replace(good, "--disallowedTools", "Bash,Grep,NotebookEdit,Task,WebFetch,WebSearch"),
            "deny_with_read": _replace(
                good, "--disallowedTools", "Bash,Glob,Grep,NotebookEdit,Read,Task,WebFetch,WebSearch"
            ),
            "zero_turns": _replace(good, "--max-turns", "0"),
            "zero_budget": _replace(good, "--max-budget-usd", "0.0000"),
            "flag_model": good + ["--model", "--add-dir"],
            "value_missing": good + ["--model"],
            "missing_strict": [a for a in good if a != "--strict-mcp-config"],
            "missing_safe_mode": [a for a in good if a != "--safe-mode"],
            "missing_turns": good[: good.index("--max-turns")] + good[good.index("--max-turns") + 2 :],
            "wrong_command": ["claude", *good[1:]],
        }
        for name, argv in cases.items():
            with self.subTest(name), self.assertRaises(WorkerRefused):
                check_argv(argv, COMMAND, SETTINGS)

    def test_read_tool_accepted(self) -> None:
        argv = self.worker(prof=profile(tools=frozenset({"Edit", "Read", "Write"}))).build_argv(SETTINGS)
        self.assertEqual(argv[argv.index("--tools") + 1], "Edit,Read,Write")
        check_argv(argv, COMMAND, SETTINGS)
        self.assertNotIn("Read", DENIED_TOOLS)

    def test_banned_flag_from_build_argv_never_reaches_runner(self) -> None:
        seen: list[str] = []

        class Sneaky(ClaudeWriterWorker):
            def build_argv(self, settings):
                seen.append(settings)
                return super().build_argv(settings) + ["--dangerously-skip-permissions"]

        sneaky = Sneaky(
            profile(), self.stage, runner=self.runner, command=COMMAND, max_budget_usd=0.5, home=self.root / "home"
        )
        with self.assertRaises(WorkerRefused) as caught:
            sneaky.start("task")
        self.assertEqual(caught.exception.code, "flag_not_allowed")
        self.assertEqual(self.runner.started, [])
        self.assertFalse(Path(seen[0]).parent.exists())

    def test_constructor_refusals(self) -> None:
        cases = {
            "relative_command": dict(command="claude"),
            "nan_budget": dict(max_budget_usd=float("nan")),
            "bool_budget": dict(max_budget_usd=True),
            "zero_budget": dict(max_budget_usd=0),
            "bad_model": dict(model="-x"),
            "bad_effort": dict(effort="extreme"),
            "unsafe_home": dict(home="C:\\Users\\a(b"),
            "unc_home": dict(home="\\\\srv\\share\\home"),
        }
        for name, overrides in cases.items():
            args = dict(runner=self.runner, command=COMMAND, max_budget_usd=0.5)
            args.update(overrides)
            with self.subTest(name), self.assertRaises(WorkerRefused):
                ClaudeWriterWorker(profile(), self.stage, **args)


def _replace(argv: list[str], flag: str, value: str) -> list[str]:
    out = list(argv)
    out[out.index(flag) + 1] = value
    return out


def _link_or_skip(case: unittest.TestCase, target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except (ImportError, OSError):
            case.skipTest("no symlink or junction support")


class RefusalTests(WorkerTestCase):
    def assert_refused(self, worker: ClaudeWriterWorker, code: str, task: str = "task") -> None:
        with self.assertRaises(WorkerRefused) as caught:
            worker.start(task)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.runner.started, [])

    def test_profile_refusals(self) -> None:
        cases = {
            "profile_tools": profile(tools=frozenset({"Edit", "Glob"})),
            "profile_network": profile(network=True),
            "budget_sessions_exhausted": profile(budget_sessions=0),
        }
        for code, prof in cases.items():
            with self.subTest(code):
                self.assert_refused(self.worker(prof=prof), code)
        for tools in (frozenset(), {"Edit", "Bash"}, {"Write", "Glob"}, {"Read", "Grep"}):
            with self.subTest(tools=sorted(tools)):
                self.assert_refused(self.worker(prof=profile(tools=frozenset(tools))), "profile_tools")

    def test_read_only_profile_allowed(self) -> None:
        self.worker(prof=profile(tools=frozenset({"Read"}))).start("task")
        self.assertEqual(len(self.runner.started), 1)

    def test_empty_task_refused(self) -> None:
        self.assert_refused(self.worker(), "task_empty", task="  ")

    def test_session_budget_is_consumed_per_start(self) -> None:
        worker = self.worker(prof=profile(budget_sessions=1))
        worker.start("task")
        with self.assertRaises(WorkerRefused) as caught:
            worker.start("again")
        self.assertEqual(caught.exception.code, "budget_sessions_exhausted")
        self.assertEqual(len(self.runner.started), 1)

    def test_stage_refusals(self) -> None:
        cases = {
            "stage_not_absolute": "stage",
            "stage_missing": self.root / "absent",
        }
        for code, stage in cases.items():
            with self.subTest(code):
                self.assert_refused(self.worker(stage), code)
        dotted = str(self.stage) + os.sep + ".." + os.sep + "stage"
        self.assert_refused(self.worker(dotted), "stage_not_absolute")
        afile = self.root / "file.txt"
        afile.write_text("x", encoding="utf-8")
        self.assert_refused(self.worker(afile), "stage_not_directory")

    def test_link_escape_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        _link_or_skip(self, outside, link)
        self.assert_refused(self.worker(link), "stage_escape")

    def test_link_below_stage_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.stage / "pkg").mkdir()
        _link_or_skip(self, outside, self.stage / "pkg" / "out")
        self.assert_refused(self.worker(), "stage_has_link")

    def test_stage_containing_home_refused(self) -> None:
        home = self.stage / "Users" / "x"
        home.mkdir(parents=True)
        mkdtemp = tempfile.mkdtemp
        made: list = []
        tempfile.mkdtemp = lambda *a, **k: made.append(a) or mkdtemp(*a, **k)
        self.addCleanup(setattr, tempfile, "mkdtemp", mkdtemp)
        cases = {
            "equal": (home, home),
            "case_differs": (Path(str(home).upper()), home),
            "parent": (self.stage / "Users", home),
            "grandparent": (self.stage, home),
        }
        for name, (stage, given_home) in cases.items():
            with self.subTest(name):
                self.assert_refused(self.worker(stage, home=given_home), "stage_contains_home")
        self.assertEqual(made, [])

    def test_stage_containing_run_dir_refused(self) -> None:
        (self.stage / "tmp").mkdir()
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        for temp in (self.stage, self.stage / "tmp", Path(str(self.stage / "tmp").upper())):
            tempfile.tempdir = str(temp)
            with self.subTest(str(temp)):
                self.assert_refused(self.worker(), "run_dir_in_stage")
        self.assertEqual(sorted(path.name for path in self.stage.iterdir()), ["tmp"])
        self.assertEqual(list((self.stage / "tmp").iterdir()), [])

    def test_cwd_mismatch_refused(self) -> None:
        other = self.root / "other"
        other.mkdir()

        class Drifting(ClaudeWriterWorker):
            def launch_cwd(self):
                return other

        drifting = Drifting(
            profile(), self.stage, runner=self.runner, command=COMMAND, max_budget_usd=0.5, home=self.root / "home"
        )
        self.assert_refused(drifting, "cwd_mismatch")


class SpawnRecheckTests(WorkerTestCase):
    """The runner calls verify_cwd right before the OS spawn (TOCTOU)."""

    def start_refused(self, worker: ClaudeWriterWorker, change, code: str) -> None:
        settings: list[Path] = []

        def before(argv):
            settings.append(settings_path(argv))
            change()

        self.runner.before = before
        with self.assertRaises(WorkerRefused) as caught:
            worker.start("task")
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.runner.started, [])
        self.assertFalse(settings[0].parent.exists())

    def test_stage_removed_before_spawn(self) -> None:
        self.start_refused(self.worker(), self.stage.rmdir, "stage_missing")

    def test_stage_swapped_for_link_before_spawn(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()

        def swap():
            self.stage.rename(self.root / "stage-old")
            _link_or_skip(self, outside, self.stage)

        self.start_refused(self.worker(), swap, "stage_escape")

    def test_stage_retargeted_before_spawn(self) -> None:
        other = self.root / "other"
        other.mkdir()
        worker = self.worker()

        def retarget():
            worker.stage = other

        self.start_refused(worker, retarget, "stage_changed")

    def test_link_below_stage_appears_before_spawn(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.start_refused(self.worker(), lambda: _link_or_skip(self, outside, self.stage / "in"), "stage_has_link")

    def test_unchanged_stage_spawns(self) -> None:
        self.runner.before = lambda argv: None
        self.worker().start("task")
        self.assertEqual(len(self.runner.started), 1)


class FakeEntry:
    def __init__(self, path: str, *, kind: str = "file", attrs: int = 0, nlink: int = 1) -> None:
        self.path = path
        self.kind = kind
        self.attrs = attrs
        self.nlink = nlink

    def is_symlink(self) -> bool:
        return self.kind == "symlink"

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self.kind == "dir"

    def stat(self, *, follow_symlinks: bool = True):
        mode = stat.S_IFLNK if self.kind == "symlink" else stat.S_IFDIR if self.kind == "dir" else stat.S_IFREG
        return types.SimpleNamespace(st_mode=mode, st_file_attributes=self.attrs)


class LinkScanTests(unittest.TestCase):
    """stage_links over a fake filesystem: {dir: [FakeEntry]}; an Exception value is raised by scandir."""

    def scan(self, tree: dict) -> None:
        def scandir(path):
            if isinstance(tree[path], Exception):
                raise tree[path]
            return contextlib.nullcontext(tree[path])

        entries = {entry.path: entry for listing in tree.values() if isinstance(listing, list) for entry in listing}
        stage_links("root", scandir=scandir, lstat=lambda path: types.SimpleNamespace(st_nlink=entries[path].nlink))

    def nested(self, leaf: FakeEntry) -> dict:
        return {
            "root": [FakeEntry("root/a.py"), FakeEntry("root/pkg", kind="dir")],
            "root/pkg": [FakeEntry("root/pkg/b.py"), FakeEntry("root/pkg/sub", kind="dir")],
            "root/pkg/sub": [FakeEntry("root/pkg/sub/c.py"), leaf],
        }

    def test_clean_nested_tree_passes(self) -> None:
        self.scan(self.nested(FakeEntry("root/pkg/sub/d.py")))

    def test_links_at_depth_refused(self) -> None:
        cases = {
            "symlink": FakeEntry("root/pkg/sub/out", kind="symlink"),
            "junction": FakeEntry("root/pkg/sub/out", kind="dir", attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT),
            "reparse_file": FakeEntry("root/pkg/sub/out.py", attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT),
            "hard_link": FakeEntry("root/pkg/sub/out.py", nlink=2),
        }
        for name, leaf in cases.items():
            with self.subTest(name), self.assertRaises(WorkerRefused) as caught:
                self.scan(self.nested(leaf))
            self.assertEqual(caught.exception.code, "stage_has_link")

    def test_scan_error_refused(self) -> None:
        for where in ("root", "root/pkg/sub"):
            tree = self.nested(FakeEntry("root/pkg/sub/d.py"))
            tree[where] = PermissionError(where)
            with self.subTest(where), self.assertRaises(WorkerRefused) as caught:
                self.scan(tree)
            self.assertEqual(caught.exception.code, "stage_scan_failed")

    def test_junction_is_not_descended(self) -> None:
        tree = {"root": [FakeEntry("root/j", kind="dir", attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT)]}
        with self.assertRaises(WorkerRefused) as caught:
            self.scan(tree)
        self.assertEqual(caught.exception.code, "stage_has_link")

    def test_real_hard_link_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stage").mkdir()
            (root / "outside.txt").write_text("secret", encoding="utf-8")
            stage_links(root / "stage")
            try:
                os.link(root / "outside.txt", root / "stage" / "innocent.txt")
            except OSError as exc:
                self.skipTest(f"hard link unavailable: {exc}")
            with self.assertRaises(WorkerRefused) as caught:
                stage_links(root / "stage")
            self.assertEqual(caught.exception.code, "stage_has_link")


class SettingsTests(WorkerTestCase):
    HOME = "C:\\Users\\x"
    HOME_FORMS = ("//c/Users/x", "//c/[uU][sS][eE][rR][sS]/[xX]")

    def test_rule_path(self) -> None:
        self.assertEqual(rule_path("C:\\Users\\x"), "//c/Users/x")
        self.assertEqual(rule_path("D:\\"), "//d")
        for bad in ("\\\\srv\\share\\x", "rel\\x", "C:rel", "C:\\a(b", "C:\\a*", "C:\\a\\..\\b",
                    "C:\\a[1]", "C:\\a{b}", "C:\\a!b", "C:\\a?"):  # fmt: skip
            with self.subTest(bad), self.assertRaises(WorkerRefused) as caught:
                rule_path(bad)
            self.assertEqual(caught.exception.code, "rule_path_unsafe")

    def test_golden_settings_stage_outside_home(self) -> None:
        settings = worker_settings(self.HOME, "D:\\work\\stage", "C:\\Temp\\run")
        self.assertEqual(settings, worker_settings(self.HOME, "D:\\work\\stage", "C:\\Temp\\run"))
        self.assertEqual(
            {key: value for key, value in settings.items() if key != "permissions"},
            {"disableAllHooks": True, "enableAllProjectMcpServers": False, "enabledMcpjsonServers": []},
        )
        permissions = settings["permissions"]
        self.assertEqual(set(permissions), {"defaultMode", "disableBypassPermissionsMode", "deny"})
        self.assertEqual(permissions["defaultMode"], "acceptEdits")
        self.assertEqual(permissions["disableBypassPermissionsMode"], "disable")
        self.assertNotIn("allow", permissions)
        deny = permissions["deny"]
        self.assertEqual(deny[:2], ["Glob", "Grep"])
        self.assertNotIn("Read", deny)
        self.assertEqual(deny[-2:], [f"Read({home}/**)" for home in self.HOME_FORMS])
        for tool in ("Edit", "Read"):
            for run in ("//c/Temp/run", "//c/[tT][eE][mM][pP]/[rR][uU][nN]"):
                self.assertIn(f"{tool}({run})", deny)
                self.assertIn(f"{tool}({run}/**)", deny)
            for home in self.HOME_FORMS:
                self.assertIn(f"{tool}({home}/**)", deny)
                self.assertIn(f"{tool}({home}/.[sS][sS][hH]/**)", deny)
        self.assertEqual(len(deny), len(set(deny)))
        self.assertTrue(all(rule[:5] in ("Edit(", "Read(") and rule.endswith(")") for rule in deny[2:]))
        for tool in ("Edit", "Read"):
            for name in (".env*", "*.db", "*.sqlite*", "*.key", "*.pem", "id_rsa*", ".git", ".ssh", ".claude",
                         ".codex", ".mcp.json", ".vscode", "claude.md", "CLAUDE.md", "AGENTS.md",
                         "AGENTS.override.md", "GEMINI.md"):  # fmt: skip
                with self.subTest(tool=tool, name=name):
                    self.assertIn(f"{tool}(**/{_fold(name)})", deny)
                    self.assertIn(f"{tool}(**/{_fold(name)}/**)", deny)
            for path in ("**/.git", "**/.[gG][iI][tT]", "**/*.[dD][bB]-*/**", "./data", "./[dD][aA][tT][aA]",
                         "./[dD][aA][tT][aA]/**", "~/.jarvis/**", "~/.[sS][sS][hH]/**",
                         "//c/Users/x/.[cC][lL][aA][uU][dD][eE]/**", "//c/Users/x/.codex"):  # fmt: skip
                self.assertIn(f"{tool}({path})", deny)

    def test_read_deny_set_mirrors_edit_deny_set(self) -> None:
        for stage in ("D:\\work\\stage", "C:\\Users\\x\\repo\\stage", "C:\\Users\\x", "C:\\Users"):
            with self.subTest(stage):
                deny = worker_settings(self.HOME, stage, "C:\\Temp\\run")["permissions"]["deny"]
                edit = [rule[5:-1] for rule in deny if rule.startswith("Edit(")]
                read = [rule[5:-1] for rule in deny if rule.startswith("Read(")]
                self.assertTrue(edit)
                self.assertEqual(read, edit)
                self.assertEqual(len(deny), 2 + 2 * len(edit))

    def test_stage_at_or_above_home_denies_whole_home(self) -> None:
        for stage in ("C:\\Users\\x", "c:\\users\\X", "C:\\Users"):
            with self.subTest(stage):
                deny = worker_settings(self.HOME, stage, "C:\\Temp\\run")["permissions"]["deny"]
                self.assertEqual(deny[-2:], [f"Read({home}/**)" for home in self.HOME_FORMS])
                for home in self.HOME_FORMS:
                    self.assertIn(f"Edit({home}/**)", deny)

    def test_stage_inside_home_keeps_stage_editable(self) -> None:
        for stage in ("C:\\Users\\x\\repo\\stage", "c:\\users\\X\\repo"):
            with self.subTest(stage):
                deny = worker_settings(self.HOME, stage, "C:\\Temp\\run")["permissions"]["deny"]
                for tool in ("Edit", "Read"):
                    for home in self.HOME_FORMS:
                        self.assertNotIn(f"{tool}({home}/**)", deny)
                        self.assertIn(f"{tool}({home}/.claude/**)", deny)
                self.assertEqual(deny[-1], "Read(//c/[tT][eE][mM][pP]/[rR][uU][nN]/**)")

    def test_unsafe_run_dir_refused(self) -> None:
        with self.assertRaises(WorkerRefused) as caught:
            worker_settings(self.HOME, "D:\\stage", "C:\\Temp\\r[1]")
        self.assertEqual(caught.exception.code, "rule_path_unsafe")

    def test_started_run_passes_its_settings_file(self) -> None:
        captured: list = []

        def before(argv):
            path = settings_path(argv)
            captured.append((path, json.loads(path.read_text(encoding="utf-8"))))

        self.runner.before = before
        worker = self.worker()
        run_id = worker.start("task")
        path, content = captured[0]
        self.assertEqual(path.name, "settings.json")
        self.assertTrue(path.parent.name.startswith(RUN_DIR_PREFIX))
        self.assertFalse(path.is_relative_to(self.stage))
        self.assertEqual(content, worker_settings(self.root / "home", self.stage, path.parent))
        for tool in ("Edit", "Read"):
            self.assertIn(f"{tool}({rule_path(self.root / 'home')}/**)", content["permissions"]["deny"])
        self.assertTrue(path.exists())
        worker.collect(run_id)
        self.assertFalse(path.parent.exists())


class EnvTests(unittest.TestCase):
    def test_scrub(self) -> None:
        env = scrubbed_env(
            {
                "PATH": "",
                "SystemRoot": "C:\\Windows",
                "CLAUDE_CODE_OAUTH_TOKEN": "keep",
                "CLAUDE_CONFIG_DIR": "C:\\cfg",
                "ANTHROPIC_API_KEY": "sk-x",
                "ANTHROPIC_BASE_URL": "http://evil",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_EXTRA_API_KEY": "x",
                "OPENAI_API_KEY": "x",
                "GITHUB_TOKEN": "x",
                "https_proxy": "http://user:pw@proxy:8080",
                "HTTP_PROXY": "http://user:pw@proxy:8080",
                "NO_PROXY": "x",
                "claude_code_disable_auto_memory": "0",
                "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "9",
                "claude_code_use_foundry": "1",
                "CLAUDE_ENV_FILE": "C:\\evil.sh",
                "CLAUDE_CODE_SHELL": "C:\\evil.exe",
                "NODE_OPTIONS": "--require C:\\evil.js",
                "enable_claudeai_mcp_servers": "true",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0",
            }
        )
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "keep")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "C:\\cfg")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], "1")
        self.assertEqual(env["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        upper = {key.upper() for key in env}
        for gone in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_EXTRA_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_ENV_FILE", "CLAUDE_CODE_SHELL", "NODE_OPTIONS",
        ):  # fmt: skip
            self.assertNotIn(gone, upper)
        self.assertNotIn("claude_code_disable_auto_memory", env)
        self.assertNotIn("enable_claudeai_mcp_servers", env)
        self.assertNotIn("http://user:pw@proxy:8080", env.values())


class LifecycleTests(WorkerTestCase):
    def test_start_status_collect(self) -> None:
        self.runner.exit_code = None
        worker = self.worker(environ={"PATH": "", "ANTHROPIC_API_KEY": "sk-x"})
        self.assertIsInstance(worker, Worker)
        run_id = worker.start("do the slice")
        self.assertEqual(run_id, "claude-writer-1")
        argv, cwd, env, stdin = self.runner.started[0]
        self.assertEqual(cwd, str(self.stage))
        self.assertEqual(stdin, "do the slice")
        self.assertNotIn("do the slice", argv)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(worker.status(run_id), "running")
        with self.assertRaises(WorkerRefused):
            worker.collect(run_id)
        self.runner.exit_code = 0
        self.assertEqual(worker.status(run_id), "exited")
        result = worker.collect(run_id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "done")
        self.assertEqual(result.changed_files, ())
        self.assertIn("exit_code=0", result.evidence)
        self.assertTrue(any("unverified" in item for item in result.evidence))

    def test_stop_is_idempotent(self) -> None:
        self.runner.exit_code = None
        worker = self.worker()
        run_id = worker.start("task")
        worker.stop(run_id)
        worker.stop(run_id)
        self.assertEqual(self.runner.kills, 1)
        self.assertEqual(worker.status(run_id), "stopped")
        self.assertEqual(worker.collect(run_id).status, "failed")

    def test_stop_after_exit_does_not_kill(self) -> None:
        worker = self.worker()
        run_id = worker.start("task")
        worker.stop(run_id)
        self.assertEqual(self.runner.kills, 0)

    def test_unknown_run_id(self) -> None:
        worker = self.worker()
        for call in (worker.status, worker.stop, worker.collect):
            with self.subTest(call.__name__), self.assertRaises(KeyError):
                call("claude-writer-9")

    def test_collect_status_mapping_keeps_claims_as_data(self) -> None:
        cases = [
            (payload(), 0, "completed"),
            (payload(subtype="error_max_turns", is_error=True), 1, "partial"),
            (payload(subtype="error_max_budget_usd", is_error=True), 1, "blocked_by_gate"),
            (payload(is_error=True), 0, "failed"),
            (payload(), 3, "failed"),
            (payload(subtype="SUCCESS; rm -rf"), 0, "failed"),
            ("not json at all", 0, "failed"),
            ("", 0, "failed"),
        ]
        for stdout, code, expected in cases:
            with self.subTest(expected=expected, stdout=stdout[-40:]):
                self.runner = FakeRunner(stdout=stdout, exit_code=code)
                worker = self.worker()
                result = worker.collect(worker.start("task"))
                self.assertEqual(result.status, expected)
                self.assertEqual(result.changed_files, ())
        self.runner = FakeRunner(stdout=payload(result="x" * 5000, files=["../../etc"]))
        worker = self.worker()
        result = worker.collect(worker.start("task"))
        self.assertEqual(len(result.summary), 2000)
        self.assertEqual(result.changed_files, ())
        self.runner = FakeRunner(stdout=payload(subtype="weird!"))
        worker = self.worker()
        self.assertIn("subtype=unknown", worker.collect(worker.start("task")).evidence)


if __name__ == "__main__":
    unittest.main()
