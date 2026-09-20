"""Provenance for the native tools JARVIS starts itself (DB-001).

A trusted tool is one JARVIS spawns on its own authority - the Claude CLI, the
Codex CLI, the browser, ``gh``, ``gws`` - as opposed to a command the operator
approved, which has its own authority model and is none of this module's
business.

THE FAILURE THIS EXISTS TO PREVENT. On Windows, handing ``CreateProcess`` a
bare program name makes the CURRENT DIRECTORY one of the places it looks, and
``shutil.which`` prepends the current directory too. A task working directory
is attacker-shaped input: a repository JARVIS was pointed at can carry a file
called ``claude.exe``. Under the old resolution that file could become the
"Claude CLI", before Core ever approved anything, with whatever trust the real
one would have had. The same holds for an empty or relative ``PATH`` entry,
which Windows reads as "here".

So: every trusted spawn's ``argv[0]`` is an absolute, canonical path to a real
file OUTSIDE every directory the task can write, discovery never asks the
operating system to search, and the file's identity is re-checked immediately
before the process is created, so a swap between resolution and spawn is
caught rather than executed. There is no fallback to a bare name: a tool that
cannot be located this way is unavailable, which is a refusal the caller
already knows how to report.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "TrustedExecutable",
    "TrustedExecutableError",
    "forbidden_roots",
    "path_directories",
    "resolve_trusted_executable",
    "sanitized_path",
    "trusted_environment",
]


class TrustedExecutableError(RuntimeError):
    """The tool could not be located, or no longer is what it was.

    Always a refusal to start anything. Never carries a path in its message:
    the caller states the shape of the failure, not the layout of the disk.
    """


#: The OS variables a child genuinely needs to run on Windows at all - the
#: system root, the loader's search behaviour, the temp directory, and the
#: per-user roots the credential stores and CLI config files live under. This
#: is an ALLOWLIST: a name that is not here does not reach the child, so a
#: credential belonging to some other part of JARVIS cannot ride along.
_WINDOWS_BASE: tuple[str, ...] = (
    "ALLUSERSPROFILE", "APPDATA", "COMPUTERNAME", "COMSPEC", "HOMEDRIVE",
    "HOMEPATH", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "OS", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "PUBLIC", "SESSIONNAME", "SYSTEMDRIVE", "SYSTEMROOT",
    "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
)

#: The same on a POSIX host, where far less is load-bearing.
_POSIX_BASE: tuple[str, ...] = (
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "SHELL", "TMPDIR", "USER",
)


def _base_names() -> tuple[str, ...]:
    return _WINDOWS_BASE if os.name == "nt" else _POSIX_BASE


def _canonical(value: Any) -> Path | None:
    """``value`` as an absolute real path, or ``None`` if it is not one.

    ``strict=True`` because a path that does not exist cannot be vouched for,
    and because resolving strictly is what follows a junction, a symlink or
    any other reparse point through to what it ACTUALLY names - a workspace
    link pointing at a system directory, or the reverse, is decided here.
    """
    try:
        resolved = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return resolved if resolved.is_absolute() else None


def _identity(path: Path) -> tuple[int, int, int, int] | None:
    """Enough of the file's stat to notice it was replaced.

    Volume and file index identify the file itself (on Windows ``st_dev`` is
    the volume serial and ``st_ino`` the file index, which a replacement
    changes); size and modification time catch an in-place rewrite that
    somehow kept the index. No hashing: this runs immediately before every
    spawn, and a megabyte-scale digest on that path would be a cost paid
    forever to narrow a window that stat already closes for ordinary
    replacement.
    """
    try:
        info = path.stat()
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _inside(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def forbidden_roots(extra: Iterable[Any] = ()) -> tuple[Path, ...]:
    """Directories a trusted executable may never be found in.

    The current directory always, because that is what the OS would search on
    its own; plus whatever the caller names - a task working directory, a
    configured workspace - each canonicalised, so a link into one of them is
    the same answer as the directory itself.
    """
    roots: list[Path] = []
    for value in (Path.cwd(), *extra):
        root = _canonical(value)
        if root is not None and root.is_dir() and root not in roots:
            roots.append(root)
    return tuple(roots)


def path_directories(
    environ: Mapping[str, str] | None = None,
    *,
    forbidden: Sequence[Path] = (),
) -> tuple[Path, ...]:
    """The usable ``PATH`` directories, enumerated here rather than searched.

    Dropped: empty entries (Windows reads those as the current directory),
    relative entries, anything that is not a real directory, and anything
    inside a forbidden root. What survives is absolute and canonical, so the
    candidate paths built from it are too.
    """
    raw = (environ if environ is not None else os.environ).get("PATH") or ""
    directories: list[Path] = []
    for entry in raw.split(os.pathsep):
        if not entry.strip():
            continue
        if not Path(entry).is_absolute():
            continue
        directory = _canonical(entry)
        if directory is None or not directory.is_dir():
            continue
        if any(_inside(directory, root) for root in forbidden):
            continue
        if directory not in directories:
            directories.append(directory)
    return tuple(directories)


def sanitized_path(environ: Mapping[str, str] | None = None) -> str:
    """A ``PATH`` for a child with the "search here" entries already gone.

    The child's own subprocesses (``gh`` calling ``git``, a CLI calling
    ``node``) still need a PATH; they do not need the empty and relative
    entries that make the current directory part of it.
    """
    return os.pathsep.join(str(d) for d in path_directories(environ))


def _candidate_names(name: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    """``name`` with the extensions Windows would have tried, in that order."""
    if os.name != "nt" or Path(name).suffix:
        return (name,)
    raw = environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    extensions = [part for part in raw.split(os.pathsep) if part.strip().startswith(".")]
    return (*[f"{name}{extension.lower()}" for extension in extensions], name)


@dataclass(frozen=True, slots=True)
class TrustedExecutable:
    """One located tool: an absolute canonical path, and who it was.

    ``path`` is what goes in ``argv[0]`` - never a bare name. ``identity`` is
    the stat taken when it was accepted, and ``argv0`` re-checks it, so the
    value only stays usable while it still names the same file.
    """

    path: str
    identity: tuple[int, int, int, int]

    def argv0(self, *, workspace: Iterable[Any] = ()) -> str:
        """The path to spawn, re-validated NOW. Raises rather than degrade.

        Called immediately before process creation, so the gap between
        deciding a tool is trustworthy and starting it is as short as the
        code can make it. ``workspace`` re-states the directories that are
        untrusted at THIS call - the task's working directory is only known
        then - because a tool that was outside every workspace when it was
        found is not automatically outside the one it is about to run in.
        """
        current = _canonical(self.path)
        if current is None or not current.is_file():
            raise TrustedExecutableError("the tool is no longer present where it was found")
        if str(current) != self.path:
            raise TrustedExecutableError("the tool now resolves somewhere else")
        if _identity(current) != self.identity:
            raise TrustedExecutableError("the tool changed identity after it was accepted")
        for root in forbidden_roots(workspace):
            if _inside(current, root):
                raise TrustedExecutableError("the tool is inside a workspace directory")
        return self.path


def _accept(candidate: Any, forbidden: Sequence[Path]) -> TrustedExecutable | None:
    """``candidate`` as a trusted executable, or ``None`` if it fails a rule."""
    path = _canonical(candidate)
    if path is None or not path.is_file():
        return None
    if any(_inside(path, root) for root in forbidden):
        return None
    identity = _identity(path)
    return None if identity is None else TrustedExecutable(str(path), identity)


def resolve_trusted_executable(
    name: str,
    *,
    configured: Any = None,
    install_paths: Iterable[Any] = (),
    environ: Mapping[str, str] | None = None,
    workspace: Iterable[Any] = (),
) -> TrustedExecutable | None:
    """Locate one trusted tool, or ``None``. Never searches from here.

    In order: an explicitly configured absolute path, then the installation
    roots this product already knows about, then ``PATH`` - enumerated
    directory by directory and joined with the candidate names ourselves, so
    the operating system is asked to open a specific file and never to go
    looking for one.
    """
    values = environ if environ is not None else os.environ
    forbidden = forbidden_roots(workspace)
    if configured:
        # A configured bare name is a name, not a location: it would have to
        # be searched for, which is the thing this module does not do.
        return _accept(configured, forbidden) if Path(configured).is_absolute() else None
    for candidate in install_paths:
        found = _accept(candidate, forbidden)
        if found is not None:
            return found
    for directory in path_directories(values, forbidden=forbidden):
        for candidate_name in _candidate_names(name, values):
            found = _accept(directory / candidate_name, forbidden)
            if found is not None:
                return found
    return None


def trusted_environment(
    *,
    allow: Iterable[str] = (),
    prefixes: Iterable[str] = (),
    forced: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment ONE trusted tool gets: an allowlist, not a subtraction.

    A removal policy has to predict every secret that might be present; an
    allowlist only has to know what this tool legitimately needs. Anything
    else JARVIS happens to be holding - another provider's token, an
    unrelated key - simply is not there to leak.

    ``allow``/``prefixes`` are the tool's own configuration and
    authentication surface (its ``*_CONFIG_*``, its OAuth cache location);
    ``forced`` is what the caller pins regardless of the parent. ``PATH`` is
    passed through sanitised, never verbatim.
    """
    values = environ if environ is not None else os.environ
    permitted = {name.upper() for name in (*_base_names(), *allow)}
    allowed_prefixes = tuple(prefix.upper() for prefix in prefixes)
    child = {
        str(key): str(value)
        for key, value in values.items()
        if str(key).upper() in permitted
        or (allowed_prefixes and str(key).upper().startswith(allowed_prefixes))
    }
    child.pop("PATH", None)
    path = sanitized_path(values)
    if path:
        child["PATH"] = path
    child.update({str(k): str(v) for k, v in (forced or {}).items()})
    return child
