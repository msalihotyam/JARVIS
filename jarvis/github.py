"""GitHub v1 - the OFFLINE security and policy foundation (Phase 1).

IMPLEMENTED NOW: the canonical destination grammar - judged on the RAW
spelling, before any lossy normalization can repair one - the closed
capability and resource vocabularies, the Core-owned identity binding a
connector can only be measured against, the canonical request frame future
approvals bind through, the credential/environment removal policy a future
``gh`` child process must satisfy, and the closed per-operation argument
vocabulary as pure data.

NOT IMPLEMENTED: any live GitHub read or write, and no execution seam of any
kind. Nothing in this module opens a socket, resolves a hostname, reads a
credential store, inspects an authenticated session or starts a process, and
there is no function here that runs anything - the process seam is DEFERRED
to Phase 2 rather than shipped switched off, so there is nothing for a later
phase to merely enable. There is deliberately no ``github-cli`` connector in
this build either, so no production path can reach GitHub at all.

Core, not a connector, is the authority. Everything here is a
*canonicalizer* or a *refusal*; none of it authorizes anything. A GitHub
destination becomes reachable only when an exact
``DestinationGrant(connector, capability, destination)`` row exists in
``jarvis.destinations`` - and this build ships none, so the default GitHub
authority is empty.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Mapping

from jarvis.contracts import (
    AttestationError,
    BillingClass,
    DataClassification,
    ErrorCode,
    MonetaryState,
    REPOSITORY_DESTINATION_PREFIX,
    canonical_utc_timestamp,
    credential_bearing,
)

# --------------------------------------------------------------------------
# Identity, closed capability vocabulary and billing
# --------------------------------------------------------------------------

#: The one registered connector name a GitHub grant may ever name. It is an
#: identity, not a permission: naming it authorizes nothing.
GITHUB_CONNECTOR = "github-cli"

#: The only host this product talks to. Enterprise hosts are out of v1, and
#: the environment variables that could silently redirect to one are stripped
#: from every child environment below.
GITHUB_HOST = "github.com"

#: Prefix of the canonical destination spelling ``github/<owner>/<repository>``.
#: Taken from Core's central normalizer rather than spelled again here: the
#: normalizer has to recognize a repository destination to refuse repairing
#: one, and two copies of that prefix could drift apart.
GITHUB_DESTINATION_PREFIX = REPOSITORY_DESTINATION_PREFIX

#: THE closed read vocabulary. Nine identifiers and not one more: there is no
#: ``github_read``, no ``github_api``, no ``rest``, no ``graphql``, no
#: ``command`` and no wildcard. An unknown GitHub capability is not a
#: capability.
GITHUB_READ_CAPABILITIES: tuple[str, ...] = (
    "github_repository_read",
    "github_file_read",
    "github_issue_list",
    "github_issue_read",
    "github_pull_request_list",
    "github_pull_request_read",
    "github_commit_list",
    "github_commit_read",
    "github_pull_request_files",
)

#: DEFERRED PAST usable-v1, and deferred for a reason that is not schedule.
#:
#: Every read here must prove which credential served it, and the only way to
#: prove that is to ask GitHub who the caller is inside the request that
#: returns the data - one request, one credential selection. GitHub's GraphQL
#: schema has no code search: its ``SearchType`` enum names ISSUE,
#: REPOSITORY, USER and DISCUSSION and no ``CODE`` member, so a code search
#: cannot be carried in the same request as the viewer and cannot be bound to
#: a principal. Rather than run it over a second, unprovable transport, it is
#: not a usable-v1 capability at all.
#:
#: The name, its resource kind and its whole grammar stay declared, exactly
#: like the write half: a later phase must not be free to invent them again.
#: Being declared is not being enabled - there is no argument builder for it,
#: so a frame naming it produces no plan.
GITHUB_DEFERRED_READ_CAPABILITIES: tuple[str, ...] = ("github_code_search",)

#: The write half of the same closed vocabulary. Declared because the
#: architecture is closed and a later phase must not be free to invent its
#: own names - NOT because anything here can execute them.
GITHUB_WRITE_CAPABILITIES: tuple[str, ...] = (
    "github_issue_create",
    "github_issue_comment_create",
    "github_pull_request_comment_create",
)

GITHUB_CAPABILITIES: tuple[str, ...] = (
    GITHUB_READ_CAPABILITIES
    + GITHUB_DEFERRED_READ_CAPABILITIES
    + GITHUB_WRITE_CAPABILITIES
)

#: What this build can actually EXECUTE, and separately so: a name existing in
#: the vocabulary above is a spelling, never a permission to run.
#:
#: Phase 2 gives the READ half a body - a process seam, a connector and a
#: derived argument vector each - so those names move here. The write half
#: stays absent and is not merely switched off: it has no argument builder
#: anywhere in this build, so a write frame produces no plan at all.
#:
#: This is still not authority. An enabled capability reaches a repository
#: only when an operator has configured the exact ``DestinationGrant`` row for
#: it AND the build holds the Core-owned identity to act under; an
#: unconfigured installation composes with neither.
GITHUB_ENABLED_CAPABILITIES: tuple[str, ...] = GITHUB_READ_CAPABILITIES

#: A GitHub operation is served by an already-authenticated local CLI over an
#: API with no per-call price. Rate limits are not money and never touch the
#: monetary ledger.
GITHUB_BILLING_CLASS = BillingClass.NO_ADDITIONAL_CHARGE
GITHUB_MONETARY_STATE = MonetaryState.NOT_APPLICABLE

#: THE canonical credential-route identity. A NON-SECRET identifier naming
#: *how* a future call is authenticated, never the credential itself: the
#: GitHub CLI's own authenticated session, backed by the OS credential store.
#: JARVIS never requests, retrieves, persists or presents the raw token.
GITHUB_CREDENTIAL_ROUTE = "github_cli_os_credential_store"

#: The closed set of credential routes. One value; an unknown route fails.
GITHUB_CREDENTIAL_ROUTES = frozenset({GITHUB_CREDENTIAL_ROUTE})


# --------------------------------------------------------------------------
# Canonical destination grammar - pure, lexical, offline
# --------------------------------------------------------------------------
#
# A GitHub identity is NOT a filesystem path and NOT a URL. ``Path.resolve``,
# ``os.path`` normalization and the local-file target helpers are all wrong
# here: they anchor to a working directory, collapse ``..`` and, on Windows,
# can turn naming a resource into an outbound inspection. The grammar below
# is a strict ASCII character test and nothing else.

#: GitHub login rule: ASCII alphanumerics with single interior hyphens.
_OWNER_PATTERN = re.compile(r"\A[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")
OWNER_MAX_CHARS = 39

#: GitHub repository name rule: ASCII alphanumerics, dot, hyphen, underscore.
_REPOSITORY_PATTERN = re.compile(r"\A[A-Za-z0-9._-]+\Z")
REPOSITORY_MAX_CHARS = 100

#: Hard ceiling on a raw requested destination before it is even split. Its
#: OWN policy surface: it bounds a GitHub identity, not a chain locator and
#: not a local read path, and must stay free to diverge from both.
GITHUB_DESTINATION_MAX_CHARS = 160


def _ascii_text_refusal(value: Any, label: str, cap: int) -> str | None:
    """Why this is not bounded printable ASCII, or ``None``.

    One rule for every GitHub scalar: exact ``str`` type (a foreign ``str``
    subclass or enum is not this contract's text), non-empty, within the
    stated cap, and free of control characters and non-ASCII. Whitespace is
    refused for identity fields by the character grammars below; only the
    file path deliberately tolerates an interior space.
    """
    if type(value) is not str:
        return f"the {label} is not a string"
    if not value:
        return f"the {label} is empty"
    if len(value) > cap:
        return f"the {label} is longer than {cap} characters"
    if not value.isascii():
        return f"the {label} is not ASCII"
    if any(char < " " or char == "\x7f" for char in value):
        return f"the {label} contains a control character"
    return None


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    """One canonical repository identity.

    ``owner`` and ``repository`` are already lowercase: the canonical
    destination is the identity Core decides, persists, fingerprints and
    attests with, so it must have exactly one spelling.
    """

    owner: str
    repository: str

    @property
    def destination(self) -> str:
        return f"{GITHUB_DESTINATION_PREFIX}{self.owner}/{self.repository}"


def github_destination_refusal(requested: Any) -> str | None:
    """Why ``requested`` is not a GitHub repository destination, or ``None``.

    Refuses by construction rather than by a list of special cases: URLs and
    any scheme or hostname form (``:``, ``//``), API and GraphQL paths and any
    extra segment (segment count), query strings and fragments (``?``/``#``),
    userinfo (``@``), percent-encoded ambiguity (``%``), backslashes, shell
    and control syntax, traversal (``.``/``..`` are not owner or repository
    names), whitespace, control characters, non-ASCII, empty segments and a
    ``.git`` suffix. None of those characters is in either grammar.
    """
    reason = _ascii_text_refusal(
        requested, "destination", GITHUB_DESTINATION_MAX_CHARS
    )
    if reason is not None:
        return reason
    parts = requested.split("/")
    if len(parts) != 3:
        return "the destination is not exactly github/<owner>/<repository>"
    prefix, owner, repository = parts
    if prefix != "github":
        return "the destination does not name the github namespace"
    if not owner or not repository:
        return "the destination has an empty segment"
    if len(owner) > OWNER_MAX_CHARS or not _OWNER_PATTERN.fullmatch(owner):
        return "the owner is not a GitHub login"
    if len(repository) > REPOSITORY_MAX_CHARS or not _REPOSITORY_PATTERN.fullmatch(
        repository
    ):
        return "the repository is not a GitHub repository name"
    if repository in (".", ".."):
        return "the repository is a traversal form"
    if repository.lower().endswith(".git"):
        return "the repository carries a .git suffix"
    return None


def github_destination(requested: Any) -> GitHubRepository | None:
    """The canonical identity of an untrusted requested destination.

    ``None`` for anything the grammar refuses. Case is folded to lowercase
    for the IDENTITY only - deterministically and with ``str.lower`` on
    already-validated ASCII, never ``casefold`` on arbitrary Unicode.
    Canonicalizing a name is never an inspection of the thing it names: no
    filesystem call, no network call, no authentication.
    """
    if github_destination_refusal(requested) is not None:
        return None
    _, owner, repository = requested.split("/")
    return GitHubRepository(owner=owner.lower(), repository=repository.lower())


def is_github_destination(canonical: Any) -> bool:
    """Whether a CANONICAL destination string is a well-formed repository.

    VALIDITY ONLY. ``jarvis.destinations`` asks this to decide whether the
    string names something Core can talk about at all; whether anyone may
    reach it is the separate, exact-tuple question no grammar can answer.
    """
    identity = github_destination(canonical)
    return identity is not None and identity.destination == canonical


def github_raw_destination_refusal(requested: Any) -> str | None:
    """Why this RAW, PRE-NORMALIZATION spelling is not a repository, or ``None``.

    THE laundering boundary. Core's generic ``normalize_destination`` strips
    surrounding whitespace, case-folds and applies Unicode case folding - all
    three of which are LOSSY, and each of which can turn a spelling this
    grammar refuses into one it accepts. ``" github/acme/widgets "``,
    ``github/acme/widKets`` and ``github/acme/widKets`` (Kelvin sign)
    all normalize to a perfectly canonical-looking repository destination
    that the raw request never actually named.

    So the raw string is judged HERE, before any of that happens: it must be
    exact ``str``, ASCII, already lowercase, carry no whitespace anywhere,
    and be its own canonical form under the grammar above. A spelling that
    needs repair is refused rather than repaired - repairing it would mean
    the destination the requester wrote and the destination Core authorizes,
    persists and attests are two different strings.

    Refusal is not the same question as authorization: a raw spelling that
    passes here has proven only that it NAMES a repository. Reaching it still
    requires the exact Core-owned (connector, capability, destination) tuple,
    and this build ships none.
    """
    if type(requested) is not str:
        return "the destination is not a string"
    if not requested.isascii():
        return "the destination is not ASCII"
    if any(character.isspace() for character in requested):
        return "the destination carries whitespace"
    if requested != requested.lower():
        return "the destination is not already lowercase"
    reason = github_destination_refusal(requested)
    if reason is not None:
        return reason
    if not is_github_destination(requested):
        return "the destination is not in canonical form"
    return None


# --------------------------------------------------------------------------
# Closed resource-target model
# --------------------------------------------------------------------------
#
# A resource target is TYPED FIELDS and nothing else. A free-form GitHub URL,
# a REST or GraphQL endpoint, a shell fragment, a ``gh`` argv or an arbitrary
# query object has no spelling here, so none of them can become a target.


class GitHubResourceKind(str, Enum):
    """The closed set of things a GitHub task can name."""

    REPOSITORY = "repository"
    FILE = "file"
    ISSUE_LIST = "issue_list"
    ISSUE = "issue"
    PULL_REQUEST_LIST = "pull_request_list"
    PULL_REQUEST = "pull_request"
    COMMIT_LIST = "commit_list"
    COMMIT = "commit"
    PULL_REQUEST_FILES = "pull_request_files"
    CODE_SEARCH = "code_search"
    NEW_ISSUE = "new_issue"
    NEW_ISSUE_COMMENT = "new_issue_comment"
    NEW_PULL_REQUEST_COMMENT = "new_pull_request_comment"


#: Capability -> the ONE resource kind it may name. An issue capability can
#: never name a pull request and vice versa: the two kinds are distinct
#: values with distinct rows, so a swap is a different frame and a different
#: binding rather than a quietly equivalent request.
GITHUB_CAPABILITY_RESOURCE: Mapping[str, GitHubResourceKind] = {
    "github_repository_read": GitHubResourceKind.REPOSITORY,
    "github_file_read": GitHubResourceKind.FILE,
    "github_issue_list": GitHubResourceKind.ISSUE_LIST,
    "github_issue_read": GitHubResourceKind.ISSUE,
    "github_pull_request_list": GitHubResourceKind.PULL_REQUEST_LIST,
    "github_pull_request_read": GitHubResourceKind.PULL_REQUEST,
    "github_commit_list": GitHubResourceKind.COMMIT_LIST,
    "github_commit_read": GitHubResourceKind.COMMIT,
    "github_pull_request_files": GitHubResourceKind.PULL_REQUEST_FILES,
    "github_code_search": GitHubResourceKind.CODE_SEARCH,
    "github_issue_create": GitHubResourceKind.NEW_ISSUE,
    "github_issue_comment_create": GitHubResourceKind.NEW_ISSUE_COMMENT,
    "github_pull_request_comment_create": (
        GitHubResourceKind.NEW_PULL_REQUEST_COMMENT
    ),
}

#: The closed list filter. Not free text and not a query object.
GITHUB_LIST_STATES: tuple[str, ...] = ("open", "closed", "all")

#: The deterministic list bound. A listing is a bounded read, never an export.
GITHUB_LIST_MAX_ITEMS = 50

#: The SEPARATE, tighter bound on a code search. Its own policy surface on
#: purpose: a listing names one repository's own issues or pull requests,
#: while a search reaches across a repository's whole content, and the two
#: must stay free to diverge. A search is a lookup, never a crawl.
GITHUB_SEARCH_MAX_ITEMS = 10

#: Hard ceiling on the one bounded search query field.
GITHUB_QUERY_MAX_CHARS = 128

#: Hard ceiling on a repository-relative file path. Its OWN policy surface:
#: it bounds a path INSIDE a repository, not a local filesystem target, so it
#: must stay free to diverge from ``LOCAL_READ_PATH_MAX_CHARS``.
GITHUB_PATH_MAX_CHARS = 512

#: Hard ceiling on a git ref. Matches git's own refname ceiling.
GITHUB_REF_MAX_CHARS = 255

#: Largest issue/pull-request number that can be named. Numbers are positive
#: and canonical; ``0``, negatives, floats, bools and numeric strings are not
#: issue numbers.
GITHUB_NUMBER_MAX = 1_000_000_000

_REF_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

#: The bounded literal search term: words and the characters real identifiers
#: carry, separated by single spaces. No ``:`` (qualifiers), no quotes, no
#: boolean operators, no wildcards, no path syntax.
_QUERY_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*\Z")

#: The optional fields, and which kinds REQUIRE each of them. A field a kind
#: does not require must be absent: the model is closed in both directions,
#: so a repository target cannot quietly carry an issue number.
_RESOURCE_FIELDS: Mapping[GitHubResourceKind, frozenset[str]] = {
    GitHubResourceKind.REPOSITORY: frozenset(),
    GitHubResourceKind.FILE: frozenset({"ref", "path"}),
    GitHubResourceKind.ISSUE_LIST: frozenset({"state", "limit"}),
    GitHubResourceKind.ISSUE: frozenset({"number"}),
    GitHubResourceKind.PULL_REQUEST_LIST: frozenset({"state", "limit"}),
    GitHubResourceKind.PULL_REQUEST: frozenset({"number"}),
    # A commit listing is bounded by count alone; naming a ref would make it
    # a second, differently-shaped history query, which usable-v1 does not
    # need. A commit READ names the exact sha (or ref) it is about.
    GitHubResourceKind.COMMIT_LIST: frozenset({"limit"}),
    GitHubResourceKind.COMMIT: frozenset({"ref"}),
    GitHubResourceKind.PULL_REQUEST_FILES: frozenset({"number", "limit"}),
    GitHubResourceKind.CODE_SEARCH: frozenset({"query", "limit"}),
    GitHubResourceKind.NEW_ISSUE: frozenset(),
    GitHubResourceKind.NEW_ISSUE_COMMENT: frozenset({"number"}),
    GitHubResourceKind.NEW_PULL_REQUEST_COMMENT: frozenset({"number"}),
}

#: The empty value of every optional field, so "absent" has one spelling.
_RESOURCE_ABSENT: Mapping[str, Any] = {
    "number": 0,
    "ref": "",
    "path": "",
    "state": "",
    "limit": 0,
    "query": "",
}


def github_number_refusal(value: Any, label: str = "number") -> str | None:
    """Why this is not a canonical positive GitHub number, or ``None``.

    ``type() is not int`` rather than ``isinstance``: ``True`` is an ``int``
    in Python and ``True == 1``, so a boolean would otherwise become issue 1.
    """
    if type(value) is not int:
        return f"the {label} is not an integer"
    if value < 1:
        return f"the {label} is not positive"
    if value > GITHUB_NUMBER_MAX:
        return f"the {label} is out of range"
    return None


def github_path_refusal(value: Any) -> str | None:
    """Why this is not a repository-relative GitHub file path, or ``None``.

    Case is PRESERVED: a repository path is content identity, not an identity
    Core folds, and no filesystem resolution happens here at all. Refuses
    absolute forms, backslashes, empty segments, ``.``/``..`` traversal,
    percent-encoded ambiguity, query/fragment syntax, leading or trailing
    whitespace and control characters. An interior space survives - real
    repository files have them.
    """
    reason = _ascii_text_refusal(value, "path", GITHUB_PATH_MAX_CHARS)
    if reason is not None:
        return reason
    if value != value.strip():
        return "the path has leading or trailing whitespace"
    if any(char in value for char in "\\%?#:*\"<>|"):
        return "the path contains an ambiguous or reserved character"
    if value.startswith("/") or value.endswith("/"):
        return "the path is not repository-relative"
    segments = value.split("/")
    if any(not segment for segment in segments):
        return "the path has an empty segment"
    if any(segment in (".", "..") for segment in segments):
        return "the path is a traversal form"
    return None


def github_ref_refusal(value: Any) -> str | None:
    """Why this is not a usable GitHub ref, or ``None``.

    A branch, tag or commit sha as an explicitly typed string - the subset of
    ``git check-ref-format`` that matters here, applied lexically.
    """
    reason = _ascii_text_refusal(value, "ref", GITHUB_REF_MAX_CHARS)
    if reason is not None:
        return reason
    if not _REF_PATTERN.fullmatch(value):
        return "the ref contains a character a git ref may not carry"
    if ".." in value or "//" in value or "@{" in value:
        return "the ref contains a reserved sequence"
    if value.endswith((".", "/", ".lock")):
        return "the ref ends with a reserved sequence"
    return None


def _state_refusal(value: Any) -> str | None:
    if type(value) is not str or value not in GITHUB_LIST_STATES:
        return "the list state is not a closed filter value"
    return None


def _limit_refusal(value: Any) -> str | None:
    if type(value) is not int:
        return "the list bound is not an integer"
    if not 1 <= value <= GITHUB_LIST_MAX_ITEMS:
        return "the list bound is outside the deterministic range"
    return None


def github_query_refusal(value: Any) -> str | None:
    """Why this is not the ONE bounded code-search term, or ``None``.

    A LITERAL TERM, never a query language. GitHub's search syntax is
    deliberately unavailable here: ``:`` would let a caller write
    ``repo:other/repo`` or ``org:...`` and reach outside the repository the
    destination grant authorizes, and the boolean/qualifier operators would
    turn one bounded lookup into a crawl. So the grammar is plain words -
    letters, digits, and a small set of characters real identifiers contain -
    with no leading dash, which is also what keeps a term from being read as
    a flag by the CLI that receives it.
    """
    reason = _ascii_text_refusal(value, "search query", GITHUB_QUERY_MAX_CHARS)
    if reason is not None:
        return reason
    if value != value.strip():
        return "the search query has leading or trailing whitespace"
    if value.startswith("-"):
        return "the search query starts like a command-line flag"
    if not _QUERY_PATTERN.fullmatch(value):
        return "the search query is not a bounded literal term"
    return None


def _search_limit_refusal(value: Any) -> str | None:
    if type(value) is not int:
        return "the search bound is not an integer"
    if not 1 <= value <= GITHUB_SEARCH_MAX_ITEMS:
        return "the search bound is outside the deterministic range"
    return None


_RESOURCE_FIELD_REFUSAL = {
    "number": github_number_refusal,
    "ref": github_ref_refusal,
    "path": github_path_refusal,
    "state": _state_refusal,
    "limit": _limit_refusal,
    "query": github_query_refusal,
}

#: The one kind whose bound is the tighter search bound rather than the list
#: bound. Stated as data so the refusal table stays a table.
_SEARCH_LIMIT_KINDS = frozenset({GitHubResourceKind.CODE_SEARCH})


@dataclass(frozen=True, slots=True)
class GitHubResource:
    """One typed GitHub resource target. Never a URL, endpoint or argv."""

    kind: GitHubResourceKind
    owner: str
    repository: str
    number: int = 0
    ref: str = ""
    path: str = ""
    state: str = ""
    limit: int = 0
    #: The ONE bounded literal search term, and only a code search may carry
    #: it. It is a term, never a query language - see ``github_query_refusal``.
    query: str = ""

    @property
    def destination(self) -> str:
        return f"{GITHUB_DESTINATION_PREFIX}{self.owner}/{self.repository}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "owner": self.owner,
            "repository": self.repository,
            "number": self.number,
            "ref": self.ref,
            "path": self.path,
            "state": self.state,
            "limit": self.limit,
            "query": self.query,
        }

    @property
    def identity(self) -> str:
        """One unambiguous, bounded canonical spelling of this target."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def github_resource_refusal(resource: Any) -> str | None:
    """Why this is not a valid typed GitHub resource target, or ``None``."""
    if type(resource) is not GitHubResource:
        return "the resource is not a GitHubResource"
    if type(resource.kind) is not GitHubResourceKind:
        return "the resource kind is not a recognized kind"
    identity = github_destination(resource.destination)
    if identity is None:
        return "the resource does not name a canonical repository"
    if (identity.owner, identity.repository) != (
        resource.owner,
        resource.repository,
    ):
        return "the resource repository is not in canonical form"
    required = _RESOURCE_FIELDS[resource.kind]
    for field_name, absent in _RESOURCE_ABSENT.items():
        value = getattr(resource, field_name)
        if field_name not in required:
            if type(value) is not type(absent) or value != absent:
                return (
                    f"the {resource.kind.value} target may not carry a {field_name}"
                )
            continue
        # A code search is bounded by its own tighter ceiling. Chosen by the
        # KIND rather than by the field, so a listing can never borrow the
        # search bound or the other way round.
        if field_name == "limit" and resource.kind in _SEARCH_LIMIT_KINDS:
            reason = _search_limit_refusal(value)
        else:
            reason = _RESOURCE_FIELD_REFUSAL[field_name](value)
        if reason is not None:
            return reason
    return None


#: The exact keys a serialized resource carries. Default-deny in both
#: directions: a missing key and an unknown key are the same failure.
_RESOURCE_KEYS = frozenset(
    {"kind", "owner", "repository"} | set(_RESOURCE_ABSENT)
)


def github_resource_from_dict(data: Any) -> GitHubResource:
    """Rebuild one typed resource from its serialized form, or fail closed.

    THE only way a stored or decoded resource becomes a typed one. Exact
    keys, exact types, no coercion, no defaults filled in for a missing key
    and no partial acceptance - and the rebuilt object is then judged by the
    same ``github_resource_refusal`` a freshly built one is, so a row that
    was valid under an older vocabulary cannot re-enter as a resource this
    build would refuse to construct.

    Raises ``GitHubExecutionUnavailable``: a resource that cannot be rebuilt
    truthfully must not exist in a half-built form that later looks whole.
    """
    if not isinstance(data, dict):
        raise GitHubExecutionUnavailable("the resource is not an object")
    if set(data) != _RESOURCE_KEYS:
        raise GitHubExecutionUnavailable(
            "the resource carries a missing or unsupported key"
        )
    raw_kind = data["kind"]
    if type(raw_kind) is not str:
        raise GitHubExecutionUnavailable("the resource kind is not a string")
    try:
        kind = GitHubResourceKind(raw_kind)
    except ValueError:
        raise GitHubExecutionUnavailable(
            f"unknown GitHub resource kind {raw_kind!r}"
        ) from None
    for field_name, absent in _RESOURCE_ABSENT.items():
        if type(data[field_name]) is not type(absent):
            raise GitHubExecutionUnavailable(
                f"the resource {field_name} is not the type this contract holds"
            )
    resource = GitHubResource(
        kind=kind,
        owner=data["owner"],
        repository=data["repository"],
        number=data["number"],
        ref=data["ref"],
        path=data["path"],
        state=data["state"],
        limit=data["limit"],
        query=data["query"],
    )
    reason = github_resource_refusal(resource)
    if reason is not None:
        raise GitHubExecutionUnavailable(reason)
    return resource


def github_resource_binding_refusal(
    resource: Any, *, capability: Any, destination: Any
) -> str | None:
    """Why this resource may not be executed under this task, or ``None``.

    THE invariant. The resource's own owner/repository must canonicalize to
    and EXACTLY equal the task's canonical destination, and the capability
    must be the one that names this resource kind. Repository A can never
    ride along inside a task approved for repository B, and an issue
    capability can never carry a pull-request target.
    """
    reason = github_resource_refusal(resource)
    if reason is not None:
        return reason
    if type(capability) is not str or capability not in GITHUB_CAPABILITIES:
        return "the capability is not in the closed GitHub vocabulary"
    if GITHUB_CAPABILITY_RESOURCE[capability] is not resource.kind:
        return "the capability does not name this resource kind"
    if type(destination) is not str or not is_github_destination(destination):
        return "the task destination is not a canonical GitHub repository"
    if resource.destination != destination:
        return "the resource repository is not the task destination"
    return None


# --------------------------------------------------------------------------
# Principal and credential route
# --------------------------------------------------------------------------


def github_principal_refusal(value: Any) -> str | None:
    """Why this is not a GitHub login, or ``None``.

    A NON-SECRET identifier. This is grammar only: it says a string COULD be
    a login, never that it is the one Core expects. That second question is
    ``github_identity_refusal``'s, and only a Core-owned binding answers it.
    """
    reason = _ascii_text_refusal(value, "principal", OWNER_MAX_CHARS)
    if reason is not None:
        return reason
    if not _OWNER_PATTERN.fullmatch(value):
        return "the principal is not a GitHub login"
    return None


def github_credential_route_refusal(value: Any) -> str | None:
    """Why this is not the canonical credential route, or ``None``."""
    if type(value) is not str or value not in GITHUB_CREDENTIAL_ROUTES:
        return "the credential route is not a recognized route"
    return None


@dataclass(frozen=True, slots=True)
class GitHubIdentityBinding:
    """The identity Core EXPECTS a future GitHub call to act under.

    CORE-OWNED, NON-SECRET, and established by a source change or an operator
    - exactly like ``DestinationGrant``. It holds two identifiers and no
    credential: the login the call must act as, and the NAME of the route it
    must be authenticated through.

    The whole point of the type is where its values may NOT come from. A
    connector's return value, a connector manifest, ``can_handle``, a model
    answer, task text, retrieved memory, GitHub content and the process
    environment are all UNTRUSTED, and none of them can construct or reach
    this object: Core hands it in, the same way it hands in
    ``destination_policy``. A connector may later OBSERVE which account a
    local CLI session belongs to, but observing is not deciding - the
    observation is compared against this, never substituted for it.
    """

    principal: str
    credential_route: str = GITHUB_CREDENTIAL_ROUTE


#: THE shipped binding, and it is deliberately ``None``. This build knows no
#: GitHub account: inventing a plausible login here would be Core asserting an
#: identity nobody established, which is the same self-attestation failure
#: from the other direction. Composition already has the seam it needs - the
#: binding is a parameter - so a later phase supplies a real one under human
#: review rather than by widening anything at runtime.
GITHUB_IDENTITY_BINDING: "GitHubIdentityBinding | None" = None


def github_identity_binding_refusal(binding: Any) -> str | None:
    """Why this is not a usable Core-owned identity binding, or ``None``."""
    if type(binding) is not GitHubIdentityBinding:
        return "no Core-owned GitHub identity binding was supplied"
    reason = github_principal_refusal(binding.principal)
    if reason is not None:
        return reason
    return github_credential_route_refusal(binding.credential_route)


def github_identity_refusal(
    binding: Any, *, observed_principal: Any, observed_credential_route: Any
) -> str | None:
    """Why the OBSERVED identity is not the one Core expects, or ``None``.

    The asymmetry is the security property. ``binding`` is Core's; the two
    observed values are whatever a connector reports about the local CLI
    session, and they are compared - by exact equality, on already-validated
    ASCII - against Core's. A connector that reports the wrong account, or
    the right account reached through a token in an environment variable
    rather than the OS credential store, is refused. It can never make Core
    expect something else, because it has no way to write into ``binding``.
    """
    reason = github_identity_binding_refusal(binding)
    if reason is not None:
        return reason
    reason = github_principal_refusal(observed_principal)
    if reason is not None:
        return f"the observed {reason.removeprefix('the ')}"
    reason = github_credential_route_refusal(observed_credential_route)
    if reason is not None:
        return f"the observed {reason.removeprefix('the ')}"
    if observed_principal.lower() != binding.principal.lower():
        return "the observed principal is not the principal Core expects"
    if observed_credential_route != binding.credential_route:
        return "the observed credential route is not the route Core expects"
    return None


# --------------------------------------------------------------------------
# H1 - the canonical request frame
# --------------------------------------------------------------------------
#
# The frame is the bytes an approval binds. It is what a future connector's
# ``canonical_outbound`` returns, so its digest becomes
# ``EgressAttestation.outbound_digest`` and therefore
# ``PermissionRequest.fingerprint``: the existing H1 machinery is EXTENDED,
# never duplicated. Change the repository, the resource kind, the issue
# number, the ref, one character of the file path, the expected principal,
# the credential route or one byte of the request body and the approval is no
# longer valid for what is about to happen.

#: Version tag of the frame. Bumping it invalidates every outstanding GitHub
#: approval, which is the point: a changed frame is a changed authority.
GITHUB_REQUEST_FRAME_HEADER = b"jarvis.github_request.v1"

#: Digest of no request body at all. A read binds this; only a write carries
#: real bytes, and an empty digest can never masquerade as one that does.
GITHUB_EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

#: The operation label every READ frame carries. One value, here rather than
#: in a connector: the label is part of what the approval digest binds, so the
#: execution boundary must know it without importing an implementation.
GITHUB_READ_OPERATION = "read"

#: Hard ceiling on the operation label. Its own bounded policy surface.
GITHUB_OPERATION_MAX_CHARS = 64
_OPERATION_PATTERN = re.compile(r"\A[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class GitHubRequestFrame:
    """Everything one GitHub approval authorizes, and nothing else."""

    connector: str
    capability: str
    destination: str
    resource: GitHubResource
    principal: str
    credential_route: str
    operation: str
    body_sha256: str = GITHUB_EMPTY_BODY_SHA256

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "capability": self.capability,
            "destination": self.destination,
            "resource": self.resource.to_dict(),
            "principal": self.principal,
            "credential_route": self.credential_route,
            "operation": self.operation,
            "body_sha256": self.body_sha256,
        }

    def canonical_bytes(self) -> bytes:
        """The exact bytes a connector attests.

        ``ensure_ascii=True`` guarantees the encoding carries no literal
        newline, so "the frame is everything after the header line" stays
        unambiguous - the same technique the local-write frame uses.
        """
        body = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return GITHUB_REQUEST_FRAME_HEADER + b"\n" + body

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _sha256_hex_refusal(value: Any, label: str) -> str | None:
    if type(value) is not str or len(value) != 64:
        return f"the {label} is not a sha256 digest"
    if any(char not in "0123456789abcdef" for char in value):
        return f"the {label} is not a lowercase hex digest"
    return None


def build_github_request_frame(
    *,
    capability: str,
    destination: str,
    resource: GitHubResource,
    binding: "GitHubIdentityBinding | None",
    observed_principal: Any,
    observed_credential_route: Any,
    operation: str,
    body: bytes = b"",
    connector: str = GITHUB_CONNECTOR,
) -> GitHubRequestFrame:
    """Validate every part and return the frame, or fail closed.

    Raises ``AttestationError`` rather than returning a half-valid frame: an
    unattestable request must never reach an approval screen looking whole.

    The identity in the returned frame is ``binding``'s - Core's - and never
    the observed pair. The connector's observation is an INPUT to a
    comparison, not a source of the value: it can fail the check, and that is
    the only influence it has. So the digest an approval binds is over the
    identity Core established, and a connector cannot help define the
    identity it is supposed to be proving.
    """
    reasons = (
        github_resource_binding_refusal(
            resource, capability=capability, destination=destination
        ),
        github_identity_refusal(
            binding,
            observed_principal=observed_principal,
            observed_credential_route=observed_credential_route,
        ),
        None if connector == GITHUB_CONNECTOR else "the connector is not github-cli",
        None
        if type(operation) is str
        and len(operation) <= GITHUB_OPERATION_MAX_CHARS
        and _OPERATION_PATTERN.fullmatch(operation)
        else "the operation is not a canonical identifier",
        None if type(body) is bytes else "the request body is not bytes",
    )
    for reason in reasons:
        if reason is not None:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return GitHubRequestFrame(
        connector=connector,
        capability=capability,
        destination=destination,
        resource=resource,
        principal=binding.principal.lower(),
        credential_route=binding.credential_route,
        operation=operation,
        body_sha256=hashlib.sha256(body).hexdigest(),
    )


def github_issue_body(title: str, body: str) -> bytes:
    """The exact bytes an issue-creation approval binds.

    Title and body are separate JSON fields rather than concatenated text, so
    moving a character across the boundary changes the digest instead of
    producing the same bytes under a different meaning.
    """
    return json.dumps(
        {"title": title, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def github_comment_body(body: str) -> bytes:
    """The exact bytes a comment-creation approval binds."""
    return json.dumps(
        {"body": body}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


# --------------------------------------------------------------------------
# Environment and credential hygiene
# --------------------------------------------------------------------------
#
# Everything a future ``gh`` child could be silently redirected by is removed
# from its environment: credentials, host, repository, config location,
# proxies and interactive editor/browser/pager hand-offs. Prefix families are
# stripped WHOLE rather than enumerated, so a variable gh gains later is
# already covered instead of waiting for someone to notice it.

#: Variable name families removed entirely. ``GH_``/``GITHUB_`` are gh's own
#: configuration surface; ``GIT_`` is the one gh shells out through.
GITHUB_REMOVED_ENVIRONMENT_PREFIXES: tuple[str, ...] = ("GH_", "GITHUB_", "GIT_")

#: Individually named variables outside those families. Proxies in particular
#: redirect the connection itself, which is a destination override by another
#: name; ``XDG_CONFIG_HOME`` relocates gh's whole config directory.
GITHUB_REMOVED_ENVIRONMENT: frozenset[str] = frozenset(
    {
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "EDITOR",
        "VISUAL",
        "BROWSER",
        "PAGER",
        "LESS",
        "LESSOPEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
    }
)

#: Values forced into every child environment. Applied AFTER the removals, so
#: a caller cannot pre-set them and the prefix strip cannot take them away.
GITHUB_FORCED_ENVIRONMENT: Mapping[str, str] = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_PAGER": "cat",
    "NO_COLOR": "1",
}

#: The explicit host argument every future invocation carries, so the host is
#: stated in argv rather than inherited from anything.
GITHUB_HOSTNAME_ARGS: tuple[str, ...] = ("--hostname", GITHUB_HOST)


def sanitized_github_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The removal policy a future ``gh`` child environment must satisfy.

    NOT WIRED INTO ANYTHING, because there is nothing to wire it into: this
    build has no execution seam at all. Deciding that a real environment is
    safe to hand a child - ``APPDATA``/``HOME``/``USERPROFILE`` included,
    which is how the OS credential store is found - is a Phase-2 question
    this phase has not answered, and calling this function answers only the
    removal half of it. It exists so that half is reviewable and testable
    now; it is a policy statement, never a step towards running something.

    Pure and offline: it reads the mapping it is handed and touches no
    process, no credential store and no filesystem. Names are compared
    upper-cased because Windows environment variables are case-insensitive.
    """
    child = {
        str(name): str(value)
        for name, value in environ.items()
        if str(name).upper() not in GITHUB_REMOVED_ENVIRONMENT
        and not str(name).upper().startswith(GITHUB_REMOVED_ENVIRONMENT_PREFIXES)
    }
    child.update(GITHUB_FORCED_ENVIRONMENT)
    return child


# --------------------------------------------------------------------------
# The closed operation plan - DATA, and no execution seam at all
# --------------------------------------------------------------------------
#
# There is no ``run_gh`` in this build, and no generic argv/env/stdin
# execution foundation for a later phase to merely switch on. The real
# process seam is DEFERRED to Phase 2, where it can be designed together with
# Core authorization, credential observation and principal binding - the
# three things that decide whether a call may happen at all.
#
# What remains is a plan: the closed, deterministic argument vector one
# validated operation WOULD use. It is reference data with no authority, and
# it is deliberately not protected by where it came from. Python dataclasses
# are constructible by anyone, so provenance - "this object came out of the
# builder, therefore it is safe" - is not a security boundary and is not used
# as one here. Instead the plan is checked by RE-DERIVATION: whenever it is
# interpreted, its arguments must be exactly what this module's closed
# builder produces for its own validated typed resource. A hand-built plan
# carrying an arbitrary host, an arbitrary subcommand or a token-shaped
# argument is refused because it does not equal that derivation - not because
# of who made it.
#
# Even a plan that passes every check authorizes nothing. Nothing in this
# build reads one in order to do anything.

#: Bound on one derived argument, and on how many there may be.
GH_ARGUMENT_MAX_CHARS = 1_024
GH_MAX_ARGUMENTS = 32


class GitHubExecutionUnavailable(RuntimeError):
    """Raised because live GitHub execution does not exist in this build."""


#: THE transport, and now the only one. Every read is ONE ``gh api graphql``
#: request, so a capability chooses a QUERY and can no longer choose a route:
#: there is no second subcommand left for a vector to reach, and no REST path
#: for one to be spelled onto.
#:
#: The reason is not tidiness. A separate identity call and a separate read
#: are two credential selections, and an account that moves between them - or
#: away and back across them - cannot be seen by any number of before/after
#: checks. One request is one credential selection, so asking GitHub who the
#: caller is IN the request that returns the data is the only way the answer
#: describes the credential that actually served it.
GITHUB_GRAPHQL_ARGS: tuple[str, ...] = ("api", *GITHUB_HOSTNAME_ARGS, "graphql")

#: The selection every derived query opens with. Not optional, not a flag and
#: not per-capability: it is prepended by the one builder below, so a query
#: that does not carry it cannot be derived at all.
GITHUB_VIEWER_SELECTION = "viewer{login}"


def _literal(value: str) -> str:
    """One already-validated scalar as a GraphQL string literal.

    Every value that reaches this has passed a grammar that excludes the
    quote and the backslash outright, so no escaping is needed - and if that
    ever stops being true this REFUSES instead of escaping, because a value
    that needs escaping is a value whose grammar moved underneath the query.
    """
    if '"' in value or "\\" in value:
        raise GitHubExecutionUnavailable(
            "a resource value cannot be spelled into a GraphQL query"
        )
    return f'"{value}"'


def _states(mapping: Mapping[str, str], state: str) -> str:
    """The state filter fragment for one closed filter value."""
    selected = mapping[state]
    return f"states:{selected}," if selected else ""


#: The closed list filter translated into each schema's own enum, once. REST's
#: ``closed`` folded merged pull requests in, so the pull request row names
#: both: moving the transport must not quietly change what a filter MEANS.
#: ``all`` is the absence of a filter rather than a third enum member.
_ISSUE_STATES: Mapping[str, str] = {
    "open": "[OPEN]",
    "closed": "[CLOSED]",
    "all": "",
}
_PULL_REQUEST_STATES: Mapping[str, str] = {
    "open": "[OPEN]",
    "closed": "[CLOSED,MERGED]",
    "all": "",
}

#: Capability -> the ONE selection it may ask for inside ``repository``. This
#: is the whole per-capability surface now: no subcommand, no flags, no path,
#: no host and no method, because the transport above owns all of those. A
#: capability with no row here has no query and therefore no plan.
_GRAPHQL_SELECTIONS: Mapping[str, Any] = {
    "github_repository_read": lambda r: (
        "name owner{login} description defaultBranchRef{name} isPrivate "
        "updatedAt"
    ),
    # ``ref:path`` is GitHub's own expression grammar. Both halves already
    # passed grammars that exclude the quote, the backslash and - for the
    # path - the colon, so neither can end the literal or add a second
    # expression.
    "github_file_read": lambda r: (
        f"object(expression:{_literal(f'{r.ref}:{r.path}')})"
        "{... on Blob{text byteSize isTruncated}}"
    ),
    "github_issue_list": lambda r: (
        f"issues(first:{r.limit},{_states(_ISSUE_STATES, r.state)}"
        "orderBy:{field:UPDATED_AT,direction:DESC})"
        "{nodes{number title state updatedAt}}"
    ),
    "github_issue_read": lambda r: (
        f"issue(number:{r.number}){{number title state body updatedAt}}"
    ),
    "github_pull_request_list": lambda r: (
        f"pullRequests(first:{r.limit},"
        f"{_states(_PULL_REQUEST_STATES, r.state)}"
        "orderBy:{field:UPDATED_AT,direction:DESC})"
        "{nodes{number title state updatedAt}}"
    ),
    "github_pull_request_read": lambda r: (
        f"pullRequest(number:{r.number}){{number title state body updatedAt}}"
    ),
    "github_commit_list": lambda r: (
        "defaultBranchRef{target{... on Commit{"
        f"history(first:{r.limit})"
        "{nodes{oid messageHeadline committedDate author{name}}}}}}"
    ),
    "github_commit_read": lambda r: (
        f"object(oid:{_literal(r.ref)})"
        "{... on Commit{oid messageHeadline message committedDate "
        "author{name}}}"
    ),
    # The changed-file list, without the per-file patch the REST route
    # carried: GraphQL does not expose one. The bounded read this capability
    # is for - which files moved and by how much - is unchanged, and the
    # patch was being cut by ``EXTERNAL_READ_MAX_CHARS`` in any case.
    "github_pull_request_files": lambda r: (
        f"pullRequest(number:{r.number})"
        f"{{files(first:{r.limit})"
        "{nodes{path additions deletions changeType}}}"
    ),
}


def _read_query(capability: str, resource: GitHubResource) -> str:
    """The ONE query for one validated resource, viewer selection included.

    The viewer and the data are selected in the same query on purpose: that
    is the whole binding. There is no branch here that can build a query
    without ``viewer{login}``, so there is no derived read whose result
    cannot be checked against the principal Core bound.
    """
    return (
        "{"
        + GITHUB_VIEWER_SELECTION
        + " repository(owner:"
        + _literal(resource.owner)
        + ",name:"
        + _literal(resource.repository)
        + "){"
        + _GRAPHQL_SELECTIONS[capability](resource)
        + "}}"
    )


#: Capability -> the ONE argv builder that serves it, derived from the table
#: above rather than written out again. Read capabilities only: the write
#: vocabulary is an inert declaration and the deferred one is not enabled, so
#: neither has a row and neither produces a plan. There is no default row, no
#: fallback and no builder that takes a subcommand as data - the subcommand is
#: a constant.
_GH_ARGV_BUILDERS: Mapping[str, Any] = {
    capability: (
        lambda r, _c=capability: (
            *GITHUB_GRAPHQL_ARGS,
            "-f",
            f"query={_read_query(_c, r)}",
        )
    )
    for capability in _GRAPHQL_SELECTIONS
}


@dataclass(frozen=True, slots=True)
class GitHubOperationPlan:
    """The argument vector one validated read operation WOULD use. DATA.

    Constructible by anyone, exactly like every other dataclass, and that is
    fine: constructing one grants nothing, because nothing in this build acts
    on one. ``github_operation_plan_refusal`` decides whether a plan is
    self-consistent by re-deriving it, so a forged plan is caught by what it
    SAYS rather than by where it came from.

    It carries no executable, no environment, no working directory, no stdin
    and no timeout. Those are not omitted fields waiting to be filled in -
    they are the Phase-2 design this build has not done, and representing
    them now would be pretending it had.
    """

    capability: str
    destination: str
    resource: GitHubResource
    arguments: tuple[str, ...]
    host: str = GITHUB_HOST

    #: A plan is never evidence that anything may run. Stated as a class
    #: constant so no instance can claim otherwise.
    authorizes_execution: ClassVar[bool] = False


def github_operation_plan_refusal(plan: Any) -> str | None:
    """Why this plan is not the closed plan it claims to be, or ``None``.

    THE check to make whenever a plan is interpreted, and it asks nothing
    about provenance. Everything is re-derived from the plan's own typed
    resource: the capability must be a closed READ capability, the resource
    must bind to the capability and the destination under the canonical
    grammar, the host must be the one fixed host, and the arguments must
    equal - element for element - what this module's builder produces. An
    arbitrary subcommand, an injected ``--repo``/``--hostname``, a second
    repository or a token-shaped argument all fail that equality.
    """
    if type(plan) is not GitHubOperationPlan:
        return "the plan is not a GitHubOperationPlan"
    if plan.host != GITHUB_HOST:
        return "the plan names a host this build does not talk to"
    if type(plan.capability) is not str or plan.capability not in _GH_ARGV_BUILDERS:
        return "the plan names no closed GitHub read operation"
    reason = github_resource_binding_refusal(
        plan.resource, capability=plan.capability, destination=plan.destination
    )
    if reason is not None:
        return reason
    derived = _derived_arguments(plan.capability, plan.resource)
    if type(derived) is str:
        return derived
    if type(plan.arguments) is not tuple or plan.arguments != derived:
        return "the plan's arguments are not the ones this operation derives"
    return None


def _derived_arguments(capability: str, resource: GitHubResource) -> tuple[str, ...] | str:
    """The derived argument vector, or the reason there is none.

    A builder that refuses - a value that cannot be spelled into a query -
    becomes that reason rather than an exception, so every caller of this
    keeps returning refusals instead of raising through a check.
    """
    try:
        arguments = _GH_ARGV_BUILDERS[capability](resource)
    except GitHubExecutionUnavailable as exc:
        return str(exc)
    if len(arguments) > GH_MAX_ARGUMENTS:
        return "the argument list is over the bound"
    for argument in arguments:
        if _ascii_text_refusal(argument, "argument", GH_ARGUMENT_MAX_CHARS):
            return "an argument is not bounded ASCII text"
        # A credential must never be an argument. Nothing above can produce
        # one, and this says so at the place it would have to appear.
        if credential_bearing(argument):
            return "an argument carries a credential shape"
    return tuple(arguments)


def github_operation_plan(frame: Any) -> GitHubOperationPlan:
    """The closed plan for one validated frame, or fail closed.

    Takes a whole ``GitHubRequestFrame`` rather than loose parts, so the
    repository, the resource kind and the capability that reach the builder
    are the same ones the approval digest binds. A caller cannot pass a
    subcommand, a flag, a repository, a hostname, stdin, an environment or an
    executable path, because none of those are parameters.

    A frame is itself just a dataclass, so one can be hand-built - and it
    changes nothing. The plan that comes back is still data, still checked by
    re-derivation, and still authorizes nothing. Core's authority lives in the
    exact ``DestinationGrant`` tuple and the Core-owned identity binding, and
    neither of those consults a frame or a plan.
    """
    if type(frame) is not GitHubRequestFrame:
        raise GitHubExecutionUnavailable("the plan names no validated frame")
    if frame.capability not in _GH_ARGV_BUILDERS:
        raise GitHubExecutionUnavailable(
            "no GitHub operation exists for this capability in this build"
        )
    reason = github_resource_binding_refusal(
        frame.resource, capability=frame.capability, destination=frame.destination
    )
    if reason is not None:
        raise GitHubExecutionUnavailable(reason)
    derived = _derived_arguments(frame.capability, frame.resource)
    if type(derived) is str:
        raise GitHubExecutionUnavailable(derived)
    plan = GitHubOperationPlan(
        capability=frame.capability,
        destination=frame.destination,
        resource=frame.resource,
        arguments=derived,
    )
    reason = github_operation_plan_refusal(plan)
    if reason is not None:  # pragma: no cover - the derivation just built it
        raise GitHubExecutionUnavailable(reason)
    return plan


# --------------------------------------------------------------------------
# The external read contract - types only
# --------------------------------------------------------------------------

#: Ceiling on external content that may reach a model or an envelope. Its OWN
#: policy surface: external inbound text is not a local read excerpt, and the
#: two must stay free to diverge.
EXTERNAL_READ_MAX_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class ExternalReadSnapshot:
    """One bounded, task-scoped view of content fetched from outside.

    UNTRUSTED DATA WITH NO AUTHORITY. It is reference material and never an
    instruction: it cannot select a capability, name or widen a destination,
    supply or satisfy an approval, classify billing, choose a connector,
    author a plan, enter project memory or grant a tool. That is enforced by
    what this type does NOT have - there is no capability-grant,
    destination-grant, approval, billing, memory or tool-grant field anywhere
    on it - and by ``trusted``, a class constant that is always ``False`` and
    that nothing constructing a snapshot can set.

    ``destination``/``resource_kind``/``resource_id`` are the identity of the
    request that PRODUCED the content, bound by Core from the task's own
    trusted state - never read back out of the content itself. ``capability``
    names the producer for the same reason, and is not a permission.
    ``classification`` defaults to SECRET so an unlabelled snapshot is the
    most restricted one, not the least.
    """

    source_task_id: str = ""
    connector: str = ""
    capability: str = ""
    destination: str = ""
    resource_kind: str = ""
    resource_id: str = ""
    content: str = ""
    truncated: bool = False
    byte_size: int = 0
    sha256: str = ""
    classification: DataClassification = DataClassification.SECRET
    captured_at: str = ""

    #: External content is never trusted, in any snapshot, ever.
    trusted: ClassVar[bool] = False

    @property
    def present(self) -> bool:
        return bool(self.source_task_id and self.destination)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_task_id": self.source_task_id,
            "connector": self.connector,
            "capability": self.capability,
            "destination": self.destination,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "content": self.content,
            "truncated": self.truncated,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "classification": self.classification.value,
            "captured_at": self.captured_at,
        }


def external_read_refusal(snapshot: Any) -> str | None:
    """Why this snapshot is not usable reference data, or ``None``.

    SELF-CONSISTENCY. Everything here can be checked from the snapshot alone:
    the shapes, the closed vocabularies, and - the part that matters - that
    the digest, the byte size and the capture time really describe the
    content the object is carrying rather than whatever a producer claimed
    beside it. Whether that content answers the request Core actually
    approved is the separate question ``external_read_binding_refusal``
    asks; neither one grants the snapshot any authority.
    """
    if type(snapshot) is not ExternalReadSnapshot:
        return "the snapshot is not an ExternalReadSnapshot"
    if type(snapshot.classification) is not DataClassification:
        return "the snapshot classification is not recognized"
    if snapshot.connector != GITHUB_CONNECTOR:
        return "the snapshot names no known producer"
    if snapshot.capability not in GITHUB_READ_CAPABILITIES:
        return "the snapshot capability is not a GitHub read capability"
    if not is_github_destination(snapshot.destination):
        return "the snapshot destination is not a canonical repository"
    if type(snapshot.truncated) is not bool:
        return "the snapshot truncation flag is not a boolean"
    if type(snapshot.content) is not str:
        return "the snapshot content is not a string"
    if len(snapshot.content) > EXTERNAL_READ_MAX_CHARS:
        return "the snapshot content is over the bound"
    reason = _sha256_hex_refusal(snapshot.sha256, "snapshot digest")
    if reason is not None:
        return reason
    if type(snapshot.byte_size) is not int or snapshot.byte_size < 0:
        return "the snapshot byte size is not a count"
    # The three claims a producer could otherwise make freely, each checked
    # against the bytes the snapshot is actually carrying.
    encoded = snapshot.content.encode("utf-8")
    if snapshot.sha256 != hashlib.sha256(encoded).hexdigest():
        return "the snapshot digest is not the digest of its content"
    if snapshot.byte_size != len(encoded):
        return "the snapshot byte size is not the size of its content"
    if canonical_utc_timestamp(snapshot.captured_at) is None:
        return "the snapshot capture time is not a canonical UTC timestamp"
    if type(snapshot.source_task_id) is not str or not snapshot.source_task_id:
        return "the snapshot names no source task"
    if type(snapshot.resource_kind) is not str or snapshot.resource_kind not in {
        kind.value for kind in GitHubResourceKind
    }:
        return "the snapshot resource kind is not a recognized kind"
    if type(snapshot.resource_id) is not str or not snapshot.resource_id:
        return "the snapshot names no resource"
    return None


def external_read_binding_refusal(snapshot: Any, *, frame: Any) -> str | None:
    """Why this snapshot does not describe THIS validated request, or ``None``.

    The binding half. A snapshot's identity fields are Core's statement about
    what was asked for, so they must equal the already-validated frame's -
    exactly, field by field. A snapshot that claims a different repository, a
    different resource kind or a different resource than the request that
    produced it is refused rather than reconciled: reconciling it would let
    content fetched for one resource be presented as the answer to another.
    """
    if type(frame) is not GitHubRequestFrame:
        return "the snapshot names no validated request"
    reason = external_read_refusal(snapshot)
    if reason is not None:
        return reason
    reason = github_resource_binding_refusal(
        frame.resource, capability=frame.capability, destination=frame.destination
    )
    if reason is not None:
        return reason
    if snapshot.connector != frame.connector:
        return "the snapshot connector is not the requesting connector"
    if snapshot.capability != frame.capability:
        return "the snapshot capability is not the requested capability"
    if snapshot.destination != frame.destination:
        return "the snapshot repository is not the requested repository"
    if snapshot.resource_kind != frame.resource.kind.value:
        return "the snapshot resource kind is not the requested kind"
    if snapshot.resource_id != frame.resource.identity:
        return "the snapshot resource is not the requested resource"
    return None


def github_atomic_read_content(stdout: Any, *, frame: Any) -> str:
    """The data half of ONE atomic read, or refuse - never a partial answer.

    THIS IS THE PRINCIPAL BINDING, and it is the only one that describes the
    credential that actually served the read. The viewer and the data were
    selected in the same GraphQL request, so the login here was answered by
    the very token that fetched these bytes: there is no interval for an
    account to move in, and no second observation whose agreement could be
    coincidence. An account that switches away and back around the read
    changes THIS login, because the request either ran under the bound
    principal or it did not.

    Refuses on every uncertainty rather than reporting a best guess: a body
    that is not one JSON object, any ``errors`` at all, a missing or
    unparseable viewer, a viewer that is not the principal the frame binds,
    or a payload with no data beside the viewer. The viewer is then REMOVED,
    so the identity proof is never handed on as content - it did its work
    here.

    Nothing from the response reaches a refusal message. A GraphQL error can
    carry paths, ids and repository names, so the shape of the failure is
    stated and the body is not.
    """
    if type(frame) is not GitHubRequestFrame:
        raise GitHubExecutionUnavailable("the read names no validated request")
    if type(stdout) is not str or not stdout.strip():
        raise GitHubExecutionUnavailable("the GitHub read returned no response")
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise GitHubExecutionUnavailable(
            "the GitHub read did not return one JSON response"
        ) from exc
    if type(payload) is not dict:
        raise GitHubExecutionUnavailable(
            "the GitHub read did not return one JSON response"
        )
    if payload.get("errors"):
        raise GitHubExecutionUnavailable(
            "the GitHub API refused part of the request"
        )
    data = payload.get("data")
    if type(data) is not dict or not data:
        raise GitHubExecutionUnavailable("the GitHub read returned no data")
    viewer = data.get("viewer")
    if type(viewer) is not dict:
        raise GitHubExecutionUnavailable(
            "the GitHub read did not identify the account that served it"
        )
    login = viewer.get("login")
    if github_principal_refusal(login) is not None:
        raise GitHubExecutionUnavailable(
            "the GitHub read did not identify the account that served it"
        )
    if login.lower() != frame.principal.lower():
        raise GitHubExecutionUnavailable(
            "the GitHub read was served by an account that is not the "
            "principal this request is bound to"
        )
    rest = {name: value for name, value in data.items() if name != "viewer"}
    if not rest or rest.get("repository") is None:
        raise GitHubExecutionUnavailable(
            "the GitHub read returned no content for this repository"
        )
    # Deterministic spelling, so the same response always produces the same
    # digest for the snapshot that carries it.
    return json.dumps(rest, sort_keys=True, separators=(",", ":"))


def build_external_read_snapshot(
    *,
    source_task_id: str,
    frame: Any,
    content: str,
    captured_at: str,
    truncated: bool = False,
) -> ExternalReadSnapshot:
    """The snapshot for one validated request and the content it returned.

    THE only sanctioned way to make one. Every field a producer could have
    lied about is DERIVED here instead of accepted: the connector, the
    capability, the repository, the resource kind and the canonical resource
    id come from the validated frame, and the digest and the byte size come
    from the content itself. What is left to a caller is the task id, the
    content, the truncation flag and the capture time - and the capture time
    still has to be a canonical UTC timestamp.

    Fails closed with ``AttestationError``: a snapshot that cannot be built
    truthfully must not exist in a half-built form that later looks whole.
    The result is still UNTRUSTED DATA. Deriving its metadata makes the
    metadata honest; it gives the content no authority at all.
    """
    if type(frame) is not GitHubRequestFrame:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED, "the snapshot names no validated request"
        )
    if frame.capability not in GITHUB_READ_CAPABILITIES:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED,
            "the snapshot capability is not a GitHub read capability",
        )
    if type(content) is not str:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED, "the snapshot content is not a string"
        )
    if len(content) > EXTERNAL_READ_MAX_CHARS:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED, "the snapshot content is over the bound"
        )
    if type(truncated) is not bool:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED, "the truncation flag is not a boolean"
        )
    if type(source_task_id) is not str or not source_task_id:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED, "the snapshot names no source task"
        )
    if canonical_utc_timestamp(captured_at) is None:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED,
            "the capture time is not a canonical UTC timestamp",
        )
    encoded = content.encode("utf-8")
    snapshot = ExternalReadSnapshot(
        source_task_id=source_task_id,
        connector=frame.connector,
        capability=frame.capability,
        destination=frame.destination,
        resource_kind=frame.resource.kind.value,
        resource_id=frame.resource.identity,
        content=content,
        truncated=truncated,
        byte_size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        classification=DataClassification.SECRET,
        captured_at=captured_at,
    )
    reason = external_read_binding_refusal(snapshot, frame=frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return snapshot


__all__ = [
    "EXTERNAL_READ_MAX_CHARS",
    "ExternalReadSnapshot",
    "GH_ARGUMENT_MAX_CHARS",
    "GH_MAX_ARGUMENTS",
    "GITHUB_BILLING_CLASS",
    "GITHUB_CAPABILITIES",
    "GITHUB_CAPABILITY_RESOURCE",
    "GITHUB_CONNECTOR",
    "GITHUB_CREDENTIAL_ROUTE",
    "GITHUB_CREDENTIAL_ROUTES",
    "GITHUB_DEFERRED_READ_CAPABILITIES",
    "GITHUB_DESTINATION_MAX_CHARS",
    "GITHUB_DESTINATION_PREFIX",
    "GITHUB_EMPTY_BODY_SHA256",
    "GITHUB_ENABLED_CAPABILITIES",
    "GITHUB_FORCED_ENVIRONMENT",
    "GITHUB_HOST",
    "GITHUB_GRAPHQL_ARGS",
    "GITHUB_HOSTNAME_ARGS",
    "GITHUB_IDENTITY_BINDING",
    "GITHUB_LIST_MAX_ITEMS",
    "GITHUB_LIST_STATES",
    "GITHUB_MONETARY_STATE",
    "GITHUB_NUMBER_MAX",
    "GITHUB_OPERATION_MAX_CHARS",
    "GITHUB_PATH_MAX_CHARS",
    "GITHUB_QUERY_MAX_CHARS",
    "GITHUB_READ_CAPABILITIES",
    "GITHUB_READ_OPERATION",
    "GITHUB_REF_MAX_CHARS",
    "GITHUB_SEARCH_MAX_ITEMS",
    "GITHUB_REMOVED_ENVIRONMENT",
    "GITHUB_REMOVED_ENVIRONMENT_PREFIXES",
    "GITHUB_REQUEST_FRAME_HEADER",
    "GITHUB_VIEWER_SELECTION",
    "GITHUB_WRITE_CAPABILITIES",
    "GitHubExecutionUnavailable",
    "GitHubOperationPlan",
    "GitHubIdentityBinding",
    "GitHubRepository",
    "GitHubRequestFrame",
    "GitHubResource",
    "GitHubResourceKind",
    "OWNER_MAX_CHARS",
    "REPOSITORY_MAX_CHARS",
    "build_external_read_snapshot",
    "build_github_request_frame",
    "external_read_binding_refusal",
    "external_read_refusal",
    "github_atomic_read_content",
    "github_comment_body",
    "github_credential_route_refusal",
    "github_destination",
    "github_destination_refusal",
    "github_identity_binding_refusal",
    "github_identity_refusal",
    "github_issue_body",
    "github_number_refusal",
    "github_operation_plan",
    "github_operation_plan_refusal",
    "github_path_refusal",
    "github_principal_refusal",
    "github_raw_destination_refusal",
    "github_query_refusal",
    "github_ref_refusal",
    "github_resource_binding_refusal",
    "github_resource_from_dict",
    "github_resource_refusal",
    "is_github_destination",
    "sanitized_github_environment",
]
