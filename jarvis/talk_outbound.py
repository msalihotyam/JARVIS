"""Outbound pump: send planned mirror actions through injected senders.

Every side effect goes through injected callables, so this module imports no
I/O, network or other talk/telegram module. A send is keyed by
(entry id, view, part); it is recorded "sending" before and "sent"/"failed"
after. A key already "sent" is skipped; a "sending" with no result (a crash
mid-send) is reported "unknown" and never resent here. Never raises.
"""

from collections.abc import Mapping

VIEWS = ("pc", "telegram")
CAPS = {"pc": 4000, "telegram": 4000}  # the mirror planner splits to this size
_STATES = ("sending", "sent", "failed")


def _out(item, result, reason=None):
    out = {"index": item["index"], "entry_id": item["entry_id"], "view": item["view"],
           "part": item["part"], "result": result}
    if reason:
        out["reason"] = reason
    return out


def _parse(index, action):
    """Snapshot one action; key is None when it cannot join a view chain."""
    item = {"index": index, "entry_id": None, "view": None, "part": None,
            "key": None, "snap": None, "reason": None}
    try:
        if not isinstance(action, Mapping):
            item["reason"] = "bad_action"
            return item
        snap = dict(action)
        entry_id, view, part, text = (snap.get(k) for k in ("entry_id", "view", "part", "text"))
        # Exact types: a str/int subclass could lie in __eq__/__hash__ and dodge the dedup key.
        if type(entry_id) not in (int, str):
            item["reason"] = "bad_entry_id"
        elif type(view) is not str or view not in VIEWS:
            item["reason"] = "unknown_view"
        elif type(part) is not int or part < 1:
            item["reason"] = "bad_part"
        else:
            item.update(entry_id=entry_id, view=view, part=part, key=(entry_id, view, part), snap=snap)
            if type(text) is not str:
                item["reason"] = "bad_text"
            elif len(text) > CAPS[view]:
                item["reason"] = "text_over_cap"  # splitting is the mirror planner's job
    except RecursionError:
        item["reason"] = "too_deep"
    except Exception:
        item["reason"] = "bad_action"
    return item


def _prior(was_sent, key):
    """Return (state, attempts, reason); reason is a fixed code, never exception text."""
    try:
        prior = was_sent(*key)
        if prior is None:
            return None, 0, None
        if not isinstance(prior, Mapping):
            return None, 0, "bad_state"
        state, attempts = prior.get("state"), prior.get("attempts")
    except Exception:  # RecursionError and ValueError subclasses included
        return None, 0, "state_unreadable"
    if type(state) is not str or state not in _STATES or type(attempts) is not int or attempts < 0:
        return None, 0, "bad_state"
    return state, attempts, None


def _deliver(item, send, record, was_sent):
    """Return (result dict, stop the view)."""
    if item["reason"]:
        return _out(item, "refused", item["reason"]), False
    if not callable(send):
        return _out(item, "refused", "no_sender"), False
    key = item["key"]
    state, attempts, reason = _prior(was_sent, key)
    if reason:
        return _out(item, "refused", reason), False
    if state == "sent":
        return _out(item, "skipped"), False
    if state == "sending":
        return _out(item, "unknown"), False
    try:
        record(*key, "sending", attempts)
    except Exception:
        return _out(item, "refused", "record_failed"), True
    try:
        reply = send(item["snap"])
        ok = isinstance(reply, Mapping) and reply.get("ok") is True
    except Exception:
        ok = False
    state = "sent" if ok else "failed"
    try:
        record(*key, state, attempts + 1)
    except Exception:
        # The store still says "sending", so a restart reports unknown, never a resend.
        return _out(item, state + "_unrecorded"), True
    return _out(item, state), False


def pump(actions, senders, record, was_sent):
    """Send actions once per (entry id, view, part).

    actions: [{"entry_id", "view", "part" (1-based), "text", ...}]; the whole
    action dict is passed to senders[view], which returns {"ok": True} on
    success (anything else, or raising, is a failure).
    record(entry_id, view, part, state, attempts); was_sent(entry_id, view,
    part) -> None or {"state": "sending"|"sent"|"failed", "attempts": int}.
    Returns {"ok": True, "results": [...]} or {"ok": False, "reason"}. Result
    codes: sent, failed, skipped, duplicate, unknown, refused, blocked (an
    earlier part of the entry did not go), stopped (the view's record broke),
    sent_unrecorded, failed_unrecorded.
    """
    try:
        if (not isinstance(actions, (list, tuple)) or not isinstance(senders, Mapping)
                or not callable(record) or not callable(was_sent)):
            return {"ok": False, "reason": "bad_input"}
        senders = {view: senders.get(view) for view in VIEWS}
        items = [_parse(index, action) for index, action in enumerate(actions)]
    except RecursionError:
        return {"ok": False, "reason": "too_deep"}
    except Exception:
        return {"ok": False, "reason": "bad_input"}
    try:
        return _run(items, senders, record, was_sent)
    except Exception:  # RecursionError included; anything recorded so far stays recorded
        return {"ok": False, "reason": "internal_error"}


def _run(items, senders, record, was_sent):
    results, seen = [], set()
    chains = {view: {} for view in VIEWS}
    for item in items:
        if item["key"] is None:
            results.append(_out(item, "refused", item["reason"]))
        elif item["key"] in seen:
            results.append(_out(item, "duplicate"))
        else:
            seen.add(item["key"])
            chains[item["view"]].setdefault(item["entry_id"], []).append(item)

    for view in VIEWS:  # views run independently: one stopping never touches the other
        stopped = False
        for chain in chains[view].values():
            blocked = False
            for item in sorted(chain, key=lambda i: i["part"]):
                if stopped or blocked:
                    results.append(_out(item, "stopped" if stopped else "blocked"))
                    continue
                out, stopped = _deliver(item, senders[view], record, was_sent)
                results.append(out)
                blocked = out["result"] not in ("sent", "skipped")
    return {"ok": True, "results": results}
