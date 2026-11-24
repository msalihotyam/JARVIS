"""N6G28PRESENCE: read-only presence signal, power proposal and shadow (R10)."""

import ast
import dataclasses
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import n6_presence as p

_SPEC = importlib.util.spec_from_file_location(
    "conveyor_g28", Path(__file__).resolve().parents[1] / "scripts" / "conveyor.py")
conveyor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(conveyor)

T0 = 1_000_000.0
HOLD = p.PRESENCE_HOLD_S
PACE = p.read_pace({})  # conveyor defaults: present 3, away 5, away after 10 min
AWAY_IDLE = 600.0


def run(steps, state=None, pace=PACE):
    """[(idle, now)] -> signals, final state."""
    state = state or p.State()
    out = []
    for idle, now in steps:
        sig, state = p.sense(state, idle, pace, now)
        out.append(sig)
    return out, state


class PresenceTests(unittest.TestCase):
    # --- the conveyor's values, pinned (a drift fails here) -----------------
    def test_constants_match_conveyor(self) -> None:
        self.assertEqual(p.PRESENT_IDLE_S, conveyor.PRESENT_IDLE_S)
        self.assertEqual(p.PRESENCE_HOLD_S, conveyor.PRESENCE_HOLD_S)
        self.assertEqual(p.MAX_LANES_CAP, conveyor.MAX_LANES_CAP)
        self.assertEqual(p.POWER_PLANS, conveyor.POWER_PLANS)
        for k, v in p.PACE_DEFAULTS.items():
            self.assertEqual(v, conveyor.PACE_DEFAULTS[k], k)

    # --- unknown, never a guess ---------------------------------------------
    def test_neg_malformed_idle_is_unknown_and_pending_dropped(self) -> None:
        pending = p.State(p.PRESENT, p.AWAY, T0)
        for bad in (None, True, "600", float("nan"), float("inf"), -1, [600], 10**400):
            sig, state = p.sense(pending, bad, PACE, T0 + 10 * HOLD)
            self.assertEqual(sig, p.Signal(p.UNKNOWN, reason="idle"), bad)
            self.assertEqual(state, p.State(p.PRESENT))
            self.assertIsNone(sig.lane_target)

    def test_neg_bad_clock_pace_or_state_is_unknown(self) -> None:
        pending = p.State(p.PRESENT, p.AWAY, T0)
        sig, state = p.sense(pending, AWAY_IDLE, PACE, T0 - 1)  # clock went back
        self.assertEqual((sig.presence, sig.reason, state), (p.UNKNOWN, "clock", p.State(p.PRESENT)))
        for now in (None, "x", float("nan"), -5):
            self.assertEqual(p.sense(pending, 0, PACE, now)[0].reason, "clock")
        self.assertEqual(p.sense(pending, 0, None, T0)[0], p.Signal(p.UNKNOWN, reason="pace"))
        for bad in (None, "present", p.State("idle"), p.State(p.AWAY, p.AWAY, T0),
                    p.State(p.PRESENT, p.AWAY, None), p.State(p.PRESENT, None, T0),
                    p.State(p.PRESENT, p.AWAY, float("nan"))):
            sig, state = p.sense(bad, 0, PACE, T0)
            self.assertEqual((sig, state), (p.Signal(p.UNKNOWN, reason="state"), bad), bad)

    def test_neg_malformed_pace_is_none(self) -> None:
        for raw in (None, [], "x", {"away_after_min": 0}, {"away_after_min": -1},
                    {"away_after_min": True}, {"away_after_min": float("nan")},
                    {"lanes_present": "3"}, {"lanes_away": None}, {"max_lanes": None},
                    {"lanes_away": float("inf")}, {"power_switch": 1},
                    {"min_lanes": 4, "max_lanes": 2}, {"min_lanes": "1"}):
            self.assertIsNone(p.read_pace(raw), raw)

    def test_neg_pace_the_conveyor_rejects_is_none(self) -> None:
        """Same accept/reject as the real Conveyor.load_pace (it drops a bad file whole)."""
        bad = ({"lanes_present": 2, "mode": "bogus"}, {"away_after_min": 1, "days": 0},
               {"power_switch": True}, {"mode": "paced"}, {"mode": "paced", "window_start": "x"},
               {"codex_max": 9}, {"jules_max": True}, {"salih_keep_pct": 31},
               {"salih_keep_window": 1}, {"claude_version": " v"}, {"power_switch_cmd": [""]},
               {"power_switch_query_cmd": []}, {"power_switch_skip_plans": "x"},
               {"autofix_fable": 1}, {"blocked_nudge_min": -1}, {"lanes_present": float("inf")})
        good = ({}, {"lanes_present": 10**400}, {"power_switch": True, "power_switch_cmd": ["x"]},
                {"mode": "paced", "window_start": "2026-11-01T00:00:00"}, {"codex_max": 0},
                {"salih_keep_pct": 30, "claude_version": "2.1"}, {"min_lanes": 0, "max_lanes": 1})
        with tempfile.TemporaryDirectory() as tmp:
            fake = SimpleNamespace(base=Path(tmp), now=lambda: T0)
            for raw, ok in [(r, False) for r in bad] + [(r, True) for r in good]:
                (Path(tmp) / "pace.json").write_text(json.dumps(raw), encoding="utf-8")
                self.assertEqual(conveyor.Conveyor.load_pace(fake)[1] is None, ok, raw)
                self.assertEqual(p.read_pace(raw) is not None, ok, raw)
        self.assertEqual(p.read_pace({"lanes_present": 10**400}).lanes_present, 8)

    # --- hysteresis ---------------------------------------------------------
    def test_hold_boundary_and_one_switch_per_change(self) -> None:
        sigs, state = run([(AWAY_IDLE, T0), (AWAY_IDLE + 119, T0 + HOLD - 1),
                           (AWAY_IDLE + 120, T0 + HOLD), (AWAY_IDLE + 300, T0 + 300)])
        self.assertEqual([s.presence for s in sigs], [p.PRESENT, p.PRESENT, p.AWAY, p.AWAY])
        self.assertEqual([s.changed for s in sigs], [False, False, True, False])
        self.assertEqual((sigs[2].was, sigs[2].lane_target), (p.PRESENT, 5))
        self.assertEqual(state, p.State(p.AWAY))
        back, _ = run([(0, T0 + 400), (0, T0 + 400 + HOLD)], state)
        self.assertEqual([(s.presence, s.changed, s.was) for s in back],
                         [(p.AWAY, False, None), (p.PRESENT, True, p.AWAY)])

    def test_thresholds_inclusive_away_and_dead_band(self) -> None:
        self.assertEqual(run([(599.9, T0), (599.9, T0 + HOLD)])[1], p.State())
        self.assertTrue(run([(600, T0), (600, T0 + HOLD)])[0][1].changed)
        away = p.State(p.AWAY)
        for idle in (60, 61, 599):  # dead band: stays away, clears pending
            sig, state = p.sense(p.State(p.AWAY, p.PRESENT, T0), idle, PACE, T0 + HOLD)
            self.assertEqual((sig.presence, sig.changed, state), (p.AWAY, False, away), idle)
        sig, _ = p.sense(p.State(p.AWAY, p.PRESENT, T0), 59.9, PACE, T0 + HOLD)
        self.assertTrue(sig.changed)

    def test_neg_no_flapping_inside_hold(self) -> None:
        steps = [(AWAY_IDLE if i % 2 == 0 else 0, T0 + i * 30) for i in range(40)]
        sigs, state = run(steps)
        self.assertFalse(any(s.changed for s in sigs))
        self.assertEqual(state.presence, p.PRESENT)

    def test_unknown_tick_drops_pending(self) -> None:
        """A hold never spans an unseen read: the change restarts its hold after unknown."""
        sigs, state = run([(AWAY_IDLE, T0), (None, T0 + 60), (AWAY_IDLE, T0 + HOLD),
                           (AWAY_IDLE, T0 + 2 * HOLD)])
        self.assertEqual([s.presence for s in sigs], [p.PRESENT, p.UNKNOWN, p.PRESENT, p.AWAY])
        self.assertEqual([s.changed for s in sigs], [False, False, False, True])
        self.assertEqual(state, p.State(p.AWAY))

    # --- lane target --------------------------------------------------------
    def test_lane_target_pace_rules(self) -> None:
        self.assertEqual((p.lane_target(p.PRESENT, PACE), p.lane_target(p.AWAY, PACE)), (3, 5))
        self.assertIsNone(p.lane_target(p.UNKNOWN, PACE))
        self.assertIsNone(p.lane_target(p.PRESENT, None))
        top = p.read_pace({"max_lanes": 2, "lanes_away": 7})
        self.assertEqual((p.lane_target(p.PRESENT, top), p.lane_target(p.AWAY, top)), (2, 2))
        clamp = p.read_pace({"lanes_present": -4, "lanes_away": 99, "away_after_min": 0.5})
        self.assertEqual((clamp.lanes_present, clamp.lanes_away, clamp.away_after_min), (1, 8, 0.5))
        self.assertEqual(p.read_pace({"lanes_present": 2.9}).lanes_present, 2)
        self.assertIsNotNone(p.read_pace({"min_lanes": 0, "max_lanes": 1}))

    # --- power: a proposal only, never executed -----------------------------
    def test_power_proposal_only_on_switch_and_needs_approval(self) -> None:
        sigs, _ = run([(AWAY_IDLE, T0), (AWAY_IDLE, T0 + HOLD), (AWAY_IDLE, T0 + 2 * HOLD)])
        got = [p.propose_power(s, PACE) for s in sigs]
        self.assertEqual(got, [None, p.PowerProposal(p.AWAY, "JarvisFocus", False), None])
        self.assertTrue(got[1].needs_approval)
        on = p.read_pace({"power_switch": True, "power_switch_cmd": ["x"]})
        self.assertTrue(p.propose_power(sigs[1], on).would_switch)
        self.assertIsNone(p.propose_power(p.Signal(p.UNKNOWN, reason="idle"), on))
        self.assertIsNone(p.propose_power(sigs[1], None))

    def test_neg_power_plan_allowlist_and_approval_fixed(self) -> None:
        for presence, plan in ((p.AWAY, "Balanced"), (p.PRESENT, "JarvisFocus"),
                               (p.UNKNOWN, "JarvisWatch"), ("away ", "JarvisFocus")):
            with self.assertRaises(ValueError):
                p.PowerProposal(presence, plan, True)
        with self.assertRaises(TypeError):
            p.PowerProposal(p.AWAY, "JarvisFocus", True, needs_approval=False)
        prop = p.PowerProposal(p.AWAY, "JarvisFocus", True)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            prop.needs_approval = False

    # --- events seed --------------------------------------------------------
    def test_state_from_events(self) -> None:
        ev = [{"event": "pace"}, {"event": "presence", "state": "away", "was": "present"}]
        self.assertEqual(p.state_from_events(ev), p.State(p.AWAY))
        self.assertEqual(p.state_from_events([]), p.State())
        for bad in (["x"], [{"event": "presence", "state": "idle", "was": "present"}],
                    [{"event": "presence", "state": "away", "was": "away"}]):
            self.assertIsNone(p.state_from_events(bad), bad)

    # --- shadow vs the conveyor's own sense_presence / priority -------------
    def oracle(self, steps, away_after_min=10):
        """The real Conveyor.sense_presence on a fake self; events per pass (+ priority)."""
        clock = {"t": 0.0, "idle": None}
        fake = SimpleNamespace(presence="present", presence_pending=None,
                               idle_unknown_logged=False, idle_s=None,
                               idle=lambda: clock["idle"], now=lambda: clock["t"])
        ticks = []
        for idle, now in steps:
            events = []
            fake.event = lambda kind, _ev=events, **kw: _ev.append({"event": kind, **kw})
            clock.update(t=now, idle=idle)
            conveyor.Conveyor.sense_presence(fake, away_after_min)
            events.append({"event": "priority", "lane": "L", "presence": fake.presence,
                           "pids": 1, "failed": []})
            ticks.append(events)
        return ticks

    def test_shadow_agrees_with_real_conveyor_on_fixture(self) -> None:
        steps = [(0, T0), (AWAY_IDLE, T0 + 30), (30, T0 + 60), (AWAY_IDLE, T0 + 90),
                 (AWAY_IDLE, T0 + 90 + HOLD - 1), (AWAY_IDLE, T0 + 90 + HOLD),
                 (300, T0 + 400), (10, T0 + 500), (10, T0 + 500 + HOLD), (0, T0 + 900)]
        sigs, _ = run(steps)
        got = p.shadow_compare(list(zip(sigs, self.oracle(steps))))
        self.assertEqual({c.verdict for c in got}, {"agree"})
        self.assertEqual([c.n6 for c in got if c.kind == "presence" and c.n6],
                         ["present->away", "away->present"])
        self.assertEqual(len(got), 2 * len(steps))

    def test_shadow_unreadable_idle_is_unknown_not_disagree(self) -> None:
        steps = [(AWAY_IDLE, T0), (AWAY_IDLE, T0 + HOLD), (None, T0 + 200),
                 (None, T0 + 200 + HOLD)]
        sigs, state = run(steps)
        got = p.shadow_compare(list(zip(sigs, self.oracle(steps))))
        pres = [c.verdict for c in got if c.kind == "presence"]
        self.assertEqual(pres, ["agree", "agree", "agree", "unknown"])  # conveyor guessed present
        self.assertEqual(state, p.State(p.AWAY))
        self.assertNotIn("disagree", [c.verdict for c in got])

    def test_shadow_disagree_and_malformed_fixtures(self) -> None:
        sw = p.Signal(p.AWAY, True, p.PRESENT, 5)
        move = {"event": "presence", "state": "away", "was": "present", "idle_s": 600}
        got = p.shadow_compare([
            (sw, []),  # N6 switched, conveyor did not
            (p.Signal(p.PRESENT, lane_target=3), [move]),  # conveyor switched, N6 did not
            (p.Signal(p.AWAY), [{"event": "priority", "presence": "present"}]),
            (p.Signal(p.AWAY), [{"event": "priority", "presence": 7}]),
            (sw, ["not a record"]),
            (sw, [{"event": "presence", "state": "away"}]),
            (sw, [move, move]),
            (sw, [{**move, "was": "away"}]),  # a no-op presence event is malformed
        ])
        self.assertEqual([(c.kind, c.verdict) for c in got], [
            ("presence", "disagree"), ("presence", "disagree"),
            ("presence", "agree"), ("priority", "disagree"),
            ("presence", "agree"), ("priority", "unknown"),
            ("presence", "unknown"), ("presence", "unknown"), ("presence", "unknown"),
            ("presence", "unknown")])
        self.assertLessEqual({c.verdict for c in got}, set(p.VERDICTS))

    # --- shadow only: no OS, power, network, subprocess or wall clock -------
    def test_module_imports_and_calls_nothing_live(self) -> None:
        tree = ast.parse(Path(p.__file__).read_text(encoding="utf-8"))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(mods, {"__future__", "math", "collections", "dataclasses", "datetime",
                                     "typing"})
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for banned in ("now", "today", "time", "monotonic", "system", "Popen", "run",
                       "write_text", "write_bytes", "open"):
            self.assertNotIn(banned, attrs, banned)  # no datetime.now/time.time/os.system/...
        for banned in ("open", "eval", "exec", "__import__", "print"):
            self.assertNotIn(banned, names, banned)


if __name__ == "__main__":
    unittest.main()
