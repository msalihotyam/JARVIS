"""Pure PM15 diff scope gate (I3): may a stage's changed paths be applied?

Input is data that PM14/PM25 parse from git; no git, filesystem, environment,
clock or network is consulted. Paths and targets are normalised by the PM10
predicates in jarvis.work_plan; anything unknown or unparseable is refused
(fail closed). A refusal blocks the whole stage: nothing is applied.
"""

import re
import sys

from jarvis.work_plan import AGENT_CONFIG_NAMES, Refusal, _target_refusal

#: Largest new blob accepted, in bytes; callers may pass a smaller cap.
MAX_FILE_BYTES = 1 << 20
#: Git index modes; "000000" is the absent side of an add or a delete.
_MODES = ("000000", "100644", "100755", "120000", "160000")
#: name-status letters; R/C may carry a 0-100 similarity score. U, X, B are
#: refused.
_STATUS = re.compile(r"[AMDT]|[RC](?:100|0?[0-9]{1,2})?")
_FIELDS = {"status", "path", "old_path", "old_mode", "new_mode", "size",
           "binary", "reparse"}
_ABSOLUTE = re.compile(r"/|[A-Za-z]:")
#: Repo, hook, CI, agent and tool config that git, CI, editors or agents obey
#: or execute. Any path component matching refuses the change.
_CONFIG_NAMES = (
    ".gitattributes", ".gitmodules", ".lfsconfig", ".githooks", ".husky",
    ".pre-commit-config.yaml", ".pre-commit-hooks.yaml", "lefthook.yml",
    "lefthook.yaml", ".github", ".gitlab", ".gitlab-ci.yml", ".circleci",
    ".travis.yml", "azure-pipelines.yml", ".azure-pipelines", "jenkinsfile",
    ".buildkite", "bitbucket-pipelines.yml", ".woodpecker", ".woodpecker.yml",
    ".drone.yml", ".gitea", ".forgejo", ".npmrc", ".yarnrc", ".yarnrc.yml",
    ".pnpmfile.cjs", "sitecustomize.py", "usercustomize.py") + AGENT_CONFIG_NAMES
#: Python runs import lines in .pth files at interpreter startup.
_CONFIG_SUFFIXES = (".pth",)
#: Build and test-runner config, lock files and compiled modules: anything
#: that changes how checks are collected or run, so a slice could fake a PASS.
#: Allowed only when the path equals a granted literal file target.
_BUILD_NAMES = (
    "conftest.py", "setup.py", "noxfile.py", "pytest.ini", "tox.ini",
    "pyproject.toml", "setup.cfg", "makefile", "gnumakefile", "package.json",
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "pnpm-workspace.yaml", ".coveragerc", ".flake8", ".pylintrc", "pylintrc",
    "pyrightconfig.json", "pipfile", "pipfile.lock", "poetry.lock", "pdm.lock",
    "uv.lock", ".python-version", "__pycache__", ".babelrc", ".nycrc",
    "tsconfig.json", "justfile", "cmakelists.txt", "manifest.in")
_BUILD_PREFIXES = (
    "requirements", "constraints", "makefile.", "gnumakefile.", "jest.config.",
    "vitest.config.", "vite.config.", "karma.conf.", ".mocharc.", "playwright.config.",
    "cypress.config.", "babel.config.", ".nycrc.")
_BUILD_SUFFIXES = (".toml", ".ini", ".cfg", ".pyc", ".pyo", ".pyd", ".so", ".mk")
#: python -m unittest puts the cwd first on sys.path, so a top-level
#: unittest/ or json.py shadows the stdlib the runner imports.
_STDLIB = frozenset(n.lower() for n in sys.stdlib_module_names) - {"test", "tests"}


def _fold(c):
    # A fold that changes length (ß -> ss, İ -> i̇) would merge distinct names.
    f = c.upper().lower()
    return f if len(f) == 1 else c


def _key(path):
    return "".join(map(_fold, path.replace("\\", "/").rstrip("/")))


def _show(value):
    # Never repr untrusted values: deep nesting would raise RecursionError.
    return value[:200] if type(value) is str else type(value).__name__


def _covering(key, granted):
    return [target for k, (kind, target) in granted.items()
            if (kind == "file" and key == k)
            or (kind == "subtree" and key.startswith(k + "/"))]


def _path_refusal(path, granted, jarvis_repo):
    if type(path) is not str or not path:
        return Refusal("unparseable", _show(path))
    slashed = path.replace("\\", "/")
    if _ABSOLUTE.match(slashed):
        return Refusal("absolute_path", path)
    if ".." in slashed.split("/"):
        return Refusal("traversal", path)
    if slashed.endswith("/"):
        return Refusal("unparseable", path)
    parts = _key(path).split("/")
    if ".git" in parts:
        return Refusal("git_dir", path)
    if any(p in _CONFIG_NAMES or p.endswith(_CONFIG_SUFFIXES) for p in parts):
        return Refusal("config_file", path)
    # PM10: control/format chars, globs, aliases, devices, protected, I12.
    refusal = _target_refusal(path, jarvis_repo)
    if refusal:
        return refusal
    if ((any(p in _BUILD_NAMES or p.startswith(_BUILD_PREFIXES)
             or p.endswith(_BUILD_SUFFIXES) for p in parts)
            or parts[0].split(".")[0] in _STDLIB)
            and granted.get(_key(path), ("",))[0] != "file"):
        # A subtree target never grants build/test config.
        return Refusal("build_config", path)
    if not _covering(_key(path), granted):
        return Refusal("out_of_scope", path)
    return None


def _change_refusal(change, granted, binary_ok, jarvis_repo, max_bytes):
    if type(change) is not dict or set(change) != _FIELDS:
        return Refusal("unparseable", _show(
            change.get("path") if type(change) is dict else change))
    status, old, new = change["status"], change["old_mode"], change["new_mode"]
    if type(status) is not str or not _STATUS.fullmatch(status):
        return Refusal("unknown_status", _show(status))
    paired = status[0] in "RC"
    if (type(old) is not str or old not in _MODES
            or type(new) is not str or new not in _MODES
            or type(change["size"]) is not int or change["size"] < 0
            or type(change["binary"]) is not bool
            or type(change["reparse"]) is not bool
            or (change["old_path"] is not None) != paired
            or (old == "000000") != (status == "A")
            or (new == "000000") != (status == "D")):
        return Refusal("unparseable", _show(change["path"]))
    path = change["path"]
    # Both sides of a rename or copy must be in scope.
    for item in (path, change["old_path"]) if paired else (path,):
        refusal = _path_refusal(item, granted, jarvis_repo)
        if refusal:
            return refusal
    if "120000" in (old, new):
        return Refusal("symlink", path)
    if "160000" in (old, new):
        return Refusal("submodule", path)
    if change["reparse"]:
        return Refusal("reparse_point", path)
    # Only an in-place edit may keep an existing executable bit.
    if new == "100755" and not (status == "M" and old == "100755"):
        return Refusal("executable_mode", path)
    if new != "000000":
        if change["size"] > max_bytes:
            return Refusal("size_cap", path)
        if change["binary"] and not any(
                t in binary_ok for t in _covering(_key(path), granted)):
            return Refusal("binary", path)
    return None


def check(changes, targets, *, jarvis_repo, binary_targets=(),
          max_bytes=MAX_FILE_BYTES):
    """Return [] when every change may be applied, else typed Refusals.

    changes is a list of dicts with exactly the keys status, path, old_path,
    old_mode, new_mode (six-digit git modes), size (new blob bytes), binary
    (git's binary flag) and reparse (True when the worktree entry or a parent
    is a junction or other reparse point). old_path is the source of an R/C
    entry, else None. targets is {target: "file" | "subtree"} as
    jarvis.work_plan.scope() returns; binary_targets are granted targets
    under which binary content is allowed. jarvis_repo must be a bool.
    """
    if type(jarvis_repo) is not bool:
        return [Refusal("unknown_fact", "jarvis_repo")]
    if (type(changes) is not list or type(targets) is not dict
            or type(max_bytes) is not int
            or not 0 <= max_bytes <= MAX_FILE_BYTES
            or type(binary_targets) not in (tuple, list, set, frozenset)):
        return [Refusal("unparseable", "arguments")]
    granted = {}
    for target, kind in targets.items():
        if (type(target) is not str or kind not in ("file", "subtree")
                or (kind == "subtree") != target.endswith(("/", "\\"))
                or _target_refusal(target, jarvis_repo)):
            return [Refusal("invalid_target", _show(target))]
        granted[_key(target)] = (kind, target)
    binary_ok = set()
    for target in binary_targets:
        if type(target) is not str or target not in targets:
            return [Refusal("invalid_target", _show(target))]
        binary_ok.add(target)
    refusals = []
    for change in changes:
        refusal = _change_refusal(change, granted, binary_ok, jarvis_repo, max_bytes)
        if refusal:
            refusals.append(refusal)
    return refusals
