"""ADVCORPUS1: proves the shared hostile-input corpus in tests/adversarial.py."""

import ast
import builtins
import importlib
import io
import json
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import adversarial

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("HOSTILE_PATH_PARTS", "HOSTILE_TEXT", "HOSTILE_JSON", "HOSTILE_NUMBERS")


class CorpusShapeTests(unittest.TestCase):
    def test_groups_are_non_empty_tuples(self):
        for name in GROUPS:
            group = getattr(adversarial, name)
            self.assertIsInstance(group, tuple, name)
            self.assertTrue(group, name)

    def test_entries_unique(self):
        for name in GROUPS:
            # type+repr: NaN != NaN and True == 1, so plain set() would lie.
            keys = [(type(v).__name__, repr(v)) for v in getattr(adversarial, name)]
            self.assertEqual(len(keys), len(set(keys)), name)

    def test_every_entry_has_class_comment(self):
        source = Path(adversarial.__file__).read_text(encoding="utf-8")
        lines = source.splitlines()
        seen = set()
        for node in ast.parse(source).body:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)):
                continue
            name = node.targets[0].id
            if name not in GROUPS:
                continue
            seen.add(name)
            for elt in node.value.elts:
                tail = lines[elt.end_lineno - 1][elt.end_col_offset:]
                comment = tail.partition("#")[2].strip()
                self.assertTrue(comment, f"{name} line {elt.lineno} has no class comment")
        self.assertEqual(seen, set(GROUPS))

    def test_required_classes_present(self):
        paths = adversarial.HOSTILE_PATH_PARTS
        for value in ("CLAUDE~1", "x::$DATA", "con.txt", "..", "C:x", "\\\\localhost\\C$",
                      "a／b", "a∕b", ".clаude"):
            self.assertIn(value, paths)
        text = adversarial.HOSTILE_TEXT
        self.assertTrue(any(len(v) >= 100_000 for v in text))
        for ch in ("‮", "⁩", "​", "﻿", "͏", "ㅤ", "\x00",
                   "\ud800", "\r\n", "Ị̇", "İ"):
            self.assertTrue(any(ch in v for v in text), repr(ch))
        self.assertIn(10**400, adversarial.HOSTILE_NUMBERS)
        self.assertIn("1" + "0" * 400, adversarial.HOSTILE_JSON)

    def test_deep_json_raises_recursion_error(self):
        self.assertEqual(sys.getrecursionlimit(), 1000)
        with self.assertRaises(RecursionError):
            json.loads(adversarial.deep_json())


class HelperTests(unittest.TestCase):
    def test_narrow_stdout_rejects_dotless_i(self):
        before = sys.stdout
        with self.assertRaises(UnicodeEncodeError):
            with adversarial.narrow_stdout():
                print("ı")
        self.assertIs(sys.stdout, before)

    def test_narrow_stdout_accepts_cp1252(self):
        with adversarial.narrow_stdout() as stream:
            print("café", end="")
            stream.flush()
            self.assertEqual(stream.buffer.getvalue(), b"caf\xe9")

    def test_each_reports_every_failing_value(self):
        class Inner(unittest.TestCase):
            def runTest(self):
                adversarial.each(self, ("a", "b", "c", "ok"), lambda v: self.assertEqual(v, "ok"))

        result = unittest.TestResult()
        Inner().run(result)
        self.assertEqual(len(result.failures), 3)
        reported = " ".join(str(test) for test, _ in result.failures)
        for value in ("'a'", "'b'", "'c'"):
            self.assertIn(value, reported)


class IsolationTests(unittest.TestCase):
    def test_import_touches_no_file_system_or_network(self):
        guards = {
            "builtins.open": mock.patch.object(builtins, "open", side_effect=AssertionError("open")),
            "io.open": mock.patch.object(io, "open", side_effect=AssertionError("io.open")),
            "os.open": mock.patch.object(os, "open", side_effect=AssertionError("os.open")),
            "socket": mock.patch.object(socket, "socket", side_effect=AssertionError("socket")),
            "connect": mock.patch.object(socket, "create_connection", side_effect=AssertionError("connect")),
        }
        saved = sys.modules.pop("tests.adversarial")
        try:
            with mock.patch.object(sys, "dont_write_bytecode", True):
                mocks = {name: guard.start() for name, guard in guards.items()}
                try:
                    fresh = importlib.import_module("tests.adversarial")
                finally:
                    mock.patch.stopall()
            for name, guard in mocks.items():
                self.assertFalse(guard.called, name)
            self.assertIsNot(fresh, saved)
            self.assertTrue(fresh.HOSTILE_TEXT)
        finally:
            sys.modules["tests.adversarial"] = saved
            sys.modules["tests"].adversarial = saved

    def test_no_product_module_imports_corpus(self):
        offenders = []
        for top in ("jarvis", "scripts"):
            for path in sorted((ROOT / top).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        names = [mod] + [f"{mod}.{a.name}" for a in node.names]
                    else:
                        continue
                    if any(n == "tests.adversarial" or n.startswith("tests.adversarial.") for n in names):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
