"""PM35 supervisor: one positive and one negative test per ported rule, the D8
proposal allowlist, and a replay over hand-checked ORCH_NOTEBOOK decisions.

The fixture is inline because tests/data is gitignored. No clock, no I/O.
"""

import unittest

from jarvis.work_supervisor import (
    ALLOWED_PROPOSALS, CORE_ACTIONS, PRECEDENCE, Decision, filter_alarms, fix_chain,
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
        self.assertEqual(d.groups[-1], ("CONVSAFE",))
        self.assertEqual([len(g) for g in d.groups[:-1]], [6, 1])

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
        self.assertEqual(d.groups, (("B", "C"), ("A",)))

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
# 355: hour 1 is outside NIGHT (2, 8); the branch is pace.ahead (51 vs 44.6) and the
# orchestrator paused overnight instead; the later night plan (396) runs non-Claude.
KNOWN_MISSES = {"136", "355"}
# 370/406/424 were recorded without a pacing line, so they hit pace.unknown_line;
# pace.rate and pace.night with a line are covered in QuotaTests.
RULE_IDS = {
    "122": "O5.cap", "129": "O5.fix", "151": "O5.fix", "187": "O5.fix", "190": "O5.cap",
    "204": "O5.fix", "389": "O5.cap", "400": "O33.finished", "284a": "O33.duplicate",
    "284b": "O33.new", "228a": "O33.finished", "228b": "O33.new", "387": "O11.split",
    "395": "O11.batch", "419": "O13.running", "136": "O11.small", "140": "O23.limit",
    "271": "pace.on_line", "287": "pace.night", "355": "pace.ahead",
    "370": "pace.unknown_line", "406": "pace.unknown_line", "424": "pace.unknown_line",
    "101": "mem.hold", "102": "mem.ok", "117": "mem.hold", "128": "mem.hysteresis",
    "130": "mem.ok", "215": "mem.hysteresis", "312": "mem.hysteresis",
    "399": "mem.hysteresis", "439": "mem.ok", "447": "mem.hysteresis",
    "64": "operator.pause", "163": "operator.pause", "220": "operator.pause",
    "206": "D8.proposal", "273": "D8.proposal",
}


class ReplayTests(unittest.TestCase):
    def test_agreement_with_orchestrator(self):
        decisions = [(ref, supervise(s), want) for ref, s, want in FIXTURE]
        misses = {ref for ref, d, want in decisions if d.action != want}
        agreement = 1 - len(misses) / len(FIXTURE)
        self.assertEqual(len(FIXTURE), 38)
        self.assertEqual(misses, KNOWN_MISSES)
        self.assertGreaterEqual(agreement, 0.9)  # 36/38 = 94.7 %
        self.assertEqual({ref: d.rule_id for ref, d, _ in decisions}, RULE_IDS)
        for _, d, _ in decisions:
            self.assertIsInstance(d, Decision)
            self.assertIn(d.action, CORE_ACTIONS)
            self.assertTrue(d.rule_id and d.reason)
        groups = {ref: d.groups for ref, d, _ in decisions if d.groups}
        self.assertEqual(groups, {"387": (("CONVCHERRY", "ORCHGENFIX2"),),
                                  "395": (("PM10", "PM10FIX", "PM10FIX2", "PM10FIX3", "PM22"),)})

    def test_proposals_never_leave_allowlist(self):
        for ref, s, _ in FIXTURE:
            if "proposal" in s:
                self.assertIn(supervise(s).action, ALLOWED_PROPOSALS | {"needs_input"})


class IntegrationOrderTests(unittest.TestCase):
    """O13: auto INT groups first, manual-key groups LAST."""

    def test_truthy_non_bool_manual_counts_as_manual(self):
        for manual in (1, "yes", 0, None, "false"):
            d = plan_integration([{"name": "A", "status": "pass", "manual": manual},
                                  _pick("B")], oldest_wait_s=3600)
            self.assertEqual(d.groups, (("B",), ("A",)), manual)

    def test_all_manual_groups_follow_all_auto(self):
        picks = [_pick(f"M{i}", manual=True) for i in range(7)] + [_pick("A"), _pick("B")]
        d = plan_integration(picks)
        self.assertEqual(d.groups, (("A", "B"), tuple(f"M{i}" for i in range(6)), ("M6",)))

    def test_split_keeps_manual_last(self):
        d = plan_integration([_pick("M", manual=True), _pick("X", "blocked"), _pick("A")])
        self.assertEqual((d.action, d.groups), ("split", (("A",), ("M",))))


class QuotaRateTests(unittest.TestCase):
    """O23.unknown: a supplied-but-invalid rate/target pauses; both absent is optional."""

    def test_invalid_rate_or_target_pauses(self):
        for rate, target in (("0.6", 0.5), (True, 0.5), (float("nan"), 0.5),
                             (float("inf"), 0.5), (-1, 0.5), (10**400, 0.5),
                             (0.4, "0.5"), (0.4, True), (0.4, 0), (0.4, -1),
                             (0.4, None), (None, 0.5)):
            for line, hour in ((50, 12), (None, 12), (50, 3)):
                d = quota_pacing(50, line, hour, rate, target)
                self.assertEqual((d.action, d.rule_id), ("pause", "O23.unknown"),
                                 (rate, target, line, hour))
        self.assertEqual(supervise(_q(50, 50, 12, "0.6", 0.5)).rule_id, "O23.unknown")

    def test_absent_pair_stays_optional(self):
        self.assertEqual(quota_pacing(50, 50, 12).rule_id, "pace.on_line")
        self.assertEqual(supervise(_q(50, 50, 12)).rule_id, "pace.on_line")
        self.assertEqual(quota_pacing(50, 50, 12, 0, 0.5).rule_id, "pace.on_line")


class PrecedenceTests(unittest.TestCase):
    """memory hold > quota non_claude_only > dispatch, as one table."""

    def test_table_covers_every_gate_action(self):
        self.assertEqual(PRECEDENCE[0], ("operator_pause", "pause"))
        for key, actions in (("quota", ("pause", "non_claude_only", "dispatch")),
                             ("memory", ("wait", "dispatch")), ("alarm", ("alarm", "suppress"))):
            for action in actions:
                self.assertIn((key, action), PRECEDENCE)
        for key in ("fix", "integration", "proposal"):
            self.assertIn((key, None), PRECEDENCE)
        rows = [r for r in PRECEDENCE if r[0] in ("quota", "memory")]
        self.assertEqual(rows, [("quota", "pause"), ("memory", "wait"),
                                ("quota", "non_claude_only"), ("memory", "dispatch"),
                                ("quota", "dispatch")])

    def test_memory_always_checked(self):
        ahead = _q(51, 44.6, 12)
        self.assertEqual(supervise({**ahead, **_m(2.0, False)}).rule_id, "mem.hold")
        self.assertEqual(supervise({**ahead, **_m(None, False)}).rule_id, "mem.unknown")
        self.assertEqual(supervise({**ahead, **_m(5.0, False)}).rule_id, "pace.ahead")
        self.assertEqual(supervise({**_q(50, 50, 12), **_m(4.0, True, 600)}).rule_id,
                         "mem.hysteresis")
        self.assertEqual(supervise({**_q(95, 50, 12), **_m(2.0, False)}).rule_id,
                         "O23.limit")

    def test_non_claude_only_hides_no_work_decision(self):
        ahead = _q(51, 44.6, 12)
        for extra, rule_id in ((_f("X", 0), "O5.fix"), (_f("X", 2), "O5.cap"),
                               (_a("A", "stuck"), "O33.new"),
                               ({"integration": {"picks": [_pick("A")], "int_running": True}},
                                "O13.running"),
                               ({"proposal": "requeue"}, "D8.proposal"),
                               (_a("A", "stuck", finished={"A"}), "pace.ahead")):
            self.assertEqual(supervise({**ahead, **_m(5.0, False), **extra}).rule_id,
                             rule_id, extra)

    def test_suppressed_alarm_hides_nothing(self):
        d = supervise({**_a("A", "stuck", alarmed={"stuck"}), **_f("X", 0)})
        self.assertEqual(d.rule_id, "O5.fix")


class _Evil(str):
    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("wait")


class StrSubclassTests(unittest.TestCase):
    def test_str_subclass_action_or_proposal_refused(self):
        for bad in ({"action": _Evil("delete")}, _Evil("wait"), {"action": {"action": "wait"}}):
            self.assertEqual(gate_proposal(bad).rule_id, "D8.refused")
            self.assertEqual(supervise({"proposal": bad}).rule_id, "D8.refused")

    def test_str_subclass_reason_dropped(self):
        d = gate_proposal({"action": "wait", "reason": _Evil("x" * 500)})
        self.assertEqual((d.action, d.reason), ("wait", "model proposal"))

    def test_extra_fields_never_change_action(self):
        d = gate_proposal({"action": "wait", "cmd": "push", "nested": {"action": "push"}})
        self.assertEqual(d.action, "wait")


class BoundTests(unittest.TestCase):
    def test_out_of_bounds_params_raise(self):
        cases = [("fix", {"target": "X", "failed_fixes": 0, "cap": c}) for c in (3, -1, True, 2.0)]
        cases += [("quota", {"used_pct": 50, "line_pct": 50, "hour": 12, "limit_pct": v})
                  for v in (101, 100.5)]
        cases += [("quota", {"used_pct": 50, "line_pct": 50, "hour": 12, "ahead_pp": v})
                  for v in (-1, float("nan"), "3", True)]
        cases += [("memory", {"free_gb": 4, "held": False, "low_gb": v}) for v in (0, -1)]
        cases += [("integration", {"picks": [_pick("A")], "min_batch": v})
                  for v in (0, -1, 1.5, True)]
        rules = {"fix": fix_chain, "quota": quota_pacing, "memory": memory_hold,
                 "integration": plan_integration}
        for key, args in cases:
            with self.assertRaises(ValueError, msg=(key, args)):
                rules[key](**args)
            with self.assertRaises(ValueError, msg=(key, args)):
                supervise({key: args})

    def test_in_bounds_edges_and_tunable_ahead(self):
        self.assertEqual(quota_pacing(50, 50, 12, limit_pct=100).action, "dispatch")
        self.assertEqual(fix_chain("X", 0, cap=0).action, "ask_salih")
        self.assertEqual(quota_pacing(51, 44.6, 12, ahead_pp=10).action, "dispatch")
        self.assertEqual(quota_pacing(45, 44.6, 12, ahead_pp=0).rule_id, "pace.ahead")


class ShapeTests(unittest.TestCase):
    def test_malformed_situation_raises_value_error(self):
        bad = [None, [], "quota", {"quota": None}, {"quota": "x"}, {"memory": []},
               {"alarm": {}}, {"memory": {"free_gb": 4}}, {"fix": {"target": "X"}},
               {"memory": {"free_gb": 4, "held": False, "x": 1}}, {"memory": {1: 2}},
               {"integration": {"picks": None}}, {"integration": {"picks": [None]}},
               {"integration": {"picks": [{"status": "pass"}]}},
               {"integration": {"picks": "AB"}}, _a("A", ["unhashable"])]
        for situation in bad:
            with self.assertRaises(ValueError, msg=situation):
                supervise(situation)

    def test_any_evaluation_error_raises_value_error(self):
        class BadGet(dict):
            def get(self, *a):
                raise TypeError("boom")

        class BadBool:
            def __bool__(self):
                raise RuntimeError("boom")

        class BadHash:
            def __hash__(self):
                raise RuntimeError("boom")

        for situation in ({"proposal": BadGet(action="wait")}, {"operator_pause": BadBool()},
                          _a("A", BadHash())):
            with self.assertRaises(ValueError, msg=situation):
                supervise(situation)

    def test_non_str_status_is_not_pass(self):
        self.assertEqual(plan_integration([_pick("A", _Evil("x"))]).rule_id, "O11.none_ready")
        d = plan_integration([_pick("A", _Evil("x")), _pick("B")], oldest_wait_s=3600)
        self.assertEqual((d.action, d.groups), ("split", (("B",),)))

    def test_out_of_range_line_or_target_pauses(self):
        for line, target in ((1000, None), (-5, None), ("50", None), (50, 0.99e308), (50, 101)):
            rate = None if target is None else 0.4
            d = quota_pacing(50, line, 12, rate, target)
            self.assertEqual((d.action, d.rule_id), ("pause", "O23.unknown"), (line, target))

    def test_mixed_unknown_keys_need_input(self):
        d = supervise({1: "x", "y": 2, "quota": None})
        self.assertEqual((d.action, d.rule_id), ("needs_input", "D8.unknown"))


if __name__ == "__main__":
    unittest.main()
