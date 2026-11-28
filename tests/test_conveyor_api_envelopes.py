"""APIBROKER2: monthly envelopes, Batch and image input on the one ledger. Fake transports + a
fake credential reader, temp roots, an injected clock; no network, no real ledger, never data/jarvis.db."""
import base64
import builtins
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import tempfile
import threading
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "conveyor_api.py"
_SPEC = importlib.util.spec_from_file_location("conveyor_api_envelopes_under_test", _SRC)
api = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(api)

KEY = "sk-ant-api03-TESTONLY_secret-0123456789"
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
SECRET_TEXT = "REQUEST-TEXT-must-never-reach-the-ledger"
MSG = [{"role": "user", "content": "hi"}]
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + b"\x00\x00\x00\x01" * 2 + b"\x08\x02\x00\x00\x00" + b"\x00" * 4
JPEG = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xd9"  # SOI, SOF0 1x1, EOI


def ts(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp()


T0 = ts(8)  # injected clock; 10 days (8..17) to the fixture credit end -> daily share 9 USD
FIXTURE_END = datetime(2026, 11, 18, tzinfo=timezone.utc)


def ok_body(n_in=10, n_out=5, text="hello"):
    return json.dumps({"content": [{"type": "text", "text": text}],
                       "usage": {"input_tokens": n_in, "output_tokens": n_out}}).encode()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "build" / "conveyor"
        self.root.mkdir(parents=True)
        self.build = self.root.parent
        self.ledger = self.root / api.LEDGER_NAME
        self.sent, self.got = [], []
        self.reply = (200, ok_body())
        self.answers = []  # replies for the fake GET, in order
        self.reads = 0
        for pin in (mock.patch.object(api, "CREDIT_END", FIXTURE_END),
                    mock.patch.object(api, "ROOT", self.root),
                    mock.patch.object(api, "IMAGE_ROOT", self.build)):
            pin.start()
            self.addCleanup(pin.stop)

    def reader(self):
        self.reads += 1
        return KEY

    def post(self, url, headers, body, timeout):
        self.sent.append((url, headers, body, timeout))
        r = self.reply(body) if callable(self.reply) else self.reply
        if isinstance(r, BaseException):
            raise r
        return r

    def get(self, url, headers, body, timeout):
        self.got.append(url)
        r = self.answers.pop(0) if self.answers else (200, json.dumps({"processing_status": "in_progress"}).encode())
        if isinstance(r, BaseException):
            raise r
        return r

    def send(self, purpose="prescreen", model=HAIKU, messages=MSG, max_tokens=100, now=T0, **kw):
        return api.send(purpose, model, "sys", messages, max_tokens, root=self.root, clock=lambda: now,
                        reader=kw.pop("reader", self.reader), post=self.post, **kw)

    def submit(self, purpose="night", requests=None, now=T0, **kw):
        reqs = requests if requests is not None else [
            {"custom_id": "a", "model": HAIKU, "messages": MSG, "max_tokens": 100},
            {"custom_id": "b", "model": SONNET, "messages": MSG, "max_tokens": 200, "system": "s"}]
        return api.submit(purpose, reqs, root=self.root, clock=lambda: now, reader=self.reader,
                          post=self.post, **kw)

    def collect(self, ref, now=T0 + 60):
        return api.collect(ref, root=self.root, clock=lambda: now, reader=self.reader, get=self.get)

    def rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(x) for x in self.ledger.read_text(encoding="utf-8").splitlines()]

    def spend(self, *rows):  # (ts, usd, purpose)
        with open(self.ledger, "a", encoding="utf-8") as f:
            for t, usd, p in rows:
                f.write(json.dumps({"ts": t, "usd": usd, "purpose": p}) + "\n")

    def net(self, purpose=None):
        return round(sum(r["usd"] - r.get("release", 0) for r in self.rows()
                         if purpose is None or r.get("purpose") == purpose), 6)

    def refused(self, fn, needle=""):
        with self.assertRaises(api.ApiRefused) as cm:
            fn()
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(self.sent, [], "a refusal must come before any request")
        return cm.exception


def cfg(**kw):
    return {**api.ENVELOPES_DEFAULT, **kw}


class EnvelopeConfigTest(Base):
    def test_default_envelopes_and_overflow(self):
        self.assertEqual(api.envelopes(), {"prescreen": 60.0, "night": 60.0, "visual": 10.0,
                                           "watch": 10.0, "overflow": 0.0})
        self.assertEqual(api.envelopes(cfg(monthly_usd=150))["overflow"], 10.0)

    def test_neg_shape_matrix_refuses_construction(self):
        bad = [[], "x", {}, {k: v for k, v in api.ENVELOPES_DEFAULT.items() if k != "watch"},
               cfg(extra=1), cfg(Prescreen=60), cfg(prescreen="60"), cfg(night=True), cfg(visual=None),
               cfg(watch=[10]), cfg(prescreen=-1), cfg(night=float("nan")), cfg(visual=float("inf")),
               cfg(watch=10**400), cfg(monthly_usd=10**400), cfg(monthly_usd=-140),
               cfg(monthly_usd=139.99), cfg(prescreen=61)]
        for c in bad:
            with self.subTest(c=repr(c)[:60]), self.assertRaises(api.ApiRefused):
                api.envelopes(c)

    def test_neg_bad_config_file_refuses_every_call(self):
        for text in ('{"monthly_usd": NaN}', "not json", '{"monthly_usd": 1' + "0" * 400 + "}",
                     json.dumps(cfg(monthly_usd=1)), "[]"):
            with self.subTest(text=text[:30]):
                (self.root / api.ENVELOPES_NAME).write_text(text, encoding="utf-8")
                self.refused(lambda: self.send(), "envelope config")
        self.assertEqual(self.rows(), [])

    def test_config_file_names_a_larger_total(self):
        (self.root / api.ENVELOPES_NAME).write_text(json.dumps(cfg(monthly_usd=145)), encoding="utf-8")
        self.assertEqual(self.send(purpose="overflow"), "hello")


BIG = {"model": SONNET, "max_tokens": 8192}  # worst case ~0.083 USD


class SendTest(Base):
    def test_neg_envelope_refused_before_send(self):
        self.spend((ts(3), 9.99, "visual"))
        self.refused(lambda: self.send(purpose="visual", **BIG), "envelope visual")
        self.assertEqual((len(self.rows()), self.reads), (1, 0))

    def test_neg_daily_share_refused_before_send(self):
        self.spend((ts(8, 1), 8.95, "watch"))
        self.refused(lambda: self.send(**BIG), "daily share")
        self.assertEqual(len(self.rows()), 1)

    def test_neg_hard_cap_refused_before_send(self):
        self.spend((ts(3), 89.99, "zzz"))
        self.refused(lambda: self.send(**BIG), "monthly cap")
        self.assertEqual(len(self.rows()), 1)

    def test_reserve_then_settle_one_call(self):
        self.assertEqual(self.send(), "hello")
        sent, done = self.rows()
        self.assertEqual((sent["status"], sent["purpose"], self.reads), ("sent", "prescreen", 1))
        self.assertGreater(sent["usd"], 0)
        self.assertEqual(done["release"], sent["usd"])
        self.assertAlmostEqual(self.net(), 4e-6)  # 3.5e-6 booked rounded up


class ConcurrencyTest(Base):
    def test_neg_two_calls_only_one_fits_exactly_one_sent(self):
        self.spend((ts(3), 9.88, "visual"))  # 0.12 left: one ~0.083 call fits, two do not
        self.reply = lambda body: (time.sleep(0.05), (200, ok_body(len(body), 8192)))[1]
        gate, out = threading.Barrier(2), []

        def one():
            gate.wait()
            try:
                out.append(self.send(purpose="visual", **BIG))
            except api.ApiRefused as e:
                out.append(e)
        threads = [threading.Thread(target=one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sorted(map(type, out), key=str), sorted([str, api.ApiRefused], key=str))
        self.assertIn("envelope visual", str(next(x for x in out if not isinstance(x, str))))


class PurposeSpoofTest(Base):
    def test_neg_purpose_spoofing(self):
        for p in ("Prescreen", " prescreen", "prescreen ", "prescreen\n", "orch test", "", None, 1):
            with self.subTest(p=p):
                self.refused(lambda: self.send(purpose=p), "unknown purpose")
                self.refused(lambda: self.submit(purpose=p), "unknown purpose")
        for p in ("visual", "watch"):
            with self.subTest(p=p):
                self.refused(lambda: self.submit(purpose=p), "not allowed for batch")
        (self.build / "a.png").write_bytes(PNG)
        self.refused(lambda: self.send(images=[self.build / "a.png"]), "not allowed for images")
        self.assertEqual(self.rows(), [])

    def test_neg_model_allow_list_by_name(self):
        for m in ("claude-fable-5-2", "CLAUDE-FABLE", "fable", None, 5):
            with self.subTest(m=m):
                self.refused(lambda: self.send(model=m), "Fable")
                self.refused(lambda: self.submit(requests=[{"custom_id": "a", "model": m, "messages": MSG,
                                                            "max_tokens": 10}]), "Fable")
        for m in ("claude-opus-5-5", HAIKU + " ", "claude-haiku-4"):
            with self.subTest(m=m):
                self.refused(lambda: self.send(model=m), "no price row")
        self.assertEqual(self.rows(), [])


def results(*lines):
    return (200, "\n".join(json.dumps(x) for x in lines).encode())


def ok_line(cid="a", model=HAIKU, n_in=10, n_out=5, text="ok"):
    return {"custom_id": cid, "result": {"type": "succeeded", "message": {
        "model": model, "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": n_in, "output_tokens": n_out}}}}


ENDED = (200, json.dumps({"processing_status": "ended"}).encode())
INFO = api.API_URL + "/batches/msgbatch_01"


class BatchTest(Base):
    def setUp(self):
        super().setUp()
        self.reply = (200, json.dumps({"id": "msgbatch_01"}).encode())

    def reserve(self):
        return self.rows()[0]["usd"]

    def test_submit_reserves_worst_case_at_batch_rate(self):
        ref = self.submit()
        self.assertRegex(ref, r"\A[0-9a-f]{16}\Z")
        self.assertEqual(self.sent[0][0], api.API_URL + "/batches")
        sent, ok = self.rows()
        self.assertEqual((sent["batch"], sent["purpose"], ok["status"]), (ref, "night", "batch submitted"))
        self.assertGreater(sent["usd"], 0)
        full = (100 * 0.5 + 200 * 10.0) / 1e6  # output tokens alone at list price
        self.assertLess(sent["usd"], full)

    def test_neg_never_answered_expires_once_at_reserved_amount(self):
        ref = self.submit()
        self.assertEqual(self.collect(ref)["status"], "pending")
        self.assertEqual(self.got, [INFO])
        for _ in range(2):
            self.assertEqual(self.collect(ref, now=T0 + 49 * 3600)["status"], "expired")
        self.assertEqual(self.got, [INFO])
        self.assertEqual(sum(1 for r in self.rows() if r.get("final")), 1)
        self.assertEqual(self.net(), self.reserve())

    def test_neg_answered_twice_settles_once(self):
        ref = self.submit()
        self.answers = [ENDED, results(ok_line(), {"custom_id": "b", "result": {"type": "errored"}})]
        r = self.collect(ref)
        self.assertEqual((r["status"], r["results"]), ("settled", {"a": "ok", "b": None}))
        self.answers = [ENDED, results(ok_line(n_out=999))]
        self.assertEqual(self.collect(ref), {"status": "settled"})
        self.assertEqual(len(self.got), 2)
        self.assertEqual(self.net(), round((10 * 0.1 + 5 * 0.5) * 0.5 / 1e6, 6))

    def test_neg_wrong_shape_counts_reserved_amount(self):
        for body in ((200, b"not json\n{"), results(ok_line(), ok_line()), (200, b""),
                     results(ok_line(model="claude-fable-5-2")), results(ok_line(n_in=-1))):
            with self.subTest(body=body[1][:40]):
                self.ledger.unlink(missing_ok=True)
                ref = self.submit()
                self.answers = [ENDED, body]
                self.assertEqual(self.collect(ref)["status"], "unpriced")
                self.assertEqual(self.net(), self.reserve())

    def test_neg_transport_trouble_stays_pending(self):
        ref = self.submit()
        for answer in (RuntimeError(KEY), (500, b"{}"), (200, b"[]"), (200, b"nope")):
            with self.subTest(answer=type(answer).__name__):
                self.answers = [answer]
                self.assertEqual(self.collect(ref)["status"], "pending")
        self.assertFalse(any(r.get("final") for r in self.rows()))

    def test_neg_unaccepted_batch_counts_reserved_amount(self):
        self.reply = (200, b"{}")
        with self.assertRaises(api.ApiError):
            self.submit()
        ref = self.rows()[0]["batch"]
        self.assertEqual(self.collect(ref)["status"], "unaccepted")
        self.assertEqual((self.got, self.net()), ([], self.reserve()))

    def test_neg_caps_on_count_bytes_and_ids(self):
        one = {"custom_id": "a", "model": HAIKU, "messages": MSG, "max_tokens": 10}
        self.refused(lambda: self.submit(requests=[]), "1 to 100")
        self.refused(lambda: self.submit(requests=[dict(one, custom_id=str(i)) for i in range(101)]), "1 to 100")
        fat = [{"role": "user", "content": "x" * 11000}]
        self.refused(lambda: self.submit(requests=[dict(one, custom_id=str(i), messages=fat)
                                                   for i in range(100)]), "batch over")
        self.refused(lambda: self.submit(requests=[one, one]), "unique")
        self.refused(lambda: self.submit(requests=[dict(one, extra=1)]), "batch request")
        self.refused(lambda: self.submit(requests="x"), "")
        for ref in ("nothex", "0" * 16, None, "A" * 16):
            with self.subTest(ref=ref), self.assertRaises(api.ApiRefused):
                self.collect(ref)
        self.assertEqual(self.rows(), [])


class ImageTest(Base):
    def test_png_and_jpeg_inside_build_for_visual(self):
        for name, data, media in (("a.png", PNG, b"image/png"), ("sub/b.jpg", JPEG, b"image/jpeg")):
            with self.subTest(name=name):
                p = self.build / name
                p.parent.mkdir(exist_ok=True)
                p.write_bytes(data)
                self.sent.clear()
                self.assertEqual(self.send(purpose="visual", images=[str(p)]), "hello")
                body = self.sent[0][2]
                self.assertIn(media, body)
                self.assertIn(base64.b64encode(data), body)

    def test_neg_paths_files_and_caps_refused(self):
        outside = self.build.parent / "x.png"
        outside.write_bytes(PNG)
        (self.build / "t.png").write_text("not an image", encoding="utf-8")
        big = self.build / "big.png"
        big.write_bytes(PNG)
        os.truncate(big, api.IMAGE_MAX_BYTES + 1)  # sparse: no 5 MB written
        ok = str(self.build / "ok.png")
        Path(ok).write_bytes(PNG)
        cases = [(str(outside), "inside build"), (str(self.build / ".." / "x.png"), "inside build"),
                 (str(self.build), "inside build"), (str(self.build / "t.png"), "PNG or JPEG"),
                 (str(big), "image over"), ("a\x00.png", "image path refused"),
                 (r"\\server\share\a.png", "image path refused"), ("//server/a.png", "image path refused"),
                 (ok + ":evil", "image path refused"), (str(self.build / "missing.png"), "image path refused"),
                 (123, "image path refused"), ("", "image path refused")]
        for path, needle in cases:
            with self.subTest(path=path):
                self.refused(lambda: self.send(purpose="visual", images=[path]), needle)
        self.refused(lambda: self.send(purpose="watch", images=[ok]), "not allowed for images")
        self.refused(lambda: self.send(purpose="visual", images=[ok] * 5), "at most 4")
        self.refused(lambda: self.send(purpose="visual", images=ok), "list of paths")
        self.refused(lambda: self.send(purpose="visual", images=[ok],
                                       messages=MSG + [{"role": "assistant", "content": "x"}]), "images go with")
        self.assertEqual(self.rows(), [])


class ReviewFixTest(Base):
    """APIBROKER2FIX: each test fails on the code before the fix."""

    def reserve(self):
        return self.rows()[0]["usd"]

    def answered(self, *lines):
        self.ledger.unlink(missing_ok=True)
        self.reply = (200, json.dumps({"id": "msgbatch_01"}).encode())
        ref = self.submit()
        self.answers = [ENDED, results(*lines) if lines and isinstance(lines[0], dict) else lines[0]]
        return self.collect(ref)

    def test_neg_f1_image_reserve_covers_pixel_tokens(self):
        self.assertGreaterEqual(api.IMAGE_MAX_TOKENS, 4784)  # 2576 px long edge, w*h/750
        p = self.build / "a.png"
        p.write_bytes(PNG)  # a few bytes on disk, billed by pixels
        for k in (1, api.IMAGE_MAX_COUNT):
            with self.subTest(k=k):
                self.ledger.unlink(missing_ok=True)
                self.sent.clear()
                self.send(purpose="visual", images=[str(p)] * k)
                floor = ((len(self.sent[0][2]) + k * 1600) * 0.1 + 100 * 0.5) / 1e6
                self.assertGreaterEqual(self.rows()[0]["usd"], floor)

    def test_neg_f2_no_image_root_parameter(self):
        for fn in (api.send, api._image):
            self.assertNotIn("image_root", inspect.signature(fn).parameters)

    def test_neg_f3_unknown_result_type_unpriced(self):
        for res in ({}, {"type": "weird"}, {"type": None}, {"type": ["errored"]}):
            with self.subTest(res=res):
                r = self.answered({"custom_id": "a", "result": res}, ok_line("b", SONNET))
                self.assertEqual(r["status"], "unpriced")
                self.assertEqual(self.net(), self.reserve())

    def test_neg_f4_ids_and_models_must_match_submission(self):
        for lines in ((ok_line("x"), ok_line("y", SONNET)), (ok_line("a", SONNET), ok_line("b", SONNET)),
                      (ok_line(), ok_line("b", HAIKU))):
            with self.subTest(lines=[(x["custom_id"], x["result"]["message"]["model"]) for x in lines]):
                self.assertEqual(self.answered(*lines)["status"], "unpriced")
                self.assertEqual(self.net(), self.reserve())
        self.ledger.unlink()
        ref = self.submit()  # a reserve row without the submitted models
        rows = self.rows()
        self.assertEqual(rows[0]["models"], {"a": HAIKU, "b": SONNET})
        del rows[0]["models"]
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.answers = [ENDED, results(ok_line(), ok_line("b", SONNET))]
        self.assertEqual(self.collect(ref)["status"], "unpriced")

    def test_neg_f5_charges_round_up(self):
        self.reply = (200, ok_body(1, 0))  # 1e-7 USD
        self.send()
        self.assertEqual(self.rows()[-1]["usd"], 1e-6)
        r = self.answered(ok_line(n_in=1, n_out=0), {"custom_id": "b", "result": {"type": "expired"}})
        self.assertEqual((r["status"], self.rows()[-1]["usd"]), ("settled", 1e-6))  # 5e-8 at batch rate

    def test_neg_f6_deep_nesting_unpriced_not_crash(self):
        r = self.answered((200, ("[" * 100000 + "]" * 100000).encode()))
        self.assertEqual(r["status"], "unpriced")

    def test_neg_f7_confinement_checked_on_opened_handle(self):
        outside = self.build.parent / "out.png"
        outside.write_bytes(PNG)
        (self.build / "a.png").write_bytes(PNG)
        swap = lambda p, m="r", *a, **k: builtins.open(outside, m)  # noqa: E731 - swapped after the check
        with mock.patch.object(api, "open", swap, create=True):
            self.refused(lambda: self.send(purpose="visual", images=[str(self.build / "a.png")]), "inside build")

    def test_neg_f8_signature_only_files_refused(self):
        for name, data in (("p.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32),  # the old fixtures: signature only
                           ("j.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32),
                           ("s.jpg", b"\xff\xd8\xff\xda\x00\x08" + b"\x00" * 12),
                           ("t.jpg", b"\xff\xd8\xff\xc0\x00\x40\x08"), ("q.png", PNG[:20])):
            with self.subTest(name=name):
                (self.build / name).write_bytes(data)
                self.refused(lambda: self.send(purpose="visual", images=[str(self.build / name)]), "PNG or JPEG")

    def test_neg_review_mixed_slash_unc_never_resolved(self):
        seen, real = [], api.Path.resolve
        spy = lambda p, *a, **k: seen.append(str(p)) or real(p, *a, **k)  # noqa: E731
        with mock.patch.object(api.Path, "resolve", spy):
            for s in ("\\/srv/share/a.png", "/\\srv\\share\\a.png", "\\/./pipe/x"):
                with self.subTest(s=s):
                    self.refused(lambda: self.send(purpose="visual", images=[s]), "image path")
        self.assertFalse([x for x in seen if "srv" in x or "pipe" in x])  # no SMB / device open

    def test_neg_review_line_separator_inside_text_still_settles(self):
        a = ok_line()
        a["result"]["message"]["content"][0]["text"] += " \u0085"
        body = "\n".join(json.dumps(x, ensure_ascii=False) for x in (a, ok_line("b", SONNET))).encode()
        self.assertEqual(self.answered((200, body))["status"], "settled")


class ReportTest(Base):
    def setUp(self):
        super().setUp()
        self.spend((ts(3), 1.0, "prescreen"), (ts(4), 0.5, "visual"), (ts(4), 0.25, "zzz"),
                   (datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp(), 7.0, "night"))

    def test_report_by_purpose_and_day(self):
        self.assertEqual(api.report("2026-11"), {
            "by_purpose": {"prescreen": 1.0, "visual": 0.5, "other": 0.25},
            "by_day": {"2026-11-03": 1.0, "2026-11-04": 0.75}, "total": 1.75})
        self.assertEqual(api.report("2026-10", root=self.root)["by_purpose"], {"night": 7.0})
        for month in ("2026-13", "2026-1", " 2026-11", None):
            with self.subTest(month=month), self.assertRaises(api.ApiRefused):
                api.report(month)

    def test_cli_prints_weekly_table(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(api.main(["report", "2026-11"]), 0)
        text = out.getvalue()
        self.assertRegex(text, r"prescreen\s+60\.00\s+1\.0000\s+59\.0000")
        self.assertIn("2026-11-03  1.0000", text)
        self.assertIn("total 1.7500 of hard cap 90.00 USD", text)
        with contextlib.redirect_stderr(err):
            self.assertEqual(api.main(["report", "2026-13"]), 1)
            self.assertEqual(api.main(["report"]), 2)


class LedgerScanTest(Base):
    def assertClean(self, *texts):
        for t in texts:
            for needle in (KEY, "sk-ant-", SECRET_TEXT):
                self.assertNotIn(needle, t)

    def test_neg_ledger_and_output_carry_no_key_no_request_text(self):
        secret = [{"role": "user", "content": SECRET_TEXT}]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.send(messages=secret)
            self.reply = (200, json.dumps({"id": "msgbatch_01"}).encode())
            ref = self.submit(requests=[{"custom_id": "a", "model": HAIKU, "messages": secret,
                                         "max_tokens": 10, "system": SECRET_TEXT}])
            self.answers = [ENDED, results(ok_line())]
            self.assertEqual(self.collect(ref)["status"], "settled")
            self.assertEqual(api.main(["report", "2026-11"]), 0)
        self.assertIn(SECRET_TEXT.encode(), self.sent[0][2])  # it did reach the (fake) API
        self.assertClean(self.ledger.read_text(encoding="utf-8"), out.getvalue(), err.getvalue())
        self.assertTrue(all(r["purpose"] in api.PURPOSES for r in self.rows()))

    def test_neg_transport_and_reader_errors_carry_no_key(self):
        self.reply = RuntimeError(KEY)
        with self.assertRaises(api.ApiError) as cm:
            self.send()
        e = cm.exception
        self.assertClean(str(e), repr(e), "".join(traceback.format_exception(e)),
                         self.ledger.read_text(encoding="utf-8"))
        self.ledger.unlink()
        self.sent.clear()

        def raises():
            raise RuntimeError(KEY)
        for reader in (lambda: "not-a-key", raises):
            with self.subTest(reader=reader):
                e = self.refused(lambda: self.send(reader=reader), "credential unreadable")
                self.assertClean("".join(traceback.format_exception(e)))
        self.assertEqual(self.rows(), [])


class StaticTest(unittest.TestCase):
    def test_neg_one_host_and_one_credential_read_site(self):
        src = _SRC.read_text(encoding="utf-8")
        hosts = re.findall(r"https?://[^\s\"'`)]+", src)
        self.assertTrue(hosts)
        for h in hosts:
            self.assertTrue(h.startswith("https://api.anthropic.com/"), h)
        self.assertEqual(src.count("def read_credential"), 1)
        self.assertEqual(src.count("read_credential)()"), 2)  # call() and the broker's _key
        self.assertNotIn("def " + "heartbeat", src)
        self.assertNotIn("project_folders", src)


if __name__ == "__main__":
    unittest.main()
