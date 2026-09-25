"""DB-002B-02: a hostname is not a destination.

THE GAP THIS PINS. Every layer before the proxy judges a request by its URL.
``request_destination_refusal`` asks whether the origin is approved and
whether the HOST NAME is a local one - and for an ordinary public name it
answers "nothing decided here", because a name's address is not a property of
the name. So an operator who approved ``https://approved.test`` got a browser
that would connect wherever ``approved.test`` happened to resolve, including
loopback, RFC1918, the link-local metadata address, or anything else inside
their network. ``ControlTests`` below states that gap as an assertion, so it
fails the day the URL layer starts claiming to cover it.

WHY A CHECK WAS NOT ENOUGH, and why the fix is a proxy. Resolving the name
ourselves, approving the answer, and then letting Chromium connect BY NAME
resolves it twice; the second answer is the one that reaches the wire. That is
rebinding, and a check written that way is one the attacker waits out. So
``PolicyProxy`` owns the step: it resolves, classifies every answer, selects
an allowed one, and connects to that numeric sockaddr.
``NoReResolutionTests`` proves the connect really receives numbers.

Nothing here resolves a real name or contacts anything but 127.0.0.1: the
resolver is injected, and the destinations are disposable local servers.
"""

from __future__ import annotations

import http.client
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from jarvis.browser import request_destination_refusal
from jarvis.browser_cdp import browser_arguments
from jarvis.policy_proxy import (
    PolicyProxy,
    ProxyRefusal,
    proxy_arguments,
    resolved_destination,
)

#: The approved, public-looking origin the operator authorized.
APPROVED = "https://approved.test"
APPROVED_HOST = "approved.test"

#: A public address the name is supposed to have.
PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:4700::1111"


def answers(*addresses: str, port: int = 443) -> list:
    """A ``getaddrinfo``-shaped reply naming exactly these addresses."""
    made = []
    for address in addresses:
        if ":" in address:
            made.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0)))
        else:
            made.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)))
    return made


def resolver_for(*addresses: str):
    """A resolver that always answers with ``addresses``."""

    def resolve(host, port, family=0, kind=0, *rest):
        return answers(*addresses, port=port)

    return resolve


class ControlTests(unittest.TestCase):
    """The blocker, stated as an assertion rather than as a story."""

    def test_the_url_layer_cannot_see_where_a_name_resolves(self) -> None:
        """THE BLOCKER. The approved URL is allowed no matter what the name
        resolves to, because the URL does not carry that fact at all."""
        self.assertIsNone(request_destination_refusal(f"{APPROVED}/page", APPROVED))
        # The same call is the only thing the pre-proxy architecture had, and
        # it returns the same answer whether approved.test points at a public
        # address, at 127.0.0.1 or at 169.254.169.254. There is nowhere in it
        # for an address to be considered.
        self.assertIsNone(request_destination_refusal(f"{APPROVED}/", APPROVED))

    def test_the_literal_forms_were_and_remain_covered(self) -> None:
        """What the URL layer DOES cover, so the gap is stated precisely."""
        for url in ("http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/"):
            with self.subTest(url=url):
                self.assertIsNotNone(request_destination_refusal(url, APPROVED))


class ResolvedAddressTests(unittest.TestCase):
    """Every address the resolver returns is judged on its own."""

    def destination(self, *addresses: str, explicit=()):
        return resolved_destination(
            APPROVED_HOST, 443, resolver=resolver_for(*addresses), explicit_local=explicit
        )

    def test_a_public_answer_is_allowed(self) -> None:
        family, sockaddr = self.destination(PUBLIC)
        self.assertEqual(socket.AF_INET, family)
        self.assertEqual(PUBLIC, sockaddr[0])

    def test_a_public_ipv6_answer_is_allowed(self) -> None:
        family, sockaddr = self.destination(PUBLIC_V6)
        self.assertEqual(socket.AF_INET6, family)
        self.assertEqual(PUBLIC_V6, sockaddr[0])

    def test_an_approved_name_resolving_to_loopback_is_refused(self) -> None:
        for address in ("127.0.0.1", "127.0.0.53"):
            with self.subTest(address=address):
                with self.assertRaises(ProxyRefusal):
                    self.destination(address)

    def test_an_approved_name_resolving_to_rfc1918_is_refused(self) -> None:
        for address in ("10.0.0.5", "172.16.3.4", "172.31.255.254", "192.168.1.10"):
            with self.subTest(address=address):
                with self.assertRaises(ProxyRefusal):
                    self.destination(address)

    def test_an_approved_name_resolving_to_link_local_is_refused(self) -> None:
        for address in ("169.254.169.254", "169.254.1.1"):
            with self.subTest(address=address):
                with self.assertRaises(ProxyRefusal):
                    self.destination(address)

    def test_an_approved_name_resolving_to_ipv6_loopback_is_refused(self) -> None:
        with self.assertRaises(ProxyRefusal):
            self.destination("::1")

    def test_an_approved_name_resolving_to_ipv6_ula_or_link_local_is_refused(self) -> None:
        for address in ("fc00::1", "fd12:3456::1", "fe80::1", "::"):
            with self.subTest(address=address):
                with self.assertRaises(ProxyRefusal):
                    self.destination(address)

    def test_a_zoned_ipv6_link_local_answer_is_refused(self) -> None:
        """A scope suffix must not make an address unparseable and therefore
        look like an ordinary name."""
        with self.assertRaises(ProxyRefusal):
            self.destination("fe80::1%15")

    def test_an_ipv4_mapped_private_answer_is_refused(self) -> None:
        for address in ("::ffff:127.0.0.1", "::ffff:192.168.1.10", "::ffff:169.254.169.254"):
            with self.subTest(address=address):
                with self.assertRaises(ProxyRefusal):
                    self.destination(address)

    def test_multicast_and_unspecified_answers_are_refused(self) -> None:
        for address in ("224.0.0.1", "0.0.0.0", "ff02::1"):
            with self.subTest(address=address):
                with self.assertRaises(ProxyRefusal):
                    self.destination(address)

    def test_only_an_independently_allowed_answer_may_be_selected(self) -> None:
        """A refused private answer is never a reason to fall back to the name."""
        family, sockaddr = self.destination("192.168.1.10", "127.0.0.1", PUBLIC)
        self.assertEqual(PUBLIC, sockaddr[0])
        self.assertEqual(socket.AF_INET, family)

    def test_a_set_of_answers_that_is_entirely_private_is_refused(self) -> None:
        with self.assertRaises(ProxyRefusal) as raised:
            self.destination("192.168.1.10", "127.0.0.1", "fe80::1")
        # The refusal names what it saw, so an operator can tell why.
        self.assertIn("192.168.1.10", str(raised.exception))

    def test_an_empty_answer_set_is_refused(self) -> None:
        with self.assertRaises(ProxyRefusal):
            resolved_destination(APPROVED_HOST, 443, resolver=lambda *a: [])

    def test_a_resolution_failure_is_refused_not_ignored(self) -> None:
        def broken(*args):
            raise socket.gaierror("no such host")

        with self.assertRaises(ProxyRefusal):
            resolved_destination(APPROVED_HOST, 443, resolver=broken)

    def test_the_answer_changing_between_connections_is_judged_each_time(self) -> None:
        """Rebinding proper: the first connection is fine, the second is not,
        and the second one is the one that matters."""
        sequence = [answers(PUBLIC), answers("127.0.0.1")]

        def rebinding(host, port, *rest):
            return sequence.pop(0)

        _family, sockaddr = resolved_destination(APPROVED_HOST, 443, resolver=rebinding)
        self.assertEqual(PUBLIC, sockaddr[0])
        with self.assertRaises(ProxyRefusal):
            resolved_destination(APPROVED_HOST, 443, resolver=rebinding)


class ExplicitLocalAuthorityTests(unittest.TestCase):
    """An operator may approve a local service. A name may not become one."""

    def test_an_explicitly_granted_literal_local_origin_is_reachable(self) -> None:
        family, sockaddr = resolved_destination(
            "127.0.0.1",
            8080,
            resolver=resolver_for("127.0.0.1"),
            explicit_local=[("127.0.0.1", 8080)],
        )
        self.assertEqual(("127.0.0.1", 8080), (sockaddr[0], sockaddr[1]))
        self.assertEqual(socket.AF_INET, family)

    def test_the_grant_covers_that_host_and_port_and_nothing_else(self) -> None:
        for host, port in (("127.0.0.1", 9999), ("192.168.1.10", 8080), ("127.0.0.2", 8080)):
            with self.subTest(host=host, port=port):
                with self.assertRaises(ProxyRefusal):
                    resolved_destination(
                        host,
                        port,
                        resolver=resolver_for(host),
                        explicit_local=[("127.0.0.1", 8080)],
                    )

    def test_a_public_name_resolving_local_does_not_inherit_that_grant(self) -> None:
        """THE DISTINCTION. attacker.example -> 127.0.0.1 is not the operator
        approving http://127.0.0.1:8080."""
        with self.assertRaises(ProxyRefusal):
            resolved_destination(
                "attacker.example",
                8080,
                resolver=resolver_for("127.0.0.1"),
                explicit_local=[("127.0.0.1", 8080)],
            )

    def test_authorize_records_only_a_local_origin(self) -> None:
        proxy = PolicyProxy()
        proxy.authorize("https://example.com")
        proxy.authorize("https://approved.test:8443")
        self.assertEqual(frozenset(), proxy.explicit_local())
        proxy.authorize("http://127.0.0.1:8080")
        proxy.authorize("http://localhost:3000")
        self.assertEqual(
            {("127.0.0.1", 8080), ("localhost", 3000)}, set(proxy.explicit_local())
        )

    def test_authorize_defaults_the_port_from_the_scheme(self) -> None:
        proxy = PolicyProxy()
        proxy.authorize("http://127.0.0.1")
        proxy.authorize("https://[::1]")
        self.assertEqual({("127.0.0.1", 80), ("::1", 443)}, set(proxy.explicit_local()))


class TargetParsingTests(unittest.TestCase):
    """Browser-to-proxy text is untrusted protocol input."""

    def refuse(self, target: str) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        with self.assertRaises(ProxyRefusal):
            proxy._handle(_Head(f"CONNECT {target} HTTP/1.1\r\nHost: x\r\n\r\n"))

    def test_a_malformed_connect_target_fails_closed(self) -> None:
        for target in (
            "",
            "host",
            "host:",
            "host:0",
            "host:65536",
            "host:https",
            "host:-1",
            "user@host:443",
            "[::1:443",
            "::1:443",
            "ho st:443",
            "host:443/path",
            "host\\x:443",
            "h" * 2000 + ":443",
        ):
            with self.subTest(target=target):
                self.refuse(target)

    def test_a_request_line_that_is_not_a_request_line_fails_closed(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        for head in ("GARBAGE\r\n\r\n", "GET\r\n\r\n", "GET / SPDY/3\r\n\r\n"):
            with self.subTest(head=head):
                with self.assertRaises(ProxyRefusal):
                    proxy._handle(_Head(head))

    def test_an_unsupported_method_fails_closed(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        with self.assertRaises(ProxyRefusal):
            proxy._handle(_Head("TRACE http://approved.test/ HTTP/1.1\r\n\r\n"))

    def test_a_non_http_absolute_target_fails_closed(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        for target in ("ftp://approved.test/", "file:///etc/passwd", "/relative"):
            with self.subTest(target=target):
                with self.assertRaises(ProxyRefusal):
                    proxy._handle(_Head(f"GET {target} HTTP/1.1\r\n\r\n"))

    def test_a_doubly_framed_body_fails_closed(self) -> None:
        """Content-Length and Transfer-Encoding together is smuggling."""
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        with self.assertRaises(ProxyRefusal):
            proxy._handle(
                _Head(
                    "POST http://approved.test/ HTTP/1.1\r\nHost: approved.test\r\n"
                    "Content-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n"
                )
            )

    def test_a_folded_header_fails_closed(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        with self.assertRaises(ProxyRefusal):
            proxy._handle(
                _Head("GET http://approved.test/ HTTP/1.1\r\nHost: a\r\n  folded\r\n\r\n")
            )

    def test_an_oversized_head_fails_closed(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        with self.assertRaises(ProxyRefusal):
            proxy._handle(_Head("GET http://approved.test/ HTTP/1.1\r\n" + "A: b\r\n" * 400 + "\r\n"))


class _Head:
    """A socket stand-in that yields one request head and nothing more."""

    def __init__(self, text: str) -> None:
        self._data = text.encode("ascii")

    def settimeout(self, value):  # pragma: no cover - interface
        pass

    def recv(self, size):
        data, self._data = self._data[:size], self._data[size:]
        return data

    def sendall(self, data):  # pragma: no cover - refusals never reach this
        raise AssertionError("the proxy sent data for a request it should have refused")


class _Destination(HTTPServer):
    """A disposable local server standing in for the real destination."""

    def __init__(self) -> None:
        self.received: list[str] = []
        super().__init__(("127.0.0.1", 0), _DestinationHandler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _DestinationHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the stdlib spelling
        self.server.received.append(f"{self.path} host={self.headers.get('Host')}")
        body = b"reached"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover - quiet
        pass


class ProxiedTrafficTests(unittest.TestCase):
    """The proxy really forwards, and really refuses, over real sockets."""

    def setUp(self) -> None:
        self.destination = _Destination()
        self.addCleanup(self.destination.stop)
        self.forbidden = _Destination()
        self.addCleanup(self.forbidden.stop)

    def proxy(self, *addresses: str, explicit=()) -> PolicyProxy:
        proxy = PolicyProxy(resolver=resolver_for(*addresses))
        for origin in explicit:
            proxy.authorize(origin)
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def request(self, proxy: PolicyProxy, url: str, host: str = APPROVED_HOST):
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=15)
        try:
            connection.request("GET", url, headers={"Host": host})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_an_allowed_http_request_is_forwarded_to_the_destination(self) -> None:
        """Requirement 13, over a real socket to a real server."""
        # The name resolves to a loopback destination only because the test's
        # own server lives there; the grant makes it explicit authority, which
        # is the only way a local address is ever reachable.
        proxy = self.proxy(
            "127.0.0.1", explicit=[f"http://127.0.0.1:{self.destination.port}"]
        )
        status, body = self.request(
            proxy,
            f"http://127.0.0.1:{self.destination.port}/page",
            host=f"127.0.0.1:{self.destination.port}",
        )
        self.assertEqual(200, status)
        self.assertEqual(b"reached", body)
        self.assertEqual(1, len(self.destination.received))
        self.assertIn("/page", self.destination.received[0])
        # Host semantics preserved, and the connection really went to numbers.
        self.assertIn(f"host=127.0.0.1:{self.destination.port}", self.destination.received[0])
        self.assertEqual([("127.0.0.1", self.destination.port)], proxy.connected)

    def test_a_name_resolving_to_a_forbidden_address_never_reaches_it(self) -> None:
        """THE FIX, over real sockets: B's own account of its wire."""
        proxy = self.proxy("127.0.0.1")
        status, _body = self.request(
            proxy, f"http://{APPROVED_HOST}:{self.forbidden.port}/exfil"
        )
        self.assertEqual(403, status)
        self.assertEqual([], self.forbidden.received)
        self.assertEqual([], proxy.connected)
        self.assertTrue(any("loopback" in one for one in proxy.refused))

    def test_a_connect_tunnel_is_established_and_refused_by_the_same_rule(self) -> None:
        """Requirement 14/15: CONNECT is how TLS and WSS leave the browser."""
        allowed = self.proxy(
            "127.0.0.1", explicit=[f"https://127.0.0.1:{self.destination.port}"]
        )
        opened = _connect(allowed.port, f"127.0.0.1:{self.destination.port}")
        self.assertIn(b"200", opened)
        self.assertEqual([("127.0.0.1", self.destination.port)], allowed.connected)

        refused = self.proxy("127.0.0.1")
        answer = _connect(refused.port, f"{APPROVED_HOST}:{self.forbidden.port}")
        self.assertIn(b"403", answer)
        self.assertEqual([], self.forbidden.received)
        self.assertEqual([], refused.connected)

    def test_each_connection_is_resolved_and_judged_again(self) -> None:
        """Requirement 19: a redirect, a retry or a second load is a second
        decision, so a name that turns private is caught on the next one."""
        sequence = [answers("127.0.0.1", port=self.destination.port), answers("127.0.0.1")]
        calls: list[int] = []

        def rebinding(host, port, *rest):
            calls.append(port)
            return sequence.pop(0) if sequence else answers("127.0.0.1", port=port)

        proxy = PolicyProxy(resolver=rebinding)
        proxy.authorize(f"http://127.0.0.1:{self.destination.port}")
        proxy.start()
        self.addCleanup(proxy.stop)
        first, _ = self.request(
            proxy,
            f"http://127.0.0.1:{self.destination.port}/one",
            host=f"127.0.0.1:{self.destination.port}",
        )
        self.assertEqual(200, first)
        second, _ = self.request(
            proxy, f"http://{APPROVED_HOST}:{self.forbidden.port}/two"
        )
        self.assertEqual(403, second)
        self.assertEqual(2, len(calls), "the second connection reused the first decision")
        self.assertEqual([], self.forbidden.received)


class NoReResolutionTests(unittest.TestCase):
    """The connect receives numbers, never the name that was judged."""

    def test_the_upstream_connect_is_given_the_validated_sockaddr(self) -> None:
        """Requirement 9: this is what removes the TOCTOU."""
        attempted: list[object] = []

        class _Socket:
            def __init__(self, family, kind):
                self.family = family

            def settimeout(self, value):
                pass

            def connect(self, address):
                attempted.append(address)
                raise OSError("not actually connecting")

            def close(self):
                pass

        proxy = PolicyProxy(resolver=resolver_for(PUBLIC), connect=_Socket)
        with self.assertRaises(ProxyRefusal):
            proxy._open(APPROVED_HOST, 443)
        self.assertEqual(1, len(attempted))
        address = attempted[0]
        self.assertEqual(PUBLIC, address[0])
        self.assertEqual(443, address[1])
        # A NUMERIC address, not the hostname that was judged.
        self.assertNotIn(APPROVED_HOST, str(address))
        socket.inet_aton(address[0])


class BrowserConfigurationTests(unittest.TestCase):
    """Chromium must not have a way around the boundary."""

    def arguments(self) -> tuple[str, ...]:
        return browser_arguments(
            profile_dir="C:/jarvis/browser-profile", port=9333, proxy_port=45678
        )

    def test_the_proxy_is_the_only_route_and_there_is_no_direct_fallback(self) -> None:
        """Requirement 17: one proxy, no DIRECT alternative in the list."""
        arguments = self.arguments()
        self.assertIn("--proxy-server=http://127.0.0.1:45678", arguments)
        self.assertFalse(
            any("DIRECT" in one.upper() for one in arguments),
            "a DIRECT fallback would make the boundary optional",
        )

    def test_the_built_in_loopback_bypass_is_removed(self) -> None:
        """Requirement 16: otherwise the one class this exists to control is
        the one class that never reaches the proxy."""
        self.assertIn("--proxy-bypass-list=<-loopback>", self.arguments())

    def test_quic_and_browser_side_dns_are_disabled(self) -> None:
        """Requirement 18: UDP cannot be carried by an HTTP proxy, and the
        browser resolving names itself is the thing being taken away."""
        arguments = self.arguments()
        self.assertIn("--disable-quic", arguments)
        self.assertIn("--disable-features=DnsOverHttps,AsyncDns,MediaRouter", arguments)
        self.assertIn("--dns-over-https-mode=off", arguments)

    def test_one_disable_features_switch_carries_every_disabled_feature(self) -> None:
        """Gate 3B. Chromium keeps only the LAST ``--disable-features``, so a
        second switch would silently re-enable everything the first named.
        MediaRouter is there because Cast discovery opened a direct TCP
        connection to a LAN receiver (port 8009) that no proxy saw."""
        for arguments in (
            self.arguments(),
            browser_arguments(profile_dir="C:/jarvis/chrome-profile", port=9333, proxy_port=45678),
        ):
            switches = [one for one in arguments if one.startswith("--disable-features")]
            self.assertEqual(1, len(switches), switches)
            features = switches[0].split("=", 1)[1].split(",")
            self.assertEqual(["DnsOverHttps", "AsyncDns", "MediaRouter"], features)
            self.assertEqual(len(features), len(set(features)))
            for flag in (
                "--disable-quic",
                "--dns-over-https-mode=off",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--proxy-server=http://127.0.0.1:45678",
                "--proxy-bypass-list=<-loopback>",
            ):
                self.assertIn(flag, arguments)

    def test_webrtc_cannot_open_its_own_udp_path(self) -> None:
        """Requirement 18's other half: ICE would let a page's script pick a
        LAN address and reach it over UDP, around any TCP proxy."""
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", self.arguments()
        )

    def test_the_dedicated_profile_is_still_the_only_one_touched(self) -> None:
        """Requirement 21: the operator's own browser is not in scope."""
        arguments = self.arguments()
        self.assertIn("--user-data-dir=C:/jarvis/browser-profile", arguments)
        self.assertIn("--remote-debugging-port=9333", arguments)
        self.assertIn("about:blank", arguments)

    def test_a_browser_cannot_be_launched_without_naming_a_boundary(self) -> None:
        """No default, so no call site can quietly produce an unproxied one."""
        with self.assertRaises(TypeError):
            browser_arguments(profile_dir="C:/x", port=9333)

    def test_the_proxy_flags_are_exactly_what_the_browser_gets(self) -> None:
        for flag in proxy_arguments(45678):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.arguments())


class ProxyLifetimeTests(unittest.TestCase):
    """Loopback only, ephemeral port, and gone when JARVIS is."""

    def test_the_proxy_binds_only_loopback_on_an_ephemeral_port(self) -> None:
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        self.addCleanup(proxy.stop)
        port = proxy.start()
        self.assertEqual(port, proxy.port)
        self.assertNotEqual(0, port)
        self.assertTrue(proxy.running)
        # Reachable on loopback...
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            self.assertIsNotNone(sock)

    def test_stopping_the_proxy_makes_it_unreachable(self) -> None:
        """Requirement 17's other half: after this, a browser pointed here
        fails rather than connecting directly."""
        proxy = PolicyProxy(resolver=resolver_for(PUBLIC))
        port = proxy.start()
        proxy.stop()
        self.assertFalse(proxy.running)
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(b"GET http://approved.test/ HTTP/1.1\r\n\r\n")
                if not sock.recv(16):
                    raise OSError("closed")


def _connect(proxy_port: int, target: str) -> bytes:
    """Send one CONNECT and return the proxy's answer line."""
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=15) as sock:
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
        return sock.recv(4096)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
