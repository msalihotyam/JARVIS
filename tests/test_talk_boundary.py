"""TALKCORE1: the single entry boundary never raises and passes only exact shapes."""

import dataclasses
import math
import unittest
from types import SimpleNamespace

from jarvis import talk_boundary as tb
from tests import adversarial


class SubInt(int):
    pass


class BadStr(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("eq")


BIG = 10**5000 - 1  # 5000 digits; str() of it raises ValueError
# label -> value; labels stay short so a 5000-digit int never reaches a message
HOSTILE = {
    "wrong_type": 3.5, "bool": True, "int_sub": SubInt(3), "str_sub": BadStr("text"),
    "huge": 10**400, "digits5000": BIG, "negative": -1, "nan": math.nan, "none": None,
    "nested": [["a"]], "nul": "a\x00b", "oversize": "a" * (tb.REPLY_CAP + 1),
}
GOOD_ITEM = {"id": "a1", "kind": "text", "text": "merhaba"}
GOOD_LINE = {"key": "in:pc:a1", "kind": "text", "text": "merhaba", "time": 5}


def raw(**fns):
    return SimpleNamespace(**fns)


def boom(*a):
    raise OSError("port")


class InboxShapeTests(unittest.TestCase):
    def test_every_item_field_times_every_hostile_value(self):
        for field in GOOD_ITEM:
            for label, value in HOSTILE.items():
                with self.subTest(field=field, value=label):
                    item = dict(GOOD_ITEM, **{field: value})
                    got = tb.PcInbox(raw(take=lambda n, i=item: [i])).take(4)
                    self.assertTrue(got[0].refused)
                    self.assertEqual(got[0].handle, 0)

    def test_hostile_whole_items_are_refused_one_by_one(self):
        items = list(HOSTILE.values()) + [{BadStr("id"): "a1"}, {"id": "a1"}, GOOD_ITEM]
        got = tb.PcInbox(raw(take=lambda n: items)).take(100)
        self.assertEqual([g.refused for g in got], [True] * (len(items) - 1) + [False])
        self.assertTrue(all(g.item_id for g in got))

    def test_adversarial_texts_never_raise(self):
        inbox = tb.PcInbox(raw(take=lambda n: [dict(GOOD_ITEM, text=v) for v in adversarial.HOSTILE_TEXT]))
        self.assertEqual(len(inbox.take(1000)), len(adversarial.HOSTILE_TEXT))

    def test_take_shapes_and_faults(self):
        for label, port in (("tuple", lambda n: (GOOD_ITEM,)), ("raise", boom), ("none", lambda n: None),
                            ("list_sub", lambda n: type("L", (list,), {})([GOOD_ITEM]))):
            with self.subTest(label):
                self.assertIs(tb.PcInbox(raw(take=port)).take(4), tb.UNKNOWN)

    def test_take_is_capped_to_limit(self):
        self.assertEqual(len(tb.PcInbox(raw(take=lambda n: [GOOD_ITEM] * 9)).take(2)), 2)

    def test_ack_by_handle_only(self):
        acked = []
        inbox = tb.PcInbox(raw(take=lambda n: [GOOD_ITEM, {"x": 1}], ack=acked.append))
        first, second = inbox.take(4)
        self.assertIs(inbox.ack(first.handle), tb.OK)
        self.assertIs(inbox.ack(second.handle), tb.OK)
        self.assertEqual(acked[0], "a1")
        self.assertEqual(acked[1], {"x": 1})
        for bad in (first.handle, True, 99, "0", None):
            self.assertTrue(inbox.ack(bad).refused)
        inbox = tb.PcInbox(raw(take=lambda n: [GOOD_ITEM], ack=boom))
        self.assertIs(inbox.ack(inbox.take(1)[0].handle), tb.UNKNOWN)


class LogShapeTests(unittest.TestCase):
    def test_every_line_field_times_every_hostile_value(self):
        for field in GOOD_LINE:
            for label, value in HOSTILE.items():
                if field == "text" and label == "oversize":
                    value = "a" * (tb.REPLY_CAP + 1)
                with self.subTest(field=field, value=label):
                    line = dict(GOOD_LINE, **{field: value})
                    log = tb.KeyedLog(raw(get=lambda k, x=line: x, read=lambda x=line: [x]))
                    self.assertIs(log.get("in:pc:a1"), tb.UNKNOWN)
                    self.assertEqual(log.read(), ())

    def test_good_line_round_trip_and_wrong_key(self):
        log = tb.KeyedLog(raw(get=lambda k: dict(GOOD_LINE), read=lambda: [GOOD_LINE, 5, {}]))
        self.assertEqual(log.get("in:pc:a1"), tb.LogLine("in:pc:a1", "text", "merhaba", 5))
        self.assertIs(log.get("reply:a1"), tb.UNKNOWN)
        self.assertEqual(len(log.read()), 1)

    def test_extra_field_and_kind_mismatch_are_dropped(self):
        for line in (dict(GOOD_LINE, extra=1), dict(GOOD_LINE, kind="reply"),
                     dict(GOOD_LINE, key="reply:a1", kind="text"), dict(GOOD_LINE, key="out:a1")):
            self.assertEqual(tb.KeyedLog(raw(read=lambda x=line: [x])).read(), ())

    def test_get_and_read_faults(self):
        for label, value in (("unknown", "unknown"), ("other_str", "gone"), ("bytes", b"absent"),
                             ("tuple", (GOOD_LINE,)), ("none", None)):
            with self.subTest(label):
                log = tb.KeyedLog(raw(get=lambda k, v=value: v, read=lambda v=value: v))
                self.assertIs(log.get("in:pc:a1"), tb.UNKNOWN)
                self.assertIs(log.read(), tb.UNKNOWN)
        self.assertIs(tb.KeyedLog(raw(get=lambda k: "absent")).get("in:pc:a1"), tb.ABSENT)
        self.assertIs(tb.KeyedLog(raw(get=lambda k: BadStr("absent"))).get("k"), tb.UNKNOWN)
        self.assertIs(tb.KeyedLog(raw(get=boom, read=boom)).read(), tb.UNKNOWN)

    def test_append_results(self):
        line = tb.LogLine("in:pc:a1", "text", "x", 1)
        for value, want in (("ok", tb.OK), ("exists", tb.EXISTS), ("failed", tb.FAILED),
                            ("unknown", tb.UNKNOWN), ("OK", tb.UNKNOWN), (True, tb.UNKNOWN),
                            (BadStr("ok"), tb.UNKNOWN), (None, tb.UNKNOWN), (["ok"], tb.UNKNOWN)):
            with self.subTest(repr(value)[:20]):
                self.assertIs(tb.KeyedLog(raw(append=lambda k, d, v=value: v)).append(line), want)
        self.assertIs(tb.KeyedLog(raw(append=boom)).append(line), tb.UNKNOWN)


class BrainViewTests(unittest.TestCase):
    def test_bad_replies_are_refused(self):
        for label, value in list(HOSTILE.items()) + [("empty", ""), ("blank", " \n\t")]:
            with self.subTest(label):
                got = tb.Brain(raw(ask=lambda t, v=value: v)).ask("soru")
                self.assertTrue(got.refused)
        for text in adversarial.HOSTILE_TEXT:
            got = tb.Brain(raw(ask=lambda t, v=text: v)).ask("soru")
            self.assertIn(type(got), (tb.Answer, tb.Refused))

    def test_good_reply_and_raise(self):
        self.assertEqual(tb.Brain(raw(ask=lambda t: "cevap: " + t)).ask("x"), tb.Answer("cevap: x"))
        self.assertIs(tb.Brain(raw(ask=boom)).ask("x"), tb.UNKNOWN)

    def test_view_publish(self):
        shown = []
        self.assertIs(tb.View(raw(show=shown.append)).publish({"turns": []}), tb.OK)
        self.assertEqual(shown, [{"turns": []}])
        self.assertIs(tb.View(raw(show=boom)).publish({}), tb.UNKNOWN)


class ClockTests(unittest.TestCase):
    def test_seconds_become_milliseconds(self):
        self.assertEqual(tb.Clock(raw(now=lambda: 1.5)).now(), tb.Tick(1500))
        self.assertEqual(tb.Clock(raw(now=lambda: 2)).now(), tb.Tick(2000))

    def test_hostile_values_are_unknown(self):
        values = dict(HOSTILE, inf=math.inf, neg_float=-0.5, big_float=1e300, text="1")
        for label, value in values.items():
            if label in ("wrong_type",):
                continue  # 3.5 is a good float
            with self.subTest(label):
                self.assertIs(tb.Clock(raw(now=lambda v=value: v)).now(), tb.UNKNOWN)
        self.assertIs(tb.Clock(raw(now=boom)).now(), tb.UNKNOWN)

    def test_never_backwards(self):
        times = iter([5, 4, 5, 6])
        clock = tb.Clock(raw(now=lambda: next(times)))
        self.assertEqual([clock.now() for _ in range(4)],
                         [tb.Tick(5000), tb.UNKNOWN, tb.Tick(5000), tb.Tick(6000)])


class ConfigTests(unittest.TestCase):
    def test_bad_values_are_refused(self):
        for label, value in list(HOSTILE.items()) + [("zero", 0), ("over", tb.MAX_PC_CAP + 1)]:
            with self.subTest(max_pc=label):
                self.assertEqual(tb.config(value, 100), tb.Refused("bad_max_pc"))
        for label, value in list(HOSTILE.items()) + [("low", 15), ("over", tb.BUDGET_CAP + 1)]:
            with self.subTest(budget=label):
                self.assertEqual(tb.config(4, value), tb.Refused("bad_budget"))

    def test_good_config(self):
        self.assertEqual(tb.config(4, 16), tb.Config(4, 16))
        self.assertEqual(tb.config(tb.MAX_PC_CAP, tb.BUDGET_CAP), tb.Config(tb.MAX_PC_CAP, tb.BUDGET_CAP))


class RecordTests(unittest.TestCase):
    def test_records_are_frozen(self):
        for record in (tb.Refused("x"), tb.PcItem("a", "text", "t", 0), tb.LogLine("k", "text", "t", 0),
                       tb.Answer("a"), tb.Tick(1), tb.Config(1, 10)):
            with self.subTest(type(record).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    record.text = "y"

    def test_wrap_builds_ports(self):
        ports = tb.wrap(*(raw() for _ in range(5)))
        self.assertEqual([type(getattr(ports, f.name)) for f in dataclasses.fields(ports)],
                         [tb.PcInbox, tb.KeyedLog, tb.Brain, tb.View, tb.Clock])
        for mark in (ports.inbox.take(1), ports.log.read(), ports.brain.ask("x"), ports.clock.now()):
            self.assertIs(mark, tb.UNKNOWN)
        self.assertIs(ports.view.publish({}), tb.UNKNOWN)
