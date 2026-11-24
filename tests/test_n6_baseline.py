"""N6BASE1: E8 conveyor baseline per lane class and writer, recomputed read-only."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis import n6_kpi

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 1, 9, 0)
_SPEC = importlib.util.spec_from_file_location("n6_baseline", ROOT / "scripts" / "n6_baseline.py")
nb = importlib.util.module_from_spec(_SPEC)
sys.modules["n6_baseline"] = nb
_SPEC.loader.exec_module(nb)
MEASURES = ("pass_rate", "post_accept_failures", "interventions", "quota_per_accepted",
            "finish_seconds_median")


def lane(name, minutes, status="PASS", route="slice model", start=0, salih=0, codex_commit=False,
         extra=()):
    t = T0 + timedelta(hours=start)
    out = [{"event": "dispatch", "lane": name, "ts": t.isoformat(), "route": route}]
    out += [{"event": "reader", "lane": name, "reading": {"kind": "needs_salih"}}] * salih
    out += [dict(e, lane=name) for e in extra]
    if codex_commit:
        out.append({"event": "codex_commit", "lane": name})
    if status is not None:
        end = t + timedelta(minutes=minutes)
        out.append({"event": "finish", "lane": name, "ts": end.isoformat(), "status": status})
    return out


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.events = self.root / "build" / "conveyor" / "events.jsonl"
        self.kpi = self.root / "build" / "n6" / "kpi.db"
        self.out = self.root / "build" / "n6" / "baseline.json"
        self.events.parent.mkdir(parents=True)
        self.kpi.parent.mkdir(parents=True)
        home = mock.patch.object(nb, "OPERATOR_HOME", self.root / "home" / ".jarvis")
        home.start()
        self.addCleanup(home.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def run_out(self, *argv, root=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = nb.main(list(argv), root=root or self.root)
        return code, buf.getvalue()

    def snapshot(self):
        return {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def ledger(self, *lanes, raw=b""):
        lines = [json.dumps(e) for group in lanes for e in group]
        self.events.write_bytes("".join(x + "\n" for x in lines).encode() + raw)

    def kpi_row(self, name, post, quota):
        n6_kpi.record(self.kpi, n6_kpi.KpiRow(name, "conveyor", True, post, 0, quota, 1.0, 0,
                                              datetime(2026, 11, 2, tzinfo=timezone.utc)))

    def run_main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return nb.main(list(argv), root=self.root)

    def doc(self, *argv):
        self.assertEqual(self.run_main(*argv), 0)
        return json.loads(self.out.read_bytes())

    def standard(self):
        self.ledger(lane("A1", 10), lane("A2", 20), lane("A3", 30, status="BLOCKED", salih=1),
                    lane("CDX-A1", 5, route="Codex wrapper"), lane("CODEX1", 40, codex_commit=True))
        self.kpi_row("A1", 1, 100)
        self.kpi_row("A2", 0, 300)


class AcceptanceTests(Fixture):
    def test_every_measure_and_n_per_class_and_writer(self):
        self.standard()
        groups = self.doc()["groups"]
        self.assertEqual(sorted(groups), ["code/claude", "code/codex", "review/claude", "review/codex"])
        for group in groups.values():
            for key in MEASURES + ("n", "small_sample", "kpi_rows_measured"):
                self.assertIn(key, group)
        code = groups["code/claude"]
        self.assertEqual((code["n"], code["accepted"], code["pass_rate"]), (3, 2, 0.666667))
        self.assertEqual((code["post_accept_failures"], code["quota_per_accepted"]), (1, 200.0))
        self.assertEqual((code["interventions"], code["interventions_n"]), (1, 1))
        self.assertEqual(code["finish_seconds_median"], 1200.0)
        self.assertEqual(groups["review/claude"]["n"], 1)  # "Codex wrapper" is a Claude CDX lane
        self.assertEqual(groups["review/codex"]["n"], 0)
        self.assertEqual(groups["code/codex"]["n"], 1)
        self.assertEqual(groups["code/codex"]["finish_seconds_median"], 2400.0)

    def test_accepted_is_conveyor_good_and_writer_from_ledger(self):
        self.ledger(lane("A1", 10, status="COMPLETE"), lane("A2", 10, status="DONE"),
                    lane("A3", 10, status="FAILED"),
                    lane("AUTO1", 10, status="BLOCKED", extra=[{"event": "route", "worker": "codex"}]),
                    lane("J1", 10, extra=[{"event": "jules_state", "new": "RUNNING"}]),
                    lane("J2", 10, extra=[{"event": "route", "worker": "jules"}]))
        doc = self.doc()
        code, codex = doc["groups"]["code/claude"], doc["groups"]["code/codex"]
        self.assertEqual((code["n"], code["accepted"], code["pass_rate"]), (3, 2, 0.666667))
        self.assertEqual((codex["n"], codex["accepted"], codex["pass_rate"]), (1, 0, 0.0))
        self.assertEqual(doc["excluded"], {"jules_lane": 2})

    def test_rerun_is_byte_identical_and_hash_file_matches(self):
        self.standard()
        self.assertEqual(self.run_main(), 0)
        first = self.out.read_bytes()
        sums = self.out.with_name("baseline.json.sha256")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.out.read_bytes(), first)
        self.assertEqual(sums.read_bytes(),
                         f"{hashlib.sha256(first).hexdigest()}  baseline.json\n".encode())

    def test_window_and_source_files_recorded(self):
        self.ledger(lane("OLD1", 5, start=-48), lane("A1", 10), lane("LATE1", 5, start=48))
        self.kpi_row("A1", 0, 50)
        doc = self.doc("--since", "2026-11-01T00:00:00", "--until", "2026-11-02T00:00:00")
        self.assertEqual(doc["window"], {"since": "2026-11-01T00:00:00", "until": "2026-11-02T00:00:00",
                                         "first_dispatch": "2026-11-01T09:00:00",
                                         "last_finish": "2026-11-01T09:10:00"})
        self.assertEqual(doc["excluded"], {"outside_window": 2})
        events = doc["sources"]["events"]
        self.assertEqual(events["path"], str(self.events.resolve()))
        self.assertEqual(events["sha256"], hashlib.sha256(self.events.read_bytes()).hexdigest())
        self.assertEqual(events["lines"], 6)
        kpi = doc["sources"]["kpi_db"]
        self.assertEqual((kpi["path"], kpi["present"], kpi["conveyor_rows"]),
                         (str(self.kpi.resolve()), True, 1))
        self.assertEqual(kpi["sha256"], hashlib.sha256(self.kpi.read_bytes()).hexdigest())


class NegativeTests(Fixture):
    def test_missing_measure_is_missing_never_zero(self):
        self.ledger(lane("A1", 10), lane("A2", 20))  # no kpi.db at all
        doc = self.doc()
        code = doc["groups"]["code/claude"]
        for key in ("post_accept_failures", "quota_per_accepted", "interventions"):
            self.assertEqual(code[key], "missing")  # no reader event either: not 0
        self.assertEqual((code["kpi_rows_measured"], code["interventions_n"]), (0, 0))
        for key in MEASURES:  # an empty group has no reading at all
            self.assertEqual(doc["groups"]["review/claude"][key], "missing")
        self.assertFalse(self.kpi.exists())  # never created by the read
        self.assertEqual(doc["sources"]["kpi_db"]["sha256"], "missing")

    def test_n_below_ten_is_small_sample(self):
        self.ledger(*(lane(f"A{i}", 10) for i in range(9)), *(lane(f"CDX-{i}", 10) for i in range(10)))
        groups = self.doc()["groups"]
        self.assertEqual((groups["code/claude"]["n"], groups["code/claude"]["small_sample"]), (9, True))
        self.assertEqual((groups["review/claude"]["n"], groups["review/claude"]["small_sample"]),
                         (10, False))

    def test_fix_and_int_lanes_are_not_slices(self):
        self.ledger(lane("INT-A1", 5), lane("DRY-1", 5), lane("A1FIX", 5), lane("INT-A1-AFX", 5),
                    lane("B1-S", 5), lane("MERGE1", 5, route="integration"), lane("A1", 5))
        doc = self.doc()
        self.assertEqual(doc["excluded"], {"fix_lane": 3, "int_lane": 3})
        self.assertEqual(sum(g["n"] for g in doc["groups"].values()), 1)

    def test_refuses_to_write_under_conveyor_or_acceptance(self):
        self.standard()
        other = tempfile.TemporaryDirectory()  # another checkout, outside --root
        self.addCleanup(other.cleanup)
        elsewhere = Path(other.name)
        for out in (self.root / "build" / "conveyor" / "baseline.json",
                    self.root / "dev" / "n6" / "acceptance" / "x" / "baseline.json",
                    self.root / "build" / "n6" / ".." / "conveyor" / "b.json",
                    elsewhere / "build" / "conveyor" / "b.json",
                    elsewhere / "Dev" / "N6" / "Acceptance" / "b.json"):
            with self.subTest(out=str(out)):
                self.assertEqual(self.run_main("--out", str(out)), 2)
                self.assertFalse(out.exists())
                self.assertFalse(out.with_name(out.name + ".sha256").exists())
        self.assertFalse((self.root / "dev").exists())
        self.assertEqual(sorted(p.name for p in self.events.parent.iterdir()), ["events.jsonl"])

    def test_refuses_operator_database(self):
        self.standard()
        self.assertEqual(self.run_main("--kpi-db", str(self.root / "elsewhere" / "jarvis.db")), 2)
        self.assertEqual(self.run_main("--kpi-db", str(self.root / "data" / "kpi.db")), 2)
        self.assertEqual(self.run_main("--events", str(self.root / "x" / "jarvis.db")), 2)
        self.assertEqual(self.run_main("--events", str(self.root / "data" / "events.jsonl")), 2)
        self.assertFalse(self.out.exists())

    def assert_refused(self, argv, reason, root=None):
        before = self.snapshot()
        code, text = self.run_out(*argv, root=root)
        self.assertEqual(code, 2, text[:80])
        self.assertIn(reason, text)
        self.assertEqual(self.snapshot(), before)  # nothing written, created or overwritten

    def test_out_refuses_operator_data(self):  # CDX-N6BASE1 F1
        self.standard()
        data, home = self.root / "data", self.root / "home" / ".jarvis"
        outs = [data / "jarvis.db", data / "b.json", data / "x" / ".." / "b.json",
                self.root / "elsewhere" / "JARVIS.DB", home / "b.json"]
        if os.name == "nt":  # case-insensitive filesystem: DATA is data
            outs.append(self.root / "DATA" / "b.json")
        for out in outs:
            with self.subTest(out=str(out)):
                self.assert_refused(("--out", str(out)), "operator data")
        self.assertFalse(data.exists())
        self.assertFalse(home.exists())
        # a worktree root: the main checkout's data/ is operator data too
        work = self.root / "main" / ".claude" / "worktrees" / "w1"
        self.assert_refused(("--events", str(self.events), "--kpi-db", str(self.kpi),
                             "--out", str(self.root / "main" / "data" / "b.json")),
                            "operator data", root=work)
        self.assertFalse((self.root / "main").exists())

    def test_out_refuses_its_own_sources(self):  # CDX-N6BASE1 F1
        self.standard()
        ledger = self.root / "ledger" / "events.jsonl"  # not under build/conveyor: _guard alone passes
        ledger.parent.mkdir()
        ledger.write_bytes(self.events.read_bytes())
        os.link(ledger, ledger.with_name("hard.jsonl"))
        sums_src = self.root / "ledger" / "x.json.sha256"
        sums_src.write_bytes(b"keep")
        fresh = self.root / "ledger" / "new.db"  # a --kpi-db not yet present
        cases = [(ledger, self.kpi, ledger),
                 (ledger, self.kpi, self.root / "ledger" / ".." / "ledger" / "events.jsonl"),
                 (ledger, self.kpi, ledger.with_name("hard.jsonl")),
                 (ledger, self.kpi, self.kpi),
                 (ledger, self.kpi, self.kpi.with_name("kpi.db-wal")),
                 (ledger, self.kpi, self.kpi.with_name("kpi.db-journal")),
                 (ledger, sums_src, sums_src.with_name("x.json")),  # out.sha256 == --kpi-db
                 (ledger, fresh, fresh)]
        if os.name == "nt":  # NTFS is case-insensitive: another spelling is the same file
            cases.append((ledger, self.kpi, self.kpi.with_name("KPI.DB")))
        for events, kpi, out in cases:
            with self.subTest(out=str(out), kpi=kpi.name):
                self.assert_refused(("--events", str(events), "--kpi-db", str(kpi), "--out", str(out)),
                                    "would overwrite the source")
        self.assertFalse(fresh.exists())

    def test_non_ascii_out_name_writes_utf8_checksum(self):  # CDX-N6BASE1 F2
        self.standard()
        out = self.root / "build" / "n6" / "çıktı_ğüş.json"
        code, text = self.run_out("--out", str(out))
        self.assertEqual(code, 0, text[:80])
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        self.assertEqual(out.with_name(out.name + ".sha256").read_bytes(),
                         f"{digest}  çıktı_ğüş.json\n".encode("utf-8"))

    def test_out_refuses_ambiguous_spellings_and_db_sidecars(self):  # N6BASE1FIX F2 probes
        self.standard()
        (self.root / "data").mkdir()
        self.assert_refused(("--out", str(self.root / "elsewhere" / "jarvis.db-wal")), "operator data")
        ambiguous = [self.root / "data " / "b.json", Path(str(self.kpi) + ":s"), Path(str(self.root / "data") + ":s"),
                     self.root / "home" / ".jarvis." / "b.json", Path("\\\\?\\" + str(self.root / "data" / "b.json"))]
        for out in ambiguous if os.name == "nt" else ambiguous[:4]:
            with self.subTest(out=str(out)):
                self.assert_refused(("--out", str(out)), "REFUSED")
        self.assertEqual(list((self.root / "data").iterdir()), [])
        self.assertFalse((self.root / "home").exists())

    def test_nul_in_out_is_refused_not_a_traceback(self):
        self.standard()
        self.assert_refused(("--out", str(self.root / "build" / "n6" / "a\x00b.json")), "REFUSED")

    def test_sums_write_failure_leaves_no_baseline(self):
        self.standard()
        self.out.with_name("baseline.json.sha256").mkdir()  # the .sha256 write fails
        code, text = self.run_out()
        self.assertEqual(code, 2, text[:80])
        self.assertFalse(self.out.exists())

    def test_nul_byte_alone_raises(self):
        self.ledger(lane("A1", 10), raw=b'{"event": "note"}\x00\n')
        with self.assertRaisesRegex(nb.BaselineError, "NUL"):
            nb.read_events(self.events)

    def test_torn_or_corrupt_ledger_line_raises(self):
        good = lane("A1", 10)
        cases = {"nul": b'{"event": "dispatch", "lane": "B\x00\x00\n',
                 "truncated": b'{"event": "finish", "lane": "A1"',
                 "corrupt": b'{"event": "dispatch",, }\n',
                 "not_object": b'[1, 2]\n',
                 "blank": b'\n',
                 "bad_ts": b'{"event": "finish", "lane": "A1", "ts": "yesterday"}\n'}
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.ledger(good, raw=raw)
                with self.assertRaises(nb.BaselineError):
                    nb.read_events(self.events)
                self.assertEqual(self.run_main(), 2)
                self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
