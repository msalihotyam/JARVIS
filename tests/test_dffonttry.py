"""DFFONTTRY: the startup-only font render switch (native, O, T) and its throwaway-data launcher.

No test starts JARVIS or opens data\\jarvis.db: main() is stopped at its
QApplication, and the launcher's seed runs on a TEMP home with the live-database
guard imported first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - the live-database guard
from PySide6.QtGui import QFont

from jarvis.ui import app as ui_app

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dev" / "font_try.ps1"
FONT_ENV = ("QT_QPA_PLATFORM", "FREETYPE_PROPERTIES", ui_app.FONT_RENDER_ENV)


class FakeApp:
    def __init__(self):
        self.fonts = []

    def font(self):
        return QFont()

    def setFont(self, font):  # noqa: N802 - Qt's name
        self.fonts.append(font)


class Stop(Exception):
    pass


class SwitchTests(unittest.TestCase):
    def mode(self, value):
        """apply_font_render_env on a clean font environment: (mode, what it set, setFont calls)."""
        env = {k: v for k, v in os.environ.items() if k not in FONT_ENV}
        if value is not None:
            env[ui_app.FONT_RENDER_ENV] = value
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "platform", "win32"):
            mode = ui_app.apply_font_render_env()
            seen = {k: os.environ.get(k) for k in ("QT_QPA_PLATFORM", "FREETYPE_PROPERTIES")}
        app = FakeApp()
        ui_app.apply_font_render_hinting(app, mode)
        return mode, seen, app.fonts

    def test_unset_or_garbage_is_t_and_native_touches_nothing(self):
        """DFFONT: T is the default; only an explicit native is today's untouched native path."""
        for value in (None, "", "o", "t", "OT", "freetype", " O"):
            with self.subTest(value=value):
                self.assertEqual(self.mode(value), ("T", {"QT_QPA_PLATFORM": "windows:fontengine=freetype",
                                                          "FREETYPE_PROPERTIES": "autofitter:no-stem-darkening=0"},
                                                    self.mode("T")[2]))
        self.assertEqual(self.mode("native"), ("native", {"QT_QPA_PLATFORM": None, "FREETYPE_PROPERTIES": None}, []))

    def test_o_is_freetype_with_vertical_hinting(self):
        mode, seen, fonts = self.mode("O")
        self.assertEqual(mode, "O")
        self.assertEqual(seen, {"QT_QPA_PLATFORM": "windows:fontengine=freetype", "FREETYPE_PROPERTIES": None})
        self.assertEqual([f.hintingPreference() for f in fonts], [QFont.HintingPreference.PreferVerticalHinting])

    def test_t_is_o_plus_stem_darkening(self):
        mode, seen, fonts = self.mode("T")
        self.assertEqual(mode, "T")
        self.assertEqual(seen, {"QT_QPA_PLATFORM": "windows:fontengine=freetype",
                                "FREETYPE_PROPERTIES": "autofitter:no-stem-darkening=0"})
        self.assertEqual([f.hintingPreference() for f in fonts], [QFont.HintingPreference.PreferVerticalHinting])

    def test_a_chosen_platform_keeps_winning(self):
        with mock.patch.dict(os.environ, {ui_app.FONT_RENDER_ENV: "O", "QT_QPA_PLATFORM": "offscreen"}), \
                mock.patch.object(sys, "platform", "win32"):
            ui_app.apply_font_render_env()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")

    def test_off_windows_is_native(self):
        with mock.patch.dict(os.environ, {ui_app.FONT_RENDER_ENV: "T"}), mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(ui_app.apply_font_render_env(), "native")

    def test_main_sets_the_environment_before_qapplication_and_hints_after(self):
        """main() up to its QApplication: the engine env is in place when it is built, the
        hinting goes onto that very app, and nothing else of the startup runs."""
        calls = []

        def qapplication(argv):
            calls.append(("QApplication", os.environ.get("QT_QPA_PLATFORM"), os.environ.get("FREETYPE_PROPERTIES")))
            return "the app"

        def hinting(app, mode):
            calls.append(("hinting", app, mode))
            raise Stop

        env = {k: v for k, v in os.environ.items() if k not in FONT_ENV}
        env[ui_app.FONT_RENDER_ENV] = "T"
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "platform", "win32"), \
                mock.patch("PySide6.QtWidgets.QApplication", qapplication), \
                mock.patch.object(ui_app, "apply_font_render_hinting", hinting), \
                mock.patch.object(ui_app, "Backend", side_effect=AssertionError("no store")):
            with self.assertRaises(Stop):
                ui_app.main([], instance=None)
        self.assertEqual(calls, [("QApplication", "windows:fontengine=freetype", "autofitter:no-stem-darkening=0"),
                                 ("hinting", "the app", "T")])


class LauncherTests(unittest.TestCase):
    text = SCRIPT.read_text(encoding="utf-8")

    def test_script_is_ascii(self):
        """Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page."""
        self.assertTrue(self.text.isascii())

    def test_modes_and_the_switch_reach_only_the_child(self):
        self.assertIn("[ValidateSet('native', 'O', 'T')]", self.text)
        self.assertIn("$env_['JARVIS_FONT_RENDER'] = $Mode", self.text)
        self.assertIn("$env_['USERPROFILE'] = (Join-Path $root 'home')", self.text)
        self.assertNotRegex(self.text, r"\$env:\w+\s*=|Set-Item\s+env:|Remove-Item\s+env:|SetEnvironmentVariable")
        self.assertIn('Write-Host "JARVIS font render: $Mode"', self.text)

    def test_refuses_while_jarvis_runs(self):
        self.assertIn("$_.Name -eq 'JARVIS.exe'", self.text)
        self.assertIn(r"-m\s+jarvis\.ui\b|launch_jarvis\.py", self.text)
        refusal = self.text.index("if ($running.Count -gt 0) {")
        self.assertLess(refusal, self.text.index("Quit JARVIS"))
        self.assertLess(self.text.index("Quit JARVIS"), self.text.index("exit 1", refusal))
        self.assertLess(self.text.index("exit 1", refusal), self.text.index("$temp ="))

    def test_the_data_folder_is_a_new_temp_folder_never_data_jarvis_db(self):
        self.assertIn("$temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())", self.text)
        self.assertIn("Join-Path $temp ('jarvis-font-try\\' + $Mode + '-' + (Get-Date", self.text)
        self.assertIn("$db = [IO.Path]::GetFullPath((Join-Path $root 'data\\jarvis.db'))", self.text)
        # data\jarvis.db appears only under $root and in the $live comparison that refuses it.
        self.assertEqual(sorted(re.findall(r"Join-Path \$(\w+) 'data\\jarvis\.db'", self.text)), ["repo", "root"])
        self.assertIn("$db -ieq $live", self.text)
        refuse = self.text.index("REFUSED: the throwaway database")
        self.assertLess(refuse, self.text.index("New-Item"))
        self.assertLess(refuse, self.text.index("Start-Python @("))

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell")
    def test_script_parses(self):
        """Parsed only (the PowerShell AST); nothing in it runs."""
        command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{SCRIPT}', [ref]$null, [ref]$e); $e.Count")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                             capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(out.stdout.strip(), "0", out.stderr)


class SeedTests(unittest.TestCase):
    """The launcher's own seed (its here-string), on a TEMP run folder, guard first."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="jarvis-dffonttry-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        source = re.search(r"\$seed = @'\r?\n(.*?)\r?\n'@", SCRIPT.read_text(encoding="utf-8"), re.S).group(1)
        self.seed = self.root / "seed.py"
        self.seed.write_text(source, encoding="utf-8")

    def run_seed(self, home: Path):
        (self.root / "home").mkdir(exist_ok=True)
        (self.root / "data").mkdir(exist_ok=True)
        env = {**os.environ, "USERPROFILE": str(home), "PYTHONPATH": str(ROOT)}
        code = (f"import sys, runpy, tests; sys.argv = ['seed.py', {str(self.root)!r}]; "
                f"runpy.run_path({str(self.seed)!r}, run_name='__main__')")
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=120)

    def test_seed_writes_the_throwaway_config_and_the_sample_chat(self):
        from jarvis.memory import SQLiteMemoryStore
        from jarvis.settings import ConfigStore, PROJECT_ROOT

        done = self.run_seed(self.root / "home")
        self.assertEqual(done.returncode, 0, done.stderr)
        db = self.root / "data" / "jarvis.db"
        settings = ConfigStore(self.root / "home" / ".jarvis" / "config.json").load().settings
        self.assertEqual(settings.memory_database, db)
        self.assertNotEqual(settings.memory_database.resolve(), (PROJECT_ROOT / "data" / "jarvis.db").resolve())
        self.assertFalse(settings.execution_enabled)
        store = SQLiteMemoryStore(db)
        chats = store.list_conversations(project_id=None)
        self.assertEqual(chats[0].title, "Yazı tipi denemesi / Font check")
        self.assertEqual(len(chats), 5)
        turns = store.load_conversation_state(chats[0].id).turns
        self.assertEqual([t.role for t in turns], ["user", "assistant", "user", "assistant"])
        self.assertIn("Şu üç kalem önemli", turns[0].text)
        self.assertIn("```python", turns[1].text)
        self.assertIn("9.840 ₺", turns[1].text)
        self.assertGreater(len(turns[3].text), 500)

    def test_seed_refuses_when_the_home_is_not_the_run_folder(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="jarvis-dffonttry-elsewhere-")).resolve()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        done = self.run_seed(elsewhere)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("REFUSED", done.stderr)
        self.assertFalse((elsewhere / ".jarvis").exists())
        self.assertFalse((self.root / "data" / "jarvis.db").exists())


if __name__ == "__main__":
    unittest.main()
