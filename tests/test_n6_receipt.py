"""N6-10: receipts bind to runner-signed invocation records (G09, G11, R06).

Negative first. No clock, no subprocess, no database: CheckResult is built
directly and every record is signed with a test key.
"""

import hashlib
import json
import unittest

from jarvis.work_checks import CheckRefused, CheckResult, invocation_record
from jarvis.work_evidence import (
    FAIL, INVALID, NOT_RUN, PARTIAL, PASS, EvidenceReceipt, InvocationRecord,
    receipt_verdict,
)

KEY = b"runner-key-for-tests"
SHA = "a" * 40
OTHER = "b" * 40
TESTS = ("python", "-m", "unittest", "tests.test_x")
LINT = ("ruff", "check", "jarvis")
REQUIRED = {"tests": TESTS, "lint": LINT}
FILES = ("jarvis/x.py",)


def signed(check_id="tests", argv=TESTS, sha=SHA, exit_code=0, key=KEY):
    return InvocationRecord(check_id, argv, sha, exit_code).sign(key)


def both(**changes):
    return [signed("tests", TESTS, **changes), signed("lint", LINT, **changes)]


def judge(records, *, required=REQUIRED, sha=SHA, changed=FILES, committed=FILES, key=KEY):
    return receipt_verdict(required, records, sha, key=key,
                           changed_files=changed, commit_files=committed)


def resign(record_json, **payload_changes):
    envelope = json.loads(record_json)
    envelope["record"].update(payload_changes)
    return json.dumps(envelope)


class N6ReceiptTests(unittest.TestCase):
    def assert_not_pass(self, records, **kw):
        result = judge(records, **kw)
        self.assertNotEqual(result.verdict, PASS)
        return result

    def test_neg_report_text_pass_without_record_is_not_pass(self):
        for text in ("PASS", "STATUS: PASS", '{"verdict":"PASS"}', ""):
            with self.subTest(text=text):
                self.assert_not_pass([text, *both()])
                self.assert_not_pass(text)
        result = judge([])
        self.assertEqual(result.verdict, NOT_RUN)
        self.assertEqual(result.checks, {"tests": NOT_RUN, "lint": NOT_RUN})

    def test_neg_partial_commit_is_not_pass(self):
        self.assertEqual(judge(both(sha=OTHER)).verdict, PARTIAL)
        mixed = judge([signed("tests"), signed("lint", LINT, sha=OTHER)])
        self.assertEqual(mixed.verdict, PARTIAL)
        self.assertEqual(mixed.checks, {"tests": PASS, "lint": PARTIAL})
        outside = judge(both(), changed=FILES + ("jarvis/extra.py",))
        self.assertEqual(outside.verdict, PARTIAL)
        self.assertIn("jarvis/extra.py", " ".join(outside.reasons))
        for bad in ("jarvis/x.py", None, 3):
            with self.subTest(changed=bad):
                self.assertEqual(judge(both(), changed=bad).verdict, INVALID)

    def test_neg_missing_required_check_is_not_run(self):
        result = judge([signed("tests")])
        self.assertEqual(result.verdict, NOT_RUN)
        self.assertEqual(result.checks, {"tests": PASS, "lint": NOT_RUN})
        self.assertIn("lint: no record", result.reasons)
        # A record for a check nobody asked for does not stand in for one.
        self.assertEqual(judge([signed("tests"), signed("extra", LINT)]).verdict, NOT_RUN)

    def test_neg_no_checks_at_all_is_refused(self):
        for required in ({}, [], None, {"tests": ()}, {"": TESTS}, {"tests": "python"}):
            with self.subTest(required=required), self.assertRaises(ValueError):
                judge(both(), required=required)
        for sha in ("A" * 40, "a" * 39, "g" * 40, "", None):
            with self.subTest(final_sha=sha), self.assertRaises(ValueError):
                judge(both(), sha=sha)
        for key in (b"", "runner-key-for-tests", None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                judge(both(), key=key)

    def test_neg_record_for_another_sha_is_not_pass(self):
        for sha in (OTHER, "a" * 64):
            with self.subTest(sha=sha):
                self.assertEqual(judge(both(sha=sha)).verdict, PARTIAL)
        for sha in ("A" * 40, "a" * 12, "z" * 40):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                signed(sha=sha)

    def test_neg_malformed_record_is_not_pass(self):
        good = signed()
        envelope = json.loads(good)
        plain = hashlib.sha256(InvocationRecord("lint", LINT, SHA, 0)
                               .canonical_json().encode()).hexdigest()
        receipt = EvidenceReceipt("a" * 64, SHA, TESTS, 0, "c" * 64, {}, "PASS")
        variants = {
            "not json": "{",
            "dict": envelope,
            "none": None,
            "bytes": good.encode(),
            "int": 0,
            "evidence receipt": receipt,
            "evidence receipt json": receipt.to_json(),
            "extra field": json.dumps({**envelope, "x": 1}),
            "missing hmac": json.dumps({"record": envelope["record"]}),
            "extra record field": resign(good, verdict="PASS"),
            "duplicate key": good.replace('{"hmac"', '{"hmac":"0","hmac"', 1),
            "hmac not str": json.dumps({**envelope, "hmac": 1}),
            "hmac non ascii": json.dumps({**envelope, "hmac": "é" * 64}),
            "plain digest": json.dumps({"record": json.loads(
                InvocationRecord("lint", LINT, SHA, 0).canonical_json()), "hmac": plain}),
            "other key": signed("lint", LINT, key=b"worker-key"),
            "field changed": resign(signed(exit_code=1), exit_code=0),
            "sha changed": resign(signed(sha=OTHER), commit_sha=SHA),
            "exit true": resign(good, exit_code=True),
            "exit str": resign(good, exit_code="0"),
            "exit float": resign(good, exit_code=0.0),
            "argv str": resign(good, argv="python"),
            "argv empty": resign(good, argv=[]),
            "id empty": resign(good, check_id=""),
        }
        for name, record in variants.items():
            with self.subTest(name):
                result = judge([record, *both()])
                self.assertEqual(result.verdict, INVALID)
                self.assertTrue(result.reasons)

    def test_neg_forged_record_fails_authentication_alone(self):
        # Each forgery is judged with no second record for its check id, so
        # only the HMAC check can stop it (CDX-N6-10 F1).
        plain = hashlib.sha256(InvocationRecord("lint", LINT, SHA, 0)
                               .canonical_json().encode()).hexdigest()
        forged = {
            "other key": signed("lint", LINT, key=b"worker-key"),
            "plain digest": json.dumps({"record": json.loads(
                InvocationRecord("lint", LINT, SHA, 0).canonical_json()), "hmac": plain}),
            "field changed": resign(signed(exit_code=1), exit_code=0),
            "sha changed": resign(signed(sha=OTHER), commit_sha=SHA),
        }
        honest = {"tests": signed("tests"), "lint": signed("lint", LINT)}
        for name, record in forged.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    InvocationRecord.from_signed(record, KEY)
                check_id = json.loads(record)["record"]["check_id"]
                others = [r for cid, r in honest.items() if cid != check_id]
                result = judge([record, *others])
                self.assertNotEqual(result.verdict, PASS)
                self.assertEqual(result.checks[check_id], INVALID)

    def test_neg_wrong_command_duplicate_and_failed_runs(self):
        wrong = judge([signed("tests", LINT), signed("lint", LINT)])
        self.assertEqual(wrong.checks["tests"], INVALID)
        self.assertEqual(judge([*both(), signed("tests")]).verdict, INVALID)
        for code in (1, -1, None):
            with self.subTest(exit_code=code):
                self.assertEqual(judge([signed(exit_code=code), signed("lint", LINT)]).verdict, FAIL)

    def test_neg_runner_record_refuses_mismatch(self):
        def result(argv, exit_code=0, timed_out=False):
            return CheckResult(argv=argv, runner="python", exit_code=exit_code, duration_s=0.0,
                               stdout="", stderr="", truncated=False, timed_out=timed_out)
        resolved = ("C:/Python/python.exe", *TESTS[1:])
        with self.assertRaises(CheckRefused):
            invocation_record("tests", TESTS, result(("C:/py.exe", "-m", "other")), SHA, key=KEY)
        for bad_sha, bad_key in ((OTHER.upper(), KEY), (SHA, b"")):
            with self.subTest(sha=bad_sha), self.assertRaises(CheckRefused):
                invocation_record("tests", TESTS, result(resolved), bad_sha, key=bad_key)
        with self.assertRaises(CheckRefused):
            invocation_record("tests", TESTS, {"argv": resolved}, SHA, key=KEY)
        timed_out = invocation_record("tests", TESTS, result(resolved, 0, True), SHA, key=KEY)
        self.assertEqual(judge([timed_out, signed("lint", LINT)]).verdict, FAIL)

    def test_ok_all_required_checks_recorded_on_final_sha(self):
        resolved = CheckResult(argv=("C:/Python/python.exe", *TESTS[1:]), runner="python",
                               exit_code=0, duration_s=0.0, stdout="", stderr="",
                               truncated=False, timed_out=False)
        record = invocation_record("tests", TESTS, resolved, SHA, key=KEY)
        result = judge((record, signed("lint", LINT)))
        self.assertEqual(result.verdict, PASS)
        self.assertEqual(result.checks, {"tests": PASS, "lint": PASS})
        self.assertEqual(result.reasons, ())
        self.assertEqual(InvocationRecord.from_signed(record, KEY).argv, TESTS)


if __name__ == "__main__":
    unittest.main()
