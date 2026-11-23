"""DECISIONID1: decision-id registry; inactive, unknown, superseded or malformed decisions are refused."""
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import orch_decisions as od  # noqa: E402

A, B, C = "D-20261123-0132-CODEXMAX", "D-20261123-0140-LANECAP", "D-20261123-0150-LANECAP2"
SRC, AT = "KARARLAR 2026-11-23 01:32", "2026-11-23T01:32:00"


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.path = Path(d) / "build" / "orch" / "decisions.json"

    def add(self, i, cls="authority"):
        return od.add(i, cls, SRC, AT, path=self.path)

    def raw(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_active_id_allowed(self):
        self.add(A)
        ok, why = od.require_active(A, path=self.path)
        self.assertTrue(ok, why)
        self.assertEqual(od.get(A, path=self.path)["state"], "active")

    def test_unknown_id_refused(self):
        self.add(A)
        self.assertEqual(od.require_active(B, path=self.path), (False, f"unknown decision {B}"))

    def test_inactive_refused(self):
        self.add(A)
        od.deactivate(A, path=self.path)
        self.assertEqual(od.require_active(A, path=self.path), (False, f"decision {A} is inactive"))

    def test_superseded_refused_and_names_the_successor(self):
        self.add(A)
        self.add(B)
        od.supersede(A, B, path=self.path)
        ok, why = od.require_active(A, path=self.path)
        self.assertFalse(ok)
        self.assertIn(f"superseded by {B}", why)
        self.assertEqual(od.get(A, path=self.path)["superseded_by"], B)
        self.assertTrue(od.require_active(B, path=self.path)[0])

    def test_malformed_file_refused_never_allowed_by_default(self):
        good = {"id": A, "class": "cost", "state": "active", "superseded_by": None, "source": SRC, "at": AT}
        cases = {
            "not json": "{",
            "not a list": json.dumps({A: good}),
            "duplicate key": json.dumps([good])[:-2] + ', "state": "active"}]',
            "duplicate id": json.dumps([good, good]),
            "extra key": json.dumps([dict(good, provisional=True)]),
            "bad class": json.dumps([dict(good, **{"class": "big"})]),
            "dangling successor": json.dumps([dict(good, state="superseded", superseded_by=B)]),
            "empty source": json.dumps([dict(good, source=" ")]),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.raw(text)
                self.assertIs(od.load(self.path), od.UNKNOWN)
                ok, why = od.require_active(A, path=self.path)
                self.assertFalse(ok)
                self.assertIn("malformed", why)
                self.assertFalse(od.require("cost", A, path=self.path)[0])

    def assert_refused_no_crash(self):
        self.assertIs(od.load(self.path), od.UNKNOWN)
        self.assertIsNone(od.get(A, path=self.path))
        ok, why = od.require_active(A, path=self.path)
        self.assertFalse(ok)
        self.assertIn("malformed", why)
        self.assertFalse(od.require("cost", A, path=self.path)[0])
        before = self.path.read_bytes()
        for write in (lambda: self.add(C), lambda: od.deactivate(A, path=self.path),
                      lambda: od.supersede(A, B, path=self.path)):
            with self.assertRaises(od.Malformed):
                write()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["decisions.json"])  # no lock left

    def test_undecodable_bytes_refused_not_crash(self):  # DECISIONID1FIX F2
        good = json.dumps([{"id": A, "class": "cost", "state": "active", "superseded_by": None,
                            "source": SRC, "at": AT}]).encode()
        cases = {"ff fe": b"\xff\xfe[]", "lone continuation": b"[\x80]", "truncated multibyte": b"[\xe2\x82",
                 "bad byte inside a valid record": good.replace(b"KARARLAR", b"KARAR\xffLAR")}
        for name, data in cases.items():
            with self.subTest(name):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(data)
                self.assert_refused_no_crash()

    def test_non_id_superseded_by_refused_not_crash(self):  # DECISIONID1FIX F1
        b = {"id": B, "class": "cost", "state": "active", "superseded_by": None, "source": SRC, "at": AT}
        for state in ("superseded", "active", "inactive"):
            for bad in ([], {}, [B], {B: B}, 5, True, 1.5):
                with self.subTest(state=state, superseded_by=bad):
                    self.raw(json.dumps([dict(b, id=A, state=state, superseded_by=bad), b]))
                    self.assert_refused_no_crash()

    def test_non_text_class_or_state_refused_not_crash(self):
        good = {"id": A, "class": "cost", "state": "active", "superseded_by": None, "source": SRC, "at": AT}
        for key in ("class", "state"):
            for bad in ([], {}, ["cost"], {"active": 1}, None, 0):
                with self.subTest(key=key, value=bad):
                    self.raw(json.dumps([dict(good, **{key: bad})]))
                    self.assert_refused_no_crash()

    def test_malformed_file_is_never_overwritten(self):
        self.raw("{")
        for write in (lambda: self.add(B), lambda: od.deactivate(A, path=self.path)):
            with self.assertRaises(od.Malformed):
                write()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{")

    def test_missing_file_refused_and_not_created_on_read(self):
        self.assertIs(od.load(self.path), od.UNKNOWN)
        self.assertIsNone(od.get(A, path=self.path))
        self.assertFalse(od.require_active(A, path=self.path)[0])
        self.assertFalse(self.path.parent.exists())

    def test_bad_id_shape_refused(self):
        for bad in ("d-20261123-0132-x", A.lower(), A + "\n", "D-２0261123-0132-X", "", None):
            with self.subTest(repr(bad)):
                self.assertFalse(od.require_active(bad, path=self.path)[0])
                with self.assertRaises((ValueError, TypeError)):
                    self.add(bad)
        self.assertFalse(self.path.exists())  # nothing was written

    def test_ids_are_immutable(self):
        self.add(A)
        od.deactivate(A, path=self.path)
        with self.assertRaises(ValueError):
            self.add(A)  # re-adding an inactive id would revive it
        self.assertEqual(od.get(A, path=self.path)["state"], "inactive")

    def test_concurrent_add_keeps_both(self):
        real, start, errors = od._read, threading.Barrier(2), []

        def slow_read(path):  # widen the read-modify-write window so an unlocked write loses a record
            recs = real(path)
            time.sleep(0.2)
            return recs

        def one(i):
            try:
                start.wait(5)
                self.add(i)
            except Exception as e:  # reported below
                errors.append(e)

        with mock.patch.object(od, "_read", slow_read):
            ts = [threading.Thread(target=one, args=(i,)) for i in (A, B)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(od.load(self.path)), [A, B])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["decisions.json"])  # no lock, no tmp

    def test_routine_action_needs_no_id(self):
        self.assertEqual(od.require("routine", path=self.path), (True, "routine: no decision needed"))
        self.assertFalse(self.path.parent.exists())  # the registry is not even read

    def test_non_routine_action_without_id_refused(self):
        self.add(A)
        for cls in ("authority", "security", "cost", "irreversible"):
            with self.subTest(cls):
                ok, why = od.require(cls, path=self.path)
                self.assertFalse(ok)
                self.assertIn("ask Salih", why)
        self.assertTrue(od.require("authority", A, path=self.path)[0])
        self.assertFalse(od.require("nonsense", A, path=self.path)[0])

    def test_routine_action_naming_a_dead_id_is_refused(self):
        self.add(A)
        od.deactivate(A, path=self.path)
        self.assertFalse(od.require("routine", A, path=self.path)[0])


if __name__ == "__main__":
    unittest.main()
