"""DB-002: an approved origin is not an approved network.

THE GAP THIS PINS. The destination gate used to pause only ``resourceType:
"Document"``, so it decided where JARVIS asked the tab to GO and nothing else.
Once the approved page was loaded, the page itself chose what to request next
- an image, a script, a stylesheet, an XHR, a form post, a frame - and every
one of those went out unjudged. That reaches any origin on the internet, and,
because the browser sits inside the operator's network, this machine's
loopback services and the intranet behind it as well.

HOW THIS IS PROVED. Two disposable local servers. Origin A is approved and
serves the page; origin B is not approved and RECORDS whatever reaches it. The
fake browser only performs an HTTP request when the client continues the
paused request, exactly as a real browser refusing a paused request never
opens the connection - so "B received nothing" is B's own account of the wire,
not our transcript of what we intended.

The private-address rules are unit-tested against the predicate rather than by
contacting anything: the task forbids touching real LAN services, and a
literal-address classification has nothing to learn from a socket.

WHY ``policies=None`` HERE. These cases pin the per-operation navigation gate
on its own, so they deliberately run without the standing policy DB-002B
added. What that policy does between and around operations has its own file,
``tests/test_browser_lifecycle``.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from jarvis.browser import (
    BrowserResource,
    BrowserResourceKind,
    browser_operation_plan,
    build_browser_request_frame,
    private_host_refusal,
    request_destination_refusal,
)
from jarvis.browser_cdp import run_operation_plan
from tests.helpers import safe_cleanup
from tests.test_browser_minimum import (
    FRAME,
    TAB,
    FakeBrowser,
    _GatedNavigator,
    _Origin,
    page_answers,
)

#: The renderer request classes a page can issue on its own, as Chromium names
#: them. Every one of these used to escape the gate entirely.
SUBRESOURCE_KINDS = (
    ("Image", "/pixel.png"),
    ("Script", "/tracker.js"),
    ("Stylesheet", "/theme.css"),
    ("XHR", "/api/read"),
    ("Fetch", "/api/collect"),
    ("Font", "/text.woff2"),
)


class _PageNavigator(_GatedNavigator):
    """A browser whose loaded page then asks for things of its own.

    The subresources are announced AFTER the document commits, which is where
    a page's requests really happen and where the old gate had already stopped
    listening.
    """

    def __init__(self, subresources: tuple[tuple[str, str], ...], **kwargs) -> None:
        super().__init__(**kwargs)
        self._subresources = subresources
        self._announced = False
        #: Requests belonging to the PAGE rather than to the navigation. They
        #: are fetched when continued, exactly like the document, but they
        #: never commit a loader - a stylesheet is not a navigation.
        self.subresource_ids: set[str] = set()

    def _pause_typed(self, url: str, resource_type: str, frame: str) -> dict:
        event = self._pause(url)
        event["params"]["resourceType"] = resource_type
        event["params"]["frameId"] = frame
        self.subresource_ids.add(event["params"]["requestId"])
        return event

    def follow(self, request_id: str) -> list[dict]:
        if request_id in self.subresource_ids:
            url = self.paused.pop(request_id, "")
            self.fetched.append(url)
            location = self._request(url)
            if not location:
                return []
            # A subresource that redirects: the hop is its own request, and
            # the gate has to judge it on its own.
            return [self._pause_typed(location, "Script", FRAME)]
        outbound = super().follow(request_id)
        if self._announced or not any(
            one.get("method") == "Page.frameNavigated" for one in outbound
        ):
            return outbound
        # The document committed. Now the page asks for what it wants.
        self._announced = True
        for resource_type, url in self._subresources:
            frame = "F-CHILD" if resource_type == "Document" else FRAME
            outbound.append(self._pause_typed(url, resource_type, frame))
        return outbound


class BrowserEgressCase(unittest.TestCase):
    """Origin A approved and serving; origin B unapproved and watching."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.profile = str(Path(self.temp.name) / "browser-profile")
        self.b = _Origin({})
        self.addCleanup(self.b.stop)
        self.a = _Origin({"/offsite": lambda: f"{self.b.origin}/landing"})
        self.addCleanup(self.a.stop)

    def drive(self, subresources: tuple[tuple[str, str], ...], *, path: str = "/final"):
        origin = self.a.origin
        frame = build_browser_request_frame(
            capability="browser_navigate",
            destination=f"browser/{origin}",
            resource=BrowserResource(
                kind=BrowserResourceKind.NAVIGATION, origin=origin, url=f"{origin}{path}"
            ),
        )
        self.navigator = _PageNavigator(subresources)
        answers = page_answers(
            **{
                "Target.getTargets": {
                    "targetInfos": [
                        {"type": "page", "targetId": TAB, "url": f"{origin}/", "title": "A"}
                    ]
                },
                "Page.getNavigationHistory": lambda params: {
                    "currentIndex": 0,
                    "entries": [{"url": self.navigator.landed, "title": "A"}],
                },
            }
        )
        browser = FakeBrowser(answers, navigator=self.navigator)
        self.addCleanup(browser.stop)
        self.browser = browser
        return run_operation_plan(
            browser_operation_plan(frame),
            frame=frame,
            profile_dir=self.profile,
            attach=lambda **kwargs: browser.socket_url,
            sleep=lambda seconds: None,
            policies=None,
        )


class SubresourceEgressTests(BrowserEgressCase):
    """The page's own requests are judged, one class at a time."""

    def test_same_origin_subresources_still_load(self) -> None:
        """Usability: an approved page must keep working."""
        wanted = tuple((kind, f"{self.a.origin}{path}") for kind, path in SUBRESOURCE_KINDS)
        self.drive(wanted)
        for _, url in wanted:
            with self.subTest(url=url):
                self.assertIn(url, self.navigator.fetched)
        # Every one of them really reached A, which is what authority means.
        for _, path in SUBRESOURCE_KINDS:
            self.assertIn(path, self.a.received)
        self.assertEqual([], self.navigator.failed)

    def test_every_cross_origin_subresource_class_is_blocked(self) -> None:
        """DB-002 proper: img, script, css, XHR, fetch and font to origin B."""
        wanted = tuple((kind, f"{self.b.origin}{path}") for kind, path in SUBRESOURCE_KINDS)
        self.drive(wanted)
        # B's own account of its wire: nothing arrived.
        self.assertEqual([], self.b.received)
        self.assertEqual(
            [], [url for url in self.navigator.fetched if self.b.origin in url]
        )
        # And each was refused rather than quietly dropped.
        for _, url in wanted:
            with self.subTest(url=url):
                self.assertIn(url, self.navigator.failed)

    def test_a_cross_origin_form_post_is_blocked(self) -> None:
        self.drive((("Document", f"{self.b.origin}/collect?stolen=1"),))
        self.assertEqual([], self.b.received)
        self.assertIn(f"{self.b.origin}/collect?stolen=1", self.navigator.failed)

    def test_a_cross_origin_iframe_is_blocked_without_failing_the_navigation(self) -> None:
        """A refused subresource is the gate working, not a failed navigation."""
        landed = self.drive((("Document", f"{self.b.origin}/frame"),))
        self.assertEqual([], self.b.received)
        self.assertIn(f"{self.b.origin}/frame", self.navigator.failed)
        self.assertEqual(f"{self.a.origin}/final", landed.payload["url"])

    def test_a_mixed_page_keeps_what_is_approved_and_loses_what_is_not(self) -> None:
        self.drive(
            (
                ("Script", f"{self.a.origin}/own.js"),
                ("Script", f"{self.b.origin}/evil.js"),
                ("Image", f"{self.a.origin}/own.png"),
                ("XHR", f"{self.b.origin}/exfil"),
            )
        )
        self.assertEqual(["/own.js", "/own.png"], [p for p in self.a.received if p != "/final"])
        self.assertEqual([], self.b.received)


class SubresourceRedirectTests(BrowserEgressCase):
    """A subresource redirect is its own request and gets its own judgement."""

    def test_a_subresource_that_redirects_offsite_never_follows(self) -> None:
        self.drive((("Script", f"{self.a.origin}/offsite"),))
        # A served the redirect, as authorized.
        self.assertIn("/offsite", self.a.received)
        # The hop it pointed at was judged on its own and refused.
        self.assertEqual([], self.b.received)
        self.assertIn(f"{self.b.origin}/landing", self.navigator.failed)


class PersistentEnforcementTests(BrowserEgressCase):
    """The gate outlives the navigation it started with."""

    def test_requests_issued_after_the_document_commits_are_still_judged(self) -> None:
        self.drive((("XHR", f"{self.b.origin}/after-load"),))
        self.assertEqual([], self.b.received)
        self.assertIn(f"{self.b.origin}/after-load", self.navigator.failed)

    def test_the_gate_is_disarmed_only_after_the_page_stops_asking(self) -> None:
        self.drive(
            (
                ("Script", f"{self.a.origin}/own.js"),
                ("XHR", f"{self.b.origin}/exfil"),
            )
        )
        methods = [method for method, _ in self.browser.sent]
        self.assertEqual(1, methods.count("Fetch.disable"))
        # Every disposition happened while the gate was still armed.
        disable_at = methods.index("Fetch.disable")
        for verb in ("Fetch.continueRequest", "Fetch.failRequest"):
            with self.subTest(verb=verb):
                self.assertTrue(
                    all(at < disable_at for at, name in enumerate(methods) if name == verb),
                    f"{verb} was sent after the gate was disarmed",
                )

    def test_nothing_is_left_paused_when_the_gate_is_disarmed(self) -> None:
        """Disabling Fetch RESUMES whatever is still paused, so nothing may be."""
        self.drive(
            (
                ("Image", f"{self.b.origin}/one.png"),
                ("Image", f"{self.a.origin}/two.png"),
            )
        )
        self.assertEqual({}, self.navigator.paused)
        self.assertEqual([], self.navigator.released)


class CustodyTests(BrowserEgressCase):
    """Losing custody of a paused request fails closed, never open."""

    def test_a_refusal_the_browser_will_not_acknowledge_destroys_the_tab(self) -> None:
        origin = self.a.origin
        frame = build_browser_request_frame(
            capability="browser_navigate",
            destination=f"browser/{origin}",
            resource=BrowserResource(
                kind=BrowserResourceKind.NAVIGATION, origin=origin, url=f"{origin}/final"
            ),
        )
        # r2 is the subresource; its refusal is refused at the protocol level.
        self.navigator = _PageNavigator(
            (("XHR", f"{self.b.origin}/exfil"),), refuse_fail_for=("r2",)
        )
        answers = page_answers(
            **{
                "Target.getTargets": {
                    "targetInfos": [
                        {"type": "page", "targetId": TAB, "url": f"{origin}/", "title": "A"}
                    ]
                },
                "Page.getNavigationHistory": lambda params: {
                    "currentIndex": 0,
                    "entries": [{"url": self.navigator.landed, "title": "A"}],
                },
            }
        )
        browser = FakeBrowser(answers, navigator=self.navigator)
        self.addCleanup(browser.stop)
        self.browser = browser
        with self.assertRaises(Exception) as raised:
            run_operation_plan(
                browser_operation_plan(frame),
                frame=frame,
                profile_dir=self.profile,
                attach=lambda **kwargs: browser.socket_url,
                sleep=lambda seconds: None,
                policies=None,
            )
        # Never released onto the wire: the tab was destroyed instead.
        self.assertEqual([], self.b.received)
        self.assertEqual([], self.navigator.released)
        self.assertNotIn("Fetch.disable", [m for m, _ in self.browser.sent])
        self.assertIsNotNone(raised.exception)


class PrivateDestinationTests(unittest.TestCase):
    """Loopback, private, link-local: classified by address, not by spelling.

    Unit-level on purpose - the task forbids contacting real LAN services, and
    an address classification has nothing to learn from a socket.
    """

    APPROVED = "https://example.com"

    def refusal(self, url: str, origin: str = APPROVED) -> str | None:
        return request_destination_refusal(url, origin)

    def test_loopback_is_blocked(self) -> None:
        for host in ("127.0.0.1", "127.1", "localhost", "[::1]", "[::ffff:127.0.0.1]"):
            with self.subTest(host=host):
                self.assertIsNotNone(private_host_refusal(host.strip("[]")))

    def test_rfc1918_is_blocked(self) -> None:
        for host in ("10.0.0.5", "172.16.3.4", "172.31.255.254", "192.168.1.1"):
            with self.subTest(host=host):
                self.assertIsNotNone(private_host_refusal(host))

    def test_link_local_is_blocked(self) -> None:
        for host in ("169.254.169.254", "fe80::1"):
            with self.subTest(host=host):
                self.assertIsNotNone(private_host_refusal(host))

    def test_ipv6_unique_local_and_unspecified_are_blocked(self) -> None:
        for host in ("fc00::1", "fd12:3456::1", "::", "0.0.0.0"):
            with self.subTest(host=host):
                self.assertIsNotNone(private_host_refusal(host))

    def test_reserved_local_names_are_blocked(self) -> None:
        for host in ("localhost", "printer.local", "wiki.internal", "db.home.arpa"):
            with self.subTest(host=host):
                self.assertIsNotNone(private_host_refusal(host))

    def test_an_ordinary_public_host_is_not_private(self) -> None:
        for host in ("example.com", "8.8.8.8", "2606:4700::1111"):
            with self.subTest(host=host):
                self.assertIsNone(private_host_refusal(host))

    def test_a_private_destination_is_refused_for_an_approved_public_origin(self) -> None:
        for url in (
            "http://127.0.0.1:8080/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.1/",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(self.refusal(url))

    def test_an_explicitly_approved_loopback_origin_is_allowed(self) -> None:
        """An operator who approves a local service gets that service."""
        origin = "http://127.0.0.1:8080"
        self.assertIsNone(self.refusal(f"{origin}/admin", origin))
        # And nothing else local comes with it.
        self.assertIsNotNone(self.refusal("http://127.0.0.1:9999/", origin))
        self.assertIsNotNone(self.refusal("http://192.168.1.1/", origin))


class OriginScopeTests(unittest.TestCase):
    """The grant is origin-level and stays that way - no broadening."""

    ORIGIN = "https://example.com"

    def test_any_path_on_the_approved_origin_is_allowed(self) -> None:
        for url in (f"{self.ORIGIN}/", f"{self.ORIGIN}/a/b?c=d"):
            with self.subTest(url=url):
                self.assertIsNone(request_destination_refusal(url, self.ORIGIN))

    def test_a_subdomain_a_sibling_and_another_port_are_all_refused(self) -> None:
        for url in (
            "https://cdn.example.com/app.js",
            "https://example.com.evil.test/",
            "https://example.com:8443/",
            "http://example.com/",
            "https://evil.test/",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(request_destination_refusal(url, self.ORIGIN))

    def test_a_second_origin_works_when_it_is_the_one_granted(self) -> None:
        self.assertIsNone(
            request_destination_refusal("https://cdn.example.com/app.js", "https://cdn.example.com")
        )

    def test_a_non_web_scheme_is_refused(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/html,x"):
            with self.subTest(url=url):
                self.assertIsNotNone(request_destination_refusal(url, self.ORIGIN))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
