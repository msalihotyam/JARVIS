"""Pure project-plan schema and content binding (PM09).

Classification consumes explicit facts; it does not validate policy, execute
checks, or grant authority.
Canonical JSON sorts object keys, preserves array order and exact string values,
uses compact UTF-8 JSON, and admits integers only for numeric schema fields.
Expiry is an explicit UTC Unix timestamp; no clock or environment is consulted.
"""

from dataclasses import asdict, dataclass, fields
import fnmatch
import hashlib
import json
import re


class PlanSchemaError(ValueError):
    """Input does not match the closed plan schema."""


def _text(value):
    if type(value) is not str or not value.strip():
        raise PlanSchemaError("expected nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlanSchemaError("string must be valid UTF-8") from exc


def _integer(value):
    if type(value) is not int or value < 0:
        raise PlanSchemaError("expected nonnegative integer")


def _strings(value):
    if type(value) is not tuple:
        raise PlanSchemaError("expected tuple of strings")
    for item in value:
        _text(item)


def _object(cls, value, converters=None):
    if type(value) is not dict:
        raise PlanSchemaError(f"{cls.__name__}: expected object")
    expected = {field.name for field in fields(cls)}
    if set(value) != expected:
        raise PlanSchemaError(f"{cls.__name__}: missing or unknown fields")
    result = dict(value)
    for key, convert in (converters or {}).items():
        result[key] = convert(result[key])
    return cls(**result)


def _array(value, convert):
    if type(value) is not list:
        raise PlanSchemaError("expected JSON array")
    return tuple(convert(item) for item in value)


def _texts(value):
    return _array(value, lambda item: item)


@dataclass(frozen=True)
class PlanBudget:
    worker_sessions: int
    revisions_per_slice: int
    failed_slices: int
    wall_clock_seconds: int
    model_calls: int

    def __post_init__(self):
        for field in fields(self):
            _integer(getattr(self, field.name))


@dataclass(frozen=True)
class PlanCheck:
    check_id: str
    argv: tuple[str, ...]
    timeout_seconds: int
    output_cap_bytes: int
    fixture_hashes: tuple[str, ...]

    def __post_init__(self):
        _text(self.check_id)
        _strings(self.argv)
        if not self.argv:
            raise PlanSchemaError("check argv cannot be empty")
        _integer(self.timeout_seconds)
        _integer(self.output_cap_bytes)
        _strings(self.fixture_hashes)


@dataclass(frozen=True)
class PlanSlice:
    slice_id: str
    goal: str
    targets: tuple[str, ...]
    check_ids: tuple[str, ...]

    def __post_init__(self):
        _text(self.slice_id)
        _text(self.goal)
        _strings(self.targets)
        _strings(self.check_ids)


@dataclass(frozen=True)
class Plan:
    goal: str
    project_root: str
    slices: tuple[PlanSlice, ...]
    targets: tuple[str, ...]
    connector_classes: tuple[str, ...]
    operation_classes: tuple[str, ...]
    checks: tuple[PlanCheck, ...]
    budgets: PlanBudget
    child_count: int
    expires_at: int
    new_dependencies: tuple[str, ...]

    def __post_init__(self):
        _text(self.goal)
        _text(self.project_root)
        for name in ("targets", "connector_classes", "operation_classes",
                     "new_dependencies"):
            _strings(getattr(self, name))
        for name, cls in (("slices", PlanSlice), ("checks", PlanCheck)):
            value = getattr(self, name)
            if type(value) is not tuple or any(type(v) is not cls for v in value):
                raise PlanSchemaError(f"{name}: expected tuple of {cls.__name__}")
        if type(self.budgets) is not PlanBudget:
            raise PlanSchemaError("expected PlanBudget")
        _integer(self.child_count)
        _integer(self.expires_at)

    @classmethod
    def from_dict(cls, value):
        return _object(cls, value, {
            "slices": lambda v: _array(v, lambda s: _object(PlanSlice, s, {
                "targets": _texts, "check_ids": _texts})),
            "checks": lambda v: _array(v, lambda c: _object(PlanCheck, c, {
                "argv": _texts, "fixture_hashes": _texts})),
            "budgets": lambda v: _object(PlanBudget, v),
            **{name: _texts for name in ("targets", "connector_classes",
                                        "operation_classes", "new_dependencies")},
        })

    @classmethod
    def from_json(cls, value):
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise PlanSchemaError(f"duplicate field: {key}")
                result[key] = item
            return result

        try:
            return cls.from_dict(json.loads(value, object_pairs_hook=unique_object))
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise PlanSchemaError("invalid plan JSON") from exc

    def to_dict(self):
        """Return detached JSON-compatible data, never mutable plan internals."""
        return json.loads(self.canonical_json())

    def canonical_json(self):
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)

    @property
    def plan_hash(self):
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def grant_envelope(self):
        """Derive declared scope; this is not validation or an approval token.

        The full immutable plan is retained so per-slice restrictions and check
        details cannot be lost by flattening scope. Consumers must still apply
        Core policy, approval-class checks and expiry against an injected clock.
        """
        return GrantEnvelope(self.plan_hash, self)


@dataclass(frozen=True)
class GrantEnvelope:
    plan_hash: str
    plan: Plan

    def __post_init__(self):
        if type(self.plan) is not Plan or self.plan_hash != self.plan.plan_hash:
            raise PlanSchemaError("envelope hash does not match plan")

    def to_dict(self):
        return {"plan_hash": self.plan_hash, "plan": self.plan.to_dict()}


def classify(brief_facts, plan):
    """Return (D4 size, reasons), without reading any model class field.

    brief_facts is a dict with target_folders (a nonempty list/tuple of
    distinct folder strings) and explicit boolean existing_repo,
    outgoing_actions, money_actions, manual_checks and jarvis_repo facts.
    plan is a Plan or a dict with positive integer slice_count and a
    list/tuple of new_dependencies. Dependencies alone do not imply an
    install: any network install must be declared in outgoing_actions.
    Missing/invalid facts fail closed. More than 15 slices is always large,
    including in an existing repo. No filesystem or environment is read.
    """
    if type(brief_facts) is not dict:
        return "large", ["unknown brief facts"]
    reasons = []
    flags = ("existing_repo", "outgoing_actions", "money_actions",
             "manual_checks", "jarvis_repo")
    for name in flags:
        if type(brief_facts.get(name)) is not bool:
            reasons.append(f"unknown {name}")
        elif name != "existing_repo" and brief_facts[name]:
            reasons.append(name)
    folders = brief_facts.get("target_folders")
    if (type(folders) not in (list, tuple) or not folders
            or any(type(v) is not str or not v.strip() for v in folders)):
        reasons.append("unknown target_folders")
    elif len(set(folders)) != len(folders):
        reasons.append("duplicate target_folders")
    if type(plan) is Plan:
        count = len(plan.slices)
        dependencies = plan.new_dependencies
    elif type(plan) is dict:
        count = plan.get("slice_count")
        dependencies = plan.get("new_dependencies")
    else:
        return "large", reasons + ["unknown plan facts"]
    if type(count) is not int or count < 1:
        reasons.append("unknown slice_count")
    elif count > 15:
        reasons.append("more than 15 slices")
    if (type(dependencies) not in (list, tuple)
            or any(type(v) is not str or not v.strip() for v in dependencies)):
        reasons.append("unknown new_dependencies")
    if reasons:
        return "large", reasons
    if count <= 5 and len(folders) == 1 and not brief_facts["existing_repo"]:
        return "small", ["at most 5 slices in one new folder"]
    if brief_facts["existing_repo"]:
        return "medium", ["existing repo with at most 15 slices"]
    return "medium", ["at most 15 slices; exceeds small scope"]


# PM10 plan validator. Pure string checks: no filesystem, environment or clock.

#: Core check allowlist (I7): argv prefixes run without a shell. Arguments after
#: a prefix may only be dotted test ids, never flags, paths or shell syntax.
CHECK_ALLOWLIST = (("python", "-m", "unittest"),)
_CHECK_ARG = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*")
#: Glob segments are judged per segment ("*" and "?" never cross "/", no
#: dotfile exception) after Win32 folding. Only "*" is checked exactly against
#: every protected, self-target, device and 8.3 short-name predicate; "?" (it
#: can spell a Win32-stripped trailing "." or " ", CDXINT93 A1), "[", "]", "{",
#: "}" and "!" are never proven safe and are refused outright (fail closed).
#: A literal trailing "." or whitespace is already path_escape, so "*" can only
#: reach such an alias at a segment end, where it is always refused.
_GLOB = re.compile(r"[*?\[\]{}!]")
_UNPROVABLE = re.compile(r"[?\[\]{}!]")
_DEVICES = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]"
                      r"|lpt[0-9¹²³])\s*(\..*)?")
_DEVICE_NAMES = ("con", "prn", "aux", "nul", "conin$", "conout$") + tuple(
    p + d for p in ("com", "lpt") for d in "0123456789¹²³")
_PROTECTED_SUFFIXES = (
    ".key", ".pem", ".crt", ".cer", ".der", ".p12", ".pfx", ".jks", ".keystore",
    ".kdbx", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
    ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".sqlite3-wal",
    ".sqlite3-shm", ".sqlite3-journal")
#: Also the I3 repo/agent-config set, so a plan cannot even declare them.
_PROTECTED_NAMES = (".jarvis", ".git", ".claude", ".codex", ".github",
                    "agents.md", "claude.md")
_KEY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
#: 8.3 short names a "*" can match (FindFirstFile also matches them): dot names
#: become NAME~N, ".env*" any ENV..., and extensions are cut to three letters;
#: after collisions NTFS hashes them to two letters + four hex + ~N.
_SHORT_PREFIXES = tuple(n[1:7] + "~" for n in _PROTECTED_NAMES
                        if n.startswith(".")) + ("env",)
_SHORT_SUFFIXES = tuple({s[:4] for s in _PROTECTED_SUFFIXES})
#: I12 name heuristics inside the JARVIS repo; over-matching fails closed.
_SELF_WORDS = ("policy", "approval", "auth", "gate", "credential", "redact",
               "secret", "push", "release", "schema")
#: Known credential/redaction/authority code the name heuristic misses.
_SELF_FILES = ("jarvis/memory.py", "jarvis/contracts.py", "jarvis/memory_filter.py",
               "jarvis/settings.py", "jarvis/gmail_cli.py", "jarvis/app.py",
               "jarvis/work_plan.py", "jarvis/trusted_exec.py",
               "jarvis/owned_process.py", "jarvis/memory_guard.py")


@dataclass(frozen=True)
class Refusal:
    """One typed validation failure; code is the stable machine reason."""
    code: str
    detail: str


def _protected(lowered):
    parts = lowered.split("/")
    return (parts[0] == "data"
            or any(p.startswith(".env") or p in _PROTECTED_NAMES
                   or p.startswith(_KEY_PREFIXES)
                   or p.endswith(_PROTECTED_SUFFIXES) for p in parts))


def _self_target(lowered):
    parts = lowered.split("/")
    return (parts[0] in ("scripts", ".claude") or lowered in _SELF_FILES
            or any(w in p for p in parts for w in _SELF_WORDS))


# Exact tests for a segment glob of literals, "?" and "*" only.
def _fits(glob, text):
    return len(glob) == len(text) and all(g in ("?", t) for g, t in zip(glob, text))


def _may_start(glob, prefix):
    head, star, _ = glob.partition("*")
    n = min(len(head), len(prefix)) if star else len(prefix)
    return len(head) >= n and _fits(head[:n], prefix[:n])


def _may_end(glob, suffix):
    return _may_start(glob[::-1], suffix[::-1])


def _may_contain(glob, word):
    return "*" in glob or any(_fits(glob[i:i + len(word)], word)
                              for i in range(len(glob) - len(word) + 1))


def _glob_protected(part, index):
    return (any(_may_start(part, p) for p in
                (".env", *_KEY_PREFIXES, *_SHORT_PREFIXES))
            or any(_may_start(part, p[:2]) and "*" in part[:7]
                   for p in _SHORT_PREFIXES)
            or any(fnmatch.fnmatchcase(n, part) for n in
                   _PROTECTED_NAMES + (("data",) if index == 0 else ()))
            or any(_may_end(part, s) for s in
                   _PROTECTED_SUFFIXES + _SHORT_SUFFIXES))


# Win32 maps a device name plus any whitespace and/or extension to the device.
def _glob_device(part):
    return any(_may_start(part, d) and (
        len(part) == len(d) or "*" in part[:len(d) + 1]
        or part[len(d)] in ".?" or part[len(d)].isspace()) for d in _DEVICE_NAMES)


def _glob_self(part, index):
    return ((index == 0 and any(fnmatch.fnmatchcase(n, part)
                                for n in ("scripts", ".claude")))
            or any(_may_contain(part, w) for w in _SELF_WORDS))


def _target_refusal(target, jarvis_repo):
    if (type(target) is not str or not target
            or any(ord(c) < 32 or ord(c) == 127 for c in target)):
        return Refusal("path_escape", repr(target))
    path = target.replace("\\", "/")
    parts = path.split("/")
    # < > " are FindFirstFile DOS wildcards (CDXINT93 F2); | is invalid on Win32.
    if (path.startswith(("/", "~")) or any(c in ':~<>"|' for c in path)
            or any(p in ("", ".", "..") or p.endswith(".") or p[-1].isspace()
                   or _DEVICES.fullmatch(p.upper().lower()) for p in parts)):
        return Refusal("path_escape", target)
    # upper().lower() also folds "ı"/"ſ"/Kelvin sign that NTFS may upcase.
    lowered = path.upper().lower()
    unprovable = False
    for index, part in enumerate(lowered.split("/")):
        if not _GLOB.search(part):
            continue
        if _UNPROVABLE.search(part):
            if (any(fnmatch.fnmatchcase(probe, part) for probe in ("..", "."))
                    or _glob_device(part)):
                return Refusal("path_escape", target)
            if jarvis_repo and _glob_self(part, index):
                return Refusal("self_target", target)
            unprovable = True
        elif jarvis_repo and _glob_self(part, index):
            return Refusal("self_target", target)
        elif _glob_protected(part, index):
            return Refusal("protected_path", target)
        elif (any(fnmatch.fnmatchcase(probe, part) for probe in ("..", "."))
              or _glob_device(part)):
            return Refusal("path_escape", target)
    if _GLOB.search(lowered):
        if any(fnmatch.fnmatchcase(p, lowered) for p in ("data", "data/x")):
            return Refusal("protected_path", target)
        if jarvis_repo and any(fnmatch.fnmatchcase(p, lowered) for p in
                               _SELF_FILES + ("scripts/x", ".claude/x")):
            return Refusal("self_target", target)
    if unprovable:
        plain = _UNPROVABLE.sub("", lowered)
        if jarvis_repo and _self_target(plain) and not _protected(plain):
            return Refusal("self_target", target)
        return Refusal("protected_path", target)
    if _protected(lowered):
        return Refusal("protected_path", target)
    if jarvis_repo and _self_target(lowered):
        return Refusal("self_target", target)
    return None


def validate(plan, project_root, *, jarvis_repo):
    """Return [] when plan passes PM10, else a list of Refusal reasons.

    project_root is the root the caller resolved for this plan and must name
    the same root as plan.project_root. jarvis_repo must be an explicit bool;
    True adds the I12 self-target set. Targets are relative to the root, in
    POSIX or Windows form; anything absolute, escaping, device-like, any glob
    that could match a protected or self-target name, any "[]{}!" glob, or
    otherwise unrecognised is refused (fail closed).
    """
    if type(plan) is not Plan:
        return [Refusal("unknown_plan", type(plan).__name__)]
    refusals = []
    if type(jarvis_repo) is not bool:
        refusals.append(Refusal("unknown_fact", "jarvis_repo"))
        jarvis_repo = True

    def root(value):
        return value.replace("\\", "/").rstrip("/").casefold()

    if (type(project_root) is not str or not project_root.strip()
            or root(project_root) != root(plan.project_root)):
        refusals.append(Refusal("root_mismatch", repr(project_root)))
    elif any(p == "data" or p.startswith(".env") or p in _PROTECTED_NAMES
             for p in root(project_root).split("/")):
        refusals.append(Refusal("protected_path", project_root))
    targets = list(plan.targets)
    for item in plan.slices:
        targets.extend(item.targets)
    for target in dict.fromkeys(targets):
        refusal = _target_refusal(target, jarvis_repo)
        if refusal:
            refusals.append(refusal)
    check_ids = [check.check_id for check in plan.checks]
    for check in plan.checks:
        argv = check.argv
        prefix = next((p for p in CHECK_ALLOWLIST if argv[:len(p)] == p), None)
        if prefix is None or not all(
                _CHECK_ARG.fullmatch(a)
                and a.split(".")[0] in ("tests", "test")
                for a in argv[len(prefix):]):
            refusals.append(Refusal("check_not_allowlisted", check.check_id))
        if check_ids.count(check.check_id) > 1:
            refusals.append(Refusal("unknown_check", check.check_id))
    for item in plan.slices:
        for check_id in item.check_ids:
            if check_id not in check_ids:
                refusals.append(Refusal("unknown_check", check_id))
    if max(plan.child_count, len(plan.slices)) > 5:
        refusals.append(Refusal("too_many_children", str(plan.child_count)))
    return refusals
