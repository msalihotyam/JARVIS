"""PM16 check runner: allowlisted argv, scrubbed offline env, stage-only cwd,
tree kill on timeout, capped output. Temp dirs only; no network is touched."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import work_checks
from jarvis.work_checks import DEAD_PROXY, CheckRefused, run_check

WINDOWS = sys.platform == "win32"
PY = {"python": sys.executable}


class _Stage(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stage = Path(tmp.name)
        (self.stage / "tests").mkdir()
        (self.stage / "tests" / "__init__.py").write_text("")
        (self.stage / "a.py").write_text("x = 1\n")

    def module(self, name: str, body: str) -> str:
        (self.stage / "tests" / f"{name}.py").write_text(body)
        return f"tests.{name}"

    def refused(self, argv, code: str, **kw) -> None:
        kw.setdefault("executables", PY)
        with mock.patch.object(work_checks.subprocess, "Popen") as popen:
            with self.assertRaises(CheckRefused) as cm:
                run_check(argv, stage=self.stage, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception)
        popen.assert_not_called()


class Refusals(_Stage):
    def test_string_command_is_never_an_argv(self) -> None:
        self.refused("python -m unittest tests.test_x", "not_argv")
        self.refused(b"python -m unittest", "not_argv")
        self.refused(["python", "-m", "unittest", 7], "not_argv")
        self.refused([], "not_argv")

    def test_unlisted_argv_refused(self) -> None:
        for argv in (["python", "-c", "print(1)"], ["cmd", "/c", "dir"],
                     ["python", "-m", "pip", "install", "x"], ["python", "-m"],
                     ["npx", "jest"], ["ruff", "format", "a.py"]):
            with self.subTest(argv=argv):
                self.refused(argv, "argv_not_allowlisted")

    def test_placeholder_must_be_a_validated_target(self) -> None:
        for argv in (["python", "-m", "pytest", "../x"], ["python", "-m", "pytest", "-p"],
                     ["python", "-m", "pytest", "-pplugin"], ["ruff", "check", "missing.py"],
                     ["ruff", "check", "C:/Windows"], ["ruff", "check", ".env"],
                     ["python", "-m", "unittest", "foo.bar"], ["python", "-m", "unittest", "-k"],
                     ["python", "-m", "unittest", "tests/test_x.py"],
                     ["python", "-m", "pytest", "@a.py"]):
            with self.subTest(argv=argv):
                self.refused(argv, "not_a_target")
        self.refused(["npm", "test", "a.py"], "placeholder_not_allowed")
        # Targets resolve from the child's cwd, not the stage root.
        (self.stage / "sub").mkdir()
        self.refused(["ruff", "check", "a.py"], "not_a_target", cwd=self.stage / "sub")

    def test_shell_metacharacters_never_reach_argv(self) -> None:
        for arg in ("tests.x;rm", "tests.x&calc", "a.py|x", "a.py>out", "%PATH%",
                    "a.py b.py", "$(x)", "a.py\nx", "tests.x\x00"):
            with self.subTest(arg=arg):
                self.refused(["python", "-m", "pytest", arg], "shell_metacharacter")

    def test_cwd_outside_stage_refused(self) -> None:
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.refused(["python", "-m", "unittest"], "cwd_outside_stage", cwd=other.name)
        self.refused(["python", "-m", "unittest"], "cwd_outside_stage", cwd=self.stage / "..")
        self.refused(["python", "-m", "unittest"], "cwd_outside_stage", cwd=self.stage / "a.py")
        with self.assertRaises(CheckRefused) as cm:
            run_check(["python", "-m", "unittest"], stage=self.stage / "nope", executables=PY)
        self.assertEqual(cm.exception.code, "stage_missing")

    def test_missing_or_untrusted_executable_refused(self) -> None:
        planted = self.stage / "python.exe"
        planted.write_bytes(b"MZ")
        for executables in ({"python": str(self.stage / "nope.exe")},
                            {"python": str(planted)}, {"python": "python"}):
            with self.subTest(executables=executables):
                self.refused(["python", "-m", "unittest"], "missing_executable",
                             executables=executables)
        self.refused(["ruff", "check", "a.py"], "missing_executable", executables=None,
                     environ={"PATH": str(self.stage)})


@unittest.skipUnless(WINDOWS, "job-object ownership is Windows-only")
class Runs(_Stage):
    def run_py(self, module: str, **kw):
        return run_check(["python", "-m", "unittest", module], stage=self.stage,
                         executables=PY, **kw)

    def test_shell_true_is_impossible(self) -> None:
        seen = {}

        def fake(args, **kwargs):
            seen.update(kwargs, args=args)
            raise OSError("not really spawning")

        with mock.patch.object(work_checks.subprocess, "Popen", side_effect=fake):
            with self.assertRaises(CheckRefused) as cm:
                self.run_py("tests")
        self.assertEqual(cm.exception.code, "spawn_failed")
        self.assertIs(seen["shell"], False)
        self.assertIsInstance(seen["args"], list)
        self.assertEqual(seen["args"][0], str(Path(sys.executable).resolve()))
        self.assertEqual(Path(seen["cwd"]), self.stage.resolve())

    def test_passing_check_returns_typed_result(self) -> None:
        mod = self.module("test_ok", "import unittest\n"
                          "class T(unittest.TestCase):\n    def test_a(self): pass\n")
        result = self.run_py(mod)
        self.assertEqual((result.exit_code, result.runner, result.timed_out, result.truncated),
                         (0, "unittest", False, False))
        self.assertIn("OK", result.stderr)
        self.assertGreaterEqual(result.duration_s, 0)

    def test_secret_env_absent_and_proxy_dead(self) -> None:
        mod = self.module("test_env", "import json, os, sys, urllib.request\n"
                          "sys.stdout.write(json.dumps({'env': dict(os.environ),"
                          " 'proxies': urllib.request.getproxies()}))\n")
        environ = dict(os.environ, FAKE_SECRET_TOKEN="pm16-s3cr3t",
                       ANTHROPIC_API_KEY="pm16-s3cr3t", GITHUB_TOKEN="pm16-s3cr3t",
                       NPM_CONFIG__AUTH="pm16-s3cr3t", HTTP_PROXY="http://evil:1",
                       NO_PROXY="*")
        result = self.run_py(mod, environ=environ)
        self.assertNotIn("pm16-s3cr3t", result.stdout + result.stderr)
        shown = json.loads(result.stdout)
        env = {k.upper(): v for k, v in shown["env"].items()}
        for name in ("FAKE_SECRET_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
                     "NPM_CONFIG__AUTH", "HOMEDRIVE", "HOMEPATH"):
            self.assertNotIn(name, env)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            self.assertEqual(env[name], DEAD_PROXY)
        self.assertEqual(env["NO_PROXY"], "")
        self.assertEqual(shown["proxies"].get("https"), DEAD_PROXY)
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP"):
            self.assertNotEqual(env[name], os.environ.get(name))
            self.assertFalse(Path(env[name]).exists(), "scratch home is removed after the run")

    def test_timeout_kills_the_whole_tree(self) -> None:
        pidfile = self.stage / "grandchild.pid"
        mod = self.module("test_tree", "import subprocess, sys, time\n"
                          "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                          f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
                          "time.sleep(120)\n")
        result = self.run_py(mod, timeout_s=8)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        pid = int(pidfile.read_text())
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:  # gone entirely is also dead
            try:
                self.assertEqual(k32.WaitForSingleObject(handle, 10_000), 0, "grandchild alive")
            finally:
                k32.CloseHandle(handle)

    def test_output_is_capped_in_bytes(self) -> None:
        mod = self.module("test_loud", "import sys\nsys.stdout.write('x' * 300_000)\n")
        result = self.run_py(mod, output_cap=1000)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout.encode()), 1000)
        self.assertFalse(result.timed_out)


if __name__ == "__main__":
    unittest.main()
