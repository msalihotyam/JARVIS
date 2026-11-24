"""QUARANTINE1: the deterministic flaky-test quarantine registry (ORKESTRATOR_MIMARI_v2 11.5).

A pure library + CLI. NOT wired into the conveyor or CI: nothing reads it to skip or pass a test today.
No network, no git, no model calls.

Rules
  R1 Eligibility: a test id whose module/class prefix maps to a never-quarantine class (security, data
     integrity, recovery, concurrency invariant, Core policy and gate) is never eligible; an id that maps to
     no class at all is not eligible either (fail closed). A protected match beats an allowed one.
  R2 Entry: the test flipped (failed and passed on the SAME sha) at least twice in the last 7 days, i.e. two
     distinct (sha, failing run) flips with the same failure signature. A fail on one sha and a pass on
     another is not evidence. The entry records the cause, the signature and the entry failure rate.
  R3 A quarantine covers only its own signature: a new or changed signature blocks again.
  R4 At most 3 days; entry opens a FIX request (data only). The test keeps running and reporting (check
     always says run=True). Expiry is computed from the clock, no write needed. An extension needs an
     approval record with approver == "salih"; anything else is refused and journalled.
  R5 Cancelled when the quarantined test fails 3 runs in a row, or its failure rate over the last (up to)
     10 runs is at least double its entry rate.
  R6 At most 5 active quarantines; the sixth entry is refused with "lane_growth_stop": true.
  R7 State is one JSON file (default build/ci/quarantine.json) with a generation number. Only Registry (the
     executor) writes it: under a lock file it re-reads the generation, refuses when it is not the one the
     caller loaded (CAS), writes a temp file and os.replace()s it, and appends one journal line
     (<state>.journal.jsonl) per decision. Every other function is a pure read.

Evidence file (propose --evidence):
  {"test_id": "tests.test_x.Class.test_y",
   "results": [{"sha": "<sha>", "run": "<run id>", "outcome": "pass" | "fail", "at": "<ISO-8601 with tz>",
                "exception": "AssertionError", "message": "..."}, ...]}
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "build" / "ci" / "quarantine.json"

WINDOW = timedelta(days=7)
MIN_FLIPS = 2
MAX_DAYS = timedelta(days=3)
MAX_ACTIVE = 5
CANCEL_STREAK = 3
RATE_RUNS = 10
APPROVER = "salih"
LOCK_TRIES = 200
LOCK_SLEEP = 0.01

# R1: the never-quarantine classes; a test mapped to one of these is never eligible.
PROTECTED_CLASSES = ("security", "data_integrity", "recovery", "concurrency_invariant", "core_policy_gate")
# Prefix -> class. Raw string prefixes: a broader protected match is the safe side.
PROTECTED_PREFIXES = {
    "tests.test_quarantine": "core_policy_gate",
    "tests.test_conveyor": "core_policy_gate",
    "tests.test_ci_skips": "core_policy_gate",
    "tests.test_policy": "core_policy_gate",
    "tests.test_permissions": "core_policy_gate",
    "tests.test_destination_policy": "core_policy_gate",
    "tests.test_billing_policy": "core_policy_gate",
    "tests.test_skill_authority_matrix": "core_policy_gate",
    "tests.test_remembered_grant_admission": "core_policy_gate",
    "tests.test_projects_gate": "core_policy_gate",
    "tests.test_key1_check": "security",
    "tests.test_privacy_canary": "security",
    "tests.test_browser_egress": "security",
    "tests.test_dora_egress": "security",
    "tests.test_egress_attestation": "security",
    "tests.test_memory": "security",
    "tests.test_skill_filter": "security",
    "tests.test_skill_learn_safety": "security",
    "tests.test_session_owner_binding": "security",
    "tests.test_load_sensitive": "security",
    "tests.test_billing_integrity": "data_integrity",
    "tests.test_registry_persistence": "data_integrity",
    "tests.test_conversation_store": "data_integrity",
    "tests.test_skill_store": "data_integrity",
    "tests.test_execution_journal": "data_integrity",
    "tests.test_recovery": "recovery",
    "tests.test_scheduler_recovery": "recovery",
    "tests.test_execution_orphan": "recovery",
    "tests.test_orphan_parity": "recovery",
    "tests.test_n6_outage": "recovery",
    "tests.test_scheduler_fencing": "concurrency_invariant",
    "tests.test_scheduler_claims": "concurrency_invariant",
    "tests.test_n6_lease": "concurrency_invariant",
    "tests.test_skill_single_writer": "concurrency_invariant",
}
ALLOWED_PREFIXES = {
    "tests.test_ui": "ui",
    "tests.test_df": "ui",
    "tests.test_g6": "ui",
    "tests.test_convui": "ui",
    "tests.test_projects_ui": "ui",
    "tests.test_project_card_ui": "ui",
    "tests.test_menus_6g": "ui",
    "tests.test_motion_6g": "ui",
    "tests.test_icons_6g": "ui",
    "tests.test_indicators_6g": "ui",
    "tests.test_code_map": "tooling",
    "tests.test_mutgen": "tooling",
    "tests.test_review_kpi": "tooling",
    "tests.test_orch_digest": "tooling",
}
ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")

_NORMALIZE = (  # order matters: paths, then timestamps, then hex ids, then bare numbers
    (re.compile(r"[A-Za-z]:[\\/][^\s'\"]*?[\\/](?:temp|tmp)[\\/][^\s'\"]*", re.I), "<tmp>"),
    (re.compile(r"(?<![\w.])/(?:var/folders|private/var|tmp|var/tmp)/[^\s'\"]*", re.I), "<tmp>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<hex>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.I), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_at(text) -> datetime:
    """An aware datetime or ValueError (a naive timestamp is refused, not guessed)."""
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {str(text)[:40]}")
    return moment


def normalize_message(message: str) -> str:
    text = str(message or "")
    for pattern, repl in _NORMALIZE:
        text = pattern.sub(repl, text)
    return text.strip()


def signature(test_id: str, exception: str, message: str) -> str:
    """R2/R3: the failure signature = test id + exception type + normalized message, hashed."""
    text = f"{test_id}\n{str(exception or '').strip()}\n{normalize_message(message)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def classify(test_id: str) -> str | None:
    """R1: the test's class; protected wins over allowed; None = unknown."""
    for table in (PROTECTED_PREFIXES, ALLOWED_PREFIXES):
        hits = [p for p in table if test_id == p or test_id.startswith(p)]
        if hits:
            return table[max(hits, key=len)]
    return None


def eligibility(test_id: str) -> dict:
    if not isinstance(test_id, str) or not ID.match(test_id):
        return {"eligible": False, "class": None, "reason": "bad_test_id"}
    cls = classify(test_id)
    if cls is None:
        return {"eligible": False, "class": None, "reason": "unknown_class"}
    if cls in PROTECTED_CLASSES:
        return {"eligible": False, "class": cls, "reason": "protected_class"}
    return {"eligible": True, "class": cls, "reason": "eligible"}


# ---- pure reads -------------------------------------------------------------------------------------------

def empty_state() -> dict:
    return {"generation": 0, "entries": {}, "fix_requests": []}


def load_state(path) -> dict:
    """A missing file is generation 0; a malformed one raises ValueError (never repaired or guessed)."""
    path = Path(path)
    if not path.exists():
        return empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("generation")) is not int
            or not isinstance(data.get("entries"), dict) or not isinstance(data.get("fix_requests"), list)):
        raise ValueError(f"malformed quarantine state: {path}")
    return data


def effective_status(entry: dict, now: datetime) -> str:
    """R4: expiry needs no write; an active entry past expires_at is simply expired (blocking again)."""
    if entry.get("status") != "active":
        return str(entry.get("status"))
    return "active" if now < parse_at(entry["expires_at"]) else "expired"


def active_ids(state: dict, now: datetime) -> list:
    return sorted(t for t, e in state["entries"].items() if effective_status(e, now) == "active")


def check(state: dict, test_id: str, sig: str, now: datetime) -> dict:
    """R3/R4: covered only by an active entry with the SAME signature. The test always runs and reports."""
    entry = state["entries"].get(test_id)
    status = effective_status(entry, now) if entry else "not_quarantined"
    if status == "active" and entry.get("signature") != sig:
        status = "signature_mismatch"
    covered = status == "active"
    return {"test_id": test_id, "signature": sig, "run": True, "covered": covered, "blocking": not covered,
            "reason": "covered" if covered else status}


def _rows(evidence) -> tuple:
    """Trust boundary: the evidence file is validated field by field; anything off raises ValueError."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("results"), list):
        raise ValueError("evidence must be an object with a 'results' list")
    test_id = evidence.get("test_id")
    if not isinstance(test_id, str) or not ID.match(test_id):
        raise ValueError("evidence.test_id must be a dotted test id")
    rows = []
    for i, row in enumerate(evidence["results"]):
        if not isinstance(row, dict) or row.get("outcome") not in ("pass", "fail"):
            raise ValueError(f"results[{i}]: outcome must be 'pass' or 'fail'")
        for key in ("sha", "run"):
            if not isinstance(row.get(key), (str, int)) or str(row.get(key)).strip() == "":
                raise ValueError(f"results[{i}]: {key} is required")
        rows.append({"sha": str(row["sha"]), "run": str(row["run"]), "outcome": row["outcome"],
                     "at": parse_at(row.get("at")), "exception": str(row.get("exception") or ""),
                     "message": str(row.get("message") or "")})
    return test_id, rows


def evaluate(evidence, now: datetime) -> dict:
    """R1 + R2, pure. ok=True only for an eligible test with >= 2 same-sha flips sharing one signature."""
    test_id, rows = _rows(evidence)
    elig = eligibility(test_id)
    if not elig["eligible"]:
        return {"ok": False, "test_id": test_id, "class": elig["class"], "reason": elig["reason"]}
    window = [r for r in rows if now - WINDOW <= r["at"] <= now]
    passed = {r["sha"] for r in window if r["outcome"] == "pass"}
    flips, texts = {}, {}
    for r in window:
        if r["outcome"] == "fail" and r["sha"] in passed:  # a fail and a pass on the SAME sha
            sig = signature(test_id, r["exception"], r["message"])
            flips.setdefault(sig, set()).add((r["sha"], r["run"]))
            texts[sig] = f"{r['exception'].strip()}: {normalize_message(r['message'])}"
    if not flips:
        return {"ok": False, "test_id": test_id, "class": elig["class"], "reason": "no_same_sha_flip"}
    best = sorted(flips, key=lambda s: (-len(flips[s]), s))[0]  # most flips, then lowest signature
    if len(flips[best]) < MIN_FLIPS:
        return {"ok": False, "test_id": test_id, "class": elig["class"], "reason": "insufficient_flips",
                "flips": len(flips[best])}
    fails = sum(1 for r in window if r["outcome"] == "fail")
    return {"ok": True, "test_id": test_id, "class": elig["class"], "signature": best,
            "signature_text": texts[best], "entry_rate": round(fails / len(window), 4),
            "cause": {"kind": "same_sha_flip", "flips": [list(f) for f in sorted(flips[best])]}}


def decide_entry(state: dict, evidence, now: datetime) -> tuple:
    """R1/R2/R4/R6, pure: (decision, new_state or None). Re-entry of a known test is refused (no self-extension)."""
    verdict = evaluate(evidence, now)
    decision = {"action": "propose", "at": iso(now), **verdict}
    if not verdict["ok"]:
        return decision, None
    test_id = verdict["test_id"]
    if test_id in state["entries"]:
        return {**decision, "ok": False, "reason": "already_registered"}, None
    if len(active_ids(state, now)) >= MAX_ACTIVE:
        return {**decision, "ok": False, "reason": "max_active", "lane_growth_stop": True}, None
    entry = {"test_id": test_id, "class": verdict["class"], "signature": verdict["signature"],
             "signature_text": verdict["signature_text"], "cause": verdict["cause"],
             "entry_rate": verdict["entry_rate"], "entered_at": iso(now), "expires_at": iso(now + MAX_DAYS),
             "status": "active", "runs": [], "extensions": []}
    new = json.loads(json.dumps(state))
    new["entries"][test_id] = entry
    new["fix_requests"].append({"id": f"FIX-{test_id}-{verdict['signature'][:8]}", "test_id": test_id,
                                "signature": verdict["signature"], "opened_at": iso(now), "status": "open"})
    return {**decision, "reason": "entered", "expires_at": entry["expires_at"]}, new


def decide_run(state: dict, test_id: str, outcome: str, now: datetime) -> tuple:
    """R4/R5, pure: a quarantined test keeps reporting; 3 fails in a row, or a full 10-run window whose
    failure rate is >= 2x the entry rate, cancels the quarantine."""
    decision = {"action": "run", "at": iso(now), "test_id": test_id, "outcome": outcome, "ok": False}
    if outcome not in ("pass", "fail"):
        return {**decision, "reason": "bad_outcome"}, None
    entry = state["entries"].get(test_id)
    if not entry or effective_status(entry, now) != "active":
        return {**decision, "reason": "not_active"}, None
    new = json.loads(json.dumps(state))
    entry = new["entries"][test_id]
    entry["runs"] = (entry["runs"] + [{"outcome": outcome, "at": iso(now)}])[-RATE_RUNS:]
    outcomes = [r["outcome"] for r in entry["runs"]]
    rate = outcomes.count("fail") / len(outcomes)
    if outcomes[-CANCEL_STREAK:] == ["fail"] * CANCEL_STREAK:
        why = "fail_streak"
    elif len(outcomes) >= RATE_RUNS and rate >= 2 * entry["entry_rate"]:
        why = "rate_doubled"
    else:
        return {**decision, "ok": True, "reason": "recorded", "rate": round(rate, 4)}, new
    entry["status"], entry["cancelled_at"], entry["cancel_reason"] = "cancelled", iso(now), why
    return {**decision, "ok": True, "reason": "cancelled", "cancel_reason": why, "rate": round(rate, 4)}, new


def decide_extension(state: dict, test_id: str, approval, now: datetime) -> tuple:
    """R4, pure: only an explicit approval record with approver == "salih" extends, by MAX_DAYS from now."""
    decision = {"action": "extend", "at": iso(now), "test_id": test_id, "ok": False}
    if not isinstance(approval, dict) or approval.get("approver") != APPROVER:
        return {**decision, "reason": "extension_needs_salih"}, None
    entry = state["entries"].get(test_id)
    if not entry or entry.get("status") != "active":
        return {**decision, "reason": "not_extendable"}, None
    new = json.loads(json.dumps(state))
    entry = new["entries"][test_id]
    entry["expires_at"] = iso(now + MAX_DAYS)
    entry["extensions"].append({"approval": approval, "at": iso(now), "expires_at": entry["expires_at"]})
    return {**decision, "ok": True, "reason": "extended", "expires_at": entry["expires_at"]}, new


# ---- the executor (the only writer) -----------------------------------------------------------------------

class Registry:
    """R7: every decision takes the lock, appends one journal line, and writes the state only when the
    generation on disk is still the one the caller loaded (CAS); temp file + os.replace."""

    def __init__(self, path=DEFAULT_STATE, clock=utc_now):
        self.path = Path(path)
        self.journal = self.path.with_name(self.path.stem + ".journal.jsonl")
        self.lock = self.path.with_name(self.path.name + ".lock")
        self.clock = clock

    def load(self) -> dict:
        return load_state(self.path)

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(LOCK_TRIES):
            try:
                os.close(os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                time.sleep(LOCK_SLEEP)
        else:
            raise TimeoutError(f"quarantine lock busy: {self.lock}")  # fail closed, never steal a lock
        try:
            yield
        finally:
            os.unlink(self.lock)

    def _apply(self, state: dict, decision: dict, new: dict | None) -> dict:
        with self._locked():
            disk = load_state(self.path)["generation"]
            if new is not None and disk != state["generation"]:
                decision = {**decision, "ok": False, "reason": "cas_conflict",
                            "expected_generation": state["generation"], "generation": disk}
            elif new is not None:
                new["generation"] = disk + 1
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(new, fh, indent=2, sort_keys=True)
                        fh.write("\n")
                    os.replace(tmp, self.path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                    raise
                decision = {**decision, "generation": new["generation"]}
            else:
                decision = {**decision, "generation": disk}
            with open(self.journal, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(decision, sort_keys=True) + "\n")
        return decision

    def propose(self, state: dict, evidence) -> dict:
        return self._apply(state, *decide_entry(state, evidence, self.clock()))

    def record_run(self, state: dict, test_id: str, outcome: str) -> dict:
        return self._apply(state, *decide_run(state, test_id, outcome, self.clock()))

    def extend(self, state: dict, test_id: str, approval) -> dict:
        return self._apply(state, *decide_extension(state, test_id, approval, self.clock()))


# ---- CLI --------------------------------------------------------------------------------------------------

def status(state: dict, now: datetime) -> dict:
    entries = {t: {"status": effective_status(e, now), "signature": e["signature"], "expires_at": e["expires_at"]}
               for t, e in sorted(state["entries"].items())}
    return {"generation": state["generation"], "active": active_ids(state, now), "max_active": MAX_ACTIVE,
            "entries": entries, "open_fix_requests": [f for f in state["fix_requests"] if f["status"] == "open"]}


def main(argv=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flaky-test quarantine registry (not wired into CI).")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="state JSON (journal sits next to it)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_check = sub.add_parser("check")
    p_check.add_argument("test_id")
    p_check.add_argument("signature")
    p_prop = sub.add_parser("propose")
    p_prop.add_argument("--evidence", required=True)
    p_prop.add_argument("--write", action="store_true", help="persist (default: dry run, nothing written)")
    args = parser.parse_args(argv)
    clock = (lambda: now) if now is not None else utc_now
    registry = Registry(args.state, clock)
    try:
        state = registry.load()
        if args.cmd == "status":
            out, code = status(state, clock()), 0
        elif args.cmd == "check":
            out = check(state, args.test_id, args.signature, clock())
            code = 0 if out["covered"] else 1
        else:
            evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            if args.write:
                out = registry.propose(state, evidence)
            else:
                out = {**decide_entry(state, evidence, clock())[0], "dry_run": True}
            code = 0 if out["ok"] else 1
    except (OSError, ValueError, TimeoutError) as exc:  # JSONDecodeError is a ValueError
        print(f"quarantine: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
