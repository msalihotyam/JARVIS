---
name: jarvis-queue
description: Run a JARVIS slice queue (build/queue/NN-slug.md files) unattended in one lane, in order. Use when told to run, continue or resume a queue.
---

# JARVIS queue

`JARVIS_RUNBOOK.md` -> "Slice queue and unattended runs" is the authority;
this skill only says how to run it and never restates or overrides it. Each
slice itself follows `jarvis-slice`.

## Before the first slice

- Confirm the lane: a native worktree `.claude/worktrees/<slice-id>` on its
  own `worktree-<slice-id>` branch (`jarvis-lifecycle` section 1; a
  `dev.ps1` helper worktree on `claude/<task>` is the fallback), clean, not
  canonical main. Not on a worktree branch: stop and report.
- List `build\queue\NN-*.md` in order. Skip slices that already have a
  `NN-<slug>.report.md` with `STATUS: PASS`.

## Per slice, in order

1. Read the slice prompt in full. It is the whole brief: its tier, owned
   files, acceptance and verification are not negotiable here.
2. Run it by `jarvis-slice`: start checks, boundary, tier verification,
   review subagent for T0 / T1, commit on this worktree branch.
3. Write `NN-<slug>.report.md` next to the prompt (the fixed report of
   `jarvis-slice`).
4. PASS: go to the next slice. BLOCKED or any red result: write the report and
   **stop the whole queue**; do not skip ahead.

## Unattended means

- Ask nothing; do not wait for approval to test or commit.
- Commit only on the lane's worktree branch, never on canonical main.
- Never push `main` (the `ci/*` exception is as in the runbook); no
  integration, no cleanup, no worktree removal, no `ExitWorktree` remove (keep
  only), no REBOUND answer.
- A slice that needs a decision, a higher tier than named, or a file outside
  its owned files is BLOCKED, not improvised.

## At the end

Print one line per slice: number, slug, STATUS, commit hash. Integration
("Parallel lanes": ff-merge or cherry-pick, only on Salih's "entegre et")
stays with the orchestrator.
