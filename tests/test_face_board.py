"""Board-reader coverage using synthetic text and the producer's JSON keys."""
import ast
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from jarvis.face import board
from tests.adversarial import HOSTILE_PATH_PARTS, deep_json

NOW = datetime.fromisoformat("2026-11-27T00:35:00+03:00")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOOD = {
    "v": 1,
    "updated": "2026-11-27T00:30:01+03:00",
    "conveyor": {"lanes_max": 8},
    "usage": {"pct": 63, "plan_pct": 60.0, "reset_at": "2026-12-03T22:00:00+03:00",
              "projected_pct_at_reset": 105.5},
    "lanes": [{"id": "A1", "title": "Örnek iş", "activity": "Kontrol"},
              {"id": "A2", "title": "Örnek iki", "activity": "Hazırlık"}],
    "queue": [{"title": "Sonraki iş"}],
    "attention": [{"id": "A3", "reason": "Örnek karar", "since": "2026-11-27T00:00:00+03:00"}],
    "today": {"finished": 9, "passed": 7},
}


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory(dir=FIXTURES)))
        self.pano = self.scratch / "build" / "pano"
        self.pano.mkdir(parents=True)
        self.path = self.pano / "conveyor_board.json"

    def load(self, payload=GOOD, *, now=NOW):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return board.read(self.path, now, pano_dir=self.pano)

    def test_maps_the_producer_shape_and_formats_turkish(self):
        result = self.load()
        self.assertEqual(board.Board(
            lanes_running=2, lanes_max=8, waiting_for_salih=1, usage_pct=63, plan_pct=60,
            reset_at=datetime.fromisoformat(GOOD["usage"]["reset_at"]), projected_pct=105.5,
            today_finished=9, today_passed=7,
            lanes=(board.Lane("A1", "Örnek iş", "Kontrol"), board.Lane("A2", "Örnek iki", "Hazırlık")),
            queue_head=("Sonraki iş",), updated=datetime.fromisoformat(GOOD["updated"]), stale=False,
        ), result)
        with self.assertRaises(AttributeError):
            result.lanes[0].doing = "changed"
        with self.assertRaises(AttributeError):
            result.usage_pct = 0
        view = board.to_view(result)
        self.assertIs(type(view), dict)
        self.assertTrue(all(type(value) is str for value in view.values()))
        self.assertEqual("Hat 2/8", view["lanes"])
        self.assertEqual("Haftalık kota %63 · plan %60", view["usage"])
        self.assertEqual("bilinmiyor", board.to_view(board.Board())["usage"])

    def test_each_bad_source_key_only_affects_its_own_fields(self):
        baseline = self.load()
        matrix = (
            (("updated",), {"updated": None, "stale": True}, 3),
            (("conveyor",), {"lanes_max": None}, "bad"),
            (("conveyor", "lanes_max"), {"lanes_max": None}, "8"),
            (("attention",), {"waiting_for_salih": None}, {}),
            (("usage",), dict.fromkeys(("usage_pct", "plan_pct", "reset_at", "projected_pct")), "bad"),
            (("usage", "pct"), {"usage_pct": None}, "63"),
            (("usage", "plan_pct"), {"plan_pct": None}, "60"),
            (("usage", "reset_at"), {"reset_at": None}, 3),
            (("usage", "projected_pct_at_reset"), {"projected_pct": None}, "105"),
            (("today",), {"today_finished": None, "today_passed": None}, "bad"),
            (("today", "finished"), {"today_finished": None}, "9"),
            (("today", "passed"), {"today_passed": None}, "7"),
            (("lanes",), {"lanes": None, "lanes_running": None}, {}),
            (("lanes", 0, "id"), {"lanes": (replace(baseline.lanes[0], id=None), *baseline.lanes[1:])}, {}),
            (("lanes", 0, "title"), {"lanes": (replace(baseline.lanes[0], title=None), *baseline.lanes[1:])}, {}),
            (("lanes", 0, "activity"), {"lanes": (replace(baseline.lanes[0], doing=None), *baseline.lanes[1:])}, {}),
            (("queue",), {"queue_head": None}, {}),
            (("queue", 0, "title"), {"queue_head": (None,)}, {}),
        )
        self.assertIs(board.UNKNOWN, None)
        for keys, changes, wrong_type in matrix:
            inputs = (("wrong type", wrong_type), ("boolean", True), ("huge integer", 10**400),
                      ("NaN", float("nan")), ("negative", -1), ("nested list", [[1]]),
                      ("1 MB string", "a" * 1_000_000), ("missing", None))
            for case, value in inputs:
                with self.subTest(keys=keys, case=case):
                    payload = deepcopy(GOOD)
                    obj = payload
                    for key in keys[:-1]:
                        obj = obj[key]
                    if case == "missing":
                        del obj[keys[-1]]
                    else:
                        obj[keys[-1]] = value
                    self.assertEqual(replace(baseline, **changes), self.load(payload))

    def test_caps_display_lists_and_refuses_inconsistent_counts_and_percentages(self):
        payload = deepcopy(GOOD)
        payload["lanes"] *= 5
        payload["queue"] *= 6
        result = self.load(payload)
        self.assertIs(result.lanes_running, board.UNKNOWN)
        self.assertEqual(8, result.lanes_max)
        self.assertEqual(8, len(result.lanes))
        self.assertEqual(5, len(result.queue_head))
        for value in (-0.01, 200.01, float("inf")):
            payload["usage"].update(pct=value, plan_pct=value, projected_pct_at_reset=value)
            result = self.load(payload)
            self.assertIs(result.usage_pct, board.UNKNOWN)
            self.assertIs(result.plan_pct, board.UNKNOWN)
            self.assertIs(result.projected_pct, board.UNKNOWN)
        payload["conveyor"]["lanes_max"] = 10
        payload["usage"].update(pct=0, plan_pct=200, projected_pct_at_reset=200)
        result = self.load(payload)
        self.assertEqual((10, 0, 200, 200), (result.lanes_running, result.usage_pct,
                                             result.plan_pct, result.projected_pct))

    def test_stale_boundaries(self):
        for age, stale in ((timedelta(minutes=15), False), (timedelta(minutes=15, seconds=1), True),
                           (timedelta(seconds=-1), True), (timedelta(), False)):
            with self.subTest(age=age):
                self.assertIs(self.load(now=datetime.fromisoformat(GOOD["updated"]) + age).stale, stale)
        self.assertTrue(self.load(now=NOW.replace(tzinfo=None)).stale)
        for stamp in ("2026-02-30T00:30:01+03:00", "2026-11-27T00:30:01+00:99", "2026-11-27T00:30:01"):
            result = self.load({**GOOD, "updated": stamp})
            self.assertIs(result.updated, board.UNKNOWN)
            self.assertTrue(result.stale)

    def test_oversized_integer_and_duplicate_key_are_isolated(self):
        payload = json.dumps(GOOD)
        for pct in ("1" + "0" * 5000, '10, "pct": 20'):
            self.path.write_text(payload.replace('"pct": 63', f'"pct": {pct}'), encoding="utf-8")
            result = board.read(self.path, NOW, pano_dir=self.pano)
            self.assertIs(result.usage_pct, board.UNKNOWN)
            self.assertEqual(60, result.plan_pct)

    def test_unreadable_oversized_or_bad_json_returns_empty_board(self):
        self.assertEqual(board.Board(), board.read(self.path, NOW, pano_dir=self.pano))
        self.assertEqual(board.Board(), board.read(self.pano, NOW, pano_dir=self.pano))
        for raw in (b"", b"{", b'{"today":', b"broken", b"null", b"[]", b"\xff", deep_json().encode()):
            with self.subTest(raw=raw[:12]):
                self.path.write_bytes(raw)
                self.assertEqual(board.Board(), board.read(self.path, NOW, pano_dir=self.pano))
        with patch.object(Path, "open") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b"x" * (1024 * 1024 + 1)
            self.assertEqual(board.Board(), board.read(self.path, NOW, pano_dir=self.pano))
            opened.assert_called_once_with("rb")
            opened.return_value.__enter__.return_value.read.assert_called_once_with(1024 * 1024 + 1)

    def test_hostile_paths_are_refused_before_open(self):
        self.load()
        sibling = self.pano.with_name("pano-other")
        candidates = [str(sibling / self.path.name), f"//host/share/{self.path.name}"]
        candidates.extend(f"{self.pano.as_posix()}/{part}/{self.path.name}" for part in HOSTILE_PATH_PARTS)
        with patch.object(Path, "open") as opened:
            for candidate in candidates:
                with self.subTest(candidate=candidate):
                    self.assertEqual(board.Board(), board.read(candidate, NOW, pano_dir=self.pano))
            opened.assert_not_called()
        with patch.object(Path, "resolve", side_effect=(self.pano, sibling / self.path.name)):
            with patch.object(Path, "open") as opened:
                self.assertEqual(board.Board(), board.read(self.path, NOW, pano_dir=self.pano))
                opened.assert_not_called()

    def test_reader_has_only_one_read_only_open_and_no_mutation_or_external_calls(self):
        source = ast.parse(Path(board.__file__).read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(source) if isinstance(node, ast.Call)]
        opens = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "open"]
        self.assertEqual(1, len(opens))
        self.assertEqual("rb", ast.literal_eval(opens[0].args[0]))
        forbidden = {"open", "write", "write_text", "write_bytes", "touch", "unlink", "rename", "replace",
                     "mkdir", "rmdir", "system", "Popen", "run", "connect", "urlopen", "socket", "exec", "eval"}
        for node in calls:
            if node in opens:
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            self.assertNotIn(name, forbidden)
