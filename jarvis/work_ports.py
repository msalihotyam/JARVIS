"""PM26A S3: production ports for jarvis.work_driver, judged on JARVIS's side.

The worker owns every byte of the stage: its config, refs, objects and hooks.
So no port runs git in the stage. The first judgement port (scope_check, which
the driver calls before any other judgement) reads the stage tip by pure
Python, imports it with stage_git.import_commit into the per-run JARVIS repo
and pins it at refs/jarvis/judged/<run>/<slice>. Every later port reads only
that pin, so a stage that moves after the pin changes nothing that was judged.

scope comes from diff-tree in the JARVIS repo; checks, regression and the
fixture hash run on a git-free export of the pinned commit; review, evidence
and the tree hash read the pinned commit. All git goes through stage_git.run.

apply, applied and undo fail closed until S4 wires them.
"""

import hashlib
import json
import re
import tempfile
import types
from pathlib import Path

from jarvis import stage_git, write_scope
from jarvis.notify import NotificationCore
from jarvis.work_checks import run_check as _run_check
from jarvis.work_evidence import EvidenceReceipt
from jarvis.work_ledger import EventKind, WorkEvent, event_payload_json
from jarvis.work_regress import RegressionVerdict, TestResult, compare_results, parse_output
from jarvis.write_stage import Stage, StageError

_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_BRANCH = re.compile(rf"jarvis/({_ID})/({_ID})")


def _payload(event):
    return json.loads(event.payload_json)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _git(repo, *args, input=None):
    try:
        return stage_git.run(repo, *args, input=input)
    except (stage_git.StageGitError, ValueError) as exc:
        raise StageError(str(exc)) from exc


def _oid(value):
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace").strip()
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise StageError("git returned no object id")
    return value


def _identity(manager, stage):
    """Return (run, slice) when the stage is exactly what the manager derives."""
    match = _BRANCH.fullmatch(stage.branch) if isinstance(stage.branch, str) else None
    expected = Path(manager.root) / match[1] / match[2] if match else None
    if (match is None
            or match[2].casefold() == "main"
            or not _OID.fullmatch(str(stage.base))
            or Path(stage.path) != expected
            or Path(stage.path).resolve() != expected.resolve()
            or Path(stage.target).resolve() == Path(stage.path).resolve()):
        raise StageError("stage identity mismatch")
    return match[1], match[2]


def _changes(repo, base, commit):
    """Rebuild write_scope.check change dicts from the JARVIS repo."""
    raw = _git(repo, "diff-tree", "-r", "-z", "--raw", "--no-renames",
               "--end-of-options", base, commit).split(b"\0")
    numstat = _git(repo, "diff-tree", "-r", "-z", "--numstat", "--no-renames",
                   "--end-of-options", base, commit).split(b"\0")
    try:
        binary = {}
        for line in numstat[:-1]:
            added, deleted, path = line.split(b"\t", 2)
            binary[path] = added == b"-" and deleted == b"-"
        if raw[-1] or len(raw) % 2 != 1:
            raise ValueError("odd raw diff")
        changes = []
        for meta, path in zip(raw[:-1:2], raw[1:-1:2]):
            old, new, _, oid, status = meta.decode("ascii").split(" ")
            old = old.removeprefix(":")
            # ponytail: one cat-file per changed file; --batch-check if scope slows.
            size = 0 if new in ("000000", "160000") else int(
                _git(repo, "cat-file", "-s", _oid(oid)))
            changes.append({
                "status": status, "path": path.decode("utf-8"), "old_path": None,
                "old_mode": old, "new_mode": new, "size": size,
                "binary": binary[path], "reparse": new == "120000"})
    except (ValueError, KeyError) as exc:
        raise StageError("unparseable git diff") from exc
    return changes


def build_ports(*, store, manager, target, lease_target, run_id, boot_id, clock, lease,
                targets, jarvis_repo, checks, base_result, locked_fixture_hash,
                fixture_hash, review, plan_hash, worker, route, task, budget_exhausted,
                needs_input, verify_cwd, link_rescan, run_check=_run_check, binary_targets=()):
    """Return the ports object jarvis.work_driver.drive expects.

    lease_target is the literal relative name the lease is held under.
    review(stage, diff) returns a work_review.GateDecision; fixture_hash(dir)
    hashes the fixtures of a git-free export of the judged commit.
    """
    target = Path(target)
    notifier = NotificationCore(write=store.append_work_event, events=store.work_events)

    def events():
        return store.work_events(run_id)

    def next_seq():
        history = events()
        return history[-1].seq + 1 if history else 1

    def append_event(kind, step_key, payload, idempotency_key=None):
        event = WorkEvent(run_id, next_seq(), kind, clock(), step_key, event_payload_json(payload))
        return store.append_work_event(event, idempotency_key=idempotency_key)

    def notify(point, dedupe_key, step_key):
        return notifier.notify(run_id, point, dedupe_key, seq=next_seq(), at=clock(),
                               step_key=step_key)

    def lease_claim(step):
        return store.claim_work_lease(lease_target, run_id, boot_id, now=clock(), lease=lease)

    def lease_release(step):
        store.release_work_lease(lease_target, run_id, boot_id, now=clock())

    def stage_create(step):
        # A resumed step reattaches to its recorded stage; a slice never gets two.
        for event in reversed(events()):
            payload = _payload(event)
            if event.step_key == step.key and payload.get("gate") == "stage":
                stage = Stage(Path(payload["target"]), Path(payload["path"]),
                              payload["branch"], payload["base"])
                _identity(manager, stage)
                return stage
        stage = manager.create(target, run_id, step.key)
        append_event(EventKind.PROGRESS, step.key, {
            "gate": "stage", "target": str(stage.target), "path": str(stage.path),
            "branch": stage.branch, "base": stage.base})
        return stage

    def _pin(stage):
        run, slice_id = _identity(manager, stage)
        root = Path(manager.root)
        return root.parent / (root.name + "-repos") / run, f"refs/jarvis/judged/{run}/{slice_id}"

    def _judged(stage):
        # The only way a later port learns what was judged; never the stage.
        repo, ref = _pin(stage)
        try:
            return repo, _oid(_git(repo, "rev-parse", "--verify", "--end-of-options",
                                   f"{ref}^{{commit}}"))
        except StageError as exc:
            raise StageError("no judged commit pinned for this stage") from exc

    def scope_check(stage):
        repo, ref = _pin(stage)
        # This attempt's pin is earned below; a refused import leaves none.
        _git(repo, "update-ref", "--no-deref", "--stdin", input=f"delete {ref}\n".encode())
        manager._validate(stage)
        claim = manager._claim(stage)
        try:
            stage_git.import_commit(stage.path, repo, claim, stage.base)
        except (stage_git.StageGitError, ValueError) as exc:
            raise StageError(str(exc)) from exc
        _git(repo, "update-ref", "--no-deref", "--stdin", input=f"update {ref} {claim}\n".encode())
        return write_scope.check(_changes(repo, stage.base, claim), targets,
                                 jarvis_repo=jarvis_repo, binary_targets=binary_targets)

    def _with_export(stage, use):
        # Fresh per call: a check that rewrites its export cannot reach the hash.
        repo, commit = _judged(stage)
        with tempfile.TemporaryDirectory(prefix="jarvis-export-",
                                         ignore_cleanup_errors=True) as tmp:
            tree = Path(tmp) / "tree"
            try:
                stage_git.export_commit(repo, commit, tree)
            except (stage_git.StageGitError, ValueError, OSError) as exc:
                raise StageError(str(exc)) from exc
            return use(tree)

    def _judged_diff(stage):
        repo, commit = _judged(stage)
        return _git(repo, "diff-tree", "-r", "-p", "--binary", "--full-index", "--no-renames",
                    "--end-of-options", _oid(stage.base), commit)

    stage_holder = [None]

    def run_checks(stage):
        stage_holder[0] = stage
        return _with_export(stage, lambda tree: tuple(
            run_check(list(argv), stage=tree, jarvis_repo=jarvis_repo) for argv in checks))

    def regress(results):
        results = [r for r in results if not isinstance(r, RegressionVerdict)]
        if (not results or stage_holder[0] is None
                or any(r.runner is None or r.timed_out or r.truncated or r.exit_code is None
                       for r in results)):
            return RegressionVerdict(False, frozenset(), ("unparseable",))
        parsed = [parse_output(r.stdout + r.stderr, r.runner) for r in results]
        # A clean summary from a process that then crashed is not a pass.
        if any(r.exit_code != 0 and not p.failed_ids for r, p in zip(results, parsed)):
            return RegressionVerdict(False, frozenset(), ("unparseable",))
        stage_result = TestResult(sum(p.total for p in parsed),
                                  frozenset().union(*(p.failed_ids for p in parsed)))
        return compare_results(base_result, stage_result,
                               locked_fixture_hash=locked_fixture_hash,
                               stage_fixture_hash=_with_export(stage_holder[0], fixture_hash))

    def review_port(stage):
        decision = review(stage, _judged_diff(stage).decode("utf-8", "replace"))
        if decision.outcome == "accepted":
            return [{"severity": "MINOR", "detail": f"{f.file}:{f.line}:{f.text}"}
                    for f in decision.logged]
        if decision.outcome == "revise" and decision.blocking:
            return [{"severity": f.severity, "detail": f"{f.file}:{f.line}:{f.text}"}
                    for f in decision.blocking]
        return [{"severity": "BLOCKER", "detail": f"{decision.outcome}:{decision.reason}"}]

    def evidence(step, stage, results, verdict):
        output = b"".join((r.stdout + r.stderr).encode("utf-8", "replace") for r in results)
        return EvidenceReceipt(
            plan_hash=plan_hash, diff_sha=_sha256(_judged_diff(stage)),
            check_argv=tuple(" ".join(r.argv) for r in results),
            exit_code=max(r.exit_code for r in results), output_sha=_sha256(output),
            base_result={"total": base_result.total, "failed_ids": sorted(base_result.failed_ids)},
            verdict="passed" if verdict.passed is True else "failed")

    def tree_hash(stage):
        repo, commit = _judged(stage)
        try:
            return stage_git.tree_of(repo, commit)
        except (stage_git.StageGitError, ValueError) as exc:
            raise StageError(str(exc)) from exc

    def apply_intent(step):
        attempt, found = 1, None
        for event in events():
            if event.step_key != step.key:
                continue
            payload = _payload(event)
            if event.kind == EventKind.STEP_STATE:
                attempt = payload.get("attempt", attempt)
            elif payload.get("gate") == "apply_intent" and attempt == step.attempt:
                found = payload["idempotency_key"]
        if found is None:
            return None
        receipted = any(_payload(e).get("gate") == "apply" and _payload(e).get("passed") is True
                        and _payload(e).get("idempotency_key") == found
                        for e in events() if e.step_key == step.key)
        return found, receipted

    def apply(stage, receipt):
        raise StageError("S4")

    def applied(stage, key):
        raise StageError("S4")

    def undo(stage, key):
        raise StageError("S4")

    return types.SimpleNamespace(
        clock=clock, append_event=append_event, notify=notify, lease_claim=lease_claim,
        lease_release=lease_release, stage_create=stage_create, route=route, worker=worker,
        task=task, budget_exhausted=budget_exhausted, verify_cwd=verify_cwd,
        needs_input=needs_input, link_rescan=link_rescan, scope_check=scope_check,
        run_checks=run_checks, regress=regress, review=review_port, evidence=evidence,
        tree_hash=tree_hash, apply=apply, apply_intent=apply_intent, applied=applied, undo=undo)
