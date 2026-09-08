"""M6 - the typed, bounded model answer.

What this file pins, end to end:

    external model text -> canonical bounded record -> AgentCore lift BEFORE
    the H4 diagnostic pipeline -> durable typed task field -> integrity
    verification -> public envelope block

plus the isolation that makes it safe to show: the answer is untrusted
inbound display data, it never reaches an approval, a fingerprint, a billing
decision, a plan, a connector choice, project memory or retrieval, and
nothing feeds it back to the model.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from jarvis.app import (
    OUTPUT_SCHEMA_VERSION,
    build_envelope,
    create_app,
    decode_task_json,
)
from jarvis.connectors import claude_code
from jarvis.connectors.claude_code import ClaudeCodeConnector
from jarvis.contracts import (
    DIAGNOSTIC_MESSAGE_MAX_CHARS,
    MODEL_ANSWER_MAX_CHARS,
    MODEL_ANSWER_RESULT_KEY,
    MODEL_ANSWER_SOURCE_MAX_CHARS,
    ActionScope,
    DataClassification,
    LocalReadSnapshot,
    ModelAnswer,
    OutcomeStatus,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    RunStatus,
    SessionState,
    SideEffect,
    SideEffectProfile,
    Task,
    TaskStatus,
    ToolResult,
    UsageRecord,
    VerificationStatus,
    bound_model_text,
    build_model_answer,
    model_answer_refusal,
    normalize_model_text,
    redact_credentials,
    utc_now,
)
from jarvis.destinations import (
    CLAUDE_CODE_DESTINATION,
    DestinationGrant,
)
from jarvis.memory import SQLiteMemoryStore
from jarvis.planning import build_task_plan
from jarvis.settings import Settings
from tests.helpers import DUMMY_CAPABILITY, DummyConnector, make_task, open_app, open_core, safe_cleanup

#: A marker inside the first 240 characters: it survives BOTH representations,
#: so it proves the bounded summary path still works.
HEAD_MARKER = "HEAD-MARKER-4c81"
#: A marker deliberately pushed past the diagnostic ceiling: it can only ever
#: appear through the typed answer, never through a summary or a decision.
DEEP_MARKER = "DEEP-MARKER-9f27"
#: An absolute local path. The H4 diagnostic pipeline destroys everything from
#: here to the end of the field; the deliverable must keep it verbatim.
WINDOWS_PATH = "C:\\Users\\realk\\project\\core.py:1103"

STRUCTURED_ANSWER = (
    f"# {HEAD_MARKER} analysis\n"
    "\n"
    "The loop has these stages:\n"
    "\n"
    "    1. understand\n"
    "    2. context_retrieval\n"
    "\n"
    "\n"
    f"See {WINDOWS_PATH} for the sanitize call, and note that this sentence "
    "runs well past the two hundred and forty character diagnostic ceiling so "
    "that the deliverable and the summary can never be confused for one "
    f"another. {DEEP_MARKER}\n"
)


class AnsweringConnector(DummyConnector):
    """A minimal, provider-neutral external-model connector.

    Its MANIFEST declares ``EXTERNAL_EGRESS``, which is the producer gate, and
    it hands back a typed answer under the one transport key. ``DummyConnector``
    already implements ``canonical_outbound``, so this is a proper attester and
    no part of the egress contract has to be relaxed to run these tests.
    """

    def __init__(self, answer_text: str = STRUCTURED_ANSWER, **kwargs: Any) -> None:
        kwargs.setdefault("name", "answering")
        kwargs.setdefault("side_effect", SideEffect.EXTERNAL_EGRESS)
        super().__init__(**kwargs)
        self.answer_text = answer_text
        #: Set to hand back something other than a canonical typed answer.
        self.raw_override: Any = None
        self.override_active = False

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        result = super().execute(task, session)
        if result.outcome is not OutcomeStatus.SUCCESS:
            return result
        result.summary = self.answer_text
        payload = (
            self.raw_override
            if self.override_active
            else build_model_answer(self.answer_text)
        )
        result.data = {
            "next_step": "continue testing",
            # Provider-internal tags a real connector also emits. They must
            # be dropped by ``_sanitize_result`` and never be persisted.
            "response_type": "result",
            "stop_reason": "end_turn",
            "api_error_status": None,
            MODEL_ANSWER_RESULT_KEY: payload,
        }
        return result


class ModelAnswerTestCase(unittest.TestCase):
    """One temp store, one answering connector, the production composition."""

    permission_mode = PermissionMode.SESSION

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(safe_cleanup, self._temp)
        self.root = Path(self._temp.name).resolve()
        self.database = self.root / "memory.db"
        self.settings = Settings(
            memory_database=self.database, permission_mode=self.permission_mode
        )
        self.connector = AnsweringConnector()
        self.app = open_app(
            settings=self.settings,
            connectors=(self.connector,),
            destination_policy=frozenset(
                {
                    DestinationGrant("answering", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION),
                }
            ),
        )
        self.core = self.app.core
        self.memory = self.app.memory

    # -- helpers ------------------------------------------------------------

    def egress_task(self, objective: str = "analyze the loop") -> Task:
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.objective = objective
        task.capability = "echo"
        return task

    def completed(self, task: Task | None = None):
        """One full approved external-model run; returns (task, run)."""
        task = task or self.egress_task()
        run = self.core.run(task)
        self.assertEqual(RunStatus.AWAITING_APPROVAL, run.status, msg=str(run.error))
        grant = PermissionGrant.for_request(run.permission_request)
        run = self.core.resume(run.session.id, task.id, grant=grant)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        return self.memory.load_task(task.id), run

    def stored(self, task_id: str) -> Task:
        """Reload through a FRESH store, so nothing in memory can help."""
        return SQLiteMemoryStore(self.database).load_task(task_id)

    def raw_state(self, task_id: str) -> str:
        """The persisted task row exactly as SQLite holds it."""
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
        finally:
            connection.close()


# --------------------------------------------------------------------------
# 1. The product deliverable
# --------------------------------------------------------------------------


class DeliverableTests(ModelAnswerTestCase):
    """The whole reason M6 exists: the answer arrives usable."""

    def test_the_answer_survives_the_diagnostic_pipeline(self) -> None:
        """The one test that fails if the lift moves after ``_sanitize_result``.

        Everything the H4 pipeline does to a diagnostic - collapse newlines,
        redact absolute paths, cut at 240 characters - is exactly wrong for a
        deliverable. All three are asserted absent from the typed answer and
        present in the summary, so the two representations cannot silently
        become one.
        """
        task, _ = self.completed()
        answer = task.model_answer
        self.assertTrue(answer.present)
        self.assertEqual(STRUCTURED_ANSWER, answer.content)
        self.assertIn(DEEP_MARKER, answer.content)
        self.assertIn(WINDOWS_PATH, answer.content)
        self.assertGreater(len(answer.content), DIAGNOSTIC_MESSAGE_MAX_CHARS)
        self.assertIn("\n", answer.content)

    def test_multiline_structure_indentation_and_blank_lines_survive(self) -> None:
        task, _ = self.completed()
        lines = task.model_answer.content.split("\n")
        self.assertIn("    1. understand", lines)
        self.assertIn("", lines, "blank lines were collapsed")
        self.assertTrue(
            task.model_answer.content.endswith("\n"), "the trailing newline was stripped"
        )
        self.assertGreater(len(lines), 5)

    def test_a_windows_absolute_path_is_preserved_verbatim(self) -> None:
        """P: paths are the answer here, not accidental diagnostic leakage."""
        task, _ = self.completed()
        self.assertIn(WINDOWS_PATH, task.model_answer.content)
        self.assertNotIn("[PATH]", task.model_answer.content)

    def test_the_result_summary_is_still_bounded_diagnostic_text(self) -> None:
        """H4 is untouched: the summary keeps every one of its bounds."""
        task, run = self.completed()
        summary = run.result.summary
        self.assertLessEqual(len(summary), DIAGNOSTIC_MESSAGE_MAX_CHARS)
        self.assertNotIn("\n", summary)
        self.assertNotIn(WINDOWS_PATH, summary)
        self.assertIn("[PATH]", summary)
        self.assertNotIn(DEEP_MARKER, summary)

    def test_both_representations_coexist_and_differ(self) -> None:
        task, run = self.completed()
        self.assertIn(HEAD_MARKER, run.result.summary)
        self.assertIn(HEAD_MARKER, task.model_answer.content)
        self.assertNotEqual(run.result.summary, task.model_answer.content)


# --------------------------------------------------------------------------
# 2. Canonical bounding
# --------------------------------------------------------------------------


class BoundingTests(unittest.TestCase):
    def test_newlines_are_normalized_and_nothing_else_is(self) -> None:
        raw = "  keep indent\r\n\r\nkeep blank\rand\ttab \n"
        self.assertEqual(
            "  keep indent\n\nkeep blank\nand\ttab \n", normalize_model_text(raw)
        )

    def test_no_nfkc_no_strip_no_whitespace_collapse(self) -> None:
        raw = "  \uff21\uff22   spaced   \n"
        bounded, truncated = bound_model_text(raw)
        self.assertEqual(raw, bounded)
        self.assertFalse(truncated)

    def test_the_cap_is_its_own_policy_surface(self) -> None:
        from jarvis.contracts import LOCAL_READ_EXCERPT_MAX_CHARS

        self.assertEqual(8_000, MODEL_ANSWER_MAX_CHARS)
        self.assertEqual(64_000, MODEL_ANSWER_SOURCE_MAX_CHARS)
        self.assertNotEqual(MODEL_ANSWER_MAX_CHARS, LOCAL_READ_EXCERPT_MAX_CHARS)

    def test_an_oversized_answer_is_bounded_and_flagged(self) -> None:
        bounded, truncated = bound_model_text("x" * (MODEL_ANSWER_MAX_CHARS + 500))
        self.assertEqual(MODEL_ANSWER_MAX_CHARS, len(bounded))
        self.assertTrue(truncated)

    def test_exactly_at_the_cap_is_not_truncated(self) -> None:
        bounded, truncated = bound_model_text("y" * MODEL_ANSWER_MAX_CHARS)
        self.assertEqual(MODEL_ANSWER_MAX_CHARS, len(bounded))
        self.assertFalse(truncated)

    def test_the_source_window_bounds_the_text_before_redaction(self) -> None:
        """Redaction never runs over unbounded input.

        The output alone cannot show this - the final cap yields the same
        8000 characters either way - so the assertion is on the WORK: no
        redaction pass may ever be handed more than the source window. That
        is the whole and only thing this control buys.
        """
        from jarvis import contracts as contracts_module

        seen: list[int] = []
        real = contracts_module.redact_credentials

        def watched(text, *args, **kwargs):
            seen.append(len(text))
            return real(text, *args, **kwargs)

        # Newline-bearing, because the window boundary is now cut at the last
        # complete line; a newline-free blob is the separate fail-closed case.
        huge = ("z" * 79 + "\n") * ((MODEL_ANSWER_SOURCE_MAX_CHARS * 3) // 80)
        with mock.patch.object(contracts_module, "redact_credentials", watched):
            bounded, truncated = bound_model_text(huge)
        self.assertEqual(MODEL_ANSWER_MAX_CHARS, len(bounded))
        self.assertTrue(truncated)
        self.assertTrue(seen, "redaction never ran")
        self.assertLessEqual(
            max(seen),
            MODEL_ANSWER_SOURCE_MAX_CHARS,
            "redaction was handed more than the source window",
        )

    def test_truncation_inserts_no_in_band_marker(self) -> None:
        """A character the model never wrote must not enter copyable text."""
        bounded, _ = bound_model_text("q" * (MODEL_ANSWER_MAX_CHARS + 10))
        self.assertEqual("q" * MODEL_ANSWER_MAX_CHARS, bounded)
        self.assertNotIn("\u2026", bounded)
        self.assertNotIn("...", bounded)

    def test_a_credential_past_the_cap_is_still_removed(self) -> None:
        """Redaction runs BEFORE truncation, so distance cannot save a secret."""
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        raw = ("a" * (MODEL_ANSWER_MAX_CHARS + 200)) + " " + secret
        bounded, truncated = bound_model_text(raw)
        self.assertTrue(truncated)
        self.assertNotIn(secret, bounded)
        self.assertNotIn(secret, build_model_answer(raw).content)

    def test_a_credential_straddling_the_cap_leaves_no_fragment(self) -> None:
        """The ORDER is the control, and this is where it is observable.

        A credential placed across the 8000-character boundary is the case
        that distinguishes the two orders. Redacting first removes the whole
        token before anything is cut. Truncating first slices it, and the
        surviving head is too short to match the credential shape any more -
        so the second pass cannot rescue it and a live fragment is persisted
        and displayed. Everything else about the two orders looks identical,
        which is exactly why this case has to be pinned explicitly.
        """
        secret = "sk-" + "a" * 40
        raw = "x" * (MODEL_ANSWER_MAX_CHARS - 11) + " " + secret + "\n"
        bounded, truncated = bound_model_text(raw)
        self.assertTrue(truncated)
        self.assertNotIn("sk-", bounded, "a credential fragment survived the cut")
        self.assertIn("[REDACTED]", bounded)

    def test_a_credential_inside_the_window_is_redacted(self) -> None:
        raw = f"before api_key: hunter2super\nafter {DEEP_MARKER}\n"
        bounded, _ = bound_model_text(raw)
        self.assertNotIn("hunter2super", bounded)
        self.assertIn("[REDACTED]", bounded)
        self.assertIn(DEEP_MARKER, bounded, "redaction must not eat the rest")

    def test_redaction_that_expands_the_text_cannot_exceed_the_cap(self) -> None:
        """Truncation is last, so an expanding replacement cannot overflow."""
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        raw = " ".join([secret] * 900)
        bounded, _ = bound_model_text(raw)
        self.assertLessEqual(len(bounded), MODEL_ANSWER_MAX_CHARS)
        self.assertNotIn(secret, bounded)

    def test_bound_model_text_is_a_fixed_point(self) -> None:
        corpus = (
            STRUCTURED_ANSWER,
            "",
            "\r\n\r\n",
            "x" * (MODEL_ANSWER_MAX_CHARS + 7),
            "sk-abcdefghijklmnopqrstuvwxyz012345 " * 400,
            "token: " + ("t" * 9_000),
            f"{WINDOWS_PATH}\n" * 300,
        )
        for raw in corpus:
            once, _ = bound_model_text(raw)
            twice, again = bound_model_text(once)
            self.assertEqual(once, twice, f"not a fixed point: {raw[:40]!r}")
            self.assertFalse(again, f"a canonical value re-truncated: {raw[:40]!r}")

    def test_a_non_string_never_becomes_an_answer(self) -> None:
        for value in (None, 7, b"bytes", ["a"], {"a": 1}, object()):
            self.assertFalse(build_model_answer(value).present)

    def test_the_canonical_pipeline_itself_refuses_a_non_string(self) -> None:
        """The exact-str rule is enforced end to end, not at one caller.

        ``build_model_answer`` gates the type, but ``bound_model_text`` is the
        canonical pipeline and must not quietly coerce either - a ``str()``
        here would hand a provider's list or dict to the redaction passes and
        then present the result as something a model wrote.
        """
        for value in (None, 7, b"bytes", ["a"], {"a": 1}, object()):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TypeError):
                    bound_model_text(value)


# --------------------------------------------------------------------------
# 2b. The source-window boundary (P2-B)
# --------------------------------------------------------------------------


class SourceWindowBoundaryTests(unittest.TestCase):
    """No fragment created by the source-window cut may reach the deliverable.

    The window cut is the one cut in ``bound_model_text`` that precedes a
    redaction pass, so a credential straddling it would be reduced to a prefix
    too short for its own pattern to recognise - and a prefix is never
    redacted, because there is nothing left to match. Heavy redaction earlier
    in the window can then compress the retained text far enough that the
    fragment lands inside the 8000-character deliverable.
    """

    #: Representative instances of every recognizer in the SHARED credential
    #: policy, as (label, credential text, the bytes that must never survive).
    #: ``test_every_form_is_actually_recognized`` re-proves the premise from
    #: ``redact_credentials`` itself, so a policy change cannot leave these
    #: quietly testing nothing.
    FORMS = (
        ("sk- token", "sk-" + "S" * 48, "S" * 48),
        ("Bearer token", "Bearer " + "B" * 48, "B" * 48),
        (
            "JWT",
            "H" * 40 + "." + "P" * 30 + "." + "G" * 30,
            "G" * 30,
        ),
        ("AWS key id", "AKIA" + "K" * 16, "K" * 16),
        ("labelled secret", "api_key: " + "V" * 48, "V" * 48),
        ("labelled equals", "password=" + "W" * 48, "W" * 48),
        ("cli flag", "--token " + "C" * 48, "C" * 48),
        ("cli quoted", '--password "' + "Q" * 48 + '"', "Q" * 48),
    )

    #: One heavily compressing line: 501 source characters collapse to
    #: ``[REDACTED]`` plus its newline, i.e. 11. That compression is what
    #: makes the exploit reachable at all.
    COMPRESSING_LINE = "Bearer " + "B" * 493 + "\n"

    def test_every_form_is_actually_recognized(self) -> None:
        """Premise check: the shared policy really redacts each form."""
        for name, credential, secret in self.FORMS:
            with self.subTest(form=name):
                redacted, _ = redact_credentials(" " + credential + " ")
                self.assertNotIn(
                    secret, redacted, f"{name} is not recognized by the policy"
                )

    def exploit(self, credential: str) -> str:
        """Credential-heavy prefix, then ``credential`` straddling the window.

        The compressing prefix ends on a newline; the final line carries the
        credential across ``MODEL_ANSWER_SOURCE_MAX_CHARS`` and is exactly what
        the boundary must discard whole.
        """
        head = ""
        while len(head) + len(self.COMPRESSING_LINE) < MODEL_ANSWER_SOURCE_MAX_CHARS:
            head += self.COMPRESSING_LINE
        # Land the credential across the window: start it 10 characters short
        # of the boundary so only an unrecognisable prefix could survive.
        lead = MODEL_ANSWER_SOURCE_MAX_CHARS - len(head) - 10
        self.assertGreater(lead, 0, "construction did not reach the window")
        return head + "x" * lead + " " + credential + " trailing\n"

    def test_no_credential_fragment_crosses_the_source_window(self) -> None:
        for name, credential, secret in self.FORMS:
            with self.subTest(form=name):
                raw = self.exploit(credential)
                self.assertGreater(len(raw), MODEL_ANSWER_SOURCE_MAX_CHARS)
                answer = build_model_answer(raw)
                # The whole boundary-crossing line is gone, so no prefix of it
                # can be in the deliverable - checked at several granularities
                # because different patterns leak different amounts.
                for length in (4, 8, 16):
                    self.assertNotIn(
                        secret[:length],
                        answer.content,
                        f"{name}: a {length}-character fragment survived",
                    )
                self.assertNotIn("x" * 11, answer.content, f"{name}: line kept")
                # ...and the record is still a well-formed, bindable answer.
                self.assertIsNone(model_answer_refusal(answer))
                self.assertTrue(answer.truncated)

    def test_the_compressing_prefix_really_reaches_the_deliverable(self) -> None:
        """Without this the regression above could pass vacuously.

        The exploit only matters because the retained text compresses to well
        under the 8000-character cap - otherwise the cap alone would remove
        the tail and there would be nothing to prove.
        """
        answer = build_model_answer(self.exploit(self.FORMS[0][1]))
        self.assertLess(len(answer.content), MODEL_ANSWER_MAX_CHARS)
        self.assertGreater(len(answer.content), 0)
        self.assertIn("[REDACTED]", answer.content)

    def test_a_window_without_a_safe_boundary_fails_closed(self) -> None:
        """No newline in the window means no boundary that can be proven safe.

        Yielding the raw cut would risk publishing half a token, so the
        pipeline yields nothing instead. That is loud: the connector's
        verifier refuses an empty answer and the task fails.
        """
        raw = "n" * (MODEL_ANSWER_SOURCE_MAX_CHARS + 500)
        content, truncated = bound_model_text(raw)
        self.assertEqual("", content)
        self.assertTrue(truncated)
        answer = build_model_answer(raw)
        self.assertEqual("", answer.content)
        # Structurally valid but empty, so VERIFY - not the contract - is what
        # refuses it, which keeps the failure visible rather than silent.
        self.assertIsNone(model_answer_refusal(answer))

    def test_the_boundary_keeps_whole_lines_only(self) -> None:
        line = "y" * 99 + "\n"
        raw = line * (MODEL_ANSWER_SOURCE_MAX_CHARS // 100 + 40)
        content, truncated = bound_model_text(raw)
        self.assertTrue(truncated)
        self.assertTrue(content.endswith("\n") or len(content) == MODEL_ANSWER_MAX_CHARS)

    def test_a_source_under_the_window_is_untouched_by_the_boundary(self) -> None:
        raw = "no newline at all, and well under the window"
        self.assertEqual((raw, False), bound_model_text(raw))


# --------------------------------------------------------------------------
# 3. The persisted structural contract
# --------------------------------------------------------------------------


class RecordContractTests(unittest.TestCase):
    def valid(self, text: str = "hello\nworld\n") -> ModelAnswer:
        answer = build_model_answer(text)
        self.assertIsNone(model_answer_refusal(answer))
        return answer

    def test_a_valid_answer_is_accepted(self) -> None:
        self.assertIsNone(model_answer_refusal(self.valid()))

    def test_presence_is_the_digest(self) -> None:
        self.assertFalse(ModelAnswer().present)
        self.assertTrue(self.valid().present)
        # An empty-but-real answer is present and structurally valid; VERIFY,
        # not this contract, is what refuses to vouch for it.
        empty = build_model_answer("")
        self.assertTrue(empty.present)
        self.assertIsNone(model_answer_refusal(empty))

    def test_a_foreign_object_is_refused(self) -> None:
        for value in (None, {}, "text", 7, LocalReadSnapshot(), object()):
            self.assertEqual("not a ModelAnswer", model_answer_refusal(value))

    def test_the_digest_must_match_the_content(self) -> None:
        answer = self.valid()
        forged = replace(answer, content=answer.content + "!")
        self.assertIsNotNone(model_answer_refusal(forged))
        wrong = replace(answer, sha256="0" * 64)
        self.assertEqual(
            "digest does not match the content", model_answer_refusal(wrong)
        )

    def test_a_malformed_digest_grammar_is_refused(self) -> None:
        answer = self.valid()
        for digest in ("", "ABC", "z" * 64, "A" * 64, "a" * 63, 7, None):
            self.assertEqual(
                "digest is not a lowercase sha256 hex digest",
                model_answer_refusal(replace(answer, sha256=digest)),
            )

    def test_the_character_count_must_match(self) -> None:
        answer = self.valid()
        self.assertEqual(
            "character count does not match the content",
            model_answer_refusal(replace(answer, character_count=1)),
        )

    def test_a_boolean_character_count_is_refused(self) -> None:
        answer = self.valid()
        self.assertEqual(
            "character count is not an integer",
            model_answer_refusal(replace(answer, character_count=True)),
        )

    def test_oversized_content_is_refused_not_rebounded(self) -> None:
        text = "w" * (MODEL_ANSWER_MAX_CHARS + 1)
        forged = ModelAnswer(
            content=text,
            character_count=len(text),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            truncated=False,
            produced_at=utc_now(),
        )
        self.assertEqual(
            "content exceeds its character ceiling", model_answer_refusal(forged)
        )

    def test_non_canonical_content_is_refused(self) -> None:
        """Raw CRLF or a live credential is not something a real answer stored."""
        for text in ("a\r\nb", "api_key: hunter2super"):
            forged = ModelAnswer(
                content=text,
                character_count=len(text),
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                truncated=False,
                produced_at=utc_now(),
            )
            self.assertEqual(
                "content is not the canonical bounded representation",
                model_answer_refusal(forged),
            )

    def test_the_truncated_flag_is_only_an_exact_boolean(self) -> None:
        """Exact type is all this validator may claim about ``truncated``.

        An earlier rule also required ``truncated=True`` to imply a content
        length of exactly the ceiling. It was false: loss has two independent
        sources - the source window and the output cap - and a
        window-truncated answer that redaction then compresses legitimately
        ends up far under the ceiling. The rule rejected honest records, and
        the history it claimed to check is unreconstructible anyway once the
        discarded text is gone.
        """
        answer = self.valid()
        self.assertEqual(
            "truncated flag is not a boolean",
            model_answer_refusal(replace(answer, truncated=1)),
        )
        # Short AND truncated is legitimate, and must not be refused.
        self.assertIsNone(model_answer_refusal(replace(answer, truncated=True)))
        # At the ceiling the flag is legitimate in either state too. Nothing
        # here re-derives truncation history; the flag is producer-set.
        full = build_model_answer("f" * (MODEL_ANSWER_MAX_CHARS + 3))
        self.assertTrue(full.truncated)
        self.assertIsNone(model_answer_refusal(full))
        self.assertIsNone(model_answer_refusal(replace(full, truncated=False)))

    def test_a_malformed_timestamp_is_refused(self) -> None:
        answer = self.valid()
        for stamp in ("", "yesterday", "2026-08-25", 7, None, "x" * 200):
            self.assertEqual(
                "produced timestamp is not a canonical UTC timestamp",
                model_answer_refusal(replace(answer, produced_at=stamp)),
            )

    def test_non_string_content_is_refused(self) -> None:
        answer = self.valid()
        self.assertEqual(
            "content is not a string", model_answer_refusal(replace(answer, content=7))
        )

    def test_a_lone_surrogate_row_loads_closed_instead_of_raising(self) -> None:
        """M7 rider A: the persisted UTF-8 boundary fails closed.

        A lone surrogate survives JSON decoding of a tampered row and cannot
        be encoded. Before the guard, ``model_answer_refusal`` raised
        ``UnicodeEncodeError`` out of ``Task.from_dict`` - the one loader
        every resume goes through - instead of loading the row closed.
        """
        row = {
            "content": "x\ud800y",
            "character_count": 3,
            "sha256": "0" * 64,
            "truncated": False,
            "produced_at": utc_now(),
        }
        answer = ModelAnswer.from_dict(row)
        self.assertFalse(answer.present)
        self.assertEqual("", answer.content)
        candidate = ModelAnswer(**row)
        self.assertIn("not encodable", model_answer_refusal(candidate))
        task = Task.from_dict(
            {
                "objective": "o",
                "capability": "c",
                "scope": {"action": "a"},
                "id": "t-surrogate",
                "model_answer": row,
            }
        )
        self.assertFalse(task.model_answer.present)

    def test_valid_unicode_is_untouched_by_the_encodability_guard(self) -> None:
        text = "astral 😀, accent é, separator line\n"
        answer = build_model_answer(text)
        self.assertIsNone(model_answer_refusal(answer))
        self.assertEqual(text, answer.content)
        self.assertEqual(answer, ModelAnswer.from_dict(answer.to_dict()))
        self.assertEqual(
            hashlib.sha256(text.encode("utf-8")).hexdigest(), answer.sha256
        )

    def test_a_valid_record_round_trips_identically(self) -> None:
        answer = self.valid(STRUCTURED_ANSWER)
        self.assertEqual(answer, ModelAnswer.from_dict(answer.to_dict()))
        self.assertEqual(
            json.dumps(answer.to_dict(), sort_keys=True),
            json.dumps(ModelAnswer.from_dict(answer.to_dict()).to_dict(), sort_keys=True),
        )

    def test_every_malformed_row_loads_as_the_empty_answer(self) -> None:
        good = self.valid().to_dict()
        rows: list[Any] = [
            None,
            "",
            [],
            {},
            {**good, "content": good["content"] + "!"},
            {**good, "sha256": "0" * 64},
            {**good, "character_count": 99},
            {**good, "character_count": "12"},
            {**good, "truncated": "yes"},
            {**good, "produced_at": "nope"},
            {**good, "content": "x" * (MODEL_ANSWER_MAX_CHARS + 1)},
            {**good, "content": None},
        ]
        for row in rows:
            self.assertFalse(
                ModelAnswer.from_dict(row).present, f"row survived: {str(row)[:60]}"
            )

    def test_nothing_is_coerced_on_load(self) -> None:
        """A coerced field would turn tampered state into plausible state.

        Every row here is chosen so that coercion would make it VALID: the
        count string coerces to the true length, the truncation integer
        coerces to the true flag. If ``from_dict`` ever calls ``int()`` or
        ``bool()``, these rows stop failing and tampered persisted state
        becomes active state wearing a clean face.
        """
        answer = self.valid()
        good = answer.to_dict()
        self.assertEqual(12, answer.character_count)
        coercible = (
            {**good, "character_count": "12"},
            {**good, "character_count": 12.0},
            {**good, "character_count": True},
            {**good, "truncated": 0},
            {**good, "truncated": ""},
            {**good, "content": 7},
        )
        for row in coercible:
            with self.subTest(row=str(row)[:60]):
                self.assertFalse(ModelAnswer.from_dict(row).present)

    def test_load_save_load_is_byte_stable(self) -> None:
        answer = self.valid(STRUCTURED_ANSWER)
        once = ModelAnswer.from_dict(answer.to_dict())
        twice = ModelAnswer.from_dict(once.to_dict())
        self.assertEqual(once, twice)
        self.assertEqual(answer, twice)


# --------------------------------------------------------------------------
# 4. Task persistence
# --------------------------------------------------------------------------


class PersistenceTests(ModelAnswerTestCase):
    def test_the_answer_survives_a_store_reload_intact(self) -> None:
        task, _ = self.completed()
        reloaded = self.stored(task.id)
        self.assertEqual(task.model_answer, reloaded.model_answer)
        self.assertEqual(STRUCTURED_ANSWER, reloaded.model_answer.content)

    def test_a_reload_resave_reload_is_stable(self) -> None:
        task, _ = self.completed()
        first = self.stored(task.id)
        json.dumps(first.to_dict(), sort_keys=True)
        second = Task.from_dict(json.loads(json.dumps(first.to_dict())))
        self.assertEqual(first.model_answer, second.model_answer)

    def test_the_answer_is_not_erased_at_terminal_state(self) -> None:
        """Unlike ``write_content``: this IS the completed task's deliverable."""
        task, _ = self.completed()
        self.assertIs(TaskStatus.COMPLETED, task.status)
        self.assertTrue(self.stored(task.id).model_answer.present)

    def test_a_tampered_row_loads_closed(self) -> None:
        task, _ = self.completed()
        document = json.loads(self.raw_state(task.id))
        document["model_answer"]["content"] += "TAMPERED"
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE tasks SET state_json = ? WHERE id = ?",
                (json.dumps(document), task.id),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(self.stored(task.id).model_answer.present)

    def test_legacy_task_json_without_the_field_loads_empty(self) -> None:
        task, _ = self.completed()
        document = task.to_dict()
        document.pop("model_answer")
        self.assertFalse(Task.from_dict(document).model_answer.present)

    def test_no_provider_internals_are_persisted(self) -> None:
        task, _ = self.completed()
        raw = self.raw_state(task.id)
        for internal in (
            "response_type",
            "subtype",
            "stop_reason",
            "api_error_status",
            "redacted_items",
            "stdout",
            "stderr",
        ):
            self.assertNotIn(internal, raw, f"provider internal persisted: {internal}")

    def test_the_sqlite_schema_is_unchanged(self) -> None:
        from jarvis.memory import SCHEMA_VERSION

        # Scheduler phase 2A owns the current SQLite schema version; this
        # milestone adds no storage schema of its own.
        self.assertEqual(10, SCHEMA_VERSION)
        self.assertEqual(10, self.memory.schema_version())


# --------------------------------------------------------------------------
# 5. The producer gate
# --------------------------------------------------------------------------


class ProducerGateTests(ModelAnswerTestCase):
    def test_a_non_egress_connector_cannot_smuggle_an_answer(self) -> None:
        """A local connector's result data is not external model output."""
        local = AnsweringConnector(name="local-ish", side_effect=SideEffect.LOCAL_READ)
        app = open_app(
            settings=Settings(
                memory_database=self.root / "local.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(local,),
            destination_policy=frozenset(
                {
                    DestinationGrant("local-ish", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION),
                }
            ),
        )
        task = make_task(effect=SideEffect.LOCAL_READ)
        task.capability = "echo"
        run = app.core.run(task)
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        self.assertFalse(app.memory.load_task(task.id).model_answer.present)

    def test_a_task_side_escalation_does_not_make_a_connector_eligible(self) -> None:
        """The gate reads the MANIFEST, never the merged effect.

        ``merge_side_effects`` lets a NONE-manifest connector take the task's
        requested effect wholesale, so a task can declare EXTERNAL_EGRESS on a
        connector that never claimed to be an external-model connector at all.
        Under a merged-effect gate that connector would become eligible to have
        "external model output" attributed to it on a task's say-so. The
        manifest is the connector's own, non-task-controlled claim, which is
        exactly why the gate reads it and nothing else.
        """
        neutral = AnsweringConnector(name="neutral", side_effect=SideEffect.NONE)
        self.assertIs(SideEffect.NONE, neutral.manifest.default_side_effect.effect)
        app = open_app(
            settings=Settings(
                memory_database=self.root / "neutral.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(neutral,),
            destination_policy=frozenset(
                {
                    DestinationGrant("neutral", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION),
                }
            ),
        )
        task = make_task(effect=SideEffect.EXTERNAL_EGRESS)
        task.capability = "echo"
        # The merge really does escalate: this is what a merged-effect gate
        # would have seen, and it is exactly EXTERNAL_EGRESS.
        self.assertIs(SideEffect.EXTERNAL_EGRESS, neutral.side_effects(task).effect)
        run = app.core.run(task)
        if run.status is RunStatus.AWAITING_APPROVAL:
            run = app.core.resume(
                run.session.id,
                task.id,
                grant=PermissionGrant.for_request(run.permission_request),
            )
        self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
        self.assertFalse(app.memory.load_task(task.id).model_answer.present)

    def test_a_malformed_candidate_binds_nothing_and_never_raises(self) -> None:
        good = build_model_answer(STRUCTURED_ANSWER)
        for payload in (
            None,
            {},
            "text",
            7,
            good.to_dict(),
            replace(good, sha256="0" * 64),
            replace(good, character_count=3),
            LocalReadSnapshot(),
        ):
            with self.subTest(payload=str(payload)[:40]):
                self.connector.raw_override = payload
                self.connector.override_active = True
                task = self.egress_task()
                run = self.core.run(task)
                grant = PermissionGrant.for_request(run.permission_request)
                run = self.core.resume(run.session.id, task.id, grant=grant)
                # The lift discards it; the connector's own verifier then
                # fails the task closed rather than vouching for nothing.
                self.assertEqual(RunStatus.COMPLETED, run.status, msg=str(run.error))
                self.assertFalse(self.memory.load_task(task.id).model_answer.present)
        self.connector.override_active = False


# --------------------------------------------------------------------------
# 6. Input and egress isolation
# --------------------------------------------------------------------------


class IsolationTests(ModelAnswerTestCase):
    def claude(self) -> ClaudeCodeConnector:
        return ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )

    def consumer(self) -> Task:
        return Task(
            objective="analyze the project",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.INTERNAL,
            ),
        )

    def test_the_answer_can_never_be_supplied_by_input(self) -> None:
        """Forged model evidence must not be accepted with zero model calls."""
        document = {
            "objective": "forge an answer",
            "capability": "code_analysis",
            "scope": {"action": "analyze", "target": "x"},
            "model_answer": build_model_answer("forged").to_dict(),
        }
        with self.assertRaises(Exception) as caught:
            decode_task_json(json.dumps(document))
        self.assertIn("model_answer", str(caught.exception))

    def test_the_answer_is_not_accepted_through_context_either(self) -> None:
        document = {
            "objective": "forge an answer",
            "capability": "code_analysis",
            "scope": {"action": "analyze", "target": "x"},
            "context": {"model_answer": "forged"},
        }
        with self.assertRaises(Exception):
            decode_task_json(json.dumps(document))

    def test_a_populated_answer_does_not_alter_the_outbound_key_set(self) -> None:
        connector = self.claude()
        task = self.consumer()
        before, before_count = connector._sanitized_payload(task)
        task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        after, after_count = connector._sanitized_payload(task)
        self.assertEqual(set(before), set(after))
        self.assertEqual(before, after)
        self.assertEqual(before_count, after_count)
        self.assertNotIn("model_answer", set(after))
        self.assertNotIn(DEEP_MARKER, json.dumps(after))

    def test_canonical_outbound_bytes_are_identical_with_and_without(self) -> None:
        connector = self.claude()
        task = self.consumer()
        before = connector.canonical_outbound(task)
        task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        self.assertEqual(before, connector.canonical_outbound(task))
        self.assertNotIn(DEEP_MARKER.encode("utf-8"), before)

    def test_the_attestation_digest_and_fingerprint_are_unaffected(self) -> None:
        connector = self.claude()
        task = self.consumer()
        first = connector.permission_request(task, "session-1")
        task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        second = connector.permission_request(task, "session-1")
        self.assertEqual(
            first.attestation.outbound_digest, second.attestation.outbound_digest
        )
        self.assertEqual(first.attestation.digest, second.attestation.digest)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_the_outbound_preview_is_unaffected(self) -> None:
        connector = self.claude()
        task = self.consumer()
        before = connector.outbound_preview(task)
        task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        after = connector.outbound_preview(task)
        self.assertEqual(before, after)

    def test_the_model_is_still_granted_no_tools_at_all(self) -> None:
        self.assertEqual((), claude_code.EGRESS_TOOL_GRANTS)
        self.assertEqual((), self.claude().external_tool_grants(self.consumer()))

    def test_pre_m6_attestation_digests_are_unchanged(self) -> None:
        """The shared EgressAttestation contract did not move.

        M6 is inbound-only, so no field had to be added here; adding one
        would have changed EVERY persisted attestation digest in the product
        and silently invalidated every outstanding approval. The field set
        and a concrete digest are both pinned, so a reordering or a changed
        serialization that preserved the keys still fails.
        """
        from jarvis.contracts import EgressAttestation

        attestation = EgressAttestation(
            scope=ActionScope(action="analyze", target="x").normalized(),
            risk_level=RiskLevel.LOW,
            side_effect=SideEffectProfile(),
            outbound_digest="a" * 64,
        )
        self.assertEqual(
            {
                "scope",
                "risk_level",
                "side_effect",
                "outbound_digest",
                "billing_gate",
                "cost_gate",
                "working_directory",
                "allowed_roots",
                "tool_grants",
            },
            set(attestation.to_dict()),
        )
        self.assertEqual(
            "d83d215a8d84426722f25fb6197a19660d8a97034b530ff0831e2abc7b4cff21",
            attestation.digest,
        )


# --------------------------------------------------------------------------
# 7. Authority isolation
# --------------------------------------------------------------------------


class AuthorityIsolationTests(ModelAnswerTestCase):
    def test_the_answer_changes_no_plan(self) -> None:
        task = self.egress_task()
        before = build_task_plan(task)
        task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        self.assertEqual(before, build_task_plan(task))
        self.assertNotIn(DEEP_MARKER, json.dumps(before))

    def test_the_answer_changes_no_permission_decision(self) -> None:
        first = self.egress_task()
        run_one = self.core.run(first)
        second = self.egress_task()
        second.model_answer = build_model_answer(STRUCTURED_ANSWER)
        run_two = self.core.run(second)
        self.assertEqual(run_one.status, run_two.status)
        self.assertEqual(
            run_one.permission_request.attestation.digest,
            run_two.permission_request.attestation.digest,
        )
        self.assertEqual(
            run_one.permission_request.risk_level, run_two.permission_request.risk_level
        )
        self.assertEqual(
            run_one.permission_request.side_effect,
            run_two.permission_request.side_effect,
        )

    def test_the_answer_changes_no_billing_or_connector_selection(self) -> None:
        first, run_one = self.completed()
        second_task = self.egress_task()
        second_task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        second, run_two = self.completed(second_task)
        self.assertEqual(first.selected_connector, second.selected_connector)
        self.assertEqual(
            run_one.result.usage.billing_class, run_two.result.usage.billing_class
        )
        self.assertEqual(
            run_one.result.usage.monetary_state, run_two.result.usage.monetary_state
        )
        self.assertEqual(first.estimate.gate_state, second.estimate.gate_state)

    def test_a_completed_answer_never_reaches_project_memory(self) -> None:
        self.completed()
        connection = sqlite3.connect(self.database)
        try:
            stored = connection.execute(
                "SELECT COUNT(*) FROM project_memory_items"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, stored)


# --------------------------------------------------------------------------
# 8. Decision / retrieval separation
# --------------------------------------------------------------------------


class RetrievalSeparationTests(ModelAnswerTestCase):
    def test_the_decision_record_carries_only_the_bounded_summary(self) -> None:
        task, run = self.completed()
        decisions = self.memory.list_decisions(run.session.id)
        self.assertEqual(1, len(decisions))
        decision = decisions[0]
        self.assertLessEqual(len(decision.summary), DIAGNOSTIC_MESSAGE_MAX_CHARS)
        self.assertNotIn(DEEP_MARKER, decision.summary)
        self.assertNotIn(DEEP_MARKER, decision.rationale)
        self.assertNotIn(WINDOWS_PATH, decision.summary)

    def test_a_populated_answer_consumes_no_extra_retrieval_quota(self) -> None:
        """Zero additional items and zero additional characters."""
        first, run = self.completed()
        session_id = run.session.id

        follow_up = self.egress_task(objective=f"follow up on {HEAD_MARKER}")
        run_two = self.core.run(follow_up, session_id=session_id)
        with_answer = run_two.task.retrieved_context

        # The same session and the same query, with the stored answer removed
        # from the source task: retrieval must be identical either way.
        stored = self.memory.load_task(first.id)
        stored.model_answer = ModelAnswer()
        self.memory.save_task(session_id, stored)
        control_task = self.egress_task(objective=f"follow up on {HEAD_MARKER}")
        run_three = self.core.run(control_task, session_id=session_id)
        without_answer = run_three.task.retrieved_context

        self.assertEqual(len(without_answer.items), len(with_answer.items))
        self.assertEqual(without_answer.character_count, with_answer.character_count)
        self.assertNotIn(DEEP_MARKER, json.dumps(with_answer.to_dict()))

    def test_no_retrieved_item_ever_carries_the_full_answer(self) -> None:
        _, run = self.completed()
        follow_up = self.egress_task(objective="what did we decide")
        run_two = self.core.run(follow_up, session_id=run.session.id)
        serialized = json.dumps(run_two.task.retrieved_context.to_dict())
        self.assertNotIn(DEEP_MARKER, serialized)
        self.assertNotIn(WINDOWS_PATH, serialized)


# --------------------------------------------------------------------------
# 9. SECRET
# --------------------------------------------------------------------------


class SecretTests(ModelAnswerTestCase):
    def test_a_secret_model_task_spawns_no_process_and_produces_no_answer(self) -> None:
        connector = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        task = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.SECRET,
            ),
        )
        with mock.patch.object(claude_code.subprocess, "Popen") as popen:
            result = connector.execute(task, SessionState())
        popen.assert_not_called()
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertNotIn(MODEL_ANSWER_RESULT_KEY, result.data)
        self.assertFalse(task.model_answer.present)

    def test_a_secret_task_cannot_even_be_attested(self) -> None:
        connector = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        task = Task(
            objective="analyze",
            capability="code_analysis",
            scope=ActionScope(
                action="analyze",
                target=str(self.root),
                destination="anthropic/claude-code",
                data_classification=DataClassification.SECRET,
            ),
        )
        with self.assertRaises(Exception):
            connector.permission_request(task, "session-1")


# --------------------------------------------------------------------------
# 10. Verification
# --------------------------------------------------------------------------


class VerificationTests(ModelAnswerTestCase):
    def connector_and_result(self, text: str):
        connector = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        result = ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=text,
            usage=UsageRecord(connector=connector.manifest.name),
        )
        return connector, result

    def test_a_valid_non_empty_answer_passes_with_integrity_evidence(self) -> None:
        connector, result = self.connector_and_result(STRUCTURED_ANSWER)
        task = self.egress_task()
        task.model_answer = build_model_answer(STRUCTURED_ANSWER)
        record = connector.verify(task, result, SessionState())
        self.assertIs(VerificationStatus.PASSED, record.status)
        self.assertEqual("model_answer_integrity", record.method)
        self.assertIn(f"sha256={task.model_answer.sha256}", record.evidence)
        self.assertIn(f"characters={task.model_answer.character_count}", record.evidence)
        self.assertIn("truncated=False", record.evidence)

    def test_a_missing_answer_fails_closed(self) -> None:
        connector, result = self.connector_and_result("anything")
        record = connector.verify(self.egress_task(), result, SessionState())
        self.assertIs(VerificationStatus.FAILED, record.status)

    def test_a_whitespace_only_answer_fails_closed(self) -> None:
        connector, result = self.connector_and_result("   \n  \n")
        task = self.egress_task()
        task.model_answer = build_model_answer("   \n  \n")
        record = connector.verify(task, result, SessionState())
        self.assertIs(VerificationStatus.FAILED, record.status)

    def test_a_tampered_answer_fails_closed(self) -> None:
        connector, result = self.connector_and_result(STRUCTURED_ANSWER)
        task = self.egress_task()
        answer = build_model_answer(STRUCTURED_ANSWER)
        task.model_answer = replace(answer, sha256="0" * 64)
        record = connector.verify(task, result, SessionState())
        self.assertIs(VerificationStatus.FAILED, record.status)

    def test_semantic_correctness_is_never_evaluated(self) -> None:
        """Nonsense verifies exactly like insight; VERIFY judges structure."""
        connector, result = self.connector_and_result("x")
        task = self.egress_task()
        task.model_answer = build_model_answer("this answer is confidently wrong")
        record = connector.verify(task, result, SessionState())
        self.assertIs(VerificationStatus.PASSED, record.status)

    def test_a_short_truncated_answer_is_valid_and_verifies(self) -> None:
        """Window loss plus heavy redaction: short, truncated, and honest.

        The exact shape an earlier coherence rule wrongly rejected. Source
        content really was discarded (the window), yet redaction compressed
        what remained to far under the ceiling, so ``truncated=True`` with a
        short body is the truthful record - and it must bind and verify like
        any other. Nothing here re-derives the truncation history; the flag is
        reported as evidence, never checked against the content.
        """
        line = "Bearer " + "B" * 493 + "\n"
        raw = line * (MODEL_ANSWER_SOURCE_MAX_CHARS // len(line) + 20)
        answer = build_model_answer(raw)

        self.assertTrue(answer.truncated, "source content really was dropped")
        self.assertGreater(len(answer.content), 0)
        self.assertLess(len(answer.content), MODEL_ANSWER_MAX_CHARS)
        self.assertIsNone(model_answer_refusal(answer), "an honest record was refused")

        connector, result = self.connector_and_result("irrelevant summary")
        task = self.egress_task()
        task.model_answer = answer
        record = connector.verify(task, result, SessionState())
        self.assertIs(VerificationStatus.PASSED, record.status, msg=str(record.evidence))
        self.assertIn("truncated=True", record.evidence)


# --------------------------------------------------------------------------
# 11. Crash and restart
# --------------------------------------------------------------------------


class _SimulatedCrash(BaseException):
    """Not an ``Exception``: no product handler may swallow it."""


class CrashRestartTests(ModelAnswerTestCase):
    def test_the_answer_is_not_durable_before_the_execution_conclusion(self) -> None:
        """The exact durability boundary, asserted from inside the window."""
        observed: dict[str, Any] = {}
        real = SQLiteMemoryStore.conclude_execution

        def watched(store, session, task, attempt_id, event_type, payload):
            # In memory the task already carries the answer; storage does not.
            observed["in_memory"] = task.model_answer.present
            persisted = self.stored(task.id)
            observed["in_storage"] = (
                persisted is not None and persisted.model_answer.present
            )
            return real(store, session, task, attempt_id, event_type, payload)

        with mock.patch.object(SQLiteMemoryStore, "conclude_execution", watched):
            task, _ = self.completed()
        self.assertTrue(observed["in_memory"])
        self.assertFalse(observed["in_storage"], "the answer was durable too early")
        self.assertTrue(self.stored(task.id).model_answer.present)

    def test_a_crash_before_the_conclusion_persists_no_answer(self) -> None:
        def crash(*args, **kwargs):
            raise _SimulatedCrash()

        task = self.egress_task()
        run = self.core.run(task)
        grant = PermissionGrant.for_request(run.permission_request)
        with mock.patch.object(SQLiteMemoryStore, "conclude_execution", crash):
            with self.assertRaises(_SimulatedCrash):
                self.core.resume(run.session.id, task.id, grant=grant)
        reloaded = self.stored(task.id)
        self.assertFalse(
            reloaded.model_answer.present,
            "an answer was claimed durable that never committed",
        )
        self.assertIsNot(TaskStatus.COMPLETED, reloaded.status)

    def test_a_restart_at_verify_uses_the_durable_answer(self) -> None:
        task, _ = self.completed()
        reloaded = self.stored(task.id)
        self.assertEqual(task.model_answer, reloaded.model_answer)
        connector = ClaudeCodeConnector(
            allowed_roots=(self.root,), default_working_directory=self.root
        )
        result = ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary="restored",
            usage=UsageRecord(connector=connector.manifest.name),
        )
        # A fresh connector instance holds no ephemeral per-task metadata, so
        # the durable record is the only thing verification can rely on.
        self.assertIs(
            VerificationStatus.PASSED,
            connector.verify(reloaded, result, SessionState()).status,
        )

    def test_a_stale_writer_cannot_attach_an_answer_to_a_terminal_task(self) -> None:
        """Terminal immutability covers the answer like every other field.

        Deliberately ``persist_checkpoint`` and not ``save_task``:
        ``save_task`` is the documented direct staging door and writes with
        ``force=True``, so it is not a product write path and proves nothing
        about arbitration. ``persist_checkpoint`` is the guarded write the
        loop actually performs, and it must lose against a terminal row.
        """
        task, run = self.completed()
        original = self.stored(task.id).model_answer
        stale = self.stored(task.id)
        stale.status = TaskStatus.RUNNING
        stale.model_answer = build_model_answer("FORGED-BY-A-STALE-WRITER")
        won = self.memory.persist_checkpoint(run.session, stale, "forged_event")
        self.assertFalse(won, "a stale writer won against a terminal row")
        self.assertEqual(original, self.stored(task.id).model_answer)
        self.assertIs(TaskStatus.COMPLETED, self.stored(task.id).status)


# --------------------------------------------------------------------------
# 12. The public envelope
# --------------------------------------------------------------------------


class EnvelopeTests(ModelAnswerTestCase):
    def test_the_envelope_is_version_five_with_a_fixed_key_set(self) -> None:
        self.assertEqual(6, OUTPUT_SCHEMA_VERSION)
        run = self.core.run(make_task())
        payload = build_envelope("run", PermissionMode.SESSION, run)
        self.assertEqual(6, payload["schema_version"])
        self.assertIn("model_answer", payload)

    def test_a_task_without_an_answer_carries_a_null_block(self) -> None:
        app = open_app(
            settings=Settings(
                memory_database=self.root / "plain.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(DummyConnector(),),
            destination_policy=frozenset(
                {
                    DestinationGrant("dummy", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION),
                }
            ),
        )
        run = app.core.run(make_task())
        payload = build_envelope("run", PermissionMode.SESSION, run)
        self.assertIsNone(payload["model_answer"])

    def test_a_completed_answer_renders_the_full_bounded_block(self) -> None:
        task, run = self.completed()
        payload = build_envelope("resume", PermissionMode.SESSION, run)
        block = payload["model_answer"]
        self.assertEqual(
            {"content", "character_count", "sha256", "truncated", "produced_at"},
            set(block),
        )
        self.assertEqual(STRUCTURED_ANSWER, block["content"])
        self.assertEqual(len(STRUCTURED_ANSWER), block["character_count"])
        self.assertFalse(block["truncated"])
        self.assertIn(WINDOWS_PATH, block["content"])
        self.assertIn(DEEP_MARKER, block["content"])
        self.assertIn("\n", block["content"])

    def test_the_envelope_carries_no_provider_internals(self) -> None:
        _, run = self.completed()
        serialized = json.dumps(build_envelope("resume", PermissionMode.SESSION, run))
        for internal in (
            "response_type",
            "subtype",
            "stop_reason",
            "api_error_status",
            "redacted_items",
            "stdout",
            "stderr",
        ):
            self.assertNotIn(internal, serialized)

    def test_a_tampered_record_renders_no_block(self) -> None:
        _, run = self.completed()
        run.task.model_answer = replace(run.task.model_answer, sha256="0" * 64)
        payload = build_envelope("resume", PermissionMode.SESSION, run)
        self.assertIsNone(payload["model_answer"])

    def test_a_credential_never_reaches_the_envelope(self) -> None:
        connector = AnsweringConnector(
            name="leaky",
            answer_text="here is the key sk-abcdefghijklmnopqrstuvwxyz012345 ok\n",
        )
        app = open_app(
            settings=Settings(
                memory_database=self.root / "leaky.db",
                permission_mode=PermissionMode.SESSION,
            ),
            connectors=(connector,),
            destination_policy=frozenset(
                {
                    DestinationGrant("leaky", DUMMY_CAPABILITY, CLAUDE_CODE_DESTINATION),
                }
            ),
        )
        task = self.egress_task()
        run = app.core.run(task)
        run = app.core.resume(
            run.session.id,
            task.id,
            grant=PermissionGrant.for_request(run.permission_request),
        )
        serialized = json.dumps(build_envelope("resume", PermissionMode.SESSION, run))
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_the_result_block_still_shows_the_bounded_summary(self) -> None:
        _, run = self.completed()
        payload = build_envelope("resume", PermissionMode.SESSION, run)
        summary = payload["result"]["summary"]
        self.assertLessEqual(len(summary), DIAGNOSTIC_MESSAGE_MAX_CHARS)
        self.assertNotIn(DEEP_MARKER, summary)
        self.assertIn(DEEP_MARKER, payload["model_answer"]["content"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
