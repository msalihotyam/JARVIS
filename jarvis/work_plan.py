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
