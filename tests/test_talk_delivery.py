import ast
import io
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
