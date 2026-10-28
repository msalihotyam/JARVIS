"""Pure project driver: runs slices through Core gates via injected ports.

The driver owns no process, database or network. ``ports`` supplies:

- ``clock()`` -> aware datetime for every ledger transition.
- ``append_event(kind, step_key, payload)``: one ledger event per step.
- ``notify(point, dedupe_key, step_key)``: one §2.1 notification point.
- ``lease_claim(step)`` / ``lease_release(step)``: the slice lease.
- ``stage_create(step)`` -> stage; ``route(step)`` -> worker name;
  ``worker(name)`` -> ``jarvis.workers.base.Worker``.
- ``task(step, stage, findings)`` -> worker task; findings travel as data.
- ``budget_exhausted()`` -> bool, asked before every worker session (D3).
- ``verify_cwd(stage)``: raises unless the stage is the worker's cwd.
- ``needs_input(result)`` -> bool: the worker asked a question (D8).
- ``link_rescan(stage)``, ``scope_check(stage)`` -> refusals ([] = pass).
- ``run_checks(stage)`` -> CheckResults; ``regress(results)`` ->
  RegressionVerdict; ``review(stage)`` -> findings with a ``severity``.
- ``evidence(step, stage, results, verdict)`` -> receipt;
  ``apply(stage, receipt)`` -> checkpoint ref.

Worker claims are data: only the Core gates accept a slice. Any gate
error fails closed as a finding, so apply never follows a failed gate.
"""

import contextlib

from jarvis.work_ledger import (
    MAX_REVISIONS,
    EventKind,
    RunState,
    StepState,
    advance_run,
    advance_step,
    run_should_pause,
    run_waits_for_input,
    runnable_steps,
)

# PM34: MINOR is logged; BLOCKER, MAJOR and any unknown severity block.
_NON_BLOCKING = frozenset({"MINOR"})
_REVISABLE = frozenset({StepState.ACCEPTING, StepState.BLOCKED_BY_GATE, StepState.TIMEOUT})


def drive(run, steps, ports):
    """Drive a BUILDING run until it waits, pauses or every slice is done.

    Returns ``(run, steps)`` with the final ledger values.
    """
    steps = {step.key: step for step in steps}
    while run.state == RunState.BUILDING:
        # Revisions cut short (budget) resume before fresh slices start.
        ready = [s for s in steps.values() if s.state in _REVISABLE] + list(runnable_steps(steps.values()))
        if not ready:
            break
        step, outcome = _drive_slice(ready[0], ports)
        steps[step.key] = step
        if outcome == "budget":
            run = _run_to(run, RunState.BLOCKED_BUDGET, ports)
            ports.notify("blocked:budget", f"budget:{step.key}:{step.attempt}", step.key)
        elif outcome == "failed" and run_should_pause(steps.values()):
            run = _run_to(run, RunState.PAUSED, ports)
    if run.state == RunState.BUILDING:
        values = tuple(steps.values())
        if run_waits_for_input(values):
            run = _run_to(run, RunState.NEEDS_INPUT, ports)
        elif values and all(step.state == StepState.ACCEPTED for step in values):
            # The acceptance phase owns awaiting_acceptance/done eligibility.
            run = _run_to(run, RunState.ACCEPTING, ports)
        else:
            # Failed slices strand their dependents; the operator decides.
            run = _run_to(run, RunState.PAUSED, ports)
    return run, tuple(sorted(steps.values(), key=lambda s: s.ordinal))


def _drive_slice(step, ports):
    ports.lease_claim(step)
    try:
        return _slice_body(step, ports)
    finally:
        ports.lease_release(step)


def _slice_body(step, ports):
    stage = ports.stage_create(step)
    worker = ports.worker(ports.route(step))
    started = step.state != StepState.PENDING
    # A resumed revision's findings live in the ledger's gate events.
    findings = (_finding("resume", "see_ledger_gate_events"),) if started else ()
    while True:
        if ports.budget_exhausted():
            return step, "budget"
        step = _step_to(step, StepState.RUNNING, ports)
        if not started:
            started = True
            ports.notify("slice_started", f"{step.key}:started:{step.attempt}", step.key)
        run_id = result = None
        try:
            ports.verify_cwd(stage)
            run_id = worker.start(ports.task(step, stage, findings))
            result = worker.collect(run_id)
        except Exception as exc:
            if run_id is not None:
                with contextlib.suppress(Exception):
                    worker.stop(run_id)
            findings = (_finding("worker", "gate_error:" + type(exc).__name__),)
        timed_out = result is not None and result.status == "timeout"
        if result is not None:
            # Claims like "completed" never skip a gate; others only narrow.
            if ports.needs_input(result):
                step = _step_to(step, StepState.NEEDS_INPUT, ports)
                ports.notify("needs_input", f"{step.key}:needs_input:{step.attempt}", step.key)
                return step, "needs_input"
            if result.status != "completed":
                findings = (_finding("worker", "status:" + result.status),)
            else:
                findings = _gate(step, "scope", ports, lambda: _scope(stage, ports))
        if findings:
            failed = StepState.TIMEOUT if timed_out else StepState.BLOCKED_BY_GATE
            step = _step_to(step, failed, ports)
        else:
            step = _step_to(step, StepState.ACCEPTING, ports)
            findings, accepted = _accept(step, stage, ports)
            if accepted:
                step = _step_to(step, StepState.ACCEPTED, ports)
                ports.notify("slice_accepted", f"{step.key}:accepted", step.key)
                return step, "accepted"
        if step.revisions >= MAX_REVISIONS:
            step = _step_to(step, StepState.FAILED_AFTER_CAP, ports)
            ports.notify("slice_failed_after_cap", f"{step.key}:failed_after_cap", step.key)
            return step, "failed"


def _scope(stage, ports):
    # The post-run link rescan runs before the scope gate reads the diff.
    refusals = list(ports.link_rescan(stage))
    return refusals or list(ports.scope_check(stage))


def _accept(step, stage, ports):
    """Checks -> regression -> review -> evidence -> apply, all fail-closed."""
    results = []

    def checks():
        results.extend(ports.run_checks(stage))
        if not results:
            return ["no_checks"]
        return [" ".join(r.argv) for r in results if r.timed_out or r.exit_code is None]

    def regression():
        verdict = ports.regress(tuple(results))
        results.append(verdict)
        if verdict.passed is not True:
            return list(verdict.new_failures) + list(verdict.reasons) or ["failed"]
        return []

    def review():
        blocking, minor = [], []
        for item in ports.review(stage):
            text = f"{item.get('severity')}:{item.get('detail', '')}"
            (minor if item.get("severity") in _NON_BLOCKING else blocking).append(text)
        if minor:
            ports.append_event(EventKind.PROGRESS, step.key, {"gate": "review_minor", "findings": minor})
        return blocking

    for name, gate in (("checks", checks), ("regression", regression), ("review", review)):
        findings = _gate(step, name, ports, gate)
        if findings:
            return findings, False
    verdict = results.pop()
    name = "evidence"
    try:
        receipt = ports.evidence(step, stage, tuple(results), verdict)
        name = "apply"
        ref = ports.apply(stage, receipt)
    except Exception as exc:
        findings = (_finding(name, "gate_error:" + type(exc).__name__),)
        _gate_event(step, name, findings, ports)
        return findings, False
    ports.append_event(EventKind.PROGRESS, step.key, {"gate": "apply", "passed": True, "ref": str(ref)})
    return (), True


def _gate(step, name, ports, gate):
    try:
        findings = tuple(_finding(name, str(item)) for item in gate())
    except Exception as exc:
        findings = (_finding(name, "gate_error:" + type(exc).__name__),)
    _gate_event(step, name, findings, ports)
    return findings


def _gate_event(step, name, findings, ports):
    ports.append_event(EventKind.PROGRESS, step.key, {
        "gate": name,
        "passed": not findings,
        "findings": [f["detail"] for f in findings],
    })


def _finding(gate, detail):
    return {"gate": gate, "detail": detail}


def _step_to(step, to, ports):
    step = advance_step(step, to, at=ports.clock())
    ports.append_event(EventKind.STEP_STATE, step.key, {"state": step.state.value, "attempt": step.attempt})
    return step


def _run_to(run, to, ports):
    run = advance_run(run, to, at=ports.clock())
    ports.append_event(EventKind.RUN_STATE, None, {"state": run.state.value})
    return run
