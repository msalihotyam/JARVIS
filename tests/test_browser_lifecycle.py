"""DB-002B: an approved page is untrusted code that keeps running.

THE GAP THIS PINS. The first DB-002 fix judged every renderer request, but
only while an operation was in progress: ``guarded_navigate`` armed the gate,
serviced the page's load, and its ``finally`` sent ``Fetch.disable`` and closed
the connection. The tab stayed open and the page stayed alive, so a
``setTimeout`` that fired a second later reached anywhere it liked - and
``browser_click``, ``browser_type``, ``browser_wait`` and ``browser_page_read``
never armed a gate at all, so a click handler could do the same thing during
an approved click.

That was measured against real Microsoft Edge before any of this was written,
and so were the two candidate fixes:

  * nothing armed                                 -> the delayed fetch arrived
  * ``Fetch`` armed, then the client disconnects  -> the delayed fetch arrived
  * ``Network.emulateNetworkConditions(offline)``,
    then the client disconnects                   -> the delayed fetch arrived
  * a SECOND, persistent client keeps ``Fetch``
    armed while the operation's client leaves     -> refused, B got nothing

So no CDP-imposed network policy survives the client's disconnect, which rules
out fencing the page offline between operations, and a standing policy that
never lets go is what is left. That is ``jarvis.browser_cdp.BrowserPolicy``,
and this file is what holds it to it.

HOW THIS IS PROVED, here. Two disposable local servers, as in
``test_browser_egress``: origin A is approved and serves, origin B is not
approved and RECORDS. The fake browser performs an HTTP request only when the
client continues the paused request, so "B received nothing" is B's own
account of the wire. The requests are announced to the STANDING POLICY's
connection, unprompted, which is what a page acting on its own looks like from
the protocol: nobody asked for it, and the only client still listening is the
one that never went away.

Nothing here contacts anything but 127.0.0.1.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from jarvis.browser import (
    BROWSER_CAPABILITY_METHODS,
    BROWSER_POLICY_METHODS,
    BrowserResource,
    BrowserResourceKind,
    browser_operation_plan,
    browser_page_digest,
    browser_page_rendering,
    build_browser_request_frame,
)
from jarvis.browser_cdp import (
    BROWSER_DEBUG_PORT,
    BrowserCommandFailed,
    BrowserPolicy,
    browser_arguments,
    owning_policy,
    read_current_tab,
    release_browser_policies,
    run_operation_plan,
    write_current_tab,
)
from jarvis.policy_proxy import PolicyProxy
from tests.helpers import safe_cleanup
from tests.test_browser_minimum import (
    FRAME,
    TAB,
    FakeBrowser,
    _GatedNavigator,
    _Origin,
    _AX_NODES,
    page_answers,
)

#: How long a test waits for the standing policy's own loop to reach a request
#: the browser announced. A bound on the WAIT, never on the enforcement: the
#: request stays paused until the loop judges it, however long that takes.
SETTLE_TIMEOUT = 15.0


class _FetchEnable:
    """Scripted ``Fetch.enable``: fine until the test says otherwise.

    Stands in for a target that has no Fetch domain at all - a dedicated
    worker, as Edge 153 answers for one.
    """

    def __init__(self) -> None:
        self.refuse = False

    def __call__(self, params):
        return "'Fetch.enable' wasn't found" if self.refuse else {}


class _LivePageNavigator(_GatedNavigator):
    """A browser whose page goes on asking for things after it has loaded.

    Unlike the navigation chain, these requests are not answers to anything
    the client did. They are pushed at whoever is still intercepting, which is
    the standing policy, and they are fetched only if that policy continues
    them.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.subresource_ids: set[str] = set()

    def spontaneous(self, url: str, resource_type: str = "XHR") -> dict:
        """The protocol frame for "the page just asked for this"."""
        event = self._pause(url)
        event["params"]["resourceType"] = resource_type
        event["params"]["frameId"] = FRAME
        self.subresource_ids.add(event["params"]["requestId"])
        return event

    def follow(self, request_id: str) -> list[dict]:
        if request_id not in self.subresource_ids:
            return super().follow(request_id)
        url = self.paused.pop(request_id, "")
        # RECORDED AFTER THE WIRE, not before: a test that waits for this and
        # then asks the server what it got would otherwise be racing the GET.
        location = self._request(url)
        self.fetched.append(url)
        if not location:
            return []
        # A redirect is its own request and gets judged on its own, standing
        # policy or not.
        return [self.spontaneous(location, "Script")]


class LifecycleCase(unittest.TestCase):
    """One approved origin A, one unapproved origin B, one mediated tab."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.profile = str(Path(self.temp.name) / "browser-profile")
        self.b = _Origin({})
        self.addCleanup(self.b.stop)
        self.a = _Origin({"/offsite": lambda: f"{self.b.origin}/landing"})
        self.addCleanup(self.a.stop)
        #: This test's own registry of standing policies. Released on the way
        #: out, which is what destroys the mediated tabs.
        self.policies: dict[str, BrowserPolicy] = {}
        self.addCleanup(release_browser_policies, self.policies)
        self.navigator = _LivePageNavigator()
        self.browser: FakeBrowser | None = None

    # -- the browser under test -------------------------------------------

    def start_browser(self, *, triggers: dict | None = None, **answers) -> FakeBrowser:
        origin = self.a.origin
        scripted = page_answers(
            **{
                "Target.getTargets": {
                    "targetInfos": [
                        {"type": "page", "targetId": TAB, "url": f"{origin}/", "title": "A"}
                    ]
                },
                "Target.createTarget": {"targetId": TAB},
                "Page.getNavigationHistory": lambda params: {
                    "currentIndex": 0,
                    "entries": [{"url": self.navigator.landed or f"{origin}/", "title": "A"}],
                },
                **answers,
            }
        )
        browser = FakeBrowser(scripted, navigator=self.navigator, triggers=triggers)
        self.addCleanup(browser.stop)
        self.browser = browser
        return browser

    def frame_for(self, capability: str, **fields):
        origin = self.a.origin
        kind = {
            "browser_navigate": BrowserResourceKind.NAVIGATION,
            "browser_tab_open": BrowserResourceKind.NEW_TAB,
            "browser_page_read": BrowserResourceKind.PAGE,
            "browser_wait": BrowserResourceKind.DELAY,
            "browser_click": BrowserResourceKind.CLICK_TARGET,
            "browser_type": BrowserResourceKind.TEXT_TARGET,
            "browser_tab_close": BrowserResourceKind.TAB_CLOSURE,
        }[capability]
        return build_browser_request_frame(
            capability=capability,
            destination=f"browser/{origin}",
            resource=BrowserResource(kind=kind, origin=origin, **fields),
        )

    def run_capability(self, capability: str, *, sleep=None, **fields):
        frame = self.frame_for(capability, **fields)
        browser = self.browser or self.start_browser()
        return run_operation_plan(
            browser_operation_plan(frame),
            frame=frame,
            profile_dir=self.profile,
            attach=lambda **kwargs: browser.socket_url,
            sleep=sleep or (lambda seconds: None),
            policies=self.policies,
        )

    def element(self, ref: str = "r4") -> dict:
        """A ref and the digest of the reading it came from, for THIS page.

        Computed rather than constant: the approval binds the element to the
        exact page state it was read against, and that state names a loopback
        port this run chose.
        """
        rendering = browser_page_rendering(_AX_NODES, url=self.navigator.landed, title="A")
        return {"ref": ref, "page_digest": browser_page_digest(rendering)}

    def navigate(self, path: str = "/final"):
        """The operation that brings the page online under mediation.

        ``browser_tab_open``, because that is how a JARVIS tab comes into
        being: with its own browser context, its own boundary and a policy
        holding both. A tab that was never opened that way is not one any
        capability will act on, so navigating a pre-existing target is not a
        state production can reach.
        """
        return self.run_capability("browser_tab_open", url=f"{self.a.origin}{path}")

    # -- what the page does on its own ------------------------------------

    def page_requests(self, url: str, resource_type: str = "XHR") -> None:
        """The page asks for ``url`` unprompted, and the policy hears it."""
        assert self.browser is not None
        self.browser.announce([self.navigator.spontaneous(url, resource_type)])

    def settled(self, url: str) -> None:
        """Wait until the standing policy has DISPOSED of ``url``."""
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while time.monotonic() < deadline:
            if url in self.navigator.fetched or url in self.navigator.failed:
                return
            time.sleep(0.02)
        self.fail(f"the standing policy never disposed of {url}")

    def assert_blocked(self, url: str) -> None:
        self.settled(url)
        self.assertIn(url, self.navigator.failed)
        self.assertNotIn(url, self.navigator.fetched)
        self.assertEqual([], self.b.received, "origin B received a request")
        self.assertEqual([], self.navigator.released)


# --------------------------------------------------------------------------
# 1-2. The two escapes Daybreak named, reproduced and then closed
# --------------------------------------------------------------------------


class DelayedRequestTests(LifecycleCase):
    """A timer that fires after the operation is over."""

    def test_a_delayed_fetch_after_the_settle_window_is_refused(self) -> None:
        """THE BLOCKER. setTimeout(() => fetch(B), later) - after everything.

        The navigation has returned, the navigation gate has been disarmed and
        its connection is closed. In the old shape nothing was left watching
        and this reached B.
        """
        self.navigate()
        self.assertNotIn(self.navigator.landed, ("",))
        self.page_requests(f"{self.b.origin}/delayed", "Fetch")
        self.assert_blocked(f"{self.b.origin}/delayed")

    def test_every_delayed_resource_class_is_refused(self) -> None:
        self.navigate()
        for kind, path in (
            ("Image", "/beacon.png"),
            ("Script", "/late.js"),
            ("XHR", "/poll"),
            ("Ping", "/beacon"),
            ("Document", "/frame"),
        ):
            with self.subTest(kind=kind):
                url = f"{self.b.origin}{path}"
                self.page_requests(url, kind)
                self.assert_blocked(url)

    def test_a_delayed_same_origin_resource_still_loads(self) -> None:
        """Usability: mediation is not a blanket refusal."""
        self.navigate()
        url = f"{self.a.origin}/late.js"
        self.page_requests(url, "Script")
        self.settled(url)
        self.assertIn(url, self.navigator.fetched)
        self.assertIn("/late.js", self.a.received)

    def test_a_delayed_request_to_a_private_address_is_refused(self) -> None:
        self.navigate()
        url = "http://169.254.169.254/latest/meta-data/"
        self.page_requests(url, "Fetch")
        self.settled(url)
        self.assertIn(url, self.navigator.failed)

    def test_a_delayed_redirect_hop_is_judged_on_its_own(self) -> None:
        self.navigate()
        url = f"{self.a.origin}/offsite"
        self.page_requests(url, "Script")
        self.settled(url)
        # A served the redirect, as authorized; the hop it named did not go.
        self.assertIn("/offsite", self.a.received)
        self.settled(f"{self.b.origin}/landing")
        self.assertEqual([], self.b.received)


class InteractionTests(LifecycleCase):
    """Click and type are when a page is most likely to reach out."""

    def test_a_click_handler_that_fetches_an_unapproved_origin_is_refused(self) -> None:
        """THE SECOND BLOCKER. browser_click never armed a gate of its own."""
        url = f"{self.b.origin}/clicked"
        self.start_browser(
            triggers={"Input.dispatchMouseEvent": lambda: self.page_requests(url, "Fetch")}
        )
        self.navigate()
        self.run_capability("browser_click", **self.element())
        self.assert_blocked(url)

    def test_a_click_triggered_form_submission_is_refused(self) -> None:
        url = f"{self.b.origin}/submit?card=4111"
        self.start_browser(
            triggers={"Input.dispatchMouseEvent": lambda: self.page_requests(url, "Document")}
        )
        self.navigate()
        self.run_capability("browser_click", **self.element())
        self.assert_blocked(url)

    def test_typing_that_triggers_a_request_is_refused(self) -> None:
        url = f"{self.b.origin}/autocomplete?q=secret"
        self.start_browser(
            triggers={"Input.insertText": lambda: self.page_requests(url, "XHR")}
        )
        self.navigate()
        self.run_capability("browser_type", text="hello", **self.element("r2"))
        self.assert_blocked(url)

    def test_a_click_handler_asking_its_own_origin_still_works(self) -> None:
        url = f"{self.a.origin}/click-api"
        self.start_browser(
            triggers={"Input.dispatchMouseEvent": lambda: self.page_requests(url, "XHR")}
        )
        self.navigate()
        self.run_capability("browser_click", **self.element())
        self.settled(url)
        self.assertIn("/click-api", self.a.received)


class QuietOperationTests(LifecycleCase):
    """Waiting and reading are operations too, and pages act during them."""

    def test_waiting_is_spent_inside_mediation(self) -> None:
        """The sleep IS the window, so the request is made during the sleep."""
        url = f"{self.b.origin}/during-wait"
        self.start_browser()
        self.navigate()
        self.run_capability(
            "browser_wait", seconds=1, sleep=lambda seconds: self.page_requests(url, "Fetch")
        )
        self.assert_blocked(url)

    def test_reading_a_page_does_not_create_an_ungated_period(self) -> None:
        url = f"{self.b.origin}/during-read"
        self.start_browser(
            triggers={"Accessibility.getFullAXTree": lambda: self.page_requests(url, "XHR")}
        )
        self.navigate()
        self.run_capability("browser_page_read")
        self.assert_blocked(url)

    def test_between_two_operations_the_policy_is_still_in_force(self) -> None:
        """The interval the old shape had no answer for at all."""
        self.start_browser()
        self.navigate()
        self.run_capability("browser_page_read")
        url = f"{self.b.origin}/between"
        self.page_requests(url, "Fetch")
        self.assert_blocked(url)
        # And the tab is still usable afterwards.
        self.run_capability("browser_page_read")


# --------------------------------------------------------------------------
# 3. The ordering: armed before online, released only by destroying the page
# --------------------------------------------------------------------------


class OrderingTests(LifecycleCase):
    """No online-and-ungated interval, at either end."""

    def methods(self) -> list[str]:
        assert self.browser is not None
        return [method for method, _ in self.browser.sent]

    def test_the_tab_is_mediated_before_it_is_ever_navigated(self) -> None:
        """browser_tab_open: blank, attached, GATED, and only then online."""
        self.start_browser()
        self.run_capability("browser_tab_open", url=f"{self.a.origin}/final")
        methods = self.methods()
        context = methods.index("Target.createBrowserContext")
        created = methods.index("Target.createTarget")
        armed = methods.index("Target.setAutoAttach")
        navigated = methods.index("Page.navigate")
        self.assertLess(context, created, "the tab existed before it had a way out")
        self.assertLess(created, armed, "the tab existed before it was mediated")
        self.assertLess(armed, navigated, "the page was sent somewhere before it was gated")
        # And the tab really was created INSIDE that context.
        target = next(
            params for method, params in self.browser.sent if method == "Target.createTarget"
        )
        self.assertEqual("CTX1", target.get("browserContextId"))
        # Created EMPTY: the only URL given to createTarget is about:blank.
        target = next(
            params for method, params in self.browser.sent if method == "Target.createTarget"
        )
        self.assertEqual("about:blank", target["url"])

    def test_the_standing_policy_cannot_disable_the_gate_at_all(self) -> None:
        """The ordering is a method set, not a sequence to remember."""
        self.assertNotIn("Fetch.disable", BROWSER_POLICY_METHODS)
        self.assertNotIn("Page.navigate", BROWSER_POLICY_METHODS)
        self.assertNotIn("Runtime.evaluate", BROWSER_POLICY_METHODS)
        self.navigate()
        policy = self.policies[TAB]
        with self.assertRaises(BrowserCommandFailed):
            policy._connection.send("Fetch.disable", session_id="s1")

    def test_the_operations_own_disarm_does_not_disarm_the_policy(self) -> None:
        """Two clients intercept; one leaving is not the other leaving."""
        self.navigate()
        self.assertIn("Fetch.disable", self.methods())
        self.assertTrue(self.policies[TAB].alive)
        url = f"{self.b.origin}/after-disable"
        self.page_requests(url, "Fetch")
        self.assert_blocked(url)

    def test_closing_the_tab_ends_the_policy_and_leaves_nothing_behind(self) -> None:
        self.start_browser()
        self.navigate()
        policy = self.policies[TAB]
        write_current_tab(self.profile, TAB)
        self.run_capability("browser_tab_close", tab_id=TAB)
        self.assertEqual({}, self.policies)
        self.assertFalse(policy.alive)
        self.assertIsNone(policy._connection)
        # The page was destroyed BEFORE its mediation was dropped.
        methods = self.methods()
        self.assertIn("Target.closeTarget", methods)

    def test_releasing_every_policy_destroys_every_mediated_tab(self) -> None:
        """Orderly shutdown: a mediated page does not outlive its mediation."""
        self.start_browser()
        self.navigate()
        policy = self.policies[TAB]
        release_browser_policies(self.policies)
        self.assertEqual({}, self.policies)
        self.assertFalse(policy.alive)
        self.assertIn("Target.closeTarget", self.methods())


# --------------------------------------------------------------------------
# 4. Workers, and failing closed
# --------------------------------------------------------------------------


class WorkerTests(LifecycleCase):
    """A page's other execution targets are gated before they may run."""

    def attached(self, session: str) -> dict:
        return {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": session,
                "waitingForDebugger": True,
                "targetInfo": {"targetId": "W1", "type": "service_worker", "url": f"{self.a.origin}/sw.js"},
            },
        }

    def test_an_attached_worker_has_a_gate_armed_on_its_own_session(self) -> None:
        """A child target is adopted and gated on its own session.

        It is NOT held at startup any more. Holding it is the textbook order,
        but on Edge 153 a held child does not answer ``Fetch.enable`` and the
        policy loop blocked on that command - measured, and it stopped the
        policy judging anything at all on any page that creates a worker. What
        covers the moment before the gate is armed is the tab's own boundary,
        which no child can reach past.
        """
        self.start_browser()
        self.navigate()
        self.browser.sent.clear()
        self.browser.announce([self.attached("w-session")])
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while time.monotonic() < deadline:
            if "Fetch.enable" in [method for method, _ in self.browser.sent]:
                break
            time.sleep(0.02)
        armed = [
            params
            for method, params in self.browser.sent
            if method == "Fetch.enable"
        ]
        self.assertTrue(armed, "the worker was never gated")
        self.assertEqual([{"urlPattern": "*", "requestStage": "Request"}], armed[0]["patterns"])

    def test_nothing_in_this_build_can_evaluate_anything_in_a_target(self) -> None:
        """The hold is gone and so is the one Runtime name that released it."""
        self.assertFalse(
            any(name.startswith("Runtime.") for name in BROWSER_POLICY_METHODS),
            "the standing policy can reach the Runtime domain",
        )
        for methods in BROWSER_CAPABILITY_METHODS.values():
            self.assertFalse(any(name.startswith("Runtime.") for name in methods))

    def test_a_target_with_no_fetch_domain_is_held_by_the_boundary_instead(self) -> None:
        """A dedicated worker has no Fetch domain; treating that as a lost
        gate destroyed the tab, so any page with a worker killed itself."""
        refuser = _FetchEnable()
        self.start_browser(**{"Fetch.enable": refuser})
        self.navigate()
        policy = self.policies[TAB]
        policy.proxy = PolicyProxy()          # a boundary exists for this tab
        self.addCleanup(policy.proxy.stop)
        refuser.refuse = True
        self.browser.announce([self.attached("w-session")])
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while time.monotonic() < deadline and not policy.ungated:
            time.sleep(0.02)
        self.assertTrue(policy.alive, "an ungateable worker killed the tab")
        self.assertEqual(["service_worker"], policy.ungated)

    def test_an_ungateable_target_with_no_boundary_still_fails_closed(self) -> None:
        """Without a boundary there is no backstop, so it must not proceed."""
        refuser = _FetchEnable()
        self.start_browser(**{"Fetch.enable": refuser})
        self.navigate()
        policy = self.policies[TAB]
        policy.proxy = None
        refuser.refuse = True
        self.browser.announce([self.attached("w-session")])
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while policy.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(policy.alive)

    def test_a_worker_that_names_no_session_fails_closed(self) -> None:
        self.start_browser()
        self.navigate()
        policy = self.policies[TAB]
        self.browser.announce(
            [{"method": "Target.attachedToTarget", "params": {"waitingForDebugger": True}}]
        )
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while policy.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(policy.alive, "an unmediatable target did not end mediation")
        self.assertIn("Target.closeTarget", [m for m, _ in self.browser.sent])


class FailClosedTests(LifecycleCase):
    """Losing custody destroys the page rather than releasing the request."""

    def test_a_refusal_the_browser_will_not_acknowledge_destroys_the_tab(self) -> None:
        self.navigator = _LivePageNavigator(refuse_fail_for=("r2", "r3", "r4"))
        self.start_browser()
        self.navigate()
        policy = self.policies[TAB]
        url = f"{self.b.origin}/exfil"
        self.page_requests(url, "Fetch")
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while policy.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(policy.alive)
        # Never released onto the wire, and the page holding it is gone.
        self.assertEqual([], self.b.received)
        self.assertEqual([], self.navigator.released)
        self.assertIn("Target.closeTarget", [m for m, _ in self.browser.sent])
        self.assertNotIn(url, self.navigator.fetched)

    def test_a_tab_whose_mediation_died_is_never_silently_reused(self) -> None:
        """Mediation dies with the tab's ownership, so the tab goes too.

        The earlier shape replaced a dead policy with a fresh one and carried
        on. That is precisely how a tab ends up mediated but with no boundary
        underneath it, so a tab whose mediation has died is now refused and
        taken away rather than re-adopted.
        """
        self.start_browser()
        self.navigate()
        policy = self.policies[TAB]
        policy._fail_closed()
        self.assertFalse(policy.alive)
        with self.assertRaises(BrowserCommandFailed):
            self.run_capability("browser_page_read")
        # And it is gone from the bookkeeping, so it cannot be found again.
        self.assertNotIn(TAB, self.policies)
        self.assertEqual("", read_current_tab(self.profile))

    def test_a_policy_that_lost_its_boundary_does_not_own_its_tab(self) -> None:
        """Every part of the relationship is asked for, not just liveness."""
        self.start_browser()
        self.navigate()
        policy = self.policies[TAB]
        self.assertIsNotNone(owning_policy(self.policies, TAB))
        policy.proxy.stop()
        self.assertIsNone(
            owning_policy(self.policies, TAB), "a stopped boundary still read as ownership"
        )

    def test_a_policy_that_lost_its_context_does_not_own_its_tab(self) -> None:
        self.start_browser()
        self.navigate()
        self.assertIsNotNone(owning_policy(self.policies, TAB))
        self.policies[TAB].context_id = ""
        self.assertIsNone(owning_policy(self.policies, TAB))

    def test_a_tab_in_a_different_context_is_not_this_policys_tab(self) -> None:
        self.start_browser()
        self.navigate()
        self.assertIsNotNone(owning_policy(self.policies, TAB, "CTX1"))
        self.assertIsNone(owning_policy(self.policies, TAB, "CTX-OTHER"))


class ScopeTests(unittest.TestCase):
    """The mediated browser is JARVIS's own, and only that."""

    def test_the_profile_and_port_are_never_the_operators_own(self) -> None:
        self.assertNotEqual(9222, BROWSER_DEBUG_PORT)
        arguments = browser_arguments(
            profile_dir="C:/jarvis/browser-profile", port=BROWSER_DEBUG_PORT, proxy_port=1
        )
        self.assertIn("--user-data-dir=C:/jarvis/browser-profile", arguments)
        self.assertIn(f"--remote-debugging-port={BROWSER_DEBUG_PORT}", arguments)

    def test_a_tab_that_cannot_be_mediated_is_never_treated_as_mediated(self) -> None:
        """Arming is the thing that may fail; pretending it worked is not."""
        policy = BrowserPolicy("ws://127.0.0.1:1/x", tab_id=TAB, origin="https://example.com")
        self.assertEqual(TAB, policy.tab_id)
        with self.assertRaises(BrowserCommandFailed):
            policy.arm()
        self.assertFalse(policy.alive)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
