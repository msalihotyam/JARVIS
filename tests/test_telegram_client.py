"""TGCLIENT1: Telegram HTTPS client tests. Fake transport only; sockets patched to raise."""

import ast
import email.message
import io
import json
import os
import re
import socket
import ssl
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from jarvis import telegram_client as tc
from jarvis.telegram_files import plan_download
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT
from tests.adversarial import deep_json, each

TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"  # fake, test-only
SECRET = TOKEN.split(":", 1)[1]
CHAT = 123
TOKEN_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
ROOT = Path(__file__).resolve().parents[1]
_patches = []


def _no_network(*_args, **_kwargs):
    raise OSError("network disabled in tests")


def setUpModule():
    for name in ("socket", "create_connection"):
        patcher = mock.patch.object(socket, name, _no_network)
        patcher.start()
        _patches.append(patcher)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.status, self.headers, self._data = status, headers or {}, body
        self.served, self.closed = 0, False

    def read(self, n=-1):
        chunk = self._data[self.served:self.served + n]
        self.served += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class Endless(FakeResponse):
    def read(self, n=-1):
        self.served += n
        return b"x" * n


class FakeTransport:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Sink:
    def __init__(self, fail=False):
        self.data, self.discarded, self.fail = bytearray(), False, fail

    def write(self, chunk):
        if self.fail:
            raise OSError("disk full")
        self.data += chunk

    def discard(self):
        self.discarded = True


def ok(result):
    return FakeResponse(json.dumps({"ok": True, "result": result}).encode())


def http_error(code, body=b"", location=None):
    headers = email.message.Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError(TOKEN_URL, code, "status", headers, io.BytesIO(body))


def client(transport, **kwargs):
    return tc.TelegramClient(lambda: TOKEN, CHAT, transport=transport, **kwargs)


def send(c, **body):
    return c.call("sendMessage", {"chat_id": CHAT, "text": "hi", **body})


class ClientCallTests(unittest.TestCase):
    def test_send_message_ok_https_single_host(self):
        t = FakeTransport(ok({"message_id": 1}))
        self.assertEqual(send(client(t, timeout=7.5)), tc.Ok({"message_id": 1}))
        request, timeout = t.calls[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.hostname, url.port), ("https", tc.API_HOST, None))
        self.assertEqual((request.get_method(), timeout), ("POST", 7.5))
        self.assertEqual(json.loads(request.data)["chat_id"], CHAT)

    def test_get_updates_ok_list(self):
        t = FakeTransport(ok([{"update_id": 1}]))
        self.assertEqual(client(t).call("getUpdates", {"timeout": 0}), tc.Ok([{"update_id": 1}]))

    def test_allowlist_is_fixed(self):
        want = {"getUpdates", "sendMessage", "sendVoice", "sendPhoto", "sendDocument", "getFile"}
        self.assertEqual(tc.ALLOWED_METHODS, want)
        self.assertEqual(tc.API_HOST, "api.telegram.org")

    def test_method_outside_allowlist_refused_before_transport(self):
        t = FakeTransport()
        bad = ("setWebhook", "deleteMessage", "forwardMessage", "banChatMember",
               "sendmessage", "sendMessage/../setWebhook", None, 1, b"sendMessage")
        each(self, bad, lambda m: self.assertEqual(
            client(t).call(m, {"chat_id": CHAT}), tc.Refused("method_not_allowed")))
        self.assertEqual(client(t).call("getFile", {}), tc.Refused("use_get_file"))
        self.assertEqual(t.calls, [])

    def test_chat_id_missing_different_or_lookalike_refused(self):
        t = FakeTransport()
        c = client(t)
        self.assertEqual(c.call("sendMessage", {"text": "x"}), tc.Refused("missing_chat_id"))
        self.assertEqual(send(c, chat_id=124), tc.Refused("chat_id_mismatch"))
        for value in ("123 ", "0123", "123", True, 123.0, *HOSTILE_NUMBERS):
            with self.subTest(value=repr(value)[:40]):
                self.assertIsInstance(send(c, chat_id=value), tc.Refused)
        bad_conf = tc.TelegramClient(lambda: TOKEN, True, transport=t)
        self.assertEqual(send(bad_conf, chat_id=1), tc.Refused("bad_configured_chat_id"))
        self.assertEqual(t.calls, [])

    def test_bad_token_refused_without_transport(self):
        t = FakeTransport()

        def boom():
            raise RecursionError

        getters = [lambda v=v: v for v in (None, "", "   ", TOKEN + "\n", "\n" + TOKEN,
                                           TOKEN + " ", 123456, "123456:short", SECRET)]
        each(self, [*getters, boom, None, 5], lambda g: self.assertEqual(
            send(tc.TelegramClient(g, CHAT, transport=t)), tc.Refused("token_unavailable")))
        self.assertEqual(t.calls, [])

    def test_token_read_at_call_time_and_never_kept(self):
        seen = []
        c = tc.TelegramClient(lambda: seen.append(1) or TOKEN, CHAT,
                              transport=FakeTransport(ok({}), http_error(500)))
        self.assertEqual(seen, [])
        results = [send(c), send(c)]
        self.assertEqual(len(seen), 2)
        self.assertEqual(results[1], tc.Failed("http_status", 500))
        for name in tc.TelegramClient.__slots__:
            self.assertNotEqual(getattr(c, name), TOKEN, name)
        for text in (repr(c), str(c), repr(results)):
            self.assertNotIn(SECRET, text)

    def test_redirect_fails_and_location_never_requested(self):
        evil = "https://evil.example/steal"
        t = FakeTransport(http_error(302, location=evil))
        self.assertEqual(send(client(t)), tc.Failed("http_status", 302))
        self.assertEqual(len(t.calls), 1)
        self.assertNotIn("evil", t.calls[0][0].full_url)
        moved = FakeResponse(b"{}", status=301, headers={"Location": evil})
        self.assertEqual(send(client(FakeTransport(moved))), tc.Failed("http_status", 301))
        self.assertEqual(send(client(FakeTransport(http_error(500)))), tc.Failed("http_status", 500))

    def test_http_429_returns_capped_wait_and_never_retries(self):
        cases = ((5, 5), (0, 0), (10**6, 60), (-1, 60), (True, 60), ("5", 60), (1.5, 60))
        for wait, want in cases:
            raw = json.dumps({"ok": False, "parameters": {"retry_after": wait}}).encode()
            t = FakeTransport(http_error(429, raw))
            with self.subTest(wait=wait):
                result = send(client(t, max_retry_after=60))
                self.assertEqual(result, tc.Failed("http_status", 429, retry_after=want))
                self.assertEqual(len(t.calls), 1)
        for raw in (b"", b"not json", deep_json().encode(), b'{"parameters": 1}', b"x" * 200):
            t = FakeTransport(http_error(429, raw))
            with self.subTest(raw=raw[:20]):
                result = send(client(t, max_retry_after=60, max_response_bytes=100))
                self.assertEqual(result.retry_after, 60)

    def test_bad_responses_are_typed_failures(self):
        bodies = [b"not json", b"\xff\xfe", b'{"ok": false, "description": "x"}', b'{"ok": true}',
                  b'{"ok": 1, "result": {}}', b'{"ok": true, "result": []}', b"[]",
                  b'{"ok": true, "result": {}, "ok": true}', deep_json().encode(),
                  *(text.encode() for text in HOSTILE_JSON)]
        each(self, bodies, lambda raw: self.assertEqual(
            send(client(FakeTransport(FakeResponse(raw)))), tc.Failed("bad_response")))

        class StrRead(FakeResponse):
            def read(self, n=-1):
                return "text"

        odd = (FakeResponse(b"{}", status="200"), FakeResponse(b"{}", status=None), StrRead(b""))
        each(self, odd, lambda r: self.assertEqual(
            send(client(FakeTransport(r))), tc.Failed("bad_response")))

    def test_response_over_cap_is_too_large_and_reading_stops(self):
        big, endless = FakeResponse(b"x" * 101), Endless()
        declared = FakeResponse(b"{}", headers={"Content-Length": "999"})
        for response in (big, endless, declared):
            result = send(client(FakeTransport(response), max_response_bytes=100))
            self.assertEqual(result, tc.Failed("too_large"))
            self.assertLessEqual(response.served, 101)
            self.assertTrue(response.closed)
        self.assertEqual(declared.served, 0)

    def test_transport_errors_are_redacted(self):
        leaks = (f"POST {TOKEN_URL} failed", f"bad token {TOKEN}", f"secret {SECRET} x",
                 f"via http://user:pw@proxy.invalid:8080/ to {TOKEN_URL}?a=1")
        for text in leaks:
            for err in (OSError(text), ValueError(text), urllib.error.URLError(text),
                        RecursionError(text), urllib.error.URLError(OSError(text))):
                with self.subTest(err=type(err).__name__, text=text[:12]):
                    result = send(client(FakeTransport(err)))
                    self.assertEqual(result.kind, "network")
                    for leak in (TOKEN, SECRET, "api.telegram.org", "://", "pw@"):
                        self.assertNotIn(leak, repr(result))

    def test_timeouts_are_typed(self):
        class SlowRead(FakeResponse):
            def read(self, n=-1):
                raise TimeoutError(TOKEN_URL)

        errors = (TimeoutError(TOKEN), socket.timeout("t"), urllib.error.URLError(TimeoutError()))
        each(self, errors, lambda err: self.assertEqual(
            send(client(FakeTransport(err))), tc.Failed("timeout")))
        self.assertEqual(send(client(FakeTransport(SlowRead()))), tc.Failed("timeout"))

    def test_bad_config_refused_before_transport(self):
        t = FakeTransport()
        cases = ({"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")},
                 {"timeout": float("inf")}, {"timeout": True}, {"timeout": "5"},
                 {"max_response_bytes": 0}, {"max_download_bytes": 1.5},
                 {"max_retry_after": -1}, {"max_retry_after": None}, {"transport": 5})
        each(self, cases, lambda kw: self.assertIsInstance(
            send(tc.TelegramClient(lambda: TOKEN, CHAT, **{"transport": t, **kw})), tc.Refused))
        self.assertEqual(t.calls, [])

    def test_bad_bodies_refused_no_exception(self):
        class Hostile(Mapping):
            def __getitem__(self, key):
                raise RecursionError

            def __iter__(self):
                return iter(["chat_id"])

            def __len__(self):
                return 1

        deep = []
        for _ in range(5000):
            deep = [deep]
        cyclic = {"chat_id": CHAT}
        cyclic["self"] = cyclic
        t = FakeTransport()
        bodies = ("text", [("chat_id", CHAT)], {"chat_id": CHAT, 1: "x"}, cyclic,
                  {"chat_id": CHAT, "x": float("nan")}, {"chat_id": CHAT, "x": object()},
                  )
        each(self, bodies, lambda b: self.assertEqual(
            client(t).call("sendMessage", b), tc.Refused("bad_body")))
        for x in (deep, 10**5000):  # outside each(): repr() of these raises
            body = {"chat_id": CHAT, "x": x}
            self.assertEqual(client(t).call("sendMessage", body), tc.Refused("bad_body"))
        self.assertEqual(client(t).call("sendMessage", Hostile()), tc.Refused("invalid_input"))
        self.assertEqual(t.calls, [])


def plan(size=10):
    return plan_download({"file_id": "x", "file_size": size, "mime_type": "image/png"})


def issue(path="photos/file_1.png", size=10, *more, getter=lambda: TOKEN):
    t = FakeTransport(ok({"file_id": "x", "file_size": size, "file_path": path}), *more)
    c = tc.TelegramClient(getter, CHAT, transport=t, max_download_bytes=100)
    return c, t, c.get_file(plan(size))


class ClientDownloadTests(unittest.TestCase):
    def test_download_happy_path_streams_to_sink_once(self):
        c, t, got = issue("photos/file_1.png", 10, FakeResponse(b"0123456789"))
        self.assertEqual(got, tc.Ok("photos/file_1.png"))
        sink = Sink()
        self.assertEqual(c.download("photos/file_1.png", sink), tc.Ok(10))
        self.assertEqual((bytes(sink.data), sink.discarded), (b"0123456789", False))
        request = t.calls[1][0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.hostname, url.port), ("https", tc.API_HOST, None))
        self.assertTrue(url.path.startswith("/file/bot"))
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(c.download("photos/file_1.png", Sink()), tc.Refused("file_path_not_issued"))
        self.assertEqual(len(t.calls), 2)

    def test_get_file_path_partition_over_hostile_parts(self):
        harmless = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "con.txt", "NUL.png", "DATA"}
        for part in HOSTILE_PATH_PARTS:
            want = tc.Ok(part) if part in harmless else tc.Refused("bad_file_path")
            with self.subTest(part=part):
                self.assertEqual(issue(part)[2], want)

    def test_bad_file_paths_refused_at_get_file_and_download(self):
        bad = ("../x", "a/../b", "https://evil.example/x", "//evil.example/x", "evil.example/x",
               "x?a=1", "x#f", "a\\b", "file:x", "%2e%2e/x", "", "a//b", "/x", "x/", "a" * 257,
               None, 5, b"x")
        each(self, bad[:-1], lambda p: self.assertEqual(  # bytes cannot be in a JSON answer
            issue(p)[2], tc.Refused("bad_file_path")))
        t = FakeTransport()
        each(self, bad, lambda p: self.assertEqual(
            client(t).download(p, Sink()), tc.Refused("bad_file_path")))
        self.assertEqual(t.calls, [])

    def test_bad_plan_or_plan_over_cap_refused_before_transport(self):
        t = FakeTransport()
        c = client(t, max_download_bytes=100)
        exe = tc.DownloadPlan("x", "exe", ".exe", 10, False, None, "application/x-msdownload", None)
        bad = (None, {}, {"file_id": "x"}, plan_download({}), "x", exe)  # exe: hand-built plan
        each(self, bad, lambda p: self.assertEqual(c.get_file(p), tc.Refused("bad_plan")))
        self.assertEqual(c.get_file(plan(101)), tc.Refused("file_over_cap"))
        self.assertEqual(t.calls, [])

    def test_bad_declared_size_is_bad_response(self):
        for size in (0, -1, 11, True, "10", 1.5, None):
            t = FakeTransport(ok({"file_path": "a", "file_size": size}))
            with self.subTest(size=size):
                c = client(t, max_download_bytes=100)
                self.assertEqual(c.get_file(plan(10)), tc.Failed("bad_response"))
                self.assertEqual(c.download("a", Sink()), tc.Refused("file_path_not_issued"))

    def test_download_over_declared_or_cap_stops_and_discards(self):
        bigger, endless = FakeResponse(b"x" * 50), Endless()
        declared = FakeResponse(b"x" * 10, headers={"Content-Length": "999"})
        for response in (bigger, endless, declared):
            c, _t, _got = issue("a", 10, response)
            sink = Sink()
            self.assertEqual(c.download("a", sink), tc.Failed("too_large"))
            self.assertTrue(sink.discarded)
            self.assertLessEqual(response.served, 11)
            self.assertTrue(response.closed)
        self.assertEqual(declared.served, 0)

    def test_short_download_failed_sink_and_http_errors_discard(self):
        class BadStatus(FakeResponse):
            @property
            def status(self):
                raise ValueError("hostile")

            @status.setter
            def status(self, _value):
                pass

        cases = ((FakeResponse(b"x" * 5), Sink(), tc.Failed("bad_response")),
                 (BadStatus(b"x" * 10), Sink(), tc.Failed("bad_response")),
                 (http_error(404), Sink(), tc.Failed("http_status", 404)),
                 (http_error(302, location="https://evil.example/"), Sink(),
                  tc.Failed("http_status", 302)))
        for response, sink, want in cases:
            c = issue("a", 10, response)[0]
            self.assertEqual(c.download("a", sink), want)
            self.assertTrue(sink.discarded)
        c, sink = issue("a", 10, FakeResponse(b"x" * 10))[0], Sink(fail=True)
        self.assertEqual(c.download("a", sink).kind, "network")
        self.assertTrue(sink.discarded)

    def test_bad_sink_and_missing_token_make_no_transport_call(self):
        class WriteOnly:
            def write(self, chunk):
                pass

        c, t, _got = issue("a")
        each(self, (None, object(), WriteOnly()), lambda s: self.assertEqual(
            c.download("a", s), tc.Refused("bad_sink")))
        tokens = iter([TOKEN, None])
        c, t, _got = issue("a", getter=lambda: next(tokens))
        self.assertEqual(c.download("a", Sink()), tc.Refused("token_unavailable"))
        self.assertEqual(len(t.calls), 1)

    def test_issued_paths_are_bounded(self):
        answers = [ok({"file_path": f"p{i}"}) for i in range(tc.MAX_ISSUED + 1)]
        c = client(FakeTransport(*answers), max_download_bytes=100)
        for i in range(tc.MAX_ISSUED + 1):
            self.assertEqual(c.get_file(plan()), tc.Ok(f"p{i}"))
        self.assertEqual(c.download("p0", Sink()), tc.Refused("file_path_not_issued"))


class ClientSafetyTests(unittest.TestCase):
    def test_default_opener_ignores_proxy_env_and_has_no_redirect(self):
        env = {"HTTPS_PROXY": "http://proxy.invalid:1", "ALL_PROXY": "http://proxy.invalid:2",
               "HTTP_PROXY": "http://proxy.invalid:3"}
        with mock.patch.dict(os.environ, env):
            opener = tc.default_opener()
        proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        # ProxyHandler({}) has no *_open method, so the opener does not even register it.
        self.assertFalse(any(h.proxies for h in proxies), "a proxy handler with proxies")
        self.assertFalse(any(isinstance(h, urllib.request.HTTPRedirectHandler)
                             for h in opener.handlers))
        self.assertEqual(set(opener.handle_open), {"https"})
        self.assertEqual(set(opener.handle_error.get("http", {})), {"default"})
        https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        self.assertEqual(https[0]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https[0]._context.check_hostname)

    def test_default_transport_hits_patched_socket_and_fails_typed(self):
        self.assertIs(socket.create_connection, _no_network)
        with self.assertRaises(OSError):
            socket.socket()
        result = send(tc.TelegramClient(lambda: TOKEN, CHAT, timeout=1))
        self.assertEqual(result.kind, "network")
        for leak in (TOKEN, SECRET, "api.telegram.org"):
            self.assertNotIn(leak, repr(result))

    def test_static_no_banned_imports_no_open_no_token(self):
        source = (ROOT / "jarvis" / "telegram_client.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "open", "module writes no file")
        banned = {"requests", "httpx", "aiohttp", "subprocess", "socket", "http"}
        self.assertEqual(names & banned, set())
        self.assertIsNone(re.search(r"\d{5,}:[A-Za-z0-9_-]{30,}", source))

    def test_module_is_imported_from_nowhere(self):
        for folder in ("jarvis", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                if path.name == "telegram_client.py":
                    continue
                with self.subTest(path=path.name):
                    text = path.read_text(encoding="utf-8", errors="replace")
                    self.assertNotIn("telegram_client", text)

    def test_redact_removes_token_and_urls_and_stays_ascii(self):
        class Bad:
            def __str__(self):
                raise RecursionError

        odd = ("8http://user:pw@proxy.invalid/x", ".https://user:pw@h/x",
               "see //user:pw@proxy.invalid:8080/x", "x user:pw@proxy.invalid y")
        for text in (*HOSTILE_TEXT, TOKEN_URL, f"x {TOKEN} y", f"x {SECRET} y", "ftp://a", *odd):
            out = tc.redact(text, TOKEN)
            with self.subTest(text=repr(text)[:40]):
                self.assertLessEqual(len(out), tc.MAX_DETAIL)
                self.assertTrue(all(" " <= ch <= "~" for ch in out))
                for leak in (TOKEN, SECRET, "://", "api.telegram.org", "pw@", "proxy.invalid"):
                    self.assertNotIn(leak, out)
        self.assertNotIn(SECRET, tc.redact(f"x {TOKEN} y"))
        self.assertEqual(tc.redact(Bad()), "[unprintable]")

    def test_logs_hold_no_body_and_no_token(self):
        marker = "BODY-MARKER-7f3a"
        answers = [ok({"message_id": 1}), OSError(f"POST {TOKEN_URL} {marker}"), http_error(500)]
        c = client(FakeTransport(*answers))
        with self.assertLogs("jarvis.telegram_client", "DEBUG") as logs:
            send(c, text=marker)
            send(c, text=HOSTILE_TEXT[0] + marker)
            send(c, text=marker)
            c.call("setWebhook", {"url": TOKEN_URL})
        text = "\n".join(logs.output) + repr([r.args for r in logs.records])
        for leak in (marker, TOKEN, SECRET, "api.telegram.org", "setWebhook"):
            self.assertNotIn(leak, text)
        self.assertTrue(all(r.exc_info is None for r in logs.records))
