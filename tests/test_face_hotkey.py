"""The face hotkey owns one OS registration and only receives its messages."""

import ast
import random
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from jarvis.face import hotkey
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_TEXT, each

FACE = Path(__file__).resolve().parents[1] / "jarvis" / "face"


class ParseTests(unittest.TestCase):
    def test_off_and_supported_keys(self):
        self.assertEqual(hotkey.parse(""), hotkey.KeySpec(None, 0))
        self.assertEqual(hotkey.parse("a"), hotkey.KeySpec(0x41, 0))
        self.assertEqual(hotkey.parse(" ctrl + ALT + Shift + Win + Space "), hotkey.KeySpec(0x20, 0xF))
        self.assertEqual(hotkey.parse("Tab+Shift"), hotkey.KeySpec(0x09, 0x4))
        self.assertEqual(hotkey.parse("0"), hotkey.KeySpec(0x30, 0))

    def test_adversarial_settings_are_refused(self):
        class UntrustedString(str):
            def __len__(self):
                raise AssertionError("must be refused before inspecting")

        invalid = (
            b"A", [], {}, UntrustedString("A"), "A" * (1024 * 1024), "Ctrl", "Ctrl+Ctrl", "A+B",
            "Ctrl+A+Ctrl", "Ctrl++A", "A+", "Unknown", "A\t", "F9", "Ctrl+F12",
            *(f"Win+F{i}" for i in range(1, 25)),
        )
        each(self, (*HOSTILE_TEXT, *(n for n in HOSTILE_NUMBERS if type(n) is not str), *invalid),
             lambda value: self.assertIsInstance(hotkey.parse(value), hotkey.Refusal))


class FakeOS:
    def __init__(self, case):
        self.case = case
        self.live = {}
        self.registered = 0
        self.unregistered = 0

    def register(self, ident, flags, vk):
        self.case.assertFalse(self.live)
        self.live[ident] = (flags, vk)
        self.registered += 1
        return True

    def unregister(self, ident):
        self.case.assertIn(ident, self.live)
        del self.live[ident]
        self.unregistered += 1
        return True


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.os = FakeOS(self)
        self.sources = []
        self.key = hotkey.HotKey(self.os.register, self.os.unregister, self.sources.append)

    def test_no_default_and_one_press_message_with_no_repeat(self):
        self.assertEqual(self.key.state, "off")
        self.assertEqual(self.key.apply("").state, "off")
        self.assertEqual(self.os.registered, 0)
        self.assertEqual(self.key.apply("Ctrl+K").state, "active")
        ident, registration = next(iter(self.os.live.items()))
        self.assertEqual(registration, (0x4002, 0x4B))  # Windows suppresses repeats with MOD_NOREPEAT.
        self.assertFalse(self.key.on_message(ident + 1))
        self.assertEqual(self.sources, [])
        self.assertTrue(self.key.on_message(ident))
        self.assertEqual(self.sources, ["hotkey"])
        self.assertTrue(self.key.on_message(ident))
        self.assertEqual(self.sources, ["hotkey", "hotkey"])
        self.assertEqual(self.key.close().state, "off")
        self.assertFalse(self.key.on_message(ident))
        self.assertEqual(self.sources, ["hotkey", "hotkey"])

    def test_random_apply_and_close_balance_every_registration(self):
        rng = random.Random(2210)
        for _ in range(40):
            for _ in range(30):
                setting, expected = rng.choice([
                    ("A", "active"), ("Win+B", "active"), ("Shift+Tab", "active"),
                    ("Ctrl+F12", "invalid"), ("", "off"), (None, "off"),
                ])
                status = self.key.close() if setting is None else self.key.apply(setting)
                self.assertEqual(status.state, expected)
                self.assertEqual(len(self.os.live), int(expected == "active"))
            self.key.close()
            self.assertFalse(self.os.live)
            self.assertEqual(self.os.registered, self.os.unregistered)
        self.assertGreater(self.os.registered, 1)

    def test_registration_refusal_is_taken_without_retry(self):
        register = Mock(return_value=False)
        key = hotkey.HotKey(register, self.os.unregister, self.sources.append)
        status = key.apply("Win+K")
        self.assertEqual(status.state, "taken")
        self.assertIn("başka", status.message)
        register.assert_called_once()
        self.assertFalse(key.on_message(register.call_args.args[0]))
        self.assertEqual(key.close().state, "off")
        self.assertEqual(self.os.unregistered, 0)
        self.assertEqual(self.sources, [])

    def test_equivalent_setting_keeps_registration(self):
        self.key.apply("Ctrl+K")
        self.assertEqual(self.key.apply("k+ctrl").state, "active")
        self.assertEqual((self.os.registered, self.os.unregistered), (1, 0))
        self.key.close()
        self.assertEqual(self.os.unregistered, 1)

    def test_failed_unregister_retains_ownership_until_close_can_release_it(self):
        attempts = iter((OSError(), False))

        def release(ident):
            result = next(attempts, None)
            if isinstance(result, OSError):
                raise result
            return self.os.unregister(ident) if result is None else result

        unregister = Mock(side_effect=release)
        key = hotkey.HotKey(self.os.register, unregister, self.sources.append)
        key.apply("A")
        ident = next(iter(self.os.live))
        self.assertEqual(key.apply("B").state, "invalid")
        self.assertEqual(key.apply("").state, "invalid")
        self.assertEqual(self.os.live, {ident: (0x4000, 0x41)})
        self.assertFalse(key.on_message(ident))
        self.assertEqual(key.close().state, "off")
        self.assertEqual(unregister.call_args_list, [call(ident)] * 3)
        self.assertFalse(self.os.live)

    def test_injected_registration_and_callback_errors_do_not_escape(self):
        register = Mock(side_effect=OSError)
        callback = Mock(side_effect=RuntimeError)
        key = hotkey.HotKey(register, self.os.unregister, callback)
        self.assertEqual(key.apply("A").state, "invalid")
        ident = register.call_args.args[0]
        self.assertFalse(key.on_message(ident))
        callback.assert_not_called()
        register.side_effect = self.os.register
        self.assertEqual(key.apply("A").state, "active")
        self.assertFalse(key.on_message(ident))
        callback.assert_called_once_with("hotkey")
        self.assertEqual(key.close().state, "off")


class BoundaryTests(unittest.TestCase):
    def test_default_os_refuses_other_platforms_without_ctypes(self):
        with patch.object(sys, "platform", "linux"), patch.dict(sys.modules, {"ctypes": None}):
            self.assertIsInstance(hotkey.default_os(), hotkey.Refusal)

    def test_default_os_refuses_unavailable_windows_apis(self):
        ctypes = Mock()
        ctypes.WinDLL.side_effect = OSError
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"ctypes": ctypes}):
            self.assertIsInstance(hotkey.default_os(), hotkey.Refusal)

    def test_default_os_binds_windows_apis_with_thread_owned_registration(self):
        ctypes = Mock()
        user32 = ctypes.WinDLL.return_value
        user32.RegisterHotKey.return_value = 1
        user32.UnregisterHotKey.return_value = 1
        with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, {"ctypes": ctypes}):
            register, unregister = hotkey.default_os()
        ctypes.WinDLL.assert_called_once_with("user32")
        self.assertEqual(user32.RegisterHotKey.argtypes,
                         (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint))
        self.assertEqual(user32.UnregisterHotKey.argtypes, (ctypes.c_void_p, ctypes.c_int))
        self.assertIs(user32.RegisterHotKey.restype, ctypes.c_int)
        self.assertIs(user32.UnregisterHotKey.restype, ctypes.c_int)
        self.assertTrue(register(3, 0x4002, 0x4B))
        self.assertTrue(unregister(3))
        user32.RegisterHotKey.assert_called_once_with(None, 3, 0x4002, 0x4B)
        user32.UnregisterHotKey.assert_called_once_with(None, 3)

    def test_face_has_no_keyboard_hooks_or_polling(self):
        banned = ("SetWindowsHookEx", "WH_KEYBOARD_LL", "GetAsyncKeyState", "GetKeyState", "RegisterRawInputDevices")
        for path in FACE.rglob("*"):
            if path.suffix not in {".py", ".qml", ".js"}:
                continue
            with self.subTest(file=path.relative_to(FACE)):
                source = path.read_text(encoding="utf-8")
                for name in banned:
                    self.assertNotIn(name, source)
                if path.suffix == ".py":
                    for node in ast.walk(ast.parse(source)):
                        if isinstance(node, ast.Import):
                            modules = [alias.name for alias in node.names]
                        elif isinstance(node, ast.ImportFrom):
                            modules = [node.module or ""]
                        else:
                            continue
                        for module in modules:
                            self.assertNotIn(module.split(".")[0], {"keyboard", "pynput"})
