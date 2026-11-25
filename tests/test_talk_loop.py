import ast
import unittest
from pathlib import Path

from jarvis import talk_loop as tl
from tests import adversarial

SOURCE = Path(__file__).resolve().parents[1] / "jarvis" / "talk_loop.py"

QUIET = dict(inbound_waiting=0, turn_in_progress=False, delivery_due_at=None, poll_due_at=None,
             recover_needed=False, shutdown_requested=False, failure_streak=0, now=100.0)


def step(state=None, **over):
    return tl.next_step(tl.LoopState() if state is None else state, **{**QUIET, **over})


class AcceptanceTests(unittest.TestCase):
    def test_each_kind(self):
        self.assertEqual(step(recover_needed=True).kind, tl.RECOVER)
        self.assertEqual(step(inbound_waiting=1).kind, tl.HANDLE_INBOUND)
        self.assertEqual(step(delivery_due_at=100.0).kind, tl.DELIVER)
        self.assertEqual(step(poll_due_at=99.0).kind, tl.POLL)
        self.assertEqual(step(shutdown_requested=True).kind, tl.STOP)
        idle = step(poll_due_at=110.0)
        self.assertEqual((idle.kind, idle.seconds), (tl.IDLE, 10.0))

    def test_order(self):
        everything = dict(inbound_waiting=1, delivery_due_at=0.0, poll_due_at=0.0)
        self.assertEqual(step(recover_needed=True, **everything).kind, tl.RECOVER)
        self.assertEqual(step(**everything).kind, tl.HANDLE_INBOUND)
        self.assertEqual(step(delivery_due_at=0.0, poll_due_at=0.0).kind, tl.DELIVER)
        self.assertEqual(step(delivery_due_at=101.0, poll_due_at=0.0).kind, tl.POLL)
        self.assertEqual(step(delivery_due_at=101.0, poll_due_at=101.0).kind, tl.IDLE)

    def test_turn_in_progress_blocks_new_inbound_only(self):
        self.assertEqual(step(inbound_waiting=5, turn_in_progress=True).kind, tl.IDLE)
        self.assertEqual(step(inbound_waiting=5, turn_in_progress=True, delivery_due_at=1.0).kind,
                         tl.DELIVER)

    def test_shutdown_starts_no_inbound_and_stops_after_turn(self):
        busy = step(shutdown_requested=True, turn_in_progress=True, inbound_waiting=3, poll_due_at=0.0)
        self.assertEqual(busy.kind, tl.IDLE)  # no inbound, no poll while the turn finishes
        self.assertEqual(step(shutdown_requested=True, turn_in_progress=True,
                              delivery_due_at=0.0).kind, tl.DELIVER)
        self.assertEqual(step(shutdown_requested=True, inbound_waiting=3).kind, tl.STOP)

    def test_idle_clamped_between_min_and_max(self):
        self.assertEqual(step(delivery_due_at=100.1).seconds, tl.MIN_WAIT)
        self.assertEqual(step(delivery_due_at=10_000.0).seconds, tl.MAX_WAIT)
        self.assertEqual(step().seconds, tl.MAX_WAIT)
        self.assertEqual(step(delivery_due_at=107.0, poll_due_at=103.0).seconds, 3.0)

    def test_failure_streak_backs_off_poll_to_cap(self):
        self.assertEqual([tl.poll_interval(n) for n in range(8)],
                         [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0])
        self.assertEqual(tl.poll_interval(tl.MAX_COUNT), tl.POLL_MAX_INTERVAL)
        self.assertEqual(step(poll_due_at=100.0, failure_streak=0).kind, tl.POLL)
        backed = step(poll_due_at=100.0, failure_streak=2)  # due at 100 + 20 - 5
        self.assertEqual((backed.kind, backed.seconds), (tl.IDLE, 15.0))
        self.assertEqual(step(poll_due_at=100.0, failure_streak=2, now=115.0).kind, tl.POLL)
        self.assertEqual(step(poll_due_at=0.0, failure_streak=50, inbound_waiting=1).kind,
                         tl.HANDLE_INBOUND)

    def test_deterministic(self):
        args = dict(inbound_waiting=2, delivery_due_at=50.0, poll_due_at=70.0, failure_streak=3)
        state = tl.LoopState(True, 4)
        self.assertEqual(step(state, **args), step(tl.LoopState(True, 4), **dict(args)))


class NegativeTests(unittest.TestCase):
    def test_past_due_times_keep_minimum_wait(self):
        # shutdown with a turn running serves nothing; a past poll time must not make it spin
        past = step(shutdown_requested=True, turn_in_progress=True, poll_due_at=0.0, now=1e6)
        self.assertEqual((past.kind, past.seconds), (tl.IDLE, tl.MIN_WAIT))
        backed = step(poll_due_at=0.0, failure_streak=6, now=1.0)  # due 295 s ahead
        self.assertEqual(backed.seconds, tl.MAX_WAIT)
        for now in (0.0, 0.25, 99.99, 1e9):
            with self.subTest(now=now):
                s = step(shutdown_requested=True, turn_in_progress=True, poll_due_at=0.0, now=now)
                self.assertGreaterEqual(s.seconds, tl.MIN_WAIT)

    def test_hostile_times_and_counts_refused(self):
        def time_check(value):
            for field in ("now", "delivery_due_at", "poll_due_at"):
                if value is None and field != "now":
                    continue
                self.assertIsInstance(step(**{field: value}), tl.Refusal, field)

        # -0.0 is a valid zero time; every other hostile number is refused
        hostile = [v for v in adversarial.HOSTILE_NUMBERS if not (type(v) is float and v == 0)]
        adversarial.each(self, hostile + [-1, -0.5], time_check)

        def count_check(value):
            for field in ("inbound_waiting", "failure_streak"):
                self.assertIsInstance(step(**{field: value}), tl.Refusal, field)
            self.assertIsInstance(tl.poll_interval(value), tl.Refusal)

        adversarial.each(self, list(adversarial.HOSTILE_NUMBERS) + [-1, 1.0], count_check)

    def test_bad_flags_and_state_refused(self):
        for flag in ("turn_in_progress", "recover_needed", "shutdown_requested"):
            for value in (1, 0, None, "yes"):
                self.assertEqual(step(**{flag: value}).code, "FLAG_INVALID")
        for state in (None, {}, tl.LoopState(1, 0), tl.LoopState(False, -1),
                      tl.LoopState(False, True)):
            self.assertEqual(tl.next_step(state, **QUIET).code, "STATE_INVALID")

    def test_hostile_text_and_deep_input_refused(self):
        adversarial.each(self, adversarial.HOSTILE_TEXT,
                         lambda v: self.assertIsInstance(step(now=v), tl.Refusal))
        deep = []
        for _ in range(5000):
            deep = [deep]
        self.assertIsInstance(step(delivery_due_at=deep), tl.Refusal)
        self.assertIsInstance(tl.next_step(tl.LoopState()), tl.Refusal)  # missing fields

    def test_wrong_arguments_refused_not_raised(self):
        # every public call is guarded: a bad call shape is a typed Refusal, never an exception
        calls = [
            lambda: tl.poll_interval(),
            lambda: tl.poll_interval(failure_streak=1, x=2),
            lambda: tl.poll_interval(1, 2),
            lambda: tl.poll_interval(x=1),
            lambda: tl.next_step(),
            lambda: tl.next_step(tl.LoopState(), *QUIET.values()),
            lambda: tl.next_step(tl.LoopState(), **QUIET, x=2),
        ]
        for i, call in enumerate(calls):
            result = call()
            self.assertIsInstance(result, tl.Refusal, i)
            self.assertEqual(result.code, "INPUT_INVALID", i)
        self.assertEqual(tl.poll_interval(failure_streak=0), tl.POLL_INTERVAL)

    def test_times_above_2_53_refused(self):
        # CDX-TALKLOOP1FIX2 F1 probes: mixed int/float above 2**53 rounded; now refused, nothing decided
        probes = [dict(poll_due_at=2.0**60, now=2**60 + 10, failure_streak=1),
                  dict(delivery_due_at=2**60 + 10, now=2.0**60)]
        for kw in probes:
            self.assertEqual(step(**kw).code, "TIME_INVALID", kw)
        for big in (2**53 + 1, 2.0**53 + 2, 2**60, 2.0**60, 10**308, 1e308, 2.0**1023):
            for field in ("now", "delivery_due_at", "poll_due_at"):
                with self.subTest(big=str(big)[:20], field=field):
                    self.assertEqual(step(**{field: big}).code, "TIME_INVALID")

    def test_2_53_boundary_accepted_and_exact(self):
        for big in (2**53, 2.0**53):
            with self.subTest(big=str(big)):
                s = step(poll_due_at=big, now=big, failure_streak=1)
                self.assertEqual((s.kind, s.seconds), (tl.IDLE, 5.0))
                self.assertEqual(step(poll_due_at=big, now=big).kind, tl.POLL)
                self.assertEqual(step(delivery_due_at=big, now=big).kind, tl.DELIVER)
        self.assertEqual(step(poll_due_at=2**53 - 5, now=2.0**53, failure_streak=1).kind, tl.POLL)
        self.assertEqual(step(poll_due_at=2.0**53 - 4, now=2**53, failure_streak=1).seconds, 1.0)

    def test_mixed_int_float_times_exact(self):
        for base in (2**52, 10**9, 2**53 - 20):
            with self.subTest(base=base):
                s = step(poll_due_at=float(base), now=base + 10, failure_streak=1)
                self.assertEqual(s.kind, tl.POLL)
                s = step(poll_due_at=float(base), now=base + 4, failure_streak=1)
                self.assertEqual((s.kind, s.seconds), (tl.IDLE, 1.0))
                s = step(delivery_due_at=base + 10, now=float(base))
                self.assertEqual((s.kind, s.seconds), (tl.IDLE, 10.0))
                self.assertEqual(step(delivery_due_at=base + 1, now=float(base) + 1).kind, tl.DELIVER)
                self.assertEqual(step(delivery_due_at=float(base) + 1, now=base).seconds, 1.0)

    def test_shutdown_with_turn_never_stops(self):
        for kw in (dict(), dict(inbound_waiting=9), dict(delivery_due_at=0.0),
                   dict(poll_due_at=0.0, failure_streak=3), dict(recover_needed=True)):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                s = step(shutdown_requested=True, turn_in_progress=True, **kw)
                self.assertNotEqual(s.kind, tl.STOP)

    def test_recover_at_most_once_per_start(self):
        first = step(recover_needed=True)
        self.assertEqual(first.kind, tl.RECOVER)
        state, kinds = first.state, []
        for _ in range(20):
            s = step(state, recover_needed=True, inbound_waiting=1)
            kinds.append(s.kind)
            state = s.state
        self.assertNotIn(tl.RECOVER, kinds)
        self.assertEqual(step(recover_needed=True).kind, tl.RECOVER)  # a new start recovers again

    def test_delivery_not_starved_by_inbound_flood(self):
        state, waiting, kinds = tl.LoopState(True, 0), 1000, []
        while waiting and len(kinds) < 2000:
            s = step(state, inbound_waiting=waiting, delivery_due_at=0.0, poll_due_at=1e9)
            kinds.append(s.kind)
            state = s.state
            if s.kind == tl.HANDLE_INBOUND:
                waiting -= 1
        self.assertLessEqual(kinds.index(tl.DELIVER), tl.FAIRNESS_LIMIT)
        runs = "".join("D" if k == tl.DELIVER else "i" for k in kinds).split("D")
        self.assertLessEqual(max(len(r) for r in runs), tl.FAIRNESS_LIMIT)
        self.assertEqual(waiting, 0)


class StaticTests(unittest.TestCase):
    ALLOWED = {"__future__", "math", "dataclasses"}  # allowlist: any new import must be reviewed here

    def test_no_io_network_or_clock_imports(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                self.assertIn(name, self.ALLOWED, name)
                self.assertEqual(getattr(node, "level", 0), 0, name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "eval", "exec"})

    def test_refusal_texts_ascii_under_cp1252_strict(self):
        with adversarial.narrow_stdout() as out:
            for code, text in tl.REFUSALS.items():
                self.assertTrue(text.isascii() and text.isprintable(), code)
                print(code, tl._refuse(code).text)
            out.flush()
        self.assertIn(b"TIME_INVALID", out.buffer.getvalue())

    def test_not_imported_by_running_app(self):
        root = SOURCE.parent
        users = [p.name for p in root.rglob("*.py")
                 if p != SOURCE and "talk_loop" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])
