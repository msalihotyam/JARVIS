"""APIBROKER: the guarded Anthropic API client. Fake transport + fake credential reader, temp dirs,
an injected clock; no network, no real credential read, never data/jarvis.db."""
import builtins
import importlib.util
import json
import os
import tempfile
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "conveyor_api", Path(__file__).resolve().parents[1] / "scripts" / "conveyor_api.py")
api = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(api)

KEY = "sk-ant-api03-TESTONLY_secret-0123456789"
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
MSG = [{"role": "user", "content": "hi"}]


def ts(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp()


T0 = ts(8)  # the injected clock: 2026-11-08 12:00 UTC, 10 days (8..17) left; never the wall clock


def ok_body(n_in=10, n_out=5, text="hello"):
    return json.dumps({"content": [{"type": "text", "text": text}, {"type": "tool_use", "id": "x"}],
                       "usage": {"input_tokens": n_in, "output_tokens": n_out}}).encode()


class ApiBrokerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = self.root / api.LEDGER_NAME
        self.sent = []
        self.reply = (200, ok_body())

    def post(self, url, headers, body, timeout):
        self.sent.append((url, headers, body, timeout))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    def call(self, model=HAIKU, messages=MSG, max_tokens=100, reader=lambda: KEY, now=T0, **kw):
        return api.call(model, "sys", messages, max_tokens, kw.pop("purpose", "test"), root=self.root,
                        clock=lambda: now, reader=reader, post=self.post, **kw)

    def rows(self):
        return [json.loads(x) for x in self.ledger.read_text(encoding="utf-8").splitlines()]

    def spend(self, *rows):
        with open(self.ledger, "a", encoding="utf-8") as f:
            for t, usd in rows:
                f.write(json.dumps({"ts": t, "usd": usd}) + "\n")

    def refused(self, fn, needle=""):
        with self.assertRaises(api.ApiRefused) as cm:
            fn()
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(self.sent, [], "a refusal must come before any request")
        return cm.exception

    def assert_clean(self, exc):
        self.assertIsNone(exc.__context__)  # no chained exception that could carry the key
        for text in (str(exc), repr(exc), "".join(traceback.format_exception(exc))):
            self.assertNotIn(KEY, text)
            self.assertNotIn("sk-ant-", text)

    # --- NEG: the key never leaks -------------------------------------------------------------
    def test_neg_key_never_in_ledger_exception_or_repr(self):
        self.assertEqual(self.call(purpose=f"why {KEY}"), "hello")
        self.reply = (401, json.dumps({"error": {"type": f"auth {KEY}", "message": KEY}}).encode())
        with self.assertRaises(api.ApiError) as cm:
            self.call()
        self.assert_clean(cm.exception)
        self.reply = TimeoutError(f"boom {KEY}")
        with self.assertRaises(api.ApiError) as cm:
            self.call()
        self.assert_clean(cm.exception)
        text = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn(KEY, text)
        self.assertNotIn("sk-ant-", text)
        self.assertEqual(self.sent[0][1]["x-api-key"], KEY)  # it does reach the transport header

    def test_neg_unreadable_credential_refuses(self):
        def broken():
            raise OSError(f"denied {KEY}")
        env = {"ANTHROPIC_API_KEY": KEY, "JARVIS_READER_API_KEY": KEY}
        with mock.patch.dict(os.environ, env):  # an env var key is never a fallback
            for reader in (lambda: "", lambda: None, broken, lambda: "not-a-key",
                           lambda: KEY + "\r\nx-evil: 1"):
                self.assert_clean(self.refused(lambda: self.call(reader=reader), "credential"))
        self.assertFalse(self.ledger.exists())

    def test_neg_default_reader_is_the_credential_manager_only(self):
        fake = mock.Mock(return_value="")  # the real CredReadW path is never run in a test
        with mock.patch.object(api, "read_credential", fake):
            self.refused(lambda: api.call(HAIKU, "", MSG, 10, "t", root=self.root, clock=lambda: T0,
                                          post=self.post), "credential")
        fake.assert_called_once_with()
        dll = mock.Mock()  # CredReadW fails: "" even with the key in the env (never a fallback)
        dll.CredReadW.return_value = 0
        with mock.patch.object(api.ctypes, "WinDLL", return_value=dll), \
                mock.patch.object(api.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}):
            self.assertEqual(api.read_credential(), "")
        self.assertEqual(dll.CredReadW.call_args.args[:3], ("konveyor-bekci", 1, 0))
        dll.CredFree.assert_not_called()

    def test_all_worktrees_share_the_main_checkout_ledger(self):
        main = self.root / "main"
        wt, gitdir = main / ".claude" / "worktrees" / "X", main / ".git" / "worktrees" / "X"
        gitdir.mkdir(parents=True)
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        self.assertEqual(api.main_checkout(wt), main.resolve())
        self.assertEqual(api.main_checkout(main), main)

    def test_neg_non_windows_refuses(self):
        with mock.patch.object(api.sys, "platform", "linux"):
            self.refused(lambda: self.call(), "Windows")
            self.assertEqual(api.read_credential(), "")

    def test_neg_opus_or_unknown_model_refused(self):
        for model in ("claude-opus-5-5", "claude-fable-5-1", "claude-haiku-5-5-x", "CLAUDE-HAIKU-5-5",
                      "claude-haiku", "", None):
            self.refused(lambda: self.call(model=model), "model")

    def test_neg_text_only_arguments(self):
        blocks = [{"role": "user", "content": [{"type": "document", "source": {}}]}]
        for kw in ({"messages": blocks}, {"messages": [{"role": "user", "content": "x", "stream": True}]},
                   {"messages": []}, {"messages": [{"role": "system", "content": "x"}]},
                   {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": api.MAX_TOKENS_CAP + 1}):
            self.refused(lambda: self.call(**kw), "text only")
        self.call()
        body = json.loads(self.sent[0][2])
        self.assertEqual(set(body), {"model", "max_tokens", "messages", "system"})  # no stream/tools
        url, headers, _, timeout = self.sent[0]
        self.assertEqual((url, timeout), ("https://api.anthropic.com/v1/messages", 60))
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    # --- NEG: money caps refuse before any request ----------------------------------------------
    def test_neg_monthly_cap_refuses_before_request(self):
        self.spend((ts(1), 90.0))
        self.refused(lambda: self.call(), "monthly cap")
        self.spend((ts(1), -1))  # a bad row never undercounts: it refuses
        self.refused(lambda: self.call(), "ledger row invalid")

    def test_neg_daily_share_refuses_before_request(self):
        self.spend((ts(8, 1), 8.99999))  # share today = 90 / 10 days = 9.0; est ~0.00006
        self.refused(lambda: self.call(), "daily share")
        self.assertEqual(len(self.rows()), 1)

    def test_daily_share_carries_over(self):
        spent_today = [(T0 - 60, 9.5)]
        with self.assertRaises(api.ApiRefused):  # Nov 8: 90 / 10 = 9.0
            api.check_budget(T0, [{"ts": t, "usd": u} for t, u in spent_today], 90.0, None, 0.01)
        # Nov 9 after an unused Nov 8: 90 / 9 = 10.0 -> 9.5 fits
        api.check_budget(ts(9), [{"ts": ts(9, 1), "usd": 9.5}], 90.0, None, 0.01)
        # a used-up Nov 8 (9.0) leaves 81 / 9 = 9.0 for Nov 9 -> 9.5 does not fit
        with self.assertRaises(api.ApiRefused):
            api.check_budget(ts(9), [{"ts": ts(8), "usd": 9.0}, {"ts": ts(9, 1), "usd": 9.5}], 90.0, None, 0.01)
        # October spend is another month; the credit end refuses everything
        api.check_budget(T0, [{"ts": ts(8) - 30 * 86400, "usd": 80.0}], 90.0, None, 0.01)
        with self.assertRaises(api.ApiRefused):
            api.check_budget(api.CREDIT_END.timestamp(), [], 90.0, None, 0.0)

    def test_override_file_only_lowers_caps(self):
        caps = self.root / api.CAPS_NAME
        self.assertEqual(api.caps(self.root), (90.0, None))
        caps.write_text('{"monthly_usd": 500, "daily_usd": 1000}')
        self.assertEqual(api.caps(self.root), (90.0, 1000))  # 1000 > share: min() keeps the share
        api.check_budget(T0, [{"ts": T0, "usd": 8.0}], 90.0, 1000, 0.5)
        caps.write_text('{"monthly_usd": 5, "daily_usd": 0.001}')
        self.assertEqual(api.caps(self.root), (5, 0.001))
        self.refused(lambda: self.call(max_tokens=api.MAX_TOKENS_CAP), "daily share")  # est ~0.004
        caps.write_text('{"monthly_usd": 0}')
        self.refused(lambda: self.call(), "monthly cap")
        for bad in ('{"monthly_usd": NaN}', '{"daily_usd": Infinity}', '{"monthly_usd": true}',
                    '{"monthly_usd": -1}', '{"monthly_usd": "1"}', '{"weekly_usd": 1}', '[1]', '{'):
            caps.write_text(bad)
            self.refused(lambda: self.call(), api.CAPS_NAME)

    # --- cost math and failures -----------------------------------------------------------------
    def test_cost_from_usage(self):
        self.reply = (200, ok_body(1_000_000, 1_000_000, "a"))
        with mock.patch.object(api, "check_budget"):  # this test is about the math, not the cap
            self.assertEqual(self.call(), "a")
            self.reply = (200, ok_body(1000, 2000, "b"))
            self.assertEqual(self.call(model=SONNET, purpose="p" * 500), "b")
        (rh, h, rs, s) = self.rows()  # a reserve row before each request, then the settled row
        self.assertEqual((rh["status"], rs["status"], h["status"]), ("sent", "sent", 200))
        self.assertEqual((h["model"], h["in"], h["out"], h["usd"]), (HAIKU, 10**6, 10**6, 0.6))
        self.assertEqual((s["model"], s["usd"], len(s["purpose"])), (SONNET, 0.022, api.PURPOSE_MAX))
        self.assertEqual(set(h), {"ts", "purpose", "model", "in", "out", "usd", "status", "release"})
        self.assertEqual((h["ts"], rh["ts"]), (T0, T0))
        self.assertGreater(rh["usd"], 0)
        self.assertEqual((h["release"], s["release"]), (rh["usd"], rs["usd"]))  # reserve released

    def test_neg_non_2xx_or_malformed_json_is_clean_error_at_zero_cost(self):
        msgs = []
        for reply in ((429, b'{"error": {"type": "rate_limit_error"}}'), (500, b""),
                      (200, b"not json"), (200, b"[1, 2]"), (200, b'{"content": []}')):
            self.reply = reply
            with self.assertRaises(api.ApiError) as cm:
                self.call()
            self.assert_clean(cm.exception)
            self.assertIsNone(cm.exception.__cause__)
            msgs.append(str(cm.exception))
        self.assertEqual(msgs, ["API status 429 rate_limit_error", "API status 500"]
                         + ["API response without usage/content"] * 3)
        rows = self.rows()
        net = [a["usd"] + b["usd"] - b["release"] for a, b in zip(rows[::2], rows[1::2])]
        self.assertEqual(net[:2], [0.0, 0.0])  # not billed: the reserve is released
        self.assertTrue(all(n > 0 for n in net[2:]))  # a 2xx may be billed: the estimate stays
        self.assertEqual([r["status"] for r in rows[1::2]], [429, 500, 200, 200, 200])
        for exc, err in ((TimeoutError(), api.ApiError), (KeyboardInterrupt(), KeyboardInterrupt)):
            self.reply = exc  # sent and maybe billed, or killed mid-call: the reserve row stays
            with self.assertRaises(err):
                self.call()
            last = self.rows()[-1]
            self.assertEqual(last["status"], "sent")
            self.assertGreater(last["usd"], 0)
        self.assertEqual(len(self.rows()), 12)

    def test_neg_never_opens_jarvis_db(self):
        seen = []

        def rec(real):
            return lambda p, *a, **kw: (seen.append(str(p)), real(p, *a, **kw))[1]

        self.spend((ts(1), 1.0))  # read_ledger opens the ledger
        with mock.patch.object(builtins, "open", rec(builtins.open)), mock.patch.object(os, "open", rec(os.open)):
            self.assertEqual(self.call(), "hello")
        self.assertTrue(seen)
        self.assertFalse(any("jarvis.db" in p.lower() for p in seen))

    def test_redirects_are_never_followed(self):
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "https://x"))


if __name__ == "__main__":
    unittest.main()
