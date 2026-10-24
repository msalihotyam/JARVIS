"""Conveyor worker adapters (WORKERADAPT, O45): the tool a slice runs on.

"claude" (default) is the conveyor's own `claude --bg` code in conveyor.py, unchanged.
"codex" runs `codex exec --json` headless in the lane's git worktree with
`--sandbox workspace-write` (never weaker), on the same route bar as jarvis/codex.py
(WORKERFIX): ChatGPT login forced, user config ignored, API-key/provider variables
stripped from the child, and a fresh account reading must show the subscription route,
the limit not reached and no credits before a lane starts (no paid usage, fail closed).
Its interface is admit() / start() / status() / violation() / stop() / collect(); the
conveyor trusts only build/reports/<ID>.md, the diff and its own test gate. Nothing
here calls a model; admit()'s default reader is jarvis.codex.codex_state (the local
app-server account/limits read).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKERS = ("claude", "codex")
CODEX_MIN_HEADROOM = 10  # % left in the tighter window; less (or unknown) = no dispatch
CODEX_STATE_MAX_AGE_S = 60  # an account/limits reading older than this is stale = refused
CODEX_LOG_DIR = "build/conveyor/codex"
SANDBOXES = ("read-only", "workspace-write")  # never danger-full-access
FORCED_LOGIN = "forced_login_method=chatgpt"  # the one value -c may carry (jarvis/codex.py)
# A flag that can widen the sandbox, skip approvals, load other config or buy credits.
BANNED_FLAGS = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
                "-c", "--config", "-p", "--profile", "--add-dir", "--oss")
BANNED_WORDS = ("danger", "credit", "overage", "bypass")
# jarvis/codex.py CODEX_PROHIBITED_ENV_VARS: any of these can move Codex off the
# ChatGPT subscription onto a metered key or another provider.
PROHIBITED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
                  "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID", "AZURE_OPENAI_API_KEY")


def check_argv(argv):
    """Raise ValueError unless argv runs `codex exec` with exactly one sandbox no weaker
    than workspace-write, `--ignore-user-config` and `-c forced_login_method=chatgpt`
    exactly once each, and no other config or flag that widens it or buys credits."""
    if len(argv) < 2 or argv[1] != "exec":
        raise ValueError("not codex exec")
    sandboxes, logins, ignores, skip = [], 0, 0, False
    for i, a in enumerate(argv[1:], 1):
        if skip:
            skip = False
            continue
        if argv[i - 1] == "-C" and not a.startswith("-"):
            continue  # the worktree path: a lane id like BYPASSFIX is no flag
        if a == "-c":  # exact-match allowlist: nothing else may ride on -c
            if argv[i + 1:i + 2] != [FORCED_LOGIN]:
                raise ValueError(f"refused config: {argv[i + 1:i + 2]}")
            logins, skip = logins + 1, True
            continue
        if a == "--ignore-user-config":
            ignores += 1
            continue
        flag = a.split("=", 1)[0]  # case-sensitive: -C is the worktree, -c is config
        # a[:2] also refuses attached short values (-cKEY=V, -pPROFILE)
        if flag in BANNED_FLAGS or a[:2] in ("-c", "-p") or a.startswith("--ignore-user-config") \
                or any(w in a.lower() for w in BANNED_WORDS):
            raise ValueError(f"refused argument: {a}")
        if flag in ("-s", "--sandbox"):
            sandboxes.append(a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else ""))
    if logins != 1 or ignores != 1:
        raise ValueError(f"needs --ignore-user-config and -c {FORCED_LOGIN} exactly once")
    if len(sandboxes) != 1 or sandboxes[0] not in SANDBOXES:
        raise ValueError(f"sandbox must be one of {SANDBOXES}: {sandboxes}")
    return argv


def route_var(key):
    """True for an environment variable that can pick Codex's route, key or provider
    (case-insensitive, as Windows names are). CODEX_HOME stays: it holds the login."""
    u = key.upper()
    return u != "CODEX_HOME" and (u in PROHIBITED_ENV or u.startswith(("OPENAI_", "AZURE_OPENAI", "CODEX_"))
                                  or u.endswith(("_API_KEY", "_BASE_URL")))


def scrubbed_env(environ=None):
    """A copy of the environment without any route variable, for the codex child."""
    return {k: v for k, v in (os.environ if environ is None else environ).items() if not route_var(k)}


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


def _limit_hit(obj, top=True):
    """Why one --json event says the run hit its limit or reached credits, else None.
    Only codex's own fields and error events count, never the agent's message text."""
    if isinstance(obj, dict):
        if top and obj.get("type") in ("error", "turn.failed"):
            text = json.dumps(obj).lower()
            if "credit" in text:
                return "credits"
            if any(w in text for w in ("usage_limit", "usage limit", "rate limit", "rate_limit", "429",
                                       "quota", "billing")):
                return "limit_reached"
        for k, v in obj.items():
            if k in ("rate_limit_reached_type", "rateLimitReachedType") and v is not None:
                return "limit_reached"
            if k in ("has_credits", "hasCredits") and v is True:
                return "credits"
            if k in ("used_percent", "usedPercent") and not isinstance(v, bool) \
                    and isinstance(v, (int, float)) and v >= 100:
                return "limit_reached"
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            why = _limit_hit(v, False)
            if why:
                return why
    return None


def _jarvis_codex():
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import jarvis.codex
    return jarvis.codex


def _codex_state():
    """jarvis.codex.codex_state(): account and limits read fresh from the local app-server."""
    return _jarvis_codex().codex_state(cached_catalog=True)


def _codex_exe():
    """jarvis.codex.codex_executable(): the native binary found by provenance, never the
    npm .cmd launcher (stopping a launcher would leave its codex.exe running), else None."""
    return _jarvis_codex().codex_executable()


class CodexWorker:
    """`codex exec` lanes. Session ids are "codex-<pid>"; the Popen handles live in this
    process only, so after a conveyor restart a session's status is "unknown"."""

    def __init__(self, root, exe=None, popen=subprocess.Popen, now=time.time, reader=_codex_state,
                 environ=None, run=subprocess.run):
        self.root = Path(root)
        self.exe = exe
        self.popen = popen
        self.run = run  # stop(): taskkill /T on Windows
        self.now = now
        self.reader = reader
        self.environ = environ  # None = os.environ at start(), scrubbed
        self.log_dir = self.root / CODEX_LOG_DIR
        self.procs = {}
        self.offsets = {}  # lane -> byte offset of this run's first unread event

    def argv(self, worktree):
        exe = self.exe or _codex_exe()
        if not exe:
            raise FileNotFoundError("no trusted native codex binary")
        return check_argv([exe, "exec", "--json", "--ignore-user-config", "-c", FORCED_LOGIN,
                           "-C", str(worktree), "--sandbox", "workspace-write", "-"])  # prompt on stdin

    def admit(self):
        """(headroom, None) when a FRESH account reading shows the ChatGPT subscription
        route, the limit not reached, no credits and enough headroom; otherwise
        (headroom or None, reason). Unknown, failed or stale = refused (fail closed)."""
        try:
            state = self.reader()
        except Exception:  # nosec B110 - a failed reading is a refusal, never a start
            return None, "unknown"
        if not isinstance(state, dict):
            return None, "unknown"
        st = state.get("status")
        if st != "available":
            return None, st[:32] if isinstance(st, str) and st else "unknown"
        quota = state.get("quota")
        if not isinstance(quota, dict):
            return None, "unknown"
        at = quota.get("checked_at")
        if type(at) is not int or not 0 <= self.now() - at <= CODEX_STATE_MAX_AGE_S:
            return None, "stale"
        if "limit_reached" in quota:
            return None, "limit_reached"
        if quota.get("has_credits") is not False:  # credits could be spent, or unknown
            return None, "has_credits" if quota.get("has_credits") is True else "unknown"
        h = headroom(quota)
        if h is None:
            return None, "unknown"
        return (h, None) if h >= CODEX_MIN_HEADROOM else (h, "low")

    def start(self, name, worktree, text):
        """Start `codex exec` in `worktree` with the prompt on stdin and a scrubbed
        environment; the --json events go to build/conveyor/codex/<name>.jsonl.
        Returns "codex-<pid>"; raises on failure."""
        argv = self.argv(worktree)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        prompt = self.log_dir / f"{name}.prompt.md"
        prompt.write_bytes(text.encode("utf-8"))  # byte-exact: no CRLF translation on Windows
        with open(prompt, "rb") as fin, open(self.log_dir / f"{name}.jsonl", "ab") as fout, \
                open(self.log_dir / f"{name}.stderr.log", "ab") as ferr:
            self.offsets[name] = fout.tell()  # an earlier run's events are not this run's
            p = self.popen(argv, cwd=str(worktree), stdin=fin, stdout=fout, stderr=ferr,
                           env=scrubbed_env(self.environ),
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

    def violation(self, name):
        """Why this run must stop now (its new events report the limit reached or
        credits), else None. An unreadable or truncated log is a stop: credits are never
        spent unseen."""
        try:
            with open(self.log_dir / f"{name}.jsonl", "rb") as f:
                if f.seek(0, os.SEEK_END) < self.offsets.get(name, 0):
                    return "log_truncated"
                f.seek(self.offsets.get(name, 0))
                data = f.read()
        except OSError:
            return "log_unreadable"
        done = data.rfind(b"\n") + 1  # a half-written last line is read next time
        self.offsets[name] = self.offsets.get(name, 0) + done
        for line in data[:done].splitlines():
            try:
                why = _limit_hit(json.loads(line))
            except ValueError:
                continue
            if why:
                return why
        return None

    def stop(self, sid):
        """Terminate a running session and, on Windows, its whole process tree; True when
        one was running."""
        p = self.procs.get(sid)
        if p is None or p.poll() is not None:
            return False
        if os.name == "nt":  # by full path: a taskkill.exe in the cwd is never run
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            self.run([str(taskkill), "/PID", str(p.pid), "/T", "/F"], capture_output=True, check=False)
        p.terminate()
        return True

    def collect(self, name, worktree):
        """The lane's report file (worktree first, then the canonical checkout), else None."""
        for base in (Path(worktree), self.root):
            f = base / "build" / "reports" / f"{name}.md"
            if f.is_file():
                return f
        return None
