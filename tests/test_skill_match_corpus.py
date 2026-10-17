"""V2.2 b3: the deterministic matcher over the Turkish/English corpus.

Every request is matched against the whole reference library (every family,
both languages), as Core would. A near-but-wrong request must never be a fit
for any skill, and a request that should match must never fit a different
skill: a false fit makes Core propose the wrong procedure. Recall and the
false-similar rate are printed per family and language, and asserted only so
far as no family may have zero recall.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jarvis.intent import NL_CAPABILITIES
from jarvis.skill_match import MatchKind, SkillWithState, match_skills
from jarvis.skill_run_plan import SkillPlanContext, plan_steps
from jarvis.skill_types import (
    Skill,
    SkillHealth,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
    SkillStep,
)

CORPUS = Path(__file__).parent / "fixtures" / "skill_match_corpus_v22.json"
LANGUAGES = ("tr", "en")
STAMP = "2026-10-15T00:00:00+00:00"
NO_HEALTH = SkillHealth(
    use_count=0,
    success_count=0,
    failure_count=0,
    consecutive_failures=0,
    last_success_at=None,
    last_failure_at=None,
)
CATALOG = {name: hint.action for name, hint in NL_CAPABILITIES.items()}


def build_skill(raw: dict) -> Skill:
    return Skill(
        skill_id=raw["skill_id"],
        version=1,
        name=raw["name"],
        summary=raw["summary"],
        scope=SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None),
        inputs=tuple(SkillInput(name=name, description="", required=True) for name in raw["inputs"]),
        steps=tuple(
            SkillStep(
                index=index,
                action=step["action"],
                capability=step["capability"],
                inputs=tuple(step["inputs"]),
                check=None,
            )
            for index, step in enumerate(raw["steps"], start=1)
        ),
        checks=(),
        provenance=SkillProvenance(
            origin=SkillOrigin.TAUGHT_FROM_RUN,
            source_run_id="run-1",
            parent_skill_id=None,
            parent_version=None,
            created_at=STAMP,
        ),
    )


FAMILIES = json.loads(CORPUS.read_text(encoding="utf-8"))["families"]
SKILLS = {
    (family, lang): build_skill(FAMILIES[family][lang]["skill"])
    for family in FAMILIES
    for lang in LANGUAGES
}
LIBRARY = tuple(
    SkillWithState(skill=skill, status=SkillStatus.ACTIVE, health=NO_HEALTH)
    for skill in SKILLS.values()
)


def fits(text: str) -> list[str]:
    result = match_skills(text, LIBRARY, None)
    return [c.skill_id for c in result.candidates if c.kind is MatchKind.FIT]


class CorpusShapeTest(unittest.TestCase):
    def test_families_come_from_the_planner_catalog(self) -> None:
        self.assertGreaterEqual(len(FAMILIES), 6)
        for (family, lang), skill in SKILLS.items():
            with self.subTest(family=family, lang=lang):
                for step in skill.steps:
                    self.assertIn(step.capability, NL_CAPABILITIES)
                values = {item.name: "sample" for item in skill.inputs}
                plan = plan_steps(skill, values, SkillPlanContext(None, CATALOG))
                self.assertEqual(len(plan), len(skill.steps))
                self.assertTrue(all(step.action for step in plan))

    def test_each_family_has_8_to_10_of_each_kind_in_both_languages(self) -> None:
        for family, by_lang in FAMILIES.items():
            for lang in LANGUAGES:
                for kind in ("match", "near"):
                    with self.subTest(family=family, lang=lang, kind=kind):
                        self.assertTrue(8 <= len(by_lang[lang][kind]) <= 10)


class CorpusMatchTest(unittest.TestCase):
    def test_no_near_but_wrong_request_is_ever_a_fit(self) -> None:
        false_fits = [
            (family, lang, text, found)
            for family, by_lang in FAMILIES.items()
            for lang in LANGUAGES
            for text in by_lang[lang]["near"]
            if (found := fits(text))
        ]
        self.assertEqual(false_fits, [])

    def test_a_matching_request_never_fits_another_skill(self) -> None:
        wrong = [
            (family, lang, text, found)
            for family, by_lang in FAMILIES.items()
            for lang in LANGUAGES
            for text in by_lang[lang]["match"]
            if set(found := fits(text)) - {SKILLS[family, lang].skill_id}
        ]
        self.assertEqual(wrong, [])

    def test_recall_and_false_similar_per_family(self) -> None:
        zero_recall = []
        for family, by_lang in FAMILIES.items():
            parts = []
            for lang in LANGUAGES:
                expected = SKILLS[family, lang].skill_id
                matches = by_lang[lang]["match"]
                near = by_lang[lang]["near"]
                # SKSIMASK: a paraphrase is surfaced as SIMILAR (one question), so
                # recall counts FIT or SIMILAR on the expected skill.
                hits = fit_hits = 0
                for text in matches:
                    best = match_skills(text, LIBRARY, None).best
                    found = best is not None and best.skill_id == expected
                    hits += found and best.kind in (MatchKind.FIT, MatchKind.SIMILAR)
                    fit_hits += found and best.kind is MatchKind.FIT
                similar = sum(
                    match_skills(text, LIBRARY, None).kind is MatchKind.SIMILAR for text in near
                )
                false_fit = sum(bool(fits(text)) for text in near)
                parts.append(
                    f"{lang} recall {hits}/{len(matches)} fit {fit_hits}/{len(matches)} "
                    f"false-similar {similar}/{len(near)} "
                    f"false-fit {false_fit}/{len(near)}"
                )
                if hits == 0:
                    zero_recall.append((family, lang))
            print(f"skill-match corpus {family}: " + "; ".join(parts))
        self.assertEqual(zero_recall, [])

    # SKSIMASK: these "match" rows were FIT before strict binding; each carries a
    # word the skill does not bind, so it is now SIMILAR (Core asks once).
    MOVED_TO_SIMILAR = (
        ("file_summary", "tr", "Lütfen proje dosyasını özetler misin?"),
        ("file_summary", "tr", "İzin verirsen proje dosyasını özetle"),
        ("file_summary", "tr", "proje dosyasını okuyup özetle"),
        ("file_summary", "tr", "proje dosyasini okuyup ozetler misin"),
        ("file_summary", "en", "Can you summarize that project file?"),
        ("code_plan", "tr", "Giriş hatası için bir kod değişikliği planla"),
        ("code_plan", "tr", "Kod değişikliği için bir plan önerir misin?"),
        ("code_plan", "tr", "Ayrıştırıcıdaki kod değişikliğini planla"),
        ("code_plan", "tr", "Kod değişikliğini planlar mısın"),
        ("code_plan", "en", "plan the code change for the login bug"),
        ("code_plan", "en", "Can you plan a code change?"),
        ("code_plan", "en", "propose a plan for the code change to the parser"),
        ("code_plan", "en", "I need a plan for a code change"),
        ("code_plan", "en", "help me plan a code change"),
        ("mail_search", "tr", "E-postada faturaları ara"),
        ("mail_search", "tr", "e-postada faturalari ara"),
        ("mail_search", "tr", "E-postada sözleşmeyi ara"),
        ("mail_search", "tr", "Lütfen e-postada fatura ara"),
        ("mail_search", "tr", "E-postada şu sorguyla ara"),
        ("mail_search", "tr", "E-POSTADA FATURALARI ARA"),
        ("mail_search", "en", "Search my email for invoices"),
        ("mail_search", "en", "Search my email for receipts"),
        ("mail_search", "en", "Please search the email for invoices"),
        ("mail_search", "en", "Search emails for invoices"),
        ("mail_draft", "tr", "Toplantı için e-posta yanıt taslağı yaz"),
        ("mail_draft", "tr", "Tedarikçiye bir yanıt taslağı yazar mısın"),
        ("mail_draft", "tr", "Müşteriye e-posta yanıtı için taslak yaz"),
        ("mail_draft", "tr", "Bu e-postaya yanıt taslağı hazırla"),
        ("mail_draft", "en", "draft an email reply about the meeting"),
        ("mail_draft", "en", "Draft a reply email to the supplier"),
        ("mail_draft", "en", "Write a draft email reply"),
        ("mail_draft", "en", "Prepare an email reply draft"),
        ("mail_draft", "en", "Draft me a quick email reply"),
        ("github_pull_requests", "tr", "Depodaki açık çekme isteklerini listeler misin"),
        ("github_pull_requests", "en", "Show me the open pull requests"),
        ("github_pull_requests", "en", "which pull requests are still open"),
        ("github_commits", "tr", "Son commit'leri listele"),
        ("github_commits", "tr", "Depodaki son commitleri listeler misin"),
        ("github_commits", "tr", "Son on commiti listele"),
        ("github_commits", "en", "can you list the most recent commits"),
        ("web_page_read", "tr", "Şu siteyi açıp sayfasını okur musun"),
        ("web_page_read", "tr", "Web sayfasını açıp okur musun"),
        ("web_page_read", "en", "Open example.com and read the page text"),
        ("app_launch", "tr", "Masaüstündeki Excel uygulamasını başlat"),
        ("app_launch", "tr", "Masaüstü uygulamasını başlatır mısın"),
        ("app_launch", "en", "Launch the desktop application Notepad"),
        ("app_launch", "en", "launch the Spotify desktop application"),
        ("app_launch", "en", "launch desktop application Excel"),
        ("app_launch", "en", "Open and launch the desktop application"),
    )

    def test_a_paraphrase_is_similar_to_its_own_skill(self) -> None:
        for family, lang, text in self.MOVED_TO_SIMILAR:
            with self.subTest(text=text):
                self.assertIn(text, FAMILIES[family][lang]["match"])
                best = match_skills(text, LIBRARY, None).best
                self.assertIsNotNone(best)
                self.assertIs(best.kind, MatchKind.SIMILAR)
                self.assertEqual(best.skill_id, SKILLS[family, lang].skill_id)


def probe_kind(skill_text: str, request: str) -> MatchKind:
    raw = {
        "skill_id": "probe",
        "name": skill_text,
        "summary": skill_text,
        "inputs": [],
        "steps": [{"action": skill_text, "capability": "file_read", "inputs": []}],
    }
    entry = SkillWithState(skill=build_skill(raw), status=SkillStatus.ACTIVE, health=NO_HEALTH)
    return match_skills(request, (entry,), None, request=request).kind


class ReviewProbeTest(unittest.TestCase):
    """INT54 review probes: a negated or object-swapped request is never a fit."""

    NOT_FIT = (
        # B1: swapped or rebound objects and places.
        ("Copy file alpha to folder beta", "Copy file beta to folder alpha"),
        ("Copy file alpha into folder beta", "Copy file beta into folder alpha"),
        ("Move file alpha from folder beta", "Move file beta from folder alpha"),
        ("Copy file alpha to folder beta", "Copy file alpha from folder beta"),
        ("Copy file alpha to folder beta", "File beta is copied to folder alpha"),
        ("alpha dosyasını beta klasörüne kopyala", "beta dosyasını alpha klasörüne kopyala"),
        ("alpha klasöründen beta klasörüne taşı", "beta klasöründen alpha klasörüne taşı"),
        ("alpha'yı beta'ya kopyala", "beta'yı alpha'ya kopyala"),
        ("alpha'yı beta'ya kopyala", "alpha'yı beta'dan kopyala"),
        # B2: a negated skill verb anywhere in the clause.
        ("Dosyayı özetle ve göster", "Dosyayı özetleme ve göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemiyorum ve göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemeden göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemeyeceğim ve göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetleyemem ve göster"),
        # B3: negative contractions, any apostrophe.
        ("Delete the report file", "I can't delete the report file"),
        ("Delete the report file", "I won't delete the report file"),
        ("Delete the report file", "I can" + chr(0x2019) + "t delete the report file"),
        ("Delete the report file", "I can" + chr(0x2BC) + "t delete the report file"),
        ("Delete the report file", "I can`t delete the report file"),
        ("Delete the report file", "I can" + chr(0xB4) + "t delete the report file"),
        ("Delete the report file", "I didn't delete the report file"),
        ("Delete the report file", "The report file isn't to delete"),
        ("Delete the report file", "It doesnt delete the report file"),
        ("Delete the report file", "I cannot delete the report file"),
        # SKMATCHFIX2 review bypasses: rebinding across clauses, case endings
        # without an apostrophe, other apostrophes, other Turkish negations.
        ("Copy file alpha to folder beta and file gamma to folder delta",
         "Copy file alpha to folder delta and file gamma to folder beta"),
        ("Copy file alpha to folder beta", "Copy file alpha to folder beta and file beta to folder alpha"),
        ("alpha dosyasını beta klasörüne kopyala", "beta dosyasından alpha klasörüne kopyala"),
        ("alpha dosyasını beta klasörüne kopyala", "alpha dosyasını beta klasöründen kopyala"),
        ("alpha dosyasını beta klasörüne kopyala", "alpha klasörünü beta dosyasına kopyala"),
        ("alpha dosyasını beta klasörüne kopyala", "alpha dosyasını beta klasöründe kopyala"),
        ("Faturayı müşteriye gönder", "Faturayı müşteriden gönder"),
        ("Raporu müdüre gönder", "Raporu müdürden gönder"),
        ("alpha'yı beta'ya kopyala", "betayı alphaya kopyala"),
        ("alpha'yı beta'ya kopyala", "alpha" + chr(0x2BC) + "yı beta" + chr(0x2BC) + "dan kopyala"),
        ("Copy file alpha to folder beta", "Copy file alpha out of folder beta"),
        ("Copy file alpha to folder beta", "Copy file alpha unto folder beta from folder gamma"),
        ("Delete the report file", "Don" + chr(0x2BB) + "t delete the report file"),
        ("Delete the report file", "Don" + chr(0x2032) + "t delete the report file"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemeksizin göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemesene ve göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemek yok, göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemek yerine göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlemen gerekmez, göster"),
        ("Dosyayı özetle ve göster", "Dosyayı özetlenmesin ve göster"),
        ("Dosyayı sil", "Dosya silinmesin"),
        ("Dosyayı sil", "Dosya silinmemeli"),
        ("Dosyayı sil", "Dosya silinmeyecek"),
        ("Dosyayı sil", "Dosya silinmez"),
        ("Dosyayı kontrol et", "Dosya kontrol edilmesin"),
        ("Dosyayı kontrol et", "Dosyayı kontrol edemem"),
        ("Dosyayı kontrol et", "Dosyayı kontrol etmesene"),
        ("Testleri çalıştır", "Testler çalıştırılmasın"),
        # SKMATCHFIX3 (CDXINT54B M1-M5 + R12 classes): an unbound literal,
        # a negation in another clause, the wider prohibition lexicon and a
        # request too long to read whole.
        ("Send the report file to Alice", "Send the report file to Bob"),
        ("Send the report file to Alice", "Send the report file to Alice and Bob"),
        ("Send the report file to Alice and Bob", "Send the report file to Alice"),
        ("Raporu Ali'ye gönder", "Raporu Veli'ye gönder"),
        ("List the 10 recent commits", "List the 100 recent commits"),
        ("List the 10 recent commits", "List the recent commits"),
        ("List the recent commits", "List the 10 recent commits"),
        ("List the three recent commits", "List the five recent commits"),
        ("3 dosyayı sil", "5 dosyayı sil"),
        ("üç dosyayı sil", "beş dosyayı sil"),
        ("Archive the logs from 2024-01-05", "Archive the logs from 2024-01-06"),
        ("Archive the logs from today", "Archive the logs from tomorrow"),
        ("Bugünkü kayıtları arşivle", "Yarınki kayıtları arşivle"),
        ("Delete \"draft one\" file", "Delete \"draft two\" file"),
        ("Delete 'draft one' file", "Delete 'draft two' file"),
        ("Copy C:/tmp/a.txt to backup", "Copy C:/tmp/b.txt to backup"),
        ("Mail the report to ali@example.com", "Mail the report to veli@example.com"),
        ("Read the page https://example.com/a", "Read the page https://example.com/b"),
        ("Delete file alpha but do not delete file beta", "Do not delete file alpha but delete file beta"),
        ("Delete file alpha and do not delete file beta", "Do not delete file alpha and delete file beta"),
        ("Dosya alpha'yı sil ama dosya beta'yı silme", "Dosya alpha'yı silme ama dosya beta'yı sil"),
        ("Dosya alpha'yı sil fakat dosya beta'yı silme", "Dosya alpha'yı silme fakat dosya beta'yı sil"),
        ("Delete file alpha and keep file beta", "Keep file alpha and delete file beta"),
        ("Draft the reply without sending it", "Draft the reply and send it"),
        ("Rapor dosyasını sil", "Sakın rapor dosyasını sil"),
        ("Rapor dosyasını sil", "Rapor dosyasını asla silme"),
        ("Rapor dosyasını sil", "Rapor dosyasını hiçbir şekilde silme"),
        ("Rapor dosyasını sil", "Rapor dosyasını hicbir sekilde sil"),
        ("Rapor dosyasını sil", "Rapor dosyasını silmeyin"),
        ("Rapor dosyasını sil", "Rapor dosyasını silmeni istemiyorum"),
        ("Rapor dosyasını sil", "Rapor dosyasını sakin sil"),
        ("Dosyayı kontrol et", "Dosyayı kontrol etme"),
        ("Dosyayı kontrol et", "Dosyayı sakın kontrol etme"),
        ("Delete the report file", "Never delete the report file"),
        ("Delete the report file", "Don't ever delete the report file"),
        ("Delete the report file", "Under no circumstances delete the report file"),
        ("Delete the report file", "Avoid deleting the report file"),
        ("Delete the report file", "Refrain from deleting the report file"),
        ("Delete the report file", "Report the file without deleting"),
        ("Delete the report file", "Stop deleting the report file"),
        ("Delete the report file", "Undo delete the report file"),
        ("Delete the report file", "Delete the report file" + " " * 2000 + "Actually, do not delete it"),
        ("Delete the report file", "Delete the report file " + "report " * 60 + "do not delete the report file"),
        ("Delete the report file", "Delete the report file " + "x" * 60),
        # R12 probe hits: a condition, an added action, a repeat, an age, a
        # question or a choice the skill does not carry.
        ("Delete the report file", "Only delete the report file if I say so"),
        ("Delete the report file", "Delete the report file then restore the report file"),
        ("Delete the report file", "Delete the report file twice"),
        ("Delete the report file", "Delete the old report file"),
        ("Rapor dosyasını sil", "Eski rapor dosyasını sil"),
        ("Rapor dosyasını sil", "Rapor dosyasını sil mi?"),
        ("Rapor dosyasını sil", "Rapor dosyasını silsem mi"),
        ("Delete file alpha but do not delete file beta", "Delete file alpha or do not delete file beta"),
    )
    SAME = (
        ("Faturayı müşteriye gönder", "Müşteriye faturayı gönder"),
        ("alpha dosyasını beta klasörüne kopyala", "beta klasörüne alpha dosyasını kopyala"),
        ("alpha'yı beta'ya kopyala", "beta'ya alpha'yı kopyala"),
        ("alpha'yı beta'ya kopyala", "alphayı betaya kopyala"),
        ("Send the invoice to the customer", "Send the customer the invoice"),
    )

    # SKSIMASK residual probes: each must be SIMILAR (asked), never FIT.
    RESIDUAL = (
        ("Delete the report file", "Delete the report file. Just kidding."),
        ("Rapor dosyasını özetle", "Rapor dosyasını özetle, vazgeçtim"),
        ("Delete the report file", "Refuse to delete the report file"),
        ("Rapor dosyasını özetle", "Rapor dosyasını özetlemekten kaçın"),
        ("Rapor dosyasını özetle", "Rapor dosyasının özetlenmesi yasaklandı"),
        ("Rapor dosyasını özetle", "Rapor dosyasını özetlemicem"),
        ("Rapor dosyasını özetle", "Rapor dosyasını özetlemiycem"),
        ("Rapor dosyasını özetle", "Rapor dosyasını özetlemiyom"),
        ("Delete the report file", "Do n​ot delete the report file"),
        ("Delete the report file", "Do nоt delete the report file"),
        ("Upload the report file", "Download the report file"),
        ("Ses seviyesini artır", "Ses seviyesini azalt"),
        ("Delete all report files", "Delete half the report files"),
        ("Yirmi rapor dosyasını sil", "Otuz rapor dosyasını sil"),
        ("Raporu aliye gönder", "Raporu veliye gönder"),
        ("Delete the report file", "Delete the report file and shred the disk"),
        # R12 "15 inputs that break this fix": a truncated request, an
        # antonym suffix, a question, a tense or mood, a determiner, a time
        # word or a longer word; a fit must equal the skill's own text.
        ("Eski dosyaları bul ve sil", "eski dosyaları bul"),
        ("Faturayı indir yazdır ve arşivle", "faturayı indir yazdır"),
        ("Dosyayı şifre koyarak yedekle", "Dosyayı şifresiz yedekle"),
        ("Save the file with password", "Save the file passwordless"),
        ("Delete the report file", "Should I delete the report file?"),
        ("Delete the report file", "How do I delete the report file?"),
        ("Delete the report file", "Why delete the report file?"),
        ("Delete the report file", "Who deleted the report file?"),
        ("Delete the report file", "Delete the report file?"),
        ("Dosyayı özetle", "Dosyayı özetledim"),
        ("Raporu özetle", "Raporu özetlesem"),
        ("Raporu özetle ve sil", "Raporu ne özetle ne de sil"),
        ("Delete the report file", "Who would delete the report file"),
        ("Delete the report file", "Delete any report files"),
        ("Raporu özetle", "Raporu daha önce özetle"),
        ("Raporu özetle", "Raporu artık özetle"),
        ("Delete the report file", "Delete the report file before"),
        ("Delete the report file", "Delete the report file after"),
        ("Delete old files", "Delete old filesystems"),
        # SKSIMASK2 (CDXINT54C A1): an in-word "-", "_" or "." and a
        # non-Turkish accent name a different object.
        ("Delete file alpha-beta", "Delete file alpha_beta"),
        ("Read file résumé.txt", "Read file resume.txt"),
        ("Delete file alpha-beta", "Delete file alpha beta"),
        ("Read file résumé", "Read file resume"),
        ("Delete file alpha_beta", "Delete file alpha-beta"),
        ("Delete file alpha.beta", "Delete file alpha_beta"),
        ("Delete file alpha.beta", "Delete file alpha beta"),
        ("Delete file alpha-beta", "Delete file alphabeta"),
        ("Delete file alpha-beta", "Delete file alpha" + chr(0x2013) + "beta"),
        ("Delete file alpha-beta", "Delete file alpha--beta"),
        ("Delete file alpha-beta", "Delete file alpha-beta-gamma"),
        ("Delete file alpha-beta", "Delete file alpha"),
        ("Delete file notes-v2.txt", "Delete file notes_v2.txt"),
        ("Read file resume", "Read file résumé"),
        ("Read file naïve", "Read file naive"),
        ("Read file café", "Read file cafe"),
        ("Read file Zoë", "Read file Zoe"),
        ("Read file señor", "Read file senor"),
        ("Delete file alpha_beta", "Delete file " + chr(0x430) + "lpha_beta"),
        ("Delete file Ölçek", "Delete file Ölçek" + chr(0x301)),
        # SKSIMASK2 F2: a compatibility form names a different object.
        ("Delete file alpha_beta", "Delete file " + chr(0xFF41) + "lpha_beta"),
        ("Delete file report_1", "Delete file report_" + chr(0x2081)),
        ("Delete file report1", "Delete file report" + chr(0xB9)),
        ("rapor_v2.pdf dosyasını aç", "rapor_v" + chr(0x2461) + ".pdf dosyasını aç"),
        ("Delete file " + chr(0xFB01) + "le1", "Delete file file1"),
        ("Delete file version" + chr(0x2163), "Delete file versioniv"),
        ("Delete file " + chr(0x2460), "Delete file 1"),
        ("Delete file Straße", "Delete file Strasse"),
    )

    # SKSIMASK3 (CDXINT54D M1-M5): any token difference names another literal.
    LITERAL = (
        ("Delete file .env", "Delete file env"),
        ("Delete file alpha+beta", "Delete file alpha=beta"),
        ("Delete file alpha+beta", "Delete file alpha beta"),
        ("Set amount .5", "Set amount 5"),
        ("Set range -5 to +5", "Set range +5 to -5"),
        ("Copy file alpha/beta to alpha@beta", "Copy file alpha@beta to alpha/beta"),
        ("Read file aq" + chr(0x301), "Read file aq"),
        ("Read file report", "Read file report{the}"),
        ('Set label "alpha beta"', 'Set label "alpha  beta"'),
        ("Set amount 5", "Set amount -5"),
        ("Set amount -5", "Set amount +5"),
        ("Set amount 5.0", "Set amount 5,0"),
        ("Delete file a/b", "Delete file a" + chr(0x5C) + "b"),
        ("Delete file a:b", "Delete file ab"),
        ("Delete file report", "Delete file report."),
        ("Delete file report", "Delete file report?"),
        ("Copy file alpha to beta", "Copy file beta to alpha"),
        ("Copy file 1 to 2", "Copy file 2 to 1"),
        ('Set label "alpha beta"', "Set label alpha beta"),
        ('Set label "alpha beta"', 'Set label "alpha" "beta"'),
        ('Set label ""', "Set label"),
        ("Delete file alpha", "Delete file " + chr(0xFF41) + "lpha"),
        ("Delete file alpha", "Delete file alpha alpha"),
        ("Read file report{name}", "Read file report"),
        ("Read file report", "Read file report: other"),
        ("Read file report", "Read file report:other"),
        ("Read file {name}", "Read file {name}"),
    )

    def test_a_different_literal_is_never_a_fit(self) -> None:
        for skill_text, request in self.LITERAL:
            for pair in ((skill_text, request), (request, skill_text)):
                with self.subTest(pair=pair):
                    self.assertIsNot(probe_kind(*pair), MatchKind.FIT)

    def test_a_placeholder_inside_a_word_is_never_said(self) -> None:
        # A stored "file{name}" has no identity a request can spell, even U+FFFF.
        for skill_text, request in (
            ("Copy file{name} to folder backup", "Copy file" + chr(0xFFFF) + " to folder backup"),
            ("Copy file{a} to folder{b}", "Copy file" + chr(0xFFFF) + " to folder" + chr(0xFFFF)),
            ("Copy file{a} to folder{b}", "Copy file{a} to folder{b}"),
        ):
            with self.subTest(request=request):
                self.assertIsNot(probe_kind(skill_text, request), MatchKind.FIT)

    # SKSIMASK4 (Salih, 2026-10-17 "Birebir aynıysa"): only a request that is
    # the skill's own whole text, up to case and Unicode form, fits; CDXINT54E
    # F1/F2, CDXINT54D M1-M5 and CDXINT54C A1 are asked about both ways.
    SAID = (
        ("Delete file C:", "Delete file C"),
        ("Set label x'alpha beta'", "Set label x'alpha  beta'"),
        ("Delete file .env", "Delete  file .env"),
        ("Read file report", "Read file report:"),
        ("Delete file .env", "Delete file env"),
        ("Delete file alpha+beta", "Delete file alpha=beta"),
        ("Set amount .5", "Set amount 5"),
        ("Set range -5 to +5", "Set range +5 to -5"),
        ("Copy file alpha/beta to alpha@beta", "Copy file alpha@beta to alpha/beta"),
        ('Set label "alpha beta"', 'Set label "alpha  beta"'),
        ("Delete file alpha-beta", "Delete file alpha_beta"),
        ("Read file résumé.txt", "Read file resume.txt"),
        # CDXINT54F O1/O2 (Salih A, 2026-10-17): a Turkish mark and an NFC
        # singleton (Kelvin, Ohm, Angstrom sign) are another text.
        ("Şirket raporunu sil", "Sirket raporunu sil"),
        ("arşiv raporunu sil", "arsiv raporunu sil"),
        ("Raporu göster", "Raporu goster"),
        ("Set unit K", "Set unit " + chr(0x212A)),
        ("Set unit " + chr(0x3A9), "Set unit " + chr(0x2126)),
        ("Set unit " + chr(0xC5), "Set unit " + chr(0x212B)),
    )
    # SKSIMASK4 F2: twenty non-case differences that try to fit; all ask.
    PROBE = (
        ("Delete file report", "Delete file report:"),
        ("Delete file report", "Delete file report."),
        ("Delete file report", "Delete file  report"),
        ("Delete file report", "Delete\tfile report"),
        ("Delete file report", "Delete file" + chr(0xA0) + "report"),
        ("Delete file report", "Delete file rep" + chr(0x200B) + "ort"),
        ("Delete file report", "Delete file 'report'"),
        ("Delete file report", "Delete file " + chr(0x201C) + "report" + chr(0x201D)),
        ("Delete file report", "Delete file report!"),
        ("Delete file report", "Delete file r" + chr(0x435) + "port"),
        ("Delete file report", "Delete file " + chr(0xFF52) + "eport"),
        ("Delete file report", "Delete report file"),
        ("Delete file report", "Delete file report now"),
        ("Delete file report", "Delete report"),
        ("Set amount 5", "Set amount " + chr(0x2212) + "5"),
        ("Set amount 5", "Set amount +5"),
        ("Set amount 5", "Set amount 5%"),
        ("Delete file C:", "Delete file C:/"),
        ("Read file {name}", "Read file {name}"),
        ("Read file {name}", "Read file report"),
        # A stored text holds no tab or newline, so these go one way only.
        ("Delete file .env", "Delete  file\t.env"),
        ("Read file report", "Read file report:  \n "),
        # A stacked Turkish mark the fold would drop.
        ("arşiv klasörüne kopyala", "arşiv klasö̈rüne kopyala"),
        ("arşiv klasörüne kopyala", "arş̧iv klasörüne kopyala"),
    )

    def test_any_non_case_difference_is_asked_about(self) -> None:
        for skill_text, request in self.SAID:
            for pair in ((skill_text, request), (request, skill_text)):
                with self.subTest(pair=pair):
                    self.assertIs(probe_kind(*pair), MatchKind.SIMILAR)
        for pair in self.PROBE:
            with self.subTest(pair=pair):
                self.assertIs(probe_kind(*pair), MatchKind.SIMILAR)

    def test_unicode_forms_still_fit(self) -> None:
        for skill_text, request in (
            ("Set range -5 to +5", "Set range -5 to +5"),
            ("Read file report", "  Read file report \n "),
            ('Set label "alpha  beta"', 'Set label "alpha  beta"'),
            ("Read file aq" + chr(0x301), "Read file aq" + chr(0x301)),
            ("Read file " + chr(0xE9), "Read file e" + chr(0x301)),
        ):
            with self.subTest(request=request):
                self.assertIs(probe_kind(skill_text, request), MatchKind.FIT)

    def test_residual_probes_are_similar(self) -> None:
        for pair in self.RESIDUAL:
            with self.subTest(request=pair[1]):
                self.assertIs(probe_kind(*pair), MatchKind.SIMILAR)

    def test_review_probes_are_never_a_fit(self) -> None:
        fits = [pair for pair in self.NOT_FIT if probe_kind(*pair) is MatchKind.FIT]
        self.assertEqual(fits, [])

    def test_a_reordered_request_is_similar(self) -> None:
        # SKSIMASK: a reorder is not the skill's own text, so it is asked about.
        for pair in self.SAME:
            with self.subTest(request=pair[1]):
                self.assertIs(probe_kind(*pair), MatchKind.SIMILAR)

    def test_the_same_request_still_fits(self) -> None:
        for text in ("Copy file alpha to folder beta", "alpha dosyasını beta klasörüne kopyala",
                     "Dosyayı özetle ve göster", "Delete the report file",
                     "List the 10 recent commits", "Send the report file to Alice", "3 dosyayı sil",
                     "Copy C:/tmp/a.txt to backup", "Delete \"draft one\" file", "Raporu Ali'ye gönder",
                     "Archive the logs from 2024-01-05", "Draft the reply without sending it"):
            with self.subTest(text=text):
                self.assertIs(probe_kind(text, text), MatchKind.FIT)

    def test_case_and_turkish_letters_still_fit(self) -> None:
        # SKSIMASK2: identity keeps case folding and the Turkish İ/ı case pairs
        # (CDXINT54F O1: a ş|s pair moved to SAID).
        for skill_text, request in (
            ("Delete file alpha-beta", "Delete file alpha-beta"),
            ("Delete file alpha_beta", "DELETE FILE alpha_beta"),
            ("Read file résumé.txt", "Read file RÉSUMÉ.TXT"),
            ("Read file résumé", "Read file re" + chr(0x301) + "sume" + chr(0x301)),
            ("İstanbul raporunu sil", "istanbul raporunu sil"),
            ("ISTANBUL RAPORUNU SİL", "ıstanbul raporunu sil"),
        ):
            with self.subTest(request=request):
                self.assertIs(probe_kind(skill_text, request), MatchKind.FIT)


if __name__ == "__main__":
    unittest.main()
