"""BROWSER-WS-01: a WebSocket is invisible to every renderer-level gate.

THE GAP THIS PINS. ``Fetch.requestPaused`` is not produced for a WebSocket
handshake, so ``BrowserPolicy`` - which judges every other renderer request -
never sees one. An approved page could therefore open
``new WebSocket("wss://unapproved.example/socket")`` and the only thing left on
the path was the pre-connect proxy, which judged the ADDRESS and not the tab's
origin authority. Worse, that proxy's explicit local grants were global and
never revoked, so a later unrelated tab could reach a local service an earlier
tab had been granted.

WHAT WAS MEASURED, against the installed Microsoft Edge 153.0.4234.48, before
anything here was written:

  * an approved page's WebSocket reached the recording listener from the main
    frame, an iframe, a dedicated worker, a service worker and a delayed timer
    - five separate wire connections;
  * ``--disable-web-sockets`` is IGNORED: the same five connections happened;
  * CDP ``Network.setBlockedURLs`` with ws/wss patterns does not cover
    WebSockets either: the connections still happened;
  * ``ws://`` reaches the proxy as ``CONNECT host:port``, exactly like HTTPS,
    so the proxy cannot tell one from the other by inspection and could never
    have blocked WSS without terminating TLS.

So there is no browser switch to rely on. What there IS, measured the same
way, is ``Target.createBrowserContext(proxyServer=...)``: a tab created in its
own browser context sends ALL of its traffic - WebSockets included - to that
context's own proxy. That gives the one thing a renderer gate cannot: per-tab
attribution for a connection nobody announced.

SO THE FIX IS AUTHORITY AT THE BOUNDARY. Each JARVIS tab gets its own context
and its own boundary, bound to that tab's approved origin. Everything the page
may legitimately request was already judged by the renderer gate and is on that
origin; a WebSocket to anywhere else is refused before a packet is sent, and
one tab's authority is not reachable from another because it is not the same
boundary.

Nothing here contacts anything but 127.0.0.1.
"""

from __future__ import annotations

import http.client
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from jarvis.browser import BROWSER_CAPABILITY_METHODS, BROWSER_POLICY_METHODS
from jarvis.browser_cdp import BrowserPolicy, browser_arguments, tab_boundary
from jarvis.policy_proxy import PolicyProxy, ProxyRefusal

APPROVED = "https://approved.test"
UNAPPROVED_HOST = "unapproved.example"
PUBLIC = "93.184.216.34"


def resolver_for(*addresses: str):
    def resolve(host, port, family=0, kind=0, *rest):
        made = []
        for address in addresses:
            if ":" in address:
                made.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0)))
            else:
                made.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)))
        return made

    return resolve


class _Listener(HTTPServer):
    """A disposable destination that records what actually reaches its wire."""

    def __init__(self) -> None:
        self.received: list[str] = []
        super().__init__(("127.0.0.1", 0), _Handler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the stdlib spelling
        self.server.received.append(self.path)
        body = b"reached"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover - quiet
        pass


class WebSocketCase(unittest.TestCase):
    """One approved origin, one unapproved destination, one bound boundary."""

    def setUp(self) -> None:
        self.approved = _Listener()
        self.addCleanup(self.approved.stop)
        self.forbidden = _Listener()
        self.addCleanup(self.forbidden.stop)
        self.origin = f"http://127.0.0.1:{self.approved.port}"

    def bound(self, *addresses: str, origin: str | None = None) -> PolicyProxy:
        """A per-tab boundary bound to ``origin``, as a mediated tab's is."""
        proxy = PolicyProxy(resolver=resolver_for(*(addresses or ("127.0.0.1",))))
        proxy.bind_origin(self.origin if origin is None else origin)
        proxy.authorize(self.origin if origin is None else origin)
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def connect(self, proxy: PolicyProxy, target: str) -> bytes:
        """One CONNECT, which is how BOTH ws:// and wss:// leave the browser."""
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=15) as sock:
            sock.sendall(
                f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii")
            )
            return sock.recv(4096)


class UnauthorizedWebSocketTests(WebSocketCase):
    """The blocker: a WebSocket to anywhere but this tab's origin."""

    def test_a_websocket_to_an_unapproved_origin_never_reaches_the_wire(self) -> None:
        """Requirements 1 and 2. ws:// and wss:// are the same CONNECT here,
        which is why one rule covers both."""
        proxy = self.bound()
        answer = self.connect(proxy, f"{UNAPPROVED_HOST}:{self.forbidden.port}")
        self.assertIn(b"403", answer)
        self.assertEqual([], self.forbidden.received, "the forbidden listener was reached")
        self.assertEqual([], proxy.connected)
        self.assertTrue(any("not the origin this tab" in one for one in proxy.refused))

    def test_it_is_refused_whichever_target_opened_it(self) -> None:
        """Requirements 3-8. The boundary sees a connection, not a frame, so a
        main frame, an iframe, a worker, a service worker, a timer and a click
        handler are one case - which is why it covers the ones a renderer gate
        cannot see at all. The real-Edge run behind this file exercised all
        six and the listener accepted nothing."""
        proxy = self.bound()
        for label in ("main", "iframe", "worker", "sw", "delayed", "click"):
            with self.subTest(origin_of_attempt=label):
                answer = self.connect(proxy, f"{UNAPPROVED_HOST}:{self.forbidden.port}")
                self.assertIn(b"403", answer)
        self.assertEqual([], self.forbidden.received)

    def test_a_websocket_to_another_port_of_the_same_host_is_refused(self) -> None:
        """An origin is host AND port, at this layer as at every other."""
        proxy = self.bound()
        answer = self.connect(proxy, f"127.0.0.1:{self.forbidden.port}")
        self.assertIn(b"403", answer)
        self.assertEqual([], self.forbidden.received)

    def test_the_refusal_happens_before_any_resolution(self) -> None:
        """Nothing is even looked up for a destination that is not authorized."""
        looked: list[str] = []

        def resolver(host, port, *rest):
            looked.append(host)
            return resolver_for("127.0.0.1")(host, port)

        proxy = PolicyProxy(resolver=resolver)
        proxy.bind_origin(self.origin)
        proxy.start()
        self.addCleanup(proxy.stop)
        self.connect(proxy, f"{UNAPPROVED_HOST}:{self.forbidden.port}")
        self.assertEqual([], looked)


class UsabilityTests(WebSocketCase):
    """The tab's own origin still works, or the fix is not one."""

    def test_the_tabs_own_origin_still_loads_through_its_boundary(self) -> None:
        """Requirements 9, 10 and 11."""
        proxy = self.bound()
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=15)
        self.addCleanup(connection.close)
        connection.request(
            "GET",
            f"http://127.0.0.1:{self.approved.port}/page",
            headers={"Host": f"127.0.0.1:{self.approved.port}"},
        )
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.assertEqual(b"reached", response.read())
        self.assertIn("/page", self.approved.received)
        self.assertEqual([("127.0.0.1", self.approved.port)], proxy.connected)

    def test_a_connect_tunnel_to_the_tabs_own_origin_is_established(self) -> None:
        proxy = self.bound()
        answer = self.connect(proxy, f"127.0.0.1:{self.approved.port}")
        self.assertIn(b"200", answer)
        self.assertEqual([("127.0.0.1", self.approved.port)], proxy.connected)

    def test_rebinding_replaces_rather_than_accumulates(self) -> None:
        """A tab navigated to a new approved origin does not keep the old
        origin's reach."""
        proxy = self.bound()
        second = f"http://127.0.0.1:{self.forbidden.port}"
        proxy.bind_origin(second)
        self.assertEqual(("127.0.0.1", self.forbidden.port), proxy.authority())
        answer = self.connect(proxy, f"127.0.0.1:{self.approved.port}")
        self.assertIn(b"403", answer)

    def test_the_profile_level_boundary_has_no_origin_authority(self) -> None:
        """The browser's own traffic is not a tab's, so it is judged by
        address alone - as it was before any of this."""
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        self.assertIsNone(proxy.authority())


class CrossTabTests(WebSocketCase):
    """One tab's authority is not reachable from another."""

    def test_a_second_tab_cannot_use_the_first_tabs_local_grant(self) -> None:
        """THE SECOND BLOCKER. The grant was global and never revoked, so a
        later unrelated tab could open a WebSocket to a local service an
        earlier tab had been authorized for."""
        first = self.bound()
        second = self.bound(origin="http://127.0.0.1:1")
        # The first tab reaches its own local service.
        self.assertIn(b"200", self.connect(first, f"127.0.0.1:{self.approved.port}"))
        # The second tab, on its own boundary, cannot.
        self.assertIn(b"403", self.connect(second, f"127.0.0.1:{self.approved.port}"))
        self.assertEqual([], second.connected)

    def test_each_tab_gets_a_boundary_of_its_own(self) -> None:
        one = tab_boundary("ws://127.0.0.1:1/x")
        self.addCleanup(one.stop)
        two = tab_boundary("ws://127.0.0.1:1/x")
        self.addCleanup(two.stop)
        self.assertNotEqual(one.port, two.port)
        self.assertIsNone(one.authority(), "a fresh boundary carries no authority")

    def test_an_explicit_local_grant_belongs_to_the_policy_that_made_it(self) -> None:
        """Requirement 14: the grant still works, for its own tab only."""
        proxy = self.bound()
        self.assertIn(("127.0.0.1", self.approved.port), proxy.explicit_local())
        other = self.bound(origin="http://127.0.0.1:1")
        self.assertNotIn(("127.0.0.1", self.approved.port), other.explicit_local())


class GrantLifetimeTests(WebSocketCase):
    """A boundary does not outlive the tab whose authority it carries."""

    def policy(self) -> tuple[BrowserPolicy, PolicyProxy]:
        proxy = PolicyProxy(resolver=resolver_for("127.0.0.1"))
        proxy.start()
        policy = BrowserPolicy(
            "ws://127.0.0.1:1/x", tab_id="T" * 32, origin=self.origin, proxy=proxy
        )
        policy.bind(self.origin)
        return policy, proxy

    def test_binding_a_policy_binds_its_boundary_to_the_same_origin(self) -> None:
        policy, proxy = self.policy()
        self.addCleanup(proxy.stop)
        self.assertEqual(("127.0.0.1", self.approved.port), proxy.authority())

    def test_releasing_the_policy_stops_the_boundary(self) -> None:
        """Requirement 16: closing the tab removes the authorization."""
        policy, proxy = self.policy()
        port = proxy.port
        policy.release()
        self.assertFalse(proxy.running)
        self.assertIsNone(policy.proxy)
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
                if not sock.recv(16):
                    raise OSError("closed")

    def test_a_policy_that_fails_closed_stops_its_boundary_too(self) -> None:
        policy, proxy = self.policy()
        policy._fail_closed()
        self.assertFalse(proxy.running)
        self.assertIsNone(policy.proxy)

    def test_the_context_is_disposed_with_the_tab(self) -> None:
        """The context is what routed the tab to its boundary; a context with
        no tab is a network identity with nothing attached to it."""
        self.assertIn("Target.disposeBrowserContext", BROWSER_POLICY_METHODS)
        self.assertIn(
            "Target.createBrowserContext", BROWSER_CAPABILITY_METHODS["browser_tab_open"]
        )


class PreservedHardeningTests(unittest.TestCase):
    """What this change must not have undone."""

    def arguments(self) -> tuple[str, ...]:
        return browser_arguments(
            profile_dir="C:/jarvis/browser-profile", port=9333, proxy_port=45678
        )

    def test_the_proxy_is_still_mandatory_with_no_direct_fallback(self) -> None:
        arguments = self.arguments()
        self.assertIn("--proxy-server=http://127.0.0.1:45678", arguments)
        self.assertFalse(any("DIRECT" in one.upper() for one in arguments))
        self.assertIn("--proxy-bypass-list=<-loopback>", arguments)

    def test_quic_and_webrtc_hardening_is_preserved(self) -> None:
        arguments = self.arguments()
        self.assertIn("--disable-quic", arguments)
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", arguments
        )

    def test_no_capability_can_reach_the_runtime_domain(self) -> None:
        for name, methods in BROWSER_CAPABILITY_METHODS.items():
            with self.subTest(capability=name):
                self.assertFalse(any(one.startswith("Runtime.") for one in methods))
        self.assertFalse(any(one.startswith("Runtime.") for one in BROWSER_POLICY_METHODS))

    def test_the_policy_still_cannot_disarm_its_own_gate(self) -> None:
        self.assertNotIn("Fetch.disable", BROWSER_POLICY_METHODS)


class RefusalShapeTests(WebSocketCase):
    """A refusal says what it refused and why, without inventing authority."""

    def test_the_boundary_never_creates_authority(self) -> None:
        """Bound to an origin, a resolvable public address is still refused if
        it is not that origin."""
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        proxy.bind_origin(APPROVED)
        with self.assertRaises(ProxyRefusal):
            proxy._open("elsewhere.test", 443)

    def test_the_bound_origin_is_reachable_by_its_default_port(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        proxy.bind_origin(APPROVED)
        self.assertEqual(("approved.test", 443), proxy.authority())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
