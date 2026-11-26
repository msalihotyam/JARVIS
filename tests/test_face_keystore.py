"""FACEKEY1: the remote window's device key and server address on the client PC.

The protect functions are injected fakes; the real LOCALAPPDATA is never touched (every store gets a
temporary base folder). Only DpapiTests calls the real DPAPI, in a temporary folder, on Windows.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import pickle  # nosec B403 - only to prove the key wrapper refuses pickling
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.face import keystore
from jarvis.face.keystore import DeviceKey, KeyStore, Refusal
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, deep_json, each

SOURCE = Path(keystore.__file__).read_text(encoding="utf-8")
KEY = "k3y-SECRET_value-0123456789abcdef"
# Plain folder names in the corpus (case variants, look-alike letters): legal under LOCALAPPDATA.
PLAIN_NAMES = {".Claude", "DATA", ".Jarvis", ".clаude", "gеt", "dоcs"}


def fake_protect(data: bytes) -> bytes:
    body = bytes(b ^ 0x5A for b in data)
    return b"FK1" + hashlib.sha256(body).digest()[:8] + body


def fake_unprotect(blob: bytes) -> bytes:
    if blob[:3] != b"FK1" or hashlib.sha256(blob[11:]).digest()[:8] != blob[3:11]:
        raise ValueError("bad blob")
    return bytes(b ^ 0x5A for b in blob[11:])


def temp_root(case: unittest.TestCase) -> str:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    return os.path.realpath(tmp.name)  # long form: an 8.3 TEMP would be refused as "~1"


def store_in(case: unittest.TestCase, protect=fake_protect, unprotect=fake_unprotect):
    root = temp_root(case)
    return root, os.path.join(root, "JARVIS", "face"), KeyStore(root, protect, unprotect)


def put(folder: str, name: str, data: bytes) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


class KeyTests(unittest.TestCase):
    def test_round_trip_stores_only_the_protected_blob_and_writes_atomically(self) -> None:
        _, folder, store = store_in(self)
        self.assertIsNone(store.save(KEY))
        loaded = store.load()
        self.assertIsInstance(loaded, DeviceKey)
        self.assertEqual(loaded.reveal(), KEY)
        blob = Path(folder, "device.key").read_bytes()
        self.assertNotIn(KEY.encode(), blob)
        self.assertEqual(os.listdir(folder), ["device.key"])  # no temp file left behind
        with mock.patch.object(keystore.os, "replace", side_effect=OSError("disk full")):
            self.assertIsInstance(store.save("another-key-0123456789"), Refusal)
        self.assertEqual(os.listdir(folder), ["device.key"])
        self.assertEqual(Path(folder, "device.key").read_bytes(), blob)  # old file intact
        self.assertIsNone(store.clear())
        self.assertEqual(os.listdir(folder), [])
        self.assertIsInstance(store.load(), Refusal)

    def test_a_protect_that_fails_or_leaks_the_key_stores_nothing(self) -> None:
        def raises(data: bytes) -> bytes:
            raise RuntimeError(f"cannot protect {data!r}")
        for protect in (lambda data: data, lambda data: b"x" + data + b"y", lambda data: data.decode(),
                        lambda data: b"", lambda data: bytearray(b"blob"), raises):
            _, folder, store = store_in(self, protect=protect)
            result = store.save(KEY)
            self.assertIsInstance(result, Refusal)
            self.assertNotIn(KEY, repr(result))
            self.assertFalse(os.path.exists(folder) and os.listdir(folder))

    def test_a_tampered_or_truncated_file_is_refused_without_an_exception(self) -> None:
        _, folder, store = store_in(self)
        store.save(KEY)
        blob = Path(folder, "device.key").read_bytes()
        flipped = [blob[:i] + bytes([blob[i] ^ 1]) + blob[i + 1:] for i in (0, 5, len(blob) - 1)]
        for data in [b"", b"F", blob[:3], blob[:11], blob[:-1], blob + b"\0", b"\xff" * 64, *flipped]:
            with self.subTest(data=data[:16]):
                put(folder, "device.key", data)
                self.assertIsInstance(store.load(), Refusal)

    def test_the_key_never_appears_in_repr_refusals_or_pickles(self) -> None:
        def leaky(blob: bytes) -> bytes:
            raise ValueError(f"wrong user for {KEY}")
        root, folder, store = store_in(self)
        store.save(KEY)
        loaded = store.load()
        for text in (repr(loaded), str(loaded), f"{loaded}", repr(store), repr([loaded])):
            self.assertNotIn(KEY, text)
        self.assertRaises(TypeError, pickle.dumps, loaded)
        self.assertRaises(AttributeError, setattr, loaded, "_value", "x")
        for unprotect in (leaky, lambda blob: KEY, lambda blob: (KEY + " ").encode()):
            refusal = KeyStore(root, fake_protect, unprotect).load()
            self.assertIsInstance(refusal, Refusal)
            self.assertNotIn(KEY, repr(refusal))
        self.assertNotIn("print(", SOURCE)
        self.assertNotIn("logging", SOURCE)

    def test_wrong_types_a_str_subclass_and_bad_lengths_are_refused(self) -> None:
        class Sub(str):
            pass
        _, folder, store = store_in(self)
        bad = [KEY.encode(), bytearray(KEY.encode()), Sub(KEY), None, ["k"], "short", "a" * 513, KEY + " ",
               KEY + "ı", KEY + "\0", KEY.replace("-", "/"), *HOSTILE_NUMBERS]
        each(self, bad, lambda key: self.assertIsInstance(store.save(key), Refusal))
        self.assertFalse(os.path.exists(folder))

    def test_a_50_mb_file_is_refused_by_size_before_it_is_read(self) -> None:
        calls = []
        _, folder, store = store_in(self, unprotect=lambda blob: calls.append(len(blob)) or b"")
        for name, load in (("device.key", store.load), ("server.json", store.load_address)):
            path = put(folder, name, b"")
            os.truncate(path, 50 * 1024 * 1024)  # sparse; nothing is written
            with mock.patch.object(keystore.os, "fstat", wraps=os.fstat) as fstat:
                self.assertIsInstance(load(), Refusal)
            fstat.assert_called()
            os.unlink(path)
        self.assertEqual(calls, [])


class PathTests(unittest.TestCase):
    def assert_refused_everywhere(self, base: str, root: object) -> None:
        store = KeyStore(root, fake_protect, fake_unprotect)
        for result in (store.save(KEY), store.load(), store.save_address("127.0.0.1", 8765),
                       store.load_address(), store.clear()):
            self.assertIsInstance(result, Refusal)
        self.assertEqual(os.listdir(base), [])  # nothing created anywhere under the base

    def test_the_hostile_path_corpus_under_the_base_is_refused(self) -> None:
        base = temp_root(self)
        parts = [p for p in HOSTILE_PATH_PARTS if p not in PLAIN_NAMES]
        # concatenated, never os.path.join: an absolute part must not become the root itself
        each(self, parts, lambda part: self.assert_refused_everywhere(base, base + "\\" + part))

    def test_unc_ads_short_names_reserved_names_and_wrong_types_are_refused(self) -> None:
        class Sub(str):
            pass
        base = temp_root(self)
        roots = ["\\\\host\\share", "\\\\host\\share\\Local", "\\\\?\\" + base, "\\\\.\\" + base, "JARVIS",
                 "C:x", base + ":ads", base + "\\x::$DATA", base + "\\PROGRA~1", base + "\\COM\u00b9",
                 base + "\\lpt1.log", base + "\\CONOUT$", base + "\\a\x07b", base + "\\", base + "\\\\a",
                 base + "/a", "\u0130:\\x", None, b"C:\\x", Path(base), Sub(base), base + "\\" + "a" * 240]
        each(self, roots, lambda root: self.assert_refused_everywhere(base, root))

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows only")
    def test_a_junction_that_leads_outside_the_base_is_refused(self) -> None:
        import _winapi
        tmp = temp_root(self)
        base, elsewhere = os.path.join(tmp, "base"), os.path.join(tmp, "elsewhere")
        os.makedirs(base)
        os.makedirs(elsewhere)
        link = os.path.join(base, "JARVIS")
        _winapi.CreateJunction(elsewhere, link)
        try:
            store = KeyStore(base, fake_protect, fake_unprotect)
            put(os.path.join(elsewhere, "face"), "server.json", b'{"ip": "127.0.0.1", "port": 8765}')
            for result in (store.save(KEY), store.load(), store.load_address(), store.clear()):
                self.assertIsInstance(result, Refusal)
            self.assertEqual(os.listdir(os.path.join(elsewhere, "face")), ["server.json"])
        finally:
            os.rmdir(link)  # removes the junction only, never its target

    def test_default_refuses_a_bad_localappdata_before_touching_it(self) -> None:
        for environ in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "\\\\host\\share"},
                        {"LOCALAPPDATA": "relative"}, {"LOCALAPPDATA": "C:x"}):
            with mock.patch.object(keystore.os.path, "realpath", side_effect=AssertionError("touched")):
                self.assertIsInstance(keystore.default(environ), Refusal)
        with mock.patch.object(keystore.sys, "platform", "linux"):
            self.assertIsInstance(keystore.default({"LOCALAPPDATA": temp_root(self)}), Refusal)


class AddressTests(unittest.TestCase):
    def test_the_address_round_trips_as_plain_json(self) -> None:
        _, folder, store = store_in(self)
        for ip, port in (("127.0.0.1", 8765), ("100.64.0.1", 1024), ("100.127.255.254", 65535)):
            self.assertIsNone(store.save_address(ip, port))
            self.assertEqual(store.load_address(), (ip, port))
            self.assertEqual(json.loads(Path(folder, "server.json").read_text("ascii")), {"ip": ip, "port": port})
        self.assertEqual(sorted(os.listdir(folder)), ["server.json"])

    def test_host_names_public_ips_ipv6_and_bad_ports_are_refused_on_save_and_on_load(self) -> None:
        class Sub(str):
            pass
        _, folder, store = store_in(self)
        bad = [("localhost", 8765), ("example.com", 8765), ("8.8.8.8", 8765), ("192.168.1.2", 8765),
               ("10.0.0.1", 8765), ("100.128.0.1", 8765), ("100.63.255.255", 8765), ("::1", 8765),
               ("::ffff:127.0.0.1", 8765), ("127.1", 8765), ("127.0.0.01", 8765), ("127.0.0.1 ", 8765),
               ("127.0.0.1", 1023), ("127.0.0.1", 65536), ("127.0.0.1", 0), ("127.0.0.1", -1),
               ("127.0.0.1", "8765"), ("127.0.0.1", 8765.0), (b"127.0.0.1", 8765), (Sub("127.0.0.1"), 8765),
               *(("127.0.0.1", n) for n in HOSTILE_NUMBERS)]

        def check(pair) -> None:
            self.assertIsInstance(store.save_address(*pair), Refusal)
            if type(pair[0]) is not str:
                return  # bytes or a str subclass cannot reach the file as such (JSON has plain strings)
            put(folder, "server.json", json.dumps({"ip": pair[0], "port": pair[1]}).encode("utf-8"))
            self.assertIsInstance(store.load_address(), Refusal)
        each(self, bad, check)

    def test_hostile_json_in_the_address_file_is_refused(self) -> None:
        _, folder, store = store_in(self)
        texts = [*HOSTILE_JSON, deep_json(), "[]", '"127.0.0.1"', '{"ip": "127.0.0.1"}',
                 '{"ip": "127.0.0.1", "port": 8765, "x": 1}', '{"ip": "127.0.0.1", "ip": "127.0.0.1", "port": 8765}',
                 '{"ip": "127.0.0.1", "port": true}', '{"ip": "127.0.0.1", "port": 1e400}',
                 '\ufeff{"ip": "127.0.0.1", "port": 8765}', '{"ip": "127.0.0.1", "port": 8765}' + " " * 5000]
        each(self, texts, lambda text: (put(folder, "server.json", text.encode("utf-8")),
                                        self.assertIsInstance(store.load_address(), Refusal)))
        put(folder, "server.json", b'{"ip": "127.0.0.1", "port": \xff}')
        self.assertIsInstance(store.load_address(), Refusal)


class StaticTests(unittest.TestCase):
    TREE = ast.parse(SOURCE)

    def test_no_network_or_process_import_and_only_the_stdlib_it_needs(self) -> None:
        modules = set()
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Import):
                modules |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        self.assertEqual(modules, {"__future__", "ipaddress", "json", "os", "sys", "tempfile", "dataclasses",
                                   "ctypes"})
        for name in ("socket", "ssl", "http", "urllib", "requests", "subprocess", "asyncio", "select"):
            self.assertNotIn(name, modules)
        self.assertNotIn("def " + "heartbeat", SOURCE)
        self.assertNotIn("project_" + "folders", SOURCE)

    def test_ctypes_is_touched_only_inside_default(self) -> None:
        default = [n for n in self.TREE.body if isinstance(n, ast.FunctionDef) and n.name == "default"]
        self.assertEqual(len(default), 1)
        inside = {id(n) for n in ast.walk(default[0])}
        uses = [n for n in ast.walk(self.TREE)
                if (isinstance(n, ast.Name) and n.id == "ctypes")
                or (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "ctypes" for a in n.names))
                or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "ctypes")]
        self.assertTrue(uses)
        self.assertTrue(all(id(n) in inside for n in uses))


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows only")
class DpapiTests(unittest.TestCase):
    def test_real_dpapi_round_trip_in_a_temp_folder(self) -> None:
        root = temp_root(self)
        store = keystore.default({"LOCALAPPDATA": root})  # never the real LOCALAPPDATA
        self.assertIsInstance(store, KeyStore)
        self.assertIsNone(store.save(KEY))
        path = Path(root, "JARVIS", "face", "device.key")
        blob = path.read_bytes()
        self.assertNotIn(KEY.encode(), blob)
        self.assertEqual(store.load().reveal(), KEY)
        middle = len(blob) // 2
        path.write_bytes(blob[:middle] + bytes([blob[middle] ^ 0xFF]) + blob[middle + 1:])
        self.assertIsInstance(store.load(), Refusal)
        path.write_bytes(blob[:-8])
        self.assertIsInstance(store.load(), Refusal)
        self.assertIsNone(store.clear())
        self.assertEqual(os.listdir(path.parent), [])
