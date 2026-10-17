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
                hits = 0
                for text in matches:
                    best = match_skills(text, LIBRARY, None).best
                    hits += best is not None and best.kind is MatchKind.FIT and best.skill_id == expected
                similar = sum(
                    match_skills(text, LIBRARY, None).kind is MatchKind.SIMILAR for text in near
                )
                false_fit = sum(bool(fits(text)) for text in near)
                parts.append(
                    f"{lang} recall {hits}/{len(matches)} false-similar {similar}/{len(near)} "
                    f"false-fit {false_fit}/{len(near)}"
                )
                if hits == 0:
                    zero_recall.append((family, lang))
            print(f"skill-match corpus {family}: " + "; ".join(parts))
        self.assertEqual(zero_recall, [])


def probe_kind(skill_text: str, request: str) -> MatchKind:
    raw = {
        "skill_id": "probe",
        "name": skill_text,
        "summary": skill_text,
        "inputs": [],
        "steps": [{"action": skill_text, "capability": "file_read", "inputs": []}],
    }
    entry = SkillWithState(skill=build_skill(raw), status=SkillStatus.ACTIVE, health=NO_HEALTH)
    return match_skills(request, (entry,), None).kind


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

    def test_review_probes_are_never_a_fit(self) -> None:
        fits = [pair for pair in self.NOT_FIT if probe_kind(*pair) is MatchKind.FIT]
        self.assertEqual(fits, [])

    def test_a_reordered_same_request_still_fits(self) -> None:
        lost = [pair for pair in self.SAME if probe_kind(*pair) is not MatchKind.FIT]
        self.assertEqual(lost, [])

    def test_the_same_request_still_fits(self) -> None:
        for text in ("Copy file alpha to folder beta", "alpha dosyasını beta klasörüne kopyala",
                     "Dosyayı özetle ve göster", "Delete the report file",
                     "List the 10 recent commits", "Send the report file to Alice", "3 dosyayı sil",
                     "Copy C:/tmp/a.txt to backup", "Delete \"draft one\" file", "Raporu Ali'ye gönder",
                     "Archive the logs from 2024-01-05", "Draft the reply without sending it"):
            with self.subTest(text=text):
                self.assertIs(probe_kind(text, text), MatchKind.FIT)


if __name__ == "__main__":
    unittest.main()
