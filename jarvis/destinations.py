"""Core-owned canonical destination policy.

THE authority that decides whether an outbound destination may be reached.

A destination that arrives with a task - from task JSON, from a chain
declaration, from a model answer, from retrieved memory, from web content or
from anything a connector hands back - is UNTRUSTED. It is a *request*, never
a permission. A connector may understand and canonicalize its own
app-specific target syntax, but it can never register a destination here and
can never widen this policy at runtime: the registry below is a module
constant in Core, and adding a row to it is a source change under human
review.

Two distinct concepts, deliberately kept apart:

``requested``  what the untrusted input says the target is.
``canonical``  the one deterministic spelling Core decides with, produced by
               ``normalize_destination`` - purely lexical, never a filesystem
               resolve and never a network round trip, so canonicalizing a
               NAME can never become an inspection of the thing it names.

Canonicalization is not authorization. A canonical destination is permitted
only when it is a row of ``DESTINATION_REGISTRY``; everything else - an
unregistered endpoint, a non-canonical spelling, a value that is not even a
string - fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AbstractSet, Any, Mapping

from jarvis.browser import browser_raw_destination_refusal
from jarvis.contracts import (
    MAILBOX_DESTINATION_PREFIX,
    ORIGIN_DESTINATION_PREFIX,
    REPOSITORY_DESTINATION_PREFIX,
    normalize_destination,
)
from jarvis.github import github_raw_destination_refusal
from jarvis.gmail import gmail_raw_destination_refusal


class DestinationKind(str, Enum):
    """What sort of endpoint a canonical destination names.

    The kind is Core's, not a connector's. It exists so a future row can be
    something other than a provider endpoint (a mailbox, a repository, an
    origin) without any call site learning a new special case. Only the kinds
    this build actually has are declared - an unknown kind is not a wildcard,
    it simply does not exist yet.
    """

    #: No outbound endpoint at all. A purely local effect names this.
    NONE = "none"
    #: A named external model/provider endpoint one connector reaches.
    PROVIDER_ENDPOINT = "provider_endpoint"
    #: One remote source-code repository, spelled ``github/<owner>/<repo>``.
    #: Unlike the two above it is not enumerable - there are millions of
    #: repositories - so its VALIDITY comes from a closed grammar rather than
    #: from a registry row. That changes nothing about AUTHORIZATION: a
    #: perfectly well-formed repository is still refused unless an exact
    #: ``DestinationGrant`` names it, and this build ships none.
    REPOSITORY = "repository"
    #: One mailbox, spelled ``gmail/<account>``. Like a repository it is
    #: not enumerable, so its validity comes from the closed grammar in
    #: ``jarvis.gmail``; reaching it still needs an exact grant, which only
    #: a configured principal composes into.
    MAILBOX = "mailbox"
    #: One web origin, spelled ``browser/<scheme>://<host>[:<port>]``. Like a
    #: repository and a mailbox it is not enumerable, so its validity comes
    #: from the closed grammar in ``jarvis.browser``; reaching it still needs
    #: an exact grant, which only a configured origin composes into.
    ORIGIN = "origin"


#: The canonical destination the Claude Code connector is registered for, and
#: the build default when a task names none.
CLAUDE_CODE_DESTINATION = "anthropic/claude-code"

#: The destination a task carries when its input names none. It is a policy
#: value, not a bottleneck: every decision below reads the REGISTRY, so a
#: second registered destination needs no change at any boundary.
DEFAULT_TASK_DESTINATION = CLAUDE_CODE_DESTINATION

#: THE closed registry of ENUMERABLE destinations. Adding Gmail or a browser
#: destination means adding a row HERE, in Core. No connector return value, no
#: task input, no chain declaration and no model answer can add one, and there
#: is no wildcard row: an unregistered destination is refused, always.
#:
#: One family is deliberately NOT a row: a GitHub repository, of which there
#: are millions. Its validity comes from the closed grammar in
#: ``jarvis.github`` instead (see ``destination_refusal``). That is a
#: statement about SPELLING, not about permission - the tuple policy below is
#: still the only thing that authorizes anything, and it holds no GitHub row.
DESTINATION_REGISTRY: Mapping[str, DestinationKind] = {
    #: A task with no outbound endpoint. Not "anything goes" - it is the
    #: explicit statement that nothing leaves, and it can never satisfy an
    #: external-egress effect (see ``destination_refusal``).
    "": DestinationKind.NONE,
    CLAUDE_CODE_DESTINATION: DestinationKind.PROVIDER_ENDPOINT,
}


@dataclass(frozen=True, slots=True)
class DestinationGrant:
    """One authorized (connector, capability, canonical destination) tuple.

    The WHOLE tuple is the key. A grant says nothing about the connector in
    general, nothing about the capability in general and nothing about the
    destination in general - only that this exact combination is permitted.
    """

    connector: str
    capability: str
    destination: str


#: THE closed authorization table, and the ONLY thing that authorizes an
#: outbound destination. Everything else in this module - the registry, the
#: kinds, the canonicalizer - decides whether a destination is well formed and
#: known; none of it decides who may reach it.
#:
#: The rows below are exactly the combinations this build already performs,
#: read off the live connector manifests (``claude-code-plan`` serving
#: ``code_analysis``/``code_planning``, and the two local-file connectors,
#: which carry this destination in scope today). Nothing is here for a future
#: connector: adding Gmail, GitHub or a browser means adding rows HERE, in
#: Core, under human review.
#:
#: There is no wildcard row and no row that stands for "any" connector,
#: capability or destination, and registering a new destination in
#: ``DESTINATION_REGISTRY`` authorizes nobody for it.
CONNECTOR_DESTINATION_POLICY: frozenset[DestinationGrant] = frozenset(
    {
        DestinationGrant("claude-code-plan", "code_analysis", CLAUDE_CODE_DESTINATION),
        DestinationGrant("claude-code-plan", "code_planning", CLAUDE_CODE_DESTINATION),
        DestinationGrant("local-file-read", "file_read", CLAUDE_CODE_DESTINATION),
        DestinationGrant("local-file-write", "file_write", CLAUDE_CODE_DESTINATION),
    }
)


def connector_destination_refusal(
    policy: AbstractSet[DestinationGrant],
    *,
    connector: str,
    capability: str,
    destination: str,
    egress: bool,
) -> str | None:
    """Why Core does not authorize this exact tuple, or ``None``.

    Two independent questions, in order, both fail-closed:

    1. is the canonical destination well formed and known to Core at all
       (``destination_refusal``) - validity, never authorization;
    2. is the EXACT (connector, capability, destination) tuple a row of the
       Core-owned policy - authorization, and nothing else grants it.

    ``connector`` must be the registered manifest name and ``capability`` the
    task's own capability, compared by exact equality exactly as
    ``ToolRegistry`` and ``can_handle`` compare them. The empty destination
    names no endpoint, so it needs no row - and it can never carry an egress,
    which question 1 already refuses.
    """
    reason = destination_refusal(destination, egress=egress)
    if reason is not None:
        return reason
    if not destination:
        return None
    if DestinationGrant(connector, capability, destination) not in policy:
        return (
            "no Core policy authorizes this connector and capability for "
            "this destination"
        )
    return None


def canonical_destination(requested: Any) -> str | None:
    """The canonical spelling of an untrusted requested destination.

    ``None`` for anything that is not a string. Deterministic and offline:
    exactly the normalizer ``ActionScope.normalized`` already applies, so the
    value Core decides with is the value persistence, the fingerprint and the
    attestation all hold - including its refusal to REPAIR a noncanonical
    repository spelling, which is enforced there, centrally, for every path
    into Core rather than only for the ones that come through here.
    """
    if type(requested) is not str:
        return None
    return normalize_destination(requested)


def destination_refusal(canonical: Any, *, egress: bool = False) -> str | None:
    """Why Core refuses this CANONICAL destination, or ``None``.

    The argument must already be canonical. A value that is not its own
    canonical form is refused rather than quietly repaired: repairing it here
    would mean the destination Core checked and the destination an approval
    binds are two different strings.

    ``egress`` says the effective side effect is an external egress. Such a
    task must name a real registered endpoint - the empty destination is a
    statement that nothing leaves, so it can never authorize something that
    does.
    """
    if type(canonical) is not str:
        return "the destination is not a string"
    if normalize_destination(canonical) != canonical:
        return "the destination is not in canonical form"
    kind = DESTINATION_REGISTRY.get(canonical)
    if kind is None and canonical.startswith(REPOSITORY_DESTINATION_PREFIX):
        # It asked for the repository namespace, so the closed GitHub grammar
        # answers - and it answers on the spelling as written. ``normalize_
        # destination`` deliberately hands a noncanonical repository spelling
        # through UNREPAIRED, so this is where a laundering attempt lands and
        # it is named as one rather than reported as "unregistered".
        reason = github_raw_destination_refusal(canonical)
        if reason is not None:
            return f"the destination is not a canonical GitHub repository: {reason}"
        # VALIDITY, and only validity. The grammar says this string names a
        # repository Core can talk about; the tuple policy still has to say
        # whether anyone may reach it, and for GitHub it says no.
        kind = DestinationKind.REPOSITORY
    if kind is None and canonical.startswith(MAILBOX_DESTINATION_PREFIX):
        # The mailbox namespace, judged by the closed Gmail grammar on the
        # spelling as written, for exactly the reason the repository is.
        reason = gmail_raw_destination_refusal(canonical)
        if reason is not None:
            return f"the destination is not a canonical mailbox: {reason}"
        kind = DestinationKind.MAILBOX
    if kind is None and canonical.startswith(ORIGIN_DESTINATION_PREFIX):
        # The origin namespace, judged by the closed browser grammar on the
        # spelling as written, for exactly the reason the other two are.
        reason = browser_raw_destination_refusal(canonical)
        if reason is not None:
            return f"the destination is not a canonical origin: {reason}"
        kind = DestinationKind.ORIGIN
    if kind is None:
        return "the destination is not registered with Core"
    if egress and kind is DestinationKind.NONE:
        return "an external egress names no outbound destination"
    return None


__all__ = [
    "CLAUDE_CODE_DESTINATION",
    "CONNECTOR_DESTINATION_POLICY",
    "DEFAULT_TASK_DESTINATION",
    "DESTINATION_REGISTRY",
    "DestinationGrant",
    "DestinationKind",
    "canonical_destination",
    "connector_destination_refusal",
    "destination_refusal",
]
