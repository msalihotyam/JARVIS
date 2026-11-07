"""N6-06 sensors and preflight: inputs are inline; no git, file, network, clock or data/jarvis.db."""

from __future__ import annotations

import unittest

from jarvis.n6_preflight import memory_input, preflight
from jarvis.work_supervisor import DEFAULT_POLICY, Situation, supervise

MAIN = "111bb6bdc9a8055814fb472d2edaf69c5c27cf5c"
OTHER = "222bb6bdc9a8055814fb472d2edaf69c5c27cf5c"
QUOTA = {"used_pct": 10, "hour": 12, "line_pct": 50}


def sit(memory: object) -> Situation:
    return Situation.from_dict(
        {"operator_pause": False, "policy": DEFAULT_POLICY, "quota": QUOTA, "memory": memory})


def sl(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "N6-06", "header": f"@jarvis lane=N6-06 model=sonnet effort=high base={MAIN}",
        "files": ["jarvis/n6_preflight.py", "tests/test_n6_preflight.py"], "depends_on": ["N6-05"]}
    return {**base, **kw}


_D = object()


def pre(slice_: object = _D, main: object = MAIN, owned: object = _D, done: object = _D):
    return preflight(sl() if slice_ is _D else slice_, main,
                     {"N6-07": ["jarvis/other.py"]} if owned is _D else owned,
                     ["N6-05"] if done is _D else done)


class N6PreflightTests(unittest.TestCase):
    # ---- negatives first
    def test_reading_below_the_floor_holds_dispatch(self) -> None:
        for args in ((2.0, 20.0, 100.0), (20.0, 2.9, 100.0), (20.0, 20.0, 1.0)):
            d = supervise(sit(memory_input(*args)))
            self.assertEqual((d.action, d.rule_id), ("wait", "mem.hold"), args)

    def test_unknown_nan_negative_or_non_numeric_holds(self) -> None:
        bad = (None, float("nan"), float("inf"), -1, -0.1, "8", True, [8], object())
        for b in bad:
            for pos in range(3):
                args: list[object] = [20.0, 20.0, 20.0]
                args[pos] = b
                m = memory_input(*args)
                self.assertIsNone(m["free_gb"], (pos, b))
                d = supervise(sit(m))
                self.assertEqual((d.action, d.rule_id), ("wait", "mem.unknown"), (pos, b))

    def test_hold_does_not_release_without_margin(self) -> None:
        d = supervise(sit(memory_input(4.0, 20.0, 20.0, held=True, ok_for_s=600)))
        self.assertEqual(d.rule_id, "mem.hysteresis")

    def test_scarcest_reading_decides(self) -> None:
        self.assertEqual(memory_input(9.0, 5.5, 7.0)["free_gb"], 5.5)

    def test_enough_of_everything_dispatches(self) -> None:
        d = supervise(sit(memory_input(8, 6, 120)))
        self.assertEqual((d.action, d.rule_id), ("dispatch", "pace.on_line"))

    # ---- preflight, negatives first
    def test_wrong_base_sha_blocks(self) -> None:
        v = pre(main=OTHER)
        self.assertFalse(v.ok)
        self.assertTrue(v.reason.startswith("base:"), v.reason)

    def test_missing_or_short_base_blocks(self) -> None:
        for header in ("@jarvis lane=X base=main", "@jarvis lane=X base=111bb6b", "@jarvis lane=X",
                       f"@jarvis lane=X base={MAIN}0", f"x\n@jarvis base={MAIN}", ""):
            v = pre(sl(header=header))
            self.assertFalse(v.ok, header)
            self.assertTrue(v.reason.startswith("base:"), header)

    def test_file_owned_by_another_running_lane_blocks(self) -> None:
        for theirs in ("jarvis/n6_preflight.py", "jarvis/*", "jarvis/n6_preflight.py.bak", "jarvis\\n6_preflight.py"):
            v = pre(owned={"N6-07": [theirs]})
            self.assertFalse(v.ok, theirs)
            self.assertTrue(v.reason.startswith("files:"), v.reason)

    def test_own_lane_entry_does_not_block(self) -> None:
        self.assertTrue(pre(owned={"N6-06": ["jarvis/n6_preflight.py"]}).ok)

    def test_unmet_dependency_blocks(self) -> None:
        for done in ([], ["N6-04"], ()):
            v = pre(done=done)
            self.assertFalse(v.ok)
            self.assertTrue(v.reason.startswith("deps:"), v.reason)

    def test_malformed_input_blocks(self) -> None:
        cases = (
            dict(slice_="x"), dict(slice_=[]), dict(slice_=sl(id="")),
            dict(slice_=sl(id=6)), dict(slice_=sl(header=None)), dict(slice_=sl(files="a.py")),
            dict(slice_=sl(files=[""])), dict(slice_=sl(files=[1])), dict(slice_=sl(depends_on="N6-05")),
            dict(slice_=sl(depends_on=[None])), dict(slice_={}),
            dict(main=None), dict(main="main"), dict(main=MAIN.upper()),
            dict(owned=[]), dict(owned={"N6-07": "jarvis/x.py"}), dict(owned={7: ["a"]}),
            dict(done="N6-05"), dict(done=None), dict(done=[["N6-05"]]),
        )
        for kw in cases:
            v = pre(**kw)  # type: ignore[arg-type]
            self.assertFalse(v.ok, kw)
            self.assertTrue(v.reason.startswith(("malformed:", "base:")), (kw, v.reason))

    # ---- positive
    def test_clean_slice_passes(self) -> None:
        self.assertEqual(tuple(pre()), (True, "ok"))
        self.assertTrue(pre(sl(depends_on=[]), done=[]).ok)
        self.assertTrue(pre(sl(depends_on=[]), done={"x"}, owned={}).ok)


if __name__ == "__main__":
    unittest.main()
