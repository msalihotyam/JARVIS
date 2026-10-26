import unittest
from jarvis.workers.base import WorkerResult, AgentProfile, Worker, effective_authority

class TestWorkersBase(unittest.TestCase):
    def test_worker_result_invalid_status(self):
        with self.assertRaises(ValueError):
            WorkerResult(status="invalid", summary="", changed_files=(), evidence=())

    def test_worker_result_valid_status(self):
        WorkerResult(status="completed", summary="", changed_files=(), evidence=())

    def test_worker_instantiation(self):
        with self.assertRaises(TypeError):
            Worker()

    def test_effective_authority_empty(self):
        with self.assertRaises(ValueError):
            effective_authority()

    def test_effective_authority_two_profiles(self):
        p1 = AgentProfile(name="p1", tools=frozenset({"a", "b"}), paths=frozenset({"p1"}), network=True, max_turns=10, budget_sessions=5)
        p2 = AgentProfile(name="p2", tools=frozenset({"b", "c"}), paths=frozenset({"p1", "p2"}), network=False, max_turns=5, budget_sessions=10)

        result = effective_authority(p1, p2)

        self.assertEqual(result.name, "effective")
        self.assertEqual(result.tools, frozenset({"b"}))
        self.assertEqual(result.paths, frozenset({"p1"}))
        self.assertFalse(result.network)
        self.assertEqual(result.max_turns, 5)
        self.assertEqual(result.budget_sessions, 5)

    def test_effective_authority_three_profiles(self):
        p1 = AgentProfile(name="p1", tools=frozenset({"a", "b", "c"}), paths=frozenset({"p1", "p2", "p3"}), network=True, max_turns=10, budget_sessions=5)
        p2 = AgentProfile(name="p2", tools=frozenset({"b", "c"}), paths=frozenset({"p1", "p2"}), network=True, max_turns=5, budget_sessions=10)
        p3 = AgentProfile(name="p3", tools=frozenset({"c", "d"}), paths=frozenset({"p2", "p3"}), network=True, max_turns=8, budget_sessions=3)

        result = effective_authority(p1, p2, p3)

        self.assertEqual(result.name, "effective")
        self.assertEqual(result.tools, frozenset({"c"}))
        self.assertEqual(result.paths, frozenset({"p2"}))
        self.assertTrue(result.network)
        self.assertEqual(result.max_turns, 5)
        self.assertEqual(result.budget_sessions, 3)

    def test_effective_authority_never_exceeds(self):
        profiles = [
            AgentProfile(name="p1", tools=frozenset({"a", "b"}), paths=frozenset({"p1"}), network=True, max_turns=10, budget_sessions=5),
            AgentProfile(name="p2", tools=frozenset({"b", "c"}), paths=frozenset({"p1", "p2"}), network=False, max_turns=5, budget_sessions=10),
            AgentProfile(name="p3", tools=frozenset({"a", "b", "c"}), paths=frozenset({"p1", "p2", "p3"}), network=True, max_turns=8, budget_sessions=3)
        ]

        import itertools
        for r in range(1, 4):
            for combo in itertools.combinations(profiles, r):
                result = effective_authority(*combo)
                for p in combo:
                    self.assertTrue(result.tools.issubset(p.tools))
                    self.assertTrue(result.paths.issubset(p.paths))
                    self.assertTrue(result.network <= p.network)
                    self.assertLessEqual(result.max_turns, p.max_turns)
                    self.assertLessEqual(result.budget_sessions, p.budget_sessions)

if __name__ == '__main__':
    unittest.main()
