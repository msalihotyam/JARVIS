"""V2.2 wave S1: cross-slice contract tests (SKS1X).

The eight wave-S1 slices were built and reviewed one by one. These tests pin
the contracts BETWEEN them, using each slice's public API (plus SKMATCH's
scope helper, named in its integration notes for exactly this cross-test):

1. D12  SKDRAFT -> SKPLAN step contract (target = step.inputs[0], placeholders
        substituted, one-line objective, literal braces as parentheses, the two
        shared ROUND3 examples, the brace rule refused on both sides; run text
        with tab/newline/CR drafts to one-line actions SKPLAN plans, an edited
        action with one is refused by SKDRAFT and SKPLAN).
2. D13  one fold: SKMATCH words, SKEXP task_tokens and SKDRAFT task_kind agree
        on Turkish/English/fullwidth spellings; every token is canonical.
3.      the scope rule: SKEXP visible_in == SKMATCH scope rule (== SKPLAN's
        project check) over global / project A / project B x no project / A / B.
4. D15  scope moves: SKLIFE (build and read side) and SKDRAFT (edited drafts)
        agree: same scope ok, project -> global ok, global -> project and
        project -> another project refused.
5. D16  usable skills: a deleted or disabled skill (usable_skill None) is never
        SKMATCH's best; declines and denied approvals never change SKLIFE
        health or status (also via SKEXP's broken_workflow capture).
6. S7   run text with an e-mail, a URL with ?token=, a UNC path and a Windows
        path goes through SKDRAFT and then SKFILTER check_fields with id-grammar
        field names: nothing secret or personal remains and the fields pass.
7.      limits agree with Core: SKPLAN refuses more than MAX_CHAIN_STEPS;
        skills_enabled defaults to False (SKFLAG).
8. D17  invisible text: every code point of the D17 list is refused by
        skill_types in text and stripped by SKFILTER's cleaned form (a TC
        kimlik split by it is still refused).
9. D18  a skill SKDRAFT drafts from a run with an e-mail, a URL and a path is
        a SKMATCH fit for the same task text with those values removed.

Pure-value tests: no Core, no database (data/jarvis.db is never opened; the
settings check uses tests.helpers.isolated_settings), no model, connector or
network call. Special characters are built with chr() so the source is ASCII.
"""

from __future__ import annotations

import dataclasses
import unittest

from jarvis import contracts
from jarvis.experience import capture_candidates, select_for_task, task_tokens, visible_in
from jarvis.settings import fail_closed_settings
from jarvis.skill_draft import build_drafts, parse_user_edited_draft, task_kind
from jarvis.skill_filter import check_fields
from jarvis.skill_lifecycle import (
    SkillEvent,
    SkillHistory,
    SkillLifecycleError,
    adapt,
    health,
    next_version,
    stale_mark_event,
    status,
    usable_skill,
    use_events,
)
from jarvis.skill_match import MatchKind, SkillWithState, _in_scope, match_skills, words
from jarvis.skill_run_plan import (
    INPUT_VALUE_MAX,
    MissingInputsError,
    SkillPlanContext,
    SkillPlanError,
    plan_steps,
)
from jarvis.skill_types import (
    Experience,
    ExperienceKind,
    RunOutcome,
    RunStep,
    RunSummary,
    Skill,
    SkillDraft,
    SkillEventKind,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillRecordError,
    SkillRef,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
    SkillStep,
    fold_token,
)
from tests.helpers import isolated_settings

CREATED_AT = "2026-10-14T00:00:00+00:00"
LATER = "2026-10-14T01:00:00+00:00"
PROJECT_A = "proj-a"
PROJECT_B = "proj-b"
GLOBAL = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
SCOPE_A = SkillScope(kind=SkillScopeKind.PROJECT, project_id=PROJECT_A)
SCOPE_B = SkillScope(kind=SkillScopeKind.PROJECT, project_id=PROJECT_B)
CONTEXTS = (None, PROJECT_A, PROJECT_B)
CATALOG = {
    "file_read": "read_file",
    "file_search": "search_file",
    "web_fetch": "fetch_url",
}

BS = chr(92)
TAB, NEWLINE, CR = chr(9), chr(10), chr(13)

# Turkish and fullwidth spellings, built from code points (ASCII source).
DOTLESS_I = chr(0x131)
DOTTED_CAPITAL_I = chr(0x130)
C_CEDILLA = chr(0xE7)
S_CEDILLA = chr(0x15F)
G_BREVE = chr(0x11F)


def fullwidth(text: str) -> str:
    """ASCII letters as their fullwidth forms (U+FF21.., U+FF41..)."""
    out = []
    for char in text:
        if "A" <= char <= "Z":
            out.append(chr(0xFF21 + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            out.append(chr(0xFF41 + ord(char) - ord("a")))
        else:
            out.append(char)
    return "".join(out)


HAFTALIK_TR = "Haftal" + DOTLESS_I + "k"
ISTANBUL_TR = DOTTED_CAPITAL_I + "stanbul"
CALISTIR_TR = C_CEDILLA + "al" + DOTLESS_I + S_CEDILLA + "t" + DOTLESS_I + "r"
CALISTIR_COMBINING = "c" + chr(0x327) + "alistir"
BETIGI_TR = "beti" + G_BREVE + "i"


# -- builders ---------------------------------------------------------------


def run_summary(
    steps,
    task_text,
    *,
    project_id=None,
    status_=RunOutcome.COMPLETED,
    matched_skill=None,
    run_id="run-s1x",
):
    """A finished run; ``steps`` are (action, capability[, outcome]) tuples."""
    return RunSummary(
        run_id=run_id,
        task_text=task_text,
        project_id=project_id,
        status=status_,
        steps=tuple(
            RunStep(
                action=item[0],
                capability=item[1],
                outcome=item[2] if len(item) > 2 else RunOutcome.COMPLETED,
                error=None,
            )
            for item in steps
        ),
        user_correction=None,
        matched_skill=matched_skill,
    )


def saved(draft: SkillDraft, skill_id: str = "sk-s1x") -> Skill:
    """The skill an approved taught draft becomes (version 1, same procedure),
    as SKSTORE will save it; SKSTORE itself is not in wave S1."""
    return Skill(
        skill_id=skill_id,
        version=1,
        name=draft.name,
        summary=draft.summary,
        scope=draft.scope,
        inputs=draft.inputs,
        steps=draft.steps,
        checks=draft.checks,
        provenance=draft.provenance,
    )


def taught(run_id: str = "run-s1x") -> SkillProvenance:
    return SkillProvenance(
        origin=SkillOrigin.TAUGHT_FROM_RUN,
        source_run_id=run_id,
        parent_skill_id=None,
        parent_version=None,
        created_at=CREATED_AT,
    )


def make_skill(
    *,
    skill_id="sk-s1x",
    scope=GLOBAL,
    steps=None,
    inputs=None,
    name="Haftalik rapor dosyasini oku",
    summary="Haftalik rapor dosyasini oku",
) -> Skill:
    if inputs is None:
        inputs = (SkillInput(name="path", description="", required=True),)
    if steps is None:
        steps = (
            SkillStep(
                index=1,
                action="Haftalik rapor dosyasini oku: {path}",
                capability="file_read",
                inputs=("path",),
                check=None,
            ),
        )
    return Skill(
        skill_id=skill_id,
        version=1,
        name=name,
        summary=summary,
        scope=scope,
        inputs=inputs,
        steps=steps,
        checks=(),
        provenance=taught(),
    )


def event(skill_id, kind, version=None, at=CREATED_AT):
    return SkillEvent(skill_id=skill_id, kind=kind, version=version, at=at)


def history_of(skill: Skill, *kinds: SkillEventKind) -> SkillHistory:
    events = [event(skill.skill_id, SkillEventKind.DRAFT_APPROVED, skill.version)]
    for kind in kinds:
        versioned = kind in (
            SkillEventKind.USED_SUCCESS,
            SkillEventKind.USED_FAILURE,
            SkillEventKind.DRAFT_APPROVED,
            SkillEventKind.ROLLED_BACK,
        )
        events.append(event(skill.skill_id, kind, skill.version if versioned else None))
    return SkillHistory(versions=(skill,), events=tuple(events))


def draft_fields(draft: SkillDraft) -> list[tuple[str, str]]:
    """Every text field of a draft, named in SKFILTER's id grammar."""
    fields = [("name", draft.name), ("summary", draft.summary), ("reason", draft.reason)]
    for number, item in enumerate(draft.inputs, start=1):
        fields.append((f"inputs-{number}-name", item.name))
        fields.append((f"inputs-{number}-description", item.description))
    for step in draft.steps:
        fields.append((f"steps-{step.index}-action", step.action))
        if step.capability is not None:
            fields.append((f"steps-{step.index}-capability", step.capability))
        for position, name in enumerate(step.inputs, start=1):
            fields.append((f"steps-{step.index}-input-{position}", name))
        if step.check is not None:
            fields.append((f"steps-{step.index}-check", step.check))
    for number, text in enumerate(draft.checks, start=1):
        fields.append((f"checks-{number}", text))
    return fields


def one_line(text: str) -> bool:
    return not any(char in text for char in (TAB, NEWLINE, CR))


# -- 1. D12: SKDRAFT -> SKPLAN ----------------------------------------------


class DraftToPlanContractTests(unittest.TestCase):
    """D12: what SKDRAFT drafts from an executed run, SKPLAN plans."""

    LINE_BREAKS = (TAB, NEWLINE, CR, CR + NEWLINE)

    PATH = "C:/notlar/a.txt"
    URL = "https://example.com/rapor?id=7"

    def plan(self, skill: Skill, values: dict, project_id=None):
        return plan_steps(skill, values, SkillPlanContext(project_id=project_id, actions=CATALOG))

    def test_a_drafted_read_and_fetch_run_is_planned_step_for_step(self):
        run = run_summary(
            [
                (f"Read the rapor file {self.PATH} and keep the {{draft}} markers", "file_read"),
                (f"Fetch {self.URL} then summarize the rapor {{x}} part", "web_fetch"),
            ],
            f"Read {self.PATH} and fetch {self.URL}",
        )
        drafts = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        read, fetch = draft.steps
        # SKDRAFT side of D12: primary object first, literal braces as parentheses.
        self.assertEqual(read.inputs[0], "path")
        self.assertEqual(fetch.inputs[0], "url")
        self.assertEqual(read.action, "Read the rapor file {path} and keep the (draft) markers")
        self.assertEqual(fetch.action, "Fetch {url} then summarize the rapor (x) part")

        values = {"path": self.PATH, "url": self.URL}
        chain = self.plan(saved(draft), values)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].target, self.PATH)
        self.assertEqual(chain[1].target, self.URL)
        self.assertEqual(
            chain[0].objective, f"Read the rapor file {self.PATH} and keep the (draft) markers"
        )
        self.assertEqual(
            chain[1].objective, f"Fetch {self.URL} then summarize the rapor (x) part"
        )
        self.assertEqual(chain[0].action, "read_file")
        self.assertEqual(chain[1].action, "fetch_url")
        for proposal, step in zip(chain, draft.steps):
            self.assertEqual(proposal.target, values[step.inputs[0]])
            self.assertTrue(one_line(proposal.objective))
            self.assertNotIn("{", proposal.objective)
            self.assertNotIn("}", proposal.objective)
            self.assertEqual(proposal.capability, step.capability)
            self.assertEqual(proposal.destination, "")
            self.assertEqual(proposal.write_mode, "")
            self.assertEqual(proposal.consumes, contracts.CHAIN_NO_PREDECESSOR)

    def test_the_drafted_values_plan_back_to_the_run_text(self):
        # Planning a draft with the values it was taught from gives back the
        # run's own step text (braces aside), so the draft lost nothing.
        actions = (
            f"Read {self.PATH}",
            f"Fetch {self.URL} and keep {{a}}",
        )
        run = run_summary(
            [(actions[0], "file_read"), (actions[1], "web_fetch")],
            f"Read {self.PATH} then fetch {self.URL}",
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        chain = self.plan(saved(draft), {"path": self.PATH, "url": self.URL})
        self.assertEqual(
            tuple(item.objective for item in chain),
            tuple(text.replace("{", "(").replace("}", ")") for text in actions),
        )

    def test_shared_example_one_through_both_modules(self):
        # ROUND3 D12: "Dosyayi oku: {path}", inputs ("path",), path C:/notlar/a.txt.
        run = run_summary(
            [(f"Dosyayi oku: {self.PATH}", "file_read")], f"{self.PATH} dosyasini oku"
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        (step,) = draft.steps
        self.assertEqual(step.action, "Dosyayi oku: {path}")
        self.assertEqual(step.inputs, ("path",))
        # The card round trip (SKDRAFT's edit parser) keeps it as it is.
        edited = parse_user_edited_draft(draft.to_dict(), original=draft)
        self.assertEqual(edited, draft)
        (proposal,) = self.plan(saved(edited), {"path": self.PATH})
        self.assertEqual(proposal.target, self.PATH)
        self.assertEqual(proposal.objective, "Dosyayi oku: C:/notlar/a.txt")

    def test_shared_example_two_through_both_modules(self):
        # ROUND3 D12: "{text} metnini {path} dosyasinda ara", inputs ("path", "text").
        run = run_summary(
            [(f'"rapor" metnini {self.PATH} dosyasinda ara', "file_search")],
            f'"rapor" metnini {self.PATH} dosyasinda ara',
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        (step,) = draft.steps
        # SKDRAFT orders the primary object (the path) first although the text
        # value comes first in the sentence.
        self.assertEqual(step.inputs, ("path", "text"))
        self.assertEqual(step.action, '"{text}" metnini {path} dosyasinda ara')
        values = {"path": self.PATH, "text": "rapor"}
        (proposal,) = self.plan(saved(draft), values)
        self.assertEqual(proposal.target, self.PATH)
        self.assertEqual(proposal.objective, f'"rapor" metnini {self.PATH} dosyasinda ara')

        # The exact shared action, set on the card: SKDRAFT accepts, SKPLAN plans.
        data = draft.to_dict()
        data["steps"][0]["action"] = "{text} metnini {path} dosyasinda ara"
        edited = parse_user_edited_draft(data, original=draft)
        (proposal,) = self.plan(saved(edited), values)
        self.assertEqual(proposal.target, self.PATH)
        self.assertEqual(proposal.objective, f"rapor metnini {self.PATH} dosyasinda ara")

    def test_an_edit_that_breaks_the_brace_rule_is_refused_by_both(self):
        run = run_summary(
            [(f'"rapor" metnini {self.PATH} dosyasinda ara', "file_search")],
            f'"rapor" metnini {self.PATH} dosyasinda ara',
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        self.assertEqual(draft.steps[0].inputs, ("path", "text"))
        values = {"path": self.PATH, "text": "rapor"}
        broken_actions = (
            "{other} metnini {path} dosyasinda ara",  # placeholder not in step.inputs
            "{text} metnini {path} dosyasinda ara }",  # stray closing brace
            "{ metnini {path} dosyasinda ara",  # stray opening brace
            "{{text}} metnini {path} dosyasinda ara",  # nested braces
            "{text metnini {path} dosyasinda ara",  # unclosed placeholder
            "{} metnini {path} dosyasinda ara",  # empty placeholder
            "{Path} metnini dosyasinda ara",  # case differs from the input name
        )
        for action in broken_actions:
            with self.subTest(action=action):
                data = draft.to_dict()
                data["steps"][0]["action"] = action
                with self.assertRaises(SkillRecordError):
                    parse_user_edited_draft(data, original=draft)
                step = dataclasses.replace(draft.steps[0], action=action)
                skill = dataclasses.replace(saved(draft), steps=(step,))
                with self.assertRaises(SkillPlanError) as caught:
                    self.plan(skill, values)
                self.assertNotIsInstance(caught.exception, MissingInputsError)

    def test_a_placeholder_for_an_input_the_step_does_not_list_is_refused_by_both(self):
        # Declared on the draft, but not one of THIS step's inputs.
        run = run_summary(
            [
                (f"Dosyayi oku: {self.PATH}", "file_read"),
                (f"Fetch {self.URL}", "web_fetch"),
            ],
            f"{self.PATH} oku, {self.URL} getir",
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        self.assertEqual(draft.steps[0].inputs, ("path",))
        self.assertIn("url", {item.name for item in draft.inputs})
        data = draft.to_dict()
        data["steps"][0]["action"] = "Dosyayi oku: {path} ve {url}"
        with self.assertRaises(SkillRecordError):
            parse_user_edited_draft(data, original=draft)
        step = dataclasses.replace(draft.steps[0], action="Dosyayi oku: {path} ve {url}")
        skill = dataclasses.replace(saved(draft), steps=(step,) + draft.steps[1:])
        with self.assertRaises(SkillPlanError) as caught:
            self.plan(skill, {"path": self.PATH, "url": self.URL})
        self.assertNotIsInstance(caught.exception, MissingInputsError)

    def test_a_one_line_edit_skdraft_accepts_plans_to_one_line(self):
        # A one-line step action SKDRAFT's edit parser accepts is planned by
        # SKPLAN to a one-line objective with the value in place.
        run = run_summary(
            [(f"Dosyayi oku: {self.PATH}", "file_read")], f"{self.PATH} dosyasini oku"
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        for action in ("Oku (once) {path}", "{path} oku", "{path}"):
            with self.subTest(action=action):
                data = draft.to_dict()
                data["steps"][0]["action"] = action
                edited = parse_user_edited_draft(data, original=draft)
                (proposal,) = self.plan(saved(edited), {"path": self.PATH})
                self.assertTrue(one_line(proposal.objective))
                self.assertEqual(proposal.objective, action.replace("{path}", self.PATH))
                self.assertEqual(proposal.target, self.PATH)

    def test_run_text_with_line_breaks_drafts_to_one_line_actions_skplan_plans(self):
        # D12 completed: tab, newline, CR (and CR LF) in run step text become
        # one space while drafting (runs of spaces collapsed, ends trimmed).
        for brk in self.LINE_BREAKS:
            with self.subTest(line_break=[ord(char) for char in brk]):
                run = run_summary(
                    [
                        (
                            "Dosyayi oku:" + brk + self.PATH + brk + " sonra"
                            + brk + brk + "bitir" + brk,
                            "file_read",
                        ),
                        (brk + "Fetch" + brk + self.URL + brk, "web_fetch"),
                    ],
                    f"{self.PATH} dosyasini oku ve {self.URL} getir",
                )
                drafts = build_drafts(run, created_at=CREATED_AT)
                self.assertEqual(len(drafts), 1)
                read, fetch = drafts[0].steps
                self.assertEqual(read.action, "Dosyayi oku: {path} sonra bitir")
                self.assertEqual(fetch.action, "Fetch {url}")
                for step in drafts[0].steps:
                    self.assertTrue(one_line(step.action))
                chain = self.plan(saved(drafts[0]), {"path": self.PATH, "url": self.URL})
                self.assertEqual(
                    tuple(item.objective for item in chain),
                    (f"Dosyayi oku: {self.PATH} sonra bitir", f"Fetch {self.URL}"),
                )
                self.assertEqual((chain[0].target, chain[1].target), (self.PATH, self.URL))

    def test_an_edited_action_with_a_line_break_is_refused_by_both(self):
        # D12 completed: SKDRAFT refuses the edit (SkillRecordError); SKPLAN
        # refuses the same step, so neither side lets it through.
        run = run_summary(
            [(f"Dosyayi oku: {self.PATH}", "file_read")], f"{self.PATH} dosyasini oku"
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        for brk in self.LINE_BREAKS:
            for action in (
                "Dosyayi oku:" + brk + "{path}",
                brk + "Dosyayi oku: {path}",
                "Dosyayi oku: {path}" + brk,
            ):
                with self.subTest(action=[ord(char) for char in action]):
                    data = draft.to_dict()
                    data["steps"][0]["action"] = action
                    with self.assertRaises(SkillRecordError):
                        parse_user_edited_draft(data, original=draft)
                    step = dataclasses.replace(draft.steps[0], action=action)
                    skill = dataclasses.replace(saved(draft), steps=(step,))
                    with self.assertRaises(SkillPlanError) as caught:
                        self.plan(skill, {"path": self.PATH})
                    self.assertNotIsInstance(caught.exception, MissingInputsError)


# -- 2. D13: one fold -------------------------------------------------------


class OneFoldContractTests(unittest.TestCase):
    """D13: SKMATCH, SKEXP and SKDRAFT fold task words the same way."""

    SPELLINGS = {
        ("haftalik", "rapor"): (
            HAFTALIK_TR + " rapor",
            "HAFTALIK RAPOR",
            "haftalik Rapor",
            fullwidth("HAFTALIK") + " rapor",
            fullwidth("haftalik rapor"),
        ),
        ("istanbul", "ofisi"): (
            ISTANBUL_TR + " ofisi",
            "ISTANBUL OFISI",
            "istanbul ofisi",
            fullwidth("Istanbul") + " ofisi",
        ),
        ("calistir", "betigi"): (
            CALISTIR_TR + " " + BETIGI_TR,
            "CALISTIR BETIGI",
            "calistir betigi",
            CALISTIR_COMBINING + " betigi",
            CALISTIR_TR.upper() + " " + BETIGI_TR.upper(),
            fullwidth("calistir") + " betigi",
        ),
    }

    def test_skmatch_words_and_skexp_task_tokens_agree_and_are_canonical(self):
        for expected, texts in self.SPELLINGS.items():
            for text in texts:
                with self.subTest(text=text):
                    matched = words(text)
                    tokens = task_tokens(text)
                    self.assertEqual(matched, expected)
                    self.assertEqual(tokens, expected)
                    for token in matched + tokens:
                        self.assertEqual(fold_token(token), token)

    def test_tokens_are_canonical_for_mixed_text(self):
        text = " ".join(
            [
                HAFTALIK_TR,
                "STRASSE",
                "Stra" + chr(0xDF) + "e",
                chr(0x1C5) + "emal",  # Dz digraph titlecase
                chr(0xFB01) + "le",  # fi ligature
                fullwidth("Rapor"),
                ISTANBUL_TR,
                "e" + chr(0x301) + "te",  # combining acute
            ]
        )
        matched = words(text)
        tokens = task_tokens(text)
        self.assertEqual(matched, tokens)
        self.assertTrue(matched)
        for token in matched:
            self.assertEqual(fold_token(token), token)

    def test_skexp_tokens_are_valid_experience_signatures_found_by_skmatch_words(self):
        # A lesson stored under SKEXP's tokens is found by a task that SKMATCH
        # reads with the same words, whatever spelling either one used.
        for expected, texts in self.SPELLINGS.items():
            stored = Experience(
                experience_id="exp-s1x",
                kind=ExperienceKind.AVOID,
                skill_id=None,
                task_signature=task_tokens(texts[0]),
                text="Once dosyanin yerini kontrol et.",
                scope=GLOBAL,
                source_run_id="run-s1x",
                created_at=CREATED_AT,
            )
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(set(words(text)), set(stored.task_signature))
                    self.assertEqual(select_for_task(text, [stored], None, None), (stored,))

    def test_skdraft_task_kind_folds_the_same_word_the_same_way(self):
        def kind(action: str) -> str:
            return task_kind(run_summary([(action, "file_read")], action))

        groups = (
            # No verb: the first word names the kind.
            (
                HAFTALIK_TR + " rapor",
                "HAFTALIK rapor",
                fullwidth("Haftalik") + " rapor",
                "haftalik rapor",
            ),
            # A Turkish verb in every spelling resolves to the same verb.
            (
                CALISTIR_TR + " haftalik betik",
                "CALISTIR haftalik betik",
                CALISTIR_COMBINING + " haftalik betik",
                fullwidth("CALISTIR") + " haftalik betik",
                CALISTIR_TR.upper() + " haftalik betik",
            ),
            (
                ISTANBUL_TR + " ofisi",
                "ISTANBUL ofisi",
                fullwidth("istanbul") + " ofisi",
            ),
        )
        kinds = []
        for group in groups:
            first = kind(group[0])
            kinds.append(first)
            for action in group[1:]:
                with self.subTest(action=action):
                    self.assertEqual(kind(action), first)
            # The words SKDRAFT keyed the kind on are the ones SKMATCH and
            # SKEXP read from the same text.
            for action in group:
                self.assertEqual(words(action)[0], task_tokens(action)[0])
        # Different words are different kinds (the check above is not vacuous).
        self.assertEqual(len(set(kinds)), len(kinds))
        self.assertNotEqual(kind("Aylik rapor"), kinds[0])


# -- 3. one scope rule ------------------------------------------------------


class ScopeRuleContractTests(unittest.TestCase):
    """SKEXP visible_in and SKMATCH's scope rule (and SKPLAN's project check)
    answer identically for every record scope and every context."""

    TASK = "Haftalik rapor dosyasini oku"
    EXPECTED = {
        ("global", None): True,
        ("global", PROJECT_A): True,
        ("global", PROJECT_B): True,
        (PROJECT_A, None): False,
        (PROJECT_A, PROJECT_A): True,
        (PROJECT_A, PROJECT_B): False,
        (PROJECT_B, None): False,
        (PROJECT_B, PROJECT_A): False,
        (PROJECT_B, PROJECT_B): True,
    }
    SCOPES = {"global": GLOBAL, PROJECT_A: SCOPE_A, PROJECT_B: SCOPE_B}

    def test_the_matrix(self):
        for (label, context), expected in self.EXPECTED.items():
            scope = self.SCOPES[label]
            skill = make_skill(scope=scope)
            lesson = Experience(
                experience_id="exp-scope",
                kind=ExperienceKind.AVOID,
                skill_id=None,
                task_signature=task_tokens(self.TASK),
                text="Once dosyanin yerini kontrol et.",
                scope=scope,
                source_run_id="run-s1x",
                created_at=CREATED_AT,
            )
            with self.subTest(record=label, context=context):
                # The two rules themselves.
                self.assertIs(visible_in(scope, context), expected)
                self.assertIs(_in_scope(skill, context), expected)
                # Through the public entry points of SKMATCH and SKEXP.
                entry = SkillWithState(
                    skill=skill, status=SkillStatus.ACTIVE, health=health(())
                )
                result = match_skills(self.TASK, [entry], context)
                self.assertIs(result.best is not None, expected)
                self.assertIs(any(c.skill == skill for c in result.candidates), expected)
                selected = select_for_task(self.TASK, [lesson], context, None)
                self.assertIs(selected == (lesson,), expected)
                # SKPLAN plans a skill exactly where it is visible.
                context_plan = SkillPlanContext(project_id=context, actions=CATALOG)
                if expected:
                    plan_steps(skill, {"path": "C:/a.txt"}, context_plan)
                else:
                    with self.assertRaises(SkillPlanError):
                        plan_steps(skill, {"path": "C:/a.txt"}, context_plan)


# -- 4. D15: scope moves ----------------------------------------------------


class ScopeMoveContractTests(unittest.TestCase):
    """D15 (completed): SKLIFE (versions and history) and SKDRAFT (edited
    drafts) apply the same scope-move rule: unchanged or project -> global
    only; global -> project and project -> another project are refused."""

    def lifecycle_move(self, parent_scope: SkillScope, child_scope: SkillScope) -> bool:
        """Whether SKLIFE accepts the move on every path: next_version,
        adapt (build side) and SkillHistory (read side). The paths must agree."""
        parent = make_skill(scope=parent_scope)
        changes = {"scope": child_scope, "summary": "Haftalik rapor dosyasini oku ve ozetle"}
        verdicts = []
        try:
            next_version(parent, changes, SkillOrigin.USER_EDIT, LATER)
            verdicts.append(True)
        except SkillLifecycleError:
            verdicts.append(False)
        try:
            adapt(parent, changes, LATER, skill_id="sk-adapted")
            verdicts.append(True)
        except SkillLifecycleError:
            verdicts.append(False)
        child = Skill(
            skill_id=parent.skill_id,
            version=2,
            name=parent.name,
            summary="Haftalik rapor dosyasini oku ve ozetle",
            scope=child_scope,
            inputs=parent.inputs,
            steps=parent.steps,
            checks=parent.checks,
            provenance=SkillProvenance(
                origin=SkillOrigin.USER_EDIT,
                source_run_id=None,
                parent_skill_id=parent.skill_id,
                parent_version=1,
                created_at=LATER,
            ),
        )
        try:
            SkillHistory(versions=(parent, child), events=())
            verdicts.append(True)
        except SkillLifecycleError:
            verdicts.append(False)
        self.assertEqual(len(set(verdicts)), 1, f"SKLIFE paths disagree: {verdicts}")
        return verdicts[0]

    def draft_move(self, original_scope: SkillScope, edited_scope: SkillScope) -> bool:
        """Whether SKDRAFT accepts an edited draft whose scope changed."""
        project_id = original_scope.project_id
        run = run_summary(
            [("Dosyayi oku: C:/notlar/a.txt", "file_read")],
            "C:/notlar/a.txt dosyasini oku",
            project_id=project_id,
        )
        original = build_drafts(run, created_at=CREATED_AT)[0]
        self.assertEqual(original.scope, original_scope)
        data = original.to_dict()
        data["scope"] = edited_scope.to_dict()
        data["summary"] = "Dosyayi oku ve ozetle"
        try:
            parse_user_edited_draft(data, original=original)
        except SkillRecordError:
            return False
        return True

    def assert_both(self, parent: SkillScope, child: SkillScope, expected: bool) -> None:
        self.assertIs(self.lifecycle_move(parent, child), expected, "SKLIFE")
        self.assertIs(self.draft_move(parent, child), expected, "SKDRAFT")

    def test_same_scope_is_accepted_by_both(self):
        for scope in (GLOBAL, SCOPE_A, SCOPE_B):
            with self.subTest(scope=scope):
                self.assert_both(scope, scope, True)

    def test_project_to_global_is_accepted_by_both(self):
        for scope in (SCOPE_A, SCOPE_B):
            with self.subTest(scope=scope):
                self.assert_both(scope, GLOBAL, True)

    def test_project_to_another_project_is_refused_by_both(self):
        self.assert_both(SCOPE_A, SCOPE_B, False)
        self.assert_both(SCOPE_B, SCOPE_A, False)

    def test_global_to_project_is_refused_by_both(self):
        for scope in (SCOPE_A, SCOPE_B):
            with self.subTest(scope=scope):
                self.assert_both(GLOBAL, scope, False)


# -- 5. D16: usable skills, declines -----------------------------------------


class UsableSkillContractTests(unittest.TestCase):
    """D16: SKMATCH never offers what SKLIFE says is unusable, and a decline or
    a denied approval leaves SKLIFE health and status untouched."""

    TASK = "Haftalik rapor dosyasini oku"

    def best_for(self, history: SkillHistory):
        skill = history.get(1)
        entry = SkillWithState(skill=skill, status=status(history), health=health(history))
        return match_skills(self.TASK, [entry], None, request=self.TASK)

    def test_usable_skill_none_is_never_matchs_best(self):
        skill = make_skill()
        cases = {
            "active": (),
            "deleted": (SkillEventKind.DELETED,),
            "disabled": (SkillEventKind.DISABLED,),
            "disabled again": (
                SkillEventKind.DISABLED,
                SkillEventKind.ENABLED,
                SkillEventKind.DISABLED,
            ),
            "deleted after enable": (SkillEventKind.ENABLED, SkillEventKind.DELETED),
            "deleted then enabled": (SkillEventKind.DELETED, SkillEventKind.ENABLED),
            "re-enabled": (SkillEventKind.DISABLED, SkillEventKind.ENABLED),
            "stale": (SkillEventKind.STALE_MARKED,),
            "disabled and stale": (SkillEventKind.STALE_MARKED, SkillEventKind.DISABLED),
        }
        unusable = 0
        for label, kinds in cases.items():
            history = history_of(skill, *kinds)
            result = self.best_for(history)
            with self.subTest(case=label):
                if usable_skill(history) is None:
                    unusable += 1
                    self.assertIn(status(history), (SkillStatus.DELETED, SkillStatus.DISABLED))
                    self.assertIsNone(result.best)
                    self.assertEqual(result.candidates, ())
                    self.assertEqual(result.needs_revalidation, ())
                    # And no use can be recorded for it either.
                    with self.assertRaises(SkillLifecycleError):
                        use_events(history, 1, True, LATER)
                elif status(history) is SkillStatus.STALE:
                    # Usable on an approved revalidation run, never automatically.
                    self.assertIsNone(result.best)
                    self.assertEqual([c.skill for c in result.needs_revalidation], [skill])
                else:
                    self.assertIs(result.kind, MatchKind.FIT)
                    self.assertEqual(result.best.skill, usable_skill(history))
        self.assertEqual(unusable, 6)

    def test_declines_and_denied_approvals_record_nothing(self):
        # There is no decline or denial event kind: the only events that move
        # health are executed uses.
        kinds = {kind.value for kind in SkillEventKind}
        for word in ("declin", "denied", "deny", "reject_use", "refus"):
            self.assertFalse(any(word in kind for kind in kinds), word)
        skill = make_skill()
        no_use = tuple(
            kind
            for kind in SkillEventKind
            if kind not in (SkillEventKind.USED_SUCCESS, SkillEventKind.USED_FAILURE)
        )
        for kind in no_use:
            with self.subTest(kind=kind):
                history = history_of(skill, kind)
                self.assertEqual(health(history), health(()))

    def test_a_denied_approval_run_does_not_mark_the_skill_stale(self):
        skill = make_skill()
        history = history_of(skill, SkillEventKind.USED_SUCCESS)
        before_health, before_status = health(history), status(history)
        self.assertIs(before_status, SkillStatus.ACTIVE)
        ref = SkillRef(skill_id=skill.skill_id, version=1)
        for outcome in (RunOutcome.REJECTED, RunOutcome.CANCELLED):
            with self.subTest(outcome=outcome):
                run = run_summary(
                    [
                        ("Haftalik rapor dosyasini oku", "file_read", outcome),
                    ],
                    self.TASK,
                    status_=outcome,
                    matched_skill=ref,
                )
                lessons = capture_candidates(
                    run, created_at=LATER, matched_skill_health=before_health
                )
                broken = [x for x in lessons if x.kind is ExperienceKind.BROKEN_WORKFLOW]
                self.assertEqual(broken, [])
                # Nothing to append, so SKLIFE health and status stay as they were.
                self.assertEqual(health(history), before_health)
                self.assertIs(status(history), before_status)
                self.assertIs(self.best_for(history).kind, MatchKind.FIT)

    def test_a_final_failure_does_mark_it_stale_through_skexp_and_sklife(self):
        # Positive control for the test above: the same wiring with a step
        # that FINALLY failed gives broken_workflow -> stale_marked -> stale.
        skill = make_skill()
        history = history_of(skill, SkillEventKind.USED_SUCCESS)
        run = run_summary(
            [("Haftalik rapor dosyasini oku", "file_read", RunOutcome.FAILED)],
            self.TASK,
            status_=RunOutcome.FAILED,
            matched_skill=SkillRef(skill_id=skill.skill_id, version=1),
        )
        lessons = capture_candidates(run, created_at=LATER, matched_skill_health=health(history))
        broken = [x for x in lessons if x.kind is ExperienceKind.BROKEN_WORKFLOW]
        self.assertEqual(len(broken), 1)
        marked = SkillHistory(
            versions=history.versions,
            events=history.events + (stale_mark_event(broken[0], LATER),),
        )
        self.assertIs(status(marked), SkillStatus.STALE)
        self.assertIsNone(self.best_for(marked).best)


# -- 6. S7: SKDRAFT -> SKFILTER ----------------------------------------------


class SecretChainContractTests(unittest.TestCase):
    """S7: values from run text become inputs in SKDRAFT, and SKFILTER then
    passes every draft field; no secret or personal value is left in one."""

    EMAIL = "ali.veli@example.com"
    URL = "https://example.com/reset?token=abc123XYZsecret987"
    UNC = BS * 2 + "fileserver" + BS + "paylasim" + BS + "rapor.xlsx"
    WIN = "C:" + BS + "Users" + BS + "salih" + BS + "Belgeler" + BS + "notlar.txt"
    SECRET_PIECES = (
        "ali.veli",
        "@",
        "example.com/reset",
        "token=",
        "abc123XYZsecret987",
        "fileserver",
        "paylasim",
        "salih",
        "Belgeler",
        "C:" + BS,
        BS,
    )

    def taught_run(self, project_id=None) -> RunSummary:
        return run_summary(
            [
                (f"Send the weekly report to {self.EMAIL}", "email_send"),
                (f"Open {self.URL} in the browser", "browser_open"),
                (f"Copy {self.UNC} to {self.WIN}", "file_copy"),
            ],
            f"Send the weekly report to {self.EMAIL}, open {self.URL} and copy "
            f"{self.UNC} to {self.WIN}",
            project_id=project_id,
        )

    def test_the_raw_run_text_is_refused_by_the_filter(self):
        # Not vacuous: SKFILTER refuses every raw value the run carried.
        run = self.taught_run()
        self.assertFalse(check_fields([("task-text", run.task_text)]).allowed)
        for number, step in enumerate(run.steps, start=1):
            with self.subTest(step=number):
                verdict = check_fields([(f"steps-{number}-action", step.action)])
                self.assertFalse(verdict.allowed)

    def test_every_draft_field_passes_and_holds_no_value(self):
        for project_id in (None, PROJECT_A):
            drafts = build_drafts(self.taught_run(project_id), created_at=CREATED_AT)
            self.assertTrue(drafts)
            names = set()
            for draft in drafts:
                fields = draft_fields(draft)
                with self.subTest(project=project_id, draft=draft.draft_id):
                    verdict = check_fields(fields)
                    self.assertTrue(verdict.allowed, verdict)
                    for name, text in fields:
                        self.assertTrue(check_fields([(name, text)]).allowed, name)
                        for piece in self.SECRET_PIECES:
                            self.assertNotIn(piece, text, name)
                    names.update(item.name for item in draft.inputs)
            # Every value became an input: the e-mail, the URL and both paths.
            self.assertTrue({"email", "url", "path", "path_2"} <= names, names)

    def test_field_names_follow_the_filter_grammar(self):
        draft = build_drafts(self.taught_run(), created_at=CREATED_AT)[0]
        fields = draft_fields(draft)
        self.assertIn("steps-1-action", dict(fields))
        # The bracket spelling is refused by SKFILTER (field_name), so the
        # id-grammar names above are the ones SKSTORE must use.
        refused = check_fields([("steps[1].action", fields[0][1])])
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.category, "field_name")


# -- 7. limits agree with Core ------------------------------------------------


class LimitsContractTests(unittest.TestCase):
    def skill_with(self, count: int) -> Skill:
        steps = tuple(
            SkillStep(
                index=index,
                action=f"Adim {index}: {{path}} dosyasini oku",
                capability="file_read",
                inputs=("path",),
                check=None,
            )
            for index in range(1, count + 1)
        )
        return make_skill(steps=steps)

    def test_skplan_refuses_more_steps_than_one_core_chain_holds(self):
        context = SkillPlanContext(project_id=None, actions=CATALOG)
        limit = contracts.MAX_CHAIN_STEPS
        chain = plan_steps(self.skill_with(limit), {"path": "C:/a.txt"}, context)
        self.assertEqual(len(chain), limit)
        with self.assertRaises(SkillPlanError):
            plan_steps(self.skill_with(limit + 1), {"path": "C:/a.txt"}, context)

    def test_a_longer_drafted_run_is_refused_not_cut(self):
        count = contracts.MAX_CHAIN_STEPS + 1
        run = run_summary(
            [(f"Rapor {n} icin C:/notlar/a.txt dosyasini oku", "file_read") for n in range(count)],
            "C:/notlar/a.txt dosyasini oku",
        )
        drafts = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(len(drafts[0].steps), count)
        values = {item.name: "1" for item in drafts[0].inputs}
        values["path"] = "C:/notlar/a.txt"
        with self.assertRaises(SkillPlanError) as caught:
            plan_steps(saved(drafts[0]), values, SkillPlanContext(actions=CATALOG))
        self.assertNotIsInstance(caught.exception, MissingInputsError)

    def test_skplan_bounds_are_cores(self):
        self.assertEqual(INPUT_VALUE_MAX, contracts.CHAIN_LOCATOR_MAX_CHARS)
        context = SkillPlanContext(project_id=None, actions=CATALOG)
        width = contracts.CHAIN_OBJECTIVE_MAX_CHARS
        fits = make_skill(
            steps=(
                SkillStep(index=1, action="x" * width, capability="file_read", inputs=(), check=None),
            ),
            inputs=(),
        )
        self.assertEqual(len(plan_steps(fits, {}, context)[0].objective), width)
        over = make_skill(
            steps=(
                SkillStep(
                    index=1, action="x" * (width + 1), capability="file_read", inputs=(), check=None
                ),
            ),
            inputs=(),
        )
        with self.assertRaises(SkillPlanError):
            plan_steps(over, {}, context)

    def test_skills_are_off_by_default_and_when_fail_closed(self):
        self.assertIs(isolated_settings(self).skills_enabled, False)
        self.assertIs(fail_closed_settings().skills_enabled, False)


# -- 8. D17: invisible text --------------------------------------------------

# ROUND5 D17: every Default_Ignorable_Code_Point (Unicode DerivedCoreProperties)
# as an explicit list, plus U+2800. Inclusive ranges.
D17_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
    (0x2800, 0x2800),
)
D17_CODE_POINTS = tuple(
    code_point for first, last in D17_RANGES for code_point in range(first, last + 1)
)


class InvisibleTextContractTests(unittest.TestCase):
    """D17: skill_types (text it accepts) and SKFILTER (the cleaned form it
    checks) handle the same invisible code points: whatever skill_types
    refuses to hold, SKFILTER removes before its checks, so it cannot split a
    value past the filter either."""

    TC_KIMLIK = "10000000146"  # checksum-valid test number

    def test_the_list_is_the_d17_list(self):
        self.assertEqual(len(D17_CODE_POINTS), 4175)
        self.assertEqual(len(set(D17_CODE_POINTS)), len(D17_CODE_POINTS))

    def test_skill_types_refuses_every_d17_code_point_in_text(self):
        accepted = []
        for code_point in D17_CODE_POINTS:
            text = "Rapor" + chr(code_point) + "dosyasi"
            for build in (
                lambda: SkillInput(name="path", description=text, required=True),
                lambda: SkillStep(
                    index=1, action=text, capability="file_read", inputs=(), check=None
                ),
            ):
                try:
                    build()
                except SkillRecordError:
                    continue
                accepted.append(hex(code_point))
        self.assertEqual(accepted, [])

    def test_skfilter_strips_every_d17_code_point_before_its_checks(self):
        self.assertFalse(check_fields([("summary", "TC " + self.TC_KIMLIK)]).allowed)
        allowed = []
        for code_point in D17_CODE_POINTS:
            split = self.TC_KIMLIK[:5] + chr(code_point) + self.TC_KIMLIK[5:]
            verdict = check_fields([("summary", "TC " + split)])
            if verdict.allowed:
                allowed.append(hex(code_point))
            elif verdict.category != "national_id":
                allowed.append((hex(code_point), verdict.category))
        self.assertEqual(allowed, [])


# -- 9. D18: placeholders and task values in matching --------------------------


class DraftMatchContractTests(unittest.TestCase):
    """D18: a skill SKDRAFT drafts from a run with an e-mail, a URL and a path
    is SIMILAR (asked, never auto-run) for the same task with those values
    removed (SKCORE's job); the user's own request with the values typed back in
    also asks while its clipped name and summary read the values differently
    (CDXINT54G C2)."""

    EMAIL = "ali.veli@example.com"
    URL = "https://example.com/rapor?id=7"
    PATH = "C:/notlar/a.txt"

    def test_a_drafted_skill_is_asked_for_the_task_without_its_values(self):
        task = (
            f"Haftalik raporu {self.PATH} dosyasindan oku, {self.URL} adresine yukle "
            f"ve linki {self.EMAIL} adresine gonder"
        )
        steps = [
            (f"Haftalik raporu {self.PATH} dosyasindan oku", "file_read"),
            (f"{self.PATH} dosyasini {self.URL} adresine yukle", "web_upload"),
            (f"{self.URL} linkini {self.EMAIL} adresine gonder", "email_send"),
        ]
        stripped = task
        for value in (self.PATH, self.URL, self.EMAIL):
            stripped = stripped.replace(value, "")
        for project_id in (None, PROJECT_A):
            with self.subTest(project=project_id):
                drafts = build_drafts(
                    run_summary(steps, task, project_id=project_id), created_at=CREATED_AT
                )
                self.assertEqual(len(drafts), 1)
                draft = drafts[0]
                self.assertEqual(
                    {item.name for item in draft.inputs}, {"path", "url", "email"}
                )
                for placeholder in ("{path}", "{url}", "{email}"):
                    self.assertIn(placeholder, draft.summary)
                skill = saved(draft)
                history = history_of(skill)
                entry = SkillWithState(
                    skill=skill, status=status(history), health=health(history)
                )
                result = match_skills(stripped, [entry], project_id)
                self.assertIs(result.kind, MatchKind.SIMILAR, result)
                self.assertEqual(result.best.skill, skill)
                self.assertEqual(result.best.coverage, 100)
                self.assertEqual(result.best.inputs, ())
                # The clipped name ends on {email}, so it binds "<email> adresine
                # gonder" while the summary binds the e-mail: two readings ask
                # (CDXINT54G C2, the same shape as "Read file {path}" + " now").
                result = match_skills(stripped, [entry], project_id, request=task)
                self.assertIs(result.kind, MatchKind.SIMILAR, result)
                self.assertEqual(result.best.inputs, ())


if __name__ == "__main__":
    unittest.main()
