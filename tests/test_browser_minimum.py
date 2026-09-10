"""Browser basic usable-v1: bounded navigation, reading and interaction.

Offline throughout. No real browser is ever launched: the protocol seam runs
against a scripted loopback DevTools server the test owns, so the hand-rolled
WebSocket framing and the method allowlist are exercised for real while the
page, the tree and every answer are values the test chose. Core is driven end
to end for the properties only Core can prove - the mandatory approval, a
mutation after approval, the in-doubt outcome of an unacknowledged
interaction and the kill switch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jarvis.app import TaskInputError, browser_authority, decode_task_json
from jarvis.browser import (
    BROWSER_CAPABILITIES,
    BROWSER_MAX_REDIRECT_HOPS,
    BROWSER_CAPABILITY_METHODS,
    BROWSER_CONNECTOR,
    BROWSER_DEFERRED_CAPABILITIES,
    BROWSER_ENABLED_CAPABILITIES,
    BROWSER_INTERACT_CAPABILITIES,
    BROWSER_READ_CAPABILITIES,
    BROWSER_TEXT_MAX_CHARS,
    BrowserExecutionUnavailable,
    BrowserOriginBlocked,
    BrowserOutcomeUnknown,
    BrowserResource,
    BrowserResourceKind,
    browser_operation_plan,
    browser_page_digest,
    browser_page_rendering,
    browser_resource_from_dict,
    browser_text_refusal,
    build_browser_request_frame,
    is_browser_destination,
    url_in_origin,
)
from jarvis.browser_cdp import (
    BROWSER_BLANK_PAGE,
    BrowserCommandFailed,
    BrowserCommandOutput,
    BrowserCommandStarted,
    CdpConnection,
    browser_arguments,
    read_current_tab,
    run_operation_plan,
    secret_field_refusal,
)
from jarvis.connectors.browser_cdp import BrowserCdpConnector
from jarvis.contracts import (
    ActionScope,
    AgentRun,
    AttemptState,
    ErrorCode,
    PermissionGrant,
    RunStatus,
    SideEffect,
    Task,
    normalize_destination,
)
from jarvis.core import AgentCore
from jarvis.destinations import (
    DestinationGrant,
    DestinationKind,
    connector_destination_refusal,
    destination_refusal,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.registry import ToolRegistry
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

ORIGIN = "https://example.test"
DEST = f"browser/{ORIGIN}"
URL = "https://example.test/page"
TAB = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
OTHER_TAB = "0F1E2D3C4B5A69788796A5B4C3D2E1F0"
POLICY = frozenset(
    DestinationGrant(BROWSER_CONNECTOR, capability, DEST)
    for capability in BROWSER_ENABLED_CAPABILITIES
)

_AX_NODES = [
    {"role": {"value": "heading"}, "name": {"value": "Sign in"}, "backendDOMNodeId": 3},
    {"role": {"value": "textbox"}, "name": {"value": "Email"}, "backendDOMNodeId": 5},
    {
        "role": {"value": "textbox"},
        "name": {"value": "Password"},
        "value": {"value": "hunter2-the-actual-secret"},
        "backendDOMNodeId": 6,
    },
    {"role": {"value": "button"}, "name": {"value": "Continue"}, "backendDOMNodeId": 7},
    {"role": {"value": "generic"}, "name": {"value": "wrapper"}, "backendDOMNodeId": 8},
]

RENDERING = browser_page_rendering(_AX_NODES, url=URL, title="Example")
DIGEST = browser_page_digest(RENDERING)


# --------------------------------------------------------------------------
# A scripted loopback DevTools server - the only "browser" in this file
# --------------------------------------------------------------------------

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_frame(connection: socket.socket) -> str:
    def take(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    first, second = struct.unpack("!BB", take(2))
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", take(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", take(8))[0]
    mask = take(4) if second & 0x80 else b""
    payload = take(size)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    assert first & 0x80, "the client fragmented a frame"
    return payload.decode("utf-8")


def _write_frame(connection: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", 0x81, size)
    elif size < 65536:
        header = struct.pack("!BBH", 0x81, 126, size)
    else:
        header = struct.pack("!BBQ", 0x81, 127, size)
    connection.sendall(header + payload)


class FakeBrowser(threading.Thread):
    """One scripted DevTools endpoint on loopback. Answers, never decides."""

    def __init__(self, answers: dict, navigator: Any = None) -> None:
        super().__init__(daemon=True)
        self.answers = answers
        self.navigator = navigator
        self._pending_id = 0
        self.sent: list[tuple[str, dict]] = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.start()

    @property
    def socket_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/browser/fake"

    def run(self) -> None:  # pragma: no cover - the thread body
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        with connection:
            self._handshake(connection)
            while True:
                try:
                    message = json.loads(_read_frame(connection))
                except (ConnectionError, OSError, ValueError):
                    return
                self.sent.append((message["method"], message.get("params", {})))
                try:
                    if not self._navigation(connection, message):
                        self._answer(connection, message)
                except OSError:
                    return

    def _answer(self, connection: socket.socket, message: dict) -> None:  # pragma: no cover
        answer = self.answers.get(message["method"])
        if callable(answer):
            answer = answer(message.get("params", {}))
        if isinstance(answer, str):  # a scripted protocol error
            body = {"id": message["id"], "error": {"message": answer}}
        else:
            body = {"id": message["id"], "result": answer or {}}
        # One unrelated event first, so the command loop must skip it.
        _write_frame(connection, json.dumps({"method": "Page.frameResized", "params": {}}))
        _write_frame(connection, json.dumps(body))

    def _navigation(self, connection: socket.socket, message: dict) -> bool:  # pragma: no cover
        """Behave like a browser under a document gate, or decline to.

        Only when the test handed over a ``navigator``. Then a navigation is
        not answered until its request is CONTINUED, each hop is announced as
        a paused request before anything is fetched, and a FAILED hop is never
        fetched at all - which is the whole property under test.
        """
        navigator = self.navigator
        if navigator is None:
            return False
        method = message["method"]
        params = message.get("params", {})
        if method == "Page.navigate":
            self._pending_id = message["id"]
            _write_frame(connection, json.dumps(navigator.pause(params["url"])))
            return True
        if method == "Fetch.failRequest":
            navigator.fail()
            _write_frame(connection, json.dumps({"id": message["id"], "result": {}}))
            return True
        if method == "Fetch.continueRequest":
            following = navigator.follow()
            _write_frame(connection, json.dumps({"id": message["id"], "result": {}}))
            if following is None:
                _write_frame(
                    connection,
                    json.dumps({"id": self._pending_id, "result": {"frameId": "f1"}}),
                )
            else:
                _write_frame(connection, json.dumps(following))
            return True
        return False

    def _handshake(self, connection: socket.socket) -> None:  # pragma: no cover
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        key = ""
        for line in request.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

    def stop(self) -> None:
        self._listener.close()


def page_answers(**overrides):
    """The scripted answers a healthy page returns."""
    answers = {
        "Target.getTargets": {
            "targetInfos": [
                {"type": "page", "targetId": TAB, "url": URL, "title": "Example"},
                {"type": "page", "targetId": OTHER_TAB, "url": "https://other.test/", "title": "Other"},
            ]
        },
        "Target.attachToTarget": {"sessionId": "s1"},
        "Target.createTarget": {"targetId": OTHER_TAB},
        "Target.activateTarget": {},
        "Target.closeTarget": {},
        "DOM.enable": {},
        "Accessibility.enable": {},
        "Page.getNavigationHistory": {"currentIndex": 0, "entries": [{"url": URL, "title": "Example"}]},
        "Accessibility.getFullAXTree": {"nodes": _AX_NODES},
        "Page.navigate": {"frameId": "f1"},
        "DOM.scrollIntoViewIfNeeded": {},
        "DOM.getBoxModel": {"model": {"content": [10, 10, 50, 10, 50, 30, 10, 30]}},
        "DOM.describeNode": lambda params: (
            {"node": {"backendNodeId": params["backendNodeId"], "children": []}}
            if "depth" in params
            else {"node": {"nodeName": "INPUT", "attributes": ["type", "email"]}}
        ),
        "DOM.focus": {},
        "Input.dispatchMouseEvent": {},
        "Input.insertText": {},
    }
    # An element really is the topmost thing at its own centre, unless a test
    # says otherwise; the scripted endpoint reproduces that by hit-testing
    # whatever element it was last asked to scroll into view.
    last: list[int] = []

    def scrolled(params):
        last.append(params["backendNodeId"])
        return {}

    answers["DOM.scrollIntoViewIfNeeded"] = scrolled
    answers["DOM.getNodeForLocation"] = lambda params: {"backendNodeId": last[-1]}
    answers.update(overrides)
    return answers


def resource(kind: BrowserResourceKind, **fields) -> BrowserResource:
    return BrowserResource(kind=kind, origin=ORIGIN, **fields)


def frame_for(capability: str, **fields):
    kind = {
        "browser_page_read": BrowserResourceKind.PAGE,
        "browser_tab_list": BrowserResourceKind.TABS,
        "browser_wait": BrowserResourceKind.DELAY,
        "browser_navigate": BrowserResourceKind.NAVIGATION,
        "browser_tab_open": BrowserResourceKind.NEW_TAB,
        "browser_click": BrowserResourceKind.CLICK_TARGET,
        "browser_type": BrowserResourceKind.TEXT_TARGET,
        "browser_tab_switch": BrowserResourceKind.TAB_SELECTION,
        "browser_tab_close": BrowserResourceKind.TAB_CLOSURE,
    }[capability]
    return build_browser_request_frame(
        capability=capability, destination=DEST, resource=resource(kind, **fields)
    )


def browser_task(capability: str, **fields) -> Task:
    return Task(
        objective="drive the browser",
        capability=capability,
        scope=ActionScope(action="browse", target="", destination=DEST),
        browser=frame_for(capability, **fields).resource.to_dict(),
    )


def connector(*, answer=None, ran=None) -> BrowserCdpConnector:
    """The real connector over a recorded plan runner."""
    ran = [] if ran is None else ran

    def runner(plan, **kwargs):
        ran.append(plan)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return BrowserCommandOutput(answer(plan))
        if answer is not None:
            return BrowserCommandOutput(answer)
        if plan.capability in BROWSER_READ_CAPABILITIES:
            return BrowserCommandOutput({"content": RENDERING})
        return BrowserCommandOutput({"tab_id": TAB, "url": URL})

    made = BrowserCdpConnector("C:/jarvis/browser-profile", plan_runner=runner)
    made.ran = ran  # type: ignore[attr-defined]
    return made


def browser_core(memory, connectors, policy=POLICY) -> AgentCore:
    registry = ToolRegistry()
    for made in connectors:
        registry.register(made)
    return AgentCore(
        registry,
        memory,
        Settings(memory_database=memory.database_path),
        destination_policy=frozenset(policy),
    )


# --------------------------------------------------------------------------
# The closed vocabulary and the bounded resource grammar
# --------------------------------------------------------------------------


class VocabularyTests(unittest.TestCase):
    def test_the_enabled_vocabulary_is_exactly_the_minimum_surface(self) -> None:
        self.assertEqual(
            {
                "browser_navigate",
                "browser_page_read",
                "browser_click",
                "browser_type",
                "browser_wait",
                "browser_tab_list",
                "browser_tab_open",
                "browser_tab_switch",
                "browser_tab_close",
            },
            set(BROWSER_ENABLED_CAPABILITIES),
        )

    def test_the_deferred_capabilities_are_named_and_unreachable(self) -> None:
        for capability in BROWSER_DEFERRED_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertIn(capability, BROWSER_CAPABILITIES)
                self.assertNotIn(capability, BROWSER_ENABLED_CAPABILITIES)
                self.assertNotIn(capability, BrowserCdpConnector.manifest.capabilities)
                self.assertNotIn(capability, BROWSER_CAPABILITY_METHODS)

    def test_no_capability_may_send_a_scripting_or_network_method(self) -> None:
        forbidden = {
            "Runtime.evaluate",
            "Runtime.callFunctionOn",
            "Page.addScriptToEvaluateOnNewDocument",
            "Network.enable",
            "Network.getCookies",
            "Storage.getCookies",
            "Browser.setDownloadBehavior",
            "Page.captureScreenshot",
            "DOM.setFileInputFiles",
        }
        for capability, methods in BROWSER_CAPABILITY_METHODS.items():
            with self.subTest(capability=capability):
                self.assertEqual(frozenset(), methods & forbidden)

    def test_a_target_field_outside_its_kind_is_refused(self) -> None:
        for kind, field in (
            (BrowserResourceKind.PAGE, {"url": URL}),
            (BrowserResourceKind.TABS, {"tab_id": TAB}),
            (BrowserResourceKind.NAVIGATION, {"ref": "r1"}),
            (BrowserResourceKind.CLICK_TARGET, {"text": "hello"}),
            (BrowserResourceKind.TAB_SELECTION, {"url": URL}),
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(BrowserExecutionUnavailable):
                    browser_resource_from_dict(
                        {"kind": kind.value, "origin": ORIGIN, **field}
                    )

    def test_a_resource_carries_no_selector_script_or_coordinate_key(self) -> None:
        keys = set(resource(BrowserResourceKind.PAGE).to_dict())
        self.assertEqual(
            {"kind", "origin", "url", "seconds", "tab_id", "ref", "page_digest", "text"},
            keys,
        )

    def test_a_wait_is_bounded(self) -> None:
        for seconds in (0, -1, 31, 10_000, True, "5"):
            with self.subTest(seconds=seconds):
                with self.assertRaises(BrowserExecutionUnavailable):
                    browser_resource_from_dict(
                        {"kind": "delay", "origin": ORIGIN, "seconds": seconds}
                    )
        self.assertEqual(
            5,
            browser_resource_from_dict(
                {"kind": "delay", "origin": ORIGIN, "seconds": 5}
            ).seconds,
        )


# --------------------------------------------------------------------------
# Bounded navigation: the origin is the boundary
# --------------------------------------------------------------------------


class DestinationTests(unittest.TestCase):
    def test_an_origin_destination_is_known_but_authorizes_nobody(self) -> None:
        self.assertTrue(is_browser_destination(DEST))
        self.assertIsNone(destination_refusal(DEST, egress=True))
        self.assertIsNotNone(
            connector_destination_refusal(
                frozenset(),
                connector=BROWSER_CONNECTOR,
                capability="browser_navigate",
                destination=DEST,
                egress=True,
            )
        )
        self.assertIsNone(
            connector_destination_refusal(
                POLICY,
                connector=BROWSER_CONNECTOR,
                capability="browser_navigate",
                destination=DEST,
                egress=True,
            )
        )

    def test_a_noncanonical_origin_is_never_repaired_into_an_authorized_one(self) -> None:
        for spelling in (f" {DEST} ", "browser/HTTPS://EXAMPLE.TEST", f"{DEST}/"):
            with self.subTest(spelling=spelling):
                self.assertEqual(spelling, normalize_destination(spelling))
                self.assertIsNotNone(destination_refusal(normalize_destination(spelling)))

    def test_a_url_outside_the_origin_is_not_expressible(self) -> None:
        for outside in (
            "https://example.test.evil.test/page",
            "http://example.test/page",
            "https://evil.test/page",
            "https://example.test:8443/page",
            "file:///C:/windows/win.ini",
            "javascript:alert(1)",
            "https://user:pass@example.test/page",
        ):
            with self.subTest(url=outside):
                self.assertFalse(url_in_origin(outside, ORIGIN))
                with self.assertRaises(BrowserExecutionUnavailable):
                    browser_resource_from_dict(
                        {"kind": "navigation", "origin": ORIGIN, "url": outside}
                    )
        self.assertTrue(url_in_origin(URL, ORIGIN))

    def test_a_destination_kind_exists_for_an_origin(self) -> None:
        self.assertEqual("origin", DestinationKind.ORIGIN.value)


# --------------------------------------------------------------------------
# The approval binding
# --------------------------------------------------------------------------


class ApprovalBindingTests(unittest.TestCase):
    def test_every_operation_is_egress_and_always_needs_approval(self) -> None:
        made = connector()
        for capability in BROWSER_ENABLED_CAPABILITIES:
            with self.subTest(capability=capability):
                profile = made.side_effects(browser_task(capability, **_fields(capability)))
                self.assertIs(SideEffect.EXTERNAL_EGRESS, profile.effect)
                self.assertTrue(profile.always_requires_approval)
                self.assertEqual(
                    capability not in BROWSER_INTERACT_CAPABILITIES, profile.reversible
                )

    def test_changing_the_target_changes_the_bytes_an_approval_binds(self) -> None:
        made = connector()
        base = made.canonical_outbound(browser_task("browser_click", ref="r4", page_digest=DIGEST))
        for changed in (
            browser_task("browser_click", ref="r2", page_digest=DIGEST),
            browser_task("browser_click", ref="r4", page_digest="0" * 64),
            browser_task("browser_navigate", url=URL),
        ):
            with self.subTest(capability=changed.capability):
                self.assertNotEqual(base, made.canonical_outbound(changed))

    def test_the_preview_names_the_element_and_never_the_page_content(self) -> None:
        preview = connector().outbound_preview(
            browser_task("browser_type", ref="r2", page_digest=DIGEST, text="hello")
        )
        self.assertEqual(DEST, preview.destination)
        self.assertIn("r2", preview.summary)
        self.assertIn("hello", preview.summary)


def _fields(capability: str) -> dict:
    return {
        "browser_navigate": {"url": URL},
        "browser_tab_open": {"url": URL},
        "browser_click": {"ref": "r4", "page_digest": DIGEST},
        "browser_type": {"ref": "r2", "page_digest": DIGEST, "text": "hello"},
        "browser_tab_switch": {"tab_id": TAB},
        "browser_tab_close": {"tab_id": TAB},
        "browser_wait": {"seconds": 2},
    }.get(capability, {})


# --------------------------------------------------------------------------
# The protocol seam, against a scripted loopback DevTools endpoint
# --------------------------------------------------------------------------


class SeamTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.profile = str(Path(self.temp.name) / "browser-profile")

    def perform(self, capability: str, answers=None, **fields) -> BrowserCommandOutput:
        frame = frame_for(capability, **fields)
        browser = FakeBrowser(page_answers(**(answers or {})))
        self.addCleanup(browser.stop)
        self.browser = browser
        return run_operation_plan(
            browser_operation_plan(frame),
            frame=frame,
            profile_dir=self.profile,
            attach=lambda **kwargs: browser.socket_url,
            sleep=lambda seconds: None,
        )


class SeamTests(SeamTestCase):
    def test_a_page_reading_returns_the_bounded_rendering(self) -> None:
        output = self.perform("browser_page_read")
        self.assertEqual(RENDERING, output.payload["content"])
        self.assertIn("r4 button", output.payload["content"])
        self.assertEqual(TAB, read_current_tab(self.profile))

    def test_a_click_dispatches_at_the_element_and_reports_where_it_landed(self) -> None:
        output = self.perform("browser_click", ref="r4", page_digest=DIGEST)
        self.assertEqual({"tab_id": TAB, "url": URL}, output.payload)
        clicks = [params for method, params in self.browser.sent if method == "Input.dispatchMouseEvent"]
        self.assertEqual(["mousePressed", "mouseReleased"], [click["type"] for click in clicks])
        self.assertEqual((30.0, 20.0), (clicks[0]["x"], clicks[0]["y"]))

    def test_typing_focuses_the_field_and_inserts_the_exact_text(self) -> None:
        self.perform("browser_type", ref="r2", page_digest=DIGEST, text="a@b.test")
        sent = dict(self.browser.sent)
        self.assertEqual({"backendNodeId": 5}, sent["DOM.focus"])
        self.assertEqual({"text": "a@b.test"}, sent["Input.insertText"])

    def test_a_page_that_moved_since_it_was_read_refuses_before_any_input(self) -> None:
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform("browser_click", ref="r4", page_digest="1" * 64)
        self.assertIn("changed since it was read", str(raised.exception))
        self.assertNotIn(
            "Input.dispatchMouseEvent", [method for method, _ in self.browser.sent]
        )

    def test_an_element_another_element_covers_is_refused(self) -> None:
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform(
                "browser_click",
                {"DOM.getNodeForLocation": {"backendNodeId": 99}},
                ref="r4",
                page_digest=DIGEST,
            )
        self.assertIn("covers the target", str(raised.exception))
        self.assertNotIn(
            "Input.dispatchMouseEvent", [method for method, _ in self.browser.sent]
        )

    def test_an_element_with_no_area_is_refused(self) -> None:
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform(
                "browser_click",
                {"DOM.getBoxModel": {"model": {"content": [0, 0, 0, 0, 0, 0, 0, 0]}}},
                ref="r4",
                page_digest=DIGEST,
            )
        self.assertIn("occupies no area", str(raised.exception))

    def test_a_ref_no_reading_produced_is_refused(self) -> None:
        # A ref outside the grammar cannot even be spelled into a resource;
        # a well-formed one that this page never printed is refused at the
        # page, before any input is dispatched.
        for ref in ("r0", "button", "r1;drop", "r4 or r2"):
            with self.subTest(ref=ref):
                with self.assertRaises(Exception):
                    frame_for("browser_click", ref=ref, page_digest=DIGEST)
        with self.assertRaises(BrowserExecutionUnavailable):
            self.perform("browser_click", ref="r99", page_digest=DIGEST)
        self.assertNotIn(
            "Input.dispatchMouseEvent", [method for method, _ in self.browser.sent]
        )

    def test_typing_into_something_that_is_not_a_text_field_is_refused(self) -> None:
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform("browser_type", ref="r4", page_digest=DIGEST, text="hello")
        self.assertIn("does not accept typed text", str(raised.exception))

    def test_typing_into_a_password_field_is_refused_at_the_element(self) -> None:
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform(
                "browser_type",
                {
                    "DOM.describeNode": lambda params: (
                        {"node": {"backendNodeId": params["backendNodeId"], "children": []}}
                        if "depth" in params
                        else {"node": {"nodeName": "INPUT", "attributes": ["type", "password"]}}
                    )
                },
                ref="r2",
                page_digest=DIGEST,
                text="ordinary words",
            )
        self.assertIn("does not type credentials", str(raised.exception))
        self.assertNotIn("Input.insertText", [method for method, _ in self.browser.sent])

    def test_a_tab_outside_the_origin_is_neither_listed_nor_switched_to(self) -> None:
        listing = self.perform("browser_tab_list").payload["content"]
        self.assertIn(TAB, listing)
        self.assertNotIn(OTHER_TAB, listing)
        self.assertNotIn("other.test", listing)
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform("browser_tab_switch", tab_id=OTHER_TAB)
        self.assertIn("outside the authorized origin", str(raised.exception))

    def test_the_tab_operations_move_and_clear_the_current_tab(self) -> None:
        self.perform("browser_tab_switch", tab_id=TAB)
        self.assertEqual(TAB, read_current_tab(self.profile))
        self.perform("browser_tab_close", tab_id=TAB)
        self.assertEqual("", read_current_tab(self.profile))
        opened = self.perform("browser_tab_open", url=URL)
        self.assertEqual(OTHER_TAB, opened.payload["tab_id"])
        self.assertEqual(OTHER_TAB, read_current_tab(self.profile))

    def test_an_ambiguous_browser_state_refuses_rather_than_choosing(self) -> None:
        answers = {
            "Target.getTargets": {
                "targetInfos": [
                    {"type": "page", "targetId": TAB, "url": URL, "title": "One"},
                    {"type": "page", "targetId": OTHER_TAB, "url": URL, "title": "Two"},
                ]
            }
        }
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform("browser_page_read", answers)
        self.assertIn("ambiguous", str(raised.exception))

    def test_a_tab_that_drifted_outside_the_origin_refuses_the_approved_action(self) -> None:
        answers = {
            "Page.getNavigationHistory": {
                "currentIndex": 0,
                "entries": [{"url": "https://evil.test/", "title": "Elsewhere"}],
            }
        }
        with self.assertRaises(BrowserCommandFailed) as raised:
            self.perform("browser_page_read", answers)
        self.assertIn("no longer inside the authorized origin", str(raised.exception))

    def test_a_wait_sleeps_the_bounded_time_and_returns_the_page_state(self) -> None:
        slept: list[int] = []
        frame = frame_for("browser_wait", seconds=3)
        browser = FakeBrowser(page_answers())
        self.addCleanup(browser.stop)
        output = run_operation_plan(
            browser_operation_plan(frame),
            frame=frame,
            profile_dir=self.profile,
            attach=lambda **kwargs: browser.socket_url,
            sleep=slept.append,
        )
        self.assertEqual([3], slept)
        self.assertEqual(RENDERING, output.payload["content"])

    def test_a_navigation_error_after_dispatch_does_not_read_as_no_effect(self) -> None:
        with self.assertRaises(BrowserCommandStarted):
            self.perform(
                "browser_navigate",
                {"Page.navigate": {"errorText": "net::ERR_ABORTED"}},
                url=URL,
            )

    def test_an_unanswered_input_after_dispatch_does_not_read_as_no_effect(self) -> None:
        with self.assertRaises(BrowserCommandStarted):
            self.perform(
                "browser_click",
                {"Input.dispatchMouseEvent": "the tab crashed"},
                ref="r4",
                page_digest=DIGEST,
            )

    def test_the_connection_refuses_a_method_outside_the_capability(self) -> None:
        browser = FakeBrowser(page_answers())
        self.addCleanup(browser.stop)
        connection = CdpConnection(
            browser.socket_url, BROWSER_CAPABILITY_METHODS["browser_tab_list"], timeout=5
        )
        self.addCleanup(connection.close)
        self.assertEqual({}, connection.command("Target.getTargets").get("nothing", {}))
        for method in ("Runtime.evaluate", "Network.getCookies", "Page.captureScreenshot"):
            with self.subTest(method=method):
                with self.assertRaises(BrowserCommandFailed):
                    connection.command(method, {"expression": "1"})
        self.assertEqual(["Target.getTargets"], [name for name, _ in browser.sent])

    def test_the_launch_vector_uses_a_dedicated_profile_and_no_ambient_session(self) -> None:
        arguments = browser_arguments(profile_dir=self.profile, port=9333)
        self.assertIn("--remote-debugging-port=9333", arguments)
        self.assertIn(f"--user-data-dir={self.profile}", arguments)
        self.assertIn("--disable-extensions", arguments)


# --------------------------------------------------------------------------
# Credentials and untrusted content
# --------------------------------------------------------------------------


class ConfidentialityTests(unittest.TestCase):
    def test_no_field_value_ever_reaches_the_rendering(self) -> None:
        # The accessibility tree reports a password input as an ordinary
        # textbox, so "redact the password fields" is not a rule that can be
        # applied; no value is rendered at all.
        nodes = [
            {
                "role": {"value": "textbox"},
                "name": {"value": "Password"},
                "value": {"value": "hunter2-the-actual-secret"},
                "backendDOMNodeId": 6,
            }
        ]
        rendered = browser_page_rendering(nodes, url=URL, title="t")
        self.assertNotIn("hunter2-the-actual-secret", rendered)
        self.assertNotIn("value=", rendered)
        self.assertNotIn("hunter2-the-actual-secret", RENDERING)
        self.assertIn('r1 textbox "Password"', rendered)

    def test_a_credential_shape_in_the_page_is_redacted_out_of_the_rendering(self) -> None:
        nodes = [
            {
                "role": {"value": "StaticText"},
                "name": {"value": "api_key=sk-live-01234567890123456789012345678901"},
                "backendDOMNodeId": 4,
            }
        ]
        rendered = browser_page_rendering(nodes, url=URL, title="t")
        self.assertNotIn("sk-live-01234567890123456789012345678901", rendered)

    def test_jarvis_refuses_to_type_a_credential_shape(self) -> None:
        for text in (
            "password: hunter2hunter2hunter2",
            "Bearer sk-live-01234567890123456789012345678901",
            "--token abcdefghijklmnopqrstuvwxyz012345",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(browser_text_refusal(text))
                with self.assertRaises(BrowserExecutionUnavailable):
                    browser_resource_from_dict(
                        {
                            "kind": "text_target",
                            "origin": ORIGIN,
                            "ref": "r2",
                            "page_digest": DIGEST,
                            "text": text,
                        }
                    )
        self.assertIsNone(browser_text_refusal("ordinary search terms"))
        self.assertIsNotNone(browser_text_refusal("x" * (BROWSER_TEXT_MAX_CHARS + 1)))

    def test_a_password_field_is_refused_at_the_element(self) -> None:
        self.assertIsNotNone(
            secret_field_refusal({"nodeName": "INPUT", "attributes": ["type", "password"]})
        )
        self.assertIsNotNone(
            secret_field_refusal(
                {"nodeName": "INPUT", "attributes": ["autocomplete", "current-password"]}
            )
        )
        self.assertIsNone(
            secret_field_refusal({"nodeName": "INPUT", "attributes": ["type", "text"]})
        )

    def test_page_content_arrives_as_untrusted_reference_material(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        memory = SQLiteMemoryStore(Path(temp.name) / "jarvis.db")
        made = connector()
        core = browser_core(memory, (made,))
        waiting = core.run(browser_task("browser_page_read"))
        done = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertEqual(RunStatus.COMPLETED, done.status, done.error)
        stored = memory.load_task(done.task.id).external_read
        self.assertEqual(BROWSER_CONNECTOR, stored["connector"])
        self.assertEqual(RENDERING, stored["content"])
        envelope = _envelope_read_block(memory.load_task(done.task.id))
        self.assertFalse(envelope["trusted"])
        self.assertEqual(DEST, envelope["destination"])
        self.assertNotIn("source_task_id", envelope)


def _envelope_read_block(task: Task) -> dict:
    from jarvis.app import _external_read_payload

    return _external_read_payload(task.external_read)


# --------------------------------------------------------------------------
# Core: the gate, the mutation, the in-doubt outcome and the kill switch
# --------------------------------------------------------------------------


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.memory = SQLiteMemoryStore(Path(self.temp.name) / "jarvis.db")

    def approve_and_resume(self, core: AgentCore, waiting: AgentRun) -> AgentRun:
        grant = PermissionGrant.for_request(waiting.permission_request)
        return core.resume(waiting.session.id, waiting.task.id, grant)

    def test_no_browser_operation_runs_without_an_exact_approval(self) -> None:
        for capability in BROWSER_ENABLED_CAPABILITIES:
            with self.subTest(capability=capability):
                made = connector()
                core = browser_core(self.memory, (made,))
                waiting = core.run(browser_task(capability, **_fields(capability)))
                self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
                self.assertEqual([], made.ran)
                done = self.approve_and_resume(core, waiting)
                self.assertEqual(RunStatus.COMPLETED, done.status, done.error)
                self.assertEqual(capability, made.ran[0].capability)

    def test_a_mutation_after_approval_is_refused_with_the_old_grant(self) -> None:
        made = connector()
        core = browser_core(self.memory, (made,))
        waiting = core.run(browser_task("browser_click", ref="r4", page_digest=DIGEST))
        grant = PermissionGrant.for_request(waiting.permission_request)
        stored = self.memory.load_task(waiting.task.id)
        stored.browser = dict(stored.browser, ref="r2")
        self.memory.save_task(waiting.session.id, stored)
        refused = core.resume(waiting.session.id, waiting.task.id, grant)
        self.assertNotEqual(RunStatus.COMPLETED, refused.status)
        self.assertEqual(ErrorCode.INVALID_GRANT, refused.error.code)
        self.assertEqual([], made.ran)

    def test_an_unauthorized_origin_never_reaches_the_connector(self) -> None:
        made = connector()
        run = browser_core(self.memory, (made,), policy=frozenset()).run(
            browser_task("browser_navigate", url=URL)
        )
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual([], made.ran)

    def test_an_unacknowledged_interaction_is_in_doubt_and_never_replayed(self) -> None:
        for answer in (
            BrowserOutcomeUnknown("the click may have reached the page"),
            BrowserCommandStarted("the tab crashed after the click"),
        ):
            with self.subTest(answer=type(answer).__name__):
                made = connector(answer=answer)
                core = browser_core(self.memory, (made,))
                waiting = core.run(browser_task("browser_click", ref="r4", page_digest=DIGEST))
                in_doubt = self.approve_and_resume(core, waiting)
                self.assertNotEqual(RunStatus.COMPLETED, in_doubt.status)
                self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, in_doubt.error.code)
                self.assertIs(
                    AttemptState.UNKNOWN_OUTCOME,
                    self.memory.latest_execution_attempt(waiting.task.id).state,
                )
                again = core.resume(
                    waiting.session.id,
                    waiting.task.id,
                    PermissionGrant.for_request(waiting.permission_request),
                )
                self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, again.error.code)
                self.assertEqual(1, len(made.ran))

    def test_an_unreadable_acknowledgement_is_in_doubt_not_a_success(self) -> None:
        made = connector(answer={"tab_id": "", "url": ""})
        core = browser_core(self.memory, (made,))
        waiting = core.run(browser_task("browser_click", ref="r4", page_digest=DIGEST))
        in_doubt = self.approve_and_resume(core, waiting)
        self.assertEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, in_doubt.error.code)

    def test_a_navigation_that_left_the_origin_is_not_reported_as_success(self) -> None:
        made = connector(answer={"tab_id": TAB, "url": "https://evil.test/landed"})
        core = browser_core(self.memory, (made,))
        waiting = core.run(browser_task("browser_navigate", url=URL))
        done = self.approve_and_resume(core, waiting)
        self.assertNotEqual(RunStatus.COMPLETED, done.status)
        self.assertIn("unauthorized landing", done.error.message)
        self.assertFalse(done.error.retryable)

    def test_a_refusal_before_dispatch_is_not_an_unknown_outcome(self) -> None:
        made = connector(answer=BrowserCommandFailed("the browser holds no such tab"))
        core = browser_core(self.memory, (made,))
        waiting = core.run(browser_task("browser_tab_switch", tab_id=TAB))
        done = self.approve_and_resume(core, waiting)
        self.assertNotEqual(ErrorCode.EXECUTION_UNKNOWN_OUTCOME, done.error.code)
        self.assertIs(
            AttemptState.FINISHED,
            self.memory.latest_execution_attempt(waiting.task.id).state,
        )

    def test_the_kill_switch_refuses_before_any_authority(self) -> None:
        made = connector()
        registry = ToolRegistry()
        registry.register(made)
        core = AgentCore(
            registry,
            self.memory,
            Settings(memory_database=self.memory.database_path, execution_enabled=False),
            destination_policy=POLICY,
        )
        run = core.run(browser_task("browser_click", ref="r4", page_digest=DIGEST))
        self.assertEqual(RunStatus.FAILED, run.status)
        self.assertEqual(ErrorCode.EXECUTION_DISABLED, run.error.code)
        self.assertEqual([], made.ran)

    def test_a_cancellation_closes_the_live_connection_and_nothing_else(self) -> None:
        made = connector()
        self.assertFalse(made.cancel("task-1"))
        closed: list[bool] = []
        made._hold(type("Live", (), {"close": lambda self: closed.append(True)})())
        self.assertTrue(made.cancel("task-1"))
        self.assertEqual([True], closed)
        made._hold(None)
        self.assertFalse(made.cancel("task-1"))

    def test_a_cancelled_run_never_reaches_the_browser(self) -> None:
        made = connector()
        core = browser_core(self.memory, (made,))
        waiting = core.run(browser_task("browser_click", ref="r4", page_digest=DIGEST))
        core.cancel(waiting.session.id, waiting.task.id)
        done = core.resume(
            waiting.session.id,
            waiting.task.id,
            PermissionGrant.for_request(waiting.permission_request),
        )
        self.assertNotEqual(RunStatus.COMPLETED, done.status)
        self.assertEqual([], made.ran)


# --------------------------------------------------------------------------
# Configured authority and the task input boundary
# --------------------------------------------------------------------------


class ConfiguredAuthorityTests(unittest.TestCase):
    def test_an_unconfigured_installation_reaches_no_origin(self) -> None:
        self.assertEqual(frozenset(), browser_authority(Settings()))

    def test_a_configured_origin_becomes_one_grant_per_enabled_capability(self) -> None:
        grants = browser_authority(Settings(browser_origins=(DEST,)))
        self.assertEqual(POLICY, grants)
        self.assertEqual(len(BROWSER_ENABLED_CAPABILITIES), len(grants))

    def test_a_deferred_capability_gets_no_grant(self) -> None:
        grants = browser_authority(Settings(browser_origins=(DEST,)))
        for capability in BROWSER_DEFERRED_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertNotIn(
                    DestinationGrant(BROWSER_CONNECTOR, capability, DEST), grants
                )

    def test_an_unusable_origin_is_refused_at_configuration(self) -> None:
        for spelling in ("example.test", "browser/ftp://example.test", "browser/https://EXAMPLE.test"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(ValueError):
                    Settings(browser_origins=(spelling,))


class TaskInputTests(unittest.TestCase):
    def decode(self, document: dict) -> Task:
        return decode_task_json(json.dumps(document))

    def base(self, **browser) -> dict:
        return {
            "objective": "drive the browser",
            "capability": "browser_click",
            "scope": {"action": "browse", "target": "", "destination": DEST},
            "browser": {
                "kind": "click_target",
                "origin": ORIGIN,
                "ref": "r4",
                "page_digest": DIGEST,
                **browser,
            },
        }

    def test_a_valid_browser_section_is_stored_canonically(self) -> None:
        task = self.decode(self.base())
        self.assertEqual("click_target", task.browser["kind"])
        self.assertEqual(DIGEST, task.browser["page_digest"])

    def test_a_browser_capability_without_a_browser_section_is_refused(self) -> None:
        document = self.base()
        del document["browser"]
        with self.assertRaises(TaskInputError):
            self.decode(document)

    def test_an_unknown_browser_key_is_refused_by_name(self) -> None:
        for key in ("selector", "script", "x", "headers", "cookie"):
            with self.subTest(key=key):
                with self.assertRaises(TaskInputError):
                    self.decode(self.base(**{key: "anything"}))

    def test_a_deferred_capability_cannot_be_named_by_a_task(self) -> None:
        for capability in BROWSER_DEFERRED_CAPABILITIES:
            with self.subTest(capability=capability):
                document = self.base()
                document["capability"] = capability
                with self.assertRaises(TaskInputError):
                    self.decode(document)

    def test_the_browser_section_may_not_ride_in_context(self) -> None:
        # ``context`` is serialized verbatim into an external model's payload,
        # so the target must be a top-level section - and ``context`` is
        # default-deny, which is what refuses the smuggled copy by name.
        document = self.base()
        document["context"] = {"browser": {"kind": "click_target", "origin": ORIGIN}}
        with self.assertRaises(TaskInputError):
            self.decode(document)


# --------------------------------------------------------------------------
# Cross-origin redirect authority: two real local origins, one real chain
# --------------------------------------------------------------------------


class _Origin(HTTPServer):
    """One controlled local origin that records every request it received."""

    def __init__(self, redirects: dict) -> None:
        self.received: list[str] = []
        self.redirects = redirects
        super().__init__(("127.0.0.1", 0), _OriginHandler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - the stdlib spelling
        self.server.received.append(self.path)
        target = self.server.redirects.get(self.path)
        if callable(target):
            target = target()
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"<html><body>landed</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # pragma: no cover - quiet
        pass


class _GatedNavigator:
    """A browser's navigation behaviour under a document gate.

    A hop is announced BEFORE it is fetched, and it is fetched only if the
    client continues it - so a hop the client fails is never requested at all,
    exactly as a real browser refusing a paused request never opens the
    connection to that origin. The redirect chain itself is real: it comes
    from the local servers, over HTTP, one hop at a time, without the client
    library following anything on its own.
    """

    def __init__(self) -> None:
        self.hop = ""
        self.fetched: list[str] = []
        self.failed: list[str] = []
        self.landed = ""

    def pause(self, url: str) -> dict:
        self.hop = url
        return {
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": f"r{len(self.fetched) + 1}",
                "request": {"url": url},
            },
        }

    def fail(self) -> None:
        self.failed.append(self.hop)
        self.hop = ""

    def follow(self) -> dict | None:
        """Perform the hop the client continued; announce the next, if any."""
        url = self.hop
        self.fetched.append(url)
        parsed = urlsplit(url)
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.request("GET", parsed.path or "/")
            response = connection.getresponse()
            location = (
                response.getheader("Location") if 300 <= response.status < 400 else None
            )
            response.read()
        finally:
            connection.close()
        if location:
            return self.pause(location)
        self.landed = url
        return None


class RedirectAuthorityTests(unittest.TestCase):
    """An approval for one origin is not an approval for wherever it points."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.profile = str(Path(self.temp.name) / "browser-profile")
        self.b = _Origin({})
        self.addCleanup(self.b.stop)
        self.a = _Origin(
            {
                "/offsite": lambda: f"{self.b.origin}/landing",
                "/local": lambda: f"{self.a.origin}/final",
            }
        )
        self.addCleanup(self.a.stop)

    def drive(self, capability: str, path: str) -> BrowserCommandOutput:
        """One real operation of ``capability`` against origin A + ``path``."""
        origin = self.a.origin
        kind = (
            BrowserResourceKind.NAVIGATION
            if capability == "browser_navigate"
            else BrowserResourceKind.NEW_TAB
        )
        frame = build_browser_request_frame(
            capability=capability,
            destination=f"browser/{origin}",
            resource=BrowserResource(kind=kind, origin=origin, url=f"{origin}{path}"),
        )
        self.navigator = _GatedNavigator()
        answers = page_answers(
            **{
                "Target.getTargets": {
                    "targetInfos": [
                        {"type": "page", "targetId": TAB, "url": f"{origin}/", "title": "A"}
                    ]
                },
                "Target.createTarget": {"targetId": TAB},
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
        )

    # -- the invariant ----------------------------------------------------

    def test_a_cross_origin_redirect_is_never_dispatched(self) -> None:
        for capability in ("browser_navigate", "browser_tab_open"):
            with self.subTest(capability=capability):
                self.a.received.clear()
                self.b.received.clear()
                with self.assertRaises(BrowserOriginBlocked) as raised:
                    self.drive(capability, "/offsite")
                # The approved origin was contacted, as authorized.
                self.assertEqual(["/offsite"], self.a.received)
                # The redirected request never reached origin B. Proved by B's
                # OWN server, not by our transcript of what we meant to do.
                self.assertEqual([], self.b.received)
                self.assertEqual(
                    [], [url for url in self.navigator.fetched if self.b.origin in url]
                )
                self.assertEqual([f"{self.b.origin}/landing"], self.navigator.failed)
                self.assertEqual(self.b.origin, raised.exception.blocked)
                # Refused at the socket, before the wire.
                self.assertIn(
                    "Fetch.failRequest", [method for method, _ in self.browser.sent]
                )
                self.assertNotIn(
                    "Fetch.continueRequest",
                    [method for method, _ in self.browser.sent][-1:],
                )

    def test_the_blocked_navigation_is_not_reported_as_success(self) -> None:
        for capability in ("browser_navigate", "browser_tab_open"):
            with self.subTest(capability=capability):
                temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                self.addCleanup(safe_cleanup, temp)
                memory = SQLiteMemoryStore(Path(temp.name) / "jarvis.db")
                origin = self.a.origin
                destination = f"browser/{origin}"
                made = BrowserCdpConnector(
                    self.profile,
                    plan_runner=lambda plan, **kwargs: self.drive(capability, "/offsite"),
                )
                registry = ToolRegistry()
                registry.register(made)
                core = AgentCore(
                    registry,
                    memory,
                    Settings(memory_database=memory.database_path),
                    destination_policy=frozenset(
                        DestinationGrant(BROWSER_CONNECTOR, name, destination)
                        for name in BROWSER_ENABLED_CAPABILITIES
                    ),
                )
                task = Task(
                    objective="drive the browser",
                    capability=capability,
                    scope=ActionScope(action="browse", target="", destination=destination),
                    browser={
                        "kind": (
                            "navigation"
                            if capability == "browser_navigate"
                            else "new_tab"
                        ),
                        "origin": origin,
                        "url": f"{origin}/offsite",
                        "seconds": 0,
                        "tab_id": "",
                        "ref": "",
                        "page_digest": "",
                        "text": "",
                    },
                )
                waiting = core.run(task)
                self.assertEqual(RunStatus.AWAITING_APPROVAL, waiting.status)
                done = core.resume(
                    waiting.session.id,
                    waiting.task.id,
                    PermissionGrant.for_request(waiting.permission_request),
                )
                self.assertNotEqual(RunStatus.COMPLETED, done.status)
                self.assertEqual(ErrorCode.EGRESS_REFUSED, done.error.code)
                self.assertFalse(done.error.retryable)
                self.assertIn("before it was sent", done.error.message)
                # Not an unknown outcome: exactly what happened is known.
                self.assertNotEqual(
                    ErrorCode.EXECUTION_UNKNOWN_OUTCOME, done.error.code
                )
                # The violation is known NOW. No page reading was needed to
                # discover it, and none was performed.
                self.assertEqual({}, memory.load_task(done.task.id).external_read)
                self.assertEqual([], self.b.received)

    # -- what must keep working -------------------------------------------

    def test_a_same_origin_redirect_still_completes(self) -> None:
        for capability in ("browser_navigate", "browser_tab_open"):
            with self.subTest(capability=capability):
                self.a.received.clear()
                output = self.drive(capability, "/local")
                self.assertEqual(f"{self.a.origin}/final", output.payload["url"])
                self.assertEqual(["/local", "/final"], self.a.received)
                self.assertEqual([], self.navigator.failed)
                self.assertEqual([], self.b.received)

    def test_a_direct_same_origin_request_still_completes(self) -> None:
        for capability in ("browser_navigate", "browser_tab_open"):
            with self.subTest(capability=capability):
                self.a.received.clear()
                output = self.drive(capability, "/final")
                self.assertEqual(f"{self.a.origin}/final", output.payload["url"])
                self.assertEqual(["/final"], self.a.received)
                self.assertEqual(TAB, output.payload["tab_id"])

    # -- how the gate is armed --------------------------------------------

    def test_a_new_tab_is_opened_empty_so_nothing_loads_before_the_gate(self) -> None:
        self.drive("browser_tab_open", "/final")
        created = [
            params
            for method, params in self.browser.sent
            if method == "Target.createTarget"
        ]
        self.assertEqual([{"url": BROWSER_BLANK_PAGE}], created)

    def test_the_gate_is_armed_before_the_navigation_and_never_left_armed(self) -> None:
        for capability in ("browser_navigate", "browser_tab_open"):
            with self.subTest(capability=capability):
                self.drive(capability, "/final")
                methods = [method for method, _ in self.browser.sent]
                self.assertLess(
                    methods.index("Fetch.enable"), methods.index("Page.navigate")
                )
                self.assertEqual(1, methods.count("Fetch.disable"))
                patterns = dict(self.browser.sent)["Fetch.enable"]["patterns"]
                self.assertEqual(
                    [
                        {
                            "urlPattern": "*",
                            "resourceType": "Document",
                            "requestStage": "Request",
                        }
                    ],
                    patterns,
                )

    def test_the_gate_is_disarmed_even_when_a_hop_is_blocked(self) -> None:
        with self.assertRaises(BrowserOriginBlocked):
            self.drive("browser_navigate", "/offsite")
        self.assertEqual(
            1, [method for method, _ in self.browser.sent].count("Fetch.disable")
        )

    def test_the_gate_cannot_synthesize_a_response_or_answer_a_credential(self) -> None:
        for capability in ("browser_navigate", "browser_tab_open"):
            with self.subTest(capability=capability):
                methods = BROWSER_CAPABILITY_METHODS[capability]
                self.assertNotIn("Fetch.fulfillRequest", methods)
                self.assertNotIn("Fetch.continueWithAuth", methods)
                self.assertNotIn("Fetch.getResponseBody", methods)
                self.assertIn("Fetch.failRequest", methods)

    def test_only_top_level_documents_are_gated(self) -> None:
        # A page's own stylesheets, scripts and XHRs are never paused, so this
        # is a destination gate on the approved navigation, not a network
        # policy over the page.
        self.drive("browser_navigate", "/final")
        patterns = dict(self.browser.sent)["Fetch.enable"]["patterns"]
        self.assertEqual(["Document"], [one["resourceType"] for one in patterns])

    def test_an_endless_redirect_chain_is_refused_rather_than_followed(self) -> None:
        loop = _Origin({})
        loop.redirects["/loop"] = lambda: f"{loop.origin}/loop"
        self.addCleanup(loop.stop)
        origin = loop.origin
        frame = build_browser_request_frame(
            capability="browser_navigate",
            destination=f"browser/{origin}",
            resource=BrowserResource(
                kind=BrowserResourceKind.NAVIGATION, origin=origin, url=f"{origin}/loop"
            ),
        )
        self.navigator = _GatedNavigator()
        browser = FakeBrowser(
            page_answers(
                **{
                    "Target.getTargets": {
                        "targetInfos": [
                            {
                                "type": "page",
                                "targetId": TAB,
                                "url": f"{origin}/",
                                "title": "loop",
                            }
                        ]
                    }
                }
            ),
            navigator=self.navigator,
        )
        self.addCleanup(browser.stop)
        with self.assertRaises(BrowserOriginBlocked):
            run_operation_plan(
                browser_operation_plan(frame),
                frame=frame,
                profile_dir=self.profile,
                attach=lambda **kwargs: browser.socket_url,
                sleep=lambda seconds: None,
            )
        self.assertLessEqual(len(self.navigator.fetched), BROWSER_MAX_REDIRECT_HOPS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
