from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class WindowsScriptTests(unittest.TestCase):
    def test_python_version_guard_rejects_pre_311(self) -> None:
        powershell = shutil.which("powershell")
        if powershell is None:
            self.skipTest("Windows PowerShell is unavailable")
        helper = Path(__file__).parents[1] / "scripts" / "python_version.ps1"
        command = (
            f". '{helper}'; "
            "@((Test-JarvisPythonVersion '3.10.9'), "
            "(Test-JarvisPythonVersion '3.11.0'), "
            "(Test-JarvisPythonVersion '4.0.0')) | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual([False, True, True], json.loads(completed.stdout))

    def test_select_python_prefers_pyside6_then_falls_back(self) -> None:
        powershell = shutil.which("powershell")
        if powershell is None:
            self.skipTest("Windows PowerShell is unavailable")
        helper = Path(__file__).parents[1] / "scripts" / "python_version.ps1"
        with tempfile.TemporaryDirectory() as tmp:
            def fake(name: str, version: str, ui: bool) -> str:
                path = Path(tmp) / f"{name}.cmd"
                path.write_text(
                    "@echo off\r\n"
                    'echo %* | findstr /c:"import PySide6" >nul && exit /b '
                    f"{0 if ui else 1}\r\n"
                    f"echo {version}\r\n",
                    encoding="ascii",
                )
                return str(path)

            old = fake("old", "3.10.1", True)
            bare = fake("bare", "3.12.1", False)
            ui = fake("ui", "3.12.2", True)

            def pick(candidates: list[str], explicit: str = "") -> object:
                quoted = ",".join(f"'{c}'" for c in candidates)
                command = (
                    f". '{helper}'; "
                    f"Select-JarvisPython -Candidates @({quoted}) "
                    f"-Explicit '{explicit}' | ConvertTo-Json -Compress"
                )
                out = subprocess.run(
                    [powershell, "-NoProfile", "-Command", command],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                return json.loads(out) if out else None

            self.assertEqual(ui, pick([old, bare, ui])["Path"])
            self.assertEqual(bare, pick([old, bare])["Path"])
            self.assertFalse(pick([old, bare])["HasPySide6"])
            self.assertEqual(bare, pick([bare, ui], explicit=bare)["Path"])
            self.assertIsNone(pick([old]))


if __name__ == "__main__":
    unittest.main()
