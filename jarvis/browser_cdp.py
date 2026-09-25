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
the set it was opened with, so ``Runtime.evaluate`` and ``callFunctionOn``,
the whole ``Network`` domain, ``Storage``, ``Browser`` and every
download/upload method are unreachable - not by convention but because the
connection rejects the name before it is written to the socket. An operation's
set is the one its capability declares; ``Fetch`` appears only in the two sets
that gate the network. The whole ``Runtime`` domain is unreachable from every
set in this build: nothing here evaluates anything.

WHOSE BROWSER. The one JARVIS drives is one JARVIS started and the operating
system will terminate with it: launched suspended, bound to a private Windows
job object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, and only then resumed
(``jarvis.owned_process``). That is what makes the standing network policy
meaningful - every part of that policy is a JARVIS object, so a browser able
to outlive JARVIS is a loaded untrusted page nothing is judging. Something
else answering on the debugging port is therefore refused rather than adopted,
and no browser is started at all if it cannot be owned.

WHICH TAB. The protocol has no notion of "the current tab", and the CLI is
one process per command, so the current tab is JARVIS's own state: one file
beside the profile it owns. If it names no live tab and the browser holds
more than one, the browser state is AMBIGUOUS and every operation refuses -
rather than picking one and acting on the wrong page.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from jarvis.policy_proxy import PolicyProxy, proxy_arguments
from jarvis.owned_process import (
    OwnedProcess,
    ProcessOwnershipError,
    launch_owned,
    listening_pid,
)
from jarvis.trusted_exec import (
    TrustedExecutable,
    TrustedExecutableError,
    resolve_trusted_executable,
)
from jarvis.browser import (
    BROWSER_CAPABILITY_METHODS,
    BROWSER_POLICY_METHODS,
    BROWSER_MAX_NAVIGATION_EVENTS,
    BROWSER_MAX_REDIRECT_HOPS,
    BROWSER_TEXT_ROLES,
    BrowserExecutionUnavailable,
    BrowserOriginBlocked,
    browser_node_role,
    browser_operation_plan_refusal,
    browser_page_rendering,
    browser_ref_index,
    browser_rendered_nodes,
    browser_staleness_refusal,
    browser_tab_id_refusal,
    browser_tab_rendering,
    request_destination_refusal,
    url_in_origin,
)

#: The JARVIS-owned debugging port. Deliberately NOT Chromium's 9222 default:
#: that is where an operator's own ad-hoc debugging session lives, and
#: attaching to it would put JARVIS inside their logged-in browsing profile.
BROWSER_DEBUG_PORT = 9333
#: The one browser this build drives: Google Chrome, and only a copy installed
#: for the whole machine. The protocol below is Chromium's.
BROWSER_EXECUTABLE_NAME = "chrome"
#: The ONLY places the browser may come from: the system-wide installation
#: roots, which are administrator-controlled. A per-user Chrome under
#: ``%LOCALAPPDATA%`` is deliberately absent - that directory is writable by
#: the same account JARVIS runs as, so a file there is no more trustworthy
#: than one in the workspace (DB-001). ``PATH`` is not consulted at all for
#: the browser, for the same reason: any entry of it can be user-writable.
_BROWSER_INSTALL_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
#: Handed to the resolver as its environment: no ``PATH``, so the enumeration
#: it would otherwise fall back to yields nothing and the install roots above
#: are the whole candidate set.
_NO_PATH_SEARCH: dict[str, str] = {}

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
        #: Events that arrived while a reply was being awaited. BUFFERED, not
        #: dropped: a request the browser paused mid-navigation is delivered as
        #: an event, and discarding it would leave that request hanging while
        #: the seam waited for it forever.
        self._events: list[dict[str, Any]] = []

    def send(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str = ""
    ) -> int:
        """Write one method and return its id, without awaiting the reply.

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
        return identifier

    def _frame(self) -> dict[str, Any]:
        try:
            answer = json.loads(self._socket.receive())
        except ValueError as exc:
            raise BrowserCommandFailed("the browser answered with unreadable protocol data") from exc
        if not isinstance(answer, dict):
            raise BrowserCommandFailed("the browser answered with unreadable protocol data")
        return answer

    @staticmethod
    def _result(method: str, answer: dict[str, Any]) -> dict[str, Any]:
        error = answer.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, dict) else error
            raise BrowserCommandFailed(f"the browser refused '{method}': {detail}")
        result = answer.get("result")
        return result if isinstance(result, dict) else {}

    def command(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Send one method and return its result, or fail closed."""
        identifier = self.send(method, params, session_id=session_id)
        for _ in range(_MAX_SKIPPED_EVENTS):
            answer = self._frame()
            if answer.get("id") != identifier:
                if "id" not in answer:
                    self._events.append(answer)
                continue  # an event, or another command's answer
            return self._result(method, answer)
        raise BrowserCommandFailed(f"the browser did not answer '{method}'")

    def exchange(
        self, identifier: int, method: str, events: tuple[str, ...]
    ) -> tuple[str, dict[str, Any]]:
        """The next frame that is either ``identifier``'s reply or one of ``events``.

        The one place the seam listens to the browser rather than asking it.
        Bounded like every other read, and deliberately narrow: one awaited
        reply and a fixed, caller-named handful of events, so nothing here
        becomes a general event loop. Everything else is buffered, never acted
        on.
        """
        for _ in range(_MAX_SKIPPED_EVENTS):
            buffered = next((one for one in self._events if one.get("method") in events), None)
            if buffered is not None:
                self._events.remove(buffered)
                return (str(buffered.get("method")), buffered.get("params") or {})
            answer = self._frame()
            if answer.get("id") == identifier:
                return ("reply", self._result(method, answer))
            if answer.get("method") in events:
                return (str(answer.get("method")), answer.get("params") or {})
            if "id" not in answer:
                self._events.append(answer)
        raise BrowserCommandFailed(f"the browser did not answer '{method}'")

    def take_buffered(self, event: str) -> list[dict[str, Any]]:
        """Every already-received ``event`` still sitting in the buffer.

        Only used to settle requests the seam had not yet judged when a
        navigation stopped being followable, so that disarming the gate cannot
        release one of them onto the wire.
        """
        return [one.get("params") or {} for one in self.take_buffered_frames(event)]

    def take_buffered_frames(self, event: str) -> list[dict[str, Any]]:
        """The same, as WHOLE frames - the params and the session they name.

        The standing policy attaches to more than one session (a page, and
        the workers and out-of-process frames that page auto-attaches), and a
        paused request has to be dispositioned on the session it arrived on.
        ``take_buffered`` drops that, because the navigation gate only ever
        deals with the one session it attached itself.
        """
        taken = [one for one in self._events if one.get("method") == event]
        self._events = [one for one in self._events if one.get("method") != event]
        return taken

    def keep_only_buffered(self, events: tuple[str, ...]) -> None:
        """Forget every buffered event that is not one of ``events``.

        A connection that lives as long as a tab accumulates everything the
        browser volunteers - frame lifecycle, resize, target chatter - and
        none of it is ever read, so without this the buffer is a leak that
        grows for as long as the page is open. Only the caller's own names
        survive, and it calls this AFTER taking them, so nothing it has to
        act on can be dropped here.
        """
        self._events = [one for one in self._events if one.get("method") in events]

    def close(self) -> None:
        self._socket.close()


# --------------------------------------------------------------------------
# The browser process
# --------------------------------------------------------------------------


def trusted_browser_lookup(
    name: str, *, install_paths: Iterable[Any] = _BROWSER_INSTALL_PATHS
) -> TrustedExecutable | None:
    """The browser's provenance rule: the system installation roots, and
    nothing else. Not ``PATH``, not the current directory, not the workspace
    JARVIS was pointed at, not a per-user install - a ``chrome.exe`` in any of
    those is not a browser (DB-001). ``jarvis.trusted_exec`` still applies its
    own rules to each root: a real file, canonical, outside every forbidden
    directory.

    Returns the TRUSTED IDENTITY, not a bare path: the caller keeps it until
    the moment of process creation and re-checks it there (``argv0``).

    The file must still be NAMED ``chrome`` once canonicalised: an install
    root that turns out to be a link to some other program is refused."""
    found = resolve_trusted_executable(
        name, install_paths=install_paths, environ=_NO_PATH_SEARCH
    )
    if found is not None and Path(found.path).stem.lower() != name.lower():
        return None
    return found


def browser_executable(*, path_lookup: Any = trusted_browser_lookup) -> TrustedExecutable:
    """The browser as a trusted identity, or fail closed. A LOCATION, never a
    credential, and a location that will be re-verified before it is run."""
    found = path_lookup(BROWSER_EXECUTABLE_NAME)
    if found is not None:
        return found
    raise BrowserCommandFailed(
        "Google Chrome is not installed for this machine (under Program Files), "
        "so JARVIS has no browser to start"
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


def browser_arguments(*, profile_dir: str, port: int, proxy_port: int) -> tuple[str, ...]:
    """The one argument vector this build launches a browser with.

    A DEDICATED profile, so JARVIS never acts inside the operator's own
    logged-in browsing session, and the background services that would make
    the browser talk to its vendor on its own are off.

    ``proxy_port`` is REQUIRED and has no default on purpose. It names the
    JARVIS-owned pre-connect boundary, and a browser launched without it would
    resolve and connect for itself - which is the DNS rebinding exposure this
    argument exists to remove (DB-002B-02). A missing default means no call
    site can quietly produce an unproxied browser.
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
        *proxy_arguments(proxy_port),
        "about:blank",
    )


#: The WebRTC IP handling policy the browser profile is held to. The
#: ``--force-webrtc-ip-handling-policy`` switch in ``proxy_arguments`` reaches
#: only the browser process's command line; Chrome 153 (and Edge 153) still
#: let the NetworkService gather host candidates and send STUN over UDP from a
#: page, straight past the proxy. The profile PREFERENCE is what the network
#: service honours (measured: 0 candidates, 0 STUN packets), so it is written
#: into the JARVIS-owned profile before every spawn. The switch stays as
#: defence in depth.
WEBRTC_IP_HANDLING_POLICY = "disable_non_proxied_udp"


def _preferences_path(profile_dir: str) -> Path:
    return Path(profile_dir) / "Default" / "Preferences"


def enforce_profile_policy(profile_dir: str) -> Path:
    """Hold the JARVIS-owned profile's ``Default/Preferences`` to the WebRTC policy.

    Called on the SPAWN path only, when no browser JARVIS owns is running,
    so nothing is writing the file concurrently. Every other preference is
    preserved: the file is parsed, one value is set, and the result replaces
    the file atomically through a same-directory temporary file. A file that
    cannot be read or parsed is not guessed at - the browser is NOT started
    (fail closed), and the original is left exactly as it was. Nothing of
    the file's content is ever put in an error message.
    """
    path = _preferences_path(profile_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        preferences: Any = {}
    except OSError as exc:
        raise BrowserCommandFailed(
            f"the browser profile's Preferences file could not be read, so the "
            f"WebRTC policy could not be enforced and the browser was not started: "
            f"{exc.__class__.__name__}"
        ) from exc
    else:
        try:
            preferences = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BrowserCommandFailed(
                "the browser profile's Preferences file is not readable JSON, so the "
                "WebRTC policy could not be enforced and the browser was not started"
            ) from exc
    webrtc = preferences.get("webrtc") if isinstance(preferences, dict) else None
    if not isinstance(preferences, dict) or (webrtc is not None and not isinstance(webrtc, dict)):
        raise BrowserCommandFailed(
            "the browser profile's Preferences file does not have the structure the "
            "WebRTC policy is written into, so the browser was not started"
        )
    if webrtc is None:
        webrtc = preferences["webrtc"] = {}
    if webrtc.get("ip_handling_policy") == WEBRTC_IP_HANDLING_POLICY:
        return path
    webrtc["ip_handling_policy"] = WEBRTC_IP_HANDLING_POLICY
    temporary = path.with_name(f"{path.name}.jarvis-{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(preferences, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise BrowserCommandFailed(
            f"the browser profile's Preferences file could not be updated with the "
            f"WebRTC policy, so the browser was not started: {exc.__class__.__name__}"
        ) from exc
    return path


#: The browser this process launched and still owns, by debugging port. The
#: value is the OS-level lifetime binding, and holding it here is what keeps
#: the browser alive: drop it and the job closes and Windows kills the tree.
_OWNED_BROWSERS: dict[int, OwnedProcess] = {}


def owned_browser(port: int = BROWSER_DEBUG_PORT) -> OwnedProcess | None:
    """The browser JARVIS owns on ``port``, if it is still owned and running.

    BOTH halves are asked, because they are different claims. ``alive`` says
    the process JARVIS started has not exited. ``contained`` says the kernel
    still counts it inside the job whose closing terminates it - which is the
    only reason to believe it cannot outlive JARVIS. A process that is running
    but no longer in the job is not JARVIS's browser any more, whatever
    started it.
    """
    owned = _OWNED_BROWSERS.get(port)
    if owned is None:
        return None
    if owned.alive() and owned.contained():
        return owned
    # It died, or it left the job. Either way the binding is over: let go of
    # the job, which also kills anything of that tree still standing.
    _OWNED_BROWSERS.pop(port, None)
    owned.close()
    return None


def release_owned_browsers(port: int | None = None) -> None:
    """Close the lifetime binding, which terminates the browser tree.

    Used by the tests and by a restart, never as the guarantee: the guarantee
    is that Windows does this for JARVIS when JARVIS stops existing, whether
    or not this function was reachable.
    """
    for owned_port in [port] if port is not None else list(_OWNED_BROWSERS):
        owned = _OWNED_BROWSERS.pop(owned_port, None)
        if owned is not None:
            owned.close()
    for owned_port in [port] if port is not None else list(_POLICY_PROXIES):
        proxy = _POLICY_PROXIES.pop(owned_port, None)
        if proxy is not None:
            proxy.stop()


#: The pre-connect boundary serving the browser on each debugging port. In
#: process, bound to loopback, and holding no credential: a browser cannot
#: reach the network except through the one that serves it.
_POLICY_PROXIES: dict[int, PolicyProxy] = {}


def browser_policy_proxy(port: int = BROWSER_DEBUG_PORT) -> PolicyProxy:
    """The running pre-connect boundary for ``port``, starting one if needed.

    Started BEFORE the browser that will be pointed at it, so there is no
    moment in which the browser is running with a proxy it cannot reach - a
    browser in that state fails its requests, which is safe but useless.
    """
    proxy = _POLICY_PROXIES.get(port)
    if proxy is not None and proxy.running:
        return proxy
    if proxy is not None:  # pragma: no cover - a listening socket does not die
        proxy.stop()
    proxy = PolicyProxy()
    proxy.start()
    _POLICY_PROXIES[port] = proxy
    return proxy


def attached_browser(
    *,
    profile_dir: str,
    port: int = BROWSER_DEBUG_PORT,
    executable_lookup: Any = browser_executable,
    spawn: Any = launch_owned,
    sleep: Any = time.sleep,
) -> str:
    """The debugging socket of JARVIS's OWNED browser, launching it if needed.

    REUSE ONLY WHAT IS OWNED (DB-002B-01). Something answering on JARVIS's
    debugging port is not evidence that it is JARVIS's browser. It could be
    an operator's own ``--remote-debugging-port`` session, a different build,
    a browser left behind by something else, or a process pretending to be
    one - and driving any of those would put JARVIS's approved pages inside a
    browser whose lifetime, profile and network mediation JARVIS does not
    control. So an endpoint is used only when this process launched it and
    the kernel still holds it in the job that kills it with JARVIS.

    Anything else is REFUSED rather than adopted, and refused rather than
    worked around: the port is taken, so a fresh managed browser cannot be
    started on it either, and starting an unmanaged one is the thing this
    exists to prevent.

    A browser is still reused ACROSS OPERATIONS within one JARVIS run, which
    is what the interaction capabilities need - the page state an approved
    click was read against lives in it. What no longer happens is reuse across
    JARVIS runs, and that is not a loss: with the lifetime binding in place
    there is no browser left over from a previous run to reuse.

    ``executable_lookup`` returns a trusted identity (``TrustedExecutable``),
    never a bare path; what reaches ``spawn`` as ``argv[0]`` is that
    identity's ``argv0()``, re-validated at the spawn.
    """
    owned = owned_browser(port)
    existing = _debugging_socket(port)
    if existing is not None:
        # OWNERSHIP OF THIS ENDPOINT, not merely of a browser. The process
        # LISTENING on the port has to be one the job contains; a browser of
        # ours that failed to bind while something else held the port would
        # otherwise read as ownership of that something else.
        if owned is not None and owned.contains(listening_pid(port)):
            proxy = _POLICY_PROXIES.get(port)
            if proxy is None or not proxy.running:
                raise BrowserCommandFailed(
                    "the browser's pre-connect boundary is not running, so where "
                    "its requests would connect cannot be decided; the browser "
                    "was not driven"
                )
            return existing
        raise BrowserCommandFailed(
            "something is already listening on JARVIS's browser debugging "
            "port and it is not a browser JARVIS owns, so it was refused "
            "rather than driven; close it and try again"
        )
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    # THE PROFILE POLICY BEFORE THE PROCESS. Only a browser this process
    # spawned is ever reused (above), and every spawn passes through here
    # first, so no browser JARVIS drives was started against a profile that
    # lacked the policy. On this path nothing JARVIS owns is running, so the
    # file is not being written by a browser while it is replaced.
    enforce_profile_policy(profile_dir)
    trusted = executable_lookup()
    # THE BOUNDARY FIRST. The browser is told to reach the network only
    # through this, so it exists before the browser does (DB-002B-02).
    proxy = browser_policy_proxy(port)
    # THE IDENTITY LAST, immediately before process creation: the file that
    # was accepted as the browser must still be that file - same canonical
    # path, same volume and index, same size and mtime - or nothing is
    # started. A swap between locating it and running it is caught here
    # rather than executed (DB-001).
    try:
        argv0 = trusted.argv0()
    except TrustedExecutableError as exc:
        raise BrowserCommandFailed(
            f"the browser executable is no longer the one that was located, so "
            f"it was not started: {exc}"
        ) from exc
    try:
        started = spawn(
            [
                argv0,
                *browser_arguments(
                    profile_dir=profile_dir, port=port, proxy_port=proxy.port
                ),
            ]
        )
    except (OSError, ProcessOwnershipError) as exc:
        # NO UNMANAGED FALLBACK. A browser whose lifetime cannot be bound to
        # JARVIS's is a browser that could outlive it holding a loaded page,
        # so it is not started at all.
        raise BrowserCommandFailed(f"the browser could not be started: {exc}") from exc
    _OWNED_BROWSERS[port] = started
    deadline = time.monotonic() + BROWSER_LAUNCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        socket_url = _debugging_socket(port)
        if socket_url is not None:
            # THE SAME QUESTION AS ON REUSE, asked again here: the port was
            # free when the browser was started, and something else could have
            # taken it while the browser was still coming up. Whatever answers
            # must be inside the job, or it is not what JARVIS started.
            if not started.contains(listening_pid(port)):
                release_owned_browsers(port)
                raise BrowserCommandFailed(
                    "something other than the browser JARVIS started answered "
                    "on its debugging port, so it was refused rather than driven"
                )
            return socket_url
        if not started.alive():
            release_owned_browsers(port)
            raise BrowserCommandFailed("the browser exited before it opened a debugging socket")
        sleep(0.25)
    # It never answered. Do not leave an owned-but-unusable browser running.
    release_owned_browsers(port)
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


#: The fail-closed refusal when no single tab of the authorized origin is
#: current (a fresh JARVIS browser has none). A constant so the interface can
#: explain this one established cause in the person's language.
NO_CURRENT_TAB_MESSAGE = (
    "the browser state is ambiguous: no single tab of the authorized origin is current"
)


def owning_policy(
    policies: dict[str, BrowserPolicy] | None, tab_id: str, context_id: str = ""
) -> BrowserPolicy | None:
    """The live policy that FULLY owns this tab, or ``None``.

    Ownership is not "a policy object exists". It is the whole relationship
    the tab was created with, and every part of it is asked for:

      * mediation is alive - the renderer gate is still judging;
      * the tab's own boundary exists AND is still listening - the thing that
        decides where this tab may connect, including for a WebSocket that no
        renderer gate can see;
      * the browser context that routes the tab to that boundary is still the
        one the policy recorded.

    A tab missing any of these is not a JARVIS tab. It may be a target left
    behind by a startup that failed half way, and adopting one of those is how
    a page ends up reachable through a boundary bound to nothing.
    """
    if policies is None:
        return None
    policy = policies.get(tab_id)
    if policy is None or not policy.alive:
        return None
    if policy.proxy is None or not policy.proxy.running:
        return None
    if not policy.context_id:
        return None
    # The browser's own account of which context the target is in, where it
    # gives one. A tab in a different context reaches a different boundary.
    if context_id and context_id != policy.context_id:
        return None
    return policy


def discard_tab(
    connection: CdpConnection,
    policies: dict[str, BrowserPolicy] | None,
    profile_dir: str,
    tab_id: str,
) -> None:
    """Forget a tab that is not owned, and take away what can be taken away.

    BEST EFFORT, FAIL CLOSED. Each step is attempted independently and a step
    that fails does not stop the others, because the property that matters is
    not "everything was cleaned up" - it is that nothing here leaves the tab
    resolvable. The bookkeeping is cleared last and unconditionally.
    """
    with _suppressed():
        connection.command("Target.closeTarget", {"targetId": tab_id})
    _drop_policy(policies, tab_id)
    if read_current_tab(profile_dir) == tab_id:
        write_current_tab(profile_dir, "")


def _resolve_tab(
    connection: CdpConnection,
    profile_dir: str,
    *,
    origin: str,
    policies: dict[str, BrowserPolicy] | None = None,
) -> dict[str, Any]:
    """The one tab this operation acts on, or fail closed as ambiguous.

    A TARGET ID IS NOT A TAB. In production every JARVIS tab was created by
    ``browser_tab_open`` inside its own browser context and behind its own
    boundary, and is owned by a live policy holding both. A target that no
    live policy owns - a blank one left behind by a startup that failed part
    way through, for instance - is not resolvable here, whatever the
    current-tab note says about it. Without that rule a later navigation
    could adopt such a target and build mediation over it with no boundary at
    all, which is a page that can reach anywhere (transactional startup).

    The recorded tab wins, when it is owned. With none recorded, exactly ONE
    owned live page inside the bound origin is unambiguous and anything else
    is not - JARVIS does not choose a page on the operator's behalf.
    """
    targets = _page_targets(connection)
    recorded = read_current_tab(profile_dir)
    if policies is None:
        # No registry: the caller mediates the tab itself. Only a test does
        # this; production always passes the one registry there is.
        if recorded:
            for target in targets:
                if target.get("targetId") == recorded:
                    return target
        candidates = [
            target for target in targets if url_in_origin(str(target.get("url", "")), origin)
        ]
        if len(candidates) != 1:
            raise BrowserCommandFailed(NO_CURRENT_TAB_MESSAGE)
        return candidates[0]
    owned = {
        str(target.get("targetId", "")): target
        for target in targets
        if owning_policy(
            policies,
            str(target.get("targetId", "")),
            str(target.get("browserContextId", "")),
        )
        is not None
    }
    if recorded:
        if recorded in owned:
            return owned[recorded]
        # Recorded but not owned: the note is stale or the tab was never
        # finished. Take it away rather than leaving it to be found again.
        discard_tab(connection, policies, profile_dir, recorded)
    candidates = [
        target for target in owned.values() if url_in_origin(str(target.get("url", "")), origin)
    ]
    if len(candidates) != 1:
        raise BrowserCommandFailed(NO_CURRENT_TAB_MESSAGE)
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


#: The empty document a new tab is created at. JARVIS's own constant, never a
#: task-supplied URL: it exists so the tab is attached and gated BEFORE the
#: approved navigation is sent, instead of racing a load already in flight.
BROWSER_BLANK_PAGE = "about:blank"

#: EVERY renderer request is paused, not only the top-level document (DB-002).
#: Gating the document alone made the approval a gate on where JARVIS asked
#: the tab to GO, while the page itself stayed free to fetch from anywhere -
#: an image, a script, a stylesheet, a font, an XHR, a form post or a frame
#: could reach an unapproved origin, this machine's loopback services or the
#: operator's intranet, none of which approving one public origin authorizes.
#: No ``resourceType`` therefore means all of them; the judgement is
#: ``request_destination_refusal``, applied identically to each.
_ALL_REQUESTS_PATTERN = [{"urlPattern": "*", "requestStage": "Request"}]

#: How many quiet pump rounds end the settle phase after a document commits.
#: The page's own subresource cascade has to be SERVICED, not stranded: a
#: paused request the gate never dispositions is released by ``Fetch.disable``
#: (fail-open) or killed with the tab (an unusable page), so the gate stays
#: armed and keeps answering until the tab stops asking.
_SETTLE_QUIET_ROUNDS = 2
#: Ceiling on that phase, so a page that requests forever cannot hold the
#: operation open. A bound on traffic, never a timer.
_SETTLE_MAX_ROUNDS = 40


def _paused_destination(payload: dict[str, Any]) -> str:
    """The URL a ``Fetch.requestPaused`` is for, as the browser reports it.

    Taken from the request the browser is HOLDING, which is the address it
    would actually connect to - so a public name that resolved to a private
    address is judged on what it resolved to when CDP reports it that way,
    and a redirect hop is judged as its own request because the browser pauses
    it as one.
    """
    requested = payload.get("request")
    return str(requested.get("url", "")) if isinstance(requested, dict) else ""


def _dispose_request(
    connection: CdpConnection, session_id: str, payload: dict[str, Any], origin: str
) -> str | None:
    """Judge ONE paused request and give it a terminal disposition.

    Returns the refusal reason when it was failed, ``None`` when it was
    continued. Raises only if the browser would not acknowledge the
    disposition, which leaves custody open and is the caller's problem to
    fail closed on - an attempt is never a disposition.
    """
    request_id = str(payload.get("requestId", ""))
    if not request_id:
        raise BrowserCommandStarted(
            "the browser paused a request it did not name, so that request "
            "cannot be disposed of and the outcome is not established"
        )
    destination = _paused_destination(payload)
    reason = request_destination_refusal(destination, origin)
    if reason is None:
        connection.command(
            "Fetch.continueRequest", {"requestId": request_id}, session_id=session_id
        )
        return None
    # BEFORE THE WIRE: refused here, so the browser never opens the
    # connection, never sends the request, never sends a cookie with it and
    # never receives a byte back.
    connection.command(
        "Fetch.failRequest",
        {"requestId": request_id, "errorReason": "BlockedByClient"},
        session_id=session_id,
    )
    return reason


def serviced_requests(
    connection: CdpConnection,
    session_id: str,
    *,
    origin: str,
    owed: list[str] | None = None,
    rounds: int = _SETTLE_MAX_ROUNDS,
) -> list[str]:
    """Keep the gate answering while the page asks for things (DB-002).

    The page's subresources arrive as paused requests AFTER the document
    commits, and for as long as its scripts keep running. Each one is judged
    and given a disposition here, so an approved page loads normally and
    everything off the approved origin is refused before it is sent.

    Bounded by traffic, not by a clock: a cheap read is issued and every
    request paused before its reply lands is serviced, which is the same
    ``exchange`` discipline the navigation loop uses. Quiet rounds end it; the
    ceiling ends a page that never goes quiet.

    Returns the destinations that were refused, for the caller to report.
    """
    refused: list[str] = []
    quiet = 0
    for _ in range(rounds):
        # ONE cheap read pumps the socket: everything that is not its reply -
        # every request the browser paused meanwhile - lands in the buffer.
        # Nothing is dispositioned while that reply is outstanding, because a
        # disposition is itself a command, and a command scanning for its own
        # id DISCARDS another command's reply - which would leave this one
        # waiting for an answer that had already been thrown away.
        connection.command("Page.getNavigationHistory", session_id=session_id)
        held = connection.take_buffered("Fetch.requestPaused")
        if not held:
            quiet += 1
            if quiet >= _SETTLE_QUIET_ROUNDS:
                break
            continue
        quiet = 0
        # CUSTODY, VISIBLY. Taking these out of the buffer means the caller's
        # cleanup can no longer find them there, so they are recorded as owed
        # BEFORE any of them is judged. Every exit from here - a refusal the
        # browser will not acknowledge, a protocol error, anything - therefore
        # still leaves the caller holding their ids, and its teardown either
        # refuses them or destroys the tab. Without this the gate would be
        # disabled with them still paused, which RELEASES them onto the wire.
        for payload in held:
            request_id = str(payload.get("requestId", ""))
            if request_id and owed is not None:
                owed.append(request_id)
        for payload in held:
            reason = _dispose_request(connection, session_id, payload, origin)
            # Acknowledged: the browser answered without an error, so this one
            # is settled and no longer owed.
            if owed is not None:
                request_id = str(payload.get("requestId", ""))
                if request_id in owed:
                    owed.remove(request_id)
            if reason is not None:
                refused.append(_origin_of(_paused_destination(payload)))
    return refused


def guarded_navigate(
    connection: CdpConnection, session_id: str, *, tab_id: str, url: str, origin: str
) -> str:
    """Navigate to ``url`` and refuse any hop that leaves ``origin``.

    THE INVARIANT. An approval for one origin is not an approval for wherever
    that origin chooses to redirect. So every top-level document request of
    this navigation - the first one and every redirect hop - is PAUSED BEFORE
    IT IS SENT and judged by ``url_in_origin``, the same predicate the
    resource grammar uses. A hop inside the origin is continued; the first hop
    outside it is failed with ``BlockedByClient``, which means the browser
    never opens a connection to that origin, never sends it a request, never
    sends it a cookie and never receives a byte from it. Detection after
    landing would have been none of those things.

    WHY ``Page.navigate``'S REPLY IS NOT THE END. It is an ACKNOWLEDGEMENT and
    nothing more. The protocol does not promise that the redirect chain has
    terminated when it arrives, and it can arrive before a later hop is even
    requested - so treating it as completion and disarming there would leave
    exactly the redirect this exists to stop free to go out unjudged. The
    reply is therefore only read for two things: the ``errorText`` that says
    the navigation definitively failed, and the ``frameId``/``loaderId`` that
    say WHICH navigation is being guarded.

    THE TERMINAL SIGNAL is ``Page.frameNavigated`` for that exact frame AND
    that exact loader. It fires when navigation of the frame has completed and
    the frame is associated with the new loader, and a redirect never commits
    a loader - only the response that ends the chain does. Both halves are
    required: the frame id alone would accept a subframe's navigation, and the
    loader id alone would accept a different navigation of the same frame.
    Anything else on the stream - other frames, other loaders, unrelated
    events - is buffered and never mistaken for completion.

    A fragment-only navigation would commit no loader and fire no
    ``frameNavigated``; it cannot arise here, because the resource grammar
    refuses a URL carrying a fragment.

    Returns the URL that navigation committed to, taken from the very event
    that proved it committed.

    NOTHING PAUSED IS EVER RELEASED BY THE DISARM. A ``Fetch.requestPaused``
    that reaches this function is in its custody from the instant it is
    delivered until a ``Fetch.continueRequest`` or ``Fetch.failRequest`` is
    ACKNOWLEDGED for it, and no exit in between - the hop limit included -
    can end that custody without failing it first.

    AN ATTEMPT IS NOT A DISPOSITION. ``Fetch.disable`` RESUMES whatever is
    still paused, so it may only be sent once every outstanding request is
    known to have been continued or failed - known, not attempted. A cleanup
    ``Fetch.failRequest`` whose acknowledgement cannot be established leaves
    custody open, and the teardown then destroys the page instead of
    disabling the gate (see ``_fail_closed_teardown``).

    Uncertainty is never converted into an answer. A navigation whose terminal
    state cannot be established - no correlatable loader, or more protocol
    traffic than one navigation may produce - raises ``BrowserCommandStarted``,
    which Core settles as an unknown outcome, because a request really was
    dispatched and what became of it is not known.

    THIS GATE IS NO LONGER THE ONLY ONE. ``BrowserPolicy`` holds a second,
    standing gate over the same tab on its own connection, armed before the
    tab is first navigated and released only by destroying the page. Two
    DevTools clients may both intercept a target and a request proceeds only
    when BOTH continue it, so what follows is unchanged by that and cannot be
    weakened by it: this function still judges its own navigation, still
    holds custody of everything it was handed, and still disarms ITS OWN
    session on the way out. What it no longer has to pretend is that
    disarming ends the page's ability to reach the network - the standing
    policy is what makes that true.

    ponytail: this gate covers the approved navigation's own redirect chain,
    which is what an approval authorizes. A page that, once loaded, navigates
    itself somewhere else with script is a NEW navigation this operation never
    asked for; it is left to the next capability, each of which re-checks the
    tab's origin and refuses when it has drifted - and, since DB-002B, to the
    standing policy, which judges that navigation's own request too.
    """
    connection.command("Page.enable", session_id=session_id)
    connection.command("Fetch.enable", {"patterns": _ALL_REQUESTS_PATTERN}, session_id=session_id)
    #: The paused request this function currently holds and has NOT yet had
    #: continued or failed. Empty means nothing is owed: either none has been
    #: delivered, or the last one's disposition was acknowledged.
    current = ""
    try:
        pending = connection.send("Page.navigate", {"url": url}, session_id=session_id)
        guarded: tuple[str, str] | None = None
        committed: dict[tuple[str, str], str] = {}
        hops = 0
        #: Origins the page asked for and did not get. Recorded, not raised:
        #: a refused subresource is the gate working, not a failed navigation.
        subresources_refused: list[str] = []
        #: Request ids the settle phase has taken OUT of the buffer and not
        #: yet had a disposition acknowledged for. The cleanup below settles
        #: them, because ``take_buffered`` can no longer find them.
        owed: list[str] = []
        for _ in range(BROWSER_MAX_NAVIGATION_EVENTS):
            if guarded is not None and guarded in committed:
                # The document committed; the page now asks for what it needs.
                # The gate stays armed and keeps answering until the tab goes
                # quiet, so its own resources load and nothing else does.
                serviced_requests(connection, session_id, origin=origin, owed=owed)
                return committed[guarded]
            kind, payload = connection.exchange(
                pending, "Page.navigate", ("Fetch.requestPaused", "Page.frameNavigated")
            )
            if kind == "reply":
                if payload.get("errorText"):
                    # Definitively failed, and the browser says so.
                    raise BrowserCommandStarted(
                        f"the browser reported a navigation error: {payload.get('errorText')}"
                    )
                frame_id = str(payload.get("frameId", ""))
                loader_id = str(payload.get("loaderId", ""))
                if not frame_id or not loader_id:
                    raise BrowserCommandStarted(
                        "the browser acknowledged the navigation without naming the "
                        "frame and loader it belongs to, so its completion cannot "
                        "be established"
                    )
                guarded = (frame_id, loader_id)
                continue
            if kind == "Page.frameNavigated":
                frame = payload.get("frame")
                if isinstance(frame, dict):
                    # Recorded, never acted on until it is known to be OURS.
                    committed[(str(frame.get("id", "")), str(frame.get("loaderId", "")))] = str(
                        frame.get("url", "")
                    )
                continue
            # CUSTODY FIRST. ``exchange`` has already taken this event off the
            # stream and out of the buffer, so from here on nothing else can
            # settle it - not the cleanup drain, not another reader, nobody.
            # Its id is therefore recorded BEFORE any check that could raise,
            # because every one of those exits would otherwise leave a paused
            # request that ``Fetch.disable`` goes on to release onto the wire.
            request_id = str(payload.get("requestId", ""))
            requested = payload.get("request")
            hop = str(requested.get("url", "")) if isinstance(requested, dict) else ""
            if not request_id:
                # Nothing here can name it, so nothing here can dispose of it.
                # That is uncertainty, not a verdict: disabling the gate may
                # release a request this seam never judged.
                raise BrowserCommandStarted(
                    "the browser paused a request it did not name, so that "
                    "request cannot be disposed of and the outcome is not "
                    "established"
                )
            current = request_id
            # WHOSE REQUEST IS THIS? Only the top-level document of the
            # navigation being guarded is a HOP - the thing the approval named
            # and whose refusal means the navigation did not happen. Everything
            # else the page asks for while it loads - a subframe's document, an
            # image, a script, a stylesheet, an XHR, a form post - is judged by
            # the same predicate but is not the navigation: refusing one is a
            # refused subresource, not a failed navigation, so it is
            # dispositioned here and the loop goes on (DB-002).
            resource_type = str(payload.get("resourceType", ""))
            frame_of = str(payload.get("frameId", ""))
            if resource_type and resource_type != "Document" or (
                guarded is not None and frame_of and frame_of != guarded[0]
            ):
                reason = _dispose_request(connection, session_id, payload, origin)
                current = ""
                if reason is not None:
                    subresources_refused.append(_origin_of(hop))
                continue
            hops += 1
            if hops > BROWSER_MAX_REDIRECT_HOPS:
                raise BrowserOriginBlocked(
                    "the navigation redirected more times than one approved "
                    "navigation may follow, so it was refused",
                    blocked=origin,
                )
            if not hop:
                raise BrowserCommandFailed("the browser paused an unreadable request")
            if request_destination_refusal(hop, origin) is not None:
                # BEFORE THE WIRE. The request is refused here, so nothing of
                # this navigation ever reaches that origin - whether or not the
                # acknowledgement for this navigation has already arrived.
                connection.command(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                    session_id=session_id,
                )
                # Acknowledged: ``command`` returned, so the browser answered
                # without an error. Custody ends HERE and not a line earlier,
                # so a failed fail is retried by the cleanup rather than
                # assumed to have worked.
                current = ""
                raise BrowserOriginBlocked(
                    "the approved origin redirected outside it, so the request to "
                    f"{_origin_of(hop)} was refused before it was sent and the "
                    "navigation did not happen",
                    blocked=_origin_of(hop),
                )
            connection.command(
                "Fetch.continueRequest", {"requestId": request_id}, session_id=session_id
            )
            current = ""
        raise BrowserCommandStarted(
            "the navigation did not reach a terminal state within the traffic "
            "one navigation may produce, so its outcome is not established"
        )
    except (BrowserOriginBlocked, BrowserCommandStarted):
        raise
    except BrowserCommandFailed as exc:
        # ONE RULE FOR BOTH CALLERS. Past ``Page.navigate`` a protocol failure
        # is not evidence that nothing happened, so it becomes uncertainty
        # here rather than in one caller and not the other.
        raise BrowserCommandStarted(f"the navigation was not acknowledged: {exc}") from exc
    finally:
        # THIS session's gate is never left armed on any path: a paused
        # request with nobody left to judge it would hang the tab. It is the
        # standing ``BrowserPolicy``, on its own connection, that keeps the
        # page mediated after this returns - and that one cannot be disabled
        # at all, because ``Fetch.disable`` is not in its method set.
        #
        # DISARMING RELEASES WHAT IT WAS HOLDING, so everything paused is given
        # an explicit terminal disposition first. TWO DISTINCT
        # RESPONSIBILITIES, and neither can stand in for the other:
        #
        # 1. THE CURRENT REQUEST - delivered to this function and therefore no
        #    longer in any buffer. Only the tracked id can settle it; the drain
        #    below would never see it. Every early exit lands here: the hop
        #    limit, an unreadable hop, a refusal whose own fail was not
        #    acknowledged, a protocol error, a cancellation, a timeout, a
        #    correlation failure, or any other exception raised after delivery.
        # 2. THE BUFFERED REQUESTS - delivered by the browser while some other
        #    reply was being awaited, and never delivered to this function.
        # 3. THE SETTLE PHASE'S OWED REQUESTS - taken out of the buffer by
        #    ``serviced_requests`` and not acknowledged as disposed, so the
        #    drain below cannot see them either (DB-002).
        unresolved: list[str] = []
        for stranded in ([current] if current else []) + list(owed) + [
            str(one.get("requestId", ""))
            for one in connection.take_buffered("Fetch.requestPaused")
        ]:
            if not stranded:
                continue
            try:
                connection.command(
                    "Fetch.failRequest",
                    {"requestId": stranded, "errorReason": "BlockedByClient"},
                    session_id=session_id,
                )
            except Exception:  # noqa: BLE001 - an attempt is not a disposition
                # NOT swallowed. The request may still be paused, so the gate
                # may not be disabled and this operation may not claim to know
                # what happened.
                unresolved.append(stranded)
        if unresolved:
            # Deliberately raised from a ``finally``: it replaces whatever
            # answer the operation had reached, because that answer was
            # premature. A navigation cannot be a success, or even a definite
            # refusal, while a request it caused may still be paused and
            # unaccounted for.
            _fail_closed_teardown(connection, session_id, tab_id, unresolved)
        with _suppressed():
            connection.command("Fetch.disable", session_id=session_id)
        with _suppressed():
            connection.command("Page.disable", session_id=session_id)


def _fail_closed_teardown(
    connection: CdpConnection, session_id: str, tab_id: str, unresolved: list[str]
) -> None:
    """End the gate WITHOUT releasing what it is still holding. Always raises.

    Reached only when a paused request could not be given an acknowledged
    terminal disposition. ``Fetch.disable`` is not an option here and is never
    sent on this path: it is defined to RESUME paused requests, so using it as
    a fallback would dispatch exactly the request that could not be refused.

    WHAT IS DONE INSTEAD is the narrowest thing that removes the request
    rather than freeing it: ``Target.closeTarget`` on the ONE
    JARVIS-owned tab this operation was driving. The request is paused at the
    Request stage, which means the browser has not sent it; it belongs to a
    navigation of that page's frame; and closing the target destroys the page,
    its frames and its pending loads. There is then no frame the response
    could be delivered to and no loader to resume, so the request cannot
    afterwards reach the network. Nothing wider is touched: not the browser,
    not the profile, not another tab.

    The outcome is UNCERTAIN either way and is reported as such. If even the
    close cannot be acknowledged, this says so rather than claiming the
    stronger guarantee: the connection is then dropped with the gate still
    ARMED, which at least leaves the request held rather than resumed, but
    that is not a proof and is not presented as one.
    """
    names = ", ".join(unresolved)
    try:
        connection.command("Target.closeTarget", {"targetId": tab_id})
    except Exception as exc:  # noqa: BLE001 - the stronger guarantee is gone
        raise BrowserCommandStarted(
            f"the browser did not acknowledge refusing paused request(s) {names}, "
            "and the page holding them could not be closed either; the gate is "
            "left armed rather than disabled so nothing is resumed, but whether "
            "those requests were sent is not established"
        ) from exc
    raise BrowserCommandStarted(
        f"the browser did not acknowledge refusing paused request(s) {names}, so "
        "the page holding them was destroyed instead of disabling the gate; "
        "those requests cannot have been sent, but the outcome of this "
        "navigation is not established"
    )


@contextmanager
def _suppressed() -> Iterator[None]:
    """Let a cleanup failure not replace the answer the operation reached."""
    try:
        yield
    except Exception:  # noqa: BLE001 - cleanup, never the reported outcome
        pass


def _origin_of(url: str) -> str:
    parsed = urlsplit(url)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


# --------------------------------------------------------------------------
# The standing policy - one JARVIS-owned tab, mediated for as long as it lives
# --------------------------------------------------------------------------

#: How long the standing policy waits between pumps of its own socket.
#: NOT a security parameter: every request the browser paused stays paused
#: until this loop judges it, so the only thing a longer interval costs is
#: latency on the page's own resources. It never lets anything through.
_POLICY_POLL_SECONDS = 0.05

#: The only two events the standing policy acts on. Everything else the
#: browser volunteers is dropped each round rather than buffered forever.
_POLICY_EVENTS = ("Fetch.requestPaused", "Target.attachedToTarget")

#: Auto-attach the page's own execution targets - out-of-process subframes and
#: its workers - so a renderer gate can be armed on each.
#:
#: NOT HELD AT STARTUP, and that was measured rather than chosen. Holding a
#: child (``waitForDebuggerOnStart``) is the textbook way to gate it before it
#: runs a line, but on Edge 153 a held child does not answer ``Fetch.enable``:
#: the standing policy's own loop blocked on that command and stopped judging
#: anything at all, on any page that creates a worker. A policy that stalls is
#: worse than one that arms a child a moment late. Chrome 153.0.8010.54 does
#: NOT stall: a held dedicated worker answered ``Fetch.enable`` within
#: milliseconds (with the "wasn't found" refusal below, as it has no Fetch
#: domain) - re-measured 2026-09-24. Not holding remains the rule: it is
#: required on Edge and costs nothing on Chrome.
#:
#: WHAT COVERS THE GAP is the per-tab boundary. A child shares its tab's
#: browser context, so anything it asks for before its gate is armed can still
#: only reach that tab's approved origin. Off-origin is impossible for it;
#: on-origin is what the grant authorizes anyway (BROWSER-WS-01).
_POLICY_AUTO_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "flatten": True,
}


class BrowserPolicy:
    """Standing network mediation over ONE JARVIS-owned tab (DB-002).

    WHY THIS EXISTS AT ALL. The gate inside ``guarded_navigate`` is armed for
    an operation and disarmed when that operation ends. That was measured
    against a real browser and it is not enough: with the page still loaded,
    a ``setTimeout`` that fires after the gate is gone reaches an unapproved
    origin, and so does a click handler during an operation that never armed a
    gate in the first place (``browser_click``, ``browser_type``,
    ``browser_wait``, ``browser_page_read``). An approved page is untrusted
    code that keeps running; mediation that ends with the operation is
    mediation that ends before the page does.

    WHY NOT OFFLINE FENCING. The alternative design - leave the page loaded
    but network-offline whenever no operation is running - was tested against
    real Edge before this was written, and it does not hold: neither
    ``Fetch`` interception nor ``Network.emulateNetworkConditions(offline)``
    survives the client's disconnect. The browser resumes normal networking
    the moment the socket closes, so there is no durable offline state to
    fence with, and a policy that must be re-armed on reconnection has an
    online-and-ungated interval by construction. The only durable fail-closed
    state is a page that is not there, which is what ``release`` produces.

    SO THE POLICY OUTLIVES THE OPERATION. It holds its OWN connection, on its
    own session, and keeps ``Fetch`` armed over every renderer request of that
    tab from before the tab is first navigated until the tab is destroyed.
    Two DevTools clients may both intercept the same target and a request
    proceeds only when BOTH continue it, so this is strictly additive: it
    cannot weaken the navigation gate, and the navigation gate disarming its
    own session does not disarm this one.

    IT CANNOT DISARM ITSELF. ``BROWSER_POLICY_METHODS`` has no
    ``Fetch.disable``, so the connection refuses that name before it is
    written. Ending mediation is therefore only expressible as destroying the
    page (``release``), which is the ordering the invariant asks for - network
    offline before policy removal - enforced by the method set rather than by
    remembering to do things in the right order.

    ponytail: one poll loop per JARVIS-owned tab. A tab is opened by a person
    asking for one, so there are a handful at most; an event-driven reader
    would need a frame-safe non-blocking read this socket does not have.
    """

    def __init__(
        self,
        socket_url: str,
        *,
        tab_id: str,
        origin: str,
        timeout: float = BROWSER_COMMAND_TIMEOUT_SECONDS,
        poll: float = _POLICY_POLL_SECONDS,
        sleep: Any = time.sleep,
        proxy: PolicyProxy | None = None,
        context_id: str = "",
    ) -> None:
        #: The pre-connect boundary this ONE tab reaches the network through,
        #: and the browser context that routes it there. Owned here because
        #: they must not outlive the tab: a boundary still carrying a tab's
        #: authority after that tab is gone is the stale cross-tab grant
        #: Daybreak found (BROWSER-WS-01).
        self.proxy = proxy
        self.context_id = context_id
        self.tab_id = tab_id
        self._socket_url = socket_url
        self._timeout = timeout
        self._poll = poll
        self._sleep = sleep
        self._origin = origin
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connection: CdpConnection | None = None
        self._session = ""
        self._thread: threading.Thread | None = None
        #: Set when mediation ended for any reason. A dead policy is never
        #: reused: the caller arms a new one, which cannot silently inherit a
        #: tab that spent any time unmediated.
        self._dead = False
        #: What was refused while no operation was watching, newest last.
        #: Evidence, bounded, and never a reason to keep going.
        self.refused: list[str] = []
        #: Target types this policy could not arm a renderer gate on, and which
        #: are therefore held to this tab's origin by the boundary alone.
        #: Reported, not hidden: it is a real difference in how they are judged.
        self.ungated: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self._dead and not self._stop.is_set()

    def arm(self) -> None:
        """Take custody of the tab, BEFORE it is asked to go anywhere.

        Raises ``BrowserCommandFailed`` if custody cannot be established, and
        the caller must then not bring the page online - an unmediated page is
        the whole defect.
        """
        try:
            connection = CdpConnection(
                self._socket_url, BROWSER_POLICY_METHODS, timeout=self._timeout
            )
        except Exception:
            # A policy whose socket never opened is a policy that is NOT
            # mediating, and it must say so: ``alive`` is what a caller asks
            # before treating a tab as covered.
            self._dead = True
            raise
        self._connection = connection
        try:
            self._session = _attach(connection, self.tab_id)
            # ARM FIRST, then admit children. Reversed, a worker could start
            # ungated while this was still setting itself up.
            connection.command(
                "Fetch.enable",
                {"patterns": _ALL_REQUESTS_PATTERN},
                session_id=self._session,
            )
            connection.command(
                "Target.setAutoAttach", dict(_POLICY_AUTO_ATTACH), session_id=self._session
            )
        except Exception:
            self._dead = True
            with _suppressed():
                connection.close()
            raise
        self._thread = threading.Thread(
            target=self._serve, name=f"browser-policy-{self.tab_id[:8]}", daemon=True
        )
        self._thread.start()

    def bind(self, origin: str) -> None:
        """The origin this tab is authorized for from now on.

        Set by an operation from its own approved plan, BEFORE that operation
        acts, so the standing policy and the navigation gate judge the same
        hop the same way and neither refuses what the other allows.
        """
        with self._lock:
            self._origin = origin
        if self.proxy is not None:
            # THE SAME DECISION, SEEN TWICE. The renderer gate judges what the
            # page may request; the boundary judges where this tab may
            # connect. A WebSocket skips the first and is caught by the
            # second, because it is not this tab's approved origin.
            self.proxy.bind_origin(origin)
            self.proxy.authorize(origin)

    def release(self) -> None:
        """End mediation the only way it can end: by destroying the page.

        Never by disabling the gate - the method set makes that unsendable,
        and it would resume whatever is paused. After this the tab does not
        exist, so there is nothing left to mediate.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._timeout)
        connection = self._connection
        self._dead = True
        if connection is None:
            # Never armed, or already let go. The boundary still has to go:
            # one left listening is an authorization with no tab behind it.
            self._release_boundary()
            return
        with _suppressed():
            connection.command("Target.closeTarget", {"targetId": self.tab_id})
        if self.context_id:
            with _suppressed():
                connection.command(
                    "Target.disposeBrowserContext", {"browserContextId": self.context_id}
                )
        with _suppressed():
            connection.close()
        self._connection = None
        self._release_boundary()

    def _release_boundary(self) -> None:
        """Stop this tab's pre-connect boundary. Its authority ends with it.

        Ordering matters and is deliberate: the page is destroyed first, then
        the boundary. A boundary stopped while the page still lived would only
        fail its requests; a boundary left running after the page is gone
        would be an authorization nothing is using and anything could.
        """
        proxy, self.proxy = self.proxy, None
        if proxy is not None:
            proxy.stop()

    # -- the loop ----------------------------------------------------------

    def _serve(self) -> None:
        """Judge whatever the tab asks for, for as long as the tab lives."""
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception:  # noqa: BLE001 - mediation ended; the page may not outlive it
                self._fail_closed()
                return
            self._sleep(self._poll)

    def _pump(self) -> None:
        connection = self._connection
        if connection is None:  # pragma: no cover - release clears it last
            raise BrowserCommandFailed("the standing policy has no connection")
        # One cheap read: everything the browser paused meanwhile lands in the
        # buffer, and nothing is dispositioned while this reply is
        # outstanding. BROWSER-LEVEL, and that is not a detail - a page-session
        # read is not answered while an auto-attached child is held at
        # startup, so a page that creates a worker used to stall this loop
        # permanently and with it every judgement it owed.
        connection.command("Target.getTargets")
        for frame in connection.take_buffered_frames("Target.attachedToTarget"):
            self._adopt(connection, frame)
        with self._lock:
            origin = self._origin
        paused = connection.take_buffered_frames("Fetch.requestPaused")
        # Everything else the browser volunteered is not this policy's
        # business and would otherwise accumulate for the life of the tab.
        # Dropped only AFTER both takes, so nothing judged here is lost.
        connection.keep_only_buffered(_POLICY_EVENTS)
        for frame in paused:
            session = str(frame.get("sessionId") or self._session)
            payload = frame.get("params") or {}
            reason = _dispose_request(connection, session, payload, origin)
            if reason is not None:
                self.refused.append(_origin_of(_paused_destination(payload)))
                del self.refused[:-_MAX_SKIPPED_EVENTS]

    def _adopt(self, connection: CdpConnection, frame: dict[str, Any]) -> None:
        """Gate a newly attached child target before it is allowed to run."""
        params = frame.get("params") or {}
        child = str(params.get("sessionId", ""))
        if not child:
            raise BrowserCommandFailed(
                "the browser attached a target it did not name, so that "
                "target's requests cannot be mediated"
            )
        try:
            connection.command(
                "Fetch.enable", {"patterns": _ALL_REQUESTS_PATTERN}, session_id=child
            )
        except BrowserCommandFailed:
            # NOT EVERY TARGET HAS A FETCH DOMAIN. A dedicated worker does not:
            # Chromium answers "'Fetch.enable' wasn't found" for it. Treating
            # that as a lost gate destroyed the tab, so any page that created a
            # worker was killed by its own mediation - fail-closed, but
            # unusable. Measured against Edge 153, and again against Chrome
            # 153.0.8010.54 (2026-09-24): the same refusal, held or running.
            #
            # WHAT STILL HOLDS FOR SUCH A TARGET is the thing a renderer gate
            # could never provide anyway: it shares this tab's browser context,
            # so every connection it makes leaves through this tab's
            # pre-connect boundary and may only reach this tab's approved
            # origin. Off-origin is impossible for it; on-origin is what the
            # grant already authorizes.
            #
            # With no boundary there is no such backstop, and an ungateable
            # target really would be unmediated - so that case fails closed.
            if self.proxy is None:
                raise
            self.ungated.append(str((params.get("targetInfo") or {}).get("type", "?")))
        with _suppressed():
            # A child of a child - a worker inside an out-of-process frame - is
            # held and gated by exactly the same rule, where the target has the
            # domain for it.
            connection.command(
                "Target.setAutoAttach", dict(_POLICY_AUTO_ATTACH), session_id=child
            )

    def _fail_closed(self) -> None:
        """Mediation is gone, so the page it was mediating goes with it."""
        self._dead = True
        connection = self._connection
        if connection is None:
            self._release_boundary()
            return
        with _suppressed():
            connection.command("Target.closeTarget", {"targetId": self.tab_id})
        if self.context_id:
            with _suppressed():
                connection.command(
                    "Target.disposeBrowserContext", {"browserContextId": self.context_id}
                )
        with _suppressed():
            connection.close()
        self._connection = None
        self._release_boundary()


#: Every tab this process is mediating, by tab id. Module state because the
#: browser it describes is module state too: one port, one profile, one
#: browser. Handed to ``run_operation_plan`` so a test can supply its own.
_POLICIES: dict[str, BrowserPolicy] = {}


def release_browser_policies(policies: dict[str, BrowserPolicy] | None = None) -> None:
    """Destroy every mediated tab. Registered to run when JARVIS exits.

    A JARVIS-owned untrusted page may not outlive the thing mediating it, so
    an orderly shutdown closes those tabs rather than leaving them loaded and
    unjudged behind a browser that keeps running.
    """
    registry = _POLICIES if policies is None else policies
    for policy in list(registry.values()):
        with _suppressed():
            policy.release()
    registry.clear()


atexit.register(release_browser_policies)


def tab_boundary(socket_url: str, *, origin: str) -> PolicyProxy:
    """A pre-connect boundary serving ONE tab, started before that tab exists.

    Its own listener, so the browser context pointed at it carries exactly one
    tab's traffic, and its own authority, so what arrives may only go to that
    tab's approved origin. Both die with the tab (BROWSER-WS-01).

    BOUND BEFORE IT IS RETURNED. The browser context that will be pointed at
    this listener does not exist yet, but the moment it does the browser may
    preconnect through it - so the authority is in place before any caller
    can hand the port to the browser, not once the tab is mediated later.
    A boundary that cannot be bound is stopped and not returned.
    """
    proxy = PolicyProxy()
    proxy.start()
    try:
        proxy.bind_origin(origin)
        proxy.authorize(origin)
        if proxy.authority() is None:
            raise ValueError("no authority")
    except Exception as exc:
        proxy.stop()
        raise BrowserCommandFailed(
            "the tab's boundary could not be bound to its approved origin, so no "
            "tab was opened"
        ) from exc
    return proxy


def mediated_tab(
    policies: dict[str, BrowserPolicy] | None,
    socket_url: str,
    *,
    tab_id: str,
    origin: str,
    proxy: PolicyProxy | None = None,
    context_id: str = "",
) -> BrowserPolicy | None:
    """The standing policy over ``tab_id``, arming one if there is none.

    Called before an operation touches the tab, so every capability - reading,
    waiting, clicking, typing, navigating - runs against an already-mediated
    page rather than arming and disarming one of its own.

    A policy that died is replaced rather than reused: a tab whose mediation
    failed spent time unjudged, and the replacement re-establishes custody
    before this operation adds anything to what that page is doing.
    """
    if policies is None:
        if proxy is not None:  # pragma: no cover - only a caller that made one
            proxy.stop()
        return None
    existing = policies.get(tab_id)
    if existing is not None and existing.alive:
        existing.bind(origin)
        if proxy is not None and proxy is not existing.proxy:
            # A boundary made for a tab that turned out to be mediated already
            # is not left listening: an unowned boundary is an authorization
            # with nothing attached to it.
            proxy.stop()
        return existing
    if existing is not None:
        policies.pop(tab_id, None)
        existing_proxy, existing.proxy = existing.proxy, None
        if existing_proxy is not None:
            existing_proxy.stop()
    if proxy is None or not context_id:
        # NEVER RECONSTRUCTED. A policy is created exactly once, by the
        # operation that also created the tab, its context and its boundary -
        # so mediation and the thing that decides where the tab may connect
        # are made together or not at all. Building one here for a tab that
        # arrived from somewhere else would produce a policy with no boundary,
        # which is a page with no destination authority at all.
        if proxy is not None:  # pragma: no cover - a caller that made one
            proxy.stop()
        raise BrowserCommandFailed(
            "this tab is not one JARVIS created and owns, so mediation was not "
            "built over it and the operation was refused"
        )
    policy = BrowserPolicy(
        socket_url, tab_id=tab_id, origin=origin, proxy=proxy, context_id=context_id
    )
    policy.bind(origin)
    policy.arm()
    policies[tab_id] = policy
    return policy


def _drop_policy(policies: dict[str, BrowserPolicy] | None, tab_id: str) -> None:
    """Forget a tab that is being destroyed on purpose."""
    if policies is None:
        return
    policy = policies.pop(tab_id, None)
    if policy is not None:
        with _suppressed():
            policy.release()


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
    policies: dict[str, BrowserPolicy] | None = _POLICIES,
) -> BrowserCommandOutput:
    """Perform one validated browser operation and report what came back.

    THE DISPATCH LINE. Everything before the browser is asked to ACT raises
    ``BrowserCommandFailed`` and means nothing happened. Everything after it
    raises ``BrowserCommandStarted``, which the connector turns into an
    unknown outcome rather than into "it did not happen".

    ``policies`` is the standing mediation this process holds over the tabs it
    owns, and it defaults to the one registry there is, because there is one
    browser. ``None`` means this caller mediates the tab itself and is only
    ever passed by a test that is exercising the navigation gate on its own -
    it is the pre-DB-002B behaviour and it leaves a loaded page unjudged
    between operations.
    """
    reason = browser_operation_plan_refusal(plan)
    if reason is not None:
        raise BrowserCommandFailed(reason)
    socket_url = attach(profile_dir=profile_dir, port=port)
    # EXPLICIT LOCAL AUTHORITY, and only that. An operator who approved a
    # literal local origin gets it through the boundary too; nothing else
    # does, and a public NAME that resolves there still will not (DB-002B-02).
    proxy = _POLICY_PROXIES.get(port)
    if proxy is not None:
        proxy.authorize(plan.resource.origin)
    connection = CdpConnection(
        socket_url, BROWSER_CAPABILITY_METHODS[plan.capability], timeout=BROWSER_COMMAND_TIMEOUT_SECONDS
    )
    # The owner keeps a handle on the live connection for exactly one reason:
    # a cancellation must be able to close it. Cleared on the way out so a
    # later cancel can never signal a connection that is already gone.
    if connection_sink is not None:
        connection_sink(connection)
    try:
        return _perform(
            connection,
            plan,
            frame=frame,
            profile_dir=profile_dir,
            sleep=sleep,
            socket_url=socket_url,
            policies=policies,
        )
    finally:
        if connection_sink is not None:
            connection_sink(None)
        connection.close()


def _perform(
    connection: CdpConnection,
    plan: Any,
    *,
    frame: Any,
    profile_dir: str,
    sleep: Any,
    socket_url: str = "",
    policies: dict[str, BrowserPolicy] | None = None,
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
        # A TRANSACTION, not a sequence. Opening a tab creates four things -
        # a boundary, a browser context, a target and the mediation over it -
        # and a tab is only a tab when it has all four. Until then every one
        # of them is PROVISIONAL: any failure undoes what was made and the tab
        # is never published as current, because a half-created tab that a
        # later navigation can find is a page with mediation built over it and
        # no boundary underneath (transactional startup).
        #
        # EMPTY FIRST, still: the target is created at about:blank, which asks
        # the network for nothing, so nothing is loading while ownership is
        # being established.
        boundary: PolicyProxy | None = None
        context_id = ""
        tab_id = ""

        def undo() -> None:
            """Take back everything this operation made. Best effort, in the
            order that removes the most first: the page, then the context that
            routed it, then the boundary it was routed to, then the
            bookkeeping - which is cleared whatever the rest managed to do."""
            if tab_id:
                with _suppressed():
                    connection.command("Target.closeTarget", {"targetId": tab_id})
            if context_id:
                with _suppressed():
                    connection.command(
                        "Target.disposeBrowserContext", {"browserContextId": context_id}
                    )
            if boundary is not None:
                with _suppressed():
                    boundary.stop()
            if tab_id:
                _drop_policy(policies, tab_id)
                if read_current_tab(profile_dir) == tab_id:
                    write_current_tab(profile_dir, "")

        try:
            target: dict[str, Any] = {"url": BROWSER_BLANK_PAGE}
            if policies is not None:
                boundary = tab_boundary(socket_url, origin=origin)
                # VERIFIED BOUND before the browser learns the port. A tab
                # proxy the browser can reach while it serves no authority is
                # a way out for whatever the browser preconnects on its own.
                if boundary.authority() is None:
                    raise BrowserCommandFailed(
                        "the tab's boundary carries no authority, so the browser "
                        "was not given it and no tab was opened"
                    )
                context_id = str(
                    connection.command(
                        "Target.createBrowserContext",
                        {
                            "disposeOnDetach": False,
                            "proxyServer": f"http://127.0.0.1:{boundary.port}",
                            "proxyBypassList": "<-loopback>",
                        },
                    ).get("browserContextId", "")
                )
                if not context_id:
                    raise BrowserCommandFailed(
                        "the browser opened no context of its own for this tab, so "
                        "the tab would share a way out with every other tab"
                    )
                target["browserContextId"] = context_id
            created = connection.command("Target.createTarget", target)
            tab_id = str(created.get("targetId", ""))
            if browser_tab_id_refusal(tab_id) is not None:
                tab_id = ""
                raise BrowserCommandFailed("the browser opened no readable tab")
            session_id = _attach(connection, tab_id)
            # MEDIATED BEFORE IT IS ONLINE, and this is the success point of
            # the transaction: after it the tab has a live policy holding its
            # context and its boundary, and only then is it a tab at all.
            mediated_tab(
                policies,
                socket_url,
                tab_id=tab_id,
                origin=origin,
                proxy=boundary,
                context_id=context_id,
            )
        except Exception:
            undo()
            raise
        # PUBLISHED LAST. Recording it earlier is what let a failed startup
        # leave a current-tab note pointing at a target nothing owned.
        write_current_tab(profile_dir, tab_id)
        landed = guarded_navigate(
            connection, session_id, tab_id=tab_id, url=resource.url, origin=origin
        )
        return BrowserCommandOutput({"tab_id": tab_id, "url": landed})

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
            # Becoming the current tab is becoming the tab the next operations
            # act on, so it is mediated here even though switching itself asks
            # the network for nothing.
            mediated_tab(policies, socket_url, tab_id=resource.tab_id, origin=origin)
            connection.command("Target.activateTarget", {"targetId": resource.tab_id})
            write_current_tab(profile_dir, resource.tab_id)
            return BrowserCommandOutput({"tab_id": resource.tab_id, "url": url})
        # THE PAGE FIRST, THEN ITS MEDIATION. Destroying the target is what
        # takes the page off the network; dropping the policy afterwards can
        # therefore never leave a loaded page behind unjudged.
        connection.command("Target.closeTarget", {"targetId": resource.tab_id})
        _drop_policy(policies, resource.tab_id)
        if read_current_tab(profile_dir) == resource.tab_id:
            write_current_tab(profile_dir, "")
        return BrowserCommandOutput({"tab_id": resource.tab_id, "url": ""})

    target = _resolve_tab(connection, profile_dir, origin=origin, policies=policies)
    tab_id = str(target.get("targetId", ""))
    session_id = _attach(connection, tab_id)
    # EVERY REMAINING CAPABILITY GOES THROUGH HERE, which is the DB-002B fix:
    # reading, waiting, clicking, typing and navigating all act on a tab that
    # is already mediated and stays mediated afterwards, instead of each one
    # deciding for itself whether a network policy applies. A click handler
    # that fetches, a timer that fires between two operations and a worker
    # started by either are judged by the same predicate as the navigation.
    mediated_tab(policies, socket_url, tab_id=tab_id, origin=origin)

    if capability == "browser_wait":
        # Waiting is the operation most likely to be the moment a page's own
        # timers fire, so it is spent INSIDE mediation, never before it.
        sleep(resource.seconds)

    if capability == "browser_navigate":
        # The only capability that may act on a tab standing outside the bound
        # origin: arriving there is what it is for. Where it may GO is decided
        # hop by hop under the gate, before each request is sent.
        url = guarded_navigate(
            connection, session_id, tab_id=tab_id, url=resource.url, origin=origin
        )
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
    "BrowserOriginBlocked",
    "BrowserPolicy",
    "CdpConnection",
    "BROWSER_BLANK_PAGE",
    "guarded_navigate",
    "attached_browser",
    "browser_arguments",
    "browser_executable",
    "browser_policy_proxy",
    "enforce_profile_policy",
    "mediated_tab",
    "tab_boundary",
    "owned_browser",
    "read_current_tab",
    "release_browser_policies",
    "release_owned_browsers",
    "run_operation_plan",
    "secret_field_refusal",
    "write_current_tab",
]
