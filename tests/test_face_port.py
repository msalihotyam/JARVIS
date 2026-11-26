"""FACEPORT1: window view model, closed command set and fake service.

Guarantees under test: present() shows the window's last N rows with labels, chips and
open approval cards; ring/focus/mode come from the caller; commands are a closed set with
typed refusals; no exception escapes; the module imports no Qt, no talk_* and no I/O, and
nothing in jarvis/ imports it.
"""
from __future__ import annotations

import ast
import unicodedata
import unittest
from pathlib import Path

from jarvis.face import port
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, deep_json, each

ROOT = Path(__file__).resolve().parents[1]
TS = "2026-11-25T10:56:00+03:00"
STATUS = {"ring": "thinking"}


def entry(**fields):
    base = {"id": 1, "ts": TS, "source": "pc", "who": "salih", "kind": "text", "text": "merhaba"}
    base.update(fields)
    return base


def view(entries, window="small", focus="Proje", mode="yazili", status=STATUS):
    return port.present(entries, status, focus, mode, window, link="ok")


class Liar(str):
    """str subclass that claims to equal anything."""

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class Alias(str):
    """str key that hashes like another string and equals anything."""

    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash(self.target)


def alias(text, target):
    key = Alias(text)
    key.target = target
    return key


def swap_key(mapping, key, text="zz"):
    """Copy of mapping whose literal key is replaced by an Alias that poses as it."""
    out = {k: v for k, v in mapping.items() if k != key}
    out[alias(text, key)] = mapping[key]
    return out


CARD =entry(id=9, who="jarvis", kind="approval_card", card_id="card-abc", text="Dosya yazilsin mi?")


class PresentTests(unittest.TestCase):
    def test_text_row_fields(self):
        row = view([entry()]).rows[0]
        self.assertEqual((row.who, row.label, row.text, row.time), ("Salih", "", "merhaba", "10:56"))

    def test_telegram_salih_row_is_telefon(self):
        self.assertEqual(view([entry(source="telegram")]).rows[0].label, "Telefon")
        self.assertEqual(view([entry(source="telegram", who="jarvis")]).rows[0].label, "")

    def test_transcript_row_label(self):
        self.assertEqual(view([entry(kind="transcript")]).rows[0].label, "Anladığım:")

    def test_attachment_is_chip(self):
        att = {"sha256": "0" * 64, "path": "C:\\x\\rapor.pdf", "name": "rapor.pdf", "size": 1200}
        chip = view([entry(kind="file", attachments=[att])]).rows[0].chips[0]
        self.assertEqual(chip, port.Chip("rapor.pdf", 1200, "pdf"))

    def test_open_card_row_has_two_buttons(self):
        vm = view([CARD])
        self.assertEqual(vm.rows[0].card_id, "card-abc")
        self.assertEqual(vm.rows[0].buttons, port.BUTTONS)
        self.assertEqual(vm.open_cards, frozenset({"card-abc"}))

    def test_resolved_card_has_no_buttons(self):
        vm = view([dict(CARD, state="APPROVED")])
        self.assertEqual((vm.rows[0].buttons, vm.open_cards), ((), frozenset()))

    def test_status_fields_are_taken_not_recomputed(self):
        for ring in port.RING_STATES:
            vm = port.present([CARD], {"ring": ring}, "Odak", "sesli", "large", link="ok")
            self.assertEqual((vm.ring, vm.focus_label, vm.mode, vm.window), (ring, "Odak", "sesli", "large"))
        self.assertEqual(view([], focus=None).focus_label, "")

    def test_deterministic(self):
        entries = [entry(id=i, text=f"t{i}") for i in range(50)] + [CARD]
        self.assertEqual(view(entries), view(entries))

    def test_bad_inputs_are_refused(self):
        cases = [
            ("x", STATUS, None, "yazili", "small"),
            ([], {"ring": "dancing"}, None, "yazili", "small"),
            ([], None, None, "yazili", "small"),
            ([], STATUS, 7, "yazili", "small"),
            ([], STATUS, None, "loud", "small"),
            ([], STATUS, None, "yazili", 30),
        ]
        for args in cases:
            self.assertIsInstance(port.present(*args), port.Refusal, args)

    def test_window_shows_only_last_n_of_10k(self):
        entries = [entry(id=i) for i in range(10_000)]
        for size, n in port.WINDOW_ROWS.items():
            rows = view(entries, window=size).rows
            self.assertEqual([r.entry_id for r in rows], list(range(10_000 - n, 10_000)))

    def test_window_shows_every_entry_when_log_is_shorter(self):
        for size, n in port.WINDOW_ROWS.items():
            for length in sorted({0, 1, n // 2, n // 2 + 1, n - 1, n, n + 1, 20, 29, 150, 199, 201}):
                for kind in (list, tuple):
                    rows = view(kind(entry(id=i) for i in range(length)), window=size).rows
                    want = list(range(max(0, length - n), length)) if n else []
                    self.assertEqual([r.entry_id for r in rows], want, (size, length, kind))

    def test_str_subclass_never_matches_a_known_value(self):
        for fields in [{"kind": Liar("hologram")}, {"who": Liar("mallory")}, {"kind": Liar("text")}]:
            self.assertEqual(view([entry(**fields)]).rows[0].kind, "unsupported", fields)
        self.assertEqual(view([entry(source=Liar("pc"))]).rows[0].label, "")
        vm = view([dict(CARD, state=Liar("APPROVED"))])
        self.assertEqual((vm.rows[0].buttons, vm.open_cards), ((), frozenset()))
        att = {"name": "x.png", "kind": Liar("exe")}
        self.assertEqual(view([entry(kind="file", attachments=[att])]).rows[0].chips[0].kind, "png")
        self.assertIsInstance(port.present([], {"ring": Liar("dancing")}, None, "yazili", "small"), port.Refusal)
        self.assertIsInstance(port.present([], STATUS, None, Liar("loud"), Liar("huge")), port.Refusal)

    def test_liar_window_alone_is_refused(self):
        # every other input valid, so only the exact-type guard on window can refuse
        self.assertIsInstance(port.present([], STATUS, None, "yazili", "small", link="ok"), port.ViewModel)
        self.assertIsInstance(port.present([], STATUS, None, "yazili", Liar("small")), port.Refusal)

    def test_records_refuse_delete(self):
        vm = view([CARD])
        cmd = port.validate({"name": "card_tap", "card_id": "card-abc", "choice": "approve"}, vm)
        self.assertIsInstance(cmd, port.Command)
        for record, field in [(vm, "rows"), (vm, "open_cards"), (vm.rows[0], "card_id"), (cmd, "name")]:
            with self.assertRaises(AttributeError, msg=(type(record).__name__, field)):
                delattr(record, field)
            self.assertTrue(hasattr(record, field), field)

    def test_liar_who_or_kind_never_renders_a_real_row(self):
        # each case renders a real row (or an open card) if the exact-type guard on kind/who is loosened
        for fields in [{"who": Liar("salih")}, {"who": Liar("jarvis")}, {"kind": Liar("text"), "card_id": "card-x"},
                       {"kind": Liar("approval_card"), "card_id": "card-x"}]:
            vm = view([entry(**fields)])
            row = vm.rows[0]
            self.assertIs(type(row.kind), str, fields)
            self.assertEqual((row.kind, row.who, row.text, row.card_id, row.buttons, vm.open_cards),
                             ("unsupported", "Sistem", port.UNSUPPORTED, None, (), frozenset()), fields)

    def test_container_subclass_is_refused(self):
        class Many(list):
            def __getitem__(self, index):
                return [entry(id=i) for i in range(500)]

        for kind in (list, tuple):
            sub = type("Sub", (kind,), {})
            self.assertIsInstance(port.present(sub([entry()]), STATUS, None, "yazili", "small"), port.Refusal)
            att = sub([{"name": "a.pdf", "size": 1}])
            self.assertEqual(view([entry(kind="file", attachments=att)]).rows[0].chips, ())
        self.assertIsInstance(port.present(Many([entry()]), STATUS, None, "yazili", "small"), port.Refusal)

    def test_metaclass_eq_subclass_is_refused(self):
        # a metaclass __eq__ fools `type(x) in (...)`; only identity checks refuse these
        meta = type("Meta", (type,), {"__eq__": lambda cls, other: True, "__hash__": type.__hash__})
        many = meta("Many", (list,), {"__getitem__": lambda self, index: [entry(id=i) for i in range(500)]})
        self.assertIsInstance(port.present(many([entry()]), STATUS, None, "yazili", "small"), port.Refusal)
        att = meta("Att", (list,), {})([{"name": "a.pdf", "size": 1}])
        self.assertEqual(view([entry(kind="file", attachments=att)]).rows[0].chips, ())
        sneaky_id = meta("Id", (int,), {})(7)
        self.assertIsNone(view([entry(id=sneaky_id)]).rows[0].entry_id)

    def test_aliased_or_non_str_keys_are_not_plain(self):
        self.assertIsInstance(port.present([], swap_key(STATUS, "ring"), None, "yazili", "small"), port.Refusal)
        self.assertIsInstance(port.present([], {**STATUS, 1: "x"}, None, "yazili", "small"), port.Refusal)
        for bad in [swap_key(entry(), "who"), swap_key(entry(), "kind"), {**entry(), 1: "x"}]:
            row = view([bad]).rows[0]
            self.assertEqual((row.kind, row.who, row.text), ("unsupported", "Sistem", port.UNSUPPORTED))
        for att in [swap_key({"name": "a.pdf", "size": 1}, "name"), {"name": "a.pdf", "size": 1, 2: "x"}]:
            chip = view([entry(kind="file", attachments=[att])]).rows[0].chips[0]
            self.assertEqual(chip, port.Chip("dosya", None, "file"))

    def test_dict_subclass_is_not_a_plain_mapping(self):
        class Sub(dict):
            pass

        self.assertEqual(view([Sub(entry())]).rows[0].kind, "unsupported")
        chip = view([entry(kind="file", attachments=[Sub(name="a.pdf", size=1)])]).rows[0].chips[0]
        self.assertEqual(chip, port.Chip("dosya", None, "file"))
        self.assertIsInstance(port.present([], Sub(STATUS), None, "yazili", "small"), port.Refusal)

    def test_row_text_is_cleaned_original_kept(self):
        def check(text):
            original = entry(text=text)
            shown = view([original]).rows[0].text
            self.assertFalse(any(unicodedata.category(c).startswith("C") for c in shown if c not in "\n\t"))
            self.assertEqual(original["text"], text)

        each(self, [t for t in HOSTILE_TEXT if type(t) is str] + ["a\u200bb\u2067c\x07"], check)
        self.assertEqual(view([entry(text="a\u202eb\u200bc")]).rows[0].text, "abc")

    def test_unknown_kind_is_neutral_row_not_dropped(self):
        bad = [entry(kind="hologram"), entry(who="mallory"), "not a mapping", None,
               entry(kind="approval_card", card_id="x y"), entry(text=deep_json())]
        rows = view(bad + [entry(id=2)]).rows
        self.assertEqual(len(rows), 7)
        for row in rows[:5]:
            self.assertEqual((row.kind, row.text, row.buttons), ("unsupported", port.UNSUPPORTED, ()))
        self.assertEqual(rows[6].entry_id, 2)

    def test_hostile_chip_names_are_cleaned(self):
        def check(part):
            att = {"path": f"C:\\in\\{part}", "name": f"..\\..\\{part}\u202e\x00:evil.png"}
            chip = view([entry(kind="file", attachments=[att])]).rows[0].chips[0]
            self.assertTrue(chip.name)
            for bad in ("\\", "/", ":", "\x00", "\u202e"):
                self.assertNotIn(bad, chip.name)
            self.assertEqual(att["path"], f"C:\\in\\{part}")

        each(self, HOSTILE_PATH_PARTS, check)

    def test_hostile_values_never_raise(self):
        class Boom(dict):
            def get(self, *a):
                raise RecursionError("deep")

        for value in list(HOSTILE_NUMBERS) + [deep_json(), Boom(), {"size": 10**400}]:
            entries = [entry(attachments=[value], ts=value, id=value), Boom(), value]
            self.assertIsInstance(view(entries), port.ViewModel)
        self.assertIsInstance(port.present([], Boom(), None, "yazili", "small"), port.Refusal)


VALID = [
    {"name": "send_text", "text": "selam"},
    {"name": "talk_start"},
    {"name": "talk_stop"},
    {"name": "attach_file", "path": "C:\\Users\\s\\rapor.pdf"},
    {"name": "attach_screenshot"},
    {"name": "set_mode", "mode": "sesli"},
    {"name": "card_tap", "card_id": "card-abc", "choice": "approve"},
    {"name": "resize", "size": "large"},
]


class CommandTests(unittest.TestCase):
    def test_closed_set_each_valid_command_is_typed(self):
        self.assertEqual({c["name"] for c in VALID} | {"talk_cancel", "send_to_phone", "pair_code"}, set(port.COMMANDS))
        vm = view([CARD])
        for command in VALID:
            result = port.validate(command, vm)
            self.assertIsInstance(result, port.Command, command)
            self.assertEqual(result.name, command["name"])

    def test_unknown_command_or_extra_field_refused(self):
        bad = [{"name": "rm_rf"}, {"name": "SEND_TEXT", "text": "a"}, {}, None, "send_text",
               {"name": 1}, {"name": "talk_start", "x": 1}, {"name": "send_text"},
               {"name": "send_text", "text": "a", "path": "b"}]
        for command in bad:
            self.assertIsInstance(port.validate(command), port.Refusal, command)

    def test_dict_subclass_command_refused(self):
        class Hide(dict):
            def keys(self):
                return ["name"]

        class Sub(dict):
            pass

        for command in [Hide(name="talk_start", x="1"), Sub(name="talk_start"), Hide(name="send_text", text="a")]:
            self.assertIsInstance(port.validate(command), port.Refusal, command)

    def test_str_subclass_command_values_refused(self):
        vm = view([CARD])
        for command in [{"name": Liar("talk_start")}, {"name": "set_mode", "mode": Liar("loud")},
                        {"name": "card_tap", "card_id": Liar("card-zzz"), "choice": "approve"},
                        {"name": "card_tap", "card_id": "card-abc", "choice": Liar("maybe")}]:
            self.assertIsInstance(port.validate(command, vm), port.Refusal, command)

    def test_aliased_or_non_str_command_keys_refused(self):
        vm = view([CARD])
        tap = {"name": "card_tap", "card_id": "card-abc", "choice": "approve"}
        for command in [{alias("extra", "name"): "talk_start"}, swap_key({"name": "talk_start"}, "name"),
                        swap_key({"name": "send_text", "text": "a"}, "text", "path"), swap_key(tap, "choice"),
                        {"name": "talk_start", 1: "x"}]:
            self.assertIsInstance(port.validate(command, vm), port.Refusal, command)

    def test_card_tap_needs_a_real_view_with_plain_open_cards(self):
        tap = {"name": "card_tap", "card_id": "card-a", "choice": "approve"}

        class SubView(port.ViewModel):
            pass

        for cards in ["card-abc", {"card-a"}, ["card-a"], frozenset({alias("card-zzz", "card-a")})]:
            hand = port.ViewModel((), "idle", "", "yazili", "small", cards, link="ok")
            self.assertIsInstance(port.validate(tap, hand), port.Refusal, cards)
            fake = port.FakeService([hand])
            fake.view()
            self.assertIsInstance(fake.send(tap), port.Refusal, cards)
        sub = SubView((), "idle", "", "yazili", "small", frozenset({"card-a"}), link="ok")
        self.assertIsInstance(port.validate(tap, sub), port.Refusal)
        plain = port.ViewModel((), "idle", "", "yazili", "small", frozenset({"card-a"}), link="ok")
        self.assertIsInstance(port.validate(tap, plain), port.Command)

    def test_card_tap_frozenset_subclass_open_cards_refused(self):
        class Always(frozenset):
            def __contains__(self, item):
                return True

        tap = {"name": "card_tap", "card_id": "card-nope", "choice": "approve"}
        forged = port.ViewModel((), "idle", "", "yazili", "small", Always({"card-a"}), link="ok")
        self.assertIsInstance(port.validate(tap, forged), port.Refusal)
        fake = port.FakeService([forged])
        fake.view()
        self.assertIsInstance(fake.send(tap), port.Refusal)

    def test_two_choices_two_buttons_and_both_taps_succeed_on_open_card(self):
        self.assertEqual(port.CHOICES, ("approve", "reject"))
        self.assertEqual(port.BUTTONS, (("approve", "Onayla"), ("reject", "Reddet")))
        vm = view([CARD])
        self.assertEqual(vm.rows[0].buttons, (("approve", "Onayla"), ("reject", "Reddet")))
        for choice in ("approve", "reject"):
            result = port.validate({"name": "card_tap", "card_id": "card-abc", "choice": choice}, vm)
            self.assertEqual(result, port.Command("card_tap", card_id="card-abc", choice=choice))

    def test_send_text_bounds(self):
        for text in ["", "   \n", "a" * (port.MAX_SEND_CHARS + 1), None, 5, b"a", ["a"]]:
            self.assertIsInstance(port.validate({"name": "send_text", "text": text}), port.Refusal)
        cap = port.validate({"name": "send_text", "text": "a" * port.MAX_SEND_CHARS})
        self.assertIsInstance(cap, port.Command)

    def test_attach_file_path_is_opaque_and_untouched(self):
        def check(part):
            path = f"..\\{part}\\x\u202e.txt"
            self.assertEqual(port.validate({"name": "attach_file", "path": path}).path, path)

        each(self, HOSTILE_PATH_PARTS, check)
        self.assertIsInstance(port.validate({"name": "attach_file", "path": ""}), port.Refusal)

    def test_card_tap_choice_and_card_must_match_view(self):
        vm = view([CARD])
        for card_id, choice in [("card-abc", "Approve"), ("card-abc", "yes"), ("card-abc", " approve"),
                                ("card-zzz", "approve"), ("card-abc", "")]:
            command = {"name": "card_tap", "card_id": card_id, "choice": choice}
            self.assertIsInstance(port.validate(command, vm), port.Refusal, command)
        tap = {"name": "card_tap", "card_id": "card-abc", "choice": "reject"}
        self.assertIsInstance(port.validate(tap), port.Refusal)
        self.assertIsInstance(port.validate(tap, view([dict(CARD, state="EXPIRED")])), port.Refusal)

    def test_mode_and_size_closed(self):
        self.assertIsInstance(port.validate({"name": "set_mode", "mode": "loud"}), port.Refusal)
        self.assertIsInstance(port.validate({"name": "resize", "size": "30"}), port.Refusal)

    def test_hostile_command_never_raises(self):
        class Boom(dict):
            def get(self, *a):
                raise RecursionError("deep")

        for value in list(HOSTILE_TEXT) + [deep_json(), Boom({"name": "talk_start"})]:
            self.assertIsInstance(port.validate({"name": "send_text", "text": value}), (port.Command, port.Refusal))
            self.assertIsInstance(port.validate(value), port.Refusal)

    def test_refusal_texts_are_ascii(self):
        reasons = [port.validate(c).reason for c in [None, {"name": "x"}, {"name": "send_text", "text": ""}]]
        reasons.append(port.present(None, STATUS, None, "yazili", "small").reason)
        self.assertTrue(all(r.isascii() for r in reasons))


class FakeServiceTests(unittest.TestCase):
    def test_records_commands_and_replays_views(self):
        first, second = view([CARD]), view([dict(CARD, state="APPROVED")])
        fake = port.FakeService([first, second])
        tap = {"name": "card_tap", "card_id": "card-abc", "choice": "approve"}
        self.assertIsInstance(fake.send(tap), port.Refusal)  # no view replayed yet
        self.assertIs(fake.view(), first)
        self.assertIsInstance(fake.send(tap), port.Command)
        self.assertIs(fake.view(), second)
        self.assertIs(fake.view(), second)
        self.assertIsInstance(fake.send(tap), port.Refusal)
        fake.send({"name": "talk_start"})
        self.assertEqual([c.name for c in fake.commands], ["card_tap", "talk_start"])
        self.assertEqual(len(fake.refused), 2)

    def test_empty_script(self):
        self.assertIsNone(port.FakeService().view())


class SafetyTests(unittest.TestCase):
    def test_port_imports_only_pure_modules(self):
        tree = ast.parse((ROOT / "jarvis" / "face" / "port.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(names, {"__future__"})
        self.assertFalse(any("Qt" in n or "talk_" in n for n in names))

    def test_nothing_in_the_app_imports_port(self):
        for path in (ROOT / "jarvis").rglob("*.py"):
            if path.name == "port.py" and path.parent.name == "face":
                continue
            parts = path.relative_to(ROOT).with_suffix("").parts
            self.assertEqual(port_imports(path.read_text(encoding="utf-8"), parts), [], path)

    def test_import_guard_sees_every_form(self):
        sub, face, work = ("jarvis", "face", "sub", "x"), ("jarvis", "face", "x"), ("jarvis", "work", "x")
        hits = [
            ("import jarvis.face.port as p", work), ("from jarvis.face import port", work),
            ("from jarvis.face.port import present", work), ("from . import port", face),
            ("from .port import present", face), ("from .. import port", sub), ("from ..port import Row", sub),
            ("from ...face import port", sub), ("from ...face.port import Row", sub),
            ("from . import port", ("jarvis", "face", "__init__")),
            ("__import__('jarvis.face.port')", work), ("__import__('jarvis.face', fromlist=['port'])", work),
            ("importlib.import_module('jarvis.face.port')", work), ("import_module('.port', 'jarvis.face')", work),
        ]
        for source, parts in hits:
            self.assertTrue(port_imports(source, parts), source)
        misses = [("from . import port", work), ("from .work_ports import x", work), ("import jarvis.face.shell", work),
                  ("from .. import shell", sub), ("importlib.import_module('jarvis.face.shell')", work)]
        for source, parts in misses:
            self.assertEqual(port_imports(source, parts), [], source)


def port_imports(source, parts):
    """Line numbers where source (module path parts under ROOT) imports jarvis.face.port, statically or by literal."""
    package = parts[:-1]
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(package[: len(package) - node.level + 1] + tuple(filter(None, base.split("."))))
            mods = [base] + [f"{base}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in (
                "__import__", "import_module"):
            # fail-closed: any literal naming a "port" module counts, resolved or not
            mods = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and type(c.value) is str]
            mods = ["jarvis.face.port" for m in mods if m == "port" or m.endswith(".port") or "face.port" in m]
        else:
            continue
        if any(m == "jarvis.face.port" or m.startswith("jarvis.face.port.") or m.endswith("face.port") for m in mods):
            found.append(node.lineno)
    return found

if __name__ == "__main__":
    unittest.main()
