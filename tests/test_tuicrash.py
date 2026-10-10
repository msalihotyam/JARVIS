"""TUICRASH: no UI test leaves a Bridge for the cyclic gc to free on another thread.

A Bridge is a reference cycle (its catalog holds its bound methods), so Python frees it
only in a gc pass - on whichever thread allocates then. On a worker thread, ~QTimer
cannot unregister its running timer and the GUI thread's next WM_TIMER is sent to freed
memory: the intermittent 0xC0000005 of the UI units under load.
"""

from __future__ import annotations

import gc
import threading
import unittest

import shiboken6
from PySide6.QtCore import qInstallMessageHandler

from jarvis.ui.bridge import Bridge
from jarvis.ui.preferences import UiPreferences
from tests.test_ui import UiTestCase


class BridgeReleaseTests(unittest.TestCase):
    def test_every_bridge_a_ui_test_makes_is_gone_before_a_worker_thread_gc(self) -> None:
        made: list[Bridge] = []

        class Probe(UiTestCase):
            def runTest(inner) -> None:
                made.append(inner.bridge(sync=False))                                       # the harness path
                made.append(Bridge(inner.backend, UiPreferences(inner.prefs_path), sync=True))   # a direct one

        result = unittest.TestResult()
        Probe().run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(2, len(made))
        self.assertEqual([False, False], [shiboken6.isValid(b) for b in made])

        warnings: list[str] = []
        previous = qInstallMessageHandler(lambda _kind, _context, text: warnings.append(text))
        try:
            made.clear()
            worker = threading.Thread(target=gc.collect, name="tuicrash-gc")
            worker.start()
            worker.join()
        finally:
            qInstallMessageHandler(previous)
        self.assertEqual([], [w for w in warnings if "another thread" in w])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
