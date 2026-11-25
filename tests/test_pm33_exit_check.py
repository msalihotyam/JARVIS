"""PM33EXIT2: the stage-2 exit checker judges a run from work_event alone.

No wall clock, no live DB: every log is built from fixed values and every DB
lives in a tempfile.TemporaryDirectory.
"""
import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("pm33_exit_check", ROOT / "scripts" / "pm33_exit_check.py")
pm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pm)

CRITERIA = ("clarify_exit", "no_mid_build_question", "every_slice_leased", "non_claude_or_no_quota",
            "no_scope_violation", "accepted_with_receipt", "notify_points_only", "no_failed_state", "pm30_pm32")


def _base(slices=("s1", "s2")):
    """(kind, step_key, payload) of a passing run."""
    out = [("readback_ready", None, {"turn": 1}),
           ("progress", None, {"gate": "clarify_confirm", "confirmed": True, "turn": 2}),
           ("run_state", None, {"state": "building"}),
           ("progress", None, {"gate": "no_quota", "reason": "codex quota exhausted"})]
    for n, s in enumerate(slices, 1):
        out += [("step_state", s, {"state": "running", "attempt": 1, "epoch": n}),
                ("progress", s, {"gate": "scope", "passed": True}),
                ("progress", s, {"gate": "checks", "passed": True}),
                ("progress", s, {"gate": "regression", "passed": True}),
                ("progress", s, {"gate": "apply_intent", "idempotency_key": "k-" + s}),
                ("progress", s, {"gate": "apply", "passed": True, "idempotency_key": "k-" + s, "ref": "ref-" + s}),
                ("step_state", s, {"state": "accepted", "attempt": 1})]
    return out + [("run_state", None, {"state": "done"}), ("progress", None, {"gate": "notify", "point": "done"})]


def _mk(spec, run="R1"):
    return [{"run_id": run, "seq": i, "kind": k, "step_key": s, "payload_json": json.dumps(p)}
            for i, (k, s, p) in enumerate(spec, 1)]


def _edit(fn):
    return _mk([fn(k, s, dict(p)) for k, s, p in _base()])


def _set(match, **change):
    """Base log with `change` merged into payloads of events matching (kind, step, payload)."""
    return _edit(lambda k, s, p: (k, s, {**p, **change}) if match(k, s, p) else (k, s, p))


def _insert(at, *items):
    spec = _base()
    return _mk(spec[:at] + list(items) + spec[at:])


def _without(pred):
    return _mk([t for t in _base() if not pred(*t)])


def _raw(fn):
    ev = _mk(_base())
    fn(ev)
    return ev


def _is(kind, step=None, **want):
    return lambda k, s, p: k == kind and (step is None or s == step) and all(p.get(a) == b for a, b in want.items())


def _put(i, key, value):
    return lambda ev: ev[i].__setitem__(key, value)


def _relabel(match, kind):
    return _edit(lambda k, s, p: (kind, s, p) if match(k, s, p) else (k, s, p))


# name -> (log builder, the line that must be NOT_YET)
MUTATIONS = {
    "no_events": (lambda: [], "log_integrity"),
    "zero_slices": (lambda: _without(lambda k, s, p: s is not None), "accepted_with_receipt"),
    "attempt2_open": (lambda: _mk(_base() + [("step_state", "s1", {"state": "running", "attempt": 2, "epoch": 3})]),
                      "accepted_with_receipt"),
    "attempt2_failed": (lambda: _mk(_base() + [("step_state", "s1", {"state": "failed_after_cap", "attempt": 2})]),
                        "accepted_with_receipt"),
    "reopened": (lambda: _mk(_base() + [("step_state", "s1", {"state": "reopened", "attempt": 1})]),
                 "accepted_with_receipt"),
    "shared_receipt": (lambda: _set(lambda k, s, p: s == "s2" and "idempotency_key" in p, idempotency_key="k-s1"),
                       "accepted_with_receipt"),
    "receipt_no_intent": (lambda: _without(_is("progress", "s2", gate="apply_intent")), "accepted_with_receipt"),
    "receipt_key_mismatch": (lambda: _set(_is("progress", "s2", gate="apply_intent"), idempotency_key="other"),
                             "accepted_with_receipt"),
    "receipt_no_ref": (lambda: _set(_is("progress", "s1", gate="apply"), ref=""), "accepted_with_receipt"),
    "checks_failed": (lambda: _set(_is("progress", "s2", gate="checks"), passed=False), "accepted_with_receipt"),
    "regression_missing": (lambda: _without(_is("progress", "s1", gate="regression")), "accepted_with_receipt"),
    "seq_gap": (lambda: _raw(lambda ev: ev.pop(5)), "log_integrity"),
    "dup_seq": (lambda: _raw(_put(5, "seq", 5)), "log_integrity"),
    "other_run": (lambda: _raw(_put(5, "run_id", "R2")), "log_integrity"),
    "bad_type_list": (lambda: _raw(lambda ev: ev.__setitem__(3, ["not", "a", "mapping"])), "log_integrity"),
    "bad_seq_str": (lambda: _raw(_put(3, "seq", "4")), "log_integrity"),
    "bad_seq_bool": (lambda: _raw(_put(0, "seq", True)), "log_integrity"),
    "missing_kind": (lambda: _raw(lambda ev: ev[3].pop("kind")), "log_integrity"),
    "unhashable_run_id": (lambda: _raw(_put(3, "run_id", ["R1"])), "log_integrity"),
    "unhashable_state": (lambda: _raw(_put(2, "payload_json", '{"state": {"a": 1}}')), "log_integrity"),
    "payload_bytes": (lambda: _raw(_put(3, "payload_json", b"{}")), "log_integrity"),
    "payload_not_json": (lambda: _raw(_put(3, "payload_json", "{nope")), "log_integrity"),
    "dup_json_keys": (lambda: _raw(_put(5, "payload_json", '{"gate": "scope", "gate": "scope", "passed": true}')),
                      "log_integrity"),
    "unknown_kind_done": (lambda: _relabel(_is("run_state", state="done"), "run_state_v2"), "terminal_done"),
    "unknown_kind_accepted": (lambda: _relabel(_is("step_state", "s1", state="accepted"), "step_state_v2"),
                              "accepted_with_receipt"),
    "readback_same_turn": (lambda: _set(_is("progress", gate="clarify_confirm"), turn=1), "clarify_exit"),
    "confirm_before_readback": (lambda: _mk([_base()[1], _base()[0]] + _base()[2:]), "clarify_exit"),
    "no_terminal": (lambda: _without(_is("run_state", state="done")), "terminal_done"),
    "terminal_failed": (lambda: _set(_is("run_state", state="done"), state="failed"), "terminal_done"),
    "done_then_reopened": (lambda: _mk(_base() + [("run_state", None, {"state": "building"})]), "terminal_done"),
    "scope_violation": (lambda: _insert(4, ("progress", "s1", {"gate": "scope", "passed": False})),
                        "no_scope_violation"),
    "scope_missing": (lambda: _without(_is("progress", "s2", gate="scope")), "no_scope_violation"),
    "mid_build_question": (lambda: _insert(4, ("questions_ready", None, {})), "no_mid_build_question"),
    "never_building": (lambda: _without(_is("run_state", state="building")), "no_mid_build_question"),
    "unfenced": (lambda: _edit(lambda k, s, p: (k, s, {a: b for a, b in p.items() if a != "epoch"})),
                 "every_slice_leased"),
    "claude_only": (lambda: _set(_is("progress", gate="no_quota"), gate="worker_kind", worker="claude-opus"),
                    "non_claude_or_no_quota"),
    "notify_off_point": (lambda: _set(_is("progress", gate="notify"), point="random_chatter"), "notify_points_only"),
    "run_failed": (lambda: _insert(4, ("run_state", None, {"state": "failed"})), "no_failed_state"),
}


def _line(lines, name):
    return next(line for line in lines if line.startswith(name + ":"))


def _run(events, pm30=True, pm32="yes"):
    return pm.evaluate(events, pm30, pm32)


class EvaluateTests(unittest.TestCase):
    def test_passing_log_is_pass_with_one_line_per_criterion(self):
        lines = _run(_mk(_base()))
        self.assertEqual(lines[-1], "OVERALL: PASS", lines)
        for name in ("log_integrity",) + CRITERIA + ("terminal_done",):
            self.assertRegex(_line(lines, name), r"^\w+: PASS \[[0-9,]*\]$")
        self.assertEqual(_line(lines, "unknown_kinds"), "unknown_kinds: 0 []")

    def test_not_yet_lines_carry_evidence_ids(self):
        line = _line(_run(MUTATIONS["checks_failed"][0]()), "accepted_with_receipt")
        self.assertTrue(line.startswith("accepted_with_receipt: NOT_YET s2: checks not passed ["), line)
        self.assertRegex(line, r"\[\d+\]$")

    def test_each_mutation_is_not_yet_on_its_line(self):
        for name, (build, target) in MUTATIONS.items():
            with self.subTest(name):
                lines = _run(build())
                self.assertIn(": NOT_YET", _line(lines, target), lines)
                self.assertEqual(lines[-1], "OVERALL: NOT_YET")

    def test_property_overall_never_pass_after_any_mutation_even_combined(self):
        names, base = sorted(MUTATIONS), _mk(_base())
        for a in names:
            for b in names[::7]:
                with self.subTest(a=a, b=b):
                    log = MUTATIONS[a][0]()
                    other = MUTATIONS[b][0]()
                    self.assertEqual(_run(log)[-1], "OVERALL: NOT_YET")
                    if not (len(log) == len(other) == len(base)):
                        continue  # truncations/insertions do not compose position-wise
                    # a's changes win; b's changes fill every slot a left as base.
                    both = [x if x != o else y for x, o, y in zip(log, base, other)]
                    self.assertEqual(_run(both)[-1], "OVERALL: NOT_YET")

    def test_pm30_and_pm32_gate(self):
        for pm30, pm32 in ((False, "yes"), (True, "no"), (True, "incomplete"), (1, "yes"), (True, "YES")):
            with self.subTest(pm30=pm30, pm32=pm32):
                lines = _run(_mk(_base()), pm30, pm32)
                self.assertIn("NOT_YET", _line(lines, "pm30_pm32"))
                self.assertEqual(lines[-1], "OVERALL: NOT_YET")

    def test_pm32_text_cannot_spoof_overall(self):
        lines = _run(_mk(_base()), True, "x: PASS")
        self.assertEqual(lines[-1], "OVERALL: NOT_YET")

    def test_malformed_events_reported_never_raised(self):
        for junk in (None, 42, "abc", [None, 3.5, {"seq": 1}], [{"seq": {}}]):
            with self.subTest(junk=junk):
                lines = _run(junk)
                self.assertIn("NOT_YET log integrity", _line(lines, "log_integrity"))
                self.assertEqual(lines[-1], "OVERALL: NOT_YET")
        line = _line(_run(MUTATIONS["payload_not_json"][0]()), "log_integrity")
        self.assertIn("malformed events", line)
        self.assertIn("4:JSONDecodeError", line)

    def test_unknown_kind_counted_and_printed(self):
        spec = _base()
        lines = _run(_mk(spec[:3] + [("telemetry_v9", None, {"state": "done"})] + spec[3:]))
        self.assertEqual(_line(lines, "unknown_kinds"), "unknown_kinds: 1 [4]")
        lines = _run(MUTATIONS["unknown_kind_done"][0]())
        self.assertTrue(_line(lines, "unknown_kinds").startswith("unknown_kinds: 1 ["))

    def test_seq_order_in_input_does_not_matter(self):
        self.assertEqual(_run(list(reversed(_mk(_base()))))[-1], "OVERALL: PASS")


_SCHEMA = """
CREATE TABLE work_event (run_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK(seq>=1), kind TEXT NOT NULL,
  step_key TEXT NULL, payload_json TEXT NOT NULL, idempotency_key TEXT NULL UNIQUE, at TEXT NOT NULL,
  PRIMARY KEY(run_id, seq));
CREATE TABLE work_run (run_id TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE work_step (run_id TEXT NOT NULL, step_key TEXT NOT NULL, state TEXT NOT NULL);
"""


def _db(folder, events, snapshot=(("R1", "s1", "accepted"), ("R1", "s2", "accepted"))):
    path = Path(folder) / "run.db"
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO work_event (run_id, seq, kind, step_key, payload_json, at) "
                         "VALUES (:run_id, :seq, :kind, :step_key, :payload_json, '2026-01-01T00:00:00Z')", events)
        conn.executemany("INSERT INTO work_step VALUES (?, ?, ?)", snapshot)
        conn.executemany("INSERT OR IGNORE INTO work_run VALUES (?, 'done')", [(r[0],) for r in snapshot])
    return path


def _main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pm.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = _db(self._tmp.name, _mk(_base()))

    def test_cli_pass_and_exit_codes(self):
        code, out, _ = _main(self.db, "R1", "--pm30-green", "yes", "--pm32-verdict", "yes")
        self.assertEqual((code, out.splitlines()[-1]), (0, "OVERALL: PASS"))
        self.assertEqual(_main(self.db, "R1")[0], 1)  # defaults: pm30 no, pm32 incomplete

    def test_unknown_or_bad_run_id_refused_with_2(self):
        for run in ("nope", "../x", ""):
            with self.subTest(run=run):
                code, out, err = _main(self.db, run)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("REFUSED", err)

    def test_snapshot_only_run_is_not_pass(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO work_step VALUES ('R9', 's1', 'accepted')")
            conn.execute("INSERT INTO work_run VALUES ('R9', 'done')")
        self.assertEqual(_main(self.db, "R9", "--pm30-green", "yes", "--pm32-verdict", "yes")[0], 2)
        self.assertEqual(_run([])[-1], "OVERALL: NOT_YET")

    def test_live_db_path_and_missing_file_refused(self):
        live = Path(self._tmp.name) / "data" / "jarvis.db"
        live.parent.mkdir()
        live.write_bytes(self.db.read_bytes())
        self.assertEqual(_main(live, "R1")[0], 2)
        self.assertEqual(_main(Path(self._tmp.name) / "missing.db", "R1")[0], 2)

    def test_connection_is_read_only(self):
        with contextlib.closing(pm._connect(self.db)) as conn:
            with self.assertRaises(sqlite3.Error):
                conn.execute("DELETE FROM work_event")

    def test_every_statement_names_only_work_event(self):
        seen, real = [], pm._connect

        def traced(path):
            conn = real(path)
            conn.set_trace_callback(seen.append)
            return conn

        with unittest.mock.patch.object(pm, "_connect", traced):
            self.assertEqual(_main(self.db, "R1", "--pm30-green", "yes", "--pm32-verdict", "yes")[0], 0)
        self.assertTrue(seen)
        for sql in seen:
            self.assertIn("work_event", sql)
            for table in ("work_run", "work_step", "work_lease", "notify_outbox"):
                self.assertNotIn(table, sql)

    def test_output_identical_when_snapshot_stale_contradictory_or_deleted(self):
        argv = (self.db, "R1", "--pm30-green", "yes", "--pm32-verdict", "yes")
        first = _main(*argv)
        edits = ("UPDATE work_step SET state = 'failed'", "UPDATE work_run SET state = 'running'",
                 "INSERT INTO work_step VALUES ('R1', 's3', 'running')", "DELETE FROM work_step",
                 "DROP TABLE work_step", "DROP TABLE work_run")
        for sql in edits:
            with self.subTest(sql=sql):
                with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(sql)
                self.assertEqual(_main(*argv), first)


if __name__ == "__main__":
    unittest.main()
