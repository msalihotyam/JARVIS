"""Centralized task-progress bookkeeping for AgentCore.

Everything here mutates or builds the informational ``Task.progress``
snapshot and the bounded session next step. Nothing here authorizes a stage
transition, bypasses a clarification/approval/cost/cancellation blocker, or
feeds permission, billing, connector selection, execution, verification or
outbound serialization. ``Task.status``/``Task.stage``, approvals, billing
state and verification remain the only lifecycle authorities.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

# The credential policy and the session next-step sanitizer are owned by
# ``jarvis.contracts`` so deserialization and runtime creation share one
# canonical implementation; the redundant aliases below re-export them for
# existing callers of this module.
from jarvis.contracts import (
    DEFAULT_SESSION_NEXT_STEP as DEFAULT_SESSION_NEXT_STEP,
    normalize_session_next_step as normalize_session_next_step,
)
from jarvis.contracts import (
    PROGRESS_MAX_ARTIFACTS,
    TERMINAL_LOOP_STAGES,
    ErrorCode,
    LoopStage,
    ProgressArtifact,
    ProgressArtifactChange,
    ProgressArtifactKind,
    ProgressIssue,
    ProgressIssueKind,
    Task,
    TaskStatus,
    credential_bearing,
    normalize_progress_text,
)

#: Issue kinds describing a resolvable permission/cost gate; cleared together
#: when a grant resolves the pending request.
GATE_ISSUE_KINDS = (
    ProgressIssueKind.APPROVAL_REQUIRED,
    ProgressIssueKind.COST_OVERRIDE_REQUIRED,
    ProgressIssueKind.INVALID_GRANT,
)

#: Everything except FAILURE; cleared when a task terminates by cancellation
#: or rejection, where the terminal status/stage carries the meaning.
NON_FAILURE_ISSUE_KINDS = (
    ProgressIssueKind.CLARIFICATION_REQUIRED,
    *GATE_ISSUE_KINDS,
    ProgressIssueKind.CANCELLATION_PENDING,
)

#: Deterministic wait-state mapping used before each blocked checkpoint.
_WAITING_STATUS_ISSUES = {
    TaskStatus.AWAITING_CLARIFICATION: ProgressIssueKind.CLARIFICATION_REQUIRED,
    TaskStatus.AWAITING_APPROVAL: ProgressIssueKind.APPROVAL_REQUIRED,
    TaskStatus.AWAITING_COST_OVERRIDE: ProgressIssueKind.COST_OVERRIDE_REQUIRED,
}


def mark_stage_completed(task: Task, stage: LoopStage) -> None:
    """Record one successfully finished work stage, in insertion order.

    Called only when AgentCore is about to transition to the deterministic
    successor. Terminal states and duplicates are never added; waiting at,
    failing in or cancelling out of a stage never completes it.
    """
    if type(stage) is not LoopStage or stage in TERMINAL_LOOP_STAGES:
        return
    if stage in task.progress.completed_stages:
        return
    task.progress = replace(
        task.progress,
        completed_stages=task.progress.completed_stages + (stage,),
    )


def set_issue(
    task: Task,
    kind: ProgressIssueKind,
    error_code: ErrorCode | None = None,
    retryable: bool = False,
) -> None:
    """Set (or replace) the one open issue of this kind."""
    if type(kind) is not ProgressIssueKind:
        return
    issue = ProgressIssue(kind=kind, error_code=error_code, retryable=bool(retryable))
    kept = tuple(item for item in task.progress.open_issues if item.kind is not kind)
    task.progress = replace(task.progress, open_issues=kept + (issue,))


def clear_issues(task: Task, *kinds: ProgressIssueKind) -> None:
    targets = set(kinds)
    kept = tuple(
        item for item in task.progress.open_issues if item.kind not in targets
    )
    if len(kept) != len(task.progress.open_issues):
        task.progress = replace(task.progress, open_issues=kept)


def set_waiting_issue(task: Task) -> None:
    """Record the blocker matching the task's current waiting status."""
    kind = _WAITING_STATUS_ISSUES.get(task.status)
    if kind is not None:
        set_issue(task, kind)


def build_progress_artifacts(
    entries: Iterable[tuple[Any, Any, Any]],
) -> tuple[ProgressArtifact, ...]:
    """Normalize future artifact metadata into the bounded typed contract.

    No production flow populates artifacts in this milestone; connector
    summaries and arbitrary ``ToolResult.data`` are never parsed into this.
    Exact enum instances are required (raw strings fail closed), labels must
    be actual strings, credential-bearing labels are dropped before they can
    persist, exact normalized duplicates keep their first occurrence, and at
    most ``PROGRESS_MAX_ARTIFACTS`` items are accepted.
    """
    accepted: list[ProgressArtifact] = []
    seen: set[tuple[ProgressArtifactKind, ProgressArtifactChange, str]] = set()
    for entry in entries:
        try:
            kind, change, label = entry
        except (TypeError, ValueError):
            continue
        if type(kind) is not ProgressArtifactKind:
            continue
        if type(change) is not ProgressArtifactChange:
            continue
        if not isinstance(label, str) or credential_bearing(label):
            continue
        normalized = normalize_progress_text(label)
        if normalized is None or credential_bearing(normalized):
            continue
        key = (kind, change, normalized)
        if key in seen:
            continue
        seen.add(key)
        accepted.append(ProgressArtifact(kind=kind, change=change, label=normalized))
        if len(accepted) >= PROGRESS_MAX_ARTIFACTS:
            break
    return tuple(accepted)
