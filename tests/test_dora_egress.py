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


if __name__ == "__main__":
    unittest.main()
