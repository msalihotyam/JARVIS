"""FACEGEO1: window geometry for the face's three states (jarvis/face/geometry.py). Pure; no Qt, file or network."""

import unittest

from jarvis.face import geometry as g
from tests.adversarial import HOSTILE_NUMBERS, each

SCALES = (1, 1.25, 1.5, 1.75, 2)
HD = (0, 0, 1920, 1040)  # 1080p minus a 40 px taskbar, device pixels
LEFT_4K = (-3840, 0, 3840, 2120)  # secondary screen left of the primary


def inside(r, area, s):
    """r (logical) lies inside area (device pixels) at scale s."""
    x, y, w, h = area
    return x <= r.x * s and y <= r.y * s and (r.x + r.width) * s <= x + w and (r.y + r.height) * s <= y + h


class AcceptanceTests(unittest.TestCase):
    def test_1_whole_logical_pixels(self):
        for state in g.STATES:
            for s in SCALES:
                r = g.rect(state, HD, s)
                self.assertEqual(len(r), 4)
                self.assertTrue(all(type(v) is int for v in r), (state, s, r))

    def test_2_ring_default_bottom_right_and_saved(self):
        self.assertEqual(g.rect("ring", HD, 1), (1920 - 24 - 72, 1040 - 24 - 72, 72, 72))
        self.assertEqual(g.rect("ring", HD, 1, (100, 200)), (100, 200, 72, 72))
        self.assertEqual(g.rect("ring", HD, 1, (1870, 1000)), (1920 - 72, 1040 - 72, 72, 72))  # centre on, edge off: clamped

    def test_3_reader_right_aligned_and_narrow_screen(self):
        self.assertEqual(g.rect("reader", HD, 1), (1920 - 24 - 880, 24, 880, 1040 - 48))
        self.assertEqual(g.rect("reader", (0, 0, 900, 1040), 1), (900 - 20 - 880, 24, 880, 1040 - 48))
        self.assertEqual(g.rect("reader", HD, 2), (960 - 24 - 880, 24, 880, 520 - 48))
        self.assertEqual(g.rect("reader", HD, 2.5), (0, 24, 768, 416 - 48))  # 768 < 880: full work width

    def test_3b_reader_880_wide_from_880_up_margin_shrinks(self):
        # FACEGEO1FIX: width -> reader x (logical); 880 wide from 880 up, right margin min(24, width - 880)
        for width, x in ((880, 0), (881, 0), (900, 0), (927, 23), (928, 24), (1000, 96)):
            with self.subTest(width=width):
                self.assertEqual(g.rect("reader", (0, 0, width, 1040), 1), (x, 24, 880, 1040 - 48))
        self.assertEqual(g.rect("reader", (0, 0, 879, 1040), 1), (0, 24, 879, 1040 - 48))  # below 880: full width
        self.assertEqual(g.rect("reader", (0, 0, 1366, 728), 1.5), (910 - 24 - 880, 24, 880, 485 - 48))  # 910 logical
        self.assertEqual(g.rect("reader", [HD, (-900, 0, 900, 1040)], 1, (-500, 500)), (-900, 24, 880, 1040 - 48))
        for width in range(1300, 1420, 3):  # device px; logical band ~743..1136 across scales, origin off-zero
            for s in (1.25, 1.5, 1.75):
                area = (-width * 2 + 1, 7, width, 1040)
                with self.subTest(width=width, s=s):
                    r, full = g.rect("reader", area, s), g.rect("full", area, s)
                    self.assertTrue(inside(r, area, s), r)
                    self.assertEqual(r.width, min(880, full.width))
                    gap = full.x + full.width - (r.x + r.width)  # right margin
                    self.assertEqual(gap, min(24, full.width - 880) if full.width >= 880 else 0)

    def test_4_full_is_the_work_area(self):
        self.assertEqual(g.rect("full", HD, 1), (0, 0, 1920, 1040))
        self.assertEqual(g.rect("full", (0, 0, 1920, 1040), 1.25), (0, 0, 1536, 832))

    def test_5_minimums_and_inside_at_every_scale(self):
        for area in (HD, LEFT_4K, (0, 0, 1366, 728), (7, 3, 1281, 1017)):
            for s in SCALES:
                for state in g.STATES:
                    r = g.rect(state, area, s, (10**6, 10**6))
                    with self.subTest(area=area, s=s, state=state):
                        self.assertTrue(inside(r, area, s), r)
                        self.assertGreaterEqual(r.width, g.READER_MIN[0] if state != "ring" else g.RING)
                        self.assertGreaterEqual(r.height, g.READER_MIN[1] if state != "ring" else g.RING)

    def test_6_ring_screen_used_removed_screen_falls_back_to_primary(self):
        screens = [HD, LEFT_4K]
        on_left = (-1000, 500)
        self.assertEqual(g.rect("ring", screens, 1, on_left), (-1000, 500, 72, 72))
        self.assertEqual(g.rect("reader", screens, 1, on_left), (-24 - 880, 24, 880, 2120 - 48))
        self.assertEqual(g.rect("full", screens, 1, on_left), (-3840, 0, 3840, 2120))
        self.assertEqual(g.rect("full", [HD], 1, on_left), (0, 0, 1920, 1040))  # left screen unplugged
        self.assertEqual(g.rect("ring", [HD], 1, on_left), (1824, 944, 72, 72))


class NegativeTests(unittest.TestCase):
    def refused(self, *args):
        with self.assertRaises(g.GeometryRefused):
            g.rect(*args)

    def test_1_bad_work_area_or_scale_refused(self):
        for area in ((0, 0, 0, 1040), (0, 0, 1920, -1), (0, 0, float("nan"), 1040), (float("nan"), 0, 1920, 1040),
                     (), [], (0, 0, 1920), "0,0,1920,1040", None, [HD, (0, 0, 0, 0)], (0, 0, 50, 50)):
            with self.subTest(area=area):
                self.refused("full", area, 1)
        for s in (0, -1, float("nan"), 0.5, 6):
            with self.subTest(scale=s):
                self.refused("full", HD, s)
        each(self, HOSTILE_NUMBERS, lambda v: self.refused("full", HD, v))
        each(self, HOSTILE_NUMBERS, lambda v: self.refused("full", (0, 0, v, 1040), 1))
        self.assertTrue(issubclass(g.GeometryRefused, ValueError))

    def test_2_saved_position_off_every_screen_gives_default_corner(self):
        default = g.rect("ring", HD, 1)
        for pos in ((-5000, -5000), (10**6, 0), (5000, 5000), (float("nan"), 0), (True, 1), ("1", "2"), (1,), None,
                    (10**400, 0), "100,200"):
            with self.subTest(pos=pos):
                self.assertEqual(g.rect("ring", HD, 1, pos), default)

    def test_3_tiny_work_area_reader_is_full_never_negative(self):
        for area in ((0, 0, 300, 1040), (0, 0, 1920, 300), (0, 0, 72, 72), (0, 0, 100, 80)):
            for s in (1,):
                with self.subTest(area=area):
                    self.assertEqual(g.rect("reader", area, s), g.rect("full", area, s))
                    ring = g.rect("ring", area, s)
                    self.assertTrue(min(ring) >= 0 and inside(ring, area, s), ring)

    def test_4_fractional_scale_no_overflow(self):
        for area in ((0, 0, 1921, 1041), (1, 1, 1919, 1039), (-3841, -7, 3843, 2161), (3, 5, 2559, 1399)):
            for s in (1.25, 1.75):
                for state in g.STATES:
                    with self.subTest(area=area, s=s, state=state):
                        self.assertTrue(inside(g.rect(state, area, s), area, s))

    def test_5_unknown_state_refused(self):
        for state in ("", "Ring", "mini", None, 1, b"ring", ("ring",)):
            with self.subTest(state=state):
                self.refused(state, HD, 1)

    def test_6_huge_and_negative_origins(self):
        far = (2**31 - 4000, -(2**31), 3840, 2160)
        for s in SCALES:
            for state in g.STATES:
                with self.subTest(s=s, state=state):
                    self.assertTrue(inside(g.rect(state, far, s), far, s))
                    self.assertTrue(inside(g.rect(state, LEFT_4K, s), LEFT_4K, s))
        self.refused("full", (2**31, 0, 1920, 1040), 1)
        self.refused("full", (10**400, 0, 1920, 1040), 1)
