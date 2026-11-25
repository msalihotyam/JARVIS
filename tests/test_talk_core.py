"""TALKCORE1: the PC conversation core over fake raw ports (no app, no network)."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import talk_boundary as tb
from jarvis import talk_core as tc

ROOT = Path(__file__).resolve().parents[1]


class FakeInbox:
    def __init__(self, *items):
        self.items, self.acked = list(items), []
        self.log = None  # set by World: records whether the terminal line existed at ack

    def take(self, limit):
        return list(self.items[:limit])

    def ack(self, value):
        for n, it in enumerate(self.items):
            if it is value or (type(it) is dict and it.get("id") is value):
                self.acked.append((it, self.log.terminal_for(it) if self.log else None))
                del self.items[n]
                return


class FakeLog:
    def __init__(self):
        self.lines = []

    def _find(self, key):
        return next((x for x in self.lines if x["key"] == key), None)

    def append(self, key, line):
        if self._find(key) is not None:
            return "exists"
        self.lines.append(dict(line))
        return "ok"

    def get(self, key):
        found = self._find(key)
        return "absent" if found is None else dict(found)

    def read(self):
        return [dict(x) for x in self.lines]

    def terminal_for(self, item):
        """True only when this item's own terminal line exists."""
        rid = item.get("id") if type(item) is dict else None
        if not (type(rid) is str and tb.ID_RE.fullmatch(rid)):
            rid = tb._tag(item)
        line = self._find("in:pc:" + rid)
        return line is not None and (line["kind"] == "refused" or self._find("reply:" + rid) is not None)


class FakeBrain:
    def __init__(self, fail=()):
        self.fail, self.asked = set(fail), []

    def ask(self, text):
        self.asked.append(text)
        if text in self.fail:
            self.fail.discard(text)
            raise TimeoutError("brain")
        return "cevap: " + text


class FakeClock:
    def __init__(self):
        self.t = 1000

    def now(self):
        self.t += 1
        return self.t


class World:
    """Raw fakes, optionally behind a counted fault injector."""

    def __init__(self, *items, fail=(), at=None, mode=None):
        self.inbox, self.log, self.brain = FakeInbox(*items), FakeLog(), FakeBrain(fail)
        self.inbox.log = self.log
        self.clock, self.shown = FakeClock(), []
        self.calls, self.at, self.mode = 0, at, mode

    def _port(self, target, name):
        def call(*args):
            n, self.calls = self.calls, self.calls + 1
            if n == self.at and self.mode == "raise_before":
                raise OSError("before")
            if n == self.at and self.mode == "wrong_type":
                return object()
            got = getattr(target, name)(*args)
            if n == self.at and self.mode == "write_then_raise":
                raise OSError("after")
            return got
        return call

    def core(self, max_pc=4, budget=40):
        raw = lambda target, *names: SimpleNamespace(**{n: self._port(target, n) for n in names})  # noqa: E731
        view = SimpleNamespace(show=self.shown.append)
        ports = tb.wrap(raw(self.inbox, "take", "ack"), raw(self.log, "append", "get", "read"),
                        raw(self.brain, "ask"), raw(view, "show"), raw(self.clock, "now"))
        return tc.start(ports, tb.config(max_pc, budget))

    def keys(self, prefix):
        return [x["key"] for x in self.log.lines if x["key"].startswith(prefix)]


def text(i, body):
    return {"id": i, "kind": "text", "text": body}


class FlowTests(unittest.TestCase):
    def test_turn_writes_in_then_reply_then_acks_and_views(self):
        w = World(text("a1", "merhaba"))
        model = w.core().step()
        self.assertEqual([x["key"] for x in w.log.lines], ["in:pc:a1", "reply:a1"])
        self.assertEqual(w.log.lines[1]["text"], "cevap: merhaba")
        self.assertEqual(w.inbox.items, [])
        self.assertEqual(w.inbox.acked[0][1], True)
        self.assertEqual(w.shown[-1], model)
        self.assertEqual(model["turns"][0]["state"], "reply")

    def test_refused_item_gets_one_keyed_turkish_line_and_never_blocks(self):
        w = World({"id": 7, "kind": "text", "text": "x"}, text("a1", "selam"))
        w.core().step()
        refused = [x for x in w.log.lines if x["kind"] == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["text"], tb.REFUSED_TEXT)
        self.assertIn("reply:a1", w.keys("reply:"))
        self.assertEqual(w.inbox.items, [])

    def test_brain_failure_one_line_then_explicit_retry(self):
        w = World(text("a1", "soru"), fail={"soru"})
        core = w.core()
        core.step()
        self.assertEqual(w.log.get("reply:a1")["text"], tb.FAILED_TEXT)
        core.step()
        self.assertEqual(w.brain.asked, ["soru"])  # failure is terminal; no silent re-ask
        w.inbox.items.append({"id": "r1", "kind": "retry", "text": "a1"})
        core.step()
        self.assertEqual(w.log.get("reply:r1")["text"], "cevap: soru")
        self.assertEqual(len(w.keys("reply:a1")), 1)

    def test_retry_of_a_good_reply_is_refused(self):
        w = World(text("a1", "soru"), {"id": "r1", "kind": "retry", "text": "a1"})
        w.core().step()
        self.assertEqual(w.log.get("reply:r1")["kind"], "refused")
        self.assertEqual(w.brain.asked, ["soru"])

    def test_malformed_head_then_good_answered_within_two_steps(self):
        bad = [{"id": "b%d" % n, "kind": "voice", "text": "x"} for n in range(4)]
        w = World(*bad, text("g1", "iyi"))
        core = w.core(max_pc=4, budget=16)
        core.step()
        core.step()
        self.assertEqual(w.log.get("reply:g1")["kind"], "reply")
        self.assertEqual(w.inbox.items, [])

    def test_same_id_after_restart_exists_and_no_second_reply(self):
        w = World(text("a1", "merhaba"))
        w.core().step()
        w.inbox.items.append(text("a1", "merhaba"))
        w.core().step()  # restart: a new Core over the same raw ports
        self.assertEqual(w.log.append("in:pc:a1", {}), "exists")
        self.assertEqual(len(w.keys("reply:")), 1)
        self.assertEqual(w.brain.asked, ["merhaba"])
        self.assertEqual(w.inbox.items, [])


class UnknownTests(unittest.TestCase):
    def test_unknown_append_and_get_holds_key_then_bad_read_keeps_it(self):
        w = World(text("a1", "soru"))
        good_append, good_get, good_read = w.log.append, w.log.get, w.log.read
        w.log.append = w.log.get = lambda *a: "unknown"
        core = w.core()
        self.assertEqual(core.step()["held"], ["in:pc:a1"])
        self.assertEqual((w.brain.asked, len(w.inbox.items)), ([], 1))
        w.log.append, w.log.get, w.log.read = good_append, good_get, lambda: 5
        before = w.calls
        self.assertEqual(core.step()["held"], ["in:pc:a1"])
        self.assertEqual(w.calls - before, 3)  # clock, read, view: no take, get, brain or write
        self.assertEqual((w.brain.asked, w.log.lines), ([], []))
        w.log.read = good_read
        core.step()  # absent now: the key is released and the item retried
        self.assertEqual(w.log.get("reply:a1")["text"], "cevap: soru")
        self.assertEqual(core.model()["held"], [])

    def test_append_unknown_but_written_counts_as_done(self):
        w = World(text("a1", "soru"))
        real = w.log.append
        w.log.append = lambda k, line: (real(k, line), "unknown")[1]
        w.core().step()
        self.assertEqual(len(w.log.lines), 2)
        self.assertEqual(w.inbox.items, [])

    def test_answer_after_exists_is_discarded(self):
        w = World(text("a1", "soru"))
        real = w.brain.ask
        w.brain.ask = lambda q: (w.log.append("reply:a1", {"key": "reply:a1", "kind": "reply",
                                                          "text": "ilk", "time": 1}), real(q))[1]
        w.core().step()
        self.assertEqual(w.keys("reply:"), ["reply:a1"])
        self.assertEqual(w.log.get("reply:a1")["text"], "ilk")

    def test_bad_brain_reply_is_failure(self):
        for label, reply in (("over", "x" * (tb.REPLY_CAP + 1)), ("empty", ""), ("blank", "  "),
                             ("int", 5), ("none", None), ("nul", "a\x00b")):
            with self.subTest(label):
                w = World(text("a1", "soru"))
                w.brain.ask = lambda q, r=reply: r
                w.core().step()
                self.assertEqual(w.log.get("reply:a1")["text"], tb.FAILED_TEXT)
                self.assertEqual(w.inbox.items, [])

    def test_bad_config_refuses_construction(self):
        w = World()
        for args in ((0, 40), (101, 400), (True, 40), (4.0, 40), (4, 15), (4, 10_001),
                     (4, True), (4, "40"), (None, None), (10**400, 40)):
            with self.subTest(repr(args)[:40]):
                self.assertTrue(w.core(*args).refused)


def once(real, key, result):
    """The first call for key gives result (or raises before any write); later calls pass through."""
    hit = []

    def call(k, *rest):
        if k == key and not hit:
            hit.append(k)
            if result == "raise":
                raise OSError("before")
            return result
        return real(k, *rest)
    return call


class PendingAnswerTests(unittest.TestCase):
    """An answer whose reply line did not land is kept and its write retried; never re-asked."""

    def world(self, *items, fail=()):
        w = World(*(items or (text("a1", "soru"),)), fail=fail)
        real = w.brain.ask
        w.brain.ask = lambda q: real(q) + " #%d" % len(w.brain.asked)  # each answer differs
        return w

    def test_reply_append_not_landed_then_later_steps_never_reask(self):
        for label, result in (("failed", "failed"), ("raise_before", "raise"), ("wrong_type", object())):
            with self.subTest(label):
                w = self.world()
                w.log.append = once(w.log.append, "reply:a1", result)
                core = w.core()
                core.step()
                self.assertNotIn("reply:a1", w.keys("reply:"))
                self.assertEqual(len(w.inbox.items), 1)  # not acked before its reply line
                for _ in range(3):
                    core.step()
                self.assertEqual(w.brain.asked, ["soru"])
                self.assertEqual(w.log.get("reply:a1")["text"], "cevap: soru #1")
                self.assertEqual((w.inbox.items, w.inbox.acked[0][1]), ([], True))
                self.assertEqual((core.pending, core.model()["held"]), ({}, []))

    def test_unknown_append_and_get_holds_then_absent_retries_the_same_answer(self):
        w = self.world()
        real_get, seen = w.log.get, []
        w.log.get = lambda k: (seen.append(k) or "unknown") if k == "reply:a1" and w.brain.asked and not seen \
            else real_get(k)
        w.log.append = once(w.log.append, "reply:a1", "raise")
        core = w.core()
        self.assertEqual(core.step()["held"], ["reply:a1"])
        core.step()
        self.assertEqual(w.brain.asked, ["soru"])
        self.assertEqual(w.log.get("reply:a1")["text"], "cevap: soru #1")
        self.assertEqual(w.inbox.items, [])

    def test_failure_line_not_landed_is_retried_without_a_new_ask(self):
        w = World(text("a1", "soru"), fail={"soru"})
        w.log.append = once(w.log.append, "reply:a1", "failed")
        core = w.core()
        for _ in range(3):
            core.step()
        self.assertEqual(w.brain.asked, ["soru"])
        self.assertEqual(w.log.get("reply:a1")["text"], tb.FAILED_TEXT)

    def test_bad_read_keeps_the_pending_answer(self):
        w = self.world()
        w.log.append = once(w.log.append, "reply:a1", "failed")
        core = w.core()
        core.step()
        good_read, w.log.read = w.log.read, lambda: 5
        core.step()
        self.assertIn("reply:a1", core.pending)
        w.log.read = good_read
        core.step()
        self.assertEqual((w.brain.asked, w.log.get("reply:a1")["text"]), (["soru"], "cevap: soru #1"))

    def test_line_stored_by_another_writer_wins_and_releases_the_answer(self):
        w = self.world()
        w.log.append = once(w.log.append, "reply:a1", "failed")
        core = w.core()
        core.step()
        w.log.append("reply:a1", {"key": "reply:a1", "kind": "reply", "text": "ilk", "time": 1})
        core.step()
        self.assertEqual((w.brain.asked, w.log.get("reply:a1")["text"]), (["soru"], "ilk"))
        self.assertEqual((core.pending, w.inbox.items), ({}, []))

    def test_in_append_unknown_without_budget_for_its_get_holds_the_key(self):
        # budget 17, max_pc 4: clock+read+take 3, two good turns 5 each, a refused item 2,
        # so the in-line append of "t" leaves 1 unit: its get is not afforded
        w = World(text("g1", "q1"), text("g2", "q2"), {"id": 7, "kind": "text", "text": "x"}, text("t", "q3"))
        w.log.append = once(w.log.append, "in:pc:t", "unknown")
        core = w.core(max_pc=4, budget=17)
        self.assertEqual(core.step()["held"], ["in:pc:t"])
        self.assertEqual(len(w.inbox.items), 1)
        core.step()
        self.assertEqual(w.brain.asked, ["q1", "q2", "q3"])
        self.assertEqual((w.log.get("reply:t")["text"], w.inbox.items), ("cevap: q3", []))

    def test_terminal_oracle_matches_the_items_own_key(self):
        log, bad, good = FakeLog(), {"id": 7, "kind": "text", "text": "x"}, text("a1", "x")
        log.append("k", {"key": "in:pc:" + tb._tag(bad), "kind": "refused", "text": "r", "time": 1})
        self.assertEqual((log.terminal_for(bad), log.terminal_for(good)), (True, False))
        log.append("k", {"key": "in:pc:a1", "kind": "text", "text": "x", "time": 1})
        self.assertFalse(log.terminal_for(good))
        log.append("k", {"key": "reply:a1", "kind": "reply", "text": "y", "time": 1})
        self.assertTrue(log.terminal_for(good))


class BudgetTests(unittest.TestCase):
    def test_calls_per_step_never_exceed_budget_and_queue_drains(self):
        w = World(*(text("a%d" % n, "q%d" % n) for n in range(9)))
        core = w.core(max_pc=4, budget=16)
        for _ in range(8):
            before = w.calls
            core.step()
            self.assertLessEqual(w.calls - before, 16)
        self.assertEqual(w.inbox.items, [])
        self.assertEqual(len(w.keys("reply:")), 9)

    def test_brain_asked_once_per_id_at_every_budget(self):
        for max_pc in (1, 3, 4):
            for budget in range(2 * max_pc + 8, 31):
                with self.subTest(max_pc=max_pc, budget=budget):
                    w = World({"id": 7, "kind": "text", "text": "x"},
                              *(text("g%d" % n, "q%d" % n) for n in range(1, 4)))
                    core = w.core(max_pc=max_pc, budget=budget)
                    for _ in range(8):
                        core.step()
                    self.assertEqual(w.brain.asked, ["q1", "q2", "q3"])
                    self.assertEqual(w.inbox.items, [])

    def test_view_gets_the_last_unit_even_when_ports_fail(self):
        w = World(text("a1", "q"))
        for name in ("append", "get", "read"):
            setattr(w.log, name, lambda *a: 1 / 0)
        core = w.core(max_pc=1, budget=10)
        core.step()
        self.assertEqual(len(w.shown), 1)


def scenario(at=None, mode=None):
    """Three PC turns, one refused item, one brain failure, a restart with a
    re-delivered id and a retry. Returns the World after quiescence."""
    w = World(text("a1", "s1"), {"id": 7, "kind": "text", "text": "x"}, text("a3", "s3"),
              fail={"s3"}, at=at, mode=mode)
    steps = []
    core = w.core()
    for _ in range(4):
        steps.append((w.calls, core.step(), w.calls))
    w.inbox.items += [text("a2", "s2"), text("a1", "s1"), {"id": "r3", "kind": "retry", "text": "a3"}]
    core = w.core()  # restart
    for _ in range(5):
        steps.append((w.calls, core.step(), w.calls))
    return w, steps


class FaultMatrixTests(unittest.TestCase):
    def test_every_call_every_fault(self):
        clean, _ = scenario()
        self.assertEqual(clean.brain.asked, ["s1", "s3", "s2", "s3"])
        self.assertEqual(clean.log.get("reply:r3")["text"], "cevap: s3")
        for at in range(clean.calls):
            for mode in ("raise_before", "write_then_raise", "wrong_type"):
                with self.subTest(at=at, mode=mode):
                    w, steps = scenario(at, mode)
                    self.assertTrue(all(b - a <= 40 for a, _, b in steps))
                    keys = [x["key"] for x in w.log.lines]
                    self.assertEqual(len(keys), len(set(keys)))
                    for rid in ("a1", "a2", "a3", "r3"):
                        self.assertIn("in:pc:" + rid, keys)
                        self.assertIn("reply:" + rid, keys)
                    self.assertEqual(len([k for k in keys if k.startswith("in:pc:~")]), 1)
                    self.assertEqual(w.inbox.items, [])
                    self.assertEqual(len(w.inbox.acked), 6)
                    self.assertTrue(all(done for _, done in w.inbox.acked))
                    self.assertEqual(steps[-1][1]["held"], [])


def source(name):
    return (ROOT / "jarvis" / (name + ".py")).read_text(encoding="utf-8")


class StaticTests(unittest.TestCase):
    def test_core_has_no_shape_checks_of_its_own(self):
        tree = ast.parse(source("talk_core"))
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertEqual(called & {"isinstance", "type", "len", "issubclass", "hasattr"}, set())

    def test_core_takes_its_records_from_the_boundary_only(self):
        tree = ast.parse(source("talk_core"))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual([getattr(n, "module", None) for n in imports], ["jarvis.talk_boundary"])

    def test_boundary_imports_only_the_standard_library(self):
        tree = ast.parse(source("talk_boundary"))
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(mods, {"hashlib", "json", "math", "re", "dataclasses"})

    def test_no_executor_tool_command_network_or_chat_port(self):
        banned = ("subprocess", "socket", "executor", "tool", "command", "urllib", "http",
                  "requests", "tele" + "gram", "heartbeat", "project_" + "folders", "os.", "eval(")
        for name in ("talk_core", "talk_boundary"):
            text = source(name).lower()
            for word in banned:
                with self.subTest(name=name, word=word):
                    self.assertNotIn(word, text)
            self.assertEqual(set(__import__("re").findall(r"talk_\w+", text)) - {"talk_boundary"}, set())
