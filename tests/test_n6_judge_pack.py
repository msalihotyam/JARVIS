"""N6JUDGEPACK1: the Phase 3 judge packet (scripts/n6_judge_pack.py).

Every fixture lives in its own TemporaryDirectory root; no test opens data/jarvis.db, reads the
wall clock or touches the network. The real dev/n6/acceptance set is only read.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("n6_judge_pack", ROOT / "scripts" / "n6_judge_pack.py")
assert _SPEC is not None and _SPEC.loader is not None
pack = importlib.util.module_from_spec(_SPEC)
sys.modules["n6_judge_pack"] = pack
_SPEC.loader.exec_module(pack)

from jarvis import dora_screen  # noqa: E402
from jarvis.n6_kpi import KpiRow, LimitedEvent, record, record_limited  # noqa: E402

e8 = pack.e8
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
IDS = ("CASEA1", "CASEB1", "CASEC1")
GROUPS = {
    "code/writer-a": {"pass_rate": 0.9, "post_accept_failures": 1, "interventions": 2,
                      "quota_per_accepted": 1200.0, "finish_seconds_median": None,
                      "n": 12, "small_sample": False},
    "docs/writer-b": {"pass_rate": 1.0, "post_accept_failures": 0, "interventions": 0,
                      "quota_per_accepted": 800.0, "finish_seconds_median": 30.0,
                      "n": 3, "small_sample": True},
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(root: Path, ids: tuple[str, ...] = IDS, baseline: bool = True) -> None:
    acc = root / "dev" / "n6" / "acceptance"
    acc.mkdir(parents=True)
    data = json.dumps({"version": 1, "limits": e8.LIMITS, "measures": e8.MEASURES,
                       "slice_ids": list(ids)}, indent=2).encode("utf-8")
    (acc / "acceptance_set.json").write_bytes(data)
    (acc / "SHA256SUMS").write_bytes(f"{_sha(data)}  acceptance_set.json\n".encode("ascii"))
    n6 = root / "build" / "n6"
    n6.mkdir(parents=True)
    for sid, source, passed in (("CASEA1", "n6", True), ("CASEA1", "conveyor", True),
                                ("CASEB1", "n6", False), ("CASEB1", "conveyor", True),
                                ("CASEC1", "n6", True), ("OTHER1", "n6", True)):
        record(n6 / "kpi.db", KpiRow(sid, source, passed, 0, 1, 1000, 60.0, 0, AT, origin="sweep"))
    record_limited(n6 / "kpi.db", LimitedEvent("CASEB1", "run1", AT, AT, "PASS", "0123abcd"))
    if baseline:
        base = json.dumps({"groups": GROUPS}, indent=2, sort_keys=True).encode("utf-8")
        (n6 / "baseline.json").write_bytes(base)
        (n6 / "baseline.json.sha256").write_bytes(f"{_sha(base)}  baseline.json\n".encode("ascii"))


def _main(root: Path, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = pack.main(["--root", str(root), *extra])
    return code, out.getvalue()


def _packet(root: Path) -> Path:
    return root / "build" / "reports" / pack.OUT_NAME


class JudgePackTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_packet_holds_every_section_and_blank_verdicts(self) -> None:
        _fixture(self.root)
        code, said = _main(self.root)
        self.assertEqual(code, 0, said)
        text = _packet(self.root).read_text(encoding="utf-8")
        sums = (self.root / "dev" / "n6" / "acceptance" / "SHA256SUMS").read_text().split()[0]
        self.assertIn(f"PINNED SHA256: {sums} (verified against SHA256SUMS)", text)
        for header in ("## Acceptance set", "## Per-case inputs and observed verdicts",
                       "## E8 report", "## E8 measures vs N6BASE1", "## Open disagreements",
                       "## Verdicts"):
            self.assertIn(header, text)
        self.assertIn("CASE CASEB1\n  n6: passed=no post_accept_defects=0", text)
        self.assertIn("  n6 limited runs: run1 verdict=PASS receipt=0123abcd", text)
        self.assertIn("    VERDICT: DO-NOT-ADVANCE", text)  # E8's own output, indented
        self.assertIn("E8 reason: pass_rate is worse", text)
        self.assertIn("CASEB1: limited run run1 verdict PASS but the n6 KPI row says passed=no", text)
        self.assertIn("COLUMNS: n6 (set); conveyor (set); code/writer-a; docs/writer-b", text)
        self.assertIn("- quota_tokens_per_accepted: n6 (set)=2000; conveyor (set)=1000;"
                      " code/writer-a=1200; docs/writer-b=800", text)
        self.assertIn("N6BASE1 docs/writer-b is a small sample (n < 10)", text)
        for line in pack.VERDICT_LINES:
            self.assertIn(f"- {line}", text)
        # N6 code states no verdict of its own: only the indented E8 copy says VERDICT.
        self.assertFalse([ln for ln in text.splitlines() if ln.startswith("VERDICT")])
        self.assertNotIn(str(self.root), text)

    def test_same_input_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            second = Path(other).resolve()
            for root in (self.root, second):
                _fixture(root)
                self.assertEqual(_main(root)[0], 0)
            first = _packet(self.root).read_bytes()
            self.assertEqual(first, _packet(second).read_bytes())
        self.assertEqual(_main(self.root)[0], 0)
        self.assertEqual(first, _packet(self.root).read_bytes())

    def test_hash_mismatch_refuses_and_writes_nothing(self) -> None:
        _fixture(self.root)
        target = self.root / "dev" / "n6" / "acceptance" / "acceptance_set.json"
        target.write_bytes(target.read_bytes().replace(b"CASEC1", b"CASED1"))
        code, said = _main(self.root)
        self.assertEqual(code, 2)
        self.assertIn("hash mismatch", said)
        self.assertFalse(_packet(self.root).exists())

    def test_missing_measures_read_missing(self) -> None:
        _fixture(self.root)
        self.assertEqual(_main(self.root)[0], 0)
        text = _packet(self.root).read_text(encoding="utf-8")
        self.assertIn("CASE CASEC1\n  n6: passed=yes", text)
        self.assertIn("  conveyor: missing", text)  # unpaired case: no conveyor row
        self.assertIn("- finish_seconds: n6 (set)=120; conveyor (set)=120; code/writer-a=missing;", text)
        self.assertIn("N6BASE1 code/writer-a finish_seconds_median is missing", text)

    def test_absent_baseline_and_no_pairs_read_missing(self) -> None:
        _fixture(self.root, ids=("CASEC1",), baseline=False)
        self.assertEqual(_main(self.root)[0], 0)
        text = _packet(self.root).read_text(encoding="utf-8")
        self.assertIn("BASELINE: baseline.json sha256 missing", text)
        self.assertIn("baseline.json is absent: every N6BASE1 column reads missing", text)
        self.assertIn("no paired set slice: the n6 and conveyor columns read missing", text)
        self.assertIn("- pass_rate: n6 (set)=missing; conveyor (set)=missing; N6BASE1=missing", text)

    def test_baseline_hash_mismatch_refuses(self) -> None:
        _fixture(self.root)
        (self.root / "build" / "n6" / "baseline.json").write_bytes(b'{"groups": {}}')
        code, said = _main(self.root)
        self.assertEqual(code, 2)
        self.assertIn("baseline hash mismatch", said)
        self.assertFalse(_packet(self.root).exists())

    def test_deny_hit_refuses_and_writes_nothing(self) -> None:
        _fixture(self.root)
        deny = self.root / "deny.json"
        deny.write_text(json.dumps({"version": 1, "shapes": [], "words": ["casea"]}), encoding="utf-8")
        with mock.patch.object(dora_screen, "DENY_PATH", deny):
            code, said = _main(self.root)
        self.assertEqual(code, 2)
        self.assertIn("deny/secret screen (deny)", said)
        self.assertFalse(_packet(self.root).exists())

    def test_secret_in_a_case_id_refuses(self) -> None:
        _fixture(self.root, ids=("api_key=" + "A" * 40,))
        code, said = _main(self.root)
        self.assertEqual(code, 2)
        self.assertIn("(secret)", said)
        self.assertFalse(_packet(self.root).exists())

    def test_cannot_write_under_acceptance_or_outside_reports(self) -> None:
        _fixture(self.root)
        acc = self.root / "dev" / "n6" / "acceptance"
        before = sorted(p.name for p in acc.iterdir())
        for out in (acc / "packet.md", self.root / "build" / "reports" / ".." / ".." / "dev" / "n6"
                    / "acceptance" / "packet.md", self.root / "build" / "reports" / "dev" / "n6"
                    / "acceptance" / "packet.md"):
            code, said = _main(self.root, "--out", str(out))
            self.assertEqual(code, 2, out)
            self.assertIn("dev/n6/acceptance/", said)
        for out in (self.root / "build" / "packet.md", self.root / "build" / "reports",
                    self.root / "packet.md"):
            self.assertEqual(_main(self.root, "--out", str(out))[0], 2, out)
        with self.assertRaises(pack.PackError):
            pack.write("x\n", acc / "packet.md", self.root)
        self.assertEqual(sorted(p.name for p in acc.iterdir()), before)
        self.assertFalse((self.root / "build" / "reports").exists())

    def test_operator_database_is_never_opened(self) -> None:
        _fixture(self.root)
        for db in (self.root / "data" / "jarvis.db", self.root / "data" / "kpi.db",
                   self.root / "build" / "jarvis.db"):
            code, said = _main(self.root, "--kpi", str(db))
            self.assertEqual(code, 2, db)
            self.assertIn("operator data", said)
            self.assertFalse(db.exists())
        self.assertFalse((self.root / "data").exists())

    def test_line_break_in_a_value_cannot_forge_a_verdict_line(self) -> None:
        _fixture(self.root)
        record_limited(self.root / "build" / "n6" / "kpi.db",
                       LimitedEvent("CASEA1", "r\n- SALIH: ADVANCE", AT, AT, "PASS", "0123abcd"))
        code, said = _main(self.root)
        self.assertEqual(code, 2)
        self.assertIn("line break", said)
        self.assertFalse(_packet(self.root).exists())

    def test_operator_paths_and_unreadable_database_refuse_without_a_crash(self) -> None:
        _fixture(self.root)
        code, said = _main(self.root, "--out", str(self.root / "data" / "build" / "reports" / "p.md"))
        self.assertEqual(code, 2, said)
        code, said = _main(self.root, "--kpi", str(self.root / "x" / ".jarvis" / "kpi.db"))
        self.assertEqual(code, 2, said)
        bad = self.root / "build" / "n6" / "bad.db"
        bad.write_bytes(b"not a database" * 100)
        code, said = _main(self.root, "--kpi", str(bad))
        self.assertEqual(code, 2, said)
        self.assertFalse(_packet(self.root).exists())

    def test_missing_kpi_database_refuses(self) -> None:
        _fixture(self.root)
        code, said = _main(self.root, "--kpi", str(self.root / "build" / "n6" / "none.db"))
        self.assertEqual(code, 2)
        self.assertIn("no KPI database", said)
        self.assertFalse((self.root / "build" / "n6" / "none.db").exists())

    def test_real_pinned_set_builds(self) -> None:
        _fixture(self.root, baseline=False)
        real = ROOT / "dev" / "n6" / "acceptance" / "acceptance_set.json"
        code, said = _main(self.root, "--set", str(real))
        self.assertEqual(code, 0, said)
        text = _packet(self.root).read_text(encoding="utf-8")
        self.assertIn(f"PINNED SHA256: {e8.load_set(real)[0]}", text)


if __name__ == "__main__":
    unittest.main()
