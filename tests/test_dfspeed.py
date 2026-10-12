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
                          "parse_ms", "output_chars"}, set(timing))
        self.assertTrue(all(type(v) is int and v > 0 for v in timing.values()), timing)
        self.assertLess(timing["auth_ms"], timing["spawn_ms"])                       # the auth check precedes the spawn
        self.assertEqual(timing["first_visible_ms"], timing["spawn_ms"] + timing["first_text_ms"])
        self.assertLess(timing["first_visible_ms"] + timing["text_ms"] + timing["parse_ms"], timing["total_ms"])
        self.assertEqual(12, payload["intent"]["usage"]["reported"]["output_tokens"])   # tokens: as the CLI reported them

    def test_exact_numbers_on_a_controlled_clock(self) -> None:
        marks = {"started": 10.2, "auth_started": 10.5, "auth_ended": 11.3, "spawned": 11.5,
                 "first_text": 13.0, "last_text": 20.0, "exited": 20.5, "ended": 20.6, "output_chars": 1234}
        app = self.build(MarkedModel(marks, proposal(kind="chat", reply=REPLY)))
        with app_clock(10.0, 21.0, 21.25, 21.5):            # start, model returned, validated, answered
            payload = app.submit_text("Selam").payload
        self.assertEqual({"total_ms": 11500, "auth_ms": 800, "spawn_ms": 1500, "first_text_ms": 1500,
                          "first_visible_ms": 3000, "text_ms": 7000, "parse_ms": 250, "output_chars": 1234},
                         payload["intent"]["usage"]["timing"])

    def test_a_model_without_marks_still_gets_its_total_and_answer_size(self) -> None:
        app = self.build(ScriptedModel(proposal(kind="chat", reply=REPLY)))
        with app_clock(0.0, 4.0, 4.1, 4.2):
            payload = app.submit_text("Selam").payload
        answer = proposal(kind="chat", reply=REPLY)
        self.assertEqual({"total_ms": 4200, "parse_ms": 100, "output_chars": len(answer)}, payload["intent"]["usage"]["timing"])

    def test_refused_and_failed_turns_never_crash_and_record_what_happened(self) -> None:
        # Refused before any model is asked: no usage record at all, exactly as before.
        app = self.build(ScriptedModel(available=False))
        payload = app.submit_text("Selam").payload
        self.assertEqual(("model_unavailable", None), (payload["intent"]["status"], payload["intent"]["usage"]))
        payload = self.build(ScriptedModel()).submit_text("bad\x00").payload
        self.assertEqual(("invalid_input", None), (payload["intent"]["status"], payload["intent"]["usage"]))
        # The model failed: its total and how long the failure took to judge.
        failing = ScriptedModel(error=StructuredError(code=ErrorCode.CONNECTOR_FAILED, message="boom"))
        with app_clock(0.0, 2.0, 2.5):
            payload = self.build(failing).submit_text("Selam").payload
        self.assertEqual("model_failed", payload["intent"]["status"])
        self.assertEqual({"total_ms": 2500, "parse_ms": 500}, payload["intent"]["usage"]["timing"])
        # The adapter raised: the total alone - no phase is invented.
        raising = ScriptedModel()
        raising.on_complete = lambda task: (_ for _ in ()).throw(RuntimeError("x"))
        with app_clock(0.0, 3.0):
            payload = self.build(raising).submit_text("Selam").payload
        self.assertEqual(("model_failed", {"total_ms": 3000}), (payload["intent"]["status"], payload["intent"]["usage"]["timing"]))


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
