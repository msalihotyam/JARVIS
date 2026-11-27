"""FACEGEO1: window geometry for the face's three fixed states (ring, reader, full).

Pure: screen facts in, one rect out. Work areas come in device pixels as Windows reports them
(primary screen first); the result is whole logical pixels kept inside the work area, so Windows
never has to move or resize the window. The saved ring position is in logical pixels.
"""

import math
from fractions import Fraction
from typing import NamedTuple

STATES = ("ring", "reader", "full")
RING = 72
MARGIN = 24
READER_WIDTH = 880
READER_MIN = (360, 320)  # logical width, height; below this the reader takes the whole work area
SCALE_RANGE = (1, 5)  # Windows display scale 100 % .. 500 %
COORD_LIMIT = 2**31  # Windows screen coordinates are 32-bit


class GeometryRefused(ValueError):
    """Unknown state, or a work area / scale that is not a real screen."""


class Rect(NamedTuple):
    x: int
    y: int
    width: int
    height: int


def rect(state: object, work_area: object, scale: object, saved_ring_pos: object = None) -> Rect:
    """Window rect for state on the screen that holds the ring (primary when none does)."""
    if not isinstance(state, str) or state not in STATES:
        raise GeometryRefused(f"unknown state: {state!r:.40}")
    s = _number(scale, "scale")
    if not SCALE_RANGE[0] <= s <= SCALE_RANGE[1]:
        raise GeometryRefused("scale outside 1..5")
    screens = [_logical(area, s) for area in _areas(work_area)]
    screen, ring_x, ring_y = _ring(screens, saved_ring_pos)
    left, top, right, bottom = screen
    width, height = right - left, bottom - top
    if state == "ring":
        return Rect(ring_x, ring_y, RING, RING)
    if state == "full" or width < READER_MIN[0] or height - 2 * MARGIN < READER_MIN[1]:
        return Rect(left, top, width, height)
    if width >= READER_WIDTH:
        side = min(MARGIN, width - READER_WIDTH)  # right margin shrinks so an 880 reader still fits
        return Rect(right - side - READER_WIDTH, top + MARGIN, READER_WIDTH, height - 2 * MARGIN)
    return Rect(left, top + MARGIN, width, height - 2 * MARGIN)


def _number(value: object, what: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryRefused(f"{what} is not a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise GeometryRefused(f"{what} is not finite")
    return Fraction(value)


def _areas(work_area: object) -> list:
    if _is_seq(work_area) and len(work_area) == 4 and not any(_is_seq(part) for part in work_area):
        return [work_area]  # one screen given bare
    if not _is_seq(work_area) or not work_area:
        raise GeometryRefused("work area must be a rect or a non-empty list of rects")
    return list(work_area)


def _is_seq(value: object) -> bool:
    return isinstance(value, (list, tuple))


def _logical(area: object, s: Fraction) -> tuple:
    """Device-pixel work area -> logical (left, top, right, bottom), rounded inward so it never overflows."""
    if not _is_seq(area) or len(area) != 4:
        raise GeometryRefused("a work area is (x, y, width, height)")
    x, y, w, h = (_number(part, "work area") for part in area)
    if w <= 0 or h <= 0:
        raise GeometryRefused("work area width and height must be positive")
    if any(abs(edge) > COORD_LIMIT for edge in (x, y, x + w, y + h)):
        raise GeometryRefused("work area outside the 32-bit desktop")
    left, top = math.ceil(x / s), math.ceil(y / s)
    right, bottom = math.floor((x + w) / s), math.floor((y + h) / s)
    if right - left < RING or bottom - top < RING:
        raise GeometryRefused("work area smaller than the ring")
    return left, top, right, bottom


def _ring(screens: list, saved: object) -> tuple:
    """(screen, x, y): the screen holding the saved ring's centre, else the primary's bottom-right corner."""
    pos = _saved(saved)
    if pos is not None:
        cx, cy = pos[0] + RING / 2, pos[1] + RING / 2
        for screen in screens:
            left, top, right, bottom = screen
            if left <= cx < right and top <= cy < bottom:
                return screen, _clamp(pos[0], left, right - RING), _clamp(pos[1], top, bottom - RING)
    screen = screens[0]
    left, top, right, bottom = screen
    return screen, max(left, right - MARGIN - RING), max(top, bottom - MARGIN - RING)


def _saved(saved: object):
    """A stored position that is not two finite in-range numbers counts as absent (default corner)."""
    if not _is_seq(saved) or len(saved) != 2:
        return None
    try:
        x, y = (_number(part, "saved ring position") for part in saved)
    except GeometryRefused:
        return None
    if abs(x) > COORD_LIMIT or abs(y) > COORD_LIMIT:
        return None
    return math.floor(x), math.floor(y)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))
