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
import signal
import subprocess
import sys
import time
from pathlib import Path

WORKERS = ("claude", "codex")
CODEX_MIN_HEADROOM = 10  # % left in the tighter window; less (or unknown) = no dispatch
CODEX_STATE_MAX_AGE_S = 60  # an account/limits reading older than this is stale = refused
CODEX_LOG_DIR = "build/conveyor/codex"
SANDBOXES = ("read-only", "workspace-write")  # never danger-full-access
FORCED_LOGIN = "forced_login_method=chatgpt"  # jarvis/codex.py's route bar
# CODEXSANDBOX: with --ignore-user-config no Windows sandbox is selected and every shell
# command is "blocked by policy"; mxc needs no admin and confines writes to the worktree.
WINDOWS_SANDBOX = "windows.sandbox=mxc"
CONFIGS = (FORCED_LOGIN, WINDOWS_SANDBOX)  # the only values -c may carry, each exactly once
# A flag that can widen the sandbox, skip approvals, load other config or buy credits.
BANNED_FLAGS = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
                "-c", "--config", "-p", "--profile", "--add-dir", "--oss",
                "--enable", "--disable", "--cd")  # --enable/--disable X = -c features.X=bool
BANNED_WORDS = ("danger", "credit", "overage", "bypass")
# jarvis/codex.py CODEX_PROHIBITED_ENV_VARS: any of these can move Codex off the
# ChatGPT subscription onto a metered key or another provider.
PROHIBITED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
                  "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID", "AZURE_OPENAI_API_KEY")


def check_argv(argv, worktree=None):
    """Raise ValueError unless argv runs `codex exec` with exactly one sandbox no weaker
    than workspace-write, `--ignore-user-config`, `-c forced_login_method=chatgpt` and
    `-c windows.sandbox=mxc` exactly once each, and no other config or flag that widens
    it or buys credits; given `worktree`, `-C` must name exactly that path, once."""
    if len(argv) < 2 or argv[1] != "exec":
        raise ValueError("not codex exec")
    sandboxes, configs, cds, ignores, skip = [], [], [], 0, False
    for i, a in enumerate(argv[1:], 1):
        if skip:
            skip = False
            continue
        if argv[i - 1] == "-C" and not a.startswith("-"):
            cds.append(a)
            continue  # the worktree path: a lane id like BYPASSFIX is no flag
        if a == "-c":  # exact-match allowlist: nothing else may ride on -c
            value = argv[i + 1] if i + 1 < len(argv) else None
            if value not in CONFIGS:
                raise ValueError(f"refused config: {argv[i + 1:i + 2]}")
            configs.append(value)
            skip = True
            continue
        if a == "--ignore-user-config":
            ignores += 1
            continue
        flag = a.split("=", 1)[0]  # case-sensitive: -C is the worktree, -c is config
        # a[:2] also refuses attached short values (-cKEY=V, -pPROFILE)
        if flag in BANNED_FLAGS or a[:2] in ("-c", "-p") or (a[:2] == "-C" and a != "-C") \
                or a.startswith("--ignore-user-config") \
                or any(w in a.lower() for w in BANNED_WORDS):
            raise ValueError(f"refused argument: {a}")
        if flag in ("-s", "--sandbox"):
            sandboxes.append(a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else ""))
    if sorted(configs) != sorted(CONFIGS) or ignores != 1:
        raise ValueError(f"needs --ignore-user-config and -c {' -c '.join(CONFIGS)} exactly once")
    if len(sandboxes) != 1 or sandboxes[0] not in SANDBOXES:
        raise ValueError(f"sandbox must be one of {SANDBOXES}: {sandboxes}")
    norm = lambda p: os.path.normcase(os.path.abspath(str(p)))  # noqa: E731
    if worktree is not None and [norm(c) for c in cds] != [norm(worktree)]:
        raise ValueError(f"-C must be the lane worktree {worktree} exactly once: {cds}")
    return argv


def route_var(key):
    """True for an environment variable that can pick Codex's route, key or provider
    (case-insensitive, as Windows names are). CODEX_HOME stays: it holds the login."""
    u = key.upper()
    return u != "CODEX_HOME" and (u in PROHIBITED_ENV or u.startswith(("OPENAI_", "AZURE_OPENAI", "CODEX_"))
                                  or u.endswith(("_API_KEY", "_BASE_URL")))


def scrubbed_env(environ=None):
    """The codex child's environment as an ALLOWLIST (WORKERADAPT2 H5), mirroring
    jarvis/codex.py _environment(): trusted_environment() with CODEX_ALLOWED_ENV_VARS /
    _PREFIXES (sanitized PATH), then no route variable. An import failure raises: no start."""
    jc = _jarvis_codex()
    from jarvis.trusted_exec import trusted_environment
    env = trusted_environment(allow=jc.CODEX_ALLOWED_ENV_VARS, prefixes=jc.CODEX_ALLOWED_ENV_PREFIXES,
                              environ=os.environ if environ is None else environ)
    return {k: v for k, v in env.items() if not route_var(k)}


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


# The only keys _limit_hit descends into: codex's event / rate_limits structures. An "item"
# (tool call, command output, agent message) is never read (WORKERADAPT2 H6).
_LIMIT_PATH = ("rate_limits", "rateLimits", "primary", "secondary", "credits", "msg", "payload", "info")


def _limit_hit(obj, top=True):
    """Why one --json event says the run hit its limit or reached credits, else None.
    Only codex's own error text and rate_limits fields count, never a tool payload."""
    if not isinstance(obj, dict):
        return None
    if top and obj.get("type") in ("error", "turn.failed"):
        text = json.dumps([obj.get("message"), obj.get("error")]).lower()
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
    for k in _LIMIT_PATH:
        why = _limit_hit(obj.get(k), False)
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


def _proc_start(pid):
    """The creation time (FILETIME ticks) of a LIVE process, None when no such process is
    running; raises when it cannot be told (access denied, not Windows)."""
    if os.name != "nt":
        raise OSError("process start time is read on Windows only")
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such pid
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value != 259:  # STILL_ACTIVE
            return None
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        k32.CloseHandle(h)


class CodexWorker:
    """`codex exec` lanes. Session ids are "codex-<pid>"; the Popen handles live in this
    process only. start() leaves pid + creation time + log in `info` for the conveyor to
    persist; after a restart attach() re-attaches a session only when the pid is alive with
    the same creation time (WORKERADAPT2 H4)."""

    def __init__(self, root, exe=None, popen=subprocess.Popen, now=time.time, reader=_codex_state,
                 environ=None, run=subprocess.run, proc_start=_proc_start):
        self.root = Path(root)
        self.exe = exe
        self.popen = popen
        self.run = run  # stop(): taskkill /T on Windows
        self.now = now
        self.reader = reader
        self.environ = environ  # None = os.environ at start(), scrubbed
        self.proc_start = proc_start
        self.log_dir = self.root / CODEX_LOG_DIR
        self.procs = {}
        self.info = {}  # sid -> {pid, start, log, offset}: what lanes.json keeps
        self.attached = {}  # sid -> (pid, start or None) re-attached after a restart
        self.offsets = {}  # lane -> byte offset of this run's first unread event

    def argv(self, worktree):
        exe = self.exe or _codex_exe()
        if not exe:
            raise FileNotFoundError("no trusted native codex binary")
        return check_argv([exe, "exec", "--json", "--ignore-user-config", "-c", FORCED_LOGIN,
                           "-c", WINDOWS_SANDBOX, "-C", str(worktree), "--sandbox",
                           "workspace-write", "-"], worktree=worktree)  # prompt on stdin

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
        try:
            st = self.proc_start(p.pid)
        except Exception:  # nosec B110 - no start time = unverifiable after a restart
            st = None
        if type(st) is not int:  # never run a session a restart could not verify (fail closed)
            if p.poll() is None:  # our handle is open: the pid cannot have been reused
                self._kill(p.pid)
                p.terminate()
            del self.procs[sid]
            raise RuntimeError("codex start: no process creation time, a restart could not verify it")
        self.info[sid] = {"pid": p.pid, "start": st, "log": str(self.log_dir / f"{name}.jsonl"),
                          "offset": self.offsets[name]}
        return sid

    def attach(self, name, sid, info):
        """Re-attach a session after a conveyor restart from its persisted `info`:
        "attached" (pid alive, same creation time), "gone" (no such live process: it
        finished) or "unverified" (cannot be told: stop it, fail closed)."""
        info = info if isinstance(info, dict) else {}
        off, pid, start = info.get("offset"), info.get("pid"), info.get("start")
        if type(off) is int and off >= 0:
            self.offsets[name] = off  # the violation check rescans this whole run
        if type(pid) is not int or pid <= 0:
            return "unverified"
        try:
            now = self.proc_start(pid)
        except Exception:  # nosec B110 - unreadable = unverified
            self.attached[sid] = (pid, None)
            return "unverified"
        if now is None:
            return "gone"
        if type(start) is not int:
            self.attached[sid] = (pid, None)
            return "unverified"
        if now != start:
            return "gone"  # the pid was reused: ours has finished
        self.attached[sid] = (pid, start)
        return "attached"

    def status(self, sid):
        p = self.procs.get(sid)
        if p is None:
            a = self.attached.get(sid)
            if a is None or a[1] is None:
                return "unknown"
            try:
                return "running" if self.proc_start(a[0]) == a[1] else "exited:?"
            except Exception:  # nosec B110 - a verified session briefly unreadable: still ours, alive
                return "running"
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
        if p is None:
            a = self.attached.get(sid)
            if a is None or (a[1] is not None and self.status(sid) != "running"):
                self.attached.pop(sid, None)
                return False
            self.attached.pop(sid)
            self._kill(a[0])  # re-attached, or unverified: stopped all the same (fail closed)
            return True
        if p.poll() is not None:
            return False
        self._kill(p.pid)
        p.terminate()
        return True

    def _kill(self, pid):
        if os.name == "nt":  # by full path: a taskkill.exe in the cwd is never run
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            self.run([str(taskkill), "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def collect(self, name, worktree):
        """The lane's report file (worktree first, then the canonical checkout), else None."""
        for base in (Path(worktree), self.root):
            f = base / "build" / "reports" / f"{name}.md"
            if f.is_file():
                return f
        return None
