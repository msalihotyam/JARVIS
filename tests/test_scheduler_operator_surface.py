"""The minimum scheduler OPERATOR SURFACE: create, list, enable, disable, archive.

Covers the whole boundary of the five verbs an operator needs to keep usable
scheduled work: one job created from its own persisted JSON encoding (a
CONDITION_WATCH included, through the existing ``condition`` + ``on_true``
model), a bounded read-only catalogue, and the three state changes - each
compare-and-set on the revision the operator just read.

TWO PROPERTIES ARE THE POINT. First, every malformed or unsupported
specification fails closed through the EXISTING validators (the job grammar,
the schedule engine, the condition vocabulary) with nothing created. Second,
defining a job is not authority: creating one materializes no task, opens no
attempt, requests no approval, consumes no grant and starts no generation.

Every instant this surface computes is the real clock's, so no assertion here
depends on a stored timestamp's value; what is asserted is the durable state
the verbs produce. No test performs a live model, auth or network call.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.app import (
    EXIT_INVALID_INPUT,
    EXIT_OK,
    job_archive_result,
    job_create_result,
    job_enablement_result,
    job_list_result,
)
from jarvis.cli import main
from jarvis.contracts import JobKind, ProjectRecord
from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings
from tests.helpers import safe_cleanup

# The phase-1A timezone fixture, reused verbatim: it installs a TZif fallback
# so a Daily schedule is expressible on a machine with no tzdata.
from tests.test_schedules import (  # noqa: F401
    NEW_YORK,
    setUpModule,
    tearDownModule,
)

DIGEST = "b" * 64
ANCHOR = "2026-09-06T12:00:00+00:00"

#: The authority tables an operator verb must never write a row into.
AUTHORITY_TABLES = (
    "sessions",
    "tasks",
    "job_runs",
    "pending_approvals",
    "permission_grants",
    "execution_attempts",
    "attempt_billing",
    "usage_ledger",
)


def interval_spec(**over) -> dict:
    """A RECURRING job in the exact encoding storage itself round-trips."""
    document = {
        "project_id": "proj-a",
        "kind": "recurring",
        "label": "nightly notes",
        "schedule": {
            "kind": "interval",
            "anchor": ANCHOR,
            "every_seconds": 3600,
            "end": None,
        },
        "steps": [
            {
                "objective": "Read the project notes.",
                "capability": "local_file_read",
                "action": "read",
                "target": "notes.txt",
            }
        ],
        "condition": None,
        "on_true": [],
    }
    document.update(over)
    return document


def watch_spec(**over) -> dict:
    """A CONDITION_WATCH: a predicate and an on_true declaration, no steps."""
    document = {
        "project_id": "proj-a",
        "kind": "condition_watch",
        "label": "watch the notes",
        "schedule": {"kind": "daily", "timezone": NEW_YORK, "at": "09:00", "end": None},
        "steps": [],
        "condition": {
            "kind": "local_read_sha256_differs",
            "reference": "notes.txt",
            "expected_sha256": DIGEST,
        },
        "on_true": [
            {
                "objective": "React to the change.",
                "capability": "local_file_read",
                "action": "read",
                "target": "notes.txt",
            }
        ],
    }
    document.update(over)
    return document


class OperatorSurfaceTestCase(unittest.TestCase):
    """A real schema-10 store and a real active project. No registry, no Core."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(safe_cleanup, self.temp)
        self.root = Path(self.temp.name)
        self.database = self.root / "jarvis.db"
        self.settings = Settings(memory_database=self.database)
        self.memory = SQLiteMemoryStore(self.database)
        self.memory.save_project(ProjectRecord(id="proj-a", name="Project A"))

    # -- helpers ----------------------------------------------------------

    def create(self, document) -> dict:
        raw = document if isinstance(document, (str, bytes)) else json.dumps(document)
        return job_create_result(self.memory, self.settings, raw)

    def created(self, document=None):
        """One created job, as (payload, JobRecord)."""
        result = self.create(interval_spec() if document is None else document)
        self.assertEqual(EXIT_OK, result.exit_code, result.payload)
        record = self.memory.load_job(result.payload["job_id"])
        self.assertIsNotNone(record)
        return result.payload, record

    def jobs(self, **over):
        result = job_list_result(self.memory, self.settings, **over)
        self.assertEqual(EXIT_OK, result.exit_code, result.payload)
        return result.payload

    def spec_file(self, document) -> Path:
        path = self.root / "spec.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def table_count(self, table: str) -> int:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def assertRefused(self, result, *, jobs: int = 0) -> None:
        """A failure envelope, and nothing durable created by it."""
        self.assertEqual(EXIT_INVALID_INPUT, result.exit_code, result.payload)
        self.assertIsNotNone(result.payload.get("error"), result.payload)
        self.assertEqual(jobs, self.table_count("jobs"))


# --------------------------------------------------------------------------
# CREATE - the existing grammar, and nothing else
# --------------------------------------------------------------------------


class CreateTests(OperatorSurfaceTestCase):
    def test_a_recurring_job_is_created_and_scheduled(self) -> None:
        payload, record = self.created()

        self.assertEqual(1, payload["revision"])
        self.assertEqual("recurring", payload["kind"])
        self.assertEqual("proj-a", payload["project_id"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(1, payload["declared_steps"])
        self.assertIsNone(payload["condition_kind"])
        # The schedule engine computed the first occurrence; nothing was run.
        self.assertIsNotNone(payload["next_due_at_utc"])
        self.assertEqual(payload["next_due_at_utc"], record.next_due_at_utc)
        self.assertIs(JobKind.RECURRING, record.spec.kind)
        self.assertFalse(payload["execution_authorized"])

    def test_a_condition_watch_is_created_from_condition_and_on_true(self) -> None:
        payload, record = self.created(watch_spec())

        self.assertEqual("condition_watch", payload["kind"])
        self.assertEqual("local_read_sha256_differs", payload["condition_kind"])
        self.assertEqual(0, payload["declared_steps"])
        self.assertEqual(1, payload["declared_on_true_steps"])
        self.assertEqual(NEW_YORK, payload["timezone"])
        # The predicate round-trips into the durable definition unchanged.
        self.assertEqual("notes.txt", record.spec.condition.reference)
        self.assertEqual(DIGEST, record.spec.condition.expected_sha256)
        self.assertEqual((), record.spec.steps)
        self.assertEqual(1, len(record.spec.on_true))

    def test_creating_a_job_creates_no_authority_row_of_any_kind(self) -> None:
        self.created()
        self.created(watch_spec())
        for table in AUTHORITY_TABLES:
            self.assertEqual(0, self.table_count(table), table)
        self.assertEqual(2, self.table_count("jobs"))


class MalformedSpecificationTests(OperatorSurfaceTestCase):
    """Every refusal below comes from an EXISTING validator, not a new one."""

    def test_a_document_that_is_not_json_is_refused(self) -> None:
        self.assertRefused(self.create("{not json"))

    def test_an_unknown_specification_key_is_refused(self) -> None:
        self.assertRefused(self.create(interval_spec(retries=3)))

    def test_an_unknown_job_kind_is_refused(self) -> None:
        self.assertRefused(self.create(interval_spec(kind="continuous")))

    def test_a_condition_on_a_non_watch_job_is_refused(self) -> None:
        document = interval_spec()
        document["condition"] = watch_spec()["condition"]
        self.assertRefused(self.create(document))

    def test_a_watch_declaring_unconditional_steps_is_refused(self) -> None:
        document = watch_spec()
        document["steps"] = interval_spec()["steps"]
        self.assertRefused(self.create(document))

    def test_a_watch_without_an_on_true_declaration_is_refused(self) -> None:
        self.assertRefused(self.create(watch_spec(on_true=[])))

    def test_a_malformed_schedule_is_refused(self) -> None:
        # A naive instant: the schedule engine refuses it rather than guessing
        # a zone for it.
        self.assertRefused(
            self.create(
                interval_spec(
                    schedule={
                        "kind": "interval",
                        "anchor": "2026-09-06T12:00:00",
                        "every_seconds": 3600,
                        "end": None,
                    }
                )
            )
        )

    def test_a_step_carrying_an_unknown_field_is_refused(self) -> None:
        document = interval_spec()
        document["steps"][0]["task_id"] = "smuggled"
        self.assertRefused(self.create(document))

    def test_a_job_for_an_unknown_project_is_refused(self) -> None:
        self.assertRefused(self.create(interval_spec(project_id="proj-missing")))

    def test_a_blank_label_is_refused(self) -> None:
        self.assertRefused(self.create(interval_spec(label="   ")))


# --------------------------------------------------------------------------
# LIST - bounded, read-only
# --------------------------------------------------------------------------


class ListTests(OperatorSurfaceTestCase):
    def test_the_catalogue_lists_jobs_with_the_revision_to_act_on(self) -> None:
        first, _record = self.created()
        second, _watch = self.created(watch_spec())

        payload = self.jobs()
        self.assertEqual(2, payload["count"])
        self.assertEqual(
            [first["job_id"], second["job_id"]],
            [job["id"] for job in payload["jobs"]],
        )
        self.assertEqual([1, 1], [job["revision"] for job in payload["jobs"]])
        self.assertTrue(all(job["readable"] for job in payload["jobs"]))

    def test_archived_jobs_are_excluded_unless_asked_for(self) -> None:
        payload, _record = self.created()
        job_archive_result(self.memory, self.settings, payload["job_id"], 1)

        self.assertEqual(0, self.jobs()["count"])
        listed = self.jobs(include_archived=True)
        self.assertEqual(1, listed["count"])
        self.assertTrue(listed["jobs"][0]["archived"])

    def test_an_out_of_range_limit_is_refused(self) -> None:
        for bad in (0, -1, 101, "20", True):
            result = job_list_result(self.memory, self.settings, bad)
            self.assertEqual(EXIT_INVALID_INPUT, result.exit_code, bad)


# --------------------------------------------------------------------------
# ENABLE / DISABLE / ARCHIVE - compare-and-set on the revision read
# --------------------------------------------------------------------------


class StateChangeTests(OperatorSurfaceTestCase):
    def test_disable_clears_the_next_occurrence_and_enable_recalculates(self) -> None:
        payload, _record = self.created()
        job_id = payload["job_id"]

        off = job_enablement_result(self.memory, self.settings, job_id, 1, False)
        self.assertEqual(EXIT_OK, off.exit_code, off.payload)
        self.assertFalse(off.payload["enabled"])
        self.assertIsNone(off.payload["next_due_at_utc"])
        # STATE, NOT DEFINITION: enablement does not move the revision, so the
        # number the operator read stays the one the next verb must name.
        self.assertEqual(1, off.payload["revision"])
        # Disabling stops FUTURE eligibility only, and says so.
        self.assertFalse(off.payload["open_generation_stopped"])

        on = job_enablement_result(self.memory, self.settings, job_id, 1, True)
        self.assertEqual(EXIT_OK, on.exit_code, on.payload)
        self.assertTrue(on.payload["enabled"])
        self.assertIsNotNone(on.payload["next_due_at_utc"])
        self.assertEqual(1, on.payload["revision"])
        self.assertFalse(on.payload["missed_occurrences_recovered"])

    def test_a_revision_that_is_not_the_current_one_is_refused(self) -> None:
        payload, _record = self.created()
        job_id = payload["job_id"]

        for bad in (2, 99):
            stale = job_enablement_result(self.memory, self.settings, job_id, bad, False)
            self.assertEqual(EXIT_INVALID_INPUT, stale.exit_code, stale.payload)
        # Not acted on at all: still enabled, still revision 1.
        record = self.memory.load_job(job_id)
        self.assertTrue(record.enabled)
        self.assertEqual(1, record.revision)

    def test_an_unknown_job_is_refused(self) -> None:
        result = job_enablement_result(
            self.memory, self.settings, "no-such-job", 1, False
        )
        self.assertEqual(EXIT_INVALID_INPUT, result.exit_code, result.payload)

    def test_archive_ends_the_future_and_keeps_everything_else(self) -> None:
        payload, _record = self.created()
        job_id = payload["job_id"]

        archived = job_archive_result(self.memory, self.settings, job_id, 1)
        self.assertEqual(EXIT_OK, archived.exit_code, archived.payload)
        self.assertTrue(archived.payload["archived"])
        self.assertFalse(archived.payload["enabled"])
        self.assertIsNone(archived.payload["next_due_at_utc"])
        self.assertFalse(archived.payload["history_deleted"])
        self.assertFalse(archived.payload["reversible"])
        # The definition itself is still there, and still readable.
        self.assertEqual(1, self.table_count("jobs"))
        self.assertIsNotNone(self.memory.load_job(job_id))

    def test_an_archived_job_cannot_be_re_enabled_or_archived_again(self) -> None:
        payload, _record = self.created()
        job_id = payload["job_id"]
        first = job_archive_result(self.memory, self.settings, job_id, 1)
        revision = first.payload["revision"]

        again = job_archive_result(self.memory, self.settings, job_id, revision)
        self.assertEqual(EXIT_INVALID_INPUT, again.exit_code, again.payload)
        revived = job_enablement_result(
            self.memory, self.settings, job_id, revision, True
        )
        self.assertEqual(EXIT_INVALID_INPUT, revived.exit_code, revived.payload)
        record = self.memory.load_job(job_id)
        self.assertTrue(record.archived)
        self.assertFalse(record.enabled)


# --------------------------------------------------------------------------
# CLI - the five verbs, wired the local-only way
# --------------------------------------------------------------------------


class CommandTests(OperatorSurfaceTestCase):
    def test_the_five_verbs_run_end_to_end_through_the_cli(self) -> None:
        path = self.spec_file(interval_spec())
        self.assertEqual(
            EXIT_OK, main(["job", "create", "--spec-json", str(path)], settings=self.settings)
        )
        self.assertEqual(EXIT_OK, main(["job", "list"], settings=self.settings))
        job_id = self.jobs()["jobs"][0]["id"]
        self.assertEqual(
            EXIT_OK,
            main(
                ["job", "disable", "--job-id", job_id, "--revision", "1"],
                settings=self.settings,
            ),
        )
        self.assertEqual(
            EXIT_OK,
            main(
                ["job", "enable", "--job-id", job_id, "--revision", "1"],
                settings=self.settings,
            ),
        )
        self.assertEqual(
            EXIT_OK,
            main(
                ["job", "archive", "--job-id", job_id, "--revision", "1"],
                settings=self.settings,
            ),
        )
        record = self.memory.load_job(job_id)
        self.assertTrue(record.archived)

    def test_a_condition_watch_is_creatable_through_the_cli(self) -> None:
        path = self.spec_file(watch_spec())
        self.assertEqual(
            EXIT_OK,
            main(["job", "create", "--spec-json", str(path)], settings=self.settings),
        )
        self.assertEqual("condition_watch", self.jobs()["jobs"][0]["kind"])

    def test_an_unreadable_specification_fails_closed(self) -> None:
        missing = str(self.root / "absent.json")
        self.assertEqual(
            EXIT_INVALID_INPUT,
            main(["job", "create", "--spec-json", missing], settings=self.settings),
        )
        self.assertEqual(0, self.table_count("jobs"))

    def test_a_non_integer_revision_is_refused_by_the_parser(self) -> None:
        payload, _record = self.created()
        self.assertEqual(
            EXIT_INVALID_INPUT,
            main(
                [
                    "job",
                    "disable",
                    "--job-id",
                    payload["job_id"],
                    "--revision",
                    "latest",
                ],
                settings=self.settings,
            ),
        )
        self.assertTrue(self.memory.load_job(payload["job_id"]).enabled)

    def test_job_without_a_sub_command_is_refused(self) -> None:
        self.assertEqual(
            EXIT_INVALID_INPUT, main(["job"], settings=self.settings)
        )


if __name__ == "__main__":
    unittest.main()
