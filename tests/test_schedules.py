"""Pure deterministic schedule arithmetic (Scheduler phase 1A).

Every nominal instant below is asserted as an EXACT UTC datetime, because a
schedule that is approximately right is a schedule that fires at the wrong
time twice a year.

What these tests are actually trying to break:

DST GAP   a local time a spring-forward deleted must produce NO occurrence
          that day, never a silently shifted one.
DST FOLD  a local time a fall-back repeated must produce exactly ONE
          occurrence, the earlier instant; the later one must never appear
          from any recurrence, from either direction of search.
CLOCK     the answers are a pure function of (schedule, instant). A clock
          that rolls back re-derives the same nominal instants, and a clock
          that jumps five years forward is answered in the same BOUNDED work
          as one that jumped five minutes - proven by counting the calendar
          probes, not by timing them.
CLOSED    an unresolvable timezone, a naive datetime, a sub-minimum interval
          and an empty weekday set all fail closed at construction.

Nothing here starts a process, opens a socket, sleeps or reads the machine's
own clock or timezone.

TIMEZONE DATABASE. These tests need real DST rules, and the deliberate
absence of a dependency in this phase means a bare Windows CPython has no
``tzdata`` at all to resolve ``America/New_York`` with. So when - and only
when - the interpreter cannot resolve the named zones itself, this module
writes two TZif files with the published US and EU rules and points
``zoneinfo`` at them for the duration of the run. Where a real database IS
present it is used and this fallback never runs, so an error in the
handwritten rules below shows up as a failure against the real one rather
than hiding behind it. Production code is untouched either way: it asks
``zoneinfo`` for a named zone, exactly as it will in deployment.
"""

from __future__ import annotations

import ast
import calendar
import struct
import tempfile
import unittest
import zoneinfo
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis import schedules
from jarvis.schedules import (
    MIN_INTERVAL,
    Daily,
    Interval,
    OneTime,
    ScheduleError,
    ScheduleKind,
    Weekday,
    Weekly,
    latest_occurrence,
    next_occurrence,
    resolve_local_instant,
)


NEW_YORK = "America/New_York"
BERLIN = "Europe/Berlin"

_TZ_FALLBACK: tempfile.TemporaryDirectory | None = None


def utc(text: str) -> datetime:
    """An exact UTC instant from an ISO string, for readable expectations."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _nth_sunday(year: int, month: int, index: int) -> date:
    """The ``index``-th Sunday of a month (``-1`` for the last)."""
    sundays = [
        date(year, month, day)
        for day in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, day).weekday() == Weekday.SUNDAY
    ]
    return sundays[index]


def _tzif(
    std_offset: int, dst_offset: int, std: str, dst: str, rules: str
) -> bytes:
    """A TZif v1 file for a two-offset zone with the published DST rules.

    US: DST from the 2nd Sunday in March at 02:00 standard time to the 1st
    Sunday in November at 02:00 daylight time. EU: from the last Sunday in
    March to the last Sunday in October, both at 01:00 UTC.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def at_utc(day: date, hour: int) -> int:
        moment = datetime.combine(day, time(hour)).replace(tzinfo=timezone.utc)
        return int((moment - epoch).total_seconds())

    transitions: list[tuple[int, int]] = []
    for year in range(2000, 2038):  # 32-bit transition times end in 2038.
        if rules == "us":
            transitions.append((at_utc(_nth_sunday(year, 3, 1), 2) - std_offset, 1))
            transitions.append((at_utc(_nth_sunday(year, 11, 0), 2) - dst_offset, 0))
        else:
            transitions.append((at_utc(_nth_sunday(year, 3, -1), 1), 1))
            transitions.append((at_utc(_nth_sunday(year, 10, -1), 1), 0))
    transitions.sort()
    names = f"{std}\0{dst}\0".encode()
    return b"".join(
        (
            struct.pack(
                ">4sc15x6L", b"TZif", b"\x00", 0, 0, 0, len(transitions), 2, len(names)
            ),
            b"".join(struct.pack(">l", moment) for moment, _ in transitions),
            bytes(index for _, index in transitions),
            struct.pack(">lBB", std_offset, 0, 0),
            struct.pack(">lBB", dst_offset, 1, len(std) + 1),
            names,
        )
    )


def setUpModule() -> None:
    global _TZ_FALLBACK
    try:
        ZoneInfo(NEW_YORK)
        ZoneInfo(BERLIN)
        return
    except ZoneInfoNotFoundError:
        pass
    _TZ_FALLBACK = tempfile.TemporaryDirectory()
    root = Path(_TZ_FALLBACK.name)
    for key, payload in (
        (NEW_YORK, _tzif(-18000, -14400, "EST", "EDT", "us")),
        (BERLIN, _tzif(3600, 7200, "CET", "CEST", "eu")),
    ):
        target = root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    zoneinfo.reset_tzpath([str(root)])
    ZoneInfo.clear_cache()


def tearDownModule() -> None:
    global _TZ_FALLBACK
    if _TZ_FALLBACK is not None:
        zoneinfo.reset_tzpath()
        ZoneInfo.clear_cache()
        _TZ_FALLBACK.cleanup()
        _TZ_FALLBACK = None


class TimezoneDatabaseTest(unittest.TestCase):
    """The zones the DST proofs below rest on really do transition."""

    def test_zones_carry_the_expected_transitions(self) -> None:
        new_york = ZoneInfo(NEW_YORK)
        berlin = ZoneInfo(BERLIN)
        self.assertEqual(
            datetime(2026, 1, 4, 9, tzinfo=new_york).utcoffset(), timedelta(hours=-5)
        )
        self.assertEqual(
            datetime(2026, 7, 4, 9, tzinfo=new_york).utcoffset(), timedelta(hours=-4)
        )
        self.assertEqual(
            datetime(2026, 1, 4, 9, tzinfo=berlin).utcoffset(), timedelta(hours=1)
        )
        self.assertEqual(
            datetime(2026, 7, 4, 9, tzinfo=berlin).utcoffset(), timedelta(hours=2)
        )


class ScheduleModelTest(unittest.TestCase):
    """Exactly four approved kinds, and no general recurrence machinery."""

    def test_only_the_approved_kinds_exist(self) -> None:
        self.assertEqual(
            {kind.value for kind in ScheduleKind},
            {"one_time", "interval", "daily", "weekly"},
        )
        self.assertIs(OneTime.kind, ScheduleKind.ONE_TIME)
        self.assertIs(Interval.kind, ScheduleKind.INTERVAL)
        self.assertIs(Daily.kind, ScheduleKind.DAILY)
        self.assertIs(Weekly.kind, ScheduleKind.WEEKLY)

    def test_no_machine_clock_or_timezone_is_ever_read(self) -> None:
        """The kernel cannot read the machine's clock or timezone at all.

        Asserted against the module's own syntax tree, because the property
        is an ABSENCE: no call site can prove that a hidden
        ``datetime.now()`` is not sitting behind some branch, but its
        non-existence can be read off the code. The tree, not the text, so
        the prose documenting the rule cannot satisfy or break it.
        """
        tree = ast.parse(Path(schedules.__file__).read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        called = {ast.unparse(call.func) for call in calls}
        self.assertEqual(
            called
            & {
                "datetime.now",
                "datetime.utcnow",
                "datetime.today",
                "datetime.fromtimestamp",
                "date.today",
                "time.time",
                "time.sleep",
                "time.localtime",
            },
            set(),
        )
        # ``astimezone()`` with no argument is exactly the machine-local read.
        self.assertEqual(
            [
                ast.unparse(call)
                for call in calls
                if ast.unparse(call.func).endswith("astimezone") and not call.args
            ],
            [],
        )

    def test_schedules_are_frozen(self) -> None:
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        with self.assertRaises(Exception):
            schedule.at = time(10, 0)  # type: ignore[misc]


class ValidationTest(unittest.TestCase):
    """Fail closed at construction: an invalid schedule cannot exist."""

    def test_timezone_must_be_a_resolvable_iana_identifier(self) -> None:
        for bad in ("", "Not/AZone", "local", "EST5EDT4", None, 42, "../etc/passwd"):
            with self.subTest(timezone=bad):
                with self.assertRaises(ScheduleError):
                    Daily(timezone=bad, at=time(9, 0))  # type: ignore[arg-type]
                with self.assertRaises(ScheduleError):
                    Weekly(
                        timezone=bad,  # type: ignore[arg-type]
                        at=time(9, 0),
                        days=frozenset({Weekday.MONDAY}),
                    )

    def test_naive_and_non_datetime_instants_are_refused(self) -> None:
        with self.assertRaises(ScheduleError):
            OneTime(at=datetime(2026, 1, 1, 9, 0))
        with self.assertRaises(ScheduleError):
            OneTime(at="2026-01-01T09:00:00Z")  # type: ignore[arg-type]
        with self.assertRaises(ScheduleError):
            Interval(anchor=datetime(2026, 1, 1), every=timedelta(hours=1))
        with self.assertRaises(ScheduleError):
            Daily(timezone=NEW_YORK, at=time(9, 0), end=datetime(2026, 1, 1))

    def test_interval_floor_is_five_minutes(self) -> None:
        self.assertEqual(MIN_INTERVAL, timedelta(minutes=5))
        anchor = utc("2026-01-01T00:00:00")
        Interval(anchor=anchor, every=MIN_INTERVAL)
        for bad in (
            timedelta(minutes=4),
            timedelta(seconds=299),
            timedelta(0),
            timedelta(minutes=-10),
        ):
            with self.subTest(every=bad):
                with self.assertRaises(ScheduleError):
                    Interval(anchor=anchor, every=bad)
        with self.assertRaises(ScheduleError):
            Interval(anchor=anchor, every=300)  # type: ignore[arg-type]

    def test_end_may_not_precede_the_anchor(self) -> None:
        with self.assertRaises(ScheduleError):
            Interval(
                anchor=utc("2026-01-01T12:00:00"),
                every=timedelta(hours=1),
                end=utc("2026-01-01T11:00:00"),
            )

    def test_local_time_must_be_a_whole_naive_hh_mm(self) -> None:
        for bad in (
            time(9, 0, 30),
            time(9, 0, 0, 1),
            time(9, 0, tzinfo=timezone.utc),
            "09:00",
            900,
        ):
            with self.subTest(at=bad):
                with self.assertRaises(ScheduleError):
                    Daily(timezone=NEW_YORK, at=bad)  # type: ignore[arg-type]

    def test_weekday_set_must_be_a_non_empty_set_of_weekday(self) -> None:
        for bad in (frozenset(), set(), [], (), 0, "MONDAY", frozenset({0, 1}), None):
            with self.subTest(days=bad):
                with self.assertRaises(ScheduleError):
                    Weekly(timezone=BERLIN, at=time(9, 0), days=bad)  # type: ignore[arg-type]
        self.assertEqual(
            Weekly(
                timezone=BERLIN,
                at=time(9, 0),
                days=[Weekday.MONDAY, Weekday.MONDAY],
            ).days,
            frozenset({Weekday.MONDAY}),
        )

    def test_unknown_schedule_kind_is_refused(self) -> None:
        now = utc("2026-01-01T00:00:00")
        for entry in (next_occurrence, latest_occurrence):
            with self.subTest(entry=entry.__name__):
                with self.assertRaises(ScheduleError):
                    entry(object(), now)  # type: ignore[arg-type]
                with self.assertRaises(ScheduleError):
                    entry("daily", now)  # type: ignore[arg-type]

    def test_naive_now_is_refused_by_every_kind(self) -> None:
        naive = datetime(2026, 6, 1, 12, 0)
        for schedule in (
            OneTime(at=utc("2026-06-01T12:00:00")),
            Interval(anchor=utc("2026-06-01T00:00:00"), every=timedelta(hours=1)),
            Daily(timezone=NEW_YORK, at=time(9, 0)),
            Weekly(timezone=BERLIN, at=time(9, 0), days=frozenset({Weekday.MONDAY})),
        ):
            with self.subTest(kind=schedule.kind):
                with self.assertRaises(ScheduleError):
                    next_occurrence(schedule, naive)
                with self.assertRaises(ScheduleError):
                    latest_occurrence(schedule, naive)


class OneTimeTest(unittest.TestCase):
    """One resolved instant, due exactly once."""

    def test_resolution_relative_to_now(self) -> None:
        schedule = OneTime(at=utc("2026-06-01T12:00:00"))
        self.assertEqual(
            next_occurrence(schedule, utc("2026-06-01T11:59:59")),
            utc("2026-06-01T12:00:00"),
        )
        self.assertIsNone(next_occurrence(schedule, utc("2026-06-01T12:00:00")))
        self.assertIsNone(latest_occurrence(schedule, utc("2026-06-01T11:59:59")))
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-06-01T12:00:00")),
            utc("2026-06-01T12:00:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2027-01-01T00:00:00")),
            utc("2026-06-01T12:00:00"),
        )

    def test_an_offset_instant_is_stored_as_utc(self) -> None:
        schedule = OneTime(at=datetime(2026, 6, 1, 9, tzinfo=ZoneInfo(NEW_YORK)))
        self.assertEqual(schedule.at, utc("2026-06-01T13:00:00"))
        self.assertEqual(schedule.at.tzinfo, timezone.utc)

    def test_local_creation_resolves_to_an_immutable_utc_instant(self) -> None:
        self.assertEqual(
            resolve_local_instant(datetime(2026, 7, 4, 9, 0), NEW_YORK),
            utc("2026-07-04T13:00:00"),
        )
        self.assertEqual(
            resolve_local_instant(datetime(2026, 1, 4, 9, 0), NEW_YORK),
            utc("2026-01-04T14:00:00"),
        )
        self.assertEqual(
            resolve_local_instant(datetime(2026, 6, 1, 9, 0), BERLIN),
            utc("2026-06-01T07:00:00"),
        )

    def test_nonexistent_local_time_is_rejected(self) -> None:
        """02:30 on 2026-03-08 in New York never happens; nothing may guess."""
        with self.assertRaises(ScheduleError) as caught:
            resolve_local_instant(datetime(2026, 3, 8, 2, 30), NEW_YORK)
        self.assertIn("does not exist", str(caught.exception))
        for fold in (0, 1):
            with self.subTest(fold=fold):
                with self.assertRaises(ScheduleError):
                    resolve_local_instant(
                        datetime(2026, 3, 8, 2, 30), NEW_YORK, fold=fold
                    )

    def test_ambiguous_local_time_requires_explicit_resolution(self) -> None:
        """01:30 on 2026-11-01 in New York happens twice; the caller picks."""
        local = datetime(2026, 11, 1, 1, 30)
        with self.assertRaises(ScheduleError) as caught:
            resolve_local_instant(local, NEW_YORK)
        self.assertIn("happens twice", str(caught.exception))
        self.assertEqual(
            resolve_local_instant(local, NEW_YORK, fold=0), utc("2026-11-01T05:30:00")
        )
        self.assertEqual(
            resolve_local_instant(local, NEW_YORK, fold=1), utc("2026-11-01T06:30:00")
        )

    def test_local_creation_fails_closed_on_bad_input(self) -> None:
        with self.assertRaises(ScheduleError):
            resolve_local_instant(
                datetime(2026, 7, 4, 9, tzinfo=timezone.utc), NEW_YORK
            )
        with self.assertRaises(ScheduleError):
            resolve_local_instant(datetime(2026, 7, 4, 9, 0), "Not/AZone")
        with self.assertRaises(ScheduleError):
            resolve_local_instant(datetime(2026, 7, 4, 9, 0), NEW_YORK, fold=2)


class IntervalTest(unittest.TestCase):
    """Elapsed-time arithmetic on a UTC anchor. No calendar, ever."""

    def setUp(self) -> None:
        self.schedule = Interval(
            anchor=utc("2026-01-01T00:00:00"), every=timedelta(minutes=5)
        )

    def test_next_is_strictly_after(self) -> None:
        self.assertEqual(
            next_occurrence(self.schedule, utc("2026-01-01T00:02:00")),
            utc("2026-01-01T00:05:00"),
        )
        self.assertEqual(
            next_occurrence(self.schedule, utc("2026-01-01T00:05:00")),
            utc("2026-01-01T00:10:00"),
        )
        self.assertEqual(
            next_occurrence(self.schedule, utc("2026-01-01T00:04:59.999999")),
            utc("2026-01-01T00:05:00"),
        )

    def test_before_the_anchor_the_anchor_is_next_and_nothing_is_due(self) -> None:
        self.assertEqual(
            next_occurrence(self.schedule, utc("2025-12-31T23:00:00")),
            utc("2026-01-01T00:00:00"),
        )
        self.assertIsNone(latest_occurrence(self.schedule, utc("2025-12-31T23:59:59")))
        self.assertEqual(
            latest_occurrence(self.schedule, utc("2026-01-01T00:00:00")),
            utc("2026-01-01T00:00:00"),
        )

    def test_latest_lands_on_the_grid(self) -> None:
        self.assertEqual(
            latest_occurrence(self.schedule, utc("2026-01-01T00:07:00")),
            utc("2026-01-01T00:05:00"),
        )
        self.assertEqual(
            latest_occurrence(self.schedule, utc("2026-01-01T00:09:59")),
            utc("2026-01-01T00:05:00"),
        )

    def test_a_dst_transition_does_not_move_an_interval(self) -> None:
        """The New York spring-forward happens at 07:00Z; nothing shifts."""
        schedule = Interval(
            anchor=utc("2026-03-08T06:00:00"), every=timedelta(minutes=30)
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-03-08T06:45:00")),
            utc("2026-03-08T07:00:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-03-08T07:29:59")),
            utc("2026-03-08T07:00:00"),
        )

    def test_end_instant_is_respected(self) -> None:
        schedule = Interval(
            anchor=utc("2026-01-01T00:00:00"),
            every=timedelta(hours=1),
            end=utc("2026-01-01T03:00:00"),
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-01-01T02:30:00")),
            utc("2026-01-01T03:00:00"),
        )
        self.assertIsNone(next_occurrence(schedule, utc("2026-01-01T03:00:00")))
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-06-01T00:00:00")),
            utc("2026-01-01T03:00:00"),
        )


class DailyTest(unittest.TestCase):
    """One local HH:MM a day, with the approved gap and fold policies."""

    def test_ordinary_next_and_latest(self) -> None:
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        self.assertEqual(
            next_occurrence(schedule, utc("2026-07-04T12:00:00")),
            utc("2026-07-04T13:00:00"),
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-07-04T13:00:00")),
            utc("2026-07-05T13:00:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-07-04T12:59:59")),
            utc("2026-07-03T13:00:00"),
        )

    def test_the_local_hour_holds_across_a_transition(self) -> None:
        """09:00 stays 09:00 locally; the UTC instant is what moves."""
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        self.assertEqual(
            next_occurrence(schedule, utc("2026-03-06T14:00:00")),
            utc("2026-03-07T14:00:00"),
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-03-08T00:00:00")),
            utc("2026-03-08T13:00:00"),
        )

    def test_spring_forward_skips_the_nonexistent_occurrence(self) -> None:
        """02:30 does not exist on 2026-03-08; that day has no occurrence."""
        schedule = Daily(timezone=NEW_YORK, at=time(2, 30))
        self.assertEqual(
            next_occurrence(schedule, utc("2026-03-07T08:00:00")),
            utc("2026-03-09T06:30:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-03-08T12:00:00")),
            utc("2026-03-07T07:30:00"),
        )

    def test_fall_back_uses_the_earlier_occurrence_only(self) -> None:
        """01:30 happens twice on 2026-11-01; only 05:30Z is ever named."""
        schedule = Daily(timezone=NEW_YORK, at=time(1, 30))
        self.assertEqual(
            next_occurrence(schedule, utc("2026-10-31T12:00:00")),
            utc("2026-11-01T05:30:00"),
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-11-01T05:30:00")),
            utc("2026-11-02T06:30:00"),
        )
        # Searching backwards from beyond the repeated hour must still refuse
        # to name 06:30Z, the second 01:30 local.
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-11-01T06:45:00")),
            utc("2026-11-01T05:30:00"),
        )

    def test_end_instant_is_respected(self) -> None:
        schedule = Daily(
            timezone=NEW_YORK, at=time(9, 0), end=utc("2026-03-10T00:00:00")
        )
        self.assertIsNone(next_occurrence(schedule, utc("2026-03-09T14:00:00")))
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-06-01T00:00:00")),
            utc("2026-03-09T13:00:00"),
        )


class WeeklyTest(unittest.TestCase):
    """An explicit weekday set, same gap and fold policies as DAILY."""

    def test_ordinary_next_occurrence(self) -> None:
        schedule = Weekly(
            timezone=BERLIN, at=time(9, 0), days=frozenset({Weekday.MONDAY})
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-05-31T00:00:00")),
            utc("2026-06-01T07:00:00"),
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-06-01T07:00:00")),
            utc("2026-06-08T07:00:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-06-03T00:00:00")),
            utc("2026-06-01T07:00:00"),
        )

    def test_multi_day_set_is_deterministic(self) -> None:
        schedule = Weekly(
            timezone=BERLIN,
            at=time(9, 0),
            days=frozenset({Weekday.MONDAY, Weekday.THURSDAY}),
        )
        moment = utc("2026-05-31T00:00:00")
        walked = []
        for _ in range(4):
            moment = next_occurrence(schedule, moment)
            walked.append(moment)
        self.assertEqual(
            walked,
            [
                utc("2026-06-01T07:00:00"),
                utc("2026-06-04T07:00:00"),
                utc("2026-06-08T07:00:00"),
                utc("2026-06-11T07:00:00"),
            ],
        )

    def test_spring_forward_skips_a_whole_week(self) -> None:
        """2026-03-08 is the Sunday New York loses 02:30 to."""
        schedule = Weekly(
            timezone=NEW_YORK, at=time(2, 30), days=frozenset({Weekday.SUNDAY})
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-03-01T08:00:00")),
            utc("2026-03-15T06:30:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-03-08T23:00:00")),
            utc("2026-03-01T07:30:00"),
        )

    def test_fall_back_uses_the_earlier_occurrence_only(self) -> None:
        """The DAILY fold policy, proven again on the weekday path.

        WEEKLY reaches the local-instant resolution through the weekday
        filter, so the earlier-instant rule is asserted here too rather than
        assumed to carry over: 01:30 happens twice on Sunday 2026-11-01 and
        only 05:30Z may ever be named, searching either direction.
        """
        schedule = Weekly(
            timezone=NEW_YORK, at=time(1, 30), days=frozenset({Weekday.SUNDAY})
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-10-31T12:00:00")),
            utc("2026-11-01T05:30:00"),
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-11-01T06:45:00")),
            utc("2026-11-01T05:30:00"),
        )
        # The second 01:30 local must not become an occurrence of its own.
        self.assertEqual(
            next_occurrence(schedule, utc("2026-11-01T05:30:00")),
            utc("2026-11-08T06:30:00"),
        )

    def test_end_instant_is_respected(self) -> None:
        schedule = Weekly(
            timezone=BERLIN,
            at=time(9, 0),
            days=frozenset({Weekday.MONDAY}),
            end=utc("2026-06-05T00:00:00"),
        )
        self.assertIsNone(next_occurrence(schedule, utc("2026-06-01T07:00:00")))
        self.assertEqual(
            latest_occurrence(schedule, utc("2030-01-01T00:00:00")),
            utc("2026-06-01T07:00:00"),
        )


class ClockTest(unittest.TestCase):
    """The answers are a pure function of (schedule, instant)."""

    def test_clock_rollback_does_not_change_nominal_identity(self) -> None:
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        forward = latest_occurrence(schedule, utc("2029-07-04T18:00:00"))
        self.assertEqual(forward, utc("2029-07-04T13:00:00"))
        # The clock jumps back behind today's occurrence: the nominal instant
        # named is the previous one, not a new or shifted generation.
        rolled_back = latest_occurrence(schedule, utc("2029-07-04T12:00:00"))
        self.assertEqual(rolled_back, utc("2029-07-03T13:00:00"))
        # And re-advancing re-derives the very same instant, so a caller that
        # already generated it recognizes it rather than repeating it.
        self.assertEqual(
            latest_occurrence(schedule, utc("2029-07-04T18:00:00")), forward
        )
        self.assertEqual(next_occurrence(schedule, rolled_back), forward)

    def test_interval_identity_survives_a_rollback(self) -> None:
        schedule = Interval(
            anchor=utc("2026-01-01T00:00:00"), every=timedelta(minutes=15)
        )
        due = latest_occurrence(schedule, utc("2026-01-01T09:20:00"))
        self.assertEqual(due, utc("2026-01-01T09:15:00"))
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-01-01T09:10:00")),
            utc("2026-01-01T09:00:00"),
        )
        self.assertEqual(latest_occurrence(schedule, utc("2026-01-01T09:20:00")), due)


class BoundedWorkTest(unittest.TestCase):
    """A long downtime costs what a short one costs.

    The bound is asserted by COUNTING calendar probes, not by timing: a
    missed-run enumeration would show up here as one probe per missed
    occurrence, whatever the machine's speed.
    """

    def _probes(self, call) -> tuple[datetime | None, int]:
        original = schedules._local_instant
        with mock.patch.object(
            schedules, "_local_instant", side_effect=original
        ) as spy:
            return call(), spy.call_count

    def test_daily_latest_after_a_five_year_downtime(self) -> None:
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        result, probes = self._probes(
            lambda: latest_occurrence(schedule, utc("2029-07-04T18:00:00"))
        )
        self.assertEqual(result, utc("2029-07-04T13:00:00"))
        self.assertLessEqual(probes, schedules._SEARCH_DAYS)

    def test_weekly_latest_after_a_five_year_downtime(self) -> None:
        schedule = Weekly(
            timezone=BERLIN, at=time(9, 0), days=frozenset({Weekday.MONDAY})
        )
        result, probes = self._probes(
            lambda: latest_occurrence(schedule, utc("2031-02-05T12:00:00"))
        )
        self.assertEqual(result, utc("2031-02-03T08:00:00"))
        self.assertLessEqual(probes, schedules._SEARCH_DAYS)

    def test_daily_next_after_a_five_year_downtime(self) -> None:
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        result, probes = self._probes(
            lambda: next_occurrence(schedule, utc("2029-07-04T18:00:00"))
        )
        self.assertEqual(result, utc("2029-07-05T13:00:00"))
        self.assertLessEqual(probes, schedules._SEARCH_DAYS)

    def test_interval_latest_across_multiple_years_is_arithmetic(self) -> None:
        """Six and a half years of five-minute occurrences, in one step."""
        schedule = Interval(
            anchor=utc("2020-01-01T00:00:00"), every=timedelta(minutes=5)
        )
        self.assertEqual(
            latest_occurrence(schedule, utc("2026-09-05T12:07:00")),
            utc("2026-09-05T12:05:00"),
        )
        self.assertEqual(
            next_occurrence(schedule, utc("2026-09-05T12:07:00")),
            utc("2026-09-05T12:10:00"),
        )

    def test_latest_due_coalesces_a_missed_history_into_one_generation(self) -> None:
        """The approved rule, expressed the way persistence will use it."""
        schedule = Daily(timezone=NEW_YORK, at=time(9, 0))
        acted_on = utc("2026-07-04T13:00:00")
        due = latest_occurrence(schedule, utc("2029-07-04T18:00:00"))
        self.assertEqual(due, utc("2029-07-04T13:00:00"))
        self.assertGreater(due, acted_on)  # exactly one generation, not 1096
        self.assertIsNone(
            next_occurrence(
                Daily(timezone=NEW_YORK, at=time(9, 0), end=acted_on), due
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
