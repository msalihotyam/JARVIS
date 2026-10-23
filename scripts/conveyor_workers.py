"""Conveyor worker adapters (WORKERADAPT, O45): the tool a slice runs on.

"claude" (default) is the conveyor's own `claude --bg` code in conveyor.py, unchanged.
"codex" runs `codex exec --json` headless in the lane's git worktree with
`--sandbox workspace-write` (never weaker). Its interface is usage() / start() /
status() / stop() / collect(); the conveyor trusts only build/reports/<ID>.md, the diff
and its own test gate. Stdlib only; nothing here calls a model, network or auth API.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

WORKERS = ("claude", "codex")
CODEX_MIN_HEADROOM = 10  # % left in the tighter window; less (or unknown) = no dispatch
CODEX_USAGE_MAX_AGE_S = 3600  # a rate_limits snapshot older than this is unknown
CODEX_LOG_DIR = "build/conveyor/codex"
SANDBOXES = ("read-only", "workspace-write")  # never danger-full-access
# A flag that can widen the sandbox, skip approvals, load other config or buy credits.
BANNED_FLAGS = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
                "-c", "--config", "-p", "--profile", "--add-dir", "--oss")
BANNED_WORDS = ("danger", "credit", "overage", "bypass")


def check_argv(argv):
    """Raise ValueError unless argv runs `codex exec` with exactly one sandbox no weaker
    than workspace-write and no flag that widens it or buys credits/overage."""
    if len(argv) < 2 or argv[1] != "exec":
        raise ValueError("not codex exec")
    sandboxes = []
    for i, a in enumerate(argv[1:], 1):
        if argv[i - 1] == "-C" and not a.startswith("-"):
            continue  # the worktree path: a lane id like BYPASSFIX is no flag
        flag = a.split("=", 1)[0]  # case-sensitive: -C is the worktree, -c is config
        # a[:2] also refuses attached short values (-cKEY=V, -pPROFILE)
        if flag in BANNED_FLAGS or a[:2] in ("-c", "-p") or any(w in a.lower() for w in BANNED_WORDS):
            raise ValueError(f"refused argument: {a}")
        if flag in ("-s", "--sandbox"):
            sandboxes.append(a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else ""))
    if len(sandboxes) != 1 or sandboxes[0] not in SANDBOXES:
        raise ValueError(f"sandbox must be one of {SANDBOXES}: {sandboxes}")
    return argv


def _rate_limits(obj):
    """The `rate_limits` dict nested anywhere in one JSON event, else None."""
    if isinstance(obj, dict):
        if isinstance(obj.get("rate_limits"), dict):
            return obj["rate_limits"]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            r = _rate_limits(v)
            if r is not None:
                return r
    return None


def headroom(rl):
    """100 - the highest used_percent of the primary/secondary windows; None when either
    present window is not a number in 0..100 or no window is present (fail closed)."""
    used = []
    for k in ("primary", "secondary"):
        w = rl.get(k)
        if w is None:
            continue
        p = w.get("used_percent") if isinstance(w, dict) else None
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 100:
            return None
        used.append(p)
    return 100 - max(used) if used else None


class CodexWorker:
    """`codex exec` lanes. Session ids are "codex-<pid>"; the Popen handles live in this
    process only, so after a conveyor restart a session's status is "unknown"."""

    def __init__(self, root, exe=None, popen=subprocess.Popen, sessions_dir=None, now=time.time):
        self.root = Path(root)
        self.exe = exe
        self.popen = popen
        self.now = now
        home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        self.sessions_dir = Path(sessions_dir) if sessions_dir else Path(home) / "sessions"
        self.log_dir = self.root / CODEX_LOG_DIR
        self.procs = {}

    def argv(self, worktree):
        exe = self.exe or shutil.which("codex")
        if not exe:
            raise FileNotFoundError("codex not on PATH")
        return check_argv([exe, "exec", "--json", "-C", str(worktree),
                           "--sandbox", "workspace-write", "-"])  # prompt on stdin

    def usage(self):
        """Live remaining % from the newest rollout/lane JSONL that carries a rate_limits
        snapshot (never summed). Missing, unreadable, malformed or older than
        CODEX_USAGE_MAX_AGE_S -> None = no headroom."""
        # ponytail: rglob of ~/.codex/sessions each pass; index by date dirs if it grows slow
        try:
            files = [p for d in (self.sessions_dir, self.log_dir) if d.is_dir()
                     for p in d.rglob("*.jsonl")]
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for p in files:
            try:
                if self.now() - p.stat().st_mtime > CODEX_USAGE_MAX_AGE_S:
                    return None  # newest first: every later file is older still
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return None
            for line in reversed(lines):
                try:
                    rl = _rate_limits(json.loads(line))
                except ValueError:
                    continue
                if rl is not None:
                    return headroom(rl)
        return None

    def start(self, name, worktree, text):
        """Start `codex exec` in `worktree` with the prompt on stdin; the --json events go
        to build/conveyor/codex/<name>.jsonl. Returns "codex-<pid>"; raises on failure."""
        argv = self.argv(worktree)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        prompt = self.log_dir / f"{name}.prompt.md"
        prompt.write_bytes(text.encode("utf-8"))  # byte-exact: no CRLF translation on Windows
        with open(prompt, "rb") as fin, open(self.log_dir / f"{name}.jsonl", "ab") as fout, \
                open(self.log_dir / f"{name}.stderr.log", "ab") as ferr:
            p = self.popen(argv, cwd=str(worktree), stdin=fin, stdout=fout, stderr=ferr,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sid = f"codex-{p.pid}"
        self.procs[sid] = p
        return sid

    def status(self, sid):
        p = self.procs.get(sid)
        if p is None:
            return "unknown"
        rc = p.poll()
        return "running" if rc is None else f"exited:{rc}"

    def stop(self, sid):
        """Terminate a running session; True when one was running."""
        p = self.procs.get(sid)
        if p is None or p.poll() is not None:
            return False
        p.terminate()
        return True

    def collect(self, name, worktree):
        """The lane's report file (worktree first, then the canonical checkout), else None."""
        for base in (Path(worktree), self.root):
            f = base / "build" / "reports" / f"{name}.md"
            if f.is_file():
                return f
        return None
