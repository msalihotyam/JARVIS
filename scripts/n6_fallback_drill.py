"""N6ROLLDRILL1: the R34/G29 fallback-switch rehearsal, entirely in a temp copy.

A temp root it created (marker + token) holds its own build/n6 and build/conveyor; the
real N6 caller (scripts/n6_limited_run.tick, runner write_claim) runs against it with an
injected clock and a fixed Situation, and a fake conveyor mirrors n6_take's refusal order.
The live build/n6/LIMITED_ON, build/conveyor and every real lane are never touched.

  python scripts/n6_fallback_drill.py   runs both modes in fresh temp dirs, prints the receipt
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis import n6_deps, n6_lease  # noqa: E402
from jarvis.contracts import AttemptState  # noqa: E402
from jarvis.work_supervisor import DEFAULT_POLICY, Situation  # noqa: E402

if "n6_limited_run" not in sys.modules:  # dataclasses resolves annotations through sys.modules
    _spec = importlib.util.spec_from_file_location("n6_limited_run", ROOT / "scripts" / "n6_limited_run.py")
    sys.modules["n6_limited_run"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["n6_limited_run"])
lr = sys.modules["n6_limited_run"]

MARKER = ".n6drill"
T0, TTL = 1_800_000_000.0, 600.0  # synthetic clock: the drill never reads the wall clock
# The conveyor's protected_path + PROTECTED_FILES (scripts/conveyor.py), not imported: a lane there is protected-range.
PROTECTED = (".github/workflows/", ".claude/", "data/", "dev/dashboard/")
PROTECTED_FILES = {"scripts/conveyor.py", "scripts/test_parallel.py", "scripts/ci.ps1", "scripts/quality.ps1"}
PROTECTED_EXT = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".db", ".sqlite", ".sqlite3")
SIT = Situation.from_dict({"operator_pause": False, "policy": DEFAULT_POLICY,
                           "memory": {"free_gb": 8.0, "held": False},
                           "quota": {"used_pct": 10, "line_pct": 10, "hour": 12}})
NOT_STARTED = "not_started"


class DrillRefused(Exception):
    """The drill will not run here (not its temp dir, or a T2/protected-range lane in flight)."""


class DrillFailed(AssertionError):
    """An invariant broke: a slice lost, doubled, or N6 not restored."""


def _real(p: Path | str) -> Path:
    return Path(os.path.realpath(p))  # one form on both sides: 8.3 TEMP names never break containment


def make_root() -> tuple[Path, str]:
    """A fresh temp root marked with a random token only this caller knows."""
    root = _real(tempfile.mkdtemp(prefix="n6drill-"))
    token = secrets.token_hex(16)
    (root / MARKER).write_text(token, encoding="utf-8")
    return root, token


def guard(root: Path | str, token: str) -> Path:
    """The root, only if it is a temp dir make_root created (under TEMP, outside the repo, token matches)."""
    r, tmp, repo = _real(root), _real(tempfile.gettempdir()), _real(ROOT)
    if tmp not in r.parents:
        raise DrillRefused(f"not a temp dir: {r}")
    if r == repo or repo in r.parents or r in repo.parents:
        raise DrillRefused(f"inside the repo: {r}")
    try:
        mark = (r / MARKER).read_text(encoding="utf-8")
    except OSError:
        raise DrillRefused(f"no drill marker: {r}") from None
    if not secrets.compare_digest(mark, token):
        raise DrillRefused(f"drill marker mismatch: {r}")
    return r


def cleanup(root: Path | str, token: str) -> None:
    shutil.rmtree(guard(root, token))  # the guard first: never an unguarded delete


def _get(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class Drill:
    def __init__(self, root: Path | str, token: str, slices=(("S1", "T1"), ("S2", "T1"))):
        self.root = guard(root, token)
        self.n6, self.conv = self.root / "build" / "n6", self.root / "build" / "conveyor"
        self.started_dir = self.conv / "inbox" / "started"
        self.started_dir.mkdir(parents=True, exist_ok=True)
        self.n6.mkdir(parents=True, exist_ok=True)
        self.clock = T0
        self.steps: list[str] = []
        self.starts: dict[str, int] = {}  # conveyor starts per slice (dispatch or claim)
        self.claims: dict[str, int] = {}  # N6 claims per slice
        if not (self.conv / "backlog.json").exists():
            lr._put(self.conv / "backlog.json", {"slices": [
                {"id": s, "tier": t, "n6": True, "depends_on": []} for s, t in slices]})
        if not (self.conv / "lanes.json").exists():
            lr._put(self.conv / "lanes.json", {})

    # -- state ---------------------------------------------------------------------------
    def tiers(self) -> dict[str, object]:
        data = _get(self.conv / "backlog.json")
        return {s["id"]: s.get("tier") for s in data["slices"]}

    def lanes(self) -> dict:
        return _get(self.conv / "lanes.json")

    def started(self, sid: str) -> list[Path]:
        return [p for p in self.started_dir.iterdir() if p.name[16:] == f"{sid}.md"]

    def log(self, who: str, notes) -> None:
        self.steps += [f"{who}: {n}" for n in (notes if isinstance(notes, list) else [notes]) if n]

    def switch(self, on: bool) -> None:
        flag = self.n6 / "LIMITED_ON"  # the temp copy's switch, never the live one
        if on:
            flag.write_text("", encoding="utf-8")
        else:
            flag.unlink(missing_ok=True)
        self.log("operator", f"fallback switch {'back to N6' if on else 'flipped to the conveyor'}")

    def ports(self):
        return lr.Ports(now=lambda: self.clock, verify=lambda sha: True, tier=self.tiers().get,
                        situation=lambda sid: SIT, run=lambda sid, lease: lr.write_claim(self.n6, lease),
                        ttl_s=TTL)

    def tick(self) -> list[str]:
        notes = lr.tick(self.root, self.ports())  # the real N6 LIMITED caller
        self.log("n6", notes)
        for n in notes:
            if n.endswith(": claimed for the conveyor"):
                sid = n.split(":", 1)[0]
                self.claims[sid] = self.claims.get(sid, 0) + 1
        return notes

    def resolve(self, sid: str, epoch: int) -> str:
        """N6's claim (intent) against the conveyor's answer (receipt), read from the temp state.
        No receipt while the conveyor has the slice is UNKNOWN_OUTCOME: never retried blind."""
        r = _get(self.n6 / "claim_result.json")
        if isinstance(r, dict) and r.get("epoch") == epoch and r.get("slice_id") == sid:
            return "started" if r.get("state") == "started" else "refused"
        return AttemptState.UNKNOWN_OUTCOME.value if lr._conveyor_has(self.root, sid) else NOT_STARTED

    # -- the fake conveyor -------------------------------------------------------------------
    def _start(self, sid: str) -> None:
        lanes = self.lanes()
        lanes[sid] = {"status": None, "finished": False}
        lr._put(self.conv / "lanes.json", lanes)
        (self.started_dir / f"{int(self.clock):015d}-{sid}.md").write_text(sid, encoding="utf-8")
        self.starts[sid] = self.starts.get(sid, 0) + 1

    def take(self, crash_before_answer: bool = False) -> str | None:
        """scripts/conveyor.py n6_take's refusal order, under the lease lock: the answer to an
        open claim, or None (no claim, lock busy, already answered, or crashed before answering)."""
        claim = _get(self.n6 / "claim.json")
        if not isinstance(claim, dict):
            return None
        lease = n6_lease.Lease(**claim)
        sid, lease_path = lease.slice_id, self.n6 / "lease.json"
        with n6_lease._locked(lease_path) as got:
            if not got or self.resolve(sid, lease.epoch) in ("started", "refused"):
                return None  # lock busy, or this claim is already answered
            states = {e["id"]: e["state"] for e in n6_deps._read(self.n6 / "deps.json")}
            if not (self.n6 / "LIMITED_ON").is_file():
                reason = "switch off"
            elif lr.INT_RE.match(sid):
                reason = "int"
            elif states.get(sid) != "started":
                reason = "not started"
            elif not n6_lease.check(lease_path, lease, self.clock):
                reason = "lease lost"
            elif self.tiers().get(sid) != "T1":
                reason = "not T1"
            elif lr._conveyor_has(self.root, sid):
                reason = "already running"
            else:
                reason = ""
                self._start(sid)
                if crash_before_answer:
                    self.log("conveyor", f"{sid}: started, crashed before the answer")
                    return None
            state = "refused" if reason else "started"
            lr._put(self.n6 / "claim_result.json",
                    {"epoch": lease.epoch, "slice_id": sid, "state": state, "reason": reason})
        self.log("conveyor", f"{sid}: claim {state}{f' ({reason})' if reason else ''}")
        return state

    def dispatch(self) -> str | None:
        """The fallback conveyor's own pass (switch off), cap 1: the first backlog slice that no
        live lease, open claim or conveyor record already holds. State-based, never blind.
        Like pick_slice (scripts/conveyor.py:4322) it never starts an n6:true slice itself."""
        if lr.switch_on(self.root):
            return None
        claim = _get(self.n6 / "claim.json")
        lease_path = self.n6 / "lease.json"
        with n6_lease._locked(lease_path) as got:
            live = n6_lease.live_slices(lease_path, self.clock)
            if not got or live is n6_lease.HELD_ALL:
                return None
            n6_only = {s["id"] for s in _get(self.conv / "backlog.json")["slices"] if s.get("n6") is True}
            for sid in self.tiers():
                if sid in n6_only or sid in live or (isinstance(claim, dict) and claim.get("slice_id") == sid) \
                        or lr._conveyor_has(self.root, sid):
                    continue
                self._start(sid)
                self.log("conveyor", f"{sid}: dispatched by the fallback conveyor")
                return sid
        return None

    def finish(self, sid: str, status: str = "PASS") -> None:
        lanes = self.lanes()
        lanes[sid] = {"status": status, "finished": True}
        lr._put(self.conv / "lanes.json", lanes)
        self.log("conveyor", f"{sid}: lane finished {status}")

    # -- the drill -----------------------------------------------------------------------
    def preflight(self) -> None:
        """Refuse while any unfinished lane is not T1 (T2, unknown) or touches a protected path."""
        backlog = {s["id"]: s for s in _get(self.conv / "backlog.json")["slices"]}
        for sid, ln in self.lanes().items():
            if isinstance(ln, dict) and ln.get("finished"):
                continue
            entry = backlog.get(sid, {})
            if entry.get("tier") != "T1":
                raise DrillRefused(f"{sid}: a {entry.get('tier') or 'non-T1'} lane is in flight")
            files = [*(ln.get("files") or [] if isinstance(ln, dict) else []), *(entry.get("files") or [])]
            hit = [f for f in files if _protected(str(f))]
            if hit:
                raise DrillRefused(f"{sid}: a protected-range lane is in flight ({hit[0]})")

    def snap(self) -> dict:
        lease = n6_lease._read(self.n6 / "lease.json")
        claim = _get(self.n6 / "claim.json")
        return {
            "switch": "N6" if lr.switch_on(self.root) else "conveyor",
            "lease": None if not isinstance(lease, dict) else {
                "slice": lease["slice_id"], "epoch": lease["epoch"], "live": lease["expires"] > self.clock},
            "deps": {e["id"]: e["state"] for e in n6_deps._read(self.n6 / "deps.json")},
            "lanes": {k: {"finished": v.get("finished"), "status": v.get("status")}
                      for k, v in self.lanes().items()},
            "started": sorted(p.name for p in self.started_dir.iterdir()),
            "claim": None if not isinstance(claim, dict) else {"slice": claim["slice_id"], "epoch": claim["epoch"]},
            "claim_result": _get(self.n6 / "claim_result.json"),
        }

    def run(self) -> dict:
        """Flip the switch while N6's started S1 runs; the conveyor finishes it exactly once;
        switching back restores N6 (it claims S2, never S1 again)."""
        self.preflight()
        self.switch(True)
        _expect(self.tick(), ["S1: claimed for the conveyor"])
        epoch0 = _get(self.n6 / "claim.json")["epoch"]
        _expect(self.take(), "started")
        before = self.snap()
        self.clock += 60
        self.switch(False)  # the flip, with S1 in progress
        _expect(self.tick(), ["S1: running"])
        _expect(self.dispatch(), None)  # S1 is N6's: the conveyor finishes it, never starts it twice
        self.clock += 60
        self.finish("S1")
        _expect(self.tick(), ["S1: PASS"])
        after = self.snap()
        _expect(len(self.started("S1")), 1)
        _expect(self.starts, {"S1": 1})
        _expect(self.claims, {"S1": 1})
        _expect(after["lanes"].get("S1"), {"finished": True, "status": "PASS"})
        _expect((after["deps"].get("S2"), "S2" in after["lanes"]), ("pending", False))  # not lost
        self.clock += 60
        self.switch(True)
        _expect(self.tick(), ["S2: claimed for the conveyor"])
        restored = self.snap()
        if not restored["claim"] or restored["claim"]["epoch"] <= epoch0:
            raise DrillFailed(f"N6 not restored on a fresh epoch: {restored['claim']}")
        _expect(self.claims, {"S1": 1, "S2": 1})
        receipt = {"drill": "N6ROLLDRILL1", "mode": "finish", "before": before, "after": after,
                   "restored": restored, "counts": {"conveyor_starts": self.starts, "n6_claims": self.claims},
                   "steps": self.steps, "verdict": "PASS"}
        lr._put(self.n6 / "drill_receipt.json", receipt)
        return receipt


def _protected(path: str) -> bool:
    """conveyor protected_path or PROTECTED_FILES, fail closed: case-insensitive, either separator, "." segments
    dropped, Win32 trailing dots/spaces dropped per segment, unicode compatibility forms, marks and format chars folded;
    empty, absolute, ".."-like, drive/stream (":"), glob or 8.3 ("~") paths and ancestors of a protected path count."""
    p = "".join(ch for ch in unicodedata.normalize("NFKD", path.casefold())
                if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf")
    raw = p.replace("\\", "/").strip().split("/")
    # any "..", "...", ".. " or ". " segment may climb or alias once Win32 trims dots/spaces: fail closed
    if raw[0] == "" or any(s not in ("", ".") and not s.rstrip(". ") for s in raw) or any(ch in p for ch in ":*?[~"):
        return True
    segs = [t for t in (s.rstrip(". ") for s in raw) if t]
    if not segs:
        return True
    p, base = "/".join(segs), segs[-1]
    known = PROTECTED + tuple(PROTECTED_FILES)
    return (p in PROTECTED_FILES or (p + "/").startswith(PROTECTED) or any(x.startswith(p + "/") for x in known)
            or base == "uv.lock" or base.startswith(".env")
            or ".jarvis" in segs or any(s.startswith("riot") for s in segs) or base.endswith(PROTECTED_EXT))


def _expect(got: object, want: object) -> None:
    if got != want:
        raise DrillFailed(f"expected {want!r}, got {got!r}")


def main() -> int:
    root, token = make_root()
    try:
        print(json.dumps(Drill(root, token).run(), indent=1))
    finally:
        cleanup(root, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
