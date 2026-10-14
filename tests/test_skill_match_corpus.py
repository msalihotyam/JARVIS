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


if __name__ == "__main__":
    unittest.main()
