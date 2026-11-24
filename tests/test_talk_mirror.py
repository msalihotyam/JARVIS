import ast
import importlib.util
from pathlib import Path
import unittest


def entry(**overrides):
    return {
        "id": 7, "source": "pc", "who": "salih", "kind": "text",
        "text": "Merhaba", "needs_you": False, "delivered": {},
        **overrides,
    }


class TalkMirrorTests(unittest.TestCase):
    def plan(self, **overrides):
        from jarvis.talk_mirror import plan_mirror

        return plan_mirror(entry(**overrides))

    def test_planner_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("jarvis.talk_mirror"))

    def test_salih_pc_is_labelled_and_silent(self):
        self.assertEqual(self.plan(), [{
            "entry_id": 7, "view": "telegram", "kind": "text",
            "text": "Sen (PC): Merhaba", "silent": True,
        }])

    def test_salih_telegram_gets_phone_label_on_pc(self):
        action, = self.plan(source="telegram")
        self.assertEqual((action["view"], action["text"]), ("pc", "Telefon: Merhaba"))

    def test_jarvis_routes_to_both_views_and_only_alerts_when_needed(self):
        for needs_you in (False, True):
            with self.subTest(needs_you=needs_you):
                actions = self.plan(who="jarvis", needs_you=needs_you)
                self.assertEqual([a["view"] for a in actions], ["pc", "telegram"])
                self.assertEqual([a["silent"] for a in actions], [False, not needs_you])

    def test_long_text_splits_at_paragraph_boundary(self):
        first, second = "a" * 2500, "b" * 1500
        actions = self.plan(who="jarvis", text=first + "\n\n" + second,
                            delivered={"pc": True})
        self.assertEqual([a["text"] for a in actions],
                         ["(1/2) " + first + "\n\n", "(2/2) " + second])

    def test_long_word_splits_and_every_part_keeps_pc_label(self):
        word = "x" * 4001
        actions = self.plan(text=word)
        self.assertEqual(len(actions), 2)
        segments = []
        for index, action in enumerate(actions, 1):
            prefix = f"Sen (PC): ({index}/2) "
            self.assertTrue(action["text"].startswith(prefix))
            self.assertLessEqual(len(action["text"]), 4000)
            segments.append(action["text"][len(prefix):])
        self.assertEqual("".join(segments), word)

    def test_voice_reply_audio_stays_on_request_view(self):
        from jarvis.talk_mirror import plan_mirror

        for source in ("pc", "telegram"):
            with self.subTest(source=source):
                actions = plan_mirror(entry(who="jarvis", source=source, kind="voice"),
                                      audio="reply-ref")
                by_view = {action["view"]: action for action in actions}
                other = "telegram" if source == "pc" else "pc"
                self.assertEqual(by_view[source]["kind"], "voice")
                self.assertEqual(by_view[source]["audio"], "reply-ref")
                self.assertEqual(by_view[other]["kind"], "text")
                self.assertEqual(by_view[other]["text"], "Merhaba")
                self.assertNotIn("audio", by_view[other])

    def test_delivered_views_are_skipped(self):
        self.assertEqual([a["view"] for a in self.plan(
            who="jarvis", delivered={"pc": False, "telegram": True},
        )], ["pc"])
        self.assertEqual(self.plan(who="jarvis", delivered={"pc": True, "telegram": True}), [])

    def test_unknown_source_or_kind_raises(self):
        for field in ("source", "kind"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.plan(**{field: "unknown"})

    def test_needs_you_is_reserved_for_jarvis(self):
        for who in ("salih", "system"):
            with self.subTest(who=who), self.assertRaises(ValueError):
                self.plan(who=who, needs_you=True)

    def test_planner_has_no_imports(self):
        source = Path(__file__).resolve().parents[1] / "jarvis" / "talk_mirror.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom))
                             for node in ast.walk(tree)))
