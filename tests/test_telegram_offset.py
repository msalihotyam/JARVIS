import ast
import io
import sys
import unittest
import unittest.mock
from pathlib import Path

from jarvis import telegram_offset as tg
from jarvis.telegram_offset import Handled, PollResult, Refusal, dump_state, load_state, process_batch

NOW = 1_800_000_000
FORBIDDEN = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "requests",
             "shutil", "tempfile", "sqlite3", "ssl", "asyncio", "telegram"}


def upd(uid, date=NOW):
    return {"update_id": uid, "message": {"date": date, "text": "x"}}


def run(batch, offset=0, max_age=60):
    return process_batch(batch, offset, now=NOW, max_age=max_age)


def ids(result):
    return [h.update_id for h in result.updates]


class TelegramOffsetTests(unittest.TestCase):
    # Acceptance 1
    def test_ascending_order_and_next_offset(self):
        r = run([upd(12), upd(10), upd(11)], offset=5)
        self.assertIsInstance(r, PollResult)
        self.assertEqual(ids(r), [10, 11, 12])
        self.assertEqual(r.next_offset, 13)
        self.assertIsInstance(r.updates[0], Handled)

    def test_empty_batch_keeps_offset(self):
        self.assertEqual(run([], offset=7).next_offset, 7)

    # Acceptance 2
    def test_replay_below_offset_dropped(self):
        r = run([upd(3), upd(4), upd(5), upd(6)], offset=5)
        self.assertEqual(ids(r), [5, 6])
        self.assertEqual(r.replayed, 2)
        self.assertEqual(r.next_offset, 7)

    def test_duplicates_in_batch_handled_once(self):
        first = upd(9)
        r = run([first, upd(9), upd(8), upd(9)])
        self.assertEqual(ids(r), [8, 9])
        self.assertIs(r.updates[1].update, first)
        self.assertEqual(r.duplicates, 2)

    def test_second_poll_with_same_batch_returns_nothing(self):
        batch = [upd(1), upd(2)]
        r1 = run(batch, offset=0)
        r2 = run(batch, offset=r1.next_offset)
        self.assertEqual(ids(r2), [])
        self.assertEqual(r2.next_offset, r1.next_offset)

    # Acceptance 3
    def test_state_round_trip_is_short_string(self):
        for offset in (0, 1, 123456789, tg.MAX_OFFSET):
            text = dump_state(offset)
            self.assertIsInstance(text, str)
            self.assertLessEqual(len(text), tg.MAX_STATE_CHARS)
            self.assertEqual(load_state(text), offset)
        self.assertEqual(dump_state(42), '{"offset":42,"v":1}')

    def test_dump_state_refuses_bad_offset(self):
        for bad in (-1, True, 1.0, "3", None, tg.MAX_OFFSET + 1):
            self.assertIsInstance(dump_state(bad), Refusal, repr(bad))

    # Acceptance 4
    def test_old_updates_returned_flagged_late(self):
        r = run([upd(1, NOW - 3600), upd(2, NOW - 60), upd(3, NOW), {"update_id": 4}], max_age=60)
        self.assertEqual(ids(r), [1, 2, 3, 4])
        self.assertEqual([h.late for h in r.updates], [True, False, False, False])

    def test_bad_date_is_not_late_and_not_lost(self):
        r = run([upd(1, "yesterday"), upd(2, True), {"update_id": 3, "message": "m"}])
        self.assertEqual(ids(r), [1, 2, 3])
        self.assertFalse(any(h.late for h in r.updates))

    # Acceptance 5
    def test_deterministic(self):
        batch = [upd(5, NOW - 999), upd(3), "junk", upd(3), upd(4)]
        self.assertEqual(run(batch, offset=2), run(batch, offset=2))
        self.assertEqual(run(list(batch), offset=2), run(batch, offset=2))

    # Negative 1
    def test_bad_update_ids_refused_and_counted_others_proceed(self):
        batch = [upd("7"), upd(7.0), upd(True), upd(-1), upd(2**63), upd(2**64),
                 {"no_id": 1}, "str", None, upd(tg.MAX_UPDATE_ID), upd(4)]
        r = run(batch)
        self.assertEqual(ids(r), [4, tg.MAX_UPDATE_ID])
        self.assertEqual(r.next_offset, tg.MAX_OFFSET)
        reasons = dict(r.refused)
        self.assertEqual(len(r.refused), 9)
        self.assertEqual(reasons[0], "update_id_type")
        self.assertEqual(reasons[1], "update_id_type")
        self.assertEqual(reasons[2], "update_id_type")
        self.assertEqual(reasons[3], "update_id_negative")
        self.assertEqual(reasons[4], "update_id_too_large")
        self.assertEqual(reasons[5], "update_id_too_large")
        self.assertEqual(reasons[6], "update_id_missing")
        self.assertEqual(reasons[7], "update_not_mapping")
        self.assertEqual(reasons[8], "update_not_mapping")

    def test_hostile_mapping_refused_not_raised(self):
        class Bad(dict):
            def __contains__(self, key):
                raise RuntimeError("boom")

        r = run([Bad(update_id=1), upd(2)])
        self.assertEqual(ids(r), [2])
        self.assertEqual(r.refused, ((0, "update_unreadable"),))

    # Negative 2
    def test_batch_not_list_refused(self):
        for bad in ((upd(1),), {"result": [upd(1)]}, "[]", None, 3, b"[]"):
            self.assertEqual(run(bad, offset=9), Refusal("batch_not_list"), repr(bad))

    def test_batch_over_limit_refused(self):
        self.assertEqual(run([upd(i) for i in range(1001)]), Refusal("batch_too_large"))
        self.assertIsInstance(run([upd(i) for i in range(1000)]), PollResult)

    def test_bad_offset_and_clock_refused(self):
        for bad in (-1, True, 1.5, "0", None, tg.MAX_OFFSET + 1):
            self.assertEqual(run([upd(1)], offset=bad), Refusal("bad_offset"), repr(bad))
        self.assertEqual(process_batch([], 0, now=1.0, max_age=1), Refusal("bad_clock"))
        self.assertEqual(process_batch([], 0, now=1, max_age=-1), Refusal("bad_clock"))
        self.assertEqual(process_batch([], 0, now=1, max_age=False), Refusal("bad_clock"))

    # Negative 3
    def test_offset_above_every_id(self):
        r = run([upd(1), upd(2), upd(3)], offset=100)
        self.assertEqual(ids(r), [])
        self.assertEqual(r.next_offset, 100)
        self.assertEqual(r.replayed, 3)

    # Negative 4
    def test_state_truncated_wrong_type_extra_fields_refused(self):
        good = dump_state(42)
        for cut in range(len(good)):
            self.assertIsInstance(load_state(good[:cut]), Refusal, good[:cut])
        cases = {
            None: "state_type", 42: "state_type", b'{"offset":1,"v":1}': "state_type",
            '{"offset":1,"v":1,"x":0}': "state_fields", '{"offset":1}': "state_fields",
            '[1,1]': "state_fields", '42': "state_fields",
            '{"offset":1,"v":2}': "state_version", '{"offset":1,"v":true}': "state_version",
            '{"offset":"1","v":1}': "bad_offset", '{"offset":-1,"v":1}': "bad_offset",
            '{"offset":1.0,"v":1}': "bad_offset", '{"offset":false,"v":1}': "bad_offset",
            '{"offset":1,"offset":2,"v":1}': "state_unparsable",
            "x" * 65: "state_too_long",
        }
        for text, reason in cases.items():
            self.assertEqual(load_state(text), Refusal(reason), repr(text)[:80])

    def test_deeply_nested_input_refused(self):
        deep = [upd(1)]
        nested = []
        for _ in range(100_000):
            nested = [nested]
        deep.append(nested)
        r = run(deep)
        self.assertEqual(ids(r), [1])
        self.assertEqual(r.refused, ((1, "update_not_mapping"),))
        # Recursion in the JSON parser is caught (length cap lifted to reach it).
        with unittest.mock.patch.object(tg, "MAX_STATE_CHARS", 10**7):
            self.assertIn(load_state("[" * 200_000), (Refusal("state_too_deep"), Refusal("state_unparsable")))

    def test_recursion_error_inside_update_refused(self):
        class Deep(dict):
            def __contains__(self, key):
                raise RecursionError

        r = run([Deep(), upd(1)])
        self.assertEqual(r.refused, ((0, "update_too_deep"),))

    # Negative 5
    def test_offset_never_moves_backwards(self):
        for batch in ([upd(1)], [], ["junk"], [upd(50), upd(10)], [upd(-5)]):
            r = run(batch, offset=30)
            self.assertGreaterEqual(r.next_offset, 30, repr(batch)[:80])
        self.assertEqual(run([upd(50), upd(10)], offset=30).next_offset, 51)

    # Common rules
    def test_refusal_text_ascii_under_cp1252_stdout(self):
        texts = [str(run("x")), str(load_state(None)), str(Refusal("bad_offset"))]
        texts += [reason for _, reason in run([upd("é"), "ü", upd(-1)]).refused]
        buf = io.BytesIO()
        wrapped = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        old = sys.stdout
        sys.stdout = wrapped
        try:
            for text in texts:
                print(text)
            wrapped.flush()
        finally:
            sys.stdout = old
        for text in texts:
            self.assertTrue(text.isascii() and text.isprintable(), text)
        self.assertIn(b"refused: batch_not_list", buf.getvalue())

    def test_module_imports_no_io_or_network(self):
        source = Path(tg.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "__import__", "exec", "eval"})
        self.assertEqual(names & FORBIDDEN, set())
        self.assertLessEqual(names, {"__future__", "json", "collections", "dataclasses", "typing"})


if __name__ == "__main__":
    unittest.main()
