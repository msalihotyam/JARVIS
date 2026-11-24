"""N6P2EXIT1: the read-only Phase 2 exit check of dev/n6/N6_KIT.md (negative-first)."""

import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jarvis import n6_kpi

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
_SPEC = importlib.util.spec_from_file_location("n6_phase_exit", ROOT / "scripts" / "n6_phase_exit.py")
px = importlib.util.module_from_spec(_SPEC)
sys.modules["n6_phase_exit"] = px
_SPEC.loader.exec_module(px)

IDS = [f"S{i:02d}" for i in range(12)]
GROUP = {"n": 20, "accepted": 10, "small_sample": False, "pass_rate": 0.5, "post_accept_failures": 2,
         "kpi_rows_measured": 10, "interventions": 10, "interventions_n": 20,
         "quota_per_accepted": 1000.0, "finish_seconds_median": 100.0}
ROW = n6_kpi.KpiRow("S00", "n6", True, 0, 0, 500, 105.0, 0, T0, "live")
TEST_ID = "tests.test_n6_lease.LeaseTests.test_neg_two_managers_exactly_one_wins"
CI = {"green": True, "accounted": True, "failures": 0, "errors": 0, "skips": [],
      "dispatched": ["tests.test_n6_lease.LeaseTests"], "durations": [[0.1, TEST_ID]]}


def n6dir(root):
    return root / "build" / "n6"


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def write_baseline(root, group=None, pinned=None):
    data = json.dumps({"version": 1, "groups": {"code/claude": group or GROUP}}).encode()
    n6dir(root).mkdir(parents=True, exist_ok=True)
    (n6dir(root) / "baseline.json").write_bytes(data)
    (n6dir(root) / "baseline.json.sha256").write_text(
        f"{pinned or hashlib.sha256(data).hexdigest()}  baseline.json\n", encoding="ascii")


def seed(root, slices=10, rows=None, tier="T1", **row):
    db = n6dir(root) / "kpi.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    for sid in IDS[:slices]:
        n6_kpi.record_limited(db, n6_kpi.LimitedEvent(sid, "run1", T0, T0, "PASS", "0123abcd"))
    for sid in IDS[:slices if rows is None else rows]:
        n6_kpi.record(db, dataclasses.replace(ROW, slice_id=sid, **row))
    write_json(root / "build" / "conveyor" / "backlog.json",
               {"slices": [{"id": sid, "tier": tier} for sid in IDS]})


def full(root, **seeding):
    seed(root, **seeding)
    write_baseline(root)
    write_json(n6dir(root) / "ci-report.json", CI)
    write_json(n6dir(root) / "fallback_drill.json",
               {"kind": "live", "root": str(root), "slice_id": "S00", "lost": 0, "doubled": 0})


def run(root, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = px.main(list(argv), root=root)
    text = out.getvalue()
    return code, text, dict(line.split(": ", 1) for line in text.splitlines())


def status(lines, name):
    return lines[name].split(" | ")[0]


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class PhaseExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_every_criterion_evidenced_is_pass_and_writes_only_the_report(self):
        full(self.root)
        before = snapshot(self.root)
        code, text, lines = run(self.root)
        self.assertEqual(code, 0, text)
        self.assertEqual(lines.pop("OVERALL"), "PASS")
        self.assertEqual(len(lines), 10)
        self.assertTrue(all(v.startswith("PASS | ") for v in lines.values()), text)
        self.assertIn(TEST_ID, lines["TWO_MANAGER"])
        after = snapshot(self.root)
        report = px.REPORT.as_posix()
        self.assertEqual(after.pop(report).decode("utf-8").splitlines(), text.splitlines())
        self.assertEqual(after, before)  # nothing else written, no -wal/-shm, db bytes unchanged

    def test_neg_nine_slices_is_not_yet(self):
        full(self.root, slices=9)
        code, text, lines = run(self.root)
        self.assertEqual((code, status(lines, "SLICES"), lines["OVERALL"]), (1, "NOT_YET", "NOT_YET"))
        self.assertIn("9 N6-run T1 slices", lines["SLICES"])

    def test_neg_non_t1_tier_is_not_counted(self):
        full(self.root, tier="T2")
        code, text, lines = run(self.root)
        self.assertEqual((code, status(lines, "SLICES"), lines["OVERALL"]), (1, "NOT_YET", "NOT_YET"))
        self.assertIn("0 N6-run T1 slices", lines["SLICES"])

    def test_neg_any_missing_reading_is_not_yet_never_pass(self):
        def no(name):
            return lambda root: (n6dir(root) / name).unlink()
        cases = {
            "E8d_QUOTA": lambda root: write_baseline(root, dict(GROUP, quota_per_accepted="missing")),
            "E8a_PASS_RATE": lambda root: write_baseline(root, dict(GROUP, small_sample=True)),
            "E8e_FINISH": lambda root: write_baseline(root, dict(GROUP, finish_seconds_median=90.0)),
            "E8c_INTERVENTIONS": no("baseline.json"),
            "TWO_MANAGER": no("ci-report.json"),
            "FALLBACK_DRILL": no("fallback_drill.json"),
            "SLICES": no("kpi.db"),
        }
        for name, knock in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                full(root)
                knock(root)
                code, text, lines = run(root)
                self.assertEqual((code, status(lines, name), lines["OVERALL"]), (1, "NOT_YET", "NOT_YET"), text)
        for name, ci in (("skipped", dict(CI, skips=[[TEST_ID, "no"]])), ("red", dict(CI, green=False)),
                         ("failures", dict(CI, failures=1)), ("bool zero", dict(CI, errors=False)),
                         ("absent", dict(CI, dispatched=[], durations=[]))):
            with self.subTest(ci=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                full(root)
                write_json(n6dir(root) / "ci-report.json", ci)
                code, text, lines = run(root)
                self.assertEqual((code, status(lines, "TWO_MANAGER"), lines["OVERALL"]), (1, "NOT_YET", "NOT_YET"))

    def test_neg_counted_slice_without_live_kpi_row_is_not_yet(self):
        for name, seeding in (("no row", {"rows": 9}), ("sweep origin", {"origin": "sweep"})):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                full(root, **seeding)
                code, text, lines = run(root)
                self.assertEqual(lines["OVERALL"], "NOT_YET", text)
                for key in ("E8a_PASS_RATE", "E8b_POST_ACCEPT", "E8c_INTERVENTIONS", "E8d_QUOTA", "E8e_FINISH"):
                    self.assertEqual(status(lines, key), "NOT_YET")
                self.assertIn("no live n6 kpi row for", lines["E8a_PASS_RATE"])

    def test_neg_one_false_pass_or_incident_fails_regardless_of_the_rest(self):
        for name, seeding in (("false pass", {"post_accept_defects": 1}), ("incident", {"security_incidents": 1})):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                full(root, **seeding)
                code, text, lines = run(root)
                self.assertEqual((code, lines["OVERALL"]), (1, "FAIL"), text)
            with self.subTest(name + ", nothing else evidenced"), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seed(root, slices=1, **seeding)
                code, text, lines = run(root)
                self.assertEqual((code, lines["OVERALL"]), (1, "FAIL"), text)

    def test_neg_baseline_hash_mismatch_refuses_and_writes_nothing(self):
        full(self.root)
        write_baseline(self.root, pinned="0" * 64)
        before = snapshot(self.root)
        code, text, _ = run(self.root)
        self.assertEqual(code, 2)
        self.assertTrue(text.startswith("REFUSED: baseline hash mismatch"), text)
        self.assertNotIn("OVERALL", text)
        self.assertEqual(snapshot(self.root), before)
        (n6dir(self.root) / "baseline.json.sha256").write_text("garbage\n", encoding="ascii")
        self.assertEqual(run(self.root)[0], 2)
        self.assertFalse((self.root / px.REPORT).exists())

    def test_neg_rehearsal_drill_is_labelled_rehearsal_not_counted(self):
        full(self.root)
        with tempfile.TemporaryDirectory() as copy:
            for receipt in ({"kind": "rehearsal", "root": str(self.root), "lost": 0, "doubled": 0},
                            {"kind": "live", "root": copy, "lost": 0, "doubled": 0},
                            {"lost": 0, "doubled": 0}):
                with self.subTest(receipt=receipt):
                    write_json(n6dir(self.root) / "fallback_drill.json", receipt)
                    code, text, lines = run(self.root)
                    self.assertEqual((code, status(lines, "FALLBACK_DRILL"), lines["OVERALL"]),
                                     (1, "NOT_YET", "NOT_YET"))
                    self.assertIn("rehearsal", lines["FALLBACK_DRILL"])
                    self.assertIn("not counted as the live drill", lines["FALLBACK_DRILL"])
        write_json(n6dir(self.root) / "fallback_drill.json", {"kind": "live", "root": str(self.root),
                                                             "lost": 1, "doubled": 0})
        self.assertEqual(status(run(self.root)[2], "FALLBACK_DRILL"), "NOT_YET")

    def test_neg_operator_database_is_never_opened(self):
        full(self.root)
        for db in (self.root / "data" / "jarvis.db", self.root / "data" / "other.db", self.root / "x" / "jarvis.db"):
            with self.subTest(db=db.as_posix()):
                code, text, _ = run(self.root, "--db", str(db))
                self.assertEqual(code, 2, text)
                self.assertFalse(db.exists())
                self.assertFalse(db.parent.exists())
        self.assertFalse((self.root / px.REPORT).exists())

    def test_neg_live_drill_receipt_without_slice_id_is_not_yet(self):
        full(self.root)
        base = {"kind": "live", "root": str(self.root), "lost": 0, "doubled": 0}
        for name, extra in (("absent", {}), ("null", {"slice_id": None}), ("empty", {"slice_id": ""}),
                            ("blank", {"slice_id": "   "}), ("int", {"slice_id": 0}), ("list", {"slice_id": ["S00"]}),
                            ("zero-width", {"slice_id": "​"}), ("lone surrogate", {"slice_id": "\ud800"})):
            with self.subTest(name):
                write_json(n6dir(self.root) / "fallback_drill.json", dict(base, **extra))
                code, text, lines = run(self.root)
                self.assertEqual((code, status(lines, "FALLBACK_DRILL"), lines["OVERALL"]),
                                 (1, "NOT_YET", "NOT_YET"), text)
                self.assertIn("no slice_id", lines["FALLBACK_DRILL"])

    def test_neg_malformed_input_is_refused_exit_2_and_writes_nothing(self):
        def ci(**fields):
            return lambda root: write_json(n6dir(root) / "ci-report.json", dict(CI, **fields))

        def base(**fields):
            return lambda root: write_baseline(root, dict(GROUP, **fields))

        def raw_baseline(text):  # JSON json.dumps cannot write (a huge int)
            def knock(root):
                data = text.encode()
                (n6dir(root) / "baseline.json").write_bytes(data)
                (n6dir(root) / "baseline.json.sha256").write_text(
                    f"{hashlib.sha256(data).hexdigest()}  baseline.json\n", encoding="ascii")
            return knock

        group = json.dumps(GROUP)[:-1]
        cases = {
            "dispatched=1": ci(dispatched=1), "dispatched str": ci(dispatched="x"), "dispatched dict": ci(dispatched={}),
            "dispatched [1]": ci(dispatched=[1]), "skips [[[]]]": ci(skips=[[[]]]), "skips 1": ci(skips=1),
            "skips [[]]": ci(skips=[[]]), "durations [[1, []]]": ci(durations=[[1, []]]),
            "durations [1]": ci(durations=[1]), "durations str seconds": ci(durations=[["x", TEST_ID]]),
            "durations NaN seconds": ci(durations=[[float("nan"), TEST_ID]]),
            "ci deep nesting": lambda root: (n6dir(root) / "ci-report.json").write_text(
                "[" * 200000 + "]" * 200000, encoding="utf-8"),
            "pass_rate 10**400": raw_baseline(
                '{"version": 1, "groups": {"code/claude": ' + group + ', "pass_rate": 1' + "0" * 400 + "}}}"),
            "pass_rate NaN": base(pass_rate=float("nan")), "finish Infinity": base(finish_seconds_median=float("inf")),
            "pass_rate -1": base(pass_rate=-1), "pass_rate 1.5": base(pass_rate=1.5),
            "interventions -1": base(interventions=-1), "finish 1.7e308 x 1.10": base(finish_seconds_median=1.7e308),
            "ratio 1e300/1e-300": base(post_accept_failures=1e300, kpi_rows_measured=1e-300),
        }
        for name, knock in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                full(root)
                knock(root)
                before = snapshot(root)
                code, text, _ = run(root)
                self.assertEqual(code, 2, text)
                self.assertTrue(text.startswith("REFUSED: "), text)
                self.assertNotIn("OVERALL", text)
                self.assertEqual(snapshot(root), before)

    def test_two_manager_pass_states_the_ci_record_has_no_tree_identity(self):
        full(self.root)
        code, text, lines = run(self.root)
        self.assertEqual(code, 0, text)
        self.assertIn("no commit/tree identity", lines["TWO_MANAGER"])

    def test_non_ascii_evidence_is_escaped_so_no_stdout_encoding_crashes(self):
        full(self.root)
        write_json(n6dir(self.root) / "fallback_drill.json",
                   {"kind": "live", "root": str(self.root), "slice_id": "日本", "lost": 0, "doubled": 0})
        code, text, lines = run(self.root)
        self.assertEqual(code, 0, text)
        self.assertTrue(text.isascii(), text)
        self.assertIn("\\u65e5", lines["FALLBACK_DRILL"])


if __name__ == "__main__":
    unittest.main()
