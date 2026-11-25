"""TALKRECOVER1: restart recovery from the log alone (pure, fail closed)."""

import ast
import copy
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis import talk_recover as tr
from tests import adversarial as adv

SOURCE = Path(__file__).resolve().parents[1] / "jarvis" / "talk_recover.py"
NOW = 1_800_000_000.0  # 08:00 UTC; frozen, the wall clock is never read
KW = {"threshold": 600, "window": 50}


def entry(id_, who="salih", kind="text", reply_to=None, **extra):
    return {"id": id_, "who": who, "kind": kind, "reply_to": reply_to, "text": "x", **extra}


def dlv(entry_id, view, state):
    return {"entry_id": entry_id, "view": view, "state": state}


def run(entries, deliveries=(), now=NOW, last_alive=NOW - 60, **kw):
    return tr.recover(list(entries), list(deliveries), now, last_alive, **{**KW, **kw})


class RaisingMapping(Mapping):
    def __init__(self, exc):
        self.exc = exc

    def __getitem__(self, key):
        raise self.exc

    def __iter__(self):
        raise self.exc

    def __len__(self):
        return 1

    def get(self, key, default=None):
        raise self.exc


class AcceptanceTests(unittest.TestCase):
    def test_1_unanswered_oldest_first_pending_deliveries_and_downtime(self):
        log = [entry(1), entry(2, "jarvis", reply_to=1), entry(3), entry(4, "system"), entry(5)]
        deliveries = [
            dlv(2, "telegram", "pending"), dlv(2, "telegram", "sent"),  # later record wins
            dlv(2, "ui", "pending"), dlv(2, "desk", "failed"), dlv(4, "ui", "unknown"),
        ]
        got = run(log, deliveries)
        self.assertIsInstance(got, tr.Recovery)
        self.assertEqual(got.brain_queue, (3, 5))
        self.assertEqual(got.pending_deliveries, ((2, "ui", "pending"), (4, "ui", "unknown")))
        self.assertEqual(got.downtime, 60.0)
        self.assertEqual((got.duplicates, got.orphans), ((), ()))

    def test_2_note_only_over_threshold(self):
        got = run([entry(1)], last_alive=NOW - 3600)
        self.assertEqual(got.note, "07:00'den beri kapalıydım")
        got = run([entry(1)], last_alive=NOW - 3600, hhmm=lambda s: "21:15")
        self.assertEqual(got.note, "21:15'den beri kapalıydım")
        for gap in (0, 599, 600):
            got = run([entry(1)], last_alive=NOW - gap)
            self.assertIsNone(got.note, gap)
            self.assertEqual(got.downtime, float(gap))

    def test_2_bad_clock_formatter_refused(self):
        self.assertEqual(run([], last_alive=0, hhmm=lambda s: "7:0").code, "bad_clock")
        self.assertEqual(run([], last_alive=0, hhmm=lambda s: 1 / 0).code, "bad_input")

    def test_3_replied_entry_never_requeued_even_if_delivery_failed(self):
        for state in ("failed", "gave_up", "pending", "unknown"):
            got = run([entry(1), entry(2, "jarvis", reply_to=1)], [dlv(2, "telegram", state)])
            self.assertEqual(got.brain_queue, (), state)

    def test_4_window_bounds_scan_and_counts_older(self):
        log = [entry(1), entry(2), entry(3, "jarvis", reply_to=2), entry(4), entry(5)]
        got = run(log, [dlv(3, "ui", "pending"), dlv(1, "ui", "unknown")], window=2)
        self.assertEqual(got.brain_queue, (4, 5))
        self.assertEqual(got.older_unanswered, 1)
        self.assertEqual(got.pending_deliveries, ())
        self.assertEqual(got.older_undelivered, 2)

    def test_5_deterministic_and_input_untouched(self):
        log = [entry(1), entry(2, "jarvis", reply_to=1), entry(3, "jarvis", reply_to=1), entry(4)]
        deliveries = [dlv(2, "ui", "pending"), dlv(4, "ui", "unknown")]
        before = copy.deepcopy((log, deliveries))
        first = run(log, deliveries, last_alive=NOW - 7200)
        self.assertEqual(first, run(log, deliveries, last_alive=NOW - 7200))
        self.assertEqual((log, deliveries), before)


class NegativeTests(unittest.TestCase):
    def test_1_last_alive_missing_future_or_nan_is_unknown(self):
        def check(value):
            got = run([entry(1)], last_alive=value)
            self.assertEqual(got.downtime, tr.UNKNOWN)
            self.assertIsNone(got.note)

        hostile = [v for v in adv.HOSTILE_NUMBERS if not (type(v) is float and v == 0)]
        adv.each(self, hostile + [NOW + 1], check)
        got = run([entry(1)], last_alive=-0.0)  # finite: real downtime, never negative
        self.assertEqual(got.downtime, NOW)

    def test_2_second_reply_is_duplicate_and_not_delivered(self):
        log = [entry(1), entry(2, "jarvis", reply_to=1), entry(3, "jarvis", reply_to=1)]
        got = run(log, [dlv(2, "telegram", "sent"), dlv(3, "telegram", "pending")])
        self.assertEqual(got.duplicates, (3,))
        self.assertEqual(got.pending_deliveries, ())
        self.assertEqual(got.brain_queue, ())

    def test_3_reply_to_missing_entry_is_orphan(self):
        log = [entry(1), entry(2, "jarvis", reply_to=99), entry(3, "jarvis", reply_to=4), entry(4)]
        got = run(log, [dlv(2, "ui", "pending")])
        self.assertEqual(got.orphans, (2, 3))
        self.assertEqual(got.brain_queue, (1, 4))
        self.assertEqual(got.pending_deliveries, ())

    def test_4_approval_tap_and_superseded_never_queued(self):
        log = [entry(1, kind="approval_tap"), entry(2, superseded=True), entry(3)]
        got = run(log)
        self.assertEqual(got.brain_queue, (3,))
        self.assertEqual(run(log, window=1).older_unanswered, 0)

    def test_5_ids_out_of_order_or_duplicated_refused(self):
        for ids in ((2, 1), (1, 1), (-1,)):
            self.assertEqual(run([entry(i) for i in ids]).code, "bad_id_order", ids)
        hostile = [v for v in adv.HOSTILE_NUMBERS if type(v) is not int]
        adv.each(self, hostile, lambda v: self.assertEqual(run([entry(v)]).code, "bad_id_order"))

    def test_hostile_now_threshold_window_refused(self):
        hostile = [v for v in adv.HOSTILE_NUMBERS if not (type(v) is float and v == 0)]
        adv.each(self, hostile, lambda v: self.assertEqual(run([], now=v).code, "bad_now"))
        for value in (-1, float("nan"), True, "1", None):
            self.assertEqual(run([], threshold=value).code, "bad_threshold")
        for value in (0, 1.0, True, None):
            self.assertEqual(run([], window=value).code, "bad_window")

    def test_hostile_text_fields(self):
        def check(text):
            self.assertEqual(run([entry(1, who=text)]).code, "bad_entry")
            got = run([entry(1, kind=text)], [dlv(1, text, "pending")])
            self.assertEqual(got.pending_deliveries, ((1, text, "pending"),))
            self.assertEqual(run([entry(1)], [dlv(1, "ui", text)]).code, "bad_delivery")

        adv.each(self, adv.HOSTILE_TEXT, check)

    def test_bad_shapes_refused(self):
        for value in (None, "x", {"id": 1}, 5):
            self.assertEqual(tr.recover(value, [], NOW, NOW, **KW).code, "bad_entries")
            self.assertEqual(tr.recover([], value, NOW, NOW, **KW).code, "bad_deliveries")
        self.assertEqual(run([entry(1)], [dlv(7, "ui", "pending")]).code, "unknown_delivery_entry")
        self.assertEqual(run([entry(1, superseded=1)]).code, "bad_entry")
        self.assertEqual(run([entry(1, reply_to="1")]).code, "bad_entry")


class NoEscapeTests(unittest.TestCase):
    def test_raising_and_deep_inputs_never_escape(self):
        deep = []
        for _ in range(5000):
            deep = [deep]
        for exc in (RecursionError(), ValueError(), TypeError()):
            self.assertEqual(run([RaisingMapping(exc)]).code, "bad_input")
            self.assertEqual(run([entry(1)], [RaisingMapping(exc)]).code, "bad_input")
        self.assertEqual(run(deep).code, "bad_entries")
        self.assertEqual(run([entry(1, text=adv.deep_json())]).brain_queue, (1,))


class PurityTests(unittest.TestCase):
    BANNED = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "requests",
              "shutil", "sqlite3", "importlib", "ctypes", "tempfile", "jarvis", "telegram"}

    def test_no_io_network_or_sibling_imports(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names = [node.module or ""]
            for name in names:
                self.assertNotIn(name.split(".")[0], self.BANNED, name)
                self.assertFalse(name.split(".")[-1].startswith(("talk_", "telegram_")), name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "eval", "exec"})

    def test_refusal_texts_ascii_under_cp1252_strict(self):
        with adv.narrow_stdout():
            for code, text in tr.REFUSALS.items():
                self.assertTrue(code.isascii() and text.isascii() and text.isprintable(), code)
                refusal = tr._refuse(code)
                print(refusal.code, refusal.text)


if __name__ == "__main__":
    unittest.main()
