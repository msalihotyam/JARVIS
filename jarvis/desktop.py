"""Windows Desktop basic usable-v1 - the closed vocabulary, grammar and frame.

The same shape as ``jarvis.browser``, on purpose: one authorized application
is one canonical destination ``desktop/<image path>``, a task names a TYPED
resource inside it and nothing else, an approval binds one canonical request
frame, and a read returns an UNTRUSTED bounded snapshot through the
external-read transport ``jarvis.github`` already defines. Nothing here
touches Windows; the seam is ``jarvis.desktop_win32`` and the connector is
``jarvis.connectors.desktop_uia``.

THE ALLOWED-APP BOUNDARY. An application is named by the canonical path of
its executable image - lower case, forward slashes - exactly as Windows
reports it for a running process. A window belongs to that application only
when its live owning process runs that exact image. The composition root
turns each configured application into exact ``DestinationGrant`` rows; an
application nobody configured has no row and is refused by Core. Shells,
terminals, script hosts, the Explorer shell and the shared UWP frame host are
refused by the grammar itself: authority over one of those would be authority
over the whole machine.

WHAT A TASK MAY NAME. A window, by the opaque id a listing printed - the
window handle, the owning process id and that process's creation time, so a
handle Windows reused for another window, or a process id reused by another
process, no longer matches. An element, by a ``ref`` an inspection printed
together with the digest of that inspection. Never a coordinate, a class
name, an automation id, a command line or a process id of its own choosing.

WHAT IS STRUCTURALLY ABSENT. No capability reads a field's value, reads or
writes the clipboard, takes a screenshot, closes or moves a window, drags,
or runs a command. Those names are refused by name.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from jarvis.contracts import (
    APPLICATION_DESTINATION_PREFIX,
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

DESKTOP_CONNECTOR = "desktop-uia"
DESKTOP_DESTINATION_PREFIX = APPLICATION_DESTINATION_PREFIX

#: Capabilities whose whole effect is to observe.
DESKTOP_READ_CAPABILITIES: tuple[str, ...] = (
    "desktop_window_list",
    "desktop_window_inspect",
)

#: Capabilities that act. Every one is irreversible for this product: a
#: launch starts a program, a focus decides where the NEXT input lands, and a
#: click or a keystroke can do anything the application does with one.
DESKTOP_INTERACT_CAPABILITIES: tuple[str, ...] = (
    "desktop_app_launch",
    "desktop_window_focus",
    "desktop_click",
    "desktop_type",
    "desktop_key",
)

#: Refused BY NAME rather than by silence. usable-v1 ships none of them.
DESKTOP_DEFERRED_CAPABILITIES: tuple[str, ...] = (
    "desktop_screenshot",
    "desktop_clipboard_read",
    "desktop_clipboard_write",
    "desktop_window_close",
    "desktop_window_move",
    "desktop_drag",
    "desktop_field_value_read",
    "desktop_command_run",
)

DESKTOP_ENABLED_CAPABILITIES: tuple[str, ...] = (
    DESKTOP_READ_CAPABILITIES + DESKTOP_INTERACT_CAPABILITIES
)
DESKTOP_CAPABILITIES: tuple[str, ...] = (
    DESKTOP_ENABLED_CAPABILITIES + DESKTOP_DEFERRED_CAPABILITIES
)

DESKTOP_BILLING_CLASS = BillingClass.NO_ADDITIONAL_CHARGE
DESKTOP_MONETARY_STATE = MonetaryState.NOT_APPLICABLE

# --------------------------------------------------------------------------
# The application grammar - a closed spelling, never a repair
# --------------------------------------------------------------------------

DESKTOP_APP_MAX_CHARS = 260
DESKTOP_TEXT_MAX_CHARS = 1_000
#: How many elements one inspection may print, and how many windows a listing.
DESKTOP_MAX_ELEMENTS = 200
DESKTOP_MAX_WINDOWS = 25

_SEGMENT = r"[a-z0-9._()+\-](?:[a-z0-9._()+\- ]*[a-z0-9._()+\-])?"
_APP_PATTERN = re.compile(rf"\A[a-z]:(?:/{_SEGMENT})+\Z")

#: Executables no grant may ever name. Each is a general-purpose way to do
#: anything at all - a shell, a terminal, a script host, an interpreter, the
#: desktop shell itself, the frame host every UWP application shares, or a
#: system administration tool - so allowing one would not be an allowed app.
DESKTOP_REFUSED_EXECUTABLES = frozenset(
    {
        "applicationframehost.exe", "bash.exe", "cmd.exe", "conhost.exe",
        "control.exe", "cscript.exe", "dllhost.exe", "explorer.exe",
        "mmc.exe", "msiexec.exe", "mshta.exe", "node.exe", "openconsole.exe",
        "powershell.exe", "powershell_ise.exe", "pwsh.exe", "py.exe",
        "python.exe", "pythonw.exe", "regedit.exe", "regsvr32.exe",
        "rundll32.exe", "runas.exe", "svchost.exe", "taskmgr.exe",
        "windowsterminal.exe", "wscript.exe", "wsl.exe", "wslhost.exe",
        "wt.exe",
    }
)
#: Console and terminal windows are never an application's window: typing
#: into one is running a command.
DESKTOP_REFUSED_WINDOW_CLASSES = frozenset(
    {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"}
)


def desktop_app_refusal(value: Any) -> str | None:
    """Why this is not a canonical allowed-application image path, or ``None``."""
    if type(value) is not str:
        return "the application is not a string"
    if not value or len(value) > DESKTOP_APP_MAX_CHARS:
        return "the application is not bounded text"
    if _APP_PATTERN.fullmatch(value) is None:
        return "the application is not spelled as a lower-case absolute image path"
    segments = value.split("/")[1:]
    if any(segment in (".", "..") for segment in segments):
        return "the application path is not canonical"
    if not segments[-1].endswith(".exe"):
        return "the application does not name an executable image"
    if segments[-1] in DESKTOP_REFUSED_EXECUTABLES:
        return "the application is a shell or system tool and is never an allowed app"
    return None


def canonical_image_path(path: Any) -> str:
    """How an image path Windows reports is compared with a grant."""
    return str(path).replace("\\", "/").lower()


def desktop_destination_refusal(requested: Any) -> str | None:
    """Why this is not a canonical ``desktop/<image path>`` destination."""
    if type(requested) is not str:
        return "the destination is not a string"
    if not requested.startswith(DESKTOP_DESTINATION_PREFIX):
        return "the destination is not in the desktop namespace"
    return desktop_app_refusal(requested[len(DESKTOP_DESTINATION_PREFIX) :])


#: Judged on the spelling AS WRITTEN; the central normalizer hands a
#: noncanonical ``desktop/...`` through unrepaired so it lands here.
desktop_raw_destination_refusal = desktop_destination_refusal


def is_desktop_destination(canonical: Any) -> bool:
    return desktop_destination_refusal(canonical) is None


# --------------------------------------------------------------------------
# Window identity, refs and the typed resource
# --------------------------------------------------------------------------

_WINDOW_ID_PATTERN = re.compile(r"\A([0-9a-f]{1,16})-([0-9]{1,10})-([0-9]{1,20})\Z")
_REF_PATTERN = re.compile(r"\Ar[1-9][0-9]{0,3}\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

#: The closed key set ``desktop_key`` may press: (virtual-key code, extended).
#: No modifier and no combination - a shortcut is a command.
DESKTOP_KEYS: Mapping[str, tuple[int, bool]] = {
    "enter": (0x0D, False),
    "tab": (0x09, False),
    "escape": (0x1B, False),
    "backspace": (0x08, False),
    "space": (0x20, False),
    "delete": (0x2E, True),
    "home": (0x24, True),
    "end": (0x23, True),
    "pageup": (0x21, True),
    "pagedown": (0x22, True),
    "left": (0x25, True),
    "up": (0x26, True),
    "right": (0x27, True),
    "down": (0x28, True),
}


def desktop_window_id(hwnd: int, pid: int, created: int) -> str:
    return f"{hwnd:x}-{pid}-{created}"


def parse_desktop_window_id(value: Any) -> tuple[int, int, int]:
    match = _WINDOW_ID_PATTERN.fullmatch(value) if type(value) is str else None
    if match is None:
        raise DesktopExecutionUnavailable("the window id is not one a listing produced")
    return int(match.group(1), 16), int(match.group(2)), int(match.group(3))


def desktop_text_refusal(value: Any) -> str | None:
    """Why this text may not be typed, or ``None``. A credential shape is
    refused outright: JARVIS does not type secrets, the person does."""
    if type(value) is not str:
        return "the text is not a string"
    if not value or len(value) > DESKTOP_TEXT_MAX_CHARS:
        return "the text is not bounded text"
    if any(char < " " or char == "\x7f" for char in value):
        return "the text carries a control character; name a key with desktop_key"
    if credential_bearing(value):
        return "the text carries a credential shape and will not be typed"
    return None


class DesktopResourceKind(str, Enum):
    LAUNCH = "launch"
    WINDOWS = "windows"
    WINDOW = "window"
    FOCUS = "focus"
    CLICK_TARGET = "click_target"
    TEXT_TARGET = "text_target"
    KEY_TARGET = "key_target"


DESKTOP_CAPABILITY_RESOURCE: Mapping[str, DesktopResourceKind] = {
    "desktop_app_launch": DesktopResourceKind.LAUNCH,
    "desktop_window_list": DesktopResourceKind.WINDOWS,
    "desktop_window_inspect": DesktopResourceKind.WINDOW,
    "desktop_window_focus": DesktopResourceKind.FOCUS,
    "desktop_click": DesktopResourceKind.CLICK_TARGET,
    "desktop_type": DesktopResourceKind.TEXT_TARGET,
    "desktop_key": DesktopResourceKind.KEY_TARGET,
}

_ELEMENT_FIELDS = frozenset({"window_id", "ref", "digest"})
_RESOURCE_FIELDS: Mapping[DesktopResourceKind, frozenset[str]] = {
    DesktopResourceKind.LAUNCH: frozenset(),
    DesktopResourceKind.WINDOWS: frozenset(),
    DesktopResourceKind.WINDOW: frozenset({"window_id"}),
    DesktopResourceKind.FOCUS: frozenset({"window_id"}),
    DesktopResourceKind.CLICK_TARGET: _ELEMENT_FIELDS,
    DesktopResourceKind.TEXT_TARGET: _ELEMENT_FIELDS | {"text"},
    DesktopResourceKind.KEY_TARGET: _ELEMENT_FIELDS | {"key"},
}
_RESOURCE_ABSENT: Mapping[str, str] = {
    "window_id": "",
    "ref": "",
    "digest": "",
    "text": "",
    "key": "",
}


def _field_refusal(name: str, value: str) -> str | None:
    if name == "window_id":
        return None if _WINDOW_ID_PATTERN.fullmatch(value) else "the window id is not one a listing produced"
    if name == "ref":
        return None if _REF_PATTERN.fullmatch(value) else "the target ref is not one an inspection produced"
    if name == "digest":
        return None if _SHA256_PATTERN.fullmatch(value) else "the inspection digest is not a sha256 digest"
    if name == "text":
        return desktop_text_refusal(value)
    return None if value in DESKTOP_KEYS else "the key is not one desktop_key may press"


@dataclass(frozen=True, slots=True)
class DesktopResource:
    """One typed desktop target. Never a coordinate, selector or command."""

    kind: DesktopResourceKind
    app: str
    window_id: str = ""
    ref: str = ""
    digest: str = ""
    text: str = ""
    key: str = ""

    @property
    def destination(self) -> str:
        return f"{DESKTOP_DESTINATION_PREFIX}{self.app}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "app": self.app,
            "window_id": self.window_id,
            "ref": self.ref,
            "digest": self.digest,
            "text": self.text,
            "key": self.key,
        }

    @property
    def identity(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class DesktopExecutionUnavailable(RuntimeError):
    """The operation was refused, and its outcome is DEFINITE."""


class DesktopOutcomeUnknown(RuntimeError):
    """Input may have reached the application and the outcome is not established."""


def desktop_resource_refusal(resource: Any) -> str | None:
    if type(resource) is not DesktopResource:
        return "the resource is not a DesktopResource"
    if type(resource.kind) is not DesktopResourceKind:
        return "the resource kind is not a recognized kind"
    reason = desktop_app_refusal(resource.app)
    if reason is not None:
        return reason
    allowed = _RESOURCE_FIELDS[resource.kind]
    for name in _RESOURCE_ABSENT:
        value = getattr(resource, name)
        if type(value) is not str:
            return f"the desktop resource '{name}' is not typed"
        if name not in allowed:
            if value:
                return f"the {resource.kind.value} target may not carry a {name}"
            continue
        reason = _field_refusal(name, value)
        if reason is not None:
            return reason
    return None


_RESOURCE_KEYS = frozenset({"kind", "app"} | set(_RESOURCE_ABSENT))


def desktop_resource_from_dict(data: Any) -> DesktopResource:
    """Rebuild a typed resource from persisted or untrusted state, or refuse."""
    if not isinstance(data, dict) or not data:
        raise DesktopExecutionUnavailable("the task names no desktop resource")
    if set(data) - _RESOURCE_KEYS:
        raise DesktopExecutionUnavailable("the desktop resource carries an unknown field")
    try:
        kind = DesktopResourceKind(str(data.get("kind", "")))
    except ValueError as exc:
        raise DesktopExecutionUnavailable("the desktop resource kind is not a recognized kind") from exc
    values = {name: data.get(name, "") for name in ("app", *_RESOURCE_ABSENT)}
    if any(type(value) is not str for value in values.values()):
        raise DesktopExecutionUnavailable("the desktop resource is not typed")
    resource = DesktopResource(kind=kind, **values)
    reason = desktop_resource_refusal(resource)
    if reason is not None:
        raise DesktopExecutionUnavailable(reason)
    return resource


def desktop_resource_binding_refusal(resource: Any, *, capability: Any, destination: Any) -> str | None:
    """Why this resource is not the one THIS capability and destination mean."""
    reason = desktop_resource_refusal(resource)
    if reason is not None:
        return reason
    expected = DESKTOP_CAPABILITY_RESOURCE.get(capability) if type(capability) is str else None
    if expected is None:
        return "the capability is not an enabled desktop capability"
    if resource.kind is not expected:
        return "the resource kind is not the one this capability acts on"
    if type(destination) is not str or destination != resource.destination:
        return "the resource does not name the requested destination"
    return None


# --------------------------------------------------------------------------
# The request frame - everything one approval authorizes
# --------------------------------------------------------------------------

DESKTOP_REQUEST_FRAME_HEADER = b"jarvis.desktop_request.v1"


@dataclass(frozen=True, slots=True)
class DesktopRequestFrame:
    connector: str
    capability: str
    destination: str
    resource: DesktopResource

    @property
    def app(self) -> str:
        return self.resource.app

    def canonical_bytes(self) -> bytes:
        body = json.dumps(
            {
                "connector": self.connector,
                "capability": self.capability,
                "destination": self.destination,
                "resource": self.resource.to_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return DESKTOP_REQUEST_FRAME_HEADER + b"\n" + body


def desktop_frame_refusal(frame: Any) -> str | None:
    if type(frame) is not DesktopRequestFrame:
        return "the frame is not a DesktopRequestFrame"
    if frame.connector != DESKTOP_CONNECTOR:
        return "the connector is not desktop-uia"
    return desktop_resource_binding_refusal(
        frame.resource, capability=frame.capability, destination=frame.destination
    )


def build_desktop_request_frame(
    *, capability: str, destination: str, resource: DesktopResource, connector: str = DESKTOP_CONNECTOR
) -> DesktopRequestFrame:
    frame = DesktopRequestFrame(
        connector=connector, capability=capability, destination=destination, resource=resource
    )
    reason = desktop_frame_refusal(frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return frame


# --------------------------------------------------------------------------
# What the backend reports, and what the agent is allowed to see of it
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DesktopWindow:
    """One visible top-level window, as Windows reported it just now."""

    hwnd: int
    pid: int
    created: int
    image: str
    title: str
    class_name: str
    minimized: bool
    foreground: bool

    @property
    def window_id(self) -> str:
        return desktop_window_id(self.hwnd, self.pid, self.created)


@dataclass(frozen=True, slots=True)
class DesktopElement:
    """One UI Automation element. ``handle`` is the backend's live reference
    and is never rendered, compared or persisted. There is deliberately no
    value field: nothing in this build reads what a field contains."""

    control_type: int
    name: str
    rect: tuple[int, int, int, int]
    enabled: bool
    password: bool
    offscreen: bool
    handle: Any = field(default=None, compare=False)


#: UI Automation control types worth printing, by their documented ids.
DESKTOP_CONTROL_ROLES: Mapping[int, str] = {
    50000: "button",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "hyperlink",
    50007: "listitem",
    50011: "menuitem",
    50013: "radiobutton",
    50015: "slider",
    50019: "tabitem",
    50020: "text",
    50024: "treeitem",
    50029: "dataitem",
    50030: "document",
    50031: "splitbutton",
}
#: The only roles ``desktop_type`` enters text into.
DESKTOP_TEXT_ROLES = frozenset({"edit", "document", "combobox"})
_LABEL_MAX_CHARS = 120


def _label(value: Any) -> str:
    return " ".join(str(value).split())[:_LABEL_MAX_CHARS]


def desktop_window_in_app(window: Any, app: str) -> bool:
    """Whether this live window belongs to the authorized application."""
    return (
        type(window) is DesktopWindow
        and canonical_image_path(window.image) == app
        and window.class_name not in DESKTOP_REFUSED_WINDOW_CLASSES
    )


def desktop_window_rendering(windows: Any, *, app: str) -> str:
    """The bounded, untrusted listing of the authorized application's windows.

    A grant for one application is not a licence to enumerate the operator's
    other windows: a window of any other process is not listed at all.
    """
    lines = []
    for window in windows:
        if len(lines) >= DESKTOP_MAX_WINDOWS:
            break
        if not desktop_window_in_app(window, app):
            continue
        lines.append(
            f"{window.window_id} foreground={'yes' if window.foreground else 'no'} "
            f"minimized={'yes' if window.minimized else 'no'} "
            f"title={json.dumps(_label(window.title), ensure_ascii=True)}"
        )
    rendered, _ = redact_credentials("\n".join(lines))
    return rendered[:EXTERNAL_READ_MAX_CHARS]


def desktop_rendered_elements(elements: Any) -> list[DesktopElement]:
    """The elements an inspection prints, in order - the ONE definition shared
    by the rendering and by the resolution of a ref back to an element."""
    rendered: list[DesktopElement] = []
    for element in elements:
        if len(rendered) >= DESKTOP_MAX_ELEMENTS:
            break
        if type(element) is not DesktopElement or element.offscreen:
            continue
        if element.control_type not in DESKTOP_CONTROL_ROLES:
            continue
        left, top, right, bottom = element.rect
        if right <= left or bottom <= top:
            continue
        rendered.append(element)
    return rendered


def desktop_element_role(element: DesktopElement) -> str:
    return DESKTOP_CONTROL_ROLES.get(element.control_type, "")


def desktop_window_view(window: DesktopWindow, elements: list[DesktopElement]) -> str:
    """The bounded, untrusted rendering of one window's actionable UI.

    Deterministic, so the digest is a usable freshness binding: the same
    window, title, elements, names and positions render to the same bytes.
    UNTRUSTED DATA: every name came from the application. It is redacted for
    credential shapes, and no field VALUE is ever read, so a password - or
    anything else typed into a field - cannot appear in it.
    """
    lines = [
        f"window: {window.window_id}",
        f"title: {json.dumps(_label(window.title), ensure_ascii=True)}",
    ]
    for index, element in enumerate(elements, start=1):
        left, top, right, bottom = element.rect
        flags = ("" if element.enabled else " disabled") + (" password" if element.password else "")
        lines.append(
            f"r{index} {desktop_element_role(element)} "
            f"{json.dumps(_label(element.name), ensure_ascii=True)}{flags} "
            f"@{left},{top},{right - left}x{bottom - top}"
        )
    rendered, _ = redact_credentials("\n".join(lines))
    return rendered[:EXTERNAL_READ_MAX_CHARS]


def desktop_view_digest(rendering: str) -> str:
    return hashlib.sha256(rendering.encode("utf-8")).hexdigest()


def desktop_ref_index(rendering: str, ref: str) -> int:
    """The 1-based position of ``ref`` in this rendering, or fail closed."""
    matches = [line for line in rendering.splitlines() if line.split(" ", 1)[0] == ref]
    if _REF_PATTERN.fullmatch(ref) is None or len(matches) != 1:
        raise DesktopExecutionUnavailable(
            "the target is not identified by exactly one element of this window"
        )
    return int(ref[1:])


# --------------------------------------------------------------------------
# The outcome of one interaction
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DesktopOutcome:
    capability: str
    destination: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "destination": self.destination, "detail": self.detail}


def desktop_outcome(payload: Any, *, frame: DesktopRequestFrame) -> DesktopOutcome:
    """What Windows reported for one interaction, or an UNKNOWN outcome.

    Reached only after the operation was dispatched, so an answer that cannot
    be read is not evidence that nothing happened.
    """
    if not isinstance(payload, dict):
        raise DesktopOutcomeUnknown(f"the {frame.capability} was dispatched and reported nothing")
    if frame.capability == "desktop_app_launch":
        pid, ready = payload.get("pid"), payload.get("ready")
        if type(pid) is not int or pid <= 0 or type(ready) is not bool:
            raise DesktopOutcomeUnknown("the launch was dispatched and reported no process")
        detail = f"pid={pid} ready={'yes' if ready else 'no'}"
    else:
        if payload.get("window_id") != frame.resource.window_id:
            raise DesktopOutcomeUnknown(f"the {frame.capability} was dispatched and reported no window")
        detail = f"window={frame.resource.window_id}"
        if frame.capability != "desktop_window_focus":
            # INJECTED is the strongest thing Windows can say: SendInput put
            # the events in the input stream; what the application did with
            # them is not observable here.
            detail += " input=injected"
    return DesktopOutcome(capability=frame.capability, destination=frame.destination, detail=detail)


# --------------------------------------------------------------------------
# The external read snapshot - the shared transport, the desktop vocabulary
# --------------------------------------------------------------------------


def desktop_external_read_refusal(snapshot: Any) -> str | None:
    reason = external_read_shape_refusal(snapshot)
    if reason is not None:
        return reason
    if snapshot.connector != DESKTOP_CONNECTOR:
        return "the snapshot names no known producer"
    if snapshot.capability not in DESKTOP_READ_CAPABILITIES:
        return "the snapshot capability is not a desktop read capability"
    if not is_desktop_destination(snapshot.destination):
        return "the snapshot destination is not a canonical application"
    if snapshot.resource_kind not in {kind.value for kind in DesktopResourceKind}:
        return "the snapshot resource kind is not a recognized kind"
    return None


def desktop_external_read_binding_refusal(snapshot: Any, *, frame: Any) -> str | None:
    """Why this snapshot does not describe THIS validated request, or ``None``."""
    reason = desktop_frame_refusal(frame) or desktop_external_read_refusal(snapshot)
    if reason is not None:
        return reason
    if (
        snapshot.connector != frame.connector
        or snapshot.capability != frame.capability
        or snapshot.destination != frame.destination
        or snapshot.resource_kind != frame.resource.kind.value
        or snapshot.resource_id != frame.resource.identity
    ):
        return "the snapshot does not describe the requested resource"
    return None


def build_desktop_read_snapshot(
    *, source_task_id: str, frame: Any, content: str, captured_at: str, truncated: bool = False
) -> ExternalReadSnapshot:
    if type(frame) is not DesktopRequestFrame or frame.capability not in DESKTOP_READ_CAPABILITIES:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, "the snapshot names no validated request")
    if type(content) is not str or len(content) > EXTERNAL_READ_MAX_CHARS:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, "the snapshot content is not bounded text")
    if canonical_utc_timestamp(captured_at) is None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, "the capture time is not a canonical UTC timestamp")
    encoded = content.encode("utf-8")
    snapshot = ExternalReadSnapshot(
        source_task_id=source_task_id,
        connector=frame.connector,
        capability=frame.capability,
        destination=frame.destination,
        resource_kind=frame.resource.kind.value,
        resource_id=frame.resource.identity,
        content=content,
        truncated=bool(truncated),
        byte_size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        classification=DataClassification.SECRET,
        captured_at=captured_at,
    )
    reason = desktop_external_read_binding_refusal(snapshot, frame=frame)
    if reason is not None:
        raise AttestationError(ErrorCode.EGRESS_REFUSED, reason)
    return snapshot
