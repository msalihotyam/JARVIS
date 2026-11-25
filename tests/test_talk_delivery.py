import ast
import io
import itertools
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

from jarvis.talk_delivery import plan_delivery

NOW = 100_000.0


def rec(view="pc", state="pending", attempts=0, last_try=None, seq=1):
    return {"view": view, "state": state, "attempts": attempts, "last_try": last_try, "seq": seq}


def req(entries, **extra):
    return {"now": NOW, "entries": entries, **extra}


def pairs(result):
    return [(a["id"], a["view"]) for a in result["actions"]]


class Raiser(Mapping):
    """Hostile mapping whose lookups blow the stack."""

    def __getitem__(self, key):
        raise RecursionError

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def get(self, key, default=None):
        raise RecursionError


class TalkDeliveryTests(unittest.TestCase):
    # Acceptance 1
    def test_pending_entries_yield_ordered_actions(self):
        out = plan_delivery(req([
            {"id": "e1", "records": [rec("telegram"), rec("pc")]},
            {"id": "e2", "records": [rec("pc")]},
        ]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["actions"], [
            {"id": "e1", "view": "pc", "not_before": NOW},
            {"id": "e1", "view": "telegram", "not_before": NOW},
            {"id": "e2", "view": "pc", "not_before": NOW},
        ])
        self.assertFalse(out["more"])

    # Acceptance 2
    def test_backoff_doubles_and_caps(self):
        params = {"base": 10.0, "cap": 50.0, "max_attempts": 9}
        got = []
        for attempts in (1, 2, 3, 4, 8):
            out = plan_delivery(req([{"id": "e", "records": [rec(state="failed", attempts=attempts, last_try=NOW)]}], **params))
            got.append(out["actions"][0]["not_before"] - NOW)
        self.assertEqual(got, [10.0, 20.0, 40.0, 50.0, 50.0])

    def test_max_attempts_gives_up_and_reports_once(self):
        first = plan_delivery(req([{"id": "e", "records": [rec(state="failed", attempts=5, last_try=NOW - 1)]}]))
        self.assertEqual(first["actions"], [])
        self.assertEqual(first["gave_up"], [{"id": "e", "view": "pc"}])
        # The caller stored the gave_up state; the next plan stays silent.
        second = plan_delivery(req([{"id": "e", "records": [
            rec(state="failed", attempts=5, last_try=NOW - 1, seq=1), rec(state="gave_up", attempts=5, last_try=NOW - 1, seq=2)]}]))
        self.assertEqual((second["actions"], second["gave_up"]), ([], []))

    # Acceptance 3
    def test_offline_view_backlog_keeps_original_order(self):
        hours_ago = NOW - 6 * 3600
        entries = [{"id": f"m{i:03d}", "records": [rec("pc", "sent", 1, hours_ago),
                                                    rec("telegram", "failed", 3, hours_ago + i)]} for i in range(50)]
        out = plan_delivery(req(entries))
        self.assertEqual(pairs(out), [(f"m{i:03d}", "telegram") for i in range(50)])
        self.assertTrue(all(a["not_before"] == NOW for a in out["actions"]))

    # Acceptance 4
    def test_sent_record_never_yields_action(self):
        for attempts in (0, 1, 7):
            out = plan_delivery(req([{"id": "e", "records": [rec(state="sent", attempts=attempts, last_try=NOW if attempts else None)]}],
                                    reset=[{"id": "e", "view": "pc"}]))
            self.assertEqual((out["actions"], out["gave_up"]), ([], []))

    # Acceptance 5
    def test_deterministic(self):
        request = req([{"id": f"e{i}", "records": [rec("pc", "failed", i % 7, NOW - i)]} for i in range(200)])
        self.assertEqual(plan_delivery(request), plan_delivery(request))

    # Negative 1
    def test_latest_sequence_wins_never_two_sends(self):
        out = plan_delivery(req([{"id": "e", "records": [
            rec(state="pending", seq=2), rec(state="failed", attempts=1, last_try=NOW - 5, seq=1), rec(state="pending", seq=2)]}]))
        self.assertEqual(out["actions"], [{"id": "e", "view": "pc", "not_before": NOW}])
        out = plan_delivery(req([{"id": "e", "records": [rec(state="sent", attempts=1, last_try=NOW, seq=3), rec(seq=2)]}]))
        self.assertEqual(out["actions"], [])

    def test_same_sequence_disagreeing_is_refused(self):
        out = plan_delivery(req([{"id": "e", "records": [rec(seq=4), rec(state="sent", seq=4)]}, {"id": "ok", "records": [rec()]}]))
        self.assertEqual(out["refused"], [{"index": 0, "reason": "conflict"}])
        self.assertEqual(pairs(out), [("ok", "pc")])

    def test_conflict_check_does_not_depend_on_record_order(self):
        # Disagreement below the max seq is history, not a conflict, in every order.
        clean = [rec(seq=2), rec(state="sent", attempts=1, last_try=NOW, seq=2), rec(state="sent", attempts=1, last_try=NOW, seq=3)]
        conflicts = {
            "state": [rec(seq=4), rec(state="sent", seq=4), rec(state="failed", attempts=1, last_try=NOW - 1, seq=1)],
            "attempts": [rec(state="failed", attempts=1, last_try=NOW - 1, seq=4), rec(state="failed", attempts=2, last_try=NOW - 1, seq=4),
                         rec(seq=2)],
            "last_try": [rec(state="failed", attempts=1, last_try=NOW - 1, seq=4), rec(state="failed", attempts=1, last_try=NOW - 2, seq=4),
                         rec(seq=3)],
        }
        for order in itertools.permutations(clean):
            out = plan_delivery(req([{"id": "e", "records": list(order)}]))
            self.assertEqual((out["actions"], out["refused"]), ([], []), order)
        for name, records in conflicts.items():
            for order in itertools.permutations(records):
                out = plan_delivery(req([{"id": "e", "records": list(order)}, {"id": "ok", "records": [rec()]}]))
                self.assertEqual(out["refused"], [{"index": 0, "reason": "conflict"}], name)
                self.assertEqual(pairs(out), [("ok", "pc")], name)

    def test_conflict_is_per_view_and_identical_duplicates_agree(self):
        # A pc disagreement at an old seq does not refuse; telegram's own max seq decides telegram.
        records = [rec("pc", seq=1), rec("pc", "sent", seq=1), rec("pc", seq=2), rec("telegram", seq=1), rec("telegram", seq=1)]
        for order in itertools.permutations(records):
            out = plan_delivery(req([{"id": "e", "records": list(order)}]))
            self.assertEqual(pairs(out), [("e", "pc"), ("e", "telegram")], order)

    def test_duplicate_entry_id_is_refused(self):
        out = plan_delivery(req([{"id": "e", "records": [rec()]}, {"id": "e", "records": [rec()]}]))
        self.assertEqual(pairs(out), [("e", "pc")])
        self.assertEqual(out["refused"], [{"index": 1, "reason": "duplicate_id"}])

    # Negative 2
    def test_bad_entry_is_refused_others_proceed(self):
        bad = {
            "future_last_try": rec(state="failed", attempts=1, last_try=NOW + 1),
            "bad_attempts": rec(attempts=-1),
            "bad_time": rec(state="failed", attempts=1, last_try=float("nan")),
        }
        entries = [{"id": "good0", "records": [rec()]}]
        entries += [{"id": k, "records": [r]} for k, r in bad.items()]
        entries += [{"id": "good1", "records": [rec()]}]
        out = plan_delivery(req(entries))
        self.assertEqual(pairs(out), [("good0", "pc"), ("good1", "pc")])
        self.assertEqual([r["reason"] for r in out["refused"]], list(bad))

    def test_huge_int_time_is_refused_per_entry(self):
        # json.loads yields ints beyond float range; one such record must not halt the whole plan.
        entries = [
            {"id": "big", "records": [rec(state="failed", attempts=1, last_try=10**400)]},
            {"id": "neg", "records": [rec(state="failed", attempts=1, last_try=-10**400)]},
            {"id": "mixed", "records": [rec("telegram"), rec("pc", "failed", 1, 10**400)]},
            {"id": "good", "records": [rec()]},
            {"id": "old", "records": [rec(state="failed", attempts=1, last_try=-10**300)]},
            {"id": "many", "records": [rec(state="failed", attempts=10**400, last_try=NOW - 1, seq=10**400)]},
        ]
        out = plan_delivery(req(entries))
        self.assertTrue(out["ok"])
        self.assertEqual(out["refused"], [{"index": 0, "reason": "bad_time"}, {"index": 1, "reason": "bad_time"},
                                          {"index": 2, "reason": "bad_time"}])
        self.assertEqual(out["actions"], [{"id": "good", "view": "pc", "not_before": NOW}, {"id": "old", "view": "pc", "not_before": NOW}])
        self.assertEqual(out["gave_up"], [{"id": "many", "view": "pc"}])

    def test_huge_int_params_refused_with_typed_reason(self):
        entries = [{"id": "e", "records": [rec()]}]
        for key in ("now", "base", "cap"):
            for value in (10**400, -10**400):
                out = plan_delivery({**req(entries), key: value})
                self.assertEqual(out, {"ok": False, "reason": "bad_" + key}, (key, value > 0))

    def test_overflowing_send_time_refused_per_entry(self):
        # last_try + delay past float max would plan an "inf" send time.
        big = 1.7e308
        out = plan_delivery({"now": big, "base": 1e308, "cap": 1e308, "entries": [
            {"id": "inf", "records": [rec("telegram"), rec("pc", "failed", 1, big)]},
            {"id": "int", "records": [rec("pc", "failed", 2, 10**308)]},
            {"id": "ok", "records": [rec()]},
        ]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["refused"], [{"index": 0, "reason": "bad_time"}, {"index": 1, "reason": "bad_time"}])
        self.assertEqual(out["actions"], [{"id": "ok", "view": "pc", "not_before": big}])

    def test_all_int_overflow_refused_per_entry_sibling_planned(self):
        # Int arithmetic past float max must not drop the whole plan.
        big = 10**308
        for attempts in (1, 2, 3):
            for params in ({"base": big, "cap": big}, {"base": big, "cap": 1.0e308}, {"base": 9 * 10**307, "cap": 1.0e308}):
                out = plan_delivery({"now": big, **params, "entries": [
                    {"id": "x", "records": [rec("pc", "failed", attempts, big)]},
                    {"id": "ok", "records": [rec()]},
                ]})
                self.assertTrue(out["ok"], (attempts, params, out))
                self.assertEqual(out["refused"], [{"index": 0, "reason": "bad_time"}], (attempts, params))
                self.assertEqual(pairs(out), [("ok", "pc")])

    def test_backoff_keeps_doubling_past_two_to_the_64_until_cap(self):
        out = plan_delivery({"now": 0, "base": 1, "cap": 1e30, "max_attempts": 100,
                             "entries": [{"id": "e", "records": [rec("pc", "failed", 66, 0)]}]})
        self.assertEqual(out["actions"][0]["not_before"], 2.0**65)
        tiny = plan_delivery({"now": 0, "base": 5e-324, "cap": 1e308, "max_attempts": 10**400,
                              "entries": [{"id": "e", "records": [rec("pc", "failed", 10**399, 0)]}]})
        self.assertEqual(tiny["actions"][0]["not_before"], 1e308)

    def test_record_missing_last_try_is_refused(self):
        for attempts in (0, 1):
            raw = rec("pc", "pending", attempts)
            del raw["last_try"]
            out = plan_delivery(req([{"id": "m", "records": [raw]}, {"id": "ok", "records": [rec()]}]))
            self.assertEqual(out["refused"], [{"index": 0, "reason": "bad_time"}], attempts)
            self.assertEqual(pairs(out), [("ok", "pc")])
        self.assertEqual(pairs(plan_delivery(req([{"id": "n", "records": [rec(last_try=None)]}]))), [("n", "pc")])

    def test_nan_now_and_bad_params_refused(self):
        for extra in ({"now": float("nan")}, {"now": float("inf")}, {"now": True}, {"base": 0}, {"cap": 1.0},
                      {"max_attempts": 0}, {"batch": 1.5}, {"reset": "e"}, {"reset": [{"id": "e", "view": "fax"}]}):
            out = plan_delivery({**req([{"id": "e", "records": [rec()]}]), **extra})
            self.assertFalse(out["ok"], extra)

    # Negative 3
    def test_unknown_view_or_state_refused(self):
        out = plan_delivery(req([{"id": "a", "records": [rec(view="fax")]}, {"id": "b", "records": [rec(state="lost")]},
                                 {"id": "c", "records": [rec(view=["pc"])]}]))
        self.assertEqual([r["reason"] for r in out["refused"]], ["unknown_view", "unknown_state", "unknown_view"])
        self.assertEqual(out["actions"], [])

    # Negative 4
    def test_large_backlog_capped_by_batch_in_order(self):
        entries = [{"id": f"e{i:05d}", "records": [rec()]} for i in range(10_000)]
        out = plan_delivery(req(entries, batch=250))
        self.assertEqual(pairs(out), [(f"e{i:05d}", "pc") for i in range(250)])
        self.assertTrue(out["more"])

    # Negative 5
    def test_gave_up_returns_only_by_explicit_reset(self):
        entries = [{"id": "e", "records": [rec(state="gave_up", attempts=5, last_try=NOW - 1)]}]
        self.assertEqual(plan_delivery(req(entries))["actions"], [])
        self.assertEqual(plan_delivery(req(entries, reset=[{"id": "e", "view": "telegram"}]))["actions"], [])
        out = plan_delivery(req(entries, reset=[{"id": "e", "view": "pc"}]))
        self.assertEqual(out["actions"], [{"id": "e", "view": "pc", "not_before": NOW}])

    # Common rules
    def test_wrong_types_and_deep_input_refuse_without_raising(self):
        deep = []
        for _ in range(100_000):
            deep = [deep]
        hostile = [None, 5, "x", [], deep, {"now": NOW}, {"now": NOW, "entries": {"id": "e"}},
                   {"now": NOW, "entries": [Raiser()]}, Raiser(), req([deep, {"id": deep, "records": deep}]),
                   req([{"id": "e", "records": [deep, {"view": "pc"}]}])]
        for value in hostile:
            out = plan_delivery(value)
            self.assertIsInstance(out, dict)
            if not out["ok"]:
                self.assertTrue(out["reason"].isascii() and out["reason"].isprintable())
            else:
                self.assertEqual(out["actions"], [])
        self.assertEqual(plan_delivery({"now": NOW, "entries": [Raiser()]}), {"ok": False, "reason": "too_deep"})

    def test_refusal_text_prints_under_cp1252_strict(self):
        out = plan_delivery(req([{"id": "şey", "records": [rec(view="çay")]}]))
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        saved, sys.stdout = sys.stdout, stream
        try:
            print(out["refused"])
            print(plan_delivery(None))
            sys.stdout.flush()
        finally:
            sys.stdout = saved

    def test_module_imports_no_io(self):
        source = Path(__file__).resolve().parents[1] / "jarvis" / "talk_delivery.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        banned = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "shutil", "sqlite3", "requests"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertNotIn(name.split(".")[0], banned)
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"open", "__import__", "eval", "exec"})


if __name__ == "__main__":
    unittest.main()
