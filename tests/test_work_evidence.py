import hashlib
import json
import unittest
from dataclasses import replace

from jarvis.work_evidence import EvidenceReceipt


class EvidenceReceiptTests(unittest.TestCase):
    def receipt(self, **changes):
        fields = dict(plan_hash="a" * 64, diff_sha="b" * 40,
                      check_argv=("python", "-m", "unittest", "tests.example"),
                      exit_code=0, output_sha="c" * 64,
                      base_result={"count": 4, "failures": ["old_failure"]},
                      verdict="pass")
        fields.update(changes)
        return EvidenceReceipt(**fields)

    def test_round_trip(self):
        receipt = self.receipt()
        loaded = EvidenceReceipt.from_json(receipt.to_json())
        self.assertEqual(loaded, receipt)
        self.assertEqual(loaded.to_json(), receipt.to_json())
        self.assertTrue(loaded.verify())

    def test_canonical_form_and_digest(self):
        receipt = self.receipt(base_result={"z": [2, 1], "a": "Proje"})
        expected = json.dumps(json.loads(receipt.canonical_json()), sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(receipt.canonical_json(), expected)
        self.assertEqual(receipt.sha256, hashlib.sha256(expected.encode()).hexdigest())
        self.assertEqual(len(json.loads(expected)), 7)

    def test_mapping_order_does_not_change_hash(self):
        self.assertEqual(self.receipt(base_result={"a": 1, "b": 2}).sha256,
                         self.receipt(base_result={"b": 2, "a": 1}).sha256)

    def test_each_field_tamper_rejected(self):
        receipt = self.receipt()
        changes = dict(plan_hash="d" * 64, diff_sha="e" * 40,
                       check_argv=("python", "other"), exit_code=1,
                       output_sha="f" * 64, base_result={"count": 3}, verdict="fail")
        for field, value in changes.items():
            with self.subTest(field=field):
                altered = replace(receipt, **{field: value})
                self.assertFalse(altered.verify())
                with self.assertRaises(ValueError):
                    EvidenceReceipt.from_json(altered.to_json())
                self.assertNotEqual(self.receipt(**{field: value}).sha256, receipt.sha256)

    def test_digest_tamper_rejected(self):
        for digest in ("0" * 64, "", "x" * 64, 123, "é" * 64):
            with self.subTest(digest=digest):
                self.assertFalse(replace(self.receipt(), sha256=digest).verify())

    def test_nested_tamper_rejected(self):
        receipt = self.receipt()
        receipt.base_result["failures"].append("new_failure")
        self.assertFalse(receipt.verify())

    def test_input_is_copied(self):
        base = {"failures": []}
        argv = ["python"]
        receipt = self.receipt(base_result=base, check_argv=argv)
        base["failures"].append("changed")
        argv.append("changed")
        self.assertEqual(receipt.base_result, {"failures": []})
        self.assertEqual(receipt.check_argv, ("python",))
        self.assertTrue(receipt.verify())

    def test_unknown_and_missing_fields_rejected(self):
        for location in ("receipt", "envelope"):
            for operation in ("add", "remove"):
                with self.subTest(location=location, operation=operation):
                    envelope = json.loads(self.receipt().to_json())
                    target = envelope["receipt"] if location == "receipt" else envelope
                    if operation == "add":
                        target["unknown"] = 1
                    else:
                        target.pop(next(iter(target)))
                    with self.assertRaises(ValueError):
                        EvidenceReceipt.from_json(json.dumps(envelope))

    def test_duplicate_keys_rejected(self):
        serialized = self.receipt().to_json().replace('"exit_code":0',
                                                     '"exit_code":1,"exit_code":0')
        with self.assertRaises(ValueError):
            EvidenceReceipt.from_json(serialized)

    def test_invalid_payload_rejected(self):
        for changes in ({"exit_code": True}, {"check_argv": "python"},
                        {"check_argv": []}, {"check_argv": [1]},
                        {"plan_hash": ""}, {"base_result": float("nan")},
                        {"base_result": {1: "value"}}, {"base_result": (1, 2)}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.receipt(**changes)

    def test_unicode_round_trip(self):
        receipt = self.receipt(base_result={"sonuç": "başarılı"})
        self.assertIn("başarılı", receipt.canonical_json())
        self.assertEqual(EvidenceReceipt.from_json(receipt.to_json()), receipt)

    def test_legacy_receipts_verify_byte_for_byte(self):
        # Written by the code at a083d6d, before N6NOTRUN1; must keep loading unchanged.
        legacy = (
            '{"receipt":{"base_result":{"count":4,"failures":["old_failure"]},"check_argv":'
            '["python","-m","unittest","tests.example"],"diff_sha":"' + "b" * 40 + '",'
            '"exit_code":0,"output_sha":"' + "c" * 64 + '","plan_hash":"' + "a" * 64 + '",'
            '"verdict":"pass"},'
            '"sha256":"7d3380ddf3c184894223d7712c8de6dec9739e0fdb016fa23197b7d372ccf6d5"}',
            '{"receipt":{"base_result":{"failed_ids":[],"total":3},"check_argv":'
            '["python -m unittest tests.x"],"diff_sha":"' + "b" * 40 + '","exit_code":0,'
            '"output_sha":"' + "c" * 64 + '","plan_hash":"' + "a" * 64 + '","verdict":"passed"},'
            '"sha256":"b34a6b30288a2e3b3c988f4a234ce94e631431bccf36293a2b8d7afeb2123cc4"}',
        )
        for serialized in legacy:
            with self.subTest(serialized[-20:]):
                loaded = EvidenceReceipt.from_json(serialized)
                self.assertTrue(loaded.verify())
                self.assertEqual(loaded.to_json(), serialized)
        self.assertEqual(self.receipt().to_json(), legacy[0])

    def test_not_run_and_partial_commit_are_covered_by_the_digest(self):
        # Acceptance 4 / negatives 4-5 (N6NOTRUN1): the verdict and reasons sit in the payload.
        receipt = self.receipt(verdict="NOT_RUN", base_result={
            "checks": {"tests": "PASS", "lint": "NOT_RUN"},
            "reasons": ["lint: no record", "partial_commit: ['jarvis/x.py']"]})
        self.assertIn('"verdict":"NOT_RUN"', receipt.canonical_json())
        self.assertIn("partial_commit", receipt.canonical_json())
        loaded = EvidenceReceipt.from_json(receipt.to_json())
        for _ in range(3):  # verify() is a check, never a promotion
            self.assertTrue(loaded.verify())
        self.assertEqual(loaded.verdict, "NOT_RUN")
        serialized = receipt.to_json()
        edits = {
            "NOT_RUN to PASS": serialized.replace('"verdict":"NOT_RUN"', '"verdict":"PASS"'),
            "one byte": serialized.replace('"verdict":"NOT_RUN"', '"verdict":"NOT_RUX"'),
            "partial dropped": serialized.replace("partial_commit", "partial_commiT"),
            "check flipped": serialized.replace('"lint":"NOT_RUN"', '"lint":"PASS"'),
        }
        for name, edited in edits.items():
            with self.subTest(name):
                self.assertNotEqual(edited, serialized)
                with self.assertRaises(ValueError):
                    EvidenceReceipt.from_json(edited)
        self.assertFalse(replace(receipt, verdict="PASS").verify())

    def test_null_digest_cannot_be_recomputed_on_load(self):
        envelope = json.loads(self.receipt().to_json())
        envelope["sha256"] = None
        with self.assertRaises(ValueError):
            EvidenceReceipt.from_json(json.dumps(envelope))
