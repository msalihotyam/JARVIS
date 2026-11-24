"""N6M6SCEN1: run the three pinned M6 scenarios (Phase 3) on temp repos and score them.

Reads ``dev/n6/acceptance/m6/scenarios.json`` and refuses (exit 2) unless its ``sha256`` (the
canonical JSON of the document without that key) verifies. Each scenario runs in its own temp
git repo: a fake writer commits the scenario's attempt files, the visible check runs with
``python -m unittest``, the real review gate (``review_slice``) judges the diff through a fake
read-only reviewer that applies the scenario's rules, a retry needs a ``root_cause_ok`` record
and stays within ``RETRY_CAP`` attempts, and an accepted slice gets a signed receipt and then the
hidden post-accept check. No network, no model, no database; nothing is written under
``dev/n6/acceptance/``.

KPIs: ``accepted_per_quota`` = accepted slices / fake worker sessions (writer attempts plus
review sessions); ``post_accept_failures`` = accepted slices whose hidden check fails. A reading
that is absent prints ``missing`` and makes the overall result FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess  # nosec B404 - local git and python -m unittest in a temp repo only
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.work_checks import CheckResult  # noqa: E402
from jarvis.work_evidence import PASS, InvocationRecord, receipt_verdict  # noqa: E402
from jarvis.work_regress import RETRY_CAP, RegressionVerdict, root_cause_ok  # noqa: E402
from jarvis.work_review import BLOCKING, review_slice  # noqa: E402
from jarvis.workers.base import AgentProfile, WorkerResult  # noqa: E402

SCENARIOS = ROOT / "dev" / "n6" / "acceptance" / "m6" / "scenarios.json"
ACCEPTANCE = ROOT / "dev" / "n6" / "acceptance"
IDS = ("A", "B", "C")
WRITER, REVIEWER = "claude", "codex"
SESSION_CAP = 4
PINNED_SHA = "a9531bcaaa23fcc04ee5c0c1e28b3e9dcc904ac560d5f427d32dd606ca1afc2a"
GIT_SUBCOMMANDS = frozenset({"init", "add", "commit", "rev-parse", "diff", "status"})
_DATE = "2026-11-24T23:30:00+00:00"  # fixed commit dates: deterministic SHAs, no clock read
_NAME = re.compile(r"[a-z_][a-z0-9_]*\.py")


class Refused(Exception):
    """The scenario file or an argument is not acceptable; nothing ran."""


def canonical_sha(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_scenarios(path: Path = SCENARIOS) -> list[dict]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"unreadable scenarios: {exc}") from exc
    if type(doc) is not dict or type(doc.get("sha256")) is not str:
        raise Refused("scenarios.json carries no sha256")
    # The pin lives here too: a fixture edit with a recomputed in-file sha256 is still refused.
    if not doc["sha256"] == canonical_sha(doc) == PINNED_SHA:
        raise Refused("scenarios.json sha256 mismatch")
    scenarios = doc.get("scenarios")
    if (type(scenarios) is not list or any(type(s) is not dict for s in scenarios)
            or tuple(s.get("id") for s in scenarios) != IDS):
        raise Refused("scenarios must be A, B, C")
    return scenarios


def out_path(raw: str) -> Path:
    path = Path(raw).resolve()
    forbidden = Path(os.path.normcase(str(ACCEPTANCE.resolve())))
    if Path(os.path.normcase(str(path))).is_relative_to(forbidden):
        raise Refused("the runner never writes under dev/n6/acceptance/")
    return path


def _env(gitconfig: Path) -> dict:
    # GIT_DIR / GIT_WORK_TREE from a caller must never point git at the real repository.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_DATE=_DATE,
               GIT_COMMITTER_DATE=_DATE, PYTHONDONTWRITEBYTECODE="1")
    return env


def _git(repo: Path, env: dict, *args: str) -> str:
    if args[0] not in GIT_SUBCOMMANDS:
        raise Refused(f"git {args[0]} is not a local scenario command")
    cmd = ["git", "-c", "user.name=m6", "-c", "user.email=m6@invalid", "-c", "commit.gpgsign=false",
           "-c", "core.autocrlf=false", *args]
    return subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,  # nosec B603 B607
                          check=True, timeout=60).stdout


def _write(repo: Path, files: dict) -> list[str]:
    for name, text in files.items():
        if not _NAME.fullmatch(name) or type(text) is not str:
            raise Refused(f"bad fixture file {name!r}")
        (repo / name).write_text(text, encoding="utf-8", newline="\n")
    return sorted(files)


def _dirty(repo: Path, env: dict, owned: set[str]) -> list[str] | None:
    """M2: the owned paths left uncommitted, staged or untracked after the writer's commit.

    None when git status cannot be read: receipt_verdict turns that into INVALID (fail closed).
    """
    try:
        out = _git(repo, env, "status", "--porcelain", "--untracked-files=all")
        # "XY path"; fixture names match _NAME, so git never quotes them and no rename arrow appears.
        return sorted({line[3:] for line in out.splitlines() if len(line) > 3} & owned)
    except Exception:  # any failure is an unknown tree, never a clean one
        return None


def _check(repo: Path, env: dict, argv: list[str]) -> CheckResult:
    argv = [sys.executable if a == "python" else a for a in argv]
    proc = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True,  # nosec B603
                          timeout=120)
    return CheckResult(argv=tuple(argv), runner="unittest", exit_code=proc.returncode, duration_s=0.0,
                       stdout=proc.stdout, stderr=proc.stderr, truncated=False, timed_out=False)


class FakeReviewer:
    """Read-only stand-in for the codex reviewer: the scenario's regex rules over added lines."""

    provider = REVIEWER
    profile = AgentProfile(name="m6-fake-reviewer", tools=frozenset({"Read"}), paths=frozenset({"."}),
                           network=False, max_turns=1, budget_sessions=1)

    def __init__(self, rules: list[dict]) -> None:
        self.rules, self.findings = rules, []

    def start(self, prompt: str) -> str:
        added = "\n".join(line[1:] for line in prompt.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        self.findings = [{"severity": r["severity"], "file": r["file"], "line": 1, "text": r["text"]}
                         for r in self.rules if re.search(r["pattern"], added, re.M)]
        return "m6-review"

    def status(self, run_id: str) -> str:
        return "completed"

    def stop(self, run_id: str) -> None:
        return None

    def collect(self, run_id: str) -> WorkerResult:
        blocking = any(f["severity"] in BLOCKING for f in self.findings)
        summary = json.dumps({"verdict": "CHANGES_REQUIRED" if blocking else "PASS",
                              "findings": self.findings})
        return WorkerResult(status="completed", summary=summary, changed_files=(), evidence=())


def run_scenario(sc: dict, work: Path, env: dict, key: bytes) -> dict:
    """Drive one scenario to accepted or blocked; raises on a broken fixture or a gate refusal."""
    repo = work / sc["id"]
    repo.mkdir()
    _git(repo, env, "init", "-q")
    _write(repo, sc["base"])
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "base")
    base = _git(repo, env, "rev-parse", "HEAD").strip()
    res = {"id": sc["id"], "outcome": "blocked", "blocked_by": None, "attempts": 0,
           "writer_sessions": 0, "review_sessions": 0, "root_causes": [], "check_failures": 0,
           "planted_file": sc.get("planted_file")}
    last: tuple[str, str] | None = None
    revisions = 0
    owned = {name for files in sc["attempts"] for name in files}  # the writer's slice
    for n, files in enumerate(sc["attempts"], 1):
        if last is not None:
            if n > RETRY_CAP:
                res["blocked_by"] = "retry_cap"
                return res
            if not root_cause_ok(sc.get("root_causes", {}).get(last[0]), *last):
                res["blocked_by"] = "no_root_cause"
                return res
            res["root_causes"].append(last[0])
        res["attempts"] = n
        res["writer_sessions"] += 1
        changed = _write(repo, files)
        _git(repo, env, "add", "-A")
        _git(repo, env, "commit", "-q", "-m", f"{sc['id']} attempt {n}")
        sha = _git(repo, env, "rev-parse", "HEAD").strip()
        check = _check(repo, env, sc["check"])
        if check.exit_code != 0:
            last, res["blocked_by"] = (f"{sc['id']}-{n}", check.stdout + check.stderr), "check"
            res["check_failures"] += 1
            continue
        decision = review_slice(
            writer=WRITER, reviewer=REVIEWER, worker=FakeReviewer(sc["review_rules"]), checks=[check],
            regression=RegressionVerdict(True, frozenset(), ()), brief=sc["title"], spec=sc["spec"],
            diff=_git(repo, env, "diff", base, sha), revisions=revisions,
            sessions_used=res["review_sessions"], session_cap=SESSION_CAP, sleep=lambda _s: None)
        revisions, res["review_sessions"] = decision.revisions, decision.sessions_used
        res.update(review=decision.outcome, review_reason=decision.reason,
                   blocking_files=sorted({f.file for f in decision.blocking}))
        if decision.outcome != "accepted":
            # Only real blocking findings count as "blocked by review"; a gate error is not.
            res["blocked_by"] = "review" if decision.reason == "blocking_findings" else decision.outcome
            last = (f"{sc['id']}-{n}", " ".join(f.text for f in decision.blocking) or decision.reason)
            continue
        record = InvocationRecord("check", check.argv, sha, check.exit_code).sign(key)
        commit_files = _git(repo, env, "diff", "--name-only", base, sha).split()
        res["receipt"] = receipt_verdict({"check": check.argv}, [record], sha, key=key,
                                         changed_files=changed, commit_files=commit_files,
                                         dirty_paths=_dirty(repo, env, owned)).verdict
        _write(repo, sc["hidden"]["files"])
        hidden = _check(repo, env, sc["hidden"]["argv"])
        res.update(outcome="accepted", blocked_by=None, post_accept_failed=hidden.exit_code != 0)
        return res
    return res


def run_all(scenarios: list[dict]) -> dict:
    key = secrets.token_bytes(32)
    results: dict = {}
    with tempfile.TemporaryDirectory(prefix="n6m6-", ignore_cleanup_errors=True) as tmp:
        gitconfig = Path(tmp) / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        env = _env(gitconfig)
        for sc in scenarios:
            try:
                results[sc["id"]] = run_scenario(sc, Path(tmp), env, key)
            except Exception:  # fail closed: a broken run is a missing reading, never a PASS
                results[sc["id"]] = None
    return results


def judge(sid: str, r: dict | None) -> str:
    if type(r) is not dict:
        return "missing"
    clean = (r.get("outcome") == "accepted" and r.get("receipt") == PASS
             and r.get("post_accept_failed") is False)
    if sid == "A":
        ok = clean and r.get("attempts") == 1 and not r.get("root_causes")
    elif sid == "B":
        ok = (r.get("outcome") == "blocked" and r.get("blocked_by") == "review"
              and r.get("review_reason") == "blocking_findings"
              and r.get("planted_file") in (r.get("blocking_files") or []))
    else:
        ok = (clean and r.get("attempts") == 2 <= RETRY_CAP and r.get("root_causes") == ["C-1"]
              and r.get("check_failures") == 1)  # attempt 1 failed its check, not review
    return "PASS" if ok else "FAIL"


def score(results: dict) -> tuple[bool, str]:
    """(overall ok, text): per-scenario PASS/FAIL/missing plus the two KPIs."""
    verdicts = {sid: judge(sid, results.get(sid)) for sid in IDS}
    lines = [f"{sid} {verdicts[sid]}" for sid in IDS]
    rows = [results.get(sid) for sid in IDS]
    counts = (all(type(r) is dict for r in rows)
              and all(type(r.get(k)) is int for r in rows for k in ("writer_sessions", "review_sessions")))
    accepted = [r for r in rows if counts and r.get("outcome") == "accepted"]
    quota = sum(r["writer_sessions"] + r["review_sessions"] for r in rows) if counts else 0
    apq = f"{len(accepted)}/{quota}" if quota else "missing"
    readable = counts and all(type(r.get("post_accept_failed")) is bool for r in accepted)
    paf = str(sum(r["post_accept_failed"] for r in accepted)) if readable else "missing"
    lines += [f"accepted_per_quota {apq}", f"post_accept_failures {paf}"]
    b = results.get("B")
    if type(b) is dict and b.get("outcome") == "accepted":
        lines.append("B accepted: false PASS")
    ok = all(v == "PASS" for v in verdicts.values()) and "missing" not in (apq, paf)
    lines.append(f"OVERALL {'PASS' if ok else 'FAIL'}")
    return ok, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and score the pinned M6 scenarios.")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--out", help="also write the score here (never under dev/n6/acceptance/)")
    args = parser.parse_args(argv)
    try:
        out = out_path(args.out) if args.out else None
        scenarios = load_scenarios(args.scenarios)
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return 2
    ok, text = score(run_all(scenarios))
    print(text)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
