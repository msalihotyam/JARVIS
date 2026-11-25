"""PM33EXIT2: stage-2 exit checker that judges one run from its event log alone.

The claim is "PASS according to the event log". Only ``work_event`` is read, over
the full history; the work_run / work_step / work_lease / notify_outbox snapshots
are never read. Evidence ids are event ``seq`` numbers. No empty-set PASS: every
criterion needs positive evidence events, and a run with no slices never passes.

Criterion -> event evidence (no new event kinds; "no writer yet" means the shape is
defined here but jarvis/work_driver.py emits nothing, so real runs print NOT_YET
"no event evidence"):
  clarify_exit           last readback_ready{turn:int} then a later
                         progress{gate:"clarify_confirm", confirmed:true, turn > readback turn}  (no writer yet)
  no_mid_build_question  run_state{state:"building"}; no questions_ready / readback_ready /
                         plan_ready / approval_required after it or after the first step_state,
                         whichever is earlier (needs_input is allowed)
  every_slice_leased     every slice ran, and every step_state{state:"running"} carries an int
                         "epoch" (the N6 lease fence the driver adds to every payload)
  non_claude_or_no_quota progress{gate:"worker_kind", worker:str not claude*} on a slice, or
                         progress{gate:"no_quota", reason:non-empty str}                         (no writer yet)
  no_scope_violation     no scope gate with passed != true anywhere; each slice's last attempt
                         has progress{gate:"scope", passed:true}
  accepted_with_receipt  each slice's LAST event is step_state accepted at its highest attempt; in
                         that attempt checks + regression passed and the receipt progress{gate:"apply",
                         passed:true, ref:str} follows an apply_intent with the same idempotency_key;
                         that attempt opens with its own running event; the slice has no other
                         terminal step state (any attempt) before the accept; no accepted receipt
                         key or ref was applied (apply passed:true, any attempt) by another slice
                         or the run (format characters dropped, NFKC, trimmed, casefolded)
  notify_points_only     progress{gate:"notify", point:str}, every point a section 2.1 point      (no writer yet)
  no_failed_state        no run_state failed or cancelled, no run_state or done event after a
                         terminal one, no step_state failed_after_cap or slice_failed_after_cap
  pm30_pm32              pm30_green is True and pm32_verdict == "yes"
  log_integrity          seq 1..N contiguous, unique, one run id, every event well-formed
                         (seq <= 2**63-1, step_state attempt an int >= 1)
OVERALL is PASS only when every line is PASS and the last terminal run event is done.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.work_ledger import TERMINAL_STEP_STATES, EventKind  # noqa: E402

KNOWN_KINDS = frozenset(k.value for k in EventKind)
NOTIFY_POINTS = frozenset({
    "questions_ready", "readback_ready", "plan_ready", "needs_input", "blocked:budget",
    "slice_failed_after_cap", "approval_required", "awaiting_acceptance", "done",
    "interrupted", "progress",
})
_QUESTIONS = frozenset({"questions_ready", "readback_ready", "plan_ready", "approval_required"})
_TERMINAL = frozenset({"done", "failed", "cancelled"})
_FIELDS = ("run_id", "seq", "kind", "step_key", "payload_json")
_MAX_SEQ = 2**63 - 1
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# Payload field types per event kind (progress: per gate), from the work_driver writers;
# bool is not int here (type(...) is, not isinstance).
_PASSED = {"passed": bool}
_SHAPES: dict[str, dict[str, type]] = {
    "readback_ready": {"turn": int},
    "step_state": {"state": str, "attempt": int, "epoch": int},
    "clarify_confirm": {"turn": int, "confirmed": bool},
    "scope": _PASSED, "checks": _PASSED, "regression": _PASSED,
    "apply_intent": {"idempotency_key": str},
    "apply": {"passed": bool, "idempotency_key": str, "ref": str},
    "worker_kind": {"worker": str}, "no_quota": {"reason": str}, "notify": {"point": str},
}
# Missing would also be skipped silently: a read-back without a turn, an attempt without a number.
_REQUIRED = {"readback_ready": ("turn",), "step_state": ("state", "attempt")}


def _no_dup(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _parse(raw: object) -> tuple[dict | None, str]:
    """One well-formed event, or None and why. Never raises."""
    try:
        if not isinstance(raw, Mapping):
            return None, "not a mapping"
        for name in _FIELDS:
            if name not in raw:
                return None, "missing " + name
        run_id, seq, kind, step, text = (raw[name] for name in _FIELDS)
        if type(run_id) is not str or not _RUN_ID.fullmatch(run_id):
            return None, "bad run_id"
        if type(seq) is not int or not 1 <= seq <= _MAX_SEQ:  # SQLite INTEGER is 64-bit
            return None, "bad seq"
        if type(kind) is not str or not kind:
            return None, "bad kind"
        if step is not None and type(step) is not str:
            return None, "bad step_key"
        if type(text) is not str:
            return None, "bad payload_json"
        payload = json.loads(text, object_pairs_hook=_no_dup)
        if type(payload) is not dict:
            return None, "payload not an object"
        for key in ("state", "gate"):
            if key in payload and type(payload[key]) is not str:
                return None, "bad " + key
        # A criterion silently skips a field of the wrong type, so a wrong type is malformed here.
        name = payload.get("gate", kind) if kind == EventKind.PROGRESS.value else kind
        for key, want in _SHAPES.get(name, {}).items():
            if (key in payload or key in _REQUIRED.get(name, ())) and type(payload.get(key)) is not want:
                return None, f"bad {name}.{key}"
        if name == "step_state" and payload["attempt"] < 1:
            return None, "bad step_state.attempt"
        return {"run_id": run_id, "seq": seq, "kind": kind, "step": step, "p": payload}, ""
    except Exception as exc:  # malformed input is reported, never raised
        return None, type(exc).__name__


def _ref(raw: object, index: int) -> str:
    try:
        seq = raw["seq"]  # type: ignore[index]
        return str(seq) if type(seq) is int else f"#{index}"
    except Exception:
        return f"#{index}"


def _state(e: dict, kind: str) -> object:
    return e["p"].get("state") if e["kind"] == kind else None


def _gate(e: dict, name: str) -> bool:
    return e["kind"] == EventKind.PROGRESS.value and e["p"].get("gate") == name


def _int(value: object) -> bool:
    return type(value) is int


def _norm(value: str) -> str:
    """One spelling per visible text: format characters (zero-width) dropped, then NFKC, trimmed,
    casefolded (dropping first lets a split combining sequence compose)."""
    text = "".join(c for c in value if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _str(value: object) -> bool:
    return type(value) is str and bool(_norm(value))


def _last_attempt(sl: list[dict]) -> list[dict]:
    """The slice's events from the last running step_state of its highest attempt on
    (empty if that attempt never ran, so an earlier attempt's evidence never counts)."""
    top = max((e["p"]["attempt"] for e in sl if e["kind"] == "step_state"), default=None)
    starts = [i for i, e in enumerate(sl) if _state(e, "step_state") == "running" and e["p"]["attempt"] == top]
    return sl[starts[-1]:] if starts else []


_Verdict = tuple[bool, str, list[int]]
_NONE = "no event evidence"


def _clarify(evs: list[dict], slices: dict) -> _Verdict:
    reads = [e for e in evs if e["kind"] == "readback_ready" and _int(e["p"].get("turn"))]
    confs = [e for e in evs if _gate(e, "clarify_confirm") and e["p"].get("confirmed") is True
             and _int(e["p"].get("turn"))]
    if not reads or not confs:
        return False, _NONE, [e["seq"] for e in reads + confs]
    last = reads[-1]
    for c in confs:
        if c["seq"] > last["seq"] and c["p"]["turn"] > last["p"]["turn"]:
            return True, "", [last["seq"], c["seq"]]
    return False, "read-back not confirmed in a later turn", [last["seq"]] + [c["seq"] for c in confs]


def _mid_build(evs: list[dict], slices: dict) -> _Verdict:
    build = [e for e in evs if _state(e, "run_state") == "building"]
    if not build:
        return False, _NONE, []
    # A slice step is building too, even if run_state building is logged late.
    start = min([build[0]["seq"]] + [e["seq"] for e in evs if e["kind"] == "step_state"])
    asked = [e["seq"] for e in evs if e["seq"] > start and e["kind"] in _QUESTIONS]
    return (False, "question after build start", asked) if asked else (True, "", [build[0]["seq"]])


def _leased(evs: list[dict], slices: dict) -> _Verdict:
    if not slices:
        return False, "no slices", []
    runs = [e for e in evs if _state(e, "step_state") == "running"]
    idle = sorted(k for k, sl in slices.items() if not _last_attempt(sl))
    if idle:
        return False, "slice never ran: " + ",".join(idle), []
    unfenced = [e["seq"] for e in runs if not _int(e["p"].get("epoch"))]
    return (False, "running without a lease epoch", unfenced) if unfenced else (True, "", [e["seq"] for e in runs])


def _worker(evs: list[dict], slices: dict) -> _Verdict:
    hits = [e["seq"] for e in evs if (
        _gate(e, "worker_kind") and e["step"] is not None and _str(e["p"].get("worker"))
        and not _norm(e["p"]["worker"]).startswith("claude")
    ) or (_gate(e, "no_quota") and _str(e["p"].get("reason")))]
    return (True, "", hits) if hits else (False, _NONE, [])


def _scope(evs: list[dict], slices: dict) -> _Verdict:
    if not slices:
        return False, "no slices", []
    bad = [e["seq"] for e in evs if _gate(e, "scope") and e["p"].get("passed") is not True]
    if bad:
        return False, "scope gate violation", bad
    ids, missing = [], []
    for key, sl in sorted(slices.items()):
        ok = [e["seq"] for e in _last_attempt(sl) if _gate(e, "scope") and e["p"].get("passed") is True]
        (ids.extend(ok) if ok else missing.append(key))
    return (False, _NONE + " (scope): " + ",".join(missing), []) if missing else (True, "", ids)


def _slice_receipt(sl: list[dict]) -> tuple[str, str, str, list[int]]:
    """(receipt key, receipt ref, why not accepted, evidence) for one slice; key is "" unless accepted."""
    last = sl[-1]
    attempts = [e["p"].get("attempt") for e in sl if e["kind"] == "step_state"]
    att = last["p"].get("attempt")
    if _state(last, "step_state") != "accepted":
        return "", "", "last event not accepted", [last["seq"]]
    if not _int(att) or any(not _int(a) or a > att for a in attempts):
        return "", "", "accepted attempt is not the highest", [last["seq"]]
    window = _last_attempt(sl)
    if not window or any(e["kind"] == "step_state" and e["p"].get("attempt") != att for e in window):
        return "", "", "accepted attempt has no running event of its own", [last["seq"]]
    # A terminal step state has no way out in the ledger, whatever attempt or place it was logged at.
    ended = [e["seq"] for e in sl[:-1] if _state(e, "step_state") in TERMINAL_STEP_STATES]
    if ended:
        return "", "", "terminal step state before the accept", ended
    for name in ("checks", "regression"):
        gates = [e for e in window if _gate(e, name)]
        if not gates or any(e["p"].get("passed") is not True for e in gates):
            return "", "", name + " not passed", [e["seq"] for e in gates] or [last["seq"]]
    applied = [e for e in window if _gate(e, "apply") and e["p"].get("passed") is True]
    if len(applied) != 1 or not _str(applied[0]["p"].get("ref")) or not _str(applied[0]["p"].get("idempotency_key")):
        return "", "", "no single verifiable receipt", [e["seq"] for e in applied] or [last["seq"]]
    rec = applied[0]
    key = rec["p"]["idempotency_key"]
    intents = [e for e in window if _gate(e, "apply_intent") and e["seq"] < rec["seq"]
               and e["p"].get("idempotency_key") == key]
    if not intents:
        return "", "", "receipt fails verify (no matching apply_intent)", [rec["seq"]]
    return key, rec["p"]["ref"], "", [intents[0]["seq"], rec["seq"], last["seq"]]


def _accepted(evs: list[dict], slices: dict) -> _Verdict:
    if not slices:
        return False, "no slices", []
    # Every receipt really applied (any attempt, any slice, or the run itself) owns its key and ref,
    # so another slice can never claim it; spelling variants count as the same receipt
    # (over-matching only fails closed).
    owners: dict[tuple[str, str], dict[str, list[int]]] = {}
    for e in evs:
        if _gate(e, "apply") and e["p"].get("passed") is True:
            for label, field in (("receipt", "idempotency_key"), ("receipt ref", "ref")):
                if type(e["p"].get(field)) is str:
                    who = owners.setdefault((label, _norm(e["p"][field])), {})
                    who.setdefault(e["step"], []).append(e["seq"])  # None = the run itself, never a slice name
    ids: list[int] = []
    for name, sl in sorted(slices.items()):
        key, ref, why, seqs = _slice_receipt(sl)
        if not key:
            return False, f"{name}: {why}", seqs
        for label, value in (("receipt", key), ("receipt ref", ref)):
            others = {k: v for k, v in owners.get((label, _norm(value)), {}).items() if k != name}
            if others:
                other = sorted(others, key=str)[0]
                by = "the run" if other is None else other
                return False, f"{label} shared by {by} and {name}", sorted(others[other] + seqs)
        ids.extend(seqs)
    return True, "", ids


def _notify(evs: list[dict], slices: dict) -> _Verdict:
    notes = [e for e in evs if _gate(e, "notify")]
    if not notes:
        return False, _NONE, []
    bad = [e["seq"] for e in notes if type(e["p"].get("point")) is not str or e["p"]["point"] not in NOTIFY_POINTS]
    return (False, "notification outside section 2.1 points", bad) if bad else (True, "", [e["seq"] for e in notes])


def _no_failed(evs: list[dict], slices: dict) -> _Verdict:
    if not slices:
        return False, "no slices", []
    runs = [e for e in evs if e["kind"] in ("run_state", EventKind.DONE.value)]
    # A terminal run state (or a done event) has no way out in the ledger, so a later run event
    # means a resumed run, and a later step event means work after the end.
    term = [e["seq"] for e in runs if e["kind"] == EventKind.DONE.value or e["p"].get("state") in _TERMINAL]
    ended = {s for s in term if s != runs[-1]["seq"]}
    ended |= {e["seq"] for e in evs if term and e["seq"] > min(term) and e["step"] is not None}
    bad = sorted(ended | {e["seq"] for e in evs if _state(e, "run_state") in ("failed", "cancelled")
                          or _state(e, "step_state") == "failed_after_cap" or e["kind"] == "slice_failed_after_cap"})
    return (False, "failed state", bad) if bad else (True, "", [])


CRITERIA = (
    ("clarify_exit", _clarify), ("no_mid_build_question", _mid_build), ("every_slice_leased", _leased),
    ("non_claude_or_no_quota", _worker), ("no_scope_violation", _scope),
    ("accepted_with_receipt", _accepted), ("notify_points_only", _notify), ("no_failed_state", _no_failed),
)


def _line(name: str, ok: bool, reason: str, ids: list) -> str:
    tail = " [" + ",".join(str(i) for i in ids) + "]"
    return f"{name}: PASS{tail}" if ok else f"{name}: NOT_YET {reason}{tail}"


def evaluate(events: object, pm30_green: object, pm32_verdict: object) -> list[str]:
    """One line per criterion from the run's events alone; the last line is OVERALL."""
    try:
        raw = list(events)  # type: ignore[call-overload]
    except TypeError:
        raw = []
    good, bad = [], []
    for index, item in enumerate(raw):
        ev, why = _parse(item)
        if ev is None:
            bad.append(f"{_ref(item, index)}:{why}")
        else:
            good.append(ev)
    good.sort(key=lambda e: e["seq"])
    seqs = [e["seq"] for e in good]
    problems, ids = [], list(bad)
    if not raw:
        problems.append("empty log")
    if bad:
        problems.append("malformed events")
    dups = sorted(s for s, n in Counter(seqs).items() if n > 1)
    if dups:
        problems.append("duplicate seq")
        ids += dups
    if not bad and not dups and seqs != list(range(1, len(seqs) + 1)):
        problems.append("seq gap")
        missing, want = [], 1  # walk the sorted seqs: a huge seq must not build a huge range
        for s in seqs:
            missing.extend(range(want, min(s, want + 20 - len(missing))))
            want = s + 1
            if len(missing) >= 20:
                break
        ids += missing
    runs = sorted({e["run_id"] for e in good})
    if len(runs) > 1:
        problems.append("more than one run id: " + ",".join(runs))
        ids += [e["seq"] for e in good if e["run_id"] != good[0]["run_id"]]
    lines = [_line("log_integrity", not problems, "log integrity: " + "; ".join(problems), ids)]
    slices: dict[str, list[dict]] = {}
    for e in good:
        if e["step"] is not None:
            slices.setdefault(e["step"], []).append(e)
    for name, check in CRITERIA:
        try:
            ok, reason, seq_ids = check(good, slices)
        except Exception as exc:  # a checker defect is NOT_YET, never an escape
            ok, reason, seq_ids = False, "checker error " + type(exc).__name__, []
        lines.append(_line(name, ok, reason, seq_ids))
    pm_ok = pm30_green is True and pm32_verdict == "yes"
    lines.append(_line("pm30_pm32", pm_ok, f"pm30_green={pm30_green!r} pm32_verdict={pm32_verdict!r}", []))
    ends = [e for e in good if e["kind"] == EventKind.DONE.value or e["kind"] == "run_state"]
    final = ends[-1] if ends else None
    done = final is not None and (final["kind"] == EventKind.DONE.value or _state(final, "run_state") == "done")
    if final is not None and not done and _state(final, "run_state") not in _TERMINAL:
        # the run moved on after any earlier terminal event; only its last run event counts
        why = "no terminal event"
    else:
        why = "last terminal event is not done"
    lines.append(_line("terminal_done", done, why if final is not None else "no terminal event",
                       [final["seq"]] if final is not None else []))
    unknown = [e["seq"] for e in good if e["kind"] not in KNOWN_KINDS]
    overall = all(line.split(" ", 2)[1] == "PASS" for line in lines)
    lines.append(f"unknown_kinds: {len(unknown)} [" + ",".join(map(str, unknown)) + "]")
    lines.append("OVERALL: " + ("PASS" if overall else "NOT_YET"))
    return lines


def _connect(path: str | Path) -> sqlite3.Connection:
    """Read-only connection; the live operator DB (data/jarvis.db) is refused."""
    p = Path(path).resolve()
    if p.name.lower() == "jarvis.db" and p.parent.name.lower() == "data":
        raise PermissionError("refused: live operator DB " + str(p))
    if not p.is_file():
        raise FileNotFoundError(str(p))
    conn = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_events(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """The run's full event history; names only the work_event table."""
    if type(run_id) is not str or not _RUN_ID.fullmatch(run_id):
        raise LookupError("bad run id")
    rows = conn.execute(
        "SELECT run_id, seq, kind, step_key, payload_json FROM work_event WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    if not rows:
        raise LookupError("unknown run id " + run_id)
    return [dict(zip(_FIELDS, row)) for row in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PM33 stage-2 exit check from the event log alone.")
    ap.add_argument("db")
    ap.add_argument("run_id")
    ap.add_argument("--pm30-green", choices=("yes", "no"), default="no")
    ap.add_argument("--pm32-verdict", default="incomplete")
    args = ap.parse_args(argv)
    try:
        conn = _connect(args.db)
        try:
            events = load_events(conn, args.run_id)
        finally:
            conn.close()
    except (LookupError, OSError, sqlite3.Error) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    lines = evaluate(events, args.pm30_green == "yes", args.pm32_verdict)
    print("\n".join(lines))
    return 0 if lines[-1] == "OVERALL: PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
