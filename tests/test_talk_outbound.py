import ast
import json
import unittest
from pathlib import Path

from jarvis.talk_outbound import CAPS, pump
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, deep_json, each

SOURCE = Path(__file__).resolve().parents[1] / "jarvis" / "talk_outbound.py"


def act(entry_id=1, view="pc", part=1, text="hi", **extra):
    return {"entry_id": entry_id, "view": view, "part": part, "text": text, "kind": "text", **extra}


class Store:
    def __init__(self, states=None, fail_on=None):
        self.states = dict(states or {})
        self.log = []
        self.fail_on = fail_on  # (view, state) whose record raises

    def record(self, entry_id, view, part, state, attempts):
        if self.fail_on == (view, state):
            raise OSError("disk full")
        self.log.append((entry_id, view, part, state, attempts))
        self.states[(entry_id, view, part)] = {"state": state, "attempts": attempts}

    def was_sent(self, entry_id, view, part):
        return self.states.get((entry_id, view, part))


class Sender:
    def __init__(self, reply=None):
        self.sent = []
        self.reply = {"ok": True} if reply is None else reply

    def __call__(self, action):
        self.sent.append((action["entry_id"], action["part"]))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply(action) if callable(self.reply) else self.reply


def run(actions, store=None, pc=None, telegram=None):
    store = store or Store()
    senders = {"pc": pc or Sender(), "telegram": telegram or Sender()}
    out = pump(actions, senders, store.record, store.was_sent)
    return out, store, senders


def results(out):
    return [(r["entry_id"], r["view"], r["part"], r["result"]) for r in out["results"]]


class AcceptanceTests(unittest.TestCase):
    def test_1_key_is_entry_view_part_and_sent_key_is_skipped(self):
        store = Store({(1, "pc", 1): {"state": "sent", "attempts": 1}})
        out, store, senders = run([act(part=1), act(part=2), act(view="telegram", part=1)], store)
        self.assertTrue(out["ok"])
        self.assertEqual(senders["pc"].sent, [(1, 2)])
        self.assertEqual(senders["telegram"].sent, [(1, 1)])
        self.assertIn((1, "pc", 1, "skipped"), results(out))

    def test_2_records_sending_then_result_with_attempts_plus_one(self):
        store = Store({(1, "pc", 1): {"state": "failed", "attempts": 2}})
        run([act(), act(entry_id=2)], store, pc=Sender(lambda a: {"ok": a["entry_id"] == 2}))
        self.assertEqual(store.log, [(1, "pc", 1, "sending", 2), (1, "pc", 1, "failed", 3),
                                     (2, "pc", 1, "sending", 0), (2, "pc", 1, "sent", 1)])

    def test_3_parts_go_in_order_and_a_failed_part_stops_later_parts(self):
        pc = Sender(lambda a: {"ok": a["part"] != 2})
        out, store, _ = run([act(part=1), act(part=2), act(part=3)], pc=pc)
        self.assertEqual(pc.sent, [(1, 1), (1, 2)])
        self.assertEqual(results(out), [(1, "pc", 1, "sent"), (1, "pc", 2, "failed"),
                                        (1, "pc", 3, "blocked")])
        self.assertNotIn((1, "pc", 3), store.states)

    def test_4_one_view_failing_never_blocks_the_other(self):
        actions = [act(view=v, part=p) for v in ("pc", "telegram") for p in (1, 2)]
        out, _, senders = run(actions, pc=Sender(RuntimeError("down")))
        self.assertEqual(senders["telegram"].sent, [(1, 1), (1, 2)])
        self.assertEqual(senders["pc"].sent, [(1, 1)])
        out, _, senders = run(actions, store=Store(fail_on=("telegram", "sending")))
        self.assertEqual(senders["pc"].sent, [(1, 1), (1, 2)])
        self.assertEqual(senders["telegram"].sent, [])

    def test_5_sending_without_result_is_unknown_and_not_resent(self):
        store = Store({(1, "pc", 1): {"state": "sending", "attempts": 0}})
        out, store, senders = run([act(part=1), act(part=2), act(entry_id=2)], store)
        self.assertEqual(results(out), [(1, "pc", 1, "unknown"), (1, "pc", 2, "blocked"),
                                        (2, "pc", 1, "sent")])
        self.assertEqual(senders["pc"].sent, [(2, 1)])
        self.assertEqual(store.states[(1, "pc", 1)]["state"], "sending")


class NegativeTests(unittest.TestCase):
    def test_1_same_action_twice_sends_once(self):
        out, _, senders = run([act(), act(), act(view="telegram"), act(view="telegram")])
        self.assertEqual(senders["pc"].sent, [(1, 1)])
        self.assertEqual(senders["telegram"].sent, [(1, 1)])
        self.assertEqual([r for *_, r in results(out)].count("duplicate"), 2)

    def test_2_sender_raises_or_returns_non_result_is_failed_and_rest_proceed(self):
        bad = (None, True, 1, "ok", [], {"ok": 1}, {"ok": "true"}, {}, RuntimeError("x"),
               RecursionError(), ValueError())

        def check(reply):
            def answer(action):
                if action["entry_id"] != 1:
                    return {"ok": True}
                if isinstance(reply, BaseException):
                    raise reply
                return reply

            out, store, _ = run([act(entry_id=1), act(entry_id=2)], pc=Sender(answer))
            self.assertEqual(results(out), [(1, "pc", 1, "failed"), (2, "pc", 1, "sent")])
            self.assertEqual(store.states[(1, "pc", 1)], {"state": "failed", "attempts": 1})

        each(self, bad, check)

    def test_3_unknown_view_is_refused(self):
        def check(view):
            out, store, senders = run([act(view=view), act(entry_id=2)])
            self.assertEqual(out["results"][0]["result"], "refused")
            self.assertEqual(out["results"][0]["reason"], "unknown_view")
            self.assertEqual(senders["pc"].sent, [(2, 1)])
            self.assertEqual(len(store.log), 2)

        each(self, ("email", "PC", "pc ", None, 1, ["pc"]) + HOSTILE_TEXT, check)

    def test_3b_known_view_without_a_sender_is_refused(self):
        store = Store()
        out = pump([act(view="telegram")], {"pc": Sender()}, store.record, store.was_sent)
        self.assertEqual(out["results"][0]["reason"], "no_sender")
        self.assertEqual(store.log, [])

    def test_4_record_raises_after_send_is_sent_unrecorded_and_view_stops(self):
        actions = [act(part=1), act(part=2), act(entry_id=2), act(view="telegram")]
        store = Store(fail_on=("pc", "sent"))
        out, store, senders = run(actions, store)
        self.assertEqual(results(out), [(1, "pc", 1, "sent_unrecorded"), (1, "pc", 2, "stopped"),
                                        (2, "pc", 1, "stopped"), (1, "telegram", 1, "sent")])
        self.assertEqual(senders["pc"].sent, [(1, 1)])
        store.fail_on = None  # restart: the store still says "sending", so no silent resend
        out, _, senders = run(actions, store)
        self.assertEqual(results(out)[0], (1, "pc", 1, "unknown"))
        self.assertEqual(senders["pc"].sent, [(2, 1)])
        self.assertEqual(senders["telegram"].sent, [])

    def test_5_part_2_before_part_1_is_sent_in_part_order(self):
        out, _, senders = run([act(part=3), act(part=2), act(part=1)])
        self.assertEqual(senders["pc"].sent, [(1, 1), (1, 2), (1, 3)])
        self.assertEqual([r["index"] for r in out["results"]], [2, 1, 0])

    def test_6_text_over_cap_is_refused_and_not_sent(self):
        cap = CAPS["telegram"]
        actions = [act(view="telegram", text="x" * (cap + 1)), act(view="telegram", part=2),
                   act(entry_id=2, view="telegram", text="x" * cap)]
        out, store, senders = run(actions)
        self.assertEqual(results(out), [(1, "telegram", 1, "refused"), (1, "telegram", 2, "blocked"),
                                        (2, "telegram", 1, "sent")])
        self.assertEqual(out["results"][0]["reason"], "text_over_cap")
        self.assertEqual(senders["telegram"].sent, [(2, 1)])
        self.assertNotIn((1, "telegram", 1), store.states)

    def test_7_part_goes_only_when_every_lower_part_is_stored_sent(self):
        unreadable = object()  # was_sent raises for part 1
        stored = {"sent": {"state": "sent", "attempts": 1}, "failed": {"state": "failed", "attempts": 1},
                  "sending": {"state": "sending", "attempts": 0}, "missing": None,
                  "bad_state": {"state": "done", "attempts": 1}, "unreadable": unreadable}
        # (part 1 stored state, part 1 in this call, part 1 send ok) -> (results, sends)
        table = [
            ("sent", True, True, ["skipped", "sent"], [(1, 2)]),
            ("failed", True, True, ["sent", "sent"], [(1, 1), (1, 2)]),
            ("failed", True, False, ["failed", "blocked"], [(1, 1)]),
            ("sending", True, True, ["unknown", "blocked"], []),
            ("missing", True, True, ["sent", "sent"], [(1, 1), (1, 2)]),
            ("missing", True, False, ["failed", "blocked"], [(1, 1)]),
            ("bad_state", True, True, ["refused", "blocked"], []),
            ("unreadable", True, True, ["refused", "blocked"], []),
            ("sent", False, True, ["sent"], [(1, 2)]),
            ("failed", False, True, ["held"], []),
            ("sending", False, True, ["held"], []),
            ("missing", False, True, ["held"], []),
            ("bad_state", False, True, ["held"], []),
            ("unreadable", False, True, ["held"], []),
        ]
        for state, in_call, ok, want, sends in table:
            with self.subTest(state=state, in_call=in_call, ok=ok):
                store = Store()
                if stored[state] is not None:
                    store.states[(1, "pc", 1)] = stored[state]
                real = store.was_sent

                def was_sent(*key, real=real):
                    if real(*key) is unreadable:
                        raise OSError("db locked")
                    return real(*key)

                pc = Sender(lambda a, ok=ok: {"ok": ok or a["part"] != 1})
                actions = ([act(part=1)] if in_call else []) + [act(part=2)]
                out = pump(actions, {"pc": pc}, store.record, was_sent)
                self.assertEqual([r["result"] for r in out["results"]], want)
                self.assertEqual(pc.sent, sends)
                self.assertNotEqual(store.states.get((1, "pc", 2), {}).get("state"), "sending")
                if want[-1] == "held":
                    self.assertEqual(out["results"][-1]["reason"], "lower_part_not_sent")
                    self.assertNotIn((1, "pc", 2), store.states)

    def test_8_every_lower_part_is_checked_not_only_the_one_below(self):
        sent = {"state": "sent", "attempts": 1}
        for gap in ("failed", "sending", None):
            with self.subTest(gap=gap):
                states = {(1, "pc", 1): sent, (1, "telegram", 1): sent, (1, "telegram", 2): sent}
                if gap:
                    states[(1, "pc", 2)] = {"state": gap, "attempts": 1}
                out, store, senders = run([act(part=1), act(part=3), act(part=4), act(view="telegram", part=3),
                                           act(entry_id=2, part=2)], Store(states))
                self.assertEqual(results(out), [(1, "pc", 1, "skipped"), (1, "pc", 3, "held"),
                                                (1, "pc", 4, "blocked"), (2, "pc", 2, "held"),
                                                (1, "telegram", 3, "sent")])
                self.assertEqual(senders["pc"].sent, [])
                self.assertEqual(senders["telegram"].sent, [(1, 3)])

    def test_9_restart_never_sends_a_part_past_a_failed_or_crashed_lower_part(self):
        store = Store()
        three = [act(part=1), act(part=2), act(part=3)]
        out, store, senders = run(three, store, pc=Sender(lambda a: {"ok": a["part"] != 2}))
        self.assertEqual([r["result"] for r in out["results"]], ["sent", "failed", "blocked"])
        out, store, senders = run([act(part=3)], store)  # restart: only part 3 is planned again
        self.assertEqual(results(out), [(1, "pc", 3, "held")])
        self.assertEqual(senders["pc"].sent, [])
        out, store, senders = run([act(part=2)], store)
        out, store, senders = run([act(part=3)], store)
        self.assertEqual(results(out), [(1, "pc", 3, "sent")])
        # A crash mid-send leaves part 1 "sending": part 2 alone after the restart is held.
        crashed = Store({(5, "pc", 1): {"state": "sending", "attempts": 0}})
        out, crashed, senders = run([act(entry_id=5, part=2)], crashed)
        self.assertEqual(results(out), [(5, "pc", 2, "held")])
        self.assertEqual((senders["pc"].sent, crashed.log), ([], []))


class HostileInputTests(unittest.TestCase):
    def assert_typed(self, out):
        self.assertIsInstance(out, dict)
        self.assertIn(out["ok"], (True, False))
        for r in out.get("results", [out]):
            self.assertTrue(str(r.get("reason", "")).isascii())

    def test_hostile_numbers_as_part_or_entry_id_are_refused_unless_exact_type(self):
        def check(value):
            valid = {"part": type(value) is int and value >= 1, "entry_id": type(value) in (int, str)}
            for field in ("part", "entry_id"):
                out, store, _ = run([act(**{field: value})])
                self.assert_typed(out)
                # A valid part above 1 with no part 1 stored sent is held, never sent.
                expected = ("held" if field == "part" and value != 1 else "sent") if valid[field] else "refused"
                self.assertEqual(out["results"][0]["result"], expected)
                self.assertEqual(len(store.log), 2 if expected == "sent" else 0)

        each(self, HOSTILE_NUMBERS + (0, -1, 1.0), check)

    def test_hostile_text_is_passed_through_or_refused_never_raises(self):
        def check(text):
            out, _, senders = run([act(text=text)])
            self.assert_typed(out)
            if not isinstance(text, str):
                self.assertEqual(out["results"][0]["reason"], "bad_text")
            elif len(text) > CAPS["pc"]:
                self.assertEqual(out["results"][0]["reason"], "text_over_cap")
            else:
                self.assertEqual(out["results"][0]["result"], "sent")

        each(self, HOSTILE_TEXT + (deep_json(), None, ["x"]), check)

    def test_deeply_nested_input_is_typed(self):
        nested = []
        for _ in range(5000):
            nested = [nested]
        for actions in ([nested], [act(text=nested)], [{"entry_id": nested}]):
            out, store, _ = run(actions)
            self.assert_typed(out)
            self.assertEqual(out["results"][0]["result"], "refused")
            self.assertEqual(store.log, [])

    def test_bad_top_level_input_is_refused(self):
        store = Store()
        for args in ((None, {}, store.record, store.was_sent), ("abc", {}, store.record, store.was_sent),
                     ([act()], [], store.record, store.was_sent), ([act()], {}, None, store.was_sent),
                     ([act()], {}, store.record, "x")):
            self.assertEqual(pump(*args), {"ok": False, "reason": "bad_input"})

    def test_was_sent_garbage_or_raising_refuses_without_sending(self):
        class GetRaises(dict):
            def get(self, *args):
                raise ValueError("böse sk-secret-token")

        bad_state = ("sent", 1, {"state": "done", "attempts": 0}, {"state": "sent", "attempts": True},
                     {"state": "sent", "attempts": -1}, {"state": "sent"})
        # Collaborator errors: their text (secrets, non-ASCII, parser messages) must never become the reason.
        unreadable = (RecursionError(), OSError(), KeyError("k"), ValueError("böse sk-secret-token"),
                      ValueError("bad_state"), ValueError(), json.JSONDecodeError("Expecting value", "{", 1),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), GetRaises(state="sent"))

        def check(value, reason):
            def was_sent(*key):
                if isinstance(value, BaseException):
                    raise value
                return value

            pc = Sender()
            out = pump([act(), act(entry_id=2)], {"pc": pc}, Store().record, was_sent)
            self.assert_typed(out)
            self.assertEqual([(r["result"], r["reason"]) for r in out["results"]], [("refused", reason)] * 2)
            self.assertEqual(pc.sent, [])

        each(self, bad_state, lambda v: check(v, "bad_state"))
        each(self, unreadable, lambda v: check(v, "state_unreadable"))

    def test_action_key_not_exactly_str_is_refused_and_never_sent(self):
        class Stateful(str):  # equals its text only when armed, once: dedup reads one value, send() another
            armed = False

            def __eq__(self, other):
                hit, Stateful.armed = Stateful.armed, False
                return hit and str.__eq__(self, other)

            __hash__ = str.__hash__

        class Plain(str):
            pass

        class Mimic:
            def __eq__(self, other):
                return other == "entry_id"

            def __hash__(self):
                return hash("entry_id")

        def check(key):
            hostile = {key: 1, **act(entry_id=2, text="x")}
            Stateful.armed = True
            out, store, senders = run([act(entry_id=2, text="y"), hostile])
            self.assert_typed(out)
            by_index = {r["index"]: r for r in out["results"]}
            self.assertEqual(by_index[1], {"index": 1, "entry_id": None, "view": None, "part": None,
                                                 "result": "refused", "reason": "bad_action"})
            self.assertEqual(senders["pc"].sent, [(2, 1)])
            self.assertEqual(store.log, [(2, "pc", 1, "sending", 0), (2, "pc", 1, "sent", 1)])

        each(self, (Stateful("entry_id"), Plain("entry_id"), Plain("note"), Mimic(), 1, None,
                    ("entry_id",), b"entry_id"), check)

    def test_record_raising_recursion_error_is_typed(self):
        def record(*args):
            raise RecursionError()

        pc = Sender()
        out = pump([act(part=1), act(part=2)], {"pc": pc}, record, Store().was_sent)
        self.assertEqual([r["result"] for r in out["results"]], ["refused", "stopped"])
        self.assertEqual(pc.sent, [])


class ModuleTests(unittest.TestCase):
    def test_module_imports_no_io_and_no_talk_or_telegram_module(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        banned = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "shutil",
                  "sqlite3", "requests", "asyncio", "ssl", "time", "datetime", "random"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                parts = name.split(".")
                self.assertNotIn(parts[0], banned, name)
                self.assertFalse(any(p.startswith(("talk_", "telegram")) for p in parts), name)
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"open", "__import__", "eval", "exec", "compile"})

    def test_all_module_strings_are_ascii(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertTrue(node.value.isascii(), node.value[:40])


if __name__ == "__main__":
    unittest.main()
