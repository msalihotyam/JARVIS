"""Gate 6G close leftovers G6POL2 (orchestrator 2026-10-11), after G6DARK and G6CAPWORD.

* The summary rail's Tasks tile and its peek name an untitled task by its capability as a
  K3 word (i18n.word("capability", id)), in en-US and tr-TR; an unknown id reads
  "Other capability" / "Diğer yetkinlik" - never the raw id. Display only: the bridge's
  task data is unchanged.
* (The bell's AA contrast after MenuRow's tail ink change is test_g6dark's
  test_a5_the_unread_bell_reads_in_both_themes.)

Temp stores only: no test opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import unittest

from tests import test_g6k6 as k6
from tests.test_g6k6 import K6Case
from tests.test_ui import settled
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

KNOWN, UNKNOWN = "code_analysis", "made_up_capability"
#: language -> (the known id's word, the unknown id's word)
WORDS = {"en-US": ("Code analysis", "Other capability"), "tr-TR": ("Kod inceleme", "Diğer yetkinlik")}


class SummaryCapabilityWordTests(K6Case):
    open_peek = k6.PeekTests.open_peek
    close_peek = k6.PeekTests.close_peek

    def task(self, bridge, capability: str, status: str) -> None:
        bridge._session_id = bridge._session_id or "s-1"
        bridge._task = {"task_id": "t-pol2", "status": status, "capability": capability}   # no title: the fallback names it
        bridge.stateChanged.emit()

    def texts(self, item) -> list:
        found, stack = [], [item]
        while stack:
            node = stack.pop()
            if node.isVisible() and isinstance(node.property("text"), str):
                found.append(node.property("text"))
            stack.extend(node.childItems())
        return found

    def each(self, bridge, check) -> None:
        self.addCleanup(lambda: bridge.setLanguage("en-US"))
        for language, words in WORDS.items():
            bridge.setLanguage(language)
            for capability, word in zip((KNOWN, UNKNOWN), words):
                with self.subTest(language=language, capability=capability):
                    check(capability, word)

    def test_the_tasks_tile_names_an_untitled_task_by_its_word(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        line = lambda: self.item(root, "railLineTasks").property("text")     # noqa: E731
        for status in ("running", "completed"):                              # the live task, then the last finished one
            def check(capability, word, status=status):
                self.task(bridge, capability, status)
                settled(300, lambda: self.assertEqual(word, line()))
                self.assertEqual(capability, bridge.task["capability"])      # the data keeps the exact id
            self.each(bridge, check)

    def test_the_tasks_peek_names_an_untitled_live_task_by_its_word(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)

        def check(capability, word):
            self.task(bridge, capability, "running")
            self.open_peek(root, "Tasks")
            settled(300, lambda: self.assertIn(word, [t for row in self.named(root, "summaryPeekRow") for t in self.texts(row)]))
            self.assertEqual([], [t for t in self.texts(self.item(root, "summaryPeek")) if capability in t])
            self.close_peek(root)
        self.each(bridge, check)


if __name__ == "__main__":
    unittest.main()
