# pyright: strict
"""N6G27INTSEQ: G27 integration sequencing (matrix R31), SHADOW ONLY.

``sequence`` orders the ready INT candidates for the next INT (or train) against the current
main sha and gives every candidate one typed verdict with a reason: ordered / held /
needs_rebase / unknown. Rules: a manual-key INT blocks an auto-key INT that overlaps its files
(O13), even when the manual one is not in this batch; a batch holds only disjoint file scopes
and no two members share a contract (ALLOWLIST T3, orch_gen integration); protected ranges are
never batched with unprotected ones; oldest first within a key class (manual before auto); a
candidate whose base is not main is needs_rebase and never ordered. ``compare_dispatch`` sets
that order beside the conveyor's actual INT dispatch order (the ``dispatch`` events of an
``n6_shadow.load`` snapshot).

Nothing here acts: no git, no file, no network, no model, no clock; pure functions over the
caller's values. It is not wired into the conveyor or orchbot (a later slice wires it after
>= 5 real shadow events; the conveyor keeps R31 until takeover). Unknown or malformed input is
a typed "unknown" verdict, never a guess.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

VERDICTS = ("ordered", "held", "needs_rebase", "unknown")
SHADOW_VERDICTS = ("agree", "disagree", "unknown")
MAX_BATCH = 6  # conveyor TRAIN_MAX / INT_MAX_BATCH
_SHA = re.compile(r"[0-9a-f]{40}")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_INT = re.compile(r"(?i)^int(?![a-z])")  # scripts/conveyor.py INT_RE, copied: never imported
_REQUIRED = frozenset({"id", "picks", "files", "key", "protected", "patch_ids", "age_s", "base"})
_OPTIONAL = frozenset({"contracts"})


@dataclass(frozen=True)
class Candidate:
    int_id: str
    picks: tuple[str, ...]
    files: frozenset[str]  # "/" separators, normalised, casefolded
    key: str  # manual | auto
    protected: bool
    patch_ids: tuple[str, ...]  # one reviewed patch-id per pick
    age_s: int
    base: str
    contracts: frozenset[str]


@dataclass(frozen=True)
class Sequence:
    ordered: tuple[str, ...]  # the next INT (or train), in order
    verdicts: tuple[tuple[str, str, str], ...]  # (id, verdict, reason), in input order
    attention: tuple[str, ...] = ()

    def verdict(self, int_id: str) -> tuple[str, str] | None:
        return next(((v, r) for i, v, r in self.verdicts if i == int_id), None)


@dataclass(frozen=True)
class Shadow:
    verdict: str  # agree | disagree | unknown
    reason: str
    n6: tuple[str, ...]
    conveyor: tuple[str, ...]


def _strings(raw: object) -> tuple[str, ...] | None:
    """A list/tuple of non-blank strings (a bare str is not a list of paths)."""
    if not isinstance(raw, (list, tuple)):
        return None
    items = tuple(cast("Iterable[object]", raw))
    if not all(isinstance(i, str) and i.strip() for i in items):
        return None
    return cast("tuple[str, ...]", items)


def _shas(raw: object) -> tuple[str, ...] | None:
    """Distinct 40-hex values, at least one."""
    items = _strings(raw)
    if not items or len(set(items)) != len(items) or not all(_SHA.fullmatch(i) for i in items):
        return None
    return items


def _path(raw: str) -> str | None:
    """A repo-relative path for the overlap test: jarvis\\x.py and jarvis/X.py are one file."""
    p = raw.replace("\\", "/")
    if p.startswith("/") or ":" in p or ".." in p.split("/"):
        return None
    norm = posixpath.normpath(p)
    return None if norm == "." else norm.casefold()


def _label(raw: object, index: int) -> str:
    if isinstance(raw, Mapping):
        sid = cast("Mapping[object, object]", raw).get("id")
        if isinstance(sid, str):
            return sid
    return f"<candidate {index}>"


def _candidate(raw: object) -> Candidate | str:
    """The validated candidate, or why it is unknown."""
    if not isinstance(raw, Mapping):
        return "candidate is not a mapping"
    m = cast("Mapping[object, object]", raw)
    keys = set(m)
    if not _REQUIRED <= keys or not keys <= _REQUIRED | _OPTIONAL:
        missing = sorted(_REQUIRED - keys)
        extra = sorted(repr(k) for k in keys - _REQUIRED - _OPTIONAL)
        return f"candidate fields: missing {missing}, unexpected {extra}"
    sid = m["id"]
    if not isinstance(sid, str) or not _LANE.fullmatch(sid) or not _INT.match(sid):
        return "id is not an INT lane name"
    picks, patch_ids = _shas(m["picks"]), _shas(m["patch_ids"])
    if picks is None:
        return "picks are not distinct 40-hex shas"
    if patch_ids is None or len(patch_ids) != len(picks):
        return "reviewed patch-ids are not one distinct 40-hex id per pick"
    names = _strings(m["files"])
    files = [_path(f) for f in names] if names else []
    if not files or any(f is None for f in files):
        return "files are not repo-relative paths"
    key, protected, age, base = m["key"], m["protected"], m["age_s"], m["base"]
    if key not in ("manual", "auto"):
        return "key is not manual|auto"
    if type(protected) is not bool:
        return "protected is not a bool"
    if protected and key != "manual":  # CONVSAFE: a protected range needs Salih's manual key 1
        return "a protected range with an auto key"
    if type(age) is not int or age < 0:
        return "age_s is not a non-negative int"
    if not isinstance(base, str) or not _SHA.fullmatch(base):
        return "base is not a 40-hex sha"
    contracts = _strings(m.get("contracts", ()))
    if contracts is None:
        return "contracts are not non-blank strings"
    return Candidate(sid, picks, frozenset(cast("list[str]", files)), cast(str, key), protected,
                     patch_ids, age, base, frozenset(contracts))


def _stem(int_id: str) -> str:
    """ALLOWLIST T3: only a slice and its own FIX may share a file (orch_gen integration)."""
    return re.sub(r"(?:FIX\d*)+$", "", int_id)


def _clash(c: Candidate, batch: list[Candidate]) -> str | None:
    """Why c cannot join the batch, naming the member it collides with."""
    for b in batch:
        if c.protected != b.protected:
            return f"protected class differs from batch head {batch[0].int_id}"
        if _stem(c.int_id) == _stem(b.int_id):
            continue
        shared = sorted(c.files & b.files)
        if shared:
            return f"scope overlaps {b.int_id} on {shared[0]}"
        contracts = sorted(c.contracts & b.contracts)
        if contracts:
            return f"shares contract {contracts[0]} with {b.int_id}"
    return None


def sequence(candidates: Iterable[object], main_sha: object,
             max_batch: int = MAX_BATCH) -> Sequence:
    """The ordered next INT (or train) and one (id, verdict, reason) per candidate."""
    raws = list(candidates)
    labels = [_label(r, i) for i, r in enumerate(raws)]
    if not isinstance(main_sha, str) or not _SHA.fullmatch(main_sha):
        return Sequence((), tuple((x, "unknown", "main sha is not 40-hex") for x in labels),
                        ("N6G27 sequencing held: main sha unreadable",))
    if type(cast(object, max_batch)) is not int or max_batch < 1:
        return Sequence((), tuple((x, "unknown", "max_batch is not a positive int")
                                  for x in labels), ("N6G27 sequencing held: bad max_batch",))
    parsed = [_candidate(r) for r in raws]
    valid = [p for p in parsed if isinstance(p, Candidate)]
    ids = Counter(c.int_id for c in valid)
    pids = Counter(p for c in valid for p in c.patch_ids)
    out: dict[int, tuple[str, str]] = {}
    for i, p in enumerate(parsed):
        if isinstance(p, str):
            out[i] = ("unknown", p)
        elif ids[p.int_id] > 1:
            out[i] = ("unknown", f"duplicate candidate id {p.int_id}")
        elif any(pids[x] > 1 for x in p.patch_ids):
            out[i] = ("unknown", "a reviewed patch-id is shared with another candidate")
        elif p.base != main_sha:
            out[i] = ("needs_rebase", f"base {p.base[:7]} is not main {main_sha[:7]}")
    good = {i: p for i, p in enumerate(parsed)
            if isinstance(p, Candidate) and out.get(i, ("", ""))[0] != "unknown"}
    # O13 blockers: every readable manual-key INT, ordered or not (rebase, other class).
    manual = sorted((c for c in good.values() if c.key == "manual"), key=lambda c: c.int_id)
    any_unknown = any(v == "unknown" for v, _ in out.values())
    eligible: list[tuple[int, Candidate]] = []
    for i, c in good.items():
        if i in out:
            continue
        if c.key == "auto":
            hit = next(((m, sorted(m.files & c.files)[0]) for m in manual
                        if m.files & c.files), None)
            if hit:
                out[i] = ("held", f"O13: manual-key INT {hit[0].int_id} overlaps {hit[1]}")
                continue
            if any_unknown:  # an unreadable candidate may be a manual-key INT on these files
                out[i] = ("held", "O13: an unknown candidate may be a manual-key INT")
                continue
        eligible.append((i, c))
    # manual before auto, then oldest first, then id: a total order, input order irrelevant
    eligible.sort(key=lambda e: (e[1].key != "manual", -e[1].age_s, e[1].int_id))
    batch: list[Candidate] = []
    for i, c in eligible:
        why = _clash(c, batch)
        if why is None and len(batch) >= max_batch:
            why = f"batch is full ({max_batch})"
        if why:
            out[i] = ("held", why)
        else:
            batch.append(c)
            out[i] = ("ordered", f"position {len(batch)}")
    attention = ("unknown candidates hold every auto-key INT (O13)",) if any_unknown else ()
    return Sequence(tuple(c.int_id for c in batch),
                    tuple((labels[i], *out[i]) for i in range(len(raws))), attention)


def compare_dispatch(seq: Sequence, events: Iterable[object]) -> Shadow:
    """N6's order beside the conveyor's: the first ``dispatch`` of each INT lane, ledger order
    (``n6_shadow.load(...).events``).

    disagree: the conveyor dispatched a candidate N6 did not order, or dispatched N6's ordered
    ids in another relative order. unknown: a malformed dispatch record, or no shared id."""
    dispatched: list[str] = []
    for e in events:
        if not isinstance(e, Mapping):
            return Shadow("unknown", "an event is not an object", seq.ordered, ())
        m = cast("Mapping[object, object]", e)
        if m.get("event") != "dispatch":
            continue
        lane = m.get("lane")
        if not isinstance(lane, str):
            return Shadow("unknown", "a dispatch record has no lane name", seq.ordered, ())
        if _INT.match(lane) and lane not in dispatched:
            dispatched.append(lane)
    conveyor = tuple(dispatched)
    verdicts = {i: (v, r) for i, v, r in seq.verdicts}
    shared = [x for x in conveyor if x in verdicts]
    if not shared:
        return Shadow("unknown", "the conveyor dispatched none of these candidates",
                      seq.ordered, conveyor)
    for x in shared:
        v, r = verdicts[x]
        if v != "ordered":
            return Shadow("disagree", f"conveyor dispatched {x}; N6 says {v}: {r}",
                          seq.ordered, conveyor)
    mine = [x for x in seq.ordered if x in shared]
    if mine != shared:
        return Shadow("disagree", f"conveyor order {shared}, N6 order {mine}",
                      seq.ordered, conveyor)
    return Shadow("agree", f"same order for {len(shared)} INT(s)", seq.ordered, conveyor)
