"""M9 - Bounded Region Read.

What this file pins, end to end:

    read.start_line / read.max_lines -> durable LocalReadKind -> a SEEK/CAPTURE
    streaming read over the SAME proven descriptor -> a region that must CLOSE
    within the capture ceiling -> a snapshot whose span describes the BOUNDED
    EXCERPT -> M3 / M8 / model payload

plus everything that must NOT happen: a corrupted region identity degrading
into a head read wearing a region label, a PARTIAL capture arriving as a
successful region, the synthetic truncation ellipsis inventing a source line,
a span that disagrees with the excerpt it describes, and any change at all to
the head read.

The producer is the real ``LocalFileReadConnector`` on a temp root; the M8
consumer is the provider-neutral ``PlanningConnector``. No live model,
connector, auth or network call is ever made.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.app import (
    OUTPUT_SCHEMA_VERSION,
    TaskInputError,
    _local_read_payload,
    create_app,
    decode_task_json,
)
from jarvis.connectors import local_files
from jarvis.connectors.claude_code import EGRESS_TOOL_GRANTS, ClaudeCodeConnector
from jarvis.connectors.local_files import LocalFileReadConnector
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_EXCERPT_MAX_CHARS,
    LOCAL_READ_MAX_BYTES,
    LOCAL_READ_MAX_REGION_LINES,
    LOCAL_READ_MAX_START_LINE,
    LOCAL_READ_REGION_SCAN_MAX_BYTES,
    LOCAL_READ_REGION_SECTION,
    LOCAL_READ_TASK_REFERENCE_KEY,
    ActionScope,
    DataClassification,
    ErrorCode,
    LocalReadContextKind,
    LocalReadKind,
    LocalReadSnapshot,
    PermissionMode,
    SessionState,
    Task,
    TaskStatus,
    bound_local_text,
    excerpt_line_span,
    local_read_region_refusal,
    local_read_snapshot_refusal,
)
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, open_app, safe_cleanup
from tests.test_local_read_context import PlanningConnector

ELLIPSIS = "…"
READ_AT = "2026-01-01T00:00:00Z"
#: 512 bytes per line, so 128 lines is EXACTLY the capture ceiling.
CEILING_LINE = "x" * 511 + "\n"
CEILING_LINES = LOCAL_READ_MAX_BYTES // 512


#: The public envelope's local_read block, for a legacy HEAD read. Frozen.
HEAD_ENVELOPE_KEYS = [
    "byte_size",
    "classification",
    "excerpt",
    "excerpt_characters",
    "read_at",
    "relative_path",
    "sha256",
    "truncated",
]
#: The same block for a REGION read: the head keys plus the span, no more.
REGION_ENVELOPE_KEYS = sorted(HEAD_ENVELOPE_KEYS + ["start_line", "end_line"])


def numbered(count: int, width: int = 8) -> str:
    """``count`` distinguishable fixed-width lines, so a span is checkable."""
    return "".join(f"L{index:0{width - 2}d}\n" for index in range(1, count + 1))


class _DescriptorProbe:
    """A handle wrapper that records the KERNEL offset after every read.

    ``os.lseek(fd, 0, SEEK_CUR)`` reports where the DESCRIPTOR actually is,
    which is the only thing that separates a bounded read from a buffered
    prefetch: a buffered reader's own ``tell()`` stays politely inside the
    ceiling while the file it sits on has already been read far past it. Every
    physical bound in this file is asserted against these offsets, never
    against a counter the implementation keeps about itself.
    """

    def __init__(self, handle, offsets: list[int]) -> None:
        self._handle = handle
        self._offsets = offsets

    def read(self, *args):
        data = self._handle.read(*args)
        self._offsets.append(os.lseek(self._handle.fileno(), 0, os.SEEK_CUR))
        return data

    def fileno(self) -> int:
        return self._handle.fileno()

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


@contextlib.contextmanager
def descriptor_offsets():
    """Yield the list of kernel offsets reached by the reader's descriptor."""
    seen: list[int] = []
    real = os.fdopen

    def probed(fd, *args, **kwargs):
        return _DescriptorProbe(real(fd, *args, **kwargs), seen)

    with mock.patch.object(local_files.os, "fdopen", probed):
        yield seen


def snapshot_of(
    excerpt: str, *, start_line: int = 0, end_line: int = 0, truncated: bool = False
) -> LocalReadSnapshot:
    return LocalReadSnapshot(
        relative_path="a.txt",
        byte_size=len(excerpt.encode("utf-8")),
        sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        excerpt=excerpt,
        truncated=truncated,
        classification=DataClassification.INTERNAL,
        read_at=READ_AT,
        start_line=start_line,
        end_line=end_line,
    )


class RegionReadBase(unittest.TestCase):
    """A real temp root and the real local-read connector."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def write(self, name: str, content: str | bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
        return path

    def region_task(
        self,
        target: Path | str,
        start_line: int,
        max_lines: int,
        *,
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> Task:
        task = Task(
            objective="read a region",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(
                action="read",
                target=str(target),
                data_classification=classification,
            ),
            context={"working_directory": str(self.root)},
        )
        task.local_read_kind = LocalReadKind.REGION
        task.read_start_line = start_line
        task.read_max_lines = max_lines
        return task

    def head_task(self, target: Path | str) -> Task:
        return Task(
            objective="read the head",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(
                action="read",
                target=str(target),
                data_classification=DataClassification.INTERNAL,
            ),
            context={"working_directory": str(self.root)},
        )

    def run_task(self, task: Task):
        return self.connector.execute(task, SessionState())

    def read_region(self, target, start_line, max_lines, **kwargs):
        return self.run_task(self.region_task(target, start_line, max_lines, **kwargs))

    def snapshot(self, result) -> LocalReadSnapshot:
        self.assertIsNone(result.error, msg=str(result.summary))
        snapshot = (result.data or {})["local_read"]
        # Every successful snapshot must satisfy the ONE canonical contract.
        self.assertIsNone(local_read_snapshot_refusal(snapshot), msg=str(snapshot))
        return snapshot

    def assert_refused(self, result, fragment: str = "") -> None:
        self.assertIsNotNone(result.error, msg=str(result.summary))
        self.assertIs(ErrorCode.INVALID_TASK, result.error.code)
        self.assertFalse(result.error.retryable)
        self.assertNotIn("local_read", result.data or {})
        if fragment:
            self.assertIn(fragment, result.summary)


# ==========================================================================
# INPUT / IDENTITY
# ==========================================================================
class InputContractTests(unittest.TestCase):
    def decode(self, read=None, capability: str = LOCAL_READ_CAPABILITY) -> Task:
        document = {
            "objective": "read",
            "capability": capability,
            "scope": {"action": "read", "target": "notes.txt"},
        }
        if read is not None:
            document[LOCAL_READ_REGION_SECTION] = read
        return decode_task_json(json.dumps(document))

    def test_a_valid_region_decodes_to_the_region_kind(self) -> None:
        task = self.decode({"start_line": 1900, "max_lines": 90})
        self.assertIs(LocalReadKind.REGION, task.local_read_kind)
        self.assertEqual(1900, task.read_start_line)
        self.assertEqual(90, task.read_max_lines)

    def test_absence_of_the_section_is_the_legacy_head_read(self) -> None:
        task = self.decode()
        self.assertIs(LocalReadKind.HEAD, task.local_read_kind)
        self.assertEqual(0, task.read_start_line)
        self.assertEqual(0, task.read_max_lines)
        self.assertNotIn(LOCAL_READ_REGION_SECTION, task.context)

    def test_the_boundary_values_are_accepted(self) -> None:
        task = self.decode(
            {
                "start_line": LOCAL_READ_MAX_START_LINE,
                "max_lines": LOCAL_READ_MAX_REGION_LINES,
            }
        )
        self.assertIs(LocalReadKind.REGION, task.local_read_kind)

    def test_out_of_range_bounds_are_refused(self) -> None:
        for read in (
            {"start_line": 0, "max_lines": 10},
            {"start_line": -1, "max_lines": 10},
            {"start_line": LOCAL_READ_MAX_START_LINE + 1, "max_lines": 1},
            {"start_line": 1, "max_lines": 0},
            {"start_line": 1, "max_lines": -5},
            {"start_line": 1, "max_lines": LOCAL_READ_MAX_REGION_LINES + 1},
            {"start_line": 1, "max_lines": 10**12},
        ):
            with self.subTest(read=read), self.assertRaises(TaskInputError):
                self.decode(read)

    def test_non_integer_bounds_are_refused(self) -> None:
        for value in (True, False, 1.5, "3", None, [1], {"a": 1}):
            with self.subTest(value=value), self.assertRaises(TaskInputError):
                self.decode({"start_line": value, "max_lines": 10})
            with self.subTest(value=value), self.assertRaises(TaskInputError):
                self.decode({"start_line": 1, "max_lines": value})

    def test_a_boolean_is_never_read_as_a_line_number(self) -> None:
        """``True`` is an ``int``; an exact-type check is the only defence."""
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"start_line": True, "max_lines": 10})
        self.assertIn("must be an integer", str(caught.exception))

    def test_both_parameters_are_mandatory(self) -> None:
        for read in ({"start_line": 1}, {"max_lines": 5}, {}):
            with self.subTest(read=read), self.assertRaises(TaskInputError):
                self.decode(read)

    def test_an_empty_section_is_refused_not_treated_as_a_head_read(self) -> None:
        """Absence means head; an empty section is an incomplete request."""
        with self.assertRaises(TaskInputError) as caught:
            self.decode({})
        self.assertIn("start_line", str(caught.exception))
        self.assertIs(LocalReadKind.HEAD, self.decode().local_read_kind)

    def test_a_non_object_section_is_refused(self) -> None:
        document = {
            "objective": "read",
            "capability": LOCAL_READ_CAPABILITY,
            "scope": {"action": "read", "target": "notes.txt"},
            LOCAL_READ_REGION_SECTION: "1900",
        }
        with self.assertRaises(TaskInputError):
            decode_task_json(json.dumps(document))

    def test_unknown_read_keys_are_refused(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"start_line": 1, "max_lines": 5, "end_line": 9})
        self.assertIn("end_line", str(caught.exception))

    def test_the_section_is_only_valid_for_file_read(self) -> None:
        with self.assertRaises(TaskInputError) as caught:
            self.decode({"start_line": 1, "max_lines": 5}, capability="code_analysis")
        self.assertIn(LOCAL_READ_CAPABILITY, str(caught.exception))

    def test_the_pair_contract_is_exact(self) -> None:
        self.assertIsNone(local_read_region_refusal(LocalReadKind.HEAD, 0, 0))
        self.assertIsNone(local_read_region_refusal(LocalReadKind.REGION, 1, 1))
        for kind, start, count in (
            (LocalReadKind.HEAD, 1, 0),
            (LocalReadKind.HEAD, 0, 1),
            (LocalReadKind.INVALID, 0, 0),
            (LocalReadKind.REGION, 0, 5),
            (LocalReadKind.REGION, 1, 0),
            (LocalReadKind.REGION, True, 5),
            ("region", 1, 5),
        ):
            with self.subTest(kind=kind, start=start, count=count):
                self.assertIsNotNone(local_read_region_refusal(kind, start, count))


class DurableIdentityTests(unittest.TestCase):
    def base(self) -> dict:
        return Task(
            objective="o",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(action="read", target="a.txt"),
        ).to_dict()

    def kind_of(self, state: dict) -> LocalReadKind:
        return Task.from_dict(state).local_read_kind

    def test_a_genuine_legacy_row_loads_as_head(self) -> None:
        state = self.base()
        for key in ("local_read_kind", "read_start_line", "read_max_lines"):
            state.pop(key, None)
        task = Task.from_dict(state)
        self.assertIs(LocalReadKind.HEAD, task.local_read_kind)
        self.assertEqual(0, task.read_start_line)
        self.assertEqual(0, task.read_max_lines)

    def test_a_deleted_kind_with_a_window_retained_is_invalid(self) -> None:
        """The anti-degrade rule: partial M9 state is never a head read."""
        state = self.base()
        state.pop("local_read_kind")
        state["read_start_line"] = 1900
        state["read_max_lines"] = 90
        self.assertIs(LocalReadKind.INVALID, self.kind_of(state))

    def test_a_head_carrying_a_window_is_invalid(self) -> None:
        state = self.base()
        state["local_read_kind"] = "head"
        state["read_start_line"] = 1900
        self.assertIs(LocalReadKind.INVALID, self.kind_of(state))

    def test_garbage_kinds_are_invalid(self) -> None:
        for value in (5, None, True, [], {}, "", "REGION", "reg"):
            state = self.base()
            state["local_read_kind"] = value
            with self.subTest(value=value):
                self.assertIs(LocalReadKind.INVALID, self.kind_of(state))

    def test_non_integer_window_values_are_invalid(self) -> None:
        for value in (True, 1.5, "3", None, [1]):
            state = self.base()
            state["local_read_kind"] = "region"
            state["read_start_line"] = value
            state["read_max_lines"] = 5
            with self.subTest(value=value):
                self.assertIs(LocalReadKind.INVALID, self.kind_of(state))

    def test_out_of_range_persisted_windows_are_invalid(self) -> None:
        for start, count in ((0, 5), (1, 0), (1, LOCAL_READ_MAX_REGION_LINES + 1)):
            state = self.base()
            state["local_read_kind"] = "region"
            state["read_start_line"] = start
            state["read_max_lines"] = count
            with self.subTest(start=start, count=count):
                self.assertIs(LocalReadKind.INVALID, self.kind_of(state))

    def test_invalid_survives_repeated_round_trips(self) -> None:
        state = self.base()
        state["local_read_kind"] = "invalid"
        task = Task.from_dict(state)
        for _ in range(3):
            self.assertIs(LocalReadKind.INVALID, task.local_read_kind)
            self.assertEqual(0, task.read_start_line)
            self.assertEqual(0, task.read_max_lines)
            task = Task.from_dict(task.to_dict())

    def test_a_raw_string_kind_serializes_as_invalid(self) -> None:
        task = Task(
            objective="o",
            capability=LOCAL_READ_CAPABILITY,
            scope=ActionScope(action="read", target="a.txt"),
        )
        task.local_read_kind = "region"  # type: ignore[assignment]
        self.assertEqual("invalid", task.to_dict()["local_read_kind"])

    def test_a_valid_region_round_trips(self) -> None:
        state = self.base()
        state["local_read_kind"] = "region"
        state["read_start_line"] = 1900
        state["read_max_lines"] = 90
        task = Task.from_dict(state)
        self.assertIs(LocalReadKind.REGION, task.local_read_kind)
        self.assertEqual((1900, 90), (task.read_start_line, task.read_max_lines))
        again = Task.from_dict(task.to_dict())
        self.assertIs(LocalReadKind.REGION, again.local_read_kind)


class IdentityRefusalTests(RegionReadBase):
    def test_an_invalid_identity_never_becomes_a_head_read(self) -> None:
        """The three-way branch. A two-way one would succeed here."""
        self.write("notes.txt", numbered(50))
        task = self.head_task(self.root / "notes.txt")
        task.local_read_kind = LocalReadKind.INVALID
        result = self.run_task(task)
        self.assert_refused(result, "window is not usable")

    def test_a_head_task_carrying_a_window_is_refused(self) -> None:
        self.write("notes.txt", numbered(50))
        task = self.head_task(self.root / "notes.txt")
        task.read_start_line = 5
        result = self.run_task(task)
        self.assert_refused(result, "head read carries a region window")

    def test_a_region_task_with_a_blanked_window_is_refused(self) -> None:
        self.write("notes.txt", numbered(50))
        task = self.region_task(self.root / "notes.txt", 1, 5)
        task.read_start_line = 0
        result = self.run_task(task)
        self.assert_refused(result, "window is not usable")


# ==========================================================================
# BOUNDARIES
# ==========================================================================
class CaptureBoundaryTests(RegionReadBase):
    def test_a_region_that_closes_below_the_ceiling_succeeds(self) -> None:
        self.write("notes.txt", numbered(500))
        snapshot = self.snapshot(self.read_region(self.root / "notes.txt", 100, 10))
        self.assertEqual((100, 109), (snapshot.start_line, snapshot.end_line))
        self.assertFalse(snapshot.truncated)
        self.assertTrue(snapshot.excerpt.startswith("L000100"))
        self.assertIn("L000109", snapshot.excerpt)
        self.assertNotIn("L000110", snapshot.excerpt)

    def test_a_region_closing_exactly_at_the_ceiling_succeeds(self) -> None:
        self.write("big.txt", CEILING_LINE * CEILING_LINES)
        snapshot = self.snapshot(
            self.read_region(self.root / "big.txt", 1, CEILING_LINES)
        )
        self.assertEqual(LOCAL_READ_MAX_BYTES, snapshot.byte_size)

    def test_eof_exactly_at_the_ceiling_succeeds(self) -> None:
        self.write("big.txt", CEILING_LINE * CEILING_LINES)
        snapshot = self.snapshot(
            self.read_region(self.root / "big.txt", 1, LOCAL_READ_MAX_REGION_LINES)
        )
        self.assertEqual(LOCAL_READ_MAX_BYTES, snapshot.byte_size)

    def test_one_byte_past_the_ceiling_with_the_region_open_refuses(self) -> None:
        self.write("big.txt", CEILING_LINE * CEILING_LINES + "y")
        result = self.read_region(
            self.root / "big.txt", 1, LOCAL_READ_MAX_REGION_LINES
        )
        self.assert_refused(result, "did not close within")
        self.assertIn("request fewer lines", result.summary)

    def test_a_region_needing_more_than_the_ceiling_refuses(self) -> None:
        self.write("big.txt", CEILING_LINE * (CEILING_LINES + 10))
        self.assert_refused(
            self.read_region(self.root / "big.txt", 1, CEILING_LINES + 1),
            "did not close within",
        )

    def test_a_giant_single_line_refuses(self) -> None:
        self.write("one.txt", "z" * (LOCAL_READ_MAX_BYTES + 500))
        self.assert_refused(
            self.read_region(self.root / "one.txt", 1, 1), "did not close within"
        )

    def test_a_start_line_past_eof_refuses_and_names_the_line_count(self) -> None:
        self.write("notes.txt", numbered(3))
        result = self.read_region(self.root / "notes.txt", 9, 5)
        self.assert_refused(result, "has 3 line(s)")

    def test_the_first_line_past_eof_refuses(self) -> None:
        self.write("notes.txt", numbered(3))
        self.assert_refused(
            self.read_region(self.root / "notes.txt", 4, 5), "has 3 line(s)"
        )

    def test_an_empty_file_refuses_a_region(self) -> None:
        self.write("empty.txt", "")
        self.assert_refused(
            self.read_region(self.root / "empty.txt", 1, 5), "has 0 line(s)"
        )

    def test_a_short_tail_returns_the_remaining_lines(self) -> None:
        self.write("notes.txt", numbered(10))
        snapshot = self.snapshot(self.read_region(self.root / "notes.txt", 8, 50))
        self.assertEqual((8, 10), (snapshot.start_line, snapshot.end_line))
        self.assertFalse(snapshot.truncated)

    def test_a_short_tail_with_no_truncation_means_eof(self) -> None:
        """The inference the capture rule exists to make sound."""
        self.write("notes.txt", numbered(10))
        snapshot = self.snapshot(self.read_region(self.root / "notes.txt", 8, 50))
        span = snapshot.end_line - snapshot.start_line + 1
        self.assertFalse(snapshot.truncated)
        self.assertLess(span, 50)
        # And it really is the end of the file.
        self.assert_refused(
            self.read_region(self.root / "notes.txt", 11, 5), "has 10 line(s)"
        )

    def test_the_scan_ceiling_is_a_separate_policy_surface(self) -> None:
        self.assertNotEqual(LOCAL_READ_REGION_SCAN_MAX_BYTES, LOCAL_READ_MAX_BYTES)
        self.assertEqual(4 * 1024 * 1024, LOCAL_READ_REGION_SCAN_MAX_BYTES)

    def test_seeking_past_the_scan_ceiling_refuses(self) -> None:
        """Behavioural. Both the ceiling and the chunk are lowered together -
        the ceiling is checked per chunk, so a ceiling below one chunk would
        refuse everything and prove nothing."""
        self.write("notes.txt", numbered(4000))
        with mock.patch.object(local_files, "_REGION_CHUNK_BYTES", 256):
            with mock.patch.object(
                local_files, "LOCAL_READ_REGION_SCAN_MAX_BYTES", 2048
            ):
                result = self.read_region(self.root / "notes.txt", 3000, 5)
        self.assert_refused(result, "scan ceiling")

    def test_a_region_within_the_scan_ceiling_still_succeeds(self) -> None:
        """The control: the same lowered ceiling must not refuse a near region."""
        self.write("notes.txt", numbered(4000))
        with mock.patch.object(local_files, "_REGION_CHUNK_BYTES", 256):
            with mock.patch.object(
                local_files, "LOCAL_READ_REGION_SCAN_MAX_BYTES", 2048
            ):
                snapshot = self.snapshot(
                    self.read_region(self.root / "notes.txt", 3, 2)
                )
        self.assertEqual((3, 4), (snapshot.start_line, snapshot.end_line))

    def test_reading_a_late_region_of_a_file_over_the_head_ceiling(self) -> None:
        """The whole point of M9: files the head read refuses outright."""
        content = numbered(20000)
        self.write("huge.txt", content)
        self.assertGreater(len(content.encode("utf-8")), LOCAL_READ_MAX_BYTES)
        self.assert_refused(
            self.run_task(self.head_task(self.root / "huge.txt")), "read ceiling"
        )
        snapshot = self.snapshot(self.read_region(self.root / "huge.txt", 15000, 5))
        self.assertEqual((15000, 15004), (snapshot.start_line, snapshot.end_line))
        self.assertTrue(snapshot.excerpt.startswith("L015000"))


# ==========================================================================
# SPAN
# ==========================================================================
class SpanTests(RegionReadBase):
    def test_the_helper_handles_every_directed_shape(self) -> None:
        for excerpt, truncated, expected in (
            ("", False, 0),
            ("alpha", False, 1),
            ("alpha\n", False, 1),
            ("a\nb", False, 2),
            ("a\nb\n", False, 2),
            ("\n\n\n", False, 3),
            ("abc" + ELLIPSIS, True, 1),
            ("a\n" + ELLIPSIS, True, 1),
            ("a\nb" + ELLIPSIS, False, 2),
        ):
            with self.subTest(excerpt=excerpt, truncated=truncated):
                self.assertEqual(expected, excerpt_line_span(excerpt, truncated))

    def test_a_truncation_landing_after_a_newline_invents_no_line(self) -> None:
        """The phantom-line case, which a naive newline count gets wrong."""
        excerpt = "a\n" + ELLIPSIS
        self.assertEqual(2, excerpt.count("\n") + 1)  # what naive counting says
        self.assertEqual(1, excerpt_line_span(excerpt, True))

    def test_a_real_trailing_ellipsis_is_preserved_when_not_truncated(self) -> None:
        self.assertEqual(2, excerpt_line_span("a\nb" + ELLIPSIS, False))

    def test_a_mid_line_truncation_counts_the_partial_line(self) -> None:
        self.write("wide.txt", ("q" * 200 + "\n") * 100)
        snapshot = self.snapshot(self.read_region(self.root / "wide.txt", 1, 100))
        self.assertTrue(snapshot.truncated)
        span = snapshot.end_line - snapshot.start_line + 1
        self.assertEqual(span, excerpt_line_span(snapshot.excerpt, snapshot.truncated))
        self.assertLess(span, 100)

    def test_crlf_input_is_normalized_and_counted_as_lf(self) -> None:
        self.write("crlf.txt", "a\r\nb\r\nc\r\n")
        snapshot = self.snapshot(self.read_region(self.root / "crlf.txt", 2, 2))
        self.assertEqual((2, 3), (snapshot.start_line, snapshot.end_line))
        self.assertNotIn("\r", snapshot.excerpt)
        self.assertEqual("b\nc\n", snapshot.excerpt)

    def test_a_final_line_without_a_newline_counts(self) -> None:
        self.write("tail.txt", "a\nb\nc")
        snapshot = self.snapshot(self.read_region(self.root / "tail.txt", 2, 5))
        self.assertEqual((2, 3), (snapshot.start_line, snapshot.end_line))
        self.assertEqual("b\nc", snapshot.excerpt)

    def test_a_single_line_file_reads_as_one_line(self) -> None:
        self.write("one.txt", "only")
        snapshot = self.snapshot(self.read_region(self.root / "one.txt", 1, 5))
        self.assertEqual((1, 1), (snapshot.start_line, snapshot.end_line))

    def test_blank_lines_count(self) -> None:
        self.write("blank.txt", "a\n\n\nb\n")
        snapshot = self.snapshot(self.read_region(self.root / "blank.txt", 2, 2))
        self.assertEqual((2, 3), (snapshot.start_line, snapshot.end_line))
        self.assertEqual("\n\n", snapshot.excerpt)

    def test_path_heavy_content_keeps_its_span(self) -> None:
        """Redaction changes line LENGTHS but never line COUNT."""
        self.write("paths.txt", "/home/user/project/file.txt\n" * 40)
        snapshot = self.snapshot(self.read_region(self.root / "paths.txt", 5, 10))
        self.assertEqual((5, 14), (snapshot.start_line, snapshot.end_line))
        self.assertNotIn("/home/user", snapshot.excerpt)

    def test_every_successful_span_matches_its_excerpt(self) -> None:
        self.write("notes.txt", numbered(400))
        for start, count in ((1, 1), (1, 50), (200, 90), (395, 20), (400, 5)):
            with self.subTest(start=start, count=count):
                snapshot = self.snapshot(
                    self.read_region(self.root / "notes.txt", start, count)
                )
                self.assertEqual(
                    snapshot.end_line - snapshot.start_line + 1,
                    excerpt_line_span(snapshot.excerpt, snapshot.truncated),
                )


class SpanTamperTests(unittest.TestCase):
    def test_the_head_span_sentinel_is_accepted(self) -> None:
        self.assertIsNone(local_read_snapshot_refusal(snapshot_of("a\n")))

    def test_a_head_task_carrying_a_window_is_refused_at_the_task_level(self) -> None:
        """The snapshot contract judges span CONSISTENCY; "a head read carries
        no window" is a property of the TASK, and that is where it is asked."""
        self.assertIsNotNone(local_read_region_refusal(LocalReadKind.HEAD, 5, 5))
        # A snapshot whose span simply agrees with its excerpt is well formed,
        # whatever produced it - so the snapshot layer must NOT be asked to
        # decide kind.
        self.assertIsNone(
            local_read_snapshot_refusal(snapshot_of("a\n", start_line=5, end_line=5))
        )

    def test_a_span_that_disagrees_with_the_excerpt_is_refused(self) -> None:
        good = snapshot_of("a\nb\nc\n", start_line=10, end_line=12)
        self.assertIsNone(local_read_snapshot_refusal(good))
        for start, end in ((10, 13), (10, 11), (9, 12), (10, 14), (10, 10)):
            with self.subTest(start=start, end=end):
                self.assertIsNotNone(
                    local_read_snapshot_refusal(
                        snapshot_of("a\nb\nc\n", start_line=start, end_line=end)
                    )
                )

    def test_a_consistent_shift_of_both_endpoints_is_the_documented_limit(
        self,
    ) -> None:
        """Honest boundary, recorded rather than overclaimed.

        The excerpt does not carry its own line numbers, so shifting BOTH
        endpoints by the same amount preserves the span and is NOT detectable
        from the snapshot alone. That is the same trust level a head snapshot
        already has - the canonical contract proves an excerpt is well formed,
        never that it matches the file. Post-approval the frozen bytes are
        still bound by the outbound digest.
        """
        shifted = snapshot_of("a\nb\nc\n", start_line=110, end_line=112)
        self.assertIsNone(local_read_snapshot_refusal(shifted))

    def test_reversed_and_out_of_range_spans_are_refused(self) -> None:
        for start, end in (
            (12, 10),
            (0, 3),
            (-1, 3),
            (LOCAL_READ_MAX_START_LINE + 1, LOCAL_READ_MAX_START_LINE + 3),
            (1, LOCAL_READ_MAX_REGION_LINES + 5),
        ):
            with self.subTest(start=start, end=end):
                self.assertIsNotNone(
                    local_read_snapshot_refusal(
                        snapshot_of("a\nb\nc\n", start_line=start, end_line=end)
                    )
                )

    def test_non_integer_spans_are_refused(self) -> None:
        for value in (True, 1.5, "3", None):
            with self.subTest(value=value):
                self.assertIsNotNone(
                    local_read_snapshot_refusal(
                        snapshot_of("a\n", start_line=value, end_line=1)
                    )
                )

    def test_a_tampered_span_does_not_survive_a_load(self) -> None:
        state = snapshot_of("a\nb\n", start_line=10, end_line=11).to_dict()
        state["end_line"] = 40
        self.assertFalse(LocalReadSnapshot.from_dict(state).present)

    def test_a_valid_region_snapshot_round_trips(self) -> None:
        original = snapshot_of("a\nb\n", start_line=10, end_line=11)
        restored = LocalReadSnapshot.from_dict(original.to_dict())
        self.assertEqual(original, restored)

    def test_a_legacy_snapshot_row_loads_with_the_head_span(self) -> None:
        state = snapshot_of("a\n").to_dict()
        state.pop("start_line")
        state.pop("end_line")
        restored = LocalReadSnapshot.from_dict(state)
        self.assertTrue(restored.present)
        self.assertEqual((0, 0), (restored.start_line, restored.end_line))


# ==========================================================================
# PATHOLOGY
# ==========================================================================
class PartialCapturePathologyTests(RegionReadBase):
    def test_a_partial_capture_that_redacts_small_still_refuses(self) -> None:
        """The defect the capture rule exists for.

        200 long absolute-path lines exceed the 64 KiB capture ceiling, but
        redaction collapses each to a short marker, so a PARTIAL capture would
        bound to well under the character cap and arrive with truncated=False.
        A consumer would then read "fewer lines than requested and nothing was
        cut" as EOF while the file continues. It must refuse instead.
        """
        line = "/" + "a" * 90 + "/" + "b" * 90 + "/" + "c" * 90 + "/" + "d" * 90 + ".t\n"
        self.assertGreater(
            len((line * LOCAL_READ_MAX_REGION_LINES).encode("utf-8")),
            LOCAL_READ_MAX_BYTES,
        )
        self.write("paths.txt", line * 260)
        result = self.read_region(
            self.root / "paths.txt", 1, LOCAL_READ_MAX_REGION_LINES
        )
        self.assert_refused(result, "did not close within")

    def test_the_partial_capture_would_have_looked_untruncated(self) -> None:
        """Proves the pathology is real, not hypothetical."""
        line = "/" + "a" * 90 + "/" + "b" * 90 + "/" + "c" * 90 + "/" + "d" * 90 + ".t\n"
        partial = line * (LOCAL_READ_MAX_BYTES // len(line))
        excerpt, truncated = bound_local_text(partial)
        self.assertGreater(len(partial.encode("utf-8")), LOCAL_READ_MAX_BYTES // 2)
        self.assertLess(len(excerpt), LOCAL_READ_EXCERPT_MAX_CHARS)
        self.assertFalse(truncated)


# ==========================================================================
# SECURITY
# ==========================================================================
class SecurityTests(RegionReadBase):
    def test_a_region_outside_the_allowed_roots_is_refused(self) -> None:
        outside = Path(tempfile.gettempdir()) / "m9-outside.txt"
        outside.write_text("a\nb\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.assert_refused(self.read_region(outside, 1, 2), "outside the allowed roots")

    def test_a_traversal_target_is_refused(self) -> None:
        self.assert_refused(
            self.read_region(self.root / ".." / "escape.txt", 1, 2),
            "outside the allowed roots",
        )

    def test_a_secret_file_is_refused_for_regions_too(self) -> None:
        self.write(".env", "SECRET=1\nMORE=2\n")
        self.assert_refused(
            self.read_region(self.root / ".env", 1, 2), "secret-file policy"
        )

    def test_an_alternate_data_stream_name_is_refused(self) -> None:
        self.assert_refused(self.read_region(self.root / "notes.txt:hidden", 1, 2))

    def test_a_directory_is_refused(self) -> None:
        (self.root / "sub").mkdir()
        self.assert_refused(
            self.read_region(self.root / "sub", 1, 2), "regular file"
        )

    def test_a_missing_file_is_refused(self) -> None:
        self.assert_refused(
            self.read_region(self.root / "nope.txt", 1, 2), "regular file"
        )

    def test_a_nul_byte_in_the_seek_prefix_is_refused(self) -> None:
        """The NUL check runs on everything SCANNED, not only what is kept."""
        self.write("binary.bin", b"a\n" * 100 + b"\x00" + b"\nb\n" * 100)
        self.assert_refused(
            self.read_region(self.root / "binary.bin", 150, 5), "not strict UTF-8"
        )

    def test_a_nul_byte_inside_the_region_is_refused(self) -> None:
        self.write("binary.bin", b"a\nb\n\x00\nc\n")
        self.assert_refused(
            self.read_region(self.root / "binary.bin", 1, 5), "not strict UTF-8"
        )

    def test_invalid_utf8_inside_the_region_is_refused(self) -> None:
        self.write("bad.bin", b"alpha\n\xff\xfe\nbeta\n")
        self.assert_refused(
            self.read_region(self.root / "bad.bin", 2, 1), "not strict UTF-8"
        )

    def test_a_hardlinked_file_is_refused(self) -> None:
        target = self.write("real.txt", numbered(10))
        link = self.root / "link.txt"
        try:
            os.link(target, link)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"hard links unavailable here: {exc}")
        self.assert_refused(self.read_region(link, 1, 5), "more than one filesystem name")

    def test_a_symlink_escaping_the_root_is_refused(self) -> None:
        outside = Path(tempfile.gettempdir()) / "m9-symlink-target.txt"
        outside.write_text(numbered(10), encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        link = self.root / "escape.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        # P2. The link is refused AS A LINK, by the no-follow chain scan,
        # rather than by resolving it first and finding the destination
        # outside the roots. Both refuse; only this order refuses without
        # having followed the link to learn where it went.
        self.assert_refused(
            self.read_region(link, 1, 5), "not an existing regular file"
        )

    def test_an_in_root_symlink_is_refused_rather_than_followed(self) -> None:
        """P2. The M3 contract this REPLACES: an in-root symlink used to be
        resolved first and read under its target's authoritative name.

        Canonicalization no longer dereferences anything, because doing so is
        an inspection of the filesystem aimed by a path no policy has accepted
        yet. So the link now reaches the no-follow chain scan as itself and is
        refused there. The real file stays readable under its own name - what
        is gone is the second, aliased route to it.
        """
        self.write("real.txt", numbered(10))
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.root / "real.txt")
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")
        self.assert_refused(
            self.read_region(link, 1, 5), "not an existing regular file"
        )
        snapshot = self.snapshot(self.read_region(self.root / "real.txt", 1, 5))
        self.assertEqual("real.txt", snapshot.relative_path)
        self.assertEqual((1, 5), (snapshot.start_line, snapshot.end_line))

    def test_the_region_reader_adds_no_new_capability(self) -> None:
        self.assertEqual(
            (LOCAL_READ_CAPABILITY,), self.connector.manifest.capabilities
        )


# ==========================================================================
# PHYSICAL BOUNDS - asserted at the DESCRIPTOR, not at a counter
# ==========================================================================
class PhysicalBoundTests(RegionReadBase):
    """P2-1 / P2-2. The declared ceilings must bound the FILE, not a variable.

    Both bugs these pin had the same shape: the logical accounting stayed
    inside its ceiling while the descriptor had already been read past it -
    once by asking for a whole chunk and auditing the ceiling afterwards, and
    once by letting a buffered reader prefetch behind a one-byte read.
    """

    def test_the_seek_never_reads_past_the_scan_ceiling(self) -> None:
        """P2-1. Over 4 MiB of seeking, measured on the descriptor."""
        self.write("huge.txt", numbered(81920, 64))
        self.assertEqual(
            5 * 1024 * 1024, (self.root / "huge.txt").stat().st_size
        )
        with descriptor_offsets() as offsets:
            result = self.read_region(self.root / "huge.txt", 81000, 5)
        self.assert_refused(result, "scan ceiling")
        self.assertTrue(offsets)
        self.assertLessEqual(max(offsets), LOCAL_READ_REGION_SCAN_MAX_BYTES)

    def test_the_seek_bound_holds_under_a_lowered_ceiling_too(self) -> None:
        """The same invariant where the chunk does not divide the ceiling."""
        self.write("notes.txt", numbered(4000))
        with mock.patch.object(local_files, "_REGION_CHUNK_BYTES", 300):
            with mock.patch.object(
                local_files, "LOCAL_READ_REGION_SCAN_MAX_BYTES", 2000
            ):
                with descriptor_offsets() as offsets:
                    result = self.read_region(self.root / "notes.txt", 3000, 5)
        self.assert_refused(result, "scan ceiling")
        self.assertLessEqual(max(offsets), 2000)

    def test_the_capture_never_reads_past_the_sentinel(self) -> None:
        """P2-2. The +1 sentinel is ONE byte at the descriptor, not a block.

        The tail matters: a buffered reader only shows itself when there is
        something left to prefetch, so the still-open region continues well
        past the ceiling and the sentinel read has a whole block available
        behind it.
        """
        self.write("big.txt", CEILING_LINE * CEILING_LINES + "y" * 100_000)
        with descriptor_offsets() as offsets:
            result = self.read_region(
                self.root / "big.txt", 1, LOCAL_READ_MAX_REGION_LINES
            )
        self.assert_refused(result, "did not close within")
        self.assertEqual(LOCAL_READ_MAX_BYTES + 1, max(offsets))

    def test_a_capture_closing_at_the_ceiling_reads_no_sentinel_block(self) -> None:
        self.write("big.txt", CEILING_LINE * CEILING_LINES)
        with descriptor_offsets() as offsets:
            snapshot = self.snapshot(
                self.read_region(self.root / "big.txt", 1, CEILING_LINES)
            )
        self.assertEqual(LOCAL_READ_MAX_BYTES, snapshot.byte_size)
        self.assertLessEqual(max(offsets), LOCAL_READ_MAX_BYTES + 1)

    def test_the_sentinel_byte_never_reaches_the_snapshot(self) -> None:
        """The one byte past the ceiling exists only to prove a refusal."""
        content = CEILING_LINE * (CEILING_LINES - 1) + "z" * 511 + "\n"
        self.write("big.txt", content)
        snapshot = self.snapshot(
            self.read_region(self.root / "big.txt", 1, CEILING_LINES)
        )
        self.assertEqual(LOCAL_READ_MAX_BYTES, snapshot.byte_size)
        self.assertEqual(
            hashlib.sha256(content.encode("utf-8")).hexdigest(), snapshot.sha256
        )

    def test_both_bounds_hold_together_for_a_late_region(self) -> None:
        """A real M9 read: seek inside 4 MiB, capture inside 64 KiB + 1."""
        self.write("huge.txt", numbered(20000))
        with descriptor_offsets() as offsets:
            snapshot = self.snapshot(
                self.read_region(self.root / "huge.txt", 15000, 5)
            )
        self.assertEqual((15000, 15004), (snapshot.start_line, snapshot.end_line))
        self.assertLessEqual(
            max(offsets),
            LOCAL_READ_REGION_SCAN_MAX_BYTES + LOCAL_READ_MAX_BYTES + 1,
        )


# ==========================================================================
# THE NUL WINDOW - only what the read actually consumes
# ==========================================================================
class NulWindowTests(RegionReadBase):
    """P2-3. A NUL refuses the read only when the read actually touched it.

    All four cases below live in the SAME fetched chunk, which is the point:
    the rule cannot be "what was fetched", it has to be "what was consumed" -
    the prefix traversed while seeking, and the region up to its closing
    newline. Anything after that boundary is not part of this read.
    """

    def test_a_nul_before_the_region_boundary_refuses(self) -> None:
        self.write("mixed.bin", b"a\x00b\nwanted\ntail\n")
        self.assert_refused(
            self.read_region(self.root / "mixed.bin", 2, 1), "not strict UTF-8"
        )

    def test_a_nul_inside_the_requested_region_refuses(self) -> None:
        self.write("mixed.bin", b"wanted\nbad\x00here\ntail\n")
        self.assert_refused(
            self.read_region(self.root / "mixed.bin", 1, 2), "not strict UTF-8"
        )

    def test_a_nul_after_the_closing_newline_succeeds(self) -> None:
        """The false refusal this finding is about."""
        self.write("mixed.bin", b"wanted\ntrailing\x00data\n")
        snapshot = self.snapshot(self.read_region(self.root / "mixed.bin", 1, 1))
        self.assertEqual("wanted\n", snapshot.excerpt)
        self.assertEqual((1, 1), (snapshot.start_line, snapshot.end_line))
        self.assertEqual(7, snapshot.byte_size)

    def test_a_nul_after_a_closed_late_region_succeeds(self) -> None:
        """Same rule when the region closes after a real seek."""
        self.write("mixed.bin", b"a\nb\nc\nwanted\ngarbage\x00\x00\x00\n")
        snapshot = self.snapshot(self.read_region(self.root / "mixed.bin", 4, 1))
        self.assertEqual("wanted\n", snapshot.excerpt)

    def test_trailing_bytes_after_closure_are_never_read(self) -> None:
        """Closure ends the read: the tail is neither fetched nor judged."""
        tail = b"\x00" * (LOCAL_READ_MAX_BYTES * 4)
        self.write("mixed.bin", b"wanted\n" + tail)
        with descriptor_offsets() as offsets:
            snapshot = self.snapshot(self.read_region(self.root / "mixed.bin", 1, 1))
        self.assertEqual("wanted\n", snapshot.excerpt)
        # One bounded chunk was fetched and the region closed inside it; no
        # read ever went looking at the rest of the file.
        self.assertEqual(1, len(offsets))
        self.assertLessEqual(max(offsets), local_files._REGION_CHUNK_BYTES)

    def test_a_nul_in_the_seek_prefix_of_the_same_chunk_still_refuses(self) -> None:
        """The seek check is not weakened - only narrowed to what it traversed."""
        self.write("mixed.bin", b"a\nb\x00c\nd\nwanted\n")
        self.assert_refused(
            self.read_region(self.root / "mixed.bin", 4, 1), "not strict UTF-8"
        )


# ==========================================================================
# COMPOSITION (M3 / M8) AND THE CLAUDE PAYLOAD
# ==========================================================================
class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.database = str(self.root / "jarvis.db")
        self.settings = Settings(
            memory_database=self.database, permission_mode=PermissionMode.SMART_AUTO
        )
        self.memory = SQLiteMemoryStore(self.database)
        self.planner = PlanningConnector()
        self.reader = LocalFileReadConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        self.app = open_app(
            settings=self.settings,
            memory=self.memory,
            connectors=(self.planner, self.reader),
            destination_policy=frozenset(
                {
                    DestinationGrant(
                        "test-planner", "code_analysis", CLAUDE_CODE_DESTINATION
                    ),
                    DestinationGrant(
                        "test-planner", "code_planning", CLAUDE_CODE_DESTINATION
                    ),
                    DestinationGrant(
                        "local-file-read", "file_read", CLAUDE_CODE_DESTINATION
                    ),
                }
            ),
        )
        self.session_id: str | None = None

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def completed_region(self, name: str, start: int, count: int) -> str:
        self.write(name, numbered(400))
        document = json.dumps(
            {
                "objective": f"read a region of {name}",
                "capability": LOCAL_READ_CAPABILITY,
                "scope": {"action": "read", "target": str(self.root / name)},
                "context": {"working_directory": str(self.root)},
                LOCAL_READ_REGION_SECTION: {
                    "start_line": start,
                    "max_lines": count,
                },
            }
        )
        payload = self.app.run(document, session_id=self.session_id).payload
        self.session_id = payload["session"]["id"]
        self.assertEqual("completed", payload["task"]["status"], payload.get("error"))
        return payload["task"]["id"]

    def completed_head(self, name: str) -> str:
        """The same loop with NO read section: the legacy head read."""
        self.write(name, numbered(20))
        document = json.dumps(
            {
                "objective": f"read the head of {name}",
                "capability": LOCAL_READ_CAPABILITY,
                "scope": {"action": "read", "target": str(self.root / name)},
                "context": {"working_directory": str(self.root)},
            }
        )
        payload = self.app.run(document, session_id=self.session_id).payload
        self.session_id = payload["session"]["id"]
        self.assertEqual("completed", payload["task"]["status"], payload.get("error"))
        return payload["task"]["id"]

    def test_a_region_read_completes_through_the_whole_loop(self) -> None:
        task_id = self.completed_region("a.txt", 100, 10)
        stored = self.memory.load_task(task_id)
        self.assertIs(TaskStatus.COMPLETED, stored.status)
        self.assertIs(LocalReadKind.REGION, stored.local_read_kind)
        self.assertEqual((100, 109), (stored.local_read.start_line, stored.local_read.end_line))

    def test_m3_carries_a_region_snapshot_unchanged(self) -> None:
        source = self.completed_region("a.txt", 100, 10)
        document = json.dumps(
            {
                "objective": "analyse the region",
                "capability": "code_analysis",
                "scope": {"action": "analyse", "target": "repo"},
                "context": {
                    "working_directory": str(self.root),
                    LOCAL_READ_TASK_REFERENCE_KEY: source,
                },
            }
        )
        payload = self.app.run(document, session_id=self.session_id).payload
        carried = self.memory.load_task(payload["task"]["id"]).local_read
        stored = self.memory.load_task(source).local_read
        self.assertTrue(carried.present)
        self.assertEqual((100, 109), (carried.start_line, carried.end_line))
        self.assertEqual(stored.excerpt, carried.excerpt)

    def test_m8_freezes_multiple_region_sources_in_order(self) -> None:
        first = self.completed_region("a.txt", 100, 5)
        second = self.completed_region("b.txt", 200, 5)
        document = json.dumps(
            {
                "objective": "analyse two regions",
                "capability": "code_analysis",
                "scope": {"action": "analyse", "target": "repo"},
                "context": {
                    "working_directory": str(self.root),
                    "local_read_task_ids": [first, second],
                },
            }
        )
        payload = self.app.run(document, session_id=self.session_id).payload
        task = self.memory.load_task(payload["task"]["id"])
        self.assertIs(LocalReadContextKind.REFERENCES, task.local_read_context_kind)
        self.assertEqual(
            [(100, 104), (200, 204)],
            [(item.start_line, item.end_line) for item in task.local_read_sources],
        )
        approval = payload["approval"]
        final = self.app.approve(
            session_id=payload["session"]["id"],
            task_id=payload["task"]["id"],
            request_id=approval["request_id"],
            fingerprint=approval["fingerprint"],
        ).payload
        self.assertEqual("completed", final["task"]["status"])
        self.assertEqual([("a.txt", "b.txt")], self.planner.seen)

    def test_no_source_is_re_read_after_the_freeze(self) -> None:
        first = self.completed_region("a.txt", 100, 5)
        second = self.completed_region("b.txt", 200, 5)
        document = json.dumps(
            {
                "objective": "analyse two regions",
                "capability": "code_analysis",
                "scope": {"action": "analyse", "target": "repo"},
                "context": {
                    "working_directory": str(self.root),
                    "local_read_task_ids": [first, second],
                },
            }
        )
        payload = self.app.run(document, session_id=self.session_id).payload
        with mock.patch.object(
            Path, "read_bytes", side_effect=AssertionError("filesystem re-read")
        ):
            approval = payload["approval"]
            final = self.app.approve(
                session_id=payload["session"]["id"],
                task_id=payload["task"]["id"],
                request_id=approval["request_id"],
                fingerprint=approval["fingerprint"],
            ).payload
        self.assertEqual("completed", final["task"]["status"])

    def test_the_envelope_exposes_the_span_without_a_new_key(self) -> None:
        """P2-4. The public envelope must say WHICH lines the excerpt is."""
        task_id = self.completed_region("a.txt", 100, 10)
        payload = self.app.resume(
            session_id=self.session_id, task_id=task_id
        ).payload
        self.assertEqual(6, payload["schema_version"])
        self.assertEqual(OUTPUT_SCHEMA_VERSION, payload["schema_version"])
        # The span rides inside the existing local_read block, nowhere else.
        self.assertNotIn("local_region", payload)
        self.assertNotIn("region", payload)
        block = payload["local_read"]
        self.assertEqual(REGION_ENVELOPE_KEYS, sorted(block))
        self.assertEqual(100, block["start_line"])
        self.assertEqual(109, block["end_line"])
        # And the span really describes the excerpt that was rendered.
        self.assertTrue(block["excerpt"].startswith("L000100"))
        self.assertIn("L000109", block["excerpt"])
        self.assertNotIn("L000110", block["excerpt"])

    def test_a_head_envelope_keeps_its_exact_existing_shape(self) -> None:
        """The HEAD envelope is byte-for-byte the shape it always had."""
        task_id = self.completed_head("h.txt")
        payload = self.app.resume(
            session_id=self.session_id, task_id=task_id
        ).payload
        block = payload["local_read"]
        self.assertEqual(HEAD_ENVELOPE_KEYS, sorted(block))
        self.assertNotIn("start_line", block)
        self.assertNotIn("end_line", block)

    def test_a_malformed_span_is_never_surfaced_as_a_trusted_payload(self) -> None:
        """A span that disagrees with its excerpt renders NO block at all -
        not a block with the span quietly dropped."""
        good = snapshot_of("a\nb\n", start_line=1900, end_line=1901)
        self.assertEqual(
            (1900, 1901),
            (
                _local_read_payload(good)["start_line"],
                _local_read_payload(good)["end_line"],
            ),
        )
        for bad in (
            snapshot_of("a\nb\n", start_line=1900, end_line=1999),
            snapshot_of("a\nb\n", start_line=1901, end_line=1900),
            snapshot_of("a\nb\n", start_line=0, end_line=1901),
            snapshot_of("a\nb\n", start_line=1900, end_line=0),
        ):
            self.assertIsNone(_local_read_payload(bad), msg=str(bad))


class ClaudePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name).resolve()
        self.connector = ClaudeCodeConnector(allowed_roots=[self.root])

    def task_with(self, **kwargs) -> Task:
        return Task(
            objective="analyse",
            capability="code_analysis",
            scope=ActionScope(
                action="analyse", data_classification=DataClassification.INTERNAL
            ),
            context={"working_directory": str(self.root)},
            **kwargs,
        )

    def test_a_head_snapshot_payload_is_unchanged(self) -> None:
        task = self.task_with(local_read=snapshot_of("alpha\n"))
        payload, _ = self.connector._sanitized_payload(task)
        self.assertEqual(
            ["byte_size", "content", "notice", "path", "truncated"],
            sorted(payload["local_read"]),
        )

    def test_a_region_snapshot_payload_carries_the_span(self) -> None:
        task = self.task_with(
            local_read=snapshot_of("a\nb\n", start_line=1900, end_line=1901)
        )
        payload, _ = self.connector._sanitized_payload(task)
        block = payload["local_read"]
        self.assertEqual(
            ["byte_size", "content", "end_line", "notice", "path", "start_line", "truncated"],
            sorted(block),
        )
        self.assertEqual((1900, 1901), (block["start_line"], block["end_line"]))

    def test_m8_head_sources_keep_their_exact_shape(self) -> None:
        task = self.task_with(
            local_read_context_kind=LocalReadContextKind.REFERENCES,
            local_read_source_task_ids=("A",),
            local_read_sources=(snapshot_of("alpha\n"),),
        )
        payload, _ = self.connector._sanitized_payload(task)
        self.assertEqual(
            ["byte_size", "content", "path", "truncated"],
            sorted(payload["local_read_context"]["sources"][0]),
        )

    def test_m8_region_sources_carry_the_span(self) -> None:
        task = self.task_with(
            local_read_context_kind=LocalReadContextKind.REFERENCES,
            local_read_source_task_ids=("A", "B"),
            local_read_sources=(
                snapshot_of("alpha\n"),
                snapshot_of("b\nc\n", start_line=1900, end_line=1901),
            ),
        )
        payload, _ = self.connector._sanitized_payload(task)
        sources = payload["local_read_context"]["sources"]
        self.assertNotIn("start_line", sources[0])
        self.assertEqual((1900, 1901), (sources[1]["start_line"], sources[1]["end_line"]))

    def test_no_external_tool_grants_exist(self) -> None:
        self.assertEqual((), EGRESS_TOOL_GRANTS)


# ==========================================================================
# COMPATIBILITY
# ==========================================================================
class HeadCompatibilityTests(RegionReadBase):
    def test_a_head_read_is_unchanged(self) -> None:
        self.write("notes.txt", "alpha\nbeta\n")
        snapshot = self.snapshot(self.run_task(self.head_task(self.root / "notes.txt")))
        self.assertEqual((0, 0), (snapshot.start_line, snapshot.end_line))
        self.assertEqual("alpha\nbeta\n", snapshot.excerpt)
        self.assertEqual(11, snapshot.byte_size)

    def test_the_head_ceiling_still_refuses_without_truncating(self) -> None:
        self.write("big.txt", "a" * (LOCAL_READ_MAX_BYTES + 1))
        result = self.run_task(self.head_task(self.root / "big.txt"))
        self.assert_refused(result, "refused rather than silently truncated")

    def test_the_head_ceiling_boundary_is_exact(self) -> None:
        self.write("exact.txt", "a" * LOCAL_READ_MAX_BYTES)
        snapshot = self.snapshot(self.run_task(self.head_task(self.root / "exact.txt")))
        self.assertEqual(LOCAL_READ_MAX_BYTES, snapshot.byte_size)

    def test_head_truncation_shape_is_unchanged(self) -> None:
        self.write("over.txt", "x" * (LOCAL_READ_EXCERPT_MAX_CHARS + 500))
        snapshot = self.snapshot(self.run_task(self.head_task(self.root / "over.txt")))
        self.assertTrue(snapshot.truncated)
        self.assertEqual(LOCAL_READ_EXCERPT_MAX_CHARS, len(snapshot.excerpt))
        self.assertEqual(1, snapshot.excerpt.count(ELLIPSIS))
        self.assertEqual((0, 0), (snapshot.start_line, snapshot.end_line))

    def test_the_head_sha256_still_digests_the_whole_file(self) -> None:
        content = "alpha\nbeta\n"
        self.write("notes.txt", content)
        snapshot = self.snapshot(self.run_task(self.head_task(self.root / "notes.txt")))
        self.assertEqual(
            hashlib.sha256(content.encode("utf-8")).hexdigest(), snapshot.sha256
        )

    def test_a_region_sha256_digests_the_captured_region(self) -> None:
        self.write("notes.txt", numbered(50))
        snapshot = self.snapshot(self.read_region(self.root / "notes.txt", 3, 2))
        expected = "L000003\nL000004\n"
        self.assertEqual(expected, snapshot.excerpt)
        self.assertEqual(
            hashlib.sha256(expected.encode("utf-8")).hexdigest(), snapshot.sha256
        )
        self.assertEqual(len(expected.encode("utf-8")), snapshot.byte_size)

    def test_verify_reports_the_span_only_for_regions(self) -> None:
        self.write("notes.txt", numbered(50))
        region = self.region_task(self.root / "notes.txt", 3, 2)
        result = self.run_task(region)
        region.local_read = self.snapshot(result)
        record = self.connector.verify(region, result, SessionState())
        self.assertIn("lines=3-4", record.evidence)

        head = self.head_task(self.root / "notes.txt")
        head_result = self.run_task(head)
        head.local_read = self.snapshot(head_result)
        head_record = self.connector.verify(head, head_result, SessionState())
        self.assertFalse(any(item.startswith("lines=") for item in head_record.evidence))

    def test_the_schemas_are_unchanged(self) -> None:
        from jarvis.memory import SCHEMA_VERSION

        # Memory V2 owns the SQLite schema (7); the output envelope
        # contract is untouched by it and remains at version 6.
        self.assertEqual(7, SCHEMA_VERSION)
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
