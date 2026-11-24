"""N6-SHADOW1: the N6 shadow runner, Phase 1 (N6_KIT section 1, N6-PLAN2 B1/B2).

``judge`` runs only when ``build/n6/SHADOW_ON`` exists, and it checks that before any read or
write. It composes ``n6_root.N6`` in sight mode and runs ``n6_shadow.compare`` over the
conveyor snapshot. For each lane judged agree, disagree or false_pass that has no rows yet, it
appends the verdict and reason to the N6 notes (``build/n6/notes.jsonl``) and records one
KpiRow per source in ``build/n6/kpi.db``. The rows in kpi.db are the cursor, so a re-run writes
nothing twice. The runner never touches ``data/jarvis.db`` and never acts on a lane.

``--live N`` is read-only and ignores the switch. It prints the newest streak of at most N
judged slices. An excluded lane neither counts nor breaks the streak; any other finished lane
without both rows breaks it. It also prints agreement, false PASS and the overhead fields.

Row fields (B1), per source:
  passed:              the side's verdict is pass.
  post_accept_defects: FIX lanes and blocked INT lanes of the slice, counted only if that side passed.
  salih_interventions: needs_salih reader events; the n6 row counts only those that name N6.
  quota_tokens:        build/ledger/usage.json by_lane total for conveyor; 0 for n6, which makes no model call.
  finish_seconds:      the lane's finished_ts - started_ts for conveyor; the compare time per lane for n6.
  security_incidents:  0 for conveyor; for n6, its "<lane> refused:" notes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # CLI: make ``jarvis`` importable
from jarvis.n6_kpi import SOURCES, KpiError, KpiRow, record, rows as read_rows  # noqa: E402
from jarvis.n6_root import N6  # noqa: E402
from jarvis.n6_shadow import BLOCK, Snapshot, compare, conveyor_verdict, load  # noqa: E402

JUDGED = ("agree", "disagree", "false_pass")


def _paths(root: Path) -> tuple[Path, Path, Path]:
    n6 = root / "build" / "n6"
    return n6 / "SHADOW_ON", n6 / "kpi.db", n6 / "notes.jsonl"


def _read(n6: N6, path: Path) -> str:
    try:
        return n6.read(path)
    except FileNotFoundError:
        return ""


def _defects(snap: Snapshot, lane: str) -> int:
    """FIX lanes of the slice, plus its INT lanes that finished blocked."""
    fix = re.compile(re.escape(lane) + r"-?FIX\d*")
    integ = re.compile("INT-" + re.escape(lane) + r"(-R\d+)?")
    return sum(1 for other, state in snap.lanes.items()
               if fix.fullmatch(other) or (integ.fullmatch(other) and state.get("status") in BLOCK))


_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*")
_TIP = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _git_dir(root: Path) -> Path:
    """The repo's .git directory; a missing one or a reftable store fails the whole run."""
    git_dir = root / ".git"
    if not git_dir.is_dir():
        raise RuntimeError(f"N6 head: {git_dir} is not a git directory")
    if (git_dir / "reftable").exists():
        raise RuntimeError("N6 head: reftable ref storage is not read")
    return git_dir


def branch_tip(n6: N6, root: Path, lane: str) -> str | None:
    """The sha of ``refs/heads/worktree-<lane>``, read from the ref files through ``n6.read``.

    No git process: stage_git refuses the live repo (it has a multi-pack-index) and walks all
    of .git per call. None only when neither a loose nor a packed ref exists (branch deleted)
    or the lane name cannot be a plain ref file; any other oddity raises (``judge`` makes a
    bad ref that lane's unknown; a missing .git or a reftable store fails the run).
    """
    if not _LANE.fullmatch(lane) or lane.endswith(".lock"):
        return None  # no such branch can be read as one plain file under refs/heads
    git_dir = _git_dir(root)
    name = f"refs/heads/worktree-{lane}"
    loose = git_dir / name
    if not loose.is_dir():  # a directory there holds worktree-<lane>/... refs, not this one
        try:
            text: str | None = n6.read(loose)
        except FileNotFoundError:
            text = None
        except UnicodeDecodeError as error:  # one lane's garbage ref, not a git-wide fault
            raise RuntimeError(f"N6 head: {name} is not text: {error}") from error
        if text is not None:
            sha = text.strip()
            if not _TIP.fullmatch(sha):
                raise RuntimeError(f"N6 head: {name} does not hold a sha: {sha[:80]!r}")
            return sha
    for line in _read(n6, git_dir / "packed-refs").splitlines():
        if line.startswith(("#", "^")) or not line.strip():
            continue
        sha, _, ref = line.strip().partition(" ")
        if ref == name:
            if not _TIP.fullmatch(sha):
                raise RuntimeError(f"N6 head: packed {name} is not a sha: {sha[:80]!r}")
            return sha
    return None


def judge(root: Path, *, clock: Callable[[], datetime],
          timer: Callable[[], float] = time.perf_counter,
          head: Callable[[str], str | None] | None = None, origin: str = "live") -> int:
    """Judge every finished lane that has no rows yet; returns the number of rows written.

    ``origin`` (live | sweep | replay) is stamped on each note and KPI row for Gate B counting;
    the conveyor spawns this with no arguments, so a plain run is "live"."""
    if origin not in ("live", "sweep", "replay"):
        raise ValueError(f"origin must be live, sweep or replay, got {origin!r}")
    switch, db, notes_path = _paths(root)
    if not switch.is_file():
        return 0  # off: nothing is read or written
    n6 = N6(SimpleNamespace(clock=clock), store=cast(Any, None), notes=notes_path.resolve(),
            clock=clock)

    def git_head(lane: str) -> str | None:
        try:
            return branch_tip(n6, root, lane)
        except PermissionError as error:
            n6.note(f"{lane} refused: {error}")
            return None
        except RuntimeError as error:  # one lane's bad ref: that lane is unknown, the pass goes on
            print(f"N6 {lane}: {error}", file=sys.stderr)
            return None

    if head is None:
        _git_dir(root)  # global git trouble fails the run once, not as a per-lane unknown
    started = timer()
    result = compare(root, clock=clock, read=n6.read, head=head or git_head)
    took = (timer() - started) / max(len(result["records"]), 1)
    snap = load(root, read=n6.read)
    for number in snap.padded:
        print(f"n6_shadow: events.jsonl line {number}: dropped NUL fill from a crashed append",
              file=sys.stderr)
    for lane, why in sorted(snap.unreadable.items()):
        print(f"N6 {lane}: report unreadable: {why}", file=sys.stderr)
    usage_text = _read(n6, root / "build" / "ledger" / "usage.json")
    usage = json.loads(usage_text).get("by_lane", {}) if usage_text else {}
    notes = _read(n6, notes_path)
    done = {(r.slice_id, r.source) for r in read_rows(db)} if db.is_file() else set()
    written = 0
    for rec in result["records"]:
        lane, verdict = rec["lane"], rec["verdict"]
        missing = [s for s in SOURCES if (lane, s) not in done]
        state = snap.lanes.get(lane)
        if verdict not in JUDGED or not missing or state is None:
            continue
        entry = usage.get(lane)
        tokens = entry.get("total_tokens") if isinstance(entry, dict) else None
        try:
            seconds = float(state["finished_ts"]) - float(state["started_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if (isinstance(tokens, bool) or not isinstance(tokens, int)
                or not math.isfinite(seconds) or seconds < 0):
            continue  # R32: a missing reading is never a zero; --live shows the lane as a breaker
        asks = [e["reading"] for e in snap.events
                if e.get("event") == "reader" and e.get("lane") == lane
                and isinstance(e.get("reading"), dict) and e["reading"].get("kind") == "needs_salih"]
        # ponytail: "caused by N6" is a word match on the reader's text; tag the reading if N6 ever acts.
        by_n6 = sum(1 for a in asks if re.search(
            r"\bN6\b", f"{a.get('summary', '')} {a.get('next', '')}".replace(lane, "")))
        defects = _defects(snap, lane)
        passed = {s: rec[s]["verdict"] == "pass" for s in SOURCES}
        values = {"conveyor": (len(asks), tokens, seconds, 0),
                  "n6": (by_n6, 0, took, notes.count(f'"text": "{lane} refused:'))}
        n6.note(f"{lane} {verdict}: conveyor {rec['conveyor']['verdict']} "
                f"({rec['conveyor']['reason']}); n6 {rec['n6']['verdict']} ({rec['n6']['reason']}) [origin={origin}]")
        for source in missing:
            salih, quota, finish, security = values[source]
            row = KpiRow(lane, source, passed[source], defects if passed[source] else 0, salih,
                         quota, float(finish), security, clock(), origin)
            try:
                record(db, row)
                written += 1
            except KpiError:  # already recorded by an earlier run
                pass
    return written


def streak(items: list[tuple[str, bool, bool]], window: int) -> tuple[list[str], str | None]:
    """(lanes counted, lane that broke it). ``items`` = (lane, excluded, both rows), newest first."""
    run: list[str] = []
    for lane, excluded, paired in items:
        if len(run) >= window:
            break
        if paired:
            run.append(lane)
        elif not excluded:
            return run, lane
    return run, None


def _finished_ts(state: dict[str, Any]) -> float:
    try:
        return float(state.get("finished_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def live(root: Path, window: int) -> str:
    """The window report: the newest consecutive judged slices, read-only."""
    snap = load(root, read=lambda p: p.read_text(encoding="utf-8"))
    db = _paths(root)[1]
    pairs: dict[str, dict[str, KpiRow]] = {}
    for row in read_rows(db) if db.is_file() else ():
        pairs.setdefault(row.slice_id, {})[row.source] = row
    order = sorted(snap.lanes, key=lambda lane: _finished_ts(snap.lanes[lane]), reverse=True)
    run, broke = streak([(lane, conveyor_verdict(snap, lane)[0] == "excluded",
                          len(pairs.get(lane, {})) == 2) for lane in order], window)
    got = [pairs[lane] for lane in run]
    agree = [lane for lane, p in zip(run, got) if p["n6"].passed == p["conveyor"].passed]
    false_pass = [lane for lane, p in zip(run, got) if p["n6"].passed
                  and (not p["conveyor"].passed or p["n6"].post_accept_defects > 0)]

    def ratio(field: str) -> str:
        n6 = sum(getattr(p["n6"], field) for p in got)
        base = sum(getattr(p["conveyor"], field) for p in got)
        return f"{n6 / base:.1%}" if base else "n/a"

    lines = [f"WINDOW: {window}",
             f"STREAK: {len(run)}" + (f" (broken by {broke})" if broke else ""),
             f"SLICES: {' '.join(run) or 'none'}",
             f"AGREEMENT: {len(agree) / len(run):.1%}" if run else "AGREEMENT: n/a",
             f"DISAGREEMENTS: {' '.join(lane for lane in run if lane not in agree) or 'none'}",
             f"FALSE PASS: {len(false_pass)} {' '.join(false_pass)}".rstrip(),
             f"N6 SALIH INTERVENTIONS: {sum(p['n6'].salih_interventions for p in got)}",
             f"N6 SECURITY INCIDENTS: {sum(p['n6'].security_incidents for p in got)}",
             f"OVERHEAD QUOTA: {ratio('quota_tokens')}",
             f"OVERHEAD TIME: {ratio('finish_seconds')}"]
    return "\n".join(lines) + "\n"


def _diff_write(root: Path, day: Any, until: datetime) -> Any:
    """scripts/n6_shadow_diff.write, loaded from this file's folder (scripts/ is no package)."""
    spec = importlib.util.spec_from_file_location("n6_shadow_diff", Path(__file__).with_name("n6_shadow_diff.py"))
    if spec is None or spec.loader is None:
        raise ImportError("scripts/n6_shadow_diff.py not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write(root, day, until=until)


def daily(root: Path, *, clock: Callable[[], datetime], writer: Callable[..., Any] = _diff_write) -> bool:
    """N6DAILY1: after a completed shadow pass, write the daily diff of the previous local day
    (``clock``'s own zone) once: skipped when its .json and .md exist or the switch is off.
    It never raises; a failure is appended to build/n6/daily/errors.log (stderr is DEVNULL
    under the conveyor) and the pass result stands. True when a report was written."""
    out = root / "build" / "n6" / "daily"
    if not (root / "build" / "n6" / "SHADOW_ON").is_file():
        return False
    day = now = None
    try:
        now = clock()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = midnight.date() - timedelta(days=1)
        if all((out / f"{day.isoformat()}{ext}").is_file() for ext in (".json", ".md")):
            return False
        writer(root, day, until=midnight)  # notes from today stay out of yesterday's report
        return True
    except Exception as error:  # a diff failure must never fail the shadow pass
        line = f"{now.isoformat() if now else '?'} day={day} {error!r}"
        print(f"N6 daily: {line}", file=sys.stderr)
        try:
            out.mkdir(parents=True, exist_ok=True)
            with (out / "errors.log").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N6 shadow runner (Phase 1)")
    parser.add_argument("--live", type=int, metavar="N", help="print the last N judged slices")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--origin", choices=("live", "sweep", "replay"), default=None,
                        help="label for this run's notes and KPI rows (default: live)")
    args = parser.parse_args(argv)
    root = cast(Path, args.root)
    if args.live is not None:
        print(live(root, cast(int, args.live)), end="")
        return 0
    if args.origin is None:
        args.origin = "live"
        print("origin=live (default)", file=sys.stderr)  # a manual run is visible; conveyor path stays live
    judge(root, clock=lambda: datetime.now(timezone.utc), origin=cast(str, args.origin))
    daily(root, clock=lambda: datetime.now().astimezone())  # local day; never fails the pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
