"""FACEPORTGAP1C: window states, connection gating and command boundaries."""
import unittest

from jarvis.face import port
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, each
from tests.test_face_port import CARD, STATUS, VALID, Liar, entry


class FacePortGapTests(unittest.TestCase):
    def present(self, entries=(), window="reader", **kwargs):
        return port.present(entries, STATUS, None, "yazili", window, **kwargs)

    def test_window_states_work_in_present_and_resize(self):
        entries = [entry(id=i) for i in range(250)]
        for size, count in (("ring", 0), ("reader", 30), ("full", 200), ("small", 30), ("large", 200)):
            with self.subTest(size=size):
                vm = self.present(entries, window=size)
                self.assertIsInstance(vm, port.ViewModel)
                self.assertEqual(vm.window, size)
                self.assertEqual([row.entry_id for row in vm.rows], list(range(250 - count, 250)))
                self.assertEqual(port.validate({"name": "resize", "size": size}, vm),
                                 port.Command("resize", size=size))

    def test_window_state_requires_exact_name(self):
        def check(size):
            self.assertIsInstance(self.present(window=size), port.Refusal)
            self.assertIsInstance(port.validate({"name": "resize", "size": size}), port.Refusal)

        each(self, ("Reader", "FULL", "Ring", " reader", "full ", "other", Liar("full"),
                    *HOSTILE_NUMBERS), check)

    def test_link_values_and_default(self):
        self.assertEqual(self.present().link, "unknown")
        self.assertEqual(port.ViewModel((), "idle", "", "yazili", "ring").link, "unknown")
        for link in ("ok", "unreachable", "unknown"):
            self.assertEqual(self.present(link=link).link, link)

    def test_link_requires_exact_value(self):
        each(self, ("OK", " unknown", "bad", Liar("ok"), *HOSTILE_NUMBERS),
             lambda link: self.assertIsInstance(self.present(link=link), port.Refusal))

    def test_link_gate_and_origin_parity_cover_all_non_pairing_commands(self):
        entries = [CARD, entry(id=7, who="jarvis")]
        commands = [*VALID, {"name": "talk_cancel"}, {"name": "send_to_phone", "entry_id": 7}]
        for link in ("ok", "unknown", "unreachable"):
            vm = self.present(entries, link=link)
            for command in commands:
                with self.subTest(link=link, command=command):
                    local = port.validate(command, vm)
                    self.assertEqual(local, port.validate(command, vm, origin="remote"))
                    if link == "ok" or command["name"] == "resize":
                        self.assertIsInstance(local, port.Command)
                    else:
                        self.assertEqual(local, port.Refusal("link"))

    def test_no_view_keeps_legacy_validation(self):
        for command in [*VALID, {"name": "talk_cancel"}, {"name": "send_to_phone", "entry_id": 7}]:
            with self.subTest(command=command):
                if command["name"] in ("card_tap", "send_to_phone"):
                    self.assertIsInstance(port.validate(command), port.Refusal)
                else:
                    self.assertIsInstance(port.validate(command), port.Command)

    def test_pair_code_is_local_only_even_without_an_ok_link(self):
        pair = {"name": "pair_code"}
        for vm in (None, self.present(), self.present(link="unreachable"), self.present(link="ok")):
            self.assertEqual(port.validate(pair, vm, origin="local"), port.Command("pair_code"))
            self.assertEqual(port.validate(pair, vm, origin="remote"), port.Refusal("local_only"))
        self.assertEqual(port.validate({**pair, "extra": 1}, origin="remote"), port.Refusal("local_only"))

    def test_origin_requires_exact_value_for_every_command(self):
        def check(origin):
            for name in ("pair_code", "talk_cancel"):
                self.assertIsInstance(port.validate({"name": name}, origin=origin), port.Refusal)

        each(self, ("REMOTE", " local", Liar("local"), *HOSTILE_NUMBERS), check)

    def test_no_arguments_allowed_for_talk_cancel_or_pair_code(self):
        for name in ("talk_cancel", "pair_code"):
            for field in ("code", "entry_id", "text", "origin", "link", "extra"):
                with self.subTest(name=name, field=field):
                    self.assertIsInstance(port.validate({"name": name, field: None}), port.Refusal)

    def test_send_to_phone_preserves_visible_jarvis_entry_id(self):
        vm = self.present([entry(id=7, who="jarvis"), entry(id="entry-8", who="jarvis")], link="ok")
        for ident in (7, "entry-8"):
            result = port.validate({"name": "send_to_phone", "entry_id": ident}, vm)
            self.assertEqual(result, port.Command("send_to_phone", entry_id=ident))
        self.assertIsInstance(port.validate({"name": "send_to_phone", "entry_id": "7"}, vm), port.Refusal)

    def test_send_to_phone_rejects_absent_non_jarvis_and_card_ids(self):
        rows = [CARD, entry(id=7), entry(id=8, who="system"), entry(id="card-abc", who="jarvis")]
        vm = self.present(rows, link="ok")
        for ident in (7, 8, 9, 10, "card-abc"):
            with self.subTest(ident=ident):
                self.assertIsInstance(port.validate({"name": "send_to_phone", "entry_id": ident}, vm), port.Refusal)

    def test_send_to_phone_rejects_hostile_ids_even_if_visible(self):
        def check(ident):
            vm = self.present([entry(id=ident, who="jarvis"), entry(id=1, who="jarvis")], link="ok")
            self.assertIsInstance(port.validate({"name": "send_to_phone", "entry_id": ident}, vm), port.Refusal)

        bidi_and_invisible = HOSTILE_TEXT[:21]
        each(self, (*bidi_and_invisible, "x" * 129, Liar("entry-7"), True, False, 10**400, "", []), check)

    def test_new_fields_keep_records_frozen_and_commands_closed(self):
        vm = self.present([entry(who="jarvis")], link="ok")
        cmd = port.validate({"name": "send_to_phone", "entry_id": 1}, vm)
        for record, field in ((vm, "link"), (cmd, "entry_id")):
            with self.assertRaises(AttributeError):
                setattr(record, field, None)
            with self.assertRaises(AttributeError):
                delattr(record, field)
        for command in ({"name": "send_to_phone"}, {"name": "send_to_phone", "entry_id": 1, "extra": True}):
            self.assertIsInstance(port.validate(command, vm), port.Refusal)
        with self.assertRaises(TypeError):
            port.Command("send_to_phone", entry_id=1, extra=True)
