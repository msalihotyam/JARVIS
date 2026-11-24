# pyright: strict
"""N6G22: the G22 CI watch port (R17), CI results to one typed verdict per candidate sha, read-only.

``ci_verdict`` reads the conveyor ledger text (``build/conveyor/events.jsonl``, its ``ci_result``
and ``ci_rerun`` rows) and/or a GitHub check-runs JSON document, both passed in as text, against
a passed-in required-check list and an explicit KNOWN-RED list (empty by default). The verdict
is one of ``VERDICTS``; ``known-red-only`` is its own state and is NEVER green. ``compare_finish``
sets that verdict against the conveyor's own ``finish`` event for the lane (shadow stage).

SHADOW ONLY: this module reads nothing itself, writes nothing, calls no network, model or git,
and is not wired into the conveyor or orchbot. It fails closed: malformed or contradictory
input, a sha mismatch, a duplicated run, a partial document or a missing required check is
never green; anything N6 cannot attribute is ``unknown``, never a guess.

Ledger-only success stays ``unknown``: the conveyor's ci_result is workflow-level and cannot
show that each required check passed on the exact sha, so green needs the check-runs document.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

VERDICTS = ("green", "red", "pending", "rerun-pending", "known-red-only", "unknown")
AGREEMENT = ("agree", "disagree")
_SHA = re.compile(r"[0-9a-f]{40}")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# unittest's failure header, e.g. "FAIL: test_x (tests.test_mod.Class.test_x)"
_HEADER = re.compile(r"(?:FAIL|ERROR): \S+ \(([A-Za-z_][\w.]*)\)")
_RULE = re.compile(r"(?:={10,}|-{10,})")
_TOTALS = re.compile(r"FAILED \(([^)]*)\)")
# one item of unittest's FAILED (...) totals; ASCII digits, at most 9 (int() refuses > 4300)
_TOTAL_ITEM = re.compile(r"(failures|errors|skipped|expected failures)=([0-9]{1,9})")
_RAN = re.compile(r"Ran [0-9]+ tests? in [0-9.]+s")
# the only lines unittest prints outside a FAIL/ERROR block: progress, rules, Ran, FAILED totals
_OUTSIDE = re.compile(r"[.EFsxu]+|={10,}|-{10,}|Ran [0-9]+ tests? in [0-9.]+s|FAILED \([^)]*\)")
# json.loads raises ValueError (JSONDecodeError, int digit limit) or RecursionError (deep nesting)
_PARSE_ERRORS = (ValueError, RecursionError)


class MalformedInput(ValueError):
    """Input that does not parse into what this reader expects."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json object_pairs_hook: json.loads keeps the last duplicate key, hiding evidence."""
    doc = dict(pairs)
    if len(doc) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return doc


@dataclass(frozen=True)
class KnownRed:
    """One accepted known failure: the unittest id and the exact last traceback line."""
    test_id: str
    signature: str


@dataclass(frozen=True)
class CiVerdict:
    sha: str
    verdict: str
    reason: str
    failing_checks: tuple[str, ...] = ()
    test_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShadowRecord:
    lane: str
    sha: str
    agreement: str
    reason: str
    n6: str
    conveyor: str


def parse_events(text: str) -> tuple[dict[str, Any], ...]:
    """events.jsonl text to rows, with n6_shadow.load's rules: NUL fill dropped, else strict."""
    if not isinstance(cast(object, text), str):
        raise MalformedInput("events.jsonl is not text")
    body = text.rstrip("\x00")
    if text and not body:
        raise MalformedInput("events.jsonl is only NUL fill")
    if body and not body.endswith("\n"):
        raise MalformedInput("events.jsonl ends in a partial line")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(body.split("\n")[:-1] if body else [], 1):
        line = line.lstrip("\x00")
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_no_duplicates)
        except _PARSE_ERRORS as error:
            raise MalformedInput(f"events.jsonl line {number} does not parse") from error
        if not isinstance(row, dict):
            raise MalformedInput(f"events.jsonl line {number} is not an object")
        rows.append(cast(dict[str, Any], row))
    return tuple(rows)


def failures(text: str) -> tuple[tuple[str, str], ...]:
    """(test id, signature) per unittest FAIL/ERROR block; the signature is its last line.

    A block whose closing rule is not followed by a header or the "Ran N tests" line, or that
    is cut off, gets the empty signature (never KNOWN-RED): the rule may be inside the message."""
    if not isinstance(cast(object, text), str):
        raise MalformedInput("failure output is not text")
    return _scan(text)[0]


def _scan(text: str) -> tuple[tuple[tuple[str, str], ...], bool]:
    """(failures(text), stray): stray is True when a non-blank line outside every FAIL/ERROR
    block is not unittest's own output (progress, rule, Ran, FAILED totals), e.g. lint text."""
    found: list[tuple[str, str]] = []
    current: str | None = None
    last = ""
    stray = False
    started = False  # the "-----" right under a header opens the traceback, it does not end it
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    for index, line in enumerate(lines):
        header = _HEADER.fullmatch(line)
        if header:
            if current is not None:
                found.append((current, last))
            current, last, started = header.group(1), "", False
        elif current is not None and _RULE.fullmatch(line):
            if started:
                after = next((lines[i] for i in range(index + 1, len(lines)) if lines[i]), "")
                trusted = _HEADER.fullmatch(after) or _RAN.fullmatch(after)  # unittest always ends with Ran
                found.append((current, last if trusted else ""))
                current = None
            started = True
        elif current is not None and line:
            last = line
        elif current is None and line and not _OUTSIDE.fullmatch(line):
            stray = True
    if current is not None:  # cut off before its closing rule: the last line may be mid-message
        found.append((current, ""))
    return tuple(found), stray


def _accounted(text: str, found: tuple[tuple[str, str], ...]) -> bool:
    """Every FAIL/ERROR header parsed and the FAILED totals equal the parsed blocks; otherwise
    a block was missed (a subTest header, non-unittest output), so known-red cannot be proven."""
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]
    if any(line.startswith(("FAIL:", "ERROR:")) and not _HEADER.fullmatch(line) for line in lines):
        return False
    totals = [m.group(1) for m in map(_TOTALS.fullmatch, lines) if m]
    counted = 0
    for total in totals:  # an unknown category (unexpected successes) is an unlisted failure
        counts: dict[str, int] = {}
        for item in total.split(", "):
            part = _TOTAL_ITEM.fullmatch(item)
            if part is None or part.group(1) in counts:
                return False
            counts[part.group(1)] = int(part.group(2))
        bad = counts.get("failures", 0) + counts.get("errors", 0)
        if not bad:
            return False
        counted += bad
    return bool(totals) and counted == len(found)


def _unknown(sha: str, reason: str) -> CiVerdict:
    return CiVerdict(sha, "unknown", reason[:200])


def _ledger(sha: str, rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """(none | rerun | success | failure | unknown, reason) from this sha's ci rows, in order."""
    mine = [r for r in rows if r.get("event") in ("ci_result", "ci_rerun") and r.get("sha") == sha]
    reruns = [i for i, r in enumerate(mine) if r.get("event") == "ci_rerun"]
    results = [i for i, r in enumerate(mine) if r.get("event") == "ci_result"]
    if len(reruns) > 1:  # R17: one automatic rerun per sha; a second claim is a restart bug
        return "unknown", f"{len(reruns)} ci_rerun rows for one sha"
    raw = [mine[i].get("result") for i in results]
    if not all(isinstance(o, str) and o for o in raw):  # checked before the set: [1] is unhashable
        return "unknown", "a ci_result row has no result"
    outcomes = set(cast(list[str], raw))
    if len(outcomes) > 1:
        return "unknown", f"conflicting ci_result rows: {sorted(cast(set[str], outcomes))}"
    if reruns and (not results or results[-1] < reruns[0]):
        rc = mine[reruns[0]].get("rc")
        if type(rc) is int and rc == 0:  # bool is an int subclass; False is not a started rerun
            return "rerun", "rerun started, no ci_result after it"
        return "unknown", f"rerun not started (rc {rc!r}) and no ci_result after it"
    if not results:
        return "none", "no ci row for the sha"
    outcome = cast(str, outcomes.pop())
    return ("success" if outcome == "success" else "failure"), f"ledger ci_result {outcome}"


def _runs(sha: str, text: str) -> dict[str, dict[str, Any]] | CiVerdict:
    """name -> run from a check-runs document, or the unknown verdict that refuses it."""
    if not isinstance(cast(object, text), str):
        return _unknown(sha, "check-runs document is not text")
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates)
    except _PARSE_ERRORS:
        return _unknown(sha, "check-runs document does not parse")
    if not isinstance(doc, dict):
        return _unknown(sha, "check-runs document is not an object")
    doc = cast(dict[str, Any], doc)
    runs, total = doc.get("check_runs"), doc.get("total_count")
    if not isinstance(runs, list) or type(total) is not int:
        return _unknown(sha, "not a check-runs document (check_runs, total_count)")
    runs = cast(list[Any], runs)
    if total != len(runs):  # a paged or truncated answer cannot prove every check
        return _unknown(sha, f"partial document: total_count {total}, {len(runs)} runs listed")
    seen: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            return _unknown(sha, "a check run is not an object")
        run = cast(dict[str, Any], run)
        name, head, status = run.get("name"), run.get("head_sha"), run.get("status")
        if not (isinstance(name, str) and name and isinstance(head, str)
                and isinstance(status, str)):
            return _unknown(sha, "a check run has no name, head_sha or status")
        if head != sha:  # success on another sha is not green, for any check
            return _unknown(sha, f"check run {name!r} is on sha {head[:12]!r}, not {sha[:12]}")
        if name in seen:
            return _unknown(sha, f"duplicated check run {name!r}")
        seen[name] = run
    return seen


def _checks(sha: str, required: Sequence[str], text: str,
            known: frozenset[tuple[str, str]]) -> CiVerdict:
    """The check-runs document's own verdict; only required checks decide pass/fail."""
    seen = _runs(sha, text)
    if isinstance(seen, CiVerdict):
        return seen
    failing: list[str] = []
    waiting: list[str] = []
    pairs: list[tuple[str, str]] = []
    unparsed = False
    for name in required:
        run = seen.get(name)
        if run is None:
            continue
        if run["status"] != "completed":
            waiting.append(name)
        elif run.get("conclusion") != "success":  # neutral, skipped, cancelled: not success
            failing.append(name)
            output = run.get("output") or {}
            if not isinstance(output, dict):
                return _unknown(sha, f"check run {name!r} output is not an object")
            parts = [cast(dict[str, Any], output).get(k) for k in ("summary", "text")]
            if not all(p is None or isinstance(p, str) for p in parts):
                return _unknown(sha, f"check run {name!r} output text is not text")
            body = "\n".join(cast(str, p) for p in parts if p)
            found, stray = _scan(body)
            # only a plain test failure in pure unittest output can be known-red; timed_out,
            # cancelled, etc. or any other text (lint, npm, the summary) stays red
            unparsed = (unparsed or run.get("conclusion") != "failure" or not found or stray
                        or not _accounted(body, found))
            pairs.extend(found)
    missing = [name for name in required if name not in seen]
    if failing:
        names = tuple(failing)
        ids = tuple(dict.fromkeys(test_id for test_id, _ in pairs))
        if unparsed or not all(pair in known for pair in pairs):
            return CiVerdict(sha, "red", f"required check failed: {', '.join(names)}"[:200],
                             names, ids)
        if waiting:
            return CiVerdict(sha, "pending", f"known-red so far, {waiting[0]!r} still running",
                             names, ids)
        if missing:
            return CiVerdict(sha, "unknown", f"known-red so far, required check {missing[0]!r} "
                             "missing", names, ids)
        return CiVerdict(sha, "known-red-only", "every failure is on the KNOWN-RED list",
                         names, ids)
    if waiting:
        return CiVerdict(sha, "pending", f"required check {waiting[0]!r} not completed")
    if missing:  # a missing required workflow or check is not green
        return _unknown(sha, f"required check {missing[0]!r} missing")
    return CiVerdict(sha, "green", "every required check is success on the exact sha")


def _known(entries: Iterable[KnownRed]) -> frozenset[tuple[str, str]] | None:
    pairs: set[tuple[str, str]] = set()
    if not isinstance(entries, (list, tuple, set, frozenset)):
        return None
    for entry in cast(Iterable[object], entries):
        # getattr: KnownRed.__new__(KnownRed) has no fields set
        test_id, signature = getattr(entry, "test_id", None), getattr(entry, "signature", None)
        if not (isinstance(entry, KnownRed) and isinstance(test_id, str) and test_id
                and isinstance(signature, str) and signature):
            return None
        pairs.add((test_id, signature))
    return frozenset(pairs)


def ci_verdict(sha: str, *, required: Sequence[str], events: str | None = None,
               checks: str | None = None, known_red: Iterable[KnownRed] = ()) -> CiVerdict:
    """One typed verdict for the exact candidate sha from the ledger and/or check-runs text."""
    if not isinstance(cast(object, sha), str) or not _SHA.fullmatch(sha):
        # no str() of a non-str sha: a 5000-digit int or a raising __str__ would escape
        return _unknown(sha if isinstance(cast(object, sha), str) else "",
                        "candidate sha is not 40 lowercase hex")
    # a bare str is a Sequence too; only a real list/tuple of names is a required-check list
    names = list(cast(Sequence[object], required)) if isinstance(required, (list, tuple)) else []
    if (not names or not all(isinstance(n, str) and n for n in names)
            or len(set(names)) != len(names)):
        return _unknown(sha, "required-check list is empty, malformed or has duplicates")
    known = _known(known_red)
    if known is None:
        return _unknown(sha, "KNOWN-RED list has a malformed entry")
    if events is None and checks is None:
        return _unknown(sha, "no CI evidence given")
    state, why = "none", "no ledger given"
    if events is not None:
        try:
            state, why = _ledger(sha, parse_events(events))
        except MalformedInput as error:
            return _unknown(sha, f"ledger: {error}")
    if state == "unknown":
        return _unknown(sha, why)
    doc = _checks(sha, cast(list[str], names), checks, known) if checks is not None else None
    if doc is not None and doc.verdict == "unknown":
        return doc
    if state == "rerun":  # the rerun supersedes the runs it replaces
        return CiVerdict(sha, "rerun-pending", why)
    if doc is None:
        if state == "failure":
            return CiVerdict(sha, "red", f"{why}, no check-runs document")
        if state == "success":
            return _unknown(sha, f"{why}, required checks unverified without check runs")
        return _unknown(sha, "no CI evidence for the sha")
    if (state == "none" or (state == "success" and doc.verdict == "green")
            or (state == "failure" and doc.verdict in ("red", "known-red-only"))):
        return doc
    return _unknown(sha, f"{why} but the check runs say {doc.verdict}")


def compare_finish(verdict: CiVerdict, finish: Mapping[str, Any], *,
                   events: str) -> ShadowRecord:
    """agree / disagree with a reason code against the conveyor's finish event for the lane.

    The finish row carries no sha, so the lane's LAST ledger ci_result must be on the
    verdict's sha; otherwise the two verdicts are about different commits (sha_unbound).
    Only both_green and both_red are agreement; every other code is a disagreement, and
    finish_* / events_malformed / sha_unbound / n6_* codes are not CI judgements to count.
    """
    row = cast(Mapping[str, Any], finish) if isinstance(cast(object, finish), Mapping) else {}
    lane, status = row.get("lane"), row.get("status")
    typed = isinstance(cast(object, verdict), CiVerdict)
    sha = getattr(verdict, "sha", None) if typed else None
    n6 = getattr(verdict, "verdict", None) if typed else None
    sha, n6 = (sha if isinstance(sha, str) else ""), (n6 if isinstance(n6, str) else "")

    def text(value: object) -> str:
        try:
            return str(value)
        except Exception:  # a 5000-digit int, a very deep list, a raising __str__
            return ""

    def record(agreement: str, reason: str) -> ShadowRecord:
        return ShadowRecord(text(lane), sha, agreement, reason, n6, text(status))

    if row.get("event") != "finish" or not isinstance(lane, str) or not _LANE.fullmatch(lane):
        return record("disagree", "finish_malformed")
    if not sha or n6 not in VERDICTS:
        return record("disagree", "n6_malformed")
    if status not in ("GREEN", "BLOCKED"):  # CI_PENDING, DEAD, STUCK...: not a CI verdict
        return record("disagree", "finish_not_ci")
    reason = row.get("reason")
    if status == "BLOCKED" and isinstance(reason, str) and reason.startswith("stacked on failed"):
        return record("disagree", "finish_stacked_base")
    # conveyor ci_step: a CI-caused BLOCKED reason is its "CI: <url> <state>" report line;
    # any other (no STATUS line, sha not on main...) is not a CI judgement
    if status == "BLOCKED" and not (isinstance(reason, str) and reason.startswith("CI: ")):
        return record("disagree", "finish_blocked_other")
    try:
        rows = parse_events(events)
    except MalformedInput:
        return record("disagree", "events_malformed")
    shas = [r.get("sha") for r in rows if r.get("event") == "ci_result" and r.get("lane") == lane]
    if not shas or shas[-1] != sha:
        return record("disagree", "sha_unbound")
    if n6 == "unknown":
        return record("disagree", "n6_unknown")
    if n6 in ("pending", "rerun-pending"):
        return record("disagree", "n6_not_final")
    if status == "GREEN":
        return record(*(("agree", "both_green") if n6 == "green"
                        else ("disagree", "false_red")))
    return record(*(("disagree", "false_green") if n6 == "green"
                    else ("agree", "both_red")))
