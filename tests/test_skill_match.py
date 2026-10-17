"""Tests for jarvis.skill_match: deterministic, scoped fit / similar / none."""

from __future__ import annotations

import ast
import dataclasses
import itertools
import random
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import skill_match as sm
from jarvis.skill_match import (
    MatchKind,
    MatchResult,
    SkillMatchError,
    SkillWithState,
    cut_text,
    match_skills,
    words,
)
from jarvis.skill_types import (
    FORBIDDEN_KEY_PATTERN,
    PROJECT_ID_PATTERN,
    TEXT_MAX,
    Skill,
    SkillHealth,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
    SkillStep,
    fold_token,
)

STAMP = "2026-10-14T09:00:00+00:00"
NO_HEALTH = SkillHealth(
    use_count=0,
    success_count=0,
    failure_count=0,
    consecutive_failures=0,
    last_success_at=None,
    last_failure_at=None,
)


def make_skill(
    skill_id: str,
    name: str,
    summary: str,
    steps: tuple[str, ...] = ("Adımı uygula",),
    *,
    project_id: str | None = None,
    inputs: tuple[str, ...] = (),
) -> Skill:
    scope = (
        SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
        if project_id is None
        else SkillScope(kind=SkillScopeKind.PROJECT, project_id=project_id)
    )
    return Skill(
        skill_id=skill_id,
        version=1,
        name=name,
        summary=summary,
        scope=scope,
        inputs=tuple(SkillInput(name=item, description="", required=True) for item in inputs),
        steps=tuple(
            SkillStep(index=i, action=text, capability=None, inputs=(), check=None)
            for i, text in enumerate(steps, start=1)
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


def health(successes: int, failures: int = 0, last_success: str | None = STAMP) -> SkillHealth:
    return SkillHealth(
        use_count=successes + failures,
        success_count=successes,
        failure_count=failures,
        consecutive_failures=0,
        last_success_at=last_success if successes else None,
        last_failure_at=STAMP if failures else None,
    )


def entry(
    skill: Skill,
    status: SkillStatus = SkillStatus.ACTIVE,
    skill_health: SkillHealth = NO_HEALTH,
) -> SkillWithState:
    return SkillWithState(skill=skill, status=status, health=skill_health)


REPORT_TR = make_skill(
    "satis-raporu",
    "Haftalık satış raporu",
    "Haftalık satış raporunu hazırlar",
    ("Satış verilerini indir", "Raporu hazırla", "Raporu e-posta ile gönder"),
)
INVOICE_EN = make_skill(
    "invoice-export",
    "Export monthly invoices",
    "Download and export monthly invoices",
    ("Download invoices from the portal", "Export the spreadsheet"),
    inputs=("customer_name",),
)
BACKUP_EN = make_skill(
    "db-backup",
    "Database backup",
    "Back up the database to the archive folder",
    ("Dump the database", "Copy the dump to the archive folder"),
)
LIBRARY = (REPORT_TR, INVOICE_EN, BACKUP_EN)


def run(task: str, *skills: Skill, project_id: str | None = None) -> MatchResult:
    return match_skills(task, [entry(skill) for skill in (skills or LIBRARY)], project_id)


class NormalisationTests(unittest.TestCase):
    def test_turkish_i_forms_fold_to_one_letter(self) -> None:
        for text in ("İNDİR", "indir", "ındır", "INDIR", "İndir"):
            with self.subTest(text=text):
                self.assertEqual(words(text), ("indir",))

    def test_turkish_accents_and_width_fold(self) -> None:
        self.assertEqual(words("ŞÇĞÖÜ şçğöü"), ("scgou",))
        self.assertEqual(words(chr(0xFF32) + "APOR"), ("rapor",))  # fullwidth R

    def test_stop_words_numbers_and_short_words_are_dropped(self) -> None:
        self.assertEqual(
            words("Lütfen bana bu raporu ve 2026 verisini hazırla, JARVIS"),
            ("raporu", "verisini", "hazirla"),
        )
        self.assertEqual(words("Please export the invoices for me"), ("export", "invoices"))

    def test_words_are_unique_in_first_seen_order_and_bounded(self) -> None:
        self.assertEqual(words("rapor Rapor RAPOR fatura"), ("rapor", "fatura"))
        many = " ".join(f"kelime{chr(0x61 + i // 26)}{chr(0x61 + i % 26)}" for i in range(200))
        self.assertEqual(len(words(many, sm.MAX_TASK_WORDS)), sm.MAX_TASK_WORDS)
        self.assertEqual(len(words("a" * 500)[0]), sm.WORD_MAX)

    def test_task_text_is_capped(self) -> None:
        padding = "x " * (sm.MAX_TASK_CHARS // 2)
        result = run(padding + "haftalık satış raporu")
        self.assertIs(result.kind, MatchKind.NONE)
        self.assertIs(run("haftalık satış raporu").kind, MatchKind.FIT)

    def test_words_are_fold_token_words(self) -> None:
        # D13: one fold. Every word is already canonical, and equals the
        # skill_types fold of the text it came from.
        samples = [
            "HAFTALIK Satış RAPORU",
            "İSTANBUL ıspanak",
            chr(0xFF24) + "EPLOY",
            "Straße",
        ]
        rng = random.Random(22)
        alphabet = [chr(code) for code in range(0x20, 0x2FFF)] + [chr(0xFDFA), chr(0x0307)]
        samples += ["".join(rng.choice(alphabet) for _ in range(30)) for _ in range(500)]
        for text in samples:
            with self.subTest(text=text):
                for word in words(text):
                    self.assertEqual(fold_token(word), word)
                    self.assertIn(word, fold_token(text))
        self.assertEqual(words("Haftalık HAFTALIK haftal" + chr(0x131) + "k"), ("haftalik",))


class StopWordTests(unittest.TestCase):
    def test_turkish_content_words_survive_the_stop_list(self) -> None:
        cases = (
            ("iş planı hazırla", ("is", "plani", "hazirla")),
            ("is plani hazirla", ("is", "plani", "hazirla")),
            ("su faturasını öde", ("su", "faturasini", "ode")),
            ("Şu faturayı öde", ("su", "faturayi", "ode")),
            ("can sıkıntısı notu", ("can", "sikintisi", "notu")),
            ("sunu dosyasını aç", ("sunu", "dosyasini", "ac")),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(words(text), expected)

    def test_no_stop_word_folds_to_a_kept_content_word(self) -> None:
        self.assertEqual(sm.STOP_WORDS & sm.KEPT_CONTENT_WORDS, frozenset())
        self.assertTrue(all(fold_token(word) == word for word in sm.STOP_WORDS))
        for word in ("ve", "ile", "icin", "the", "and", "please", "jarvis"):
            self.assertIn(word, sm.STOP_WORDS)

    def test_water_bill_task_prefers_the_water_bill_skill(self) -> None:
        water = make_skill(
            "su-faturasi", "Su faturası", "Su faturasını öder", ("Su faturasını öde",)
        )
        power = make_skill(
            "elektrik-faturasi",
            "Elektrik faturası",
            "Elektrik faturasını öder",
            ("Elektrik faturasını öde",),
        )
        # SKSIMASK: "öde" is not the skill's own text ("öder"): only similar.
        result = run("su faturasını öde", water, power)
        self.assertEqual(result.best.skill_id, "su-faturasi")
        self.assertIs(result.best.kind, MatchKind.SIMILAR)
        self.assertEqual(result.best.score, 100)
        self.assertEqual([c.skill_id for c in result.candidates][1:], ["elektrik-faturasi"])
        self.assertIs(result.candidates[1].kind, MatchKind.SIMILAR)


class CutTests(unittest.TestCase):
    def test_cut_never_leaves_a_fragment_of_a_word(self) -> None:
        self.assertEqual(cut_text("rapor hazirla", 9), "rapor ")
        self.assertEqual(cut_text("rapor hazirla", 6), "rapor ")
        self.assertEqual(cut_text("rapor hazirla", 5), "rapor")  # the cut is at a space
        self.assertEqual(cut_text("rapor hazirla", 3), "")
        self.assertEqual(cut_text("rapor", 10), "rapor")
        # A combining mark continues the word it follows.
        self.assertEqual(cut_text("rapor s" + chr(0x0327) + "u", 7), "rapor ")

    def test_long_task_cut_drops_the_partial_last_word(self) -> None:
        task = "rapor " * 1_000_000
        self.assertNotEqual(sm.MAX_TASK_CHARS % len("rapor "), 0)  # the cut is mid-word
        self.assertEqual(words(cut_text(task, sm.MAX_TASK_CHARS)), ("rapor",))
        best = run(task, REPORT_TR).best
        self.assertEqual(best.score, 100)
        self.assertIn("score 100: 1 of 1 task words matched", best.reasons)


class TableTests(unittest.TestCase):
    TABLE = (
        # (task, expected kind, expected best skill id or None)
        # SKSIMASK: only the skill's own name or summary text is a fit; a
        # paraphrase of it is similar (Core asks once).
        ("Haftalık satış raporunu hazırla ve gönder", MatchKind.SIMILAR, "satis-raporu"),
        ("Haftalık satış raporunu hazırlar", MatchKind.FIT, "satis-raporu"),
        ("HAFTALIK SATIŞ RAPORU", MatchKind.FIT, "satis-raporu"),
        ("haftalik satis raporu", MatchKind.FIT, "satis-raporu"),
        ("Satış raporu", MatchKind.SIMILAR, "satis-raporu"),
        ("Müşteri listesini temizle ve satış raporu ekle", MatchKind.SIMILAR, "satis-raporu"),
        ("Rapor", MatchKind.SIMILAR, "satis-raporu"),
        ("Bugün hava nasıl olacak?", MatchKind.NONE, None),
        ("Export the monthly invoices", MatchKind.SIMILAR, "invoice-export"),
        ("Export monthly invoices", MatchKind.FIT, "invoice-export"),
        ("Download and export monthly invoices", MatchKind.FIT, "invoice-export"),
        # Two of the four name and summary words: coverage 50, not a fit.
        ("Please download invoices", MatchKind.SIMILAR, "invoice-export"),
        ("Please download the monthly invoices", MatchKind.SIMILAR, "invoice-export"),
        ("Back up the database", MatchKind.SIMILAR, "db-backup"),
        ("Back up the database to the archive folder", MatchKind.FIT, "db-backup"),
        ("Print the invoices list", MatchKind.SIMILAR, "invoice-export"),
        ("What is the weather tomorrow?", MatchKind.NONE, None),
        ("", MatchKind.NONE, None),
        ("ve ile the and 2026", MatchKind.NONE, None),
    )

    def test_table_tr_and_en(self) -> None:
        for task, kind, best in self.TABLE:
            with self.subTest(task=task):
                result = run(task)
                self.assertIs(result.kind, kind)
                if best is None:
                    self.assertIsNone(result.best)
                    self.assertEqual(result.candidates, ())
                else:
                    self.assertIsNotNone(result.best)
                    self.assertEqual(result.best.skill_id, best)
                    self.assertIs(result.best, result.candidates[0])
                    self.assertIs(result.best.kind, kind)

    def test_one_word_task_is_never_a_fit(self) -> None:
        result = run("raporu", make_skill("r", "Rapor", "Rapor"))
        self.assertIs(result.kind, MatchKind.SIMILAR)
        self.assertEqual(result.best.score, 100)
        self.assertIn("not a fit: fewer than 2 matched words", result.best.reasons)

    def test_fit_needs_a_name_or_summary_word(self) -> None:
        skill = make_skill("x", "Genel bakım", "Sistem işleri", ("Arşiv klasörünü sıkıştır",))
        result = run("arşiv klasörünü sıkıştır", skill)
        self.assertIs(result.kind, MatchKind.SIMILAR)
        self.assertEqual(result.best.score, 100)
        self.assertIn("not a fit: no task word in its name or summary", result.best.reasons)

    def test_input_names_are_matched(self) -> None:
        result = run("customer name export", INVOICE_EN)
        self.assertIs(result.kind, MatchKind.SIMILAR)  # coverage 25: one of four
        self.assertEqual(result.best.score, 100)
        self.assertIn("matched words: customer, name, export", result.best.reasons)
        self.assertIn("not a fit: coverage below 60", result.best.reasons)

    def test_suffixed_forms_match_and_short_words_do_not_prefix_match(self) -> None:
        self.assertEqual(run("raporunu gönderir", REPORT_TR).best.score, 100)
        self.assertEqual(run("sends invoices", INVOICE_EN).best.score, 50)  # "send" not in skill
        self.assertIs(run("ara", make_skill("a", "Arama", "Arşivde arama")).kind, MatchKind.NONE)

    def test_reasons_are_bounded_and_explain(self) -> None:
        result = run("Haftalık satış raporunu hazırla")
        reasons = result.best.reasons
        self.assertEqual(reasons[0], "scope: global")
        self.assertEqual(reasons[1], "score 100: 4 of 4 task words matched")
        self.assertTrue(all(len(reason) <= 500 for reason in reasons))

    def test_thresholds_are_module_constants(self) -> None:
        for name in (
            "FIT_MIN_SCORE",
            "FIT_MIN_MATCHED",
            "SIMILAR_MIN_SCORE",
            "SIMILAR_MIN_MATCHED",
            "MAX_CANDIDATES",
            "MAX_TASK_CHARS",
            "MAX_TASK_WORDS",
            "PREFIX_MIN",
            "MAX_SKILLS",
        ):
            self.assertIs(type(getattr(sm, name)), int, name)
        self.assertLess(sm.SIMILAR_MIN_SCORE, sm.FIT_MIN_SCORE)
        self.assertGreaterEqual(sm.FIT_MIN_MATCHED, 2)


class ScopeTests(unittest.TestCase):
    TASK = "Proje dağıtım notları"  # SKSIMASK: the skill's own name, so a fit

    def setUp(self) -> None:
        self.p1 = make_skill(
            "p1-notes",
            "Proje dağıtım notları",
            "Dağıtım notlarını derler",
            ("Notları derle",),
            project_id="proj-a",
        )
        self.p1_similar = make_skill(
            "p1-other",
            "Proje dağıtım",
            "Sunucu işleri",
            ("Sunucuyu yeniden başlat",),
            project_id="proj-a",
        )
        self.p1_stale = make_skill(
            "p1-stale",
            "Proje dağıtım notları eski",
            "Dağıtım notlarını derler",
            project_id="proj-a",
        )
        self.offer = [
            entry(self.p1),
            entry(self.p1_similar),
            entry(self.p1_stale, SkillStatus.STALE),
        ]

    def _ids(self, result: MatchResult) -> set[str]:
        return {c.skill_id for c in result.candidates + result.needs_revalidation} | (
            set() if result.best is None else {result.best.skill_id}
        )

    def test_cross_project_skill_never_appears(self) -> None:
        for project_id in ("proj-b", None):
            with self.subTest(project_id=project_id):
                result = match_skills(self.TASK, self.offer, project_id)
                self.assertIs(result.kind, MatchKind.NONE)
                self.assertIsNone(result.best)
                self.assertEqual(result.candidates, ())
                self.assertEqual(result.needs_revalidation, ())
                self.assertEqual(self._ids(result), set())

    def test_same_project_skill_is_offered(self) -> None:
        result = match_skills(self.TASK, self.offer, "proj-a")
        self.assertIs(result.kind, MatchKind.FIT)
        self.assertEqual(result.best.skill_id, "p1-notes")
        self.assertEqual(result.best.reasons[0], "scope: project")
        self.assertEqual(self._ids(result), {"p1-notes", "p1-other", "p1-stale"})

    def test_global_skill_is_offered_in_every_project(self) -> None:
        for project_id in (None, "proj-a", "proj-b"):
            with self.subTest(project_id=project_id):
                result = match_skills("satış raporu hazırla", [entry(REPORT_TR)], project_id)
                self.assertEqual(result.best.skill_id, "satis-raporu")

    def test_mixed_offer_keeps_only_global_for_another_project(self) -> None:
        result = match_skills(
            self.TASK + " satış raporu", self.offer + [entry(REPORT_TR)], "proj-b"
        )
        self.assertEqual(self._ids(result), {"satis-raporu"})


class StatusTests(unittest.TestCase):
    TASK = "Haftalık satış raporunu hazırlar"  # SKSIMASK: the skill's own summary

    def test_disabled_and_deleted_are_never_offered(self) -> None:
        for status in (SkillStatus.DISABLED, SkillStatus.DELETED):
            with self.subTest(status=status):
                result = match_skills(self.TASK, [entry(REPORT_TR, status)], None)
                self.assertIs(result.kind, MatchKind.NONE)
                self.assertEqual(result.candidates, ())
                self.assertEqual(result.needs_revalidation, ())

    def test_stale_is_only_in_needs_revalidation_and_never_a_fit(self) -> None:
        result = match_skills(self.TASK, [entry(REPORT_TR, SkillStatus.STALE)], None)
        self.assertIs(result.kind, MatchKind.NONE)
        self.assertIsNone(result.best)
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.needs_revalidation), 1)
        stale = result.needs_revalidation[0]
        self.assertEqual(stale.skill_id, "satis-raporu")
        self.assertIs(stale.kind, MatchKind.SIMILAR)
        self.assertEqual(stale.reasons[-1], "stale: needs revalidation before use (would be fit)")

    def test_stale_does_not_hide_an_active_fit_and_is_not_best(self) -> None:
        twin = make_skill("satis-raporu-2", REPORT_TR.name, REPORT_TR.summary, ("Raporu hazırla",))
        result = match_skills(
            self.TASK, [entry(REPORT_TR, SkillStatus.STALE, health(9)), entry(twin)], None
        )
        self.assertIs(result.kind, MatchKind.FIT)
        self.assertEqual(result.best.skill_id, "satis-raporu-2")
        self.assertEqual([c.skill_id for c in result.candidates], ["satis-raporu-2"])
        self.assertEqual([c.skill_id for c in result.needs_revalidation], ["satis-raporu"])

    def test_unmatched_stale_is_not_listed(self) -> None:
        result = match_skills("hava durumu", [entry(REPORT_TR, SkillStatus.STALE)], None)
        self.assertEqual(result.needs_revalidation, ())


class OrderTests(unittest.TestCase):
    TASK = "satış raporu hazırla"

    def _same(self, skill_id: str) -> Skill:
        return make_skill(skill_id, "Satış raporu", "Satış raporunu hazırlar")

    def test_order_score_then_rate_then_last_success_then_id(self) -> None:
        offer = [
            entry(self._same("d-low-rate"), skill_health=health(1, 3)),
            entry(self._same("c-old"), skill_health=health(2, 0, "2026-10-01T09:00:00+00:00")),
            entry(self._same("b-new"), skill_health=health(2, 0, "2026-10-13T09:00:00.500000+00:00")),
            entry(self._same("e-same-as-b"), skill_health=health(2, 0, "2026-10-13T09:00:00.500000+00:00")),
            entry(make_skill("f-partial", "Satış", "Satış işleri", ("Listeyi hazırla",))),
        ]
        expected = ["b-new", "e-same-as-b", "c-old", "d-low-rate", "f-partial"]
        result = match_skills(self.TASK, offer, None)
        self.assertEqual([c.skill_id for c in result.candidates], expected)
        self.assertEqual([c.score for c in result.candidates], [100, 100, 100, 100, 66])
        for permutation in itertools.permutations(offer):
            with self.subTest(order=[e.skill.skill_id for e in permutation]):
                self.assertEqual(match_skills(self.TASK, permutation, None), result)

    def test_fit_comes_before_a_higher_scoring_similar(self) -> None:
        # SKSIMASK: a fit binds every request word, so it scores 100 and a
        # similar skill with the same score still ranks after it.
        no_headline = make_skill("a-steps", "Genel", "Genel işler", ("Satış raporu hazırla",))
        fit = make_skill("z-fit", "Satış raporu hazırla", "Rapor", ("Belgeyi hazırla",))
        result = match_skills(self.TASK, [entry(no_headline), entry(fit)], None)
        self.assertEqual([c.skill_id for c in result.candidates], ["z-fit", "a-steps"])
        self.assertEqual([c.score for c in result.candidates], [100, 100])
        self.assertEqual(
            [c.kind for c in result.candidates], [MatchKind.FIT, MatchKind.SIMILAR]
        )
        self.assertIs(result.best.kind, MatchKind.FIT)
        # The old 66-scoring "fit" leaves "hazırla" unbound: now only similar.
        partial = make_skill("z-fit", "Satış raporu", "Rapor", ("Belgeyi kaydet",))
        result = match_skills(self.TASK, [entry(no_headline), entry(partial)], None)
        self.assertEqual([c.skill_id for c in result.candidates], ["a-steps", "z-fit"])
        self.assertIs(result.kind, MatchKind.SIMILAR)

    def test_candidates_and_revalidation_are_bounded(self) -> None:
        active = [entry(self._same(f"s{i:02d}")) for i in range(20)]
        stale = [entry(self._same(f"t{i:02d}"), SkillStatus.STALE) for i in range(20)]
        result = match_skills(self.TASK, active + stale, None)
        self.assertEqual(len(result.candidates), sm.MAX_CANDIDATES)
        self.assertEqual(len(result.needs_revalidation), sm.MAX_CANDIDATES)
        self.assertEqual(result.candidates[0].skill_id, "s00")

    def test_deterministic_repeat(self) -> None:
        self.assertEqual(run("Export the monthly invoices"), run("Export the monthly invoices"))


class FailClosedTests(unittest.TestCase):
    def test_bad_arguments_are_refused(self) -> None:
        offer = [entry(REPORT_TR)]
        bad_calls = (
            (None, offer, None),
            (b"rapor", offer, None),
            ("rapor", offer, "Proj-a"),
            ("rapor", offer, "p1"),
            ("rapor", offer, "-proj"),
            ("rapor", offer, "proj_a"),
            ("rapor", offer, ""),
            ("rapor", offer, "a" * 65),
            ("rapor", offer, 7),
            ("rapor", "not-a-list", None),
            ("rapor", None, None),
            ("rapor", [REPORT_TR], None),
            ("rapor", [{"skill": REPORT_TR}], None),
            ("rapor", [entry(REPORT_TR), entry(REPORT_TR, SkillStatus.STALE)], None),
        )
        for task, skills, project_id in bad_calls:
            with self.subTest(task=task, skills=type(skills).__name__, project_id=project_id):
                with self.assertRaises(SkillMatchError):
                    match_skills(task, skills, project_id)

    def test_project_ids_follow_cores_pattern(self) -> None:
        offer = [entry(REPORT_TR)]
        samples = ("proj-a", "abc", "0ab", "a" * 64, "p1", "ab", "a" * 65, "-ab", "AB1", "a_b")
        for project_id in samples:
            with self.subTest(project_id=project_id):
                if PROJECT_ID_PATTERN.fullmatch(project_id):
                    self.assertIs(match_skills("rapor", offer, project_id).kind, MatchKind.SIMILAR)
                else:
                    with self.assertRaises(SkillMatchError):
                        match_skills("rapor", offer, project_id)

    def test_skill_match_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(SkillMatchError, ValueError))

    def test_skill_with_state_checks_its_types(self) -> None:
        with self.assertRaises(SkillMatchError):
            SkillWithState(skill=REPORT_TR, status="active", health=NO_HEALTH)
        with self.assertRaises(SkillMatchError):
            SkillWithState(skill=REPORT_TR.to_dict(), status=SkillStatus.ACTIVE, health=NO_HEALTH)
        with self.assertRaises(SkillMatchError):
            SkillWithState(skill=REPORT_TR, status=SkillStatus.ACTIVE, health=None)

    def test_too_many_skills_are_refused(self) -> None:
        many = (entry(make_skill(f"s{i}", "Ad", "Özet")) for i in range(sm.MAX_SKILLS + 1))
        with self.assertRaises(SkillMatchError):
            match_skills("rapor", many, None)


def vocab(count: int, lead: str = "q") -> list[str]:
    """Distinct three-letter words (exact matches only: shorter than PREFIX_MIN)."""
    return [lead + chr(0x61 + i // 26) + chr(0x61 + i % 26) for i in range(count)]


def headline_skill(skill_id: str, headline: list[str]) -> Skill:
    return make_skill(skill_id, " ".join(headline[:5]), " ".join(headline), ("Adımı uygula",))


class CoverageTests(unittest.TestCase):
    """A fit must also cover most of the skill's own name and summary."""

    BROAD_TEXT = (
        "Muhasebe klasorundeki eski dosyalari sil, sonra musteriye faturayi e-posta ile gonder"
    )

    def setUp(self) -> None:
        self.broad = make_skill(
            "muhasebe-temizle-ve-fatura",
            "Muhasebe dosyalarini sil ve faturayi gonder",
            self.BROAD_TEXT,
            ("Eski dosyalari sil", "Faturayi e-posta ile gonder"),
        )
        self.precise = make_skill(
            "fatura-gonder",
            "Faturayi gonder",
            "Faturayi musteriye gonder",
            ("Faturayi e-posta ile gonder",),
        )
        self.offer = [
            entry(self.broad, skill_health=health(5)),
            entry(self.precise, skill_health=health(3, 1)),
        ]

    def test_broad_skill_is_not_the_fit_for_a_narrow_task(self) -> None:
        result = match_skills("Faturayi musteriye gonder", self.offer, None)
        self.assertIs(result.kind, MatchKind.FIT)
        self.assertEqual(result.best.skill_id, "fatura-gonder")
        self.assertEqual((result.best.score, result.best.coverage), (100, 100))
        broad = result.candidates[1]
        self.assertEqual(broad.skill_id, "muhasebe-temizle-ve-fatura")
        self.assertIs(broad.kind, MatchKind.SIMILAR)
        self.assertEqual(broad.score, 100)
        self.assertLess(broad.coverage, sm.FIT_MIN_COVERAGE)
        self.assertIn("not a fit: coverage below 60", broad.reasons)

    def test_broad_skill_is_only_similar_for_part_of_it(self) -> None:
        result = match_skills("eski dosyalari temizle", self.offer, None)
        self.assertIs(result.kind, MatchKind.SIMILAR)
        self.assertEqual(result.best.skill_id, "muhasebe-temizle-ve-fatura")
        self.assertEqual(result.best.score, 66)

    def test_coverage_ranks_before_success_rate(self) -> None:
        exact = make_skill("b-exact", "Satış raporu", "Satış raporunu hazırlar")
        wider = make_skill("a-wider", "Satış raporu", "Satış raporunu hazırlar ve arşivler")
        result = match_skills(
            "satış raporu hazırla",
            [entry(wider, skill_health=health(4)), entry(exact, skill_health=health(1, 3))],
            None,
        )
        self.assertEqual([c.skill_id for c in result.candidates], ["b-exact", "a-wider"])
        # SKSIMASK: neither skill's own text, so both similar; same order.
        self.assertEqual(
            [c.kind for c in result.candidates], [MatchKind.SIMILAR, MatchKind.SIMILAR]
        )
        self.assertEqual([c.score for c in result.candidates], [100, 100])
        self.assertEqual([c.coverage for c in result.candidates], [100, 80])


def drafted_skill(
    skill_id: str, text: str, steps: tuple[str, ...], inputs: tuple[str, ...]
) -> Skill:
    """An SKDRAFT-shaped skill: name and summary are the run text with
    "{name}" placeholders, and each step lists the inputs it uses (D12)."""
    skill = make_skill(skill_id, text, text, steps, inputs=inputs)
    return dataclasses.replace(
        skill,
        steps=tuple(
            dataclasses.replace(
                step,
                inputs=tuple(name for name in inputs if "{" + name + "}" in step.action),
            )
            for step in skill.steps
        ),
    )


class PlaceholderTests(unittest.TestCase):
    """D18: "{name}" placeholders in a skill are not words of the skill."""

    INVOICE = drafted_skill(
        "fatura-eposta",
        "Faturayi {email} adresine gonder",
        ("Faturayi {email} adresine gonder",),
        ("email",),
    )
    DOWNLOAD = drafted_skill(
        "download-save",
        "Download {url} and save as {path}",
        ("Download {url} and save as {path}",),
        ("url", "path"),
    )
    TABLE = (
        # (skill, task, kind, score, coverage, headline words)
        # SKSIMASK4: a stored text with a placeholder never fits by text
        # (Salih, 2026-10-17 "Birebir aynıysa"); it is asked about.
        (INVOICE, "faturayi gonder", MatchKind.SIMILAR, 100, 66, {"faturayi", "adresine", "gonder"}),
        (INVOICE, "faturayi adresine gonder", MatchKind.SIMILAR, 100, 100, {"faturayi", "adresine", "gonder"}),
        # SKSIMASK: "pdf" is a value Core strips before matching (D18); left
        # in, it is an unbound word, so only similar.
        (DOWNLOAD, "download and save it", MatchKind.SIMILAR, 100, 100, {"download", "save"}),
        (DOWNLOAD, "download and save as", MatchKind.SIMILAR, 100, 100, {"download", "save"}),
        (DOWNLOAD, "download the pdf and save it", MatchKind.SIMILAR, 66, 100, {"download", "save"}),
    )

    def test_drafted_skill_is_a_fit_again(self) -> None:
        for skill, task, kind, score, coverage, headline in self.TABLE:
            with self.subTest(task=task):
                result = run(task, skill)
                self.assertIs(result.kind, kind)
                self.assertEqual(result.best.skill_id, skill.skill_id)
                self.assertEqual((result.best.score, result.best.coverage), (score, coverage))
                self.assertEqual(sm._skill_words(skill).headline.exact, frozenset(headline))

    def test_placeholder_names_are_not_words_of_the_text(self) -> None:
        skill = make_skill(
            "ph", "Rapor {xyzzy} {a-1}{b_2} gonder", "{kopya}", ("Raporu {plugh} gonder",)
        )
        index = sm._skill_words(skill)
        self.assertEqual(index.headline.exact, frozenset({"rapor", "gonder"}))
        self.assertEqual(index.body.exact, frozenset({"rapor", "gonder", "raporu"}))
        # Not a placeholder (not an input-name shape): its word stays.
        other = make_skill("ph2", "Rapor {Email} gonder", "{ } gonder")
        self.assertIn("email", sm._skill_words(other).headline.exact)

    def test_a_skill_named_only_by_placeholders_never_fits(self) -> None:
        cases = (
            ("{url}", "{url} {path}"),
            ("{url} {path}", "{path}"),
            ("{url}{path}", "{url}"),
        )
        for name, summary in cases:
            skill = make_skill(
                "only-ph", name, summary, ("Indir {url} {path}",), inputs=("url", "path")
            )
            for task in ("url path indir", "indir url", "{url} {path} indir"):
                with self.subTest(name=name, task=task):
                    result = run(task, skill)
                    self.assertIsNot(result.kind, MatchKind.FIT)
                    self.assertEqual(result.best.coverage, 0)
                    self.assertIn(
                        "not a fit: no task word in its name or summary", result.best.reasons
                    )

    def test_task_text_is_read_as_it_is(self) -> None:
        # Values and braces in the TASK are not this module's job (D18: SKCORE).
        self.assertEqual(words("{email} faturayi gonder"), ("email", "faturayi", "gonder"))


class BoundaryTests(unittest.TestCase):
    """Exact threshold edges: the constants cannot drift unnoticed."""

    def _match(self, matched: int, unmatched: int):
        hit = vocab(matched)
        miss = vocab(unmatched, "z")
        result = match_skills(" ".join(hit + miss), [entry(headline_skill("s", hit))], None)
        return result

    def test_fit_min_score_edge(self) -> None:
        # SKSIMASK: a fit binds every request word, so the score edge alone
        # no longer makes one; 60 with two unbound words is only similar.
        at = self._match(3, 2)  # 3 of 5 = 60
        self.assertEqual((at.best.score, at.best.coverage), (60, 100))
        self.assertIs(at.kind, MatchKind.SIMILAR)
        self.assertIn(
            "not a fit: request words the skill does not bind: zaa, zab", at.best.reasons
        )
        bound = self._match(3, 0)  # 3 of 3 = 100, nothing unbound
        self.assertEqual((bound.best.score, bound.best.coverage), (100, 100))
        self.assertIs(bound.kind, MatchKind.FIT)
        below = self._match(13, 9)  # 13 of 22 = 59
        self.assertEqual((below.best.score, below.best.coverage), (59, 100))
        self.assertIs(below.kind, MatchKind.SIMILAR)
        self.assertIn("not a fit: score below 60", below.best.reasons)

    def test_similar_min_score_edge(self) -> None:
        at = self._match(3, 7)  # 3 of 10 = 30
        self.assertEqual(at.best.score, 30)
        self.assertIs(at.kind, MatchKind.SIMILAR)
        below = self._match(7, 17)  # 7 of 24 = 29
        self.assertIs(below.kind, MatchKind.NONE)
        self.assertEqual(below.candidates, ())

    def test_fit_min_coverage_edge(self) -> None:
        def cover(covered: int, extra: int):
            # SKSIMASK: the request is the skill's own name, so only the
            # coverage of its summary decides.
            headline = vocab(covered) + vocab(extra, "z")
            skill = make_skill("s", " ".join(vocab(covered)), " ".join(headline), ("Adımı uygula",))
            return match_skills(" ".join(vocab(covered)), [entry(skill)], None)

        at = cover(3, 2)  # 3 of 5 = 60
        self.assertEqual((at.best.score, at.best.coverage), (100, 60))
        self.assertIs(at.kind, MatchKind.FIT)
        below = cover(13, 9)  # 13 of 22 = 59
        self.assertEqual((below.best.score, below.best.coverage), (100, 59))
        self.assertIs(below.kind, MatchKind.SIMILAR)
        self.assertIn("not a fit: coverage below 60", below.best.reasons)

    def test_a_never_used_skill_ranks_below_any_success(self) -> None:
        text = ("Satış raporu", "Satış raporunu hazırlar")
        offer = [
            entry(make_skill("a-new", *text)),
            entry(make_skill("c-failed", *text), skill_health=health(0, 2)),
            entry(make_skill("b-used", *text), skill_health=health(1, 1)),
        ]
        result = match_skills("satış raporu hazırla", offer, None)
        self.assertEqual([c.skill_id for c in result.candidates], ["b-used", "a-new", "c-failed"])


def big_skill(skill_id: str, fill: str) -> Skill:
    """A skill near the 16 KiB record cap, every text field filled."""
    if fill == "fdfa":
        return make_skill(skill_id, "Genel rapor", chr(0xFDFA) * 400, (chr(0xFDFA) * TEXT_MAX,) * 9)
    steps = tuple(
        " ".join(f"aaaa{i}z{k}" for k in range(60))[: TEXT_MAX - 20].rsplit(" ", 1)[0]
        for i in range(20)
    )
    return make_skill(skill_id, "Genel rapor", steps[0][:400].rsplit(" ", 1)[0], steps)


class CostTests(unittest.TestCase):
    """Time is bounded per field, per skill, per call; words are cached."""

    def setUp(self) -> None:
        sm._skill_words.cache_clear()

    def tearDown(self) -> None:
        sm._skill_words.cache_clear()

    def test_field_text_is_capped_without_a_word_fragment(self) -> None:
        folded = sm._fold_capped(chr(0xFDFA) * TEXT_MAX, sm.FIELD_TEXT_MAX)
        self.assertLessEqual(len(folded), sm.FIELD_TEXT_MAX)
        self.assertGreater(len(folded), sm.FIELD_TEXT_MAX - 30)
        whole = fold_token(chr(0xFDFA) * TEXT_MAX)
        self.assertEqual(folded, whole[: len(folded)])
        # The cut ends at a word border: no fragment of the next word is kept.
        self.assertFalse(folded[-1].isalnum() and whole[len(folded)].isalnum())

    def test_expanding_text_is_folded_only_up_to_the_cap(self) -> None:
        # 500 U+FDFA fold to 9,000 characters; only a few chunks are folded.
        seen: list[int] = []

        def counting_fold(token: str) -> str:
            seen.append(len(token))
            return fold_token(token)

        with mock.patch.object(sm, "fold_token", counting_fold):
            folded = sm._fold_capped(chr(0xFDFA) * TEXT_MAX, sm.FIELD_TEXT_MAX)
        self.assertLessEqual(len(folded), sm.FIELD_TEXT_MAX)
        self.assertLessEqual(sum(seen), sm.FIELD_TEXT_MAX // 18 + 2 * sm._FOLD_CHUNK)

    def test_skill_words_are_bounded(self) -> None:
        for fill in ("fdfa", "ascii"):
            with self.subTest(fill=fill):
                index = sm._skill_words(big_skill("big", fill))
                self.assertLessEqual(len(index.body.exact), sm.MAX_SKILL_WORDS)
                self.assertTrue(index.headline.exact <= index.body.exact)
        ascii_words = sm._skill_words(big_skill("big", "ascii")).body.exact
        self.assertEqual(len(ascii_words), sm.MAX_SKILL_WORDS)

    def test_text_past_the_skill_cap_is_not_read(self) -> None:
        filler = " ".join(f"dolgu{i:03d}" for i in range(55))  # ~ 494 characters
        late = make_skill("late", "Genel", "Genel", (filler,) * 7 + ("benzersizkelime arsivle",))
        early = make_skill("early", "Genel", "Genel", ("benzersizkelime arsivle",))
        self.assertGreater(sum(len(step.action) for step in late.steps), sm.SKILL_TEXT_MAX)
        self.assertEqual(run("benzersizkelime arsivle", early).best.score, 100)
        self.assertIs(run("benzersizkelime arsivle", late).kind, MatchKind.NONE)

    def test_words_are_cached_per_skill_version_and_bounded(self) -> None:
        self.assertEqual(sm._skill_words.cache_parameters()["maxsize"], sm.CACHE_MAX)
        self.assertGreaterEqual(sm.CACHE_MAX, sm.MAX_SKILLS)
        first = run("Export the monthly invoices")
        misses = sm._skill_words.cache_info().misses
        rebuilt = [Skill.from_dict(skill.to_dict()) for skill in LIBRARY]  # equal, new objects
        again = match_skills("Export the monthly invoices", [entry(s) for s in rebuilt], None)
        self.assertEqual(again, first)
        self.assertEqual(sm._skill_words.cache_info().misses, misses)
        # Same id and version, another body: never served the cached words.
        changed = make_skill(INVOICE_EN.skill_id, "Database backup", "Back up the database")
        self.assertEqual(run("back up the database", changed).best.skill_id, "invoice-export")
        self.assertIs(run("Export the monthly invoices", changed).kind, MatchKind.NONE)

    def test_worst_case_call_stays_fast(self) -> None:
        # MAX_SKILLS maximum-size skills: U+FDFA text (18x NFKC growth) and
        # words sharing one prefix with every task word. Measured on the
        # Linux lane: cold ~1.4 s, warm ~0.02 s; the bounds are generous.
        offer = [
            entry(big_skill(f"s{i:04d}", "fdfa" if i % 2 else "ascii"))
            for i in range(sm.MAX_SKILLS)
        ]
        task = " ".join(f"aaaa{k}q" for k in range(sm.MAX_TASK_WORDS))
        started = time.perf_counter()
        cold = match_skills(task, offer, None)
        cold_seconds = time.perf_counter() - started
        started = time.perf_counter()
        warm = match_skills(task, offer, None)
        warm_seconds = time.perf_counter() - started
        self.assertEqual(cold, warm)
        self.assertLess(cold_seconds, 10.0)
        self.assertLess(warm_seconds, 2.0)


class AuthorityAndPurityTests(unittest.TestCase):
    SOURCE = Path(sm.__file__).read_text(encoding="utf-8")

    def test_result_records_carry_no_authority_field(self) -> None:
        for record in (sm.MatchResult, sm.MatchCandidate, sm.SkillWithState):
            for name in record.__dataclass_fields__:
                with self.subTest(record=record.__name__, field=name):
                    self.assertIsNone(FORBIDDEN_KEY_PATTERN.search(name))

    def test_module_is_pure(self) -> None:
        tree = ast.parse(self.SOURCE)
        allowed = {
            "__future__",
            "unicodedata",
            "collections.abc",
            "dataclasses",
            "datetime",
            "enum",
            "fractions",
            "functools",
            "bisect",
            "re",
            "jarvis.skill_types",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "eval", "exec", "__import__"})
            elif isinstance(node, ast.Attribute):
                # D13: skill_types.fold_token is the only fold; no own one.
                self.assertNotIn(node.attr, {"normalize", "casefold", "lower", "upper"})

    def test_source_is_printable_ascii_plus_turkish_letters(self) -> None:
        # Turkish stop words are the only non-ASCII text: no control or bidi
        # characters can hide in the module source.
        turkish = set("çğıöşüÇĞİÖŞÜ")
        for char in self.SOURCE:
            self.assertTrue(
                char == chr(10) or 0x20 <= ord(char) < 0x7F or char in turkish, repr(char)
            )


if __name__ == "__main__":
    unittest.main()
