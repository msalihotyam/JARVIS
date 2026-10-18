"""Static checks of scripts/watchdog.ps1 plus a -DryRun run against a temp root."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "watchdog.ps1"
NOW = 1792320000.0  # injected clock; the test never reads the wall clock
SENTINEL = "wdSentinelTopic42"
POWERSHELL = shutil.which("powershell.exe")


class WatchdogStaticTest(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_ascii_only_for_ps51(self):
        self.assertTrue(self.text.isascii())

    def test_never_kills_deletes_or_touches_repo(self):
        for bad in ("Stop-Process", "Remove-Item", "Start-Process", "Set-ItemProperty",
                    "Register-ScheduledTask", "Unregister-ScheduledTask", "taskkill",
                    "git ", "Invoke-Expression", "New-ItemProperty"):
            self.assertNotIn(bad, self.text, bad)

    def test_only_network_is_ntfy(self):
        self.assertEqual(len(re.findall(r"Invoke-(RestMethod|WebRequest)", self.text)), 1)
        self.assertEqual(re.findall(r"https?://[^'\"\s]+", self.text), ["https://ntfy.sh/"])
        self.assertNotIn("WebClient", self.text)

    def test_topic_never_output(self):
        self.assertNotRegex(self.text, r"(Say|Write-\w+)\s+[^;\n]*\$t(opic)?\b")
        self.assertNotRegex(self.text, r"\$t(opic)?\b[^;\n]*\|\s*(Set-Content|Out-File|Add-Content)")
        self.assertNotRegex(self.text, r"\$state\[[^\]]*\]\s*=\s*\$t(opic)?\b")

    def test_conditions_present(self):
        for needle in ("Start-ScheduledTask -TaskName 'JARVIS Konveyor'", "-gt 300", "-gt 600",
                       "-gt 1800", "-gt 5400", "-lt 20", "$THROTTLE_S = 7200",
                       "watchdog_state.json", "LastBootUpTime", "[switch]$DryRun"):
            self.assertIn(needle, self.text, needle)


@unittest.skipUnless(POWERSHELL, "powershell.exe not available")
class WatchdogDryRunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wd_test_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.conv = self.root / "build" / "conveyor"
        self.conv.mkdir(parents=True)
        (self.root / "build" / "orch").mkdir()

    def put(self, rel, content, age):
        path = self.root / rel
        path.write_text(content, encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))

    def run_dry(self):
        state = self.conv / "watchdog_state.json"
        before = state.read_bytes() if state.exists() else None
        env = dict(os.environ, JARVIS_NTFY_TOPIC=SENTINEL)
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
             "-DryRun", "-Root", str(self.root), "-Now", str(int(NOW)),
             "-ProcessName", "jarvis-no-such-process"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")
        self.assertNotIn(SENTINEL, proc.stdout)
        self.assertEqual(state.read_bytes() if state.exists() else None, before)
        return proc.stdout

    def test_everything_stale(self):
        self.put("build/conveyor/status.json", json.dumps({"outage_hold": True}), 700)
        self.put("build/conveyor/conveyor.lock",
                 json.dumps({"pid": 999999, "heartbeat": NOW - 900}), 900)
        self.put("build/conveyor/pager.json", json.dumps({"outage": {"since": NOW - 2000}}), 10)
        self.put("build/orch/heartbeat", "1 next-wake", 6000)
        out = self.run_dry()
        self.assertIn("would Start-ScheduledTask 'JARVIS Konveyor'", out)
        self.assertIn("would page [conveyor]", out)
        self.assertIn("Kilit bayat (pid 999999, 15 dk)", out)
        self.assertIn("would page [claude]", out)
        self.assertIn("would page [outage]", out)
        self.assertIn("would page [orch]", out)
        self.assertTrue((self.conv / "conveyor.lock").exists())

    def test_fresh_and_throttled(self):
        self.put("build/conveyor/status.json", json.dumps({"outage_hold": False}), 30)
        self.put("build/conveyor/pager.json", "{}", 30)
        self.put("build/orch/heartbeat", "1 next-wake", 60)
        self.put("build/conveyor/watchdog_state.json",
                 json.dumps({"page:claude": NOW - 600}), 600)
        out = self.run_dry()
        self.assertNotIn("Start-ScheduledTask", out)
        for cond in ("conveyor", "outage", "orch", "claude"):
            self.assertNotIn(f"would page [{cond}]", out)
        self.assertIn("throttled claude", out)


if __name__ == "__main__":
    unittest.main()
