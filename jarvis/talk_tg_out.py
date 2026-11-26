"""Telegram text out: the phone copy of each reply, sent at most once.

ADR K1/K6 (Salih 2026-11-25), TALKWIRE lesson: the phone copy is a courtesy
view. Per reply, "attempt:<key>" is appended to the delivery log BEFORE the
send, and a reply with an attempt record is never sent again by step(); only
resend(key) tries again, under a numbered attempt. The destination is always
settings["chat_id"]. Every input becomes a typed result; nothing raises.

Ports: reply_lines is the core log's read() (LogLine or raw line dicts);
delivery_log is a raw keyed log (append(key, line) -> "ok" | "exists" |
"failed" | "unknown", read() -> list); client.call(method, body) returns the
telegram client's Ok | Refused | Failed; clock.now() returns seconds.
redact is the TALKREDACT2 content filter, injected (repo guards keep its module
name and the client's out of this file, as in "Telefona gönder"); a missing one
is refused, never defaulted open.
"""

from dataclasses import dataclass

from jarvis import talk_boundary as tb
from jarvis.talk_mirror import plan_mirror
from jarvis.telegram_out import Request, build_request

SENT, FAILED, UNKNOWN, NONE = "sent", "failed", "unknown", "none"
HELD_LINE = "[gizli içerik var; PC'de bak]"  # equals the filter's FIXED_LINE (tested)
VIEW_TEXT = {SENT: "telefon kopyası: gönderildi", FAILED: "telefon kopyası: gönderilemedi",
             UNKNOWN: "telefon kopyası: belirsiz", NONE: ""}
DEFAULT_BUDGET, DEFAULT_CAP = 100, 20
_FATAL = (KeyboardInterrupt, SystemExit)
_CHAT_LIMIT = 2**53
_FIXED = {"refused": tb.REFUSED_TEXT, "failed": tb.FAILED_TEXT}


@dataclass(frozen=True)
class Refusal:
    reason: str
    ok = False


@dataclass(frozen=True)
class StepResult:
    outcomes: tuple  # (reply key, SENT | FAILED | UNKNOWN) per attempt made this step
    skipped: int
    notice: str  # the notice text sent this step, or ""
    pending: int  # replies still without a delivery record after this step
    ok = True


class _Stop(Exception):
    """Out of budget or a port that cannot be trusted: end the step quietly."""


def _settings(settings):
    """(chat_id, budget, cap) or Refusal; enabled is read first."""
    if type(settings) is not dict or settings.get("enabled") is not True:
        return Refusal("disabled" if type(settings) is dict and settings.get("enabled") is False
                       else "bad_settings")
    chat_id = settings.get("chat_id")
    budget = settings.get("budget", DEFAULT_BUDGET)
    cap = settings.get("cap", DEFAULT_CAP)
    if not (type(chat_id) is int and 0 < chat_id < _CHAT_LIMIT and type(budget) is int
            and 8 <= budget <= tb.BUDGET_CAP and type(cap) is int and 1 <= cap <= 10_000):
        return Refusal("bad_settings")
    return chat_id, budget, cap


def _replies(reply_lines):
    """{key: LogLine} of the reply-kind lines, or None when the input is not a sequence."""
    if type(reply_lines) not in (list, tuple):
        return None
    out = {}
    for raw in reply_lines:
        if type(raw) is tb.LogLine:
            raw = {"key": raw.key, "kind": raw.kind, "text": raw.text, "time": raw.time}
        line = tb._line(raw)  # the core's own line check; an "in:" line qualifies only as "refused"
        if line is not None and line.kind in tb.REPLY_KINDS and line.key not in out:
            out[line.key] = line
    return out


class _Run:
    """One step's port access: every delivery-log and client call costs one unit."""

    def __init__(self, delivery_log, client, chat_id, budget, now, redact):
        self.log, self.client, self.chat_id, self.left, self.now = delivery_log, client, chat_id, budget, now
        self.redact = redact

    def _spend(self, need):
        if self.left < need:
            raise _Stop
        self.left -= 1

    def read(self):
        self._spend(1)
        got = _call(self.log.read)
        if type(got) is not list:
            raise _Stop
        return {raw["key"] for raw in got if type(raw) is dict and type(raw.get("key")) is str}

    def mark(self, key, kind, need=1):
        """Append one delivery record (no message text, ever); "ok" | "exists" | "failed" | "unknown"."""
        self._spend(need)
        got = _call(lambda: self.log.append(key, {"key": key, "kind": kind, "text": "", "time": self.now}))
        return got if type(got) is str and got in ("ok", "exists", "failed") else "unknown"

    def send(self, request):
        self._spend(1)
        got = _call(lambda: self.client.call(request.method, request.body))
        name = type(got).__name__  # the client's result classes, by name (its module may not be named here)
        if name == "Ok":
            return SENT
        if name == "Refused" or name == "Failed" and _call(lambda: got.kind) == "http_status":
            return FAILED  # the server or the client said no to this chunk (earlier chunks may have landed)
        return UNKNOWN  # timeout, broken response, raised: it may have reached the phone


def _call(fn):
    try:
        return fn()
    except _FATAL:
        raise
    except BaseException:
        return None


def _requests(text, key, chat_id, redact):
    """The send requests for one copy (pure), or None when the copy cannot be built.

    A flagged or unscannable text becomes HELD_LINE alone: no fragment of it leaves.
    """
    clean = getattr(_call(lambda: redact(text)), "flagged", None) is False
    entry = {"id": key, "source": "pc", "who": "jarvis", "kind": "text", "needs_you": False,
             "text": text if clean else HELD_LINE, "delivered": {"pc": True}}
    actions = _call(lambda: plan_mirror(entry))
    if type(actions) is not list or not actions:
        return None
    out = tuple(build_request({"chat_id": chat_id, "kind": "text", "text": action.get("text"), "silent": True},
                              chat_id) for action in actions)
    return out if all(type(request) is Request for request in out) else None


def _copy(line, run):
    return _requests(_FIXED.get(line.kind, line.text), line.key, run.chat_id, run.redact)


def _attempt(run, suffix, requests):
    """Attempt record first, then the send, then the outcome. None when the record already existed."""
    got = run.mark("attempt:" + suffix, "attempt", 2 + len(requests or ()))
    if got == "exists":
        return None
    if got != "ok":
        raise _Stop  # no attempt record we can count on: nothing is sent
    if requests is None:
        run.mark("failed:" + suffix, FAILED)
        return FAILED
    result = SENT
    for request in requests:
        result = run.send(request)
        if result != SENT:
            break
    if result != UNKNOWN:
        run.mark(result + ":" + suffix, result)
    return result


def _numbers(key, keys):
    """Attempt numbers recorded for one reply key."""
    base = "attempt:" + key
    nums = [1] if base in keys else []
    for k in keys:
        if k.startswith(base + "#") and k[len(base) + 1:].isdigit() and k[len(base) + 1:].isascii():
            nums.append(int(k[len(base) + 1:]))
    return nums


def _guarded(fn, *args):
    try:
        return fn(*args)
    except _FATAL:
        raise
    except BaseException:
        return Refusal("internal_error")


def _open(reply_lines, delivery_log, client, clock, settings, redact):
    """(replies, run, keys, cap) or Refusal. Settings first: disabled touches no port."""
    got = _settings(settings)
    if type(got) is Refusal:
        return got
    chat_id, budget, cap = got
    if not callable(redact):
        return Refusal("bad_redact")
    replies = _replies(reply_lines)
    if replies is None:
        return Refusal("bad_reply_lines")
    tick = tb.Clock(clock).now()
    if type(tick) is not tb.Tick:
        return Refusal("clock_unknown")
    run = _Run(delivery_log, client, chat_id, budget, tick.now, redact)
    try:
        return replies, run, run.read(), cap
    except _Stop:
        return Refusal("log_unknown")


def step(reply_lines, delivery_log, client, clock, settings, *, redact=None):
    """Send the phone copy of each reply without a delivery record, oldest first, within budget."""
    return _guarded(_step, reply_lines, delivery_log, client, clock, settings, redact)


def _step(reply_lines, delivery_log, client, clock, settings, redact):
    got = _open(reply_lines, delivery_log, client, clock, settings, redact)
    if type(got) is Refusal:
        return got
    replies, run, keys, cap = got
    order = sorted((line.time, k) for k, line in replies.items()
                   if "attempt:" + k not in keys and "skipped:" + k not in keys)
    # A notice covers the replies read up to its anchor (log position, not time: a late write can
    # carry an old time); those are only marked skipped.
    pos = {k: i for i, k in enumerate(replies)}
    mark = max((pos[k[15:]] for k in keys if k.startswith("attempt:notice:") and k[15:] in pos), default=-1)
    covered = [k for _, k in order if pos[k] <= mark]
    fresh = [k for _, k in order if pos[k] > mark]
    outcomes, skipped, done, notice = [], 0, 0, ""
    try:
        if len(fresh) > cap:
            text = f"{len(order)} cevap PC'de bekliyor"
            anchor = max(fresh, key=pos.get)
            result = _attempt(run, "notice:" + anchor, _requests(text, "notice", run.chat_id, run.redact))
            notice = text if result is not None else ""
            if result is not None:
                outcomes.append(("notice:" + anchor, result))
            covered, fresh = covered + fresh, []
        for k in covered:
            if run.mark("skipped:" + k, "skipped") not in ("ok", "exists"):
                raise _Stop
            skipped, done = skipped + 1, done + 1
        for k in fresh:
            result = _attempt(run, k, _copy(replies[k], run))
            done += 1
            if result is not None:
                outcomes.append((k, result))
    except _Stop:
        pass
    return StepResult(tuple(outcomes), skipped, notice, len(order) - done)


def resend(key, reply_lines, delivery_log, client, clock, settings, *, redact=None):
    """The only way to try a reply again: a numbered new attempt, same order as step()."""
    return _guarded(_resend, key, reply_lines, delivery_log, client, clock, settings, redact)


def _resend(key, reply_lines, delivery_log, client, clock, settings, redact):
    got = _open(reply_lines, delivery_log, client, clock, settings, redact)
    if type(got) is Refusal:
        return got
    replies, run, keys, _ = got
    if type(key) is not str or key not in replies:
        return Refusal("no_such_reply")
    n = max(_numbers(key, keys), default=0) + 1
    try:
        result = _attempt(run, key if n == 1 else f"{key}#{n}", _copy(replies[key], run))
    except _Stop:
        return Refusal("log_unknown")
    if result is None:
        return Refusal("attempt_exists")
    return StepResult(((key, result),), 0, "", 0)


def status(key, delivery_log):
    """sent | failed | unknown | none for the PC view (VIEW_TEXT); never raises."""
    got = _call(lambda: delivery_log.read()) if type(key) is str else None
    if type(got) is not list:
        return UNKNOWN
    keys = {raw["key"] for raw in got if type(raw) is dict and type(raw.get("key")) is str}
    nums = _numbers(key, keys)
    if not nums:
        return FAILED if "skipped:" + key in keys else NONE  # skipped after an outage: never sent
    top = max(nums)
    suffix = key if top == 1 else f"{key}#{top}"
    return SENT if "sent:" + suffix in keys else FAILED if "failed:" + suffix in keys else UNKNOWN
