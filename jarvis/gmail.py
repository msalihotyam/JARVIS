"""Gmail minimum usable-v1 - the closed vocabulary, grammar, frame and plan.

The same shape as ``jarvis.github``, on purpose: one mailbox is one canonical
destination ``gmail/<account>``, a task names a TYPED resource inside it and
nothing else, Core owns the identity the call must act under, an approval
binds one canonical request frame, and a read returns an UNTRUSTED bounded
snapshot through the one external-read transport ``jarvis.github`` already
defines. Nothing here opens a socket, reads a credential or starts a process;
the process seam is ``jarvis.gmail_cli`` and the connector is
``jarvis.connectors.gmail_cli``.

WHICH ACCOUNT SERVED A CALL. Every derived request names the bound principal
in its OWN path - ``users/<account>/...`` - and never the API's ``me`` alias,
so the Gmail API itself refuses to serve it under a credential that is not
that account's. That is the per-request principal binding: there is no
interval between an observation and a read for a session to move in, because
the read cannot be served for another account at all.

WHAT A WRITE BINDS. A draft and a send are the same typed resource: sender
(the bound account), exact recipients, subject and body. JARVIS builds the
RFC 2822 bytes itself, deterministically, and the approval digest covers
them, so a changed recipient, subject or body is a different frame and the
approval is no longer for it. Model-provided text is content inside that
frame; it cannot add a recipient, change the account, pick the operation or
reach any authority.

Core, not a connector, is the authority. Everything here is a canonicalizer
or a refusal; none of it authorizes anything. A mailbox becomes reachable
only when the composition root turned a configured principal into a
``GmailIdentityBinding`` and exact ``DestinationGrant`` rows.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Mapping

from jarvis.contracts import (
    AttestationError,
    BillingClass,
    DataClassification,
    ErrorCode,
    MAILBOX_DESTINATION_PREFIX,
    MonetaryState,
    canonical_utc_timestamp,
    credential_bearing,
)
from jarvis.github import (
    EXTERNAL_READ_MAX_CHARS,
    ExternalReadSnapshot,
    external_read_shape_refusal,
)

# --------------------------------------------------------------------------
# Identity, closed capability vocabulary and billing
# --------------------------------------------------------------------------

#: The one registered connector name a Gmail grant may ever name.
GMAIL_CONNECTOR = "gmail-cli"

#: Prefix of the canonical destination spelling ``gmail/<account>``. Taken
#: from Core's central normalizer, which has to recognize it to refuse
#: repairing it.
GMAIL_DESTINATION_PREFIX = MAILBOX_DESTINATION_PREFIX

#: THE closed read vocabulary. Four identifiers: a bounded listing, a bounded
#: search, one message and one thread. No ``gmail_read``, no ``gmail_api``,
#: no query object and no wildcard.
GMAIL_READ_CAPABILITIES: tuple[str, ...] = (
    "gmail_thread_list",
    "gmail_search",
    "gmail_message_read",
    "gmail_thread_read",
)

#: THE closed write vocabulary: create one draft, send one message. Both are
#: side effects behind exact approval. There is deliberately no reply, no
#: forward, no label, no archive, no delete, no bulk and no auto-anything:
#: those names do not exist, so no task can name them.
GMAIL_WRITE_CAPABILITIES: tuple[str, ...] = ("gmail_draft_create", "gmail_send")

#: DECLARED past usable-v1 and NOT enabled: attachment content is advanced
#: attachment processing. The name and its resource kind are reserved here so
#: a later phase cannot invent them differently; there is no argument builder
#: for it, so a frame naming it produces no plan.
GMAIL_DEFERRED_CAPABILITIES: tuple[str, ...] = ("gmail_attachment_read",)

GMAIL_CAPABILITIES: tuple[str, ...] = (
    GMAIL_READ_CAPABILITIES + GMAIL_WRITE_CAPABILITIES + GMAIL_DEFERRED_CAPABILITIES
)

#: What this build can EXECUTE. Still not authority: an enabled capability
#: reaches a mailbox only under an exact ``DestinationGrant`` row AND the
#: Core-owned identity binding for that account.
GMAIL_ENABLED_CAPABILITIES: tuple[str, ...] = (
    GMAIL_READ_CAPABILITIES + GMAIL_WRITE_CAPABILITIES
)

#: The Gmail API through an already-authenticated local CLI has no per-call
#: price. Quotas are not money.
GMAIL_BILLING_CLASS = BillingClass.NO_ADDITIONAL_CHARGE
GMAIL_MONETARY_STATE = MonetaryState.NOT_APPLICABLE

#: THE canonical credential-route identity, and it names exactly what is
#: allowed: OAuth2 USER credentials that the Google Workspace CLI stores
#: ENCRYPTED in a JARVIS-DEDICATED config directory, with the encryption key
#: held by the OS KEYRING. Not a token from the environment, not a
#: credentials file named by the environment, not plaintext storage, not
#: Application Default Credentials and not a service account: the process
#: seam proves this route with ``gws auth status`` before every call
#: (``gmail_route_refusal``), and the constant is reported only after that
#: proof. JARVIS never requests, retrieves, persists or presents the token.
GMAIL_CREDENTIAL_ROUTE = "gws_dedicated_config_oauth2_encrypted_keyring"
GMAIL_CREDENTIAL_ROUTES = frozenset({GMAIL_CREDENTIAL_ROUTE})


# --------------------------------------------------------------------------
# Grammar - pure, lexical, offline
# --------------------------------------------------------------------------

#: A bare address, and only a bare address: no display name, no angle
#: brackets, no comment, no group syntax, no IDN and no quoted local part.
#: Those are all real RFC 5322 forms; none of them is expressible here, so
#: none of them can smuggle a second recipient or a header break.
_LOCAL_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9._+-]*[A-Za-z0-9])?"
_LABEL_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_ADDRESS_PATTERN = re.compile(
    rf"\A{_LOCAL_PATTERN}@(?:{_LABEL_PATTERN}\.)+{_LABEL_PATTERN}\Z"
)
GMAIL_ADDRESS_MAX_CHARS = 254
GMAIL_LOCAL_PART_MAX_CHARS = 64
#: Hard ceiling on a raw requested destination before it is even split.
GMAIL_DESTINATION_MAX_CHARS = 264

#: Gmail message, thread, draft and attachment identifiers are opaque
#: URL-safe tokens. Bounded ASCII, no separators, no path syntax.
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\Z")
GMAIL_ID_MAX_CHARS = 128

#: The deterministic list bound. A listing is a bounded read, never an export
#: - and never a bulk operation.
GMAIL_LIST_MAX_ITEMS = 25
#: Hard ceiling on the one search query. Gmail's own search syntax is allowed
#: inside it: a query selects WHICH of the bound account's messages to list
#: and can reach nothing outside that account, because the account is fixed
#: by the request path rather than by anything in the query.
GMAIL_QUERY_MAX_CHARS = 256
#: Recipients across to/cc/bcc, together. One message to a few people is a
#: message; more than this is a campaign, which is not a usable-v1 feature.
GMAIL_RECIPIENTS_MAX = 10
GMAIL_SUBJECT_MAX_CHARS = 256
#: Bound on the body in UTF-8 BYTES. A usable-v1 message is a bounded
#: deliverable, not an attachment channel; 12 000 bytes is a long email.
GMAIL_BODY_MAX_BYTES = 12_000


def _text_refusal(value: Any, label: str, cap: int, *, ascii_only: bool) -> str | None:
    """Why this is not bounded printable text, or ``None``.

    Exact ``str``, non-empty, within the cap, no control character (so no
    header break, no NUL, no escape sequence) and, where stated, ASCII.
    """
    if type(value) is not str:
        return f"the {label} is not a string"
    if not value:
        return f"the {label} is empty"
    if len(value) > cap:
        return f"the {label} is longer than {cap} characters"
    if ascii_only and not value.isascii():
        return f"the {label} is not ASCII"
    if any(char < " " or char == "\x7f" for char in value):
        return f"the {label} contains a control character"
    return None


def gmail_address_refusal(value: Any, label: str = "address") -> str | None:
    """Why this is not one bare mailbox address, or ``None``."""
    reason = _text_refusal(value, label, GMAIL_ADDRESS_MAX_CHARS, ascii_only=True)
    if reason is not None:
        return reason
    if not _ADDRESS_PATTERN.fullmatch(value):
        return f"the {label} is not a bare mailbox address"
    if len(value.split("@", 1)[0]) > GMAIL_LOCAL_PART_MAX_CHARS:
        return f"the {label} local part is too long"
    return None


def gmail_principal_refusal(value: Any) -> str | None:
    """Why this is not the canonical spelling of an account, or ``None``.

    A NON-SECRET identifier, already lowercase: the principal is the identity
    Core decides, persists, fingerprints and attests with, so it must have
    exactly one spelling. Grammar only - whether it is the account Core
    expects is ``gmail_identity_refusal``'s question.
    """
    reason = gmail_address_refusal(value, "principal")
    if reason is not None:
        return reason
    if value != value.lower():
        return "the principal is not already lowercase"
    return None


@dataclass(frozen=True, slots=True)
class GmailMailbox:
    """One canonical mailbox identity."""

    account: str

    @property
    def destination(self) -> str:
        return f"{GMAIL_DESTINATION_PREFIX}{self.account}"


def gmail_destination_refusal(requested: Any) -> str | None:
    """Why ``requested`` is not a mailbox destination, or ``None``."""
    reason = _text_refusal(
        requested, "destination", GMAIL_DESTINATION_MAX_CHARS, ascii_only=True
    )
    if reason is not None:
        return reason
    parts = requested.split("/")
    if len(parts) != 2:
        return "the destination is not exactly gmail/<account>"
    prefix, account = parts
    if prefix != "gmail":
        return "the destination does not name the gmail namespace"
    reason = gmail_principal_refusal(account)
    if reason is not None:
        return f"the destination account is unusable: {reason}"
    return None


def gmail_destination(requested: Any) -> GmailMailbox | None:
    """The canonical identity of a requested destination, or ``None``."""
    if gmail_destination_refusal(requested) is not None:
        return None
    return GmailMailbox(account=requested.split("/", 1)[1])


def is_gmail_destination(canonical: Any) -> bool:
    """Whether a CANONICAL destination string is a well-formed mailbox.

    VALIDITY ONLY: whether anyone may reach it is the exact-tuple question.
    """
    identity = gmail_destination(canonical)
    return identity is not None and identity.destination == canonical


def gmail_raw_destination_refusal(requested: Any) -> str | None:
    """Why this RAW, PRE-NORMALIZATION spelling is not a mailbox, or ``None``.

    The laundering boundary, exactly as for a repository: judged before any
    lossy normalization, so a spelling that would need repair is refused
    rather than repaired into a destination the requester never wrote.
    """
    if type(requested) is not str:
        return "the destination is not a string"
    if not requested.isascii():
        return "the destination is not ASCII"
    if any(character.isspace() for character in requested):
        return "the destination carries whitespace"
    if requested != requested.lower():
        return "the destination is not already lowercase"
    reason = gmail_destination_refusal(requested)
    if reason is not None:
        return reason
    if not is_gmail_destination(requested):
        return "the destination is not in canonical form"
    return None


# --------------------------------------------------------------------------
# Closed resource-target model
# --------------------------------------------------------------------------


class GmailResourceKind(str, Enum):
    """The closed set of things a Gmail task can name."""

    THREAD_LIST = "thread_list"
    SEARCH = "search"
    MESSAGE = "message"
    THREAD = "thread"
    NEW_DRAFT = "new_draft"
    OUTBOUND_MESSAGE = "outbound_message"
    ATTACHMENT = "attachment"


#: Capability -> the ONE resource kind it may name.
GMAIL_CAPABILITY_RESOURCE: Mapping[str, GmailResourceKind] = {
    "gmail_thread_list": GmailResourceKind.THREAD_LIST,
    "gmail_search": GmailResourceKind.SEARCH,
    "gmail_message_read": GmailResourceKind.MESSAGE,
    "gmail_thread_read": GmailResourceKind.THREAD,
    "gmail_draft_create": GmailResourceKind.NEW_DRAFT,
    "gmail_send": GmailResourceKind.OUTBOUND_MESSAGE,
    "gmail_attachment_read": GmailResourceKind.ATTACHMENT,
}

#: The kinds that are a write: the frame binds real body bytes and the
#: operation label is not ``read``.
GMAIL_WRITE_KINDS = frozenset({GmailResourceKind.NEW_DRAFT, GmailResourceKind.OUTBOUND_MESSAGE})

#: The optional fields, and which kinds MAY carry each. A field a kind does
#: not list must be absent: a listing cannot quietly carry a recipient, and
#: a send cannot quietly carry a query.
_RESOURCE_FIELDS: Mapping[GmailResourceKind, frozenset[str]] = {
    GmailResourceKind.THREAD_LIST: frozenset({"limit"}),
    GmailResourceKind.SEARCH: frozenset({"query", "limit"}),
    GmailResourceKind.MESSAGE: frozenset({"message_id"}),
    GmailResourceKind.THREAD: frozenset({"thread_id"}),
    GmailResourceKind.NEW_DRAFT: frozenset({"to", "cc", "bcc", "subject", "body"}),
    GmailResourceKind.OUTBOUND_MESSAGE: frozenset({"to", "cc", "bcc", "subject", "body"}),
    GmailResourceKind.ATTACHMENT: frozenset({"message_id", "attachment_id"}),
}

#: The empty value of every optional field, so "absent" has one spelling.
_RESOURCE_ABSENT: Mapping[str, Any] = {
    "query": "",
    "limit": 0,
    "message_id": "",
    "thread_id": "",
    "attachment_id": "",
    "to": (),
    "cc": (),
    "bcc": (),
    "subject": "",
    "body": "",
}


def gmail_id_refusal(value: Any, label: str = "identifier") -> str | None:
    reason = _text_refusal(value, label, GMAIL_ID_MAX_CHARS, ascii_only=True)
    if reason is not None:
        return reason
    if not _ID_PATTERN.fullmatch(value):
        return f"the {label} is not a Gmail identifier"
    return None


def gmail_query_refusal(value: Any) -> str | None:
    """Why this is not one bounded search query, or ``None``.

    Gmail's search operators are allowed: they narrow WHICH of the bound
    account's messages match, and the account is fixed by the request path.
    What is refused is a line break or any other control character, and an
    unbounded or empty string.
    """
    reason = _text_refusal(value, "search query", GMAIL_QUERY_MAX_CHARS, ascii_only=False)
    if reason is not None:
        return reason
    if value != value.strip():
        return "the search query has leading or trailing whitespace"
    return None


def _limit_refusal(value: Any) -> str | None:
    if type(value) is not int:
        return "the list bound is not an integer"
    if not 1 <= value <= GMAIL_LIST_MAX_ITEMS:
        return "the list bound is outside the deterministic range"
    return None


def _recipients_refusal(value: Any, label: str, *, required: bool) -> str | None:
    if type(value) is not tuple:
        return f"the {label} recipients are not a list"
    if required and not value:
        return f"the {label} recipients are empty"
    for address in value:
        reason = gmail_address_refusal(address, f"{label} recipient")
        if reason is not None:
            return reason
    return None


def gmail_subject_refusal(value: Any) -> str | None:
    return _text_refusal(value, "subject", GMAIL_SUBJECT_MAX_CHARS, ascii_only=False)


def gmail_body_refusal(value: Any) -> str | None:
    """Why this is not a bounded plain-text body, or ``None``.

    Line breaks and tabs are text; every other control character is not.
    """
    if type(value) is not str:
        return "the body is not a string"
    if not value.strip():
        return "the body is empty"
    if len(value.encode("utf-8")) > GMAIL_BODY_MAX_BYTES:
        return f"the body is longer than {GMAIL_BODY_MAX_BYTES} bytes"
    if any((char < " " and char not in "\n\r\t") or char == "\x7f" for char in value):
        return "the body contains a control character"
    return None


_RESOURCE_FIELD_REFUSAL = {
    "query": gmail_query_refusal,
    "limit": _limit_refusal,
    "message_id": lambda value: gmail_id_refusal(value, "message id"),
    "thread_id": lambda value: gmail_id_refusal(value, "thread id"),
    "attachment_id": lambda value: gmail_id_refusal(value, "attachment id"),
    "to": lambda value: _recipients_refusal(value, "to", required=True),
    "cc": lambda value: _recipients_refusal(value, "cc", required=False),
    "bcc": lambda value: _recipients_refusal(value, "bcc", required=False),
    "subject": gmail_subject_refusal,
    "body": gmail_body_refusal,
}


@dataclass(frozen=True, slots=True)
class GmailResource:
    """One typed Gmail resource target. Never a URL, endpoint or argv."""

    kind: GmailResourceKind
    account: str
    query: str = ""
    limit: int = 0
    message_id: str = ""
    thread_id: str = ""
    attachment_id: str = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str = ""
    body: str = ""

    @property
    def destination(self) -> str:
        return f"{GMAIL_DESTINATION_PREFIX}{self.account}"

    @property
    def recipients(self) -> tuple[str, ...]:
        return self.to + self.cc + self.bcc

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "account": self.account,
            "query": self.query,
            "limit": self.limit,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "attachment_id": self.attachment_id,
            "to": list(self.to),
            "cc": list(self.cc),
            "bcc": list(self.bcc),
            "subject": self.subject,
            "body": self.body,
        }

    @property
    def identity(self) -> str:
        """One unambiguous canonical spelling of this target."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def gmail_resource_refusal(resource: Any) -> str | None:
    """Why this is not a valid typed Gmail resource target, or ``None``."""
    if type(resource) is not GmailResource:
        return "the resource is not a GmailResource"
    if type(resource.kind) is not GmailResourceKind:
        return "the resource kind is not a recognized kind"
    if type(resource.account) is not str:
        return "the resource account is not a string"
    identity = gmail_destination(resource.destination)
    if identity is None or identity.account != resource.account:
        return "the resource does not name a canonical mailbox"
    allowed = _RESOURCE_FIELDS[resource.kind]
    for field_name, absent in _RESOURCE_ABSENT.items():
        value = getattr(resource, field_name)
        if field_name not in allowed:
            if type(value) is not type(absent) or value != absent:
                return f"the {resource.kind.value} target may not carry a {field_name}"
            continue
        reason = _RESOURCE_FIELD_REFUSAL[field_name](value)
        if reason is not None:
            return reason
    if resource.kind in GMAIL_WRITE_KINDS:
        recipients = resource.recipients
        if len(recipients) > GMAIL_RECIPIENTS_MAX:
            return f"the message names more than {GMAIL_RECIPIENTS_MAX} recipients"
        if len({address.lower() for address in recipients}) != len(recipients):
            return "the message names a recipient twice"
    return None


_RESOURCE_KEYS = frozenset({"kind", "account"} | set(_RESOURCE_ABSENT))
_LIST_FIELDS = frozenset({"to", "cc", "bcc"})


class GmailExecutionUnavailable(RuntimeError):
    """A Gmail operation cannot be derived or did not happen. Nothing did."""


class GmailOutcomeUnknown(RuntimeError):
    """A Gmail WRITE may or may not have happened.

    Deliberately NOT a subclass of ``GmailExecutionUnavailable``: that one
    means nothing happened. This one escapes the connector on purpose, so
    Core marks the attempt in doubt instead of recording a clean failure that
    an automation would retry into a second send.
    """


def gmail_resource_from_dict(data: Any) -> GmailResource:
    """Rebuild one typed resource from its serialized form, or fail closed.

    Exact keys, exact types (a recipient list is a JSON list of strings and
    becomes a tuple; nothing else is coerced), and the rebuilt object is then
    judged by ``gmail_resource_refusal`` exactly like a fresh one.
    """
    if not isinstance(data, dict):
        raise GmailExecutionUnavailable("the resource is not an object")
    if set(data) != _RESOURCE_KEYS:
        raise GmailExecutionUnavailable("the resource carries a missing or unsupported key")
    raw_kind = data["kind"]
    if type(raw_kind) is not str:
        raise GmailExecutionUnavailable("the resource kind is not a string")
    try:
        kind = GmailResourceKind(raw_kind)
    except ValueError:
        raise GmailExecutionUnavailable(f"unknown Gmail resource kind {raw_kind!r}") from None
    if type(data["account"]) is not str:
        raise GmailExecutionUnavailable("the resource account is not a string")
    fields: dict[str, Any] = {}
    for field_name, absent in _RESOURCE_ABSENT.items():
        value = data[field_name]
        if field_name in _LIST_FIELDS:
            if type(value) is not list or any(type(item) is not str for item in value):
                raise GmailExecutionUnavailable(
                    f"the resource {field_name} is not a list of strings"
                )
            fields[field_name] = tuple(value)
            continue
        if type(value) is not type(absent):
            raise GmailExecutionUnavailable(
                f"the resource {field_name} is not the type this contract holds"
            )
        fields[field_name] = value
    resource = GmailResource(kind=kind, account=data["account"], **fields)
    reason = gmail_resource_refusal(resource)
    if reason is not None:
        raise GmailExecutionUnavailable(reason)
    return resource


def gmail_resource_binding_refusal(
    resource: Any, *, capability: Any, destination: Any
) -> str | None:
    """Why this resource may not be executed under this task, or ``None``.

    THE invariant: the resource's account must equal the task's canonical
    destination, and the capability must be the one that names this kind.
    Mailbox A can never ride inside a task approved for mailbox B, and a read
    capability can never carry an outbound message.
    """
    reason = gmail_resource_refusal(resource)
    if reason is not None:
        return reason
    if type(capability) is not str or capability not in GMAIL_CAPABILITIES:
        return "the capability is not in the closed Gmail vocabulary"
    if GMAIL_CAPABILITY_RESOURCE[capability] is not resource.kind:
        return "the capability does not name this resource kind"
    if type(destination) is not str or not is_gmail_destination(destination):
        return "the task destination is not a canonical mailbox"
    if resource.destination != destination:
        return "the resource account is not the task destination"
    return None


# --------------------------------------------------------------------------
# Principal and credential route
# --------------------------------------------------------------------------


def gmail_credential_route_refusal(value: Any) -> str | None:
    if type(value) is not str or value not in GMAIL_CREDENTIAL_ROUTES:
        return "the credential route is not a recognized route"
    return None


@dataclass(frozen=True, slots=True)
class GmailIdentityBinding:
    """The account Core EXPECTS a Gmail call to act as.

    CORE-OWNED and NON-SECRET, established at composition from configuration
    exactly like ``DestinationGrant``: two identifiers and no credential. A
    connector's return value, a manifest, a task, a model answer, mail content
    and the process environment are all untrusted and none of them can reach
    this object; a connector may OBSERVE which account its session belongs
    to, and the observation is only ever compared against this.
    """

    principal: str
    credential_route: str = GMAIL_CREDENTIAL_ROUTE
    #: The JARVIS-dedicated Google Workspace CLI configuration directory the
    #: credential must come from. Part of the binding because it is part of
    #: the route: the seam pins the CLI to this directory and refuses any
    #: credential that does not live encrypted inside it.
    config_dir: str = ""


GMAIL_CONFIG_DIR_MAX_CHARS = 1_024


def gmail_config_dir_refusal(value: Any) -> str | None:
    """Why this is not a usable dedicated configuration directory, or ``None``.

    Lexical only: an absolute, bounded, control-character-free path. Whether
    it holds the dedicated credential is the seam's question, asked of the
    CLI itself before every call.
    """
    reason = _text_refusal(value, "configuration directory", GMAIL_CONFIG_DIR_MAX_CHARS, ascii_only=False)
    if reason is not None:
        return reason
    if not os.path.isabs(value):
        return "the configuration directory is not an absolute path"
    return None


def gmail_identity_binding_refusal(binding: Any) -> str | None:
    if type(binding) is not GmailIdentityBinding:
        return "no Core-owned Gmail identity binding was supplied"
    reason = gmail_principal_refusal(binding.principal)
    if reason is not None:
        return reason
    reason = gmail_credential_route_refusal(binding.credential_route)
    if reason is not None:
        return reason
    return gmail_config_dir_refusal(binding.config_dir)


def gmail_identity_refusal(
    binding: Any, *, observed_principal: Any, observed_credential_route: Any
) -> str | None:
    """Why the OBSERVED identity is not the one Core expects, or ``None``."""
    reason = gmail_identity_binding_refusal(binding)
    if reason is not None:
        return reason
    reason = gmail_address_refusal(observed_principal, "observed principal")
    if reason is not None:
        return reason
    reason = gmail_credential_route_refusal(observed_credential_route)
    if reason is not None:
        return f"the observed {reason.removeprefix('the ')}"
    if observed_principal.lower() != binding.principal:
        return "the observed principal is not the principal Core expects"
    if observed_credential_route != binding.credential_route:
        return "the observed credential route is not the route Core expects"
    return None


# --------------------------------------------------------------------------
# The outbound message - deterministic bytes an approval binds
# --------------------------------------------------------------------------

#: Encoded-word input chunk: 11 characters of up to 4 UTF-8 bytes each keeps
#: every ``=?utf-8?b?...?=`` word inside RFC 2047's 75-character limit.
_ENCODED_WORD_CHARS = 11


def _header_text(value: str) -> str:
    """One header value: ASCII verbatim, otherwise RFC 2047 encoded words."""
    if value.isascii():
        return value
    words = []
    for start in range(0, len(value), _ENCODED_WORD_CHARS):
        chunk = value[start : start + _ENCODED_WORD_CHARS].encode("utf-8")
        words.append("=?utf-8?b?" + base64.b64encode(chunk).decode("ascii") + "?=")
    return "\r\n ".join(words)


def gmail_outbound_message(resource: GmailResource) -> bytes:
    """The exact RFC 2822 bytes a draft or send carries. DETERMINISTIC.

    Built from the typed fields and nothing else: the sender is the resource's
    own bound account, the recipients are the validated lists, the subject is
    verbatim or encoded, the body is base64 plain text. No date, no message
    id and no boundary is generated here - Gmail assigns those - so the same
    resource always produces the same bytes and therefore the same digest.
    """
    reason = gmail_resource_refusal(resource)
    if reason is not None or resource.kind not in GMAIL_WRITE_KINDS:
        raise GmailExecutionUnavailable(reason or "the resource is not an outbound message")
    lines = [f"From: {resource.account}", "To: " + ", ".join(resource.to)]
    if resource.cc:
        lines.append("Cc: " + ", ".join(resource.cc))
    if resource.bcc:
        lines.append("Bcc: " + ", ".join(resource.bcc))
    lines += [
        "Subject: " + _header_text(resource.subject),
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: base64",
        "",
    ]
    encoded = base64.b64encode(resource.body.encode("utf-8")).decode("ascii")
    lines += [encoded[start : start + 76] for start in range(0, len(encoded), 76)]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


# --------------------------------------------------------------------------
# The canonical request frame
# --------------------------------------------------------------------------

#: Version tag of the frame. Bumping it invalidates every outstanding Gmail
#: approval, which is the point.
GMAIL_REQUEST_FRAME_HEADER = b"jarvis.gmail_request.v1"
GMAIL_EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

GMAIL_READ_OPERATION = "read"
GMAIL_DRAFT_OPERATION = "draft"
GMAIL_SEND_OPERATION = "send"

#: Capability -> the ONE operation label its frame carries. The label is
#: part of what the approval digest binds, so a draft approval can never be
#: spent on a send of the same bytes.
GMAIL_CAPABILITY_OPERATION: Mapping[str, str] = {
    **{capability: GMAIL_READ_OPERATION for capability in GMAIL_READ_CAPABILITIES},
    "gmail_draft_create": GMAIL_DRAFT_OPERATION,
    "gmail_send": GMAIL_SEND_OPERATION,
}


@dataclass(frozen=True, slots=True)
class GmailRequestFrame:
    """Everything one Gmail approval authorizes, and nothing else."""

    connector: str
    capability: str
    destination: str
    resource: GmailResource
    principal: str
    credential_route: str
    operation: str
    body_sha256: str = GMAIL_EMPTY_BODY_SHA256

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
        body = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return GMAIL_REQUEST_FRAME_HEADER + b"\n" + body

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def gmail_frame_refusal(frame: Any) -> str | None:
    """Why this frame is not the one this build would derive, or ``None``.

    Re-derivation, never provenance: the resource must bind to the capability
    and the destination, the principal must be the resource's own account,
    the operation must be the capability's one label and the body digest must
    be the digest of the bytes ``gmail_outbound_message`` builds for that
    resource (or the empty digest for a read).
    """
    if type(frame) is not GmailRequestFrame:
        return "the frame is not a GmailRequestFrame"
    if frame.connector != GMAIL_CONNECTOR:
        return "the connector is not gmail-cli"
    reason = gmail_resource_binding_refusal(
        frame.resource, capability=frame.capability, destination=frame.destination
    )
    if reason is not None:
        return reason
    if frame.principal != frame.resource.account:
        return "the frame principal is not the resource account"
    reason = gmail_credential_route_refusal(frame.credential_route)
    if reason is not None:
        return reason
    if frame.operation != GMAIL_CAPABILITY_OPERATION.get(frame.capability):
        return "the operation is not the one this capability performs"
    expected = (
        hashlib.sha256(gmail_outbound_message(frame.resource)).hexdigest()
        if frame.resource.kind in GMAIL_WRITE_KINDS
        else GMAIL_EMPTY_BODY_SHA256
    )
    if frame.body_sha256 != expected:
        return "the body digest is not the digest of the message this resource builds"
    return None


def build_gmail_request_frame(
    *,
    capability: str,
    destination: str,
    resource: GmailResource,
    binding: Any,
    connector: str = GMAIL_CONNECTOR,
) -> GmailRequestFrame:
    """Validate every part and return the frame, or fail closed.

    The identity in the frame is Core's ``binding`` and it must equal the
    resource's account: a task cannot name a mailbox the build has no
    identity for, and the binding cannot be talked into another account by
    anything a task carries.
    """
    reason = gmail_identity_binding_refusal(binding)
    if reason is None:
        reason = gmail_resource_binding_refusal(
            resource, capability=capability, destination=destination
        )
    if reason is None and binding.principal != resource.account:
        reason = "the resource account is not the principal Core expects"
    if reason is None and capability not in GMAIL_CAPABILITY_OPERATION:
        reason = "no Gmail operation exists for this capability in this build"
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    body = gmail_outbound_message(resource) if resource.kind in GMAIL_WRITE_KINDS else b""
    frame = GmailRequestFrame(
        connector=connector,
        capability=capability,
        destination=destination,
        resource=resource,
        principal=binding.principal,
        credential_route=binding.credential_route,
        operation=GMAIL_CAPABILITY_OPERATION[capability],
        body_sha256=hashlib.sha256(body).hexdigest(),
    )
    reason = gmail_frame_refusal(frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return frame


# --------------------------------------------------------------------------
# Environment and credential hygiene
# --------------------------------------------------------------------------

#: Variable name families removed whole from every child environment: the
#: Google CLI/SDK configuration surfaces that could select a credential, a
#: project or an endpoint.
GMAIL_REMOVED_ENVIRONMENT_PREFIXES: tuple[str, ...] = (
    "GOOGLE_",
    "GWS_",
    "CLOUDSDK_",
    "GCLOUD_",
    "OAUTH_",
    "GMAIL_",
)

GMAIL_REMOVED_ENVIRONMENT: frozenset[str] = frozenset(
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

GMAIL_FORCED_ENVIRONMENT: Mapping[str, str] = {"NO_COLOR": "1", "CI": "1"}

#: The two variables that PIN the CLI to the dedicated route, set explicitly
#: in every child environment after the removals. dotenvy - the CLI's
#: ``.env`` support - never overrides a variable that is already set, so a
#: side-loaded file cannot move the config directory or the key storage.
GMAIL_CONFIG_DIR_VARIABLE = "GOOGLE_WORKSPACE_CLI_CONFIG_DIR"
GMAIL_KEYRING_BACKEND_VARIABLE = "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"
GMAIL_KEYRING_BACKEND = "keyring"


def sanitized_gmail_environment(
    environ: Mapping[str, str], *, config_dir: str
) -> dict[str, str]:
    """The child environment: removals first, forced values last, then the
    dedicated config directory and the keyring backend pinned. Pure."""
    reason = gmail_config_dir_refusal(config_dir)
    if reason is not None:
        raise GmailExecutionUnavailable(reason)
    child = {
        str(name): str(value)
        for name, value in environ.items()
        if str(name).upper() not in GMAIL_REMOVED_ENVIRONMENT
        and not str(name).upper().startswith(GMAIL_REMOVED_ENVIRONMENT_PREFIXES)
    }
    child.update(GMAIL_FORCED_ENVIRONMENT)
    child[GMAIL_CONFIG_DIR_VARIABLE] = config_dir
    child[GMAIL_KEYRING_BACKEND_VARIABLE] = GMAIL_KEYRING_BACKEND
    return child


# --------------------------------------------------------------------------
# The closed operation plan - data, checked by re-derivation
# --------------------------------------------------------------------------

#: Bounds on one derived argument and on how many there may be. No argument
#: ever carries message content: a write names a transport FILE, so the
#: vector is short and the bound is tight.
GWS_ARGUMENT_MAX_CHARS = 1_024
GWS_MAX_ARGUMENTS = 12

#: The write transport. The RFC 2822 bytes live in a file of this name inside
#: a unique JARVIS-owned directory that is the child's working directory, and
#: the CLI uploads it as ``message/rfc822`` through the multipart upload
#: endpoint. The command line therefore names the file and nothing of what
#: is in it. The name is a constant so the vector stays derivable from the
#: frame alone and re-provable without knowing the directory.
GMAIL_UPLOAD_NAME = "message.eml"
GMAIL_UPLOAD_CONTENT_TYPE = "message/rfc822"

#: The CLI's local rehearsal flag: it validates the request and reports what
#: WOULD be sent, without sending. A write is rehearsed under its exact
#: vector before the real call.
GWS_DRY_RUN_FLAG = "--dry-run"

#: The constant vector that reports the CLI's authentication state.
GMAIL_AUTH_STATUS_ARGS: tuple[str, ...] = ("auth", "status")

#: The one API family every derived vector names. A capability chooses a
#: resource and a method; it cannot choose another API.
GWS_API_ARGS: tuple[str, ...] = ("gmail", "users")


def _params(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


#: Capability -> the ONE argument vector that serves it. Every row names the
#: bound account as ``userId`` - never ``me`` - so the API refuses to serve
#: the request under any other account's credential. There is no default
#: row, no fallback and no builder that takes a method as data.
_GWS_ARGV_BUILDERS: Mapping[str, Any] = {
    "gmail_thread_list": lambda r: (
        *GWS_API_ARGS, "threads", "list",
        "--params", _params(userId=r.account, maxResults=r.limit),
    ),
    "gmail_search": lambda r: (
        *GWS_API_ARGS, "threads", "list",
        "--params", _params(userId=r.account, maxResults=r.limit, q=r.query),
    ),
    "gmail_message_read": lambda r: (
        *GWS_API_ARGS, "messages", "get",
        "--params", _params(userId=r.account, id=r.message_id, format="full"),
    ),
    "gmail_thread_read": lambda r: (
        *GWS_API_ARGS, "threads", "get",
        "--params", _params(userId=r.account, id=r.thread_id, format="full"),
    ),
    # The two writes name the transport file, never the message: the bytes
    # are derived from the same validated resource by the seam and written
    # to that file in the child's working directory.
    "gmail_draft_create": lambda r: (
        *GWS_API_ARGS, "drafts", "create",
        "--params", _params(userId=r.account),
        "--upload", GMAIL_UPLOAD_NAME,
        "--upload-content-type", GMAIL_UPLOAD_CONTENT_TYPE,
    ),
    "gmail_send": lambda r: (
        *GWS_API_ARGS, "messages", "send",
        "--params", _params(userId=r.account),
        "--upload", GMAIL_UPLOAD_NAME,
        "--upload-content-type", GMAIL_UPLOAD_CONTENT_TYPE,
    ),
}


@dataclass(frozen=True, slots=True)
class GmailOperationPlan:
    """The argument vector one validated operation WOULD use. DATA."""

    capability: str
    destination: str
    resource: GmailResource
    arguments: tuple[str, ...]

    authorizes_execution: ClassVar[bool] = False

    @property
    def is_write(self) -> bool:
        return self.capability in GMAIL_WRITE_CAPABILITIES


def _derived_arguments(capability: str, resource: GmailResource) -> tuple[str, ...] | str:
    try:
        arguments = _GWS_ARGV_BUILDERS[capability](resource)
    except GmailExecutionUnavailable as exc:
        return str(exc)
    if len(arguments) > GWS_MAX_ARGUMENTS:
        return "the argument list is over the bound"
    for argument in arguments:
        if type(argument) is not str or not argument or len(argument) > GWS_ARGUMENT_MAX_CHARS:
            return "an argument is not bounded text"
        if not argument.isascii() or any(char < " " or char == "\x7f" for char in argument):
            return "an argument is not bounded ASCII text"
        if credential_bearing(argument):
            return "an argument carries a credential shape"
    return tuple(arguments)


def gmail_operation_plan_refusal(plan: Any) -> str | None:
    """Why this plan is not the closed plan it claims to be, or ``None``."""
    if type(plan) is not GmailOperationPlan:
        return "the plan is not a GmailOperationPlan"
    if type(plan.capability) is not str or plan.capability not in _GWS_ARGV_BUILDERS:
        return "the plan names no closed Gmail operation"
    reason = gmail_resource_binding_refusal(
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


def gmail_operation_plan(frame: Any) -> GmailOperationPlan:
    """The closed plan for one validated frame, or fail closed."""
    reason = gmail_frame_refusal(frame)
    if reason is not None:
        raise GmailExecutionUnavailable(reason)
    if frame.capability not in _GWS_ARGV_BUILDERS:
        raise GmailExecutionUnavailable(
            "no Gmail operation exists for this capability in this build"
        )
    derived = _derived_arguments(frame.capability, frame.resource)
    if type(derived) is str:
        raise GmailExecutionUnavailable(derived)
    plan = GmailOperationPlan(
        capability=frame.capability,
        destination=frame.destination,
        resource=frame.resource,
        arguments=derived,
    )
    reason = gmail_operation_plan_refusal(plan)
    if reason is not None:  # pragma: no cover - the derivation just built it
        raise GmailExecutionUnavailable(reason)
    return plan


# --------------------------------------------------------------------------
# The one credential route, as the CLI reports it
# --------------------------------------------------------------------------
#
# ``gws auth status`` (0.22.5) prints one JSON object: ``auth_method`` (none |
# oauth2), ``credential_source`` (none, or the environment source in use:
# token_env_var / environment_variables), ``storage`` (none | plaintext |
# encrypted), ``keyring_backend`` (keyring | file), the paths of the
# encrypted and plaintext credential files and of the OAuth client
# configuration with an ``_exists`` flag each, ``token_cache_exists`` and,
# when relevant, ``encryption_valid`` / ``encryption_error`` / ``token_error``.
# Application Default Credentials do not appear in it at all - which is why
# the dedicated credentials are REQUIRED to exist: the CLI consults ADC only
# when its config directory holds none, and the env-named ADC route is
# stripped from the child environment.

GMAIL_ENCRYPTED_CREDENTIALS_NAME = "credentials.enc"
GMAIL_CLIENT_CONFIG_NAME = "client_secret.json"


def _same_path(reported: Any, expected: str) -> bool:
    """Lexical path equality, case-insensitive on Windows. No filesystem."""
    if type(reported) is not str or not reported:
        return False
    text = reported[4:] if reported.startswith("\\\\?\\") else reported
    return os.path.normcase(os.path.normpath(text)) == os.path.normcase(
        os.path.normpath(expected)
    )


def gmail_route_refusal(payload: Any, *, config_dir: str) -> str | None:
    """Why the CLI's reported authentication state is not the ONE allowed
    route, or ``None``.

    Every clause is a route this build refuses by name: a token or client
    from the environment, a plaintext credential file (the CLI's own or one
    named by the environment), no stored credential at all (which would
    hand the call to ADC or a service account), a file-backed encryption
    key, a missing OAuth client configuration, an unusable credential, and
    any file that lives outside the dedicated directory.
    """
    if type(payload) is not dict:
        return "the authentication state is not one JSON object"
    if payload.get("auth_method") != "oauth2":
        return "the session is not OAuth2 user credentials"
    if payload.get("credential_source") != "none":
        return "the credential comes from the environment, not the dedicated directory"
    if payload.get("storage") != "encrypted":
        return "the credential is not stored encrypted"
    if payload.get("keyring_backend") != GMAIL_KEYRING_BACKEND:
        return "the encryption key is not held by the OS keyring"
    if payload.get("encrypted_credentials_exists") is not True:
        return "no encrypted credential exists in the dedicated directory"
    if payload.get("plain_credentials_exists") is not False:
        return "a plaintext credential file is present"
    if payload.get("client_config_exists") is not True:
        return "the OAuth client configuration is not in the dedicated directory"
    if payload.get("encryption_valid") is False or "encryption_error" in payload:
        return "the stored credential cannot be decrypted on this machine"
    if "token_error" in payload:
        return "the stored credential is not usable"
    if not _same_path(
        payload.get("encrypted_credentials"),
        os.path.join(config_dir, GMAIL_ENCRYPTED_CREDENTIALS_NAME),
    ):
        return "the encrypted credential is not the dedicated directory's"
    if not _same_path(
        payload.get("client_config"), os.path.join(config_dir, GMAIL_CLIENT_CONFIG_NAME)
    ):
        return "the OAuth client configuration is not the dedicated directory's"
    return None


GMAIL_UPLOAD_URL_PREFIX = "https://gmail.googleapis.com/upload/gmail/v1/users/"


def gmail_dry_run_refusal(payload: Any) -> str | None:
    """Why the CLI's local rehearsal does not describe the write this build
    performs, or ``None``: a POST to the Gmail UPLOAD endpoint, carried as a
    multipart upload, with NO JSON body - so the message travels as the
    file and nothing about it is on the command line or in a body."""
    if type(payload) is not dict:
        return "the rehearsal is not one JSON object"
    if payload.get("dry_run") is not True:
        return "the rehearsal did not run as a dry run"
    if payload.get("method") != "POST":
        return "the rehearsal is not a POST"
    if payload.get("is_multipart_upload") is not True:
        return "the rehearsal is not a multipart upload"
    if payload.get("body") is not None:
        return "the rehearsal carries a request body"
    url = payload.get("url")
    if type(url) is not str or not url.startswith(GMAIL_UPLOAD_URL_PREFIX):
        return "the rehearsal does not target the Gmail upload endpoint"
    return None


# --------------------------------------------------------------------------
# Responses - untrusted content, bounded and bound to the request
# --------------------------------------------------------------------------

#: Bounds on what one rendered message may carry into the snapshot. Small on
#: purpose: the snapshot ceiling is shared with every external read, and a
#: thread should show several messages rather than one message's whole body.
GMAIL_HEADER_MAX_CHARS = 512
GMAIL_SNIPPET_MAX_CHARS = 200
GMAIL_BODY_TEXT_MAX_CHARS = 1_500
GMAIL_THREAD_MESSAGES_MAX = 10
GMAIL_ATTACHMENTS_MAX = 20
_RENDERED_HEADERS = ("From", "To", "Cc", "Date", "Subject")


def _clean_text(value: Any, cap: int) -> str:
    """A bounded, control-character-free rendering of one untrusted string."""
    if type(value) is not str:
        return ""
    cleaned = "".join(char if char >= " " and char != "\x7f" else " " for char in value)
    return cleaned[:cap]


def _decode_body_data(data: Any) -> str:
    if type(data) is not str or not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""


def _walk_parts(part: Any, texts: list[str], attachments: list[dict[str, Any]]) -> None:
    if type(part) is not dict:
        return
    mime_type = _clean_text(part.get("mimeType"), GMAIL_HEADER_MAX_CHARS)
    filename = _clean_text(part.get("filename"), GMAIL_HEADER_MAX_CHARS)
    body = part.get("body") if type(part.get("body")) is dict else {}
    if filename:
        if len(attachments) < GMAIL_ATTACHMENTS_MAX:
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": body.get("size") if type(body.get("size")) is int else 0,
                    "attachment_id": _clean_text(body.get("attachmentId"), GMAIL_ID_MAX_CHARS),
                }
            )
    elif mime_type == "text/plain" and not texts:
        texts.append(_decode_body_data(body.get("data")))
    for child in part.get("parts") or ():
        _walk_parts(child, texts, attachments)


def _render_message(payload: Any) -> dict[str, Any]:
    """One bounded, untrusted view of a message resource."""
    if type(payload) is not dict:
        return {}
    headers: dict[str, str] = {}
    root = payload.get("payload") if type(payload.get("payload")) is dict else {}
    for header in root.get("headers") or ():
        if type(header) is not dict:
            continue
        name = header.get("name")
        if type(name) is str and name.title() in _RENDERED_HEADERS:
            headers[name.lower()] = _clean_text(header.get("value"), GMAIL_HEADER_MAX_CHARS)
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    _walk_parts(root, texts, attachments)
    body_text = texts[0] if texts else ""
    return {
        "id": _clean_text(payload.get("id"), GMAIL_ID_MAX_CHARS),
        "thread_id": _clean_text(payload.get("threadId"), GMAIL_ID_MAX_CHARS),
        "label_ids": [
            _clean_text(label, GMAIL_ID_MAX_CHARS)
            for label in (payload.get("labelIds") or ())[:GMAIL_ATTACHMENTS_MAX]
            if type(label) is str
        ],
        "headers": headers,
        "snippet": _clean_text(payload.get("snippet"), GMAIL_SNIPPET_MAX_CHARS),
        "body_text": body_text[:GMAIL_BODY_TEXT_MAX_CHARS],
        "body_truncated": len(body_text) > GMAIL_BODY_TEXT_MAX_CHARS,
        "attachments": attachments,
    }


def _one_json_object(stdout: Any, what: str) -> dict[str, Any]:
    if type(stdout) is not str or not stdout.strip():
        raise GmailExecutionUnavailable(f"the Gmail {what} returned no response")
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise GmailExecutionUnavailable(
            f"the Gmail {what} did not return one JSON response"
        ) from exc
    if type(payload) is not dict:
        raise GmailExecutionUnavailable(f"the Gmail {what} did not return one JSON response")
    if "error" in payload:
        # Nothing from the error reaches the refusal: it can carry addresses,
        # ids and quota details.
        raise GmailExecutionUnavailable(f"the Gmail API refused the {what}")
    return payload


def gmail_read_content(stdout: Any, *, frame: Any) -> str:
    """The bounded, untrusted content of ONE read, bound to its request.

    A message or thread response must carry the exact id the frame asked for;
    a listing is rendered as ids and snippets only. Everything is cleaned,
    bounded and serialized deterministically, and none of it is trusted for
    anything downstream.
    """
    if type(frame) is not GmailRequestFrame or frame.capability not in GMAIL_READ_CAPABILITIES:
        raise GmailExecutionUnavailable("the read names no validated request")
    payload = _one_json_object(stdout, "read")
    resource = frame.resource
    if resource.kind in (GmailResourceKind.THREAD_LIST, GmailResourceKind.SEARCH):
        threads = payload.get("threads") or ()
        if type(threads) is not list:
            raise GmailExecutionUnavailable("the Gmail listing is not a list")
        rendered: dict[str, Any] = {
            "threads": [
                {
                    "id": _clean_text(thread.get("id"), GMAIL_ID_MAX_CHARS),
                    "snippet": _clean_text(thread.get("snippet"), GMAIL_SNIPPET_MAX_CHARS),
                }
                for thread in threads[: resource.limit]
                if type(thread) is dict
            ],
            "result_size_estimate": (
                payload["resultSizeEstimate"]
                if type(payload.get("resultSizeEstimate")) is int
                else 0
            ),
        }
    elif resource.kind is GmailResourceKind.MESSAGE:
        if payload.get("id") != resource.message_id:
            raise GmailExecutionUnavailable(
                "the Gmail read returned a different message than requested"
            )
        rendered = _render_message(payload)
    elif resource.kind is GmailResourceKind.THREAD:
        if payload.get("id") != resource.thread_id:
            raise GmailExecutionUnavailable(
                "the Gmail read returned a different thread than requested"
            )
        messages = payload.get("messages") or ()
        if type(messages) is not list:
            raise GmailExecutionUnavailable("the Gmail thread carries no message list")
        rendered = {
            "id": resource.thread_id,
            "message_count": len(messages),
            "messages": [
                _render_message(message) for message in messages[:GMAIL_THREAD_MESSAGES_MAX]
            ],
        }
    else:  # pragma: no cover - every read kind is handled above
        raise GmailExecutionUnavailable("the read names no renderable kind")
    return json.dumps(rendered, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class GmailWriteOutcome:
    """What the API acknowledged for one draft or send. Identifiers only."""

    operation: str
    message_id: str
    thread_id: str
    draft_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "operation": self.operation,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "draft_id": self.draft_id,
        }


def gmail_write_outcome(stdout: Any, *, frame: Any) -> GmailWriteOutcome:
    """The acknowledged identifiers of ONE write, or ``GmailOutcomeUnknown``.

    The process exited successfully, so the request reached the API. A body
    that cannot be read as the acknowledgement is therefore NOT a failure:
    the draft or the sent message may exist, and saying "nothing happened"
    would invite a retry that sends it twice.
    """
    if type(frame) is not GmailRequestFrame or frame.capability not in GMAIL_WRITE_CAPABILITIES:
        raise GmailExecutionUnavailable("the write names no validated request")
    try:
        payload = _one_json_object(stdout, frame.operation)
        if frame.operation == GMAIL_DRAFT_OPERATION:
            draft_id = payload.get("id")
            message = payload.get("message") if type(payload.get("message")) is dict else {}
        else:
            draft_id = ""
            message = payload
        message_id = message.get("id")
        thread_id = message.get("threadId")
        for value, label in ((message_id, "message id"), (thread_id, "thread id")):
            if gmail_id_refusal(value, label) is not None:
                raise GmailExecutionUnavailable(f"the acknowledgement carries no {label}")
        if frame.operation == GMAIL_DRAFT_OPERATION and gmail_id_refusal(draft_id) is not None:
            raise GmailExecutionUnavailable("the acknowledgement carries no draft id")
    except GmailExecutionUnavailable as exc:
        raise GmailOutcomeUnknown(
            f"the Gmail {frame.operation} may have happened but was not acknowledged: {exc}"
        ) from exc
    return GmailWriteOutcome(
        operation=frame.operation,
        message_id=message_id,
        thread_id=thread_id,
        draft_id=draft_id or "",
    )


# --------------------------------------------------------------------------
# The external read snapshot - the shared transport, Gmail's vocabulary
# --------------------------------------------------------------------------


def gmail_external_read_refusal(snapshot: Any) -> str | None:
    """Why this snapshot is not usable Gmail reference data, or ``None``."""
    reason = external_read_shape_refusal(snapshot)
    if reason is not None:
        return reason
    if snapshot.connector != GMAIL_CONNECTOR:
        return "the snapshot names no known producer"
    if snapshot.capability not in GMAIL_READ_CAPABILITIES:
        return "the snapshot capability is not a Gmail read capability"
    if not is_gmail_destination(snapshot.destination):
        return "the snapshot destination is not a canonical mailbox"
    if snapshot.resource_kind not in {kind.value for kind in GmailResourceKind}:
        return "the snapshot resource kind is not a recognized kind"
    return None


def gmail_external_read_binding_refusal(snapshot: Any, *, frame: Any) -> str | None:
    """Why this snapshot does not describe THIS validated request, or ``None``."""
    if type(frame) is not GmailRequestFrame:
        return "the snapshot names no validated request"
    reason = gmail_external_read_refusal(snapshot)
    if reason is not None:
        return reason
    reason = gmail_frame_refusal(frame)
    if reason is not None:
        return reason
    if snapshot.connector != frame.connector:
        return "the snapshot connector is not the requesting connector"
    if snapshot.capability != frame.capability:
        return "the snapshot capability is not the requested capability"
    if snapshot.destination != frame.destination:
        return "the snapshot mailbox is not the requested mailbox"
    if snapshot.resource_kind != frame.resource.kind.value:
        return "the snapshot resource kind is not the requested kind"
    if snapshot.resource_id != frame.resource.identity:
        return "the snapshot resource is not the requested resource"
    return None


def build_gmail_read_snapshot(
    *,
    source_task_id: str,
    frame: Any,
    content: str,
    captured_at: str,
    truncated: bool = False,
) -> ExternalReadSnapshot:
    """The snapshot for one validated read and the content it returned.

    Every identity field is DERIVED from the validated frame and the digest
    and size from the content itself; the result is still untrusted data.
    """
    if type(frame) is not GmailRequestFrame or frame.capability not in GMAIL_READ_CAPABILITIES:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, "the snapshot names no validated request")
    if type(content) is not str or len(content) > EXTERNAL_READ_MAX_CHARS:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, "the snapshot content is not bounded text")
    if type(truncated) is not bool or type(source_task_id) is not str or not source_task_id:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, "the snapshot names no source task")
    if canonical_utc_timestamp(captured_at) is None:
        raise AttestationError(
            ErrorCode.EGRESS_REFUSED, "the capture time is not a canonical UTC timestamp"
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
    reason = gmail_external_read_binding_refusal(snapshot, frame=frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return snapshot


__all__ = [
    "GMAIL_AUTH_STATUS_ARGS",
    "GMAIL_BILLING_CLASS",
    "GMAIL_BODY_MAX_BYTES",
    "GMAIL_CLIENT_CONFIG_NAME",
    "GMAIL_CONFIG_DIR_MAX_CHARS",
    "GMAIL_CONFIG_DIR_VARIABLE",
    "GMAIL_ENCRYPTED_CREDENTIALS_NAME",
    "GMAIL_KEYRING_BACKEND",
    "GMAIL_KEYRING_BACKEND_VARIABLE",
    "GMAIL_UPLOAD_CONTENT_TYPE",
    "GMAIL_UPLOAD_NAME",
    "GMAIL_UPLOAD_URL_PREFIX",
    "GWS_DRY_RUN_FLAG",
    "GMAIL_CAPABILITIES",
    "GMAIL_CAPABILITY_OPERATION",
    "GMAIL_CAPABILITY_RESOURCE",
    "GMAIL_CONNECTOR",
    "GMAIL_CREDENTIAL_ROUTE",
    "GMAIL_CREDENTIAL_ROUTES",
    "GMAIL_DEFERRED_CAPABILITIES",
    "GMAIL_DESTINATION_PREFIX",
    "GMAIL_DRAFT_OPERATION",
    "GMAIL_ENABLED_CAPABILITIES",
    "GMAIL_FORCED_ENVIRONMENT",
    "GMAIL_LIST_MAX_ITEMS",
    "GMAIL_MONETARY_STATE",
    "GMAIL_QUERY_MAX_CHARS",
    "GMAIL_READ_CAPABILITIES",
    "GMAIL_READ_OPERATION",
    "GMAIL_RECIPIENTS_MAX",
    "GMAIL_REMOVED_ENVIRONMENT",
    "GMAIL_REMOVED_ENVIRONMENT_PREFIXES",
    "GMAIL_REQUEST_FRAME_HEADER",
    "GMAIL_SEND_OPERATION",
    "GMAIL_SUBJECT_MAX_CHARS",
    "GMAIL_WRITE_CAPABILITIES",
    "GMAIL_WRITE_KINDS",
    "GWS_API_ARGS",
    "GWS_ARGUMENT_MAX_CHARS",
    "GWS_MAX_ARGUMENTS",
    "GmailExecutionUnavailable",
    "GmailIdentityBinding",
    "GmailMailbox",
    "GmailOperationPlan",
    "GmailOutcomeUnknown",
    "GmailRequestFrame",
    "GmailResource",
    "GmailResourceKind",
    "GmailWriteOutcome",
    "build_gmail_read_snapshot",
    "build_gmail_request_frame",
    "gmail_address_refusal",
    "gmail_body_refusal",
    "gmail_config_dir_refusal",
    "gmail_dry_run_refusal",
    "gmail_credential_route_refusal",
    "gmail_destination",
    "gmail_destination_refusal",
    "gmail_external_read_binding_refusal",
    "gmail_external_read_refusal",
    "gmail_frame_refusal",
    "gmail_id_refusal",
    "gmail_identity_binding_refusal",
    "gmail_identity_refusal",
    "gmail_operation_plan",
    "gmail_operation_plan_refusal",
    "gmail_outbound_message",
    "gmail_principal_refusal",
    "gmail_query_refusal",
    "gmail_raw_destination_refusal",
    "gmail_read_content",
    "gmail_resource_binding_refusal",
    "gmail_resource_from_dict",
    "gmail_resource_refusal",
    "gmail_route_refusal",
    "gmail_subject_refusal",
    "gmail_write_outcome",
    "is_gmail_destination",
    "sanitized_gmail_environment",
]
