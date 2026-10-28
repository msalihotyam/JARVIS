import copy
import unittest

from jarvis.work_plan import Plan, classify
from tests.test_work_plan import example


def facts():
    return dict(target_folders=["projects/demo"], existing_repo=False,
                outgoing_actions=False, money_actions=False,
                manual_checks=False, jarvis_repo=False)


def plan(count=1):
    return dict(slice_count=count, new_dependencies=[], connector_classes=[],
                operation_classes=[])


class ClassifyTests(unittest.TestCase):
    def test_boundaries(self):
        for existing in (False, True):
            for count in (1, 5, 6, 15, 16):
                with self.subTest(existing=existing, count=count):
                    brief = facts()
                    brief["existing_repo"] = existing
                    expected = ("large" if count > 15 else "medium"
                                if existing or count > 5 else "small")
                    size, reasons = classify(brief, plan(count))
                    self.assertEqual(expected, size)
                    self.assertTrue(reasons)

    def test_each_forcing_fact_alone(self):
        for name in ("outgoing_actions", "money_actions", "manual_checks",
                     "jarvis_repo"):
            with self.subTest(name=name):
                brief = facts()
                brief[name] = True
                self.assertEqual(("large", [name]), classify(brief, plan()))

    def test_multiple_folders_are_medium(self):
        brief = facts()
        brief["target_folders"].append("projects/other")
        self.assertEqual("medium", classify(brief, plan())[0])

    def test_new_dependency_without_outgoing_install(self):
        value = plan()
        value["new_dependencies"] = ["local-package"]
        self.assertEqual("small", classify(facts(), value)[0])

    def test_missing_each_fact(self):
        for source in ("brief", "plan"):
            for key in (facts() if source == "brief" else plan()):
                with self.subTest(source=source, key=key):
                    brief, value = facts(), plan()
                    (brief if source == "brief" else value).pop(key)
                    self.assertEqual("large", classify(brief, value)[0])

    def test_unknown_or_invalid_facts(self):
        cases = [(key, v) for key in facts() if key != "target_folders"
                 for v in (None, "false", 0, 1)]
        cases += [("target_folders", v) for v in
                  (None, [], "folder", [None], [" "], ["a", "a"])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                brief = facts()
                brief[key] = value
                self.assertEqual("large", classify(brief, plan())[0])
        for key, values in (("slice_count", (None, True, 0, -1, 5.0, "5")),
                            ("new_dependencies", (None, "pkg", [None], [""])),
                            ("connector_classes", (None, "local", ["bogus"], [None])),
                            ("operation_classes", (None, "check", ["push"], [[]]))):
            for value in values:
                with self.subTest(key=key, value=value):
                    data = plan()
                    data[key] = value
                    self.assertEqual("large", classify(facts(), data)[0])
        for brief, value in ((None, plan()), ({}, {}), (facts(), None)):
            self.assertEqual("large", classify(brief, value)[0])

    def test_model_class_ignored(self):
        for count in (5, 6, 16):
            expected = classify(facts(), plan(count))
            for label in ("small", "medium", "large", None, object()):
                brief, value = facts(), plan(count)
                for key in ("class", "size_class", "model_size_class"):
                    brief[key] = value[key] = label
                self.assertEqual(expected, classify(brief, value))

    def test_declared_action_classes_force_large(self):
        # CDXINT109 F2: the size never comes from the brief alone.
        for name, value in (("connector_classes", ["local"]),
                            ("operation_classes", ["stage_write"]),
                            ("operation_classes", ("check",))):
            with self.subTest(name=name, value=value):
                data = plan()
                data[name] = value
                self.assertEqual(("large", ["declared action classes"]),
                                 classify(facts(), data))

    def test_plan_object_and_hash_unchanged(self):
        for classes, expected in ((None, "large"), ([], "small")):
            data = example()
            if classes is not None:
                data["connector_classes"] = data["operation_classes"] = classes
            value = Plan.from_dict(data)
            before = value.canonical_json(), value.plan_hash
            self.assertEqual(expected, classify(facts(), value)[0])
            self.assertEqual(before, (value.canonical_json(), value.plan_hash))

    def test_pure_and_deterministic(self):
        brief, value = facts(), plan(6)
        before = copy.deepcopy((brief, value))
        first = classify(brief, value)
        self.assertEqual(first, classify(brief, value))
        first[1].append("caller change")
        self.assertNotIn("caller change", classify(brief, value)[1])
        self.assertEqual(before, (brief, value))


if __name__ == "__main__":
    unittest.main()
