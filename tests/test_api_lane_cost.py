"""APILANE3: overflow session cost meter (counts only) and its one ledger row per session."""
import importlib.util
import json
import math
import re
import tempfile
import unittest
from pathlib import Path

from tests.adversarial import HOSTILE_NUMBERS, each

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "api_lane_cost.py"
_SPEC = importlib.util.spec_from_file_location("api_lane_cost", _PATH)
lc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lc)
PRICES = lc.ca.PRICES
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
SECRET = "SECRET-PROMPT-TEXT-do-not-store"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def msg(mid="m1", model=SONNET, sid="s1", **usage):
    return {"type": "assistant", "session_id": sid,
            "message": {"id": mid, "model": model, "usage": usage,
                        "content": [{"type": "text", "text": SECRET}]}}


def meter(cap=1.0, **kw):
    return lc.Meter(PRICES, cap, clock=Clock(), **kw)


def ledger_rows(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class AcceptanceTests(unittest.TestCase):
    def test_running_total_and_cap(self):
        m = meter(cap=12.0)
        m.feed(msg(input_tokens=1_000_000, output_tokens=0))
        self.assertAlmostEqual(m.usd(), 2.0)
        self.assertFalse(m.over_cap())
        m.feed(msg("m2", output_tokens=1_000_000))
        self.assertAlmostEqual(m.usd(), 12.0)
        self.assertTrue(m.over_cap())

    def test_cache_rates_and_kinds(self):
        m = meter()
        m.feed(msg(cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000))
        self.assertAlmostEqual(m.usd(), 2.0 * 0.1 + 2.0 * 1.25)
        c = m.final({"type": "result"})
        self.assertEqual(c.tokens, {"in": 0, "out": 0, "cache_read": 1_000_000, "cache_write": 1_000_000})

    def test_repeated_message_counted_once(self):
        m = meter()
        m.feed(msg(input_tokens=500_000, output_tokens=10))
        m.feed(msg(input_tokens=500_000, output_tokens=100_000))
        self.assertAlmostEqual(m.usd(), 1.0 + 1.0)

    def test_final_cost_charges_the_larger(self):
        clock = Clock(10.0)
        m = lc.Meter(PRICES, 5.0, clock=clock)
        m.feed(msg(model=HAIKU, input_tokens=1_000_000))
        clock.t = 72.34
        c = m.final({"type": "result", "total_cost_usd": 0.75, "session_id": "s1"})
        self.assertEqual((c.usd, c.measured_usd, c.reported_usd), (0.75, 0.1, 0.75))
        self.assertEqual((c.model, c.seconds, c.session), (HAIKU, 62.3, "s1"))
        m2 = meter()
        m2.feed(msg(input_tokens=1_000_000))
        c2 = m2.final({"type": "result", "total_cost_usd": 0.5})
        self.assertEqual((c2.usd, c2.measured_usd, c2.reported_usd), (2.0, 2.0, 0.5))

    def test_result_event_finalizes(self):
        m = meter()
        m.feed(msg(input_tokens=10))
        m.feed({"type": "result", "total_cost_usd": 1.5})
        self.assertEqual(m.cost.usd, 1.5)

    def test_unknown_model_dearest_rate_and_flag(self):
        for model in ("claude-mystery-9", "claude-fable-5-2", None):
            m = meter()
            m.feed(msg(model=model, input_tokens=1_000_000, output_tokens=1_000_000))
            self.assertAlmostEqual(m.usd(), 2.0 + 10.0)
            self.assertIn("unknown_model", m.final().flags)

    def test_killed_session_charged_with_measured(self):
        m = meter()
        m.feed(msg(output_tokens=100_000))
        c = m.final(None)
        self.assertAlmostEqual(c.usd, 1.0)
        self.assertIn("no_final", c.flags)

    def test_no_text_kept(self):
        m = meter()
        m.feed(msg(input_tokens=5))
        c = m.final({"type": "result", "result": SECRET})
        self.assertNotIn(SECRET, repr(c) + repr(vars(m)))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "api_ledger.jsonl"
            lc.write(p, "APILANE3", c, clock=Clock())
            self.assertNotIn(SECRET, p.read_text(encoding="utf-8"))


class EventMatrixTests(unittest.TestCase):
    def test_hostile_counts(self):
        def check(v):
            m = meter()
            m.feed(msg(input_tokens=v, output_tokens=v, cache_read_input_tokens=v))
            u = m.usd()
            self.assertTrue(math.isfinite(u) and u >= 0)
            c = m.final()
            self.assertTrue(all(isinstance(n, int) and 0 <= n <= lc.HUGE for n in c.tokens.values()))
        each(self, HOSTILE_NUMBERS, check)

    def test_flags_for_bad_counts(self):
        for v, flag in ((-5, "negative_count"), (10**400, "huge_count"), (True, "bad_count"),
                        (float("nan"), "bad_count"), ("7", "bad_count")):
            m = meter()
            m.feed(msg(input_tokens=v))
            self.assertIn(flag, m.final().flags, v)

    def test_malformed_events_ignored(self):
        m = meter()
        for e in (None, 3, "x", [], {}, {"type": "assistant"}, {"type": "assistant", "message": 5},
                  {"type": "assistant", "message": {"usage": "x"}}, {"type": "user", "message": {"usage": {}}},
                  {"type": "assistant", "message": {"id": 5, "model": 7, "usage": {"input_tokens": 1}}}):
            m.feed(e)
        self.assertGreaterEqual(m.usd(), 0)
        self.assertIn("bad_event", m.final().flags)

    def test_bad_reported_total_ignored(self):
        for rep in (-1, float("nan"), float("inf"), True, "9", None):
            m = meter()
            m.feed(msg(input_tokens=1_000_000))
            c = m.final({"type": "result", "total_cost_usd": rep})
            self.assertEqual(c.usd, 2.0)
            self.assertIsNone(c.reported_usd)

    def test_bad_cap_is_reached(self):
        for cap in (float("nan"), -1, True, "5", None, float("inf") * 0):
            self.assertTrue(meter(cap=cap).over_cap(), cap)

    def test_events_after_final_and_duplicate_final(self):
        m = meter()
        m.feed(msg(input_tokens=1_000_000))
        c = m.final({"type": "result", "total_cost_usd": 3.0})
        m.feed(msg("m9", output_tokens=10**6))
        m.feed({"type": "result", "total_cost_usd": 99.0})
        self.assertIs(m.final(None), c)
        self.assertEqual(m.usd(), 2.0)
        self.assertIn("late_event", m.flags)
        self.assertIn("duplicate_final", m.flags)


class LedgerTests(unittest.TestCase):
    def test_one_row_broker_readable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "api_ledger.jsonl"
            m = meter(session="sess-1")
            m.feed(msg(input_tokens=1_000_000))
            self.assertTrue(lc.write(p, "APILANE3", m.final(None), clock=Clock(1.8e9)))
            rows = lc.ca.read_ledger(p)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual((r["purpose"], r["slice"], r["session"], r["usd"], r["ts"]),
                             ("overflow", "APILANE3", "sess-1", 2.0, 1.8e9))
            self.assertIn("no_final", r["flags"])

    def test_no_usage_writes_zero_and_flag(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "api_ledger.jsonl"
            lc.write(p, "S", meter().final(None), clock=Clock())
            (r,) = ledger_rows(p)
            self.assertEqual(r["usd"], 0.0)
            self.assertIn("no_usage", r["flags"])

    def test_same_session_written_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "api_ledger.jsonl"
            c = meter(session="s").final(None)
            self.assertTrue(lc.write(p, "S", c, clock=Clock()))
            self.assertFalse(lc.write(p, "S", c, clock=Clock()))
            self.assertFalse(lc.write(p, "S", meter(session="s").final(None), clock=Clock()))
            self.assertEqual(len(ledger_rows(p)), 1)
            self.assertTrue(lc.write(p, "S", meter(session="other").final(None), clock=Clock()))
            self.assertEqual(len(ledger_rows(p)), 2)

    def test_slice_id_scrubbed_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "api_ledger.jsonl"
            lc.write(p, "x" * 500, meter().final(None), clock=Clock())
            self.assertEqual(len(ledger_rows(p)[0]["slice"]), lc.SLICE_MAX)


class StaticTests(unittest.TestCase):
    def test_no_network_credential_or_text_access(self):
        src = _PATH.read_text(encoding="utf-8")
        for bad in ("urllib", "socket", "http", "requests", "read_credential", "https_post", "ca.call",
                    "os.environ", "getenv", "subprocess", "ctypes", "keyring"):
            self.assertNotIn(bad, src, bad)
        self.assertIsNone(re.search(r"""["'](content|text|prompt)["']""", src))


if __name__ == "__main__":
    unittest.main()
