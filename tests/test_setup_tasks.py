"""SETUPTASKS1: scripts/setup_tasks.ps1 is checked as text only; nothing is registered."""
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SETUP = SCRIPTS / "setup_tasks.ps1"

# Task names other code starts by name: schtasks /TN "\X", Start-ScheduledTask 'X', $Name = 'X'.
NAME_PATTERNS = (
    re.compile(r'"/TN",\s*"\\\\([^"]+)"'),
    re.compile(r"Start-ScheduledTask\s+(?:-TaskName\s+)?'([^']+)'"),
    re.compile(r"^\$Name\s*=\s*'([^']+)'", re.M),
)


def referenced_task_names():
    names = set()
    for path in list(SCRIPTS.glob("*.py")) + list(SCRIPTS.glob("*.ps1")):
        if path.name == SETUP.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in NAME_PATTERNS:
            names.update(pattern.findall(text))
    return names


def registered_task_names(text):
    return set(re.findall(r"^\s*Name\s*=\s*'([^']+)'", text, re.M))


class SetupTasksScriptTest(unittest.TestCase):
    def setUp(self):
        self.raw = SETUP.read_bytes()
        self.text = self.raw.decode("ascii")

    def test_ascii_only(self):
        self.assertTrue(all(b < 128 for b in self.raw))

    def test_no_absolute_user_paths(self):
        for needle in ("realk", "Masa", "C:\\Users", "C:/Users", "AppData", "Python313"):
            self.assertNotIn(needle.lower(), self.text.lower(), needle)

    def test_repo_root_from_script_dir(self):
        self.assertIn("Join-Path $PSScriptRoot '..'", self.text)

    def test_every_referenced_task_is_registered(self):
        wanted = referenced_task_names()
        self.assertTrue({"JARVIS Konveyor", "Konveyor Yeniden Baslat"} <= wanted, wanted)
        self.assertLessEqual(wanted, registered_task_names(self.text))

    def test_default_is_disabled(self):
        self.assertRegex(self.text, r"\[switch\]\$Enable\b")
        self.assertIn("else { $on = $false }", self.text)
        self.assertIn("if (-not $on) { $settings.Enabled = $false }", self.text)

    def test_side_effects_behind_should_process(self):
        self.assertIn("SupportsShouldProcess = $true", self.text)
        lines = self.text.splitlines()
        for verb in ("Register-ScheduledTask @p", "Copy-Item"):
            idx = [i for i, line in enumerate(lines) if verb in line]
            self.assertEqual(len(idx), 1, verb)
            self.assertIn("ShouldProcess", lines[idx[0] - 1], verb)

    def test_does_not_touch_existing_tasks_otherwise(self):
        for verb in ("Unregister-ScheduledTask", "Start-ScheduledTask", "Disable-ScheduledTask",
                     "Stop-ScheduledTask", "schtasks.exe", "/Delete", "/Change"):
            self.assertNotIn(verb, self.text)

    def test_no_secrets(self):
        self.assertNotRegex(self.text, r"(?i)ntfy\.sh/|-Password|SetEnvironmentVariable|\$env:JARVIS_")


if __name__ == "__main__":
    unittest.main()
