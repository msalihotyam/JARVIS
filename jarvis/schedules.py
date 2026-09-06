"""Pure deterministic schedule arithmetic (Scheduler phase 1A).

The whole calculation kernel and nothing else: given a schedule and an
EXPLICIT instant, what is the next nominal occurrence, and what is the latest
nominal occurrence at or before that instant. No persistence, no dispatch, no
task materialization, no execution, no clock of its own.

Two questions, deliberately kept apart:

NOMINAL     what instant does this schedule name? ``next_occurrence`` and
            ``latest_occurrence`` answer it, purely, from arguments alone.
DUE         should something be generated for that instant? Only the caller
            answers it, by comparing a nominal instant against the anchor it
            already holds. This module keeps no state, so it cannot.

That split is what makes the approved coalescing rule - "at most one
latest-due recurring generation" - implementable without enumerating a missed
history: persistence asks ``latest_occurrence(schedule, now)`` for the single
latest nominal instant and compares it with the last one it generated. A
machine that was offline for a year answers in the same bounded work as one
that was offline for a minute; nothing here ever loops once per missed
interval.

Time policy:

* Every instant crossing this boundary is an AWARE datetime. A naive
  datetime is refused, never assumed to be UTC and never assumed to be local.
* Calendar kinds (DAILY, WEEKLY) carry an EXPLICIT IANA timezone identifier,
  resolved through ``zoneinfo``. The machine's own timezone is never read -
  there is no ``datetime.now()``, no argument-less ``astimezone()`` and no
  ``ZoneInfo`` fallback here. A key that cannot be resolved fails closed.
* ``INTERVAL`` is pure elapsed-time arithmetic on a UTC anchor. It has no
  calendar interpretation at all, so a DST transition does not shift it.

DST, for the calendar kinds:

GAP  (spring forward) a local time that does not exist that day has NO
     occurrence that day; the day is skipped, never shifted into the hour
     that replaced it.
FOLD (fall back) a local time that happens twice occurs ONCE, at the earlier
     of the two instants (``fold=0``).

Only the four approved kinds exist. There is no cron parser, no RRULE
parser, no ``BYSETPOS`` and no general recurrence machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum, IntEnum
from typing import ClassVar, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleError(ValueError):
    """An invalid schedule, or an input the kernel refuses to interpret.

    Raised at construction, so an invalid schedule cannot exist to be asked a
    question later, and by the calculation entry points for an instant that
    would otherwise have to be guessed at.
    """


class ScheduleKind(str, Enum):
    """The closed set of approved schedule kinds. There is no other."""

    ONE_TIME = "one_time"
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"


class Weekday(IntEnum):
    """Weekday vocabulary, numbered exactly as ``datetime.weekday()``."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


#: The approved floor for INTERVAL. A shorter period is refused, not clamped.
MIN_INTERVAL = timedelta(minutes=5)

#: How many consecutive local days a calendar search may inspect before it
#: gives up. Seven for a weekly cycle, plus slack for the days a DST gap
#: deletes the occurrence from (at most one such day per transition, and a
#: zone has at most two transitions a year). The bound is what makes an
#: arbitrarily long downtime cost the same as a short one; exhausting it is a
#: bug, not a "no occurrence" answer, so it raises.
_SEARCH_DAYS = 16


def _aware(value: object, field: str) -> datetime:
    """The value as a UTC instant, or ``ScheduleError``.

    A naive datetime is refused rather than read as UTC or as machine-local
    time: both readings are guesses, and one of them is silently wrong.
    """
    if not isinstance(value, datetime):
        raise ScheduleError(f"{field} is not a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError(f"{field} is naive; an explicit timezone is required")
    return value.astimezone(timezone.utc)


def _zone(name: object) -> ZoneInfo:
    """The IANA zone for an explicit identifier, or ``ScheduleError``.

    The only place a timezone enters this module. There is no default and no
    fallback: an unknown, unreadable or non-string key fails closed, so a
    build whose timezone database cannot answer refuses to calculate rather
    than quietly answering in UTC or in the machine's own timezone. Supplying
    a reviewed pinned database later is a deployment question - this function
    does not change.
    """
    if type(name) is not str or not name:
        raise ScheduleError("the timezone is not an IANA identifier")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError(f"unknown IANA timezone {name!r}") from None


def _clock(value: object, field: str) -> time:
    """The value as a naive local HH:MM, or ``ScheduleError``."""
    if type(value) is not time:
        raise ScheduleError(f"{field} is not a time")
    if value.tzinfo is not None:
        raise ScheduleError(f"{field} carries a timezone; the schedule owns it")
    if value.second or value.microsecond or value.fold:
        raise ScheduleError(f"{field} is not a whole HH:MM")
    return value


def _weekdays(value: object) -> frozenset[Weekday]:
    """The value as a non-empty set of ``Weekday``, or ``ScheduleError``.

    Bare integers are refused even though ``Weekday`` is an ``IntEnum``: the
    vocabulary is the enum, and ``7`` is not a day of the week.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ScheduleError("the weekday set is not a set of Weekday")
    days = frozenset(value)
    if not days:
        raise ScheduleError("the weekday set is empty")
    if any(type(day) is not Weekday for day in days):
        raise ScheduleError("the weekday set holds something that is not a Weekday")
    return days


@dataclass(frozen=True, slots=True)
class OneTime:
    """A single absolute UTC instant.

    Already resolved: a local wall-clock intent becomes one of these through
    ``resolve_local_instant``, which is where DST ambiguity is settled. Once
    constructed there is no wall clock left to reinterpret.
    """

    kind: ClassVar[ScheduleKind] = ScheduleKind.ONE_TIME

    at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _aware(self.at, "at"))


@dataclass(frozen=True, slots=True)
class Interval:
    """Every ``every`` from ``anchor``, as elapsed time.

    UTC anchored and calendar-free by construction: the occurrences are
    ``anchor + n * every``, so a DST transition moves none of them.
    """

    kind: ClassVar[ScheduleKind] = ScheduleKind.INTERVAL

    anchor: datetime
    every: timedelta
    end: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor", _aware(self.anchor, "anchor"))
        if type(self.every) is not timedelta:
            raise ScheduleError("every is not a timedelta")
        if self.every < MIN_INTERVAL:
            raise ScheduleError(
                f"the interval is shorter than the {MIN_INTERVAL} minimum"
            )
        if self.end is not None:
            object.__setattr__(self, "end", _aware(self.end, "end"))
            if self.end < self.anchor:
                raise ScheduleError("the end instant precedes the anchor")


@dataclass(frozen=True, slots=True)
class Daily:
    """One local HH:MM every day, in an explicit IANA timezone."""

    kind: ClassVar[ScheduleKind] = ScheduleKind.DAILY

    timezone: str
    at: time
    end: datetime | None = None

    def __post_init__(self) -> None:
        _zone(self.timezone)
        object.__setattr__(self, "at", _clock(self.at, "at"))
        if self.end is not None:
            object.__setattr__(self, "end", _aware(self.end, "end"))


@dataclass(frozen=True, slots=True)
class Weekly:
    """One local HH:MM on an explicit non-empty set of weekdays."""

    kind: ClassVar[ScheduleKind] = ScheduleKind.WEEKLY

    timezone: str
    at: time
    days: frozenset[Weekday]
    end: datetime | None = None

    def __post_init__(self) -> None:
        _zone(self.timezone)
        object.__setattr__(self, "at", _clock(self.at, "at"))
        object.__setattr__(self, "days", _weekdays(self.days))
        if self.end is not None:
            object.__setattr__(self, "end", _aware(self.end, "end"))


Schedule = OneTime | Interval | Daily | Weekly


def resolve_local_instant(
    local: datetime, timezone_name: str, *, fold: int | None = None
) -> datetime:
    """The UTC instant a naive local wall-clock time names.

    The one creation helper that turns a local intent into an absolute
    instant, and the only place a caller is asked to settle DST for a single
    moment:

    * a local time a spring-forward deleted is REFUSED - there is no instant
      it names, and picking the neighbouring hour would silently run
      something at a time nobody asked for;
    * a local time a fall-back repeated is refused unless ``fold`` says which
      of the two instants is meant (``0`` earlier, ``1`` later).

    ``fold`` on an unambiguous time is accepted and has no effect, exactly as
    PEP 495 defines it.
    """
    if not isinstance(local, datetime):
        raise ScheduleError("the local time is not a datetime")
    if local.tzinfo is not None:
        raise ScheduleError("the local time already carries a timezone")
    if fold is not None and fold not in (0, 1):
        raise ScheduleError("fold is neither 0 nor 1")
    zone = _zone(timezone_name)
    earlier = local.replace(tzinfo=zone, fold=0)
    if earlier.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
        raise ScheduleError(f"{local.isoformat()} does not exist in {timezone_name}")
    if earlier.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
        if fold is None:
            raise ScheduleError(
                f"{local.isoformat()} happens twice in {timezone_name}; "
                "an explicit fold is required"
            )
    return local.replace(tzinfo=zone, fold=fold or 0).astimezone(timezone.utc)


def _local_instant(day: date, at: time, zone: ZoneInfo) -> datetime | None:
    """The UTC instant of ``at`` on local ``day``, or ``None`` if it is a gap.

    ``fold=0`` implements the approved FOLD policy directly: when the local
    time happens twice, this is the earlier of the two instants, and the later
    one is never produced by any recurrence.

    The round trip is the GAP test. A nonexistent local time still converts -
    with the offset in force before the transition - but converting back
    renders a different wall clock, namely the hour that replaced the one
    asked for.
    """
    naive = datetime.combine(day, at)
    local = naive.replace(tzinfo=zone, fold=0)
    if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return local.astimezone(timezone.utc)


def _calendar_occurrence(
    schedule: Daily | Weekly, moment: datetime, *, forward: bool
) -> datetime | None:
    """The first occurrence strictly after / at or before ``moment``.

    Bounded by construction: it walks local DAYS, at most ``_SEARCH_DAYS`` of
    them, so a five-year downtime costs what a five-minute one costs. It
    never walks occurrences, which is what an unbounded missed-run
    enumeration would do.
    """
    zone = _zone(schedule.timezone)
    days = getattr(schedule, "days", None)
    start = moment.astimezone(zone).date()
    step = timedelta(days=1 if forward else -1)
    for offset in range(_SEARCH_DAYS):
        day = start + offset * step
        if days is not None and Weekday(day.weekday()) not in days:
            continue
        occurrence = _local_instant(day, schedule.at, zone)
        if occurrence is None:
            continue
        if forward:
            if occurrence <= moment:
                continue
            if schedule.end is not None and occurrence > schedule.end:
                return None
        elif occurrence > moment:
            continue
        return occurrence
    raise ScheduleError(
        f"no occurrence within {_SEARCH_DAYS} days of {moment.isoformat()}"
    )


def next_occurrence(schedule: Schedule, after: datetime) -> datetime | None:
    """The first nominal occurrence STRICTLY after ``after``, or ``None``.

    ``None`` means the schedule names nothing later: a one-time already at or
    before ``after``, or a recurrence whose next occurrence would pass its end
    instant. ``after`` is the caller's anchor - the occurrence it last acted
    on, or the moment it is planning from - and must be an aware datetime.
    """
    moment = _aware(after, "after")
    if isinstance(schedule, OneTime):
        return schedule.at if schedule.at > moment else None
    if isinstance(schedule, Interval):
        if moment < schedule.anchor:
            occurrence = schedule.anchor
        else:
            elapsed = (moment - schedule.anchor) // schedule.every
            occurrence = schedule.anchor + (elapsed + 1) * schedule.every
        if schedule.end is not None and occurrence > schedule.end:
            return None
        return occurrence
    if isinstance(schedule, (Daily, Weekly)):
        return _calendar_occurrence(schedule, moment, forward=True)
    raise ScheduleError("unknown schedule kind")


def latest_occurrence(schedule: Schedule, now: datetime) -> datetime | None:
    """The latest nominal occurrence AT OR BEFORE ``now``, or ``None``.

    The whole missed-run answer, in one value and in bounded work. For a
    ONE_TIME it is the single occurrence, once it is due. For a recurrence it
    is the latest one the schedule names, whether the caller was running then
    or offline for a year - which is what lets persistence coalesce a missed
    history into at most one generation by comparing this against the last
    occurrence it acted on.

    An end instant caps it: after the end the answer stays the last
    occurrence the schedule named, it does not become ``None``.
    """
    moment = _aware(now, "now")
    if isinstance(schedule, OneTime):
        return schedule.at if schedule.at <= moment else None
    if isinstance(schedule, Interval):
        bound = moment if schedule.end is None else min(moment, schedule.end)
        if bound < schedule.anchor:
            return None
        elapsed = (bound - schedule.anchor) // schedule.every
        return schedule.anchor + elapsed * schedule.every
    if isinstance(schedule, (Daily, Weekly)):
        bound = moment if schedule.end is None else min(moment, schedule.end)
        return _calendar_occurrence(schedule, bound, forward=False)
    raise ScheduleError("unknown schedule kind")


__all__ = [
    "MIN_INTERVAL",
    "Daily",
    "Interval",
    "OneTime",
    "Schedule",
    "ScheduleError",
    "ScheduleKind",
    "Weekday",
    "Weekly",
    "latest_occurrence",
    "next_occurrence",
    "resolve_local_instant",
]
