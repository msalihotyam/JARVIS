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

    def test_null_digest_cannot_be_recomputed_on_load(self):
        envelope = json.loads(self.receipt().to_json())
        envelope["sha256"] = None
        with self.assertRaises(ValueError):
            EvidenceReceipt.from_json(json.dumps(envelope))
