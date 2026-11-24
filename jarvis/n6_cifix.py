"""N6CIFIX1: may one fix chain start for an INT after its own CI run went red? SHADOW ONLY.

``decide(verdict, read_ledger, known_red)`` turns one typed red CI verdict plus the ledger rows
(read through the injected ``read_ledger`` callable, the only I/O) into a ``Result``:
"propose" with a proposal record, "refused" with a reason code from ``REASONS``, or
"decision_needed" once ``FIX_CAP`` fixes of the INT's one chain have failed (the cap decision
is ``work_supervisor.fix_chain``; no third fix ever). Nothing starts a lane, writes a prompt or
touches a key: the proposal says "no key1, cannot start a chain". Nothing in the app imports
this module. Any doubt (malformed, unknown or ambiguous input, any exception) is a refusal.

Verdict (dict): int, sha (40 lowercase hex), failing (list of test ids), source ("own_ci_run"),
ci ("red" | "api_error" | "missing_workflow" | "base_red"), requester (lane name).
Ledger rows (dicts; every row must be one of these, anything else refuses):
  {"event": "int_candidate", "int", "sha"}           the INT's candidate SHA
  {"event": "fix_proposed", "int", "sha", "failing", "chain"}   a record this module emitted
  {"event": "fix_lane", "int", "lane"}                a fix lane started for the INT's chain
  {"event": "fix_failed", "int", "lane"}              one failed fix of the INT's chain
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any, NamedTuple

from jarvis.work_supervisor import FIX_CAP, fix_chain

NOTE = "no key1, cannot start a chain"
CHAIN = 1  # one chain per INT name, ever
MAX_IDS = 200
REASONS = (
    "proposed", "fix_cap_reached", "bad_verdict", "bad_int", "bad_sha", "not_own_ci_run",
    "no_failing_ids", "bad_failing_ids", "too_many_ids", "wait_ci_api_error",
    "wait_missing_workflow", "wait_base_red", "bad_requester", "requester_is_fix_lane",
    "ledger_unreadable", "ledger_ambiguous", "no_candidate", "sha_mismatch", "only_known_red",
    "chain_exists", "internal_error",
)
_SHA = re.compile(r"[0-9a-f]{40}")
_INT_KEY = re.compile(r"int[a-z0-9_-]{1,60}")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_TEST_ID = re.compile(r"tests\.test_[a-z0-9_]{1,80}(?:\.[A-Z][A-Za-z0-9_]{0,80}(?:\.test[A-Za-z0-9_]{0,80})?)?")
_PROTECTED_TAIL = frozenset({"db", "sqlite", "sqlite3", "wal", "shm", "journal"})
_WAIT = {"api_error": "wait_ci_api_error", "missing_workflow": "wait_missing_workflow",
         "base_red": "wait_base_red"}
_FIELDS = {"int_candidate": ("int", "sha"), "fix_proposed": ("int", "sha", "failing", "chain"),
           "fix_lane": ("int", "lane"), "fix_failed": ("int", "lane")}
_VERDICT = frozenset({"int", "sha", "failing", "source", "ci", "requester"})


class Result(NamedTuple):
    outcome: str        # "propose" | "refused" | "decision_needed"
    reason: str         # one of REASONS
    proposal: dict[str, Any] | None = None
    record: dict[str, Any] | None = None   # row to append; None when nothing new to write


class _Refuse(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _refused(reason: str) -> Result:
    return Result("refused", reason)


def int_key(raw: object) -> str | None:
    """Raw ASCII check first (NFKC would fold look-alikes), then NFKC + casefold + strip."""
    if type(raw) is not str or not raw.isascii() or not raw.isprintable():
        return None
    key = unicodedata.normalize("NFKC", raw).casefold().strip().rstrip(" .")
    return key if _INT_KEY.fullmatch(key) else None


def _sha(raw: object) -> str | None:
    return raw if type(raw) is str and _SHA.fullmatch(raw) else None


def _ids(raw: object) -> tuple[str, ...]:
    """Sorted unique test ids; any malformed or protected-looking id refuses the whole list."""
    if type(raw) not in (list, tuple):
        raise _Refuse("bad_failing_ids")
    if len(raw) > MAX_IDS * 4:
        raise _Refuse("too_many_ids")
    ids: set[str] = set()
    for item in raw:
        if type(item) is not str or not _TEST_ID.fullmatch(item):
            raise _Refuse("bad_failing_ids")
        if item.rsplit(".", 1)[-1].casefold() in _PROTECTED_TAIL:
            raise _Refuse("bad_failing_ids")
        ids.add(item)
    if not ids:
        raise _Refuse("no_failing_ids")
    if len(ids) > MAX_IDS:
        raise _Refuse("too_many_ids")
    return tuple(sorted(ids))


def _field(field: str, value: object) -> object:
    if field == "sha":
        return _sha(value)
    if field == "lane":
        return value if type(value) is str and _LANE.fullmatch(value) else None
    if field == "chain":
        return value if type(value) is int and value == CHAIN else None
    try:
        return _ids(value)
    except _Refuse:
        return None


def _ledger(read_ledger: Callable[[], object]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every row must parse; one malformed or unknown row refuses the whole ledger."""
    try:
        raw = read_ledger()
        if type(raw) not in (list, tuple):
            raise _Refuse("ledger_unreadable")
        rows = list(raw)  # type: ignore[call-overload]
    except _Refuse:
        raise
    except Exception:  # noqa: BLE001 - any reader failure is a typed refusal (fail closed)
        raise _Refuse("ledger_unreadable") from None
    parsed: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        if type(row) is not dict:
            raise _Refuse("ledger_ambiguous")
        event = row.get("event")
        if type(event) is not str or event not in _FIELDS:
            raise _Refuse("ledger_ambiguous")
        if set(row) != {"event", *_FIELDS[event]}:
            raise _Refuse("ledger_ambiguous")
        key = int_key(row["int"])
        if key is None:
            raise _Refuse("ledger_ambiguous")
        values: dict[str, Any] = {}
        for field in _FIELDS[event][1:]:
            values[field] = _field(field, row[field])
            if values[field] is None:
                raise _Refuse("ledger_ambiguous")
        parsed.append((event, key, values))
    return parsed


def _known(raw: object) -> frozenset[str]:
    if type(raw) not in (list, tuple, set, frozenset):
        raise _Refuse("bad_verdict")
    items = list(raw)  # type: ignore[call-overload]
    if any(type(k) is not str or not _TEST_ID.fullmatch(k) for k in items):
        raise _Refuse("bad_verdict")
    return frozenset(items)


def _decide(verdict: object, read_ledger: Callable[[], object], known_red: object) -> Result:
    if type(verdict) is not dict or set(verdict) != _VERDICT:
        raise _Refuse("bad_verdict")
    key = int_key(verdict["int"])
    if key is None:
        raise _Refuse("bad_int")
    ci = verdict["ci"]
    if type(ci) is not str or (ci != "red" and ci not in _WAIT):
        raise _Refuse("bad_verdict")
    if ci != "red":
        raise _Refuse(_WAIT[ci])  # CI could not judge: wait, not fix
    if type(verdict["source"]) is not str or verdict["source"] != "own_ci_run":
        raise _Refuse("not_own_ci_run")
    sha = _sha(verdict["sha"])
    if sha is None:
        raise _Refuse("bad_sha")
    ids = _ids(verdict["failing"])
    requester = verdict["requester"]
    if type(requester) is not str or not _LANE.fullmatch(requester):
        raise _Refuse("bad_requester")
    if "fix" in requester.casefold():
        raise _Refuse("requester_is_fix_lane")
    known = _known(known_red)

    rows = _ledger(read_ledger)
    lanes = {val["lane"].casefold() for e, _, val in rows if "lane" in val}
    if requester.casefold() in lanes:
        raise _Refuse("requester_is_fix_lane")
    mine = [(e, val) for e, k, val in rows if k == key]
    candidates = {val["sha"] for e, val in mine if e == "int_candidate"}
    proposals = {(val["sha"], val["failing"]) for e, val in mine if e == "fix_proposed"}
    fixes = [e for e, _ in mine if e in ("fix_lane", "fix_failed")]
    failed = fixes.count("fix_failed")  # every row counts; duplicates never shrink it
    if len(candidates) > 1 or len(proposals) > 1:
        raise _Refuse("ledger_ambiguous")
    if fixes and not proposals:
        raise _Refuse("ledger_ambiguous")  # a fix without its chain's proposal row
    if not candidates:
        raise _Refuse("no_candidate")
    if sha not in candidates:
        raise _Refuse("sha_mismatch")
    red = tuple(i for i in ids if not any(i == k or i.startswith(k + ".") for k in known))
    if not red:
        raise _Refuse("only_known_red")

    if proposals and fix_chain(key, failed, FIX_CAP).action == "ask_salih":
        return Result("decision_needed", "fix_cap_reached")  # never a third fix
    name = verdict["int"].strip().rstrip(" .")
    proposal = {"int": name, "sha": sha, "failing": list(red), "chain": CHAIN, "note": NOTE}
    if proposals:
        if fixes or proposals != {(sha, red)}:
            raise _Refuse("chain_exists")  # one chain per INT name, ever
        return Result("propose", "proposed", proposal, None)  # restart: row already written
    record = {"event": "fix_proposed", "int": name, "sha": sha, "failing": list(red), "chain": CHAIN}
    return Result("propose", "proposed", proposal, record)


def decide(verdict: object, read_ledger: Callable[[], object], known_red: object = ()) -> Result:
    """Never raises an Exception: every failure is a typed refusal."""
    try:
        return _decide(verdict, read_ledger, known_red)
    except _Refuse as refusal:
        return _refused(refusal.reason)
    except Exception:  # noqa: BLE001 - RecursionError, TypeError, ...: any doubt, no chain
        return _refused("internal_error")
