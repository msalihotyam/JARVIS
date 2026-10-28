"""PM35 supervisor: one positive and one negative test per ported rule, the D8
proposal allowlist, and a replay over hand-checked ORCH_NOTEBOOK decisions.

The fixture is inline because tests/data is gitignored. No clock, no I/O.
"""

import unittest

from jarvis.work_supervisor import (
    ALLOWED_PROPOSALS, CORE_ACTIONS, Decision, filter_alarms, fix_chain,
    gate_proposal, memory_hold, plan_integration, quota_pacing, supervise,
)


def _pick(name, status="pass", manual=False):
    return {"name": name, "status": status, "manual": manual}


class FixChainTests(unittest.TestCase):
    def test_below_cap_requeues(self):
        d = fix_chain("CDXDFPAUSE", 1)
        self.assertEqual((d.action, d.rule_id), ("requeue", "O5.fix"))
        self.assertIn("fix 2/2", d.reason)

    def test_cap_asks_one_simplification_question(self):
        d = fix_chain("CDXINT94", 2)
        self.assertEqual((d.action, d.rule_id), ("ask_salih", "O5.cap"))
        self.assertEqual(d.options, ("simplify", "stop_target"))
        self.assertEqual(d.default, "stop_target")
        self.assertEqual(fix_chain("X", 7).action, "ask_salih")

    def test_bad_counts_raise(self):
        for bad in (-1, 1.0, True, None, "2"):
            with self.assertRaises(ValueError):
                fix_chain("X", bad)


class AlarmTests(unittest.TestCase):
    def test_new_cause_alarms_once(self):
        out = filter_alarms([("MDREAD", "pending"), ("UIRED5V", "pending"),
                             ("UIRED5V", "stuck")])
        self.assertEqual([d.action for d in out], ["alarm", "suppress", "alarm"])
        self.assertEqual(out[1].rule_id, "O33.duplicate")

    def test_finished_item_never_alarms(self):
        out = filter_alarms([("PM22", "STUCK PM22")], finished={"PM22"})
        self.assertEqual((out[0].action, out[0].rule_id), ("suppress", "O33.finished"))

    def test_past_alarm_suppresses(self):
        out = filter_alarms([("INT66", "cleanup refused")], alarmed={"cleanup refused"})
        self.assertEqual(out[0].action, "suppress")

    def test_string_sets_raise(self):
        with self.assertRaises(ValueError):
            filter_alarms([("PM2", "stuck")], finished="PM22")
        with self.assertRaises(ValueError):
            filter_alarms([("PM2", "stuck")], alarmed="stuck again")


class IntegrationTests(unittest.TestCase):
    def test_blocked_part_is_split_out(self):
        d = plan_integration([_pick("PM10FIX", "blocked"), _pick("ORCHGENFIX2"),
                              _pick("CONVCHERRY")])
        self.assertEqual((d.action, d.rule_id), ("split", "O11.split"))
        self.assertEqual(d.groups, (("ORCHGENFIX2", "CONVCHERRY"),))
        self.assertIn("PM10FIX", d.reason)

    def test_all_pass_batches_manual_apart_and_capped(self):
        picks = [_pick(f"A{i}") for i in range(7)] + [_pick("CONVSAFE", manual=True)]
        d = plan_integration(picks)
        self.assertEqual((d.action, d.rule_id), ("batch", "O11.batch"))
        self.assertEqual(d.groups[0], ("CONVSAFE",))
        self.assertEqual([len(g) for g in d.groups[1:]], [6, 1])

    def test_small_batch_waits_until_old(self):
        picks = [_pick("A"), _pick("B")]
        self.assertEqual(plan_integration(picks, oldest_wait_s=600).rule_id, "O11.small")
        self.assertEqual(plan_integration(picks, oldest_wait_s=3600).action, "batch")

    def test_running_int_and_nothing_ready_wait(self):
        self.assertEqual(plan_integration([_pick("A")] * 0 + [_pick("A")],
                                          int_running=True).rule_id, "O13.running")
        self.assertEqual(plan_integration([_pick("A", "blocked")]).rule_id,
                         "O11.none_ready")

    def test_missing_manual_key_counts_as_manual(self):
        d = plan_integration([{"name": "A", "status": "pass"}, _pick("B"), _pick("C")])
        self.assertEqual(d.groups, (("A",), ("B", "C")))

    def test_bad_names_raise(self):
        for picks in ([_pick("A"), _pick("A")], [_pick("")], [_pick(None)]):
            with self.assertRaises(ValueError):
                plan_integration(picks)
        for bad in (0, -1, 2.0, True):
            with self.assertRaises(ValueError):
                plan_integration([_pick("A")], max_batch=bad)

    def test_unknown_wait_waits(self):
        picks = [_pick("A"), _pick("B")]
        for wait in (None, float("nan"), -1, "3600"):
            self.assertEqual(plan_integration(picks, oldest_wait_s=wait).rule_id,
                             "O11.unknown_wait")


class QuotaTests(unittest.TestCase):
    def test_limit_is_hard_stop(self):
        d = quota_pacing(90, 95, 12)
        self.assertEqual((d.action, d.rule_id), ("pause", "O23.limit"))
        self.assertEqual(quota_pacing(89.9, 95, 12).action, "dispatch")

    def test_highest_reading_counts(self):
        self.assertEqual(quota_pacing((66, 30), 66, 14).action, "dispatch")
        self.assertEqual(quota_pacing([30, 91], 95, 14).rule_id, "O23.limit")

    def test_unknown_reading_or_hour_pauses(self):
        for used in (None, "50", True, float("nan"), (), [50, None], -5, [50, -1],
                     10**400):
            self.assertEqual(quota_pacing(used, 50, 12).rule_id, "O23.unknown")
        for limit in (float("nan"), None, 0, -10):
            self.assertEqual(quota_pacing(50, 50, 12, limit_pct=limit).rule_id,
                             "O23.unknown")
        for hour in (None, 24, -1, 12.0, True):
            self.assertEqual(quota_pacing(50, 50, hour).rule_id, "O23.unknown")

    def test_ahead_of_line_non_claude_only(self):
        self.assertEqual(quota_pacing(51, 44.6, 12).rule_id, "pace.ahead")
        self.assertEqual(quota_pacing(47, 44, 12).action, "dispatch")

    def test_rate_over_target(self):
        self.assertEqual(quota_pacing(68, 68, 15, 0.67, 0.54).rule_id, "pace.rate")
        self.assertEqual(quota_pacing(68, 68, 15, 0.64, 0.54).action, "dispatch")

    def test_night_non_claude_only(self):
        self.assertEqual(quota_pacing(20, 20, 2).rule_id, "pace.night")
        self.assertEqual(quota_pacing(20, 20, 7).rule_id, "pace.night")
        self.assertEqual(quota_pacing(20, 20, 8).rule_id, "pace.on_line")
        self.assertEqual(quota_pacing(20, 20, 1).rule_id, "pace.on_line")

    def test_unknown_line_non_claude_only(self):
        self.assertEqual(quota_pacing(20, None, 12).rule_id, "pace.unknown_line")


class MemoryTests(unittest.TestCase):
    def test_low_memory_holds(self):
        self.assertEqual(memory_hold(2.9, False).rule_id, "mem.hold")
        self.assertEqual(memory_hold(3.0, False).action, "dispatch")

    def test_hysteresis_needs_margin_and_time(self):
        self.assertEqual(memory_hold(4.4, True, 600).rule_id, "mem.hysteresis")
        self.assertEqual(memory_hold(4.5, True, 299).rule_id, "mem.hysteresis")
        self.assertEqual(memory_hold(4.5, True, None).rule_id, "mem.hysteresis")
        self.assertEqual(memory_hold(4.5, True, 300).rule_id, "mem.ok")

    def test_unknown_memory_holds(self):
        for free in (None, "4", float("inf"), False):
            self.assertEqual(memory_hold(free, False).rule_id, "mem.unknown")
        self.assertEqual(memory_hold(4.0, False, low_gb=float("nan")).rule_id, "mem.unknown")
        self.assertEqual(memory_hold(4.0, False, margin_gb=-1).rule_id, "mem.unknown")

    def test_non_bool_held_fails_closed(self):
        self.assertEqual(memory_hold(4.0, None, 600).action, "wait")


class ProposalTests(unittest.TestCase):
    def test_allowlisted_proposals_pass(self):
        for action in ALLOWED_PROPOSALS:
            d = gate_proposal({"action": action, "reason": "r" * 500})
            self.assertEqual((d.action, d.rule_id), (action, "D8.proposal"))
            self.assertEqual(len(d.reason), 200)
        self.assertEqual(gate_proposal("wait").reason, "model proposal")

    def test_outside_allowlist_needs_input(self):
        for bad in ("Pause", " wait", "delete", "dispatch", "batch", None, 5, ["wait"],
                    {"reason": "x"}, {"action": "push"}):
            d = gate_proposal(bad)
            self.assertEqual((d.action, d.rule_id), ("needs_input", "D8.refused"))
            self.assertEqual(d.options, tuple(sorted(ALLOWED_PROPOSALS)))
            self.assertEqual(d.default, "wait")
            self.assertTrue(d.question)


class SuperviseTests(unittest.TestCase):
    def test_unknown_key_needs_input(self):
        d = supervise({"shell": "rm -rf"})
        self.assertEqual((d.action, d.rule_id), ("needs_input", "D8.unknown"))

    def test_operator_pause_wins(self):
        d = supervise({"operator_pause": True, "proposal": "requeue",
                       "quota": {"used_pct": 10, "line_pct": 10, "hour": 12}})
        self.assertEqual(d.rule_id, "operator.pause")

    def test_gates_precede_work(self):
        d = supervise({"memory": {"free_gb": 2.0, "held": False}, "fix": {
            "target": "X", "failed_fixes": 0}})
        self.assertEqual(d.rule_id, "mem.hold")
        d = supervise({"memory": {"free_gb": 5.0, "held": False}, "fix": {
            "target": "X", "failed_fixes": 0}})
        self.assertEqual(d.rule_id, "O5.fix")

    def test_idle(self):
        self.assertEqual(supervise({}).action, "none")
        self.assertEqual(supervise({"memory": {"free_gb": 5.0, "held": False}}).rule_id,
                         "mem.ok")


def _q(used, line, hour, rate=None, target=None):
    return {"quota": {"used_pct": used, "line_pct": line, "hour": hour,
                      "rate_pct_h": rate, "target_pct_h": target}}


def _m(free, held, ok=0):
    return {"memory": {"free_gb": free, "held": held, "ok_for_s": ok}}


def _f(target, n):
    return {"fix": {"target": target, "failed_fixes": n}}


def _a(item, cause, finished=(), alarmed=()):
    return {"alarm": {"item": item, "cause": cause, "finished": set(finished),
                      "alarmed": set(alarmed)}}


# (ORCH_NOTEBOOK line, situation, the orchestrator's recorded action)
FIXTURE = [
    ("122", _f("CDXINT54H", 2), "ask_salih"),
    ("129", _f("CDXCONVB", 1), "requeue"),
    ("151", _f("CDXDFPAUSE", 0), "requeue"),
    ("187", _f("INT58", 1), "requeue"),
    ("190", _f("INT58B", 2), "ask_salih"),
    ("204", _f("CDXINT61", 0), "requeue"),
    ("389", _f("CDXINT94", 2), "ask_salih"),
    ("400", _a("PM22", "STUCK", finished={"PM22"}), "suppress"),
    ("284a", _a("CDXINT66", "cleanup refused", alarmed={"cleanup refused"}), "suppress"),
    ("284b", _a("MDREAD", "pending"), "alarm"),
    ("228a", _a("V21HARD2B", "pending", finished={"V21HARD2B"}), "suppress"),
    ("228b", _a("UIRED5V", "UIRED5V pending", alarmed={"MDREAD pending"}), "alarm"),
    ("387", {"integration": {"picks": [_pick("PM10FIX", "blocked"), _pick("CONVCHERRY"),
                                       _pick("ORCHGENFIX2")]}}, "split"),
    ("395", {"integration": {"picks": [_pick(n) for n in (
        "PM10", "PM10FIX", "PM10FIX2", "PM10FIX3", "PM22")]}}, "batch"),
    ("419", {"integration": {"picks": [_pick("PM16")], "int_running": True}}, "wait"),
    ("136", {"integration": {"picks": [_pick("INT54R2"), _pick("INTCONV2")],
                             "oldest_wait_s": 2820}}, "batch"),
    ("140", _q(90, None, 21), "pause"),
    ("271", _q(10, 8, 16), "dispatch"),
    ("287", _q(20, 17, 3), "non_claude_only"),
    ("355", _q(51, 44.6, 1), "pause"),
    ("370", _q(57, None, 10, 1.8, 0.55), "non_claude_only"),
    ("406", _q(63, None, 2), "non_claude_only"),
    ("424", _q(68, None, 15, 0.67, 0.54), "non_claude_only"),
    ("101", _m(2.6, False), "wait"),
    ("102", _m(4.8, True, 300), "dispatch"),
    ("117", _m(2.9, False), "wait"),
    ("128", _m(3.0, True), "wait"),
    ("130", _m(3.8, False), "dispatch"),
    ("215", _m(4.0, True, 600), "wait"),
    ("312", _m(4.2, True, 600), "wait"),
    ("399", _m(3.7, True), "wait"),
    ("439", _m(3.7, False), "dispatch"),
    ("447", _m(3.8, True), "wait"),
    ("64", {"operator_pause": True}, "pause"),
    ("163", {"operator_pause": True}, "pause"),
    ("220", {"operator_pause": True}, "pause"),
    ("206", {"proposal": {"action": "ask_salih", "reason": "CONVSAFE manual key"}},
     "ask_salih"),
    ("273", {"proposal": "ask_salih"}, "ask_salih"),
]
# 136: the orchestrator moved a 2-pick INT forward after 47 min (O11 threshold is 60).
# 355: the orchestrator paused overnight; the later night plan (396) runs non-Claude.
KNOWN_MISSES = {"136", "355"}


class ReplayTests(unittest.TestCase):
    def test_agreement_with_orchestrator(self):
        decisions = [(ref, supervise(s), want) for ref, s, want in FIXTURE]
        misses = {ref for ref, d, want in decisions if d.action != want}
        agreement = 1 - len(misses) / len(FIXTURE)
        self.assertEqual(len(FIXTURE), 38)
        self.assertEqual(misses, KNOWN_MISSES)
        self.assertGreaterEqual(agreement, 0.9)  # 36/38 = 94.7 %
        for _, d, _ in decisions:
            self.assertIsInstance(d, Decision)
            self.assertIn(d.action, CORE_ACTIONS)
            self.assertTrue(d.rule_id and d.reason)

    def test_proposals_never_leave_allowlist(self):
        for ref, s, _ in FIXTURE:
            if "proposal" in s:
                self.assertIn(supervise(s).action, ALLOWED_PROPOSALS | {"needs_input"})


if __name__ == "__main__":
    unittest.main()
