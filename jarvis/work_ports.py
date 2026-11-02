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

S4: apply moves the pin onto refs/heads/jarvis/<run>/main in the target in one
atomic update-ref transaction with its record, refs/jarvis/applied/<run>/
<sha256(key)>/{old,new}. applied reads only that record; undo restores the
recorded old under the same expected-value transaction.
"""

import hashlib
import json
import os
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


def _ref(repo, ref):
    """The value of a plain ref, None when absent; a symbolic ref fails closed."""
    listing = ("for-each-ref", "--format=%(refname) %(objectname) %(symref)")
    lines = _git(repo, *listing, ref).decode("utf-8", "replace").splitlines()
    if not lines:
        # for-each-ref skips a broken ref with rc 0, so absence needs checks that fail on one.
        try:
            # The full name tells ref itself from what rev-parse would guess, like refs/tags/<ref>.
            named = _git(repo, "rev-parse", "--revs-only", "--symbolic-full-name",
                         "--end-of-options", ref).decode("utf-8", "replace").strip()
            # A named lookup binary-searches a "sorted" packed-refs and can step over a bad line.
            rows = _git(repo, *listing).decode("utf-8", "replace").splitlines()
            # rev-parse steps past a zero OID file to a same-named tag, and the transaction misses it.
            present = named == ref or (Path(repo) / ".git" / ref).is_file()
            if not present:
                _git(repo, "update-ref", "--no-deref", "--stdin",
                     input=f"start\nverify {ref}\nprepare\nabort\n".encode())
        except StageError as exc:
            raise StageError(f"broken ref refused: {ref}: {exc}") from exc
        if not present:
            return None
        # ref exists but the named listing missed it: a broken file, listed nowhere, or a loose
        # ref under a directory another branch spelled in another case, listed by that name.
        lines = [row for row in rows if row.split(" ")[0].casefold() == ref.casefold()]
        if not lines:
            raise StageError(f"broken ref refused: {ref}")
    fields = lines[0].split(" ")
    if len(lines) != 1 or len(fields) != 3 or fields[0].casefold() != ref.casefold():
        raise StageError("unexpected ref listing")
    if fields[2]:
        raise StageError(f"symbolic ref refused: {ref}")
    return _oid(fields[1])


def _refuse_checked_out(target, ref):
    """Raise when any worktree of target has ref checked out, or may have it."""
    # git reports a garbage, directory, missing or over-deep worktree HEAD as rc 0 with a
    # zero HEAD, and skips an unreadable worktree record, so neither reads as "not checked out".
    records = _git(target, "worktree", "list", "--porcelain", "-z").split(b"\0\0")
    gitdir = Path(target) / ".git"
    try:
        linked = os.listdir(gitdir / "worktrees")
    except FileNotFoundError:
        linked = []
    except OSError as exc:
        raise StageError("unreadable worktree records") from exc
    if records.pop() or len(records) != 1 + len(linked):
        raise StageError("worktree records do not match git's listing")
    for record in records:
        attrs = record.decode("utf-8", "surrogateescape").split("\0")
        head = next((a[5:] for a in attrs if a.startswith("HEAD ")), "")
        branches = [a[7:] for a in attrs if a.startswith("branch ")]
        if not _OID.fullmatch(head) or not head.strip("0") or not (branches or "detached" in attrs):
            raise StageError("unreadable worktree HEAD")
        # NTFS and APFS fold ref names: HEAD naming Int moves when int moves.
        if any(branch.casefold() == ref.casefold() for branch in branches):
            raise StageError(f"integration ref is checked out: {ref}")
    # A paused rebase lists as detached, and finishing it force-moves the branch it names.
    for base in (gitdir, *(gitdir / "worktrees" / name for name in linked)):
        for state in ("rebase-merge", "rebase-apply"):
            try:
                head = (base / state / "head-name").read_bytes()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                raise StageError("unreadable rebase state") from exc
            if head.decode("utf-8", "surrogateescape").strip().casefold() == ref.casefold():
                raise StageError(f"integration ref is checked out: {ref}")


def _transact(repo, *lines):
    body = "\n".join(("start", *lines, "prepare", "commit")) + "\n"
    _git(repo, "update-ref", "--no-deref", "--stdin", input=body.encode())


def build_ports(*, store, manager, target, lease_target, run_id, boot_id, clock, lease,
                targets, jarvis_repo, checks, base_result, locked_fixture_hash,
                fixture_hash, review, plan_hash, worker, route, task, budget_exhausted,
                needs_input, verify_cwd, link_rescan, run_check=_run_check, binary_targets=()):
    """Return the ports object jarvis.work_driver.drive expects.

    lease_target is the literal relative name the lease is held under.
    review(stage, diff) returns a work_review.GateDecision; fixture_hash(dir)
    hashes the fixtures of a git-free export of the judged commit.
    """
    # run_id reaches ref names and update-ref's stdin, so a newline would inject transaction lines.
    if not isinstance(run_id, str) or not re.fullmatch(_ID, run_id):
        raise StageError("invalid run or slice identifier")
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
        # A later slice builds on what the earlier ones applied; only the first starts at HEAD.
        current = _ref(target, f"refs/heads/jarvis/{run_id}/main")
        stage = manager.create(target, run_id, step.key, base=current or "HEAD")
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

    def _pending_key(stage):
        # The newest intent of the step that owns this stage (attempts only grow).
        history = events()
        owners = {e.step_key for e in history
                  if _payload(e).get("gate") == "stage" and _payload(e).get("branch") == stage.branch}
        for event in reversed(history):
            payload = _payload(event)
            if event.step_key in owners and payload.get("gate") == "apply_intent":
                return payload["idempotency_key"]
        raise StageError("no apply intent recorded for this stage")

    def _refs(stage, key):
        run, _ = _identity(manager, stage)
        return f"refs/heads/jarvis/{run}/main", f"refs/jarvis/applied/{run}/{_sha256(key.encode())}"

    def apply(stage, receipt):
        repo, commit = _judged(stage)
        if commit == stage.base:
            raise StageError("nothing to apply")
        ref, records = _refs(stage, _pending_key(stage))
        # Early refusal only; the transaction's expected old is the authority.
        if _ref(target, ref) != stage.base:
            raise StageError(f"{ref} is not at the stage base; re-run")
        _refuse_checked_out(target, ref)
        try:
            stage_git.import_commit(repo, target, commit, stage.base)
        except (stage_git.StageGitError, ValueError) as exc:
            raise StageError(str(exc)) from exc
        _refuse_checked_out(target, ref)  # a checkout may have appeared during the import
        _transact(target, f"update {ref} {commit} {stage.base}",
                  f"create {records}/old {stage.base}", f"create {records}/new {commit}")
        return commit

    def applied(stage, key):
        ref, records = _refs(stage, key)
        old, new, current = _ref(target, f"{records}/old"), _ref(target, f"{records}/new"), _ref(target, ref)
        if old is None and new is None and current == stage.base:
            return False
        if old is not None and new is not None and new == current and old == stage.base:
            return True
        raise StageError("apply record does not match the integration ref; operator decides")

    def undo(stage, key):
        ref, records = _refs(stage, key)
        old, new = _ref(target, f"{records}/old"), _ref(target, f"{records}/new")
        if old is None and new is None:
            if _ref(target, ref) != stage.base:
                raise StageError(f"no apply record and {ref} is not at the stage base")
            return
        if old is None or new is None:
            raise StageError("partial apply record; operator decides")
        # --no-deref would overwrite a symbolic ref, so read it here as apply does.
        if old != stage.base or _ref(target, ref) != new:
            raise StageError("apply record does not match the integration ref; operator decides")
        _refuse_checked_out(target, ref)
        _transact(target, f"update {ref} {old} {new}",
                  f"delete {records}/old {old}", f"delete {records}/new {new}")

    return types.SimpleNamespace(
        clock=clock, append_event=append_event, notify=notify, lease_claim=lease_claim,
        lease_release=lease_release, stage_create=stage_create, route=route, worker=worker,
        task=task, budget_exhausted=budget_exhausted, verify_cwd=verify_cwd,
        needs_input=needs_input, link_rescan=link_rescan, scope_check=scope_check,
        run_checks=run_checks, regress=regress, review=review_port, evidence=evidence,
        tree_hash=tree_hash, apply=apply, apply_intent=apply_intent, applied=applied, undo=undo)
