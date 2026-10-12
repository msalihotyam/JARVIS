"""DFMENU: one context menu everywhere (dogfood, Salih 2026-10-12).

* Qt Quick Controls (PySide6 >= 6.9) gives every TextField / TextArea a stock
  context menu (black, English). Every text field sets `ContextMenu.menu: null`
  and has a JARVIS EditMenu as its `editor`; the EditMenu opens itself on the
  editor's right-click. So a right-click shows exactly one menu, and it is ours.
* The static guard: every TextField / TextArea / TextInput / TextEdit in
  jarvis/ui/qml uses that shared way, or is a read-only QtQuick text listed in
  EXEMPT (Qt gives those no menu at all). A new field cannot bring the stock
  menu back.

No test performs a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from PySide6.QtCore import QObject, Qt

from tests.test_menus_6g import MenuCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

QML = Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "qml"
DECL = re.compile(r"\b(TextField|TextArea|TextInput|TextEdit)\s*\{")
# Read-only QtQuick TextEdits per file: Qt gives them no context menu, so there is no stock one to replace.
EXEMPT = {
    "components/ApprovalCard.qml": 6,        # approval card values, outbound preview, attestation
    "components/FeedView.qml": 1,            # streamingReplyText (the final message row has the menu)
    "components/QuoteDetail.qml": 1,         # the quoted text
    "components/SafetyCards.qml": 2,         # safety card values
    "overlays/AttachmentPreview.qml": 1,     # a text attachment's body
    "pages/MemoryPage.qml": 1,               # a memory's text
    "pages/TasksPage.qml": 1,                # the task snapshot lines
}


def body_of(text: str, brace: int) -> str:
    depth = 0
    for i in range(brace, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[brace + 1:i]
    return text[brace + 1:]


def top_level(body: str) -> str:
    while True:
        flat = re.sub(r"\{[^{}]*\}", "", body)
        if flat == body:
            return flat
        body = flat


def offenders(sources: dict) -> list:
    bad = []
    for rel, text in sorted(sources.items()):
        exempt = 0
        for match in DECL.finditer(text):
            kind = match.group(1)
            top = top_level(body_of(text, match.end() - 1))
            ident = re.search(r"\bid:\s*(\w+)", top)
            name = ident.group(1) if ident else "?"
            # Its own menu opens itself; a shared menu (`.editor = id`) needs the text's own right-click.
            owned = re.search(r"EditMenu\s*\{(?![^{}]*ownsRightClick:\s*false)[^{}]*\beditor:\s*%s\b" % name, text)
            shared = re.search(r"\.editor\s*=\s*%s\b" % name, text) and re.search(r"Qt\.RightButton", body_of(text, match.end() - 1))
            wired = bool(ident) and bool(owned or shared)
            # Only the Controls types carry the stock menu.
            stock_off = kind in ("TextInput", "TextEdit") or re.search(r"\bContextMenu\.menu:\s*null\b", top)
            if wired and stock_off:
                continue
            if kind in ("TextInput", "TextEdit") and re.search(r"\breadOnly:\s*true\b", top):
                exempt += 1
                continue
            bad.append(f"{rel}: {kind} {name} (wired={wired}, stock menu off={bool(stock_off)})")
        if exempt != EXEMPT.get(rel, 0):
            bad.append(f"{rel}: {exempt} read-only text(s) without a menu, EXEMPT lists {EXEMPT.get(rel, 0)}")
    return bad


def sources() -> dict:
    return {p.relative_to(QML).as_posix(): p.read_text(encoding="utf-8") for p in QML.rglob("*.qml")}


class GuardTests(unittest.TestCase):
    def test_every_text_field_has_our_menu_and_never_the_stock_one(self) -> None:
        self.assertEqual([], offenders(sources()))

    def test_a_new_field_without_the_shared_way_fails_the_guard(self) -> None:
        rel = "components/SidebarSearch.qml"
        for sample in ("TextField { id: probe }",                                               # nothing
                       "TextField { id: probe; ContextMenu.menu: null }",                       # no menu of ours
                       "TextArea { id: probe; EditMenu { editor: probe } }",                    # the stock one too
                       "TextField { id: probe; ContextMenu.menu: null\n"
                       "            EditMenu { ownsRightClick: false; editor: probe } }",       # a menu that never opens
                       "TextEdit { id: probe; Component.onCompleted: messageMenu.editor = probe }",  # shared, no right-click
                       "TextEdit { id: probe }",                                                # editable, no menu
                       "TextInput { id: probe }"):
            found = sources()
            found[rel] = found[rel].rstrip()[:-1] + sample + "\n}\n"
            self.assertTrue(any("probe" in line for line in offenders(found)), sample)
        found = sources()
        found["pages/MemoryPage.qml"] += "\nTextEdit { readOnly: true }\n"                      # a new unlisted read-only text
        self.assertTrue(any("MemoryPage" in line for line in offenders(found)))
        found = sources()
        found[rel] = found[rel].rstrip()[:-1] + "TextField { id: probe; ContextMenu.menu: null; EditMenu { editor: probe } }\n}\n"
        self.assertEqual([], offenders(found))                                                  # the shared way passes


class RightClickTests(MenuCase):
    def one_menu(self, root, field, menu_name) -> None:
        self.tap(root, self.item(root, field), Qt.RightButton,
                 until=lambda: any(o.objectName() == menu_name and o.property("opened") for o in root.findChildren(QObject)))
        popups = [o for o in root.findChildren(QObject)
                  if o.inherits("QQuickPopup") and not o.inherits("QQuickToolTip") and o.property("visible")]
        self.assertEqual([menu_name], [o.objectName() for o in popups], field)
        self.assertTrue(popups[0].metaObject().className().startswith("EditMenu"), field)
        stock = [o for o in root.findChildren(QObject)
                 if (o.inherits("QQuickMenu") or "TextEditingContextMenu" in o.metaObject().className()) and o.property("visible")]
        self.assertEqual([], stock, field)
        self.key(root, Qt.Key_Escape)
        self.assertEqual(0, self.execute_calls())

    def test_composer(self) -> None:
        root = self.window(self.bridge())
        self.one_menu(root, "composerInput", "composerEditMenu")

    def test_sidebar_search(self) -> None:
        root = self.window(self.bridge())
        self.tap(root, self.item(root, "sidebarSearchEntry"), until=lambda: bool(self.shown(root, "sidebarSearchField")))
        self.one_menu(root, "sidebarSearchField", "sidebarSearchFieldMenu")

    def test_automations_json_editor(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setPage("automations")
        pump(300)
        self.one_menu(root, "jobSpecInput", "jobSpecInputMenu")

    def test_project_search(self) -> None:
        self.db().create_project("Evim")
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.showProject("")
        pump(300)
        self.one_menu(root, "projectsSearch", "projectsSearchMenu")


if __name__ == "__main__":
    unittest.main()
