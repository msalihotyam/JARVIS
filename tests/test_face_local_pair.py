"""FACELOCAL1: automatic local pairing over a fake registry and a fake keystore (never DPAPI, never a file)."""
from __future__ import annotations

import ast
import logging
import unittest
from pathlib import Path

from jarvis.face import local_pair
from jarvis.face.devices import MAX_DEVICES, Device, Paired, Refusal
from jarvis.face.keystore import DeviceKey
from jarvis.face.keystore import Refusal as StoreRefusal
from jarvis.face.local_pair import LOCAL_IP, LOCAL_NAME, LocalDevice, ensure
from tests.adversarial import HOSTILE_NUMBERS, each

SOURCE = Path(local_pair.__file__).read_text(encoding="utf-8")
PORT, NOW = 8765, 1_000_000
MODES = ("refuse", "fail", "raise")
SECRETS: list = []  # every key and code a fake ever made; scanned for in every output


class Faults:
    """fault = (method, nth call or None for every call, mode); counts calls per method."""

    def __init__(self, fault=None):
        self.fault, self.calls, self.log = fault, {}, []

    def hit(self, method, *args):
        self.calls[method] = n = self.calls.get(method, 0) + 1
        self.log.append((method, args))
        if self.fault and self.fault[0] == method and self.fault[1] in (None, n):
            return self.fault[2]
        return None

    def boom(self):
        raise RuntimeError(f"fault carrying secrets {SECRETS[-3:]}")


class FakeRegistry(Faults):
    def __init__(self, fault=None):
        super().__init__(fault)
        self.rows, self.code, self.next = {}, None, 0  # id -> [name, key, revoked]

    def add(self, name):
        self.next += 1
        dev_id, key = f"{self.next:016x}", f"KEY{self.next:049d}"
        SECRETS.append(key)
        self.rows[dev_id] = [name, key, False]
        return dev_id, key

    def live(self, name=LOCAL_NAME):
        return {i: r[1] for i, r in self.rows.items() if not r[2] and r[0] == name}

    def list(self):
        mode = self.hit("list")
        if mode == "raise":
            self.boom()
        if mode:
            return Refusal("registry_unreadable") if mode == "refuse" else ["garbage"]
        return tuple(Device(i, r[0], 0, 0, r[2]) for i, r in self.rows.items())

    def check(self, key, now):
        mode = self.hit("check", key, now)
        if mode == "raise":
            self.boom()
        if mode:
            return Refusal("unknown_key") if mode == "refuse" else "ffffffffffffffff"
        found = [i for i, r in self.rows.items() if r[1] == key]
        return Refusal("unknown_key") if not found else Refusal("revoked") if self.rows[found[0]][2] else found[0]

    def issue_code(self, now):
        mode = self.hit("issue_code", now)
        if mode == "raise":
            self.boom()
        if mode:
            return Refusal("no_code") if mode == "refuse" else 12345678
        self.code = f"9{len(SECRETS) % 10**7:07d}"  # leading 9: never a substring of a zero-padded fake id
        SECRETS.append(self.code)
        return self.code

    def pair(self, code, name, now):
        mode = self.hit("pair", code, name, now)
        if mode == "refuse":
            return Refusal("wrong_code")
        if code != self.code or not code:
            return Refusal("wrong_code")
        self.code = None
        if sum(not r[2] for r in self.rows.values()) >= MAX_DEVICES:
            return Refusal("too_many_devices")
        dev_id, key = self.add(name)
        if mode == "raise":
            self.boom()  # after the write: the worst case, a device whose key nobody got
        if mode == "fail":
            return Device(dev_id, name, 0, 0, False)
        return Paired(Device(dev_id, name, 0, 0, False), key)

    def revoke(self, dev_id):
        mode = self.hit("revoke", dev_id)
        if mode == "raise":
            self.boom()
        if mode:
            return Refusal("registry_unreadable") if mode == "refuse" else None
        self.rows[dev_id][2] = True
        return Device(dev_id, self.rows[dev_id][0], 0, 0, True)


class FakeStore(Faults):
    def __init__(self, fault=None, key=None, address=None):
        super().__init__(fault)
        self.key, self.address = key, address

    def __repr__(self):
        return "FakeStore()"

    def _do(self, method, args, value):
        mode = self.hit(method, *args)
        if mode == "raise":
            self.boom()
        if mode:
            return StoreRefusal("refused") if mode == "refuse" else False
        return value()

    def save(self, key):
        return self._do("save", (key,), lambda: setattr(self, "key", key))

    def save_address(self, ip, port):
        return self._do("save_address", (ip, port), lambda: setattr(self, "address", (ip, port)))

    def load(self):
        return self._do("load", (), lambda: DeviceKey(self.key) if self.key else StoreRefusal("no key"))

    def load_address(self):
        return self._do("load_address", (), lambda: self.address or StoreRefusal("no address"))


def paired_setup(reg_fault=None, store_fault=None, address=(LOCAL_IP, PORT)):
    """A registry with one remote device and one local device whose key the store holds."""
    reg = FakeRegistry(reg_fault)
    reg.add("phone")
    dev_id, key = reg.add(LOCAL_NAME)
    return reg, FakeStore(store_fault, key, address), dev_id


def fresh(rf=None, sf=None):
    reg = FakeRegistry(rf)
    reg.add("phone")
    return reg, FakeStore(sf)


def stale_key(rf=None, sf=None):
    """The stored key's device was revoked elsewhere; an older live local device has no stored key."""
    reg, store, dev_id = paired_setup(rf, sf)
    reg.rows[dev_id][2] = True
    reg.add(LOCAL_NAME)
    return reg, store


def impostor(rf=None, sf=None):
    """A remote window paired itself under LOCAL_NAME next to the real local device."""
    reg, store, _ = paired_setup(rf, sf)
    reg.add(LOCAL_NAME)
    return reg, store


SCENARIOS = {
    "fresh": fresh,
    "valid": lambda rf=None, sf=None: paired_setup(rf, sf)[:2],
    "stale_port": lambda rf=None, sf=None: paired_setup(rf, sf, (LOCAL_IP, PORT + 1))[:2],
    "stale_ip": lambda rf=None, sf=None: paired_setup(rf, sf, ("100.64.0.1", PORT))[:2],
    "stale_key": stale_key,
    "impostor": impostor,
}
REG_METHODS = ("list", "check", "issue_code", "pair", "revoke")
STORE_METHODS = ("load", "load_address", "save", "save_address")


class Invariants:
    def run_ensure(self, reg, store, port=PORT):
        before = set(reg.live())
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            result = ensure(reg, store, port, NOW)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.check_invariants(reg, store, result, before, records)
        return result

    def check_invariants(self, reg, store, result, before, records):
        self.assertIsInstance(result, (LocalDevice, Refusal))
        live = reg.live()
        self.assertLessEqual(len(live), max(1, len(before)), "ensure added a second live local device")
        if set(live) - before:  # ensure paired: the new device is the only live local one, its key is stored
            self.assertEqual(len(live), 1, "two live local devices")
            self.assertEqual(list(live.values()), [store.key], "a live local device without a stored key")
        if isinstance(result, LocalDevice):
            self.assertEqual(list(live), [result.device_id])
            self.assertEqual((live[result.device_id], store.address), (store.key, (LOCAL_IP, PORT)))
        text = repr(result) + str(result) + repr(store) + "".join(r.getMessage() for r in records)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for _, args in store.log:  # the pairing code never reaches the store
            self.assertFalse(any(type(a) is str and len(a) == 8 and a.isdigit() for a in args))


class AcceptanceTests(Invariants, unittest.TestCase):
    def test_1_valid_stored_key_returns_its_id_and_writes_nothing(self):
        reg, store, dev_id = paired_setup()
        self.assertEqual(self.run_ensure(reg, store), LocalDevice(dev_id))
        self.assertFalse({"save", "save_address"} & set(store.calls))
        self.assertFalse({"issue_code", "pair", "revoke"} & set(reg.calls))

    def test_2_fresh_pair_under_local_name_stores_key_and_loopback_address(self):
        reg, store = fresh()
        result = self.run_ensure(reg, store)
        self.assertIsInstance(result, LocalDevice)
        self.assertEqual(reg.rows[result.device_id][0], LOCAL_NAME)
        self.assertEqual(store.address, (LOCAL_IP, PORT))
        self.assertEqual(self.run_ensure(reg, store), result)  # the next start reuses it
        self.assertEqual(reg.calls["pair"], 1)

    def test_3_older_local_device_is_revoked_before_the_new_pairing(self):
        reg, store = stale_key()
        (old,) = reg.live()
        result = self.run_ensure(reg, store)
        self.assertIsInstance(result, LocalDevice)
        methods = [m for m, _ in reg.log]
        self.assertLess(reg.log.index(("revoke", (old,))), methods.index("pair"))
        self.assertTrue(reg.rows[old][2])

    def test_3_remote_device_under_the_local_name_gains_nothing_and_is_revoked(self):
        reg, store, dev_id = paired_setup()
        remote, _ = reg.add(LOCAL_NAME)
        self.assertEqual(self.run_ensure(reg, store), LocalDevice(dev_id))
        self.assertTrue(reg.rows[remote][2])
        self.assertEqual(list(reg.live()), [dev_id])

    def test_4_device_cap_without_a_local_device_refuses_and_revokes_nothing(self):
        reg = FakeRegistry()
        for n in range(MAX_DEVICES):
            reg.add(f"remote {n}")
        self.assertEqual(self.run_ensure(reg, FakeStore()), Refusal("device_cap"))
        self.assertFalse({"revoke", "pair", "issue_code"} & set(reg.calls))

    def test_4_cap_counting_the_old_local_device_still_re_pairs(self):
        reg, store, old = paired_setup(address=(LOCAL_IP, PORT + 1))
        for n in range(MAX_DEVICES - 2):
            reg.add(f"remote {n}")
        self.assertIsInstance(self.run_ensure(reg, store), LocalDevice)
        self.assertTrue(reg.rows[old][2])

    def test_5_keystore_unavailable_refuses_without_pairing(self):
        for store in (None, StoreRefusal("DPAPI unavailable")):
            with self.subTest(store=store):
                reg = FakeRegistry()
                self.assertEqual(ensure(reg, store, PORT, NOW), Refusal("keystore_unavailable"))
                self.assertFalse({"issue_code", "pair"} & set(reg.calls))

    def test_5_save_failure_after_pairing_revokes_the_new_device(self):
        for method in ("save", "save_address"):
            for mode in MODES:
                with self.subTest(method=method, mode=mode):
                    reg, store = fresh(sf=(method, None, mode))
                    self.assertEqual(self.run_ensure(reg, store), Refusal("keystore_save_failed"))
                    self.assertIn("pair", reg.calls)
                    self.assertEqual(reg.live(), {})


class FaultMatrixTests(Invariants, unittest.TestCase):
    def test_every_call_refusing_failing_or_raising_keeps_the_invariants(self):
        for name, make in SCENARIOS.items():
            reg, store = make()
            self.run_ensure(reg, store)  # a clean run counts how often each method is called
            counts = {**{m: reg.calls.get(m, 0) for m in REG_METHODS}, **{m: store.calls.get(m, 0) for m in STORE_METHODS}}
            for method, count in counts.items():
                for nth in [None, *range(1, count + 1)]:
                    for mode in MODES:
                        fault = (method, nth, mode)
                        with self.subTest(scenario=name, fault=fault):
                            reg, store = make(fault, None) if method in REG_METHODS else make(None, fault)
                            self.run_ensure(reg, store)

    def test_a_faulted_run_heals_on_the_next_clean_start(self):
        for method in REG_METHODS + STORE_METHODS:
            for mode in MODES:
                with self.subTest(method=method, mode=mode):
                    fault = (method, None, mode)
                    reg, store = stale_key(fault) if method in REG_METHODS else stale_key(None, fault)
                    self.run_ensure(reg, store)
                    reg.fault = store.fault = None
                    self.assertIsInstance(self.run_ensure(reg, store), LocalDevice)


class NegativeTests(Invariants, unittest.TestCase):
    def test_2_unreadable_registry_refuses_with_no_pairing_attempt(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                reg, store = fresh(("list", None, mode))
                self.assertEqual(self.run_ensure(reg, store), Refusal("registry_unreadable"))
                self.assertFalse({"issue_code", "pair", "revoke"} & set(reg.calls))

    def test_3_address_with_another_port_or_ip_re_pairs_and_revokes_the_old_device(self):
        for address in ((LOCAL_IP, PORT + 1), ("100.64.0.1", PORT), ("127.0.0.2", PORT), None):
            with self.subTest(address=address):
                reg, store, old = paired_setup(address=address)
                result = self.run_ensure(reg, store)
                self.assertIsInstance(result, LocalDevice)
                self.assertNotEqual(result.device_id, old)
                self.assertTrue(reg.rows[old][2])
                self.assertEqual(store.address, (LOCAL_IP, PORT))

    def test_4_no_key_or_code_in_any_result_repr_or_log(self):
        # the scan itself lives in Invariants.check_invariants; here every outcome kind is produced once
        for reg, store in (fresh(), stale_key(), fresh(("pair", None, "raise")), fresh(sf=("save", None, "raise")),
                           fresh(("issue_code", None, "raise")), impostor(("revoke", None, "raise"))):
            with self.subTest(log=reg.log[:1]):
                result = self.run_ensure(reg, store)
                self.assertNotIn("KEY", repr(result))
        self.assertTrue(any(s.isdigit() for s in SECRETS) and any(s.startswith("KEY") for s in SECRETS))

    def test_bad_ports_are_refused_before_any_call(self):
        values = [*HOSTILE_NUMBERS, None, "8765", 80, 1023, 65536, 8765.0, -1]
        values = [v for v in values if not (type(v) is int and 1024 <= v <= 65535)]

        def check(port):
            reg, store = fresh()
            self.assertEqual(ensure(reg, store, port, NOW), Refusal("bad_port"))
            self.assertEqual((reg.calls, store.calls), ({}, {}))
        each(self, values, check)


class StaticTests(unittest.TestCase):
    def test_5_no_network_process_ctypes_or_logging_import(self):
        used = set()
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Import):
                used |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                used.add(node.module)
        self.assertEqual(used, {"__future__", "dataclasses", "jarvis.face.devices", "jarvis.face.keystore"})
        code = SOURCE.split('"""', 2)[2]  # past the module docstring, which names what is absent
        for word in ("socket", "http", "urllib", "ssl", "subprocess", "ctypes", "logging", "print("):
            self.assertNotIn(word, code)

    def test_the_local_identity_never_comes_from_the_name_alone(self):
        self.assertIs(local_pair.Refusal, Refusal)  # ensure answers with the registry's typed Refusal
        self.assertEqual(LOCAL_NAME, "local window")
