"""Safe local settings and cost policy."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from jarvis.contracts import (
    MONTHLY_HARD_LIMIT_NANO_USD,
    BillingIntegrityError,
    ErrorCode,
    PermissionMode,
    credential_bearing,
    nano_usd,
)
# The GitHub grammars themselves, so a configured value is judged by the same
# contracts a request is - never by a second, weaker copy living here.
from jarvis.github import (
    github_principal_refusal,
    github_raw_destination_refusal,
)
from jarvis.gmail import gmail_principal_refusal

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: The fixed monthly monetary hard limit. Changing it is a user decision, not
#: a configuration knob, so it is validated exactly rather than by range. The
#: canonical value lives in ``jarvis.contracts`` as exact nano-USD, because the
#: storage boundary enforces the same cap and never trusts a caller-supplied
#: larger limit.
MONTHLY_HARD_LIMIT_USD = float(MONTHLY_HARD_LIMIT_NANO_USD) / 1_000_000_000

#: The largest per-call ceiling an operator may set. NOT the monthly cap -
#: that is fixed in contracts and is not configurable at all - but a bound
#: that keeps a typo from authorizing an absurd single call.
MAX_CALL_BUDGET_USD = 20.0

#: Bounds on the approval window, in minutes.
MIN_APPROVAL_TTL_MINUTES = 1
MAX_APPROVAL_TTL_MINUTES = 1440

#: The closed effort vocabulary the manual Claude selection accepts: the
#: published ``--effort`` values of the installed Claude Code integration.
#: These are DATA passed to the CLI as ``--effort <value>``; a closed set is
#: what makes the value unspendable as a second CLI argument. Nothing
#: speculative is admitted; the set is provider-scoped to Claude.
CLAUDE_EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")

#: The grammar of a manually selected Claude model identifier. Bounded on
#: purpose: it must start with a letter or digit (so it can never read as a
#: CLI flag), admit only letters, digits, dots, underscores and hyphens (no
#: whitespace, separators, quotes, slashes or control characters), and never
#: exceed 64 characters. This is a SHAPE check, not a list of model
#: releases; which names the installed CLI accepts stays the CLI's business.
CLAUDE_MODEL_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_MODEL_GRAMMAR_MESSAGE = (
    "claude_model must be 1-64 characters of letters, digits, dot, "
    "underscore or hyphen, starting with a letter or digit"
)


def validate_claude_model(value: object) -> str:
    """The one validation for a manually selected Claude model identifier.

    Raises ``ValueError`` with a bounded, value-free message; the offending
    text is never echoed back.
    """
    if type(value) is not str or not CLAUDE_MODEL_PATTERN.match(value):
        raise ValueError(_MODEL_GRAMMAR_MESSAGE)
    return value


def validate_claude_effort(value: object) -> str:
    """The one validation for a manually selected Claude effort value."""
    if type(value) is not str or value not in CLAUDE_EFFORT_VALUES:
        raise ValueError(
            "claude_effort must be one of: " + ", ".join(CLAUDE_EFFORT_VALUES) + "."
        )
    return value


def _finite(name: str, value: float) -> float:
    """Reject a monetary/limit setting that is not an ordinary finite number.

    Booleans, NaN and both infinities are refused here: every ordinary range
    comparison below silently passes NaN, so a NaN limit would otherwise look
    like a satisfied policy.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class Settings:
    memory_database: Path = PROJECT_ROOT / "data" / "jarvis.db"
    #: THE global execution barrier. Not a permission mode, not a gate, and
    #: deliberately not approvable: when it is False nothing may start work,
    #: and no approval, grant, permission mode or paid-API decision can
    #: satisfy it, because it is not asking a question. Default True, so an
    #: installation with no configuration behaves exactly as it always has.
    execution_enabled: bool = True
    permission_mode: PermissionMode = PermissionMode.SESSION
    monthly_hard_limit_usd: float = MONTHLY_HARD_LIMIT_USD
    paid_api_enabled: bool = False
    #: Compatibility metadata only. A non-blank approval id is required to
    #: flip ``paid_api_enabled`` and is stored stripped, but the id itself is
    #: NOT execution authority: it is never verified against a persisted user
    #: decision. While the switch is off, a task that requires the paid API
    #: raises the task-specific PAID_API_GATE approval in ``jarvis.core``;
    #: with the switch on that gate is not raised, and only a projected
    #: hard-cap breach raises the separate COST_CAP_GATE.
    paid_api_approval_id: str | None = None
    approval_ttl_minutes: int = 30
    claude_call_budget_usd: float = 0.25
    #: Manual Claude selection (provider-scoped on purpose: only Claude is
    #: wired, and the names must not pretend a multi-provider router exists).
    #: ``None`` preserves the CLI's own default behavior exactly; a set value
    #: is operator authority, never connector or runtime choice. Neither
    #: field is a secret: both are bounded, validated, displayable data.
    claude_model: str | None = None
    claude_effort: str | None = None
    #: GitHub v1 authority, and it is CONFIGURATION rather than source. The
    #: build ships no account and no repository: an operator states the login
    #: their local CLI session belongs to and the exact repositories that
    #: session may read, and the composition root turns those two statements
    #: into the Core-owned identity binding and the exact
    #: ``DestinationGrant`` rows. Nothing is inferred - not from the CLI's own
    #: session, not from a local git remote, not from task text - so an
    #: unconfigured installation can reach no repository at all.
    #:
    #: ``github_principal`` is a NON-SECRET login, never a token. There is no
    #: setting anywhere in this product that holds a GitHub credential.
    github_principal: str | None = None
    #: Canonical repository destinations, spelled exactly
    #: ``github/<owner>/<repository>``. Each becomes one exact destination
    #: grant, and a read outside that list is refused by Core's destination
    #: policy rather than by the connector.
    github_repositories: tuple[str, ...] = ()
    #: Gmail minimum usable-v1 authority, configuration like GitHub's: the
    #: one mailbox address the local Google Workspace CLI session belongs to.
    #: The composition root turns it into the Core-owned identity binding
    #: and one exact ``DestinationGrant`` per enabled Gmail capability for
    #: ``gmail/<account>``. A NON-SECRET address, never a token: there is no
    #: setting anywhere in this product that holds a Google credential.
    gmail_principal: str | None = None

    @property
    def gmail_config_dir(self) -> Path:
        """The JARVIS-dedicated Google Workspace CLI configuration directory.

        DERIVED, never configured: it lives beside the memory database, in
        JARVIS's own data directory, so the one credential route Gmail
        accepts is always the login an operator performed INTO this
        directory (``GOOGLE_WORKSPACE_CLI_CONFIG_DIR=<this> gws auth login``)
        and never the operator's ambient ``~/.config/gws`` session. Not a
        secret and not a credential: a location.
        """
        return Path(self.memory_database).resolve().parent / "gws-gmail"

    def __post_init__(self) -> None:
        # THE typed runtime authority, and it has to actually be typed. Core
        # compares ``permission_mode`` with ``is`` against enum members and
        # reads the booleans as booleans, so a raw ``"strict"`` string or an
        # ``int`` 1 would sail past every policy check by never matching -
        # failing open in the quietest possible way. Each field is therefore
        # checked for its EXACT runtime type here, at the one place every
        # construction passes through.
        if type(self.execution_enabled) is not bool:
            raise ValueError("execution_enabled must be exactly a bool")
        if type(self.permission_mode) is not PermissionMode:
            raise ValueError("permission_mode must be a PermissionMode member")
        if type(self.paid_api_enabled) is not bool:
            raise ValueError("paid_api_enabled must be exactly a bool")
        # bool is an int subclass, so ``True`` would otherwise pass as 1 minute.
        if type(self.approval_ttl_minutes) is not int:
            raise ValueError("approval_ttl_minutes must be exactly an int")
        if type(self.claude_call_budget_usd) not in (int, float):
            raise ValueError("claude_call_budget_usd must be a number")
        if type(self.monthly_hard_limit_usd) not in (int, float):
            raise ValueError("monthly_hard_limit_usd must be a number")
        # A path may legitimately arrive as a string - that is how callers
        # and tests have always written it - so it is CONVERTED to the Path
        # the runtime actually uses, not merely accepted as text. What is not
        # negotiable is that the result be absolute: a relative database
        # would mean a different file from a different working directory, and
        # a Settings that says "the database" must name exactly one.
        if type(self.memory_database) is str:
            object.__setattr__(self, "memory_database", Path(self.memory_database))
        if not isinstance(self.memory_database, Path):
            raise ValueError("memory_database must be a path")
        if not self.memory_database.is_absolute():
            raise ValueError("memory_database must be an absolute path")
        # Finiteness first: NaN passes every ordinary comparison below.
        limit = _finite("monthly_hard_limit_usd", self.monthly_hard_limit_usd)
        _finite("claude_call_budget_usd", self.claude_call_budget_usd)
        try:
            limit_nano = nano_usd(limit, "monthly_hard_limit_usd")
        except BillingIntegrityError as exc:
            raise ValueError(str(exc)) from exc
        if limit_nano != MONTHLY_HARD_LIMIT_NANO_USD:
            raise ValueError("The hard limit is fixed at 20 USD without a new user decision")
        # Both construction doors (direct construction here and
        # ``enable_paid_api`` below) apply the same stripped/non-blank rule
        # and store the id in its stripped form.
        approval_id = self.paid_api_approval_id
        if approval_id is not None:
            if type(approval_id) is not str or not approval_id.strip():
                raise ValueError(
                    "paid_api_approval_id must be a non-blank string when set"
                )
            approval_id = approval_id.strip()
            # THE established provider-neutral policy, not a second weaker
            # one. It is syntax-aware, so a labelled value like
            # ``api_key:HUNTER2`` or ``AWS_SECRET_ACCESS_KEY:...`` is caught
            # where a prefix check would wave it through - and this field is
            # inert metadata that has no business holding either.
            if _looks_like_a_credential(approval_id):
                raise ValueError(
                    "paid_api_approval_id looks like a credential; it is "
                    "inert metadata and must never hold one"
                )
            object.__setattr__(self, "paid_api_approval_id", approval_id)
        if self.paid_api_enabled and approval_id is None:
            raise ValueError("Enabling paid APIs requires an explicit approval id")
        # GitHub authority, validated by the GitHub contracts themselves so
        # this module grows no second grammar. A configured value that would
        # not pass there is refused HERE, at construction, rather than
        # becoming a grant nobody can use or - worse - a plausible one.
        principal = self.github_principal
        if principal is not None:
            reason = github_principal_refusal(principal)
            if reason is not None:
                raise ValueError(f"github_principal is unusable: {reason}")
        if type(self.github_repositories) is list:
            object.__setattr__(
                self, "github_repositories", tuple(self.github_repositories)
            )
        if type(self.github_repositories) is not tuple:
            raise ValueError(
                "github_repositories must be a tuple of github/<owner>/<repository>"
            )
        for entry in self.github_repositories:
            reason = github_raw_destination_refusal(entry)
            if reason is not None:
                raise ValueError(f"github_repositories entry is unusable: {reason}")
        if self.github_repositories and principal is None:
            # A repository with nobody to read it as would be an authority
            # that can never be exercised, and an identity-less GitHub grant
            # is exactly what Core refuses at execution. Refused here instead,
            # where the operator can see why.
            raise ValueError(
                "github_repositories requires github_principal to be set"
            )
        if self.gmail_principal is not None:
            reason = gmail_principal_refusal(self.gmail_principal)
            if reason is not None:
                raise ValueError(f"gmail_principal is unusable: {reason}")
        if not MIN_APPROVAL_TTL_MINUTES <= self.approval_ttl_minutes <= (
            MAX_APPROVAL_TTL_MINUTES
        ):
            raise ValueError(
                "approval_ttl_minutes must be between "
                f"{MIN_APPROVAL_TTL_MINUTES} and {MAX_APPROVAL_TTL_MINUTES}"
            )
        if not 0 < self.claude_call_budget_usd <= MAX_CALL_BUDGET_USD:
            raise ValueError(
                "claude_call_budget_usd must be greater than 0 and at most "
                f"{MAX_CALL_BUDGET_USD}"
            )
        # Manual Claude selection. Validated here, at the one place every
        # construction passes through, so an invalid value can never be
        # persisted, loaded or handed to a connector.
        if self.claude_model is not None:
            object.__setattr__(
                self, "claude_model", validate_claude_model(self.claude_model)
            )
        if self.claude_effort is not None:
            object.__setattr__(
                self, "claude_effort", validate_claude_effort(self.claude_effort)
            )

    def enable_paid_api(self, approval_id: str) -> Settings:
        if not approval_id.strip():
            raise ValueError("approval_id is required")
        return replace(
            self, paid_api_enabled=True, paid_api_approval_id=approval_id.strip()
        )


# --------------------------------------------------------------------------
# Persistent configuration (M12 S4)
#
# One flat JSON document, one version, one authority. It holds operator
# PREFERENCES and nothing else: no secret, no credential, no monthly cap, no
# lifecycle fact. Everything it loads is handed to the ``Settings``
# constructor above, so the invariants that already existed stay the last
# word rather than being re-implemented here.
#
# The document names the database, which is why it cannot live inside one.
# --------------------------------------------------------------------------

#: The one supported document version. A missing, older or newer value is
#: not a migration opportunity - it is a document this build cannot vouch
#: for, and it is refused whole.
CONFIG_VERSION = 1

#: A hard ceiling on what will be read at all. A configuration file is a
#: handful of scalars; anything larger is not one, and refusing by size
#: before parsing keeps a hostile file from costing anything.
CONFIG_MAX_BYTES = 16_384

#: The closed key set. Exactly these, always all of them, in every document
#: this code writes or accepts. "Unknown key" and "missing key" are the same
#: verdict for the same reason: a document this build does not fully
#: understand is one whose meaning it cannot promise the operator.
CONFIG_KEYS = (
    "config_version",
    "execution_enabled",
    "permission_mode",
    "paid_api_enabled",
    "paid_api_approval_id",
    "claude_call_budget_usd",
    "approval_ttl_minutes",
    "memory_database",
    "claude_model",
    "claude_effort",
    # GitHub v1. Authority as CONFIGURATION: the login the local CLI session
    # belongs to, and the exact repositories it may read. Neither is a
    # credential, and there is no key anywhere in this document that holds
    # one.
    "github_principal",
    "github_repositories",
    # Gmail minimum. Authority as CONFIGURATION: the one mailbox the local
    # Google Workspace CLI session belongs to. Not a credential.
    "gmail_principal",
)

#: The pre-Gmail key set: the document exactly as the GitHub v1 build wrote
#: it. Accepted whole, with the Gmail setting unset, for the same reason the
#: two older shapes are: a second CLOSED shape, not an optional-keys hole.
PRE_GMAIL_CONFIG_KEYS = frozenset(CONFIG_KEYS) - {"gmail_principal"}

#: The pre-GitHub key set: the document exactly as a build before GitHub v1
#: wrote it. Accepted whole, with both GitHub settings unset, for the same
#: reason the legacy shape below is - an operator's existing preferences must
#: survive an upgrade without a repair - and it is a second CLOSED shape, not
#: an "optional keys" hole.
PRE_GITHUB_CONFIG_KEYS = PRE_GMAIL_CONFIG_KEYS - {
    "github_principal",
    "github_repositories",
}

#: The legacy v1 key set: the document exactly as the canonical ea1c123 build
#: wrote it, before manual Claude selection existed. A document carrying
#: EXACTLY this key set still loads VALID with both new settings unset, so an
#: operator's existing preferences survive without a repair. Any other shape
#: - one new key without the other, any unknown key, any missing
#: pre-existing key - is refused whole: this is a second accepted CLOSED
#: shape, never a general "optional keys" hole.
LEGACY_CONFIG_KEYS = PRE_GITHUB_CONFIG_KEYS - {"claude_model", "claude_effort"}

#: Raw provider tokens the canonical policy does not classify. It is
#: syntax-aware - it reads labels and delimiters - and a raw token carries
#: neither, so there is nothing for it to catch. These are therefore kept as
#: a STRICT ADDITION to ``credential_bearing``, never a replacement: this
#: field refuses a superset of what the shared egress policy refuses.
#:
#: This is NOT a second general credential engine, and must not become one.
#: It is a closed list of ANCHORED, recognizable raw token families, matched
#: only at the start of the value. There is deliberately no substring search
#: for "token", "secret" or "key": an approval id is allowed to contain an
#: ordinary English word, and refusing ``approval_tokenizer`` or
#: ``github-approval`` would be a false positive on inert metadata.
_EXTRA_CREDENTIAL_SHAPES = (
    # GitHub: fine-grained PAT, then the classic prefixed families.
    re.compile(r"\Agithub_pat_", re.IGNORECASE),
    re.compile(r"\Agh[pousr]_", re.IGNORECASE),
    # GitLab personal access token.
    re.compile(r"\Aglpat-", re.IGNORECASE),
    # Slack bot/user/app/refresh/workspace tokens, and app-level tokens.
    re.compile(r"\Axox[abprs]-", re.IGNORECASE),
    re.compile(r"\Axapp-", re.IGNORECASE),
    # Google OAuth access token.
    re.compile(r"\Aya29\.", re.IGNORECASE),
    # Google OAuth refresh token and API key (Gmail minimum).
    re.compile(r"\A1//0"),
    re.compile(r"\AAIza"),
)


def _looks_like_a_credential(candidate: str) -> bool:
    """The canonical provider-neutral policy, plus the raw shapes it misses."""
    if credential_bearing(candidate):
        return True
    return any(shape.match(candidate) for shape in _EXTRA_CREDENTIAL_SHAPES)


#: The approval id's grammar. Deliberately narrow: it is inert compatibility
#: metadata, so there is no reason for it to admit anything that could look
#: like a token, a path or a sentence.
_APPROVAL_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

def default_config_path() -> Path:
    """Where the document lives. One per operator, not per project."""
    return Path.home() / ".jarvis" / "config.json"


class ConfigStatus(str, Enum):
    """What the store had to say, separately from what was loaded."""

    #: No document exists. Ordinary first run; nothing is created.
    ABSENT = "absent"
    #: A complete, valid document was applied.
    VALID = "valid"
    #: A document exists and cannot be accepted. Nothing from it is applied.
    INVALID = "invalid"
    #: The document could not be read at all.
    UNREADABLE = "unreadable"


class ConfigError(Exception):
    """A refused document or a failed store operation. Bounded by design.

    It carries a ``code`` and a short operator-facing ``message`` that never
    contains the offending value, the underlying OS error, a path or a
    traceback - a configuration refusal is not a place to leak either the
    filesystem or whatever somebody pasted into a field.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SettingsLoadResult:
    """The settings to run with, and how they were arrived at.

    Two values rather than one, because ``Settings`` must stay a statement
    about policy and never carry diagnostics about its own provenance. A
    caller that needs to refuse an ordinary command on a broken store reads
    ``status``; everything downstream reads ``settings`` and nothing else.
    """

    settings: Settings
    status: ConfigStatus
    reason: str | None = None

    @property
    def usable(self) -> bool:
        """Whether the store was in a state ordinary commands may rely on."""
        return self.status in (ConfigStatus.ABSENT, ConfigStatus.VALID)


def _fail(message: str) -> None:
    raise ConfigError(ErrorCode.CONFIG_INVALID, message)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse a document whose object repeats a key.

    ``json`` keeps the last occurrence silently, so ``{"execution_enabled":
    false, "execution_enabled": true}`` would parse to ``true`` and the
    operator would have no way to know which half won.
    """
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            _fail("The configuration document repeats a key.")
        seen[key] = value
    return seen


def _exact_bool(value: object, key: str) -> bool:
    if type(value) is not bool:
        _fail(f"'{key}' must be exactly true or false.")
    return bool(value)


def _exact_int(value: object, key: str, low: int, high: int) -> int:
    # bool is an int subclass; a JSON ``true`` is not a minute count.
    if type(value) is not int:
        _fail(f"'{key}' must be a whole number.")
    number = int(value)  # type: ignore[arg-type]
    if not low <= number <= high:
        _fail(f"'{key}' must be between {low} and {high}.")
    return number


def _exact_amount(value: object, key: str) -> float:
    if type(value) not in (int, float):
        _fail(f"'{key}' must be a number.")
    amount = float(value)  # type: ignore[arg-type]
    if not math.isfinite(amount):
        _fail(f"'{key}' must be a finite number.")
    if not 0 < amount <= MAX_CALL_BUDGET_USD:
        _fail(f"'{key}' must be greater than 0 and at most {MAX_CALL_BUDGET_USD}.")
    return amount


def _exact_mode(value: object) -> PermissionMode:
    if type(value) is not str:
        _fail("'permission_mode' must be one of: strict, session, smart_auto.")
    try:
        return PermissionMode(value)
    except ValueError:
        _fail("'permission_mode' must be one of: strict, session, smart_auto.")
    raise AssertionError("unreachable")


def _exact_approval_id(value: object) -> str | None:
    """The id, stripped once, or ``None``. The value never reaches an error."""
    if value is None:
        return None
    if type(value) is not str:
        _fail("'paid_api_approval_id' must be a short identifier or null.")
    candidate = value.strip()  # type: ignore[union-attr]
    if not candidate:
        _fail("'paid_api_approval_id' must not be blank when set.")
    if not _APPROVAL_ID_PATTERN.match(candidate):
        _fail(
            "'paid_api_approval_id' must be 1-128 characters of letters, "
            "digits, dot, underscore, colon or hyphen."
        )
    if _looks_like_a_credential(candidate):
        _fail(
            "'paid_api_approval_id' looks like a credential. It is inert "
            "metadata and must never hold one."
        )
    return candidate


def _exact_database(value: object) -> Path:
    """An ABSOLUTE persisted path. Relative is refused, never re-resolved.

    A relative path stored here would mean a different database from a
    different working directory, so the same document would silently select
    different data. Normalization happens once, at set time, against the cwd
    the operator actually typed the command in.
    """
    if type(value) is not str or not value.strip():
        _fail("'memory_database' must be a non-empty path.")
    text = str(value)
    if "\x00" in text:
        _fail("'memory_database' must not contain a NUL character.")
    candidate = Path(text)
    if not candidate.is_absolute():
        _fail("'memory_database' must be an absolute path.")
    if candidate.is_dir():
        _fail("'memory_database' names a directory, not a database file.")
    return candidate


def normalize_database_path(raw: str, *, base: Path | None = None) -> Path:
    """Canonicalize an operator-supplied database path, once, at set time.

    ``~`` is expanded and a relative path is resolved against the directory
    the command was run in - then the ABSOLUTE result is what gets stored, so
    the document keeps meaning the same database no matter where a later
    command runs. Nothing is created: a database that does not exist yet is
    a perfectly ordinary thing to configure.
    """
    if type(raw) is not str or not raw.strip():
        _fail("'memory_database' must be a non-empty path.")
    if "\x00" in raw:
        _fail("'memory_database' must not contain a NUL character.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    resolved = candidate.resolve(strict=False)
    if resolved.is_dir():
        _fail("'memory_database' names a directory, not a database file.")
    return resolved


def _exact_claude_model(value: object) -> str | None:
    """A validated model identifier or ``None``. The value is never echoed."""
    if value is None:
        return None
    if type(value) is not str:
        _fail("'claude_model' must be a model identifier or null.")
    try:
        return validate_claude_model(value)
    except ValueError:
        _fail(_MODEL_GRAMMAR_MESSAGE)
    raise AssertionError("unreachable")


def _exact_claude_effort(value: object) -> str | None:
    """A validated effort value or ``None``. The value is never echoed."""
    if value is None:
        return None
    if type(value) is not str:
        _fail("'claude_effort' must be one of: "
              + ", ".join(CLAUDE_EFFORT_VALUES) + ", or null.")
    try:
        return validate_claude_effort(value)
    except ValueError:
        _fail(
            "'claude_effort' must be one of: "
            + ", ".join(CLAUDE_EFFORT_VALUES)
            + ", or null."
        )
    raise AssertionError("unreachable")


def _exact_github_principal(value: object) -> str | None:
    """The configured login, or ``None``. Never coerced, never a credential."""
    if value is None:
        return None
    if type(value) is not str:
        _fail("'github_principal' must be a string or null.")
    return value


def _exact_github_repositories(value: object) -> tuple[str, ...]:
    """The configured canonical repository destinations, exactly as written.

    A list of strings and nothing else: no coercion, no splitting of a single
    string, no de-duplication that could hide a typo. Each entry's spelling is
    judged by the GitHub destination grammar in the constructor.
    """
    if value is None:
        return ()
    if type(value) is not list or any(type(item) is not str for item in value):
        _fail("'github_repositories' must be a list of strings.")
    return tuple(value)


def _exact_gmail_principal(value: object) -> str | None:
    """The configured mailbox address, or ``None``. Never coerced."""
    if value is None:
        return None
    if type(value) is not str:
        _fail("'gmail_principal' must be a string or null.")
    return value


def _document_from_settings(settings: Settings) -> dict[str, object]:
    """The complete canonical document for a Settings. Always every key."""
    return {
        "config_version": CONFIG_VERSION,
        "execution_enabled": settings.execution_enabled,
        "permission_mode": settings.permission_mode.value,
        "paid_api_enabled": settings.paid_api_enabled,
        "paid_api_approval_id": settings.paid_api_approval_id,
        "claude_call_budget_usd": settings.claude_call_budget_usd,
        "approval_ttl_minutes": settings.approval_ttl_minutes,
        "memory_database": str(settings.memory_database),
        "claude_model": settings.claude_model,
        "claude_effort": settings.claude_effort,
        "github_principal": settings.github_principal,
        "github_repositories": list(settings.github_repositories),
        "gmail_principal": settings.gmail_principal,
    }


def _settings_from_document(document: dict[str, object]) -> Settings:
    """Validate a complete document into Settings, or raise ConfigError.

    Every field is checked exactly, and the result is then built through the
    ordinary ``Settings`` constructor - so the fixed monthly cap, the
    paid-API relationship and every existing bound are enforced by the code
    that already owned them, not by a second copy that could drift.
    """
    if type(document) is not dict:
        _fail("The configuration document must be a JSON object.")
    keys = set(document)
    if keys not in (
        set(CONFIG_KEYS),
        set(PRE_GMAIL_CONFIG_KEYS),
        set(PRE_GITHUB_CONFIG_KEYS),
        set(LEGACY_CONFIG_KEYS),
    ):
        # One verdict for missing and unknown alike: a typo silently ignored
        # is a setting the operator believes they made and did not. Exactly
        # two shapes are accepted: the current full v1 key set, or the
        # complete pre-selection v1 key set (see LEGACY_CONFIG_KEYS).
        _fail(
            "The configuration document must contain exactly the current v1 "
            "keys: " + ", ".join(CONFIG_KEYS) + "."
        )
    if document["config_version"] != CONFIG_VERSION or type(
        document["config_version"]
    ) is not int:
        _fail(f"The configuration document must be version {CONFIG_VERSION}.")
    approval_id = _exact_approval_id(document["paid_api_approval_id"])
    paid_enabled = _exact_bool(document["paid_api_enabled"], "paid_api_enabled")
    if paid_enabled and approval_id is None:
        _fail("'paid_api_enabled' requires a 'paid_api_approval_id'.")
    try:
        return Settings(
            memory_database=_exact_database(document["memory_database"]),
            execution_enabled=_exact_bool(
                document["execution_enabled"], "execution_enabled"
            ),
            permission_mode=_exact_mode(document["permission_mode"]),
            paid_api_enabled=paid_enabled,
            paid_api_approval_id=approval_id,
            approval_ttl_minutes=_exact_int(
                document["approval_ttl_minutes"],
                "approval_ttl_minutes",
                MIN_APPROVAL_TTL_MINUTES,
                MAX_APPROVAL_TTL_MINUTES,
            ),
            claude_call_budget_usd=_exact_amount(
                document["claude_call_budget_usd"], "claude_call_budget_usd"
            ),
            # The key-set check above guarantees both are present (current
            # shape) or both absent (legacy shape); absent means unset.
            claude_model=_exact_claude_model(document.get("claude_model")),
            claude_effort=_exact_claude_effort(document.get("claude_effort")),
            # Absent means unset (a pre-GitHub document), which composes to no
            # connector, no grant and no expected identity. A present value is
            # judged by the GitHub grammars through the constructor below.
            github_principal=_exact_github_principal(
                document.get("github_principal")
            ),
            github_repositories=_exact_github_repositories(
                document.get("github_repositories")
            ),
            # Absent means unset (a pre-Gmail document): no connector, no
            # grant and no expected identity.
            gmail_principal=_exact_gmail_principal(document.get("gmail_principal")),
        )
    except ValueError as exc:
        # The existing constructor refused it. Its message is already
        # bounded and value-free.
        raise ConfigError(ErrorCode.CONFIG_INVALID, str(exc)) from exc


def fail_closed_settings() -> Settings:
    """What to run as when the store cannot be trusted.

    Not the ordinary defaults. An operator who had chosen STRICT, or who had
    switched execution off, must not have that quietly relaxed because their
    file got corrupted - so the fallback is the strictest posture the
    settings can express, and it is the same one every time.
    """
    return Settings(
        execution_enabled=False,
        permission_mode=PermissionMode.STRICT,
        paid_api_enabled=False,
    )


class ConfigStore:
    """The one reader and writer of the persistent configuration document.

    Reads are cheap and never create anything. Writes take a same-directory
    cross-process lock, re-read under it, and land through a temp file and an
    atomic replace - so two operators running ``config set`` at once cannot
    interleave into a half-document, and a stale reader cannot restore a
    value the other one just changed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    # -- reading -------------------------------------------------------

    def load(self) -> SettingsLoadResult:
        """The settings to run with, and the store's status. Never raises."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return SettingsLoadResult(Settings(), ConfigStatus.ABSENT)
        except OSError:
            return SettingsLoadResult(
                fail_closed_settings(),
                ConfigStatus.UNREADABLE,
                "The configuration store could not be read.",
            )
        try:
            return SettingsLoadResult(self._settings_from_bytes(raw), ConfigStatus.VALID)
        except ConfigError as exc:
            return SettingsLoadResult(
                fail_closed_settings(), ConfigStatus.INVALID, exc.message
            )

    def _settings_from_bytes(self, raw: bytes) -> Settings:
        if len(raw) > CONFIG_MAX_BYTES:
            _fail("The configuration document is too large.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("The configuration document is not valid UTF-8.")
        try:
            document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except ConfigError:
            raise
        except RecursionError:
            _fail("The configuration document is nested too deeply.")
        except ValueError:
            _fail("The configuration document is not valid JSON.")
        return _settings_from_document(document)

    def read_document(self) -> dict[str, object]:
        """The current document, validated. Raises ConfigError otherwise."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _document_from_settings(Settings())
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration store could not be read.",
            ) from exc
        return _document_from_settings(self._settings_from_bytes(raw))

    # -- writing -------------------------------------------------------

    def write(self, settings: Settings) -> None:
        """Replace the document with the complete canonical form of settings.

        Validated through the SAME grammar ``load`` uses, BEFORE the
        destination is touched. A store whose write path is more permissive
        than its read path can persist a document it will then refuse on the
        next startup - and the operator would have no way to tell that from
        corruption they did not cause.
        """
        document = _document_from_settings(settings)
        # Raises ConfigError, so nothing below runs and the old bytes stand.
        _settings_from_document(document)
        with self._locked():
            self._atomic_write(document)

    def mutate(self, change) -> Settings:
        """Read, validate, derive and write - all under one held lock.

        The re-read inside the lock is the point: without it, two rapid
        ``config set`` calls could each read the old document and the second
        would write back a value the first had already replaced, quietly
        restoring an execution switch the operator had just turned off.
        """
        with self._locked():
            current = _settings_from_document(self._read_document_unlocked())
            try:
                updated = change(current)
            except ConfigError:
                raise
            except ValueError as exc:
                # The frozen Settings refused the change. Its message is
                # already bounded and value-free; it just needs the typed
                # shape the rest of this surface speaks.
                raise ConfigError(ErrorCode.CONFIG_INVALID, str(exc)) from exc
            # Re-validate through the SAME grammar a stored document must
            # pass. Otherwise a value the loader would refuse could be
            # written here and only fail on the next startup - a store that
            # accepts what it cannot later read is worse than one that says
            # no now.
            document = _document_from_settings(updated)
            validated = _settings_from_document(document)
            self._atomic_write(_document_from_settings(validated))
            return validated

    def reset_all(self) -> Settings:
        """THE repair operation: one complete canonical default document.

        Deliberately the only mutation that does not require the existing
        document to be valid - it is what an operator reaches for when it is
        not, and requiring validity would leave a corrupted store with no way
        back except a text editor.
        """
        defaults = Settings()
        with self._locked():
            self._atomic_write(_document_from_settings(defaults))
        return defaults

    def _read_document_unlocked(self) -> dict[str, object]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _document_from_settings(Settings())
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration store could not be read.",
            ) from exc
        if len(raw) > CONFIG_MAX_BYTES:
            _fail("The configuration document is too large.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("The configuration document is not valid UTF-8.")
        try:
            document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except ConfigError:
            raise
        except RecursionError:
            _fail("The configuration document is nested too deeply.")
        except ValueError:
            _fail("The configuration document is not valid JSON.")
        # Validate by round-tripping through Settings, then hand back the
        # canonical document so a caller edits a known shape.
        return _document_from_settings(_settings_from_document(document))

    @contextmanager
    def _locked(self):
        """A same-directory, cross-process advisory lock for the whole write.

        The lock FILE is not the lock - it may be left behind and means
        nothing on its own. The operating system's own lock on the open
        handle is what excludes a second process, which is why an abandoned
        empty file is harmless.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration directory could not be created.",
            ) from exc
        try:
            handle = open(self.lock_path, "a+b")  # noqa: SIM115 - closed in finally
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration lock could not be opened.",
            ) from exc
        try:
            _acquire(handle)
            yield
        finally:
            try:
                _release(handle)
            finally:
                handle.close()

    def _atomic_write(self, document: dict[str, object]) -> None:
        """Whole document or nothing: temp file, fsync, atomic replace."""
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n"
        ).encode("utf-8")
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".config-", suffix=".tmp", dir=str(self.path.parent)
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Closed before the replace: Windows will not rename an open file.
            os.replace(temporary, self.path)
            temporary = None
            self._sync_directory()
        except ConfigError:
            raise
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration document could not be written.",
            ) from exc
        finally:
            if temporary is not None:
                # Only the exact file this write created.
                with contextlib.suppress(OSError):
                    temporary.unlink()

    def _sync_directory(self) -> None:
        """Durably record the rename where the platform supports it."""
        if os.name == "nt":
            return
        try:
            descriptor = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


if os.name == "nt":  # pragma: no cover - platform split
    import msvcrt

    def _acquire(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration store is busy; try again.",
            ) from exc

    def _release(handle) -> None:
        handle.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - platform split
    import fcntl

    def _acquire(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise ConfigError(
                ErrorCode.CONFIG_IO_FAILED,
                "The configuration store is busy; try again.",
            ) from exc

    def _release(handle) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_settings(path: Path | None = None) -> SettingsLoadResult:
    """THE one load point. Reads the store once and returns what to run as."""
    return ConfigStore(path).load()
