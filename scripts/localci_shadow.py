"""LOCALCI0: the local CI shadow (architecture v2.4 Gate CI: >= 10 SHAs, cloud and local verdict equal,
test ids equal).

Each run (a scheduled task, every 10 min, under pythonw) takes one lock so two runs never overlap, then:
  1. for every recorded sha whose GitHub comparison is not complete, tries it (gh read-only);
  2. picks the newest ``ci_pending`` sha of build/conveyor/events.jsonl with no
     build/conveyor/localci/<sha>.json yet, runs CI's command unsharded in a detached scratch worktree
     under %TEMP%, writes the json, removes the worktree (never --force, never prune).
NO authority: it reads events.jsonl and writes only build/conveyor/localci/. It never approves, pushes or
changes conveyor state, never opens data/, never reads ntfy. ``--summary`` prints the gate's numbers.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "tests"
SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_DIFF = 50
SUITE_TIMEOUT = 4 * 3600
TAIL = 4000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def suite_python(exe: str = sys.executable) -> str:
    """The console interpreter next to pythonw.exe (the task runs under pythonw; the suite wants python)."""
    path = Path(exe)
    console = path.with_name("python.exe")
    return str(console) if path.name.lower() == "pythonw.exe" and console.exists() else str(path)


def ids_hash(ids) -> str:
    return hashlib.sha256("\n".join(sorted(set(ids))).encode("utf-8")).hexdigest()


def pending_shas(events: Path) -> list[dict]:
    """``ci_pending`` events, newest first, one per sha; unreadable lines are skipped."""
    try:
        text = events.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    seen: set[str] = set()
    found = []
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("event") != "ci_pending":
            continue
        sha = str(event.get("sha") or "")
        if SHA.match(sha) and sha not in seen:
            seen.add(sha)
            found.append({"sha": sha, "lane": event.get("lane"), "branch": event.get("branch")})
    return found


@contextlib.contextmanager
def file_lock(path: Path):
    """Yields True when this process holds the lock, False when another run does. The OS drops it when
    the holder dies, so there is no stale lock to clear."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _count(value) -> int:
    return value if isinstance(value, int) else len(value or [])


class Shadow:
    def __init__(self, root: Path = ROOT, out: Path | None = None, events: Path | None = None,
                 temp: Path | None = None, clock=utc_now, python: str | None = None):
        self.root = Path(root)
        self.out = Path(out) if out else self.root / "build" / "conveyor" / "localci"
        self.events = Path(events) if events else self.root / "build" / "conveyor" / "events.jsonl"
        self.temp = Path(temp) if temp else Path(tempfile.gettempdir())
        self.clock = clock
        self.python = python or suite_python()

    def cmd(self, argv: list[str], cwd: Path | None = None, timeout: float = 600) -> tuple[int, str]:
        """(returncode, stdout; plus stderr when it failed). Never raises; -1 when it could not run."""
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            done = subprocess.run(argv, cwd=cwd or self.root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                                  env={**os.environ, "PYTHONUTF8": "1"}, **flags)
        except (OSError, subprocess.SubprocessError) as exc:
            # shortcut: a timeout kills test_parallel, not its workers; they end on their own unit timeouts.
            return -1, f"{type(exc).__name__}: {exc}"
        return done.returncode, done.stdout if done.returncode == 0 else done.stdout + done.stderr

    def suite(self, argv: list[str], cwd: Path) -> tuple[int, str]:
        return self.cmd(argv, cwd=cwd, timeout=SUITE_TIMEOUT)

    def path(self, sha: str) -> Path:
        return self.out / f"{sha}.json"

    @staticmethod
    def load(path: Path) -> dict | None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def save(self, record: dict) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        target = self.path(record["sha"])
        part = target.with_suffix(".part")
        part.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(part, target)  # a half-written file never counts as done

    # ---- the local run -------------------------------------------------------------------------------
    def run_local(self, event: dict) -> dict:
        """Runs the suite at the sha and ALWAYS writes its json (under pythonw it is the only trace)."""
        record = {"sha": event["sha"], "lane": event.get("lane"), "branch": event.get("branch"),
                  "machine": platform.node(), "python": self.python, "started": self.clock(),
                  "verdict": "error", "errors": []}
        try:
            self._run_local(record)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            record["errors"].append(f"{type(exc).__name__}: {exc}")
        record["finished"] = self.clock()
        self.save(record)
        return record

    def _run_local(self, record: dict) -> None:
        sha, git = record["sha"], ["git", "-C", str(self.root)]
        code, out = self.cmd([*git, "cat-file", "-e", f"{sha}^{{commit}}"])
        if code:
            record["errors"].append(f"commit not found: {out.strip()[:300]}")
            return
        scratch = Path(tempfile.mkdtemp(prefix=f"localci-{sha[:12]}-", dir=self.temp))
        tree, report = scratch / "wt", scratch / "local-report.json"
        code, out = self.cmd([*git, "worktree", "add", "--detach", str(tree), sha])
        if code:
            record["errors"].append(f"worktree add failed: {out.strip()[:300]}")
            shutil.rmtree(scratch, ignore_errors=True)
            return
        try:
            # CI's command (.github/workflows/tests.yml "Run this shard"), unsharded, the sha's own runner.
            code, out = self.suite([self.python, "scripts/test_parallel.py", "--durations", "30",
                                    "--report", str(report)], tree)
            record["returncode"] = code
            data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
            if not isinstance(data, dict):
                record["errors"].append("no report written")
                record["tail"] = out[-TAIL:]
                return
            ids = sorted(set(data.get("dispatched") or []))
            record.update({
                "verdict": "green" if code == 0 and data.get("green") is True else "red",
                "counts": {"run": _count(data.get("ran")), "skipped": _count(data.get("skipped")),
                           "failures": _count(data.get("failures")), "errors": _count(data.get("errors"))},
                "duration": data.get("wall"), "ids": ids, "ids_count": len(ids), "ids_hash": ids_hash(ids)})
            if record["verdict"] != "green":
                record["tail"] = out[-TAIL:]
        finally:
            code, out = self.cmd([*git, "worktree", "remove", str(tree)])
            if code:
                record["cleanup_error"] = out.strip()[:500]  # left in place for a look; never --force
            else:
                shutil.rmtree(scratch, ignore_errors=True)

    # ---- the GitHub side (read-only) -----------------------------------------------------------------
    def compare(self, record: dict) -> None:
        """Adds GitHub's verdict and id set once its run for the sha is complete; errors are recorded."""
        done = (record.get("github") or {}).get("status") in ("complete", "cancelled")
        if record.get("verdict") not in ("green", "red") or done:
            return
        try:
            record["github"] = self._github(record)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            record["github"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        # shortcut: pending/error retry every run forever (expired artifacts, deleted branch); give up after N days.
        record["github"]["checked"] = self.clock()
        self.save(record)

    def _github(self, record: dict) -> dict:
        if not record.get("branch"):
            return {"status": "error", "error": "no branch in the ci_pending event"}
        code, out = self.cmd(["gh", "run", "list", "--branch", record["branch"], "--limit", "50", "--json",
                              "databaseId,headSha,status,conclusion,workflowName"])
        if code:
            return {"status": "error", "error": out.strip()[:500]}
        runs = [r for r in json.loads(out or "[]")
                if r.get("workflowName") == WORKFLOW and r.get("headSha") == record["sha"]]
        if not runs:
            return {"status": "pending", "reason": "no run yet"}
        run = runs[0]  # gh lists newest first
        if run.get("status") != "completed":
            return {"status": "pending", "run": run.get("databaseId")}
        if run.get("conclusion") == "cancelled":
            return {"status": "cancelled", "run": run.get("databaseId")}
        shards = []
        with tempfile.TemporaryDirectory(prefix="localci-gh-", dir=self.temp) as tmp:
            code, out = self.cmd(["gh", "run", "download", str(run["databaseId"]), "--pattern", "ci-shard-*",
                                  "--dir", tmp])
            if code:
                return {"status": "error", "run": run.get("databaseId"), "error": out.strip()[:500]}
            for path in sorted(Path(tmp).glob("ci-shard-*/shard-report.json")):
                data = json.loads(path.read_text(encoding="utf-8"))  # parsed only, never executed
                if isinstance(data, dict):
                    shards.append(data)
        return self._github_result(record, run, shards)

    def _github_result(self, record: dict, run: dict, shards: list[dict]) -> dict:
        indices = {tuple(s["shard"]) for s in shards if isinstance(s.get("shard"), list) and len(s["shard"]) == 2}
        total = max((n for _, n in indices), default=0)
        whole = total > 0 and indices == {(i, total) for i in range(1, total + 1)}
        verdict = "green" if whole and all(s.get("green") is True for s in shards) else "red"
        cloud = {t for s in shards for t in (s.get("dispatched") or [])}
        local = set(record.get("ids") or [])
        only_local = sorted(local - cloud)[:MAX_DIFF]
        only_github = sorted(cloud - local)[:MAX_DIFF - len(only_local)]
        walls = [s["wall"] for s in shards if isinstance(s.get("wall"), (int, float))]
        sums = (("run", "ran"), ("skipped", "skipped"), ("failures", "failures"), ("errors", "errors"))
        return {"status": "complete", "run": run.get("databaseId"), "conclusion": run.get("conclusion"),
                "shards": len(shards), "verdict": verdict, "verdict_equal": verdict == record.get("verdict"),
                "ids_count": len(cloud), "ids_hash": ids_hash(cloud), "ids_equal": cloud == local,
                "ids_only_local": only_local, "ids_only_github": only_github,
                "counts": {k: sum(_count(s.get(src)) for s in shards) for k, src in sums},
                "duration": max(walls) if walls else None}  # shards run in parallel: the slowest one

    # ---- one scheduled run ---------------------------------------------------------------------------
    def run_once(self) -> str:
        self.out.mkdir(parents=True, exist_ok=True)
        with file_lock(self.out / ".lock") as held:
            if not held:
                return "busy: another run holds the lock"
            for path in sorted(self.out.glob("*.json")):
                record = self.load(path)
                if record and record.get("sha"):
                    self.compare(record)
            todo = [e for e in pending_shas(self.events) if not self.path(e["sha"]).exists()]
            if not todo:
                return "idle: no new ci_pending sha"
            record = self.run_local(todo[0])
            self.compare(record)
            return f"ran {record['sha']}: {record['verdict']}"


def summary(out: Path) -> str:
    """The Gate CI numbers: SHAs compared, equal verdicts, equal id sets, median local vs GitHub duration."""
    records = [r for r in (Shadow.load(p) for p in sorted(Path(out).glob("*.json"))) if r]
    done = [r for r in records if (r.get("github") or {}).get("status") == "complete"]

    def median(values) -> str:
        values = [v for v in values if isinstance(v, (int, float))]
        return f"{statistics.median(values):.1f}s" if values else "n/a"

    return "\n".join([
        f"local runs: {len(records)}",
        f"SHAs compared: {len(done)}",
        f"equal verdicts: {sum(r['github'].get('verdict_equal') is True for r in done)}/{len(done)}",
        f"equal id sets: {sum(r['github'].get('ids_equal') is True for r in done)}/{len(done)}",
        f"median duration: local {median(r.get('duration') for r in done)}"
        f" vs GitHub {median(r['github'].get('duration') for r in done)} (slowest shard)"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", action="store_true", help="print the comparison numbers and exit")
    args = parser.parse_args(argv)
    try:
        if args.summary:
            print(summary(ROOT / "build" / "conveyor" / "localci"))
        else:
            print(Shadow().run_once())
    except Exception as exc:  # noqa: BLE001 - a scheduled task: report, never crash the scheduler
        print(f"localci_shadow: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
