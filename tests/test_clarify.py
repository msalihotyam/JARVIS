import unittest
from dataclasses import replace

from jarvis.clarify import (
    MAX_ROUNDS,
    NEEDS_INPUT,
    PLANNING,
    SLOTS,
    AcceptanceExample,
    ClarifyState,
    MidBuildQuestion,
    ProjectBrief,
    Question,
    QuestionSet,
    ReadBack,
    batch_mid_build,
    brief_hash,
    check_understood,
    empty_slots,
    next_step,
    validate_question_set,
)

FULL = ProjectBrief(
    goal="track expenses",
    users="Salih, daily",
    platform="Windows desktop, Python 3.12",
    target_location=r"C:\projects\expenses",
    definition_of_done="adds and lists expenses",
    acceptance_examples=(AcceptanceExample("adding 5 TL shows 5 TL", check="unit test"),),
    constraints="no network",
    out_of_scope="sync",
    data_sensitivity="personal finance",
    outgoing_or_money=False,
)


def confirmed(brief, shown=1, confirmed_turn=2, **kw):
    return ClarifyState(
        brief=brief,
        readback=ReadBack(brief_hash(brief), shown),
        confirmed_turn=confirmed_turn,
        **kw,
    )


class ExitCheckTests(unittest.TestCase):
    def test_full_confirmed_brief_passes(self):
        verdict = check_understood(confirmed(FULL))
        self.assertTrue(verdict.ok, verdict.reasons)
        self.assertFalse(verdict.large_forced)
        self.assertEqual(next_step(confirmed(FULL)), PLANNING)

    def test_any_empty_slot_fails(self):
        self.assertEqual(empty_slots(FULL), ())
        for slot in SLOTS:
            if slot == "definition_of_done":
                brief = replace(FULL, acceptance_examples=())
            elif slot == "outgoing_or_money":
                brief = replace(FULL, outgoing_or_money=None)
            else:
                brief = replace(FULL, **{slot: "  "})
            verdict = check_understood(confirmed(brief))
            self.assertFalse(verdict.ok, slot)
            self.assertIn(f"empty slot: {slot}", verdict.reasons)

    def test_model_claim_alone_fails(self):
        # No input exists for a model's "understood"; with nothing confirmed it fails.
        verdict = check_understood(ClarifyState(brief=FULL))
        self.assertFalse(verdict.ok)
        self.assertIn("read-back not shown", verdict.reasons)

    def test_confirmation_in_same_turn_fails(self):
        verdict = check_understood(confirmed(FULL, shown=3, confirmed_turn=3))
        self.assertFalse(verdict.ok)
        self.assertFalse(check_understood(confirmed(FULL, shown=3, confirmed_turn=2)).ok)

    def test_unconfirmed_or_stale_readback_fails(self):
        state = replace(confirmed(FULL), confirmed_turn=None)
        self.assertIn("read-back not confirmed", check_understood(state).reasons)
        stale = replace(confirmed(FULL), brief=replace(FULL, goal="other"))
        self.assertIn("brief changed after read-back", check_understood(stale).reasons)

    def test_open_question_fails(self):
        verdict = check_understood(confirmed(FULL, open_questions=("platform",)))
        self.assertFalse(verdict.ok)

    def test_unconvertible_example_fails_and_manual_forces_large(self):
        vague = replace(FULL, acceptance_examples=(AcceptanceExample("works well"),))
        self.assertIn("definition_of_done", empty_slots(vague))
        manual = replace(
            FULL, acceptance_examples=(AcceptanceExample("looks right", manual=True),)
        )
        verdict = check_understood(confirmed(manual))
        self.assertTrue(verdict.ok, verdict.reasons)
        self.assertTrue(verdict.large_forced)


class RoundTests(unittest.TestCase):
    def test_round_four_goes_to_needs_input(self):
        partial = replace(FULL, goal="")
        self.assertEqual(MAX_ROUNDS, 3)
        self.assertEqual(next_step(ClarifyState(partial, rounds_asked=2)), "clarifying")
        self.assertEqual(next_step(ClarifyState(partial, rounds_asked=3)), NEEDS_INPUT)
        with self.assertRaises(ValueError):
            validate_question_set(QuestionSet(4, (Question("goal", "Goal?"),)), partial)

    def test_question_set_numbered_and_follow_ups_only_empty_slots(self):
        partial = replace(FULL, goal="", platform="")
        qs = QuestionSet(1, (Question("goal", "Goal?"), Question("users", "Who?")))
        validate_question_set(qs, partial)
        self.assertEqual(qs.render(), "1. Goal?\n2. Who?")
        with self.assertRaises(ValueError):
            validate_question_set(QuestionSet(2, (Question("users", "Who?"),)), partial)
        validate_question_set(QuestionSet(2, (Question("platform", "Where?"),)), partial)
        for bad in (QuestionSet(1, ()), QuestionSet(1, (Question("nope", "x"),)),
                    QuestionSet(1, (Question("goal", " "),))):
            with self.assertRaises(ValueError):
                validate_question_set(bad, partial)


class MidBuildTests(unittest.TestCase):
    def q(self, **kw):
        base = dict(decision="Store as SQLite or JSON?", options=("SQLite", "JSON"),
                    default="SQLite", blocks=("S2",), changes_result=True)
        base.update(kw)
        return MidBuildQuestion(**base)

    def test_valid_only_for_result_changing_decision(self):
        self.q()
        for bad in (dict(changes_result=False), dict(decision=" "),
                    dict(options=("SQLite",)), dict(options=("A", "A")),
                    dict(default="CSV"), dict(blocks=())):
            with self.assertRaises(ValueError, msg=bad):
                self.q(**bad)

    def test_batching_merges_and_blocks_only_dependents(self):
        batch = batch_mid_build(
            [self.q(), self.q(blocks=("S3",)),
             self.q(decision="Dark theme?", options=("yes", "no"), default="no",
                    blocks=("S4",))],
            ["S1", "S2", "S3", "S4", "S5"],
        )
        self.assertEqual(len(batch.questions), 2)
        self.assertEqual(batch.blocked, frozenset({"S2", "S3", "S4"}))
        self.assertEqual(batch.runnable, ("S1", "S5"))
        self.assertTrue(batch.message.startswith("1. Store as SQLite or JSON?"))
        self.assertIn("\n2. Dark theme?", batch.message)

    def test_batching_fails_closed(self):
        with self.assertRaises(ValueError):
            batch_mid_build([self.q(), self.q(default="JSON")], ["S2"])
        with self.assertRaises(ValueError):
            batch_mid_build([self.q()], ["S1"])
        with self.assertRaises(ValueError):
            batch_mid_build([], ["S1"])


if __name__ == "__main__":
    unittest.main()
