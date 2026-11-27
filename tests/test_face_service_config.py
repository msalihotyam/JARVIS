"""FACECONF1: the service settings file loader (talk\\state\\service.json), parsed strictly.

Temp roots only; no network, no model, no real talk\\ folder, no wall clock.
"""
import ast
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from jarvis.face import service_config as sc
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, deep_json, each

MB = 1024 * 1024


class _Root(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)  # long names: an 8.3 TEMP would be refused
        self.path = os.path.join(self.root, *sc.RELATIVE)
        os.makedirs(os.path.dirname(self.path))

    def write(self, data):
        if not isinstance(data, bytes):
            data = (data if isinstance(data, str) else json.dumps(data)).encode("utf-8")
        with open(self.path, "wb") as f:
            f.write(data)
        return sc.load(self.path)

    def refused(self, result, reason=None, field=None):
        self.assertIsInstance(result, sc.Refusal)
        if reason is not None:
            self.assertEqual(result.reason, reason)
        if field is not None:
            self.assertEqual(result.field, field)


class AcceptanceTests(_Root):
    def test_missing_file_or_folder_gives_the_defaults(self):
        self.assertEqual(sc.load(self.path), sc.ServiceConfig())
        self.assertEqual(sc.load(Path(self.path)), sc.ServiceConfig())
        other = os.path.join(self.root, "fresh", *sc.RELATIVE)
        self.assertEqual(sc.load(other), sc.ServiceConfig(48117, "", 25 * MB, False))

    def test_a_full_file_and_partial_files(self):
        full = {"net": {"port": 50000, "remote_bind": "100.101.102.103", "blob_cap_bytes": 100 * MB},
                "telegram": {"enabled": True}}
        self.assertEqual(self.write(full), sc.ServiceConfig(50000, "100.101.102.103", 100 * MB, True))
        self.assertEqual(self.write({}), sc.ServiceConfig())
        self.assertEqual(self.write({"net": {}, "telegram": {}}), sc.ServiceConfig())
        self.assertEqual(self.write({"net": {"port": 1024}}), sc.ServiceConfig(port=1024))
        self.assertEqual(self.write({"net": {"port": 65535, "remote_bind": ""}}), sc.ServiceConfig(port=65535))
        for ip in ("100.64.0.0", "100.64.0.1", "100.127.255.255"):
            self.assertEqual(self.write({"net": {"remote_bind": ip}}), sc.ServiceConfig(remote_bind=ip))

    def test_blob_cap_bounds(self):
        self.assertEqual(self.write({"net": {"blob_cap_bytes": 1}}), sc.ServiceConfig(blob_cap_bytes=1))
        for bad in (0, -1, 100 * MB + 1, 10**400):
            self.refused(self.write({"net": {"blob_cap_bytes": bad}}), "bad_value", "net.blob_cap_bytes")
        for bad in (True, 25.0, "26214400", None, [1]):
            self.refused(self.write({"net": {"blob_cap_bytes": bad}}), "wrong_type", "net.blob_cap_bytes")

    def test_telegram_enabled_is_a_bool_only(self):
        self.assertEqual(self.write({"telegram": {"enabled": False}}), sc.ServiceConfig())
        for bad in (1, 0, "true", None, [True]):
            self.refused(self.write({"telegram": {"enabled": bad}}), "wrong_type", "telegram.enabled")

    def test_loopback_and_other_unknown_keys_are_refused_by_name(self):
        self.refused(self.write({"net": {"loopback": "127.0.0.1"}}), "unknown_key", "net.loopback")
        self.refused(self.write({"net": {"bind": "127.0.0.1"}}), "unknown_key", "net.bind")
        self.refused(self.write({"loopback": True}), "unknown_key", "loopback")
        self.refused(self.write({"telegram": {"chat": 5}}), "unknown_key", "telegram.chat")
        self.refused(self.write({"net": {"port": 50000}, "extra": {}}), "unknown_key", "extra")

    def test_wrong_shapes_name_the_field(self):
        self.refused(self.write([]), "wrong_type", "(root)")
        self.refused(self.write("48117"), "wrong_type", "(root)")
        self.refused(self.write({"net": [48117]}), "wrong_type", "net")
        self.refused(self.write({"telegram": True}), "wrong_type", "telegram")

    def test_duplicate_keys_name_the_field_never_the_value(self):
        result = self.write('{"net": {"port": 50000, "port": 50001}}')
        self.refused(result, "duplicate_key", "net.port")
        self.assertNotIn("5000", repr(result))
        self.refused(self.write('{"net": {}, "net": {}}'), "duplicate_key", "net")
        self.refused(self.write('{"telegram": {"enabled": true, "enabled": false}}'), "duplicate_key",
                     "telegram.enabled")

    def test_secret_looking_keys_are_refused_anywhere(self):
        for name in ("token", "bot_token", "telegramToken", "api_key", "KEY", "Secret", "client-secret",
                     "password", "PassWord", "passwd", "credentials", "t_o_k_e_n"):
            for doc in ({"telegram": {name: "s3cr3t-v4lue"}}, {name: "s3cr3t-v4lue"},
                        {"net": {"x": [{name: "s3cr3t-v4lue"}]}}):
                with self.subTest(name=name, doc=doc):
                    result = self.write(doc)
                    self.refused(result, "secret_in_config")
                    self.assertNotIn("s3cr3t", repr(result))
        self.refused(self.write({"telegram": {"token": "x"}}), "secret_in_config", "telegram.token")

    def test_refusals_never_carry_the_value(self):
        for doc in ({"net": {"port": 99999}}, {"net": {"remote_bind": "8.8.4.4"}},
                    {"net": {"remote_bind": "zz-host-zz"}}, {"telegram": {"enabled": "zz-yes-zz"}}):
            with self.subTest(doc=doc):
                result = self.write(doc)
                self.refused(result)
                self.assertNotIn(str(list(doc.values())[0].popitem()[1]), repr(result))

    def test_values_are_frozen(self):
        config, refusal = sc.ServiceConfig(), sc.Refusal("x", "net.port")
        with self.assertRaises(FrozenInstanceError):
            config.port = 1
        with self.assertRaises(FrozenInstanceError):
            refusal.reason = "y"

    def test_the_corpus_hostile_json_is_refused(self):
        each(self, HOSTILE_JSON, lambda text: self.refused(self.write(text)))


class NegativeTests(_Root):
    def test_remote_bind_matrix_is_refused(self):
        bad = ("0.0.0.0", "::",  # nosec B104 - addresses the loader must refuse, never bound
"8.8.8.8", "192.168.1.10", "10.0.0.5", "172.16.0.1", "127.0.0.1",
               "100.63.255.255", "100.128.0.0", "100.064.0.1", " 100.64.0.1", "100.64.0.1/32",
               "jarvis.local", "localhost", "fd7a:115c:a1e0::1", "::ffff:100.64.0.1", "::1")
        each(self, bad, lambda ip: self.refused(self.write({"net": {"remote_bind": ip}}), "bad_value",
                                                 "net.remote_bind"))
        for wrong in (["100.64.0.1"], {"ip": "100.64.0.1"}, None, 1684275201, True):
            self.refused(self.write({"net": {"remote_bind": wrong}}), "wrong_type", "net.remote_bind")

    def test_port_matrix_is_refused(self):
        for port in (0, 80, 1023, 65536, -1, 10**400):
            self.refused(self.write({"net": {"port": port}}), "bad_value", "net.port")
        for port in ("48117", True, False, 48117.0, None, [48117]):
            self.refused(self.write({"net": {"port": port}}), "wrong_type", "net.port")
        self.refused(self.write('{"net": {"port": NaN}}'), "malformed")
        each(self, HOSTILE_NUMBERS, lambda n: self.refused(self.write({"net": {"port": n}})))

    def test_deep_json_is_refused_without_an_exception(self):
        for text in (deep_json(), deep_json(30000), '{"net":' * 10000 + "1" + "}" * 10000):
            self.refused(self.write(text))

    def test_huge_files_are_refused_before_reading(self):
        with open(self.path, "wb"):
            pass
        os.truncate(self.path, 50 * MB)  # sparse: nothing real is written
        self.refused(sc.load(self.path), "too_large")
        self.refused(self.write(b" " * (64 * 1024 + 1)), "too_large")
        self.refused(sc.parse(b" " * (64 * 1024 + 1)), "too_large")
        self.assertEqual(self.write(b" " * (64 * 1024 - 2) + b"{}"), sc.ServiceConfig())

    def test_invalid_utf8_and_nul_are_refused(self):
        for raw in (b"\xff\xfe{\x00}\x00", b'{"net": {"remote_bind": "\xc3"}}', b"\x80", b"{\xed\xa0\x80}"):
            self.refused(self.write(raw), "not_utf8")
        for raw in (b'{"net": {"port": 48117}}\x00', b"\x00", b'{"net\x00": {}}', b"\xef\xbb\xbf{}", b""):
            self.refused(self.write(raw))

    def test_unreadable_file_is_a_refusal_not_the_defaults(self):
        os.mkdir(self.path)  # a folder where the file should be
        self.refused(sc.load(self.path), "unreadable")

    def test_path_tricks_are_refused(self):
        tail = "\\talk\\state\\service.json"

        def check(part):
            self.refused(sc.load(self.root + "\\" + part + tail), "bad_path")
            self.refused(sc.load(part), "bad_path")
        each(self, HOSTILE_PATH_PARTS, check)
        for path in (None, 5, b"C:\\x\\talk\\state\\service.json", "talk\\state\\service.json",
                     "C:talk\\state\\service.json", self.root + "\\talk\\state\\other.json",
                     self.root + "\\talk\\service.json", self.root + "/talk/state/service.json",
                     self.root + tail + "\x00", self.root + "\\data" + tail, "C:\\" + "a\\" * 200 + tail[1:]):
            self.refused(sc.load(path), "bad_path")


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(sc.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_network_import_and_no_environment_read(self):
        modules = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                modules |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)
        self.assertEqual(modules, {"__future__", "ipaddress", "json", "os", "unicodedata", "dataclasses"})
        names = {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        for banned in ("environ", "getenv", "putenv", "environb", "socket", "urlopen", "__import__",
                       "import_module", "system", "popen"):
            self.assertNotIn(banned, names)
        self.assertNotIn("def heartbeat", self.source)
        self.assertNotIn("project_folders", self.source)


if __name__ == "__main__":
    unittest.main()
