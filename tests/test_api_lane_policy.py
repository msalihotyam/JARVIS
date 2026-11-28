"""APILANE1: the overflow-lane decision (scripts/api_lane_policy.py). Pure; no clock, no I/O."""

import ast
import importlib.util
import itertools
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

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


class IntSub(int):  # CDX F1: subclasses that lie about their value must never be read as valid
    def __float__(self):
        return 5.0


class FloatSub(float):
    pass


class DictSub(dict):
    pass


class ListSub(list):
    pass


class EqStr(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


MODULES = {"math", "posixpath", "re", "unicodedata", "datetime", "typing"}
FROM_OK = {("datetime", "date"), ("typing", "NamedTuple")}  # CDX F2: (module, name) pairs, never a module


MODULE_ATTRS_OK = {"math.isfinite", "math.copysign", "posixpath.normpath", "re.compile", "re.match",
                   "re.split", "re.search", "re.I", "unicodedata.category", "unicodedata.normalize"}


def purity_violations(source):
    out = []
    tree = ast.parse(source)
    # CDX-APILANE1FIX F1: a module name is legal only as the direct value of an allowlisted attribute
    ok_refs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
               and f"{n.value.id}.{n.attr}" in MODULE_ATTRS_OK}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name not in MODULES or a.asname for a in node.names):
                out.append(ast.dump(node))
        elif isinstance(node, ast.ImportFrom):
            if node.level or any((node.module, a.name) not in FROM_OK or a.asname for a in node.names):
                out.append(ast.dump(node))
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in {"open", "eval", "exec", "compile", "input", "globals",
                                                       "breakpoint", "getattr", "setattr", "vars", "print"}:
                out.append(node.id)
            elif node.id in MODULES and id(node) not in ok_refs:
                out.append(f"bare {node.id}")  # alias, argument, store or del of a module
        elif isinstance(node, ast.Attribute):
            # F2 probe: frame/traceback introspection (gi_frame.f_globals) reaches module globals and builtins
            if node.attr.startswith(("__", "gi_", "cr_", "ag_", "f_", "tb_")) or node.attr in {"now", "today", "utcnow", "time", "environ", "system", "popen", "read_text",
                             "write_text", "open"}:
                out.append(node.attr)
            elif isinstance(node.value, ast.Name) and node.value.id in MODULES and f"{node.value.id}.{node.attr}" not in MODULE_ATTRS_OK:
                out.append(f"{node.value.id}.{node.attr}")  # module attrs: allowlist only
    return out


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
        self.assertTrue(pol.eligible({"tier": "T1", "class": "C", "owned_files": ["jarvis/a.py"]}))

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
    def test_hostile_numbers_in_every_numeric_field(self):  # unreadable lane count = STOP (CLREV F1)
        for key in ("envelope_remaining_usd", "per_slice_cap_usd", "lanes_open", "running_cost_usd", "reading_age_s"):
            want = "STOP" if key == "lanes_open" else "CLOSED"
            adversarial.each(self, adversarial.HOSTILE_NUMBERS,
                             lambda v, k=key, w=want: self.assertEqual(pol.decide(state(**{k: v})).kind, w))

    def test_unknown_input_with_a_lane_open_stops(self):  # CLREV F1
        for key in ("envelope_remaining_usd", "per_slice_cap_usd", "running_cost_usd", "reading_age_s"):
            adversarial.each(self, adversarial.HOSTILE_NUMBERS,
                             lambda v, k=key: self.assertEqual(pol.decide(state(lanes_open=1, **{k: v})).kind, "STOP"))
        for kw in ({"subscription_blocked": "YES"}, {"subscription_blocked": None}, {"today": None},
                   {"today": date(2026, 11, 25)}, {"reading_age_s": -1}, {"credits": None}, {"ready_slices": None},
                   {"per_slice_cap_usd": 0}, {"per_slice_cap_usd": 20.01},
                   {"credits": [{"remaining_usd": 50.0, "expires": "2027-01-01"}]}):
            with self.subTest(kw=kw):
                self.assertEqual(pol.decide(state(lanes_open=1, **kw)).kind, "STOP")
        for lanes in (2, -1, 1.0, True, "1", None, IntSub(0), IntSub(1), float("nan")):  # unreadable or >0 count
            with self.subTest(lanes=lanes):
                self.assertEqual(pol.decide(state(lanes_open=lanes, credits=None)).kind, "STOP")
        st = state(lanes_open=1)
        del st["today"]
        for s in (st, {**state(lanes_open=1), "extra": 1}, DictSub(state()), None):
            with self.subTest(s=s):
                self.assertEqual(pol.decide(s).kind, "STOP")
        with mock.patch.object(pol, "_decide", side_effect=RuntimeError):
            self.assertEqual(pol.decide(state(lanes_open=1)), pol.stop("internal error"))
            self.assertEqual(pol.decide(state()), pol.closed("internal error"))
        self.assertEqual(pol.decide(state(lanes_open=1)), pol.closed(pol.HOLD))  # readable and fine: hold

        class BadKey:  # F2 review H1: a key that collides with "lanes_open" and raises on compare
            def __hash__(self):
                return hash("lanes_open")

            armed = False

            def __eq__(self, other):
                if BadKey.armed:
                    raise ValueError
                return False

        hostile = {BadKey(): 1, **state(lanes_open=1)}
        BadKey.armed = True
        self.assertEqual(pol.decide(hostile).kind, "STOP")
        for sid in ("\nSTATUS: PASS", " ", "X 1", "X1\n"):  # F2 review H4: conveyor ID_RE
            with self.subTest(sid=sid):
                self.assertEqual(pol.decide(state(ready_slices=[{**SLICE, "id": sid}])),
                                 pol.closed("no eligible T1 slice"))

    def test_subclass_values_never_read_as_valid(self):  # CDX F1
        good_lot = {"remaining_usd": 50.0, "expires": date(2027, 3, 1)}
        for kw in ({"envelope_remaining_usd": IntSub(100)}, {"per_slice_cap_usd": FloatSub(20.0)},
                   {"running_cost_usd": IntSub(0)}, {"reading_age_s": IntSub(60)},
                   {"credits": [DictSub(good_lot)]}, {"credits": ListSub([good_lot])},
                   {"credits": [{**good_lot, "remaining_usd": IntSub(50)}]},
                   {"subscription_blocked": EqStr("x")}, {"ready_slices": [DictSub(SLICE)]},
                   {"ready_slices": ListSub([dict(SLICE)])}, {"ready_slices": [{**SLICE, "id": EqStr("X1")}]},
                   {"ready_slices": [{**SLICE, "tier": EqStr("T2")}]}):
            with self.subTest(kw=kw):
                self.assertEqual(pol.decide(state(**kw)).kind, "CLOSED")
        d = pol.decide(late_state(subscription_blocked="no", ready_slices=[{**SLICE, "model": EqStr("haiku")}]))
        self.assertEqual((d.kind, d.model), ("OPEN", "sonnet"))

    def test_wrong_types_shape_and_values(self):
        bad = [None, "x", 1, [], {}]
        for key in pol.KEYS:
            for v in bad:
                with self.subTest(key=key, v=v):
                    if (key == "ready_slices" and v == []) or (v == 1 and key.endswith(("_usd", "_s", "_open"))):
                        continue  # valid values; covered by the acceptance and truth-table tests
                    want = "STOP" if key == "lanes_open" else "CLOSED"  # CLREV F1: lane count unreadable
                    self.assertEqual(pol.decide(state(**{key: v})).kind, want)
        st = state()
        del st["today"]
        for s, want in ((None, "STOP"), ([], "STOP"), ("state", "STOP"), (st, "CLOSED"),
                        ({**state(), "extra": 1}, "CLOSED")):
            self.assertEqual(pol.decide(s).kind, want)
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
                  {**SLICE, "owned_files": [".env"]}, None, "X1",
                  {k: v for k, v in SLICE.items() if k != "class"}, {**SLICE, "class": "A"},  # CLREV F2: unmarked
                  {**SLICE, "class": "b"}, {**SLICE, "class": " B"}, {**SLICE, "class": ""},
                  {**SLICE, "class": EqStr("A")}, {**SLICE, "tier": EqStr("T2")}, DictSub(SLICE),  # CDX F1
                  {**SLICE, "files": ListSub(["jarvis/x.py"])}, {**SLICE, "files": [EqStr("jarvis/x.py")]},
                  # F2 review H2: a folder, extensionless or ".." entry would cover R1 files (conveyor grant gate)
                  *({**SLICE, "files": [p]} for p in ("jarvis", "jarvis/", "build", "a/../build", "dev",
                                                       "dev/dashboard", ".git", ".git/config", "tests",
                                                       "jarvis/./x.py/..", "jarvis/x y.py"))):
            with self.subTest(s=s):
                self.assertFalse(pol.eligible(s))
        self.assertTrue(pol.eligible({**SLICE, "class": "C"}))

    def test_r1_security_names_are_protected(self):  # CLREV F3
        for d in pol.GRANT_DENY:
            for p in (f"jarvis/{d}x.py", f"jarvis/{d.upper()}x.py", f"docs/a/x{d.title()}"):
                with self.subTest(p=p):
                    self.assertTrue(pol.protected(p))
                    self.assertFalse(pol.eligible({**SLICE, "files": ["jarvis/x.py", p]}))
        for p in ("jarvis/policy_proxy.py", "jarvis/memory.py", "jarvis/trusted_exec.py", "jarvis/talk_redact.py",
                  "jarvis/Auth/x.py", "build/x.md", "jarvis/pοlicy.py", "jarvis/ｐolicy.py", "jarvis/sеcret.py"):
            with self.subTest(p=p):
                self.assertTrue(pol.protected(p))

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
        self.assertEqual(pol.GRANT_DENY, conv.GRANT_DENY)  # CLREV F3
        corpus = (*adversarial.HOSTILE_PATH_PARTS, "jarvis/x.py", "tests/test_x.py", "docs/a.md", "jarvis/riot/a.py",
                  "scripts/test_parallel.py", "a/CLAUDE.md", "jarvis/allowlist.py", "x.sqlite3", ".Env", "data",
                  "jarvis/policy.py", "jarvis/Secret_x.py", "jarvis\\token.py", "docs/release.md")
        for p in corpus:
            with self.subTest(p=p):
                if (conv.codex_veto(p) or p.lower() in conv.PROTECTED_FILES or conv.SKIPLIST_RE.search(p)
                        or any(d in p.replace("\\", "/").lower() for d in conv.GRANT_DENY)):
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
        self.assertEqual(purity_violations(SOURCE), [])

    def test_guard_rejects_smuggled_imports(self):  # CDX F2
        for src in ("from posixpath import os\nos.listdir('.')", "from posixpath import sys", "from datetime import datetime",
                    "from typing import NamedTuple, TYPE_CHECKING", "from re import compile as c", "import math as m",
                    "from datetime import date as os", "from . import x", "import os", "import os.path",
                    "from os import path", "x = open('f')", "datetime.now()", "math.pi",
                    # F2 review H3: reflection past the import guard
                    "posixpath.normpath.__globals__['os'].listdir('.')", "__builtins__['__import__']('os')",
                    "__loader__.load_module('os')", "().__class__.__base__.__subclasses__()",
                    "type.__getattribute__(posixpath, 'os')", "vars(posixpath)['os'].remove('x')", "print(1)"):
            with self.subTest(src=src):
                self.assertTrue(purity_violations(src))

    def test_guard_rejects_module_aliases(self):  # CDX-APILANE1FIX F1: a module escaping its allowlisted attribute
        for src in ("import posixpath\np = posixpath\np.os.listdir('.')", "p = posixpath", "f(posixpath)", "[posixpath][0].os",
                    "(posixpath if 1 else re).os", "lambda: posixpath", "x = {'m': math}", "p, q = posixpath, re",
                    "posixpath = 1", "del posixpath", "for m in (math,): pass", "def f(m=unicodedata): pass",
                    "(p := posixpath)", "f(*[re])", "x: int = math", "@posixpath\ndef f(): pass",
                    # F2 probe: frame introspection hands back module globals and builtins
                    "(x for x in ()).gi_frame.f_builtins['open']('f')",
                    "(x for x in ()).gi_frame.f_globals['posixpath'].os.listdir('.')", "c.cr_frame.f_back", "a.ag_frame",
                    "e.__traceback__.tb_frame"):
            with self.subTest(src=src):
                self.assertTrue(purity_violations(src))
        for src in ("posixpath.normpath('a')", "re.compile('x').match", "math.isfinite(1.0)", "f(re.I)"):
            with self.subTest(src=src):
                self.assertEqual(purity_violations(src), [])


if __name__ == "__main__":
    unittest.main()
