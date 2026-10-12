"""Parallel runner for the JARVIS test suite (stdlib only).

Runs the SAME tests as ``python -m unittest discover -s tests`` as several
``python -m unittest -v <names>`` processes and prints one combined summary.
The serial run stays the reference when a result is disputed.

* Discovery first, in its own process: every module's TestCase classes and
  their test ids, checked against the serial ``discover -s tests`` set (the
  same ids, the same number of times) before anything runs.
* Work units are modules; a module whose recorded duration is large is split
  at TestCase level into duration-balanced chunks (``scripts/test_durations.json``,
  ``--write-durations``), and a long class goes into them one test method at a
  time. A module with ``load_tests`` is never split.
* Units that drive a real QML window or launch the app (modules importing
  PySide6 / jarvis.ui, directly or through another test module) run over K UI
  processes (default ``min(4, cpu_count // 3)``, at least 1); the rest over N
  workers (default ``min(cpu_count - 1, 6)``); longest first. Every process
  has its own TEMP / TMP / TMPDIR and, being a process, its own QApplication.
  ``tests/__init__.py`` (the live-database guard) is imported by every
  ``tests.*`` module, so the guard holds in every process.
* Exit code 0 only when every unit passed AND the union of dispatched test ids
  equals the discovered set exactly AND every process ran exactly the tests it
  was given (its ``Ran N`` count and every id in its ``-v`` output). A failure
  that is ONLY on the load-sensitive list in JARVIS_ACTIVE_STATE.md is rerun
  alone once; if all of those pass, the run counts as green and both results
  are printed (rule D).
* CI: ``--shard I/N`` runs shard I of a deterministic plan (the recorded
  durations, longest first onto the least-loaded shard; units without a
  recorded duration last, each onto the least-loaded shard) and ``--report``
  writes what it dispatched and ran (with every test's seconds);
  ``--aggregate DIR --shards N`` checks all the shard reports against a fresh
  discovery and the same plan and prints one combined summary, and with
  ``--write-durations`` records CI's own seconds for the next plan. It prints
  every skip with its reason and fails on any skip not on ``ALLOWED_SKIPS``.
* CONVUI: the UI units of one run at a time on this machine (Salih 2026-10-12,
  "arayuz testleri sirayla"). A run with UI units takes ``build/locks/ui.json``
  of the main checkout before its first UI process and frees it when its last
  UI unit ends (or on an error); meanwhile another run's UI processes wait in the
  foreground and its non-UI units go on. A full suite holding the A1 suite lock
  (``build/locks/suite.json``) of another session counts as holding it; a holder
  whose pid is gone is taken over. No lock under CI (the ``CI`` variable) or with
  ``--no-ui-lock``.

    python scripts/test_parallel.py [--workers N] [--ui-workers K] [--durations N] [--no-rerun]
                                    [--python PATH] [--shard I/N] [--report PATH] [--no-ui-lock]
                                    [--write-durations PATH] [modules ...]
    python scripts/test_parallel.py --aggregate DIR --shards N [--durations N] [--write-durations PATH]
"""

from __future__ import annotations

import argparse
import ast
import collections
import concurrent.futures as futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
STATE = ROOT / "JARVIS_ACTIVE_STATE.md"
DURATIONS = Path(__file__).resolve().with_name("test_durations.json")

#: A module recorded above this many seconds is split into chunks of about this size.
CHUNK_SECONDS = 60.0
#: THIZ: a class recorded above this many seconds goes into those chunks one test method at a time.
SPLIT_CLASS_SECONDS = CHUNK_SECONDS / 4

_UI_MARKERS = re.compile(r"^\s*(?:from|import)\s+(?:PySide6|jarvis\.ui)\b", re.MULTILINE)
_TEST_IMPORT = re.compile(r"^\s*(?:from|import)\s+tests\.(test_\w+)", re.MULTILINE)
_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.MULTILINE)
_COUNTS = re.compile(r"^(?:OK|FAILED)(?: \(([^)]*)\))?\s*$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)
_DURATION = re.compile(r"^\s*(\d+\.\d+)s\s+(\S.*)$", re.MULTILINE)
_SEEN = re.compile(r"\b(\w+) \((tests\.[\w.]+)\)")
_SKIPPED = re.compile(r" \.\.\. skipped (.*)$", re.MULTILINE)

#: V2.1 C-5: the only skips CI's aggregate accepts, test id -> the reason it must skip with
#: (a skipped schema-10 rollback proof must never pass as green). Any other skip fails it.
ALLOWED_SKIPS = {
    "tests.test_local_file_write.ContainmentTests.test_the_posix_parent_walk_is_no_follow_at_every_step":
        "no directory-handle traversal",
    "tests.test_ui.LocalizationTests.test_turkish_catalog_covers_every_marked_string":
        "the PySide6 Linguist tools are not installed",
    # CI's windows-latest runner has no gws; locally these run against the installed CLI.
    "tests.test_gmail_minimum.LiveProbeTests.test_the_installed_cli_can_read_a_held_credential_but_not_remove_it":
        "the Google Workspace CLI is not installed here",
    "tests.test_gmail_minimum.LiveProbeTests.test_the_installed_cli_refuses_an_empty_dedicated_directory":
        "the Google Workspace CLI is not installed here",
    "tests.test_gmail_minimum.LiveProbeTests.test_the_installed_cli_rehearses_both_writes_as_bodiless_multipart_uploads":
        "the Google Workspace CLI is not installed here",
    # Gate 6G C-5: tool- or font-dependent skips the windows-latest image may hit (allowed when unsure).
    "tests.test_dffont.FaceTests.test_freetype_gets_the_variable_face_not_the_static_fallback":
        "Windows 11 Segoe UI Variable",
    "tests.test_dffont.FaceTests.test_directwrite_keeps_its_own_family":
        "Windows 11 Segoe UI Variable",
    "tests.test_trusted_exec.ChromeTrustTests.test_the_real_system_chrome_resolves_when_installed":
        "Google Chrome is not installed for this machine",
    "tests.test_trusted_exec.SpawnShapeTests.test_the_connector_and_the_intent_adapter_both_spawn_an_absolute_path":
        "the Claude CLI is not installed on this machine",
    "tests.test_trusted_exec.LegitimateAvailabilityTests.test_an_installed_tool_on_the_real_path_still_resolves":
        "no trusted CLI is installed on this machine",
    "tests.test_trusted_exec.LegitimateAvailabilityTests.test_a_real_resolution_survives_a_spawn_shaped_revalidation":
        "no trusted CLI is installed on this machine",
}


def discover(selected: list[str]) -> list[str]:
    names = sorted(p.stem for p in TESTS.glob("test_*.py"))
    if selected:
        wanted = {s.removeprefix("tests.") for s in selected}
        names = [n for n in names if n in wanted]
    return names


def ui_modules(names: list[str]) -> set[str]:
    """Modules that import PySide6 / jarvis.ui, directly or via another test module."""
    every = sorted(p.stem for p in TESTS.glob("test_*.py"))
    source = {n: (TESTS / f"{n}.py").read_text(encoding="utf-8", errors="replace") for n in every}
    ui = {n for n, text in source.items() if _UI_MARKERS.search(text)}
    changed = True
    while changed:
        changed = False
        for n, text in source.items():
            if n not in ui and any(m in ui for m in _TEST_IMPORT.findall(text)):
                ui.add(n)
                changed = True
    return ui & set(names)


def load_sensitive() -> set[str]:
    """Test ids (``module.Class.test_name``) the orchestrator listed as load-sensitive: only the
    "Load-sensitive list" item of the state file counts, never an id mentioned elsewhere in it."""
    try:
        text = STATE.read_text(encoding="utf-8")
    except OSError:
        return set()
    section = re.search(r"^- Load-sensitive list.*?(?=^- |^#|\Z)", text, re.MULTILINE | re.DOTALL)
    flat = re.sub(r"\s+", " ", section.group(0)) if section else ""
    return set(re.findall(r"\b(test_\w+\.\w+\.test_\w+)", flat))


# -- discovery (in its own process) ---------------------------------------------

def _discover_child(modules: list[str], serial: bool) -> dict:
    """``{module: {"split": bool, "classes": {attr: [ids]}}}`` as each worker loads it, and the
    serial ``discover -s tests`` id multiset when ``serial``. Runs in a child process."""
    import importlib
    import unittest

    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    loader = unittest.TestLoader()
    out: dict = {"modules": {}, "errors": []}

    def ids(suite) -> list[str]:
        found = []
        for item in suite:
            found.extend(ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
        return found

    for name in modules:
        try:
            module = importlib.import_module(f"tests.{name}")
        except Exception as exc:  # noqa: BLE001 - reported, the run is red
            out["errors"].append(f"tests.{name}: {type(exc).__name__}: {exc}")
            continue
        if hasattr(module, "load_tests"):
            out["modules"][name] = {"split": False, "classes": {"": ids(loader.loadTestsFromModule(module))}}
            continue
        classes = {}
        for attr in dir(module):                     # the loader's own order and filter
            obj = getattr(module, attr)
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
                found = ids(loader.loadTestsFromTestCase(obj))
                if found:
                    classes[attr] = found
        out["modules"][name] = {"split": True, "classes": classes}
    out["errors"].extend(str(e) for e in loader.errors)
    if serial:
        reference = unittest.TestLoader().discover("tests")
        out["serial"] = [i if i.startswith("tests.") else "tests." + i for i in ids(reference)]
    return out


def discover_tests(python: str, modules: list[str], serial: bool) -> dict:
    command = [python, str(Path(__file__).resolve()), "--_discover-child", "--_serial" if serial else "--_no-serial", *modules]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    base = Path(tempfile.mkdtemp(prefix="jarvis-test-discover-"))
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(base)
    try:
        done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    marker = done.stdout.rfind("\n@@DISCOVERY@@")
    if done.returncode != 0 or marker < 0:
        raise SystemExit(f"discovery failed (exit {done.returncode}):\n{done.stdout[-3000:]}{done.stderr[-3000:]}")
    return json.loads(done.stdout[marker + len("\n@@DISCOVERY@@"):])


# -- units and the plan -------------------------------------------------------------

class Unit:
    """One ``python -m unittest`` name list run as one process: a whole module or a chunk of its classes."""

    def __init__(self, module: str, attrs: list[str], ids: list[str], seconds: float | None, ui: bool):
        self.module, self.attrs, self.ids, self.seconds, self.ui = module, attrs, ids, seconds, ui

    @property
    def names(self) -> list[str]:
        return [f"tests.{self.module}"] if self.attrs == [""] else [f"tests.{self.module}.{a}" for a in self.attrs]

    @property
    def label(self) -> str:
        return self.names[0] if len(self.names) == 1 else f"tests.{self.module}[{self.attrs[0]}..{self.attrs[-1]}]"

    @property
    def weight(self) -> float:
        return self.seconds if self.seconds is not None else 0.0


def load_durations(path: Path = DURATIONS, key: str = "classes") -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).get(key, {}).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def build_units(discovery: dict, ui: set[str], durations: dict[str, float], tests: dict[str, float] | None = None) -> list[Unit]:
    """Whole modules, except a module recorded above CHUNK_SECONDS: its classes in chunks of
    about CHUNK_SECONDS each, longest first onto the least-loaded chunk. A class recorded above
    SPLIT_CLASS_SECONDS goes into the chunks one test method at a time (THIZ: its seconds from
    ``tests``, the recorded per-test seconds, else an equal share), so one long class is no
    longer one long unit. A unit's seconds are the sum of its parts' recorded seconds; None when
    any class has no record."""
    tests = tests or {}
    units = []
    for module, info in sorted(discovery["modules"].items()):
        per: dict[str, float | None] = {}
        for attr, ids in info["classes"].items():
            known = [durations.get(c) for c in sorted({t.rsplit(".", 1)[0] for t in ids})]
            per[attr] = None if any(k is None for k in known) else sum(known)
        total = None if any(v is None for v in per.values()) else sum(per.values())
        if not info["split"] or total is None or total <= CHUNK_SECONDS or len(per) == 1 and len(next(iter(info["classes"].values()))) == 1:
            ids = [t for attr_ids in info["classes"].values() for t in attr_ids]
            units.append(Unit(module, [""], ids, total, module in ui))
            continue
        members = {attr: info["classes"][attr] for attr in per}
        for attr, seconds in list(per.items()):
            ids = info["classes"][attr]
            if seconds > SPLIT_CLASS_SECONDS and len(ids) > 1:
                del per[attr], members[attr]
                for test in ids:
                    part = f"{attr}.{test.rsplit('.', 1)[1]}"
                    per[part], members[part] = tests.get(test, seconds / len(ids)), [test]
        pieces = max(2, round(total / CHUNK_SECONDS))
        bins: list[list[str]] = [[] for _ in range(pieces)]
        loads = [0.0] * pieces
        for attr in sorted(per, key=lambda a: (-per[a], a)):          # longest first onto the least-loaded chunk
            at = loads.index(min(loads))
            bins[at].append(attr)
            loads[at] += per[attr]
        for chunk, load in zip(bins, loads):
            if chunk:
                chunk.sort()
                ids = [t for attr in chunk for t in members[attr]]
                units.append(Unit(module, chunk, ids, load, module in ui))
    return units


def shard_plan(units: list[Unit], shards: int) -> list[list[Unit]]:
    """Deterministic. UI units and the others are balanced separately (a shard runs them side
    by side): recorded units longest first (ties by name) onto the shard least loaded with that
    kind (ties: the lowest index); then the units without a record, by name, onto the least-loaded
    shard overall."""
    plan: list[list[Unit]] = [[] for _ in range(shards)]
    loads = {kind: [0.0] * shards for kind in (True, False)}
    for kind in (True, False):
        for unit in sorted((u for u in units if u.seconds is not None and u.ui is kind), key=lambda u: (-u.seconds, u.label)):
            at = loads[kind].index(min(loads[kind]))
            plan[at].append(unit)
            loads[kind][at] += unit.seconds
    for unit in sorted((u for u in units if u.seconds is None), key=lambda u: u.label):
        total = [loads[True][i] + loads[False][i] for i in range(shards)]
        at = total.index(min(total))
        plan[at].append(unit)
        loads[unit.ui][at] += CHUNK_SECONDS / 4
    return plan


def discovered_ids(discovery: dict) -> collections.Counter:
    return collections.Counter(t for info in discovery["modules"].values() for ids in info["classes"].values() for t in ids)


# -- running ------------------------------------------------------------------------

class Result:
    def __init__(self, unit, command, returncode, output, wall):
        self.unit, self.command, self.returncode, self.output, self.wall = unit, command, returncode, output, wall
        self.label = unit.label if unit is not None else "rerun"
        ran = _RAN.search(output)
        self.ran = int(ran.group(1)) if ran else 0
        counts = {}
        tail = _COUNTS.findall(output)
        for part in (tail[-1] if tail else "").split(","):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                counts[key] = int(value)
        self.failures, self.errors, self.skipped = (counts.get(k, 0) for k in ("failures", "errors", "skipped"))
        self.parsed = bool(ran) and bool(tail)
        self.skips = _skips(output)
        # ("Class.test_name", "module") for every FAIL / ERROR line.
        self.failed = [(".".join(path.split(".")[-2:]), path.split(".")[-3] if path.count(".") >= 2 else "")
                       for path in (m.group(2) for m in _FAILED_TEST.finditer(output))]
        seen = {m.group(2) for m in _SEEN.finditer(output) if m.group(2).endswith("." + m.group(1))}
        # Exactly what it was given: the count, and every id it was given in its -v output.
        self.missing = sorted(set(unit.ids) - seen) if unit is not None else []
        self.accounted = unit is None or (self.parsed and self.ran == len(unit.ids) and not self.missing)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.parsed and not self.failures and not self.errors and self.accounted


def _skips(output: str) -> list[list[str]]:
    """[test id, reason] for every ``... skipped 'reason'`` line of a -v run; the id is the last
    one named before it (a test with a docstring prints it on the line before)."""
    seen = [(m.start(), m.group(2)) for m in _SEEN.finditer(output) if m.group(2).endswith("." + m.group(1))]
    skips = []
    for match in _SKIPPED.finditer(output):
        named = [test for at, test in seen if at < match.start()]
        try:
            reason = ast.literal_eval(match.group(1).strip())
        except Exception:  # noqa: BLE001 - any odd text is kept as it is, never ends the run
            reason = match.group(1).strip()
        skips.append([named[-1] if named else "?", str(reason)])
    return skips


#: CONVUI: the byte locked while a lock file is read and changed (past any record, so readers never block).
_CAS_OFFSET = 1 << 20
#: A1: a suite lock heartbeat older than this, or further ahead than _FUTURE_SKEW, is stale.
_SUITE_STALE = 45 * 60
_FUTURE_SKEW = 5 * 60
_SUITE_FIELDS = ("lane", "session", "started_at", "command", "heartbeat")


def lock_dir() -> Path:
    """``build/locks`` of the main checkout, shared by every worktree's runs (A1)."""
    try:
        done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8")
        common = done.stdout.strip() if done.returncode == 0 else ""
    except OSError:
        common = ""
    return (Path(common).parent if common else ROOT) / "build" / "locks"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    # os.kill(pid, 0) would END the process on Windows: ask for its exit code instead.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _cas(path: Path, change):
    """``change(text) -> (result, new_text | None)`` on the lock file, one run at a time (empty file = free)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        while True:
            os.lseek(fd, _CAS_OFFSET, os.SEEK_SET)
            try:
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, _CAS_OFFSET)
                break
            except OSError:
                time.sleep(0.05)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            result, new = change(os.read(fd, 1 << 16).decode("utf-8", "replace"))
            if new is not None:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, new.encode("utf-8"))
            return result
        finally:
            os.lseek(fd, _CAS_OFFSET, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.lockf(fd, fcntl.LOCK_UN, 1, _CAS_OFFSET)
    finally:
        os.close(fd)


def _plain(text: object, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9._:+-]", "_", str(text))[:limit]


class UiLock:
    """CONVUI: the machine-wide UI test lock, ``build/locks/ui.json`` (the A1 record plus ``pid``)."""

    poll = 2.0
    notice = 30.0

    def __init__(self, folder: Path, command: str):
        self.path, self.suite = folder / "ui.json", folder / "suite.json"
        self.session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.record = json.dumps({"lane": ROOT.name, "session": self.session, "pid": os.getpid(), "started_at": stamp,
                                  "command": command[:200], "heartbeat": stamp})
        self.held = False

    def _suite_holder(self) -> str | None:
        """Another session's live full suite (A1 rules: fresh heartbeat, all fields)."""
        try:
            rec = json.loads(self.suite.read_text(encoding="utf-8"))
            if not all(isinstance(rec.get(k), str) for k in _SUITE_FIELDS) or (self.session and rec["session"] == self.session):
                return None
            age = time.time() - datetime.fromisoformat(rec["heartbeat"].replace("Z", "+00:00")).timestamp()
        except (OSError, ValueError, AttributeError):
            return None  # no file, free (empty), mid-write or garbage: not a holder
        if not -_FUTURE_SKEW <= age <= _SUITE_STALE:
            return None
        return f"{_plain(rec['lane'])} (full suite) since {_plain(rec['started_at'])}"

    def _take(self, text: str):
        if text.strip() and text != self.record:
            try:
                rec = json.loads(text)
                pid = int(rec["pid"])
            except (ValueError, KeyError, TypeError):
                pid = 0  # garbage: stale
            if pid_alive(pid):
                return f"{_plain(rec.get('lane'))}/{pid} since {_plain(rec.get('started_at'))}", None
        return None, self.record

    def acquire(self) -> None:
        told = None
        while True:
            holder = self._suite_holder() or _cas(self.path, self._take)
            if holder is None:
                self.held = True
                return
            if told is None or time.monotonic() - told >= self.notice:
                print(f"waiting for the UI test lock held by {holder}", flush=True)
                told = time.monotonic()
            time.sleep(self.poll)

    def release(self) -> None:
        if self.held:
            self.held = False
            _cas(self.path, lambda text: (None, "" if text == self.record else None))


def ui_lock_for(args, units: list[Unit]) -> UiLock | None:
    if args.no_ui_lock or os.environ.get("CI") or not any(u.ui for u in units):
        return None
    return UiLock(lock_dir(), " ".join(["test_parallel.py", *sys.argv[1:]]))


def worker_env(index: int, bases: list[Path]) -> dict[str, str]:
    base = Path(tempfile.mkdtemp(prefix=f"jarvis-test-w{index}-"))
    bases.append(base)
    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(base)
    env["PYTHONUNBUFFERED"] = "1"
    # TUICRASH: a native crash (0xC0000005) prints the Python stack it hit, not just an exit code.
    env["PYTHONFAULTHANDLER"] = "1"
    return env


def run_unit(python: str, unit: Unit, env: dict[str, str], durations: int | None) -> Result:
    command = [python, "-m", "unittest", "-v", *unit.names]
    if durations is not None:
        command[4:4] = ["--durations", str(durations)]
    started = time.monotonic()
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return Result(unit, command, done.returncode, done.stdout + done.stderr, time.monotonic() - started)


def execute(args, units: list[Unit], ui_workers: int, workers: int,
            ui_lock: UiLock | None = None) -> tuple[list[Result], dict[str, float], float, list[Path]]:
    durations = 0 if args.write_durations or args.report else (args.durations or None)
    bases: list[Path] = []
    results: list[Result] = []
    lock, gate = threading.Lock(), threading.Lock()
    order = lambda u: (-(u.seconds if u.seconds is not None else CHUNK_SECONDS / 4), u.label)  # noqa: E731
    queues = {"ui": sorted((u for u in units if u.ui), key=order), "plain": sorted((u for u in units if not u.ui), key=order)}
    ui_left = len(queues["ui"])
    worker_wall: dict[str, float] = {}

    def worker(kind: str, index: int) -> None:
        nonlocal ui_left
        if kind == "ui" and ui_lock is not None:
            # CONVUI: UI processes start only under the machine-wide UI lock; the plain workers do not wait.
            with gate:
                if ui_left and not ui_lock.held:
                    ui_lock.acquire()
        env, began = worker_env(index, bases), time.monotonic()
        while True:
            with lock:
                # THIZ2: an idle UI process takes the leftover non-UI units (never the reverse);
                # every unit is its own subprocess, so nothing carries over between them.
                queue = queues[kind] or (queues["plain"] if kind == "ui" else [])
                if not queue:
                    break
                unit = queue.pop(0)
            result = run_unit(args.python, unit, env, durations)
            with lock:
                results.append(result)
                if unit.ui:
                    ui_left -= 1
                    if not ui_left and ui_lock is not None:
                        ui_lock.release()
        worker_wall[f"{kind}{index}"] = time.monotonic() - began

    started = time.monotonic()
    plan = [("ui", i) for i in range(1, ui_workers + 1)] + [("plain", i) for i in range(ui_workers + 1, ui_workers + workers + 1)]
    try:
        with futures.ThreadPoolExecutor(len(plan)) as pool:
            for job in [pool.submit(worker, kind, i) for kind, i in plan]:
                job.result()
    finally:
        if ui_lock is not None:
            ui_lock.release()
    return results, worker_wall, time.monotonic() - started, bases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="*", help="test modules (default: all of tests/)")
    parser.add_argument("--workers", type=int, default=0, help="non-UI workers (default min(cpu_count - 1, 6))")
    parser.add_argument("--ui-workers", type=int, default=0, help="UI processes at once (default min(4, cpu_count // 3), >= 1)")
    parser.add_argument("--durations", type=int, default=0, help="report the N slowest tests")
    parser.add_argument("--no-rerun", action="store_true", help="do not rerun load-sensitive failures alone")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the workers")
    parser.add_argument("--shard", default="", help="I/N: run shard I of N of the deterministic plan (CI)")
    parser.add_argument("--report", default="", help="write a JSON report of what was dispatched and ran")
    parser.add_argument("--write-durations", default="", help="record every test class's seconds to this JSON file")
    parser.add_argument("--aggregate", default="", help="check and combine the shard reports in this directory")
    parser.add_argument("--shards", type=int, default=0, help="with --aggregate: the number of shards")
    parser.add_argument("--no-ui-lock", action="store_true", help="do not wait for the machine-wide UI test lock (a deliberate manual run)")
    parser.add_argument("--_discover-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_serial", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_no-serial", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # A test's output may hold any character; a console code page (cp1254) must never end the run.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    if args._discover_child:
        print("\n@@DISCOVERY@@" + json.dumps(_discover_child(args.modules, args._serial)), flush=True)
        return 0
    if args.aggregate:
        return aggregate(args)

    names = discover(args.modules)
    if not names:
        print("no test modules found", file=sys.stderr)
        return 2
    shard = None
    if args.shard:
        match = re.fullmatch(r"(\d+)/(\d+)", args.shard)
        if not match or not 1 <= int(match.group(1)) <= int(match.group(2)) or args.modules:
            print("--shard must be I/N with 1 <= I <= N, over the whole suite", file=sys.stderr)
            return 2
        shard = (int(match.group(1)), int(match.group(2)))

    cpus = os.cpu_count() or 2
    workers = max(1, args.workers or min(cpus - 1, 6))
    ui_workers = max(1, args.ui_workers or min(4, cpus // 3))
    began = time.monotonic()
    discovery = discover_tests(args.python, names, serial=not args.modules)
    if discovery["errors"]:
        print("discovery errors:\n  " + "\n  ".join(discovery["errors"]))
        return 1
    expected = discovered_ids(discovery)
    if not args.modules and collections.Counter(discovery["serial"]) != expected:
        _print_set_difference("the serial discover -s tests set", collections.Counter(discovery["serial"]), expected)
        return 1
    units = build_units(discovery, ui_modules(names), load_durations(), load_durations(key="tests"))
    if shard is not None:
        units = shard_plan(units, shard[1])[shard[0] - 1]
    # The union of dispatched ids equals the discovered set exactly (the whole suite, or this shard's plan).
    dispatched = collections.Counter(t for u in units for t in u.ids)
    if shard is None and dispatched != expected:
        _print_set_difference("the dispatched ids", dispatched, expected)
        return 1
    ui_count = sum(1 for u in units if u.ui)
    print(f"{len(names)} modules, {sum(expected.values())} tests discovered (serial set checked: "
          f"{'yes' if not args.modules else 'no, modules given'}) in {time.monotonic() - began:.0f}s"
          + (f"; shard {shard[0]}/{shard[1]}" if shard else ""), flush=True)
    print(f"{len(units)} units: {ui_count} UI over {ui_workers} processes, {len(units) - ui_count} other over {workers} workers;"
          f" {sum(dispatched.values())} tests dispatched", flush=True)

    results, worker_wall, wall, bases = execute(args, units, ui_workers, workers, ui_lock_for(args, units))
    try:
        ran = sum(r.ran for r in results)
        skipped = sum(r.skipped for r in results)
        failures = sum(r.failures for r in results)
        errors = sum(r.errors for r in results)
        broken = [r for r in results if not r.ok]
        print()
        for label, seconds in sorted(worker_wall.items(), key=lambda kv: (kv[0][0] != "u", int(re.sub(r"\D", "", kv[0])))):
            print(f"worker {label}: {seconds:.0f} s")
        print(f"Ran {ran} tests in {wall:.1f}s wall (skipped={skipped}, failures={failures}, errors={errors})")
        accounted = len(results) == len(units) and all(r.accounted for r in results) and ran == sum(dispatched.values())
        if not accounted:
            for r in results:
                if not r.accounted:
                    print(f"NOT ACCOUNTED: {r.label}: ran {r.ran} of {len(r.unit.ids)}"
                          + (f"; missing {', '.join(r.missing[:5])}{' ...' if len(r.missing) > 5 else ''}" if r.missing else ""))
            print(f"dispatched {sum(dispatched.values())} tests in {len(units)} units; {len(results)} units reported, {ran} ran")
        green = accounted and not broken
        rule_d = []
        if broken:
            green = _report_failures(broken, args, bases, rule_d) and accounted
        if args.durations:
            _print_durations(results, args.durations)
        if args.write_durations:
            _write_durations(_durations_of(results), [t for r in results for t in r.unit.ids], Path(args.write_durations),
                             "one local parallel run")
        if args.report:
            _write_report(Path(args.report), shard, units, results, wall, green, rule_d, discovery)
        print("OK" if green else "FAILED", flush=True)
        return 0 if green else 1
    finally:
        for base in bases:
            shutil.rmtree(base, ignore_errors=True)


def _print_set_difference(what: str, got: collections.Counter, expected: collections.Counter) -> None:
    extra, lost = got - expected, expected - got
    print(f"MISMATCH: {what} differ from the discovered set ({sum(got.values())} vs {sum(expected.values())});"
          f" {sum(extra.values())} extra, {sum(lost.values())} missing")
    for test in list(extra)[:10]:
        print(f"  extra:   {test}")
    for test in list(lost)[:10]:
        print(f"  missing: {test}")


def _report_failures(broken: list[Result], args, bases: list[Path], rule_d: list) -> bool:
    """Print every failure; apply rule D. True when the run counts as green."""
    allowed = load_sensitive()
    named = [test for r in broken for test in r.failed]
    only_known = (not args.no_rerun and bool(named) and all(r.parsed and r.accounted for r in broken)
                  and all(f"{module}.{key}" in allowed for key, module in named)
                  and sum(len(r.failed) for r in broken) == sum(r.failures + r.errors for r in broken))
    for result in broken:
        print(f"\n===== FAILED: {result.label} (exit {result.returncode}) {' '.join(result.command[2:])}")
        print(result.output[-6000:] if not result.parsed else _traces(result.output))
    if not only_known:
        return False
    print("\nOnly load-sensitive tests failed; rerunning each alone once (rule D):")
    green = True
    for key, module in dict.fromkeys(named):
        command = [args.python, "-m", "unittest", f"tests.{module}.{key}"]
        done = subprocess.run(command, cwd=ROOT, env=worker_env(99, bases), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        passed = done.returncode == 0
        rule_d.append({"test": f"{module}.{key}", "alone": "PASS" if passed else "FAIL"})
        print(f"  {module}.{key}: {'PASS' if passed else 'FAIL'} alone (first run failed)")
        if not passed:
            print(done.stdout + done.stderr)
        green &= passed
    print("rule D: " + ("every rerun passed - the run counts as green" if green else "a rerun failed - the run is red"))
    return green


def _traces(output: str) -> str:
    start = output.find("======")
    return output[start:] if start >= 0 else output[-4000:]


def _durations_of(results: list[Result]) -> list[tuple[float, str]]:
    return sorted(((float(m.group(1)), m.group(2)) for r in results for m in _DURATION.finditer(r.output)), reverse=True)


def _print_durations(results: list[Result], top: int) -> None:
    _print_rows(_durations_of(results), top)


def _print_rows(rows: list[tuple[float, str]], top: int) -> None:
    print(f"\nSlowest {top} tests (all workers):")
    for seconds, name in rows[:top]:
        print(f"  {seconds:8.3f}s  {name}")


def _write_durations(rows: list[tuple[float, str]], dispatched: list[str], path: Path, source: str) -> None:
    """Seconds per test class (``tests.module.Class``), averaged over the times it ran, and (THIZ)
    per test (``tests.module.Class.test``) for a class long enough to be split by its methods."""
    runs = collections.Counter(dispatched)
    total: dict[str, float] = collections.defaultdict(float)
    tests: dict[str, float] = collections.defaultdict(float)
    for seconds, name in rows:
        match = _SEEN.search(name)
        if match:
            share = seconds / max(1, runs.get(match.group(2), 1))
            total[match.group(2).rsplit(".", 1)[0]] += share
            tests[match.group(2)] += share
    classes = {cls: round(seconds, 3) for cls, seconds in sorted(total.items())}
    for cls in {t.rsplit(".", 1)[0] for t in dispatched}:  # classes too fast to be listed still get a record
        classes.setdefault(cls, 0.0)
    split = {t: round(s, 3) for t, s in sorted(tests.items()) if classes[t.rsplit(".", 1)[0]] > SPLIT_CLASS_SECONDS / 2}
    path.write_text(json.dumps({"version": 1, "about": f"Seconds per test class from {source}, and per test for the long "
                                "classes; scripts/test_parallel.py plans its units and CI shards from it (--write-durations "
                                "regenerates it; CI's aggregate job writes it as ci-durations.json).",
                                "classes": dict(sorted(classes.items())), "tests": split},
                               indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(classes)} class and {len(split)} test durations to {path}")


def _write_report(path: Path, shard, units, results, wall, green, rule_d, discovery) -> None:
    path.write_text(json.dumps({
        "shard": list(shard) if shard else None,
        "green": green,
        "wall": round(wall, 1),
        "units": [u.label for u in units],
        "dispatched": [t for u in units for t in u.ids],
        "ran": sum(r.ran for r in results),
        "skipped": sum(r.skipped for r in results),
        "skips": [skip for r in results for skip in r.skips],  # [id, reason] each; the aggregate gates them
        "failures": sum(r.failures for r in results),
        "errors": sum(r.errors for r in results),
        "accounted": all(r.accounted for r in results) and len(results) == len(units),
        "rule_d": rule_d,
        "durations": _durations_of(results),                 # every test: the aggregate writes CI's durations from them
        "discovered": sum(discovered_ids(discovery).values()),
    }, ensure_ascii=False), encoding="utf-8")


def _gate_skips(shard: str, report: dict) -> list[str]:
    """V2.1 C-5: print every skip with its reason; a problem for each one not on ALLOWED_SKIPS
    (same id and reason), and when the skips cannot all be named."""
    skips = report.get("skips")
    problems = []
    if not isinstance(skips, list) or len(skips) != report["skipped"]:
        problems.append(f"{shard}: {report['skipped']} skipped, but {len(skips) if isinstance(skips, list) else 'none'} named")
    for test, reason in skips if isinstance(skips, list) else []:
        allowed = ALLOWED_SKIPS.get(test) == reason
        print(f"  skipped{'' if allowed else ' (NOT ALLOWED)'}: {test}: {reason}")
        if not allowed:
            problems.append(f"{shard}: {test} skipped ({reason}) and is not on the allowed-skip list")
    return problems


def aggregate(args) -> int:
    """CI's final job: every shard's report against a fresh discovery and the same plan; one result."""
    directory = Path(args.aggregate)
    shards = args.shards
    names = discover([])
    discovery = discover_tests(args.python, names, serial=True)
    expected = discovered_ids(discovery)
    units = build_units(discovery, ui_modules(names), load_durations(), load_durations(key="tests"))
    plan = shard_plan(units, shards) if shards > 0 else []
    reports = {}
    for file in sorted(directory.rglob("*.json")):
        try:
            report = json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(report, dict) and isinstance(report.get("shard"), list):
            reports[tuple(report["shard"])] = report
    problems = []
    if discovery["errors"]:
        problems.append("discovery errors: " + "; ".join(discovery["errors"]))
    if collections.Counter(discovery["serial"]) != expected:
        problems.append("the per-module discovery differs from the serial discover -s tests set")
    if shards <= 0:
        problems.append("--shards N is required")
    dispatched: collections.Counter = collections.Counter()
    print(f"{sum(expected.values())} tests discovered; {len(units)} units over {shards} shards")
    for index in range(1, shards + 1):
        report = reports.get((index, shards))
        if report is None:
            problems.append(f"shard {index}/{shards}: no report")
            print(f"shard {index}/{shards}: NO REPORT")
            continue
        planned = [u.label for u in plan[index - 1]]
        if report["units"] != planned:
            problems.append(f"shard {index}/{shards}: its units differ from the plan")
        dispatched.update(report["dispatched"])
        if not report["accounted"] or report["ran"] != len(report["dispatched"]):
            problems.append(f"shard {index}/{shards}: ran {report['ran']} of {len(report['dispatched'])} dispatched")
        if not report["green"]:
            problems.append(f"shard {index}/{shards}: red")
        planned_ui = sum(u.weight for u in plan[index - 1] if u.ui)
        planned_other = sum(u.weight for u in plan[index - 1] if not u.ui)
        print(f"shard {index}/{shards}: {'green' if report['green'] else 'RED'}, {report['ran']} tests in {report['wall']:.0f}s"
              f" (planned UI {planned_ui:.0f}s / other {planned_other:.0f}s;"
              f" skipped={report['skipped']}, failures={report['failures']}, errors={report['errors']})"
              + (f", rule D: {report['rule_d']}" if report["rule_d"] else ""))
        problems += _gate_skips(f"shard {index}/{shards}", report)
    if dispatched != expected:
        _print_set_difference("the shards' dispatched ids", dispatched, expected)
        problems.append("the union of dispatched ids is not the discovered set")
    ran = sum(r["ran"] for r in reports.values())
    print(f"Ran {ran} tests over {len(reports)} shards (discovered {sum(expected.values())};"
          f" skipped={sum(r['skipped'] for r in reports.values())}, failures={sum(r['failures'] for r in reports.values())},"
          f" errors={sum(r['errors'] for r in reports.values())})")
    rows = sorted((tuple(row) for r in reports.values() for row in r["durations"]), reverse=True)
    if rows:
        _print_rows(rows, args.durations or 30)
    if args.write_durations and rows and not problems:      # never a partial plan from a red or missing shard
        _write_durations(rows, [t for r in reports.values() for t in r["dispatched"]], Path(args.write_durations),
                         f"CI ({shards} shards)")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    green = not problems
    print("OK" if green else "FAILED", flush=True)
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
