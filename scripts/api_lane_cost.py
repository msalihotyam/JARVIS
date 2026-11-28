"""APILANE3: what an overflow (API) lane session costs, while it runs and after it ends.

Meter(prices, cap) is fed the headless session's stream events (the "assistant" message usage and
the final "result"), keeps a running USD total and says over_cap() once the per-slice cap is
reached. final() gives a Cost; write() books it once per session in the broker's ledger under
purpose "overflow". Only counts are read: never message content, never a key, never the network.
Stdlib + the broker (scripts/conveyor_api.py: PRICES, ledger reader, lock and appender).
"""
import importlib.util
import math
import secrets
import time
from collections import namedtuple
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("conveyor_api", Path(__file__).with_name("conveyor_api.py"))
ca = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ca)

PURPOSE = "overflow"
KINDS = ("in", "out", "cache_read", "cache_write")
# the stream's usage field for each kind (same names scripts/usage_ledger.py reads)
FIELDS = {"in": "input_tokens", "out": "output_tokens",
          "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}
CACHE_READ_X, CACHE_WRITE_X = 0.1, 1.25  # cache rates as multiples of the input rate (API pricing)
HUGE = 10**8  # tokens per message; above this a count is clamped (and flagged), never trusted
SLICE_MAX = 80

Cost = namedtuple("Cost", "usd tokens model seconds measured_usd reported_usd session flags")


def rates(prices, model):
    """USD per token for each kind, and whether the model was unknown (then: the dearest known rate)."""
    known = isinstance(model, str) and model in prices and "fable" not in model.lower()
    p_in, p_out = prices[model] if known else (max(p[0] for p in prices.values()),
                                               max(p[1] for p in prices.values()))
    return {"in": p_in / 1e6, "out": p_out / 1e6, "cache_read": p_in * CACHE_READ_X / 1e6,
            "cache_write": p_in * CACHE_WRITE_X / 1e6}, not known


def _count(x, flags):
    """A token count as a non-negative int; anything else is 0 or clamped, and flagged."""
    if x is None:
        return 0
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        flags.add("bad_count")
        return 0
    if isinstance(x, float) and not math.isfinite(x):
        flags.add("bad_count")
        return HUGE if x > 0 else 0
    if x < 0:
        flags.add("negative_count")
        return 0
    if x > HUGE:
        flags.add("huge_count")
        return HUGE
    return int(math.ceil(x))


def _money(x):
    """A USD amount as a finite non-negative float, else None (bool, non-number, NaN/inf, negative, huge int)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        x = float(x)
    except OverflowError:  # an int past float range (10**400) cannot be an honest amount
        return None
    return x + 0.0 if math.isfinite(x) and x >= 0 else None  # + 0.0: -0.0 becomes 0.0


class Meter:
    def __init__(self, prices=None, cap=0.0, *, session=None, clock=time.monotonic):
        self.prices = dict(ca.PRICES if prices is None else prices)
        self.flags = set()
        self.cap = _money(cap)
        if self.cap is None:
            self.flags.add("bad_cap")
            self.cap = 0.0  # a bad cap is already reached (fail closed)
        self.session = session if isinstance(session, str) and session else None
        self.clock, self.t0 = clock, clock()
        self.msgs = {}  # message id -> (model, {kind: count}, usd)
        self.total = 0.0
        self.n = 0
        self.cost = None

    def feed(self, event):
        """One stream event; anything malformed is ignored or clamped and flagged, never raised."""
        try:
            self._feed(event)
        except Exception:  # a parser bug must not stop the metering of later events
            self.flags.add("bad_event")

    def _feed(self, event):
        if self.cost is not None:
            self.flags.add("late_event")
            return
        if not isinstance(event, dict):
            self.flags.add("bad_event")
            return
        self._take_session(event)
        if event.get("type") == "result":
            self.final(event)
            return
        msg = event.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if event.get("type") != "assistant" or not isinstance(usage, dict):
            return
        model = msg.get("model")
        model = model[:SLICE_MAX] if isinstance(model, str) else "?"
        mid = msg.get("id")
        self.n += 1
        key = mid if isinstance(mid, str) and mid else ("#", self.n)  # no id: counted on its own
        counts = {k: _count(usage.get(f), self.flags) for k, f in FIELDS.items()}
        old = self.msgs.get(key)
        if old:  # the stream repeats a message's usage per block: keep the largest of each count
            counts = {k: max(counts[k], old[1][k]) for k in KINDS}
        r, unknown = rates(self.prices, model)
        if unknown:
            self.flags.add("unknown_model")
        usd = sum(counts[k] * r[k] for k in KINDS)
        self.total += usd - (old[2] if old else 0.0)
        self.msgs[key] = (model, counts, usd)

    def _take_session(self, event):
        sid = event.get("session_id")
        if self.session is None and isinstance(sid, str) and sid:
            self.session = sid[:SLICE_MAX]

    def usd(self):
        return max(self.total, 0.0)

    def over_cap(self):
        # after final, the charge (max of measured and reported) is what counts against the cap
        return max(self.usd(), self.cost.usd if self.cost else 0.0) >= self.cap

    def final(self, result=None):
        """The session's Cost; result = its "result" event or None (killed / crashed). Idempotent."""
        if self.cost is not None:
            self.flags.add("duplicate_final")
            return self.cost
        if isinstance(result, dict):
            self._take_session(result)
        raw = result.get("total_cost_usd") if isinstance(result, dict) else None
        rep = _money(raw)
        if raw is not None and rep is None:
            self.flags.add("bad_reported")
        if result is None:
            self.flags.add("no_final")
        if not self.msgs:
            self.flags.add("no_usage")
        tokens = {k: sum(m[1][k] for m in self.msgs.values()) for k in KINDS}
        measured = round(self.usd(), 6)
        try:
            seconds = max(round(float(self.clock() - self.t0), 1), 0.0)
        except Exception:
            seconds = 0.0
        self.cost = Cost(max(measured, rep or 0.0), tokens, ",".join(sorted({m[0] for m in self.msgs.values()})),
                         seconds, measured, rep, self.session or secrets.token_hex(8), tuple(sorted(self.flags)))
        return self.cost


def write(ledger, slice_id, cost, *, clock=time.time):
    """Append the session's one overflow row to the broker ledger; False when it is already there."""
    ledger = Path(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ca._locked(ledger.with_name(ledger.name + ".lock")):
        try:
            rows = ca.read_ledger(ledger)
        except ca.ApiRefused:
            rows = []  # unreadable ledger: a second row overcharges, a missing one would undercount
        if any(r.get("status") == PURPOSE and r.get("session") == cost.session for r in rows):
            return False
        ca._append(ledger, {"ts": clock(), "purpose": PURPOSE, "status": PURPOSE,
                            "slice": ca.scrub(str(slice_id))[:SLICE_MAX], "session": cost.session,
                            "model": cost.model, **cost.tokens, "usd": cost.usd,
                            "measured_usd": cost.measured_usd, "reported_usd": cost.reported_usd,
                            "seconds": cost.seconds, "flags": list(cost.flags)})
    return True
