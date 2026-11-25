"""Pure delivery planner: what to send again, when, and when to give up.

Nothing here does I/O. Input is plain mappings; output is a plain dict.
Every bad input becomes a typed refusal with an ASCII reason code.
"""

import math
from collections.abc import Mapping

VIEWS = ("pc", "telegram")
STATES = ("pending", "sent", "failed", "gave_up")
DEFAULTS = {"base": 30.0, "cap": 3600.0, "max_attempts": 5, "batch": 100}


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _seq(value):
    return isinstance(value, (list, tuple))


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _num(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError, TypeError):  # int beyond float range (json.loads yields these)
        return False


def _params(request):
    out = {}
    for key, default in DEFAULTS.items():
        value = request.get(key, default)
        if key in ("max_attempts", "batch"):
            ok = _int(value) and value >= 1
        else:
            ok = _num(value) and value > 0
        if not ok:
            raise _Refused("bad_" + key)
        out[key] = value
    if out["cap"] < out["base"]:
        raise _Refused("bad_cap")
    return out


def _resets(request):
    raw = request.get("reset", ())
    if not _seq(raw):
        raise _Refused("bad_reset")
    out = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _Refused("bad_reset")
        entry_id, view = item.get("id"), item.get("view")
        if not isinstance(entry_id, str) or not entry_id or view not in VIEWS:
            raise _Refused("bad_reset")
        out.add((entry_id, view))
    return out


def _record(raw, now):
    if not isinstance(raw, Mapping):
        raise _Refused("bad_record")
    view, state = raw.get("view"), raw.get("state")
    if not isinstance(view, str) or view not in VIEWS:
        raise _Refused("unknown_view")
    if not isinstance(state, str) or state not in STATES:
        raise _Refused("unknown_state")
    seq, attempts, last_try = raw.get("seq"), raw.get("attempts"), raw.get("last_try")
    if "last_try" not in raw:  # a missing field is refused, never read as None
        raise _Refused("bad_time")
    if not _int(seq) or seq < 0:
        raise _Refused("bad_seq")
    if not _int(attempts) or attempts < 0:
        raise _Refused("bad_attempts")
    if last_try is None:
        if attempts > 0:
            raise _Refused("bad_time")
    elif not _num(last_try):
        raise _Refused("bad_time")
    elif last_try > now:
        raise _Refused("future_last_try")
    return view, (seq, state, attempts, last_try)


def _latest(raw_entry, now):
    """Latest record per view by seq; records at that max seq that disagree are a conflict.

    Two passes so the result does not depend on record order.
    """
    records = raw_entry.get("records")
    if not _seq(records):
        raise _Refused("bad_records")
    parsed = [_record(raw, now) for raw in records]
    latest = {}
    for view, rec in parsed:
        held = latest.get(view)
        if held is None or rec[0] > held[0]:
            latest[view] = rec
    for view, rec in parsed:
        if rec[0] == latest[view][0] and rec != latest[view]:
            raise _Refused("conflict")
    return latest


def _entry_plan(entry_id, latest, now, params, resets):
    actions, gave_up = [], []
    for view in VIEWS:
        if view not in latest:
            continue
        _, state, attempts, last_try = latest[view]
        if state == "sent":
            continue
        if state == "gave_up":
            # Only an explicit reset brings a given-up record back.
            if (entry_id, view) in resets:
                actions.append({"id": entry_id, "view": view, "not_before": now})
            continue
        if attempts >= params["max_attempts"]:
            gave_up.append({"id": entry_id, "view": view})
            continue
        not_before = now
        if attempts > 0:
            # Float arithmetic: an int sum past float max would raise OverflowError in isfinite.
            cap, delay = float(params["cap"]), float(params["base"])
            for _ in range(attempts - 1):  # stops at cap: at most ~2100 doublings for finite floats
                if delay >= cap:
                    break
                delay *= 2.0
            not_before = max(now, float(last_try) + min(cap, delay))
            if not math.isfinite(not_before):  # last_try near float max: no real send time
                raise _Refused("bad_time")
        actions.append({"id": entry_id, "view": view, "not_before": not_before})
    return actions, gave_up


def _plan(request):
    if not isinstance(request, Mapping):
        raise _Refused("bad_request")
    now = request.get("now")
    if not _num(now):
        raise _Refused("bad_now")
    params = _params(request)
    resets = _resets(request)
    entries = request.get("entries")
    if not _seq(entries):
        raise _Refused("bad_entries")
    actions, gave_up, refused, seen = [], [], [], set()
    for index, raw_entry in enumerate(entries):
        try:
            if not isinstance(raw_entry, Mapping):
                raise _Refused("bad_entry")
            entry_id = raw_entry.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise _Refused("bad_id")
            if entry_id in seen:
                raise _Refused("duplicate_id")
            seen.add(entry_id)
            latest = _latest(raw_entry, now)
            entry_actions, entry_gave_up = _entry_plan(entry_id, latest, now, params, resets)
        except _Refused as exc:
            refused.append({"index": index, "reason": exc.reason})
            continue
        actions += entry_actions
        gave_up += entry_gave_up
    batch = params["batch"]
    return {
        "ok": True,
        "actions": actions[:batch],
        "more": len(actions) > batch,
        "gave_up": gave_up,
        "refused": refused,
    }


def plan_delivery(request):
    """Plan resends in original entry order.

    request: {"now", "entries": [{"id", "records": [{"view", "state",
    "attempts", "last_try", "seq"}]}], optional "base", "cap",
    "max_attempts", "batch", "reset": [{"id", "view"}]}.
    Returns {"ok": True, "actions", "more", "gave_up", "refused"} or
    {"ok": False, "reason"}; never raises. The caller stores "gave_up"
    for each reported pair, so it is reported once.
    """
    try:
        return _plan(request)
    except _Refused as exc:
        return {"ok": False, "reason": exc.reason}
    except RecursionError:
        return {"ok": False, "reason": "too_deep"}
    except Exception:  # fail closed on hostile mappings
        return {"ok": False, "reason": "bad_input"}
