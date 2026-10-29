"""Pure project-plan schema and content binding (PM09).

Classification consumes explicit facts; it does not validate policy, execute
checks, or grant authority.
Canonical JSON sorts object keys, preserves array order and exact string values,
uses compact UTF-8 JSON, and admits integers only for numeric schema fields.
Expiry is an explicit UTC Unix timestamp; no clock or environment is consulted.
"""

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import re
import unicodedata
from urllib.parse import unquote


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
    """Return (D4 size, reasons), without reading any model size class field.

    brief_facts is a dict with target_folders (a nonempty list/tuple of
    distinct folder strings) and explicit boolean existing_repo,
    outgoing_actions, money_actions, manual_checks and jarvis_repo facts.
    plan is a Plan or a dict with positive integer slice_count and
    list/tuples of new_dependencies, connector_classes and operation_classes.
    Any declared or unknown connector/operation class forces large, so the
    size never comes from the brief alone (CDXINT109 F2). Dependencies alone
    do not imply an install: any network install must be declared in outgoing_actions.
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
        classes = plan.connector_classes, plan.operation_classes
    elif type(plan) is dict:
        count = plan.get("slice_count")
        dependencies = plan.get("new_dependencies")
        classes = plan.get("connector_classes"), plan.get("operation_classes")
    else:
        return "large", reasons + ["unknown plan facts"]
    if type(count) is not int or count < 1:
        reasons.append("unknown slice_count")
    elif count > 15:
        reasons.append("more than 15 slices")
    if (type(dependencies) not in (list, tuple)
            or any(type(v) is not str or not v.strip() for v in dependencies)):
        reasons.append("unknown new_dependencies")
    for value, known in zip(classes, (CONNECTOR_CLASSES, OPERATION_CLASSES)):
        if type(value) not in (list, tuple) or any(v not in known for v in value):
            reason = "unknown action classes"
        elif value:
            reason = "declared action classes"
        else:
            continue
        if reason not in reasons:
            reasons.append(reason)
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
#: Closed class sets a plan may declare; anything else is refused by validate().
CONNECTOR_CLASSES = ("local",)
OPERATION_CLASSES = ("stage_write", "check")
_CHECK_ARG = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*")
#: RULE A (PM10FIX3): a target is a literal path. Any glob metacharacter, also
#: URL-encoded ("%2A") or as an NFKC lookalike (U+FF0A), is refused as "glob";
#: no glob is ever matched against protected or self-target names.
_GLOB = re.compile(r"[*?\[\]{}!]")
_DEVICES = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]"
                      r"|lpt[0-9¹²³])\s*(\..*)?")
_PROTECTED_SUFFIXES = (
    ".key", ".pem", ".crt", ".cer", ".der", ".p12", ".pfx", ".jks", ".keystore",
    ".kdbx", ".db", ".sqlite", ".sqlite3")
#: SQLite sidecars: -wal, -shm, -journal and the -mjXXXX super-journal.
_SIDECAR = re.compile(r"\.(db|sqlite3?)-")
#: Agent, tool and IDE config that agents or editors obey or run (lowercase
#: path components). Single source: jarvis.write_scope imports this tuple.
AGENT_CONFIG_NAMES = (
    ".claude", ".codex", ".cursor", ".gemini", "agents.md", "agents.override.md",
    "claude.md", "claude.local.md", "gemini.md", ".mcp.json", ".cursorrules",
    ".windsurfrules", ".aider.conf.yml", ".vscode", ".idea", ".devcontainer")
#: Also the I3 repo/agent-config set, so a plan cannot even declare them.
_PROTECTED_NAMES = (".jarvis", ".git", ".github", ".ssh") + AGENT_CONFIG_NAMES
_KEY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
#: I12 name heuristics inside the JARVIS repo; over-matching fails closed.
_SELF_WORDS = ("policy", "approval", "auth", "gate", "credential", "redact",
               "secret", "push", "release", "schema")
#: Known credential/redaction/authority code the name heuristic misses.
_SELF_FILES = ("jarvis/memory.py", "jarvis/contracts.py", "jarvis/memory_filter.py",
               "jarvis/settings.py", "jarvis/gmail_cli.py", "jarvis/app.py",
               "jarvis/work_plan.py", "jarvis/trusted_exec.py",
               "jarvis/owned_process.py", "jarvis/memory_guard.py",
               "jarvis/__init__.py",
               # PM31: the project-mode authority chain and its runtime core.
               "jarvis/work_planner.py", "jarvis/work_driver.py",
               "jarvis/work_review.py", "jarvis/work_checks.py",
               "jarvis/work_controls.py", "jarvis/work_evidence.py",
               "jarvis/work_ledger.py", "jarvis/work_regress.py",
               "jarvis/work_scout.py", "jarvis/write_scope.py",
               "jarvis/write_stage.py", "jarvis/policy_proxy.py",
               "jarvis/lifecycle.py", "jarvis/core.py", "jarvis/dispatch.py")
#: PM31 I12 self-dev profile: closed JARVIS-repo hard-deny set, refused
#: whatever the approval. Lowercase; "/" ends a subtree; every entry matches
#: at any folder depth (a root above the checkout, a nested JARVIS/ prefix).
#: CONVSAFE mirrors scripts/conveyor.py PROTECTED_*.
SELF_DENY = (
    "scripts/conveyor.py", "scripts/test_parallel.py", "scripts/ci.ps1",
    "scripts/quality.ps1", ".github/workflows/", ".claude/",
    "scripts/", "dev/", "packaging/", "build/", "data/", "jarvis/workers/",
    "uv.lock", "pyproject.toml", "dev.ps1", ".gitignore", ".gitattributes",
    "claude.md", "agents.md", "jarvis_runbook.md", "architecture.md",
    "project_context.md", "jarvis_active_state.md",
    "jarvis_v2plus_roadmap_and_decisions.md", "contracts.py")
#: Tests that pin authority behaviour (tests/<prefix>*): rewriting them would
#: unpin the gate they cover.
_SELF_TESTS = ("test_work_", "test_write_", "test_conveyor", "test_memory",
               "test_contracts", "test_settings", "test_trusted_exec",
               "test_owned_process")
#: D7: a JARVIS-repo run is always shadow; there is no switch to turn it off.
SHADOW_MODE = True
_SHADOW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


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
                   or p.endswith(_PROTECTED_SUFFIXES) or _SIDECAR.search(p)
                   for p in parts))


def _self_denied(parts):
    # Every tail is checked, so a checkout-folder prefix (JARVIS/scripts/..)
    # or a root overlap cannot hide a denied path (CDXINT109).
    for i in range(len(parts)):
        tail = "/".join(parts[i:])
        for entry in SELF_DENY:
            name = entry.rstrip("/")
            if tail == name or tail.startswith(name + "/"):
                return True
    return any(p == "tests" and n.startswith(_SELF_TESTS)
               for p, n in zip(parts, parts[1:]))


def _self_target(lowered):
    parts = lowered.split("/")
    # A same-named package dir, .pyd/.pyc or __pycache__ entry shadows the .py;
    # every tail is checked so a root above the checkout still matches.
    tails = ["/".join(parts[i:]) for i in range(len(parts))]
    return ("__pycache__" in parts or _self_denied(parts)
            or any(p.startswith("agents") for p in parts)
            or any(t == f[:-3] or t.startswith((f[:-3] + "/", f[:-3] + "."))
                   for t in tails for f in _SELF_FILES)
            or any(w in p for p in parts for w in _SELF_WORDS))


def shadow_candidate(run_id, slice_id, *, jarvis_repo):
    """D7 export of an accepted JARVIS-repo slice for the conveyor.

    Project mode never applies a JARVIS-repo slice to the canonical checkout
    or main and has no push path: the slice leaves as a candidate branch plus
    a report stub, and only the conveyor two-key gate may push main."""
    if jarvis_repo is not True:
        raise ValueError("shadow export is only for the JARVIS repo")
    for value in (run_id, slice_id):
        if type(value) is not str or not _SHADOW_ID.fullmatch(value):
            raise ValueError("invalid shadow id")
    if {run_id.casefold(), slice_id.casefold()} & {"main", "master", "head"}:
        raise ValueError("shadow id names a protected ref")
    branch = f"pm-shadow/{run_id}/{slice_id}"
    return {"branch": branch, "applies_to": "branch", "canonical_checkout": False,
            "push": False,
            "report_stub": (f"STATUS: PASS\nBRANCH: {branch}\n"
                            "COMMIT: (conveyor fills)\nPUSHED: no\n"
                            f"INTEGRATION LANE NEEDS: shadow slice {slice_id} of run {run_id}\n")}


def _target_refusal(target, jarvis_repo):
    # Category C: control, format (zero-width joiners git may ignore), unassigned.
    if (type(target) is not str or not target
            or any(unicodedata.category(c)[0] == "C" for c in target)):
        return Refusal("path_escape", repr(target))
    canonical = unicodedata.normalize("NFKC", unquote(target))
    if _GLOB.search(target) or _GLOB.search(canonical):
        return Refusal("glob", target)
    path = target.replace("\\", "/")
    subtree = path.endswith("/")
    if subtree:
        path = path[:-1]
    if path in ("", "."):
        return Refusal("repo_root", target)
    parts = path.split("/")
    # < > " are FindFirstFile DOS wildcards (CDXINT93 F2); | is invalid on Win32.
    if (path.startswith(("/", "~")) or any(c in ':~<>"|' for c in path)
            or any(p in ("", ".", "..") or p.endswith(".") or p[-1].isspace()
                   or p[0].isspace()
                   or _DEVICES.fullmatch(p.upper().lower()) for p in parts)):
        return Refusal("path_escape", target)
    # upper().lower() also folds "ı"/"ſ"/Kelvin sign that NTFS may upcase.
    lowered = path.upper().lower()
    if _protected(lowered):
        return Refusal("protected_path", target)
    # The pure validator cannot see a folder's files, so in the JARVIS repo
    # every subtree may contain a self-target and is refused (fail closed).
    if jarvis_repo and (subtree or _self_target(lowered)):
        return Refusal("self_target", target)
    # %XX escapes and NFKC lookalikes ("．．", fullwidth letters) are not literal.
    if canonical != target:
        return Refusal("path_escape", target)
    return None


def validate(plan, project_root, *, jarvis_repo, repo_prefix=None):
    """Return [] when plan passes PM10, else a list of Refusal reasons.

    project_root is the root the caller resolved for this plan and must name
    the same root as plan.project_root. jarvis_repo must be an explicit bool;
    True adds the I12 self-target set. repo_prefix ("" or "a/b/") is where
    project_root sits inside the JARVIS checkout: each target is then also
    checked as repo_prefix + target, as the checkout sees it (CDXINT109 F1).
    Unknown connector/operation classes are refused. Targets are literal paths relative to
    the root, in POSIX or Windows form; any glob, the root itself, anything
    absolute, escaping, device-like or otherwise unrecognised is refused (fail
    closed). A folder target (trailing / or \\) is a subtree scope, see scope().
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
        refusal = _target_refusal(target, jarvis_repo) or (
            repo_prefix is not None and _target_refusal(repo_prefix + target, True))
        if refusal:
            refusals.append(refusal)
    for name, known in (("connector_classes", CONNECTOR_CLASSES),
                        ("operation_classes", OPERATION_CLASSES)):
        refusals.extend(Refusal("unknown_class", v) for v in getattr(plan, name)
                        if v not in known)
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


def scope(plan, project_root, *, jarvis_repo, repo_prefix=None):
    """Return {target: "file" | "subtree"} when validate() passes, else None.

    A "subtree" target is a scope, not a file: the PM15 diff scope gate must
    check every real changed file under it one by one.
    """
    if validate(plan, project_root, jarvis_repo=jarvis_repo, repo_prefix=repo_prefix):
        return None
    targets = list(plan.targets)
    for item in plan.slices:
        targets.extend(item.targets)
    return {t: "subtree" if t.endswith(("/", "\\")) else "file" for t in targets}
