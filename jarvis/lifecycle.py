"""Provider-independent task lifecycle transition policy (H2-D).

The single source of truth for which task states are terminal and how a
terminal task is reported. Terminal states are immutable for the normal
commands (resume/clarify/approve/reject/cancel); a terminal task must be
readable and reportable even if its old connector is no longer registered,
so nothing here needs a connector.
"""

from __future__ import annotations

from jarvis.contracts import LoopStage, RunStatus, Task, TaskStatus


#: Terminal task statuses. Once a task reaches one of these it cannot be
#: reopened or mutated by a normal command, and a stale execution result can
#: never overwrite it.
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    }
)

#: Terminal loop stages (the stage machine has no successor for these).
TERMINAL_STAGES = frozenset(
    {LoopStage.COMPLETED, LoopStage.FAILED, LoopStage.CANCELLED}
)

#: Deterministic terminal status -> run status projection.
_TERMINAL_RUN_STATUS = {
    TaskStatus.COMPLETED: RunStatus.COMPLETED,
    TaskStatus.FAILED: RunStatus.FAILED,
    TaskStatus.REJECTED: RunStatus.REJECTED,
    TaskStatus.CANCELLED: RunStatus.CANCELLED,
}


def is_terminal(task: Task) -> bool:
    """Whether the task is in a terminal lifecycle state.

    Both the status and the stage are consulted so a task that is terminal by
    either authority is treated as immutable; ``CANCEL_REQUESTED`` (a pending,
    not-yet-final intent) is deliberately not terminal.
    """
    return (
        task.status in TERMINAL_TASK_STATUSES
        or task.stage in TERMINAL_STAGES
    )


def terminal_run_status(task: Task) -> RunStatus:
    """The run status for reporting a terminal task, without executing it."""
    status = _TERMINAL_RUN_STATUS.get(task.status)
    if status is not None:
        return status
    # Terminal by stage only (defensive; a normally-finalized task sets both).
    if task.stage is LoopStage.COMPLETED:
        return RunStatus.COMPLETED
    if task.stage is LoopStage.CANCELLED:
        return RunStatus.CANCELLED
    return RunStatus.FAILED
