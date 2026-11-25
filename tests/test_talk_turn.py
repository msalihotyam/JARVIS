import ast
import dataclasses
import unittest
from collections.abc import Mapping
from pathlib import Path

import jarvis.talk_turn as tt
from jarvis.talk_turn import FAILED, FAILED_APPEND, REFUSED, SKIP, DONE, Reply, process
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each, narrow_stdout

HOSTILE_KEYS = ("action", "command", "tool", "approve", "key", "exec", "path", "argv")


def salih(entry_id=7, text="hello", focus="talk", kind="text", who="salih"):
    return {"id": entry_id, "who": who, "kind": kind, "text": text, "focus": focus}


class Fake:
    """Stateful fake store: append_entry records, has_reply reads the record."""

    def __init__(self, answer=None, append_ok=True):
        self.calls, self.log = [], []
        self.answer = {"text": "ok", "needs_you": False, "proposals": []} if answer is None else answer
        self.append_ok = append_ok

    def steps(self):
        return {"build_packet": self.build_packet, "ask_brain": self.ask_brain,
                "append_entry": self.append_entry, "has_reply": self.has_reply}

    def has_reply(self, entry_id):
        self.calls.append(("has_reply", entry_id))
        return any(r["reply_to"] == entry_id for r in self.log)

    def build_packet(self, entry):
        self.calls.append(("build_packet", entry["id"]))
        return "PACKET:" + entry["text"]

    def ask_brain(self, *args, **kwargs):
        self.calls.append(("ask_brain", args, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def append_entry(self, record):
        self.calls.append(("append_entry", dict(record)))
        if not self.append_ok:
            raise OSError("disk full")
        self.log.append(dict(record))

    def names(self):
        return [c[0] for c in self.calls]


class AcceptanceTests(unittest.TestCase):
    def test_order_packet_brain_append_with_reply_to_and_focus(self):
        fake = Fake({"text": "Hi Salih", "needs_you": True, "proposals": ["dec-1"]})
        out = process(salih(focus="plans"), fake.steps())
        self.assertEqual(out.status, DONE)
        self.assertEqual(fake.names(), ["has_reply", "build_packet", "ask_brain", "append_entry"])
        self.assertEqual(fake.calls[2][1:], (("PACKET:hello",), {}))  # brain gets the packet only
        self.assertEqual(fake.log, [{"who": "jarvis", "kind": "text", "text": "Hi Salih",
                                     "focus": "plans", "reply_to": 7}])
        self.assertEqual(out.reply, Reply("Hi Salih", True, ("dec-1",)))

    def test_has_reply_true_skips_without_brain(self):
        fake = Fake()
        fake.log.append({"reply_to": 7})
        self.assertEqual(process(salih(), fake.steps()).status, SKIP)
        self.assertEqual(fake.names(), ["has_reply"])

    def test_brain_raises_or_returns_nothing_failed_nothing_appended(self):
        for answer in (RuntimeError("x"), RecursionError(), {}, "", [], ()):
            fake = Fake()
            fake.answer = answer
            out = process(salih(), fake.steps())
            self.assertEqual(out.status, FAILED, repr(answer))
            self.assertEqual(fake.log, [])
        fake = Fake()
        fake.ask_brain = lambda packet: None
        self.assertEqual(process(salih(), fake.steps()).status, FAILED)

    def test_append_fails_returns_reply_and_retry_skips_brain(self):
        fake = Fake({"text": "answer", "needs_you": False, "proposals": []}, append_ok=False)
        out = process(salih(), fake.steps())
        self.assertEqual((out.status, out.reply), (FAILED_APPEND, Reply("answer", False, ())))
        fake.append_ok = True
        again = process(salih(), fake.steps(), out.reply)
        self.assertEqual(again.status, DONE)
        self.assertEqual(fake.names().count("ask_brain"), 1)
        self.assertEqual(fake.log[0]["text"], "answer")
        fake2 = Fake()
        fake2.append_entry = lambda record: False
        self.assertEqual(process(salih(), fake2.steps()).status, FAILED_APPEND)

    def test_forged_retry_reply_refused(self):
        forged = (Reply("x", "yes", ()), Reply("", False, ()), Reply("x", False, ["a"]),
                  Reply("x", False, ("../a",)), {"text": "x"}, "x")
        for reply in forged:
            fake = Fake()
            self.assertEqual(process(salih(), fake.steps(), reply).status, REFUSED, repr(reply))
            self.assertEqual(fake.log, [])
            self.assertNotIn("ask_brain", fake.names())

    def test_reply_shape_is_text_needs_you_proposals_only(self):
        self.assertEqual([f.name for f in dataclasses.fields(Reply)], ["text", "needs_you", "proposals"])
        out = process(salih(), Fake({"text": "t", "proposals": ["a", "b"]}).steps())
        self.assertIs(type(out.reply.proposals), tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            out.reply.text = "y"

    def test_deterministic(self):
        runs = [process(salih(), Fake({"text": "t", "x": 1, "proposals": ["a", ".."]}).steps())
                for _ in range(3)]
        self.assertEqual(len(set(runs)), 1)


class StaticTests(unittest.TestCase):
    def test_no_jarvis_io_network_or_action_surface(self):
        tree = ast.parse(Path(tt.__file__).read_text(encoding="utf-8"))
        imported = set()
        banned = ("tool", "action", "exec", "subprocess", "network", "command", "socket")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "exec", "eval", "compile", "__import__"})
            elif isinstance(node, (ast.FunctionDef, ast.Lambda)):
                args = node.args
                for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                    if a is not None:
                        self.assertFalse(any(b in a.arg.lower() for b in banned), a.arg)
        self.assertLessEqual(imported, {"__future__", "re", "collections.abc", "dataclasses"})
        self.assertEqual(tt.STEP_NAMES, {"build_packet", "ask_brain", "append_entry", "has_reply"})

    def test_reason_texts_ascii(self):
        with narrow_stdout():
            for reason, text in tt.REASONS.items():
                self.assertTrue(text.isascii() and text.isprintable(), reason)
                print(tt.Outcome(REFUSED, reason).message)


class Hostile(dict):
    """A Mapping whose every read raises."""

    def get(self, *a):
        raise RuntimeError("boom")

    def keys(self):
        raise RuntimeError("boom")


class NegativeTests(unittest.TestCase):
    def test_brain_action_fields_dropped_counted_never_passed_on(self):
        answer = {"text": "t", "needs_you": False, "proposals": [], 1: "x"}
        answer.update({k: "rm -rf" for k in HOSTILE_KEYS})
        fake = Fake(answer)
        out = process(salih(), fake.steps())
        self.assertEqual((out.status, out.dropped_fields), (DONE, len(HOSTILE_KEYS) + 1))
        self.assertEqual(set(fake.log[0]), {"who", "kind", "text", "focus", "reply_to"})
        self.assertNotIn("rm -rf", repr(out) + repr(fake.calls))

    def test_non_salih_or_approval_entries_refused_before_any_step(self):
        whos = ("Salih", "SALIH", " salih", "jarvis", "system", None, 1, b"salih") + HOSTILE_TEXT[:5]
        kinds = ("approval_tap", "approval_card", "TEXT", "", None, 1)
        cases = [salih(who=w) for w in whos] + [salih(kind=k) for k in kinds]
        cases += [salih(entry_id=v) for v in (True, -1, "7", 7.0, None)]
        cases += [salih(text=None), salih(focus=1), None, "salih", ["salih"]]
        for entry in cases:
            fake = Fake()
            self.assertEqual(process(entry, fake.steps()).status, REFUSED, repr(entry)[:80])
            self.assertEqual(fake.calls, [])

    def test_bad_reply_text_failed_nothing_appended(self):
        def check(text):
            fake = Fake({"text": text, "needs_you": True})
            self.assertEqual(process(salih(), fake.steps()).status, FAILED)
            self.assertEqual(fake.log, [])
        each(self, (None, 1, b"x", ["x"], "", "   ", "a" * 4001) + tuple(v for v in HOSTILE_NUMBERS if type(v) is not str), check)
        self.assertEqual(process(salih(), Fake({"text": "a" * 4000}).steps()).status, DONE)

    def test_needs_you_not_bool_is_false(self):
        def check(value):
            out = process(salih(), Fake({"text": "t", "needs_you": value}).steps())
            self.assertIs(out.reply.needs_you, value is True)
        each(self, HOSTILE_NUMBERS + ("true", 1, [True]), check)

    def test_bad_proposal_names_dropped_counted(self):
        names = list(HOSTILE_PATH_PARTS) + ["a/b", "..", "a:b", "x~1", 7, None, "DATA"]
        out = process(salih(), Fake({"text": "t", "proposals": names}).steps())
        self.assertEqual(out.reply.proposals, ("DATA",))
        self.assertEqual(out.dropped_proposals, len(names) - 1)
        for value in ("abc", {"a": 1}, None):
            out = process(salih(), Fake({"text": "t", "proposals": value}).steps())
            self.assertEqual((out.reply.proposals, out.dropped_proposals), ((), 1))
        many = [f"p{i}" for i in range(20)]
        out = process(salih(), Fake({"text": "t", "proposals": many}).steps())
        self.assertEqual((len(out.reply.proposals), out.dropped_proposals), (16, 4))

    def test_entry_whose_get_lies_is_refused(self):
        class Liar(dict):
            def get(self, key, default=None):
                return {"who": "salih", "kind": "text"}.get(key, dict.get(self, key, default))
        fake = Fake()
        entry = Liar(salih(who="mallory", kind="approval_tap"))
        self.assertEqual(process(entry, fake.steps()).status, REFUSED)
        self.assertEqual(fake.calls, [])

    def test_str_subclass_key_eq_never_runs(self):
        class K(str):
            def __eq__(self, other):
                raise RuntimeError("eq ran")
            __hash__ = str.__hash__
        out = process(salih(), Fake({K("zz"): 1, "text": "hi"}).steps())
        self.assertEqual((out.status, out.dropped_fields), (DONE, 1))

    def test_same_entry_twice_second_is_skip(self):
        fake = Fake()
        first, second = process(salih(), fake.steps()), process(salih(), fake.steps())
        self.assertEqual((first.status, second.status), (DONE, SKIP))
        self.assertEqual(fake.names().count("ask_brain"), 1)
        self.assertEqual(len(fake.log), 1)

    def test_hostile_entry_text_reaches_packet_only(self):
        def check(text):
            fake = Fake()
            self.assertEqual(process(salih(text=text), fake.steps()).status, DONE)
            self.assertEqual(fake.log[0]["text"], "ok")
        each(self, HOSTILE_TEXT, check)

    def test_no_exception_escapes(self):
        def recurse(*a):
            return recurse(*a)
        fake = Fake()
        class Broken(Mapping):
            __getitem__ = __iter__ = __len__ = Hostile.keys
        self.assertEqual(process(Broken(), fake.steps()).status, FAILED)
        self.assertEqual(process(salih(), Fake(Hostile(text="t")).steps()).status, FAILED)
        for name in ("has_reply", "build_packet", "ask_brain"):
            steps = Fake().steps()
            steps[name] = recurse
            self.assertEqual(process(salih(), steps).status, FAILED, name)
        steps = Fake().steps()
        steps["append_entry"] = recurse
        self.assertEqual(process(salih(), steps).status, FAILED_APPEND)
        for bad in ({**fake.steps(), "run_tool": print}, {**fake.steps(), "has_reply": 1}, [], None):
            self.assertEqual(process(salih(), bad).status, REFUSED)
        self.assertEqual(fake.calls, [])
        for value in (None, 1, "", "x"):
            steps = Fake().steps()
            steps["has_reply"] = lambda i, v=value: v
            self.assertEqual(process(salih(), steps).status, FAILED)
