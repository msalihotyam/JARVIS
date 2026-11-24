import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from jarvis import talk_log
from jarvis.talk_log import MAX_RECORD_BYTES, TalkLog, TalkLogError


class TalkLogTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "talk.jsonl"
        self.attachments = Path(directory) / "attachments"
        self.attachments.mkdir()
        self.log = self.open_log()

    def open_log(self):
        return TalkLog(
            self.path, self.attachments,
            clock=lambda: datetime(2026, 11, 24, tzinfo=timezone.utc),
        )

    def append(self, **fields):
        return self.log.append(**{
            "source": "pc", "who": "salih", "kind": "text", "text": "Merhaba",
            **fields,
        })

    def test_entry_round_trip(self):
        attachment = {"sha256": "a" * 64, "path": "images/photo.png"}
        first = self.append(attachments=[attachment], focus="project")
        second = self.append(source="telegram", who="jarvis", reply_to=first["id"])
        self.assertEqual(first, {
            "id": 1, "ts": "2026-11-24T00:00:00+00:00", "source": "pc",
            "who": "salih", "kind": "text", "text": "Merhaba",
            "attachments": [attachment], "focus": "project", "reply_to": None,
            "delivery": {"pc": "pending", "telegram": "pending"},
        })
        self.assertEqual(second["id"], 2)
        self.assertEqual(self.open_log().read(), [first, second])

    def test_concurrent_writers_allocate_ordered_ids(self):
        def append(index):
            return self.open_log().append(
                source="pc", who="salih", kind="text", text=str(index),
            )["id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(append, range(12)))
        self.assertEqual(sorted(ids), list(range(1, 13)))
        self.assertEqual([entry["id"] for entry in self.log.read()], list(range(1, 13)))

    def test_delivery_replays_without_rewriting_entry(self):
        first = self.append()
        before = self.path.read_bytes()
        self.log.set_delivery(first["id"], "telegram", "delivered")
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual(len(self.path.read_bytes().splitlines()), 2)
        self.assertEqual(self.open_log().read()[0]["delivery"], {
            "pc": "pending", "telegram": "delivered",
        })
        self.assertGreater(self.append()["id"], first["id"])

    def test_torn_tail_preserves_readable_prefix_and_blocks_append(self):
        first = self.append()
        with self.path.open("ab") as file:
            file.write(b'{"record":')
        before = self.path.read_bytes()
        self.assertEqual(self.open_log().read(), [first])
        with self.assertRaises(TalkLogError):
            self.append()
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_id_is_refused(self):
        self.append()
        before = self.path.read_bytes()
        with self.assertRaises(TalkLogError):
            self.append(id=1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_edit_to_earlier_record_is_detected(self):
        self.append()
        self.append()
        first, second = self.path.read_bytes().splitlines(keepends=True)
        record = json.loads(first)["record"]
        record["entry"]["text"] = "Changed"
        edited, _ = talk_log._seal(record, "0" * 64)
        self.path.write_bytes(edited + second)
        with self.assertRaises(TalkLogError):
            self.open_log().read()

    def test_mid_file_corruption_raises_talk_log_error_and_releases_lock(self):
        for _ in range(3):
            self.append()
        good = self.path.read_bytes()
        lines = good.splitlines(keepends=True)

        def resealed(index, change):
            record = json.loads(lines[index])["record"]
            change(record)
            previous = json.loads(lines[index - 1])["hash"] if index else "0" * 64
            return talk_log._seal(record, previous)[0]

        def set_text(record):
            record["entry"]["text"] = "Changed"

        def add_field(record):
            record["entry"]["extra"] = True

        variants = {
            "edit record 1 of 3": (0, resealed(0, set_text)),
            "edit record 2 of 3": (1, resealed(1, set_text)),
            "invalid json record 1": (0, b"{not json}\n"),
            "unknown field record 1": (0, resealed(0, add_field)),
            "oversized record 1": (0, b"x" * (MAX_RECORD_BYTES + 1) + b"\n"),
        }
        for name, (index, line) in variants.items():
            with self.subTest(name):
                corrupt = b"".join(lines[:index] + [line] + lines[index + 1:])
                self.path.write_bytes(corrupt)
                with self.assertRaises(TalkLogError):
                    self.open_log().read()
                with self.assertRaises(TalkLogError):
                    self.append()
                self.assertEqual(self.path.read_bytes(), corrupt)
                self.path.write_bytes(good)
                self.assertEqual(self.append()["id"], 4)
                self.path.write_bytes(good)

    def test_unknown_fields_and_oversized_entries_are_refused(self):
        with self.assertRaises(TypeError):
            self.append(extra=True)
        with self.assertRaises(TalkLogError):
            self.append(text="ü" * MAX_RECORD_BYTES)
        self.assertEqual(self.log.read(), [])
        self.append()
        record = json.loads(self.path.read_bytes())["record"]
        record["entry"]["extra"] = True
        line, _ = talk_log._seal(record, "0" * 64)
        self.path.write_bytes(line)
        with self.assertRaisesRegex(TalkLogError, "Unknown or missing entry fields"):
            self.log.read()

    def test_attachment_cannot_escape_root(self):
        for path in ("../secret", "images/../../secret", r"..\secret",
                     "/secret", r"C:\secret", "C:secret"):
            with self.subTest(path=path), self.assertRaises(TalkLogError):
                self.append(attachments=[{"sha256": "a" * 64, "path": path}])
        self.assertEqual(self.log.read(), [])

    def test_database_path_and_sqlite_import_are_refused(self):
        for name in ("jarvis.db", "jarvis.db::$DATA"):
            with self.subTest(name=name), self.assertRaises(TalkLogError):
                TalkLog(self.path.parent / "data" / name, self.attachments)
        tree = ast.parse(Path(talk_log.__file__).read_text(encoding="utf-8"))
        imports = [
            name.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for name in node.names
        ] + [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        self.assertFalse(any(name and "sqlite" in name for name in imports))
