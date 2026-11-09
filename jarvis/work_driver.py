# pyright: strict
"""Pure project driver: runs slices through Core gates via injected ports.

The driver owns no process, database or network. ``ports`` supplies:

- ``clock()`` -> aware datetime for every ledger transition.
- ``append_event(kind, step_key, payload, idempotency_key=None)``: one
  durable ledger event; a reused idempotency key raises.
- ``notify(point, dedupe_key, step_key)``: one §2.1 notification point.
- ``lease_claim(step)`` / ``lease_release(step)``: the slice lease. A claim that
  returns an N6 ``Lease`` (N6-08) fences the slice: each write runs inside the
  ``lease_fenced(lease, write) -> bool`` port (epoch check and write under one lock,
  N6-HARD2 H1); no port or a refusal raises ``Fenced``, and every event payload
  carries the epoch. Ports with ``lease_check`` require that Lease:
  a claim yielding anything else raises ``Fenced`` before any write (N6-08FIX).
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
- ``tree_hash(stage)`` -> lowercase hex tree hash (40-64 chars).
- ``apply_intent(step)`` -> ``(key, receipted)`` of this attempt's recorded
  apply intent, or None when none was recorded.
- ``applied(stage, key)`` -> bool: the apply for ``key`` took effect.
- ``undo(stage, key)``: roll ``key``'s effect back to the checkpoint before
  it; a no-op when nothing of ``key`` is applied, so it is safe to repeat.
- ``boot_scan()`` -> ``n6_boot.Report``, ``boot_record(item)`` and
  ``boot_resume(item)``: the scan runs once and ``n6_boot.act`` carries it out
  before the driver acts. A missing port, a non-Report, or a held report
  (paused, HELD_ALL) stops the driver untouched (fail-closed).
- ``root_cause(step)`` -> str | None (optional; N6-13 G17/R23): the written
  "ROOT CAUSE <key>:<attempt>: <why>" record for the step's last failure.
  EVERY retry needs a valid record, checked against the failed attempt's
  real findings; otherwise the step is held (step unchanged, attention
  notify, run paused); a missing port holds. The cap counts attempts: once
  ``step.attempt >= RETRY_CAP`` the next attempt is always held, whatever
  the record says.
- ``gate_findings(step)`` -> iterable of gate payloads (optional; N6-13FIX):
  the ledger's PROGRESS gate payloads for the step's current attempt, in
  order. A resumed retry takes its failure text from the last failing one;
  a missing port, an error or no failing payload holds the step.

Worker claims are data: only the Core gates accept a slice. Any gate
error fails closed as a finding, so apply never follows a failed gate.
A gate error that is not ``work_regress.retriable`` (StageError, any
unknown class) cancels the slice: no retry, ever; the operator decides.
Apply is exactly-once: a durable intent event carries the idempotency key
before apply and a receipt event follows it. A resumed ACCEPTING step with
an intent is verified, never applied again: receipted or applied ->
accepted, otherwise undone and counted as a failed attempt.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from jarvis import n6_boot, n6_notify, n6_outage, n6_preflight, n6_refcheck
from jarvis.n6_lease import Fenced, Lease
from jarvis.work_supervisor import n6_hold
from jarvis.work_ledger import (
    MAX_REVISIONS,
    EventKind,
    RunState,
    StepState,
    WorkRun,
    WorkStep,
    advance_run,
    advance_step,
    idempotency_key,
    run_should_pause,
    run_waits_for_input,
    runnable_steps,
)
from jarvis.work_regress import RETRY_CAP, retriable, root_cause_ok
from jarvis.workers.base import WorkerResult

if TYPE_CHECKING:
    # Types only: the driver stays pure and imports no process/stage code.
    from jarvis.work_checks import CheckResult
    from jarvis.work_evidence import EvidenceReceipt
    from jarvis.work_regress import RegressionVerdict
    from jarvis.write_stage import Stage

_Finding = dict[str, str]
_Findings = tuple[_Finding, ...]


class _Worker(Protocol):
    # collect -> object: the driver re-checks for a WorkerResult (claims are data).
    def start(self, task: str, /) -> str: ...
    def collect(self, run_id: str, /) -> object: ...
    def stop(self, run_id: str, /) -> object: ...


class Ports(Protocol):
    """The injected ports listed in the module docstring.

    Results the driver re-checks fail-closed are typed ``object``.
    """

    def clock(self) -> datetime: ...
    def append_event(self, kind: EventKind, step_key: str | None, payload: Mapping[str, object], /,
                     idempotency_key: str | None = None) -> object: ...
    def notify(self, point: str, dedupe_key: str, step_key: str | None, /) -> object: ...
    def lease_claim(self, step: WorkStep, /) -> object: ...
    def lease_release(self, step: WorkStep, /) -> object: ...
    def stage_create(self, step: WorkStep, /) -> Stage: ...
    def route(self, step: WorkStep, /) -> str: ...
    def worker(self, name: str, /) -> _Worker: ...
    def task(self, step: WorkStep, stage: Stage, findings: _Findings, /) -> str: ...
    def budget_exhausted(self) -> object: ...
    def verify_cwd(self, stage: Stage, /) -> object: ...
    def needs_input(self, result: WorkerResult, /) -> object: ...
    def link_rescan(self, stage: Stage, /) -> Iterable[object]: ...
    def scope_check(self, stage: Stage, /) -> Iterable[object]: ...
    def run_checks(self, stage: Stage, /) -> Iterable[CheckResult]: ...
    def regress(self, results: tuple[CheckResult, ...], /) -> RegressionVerdict: ...
    def review(self, stage: Stage, /) -> Iterable[Mapping[str, object]]: ...
    def evidence(self, step: WorkStep, stage: Stage, results: tuple[CheckResult, ...],
                 verdict: RegressionVerdict, /) -> EvidenceReceipt: ...
    def apply(self, stage: Stage, receipt: EvidenceReceipt, /) -> object: ...
    def tree_hash(self, stage: Stage, /) -> object: ...
    def apply_intent(self, step: WorkStep, /) -> tuple[str, object] | None: ...
    def applied(self, stage: Stage, key: str, /) -> object: ...
    def undo(self, stage: Stage, key: str, /) -> object: ...
    def boot_scan(self) -> object: ...
    def boot_record(self, item: n6_boot.Item, /) -> object: ...
    def boot_resume(self, item: n6_boot.Item, /) -> object: ...

@dataclass(frozen=True)
class N6:
    """N6-WIRE1: the optional ``ports.n6`` port (shadow; the app never sets it, N6-01).

    dir: the one N6 state dir (never build/conveyor/, never a database). now: epoch seconds.
    slice(step) -> (slice_, main_sha, owned_by_running_lanes, done_ids) for n6_preflight.
    watchdog(step, result) -> an n6_outage verdict ("continue" | "outage" | "timeout").
    int_check(step) -> None (not an INT) or (git, ref, read_report, run_id) for n6_refcheck."""

    dir: Path
    now: Callable[[], float]
    slice: Callable[[WorkStep], tuple[object, object, object, object]]
    watchdog: Callable[[WorkStep, WorkerResult | None], object] | None = None
    int_check: Callable[[WorkStep], tuple[n6_refcheck.GitPort, str, Callable[[], object], str] | None] | None = None


def _n6_dir(n6: N6) -> Path:
    """The state dir, checked; raises (the caller holds) on anything doubtful."""
    d = Path(n6.dir).resolve()
    parts = [p.lower() for p in d.parts]
    if any(a == "build" and b == "conveyor" for a, b in zip(parts, parts[1:])) or any(
        "jarvis.db" in p for p in parts
    ):
        raise ValueError("N6 state dir must not be build/conveyor/ or a database")
    if not d.is_dir():
        raise ValueError("N6 state dir is missing")
    return d


def _n6_file(n6: N6, prefix: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:40]
    return _n6_dir(n6) / f"{prefix}-{safe}-{hashlib.sha1(key.encode()).hexdigest()[:8]}.json"


def _notify(ports: Ports, point: str, dedupe: str, step_key: str | None) -> object:
    """In N6 mode every notification goes through n6_notify's importance filter only
    (ORCHESTRATOR items are never stored or paged); a failed submit is not paged."""
    n6 = getattr(ports, "n6", None)
    if n6 is None:
        return ports.notify(point, dedupe, step_key)
    try:
        return n6_notify.submit(_n6_dir(n6) / "notify.json", n6_notify.Event(point, dedupe, step_key or ""))
    except Exception:
        return False


# PM34: MINOR is logged; BLOCKER, MAJOR and any unknown severity block.
_NON_BLOCKING = frozenset({"MINOR"})
_REVISABLE = frozenset({StepState.ACCEPTING, StepState.BLOCKED_BY_GATE, StepState.TIMEOUT})
_TREE_HASH = re.compile(r"[0-9a-f]{40,64}")


def _booted(ports: object) -> bool:
    """N6-12: scan once and act on the report before the driver acts. Fail-closed: a missing
    boot port, a scan that is not a ``Report`` or a held report (act returns False) acts on nothing.
    N6-12FIX3: act judges liveness at a FRESH clock reading taken after the scan (not
    ``report.now``), so a lease that expired between scan and act is not resumed; a clock
    that is not an aware datetime acts on nothing."""
    names = ("boot_scan", "boot_record", "boot_resume", "clock")
    scan, record, resume, clock = (getattr(ports, n, None) for n in names)
    if not (callable(scan) and callable(record) and callable(resume) and callable(clock)):
        return False
    report = cast("object", scan())
    if not isinstance(report, n6_boot.Report):
        return False
    now = cast("object", clock())
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return False

    def fresh() -> float:  # N6-HARD2C H4: act re-reads the clock per item, floored at the scan
        t = cast("object", clock())
        if not isinstance(t, datetime) or t.utcoffset() is None:
            return math.inf  # a bad reading mid-act: every lease reads expired, nothing resumes
        return max(t.timestamp(), report.now)
    return n6_boot.act(
        report, report.lease_path, fresh, cast("Callable[[n6_boot.Item], object]", record),
        cast("Callable[[n6_boot.Item], object]", resume))


def drive(run: WorkRun, steps: Iterable[WorkStep], ports: Ports) -> tuple[WorkRun, tuple[WorkStep, ...]]:
    """Drive a BUILDING run until it waits, pauses or every slice is done.

    Returns ``(run, steps)`` with the final ledger values.
    """
    by_key = {step.key: step for step in steps}
    if not _booted(ports):
        return run, tuple(sorted(by_key.values(), key=lambda s: s.ordinal))
    while run.state == RunState.BUILDING:
        # Checked first: a resumed run may already hold three failed slices.
        if run_should_pause(by_key.values()):
            run = _run_to(run, RunState.PAUSED, ports)
            break
        # Revisions cut short (budget) resume before fresh slices start.
        ready = [s for s in by_key.values() if s.state in _REVISABLE] + list(runnable_steps(by_key.values()))
        if not ready:
            break
        step, outcome = _drive_slice(ready[0], ports)
        by_key[step.key] = step
        if outcome == "budget":
            run = _run_to(run, RunState.BLOCKED_BUDGET, ports)
            _notify(ports, "blocked:budget", f"budget:{step.key}:{step.attempt}", step.key)
        elif outcome == "held":
            break  # N6-13: an attention item; the run pauses below
    if run.state == RunState.BUILDING:
        values = tuple(by_key.values())
        if run_waits_for_input(values):
            run = _run_to(run, RunState.NEEDS_INPUT, ports)
        elif values and all(step.state == StepState.ACCEPTED for step in values):
            # The acceptance phase owns awaiting_acceptance/done eligibility.
            run = _run_to(run, RunState.ACCEPTING, ports)
        else:
            # Failed slices strand their dependents; the operator decides.
            run = _run_to(run, RunState.PAUSED, ports)
    return run, tuple(sorted(by_key.values(), key=lambda s: s.ordinal))


# N6-08 (M8): with an N6 lease, these writes re-check the epoch first and are refused once stale.
_FENCED_WRITES = frozenset({"append_event", "notify", "stage_create", "evidence", "apply", "undo"})


class _FencedPorts:
    """Ports behind an N6 lease: each write runs inside ``ports.lease_fenced(lease, write)``, which
    refuses a stale epoch atomically; events carry the epoch."""

    def __init__(self, ports: Ports, lease: Lease) -> None:
        self._ports = ports
        self._lease = lease
        self.fenced = False

    def __getattr__(self, name: str) -> object:
        attr: object = getattr(self._ports, name)
        if name not in _FENCED_WRITES or not callable(attr):
            return attr
        target = cast("Callable[..., object]", attr)

        def write(*args: object, **kw: object) -> object:
            if name == "append_event" and len(args) >= 3:
                payload = cast("Mapping[str, object]", args[2])
                args = (*args[:2], {**payload, "epoch": self._lease.epoch}, *args[3:])
            box: list[object] = []
            fence: object = getattr(self._ports, "lease_fenced", None)
            # N6-HARD2 H1: the epoch check and the write are one step under the lease lock (no
            # check-then-act). Fail closed: no fence port, a refusal or an earlier refusal fences
            # every later write. The write must not call the lease itself (the lock is not reentrant).
            if self.fenced or not callable(fence) or not fence(self._lease, lambda: box.append(target(*args, **kw))):
                self.fenced = True
                raise Fenced(f"stale epoch {self._lease.epoch} for {self._lease.slice_id}")
            return box[0] if box else None

        return write


def _drive_slice(step: WorkStep, ports: Ports) -> tuple[WorkStep, str]:
    track = [step]
    done = False
    claimed = ports.lease_claim(step)
    if getattr(ports, "lease_check", None) is not None and not isinstance(claimed, Lease):
        # N6-08FIX F3: N6 ports need a Lease; a failed claim refuses, never runs unfenced.
        if claimed is not None:  # a non-Lease claim still holds something: give it back
            ports.lease_release(step)
        raise Fenced(f"N6 lease claim failed for {step.key}")
    fenced = _FencedPorts(ports, claimed) if isinstance(claimed, Lease) else None
    if fenced is not None:
        ports = cast("Ports", fenced)
    try:
        outcome = _slice_body(track, ports)
        done = True
        return outcome
    finally:
        # An escaping exception must not strand the step in RUNNING.
        if not done and track[0].state == StepState.RUNNING:
            with contextlib.suppress(Exception):
                _step_to(track[0], StepState.INTERRUPTED, ports)
        # A fenced manager writes nothing more, its release included: the lease is not its own.
        if fenced is None or not fenced.fenced:
            ports.lease_release(step)


def _slice_body(track: list[WorkStep], ports: Ports) -> tuple[WorkStep, str]:
    def move(to: StepState) -> WorkStep:
        track[0] = _step_to(track[0], to, ports)
        return track[0]

    def accepted() -> tuple[WorkStep, str]:
        step = move(StepState.ACCEPTED)
        _notify(ports, "slice_accepted", f"{step.key}:accepted", step.key)
        return step, "accepted"

    def capped() -> tuple[WorkStep, str] | None:
        step = track[0]
        if step.revisions < MAX_REVISIONS:
            return None
        step = move(StepState.FAILED_AFTER_CAP)
        _notify(ports, "slice_failed_after_cap", f"{step.key}:failed_after_cap", step.key)
        return step, "failed"

    def ended(found: _Findings) -> tuple[WorkStep, str]:
        # N6-13: a non-retriable gate error ends the slice; CANCELLED is terminal.
        step = move(StepState.CANCELLED)
        _gate_event(step, "non_retriable", found, ports)
        _notify(ports, "slice_failed_after_cap", f"{step.key}:non_retriable", step.key)
        return step, "held"

    def hold(gate: str, detail: str, n6_gate: bool = False) -> tuple[WorkStep, str]:
        step = track[0]
        # N6-WIRE1FIX: an N6 wiring hold carries the supervisor's own non-worker decision.
        _gate_event(step, gate, (_finding(gate, detail),), ports, n6_hold(gate, detail) if n6_gate else None)
        _notify(ports, "slice_failed_after_cap", f"{step.key}:{gate}:{step.attempt}", step.key)
        return step, "held"

    def unrooted(found: _Findings | None) -> tuple[WorkStep, str] | None:
        # N6-13FIX (O61/O64): the cap counts attempts and fails closed to the lower
        # number, so the attempt after RETRY_CAP is held even with a valid record.
        step = track[0]
        failure = f"{step.key}:{step.attempt}"
        if step.attempt >= RETRY_CAP:
            return hold("retry_cap", "attempt_after_cap:" + failure)
        # Every retry needs a written root cause that says more than the real failure text.
        port = getattr(ports, "root_cause", None)
        try:
            record = port(step) if callable(port) else None
        except Exception:
            record = None
        # N6-HARD2C H5: the failed attempt's exception message counts as error text too.
        error = " ".join([f["detail"] for f in found or ()] + [f.get("message", "") for f in found or ()])
        if found and root_cause_ok(record, failure, error):
            return None
        return hold("root_cause", ("missing_or_invalid:" if found else "findings_unreadable:") + failure)

    step = track[0]
    started = step.state != StepState.PENDING
    n6: N6 | None = getattr(ports, "n6", None)
    if n6 is not None and not started:
        # N6-WIRE1: preflight before anything is created; a block (or any doubt) holds the slice.
        try:
            _n6_dir(n6)
            verdict = n6_preflight.preflight(*n6.slice(step))
            why = None if verdict.ok is True else str(verdict.reason)
        except Exception as exc:
            why = f"error:{type(exc).__name__}"
        if why is not None:
            return hold("preflight", why, n6_gate=True)
    stage = ports.stage_create(step)
    worker = ports.worker(ports.route(step))
    # A resumed revision's findings are read back from the ledger; unreadable stays None (held).
    findings: _Findings | None = _ledger_findings(step, ports) if started else ()
    resumed = _resume_apply(step, stage, ports) if step.state == StepState.ACCEPTING else None
    if resumed is not None:
        findings, ok = resumed
        if ok:
            return accepted()
        # Not applied and undone: a failed attempt, never a second apply.
    elif findings and findings[0].get("n6_hold") == "yes":
        # N6-WIRE1FIX3: a persisted refcheck hold re-holds until the evidence is accepted:
        # no worker revision, no FAILED_AFTER_CAP. Only ACCEPTING under N6 re-checks; without
        # N6 _accept would skip refcheck and apply, so it holds (fail closed).
        if step.state == StepState.ACCEPTING and n6 is not None:
            findings, ok = _accept(step, stage, ports)
            if ok:
                return accepted()
        if findings[0].get("n6_hold") == "yes":
            return hold("refcheck", findings[0]["detail"], n6_gate=True)
    if findings and _fatal(findings):  # N6-HARD2C H5: a persisted non-retriable failure stays fatal
        return ended(findings)
    if step.state in _REVISABLE:
        # A resumed step already at the cap fails instead of wedging on RUNNING.
        outcome = capped()
        if outcome is not None:
            return outcome
    while True:
        # D3 fails closed: only an explicit False lets a worker session start.
        if ports.budget_exhausted() is not False:
            return step, "budget"
        if step.state in _REVISABLE:
            held = unrooted(findings)
            if held is not None:
                return held
        step = move(StepState.RUNNING)
        if not started:
            started = True
            _notify(ports, "slice_started", f"{step.key}:started:{step.attempt}", step.key)
        run_id: str | None = None
        result: WorkerResult | None = None
        try:
            ports.verify_cwd(stage)
            run_id = worker.start(ports.task(step, stage, findings or ()))
            collected = worker.collect(run_id)
            if not isinstance(collected, WorkerResult):
                raise TypeError("collect returned no WorkerResult")
            result = collected
        except Exception as exc:
            if run_id is not None:
                with contextlib.suppress(Exception):
                    worker.stop(run_id)
            result = None
            findings = (_gate_error("worker", exc),)
        timed_out = result is not None and result.status == "timeout"
        if n6 is not None and n6.watchdog is not None:
            # N6-WIRE1: the watchdog verdict goes through n6_outage. HOLD/NUDGE/WAIT are an
            # outage: INTERRUPTED, never counted (attempt and revisions unchanged). DROP counts
            # as a timeout. Any error holds the same uncounted way (fail closed).
            try:
                decision = n6_outage.step(_n6_file(n6, "outage", step.key), n6.watchdog(step, result), n6.now())
                action, counts = decision.action, decision.counts_failure is True
            except Exception as exc:
                action, counts = f"error:{type(exc).__name__}", False
            if action != "continue" and not counts:
                if result is None and run_id is not None:
                    with contextlib.suppress(Exception):
                        worker.stop(run_id)
                step = move(StepState.INTERRUPTED)
                return hold("outage", str(action), n6_gate=True)
            if action != "continue":
                timed_out = True
                if result is not None and result.status == "completed":
                    result = None
                    findings = (_finding("outage", "drop"),)
        if result is not None:
            # Claims like "completed" never skip a gate; others only narrow.
            if ports.needs_input(result):
                step = move(StepState.NEEDS_INPUT)
                _notify(ports, "needs_input", f"{step.key}:needs_input:{step.attempt}", step.key)
                return step, "needs_input"
            if result.status != "completed":
                findings = (_finding("worker", "status:" + result.status),)
            else:
                findings = _gate(step, "scope", ports, lambda: _scope(stage, ports))
        if result is None or result.status != "completed":
            # N6-13FIX: worker failures go to the ledger too, so a resume can read them back.
            _gate_event(step, "worker", findings, ports)
        if _fatal(findings):
            return ended(findings)
        if findings:
            step = move(StepState.TIMEOUT if timed_out else StepState.BLOCKED_BY_GATE)
        else:
            step = move(StepState.ACCEPTING)
            findings, ok = _accept(step, stage, ports)
            if ok:
                return accepted()
            if findings and findings[0].get("n6_hold") == "yes":
                return hold("refcheck", findings[0]["detail"], n6_gate=True)
            if _fatal(findings):
                return ended(findings)
        outcome = capped()
        if outcome is not None:
            return outcome


def _resume_apply(step: WorkStep, stage: Stage, ports: Ports) -> tuple[_Findings, bool] | None:
    """Settle a recorded apply intent without applying; None if none."""
    intent = ports.apply_intent(step)
    if intent is None:
        return None
    key, receipted = intent
    if receipted is True:
        return (), True
    if ports.applied(stage, key) is True:
        _receipt(step, key, "verified", ports)
        return (), True
    ports.undo(stage, key)
    findings = (_finding("apply", "resume:not_applied"),)
    _gate_event(step, "apply", findings, ports)
    return findings, False


def _scope(stage: Stage, ports: Ports) -> list[object]:
    # The post-run link rescan runs before the scope gate reads the diff.
    refusals = list(ports.link_rescan(stage))
    return refusals or list(ports.scope_check(stage))


def _accept(step: WorkStep, stage: Stage, ports: Ports) -> tuple[_Findings, bool]:
    """Checks -> regression -> review -> evidence -> apply, all fail-closed."""
    results: list[CheckResult] = []
    verdicts: list[RegressionVerdict] = []

    def checks() -> list[str]:
        results.extend(ports.run_checks(stage))
        if not results:
            return ["no_checks"]
        return [" ".join(r.argv) for r in results if r.timed_out or r.exit_code is None]

    def regression() -> list[str]:
        verdict = ports.regress(tuple(results))
        verdicts.append(verdict)
        if verdict.passed is not True:
            return list(verdict.new_failures) + list(verdict.reasons) or ["failed"]
        return []

    def review() -> list[str]:
        blocking: list[str] = []
        minor: list[str] = []
        for item in ports.review(stage):
            text = f"{item.get('severity')}:{item.get('detail', '')}"
            (minor if item.get("severity") in _NON_BLOCKING else blocking).append(text)
        if minor:
            ports.append_event(EventKind.PROGRESS, step.key, {"gate": "review_minor", "findings": minor})
        return blocking

    gates: tuple[tuple[str, Callable[[], list[str]]], ...] = (
        ("checks", checks), ("regression", regression), ("review", review))
    for name, gate in gates:
        findings = _gate(step, name, ports, gate)
        if findings:
            return findings, False
    n6 = getattr(ports, "n6", None)
    if n6 is not None:  # shadow only: no gate event without N6
        # N6-WIRE1: an INT result is accepted only through n6_refcheck (read_evidence then
        # accept). N6-WIRE1FIX2: any refusal or error is an N6 hold the driver turns into
        # wait/n6.refcheck/none; a worker cannot fix an unverified INT push (no revision).
        try:
            spec = n6.int_check(step) if n6.int_check is not None else None
            if spec is not None:
                git, ref, read_report, run_id = spec
                n6_refcheck.accept(git, n6_refcheck.read_evidence(git, ref, read_report, run_id), run_id)
        except Exception as exc:
            try:
                msg = str(exc)
            except Exception:  # an unprintable error still holds
                msg = "?"
            return ({"gate": "refcheck", "detail": f"{type(exc).__name__}:{msg}"[:300], "n6_hold": "yes"},), False
        _gate_event(step, "refcheck", (), ports)
    verdict = verdicts.pop()
    name = "evidence"
    try:
        receipt = ports.evidence(step, stage, tuple(results), verdict)
        name = "apply"
        tree = ports.tree_hash(stage)
        if not isinstance(tree, str) or not _TREE_HASH.fullmatch(tree):
            raise ValueError("tree hash must be 40-64 lowercase hex")
        key = idempotency_key(step.run_id, step.key, step.attempt, "apply:" + tree[:40])
        # The durable intent precedes any effect, so a resume can verify it.
        ports.append_event(
            EventKind.PROGRESS, step.key, {"gate": "apply_intent", "idempotency_key": key}, idempotency_key=key,
        )
    except Exception as exc:
        findings = (_gate_error(name, exc),)
        _gate_event(step, name, findings, ports)
        return findings, False
    try:
        ref = ports.apply(stage, receipt)
        if ref is None or not str(ref):
            raise ValueError("apply returned no checkpoint ref")
    except Exception as exc:
        # A partial effect rolls back before the attempt counts as failed; an
        # undo that raises escapes and leaves the intent for the resume.
        ports.undo(stage, key)
        findings = (_gate_error("apply", exc),)
        _gate_event(step, "apply", findings, ports)
        return findings, False
    _receipt(step, key, str(ref), ports)
    return (), True


def _receipt(step: WorkStep, key: str, ref: str, ports: Ports) -> None:
    ports.append_event(
        EventKind.PROGRESS, step.key,
        {"gate": "apply", "passed": True, "idempotency_key": key, "ref": ref},
        idempotency_key=idempotency_key(step.run_id, step.key, step.attempt, "applied:" + key[:40]),
    )


def _gate(step: WorkStep, name: str, ports: Ports, gate: Callable[[], Iterable[object]]) -> _Findings:
    try:
        findings = tuple(_finding(name, str(item)) for item in gate())
    except Exception as exc:
        findings = (_gate_error(name, exc),)
    _gate_event(step, name, findings, ports)
    return findings


def _gate_error(gate: str, exc: Exception) -> _Finding:
    found = _finding(gate, "gate_error:" + type(exc).__name__)
    found["message"] = str(exc)[:500]  # N6-HARD2C H5: a root cause must say more than this
    if not retriable(exc):
        found["retriable"] = "no"  # the detail format stays; only the driver reads this flag
    return found


def _fatal(findings: _Findings) -> bool:
    return any(f.get("retriable") == "no" for f in findings)


def _gate_event(step: WorkStep, name: str, findings: _Findings, ports: Ports,
                held: object = None) -> None:
    payload: dict[str, object] = {
        "gate": name,
        "passed": not findings,
        "findings": [f["detail"] for f in findings],
        # N6-HARD2C H5: persisted so a resume restores them (parallel to "findings").
        "retriable": ["no" if f.get("retriable") == "no" else "yes" for f in findings],
        "messages": [f.get("message", "") for f in findings],
    }
    if held is not None:
        payload["decision"] = {k: getattr(held, k) for k in ("action", "rule_id", "constraint")}
    ports.append_event(EventKind.PROGRESS, step.key, payload)


def _ledger_findings(step: WorkStep, ports: Ports) -> _Findings | None:
    # N6-13FIX: the last failing gate of the step's current attempt; None (unreadable) holds the step.
    port = getattr(ports, "gate_findings", None)
    try:
        payloads = list(port(step)) if callable(port) else []
        failing = [p for p in payloads if isinstance(p, Mapping) and p.get("passed") is False
                   and p.get("gate") not in ("root_cause", "retry_cap")]
        if not failing:
            return None
        last = failing[-1]
        gate, details = str(last["gate"]), last.get("findings")
        if not isinstance(details, list) or not details or not all(isinstance(d, str) and d for d in details):
            return None
        # N6-HARD2C H5: rows before H5 lack both keys (legacy, read as retriable, no message);
        # a row with either key must carry both, parallel to "findings", or it is unreadable.
        flags, messages = last.get("retriable"), last.get("messages")
        if flags is None and messages is None:
            flags, messages = ["yes"] * len(details), [""] * len(details)
        if (not isinstance(flags, list) or not isinstance(messages, list)
                or len(flags) != len(details) or len(messages) != len(details)
                or not all(f in ("yes", "no") for f in flags)
                or not all(isinstance(m, str) for m in messages)):
            return None
        out: list[_Finding] = []
        for d, f, m in zip(details, flags, messages):
            found = _finding(gate, d)
            if m:
                found["message"] = m
            if f == "no":
                found["retriable"] = "no"
            if gate == "refcheck":  # N6-WIRE1FIX3: only hold() writes a failing refcheck row
                found["n6_hold"] = "yes"
            out.append(found)
        return tuple(out)
    except Exception:
        return None


def _finding(gate: str, detail: str) -> _Finding:
    return {"gate": gate, "detail": detail}


def _step_to(step: WorkStep, to: StepState, ports: Ports) -> WorkStep:
    step = advance_step(step, to, at=ports.clock())
    ports.append_event(EventKind.STEP_STATE, step.key, {"state": step.state.value, "attempt": step.attempt})
    return step


def _run_to(run: WorkRun, to: RunState, ports: Ports) -> WorkRun:
    run = advance_run(run, to, at=ports.clock())
    ports.append_event(EventKind.RUN_STATE, None, {"state": run.state.value})
    return run
