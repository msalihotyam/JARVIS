"""N6PUSHPATHS1: pure push path decision from git plumbing output (G24 / R19).

Input: the raw bytes of ``git diff-tree -r -z --raw --no-renames <base> <cand>``,
the raw bytes of ``git ls-tree -r -z <cand>`` and a caller policy (never-list,
manual-key list). Output: ALLOW, NEEDS_MANUAL_KEY or REFUSE plus one ASCII
reason per offending path.

Rule that closes the redirect class: compare TREE PATH TEXT, never OS paths.
Any form Windows could resolve to another name is refused up front
("suspicious form = refuse"); nothing is resolved and compared. No file
system, git call, config, remote or network: this module imports none of
os, pathlib, subprocess or socket, and never returns a key or a push result.
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

ALLOW = "ALLOW"
NEEDS_MANUAL_KEY = "NEEDS_MANUAL_KEY"
REFUSE = "REFUSE"

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 50_000

_SHA = rb"[0-9a-f]{40}|[0-9a-f]{64}"
_DIFF_META = re.compile(rb":([0-7]{6}) ([0-7]{6}) (" + _SHA + rb") (" + _SHA + rb") ([ADMT])")
_TREE_ENTRY = re.compile(rb"([0-7]{6}) (blob|commit) (" + _SHA + rb")\t(.+)", re.S)
_TREE_MODES = {b"100644": b"blob", b"100755": b"blob", b"120000": b"blob", b"160000": b"commit"}
_LINK_MODES = frozenset((b"120000", b"160000"))
_NULL_MODE = b"000000"

_DEVICES = frozenset(
    ["con", "prn", "aux", "nul", "conin$", "conout$", "clock$"]
    + [prefix + digit for prefix in ("com", "lpt") for digit in "0123456789"]
)
# "/" only shows up in a folded segment (fullwidth solidus and friends under NFKC).
_BAD_CHARS = frozenset('\\/:<>"|?*')
_BAD_CATEGORIES = frozenset(("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp", "Zs"))
# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties): invisible chars that are
# Mn/Lo (U+034F, fillers, variation selectors) and so slip past the category check;
# plus blank-rendering U+2800, U+2D7F, U+FFFC, U+1D159.
_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u2800\u2d7f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\ufffc\U0001bca0-\U0001bca3"
    "\U0001d159\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_SHORT_NAME = re.compile(r"~\d")
_SHADOW_SUFFIXES = (".pyc", ".pyd", ".pyw", ".so")
_STARTUP_HOOKS = frozenset(("sitecustomize", "usercustomize"))


class PathDecision(NamedTuple):
    verdict: str
    reasons: tuple[str, ...]


class _Refuse(Exception):
    """Whole-input refusal: malformed input or policy."""


def decide(diff_raw, ls_tree, never, manual_key) -> PathDecision:
    """Decide on a candidate's changed paths; never raises (fail closed)."""
    try:
        return _decide(diff_raw, ls_tree, never, manual_key)
    except _Refuse as exc:
        return PathDecision(REFUSE, (str(exc),))
    except Exception as exc:  # noqa: BLE001 - no exception escapes; refuse instead
        return PathDecision(REFUSE, ("internal error: " + type(exc).__name__,))


def _fold(text: str) -> str:
    # upper() first so NTFS-style upcase pairs (dotless i -> I) fold together too.
    text = unicodedata.normalize("NFKC", text)
    return unicodedata.normalize("NFKC", text.upper().casefold())


def _show(raw: bytes) -> str:
    text = repr(bytes(raw[:160]))[2:-1]
    return text + "..." if len(raw) > 160 else text


def _segments(raw: bytes):
    """(refusal, None) for a suspicious form, else (None, folded segments)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "path not valid UTF-8", None
    if unicodedata.normalize("NFC", text) != text:
        return "path not NFC", None
    folded = []
    for seg in text.split("/"):
        if seg in ("", ".", ".."):
            return "empty or dot segment", None
        fold = _fold(seg)
        for form in (seg, fold):
            if form != form.strip(" ") or form.endswith("."):
                return "segment starts or ends in space or ends in dot", None
            if _IGNORABLE.search(form) or any(
                ch in _BAD_CHARS or (ch != " " and unicodedata.category(ch) in _BAD_CATEGORIES)
                for ch in form
            ):
                return "segment has a reserved, control, bidi or zero-width character", None
            if _SHORT_NAME.search(form):
                return "segment looks like an 8.3 short name", None
        if fold.split(".")[0].rstrip(" ") in _DEVICES:
            return "segment is a reserved device name", None
        if fold == ".git":
            return "segment is .git", None
        folded.append(fold)
    return None, tuple(folded)


def _records(data, label: str, width: int) -> list:
    if not isinstance(data, bytes):
        raise _Refuse(label + " input is not bytes")
    if len(data) > MAX_INPUT_BYTES or data.count(b"\0") > MAX_RECORDS * width:
        raise _Refuse(label + " input too large")
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise _Refuse(label + " input truncated: no NUL terminator")
    fields = data[:-1].split(b"\0")
    if len(fields) % width:
        raise _Refuse(label + " input truncated: incomplete record")
    return [tuple(fields[i:i + width]) for i in range(0, len(fields), width)]


def _policy(patterns, label: str, required: bool) -> list:
    if not isinstance(patterns, (list, tuple, set, frozenset)) or (required and not patterns):
        raise _Refuse(label + " malformed or empty")
    rules = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip("/"):
            raise _Refuse(label + " has an empty or non-text pattern")
        body = pattern.strip("/")
        anchored = "/" in body or pattern.startswith("/")
        rules.append((pattern.endswith("/"), anchored, tuple(_fold(s) for s in body.split("/"))))
    return rules


def _glob(pattern: str, seg: str) -> bool:
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), seg, re.S) is not None


def _matches(rules: list, segs: tuple) -> bool:
    # gitignore-like: no inner "/" = any depth; trailing "/" = directory; a match
    # on a leading part of the path covers everything below it.
    for dir_only, anchored, psegs in rules:
        last = len(segs) - len(psegs) - (1 if dir_only else 0)
        if last < 0:
            continue
        for start in [0] if anchored else range(last + 1):
            if all(_glob(p, s) for p, s in zip(psegs, segs[start:start + len(psegs)])):
                return True
    return False


def _parse_tree(records: list, refuse: dict) -> dict:
    """raw path -> (mode, folded segs or None); refuses suspicious forms and collisions."""
    tree, nodes, siblings = {}, {}, {}
    for (rec,) in records:
        m = _TREE_ENTRY.fullmatch(rec)
        if not m or _TREE_MODES.get(m.group(1)) != m.group(2):
            raise _Refuse("ls-tree record malformed")
        mode, path = m.group(1), m.group(4)
        if path in tree:
            raise _Refuse("ls-tree has a duplicate path")
        why, segs = _segments(path)
        tree[path] = (mode, segs)
        if why:
            refuse.setdefault(path, why)
            continue
        # One folded prefix may stand for one raw prefix only (file/file, dir/dir, file/dir).
        # Prefixes are chained node ids, so a deep path costs linear time, not quadratic.
        node = 0
        for fold, raw in zip(segs, path.decode("utf-8").split("/")):
            node, seen = nodes.setdefault((node, fold), (len(nodes) + 1, raw))
            if seen != raw:
                refuse.setdefault(path, "case-fold collision with another tree path")
                break
        if "__pycache__" in segs:
            refuse.setdefault(path, "python module shadowing")
        siblings.setdefault(segs[:-1], set()).add(segs[-1])
    for parent, names in siblings.items():
        for name in names:
            stems = {name.split(".")[0], name.rsplit(".", 1)[0]}
            is_init = name == "__init__.py" or (name.endswith(_SHADOW_SUFFIXES) and "__init__" in stems)
            shadow = is_init and parent and parent[-1] + ".py" in siblings.get(parent[:-1], ())
            shadow = shadow or (name.endswith(_SHADOW_SUFFIXES) and any(s + ".py" in names for s in stems))
            if shadow:
                culprit = "/".join(parent + (name,)).encode("utf-8", "backslashreplace")
                refuse.setdefault(culprit, "python module shadowing")
    return tree


def _decide(diff_raw, ls_tree, never, manual_key) -> PathDecision:
    never_rules = _policy(never, "never-list", True)
    key_rules = _policy(manual_key, "manual-key list", False)
    diff = _records(diff_raw, "diff-tree", 2)
    if not diff:
        raise _Refuse("nothing to push: empty diff")
    refuse, key = {}, {}
    tree = _parse_tree(_records(ls_tree, "ls-tree", 1), refuse)
    seen = set()
    for meta, path in diff:
        m = _DIFF_META.fullmatch(meta)
        if not m or not path:
            raise _Refuse("diff-tree record malformed or unknown status")
        src, dst, status = m.group(1), m.group(2), m.group(5)
        if (src == _NULL_MODE) != (status == b"A") or (dst == _NULL_MODE) != (status == b"D"):
            raise _Refuse("diff-tree modes do not fit the status")
        if any(mode not in _TREE_MODES for mode in (src, dst) if mode != _NULL_MODE):
            raise _Refuse("diff-tree mode is not a tree mode")
        if path in seen:
            raise _Refuse("diff-tree has a duplicate path")
        seen.add(path)
        why, segs = _segments(path)
        if why:
            refuse.setdefault(path, why)
            continue
        if status == b"D":
            if path in tree:
                refuse.setdefault(path, "deleted path is still in the candidate tree")
        elif path not in tree:
            refuse.setdefault(path, "changed path missing from the candidate tree")
        elif tree[path][0] != dst:
            refuse.setdefault(path, "diff mode differs from the candidate tree")
        name = segs[-1]
        if src in _LINK_MODES or dst in _LINK_MODES:
            refuse.setdefault(path, "symlink or submodule mode")
        if name == ".gitmodules":
            refuse.setdefault(path, ".gitmodules changed")
        hook = any(seg.split(".")[0] in _STARTUP_HOOKS for seg in segs)  # module or package
        if status != b"D" and (name.endswith(".pth") or hook):
            refuse.setdefault(path, "python startup hook (.pth or site/usercustomize)")
        if _matches(never_rules, segs):
            refuse.setdefault(path, "never-list path")
        elif _matches(key_rules, segs):
            key.setdefault(path, "manual-key path")
    if refuse:
        return PathDecision(REFUSE, tuple(sorted(_show(p) + ": " + r for p, r in refuse.items())))
    if key:
        return PathDecision(NEEDS_MANUAL_KEY, tuple(sorted(_show(p) + ": " + r for p, r in key.items())))
    return PathDecision(ALLOW, ())
