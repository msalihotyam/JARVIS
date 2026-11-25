"""Tests for jarvis.talk_segments: pure segment rotation and chain verify (no files)."""

import ast
import hashlib
import json
import math
import unittest
from pathlib import Path

from jarvis import talk_segments as ts
from tests import adversarial

ROOT = Path(__file__).resolve().parents[1]
GENESIS = "0" * 64
LIMITS = {"max_bytes": 1000, "max_entries": 3}


def enc(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def seal(record, previous):
    digest = hashlib.sha256(previous.encode("ascii") + enc(record)).hexdigest()
    return enc({"hash": digest, "record": record}) + b"\n", digest


def header(prev, count, digest):
    return enc({"segment": {"count": count, "hash": digest, "prev": prev}}) + b"\n"


def name(number):
    return f"talk-{number:04d}.jsonl"


def build(counts):
    """[(name, bytes)]: segment k+1 holds counts[k] records; ids and hashes run on."""
    segments, previous, next_id = [], GENESIS, 1
    for number, count in enumerate(counts, 1):
        body = b"" if number == 1 else header(name(number - 1), counts[number - 2], previous)
        for _ in range(count):
            line, previous = seal({"id": next_id, "entry": {"text": f"m{next_id}"}}, previous)
            body, next_id = body + line, next_id + 1
        segments.append((name(number), body))
    return segments


def hash_of(line):
    return json.loads(line)["hash"]


class NameTests(unittest.TestCase):
    def refused(self, result, code):
        self.assertIs(type(result), ts.Refusal)
        self.assertEqual(result.code, code)

    def test_next_name_is_zero_padded_and_monotonic(self):
        self.assertEqual(ts.next_name([]), "talk-0001.jsonl")
        self.assertEqual(ts.next_name(("talk-0002.jsonl", "talk-0001.jsonl")), "talk-0003.jsonl")
        names = []
        for _ in range(12):
            names.append(ts.next_name(names))
        self.assertEqual(names, [name(n) for n in range(1, 13)])

    def test_more_than_9999_segments_sort_by_number(self):
        names = [name(n) for n in range(1, 10002)]
        text_order = sorted(names)
        self.assertNotEqual(text_order, names)  # "talk-10000" < "talk-9999" as text
        self.assertEqual(ts.order(text_order), tuple(names))
        self.assertEqual(ts.next_name(text_order), "talk-10002.jsonl")

    def test_gap_or_duplicate_number_refused(self):
        self.refused(ts.next_name(["talk-0001.jsonl", "talk-0003.jsonl"]), "SEGMENT_GAP")
        self.refused(ts.next_name(["talk-0002.jsonl"]), "SEGMENT_GAP")
        self.refused(ts.order(["talk-0001.jsonl", "talk-0001.jsonl"]), "SEGMENT_DUPLICATE")
        # talk-00001 would alias talk-0001: refused as non-canonical, never read twice.
        self.refused(ts.next_name(["talk-0001.jsonl", "talk-00001.jsonl"]), "NAME_INVALID")

    def test_hostile_names_refused(self):
        names = adversarial.HOSTILE_PATH_PARTS + adversarial.HOSTILE_TEXT + (
            "../talk-0001.jsonl", "dir/talk-0001.jsonl", "dir\\talk-0001.jsonl",
            "C:talk-0001.jsonl", "talk-0001.jsonl:x", "talk-0001.jsonl::$DATA",
            "TALK-0~1.JSO", "talk-~1.jsonl", "talk-0001~1.jsonl",
            "talk-０００１.jsonl",  # full-width digits
            "talk-٠٠٠١.jsonl",  # Arabic-Indic digits
            "talk-0000.jsonl", "talk-0001.jsonl\n", "talk-0001.jsonl ", "talk-0001.jsonl.",
            "talk-0001.JSONL", "Talk-0001.jsonl", "talk-1.jsonl", "talk-0001", "",
        )

        def check(value):
            self.refused(ts.next_name([value]), "NAME_INVALID")
            self.refused(ts.order([value]), "NAME_INVALID")

        adversarial.each(self, names, check)

    def test_names_equal_after_case_folding_refused(self):
        self.refused(ts.next_name(["talk-0001.jsonl", "TALK-0001.JSONL"]), "NAME_DUPLICATE")
        self.refused(ts.order(["Talk-0002.jsonl", "talk-0001.jsonl", "talk-0002.jsonl"]),
                     "NAME_DUPLICATE")

    def test_non_list_or_non_str_refused(self):
        class Sub(str):
            pass

        for bad in ("talk-0001.jsonl", None, {"talk-0001.jsonl"}, 1):
            self.refused(ts.next_name(bad), "NAMES_INVALID")
        for bad in (Sub("talk-0001.jsonl"), b"talk-0001.jsonl", 1, None):
            self.refused(ts.next_name([bad]), "NAME_INVALID")


class RollTests(unittest.TestCase):
    def refused(self, result, code):
        self.assertIs(type(result), ts.Refusal)
        self.assertEqual(result.code, code)

    def test_rolls_when_either_limit_is_reached(self):
        self.assertIs(ts.should_roll(999, 2, LIMITS), False)
        self.assertIs(ts.should_roll(1000, 1, LIMITS), True)
        self.assertIs(ts.should_roll(10, 3, LIMITS), True)

    def test_empty_segment_never_rolls(self):
        self.assertIs(ts.should_roll(0, 0, LIMITS), False)
        self.assertIs(ts.should_roll(5000, 0, LIMITS), False)  # a header alone

    def test_zero_negative_nan_limits_refused(self):
        bad = (0, -1, math.nan, math.inf, 1.0, 2**63) + adversarial.HOSTILE_NUMBERS

        def check(value):
            for key in LIMITS:
                self.refused(ts.should_roll(1, 1, {**LIMITS, key: value}), "LIMITS_INVALID")

        adversarial.each(self, bad, check)
        for limits in ({}, {"max_bytes": 5}, {**LIMITS, "x": 1}, [("max_bytes", 5)], None):
            self.refused(ts.should_roll(1, 1, limits), "LIMITS_INVALID")

    def test_bad_counts_refused(self):
        def check(value):
            self.refused(ts.should_roll(value, 1, LIMITS), "COUNT_INVALID")
            self.refused(ts.should_roll(1, value, LIMITS), "COUNT_INVALID")

        adversarial.each(self, (-1, 1.0, math.nan, True, "1", None, 10**400), check)


class HeaderTests(unittest.TestCase):
    def test_first_segment_has_no_header(self):
        self.assertEqual(ts.roll([]), ts.Next("talk-0001.jsonl", b""))

    def test_new_segment_header_names_previous_segment_hash_and_count(self):
        segments = build([2])
        result = ts.roll(segments)
        last = segments[0][1].split(b"\n")[-2]
        self.assertEqual(result.name, "talk-0002.jsonl")
        self.assertTrue(result.header.endswith(b"\n"))
        self.assertEqual(result.header.count(b"\n"), 1)
        self.assertEqual(json.loads(result.header), {"segment": {
            "prev": "talk-0001.jsonl", "hash": hash_of(last), "count": 2}})
        # The rolled header plus the next sealed record verifies; segment 1 is untouched.
        line, digest = seal({"id": 3, "entry": {"text": "m3"}}, hash_of(last))
        rolled = segments + [(result.name, result.header + line)]
        self.assertEqual(ts.verify(rolled), ts.Ok(2, 3, 3, "talk-0002.jsonl", digest, 1))
        self.assertEqual(rolled[0], build([2])[0])

    def test_roll_refuses_after_empty_or_broken_segment(self):
        refusal = ts.roll(build([0]))
        self.assertEqual((type(refusal), refusal.code), (ts.Refusal, "SEGMENT_EMPTY"))
        broken = ts.roll([("talk-0001.jsonl", b"x\n")])
        self.assertEqual((type(broken), broken.code), (ts.Break, "RECORD_INVALID"))

    def test_seal_matches_talk_log(self):
        from jarvis import talk_log

        record = {"id": 1, "entry": {"text": "çay İ"}}
        self.assertEqual(ts._seal(record, GENESIS), talk_log._seal(record, GENESIS))
        self.assertEqual(ts._seal(record, GENESIS), seal(record, GENESIS))


class VerifyTests(unittest.TestCase):
    def broke(self, segments, code, segment, line):
        result = ts.verify(segments)
        self.assertIs(type(result), ts.Break, result)
        self.assertEqual((result.code, result.segment, result.line), (code, segment, line))
        self.assertEqual(result.text, ts.REFUSALS[code])

    def test_chain_runs_across_segments_in_number_order(self):
        segments = build([3, 2, 1])
        last = hash_of(segments[-1][1].split(b"\n")[-2])
        expected = ts.Ok(3, 6, 6, "talk-0003.jsonl", last, 1)
        self.assertEqual(ts.verify(segments), expected)
        self.assertEqual(ts.verify(tuple(reversed(segments))), expected)
        self.assertEqual(ts.verify([]), ts.Ok(0, 0, 0, "", GENESIS, 0))

    def test_gap_or_duplicate_segment_number_fails_there(self):
        segments = build([1, 1, 1, 1])
        self.broke(segments[:2] + segments[3:], "SEGMENT_GAP", "talk-0004.jsonl", 0)
        self.broke(segments + [segments[1]], "SEGMENT_DUPLICATE", "talk-0002.jsonl", 0)
        self.broke(segments[1:], "SEGMENT_GAP", "talk-0002.jsonl", 0)

    def test_header_hash_count_or_prev_mismatch_names_segment(self):
        segments = build([2, 1])
        lines = segments[1][1].split(b"\n")
        good = json.loads(lines[0])["segment"]
        for bad in ({**good, "count": 1}, {**good, "count": 3}, {**good, "hash": "f" * 64},
                    {**good, "prev": "talk-0009.jsonl"}, {**good, "extra": 1}):
            with self.subTest(bad=bad):
                body = enc({"segment": bad}) + b"\n" + b"\n".join(lines[1:])
                self.broke([segments[0], ("talk-0002.jsonl", body)],
                           "HEADER_MISMATCH", "talk-0002.jsonl", 1)

    def test_old_segment_grown_after_roll_fails_at_next_header(self):
        segments = build([2, 1])
        extra, _ = seal({"id": 3, "entry": {"text": "late"}}, hash_of(segments[0][1].split(b"\n")[-2]))
        grown = [(segments[0][0], segments[0][1] + extra), segments[1]]
        self.broke(grown, "HEADER_MISMATCH", "talk-0002.jsonl", 1)

    def test_header_line_anywhere_but_first_is_tampering(self):
        segments = build([2, 1])
        head, record = segments[1][1].split(b"\n")[:2]
        self.broke([segments[0], ("talk-0002.jsonl", head + b"\n" + head + b"\n" + record + b"\n")],
                   "TAMPER", "talk-0002.jsonl", 2)
        self.broke([segments[0], ("talk-0002.jsonl", head + b"\n" + record + b"\n" + head + b"\n")],
                   "TAMPER", "talk-0002.jsonl", 3)
        first = segments[0][1].split(b"\n")
        self.broke([("talk-0001.jsonl", head + b"\n" + segments[0][1])], "TAMPER", "talk-0001.jsonl", 1)
        self.broke([("talk-0001.jsonl", b"\n".join([first[0], head, first[1], b""]))],
                   "TAMPER", "talk-0001.jsonl", 2)

    def test_missing_header_torn_line_and_edited_record(self):
        segments = build([2, 1])
        record = segments[1][1].split(b"\n", 1)[1]
        self.broke([segments[0], ("talk-0002.jsonl", record)], "HEADER_MISSING", "talk-0002.jsonl", 1)
        self.broke([segments[0], ("talk-0002.jsonl", b"")], "HEADER_MISSING", "talk-0002.jsonl", 1)
        self.broke([(segments[0][0], segments[0][1][:-1])], "LINE_TORN", "talk-0001.jsonl", 2)
        self.broke([(segments[0][0], segments[0][1].replace(b"m1", b"mX"))],
                   "HASH_MISMATCH", "talk-0001.jsonl", 1)
        self.broke([segments[0], (segments[1][0], segments[1][1].replace(b"m3", b"mX"))],
                   "HASH_MISMATCH", "talk-0002.jsonl", 2)

    def test_ids_must_keep_rising_across_segments(self):
        one, h1 = seal({"id": 1, "entry": {}}, GENESIS)
        three, _ = seal({"id": 3, "entry": {}}, h1)
        self.broke([("talk-0001.jsonl", one + three)], "ID_OUT_OF_ORDER", "talk-0001.jsonl", 2)
        restart, _ = seal({"id": 1, "entry": {}}, h1)
        second = header("talk-0001.jsonl", 1, h1) + restart
        self.broke([("talk-0001.jsonl", one), ("talk-0002.jsonl", second)],
                   "ID_OUT_OF_ORDER", "talk-0002.jsonl", 2)

    def test_empty_segment_before_the_last_fails(self):
        self.broke(build([1, 0, 1]), "SEGMENT_EMPTY", "talk-0002.jsonl", 0)

    def test_hostile_lines_give_a_typed_break(self):
        deep = adversarial.deep_json()
        texts = (deep, '{"hash":"x","record":' + deep + "}") + adversarial.HOSTILE_JSON \
            + adversarial.HOSTILE_TEXT

        def check(text):
            body = text.encode("utf-8", "surrogatepass").replace(b"\n", b" ") + b"\n"
            result = ts.verify([("talk-0001.jsonl", body)])
            self.assertIs(type(result), ts.Break, result)
            self.assertIn(result.code, {"RECORD_INVALID", "HASH_MISMATCH", "TAMPER"})

        adversarial.each(self, texts, check)

    def test_bad_input_shapes_refused_or_named(self):
        for bad in (None, "talk-0001.jsonl", {"talk-0001.jsonl": b""},
                    [("talk-0001.jsonl", "text")], [["talk-0001.jsonl", b""]],
                    [("talk-0001.jsonl", b"", 1)], [("talk-0001.jsonl", bytearray())]):
            with self.subTest(bad=bad):
                result = ts.verify(bad)
                self.assertEqual((type(result), result.code), (ts.Refusal, "SEGMENTS_INVALID"))
        self.broke([(1, b"")], "NAME_INVALID", "", 0)
        self.broke([("../talk-0001.jsonl", b"")], "NAME_INVALID", "../talk-0001.jsonl", 0)
        self.broke([("talk-0001.jsonl", b""), ("TALK-0001.jsonl", b"")],
                   "NAME_DUPLICATE", "TALK-0001.jsonl", 0)

    def test_more_than_9999_segments_verify_in_number_order(self):
        segments = build([1] * 10001)
        last = hash_of(segments[-1][1].split(b"\n")[-2])
        result = ts.verify(sorted(segments))  # text order puts talk-10000 before talk-9999
        self.assertEqual(result, ts.Ok(10001, 10001, 10001, "talk-10001.jsonl", last, 1))
        self.assertEqual(ts.roll(segments).name, "talk-10002.jsonl")
        self.assertEqual(ts.verify(segments), result)  # deterministic


class PurityTests(unittest.TestCase):
    BANNED = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "requests",
              "shutil", "sqlite3", "importlib", "ctypes", "tempfile"}

    def test_no_io_or_network_import_or_call(self):
        tree = ast.parse((ROOT / "jarvis" / "talk_segments.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], self.BANNED)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], self.BANNED)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "eval", "exec"})

    def test_refusal_texts_are_ascii(self):
        with adversarial.narrow_stdout() as stream:
            for code, text in ts.REFUSALS.items():
                self.assertEqual(code.encode("ascii").decode(), code)
                self.assertEqual(text.encode("ascii").decode(), text)
                print(code, text, file=stream)

    def test_running_app_does_not_import_module(self):
        for path in (ROOT / "jarvis").rglob("*.py"):
            if path.name == "talk_segments.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [alias.name for alias in node.names]
                with self.subTest(path=str(path)):
                    self.assertFalse(any("talk_segments" in name for name in names))


if __name__ == "__main__":
    unittest.main()
