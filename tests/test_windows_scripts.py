from __future__ import annotations

import json
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main()
