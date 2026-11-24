"""N6GROUPGATE1: Phase 4 readiness check per behaviour group (scripts/n6_group_gate.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("n6_group_gate", ROOT / "scripts" / "n6_group_gate.py")
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["n6_group_gate"] = gate
_SPEC.loader.exec_module(gate)

GOOD_E8 = {"slices": 10, "worse": []}


def rows(group, n, origin="live", prefix="e", **extra):
    base = {"group": group, "n6_decision": "fix", "conveyor_decision": "fix", "safe": True, "origin": origin}
    return [{**base, "event_id": f"{prefix}{i}", **extra} for i in range(n)]


def inputs(group="R17R18"):
    """Everything READY for ``group`` alone; earlier groups taken over."""
    earlier = list(gate.GROUPS[:gate.GROUPS.index(group)])
    return rows(group, 5), {group: GOOD_E8}, [{"group": group, "passed": True}], earlier


class GroupGateTests(unittest.TestCase):
    def verdict(self, group, shadow, e8, drills, taken_over):
        return gate.check(shadow, e8, drills, taken_over)[group]

    def test_full_ready(self):
        verdict, reasons = self.verdict("R17R18", *inputs())
        self.assertEqual((verdict, reasons), (gate.READY, []))

    def test_four_events_not_ready(self):
        shadow, e8, drills, done = inputs()
        verdict, reasons = self.verdict("R17R18", shadow[:4], e8, drills, done)
        self.assertEqual(verdict, gate.NOT_READY)
        self.assertIn("4 real shadow events, needs 5", reasons)

    def test_duplicate_event_ids_count_once(self):
        shadow, e8, drills, done = inputs()
        verdict, _ = self.verdict("R17R18", shadow[:4] + shadow[:1], e8, drills, done)
        self.assertEqual(verdict, gate.NOT_READY)

    def test_one_unsafe_disagreement_not_ready(self):
        shadow, e8, drills, done = inputs()
        bad = rows("R17R18", 1, prefix="x", conveyor_decision="skip", safe=False)
        verdict, reasons = self.verdict("R17R18", shadow + bad, e8, drills, done)
        self.assertEqual(verdict, gate.NOT_READY)
        self.assertIn("unsafe disagreement 1: x0", reasons)

    def test_unsafe_disagreement_counts_from_non_live_rows(self):
        shadow, e8, drills, done = inputs()
        bad = rows("R17R18", 1, origin="rehearsal", prefix="x", conveyor_decision="skip", safe=False)
        self.assertEqual(self.verdict("R17R18", shadow + bad, e8, drills, done)[0], gate.NOT_READY)

    def test_safe_disagreement_and_non_bool_safe(self):
        shadow, e8, drills, done = inputs()
        ok = rows("R17R18", 1, prefix="s", conveyor_decision="skip", safe=True)
        self.assertEqual(self.verdict("R17R18", shadow + ok, e8, drills, done)[0], gate.READY)
        bad = rows("R17R18", 1, prefix="t", conveyor_decision="skip", safe="yes")
        self.assertEqual(self.verdict("R17R18", shadow + bad, e8, drills, done)[0], gate.NOT_READY)

    def test_fixture_events_not_real(self):
        _, e8, drills, done = inputs()
        verdict, reasons = self.verdict("R17R18", rows("R17R18", 5, origin="fixture"), e8, drills, done)
        self.assertEqual(verdict, gate.NOT_READY)
        self.assertIn("0 real shadow events, needs 5", reasons)

    def test_rehearsal_events_not_real(self):
        _, e8, drills, done = inputs()
        verdict, _ = self.verdict("R17R18", rows("R17R18", 5, origin="rehearsal"), e8, drills, done)
        self.assertEqual(verdict, gate.NOT_READY)

    def test_missing_origin_not_real(self):
        shadow, e8, drills, done = inputs()
        for row in shadow:
            del row["origin"]
        self.assertEqual(self.verdict("R17R18", shadow, e8, drills, done)[0], gate.NOT_READY)

    def test_unknown_group_raises(self):
        shadow, e8, drills, done = inputs()
        with self.assertRaises(gate.GateError):
            gate.check(shadow + rows("R99", 1), e8, drills, done)
        with self.assertRaises(gate.GateError):
            gate.check(shadow, {"R99": GOOD_E8}, drills, done)
        with self.assertRaises(gate.GateError):
            gate.check(shadow, e8, [{"group": "R99", "passed": True}], done)
        with self.assertRaises(gate.GateError):
            gate.check(shadow, e8, drills, ["R99"])
        self.assertTrue(issubclass(gate.GateError, ValueError))

    def test_malformed_row_raises(self):
        shadow, e8, drills, done = inputs()
        with self.assertRaises(gate.GateError):
            gate.check([{"group": "R17R18", "event_id": "e"}], e8, drills, done)
        broken = dict(shadow[0], event_id="")
        with self.assertRaises(gate.GateError):
            gate.check([broken], e8, drills, done)
        for key in ("n6_decision", "conveyor_decision"):
            for empty in (None, ""):
                with self.subTest(key=key, empty=empty), self.assertRaises(gate.GateError):
                    gate.check([dict(shadow[0], **{key: empty})], e8, drills, done)

    def test_failed_drill_receipt_outweighs_pass(self):
        shadow, e8, drills, done = inputs()
        failed = drills + [{"group": "R17R18", "passed": False}]
        self.assertEqual(self.verdict("R17R18", shadow, e8, failed, done)[0], gate.NOT_READY)

    def test_missing_e8_not_ready(self):
        shadow, _, drills, done = inputs()
        verdict, reasons = self.verdict("R17R18", shadow, {}, drills, done)
        self.assertEqual(verdict, gate.NOT_READY)
        self.assertIn("no E8 reading", reasons)

    def test_e8_short_window_worse_or_malformed_not_ready(self):
        shadow, _, drills, done = inputs()
        for reading in ({"slices": 9, "worse": []}, {"slices": 10, "worse": ["pass_rate"]},
                        {"slices": "10", "worse": []}, {"slices": 10}, "ok"):
            with self.subTest(reading=reading):
                verdict, _ = self.verdict("R17R18", shadow, {"R17R18": reading}, drills, done)
                self.assertEqual(verdict, gate.NOT_READY)

    def test_drill_receipt_required(self):
        shadow, e8, _, done = inputs()
        for drills in ([], [{"group": "R17R18", "passed": False}], [{"group": "R17R18", "passed": "true"}]):
            with self.subTest(drills=drills):
                verdict, reasons = self.verdict("R17R18", shadow, e8, drills, done)
                self.assertEqual(verdict, gate.NOT_READY)
                self.assertIn("no passing R34 fallback drill receipt", reasons)


class OrderTests(unittest.TestCase):
    def test_takeover_order_is_fixed_and_r19r20_last(self):
        self.assertEqual(gate.GROUPS, ("R17R18", "R21", "R31", "R26", "R10", "R19R20"))

    def test_later_group_not_ready_until_earlier_taken_over(self):
        shadow, e8, drills, _ = inputs("R31")
        verdict, reasons = gate.check(shadow, e8, drills, ["R17R18"])["R31"]
        self.assertEqual(verdict, gate.NOT_READY)
        self.assertEqual(reasons, ["earlier group R21 has not taken over"])
        self.assertEqual(gate.check(shadow, e8, drills, ["R17R18", "R21"])["R31"], (gate.READY, []))

    def test_r19r20_needs_all_five_earlier_groups(self):
        shadow, e8, drills, done = inputs("R19R20")
        self.assertEqual(len(done), 5)
        self.assertEqual(gate.check(shadow, e8, drills, done)["R19R20"], (gate.READY, []))
        verdict, reasons = gate.check(shadow, e8, drills, done[:4])["R19R20"]
        self.assertEqual((verdict, reasons), (gate.NOT_READY, ["earlier group R10 has not taken over"]))

    def test_ready_evidence_for_later_group_does_not_bypass_order(self):
        shadow, e8, drills, _ = inputs("R21")
        result = gate.check(shadow, e8, drills, [])
        self.assertEqual(result["R21"][0], gate.NOT_READY)
        self.assertEqual(list(result), list(gate.GROUPS))

    def test_out_of_order_taken_over_raises(self):
        shadow, e8, drills, _ = inputs()
        with self.assertRaises(gate.GateError):
            gate.check(shadow, e8, drills, ["R21"])


class CliTests(unittest.TestCase):
    def run_cli(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.json"
            path.write_text(payload, encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = gate.main(["--input", str(path)])
        return code, out.getvalue()

    def test_cli_prints_verdicts(self):
        shadow, e8, drills, done = inputs()
        code, out = self.run_cli(json.dumps({"shadow": shadow, "e8": e8, "drills": drills, "taken_over": done}))
        self.assertEqual(code, 0)
        self.assertIn("R17R18: READY\n", out)
        self.assertIn("R21: NOT_READY\n", out)

    def test_cli_refuses_bad_input(self):
        for payload in ("[]", "{not json", json.dumps({"shadow": rows("R99", 1)})):
            with self.subTest(payload=payload):
                code, out = self.run_cli(payload)
                self.assertEqual(code, 2)
                self.assertTrue(out.startswith("REFUSED: "))


if __name__ == "__main__":
    unittest.main()
