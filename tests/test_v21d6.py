"""V21D6: Gate 6G D-6 finished. Every user-visible project folder message is
a stable English code in Core/connector (app K1/E7, the K3 connector, the
unverified-path label) and is shown in the interface language by the UI
layer (``jarvis.ui.present``, ``jarvis.ui.bridge``); an unknown one is one
generic localized line, never raw. Messages stay data: no flow changes."""

from __future__ import annotations

import inspect
import os
import re
import unittest
from unittest import mock

from jarvis import app, project_folders
from jarvis.connectors import project_folder as connector
from jarvis.ui import present
from jarvis.ui.bridge import Bridge
from jarvis.ui.i18n import localization, render
from tests import test_project_folder_k3 as k3
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import _APP

GENERIC = {"en-US": "The folder step did not complete as expected. Check the project's folder before trying again.",
           "tr-TR": "Klasör adımı beklendiği gibi tamamlanmadı. Yeniden denemeden önce projenin klasörünü kontrol edin."}
TURKISH_LETTERS = re.compile("[çğıöşüÇĞİÖŞÜ]")


def shown(entries: list[dict]) -> str:
    return "\n".join(render(entry["text"]) for entry in entries)


class Languages(unittest.TestCase):
    def each(self) -> tuple[str, ...]:
        """Every interface language; ``speak`` switches to one (English restored afterwards)."""
        self.addCleanup(localization(_APP).apply, "en-US")
        return ("en-US", "tr-TR")

    @staticmethod
    def speak(language: str) -> str:
        localization(_APP).apply(language)
        return language


class _Stub:
    """Only what ``Bridge._k3_switched_to`` and ``_folder_reason`` use."""

    _FOLDER_REASONS, _FOLDER_REASON_PREFIXES = Bridge._FOLDER_REASONS, Bridge._FOLDER_REASON_PREFIXES
    _folder_reason, _k3_switched_to = Bridge._folder_reason, Bridge._k3_switched_to
    _error_message = staticmethod(Bridge._error_message)

    def __init__(self) -> None:
        self.notes: list[tuple] = []

    def _note(self, message, kind: str = "system") -> None:
        self.notes.append((message, kind))


class CodeTests(unittest.TestCase):
    """Core/connector say codes, and every code has its UI mapping."""

    def test_the_connector_and_the_store_say_no_turkish(self) -> None:
        self.assertIsNone(TURKISH_LETTERS.search(inspect.getsource(connector)))
        self.assertIsNone(TURKISH_LETTERS.search(project_folders._UNVERIFIED))

    def test_every_connector_code_is_mapped(self) -> None:
        for code in ("egress_refused", "invalid_task"):
            self.assertIn((code, connector.STALE_PROPOSAL), present.FAILURE_TEXT)
        self.assertIn(("invalid_task", connector.NOTHING_CREATED), present.FAILURE_TEXT)
        self.assertEqual({connector.PARTIAL_LEAF, connector.PARTIAL_PARENT}, set(present.FOLDER_PARTIAL_TEXT))
        self.assertIn(connector.CREATED, present.RESULT_TEXT)
        for summary in connector.PREVIEW.values():
            self.assertIn(summary, present.OUTBOUND_TEXT)
        self.assertEqual(connector.PROJECT_FOLDER_CAPABILITY, present.PROJECT_FOLDER_CAPABILITY)

    def test_every_app_code_is_mapped(self) -> None:
        source = inspect.getsource(app)
        for code in ("The folder path changed; confirm the new path.",
                     "JARVIS is frozen; the folder request was not changed.",
                     "This folder request is no longer pending here; nothing was changed."):
            self.assertIn(f'"{code}"', source)
            self.assertIn(code, Bridge._FOLDER_REASONS)
        self.assertIn(app._NO_FOLDER_PROPOSAL, present.VERDICT_TEXT)
        self.assertIn("Folder creation stopped partway: ", source)


class BridgeTests(Languages):
    def test_k1_codes_in_each_language_and_an_unknown_one_is_generic(self) -> None:
        said = {"en-US": "The folder path changed. Confirm the new path.",
                "tr-TR": "Klasör yolu değişti; yeni yolu onaylayın."}
        for language in map(self.speak, self.each()):
            self.assertEqual(said[language], _Stub()._folder_reason("The folder path changed; confirm the new path."))
            for unknown in ("Something new.", "OSError", "Klasör önerisi değişti; yeniden isteyin."):
                self.assertEqual(GENERIC[language], _Stub()._folder_reason(unknown))

    def test_k1_partial_creation_from_its_fields(self) -> None:
        kept, intended = "C:\\D\\JARVIS Projeleri", "C:\\D\\JARVIS Projeleri\\Salon"
        folder = {"created": [kept, f"{intended} ({project_folders._UNVERIFIED})"], "link_state": "not linked"}
        said = {"en-US": ["Folder creation stopped partway: 2 folder(s) were created and kept (no folder was deleted). "
                          "Project link: not linked.", kept, intended + " (intended path; its location could not be verified)"],
                "tr-TR": ["Klasör oluşturma yarıda kaldı: 2 klasör oluşturuldu ve korundu (klasör silinmedi). "
                          "Proje bağlantısı: bağlı değil.", kept, intended + " (amaçlanan yol; konumu doğrulanamadı)"]}
        unknown = {"en-US": "Project link: unknown (could not be read).", "tr-TR": "Proje bağlantısı: bilinmiyor (okunamadı)."}
        for language in map(self.speak, self.each()):
            self.assertEqual(said[language], Bridge._folder_partial(folder).split("\n"))
            for state in (None, "unknown", "bağlı"):
                self.assertIn(unknown[language], Bridge._folder_partial({**folder, "link_state": state}))

    def test_the_window_s_own_and_the_backend_s_refusals_keep_their_words(self) -> None:
        said = {"en-US": ("The project store is unavailable.", "The stored configuration was refused, so execution is "
                          "disabled. Repair it with 'jarvis config reset --all'."),
                "tr-TR": ("Proje deposuna ulaşılamıyor.", None)}
        for language in map(self.speak, self.each()):
            store, config = (_Stub()._folder_reason(m) for m in Bridge._FOLDER_REASONS if m.startswith(
                ("The project store", "The stored configuration")))
            self.assertEqual(said[language][0], store)
            if language == "en-US":
                self.assertEqual(said[language][1], config)
            else:
                self.assertIn("'jarvis config reset --all'", config)
                self.assertNotIn("The stored configuration", config)

    def test_e7_an_unknown_failure_is_generic_a_stale_one_says_so_and_an_empty_one_says_no_decision(self) -> None:
        stub = _Stub()
        stub._k3_switched_to({"error": {"code": "connector_failed", "message": "Something new."}})
        stub._k3_switched_to({"error": {"code": "stale_grant", "message": "Approval request expired."}})
        stub._k3_switched_to({"error": {"message": ""}})
        said = {"en-US": [GENERIC["en-US"], "Request changed, expired or was resolved elsewhere — decision not recorded",
                          "Decision not recorded"],
                "tr-TR": [GENERIC["tr-TR"], None, None]}
        for language in map(self.speak, self.each()):                         # the notes follow a language switch
            notes = [render(m) for m, _ in stub.notes]
            self.assertEqual(["error"] * 3, [k for _, k in stub.notes])
            for expected, note in zip(said[language], notes):
                self.assertEqual(expected or note, note)
            if language == "tr-TR":
                self.assertFalse({"Something new.", "Approval request expired.", "Decision not recorded"} & set(notes))


class PresentTests(Languages):
    def test_an_unknown_folder_summary_is_generic_and_another_capability_keeps_its_own(self) -> None:
        payload = {"command": "approve", "result": {"summary": "Something new."}}
        for language in map(self.speak, self.each()):
            self.assertEqual(GENERIC[language],
                             shown(present.run_entries(payload, {"capability": present.PROJECT_FOLDER_CAPABILITY})))
            self.assertEqual(GENERIC[language], shown(present.run_entries(
                {**payload, "task": {"capability": present.PROJECT_FOLDER_CAPABILITY}})))
            self.assertEqual("Something new.", shown(present.run_entries(payload, {"capability": "planning"})))

    def test_an_unknown_k3_card_summary_is_generic_and_another_connector_keeps_its_own(self) -> None:
        def card(connector_name: str):
            return present.approval_card({"approval": {"scope": {"connector": connector_name},
                                                       "outbound_preview": {"summary": "Proje klasörü oluştur ve bağla."}}})
        for language in map(self.speak, self.each()):
            self.assertEqual(GENERIC[language], render(card(connector.PROJECT_FOLDER_CONNECTOR)["summary"]))
            self.assertEqual("Proje klasörü oluştur ve bağla.", render(card("planning")["summary"]))
        self.assertEqual(connector.PROJECT_FOLDER_CONNECTOR, present.PROJECT_FOLDER_CONNECTOR)

    def test_the_safety_cards_say_a_known_failure_in_the_interface_language(self) -> None:
        said = {"en-US": "The folder proposal changed. Ask again.", "tr-TR": "Klasör önerisi değişti; yeniden isteyin."}
        for language in map(self.speak, self.each()):
            for code in ("egress_refused", "invalid_task"):
                self.assertEqual(said[language], render(present.failure_message(code, connector.STALE_PROPOSAL)))
            self.assertEqual("Something new.", present.failure_message("invalid_task", "Something new."))


@unittest.skipUnless(k3.h6.WINDOWS, "Windows folder creation")
class EnvelopeTests(k3.K3Case, Languages):
    """The real app's envelopes (fake model, test store), as the window says them."""

    def entries(self, done: dict) -> str:
        """The run's own line (a failure also lists its open issue after it)."""
        return shown(present.run_entries(done)).split("\n")[0]

    def test_k3_card_and_success_in_each_language(self) -> None:
        payload, _ = self.ask(self.p)
        desktop, _ = self.ask(self.q, "desktop")
        cards = {"en-US": ("Create the project folder in Documents\\JARVIS Projeleri and link it. Nothing is sent.",
                           "Create the project folder on the Desktop and link it. Nothing is sent."),
                 "tr-TR": ("Proje klasörünü Belgeler\\JARVIS Projeleri içinde oluştur ve bağla. Hiçbir şey gönderilmez.",
                           "Proje klasörünü Masaüstünde oluştur ve bağla. Hiçbir şey gönderilmez.")}
        for language in map(self.speak, self.each()):
            self.assertEqual(cards[language], tuple(render(present.approval_card(p["run"])["summary"])
                                                    for p in (payload, desktop)))
        done = self.approve(payload)
        self.assertEqual(("completed", connector.CREATED), (done["run_status"], done["result"]["summary"]))
        said = {"en-US": "The project folder was created and linked to the project.",
                "tr-TR": "Proje klasörü oluşturuldu ve projeye bağlandı."}
        for language in map(self.speak, self.each()):
            self.assertEqual(said[language], self.entries(done))

    def test_k3_stale_and_nothing_created_in_each_language(self) -> None:
        payload, path = self.ask(self.p)[0], self.proposed(self.p)
        os.makedirs(path)                                                       # the proposal changed
        stale = self.approve(payload)
        self.assertEqual(connector.STALE_PROPOSAL, stale["error"]["message"])
        payload, _ = self.ask(self.q)
        with mock.patch.object(project_folders, "create_and_link", side_effect=RuntimeError("x")):
            nothing = self.approve(payload)
        self.assertEqual(connector.NOTHING_CREATED, nothing["error"]["message"])
        said = {"en-US": ("The folder proposal changed. Ask again.",
                          "The project folder could not be created. No folder was created and nothing changed."),
                "tr-TR": ("Klasör önerisi değişti; yeniden isteyin.",
                          "Proje klasörü oluşturulamadı; hiçbir klasör oluşturulmadı ve hiçbir şey değişmedi.")}
        for language in map(self.speak, self.each()):
            self.assertEqual(said[language], (self.entries(stale), self.entries(nothing)))

    def test_k3_partial_failures_in_each_language(self) -> None:
        payload, _ = self.ask(self.p)
        real = project_folders._create_child

        def child(parent, name):
            if name != k3.JP:
                raise OSError("no")
            return real(parent, name)

        with mock.patch.object(project_folders, "_create_child", side_effect=child):
            parent = self.approve(payload)                                      # only the shared parent is made
        self.assertEqual(f"{connector.PARTIAL_PARENT} Project link: not linked.", parent["error"]["message"])
        payload, _ = self.ask(self.q)
        k3.h6.CreatePartialFailureTests.insert_fails(self)
        leaf = self.approve(payload)
        self.assertEqual(f"{connector.PARTIAL_LEAF} Project link: not linked.", leaf["error"]["message"])
        said = {"en-US": ("The project folder was created but could not be linked. It was not deleted (its path was on "
                          "the approval card). Project link: not linked.",
                          "Only the shared \"JARVIS Projeleri\" folder was created. The project folder was not, and "
                          "nothing was deleted. Project link: not linked."),
                "tr-TR": ("Proje klasörü oluşturuldu ama projeye bağlanamadı; klasör silinmedi (yolu onay kartındaydı). "
                          "Proje bağlantısı: bağlı değil.",
                          "Yalnızca ortak \"JARVIS Projeleri\" klasörü oluşturuldu; proje klasörü oluşturulmadı ve hiçbir "
                          "şey silinmedi. Proje bağlantısı: bağlı değil.")}
        for language in map(self.speak, self.each()):
            self.assertEqual(said[language], (self.entries(leaf), self.entries(parent)))
        states = {"en-US": ("linked", "unknown (could not be read)"), "tr-TR": ("bağlı", "bilinmiyor (okunamadı)")}
        for language in map(self.speak, self.each()):
            for state, word in zip(("linked", "unknown"), states[language]):
                error = {"code": "invalid_task", "message": f"{connector.PARTIAL_LEAF} Project link: {state}."}
                self.assertTrue(shown(present.error_entries(error)).endswith(f": {word}."))

    def test_k3_refusals_before_any_task_in_each_language(self) -> None:
        with mock.patch.object(self.connector, "proposal", side_effect=ValueError("x")):
            refused, _ = self.ask(self.p)
        self.assertEqual(app._NO_FOLDER_PROPOSAL, refused["intent"]["message"])
        said = {"en-US": "JARVIS could not propose a folder for this project. Nothing was created.",
                "tr-TR": "JARVIS bu proje için bir klasör öneremedi. Hiçbir şey oluşturulmadı."}
        for language in map(self.speak, self.each()):
            self.assertIn(said[language], shown(present.text_entries(refused)))
            if language == "tr-TR":
                self.assertNotIn(app._NO_FOLDER_PROPOSAL, shown(present.text_entries(refused)))
        malformed, _ = self.ask(self.p, target="masaüstü")
        self.assertEqual("malformed_model_output", malformed["intent"]["status"])
        said = {"en-US": "The project folder proposal was refused: it may only say whether the folder goes on the Desktop. "
                         "Nothing was done.",
                "tr-TR": "Proje klasörü önerisi reddedildi: yalnızca klasörün Masaüstüne gidip gitmeyeceğini söyleyebilir. "
                         "Hiçbir şey yapılmadı."}
        for language in map(self.speak, self.each()):
            self.assertIn(said[language], shown(present.text_entries(malformed)))

    def test_e7_failures_in_each_language(self) -> None:
        payload, _ = self.ask(self.p)
        session_id, task_id, request_id, fingerprint = k3.ids(payload)
        gone = self.k3.project_folder_switch(session_id, task_id, request_id, "0" * 64).payload
        self.k3.core.execution_gate = lambda: False
        frozen = self.k3.project_folder_switch(session_id, task_id, request_id, fingerprint).payload
        self.assertEqual(("invalid_grant", "execution_disabled"), (gone["error"]["code"], frozen["error"]["code"]))
        said = {"en-US": ["This folder request is no longer pending here. Nothing was changed.",
                          "JARVIS is frozen; the folder request was not changed."],
                "tr-TR": ["Bu klasör isteği artık burada beklemiyor. Hiçbir şey değiştirilmedi.",
                          "JARVIS donduruldu; klasör isteği değiştirilmedi."]}
        for language in map(self.speak, self.each()):
            stub = _Stub()
            stub._k3_switched_to(gone)
            stub._k3_switched_to(frozen)
            self.assertEqual(said[language], [render(m) for m, _ in stub.notes])


@unittest.skipUnless(k3.h6.WINDOWS, "Windows folder creation")
class SafetyCardTests(k3.h1b.Case, Languages):
    """The real window: the Safety rail's decision and outcome cards (review finding 1)."""

    factory, pending = k3.CardTests.factory, k3.CardTests.pending

    def test_k3_failures_on_the_safety_cards_in_each_language(self) -> None:
        bridge = self.pending()
        shown_card = bridge.approval
        with mock.patch.object(project_folders, "create_and_link", side_effect=RuntimeError("x")):
            bridge.approveRequest(shown_card["request_id"], shown_card["fingerprint"])
        self.assertEqual([], self.made())
        said = {"en-US": "The project folder could not be created. No folder was created and nothing changed.",
                "tr-TR": "Proje klasörü oluşturulamadı; hiçbir klasör oluşturulmadı ve hiçbir şey değişmedi."}
        for language in map(self.speak, self.each()):
            safety = bridge.safety
            self.assertEqual(said[language], safety["outcome"]["message"])
            self.assertIn(safety["decision"].get("message", ""), ("", said[language]))
            self.assertNotIn(connector.NOTHING_CREATED, str(safety))


if __name__ == "__main__":
    unittest.main()
