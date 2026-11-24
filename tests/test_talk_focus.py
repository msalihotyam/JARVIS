import unittest

from jarvis.talk_focus import Action, Focus, FocusState, Intent, Proposal, decide


class TalkFocusTests(unittest.TestCase):
    def setUp(self):
        self.focus = Focus("Atlas", "planning")
        self.projects = ("Atlas", "Beacon")

    def decide(self, intent, targets=("Beacon",), **kwargs):
        return decide(self.focus, Proposal(intent, "review", targets, **kwargs), self.projects)

    def test_focus_can_have_no_project(self):
        focus = Focus(None, "general")
        self.assertIsNone(focus.project)
        self.assertEqual(focus.topic, "general")

    def test_explicit_target_switches_with_one_word_notice(self):
        result = self.decide(Intent.WRITE, explicit=True)
        self.assertEqual(result.action, Action.SWITCH)
        self.assertEqual(result.focus, Focus("Beacon", "review"))
        self.assertEqual(len(result.notice.split()), 1)
        self.assertIsNone(result.question)

    def test_guessed_read_or_chat_switches_with_notice(self):
        for intent in (Intent.READ, Intent.CHAT):
            with self.subTest(intent=intent):
                result = self.decide(intent)
                self.assertEqual(result.action, Action.SWITCH)
                self.assertEqual(result.focus, Focus("Beacon", "review"))
                self.assertTrue(result.notice)

    def test_guessed_write_or_action_asks_without_switching(self):
        state = FocusState(self.focus)
        for intent in (Intent.WRITE, Intent.ACTION):
            with self.subTest(intent=intent):
                result = state.apply(Proposal(intent, "review", ("Beacon",)), self.projects)
                self.assert_asks(result)
                self.assertEqual(result.question, "Switch to Beacon?")
                self.assertIs(state.focus, self.focus)

    def test_topic_change_is_silent(self):
        for targets, explicit in ((None, False), (("Atlas",), False), (("Atlas",), True)):
            with self.subTest(targets=targets, explicit=explicit):
                result = self.decide(Intent.WRITE, targets, explicit=explicit)
                self.assertEqual(result.action, Action.SWITCH)
                self.assertEqual(result.focus, Focus("Atlas", "review"))
                self.assertIsNone(result.notice)
                self.assertIsNone(result.question)

    def test_start_project_opens_listed_project_and_moves_focus(self):
        result = self.decide(Intent.START_PROJECT)
        self.assertEqual(result.action, Action.OPEN_PROJECT)
        self.assertEqual(result.focus, Focus("Beacon", "review"))

    def test_both_views_share_one_focus_value(self):
        state = FocusState(self.focus)
        project_view = state
        chat_view = state
        result = chat_view.apply(Proposal(Intent.CHAT, "review", ("Beacon",)), self.projects)
        self.assertEqual(project_view.focus, Focus("Beacon", "review"))
        self.assertIs(project_view.focus, result.focus)

    def test_job_report_uses_original_scope_and_preserves_focus(self):
        state = FocusState(self.focus)
        job = state.start_job()
        state.apply(Proposal(Intent.READ, "review", ("Beacon",)), self.projects)
        self.assertEqual(job.report_scope(), Focus("Atlas", "planning"))
        self.assertEqual(state.focus, Focus("Beacon", "review"))

    def test_unlisted_target_asks_even_when_explicit_or_starting(self):
        for intent in (Intent.READ, Intent.START_PROJECT):
            with self.subTest(intent=intent):
                self.assert_asks(self.decide(intent, ("Unknown",), explicit=True))

    def test_ambiguous_or_missing_target_asks(self):
        for targets in (("Atlas", "Beacon"), ("Beacon", "Unknown"), ()):
            with self.subTest(targets=targets):
                self.assert_asks(self.decide(Intent.READ, targets, explicit=True))
        self.assert_asks(self.decide(Intent.START_PROJECT, None))

    def test_confidence_does_not_affect_decision(self):
        for intent in (Intent.READ, Intent.WRITE):
            with self.subTest(intent=intent):
                self.assertEqual(
                    self.decide(intent, confidence=0.0),
                    self.decide(intent, confidence=1.0),
                )

    def test_unknown_action_type_raises(self):
        with self.assertRaises(ValueError):
            self.decide("unknown", None)

    def assert_asks(self, result):
        self.assertEqual(result.action, Action.ASK)
        self.assertEqual(result.focus, self.focus)
        self.assertIsNone(result.notice)
        self.assertTrue(result.question.endswith("?"))
        self.assertEqual(result.question.count("?"), 1)
