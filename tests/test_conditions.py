"""Pure deterministic condition predicates (Condition Watch kernel).

What these tests are actually trying to break:

FABRICATED FALSE  the failure the three-valued design exists to prevent. A
                  read that went missing, failed, was refused, covered only a
                  region, or looked at a different file must answer UNKNOWN.
                  Downstream, a FALSE from LOCAL_READ_SHA256_DIFFERS reads as
                  "the file is unchanged" - a positive claim no failed read
                  can support - so every non-conclusive path is asserted
                  individually rather than as a group.
MASQUERADE        a region observation, a duck-typed impostor, a subclass, a
                  ``str`` subclass with a lying ``__eq__``, a raw dict. None
                  may reach the comparison.
COERCION          ``bool(result)`` must raise rather than fold UNKNOWN into a
                  branch, and no status or digest field may be read through
                  ``bool()``.
PURITY            asserted twice over: from the module's own import set and
                  syntax tree (it cannot reach a file, socket, process or
                  clock, because it imports nothing that could), and again at
                  runtime with those primitives patched to raise.
NON-AUTHORITY     the module imports nothing from the rest of ``jarvis``, so
                  it cannot consult - or appear to establish - task status,
                  approval, billing, classification or connector trust.

Nothing here reads a file, opens a socket, starts a process or looks at a
clock.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import os
import socket
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import conditions
from jarvis.conditions import (
    ConditionError,
    ConditionKind,
    ConditionResult,
    FileObservation,
    LocalReadCondition,
    ObservationScope,
    ObservationStatus,
    evaluate,
)


PATH = "notes/report.md"
OTHER_PATH = "notes/other.md"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
#: A real sha256 digest, so the canonical form is proven against something
#: other than a repeated character: sha256(b"jarvis").
DIGEST_REAL = "dda820158d142a0b73380cd3efa9ff7d9fa11336e9ba4b3693098964537d61a3"

EQUALS = ConditionKind.LOCAL_READ_SHA256_EQUALS
DIFFERS = ConditionKind.LOCAL_READ_SHA256_DIFFERS


def condition(kind: ConditionKind = EQUALS, digest: str = DIGEST_A) -> LocalReadCondition:
    return LocalReadCondition(kind=kind, reference=PATH, expected_sha256=digest)


def whole_file(digest: str = DIGEST_A, reference: str = PATH) -> FileObservation:
    return FileObservation(
        reference=reference,
        status=ObservationStatus.OK,
        scope=ObservationScope.WHOLE_FILE,
        sha256=digest,
    )


class VocabularyTest(unittest.TestCase):
    """Exactly two kinds, three results, and no way to add a third from data."""

    def test_only_the_approved_condition_kinds_exist(self) -> None:
        self.assertEqual(
            {kind.value for kind in ConditionKind},
            {"local_read_sha256_equals", "local_read_sha256_differs"},
        )

    def test_the_result_enum_is_exactly_three_valued(self) -> None:
        self.assertEqual(
            {result.value for result in ConditionResult},
            {"true", "false", "unknown"},
        )

    def test_true_false_and_unknown_remain_distinct(self) -> None:
        """Requirement 23. Three values, pairwise distinct, none aliasing."""
        members = [ConditionResult.TRUE, ConditionResult.FALSE, ConditionResult.UNKNOWN]
        self.assertEqual(len(set(members)), 3)
        for left in members:
            for right in members:
                if left is right:
                    continue
                self.assertNotEqual(left, right)
                self.assertIsNot(left, right)
        # And the one that matters most: UNKNOWN is not FALSE, by any reading.
        self.assertIsNot(ConditionResult.UNKNOWN, ConditionResult.FALSE)
        self.assertNotEqual(ConditionResult.UNKNOWN, ConditionResult.FALSE)

    def test_a_result_refuses_truthiness(self) -> None:
        """``if evaluate(...):`` must not silently swallow UNKNOWN."""
        for result in ConditionResult:
            with self.subTest(result=result.value):
                with self.assertRaises(ConditionError):
                    bool(result)
                with self.assertRaises(ConditionError):
                    if result:  # noqa: SIM103 - the coercion IS the assertion
                        pass
                with self.assertRaises(ConditionError):
                    not result

    def test_no_parser_factory_or_expression_entry_point_is_exported(self) -> None:
        """Requirement 24, half of 17. There is no string-to-predicate route."""
        self.assertEqual(
            set(conditions.__all__),
            {
                "ConditionError",
                "ConditionKind",
                "ConditionResult",
                "FileObservation",
                "LocalReadCondition",
                "ObservationScope",
                "ObservationStatus",
                "evaluate",
            },
        )
        for banned in (
            "parse",
            "from_dict",
            "from_string",
            "from_json",
            "compile_condition",
            "build",
            "make",
            "loads",
            "comparator",
            "operator",
            "expression",
        ):
            with self.subTest(name=banned):
                self.assertFalse(hasattr(conditions, banned))


class TruthTableTest(unittest.TestCase):
    """Requirements 1-4. The whole conclusive surface, both kinds."""

    def test_equals_is_true_only_on_an_exact_match(self) -> None:
        self.assertIs(
            evaluate(condition(EQUALS, DIGEST_A), whole_file(DIGEST_A)),
            ConditionResult.TRUE,
        )

    def test_equals_is_false_on_a_conclusive_difference(self) -> None:
        self.assertIs(
            evaluate(condition(EQUALS, DIGEST_A), whole_file(DIGEST_B)),
            ConditionResult.FALSE,
        )

    def test_differs_is_false_on_an_exact_match(self) -> None:
        self.assertIs(
            evaluate(condition(DIFFERS, DIGEST_A), whole_file(DIGEST_A)),
            ConditionResult.FALSE,
        )

    def test_differs_is_true_on_a_conclusive_difference(self) -> None:
        self.assertIs(
            evaluate(condition(DIFFERS, DIGEST_A), whole_file(DIGEST_B)),
            ConditionResult.TRUE,
        )

    def test_the_two_kinds_are_exact_complements_when_conclusive(self) -> None:
        """Only where the answer is conclusive; UNKNOWN is not negatable."""
        for observed in (DIGEST_A, DIGEST_B, DIGEST_REAL):
            with self.subTest(observed=observed):
                equals = evaluate(condition(EQUALS, DIGEST_A), whole_file(observed))
                differs = evaluate(condition(DIFFERS, DIGEST_A), whole_file(observed))
                self.assertIn(equals, (ConditionResult.TRUE, ConditionResult.FALSE))
                self.assertIsNot(equals, differs)

    def test_a_single_character_difference_is_conclusive(self) -> None:
        near = DIGEST_A[:-1] + "b"
        self.assertIs(
            evaluate(condition(EQUALS, DIGEST_A), whole_file(near)),
            ConditionResult.FALSE,
        )
        self.assertIs(
            evaluate(condition(DIFFERS, DIGEST_A), whole_file(near)),
            ConditionResult.TRUE,
        )


class UnknownTest(unittest.TestCase):
    """Requirements 5, 7, 8, 9. Every non-conclusive path, never FALSE."""

    def test_a_missing_observation_is_unknown(self) -> None:
        """Requirement 5, as ``None``: no observation exists yet."""
        for kind in ConditionKind:
            with self.subTest(kind=kind.value):
                self.assertIs(evaluate(condition(kind), None), ConditionResult.UNKNOWN)

    def test_every_non_ok_status_is_unknown_for_both_kinds(self) -> None:
        """Requirements 5, 7, 8: missing, failed, refused, unknown."""
        for status in ObservationStatus:
            if status is ObservationStatus.OK:
                continue
            observation = FileObservation(
                reference=PATH, status=status, scope=ObservationScope.WHOLE_FILE
            )
            for kind in ConditionKind:
                with self.subTest(status=status.value, kind=kind.value):
                    self.assertIs(
                        evaluate(condition(kind), observation),
                        ConditionResult.UNKNOWN,
                    )

    def test_a_region_observation_is_unknown_even_when_the_digest_matches(self) -> None:
        """Requirement 9. The masquerade this rule exists to stop.

        The region's digest is REAL and happens to equal the expectation; a
        kernel that compared digests without consulting scope would answer
        TRUE here and claim a whole-file fact from a fragment.
        """
        region = FileObservation(
            reference=PATH,
            status=ObservationStatus.OK,
            scope=ObservationScope.REGION,
            sha256=DIGEST_A,
        )
        for kind in ConditionKind:
            with self.subTest(kind=kind.value):
                self.assertIs(
                    evaluate(condition(kind, DIGEST_A), region), ConditionResult.UNKNOWN
                )
        # And with a differing digest it is still UNKNOWN, not FALSE/TRUE.
        differing = FileObservation(
            reference=PATH,
            status=ObservationStatus.OK,
            scope=ObservationScope.REGION,
            sha256=DIGEST_B,
        )
        for kind in ConditionKind:
            with self.subTest(kind=kind.value, digest="differing"):
                self.assertIs(
                    evaluate(condition(kind, DIGEST_A), differing),
                    ConditionResult.UNKNOWN,
                )

    def test_an_observation_of_another_file_is_unknown(self) -> None:
        """Evidence about another file says nothing about this one."""
        other = whole_file(DIGEST_A, reference=OTHER_PATH)
        for kind in ConditionKind:
            with self.subTest(kind=kind.value):
                self.assertIs(
                    evaluate(condition(kind, DIGEST_A), other), ConditionResult.UNKNOWN
                )

    def test_no_unknown_path_ever_produces_false(self) -> None:
        """The single invariant the other tests decompose, stated whole."""
        non_conclusive: list[FileObservation | None] = [None]
        non_conclusive += [
            FileObservation(
                reference=PATH, status=status, scope=ObservationScope.WHOLE_FILE
            )
            for status in ObservationStatus
            if status is not ObservationStatus.OK
        ]
        non_conclusive += [
            FileObservation(
                reference=PATH,
                status=ObservationStatus.OK,
                scope=ObservationScope.REGION,
                sha256=digest,
            )
            for digest in (DIGEST_A, DIGEST_B)
        ]
        non_conclusive += [whole_file(DIGEST_A, reference=OTHER_PATH)]
        for observation in non_conclusive:
            for kind in ConditionKind:
                with self.subTest(observation=observation, kind=kind.value):
                    result = evaluate(condition(kind, DIGEST_A), observation)
                    self.assertIs(result, ConditionResult.UNKNOWN)
                    self.assertIsNot(result, ConditionResult.FALSE)
                    self.assertIsNot(result, ConditionResult.TRUE)


class DigestFormatTest(unittest.TestCase):
    """Requirements 10-15. One canonical form; everything else is refused."""

    #: Each entry is a non-canonical rendering of an otherwise valid digest.
    BAD_DIGESTS = (
        DIGEST_A.upper(),  # 11: uppercase
        DIGEST_REAL.upper(),
        DIGEST_A[:32] + DIGEST_A[32:].upper(),  # mixed case
        "sha256:" + DIGEST_A,  # 12: prefixed
        "SHA256:" + DIGEST_A,
        " " + DIGEST_A,  # 13: whitespace
        DIGEST_A + " ",
        DIGEST_A + "\n",
        DIGEST_A[:32] + " " + DIGEST_A[33:],
        "\t" + DIGEST_A[1:],
        "g" * 64,  # 14: non-hex
        DIGEST_A[:63] + "z",
        DIGEST_A[:63] + "-",
        DIGEST_A[:63] + "é",
        DIGEST_A[:63] + "ａ",  # fullwidth 'a' is not hex
        "",  # 15: wrong length
        DIGEST_A[:63],
        DIGEST_A + "a",
        DIGEST_A * 2,
        "0",
    )

    #: Wrong TYPE rather than wrong text: bytes must not be decoded, and
    #: nothing may be stringified into canonical form.
    BAD_TYPES = (
        DIGEST_A.encode(),
        bytearray(DIGEST_A.encode()),
        memoryview(DIGEST_A.encode()),
        None,
        0,
        0xA,
        True,
        ["a" * 64],
        {"sha256": DIGEST_A},
    )

    def test_a_malformed_expected_digest_is_refused(self) -> None:
        """Requirement 10, and 11-15 on the expectation side."""
        for bad in self.BAD_DIGESTS + self.BAD_TYPES:
            with self.subTest(digest=repr(bad)):
                with self.assertRaises(ConditionError):
                    LocalReadCondition(
                        kind=EQUALS,
                        reference=PATH,
                        expected_sha256=bad,  # type: ignore[arg-type]
                    )

    def test_a_malformed_observed_digest_is_refused(self) -> None:
        """The same canonical rule on the observation side; one form only."""
        for bad in self.BAD_DIGESTS + self.BAD_TYPES:
            with self.subTest(digest=repr(bad)):
                with self.assertRaises(ConditionError):
                    FileObservation(
                        reference=PATH,
                        status=ObservationStatus.OK,
                        scope=ObservationScope.WHOLE_FILE,
                        sha256=bad,  # type: ignore[arg-type]
                    )

    def test_the_canonical_form_is_accepted(self) -> None:
        for good in (DIGEST_A, DIGEST_B, DIGEST_REAL, "0" * 64, "0123456789abcdef" * 4):
            with self.subTest(digest=good):
                self.assertEqual(
                    LocalReadCondition(
                        kind=EQUALS, reference=PATH, expected_sha256=good
                    ).expected_sha256,
                    good,
                )

    def test_a_string_subclass_cannot_decide_the_comparison(self) -> None:
        """A lying ``__eq__`` must never reach the digest comparison."""

        class Agreeable(str):
            def __eq__(self, other: object) -> bool:
                return True

            def __hash__(self) -> int:
                return hash(str(self))

        agreeable = Agreeable(DIGEST_B)
        self.assertEqual(len(agreeable), 64)  # it would pass a length check
        with self.assertRaises(ConditionError):
            LocalReadCondition(
                kind=EQUALS, reference=PATH, expected_sha256=agreeable
            )
        with self.assertRaises(ConditionError):
            FileObservation(
                reference=PATH,
                status=ObservationStatus.OK,
                scope=ObservationScope.WHOLE_FILE,
                sha256=agreeable,
            )


class ConstructionRefusalTest(unittest.TestCase):
    """Requirements 6, 16, 17. Malformed input dies at construction, always."""

    def test_an_unknown_condition_kind_is_refused(self) -> None:
        """Requirement 16. Only an exact ``ConditionKind`` member is a kind."""
        for bad in (
            "local_read_sha256_equals",
            "LOCAL_READ_SHA256_EQUALS",
            "local_read_size_greater_than",
            0,
            1,
            None,
            True,
            ConditionResult.TRUE,
            ObservationStatus.OK,
            SimpleNamespace(value="local_read_sha256_equals"),
        ):
            with self.subTest(kind=repr(bad)):
                with self.assertRaises(ConditionError):
                    LocalReadCondition(
                        kind=bad,  # type: ignore[arg-type]
                        reference=PATH,
                        expected_sha256=DIGEST_A,
                    )

    def test_an_int_enum_lookalike_cannot_pass_as_a_kind(self) -> None:
        """A same-valued member of a DIFFERENT enum is still not the kind."""
        import enum

        class Impostor(enum.Enum):
            LOCAL_READ_SHA256_EQUALS = "local_read_sha256_equals"

        with self.assertRaises(ConditionError):
            LocalReadCondition(
                kind=Impostor.LOCAL_READ_SHA256_EQUALS,  # type: ignore[arg-type]
                reference=PATH,
                expected_sha256=DIGEST_A,
            )

    def test_a_malformed_observation_shape_is_refused_consistently(self) -> None:
        """Requirement 6. Every malformed shape raises; none degrades quietly."""
        base = {
            "reference": PATH,
            "status": ObservationStatus.OK,
            "scope": ObservationScope.WHOLE_FILE,
            "sha256": DIGEST_A,
        }
        malformed = [
            {**base, "reference": ""},
            {**base, "reference": None},
            {**base, "reference": 0},
            {**base, "reference": b"notes/report.md"},
            {**base, "reference": Path(PATH)},
            {**base, "status": "ok"},
            {**base, "status": "OK"},
            {**base, "status": None},
            {**base, "status": True},
            {**base, "status": 1},
            {**base, "status": ObservationScope.WHOLE_FILE},
            {**base, "scope": "whole_file"},
            {**base, "scope": None},
            {**base, "scope": True},
            {**base, "scope": ObservationStatus.OK},
            {**base, "scope": ConditionResult.TRUE},
        ]
        for fields in malformed:
            with self.subTest(fields={k: repr(v) for k, v in fields.items()}):
                with self.assertRaises(ConditionError):
                    FileObservation(**fields)  # type: ignore[arg-type]

    def test_a_non_ok_observation_may_not_carry_a_digest(self) -> None:
        """A failed read holding a stale digest is a comparison waiting to happen."""
        for status in ObservationStatus:
            if status is ObservationStatus.OK:
                continue
            with self.subTest(status=status.value):
                with self.assertRaises(ConditionError):
                    FileObservation(
                        reference=PATH,
                        status=status,
                        scope=ObservationScope.WHOLE_FILE,
                        sha256=DIGEST_A,
                    )
                # The well-formed counterpart, with no digest, is accepted.
                self.assertEqual(
                    FileObservation(
                        reference=PATH,
                        status=status,
                        scope=ObservationScope.WHOLE_FILE,
                    ).sha256,
                    "",
                )

    def test_an_ok_observation_must_carry_a_digest(self) -> None:
        with self.assertRaises(ConditionError):
            FileObservation(
                reference=PATH,
                status=ObservationStatus.OK,
                scope=ObservationScope.WHOLE_FILE,
            )

    def test_unknown_fields_are_refused(self) -> None:
        """No extra field can ride along and be read by something later."""
        with self.assertRaises(TypeError):
            FileObservation(  # type: ignore[call-arg]
                reference=PATH,
                status=ObservationStatus.OK,
                scope=ObservationScope.WHOLE_FILE,
                sha256=DIGEST_A,
                authoritative=True,
            )
        with self.assertRaises(TypeError):
            LocalReadCondition(  # type: ignore[call-arg]
                kind=EQUALS,
                reference=PATH,
                expected_sha256=DIGEST_A,
                approved=True,
            )

    def test_constructed_objects_are_frozen(self) -> None:
        observation = whole_file()
        with self.assertRaises(Exception):
            observation.sha256 = DIGEST_B  # type: ignore[misc]
        predicate = condition()
        with self.assertRaises(Exception):
            predicate.kind = DIFFERS  # type: ignore[misc]


class NoUntypedAuthorityTest(unittest.TestCase):
    """Requirements 17, 18. Nothing untyped reaches the comparison."""

    def test_a_raw_dict_cannot_become_a_predicate_or_an_observation(self) -> None:
        """Requirement 18. A connector payload is not an authority record."""
        payload = {
            "kind": "local_read_sha256_equals",
            "reference": PATH,
            "expected_sha256": DIGEST_A,
            "sha256": DIGEST_A,
            "status": "ok",
            "scope": "whole_file",
        }
        with self.assertRaises(ConditionError):
            evaluate(payload, whole_file())  # type: ignore[arg-type]
        with self.assertRaises(ConditionError):
            evaluate(condition(), payload)  # type: ignore[arg-type]

    def test_arbitrary_model_text_cannot_become_a_predicate(self) -> None:
        """Requirement 17. No string is ever interpreted as a condition."""
        for text in (
            "local_read_sha256_equals",
            "sha256(file) == expected",
            "LOCAL_READ_SHA256_EQUALS notes/report.md " + DIGEST_A,
            "__import__('os').system('id')",
            "True",
            "1 == 1",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ConditionError):
                    evaluate(text, whole_file())  # type: ignore[arg-type]
                with self.assertRaises(ConditionError):
                    evaluate(condition(), text)  # type: ignore[arg-type]

    def test_a_duck_typed_impostor_is_refused(self) -> None:
        """Right attributes, wrong type. ``type() is`` and not ``isinstance``."""
        impostor = SimpleNamespace(
            reference=PATH,
            status=ObservationStatus.OK,
            scope=ObservationScope.WHOLE_FILE,
            sha256=DIGEST_A,
        )
        with self.assertRaises(ConditionError):
            evaluate(condition(), impostor)  # type: ignore[arg-type]
        fake_condition = SimpleNamespace(
            kind=EQUALS, reference=PATH, expected_sha256=DIGEST_A
        )
        with self.assertRaises(ConditionError):
            evaluate(fake_condition, whole_file())  # type: ignore[arg-type]

    def test_a_subclass_cannot_inherit_its_way_into_the_comparison(self) -> None:
        """Nothing may widen the contract by subclassing it."""

        class WiderObservation(FileObservation):
            pass

        class WiderCondition(LocalReadCondition):
            pass

        wider = WiderObservation(
            reference=PATH,
            status=ObservationStatus.OK,
            scope=ObservationScope.WHOLE_FILE,
            sha256=DIGEST_A,
        )
        with self.assertRaises(ConditionError):
            evaluate(condition(), wider)
        with self.assertRaises(ConditionError):
            evaluate(
                WiderCondition(
                    kind=EQUALS, reference=PATH, expected_sha256=DIGEST_A
                ),
                whole_file(),
            )

    def test_no_status_or_trust_field_is_read_through_bool(self) -> None:
        """Requirement: no ``bool(value)`` coercion on status/trust fields.

        Read off the syntax tree, because the property is an absence: the
        module must contain no ``bool(...)`` call at all.
        """
        tree = ast.parse(Path(conditions.__file__).read_text(encoding="utf-8"))
        called = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("bool", called)


class PurityTest(unittest.TestCase):
    """Requirements 19-21, 24. Proven from the code, then again at runtime."""

    #: Everything the module is allowed to import. Anything else - ``os``,
    #: ``io``, ``socket``, ``subprocess``, ``time``, ``datetime``, ``re``,
    #: ``hashlib``, ``pathlib``, or any ``jarvis`` module - would give it a
    #: route to a file, a socket, a process, a clock or an authority record.
    ALLOWED_IMPORTS = {"__future__", "dataclasses", "enum"}

    def _tree(self) -> ast.Module:
        return ast.parse(Path(conditions.__file__).read_text(encoding="utf-8"))

    def test_the_module_imports_nothing_that_could_reach_the_world(self) -> None:
        """Requirements 19, 20, 21, and the non-authority invariant.

        The strongest available proof, and the reason the runtime guard below
        is a second line rather than the first: a module that never imports
        ``os``, ``socket``, ``subprocess``, ``time`` or ``datetime`` cannot
        touch a filesystem, a network, a process or a clock down any branch,
        reachable or not.
        """
        imported: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.fail("the kernel uses a relative import")
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported, self.ALLOWED_IMPORTS)
        self.assertEqual(imported & {"jarvis"}, set())

    def test_no_dynamic_execution_or_expression_machinery_exists(self) -> None:
        """Requirement 24. No comparator framework, no expression language."""
        called = {
            ast.unparse(node.func)
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call)
        }
        self.assertEqual(
            called
            & {
                "eval",
                "exec",
                "compile",
                "__import__",
                "importlib.import_module",
                "getattr",
                "setattr",
                "globals",
                "locals",
                "vars",
                "open",
                "input",
                "re.compile",
                "re.match",
                "re.search",
                "operator.eq",
            },
            set(),
        )

    def test_no_callable_field_can_be_supplied_as_a_predicate(self) -> None:
        """A Python callback is not a condition; a typed kind is."""
        with self.assertRaises(ConditionError):
            LocalReadCondition(
                kind=lambda observation: True,  # type: ignore[arg-type]
                reference=PATH,
                expected_sha256=DIGEST_A,
            )
        with self.assertRaises(ConditionError):
            evaluate(lambda: ConditionResult.TRUE, whole_file())  # type: ignore[arg-type]

    def test_evaluation_touches_no_file_socket_process_or_clock(self) -> None:
        """Requirements 19-21, at runtime. Fail-if-touched, not fail-if-slow.

        Results are collected inside the guarded block and asserted outside
        it, so a failure cannot be masked by ``unittest`` needing ``open`` to
        format a traceback.
        """

        def forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("the kernel touched a forbidden primitive")

        cases = [
            (condition(EQUALS, DIGEST_A), whole_file(DIGEST_A), ConditionResult.TRUE),
            (condition(EQUALS, DIGEST_A), whole_file(DIGEST_B), ConditionResult.FALSE),
            (condition(DIFFERS, DIGEST_A), whole_file(DIGEST_A), ConditionResult.FALSE),
            (condition(DIFFERS, DIGEST_A), whole_file(DIGEST_B), ConditionResult.TRUE),
            (condition(EQUALS, DIGEST_A), None, ConditionResult.UNKNOWN),
            (
                condition(EQUALS, DIGEST_A),
                FileObservation(
                    reference=PATH,
                    status=ObservationStatus.FAILED,
                    scope=ObservationScope.WHOLE_FILE,
                ),
                ConditionResult.UNKNOWN,
            ),
            (
                condition(EQUALS, DIGEST_A),
                FileObservation(
                    reference=PATH,
                    status=ObservationStatus.OK,
                    scope=ObservationScope.REGION,
                    sha256=DIGEST_A,
                ),
                ConditionResult.UNKNOWN,
            ),
        ]
        targets = [
            (builtins, "open"),
            (os, "stat"),
            (os, "listdir"),
            (os, "scandir"),
            (os, "getenv"),
            (socket, "socket"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (subprocess, "Popen"),
            (subprocess, "run"),
            (time, "time"),
            (time, "monotonic"),
            (time, "time_ns"),
            (time, "sleep"),
        ]
        observed = []
        error: BaseException | None = None
        with contextlib.ExitStack() as stack:
            for module, name in targets:
                stack.enter_context(mock.patch.object(module, name, forbidden))
            try:
                observed = [evaluate(predicate, obs) for predicate, obs, _ in cases]
            except BaseException as caught:  # re-raised outside the guard
                error = caught
        if error is not None:
            raise error
        self.assertEqual(observed, [expected for _, _, expected in cases])


class DeterminismTest(unittest.TestCase):
    """Requirement 22. Same typed inputs, same answer, every time."""

    def test_repeated_evaluation_is_stable(self) -> None:
        cases = [
            (condition(EQUALS, DIGEST_A), whole_file(DIGEST_A)),
            (condition(EQUALS, DIGEST_A), whole_file(DIGEST_B)),
            (condition(DIFFERS, DIGEST_A), whole_file(DIGEST_A)),
            (condition(DIFFERS, DIGEST_A), whole_file(DIGEST_B)),
            (condition(EQUALS, DIGEST_A), None),
        ]
        for predicate, observation in cases:
            first = evaluate(predicate, observation)
            with self.subTest(kind=predicate.kind.value, observation=observation):
                self.assertEqual(
                    {evaluate(predicate, observation) for _ in range(500)}, {first}
                )

    def test_equal_inputs_built_separately_agree(self) -> None:
        """Determinism across object identity, not just across repetition."""
        for kind in ConditionKind:
            for digest in (DIGEST_A, DIGEST_B):
                with self.subTest(kind=kind.value, digest=digest):
                    left = evaluate(condition(kind, DIGEST_A), whole_file(digest))
                    right = evaluate(condition(kind, DIGEST_A), whole_file(digest))
                    self.assertIs(left, right)

    def test_evaluation_does_not_mutate_its_inputs(self) -> None:
        predicate = condition(EQUALS, DIGEST_A)
        observation = whole_file(DIGEST_B)
        before = (
            predicate.kind,
            predicate.reference,
            predicate.expected_sha256,
            observation.reference,
            observation.status,
            observation.scope,
            observation.sha256,
        )
        evaluate(predicate, observation)
        self.assertEqual(
            (
                predicate.kind,
                predicate.reference,
                predicate.expected_sha256,
                observation.reference,
                observation.status,
                observation.scope,
                observation.sha256,
            ),
            before,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
