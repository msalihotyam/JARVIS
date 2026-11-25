import ast
import itertools
import unittest
from pathlib import Path

import jarvis.talk_status as ts
from jarvis.talk_status import (
    RING_STATES, UNKNOWN, Heartbeat, Refusal, heartbeat, last_alive, needs_you,
    parse_heartbeat, ring_state,
)
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_TEXT, deep_json, each

NOW = 1_764_000_000
BOOT = "boot-a1"
FLAGS = RING_STATES[:-1]


def entry(eid, who, flag=False):
    return {"id": eid, "who": who, "needs_you": flag}


def refused(case, value, reason=None):
    case.assertIs(type(value), Refusal)
    case.assertTrue(value.message.isascii())
    if reason:
        case.assertEqual(value.reason, reason)


class AcceptanceTests(unittest.TestCase):
    def test_each_single_flag_and_idle(self):
        self.assertEqual(ring_state({}), "idle")
        self.assertEqual(ring_state({f: False for f in FLAGS}), "idle")
        for flag in FLAGS:
            self.assertEqual(ring_state({flag: True}), flag)

    def test_precedence_order(self):
        self.assertEqual(RING_STATES, ("needs_you", "speaking", "thinking",
                                       "understanding", "listening", "idle"))

    def test_open_card_holds(self):
        self.assertIs(needs_you([{"resolved": False}], [], NOW), True)
        self.assertIs(needs_you([{"resolved": False, "expires_at": NOW + 1}], [], NOW), True)

    def test_unanswered_jarvis_entry_holds_until_salih(self):
        log = [entry(1, "salih"), entry(2, "jarvis", True)]
        self.assertIs(needs_you([], log, NOW), True)
        self.assertIs(needs_you([], log + [entry(3, "system")], NOW), True)
        self.assertIs(needs_you([], log + [entry(3, "jarvis")], NOW), True)
        self.assertIs(needs_you([], log + [entry(3, "salih")], NOW), False)
        self.assertIs(needs_you([], [entry(1, "jarvis"), entry(2, "salih")], NOW), False)
        self.assertIs(needs_you([], [], NOW), False)

    def test_heartbeat_round_trip(self):
        text = heartbeat(NOW, BOOT)
        self.assertEqual(text, f"hb1 {NOW} {BOOT}")
        self.assertLessEqual(len(text), ts.MAX_RECORD_CHARS)
        self.assertEqual(parse_heartbeat(text), Heartbeat(NOW, BOOT))
        self.assertEqual(parse_heartbeat(heartbeat(0, "x")), Heartbeat(0, "x"))

    def test_last_alive_is_last_record(self):
        records = [heartbeat(NOW - 60, BOOT), heartbeat(NOW - 30, BOOT)]
        self.assertEqual(last_alive(records, NOW, BOOT), NOW - 30)
        self.assertEqual(last_alive(tuple(records), NOW - 30, BOOT), NOW - 30)
        self.assertEqual(last_alive([], NOW, BOOT), UNKNOWN)

    def test_deterministic(self):
        for _ in range(3):
            self.assertEqual(ring_state({"thinking": True, "listening": True}), "thinking")
            self.assertEqual(heartbeat(NOW, BOOT), f"hb1 {NOW} {BOOT}")
            self.assertIs(needs_you([], [entry(2, "jarvis", True)], NOW), True)


class NegativeTests(unittest.TestCase):
    def test_contradictory_flags_precedence(self):
        for n in range(2, len(FLAGS) + 1):
            for combo in itertools.combinations(FLAGS, n):
                with self.subTest(combo=combo):
                    winner = min(combo, key=RING_STATES.index)
                    self.assertEqual(ring_state(dict.fromkeys(combo, True)), winner)

    def test_expired_or_resolved_card_releases(self):
        self.assertIs(needs_you([{"resolved": True}], [], NOW), False)
        self.assertIs(needs_you([{"resolved": False, "expires_at": NOW}], [], NOW), False)
        self.assertIs(needs_you([{"resolved": False, "expires_at": NOW - 1}], [], NOW), False)
        self.assertIs(needs_you([{"resolved": True, "expires_at": NOW + 99}], [], NOW), False)

    def test_corrupt_future_or_foreign_heartbeat_unknown(self):
        good = heartbeat(NOW - 10, BOOT)
        bad_rows = ["", "hb1", f"hb2 {NOW} {BOOT}", f"hb1 {NOW} {BOOT} ", f"hb1  {NOW} {BOOT}",
                    f"hb1 0{NOW} {BOOT}", f"hb1 +{NOW} {BOOT}", f"hb1 -{NOW} {BOOT}",
                    f"hb1 {NOW}.0 {BOOT}", f"hb1 {NOW} {BOOT}\n", f"hb1 {NOW}",
                    f"hb1 ١٢ {BOOT}", f"hb1 {NOW} bоot", f"hb1 {NOW} " + "b" * 65,
                    f"hb1 {NOW + 1} {BOOT}", heartbeat(NOW - 5, "boot-b"),
                    f"hb1 {10**11} {BOOT}"] + [s for s in HOSTILE_TEXT if s.isascii()][:8]
        each(self, bad_rows, lambda row: self.assertEqual(last_alive([good, row], NOW, BOOT), UNKNOWN))
        each(self, list(HOSTILE_TEXT) + list(HOSTILE_JSON) + [deep_json()],
             lambda row: self.assertEqual(last_alive([good, row], NOW, BOOT), UNKNOWN))
        each(self, list(HOSTILE_TEXT), lambda row: refused(self, parse_heartbeat(row), "bad_record"))

    def test_entries_out_of_time_order_sequence_decides(self):
        late_ts = [{"id": 5, "who": "salih", "ts": "2026-01-01T00:00:00+00:00"},
                   {"id": 9, "who": "jarvis", "needs_you": True, "ts": "2020-01-01T00:00:00+00:00"}]
        self.assertIs(needs_you([], late_ts, NOW), True)
        self.assertIs(needs_you([], list(reversed(late_ts)), NOW), True)
        answered = [entry(7, "salih"), entry(3, "jarvis", True)]
        self.assertIs(needs_you([], answered, NOW), False)
        refused(self, needs_you([], [entry(3, "salih"), entry(3, "jarvis", True)], NOW), "bad_order")

    def test_wrong_types_refused(self):
        bad_flags = [None, [], "thinking", 1, {"thinking": 1}, {"thinking": None},
                     {"talking": True}, {1: True}, {"idle": True}, {"thinking": "yes"}]
        each(self, bad_flags, lambda f: refused(self, ring_state(f), "bad_flags"))
        each(self, [n for n in HOSTILE_NUMBERS if type(n) is not int or n > ts.MAX_TIME] + [-1, 1.0],
             lambda n: (refused(self, heartbeat(n, BOOT), "bad_time"),
                        refused(self, needs_you([], [], n), "bad_time"),
                        refused(self, last_alive([], n, BOOT), "bad_time")))
        each(self, [None, "", 5, "a b", "a" * 65, "bоot"] + list(HOSTILE_TEXT),
             lambda b: (refused(self, heartbeat(NOW, b), "bad_boot"),
                        refused(self, last_alive([], NOW, b), "bad_boot")))
        each(self, [None, "x", {}, iter([]), [1], [b"hb1 1 a"]],
             lambda r: refused(self, last_alive(r, NOW, BOOT), "bad_records"))
        each(self, [None, {}, [None], [{"resolved": 0}], [{"resolved": False, "expires_at": "1"}],
                    [{"resolved": False, "expires_at": True}], [{1: False}]],
             lambda c: refused(self, needs_you(c, [], NOW), "bad_cards"))
        each(self, [None, [None], [{"id": True, "who": "salih"}], [{"id": "1", "who": "salih"}],
                    [{"id": -1, "who": "salih"}], [{"id": 1, "who": "SALIH"}], [{"id": 1}],
                    [entry(1, "salih", True)], [{"id": 1, "who": "jarvis", "needs_you": 1}]],
             lambda e: refused(self, needs_you([], e, NOW), "bad_entries"))
        each(self, [None, 5, b"hb1 1 a", "x" * 100_000], lambda t: refused(self, parse_heartbeat(t), "bad_record"))

    def test_no_exception_escapes(self):
        class Boom(dict):
            def items(self):
                raise RecursionError

        refused(self, ring_state(Boom()), "internal")
        refused(self, needs_you([Boom()], [], NOW), "internal")
        refused(self, needs_you([], [Boom()], NOW), "internal")


class StaticTests(unittest.TestCase):
    def test_pure_module(self):
        tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "exec", "eval", "compile", "__import__"})
        self.assertLessEqual(imported, {"__future__", "re", "collections.abc", "dataclasses"})

    def test_reason_texts_ascii(self):
        for reason, text in ts.REASONS.items():
            self.assertTrue(text.isascii(), reason)

    def test_running_app_does_not_import(self):
        root = Path(ts.__file__).parent
        for path in root.rglob("*.py"):
            if path.name != "talk_status.py":
                self.assertNotIn("talk_status", path.read_text(encoding="utf-8", errors="replace"),
                                 str(path))


if __name__ == "__main__":
    unittest.main()
