"""TALKTG1: Telegram text in over fake client, store and clock (no network, no real talk dir)."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import talk_boundary as tb
from jarvis import talk_core as tc
from jarvis import talk_tg_in as tti
from jarvis import telegram_client as tg
from jarvis import telegram_offset as off
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, each
from tests.test_talk_core import FakeBrain, FakeClock, FakeLog

ROOT = Path(__file__).resolve().parents[1]
CHAT = 4242


def text(uid, body="selam", chat=CHAT, ctype="private", **extra):
    msg = {"message_id": uid, "date": 1700000000, "chat": {"id": chat, "type": ctype},
           "from": {"id": chat, "is_bot": False}, "text": body}
    msg.update(extra)
    return {"update_id": uid, "message": msg}


def media(uid, key, value):
    msg = {"message_id": uid, "date": 1700000000, "chat": {"id": CHAT, "type": "private"}, key: value}
    return {"update_id": uid, "message": msg}


def voice(uid):
    return media(uid, "voice", {"file_id": "f", "duration": 3})


class Client:
    def __init__(self, *answers, repeat=None):
        self.answers, self.repeat, self.calls = list(answers), repeat, []

    def call(self, method, body):
        self.calls.append((method, dict(body)))
        got = self.answers.pop(0) if self.answers else self.repeat
        if isinstance(got, BaseException):
            raise got
        return tg.Ok([]) if got is None else got


class Store:
    def __init__(self, state=None, fail=False):
        self.state, self.fail, self.loads, self.saves = state, fail, 0, []

    def load(self):
        self.loads += 1
        if self.fail == "load":
            raise OSError("disk")
        return self.state

    def save(self, state):
        self.saves.append(state)
        if self.fail is True:
            raise OSError("disk")
        self.state = state

    @property
    def offset(self):
        return 0 if self.state is None else off.load_state(self.state)


class Clock:
    def __init__(self):
        self.calls = 0

    def now(self):
        self.calls += 1
        return 1700000100


def inbox(*answers, repeat=None, store=None, enabled=True):
    client, store, clock = Client(*answers, repeat=repeat), store or Store(), Clock()
    return tti.TgInbox(client, store, CHAT, clock, enabled=enabled), client, store, clock


def batch(*updates):
    return tg.Ok(list(updates))


class TakeTest(unittest.TestCase):
    def test_allowed_text_becomes_one_item(self):
        box, client, store, _ = inbox(batch(text(5, "merhaba")))
        items = box.take(10)
        self.assertEqual(items, [{"id": "tg.5", "kind": "text", "text": "merhaba", "handle": 5}])
        self.assertIsInstance(items, list)
        self.assertTrue(tb.ID_RE.fullmatch(items[0]["id"]))
        self.assertEqual(client.calls, [("getUpdates", {"offset": 0, "limit": 10, "timeout": 0})])
        self.assertEqual(box.counts, {"text": 1})
        self.assertEqual(store.saves, [])  # the text is pending: nothing passes it

    def test_limit_is_clamped_to_the_api_range(self):
        box, client, _, _ = inbox()
        box.take(500)
        self.assertEqual(client.calls[0][1]["limit"], 100)
        for bad in (0, -1, True, "3", None, 2.0):
            self.assertEqual(box.take(bad), [])
        self.assertEqual(len(client.calls), 1)

    def test_other_updates_are_consumed_and_counted_by_kind(self):
        others = [
            voice(1), media(2, "photo", [{"file_id": "p", "width": 1, "height": 1}]),
            media(3, "document", {"file_id": "d"}),
            {"update_id": 4, "callback_query": {"id": "c", "data": "x", "message": text(4)["message"]}},
            {"update_id": 5, "edited_message": text(5)["message"]}, text(6, chat=777),
            {"update_id": 7, "poll": {}},
        ]
        box, _, store, _ = inbox(batch(*others))
        self.assertEqual(box.take(10), [])
        self.assertEqual(box.counts, {"voice": 1, "photo": 1, "document": 1, "button": 1, "edited": 1,
                                      "chat_not_allowed": 1, "invalid_update": 1})
        self.assertEqual(store.offset, 8)

    def test_same_update_again_gives_the_same_item(self):
        box, client, _, _ = inbox(repeat=batch(text(9, "tekrar")))
        first, second = box.take(4), box.take(4)
        self.assertEqual(first, second)
        self.assertEqual([c[1]["offset"] for c in client.calls], [0, 0])

    def test_counts_carry_no_message_text(self):
        secret = "gizli-metin-7781"
        box, _, _, _ = inbox(batch(text(1, secret), text(2, secret, chat=1), text(3, secret + "\u202e"),
                                   text(4, secret * 500)))
        box.take(10)
        self.assertNotIn("gizli", repr(box.counts))
        self.assertTrue(all(type(k) is str and k.isascii() and type(v) is int for k, v in box.counts.items()))


class OffsetTest(unittest.TestCase):
    def test_ack_stores_handle_plus_one_and_the_next_take_uses_it(self):
        box, client, store, _ = inbox(batch(text(7)))
        item = box.take(4)[0]
        self.assertTrue(box.ack(item["handle"]))
        self.assertEqual(store.offset, 8)
        box.take(4)
        self.assertEqual(client.calls[1][1]["offset"], 8)

    def test_voice_between_texts_passes_only_after_the_first_text(self):
        for order, offsets in (((3, 1), [None, 4]), ((1, 3), [3, 4])):
            with self.subTest(order=order):
                box, _, store, _ = inbox(batch(text(1, "bir"), voice(2), text(3, "uc")))
                self.assertEqual([i["handle"] for i in box.take(4)], [1, 3])
                self.assertEqual(store.saves, [])
                seen = []
                for handle in order:
                    box.ack(handle)
                    seen.append(store.offset if store.saves else None)
                self.assertEqual(seen, offsets)

    def test_a_lone_voice_is_passed_at_take(self):
        box, _, store, _ = inbox(batch(voice(9)))
        self.assertEqual(box.take(4), [])
        self.assertEqual(store.offset, 10)

    def test_ack_takes_the_core_id_and_refuses_strangers(self):
        box, _, store, _ = inbox(batch(text(11)))
        box.take(4)
        for bad in (99, True, 11.0, "tg.x", "tg.١١", "11", None, object()):
            self.assertIs(box.ack(bad), False)
        self.assertEqual(box.counts["bad_ack"], 8)
        self.assertTrue(box.ack("tg.11"))
        self.assertEqual(store.offset, 12)
        self.assertIs(box.ack("tg.11"), False)  # acked once only


class PcRaw:
    def __init__(self, *items, fail=False):
        self.items, self.fail, self.acked = list(items), fail, []

    def take(self, limit):
        if self.fail == "raise":
            raise OSError("pc")
        return tuple(self.items) if self.fail else list(self.items[:limit])

    def ack(self, value):
        self.acked.append(value)


def pc_item(rid):
    return {"id": rid, "kind": "text", "text": "pc " + rid}


class MergedTest(unittest.TestCase):
    def test_per_source_share_and_rotation(self):
        pc = PcRaw(*(pc_item("p%d" % n) for n in range(5)))
        box, _, _, _ = inbox(repeat=batch(*(text(n) for n in range(1, 6))))
        merged = tti.MergedInbox(pc, box)
        self.assertEqual(len(merged.take(4)), 4)
        firsts = [merged.take(1)[0]["id"][:1] for _ in range(4)]
        self.assertEqual(sorted(firsts), ["p", "p", "t", "t"])
        self.assertEqual(merged.take(0), [])

    def test_ack_reaches_the_right_source(self):
        pc = PcRaw(pc_item("p1"))
        box, _, store, _ = inbox(batch(text(5)))
        merged = tti.MergedInbox(pc, box)
        for raw in merged.take(4):
            merged.ack(tb._ack_value(raw))
        self.assertEqual(pc.acked, ["p1"])
        self.assertEqual(store.offset, 6)
        self.assertIs(merged.ack("p1"), False)

    def test_a_failing_source_does_not_block_the_other(self):
        for fail in ("raise", "tuple"):
            with self.subTest(fail=fail):
                box, _, _, _ = inbox(batch(text(5)))
                merged = tti.MergedInbox(PcRaw(pc_item("p1"), fail=fail), box)
                self.assertEqual([r["id"] for r in merged.take(4)], ["tg.5"])
                self.assertEqual(merged.counts, {"pc_failed": 1})
        broken = SimpleNamespace(take=lambda limit: 1 / 0, ack=lambda v: 1 / 0)
        merged = tti.MergedInbox(PcRaw(pc_item("p1")), broken)
        self.assertEqual([r["id"] for r in merged.take(4)], ["p1"])

    def test_a_pc_item_cannot_take_a_telegram_id(self):
        pc = PcRaw(pc_item("tg.5"))
        merged = tti.MergedInbox(pc, inbox()[0])
        raw = merged.take(4)[0]
        self.assertEqual(raw["id"], "!tg.5")
        self.assertIsNone(tb.ID_RE.fullmatch(raw["id"]))
        merged.ack(raw["id"])
        self.assertEqual(pc.acked, ["tg.5"])


class DisabledTest(unittest.TestCase):
    def test_off_makes_no_call(self):
        for flag in ({}, {"enabled": False}, {"enabled": 1}, {"enabled": "yes"}):
            with self.subTest(flag=flag):
                client, store, clock = Client(batch(text(1))), Store(), Clock()
                box = tti.TgInbox(client, store, CHAT, clock, **flag)
                self.assertEqual(box.take(4), [])
                self.assertIs(box.ack(1), False)
                self.assertEqual((client.calls, store.loads, store.saves, clock.calls), ([], 0, [], 0))

    def test_a_bad_allowed_chat_stays_off(self):
        for chat in ("4242", 0, -4242, True, None, 4242.0):
            with self.subTest(chat=chat):
                client, store = Client(batch(text(1))), Store()
                box = tti.TgInbox(client, store, chat, Clock(), enabled=True)
                self.assertEqual(box.take(4), [])
                self.assertEqual((client.calls, store.saves), ([], []))

    def test_a_float_clock_is_accepted_and_a_bad_one_counted(self):
        for now, want in ((1700000100.5, 1), (float("nan"), 0), (float("inf"), 0), ("1", 0)):
            with self.subTest(now=now):
                box, _, _, clock = inbox(batch(text(1)))
                clock.now = lambda: now
                self.assertEqual(len(box.take(4)), want)
                if not want:
                    self.assertEqual(box.counts, {"clock_bad": 1})


class UpdateMatrixTest(unittest.TestCase):
    def test_only_a_clean_private_text_becomes_an_item(self):
        cases = {
            "group": text(1, ctype="group"),
            "forwarded": text(2, forward_origin={"type": "user", "date": 1}),
            "bot": text(3, **{"from": {"id": CHAT, "is_bot": True}}),
            "sender": text(4, **{"from": {"id": 9, "is_bot": False}}),
            "edited": {"update_id": 5, "edited_message": text(5)["message"]},
            "too_long": text(6, "a" * 5000),
            "nul": text(7, "a\x00b"),
            "bidi": text(8, "a\u202eb"),
            "invalid_update": text(9, ""),
            "empty": text(10, " \t "),
        }
        box, _, store, _ = inbox(batch(*cases.values()))
        self.assertEqual(box.take(20), [])
        self.assertEqual(box.counts, dict.fromkeys(cases, 1))
        self.assertEqual(store.offset, 11)

    def test_bad_update_ids_give_no_item(self):
        def check(entry):
            box, _, store, _ = inbox(batch(entry))
            self.assertEqual(box.take(4), [])
            self.assertEqual(box.counts, {"bad_update_id": 1})
            self.assertEqual(store.saves, [])

        def with_id(uid):
            update = text(1)
            update["update_id"] = uid
            return update
        missing = text(1)
        del missing["update_id"]
        entries = [with_id(uid) for uid in (-1, 2**63, True, *HOSTILE_NUMBERS)]
        each(self, entries + [missing, "x", 3, None, [1]], check)

    def test_hostile_text_never_raises_and_passes_only_clean(self):
        def check(value):
            box, _, _, _ = inbox(batch(text(1, value)))
            items = box.take(4)
            self.assertIsInstance(items, list)
            dirty = "\x00" in value or any(ch in tti._BIDI for ch in value) or len(value) > tti.MAX_TEXT
            try:
                value.encode("utf-8")  # parse_update refuses unpaired surrogates
            except UnicodeEncodeError:
                dirty = True
            self.assertEqual(len(items), 0 if dirty or not value.strip() else 1)
            for item in items:
                self.assertEqual((item["kind"], item["text"]), ("text", value))
        each(self, HOSTILE_TEXT, check)


class ClientFailureTest(unittest.TestCase):
    def test_a_bad_answer_gives_nothing_and_keeps_the_offset(self):
        answers = (("client_refused", tg.Refused("x")), ("client_failed", tg.Failed("network")),
                   ("client_bad", tg.Ok("notalist")), ("client_bad", object()),
                   ("client_raised", OSError("net")))
        for kind, answer in answers:
            with self.subTest(kind=kind, answer=type(answer).__name__):
                box, client, store, _ = inbox(answer, batch(text(3)))
                self.assertEqual(box.take(4), [])
                self.assertEqual(box.counts, {kind: 1})
                self.assertEqual(store.saves, [])
                box.take(4)
                self.assertEqual([c[1]["offset"] for c in client.calls], [0, 0])

    def test_an_unreadable_store_makes_no_call(self):
        for store, kind in ((Store(fail="load"), "store_load_failed"), (Store(state="{bad"), "offset_bad")):
            with self.subTest(kind=kind):
                box, client, _, _ = inbox(batch(text(1)), store=store)
                self.assertEqual(box.take(4), [])
                self.assertEqual((box.counts, client.calls), ({kind: 1}, []))


class StoreFailureTest(unittest.TestCase):
    def test_a_failed_save_brings_the_item_again_and_the_core_logs_it_once(self):
        store = Store(fail=True)
        box, client, _, _ = inbox(repeat=batch(text(5)), store=store)
        log, brain, shown = FakeLog(), FakeBrain(), []
        ports = tb.wrap(tti.MergedInbox(PcRaw(), box), log, brain, SimpleNamespace(show=shown.append),
                        FakeClock())
        core = tc.start(ports, tb.config(4, 40))
        core.step()
        core.step()
        self.assertTrue(client.calls and all(c[1]["offset"] == 0 for c in client.calls))
        self.assertGreaterEqual(box.counts["store_failed"], 2)
        self.assertEqual([x["key"] for x in log.lines if x["key"].startswith("in:")], ["in:pc:tg.5"])
        self.assertEqual(brain.asked, ["selam"])
        store.fail = False
        core.step()
        self.assertEqual(store.offset, 6)
        self.assertEqual(brain.asked, ["selam"])


class StaticTest(unittest.TestCase):
    SOURCE = (ROOT / "jarvis" / "talk_tg_in.py").read_text(encoding="utf-8")

    def test_no_network_no_secret_no_logging(self):
        tree = ast.parse(self.SOURCE)
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                mods.update(node.module if node.module != "jarvis" else "jarvis." + a.name
                            for a in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"print", "open", "eval", "exec", "__import__"})
        self.assertLessEqual(mods, {"__future__", "json", "sys", "jarvis.telegram_offset",
                                    "jarvis.telegram_wire", "jarvis.talk_boundary"})
        low = self.SOURCE.lower()
        for word in ("token", "environ", "getenv", "urllib", "socket", "ssl", "requests",
                     "import logging", "project_folders", "def " + "heartbeat"):
            self.assertNotIn(word, low)
