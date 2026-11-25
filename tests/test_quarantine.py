"""QUARANTINE1: the flaky-test quarantine registry (scripts/quarantine.py). Temp dirs + an injected clock only."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("quarantine", ROOT / "scripts" / "quarantine.py")
q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
UI = "tests.test_ui.UiTests.test_resize"
MSG = r"timeout after 12ms at C:\Users\x\AppData\Local\Temp\tmpab12cd\f.txt (2026-11-24T10:00:01Z, id 0x7f3a)"


def row(sha, run, outcome, hours=1, exc="AssertionError", msg=MSG):
    return {"sha": sha, "run": run, "outcome": outcome, "at": (NOW - timedelta(hours=hours)).isoformat(),
            "exception": exc, "message": msg}


def flaky(test_id=UI, extra=()):
    """Two same-sha flips (sha a and sha b), one signature."""
    return {"test_id": test_id, "results": [row("a", "1", "fail"), row("a", "2", "pass"),
                                            row("b", "3", "fail", msg=MSG.replace("12", "907")),
                                            row("b", "4", "pass"), *extra]}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "ci" / "quarantine.json"
        self.now = NOW
        self.reg = q.Registry(self.path, clock=lambda: self.now)

    def journal(self):
        return [json.loads(x) for x in self.reg.journal.read_text(encoding="utf-8").splitlines()]

    def enter(self, test_id=UI, extra=()):
        out = self.reg.propose(self.reg.load(), flaky(test_id, extra))
        self.assertTrue(out["ok"], out)
        return out


class EligibilityTests(Base):  # R1
    def test_r1_allowed_ui_test_is_eligible(self):
        self.assertEqual(q.eligibility(UI), {"eligible": True, "class": "ui", "reason": "eligible"})
        self.assertTrue(q.evaluate(flaky(), NOW)["ok"])

    def test_r1_protected_classes_are_never_eligible(self):
        for test_id, cls in (("tests.test_privacy_canary.T.test_x", "security"),
                             ("tests.test_billing_integrity.T.test_x", "data_integrity"),
                             ("tests.test_scheduler_recovery.T.test_x", "recovery"),
                             ("tests.test_scheduler_fencing.T.test_x", "concurrency_invariant"),
                             ("tests.test_conveyor.T.test_x", "core_policy_gate"),
                             ("tests.test_quarantine.EligibilityTests.test_x", "core_policy_gate")):
            self.assertEqual(q.eligibility(test_id)["class"], cls)
            out = q.evaluate(flaky(test_id), NOW)
            self.assertEqual((out["ok"], out["reason"]), (False, "protected_class"), test_id)
        self.assertEqual(set(q.PROTECTED_PREFIXES.values()), set(q.PROTECTED_CLASSES))

    def test_r1_unknown_class_or_bad_id_fails_closed(self):
        self.assertEqual(q.eligibility("tests.test_brand_new.T.test_x")["reason"], "unknown_class")
        self.assertEqual(q.eligibility("not an id")["reason"], "bad_test_id")
        self.assertEqual(q.evaluate(flaky("tests.test_brand_new.T.test_x"), NOW)["reason"], "unknown_class")

    def test_r1_protected_match_beats_allowed_match(self):
        with mock.patch.dict(q.ALLOWED_PREFIXES, {"tests.test_privacy": "ui"}):
            self.assertFalse(q.eligibility("tests.test_privacy_canary.T.test_x")["eligible"])
            self.assertTrue(q.eligibility("tests.test_privacy_other.T.test_x")["eligible"])


class EntryTests(Base):  # R2
    def test_r2_two_same_sha_flips_enter_with_cause_and_signature(self):
        out = q.evaluate(flaky(), NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["cause"], {"kind": "same_sha_flip", "flips": [["a", "1"], ["b", "3"]]})
        self.assertEqual(out["signature"], q.signature(UI, "AssertionError", MSG))
        self.assertEqual(out["signature_text"], "AssertionError: timeout after <n>ms at <tmp> (<ts>, id <hex>)")
        self.assertEqual(out["entry_rate"], 0.5)
        self.assertEqual(out["entry_counts"], [2, 4])

    def test_r2_intermittent_failure_without_same_sha_flip_is_not_evidence(self):
        ev = {"test_id": UI, "results": [row("a", "1", "fail"), row("b", "2", "fail"),
                                         row("c", "3", "pass"), row("d", "4", "pass")]}
        self.assertEqual(q.evaluate(ev, NOW)["reason"], "no_same_sha_flip")

    def test_r2_one_flip_or_split_signatures_are_not_enough(self):
        one = {"test_id": UI, "results": [row("a", "1", "fail"), row("a", "2", "pass")]}
        self.assertEqual(q.evaluate(one, NOW)["reason"], "insufficient_flips")
        split = flaky()
        split["results"][2]["exception"] = "TimeoutError"
        self.assertEqual(q.evaluate(split, NOW)["reason"], "insufficient_flips")

    def test_r2_flips_older_than_seven_days_do_not_count(self):
        old = {"test_id": UI, "results": [row("a", "1", "fail", hours=24 * 8), row("a", "2", "pass", hours=24 * 8),
                                          row("b", "3", "fail"), row("b", "4", "pass")]}
        self.assertEqual(q.evaluate(old, NOW)["reason"], "insufficient_flips")
        future = {"test_id": UI, "results": [r | {"at": (NOW + timedelta(hours=1)).isoformat()}
                                             for r in flaky()["results"]]}
        self.assertEqual(q.evaluate(future, NOW)["reason"], "no_same_sha_flip")

    def test_r2_signature_strips_numbers_hex_temp_paths_timestamps_only(self):
        other = "timeout after 3ms at /tmp/pytest-9/x.txt (2025-01-02 03:04:05+00:00, id 0xDEAD)"
        self.assertEqual(q.signature(UI, "AssertionError", MSG), q.signature(UI, "AssertionError", other))
        self.assertEqual(q.normalize_message("uuid 123e4567-e89b-12d3-a456-426614174000 sha deadbeef1"),
                         "uuid <hex> sha <hex>")
        self.assertNotEqual(q.signature(UI, "AssertionError", MSG), q.signature(UI, "KeyError", MSG))
        self.assertNotEqual(q.signature(UI, "AssertionError", MSG),
                            q.signature(UI, "AssertionError", "boom after 12ms"))
        self.assertNotEqual(q.signature(UI, "AssertionError", MSG),
                            q.signature("tests.test_ui.UiTests.test_other", "AssertionError", MSG))

    def test_r2_temp_directly_under_drive_root_is_normalized(self):
        for path in (r"C:\Temp\abc\f.txt", "c:/tmp/x", r"D:\TEMP\x", r"C:\Users\x\AppData\Local\Temp\y"):
            self.assertEqual(q.normalize_message(f"at {path} end"), "at <tmp> end", path)
        self.assertEqual(q.normalize_message(r"at C:\TempFolder\x end"), r"at C:\TempFolder\x end")

    def test_r2_temp_path_does_not_swallow_semicolon_joined_paths(self):  # POLISHQ1 F5
        for text, want in ((r"C:\Temp\a;C:\keep\b", r"<tmp>;C:\keep\b"),
                           (r"C:\keep\b;D:\Temp\a", r"C:\keep\b;<tmp>"),
                           (r"C:\keep\b;/tmp/a", r"C:\keep\b;<tmp>"),
                           ("/private/var/a;/home/keep", "<tmp>;/home/keep"),
                           (r"c:/tmp/x;c:/keep;d:\temp\y", r"<tmp>;c:/keep;<tmp>")):
            self.assertEqual(q.normalize_message(text), want, text)

    def test_r2_normalize_is_linear_on_repeated_drive_prefixes(self):  # POLISHQ1 F6
        for unit in ("C:/", "C:\\", "C:/x"):
            start = time.perf_counter()
            out = q.normalize_message(unit * 50000)
            self.assertLess(time.perf_counter() - start, 1.0, unit)
            self.assertNotIn("<tmp>", out)

    def test_r2_bool_sha_or_run_is_refused(self):  # POLISHQ1 F4
        for key in ("sha", "run"):
            for value in (True, False):
                ev = flaky()
                ev["results"][0][key] = value
                with self.assertRaises(ValueError, msg=f"{key}={value}"):
                    q.evaluate(ev, NOW)
        ev = flaky()
        ev["results"][1]["run"] = 7
        self.assertTrue(q.evaluate(ev, NOW)["ok"])  # a plain int run id is still accepted

    def test_r2_malformed_evidence_is_refused(self):
        for bad in ([], {"test_id": UI}, {"test_id": UI, "results": [{"sha": "a", "run": "1", "outcome": "maybe",
                                                                     "at": NOW.isoformat()}]},
                    {"test_id": UI, "results": [row("a", "1", "fail") | {"at": "2026-11-24T10:00:00"}]},
                    {"test_id": UI, "results": [row("", "1", "fail")]}):
            with self.assertRaises(ValueError):
                q.evaluate(bad, NOW)

    def test_r2_fail_row_without_exception_type_is_refused(self):
        for exc in (None, "", "  ", 123, []):
            ev = flaky()
            ev["results"][0]["exception"] = exc
            with self.assertRaises(ValueError, msg=repr(exc)):
                q.evaluate(ev, NOW)
        ev = flaky()
        del ev["results"][2]["exception"]
        with self.assertRaises(ValueError):
            q.evaluate(ev, NOW)
        ev = flaky()
        for r in ev["results"]:
            if r["outcome"] == "pass":
                del r["exception"]
        self.assertTrue(q.evaluate(ev, NOW)["ok"])  # pass rows need no exception


SIG = q.signature(UI, "AssertionError", MSG)


class SignatureBindingTests(Base):  # R3
    def test_r3_same_signature_is_covered_and_still_runs(self):
        self.enter()
        out = q.check(self.reg.load(), UI, SIG, NOW)
        self.assertEqual((out["covered"], out["blocking"], out["run"], out["reason"]), (True, False, True, "covered"))

    def test_r3_new_or_changed_signature_blocks_again(self):
        self.enter()
        state = self.reg.load()
        for sig in (q.signature(UI, "KeyError", MSG), q.signature(UI, "AssertionError", "other failure")):
            out = q.check(state, UI, sig, NOW)
            self.assertEqual((out["covered"], out["blocking"], out["reason"]), (False, True, "signature_mismatch"))
        out = q.check(state, "tests.test_ui.UiTests.test_other", SIG, NOW)
        self.assertEqual((out["covered"], out["reason"]), (False, "not_quarantined"))


class ExpiryTests(Base):  # R4
    def test_r4_entry_opens_fix_request_and_expires_after_three_days(self):
        self.enter()
        state = self.reg.load()
        self.assertEqual(state["fix_requests"], [{"id": f"FIX-{UI}-{SIG[:8]}", "test_id": UI, "signature": SIG,
                                                  "opened_at": q.iso(NOW), "status": "open"}])
        self.assertEqual(state["entries"][UI]["expires_at"], q.iso(NOW + timedelta(days=3)))
        self.assertTrue(q.check(state, UI, SIG, NOW + timedelta(days=3, seconds=-1))["covered"])
        out = q.check(state, UI, SIG, NOW + timedelta(days=3))
        self.assertEqual((out["covered"], out["blocking"], out["run"], out["reason"]), (False, True, True, "expired"))
        self.assertEqual(q.active_ids(state, NOW + timedelta(days=3)), [])
        self.now = NOW + timedelta(days=3)
        self.assertEqual(self.reg.record_run(self.reg.load(), UI, "fail")["reason"], "not_active")

    def test_r4_extension_without_salih_is_refused_and_journalled(self):
        self.enter()
        for approval in (None, {}, "salih", {"approver": "Salih"}, {"approver": "orchestrator"}):
            out = self.reg.extend(self.reg.load(), UI, approval)
            self.assertEqual((out["ok"], out["reason"], out["generation"]), (False, "extension_needs_salih", 1))
        self.assertEqual(self.reg.load()["entries"][UI]["expires_at"], q.iso(NOW + timedelta(days=3)))
        self.assertEqual([j["reason"] for j in self.journal()], ["entered"] + ["extension_needs_salih"] * 5)

    def test_r4_extension_with_salih_approval_record(self):
        self.enter()
        self.now = NOW + timedelta(days=2)
        out = self.reg.extend(self.reg.load(), UI, {"approver": "salih", "note": "fix in flight"})
        self.assertEqual((out["ok"], out["reason"], out["generation"]), (True, "extended", 2))
        entry = self.reg.load()["entries"][UI]
        self.assertEqual(entry["expires_at"], q.iso(NOW + timedelta(days=5)))
        self.assertEqual(entry["extensions"][0]["approval"]["approver"], "salih")
        missing = self.reg.extend(self.reg.load(), "tests.test_ui.UiTests.test_other", {"approver": "salih"})
        self.assertEqual(missing["reason"], "not_extendable")


class CancelTests(Base):  # R5
    def runs(self, outcomes):
        return [self.reg.record_run(self.reg.load(), UI, o) for o in outcomes]

    def test_r5_three_fails_in_a_row_cancel(self):
        self.enter()
        out = self.runs(["fail", "pass", "fail", "fail", "fail"])
        self.assertEqual([o["reason"] for o in out], ["recorded"] * 4 + ["cancelled"])
        self.assertEqual(out[-1]["cancel_reason"], "fail_streak")
        self.assertEqual(q.check(self.reg.load(), UI, SIG, NOW)["reason"], "cancelled")
        self.assertEqual(self.runs(["pass"])[0]["reason"], "not_active")
        self.assertEqual(self.reg.extend(self.reg.load(), UI, {"approver": "salih"})["reason"], "not_extendable")

    def test_r5_failure_rate_doubled_over_ten_runs_cancels(self):
        extra = [row(f"p{i}", f"x{i}", "pass") for i in range(6)]  # entry rate 2/10 = 0.2
        self.enter(extra=extra)
        self.assertEqual(self.reg.load()["entries"][UI]["entry_rate"], 0.2)
        out = self.runs(["pass", "fail", "fail", "pass", "fail", "fail", "pass", "pass", "pass", "pass"])
        self.assertEqual([o["reason"] for o in out], ["recorded"] * 9 + ["cancelled"])
        self.assertEqual((out[-1]["cancel_reason"], out[-1]["rate"]), ("rate_doubled", 0.4))

    def test_r5_rate_needs_a_full_ten_run_window_and_the_doc_says_so(self):  # POLISHQ1 F2
        self.assertNotIn("(up to)", q.__doc__)
        self.assertIn("last 10 runs (a full window", q.__doc__)
        self.enter(extra=[row(f"p{i}", f"x{i}", "pass") for i in range(6)])  # entry rate 0.2
        out = self.runs(["fail", "fail", "pass", "fail", "fail", "pass", "fail", "fail", "pass"])  # 6/9, no streak
        self.assertEqual({o["reason"] for o in out}, {"recorded"})
        last = self.runs(["fail"])[0]
        self.assertEqual((last["reason"], last["cancel_reason"]), ("cancelled", "rate_doubled"))

    def test_r5_mixed_runs_below_double_rate_stay_active(self):
        self.enter(extra=[row(f"p{i}", f"x{i}", "pass") for i in range(6)])
        out = self.runs(["fail", "fail", "pass", "fail", "pass", "pass", "pass", "pass", "pass", "pass", "pass"])
        self.assertEqual({o["reason"] for o in out}, {"recorded"})
        self.assertEqual(out[-1]["rate"], 0.2)  # last 10 of the 11: F P F P P P P P P P
        self.assertEqual(len(self.reg.load()["entries"][UI]["runs"]), 10)
        self.assertTrue(q.check(self.reg.load(), UI, SIG, NOW)["covered"])

    def test_r5_compare_uses_exact_counts_not_the_rounded_rate(self):
        extra = ([row(f"f{i}", f"y{i}", "fail") for i in range(199)]
                 + [row(f"p{i}", f"x{i}", "pass") for i in range(1806)])  # 201/2009 rounds to 0.1
        self.enter(extra=extra)
        entry = self.reg.load()["entries"][UI]
        self.assertEqual((entry["entry_rate"], entry["entry_counts"]), (0.1, [201, 2009]))
        out = self.runs(["fail", "pass", "pass", "pass", "fail", "pass", "pass", "pass", "pass", "pass"])
        self.assertEqual({o["reason"] for o in out}, {"recorded"})  # 2/10 < 2*201/2009
        self.assertTrue(q.check(self.reg.load(), UI, SIG, NOW)["covered"])


class MaxActiveTests(Base):  # R6
    def test_r6_sixth_entry_is_refused_with_lane_growth_stop(self):
        for i in range(5):
            self.assertNotIn("lane_growth_stop", self.enter(f"tests.test_ui.UiTests.test_{i}"))
        out = self.reg.propose(self.reg.load(), flaky("tests.test_ui.UiTests.test_5"))
        self.assertEqual((out["ok"], out["reason"], out["lane_growth_stop"]), (False, "max_active", True))
        self.assertEqual(self.reg.load()["generation"], 5)
        self.assertNotIn("tests.test_ui.UiTests.test_5", self.reg.load()["entries"])
        self.assertTrue(self.journal()[-1]["lane_growth_stop"])

    def test_r6_expired_entries_free_the_slots(self):
        for i in range(5):
            self.enter(f"tests.test_ui.UiTests.test_{i}")
        self.now = NOW + q.MAX_DAYS
        self.enter("tests.test_ui.UiTests.test_5")

    def expired_plus(self, others):
        self.enter()
        self.now = NOW + q.MAX_DAYS  # UI expired, stored status still "active"
        for i in range(others):
            self.enter(f"tests.test_ui.UiTests.test_{i}")

    def test_r6_extending_an_expired_entry_into_a_sixth_slot_is_refused(self):
        self.expired_plus(5)
        out = self.reg.extend(self.reg.load(), UI, {"approver": "salih"})
        self.assertEqual((out["ok"], out["reason"], out["lane_growth_stop"]), (False, "max_active", True))
        self.assertEqual(self.reg.load()["generation"], 6)
        self.assertEqual(len(q.active_ids(self.reg.load(), self.now)), 5)
        self.assertNotIn(UI, q.active_ids(self.reg.load(), self.now))
        self.assertTrue(self.journal()[-1]["lane_growth_stop"])

    def test_r6_extending_an_expired_entry_with_a_free_slot_succeeds(self):
        self.expired_plus(4)
        out = self.reg.extend(self.reg.load(), UI, {"approver": "salih"})
        self.assertEqual((out["ok"], out["reason"]), (True, "extended"))
        self.assertEqual(len(q.active_ids(self.reg.load(), self.now)), 5)

    def test_r6_extending_a_still_active_entry_at_five_active_is_allowed(self):
        for i in range(5):
            self.enter(f"tests.test_ui.UiTests.test_{i}")
        out = self.reg.extend(self.reg.load(), "tests.test_ui.UiTests.test_0", {"approver": "salih"})
        self.assertEqual((out["ok"], out["reason"]), (True, "extended"))  # no growth, no refusal


class StateTests(Base):  # R7
    def test_r7_write_is_atomic_with_generation_and_journal(self):
        self.enter()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["generation"], 1)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(self.reg.lock.exists())
        self.assertEqual([j["reason"] for j in self.journal()], ["entered"])

    def test_r7_failed_replace_leaves_no_state_and_no_temp(self):
        with mock.patch.object(q.os, "replace", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.reg.propose(self.reg.load(), flaky())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(self.reg.lock.exists())
        self.assertEqual([(j["reason"], j["generation"]) for j in self.journal()], [("write_failed", 0)])
        self.assertEqual(self.journal()[-1]["aborted_generation"], 1)

    def test_r7_no_journal_line_before_the_state_lands(self):  # POLISHQ1 F1
        real, seen = q.os.replace, []

        def replace(src, dst):
            seen.append(self.reg.journal.read_text(encoding="utf-8") if self.reg.journal.exists() else "")
            real(src, dst)

        with mock.patch.object(q.os, "replace", side_effect=replace):
            self.enter()
        self.assertEqual(seen, [""])  # a crash inside replace leaves no ok line for an unlanded generation
        self.assertEqual([(j["reason"], j["generation"]) for j in self.journal()], [("entered", 1)])

    def test_r7_journal_failure_after_landing_writes_no_failure_line(self):  # POLISHQ1 F1
        state = self.reg.load()
        decision, new = q.decide_entry(state, flaky(), NOW)
        with mock.patch.object(q.json, "dumps", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.reg._apply(state, decision, new)
        self.assertEqual(self.reg.load()["generation"], 1)  # landed; never journalled as write_failed
        self.assertEqual(self.reg.journal.read_text(encoding="utf-8"), "")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(self.reg.lock.exists())

    def test_r7_bad_entry_counts_are_refused_at_load(self):  # POLISHQ1 F3
        self.enter()
        good = json.loads(self.path.read_text(encoding="utf-8"))
        for bad in (None, [], [2], [2, 4, 6], "2/4", {"a": 1}, [True, 4], [2, True], [2.0, 4], [2, "4"],
                    [0, 4], [-1, 4], [5, 4], [2, 0], [-2, -1], "missing", "not_a_dict"):
            state = json.loads(json.dumps(good))
            if bad == "missing":
                del state["entries"][UI]["entry_counts"]
            elif bad == "not_a_dict":
                state["entries"][UI] = [2, 4]
            else:
                state["entries"][UI]["entry_counts"] = bad
            self.path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.reg.load()
        self.path.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(self.reg.load()["entries"][UI]["entry_counts"], [2, 4])

    def test_r7_journal_failure_leaves_state_unchanged(self):
        self.enter()
        self.reg.journal = self.dir / "missing" / "j.jsonl"
        with self.assertRaises(OSError):
            self.reg.propose(self.reg.load(), flaky("tests.test_ui.UiTests.test_other"))
        state = self.reg.load()
        self.assertEqual(state["generation"], 1)
        self.assertNotIn("tests.test_ui.UiTests.test_other", state["entries"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(self.reg.lock.exists())

    def test_r7_stale_generation_is_refused(self):
        stale = self.reg.load()
        self.enter()
        out = self.reg.propose(stale, flaky("tests.test_ui.UiTests.test_other"))
        self.assertEqual((out["ok"], out["reason"], out["expected_generation"], out["generation"]),
                         (False, "cas_conflict", 0, 1))
        self.assertNotIn("tests.test_ui.UiTests.test_other", self.reg.load()["entries"])
        self.assertEqual([j["reason"] for j in self.journal()], ["entered", "cas_conflict"])

    def test_r7_concurrent_writers_exactly_one_wins(self):
        n = 8
        barrier, results = threading.Barrier(n), [None] * n

        def writer(i):
            reg = q.Registry(self.path, clock=lambda: NOW)
            state = reg.load()
            barrier.wait()
            results[i] = reg.propose(state, flaky(f"tests.test_ui.UiTests.test_{i}"))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sum(r["ok"] for r in results), 1, results)
        self.assertEqual(sorted(r["reason"] for r in results), ["cas_conflict"] * (n - 1) + ["entered"])
        state = self.reg.load()
        self.assertEqual((state["generation"], len(state["entries"])), (1, 1))
        self.assertEqual(len(self.journal()), n)

    def test_r7_malformed_state_fails_closed(self):
        self.path.parent.mkdir(parents=True)
        for text in ("[]", '{"generation": "1", "entries": {}, "fix_requests": []}', "{not json"):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError, msg=text):
                self.reg.load()

    def test_r7_busy_lock_times_out_and_is_not_stolen(self):
        self.path.parent.mkdir(parents=True)
        self.reg.lock.write_text("", encoding="utf-8")
        with mock.patch.object(q, "LOCK_TRIES", 2), mock.patch.object(q, "LOCK_SLEEP", 0), \
                self.assertRaises(TimeoutError):
            self.reg.propose(self.reg.load(), flaky())
        self.assertTrue(self.reg.lock.exists())
        self.assertFalse(self.path.exists())


class CliTests(Base):  # R8
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = q.main(["--state", str(self.path), *argv], now=self.now)
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def evidence(self, body):
        path = self.dir / "evidence.json"
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return str(path)

    def test_r8_status_on_empty_registry(self):
        code, out, _ = self.cli("status")
        self.assertEqual((code, out["generation"], out["active"]), (0, 0, []))
        self.assertFalse(self.path.exists())

    def test_r8_propose_is_dry_run_unless_write(self):
        ev = self.evidence(flaky())
        code, out, _ = self.cli("propose", "--evidence", ev)
        self.assertEqual((code, out["ok"], out["dry_run"]), (0, True, True))
        self.assertFalse(self.path.parent.exists())
        code, out, _ = self.cli("propose", "--evidence", ev, "--write")
        self.assertEqual((code, out["ok"], out["generation"]), (0, True, 1))
        self.assertNotIn("dry_run", out)
        code, out, _ = self.cli("status")
        self.assertEqual((out["active"], len(out["open_fix_requests"])), ([UI], 1))

    def test_r8_check_exit_code_follows_coverage(self):
        self.enter()
        self.assertEqual(self.cli("check", UI, SIG)[0], 0)
        code, out, _ = self.cli("check", UI, "0" * 20)
        self.assertEqual((code, out["reason"]), (1, "signature_mismatch"))

    def test_r8_bad_input_fails_closed(self):
        code, out, err = self.cli("propose", "--evidence", self.evidence("{not json"), "--write")
        self.assertEqual((code, out), (2, None))
        self.assertIn("quarantine:", err)
        self.assertFalse(self.path.exists())
        code, out, _ = self.cli("propose", "--evidence", self.evidence(flaky("tests.test_conveyor.T.test_x")))
        self.assertEqual((code, out["reason"]), (1, "protected_class"))


if __name__ == "__main__":
    unittest.main()
