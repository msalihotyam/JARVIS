"""N6STOP1: the kit section 5 stop rule evaluator (pure; no clock, file or network)."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone

from jarvis import n6_stop
from jarvis.n6_kpi import KpiRow
from jarvis.n6_stop import CONTINUE, PAUSE, STOP, Event, StopInputError, Verdict, evaluate

AT = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def row(slice_id: str = "S1", source: str = "n6", *, passed: bool = True, defects: int = 0,
        security: int = 0) -> KpiRow:
    return KpiRow(slice_id, source, passed, defects, 0, 1000, 60.0, security, AT)


def run(rows: object = (), events: object = (), usage: object = 50.0,
        share: object = 5.0) -> Verdict:
    return evaluate(rows=rows, events=events, usage_pct=usage, n6_share_pct=share)  # type: ignore[arg-type]


def results(*spec: str) -> list[Event]:
    """"n6:blocked conveyor:pass ..." -> result events, oldest first."""
    return [Event(kind, source) for source, kind in (s.split(":") for s in spec)]


class StopTests(unittest.TestCase):
    def test_security_incident_stops(self) -> None:
        self.assertEqual(run(rows=[row(security=1)]), Verdict(STOP, n6_stop.SECURITY_INCIDENT))
        self.assertEqual(run(events=[Event("security_incident", "n6")]), Verdict(STOP, n6_stop.SECURITY_INCIDENT))

    def test_false_pass_stops(self) -> None:
        self.assertEqual(run(rows=[row(defects=1)]), Verdict(STOP, n6_stop.FALSE_PASS))
        self.assertEqual(run(events=[Event("false_pass", "n6")]), Verdict(STOP, n6_stop.FALSE_PASS))

    def test_two_manager_stops(self) -> None:
        self.assertEqual(run(events=[Event("two_manager", "n6")]), Verdict(STOP, n6_stop.TWO_MANAGER))

    def test_acceptance_hash_mismatch_stops(self) -> None:
        self.assertEqual(run(events=[Event("acceptance_hash_mismatch", "conveyor")]),
                         Verdict(STOP, n6_stop.ACCEPTANCE_HASH_MISMATCH))

    def test_stop_outranks_pause(self) -> None:
        stop = run(events=[Event("two_manager", "n6"), *results("n6:blocked", "n6:blocked", "n6:blocked")],
                   usage=99.0, share=50.0)
        self.assertEqual(stop, Verdict(STOP, n6_stop.TWO_MANAGER))

    def test_stop_outranks_missing_input(self) -> None:
        self.assertEqual(run(rows=[row(security=1)], events=None, usage=None, share=None),
                         Verdict(STOP, n6_stop.SECURITY_INCIDENT))


class PauseTests(unittest.TestCase):
    def test_quota_at_line_or_inside_reserve_pauses(self) -> None:
        self.assertEqual(run(usage=94.9).decision, CONTINUE)
        for usage in (95, 95.0, 97.5, 100):
            self.assertEqual(run(usage=usage), Verdict(PAUSE, n6_stop.QUOTA_LINE), usage)

    def test_three_consecutive_blocked_pauses(self) -> None:
        self.assertEqual(run(events=results("n6:blocked", "n6:blocked", "n6:blocked")),
                         Verdict(PAUSE, n6_stop.BLOCKED_RUN))

    def test_conveyor_result_does_not_reset_streak(self) -> None:
        self.assertEqual(run(events=results("n6:blocked", "conveyor:pass", "n6:blocked", "n6:blocked")),
                         Verdict(PAUSE, n6_stop.BLOCKED_RUN))

    def test_share_cap_pauses(self) -> None:
        self.assertEqual(run(share=14.9).decision, CONTINUE)
        self.assertEqual(run(share=15), Verdict(PAUSE, n6_stop.SHARE_CAP))

    def test_otherwise_continue(self) -> None:
        self.assertEqual(run(rows=[row(), row(source="conveyor", passed=False)],
                             events=results("n6:pass", "n6:blocked")), Verdict(CONTINUE, n6_stop.OK))
        self.assertEqual(run(rows=[], events=[]), Verdict(CONTINUE, n6_stop.OK))

    def test_thresholds_are_constants_not_arguments(self) -> None:
        self.assertEqual(set(inspect.signature(evaluate).parameters),
                         {"rows", "events", "usage_pct", "n6_share_pct"})
        self.assertEqual((n6_stop.QUOTA_LINE_PCT, n6_stop.RESERVE_PCT, n6_stop.BLOCKED_STREAK,
                          n6_stop.N6_SHARE_CAP_PCT), (95, 5, 3, 15))
        with self.assertRaises(TypeError):
            evaluate(rows=(), events=(), usage_pct=50.0, n6_share_pct=5.0,  # type: ignore[call-arg]
                     quota_line=100)


class NegativeTests(unittest.TestCase):
    def test_missing_input_pauses_never_continues(self) -> None:
        self.assertEqual(run(rows=None), Verdict(PAUSE, n6_stop.INPUT_MISSING))
        self.assertEqual(run(events=None), Verdict(PAUSE, n6_stop.INPUT_MISSING))
        self.assertEqual(run(share=None), Verdict(PAUSE, n6_stop.SHARE_UNKNOWN))

    def test_unknown_quota_pauses(self) -> None:
        self.assertEqual(run(usage=None), Verdict(PAUSE, n6_stop.QUOTA_UNKNOWN))

    def test_blocked_pass_blocked_continues(self) -> None:
        self.assertEqual(run(events=results("n6:blocked", "n6:blocked", "n6:pass", "n6:blocked")),
                         Verdict(CONTINUE, n6_stop.OK))
        # below the streak, and a later N6 PASS ends a streak (only the current run counts)
        self.assertEqual(run(events=results("n6:pass", "n6:blocked", "n6:blocked")), Verdict(CONTINUE, n6_stop.OK))
        self.assertEqual(run(events=results("n6:blocked", "n6:blocked", "n6:blocked", "n6:pass")),
                         Verdict(CONTINUE, n6_stop.OK))

    def test_conveyor_false_pass_does_not_stop_n6(self) -> None:
        self.assertEqual(run(rows=[row(source="conveyor", defects=2)], events=[Event("false_pass", "conveyor")]),
                         Verdict(CONTINUE, n6_stop.OK))

    def test_malformed_row_raises_no_verdict(self) -> None:
        good = row()
        forged = row()
        object.__setattr__(forged, "post_accept_defects", -1)
        bad_event = Event("pass", "n6")
        object.__setattr__(bad_event, "kind", "maybe")
        unhashable = Event("pass", "n6")
        object.__setattr__(unhashable, "kind", ["pass"])
        for rows, events in (([{"slice_id": "S1"}], []), ([good, ("S1", "n6")], []), ([forged], []),
                             ("S1", []), ([good], ["pass"]), ([good], [bad_event]), ([good], [unhashable]),
                             # malformed beats a STOP elsewhere: no verdict at all
                             ([row(security=1), {"x": 1}], []), ([], [Event("two_manager", "n6"), None])):
            with self.subTest(rows=rows, events=events), self.assertRaises(StopInputError):
                run(rows=rows, events=events)

    def test_malformed_reading_raises(self) -> None:
        for value in (True, "95", float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value), self.assertRaises(StopInputError):
                run(usage=value)
            with self.subTest(share=value), self.assertRaises(StopInputError):
                run(share=value)

    def test_unknown_event_kind_or_source_refused(self) -> None:
        with self.assertRaises(StopInputError):
            Event("paused", "n6")
        with self.assertRaises(StopInputError):
            Event("blocked", "codex")


class LyingStr(str):
    """Equal to everything, unequal to everything: defeats == / != / in checks."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


class LyingFloat(float):
    """Every comparison True: passes 0 <= x <= 100 whatever its value."""

    def __ge__(self, other: object) -> bool:
        return True

    __le__ = __lt__ = __gt__ = __ge__


class LyingInt(int):
    """Every comparison False: a negative count passes ``< 0`` and a real incident fails ``> 0``."""

    def __lt__(self, other: object) -> bool:
        return False

    __gt__ = __le__ = __ge__ = __lt__


class EqualToEveryType(type):
    """A metaclass equal to every type: its classes pass `type(x) in (int, float)` style gates."""

    def __eq__(cls, other: object) -> bool:
        return True

    __hash__ = type.__hash__


class MetaLieInt(int, metaclass=EqualToEveryType):
    """Every comparison True and float() 0.0: a real 99% reads as a safe 0% if the gate lets it in."""

    def __ge__(self, other: object) -> bool:
        return True

    __le__ = __lt__ = __gt__ = __ge__

    def __float__(self) -> float:
        return 0.0


class MetaLieFloat(float, metaclass=EqualToEveryType):
    def __ge__(self, other: object) -> bool:
        return True

    __le__ = __lt__ = __gt__ = __ge__

    def __float__(self) -> float:
        return 0.0


class MetaLieList(list[object], metaclass=EqualToEveryType):
    """Hides its items from iteration: a real incident inside reads as no rows/events."""

    def __iter__(self):  # type: ignore[override]
        return iter(())


class MetaLieTuple(tuple[object, ...], metaclass=EqualToEveryType):
    def __iter__(self):  # type: ignore[override]
        return iter(())


def forged(item: object, **fields: object) -> object:
    for name, value in fields.items():
        object.__setattr__(item, name, value)
    return item


class ExactTypeTests(unittest.TestCase):
    """N6STOP1FIX: lying subclasses and shadowed __post_init__ raise, never CONTINUE."""

    def test_lying_str_event_kind_or_source_refused(self) -> None:
        for kind, source in ((LyingStr("false_pass"), "n6"), ("false_pass", LyingStr("n6")),
                             (LyingStr("maybe"), "n6"), ("pass", LyingStr("codex"))):
            with self.subTest(kind=kind, source=source), self.assertRaises(StopInputError):
                Event(kind, source)

    def test_forged_lying_str_event_raises(self) -> None:
        for fields in ({"kind": LyingStr("false_pass")}, {"source": LyingStr("n6")}):
            with self.subTest(fields=fields), self.assertRaises(StopInputError):
                run(events=[forged(Event("false_pass", "n6"), **fields)])

    def test_shadowed_post_init_still_checked(self) -> None:
        noop = {"__post_init__": lambda: None}
        event = forged(Event("pass", "n6"), kind="maybe", **noop)
        kpi = forged(row(), security_incidents=-1, **noop)
        for rows, events in (([], [event]), ([kpi], [])):
            with self.subTest(rows=rows, events=events), self.assertRaises(StopInputError):
                run(rows=rows, events=events)

    def test_lying_number_reading_refused(self) -> None:
        for value in (LyingFloat(-5.0), LyingFloat(500.0), LyingFloat(99.0), LyingInt(-1), LyingInt(50)):
            with self.subTest(usage=value), self.assertRaises(StopInputError):
                run(usage=value)
            with self.subTest(share=value), self.assertRaises(StopInputError):
                run(share=value)

    def test_metaclass_eq_number_reading_refused(self) -> None:
        """N6STOP1FIX2: the gate is identity; a metaclass __eq__ -> True does not pass it."""
        self.assertTrue(MetaLieInt in (int, float))  # the gate the fix replaced would have let it in
        for value in (MetaLieInt(99), MetaLieInt(-5), MetaLieInt(500), MetaLieFloat(99.0), MetaLieFloat(-5.0)):
            with self.subTest(usage=value), self.assertRaises(StopInputError):
                run(usage=value)
        for value in (MetaLieInt(80), MetaLieInt(-5), MetaLieFloat(80.0)):
            with self.subTest(share=value), self.assertRaises(StopInputError):
                run(share=value)

    def test_metaclass_eq_container_refused(self) -> None:
        self.assertTrue(MetaLieList in (list, tuple))
        for container in (MetaLieList, MetaLieTuple):
            with self.subTest(rows=container.__name__), self.assertRaises(StopInputError):
                run(rows=container([row(security=1)]))
            with self.subTest(events=container.__name__), self.assertRaises(StopInputError):
                run(events=container([Event("false_pass", "n6")]))

    def test_lying_row_field_refused(self) -> None:
        for fields in ({"security_incidents": LyingInt(1)}, {"post_accept_defects": LyingInt(-1)},
                       {"source": LyingStr("n6")}):
            with self.subTest(fields=fields), self.assertRaises(StopInputError):
                run(rows=[forged(row(), **fields)])
        with self.assertRaises(StopInputError):  # built directly: n6_kpi's isinstance check lets it in
            run(rows=[KpiRow("S1", "n6", True, 0, 0, 1000, 60.0, LyingInt(1), AT)])

    def test_later_check_cannot_rewrite_a_checked_row(self) -> None:
        incident = row(security=1)

        class Rewriting(datetime):  # tzinfo runs inside the next row's __post_init__
            @property
            def tzinfo(self) -> timezone:  # type: ignore[override]
                object.__setattr__(incident, "security_incidents", 0)
                return timezone.utc

        later = forged(row("S2"), recorded_at=Rewriting(2026, 11, 24))
        self.assertEqual(run(rows=[incident, later]), Verdict(STOP, n6_stop.SECURITY_INCIDENT))

        class Resetting(int):  # runs inside the same row's __post_init__, after the copy was taken
            def __lt__(self, other: object) -> bool:
                object.__setattr__(negative, "security_incidents", 0)
                return False

        negative = forged(row(), security_incidents=-1, quota_tokens=Resetting(5))
        with self.assertRaises(StopInputError):
            run(rows=[negative])

    def test_hostile_input_raises_stop_input_error_only(self) -> None:
        class Seq(list[KpiRow]):
            def __iter__(self):  # type: ignore[override]
                raise ZeroDivisionError

        class BadRepr:
            def __repr__(self) -> str:
                raise RuntimeError

        for rows in (Seq([row()]), bytearray(), memoryview(b""), range(0), [BadRepr()],
                     [object.__new__(KpiRow)], [forged(row(), origin=BadRepr())]):
            with self.subTest(rows=type(rows).__name__), self.assertRaises(StopInputError):
                run(rows=rows)
        for value in (10**400, -(10**400)):
            with self.subTest(value="huge"), self.assertRaises(StopInputError):
                run(usage=value)
            with self.subTest(share="huge"), self.assertRaises(StopInputError):
                run(share=value)
        with self.assertRaises(StopInputError):
            Event("pass", BadRepr())  # type: ignore[arg-type]


class RaisingName(type):
    @property
    def __name__(self) -> str:  # type: ignore[override]
        raise RuntimeError("no name")


def rewriting_at(effect: object) -> datetime:
    """A recorded_at whose tzinfo (read by KpiRow.__post_init__) runs ``effect``."""
    class Rewriting(datetime):
        @property
        def tzinfo(self) -> timezone:  # type: ignore[override]
            effect()  # type: ignore[operator]
            return timezone.utc
    return Rewriting(2026, 11, 24)


def keyed(kind: type, name: str, effect: object, fields: dict[str, object]) -> object:
    """An item whose instance dict holds, first, a str-subclass key hashing like ``name``: reading
    ``name`` through getattr calls its __eq__, which runs ``effect`` once armed."""
    armed: list[bool] = []

    class Key(str):
        def __hash__(self) -> int:
            return hash(name)

        def __eq__(self, other: object) -> bool:
            if armed:
                effect()  # type: ignore[operator]
            return False

    item = object.__new__(kind)
    object.__setattr__(item, Key("x" + name), 0)
    for field, value in fields.items():
        object.__setattr__(item, field, value)
    armed.append(True)
    return item


class SnapshotTests(unittest.TestCase):
    """N6STOP1FIX3: every row and event is copied before any check runs foreign code."""

    def test_earlier_row_check_cannot_rewrite_a_later_row(self) -> None:
        incident = row(security=1)
        later = forged(row("S2"), recorded_at=rewriting_at(
            lambda: object.__setattr__(incident, "security_incidents", 0)))
        self.assertEqual(run(rows=[later, incident]), Verdict(STOP, n6_stop.SECURITY_INCIDENT))

    def test_row_check_cannot_rewrite_an_event(self) -> None:
        false_pass = Event("false_pass", "n6")
        later = forged(row(), recorded_at=rewriting_at(lambda: object.__setattr__(false_pass, "kind", "pass")))
        self.assertEqual(run(rows=[later], events=[false_pass]), Verdict(STOP, n6_stop.FALSE_PASS))

    def test_colliding_dict_key_refused(self) -> None:
        """getattr on such an item runs foreign code; the snapshot refuses the item instead."""
        incident = row(security=1)
        krow = keyed(KpiRow, "source", lambda: object.__setattr__(incident, "security_incidents", 0),
                     dict(vars(row("S2"))))
        with self.assertRaises(StopInputError):
            run(rows=[krow, incident])
        false_pass = Event("false_pass", "n6")
        kevent = keyed(Event, "kind", lambda: object.__setattr__(false_pass, "kind", "pass"),
                       {"kind": "pass", "source": "n6"})
        with self.assertRaises(StopInputError):
            run(events=[kevent, false_pass])

    def test_dict_subclass_instance_dict_refused(self) -> None:
        class Lying(dict[str, object]):
            def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
                return 0 if key == "security_incidents" else super().get(key, default)

        incident = row(security=1)
        object.__setattr__(incident, "__dict__", Lying(vars(incident)))
        with self.assertRaises(StopInputError):
            run(rows=[incident])

    def test_raising_type_name_gives_stop_input_error(self) -> None:
        class Num(float, metaclass=RaisingName):
            pass

        class Text(str, metaclass=RaisingName):
            pass

        class Thing(metaclass=RaisingName):
            pass

        class Boom(Exception, metaclass=RaisingName):
            pass

        def boom() -> None:
            raise Boom

        for usage, share in ((Num(50.0), 5.0), (50.0, Num(5.0))):
            with self.assertRaises(StopInputError):
                run(usage=usage, share=share)
        with self.assertRaises(StopInputError):
            Event("pass", Text("n6"))
        with self.assertRaises(StopInputError):
            run(rows=[Thing()])
        with self.assertRaises(StopInputError):
            run(rows=[forged(row(), recorded_at=rewriting_at(boom))])


if __name__ == "__main__":
    unittest.main()
