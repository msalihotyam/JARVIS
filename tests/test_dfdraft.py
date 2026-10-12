"""DFDRAFT dogfood fix (Salih 2026-10-12): "mesaj kutusunda yazilip gonderilmeyen
taslak mesaj butun sohbetler arasi kaliyor ... her sohbetin kendi hafizasinda
kalmali taslaklar."

1. Every conversation has its own draft - the text and its context items
   (quotes with their notes, files); a switch shows the opened one's own, and
   coming back restores the first exactly (text, caret at the end, chips).
2. The New chat screen has its own draft, whichever owner its picker shows;
   its first message makes the conversation and spends the draft.
3. Sending spends only that conversation's draft; a refused send keeps it.
4. The drafts survive a restart (the preferences' drafts file, outside the
   database); a deleted conversation's draft goes; caps; a bad file reads as
   no drafts, never a crash.
5. Local only: typing and switching call no model and run nothing.

The test's own temporary database and preferences only; no live model, auth or network call.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject

from jarvis.ui import preferences
from jarvis.ui.preferences import DRAFT_MAX_BYTES, DRAFT_MAX_COUNT, NEW_CHAT_DRAFT, UiPreferences
from tests.helpers import settled
from tests.test_dfnew import CHAT, say
from tests.test_projects_ui import ProjectsUiCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


class DraftCase(ProjectsUiCase):
    def two(self, *answers):
        store = self.db()
        a, b = store.create_conversation().id, store.create_conversation().id
        say(store, a, "A'nın ilk mesajı")
        say(store, b, "B'nin ilk mesajı")
        bridge = self.bridge(*answers)
        root = self.window(bridge)
        return store, bridge, root, a, b

    def composer(self, root) -> QObject:
        return root.findChild(QObject, "canonicalComposer")

    def type(self, root, text: str) -> None:
        self.composer(root).setProperty("text", text)
        pump(20)

    def text_file(self, name: str, body: str) -> str:
        folder = Path(tempfile.mkdtemp(prefix="jarvis-dfdraft-"))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / name).write_text(body, encoding="utf-8")
        return (folder / name).as_uri()

    def chips(self, bridge) -> tuple:
        return ([(g["reference"]["text"], g["note"]) for g in bridge.contextGroups],
                [f["name"] for f in bridge.globalAttachments])

    def dress(self, bridge, note: str, file: str) -> None:
        """A quote of the open conversation's first message with a note, and a text file."""
        entry = next(e for e in bridge.feed if e["kind"] == "user")
        self.assertTrue(bridge.quoteMessage(entry["id"]))
        bridge.setGroupNote(bridge.contextGroups[0]["id"], note)
        self.assertTrue(bridge.attachFile(self.text_file(file, f"{file} içeriği")))
        pump(50)


class OwnDraftTests(DraftCase):
    def test_a_each_conversation_keeps_its_own_text_and_chips(self) -> None:
        store, bridge, root, a, b = self.two()
        composer = self.composer(root)
        self.assertTrue(bridge.openConversation(a))
        self.type(root, "A için yarım kalan")
        self.dress(bridge, "A notu", "a.txt")
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        self.assertEqual(("", ([], [])), (composer.property("text"), self.chips(bridge)))
        self.type(root, "B için ayrı taslak")
        self.assertTrue(bridge.openConversation(a))
        pump(50)
        self.assertEqual("A için yarım kalan", composer.property("text"))
        self.assertEqual(len("A için yarım kalan"), root.findChild(QObject, "composerInput").property("cursorPosition"))
        self.assertEqual(([("A'nın ilk mesajı", "A notu")], ["a.txt"]), self.chips(bridge))
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        self.assertEqual(("B için ayrı taslak", ([], [])), (composer.property("text"), self.chips(bridge)))
        self.assertEqual([], self.model.calls)                         # local only: nothing was sent
        self.assertEqual(0, self.execute_calls())

    def test_b_the_new_chat_screen_has_its_own_draft_whatever_its_project(self) -> None:
        store, bridge, root, a, b = self.two(CHAT)
        evim = store.create_project("Evim")
        bridge.refreshProjects()
        composer = self.composer(root)
        self.assertTrue(bridge.openConversation(a))
        self.type(root, "A taslağı")
        self.assertTrue(bridge.startNewChat())
        pump(50)
        self.assertEqual("", composer.property("text"))
        self.type(root, "yeni sohbetin taslağı")
        self.assertTrue(bridge.setNewChatProject(evim.id))            # the picker: the same screen, the same draft
        pump(50)
        self.assertEqual("yeni sohbetin taslağı", composer.property("text"))
        self.assertTrue(bridge.openConversation(a))
        pump(50)
        self.assertEqual("A taslağı", composer.property("text"))
        self.assertTrue(bridge.startNewChat())
        pump(50)
        self.assertEqual("yeni sohbetin taslağı", composer.property("text"))
        self.assertTrue(bridge.setNewChatProject(evim.id))            # back from A the screen is Global's again
        pump(50)
        self.assertEqual("yeni sohbetin taslağı", composer.property("text"))
        # Its first message makes the conversation and spends the draft.
        QMetaObject.invokeMethod(composer, "submit")
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        bridge.waitIdle()
        pump(100)
        made = bridge.conversationId
        self.assertEqual(("", evim.id), (composer.property("text"), store.load_conversation(made).project_id))
        self.assertTrue(bridge.startNewChat())
        pump(50)
        self.assertNotEqual(made, bridge.conversationId)
        self.assertEqual("", composer.property("text"))
        self.assertTrue(bridge.openConversation(made))
        pump(50)
        self.assertEqual("", composer.property("text"))
        self.assertTrue(bridge.openConversation(a))
        pump(50)
        self.assertEqual("A taslağı", composer.property("text"))

    def test_c_sending_spends_only_that_conversations_draft_and_a_refusal_keeps_it(self) -> None:
        store, bridge, root, a, b = self.two(CHAT)
        composer = self.composer(root)
        self.assertTrue(bridge.openConversation(b))
        self.type(root, "B bekliyor")
        self.assertTrue(bridge.openConversation(a))
        self.type(root, "A'ya gönder")
        self.dress(bridge, "A notu", "a.txt")
        bridge._ticket = object()                                       # an approval pending: the send is refused
        QMetaObject.invokeMethod(composer, "submit")
        pump(50)
        self.assertEqual(("A'ya gönder", ([("A'nın ilk mesajı", "A notu")], ["a.txt"])),
                         (composer.property("text"), self.chips(bridge)))
        bridge._ticket = None
        QMetaObject.invokeMethod(composer, "submit")
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        bridge.waitIdle()
        pump(100)
        self.assertEqual(("", ([], [])), (composer.property("text"), self.chips(bridge)))
        self.assertTrue(bridge.openConversation(b))
        pump(50)
        self.assertEqual("B bekliyor", composer.property("text"))
        self.assertTrue(bridge.openConversation(a))
        pump(50)
        self.assertEqual(("", ([], [])), (composer.property("text"), self.chips(bridge)))
        self.assertEqual({b}, set(json.loads(bridge.prefs.drafts_path.read_text(encoding="utf-8"))["drafts"]))


class ProjectPageSendTests(DraftCase):
    def test_h_a_project_page_send_carries_no_parked_draft_and_keeps_it(self) -> None:
        # Review MAJOR: the project page's composer opens the project's blank conversation (the New
        # chat key) and sends there; the New chat draft must neither go out with it nor be lost.
        store, bridge, root, a, b = self.two(CHAT)
        evim = store.create_project("Evim")
        bridge.refreshProjects()
        self.assertTrue(bridge.startNewChat())
        self.type(root, "yeni sohbetin taslağı")
        self.assertTrue(bridge.attachFile(self.text_file("n.txt", "yeni")))
        self.assertTrue(bridge.openConversation(a))
        self.type(root, "A taslağı")
        bridge.showProject(evim.id)
        pump(200)
        # Empty, its send only opens the project's New chat screen: released, the screen's own draft is back.
        QMetaObject.invokeMethod(root.findChild(QObject, "projectComposer"), "submit")
        pump(100)
        self.assertEqual(("yeni sohbetin taslağı", ["n.txt"]),
                         (self.composer(root).property("text"), [f["name"] for f in bridge.globalAttachments]))
        self.assertTrue(bridge.openConversation(a))
        bridge.showProject(evim.id)
        pump(200)
        project = next(i for i in self.all_items(root, "projectComposer") if i.isVisible())   # the page loaded again
        project.setProperty("text", "proje için ilk mesaj")
        QMetaObject.invokeMethod(project, "submit")
        settled(600, lambda: self.assertEqual(1, len(self.model.calls)))
        bridge.waitIdle()
        pump(100)
        sent = next(e for e in bridge.feed if e["kind"] == "user")
        self.assertEqual(("proje için ilk mesaj", [], []), (sent["text"], sent.get("attachments"), sent.get("groups")))
        self.assertEqual(("", ([], [])), (bridge.draftText, self.chips(bridge)))
        self.assertTrue(bridge.startNewChat())                          # evim's New chat screen: the draft is whole
        pump(50)
        self.assertEqual(("yeni sohbetin taslağı", ["n.txt"]),
                         (self.composer(root).property("text"), [f["name"] for f in bridge.globalAttachments]))
        self.assertTrue(bridge.openConversation(a))
        pump(50)
        self.assertEqual("A taslağı", self.composer(root).property("text"))


class StoredDraftTests(DraftCase):
    def test_d_a_deleted_conversation_takes_its_draft_with_it(self) -> None:
        store, bridge, root, a, b = self.two()
        self.assertTrue(bridge.openConversation(a))
        self.type(root, "silinecek taslak")
        self.assertTrue(bridge.openConversation(b))
        stored = lambda: set(json.loads(bridge.prefs.drafts_path.read_text(encoding="utf-8"))["drafts"])  # noqa: E731
        self.assertEqual({a}, stored())
        bridge.previewDeleteConversation(a)
        self.assertTrue(bridge.confirmDeleteConversation(a))
        self.assertEqual((set(), {}), (stored(), bridge._drafts))
        # The open one deleted: it is left (the New chat screen) and its draft is not parked anywhere.
        self.assertTrue(bridge.openConversation(b))
        self.type(root, "B de silinecek")
        bridge.previewDeleteConversation(b)
        self.assertTrue(bridge.confirmDeleteConversation(b))
        self.assertNotIn(b, stored() | set(bridge._drafts))
        # Deleted by another path while the window was closed: a restart forgets it.
        bridge.prefs.save_drafts({"c-gone": {"text": "yetim", "mode": "together", "at": 1, "groups": [], "attachments": []}})
        fresh = self.bridge()
        self.assertNotIn("c-gone", fresh._drafts)

    def test_e_drafts_survive_a_restart(self) -> None:
        store, bridge, root, a, b = self.two()
        self.assertTrue(bridge.openConversation(a))
        self.type(root, "A, yeniden başlatmadan sonra")
        self.dress(bridge, "A notu", "a.txt")
        self.assertTrue(bridge.startNewChat())
        self.type(root, "yeni sohbet, yeniden başlatmadan sonra")
        self.assertTrue(bridge.openConversation(b))
        self.type(root, "B, kaydırılmadan")                            # live, never switched away: the quit writes it
        bridge.confirmQuit()
        bridge.shutdown()

        again = self.bridge()
        root = self.window(again)
        composer = self.composer(root)
        self.assertTrue(again.openConversation(a))
        pump(50)
        self.assertEqual("A, yeniden başlatmadan sonra", composer.property("text"))
        self.assertEqual(([("A'nın ilk mesajı", "A notu")], ["a.txt"]), self.chips(again))
        self.assertEqual("a.txt içeriği", again.globalAttachments[0]["text"])
        self.assertTrue(again.openConversation(b))
        pump(50)
        self.assertEqual(("B, kaydırılmadan", ([], [])), (composer.property("text"), self.chips(again)))
        self.assertTrue(again.startNewChat())
        pump(50)
        self.assertEqual("yeni sohbet, yeniden başlatmadan sonra", composer.property("text"))
        # A restored chip is ordinary draft context: its ids are this run's, and it can be removed. Its
        # quote's feed entry id was the last run's, so it marks no message now.
        self.assertTrue(again.openConversation(a))
        self.assertEqual("", again.contextGroups[0]["reference"]["source_id"])
        group = again.contextGroups[0]["id"]
        again.removeGroup(group)
        self.assertEqual([], again.contextGroups)

    def test_f_a_bad_drafts_file_reads_as_no_drafts_and_never_crashes(self) -> None:
        path = UiPreferences(self.prefs_path).drafts_path
        path.parent.mkdir(parents=True, exist_ok=True)
        for bad in (b"{not json", b"\xff\xfe\x00", b"[1, 2]", json.dumps({"version": 9, "drafts": {}}).encode(),
                    json.dumps({"version": 1, "drafts": {"c-1": {"text": 5}}}).encode(), b"[" * 100_000):
            path.write_bytes(bad)
            bridge = self.bridge()
            self.assertEqual(({}, ""), (bridge._drafts, bridge.draftText))
        # One malformed entry is dropped; a good one beside it stays.
        good = {"text": "iyi", "mode": "together", "at": 2, "groups": [], "attachments": []}
        path.write_text(json.dumps({"version": 1, "drafts": {"c-bad": {**good, "groups": [{"reference": 1}]},
                                                                "c-good": good}}), encoding="utf-8")
        self.assertEqual({"c-good"}, set(UiPreferences(self.prefs_path).load_drafts()))
        # Review MAJOR: a lone surrogate (valid JSON, not encodable) - in the file or in a live draft.
        path.write_text(json.dumps({"version": 1, "drafts": {"c-1": {**good, "text": "a\ud800b"}}}), encoding="utf-8")
        self.assertEqual("a\ud800b", UiPreferences(self.prefs_path).load_drafts()["c-1"]["text"])
        bridge = self.bridge()
        bridge.setDraftText("yarım \udfff kaldı")
        bridge._save_drafts()
        self.assertEqual("yarım \udfff kaldı", UiPreferences(self.prefs_path).load_drafts()[NEW_CHAT_DRAFT]["text"])


class DraftStoreTests(DraftCase):
    def test_g_the_size_and_count_caps(self) -> None:
        prefs = UiPreferences(self.prefs_path)
        quote = {"reference": {"source_role": "user", "source_id": "e1", "source_time": "0", "selection": "full",
                               "text": "alıntı"}, "note": "not", "attachments": []}
        big = {"text": "ş" * 100_000, "mode": "together", "at": 1, "groups": [quote], "attachments": []}
        many = {f"c-{i}": {"text": f"t{i}", "mode": "together", "at": 10 + i, "groups": [], "attachments": []}
                for i in range(DRAFT_MAX_COUNT + 10)}
        self.assertTrue(prefs.save_drafts({"c-big": big, **many}))
        loaded = prefs.load_drafts()
        self.assertEqual(DRAFT_MAX_COUNT, len(loaded))                 # the newest only: the big, oldest one went
        self.assertNotIn("c-big", loaded)
        self.assertEqual({f"c-{i}" for i in range(10, DRAFT_MAX_COUNT + 10)}, set(loaded))
        fitted = preferences.stored_draft(big)
        self.assertLessEqual(len(json.dumps(fitted)), DRAFT_MAX_BYTES)
        self.assertEqual([], fitted["groups"])                         # the quotes go ...
        self.assertTrue("ş" * 1000 in fitted["text"] and big["text"].startswith(fitted["text"]))   # ... then the tail
        # Review MINOR: the text files go first, largest first; the quote and its note stay.
        def file(name, size):
            return {"kind": "text", "name": name, "text": "x" * size}
        heavy = {"text": "kısa", "mode": "together", "at": 1, "attachments": [file("g25", 25_000), file("g10", 10_000)],
                 "groups": [{**quote, "attachments": [file("q20", 20_000), file("q15", 15_000)]}]}
        fitted = preferences.stored_draft(heavy)
        self.assertEqual(("kısa", "not", ["g10"], ["q20", "q15"]),
                         (fitted["text"], fitted["groups"][0]["note"], [f["name"] for f in fitted["attachments"]],
                          [f["name"] for f in fitted["groups"][0]["attachments"]]))
        # Never stored: ids, snapshot paths, images; an empty draft is no draft.
        image = {"id": "att-1", "kind": "image", "name": "x.png", "path": "C:/x.png"}
        text = {"id": "att-2", "kind": "text", "name": "a.txt", "text": "içerik", "path": "C:/a.txt"}
        kept = preferences.stored_draft({"text": "", "mode": "together", "at": 1, "groups": [],
                                         "attachments": [image, text]})
        self.assertEqual([{"type": "attachment", "kind": "text", "scope": "chat", "name": "a.txt", "text": "içerik"}],
                         kept["attachments"])
        self.assertIsNone(preferences.stored_draft({"text": "", "mode": "together", "at": 1, "groups": [],
                                                    "attachments": [image]}))
