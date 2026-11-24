"""DORAEGRESS2 (A13, architecture v2.4 s12.3): Dora data never leaves the machine except to
models; unclassifiable records are not sent. Negative tests for every outward path that has a
gate today, on the shared screen jarvis/dora_screen.py (DORAGATE1). Every refusal asserts its
reason; every lesson_extract layer has a marker only that layer catches. Ungated paths are GAPS
in build/reports/DORAEGRESS2.md, not tests here (a test there would pin the leak as expected).
Synthetic fixtures only: no real deny words, no Dora file, no network, no wall clock."""
import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import conveyor_api as A  # noqa: E402
import lesson_extract as L  # noqa: E402
from jarvis import dora_screen, n6_notify  # noqa: E402
from jarvis.n6_notify import Event, evening_summary, flush, submit  # noqa: E402
from jarvis.notify import NotificationCore  # noqa: E402
from tests.test_lesson_extract import OUTPUTS, FakeApi  # noqa: E402
from tests.test_n6_notify import DAY1, Ledger  # noqa: E402

MARK = "zqxvkmark"  # synthetic Dora term, far from any real word after leet folding
CLEAN = "CLEANMARK"
SECRET = "sk-" + "ant-" + "A" * 40  # synthetic key shape, split so scanners skip the literal


def write_deny(path, words):
    path.write_text(json.dumps({"version": 1, "about": "synthetic", "shapes": [], "words": words}),
                    encoding="utf-8")
    return path


class TrackingApi(FakeApi):
    opened = False

    def open_envelope(self, ceiling, purpose):
        self.opened = True
        return super().open_envelope(ceiling, purpose)


class LessonGateTests(unittest.TestCase):
    """Model path: scripts/lesson_extract.py is the only Dora classifier before conveyor_api."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "orch"
        (self.root / "kayit" / "sohbet").mkdir(parents=True)
        (self.root / "ORCH_RULES.md").write_text(f"O1 kayit {MARK} notu 12 adet\n", encoding="utf-8")
        (self.root / "kayit" / "KARARLAR.md").write_text(f"Karar: kuyruk sirayla islendi {CLEAN}\n",
                                                         encoding="utf-8")

    def deny_file(self, words, name="deny.json"):
        return write_deny(self.tmp / name, words)

    def run_gate(self, words):
        api = TrackingApi(self.tmp)
        rep = L.run(self.root, self.tmp / "out", 1.0, api, deny_path=self.deny_file(words))
        return api, rep, "\n".join(api.payloads)

    def assert_outputs_clean(self, *needles):
        for n in OUTPUTS:
            data = (self.tmp / "out" / n).read_bytes()
            for needle in needles:
                self.assertNotIn(needle.encode(), data, n)

    def test_control_without_gate_the_dora_record_would_leave(self):
        # red-first evidence: same fixture, a deny list without MARK -> the marker reaches api.call
        _, rep, sent = self.run_gate(["unrelatedword"])
        self.assertIn(MARK, sent)
        self.assertEqual(rep["dropped_dora"], 0)

    def test_dora_record_never_sent_classified_record_is(self):
        api, rep, sent = self.run_gate([MARK])
        self.assertNotIn(MARK, sent)
        self.assertGreaterEqual(rep["dropped_dora"], 1)
        self.assertIn(CLEAN, sent)  # the model path still accepts classified non-Dora content
        self.assert_outputs_clean(MARK)

    def test_obfuscated_dora_term_still_refused(self):
        deny = L.load_deny(self.deny_file([MARK]))
        for text in (MARK.upper(), "zq​xvkmark", "zqxvkm4rk"):
            self.assertTrue(L.denied(f"not {text} ok", deny), text)
            self.assertEqual(dora_screen.screen(f"not {text} ok", deny), (False, "deny"), text)
        self.assertFalse(L.denied(f"plain {CLEAN}", deny))
        self.assertEqual(dora_screen.screen(f"plain {CLEAN}", deny), (True, ""))

    def test_unclassifiable_record_is_refused_by_the_screen(self):
        deny = L.load_deny(self.deny_file([MARK]))
        for value in (None, b"bytes", 123, ["list"], object()):
            self.assertTrue(L.denied(value, deny), repr(value))
            self.assertEqual(dora_screen.screen(value, deny), (False, "doubt:type"), repr(value))

    def test_unreadable_deny_list_refuses_before_any_call(self):
        for path in (self.deny_file(["a-b"], "bad.json"), self.deny_file([], "empty.json"),
                     self.tmp / "missing.json"):
            api = TrackingApi(self.tmp)
            with self.assertRaises((ValueError, OSError)):
                L.run(self.root, self.tmp / "out", 1.0, api, deny_path=path)
            self.assertEqual(api.payloads, [])
            self.assertFalse(api.opened)


class LessonChunkLayerTests(unittest.TestCase):
    """CDX F2/F3: run()'s per-chunk screen (text, source, pointer, purpose) is load-bearing.
    Each marker here passes the whole-file hit_lines layer; only the per-chunk screen stops it.
    Each control opens that one layer (L.screen) and shows the marker would then leave."""

    setUp = LessonGateTests.setUp
    deny_file = LessonGateTests.deny_file
    assert_outputs_clean = LessonGateTests.assert_outputs_clean

    def run_layer(self, open_chunk_screen=False):
        api = TrackingApi(self.tmp)
        path = self.deny_file([MARK])
        if open_chunk_screen:
            with mock.patch.object(L, "screen", lambda text, deny=None: (True, "")):
                rep = L.run(self.root, self.tmp / "out", 1.0, api, deny_path=path)
        else:
            rep = L.run(self.root, self.tmp / "out", 1.0, api, deny_path=path)
        return api, rep, "\n".join(api.payloads)

    def test_secret_in_chunk_text_only_the_chunk_screen_stops(self):
        f = self.root / "kayit" / "ACIK_KONULAR.md"
        f.write_text(f"Konu: anahtar {SECRET} burada\n", encoding="utf-8")
        deny = L.load_deny(self.deny_file([MARK]))
        self.assertEqual(L.hit_lines(f, deny), [])  # the whole-file layer does not catch it
        self.assertEqual(dora_screen.screen(f.read_text("utf-8"), deny), (False, "secret"))
        _, _, leaked = self.run_layer(open_chunk_screen=True)
        self.assertIn(SECRET, leaked)  # control: without the chunk screen the key would leave
        api, rep, sent = self.run_layer()
        self.assertNotIn(SECRET, sent)
        self.assertNotIn("anahtar", sent)
        self.assertIn(CLEAN, sent)
        self.assertGreaterEqual(rep["dropped_dora"], 2)  # the ORCH_RULES record and this one
        self.assert_outputs_clean(SECRET, MARK)

    def test_dora_term_only_in_source_name_never_sent(self):
        f = self.root / "kayit" / "sohbet" / f"{MARK}_notlar.md"
        f.write_text("Not: plan tamam, sira korunuyor\n", encoding="utf-8")
        deny = L.load_deny(self.deny_file([MARK]))
        self.assertEqual(L.hit_lines(f, deny), [])  # content is clean: whole-file layer passes it
        _, _, leaked = self.run_layer(open_chunk_screen=True)
        self.assertIn(f"kayit/sohbet/{MARK}_notlar.md", leaked)  # control: pointer and purpose
        api, rep, sent = self.run_layer()
        self.assertNotIn(MARK, sent)
        self.assertNotIn("plan tamam", sent)
        self.assertIn(CLEAN, sent)
        self.assertGreaterEqual(rep["dropped_dora"], 2)
        self.assert_outputs_clean(MARK)

    def test_unclassifiable_chunk_never_reaches_api_call(self):
        # F3: a record the screen cannot classify (non-str text) is dropped inside run().
        values = (None, b"raw " + MARK.encode(), 123, [MARK])
        fake = [{"source": "kayit/KARARLAR.md", "rule": None, "spans": [(1, 1)], "start": 1,
                 "end": 1, "text": v} for v in values]
        api = TrackingApi(self.tmp)
        with mock.patch.object(L, "chunks", return_value=(fake, [])):
            rep = L.run(self.root, self.tmp / "out", 1.0, api, deny_path=self.deny_file([MARK]))
        self.assertTrue(api.opened)  # the batch ran; the gate, not a missing envelope, held it
        self.assertEqual(api.payloads, [])
        self.assertEqual((rep["dropped_dora"], rep["sent"]), (len(values), 0))

    def test_screen_error_chunk_never_reaches_api_call(self):
        # F3: doubt inside the chunk screen (doubt:screen) drops the record; hit_lines is unaffected.
        api = TrackingApi(self.tmp)
        with mock.patch.object(dora_screen, "_screen", side_effect=RuntimeError("screen broke")):
            self.assertEqual(dora_screen.screen(CLEAN, L.load_deny(self.deny_file([MARK]))),
                             (False, "doubt:screen"))
            rep = L.run(self.root, self.tmp / "out", 1.0, api, deny_path=self.deny_file([MARK]))
        self.assertTrue(api.opened)
        self.assertEqual(api.payloads, [])
        self.assertEqual(rep["sent"], 0)
        self.assertEqual(rep["dropped_dora"], rep["chunks"])


class LessonFileLayerTests(unittest.TestCase):
    """CDX-DORAEGRESS2 F1: run()'s whole-file _hit(hit_lines) gate is load-bearing. The marker is
    split over a chunk boundary, so every chunk passes screen() alone; only the whole-file layer
    sees it. Each control opens that one layer (hit_lines -> []) and shows the parts would leave."""

    setUp = LessonGateTests.setUp
    deny_file = LessonGateTests.deny_file
    assert_outputs_clean = LessonGateTests.assert_outputs_clean

    def check_split(self, lines):
        f = self.root / "kayit" / "ACIK_KONULAR.md"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        deny = L.load_deny(self.deny_file([MARK]))
        parts = L.chunk_plain(f, "kayit/ACIK_KONULAR.md")
        self.assertEqual(len(parts), 2)
        for c in parts:  # each chunk alone passes the per-chunk screen
            self.assertEqual(dora_screen.screen(c["text"], deny), (True, ""), c["text"])
        self.assertNotEqual(L.hit_lines(f, deny), [])  # the whole file holds the marker
        api = TrackingApi(self.tmp)
        with mock.patch.object(L, "hit_lines", return_value=[]):
            L.run(self.root, self.tmp / "out", 1.0, api, deny_path=self.deny_file([MARK]))
        leaked = "\n".join(api.payloads)
        self.assertIn("QQFRAGA", leaked)  # control: without the whole-file gate both parts leave
        self.assertIn("QQFRAGB", leaked)
        api = TrackingApi(self.tmp)
        rep = L.run(self.root, self.tmp / "out", 1.0, api, deny_path=self.deny_file([MARK]))
        sent = "\n".join(api.payloads)
        for needle in ("QQFRAGA", "QQFRAGB", "zqxvk", MARK):
            self.assertNotIn(needle, sent)
        self.assertIn(CLEAN, sent)
        self.assertGreaterEqual(rep["dropped_dora"], 3)  # ORCH_RULES and both parts
        self.assert_outputs_clean(MARK, "QQFRAGA", "QQFRAGB")

    def test_marker_split_at_heading_boundary_never_sent(self):
        self.check_split(["Konu: QQFRAGA notu zqxvk", "# mark QQFRAGB notu"])

    def test_marker_split_at_line_limit_boundary_never_sent(self):
        filler = [f"satir {i} notu" for i in range(1, L.CHUNK_LINES)]
        self.check_split([*filler, "QQFRAGA notu zqxvk", "mark QQFRAGB notu"])


class BrokerGateTests(unittest.TestCase):
    """Model path: scripts/conveyor_api.call refuses non-model and non-text before anything is sent.
    CDX F1: the reason is asserted, so a later gate (cut switch, platform) cannot stand in."""

    TEXT_ONLY = "arguments must be text only"
    MODEL_ONLY = "model not allowed"

    def setUp(self):
        patcher = mock.patch.object(A, "cut_set", return_value=False)  # machine state independent
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.posts = []

    def refused(self, model, system, messages, reason):
        with self.assertRaises(A.ApiRefused) as cm:
            A.call(model, system, messages, 10, "ders x", root=self.root, clock=lambda: 0.0,
                   reader=lambda: "sk-ant-x", post=lambda *a: self.posts.append(a))
        self.assertIn(reason, str(cm.exception), repr((model, system, messages)))
        self.assertEqual(self.posts, [])
        self.assertFalse((self.root / A.LEDGER_NAME).exists())

    def test_non_text_content_refused_before_send(self):
        model = next(iter(A.PRICES))
        for messages in ([{"role": "user", "content": [{"type": "image", "data": MARK}]}],
                         [{"role": "user", "content": MARK.encode()}], "not a list", []):
            self.refused(model, "", messages, self.TEXT_ONLY)

    def test_non_str_system_refused_before_send(self):  # CDX F4
        model = next(iter(A.PRICES))
        for system in (123, [MARK], None, MARK.encode(), {"text": MARK}):
            self.refused(model, system, [{"role": "user", "content": "ok"}], self.TEXT_ONLY)

    def test_invalid_role_refused_before_send(self):  # CDX F4
        model = next(iter(A.PRICES))
        for msg in ({"role": "system", "content": MARK}, {"role": "tool", "content": MARK},
                    {"role": "", "content": MARK}, {"role": None, "content": MARK},
                    {"role": "user", "content": MARK, "name": "x"}):
            self.refused(model, "", [msg], self.TEXT_ONLY)

    def test_non_model_destination_refused(self):
        for model in ("not-a-model", None, "https://example.invalid/v1"):
            self.refused(model, "", [{"role": "user", "content": MARK}], self.MODEL_ONLY)


class N6FlushGateTests(unittest.TestCase):
    """Phone path: jarvis/n6_notify flush / evening_summary screen with the shared screen
    (DORAGATE1, GAP 5 closed). Synthetic deny list patched into dora_screen.DENY_PATH."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "notify.json"
        self.deny = write_deny(Path(tmp.name) / "deny.json", [MARK])
        for patcher in (mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db opened")),
                        mock.patch.object(n6_notify.dora_screen, "DENY_PATH", self.deny)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = Ledger()
        self.core = NotificationCore(self.ledger.write, self.ledger.read)
        self.seq = itertools.count(1)
        self.sent = []

    def flush(self):
        return flush(self.path, core=self.core, run_id="run-1", next_seq=lambda: next(self.seq),
                     at=DAY1, send=self.sent.append)

    def counts(self):
        return json.loads(self.path.with_name(self.path.name + ".refused").read_text("utf-8"))

    def test_deny_and_secret_lines_never_sent_clean_line_is(self):
        submit(self.path, Event("urgent", "a", f"kota {MARK} bitti"))
        submit(self.path, Event("urgent", "b", f"anahtar {SECRET}"))
        submit(self.path, Event("urgent", "c", "kota bitti"))
        self.assertEqual(self.flush(), "kota bitti")
        self.assertEqual(self.sent, ["kota bitti"])
        self.assertEqual(self.counts(), {"deny": 1, "secret": 1})
        state = self.path.read_text("utf-8")
        self.assertNotIn(MARK, state)
        self.assertNotIn(SECRET, state)

    def test_screen_error_line_never_sent(self):
        submit(self.path, Event("urgent", "a", "kota bitti"))
        with mock.patch.object(dora_screen, "_screen", side_effect=RuntimeError("screen broke")):
            self.assertIsNone(self.flush())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.ledger.events, [])
        self.assertEqual(self.counts(), {"doubt:screen": 1})

    def test_unreadable_deny_list_sends_and_consumes_nothing(self):
        submit(self.path, Event("urgent", "a", "kota bitti"))
        submit(self.path, Event("status", "b", "durum temiz"))
        before = self.path.read_bytes()
        delivered = []
        for words in (["a-b"], []):
            write_deny(self.deny, words)
            with self.assertRaises(ValueError):
                self.flush()
            with self.assertRaises(ValueError):
                evening_summary(self.path, delivered.append)
        self.deny.unlink()
        with self.assertRaises(OSError):
            self.flush()
        with self.assertRaises(OSError):
            evening_summary(self.path, delivered.append)
        self.assertEqual((self.sent, delivered, self.ledger.events), ([], [], []))
        self.assertEqual(self.path.read_bytes(), before)  # nothing consumed or dropped

    def test_evening_summary_refuses_deny_and_secret_items(self):
        submit(self.path, Event("status", "a", f"durum {MARK}"))
        submit(self.path, Event("status", "b", f"durum {SECRET}"))
        submit(self.path, Event("status", "c", "durum temiz"))
        delivered = []
        self.assertEqual(evening_summary(self.path, delivered.append), ["durum temiz"])
        self.assertEqual(delivered, ["durum temiz"])
        self.assertEqual(self.counts(), {"deny": 1, "secret": 1})
        state = self.path.read_text("utf-8")
        self.assertNotIn(MARK, state)
        self.assertNotIn(SECRET, state)

    # CDX-DORAEGRESS2 F2: two lines each clean alone, whose join spells MARK (the skeleton
    # folds the newline). The joined-message screen sends them separately, never joined.
    def check_pending_join(self, first, second):
        deny = dora_screen.load_deny(self.deny)
        self.assertEqual(dora_screen.screen(first, deny), (True, ""))
        self.assertEqual(dora_screen.screen(second, deny), (True, ""))
        self.assertEqual(dora_screen.screen(f"{first}\n{second}", deny), (False, "deny"))
        submit(self.path, Event("urgent", "a", first))
        submit(self.path, Event("urgent", "b", second))
        self.assertEqual(self.flush(), first)
        self.assertEqual(self.flush(), second)
        self.assertEqual(self.sent, [first, second])
        self.assertFalse(self.path.with_name(self.path.name + ".refused").exists())  # a wait, no refusal

    def test_pending_lines_whose_join_is_refused_go_separately(self):
        self.check_pending_join("kota zqxvk", "mark bitti")

    def test_pending_join_refused_after_case_and_leet_folding(self):
        self.check_pending_join("kota ZQXVK", "m4rk bitti")

    # CDX-DORAEGRESS2 F3: a persisted outbox (queued by an earlier flush) is screened again.
    def seed_outbox(self, *lines):
        self.path.write_text(json.dumps({"version": 1, "pending": [], "daily": [], "ask_day": "",
                                         "asks": 0, "outbox": "\n".join(lines), "outbox_asks": 0}),
                             encoding="utf-8")

    def test_seeded_outbox_forbidden_lines_dropped_and_counted(self):
        self.seed_outbox(f"kota {MARK} bitti", f"anahtar {SECRET}", "durum temiz")
        self.assertEqual(self.flush(), "durum temiz")
        self.assertEqual(self.sent, ["durum temiz"])
        self.assertEqual(self.counts(), {"deny": 1, "secret": 1})
        state = self.path.read_text("utf-8")
        self.assertNotIn(MARK, state)
        self.assertNotIn(SECRET, state)

    def test_seeded_outbox_join_refused_line_waits_before_pending(self):
        self.seed_outbox("durum temiz", "kota zqxvk", "mark bitti")
        submit(self.path, Event("urgent", "c", "kuyruk temiz"))
        self.assertEqual(self.flush(), "durum temiz\nkota zqxvk")
        self.assertEqual(self.flush(), "mark bitti\nkuyruk temiz")  # the waiting line goes first
        self.assertEqual(self.sent, ["durum temiz\nkota zqxvk", "mark bitti\nkuyruk temiz"])
        self.assertFalse(self.path.with_name(self.path.name + ".refused").exists())

    # The join screen covers the whole message, not only the last pair: three lines, every
    # line and every adjacent pair clean, only all three together spell MARK.
    THREE = ("kota zqx", "vk", "mark bitti")

    def check_three_clean_pairs(self):
        deny = dora_screen.load_deny(self.deny)
        a, b, c = self.THREE
        for text in (a, b, c, f"{a}\n{b}", f"{b}\n{c}"):
            self.assertEqual(dora_screen.screen(text, deny), (True, ""), text)
        self.assertEqual(dora_screen.screen(f"{a}\n{b}\n{c}", deny), (False, "deny"))

    def test_pending_three_line_split_never_joined(self):
        self.check_three_clean_pairs()
        for i, line in enumerate(self.THREE):
            submit(self.path, Event("urgent", f"t{i}", line))
        self.assertEqual(self.flush(), "kota zqx\nvk")
        self.assertEqual(self.flush(), "mark bitti")
        self.assertEqual(self.sent, ["kota zqx\nvk", "mark bitti"])
        self.assertFalse(self.path.with_name(self.path.name + ".refused").exists())

    def test_seeded_outbox_three_line_split_never_joined(self):
        self.check_three_clean_pairs()
        self.seed_outbox(*self.THREE)
        self.assertEqual(self.flush(), "kota zqx\nvk")
        self.assertEqual(self.flush(), "mark bitti")
        self.assertEqual(self.sent, ["kota zqx\nvk", "mark bitti"])
        self.assertFalse(self.path.with_name(self.path.name + ".refused").exists())


if __name__ == "__main__":
    unittest.main()
