import unittest

from jarvis.work_lessons import (
    FailedSlice,
    FailureKind,
    FailureSignal,
    GuardKind,
    LessonError,
    ReviewFinding,
    SalihCorrection,
    SliceOutcome,
    learn,
    planner_lessons,
)


class LessonTests(unittest.TestCase):
    def test_new_class_proposes_one_self_dev_slice(self):
        result = learn((
            FailedSlice(SliceOutcome.FAILED, FailureSignal.TEST_FAILURE, "receipt-replay"),
            ReviewFinding(FailureSignal.MISSING_TEST, "receipt-replay"),
        ))
        self.assertEqual(len(result.classes), 2)
        self.assertEqual(result.classes[0], result.classes[1])
        self.assertEqual(result.classes[0].key, "regression:receipt-replay")
        self.assertEqual(len(result.proposals), 1)
        guard = result.proposals[0]
        self.assertEqual(guard.kind, GuardKind.TEST)
        self.assertEqual(guard.slice.targets, (guard.target,))
        self.assertEqual(guard.slice.goal, guard.why)
        self.assertEqual(guard.slice.check_ids, ())
        self.assertFalse(guard.needs_salih)

    def test_known_class_is_available_to_planner_and_not_reproposed(self):
        first = learn((ReviewFinding(FailureSignal.MISSING_TEST, "receipt-replay"),))
        repeat = learn((
            FailedSlice(SliceOutcome.BLOCKED, FailureSignal.TEST_FAILURE, "receipt-replay"),
            SalihCorrection(FailureSignal.MISSING_TEST, "stale-receipt"),
        ), known=first.classes)
        self.assertEqual(repeat.classes[0], first.classes[0])
        self.assertEqual(len(repeat.proposals), 1)
        self.assertEqual(repeat.proposals[0].failure.key, "regression:stale-receipt")
        self.assertEqual(planner_lessons(first.classes * 2), first.proposals)

    def test_policy_and_authority_require_salih_even_for_corrections(self):
        result = learn((
            SalihCorrection(FailureSignal.POLICY, "protected-target"),
            ReviewFinding(FailureSignal.AUTHORITY, "approval-binding"),
        ))
        self.assertEqual({guard.failure.kind for guard in result.proposals},
                         {FailureKind.POLICY, FailureKind.AUTHORITY})
        for guard in result.proposals:
            self.assertEqual(guard.kind, GuardKind.VALIDATOR_RULE)
            self.assertTrue(guard.needs_salih)

    def test_malformed_input_and_known_records_are_refused(self):
        valid = ReviewFinding(FailureSignal.STATIC_DEFECT, "unused-import")
        for invalid in (
            {},
            FailedSlice("passed", FailureSignal.TEST_FAILURE, "receipt"),
            ReviewFinding("missing_test", "receipt"),
            SalihCorrection(FailureSignal.POLICY, "../approval"),
            SalihCorrection(FailureSignal.POLICY, "policy\n"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(LessonError):
                learn((valid, invalid))
        with self.assertRaises(LessonError):
            learn((valid,), known=("static:unused-import",))
        with self.assertRaises(LessonError):
            planner_lessons(None)

    def test_proposals_are_deterministic_and_sorted_by_key(self):
        observations = (
            ReviewFinding(FailureSignal.STATIC_DEFECT, "unused-import"),
            FailedSlice(SliceOutcome.BLOCKED, FailureSignal.PLAN_GAP, "missing-check"),
            SalihCorrection(FailureSignal.PLAN_GAP, "missing-check"),
        )
        result = learn(observations)
        self.assertEqual(result, learn(observations))
        self.assertEqual(result.proposals, learn(observations[::-1]).proposals)
        self.assertEqual(tuple(guard.kind for guard in result.proposals),
                         (GuardKind.PLANNER_INSTRUCTION, GuardKind.LINT_RULE))
