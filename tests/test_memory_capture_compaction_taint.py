"""SECCAPFIX F4: a compacted digest keeps the capture taint of the turn it replaced."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jarvis.contracts import MemoryRecordOrigin
from jarvis.conversation import Compacted, ConversationState, append_turn, state_from_dict
from jarvis.core import _capture_session_taint
from jarvis.memory_guard import SessionTaint
from tests.helpers import safe_cleanup
from tests import test_memory_capture_guard as guard
from tests.test_memory_capture_guard import BUILD, DEPLOY, FACT

SESSION_END = MemoryRecordOrigin.CAPTURE_SESSION_END
QUOTE = [{"type": "quote", "group": "q1", "sha256": ""}]


def digest(kind, refs, capability=""):
    return Compacted(seq=1, role="user" if kind == "user" else "assistant", kind=kind,
                     digest="d", capability=capability, refs=refs)


class CompactedRefsTests(unittest.TestCase):
    def test_compaction_keeps_refs_and_round_trips(self) -> None:
        state, _ = append_turn(ConversationState(), role="user", kind="user", text="see this",
                               refs=QUOTE, key="a")
        state, _ = append_turn(state, role="user", kind="user", text="plain", key="b",
                               raw_turns_max=0)
        self.assertEqual([c.refs for c in state.compacted], [tuple(QUOTE), ()])
        self.assertEqual(state_from_dict(state.to_dict()).compacted, state.compacted)

    def test_old_digest_without_refs_stays_unknown_when_rewritten(self) -> None:
        old = {"turns": [], "compacted": [{"seq": 1, "role": "user", "kind": "user",
                                           "digest": "d", "capability": ""}],
               "omitted": 0, "pins": [], "keys": [], "next_seq": 2, "titles": []}
        state = state_from_dict(old)
        self.assertIsNone(state.compacted[0].refs)
        self.assertNotIn("refs", state.to_dict()["compacted"][0])


class CompactedTaintTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, temp)
        self.root = Path(temp.name)
        (self.root / "notes.txt").write_text("x", encoding="utf-8")

    def taint(self, *entries):
        return _capture_session_taint(entries, SESSION_END, (self.root,))

    def test_digest_taint_follows_its_refs(self) -> None:
        inside = [{"type": "file", "path": str(self.root / "notes.txt")}]
        outside = [{"type": "file", "path": "C:\\Windows\\win.ini"}]
        for refs, expected in (
            ((), SessionTaint.USER_TYPED),
            (QUOTE, SessionTaint.IMPORT),
            (inside, SessionTaint.LOCAL_TRUSTED_FILE),
            (outside, SessionTaint.IMPORT),
        ):
            with self.subTest(refs=refs):
                self.assertIs(self.taint(digest("user", tuple(refs))), expected)

    def test_old_digest_without_marker_is_import(self) -> None:
        for kind in ("user", "reply"):
            with self.subTest(kind=kind):
                self.assertIs(self.taint(digest(kind, None)), SessionTaint.IMPORT)

    def test_result_digest_keeps_its_capability_taint(self) -> None:
        self.assertIs(self.taint(digest("result", (), "web_search")), SessionTaint.WEB)


class CompactedCaptureTests(unittest.TestCase):
    """End to end: a quoted turn compacted away still taints the capture."""

    # The guard module's fixture, borrowed (not inherited, so its tests run once).
    _base = guard.CaptureGuardTests
    setUp, open, say, model, capture, records = (
        _base.setUp, _base.open, _base.say, _base.model, _base.capture, _base.records,
    )

    def test_quote_ref_import_taint_survives_compaction(self) -> None:
        def change(state):
            state, _ = append_turn(state, role="user", kind="user", text="look at this",
                                   message="look at this", refs=QUOTE, key="quote")
            state, _ = append_turn(state, role="user", kind="user", text=DEPLOY,
                                   message=DEPLOY, key="d2")
            state, turn = append_turn(state, role="user", kind="user", text=BUILD,
                                      message=BUILD, key="b2", raw_turns_max=2)
            self.turn_ids = [None, turn.id]
            return state

        self.store.update_conversation_state(self.conversation, change)
        state = self.store.load_conversation_state(self.conversation)
        self.assertIn(tuple(QUOTE), [c.refs for c in state.compacted])
        result = self.capture(self.model({}))
        self.assertEqual((result.approved, result.pending), (0, 1))
        self.assertEqual(self.records(), [("rule", FACT, "pending", "user_stated", "import")])

    def test_clean_compaction_still_approves(self) -> None:
        def change(state):
            state, _ = append_turn(state, role="user", kind="user", text=DEPLOY,
                                   message=DEPLOY, key="d2")
            state, turn = append_turn(state, role="user", kind="user", text=BUILD,
                                      message=BUILD, key="b2", raw_turns_max=2)
            self.turn_ids = [None, turn.id]
            return state

        self.store.update_conversation_state(self.conversation, change)
        self.assertTrue(self.store.load_conversation_state(self.conversation).compacted)
        result = self.capture(self.model({}))
        self.assertEqual((result.approved, result.pending), (1, 0))


if __name__ == "__main__":
    unittest.main()
