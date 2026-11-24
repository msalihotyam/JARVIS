import ast
import contextlib
import dataclasses
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from jarvis import talk_inbox as ti
from jarvis.talk_inbox import InboxRecord, Refusal, validate_record

VALID = {
    "decision": {"kind": "decision", "class": "ordinary", "cited_turns": ["t-0042"], "summary": "Use the new face."},
    "deferred": {"kind": "deferred", "class": "ordinary", "cited_turns": ["t_7"], "summary": "Later.", "reason": "Quota."},
    "question": {"kind": "question", "question": "Which colour?"},
    "glossary_candidate": {"kind": "glossary_candidate", "term": "ON", "definition": "front lane"},
    "spec_proposal": {"kind": "spec_proposal", "title": "Inbox", "body": "Line one\nline two"},
}


def enc(obj):
    return json.dumps(obj).encode("utf-8")


def rec(kind="decision", **changes):
    obj = dict(VALID[kind])
    for key, value in changes.items():
        if value is ...:
            obj.pop(key, None)
        else:
            obj[key] = value
    return enc(obj)


class TalkInboxAcceptance(unittest.TestCase):
    def refused(self, data, reason):
        result = validate_record(data)
        self.assertIsInstance(result, Refusal, repr(data)[:80])
        self.assertEqual(result.reason, reason, repr(data)[:80])

    def test_a1_kinds(self):
        for kind in VALID:
            result = validate_record(rec(kind))
            self.assertIsInstance(result, InboxRecord, kind)
            self.assertEqual(result.kind, kind)
        for bad in ("Decision", "approval", "", " decision", 5, None, ["decision"]):
            self.refused(enc(dict(VALID["decision"], kind=bad)), "bad_kind")
        self.refused(enc({k: v for k, v in VALID["decision"].items() if k != "kind"}), "bad_kind")

    def test_a2_unknown_field_refused_not_dropped(self):
        for kind in VALID:
            self.refused(rec(kind, note="x"), "unknown_field")
        self.refused(rec("question", **{"class": "ordinary"}), "unknown_field")
        for kind, (required, optional) in ti.KIND_FIELDS.items():
            for name in required | optional:
                self.assertFalse(ti._forbidden(name), name)

    def test_a3_decision_and_deferred_need_turns_and_class(self):
        for kind in ("decision", "deferred"):
            self.refused(rec(kind, cited_turns=...), "turns_missing")
            self.refused(rec(kind, cited_turns=[]), "turns_missing")
            self.refused(rec(kind, cited_turns="t1"), "turns_missing")
            self.refused(rec(kind, **{"class": ...}), "class_missing")
            self.refused(rec(kind, summary=...), "missing_field")
        ok = validate_record(rec("decision", cited_turns=["t1", "t2"]))
        self.assertEqual(ok.cited_turns, ("t1", "t2"))
        self.assertEqual(ok.record_class, "ordinary")

    def test_a4_authority_needs_card_never_approved(self):
        auth = validate_record(rec("decision", **{"class": "authority"}))
        self.assertTrue(auth.needs_card)
        self.assertFalse(validate_record(rec("deferred")).needs_card)
        self.assertFalse(validate_record(rec("question")).needs_card)
        names = {f.name for f in dataclasses.fields(InboxRecord)}
        self.assertFalse({n for n in names if "approv" in n or "appl" in n or "trust" in n})
        self.assertNotIn("approved", json.dumps(dataclasses.asdict(auth)).casefold())

    def test_a5_size_cap_and_text_only(self):
        self.refused(rec(summary="x" * (ti.MAX_TEXT_CHARS + 1)), "bad_text")
        self.assertIsInstance(validate_record(rec(summary="x" * ti.MAX_TEXT_CHARS)), InboxRecord)
        for bad in (5, 1.5, True, None, ["a"], {"a": "b"}, "", "   "):
            self.refused(rec(summary=bad), "bad_text")
        for bad in ("a\u202eb", "a\u200bb", "a\x00b", "a\x1bb", "a\u2066b", "a\u2028b", "a\u2029b", "a\uffffb"):
            self.refused(rec(summary=bad), "bad_text")
        self.refused(b'{"kind":"question","question":"\\ud800"}', "bad_text")
        self.refused(b'{"kind":"question","question":"x"}' + b" " * ti.MAX_RECORD_BYTES, "too_large")

    def test_a6_pure_bytes_in_typed_out_deterministic(self):
        for bad in ('{"kind":"question","question":"x"}', bytearray(b"{}"), {"kind": "question"}, None, 5):
            self.refused(bad, "wrong_type")
        data = rec("spec_proposal")
        first, second = validate_record(data), validate_record(data)
        self.assertEqual(first, second)
        self.assertEqual(first.text, (("body", "Line one\nline two"), ("title", "Inbox")))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.needs_card = True

    def test_no_io_or_network_imports(self):
        tree = ast.parse(Path(ti.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "exec", "eval", "compile", "__import__"})
        self.assertLessEqual(imported, {"__future__", "json", "re", "unicodedata", "dataclasses"})
        self.assertFalse(imported & {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "shutil"})

    def test_refusal_texts_ascii_cp1252(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stdout", out), contextlib.redirect_stdout(out):
            for reason in ti.REASONS:
                msg = Refusal(reason).message
                self.assertTrue(msg.isascii() and msg.isprintable(), reason)
                print(reason, msg)
            print(Refusal("no-such").message)
            out.flush()
        self.assertIn(b"too_large", raw.getvalue())

    def test_no_exception_escapes(self):
        with mock.patch.object(ti, "_too_deep", return_value=False):
            self.refused(b"[" * 5000 + b"]" * 5000, "too_deep")
        with mock.patch.object(ti, "_check", side_effect=KeyError("x")):
            self.refused(rec("question"), "internal")


class TalkInboxNegative(unittest.TestCase):
    refused = TalkInboxAcceptance.refused

    def test_n1_target_fields(self):
        for name in ("path", "target_file", "target", "command", "cmd", "key1", "backlog",
                     "allowlist", "allowlist_entry", "Path", "target-file", "ALLOW_LIST", "file", "script"):
            for kind in VALID:
                self.refused(rec(kind, **{name: "x"}), "forbidden_field")

    def test_n2_trust_claims(self):
        for name, value in (("approved", True), ("applied", "yes"), ("trusted", True), ("by", "salih"),
                            ("By", "Salih"), ("approved_by", "salih"), ("is-approved", "true")):
            self.refused(rec("decision", **{name: value}), "forbidden_field")
        for name in ("by: salih", "by_salih", "B_Y", "signed by"):
            self.refused(rec("decision", **{name: "x"}), "forbidden_field")

    def test_n3_turn_ids(self):
        for bad in ("../t1", "a/b", "a\\b", "t.1", "t:1", "t 1", "t\u202e1", "\u2066t1", "x" * 65,
                    "", "t1\n", "\uff11", "C:t1", "--exec", "_t", 5, None, True):
            self.refused(rec(cited_turns=[bad]), "bad_turn_id")
        self.refused(rec(cited_turns=["t1", "t1"]), "bad_turn_id")
        self.refused(rec(cited_turns=["t%d" % i for i in range(ti.MAX_TURNS + 1)]), "bad_turn_id")
        self.assertIsInstance(validate_record(rec(cited_turns=["x" * 64])), InboxRecord)

    def test_n4_hostile_encodings(self):
        self.refused(b'{"kind":"question","kind":"question","question":"x"}', "duplicate_key")
        self.refused(b'{"kind":"question","question":"\xff"}', "bad_encoding")
        self.refused(b'{"kind":"question","question":"\xed\xa0\x80"}', "bad_encoding")
        for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
            self.refused(bom + rec("question"), "bom")
        self.refused(rec("question") + b"{}", "trailing_data")
        self.refused(rec("question") + b" x", "trailing_data")
        self.refused(b'{"kind":"question","question":{"a":["b"]}}', "too_deep")
        self.refused(b"[" * 4000, "too_deep")
        self.refused(b'{"kind":"question","question":NaN}', "bad_json")
        self.refused(b"", "bad_json")
        self.refused(b'"decision"', "not_object")
        self.refused(b'["decision"]', "not_object")
        self.refused(b'{"kind":"question","question":{"a":"b"}}', "bad_text")
        self.assertIsInstance(validate_record(b' \n' + rec("question") + b'\r\n'), InboxRecord)

    def test_n5_class_never_guessed(self):
        self.refused(rec(summary="class: authority", **{"class": ...}), "class_missing")
        for bad in ("Authority", " authority", "auth", "authority\n", "", True, None, ["authority"]):
            self.refused(rec(**{"class": bad}), "bad_class")

    def test_n6_over_cap_refused_before_parsing(self):
        data = b"{" + b"x" * ti.MAX_RECORD_BYTES
        with mock.patch.object(ti.json, "JSONDecoder", side_effect=AssertionError("parsed")), \
                mock.patch.object(ti, "_too_deep", side_effect=AssertionError("scanned")):
            self.refused(data, "too_large")
            self.refused(b"\xff" * (ti.MAX_RECORD_BYTES + 1), "too_large")


if __name__ == "__main__":
    unittest.main()
