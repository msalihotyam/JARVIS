"""DFSPEED: every text turn records its timing in the envelope's usage record
(``intent.usage.timing``) and a streamed reply's detail line reads "first text X s
· total Y s". Measure only: nothing here changes what a turn does.

No live model, network or CLI: the adapter's process is a fake, the clocks are
controlled, and every store is the test's own TEMP database.
"""

from __future__ import annotations

import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jarvis.app as app_module
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import ErrorCode, StructuredError
from jarvis.intent import CHAT_REPLY_MAX_CHARS, ClaudeIntentAdapter
from jarvis.ui.bridge import _waited, reported_tokens
from tests.test_intent import IntentTestCase, ScriptedModel, proposal, stream_line
import tests.test_ui as ui
from tests.test_ui import ReportingModel, SurfaceCase, UiTestCase, pump

LOGGED_IN = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}
REPLY = "Merhaba, nasıl yardımcı olabilirim?"


class FakeCliProcess:
    """A finished Claude CLI call: its stream-json stdout, read line by line."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stderr = io.StringIO("")
        self.stdin = io.StringIO()
        self.args = ["claude"]
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def communicate(self, *args, **kwargs):
        return "", ""


class MarkedModel(ScriptedModel):
    """A scripted model that reports fixed adapter marks, like ``ClaudeIntentAdapter.timings``."""

    def __init__(self, marks, *answers, **kwargs):
        super().__init__(*answers, **kwargs)
        self.marks = marks
        self.timings = {}

    def complete(self, task, session):
        self.timings[task.id] = dict(self.marks)
        return super().complete(task, session)


def app_clock(*ticks):
    """``jarvis.app``'s own clock, read in order; nothing else's time moves."""
    return patch.object(app_module, "time", SimpleNamespace(monotonic=iter(ticks).__next__))


class TurnTimingTests(IntentTestCase):
    def test_the_real_adapter_records_every_phase_in_order(self) -> None:
        """The Claude adapter through the real ``execute``: auth check, spawn, first and
        last streamed text, exit - each marked once, in that order, on one clock."""
        adapter = ClaudeIntentAdapter(allowed_roots=(Path.cwd(),), default_working_directory=Path.cwd())
        answer = json.dumps({"kind": "chat", "reply": REPLY})
        deltas = [answer[:20], answer[20:40], answer[40:]]
        lines = [stream_line(d) for d in deltas]
        lines.append(json.dumps({"type": "result", "result": answer, "usage": {"output_tokens": 12}}) + "\n")
        tick = itertools.count(100.0, 0.5)
        app = self.build(adapter)
        with patch.object(ClaudeCodeConnector, "_auth_status", return_value=LOGGED_IN), \
                patch.object(ClaudeCodeConnector, "prohibited_billing_env_vars", return_value=()), \
                patch.object(ClaudeCodeConnector, "_argv0", return_value="claude"), \
                patch("jarvis.connectors.claude_code.subprocess.Popen", return_value=FakeCliProcess(lines)), \
                patch("time.monotonic", side_effect=lambda: next(tick)):
            previews = []
            payload = app.submit_text("Selam", on_reply_preview=previews.append).payload
        self.assertEqual(("conversation", REPLY), (payload["intent"]["status"], payload["intent"]["reply"]))
        marks = adapter.timings[payload["intent"]["request_id"]]
        order = ["started", "auth_started", "auth_ended", "spawned", "first_text", "last_text", "exited", "ended"]
        self.assertEqual(sorted(order + ["output_chars"]), sorted(marks))
        self.assertEqual(sorted(marks[name] for name in order), [marks[name] for name in order])
        self.assertLess(marks["first_text"], marks["last_text"])
        self.assertEqual(len(answer), marks["output_chars"])
        timing = payload["intent"]["usage"]["timing"]
        self.assertEqual({"total_ms", "auth_ms", "spawn_ms", "first_text_ms", "first_visible_ms", "text_ms",
                          "parse_ms", "output_chars", "lane", "t_route", "t_proc_ready", "t_stdin_write",
                          "t_first_token", "t_turn_done", "tokens_out"}, set(timing))
        self.assertEqual("current", timing.pop("lane"))
        self.assertTrue(all(type(v) is int and v > 0 for v in timing.values()), timing)
        self.assertEqual(12, timing["tokens_out"])
        self.assertLess(timing["t_route"], timing["t_proc_ready"])
        self.assertEqual((timing["spawn_ms"], timing["first_visible_ms"], timing["total_ms"]),
                         (timing["t_proc_ready"], timing["t_first_token"], timing["t_turn_done"]))
        self.assertLess(timing["auth_ms"], timing["spawn_ms"])                       # the auth check precedes the spawn
        self.assertEqual(timing["first_visible_ms"], timing["spawn_ms"] + timing["first_text_ms"])
        self.assertLess(timing["first_visible_ms"] + timing["text_ms"] + timing["parse_ms"], timing["total_ms"])
        self.assertEqual(12, payload["intent"]["usage"]["reported"]["output_tokens"])   # tokens: as the CLI reported them

    def test_exact_numbers_on_a_controlled_clock(self) -> None:
        marks = {"started": 10.2, "auth_started": 10.5, "auth_ended": 11.3, "spawned": 11.5,
                 "first_text": 13.0, "last_text": 20.0, "exited": 20.5, "ended": 20.6, "output_chars": 1234}
        app = self.build(MarkedModel(marks, proposal(kind="chat", reply=REPLY)))
        with app_clock(10.0, 10.1, 21.0, 21.25, 21.5):      # start, routed, model returned, validated, answered
            payload = app.submit_text("Selam").payload
        self.assertEqual({"total_ms": 11500, "auth_ms": 800, "spawn_ms": 1500, "first_text_ms": 1500,
                          "first_visible_ms": 3000, "text_ms": 7000, "parse_ms": 250, "output_chars": 1234,
                          "lane": "current", "t_route": 100, "t_proc_ready": 1500, "t_stdin_write": 1500,
                          "t_first_token": 3000, "t_turn_done": 11500},
                         payload["intent"]["usage"]["timing"])

    def test_speedbase_fields_carry_the_model_effort_and_reported_usage(self) -> None:
        marks = {"started": 0.1, "spawned": 0.5, "first_text": 1.0, "last_text": 2.0}
        model = MarkedModel(marks, proposal(kind="chat", reply=REPLY))
        model.model, model.effort, model.reported = "sonnet", "low", {}
        usage = {"input_tokens": 7, "output_tokens": 40, "cache_read_input_tokens": 9000,
                 "cache_creation_input_tokens": 300, "service_tier": "standard"}
        model.on_complete = lambda task: model.reported.__setitem__(task.id, usage)
        with app_clock(0.0, 0.05, 2.5, 2.6, 2.7):
            timing = self.build(model).submit_text("Selam").payload["intent"]["usage"]["timing"]
        self.assertEqual({"lane": "current", "model": "sonnet", "effort": "low", "t_route": 50,
                          "t_proc_ready": 500, "t_stdin_write": 500, "t_first_token": 1000, "t_turn_done": 2700,
                          "tokens_in": 7, "tokens_out": 40, "cache_read": 9000, "cache_creation": 300},
                         {k: v for k, v in timing.items() if k.startswith(("t_", "tokens", "cache"))
                          or k in ("lane", "model", "effort")})

    def test_a_model_without_marks_still_gets_its_total_and_answer_size(self) -> None:
        app = self.build(ScriptedModel(proposal(kind="chat", reply=REPLY)))
        with app_clock(0.0, 0.2, 4.0, 4.1, 4.2):
            payload = app.submit_text("Selam").payload
        answer = proposal(kind="chat", reply=REPLY)
        self.assertEqual({"total_ms": 4200, "parse_ms": 100, "output_chars": len(answer), "lane": "current",
                          "t_route": 200, "t_turn_done": 4200}, payload["intent"]["usage"]["timing"])

    def test_refused_and_failed_turns_never_crash_and_record_what_happened(self) -> None:
        # Refused before any model is asked: no usage record at all, exactly as before.
        app = self.build(ScriptedModel(available=False))
        payload = app.submit_text("Selam").payload
        self.assertEqual(("model_unavailable", None), (payload["intent"]["status"], payload["intent"]["usage"]))
        payload = self.build(ScriptedModel()).submit_text("bad\x00").payload
        self.assertEqual(("invalid_input", None), (payload["intent"]["status"], payload["intent"]["usage"]))
        # The model failed: its total and how long the failure took to judge.
        failing = ScriptedModel(error=StructuredError(code=ErrorCode.CONNECTOR_FAILED, message="boom"))
        with app_clock(0.0, 0.0, 2.0, 2.5):
            payload = self.build(failing).submit_text("Selam").payload
        self.assertEqual("model_failed", payload["intent"]["status"])
        self.assertEqual({"total_ms": 2500, "parse_ms": 500, "lane": "current", "t_route": 0, "t_turn_done": 2500},
                         payload["intent"]["usage"]["timing"])
        # The adapter raised: the total alone - no phase is invented.
        raising = ScriptedModel()
        raising.on_complete = lambda task: (_ for _ in ()).throw(RuntimeError("x"))
        with app_clock(0.0, 0.0, 3.0):
            payload = self.build(raising).submit_text("Selam").payload
        self.assertEqual(("model_failed", {"total_ms": 3000, "lane": "current", "t_route": 0, "t_turn_done": 3000}),
                         (payload["intent"]["status"], payload["intent"]["usage"]["timing"]))


class WarmLaneWiringTests(IntentTestCase):
    """SPEEDWARM2D: the chat turn names its conversation as the adapter's warm lane
    for the call only, and the timing records the lane the adapter reported."""

    def test_the_timing_records_the_lane_the_adapter_marked(self) -> None:
        marks = {"started": 0.1, "spawned": 0.5, "lane": "warm"}
        with app_clock(0.0, 0.05, 2.5, 2.6, 2.7):
            timing = self.build(MarkedModel(marks, proposal(kind="chat", reply=REPLY))).submit_text(
                "Selam").payload["intent"]["usage"]["timing"]
        self.assertEqual("warm", timing["lane"])

    def lane_model(self, *answers, **kwargs):
        model = ScriptedModel(*answers, **kwargs)
        model.warm_lane, model.seen = None, []
        previous = model.on_complete
        model.on_complete = lambda task: (model.seen.append(model.warm_lane), previous and previous(task))
        return model

    def test_the_lane_is_the_conversation_during_the_call_and_cleared_after_success(self) -> None:
        conversation = self.memory.create_conversation()
        model = self.lane_model(proposal(kind="chat", reply=REPLY))
        payload = self.build(model).submit_text("Selam", conversation_id=conversation.id).payload
        self.assertEqual("conversation", payload["intent"]["status"])
        self.assertEqual(([conversation.id], None), (model.seen, model.warm_lane))

    def test_the_lane_is_cleared_after_a_failed_and_a_raising_call(self) -> None:
        conversation = self.memory.create_conversation()
        failing = self.lane_model(error=StructuredError(code=ErrorCode.CONNECTOR_FAILED, message="boom"))
        payload = self.build(failing).submit_text("Selam", conversation_id=conversation.id).payload
        self.assertEqual(("model_failed", [conversation.id], None),
                         (payload["intent"]["status"], failing.seen, failing.warm_lane))
        raising = self.lane_model()
        record = raising.on_complete
        raising.on_complete = lambda task: (record(task), (_ for _ in ()).throw(RuntimeError("x")))
        payload = self.build(raising).submit_text("Selam", conversation_id=conversation.id).payload
        self.assertEqual(("model_failed", [conversation.id], None),
                         (payload["intent"]["status"], raising.seen, raising.warm_lane))

    def test_the_lane_is_cleared_after_a_cancelled_call(self) -> None:
        conversation = self.memory.create_conversation()
        model = self.lane_model(proposal(kind="chat", reply=REPLY))
        app = self.build(model)
        inner = model.on_complete
        model.on_complete = lambda task: (inner(task), app.cancel_text(task.id))
        payload = app.submit_text("Selam", conversation_id=conversation.id, request_id="r-cancel").payload
        self.assertEqual("model_cancelled", payload["intent"]["status"])
        self.assertEqual((["r-cancel"], [conversation.id], None), (model.cancelled, model.seen, model.warm_lane))

    def test_a_model_without_a_warm_lane_is_left_alone(self) -> None:
        model = ScriptedModel(proposal(kind="chat", reply=REPLY))
        self.build(model).submit_text("Selam", conversation_id=self.memory.create_conversation().id)
        self.assertFalse(hasattr(model, "warm_lane"))

    def test_flag_off_the_real_adapter_spawns_the_same_argv_with_or_without_a_lane(self) -> None:
        """warm_claude off (the default): the lane is set for the call, yet the adapter
        takes the one-shot path - same argv, no warm session kept."""
        adapter = ClaudeIntentAdapter(allowed_roots=(Path.cwd(),), default_working_directory=Path.cwd())
        self.assertFalse(adapter.warm_claude)
        answer = json.dumps({"kind": "chat", "reply": REPLY})
        spawned = []

        def popen(argv, *args, **kwargs):
            spawned.append((list(argv), adapter.warm_lane))
            return FakeCliProcess([stream_line(answer[:20]), stream_line(answer[20:]), json.dumps(
                {"type": "result", "result": answer, "usage": {"output_tokens": 12}}) + "\n"])

        tick = itertools.count(100.0, 0.5)
        app = self.build(adapter)
        conversation = self.memory.create_conversation()
        with patch.object(ClaudeCodeConnector, "_auth_status", return_value=LOGGED_IN), \
                patch.object(ClaudeCodeConnector, "prohibited_billing_env_vars", return_value=()), \
                patch.object(ClaudeCodeConnector, "_argv0", return_value="claude"), \
                patch("jarvis.connectors.claude_code.subprocess.Popen", side_effect=popen), \
                patch("time.monotonic", side_effect=lambda: next(tick)):
            plain = app.submit_text("Selam", on_reply_preview=[].append).payload
            laned = app.submit_text("Selam", conversation_id=conversation.id, on_reply_preview=[].append).payload
        self.assertEqual(["conversation", "conversation"], [plain["intent"]["status"], laned["intent"]["status"]],
                         (plain.get("error"), spawned))
        (plain_argv, plain_lane), (laned_argv, laned_lane) = spawned
        self.assertEqual((None, conversation.id), (plain_lane, laned_lane))
        self.assertEqual(plain_argv, laned_argv)
        self.assertEqual(({}, None), (adapter._warm, adapter.warm_lane))
        self.assertEqual("current", laned["intent"]["usage"]["timing"]["lane"])


class SpeedBaselineScriptTests(unittest.TestCase):
    def test_the_script_refuses_the_live_data_dir(self) -> None:
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("speed_baseline", root / "scripts" / "speed_baseline.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        for live in (root / "data", root / "data" / "sub", root / "data" / "jarvis.db"):
            with self.assertRaises(SystemExit):
                script.refuse_live(live)
        script.refuse_live(Path(tempfile.gettempdir()) / "speedbase-x")     # a TEMP dir passes


class DetailLineTests(UiTestCase):
    def test_a_rollback_era_reader_ignores_the_timing_field(self) -> None:
        """What a release without DFSPEED reads from the usage record - the reported
        tokens, the measured line, the provider limits - is the same with ``timing`` or without."""
        usage = {"provider": "anthropic", "reported": {"input_tokens": 500, "output_tokens": 300}}
        shown = []
        for record in (usage, {**usage, "timing": {"total_ms": 9000, "auth_ms": 700, "queued_ms": 50}}):
            bridge = self.bridge()
            ticks = [0.0, 9.0]
            bridge._clock = lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0]
            payload = {"command": "text", "intent": {"status": "conversation", "reply": REPLY, "usage": json.loads(json.dumps(record))}}
            bridge._run("task", lambda: payload, lambda p: bridge._apply_text(p, "Selam"))
            shown.append((bridge.feed[-1]["text"], bridge.feed[-1].get("telemetry"),
                          reported_tokens(*bridge._intent_reports(payload["intent"]))))
        self.assertEqual(shown[0], shown[1])
        self.assertEqual({"elapsed_ms": 9000, "tokens": 800}, shown[0][1])

    def test_a_refused_turn_without_timing_shows_nothing_and_breaks_nothing(self) -> None:
        bridge = self.bridge()
        refused = {"command": "text", "intent": {"status": "model_unavailable", "message": "off", "usage": None}}
        bridge._run("task", lambda: refused, lambda p: bridge._apply_text(p, "Selam"))
        self.assertFalse([e for e in bridge.feed if "telemetry" in e])
        self.assertFalse(bridge.busy)

    def test_a_queued_message_records_how_long_it_waited(self) -> None:
        bridge = self.bridge(proposal(kind="chat", reply=REPLY))
        store = bridge._conversation_db()
        cid = store.create_conversation().id
        self.assertTrue(bridge.openConversation(cid))
        bridge._clock = lambda: 7.5
        bridge._start_queued({"id": "q-1", "cid": cid, "text": "Selam", "groups": (), "attachments": (),
                              "mode": "together", "route": bridge._provider_route(), "claude": None, "queued_at": 5.0})
        self.assertEqual(REPLY, bridge.feed[-1]["text"])
        self.assertEqual(2500, bridge._chat_usage["timing"]["queued_ms"])
        # Only a record that has a timing gets the wait; anything else passes unchanged.
        for payload in ({"intent": {"usage": None}}, {"command": "worker", "error": {}}, None):
            self.assertEqual(json.dumps(payload), json.dumps(_waited(payload, 10)))


class CutShortTimingTests(SurfaceCase):
    clocked = ui.ResponseTelemetryTests.clocked
    line = ui.ResponseTelemetryTests.line

    def test_a_reply_too_long_to_keep_whole_keeps_its_timing(self) -> None:
        """DFREPLY: the reply kept up to its ceiling, marked cut short, still says how long it took."""
        long_reply = "a" * (CHAT_REPLY_MAX_CHARS + 50)
        self.model = ReportingModel(usage={"output_tokens": 9000}, previews=("aaa",))
        bridge = self.clocked(self.bridge(proposal(kind="chat", reply=long_reply)), 0.0, 2.0, 61.5)
        root = self.window(bridge)
        bridge.submitText("Uzun yaz")
        pump(400)
        reply = next(e for e in bridge.feed if e.get("cut"))
        self.assertEqual({"elapsed_ms": 61500, "first_ms": 2000, "tokens": 9000}, reply["telemetry"])
        self.assertEqual(["First text 2.0 s · total 1 min 2 s · 9.0K tokens"], self.line(root))
        from jarvis.ui.i18n import localization
        self.addCleanup(lambda: localization(ui._APP).apply("en-US"))
        bridge.setLanguage("tr-TR")
        pump(300)
        self.assertEqual(["İlk yanıt 2,0 sn · toplam 1 dk 2 sn · 9,0K token"], self.line(root))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
