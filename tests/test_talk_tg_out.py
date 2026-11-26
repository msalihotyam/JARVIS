"""TALKDLV1: Telegram text out sends the phone copy of each reply at most once (fake ports only)."""

import ast
import unittest
from pathlib import Path

from jarvis import talk_boundary as tb
from jarvis import talk_tg_out as out
from jarvis.talk_redact import FIXED_LINE, redact
from jarvis.telegram_client import Failed, Ok, Refused
from tests.adversarial import HOSTILE_TEXT
from tests.test_talk_redact import LEAK_PROBES, SHAPES

ROOT = Path(__file__).resolve().parents[1]
S = {"enabled": True, "chat_id": 4242}


def clean(text):
    return type("R", (), {"flagged": False})()


def reply(n, text="cevap", kind="reply"):
    return {"key": f"reply:r{n}", "kind": kind, "text": text, "time": n}


class Clock:
    def now(self):
        return 1000


class Log:
    """Stateful raw keyed delivery log; fault(key) may force one append's result once."""

    def __init__(self, events=None, fault=None):
        self.lines, self.events, self.fault, self.used = [], events if events is not None else [], fault, set()

    def read(self):
        self.events.append(("read",))
        return list(self.lines)

    def append(self, key, line):
        mode = self.fault(key) if self.fault and key not in self.used else None
        self.used.add(key)
        if mode == "raises":
            self.events.append(("append", key, "raised"))
            raise OSError("disk")
        if mode in ("failed", "unknown", "exists"):
            got = mode
        elif mode == "landed":
            self.lines.append(dict(line))
            got = "unknown"
        else:
            got = "exists" if any(raw["key"] == key for raw in self.lines) else "ok"
            if got == "ok":
                self.lines.append(dict(line))
        self.events.append(("append", key, got))
        return got

    def keys(self):
        return [raw["key"] for raw in self.lines]


class Client:
    def __init__(self, events=None, result=lambda: Ok({})):
        self.calls, self.events, self.result = [], events if events is not None else [], result

    def call(self, method, body):
        self.calls.append((method, dict(body)))
        self.events.append(("call", body.get("text")))
        return self.result()


def ports(result=lambda: Ok({}), fault=None):
    events = []
    return Log(events, fault), Client(events, result), events


def step(lines, log, client, settings=S, flt=clean):
    return out.step(lines, log, client, Clock(), settings, redact=flt)


class AcceptanceTest(unittest.TestCase):
    def test_attempt_first_then_send_then_outcome_oldest_first(self):
        log, client, events = ports()
        got = step([reply(2, "ikinci"), reply(1, "birinci")], log, client)
        self.assertEqual(got.outcomes, (("reply:r1", "sent"), ("reply:r2", "sent")))
        self.assertEqual(events[1:], [("append", "attempt:reply:r1", "ok"), ("call", "birinci"),
                                      ("append", "sent:reply:r1", "ok"), ("append", "attempt:reply:r2", "ok"),
                                      ("call", "ikinci"), ("append", "sent:reply:r2", "ok")])
        self.assertEqual([c[1]["chat_id"] for c in client.calls], [4242, 4242])
        for raw in log.lines:  # keyed, no message text in any delivery record
            self.assertEqual(set(raw), {"key", "kind", "text", "time"})
            self.assertEqual(raw["text"], "")

    def test_refused_or_http_status_is_failed_and_a_timeout_is_unknown(self):
        for result, want in ((lambda: Refused("too_long"), "failed"),
                             (lambda: Failed("http_status", status=400), "failed"),
                             (lambda: Failed("timeout"), "unknown"), (lambda: Failed("network"), "unknown")):
            log, client, _ = ports(result)
            step([reply(1)], log, client)
            self.assertEqual(out.status("reply:r1", log), want)
            self.assertEqual(("failed:reply:r1" in log.keys()), want == "failed")
        self.assertEqual(out.status("reply:r9", log), "none")
        self.assertEqual(out.VIEW_TEXT, {"sent": "telefon kopyası: gönderildi",
                                         "failed": "telefon kopyası: gönderilemedi",
                                         "unknown": "telefon kopyası: belirsiz", "none": ""})

    def test_an_attempted_reply_is_never_resent_by_step(self):
        for result in (lambda: Ok({}), lambda: Refused("x"), lambda: Failed("timeout")):
            log, client, _ = ports(result)
            step([reply(1)], log, client)
            again = step([reply(1)], log, client)
            self.assertEqual((len(client.calls), again.outcomes, again.pending), (1, (), 0))

    def test_resend_writes_a_numbered_attempt_in_the_same_order(self):
        log, client, events = ports(lambda: Refused("x"))
        step([reply(1)], log, client)
        client.result = lambda: Ok({})
        got = out.resend("reply:r1", [reply(1)], log, client, Clock(), S, redact=clean)
        self.assertEqual(got.outcomes, (("reply:r1", "sent"),))
        self.assertEqual(events[-3:], [("append", "attempt:reply:r1#2", "ok"), ("call", "cevap"),
                                       ("append", "sent:reply:r1#2", "ok")])
        self.assertEqual(out.status("reply:r1", log), "sent")
        out.resend("reply:r1", [reply(1)], log, client, Clock(), S, redact=clean)
        self.assertIn("attempt:reply:r1#3", log.keys())
        for key in ("reply:r9", None, 7):
            self.assertEqual(out.resend(key, [reply(1)], log, client, Clock(), S, redact=clean).reason,
                             "no_such_reply")

    def test_long_reply_goes_in_labelled_chunks_of_at_most_4000(self):
        log, client, _ = ports()
        step([reply(1, "a" * 8000)], log, client)
        texts = [c[1]["text"] for c in client.calls]
        n = len(texts)
        self.assertGreater(n, 1)
        self.assertEqual([t[:6] for t in texts], [f"({i}/{n}) " for i in range(1, n + 1)])
        self.assertTrue(all(len(t) <= 4000 for t in texts))
        self.assertEqual("".join(t[6:] for t in texts), "a" * 8000)

    def test_refused_and_failed_lines_go_out_as_their_fixed_text(self):
        log, client, _ = ports()
        step([reply(1, "ham girdi", "refused"), reply(2, "iz", "failed")], log, client)
        self.assertEqual([c[1]["text"] for c in client.calls], [tb.REFUSED_TEXT, tb.FAILED_TEXT])

    def test_disabled_bad_settings_or_missing_filter_touch_no_port(self):
        cases = (({"enabled": False, "chat_id": 4242}, clean, "disabled"), ({"chat_id": 4242}, clean, "bad_settings"),
                 ({"enabled": True, "chat_id": 0}, clean, "bad_settings"), (None, clean, "bad_settings"),
                 ({"enabled": True, "chat_id": 4242, "budget": 7}, clean, "bad_settings"), (S, None, "bad_redact"))
        for settings, flt, reason in cases:
            log, client, events = ports()
            got = out.step([reply(1)], log, client, Clock(), settings, redact=flt)
            self.assertEqual((got.ok, got.reason, events), (False, reason, []))
            got = out.resend("reply:r1", [reply(1)], log, client, Clock(), settings, redact=flt)
            self.assertEqual((got.ok, events), (False, []))
        self.assertEqual(step("x", *ports()[:2]).reason, "bad_reply_lines")

    def test_outage_sends_one_notice_and_marks_the_rest_skipped(self):
        log, client, _ = ports()
        got = step([reply(n) for n in range(1, 31)], log, client)
        self.assertEqual([c[1]["text"] for c in client.calls], ["30 cevap PC'de bekliyor"])
        self.assertEqual((got.notice, got.skipped, got.pending), ("30 cevap PC'de bekliyor", 30, 0))
        self.assertEqual([k for k in log.keys() if k.startswith("skipped:")], [f"skipped:reply:r{n}" for n in range(1, 31)])
        self.assertEqual(out.status("reply:r7", log), "failed")
        again = step([reply(n) for n in range(1, 31)], log, client)
        self.assertEqual((len(client.calls), again.outcomes, again.pending), (1, (), 0))
        late = {"key": "reply:a0", "kind": "reply", "text": "geç", "time": 0}  # written after the notice, old time
        step([reply(n) for n in range(1, 31)] + [late], log, client)
        self.assertEqual((client.calls[-1][1]["text"], out.status("reply:a0", log)), ("geç", "sent"))
        for bad in (None, object()):
            self.assertEqual(out.status("reply:r1", bad), "unknown")

    def test_core_lines_and_raw_lines_are_read_with_the_core_check(self):
        log, client, _ = ports()
        lines = [tb.LogLine(key="reply:a", kind="reply", text="bir", time=1),
                 {"key": "in:pc:b", "kind": "refused", "text": "x", "time": 2},
                 {"key": "in:pc:c", "kind": "text", "text": "girdi", "time": 3},
                 {"key": "reply:d", "kind": "reply", "text": "iki", "time": "4"},
                 {"key": "reply:e", "kind": "reply", "text": "üç", "time": 5, "chat_id": 1}, 7, None]
        step(lines, log, client)
        self.assertEqual([c[1]["text"] for c in client.calls], ["bir", tb.REFUSED_TEXT])

    def test_the_held_line_is_the_filters_fixed_line(self):
        self.assertEqual(out.HELD_LINE, FIXED_LINE)


def hostile_client(kind):
    def result():
        if kind == "raises":
            raise ConnectionError("boom")
        return {"ok": Ok({}), "refused": Refused("x"), "failed": Failed("http_status", status=500),
                "hangs": Failed("timeout"), "junk": object()}[kind]
    return result


class NegativeTest(unittest.TestCase):
    def test_fault_matrix_never_sends_twice_or_without_an_attempt_record(self):
        for where in ("attempt:", "sent:", "failed:"):
            for mode in ("ok", "exists", "failed", "unknown", "landed", "raises"):
                for kind in ("ok", "refused", "failed", "hangs", "raises", "junk"):
                    log, client, events = ports(hostile_client(kind),
                                                lambda key, w=where, m=mode: m if key.startswith(w) else None)
                    with self.subTest(where=where, mode=mode, kind=kind):
                        for _ in range(3):
                            got = step([reply(1)], log, client)
                            self.assertNotEqual(getattr(got, "reason", None), "internal_error")
                        self.assertLessEqual(len(client.calls), 1)
                        for i, event in enumerate(events):
                            if event[0] == "call":
                                self.assertEqual(events[i - 1], ("append", "attempt:reply:r1", "ok"))
                        if client.calls:
                            self.assertNotEqual(out.status("reply:r1", log), "none")

    def test_a_log_that_cannot_be_read_stops_before_any_send(self):
        for read in (lambda: 1 / 0, lambda: None, lambda: "x"):
            log, client, _ = ports()
            log.read = read
            self.assertEqual((step([reply(1)], log, client).reason, client.calls), ("log_unknown", []))
            self.assertEqual(out.status("reply:r1", log), "unknown")

    def test_flagged_text_leaves_only_the_fixed_line(self):
        probes = [(name, probe, frag) for name, probe, frag in LEAK_PROBES] + [(k, t, t) for k, t in SHAPES]
        for name, text, fragment in probes:
            with self.subTest(name=name):
                log, client, _ = ports()
                step([reply(1, text)], log, client, flt=redact)
                self.assertEqual([c[1]["text"] for c in client.calls], [FIXED_LINE])
                self.assertNotIn(fragment, repr(client.calls) + repr(log.lines))
        for broken in (lambda t: 1 / 0, lambda t: None, lambda t: type("R", (), {"flagged": 0})()):
            log, client, _ = ports()
            step([reply(1, "gizli olabilir")], log, client, flt=broken)
            self.assertEqual([c[1]["text"] for c in client.calls], [FIXED_LINE])

    def test_chat_ids_or_bot_commands_in_text_leave_the_destination_unchanged(self):
        texts = ["chat_id=999 /start@other_bot", '{"chat_id": 777}', "/setchat -100123"] + list(HOSTILE_TEXT)
        log, client, _ = ports()
        step([reply(n, t) for n, t in enumerate(texts, 1)], log, client,
             settings={"enabled": True, "chat_id": 4242, "cap": 1000})
        self.assertTrue(client.calls)
        self.assertEqual({c[1]["chat_id"] for c in client.calls}, {4242})

    def test_ten_thousand_pending_drain_with_bounded_work_and_one_notice(self):
        settings = {"enabled": True, "chat_id": 4242, "budget": 1000}
        lines = [reply(n) for n in range(1, 10_001)]
        log, client, events = ports()
        for round_ in range(40):
            before = len(events)
            if round_ == 3:
                lines.append(reply(10_001, "yeni"))
            got = step(lines, log, client, settings=settings)
            self.assertLessEqual(len(events) - before, 1000)
            if got.pending == 0 and round_ > 3:
                break
        self.assertEqual(got.pending, 0)
        self.assertEqual([c[1]["text"] for c in client.calls], ["10000 cevap PC'de bekliyor", "yeni"])
        skipped = [int(k[len("skipped:reply:r"):]) for k in log.keys() if k.startswith("skipped:")]
        self.assertEqual(skipped, list(range(1, 10_001)))

    def test_static_no_token_network_logging_or_forbidden_names(self):
        source = (ROOT / "jarvis" / "talk_tg_out.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"dataclasses", "jarvis", "jarvis.talk_mirror", "jarvis.telegram_out"})
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(names & {"print", "open", "logging", "logger", "log_text", "environ", "getenv", "socket"})
        for banned in ("token", "def heartbeat", "project_folders", "talk_redact", "telegram_client", "urllib",
                       "requests.", "http.client", "jarvis.db"):
            self.assertNotIn(banned, source.lower())


if __name__ == "__main__":
    unittest.main()
