"""FACEDEV1: remote-window device registry and one-time pairing codes (pure logic, temp root, frozen clock)."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.face import devices
from jarvis.face.devices import Device, Paired, Refusal, Registry
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_TEXT, each

T0 = 1_800_000_000
PROBE_KEY, PROBE_ID = "A" * 52, "0123456789abcdef"  # present in the shape-matrix fixture
SOURCE = Path(devices.__file__).read_text(encoding="utf-8")


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reg = Registry(self.root)
        self.file = self.root / "talk" / "state" / "devices.json"

    def paired(self, name="Predator", now=T0) -> Paired:
        result = self.reg.pair(self.reg.issue_code(now), name, now)
        self.assertIsInstance(result, Paired)
        return result

    def write_raw(self, raw) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))

    def assert_refuses_to_load(self) -> None:
        unreadable = Refusal("registry_unreadable")
        self.assertEqual(self.reg.list(), unreadable)
        self.assertEqual(self.reg.check(PROBE_KEY, T0), unreadable)
        self.assertEqual(self.reg.revoke(PROBE_ID), unreadable)
        before = self.file.read_bytes() if self.file.is_file() else None
        self.assertEqual(self.reg.pair(self.reg.issue_code(T0), "x", T0), unreadable)
        if before is not None:
            self.assertEqual(self.file.read_bytes(), before)  # a bad file is never overwritten


class RegistryFileTests(_Root):
    def test_missing_file_means_no_devices(self) -> None:
        self.assertEqual(self.reg.list(), ())
        self.assertFalse(self.file.exists())

    def test_record_fields_and_atomic_write(self) -> None:
        p = self.paired("  Pred\u202eator\x00  laptop ")
        data = json.loads(self.file.read_text(encoding="ascii"))
        self.assertEqual(set(data), {"version", "devices"})
        (rec,) = data["devices"]
        self.assertEqual(set(rec), devices.FIELDS)
        self.assertEqual(rec["name"], "Predator laptop")
        self.assertEqual(rec["key_hash"], devices._digest(p.key))
        self.assertEqual((rec["created"], rec["last_seen"], rec["revoked"]), (T0, T0, False))
        self.assertEqual(rec["id"], p.device.id)
        self.assertEqual([f.name for f in self.file.parent.iterdir()], ["devices.json"])  # no temp left
        with mock.patch.object(devices.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.paired("second")
        self.assertEqual([f.name for f in self.file.parent.iterdir()], ["devices.json"])
        self.assertEqual(len(self.reg.list()), 1)

    def test_shape_matrix_refuses_to_load(self) -> None:
        good = {"id": PROBE_ID, "name": "a", "key_hash": devices._digest(PROBE_KEY), "created": 1, "last_seen": 1,
                "revoked": False}

        def doc(**over):
            return json.dumps({"version": 1, "devices": [{**good, **over}]})

        bad = [doc(id=1), doc(id="ABC"), doc(name=""), doc(name=" a"), doc(name="a\u0000b"), doc(name=["a"]),
               doc(key_hash="0" * 63), doc(key_hash="G" * 64), doc(created=True), doc(created=-1),
               doc(created=1.5), doc(last_seen=None), doc(revoked=0), doc(revoked="false"),
               doc(created=10**400), doc(created=1).replace('"created": 1,', '"created": ' + "9" * 5000 + ","),
               doc(extra=1),
               json.dumps({"version": 1, "devices": [good, {**good, "key_hash": "1" * 64}]}),  # duplicate id
               json.dumps({"version": 1, "devices": [good, {**good, "id": "1" * 16}]}),  # duplicate key hash
               json.dumps({"version": 2, "devices": []}), json.dumps({"version": True, "devices": []}),
               json.dumps({"devices": []}), json.dumps({"version": 1, "devices": {}}), json.dumps([]),
               '{"version": 1, "version": 1, "devices": []}', '{"version": 1, "devices": []}\x00',
               "", "{", b"\xff\xfe", "nul\x00"] + list(HOSTILE_JSON) + ["[" * 5000 + "]" * 5000]
        many = [{**good, "id": f"{i:016x}", "key_hash": f"{i:064x}"} for i in range(5)]
        bad.append(json.dumps({"version": 1, "devices": many}))  # five live devices
        each(self, bad, lambda raw: (self.write_raw(raw), self.assert_refuses_to_load()))
        self.write_raw(json.dumps({"version": 1, "devices": [good]}))
        self.assertEqual(len(self.reg.list()), 1)
        self.assertEqual(self.reg.check(PROBE_KEY, T0), PROBE_ID)  # the probes hit a readable file
        self.assertEqual(self.reg.revoke(PROBE_ID).id, PROBE_ID)

    def test_a_50_mb_file_or_a_directory_refuses_to_load(self) -> None:
        self.write_raw(json.dumps({"version": 1, "devices": []}))
        os.truncate(self.file, 50 * 1024 * 1024)  # sparse
        self.assert_refuses_to_load()
        self.file.unlink()
        self.file.mkdir()
        self.assert_refuses_to_load()


class CodeTests(_Root):
    def test_code_is_eight_ascii_digits_and_single_use(self) -> None:
        code = self.reg.issue_code(T0)
        self.assertRegex(code, r"\A[0-9]{8}\Z")
        self.assertIsInstance(self.reg.pair(code, "a", T0 + 299), Paired)
        self.assertEqual(self.reg.pair(code, "b", T0 + 1), Refusal("no_code"))

    def test_only_one_live_code(self) -> None:
        old = self.reg.issue_code(T0)
        new = self.reg.issue_code(T0)
        if old != new:
            self.assertEqual(self.reg.pair(old, "a", T0), Refusal("wrong_code"))
        self.assertIsInstance(self.reg.pair(new, "a", T0), Paired)

    def test_no_live_code_or_expired_code_refuses(self) -> None:
        self.assertEqual(self.reg.pair("12345678", "a", T0), Refusal("no_code"))
        code = self.reg.issue_code(T0)
        self.assertEqual(self.reg.pair(code, "a", T0 + 300), Refusal("expired"))
        self.assertEqual(self.reg.pair(code, "a", T0 + 1), Refusal("no_code"))  # expiry burns it

    def test_a_clock_that_goes_backwards_expires_the_code(self) -> None:
        code = self.reg.issue_code(T0)
        self.assertEqual(self.reg.pair(code, "a", T0 - 1), Refusal("expired"))
        self.assertEqual(self.reg.pair(code, "a", T0), Refusal("no_code"))

    def test_a_clock_that_goes_below_an_earlier_attempt_burns_the_code(self) -> None:
        code = self.reg.issue_code(T0)
        wrong = "00000000" if code != "00000000" else "11111111"
        self.assertEqual(self.reg.pair(wrong, "a", T0 + 100), Refusal("wrong_code"))
        self.assertEqual(self.reg.pair(code, "a", T0 + 50), Refusal("expired"))
        self.assertEqual(self.reg.pair(code, "a", T0 + 101), Refusal("no_code"))
        code = self.reg.issue_code(T0)  # a refused name still records its clock
        self.assertEqual(self.reg.pair(code, "\x00", T0 + 100), Refusal("bad_name"))
        self.assertEqual(self.reg.pair(code, "a", T0 + 50), Refusal("expired"))
        code = self.reg.issue_code(T0)  # a fraction of a second back is back too
        wrong = "00000000" if code != "00000000" else "11111111"
        self.assertEqual(self.reg.pair(wrong, "a", T0 + 100.5), Refusal("wrong_code"))
        self.assertEqual(self.reg.pair(code, "a", T0 + 100), Refusal("expired"))
        self.assertFalse(self.file.exists())

    def test_the_same_clock_again_keeps_the_code(self) -> None:
        code = self.reg.issue_code(T0)
        wrong = "00000000" if code != "00000000" else "11111111"
        self.assertEqual(self.reg.pair(wrong, "a", T0 + 100), Refusal("wrong_code"))
        self.assertIsInstance(self.reg.pair(code, "a", T0 + 100), Paired)

    def test_a_try_that_times_out_on_the_lock_still_records_its_clock(self) -> None:
        code = self.reg.issue_code(T0)
        with mock.patch.object(self.reg, "_locked", side_effect=TimeoutError("registry busy")):
            self.assertRaises(TimeoutError, self.reg.pair, code, "a", T0 + 100)
        self.assertEqual(self.reg.pair(code, "a", T0 + 50), Refusal("expired"))

    def test_five_wrong_attempts_burn_the_code(self) -> None:
        code = self.reg.issue_code(T0)
        wrong = [c for c in ("00000000", "11111111", "22222222", "33333333", "44444444", "55555555") if c != code]
        for guess in [wrong[0], wrong[1], 12345678, "\u0661" * 8, wrong[2]]:  # malformed guesses count too
            self.assertEqual(self.reg.pair(guess, "a", T0), Refusal("wrong_code"))
        self.assertEqual(self.reg.pair(code, "a", T0), Refusal("no_code"))
        self.assertFalse(self.file.exists())

    def test_attempt_is_counted_before_comparing(self) -> None:
        self.reg.issue_code(T0)
        seen = []
        real = devices.hmac.compare_digest

        def spy(a, b):
            seen.append(self.reg._code.attempts)
            return real(a, b)

        with mock.patch.object(devices.hmac, "compare_digest", spy):
            self.reg.pair("00000000", "a", T0)
        self.assertEqual(seen, [1])

    def test_bad_clock_values_raise_without_secrets(self) -> None:
        each(self, [v for v in HOSTILE_NUMBERS if not (type(v) is float and v == 0)] + [-1, 2**53],
             lambda v: self.assertRaises(ValueError, self.reg.issue_code, v))


class CheckTests(_Root):
    def test_check_returns_the_device_and_refuses_revoked(self) -> None:
        p = self.paired()
        self.assertEqual(self.reg.check(p.key, T0), p.device.id)
        self.assertEqual(self.reg.check("A" * 52, T0), Refusal("unknown_key"))
        self.assertEqual(self.reg.revoke(p.device.id).revoked, True)
        self.assertEqual(self.reg.check(p.key, T0), Refusal("revoked"))
        self.assertEqual(self.reg.revoke("f" * 16), Refusal("unknown_device"))
        self.assertEqual(self.reg.revoke(None), Refusal("unknown_device"))

    def test_bad_key_shapes_refuse_before_any_hashing(self) -> None:
        p = self.paired()

        class Sub(str):
            pass

        bad = [p.key[:-1], p.key + "A", p.key.lower(), p.key[:-1] + "1", p.key[:-1] + "=", p.key[:-1] + "\u0410",
               p.key + "\n", "", None, 1, p.key.encode(), [p.key], Sub(p.key)] + list(HOSTILE_TEXT)
        with mock.patch.object(devices, "_digest", side_effect=AssertionError("hashed")), \
                mock.patch.object(devices.hmac, "compare_digest", side_effect=AssertionError("compared")):
            each(self, bad, lambda k: self.assertEqual(self.reg.check(k, T0), Refusal("bad_key")))

    def test_last_seen_is_written_at_most_once_a_minute(self) -> None:
        p = self.paired()
        writes = []
        real = self.reg._write
        with mock.patch.object(self.reg, "_write", side_effect=lambda r: (writes.append(1), real(r))):
            for t in (T0 + 1, T0 + 59, T0 - 100, T0 + 60, T0 + 61, T0 + 119):
                self.assertEqual(self.reg.check(p.key, t), p.device.id)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.reg.list()[0].last_seen, T0 + 60)
        with mock.patch.object(self.reg, "_write", side_effect=OSError("disk")):
            self.assertEqual(self.reg.check(p.key, T0 + 500), p.device.id)

    def test_a_last_seen_write_never_undoes_a_concurrent_revoke(self) -> None:
        p = self.paired()
        other = Registry(self.root)  # e.g. the CLI, another process
        real = self.reg._load
        calls = []

        def load_then_revoke():
            records = real()
            if not calls:
                other.revoke(p.device.id)  # lands between check's read and its last_seen write
            calls.append(1)
            return records

        with mock.patch.object(self.reg, "_load", load_then_revoke):
            self.reg.check(p.key, T0 + 120)
        self.assertTrue(other.list()[0].revoked)
        self.assertEqual(self.reg.check(p.key, T0 + 240), Refusal("revoked"))

    def test_a_racing_last_seen_write_never_moves_it_backwards(self) -> None:
        other = Registry(self.root)
        real, real_write = self.reg._load, self.reg._write
        calls, writes = [], []

        def load_then_check(at):
            def load():
                records = real()
                if not calls:
                    self.assertEqual(other.check(p.key, at), p.device.id)  # the racer writes first
                calls.append(1)
                return records
            return load

        for racer, mine in ((T0 + 61, T0 + 60), (T0 + 60, T0 + 61)):  # a later or an earlier racer: no second write
            p = self.paired()
            calls.clear()
            with mock.patch.object(self.reg, "_load", load_then_check(racer)), \
                    mock.patch.object(self.reg, "_write", side_effect=lambda r: (writes.append(1), real_write(r))):
                self.assertEqual(self.reg.check(p.key, mine), p.device.id)
            self.assertEqual(writes, [])
            self.assertEqual({d.id: d.last_seen for d in other.list()}[p.device.id], racer)

    def test_writers_take_the_lock_and_a_stale_lock_is_broken(self) -> None:
        p = self.paired()
        lock = self.file.with_name("devices.lock")
        lock.write_bytes(b"")
        with mock.patch.object(devices, "LOCK_WAIT", 0.05):
            with self.assertRaises(TimeoutError):
                self.reg.revoke(p.device.id)
        self.assertFalse(self.reg.list()[0].revoked)
        old = lock.stat().st_mtime - devices.LOCK_STALE - 5
        os.utime(lock, (old, old))
        self.assertTrue(self.reg.revoke(p.device.id).revoked)
        self.assertFalse(lock.exists())

    def test_a_fifth_device_is_refused_until_one_is_revoked(self) -> None:
        first = [self.paired(f"d{i}") for i in range(4)]
        code = self.reg.issue_code(T0)
        self.assertEqual(self.reg.pair(code, "fifth", T0), Refusal("too_many_devices"))
        self.reg.revoke(first[0].device.id)
        self.assertIsInstance(self.reg.pair(code, "fifth", T0), Paired)  # the full refusal did not burn the code
        self.assertEqual(sum(not d.revoked for d in self.reg.list()), 4)

    def test_revoked_records_are_capped(self) -> None:
        for i in range(devices.MAX_RECORDS + 3):
            self.reg.revoke(self.paired(f"d{i}", now=T0 + i).device.id)
        names = [d.name for d in self.reg.list()]
        self.assertEqual(len(names), devices.MAX_RECORDS)
        self.assertEqual(names[-1], f"d{devices.MAX_RECORDS + 2}")


class LeakTests(_Root):
    def test_no_key_or_code_in_repr_str_file_list_or_messages(self) -> None:
        code = self.reg.issue_code(T0)
        texts = [repr(self.reg), str(self.reg), repr(self.reg._code), str(self.reg._code)]
        p = self.reg.pair(code, "a", T0)
        code2 = self.reg.issue_code(T0)
        texts += [repr(self.reg._code), repr(p), str(p), repr(p.device), str(self.reg.list()),
                  repr(self.reg.pair("00000000" if code2 != "00000000" else "11111111", "b", T0)),
                  repr(self.reg.check(p.key[:-1], T0)), self.file.read_text(encoding="ascii")]
        for bad in (float("nan"), "x"):
            with self.assertRaises(ValueError) as ctx:
                self.reg.check(p.key, bad)
            texts.append(str(ctx.exception))
        with mock.patch.object(devices.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError) as ctx:
                self.reg.pair(code2, "c", T0)
            texts.append(str(ctx.exception))
        digest = devices._digest(code)
        for text in texts:
            for secret in (p.key, code, code2, digest, devices._digest(code2)):
                self.assertNotIn(secret, text)

    def test_names_are_cleaned_labels(self) -> None:
        def check(text):
            label = devices.clean_name(text)
            self.assertLessEqual(len(label), devices.MAX_NAME)
            self.assertEqual(devices.clean_name(label), label)
            self.assertFalse(any(ch in label for ch in "\x00\x07\x1b\u202e\u200b\ufeff\ud800\r\n"))
        each(self, list(HOSTILE_TEXT), check)
        self.assertEqual(self.reg.pair(self.reg.issue_code(T0), "\u200b\x00 ", T0), Refusal("bad_name"))
        self.assertIsInstance(self.reg.pair(self.reg.issue_code(T0), "Cafe\u200b\u0301", T0), Paired)
        self.assertEqual(self.reg.list()[0].name, "Caf\u00e9")  # stored NFC, so the file still loads


class CliAndStaticTests(_Root):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = devices.main(["--root", str(self.root), *argv])
            except SystemExit as exc:  # argparse refusals and --help exit
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_list_revoke(self) -> None:
        p = self.paired("laptop")
        rc, out, _ = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn(p.device.id, out)
        self.assertNotIn(p.key, out)
        self.assertNotIn(devices._digest(p.key), out)
        self.assertEqual(self.run_cli("revoke", p.device.id)[0], 0)
        self.assertIn("revoked", self.run_cli("list")[1])
        self.assertEqual(self.run_cli("revoke", "nope")[0], 1)

    def test_cli_has_no_pairing_code_command(self) -> None:
        issued = mock.patch.object(Registry, "issue_code", side_effect=AssertionError("issued"))
        with issued:
            for argv in (["code"], ["Code"], ["CODE"], ["code", "--help"], ["pair"], ["pair_code"], ["cod"],
                         ["list", "code"], ["revoke"], [""], ["code", "list"]):
                with self.subTest(argv=argv):
                    rc, out, err = self.run_cli(*argv)
                    self.assertEqual((rc, out), (2, ""))
                    self.assertNotRegex(err, "[0-9]{8}")
                    self.assertFalse(self.file.parent.exists())  # nothing written to the registry
        rc, out, _ = self.run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("{list,revoke}", out)
        self.assertNotIn("code", out)

    def test_parser_registers_only_list_and_revoke(self) -> None:
        seen = []
        real = devices.argparse.ArgumentParser.parse_args

        def spy(parser, *a, **k):
            seen.extend(act.choices for act in parser._actions if isinstance(act, devices.argparse._SubParsersAction))
            return real(parser, *a, **k)
        with mock.patch.object(devices.argparse.ArgumentParser, "parse_args", spy):
            self.run_cli("list")
        self.assertEqual([set(c) for c in seen], [{"list", "revoke"}])
        self.assertNotIn("issue_code", SOURCE[SOURCE.index("def main("):])

    def test_a_cli_revoke_from_another_process_reaches_a_running_registry(self) -> None:
        p = self.paired("phone")  # paired on the serving Registry A
        self.assertEqual(self.reg.check(p.key, T0), p.device.id)
        b = Registry(self.root)  # a second instance on the same root, as another process would hold
        self.assertEqual(b.check(p.key, T0), p.device.id)
        self.assertEqual(self.run_cli("revoke", p.device.id)[0], 0)  # main() builds its own Registry
        self.assertEqual(self.reg.check(p.key, T0 + 1), Refusal("revoked"))  # A re-reads the file on every check
        self.assertEqual(b.check(p.key, T0 + 1), Refusal("revoked"))

    def test_list_never_returns_keys_or_hashes(self) -> None:
        self.paired()
        self.assertEqual(set(Device.__dataclass_fields__), {"id", "name", "created", "last_seen", "revoked"})

    def test_no_network_or_process_import(self) -> None:
        names = set()
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertFalse(names & {"socket", "http", "urllib", "ssl", "requests", "asyncio", "subprocess", "logging"})
        for word in [stem + "_" for stem in ("talk", "telegram", "brain")] + ["heartbeat", "project" + "_folders"]:
            self.assertNotIn(word, SOURCE)


if __name__ == "__main__":
    unittest.main()
