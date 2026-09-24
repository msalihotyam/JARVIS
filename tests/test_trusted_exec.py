"""DB-001: a workspace executable must never become a trusted tool.

The shape of the attack this file pins. JARVIS is pointed at a repository and
runs native tools - the Claude CLI, the Codex CLI, the browser - on its own
authority, before Core has approved anything. If locating those tools ever
consults the current directory, a file called ``claude.exe`` committed to that
repository IS the Claude CLI, and it runs with the trust the real one had.

Every marker here is inert: it appends one line to a log file and exits, so an
"invocation" is a file appearing on disk and "never invoked" is that file's
absence. Nothing in this module writes to production state - each test builds
its own temporary workspace and is handed its own PATH.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from jarvis import browser_cdp, codex
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.intent import ClaudeIntentAdapter, claude_model_catalog
from jarvis.trusted_exec import (
    TrustedExecutable,
    TrustedExecutableError,
    path_directories,
    resolve_trusted_executable,
    trusted_environment,
)

WINDOWS = os.name == "nt"
#: What a marker writes when it runs. Its presence is the whole assertion.
INVOKED = "invoked.log"


class WorkspaceCase(unittest.TestCase):
    """A disposable workspace that behaves like a hostile checkout."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-db001-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.log = self.workspace / INVOKED

    def marker(self, stem: str) -> Path:
        """A runnable stand-in for ``stem`` that only records that it ran.

        A ``.cmd`` on Windows because it is genuinely executable there
        through ``PATHEXT`` - a hand-written file named ``.exe`` would fail to
        load and could not prove the difference between "refused" and "could
        not run".
        """
        if WINDOWS:
            script = self.workspace / f"{stem}.cmd"
            script.write_text(f'@echo {stem} >> "%~dp0{INVOKED}"\r\n', encoding="ascii")
        else:
            script = self.workspace / stem
            script.write_text(f'#!/bin/sh\necho {stem} >> "$(dirname "$0")/{INVOKED}"\n', encoding="ascii")
            script.chmod(0o755)
        return script

    def planted(self, stem: str) -> Path:
        """A file merely NAMED like the tool - the committed-binary case."""
        candidate = self.workspace / (f"{stem}.exe" if WINDOWS else stem)
        candidate.write_bytes(b"not a real program")
        if not WINDOWS:
            candidate.chmod(0o755)
        return candidate

    def only_workspace(self) -> dict[str, str]:
        """An environment whose entire PATH is the hostile workspace."""
        values = {"PATH": str(self.workspace)}
        if WINDOWS:
            values["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        return values

    def assert_never_invoked(self) -> None:
        self.assertFalse(
            self.log.exists(),
            f"a workspace executable ran: {self.log.read_text(encoding='utf-8', errors='replace') if self.log.exists() else ''}",
        )


class ReproductionTests(WorkspaceCase):
    """The old resolution really would have chosen the workspace copy."""

    def test_the_operating_systems_own_lookup_selects_the_workspace_copy(self) -> None:
        """``shutil.which`` - what every one of these call sites used - picks
        the planted file. This is DB-001 itself, kept as a test so the fix is
        measured against a demonstrated behaviour and not against a belief."""
        planted = self.planted("claude")
        previous = Path.cwd()
        os.chdir(self.workspace)
        try:
            found = shutil.which("claude", path=str(self.workspace))
            # On Windows the current directory is searched even when an
            # explicit path is supplied; both readings are the vulnerability.
            self.assertIsNotNone(found, "the planted file was not even a candidate")
            self.assertEqual(planted.resolve(), Path(str(found)).resolve())
        finally:
            os.chdir(previous)

    def test_the_trusted_resolver_refuses_the_same_workspace_copy(self) -> None:
        self.planted("claude")
        previous = Path.cwd()
        os.chdir(self.workspace)
        try:
            self.assertIsNone(
                resolve_trusted_executable("claude", environ=self.only_workspace())
            )
        finally:
            os.chdir(previous)


class WorkspaceShadowTests(WorkspaceCase):
    """Each trusted tool, given nothing but a hostile workspace, starts nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, self.previous)

    def test_a_workspace_claude_is_never_invoked(self) -> None:
        self.marker("claude")
        self.planted("claude-not-a-program")
        connector = ClaudeCodeConnector(
            allowed_roots=(self.workspace,),
            default_working_directory=self.workspace,
        )
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.workspace)
        self.addCleanup(os.environ.__setitem__, "PATH", previous_path)
        self.assertFalse(connector.availability().available)
        self.assertIsNone(connector._auth_status())
        self.assert_never_invoked()

    def test_a_workspace_codex_is_never_invoked(self) -> None:
        self.marker("codex")
        # Also the committed-binary shape: under the old lookup a ``.exe``
        # was returned verbatim, so it is the case that must not resolve.
        self.planted("codex")
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.workspace)
        self.addCleanup(os.environ.__setitem__, "PATH", previous_path)
        self.assertIsNone(codex.codex_executable())
        # The pre-Core intent adapter is the path that runs before approval.
        adapter = codex.CodexIntentAdapter("system prompt", "gpt-5")
        self.assertFalse(adapter.availability().available)
        self.assert_never_invoked()

    def test_a_workspace_chrome_is_never_invoked(self) -> None:
        """The production browser lookup, given a workspace that holds a
        runnable ``chrome`` and a planted ``chrome.exe`` (and the old
        ``msedge`` pair, which counts for even less now), as the whole of
        PATH and as the current directory: never the workspace copy, and
        nothing runs."""
        for stem in ("chrome", "msedge"):
            self.marker(stem)
            self.planted(stem)
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.workspace)
        self.addCleanup(os.environ.__setitem__, "PATH", previous_path)
        # The real browser may be installed at a system root; what must never
        # happen is the workspace copy being chosen.
        try:
            chosen = Path(browser_cdp.browser_executable().path).resolve()
        except browser_cdp.BrowserCommandFailed:
            chosen = None
        if chosen is not None:
            self.assertFalse(
                chosen.is_relative_to(self.workspace),
                "the browser resolved to the workspace",
            )
            if WINDOWS:
                self.assertTrue(str(chosen).lower().startswith("c:\\program files"), chosen)
        self.assert_never_invoked()


class ChromeTrustTests(WorkspaceCase):
    """V2 M1 Gate 1: the browser comes from a system installation root, only.

    Not PATH, not the current directory, not the workspace, not a per-user
    install under ``%LOCALAPPDATA%``. Every root here is a temporary stand-in
    handed to the lookup as its install root; the production roots are only
    inspected, never resolved against, except in the one test that asks
    whether the real Chrome on this machine resolves.
    """

    def system_root(self) -> Path:
        """A stand-in for ``C:\\Program Files\\...\\chrome.exe``: a real file in
        a directory outside the workspace."""
        installed = Path(tempfile.mkdtemp(prefix="jarvis-chrome-root-")).resolve()
        self.addCleanup(shutil.rmtree, installed, True)
        exe = installed / ("chrome.exe" if WINDOWS else "chrome")
        exe.write_bytes(b"stand-in browser")
        return exe

    def lookup(self, *roots: Path):
        return browser_cdp.trusted_browser_lookup("chrome", install_paths=tuple(str(r) for r in roots))

    def test_the_production_roots_are_system_locations_only(self) -> None:
        self.assertEqual("chrome", browser_cdp.BROWSER_EXECUTABLE_NAME)
        roots = browser_cdp._BROWSER_INSTALL_PATHS
        self.assertTrue(roots)
        for root in roots:
            lowered = root.lower()
            self.assertTrue(lowered.startswith("c:\\program files"), root)
            self.assertTrue(lowered.endswith("\\google\\chrome\\application\\chrome.exe"), root)
            self.assertNotIn("appdata", lowered)
            self.assertNotIn("%", root)
        # The resolver is handed NO PATH: the roots are the whole candidate set.
        self.assertEqual({}, browser_cdp._NO_PATH_SEARCH)

    def test_a_chrome_at_an_allowed_system_root_is_selected_as_a_trusted_identity(self) -> None:
        exe = self.system_root()
        found = self.lookup(exe)
        self.assertIsInstance(found, TrustedExecutable)
        self.assertEqual(str(exe), found.path)
        self.assertEqual(found.path, found.argv0())
        # ``browser_executable`` passes the identity through, not a string.
        via = browser_cdp.browser_executable(path_lookup=lambda name: self.lookup(exe))
        self.assertIsInstance(via, TrustedExecutable)
        self.assertEqual(found.path, via.path)
        # The second root is tried when the first is absent, in order.
        self.assertEqual(found.path, self.lookup(exe.parent / "missing" / exe.name, exe).path)

    def test_a_workspace_copy_is_never_a_candidate(self) -> None:
        self.marker("chrome")
        self.planted("chrome")
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.workspace)
        self.addCleanup(os.environ.__setitem__, "PATH", previous_path)
        # Even as the whole of PATH, with no root to prefer: nothing.
        self.assertIsNone(self.lookup())
        self.assertIsNone(self.lookup(self.workspace / "missing" / "chrome.exe"))
        self.assert_never_invoked()

    def test_the_current_directory_is_refused_even_as_a_named_root(self) -> None:
        planted = self.planted("chrome")
        previous = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)
        # The file exists and is named as a root, and the root is the current
        # directory - the one place the OS would have searched on its own.
        self.assertIsNone(self.lookup(planted))
        self.assert_never_invoked()

    def test_a_user_writable_path_directory_is_never_consulted(self) -> None:
        tools = Path(tempfile.mkdtemp(prefix="jarvis-chrome-path-")).resolve()
        self.addCleanup(shutil.rmtree, tools, True)
        on_path = tools / ("chrome.exe" if WINDOWS else "chrome")
        on_path.write_bytes(b"a chrome on PATH")
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(tools)
        self.addCleanup(os.environ.__setitem__, "PATH", previous_path)
        # The general resolver WOULD find it on PATH; the browser lookup does not.
        self.assertIsNotNone(
            resolve_trusted_executable("chrome", environ={"PATH": str(tools), "PATHEXT": ".EXE"})
        )
        self.assertIsNone(self.lookup(tools / "no-such-root" / on_path.name))
        # The refusal is about the SOURCE, not the file: named as a root it resolves.
        self.assertEqual(str(on_path), self.lookup(on_path).path)

    def test_a_per_user_install_under_localappdata_is_not_accepted(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="jarvis-chrome-local-")).resolve()
        self.addCleanup(shutil.rmtree, home, True)
        per_user = home / "Google" / "Chrome" / "Application"
        per_user.mkdir(parents=True)
        (per_user / ("chrome.exe" if WINDOWS else "chrome")).write_bytes(b"per-user chrome")
        previous = {name: os.environ.get(name) for name in ("LOCALAPPDATA", "PATH")}
        os.environ["LOCALAPPDATA"] = str(home)
        os.environ["PATH"] = str(per_user)
        for name, value in previous.items():
            self.addCleanup(os.environ.__setitem__, name, value if value is not None else "")
        with self.assertRaises(browser_cdp.BrowserCommandFailed) as raised:
            browser_cdp.browser_executable(
                path_lookup=lambda name: self.lookup(home / "no-such-root" / "chrome.exe")
            )
        self.assertIn("Google Chrome", str(raised.exception))
        self.assertNotIn("PATH", str(raised.exception))
        self.assertFalse(any("appdata" in root.lower() for root in browser_cdp._BROWSER_INSTALL_PATHS))

    def test_missing_system_chrome_fails_closed_with_chrome_wording(self) -> None:
        with self.assertRaises(browser_cdp.BrowserCommandFailed) as raised:
            browser_cdp.browser_executable(path_lookup=lambda name: None)
        text = str(raised.exception)
        self.assertIn("Google Chrome", text)
        self.assertIn("Program Files", text)
        for stale in ("PATH", "msedge", "Edge"):
            self.assertNotIn(stale, text)

    def test_a_wrong_candidate_is_refused(self) -> None:
        installed = Path(tempfile.mkdtemp(prefix="jarvis-chrome-wrong-")).resolve()
        self.addCleanup(shutil.rmtree, installed, True)
        (installed / "chrome.exe").mkdir()                       # a directory, not a program
        (installed / "other.exe").write_bytes(b"some other program")
        (installed / "notes.txt").write_bytes(b"not a program at all")
        for candidate in (
            installed / "chrome.exe",
            installed / "other.exe",                             # a real file, but not chrome
            installed / "notes.txt",
            installed / "absent" / "chrome.exe",
        ):
            with self.subTest(candidate=candidate.name):
                self.assertIsNone(self.lookup(candidate))

    def test_the_real_system_chrome_resolves_when_installed(self) -> None:
        """The production roots on this machine. Nothing is launched."""
        found = browser_cdp.trusted_browser_lookup("chrome")
        if found is None:
            self.skipTest("Google Chrome is not installed for this machine")
        self.assertTrue(Path(found.path).is_file())
        self.assertEqual("chrome.exe" if WINDOWS else "chrome", Path(found.path).name.lower())
        if WINDOWS:
            self.assertTrue(found.path.lower().startswith("c:\\program files"), found.path)
        self.assertEqual(found.path, found.argv0())


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ChromeSpawnBoundaryTests(unittest.TestCase):
    """The identity accepted at lookup is re-checked at the spawn, by
    ``attached_browser`` itself, and what reaches ``spawn`` is its answer.

    No browser runs: ``spawn`` is a fake that records what it was handed.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-chrome-spawn-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.profile = str(root / "browser-profile")
        self.exe = root / "install" / ("chrome.exe" if WINDOWS else "chrome")
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"the original")
        self.port = _free_port()
        # ``attached_browser`` starts this port's pre-connect boundary before
        # it reaches the spawn; the boundary is let go with the rest.
        self.addCleanup(browser_cdp.release_owned_browsers, self.port)

    def located(self):
        found = browser_cdp.trusted_browser_lookup("chrome", install_paths=(str(self.exe),))
        self.assertIsNotNone(found)
        return found

    def attach(self, lookup, spawn):
        return browser_cdp.attached_browser(
            profile_dir=self.profile,
            port=self.port,
            executable_lookup=lookup,
            spawn=spawn,
            sleep=lambda seconds: None,
        )

    def test_a_replacement_between_lookup_and_spawn_starts_nothing(self) -> None:
        found = self.located()
        spawned: list = []

        def spawn(argv):
            spawned.append(list(argv))
            raise AssertionError("a replaced browser was spawned")

        self.exe.write_bytes(b"a different program entirely")
        with self.assertRaises(browser_cdp.BrowserCommandFailed) as raised:
            self.attach(lambda: found, spawn)
        self.assertIn("no longer the one that was located", str(raised.exception))
        self.assertEqual([], spawned)
        self.assertIsNone(browser_cdp.owned_browser(self.port))

    def test_a_removal_between_lookup_and_spawn_starts_nothing(self) -> None:
        found = self.located()
        spawned: list = []
        self.exe.unlink()
        with self.assertRaises(browser_cdp.BrowserCommandFailed):
            self.attach(lambda: found, lambda argv: spawned.append(argv))
        self.assertEqual([], spawned)
        self.assertIsNone(browser_cdp.owned_browser(self.port))

    def test_the_spawn_receives_the_revalidated_argv0_and_this_builds_flags(self) -> None:
        class Revalidated:
            """An identity whose spawn-time answer differs from where it was
            located: the spawn must use the answer, never the location."""

            path = "C:/located/chrome.exe"

            def argv0(self, **_kwargs: object) -> str:
                return "C:/revalidated/chrome.exe"

        class Dead:
            """An owned process that exited at once, so the launch is reported
            rather than waited out."""

            pid = 0

            def alive(self) -> bool:
                return False

            def contained(self) -> bool:
                return False

            def close(self) -> None:
                pass

        spawned: list = []

        def spawn(argv):
            spawned.append(list(argv))
            return Dead()

        with self.assertRaises(browser_cdp.BrowserCommandFailed) as raised:
            self.attach(lambda: Revalidated(), spawn)
        self.assertIn("exited before it opened a debugging socket", str(raised.exception))
        self.assertEqual(1, len(spawned))
        argv = spawned[0]
        self.assertEqual("C:/revalidated/chrome.exe", argv[0])
        self.assertEqual(f"--remote-debugging-port={self.port}", argv[1])
        self.assertIn(f"--user-data-dir={self.profile}", argv)
        self.assertTrue(any(a.startswith("--proxy-server=http://127.0.0.1:") for a in argv))
        self.assertIn("--proxy-bypass-list=<-loopback>", argv)
        self.assertEqual("about:blank", argv[-1])

    def test_an_unchanged_real_identity_reaches_the_spawn(self) -> None:
        found = self.located()
        spawned: list = []

        class Dead:
            pid = 0

            def alive(self) -> bool:
                return False

            def contained(self) -> bool:
                return False

            def close(self) -> None:
                pass

        with self.assertRaises(browser_cdp.BrowserCommandFailed):
            self.attach(lambda: found, lambda argv: spawned.append(list(argv)) or Dead())
        self.assertEqual(found.path, spawned[0][0])
        self.assertEqual(str(self.exe), spawned[0][0])


class PathEntryTests(WorkspaceCase):
    """Empty and relative PATH entries are "here", and are dropped as such."""

    def test_an_empty_entry_is_never_a_source(self) -> None:
        self.planted("claude")
        previous = Path.cwd()
        os.chdir(self.workspace)
        try:
            environ = {"PATH": os.pathsep.join(["", "   ", ""]), "PATHEXT": ".EXE"}
            self.assertEqual((), path_directories(environ))
            self.assertIsNone(resolve_trusted_executable("claude", environ=environ))
        finally:
            os.chdir(previous)

    def test_a_relative_entry_is_never_a_source(self) -> None:
        self.planted("claude")
        previous = Path.cwd()
        os.chdir(self.workspace)
        try:
            environ = {"PATH": os.pathsep.join([".", "..", "sub"]), "PATHEXT": ".EXE"}
            self.assertEqual((), path_directories(environ))
            self.assertIsNone(resolve_trusted_executable("claude", environ=environ))
        finally:
            os.chdir(previous)

    def test_a_path_entry_inside_the_workspace_is_dropped(self) -> None:
        nested = self.workspace / "tools"
        nested.mkdir()
        environ = {"PATH": str(nested), "PATHEXT": ".EXE"}
        self.assertEqual(
            (), path_directories(environ, forbidden=(self.workspace,))
        )


class ConfiguredPathTests(WorkspaceCase):
    """An operator's explicit absolute path stays usable - that is the point."""

    def test_an_absolute_configured_executable_resolves(self) -> None:
        installed = Path(tempfile.mkdtemp(prefix="jarvis-db001-install-"))
        self.addCleanup(shutil.rmtree, installed, True)
        tool = installed / ("tool.exe" if WINDOWS else "tool")
        tool.write_bytes(b"stand-in")
        found = resolve_trusted_executable("tool", configured=str(tool))
        self.assertIsNotNone(found)
        self.assertEqual(str(tool.resolve()), found.path)
        self.assertEqual(found.path, found.argv0())

    def test_a_configured_bare_name_is_refused(self) -> None:
        self.assertIsNone(resolve_trusted_executable("claude", configured="claude"))

    def test_a_configured_path_inside_the_workspace_is_refused(self) -> None:
        planted = self.planted("claude")
        self.assertIsNone(
            resolve_trusted_executable(
                "claude", configured=str(planted), workspace=(self.workspace,)
            )
        )


class ReplacementTests(WorkspaceCase):
    """Identity is re-checked at the spawn, not just at discovery."""

    def setUp(self) -> None:
        super().setUp()
        self.installed = Path(tempfile.mkdtemp(prefix="jarvis-db001-install-"))
        self.addCleanup(shutil.rmtree, self.installed, True)
        self.tool = self.installed / ("tool.exe" if WINDOWS else "tool")
        self.tool.write_bytes(b"the original")

    def resolved(self):
        found = resolve_trusted_executable("tool", configured=str(self.tool))
        self.assertIsNotNone(found)
        return found

    def test_replacement_after_resolution_fails_closed(self) -> None:
        found = self.resolved()
        self.assertEqual(found.path, found.argv0())
        self.tool.write_bytes(b"a different program entirely")
        with self.assertRaises(TrustedExecutableError):
            found.argv0()

    def test_removal_after_resolution_fails_closed(self) -> None:
        found = self.resolved()
        self.tool.unlink()
        with self.assertRaises(TrustedExecutableError):
            found.argv0()

    def test_a_tool_that_became_part_of_the_workspace_fails_closed(self) -> None:
        found = self.resolved()
        with self.assertRaises(TrustedExecutableError):
            found.argv0(workspace=(self.installed,))


class SpawnShapeTests(unittest.TestCase):
    """What actually reaches ``Popen``: an absolute path, never a bare name.

    Rule 2A is about ``argv[0]`` at the moment of process creation, so it is
    asserted there - including on the pre-Core intent adapter, which inherits
    the connector's argv and would otherwise be the one path still spawned by
    name (the freeze/approval finding in DB-001 §6).
    """

    def test_the_connector_and_the_intent_adapter_both_spawn_an_absolute_path(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jarvis-db001-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        connector = ClaudeCodeConnector(allowed_roots=(root,), default_working_directory=root)
        if connector._trusted_executable() is None:
            self.skipTest("the Claude CLI is not installed on this machine")
        resolved = connector._argv0()
        self.assertTrue(Path(resolved).is_absolute())
        self.assertTrue(Path(resolved).is_file())
        # The adapter the pre-Core intent path uses inherits the same rule.
        adapter = ClaudeIntentAdapter(
            allowed_roots=(root,), default_working_directory=root
        )
        adapter_argv0 = adapter._argv0()
        self.assertEqual(resolved, adapter_argv0)
        self.assertNotEqual("claude", adapter_argv0)

    def test_the_model_catalog_refuses_a_workspace_cli(self) -> None:
        """The catalog probe runs before Core approves anything at all."""
        workspace = Path(tempfile.mkdtemp(prefix="jarvis-db001-catalog-")).resolve()
        self.addCleanup(shutil.rmtree, workspace, True)
        marker = workspace / ("claude.cmd" if WINDOWS else "claude")
        if WINDOWS:
            marker.write_text(f'@echo x >> "%~dp0{INVOKED}"\r\n', encoding="ascii")
        else:
            marker.write_text(f'#!/bin/sh\necho x >> "$(dirname "$0")/{INVOKED}"\n', encoding="ascii")
            marker.chmod(0o755)
        previous_path, previous_cwd = os.environ.get("PATH", ""), Path.cwd()
        os.environ["PATH"] = str(workspace)
        os.chdir(workspace)
        self.addCleanup(os.chdir, previous_cwd)
        self.addCleanup(os.environ.__setitem__, "PATH", previous_path)
        self.assertEqual([], claude_model_catalog())
        self.assertFalse((workspace / INVOKED).exists(), "the workspace CLI ran")


class EnvironmentContainmentTests(unittest.TestCase):
    """A trusted child gets what it needs, not everything JARVIS holds."""

    SENTINEL = "JARVIS_UNRELATED_SECRET_SENTINEL"

    def environ(self) -> dict[str, str]:
        return {
            "PATH": os.pathsep.join(["", str(Path(tempfile.gettempdir()).resolve())]),
            "SYSTEMROOT": r"C:\Windows",
            "HOME": "/home/operator",
            self.SENTINEL: "not-a-real-secret-placeholder",
            "ANTHROPIC_API_KEY": "not-a-real-secret-placeholder",
            "OPENAI_API_KEY": "not-a-real-secret-placeholder",
            "GH_TOKEN": "not-a-real-secret-placeholder",
            "CLAUDE_CONFIG_DIR": "/config/claude",
        }

    def test_an_unrelated_sentinel_is_not_inherited(self) -> None:
        child = trusted_environment(prefixes=("CLAUDE_",), environ=self.environ())
        self.assertNotIn(self.SENTINEL, child)

    def test_another_tools_credential_is_not_inherited(self) -> None:
        child = trusted_environment(prefixes=("CLAUDE_",), environ=self.environ())
        for foreign in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN"):
            self.assertNotIn(foreign, child)

    def test_the_tools_own_configuration_still_reaches_it(self) -> None:
        child = trusted_environment(prefixes=("CLAUDE_",), environ=self.environ())
        self.assertEqual("/config/claude", child["CLAUDE_CONFIG_DIR"])

    def test_the_child_path_has_no_search_here_entries(self) -> None:
        child = trusted_environment(environ=self.environ())
        self.assertNotIn("", child["PATH"].split(os.pathsep))

    def test_forced_values_win(self) -> None:
        child = trusted_environment(forced={"GH_PROMPT_DISABLED": "1"}, environ=self.environ())
        self.assertEqual("1", child["GH_PROMPT_DISABLED"])

    def test_the_real_builders_do_not_pass_the_sentinel_on(self) -> None:
        """The production environments themselves, not just the helper.

        The sentinel is put in this process's REAL environment and removed
        again, which is what a leak would actually look like: a variable
        JARVIS happens to hold while it starts somebody else's CLI.
        """
        os.environ[self.SENTINEL] = "not-a-real-secret-placeholder"
        self.addCleanup(os.environ.pop, self.SENTINEL, None)
        builders = {
            "claude": ClaudeCodeConnector._sanitized_environment,
            "codex": codex._environment,
        }
        for tool, build in builders.items():
            with self.subTest(tool=tool):
                self.assertNotIn(self.SENTINEL, build())

    def test_the_real_builders_still_carry_the_os_basics(self) -> None:
        """Containment must not break ordinary Windows execution."""
        required = "SYSTEMROOT" if WINDOWS else "HOME"
        for tool, build in (
            ("claude", ClaudeCodeConnector._sanitized_environment),
            ("codex", codex._environment),
        ):
            with self.subTest(tool=tool):
                child = build()
                if required in os.environ:
                    self.assertIn(required, child)
                self.assertIn("PATH", child)


class LegitimateAvailabilityTests(unittest.TestCase):
    """The real installations on this machine still resolve."""

    def test_an_installed_tool_on_the_real_path_still_resolves(self) -> None:
        """Whichever of the trusted tools this machine has, provenance finds
        it. Skipped rather than failed where none is installed: this asserts
        the fix did not break discovery, not that any tool must be present."""
        installed = [
            name
            for name in ("claude", "codex", "gh", "gws")
            if shutil.which(name) is not None
        ]
        if not installed:
            self.skipTest("no trusted CLI is installed on this machine")
        for name in installed:
            with self.subTest(tool=name):
                found = resolve_trusted_executable(name)
                self.assertIsNotNone(found, f"{name} is installed but was not resolved")
                self.assertTrue(Path(found.path).is_absolute())
                self.assertTrue(Path(found.path).is_file())

    def test_a_real_resolution_survives_a_spawn_shaped_revalidation(self) -> None:
        name = next((n for n in ("gh", "claude", "codex", "gws") if shutil.which(n)), None)
        if name is None:
            self.skipTest("no trusted CLI is installed on this machine")
        found = resolve_trusted_executable(name)
        self.assertEqual(found.path, found.argv0())


class HeadlessConsoleTests(unittest.TestCase):
    """Probes the windowed JARVIS.exe runs in the background open no console.

    The execution paths (Claude, Codex, the Claude catalog, Antigravity) are
    asserted where their spawn shape is already pinned; these are the probes
    that had no spawn-shape test of their own.
    """

    def test_the_flag_is_create_no_window_on_windows_and_inert_elsewhere(self) -> None:
        import subprocess
        from jarvis.trusted_exec import NO_CONSOLE_WINDOW
        self.assertEqual(subprocess.CREATE_NO_WINDOW if WINDOWS else 0, NO_CONSOLE_WINDOW)

    def test_the_claude_auth_probe_is_headless(self) -> None:
        from unittest.mock import MagicMock, patch
        from jarvis.trusted_exec import NO_CONSOLE_WINDOW
        root = Path(tempfile.mkdtemp(prefix="jarvis-noconsole-"))
        self.addCleanup(shutil.rmtree, root, True)
        connector = ClaudeCodeConnector(allowed_roots=(root,), default_working_directory=root)
        completed = MagicMock(returncode=0, stdout='{"loggedIn": true}')
        with patch.object(ClaudeCodeConnector, "_argv0", return_value="C:/abs/claude.exe"), \
                patch("jarvis.connectors.claude_code.subprocess.run", return_value=completed) as run:
            connector._auth_status()
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(["C:/abs/claude.exe", "auth", "status", "--json"], argv)
        self.assertEqual(NO_CONSOLE_WINDOW, kwargs["creationflags"])
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs["shell"])
        self.assertIn("timeout", kwargs)
        self.assertIsInstance(kwargs["env"], dict)

    def test_the_codex_app_server_and_model_probe_are_headless(self) -> None:
        import subprocess
        from unittest.mock import MagicMock, patch
        process = MagicMock()
        process.stdout = iter([])
        with patch.object(codex.subprocess, "Popen", return_value=process) as popen:
            codex._app_server("C:/abs/codex.exe", [("account/read", None)], 0.01)
        argv, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(["C:/abs/codex.exe", "app-server"], argv)
        self.assertEqual(codex.NO_CONSOLE_WINDOW, kwargs["creationflags"])
        self.assertEqual((subprocess.PIPE, subprocess.PIPE), (kwargs["stdin"], kwargs["stdout"]))
        self.assertFalse(kwargs["shell"])
        process.kill.assert_called()                                   # still reaped on the timeout
        with patch.object(codex.subprocess, "run", return_value=MagicMock(stdout='{"models": []}')) as run:
            self.assertEqual([], codex._catalog("C:/abs/codex.exe"))
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(["C:/abs/codex.exe", "debug", "models"], argv)
        self.assertEqual(codex.NO_CONSOLE_WINDOW, kwargs["creationflags"])
        self.assertEqual(codex.CODEX_PROBE_TIMEOUT_SECONDS, kwargs["timeout"])
        self.assertFalse(kwargs["shell"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
