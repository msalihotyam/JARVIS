"""TYPEGATE3: the files CI type-checks (scripts/type_gate.txt) stay a sane list.

CI's "types" job runs basedpyright on exactly these paths; an entry that is
missing, duplicated or outside jarvis/ or scripts/ would silently weaken it.
"""
import posixpath
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "scripts" / "type_gate.txt"
PATCH = ROOT / "build" / "reports" / "TYPEGATE3.tests.yml.patch"
PIN = "basedpyright==1.40.2"


def entry_problem(line: str) -> str:
    """Why a list line is not a valid gated path, or "" when it is."""
    if not line or line != line.strip():
        return "blank or padded"
    if "\\" in line or line.startswith("/") or ":" in line:
        return "not a relative posix path"
    if posixpath.normpath(line) != line:
        return "not normalized"
    if not line.startswith(("jarvis/", "scripts/")):
        return "outside jarvis/ or scripts/"
    if not line.endswith(".py"):
        return "not a .py file"
    return ""


class TypeGateTests(unittest.TestCase):
    def setUp(self):
        self.lines = LIST.read_text(encoding="utf-8").splitlines()

    def test_list_is_valid_unique_and_tracked(self):
        self.assertTrue(self.lines)
        self.assertEqual(len(self.lines), len(set(self.lines)))
        for line in self.lines:
            self.assertEqual(entry_problem(line), "", line[:80])
        tracked = subprocess.run(
            ["git", "ls-files", "--", *self.lines], cwd=ROOT,
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(sorted(tracked), sorted(self.lines))

    def test_entries_outside_jarvis_or_scripts_are_rejected(self):
        for bad in ("tests/test_x.py", "jarvis/../tests/x.py", "/jarvis/x.py",
                    "C:/jarvis/x.py", "jarvis\\x.py", " jarvis/x.py", "",
                    "jarvis/x.txt", "./jarvis/x.py", "docs/x.py"):
            self.assertNotEqual(entry_problem(bad), "", bad)

    def test_patch_runs_the_list_with_the_pinned_version(self):
        if PATCH.exists():  # build/ is not in git, so CI has no patch to read
            text = PATCH.read_text(encoding="utf-8")
            self.assertIn("scripts/type_gate.txt", text)
            self.assertIn(PIN, text)
            self.assertIn("--level error", text)


if __name__ == "__main__":
    unittest.main()
