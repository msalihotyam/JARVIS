import unittest

from jarvis.context_usage import ContextUsage, context_usage
from jarvis.conversation import ESTIMATOR, RAW_TURNS_MAX, ContextProfile

PROFILE = ContextProfile(context_window_tokens=10_000, output_reserve_tokens=2_000)


class ContextUsageTests(unittest.TestCase):
    def test_zero_turns_is_empty_and_full_runway(self):
        usage = context_usage(0, 0, PROFILE)
        self.assertEqual(usage, ContextUsage(0, 8_000, 0.0, RAW_TURNS_MAX, "normal"))

    def test_turns_until_autocompact_boundaries(self):
        self.assertEqual(context_usage(RAW_TURNS_MAX - 1, 0, PROFILE).turns_until_autocompact, 1)
        self.assertEqual(context_usage(RAW_TURNS_MAX, 0, PROFILE).turns_until_autocompact, 0)
        self.assertEqual(context_usage(RAW_TURNS_MAX + 5, 0, PROFILE).turns_until_autocompact, 0)

    def test_output_reserve_is_subtracted(self):
        usage = context_usage(3, 0, PROFILE, assembled_tokens=4_000)
        self.assertEqual(usage.max_tokens, 8_000)
        self.assertEqual(usage.ratio, 0.5)

    def test_chars_use_the_shared_estimator(self):
        chars = 3_000
        usage = context_usage(1, chars, PROFILE)
        self.assertEqual(usage.used_tokens, ESTIMATOR.tokens("x" * chars))

    def test_exact_tokens_win_over_chars(self):
        self.assertEqual(context_usage(1, 999_999, PROFILE, assembled_tokens=10).used_tokens, 10)

    def test_labels_and_ratio_clamped(self):
        self.assertEqual(context_usage(1, 0, PROFILE, assembled_tokens=6_400).label_key, "yuksek")
        full = context_usage(1, 0, PROFILE, assembled_tokens=50_000)
        self.assertEqual((full.ratio, full.label_key), (1.0, "dolu"))

    def test_unknown_profile_is_honest(self):
        for profile in (None, ContextProfile(context_window_tokens=0),
                        ContextProfile(context_window_tokens=1_000, output_reserve_tokens=1_000)):
            usage = context_usage(4, 400, profile)
            self.assertIsNone(usage.max_tokens)
            self.assertIsNone(usage.ratio)
            self.assertEqual(usage.label_key, "bilinmiyor")
            self.assertEqual(usage.turns_until_autocompact, RAW_TURNS_MAX - 4)
            self.assertGreater(usage.used_tokens, 0)


if __name__ == "__main__":
    unittest.main()
