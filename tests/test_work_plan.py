import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest

from jarvis.work_plan import GrantEnvelope, Plan, PlanSchemaError


def example():
    return {
        "goal": "Build a project", "project_root": "projects/demo",
        "slices": [{"slice_id": "s1", "goal": "Implement", "targets": ["a.py"],
                    "check_ids": ["unit"]}],
        "targets": ["a.py"], "connector_classes": ["local"],
        "operation_classes": ["stage_write", "check"],
        "checks": [{"check_id": "unit", "argv": ["python", "-m", "unittest"],
                    "timeout_seconds": 60, "output_cap_bytes": 4096,
                    "fixture_hashes": ["a" * 64]}],
        "budgets": {"worker_sessions": 10, "revisions_per_slice": 5,
                    "failed_slices": 3, "wall_clock_seconds": 7200,
                    "model_calls": 20},
        "child_count": 1, "expires_at": 1800000000, "new_dependencies": [],
    }


class PlanTests(unittest.TestCase):
    def test_round_trip_and_hash_definition(self):
        plan = Plan.from_dict(example())
        self.assertEqual(plan, Plan.from_json(plan.canonical_json()))
        self.assertEqual(example(), plan.to_dict())
        expected = json.dumps(example(), sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"))
        self.assertEqual(expected, plan.canonical_json())
        self.assertEqual(hashlib.sha256(expected.encode("utf-8")).hexdigest(),
                         plan.plan_hash)

    def test_recursive_field_reordering(self):
        def reverse(value):
            if isinstance(value, dict):
                return {k: reverse(v) for k, v in reversed(list(value.items()))}
            if isinstance(value, list):
                return [reverse(v) for v in value]
            return value
        self.assertEqual(Plan.from_dict(example()).plan_hash,
                         Plan.from_json(json.dumps(reverse(example()))).plan_hash)

    def test_every_leaf_is_hash_bound(self):
        original = example()
        def leaves(value, path=()):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield from leaves(item, path + (key,))
            elif isinstance(value, list) and value:
                for index, item in enumerate(value):
                    yield from leaves(item, path + (index,))
            else:
                yield path, value
        initial = Plan.from_dict(original).plan_hash
        for path, value in leaves(original):
            with self.subTest(path=path):
                changed = copy.deepcopy(original)
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = (value + 1 if type(value) is int else
                                    value + "x" if type(value) is str else ["pkg"])
                self.assertNotEqual(initial, Plan.from_dict(changed).plan_hash)

    def test_unknown_and_missing_fields_at_each_object_level(self):
        for path in ((), ("slices", 0), ("checks", 0), ("budgets",)):
            for unknown in (True, False):
                with self.subTest(path=path, unknown=unknown):
                    data = example()
                    target = data
                    for key in path:
                        target = target[key]
                    if unknown:
                        target["model_size_class"] = "small"
                    else:
                        target.pop(next(iter(target)))
                    with self.assertRaises(PlanSchemaError):
                        Plan.from_dict(data)

    def test_duplicate_json_keys_refused(self):
        encoded = json.dumps(example())
        for value in (encoded.replace('"goal":', '"goal": "other", "goal":', 1),
                      encoded.replace('"worker_sessions":',
                                      '"worker_sessions": 1, "worker_sessions":')):
            with self.subTest(value=value), self.assertRaises(PlanSchemaError):
                Plan.from_json(value)

    def test_invalid_shapes_and_numeric_types(self):
        for key, value in (("child_count", True), ("expires_at", 1.5),
                           ("child_count", -1), ("goal", " "),
                           ("targets", "a.py"), ("targets", [None]),
                           ("checks", [None]), ("budgets", []),
                           ("slices", {}), ("goal", "\ud800")):
            with self.subTest(key=key, value=repr(value)):
                data = example()
                data[key] = value
                with self.assertRaises(PlanSchemaError):
                    Plan.from_dict(data)

    def test_invalid_json(self):
        for value in ("{", "[]", "null", "true", 42):
            with self.subTest(value=value), self.assertRaises(PlanSchemaError):
                Plan.from_json(value)

    def test_input_output_and_envelope_immutable(self):
        source = example()
        plan = Plan.from_dict(source)
        initial = plan.plan_hash
        source["checks"][0]["argv"].append("changed")
        plan.to_dict()["targets"].append("changed")
        envelope = plan.grant_envelope()
        envelope.to_dict()["plan"]["targets"].append("changed")
        self.assertEqual(initial, plan.plan_hash)
        self.assertEqual(initial, envelope.plan_hash)
        self.assertEqual(plan.to_dict(), envelope.to_dict()["plan"])
        with self.assertRaises(FrozenInstanceError):
            plan.goal = "changed"
        with self.assertRaises(FrozenInstanceError):
            plan.budgets.model_calls = 100
        with self.assertRaises(PlanSchemaError):
            GrantEnvelope("0" * 64, plan)

    def test_array_order_and_exact_text_preserved(self):
        data = example()
        plan = Plan.from_dict(data)
        data["operation_classes"].reverse()
        self.assertNotEqual(plan.plan_hash, Plan.from_dict(data).plan_hash)
        data["goal"] = "Proje: İstanbul"
        self.assertIn("İstanbul", Plan.from_dict(data).canonical_json())

    def test_schema_does_not_make_policy_decisions(self):
        data = example()
        data["targets"] = ["../outside"]
        data["child_count"] = 6
        plan = Plan.from_dict(data)
        self.assertEqual(("../outside",), plan.grant_envelope().plan.targets)
        self.assertEqual(6, plan.child_count)


if __name__ == "__main__":
    unittest.main()
