"""The last-known-good provider catalog (jarvis.model_catalog), with fake probes only."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import model_catalog
from jarvis.model_catalog import FAILURE_RETRY_SECONDS, TTL_SECONDS, ProviderCatalog, load, save

CLAUDE = {"id": "claude", "vendor": "Anthropic", "name": "Claude", "status": "available", "evidence": "claude_auth_status:oauth_subscription",
          "models": [{"id": "default", "name": "Default", "resolved": "claude-opus-5[1m]", "efforts": ["low", "high"]}], "research": True}
CODEX = {"id": "codex", "vendor": "OpenAI", "name": "Codex", "status": "available", "plan": "plus",
         "models": [{"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna", "efforts": ["low"], "default_effort": "low"}],
         "quota": {"primary": {"used_percent": 4.0, "window_minutes": 300, "resets_at": 1789492870}, "checked_at": 100}}
GOOGLE = {"id": "google", "vendor": "Google", "name": "Antigravity", "status": "not_installed", "models": [], "research": False}
GOOD = {"claude": CLAUDE, "codex": CODEX, "google": GOOGLE}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "provider-catalog.json"
        self.clock = Clock()
        self.lines = dict(GOOD)
        self.calls: list[str] = []

    def probe(self, pid: str) -> dict:
        self.calls.append(pid)
        line = self.lines[pid]
        if isinstance(line, Exception):
            raise line
        return json.loads(json.dumps(line))

    def catalog(self, **kwargs) -> ProviderCatalog:
        return ProviderCatalog(self.path, kwargs.pop("probe", self.probe), sync=kwargs.pop("sync", True), clock=self.clock, **kwargs)

    def test_a_valid_cache_is_served_at_once_with_no_probe(self) -> None:
        self.catalog().refresh(force=True)
        self.calls.clear()
        again = self.catalog()
        self.assertEqual(["claude", "codex", "google"], [line["id"] for line in again.snapshot()])
        self.assertEqual(CLAUDE["models"], again.snapshot()[0]["models"])
        self.assertEqual([], self.calls)

    def test_within_the_ttl_nothing_is_probed_after_it_everything_due_is_and_force_bypasses_it(self) -> None:
        catalog = self.catalog()
        self.assertEqual(["claude", "codex", "google"], catalog.refresh())         # nothing known: everything is due
        for _ in range(10):
            self.assertEqual([], catalog.refresh())
        self.clock.now += TTL_SECONDS - 1
        self.assertEqual([], catalog.refresh())
        self.assertEqual(["claude", "codex", "google"], catalog.refresh(force=True))
        self.clock.now += TTL_SECONDS
        self.assertEqual(["claude", "codex", "google"], catalog.refresh())
        self.assertEqual({"claude": 3, "codex": 3, "google": 3}, catalog.probes_started)

    def test_requests_while_a_refresh_runs_collapse_to_one_even_when_forced(self) -> None:
        gate, entered = threading.Event(), threading.Event()

        def slow(pid):
            entered.set()
            gate.wait(5)
            return self.probe(pid)
        catalog = self.catalog(probe=slow, sync=False)
        self.assertEqual(["claude", "codex", "google"], catalog.refresh(force=True))
        entered.wait(5)
        for _ in range(10):
            self.assertEqual([], catalog.refresh())
            self.assertEqual([], catalog.refresh(force=True))
        gate.set()
        deadline = time.monotonic() + 5
        while catalog.busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(catalog.busy())
        self.assertEqual({"claude": 1, "codex": 1, "google": 1}, catalog.probes_started)
        self.assertEqual(3, len(self.calls))

    def test_claude_and_codex_are_checked_at_the_same_time(self) -> None:
        def slow(pid):
            time.sleep(0.5)
            return self.probe(pid)
        catalog = self.catalog(probe=slow, sync=False)
        started = time.monotonic()
        catalog.refresh(force=True)
        while catalog.busy():
            time.sleep(0.01)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.9, "the provider chains ran one after another")        # serial would be >= 1.5 s
        self.assertEqual(["claude", "codex", "google"], [line["id"] for line in catalog.snapshot()])

    def test_a_failed_provider_keeps_its_last_good_line_and_never_touches_another(self) -> None:
        updates = []
        catalog = self.catalog(on_update=lambda: updates.append(1))
        catalog.refresh(force=True)
        good = catalog.snapshot()
        on_disk = self.path.read_text(encoding="utf-8")
        for failure in (OSError("probe crashed"), {"id": "claude", "status": "unavailable", "models": []}, "garbage", None):
            self.lines["claude"] = failure
            self.lines["codex"] = {**CODEX, "plan": "pro"}
            updates.clear()
            catalog.refresh(force=True)
            snapshot = catalog.snapshot()
            with self.subTest(failure=failure):
                self.assertEqual(good[0], snapshot[0])                               # Claude: last known good
                self.assertEqual("pro", snapshot[1]["plan"])                        # Codex: its own fresh result
        self.assertEqual(json.loads(on_disk)["providers"]["claude"]["entry"],
                         json.loads(self.path.read_text(encoding="utf-8"))["providers"]["claude"]["entry"])
        # A failed provider is retried soon - not on every open, not only after the full TTL.
        self.lines["claude"] = OSError("still down")
        self.calls.clear()
        self.assertEqual([], catalog.refresh())
        self.clock.now += FAILURE_RETRY_SECONDS
        self.assertEqual(["claude"], catalog.refresh())

    def test_an_empty_model_list_never_blanks_a_good_one(self) -> None:
        catalog = self.catalog()
        catalog.refresh(force=True)
        self.lines["claude"] = {**CLAUDE, "models": []}                    # the route answered, the model list did not
        self.lines["codex"] = {**CODEX, "models": [], "quota": {}}
        catalog.refresh(force=True)
        claude, codex, _ = catalog.snapshot()
        self.assertEqual((CLAUDE["models"], CODEX["models"], CODEX["quota"]), (claude["models"], codex["models"], codex["quota"]))
        stored = load(self.path)
        self.assertEqual(CLAUDE["models"], stored["claude"]["entry"]["models"])
        # A real change of state is shown: signed out is not a probe failure.
        self.lines["claude"] = {**CLAUDE, "status": "not_logged_in", "models": [], "research": False}
        catalog.refresh(force=True)
        self.assertEqual("not_logged_in", catalog.snapshot()[0]["status"])

    def test_a_corrupt_or_foreign_cache_is_ignored_and_rebuilt(self) -> None:
        for content in (b"{not json", b"\xff\xfe\x00", json.dumps({"version": 99, "providers": {}}).encode(),
                        json.dumps({"version": 1, "providers": {"claude": {"entry": {"id": "codex", "status": "available"},
                                                                          "checked_at": 1}}}).encode(),
                        json.dumps({"version": 1, "providers": {"claude": {"entry": {"id": "claude"}, "checked_at": 1}}}).encode(),
                        json.dumps([1, 2]).encode()):
            with self.subTest(content=content[:30]):
                self.path.write_bytes(content)
                catalog = self.catalog()
                self.assertEqual([], catalog.snapshot())
                self.assertEqual(["claude", "codex", "google"], catalog.refresh())
                self.assertEqual(3, len(load(self.path)))

    def test_the_file_is_written_atomically_and_holds_display_data_only(self) -> None:
        self.lines["claude"] = {**CLAUDE, "email": "person@example.com", "organization": "org-123", "token": "sk-x",
                                "models": [{**CLAUDE["models"][0], "account": "person@example.com"}]}
        self.lines["codex"] = {**CODEX, "models": [{**CODEX["models"][0], "verified": {"state": "usable"}, "successor": "gpt-6"}]}
        catalog = self.catalog()
        catalog.refresh(force=True)
        text = self.path.read_text(encoding="utf-8")
        for secret in ("person@example.com", "org-123", "sk-x", "verified", "successor", "retired"):
            self.assertNotIn(secret, text)
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))
        before = self.path.read_bytes()
        with patch.object(model_catalog.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(save(self.path, {"claude": {"entry": {"id": "claude", "status": "unavailable"}, "checked_at": 1.0}}))
        self.assertEqual(before, self.path.read_bytes())                              # the old file, whole
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))
        self.assertEqual(load(self.path)["codex"]["entry"]["plan"], "plus")


if __name__ == "__main__":
    unittest.main()
