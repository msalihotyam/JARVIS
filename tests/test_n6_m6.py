"""N6M6SCEN1: the three pinned M6 scenarios, their sha256 pin and the scorer (fake worker only)."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("n6_m6_run", ROOT / "scripts" / "n6_m6_run.py")
m6 = importlib.util.module_from_spec(_SPEC)
sys.modules["n6_m6_run"] = m6
_SPEC.loader.exec_module(m6)


def _refuse(*_a, **_k):
    raise AssertionError("no network or database access in M6")


def _main(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m6.main(list(argv))
    return code, buf.getvalue()


def _tree_sha(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(path)).encode() + p.read_bytes())
    return h.hexdigest()


class M6RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guards = [mock.patch.object(sqlite3, "connect", _refuse),
                      mock.patch.object(socket.socket, "connect", _refuse),
                      mock.patch.object(socket, "create_connection", _refuse)]
        for g in cls.guards:
            g.start()
        cls.before = _tree_sha(m6.ACCEPTANCE)
        cls.scenarios = {s["id"]: s for s in m6.load_scenarios()}
        cls.results = m6.run_all(list(cls.scenarios.values()))
        cls.ok, cls.text = m6.score(cls.results)

    @classmethod
    def tearDownClass(cls):
        for g in cls.guards:
            g.stop()

    def _variant(self, sid, **changes):
        sc = copy.deepcopy(self.scenarios[sid])
        sc.update(changes)
        return m6.run_all([sc])[sid]

    def test_full_run_passes_with_kpis(self):
        self.assertTrue(self.ok, self.text)
        self.assertEqual(self.text.splitlines(), ["A PASS", "B PASS", "C PASS", "accepted_per_quota 2/7",
                                                  "post_accept_failures 0", "OVERALL PASS"])
        a, b, c = (self.results[k] for k in "ABC")
        self.assertEqual((a["attempts"], a["receipt"]), (1, "PASS"))
        self.assertEqual((b["blocked_by"], b["review_reason"], b["blocking_files"]),
                         ("review", "blocking_findings", ["discount.py"]))
        self.assertEqual((c["attempts"], c["root_causes"], c["receipt"]), (2, ["C-1"], "PASS"))

    def test_runner_wrote_nothing_under_acceptance(self):
        self.assertEqual(_tree_sha(m6.ACCEPTANCE), self.before)

    def test_b_missed_by_review_is_a_false_pass(self):
        r = self._variant("B", review_rules=[])
        self.assertEqual((r["outcome"], r["post_accept_failed"]), ("accepted", True))  # tests miss it
        ok, text = m6.score({**self.results, "B": r})
        self.assertFalse(ok)
        self.assertIn("B FAIL", text)
        self.assertIn("B accepted: false PASS", text)
        self.assertIn("post_accept_failures 1", text)
        self.assertTrue(text.endswith("OVERALL FAIL"))

    def test_c_retry_without_root_cause_fails(self):
        r = self._variant("C", root_causes={})
        self.assertEqual((r["outcome"], r["blocked_by"], r["attempts"]), ("blocked", "no_root_cause", 1))
        self.assertEqual(m6.judge("C", r), "FAIL")

    def test_c_root_cause_that_repeats_the_error_fails(self):
        r = self._variant("C", root_causes={"C-1": "ROOT CAUSE C-1: AssertionError"})
        self.assertEqual(r["blocked_by"], "no_root_cause")
        self.assertEqual(m6.judge("C", r), "FAIL")

    def test_c_beyond_retry_cap_is_blocked(self):
        sc = self.scenarios["C"]
        bad, good = sc["attempts"]
        why = sc["root_causes"]["C-1"]
        bad2 = {"words.py": bad["words.py"] + "# second try, same bug\n"}  # a new commit, still failing
        r = self._variant("C", attempts=[bad, bad2, good],
                          root_causes={"C-1": why, "C-2": why.replace("C-1", "C-2")})
        self.assertEqual((r["blocked_by"], r["attempts"]), ("retry_cap", m6.RETRY_CAP))
        self.assertEqual(m6.judge("C", r), "FAIL")


class M6PinAndScoreTests(unittest.TestCase):
    def _copy(self, tmp, edit):
        doc = json.loads(m6.SCENARIOS.read_text(encoding="utf-8"))
        edit(doc)
        path = Path(tmp) / "scenarios.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def test_committed_pin_verifies(self):
        self.assertEqual([s["id"] for s in m6.load_scenarios()], ["A", "B", "C"])

    def test_edited_or_unpinned_scenarios_are_refused_before_anything_runs(self):
        def repinned(d):  # edited and re-hashed in the file: the runner's own pin still refuses it
            d["scenarios"][1].update(review_rules=[])
            d["sha256"] = m6.canonical_sha(d)
        edits = {"sha256 mismatch": lambda d: d["scenarios"][1].update(review_rules=[]),
                 "sha256 mismatch ": repinned,
                 "carries no sha256": lambda d: d.pop("sha256")}
        for why, edit in edits.items():
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(m6.subprocess, "run", side_effect=AssertionError("ran")):
                code, out = _main("--scenarios", self._copy(tmp, edit))
            self.assertEqual(code, 2)
            self.assertIn(f"REFUSED: scenarios.json {why.strip()}", out)

    def test_out_under_acceptance_is_refused_any_case(self):
        target = str(m6.ACCEPTANCE / "m6" / "score.txt").upper()
        with mock.patch.object(m6.subprocess, "run", side_effect=AssertionError("ran")):
            code, out = _main("--out", target)
        self.assertEqual(code, 2)
        self.assertIn("never writes under dev/n6/acceptance/", out)

    def test_b_accepted_fails_overall_even_with_a_and_c_pass(self):
        a = {"outcome": "accepted", "receipt": "PASS", "post_accept_failed": False, "attempts": 1,
             "root_causes": [], "writer_sessions": 1, "review_sessions": 1}
        c = {**a, "attempts": 2, "root_causes": ["C-1"], "writer_sessions": 2, "check_failures": 1}
        b = {**a, "planted_file": "discount.py"}
        ok, text = m6.score({"A": a, "B": b, "C": c})
        self.assertFalse(ok)
        self.assertEqual(text.splitlines()[:3], ["A PASS", "B FAIL", "C PASS"])
        self.assertIn("accepted_per_quota 3/7", text)
        self.assertTrue(text.endswith("OVERALL FAIL"))

    def test_missing_readings_print_missing(self):
        ok, text = m6.score({"A": None, "C": {"outcome": "accepted"}})
        self.assertFalse(ok)
        self.assertEqual(text.splitlines(), ["A missing", "B missing", "C FAIL", "accepted_per_quota missing",
                                             "post_accept_failures missing", "OVERALL FAIL"])

    def test_no_network_model_or_clock_imports_and_app_never_imports_runner(self):
        tree = ast.parse((ROOT / "scripts" / "n6_m6_run.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        allowed = {"__future__", "argparse", "hashlib", "json", "os", "re", "secrets", "subprocess", "sys",
                   "tempfile", "pathlib", "jarvis.work_checks", "jarvis.work_evidence",
                   "jarvis.work_regress", "jarvis.work_review", "jarvis.workers.base"}
        self.assertEqual(names - allowed, set())
        users = [p for p in (ROOT / "jarvis").rglob("*.py") if "n6_m6" in p.read_text(encoding="utf-8")]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
