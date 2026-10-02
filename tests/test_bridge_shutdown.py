"""A catalog probe that finishes after the Bridge is gone is never delivered.

Defect (test-speed report, 2026-10-02): after the window closed, the
``jarvis-catalog-*`` threads called ``Bridge._catalog_changed``, which emitted
a signal of a Bridge whose C++ object was already deleted ("Signal source has
been deleted"). ``shutdown()`` only guards a teardown that runs it; a Bridge
that is deleted WITHOUT it (an early return from ``main``, a launch path that
does not tear down) left the flag unset.

A late result may still be saved to the catalog file; it is never delivered to
the UI, nothing blocks on the probe thread, and nothing is raised.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import threading
import time

import shiboken6

from jarvis.ui.app import create_engine, teardown
from jarvis.ui.backend import Backend
from jarvis.ui.bridge import Bridge
from jarvis.ui.preferences import UiPreferences
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import UiTestCase


class LateProbeTests(UiTestCase):
    def blocked_bridge(self):
        """A real (threaded) Bridge whose provider probes wait for ``release``."""
        entered, release = threading.Event(), threading.Event()
        backend = Backend(self.store, self.factory, codex_factory=lambda prompt, model, effort: self.model,
                          codex_state=lambda: {"status": "not_installed", "models": []}, claude_catalog=lambda: [],
                          google_state=lambda: {"id": "google", "vendor": "Google", "name": "Antigravity",
                                                "status": "not_installed", "models": [], "research": False})

        def probe(pid: str) -> dict:
            entered.set()
            release.wait(20)
            return {"id": pid, "vendor": "x", "name": pid, "status": "available", "models": [], "research": False}
        backend.provider_line = probe
        bridge = Bridge(backend, UiPreferences(self.prefs_path), sync=False)
        bridge.refreshProviders(force=True)                                       # what start() does at launch
        self.assertTrue(entered.wait(10), "the catalog probe never started")
        return bridge, release

    def run_late(self, delete) -> list:
        raised = []
        original = threading.excepthook
        threading.excepthook = lambda args: raised.append((args.thread.name if args.thread else "", args.exc_type, str(args.exc_value)))
        self.addCleanup(setattr, threading, "excepthook", original)
        bridge, release = self.blocked_bridge()
        probes = [t for t in threading.enumerate() if t.name.startswith("jarvis-catalog-")]
        self.assertTrue(probes)
        delete(bridge)
        self.assertFalse(shiboken6.isValid(bridge))
        started = time.monotonic()
        release.set()                                    # the probes finish AFTER the Bridge is gone
        for thread in probes:
            thread.join(10)
        self.assertFalse(any(t.is_alive() for t in probes), "a probe thread outlived its result")
        self.assertLess(time.monotonic() - started, 10)
        return raised

    def test_a_late_probe_after_the_real_teardown_is_dropped_quietly(self) -> None:
        def real_teardown(bridge) -> None:
            engine = create_engine(bridge)               # the real engine over this Bridge
            self.assertTrue(engine.rootObjects())
            teardown(bridge, engine)                     # shutdown() first, then the engine, then the Bridge may go
            shiboken6.delete(bridge)
        self.assertEqual([], self.run_late(real_teardown))

    def test_a_late_probe_after_a_bridge_deleted_without_shutdown_is_dropped_quietly(self) -> None:
        # The launch path that never calls shutdown() (main's early return, the
        # test's mini launch): the delivery itself must refuse a deleted Bridge.
        self.assertEqual([], self.run_late(shiboken6.delete))

    def test_the_result_is_still_saved_to_the_catalog_file(self) -> None:
        bridge, release = self.blocked_bridge()
        path = bridge._catalog.path
        bridge.shutdown()
        shiboken6.delete(bridge)
        release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.05)
        self.assertTrue(path.exists(), "a late probe result must still reach the catalog file")
