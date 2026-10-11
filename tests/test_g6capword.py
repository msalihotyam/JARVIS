"""Gate 6G close follow-up G6CAPWORD (GATE6G review A-8; orchestrator 2026-10-11), in the
MAIN ("space") and the DARK ("dark") theme.

A-8  K3 words for capabilities: the work panel's "Used in this chat" shows every
     capability a connector declares as a plain word (English, tr-TR); an unknown id
     reads "Other capability" ("Diğer yetkinlik": the catalog's capability = yetkinlik,
     test_ui's glossary rule) - never the raw id.
V1/V7 No "Global"/"Genel" scope word for a plain chat: the Focus tile and its peek,
     the chat header's chip name (the Command preset's menu row reads "No project")
     and the delete dialog's identity line say nothing; a project chat keeps its name.

Screenshots: set JARVIS_SHOTS_DIR to save each themed state as a PNG.
Temp stores only: no test opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from jarvis.connectors.browser_cdp import BROWSER_ENABLED_CAPABILITIES
from jarvis.connectors.desktop_uia import DESKTOP_ENABLED_CAPABILITIES
from jarvis.connectors.github_cli import GITHUB_READ_CAPABILITIES
from jarvis.connectors.gmail_cli import GMAIL_ENABLED_CAPABILITIES
from jarvis.contracts import LOCAL_WRITE_CAPABILITY
from jarvis.intent import INTENT_CAPABILITY, NL_CAPABILITIES
from jarvis.memory import SQLiteMemoryStore
from jarvis.ui.i18n import localization
from jarvis.ui.wording import CODE_WORDS, code_word
from tests import test_g6k6 as k6, test_g6w3_work_panel as w3     # the modules: their tests are not collected here
from tests.test_g6k6 import K6Case
from tests.test_ui import _APP, UiTestCase, pump, settled
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401

QML_DIR = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml"
THEMES = ("space", "dark")
GONE = ("Global", "Genel")
#: Every capability id the work panel can show: what a result records is a connector's capability.
KNOWN = sorted({*NL_CAPABILITIES, *BROWSER_ENABLED_CAPABILITIES, *DESKTOP_ENABLED_CAPABILITIES,
                *GITHUB_READ_CAPABILITIES, *GMAIL_ENABLED_CAPABILITIES, LOCAL_WRITE_CAPABILITY,
                INTENT_CAPABILITY, "web_research"})
UNKNOWN = "made_up_capability"


def shot(root, name: str) -> None:
    folder = os.environ.get("JARVIS_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        root.grabWindow().save(str(Path(folder) / f"{name}.png"))


def tr_language(case) -> None:
    localization(_APP).apply("tr-TR")
    case.addCleanup(lambda: localization(_APP).apply("en-US"))


class CapabilityWordTests(UiTestCase):
    def test_a8_every_known_capability_has_a_word_and_no_word_is_stale(self) -> None:
        self.assertEqual(set(KNOWN), set(CODE_WORDS["capability"]))
        english = {c: code_word("capability", c) for c in KNOWN}
        tr_language(self)
        turkish = {c: code_word("capability", c) for c in KNOWN}
        for capability in KNOWN:
            with self.subTest(capability):
                for word in (english[capability], turkish[capability]):
                    self.assertTrue(word)
                    self.assertNotIn("_", word)                       # never the raw id, nor a piece of it
                    self.assertNotIn(capability, word)
                self.assertNotEqual(english[capability], turkish[capability])   # translated, not the fallback
        self.assertEqual(("Dosya okuma", "Dosya yazma", "Web araştırması", "Kod inceleme", "Kod planlama"),
                         tuple(turkish[c] for c in ("file_read", "file_write", "web_research", "code_analysis", "code_planning")))

    def test_a8_an_unknown_capability_reads_a_generic_word(self) -> None:
        self.assertEqual("Other capability", code_word("capability", UNKNOWN))
        self.assertEqual("", code_word("capability", ""))
        self.assertEqual(f"Unrecognized ({UNKNOWN})", code_word("stage", UNKNOWN))   # other vocabularies keep K3's exact form
        tr_language(self)
        self.assertEqual("Diğer yetkinlik", code_word("capability", UNKNOWN))   # the glossary: capability = yetkinlik


class WorkPanelWordTests(w3.Case):
    def lines(self, root) -> list:
        return [i.property("text") for i in self.visible(root, "workPanelCapability")]

    def test_a8_the_panel_shows_words_never_raw_ids_in_either_theme(self) -> None:
        bridge = self.bridge()
        self.assertTrue(bridge.newProjectConversation(self.project.id))
        for n, capability in enumerate([*KNOWN, UNKNOWN]):
            self.result(bridge, f"t-{n}", capability)
        root = self.window(bridge)
        pump(200)
        self.assertEqual([*KNOWN, UNKNOWN], bridge.workPanelCapabilities)
        expected = [code_word("capability", c) for c in KNOWN] + ["Other capability"]
        for theme in THEMES:
            with self.subTest(theme=theme):
                bridge.setTheme(theme)
                pump(150)
                self.assertEqual(theme, root.property("themeName"))
                self.assertEqual(expected, self.lines(root))
                shot(root, f"a8_work_panel_{theme}")
        bridge.setLanguage("tr-TR")
        self.addCleanup(lambda: bridge.setLanguage("en-US"))
        settled(300, lambda: self.assertIn("Dosya okuma", self.lines(root)))      # a live switch rewrites the words
        shown = self.lines(root)
        self.assertEqual(len(KNOWN) + 1, len(shown))
        self.assertEqual("Diğer yetkinlik", shown[-1])
        self.assertEqual([], [t for t in shown if "_" in t or t in KNOWN or t == UNKNOWN])
        shot(root, "a8_work_panel_tr")


class PlainChatScopeTests(K6Case):
    def stage(self):
        store = SQLiteMemoryStore(self.database)
        project = store.create_project("Tez")
        plain = store.create_conversation(title="Düz sohbet")
        own = store.create_conversation(project.id, title="Tez sohbeti")
        bridge = self.bridge()
        bridge.openConversation(plain.id)
        root = self.window(bridge)
        return bridge, root, own

    def gone(self, texts) -> list:
        return [t for t in texts if any(word in t for word in GONE)]

    def test_v1_a_plain_chat_shows_no_scope_word_in_either_theme(self) -> None:
        bridge, root, own = self.stage()
        bridge.setLanguage("tr-TR")
        self.addCleanup(lambda: bridge.setLanguage("en-US"))
        pump(200)
        self.assertEqual("Genel", bridge.conversationProject["name"])            # the bridge's data is unchanged
        for theme in THEMES:
            with self.subTest(theme=theme):
                bridge.setTheme(theme)
                pump(150)
                self.assertEqual(theme, root.property("themeName"))
                self.assertEqual(("", "proje bağlamı yok"), (self.item(root, "railCountFocus").property("text"),
                                                             self.item(root, "railTipFocus").property("text")))
                self.assertEqual("", self.item(root, "chatHeaderScopeName").property("text"))
                self.assertEqual([], self.gone(self.shown_texts(root)))
                self.open_peek(root, "Focus")
                self.assertEqual([], self.gone(self.shown_texts(root)))
                shot(root, f"v1_focus_peek_{theme}")
                self.close_peek(root)
        # A project chat keeps its project's name on the tile and in the header.
        bridge.openConversation(own.id)
        pump(300)
        self.assertEqual(("Tez", "Tez"), (self.item(root, "railCountFocus").property("text"),
                                          self.item(root, "chatHeaderScopeName").property("text")))

    open_peek = k6.PeekTests.open_peek
    close_peek = k6.PeekTests.close_peek

    def test_v1_no_qml_offers_global_as_a_scope_word(self) -> None:
        sources = {p.name: p.read_text(encoding="utf-8") for p in QML_DIR.rglob("*.qml")}
        self.assertEqual([], [n for n, s in sources.items() if 'qsTr("Global")' in s or "Genel" in re.sub(r"//.*", "", s)])
        for name in ("ChatHeader.qml", "ConversationDeleteDialog.qml", "SummaryRail.qml", "SummaryPeek.qml", "WorkPanel.qml"):
            with self.subTest(name):
                self.assertEqual([], re.findall(r'"[^"\n]*\bGlobal\b[^"\n]*"', sources[name]))
        self.assertIn('label: qsTr("No project")', sources["ChatHeader.qml"])          # the Command preset's menu row


if __name__ == "__main__":
    unittest.main()
