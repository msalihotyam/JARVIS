"""Gate 6G G6SB3F: one composer row everywhere (build/6g/PLAN_DRAFT.txt C1, V4, P3).

C1  The row UNDER the composer bar is the same on the chat, the New chat
    screen and a project's page: "+" first, then the mode chip on the left;
    Model, Effort and the usage ring on the right.
V4  The New chat screen's project picker sits IN that row (left group, after
    the mode chip); the composer keeps its normal place. Behaviour unchanged.
V4  "Yeni proje…" -> the existing dialog; a confirmed create selects the new
    project for the new chat, a cancel changes nothing.
P3  The project page's composer is the real one; an empty send still opens a
    new empty conversation in that project.

No test makes a live model, auth or network call or opens the live DB.
"""

from __future__ import annotations

from jarvis.conversation import PersistedConversation
from tests.helpers import settled, wait_until
from tests import test_g6sb3_sidebar
from tests.test_g6sb3_sidebar import G6SB3Case
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump

LEFT = ("attachControl", "permissionModeControl", "projectPickerChip")
RIGHT = ("modelMenuChip", "effortMenuChip")


class Case(G6SB3Case):
    picker_menu = test_g6sb3_sidebar.PickerTests.picker_menu
    owner = test_g6sb3_sidebar.PickerTests.owner

    def named(self, node, name) -> list:
        return [i for i in self.descendants(node) if i.objectName() == name]

    def c1(self, root, composer_name) -> tuple[list, list]:
        """The composer's under-row: the visible chips of each side, left to right."""
        composer = self.item(root, composer_name)
        self.assertTrue(composer.isVisible(), composer_name)
        row = self.named(composer, "composerChipRow")[0]
        bar = self.named(composer, "composerBar")[0]
        self.assertGreaterEqual(self.rect(row).top(), self.rect(bar).bottom() - 0.5)      # UNDER the bar
        chips = [i for i in self.descendants(row) if i.objectName() in LEFT + RIGHT and i.isVisible()]
        middle = self.rect(row).center().x()

        def side(left):
            found = [i for i in chips if (self.rect(i).center().x() < middle) == left]
            return [i.objectName() for i in sorted(found, key=lambda i: self.rect(i).x())]
        # The usage ring shares the right group with Effort (it shows only reported rows, K3).
        ring, effort = self.named(row, "usageRing")[0], self.named(row, "effortMenuChip")[0]
        self.assertIs(ring.parentItem(), effort.parentItem())
        return side(True), side(False)

    def chat_with_a_message(self, store):
        PersistedConversation(store, self.plain[0].id).append(role="user", kind="user", text="Merhaba", message="Merhaba")
        return self.plain[0].id


class RowTests(Case):
    def test_a_the_c1_row_is_the_same_on_chat_new_chat_and_the_project_page(self) -> None:
        store = self.data()
        chat = self.chat_with_a_message(store)
        bridge, root = self.open_window()
        root.resize(1600, 1000)
        self.assertTrue(bridge.openConversation(chat))
        settled(300, lambda: self.assertEqual((["attachControl", "permissionModeControl"], list(RIGHT)),
                                              self.c1(root, "canonicalComposer")))
        chat_bottom = self.rect(self.item(root, "canonicalComposer")).bottom()
        # New chat: the same row, the picker after the mode chip; the composer in its normal place.
        self.assertTrue(bridge.startNewChat())
        settled(300, lambda: self.assertEqual((list(LEFT), list(RIGHT)), self.c1(root, "canonicalComposer")))
        self.assertAlmostEqual(chat_bottom, self.rect(self.item(root, "canonicalComposer")).bottom(), delta=0.5)
        self.assertEqual(self.all_items(root, "projectPicker"), self.named(self.item(root, "composerChipRow"), "projectPicker"))
        # The project page: the same composer and row (no picker: the page is the project).
        bridge.showProject(self.tez.id)
        settled(300, lambda: self.assertEqual((["attachControl", "permissionModeControl"], list(RIGHT)),
                                              self.c1(root, "projectComposer")))
        self.assertEqual(0, self.execute_calls())


class PickerInRowTests(Case):
    def test_b_the_picker_sits_in_the_row_and_keeps_v4(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        # Default: the project page's project.
        bridge.showProject(self.tez.id)
        pump(200)
        self.assertTrue(bridge.startNewChat())
        settled(300, lambda: self.assertEqual("Tez", self.item(root, "projectPickerLabel").property("text")))
        picker = self.item(root, "projectPicker")
        row = self.item(root, "composerChipRow")
        self.assertIn(picker, self.named(row, "projectPicker"))
        mode = self.rect(self.item(root, "permissionModeControl"))
        self.assertGreater(self.rect(picker).left(), mode.right())
        self.assertAlmostEqual(mode.center().y(), self.rect(picker).center().y(), delta=1)
        # Search, then Proje yok, from the row's chip.
        menu = self.picker_menu(root)
        search = self.popup_items(menu, "projectPickerSearch")[0]
        search.setProperty("text", "ko")
        settled(100, lambda: self.assertEqual(["Kod"], [i.property("label") for i in self.popup_items(menu, "projectPickerItem")
                                                        if i.isVisible()]))
        self.tap(root, self.popup_items(menu, "projectPickerNone")[0], until=lambda: not menu.property("visible"))
        settled(300, lambda: (self.assertIsNone(self.owner(store, bridge)),
                              self.assertEqual("Choose project", self.item(root, "projectPickerLabel").property("text"))))
        # The first message fixes the owner: the picker leaves the row.
        menu = self.picker_menu(root)
        kod = [i for i in self.popup_items(menu, "projectPickerItem") if i.property("label") == "Kod"][0]
        self.tap(root, kod, until=lambda: not menu.property("visible"))
        settled(300, lambda: self.assertEqual(self.kod.id, self.owner(store, bridge)))
        chat = bridge.conversationId
        PersistedConversation(store, chat).append(role="user", kind="user", text="İlk", message="İlk")
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        self.assertTrue(bridge.openConversation(chat))
        settled(300, lambda: self.assertFalse(picker.isVisible()))
        self.assertFalse(bridge.setNewChatProject(""))
        self.assertEqual(0, self.execute_calls())


class NewProjectTests(Case):
    def new_project_dialog(self, root, bridge):
        menu = self.picker_menu(root)
        self.tap(root, self.popup_items(menu, "projectPickerNew")[0], until=lambda: bridge.page == "projects")
        dialog = self.popup_named(root, "newProjectDialog")
        self.assertTrue(wait_until(lambda: bool(dialog.property("opened")), 1500))
        return dialog

    def create(self, root, name: str) -> None:
        self.item(root, "newProjectName").setProperty("text", name)
        pump(30)
        self.click(root, "newProjectCreate")

    def test_c_yeni_proje_confirmed_selects_it_cancel_changes_nothing(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.startNewChat())
        pump(200)
        self.new_project_dialog(root, bridge)
        self.create(root, "Mutfak")
        mutfak = lambda: next((r["id"] for r in bridge.projectRecords if r["name"] == "Mutfak"), None)   # noqa: E731
        self.assertTrue(wait_until(lambda: bridge.page == "home" and mutfak() is not None, 3000))   # the dialog left with its page
        settled(300, lambda: (self.assertEqual(mutfak(), self.owner(store, bridge)),
                              self.assertEqual("Mutfak", self.item(root, "projectPickerLabel").property("text")),
                              self.assertTrue(self.item(root, "projectPicker").isVisible())))
        chat = bridge.conversationId
        self.assertEqual([], list(store.load_conversation_state(chat).turns))
        # Cancel: the dialog closes, the new chat keeps its project.
        dialog = self.new_project_dialog(root, bridge)
        self.click(root, "newProjectCancel")
        self.assertTrue(wait_until(lambda: not dialog.property("visible"), 1500))
        self.assertEqual((chat, mutfak()), (bridge.conversationId, self.owner(store, bridge)))
        # ...and the request is gone: the Projects page's own create opens its project as before.
        self.click(root, "projectsNew")
        self.assertTrue(wait_until(lambda: bool(dialog.property("opened")), 1500))
        self.create(root, "Banyo")
        banyo = lambda: next((r["id"] for r in bridge.projectRecords if r["name"] == "Banyo"), None)   # noqa: E731
        self.assertTrue(wait_until(lambda: banyo() is not None and bridge.openProject.get("id") == banyo(), 3000))
        self.assertEqual(("projects", chat, mutfak()), (bridge.page, bridge.conversationId, self.owner(store, bridge)))
        self.assertEqual(0, self.execute_calls())

    def test_c_a_dialog_opened_elsewhere_never_takes_over_the_new_chat(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.startNewChat())
        chat = bridge.conversationId
        made = lambda name: next((r["id"] for r in bridge.projectRecords if r["name"] == name), None)   # noqa: E731
        bridge.requestNewProject()
        bridge.dropNewProjectChat()
        bridge.createProject("Bahçe")
        self.assertTrue(wait_until(lambda: made("Bahçe") is not None, 3000))
        bridge.showProject(made("Bahçe"))
        self.assertEqual(("projects", chat), (bridge.page, bridge.conversationId))
        self.assertIsNone(self.owner(store, bridge))
        # An existing project shown while a request is open is not "new": the request just ends.
        bridge.setPage("home")
        bridge.requestNewProject()
        bridge.showProject(self.tez.id)
        self.assertEqual(("projects", self.tez.id, chat), (bridge.page, bridge.openProject["id"], bridge.conversationId))
        bridge.createProject("Çatı")
        self.assertTrue(wait_until(lambda: made("Çatı") is not None, 3000))
        bridge.showProject(made("Çatı"))
        self.assertEqual(("projects", chat), (bridge.page, bridge.conversationId))
        self.assertIsNone(self.owner(store, bridge))
        self.assertEqual(0, self.execute_calls())


class ProjectPageTests(Case):
    def test_d_the_project_page_composer_starts_an_empty_conversation_there(self) -> None:
        store = self.data()
        bridge, root = self.open_window()
        self.assertTrue(bridge.openConversation(self.plain[0].id))
        bridge.showProject(self.tez.id)
        pump(300)
        composer = self.item(root, "projectComposer")
        self.assertEqual("New conversation in Tez", self.item(root, "projectComposerInput").property("placeholderText"))
        self.assertEqual([], [i for i in self.named(composer, "readOnlyBanner") if i.isVisible()])
        before = {c.id for c in store.list_conversations(project_id=self.tez.id)}
        send = self.item(root, "projectNewConversation")
        self.assertTrue(send.isEnabled())                                       # empty is a valid start here
        self.tap(root, send, until=lambda: bridge.page == "home")
        self.assertEqual("home", bridge.page)
        created = [c.id for c in store.list_conversations(project_id=self.tez.id) if c.id not in before]
        self.assertEqual([bridge.conversationId], created)
        self.assertEqual([], list(store.load_conversation_state(bridge.conversationId).turns))
        self.assertEqual(0, self.execute_calls())


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
