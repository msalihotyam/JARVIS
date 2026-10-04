"""V2 Projects Gate 6E, slice C1: copy, plurals and catalogs (Salih 2026-10-03).

A. English counts: 0 / 1 / n for messages (delete dialog), turns (attach
   dialog), tasks (History sessions) and the Bridge's activity notes; the
   Turkish catalog has one form ("1 mesaj", "1 görev").
B. One untitled name: "New Chat" | "Yeni Sohbet" in every list, menu and
   dialog; the actions stay sentence case ("Yeni sohbet").
C. One word: no "konuşma" in any Turkish string of the 6A-6D surfaces, the
   Composer, the command palette or the Bridge; the headings read SOHBETLER,
   ARŞİVLENMİŞ SOHBETLER and ARŞİVLENMİŞ PROJELER.

Slice C2 - the shared parts the surfaces adopt in C3-C5 (exercised on their
own in a harness built in the real window's engine):
K. Theme tokens: pillHeight, rowHeight, menuRowHeight, scrimOpacity, tipDelay.
L-M. HoverTip: waits for tipDelay; the whole name, wrapped, never elided, at
   most 480 px; full opacity under a disabled, dimmed owner.
N-Q. JarvisDialog: black scrim at .56; inside the window at every size with an
   oversized body, footer visible; Esc / Tab / initial focus / focus return /
   Enter rules; confirm only while fully open; motion on its tokens (judged on
   animation frames), none at all with ambient effects off.
R. Segmented: the focus ring on Tab focus.

C1 tests are strings only. No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import re
import time
import unittest
from pathlib import Path

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPoint, QPointF, QRectF, Qt
from PySide6.QtTest import QTest

from jarvis.conversation import append_turn
from jarvis.ui.i18n import CATALOG_DIR, load_catalog, localization, render, tx
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP, UiTestCase, pump

SURFACES = ("TopBar", "HistoryPage", "ProjectsPage", "AttachDialog", "ConversationDeleteDialog", "Composer",
            "CommandPalette", "Bridge", "Sidebar", "ConversationMenu", "ChatHeader")


def say(memory, conversation_id: str, text: str) -> None:
    memory.update_conversation_state(conversation_id, lambda s: append_turn(
        s, role="user", kind="user", text=text, message=text, key=text)[0])


class WindowCase(ProjectsUiCase):
    def press(self, root, item) -> None:
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, center)
        pump(260)

    def visible(self, root, name) -> list:
        return [i for i in self.all_items(root, name) if i.isVisible()]

    def tr(self, bridge) -> None:
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        bridge.setLanguage("tr-TR"); pump(300)

    def motion(self, root, popup) -> list:
        """(time, visible, opacity) of ``popup`` at every animation frame of the window."""
        samples = []
        root.afterAnimating.connect(lambda: samples.append(
            (time.monotonic(), popup.property("visible"), popup.property("opacity"))))
        return samples

    def assert_motion_within_token_plus_one_frame(self, samples, called_at, mark, opening, token_ms) -> None:
        """The end state (opening: shown at full opacity; closing: hidden or
        transparent) is on screen within the token plus one frame of the
        animation itself: from the first frame that shows motion to the first
        frame that shows the end state takes at most the token plus the longest
        gap between two frames that moved. Judged on the animation's own frames,
        not a wall-clock budget - a starved loop only makes fewer, longer
        frames - and never before the token has run since the call."""
        done = (lambda s: s[1] and s[2] == 1.0) if opening else (lambda s: not s[1] or s[2] == 0.0)
        settle(lambda: any(done(sample) for sample in samples[mark:]))
        window = samples[mark:]
        first = next(i for i, sample in enumerate(window) if done(sample))
        moved = [i for i in range(1, first + 1) if window[i][1:] != window[i - 1][1:]]
        self.assertTrue(moved, "no frame showed the motion")
        gap = max([window[b][0] - window[a][0] for a, b in zip(moved, moved[1:])] or [1 / 60])
        self.assertLessEqual(window[first][0] - window[moved[0]][0], token_ms / 1000 + gap, (token_ms, gap))
        self.assertGreaterEqual(window[first][0] - called_at, token_ms / 1000 - 0.03)


class CountTests(WindowCase):
    def test_a_delete_dialog_counts_messages_in_english(self) -> None:
        store = self.db()
        ids = {}
        for turns in (0, 1, 2):
            conversation = store.create_conversation()
            for n in range(turns):
                say(store, conversation.id, f"m{turns}{n}")
            store.archive_conversation(conversation.id)
            ids[turns] = conversation.id
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("history"); pump(400)
        order = {r["id"]: i for i, r in enumerate(bridge.archivedConversations)}
        for turns, expected in ((0, "0 messages"), (1, "1 message"), (2, "2 messages")):
            self.press(root, self.visible(root, "historyDelete")[order[ids[turns]]])
            identity = self.visible(root, "deleteIdentity")[0].property("text")
            self.assertTrue(identity.startswith(f"«New Chat» · {expected} · created "), identity)
            self.press(root, self.visible(root, "deleteCancel")[0])

    def test_b_attach_dialog_counts_turns_in_english_and_messages_in_turkish(self) -> None:
        store = self.db()
        store.create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        conversation_id = bridge.conversationId
        for turns, expected in ((0, "0 turns"), (1, "1 turn"), (2, "2 turns")):
            if turns:
                say(store, conversation_id, f"t{turns}")
            self.attach_identity_is(root, f'"New Chat" · {expected} · created ')
        self.tr(bridge)
        self.attach_identity_is(root, '"Yeni Sohbet" · 2 mesaj · oluşturuldu ')
        say(store, conversation_id, "t3")                    # 3 messages: the same single Turkish form
        self.attach_identity_is(root, '"Yeni Sohbet" · 3 mesaj · oluşturuldu ')

    def attach_identity_is(self, root, prefix: str) -> None:
        pump(200)                                       # the previous dialog's exit motion ends first
        self.steady(root, "chatHeaderAttach")
        self.click(root, "chatHeaderAttach"); pump(300)
        item = [i for i in self.all_items(root, "attachProject") if i.property("label") == "Evim"][0]
        self.press(root, item)
        identity = self.item(root, "attachIdentity").property("text")
        self.assertTrue(identity.startswith(prefix), identity)
        self.press(root, self.visible(root, "attachCancel")[0])

    def test_c_history_sessions_count_tasks(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._sessions = [{"session_id": f"s{n}", "title": f"Oturum {n}", "updated_at": 1_700_000_000.0 + n,
                             "task_count": n, "active_task_id": "", "readable": True} for n in (0, 1, 2)]
        bridge.dataChanged.emit()
        bridge.setPage("history"); pump(400)
        metas = lambda: sorted(t.split("  ·  ")[1] for t in self.texts(root, "historySessionMeta"))  # noqa: E731
        self.assertEqual(["0 tasks", "1 task", "2 tasks"], metas())
        self.tr(bridge)
        self.assertEqual(["0 görev", "1 görev", "2 görev"], metas())

    def test_d_the_activity_notes_count_turns(self) -> None:
        store = self.db()
        bridge = self.bridge()
        notes = lambda: [n["text"] for n in bridge.activity]  # noqa: E731
        for turns, expected in ((0, "(0 turns)"), (1, "(1 turn)"), (2, "(2 turns)")):
            other = store.create_conversation()
            for n in range(turns):
                say(store, other.id, f"a{turns}{n}")
            self.assertTrue(bridge.openConversation(other.id))
            self.assertIn(f"Opened a conversation {expected}", notes())
        self.assertFalse([n for n in notes() if "(1 turns)" in n or "(0 turn)" in n])
        # The Turkish catalog has one form, and the singular source is its own entry.
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        localization(_APP).apply("tr-TR")
        self.assertEqual(["Bir sohbet açıldı (0 mesaj)", "Bir sohbet açıldı (1 mesaj)", "Bir sohbet açıldı (2 mesaj)"],
                         [render(("Bridge", "Opened a conversation (1 turn)" if n == 1 else "Opened a conversation (%1 turns)", (n,)))
                          for n in (0, 1, 2)])
        self.assertEqual(["Önceki sohbet sürdürülüyor (1 mesaj)", "Önceki sohbet sürdürülüyor (4 mesaj)"],
                         [render(("Bridge", "Continuing the previous conversation (1 turn)" if n == 1
                                  else "Continuing the previous conversation (%1 turns)", (n,))) for n in (1, 4)])

    def test_e_continuing_the_previous_conversation_counts_turns(self) -> None:
        store = self.db()
        previous = store.create_conversation()
        say(store, previous.id, "tek")
        one = self.bridge()
        self.assertIn("Continuing the previous conversation (1 turn)", [n["text"] for n in one.activity])
        say(store, previous.id, "iki")
        two = self.bridge()
        self.assertIn("Continuing the previous conversation (2 turns)", [n["text"] for n in two.activity])


class UntitledNameTests(WindowCase):
    def test_f_one_untitled_name_in_every_list_and_dialog(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        loose = store.create_conversation()
        store.archive_conversation(loose.id)
        inside, archived = store.create_conversation(project.id), store.create_conversation(project.id)
        store.archive_conversation(archived.id)
        bridge = self.bridge()
        root = self.window(bridge)

        def names() -> list:
            bridge.refreshProjects(); bridge._refresh_conversations(); bridge.showProject(project.id); pump(300)
            return [r["title"] for r in (*bridge.archivedConversations, *bridge.projectConversations,
                                         *bridge.projectArchivedConversations)]
        self.assertEqual(["New Chat"] * 3, names())
        bridge.setPage("home"); pump(300)                                      # the header and sidebar live on home
        self.assertEqual("New Chat", self.item(root, "chatHeaderTitle").property("text"))
        self.assertEqual("New Chat", self.open_row(root).property("title"))
        self.tr(bridge)
        self.assertEqual(["Yeni Sohbet"] * 3, names())
        bridge.setPage("home"); pump(300)
        self.assertEqual("Yeni Sohbet", self.item(root, "chatHeaderTitle").property("text"))
        self.assertEqual("Yeni Sohbet", self.open_row(root).property("title"))
        self.assertEqual(inside.id, bridge.projectConversations[0]["id"])
        self.assertEqual("Yeni Sohbet", tx("AttachDialog", "New Chat"))
        self.assertEqual("Yeni Sohbet", tx("ConversationDeleteDialog", "New Chat"))

    def open_row(self, root):
        return [i for i in self.all_items(root, "sidebarConversation") if i.isVisible() and i.property("current")][0]

    def test_g_the_actions_stay_sentence_case(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("Evim içinde yeni sohbet", tx("Sidebar", "New conversation in %1").replace("%1", "Evim"))
        self.assertEqual("Yeni sohbet", tx("HistoryPage", "New conversation"))
        self.assertEqual("Yeni sohbet", tx("CommandPalette", "New conversation"))
        self.assertEqual("Evim içinde yeni sohbet", tx("ProjectsPage", "New conversation in %1").replace("%1", "Evim"))

    def test_h_the_archived_headings_name_what_they_hold(self) -> None:
        store = self.db()
        project = store.create_project("Evim")
        other = store.create_project("Açık")
        kept = store.create_conversation(other.id, title="Eski")
        store.archive_conversation(kept.id)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.archiveProject(project.id); pump(200)
        self.tr(bridge)
        bridge.showProject(other.id); bridge.setPage("projects"); pump(400)
        heading = lambda name: self.item(root, name).property("text")  # noqa: E731
        self.assertEqual("ARŞİVLENMİŞ PROJELER", heading("projectsArchivedHeading"))
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", heading("projectArchivedConversationsHeading"))
        bridge.setPage("history"); pump(400)
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", heading("historyArchivedHeading"))


class CatalogTests(UiTestCase):
    def test_i_no_konusma_in_the_turkish_catalog_of_these_surfaces(self) -> None:
        table = load_catalog(CATALOG_DIR / "jarvis_tr-TR.ts")
        found = {key: value for key, value in table.items() if key[0] in SURFACES and re.search("konuşma|KONUŞMA", value, re.I)}
        self.assertEqual({}, found)

    def test_j_the_copy_rules(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("SOHBETLER", tx("HistoryPage", "CONVERSATIONS"))
        self.assertEqual("SOHBETLER", tx("ProjectsPage", "CONVERSATIONS"))
        self.assertEqual("SOHBETLER", tx("Sidebar", "CONVERSATIONS"))
        self.assertEqual("ARŞİVLENMİŞ SOHBETLER", tx("ProjectsPage", "ARCHIVED CONVERSATIONS"))
        self.assertEqual("ARŞİVLENMİŞ PROJELER", tx("ProjectsPage", "ARCHIVED PROJECTS"))
        self.assertEqual("Bu projede henüz sohbet yok.", tx("ProjectsPage", "No conversation in this project yet."))
        self.assertEqual("Sohbetlerini görmek için bir proje seçin.", tx("ProjectsPage", "Choose a project to see its conversations."))
        self.assertEqual("Salt okunur: bu sohbet arşivlenmiş %1 projesine ait. Devam etmek için projeyi arşivden çıkarın.",
                         tx("Composer", "Read-only: this conversation belongs to the archived project %1. Unarchive the project to continue it."))
        self.assertEqual("Yeni sohbet", tx("Bridge", "New conversation"))      # the activity note
        self.assertEqual("Yeni Sohbet", tx("Bridge", "New Chat"))              # the untitled NAME: its own source
        for source, expected in (("1 message", "1 mesaj"), ("%1 messages", "%1 mesaj")):
            self.assertEqual(expected, tx("ConversationDeleteDialog", source))
        for source, expected in (("1 turn", "1 mesaj"), ("%1 turns", "%1 mesaj")):
            self.assertEqual(expected, tx("AttachDialog", source))
        self.assertEqual("1 görev", tx("HistoryPage", "1 task"))
        self.assertEqual("%1 görev", tx("HistoryPage", "%1 tasks"))


# -- slice C2: the shared parts (Theme tokens, HoverTip, JarvisDialog, Segmented) --

COMPONENTS = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml" / "components"
LONG_NAME = ("Kat malikleri toplantısı için hazırlanacak belgeler, aidat hesapları, tadilat izinleri ve "
             "yönetim planı özeti")                                              # 109 characters
SIZES = ((1100, 700), (1280, 760), (1920, 1080))


def attach_dialog(root):
    """The "Projeye bağla" dialog, by its type: the composer's file picker has
    the objectName "attachDialog" too (Gate 6F: the lookup is unambiguous)."""
    found = [d for d in root.findChildren(QObject, "attachDialog") if d.metaObject().className().startswith("AttachDialog")]
    assert len(found) == 1, [d.metaObject().className() for d in root.findChildren(QObject, "attachDialog")]
    return found[0]
# Built in the real window's engine (its Theme, i18n and imports); no surface
# adopts these parts before C3-C5, so they are exercised here on their own.
HARNESS = """
import QtQuick
import QtQuick.Controls.Basic
import "../theme"
Item {
    anchors.fill: parent; z: 10000                  // above the window's own full-size layers
    property int confirmed: 0
    property int lines: 2
    ActionButton { id: before; objectName: "h6eBefore"; label: "Before"; x: 40; y: 300
                   onClicked: dialog.openFrom(before) }
    Segmented { objectName: "h6eSegmented"; x: 160; y: 300; value: "a"
                options: [{ value: "a", label: "A" }, { value: "b", label: "B" }] }
    Rectangle { id: owner; objectName: "h6eOwner"; x: 40; y: 360; width: 160; height: 30
                enabled: false; opacity: 0.45; color: "#334455" }
    HoverTip { objectName: "h6eSiblingTip"; target: owner; text: "Wait for the current run to finish." }
    Rectangle { objectName: "h6eNamed"; x: 40; y: 420; width: 160; height: 30; color: "#334455"
                MouseArea { objectName: "h6eNamedArea"; anchors.fill: parent; hoverEnabled: true }
                HoverTip { objectName: "h6eChildTip"; text: "%s" } }
    JarvisDialog {
        id: dialog
        objectName: "h6eDialog"
        label: "Delete permanently"; title: "Delete this conversation permanently?"; danger: true
        initialFocus: cancel; confirmItem: confirm; enterGuard: [guarded]
        Repeater { model: parent ? lines : 0
                   Text { objectName: "h6eLine"; text: "Line " + index; height: 30; color: Theme.text; font: Theme.body } }
        TextField { id: field; objectName: "h6eField"; width: 220 }
        TextField { id: guarded; objectName: "h6eGuarded"; width: 220 }
        buttons: [
            ActionButton { id: cancel; objectName: "h6eCancel"; label: "Cancel"; onClicked: dialog.close() },
            ActionButton { id: confirm; objectName: "h6eConfirm"; label: "Delete permanently"; danger: true; primary: true
                           onClicked: { confirmed += 1; dialog.close() } }
        ]
    }
}
""" % LONG_NAME


def settle(condition, timeout: float = 5.0) -> None:
    """Turn the loop until the asserted condition holds (bounded), not for a fixed time."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        pump(10)


class SharedPartsCase(WindowCase):
    def harness(self, bridge, size=(1920, 1080)):
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent
        root = self.window(bridge)
        root.resize(*size); pump(300)
        component = QQmlComponent(self.engine)
        component.setData(HARNESS.encode("utf-8"), QUrl.fromLocalFile(str(COMPONENTS / "Harness6e.qml")))
        self.assertFalse(component.isError(), component.errorString())
        harness = component.beginCreate(self.engine.rootContext())
        harness.setParentItem(root.contentItem())
        harness.setParent(root.contentItem())                     # owned by the window, deleted with it
        component.completeCreate()
        pump(100)
        self.root, self.harness_item = root, harness
        return root, harness

    def obj(self, name):
        found = self.harness_item.findChild(QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def scene_rect(self, item) -> QRectF:
        return self.rect(item)

    def hover(self, item) -> None:
        QTest.mouseMove(self.root, item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint())
        pump(20)

    def focus(self):
        return self.root.activeFocusItem()

    def inside(self, item, ancestor) -> bool:
        while item is not None:
            if item == ancestor:
                return True
            item = item.parentItem()
        return False

    def scrims(self) -> list:
        return [i for i in self.all_items(self.root, "jarvisDialogScrim") if i.isVisible()]


class TokenTests(UiTestCase):
    def test_k_the_shared_tokens(self) -> None:
        bridge = self.bridge()
        root = self.load(bridge)
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent
        component = QQmlComponent(self.engine)
        component.setData(b'import QtQuick\nimport "../theme"\nQtObject { readonly property var t: '
                          b'[Theme.pillHeight, Theme.rowHeight, Theme.menuRowHeight, Theme.scrimOpacity, Theme.tipDelay] }',
                          QUrl.fromLocalFile(str(COMPONENTS / "Tokens6e.qml")))
        probe = component.create(self.engine.rootContext())
        probe.setParent(root)                                      # owned by the window, deleted with it
        self.assertEqual([30, 40, 36, 0.56, 500], [round(v, 2) for v in probe.property("t").toVariant()])


class HoverTipTests(SharedPartsCase):
    def test_l_the_tip_waits_for_its_delay_then_shows_the_whole_name(self) -> None:
        root, _ = self.harness(self.bridge(), (1100, 700))
        tip = self.obj("h6eChildTip")
        started = time.monotonic()
        self.hover(self.obj("h6eNamed"))
        pump(150)
        if time.monotonic() - started < 0.45:                  # still inside the 500 ms delay
            self.assertFalse(tip.property("shown"))
        settle(lambda: tip.property("shown"))
        self.assertTrue(tip.property("shown"))
        self.assertGreaterEqual(time.monotonic() - started, 0.5 - 0.02)
        self.assertTrue(self.obj("h6eNamedArea").property("containsMouse"))     # the owner's own hover is shared
        text = self.visible(root, "hoverTipText")[0]
        popup = text.parentItem()
        self.assertEqual(LONG_NAME, text.property("text"))
        self.assertFalse(text.property("truncated"))           # nothing elided or cut
        self.assertIn(text.property("lineCount"), (2, 3))      # wraps, within 3 lines
        self.assertLessEqual(popup.width(), 480)
        self.assertGreater(popup.width(), 400)                 # a long name uses the room before wrapping
        box = self.rect(popup)
        self.assertTrue(QRectF(0, 0, root.width(), root.height()).contains(box), box)

    def test_m_a_disabled_owners_tip_is_at_full_opacity(self) -> None:
        root, _ = self.harness(self.bridge())
        owner, tip = self.obj("h6eOwner"), self.obj("h6eSiblingTip")
        self.assertFalse(owner.isEnabled())
        self.hover(owner)
        settle(lambda: tip.property("shown"))
        self.assertTrue(tip.property("shown"))
        text = self.visible(root, "hoverTipText")[0]
        opacity, item = 1.0, text
        while item is not None:                                # what the eye gets: every ancestor's opacity
            opacity *= item.opacity()
            item = item.parentItem()
        self.assertEqual(1.0, opacity)
        self.assertEqual(1, text.property("lineCount"))        # a short reason: one line, its own width
        self.assertLess(text.parentItem().width(), 480)
        QTest.mouseMove(root, QPoint(5, root.height() - 5)); pump(100)
        self.assertFalse(tip.property("shown"))


class DialogShellTests(SharedPartsCase):
    def open(self, dialog) -> None:
        QMetaObject.invokeMethod(self.obj("h6eBefore"), "forceActiveFocus")
        QMetaObject.invokeMethod(dialog, "openFrom", Q_ARG("QVariant", self.obj("h6eBefore")))

    def test_n_scrim_size_and_footer_at_every_size(self) -> None:
        bridge = self.bridge()
        root, harness = self.harness(bridge)
        dialog = self.obj("h6eDialog")
        harness.setProperty("lines", 60)                      # a body far taller than any window
        for width, height in SIZES:
            with self.subTest(size=(width, height)):
                root.resize(width, height); pump(300)
                self.open(dialog)
                settle(lambda: dialog.property("opened"))
                scrim = self.scrims()
                self.assertEqual(1, len(scrim))
                settle(lambda: scrim[0].opacity() == 1.0)
                self.assertAlmostEqual(0.56, scrim[0].property("color").alphaF(), places=2)
                self.assertEqual((0.0, 0.0, 0.0), scrim[0].property("color").getRgbF()[:3])
                self.assertEqual(1.0, scrim[0].opacity())
                window = QRectF(16, 16, width - 32, height - 32)
                panel = self.rect(self.obj("jarvisDialogBody").parentItem())
                self.assertTrue(window.contains(panel), (panel, width, height))
                for name in ("h6eCancel", "h6eConfirm"):
                    button = self.rect(self.obj(name))
                    self.assertTrue(panel.contains(button), (name, button, panel))
                body = self.obj("jarvisDialogBody")
                self.assertGreater(body.property("contentHeight"), body.height())      # the body scrolls
                QMetaObject.invokeMethod(dialog, "close")
                settle(lambda: not dialog.property("visible"))

    def test_o_keys_focus_and_enter(self) -> None:
        bridge = self.bridge()
        root, harness = self.harness(bridge)
        bridge.setPage("history"); pump(500)
        dialog = self.obj("h6eDialog")
        cancel, confirm, field, guarded = (self.obj(n) for n in ("h6eCancel", "h6eConfirm", "h6eField", "h6eGuarded"))
        self.open(dialog)
        settle(lambda: dialog.property("opened"))
        self.assertTrue(cancel.hasActiveFocus())                                    # the SAFE control
        frame = self.obj("jarvisDialogBody").parentItem()
        for _ in range(8):                                                         # Tab never leaves the dialog
            QTest.keyClick(root, Qt.Key_Tab); pump(30)
            self.assertTrue(self.inside(self.focus(), frame), self.focus())
        # Enter in a field confirms nothing (the dialog did not opt in) ...
        field.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return); pump(60)
        self.assertEqual((0, True), (harness.property("confirmed"), dialog.property("visible")))
        # ... and a guarded field never does, even when the dialog opts in.
        dialog.setProperty("enterConfirms", True)
        guarded.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return); pump(60)
        self.assertEqual((0, True), (harness.property("confirmed"), dialog.property("visible")))
        # Enter on the focused Cancel cancels.
        cancel.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return)
        settle(lambda: not dialog.property("visible"))
        self.assertEqual(0, harness.property("confirmed"))
        self.assertTrue(self.obj("h6eBefore").hasActiveFocus())                    # focus returns to the opener
        # Esc cancels: the dialog closes, the page under it stays.
        self.open(dialog)
        settle(lambda: dialog.property("opened"))
        QTest.keyClick(root, Qt.Key_Escape)
        settle(lambda: not dialog.property("visible"))
        self.assertEqual((0, "history"), (harness.property("confirmed"), bridge.page))
        self.assertTrue(self.obj("h6eBefore").hasActiveFocus())
        # Enter on the focused confirm button confirms; with opt-in an unguarded field does too.
        self.open(dialog)
        settle(lambda: dialog.property("opened"))
        confirm.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return)
        settle(lambda: not dialog.property("visible"))
        self.open(dialog)
        settle(lambda: dialog.property("opened"))
        field.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return)
        settle(lambda: not dialog.property("visible"))
        self.assertEqual(2, harness.property("confirmed"))

    def test_p_confirm_waits_for_the_motion_and_the_motion_keeps_its_tokens(self) -> None:
        bridge = self.bridge()
        root, _ = self.harness(bridge)
        dialog, confirm = self.obj("h6eDialog"), self.obj("h6eConfirm")
        samples = self.motion(root, dialog)
        mark, called = len(samples), time.monotonic()
        self.open(dialog)
        self.assertFalse(confirm.isEnabled())                 # not while it is still opening
        self.assertLess(dialog.property("opacity"), 1.0)
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, True, 220)    # Theme.pageMs
        settle(lambda: dialog.property("opened"))
        self.assertTrue(confirm.isEnabled())
        self.assertEqual((1.0, 1.0), (dialog.property("opacity"), dialog.property("scale")))
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(confirm.isEnabled())                 # nor while it is closing
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, False, 150)   # Theme.panelOut

    def test_q_ambient_off_means_no_motion_and_end_states_at_once(self) -> None:
        bridge = self.bridge()
        bridge.setAmbientEffects(False)
        root, _ = self.harness(bridge)
        dialog, confirm = self.obj("h6eDialog"), self.obj("h6eConfirm")
        self.open(dialog)                                     # no pumping between the call and the checks
        self.assertTrue(dialog.property("opened"))
        self.assertEqual((1.0, 1.0), (dialog.property("opacity"), dialog.property("scale")))
        self.assertTrue(confirm.isEnabled())
        self.assertEqual([1.0], [s.opacity() for s in self.scrims()])
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(dialog.property("visible"))
        self.assertTrue(self.obj("h6eBefore").hasActiveFocus())


class SegmentedFocusTests(SharedPartsCase):
    def test_r_the_segmented_well_shows_the_ring_on_tab_focus(self) -> None:
        root, _ = self.harness(self.bridge())
        segmented = self.obj("h6eSegmented")
        ring = segmented.findChild(QObject, "segmentedFocusRing")
        self.assertFalse(ring.isVisible())
        QMetaObject.invokeMethod(self.obj("h6eBefore"), "forceActiveFocus"); pump(30)
        QTest.keyClick(root, Qt.Key_Tab); pump(60)
        self.assertTrue(segmented.hasActiveFocus())
        self.assertTrue(ring.isVisible())
        from PySide6.QtQml import QQmlProperty
        self.assertEqual(2, QQmlProperty.read(ring, "border.width"))
        self.assertEqual("a", segmented.property("value"))      # focus alone picks nothing


# -- slice C3: the TopBar scope group, its menus and the rename editor --

LONG_PROJECT = "İstanbul Beşiktaş daire tadilatı, taşınma planlaması ve kira sözleşmesi takibi 2026"
OTHERS = ("brandWordmark", "brandTagline", "settingsOrb", "jarvisCore", "commandStrip", "leftContextCard",
          "coreStatusGroup", "editLayoutTrigger")


class ScopeGroupCase(WindowCase):
    def seed(self):
        store = self.db()
        longp = store.create_project(LONG_PROJECT)
        thesis = store.create_project("Tez 2025 okuma ve yazım çalışması")
        self.ids = {
            "long": store.create_conversation(longp.id, title=LONG_NAME).id,
            "thesis": store.create_conversation(thesis.id, title="Okuma listesi taslağı ve kaynakça").id,
            "global": store.create_conversation(title="Kısa").id,
        }
        store.set_project_active(thesis.id, False)
        return store

    def home(self, bridge, key) -> None:
        bridge.openConversation(self.ids[key]); bridge.setPage("home"); pump(350)

    def painted(self, text_item) -> int:
        """Characters a Text really shows: its text elided to its own width."""
        from PySide6.QtGui import QFontMetricsF
        text = text_item.property("text")
        shown = QFontMetricsF(text_item.property("font")).elidedText(text, Qt.TextElideMode.ElideRight, text_item.width())
        return len(text) if shown == text else len(shown.rstrip("…"))


class ScopeLayoutTests(ScopeGroupCase):
    def test_s_names_are_never_a_bare_ellipsis_and_the_tip_has_them_whole(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        for language in ("en-US", "tr-TR"):
            if language == "tr-TR":
                self.tr(bridge)
            for key in ("long", "thesis", "global"):
                self.home(bridge, key)
                for width, height in SIZES:
                    with self.subTest(language=language, owner=key, size=(width, height)):
                        root.resize(width, height); pump(400)
                        name, title = self.item(root, "chatHeaderScopeName"), self.item(root, "chatHeaderTitle")
                        side = self.item(root, "sidebarScopeName")
                        for text_item in (name, title, side):
                            self.assertGreaterEqual(self.painted(text_item), min(6, len(text_item.property("text"))),
                                                    text_item.property("text"))
                        badge = self.item(root, "chatHeaderScopeArchived")
                        self.assertEqual(key == "thesis", badge.isVisible())
                        suffix = " · " + ("archived" if language == "en-US" else "arşivlendi")
                        if key == "thesis":
                            self.assertEqual({"en-US": "ARCHIVED", "tr-TR": "ARŞİV"}[language], badge.property("text"))
                            self.assertEqual(name.property("text") + suffix, self.item(root, "chatHeaderScopeTip").property("text"))
                            self.assertEqual(side.property("text") + suffix, self.item(root, "sidebarScopeTip").property("text"))
                        else:
                            self.assertEqual(name.property("text"), self.item(root, "chatHeaderScopeTip").property("text"))
                            self.assertEqual(side.property("text"), self.item(root, "sidebarScopeTip").property("text"))
                        tip = self.item(root, "chatHeaderTitleTip")
                        self.assertEqual(bridge.conversationTitle, tip.property("text"))           # the whole title


class ScopeKeyboardTests(ScopeGroupCase):
    def test_u_tab_reaches_the_pills_and_the_keys_open_the_menus(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "long")
        # Gate 6F: the scope picks are the sidebar's rows and its "+" (the pills left the bar).
        rings = {"sidebarGlobal": "sidebarRowFocusRing", "sidebarProject": "sidebarRowFocusRing",
                 "sidebarNewConversation": "sidebarPlusFocusRing", "sidebarConversation": "sidebarRowFocusRing",
                 "chatHeaderMenu": None}
        reached = set()
        self.item(root, "sidebarGlobal").forceActiveFocus(); pump(30)
        for _ in range(16):
            focused = root.activeFocusItem()
            if focused is not None and focused.objectName() in rings:
                reached.add(focused.objectName())
                if rings[focused.objectName()]:
                    ring = [c for c in focused.childItems() if c.objectName() == rings[focused.objectName()]][0]
                    self.assertTrue(ring.isVisible(), focused.objectName())
            QTest.keyClick(root, Qt.Key_Tab); pump(20)
        self.assertLessEqual({"sidebarGlobal", "sidebarProject", "sidebarNewConversation", "sidebarConversation"}, reached)
        menu = lambda name: root.findChild(QObject, name)  # noqa: E731
        # Return / Space on a scope row: the 6B pick.
        for key in (Qt.Key_Return, Qt.Key_Space):
            self.item(root, "sidebarGlobal").forceActiveFocus(); QTest.keyClick(root, key); pump(300)
            self.assertTrue(bridge.conversationProject["global"], key)
            row = [i for i in self.visible(root, "sidebarProject") if i.property("title") == LONG_PROJECT][0]
            row.forceActiveFocus(); QTest.keyClick(root, key); pump(300)
            self.assertEqual(LONG_PROJECT, bridge.conversationProject["name"], key)
        # Menu / Shift+F10 on a row, and Return / Space on "…": the conversation menu.
        for key, modifier in ((Qt.Key_Menu, Qt.NoModifier), (Qt.Key_F10, Qt.ShiftModifier)):
            row = [i for i in self.visible(root, "sidebarConversation") if i.property("current")][0]
            row.forceActiveFocus(); QTest.keyClick(root, key, modifier); pump(300)
            self.assertTrue(menu("conversationSwitcherContext").property("visible"), key)
            self.assertEqual(LONG_NAME, self.item(root, "conversationContextHeader").property("text"))
            QTest.keyClick(root, Qt.Key_Escape); pump(300)
            self.assertFalse(menu("conversationSwitcherContext").property("visible"))
        for key in (Qt.Key_Return, Qt.Key_Space):
            self.item(root, "chatHeaderMenu").forceActiveFocus(); QTest.keyClick(root, key); pump(300)
            self.assertTrue(menu("conversationSwitcherContext").property("visible"), key)
            self.assertEqual(LONG_NAME, self.item(root, "conversationContextHeader").property("text"))
            QTest.keyClick(root, Qt.Key_Escape); pump(300)
            self.assertFalse(menu("conversationSwitcherContext").property("visible"))
        # Return on "+" does what its click does: a new conversation in this scope.
        before = bridge.conversationId
        self.item(root, "sidebarNewConversation").forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return); pump(400)
        self.assertNotEqual(before, bridge.conversationId)
        self.assertEqual(LONG_PROJECT, bridge.conversationProject["name"])
        self.assertEqual(0, self.execute_calls())


class ScopeStateTests(ScopeGroupCase):
    def test_v_the_disabled_new_button_tip_is_at_full_opacity(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "thesis")
        button = self.item(root, "sidebarNewConversation")
        self.assertLess(button.opacity(), 1.0)                                    # dimmed: not allowed here
        QTest.mouseMove(root, button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint())
        tip = [c for c in button.childItems() if c.objectName() == "sidebarPlusTip"][0]
        settle(lambda: tip.property("shown"))
        text = [t for t in self.visible(root, "hoverTipText")
                if t.property("text").startswith("The project is archived")][0]
        opacity, item = 1.0, text
        while item is not None:
            opacity *= item.opacity()
            item = item.parentItem()
        self.assertEqual(1.0, opacity)
        before = bridge.conversationId
        self.press(root, button)
        self.assertEqual(before, bridge.conversationId)                           # still refused, as before

    def test_x_menu_motion_follows_its_tokens_and_stops_with_ambient_off(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "global")
        menu = root.findChild(QObject, "conversationSwitcherContext")           # the header's "…" menu
        samples = self.motion(root, menu)
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(menu, "open")
        self.assertLess(menu.property("opacity"), 1.0)
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, True, 180)    # Theme.panelIn
        settle(lambda: menu.property("y") == menu.property("restY"))
        self.assertEqual(1.0, menu.property("opacity"))
        self.assertEqual(menu.property("restY"), menu.property("y"))
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(menu, "close")
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, False, 150)   # Theme.panelOut
        # Ambient effects off: no motion at all - the end state at once.
        bridge.setAmbientEffects(False); pump(100)
        for name in ("conversationSwitcherContext", "conversationRenameEditor"):
            popup = root.findChild(QObject, name)
            QMetaObject.invokeMethod(popup, "open")
            self.assertTrue(popup.property("opened"), name)
            self.assertEqual((1.0, popup.property("restY")), (popup.property("opacity"), popup.property("y")))
            QMetaObject.invokeMethod(popup, "close")
            self.assertFalse(popup.property("visible"), name)


# -- slice C4: the attach, delete and purge dialogs on the shell --

LONG_NEW = "Yazlık evin çatı onarımı, sigorta başvurusu, usta teklifleri ve belediye izinlerinin karşılaştırması"


class DialogAdoptionCase(ScopeGroupCase):
    def stage(self):
        """Global conversation (attach), an archived one (delete), a retired file (purge)."""
        store = self.db()
        self.home_project = store.create_project("Evim")
        doomed = store.create_conversation(title="Eski vergi notları")
        say(store, doomed.id, "bir")
        store.archive_conversation(doomed.id)
        self.doomed = doomed.id
        bridge = self.bridge()
        root = self.window(bridge)
        path = self.root / "fixture-files" / "rapor.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("gizli veri " * 30, encoding="utf-8")
        bridge.backend.project_file("add", self.home_project.id, path=str(path), classification="sensitive")
        file_id = store.list_project_files(self.home_project.id)[0]["id"]
        bridge.backend.project_file("retire", self.home_project.id, file_id=file_id)
        return store, bridge, root

    def dialog(self, root, name):
        if name == "attachDialog":
            return attach_dialog(root)
        shown = [d for d in root.findChildren(QObject, name) if d.property("visible")]
        return shown[0] if shown else root.findChildren(QObject, name)[0]

    def open_attach(self, bridge, root):
        bridge.setPage("home"); pump(300)
        self.steady(root, "chatHeaderAttach")
        self.click(root, "chatHeaderAttach"); pump(100)
        return attach_dialog(root)

    def open_delete(self, bridge, root):
        bridge.setPage("history"); pump(400)
        self.press(root, self.visible(root, "historyDelete")[0])
        return self.dialog(root, "conversationDeleteDialog")

    def open_purge(self, bridge, root):
        bridge.showProject(self.home_project.id); pump(400)
        self.item(root, "projectsPage").setProperty("section", "files"); pump(200)
        self.press(root, self.visible(root, "filePurge")[0])
        return self.dialog(root, "purgeDialog")

    def frame(self, dialog):
        return dialog.findChild(QObject, "jarvisDialogBody").parentItem()

    def inside(self, item, ancestor) -> bool:
        while item is not None:
            if item == ancestor:
                return True
            item = item.parentItem()
        return False

    def scrims(self, root) -> list:
        return [i for i in self.all_items(root, "jarvisDialogScrim") if i.isVisible()]

    def close(self, dialog) -> None:
        QMetaObject.invokeMethod(dialog, "close")
        settle(lambda: not dialog.property("visible"))


class DialogLayoutTests(DialogAdoptionCase):
    def test_y_every_dialog_stays_inside_the_window_with_its_footer_and_scrim(self) -> None:
        store, bridge, root = self.stage()
        cases = (("attach", self.open_attach, ("attachCancel", "attachConfirm")),
                 ("delete", self.open_delete, ("deleteCancel", "deleteConfirm")),
                 ("purge", self.open_purge, ("purgeCancel", "purgeConfirm")))
        for width, height in SIZES:
            for name, opener, buttons in cases:
                with self.subTest(dialog=name, size=(width, height)):
                    root.resize(width, height); pump(300)
                    dialog = opener(bridge, root)
                    if name == "attach":                                # the long new name of D24
                        self.visible(root, "attachNewName")[0].setProperty("text", LONG_NEW)
                        dialog.setProperty("useNew", True)
                        bridge.previewAttach("", LONG_NEW); pump(100)
                    settle(lambda: dialog.property("opened"))
                    panel = self.rect(self.frame(dialog))
                    self.assertTrue(QRectF(16, 16, width - 32, height - 32).contains(panel), (panel, width, height))
                    for button in buttons:
                        box = self.rect(self.visible(root, button)[0])
                        self.assertTrue(panel.contains(box), (button, box, panel))
                    scrim = self.scrims(root)
                    self.assertEqual(1, len(scrim))
                    settle(lambda: scrim[0].opacity() == 1.0)
                    self.assertAlmostEqual(0.56, scrim[0].property("color").alphaF(), places=2)
                    self.close(dialog)

    def test_z_the_identity_cards_and_the_attach_placeholder(self) -> None:
        store, bridge, root = self.stage()
        dialog = self.open_attach(bridge, root)
        settle(lambda: dialog.property("opened"))
        placeholder = self.visible(root, "attachPlaceholder")
        self.assertEqual(["Choose a project to preview."], [p.property("text") for p in placeholder])
        self.press(root, self.visible(root, "attachProject")[0])
        self.assertEqual([], self.visible(root, "attachPlaceholder"))
        card = self.visible(root, "attachIdentityCard")[0]
        self.assertTrue(self.inside(self.visible(root, "attachIdentity")[0], card))
        # A new project is named once (Target); the lines below refer to it.
        self.visible(root, "attachNewName")[0].setProperty("text", LONG_NEW)
        dialog.setProperty("useNew", True)
        bridge.previewAttach("", LONG_NEW); pump(100)
        self.assertEqual(f"Target: new project: {LONG_NEW}", self.visible(root, "attachTarget")[0].property("text"))
        changes = self.visible(root, "attachChanges")[0].property("text")
        self.assertNotIn(LONG_NEW, changes)
        self.assertIn("The owner becomes the new project, permanently.", changes)
        self.close(dialog)
        dialog = self.open_delete(bridge, root)
        card = self.visible(root, "deleteIdentityCard")[0]
        self.assertTrue(self.inside(self.visible(root, "deleteIdentity")[0], card))
        self.assertEqual("Delete this conversation permanently?", self.visible(root, "deleteTitle")[0].property("text"))
        self.assertEqual("DELETE PERMANENTLY", self.visible(root, "jarvisDialogLabel")[0].property("text"))


class DialogKeyboardTests(DialogAdoptionCase):
    def test_aa_initial_focus_tab_escape_focus_return_and_enter(self) -> None:
        store, bridge, root = self.stage()
        # Delete, opened from the keyboard: focus on Vazgeç, Tab stays in, Esc returns to the opener.
        bridge.setPage("history"); pump(400)
        opener = self.visible(root, "historyDelete")[0]
        opener.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return)
        dialog = self.dialog(root, "conversationDeleteDialog")
        settle(lambda: dialog.property("opened"))
        self.assertTrue(self.visible(root, "deleteCancel")[0].hasActiveFocus())
        for _ in range(6):
            QTest.keyClick(root, Qt.Key_Tab); pump(30)
            self.assertTrue(self.inside(root.activeFocusItem(), self.frame(dialog)), root.activeFocusItem())
        QTest.keyClick(root, Qt.Key_Escape)
        settle(lambda: not dialog.property("visible"))
        self.assertEqual("history", bridge.page)
        self.assertTrue(opener.hasActiveFocus())
        self.assertFalse(store.is_conversation_deleted(self.doomed))
        # Purge: focus in the name field; Enter there (even with the right name) purges nothing.
        bridge.showProject(self.home_project.id); pump(400)
        self.item(root, "projectsPage").setProperty("section", "files"); pump(200)
        opener = self.visible(root, "filePurge")[0]
        opener.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return)
        dialog = self.dialog(root, "purgeDialog")
        settle(lambda: dialog.property("opened"))
        name = self.visible(root, "purgeConfirmName")[0]
        self.assertTrue(name.hasActiveFocus())
        name.setProperty("text", "rapor.txt"); pump(60)
        QTest.keyClick(root, Qt.Key_Return); pump(100)
        self.assertTrue(dialog.property("visible"))
        self.assertEqual("retired", store.list_project_files(self.home_project.id)[0]["lifecycle"])
        QTest.keyClick(root, Qt.Key_Escape)
        settle(lambda: not dialog.property("visible"))
        self.assertTrue(opener.hasActiveFocus())
        # Attach: focus on the first project; Enter in the new-name field only previews.
        dialog = self.open_attach(bridge, root)
        settle(lambda: dialog.property("opened"))
        self.assertTrue(self.visible(root, "attachProject")[0].hasActiveFocus())
        field = self.visible(root, "attachNewName")[0]
        field.forceActiveFocus()
        for ch in "Mutfak":
            QTest.keyClick(root, ch)
        pump(100)
        self.assertEqual("Target: new project: Mutfak", self.visible(root, "attachTarget")[0].property("text"))
        QTest.keyClick(root, Qt.Key_Return); pump(200)
        self.assertTrue(dialog.property("visible"))
        self.assertIsNone(store.load_conversation(bridge.conversationId).project_id)       # still Global
        self.assertEqual([], [p for p in store.list_projects() if p.name == "Mutfak"])
        self.close(dialog)
        self.assertTrue(self.item(root, "chatHeaderAttach").hasActiveFocus())          # back to its opener
        self.assertEqual(0, self.execute_calls())


class DialogMotionTests(DialogAdoptionCase):
    def test_ab_confirm_waits_for_the_motion_and_ambient_off_is_instant(self) -> None:
        store, bridge, root = self.stage()
        bridge.setPage("history"); pump(400)
        dialog = root.findChildren(QObject, "conversationDeleteDialog")[0]
        confirm = dialog.findChild(QObject, "deleteConfirm")
        samples = self.motion(root, dialog)
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(dialog, "openFor", Q_ARG("QVariant", self.doomed))
        self.assertTrue(dialog.property("shown"))
        self.assertFalse(confirm.isEnabled())                            # not while opening
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, True, 220)    # Theme.pageMs
        settle(lambda: dialog.property("opened"))
        self.assertTrue(confirm.isEnabled())
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(confirm.isEnabled())                            # nor while closing
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, False, 150)   # Theme.panelOut
        # Ambient effects off: every dialog at once.
        bridge.setAmbientEffects(False); pump(100)
        QMetaObject.invokeMethod(dialog, "openFor", Q_ARG("QVariant", self.doomed))
        self.assertTrue(dialog.property("opened"))
        self.assertEqual((1.0, 1.0), (dialog.property("opacity"), dialog.property("scale")))
        self.assertTrue(confirm.isEnabled())
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(dialog.property("visible"))
        for name in ("attachDialog", "purgeDialog"):
            if name == "purgeDialog":
                bridge.showProject(self.home_project.id); pump(400)
            other = attach_dialog(root) if name == "attachDialog" else root.findChildren(QObject, name)[0]
            QMetaObject.invokeMethod(other, "open")
            self.assertTrue(other.property("opened"), name)
            QMetaObject.invokeMethod(other, "close")
            self.assertFalse(other.property("visible"), name)


# -- slice C5: the History and Projects lists, rows and empty states; RenameMenu on the shell --
# AC whole rows, every button inside its clip (D15, D20); AD headings and actions never
# meet, a long project name included (D17, D19); AE row states, keys, Tab and names (D18);
# AF archived = ARŞİV pill + textLabel (G5, D21, D28); AG empty states (D16, D23); AH Q6
# rename; AI no "konuşma" on either page; AJ the RenameMenu dialog on the shell; AK the
# project switch fade.

class ListCase(DialogAdoptionCase):
    def lists(self, archived: int = 3):
        store = self.db()
        self.project = store.create_project(LONG_PROJECT)
        for title in ("Mutfak planı", LONG_NAME, ""):
            store.create_conversation(self.project.id, title=title)
        kept = store.create_conversation(self.project.id, title="Eski teklif notları")
        store.archive_conversation(kept.id)
        store.create_conversation(title="Mutfak bütçesi soruları")
        store.create_conversation(title=LONG_NAME)
        self.archived = []
        for n in range(archived):
            record = store.create_conversation(title=f"Arşiv {n + 1}")
            say(store, record.id, "bir")
            store.archive_conversation(record.id)
            self.archived.append(record.id)
        self.thesis = store.create_project("Tez 2025")
        store.set_project_active(self.thesis.id, False)
        bridge = self.bridge()
        root = self.window(bridge)
        return store, bridge, root

    def sessions(self, bridge, count: int = 3) -> None:
        bridge._sessions = [{"session_id": f"s{n}", "title": f"Oturum {n}", "updated_at": 1_790_000_000.0 + n,
                             "task_count": n, "active_task_id": "", "readable": True} for n in range(count)]
        bridge.dataChanged.emit()

    def under(self, item, name) -> list:
        found = []

        def walk(node):
            if node.objectName() == name and node.isVisible():
                found.append(node)
            for child in node.childItems():
                walk(child)
        walk(item)
        return found

    def shown_texts(self, item) -> list:
        found = []

        def walk(node):
            if not node.isVisible():
                return
            text = node.property("text")
            if isinstance(text, str) and text:
                found.append(text)
            for child in node.childItems():
                walk(child)
        walk(item)
        return found

    def projects(self, bridge, root, project_id, section="conversations") -> None:
        bridge.showProject(project_id); bridge.setPage("projects"); pump(400)
        self.item(root, "projectsPage").setProperty("section", section); pump(150)

    def apart(self, items) -> None:
        boxes = [(i.objectName() or i.metaObject().className(), self.rect(i)) for i in items if i.isVisible() and i.width() > 0]
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                self.assertFalse(boxes[a][1].intersects(boxes[b][1]), (boxes[a], boxes[b]))


class ListLayoutTests(ListCase):
    def test_ac_whole_rows_and_every_button_inside_the_clip(self) -> None:
        store, bridge, root = self.lists()
        self.tr(bridge)
        # D15 exactly: three archived, nothing scrolled - every row and its buttons whole, at every size.
        for width, height in SIZES:
            with self.subTest(size=(width, height), archived=3):
                root.resize(width, height); pump(300)
                bridge.setPage("history"); pump(400)
                self.item(root, "historyRightColumn").setProperty("contentY", 0); pump(100)
                clip = self.rect(self.item(root, "historyArchivedList")).intersected(self.rect(self.item(root, "historyRightColumn")))
                rows = self.visible(root, "historyArchivedRow")
                self.assertEqual(3, len(rows))
                for row in rows:
                    self.assertTrue(clip.contains(self.rect(row)), (self.rect(row), clip))
                    for name in ("historyUnarchive", "historyDelete"):
                        self.assertTrue(clip.contains(self.rect(self.under(row, name)[0])), name)
                # Projects: the archived conversations are whole rows with their buttons inside.
                self.projects(bridge, root, self.project.id)
                box = self.rect(self.item(root, "projectArchivedConversationList"))
                for row in self.visible(root, "projectArchivedConversationRow"):
                    self.assertTrue(box.contains(self.rect(row)))
                    for name in ("projectConversationUnarchive", "projectArchivedConversationDelete"):
                        self.assertTrue(self.rect(row).contains(self.rect(self.under(row, name)[0])), name)
        # Seven: five whole rows, then the list's own scroll; the column scrolls to it.
        for n in range(4):
            record = store.create_conversation(title=f"Daha eski {n + 1}")
            store.archive_conversation(record.id)
        bridge.refreshHistory(); bridge._refresh_conversations(); pump(200)
        for width, height in SIZES:
            with self.subTest(size=(width, height), archived=7):
                root.resize(width, height); pump(300)
                bridge.setPage("history"); pump(400)
                column, listview = self.item(root, "historyRightColumn"), self.item(root, "historyArchivedList")
                column.setProperty("contentY", max(0.0, min(listview.y(), column.property("contentHeight") - column.height())))
                pump(100)
                box, view = self.rect(listview), self.rect(column)
                rows = [r for r in self.visible(root, "historyArchivedRow") if self.rect(r).intersects(box)]
                self.assertEqual(5, len(rows))
                for row in rows:
                    self.assertTrue(box.contains(self.rect(row)) and view.contains(self.rect(row)), (self.rect(row), box, view))
                    for name in ("historyUnarchive", "historyDelete"):
                        self.assertTrue(self.rect(row).contains(self.rect(self.under(row, name)[0])), name)

    def test_ad_headings_and_actions_never_meet_with_a_long_name(self) -> None:
        store, bridge, root = self.lists()
        self.tr(bridge)
        for width, height in SIZES:
            with self.subTest(size=(width, height)):
                root.resize(width, height); pump(300)
                bridge.setPage("history"); pump(400)
                self.apart([self.item(root, "historyConversationsHeading"), self.item(root, "historyNewConversation")])
                for row in self.visible(root, "historyConversationRow") + self.visible(root, "historyArchivedRow"):
                    parts = [row.childItems()[0], *self.under(row, "listRowBadge"), *self.under(row, "listRowMeta"),
                             *self.under(row, "historyUnarchive"), *self.under(row, "historyDelete")]
                    self.apart(parts)
                    self.assertGreaterEqual(self.painted(row.childItems()[0]), min(6, len(row.property("title"))))
                self.projects(bridge, root, self.project.id)
                self.apart([self.item(root, "projectConversationsHeading"), self.item(root, "projectNewConversation")])
                name = self.item(root, "projectDetailName")
                self.apart([name, self.item(root, "projectStatePill"), self.item(root, "projectRename"),
                            self.item(root, "projectArchive")])
                self.assertGreaterEqual(self.painted(name), 6)
                self.assertEqual("+ Yeni sohbet", self.item(root, "projectNewConversation").property("label"))
                self.assertEqual(f"{LONG_PROJECT} içinde yeni sohbet", self.item(root, "projectNewConversationTip").property("text"))
                for row in self.visible(root, "projectConversationRow") + self.visible(root, "projectArchivedConversationRow"):
                    self.apart([row.childItems()[0], *self.under(row, "listRowBadge"), *self.under(row, "listRowMeta"),
                                *self.under(row, "projectConversationUnarchive"),
                                *self.under(row, "projectArchivedConversationDelete")])
                self.projects(bridge, root, self.thesis.id)
                self.apart([self.item(root, "projectDetailName"), self.item(root, "projectStatePill"),
                            self.item(root, "projectDetailUnarchive")])
                self.assertEqual("Önce projeyi arşivden çıkarın", self.item(root, "projectNewConversationTip").property("text"))


class ListStateTests(ListCase):
    def test_ae_hover_open_mark_focus_ring_tab_keys_and_names(self) -> None:
        from PySide6.QtGui import QAccessible
        store, bridge, root = self.lists()
        self.tr(bridge)
        bridge.setPage("history"); pump(400)
        rows = self.visible(root, "historyConversationRow")
        opened = next(r for r in rows if r.property("current"))
        other = next(r for r in rows if not r.property("current"))
        edge = lambda row: self.under(row, "listRowEdge")[0]  # noqa: E731
        self.assertEqual((1.0, 0.0), (edge(opened).opacity(), edge(other).opacity()))
        # The open row: its edge mark and the AÇIK pill; its name is only its name.
        self.assertEqual(["AÇIK"], [t for t in self.shown_texts(self.under(opened, "listRowBadge")[0]) if t.isupper()])
        self.assertEqual(opened.property("title"), opened.childItems()[0].property("text"))
        self.assertFalse([t for t in self.shown_texts(self.item(root, "historyPage")) if "· açık" in t])
        # Hover: a tonal lift, eased in.
        self.assertEqual(0.0, other.property("color").alphaF())
        QTest.mouseMove(root, other.mapToScene(QPointF(other.width() / 2, other.height() / 2)).toPoint())
        settle(lambda: other.property("color").alphaF() > 0.03)
        # Tab reaches the rows; each shows the ring; Return opens the focused one.
        self.item(root, "historyNewConversation").forceActiveFocus()
        QTest.keyClick(root, Qt.Key_Tab); pump(60)
        first = root.activeFocusItem()
        self.assertEqual("historyConversationRow", first.objectName())
        self.assertTrue(self.under(first, "listRowFocusRing"))
        QTest.keyClick(root, Qt.Key_Tab); pump(60)
        self.assertEqual("historyConversationRow", root.activeFocusItem().objectName())
        self.assertIsNot(first, root.activeFocusItem())
        # Accessible: a button named by the conversation (and its state).
        face = QAccessible.queryAccessibleInterface(opened)
        self.assertEqual(QAccessible.Role.Button, face.role())
        self.assertEqual(f"{opened.property('title')}, AÇIK", face.text(QAccessible.Text.Name))
        target = bridge.conversations[0]["id"]
        first.forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return); pump(300)
        self.assertEqual((target, "home"), (bridge.conversationId, bridge.page))
        # Projects: Tab from Create reaches the project rows; Return opens one; the tabs take arrows.
        bridge.showProject(""); bridge.setPage("projects"); pump(400)
        self.item(root, "projectCreate").forceActiveFocus()
        QTest.keyClick(root, Qt.Key_Tab); pump(60)
        self.assertEqual("projectRow", root.activeFocusItem().objectName())
        QTest.keyClick(root, Qt.Key_Return); pump(300)
        self.assertEqual(bridge.projectRecords[0]["id"], bridge.openProject["id"])
        tabs = self.item(root, "projectTabs")
        tabs.forceActiveFocus(); pump(60)
        self.assertTrue(self.under(tabs, "segmentedFocusRing"))
        page = self.item(root, "projectsPage")
        QTest.keyClick(root, Qt.Key_Right); pump(60)
        self.assertEqual("instruction", page.property("section"))
        QTest.keyClick(root, Qt.Key_Left); pump(60)
        self.assertEqual("conversations", page.property("section"))
        self.assertEqual(0, self.execute_calls())

    def test_af_archived_is_a_pill_and_a_readable_name_never_colour_alone(self) -> None:
        store, bridge, root = self.lists()
        self.tr(bridge)
        bridge.setPage("history"); pump(400)
        label = self.item(root, "historyArchivedHeading").property("color")              # Theme.textLabel
        for row in self.visible(root, "historyArchivedRow"):
            self.assertEqual("ARŞİV", self.under(row, "listRowBadge")[0].property("text"))
            self.assertEqual(label, row.childItems()[0].property("color"))
        for row in self.visible(root, "historyConversationRow"):
            if not row.property("current"):
                self.assertEqual([], self.under(row, "listRowBadge"))
                self.assertNotEqual(label, row.childItems()[0].property("color"))
        self.projects(bridge, root, self.project.id)
        thesis = next(r for r in self.visible(root, "projectRow") if r.property("title") == "Tez 2025")
        self.assertEqual("ARŞİV", self.under(thesis, "listRowBadge")[0].property("text"))
        self.assertEqual(label, self.under(thesis, "projectRowName")[0].property("color"))
        self.assertIn("ETKİN", self.shown_texts(self.item(root, "projectStatePill")))
        for row in self.visible(root, "projectArchivedConversationRow"):
            self.assertEqual("ARŞİV", self.under(row, "listRowBadge")[0].property("text"))
            self.assertEqual(label, row.childItems()[0].property("color"))
        self.projects(bridge, root, self.thesis.id)
        self.assertIn("ARŞİVLENDİ", self.shown_texts(self.item(root, "projectStatePill")))

    def test_ag_empty_states_are_compact_lines_at_the_top(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.tr(bridge)
        bridge.setPage("projects"); pump(400)
        empty = self.item(root, "projectsEmpty")
        self.assertTrue(empty.isVisible())
        self.assertEqual("Henüz proje yok. Soldaki alana bir ad yazıp Oluştur'a basın.", empty.property("text"))
        self.assertEqual((0.0, 0.0), (empty.x(), empty.y()))
        bridge.setPage("history"); pump(400)
        self.assertEqual(["Henüz kalıcı oturum yok."], [i.property("text") for i in self.visible(root, "historySessionsEmpty")])
        self.sessions(bridge); pump(200)
        self.assertEqual([], self.visible(root, "historySessionsEmpty"))
        self.assertEqual(3, len(self.visible(root, "historySessionRow")))
        # A project without conversations or jobs: its lines and its action, at the left.
        bridge.createProject("Bahçe"); pump(100)
        garden = bridge.projectRecords[0]["id"]
        bridge.showProject(""); bridge.setPage("projects"); pump(300)
        self.assertEqual("Sohbetlerini görmek için bir proje seçin.", self.item(root, "projectsEmpty").property("text"))
        bridge.showProject(garden); pump(300)
        for name, text in (("projectConversationsEmpty", "Bu projede henüz sohbet yok."),
                           ("projectJobsEmpty", "Bu proje için zamanlanmış iş yok.")):
            line = self.item(root, name)
            self.assertEqual((True, text, 0.0), (line.isVisible(), line.property("text"), line.x()))
        self.assertTrue(self.item(root, "projectNewConversation").isEnabled())

    def test_ah_q6_the_rename_field_shows_only_after_rename(self) -> None:
        store = self.db()
        evim = store.create_project("Evim")
        store.create_project("Tez")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject(evim.id); bridge.setPage("projects"); pump(400)
        self.assertEqual([], self.visible(root, "projectRenameField"))
        self.press(root, self.item(root, "projectRename"))
        field = self.visible(root, "projectRenameField")[0]
        self.assertEqual(("Evim", True), (field.property("text"), field.hasActiveFocus()))
        self.assertEqual([], self.visible(root, "projectRename"))
        field.setProperty("text", "Evim 2"); QTest.keyClick(root, Qt.Key_Return); pump(200)
        self.assertEqual("Evim 2", store.load_project(evim.id).name)
        self.assertEqual([], self.visible(root, "projectRenameField"))
        self.assertTrue(self.item(root, "projectRename").hasActiveFocus())
        # Esc: put away, nothing renamed.
        self.press(root, self.item(root, "projectRename"))
        QTest.keyClick(root, Qt.Key_Escape); pump(100)
        self.assertEqual(([], "Evim 2"), (self.visible(root, "projectRenameField"), store.load_project(evim.id).name))
        # A refusal keeps the field and shows the store's notice; Cancel puts it away.
        self.press(root, self.item(root, "projectRename"))
        self.visible(root, "projectRenameField")[0].setProperty("text", "tez")
        self.press(root, self.visible(root, "projectRenameSave")[0])
        self.assertIn("already exists", bridge.projectNotice)
        self.assertEqual(1, len(self.visible(root, "projectRenameField")))
        self.press(root, self.visible(root, "projectRenameCancel")[0])
        self.assertEqual(([], "Evim 2"), (self.visible(root, "projectRenameField"), store.load_project(evim.id).name))
        # Another project, or an archived one, never shows a field left open.
        self.press(root, self.item(root, "projectRename"))
        bridge.archiveProject(evim.id); pump(300)
        self.assertEqual(([], []), (self.visible(root, "projectRenameField"), self.visible(root, "projectRename")))
        self.assertEqual(0, self.execute_calls())

    def test_ai_no_konusma_on_either_page_in_turkish(self) -> None:
        store, bridge, root = self.lists()
        self.sessions(bridge)
        self.tr(bridge)
        bridge.setPage("history"); pump(400)
        shown = self.shown_texts(self.item(root, "historyPage"))
        for section in ("conversations", "instruction", "files"):
            self.projects(bridge, root, self.project.id, section)
            shown += self.shown_texts(self.item(root, "projectsPage"))
        self.projects(bridge, root, self.thesis.id)
        shown += self.shown_texts(self.item(root, "projectsPage"))
        self.assertTrue(shown)
        self.assertEqual([], [t for t in shown if re.search("konuşma", t, re.I)])

    def test_ak_a_project_switch_fades_the_detail_in_and_not_with_ambient_off(self) -> None:
        store, bridge, root = self.lists()
        self.projects(bridge, root, self.project.id)
        detail = self.item(root, "projectDetail")
        samples = self.motion(root, detail)
        mark, called = len(samples), time.monotonic()
        bridge.showProject(self.thesis.id)
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, True, 180)    # Theme.panelIn
        bridge.setAmbientEffects(False); pump(100)
        bridge.showProject(self.project.id)
        self.assertEqual(1.0, detail.opacity())


class RenameDialogTests(ListCase):
    def test_aj_the_rename_dialog_on_the_shell(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.sessions(bridge)
        self.tr(bridge)
        bridge.setPage("history"); pump(400)
        row = self.visible(root, "historySessionRow")[0]
        row.forceActiveFocus(); pump(100)
        QTest.keyClick(root, Qt.Key_Menu); pump(300)                     # the keyboard path to the menu
        menu = root.findChild(QObject, "renameMenu")
        self.assertTrue(menu.property("visible"))
        QMetaObject.invokeMethod(menu.findChild(QObject, "renameAction"), "triggered")
        QMetaObject.invokeMethod(menu, "close")
        dialog = root.findChild(QObject, "renameDialog")
        settle(lambda: dialog.property("opened"))
        self.assertEqual("YENİDEN ADLANDIR", self.under(self.frame(dialog), "jarvisDialogLabel")[0].property("text"))
        scrim = self.scrims(root)
        self.assertEqual(1, len(scrim))
        settle(lambda: scrim[0].opacity() == 1.0)
        self.assertAlmostEqual(0.56, scrim[0].property("color").alphaF(), places=2)
        field = self.visible(root, "renameField")[0]
        self.assertTrue(field.hasActiveFocus())
        self.assertEqual(("Oturum 0", "Oturum 0"), (field.property("text"), field.property("selectedText")))
        for _ in range(4):
            QTest.keyClick(root, Qt.Key_Tab); pump(30)
            self.assertTrue(self.inside(root.activeFocusItem(), self.frame(dialog)), root.activeFocusItem())
        QTest.keyClick(root, Qt.Key_Escape)
        settle(lambda: not dialog.property("visible"))
        self.assertTrue(row.hasActiveFocus())                            # back to the row it was opened from
        self.assertEqual("Oturum 0", bridge.sessions[0]["title"])
        # Motion on its tokens; Save only while fully open; none with ambient effects off.
        save = dialog.findChild(QObject, "renameSave")
        samples = self.motion(root, dialog)
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(dialog, "openFor", Q_ARG("QVariant", "s1"), Q_ARG("QVariant", "Oturum 1"))
        self.assertFalse(save.isEnabled())
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, True, 220)    # Theme.pageMs
        settle(lambda: dialog.property("opened"))
        self.assertTrue(save.isEnabled())
        mark, called = len(samples), time.monotonic()
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(save.isEnabled())
        self.assert_motion_within_token_plus_one_frame(samples, called, mark, False, 150)   # Theme.panelOut
        bridge.setAmbientEffects(False); pump(100)
        QMetaObject.invokeMethod(dialog, "openFor", Q_ARG("QVariant", "s1"), Q_ARG("QVariant", "Oturum 1"))
        self.assertTrue(dialog.property("opened"))
        self.assertEqual((1.0, 1.0), (dialog.property("opacity"), dialog.property("scale")))
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(dialog.property("visible"))
        self.assertEqual(0, self.execute_calls())


# -- slice C7: a live language switch (Q2, D26) and the copy residue --
# AL the Bridge-projected labels follow a live switch with no refresh; AM
# "retranslated" re-reads only (no store write, no state change); AN TasksPage
# counts tasks; AO no "konuşma" is left in the Turkish catalog.

def dump(database) -> dict:
    import contextlib
    import sqlite3
    with contextlib.closing(sqlite3.connect(database)) as connection:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: connection.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}


class LiveLanguageTests(ListCase):
    def untitled_global(self, store, bridge) -> None:
        bridge.openConversation(store.create_conversation().id); bridge.setPage("home"); pump(350)

    def test_al_a_live_switch_rewrites_the_projected_labels_without_a_refresh(self) -> None:
        store, bridge, root = self.lists()
        self.untitled_global(store, bridge)
        owner = lambda: self.item(root, "chatHeaderScopeName").property("text")  # noqa: E731
        switcher = lambda: self.item(root, "chatHeaderTitle").property("text")  # noqa: E731
        side = lambda: self.item(root, "sidebarGlobal").property("title")  # noqa: E731
        self.assertEqual(("Global", "New Chat", "Global"), (owner(), switcher(), side()))
        self.tr(bridge)                                                       # no refresh of any kind after the switch
        self.assertEqual(("Genel", "Yeni Sohbet", "Genel"), (owner(), switcher(), side()))
        self.assertEqual("Genel", bridge.conversationProject["name"])
        # The open project's detail: the untitled row and the archived section rewrite as well.
        bridge.showProject(self.project.id); pump(400)
        untitled = lambda: [r["title"] for r in bridge.projectConversations if r["title"] in ("New Chat", "Yeni Sohbet")]  # noqa: E731
        self.assertEqual(["Yeni Sohbet"], untitled())
        self.assertIn("Yeni Sohbet", self.shown_texts(self.item(root, "projectsPage")))
        bridge.setLanguage("en-US"); pump(300)
        self.assertEqual(["New Chat"], untitled())
        self.assertIn("New Chat", self.shown_texts(self.item(root, "projectsPage")))
        self.assertNotIn("Yeni Sohbet", self.shown_texts(self.item(root, "projectsPage")))
        bridge.setPage("home"); pump(350)
        self.assertEqual(("Global", "New Chat"), (owner(), switcher()))
        bridge.setLanguage("tr-TR"); pump(300)                                # and again, the other way
        self.assertEqual(("Genel", "Yeni Sohbet"), (owner(), switcher()))

    def test_am_retranslated_reads_again_and_changes_nothing(self) -> None:
        store, bridge, root = self.lists()
        self.untitled_global(store, bridge)
        bridge.showProject(self.project.id); pump(300)
        seen = []
        bridge.conversationChanged.connect(lambda: seen.append("conversation"))
        bridge.projectsChanged.connect(lambda: seen.append("projects"))

        def state() -> tuple:
            return (bridge.conversationId, bridge.page, bridge.openProject, bridge.config, bridge.busy, bridge.conversations,
                    bridge.projectConversations, bridge.projectArchivedConversations, bridge.activity, bridge.chatProvider)
        before, rows = state(), dump(self.database)
        prefs = self.prefs_path.read_bytes() if self.prefs_path.exists() else None
        settings = self.store.load().settings
        bridge.retranslated(); pump(200)
        self.assertEqual(before, state())
        self.assertEqual(rows, dump(self.database))                             # no store write, no new row anywhere
        self.assertEqual(prefs, self.prefs_path.read_bytes() if self.prefs_path.exists() else None)
        self.assertEqual(settings, self.store.load().settings)
        self.assertTrue({"conversation", "projects"} <= set(seen))               # the presentation is read again
        self.assertEqual(0, self.execute_calls())

    def test_an_the_tasks_page_counts_tasks(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        self.sessions(bridge)
        bridge.setPage("tasks"); pump(400)
        metas = lambda: sorted(t.split("  ·  ")[1] for t in self.texts(root, "tasksSessionMeta"))  # noqa: E731
        self.assertEqual(["0 tasks", "1 task", "2 tasks"], metas())
        self.tr(bridge)
        self.assertEqual(["0 görev", "1 görev", "2 görev"], metas())

    def test_ao_no_konusma_is_left_in_the_turkish_catalog(self) -> None:
        table = load_catalog(CATALOG_DIR / "jarvis_tr-TR.ts")
        self.assertEqual({}, {k: v for k, v in table.items() if re.search("konuşma|KONUŞMA", v, re.I)})
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertIn("sohbetiniz", tx("SafetyCards", "Nothing new can start, and paused work stays where it stopped. "
                                       "JARVIS stays open: your conversation, results and history are here, and you can still "
                                       "reject a pending approval."))
        self.assertEqual("1 görev", tx("TasksPage", "1 task"))
        self.assertEqual("%1 görev", tx("TasksPage", "%1 tasks"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
