"""Gate 6G F8: the sidebar's conversation search (build/6f/F8_DECISIONS.txt S1-S9,
roadmap 3.7 "Arama (F8)", in the 6G sidebar: the entry is the navigation's top row).

S1  A field-like "Sohbetlerde ara" button on top; a click makes it the field, its X
    closes it and the normal list returns (mouse only).
S2  BAŞLIKLAR (name matches) then İÇERİKLER (text matches, ~80-character snippet, the
    hit apart); a title match is not repeated under İÇERİKLER.
S3  Every owner's conversations with their scope; deleted never; archived ones and an
    archived project's are not in the sidebar (V9), so not here either.
S4  Case-insensitive, Turkish-aware (I/ı/İ/i), at least 2 characters, debounced, at
    most 50 results and an honest "daha fazla sonuç var".
S5  A read-only scan through the store's read paths, off the window's thread.
S6  Older compacted turns are found by their digest and marked "eski özet".
S7  A result opens its conversation.
S8  Read-only: the database is byte-identical after a search.
S9  "Aranıyor…" only while a scan runs; "Sonuç yok".

Temp stores only: no test opens the live DB or makes a live model, auth or network call.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from PySide6.QtTest import QTest

from jarvis import conversation_search as cs
from jarvis.app import _redact_text
from jarvis.conversation import RAW_TURNS_MAX, append_turn
from jarvis.memory import SQLiteMemoryStore
from tests.helpers import wait_until
from tests.test_projects_ui_6f import F6Case, dump
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


def say(store, conversation_id: str, *texts: str, role: str = "user") -> None:
    def change(state):
        for text in texts:
            kind = "user" if role == "user" else "reply"
            state = append_turn(state, role=role, kind=kind, text=text, message=text if role == "user" else "",
                                key=f"{role}:{state.next_seq}:{text}")[0]
        return state
    store.update_conversation_state(conversation_id, change)


class MatchingTests(unittest.TestCase):
    """S4: Turkish-aware, case-insensitive; S2: the snippet."""

    def hits(self, query: str, text: str) -> bool:
        return cs.find(cs.pattern(query), cs.normalized(text)) is not None

    def test_plain_lower_and_casefold_are_not_enough(self) -> None:
        # Why the fold exists: Python's own lowering breaks both Turkish pairs.
        self.assertNotIn("istanbul", "İSTANBUL".lower())
        self.assertNotIn("ılık", "ILIK".casefold())

    def test_turkish_i_letters_match_as_a_turkish_reader_expects(self) -> None:
        for query, text in (("istanbul", "İSTANBUL'a gidiyoruz"), ("İstanbul", "istanbul"), ("İSTANBUL", "İstanbul"),
                            ("ılık", "ILIK su"), ("Ilık", "ılık"), ("ILIK", "Ilık"),
                            ("istanbul", "ISTANBUL"), ("api", "the API key"), ("şeker", "ŞEKER"), ("ÇAĞ", "çağ")):
            with self.subTest(query=query, text=text):
                self.assertTrue(self.hits(query, text))
        # The dotted and the dotless letter stay two letters.
        for query, text in (("ılık", "ilik"), ("ilik", "ılık"), ("kır", "kir"), ("kir", "kır"),
                            ("İlik", "ılık"), ("ılık", "İLİK")):
            with self.subTest(query=query, text=text):
                self.assertFalse(self.hits(query, text))

    def test_at_least_two_characters(self) -> None:
        for short in ("", " ", "a", "  ş  ", "İ"):
            self.assertIsNone(cs.pattern(short), short)
        self.assertIsNotNone(cs.pattern("ab"))
        self.assertEqual({"titles": [], "contents": [], "more": False}, cs.search(object(), "a"))

    def test_the_hit_keeps_its_place_and_its_own_letters(self) -> None:
        text = cs.normalized("Yarın  İSTANBUL'da\nbuluşuyoruz")
        span = cs.find(cs.pattern("istanbul"), text)
        self.assertEqual("İSTANBUL", text[span[0]:span[1]])
        piece = cs.snippet(text, span)
        self.assertEqual(("Yarın ", "İSTANBUL", "'da buluşuyoruz"), (piece["before"], piece["hit"], piece["after"]))

    def test_a_snippet_is_about_eighty_characters_around_the_hit(self) -> None:
        text = cs.normalized(" ".join(f"kelime{n}" for n in range(60)) + " bahçe sulama " + " ".join(f"söz{n}" for n in range(60)))
        piece = cs.snippet(text, cs.find(cs.pattern("bahçe"), text))
        self.assertEqual("bahçe", piece["hit"])
        self.assertTrue(piece["before"].startswith("…") and piece["after"].endswith("…"))
        self.assertLessEqual(len(piece["before"] + piece["hit"] + piece["after"]), cs.SNIPPET_CHARS + 2)
        self.assertGreaterEqual(len(piece["before"] + piece["hit"] + piece["after"]), cs.SNIPPET_CHARS - 2)
        # Near the start nothing is cut there; a short text is whole.
        self.assertEqual({"before": "", "hit": "kısa", "after": " not"}, cs.snippet("kısa not", (0, 4)))


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "jarvis.db"
        self.store = SQLiteMemoryStore(self.database)


class ScopeTests(StoreCase):
    def test_titles_then_contents_every_live_owner_never_deleted_or_archived(self) -> None:
        store = self.store
        bahce = store.create_project("Bahçe")
        eski = store.create_project("Eski")
        genel = store.create_conversation(title="Sulama planı")
        project = store.create_conversation(bahce.id, title="Bahçe sulama teklifi")
        content = store.create_conversation(title="Haftalık notlar")
        say(store, content.id, "Damla SULAMA borusu kaç metre?")
        say(store, content.id, "Yaklaşık 40 metre.", role="assistant")
        both = store.create_conversation(title="Sulama ve gübre")
        say(store, both.id, "sulama yine")                       # a title match is not repeated under İÇERİKLER
        archived = store.create_conversation(title="Sulama arşivi")
        store.archive_conversation(archived.id)
        in_old = store.create_conversation(eski.id, title="Sulama eski proje")
        store.set_project_active(eski.id, False)
        gone = store.create_conversation(title="Sulama silinecek")
        say(store, gone.id, "sulama")
        state = store.load_conversation_state(gone.id)
        store.delete_conversation(gone.id, confirm=gone.id, expected_next_seq=state.next_seq, expected_title="Sulama silinecek")

        found = cs.search(store, "SULAMA")
        self.assertEqual([both.id, project.id, genel.id], [r["id"] for r in found["titles"]])   # newest first
        self.assertEqual([content.id], [r["id"] for r in found["contents"]])
        self.assertFalse(found["more"])
        shown = {r["id"] for r in found["titles"] + found["contents"]}
        self.assertFalse(shown & {archived.id, in_old.id, gone.id})
        by_id = {r["id"]: r for r in found["titles"]}
        self.assertEqual(("Bahçe", bahce.id), (by_id[project.id]["project_name"], by_id[project.id]["project_id"]))
        self.assertEqual(("", ""), (by_id[genel.id]["project_name"], by_id[genel.id]["project_id"]))
        hit = found["contents"][0]
        self.assertEqual(("Damla ", "SULAMA", " borusu kaç metre?", False),
                         (hit["before"], hit["hit"], hit["after"], hit["digest"]))
        self.assertEqual("Haftalık notlar", hit["title"])
        # The reply's words are found too, an untitled conversation by its text.
        untitled = store.create_conversation()
        say(store, untitled.id, "Soru", "Cevap: metrekare başına iki damlatıcı", role="assistant")
        self.assertEqual([untitled.id], [r["id"] for r in cs.search(store, "damlatıcı")["contents"]])
        self.assertEqual("", cs.search(store, "damlatıcı")["contents"][0]["title"])

    def test_older_compacted_turns_are_found_by_their_digest_and_marked(self) -> None:
        store = self.store
        long = store.create_conversation(title="Uzun sohbet")
        say(store, long.id, "zeytinyağı fiyatı ne oldu", *[f"tur {n}" for n in range(RAW_TURNS_MAX + 4)])
        state = store.load_conversation_state(long.id)
        self.assertNotIn("zeytinyağı", " ".join(t.text for t in state.turns))
        self.assertIn("zeytinyağı", " ".join(c.digest for c in state.compacted))
        found = cs.search(store, "ZEYTİNYAĞI")["contents"]
        self.assertEqual([(long.id, True, "zeytinyağı")], [(r["id"], r["digest"], r["hit"]) for r in found])
        # A word both in a digest and a full turn: the full turn wins (no "eski özet").
        say(store, long.id, "yine zeytinyağı")
        self.assertFalse(cs.search(store, "zeytinyağı")["contents"][0]["digest"])

    def test_at_most_fifty_and_an_honest_more(self) -> None:
        store = self.store
        for n in range(cs.RESULT_CAP):
            store.create_conversation(title=f"Rapor {n}")
        found = cs.search(store, "rapor")
        self.assertEqual((cs.RESULT_CAP, False), (len(found["titles"]), found["more"]))
        extra = store.create_conversation(title="Haftalık")
        say(store, extra.id, "rapor taslağı")
        found = cs.search(store, "rapor")
        self.assertEqual((cs.RESULT_CAP, 0, True), (len(found["titles"]), len(found["contents"]), found["more"]))
        store.create_conversation(title="Rapor son")
        found = cs.search(store, "rapor")
        self.assertEqual((cs.RESULT_CAP, True), (len(found["titles"]), found["more"]))

    def test_read_only_and_a_secret_is_never_found_or_shown(self) -> None:
        store = self.store
        secret = "sk-ant-api03-" + "x" * 40
        c = store.create_conversation(title="Anahtar")
        say(store, c.id, f"benim anahtarım {secret} burada")
        before = dump(self.database)
        self.assertEqual([], cs.search(store, "xxxxxxxx", redact=_redact_text)["contents"])
        found = cs.search(store, "anahtarım", redact=_redact_text)["contents"]
        self.assertEqual(1, len(found))
        self.assertNotIn(secret, found[0]["before"] + found[0]["hit"] + found[0]["after"])
        self.assertEqual(before, dump(self.database))


class SidebarSearchTests(F6Case):
    def seed(self):
        store = self.db()
        self.bahce = store.create_project("Bahçe")
        self.plan = store.create_conversation(title="Sulama planı")
        self.note = store.create_conversation(self.bahce.id, title="Notlar")
        say(store, self.note.id, "Damla sulama borusu İSTANBUL deposundan gelecek")
        self.other = store.create_conversation(title="Diğer")
        return store

    def entry(self, root):
        return self.item(root, "sidebarSearchEntry")

    def type(self, root, text: str) -> None:
        for char in text:
            QTest.keyClick(root, char)

    def status(self, root) -> str:
        shown = [i for i in self.all_items(root, "sidebarSearchStatus") if i.isVisible()]
        return shown[0].property("text") if shown else ""

    def test_mouse_first_open_type_pick_and_close(self) -> None:
        self.seed()
        bridge = self.bridge()
        root = self.window(bridge)
        pump(100)
        self.assertTrue(self.shown(root, "sidebarSearchLabel"))
        self.assertFalse(self.shown(root, "sidebarSearchField"))
        self.tap(root, self.entry(root), until=lambda: bool(self.shown(root, "sidebarSearchField")))
        field = self.item(root, "sidebarSearchField")
        self.assertTrue(field.property("activeFocus"))
        self.assertTrue(self.shown(root, "sidebarSearchClose"))
        # One character: still the normal list.
        self.type(root, "s")
        pump(400)
        self.assertFalse(self.shown(root, "sidebarSearchResults"))
        self.assertTrue(self.shown(root, "sidebarConversation"))
        # Two and more (debounced): the results take the list's place.
        self.type(root, "ula")
        wait_until(lambda: bool(self.shown(root, "sidebarSearchTitleRow")), 2000)
        self.assertFalse(self.shown(root, "sidebarConversation"))
        self.assertFalse(self.shown(root, "sidebarNewChat"))
        self.assertEqual(["Sulama planı"], [r.property("name") for r in self.shown(root, "sidebarSearchTitleRow")])
        self.assertEqual(["Notlar"], [r.property("name") for r in self.shown(root, "sidebarSearchContentRow")])
        self.assertEqual(["Global"], [r.property("scope") for r in self.shown(root, "sidebarSearchTitleRow")])
        self.assertEqual(["Bahçe"], [r.property("scope") for r in self.shown(root, "sidebarSearchContentRow")])
        self.assertEqual(["sula"], [t.property("text") for t in self.shown(root, "sidebarSearchHit")])
        self.assertEqual(["TITLES"], [t.property("text") for t in self.shown(root, "sidebarSearchTitlesHead")])
        self.assertEqual(["CONTENTS"], [t.property("text") for t in self.shown(root, "sidebarSearchContentsHead")])
        self.assertEqual([], self.shown(root, "sidebarSearchDigest"))
        self.assertEqual([], self.shown(root, "sidebarSearchMore"))
        self.assertEqual("", self.status(root))
        # S7: a content result opens its conversation (scope follows, like a pin).
        self.tap(root, self.shown(root, "sidebarSearchContentRow")[0], until=lambda: bridge.conversationId == self.note.id)
        self.assertEqual(self.note.id, bridge.conversationId)
        self.assertEqual(self.bahce.id, bridge.conversationProject["id"])
        self.assertTrue(self.shown(root, "sidebarSearchContentRow")[0].property("current"))
        # Nothing matches: "No results".
        field.setProperty("text", "zzqq")
        wait_until(lambda: self.status(root) == "No results", 2000)
        self.assertEqual([], self.shown(root, "sidebarSearchTitleRow") + self.shown(root, "sidebarSearchContentRow"))
        # The X closes the field; the normal list returns.
        self.tap(root, self.item(root, "sidebarSearchClose"), until=lambda: not self.shown(root, "sidebarSearchField"))
        self.assertEqual("", field.property("text"))
        self.assertFalse(self.shown(root, "sidebarSearchResults"))
        self.assertTrue(self.shown(root, "sidebarConversation") and self.shown(root, "sidebarNewChat"))
        self.assertTrue(self.shown(root, "sidebarSearchLabel"))

    def test_the_scan_runs_off_the_window_thread_and_searching_is_a_real_state(self) -> None:
        self.seed()
        bridge = self.bridge(sync=False)
        root = self.window(bridge)
        pump(100)
        gate, threads, scan = threading.Event(), [], cs.search

        def held(*args, **kwargs):
            threads.append(threading.current_thread())
            gate.wait(10)
            return scan(*args, **kwargs)
        from unittest import mock
        with mock.patch.object(cs, "search", held):
            self.tap(root, self.entry(root), until=lambda: bool(self.shown(root, "sidebarSearchField")))
            self.type(root, "sulama")
            wait_until(lambda: self.status(root) == "Searching…", 2000)
            self.assertEqual("Searching…", self.status(root))
            self.assertEqual([], self.shown(root, "sidebarSearchTitleRow"))
            gate.set()
            wait_until(lambda: bool(self.shown(root, "sidebarSearchTitleRow")), 3000)
        self.assertEqual("", self.status(root))
        self.assertTrue(threads)
        self.assertTrue(all(t is not threading.main_thread() for t in threads))

    def test_the_turkish_catalog_says_it_in_turkish(self) -> None:
        from jarvis.ui.i18n import localization
        from tests.test_ui import _APP
        self.seed()
        store = self.db()
        old = store.create_conversation(title="Eski")
        say(store, old.id, "eski tohum listesi", *[f"tur {n}" for n in range(RAW_TURNS_MAX + 2)])
        bridge = self.bridge()
        root = self.window(bridge)
        self.addCleanup(lambda: localization(_APP).apply("en-US"))
        bridge.setLanguage("tr-TR")
        pump(300)
        self.assertEqual(["Sohbetlerde ara"], [t.property("text") for t in self.shown(root, "sidebarSearchLabel")])
        self.tap(root, self.entry(root), until=lambda: bool(self.shown(root, "sidebarSearchField")))
        self.type(root, "tohum")
        wait_until(lambda: bool(self.shown(root, "sidebarSearchContentRow")), 2000)
        self.assertEqual(["İÇERİKLER"], [t.property("text") for t in self.shown(root, "sidebarSearchContentsHead")])
        self.assertEqual(["eski özet"], [t.property("text") for t in self.shown(root, "sidebarSearchDigest")])
        self.item(root, "sidebarSearchField").setProperty("text", "sulama")
        wait_until(lambda: bool(self.shown(root, "sidebarSearchTitleRow")), 2000)
        self.assertEqual(["BAŞLIKLAR"], [t.property("text") for t in self.shown(root, "sidebarSearchTitlesHead")])
        self.assertEqual(["Genel"], [r.property("scope") for r in self.shown(root, "sidebarSearchTitleRow")])
        self.item(root, "sidebarSearchField").setProperty("text", "zzqq")
        wait_until(lambda: self.status(root) == "Sonuç yok", 2000)
        self.assertEqual("Sonuç yok", self.status(root))


if __name__ == "__main__":
    unittest.main()
