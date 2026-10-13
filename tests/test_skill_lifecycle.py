"""V2.2 S1 skill lifecycle: adaptation, versions, rollback, health and status."""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import itertools
import os
import subprocess
import sys
import unittest
from pathlib import Path

from jarvis import skill_lifecycle
from jarvis.skill_lifecycle import (
    CHANGEABLE_FIELDS,
    STALE_AFTER,
    SkillEvent,
    SkillHistory,
    SkillLifecycleError,
    active_skill,
    active_version,
    adapt,
    health,
    next_version,
    rollback_event,
    stale_mark_event,
    status,
    usable_skill,
    use_events,
)
from jarvis.skill_types import (
    FORBIDDEN_KEY_PATTERN,
    ID_PATTERN,
    AuthorityKeyError,
    Experience,
    ExperienceKind,
    Skill,
    SkillEventKind,
    SkillHealth,
    SkillInput,
    SkillOrigin,
    SkillProvenance,
    SkillRecordError,
    SkillScope,
    SkillScopeKind,
    SkillStatus,
    SkillStep,
)

NOW = "2026-10-14T01:20:00+00:00"
LATER = "2026-10-14T09:30:15.250000+00:00"

GLOBAL = SkillScope(kind=SkillScopeKind.GLOBAL, project_id=None)
PROJECT = SkillScope(kind=SkillScopeKind.PROJECT, project_id="jarvis-v1")
OTHER_PROJECT = SkillScope(kind=SkillScopeKind.PROJECT, project_id="arsaplan")

K = SkillEventKind


def make_step(index=1, **overrides):
    values = {
        "index": index,
        "action": "Read the open issues of the repository",
        "capability": "github_issue_list",
        "inputs": ("repo",),
        "check": "The issue list was read",
    }
    values.update(overrides)
    return SkillStep(**values)


def make_skill(skill_id="weekly_report", version=1, *, scope=GLOBAL, provenance=None, **extra):
    if provenance is None:
        provenance = SkillProvenance(
            origin=SkillOrigin.TAUGHT_FROM_RUN,
            source_run_id="run_1",
            parent_skill_id=None,
            parent_version=None,
            created_at=NOW,
        )
    values = {
        "skill_id": skill_id,
        "version": version,
        "name": "Weekly issue report",
        "summary": "Collect the open issues and write the weekly report",
        "scope": scope,
        "inputs": (SkillInput(name="repo", description="The repository", required=True),),
        "steps": (
            make_step(1),
            make_step(2, action="Write the report", capability=None, check=None),
        ),
        "checks": ("The report file exists",),
        "provenance": provenance,
    }
    values.update(extra)
    return Skill(**values)


def three_versions():
    v1 = make_skill()
    v2 = next_version(v1, {"summary": "Second take on the report"}, "user_edit", NOW)
    v3 = next_version(v2, {"summary": "Third take on the report"}, "user_edit", LATER)
    return v1, v2, v3


def event(kind, version=None, *, skill_id="weekly_report", at=NOW, experience_id=None):
    return SkillEvent(
        skill_id=skill_id, kind=kind, version=version, at=at, experience_id=experience_id
    )


def approved(version, at=NOW):
    return event(K.DRAFT_APPROVED, version, at=at)


def ok(version=1, at=NOW):
    return event(K.USED_SUCCESS, version, at=at)


def fail(version=1, at=NOW):
    return event(K.USED_FAILURE, version, at=at)


def history(versions=None, events=()):
    if versions is None:
        versions = three_versions()
    return SkillHistory(versions=tuple(versions), events=tuple(events))


def broken_workflow(skill_id="weekly_report", kind=ExperienceKind.BROKEN_WORKFLOW):
    return Experience(
        experience_id="exp_1",
        kind=kind,
        skill_id=skill_id,
        task_signature=("weekly", "report"),
        text="The report step failed where it used to succeed",
        scope=GLOBAL,
        source_run_id="run_7",
        created_at=NOW,
    )


class AdaptTests(unittest.TestCase):
    def test_adapt_makes_a_new_skill_with_parent_provenance(self):
        parent = next_version(make_skill(), {"summary": "Second take"}, "user_edit", NOW)
        child = adapt(parent, {"name": "Monthly issue report"}, LATER, skill_id="monthly_report")
        self.assertEqual(child.skill_id, "monthly_report")
        self.assertEqual(child.version, 1)
        self.assertEqual(child.name, "Monthly issue report")
        self.assertEqual(child.steps, parent.steps)
        prov = child.provenance
        self.assertIs(prov.origin, SkillOrigin.ADAPTED)
        self.assertEqual((prov.parent_skill_id, prov.parent_version), ("weekly_report", 2))
        self.assertEqual(prov.created_at, LATER)
        self.assertIsNone(prov.source_run_id)

    def test_adapt_keeps_the_parent_equal_to_a_deep_copy_taken_before(self):
        parent = make_skill()
        before = copy.deepcopy(parent)
        before_data = parent.to_dict()
        adapt(
            parent,
            {"steps": (make_step(1), make_step(2, action="Mail the report")), "checks": ()},
            NOW,
            skill_id="mailed_report",
        )
        self.assertEqual(parent, before)
        self.assertEqual(parent.to_dict(), before_data)
        self.assertIs(parent.provenance.origin, SkillOrigin.TAUGHT_FROM_RUN)

    def test_adapt_mints_a_fresh_valid_id_when_none_is_given(self):
        parent = make_skill()
        first = adapt(parent, {"name": "Copy one"}, NOW)
        second = adapt(parent, {"name": "Copy one"}, NOW)
        for child in (first, second):
            self.assertIsNotNone(ID_PATTERN.fullmatch(child.skill_id))
            self.assertNotEqual(child.skill_id, parent.skill_id)
        self.assertNotEqual(first.skill_id, second.skill_id)

    def test_adapt_records_a_source_run(self):
        child = adapt(make_skill(), {"name": "Other"}, NOW, skill_id="other", source_run_id="run_5")
        self.assertEqual(child.provenance.source_run_id, "run_5")

    def test_adapt_accepts_plain_data_changes(self):
        # v3 (D15): the parent is a project skill widened to global as plain data
        # (a global parent may no longer move into a project).
        child = adapt(
            make_skill(scope=PROJECT),
            {"steps": [make_step(1).to_dict()], "scope": GLOBAL.to_dict()},
            NOW,
            skill_id="one_step",
        )
        self.assertEqual(child.steps, (make_step(1),))
        self.assertEqual(child.scope, GLOBAL)

    def test_adapt_refuses_the_parents_id_and_bad_ids(self):
        for bad in ("weekly_report", "Weekly", "", "a" * 65, 7):
            with self.subTest(skill_id=bad), self.assertRaises(SkillLifecycleError):
                adapt(make_skill(), {"name": "Other"}, NOW, skill_id=bad)

    def test_adapt_refuses_a_non_skill_parent(self):
        for bad in (None, make_skill().to_dict(), "weekly_report"):
            with self.subTest(parent=bad), self.assertRaises(SkillLifecycleError):
                adapt(bad, {"name": "Other"}, NOW, skill_id="other")

    def test_adapt_refuses_an_unchanged_procedure(self):
        for changes in ({}, {"name": "Weekly issue report"}):
            with self.subTest(changes=changes), self.assertRaises(SkillLifecycleError):
                adapt(make_skill(), changes, NOW, skill_id="other")

    def test_adapt_refuses_a_deleted_parent_given_its_history(self):
        v1, v2, v3 = three_versions()
        for events in ((event(K.DELETED),), (approved(3), event(K.DELETED), event(K.ENABLED))):
            hist = history(events=events)
            with self.subTest(events=events), self.assertRaises(SkillLifecycleError):
                adapt(v3, {"name": "Other"}, NOW, skill_id="other", history=hist)
        # Without the history the deletion cannot be seen (SKSTORE must pass it).
        self.assertEqual(adapt(v3, {"name": "Other"}, NOW, skill_id="other").version, 1)
        # A disabled or stale parent may still be adapted.
        for events in ((event(K.DISABLED),), (fail(3), fail(3), fail(3))):
            hist = history(events=events)
            child = adapt(v3, {"name": "Other"}, NOW, skill_id="other", history=hist)
            self.assertEqual(child.provenance.parent_version, 3)

    def test_adapt_refuses_a_parent_that_is_not_in_the_given_history(self):
        hist = history()
        for parent in (make_skill("other_skill"), make_skill(summary="Not saved like this")):
            with self.subTest(parent=parent.skill_id), self.assertRaises(SkillLifecycleError):
                adapt(parent, {"name": "Other"}, NOW, skill_id="third", history=hist)
        for bad in (object(), three_versions(), hist.events):
            with self.subTest(history=bad), self.assertRaises(SkillLifecycleError):
                adapt(hist.get(1), {"name": "Other"}, NOW, skill_id="third", history=bad)

    def test_adapt_refuses_a_bad_timestamp(self):
        for bad in ("2026-10-14 01:20:00", "2026-10-14T01:20:00", "now", None):
            with self.subTest(now=bad), self.assertRaises(SkillRecordError):
                adapt(make_skill(), {"name": "Other"}, bad, skill_id="other")


class ChangeTests(unittest.TestCase):
    """Rules shared by adapt and next_version."""

    def build(self, changes, parent=None, mode="adapt"):
        parent = parent or make_skill()
        if mode == "adapt":
            return adapt(parent, changes, NOW, skill_id="other")
        return next_version(parent, changes, SkillOrigin.USER_EDIT, NOW)

    def test_changeable_fields_are_the_procedure_only(self):
        self.assertEqual(
            CHANGEABLE_FIELDS, {"name", "summary", "scope", "inputs", "steps", "checks"}
        )

    def test_lifecycle_fields_cannot_be_changed(self):
        for mode in ("adapt", "version"):
            for key, value in (
                ("skill_id", "elsewhere"),
                ("version", 9),
                ("provenance", make_skill().provenance),
            ):
                with self.subTest(mode=mode, key=key), self.assertRaises(SkillLifecycleError):
                    self.build({key: value, "name": "Changed"}, mode=mode)

    def test_authority_keys_are_refused_at_any_depth(self):
        step_with_authority = dict(make_step(1).to_dict(), auto_run=True)
        cases = (
            {"approved": True},
            {"requires_approval": False},
            {"risk_tier": "low"},
            {"Side-Effect": "none"},
            {"remember_for_session": True},
            {"steps": [step_with_authority]},
        )
        for mode in ("adapt", "version"):
            for changes in cases:
                with self.subTest(mode=mode, changes=changes), self.assertRaises(
                    AuthorityKeyError
                ):
                    self.build(changes, mode=mode)

    def test_unknown_keys_and_bad_values_are_refused(self):
        cases = (
            {"status": "active"},
            {"health": None},
            {"name": ""},
            {"steps": ()},
            {"steps": (make_step(2),)},
            {"inputs": ()},  # step 1 names the input it would lose
            {"checks": "not a list"},
            {1: "x"},
        )
        for mode in ("adapt", "version"):
            for changes in cases:
                with self.subTest(mode=mode, changes=changes), self.assertRaises(
                    SkillRecordError
                ):
                    self.build(changes, mode=mode)

    def test_changes_must_be_a_mapping(self):
        for bad in (None, [("name", "x")], "name"):
            with self.subTest(changes=bad), self.assertRaises(SkillLifecycleError):
                self.build(bad)

    def test_a_project_procedure_never_moves_to_another_project(self):
        parent = make_skill(scope=PROJECT)
        for mode in ("adapt", "version"):
            for scope in (OTHER_PROJECT, OTHER_PROJECT.to_dict()):
                with self.subTest(mode=mode, scope=scope), self.assertRaises(
                    SkillLifecycleError
                ):
                    self.build({"scope": scope}, parent=parent, mode=mode)
            same = self.build({"name": "Same project"}, parent=parent, mode=mode)
            self.assertEqual(same.scope, PROJECT)

    def test_a_project_procedure_may_become_global(self):
        # D15 / D8: the user may widen a project procedure to global on the card.
        parent = make_skill(scope=PROJECT)
        for mode in ("adapt", "version"):
            with self.subTest(mode=mode):
                child = self.build({"scope": GLOBAL}, parent=parent, mode=mode)
                self.assertEqual(child.scope, GLOBAL)

    def test_a_global_procedure_never_moves_into_a_project(self):
        # D15 (completed, v3): global -> project is refused (was accepted in v2).
        for mode in ("adapt", "version"):
            for scope in (PROJECT, PROJECT.to_dict(), OTHER_PROJECT):
                with self.subTest(mode=mode, scope=scope), self.assertRaises(
                    SkillLifecycleError
                ):
                    self.build({"scope": scope, "summary": "Narrowed"}, mode=mode)
            same = self.build({"scope": GLOBAL, "summary": "Still global"}, mode=mode)
            self.assertEqual(same.scope, GLOBAL)

    def test_a_global_version_never_moves_into_a_project_with_its_history(self):
        # D15 build side as SKSTORE calls it: next_version and adapt with history=.
        v1 = make_skill()
        hist = SkillHistory(versions=(v1,), events=(approved(1),))
        with self.assertRaises(SkillLifecycleError):
            next_version(v1, {"scope": PROJECT}, SkillOrigin.USER_EDIT, NOW, history=hist)
        with self.assertRaises(SkillLifecycleError):
            adapt(v1, {"scope": PROJECT}, NOW, skill_id="other", history=hist)
        v2 = next_version(
            v1, {"summary": "Still global"}, SkillOrigin.USER_EDIT, NOW, history=hist
        )
        self.assertEqual(v2.scope, GLOBAL)

    def test_project_a_to_global_to_project_b_is_refused_at_the_second_move(self):
        # D15: widening to global does not open a path into another project.
        v1 = make_skill(scope=PROJECT)
        hist1 = SkillHistory(versions=(v1,), events=(approved(1),))
        v2 = next_version(v1, {"scope": GLOBAL}, SkillOrigin.USER_EDIT, NOW, history=hist1)
        self.assertEqual(v2.scope, GLOBAL)
        hist2 = SkillHistory(versions=(v1, v2), events=(approved(1), approved(2)))
        for target in (OTHER_PROJECT, PROJECT):
            with self.subTest(mode="version", target=target), self.assertRaises(
                SkillLifecycleError
            ):
                next_version(v2, {"scope": target}, SkillOrigin.USER_EDIT, LATER, history=hist2)
        widened = adapt(v1, {"scope": GLOBAL}, NOW, skill_id="widened")
        self.assertEqual(widened.scope, GLOBAL)
        for target in (OTHER_PROJECT, PROJECT):
            with self.subTest(mode="adapt", target=target), self.assertRaises(
                SkillLifecycleError
            ):
                adapt(widened, {"scope": target}, LATER, skill_id="moved")

    def test_cyclic_or_very_deep_changes_raise_the_lifecycle_error(self):
        cyclic_list = []
        cyclic_list.append(cyclic_list)
        cyclic_dict = dict(make_step(1).to_dict())
        cyclic_dict["inputs"] = [cyclic_dict]
        very_deep = []
        for _ in range(100_000):
            very_deep = [very_deep]
        deep = []
        for _ in range(40):
            deep = [deep]
        cases = (
            {"checks": cyclic_list},
            {"steps": [cyclic_dict]},
            {"steps": very_deep},
            {"checks": deep},
        )
        for mode in ("adapt", "version"):
            for number, changes in enumerate(cases):
                with self.subTest(mode=mode, case=number), self.assertRaises(
                    SkillLifecycleError
                ):
                    self.build(changes, mode=mode)

    def test_documented_pass_through_errors_keep_their_types(self):
        # Callers catch SkillRecordError, the common base of all three.
        for mode in ("adapt", "version"):
            with self.subTest(mode=mode, error="bad now"):
                with self.assertRaises(SkillRecordError) as caught:
                    if mode == "adapt":
                        adapt(make_skill(), {"name": "Other"}, "yesterday", skill_id="other")
                    else:
                        next_version(make_skill(), {"name": "Other"}, "user_edit", "yesterday")
                self.assertNotIsInstance(caught.exception, SkillLifecycleError)
            with self.subTest(mode=mode, error="authority"):
                bad_step = dict(make_step(1).to_dict(), approved=True)
                with self.assertRaises(AuthorityKeyError):
                    self.build({"steps": [bad_step]}, mode=mode)
            with self.subTest(mode=mode, error="unknown key"):
                with self.assertRaises(SkillRecordError) as caught:
                    self.build({"colour": "red"}, mode=mode)
                self.assertNotIsInstance(caught.exception, SkillLifecycleError)
        self.assertTrue(issubclass(SkillLifecycleError, SkillRecordError))
        self.assertTrue(issubclass(AuthorityKeyError, SkillRecordError))


class NextVersionTests(unittest.TestCase):
    def test_next_version_increments_and_keeps_provenance(self):
        v1 = make_skill()
        v1_before = copy.deepcopy(v1)
        for origin in (SkillOrigin.USER_EDIT, SkillOrigin.VERSION_AFTER_FAILURE):
            for spelled in (origin, origin.value):
                with self.subTest(origin=spelled):
                    v2 = next_version(
                        v1, {"summary": "Fixed report"}, spelled, LATER, source_run_id="run_9"
                    )
                    self.assertEqual(v2.skill_id, v1.skill_id)
                    self.assertEqual(v2.version, 2)
                    self.assertIs(v2.provenance.origin, origin)
                    self.assertEqual(
                        (v2.provenance.parent_skill_id, v2.provenance.parent_version),
                        ("weekly_report", 1),
                    )
                    self.assertEqual(v2.provenance.source_run_id, "run_9")
                    self.assertEqual(v2.provenance.created_at, LATER)
                    self.assertEqual(v1, v1_before)

    def test_the_lineage_chain_points_one_version_back(self):
        v1, v2, v3 = three_versions()
        self.assertEqual(v3.version, 3)
        self.assertEqual(v3.provenance.parent_version, 2)
        self.assertEqual(v2.provenance.parent_version, 1)
        self.assertIs(v1.provenance.origin, SkillOrigin.TAUGHT_FROM_RUN)

    def test_old_versions_in_a_history_are_untouched(self):
        hist = history(events=(approved(1), approved(2), approved(3)))
        snapshot = copy.deepcopy(hist)
        v4 = next_version(hist.get(3), {"summary": "Fourth"}, "user_edit", NOW, history=hist)
        self.assertEqual(v4.version, 4)
        self.assertEqual(hist, snapshot)

    def test_editing_an_older_version_takes_the_next_free_number(self):
        hist = history(events=(approved(1), approved(2), approved(3), event(K.ROLLED_BACK, 1)))
        new = next_version(hist.get(1), {"summary": "Fresh"}, "user_edit", NOW, history=hist)
        self.assertEqual(new.version, 4)
        self.assertEqual(new.provenance.parent_version, 1)

    def test_next_version_refuses_new_skill_origins_and_junk(self):
        for origin in (SkillOrigin.ADAPTED, SkillOrigin.TAUGHT_FROM_RUN, "adapted", "edit", None, 1):
            with self.subTest(origin=origin), self.assertRaises(SkillLifecycleError):
                next_version(make_skill(), {"summary": "x y"}, origin, NOW)

    def test_next_version_refuses_a_deleted_skill(self):
        hist = history(events=(approved(1), event(K.DELETED)))
        with self.assertRaises(SkillLifecycleError):
            next_version(hist.get(3), {"summary": "x y"}, "user_edit", NOW, history=hist)

    def test_next_version_refuses_a_skill_that_is_not_in_the_history(self):
        hist = history()
        stranger = make_skill("other_skill")
        altered = make_skill(summary="Not the saved version one")
        for skill in (stranger, altered):
            with self.subTest(skill=skill.skill_id), self.assertRaises(SkillLifecycleError):
                next_version(skill, {"summary": "x y"}, "user_edit", NOW, history=hist)
        with self.assertRaises(SkillLifecycleError):
            next_version(make_skill(), {"summary": "x y"}, "user_edit", NOW, history=object())

    def test_next_version_refuses_an_unchanged_procedure(self):
        with self.assertRaises(SkillLifecycleError):
            next_version(make_skill(), {}, "user_edit", NOW)

    def test_next_version_refuses_a_non_skill(self):
        with self.assertRaises(SkillLifecycleError):
            next_version(make_skill().to_dict(), {"summary": "x y"}, "user_edit", NOW)


class RollbackTests(unittest.TestCase):
    def test_rollback_moves_the_pointer_and_keeps_every_version(self):
        hist = history(events=(approved(1), approved(2), approved(3)))
        versions_before = copy.deepcopy(hist.versions)
        self.assertEqual(active_version(hist), 3)
        rolled = rollback_event(hist, 1, LATER)
        self.assertEqual(
            rolled,
            SkillEvent(skill_id="weekly_report", kind=K.ROLLED_BACK, version=1, at=LATER),
        )
        after = SkillHistory(versions=hist.versions, events=hist.events + (rolled,))
        self.assertEqual(active_version(after), 1)
        self.assertEqual(active_skill(after), versions_before[0])
        self.assertEqual(after.versions, versions_before)
        forward = rollback_event(after, 3, LATER)
        again = SkillHistory(versions=hist.versions, events=after.events + (forward,))
        self.assertEqual(active_version(again), 3)

    def test_a_newly_approved_version_becomes_active_after_a_rollback(self):
        hist = history(events=(approved(1), approved(2), event(K.ROLLED_BACK, 1), approved(3)))
        self.assertEqual(active_version(hist), 3)

    def test_without_pointer_events_the_latest_version_is_active(self):
        self.assertEqual(active_version(history()), 3)
        self.assertEqual(active_skill(history()).version, 3)

    def test_rollback_to_an_unknown_version_is_refused(self):
        hist = history(events=(approved(3),))
        for target in (0, 4, 99, -1, "1", True, None, 1.0):
            with self.subTest(target=target), self.assertRaises(SkillLifecycleError):
                rollback_event(hist, target, NOW)

    def test_rollback_of_a_deleted_skill_is_refused(self):
        for events in (
            (approved(3), event(K.DELETED)),
            (event(K.DELETED), event(K.ENABLED)),
            (event(K.ROLLED_BACK, 2), event(K.DELETED)),
        ):
            hist = history(events=events)
            with self.subTest(events=events), self.assertRaises(SkillLifecycleError):
                rollback_event(hist, 1, NOW)

    def test_rollback_to_the_active_version_is_refused(self):
        with self.assertRaises(SkillLifecycleError):
            rollback_event(history(events=(event(K.ROLLED_BACK, 2),)), 2, NOW)

    def test_rollback_refuses_a_non_history(self):
        with self.assertRaises(SkillLifecycleError):
            rollback_event(three_versions(), 1, NOW)
        with self.assertRaises(SkillLifecycleError):
            active_version(three_versions())


class StatusTests(unittest.TestCase):
    def test_status_precedence_table(self):
        # Every combination of the three conditions; the strongest one wins.
        for deleted, disabled, stale in itertools.product((False, True), repeat=3):
            events = [ok()]
            if stale:
                events += [fail(), fail(), fail()]
            if disabled:
                events.append(event(K.DISABLED))
            if deleted:
                events.append(event(K.DELETED))
            expected = (
                SkillStatus.DELETED if deleted
                else SkillStatus.DISABLED if disabled
                else SkillStatus.STALE if stale
                else SkillStatus.ACTIVE
            )
            with self.subTest(deleted=deleted, disabled=disabled, stale=stale):
                self.assertIs(status(events), expected)

    def test_no_events_is_active(self):
        self.assertIs(status(()), SkillStatus.ACTIVE)

    def test_the_last_enable_or_disable_wins(self):
        self.assertIs(status([event(K.DISABLED), event(K.ENABLED)]), SkillStatus.ACTIVE)
        self.assertIs(status([event(K.ENABLED), event(K.DISABLED)]), SkillStatus.DISABLED)

    def test_deleted_is_final(self):
        events = [event(K.DELETED), event(K.ENABLED), ok(), event(K.REVALIDATED)]
        self.assertIs(status(events), SkillStatus.DELETED)

    def test_stale_after_three_consecutive_failures(self):
        self.assertEqual(STALE_AFTER, 3)
        events = [ok()]
        for count in range(1, 6):
            events.append(fail())
            with self.subTest(failures=count):
                expected = SkillStatus.STALE if count >= STALE_AFTER else SkillStatus.ACTIVE
                self.assertIs(status(events), expected)

    def test_a_success_clears_stale(self):
        events = [fail(), fail(), fail()]
        self.assertIs(status(events), SkillStatus.STALE)
        events.append(ok(at=LATER))
        self.assertIs(status(events), SkillStatus.ACTIVE)
        self.assertEqual(health(events).consecutive_failures, 0)

    def test_failures_split_by_a_success_are_not_consecutive(self):
        events = [fail(), fail(), ok(), fail(), fail()]
        self.assertIs(status(events), SkillStatus.ACTIVE)
        self.assertIs(status(events + [fail()]), SkillStatus.STALE)

    def test_revalidated_alone_does_not_clear_stale(self):
        self.assertIs(status([fail(), fail(), fail(), event(K.REVALIDATED)]), SkillStatus.STALE)

    def test_a_linked_broken_workflow_makes_stale_until_a_success(self):
        marked = stale_mark_event(broken_workflow(), LATER)
        self.assertEqual(marked.kind, K.STALE_MARKED)
        self.assertEqual(marked.experience_id, "exp_1")
        self.assertEqual(marked.skill_id, "weekly_report")
        self.assertIs(status([ok(), marked]), SkillStatus.STALE)
        self.assertIs(status([ok(), marked, fail()]), SkillStatus.STALE)
        self.assertIs(status([ok(), marked, ok()]), SkillStatus.ACTIVE)
        self.assertIs(status([marked, event(K.DISABLED)]), SkillStatus.DISABLED)

    def test_rollback_and_new_versions_do_not_clear_stale(self):
        events = [fail(), fail(), fail(), event(K.ROLLED_BACK, 1), approved(3)]
        self.assertIs(status(events), SkillStatus.STALE)

    def test_draft_and_pointer_events_do_not_change_status(self):
        events = [approved(1), event(K.DRAFT_REJECTED), event(K.ROLLED_BACK, 1)]
        self.assertIs(status(events), SkillStatus.ACTIVE)

    def test_stale_mark_event_refuses_other_lessons(self):
        for kind in ExperienceKind:
            if kind is ExperienceKind.BROKEN_WORKFLOW:
                continue
            with self.subTest(kind=kind), self.assertRaises(SkillLifecycleError):
                stale_mark_event(broken_workflow(kind=kind), NOW)
        with self.assertRaises(SkillLifecycleError):
            stale_mark_event(broken_workflow(skill_id=None), NOW)
        with self.assertRaises(SkillLifecycleError):
            stale_mark_event(broken_workflow().to_dict(), NOW)


class HealthTests(unittest.TestCase):
    def test_health_counts_uses_in_order(self):
        events = [
            approved(1),
            ok(at="2026-10-14T01:00:00+00:00"),
            fail(at="2026-10-14T02:00:00+00:00"),
            ok(at="2026-10-14T03:00:00+00:00"),
            fail(at="2026-10-14T04:00:00+00:00"),
            fail(at="2026-10-14T05:00:00+00:00"),
            event(K.DISABLED),
        ]
        self.assertEqual(
            health(events),
            SkillHealth(
                use_count=5,
                success_count=2,
                failure_count=3,
                consecutive_failures=2,
                last_success_at="2026-10-14T03:00:00+00:00",
                last_failure_at="2026-10-14T05:00:00+00:00",
            ),
        )

    def test_empty_health(self):
        self.assertEqual(
            health(()),
            SkillHealth(
                use_count=0,
                success_count=0,
                failure_count=0,
                consecutive_failures=0,
                last_success_at=None,
                last_failure_at=None,
            ),
        )

    def test_health_and_status_refuse_mixed_or_foreign_input(self):
        mixed = [ok(), event(K.USED_FAILURE, 1, skill_id="other_skill")]
        for bad in (
            mixed, [ok().to_dict()], "events", {"a": ok()}, [None], None, 5, b"x", iter(()),
        ):
            for function in (health, status):
                with self.subTest(function=function.__name__, events=bad), self.assertRaises(
                    SkillLifecycleError
                ):
                    function(bad)


class UseEventsTests(unittest.TestCase):
    def test_a_plain_success_and_failure(self):
        hist = history(events=(approved(3),))
        self.assertEqual(use_events(hist, 3, True, LATER), (ok(3, LATER),))
        self.assertEqual(use_events(hist, 3, False, LATER), (fail(3, LATER),))

    def test_the_failure_that_makes_a_skill_stale_also_marks_it(self):
        hist = history(events=(fail(3), fail(3)))
        events = use_events(hist, 3, False, LATER)
        self.assertEqual([item.kind for item in events], [K.USED_FAILURE, K.STALE_MARKED])
        after = hist.events + events
        self.assertIs(status(after), SkillStatus.STALE)
        more = use_events(history(events=after), 3, False, LATER)
        self.assertEqual([item.kind for item in more], [K.USED_FAILURE])

    def test_a_success_on_a_stale_skill_revalidates_it(self):
        hist = history(events=(fail(3), fail(3), fail(3)))
        events = use_events(hist, 3, True, LATER)
        self.assertEqual([item.kind for item in events], [K.USED_SUCCESS, K.REVALIDATED])
        self.assertIs(status(hist.events + events), SkillStatus.ACTIVE)

    def test_use_is_refused_for_deleted_disabled_and_unknown(self):
        for events, version in (
            ((event(K.DELETED),), 3),
            ((event(K.DISABLED),), 3),
            ((), 7),
            ((), "3"),
        ):
            hist = history(events=events)
            with self.subTest(events=events, version=version), self.assertRaises(
                SkillLifecycleError
            ):
                use_events(hist, version, True, NOW)
        with self.assertRaises(SkillLifecycleError):
            use_events(history(), 3, 1, NOW)


class OrderedEventsTests(unittest.TestCase):
    """health/status take an ORDERED sequence: tuple, list or a SkillHistory."""

    PROBE = (fail(at=NOW), fail(at=LATER), fail(at="2026-10-14T11:00:00+00:00"), ok())

    def test_unordered_collections_are_refused(self):
        probe = self.PROBE
        unordered = (
            set(probe),
            frozenset(probe),
            set(),
            frozenset(),
            {ok()},
            dict.fromkeys(probe).keys(),
            {item: 1 for item in probe}.values(),
            (item for item in probe),
            iter(probe),
            reversed(probe),
        )
        for function in (health, status):
            for bad in unordered:
                with self.subTest(function=function.__name__, kind=type(bad).__name__):
                    with self.assertRaises(SkillLifecycleError):
                        function(bad)

    def test_tuple_list_and_history_give_the_same_answer(self):
        probe = self.PROBE
        hist = SkillHistory(versions=(make_skill(),), events=probe)
        for source in (probe, list(probe), hist):
            with self.subTest(kind=type(source).__name__):
                self.assertIs(status(source), SkillStatus.ACTIVE)
                self.assertEqual(health(source).consecutive_failures, 0)

    def test_results_do_not_depend_on_the_hash_seed(self):
        # The reviewer's probe: as a set its order (and so its status) depended
        # on PYTHONHASHSEED. Now the set is refused and the tuple is stable.
        code = chr(10).join(
            (
                "from jarvis.skill_lifecycle import SkillEvent, SkillLifecycleError, status",
                "from jarvis.skill_types import SkillEventKind as K",
                "ats = ['2026-10-14T01:00:00+00:00', '2026-10-14T02:00:00+00:00',",
                "       '2026-10-14T03:00:00+00:00', '2026-10-14T04:00:00+00:00']",
                "kinds = [K.USED_FAILURE] * 3 + [K.USED_SUCCESS]",
                "ev = [SkillEvent(skill_id='s', kind=k, version=1, at=a)",
                "      for k, a in zip(kinds, ats)]",
                "try:",
                "    status(set(ev))",
                "    print('accepted')",
                "except SkillLifecycleError:",
                "    print('refused')",
                "print(status(tuple(ev)).value, status(tuple(reversed(ev))).value)",
            )
        )
        root = Path(__file__).resolve().parent.parent
        outputs = set()
        for seed in ("0", "1", "2", "3", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            # Fixed argv, our own interpreter, no shell.
            result = subprocess.run(  # nosec B603
                [sys.executable, "-c", code],
                cwd=root, env=env, capture_output=True, text=True, timeout=60, check=True,
            )
            outputs.add(result.stdout)
        self.assertEqual(outputs, {"refused" + chr(10) + "active stale" + chr(10)})


class ActiveVersionStaleTests(unittest.TestCase):
    """S12: only a success of the ACTIVE version clears stale."""

    def stale_on_three(self):
        return (approved(1), approved(2), approved(3), fail(3), fail(3), fail(3))

    def test_a_success_of_an_older_version_does_not_clear_stale(self):
        events = self.stale_on_three() + (ok(1, LATER),)
        for source in (events, history(events=events)):
            with self.subTest(kind=type(source).__name__):
                self.assertIs(status(source), SkillStatus.STALE)
                self.assertEqual(health(source).consecutive_failures, 3)
                self.assertEqual(health(source).success_count, 1)
        cleared = events + (ok(3, LATER),)
        self.assertIs(status(cleared), SkillStatus.ACTIVE)
        self.assertEqual(health(cleared).consecutive_failures, 0)

    def test_after_a_rollback_only_the_rolled_back_to_version_clears(self):
        events = self.stale_on_three() + (event(K.ROLLED_BACK, 1),)
        self.assertIs(status(events), SkillStatus.STALE)
        self.assertIs(status(events + (ok(3),)), SkillStatus.STALE)
        self.assertIs(status(events + (ok(1),)), SkillStatus.ACTIVE)

    def test_a_marked_skill_is_cleared_only_by_the_active_version(self):
        marked = stale_mark_event(broken_workflow(), LATER)
        events = (approved(1), approved(2), marked)
        self.assertIs(status(events + (ok(1),)), SkillStatus.STALE)
        self.assertIs(status(events + (ok(2),)), SkillStatus.ACTIVE)

    def test_without_pointer_events_the_history_names_the_active_version(self):
        events = (fail(1), fail(1), fail(1), ok(1))
        hist = history(events=events)  # holds v1..v3, so v3 is active
        self.assertIs(status(hist), SkillStatus.STALE)
        self.assertEqual(health(hist).consecutive_failures, 3)
        # A bare list only knows the versions it names; v1 is the newest there.
        self.assertIs(status(events), SkillStatus.ACTIVE)
        self.assertIs(status(events + (fail(3),)), SkillStatus.ACTIVE)
        self.assertIs(status((fail(3),) + events), SkillStatus.STALE)

    def test_use_events_revalidates_only_for_the_active_version(self):
        hist = history(events=self.stale_on_three())
        old = use_events(hist, 1, True, LATER)
        self.assertEqual(old, (ok(1, LATER),))
        after_old = history(events=hist.events + old)
        self.assertIs(status(after_old), SkillStatus.STALE)
        current = use_events(after_old, 3, True, LATER)
        self.assertEqual([item.kind for item in current], [K.USED_SUCCESS, K.REVALIDATED])
        self.assertIs(status(history(events=after_old.events + current)), SkillStatus.ACTIVE)


class UsableSkillTests(unittest.TestCase):
    def test_usable_skill_is_the_active_version_of_an_active_or_stale_skill(self):
        for events in ((), (approved(2),), (fail(3), fail(3), fail(3))):
            hist = history(events=events)
            with self.subTest(events=events):
                self.assertEqual(usable_skill(hist), active_skill(hist))
        self.assertEqual(usable_skill(history(events=(event(K.ROLLED_BACK, 1),))).version, 1)

    def test_a_deleted_or_disabled_skill_is_not_usable(self):
        for events in (
            (event(K.DELETED),),
            (event(K.DELETED), event(K.ENABLED)),
            (event(K.DISABLED),),
            (event(K.ENABLED), event(K.DISABLED)),
        ):
            hist = history(events=events)
            with self.subTest(events=events):
                self.assertIsNone(usable_skill(hist))
                # active_skill stays a display lookup.
                self.assertEqual(active_skill(hist).version, 3)
        self.assertIsNotNone(usable_skill(history(events=(event(K.DISABLED), event(K.ENABLED)))))

    def test_usable_skill_refuses_a_non_history(self):
        for bad in (None, three_versions(), (), make_skill()):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(SkillLifecycleError):
                usable_skill(bad)
            with self.subTest(bad=type(bad).__name__), self.assertRaises(SkillLifecycleError):
                active_skill(bad)


class OnlyExecutedRunsCountTests(unittest.TestCase):
    """D16: a declined announcement or a denied approval is not a use."""

    def test_only_used_success_and_used_failure_change_health(self):
        base = (approved(1), ok(), fail())
        before = health(base)
        for kind in SkillEventKind:
            extra = event(
                kind,
                1 if kind is not K.STALE_MARKED else None,
                experience_id="exp_1" if kind is K.STALE_MARKED else None,
            )
            after = health(base + (extra,) * 5)
            with self.subTest(kind=kind):
                if kind in (K.USED_SUCCESS, K.USED_FAILURE):
                    self.assertNotEqual(after, before)
                else:
                    self.assertEqual(after, before)

    def test_no_non_use_event_makes_a_skill_stale_or_less_healthy(self):
        for kind in (K.DRAFT_REJECTED, K.REVALIDATED, K.ENABLED, K.ROLLED_BACK, K.DRAFT_APPROVED):
            events = (approved(1),) + (event(kind, 1),) * 10
            with self.subTest(kind=kind):
                self.assertIs(status(events), SkillStatus.ACTIVE)
                self.assertEqual(health(events).failure_count, 0)

    def test_use_events_has_no_decline_or_denial_input(self):
        # A use is recorded only for an executed run; there is no way to pass
        # a decline or an approval denial (SKCORE records nothing for those).
        self.assertEqual(
            list(inspect.signature(use_events).parameters),
            ["history", "version", "succeeded", "now"],
        )
        for doc in (use_events.__doc__, health.__doc__, status.__doc__, skill_lifecycle.__doc__):
            text = " ".join(doc.split())
            for phrase in ("declined announcement", "denied", "not a use", "never a failure"):
                with self.subTest(doc=text[:30], phrase=phrase):
                    self.assertIn(phrase, text)


class SkillEventTests(unittest.TestCase):
    def test_every_kind_round_trips(self):
        for kind in SkillEventKind:
            with self.subTest(kind=kind):
                item = event(kind, 2, experience_id="exp_1" if kind is K.STALE_MARKED else None)
                data = item.to_dict()
                self.assertEqual(data["kind"], kind.value)
                self.assertEqual(SkillEvent.from_dict(data), item)

    def test_versioned_kinds_need_a_version(self):
        for kind in (K.USED_SUCCESS, K.USED_FAILURE, K.ROLLED_BACK, K.DRAFT_APPROVED):
            with self.subTest(kind=kind), self.assertRaises(SkillLifecycleError):
                event(kind, None)
        for kind in (K.ENABLED, K.DISABLED, K.DELETED, K.STALE_MARKED, K.REVALIDATED):
            self.assertIsNone(event(kind).version)

    def test_only_stale_marked_links_an_experience(self):
        for kind in SkillEventKind:
            if kind is K.STALE_MARKED:
                continue
            with self.subTest(kind=kind), self.assertRaises(SkillLifecycleError):
                event(kind, 1, experience_id="exp_1")
        with self.assertRaises(SkillLifecycleError):
            event(K.STALE_MARKED, experience_id="Bad Id")

    def test_bad_values_are_refused(self):
        cases = (
            {"skill_id": "Bad"},
            {"kind": "used_success"},
            {"version": 0},
            {"version": True},
            {"at": "2026-10-14T01:20:00"},
            {"at": "2026-10-14T04:20:00+03:00"},
        )
        base = {"skill_id": "s", "kind": K.USED_SUCCESS, "version": 1, "at": NOW}
        for change in cases:
            with self.subTest(change=change), self.assertRaises(SkillLifecycleError):
                SkillEvent(**dict(base, **change))

    def test_from_dict_is_strict(self):
        good = ok().to_dict()
        cases = (
            dict(good, extra=1),
            dict(good, approved=True),
            {key: value for key, value in good.items() if key != "experience_id"},
            dict(good, kind="used"),
            dict(good, kind=K.USED_SUCCESS.name),
            dict(good, kind=None),
            dict(good, version="1"),
            [good],
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises(SkillLifecycleError):
                SkillEvent.from_dict(data)

    def test_events_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ok().version = 2  # type: ignore[misc]


class SkillHistoryTests(unittest.TestCase):
    def test_ill_formed_histories_are_refused(self):
        v1, v2, v3 = three_versions()
        cases = (
            ((), ()),
            ([v1], ()),
            ((v1.to_dict(),), ()),
            ((v1, v1), ()),
            ((v1, make_skill("other_skill")), ()),
            ((v1, v2), (event(K.USED_SUCCESS, 1, skill_id="other_skill"),)),
            ((v1, v2), (ok(3),)),
            ((v1,), [ok(1)]),
            ((v1,), (ok(1).to_dict(),)),
        )
        for versions, events in cases:
            with self.subTest(versions=versions, events=events), self.assertRaises(
                SkillLifecycleError
            ):
                SkillHistory(versions=versions, events=events)

    def project_version(self, scope, parent_version=1, version=2):
        return make_skill(
            version=version,
            scope=scope,
            summary=f"Version {version}",
            provenance=SkillProvenance(
                origin=SkillOrigin.USER_EDIT,
                source_run_id=None,
                parent_skill_id="weekly_report",
                parent_version=parent_version,
                created_at=NOW,
            ),
        )

    def test_a_version_may_not_move_to_another_project(self):
        # Review probe 1 (D15): v1 in project P, v2 in project Q is refused.
        v1 = make_skill(scope=PROJECT)
        v2 = self.project_version(OTHER_PROJECT)
        for versions in ((v1, v2), (v2, v1)):
            for events in ((), (approved(2),), (ok(2),)):
                with self.subTest(order=[item.version for item in versions], events=events):
                    with self.assertRaises(SkillLifecycleError):
                        SkillHistory(versions=versions, events=events)

    def test_a_version_may_stay_in_its_project_or_become_global(self):
        v1 = make_skill(scope=PROJECT)
        for scope in (PROJECT, GLOBAL):
            hist = SkillHistory(versions=(v1, self.project_version(scope)), events=())
            with self.subTest(scope=scope.kind):
                self.assertEqual(active_skill(hist).scope, scope)
                self.assertEqual(usable_skill(hist).scope, scope)
        # An unchanged global scope is read as it is.
        hist = SkillHistory(versions=(make_skill(), self.project_version(GLOBAL)), events=())
        self.assertEqual(usable_skill(hist).scope, GLOBAL)

    def test_a_global_version_may_not_move_into_a_project(self):
        # D15 read side (v3; v2 accepted this history): v1 global, v2 in a project.
        v1 = make_skill(scope=GLOBAL)
        for scope in (PROJECT, OTHER_PROJECT):
            v2 = self.project_version(scope)
            for versions in ((v1, v2), (v2, v1)):
                for events in ((), (approved(2),), (ok(2),)):
                    with self.subTest(
                        scope=scope.project_id,
                        order=[item.version for item in versions],
                        events=events,
                    ), self.assertRaises(SkillLifecycleError):
                        SkillHistory(versions=versions, events=events)

    def test_a_read_chain_project_a_global_project_b_is_refused(self):
        # D15 read side: v1 project A -> v2 global (allowed) -> v3 project B (refused).
        v1 = make_skill(scope=PROJECT)
        v2 = self.project_version(GLOBAL)
        self.assertEqual(SkillHistory(versions=(v1, v2), events=()).latest_version, 2)
        for scope in (OTHER_PROJECT, PROJECT):
            v3 = self.project_version(scope, parent_version=2, version=3)
            with self.subTest(scope=scope.project_id), self.assertRaises(SkillLifecycleError):
                SkillHistory(versions=(v1, v2, v3), events=())

    def test_each_version_is_checked_against_its_own_parent(self):
        v1 = make_skill(scope=PROJECT)
        v2 = self.project_version(PROJECT)
        v3_other = self.project_version(OTHER_PROJECT, parent_version=2, version=3)
        with self.assertRaises(SkillLifecycleError):
            SkillHistory(versions=(v1, v2, v3_other), events=())
        # Parent not held (a broken row the store skipped): nothing to check against.
        hist = SkillHistory(versions=(self.project_version(OTHER_PROJECT),), events=())
        self.assertEqual(hist.latest_version, 2)

    def test_versions_may_come_in_any_order(self):
        v1, v2, v3 = three_versions()
        hist = SkillHistory(versions=(v3, v1, v2), events=())
        self.assertEqual(hist.latest_version, 3)
        self.assertEqual(hist.get(2), v2)
        with self.assertRaises(SkillLifecycleError):
            hist.get(4)


class AuthorityAndPurityTests(unittest.TestCase):
    def test_no_lifecycle_record_has_an_authority_field(self):
        for record in (SkillEvent, SkillHistory):
            for item in dataclasses.fields(record):
                with self.subTest(record=record.__name__, field=item.name):
                    self.assertIsNone(FORBIDDEN_KEY_PATTERN.search(item.name))

    def test_module_is_pure(self):
        source = Path(skill_lifecycle.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = {
            "__future__", "collections.abc", "dataclasses", "datetime", "enum", "typing",
            "uuid", "jarvis.skill_types",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module}
            else:
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "exec", "eval", "__import__"})
                continue
            with self.subTest(imports=names):
                self.assertLessEqual(names, allowed)
        self.assertNotIn("sqlite", source)
        self.assertNotIn("UPDATE ", source)
        self.assertNotIn("DELETE ", source)


if __name__ == "__main__":
    unittest.main()
