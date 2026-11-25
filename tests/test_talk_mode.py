import ast
from pathlib import Path
import unittest

from jarvis import talk_mode
from jarvis.talk_mode import (ModeState, Refusal, change, current_mode, parse_button,
                              telegram_keyboard, wants_voice)
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, each

YAZILI_LABEL = "Yazılı cevap"


def mode(seq, view, value, who="salih", ts=None):
    entry = {"id": seq, "kind": "system", "who": who, "view": view, "mode": value}
    if ts is not None:
        entry["ts"] = ts
    return entry


def request(view):
    return {"id": 99, "kind": "text", "who": "salih", "source": view, "text": "hi"}


class DeepList(list):
    def __iter__(self):
        raise RecursionError("deep")


class TalkModeTests(unittest.TestCase):
    # Acceptance 1
    def test_default_is_yazili(self):
        self.assertEqual(current_mode([], "pc"), ModeState("yazili", 0))
        self.assertEqual(current_mode([request("pc")], "telegram"), ModeState("yazili", 0))

    def test_latest_change_for_view_wins(self):
        entries = [mode(1, "pc", "sesli"), mode(2, "telegram", "yazili"),
                   mode(3, "pc", "yazili"), mode(4, "pc", "sesli")]
        self.assertEqual(current_mode(entries, "pc").mode, "sesli")
        self.assertEqual(current_mode(entries, "telegram").mode, "yazili")

    # Acceptance 2
    def test_telegram_keyboard_two_persistent_labels(self):
        board = telegram_keyboard()
        labels = [b["text"] for row in board["keyboard"] for b in row]
        self.assertEqual(labels, ["Sesli cevap", "Yazılı cevap"])
        self.assertIs(board["is_persistent"], True)
        board["keyboard"].clear()
        self.assertEqual(len(telegram_keyboard()["keyboard"][0]), 2)

    # Acceptance 3
    def test_parse_button_exact_labels(self):
        self.assertEqual(parse_button("Sesli cevap"), "sesli")
        self.assertEqual(parse_button(YAZILI_LABEL), "yazili")
        for text in ("merhaba", "sesli", "Sesli", "", "Sesli cevap lütfen"):
            self.assertIsNone(parse_button(text))

    # Acceptance 4
    def test_change_is_log_record(self):
        self.assertEqual(change("telegram", "sesli"),
                         {"kind": "system", "who": "salih", "view": "telegram", "mode": "sesli"})
        self.assertEqual(change("mobile", "sesli"), Refusal("unknown view"))
        self.assertEqual(change("pc", "loud"), Refusal("unknown mode"))

    def test_change_round_trips_through_current_mode(self):
        record = dict(change("pc", parse_button("Sesli cevap")), id=7)
        self.assertEqual(current_mode([record], "pc").mode, "sesli")
        self.assertTrue(wants_voice(request("pc"), [record]))

    # Acceptance 5
    def test_wants_voice_only_own_view_sesli(self):
        entries = [mode(1, "pc", "sesli")]
        self.assertTrue(wants_voice(request("pc"), entries))
        self.assertFalse(wants_voice(request("telegram"), entries))
        self.assertFalse(wants_voice(request("pc"), [mode(1, "pc", "yazili")]))
        self.assertFalse(wants_voice(request("pc"), []))

    # Negative 1
    def test_lookalike_labels_not_mode_change(self):
        lookalikes = ["sesli cevap", "SESLI CEVAP", " Sesli cevap", "Sesli cevap ",
                      "Sesli  cevap", "Sesli cevap", "Sesli　cevap",
                      "Sеsli cevap", "Yazili cevap", "YAZILI CEVAP",
                      "Yazılı Cevap", "Yazi̇li̇ cevap", "Sesli cevap\n"]
        lookalikes += [label[:3] + bad + label[3:]
                       for label in ("Sesli cevap", YAZILI_LABEL) for bad in HOSTILE_TEXT]
        lookalikes += [None, 1, b"Sesli cevap", ["Sesli cevap"]]

        class Sneaky(str):
            def __eq__(self, other):
                return True
            __hash__ = str.__hash__
        lookalikes.append(Sneaky("Sesli cevap"))
        each(self, lookalikes, lambda text: self.assertIsNone(parse_button(text)))

    # Negative 2
    def test_mode_entry_not_from_salih_ignored_and_counted(self):
        def check(who):
            entries = [mode(1, "pc", "yazili"), mode(2, "pc", "sesli", who=who)]
            self.assertEqual(current_mode(entries, "pc"), ModeState("yazili", 1))
            self.assertFalse(wants_voice(request("pc"), entries))
        each(self, ["jarvis", "system", "Salih", "salih ", None, 1] + list(HOSTILE_TEXT), check)

    # Negative 3
    def test_unknown_view_or_mode_ignored_counted_default_kept(self):
        bad = ["mobile", "PC", "pc ", "", None, 1, ["pc"]] + list(HOSTILE_TEXT)
        each(self, bad, lambda v: self.assertEqual(
            current_mode([mode(1, v, "sesli")], "pc"), ModeState("yazili", 1)))
        bad_modes = ["SESLI", "voice", "", None, True, ["sesli"]] + list(HOSTILE_TEXT)
        each(self, bad_modes, lambda m: self.assertEqual(
            current_mode([mode(1, "pc", m)], "pc"), ModeState("yazili", 1)))

    def test_bad_sequence_ignored_and_counted(self):
        each(self, HOSTILE_NUMBERS, lambda seq: self.assertEqual(
            current_mode([mode(seq, "pc", "sesli")], "pc"), ModeState("yazili", 1)))

    def test_unknown_requested_view_refused(self):
        each(self, ["mobile", "", None, 1] + list(HOSTILE_TEXT), lambda v: self.assertEqual(
            current_mode([mode(1, "pc", "sesli")], v), Refusal("unknown view")))
        self.assertFalse(wants_voice({"source": "mobile"}, [mode(1, "pc", "sesli")]))

    # Negative 4
    def test_sequence_beats_timestamp(self):
        entries = [mode(5, "pc", "sesli", ts="2026-01-01T00:00:00Z"),
                   mode(2, "pc", "yazili", ts="2030-01-01T00:00:00Z")]
        self.assertEqual(current_mode(entries, "pc").mode, "sesli")
        self.assertEqual(current_mode(list(reversed(entries)), "pc").mode, "sesli")

    # Negative 5
    def test_views_never_cross(self):
        self.assertFalse(wants_voice(request("telegram"), [mode(1, "pc", "sesli")]))
        self.assertFalse(wants_voice(request("pc"), [mode(1, "telegram", "sesli")]))
        both = [mode(1, "pc", "sesli"), mode(2, "telegram", "yazili")]
        self.assertTrue(wants_voice(request("pc"), both))
        self.assertFalse(wants_voice(request("telegram"), both))

    # Common rules
    def test_no_exception_escapes(self):
        self.assertEqual(current_mode(DeepList(), "pc"), Refusal("entries unreadable"))
        self.assertEqual(current_mode(None, "pc"), Refusal("entries unreadable"))
        self.assertFalse(wants_voice(request("pc"), DeepList()))
        for entry in (None, "pc", {"source": None}, {"source": ["pc"]}, DeepList()):
            self.assertFalse(wants_voice(entry, [mode(1, "pc", "sesli")]))
        self.assertEqual(current_mode([1, "x", None, request("pc")], "pc"), ModeState("yazili", 0))

    def test_refusal_texts_ascii(self):
        for refusal in (change("x", "sesli"), change("pc", "x"), current_mode([], "x"),
                        current_mode(DeepList(), "pc")):
            self.assertIsInstance(refusal, Refusal)
            self.assertTrue(refusal.reason.isascii())

    def test_deterministic(self):
        entries = [mode(3, "pc", "sesli"), mode(1, "pc", "yazili"), mode(3, "x", "sesli")]
        self.assertEqual({current_mode(list(entries), "pc") for _ in range(5)},
                         {ModeState("sesli", 1)})

    def test_no_io_or_network_imports(self):
        banned = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess",
                  "shutil", "tempfile", "sqlite3", "requests", "ssl", "asyncio"}
        tree = ast.parse(Path(talk_mode.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                    "open", "__import__", "eval", "exec"):
                self.fail(f"forbidden call {node.func.id}")
        self.assertFalse(names & banned, names & banned)

    def test_running_app_does_not_import_module(self):
        root = Path(talk_mode.__file__).parent
        users = [p.name for p in root.rglob("*.py")
                 if p.name != "talk_mode.py" and "talk_mode" in p.read_text(encoding="utf-8")]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
