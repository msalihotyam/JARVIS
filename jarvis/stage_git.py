"""The one hardened Git helper for stage and target repositories (PM26A).

A worker can write the repository it worked in, ``.git/config``, hooks and
``info/attributes`` included, and Git reads those on every call: a filter, hook,
fsmonitor, textconv, signature or transport command planted there would run
inside JARVIS's own Git calls, outside the killed worker job (CDXINT124). So
only plumbing with no such surface is spawned, the configuration that has one
is pinned on the command line, and ``archive`` (the one command that converts
content on the way out) reads the repository's objects through a bare view of
JARVIS's own, so the worker's configuration is not read at all. Git is pointed
at ``repo/.git`` explicitly and only after a check that nothing in it can
redirect Git elsewhere (gitfile, commondir, alternates, a multi-pack-index,
config includes, links or junctions). The worker is assumed dead by then: the
check is not race-free against a live writer. Anything else is refused before a
process exists.

``import_commit`` is how a worker's commit enters JARVIS's own repository once
the worker has started: no Git has the stage as repository, cwd or GIT_DIR.
``pack-objects`` runs in a view that reaches the stage's objects through one
alternates line and is an untrusted byte source; ``index-pack --strict`` in
JARVIS's repository computes every id itself. The object format comes from
JARVIS's repository, never the stage, and the tip is only a claim until the
checks after the pipe pass. Only Git subprocesses are bounded by a timeout: the
``_git_dir`` walk and the Python archive extraction are not.
"""

from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
from pathlib import Path

from jarvis.trusted_exec import (
    NO_CONSOLE_WINDOW, TrustedExecutableError, resolve_trusted_executable, trusted_environment,
)

ALLOWED = frozenset({
    "rev-parse", "cat-file", "ls-tree", "diff-tree", "update-ref", "for-each-ref", "archive",
    "pack-objects", "index-pack", "rev-list", "merge-base", "worktree",
})
# Long options that run a command, read or write a file, switch repositories or convert
# content. Git accepts any unique prefix (--ext means --ext-diff), so a prefix of one of
# these is refused too. Short options are refused in _check.
_REFUSED = ("exec", "remote", "output", "textconv", "filters", "ext-diff", "show-signature",
            "submodule", "worktree-attributes", "orderfile", "resolve-git-dir", "git-dir",
            "work-tree", "git-path")
# diff short options whose value is attached (-S<string>): the rest of the bundle is that value.
_VALUED = frozenset("SGIlUMCBX")
_PINS = (
    # --help is refused in _check; these keep a help run off the browser and off autocorrect.
    "-c", "help.format=man", "-c", "help.autocorrect=0",
    # A multi-pack-index names its packs by path, "../" included: never read one (CDXINT128).
    "-c", "core.multiPackIndex=false",
    # Both are stage-writable answers to "what is reachable", read through alternates: walk the objects.
    "-c", "core.commitGraph=false", "-c", "pack.useBitmaps=false",
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "diff.external=",
    "-c", "diff.submodule=short", "-c", "core.useReplaceRefs=false", "-c", "core.autocrlf=false",
    "-c", "protocol.allow=never", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
    "-c", "core.logAllRefUpdates=false", "-c", "mailmap.blob=",
    # Event level: also covers config-defined hooks (hook.<name>.command), which ignore core.hooksPath.
    "-c", "hook.reference-transaction.enabled=false",
    # protocol.<name>.allow beats protocol.allow.
    *(item for name in ("file", "ext", "ssh", "git", "http", "https")
      for item in ("-c", f"protocol.{name}.allow=never")),
)
# Files whose mere presence makes Git read another repository or object store, or rewrite history.
_REDIRECTS = ("commondir", "objects/info/alternates", "objects/info/http-alternates", "info/grafts")
_INCLUDE = re.compile(rb"\[\s*include", re.IGNORECASE)
# extensions.refStorage may name a ref store by path (files://, reftable://) and -c cannot override it.
_REFSTORAGE = re.compile(rb"refstorage", re.IGNORECASE)
_GPG = ("gpg.program", "gpg.openpgp.program", "gpg.x509.program", "gpg.ssh.program")
# info/attributes beats the archived tree's .gitattributes: bytes leave exactly as committed.
_VIEW_ATTRIBUTES = b"* -filter -text -eol -ident -export-subst -export-ignore -working-tree-encoding\n"
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_OID_LENGTH = {"sha1": 40, "sha256": 64}
_PACK_OBJECTS = ("pack-objects", "--revs", "--stdout", "-q")
# ponytail: one cap on the stored pack, not on what it expands to: a 16 KiB delta bomb under a
# 1 MiB cap measured ~2 GiB of index-pack memory, bounded only by the timeout. Job Object memory
# limit if that ever bites; per-task budgets if a real worker commit ever nears the cap.
_MAX_PACK = 256 * 1024 * 1024
_MAX_CONFIG = 1024 * 1024
_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{port}{n}" for port in ("COM", "LPT") for n in (*"123456789", "¹", "²", "³")),
})


class StageGitError(RuntimeError):
    """A Git failure or a refused repository or export; the repository itself is never changed by it."""


def _check(args: tuple) -> None:
    if not args or not all(isinstance(arg, str) for arg in args) or args[0] not in ALLOWED:
        raise ValueError("git subcommand is not allowed")
    # `git <sub> --help` runs `git help`, which starts the repository's help.format viewer
    # (web.browser, browser.<tool>.cmd, man.viewer); -h and "help" are refused with it.
    if args[1:2] == ("help",):
        raise ValueError("git help is not allowed")
    # Long options are refused anywhere, operands included (fail closed). Git itself takes
    # nothing after --end-of-options as an option, which is where worker-derived operands belong.
    # After an option ("-I --end-of-options") it may be that option's value, so it counts only
    # after the subcommand or an operand (fail closed).
    options = True
    for prev, arg in zip(args, args[1:]):
        name = arg[2:].split("=", 1)[0]
        if arg.startswith("--") and name and any(refused.startswith(name) for refused in _REFUSED):
            raise ValueError(f"git option is not allowed: {arg.split('=', 1)[0]}")
        if arg == "-h" or (arg.startswith("--") and name and (
                "help".startswith(name.casefold()) or name.casefold().startswith("help"))):
            raise ValueError(f"git option is not allowed: {arg.split('=', 1)[0]}")
        options = options and not (arg == "--end-of-options" and prev[:1] != "-")
        # diff-tree -O<file> reads a file, also inside a bundle (-pO<file> is -p -O<file>); a
        # value-taking letter (-SFOO, -GFOO) ends the bundle.
        if options and args[0] == "diff-tree" and arg[:1] == "-" and arg[1:2] != "-":
            for letter in arg[1:]:
                if letter == "O":
                    raise ValueError(f"git option is not allowed: {arg}")
                if letter in _VALUED:
                    break
    if args[0] == "archive" and (len(args) != 3 or args[1] != "--format=tar" or not _OID.fullmatch(args[2])):
        raise ValueError("archive takes exactly --format=tar and one object id")
    if args[0] == "worktree" and args != ("worktree", "list", "--porcelain", "-z"):
        raise ValueError("worktree takes exactly the read-only list form")
    if args[0] == "pack-objects" and args != _PACK_OBJECTS:
        raise ValueError("pack-objects takes exactly the import form")
    # --max-input-size=0 means no limit.
    if args[0] == "index-pack" and (
            len(args) != 4 or args[1] != "--strict" or args[3] != "--stdin"
            or not re.fullmatch(r"--max-input-size=[1-9][0-9]*", args[2])):
        raise ValueError("index-pack takes exactly the import form")


def _linked(info: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (junctions included)."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _git_dir(repo: Path) -> Path:
    """``repo/.git`` once nothing in it can send Git (reads or writes) outside it."""
    git_dir = repo / ".git"
    try:
        info = os.lstat(git_dir)
        if _linked(os.lstat(repo)) or _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise StageGitError("repository has no plain .git directory")
        # ponytail: walks every file under .git on each call; fine for stage repos, cache per run if slow.
        pending = [git_dir]
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if _linked(info):
                        raise StageGitError(f"repository contains a link: {entry.path}")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
        if any(os.path.lexists(git_dir / name) for name in _REDIRECTS):
            raise StageGitError("repository redirects to another object store or history")
        # Also pinned off; any spelling or variant (.rev, .bitmap, incremental .d) is refused.
        pack = git_dir / "objects" / "pack"
        if pack.is_dir() and any(name.casefold().startswith("multi-pack-index") for name in os.listdir(pack)):
            raise StageGitError("repository has a multi-pack-index")
        for name in ("config", "config.worktree"):
            data = b""
            if (git_dir / name).is_file():
                with (git_dir / name).open("rb") as handle:
                    data = handle.read(_MAX_CONFIG + 1)
            if len(data) > _MAX_CONFIG:
                raise StageGitError("repository configuration is too large")
            if _INCLUDE.search(data):
                raise StageGitError("repository configuration includes another file")
            if _REFSTORAGE.search(data):
                raise StageGitError("repository configuration sets a ref storage")
    except OSError as exc:
        raise StageGitError(f"repository check failed: {type(exc).__name__}") from exc
    return git_dir


def _launch(scratch: Path) -> tuple[dict, tuple]:
    """The environment and command-line pins every Git process starts with."""
    empty, absent = scratch / "empty", scratch / "absent"
    env = trusted_environment(forced={
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty), "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_PROTOCOL_FROM_USER": "0",
    })
    # Path-valued settings point at JARVIS's own files: a planted UNC path would reach the network.
    pins = ("-c", f"core.hooksPath={scratch / 'hooks'}", "-c", f"core.attributesFile={empty}",
            "-c", f"core.excludesFile={empty}", "-c", f"diff.orderFile={empty}",
            "-c", f"mailmap.file={absent}",
            *(item for key in _GPG for item in ("-c", f"{key}={absent}")), *_PINS)
    return env, pins


def _spawn(git, repo: Path, scratch: Path, location: tuple, args: tuple,
           input: bytes | None, timeout: float) -> bytes:
    env, pins = _launch(scratch)
    forced = ("--no-ext-diff", "--no-textconv") if args[0] == "diff-tree" else ()
    try:
        result = subprocess.run(
            [git.argv0(workspace=(repo,)), "--no-pager", *pins, *location, args[0], *forced, *args[1:]],
            input=input, capture_output=True, timeout=timeout, env=env, shell=False,
            cwd=scratch, creationflags=NO_CONSOLE_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired, TrustedExecutableError) as exc:
        raise StageGitError(f"git {args[0]} failed: {type(exc).__name__}") from exc
    if result.returncode:
        tail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise StageGitError(f"git {args[0]} exited {result.returncode}: {tail}")
    return result.stdout


def _view(git_dir: Path, form: str, scratch: Path) -> Path:
    """A bare ``form`` repository of JARVIS's own that borrows ``git_dir``'s objects via alternates."""
    if form not in _OID_LENGTH or not (git_dir / "objects").is_dir():
        raise StageGitError("repository object store not found")
    view = scratch / "view"
    for directory in ("objects/info", "refs", "info"):
        (view / directory).mkdir(parents=True)
    (view / "HEAD").write_bytes(b"ref: refs/heads/view\n")
    (view / "config").write_bytes(
        f"[core]\n\trepositoryformatversion = 1\n\tbare = true\n[extensions]\n\tobjectformat = {form}\n"
        .encode())
    (view / "objects/info/alternates").write_bytes(f"{git_dir / 'objects'}\n".encode())
    (view / "info/attributes").write_bytes(_VIEW_ATTRIBUTES)
    return view


def run(repo: str | os.PathLike, *args: str, input: bytes | None = None, timeout: float = 30) -> bytes:
    """Run one allowlisted plumbing command in ``repo`` and return its stdout.

    ``archive`` takes exactly ``--format=tar <object id>`` and runs in the
    object-only view. Raises ValueError before spawning for anything else, and
    StageGitError (also before spawning) for a repository that redirects Git.
    Operands derived from worker data must follow ``--end-of-options``.
    """
    _check(args)
    repo = Path(repo).absolute()
    git_dir = _git_dir(repo)
    git = resolve_trusted_executable("git", workspace=(repo,))
    if git is None:
        raise StageGitError("trusted git is unavailable")
    with tempfile.TemporaryDirectory(prefix="jarvis-git-") as name:
        scratch = Path(name)
        (scratch / "hooks").mkdir()
        (scratch / "empty").write_bytes(b"")
        location = (f"--git-dir={git_dir}", f"--work-tree={repo}")
        if args[0] == "archive":
            form = _spawn(git, repo, scratch, location, ("rev-parse", "--show-object-format"), None,
                          timeout).decode("ascii", "replace").strip()
            location = (f"--git-dir={_view(git_dir, form, scratch)}",)
        return _spawn(git, repo, scratch, location, args, input, timeout)


def _pipe(producer: list, consumer: list, revs: bytes, env: dict, scratch: Path, timeout: float) -> None:
    """``producer < revs | consumer`` without a shell; the timeout kills both, any failure raises."""
    (scratch / "revs").write_bytes(revs)
    errors = scratch / "producer.err", scratch / "consumer.err"
    common = {"env": env, "shell": False, "cwd": scratch, "creationflags": NO_CONSOLE_WINDOW}
    started = []
    try:
        with open(scratch / "revs", "rb") as stdin, open(errors[0], "wb") as out, open(errors[1], "wb") as err:
            started.append(subprocess.Popen(producer, stdin=stdin, stdout=subprocess.PIPE, stderr=out, **common))
            started.append(subprocess.Popen(consumer, stdin=started[0].stdout, stdout=subprocess.DEVNULL,
                                            stderr=err, **common))
            # Only the consumer holds the read end now: if it dies the producer gets a broken pipe.
            started[0].stdout.close()
            deadline = time.monotonic() + timeout
            for process in started:
                process.wait(max(0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageGitError(f"git import failed: {type(exc).__name__}") from exc
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
                process.wait()
        if started:
            started[0].stdout.close()
    failures = [
        f"git {name} exited {process.returncode}: "
        + error.read_bytes().decode("utf-8", "replace").strip()[-500:]
        for name, process, error in zip(("pack-objects", "index-pack"), started, errors)
        if process.returncode
    ]
    if failures:
        raise StageGitError("; ".join(failures))


def import_commit(stage: str | os.PathLike, jarvis: str | os.PathLike, commit: str, base: str, *,
                  max_bytes: int = _MAX_PACK, timeout: float = 120) -> None:
    """Copy ``commit`` and what ``base`` lacks from a dead worker's ``stage`` into ``jarvis``.

    ``base`` must already be in ``jarvis``. Raises ValueError before spawning for anything
    but full object ids and a positive int cap, StageGitError for every refused import. A
    refused import can leave an unreferenced pack behind; an accepted one sets no ref.
    """
    if not all(isinstance(oid, str) and _OID.fullmatch(oid) for oid in (commit, base)):
        raise ValueError("commit and base must be full object ids")
    if type(max_bytes) is not int:
        raise ValueError("max_bytes must be an int")
    consumer = ("index-pack", "--strict", f"--max-input-size={max_bytes}", "--stdin")
    _check(_PACK_OBJECTS)
    _check(consumer)
    stage, jarvis = Path(stage).absolute(), Path(jarvis).absolute()
    stage_dir, jarvis_dir = _git_dir(stage), _git_dir(jarvis)
    form = run(jarvis, "rev-parse", "--show-object-format", timeout=timeout).decode("ascii", "replace").strip()
    if {len(commit), len(base)} != {_OID_LENGTH.get(form)}:
        raise StageGitError(f"object format mismatch: JARVIS's repository is {form}")
    workspace = (stage, jarvis)
    git = resolve_trusted_executable("git", workspace=workspace)
    if git is None:
        raise StageGitError("trusted git is unavailable")
    # A killed producer can outlive its handle on the temp files for a moment.
    with tempfile.TemporaryDirectory(prefix="jarvis-git-", ignore_cleanup_errors=True) as name:
        scratch = Path(name)
        (scratch / "hooks").mkdir()
        (scratch / "empty").write_bytes(b"")
        view = _view(stage_dir, form, scratch)
        env, pins = _launch(scratch)
        try:
            argv0 = git.argv0(workspace=workspace)
        except TrustedExecutableError as exc:
            raise StageGitError(f"git import failed: {type(exc).__name__}") from exc
        _pipe([argv0, "--no-pager", *pins, f"--git-dir={view}", *_PACK_OBJECTS],
              [argv0, "--no-pager", *pins, f"--git-dir={jarvis_dir}", *consumer],
              f"{commit}\n^{base}\n".encode(), env, scratch, timeout)
    # index-pack accepts a pack without the claimed tip, and a forged object can stop it from
    # checking the rest: only these three say the claimed commit arrived whole on top of base.
    # -t, not -e with ^{commit}: that peels, so it would pass a claimed tag.
    if run(jarvis, "cat-file", "-t", commit, timeout=timeout).strip() != b"commit":
        raise StageGitError("claimed object is not a commit")
    run(jarvis, "rev-list", "--objects", "--quiet", commit, f"^{base}", timeout=timeout)
    run(jarvis, "merge-base", "--is-ancestor", base, commit, timeout=timeout)


def tree_of(repo: str | os.PathLike, commit: str, *, timeout: float = 30) -> str:
    """The tree id of ``commit`` (replace refs ignored)."""
    if not isinstance(commit, str) or not commit or commit.startswith("-"):
        raise ValueError("invalid commit")
    tree = run(repo, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}^{{tree}}",
               timeout=timeout).decode("ascii", "replace").strip()
    if not _OID.fullmatch(tree):
        raise StageGitError("rev-parse returned no tree id")
    return tree


def export_commit(repo: str | os.PathLike, commit: str, dest: str | os.PathLike, *,
                  timeout: float = 120) -> Path:
    """Write ``commit``'s tree byte for byte into an empty ``dest`` (no ``.git``); return dest.

    ``dest`` is created if missing. After a refusal it may be partly written.
    """
    dest = Path(dest).absolute()
    try:
        info = os.lstat(dest)
    except FileNotFoundError:
        dest.mkdir(parents=True)
    else:
        if _linked(info) or not stat.S_ISDIR(info.st_mode) or any(dest.iterdir()):
            raise StageGitError("export destination is not an empty directory")
    # A tree id, not the commit: no commit id or export-subst source rides into the archive.
    _extract_tar(run(repo, "archive", "--format=tar", tree_of(repo, commit, timeout=timeout),
                     timeout=timeout), dest)
    return dest


def _member_parts(name: str) -> list[str]:
    parts = name.split("/")
    for part in parts:
        if (part in ("", ".", "..") or part != part.rstrip(". ")
                or any(char in '\\:<>"|?*' or ord(char) < 32 for char in part)
                or part.casefold() in (".git", "git~1")
                or part.split(".")[0].rstrip(" ").upper() in _RESERVED):
            raise StageGitError(f"unsafe archive member name: {name!r}")
    return parts


def _extract_tar(data: bytes, dest: Path) -> None:
    """Regular files and directories only, each strictly inside ``dest``; never overwrite.

    Two paths that are equal after NFC and casefold are refused even where NTFS would keep
    them apart (cafe + U+0301 vs NFC, STRASSE vs straße): intentionally fail closed, since
    other file systems and tools do merge them.
    """
    root = dest.resolve()
    # Every path made so far, case-folded -> its spelling: A/one then a/two would merge on
    # Windows (WindowsPath equality ignores case, so the resolve check below cannot see it).
    spelled: dict[str, str] = {}
    made: set[tuple] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                parts = _member_parts(member.name.rstrip("/") if member.isdir() else member.name)
                if not (member.isdir() or member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)):
                    raise StageGitError(f"archive member is not a file or directory: {member.name!r}")
                for end in range(1, len(parts) + 1):
                    path = "/".join(parts[:end])
                    if spelled.setdefault(unicodedata.normalize("NFC", path).casefold(), path) != path:
                        raise StageGitError(f"archive member collides with another by case: {member.name!r}")
                dirs = parts if member.isdir() else parts[:-1]
                for end in range(1, len(dirs) + 1):
                    if tuple(dirs[:end]) not in made:
                        # No exist_ok: whatever the file system aliases (other case rules, short names) fails.
                        root.joinpath(*dirs[:end]).mkdir()
                        made.add(tuple(dirs[:end]))
                folder = root.joinpath(*dirs)
                # Backstop: a directory that resolves elsewhere (a link or short-name alias).
                if folder.resolve() != folder:
                    raise StageGitError(f"archive member escapes the destination: {member.name!r}")
                if not member.isdir():
                    with open(root.joinpath(*parts), "xb") as handle:
                        handle.write(archive.extractfile(member).read())
    except (tarfile.TarError, OSError) as exc:
        raise StageGitError(f"archive extraction failed: {type(exc).__name__}") from exc
