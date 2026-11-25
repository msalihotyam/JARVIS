"""E1ROUTE1A: optional fast model/effort per message type for the understanding turn.

Every value here is fake; no process, network, model or auth call. Routing is
off unless both operator settings are set; the off fixtures were recorded from
main (a910ffb) by running ``RouteOffTests`` there.
"""

from __future__ import annotations

import itertools
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jarvis.app as app_module
from jarvis import router_tr
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.intent import ClaudeIntentAdapter
from jarvis.settings import ConfigError, Settings, _document_from_settings, _settings_from_document
from tests.test_intent import IntentTestCase, ScriptedModel, proposal, stream_line
from tests.test_speedwarm import MARKER, WarmTest

ROOT = Path(__file__).resolve().parents[1]
REPLY = "Merhaba."
LOGGED_IN = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}
FAST = {"claude_fast_model": "fake-fast", "claude_fast_effort": "low"}

#: Recorded from main (a910ffb): what the model saw during each call, and the timing record.
MAIN_SEEN = [("base-model", "high", "<conversation>", False)] * 2
MAIN_TIMING = [{"total_ms": 2000, "parse_ms": 500, "output_chars": 37, "lane": "current", "model": "base-model",
                "effort": "high", "t_route": 500, "t_turn_done": 2000}] * 2
#: Recorded from main (a910ffb): the real adapter's argv (the system prompt text, which carries
#: the catalog, is replaced by a placeholder; this slice does not touch it).
MAIN_ARGV = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--permission-mode", "plan", "--safe-mode", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}', "--tools", "", "--disable-slash-commands", "--no-chrome",
             "--no-session-persistence", "--max-turns", "3", "--max-budget-usd", "0.2500",
             "--system-prompt", "<prompt>", "--model", "sonnet", "--effort", "medium"]


def light(text):
    return router_tr.Route("L1", "chat", 0.6, "test")


def heavy(text):
    return router_tr.Route("L2", "task", 0.8, "test")


class RecordingModel(ScriptedModel):
    """A warm-capable model with a default model/effort; records what each call saw."""

    def __init__(self, *answers, **kwargs):
        super().__init__(*answers, **kwargs)
        self.model, self.effort = "base-model", "high"
        self.warm_lane, self.warm_resend, self.seen = None, False, []
        self.on_complete = lambda task: self.seen.append((self.model, self.effort, self.warm_lane, self.warm_resend))


def tick_clock():
    return patch.object(app_module, "time", SimpleNamespace(monotonic=itertools.count(0.0, 0.5).__next__))


class RouteOffTests(IntentTestCase):
    """Acceptance 2: both settings unset = the turn is exactly main's."""

    def record(self):
        conversation = self.memory.create_conversation()
        model = RecordingModel(proposal(kind="chat", reply=REPLY), proposal(kind="chat", reply=REPLY))
        app = self.build(model)
        with tick_clock():
            first = app.submit_text("Selam", conversation_id=conversation.id).payload
            second = app.submit_text("Bir dosya oluştur", conversation_id=conversation.id).payload
        seen = [(m, e, None if lane is None else lane.replace(conversation.id, "<conversation>"), r)
                for m, e, lane, r in model.seen]
        after = (model.model, model.effort, model.warm_lane, model.warm_resend)
        return seen, after, [first["intent"]["usage"]["timing"], second["intent"]["usage"]["timing"]]

    def test_off_turn_matches_main(self) -> None:
        seen, after, timing = self.record()
        self.assertEqual((MAIN_SEEN, MAIN_TIMING), (seen, timing), json.dumps([seen, timing]))
        self.assertEqual(("base-model", "high", None, False), after)

    def test_off_real_adapter_argv_matches_main(self) -> None:
        adapter = ClaudeIntentAdapter(allowed_roots=(self.root,), default_working_directory=self.root,
                                      model="sonnet", effort="medium")
        answer = json.dumps({"kind": "chat", "reply": REPLY})
        spawned = []

        def popen(argv, *args, **kwargs):
            argv = [str(a) for a in argv]
            argv[argv.index("--system-prompt") + 1] = "<prompt>"
            spawned.append((argv, adapter.warm_lane))
            return FakeCliProcess([stream_line(answer), json.dumps(
                {"type": "result", "result": answer, "usage": {"output_tokens": 3}}) + "\n"])

        app = self.build(adapter)
        conversation = self.memory.create_conversation()
        with patch.object(ClaudeCodeConnector, "_auth_status", return_value=LOGGED_IN), \
                patch.object(ClaudeCodeConnector, "prohibited_billing_env_vars", return_value=()), \
                patch.object(ClaudeCodeConnector, "_argv0", return_value="claude"), \
                patch("jarvis.connectors.claude_code.subprocess.Popen", side_effect=popen), \
                patch("time.monotonic", side_effect=itertools.count(100.0, 0.5).__next__):
            app.submit_text("Selam", conversation_id=conversation.id, on_reply_preview=[].append)
        (argv, lane), = spawned
        self.assertEqual(conversation.id, lane)
        self.assertEqual(MAIN_ARGV, argv, json.dumps(argv))
        self.assertEqual(("sonnet", "medium", None), (adapter.model, adapter.effort, adapter.warm_lane))


class FakeCliProcess:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stderr = iter(())
        self.stdin = None
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def communicate(self, *args, **kwargs):
        return "", ""


class RouteOnTests(IntentTestCase):
    """Acceptance 3 (fake setting values, tests only)."""

    def build_on(self, model):
        return self.build(model, settings=Settings(memory_database=self.database, **FAST))

    def test_alternating_types_keep_two_lanes_resend_on_switch_and_record_the_model_used(self) -> None:
        conversation = self.memory.create_conversation()
        texts = {"a": light, "b": heavy}
        model = RecordingModel(*[proposal(kind="chat", reply=REPLY)] * 4)
        # A fresh app per turn, as the UI backend and CLI do; only the adapter is shared.
        with tick_clock(), patch.dict(app_module._ROUTE_LAST, clear=True), \
                patch.object(router_tr, "route", side_effect=lambda t: texts[t](t)):
            timings = [self.build_on(model).submit_text(t, conversation_id=conversation.id)
                       .payload["intent"]["usage"]["timing"] for t in ("a", "b", "a", "a")]
        fast, base = f"{conversation.id}:fake-fast:low", f"{conversation.id}:base-model:high"
        self.assertEqual([("fake-fast", "low", fast, False), ("base-model", "high", base, True),
                          ("fake-fast", "low", fast, True), ("fake-fast", "low", fast, False)], model.seen)
        self.assertEqual([("fake-fast", "low"), ("base-model", "high")] + [("fake-fast", "low")] * 2,
                         [(t["model"], t["effort"]) for t in timings])
        self.assertEqual(("base-model", "high", None, False),
                         (model.model, model.effort, model.warm_lane, model.warm_resend))

    def test_unsure_unknown_and_router_error_take_the_default(self) -> None:
        settings = Settings(memory_database=self.database, **FAST)
        model = RecordingModel()
        unsure = router_tr.Route("L1", "chat", 0.5, "test")
        with patch.object(router_tr, "route", return_value=unsure):
            self.assertEqual(("base-model", "high"), app_module._route_choice(settings, "x", model))
        # Real router: unrecognised content is L2 at 0.5.
        self.assertEqual(("base-model", "high"), app_module._route_choice(settings, "qzx vbnm", model))
        with patch.object(router_tr, "route", side_effect=RuntimeError("boom")):
            self.assertEqual(("base-model", "high"), app_module._route_choice(settings, "x", model))
        with patch.object(router_tr, "route", side_effect=light):
            self.assertEqual(("fake-fast", "low"), app_module._route_choice(settings, "x", model))

    def test_router_error_never_blocks_the_turn(self) -> None:
        conversation = self.memory.create_conversation()
        model = RecordingModel(proposal(kind="chat", reply=REPLY))
        with patch.object(router_tr, "route", side_effect=RuntimeError("boom")):
            payload = self.build_on(model).submit_text("Selam", conversation_id=conversation.id).payload
        self.assertEqual("conversation", payload["intent"]["status"])
        self.assertEqual([("base-model", "high", f"{conversation.id}:base-model:high", False)], model.seen)

    def test_off_or_no_model_attrs_is_no_route(self) -> None:
        self.assertIsNone(app_module._route_choice(self.settings, "Selam", RecordingModel()))
        self.assertIsNone(app_module._route_choice(Settings(memory_database=self.database, **FAST),
                                                   "Selam", ScriptedModel()))

    def test_billing_estimate_is_the_same_for_either_model(self) -> None:
        adapter = ClaudeIntentAdapter(allowed_roots=(self.root,), default_working_directory=self.root,
                                      model="sonnet", effort="high")
        task = RouteWarmTests.task(None)
        with patch.object(ClaudeCodeConnector, "_auth_status", return_value=LOGGED_IN), \
                patch.object(ClaudeCodeConnector, "prohibited_billing_env_vars", return_value=()):
            default = adapter.estimate_cost(task)
            adapter.model, adapter.effort = "fake-fast", "low"
            fast = adapter.estimate_cost(task)
        self.assertEqual(default, fast)


class RouteWarmTests(WarmTest):
    """Acceptance 3 on the real adapter: two warm lanes, no respawn per message,
    and a switched-back lane gets the window it missed (intent.py ``warm_resend``)."""

    def lane_turn(self, model, effort, resend):
        self.adapter.model, self.adapter.effort = model, effort
        self.adapter.warm_lane, self.adapter.warm_resend = f"c1:{model}:{effort}", resend
        return self.turn()

    def test_two_lanes_no_respawn_and_resend_on_switch(self) -> None:
        for args in (("fake-fast", "low", False), ("base-model", "high", True),
                     ("fake-fast", "low", True), ("fake-fast", "low", False)):
            self.assertIsNone(self.lane_turn(*args).error)
        self.assertEqual(2, self.popen.call_count)
        self.assertEqual({"c1:fake-fast:low", "c1:base-model:high"}, set(self.adapter._warm))
        fast, base = self.processes
        self.assertEqual("fake-fast", fast.args[fast.args.index("--model") + 1])
        self.assertEqual("base-model", base.args[base.args.index("--model") + 1])
        self.assertEqual([True, True, False], [MARKER in line for line in fast.user_lines()])
        self.assertEqual([True], [MARKER in line for line in base.user_lines()])


class RouteSettingsTests(unittest.TestCase):
    """Acceptance 1 and negatives 3-5."""

    def test_default_is_off(self) -> None:
        settings = Settings()
        self.assertEqual((None, None), (settings.claude_fast_model, settings.claude_fast_effort))
        self.assertFalse(set(FAST) & set(_document_from_settings(settings)))

    def test_one_alone_is_refused(self) -> None:
        for key in FAST:
            with self.assertRaises(ValueError):
                Settings(**{key: FAST[key]})
            document = _document_from_settings(Settings())
            document[key] = FAST[key]
            with self.assertRaises(ConfigError):
                _settings_from_document(document)

    def test_bad_values_are_refused_by_the_existing_validators(self) -> None:
        for model in (" fake", "fake fast", "fa\x07st", "fa\nst", "a" * 65, "", 7):
            with self.assertRaises(ValueError):
                Settings(claude_fast_model=model, claude_fast_effort="low")
        for effort in ("low ", "LOW", "fast", "lo\x00w", ""):
            with self.assertRaises(ValueError):
                Settings(claude_fast_model="fake-fast", claude_fast_effort=effort)

    def test_the_pair_round_trips(self) -> None:
        document = _document_from_settings(Settings(**FAST))
        self.assertEqual(FAST, {k: document[k] for k in FAST})
        loaded = _settings_from_document(document)
        self.assertEqual(("fake-fast", "low"), (loaded.claude_fast_model, loaded.claude_fast_effort))

    def test_no_code_path_writes_the_settings(self) -> None:
        """Negative 5: outside settings.py nothing assigns or passes either key."""
        writes = re.compile(r"claude_fast_(model|effort)\s*=(?!=)|[\"']claude_fast_(model|effort)[\"']\s*\]\s*=(?!=)")
        found = []
        for folder in ("jarvis", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                if path == ROOT / "jarvis" / "settings.py":
                    continue
                if writes.search(path.read_text(encoding="utf-8", errors="replace")):
                    found.append(str(path.relative_to(ROOT)))
        self.assertEqual([], found)


if __name__ == "__main__":
    unittest.main()
