"""The pre-connect boundary: JARVIS owns the resolve-and-connect step.

WHY A PROXY AND NOT A CHECK. Every layer before this one judges a request by
its URL. That is the right question for authority - the operator approved an
origin, and ``request_destination_refusal`` holds each request to it - but it
is the wrong question for where the packets go. A hostname is not a
destination; it is an instruction to ask a resolver for one, and whoever
controls the answer controls the destination. An operator who approves
``https://example.test`` has not approved ``example.test -> 127.0.0.1``,
``-> 192.168.1.10`` or ``-> 169.254.169.254``, and the browser sitting inside
their network can reach every one of those (DB-002B-02).

WHY NOT JUST RESOLVE AND LOOK. Calling ``getaddrinfo`` ourselves, approving
the answer, and then letting Chromium connect by NAME resolves the name twice.
The second answer is the one that reaches the wire, and nothing forces it to
match the first - that is the whole DNS rebinding technique, and a check
written that way is a check the attacker simply waits out.

SO THIS OWNS THE STEP. Chromium is configured to send its traffic here and
cannot connect for itself. For each connection this seam resolves the name,
classifies EVERY address the resolver returned, selects one that is allowed,
and connects to THAT NUMERIC SOCKADDR. There is no second resolution, so
there is no window between the decision and the connection.

WHAT IT IS NOT. Not a general-purpose proxy: it speaks the two things
Chromium needs of it - ``CONNECT`` for TLS and WebSocket, and absolute-form
requests for plain HTTP - and fails closed on everything else. Not a TLS
interceptor: a ``CONNECT`` is tunnelled byte-for-byte, so TLS stays between
the browser and the destination and no local certificate authority exists.
Not an authority: it can only ever REFUSE what the origin policy already
allowed. A name resolving successfully here grants nothing.

ponytail: one thread per connection and ``Connection: close`` on every
forwarded request. Keep-alive would mean reusing a socket whose destination
was validated for an earlier request, and connection reuse is where request
smuggling lives; a browsing tool does not need the throughput.
"""

from __future__ import annotations

import socket
import threading
from typing import Any, Callable, Iterable

from jarvis.browser import private_host_refusal

__all__ = [
    "PolicyProxy",
    "ProxyRefusal",
    "proxy_arguments",
    "resolved_destination",
]


class ProxyRefusal(Exception):
    """This connection is not one JARVIS will open. Never a status code."""


#: The most of a request head this will read before giving up on it. Chromium
#: sends kilobytes; anything beyond this is not a browser being a browser.
MAX_HEAD_BYTES = 32 * 1024
#: The most header lines one request may carry.
MAX_HEADER_LINES = 200
#: How long a stalled peer may hold a connection open.
CONNECT_TIMEOUT_SECONDS = 15.0
TUNNEL_TIMEOUT_SECONDS = 300.0
#: What is copied between the two sockets at a time.
_CHUNK = 64 * 1024

#: The methods this proxy will act on. Anything else is refused: JARVIS's
#: browser has no use for them and a proxy that forwards verbs it does not
#: understand is a proxy that forwards surprises.
_FORWARDABLE = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

#: Headers that belong to the browser-to-proxy hop and must not be forwarded.
_HOP_BY_HOP = frozenset(
    {
        "proxy-connection",
        "proxy-authorization",
        "proxy-authenticate",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
    }
)


def _classify(address: str) -> str | None:
    """Why this RESOLVED address may not be connected to, or ``None``.

    The same rules the origin policy uses on a literal host, applied to what
    the resolver actually returned - so loopback, RFC1918, link-local,
    unique-local, unspecified, multicast and every other non-global block are
    refused whatever name asked for them.

    An IPv6 sockaddr can carry a zone (``fe80::1%15``), which ``ipaddress``
    will not parse. Stripped first, deliberately: an address that fails to
    parse would otherwise read as "an ordinary name" and be let through, which
    is precisely backwards for the one family whose link-local form is the
    most common local target.
    """
    return private_host_refusal(str(address).partition("%")[0].strip("[]"))


def resolved_destination(
    host: str,
    port: int,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    explicit_local: Iterable[tuple[str, int]] = (),
) -> tuple[int, tuple]:
    """The one numeric sockaddr this connection may be opened to.

    THE STEP THAT CANNOT BE SPLIT. Resolution, classification and selection
    happen here and produce a sockaddr; the caller connects to that and never
    to ``host`` again. Every address the resolver returned is judged on its
    own and only an allowed one can be selected - a refused private answer is
    never a reason to fall back to connecting by name.

    An EXPLICIT grant is the one way a local destination is reachable: the
    operator who approved ``http://127.0.0.1:8080`` gets that host and port
    and nothing else. A public-looking NAME never inherits that authority,
    however it resolves, because the grant names a host and this asks whether
    THIS host is the granted one.
    """
    granted = (host.casefold(), port) in {
        (str(name).casefold(), int(number)) for name, number in explicit_local
    }
    if not granted:
        # A literal private address asked for by name-position: refused before
        # the resolver is even consulted.
        reason = private_host_refusal(host)
        if reason is not None:
            raise ProxyRefusal(f"{host}:{port} is not an authorized destination: {reason}")
    try:
        answers = resolver(host, port, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ProxyRefusal(f"{host} could not be resolved: {exc}") from exc
    refused: list[str] = []
    for answer in answers:
        try:
            family, _kind, _proto, _canonical, sockaddr = answer
        except (TypeError, ValueError):  # pragma: no cover - resolver contract
            continue
        if not sockaddr:  # pragma: no cover - resolver contract
            continue
        reason = None if granted else _classify(sockaddr[0])
        if reason is not None:
            refused.append(f"{sockaddr[0]} ({reason})")
            continue
        # NUMERIC, and the exact one that was judged. The caller connects to
        # this tuple; nothing resolves anything again.
        return int(family), tuple(sockaddr)
    detail = "; ".join(refused) if refused else "the resolver returned nothing usable"
    raise ProxyRefusal(f"{host}:{port} resolves only to destinations JARVIS refuses: {detail}")


def _authority_of(origin: str) -> tuple[str, int]:
    """``(host, port)`` as the browser will ask a proxy for this origin."""
    from urllib.parse import urlsplit

    parsed = urlsplit(origin)
    host = (parsed.hostname or "").casefold()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, int(port)


def _split_authority(target: str) -> tuple[str, int]:
    """``host:port`` as a browser writes it, or refuse it.

    Untrusted protocol text, not a convenience parse: an ambiguous authority
    is how a proxy is talked into opening a connection its policy never saw.
    """
    value = target.strip()
    if not value or len(value) > 1024 or not value.isascii():
        raise ProxyRefusal("the proxy target is not bounded ASCII text")
    if any(char < " " or char == "\x7f" for char in value):
        raise ProxyRefusal("the proxy target carries control characters")
    if "@" in value:
        raise ProxyRefusal("the proxy target carries userinfo")
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise ProxyRefusal("the proxy target has an unclosed IPv6 literal")
        host = value[1:closing]
        rest = value[closing + 1 :]
        if not rest.startswith(":"):
            raise ProxyRefusal("the proxy target names no port")
        number = rest[1:]
    else:
        host, separator, number = value.rpartition(":")
        if not separator:
            raise ProxyRefusal("the proxy target names no port")
        if ":" in host:
            raise ProxyRefusal("the proxy target is an unbracketed IPv6 literal")
    if not host or "/" in host or "\\" in host:
        raise ProxyRefusal("the proxy target names no host")
    if not number.isdigit() or not 1 <= int(number) <= 65535:
        raise ProxyRefusal("the proxy target names no valid port")
    return host.rstrip(".").casefold(), int(number)


def _split_absolute(target: str) -> tuple[str, int, str]:
    """An absolute-form ``http://host[:port]/path`` request target."""
    if not target.lower().startswith("http://"):
        raise ProxyRefusal("the proxy will only forward absolute http requests")
    rest = target[len("http://") :]
    cut = min(
        (index for index in (rest.find("/"), rest.find("?"), rest.find("#")) if index >= 0),
        default=-1,
    )
    authority = rest if cut < 0 else rest[:cut]
    path = "/" if cut < 0 else rest[cut:]
    if ":" not in authority or authority.startswith("["):
        host, port = _split_authority(
            authority if ":" in authority.rpartition("]")[2] else f"{authority}:80"
        )
    else:
        host, port = _split_authority(authority)
    if path.startswith("#"):
        raise ProxyRefusal("the proxy target carries a fragment")
    return host, port, path


class PolicyProxy:
    """A loopback proxy that decides where JARVIS's browser may connect.

    Bound to 127.0.0.1 on an ephemeral port, started before the browser it
    serves and stopped when JARVIS stops. It holds no credential and trusts
    nothing about a peer for being local: everything a client asks for is
    parsed as untrusted protocol text and judged the same way.
    """

    def __init__(
        self,
        *,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        connect: Callable[..., Any] = socket.socket,
    ) -> None:
        self._resolver = resolver
        self._connect = connect
        self._lock = threading.Lock()
        #: Literal local destinations an operator explicitly approved, as the
        #: browser will ask for them. Never widened by a resolution.
        self._explicit_local: set[tuple[str, int]] = set()
        #: THE ONE AUTHORITY THIS PROXY SERVES, or ``None`` for the
        #: profile-level proxy that serves the browser itself.
        #:
        #: A per-tab proxy is bound to the origin its tab is approved for, and
        #: it is reachable only from that tab's browser context - so a
        #: connection arriving here came from THAT tab, and may only go to
        #: THAT origin. This is what closes BROWSER-WS-01: a WebSocket is
        #: never seen by the renderer-level gate, so it arrives at this seam
        #: with no authorization behind it, and a destination that is not the
        #: tab's own origin is refused before a packet is sent.
        self._authority: tuple[str, int] | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.port = 0
        #: What was refused, newest last. Evidence for the operator and for
        #: the tests; never a reason to allow anything.
        self.refused: list[str] = []
        #: Destinations actually connected to, as numeric sockaddrs.
        self.connected: list[tuple[str, int]] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name="jarvis-policy-proxy", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:  # pragma: no cover
                pass

    @property
    def running(self) -> bool:
        return self._listener is not None and not self._stop.is_set()

    def authorize(self, origin: str) -> None:
        """Record an origin's EXPLICIT authority over a local destination.

        Called with the origin the operator approved. It widens nothing on its
        own: only a grant whose own host is local produces an entry, and that
        entry is the exact host and port the browser will ask for.
        """
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        host = parsed.hostname
        if not host or private_host_refusal(host) is None:
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with self._lock:
            self._explicit_local.add((host.casefold(), int(port)))

    def explicit_local(self) -> frozenset[tuple[str, int]]:
        with self._lock:
            return frozenset(self._explicit_local)

    def bind_origin(self, origin: str | None) -> None:
        """Serve exactly this origin's authority, or (``None``) the browser's.

        Set from the approved plan before the tab acts, so the destination
        policy and the origin policy are the same decision seen from two
        places. Rebinding replaces: a tab navigated to a new approved origin
        does not keep the old one's reach.
        """
        with self._lock:
            self._authority = None if origin is None else _authority_of(origin)

    def authority(self) -> tuple[str, int] | None:
        with self._lock:
            return self._authority

    # -- serving -----------------------------------------------------------

    def _serve(self) -> None:  # pragma: no cover - the accept loop
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(client,), daemon=True).start()

    def _client(self, client: socket.socket) -> None:  # pragma: no cover - per connection
        try:
            client.settimeout(CONNECT_TIMEOUT_SECONDS)
            self._handle(client)
        except ProxyRefusal as refusal:
            self._note(str(refusal))
            _refuse(client, "403 Forbidden")
        except (OSError, ValueError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _note(self, reason: str) -> None:
        with self._lock:
            self.refused.append(reason)
            del self.refused[:-500]

    def _handle(self, client: socket.socket) -> None:
        head, body = _read_head(client)
        lines = head.split("\r\n")
        request = lines[0]
        parts = request.split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise ProxyRefusal("the proxy received no readable request line")
        method, target, _version = parts
        headers = _parse_headers(lines[1:])
        if method == "CONNECT":
            host, port = _split_authority(target)
            upstream = self._open(host, port)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if body:
                upstream.sendall(body)
            _tunnel(client, upstream)
            return
        if method not in _FORWARDABLE:
            raise ProxyRefusal(f"the proxy will not forward '{method[:16]}'")
        host, port, path = _split_absolute(target)
        upstream = self._open(host, port)
        upstream.sendall(_origin_form(method, path, headers, host, port))
        if body:
            upstream.sendall(body)
        _tunnel(client, upstream)

    def _open(self, host: str, port: int) -> socket.socket:
        """Resolve, judge, and connect to the NUMERIC address that was judged.

        Done afresh for every connection, so a redirect, a retry, a WebSocket
        handshake or a second page load is a second decision. A DNS answer is
        never carried forward past the connection it was made for.

        AUTHORITY FIRST, when this proxy serves one tab. Everything a page is
        allowed to request was already judged by the renderer gate, so a
        destination that is not this tab's approved origin is something the
        gate never saw - a WebSocket, which produces no interception event at
        all. It is refused here, which is the only place it is visible
        (BROWSER-WS-01).
        """
        authority = self.authority()
        if authority is not None and (host.casefold(), int(port)) != authority:
            raise ProxyRefusal(
                f"{host}:{port} is not the origin this tab is authorized for "
                f"({authority[0]}:{authority[1]}), so the connection was refused "
                "before it was opened"
            )
        family, sockaddr = resolved_destination(
            host, port, resolver=self._resolver, explicit_local=self.explicit_local()
        )
        upstream = self._connect(family, socket.SOCK_STREAM)
        try:
            upstream.settimeout(CONNECT_TIMEOUT_SECONDS)
            upstream.connect(sockaddr)
        except OSError as exc:
            try:
                upstream.close()
            except OSError:  # pragma: no cover
                pass
            raise ProxyRefusal(f"{host}:{port} could not be reached: {exc}") from exc
        with self._lock:
            self.connected.append((str(sockaddr[0]), int(sockaddr[1])))
            del self.connected[:-500]
        return upstream


def _read_head(client: socket.socket) -> tuple[str, bytes]:
    """The request head, and whatever of the body arrived with it."""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        if len(buffer) > MAX_HEAD_BYTES:
            raise ProxyRefusal("the proxy request head is over the bound")
        chunk = client.recv(_CHUNK)
        if not chunk:
            raise ProxyRefusal("the proxy client closed before sending a request")
        buffer += chunk
    head, _, body = buffer.partition(b"\r\n\r\n")
    if b"\x00" in head:
        raise ProxyRefusal("the proxy request head carries a null byte")
    try:
        return head.decode("ascii"), body
    except UnicodeDecodeError as exc:
        raise ProxyRefusal("the proxy request head is not ASCII") from exc


def _parse_headers(lines: list[str]) -> list[tuple[str, str]]:
    """Header lines as pairs, or refuse the request.

    A bare newline, a continuation line or a duplicated framing header is how
    a request is smuggled past one hop and re-read differently by the next, so
    each is refused rather than normalised.
    """
    if len(lines) > MAX_HEADER_LINES:
        raise ProxyRefusal("the proxy request carries too many headers")
    headers: list[tuple[str, str]] = []
    framing = 0
    for line in lines:
        if not line:
            continue
        if line[0] in " \t":
            raise ProxyRefusal("the proxy request folds a header line")
        name, separator, value = line.partition(":")
        if not separator or not name or any(char <= " " for char in name):
            raise ProxyRefusal("the proxy request carries an unreadable header")
        lowered = name.casefold()
        if lowered in {"content-length", "transfer-encoding"}:
            framing += 1
        headers.append((name, value.strip()))
    if framing > 1:
        raise ProxyRefusal("the proxy request frames its body more than one way")
    return headers


def _origin_form(
    method: str, path: str, headers: list[tuple[str, str]], host: str, port: int
) -> bytes:
    """The request to send upstream: origin-form, one hop, no reuse.

    ``Host`` is preserved as the browser sent it - the destination's own
    virtual hosting depends on it - and is supplied from the request target
    only if the browser sent none.
    """
    kept = [(name, value) for name, value in headers if name.casefold() not in _HOP_BY_HOP]
    if not any(name.casefold() == "host" for name, _ in kept):
        authority = f"[{host}]" if ":" in host else host
        kept.append(("Host", authority if port == 80 else f"{authority}:{port}"))
    kept.append(("Connection", "close"))
    request = f"{method} {path} HTTP/1.1\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in kept
    )
    return (request + "\r\n").encode("ascii", "strict")


def _refuse(client: socket.socket, status: str) -> None:  # pragma: no cover - best effort
    try:
        client.sendall(
            f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
        )
    except OSError:
        pass


def _tunnel(client: socket.socket, upstream: socket.socket) -> None:  # pragma: no cover
    """Copy bytes both ways until either side is done. Reads nothing into policy."""
    done = threading.Event()

    def pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while not done.is_set():
                chunk = source.recv(_CHUNK)
                if not chunk:
                    break
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            done.set()
            for side in (source, sink):
                try:
                    side.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    client.settimeout(TUNNEL_TIMEOUT_SECONDS)
    upstream.settimeout(TUNNEL_TIMEOUT_SECONDS)
    back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    back.start()
    pump(client, upstream)
    back.join(timeout=TUNNEL_TIMEOUT_SECONDS)
    try:
        upstream.close()
    except OSError:
        pass


def proxy_arguments(proxy_port: int) -> tuple[str, ...]:
    """The browser flags that make the proxy the ONLY way out.

    ``--proxy-server`` names one proxy and no ``DIRECT`` alternative, so a
    proxy that is not listening produces a failed request rather than a direct
    connection - which is the behaviour this whole seam depends on.

    ``<-loopback>`` REMOVES Chromium's built-in bypass for loopback and
    link-local names. Without it the one class of destination this exists to
    control would be the one class that never reaches the proxy.

    QUIC is disabled because it is UDP: an HTTP proxy cannot carry it, and a
    transport that cannot be mediated must not be available to a page JARVIS
    is responsible for. The same reasoning removes the built-in resolver's
    own DNS-over-HTTPS, which would otherwise resolve names in the browser
    for a connection this seam is supposed to be resolving.

    WebRTC is the other way out of a TCP proxy, and the less obvious one: its
    ICE candidates open UDP sockets straight to whatever address the page's
    script nominates, which is a page choosing its own destination - a LAN
    address included - with nothing in between.
    ``disable_non_proxied_udp`` is Chromium's own control for exactly that: it
    forces WebRTC through the configured proxy and forbids the direct UDP path,
    so a transport that cannot be mediated cannot be used.

    MEDIA ROUTER is off for the same reason. Opening any tab starts Cast
    discovery (mDNS on 5353 from the browser process), and the network
    service then opens a Cast channel - a DIRECT TCP connection to whatever
    receiver answered on the LAN (port 8009) - that no proxy ever sees
    (measured on Chrome 153 and Edge 153). Casting is not something JARVIS
    does, so the feature is disabled rather than mediated. It is in the ONE
    ``--disable-features`` switch: a second one would replace the first.
    """
    return (
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--disable-features=DnsOverHttps,AsyncDns,MediaRouter",
        "--dns-over-https-mode=off",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    )
