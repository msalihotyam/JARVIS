"""V2.1 cleanup V21UI1 (Salih 2026-10-11 "temizlik listesi"), after G6POL2's LEFT RAW.

* The feed's task result label and the workflow well's Capability row name the capability
  as a K3 word (i18n.word("capability", id)), in en-US and tr-TR; an unknown id reads
  "Other capability" / "Diğer yetkinlik" - never the raw id. Display only: the data keeps
  the exact id. (The K3 approval/outcome cards keep raw ids: authority evidence.)
* D-7: the top bar bell draws its glyph through the shared Icon.qml / icons.js set (K1),
  18 px on Theme.textLabel in both themes, as the old hand-drawn path was.

Temp stores only: no test opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import unittest

from PySide6.QtGui import QColor

from tests.test_g6k6 import K6Case
from tests.test_ui import settled
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

KNOWN, UNKNOWN = "code_analysis", "made_up_capability"
DESTINATION = "anthropic/claude-code"
#: language -> (the known id's word, the unknown id's word)
WORDS = {"en-US": ("Code analysis", "Other capability"), "tr-TR": ("Kod inceleme", "Diğer yetkinlik")}
#: theme -> Theme.textLabel, the bell's idle ink before and after D-7
LABEL_INK = {"space": "#b8c6d4", "dark": "#b2c1cb"}


class CapabilityWordTests(K6Case):
    def languages(self, bridge):
        self.addCleanup(lambda: bridge.setLanguage("en-US"))
        for language, words in WORDS.items():
            bridge.setLanguage(language)
            yield language, dict(zip((KNOWN, UNKNOWN), words))

    def test_the_feed_result_label_names_the_capability_by_its_word(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        for capability in (KNOWN, UNKNOWN):
            bridge._say("result", f"findings of {capability}", capability=capability, destination=DESTINATION)
        self.assertEqual([KNOWN, UNKNOWN], [e["capability"] for e in bridge.feed if e["kind"] == "result"])

        def labels():
            return [str(i.property("text")) for i in self.all_items(root) if i.isVisible()
                    and isinstance(i.property("text"), str) and "  ·  " + DESTINATION in str(i.property("text"))]
        for language, words in self.languages(bridge):     # live: the same rows re-read in each language
            with self.subTest(language=language):
                settled(400, lambda: self.assertEqual(
                    [True, True], [any("  ·  " + words[c] + "  ·  " in t for t in labels()) for c in (KNOWN, UNKNOWN)]))
                self.assertEqual(2, len(labels()))
                self.assertEqual([], [t for t in labels() if KNOWN in t or UNKNOWN in t])

    def test_the_workflow_well_names_the_capability_by_its_word(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        value = lambda: self.item(root, "workflowValue_Capability").property("text")     # noqa: E731
        bridge._session_id = bridge._session_id or "s-1"
        for language, words in self.languages(bridge):
            for capability, word in words.items():
                with self.subTest(language=language, capability=capability):
                    bridge._task = {"task_id": "t-v21ui1", "status": "running", "capability": capability}
                    bridge.stateChanged.emit()
                    settled(300, lambda: self.assertEqual(word, value()))
                    self.assertEqual(capability, bridge.task["capability"])      # the data keeps the exact id


class BellIconTests(K6Case):
    def test_the_bell_glyph_comes_from_the_shared_icon_set_in_both_themes(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        button = self.item(root, "unreadBellButton")
        icons = [c for c in button.childItems() if c.property("name") == "bell"]
        self.assertEqual(1, len(icons))
        icon = icons[0]
        self.assertTrue(icon.property("known"))          # icons.js has the glyph: no crossed-box fallback
        self.assertEqual("bell", button.property("icon"))
        self.addCleanup(lambda: bridge.setTheme("space"))
        for theme, ink in LABEL_INK.items():
            with self.subTest(theme=theme):
                bridge.setTheme(theme)
                settled(1200, lambda: self.assertEqual(theme, root.property("themeName")))
                self.assertTrue(icon.isVisible())
                self.assertEqual((18.0, 18.0, 18.0), (icon.property("size"), icon.width(), icon.height()))
                settled(1200, lambda: self.assertEqual(QColor(ink).name(), QColor(icon.property("tint")).name()))


if __name__ == "__main__":
    unittest.main()
