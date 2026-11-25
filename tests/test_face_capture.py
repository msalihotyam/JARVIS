"""FACECAP1: click-only single screenshot handed to attachment intake (fakes only, injected clock)."""
from __future__ import annotations

import ast
import copy
import json
import pickle
import unittest
from pathlib import Path

from jarvis.face import capture as cap
from tests.adversarial import HOSTILE_JSON, HOSTILE_NUMBERS, HOSTILE_TEXT, deep_json, each

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "jarvis" / "face" / "capture.py"
PNG = cap.PNG_MAGIC + b"\x00" * 32


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Run:
    """One fake click: records the order of before/grab/after/intake calls."""

    def __init__(self, png: object = PNG, grab_error: BaseException | None = None,
                 intake_error: BaseException | None = None, record: object = "rec-1") -> None:
        self.calls: list[str] = []
        self.png, self.grab_error, self.intake_error, self.record = png, grab_error, intake_error, record
        self.received: list[bytes] = []

    def before(self) -> None:
        self.calls.append("before")

    def after(self) -> None:
        self.calls.append("after")

    def grab(self) -> object:
        self.calls.append("grab")
        if self.grab_error is not None:
            raise self.grab_error
        return self.png

    def intake(self, data: bytes) -> object:
        self.calls.append("intake")
        self.received.append(data)
        if self.intake_error is not None:
            raise self.intake_error
        return self.record

    def go(self, token: object, clock: Clock, **limits: object) -> cap.CaptureResult:
        return cap.capture(token, self.grab, self.intake, self.before, self.after, clock, **limits)


class AcceptanceTests(unittest.TestCase):
    def test_a1_click_token_grab_png_goes_to_intake_and_record_returns(self) -> None:
        clock, run = Clock(), Run()
        result = run.go(cap.click_token(clock), clock)
        self.assertTrue(result.ok)
        self.assertEqual(result.record, "rec-1")
        self.assertEqual(run.received, [PNG])

    def test_a2_before_runs_before_grab_and_after_always_runs(self) -> None:
        clock, run = Clock(), Run()
        run.go(cap.click_token(clock), clock)
        self.assertEqual(run.calls, ["before", "grab", "after", "intake"])
        clock, run = Clock(), Run()
        run.before = lambda: (run.calls.append("before"), 1 / 0)  # type: ignore[method-assign]
        result = run.go(cap.click_token(clock), clock)
        self.assertEqual((result.ok, result.reason, run.calls), (False, "before_failed", ["before", "after"]))

    def test_a3_size_cap_over_it_is_refused(self) -> None:
        clock, run = Clock(), Run()
        result = run.go(cap.click_token(clock), clock, max_bytes=len(PNG) - 1)
        self.assertEqual((result.ok, result.reason), (False, "too_large"))
        self.assertNotIn("intake", run.calls)
        clock, run = Clock(), Run()
        self.assertTrue(run.go(cap.click_token(clock), clock, max_bytes=len(PNG)).ok)

        def bad_cap(value: object) -> None:
            c, r = Clock(), Run()
            self.assertEqual(r.go(cap.click_token(c), c, max_bytes=value).reason, "bad_limit")
            self.assertEqual(r.calls, [])
        each(self, tuple(v for v in HOSTILE_NUMBERS if type(v) is not int) + (0, -1, 1.5), bad_cap)

    def test_a4_module_keeps_no_image_and_result_carries_none(self) -> None:
        clock, run = Clock(), Run()
        result = run.go(cap.click_token(clock), clock)
        self.assertEqual(set(cap.CaptureResult.__slots__), {"ok", "reason", "record"})
        self.assertNotIn(PNG, (result.ok, result.reason, result.record))
        for value in vars(cap).values():
            self.assertIsNot(value, run.png)
        # buffer.open() is the in-memory QBuffer; no file open, write, unlink or temp file
        self.assertNotRegex(SOURCE.read_text(encoding="utf-8"), r"(?<!\.)\bopen\(|\.write\w*\(|unlink|mkdir|tempfile")

    def test_a5_real_grabber_is_one_small_separate_qt_function(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "qt_grab_screen")
        text = ast.unparse(fn)
        for needle in ("grabWindow(0)", "QBuffer", "'PNG'"):
            self.assertIn(needle, text)
        self.assertLessEqual(fn.end_lineno - fn.lineno, 10)


class NegativeTests(unittest.TestCase):
    def assert_refused_without_grab(self, token: object, clock: Clock, reason: str) -> None:
        run = Run()
        result = run.go(token, clock)
        self.assertEqual((result.ok, result.reason), (False, reason))
        self.assertEqual(run.calls, [])

    def test_n1_missing_reused_or_old_token_is_refused_before_grab(self) -> None:
        clock = Clock()
        self.assert_refused_without_grab(None, clock, "no_token")
        token = cap.click_token(clock)
        self.assertTrue(Run().go(token, clock).ok)
        self.assert_refused_without_grab(token, clock, "token_used")
        token = cap.click_token(clock)
        clock.t += cap.DEFAULT_MAX_AGE + 0.001
        self.assert_refused_without_grab(token, clock, "token_expired")
        self.assert_refused_without_grab(token, clock, "token_used")  # an expired try still burns it
        token, clock.t = cap.click_token(clock), clock.t - 1  # clock went backwards
        self.assert_refused_without_grab(token, clock, "token_expired")

    def test_n1_bad_clock_values_refuse_and_never_raise(self) -> None:
        def check(value: object) -> None:
            good = cap.click_token(Clock())
            self.assertEqual(Run().go(good, lambda: value).reason, "bad_clock")
            self.assertIsNone(cap.click_token(lambda: value))
        each(self, HOSTILE_NUMBERS[:2] + HOSTILE_NUMBERS[3:6] + HOSTILE_NUMBERS[7:], check)
        huge = cap.click_token(Clock(10**400))  # finite int, OverflowError against a float clock
        self.assertEqual(Run().go(huge, Clock(0.5)).reason, "bad_clock")

    def test_n2_tokens_built_from_str_dict_json_or_copies_are_refused(self) -> None:
        clock = Clock()
        forged: list[object] = list(HOSTILE_TEXT) + list(HOSTILE_JSON) + [deep_json()]
        forged += [json.loads(s) for s in ('{"created": 1000.0}', '"token"', "[]")]
        forged += [{"_created": 1000.0}, object(), cap._ClickToken, b"token"]
        each(self, forged, lambda value: self.assert_refused_without_grab(value, clock, "no_token"))
        with self.assertRaises(TypeError):
            cap._ClickToken(object(), 1000.0)
        real = cap.click_token(clock)
        for dup in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.subTest(dup=dup), self.assertRaises(TypeError):
                dup(real)
        fake = object.__new__(cap._ClickToken)  # right class, not from the factory
        self.assert_refused_without_grab(fake, clock, "no_token")

    def test_n3_grab_raises_or_returns_empty_or_non_png_after_still_ran(self) -> None:
        cases = [(Run(grab_error=OSError("x")), "grab_failed"),
                 (Run(grab_error=RecursionError()), "grab_failed"),
                 (Run(png=b""), "empty"), (Run(png=None), "not_png"),
                 (Run(png=b"GIF89a" + b"\x00" * 8), "not_png"),
                 (Run(png=bytearray(PNG)), "not_png"), (Run(png=PNG.decode("latin-1")), "not_png")]
        for run, reason in cases:
            with self.subTest(reason=reason, png=repr(run.png)[:20]):
                clock = Clock()
                result = run.go(cap.click_token(clock), clock)
                self.assertEqual((result.ok, result.reason), (False, reason))
                self.assertIn("after", run.calls)
                self.assertNotIn("intake", run.calls)

    def test_n4_intake_refuses_result_refused_nothing_kept(self) -> None:
        for run in (Run(intake_error=ValueError("too_large")), Run(intake_error=RecursionError()),
                    Run(record=None)):
            with self.subTest(run=run.intake_error):
                clock = Clock()
                result = run.go(cap.click_token(clock), clock)
                self.assertEqual((result.ok, result.reason, result.record), (False, "intake_refused", None))

    def test_refusals_are_ascii_and_nothing_escapes(self) -> None:
        clock = Clock()
        boom = lambda: (_ for _ in ()).throw(RecursionError())  # noqa: E731
        results = [cap.capture(cap.click_token(clock), "x", 1, None, [], clock),
                   cap.capture(cap.click_token(clock), boom, boom, boom, boom, clock),
                   cap.capture(cap.click_token(clock), boom, boom, lambda: None, lambda: None, boom)]
        self.assertEqual([r.reason for r in results], ["bad_collaborator", "before_failed", "internal_error"])
        for result in results:
            self.assertFalse(result.ok)
            self.assertTrue(result.reason.isascii())
        token = cap.click_token(clock)
        token._created = clock.t + 10**6  # resetting the visible time does not move the real one
        clock.t += cap.DEFAULT_MAX_AGE + 1
        self.assertEqual(Run().go(token, clock).reason, "token_expired")


class StaticTests(unittest.TestCase):
    def test_n5_no_timer_thread_scheduler_socket_subprocess_and_grab_only_after_token(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(text)
        banned = r"^(threading|_thread|sched|socket|subprocess|asyncio|concurrent|signal|time|urllib|http)"
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                self.assertNotRegex(name, banned)
        self.assertNotRegex(text, r"QTimer|QThread|singleShot|after\(\d")
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name, fn in funcs.items():
            src = ast.unparse(fn)
            self.assertEqual("grabWindow" in src, name == "qt_grab_screen", name)
            self.assertNotIn("qt_grab_screen", src.replace("def qt_grab_screen", ""), name)
            self.assertEqual("grab()" in src, name == "_capture", name)
        body = ast.unparse(funcs["_capture"])
        self.assertLess(body.index("_USED.add(token)"), body.index("grab()"))

    def test_nothing_in_the_running_app_imports_capture(self) -> None:
        for path in (ROOT / "jarvis").rglob("*.py"):
            if path != SOURCE:
                self.assertNotIn("face.capture", path.read_text(encoding="utf-8"), str(path))
                self.assertNotRegex(path.read_text(encoding="utf-8"),
                                    r"from jarvis\.face import .*capture|from \.+capture|from \.+ import .*capture")


if __name__ == "__main__":
    unittest.main()
