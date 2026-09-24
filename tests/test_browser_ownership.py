"""DB-002B-01: the browser may not outlive the thing that mediates it.

THE GAP THIS PINS. Every part of JARVIS's browser boundary is a JARVIS
object - a socket, a thread, a method set - and Chromium drops interception
when its DevTools client disappears. The standing policy closes its tab on an
orderly exit, but an orderly exit was never the problem. ``TerminateProcess``,
a hard crash, Task Manager: none of them run ``atexit``, a ``finally``, or a
protocol message. The browser was started with an ordinary ``Popen`` whose
handle was thrown away, so it survived, holding a loaded untrusted page with
nothing left judging its network.

HOW THIS IS PROVED. Real Windows processes, and the control case first: a
parent that spawns a child the old way is killed, and the child is shown to
still be running. The same parent using the owned launcher is killed, and the
kernel has already terminated the child and its descendants before anyone
asks. Nothing here touches a browser, a profile or any JARVIS state - the
children are Python processes that sleep, and every one this file starts, it
accounts for.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from jarvis import browser_cdp as cdp_module
from jarvis.browser_cdp import (
    BrowserCommandFailed,
    attached_browser,
    browser_arguments,
    browser_policy_proxy,
    owned_browser,
    release_owned_browsers,
)
from jarvis.owned_process import (
    ProcessOwnershipError,
    launch_owned,
    listening_pid,
    ownership_is_available,
)

#: A browser stand-in: it BINDS THE PORT itself and answers ``/json/version``
#: the way a DevTools endpoint does. It has to be the listener, because that
#: is exactly what ownership of an endpoint is now checked against.
_STAND_IN = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(
            {'webSocketDebuggerUrl': 'ws://127.0.0.1:%d/devtools/browser/x' % port}
        ).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(('127.0.0.1', port), H).serve_forever()
"""

ROOT = str(Path(__file__).resolve().parent.parent)

#: How long the kernel is given to finish terminating a job's contents. A
#: bound on the WAIT, not on the guarantee - in practice it is immediate.
REAPED_TIMEOUT = 20.0
#: How long the control case is watched to establish that a process really
#: did survive its parent, rather than merely not having died yet.
SURVIVED_SECONDS = 3.0

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no branch - this file is about Windows lifetimes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    )
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is a process that is still running, asked of the OS."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(wintypes.HANDLE(handle))


def kill_pid(pid: int) -> None:
    """Best-effort cleanup for a process this file started and must not leak."""
    if not process_alive(pid):
        return
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


#: What the grandchild runs: it starts a child of its OWN, writes both pids
#: where the test can read them, and then does nothing for a long time. Its
#: own child is what proves the job holds a TREE and not one process.
_GRANDCHILD = (
    "import os, subprocess, sys, time, pathlib\n"
    "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()) + ' ' + str(kid.pid))\n"
    "time.sleep(600)\n"
)


def _parent_source(*, owned: bool, marker: str, pids: str) -> str:
    """The JARVIS-like parent: it starts a browser stand-in, then waits."""
    launcher = (
        "from jarvis.owned_process import launch_owned\n"
        "child = launch_owned([sys.executable, '-c', GRANDCHILD, PIDS])\n"
        "pid = child.pid\n"
        if owned
        else (
            "import subprocess\n"
            "child = subprocess.Popen([sys.executable, '-c', GRANDCHILD, PIDS])\n"
            "pid = child.pid\n"
        )
    )
    return (
        "import sys, time, pathlib\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        f"GRANDCHILD = {_GRANDCHILD!r}\n"
        f"PIDS = {pids!r}\n"
        + launcher
        + f"pathlib.Path({marker!r}).write_text(str(pid))\n"
        "time.sleep(600)\n"
    )


@unittest.skipUnless(_IS_WINDOWS, "process lifetime is bound with a Windows job object")
class ProcessLifetimeCase(unittest.TestCase):
    """One JARVIS-like parent, one browser stand-in, one descendant."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.marker = str(Path(self.temp.name) / "child.pid")
        self.pids = str(Path(self.temp.name) / "tree.pids")
        self.started: list[int] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        for pid in self.started:
            kill_pid(pid)

    def start_parent(self, *, owned: bool) -> subprocess.Popen:
        parent = subprocess.Popen(
            [sys.executable, "-c", _parent_source(owned=owned, marker=self.marker, pids=self.pids)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.started.append(parent.pid)
        return parent

    def tree(self) -> tuple[int, int]:
        """The grandchild's pid and its own child's, once both exist."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                text = Path(self.pids).read_text().split()
            except OSError:
                text = []
            if len(text) == 2:
                pids = (int(text[0]), int(text[1]))
                self.started.extend(pids)
                return pids
            time.sleep(0.05)
        self.fail("the browser stand-in never reported its process tree")

    def assert_reaped(self, pid: int, what: str) -> None:
        deadline = time.monotonic() + REAPED_TIMEOUT
        while time.monotonic() < deadline:
            if not process_alive(pid):
                return
            time.sleep(0.05)
        self.fail(f"{what} (pid {pid}) outlived the process that owned it")


class ControlTests(ProcessLifetimeCase):
    """The defect itself, reproduced before anything claims to fix it."""

    def test_an_unowned_child_survives_its_parents_abrupt_death(self) -> None:
        """THE BLOCKER. Popen, handle discarded, parent killed: it lives on."""
        parent = self.start_parent(owned=False)
        child, grandchild = self.tree()
        parent.kill()
        parent.wait(timeout=30)
        # Watched, not glanced at: still there well after the owner is gone.
        time.sleep(SURVIVED_SECONDS)
        self.assertTrue(
            process_alive(child),
            "the control case did not reproduce: the unowned child died anyway",
        )
        self.assertTrue(process_alive(grandchild))


class OwnedLifetimeTests(ProcessLifetimeCase):
    """The same parent, the same child, bound by the kernel instead."""

    def test_an_owned_child_dies_when_its_owner_is_forcibly_terminated(self) -> None:
        """TerminateProcess: no atexit, no finally, no protocol message."""
        parent = self.start_parent(owned=True)
        child, grandchild = self.tree()
        self.assertTrue(process_alive(child))
        parent.kill()
        parent.wait(timeout=30)
        self.assert_reaped(child, "the owned browser stand-in")

    def test_the_whole_descendant_tree_dies_with_it(self) -> None:
        """A browser is a tree; containment that stopped at one process would
        leave the renderer - which is where the page actually runs."""
        parent = self.start_parent(owned=True)
        child, grandchild = self.tree()
        self.assertTrue(process_alive(grandchild))
        parent.kill()
        parent.wait(timeout=30)
        self.assert_reaped(child, "the owned browser stand-in")
        self.assert_reaped(grandchild, "a descendant of the owned browser")

    def test_an_owned_child_dies_when_its_owner_exits_normally(self) -> None:
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                # Exits on its own, but not before the stand-in has reported
                # its tree - otherwise the job closes with nothing to observe.
                _parent_source(owned=True, marker=self.marker, pids=self.pids).replace(
                    "time.sleep(600)\n", "time.sleep(4)\n", 1
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.started.append(parent.pid)
        child, grandchild = self.tree()
        parent.wait(timeout=30)
        self.assert_reaped(child, "the owned browser stand-in")
        self.assert_reaped(grandchild, "a descendant of the owned browser")


@unittest.skipUnless(_IS_WINDOWS, "process lifetime is bound with a Windows job object")
class JobConfigurationTests(unittest.TestCase):
    """The job is configured to kill, and to let nothing escape."""

    def owned(self):
        process = launch_owned([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(process.close)
        return process

    def test_the_job_kills_on_close_and_permits_no_breakaway(self) -> None:
        flags = self.owned().limit_flags()
        self.assertTrue(flags & 0x2000, "kill-on-close is not set")
        self.assertFalse(flags & 0x0800, "JOB_OBJECT_LIMIT_BREAKAWAY_OK is set")
        self.assertFalse(flags & 0x1000, "SILENT_BREAKAWAY_OK is set")
        self.assertEqual(0x2000, flags, "the job carries limits this build did not ask for")

    def test_the_job_handle_is_not_inheritable(self) -> None:
        """An inherited job handle would mean JARVIS's close is not the last."""
        self.assertFalse(self.owned().job_is_inheritable())

    def test_the_child_is_verified_to_be_inside_the_job(self) -> None:
        process = self.owned()
        self.assertTrue(process.alive())
        self.assertTrue(process.contained())

    def test_closing_the_job_terminates_the_child(self) -> None:
        process = launch_owned([sys.executable, "-c", "import time; time.sleep(600)"])
        pid = process.pid
        self.assertTrue(process_alive(pid))
        process.close()
        deadline = time.monotonic() + REAPED_TIMEOUT
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.05)
        self.assertFalse(process_alive(pid))
        # And a released binding reports itself gone rather than half-alive.
        self.assertFalse(process.alive())
        self.assertFalse(process.contained())

    def test_a_failed_assignment_kills_the_child_and_raises(self) -> None:
        """Fail closed: never a running process JARVIS does not own."""
        seen: list[int] = []

        def refuse(job: int, process: int) -> bool:
            seen.append(process)
            return False

        with self.assertRaises(ProcessOwnershipError):
            launch_owned([sys.executable, "-c", "import time; time.sleep(600)"], assign=refuse)
        self.assertEqual(1, len(seen), "the child was never created, so nothing was proved")

    def test_an_empty_command_starts_nothing(self) -> None:
        with self.assertRaises(ProcessOwnershipError):
            launch_owned([])

    def test_a_missing_executable_fails_closed(self) -> None:
        with self.assertRaises(ProcessOwnershipError):
            launch_owned([str(Path(tempfile.gettempdir()) / "no-such-jarvis-browser.exe")])

    def test_ownership_is_available_on_this_platform(self) -> None:
        self.assertTrue(ownership_is_available())


class _Endpoint(HTTPServer):
    """Something answering ``/json/version`` on a port. Not JARVIS's browser."""

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _EndpointHandler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _EndpointHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - the stdlib spelling
        body = json.dumps(
            {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.server.port}/devtools/browser/x"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover - quiet
        pass


class AttachOwnershipTests(unittest.TestCase):
    """Answering on the port is not the same as being JARVIS's browser."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.profile = str(Path(self.temp.name) / "browser-profile")
        self.addCleanup(release_owned_browsers)

    def test_an_unowned_debug_endpoint_is_refused_not_adopted(self) -> None:
        """DB-002B-01. Driving this would put approved pages in a browser
        whose lifetime, profile and mediation JARVIS does not control."""
        endpoint = _Endpoint()
        self.addCleanup(endpoint.stop)
        with self.assertRaises(BrowserCommandFailed) as raised:
            attached_browser(profile_dir=self.profile, port=endpoint.port)
        self.assertIn("not a browser JARVIS owns", str(raised.exception))

    def test_nothing_is_launched_to_work_around_a_taken_port(self) -> None:
        endpoint = _Endpoint()
        self.addCleanup(endpoint.stop)
        launched: list[list[str]] = []

        def spawn(argv):
            launched.append(argv)
            raise AssertionError("a browser was started onto an occupied port")

        with self.assertRaises(BrowserCommandFailed):
            attached_browser(profile_dir=self.profile, port=endpoint.port, spawn=spawn)
        self.assertEqual([], launched)
        self.assertIsNone(owned_browser(endpoint.port))

    def stand_in(self, port: int):
        """An owned browser stand-in that really listens on ``port``."""
        process = launch_owned([sys.executable, "-c", _STAND_IN, str(port)])
        self.addCleanup(process.close)
        return process

    def test_a_browser_this_process_launched_is_reused(self) -> None:
        """Within one JARVIS run: the page state an approved click was read
        against lives in that browser, so it must be the same one."""
        port = _free_port()
        argv: list[list[str]] = []
        started: list = []

        def spawn(command):
            argv.append(command)
            started.append(self.stand_in(port))
            return started[-1]

        first = attached_browser(
            profile_dir=self.profile,
            port=port,
            executable_lookup=_trusted_stand_in,
            spawn=spawn,
        )
        self.assertEqual(f"ws://127.0.0.1:{port}/devtools/browser/x", first)
        # The argument vector is the trusted executable plus this build's own
        # flags, and nothing else.
        self.assertEqual(
            [
                _TRUSTED_PATH,
                *browser_arguments(
                    profile_dir=self.profile,
                    port=port,
                    proxy_port=browser_policy_proxy(port).port,
                ),
            ],
            argv[0],
        )
        # The endpoint really is the owned process, asked of the OS.
        self.assertEqual(started[0].pid, listening_pid(port))
        self.assertTrue(started[0].contains(listening_pid(port)))
        # Second call: same endpoint, no second browser.
        self.assertEqual(first, attached_browser(profile_dir=self.profile, port=port))
        self.assertEqual(1, len(argv))
        self.assertIs(started[0], owned_browser(port))

    def test_an_endpoint_outside_the_job_is_refused_even_while_we_own_one(self) -> None:
        """The port-steal case: a live owned browser is not a licence to drive
        whatever happens to be answering."""
        endpoint = _Endpoint()
        self.addCleanup(endpoint.stop)
        mine = launch_owned([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(mine.close)
        import jarvis.browser_cdp as cdp

        cdp._OWNED_BROWSERS[endpoint.port] = mine
        self.addCleanup(cdp._OWNED_BROWSERS.pop, endpoint.port, None)
        # The owned process is alive and contained...
        self.assertIsNotNone(owned_browser(endpoint.port))
        # ...but it is not what is listening, so the endpoint is refused.
        self.assertFalse(mine.contains(listening_pid(endpoint.port)))
        with self.assertRaises(BrowserCommandFailed):
            attached_browser(profile_dir=self.profile, port=endpoint.port)

    def test_a_browser_that_died_is_not_reported_as_owned(self) -> None:
        """A dead owned browser cannot lend its ownership to anything."""
        port = _free_port()
        started: list = []

        def spawn(command):
            started.append(self.stand_in(port))
            return started[-1]

        attached_browser(
            profile_dir=self.profile,
            port=port,
            executable_lookup=_trusted_stand_in,
            spawn=spawn,
        )
        self.assertIsNotNone(owned_browser(port))
        started[0].close()
        deadline = time.monotonic() + REAPED_TIMEOUT
        while time.monotonic() < deadline and process_alive(started[0].pid):
            time.sleep(0.05)
        self.assertIsNone(owned_browser(port))

    def test_a_browser_that_cannot_be_owned_is_never_started_unowned(self) -> None:
        def refuse(command):
            raise ProcessOwnershipError("no job object here")

        with self.assertRaises(BrowserCommandFailed) as raised:
            attached_browser(
                profile_dir=self.profile,
                port=_free_port(),
                executable_lookup=_trusted_stand_in,
                spawn=refuse,
                sleep=lambda seconds: None,
            )
        self.assertIn("could not be started", str(raised.exception))

    def test_a_port_taken_during_startup_is_refused_not_driven(self) -> None:
        """The race the endpoint check exists for: the port was free when the
        browser was launched, and something else answered first."""
        port = _free_port()
        mine = launch_owned([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(mine.close)

        def spawn(command):
            # A foreign listener appears while "the browser" is starting. It
            # belongs to this test process, which is not in the job.
            endpoint = _Endpoint(port)
            self.addCleanup(endpoint.stop)
            return mine

        with self.assertRaises(BrowserCommandFailed) as raised:
            attached_browser(
                profile_dir=self.profile,
                port=port,
                executable_lookup=_trusted_stand_in,
                spawn=spawn,
                sleep=lambda seconds: None,
            )
        self.assertIn("other than the browser JARVIS started", str(raised.exception))
        self.assertIsNone(owned_browser(port))

    def test_a_browser_that_never_answers_is_not_left_running(self) -> None:
        """An owned-but-unusable browser is released, not abandoned."""
        started = launch_owned([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(started.close)
        port = _free_port()
        import jarvis.browser_cdp as cdp

        original = cdp.BROWSER_LAUNCH_TIMEOUT_SECONDS
        cdp.BROWSER_LAUNCH_TIMEOUT_SECONDS = 0.5
        self.addCleanup(setattr, cdp, "BROWSER_LAUNCH_TIMEOUT_SECONDS", original)
        with self.assertRaises(BrowserCommandFailed):
            attached_browser(
                profile_dir=self.profile,
                port=port,
                executable_lookup=_trusted_stand_in,
                spawn=lambda command: started,
                sleep=lambda seconds: None,
            )
        self.assertIsNone(owned_browser(port))
        deadline = time.monotonic() + REAPED_TIMEOUT
        while time.monotonic() < deadline and process_alive(started.pid):
            time.sleep(0.05)
        self.assertFalse(process_alive(started.pid))

    def test_a_browser_that_exits_early_is_reported_not_waited_out(self) -> None:
        """Requirement 7: the browser dies during startup."""
        port = _free_port()
        gone = launch_owned([sys.executable, "-c", "pass"])
        self.addCleanup(gone.close)
        deadline = time.monotonic() + REAPED_TIMEOUT
        while time.monotonic() < deadline and gone.alive():
            time.sleep(0.05)
        with self.assertRaises(BrowserCommandFailed) as raised:
            attached_browser(
                profile_dir=self.profile,
                port=port,
                executable_lookup=_trusted_stand_in,
                spawn=lambda command: gone,
                sleep=lambda seconds: None,
            )
        self.assertIn("exited before it opened a debugging socket", str(raised.exception))
        self.assertIsNone(owned_browser(port))

    def test_the_launch_arguments_still_name_the_dedicated_profile(self) -> None:
        arguments = browser_arguments(profile_dir=self.profile, port=9333, proxy_port=54321)
        self.assertIn(f"--user-data-dir={self.profile}", arguments)
        self.assertIn("about:blank", arguments)

    def test_releasing_jarvis_browsers_never_touches_an_unrelated_process(self) -> None:
        """Cleanup releases JARVIS's own job objects and proxies. It never
        matches a process by name, so an unrelated process that merely looks
        like a browser (here: another python, standing in for the operator's
        own Chrome) is untouched."""
        import jarvis.browser_cdp as cdp

        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(bystander.kill)
        mine = self.stand_in(_free_port())
        cdp._OWNED_BROWSERS[9_999] = mine
        cdp.release_owned_browsers()
        cdp.release_browser_policies()
        deadline = time.monotonic() + REAPED_TIMEOUT
        while time.monotonic() < deadline and process_alive(mine.pid):
            time.sleep(0.05)
        self.assertFalse(process_alive(mine.pid))                      # ours: gone with its job
        self.assertIsNone(bystander.poll())                            # theirs: still running
        self.assertTrue(process_alive(bystander.pid))


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ChromeProfileTests(unittest.TestCase):
    """V2 M1 Gate 2: the browser profile is a fresh Chrome directory beside
    the memory database - never the Edge profile it replaced, never the
    operator's own Chrome profile."""

    def settings(self):
        from dataclasses import replace

        from jarvis.settings import ConfigStore

        root = Path(tempfile.mkdtemp(prefix="jarvis-chrome-profile-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        loaded = ConfigStore(root / "config.json").load().settings
        return replace(loaded, memory_database=root / "jarvis.db"), root

    def test_the_profile_is_chrome_profile_beside_the_database(self) -> None:
        settings, root = self.settings()
        self.assertEqual(root / "chrome-profile", settings.browser_profile_dir)

    def test_the_edge_profile_is_a_sibling_that_is_never_named(self) -> None:
        settings, root = self.settings()
        profile = settings.browser_profile_dir
        self.assertNotEqual("browser-profile", profile.name)
        self.assertEqual(profile.parent, (root / "browser-profile").parent)     # same parent: rollback stays beside
        arguments = browser_arguments(profile_dir=str(profile), port=9333, proxy_port=1)
        self.assertIn(f"--user-data-dir={profile}", arguments)
        self.assertFalse(any("browser-profile" in a for a in arguments), arguments)

    def test_the_operators_own_chrome_profile_is_never_named(self) -> None:
        settings, _ = self.settings()
        profile = str(settings.browser_profile_dir)
        self.assertNotIn("User Data", profile)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            # Chrome's own profile root, whatever the temp directory happens to be under.
            self.assertFalse(Path(profile).is_relative_to(Path(local) / "Google" / "Chrome" / "User Data"), profile)
        arguments = browser_arguments(profile_dir=profile, port=9333, proxy_port=1)
        self.assertFalse(any("User Data" in a or "Google\\Chrome" in a for a in arguments), arguments)

    def test_the_current_tab_note_sits_beside_the_profile_not_inside_it(self) -> None:
        settings, root = self.settings()
        note = cdp_module._current_tab_path(str(settings.browser_profile_dir))
        self.assertEqual(root / "current-tab", note)

    def test_the_debugging_endpoint_is_ipv4_loopback_only(self) -> None:
        """The ownership model is IPv4: discovery asks 127.0.0.1, the listener
        check reads the IPv4 table, and a socket named on any other host -
        including the IPv6 loopback - is refused rather than connected."""
        self.assertEqual("http://127.0.0.1:9333", cdp_module._endpoint(9333))
        for url in ("ws://[::1]:9333/devtools/browser/x", "ws://0.0.0.0:9333/x", "ws://192.168.1.2:9333/x", "wss://127.0.0.1:9333/x"):
            with self.subTest(url=url), self.assertRaises(BrowserCommandFailed):
                cdp_module._WebSocket(url, timeout=0.2)


#: What ``executable_lookup`` hands ``attached_browser`` here: a trusted
#: identity whose ``argv0()`` answers the way a real ``TrustedExecutable``
#: does, so the spawn boundary runs exactly as in production - the identity
#: re-check included - without a browser binary. The browser these tests
#: "start" is the python stand-in ``spawn`` supplies; the path is never run.
_TRUSTED_PATH = "C:/chrome.exe"


class _TrustedStandIn:
    path = _TRUSTED_PATH

    def argv0(self, **_kwargs: object) -> str:
        return self.path


def _trusted_stand_in() -> _TrustedStandIn:
    return _TrustedStandIn()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
