import itertools
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jarvis import n6_notify
from jarvis.n6_notify import AskRefused, Event, Route, StateError, evening_summary, flush, submit
from jarvis.notify import NotificationCore

DAY1 = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
DAY1_LATE = datetime(2026, 11, 8, 20, 59, tzinfo=timezone.utc)  # 23:59 Istanbul
DAY2 = datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)  # 00:00 Istanbul, next day


class Ledger:
    """Fake work-event ledger: writes persist, so NotificationCore dedupe is real."""

    def __init__(self):
        self.events = []

    def read(self, run_id):
        return [e for e in self.events if e.run_id == run_id]

    def write(self, event, *, idempotency_key, severity):
        self.events.append(event)
        return True


def ask(key, rec="evet, devam et"):
    return Event("ask", key, f"soru {key}", rec)


class N6NotifyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "notify.json"
        patcher = mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger = Ledger()
        self.core = NotificationCore(self.ledger.write, self.ledger.read)
        self.seq = itertools.count(1)
        self.sent = []

    def flush(self, at=DAY1, send=None):
        return flush(self.path, core=self.core, run_id="run-1", next_seq=lambda: next(self.seq),
                     at=at, send=send or self.sent.append)

    def test_neg_orchestrator_item_never_reaches_phone(self):
        for kind in ("attention", "lane_blocked", "rule_miss", "something_new", ""):
            self.assertIs(submit(self.path, Event(kind, "k", "lane N6-9 blocked")), Route.ORCHESTRATOR)
        self.assertIsNone(self.flush())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.ledger.events, [])

    def test_neg_fifth_ask_in_a_day_is_held_for_next_batch(self):
        for i in range(5):
            submit(self.path, ask(f"q{i}"))
        first = self.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([f"soru q{i}" in first for i in range(5)], [True] * 4 + [False])
        submit(self.path, ask("q5"))
        self.assertIsNone(self.flush(at=DAY1_LATE))
        self.assertEqual(len(self.sent), 1)
        second = self.flush(at=DAY2)
        self.assertIn("soru q4", second)
        self.assertIn("soru q5", second)
        self.assertEqual(len(self.sent), 2)

    def test_neg_ask_without_recommendation_refused(self):
        for rec in ("", "   ", "\n\t", "https://example.com/do-it"):
            with self.assertRaises(AskRefused):
                submit(self.path, Event("ask", "q", "soru", rec))
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.flush())
        self.assertEqual(self.sent, [])

    def test_neg_duplicate_event_pages_once(self):
        urgent = Event("urgent", "budget", "kota bitti")
        submit(self.path, urgent)
        submit(self.path, urgent)
        self.assertEqual(self.flush(), "kota bitti")
        submit(self.path, urgent)
        self.assertIsNone(self.flush())
        self.assertEqual(self.sent, ["kota bitti"])
        self.assertEqual(len(self.ledger.events), 1)

    def test_job_finished_goes_as_one_message(self):
        for i in range(6):
            submit(self.path, Event("slice_started", f"s{i}", f"dilim {i} basladi"))
        submit(self.path, Event("job_finished", "job-1", "is bitti: 6 dilim"))
        submit(self.path, Event("urgent", "u1", "acil: disk dolu"))
        text = self.flush()
        self.assertEqual(self.sent, [text])
        self.assertIn("is bitti: 6 dilim", text)
        self.assertIn("acil: disk dolu", text)
        self.assertNotIn("dilim 0", text)
        summaries = []
        summary = evening_summary(self.path, summaries.append)
        self.assertEqual(summaries, [summary])
        self.assertEqual(summary.count("basladi"), 6)
        self.assertIsNone(evening_summary(self.path, summaries.append))
        self.assertEqual(len(self.sent), 1)

    def test_neg_tests_never_open_the_database(self):
        src = Path(n6_notify.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", src)
        self.assertNotIn("jarvis.db", src)
        submit(self.path, Event("urgent", "u", "x"))
        self.flush()  # sqlite3.connect is patched to raise in setUp

    def test_neg_malformed_state_fails_closed(self):
        good = {"version": 1, "pending": [], "daily": [], "ask_day": "", "asks": 0, "outbox": "",
                "outbox_asks": 0}
        sent_day = {**good, "ask_day": "2026-11-08", "asks": 1, "outbox": "x"}
        bad_states = [
            json.dumps({**good, "ask_day": "garbage"}), json.dumps({**good, "asks": 3}),
            json.dumps({**good, "outbox": "bak https://evil.example/x"}),
            json.dumps({**good, "outbox": "a\n\nb"}), json.dumps({**good, "daily": ["a\x1bb"]}),
            json.dumps({**good, "outbox_asks": 1}), json.dumps({**sent_day, "outbox_asks": 2}),
            "", "{", "[]", json.dumps({**good, "asks": True}), json.dumps({**good, "asks": -1}),
            json.dumps({**good, "asks": "0"}), json.dumps({**good, "version": 2}),
            json.dumps({**good, "extra": 1}), json.dumps({**good, "daily": [1]}),
            json.dumps({**good, "pending": [{"kind": "attention", "key": "k", "text": "t",
                                             "recommendation": ""}]}),
            json.dumps({**good, "pending": [{"kind": "ask", "key": "k", "text": "t",
                                             "recommendation": " "}]}),
        ]
        for raw in bad_states:
            self.path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw):
                with self.assertRaises(StateError):
                    self.flush()
                with self.assertRaises(StateError):
                    submit(self.path, Event("urgent", "u", "x"))
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.ledger.events, [])

    def test_neg_links_and_control_characters_never_reach_the_page(self):
        submit(self.path, Event("urgent", "u", "bak https://evil.example/x\x1b[31m ve www.a.io‮"
                                                " ile evil.com/y hxxp\x00://z.net пример.рф/x"
                                                " x_evil.com/p 192.168.1.1/admin evil。com"
                                                " ｅｖｉｌ．ｃｏｍ"))
        text = self.flush()
        for bad in ("http", "://", "www", "evil", ".io", ".net", "\x1b", "‮", "\x00", "рф",
                    "192", "admin", "com"):
            self.assertNotIn(bad, text)
        self.assertIn("bak", text)

    def test_neg_overflow_waits_for_next_page_without_loss(self):
        for i in range(10):
            submit(self.path, Event("urgent", f"u{i}", f"{i}" + "x" * 300))
        while self.flush():
            pass
        self.assertTrue(all(len(t) <= n6_notify.MAX_CHARS for t in self.sent))
        lines = [line for t in self.sent for line in t.split("\n")]
        self.assertEqual(sorted(line[0] for line in lines), [str(i) for i in range(10)])
        self.assertEqual(len(self.ledger.events), 10)

    def test_neg_unsent_asks_count_after_day_rollover(self):
        def boom(_text):
            raise OSError("telegram down")

        for i in range(4):
            submit(self.path, ask(f"q{i}"))
        with self.assertRaises(OSError):
            self.flush(at=DAY1_LATE, send=boom)
        submit(self.path, ask("q4"))
        text = self.flush(at=DAY2)
        self.assertIn("soru q3", text)
        self.assertNotIn("soru q4", text)

    def test_neg_clock_going_back_does_not_reset_the_cap(self):
        for i in range(5):
            submit(self.path, ask(f"q{i}"))
        self.assertNotIn("soru q4", self.flush(at=DAY2))
        self.assertIsNone(self.flush(at=DAY1))
        self.assertEqual(len(self.sent), 1)

    def test_neg_daily_replay_and_non_str_fields(self):
        submit(self.path, Event("status", "s", "durum iyi"))
        submit(self.path, Event("status", "s", "durum iyi"))
        self.assertEqual(evening_summary(self.path, lambda _t: None), "durum iyi")
        for bad in (Event("urgent", "k", 5), Event("ask", "k", "t", None), Event(1, "k", "t")):
            with self.assertRaises(ValueError):
                submit(self.path, bad)
        self.assertIsNone(self.flush())

    def test_neg_send_error_is_resent_once_without_duplicate(self):
        def boom(_text):
            raise OSError("telegram down")

        submit(self.path, Event("job_finished", "j", "is bitti"))
        with self.assertRaises(OSError):
            self.flush(send=boom)
        self.assertEqual(self.flush(), "is bitti")
        self.assertIsNone(self.flush())
        self.assertEqual(self.sent, ["is bitti"])
        self.assertEqual(len(self.ledger.events), 1)

    def test_neg_ledger_error_keeps_the_item_pending(self):
        submit(self.path, Event("urgent", "u", "acil"))
        with mock.patch.object(self.core, "notify", side_effect=RuntimeError("ledger")):
            with self.assertRaises(RuntimeError):
                self.flush()
        self.assertEqual(self.flush(), "acil")

    def test_neg_naive_time_refused(self):
        with self.assertRaises(ValueError):
            self.flush(at=datetime(2026, 11, 8, 10, 0))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
