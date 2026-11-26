"""FONTS1: the bundled Inter and JetBrains Mono files (SIL OFL 1.1) are exactly the pinned release bytes."""
from __future__ import annotations

import hashlib
import unittest

from jarvis.face.tokens import FONT_DIR, FONT_FAMILIES, font_plan

# rsms/inter v4.1, JetBrains/JetBrainsMono v2.304 release archives.
PINNED_TTF = {
    "Inter-Regular.ttf": "40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82",
    "Inter-Medium.ttf": "97ad806f526e41546d46365bb3a393145f75b7b1568913db74549ad8b8dba872",
    "Inter-SemiBold.ttf": "78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3",
    "JetBrainsMono-Regular.ttf": "a0bf60ef0f83c5ed4d7a75d45838548b1f6873372dfac88f71804491898d138f",
    "JetBrainsMono-Medium.ttf": "31c92d01a8a08528b718a43addf0ad3df0af2ca4b7b3290a452f70f358e14d3d",
    "JetBrainsMono-SemiBold.ttf": "1b3bfa1ed5665a4ce3f9feb68d2d4e40e70bf8b4b7d9a3edd418f321b4e166a0",
}
LICENSES = ("Inter-OFL.txt", "JetBrainsMono-OFL.txt")


class BundledFontTests(unittest.TestCase):
    def test_every_family_file_matches_its_pinned_hash(self):
        names = [f for spec in FONT_FAMILIES.values() for f in spec["files"]]
        self.assertEqual(sorted(names), sorted(PINNED_TTF))
        for name in names:
            with self.subTest(name=name):
                path = FONT_DIR / name
                self.assertTrue(path.is_file())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), PINNED_TTF[name])

    def test_both_licenses_are_ofl_1_1(self):
        # Text, not a hash: a CRLF checkout must not fail the license check.
        for name in LICENSES:
            with self.subTest(name=name):
                self.assertIn(b"SIL Open Font License, Version 1.1", (FONT_DIR / name).read_bytes())

    def test_font_plan_picks_the_bundled_files(self):
        plan = font_plan(list(FONT_DIR.iterdir()))
        self.assertEqual(set(plan), set(FONT_FAMILIES))
        for key, spec in FONT_FAMILIES.items():
            with self.subTest(family=key):
                self.assertEqual(plan[key]["chosen"], "bundled")
                self.assertEqual(plan[key]["reason"], "")
                self.assertEqual(plan[key]["families"], (spec["family"],))
                self.assertEqual(plan[key]["files"], tuple(str(FONT_DIR / f) for f in spec["files"]))

    def test_font_dir_holds_nothing_else(self):
        self.assertEqual({p.name for p in FONT_DIR.iterdir()}, set(PINNED_TTF) | set(LICENSES))


if __name__ == "__main__":
    unittest.main()
