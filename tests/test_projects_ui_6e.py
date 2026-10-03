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
            "CommandPalette", "Bridge")


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
        self.click(root, "projectSelector"); pump(200)
        self.click(root, "projectSelectorAttach"); pump(300)
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
        self.assertEqual("New Chat", self.item(root, "conversationSwitcherText").property("text"))
        self.tr(bridge)
        self.assertEqual(["Yeni Sohbet"] * 3, names())
        bridge.setPage("home"); pump(300)
        self.assertEqual("Yeni Sohbet", self.item(root, "conversationSwitcherText").property("text"))
        self.assertEqual(inside.id, bridge.projectConversations[0]["id"])
        self.assertEqual("Yeni Sohbet", tx("AttachDialog", "New Chat"))
        self.assertEqual("Yeni Sohbet", tx("ConversationDeleteDialog", "New Chat"))

    def test_g_the_actions_stay_sentence_case(self) -> None:
        localization(_APP).apply("tr-TR")
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        self.assertEqual("+ Yeni sohbet", tx("TopBar", "+ New conversation"))
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
        self.assertEqual("SOHBETLER", tx("TopBar", "CONVERSATIONS"))
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
        from PySide6.QtCore import SIGNAL
        bridge = self.bridge()
        root, _ = self.harness(bridge)
        dialog, confirm = self.obj("h6eDialog"), self.obj("h6eConfirm")
        ticks, events = [], {}
        root.afterAnimating.connect(lambda: ticks.append(time.monotonic()))
        QObject.connect(dialog, SIGNAL("opened()"), lambda: events.setdefault("opened", (time.monotonic(), len(ticks))))
        QObject.connect(dialog, SIGNAL("closed()"), lambda: events.setdefault("closed", (time.monotonic(), len(ticks))))

        def within_token_plus_one_frame(event: str, started_at: int, token_ms: int) -> None:
            # Judged on the window's own animation frames, not on a wall-clock
            # budget: the end state is reached no later than the frame after the
            # first frame at or past the token (a starved loop just makes fewer,
            # longer frames).
            at, seen = events[event]
            start = ticks[started_at] if len(ticks) > started_at else at
            late = [t for t in ticks[started_at:seen] if t >= start + token_ms / 1000]
            self.assertLessEqual(len(late), 1, (event, len(late)))
            self.assertGreaterEqual(at - start, token_ms / 1000 - 0.03, event)      # it really moved

        mark = len(ticks)
        self.open(dialog)
        self.assertFalse(confirm.isEnabled())                 # not while it is still opening
        self.assertLess(dialog.property("opacity"), 1.0)
        settle(lambda: "opened" in events)
        self.assertTrue(confirm.isEnabled())
        within_token_plus_one_frame("opened", mark, 220)      # Theme.pageMs
        self.assertEqual((1.0, 1.0), (dialog.property("opacity"), dialog.property("scale")))
        mark = len(ticks)
        QMetaObject.invokeMethod(dialog, "close")
        self.assertFalse(confirm.isEnabled())                 # nor while it is closing
        settle(lambda: "closed" in events)
        within_token_plus_one_frame("closed", mark, 150)      # Theme.panelOut

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

    def frames(self, root):
        ticks = []
        root.afterAnimating.connect(lambda: ticks.append(time.monotonic()))
        return ticks

    def within_token_plus_one_frame(self, ticks, started_at, at, seen, token_ms) -> None:
        # The window's own animation frames, not a wall-clock budget (see C2).
        start = ticks[started_at] if len(ticks) > started_at else at
        late = [t for t in ticks[started_at:seen] if t >= start + token_ms / 1000]
        self.assertLessEqual(len(late), 1)


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
                        name, title = self.item(root, "projectSelectorName"), self.item(root, "conversationSwitcherText")
                        for text_item in (name, title):
                            self.assertGreaterEqual(self.painted(text_item), min(6, len(text_item.property("text"))),
                                                    text_item.property("text"))
                        badge = self.item(root, "projectSelectorArchived")
                        self.assertEqual(key == "thesis", badge.isVisible())
                        if key == "thesis":
                            self.assertEqual({"en-US": "ARCHIVED", "tr-TR": "ARŞİV"}[language],
                                             badge.childItems()[0].property("text"))
                            self.assertEqual(name.property("text") + " · " + ("archived" if language == "en-US" else "arşivlendi"),
                                             self.item(root, "projectSelectorTip").property("text"))
                        else:
                            self.assertEqual(name.property("text"), self.item(root, "projectSelectorTip").property("text"))
                            # The room the spec promises before any floor is used.
                            selector, switcher = self.item(root, "projectSelector"), self.item(root, "conversationSwitcher")
                            group = self.item(root, "projectScope")
                            self.assertGreaterEqual(selector.width(), min(group.property("selNat"), 110))
                            self.assertGreaterEqual(switcher.width(), min(group.property("swNat"), 140))
                        tip = self.item(root, "conversationSwitcherTipText")
                        self.assertEqual(bridge.conversationTitle, tip.property("text"))           # the whole title

    def test_t_the_scope_group_never_overlaps_the_bar(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        for theme in ("space", "command"):
            bridge.setTheme(theme); pump(400)
            for key in ("long", "thesis"):
                self.home(bridge, key)
                for width, height in SIZES:
                    with self.subTest(theme=theme, owner=key, size=(width, height)):
                        root.resize(width, height); pump(500)
                        group = self.item(root, "projectScope")
                        rects = [self.rect(self.item(root, n)) for n in
                                 ("projectSelector", "conversationSwitcher", "newConversationButton")]
                        for left, right in zip(rects, rects[1:]):
                            self.assertLessEqual(left.right(), right.left())
                        self.assertTrue(QRectF(0, 0, width, height).contains(self.rect(group)))
                        for mine in rects:
                            for name in OTHERS:
                                for other in self.all_items(root, name):
                                    if other.isVisible() and other.width() > 0:
                                        self.assertFalse(mine.intersects(self.rect(other)), name)
                        # The header progress (shown only during a run) starts after
                        # the scope row or is below its own 240 px threshold.
                        hud = self.item(root, "hudProgress")
                        band = QRectF(hud.mapToScene(QPointF(0, 0)), hud.size())
                        if hud.width() >= 240:
                            self.assertFalse(band.intersects(self.rect(group)))


class ScopeKeyboardTests(ScopeGroupCase):
    def test_u_tab_reaches_the_pills_and_the_keys_open_the_menus(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "long")
        names = ("projectSelector", "conversationSwitcher", "newConversationButton")
        reached = set()
        # The window's Tab chain (unchanged): ... window controls, the scope
        # pills, the orb, the rail ... (the composer keeps Tab for itself).
        self.item(root, "windowCloseControl").forceActiveFocus(); pump(30)
        for _ in range(6):
            QTest.keyClick(root, Qt.Key_Tab); pump(20)
            focused = root.activeFocusItem()
            if focused is not None and focused.objectName() in names:
                reached.add(focused.objectName())
                ring = [c for c in focused.childItems() if c.objectName() == "scopePillFocusRing"][0]
                self.assertTrue(ring.isVisible(), focused.objectName())
        self.assertEqual(set(names), reached)
        menu = lambda name: root.findChild(QObject, name)  # noqa: E731
        selector, switcher = self.item(root, "projectSelector"), self.item(root, "conversationSwitcher")
        for key in (Qt.Key_Return, Qt.Key_Space, Qt.Key_Down):
            selector.forceActiveFocus(); QTest.keyClick(root, key); pump(300)
            self.assertTrue(menu("projectSelectorMenu").property("visible"), key)
            QTest.keyClick(root, Qt.Key_Escape); pump(300)
            self.assertFalse(menu("projectSelectorMenu").property("visible"))
            switcher.forceActiveFocus(); QTest.keyClick(root, key); pump(300)
            self.assertTrue(menu("conversationSwitcherMenu").property("visible"), key)
            QTest.keyClick(root, Qt.Key_Escape); pump(300)
        for key, modifier in ((Qt.Key_Menu, Qt.NoModifier), (Qt.Key_F10, Qt.ShiftModifier)):
            switcher.forceActiveFocus(); QTest.keyClick(root, key, modifier); pump(300)
            self.assertTrue(menu("conversationSwitcherContext").property("visible"), key)
            self.assertEqual(LONG_NAME, self.item(root, "conversationContextHeader").property("text"))
            QTest.keyClick(root, Qt.Key_Escape); pump(300)
            self.assertFalse(menu("conversationSwitcherContext").property("visible"))
        # Return on "+" does what its click does: a new conversation in this scope.
        before = bridge.conversationId
        self.item(root, "newConversationButton").forceActiveFocus(); QTest.keyClick(root, Qt.Key_Return); pump(400)
        self.assertNotEqual(before, bridge.conversationId)
        self.assertEqual(LONG_PROJECT, bridge.conversationProject["name"])
        self.assertEqual(0, self.execute_calls())


class ScopeStateTests(ScopeGroupCase):
    def test_v_the_disabled_new_button_tip_is_at_full_opacity(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "thesis")
        button = self.item(root, "newConversationButton")
        self.assertLess(button.opacity(), 1.0)                                    # dimmed: not allowed here
        QTest.mouseMove(root, button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint())
        tip = self.item(root, "newConversationTip")
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

    def test_w_the_chevron_turns_while_its_menu_is_open(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "global")
        for pill, chevron, menu in (("projectSelector", "projectSelectorChevron", "projectSelectorMenu"),
                                    ("conversationSwitcher", "conversationSwitcherChevron", "conversationSwitcherMenu")):
            shape = self.item(root, chevron)
            self.assertEqual(0, shape.rotation())
            self.press(root, self.item(root, pill))
            settle(lambda: shape.rotation() == 180)
            self.assertEqual(180, shape.rotation())
            QMetaObject.invokeMethod(root.findChild(QObject, menu), "close")
            settle(lambda: shape.rotation() == 0)
            self.assertEqual(0, shape.rotation())

    def test_x_menu_motion_follows_its_tokens_and_stops_with_ambient_off(self) -> None:
        from PySide6.QtCore import SIGNAL
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        self.home(bridge, "global")
        menu = root.findChild(QObject, "projectSelectorMenu")
        ticks, events = self.frames(root), {}
        QObject.connect(menu, SIGNAL("opened()"), lambda: events.__setitem__("opened", (time.monotonic(), len(ticks))))
        QObject.connect(menu, SIGNAL("closed()"), lambda: events.__setitem__("closed", (time.monotonic(), len(ticks))))
        mark = len(ticks)
        QMetaObject.invokeMethod(menu, "open")
        self.assertLess(menu.property("opacity"), 1.0)
        settle(lambda: "opened" in events)
        self.within_token_plus_one_frame(ticks, mark, *events["opened"], 180)     # Theme.panelIn
        self.assertEqual(1.0, menu.property("opacity"))
        self.assertEqual(menu.property("restY"), menu.property("y"))
        mark = len(ticks)
        QMetaObject.invokeMethod(menu, "close")
        settle(lambda: "closed" in events)
        self.within_token_plus_one_frame(ticks, mark, *events["closed"], 150)     # Theme.panelOut
        # Ambient effects off: no motion at all - the end state at once.
        bridge.setAmbientEffects(False); pump(100)
        for name in ("projectSelectorMenu", "conversationSwitcherMenu", "conversationSwitcherContext"):
            popup = root.findChild(QObject, name)
            QMetaObject.invokeMethod(popup, "open")
            self.assertTrue(popup.property("opened"), name)
            self.assertEqual((1.0, popup.property("restY")), (popup.property("opacity"), popup.property("y")))
            QMetaObject.invokeMethod(popup, "close")
            self.assertFalse(popup.property("visible"), name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
