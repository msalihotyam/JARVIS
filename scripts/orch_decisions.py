"""DECISIONID1: the decision-id registry (Gate B item B1d).

build/orch/decisions.json (gitignored) holds a JSON list of records:
  {"id": "D-YYYYMMDD-HHMM-<SLUG>", "class": authority|security|cost|irreversible|routine,
   "state": active|inactive|superseded, "superseded_by": <id>|null,
   "source": "KARARLAR 2026-11-23 01:32" (verbatim quote ref), "at": <iso>}
A missing, unreadable or malformed file (one bad record, a duplicate id or key) is UNKNOWN, and
UNKNOWN refuses everything: nothing is ever "allowed by default". No provisional state (B6 was
removed): when Fable/Salih has not decided, the action waits. Reading never creates the file.
An id is written once; only its state moves (active -> inactive | superseded).
"""
import json
import os
import re
import secrets
import time
from pathlib import Path

REL = Path("build") / "orch" / "decisions.json"
DEFAULT = Path(__file__).resolve().parents[1] / REL
ID_RE = re.compile(r"D-[0-9]{8}-[0-9]{4}-[A-Z0-9][A-Z0-9_-]{0,63}")  # fullmatch: ascii, one case
CLASSES = ("authority", "security", "cost", "irreversible", "routine")
STATES = ("active", "inactive", "superseded")
KEYS = {"id", "class", "state", "superseded_by", "source", "at"}
LOCK_TRIES, LOCK_GAP_S = 500, 0.01
REPLACE_TRIES, REPLACE_GAP_S = 5, 0.05


class Unknown:
    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = Unknown()


class Malformed(ValueError):
    pass


def _no_dup_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise Malformed(f"duplicate key {k!r}")
        out[k] = v
    return out


def _check(recs):
    """recs: the parsed list -> {id: record}; raises Malformed on anything off."""
    if not isinstance(recs, list):
        raise Malformed("not a list")
    out = {}
    for r in recs:
        if not isinstance(r, dict) or set(r) != KEYS:
            raise Malformed(f"bad record {r!r}"[:200])
        i = r["id"]
        if not isinstance(i, str) or not ID_RE.fullmatch(i):
            raise Malformed(f"bad id {i!r}")
        if i in out:
            raise Malformed(f"duplicate id {i}")
        if r["class"] not in CLASSES or r["state"] not in STATES:
            raise Malformed(f"{i}: bad class or state")
        if not all(isinstance(r[k], str) and r[k].strip() for k in ("source", "at")):
            raise Malformed(f"{i}: source and at must be non-empty text")
        if (r["state"] == "superseded") != (r["superseded_by"] is not None):
            raise Malformed(f"{i}: superseded_by goes with state superseded only")
        out[i] = r
    for i, r in out.items():
        s = r["superseded_by"]
        if s is not None and (s == i or s not in out):
            raise Malformed(f"{i}: superseded_by {s!r} is not another recorded id")
    return out


def _read(path):
    """{id: record}, {} when the file is missing; raises Malformed / OSError otherwise."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    try:
        return _check(json.loads(text, object_pairs_hook=_no_dup_keys))
    except (ValueError, RecursionError) as e:
        raise Malformed(f"{e}") from e


def load(path=DEFAULT):
    """{id: record}, or UNKNOWN when the file is missing, unreadable or malformed (fail closed)."""
    if not Path(path).exists():
        return UNKNOWN
    try:
        return _read(path)
    except (Malformed, OSError):  # a read racing a Windows replace lands here too: refused, not allowed
        return UNKNOWN


def get(decision_id, path=DEFAULT):
    recs = load(path)
    return None if recs is UNKNOWN else recs.get(decision_id)


def require_active(decision_id, path=DEFAULT):
    """(ok, reason): only an active, recorded id in a well-formed registry passes."""
    if not isinstance(decision_id, str) or not ID_RE.fullmatch(decision_id):
        return False, f"malformed decision id {decision_id!r}"[:200]
    recs = load(path)
    if recs is UNKNOWN:
        return False, f"decision registry missing or malformed: {decision_id} refused"
    r = recs.get(decision_id)
    if r is None:
        return False, f"unknown decision {decision_id}"
    if r["state"] == "superseded":
        return False, f"decision {decision_id} is superseded by {r['superseded_by']}"
    if r["state"] != "active":
        return False, f"decision {decision_id} is {r['state']}"
    return True, f"decision {decision_id} active ({r['source']})"


def require(action_class, decision_id=None, path=DEFAULT):
    """(ok, reason) for acting on an action of action_class: routine needs no id; every other class
    needs an active decision id; a named id is always checked, whatever the class."""
    if action_class not in CLASSES:
        return False, f"unclassified action class {action_class!r}"
    if decision_id is None:
        if action_class == "routine":
            return True, "routine: no decision needed"
        return False, f"{action_class} action needs a Salih decision id (ask Salih)"
    return require_active(decision_id, path)


def _lock(path):
    lock = Path(f"{path}.lock")
    for _ in range(LOCK_TRIES):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lock
        except FileExistsError:
            time.sleep(LOCK_GAP_S)
    # shortcut: a lock left by a killed writer blocks writes until someone deletes it, by hand on purpose
    raise TimeoutError(f"{lock} held; delete it only if no writer is running")


def _write(path, recs):
    tmp = Path(f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(recs.values()), f, indent=1, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        for i in range(REPLACE_TRIES):
            try:  # a reader holding the file open makes os.replace raise PermissionError on Windows
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_GAP_S)
    finally:
        if tmp.exists():
            tmp.unlink()


def _mutate(path, change):
    """Lock, read (missing -> empty; malformed raises and is never overwritten), change, check, write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock(path)
    try:
        recs = _read(path)
        change(recs)
        _check(list(recs.values()))
        _write(path, recs)
        return recs
    finally:
        lock.unlink()


def add(decision_id, action_class, source, at, path=DEFAULT):
    """A new active decision; an id already written (any state) is refused - ids are immutable."""
    def change(recs):
        if decision_id in recs:
            raise ValueError(f"decision {decision_id} already recorded; ids are immutable")
        recs[decision_id] = {"id": decision_id, "class": action_class, "state": "active",
                             "superseded_by": None, "source": source, "at": at}
    return _mutate(path, change)[decision_id]


def deactivate(decision_id, path=DEFAULT):
    def change(recs):
        r = recs.get(decision_id)
        if r is None or r["state"] != "active":
            raise ValueError(f"decision {decision_id} is not active")
        r["state"] = "inactive"
    return _mutate(path, change)[decision_id]


def supersede(decision_id, successor, path=DEFAULT):
    """decision_id (active or inactive) -> superseded by successor, which must be active."""
    def change(recs):
        r, s = recs.get(decision_id), recs.get(successor)
        if r is None or r["state"] == "superseded":
            raise ValueError(f"decision {decision_id} is unknown or already superseded")
        if s is None or s["state"] != "active" or successor == decision_id:
            raise ValueError(f"successor {successor} is not another active decision")
        r["state"], r["superseded_by"] = "superseded", successor
    return _mutate(path, change)[decision_id]
