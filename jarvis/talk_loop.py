"""Service loop scheduler: from the current state, the next step and the wait.

Pure module (ON specs-08/11): values in, frozen values out. No I/O, no
network, no clock; the caller passes `now`. Nothing in the running app imports
it yet. Every public call returns a Refusal instead of raising (fail closed).

The caller keeps one LoopState per start (LoopState() at start) and passes the
state from each Step into the next call; that is how RECOVER stays once per
start and how the inbound fairness run is counted without hidden memory.

Order: STOP (shutdown, no turn running) > RECOVER (once per start) >
HANDLE_INBOUND (no turn running, no shutdown) > DELIVER when due > POLL when
due (not during shutdown) > IDLE(seconds). After FAIRNESS_LIMIT inbound steps
in a row while a delivery or poll was due, that due item gets the next step
(DELIVER outranks POLL there too, so a delivery that stays due every pass can
hold POLL back; the caller clears or reschedules a delivery once it runs).

Times are seconds on one monotonic scale, int or float in [0, 2**53] (anything
else is refused, so int and float may be mixed exactly); a due time of None =
nothing scheduled. poll_due_at is the due time at the base POLL_INTERVAL; a transport
failure streak of n moves it later so the interval is
min(POLL_INTERVAL * 2**n, POLL_MAX_INTERVAL). Inbound is never delayed by it.
"""

from __future__ import annotations

from dataclasses import dataclass

STOP = "STOP"
RECOVER = "RECOVER"
HANDLE_INBOUND = "HANDLE_INBOUND"
DELIVER = "DELIVER"
POLL = "POLL"
IDLE = "IDLE"

MIN_WAIT = 0.5  # seconds; IDLE never waits less, so past due times cannot spin
MAX_WAIT = 30.0
POLL_INTERVAL = 5.0
POLL_MAX_INTERVAL = 300.0
FAIRNESS_LIMIT = 8
MAX_COUNT = 2**31 - 1  # larger counts are corrupt input, refused
MAX_TIME = 2**53  # every int up to here is an exact float, so mixed int/float times cannot round

REFUSALS = {
    "STATE_INVALID": "state must be a LoopState from this module",
    "FLAG_INVALID": "turn, recover and shutdown flags must be bool",
    "COUNT_INVALID": "inbound count and failure streak must be int in [0, 2**31 - 1]",
    "TIME_INVALID": "now and due times must be int or float in [0, 2**53] (due may be None)",
    "INPUT_TOO_DEEP": "input nested too deep",
    "INPUT_INVALID": "input could not be read",
}


@dataclass(frozen=True)
class Refusal:
    code: str
    text: str


@dataclass(frozen=True)
class LoopState:
    recovered: bool = False  # RECOVER already returned this start
    inbound_run: int = 0  # HANDLE_INBOUND steps in a row while other work was due


@dataclass(frozen=True)
class Step:
    kind: str
    seconds: float  # wait for IDLE, 0.0 otherwise
    state: LoopState


def _refuse(code):
    return Refusal(code, REFUSALS[code])


def _guarded(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RecursionError:
            return _refuse("INPUT_TOO_DEEP")
        except Exception:  # fail closed: no exception escapes a public call
            return _refuse("INPUT_INVALID")

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _time(value):
    # the range check also refuses nan and +-inf; int-float compares are exact in Python
    return type(value) in (int, float) and 0 <= value <= MAX_TIME


@_guarded
def poll_interval(failure_streak):
    """Seconds between polls after `failure_streak` transport failures in a row."""
    if not _count(failure_streak):
        return _refuse("COUNT_INVALID")
    # 2**12 * 5 s already passes the cap; clamping n keeps huge ints cheap
    return min(POLL_INTERVAL * 2 ** min(failure_streak, 12), POLL_MAX_INTERVAL)


@_guarded
def next_step(state, *, inbound_waiting, turn_in_progress, delivery_due_at,
              poll_due_at, recover_needed, shutdown_requested, failure_streak, now):
    """Step(kind, seconds, state) for this loop pass, or a Refusal."""
    if type(state) is not LoopState or type(state.recovered) is not bool \
            or not _count(state.inbound_run):
        return _refuse("STATE_INVALID")
    if any(type(f) is not bool for f in (turn_in_progress, recover_needed, shutdown_requested)):
        return _refuse("FLAG_INVALID")
    if not (_count(inbound_waiting) and _count(failure_streak)):
        return _refuse("COUNT_INVALID")
    if not _time(now) or any(d is not None and not _time(d) for d in (delivery_due_at, poll_due_at)):
        return _refuse("TIME_INVALID")

    if shutdown_requested and not turn_in_progress:
        return Step(STOP, 0.0, state)
    if recover_needed and not state.recovered:
        return Step(RECOVER, 0.0, LoopState(True, 0))

    # checked above: every time is <= 2**53, so float() is exact and no int-float mix rounds
    now = float(now)
    delivery_due_at = None if delivery_due_at is None else float(delivery_due_at)
    poll_due_at = None if poll_due_at is None else float(poll_due_at)
    backoff = poll_interval(failure_streak) - POLL_INTERVAL
    # work in offsets from now: due + backoff rounds the backoff away at large timestamps
    deliver_left = None if delivery_due_at is None else delivery_due_at - now
    poll_left = None if poll_due_at is None else poll_due_at - now + backoff
    deliver_due = deliver_left is not None and delivery_due_at <= now
    # no new inbound is fetched during shutdown; the poll time still paces IDLE
    poll_due = (poll_due_at is not None and not shutdown_requested
                and poll_due_at - now <= -backoff)

    if inbound_waiting and not turn_in_progress and not shutdown_requested:
        other_due = deliver_due or poll_due
        if not other_due:
            return Step(HANDLE_INBOUND, 0.0, LoopState(state.recovered, 0))
        if state.inbound_run < FAIRNESS_LIMIT:
            return Step(HANDLE_INBOUND, 0.0, LoopState(state.recovered, state.inbound_run + 1))
    fresh = LoopState(state.recovered, 0)
    if deliver_due:
        return Step(DELIVER, 0.0, fresh)
    if poll_due:
        return Step(POLL, 0.0, fresh)
    lefts = [d for d in (deliver_left, poll_left) if d is not None]
    wait = min(lefts) if lefts else MAX_WAIT
    return Step(IDLE, float(min(max(wait, MIN_WAIT), MAX_WAIT)), fresh)
