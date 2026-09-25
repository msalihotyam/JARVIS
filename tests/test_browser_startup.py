"""Opening a tab is a transaction, or it is a way to leak one.

THE GAP THIS PINS. ``browser_tab_open`` used to record the new tab as current
BEFORE it attached to it. A failure at the attach - the first thing that talks
to the page - therefore left behind a browser context, a blank target, a
current-tab note naming it, and a per-tab boundary still listening with its
authority never bound to anything. Nothing owned any of it.

WHY THAT MATTERED. A later ``browser_navigate`` resolved that target purely
because the note named it, built mediation over it, and got a policy with no
boundary of its own. The tab's traffic then left through the leaked,
unbound boundary - and an unbound boundary refuses nothing on origin grounds,
so an approved page could open a WebSocket to any off-origin destination
through it. ``BeforeFixTests`` below states each half of that as an assertion
about the pieces, so the shape cannot come back unnoticed.

WHAT IS PINNED NOW. Every resource is provisional until one success point:
mediation holding the target, the context and the boundary together. Any
failure before it undoes everything; the current-tab note is written only
after it; and tab RESOLUTION refuses any target a live policy does not fully
own, so even a leak that somehow survived could not be adopted.

Nothing here contacts anything but 127.0.0.1.
"""

from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import jarvis.browser_cdp as cdp
from jarvis.browser import (
    BrowserResource,
    BrowserResourceKind,
    browser_operation_plan,
    build_browser_request_frame,
)
from jarvis.browser_cdp import (
    BrowserCommandFailed,
    BrowserPolicy,
    owning_policy,
    read_current_tab,
    release_browser_policies,
    run_operation_plan,
    write_current_tab,
)
from jarvis.policy_proxy import PolicyProxy, ProxyRefusal
from tests.helpers import safe_cleanup
from tests.test_browser_minimum import TAB, FakeBrowser, _GatedNavigator, _Origin, page_answers

#: A public-looking destination that is not the approved origin - what an
#: approved page would aim a WebSocket at.
OFF_ORIGIN = "unapproved.example"


def resolver_for(address: str = "93.184.216.34"):
    def resolve(host, port, family=0, kind=0, *rest):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


class StartupCase(unittest.TestCase):
    """One approved origin, one tab open attempt, one look at the wreckage."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.profile = str(Path(self.temp.name) / "browser-profile")
        self.a = _Origin({})
        self.addCleanup(self.a.stop)
        self.origin = self.a.origin
        self.policies: dict[str, BrowserPolicy] = {}
        self.addCleanup(release_browser_policies, self.policies)
        self.navigator = _GatedNavigator()
        #: Every boundary ``browser_tab_open`` made, so a test can ask whether
        #: one was left listening.
        self.boundaries: list[PolicyProxy] = []
        original = cdp.tab_boundary
        self.real_tab_boundary = original

        def watched(socket_url, *, origin):
            made = PolicyProxy(resolver=resolver_for())
            made.start()
            made.bind_origin(origin)
            made.authorize(origin)
            self.boundaries.append(made)
            return made

        cdp.tab_boundary = watched
        self.addCleanup(setattr, cdp, "tab_boundary", original)
        self.addCleanup(self.stop_boundaries)

    def stop_boundaries(self) -> None:
        for made in self.boundaries:
            try:
                made.stop()
            except OSError:
                pass

    def start_browser(self, *, silent=None, **answers) -> FakeBrowser:
        scripted = page_answers(
            **{
                "Target.getTargets": {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": TAB,
                            "url": f"{self.origin}/",
                            "title": "A",
                            "browserContextId": "CTX1",
                        }
                    ]
                },
                "Target.createTarget": {"targetId": TAB},
                "Page.getNavigationHistory": lambda params: {
                    "currentIndex": 0,
                    "entries": [{"url": self.navigator.landed or f"{self.origin}/", "title": "A"}],
                },
                **answers,
            }
        )
        browser = FakeBrowser(scripted, navigator=self.navigator, silent=silent)
        self.addCleanup(browser.stop)
        self.browser = browser
        return browser

    def run_capability(self, capability: str, **fields):
        kind = {
            "browser_tab_open": BrowserResourceKind.NEW_TAB,
            "browser_navigate": BrowserResourceKind.NAVIGATION,
            "browser_page_read": BrowserResourceKind.PAGE,
        }[capability]
        frame = build_browser_request_frame(
            capability=capability,
            destination=f"browser/{self.origin}",
            resource=BrowserResource(kind=kind, origin=self.origin, **fields),
        )
        return run_operation_plan(
            browser_operation_plan(frame),
            frame=frame,
            profile_dir=self.profile,
            attach=lambda **kwargs: self.browser.socket_url,
            sleep=lambda seconds: None,
            policies=self.policies,
        )

    def open_tab(self):
        return self.run_capability("browser_tab_open", url=f"{self.origin}/final")

    # -- what must be true after ANY failed startup ------------------------

    def assert_nothing_reusable(self) -> None:
        self.assertEqual(
            "", read_current_tab(self.profile), "a failed startup was published as current"
        )
        self.assertEqual({}, self.policies, "a failed startup left a policy behind")
        self.assertIsNone(owning_policy(self.policies, TAB))
        for made in self.boundaries:
            self.assertFalse(made.running, "a boundary was left listening with no tab")
        # And a later operation cannot find it.
        with self.assertRaises(BrowserCommandFailed):
            self.run_capability("browser_navigate", url=f"{self.origin}/next")
        with self.assertRaises(BrowserCommandFailed):
            self.run_capability("browser_page_read")


class BeforeFixTests(unittest.TestCase):
    """The defect, stated as assertions about the pieces it was made of."""

    def test_an_unbound_boundary_would_open_an_off_origin_connection(self) -> None:
        """Why a leaked boundary was the whole problem: with no authority
        bound, an off-origin destination is simply connected to."""
        attempted: list = []

        class _Socket:
            def __init__(self, family, kind):
                pass

            def settimeout(self, value):
                pass

            def connect(self, address):
                attempted.append(address)

            def close(self):  # pragma: no cover - never reached
                pass

        leaked = PolicyProxy(resolver=resolver_for(), connect=_Socket)
        self.assertIsNone(leaked.authority(), "a leaked boundary carries no origin")
        leaked._open(OFF_ORIGIN, 443)
        self.assertEqual([("93.184.216.34", 443)], attempted)

    def test_a_bound_boundary_is_what_refuses_it(self) -> None:
        bound = PolicyProxy(resolver=resolver_for())
        bound.bind_origin("https://approved.test")
        with self.assertRaises(ProxyRefusal):
            bound._open(OFF_ORIGIN, 443)

    def test_ownership_requires_every_part_of_the_relationship(self) -> None:
        """A policy object is not ownership - this is the predicate that
        stopped a half-created tab being adopted."""
        proxy = PolicyProxy()
        proxy.start()
        self.addCleanup(proxy.stop)
        policies = {TAB: BrowserPolicy("ws://127.0.0.1:1/x", tab_id=TAB, origin="https://a.test")}
        self.assertIsNone(owning_policy(policies, TAB), "a policy with no boundary read as owner")
        policies[TAB].proxy = proxy
        self.assertIsNone(owning_policy(policies, TAB), "a policy with no context read as owner")
        policies[TAB].context_id = "CTX1"
        self.assertIsNotNone(owning_policy(policies, TAB))
        self.assertIsNone(
            owning_policy(policies, TAB, "CTX-OTHER"), "another context read as this one"
        )


class AttachFailureTests(StartupCase):
    """THE DAYBREAK SEQUENCE: boundary, context and target made, attach fails."""

    def test_a_failed_attach_leaves_zero_reusable_state(self) -> None:
        """Requirement 6.1 and the key release regression."""
        self.start_browser(**{"Target.attachToTarget": "the target went away"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        # Everything it made was taken back.
        methods = [method for method, _ in self.browser.sent]
        self.assertIn("Target.createBrowserContext", methods)
        self.assertIn("Target.closeTarget", methods)
        self.assertIn("Target.disposeBrowserContext", methods)
        self.assertTrue(self.boundaries, "no boundary was made, so nothing was proved")
        self.assert_nothing_reusable()

    def test_a_failed_attach_that_times_out_leaves_zero_reusable_state(self) -> None:
        """Requirement 6.2. The browser simply never answers."""
        original = cdp.BROWSER_COMMAND_TIMEOUT_SECONDS
        cdp.BROWSER_COMMAND_TIMEOUT_SECONDS = 1.0
        self.addCleanup(setattr, cdp, "BROWSER_COMMAND_TIMEOUT_SECONDS", original)
        self.start_browser(silent={"Target.attachToTarget"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assert_nothing_reusable()

    def test_the_leaked_boundary_cannot_route_anything(self) -> None:
        """The other half of the Daybreak proof: after the failure the
        boundary is not merely unbound, it is not listening at all."""
        self.start_browser(**{"Target.attachToTarget": "the target went away"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        for made in self.boundaries:
            self.assertFalse(made.running)
            with self.assertRaises(OSError):
                with socket.create_connection(("127.0.0.1", made.port), timeout=5) as sock:
                    sock.sendall(f"CONNECT {OFF_ORIGIN}:443 HTTP/1.1\r\n\r\n".encode("ascii"))
                    if not sock.recv(16):
                        raise OSError("closed")


class OtherFailurePointsTests(StartupCase):
    """Every other step of the transaction, failed on purpose."""

    def test_context_creation_failing_starts_no_target(self) -> None:
        """Requirement 6.4's first half."""
        self.start_browser(**{"Target.createBrowserContext": "no context for you"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assertNotIn("Target.createTarget", [m for m, _ in self.browser.sent])
        self.assert_nothing_reusable()

    def test_a_context_with_no_id_is_refused(self) -> None:
        self.start_browser(**{"Target.createBrowserContext": {}})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assert_nothing_reusable()

    def test_target_creation_failing_after_a_context_undoes_the_context(self) -> None:
        """Requirement 6.4."""
        self.start_browser(**{"Target.createTarget": "no target for you"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assertIn("Target.disposeBrowserContext", [m for m, _ in self.browser.sent])
        self.assert_nothing_reusable()

    def test_an_unreadable_target_id_leaves_nothing(self) -> None:
        """Requirement 6.5's neighbour: the target is not one we can name."""
        self.start_browser(**{"Target.createTarget": {"targetId": "not-a-tab"}})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assert_nothing_reusable()

    def test_policy_arming_failing_after_attach_undoes_everything(self) -> None:
        """Requirement 6.3: the gate cannot be armed on the new tab."""
        self.start_browser(**{"Fetch.enable": "no fetch domain here"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assert_nothing_reusable()

    def test_auto_attach_failing_undoes_everything(self) -> None:
        self.start_browser(**{"Target.setAutoAttach": "no auto attach"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assert_nothing_reusable()


class CleanupThatItselfFailsTests(StartupCase):
    """Requirements 6.6-6.8: the undo is best effort and still fails closed."""

    def test_a_close_that_fails_does_not_stop_the_rest_of_the_undo(self) -> None:
        self.start_browser(
            **{
                "Target.attachToTarget": "the target went away",
                "Target.closeTarget": "cannot close that",
            }
        )
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        # The context was still disposed and the boundary still stopped.
        self.assertIn("Target.disposeBrowserContext", [m for m, _ in self.browser.sent])
        self.assert_nothing_reusable()

    def test_a_dispose_that_fails_does_not_stop_the_rest_of_the_undo(self) -> None:
        self.start_browser(
            **{
                "Target.attachToTarget": "the target went away",
                "Target.disposeBrowserContext": "cannot dispose that",
            }
        )
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assert_nothing_reusable()

    def test_a_boundary_whose_stop_raises_still_leaves_nothing_reusable(self) -> None:
        original = cdp.tab_boundary
        raised: list[str] = []

        def brittle(socket_url, *, origin):
            made = PolicyProxy(resolver=resolver_for())
            made.start()
            made.bind_origin(origin)
            made.authorize(origin)
            real_stop = made.stop

            def angry():
                raised.append("stop")
                real_stop()
                raise OSError("the listener would not close")

            made.stop = angry
            self.boundaries.append(made)
            return made

        cdp.tab_boundary = brittle
        self.addCleanup(setattr, cdp, "tab_boundary", original)
        self.start_browser(**{"Target.attachToTarget": "the target went away"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        self.assertTrue(raised, "the boundary was never asked to stop")
        self.assertEqual("", read_current_tab(self.profile))
        self.assertEqual({}, self.policies)
        with self.assertRaises(BrowserCommandFailed):
            self.run_capability("browser_page_read")


class StaleStateTests(StartupCase):
    """Even a note that somehow survives cannot be turned into a tab."""

    def test_a_current_tab_note_with_no_owner_is_refused_and_cleared(self) -> None:
        """Daybreak's reuse step, forced: the note is there, ownership is not."""
        self.start_browser()
        write_current_tab(self.profile, TAB)
        self.assertEqual(TAB, read_current_tab(self.profile))
        with self.assertRaises(BrowserCommandFailed):
            self.run_capability("browser_navigate", url=f"{self.origin}/next")
        self.assertEqual("", read_current_tab(self.profile), "the stale note survived")

    def test_an_unowned_target_is_never_adopted_by_the_single_candidate_rule(self) -> None:
        """With no note at all, the one-live-page fallback must not adopt it."""
        self.start_browser()
        self.assertEqual("", read_current_tab(self.profile))
        with self.assertRaises(BrowserCommandFailed):
            self.run_capability("browser_page_read")

    def test_mediation_is_never_built_for_a_tab_with_no_boundary(self) -> None:
        """The prohibition stated directly: no production reconstruction."""
        with self.assertRaises(BrowserCommandFailed) as raised:
            cdp.mediated_tab(self.policies, "ws://127.0.0.1:1/x", tab_id=TAB, origin=self.origin)
        self.assertIn("not one JARVIS created and owns", str(raised.exception))
        self.assertEqual({}, self.policies)


class SuccessPathTests(StartupCase):
    """Requirement 7: the ordinary path is unchanged."""

    def test_a_successful_open_publishes_an_owned_tab(self) -> None:
        self.start_browser()
        output = self.open_tab()
        self.assertEqual(TAB, output.payload["tab_id"])
        self.assertEqual(f"{self.origin}/final", output.payload["url"])
        self.assertEqual(TAB, read_current_tab(self.profile))
        policy = owning_policy(self.policies, TAB, "CTX1")
        self.assertIsNotNone(policy)
        self.assertEqual("CTX1", policy.context_id)
        self.assertIs(self.boundaries[0], policy.proxy)
        self.assertTrue(policy.proxy.running)

    def test_the_published_tab_is_bound_to_its_approved_origin(self) -> None:
        self.start_browser()
        self.open_tab()
        authority = self.policies[TAB].proxy.authority()
        self.assertEqual(("127.0.0.1", self.a.server_address[1]), authority)

    def test_the_owned_tab_is_resolvable_by_a_later_operation(self) -> None:
        self.start_browser()
        self.open_tab()
        self.run_capability("browser_page_read")

    def test_the_tab_is_published_only_after_ownership_is_established(self) -> None:
        """Ordering, asserted: the note is written after the gate is armed."""
        self.start_browser()
        self.open_tab()
        methods = [method for method, _ in self.browser.sent]
        self.assertLess(
            methods.index("Target.createBrowserContext"), methods.index("Target.createTarget")
        )
        self.assertLess(methods.index("Target.createTarget"), methods.index("Fetch.enable"))
        self.assertLess(methods.index("Fetch.enable"), methods.index("Page.navigate"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class BoundBeforeExposureTests(StartupCase):
    """V2 M1 Gate 3A. Live Chrome preconnected to public Google hosts through
    a per-tab boundary in the interval between ``Target.createBrowserContext``
    and ``mediated_tab()``, because the boundary was started UNBOUND and only
    bound at mediation. A per-tab boundary now carries its authority before
    the browser can learn its port."""

    @property
    def a_port(self) -> int:
        return int(urlsplit(self.origin).port)

    def _connect(self, proxy: PolicyProxy, target: str) -> bytes:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=15) as sock:
            sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
            return sock.recv(4096)

    def test_the_boundary_is_bound_before_the_browser_receives_its_port(self) -> None:
        seen: list = []
        self.start_browser()

        def at_context_creation():
            params = [p for m, p in self.browser.sent if m == "Target.createBrowserContext"][-1]
            seen.append(
                (
                    params.get("proxyServer"),
                    [(b.port, b.authority()) for b in self.boundaries],
                    dict(self.policies),
                    read_current_tab(self.profile),
                )
            )

        self.browser.triggers["Target.createBrowserContext"] = at_context_creation
        self.open_tab()
        (proxy_server, boundaries, policies_then, note_then), = seen
        (port, authority), = boundaries
        self.assertEqual(f"http://127.0.0.1:{port}", proxy_server)
        self.assertEqual(("127.0.0.1", self.a_port), authority, "the browser was handed an unbound boundary")
        self.assertEqual({}, policies_then, "mediation existed before the context did")
        self.assertEqual("", note_then, "current-tab was published before the tab existed")

    def test_a_connection_before_mediation_is_already_constrained(self) -> None:
        """The Gate 3 observation, as a regression: what arrives at the tab
        proxy while the browser has its port but the tab is not yet mediated
        may only go to the approved origin."""
        answers: list = []

        def loopback_bound(socket_url, *, origin):
            # The fixture's resolver answers every name with a public address;
            # this tab's approved origin is loopback, so resolve it as such.
            made = PolicyProxy(resolver=resolver_for("127.0.0.1"))
            made.start()
            made.bind_origin(origin)
            made.authorize(origin)
            self.boundaries.append(made)
            return made

        cdp.tab_boundary = loopback_bound
        self.start_browser()

        def preconnect_like_chrome():
            boundary = self.boundaries[-1]
            self.assertEqual({}, self.policies, "not before mediation")
            answers.append(("public", self._connect(boundary, f"{OFF_ORIGIN}:443")))
            answers.append(("private", self._connect(boundary, "192.168.1.10:443")))
            answers.append(("approved", self._connect(boundary, f"127.0.0.1:{self.a_port}")))

        self.browser.triggers["Target.createBrowserContext"] = preconnect_like_chrome
        self.open_tab()
        outcome = dict(answers)
        self.assertIn(b"403", outcome["public"])
        self.assertIn(b"403", outcome["private"])
        self.assertIn(b"200", outcome["approved"])
        boundary = self.boundaries[-1]
        self.assertEqual([("127.0.0.1", self.a_port)], boundary.connected, "something else got out")
        self.assertEqual(2, len(boundary.refused))

    def test_binding_failure_creates_no_context_and_stops_the_boundary(self) -> None:
        """Through the REAL tab_boundary: a proxy that cannot bind is stopped
        and the browser is never asked for a context."""
        made: list = []
        real_proxy = cdp.PolicyProxy

        class Unbindable(real_proxy):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                made.append(self)

            def bind_origin(self, origin):
                raise ValueError("cannot bind")

        cdp.tab_boundary = self.real_tab_boundary
        cdp.PolicyProxy = Unbindable
        self.addCleanup(setattr, cdp, "PolicyProxy", real_proxy)
        self.start_browser()
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.open_tab()
        self.assertIn("could not be bound", str(raised.exception))
        self.assertEqual(1, len(made))
        self.assertFalse(made[0].running, "an unbindable boundary was left listening")
        self.assertNotIn("Target.createBrowserContext", [m for m, _ in self.browser.sent])
        self.assert_nothing_reusable()

    def test_an_unbound_boundary_is_never_handed_to_the_browser(self) -> None:
        """Even if a boundary somehow arrives unbound, the transaction refuses
        to expose it: no context, boundary stopped, nothing reusable."""

        def unbound(socket_url, *, origin):
            proxy = PolicyProxy(resolver=resolver_for())
            proxy.start()
            self.boundaries.append(proxy)
            return proxy

        cdp.tab_boundary = unbound
        self.start_browser()
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.open_tab()
        self.assertIn("carries no authority", str(raised.exception))
        self.assertNotIn("Target.createBrowserContext", [m for m, _ in self.browser.sent])
        self.assert_nothing_reusable()

    def test_a_failure_after_binding_still_stops_the_bound_boundary(self) -> None:
        self.start_browser(**{"Target.attachToTarget": "the target went away"})
        with self.assertRaises(BrowserCommandFailed):
            self.open_tab()
        (boundary,) = self.boundaries
        self.assertEqual(("127.0.0.1", self.a_port), boundary.authority())
        self.assertFalse(boundary.running)
        self.assert_nothing_reusable()

    def test_the_real_boundary_is_born_bound(self) -> None:
        boundary = self.real_tab_boundary("ws://127.0.0.1:1/x", origin="https://approved.test:8443")
        self.addCleanup(boundary.stop)
        self.assertEqual(("approved.test", 8443), boundary.authority())
        self.assertTrue(boundary.running)

    def test_the_profile_level_proxy_is_still_unbound(self) -> None:
        """The browser's own boundary serves every tab's vendor traffic and is
        NOT a tab boundary: it keeps its ``None`` authority (Gate 3A leaves
        it exactly as it was)."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proxy = cdp.browser_policy_proxy(port)
        self.addCleanup(cdp.release_owned_browsers, port)
        self.assertIsNone(proxy.authority())
        self.assertTrue(proxy.running)
