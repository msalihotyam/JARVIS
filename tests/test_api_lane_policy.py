"""APILANE1: the overflow-lane decision (scripts/api_lane_policy.py). Pure; no clock, no I/O."""

import ast
import importlib.util
import itertools
import unittest
from datetime import date, datetime
from pathlib import Path

from tests import adversarial

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pol = _load("api_lane_policy", "scripts/api_lane_policy.py")
SOURCE = (ROOT / "scripts" / "api_lane_policy.py").read_text(encoding="utf-8")
FORBIDDEN_MODEL = "fa" + "ble"
TODAY = date(2026, 11, 27)
SLICE = {"id": "X1", "tier": "T1", "class": "B", "files": ["jarvis/x.py", "tests/test_x.py"]}


def state(**kw):
    st = {"subscription_blocked": "yes", "ready_slices": [dict(SLICE)], "envelope_remaining_usd": 100.0,
          "per_slice_cap_usd": 20.0, "lanes_open": 0, "running_cost_usd": 0.0,
          "credits": [{"remaining_usd": 50.0, "expires": date(2027, 3, 1)}], "reading_age_s": 60, "today": TODAY}
    st.update(kw)
    return st


def late_state(**kw):  # month-end window: a December 2026 lot expiring in 4 days with 50 > cap left
    kw.setdefault("today", date(2026, 12, 27))
    kw.setdefault("credits", [{"remaining_usd": 50.0, "expires": date(2026, 12, 31)}])
    return state(**kw)


class AcceptanceTests(unittest.TestCase):
    def test_open_when_blocked_ready_covered_none_open_fresh(self):  # A1, A2
        d = pol.decide(state(ready_slices=[{**SLICE, "id": "T2X", "tier": "T2"}, dict(SLICE)]))
        self.assertEqual(d, pol.Decision("OPEN", "", "sonnet", "high", "X1"))

    def test_each_missing_open_condition_closes(self):  # A2, A6
        for kw in ({"subscription_blocked": "no"}, {"subscription_blocked": "unknown"}, {"ready_slices": []},
                   {"envelope_remaining_usd": 19.99}, {"lanes_open": 1}, {"reading_age_s": pol.FRESH_S + 1},
                   {"credits": [{"remaining_usd": 19.0, "expires": date(2027, 1, 5)}]},
                   {"credits": [{"remaining_usd": 90.0, "expires": date(2026, 11, 26)}]}):  # expired lot
            with self.subTest(kw=kw):
                d = pol.decide(state(**kw))
                self.assertEqual(d.kind, "CLOSED")
                self.assertTrue(d.reason)

    def test_month_end_opens_while_subscription_runs_and_allows_opus(self):  # A3
        self.assertEqual(pol.decide(late_state(subscription_blocked="no")).model, "sonnet")
        d = pol.decide(late_state(subscription_blocked="no", ready_slices=[{**SLICE, "model": "opus"}]))
        self.assertEqual((d.kind, d.model, d.effort), ("OPEN", "opus", "high"))
        self.assertEqual(pol.decide(late_state(subscription_blocked="unknown")).kind, "CLOSED")

    def test_month_end_margin_and_inert_after_27th(self):  # A3
        at_margin = [{"remaining_usd": 20.0, "expires": date(2026, 12, 31)}]
        self.assertEqual(pol.decide(late_state(subscription_blocked="no", credits=at_margin)).kind, "CLOSED")
        self.assertEqual(pol.decide(late_state(subscription_blocked="no", today=date(2026, 12, 28))).kind, "CLOSED")
        self.assertEqual(pol.decide(state(subscription_blocked="no", today=date(2026, 12, 20),
                                          credits=[{"remaining_usd": 50.0, "expires": date(2026, 12, 31)}])).kind,
                         "CLOSED")  # 11 days out: outside the 5-day window

    def test_eligible_t1_clean_files_not_security(self):  # A4
        self.assertTrue(pol.eligible(SLICE))
        self.assertTrue(pol.eligible({"tier": "T1", "owned_files": ["jarvis/a.py"]}))

    def test_stop_conditions_with_a_lane_open(self):  # A5, A6
        for kw, why in (({"running_cost_usd": 20.0}, "per-slice cap reached"),
                        ({"envelope_remaining_usd": 0.0}, "envelope exhausted"),
                        ({"reading_age_s": pol.FRESH_S + 1}, "credit reading stale"),
                        ({"credits": []}, "credit exhausted"),
                        ({"lanes_open": 2}, "too many overflow lanes")):
            with self.subTest(kw=kw):
                self.assertEqual(pol.decide(state(**{"lanes_open": 1, **kw})), pol.stop(why))
        self.assertEqual(pol.decide(state(lanes_open=1, running_cost_usd=19.99)).kind, "CLOSED")
        self.assertEqual(pol.decide(state(running_cost_usd=20.0)), pol.stop("per-slice cap reached"))  # no lane


class UnknownInputTests(unittest.TestCase):  # N1
    def test_hostile_numbers_in_every_numeric_field(self):
        for key in ("envelope_remaining_usd", "per_slice_cap_usd", "lanes_open", "running_cost_usd", "reading_age_s"):
            adversarial.each(self, adversarial.HOSTILE_NUMBERS,
                             lambda v, k=key: self.assertEqual(pol.decide(state(**{k: v})).kind, "CLOSED"))

    def test_wrong_types_shape_and_values(self):
        bad = [None, "x", 1, [], {}]
        for key in pol.KEYS:
            for v in bad:
                with self.subTest(key=key, v=v):
                    if (key == "ready_slices" and v == []) or (v == 1 and key.endswith(("_usd", "_s", "_open"))):
                        continue  # valid values; covered by the acceptance and truth-table tests
                    self.assertEqual(pol.decide(state(**{key: v})).kind, "CLOSED")
        st = state()
        del st["today"]
        for s in (None, [], "state", st, {**state(), "extra": 1}):
            self.assertEqual(pol.decide(s).kind, "CLOSED")
        for kw in ({"subscription_blocked": "YES"}, {"subscription_blocked": True},
                   {"today": datetime(2026, 11, 27, 12)}, {"per_slice_cap_usd": 20.01}, {"per_slice_cap_usd": 0},
                   {"credits": [{"remaining_usd": 50.0}]}, {"credits": [{"remaining_usd": 50.0, "expires": "2027-01-01"}]},
                   {"credits": [{"remaining_usd": 50.0, "expires": date(2027, 1, 1), "x": 1}]},
                   {"credits": [{"remaining_usd": float("nan"), "expires": date(2027, 1, 1)}]},
                   {"ready_slices": [{**SLICE, "id": None}]}, {"ready_slices": ["X1"]}):
            with self.subTest(kw=kw):
                self.assertEqual(pol.decide(state(**kw)).kind, "CLOSED")


class EligibilityTests(unittest.TestCase):  # N2
    def test_not_eligible(self):
        for s in ({**SLICE, "tier": "T2"}, {**SLICE, "tier": "T0"}, {**SLICE, "tier": "t1"},
                  {k: v for k, v in SLICE.items() if k != "tier"}, {**SLICE, "class": "security"},
                  {**SLICE, "class": " Security "}, {**SLICE, "class": None}, {**SLICE, "files": []},
                  {**SLICE, "files": "jarvis/x.py"}, {**SLICE, "files": ["jarvis/x.py", 3]},
                  {**SLICE, "owned_files": [".env"]}, None, "X1"):
            with self.subTest(s=s):
                self.assertFalse(pol.eligible(s))

    def test_protected_files(self):
        for p in ("data/jarvis.db", "data\\x.json", "uv.lock", ".env.local", ".claude/settings.json",
                  ".github/workflows/ci.yml", "scripts/conveyor.py", "scripts/api_lane_policy.py", "jarvis/key.pem",
                  "jarvis/skip_list.py", "jarvis/load-sensitive.json", "AGENTS.md", "dev/dashboard/x.js",
                  "../x.py", "/x.py", "jarvis/*.py", "jarvis/x.py:s", "CLAUDE~1/x", "x/.jarvis/a",
                  *adversarial.HOSTILE_PATH_PARTS[:31]):
            with self.subTest(p=p):
                self.assertFalse(pol.eligible({**SLICE, "files": ["jarvis/x.py", p]}))

    def test_mirror_matches_conveyor(self):
        conv = _load("conveyor_apilane1", "scripts/conveyor.py")
        corpus = (*adversarial.HOSTILE_PATH_PARTS, "jarvis/x.py", "tests/test_x.py", "docs/a.md", "jarvis/riot/a.py",
                  "scripts/test_parallel.py", "a/CLAUDE.md", "jarvis/allowlist.py", "x.sqlite3", ".Env", "data")
        for p in corpus:
            with self.subTest(p=p):
                if conv.codex_veto(p) or p.lower() in conv.PROTECTED_FILES or conv.SKIPLIST_RE.search(p):
                    self.assertTrue(pol.protected(p))  # the lane set is a superset of the conveyor's
                self.assertEqual(bool(pol.codex_veto(p)), bool(conv.codex_veto(p)))
                self.assertEqual(bool(pol.protected_path(p)), bool(conv.protected_path(p)))


class TruthTableTests(unittest.TestCase):  # N3, A6, N5
    def test_open_only_in_the_allowed_rows(self):
        rows = itertools.product(("yes", "no", "unknown"), (True, False), (True, False), (0, 1), (True, False),
                                 (True, False))
        for blocked, elig, env, lanes, fresh, late in rows:
            make = late_state if late else state
            d = pol.decide(make(subscription_blocked=blocked, lanes_open=lanes,
                                ready_slices=[{**SLICE, "model": "opus", "tier": "T1" if elig else "T2"}],
                                envelope_remaining_usd=100.0 if env else 10.0, reading_age_s=60 if fresh else 1000))
            want = (blocked == "yes" or (late and blocked == "no")) and elig and env and lanes == 0 and fresh
            with self.subTest(row=(blocked, elig, env, lanes, fresh, late), d=d):
                self.assertEqual(d.kind == "OPEN", want)
                if d.kind == "OPEN":
                    self.assertEqual((d.model, d.effort), ("opus" if late else "sonnet", "high"))
                else:
                    self.assertEqual(d.model, "")
                    self.assertTrue(0 < len(d.reason) <= 40)
                if lanes == 1 and not fresh:
                    self.assertEqual(d.kind, "STOP")


class DateTests(unittest.TestCase):  # N4
    def test_month_end_never_outside_december_2026(self):
        for today, exp in ((date(2026, 11, 28), date(2026, 11, 30)), (date(2026, 12, 29), date(2027, 1, 1)),
                           (date(2027, 12, 27), date(2027, 12, 31)), (date(2026, 12, 27), date(2026, 12, 26)),
                           (date(2025, 12, 27), date(2025, 12, 31))):
            credits = [{"remaining_usd": 50.0, "expires": exp}, {"remaining_usd": 50.0, "expires": date(2028, 1, 1)}]
            with self.subTest(today=today, exp=exp):
                self.assertFalse(pol.month_end([(50.0, exp)], today, 20.0))
                d = pol.decide(state(subscription_blocked="no", today=today, credits=credits,
                                     ready_slices=[{**SLICE, "model": "opus"}]))
                self.assertEqual(d.kind, "CLOSED")

    def test_clock_going_backwards_closes(self):
        for kw in ({"reading_age_s": -1}, {"reading_age_s": -0.5}, {"today": date(2026, 11, 25)}):
            with self.subTest(kw=kw):
                self.assertEqual(pol.decide(state(**kw)), pol.closed("clock went backwards"))
        self.assertEqual(pol.decide(late_state(reading_age_s=-1)).kind, "CLOSED")


class ModelTests(unittest.TestCase):  # N5
    def test_only_sonnet_or_opus_and_no_forbidden_name(self):
        for want in ("haiku", FORBIDDEN_MODEL, "claude-opus-5-5", "OPUS", "opus"):
            for make in (state, late_state):
                with self.subTest(want=want, late=make is late_state):
                    d = pol.decide(make(ready_slices=[{**SLICE, "model": want}]))
                    expect = "opus" if (make is late_state and want == "opus") else "sonnet"
                    self.assertEqual((d.kind, d.model), ("OPEN", expect))
        self.assertNotIn(FORBIDDEN_MODEL, SOURCE.casefold())
        self.assertNotIn(FORBIDDEN_MODEL, repr(pol.__dict__.get("MODEL_SONNET")) + repr(pol.__dict__.get("MODEL_OPUS")))


class StaticPurityTests(unittest.TestCase):  # N6
    def test_imports_and_calls(self):
        tree = ast.parse(SOURCE)
        allowed = {"math", "posixpath", "re", "unicodedata", "datetime", "typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue({a.name.split(".")[0] for a in node.names} <= allowed, ast.dump(node))
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], allowed)
                self.assertEqual(node.level, 0)
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, {"open", "eval", "exec", "compile", "__import__", "input", "globals",
                                           "breakpoint", "getattr", "setattr"})
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"now", "today", "utcnow", "time", "environ", "system", "popen",
                                             "read_text", "write_text", "open"})
                if isinstance(node.value, ast.Name) and node.value.id in allowed:  # module attrs: allowlist only
                    self.assertIn(f"{node.value.id}.{node.attr}", {
                        "math.isfinite", "math.copysign", "posixpath.normpath", "re.compile", "re.match",
                        "re.split", "re.search", "re.I", "unicodedata.category", "unicodedata.normalize"})


if __name__ == "__main__":
    unittest.main()
