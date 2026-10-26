"""Decision table for the pure worker router."""

import unittest

from jarvis.workers.route import route


def facts(**changes):
    result = dict(tier="T1", **{"class": "B"}, files=["jarvis/example.py"],
                  veto=[], linux_ok=True, new_project=False, worker="auto",
                  usage=80, headroom={"codex": 50, "jules": 5},
                  admitted={"codex": True, "jules": True}, subscription_only=True)
    result.update(changes)
    return result


class TestWorkersRoute(unittest.TestCase):
    def test_decision_table(self):
        rows = [
            ({}, ("codex", "more_headroom")),
            ({"tier": "T0", "class": "C"}, ("codex", "more_headroom")),
            ({"tier": "T2"}, ("claude", "tier")),
            ({"class": "A"}, ("claude", "class")),
            ({"files": []}, ("claude", "no_files")),
            ({"veto": ["JARVIS/example.py"]}, ("claude", "veto")),
            ({"usage": 50}, ("claude", "claude_headroom")),
            ({"usage": 0}, ("claude", "claude_headroom")),
            ({"usage": 100}, ("codex", "more_headroom")),
            ({"worker": "claude"}, ("claude", "explicit")),
            ({"worker": "codex", "usage": 0}, ("codex", "explicit")),
            ({"worker": "codex", "tier": "T2"}, ("claude", "tier")),
            ({"worker": "codex", "class": "A"}, ("claude", "class")),
            ({"worker": "jules"}, ("jules", "explicit")),
            ({"worker": "jules", "linux_ok": False}, ("claude", "linux_required")),
            ({"worker": "jules", "new_project": True}, ("claude", "new_project")),
            ({"new_project": True}, ("codex", "more_headroom")),
            ({"linux_ok": False}, ("codex", "more_headroom")),
            ({"subscription_only": False}, ("claude", "subscription_required")),
            ({"admitted": {"codex": False, "jules": True}},
             ("claude", "admission_refused")),
            ({"worker": "jules", "admitted": {"codex": True, "jules": False}},
             ("claude", "admission_refused")),
            ({"headroom": {"codex": 9, "jules": 5}}, ("claude", "low_headroom")),
            ({"usage": 95, "headroom": {"codex": 10, "jules": 5}},
             ("codex", "more_headroom")),
            ({"worker": "jules", "headroom": {"codex": 50, "jules": 0}},
             ("claude", "low_headroom")),
        ]
        for changes, expected in rows:
            with self.subTest(changes=changes):
                self.assertEqual(route(facts(**changes)), expected)

    def test_protected_path_table(self):
        paths = ["", " ", ".", "..", "../x", "/tmp/x", "C:\\x", "*.py",
                 "a?b", "a[b", "SCRIPTS/x", "scripts./x", "a /x", "a~1/x",
                 ".claude/x", ".github/x", "data/x", "a/AGENTS.md", "CLAUDE.md",
                 "uv.lock", ".env.test", "dev/dashboard/x", "a/.jarvis/x",
                 "a.key", "a.pem", "a.p12", "a.pfx", "a.crt", "a.cer",
                 "a.db", "a.sqlite", "a.sqlite3", "jarvis/../scripts/x"]
        for path in paths:
            for worker in ("auto", "codex", "jules"):
                with self.subTest(path=path, worker=worker):
                    self.assertEqual(route(facts(files=[path], worker=worker)),
                                     ("claude", "veto"))

    def test_invalid_input_table(self):
        for value in (None, [], "UNKNOWN", True, 42):
            with self.subTest(value=value):
                self.assertEqual(route(value), ("claude", "invalid_input"))
        for key in facts():
            missing = facts()
            del missing[key]
            with self.subTest(missing=key):
                self.assertEqual(route(missing), ("claude", "invalid_input"))
        for changes in ({"worker": "paid"}, {"tier": []}, {"class": None},
                        {"linux_ok": 1}, {"new_project": "false"},
                        {"subscription_only": 1}, {"files": "x"}, {"files": [None]},
                        {"veto": None}, {"veto": [1]}, {"admitted": {}},
                        {"admitted": {"codex": 1, "jules": True}}, {"headroom": []}):
            with self.subTest(changes=changes):
                self.assertEqual(route(facts(**changes)), ("claude", "invalid_input"))
        for value in (None, "UNKNOWN", True, -1, float("nan"), float("inf"), 101):
            with self.subTest(usage=value):
                self.assertEqual(route(facts(usage=value)), ("claude", "usage_unknown"))
        for worker in ("codex", "jules"):
            for value in (None, "UNKNOWN", True, -1, float("nan"), float("inf")):
                h = {"codex": 50, "jules": 5}
                h[worker] = value
                with self.subTest(worker=worker, headroom=value):
                    self.assertEqual(route(facts(headroom=h)),
                                     ("claude", "headroom_unknown"))
        self.assertEqual(route(facts(headroom={})), ("claude", "headroom_unknown"))
        self.assertEqual(route(facts(headroom={"codex": 101, "jules": 5})),
                         ("claude", "headroom_unknown"))
