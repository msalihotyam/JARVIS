"""Browser basic usable-v1 - the closed vocabulary, grammar, frame and plan.

The same shape as ``jarvis.github`` and ``jarvis.gmail``, on purpose: one
authorized web origin is one canonical destination ``browser/<origin>``, a
task names a TYPED resource inside it and nothing else, an approval binds one
canonical request frame, and a read returns an UNTRUSTED bounded snapshot
through the external-read transport ``jarvis.github`` already defines.
Nothing here opens a socket or starts a process; the process seam is
``jarvis.browser_cdp`` and the connector is
``jarvis.connectors.browser_cdp``.

WHAT A TASK MAY NAME. A URL - only for the two capabilities that navigate,
and only one that is INSIDE the bound origin. A tab, by its opaque id. A
target element, by a ``ref`` that a previous read printed, together with the
digest of the page state that read saw. Never a selector, never a script,
never an argv, never a coordinate. A model cannot author a target: it can
only name one JARVIS itself already rendered and refused to make ambiguous.

WHY THE DIGEST. An approved click is an approval to click THAT element on
THAT page. Between the read and the click the page can navigate, re-render
or replace the element under the same position. So every interaction carries
``page_digest`` - the digest of the exact bounded rendering the read
produced - and the connector re-reads the live page and refuses unless the
fresh rendering is byte-identical. A page that moved is a page the approval
was never for.

WHAT IS STRUCTURALLY ABSENT. There is no capability that evaluates script,
solves a challenge, reads a credential store, uploads or downloads a file,
or repairs a target that no longer matches. Those names do not exist in the
vocabulary, so no task can ask for them.

Core, not a connector, is the authority. Everything here is a canonicalizer
or a refusal; none of it authorizes anything. An origin becomes reachable
only when the composition root turned a configured origin into exact
``DestinationGrant`` rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Mapping
from urllib.parse import urlsplit

from jarvis.contracts import (
    ORIGIN_DESTINATION_PREFIX,
    AttestationError,
    BillingClass,
    DataClassification,
    ErrorCode,
    MonetaryState,
    canonical_utc_timestamp,
    credential_bearing,
    redact_credentials,
)
from jarvis.github import (
    EXTERNAL_READ_MAX_CHARS,
    ExternalReadSnapshot,
    external_read_shape_refusal,
)

# --------------------------------------------------------------------------
# Closed capability vocabulary and billing
# --------------------------------------------------------------------------

#: The one registered connector name a browser grant may ever name.
BROWSER_CONNECTOR = "browser-cdp"

#: Re-exported from the central normalizer, which must refuse to REPAIR this
#: namespace, exactly as it does for repositories and mailboxes.
BROWSER_DESTINATION_PREFIX = ORIGIN_DESTINATION_PREFIX

#: Capabilities whose whole effect is to observe. Each returns one bounded
#: UNTRUSTED snapshot and changes nothing.
BROWSER_READ_CAPABILITIES: tuple[str, ...] = (
    "browser_page_read",
    "browser_tab_list",
    "browser_wait",
)

#: Capabilities that act on the browser. Every one is treated as irreversible:
#: a click can submit, a navigation can be a request the site acts on, a tab
#: switch decides which page the NEXT approved action lands on, and a close
#: destroys page state. None of them can be undone by this product.
BROWSER_INTERACT_CAPABILITIES: tuple[str, ...] = (
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_tab_open",
    "browser_tab_switch",
    "browser_tab_close",
)

#: Named so they are refused BY NAME rather than by silence. usable-v1 ships
#: none of them and no code path can reach one.
BROWSER_DEFERRED_CAPABILITIES: tuple[str, ...] = (
    "browser_script_evaluate",
    "browser_download",
    "browser_upload",
    "browser_screenshot",
    "browser_cookie_read",
)

BROWSER_ENABLED_CAPABILITIES: tuple[str, ...] = (
    BROWSER_READ_CAPABILITIES + BROWSER_INTERACT_CAPABILITIES
)

BROWSER_CAPABILITIES: tuple[str, ...] = (
    BROWSER_ENABLED_CAPABILITIES + BROWSER_DEFERRED_CAPABILITIES
)

#: The local browser is the operator's own already-installed program. No
#: metered call, no provider account, no charge.
BROWSER_BILLING_CLASS = BillingClass.NO_ADDITIONAL_CHARGE
BROWSER_MONETARY_STATE = MonetaryState.NOT_APPLICABLE


# --------------------------------------------------------------------------
# The origin grammar - a closed spelling, never a repair
# --------------------------------------------------------------------------

#: Only the two web schemes. ``file:``, ``data:``, ``about:``, ``chrome:`` and
#: every extension scheme are outside the grammar, so no grant can name one.
BROWSER_SCHEMES: frozenset[str] = frozenset({"http", "https"})

BROWSER_ORIGIN_MAX_CHARS = 255
BROWSER_URL_MAX_CHARS = 2_048
#: One typed entry. Long enough for a real form field, short enough that it
#: can never be a payload.
BROWSER_TEXT_MAX_CHARS = 2_000
BROWSER_WAIT_MIN_SECONDS = 1
BROWSER_WAIT_MAX_SECONDS = 30
#: How many accessibility nodes one page rendering may print. The character
#: ceiling usually binds first; this refuses an absurd page outright.
BROWSER_MAX_NODES = 200
#: How many tabs one listing may print.
BROWSER_MAX_TABS = 25

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_ORIGIN_PATTERN = re.compile(
    rf"\A(https?)://({_HOST_LABEL}(?:\.{_HOST_LABEL})*)(?::([1-9][0-9]{{0,4}}))?\Z"
)
#: A ref is JARVIS's own label for a node it printed. Not a selector: it means
#: nothing outside the one rendering that produced it.
_REF_PATTERN = re.compile(r"\Ar[1-9][0-9]{0,3}\Z")
#: A DevTools target id, as the browser spells it.
_TAB_ID_PATTERN = re.compile(r"\A[0-9A-Fa-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


def browser_origin_refusal(value: Any) -> str | None:
    """Why this is not a canonical web origin, or ``None``."""
    if type(value) is not str:
        return "the origin is not a string"
    if not value or len(value) > BROWSER_ORIGIN_MAX_CHARS:
        return "the origin is not bounded text"
    match = _ORIGIN_PATTERN.fullmatch(value)
    if match is None:
        return "the origin is not spelled scheme://host[:port]"
    scheme, host, port = match.groups()
    if scheme not in BROWSER_SCHEMES:
        return "the origin does not name a web scheme"
    if value != f"{scheme}://{host}" + (f":{port}" if port else ""):
        return "the origin is not in canonical form"
    if host != host.lower():
        return "the origin host is not lower case"
    return None


@dataclass(frozen=True, slots=True)
class BrowserOrigin:
    origin: str


def browser_destination_refusal(requested: Any) -> str | None:
    """Why this is not a canonical ``browser/<origin>`` destination."""
    if type(requested) is not str:
        return "the destination is not a string"
    if not requested.startswith(BROWSER_DESTINATION_PREFIX):
        return "the destination is not in the browser namespace"
    return browser_origin_refusal(requested[len(BROWSER_DESTINATION_PREFIX) :])


def browser_destination(requested: Any) -> BrowserOrigin | None:
    if browser_destination_refusal(requested) is not None:
        return None
    return BrowserOrigin(origin=requested[len(BROWSER_DESTINATION_PREFIX) :])


def is_browser_destination(canonical: Any) -> bool:
    return browser_destination_refusal(canonical) is None


def browser_raw_destination_refusal(requested: Any) -> str | None:
    """The refusal Core's destination policy prints for this namespace.

    Judged on the spelling AS WRITTEN: ``normalize_destination`` hands a
    noncanonical ``browser/...`` through unrepaired precisely so a laundering
    attempt lands here and is named as one.
    """
    return browser_destination_refusal(requested)


def url_in_origin(url: Any, origin: str) -> bool:
    """Whether this exact URL belongs to this exact origin.

    Compared on the parsed scheme, host and port - never on a prefix. A prefix
    test would accept ``https://example.com.evil.test/`` for the origin
    ``https://example.com``.
    """
    if browser_url_refusal(url) is not None:
        return False
    parsed = urlsplit(url)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}" == origin


def browser_url_refusal(value: Any) -> str | None:
    """Why this is not a bounded, explicit, canonical web URL, or ``None``."""
    if type(value) is not str:
        return "the url is not a string"
    if not value or len(value) > BROWSER_URL_MAX_CHARS:
        return "the url is not bounded text"
    if not value.isascii() or any(char < " " or char == "\x7f" for char in value):
        return "the url is not bounded ASCII text"
    parsed = urlsplit(value)
    if parsed.scheme not in BROWSER_SCHEMES:
        return "the url does not name a web scheme"
    if parsed.username is not None or parsed.password is not None:
        return "the url carries embedded credentials"
    if not parsed.hostname or parsed.hostname != parsed.hostname.lower():
        return "the url names no canonical host"
    if parsed.fragment:
        return "the url carries a fragment"
    origin_reason = browser_origin_refusal(
        f"{parsed.scheme}://{parsed.hostname}"
        + (f":{parsed.port}" if parsed.port else "")
    )
    if origin_reason is not None:
        return f"the url names no canonical origin: {origin_reason}"
    if credential_bearing(value):
        return "the url carries a credential shape"
    return None


# --------------------------------------------------------------------------
# The typed resource - one target inside one origin
# --------------------------------------------------------------------------


class BrowserResourceKind(str, Enum):
    """What one browser operation acts on. One kind per capability."""

    PAGE = "page"
    TABS = "tabs"
    DELAY = "delay"
    NAVIGATION = "navigation"
    NEW_TAB = "new_tab"
    CLICK_TARGET = "click_target"
    TEXT_TARGET = "text_target"
    TAB_SELECTION = "tab_selection"
    TAB_CLOSURE = "tab_closure"


BROWSER_CAPABILITY_RESOURCE: Mapping[str, BrowserResourceKind] = {
    "browser_page_read": BrowserResourceKind.PAGE,
    "browser_tab_list": BrowserResourceKind.TABS,
    "browser_wait": BrowserResourceKind.DELAY,
    "browser_navigate": BrowserResourceKind.NAVIGATION,
    "browser_tab_open": BrowserResourceKind.NEW_TAB,
    "browser_click": BrowserResourceKind.CLICK_TARGET,
    "browser_type": BrowserResourceKind.TEXT_TARGET,
    "browser_tab_switch": BrowserResourceKind.TAB_SELECTION,
    "browser_tab_close": BrowserResourceKind.TAB_CLOSURE,
}

#: The kinds that bind an element on a page that was already read.
BROWSER_ELEMENT_KINDS = frozenset(
    {BrowserResourceKind.CLICK_TARGET, BrowserResourceKind.TEXT_TARGET}
)

#: Which optional fields each kind may carry. Everything else must be absent
#: at its own type's empty value - default-deny, per kind.
_RESOURCE_FIELDS: Mapping[BrowserResourceKind, frozenset[str]] = {
    BrowserResourceKind.PAGE: frozenset(),
    BrowserResourceKind.TABS: frozenset(),
    BrowserResourceKind.DELAY: frozenset({"seconds"}),
    BrowserResourceKind.NAVIGATION: frozenset({"url"}),
    BrowserResourceKind.NEW_TAB: frozenset({"url"}),
    BrowserResourceKind.CLICK_TARGET: frozenset({"ref", "page_digest"}),
    BrowserResourceKind.TEXT_TARGET: frozenset({"ref", "page_digest", "text"}),
    BrowserResourceKind.TAB_SELECTION: frozenset({"tab_id"}),
    BrowserResourceKind.TAB_CLOSURE: frozenset({"tab_id"}),
}

_RESOURCE_ABSENT: Mapping[str, Any] = {
    "url": "",
    "seconds": 0,
    "tab_id": "",
    "ref": "",
    "page_digest": "",
    "text": "",
}


def _ref_refusal(value: Any) -> str | None:
    if type(value) is not str or _REF_PATTERN.fullmatch(value) is None:
        return "the target ref is not one a page reading produced"
    return None


def _tab_id_refusal(value: Any) -> str | None:
    if type(value) is not str or _TAB_ID_PATTERN.fullmatch(value) is None:
        return "the tab id is not a browser target id"
    return None


def _digest_refusal(value: Any) -> str | None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        return "the page digest is not a sha256 digest"
    return None


def _seconds_refusal(value: Any) -> str | None:
    if type(value) is not int or type(value) is bool:
        return "the wait is not a whole number of seconds"
    if not BROWSER_WAIT_MIN_SECONDS <= value <= BROWSER_WAIT_MAX_SECONDS:
        return (
            f"the wait is outside {BROWSER_WAIT_MIN_SECONDS}-"
            f"{BROWSER_WAIT_MAX_SECONDS} seconds"
        )
    return None


def browser_text_refusal(value: Any) -> str | None:
    """Why this typed text may not be entered, or ``None``.

    A credential shape is refused OUTRIGHT rather than redacted. JARVIS does
    not type secrets: if a field needs one, the person types it.
    """
    if type(value) is not str:
        return "the text is not a string"
    if not value or len(value) > BROWSER_TEXT_MAX_CHARS:
        return "the text is not bounded text"
    if any(char < " " and char not in "\t" for char in value) or "\x7f" in value:
        return "the text carries a control character"
    if credential_bearing(value):
        return "the text carries a credential shape and will not be typed"
    return None


_RESOURCE_FIELD_REFUSAL = {
    "url": browser_url_refusal,
    "seconds": _seconds_refusal,
    "tab_id": _tab_id_refusal,
    "ref": _ref_refusal,
    "page_digest": _digest_refusal,
    "text": browser_text_refusal,
}


@dataclass(frozen=True, slots=True)
class BrowserResource:
    """One typed browser target. Never a selector, script, argv or coordinate."""

    kind: BrowserResourceKind
    origin: str
    url: str = ""
    seconds: int = 0
    tab_id: str = ""
    ref: str = ""
    page_digest: str = ""
    text: str = ""

    @property
    def destination(self) -> str:
        return f"{BROWSER_DESTINATION_PREFIX}{self.origin}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "origin": self.origin,
            "url": self.url,
            "seconds": self.seconds,
            "tab_id": self.tab_id,
            "ref": self.ref,
            "page_digest": self.page_digest,
            "text": self.text,
        }

    @property
    def identity(self) -> str:
        """One unambiguous canonical spelling of this target."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def browser_resource_refusal(resource: Any) -> str | None:
    """Why this is not a valid typed browser target, or ``None``."""
    if type(resource) is not BrowserResource:
        return "the resource is not a BrowserResource"
    if type(resource.kind) is not BrowserResourceKind:
        return "the resource kind is not a recognized kind"
    reason = browser_origin_refusal(resource.origin)
    if reason is not None:
        return reason
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
    # THE DESTINATION BOUNDARY, stated on the resource itself: a navigating
    # capability may only name a URL inside the origin its grant authorized.
    if resource.url and not url_in_origin(resource.url, resource.origin):
        return "the url is outside the authorized origin"
    return None


_RESOURCE_KEYS = frozenset({"kind", "origin"} | set(_RESOURCE_ABSENT))


class BrowserExecutionUnavailable(RuntimeError):
    """The operation cannot be performed, and NOTHING happened."""


class BrowserOutcomeUnknown(RuntimeError):
    """The operation may have taken effect and the outcome is not established."""


def browser_resource_from_dict(data: Any) -> BrowserResource:
    """Rebuild a typed resource from persisted or untrusted state, or refuse.

    A persisted resource is STATE, never a promise: every field is judged
    again here, exactly as it was at the input boundary.
    """
    if not isinstance(data, dict) or not data:
        raise BrowserExecutionUnavailable("the task names no browser resource")
    unknown = set(data) - _RESOURCE_KEYS
    if unknown:
        raise BrowserExecutionUnavailable("the browser resource carries an unknown field")
    try:
        kind = BrowserResourceKind(str(data.get("kind", "")))
    except ValueError as exc:
        raise BrowserExecutionUnavailable("the browser resource kind is not a recognized kind") from exc
    values: dict[str, Any] = {}
    for name, absent in _RESOURCE_ABSENT.items():
        value = data.get(name, absent)
        if type(value) is not type(absent):
            raise BrowserExecutionUnavailable(f"the browser resource '{name}' is not typed")
        values[name] = value
    origin = data.get("origin", "")
    if type(origin) is not str:
        raise BrowserExecutionUnavailable("the browser resource origin is not a string")
    resource = BrowserResource(kind=kind, origin=origin, **values)
    reason = browser_resource_refusal(resource)
    if reason is not None:
        raise BrowserExecutionUnavailable(reason)
    return resource


def browser_resource_binding_refusal(
    resource: Any, *, capability: Any, destination: Any
) -> str | None:
    """Why this resource is not the one THIS capability and destination mean."""
    reason = browser_resource_refusal(resource)
    if reason is not None:
        return reason
    expected = BROWSER_CAPABILITY_RESOURCE.get(capability) if type(capability) is str else None
    if expected is None:
        return "the capability is not an enabled browser capability"
    if resource.kind is not expected:
        return "the resource kind is not the one this capability acts on"
    if type(destination) is not str or destination != resource.destination:
        return "the resource does not name the requested destination"
    return None


# --------------------------------------------------------------------------
# The request frame - everything one approval authorizes
# --------------------------------------------------------------------------

BROWSER_REQUEST_FRAME_HEADER = b"jarvis.browser_request.v1"

BROWSER_READ_OPERATION = "read"
BROWSER_INTERACT_OPERATION = "interact"

BROWSER_CAPABILITY_OPERATION: Mapping[str, str] = {
    **{name: BROWSER_READ_OPERATION for name in BROWSER_READ_CAPABILITIES},
    **{name: BROWSER_INTERACT_OPERATION for name in BROWSER_INTERACT_CAPABILITIES},
}


@dataclass(frozen=True, slots=True)
class BrowserRequestFrame:
    """Everything one browser approval authorizes, and nothing else."""

    connector: str
    capability: str
    destination: str
    resource: BrowserResource
    origin: str
    operation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "capability": self.capability,
            "destination": self.destination,
            "resource": self.resource.to_dict(),
            "origin": self.origin,
            "operation": self.operation,
        }

    def canonical_bytes(self) -> bytes:
        body = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return BROWSER_REQUEST_FRAME_HEADER + b"\n" + body

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def browser_frame_refusal(frame: Any) -> str | None:
    """Why this frame is not the one this build would derive, or ``None``."""
    if type(frame) is not BrowserRequestFrame:
        return "the frame is not a BrowserRequestFrame"
    if frame.connector != BROWSER_CONNECTOR:
        return "the connector is not browser-cdp"
    reason = browser_resource_binding_refusal(
        frame.resource, capability=frame.capability, destination=frame.destination
    )
    if reason is not None:
        return reason
    if frame.origin != frame.resource.origin:
        return "the frame origin is not the resource origin"
    if frame.operation != BROWSER_CAPABILITY_OPERATION.get(frame.capability):
        return "the operation is not the one this capability performs"
    return None


def build_browser_request_frame(
    *,
    capability: str,
    destination: str,
    resource: BrowserResource,
    connector: str = BROWSER_CONNECTOR,
) -> BrowserRequestFrame:
    """Validate every part and return the frame, or fail closed."""
    reason = browser_resource_binding_refusal(
        resource, capability=capability, destination=destination
    )
    if reason is None and capability not in BROWSER_CAPABILITY_OPERATION:
        reason = "no browser operation exists for this capability in this build"
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    frame = BrowserRequestFrame(
        connector=connector,
        capability=capability,
        destination=destination,
        resource=resource,
        origin=resource.origin,
        operation=BROWSER_CAPABILITY_OPERATION[capability],
    )
    reason = browser_frame_refusal(frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return frame


# --------------------------------------------------------------------------
# The operation plan - the closed set of protocol methods one capability uses
# --------------------------------------------------------------------------
#
# The browser is driven by the DevTools protocol, whose method name is a
# STRING. So the closed thing here is not an argument vector but the exact
# set of methods each capability is permitted to send, enforced at the socket
# by ``jarvis.browser_cdp``. ``Runtime.evaluate``, ``Runtime.callFunctionOn``,
# ``Page.addScriptToEvaluateOnNewDocument``, the whole ``Network`` domain and
# every ``Browser``/``Storage``/``Fetch`` method are absent from every row, so
# no capability can send one and no task can select one: a capability chooses
# a resource, never an API.

_PAGE_READ_METHODS = frozenset(
    {
        "Target.getTargets",
        "Target.attachToTarget",
        "DOM.enable",
        "Accessibility.enable",
        "Page.getNavigationHistory",
        "Accessibility.getFullAXTree",
    }
)
_ELEMENT_METHODS = _PAGE_READ_METHODS | {
    "DOM.describeNode",
    "DOM.getBoxModel",
    "DOM.getNodeForLocation",
    "DOM.scrollIntoViewIfNeeded",
}

BROWSER_CAPABILITY_METHODS: Mapping[str, frozenset[str]] = {
    "browser_page_read": _PAGE_READ_METHODS,
    "browser_tab_list": frozenset({"Target.getTargets"}),
    "browser_wait": _PAGE_READ_METHODS,
    "browser_navigate": _PAGE_READ_METHODS | {"Page.navigate"},
    "browser_tab_open": frozenset({"Target.getTargets", "Target.createTarget"}),
    "browser_tab_switch": frozenset({"Target.getTargets", "Target.activateTarget"}),
    "browser_tab_close": frozenset({"Target.getTargets", "Target.closeTarget"}),
    "browser_click": _ELEMENT_METHODS | {"Input.dispatchMouseEvent"},
    "browser_type": _ELEMENT_METHODS | {"DOM.focus", "Input.insertText"},
}


@dataclass(frozen=True, slots=True)
class BrowserOperationPlan:
    """The methods and target one validated operation WOULD use. DATA."""

    capability: str
    destination: str
    resource: BrowserResource
    methods: frozenset[str]

    authorizes_execution: ClassVar[bool] = False

    @property
    def is_interaction(self) -> bool:
        return self.capability in BROWSER_INTERACT_CAPABILITIES


def browser_operation_plan_refusal(plan: Any) -> str | None:
    """Why this plan is not the closed plan it claims to be, or ``None``."""
    if type(plan) is not BrowserOperationPlan:
        return "the plan is not a BrowserOperationPlan"
    if type(plan.capability) is not str or plan.capability not in BROWSER_CAPABILITY_METHODS:
        return "the plan names no closed browser operation"
    reason = browser_resource_binding_refusal(
        plan.resource, capability=plan.capability, destination=plan.destination
    )
    if reason is not None:
        return reason
    if plan.methods != BROWSER_CAPABILITY_METHODS[plan.capability]:
        return "the plan's methods are not the ones this operation derives"
    return None


def browser_operation_plan(frame: Any) -> BrowserOperationPlan:
    """The closed plan for one validated frame, or fail closed."""
    reason = browser_frame_refusal(frame)
    if reason is not None:
        raise BrowserExecutionUnavailable(reason)
    if frame.capability not in BROWSER_CAPABILITY_METHODS:
        raise BrowserExecutionUnavailable(
            "no browser operation exists for this capability in this build"
        )
    plan = BrowserOperationPlan(
        capability=frame.capability,
        destination=frame.destination,
        resource=frame.resource,
        methods=BROWSER_CAPABILITY_METHODS[frame.capability],
    )
    reason = browser_operation_plan_refusal(plan)
    if reason is not None:  # pragma: no cover - the derivation just built it
        raise BrowserExecutionUnavailable(reason)
    return plan


# --------------------------------------------------------------------------
# Rendering - what the agent is allowed to see of a page
# --------------------------------------------------------------------------

#: Roles worth printing at all. Everything else is structure the agent cannot
#: act on, and printing it would only crowd out what it can.
_RENDERED_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "heading",
        "link",
        "listbox",
        "menuitem",
        "option",
        "radio",
        "searchbox",
        "slider",
        "StaticText",
        "switch",
        "tab",
        "textbox",
    }
)
#: The only roles ``browser_type`` will enter text into. A button or a link
#: is a perfectly valid click target and never a text field.
BROWSER_TEXT_ROLES = frozenset({"textbox", "searchbox", "combobox"})
_LABEL_MAX_CHARS = 120


def _ax_text(node: Any, key: str) -> str:
    """One bounded plain string out of an accessibility node's property."""
    if not isinstance(node, dict):
        return ""
    holder = node.get(key)
    if not isinstance(holder, dict):
        return ""
    value = holder.get("value")
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_LABEL_MAX_CHARS]


def browser_node_role(node: Any) -> str:
    """The accessibility role one rendered node carries."""
    return _ax_text(node, "role")


def browser_rendered_nodes(nodes: Any) -> list[dict[str, Any]]:
    """The accessibility nodes ``browser_page_rendering`` prints, in order.

    The ONE definition of which nodes exist, shared by the rendering and by
    the connector that resolves a ref back to a node. Two definitions could
    drift, and a drift here would mean a ref naming a different element than
    the one the reading showed.
    """
    if not isinstance(nodes, list):
        raise BrowserExecutionUnavailable("the page returned no accessibility tree")
    rendered: list[dict[str, Any]] = []
    for node in nodes:
        if len(rendered) >= BROWSER_MAX_NODES:
            break
        if not isinstance(node, dict) or node.get("ignored"):
            continue
        if _ax_text(node, "role") not in _RENDERED_ROLES:
            continue
        rendered.append(node)
    return rendered


def browser_page_rendering(nodes: Any, *, url: str, title: str) -> str:
    """The bounded, untrusted rendering of one page state.

    Deterministic: the same page state renders to the same bytes, which is
    what makes the digest a usable freshness binding. Each actionable node
    gets a ``ref`` that means nothing outside this rendering.

    UNTRUSTED DATA. Every string in it came from the page. It is redacted for
    credential shapes and a password field's value is never printed at all,
    but nothing in it is an instruction and nothing in it may become a
    target, a URL or a capability.
    """
    if not isinstance(nodes, list):
        raise BrowserExecutionUnavailable("the page returned no accessibility tree")
    lines = [f"url: {url}", f"title: {' '.join(str(title).split())[:_LABEL_MAX_CHARS]}"]
    for index, node in enumerate(browser_rendered_nodes(nodes), start=1):
        role = _ax_text(node, "role")
        name = _ax_text(node, "name")
        # NO FIELD VALUES, EVER. The accessibility tree reports a password
        # input as an ordinary ``textbox`` - there is no reliable flag on it -
        # so a rendering that printed values would print the one thing it
        # must not. Role and name are what an agent needs to choose a target;
        # what is already inside a field is not.
        lines.append(f"r{index} {role} {json.dumps(name, ensure_ascii=True)}")
    rendered, _ = redact_credentials("\n".join(lines))
    return rendered[:EXTERNAL_READ_MAX_CHARS]


def browser_tab_rendering(targets: Any, *, origin: str) -> str:
    """The bounded, untrusted listing of the open tabs INSIDE one origin.

    A grant for one origin is not a licence to enumerate the operator's other
    browsing: a tab outside the bound origin is not listed at all, so its URL
    and title never reach the model.
    """
    if not isinstance(targets, list):
        raise BrowserExecutionUnavailable("the browser returned no tab listing")
    lines = []
    for target in targets[:BROWSER_MAX_TABS]:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        tab_id = str(target.get("targetId", ""))
        if _tab_id_refusal(tab_id) is not None:
            continue
        url = " ".join(str(target.get("url", "")).split())[:BROWSER_URL_MAX_CHARS]
        if not url_in_origin(url, origin):
            continue
        title = " ".join(str(target.get("title", "")).split())[:_LABEL_MAX_CHARS]
        lines.append(f"{tab_id} {json.dumps(title, ensure_ascii=True)} {url}")
    rendered, _ = redact_credentials("\n".join(lines))
    return rendered[:EXTERNAL_READ_MAX_CHARS]


def browser_page_digest(rendering: str) -> str:
    """The digest an interaction binds. Of the rendering, and nothing else."""
    if type(rendering) is not str:
        raise BrowserExecutionUnavailable("the page rendering is not text")
    return hashlib.sha256(rendering.encode("utf-8")).hexdigest()


def browser_ref_index(rendering: Any, ref: Any) -> int:
    """The 1-based position of ``ref`` in this rendering, or fail closed.

    Exactly one line may open with the ref. Zero means the element the
    approval named is not on this page; more than one means the rendering is
    ambiguous and JARVIS will not guess which was meant.
    """
    if type(rendering) is not str or _ref_refusal(ref) is not None:
        raise BrowserExecutionUnavailable("the target ref is not one a page reading produced")
    matches = [line for line in rendering.splitlines() if line.split(" ", 1)[0] == ref]
    if len(matches) != 1:
        raise BrowserExecutionUnavailable(
            "the target is not identified by exactly one element of this page"
        )
    return int(ref[1:])


def browser_staleness_refusal(rendering: Any, *, frame: Any) -> str | None:
    """Why the live page is not the page the approval was for, or ``None``."""
    if type(frame) is not BrowserRequestFrame:
        return "the interaction names no validated request"
    if frame.resource.kind not in BROWSER_ELEMENT_KINDS:
        return "the interaction names no page element"
    if type(rendering) is not str:
        return "the live page returned no rendering"
    if browser_page_digest(rendering) != frame.resource.page_digest:
        return (
            "the page changed since it was read, so the approved target no "
            "longer describes it"
        )
    return None


# --------------------------------------------------------------------------
# The outcome of one interaction
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserOutcome:
    """What the browser acknowledged for one interaction."""

    capability: str
    destination: str
    tab_id: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "destination": self.destination,
            "tab_id": self.tab_id,
            "url": self.url,
        }


def browser_outcome(payload: Any, *, frame: Any) -> BrowserOutcome:
    """The acknowledgement for one interaction, or an UNKNOWN outcome.

    Reached only after the command was dispatched. An acknowledgement that
    cannot be read is not evidence that nothing happened, so it escapes as
    ``BrowserOutcomeUnknown`` rather than becoming a refusal.
    """
    try:
        if type(frame) is not BrowserRequestFrame:
            raise BrowserExecutionUnavailable("the acknowledgement names no request")
        if not isinstance(payload, dict):
            raise BrowserExecutionUnavailable("the browser acknowledged nothing")
        tab_id = str(payload.get("tab_id", ""))
        if _tab_id_refusal(tab_id) is not None:
            raise BrowserExecutionUnavailable("the acknowledgement names no tab")
        url = str(payload.get("url", ""))
        # A closed tab has no url left to report; every other interaction
        # must name where it landed.
        if frame.capability != "browser_tab_close" and browser_url_refusal(url) is not None:
            raise BrowserExecutionUnavailable("the acknowledgement names no page")
    except BrowserExecutionUnavailable as exc:
        raise BrowserOutcomeUnknown(
            f"the browser {frame_capability(frame)} may have happened but was not "
            f"acknowledged: {exc}"
        ) from exc
    return BrowserOutcome(
        capability=frame.capability,
        destination=frame.destination,
        tab_id=tab_id,
        url=url,
    )


def frame_capability(frame: Any) -> str:
    return frame.capability if type(frame) is BrowserRequestFrame else "operation"


# --------------------------------------------------------------------------
# The external read snapshot - the shared transport, the browser's vocabulary
# --------------------------------------------------------------------------


def browser_external_read_refusal(snapshot: Any) -> str | None:
    """Why this snapshot is not usable browser reference data, or ``None``."""
    reason = external_read_shape_refusal(snapshot)
    if reason is not None:
        return reason
    if snapshot.connector != BROWSER_CONNECTOR:
        return "the snapshot names no known producer"
    if snapshot.capability not in BROWSER_READ_CAPABILITIES:
        return "the snapshot capability is not a browser read capability"
    if not is_browser_destination(snapshot.destination):
        return "the snapshot destination is not a canonical origin"
    if snapshot.resource_kind not in {kind.value for kind in BrowserResourceKind}:
        return "the snapshot resource kind is not a recognized kind"
    return None


def browser_external_read_binding_refusal(snapshot: Any, *, frame: Any) -> str | None:
    """Why this snapshot does not describe THIS validated request, or ``None``."""
    if type(frame) is not BrowserRequestFrame:
        return "the snapshot names no validated request"
    reason = browser_external_read_refusal(snapshot)
    if reason is not None:
        return reason
    reason = browser_frame_refusal(frame)
    if reason is not None:
        return reason
    if snapshot.connector != frame.connector:
        return "the snapshot connector is not the requesting connector"
    if snapshot.capability != frame.capability:
        return "the snapshot capability is not the requested capability"
    if snapshot.destination != frame.destination:
        return "the snapshot origin is not the requested origin"
    if snapshot.resource_kind != frame.resource.kind.value:
        return "the snapshot resource kind is not the requested kind"
    if snapshot.resource_id != frame.resource.identity:
        return "the snapshot resource is not the requested resource"
    return None


def build_browser_read_snapshot(
    *,
    source_task_id: str,
    frame: Any,
    content: str,
    captured_at: str,
    truncated: bool = False,
) -> ExternalReadSnapshot:
    """The snapshot for one validated read and the content it returned."""
    if type(frame) is not BrowserRequestFrame or frame.capability not in BROWSER_READ_CAPABILITIES:
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
    reason = browser_external_read_binding_refusal(snapshot, frame=frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return snapshot


__all__ = [
    "BROWSER_BILLING_CLASS",
    "BROWSER_TEXT_ROLES",
    "BROWSER_CAPABILITIES",
    "BROWSER_CAPABILITY_METHODS",
    "BROWSER_CAPABILITY_OPERATION",
    "BROWSER_CAPABILITY_RESOURCE",
    "BROWSER_CONNECTOR",
    "BROWSER_DEFERRED_CAPABILITIES",
    "BROWSER_DESTINATION_PREFIX",
    "BROWSER_ELEMENT_KINDS",
    "BROWSER_ENABLED_CAPABILITIES",
    "BROWSER_INTERACT_CAPABILITIES",
    "BROWSER_MAX_NODES",
    "BROWSER_MAX_TABS",
    "BROWSER_MONETARY_STATE",
    "BROWSER_ORIGIN_MAX_CHARS",
    "BROWSER_READ_CAPABILITIES",
    "BROWSER_SCHEMES",
    "BROWSER_TEXT_MAX_CHARS",
    "BROWSER_URL_MAX_CHARS",
    "BROWSER_WAIT_MAX_SECONDS",
    "BROWSER_WAIT_MIN_SECONDS",
    "BrowserExecutionUnavailable",
    "BrowserOperationPlan",
    "BrowserOrigin",
    "BrowserOutcome",
    "BrowserOutcomeUnknown",
    "BrowserRequestFrame",
    "BrowserResource",
    "BrowserResourceKind",
    "build_browser_read_snapshot",
    "build_browser_request_frame",
    "browser_destination",
    "browser_destination_refusal",
    "browser_external_read_binding_refusal",
    "browser_external_read_refusal",
    "browser_frame_refusal",
    "browser_node_role",
    "browser_operation_plan",
    "browser_operation_plan_refusal",
    "browser_origin_refusal",
    "browser_outcome",
    "browser_page_digest",
    "browser_page_rendering",
    "browser_raw_destination_refusal",
    "browser_ref_index",
    "browser_rendered_nodes",
    "browser_resource_binding_refusal",
    "browser_resource_from_dict",
    "browser_resource_refusal",
    "browser_staleness_refusal",
    "browser_tab_rendering",
    "browser_text_refusal",
    "browser_url_refusal",
    "is_browser_destination",
    "url_in_origin",
]
