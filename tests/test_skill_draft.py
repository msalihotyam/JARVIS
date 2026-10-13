"""V2.2 S1 save-as-skill: the offer policy and deterministic drafts from runs."""

from __future__ import annotations

import ast
import dataclasses
import json
import random
import unittest
from pathlib import Path
from unittest import mock

from jarvis import skill_draft
from jarvis.skill_draft import (
    CHAT_CAPABILITIES,
    EDITED_PAYLOAD_MAX_BYTES,
    GLOBAL_SUPPRESSION,
    MAX_DRAFTS,
    OfferDecision,
    OfferReason,
    build_drafts,
    parse_user_edited_draft,
    should_offer,
    task_kind,
)
from jarvis.skill_draft import _find_params, _placeholders_ok
from jarvis.skill_types import (
    FORBIDDEN_KEY_PATTERN,
    ID_PATTERN,
    MAX_INPUTS,
    NAME_MAX,
    TEXT_MAX,
    AuthorityKeyError,
    RunOutcome,
    RunStep,
    RunSummary,
    SkillDraft,
    SkillOrigin,
    SkillRecordError,
    SkillRef,
    SkillScopeKind,
    SkillStep,
)

CREATED_AT = "2026-10-14T08:00:00+00:00"
URL = "https://example.com/files/q3.pdf"
EMAIL = "ali@example.com"
BS = chr(92)


def step(action, capability="file_write", outcome=RunOutcome.COMPLETED):
    return RunStep(action=action, capability=capability, outcome=outcome, error=None)


def make_run(steps=None, **overrides):
    fields = {
        "run_id": "run-1",
        "task_text": (
            f"Download the Q3 report from {URL}, save it as reports/q3.pdf "
            "and check that it opens"
        ),
        "project_id": None,
        "status": RunOutcome.COMPLETED,
        "steps": (
            step(f"Download {URL}", "browser_download"),
            step("Save the report as reports/q3.pdf", "file_write"),
            step("Verify reports/q3.pdf opens", "file_read"),
        )
        if steps is None
        else tuple(steps),
        "user_correction": None,
        "matched_skill": None,
    }
    fields.update(overrides)
    return RunSummary(**fields)


def two_segment_run(**overrides):
    return make_run(
        steps=(
            step("Resize image 'holiday.png' to 800 pixels", "image_resize"),
            step("Save resized image as 'holiday-small.png'", "file_write"),
            step(f"Send meeting notes to {EMAIL}", "gmail_send"),
            step("Archive meeting notes in 'Notes/2026'", "file_write"),
        ),
        task_text=(
            f"Resize holiday.png to 800 pixels and send the meeting notes to {EMAIL}"
        ),
        **overrides,
    )


def offer(run, suppressions=(), offered=()):
    return should_offer(run, suppressions, offered)


class OfferPolicyTests(unittest.TestCase):
    def test_offer_table(self):
        kind = task_kind(make_run())
        cases = [
            ("repeatable 3-step run", make_run(), (), (), True, OfferReason.OFFER),
            (
                "trivial: one step, no value from the user",
                make_run(steps=[step("Open the downloads folder", "desktop_open")]),
                (), (), False, OfferReason.TRIVIAL,
            ),
            (
                "failed run",
                make_run(status=RunOutcome.FAILED),
                (), (), False, OfferReason.NOT_SUCCEEDED,
            ),
            (
                "rejected run",
                make_run(status=RunOutcome.REJECTED),
                (), (), False, OfferReason.NOT_SUCCEEDED,
            ),
            (
                "cancelled run",
                make_run(status=RunOutcome.CANCELLED),
                (), (), False, OfferReason.NOT_SUCCEEDED,
            ),
            (
                "chat-only run (no steps)",
                make_run(steps=[]),
                (), (), False, OfferReason.CHAT_ONLY,
            ),
            (
                "chat-only run (answer capabilities)",
                make_run(
                    steps=[
                        step("Answer the question about the weather", "chat"),
                        step(f"Search the web for {URL}", "web_research"),
                        step("Summarise what was found", None),
                    ]
                ),
                (), (), False, OfferReason.CHAT_ONLY,
            ),
            ("suppressed task kind", make_run(), {kind}, (), False, OfferReason.SUPPRESSED),
            (
                "globally suppressed",
                make_run(),
                [GLOBAL_SUPPRESSION], (), False, OfferReason.SUPPRESSED,
            ),
            (
                "already offered for this run",
                make_run(),
                (), {"run-1"}, False, OfferReason.ALREADY_OFFERED,
            ),
            (
                "existing skill used unchanged",
                make_run(matched_skill=SkillRef(skill_id="skill-a", version=2)),
                (), (), False, OfferReason.SKILL_USED,
            ),
        ]
        for label, run, suppressions, offered, expected, reason in cases:
            with self.subTest(label):
                decision = offer(run, suppressions, offered)
                self.assertIs(decision.offer, expected)
                self.assertIs(decision.reason, reason)
                self.assertEqual(decision.task_kind, task_kind(run))

    def test_other_suppressions_and_offers_do_not_block(self):
        decision = offer(make_run(), {"tk-" + "0" * 32}, {"run-2"})
        self.assertTrue(decision.offer)

    def test_a_matched_skill_with_a_correction_is_left_to_the_lifecycle(self):
        run = make_run(
            matched_skill=SkillRef(skill_id="skill-a", version=1),
            user_correction="Save it under archive instead",
        )
        self.assertIs(offer(run).reason, OfferReason.SKILL_USED)

    def test_one_step_with_a_value_the_user_gave_is_offered(self):
        run = make_run(
            steps=[step(f"Download {URL}", "browser_download")],
            task_text=f"Download {URL}",
        )
        self.assertIs(offer(run).reason, OfferReason.OFFER)

    def test_one_step_with_a_value_the_user_did_not_give_is_trivial(self):
        run = make_run(
            steps=[step(f"Download {URL}", "browser_download")],
            task_text="Get me the latest quarterly report",
        )
        self.assertIs(offer(run).reason, OfferReason.TRIVIAL)

    def test_a_value_inside_a_word_is_not_given(self):
        run = make_run(
            steps=[step("Open page 3 of the report", "browser_open")],
            task_text="Open the Q3 report",
        )
        self.assertIs(offer(run).reason, OfferReason.TRIVIAL)

    def test_only_executed_steps_count(self):
        run = make_run(
            steps=[
                step("Open the downloads folder", "desktop_open"),
                step("Delete the old copies", "file_delete", RunOutcome.REJECTED),
                step("Empty the recycle bin", "desktop_run", RunOutcome.FAILED),
                step("Close the window", "desktop_close", RunOutcome.CANCELLED),
            ]
        )
        self.assertIs(offer(run).reason, OfferReason.TRIVIAL)

    def test_one_real_step_among_answers_is_not_chat_only(self):
        run = make_run(
            steps=[
                step("Answer the question", "chat"),
                step("Save the answer as notes/answer.md", "file_write"),
            ]
        )
        self.assertIs(offer(run).reason, OfferReason.OFFER)

    def test_chat_capabilities_are_ids(self):
        for capability in CHAT_CAPABILITIES:
            self.assertIsNotNone(ID_PATTERN.fullmatch(capability))

    def test_bad_input_never_offers(self):
        run = make_run()
        cases = [
            ("run is a dict", run.to_dict(), (), ()),
            ("run is None", None, (), ()),
            ("suppressions is a string", run, "tk-x", ()),
            ("suppressions is a mapping", run, {"tk-x": True}, ()),
            ("suppressions is an iterator", run, iter(["tk-x"]), ()),
            ("suppressions holds a non-string", run, [1], ()),
            ("suppressions holds an unhashable item", run, [["tk-x"]], ()),
            ("suppressions is None", run, None, ()),
            ("offered ids is a string", run, (), "run-1"),
            ("offered ids is bytes", run, (), b"run-1"),
            ("offered ids holds a non-string", run, (), [None]),
        ]
        for label, bad_run, suppressions, offered in cases:
            with self.subTest(label):
                decision = should_offer(bad_run, suppressions, offered)
                self.assertFalse(decision.offer)
                self.assertIs(decision.reason, OfferReason.INVALID_INPUT)
                self.assertIsNone(decision.task_kind)

    def test_no_valid_draft_means_no_offer(self):
        numbers = " ".join(str(n) for n in range(10, 10 + MAX_INPUTS + 1))
        run = make_run(
            steps=[
                step(f"Type the values {numbers}", "desktop_type"),
                step("Press enter", "desktop_key"),
            ],
            task_text=f"Type {numbers}",
        )
        self.assertEqual(build_drafts(run, created_at=CREATED_AT), ())
        self.assertIs(offer(run).reason, OfferReason.NO_DRAFT)

    def test_every_offer_has_a_draft(self):
        for run in (make_run(), two_segment_run()):
            with self.subTest(run=run.run_id):
                self.assertTrue(offer(run).offer)
                self.assertTrue(build_drafts(run, created_at=CREATED_AT))

    def test_decision_carries_no_authority(self):
        names = {item.name for item in dataclasses.fields(OfferDecision)}
        self.assertEqual(names, {"offer", "reason", "task_kind"})
        for name in names:
            self.assertIsNone(FORBIDDEN_KEY_PATTERN.search(name))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            offer(make_run()).offer = False  # type: ignore[misc]


class TaskKindTests(unittest.TestCase):
    def test_kind_is_a_stable_id(self):
        kind = task_kind(make_run())
        self.assertEqual(kind, task_kind(make_run()))
        self.assertRegex(kind, r"^tk-[0-9a-f]{32}$")
        self.assertIsNotNone(ID_PATTERN.fullmatch(kind))

    def test_values_and_synonyms_do_not_change_the_kind(self):
        base = make_run(
            steps=[
                step(f"Download {URL}", "browser_download"),
                step("Save it as reports/q3.pdf", "file_write"),
            ]
        )
        other = make_run(
            run_id="run-2",
            task_text="Something else entirely",
            steps=[
                step("download https://other.example.org/a.csv", "browser_download"),
                step("Store it as data/a.csv", "file_write"),
            ],
        )
        self.assertEqual(task_kind(base), task_kind(other))

    def test_turkish_verbs_normalise_like_english(self):
        english = make_run(
            steps=[step("Open the browser", "browser_open"), step("Send the mail", "gmail_send")]
        )
        turkish = make_run(
            steps=[
                step("Tarayıcıyı aç", "browser_open"),
                step("E-postayı gönder", "gmail_send"),
            ]
        )
        self.assertEqual(task_kind(english), task_kind(turkish))

    def test_dotless_i_verbs_fold_through_fold_token(self):
        # PLAN D13: lower case (dotless i), capitals (dotted capital I) and the
        # ASCII spelling of a Turkish verb all give the English task kind.
        groups = {
            "file_delete": (
                "Delete the file",
                ("Dosyayı kaldır", "DOSYAYI KALDIR", "Dosyayi kaldir"),
            ),
            "desktop_run": (
                "Run the tests",
                ("Testleri çalıştır", "TESTLERİ ÇALIŞTIR", "Testleri calistir"),
            ),
            "file_move": (
                "Move the report",
                ("Raporu taşı", "RAPORU TAŞI", "Raporu tasi"),
            ),
            "gmail_send": (
                "Reply to the mail",
                ("Maili yanıtla", "MAİLİ YANITLA", "Maili yanitla"),
            ),
        }
        for capability, (english, spellings) in groups.items():
            expected = task_kind(
                make_run(steps=[step(english, capability), step("Save the log", "file_write")])
            )
            for spelling in spellings:
                with self.subTest(spelling):
                    run = make_run(
                        steps=[step(spelling, capability), step("Save the log", "file_write")]
                    )
                    self.assertEqual(task_kind(run), expected)

    def test_a_suppressed_ascii_spelling_suppresses_the_turkish_one(self):
        def run(action):
            return make_run(
                steps=[step(action, "desktop_run"), step("Sonucu logs/out.txt dosyasina kaydet")],
                task_text="Testleri calistir ve sonucu kaydet",
            )

        suppressed = task_kind(run("Testleri calistir"))
        self.assertTrue(offer(run("Testleri çalıştır")).offer)
        for action in ("Testleri çalıştır", "TESTLERİ ÇALIŞTIR", "testleri çalıştır"):
            with self.subTest(action):
                self.assertIs(offer(run(action), {suppressed}).reason, OfferReason.SUPPRESSED)

    def test_the_module_keeps_no_fold_of_its_own(self):
        source = Path(skill_draft.__file__).read_text(encoding="utf-8")
        self.assertNotIn("casefold", source)
        self.assertNotIn("unicodedata", source)
        self.assertIn("fold_token", source)

    def test_capability_and_verb_change_the_kind(self):
        base = make_run()
        other_capability = make_run(
            steps=[*base.steps[:2], step("Verify reports/q3.pdf opens", "desktop_open")]
        )
        other_verb = make_run(
            steps=[*base.steps[:2], step("Delete reports/q3.pdf", "file_read")]
        )
        self.assertNotEqual(task_kind(base), task_kind(other_capability))
        self.assertNotEqual(task_kind(base), task_kind(other_verb))

    def test_only_executed_steps_make_the_kind(self):
        base = make_run()
        with_rejected = make_run(
            steps=[*base.steps, step("Delete everything", "file_delete", RunOutcome.REJECTED)]
        )
        self.assertEqual(task_kind(base), task_kind(with_rejected))


class BuildDraftTests(unittest.TestCase):
    def test_three_step_run_gives_one_exact_draft(self):
        drafts = build_drafts(make_run(), created_at=CREATED_AT)
        self.assertEqual(len(drafts), 1)
        data = drafts[0].to_dict()
        self.assertRegex(data.pop("draft_id"), r"^draft-[0-9a-f]{32}$")
        self.assertEqual(
            data,
            {
                "name": "Download the Q3 report from {url}, save it as {path} and check that it opens",
                "summary": (
                    "Download the Q3 report from {url}, save it as {path} "
                    "and check that it opens"
                ),
                "scope": {"kind": "global", "project_id": None},
                "inputs": [
                    {
                        "name": "url",
                        "description": "A url the user gave in the task",
                        "required": True,
                    },
                    {
                        "name": "path",
                        "description": "A path the user gave in the task",
                        "required": True,
                    },
                ],
                "steps": [
                    {
                        "index": 1,
                        "action": "Download {url}",
                        "capability": "browser_download",
                        "inputs": ["url"],
                        "check": None,
                    },
                    {
                        "index": 2,
                        "action": "Save the report as {path}",
                        "capability": "file_write",
                        "inputs": ["path"],
                        "check": None,
                    },
                    {
                        "index": 3,
                        "action": "Verify {path} opens",
                        "capability": "file_read",
                        "inputs": ["path"],
                        "check": None,
                    },
                ],
                "checks": ["Verify {path} opens"],
                "provenance": {
                    "origin": "taught_from_run",
                    "source_run_id": "run-1",
                    "parent_skill_id": None,
                    "parent_version": None,
                    "created_at": CREATED_AT,
                },
                "reason": (
                    "Taught from run run-1: 3 executed steps. "
                    "Saved only if you approve this draft."
                ),
                "target_skill_id": None,
            },
        )

    def test_drafts_are_deterministic_and_round_trip(self):
        first = build_drafts(two_segment_run(), created_at=CREATED_AT)
        second = build_drafts(two_segment_run(), created_at=CREATED_AT)
        self.assertEqual(first, second)
        for draft in first:
            self.assertEqual(SkillDraft.from_dict(json.loads(json.dumps(draft.to_dict()))), draft)

    def test_default_created_at_is_a_valid_timestamp(self):
        drafts = build_drafts(make_run())
        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0].provenance.created_at.endswith("+00:00"))

    def test_two_independent_segments_give_two_drafts(self):
        drafts = build_drafts(two_segment_run(), created_at=CREATED_AT)
        self.assertEqual(len(drafts), 2)
        first, second = drafts
        self.assertEqual(
            [s.action for s in first.steps],
            ["Resize image '{path}' to {number} pixels", "Save resized image as '{path_2}'"],
        )
        self.assertEqual([i.name for i in first.inputs], ["path", "number", "path_2"])
        self.assertEqual(
            [s.action for s in second.steps],
            ["Send meeting notes to {email}", "Archive meeting notes in '{text}'"],
        )
        self.assertEqual([i.name for i in second.inputs], ["email", "text"])
        self.assertEqual([s.index for s in second.steps], [1, 2])
        self.assertNotEqual(first.draft_id, second.draft_id)
        self.assertTrue(first.summary.startswith("Part 1 of 2: "))
        self.assertTrue(second.summary.startswith("Part 2 of 2: "))
        self.assertIn("{email}", second.summary)
        self.assertNotIn(EMAIL, json.dumps([d.to_dict() for d in drafts]))
        for draft in drafts:
            self.assertIs(draft.provenance.origin, SkillOrigin.TAUGHT_FROM_RUN)
            self.assertEqual(draft.provenance.source_run_id, "run-1")
            self.assertIsNone(draft.target_skill_id)

    def test_dependent_steps_stay_one_draft(self):
        # The download and the save share no word, but the first segment has
        # no goal word of its own: no positive evidence of independence.
        self.assertEqual(len(build_drafts(make_run(), created_at=CREATED_AT)), 1)
        shared_value = make_run(
            steps=[
                step(f"Download {URL} as a spreadsheet", "browser_download"),
                step(f"Send {URL} to the finance team", "gmail_send"),
            ]
        )
        self.assertEqual(len(build_drafts(shared_value, created_at=CREATED_AT)), 1)

    def test_segments_that_meet_again_are_one_draft(self):
        run = make_run(
            steps=[
                step("Resize image 'holiday.png' to 800 pixels", "image_resize"),
                step(f"Send meeting notes to {EMAIL}", "gmail_send"),
                step("Attach the resized image 'holiday.png' too", "gmail_send"),
            ],
            task_text=f"Resize holiday.png and mail the notes to {EMAIL}",
        )
        drafts = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(len(drafts[0].steps), 3)

    def test_a_segment_that_is_not_repeatable_alone_keeps_the_run_whole(self):
        run = make_run(
            steps=[
                step("Resize image 'holiday.png' to 800 pixels", "image_resize"),
                step("Save resized image as 'holiday-small.png'", "file_write"),
                step("Send meeting notes to bob@example.org", "gmail_send"),
            ],
            task_text="Resize holiday.png and send the meeting notes",
        )
        drafts = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(len(drafts[0].steps), 3)

    def test_more_segments_than_the_cap_make_one_draft(self):
        topics = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
        steps = []
        for n, topic in enumerate(topics[: MAX_DRAFTS + 1]):
            steps.append(step(f"Export {topic} sheet to 'out{n}.csv'", "file_write"))
            steps.append(step(f"Verify {topic} sheet in 'out{n}.csv'", "file_read"))
        run = make_run(steps=steps, task_text="Export the sheets")
        drafts = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(len(drafts[0].steps), len(steps))

    def test_only_executed_steps_are_drafted(self):
        base = make_run()
        run = make_run(
            steps=[
                base.steps[0],
                step("Delete D:\\Temp\\old\\secret.txt", "file_delete", RunOutcome.REJECTED),
                base.steps[1],
                step("Upload reports/q3.pdf", "browser_upload", RunOutcome.FAILED),
                base.steps[2],
            ]
        )
        drafts = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(
            [s.action for s in drafts[0].steps],
            ["Download {url}", "Save the report as {path}", "Verify {path} opens"],
        )
        text = json.dumps(drafts[0].to_dict())
        self.assertNotIn("secret", text)
        self.assertNotIn("Upload", text)

    def test_scope_is_the_runs_project_else_global(self):
        project = build_drafts(make_run(project_id="proj-a"), created_at=CREATED_AT)[0]
        self.assertIs(project.scope.kind, SkillScopeKind.PROJECT)
        self.assertEqual(project.scope.project_id, "proj-a")
        global_ = build_drafts(make_run(), created_at=CREATED_AT)[0]
        self.assertIs(global_.scope.kind, SkillScopeKind.GLOBAL)
        self.assertIsNone(global_.scope.project_id)
        for draft in build_drafts(two_segment_run(project_id="proj-b"), created_at=CREATED_AT):
            self.assertEqual(draft.scope.project_id, "proj-b")

    def test_values_become_inputs_and_leave_the_text(self):
        token = "ghp" + "_" + "abc123DEF456ghi789JKL000"
        run = make_run(
            steps=[
                step(
                    f"Log in to {URL} with key {token} on 2025-03-01 at 10:30",
                    "browser_login",
                ),
                step('Search for "quarterly totals" in C:\\Data\\q3.xlsx', "file_read"),
            ],
            task_text="Find the quarterly totals in my Q3 workbook",
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        names = [item.name for item in draft.inputs]
        self.assertEqual(names, ["url", "value", "date", "time", "text", "path"])
        text = json.dumps(draft.to_dict())
        for value in (URL, token, "2025-03-01", "10:30", "C:\\\\Data", "q3.xlsx"):
            self.assertNotIn(value, text)
        descriptions = {item.name: item.description for item in draft.inputs}
        self.assertEqual(descriptions["text"], "A text the user gave in the task")
        self.assertEqual(descriptions["url"], "A url that may change between runs")
        self.assertTrue(all(item.required for item in draft.inputs))

    def test_a_repeated_value_is_one_input(self):
        draft = build_drafts(make_run(), created_at=CREATED_AT)[0]
        self.assertEqual([i.name for i in draft.inputs], ["url", "path"])
        self.assertEqual(draft.steps[1].inputs, ("path",))
        self.assertEqual(draft.steps[2].inputs, ("path",))

    def test_names_and_summaries_fit_their_limits(self):
        long_task = "Prepare " + "the very long monthly report " * 120
        run = make_run(
            steps=[
                step("Collect " + "monthly figures " * 25 + f"from {URL}", "browser_read"),
                step("Write the monthly report to reports/m.docx", "file_write"),
            ],
            task_text=long_task[:2000],
        )
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        self.assertLessEqual(len(draft.name), NAME_MAX)
        self.assertLessEqual(len(draft.summary), TEXT_MAX)
        self.assertTrue(draft.summary.endswith("..."))
        self.assertEqual(draft.name, draft.name.strip())

    def test_a_multiline_task_makes_a_one_line_name(self):
        run = make_run(task_text=f"Get the report\nfrom {URL}\tand save reports/q3.pdf")
        draft = build_drafts(run, created_at=CREATED_AT)[0]
        self.assertTrue(draft.name.isprintable())
        self.assertEqual(draft.name, "Get the report from {url} and save {path}")

    def test_no_executed_steps_no_draft(self):
        self.assertEqual(build_drafts(make_run(steps=[]), created_at=CREATED_AT), ())
        trivial = make_run(steps=[step("Open the downloads folder", "desktop_open")])
        self.assertEqual(build_drafts(trivial, created_at=CREATED_AT), ())

    def test_a_too_long_templated_step_is_no_draft(self):
        action = ("'a' " * 125).strip()
        self.assertLessEqual(len(action), TEXT_MAX)
        run = make_run(steps=[step(action, "desktop_type"), step("Press enter", "desktop_key")])
        self.assertEqual(build_drafts(run, created_at=CREATED_AT), ())

    def test_build_drafts_refuses_a_non_run(self):
        with self.assertRaises(SkillRecordError):
            build_drafts(make_run().to_dict(), created_at=CREATED_AT)  # type: ignore[arg-type]
        with self.assertRaises(SkillRecordError):
            build_drafts(make_run(), created_at="yesterday")


def plan_step(skill_step, values):
    """The SKPLAN side of PLAN D12, as a reference: the objective with every
    placeholder replaced, and the target = the value of the first input."""
    objective = skill_step.action
    for name in skill_step.inputs:
        objective = objective.replace("{" + name + "}", values[name])
    target = values[skill_step.inputs[0]] if skill_step.inputs else ""
    return objective, target


class StepContractTests(unittest.TestCase):
    """PLAN D12: primary object first in step.inputs; every brace is a
    placeholder of one of the step's inputs."""

    def test_shared_example_one_input(self):
        path = "C:/notlar/a.txt"
        run = make_run(
            steps=[step(f"Dosyayi oku: {path}", "file_read")],
            task_text=f"{path} dosyasini oku",
        )
        (draft,) = build_drafts(run, created_at=CREATED_AT)
        (first,) = draft.steps
        self.assertEqual(first.action, "Dosyayi oku: {path}")
        self.assertEqual(first.inputs, ("path",))
        objective, target = plan_step(first, {"path": path})
        self.assertEqual(target, path)
        self.assertEqual(objective, "Dosyayi oku: C:/notlar/a.txt")

    def test_shared_example_primary_object_first(self):
        path = "C:/notlar/a.txt"
        run = make_run(
            steps=[step(f'"rapor" metnini {path} dosyasinda ara', "file_search")],
            task_text=f'"rapor" metnini {path} dosyasinda ara',
        )
        (draft,) = build_drafts(run, created_at=CREATED_AT)
        (first,) = draft.steps
        # The quotes typed in the run stay; the text comes second.
        self.assertEqual(first.action, '"{text}" metnini {path} dosyasinda ara')
        self.assertEqual(first.inputs, ("path", "text"))
        objective, target = plan_step(first, {"path": path, "text": "rapor"})
        self.assertEqual(target, path)
        self.assertEqual(objective, '"rapor" metnini C:/notlar/a.txt dosyasinda ara')
        shared = SkillStep(
            index=1,
            action="{text} metnini {path} dosyasinda ara",
            capability="file_search",
            inputs=("path", "text"),
            check=None,
        )
        self.assertTrue(_placeholders_ok(shared.action, shared.inputs))
        objective, target = plan_step(shared, {"path": path, "text": "rapor"})
        self.assertEqual(target, path)
        self.assertEqual(objective, "rapor metnini C:/notlar/a.txt dosyasinda ara")

    def test_primary_object_is_the_first_path_url_or_email(self):
        cases = [
            ('Search for "quarterly totals" in C:/Data/q3.xlsx', ("path", "text")),
            (f"Send report.pdf to {EMAIL}", ("path", "email")),
            (f"Mail {EMAIL} the file report.pdf", ("email", "path")),
            ("Wait 5 minutes after 2026-01-02", ("number", "date")),
            ("On 2026-01-02 download https://a.example.com/x", ("url", "date")),
        ]
        for action, expected in cases:
            with self.subTest(action):
                run = make_run(steps=[step(action), step("Close the window", "desktop_close")])
                (draft,) = build_drafts(run, created_at=CREATED_AT)
                self.assertEqual(draft.steps[0].inputs, expected)

    def test_literal_braces_become_parentheses(self):
        run = make_run(
            steps=[
                step("Download https://a.example.com/x", "browser_download"),
                step("Append the line {url} done to log.txt", "file_write"),
                step('Write {"a": 1} and {{x}} to out.json', "file_write"),
            ],
            task_text="Download it and log {it}",
        )
        (draft,) = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(draft.name, "Download it and log (it)")
        second, third = draft.steps[1], draft.steps[2]
        self.assertEqual(second.action, "Append the line (url) done to {path}")
        self.assertEqual(second.inputs, ("path",))
        self.assertEqual(third.action, 'Write ("{text}": {number}) and ((x)) to {path_2}')
        self.assertEqual(third.inputs, ("path_2", "text", "number"))

    def test_every_drafted_brace_is_a_placeholder_of_the_step(self):
        runs = [make_run(), two_segment_run()]
        runs.append(
            make_run(
                steps=[
                    step("Open {braces} at https://a.example.com/{id}?q={x}", "browser_open"),
                    step("Save } and { to C:/out/{name}.txt", "file_write"),
                ],
                task_text="Save the page",
            )
        )
        for run in runs:
            for draft in build_drafts(run, created_at=CREATED_AT):
                for item in draft.steps:
                    with self.subTest(item.action):
                        self.assertTrue(_placeholders_ok(item.action, item.inputs))

    def test_tab_newline_and_cr_in_run_text_draft_one_line(self):
        # SKS1X open issue 2: the shared example with a line break in the run text.
        path = "C:/notlar/a.txt"
        breaks = {
            "newline": chr(10),
            "tab": chr(9),
            "carriage return": chr(13),
            "CR LF": chr(13) + chr(10),
            "spaces around a newline": "  " + chr(10) + chr(9) + " ",
        }
        for label, gap in breaks.items():
            with self.subTest(label):
                run = make_run(
                    steps=[step("Dosyayi oku:" + gap + path, "file_read")],
                    task_text=f"{path} dosyasini oku",
                )
                self.assertTrue(offer(run).offer)
                (draft,) = build_drafts(run, created_at=CREATED_AT)
                (first,) = draft.steps
                self.assertEqual(first.action, "Dosyayi oku: {path}")
                self.assertEqual(first.inputs, ("path",))
                objective, target = plan_step(first, {"path": path})
                self.assertEqual(target, path)
                self.assertEqual(objective, "Dosyayi oku: C:/notlar/a.txt")
        run = make_run(
            steps=[step(chr(9) + " Dosyayi" + chr(10) * 2 + "oku:  " + path + " " + chr(13), "file_read")],
            task_text=f"{path} dosyasini oku",
        )
        (draft,) = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(draft.steps[0].action, "Dosyayi oku: {path}")

    def test_reviewer_multiline_step_drafts_one_line(self):
        # SKDRAFT re-review MED (D12): a newline inside a run step's text.
        run = make_run(
            steps=[
                step("Read C:/x/a.txt" + chr(10) + "then keep it", "file_read"),
                step("Search 'abc' in C:/x/a.txt", "file_search"),
            ],
            task_text="Read C:/x/a.txt" + chr(10) + "and search" + chr(9) + "'abc' in it",
        )
        self.assertTrue(offer(run).offer)
        (draft,) = build_drafts(run, created_at=CREATED_AT)
        first, second = draft.steps
        self.assertEqual(first.action, "Read {path} then keep it")
        self.assertEqual(second.action, "Search '{text}' in {path}")
        for text in (draft.name, draft.summary, first.action, second.action, *draft.checks):
            with self.subTest(text):
                for ch in (chr(9), chr(10), chr(13)):
                    self.assertNotIn(ch, text)
                self.assertEqual(text, text.strip())
                self.assertNotIn("  ", text)
        self.assertEqual(parse_user_edited_draft(draft.to_dict(), original=draft), draft)

    def test_the_brace_rule_itself(self):
        self.assertTrue(_placeholders_ok("Read {path}", ("path",)))
        self.assertTrue(_placeholders_ok("Wait", ()))
        self.assertFalse(_placeholders_ok("Read {path}", ()))
        self.assertFalse(_placeholders_ok("Read {other}", ("path",)))
        self.assertFalse(_placeholders_ok("Read {path", ("path",)))
        self.assertFalse(_placeholders_ok("Read path}", ("path",)))
        self.assertFalse(_placeholders_ok("Read {{path}}", ("path",)))
        self.assertFalse(_placeholders_ok("Read {}", ("path",)))


class ValueDetectionTests(unittest.TestCase):
    """UNC paths, non-ASCII and local e-mail addresses and whole URLs (query
    string included) become inputs; nothing of them stays in the text."""

    UNC = BS * 2 + "fileserver" + BS + "hr" + BS + "salaries-2026.xlsx"
    CASES = [
        (UNC, "path"),
        (BS * 2 + "nas01" + BS + "backup", "path"),
        ("ali@örnek.com", "email"),
        ("çağrı.yılmaz@example.com", "email"),
        ("admin@localhost", "email"),
        ("https://example.com/reset?token=abc123XYZ&user=ali", "url"),
        ("www.example.com/reset?token=abc123XYZ", "url"),
        ("example.com/reset?token=abc123XYZ", "url"),
        ("ftp://files.example.com/x?sig=s3cr3t", "url"),
        ("file:///home/salih/secret.txt", "url"),
    ]

    def test_each_value_is_one_whole_input(self):
        for value, kind in self.CASES:
            with self.subTest(value):
                params = _find_params(f"Open {value} now")
                self.assertEqual([(p.value, p.kind) for p in params], [(value, kind)])

    def test_nothing_of_the_value_stays_in_the_draft(self):
        leftovers = {
            self.UNC: ("fileserver", "salaries", BS + "hr"),
            "ali@örnek.com": ("ali@", "örnek"),
            "çağrı.yılmaz@example.com": ("çağrı", "yılmaz"),
            "admin@localhost": ("admin", "localhost"),
            "www.example.com/reset?token=abc123XYZ": ("token", "abc123XYZ", "reset"),
            "example.com/reset?token=abc123XYZ": ("token", "abc123XYZ", "reset"),
            "https://example.com/reset?token=abc123XYZ&user=ali": ("token", "abc123XYZ", "user="),
        }
        for value, parts in leftovers.items():
            with self.subTest(value):
                run = make_run(
                    steps=[
                        step(f"Use {value} for the export", "file_read"),
                        step(f"Copy the export of {value}.", "file_write"),
                    ],
                    task_text=f"Export from {value}",
                )
                (draft,) = build_drafts(run, created_at=CREATED_AT)
                text = json.dumps(draft.to_dict(), ensure_ascii=False)
                for part in parts:
                    self.assertNotIn(part, text)
                self.assertEqual(len(draft.inputs), 1)
                name = draft.inputs[0].name
                self.assertEqual(draft.steps[1].action, "Copy the export of {%s}." % name)


class EditedDraftTests(unittest.TestCase):
    def setUp(self):
        self.original = build_drafts(make_run(project_id="proj-a"), created_at=CREATED_AT)[0]

    def edited(self):
        data = self.original.to_dict()
        data["name"] = "Fetch and file the quarterly report"
        data["scope"] = {"kind": "project", "project_id": "proj-a"}
        data["steps"][1]["action"] = "Save it as {path} in the archive"
        return data

    def test_a_valid_edit_parses(self):
        draft = parse_user_edited_draft(self.edited(), original=self.original)
        self.assertEqual(draft.name, "Fetch and file the quarterly report")
        self.assertIs(draft.scope.kind, SkillScopeKind.PROJECT)
        self.assertEqual(draft.provenance, self.original.provenance)

    def test_json_text_parses(self):
        draft = parse_user_edited_draft(json.dumps(self.edited()), original=self.original)
        self.assertEqual(draft.steps[1].action, "Save it as {path} in the archive")

    def test_a_forbidden_key_is_refused(self):
        cases = {
            "top level approved": lambda d: d.update(approved=True),
            "auto_run on a step": lambda d: d["steps"][0].update(auto_run=True),
            "risk on an input": lambda d: d["inputs"][0].update(riskLevel="low"),
            "remember in provenance": lambda d: d["provenance"].update(remember=True),
            "trusted scope": lambda d: d["scope"].update(trusted=True),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                data = self.edited()
                mutate(data)
                with self.assertRaises(AuthorityKeyError):
                    parse_user_edited_draft(data, original=self.original)
                with self.assertRaises(AuthorityKeyError):
                    parse_user_edited_draft(json.dumps(data), original=self.original)

    def test_malformed_edits_are_refused(self):
        cases = {
            "unknown key": lambda d: d.update(colour="blue"),
            "missing key": lambda d: d.pop("checks"),
            "wrong type": lambda d: d.update(name=7),
            "undeclared input": lambda d: d["steps"][0].update(inputs=["nope"]),
            "bad step numbering": lambda d: d["steps"][0].update(index=2),
            "control character": lambda d: d.update(summary="a" + chr(0x202E) + "b"),
            "project scope without id": lambda d: d.update(
                scope={"kind": "project", "project_id": None}
            ),
            "no steps": lambda d: d.update(steps=[]),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                data = self.edited()
                mutate(data)
                with self.assertRaises(SkillRecordError):
                    parse_user_edited_draft(data, original=self.original)

    def test_lineage_cannot_change(self):
        cases = {
            "draft id": lambda d: d.update(draft_id="draft-other"),
            "source run": lambda d: d["provenance"].update(source_run_id="run-9"),
            "created at": lambda d: d["provenance"].update(
                created_at="2026-10-15T08:00:00+00:00"
            ),
            "version proposal of another skill": lambda d: (
                d.update(target_skill_id="skill-x"),
                d["provenance"].update(
                    origin="user_edit",
                    source_run_id=None,
                    parent_skill_id="skill-x",
                    parent_version=1,
                ),
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                data = self.edited()
                mutate(data)
                with self.assertRaises(SkillRecordError):
                    parse_user_edited_draft(data, original=self.original)

    def test_bad_payloads_are_refused(self):
        good = json.dumps(self.edited())
        cases = {
            "not JSON": "{not json",
            "repeated key": good[:-1] + ', "name": "Other"}',
            "too large": " " * (EDITED_PAYLOAD_MAX_BYTES + 1) + good,
            "deeply nested": "[" * 100_000 + "]" * 100_000,
            "a list": "[]",
            "None": None,
            "bytes": good.encode("utf-8"),
            "lone surrogate": chr(0xD800),
            "lone surrogate inside the JSON text": good.replace(
                '"summary": "', '"summary": "' + chr(0xDC00), 1
            ),
            "escaped lone surrogate": good.replace(
                '"summary": "', '"summary": "' + chr(92) + "ud800", 1
            ),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                with self.assertRaises(SkillRecordError):
                    parse_user_edited_draft(payload, original=self.original)
        with self.assertRaises(SkillRecordError):
            parse_user_edited_draft(self.edited(), original=self.edited())


class EditedDraftGuardTests(unittest.TestCase):
    """The reviewer's probes: original is required, lineage and scope hold."""

    def setUp(self):
        self.original = build_drafts(make_run(project_id="proj-a"), created_at=CREATED_AT)[0]

    def hijack(self):
        data = self.original.to_dict()
        data["target_skill_id"] = "victim-skill"
        data["provenance"].update(
            origin="user_edit",
            source_run_id=None,
            parent_skill_id="victim-skill",
            parent_version=3,
        )
        return data

    def test_original_is_required(self):
        with self.assertRaises(TypeError):
            parse_user_edited_draft(self.hijack())  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            parse_user_edited_draft(self.original.to_dict())  # type: ignore[call-arg]
        payload = self.original.to_dict()
        for bad in (None, self.original.to_dict(), "draft", 0):
            with self.subTest(original=type(bad).__name__):
                with self.assertRaises(SkillRecordError):
                    parse_user_edited_draft(payload, original=bad)  # type: ignore[arg-type]

    def test_reviewer_probes_are_refused(self):
        other_ids = self.original.to_dict()
        other_ids["draft_id"] = "draft-attacker"
        other_ids["provenance"]["source_run_id"] = "run-other"
        only_run = self.original.to_dict()
        only_run["provenance"]["source_run_id"] = "run-other"
        cases = {
            "version proposal of another skill": self.hijack(),
            "draft id and source run": other_ids,
            "source run only": only_run,
        }
        for label, data in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(SkillRecordError, "lineage"):
                    parse_user_edited_draft(data, original=self.original)
                with self.assertRaisesRegex(SkillRecordError, "lineage"):
                    parse_user_edited_draft(json.dumps(data), original=self.original)

    def test_a_project_draft_stays_in_its_project_or_becomes_global(self):
        same = parse_user_edited_draft(self.original.to_dict(), original=self.original)
        self.assertEqual(same.scope, self.original.scope)
        data = self.original.to_dict()
        data["scope"] = {"kind": "global", "project_id": None}
        widened = parse_user_edited_draft(data, original=self.original)
        self.assertIs(widened.scope.kind, SkillScopeKind.GLOBAL)
        data["scope"] = {"kind": "project", "project_id": "proj-b"}
        with self.assertRaisesRegex(SkillRecordError, "scope"):
            parse_user_edited_draft(data, original=self.original)

    def test_a_global_draft_never_moves_into_a_project(self):
        original = build_drafts(make_run(), created_at=CREATED_AT)[0]
        self.assertIs(original.scope.kind, SkillScopeKind.GLOBAL)
        self.assertEqual(parse_user_edited_draft(original.to_dict(), original=original), original)
        data = original.to_dict()
        data["scope"] = {"kind": "project", "project_id": "proj-a"}
        with self.assertRaisesRegex(SkillRecordError, "scope"):
            parse_user_edited_draft(data, original=original)

    def test_an_edited_step_keeps_the_brace_rule(self):
        cases = {
            "unknown placeholder": "Save it as {path} in {archive}",
            "placeholder of an input the step does not list": "Download {url} to {path}",
            "lone open brace": "Save it as {path} {",
            "lone close brace": "Save it as {path} }",
        }
        for label, action in cases.items():
            with self.subTest(label):
                data = self.original.to_dict()
                data["steps"][1]["action"] = action
                with self.assertRaisesRegex(SkillRecordError, "brace"):
                    parse_user_edited_draft(data, original=self.original)

    def test_an_edited_step_action_is_one_line(self):
        # PLAN D12: an edit that puts a tab, newline or CR in a step action is refused.
        for code in (9, 10, 13):
            with self.subTest(code=code):
                data = self.original.to_dict()
                data["steps"][1]["action"] = "Save it as" + chr(code) + "{path}"
                with self.assertRaisesRegex(SkillRecordError, "one line"):
                    parse_user_edited_draft(data, original=self.original)
                with self.assertRaisesRegex(SkillRecordError, "one line"):
                    parse_user_edited_draft(json.dumps(data), original=self.original)
                data["steps"][1]["action"] = "Save it as {path}" + chr(code)
                with self.assertRaisesRegex(SkillRecordError, "one line"):
                    parse_user_edited_draft(data, original=self.original)
        data = self.original.to_dict()
        data["steps"][1]["action"] = "Save it as {path}"
        self.assertEqual(
            parse_user_edited_draft(data, original=self.original).steps[1].action,
            "Save it as {path}",
        )

    def test_size_is_checked_before_encoding(self):
        huge = chr(0xD800) * (EDITED_PAYLOAD_MAX_BYTES + 1)
        with self.assertRaisesRegex(SkillRecordError, "too large"):
            parse_user_edited_draft(huge, original=self.original)
        with self.assertRaisesRegex(SkillRecordError, "not valid text"):
            parse_user_edited_draft("{" + chr(0xD800) + "}", original=self.original)
        wide = "ş" * (EDITED_PAYLOAD_MAX_BYTES // 2 + 1)
        with self.assertRaisesRegex(SkillRecordError, "too large"):
            parse_user_edited_draft(wide, original=self.original)


#: PLAN D17: every Default_Ignorable_Code_Point range (explicit list) + U+2800.
D17_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF), (0x2800, 0x2800),
)
#: Characters skill_types v3 refuses in text: both ends of every D17 range, the
#: ones the task names (CGJ, Hangul filler, bidi, zero-width, TAG, U+2800), and
#: other controls, separators, private-use, surrogate and unassigned ones.
REFUSED_CHARS = tuple(
    sorted(
        {chr(code) for low, high in D17_RANGES for code in (low, high)}
        | {
            chr(code)
            for code in (
                0x034F, 0x3164, 0x202E, 0x2066, 0x2069, 0x200B, 0x200D, 0x2060,
                0xFEFF, 0xE0001, 0xE0041, 0xE007F, 0xE0100, 0x2800, 0xFE0F,
                0x0007, 0x001B, 0x0085, 0x2028, 0x2029, 0xE000, 0xD800, 0x0378,
            )
        }
    )
)
HEART = chr(0x2764)
#: What each insertion is in the run text, and what stays of it visibly.
INSERTIONS = tuple((char, "") for char in REFUSED_CHARS) + (
    (HEART + chr(0xFE0F), HEART),
    (chr(0x1F44D) + chr(0x1F3FD) + chr(0xFE0F), chr(0x1F44D) + chr(0x1F3FD)),
    ("Re" + chr(0x034F) + chr(0x3164) + "port", "Report"),
)
#: "<>" marks where the insertion goes; inside words, values, ends.
RAW_TASK = (
    "Down<>load the Q<>3 report from https://exa<>mple.com/files/q3.pdf<>, "
    "save it as reports/q<>3.pdf and check that it opens<>"
)
RAW_STEPS = (
    ("Down<>load https://example.com/files/q3<>.pdf", "browser_download"),
    ("<>Save the re<>port as reports/q3.pdf<>", "file_write"),
    ("Verify reports/q3.pdf<> opens<>", "file_read"),
)


def raw_run(task_text, actions, capabilities, **overrides):
    """A run whose texts hold characters skill_types refuses. RunSummary v3
    refuses them too, but SKDRAFT must not rely on that (run text comes from
    tools and users), so they are put in past the record check."""
    run = make_run(
        steps=[step("placeholder", capability) for capability in capabilities],
        task_text="placeholder",
        **overrides,
    )
    object.__setattr__(run, "task_text", task_text)
    for run_step, action in zip(run.steps, actions):
        object.__setattr__(run_step, "action", action)
    return run


def draft_texts(draft):
    texts = [draft.name, draft.summary, draft.reason, *draft.checks]
    texts += [item.description for item in draft.inputs]
    texts += [skill_step.action for skill_step in draft.steps]
    return texts


class InvisibleRunTextTests(unittest.TestCase):
    """PLAN D17 on the draft side: run text may hold characters skill_types
    refuses; drafting removes them (visible text stays) and never raises."""

    def assert_clean(self, drafts):
        for draft in drafts:
            self.assertEqual(SkillDraft.from_dict(draft.to_dict()), draft)
            for text in draft_texts(draft):
                self.assertFalse(set(text) & set(REFUSED_CHARS), ascii(text))
                self.assertNotIn(chr(0xFE0F), text)

    def test_each_refused_character_is_removed_and_the_visible_text_stays(self):
        capabilities = [capability for _action, capability in RAW_STEPS]
        for insertion, visible in INSERTIONS:
            with self.subTest(insertion=ascii(insertion)):
                raw = raw_run(
                    RAW_TASK.replace("<>", insertion),
                    [action.replace("<>", insertion) for action, _cap in RAW_STEPS],
                    capabilities,
                )
                clean = make_run(
                    steps=[
                        step(action.replace("<>", visible), capability)
                        for action, capability in RAW_STEPS
                    ],
                    task_text=RAW_TASK.replace("<>", visible),
                )
                expected = build_drafts(clean, created_at=CREATED_AT)
                self.assertEqual(len(expected), 1)
                drafts = build_drafts(raw, created_at=CREATED_AT)
                self.assertEqual(drafts, expected)
                self.assert_clean(drafts)
                self.assertEqual(offer(raw), offer(clean))
                self.assertTrue(offer(raw).offer)
                self.assertEqual(task_kind(raw), task_kind(clean))
        (draft,) = build_drafts(
            raw_run(
                RAW_TASK.replace("<>", chr(0x200B)),
                [action.replace("<>", chr(0x2066)) for action, _cap in RAW_STEPS],
                capabilities,
            ),
            created_at=CREATED_AT,
        )
        self.assertEqual(
            draft.summary,
            "Download the Q3 report from {url}, save it as {path} and check that it opens",
        )
        self.assertEqual(
            [skill_step.action for skill_step in draft.steps],
            ["Download {url}", "Save the report as {path}", "Verify {path} opens"],
        )

    def test_emoji_with_vs16_keeps_the_emoji(self):
        task = "Send the " + HEART + chr(0xFE0F) + " report to " + EMAIL
        run = raw_run(
            task,
            [
                "Write the " + HEART + chr(0xFE0F) + " report",
                "Send the report to " + EMAIL + " " + HEART + chr(0xFE0F),
            ],
            ["file_write", "gmail_send"],
        )
        self.assertTrue(offer(run).offer)
        (draft,) = build_drafts(run, created_at=CREATED_AT)
        self.assertEqual(draft.name, "Send the " + HEART + " report to {email}")
        self.assertEqual(draft.steps[0].action, "Write the " + HEART + " report")
        self.assertEqual(draft.steps[1].action, "Send the report to {email} " + HEART)
        self.assert_clean((draft,))

    def test_a_record_that_still_cannot_be_built_gives_no_draft_and_no_offer(self):
        for char in (chr(0x034F), chr(0x3164), chr(0x2800), chr(0xE0041), chr(0x200B)):
            with self.subTest(char=ascii(char)):
                run = raw_run(
                    f"Download {URL} and save it",
                    [f"Download {URL}", char * 5],
                    ["browser_download", "file_write"],
                )
                self.assertEqual(build_drafts(run, created_at=CREATED_AT), ())
                decision = offer(run)
                self.assertFalse(decision.offer)
                self.assertIs(decision.reason, OfferReason.NO_DRAFT)
                # An all-invisible task text still drafts: the summary falls back.
                run = raw_run(
                    char * 3, [f"Download {URL}", "Save it as reports/q3.pdf"],
                    ["browser_download", "file_write"],
                )
                (draft,) = build_drafts(run, created_at=CREATED_AT)
                self.assertEqual(draft.summary, "Steps from a finished run")
                self.assertTrue(offer(run).offer)
                self.assert_clean((draft,))

    def test_no_record_error_escapes_and_offer_agrees_with_drafts(self):
        # Defence in depth: whatever record fails, the answer is no draft, no offer.
        run = make_run()
        with mock.patch.object(skill_draft, "_template", side_effect=SkillRecordError("x")):
            self.assertEqual(build_drafts(run, created_at=CREATED_AT), ())
            decision = offer(run)
        self.assertFalse(decision.offer)
        self.assertIs(decision.reason, OfferReason.NO_DRAFT)

    def test_random_run_text_never_raises(self):
        rng = random.Random(20261014)
        visible = (
            list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
            + [chr(c) for c in (0xE7, 0x11F, 0x131, 0x130, 0xF6, 0xFC, 0x15F, 0x1F600, 0x2764)]
            + list("     ./:@-_{}'`,;!?()[]") + [chr(34), chr(9), chr(10), chr(13)]
            + ["https://example.com/a?x=1", EMAIL, "C:/notlar/a.txt", "reports/q3.pdf",
               "2026-10-14", "12:30", "'holiday.png'", " open ", " save ", " verify "]
        )
        refused = list(REFUSED_CHARS)
        capabilities = ["file_read", "file_write", "browser_download", "gmail_send", "chat", None]

        def text(low, high):
            pieces = []
            for _ in range(rng.randint(low, high)):
                pool = refused if rng.random() < 0.35 else visible
                pieces.append(rng.choice(pool))
            return "".join(pieces)

        def strip(value):
            return "".join(char for char in value if char not in REFUSED_CHARS)

        compared = 0
        for _ in range(600):
            task = text(0, 40) or chr(0x200B)
            actions = [text(0, 30) for _step in range(rng.randint(1, 5))]
            caps = [rng.choice(capabilities) for _action in actions]
            project = rng.choice([None, "proj-a"])
            run = raw_run(task, actions, caps, project_id=project)
            drafts = build_drafts(run, created_at=CREATED_AT)
            decision = offer(run)
            self.assert_clean(drafts)
            if decision.offer:
                self.assertTrue(drafts)
            if not drafts:
                self.assertFalse(decision.offer)
            try:
                clean = make_run(
                    steps=[step(strip(a), c) for a, c in zip(actions, caps)],
                    task_text=strip(task),
                    project_id=project,
                )
            except SkillRecordError:
                continue
            compared += 1
            self.assertEqual(drafts, build_drafts(clean, created_at=CREATED_AT))
            self.assertEqual(decision, offer(clean))
        self.assertGreater(compared, 200)

    def test_edited_drafts_still_refuse_such_characters(self):
        (original,) = build_drafts(make_run(), created_at=CREATED_AT)
        places = (
            ("name", lambda d, v: d.__setitem__("name", "Report" + v + "x")),
            ("summary", lambda d, v: d.__setitem__("summary", "Down" + v + "load")),
            ("step", lambda d, v: d["steps"][0].__setitem__("action", "Download " + v + "{url}")),
            ("check", lambda d, v: d.__setitem__("checks", ["Verify" + v + " {path} opens"])),
            ("input", lambda d, v: d["inputs"][0].__setitem__("description", "A url" + v)),
        )
        for char in REFUSED_CHARS + (HEART + chr(0xFE0F),):
            for label, put in places:
                with self.subTest(char=ascii(char), field=label):
                    data = original.to_dict()
                    put(data, char)
                    with self.assertRaises(SkillRecordError):
                        parse_user_edited_draft(data, original=original)
                    if not 0xD800 <= ord(char[0]) <= 0xDFFF:
                        with self.assertRaises(SkillRecordError):
                            parse_user_edited_draft(json.dumps(data), original=original)


class PurityTests(unittest.TestCase):
    def test_module_imports_only_stdlib_and_skill_types(self):
        source = Path(skill_draft.__file__).read_text(encoding="utf-8")
        allowed = {
            "__future__", "hashlib", "json", "re", "unicodedata", "collections.abc",
            "dataclasses", "datetime", "enum", "typing", "jarvis.skill_types",
        }
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed)

    def test_module_source_is_plain_ascii(self):
        raw = Path(skill_draft.__file__).read_bytes()
        self.assertTrue(all(byte < 0x80 for byte in raw))


if __name__ == "__main__":
    unittest.main()
