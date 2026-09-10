"""The browser process and protocol seam. Stdlib only.

The counterpart of ``jarvis.gmail_cli``: everything that touches a process,
a socket or the clock lives here, and nothing here decides anything. It
receives a plan ``jarvis.browser`` already validated, sends the closed set of
protocol methods that plan permits, and reports what came back.

WHY THE DEVTOOLS PROTOCOL, AND WHY NO DEPENDENCY. This product ships
``dependencies = []`` and reaches external tools as child processes the
operator already has (``gh``, ``gws``). The Chromium DevTools protocol is the
same kind of seam: the browser is already installed, the protocol is spoken
over a loopback socket, and driving it needs an HTTP GET and a WebSocket -
both of which the standard library can do. So a browser is added without
adding a dependency, a driver binary or a second runtime.

WHAT THE SOCKET WILL NOT SEND. ``CdpConnection`` refuses any method outside
the set the running capability declared, so the whole ``Runtime`` domain
(``evaluate``, ``callFunctionOn``), the whole ``Network`` domain, ``Storage``,
``Fetch``, ``Browser`` and every download/upload method are unreachable - not
by convention but because the connection rejects the name before it is
written to the socket.

WHICH TAB. The protocol has no notion of "the current tab", and the CLI is
one process per command, so the current tab is JARVIS's own state: one file
beside the profile it owns. If it names no live tab and the browser holds
more than one, the browser state is AMBIGUOUS and every operation refuses -
rather than picking one and acting on the wrong page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, Iterable
from urllib.error import URLError
from urllib.request import urlopen

from jarvis.browser import (
    BROWSER_CAPABILITY_METHODS,
    BROWSER_TEXT_ROLES,
    BrowserExecutionUnavailable,
    browser_node_role,
    browser_operation_plan_refusal,
    browser_page_rendering,
    browser_ref_index,
    browser_rendered_nodes,
    browser_staleness_refusal,
    browser_tab_rendering,
    url_in_origin,
)

#: The JARVIS-owned debugging port. Deliberately NOT Chromium's 9222 default:
#: that is where an operator's own ad-hoc debugging session lives, and
#: attaching to it would put JARVIS inside their logged-in browsing profile.
BROWSER_DEBUG_PORT = 9333
#: The one browser this build drives. Chromium-based and present on every
#: supported Windows install; the protocol below is Chromium's.
BROWSER_EXECUTABLE_NAME = "msedge"
_BROWSER_INSTALL_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

BROWSER_LAUNCH_TIMEOUT_SECONDS = 30
BROWSER_COMMAND_TIMEOUT_SECONDS = 30
#: Ceiling on one protocol message. An accessibility tree is the large one.
BROWSER_MESSAGE_MAX_BYTES = 8 * 1024 * 1024
#: How many unrelated protocol events one command may skip before the answer
#: is declared unreadable. Bounded so a chatty page cannot hold a command open.
_MAX_SKIPPED_EVENTS = 500
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CURRENT_TAB_NAME = "current-tab"


class BrowserCommandFailed(BrowserExecutionUnavailable):
    """The command was refused or never dispatched. NOTHING happened."""


class BrowserCommandStarted(BrowserCommandFailed):
    """The command reached the browser. Its effect is not established."""


@dataclass(frozen=True, slots=True)
class BrowserCommandOutput:
    """What one completed operation reports. DATA, and mostly untrusted."""

    payload: dict[str, Any]
    truncated: bool = False


# --------------------------------------------------------------------------
# A minimal WebSocket client (RFC 6455), client side, text frames only
# --------------------------------------------------------------------------


class _WebSocket:
    """One loopback WebSocket connection. No extensions, no fragmenting."""

    def __init__(self, url: str, *, timeout: float) -> None:
        if not url.startswith("ws://"):
            raise BrowserCommandFailed("the browser named no loopback debugging socket")
        authority, _, path = url[len("ws://") :].partition("/")
        host, _, port = authority.partition(":")
        if host not in {"127.0.0.1", "localhost"}:
            raise BrowserCommandFailed("the browser named a socket outside loopback")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        try:
            self._socket = socket.create_connection((host, int(port or 80)), timeout)
            self._socket.settimeout(timeout)
            self._socket.sendall(
                (
                    f"GET /{path} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
            )
        except (OSError, ValueError) as exc:
            raise BrowserCommandFailed(f"the browser debugging socket refused a connection: {exc}") from exc
        self._buffer = bytearray()
        self._handshake(key)

    def _handshake(self, key: str) -> None:
        while b"\r\n\r\n" not in self._buffer:
            if len(self._buffer) > 16 * 1024:
                raise BrowserCommandFailed("the browser debugging handshake is over the bound")
            self._fill()
        head, _, rest = bytes(self._buffer).partition(b"\r\n\r\n")
        self._buffer = bytearray(rest)
        lines = head.decode("latin-1").split("\r\n")
        if "101" not in lines[0]:
            raise BrowserCommandFailed("the browser did not accept the debugging handshake")
        expected = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        accepted = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:])
        }
        if accepted.get("sec-websocket-accept") != expected:
            raise BrowserCommandFailed("the browser debugging handshake did not verify")

    def _fill(self) -> None:
        try:
            chunk = self._socket.recv(65536)
        except OSError as exc:
            raise BrowserCommandFailed(f"the browser debugging socket ended: {exc}") from exc
        if not chunk:
            raise BrowserCommandFailed("the browser debugging socket closed")
        self._buffer += chunk

    def _take(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._fill()
        taken = bytes(self._buffer[:count])
        del self._buffer[:count]
        return taken

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x81, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, size)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self._socket.sendall(header + mask + masked)
        except OSError as exc:
            raise BrowserCommandFailed(f"the browser debugging socket refused a write: {exc}") from exc

    def receive(self) -> str:
        """One complete text message, reassembled across continuations."""
        message = bytearray()
        while True:
            first, second = struct.unpack("!BB", self._take(2))
            opcode = first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._take(8))[0]
            if len(message) + size > BROWSER_MESSAGE_MAX_BYTES:
                raise BrowserCommandFailed("the browser protocol message is over the bound")
            mask = self._take(4) if second & 0x80 else b""
            data = self._take(size)
            if mask:
                data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
            if opcode == 0x8:
                raise BrowserCommandFailed("the browser closed the debugging socket")
            if opcode == 0x9:  # ping
                self._pong(data)
                continue
            if opcode == 0xA:  # pong
                continue
            message += data
            if first & 0x80:  # FIN
                return message.decode("utf-8", "replace")

    def _pong(self, data: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        try:
            self._socket.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) + mask + masked)
        except OSError:  # pragma: no cover - a dead socket fails on the next read
            pass

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:  # pragma: no cover
            pass


class CdpConnection:
    """One protocol connection, bounded to one capability's method set."""

    def __init__(self, socket_url: str, methods: Iterable[str], *, timeout: float) -> None:
        self._methods = frozenset(methods)
        self._socket = _WebSocket(socket_url, timeout=timeout)
        self._next_id = 0

    def command(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Send one method and return its result, or fail closed.

        THE METHOD ALLOWLIST. A method outside the running capability's set is
        refused HERE, before anything is written, so a capability can never
        reach a domain it was not derived for.
        """
        if method not in self._methods:
            raise BrowserCommandFailed(
                f"'{method}' is not a method this browser capability may send"
            )
        self._next_id += 1
        identifier = self._next_id
        message: dict[str, Any] = {"id": identifier, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._socket.send(json.dumps(message, separators=(",", ":")))
        for _ in range(_MAX_SKIPPED_EVENTS):
            try:
                answer = json.loads(self._socket.receive())
            except ValueError as exc:
                raise BrowserCommandFailed("the browser answered with unreadable protocol data") from exc
            if not isinstance(answer, dict) or answer.get("id") != identifier:
                continue  # an event, or another command's answer
            error = answer.get("error")
            if error is not None:
                detail = error.get("message") if isinstance(error, dict) else error
                raise BrowserCommandFailed(f"the browser refused '{method}': {detail}")
            result = answer.get("result")
            return result if isinstance(result, dict) else {}
        raise BrowserCommandFailed(f"the browser did not answer '{method}'")

    def close(self) -> None:
        self._socket.close()


# --------------------------------------------------------------------------
# The browser process
# --------------------------------------------------------------------------


def browser_executable(*, path_lookup: Any = which) -> str:
    """Where the browser is, or fail closed. A LOCATION, never a credential."""
    found = path_lookup(BROWSER_EXECUTABLE_NAME)
    if found:
        return str(found)
    for candidate in _BROWSER_INSTALL_PATHS:
        if Path(candidate).is_file():
            return candidate
    raise BrowserCommandFailed(
        f"'{BROWSER_EXECUTABLE_NAME}' is not installed or not on PATH"
    )


def _endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _debugging_socket(port: int) -> str | None:
    """The browser-level protocol socket, or ``None`` if nothing answers."""
    try:
        with urlopen(f"{_endpoint(port)}/json/version", timeout=2) as answer:
            payload = json.loads(answer.read(64 * 1024).decode("utf-8", "replace"))
    except (URLError, OSError, ValueError):
        return None
    url = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
    return url if isinstance(url, str) and url.startswith("ws://") else None


def browser_arguments(*, profile_dir: str, port: int) -> tuple[str, ...]:
    """The one argument vector this build launches a browser with.

    A DEDICATED profile, so JARVIS never acts inside the operator's own
    logged-in browsing session, and the background services that would make
    the browser talk to its vendor on its own are off.
    """
    return (
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--no-service-autorun",
        "about:blank",
    )


def attached_browser(
    *,
    profile_dir: str,
    port: int = BROWSER_DEBUG_PORT,
    executable_lookup: Any = browser_executable,
    spawn: Any = subprocess.Popen,
    sleep: Any = time.sleep,
) -> str:
    """The debugging socket of JARVIS's browser, launching it if needed.

    Attach first: the CLI is one process per command, so the browser that
    served the previous task is normally still there and must be reused -
    a fresh one would have none of the page state the approved interaction
    was read against.
    """
    existing = _debugging_socket(port)
    if existing is not None:
        return existing
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    executable = executable_lookup()
    try:
        spawn(
            [executable, *browser_arguments(profile_dir=profile_dir, port=port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise BrowserCommandFailed(f"the browser could not be started: {exc}") from exc
    deadline = time.monotonic() + BROWSER_LAUNCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        socket_url = _debugging_socket(port)
        if socket_url is not None:
            return socket_url
        sleep(0.25)
    raise BrowserCommandFailed("the browser did not open a debugging socket in time")


# --------------------------------------------------------------------------
# Which tab - JARVIS's own state, because the protocol has none
# --------------------------------------------------------------------------


def _current_tab_path(profile_dir: str) -> Path:
    return Path(profile_dir).parent / _CURRENT_TAB_NAME


def read_current_tab(profile_dir: str) -> str:
    try:
        return _current_tab_path(profile_dir).read_text(encoding="ascii").strip()
    except OSError:
        return ""


def write_current_tab(profile_dir: str, tab_id: str) -> None:
    path = _current_tab_path(profile_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tab_id, encoding="ascii")
    except OSError:  # pragma: no cover - a lost note re-derives on the next run
        pass


def _page_targets(connection: CdpConnection) -> list[dict[str, Any]]:
    targets = connection.command("Target.getTargets").get("targetInfos")
    if not isinstance(targets, list):
        raise BrowserCommandFailed("the browser reported no tabs")
    return [
        target
        for target in targets
        if isinstance(target, dict) and target.get("type") == "page"
    ]


def _resolve_tab(
    connection: CdpConnection, profile_dir: str, *, origin: str
) -> dict[str, Any]:
    """The one tab this operation acts on, or fail closed as ambiguous.

    The recorded tab wins. With none recorded, exactly ONE live page inside
    the bound origin is unambiguous and anything else is not - JARVIS does
    not choose a page on the operator's behalf.
    """
    targets = _page_targets(connection)
    recorded = read_current_tab(profile_dir)
    if recorded:
        for target in targets:
            if target.get("targetId") == recorded:
                return target
    candidates = [
        target for target in targets if url_in_origin(str(target.get("url", "")), origin)
    ]
    if len(candidates) != 1:
        raise BrowserCommandFailed(
            "the browser state is ambiguous: no single tab of the authorized "
            "origin is current"
        )
    return candidates[0]


def _attach(connection: CdpConnection, tab_id: str) -> str:
    session_id = connection.command(
        "Target.attachToTarget", {"targetId": tab_id, "flatten": True}
    ).get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise BrowserCommandFailed("the browser did not attach to the tab")
    return session_id


def _page_state(connection: CdpConnection, session_id: str) -> tuple[str, str]:
    """The live URL and title of the attached tab."""
    history = connection.command("Page.getNavigationHistory", session_id=session_id)
    entries = history.get("entries")
    index = history.get("currentIndex")
    if not isinstance(entries, list) or not isinstance(index, int) or not 0 <= index < len(entries):
        raise BrowserCommandFailed("the browser reported no page state")
    entry = entries[index]
    if not isinstance(entry, dict):
        raise BrowserCommandFailed("the browser reported no page state")
    return str(entry.get("url", "")), str(entry.get("title", ""))


def _rendering(connection: CdpConnection, session_id: str) -> tuple[str, list[dict[str, Any]], str]:
    """The bounded rendering, its nodes and the live URL of the attached tab."""
    connection.command("DOM.enable", session_id=session_id)
    connection.command("Accessibility.enable", session_id=session_id)
    url, title = _page_state(connection, session_id)
    nodes = connection.command("Accessibility.getFullAXTree", session_id=session_id).get("nodes")
    return (
        browser_page_rendering(nodes, url=url, title=title),
        browser_rendered_nodes(nodes),
        url,
    )


def _element(
    connection: CdpConnection, session_id: str, node: dict[str, Any]
) -> tuple[int, float, float]:
    """The backend node id and the point to act on, or fail closed.

    Three independent reasons to refuse, all of them "the intended element
    cannot be identified confidently": the node has no backend identity, it
    occupies no area on the page, or something else is on top of the point
    that would be acted on. The last is the one that matters in practice - a
    consent banner or a modal over the approved button - and guessing through
    it would act on the overlay instead.
    """
    backend_id = node.get("backendDOMNodeId")
    if not isinstance(backend_id, int):
        raise BrowserCommandFailed("the target element has no identity on this page")
    connection.command(
        "DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id}, session_id=session_id
    )
    try:
        model = connection.command(
            "DOM.getBoxModel", {"backendNodeId": backend_id}, session_id=session_id
        ).get("model")
    except BrowserCommandFailed as exc:
        raise BrowserCommandFailed(f"the target element is not on the page: {exc}") from exc
    quad = model.get("content") if isinstance(model, dict) else None
    if not isinstance(quad, list) or len(quad) != 8:
        raise BrowserCommandFailed("the target element occupies no area on the page")
    xs, ys = quad[0::2], quad[1::2]
    if max(xs) - min(xs) <= 0 or max(ys) - min(ys) <= 0:
        raise BrowserCommandFailed("the target element occupies no area on the page")
    x, y = sum(xs) / 4.0, sum(ys) / 4.0
    hit = connection.command(
        "DOM.getNodeForLocation",
        {"x": int(x), "y": int(y), "includeUserAgentShadowDOM": False},
        session_id=session_id,
    ).get("backendNodeId")
    if hit != backend_id and hit not in _subtree(connection, session_id, backend_id):
        raise BrowserCommandFailed(
            "another element covers the target, so the approved element cannot "
            "be acted on confidently"
        )
    return backend_id, x, y


def _subtree(connection: CdpConnection, session_id: str, backend_id: int) -> frozenset[int]:
    """Every backend node id inside the target, so its own text counts as it."""
    described = connection.command(
        "DOM.describeNode",
        {"backendNodeId": backend_id, "depth": -1, "pierce": True},
        session_id=session_id,
    ).get("node")
    found: set[int] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict) or len(found) > 1_000:
            return
        identifier = node.get("backendNodeId")
        if isinstance(identifier, int):
            found.add(identifier)
        for child in node.get("children") or ():
            walk(child)
        for child in node.get("shadowRoots") or ():
            walk(child)

    walk(described)
    return frozenset(found)


def _describe(connection: CdpConnection, session_id: str, backend_id: int) -> dict[str, Any]:
    node = connection.command(
        "DOM.describeNode", {"backendNodeId": backend_id}, session_id=session_id
    ).get("node")
    return node if isinstance(node, dict) else {}


def secret_field_refusal(node: dict[str, Any]) -> str | None:
    """Why JARVIS will not type into this element, or ``None``.

    A password field is refused OUTRIGHT. Whatever the text is, entering it
    here would be JARVIS operating a credential prompt on the operator's
    behalf, which this product does not do.
    """
    attributes = node.get("attributes")
    pairs = dict(zip(attributes[0::2], attributes[1::2])) if isinstance(attributes, list) else {}
    if str(node.get("nodeName", "")).upper() == "INPUT":
        if str(pairs.get("type", "")).lower() == "password":
            return "the target is a password field and JARVIS does not type credentials"
    if str(pairs.get("autocomplete", "")).lower().endswith("password"):
        return "the target is a password field and JARVIS does not type credentials"
    return None


# --------------------------------------------------------------------------
# The one runner - one validated plan, one operation
# --------------------------------------------------------------------------


def run_operation_plan(
    plan: Any,
    *,
    frame: Any,
    profile_dir: str,
    port: int = BROWSER_DEBUG_PORT,
    attach: Any = attached_browser,
    sleep: Any = time.sleep,
    connection_sink: Any = None,
) -> BrowserCommandOutput:
    """Perform one validated browser operation and report what came back.

    THE DISPATCH LINE. Everything before the browser is asked to ACT raises
    ``BrowserCommandFailed`` and means nothing happened. Everything after it
    raises ``BrowserCommandStarted``, which the connector turns into an
    unknown outcome rather than into "it did not happen".
    """
    reason = browser_operation_plan_refusal(plan)
    if reason is not None:
        raise BrowserCommandFailed(reason)
    socket_url = attach(profile_dir=profile_dir, port=port)
    connection = CdpConnection(
        socket_url, BROWSER_CAPABILITY_METHODS[plan.capability], timeout=BROWSER_COMMAND_TIMEOUT_SECONDS
    )
    # The owner keeps a handle on the live connection for exactly one reason:
    # a cancellation must be able to close it. Cleared on the way out so a
    # later cancel can never signal a connection that is already gone.
    if connection_sink is not None:
        connection_sink(connection)
    try:
        return _perform(connection, plan, frame=frame, profile_dir=profile_dir, sleep=sleep)
    finally:
        if connection_sink is not None:
            connection_sink(None)
        connection.close()


def _perform(
    connection: CdpConnection, plan: Any, *, frame: Any, profile_dir: str, sleep: Any
) -> BrowserCommandOutput:
    resource = plan.resource
    origin = resource.origin
    capability = plan.capability

    if capability == "browser_tab_list":
        content = browser_tab_rendering(
            connection.command("Target.getTargets").get("targetInfos"), origin=origin
        )
        return BrowserCommandOutput({"content": content})

    if capability == "browser_tab_open":
        created = connection.command("Target.createTarget", {"url": resource.url})
        tab_id = str(created.get("targetId", ""))
        write_current_tab(profile_dir, tab_id)
        return BrowserCommandOutput({"tab_id": tab_id, "url": resource.url})

    if capability in {"browser_tab_switch", "browser_tab_close"}:
        target = next(
            (
                candidate
                for candidate in _page_targets(connection)
                if candidate.get("targetId") == resource.tab_id
            ),
            None,
        )
        if target is None:
            raise BrowserCommandFailed("the browser holds no such tab")
        url = str(target.get("url", ""))
        if not url_in_origin(url, origin):
            raise BrowserCommandFailed("the tab is outside the authorized origin")
        if capability == "browser_tab_switch":
            connection.command("Target.activateTarget", {"targetId": resource.tab_id})
            write_current_tab(profile_dir, resource.tab_id)
            return BrowserCommandOutput({"tab_id": resource.tab_id, "url": url})
        connection.command("Target.closeTarget", {"targetId": resource.tab_id})
        if read_current_tab(profile_dir) == resource.tab_id:
            write_current_tab(profile_dir, "")
        return BrowserCommandOutput({"tab_id": resource.tab_id, "url": ""})

    if capability == "browser_wait":
        sleep(resource.seconds)

    target = _resolve_tab(connection, profile_dir, origin=origin)
    tab_id = str(target.get("targetId", ""))
    session_id = _attach(connection, tab_id)

    if capability == "browser_navigate":
        # The only capability that may act on a tab standing outside the
        # bound origin: arriving there is what it is for. Where it LANDED is
        # judged by the connector against the same origin.
        answer = connection.command(
            "Page.navigate", {"url": resource.url}, session_id=session_id
        )
        if answer.get("errorText"):
            raise BrowserCommandStarted(
                f"the browser reported a navigation error: {answer.get('errorText')}"
            )
        try:
            url, _ = _page_state(connection, session_id)
        except BrowserCommandFailed as exc:
            raise BrowserCommandStarted(f"the navigation was not acknowledged: {exc}") from exc
        write_current_tab(profile_dir, tab_id)
        return BrowserCommandOutput({"tab_id": tab_id, "url": url})

    rendering, nodes, url = _rendering(connection, session_id)
    if not url_in_origin(url, origin):
        raise BrowserCommandFailed(
            "the tab is no longer inside the authorized origin, so the approved "
            "action does not match the browser state"
        )

    if capability in {"browser_page_read", "browser_wait"}:
        write_current_tab(profile_dir, tab_id)
        return BrowserCommandOutput({"content": rendering})

    # The two element interactions. Staleness first: a page that moved since
    # it was read is a page the approval was never for.
    reason = browser_staleness_refusal(rendering, frame=frame)
    if reason is not None:
        raise BrowserCommandFailed(reason)
    index = browser_ref_index(rendering, resource.ref)
    if index > len(nodes):  # pragma: no cover - the digest already matched
        raise BrowserCommandFailed("the target is not an element of this page")
    node = nodes[index - 1]
    if capability == "browser_type" and browser_node_role(node) not in BROWSER_TEXT_ROLES:
        raise BrowserCommandFailed("the target does not accept typed text")
    backend_id, x, y = _element(connection, session_id, node)

    if capability == "browser_type":
        reason = secret_field_refusal(_describe(connection, session_id, backend_id))
        if reason is not None:
            raise BrowserCommandFailed(reason)
        connection.command("DOM.focus", {"backendNodeId": backend_id}, session_id=session_id)
        try:
            connection.command("Input.insertText", {"text": resource.text}, session_id=session_id)
        except BrowserCommandFailed as exc:
            raise BrowserCommandStarted(f"the text entry was not acknowledged: {exc}") from exc
    else:
        for event in ("mousePressed", "mouseReleased"):
            try:
                connection.command(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event,
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": 1,
                        "buttons": 1 if event == "mousePressed" else 0,
                    },
                    session_id=session_id,
                )
            except BrowserCommandFailed as exc:
                raise BrowserCommandStarted(f"the click was not acknowledged: {exc}") from exc

    try:
        landed, _ = _page_state(connection, session_id)
    except BrowserCommandFailed as exc:
        raise BrowserCommandStarted(f"the page state after the action is unknown: {exc}") from exc
    write_current_tab(profile_dir, tab_id)
    return BrowserCommandOutput({"tab_id": tab_id, "url": landed})


__all__ = [
    "BROWSER_COMMAND_TIMEOUT_SECONDS",
    "BROWSER_DEBUG_PORT",
    "BROWSER_EXECUTABLE_NAME",
    "BROWSER_LAUNCH_TIMEOUT_SECONDS",
    "BROWSER_MESSAGE_MAX_BYTES",
    "BrowserCommandFailed",
    "BrowserCommandOutput",
    "BrowserCommandStarted",
    "CdpConnection",
    "attached_browser",
    "browser_arguments",
    "browser_executable",
    "read_current_tab",
    "run_operation_plan",
    "secret_field_refusal",
    "write_current_tab",
]
