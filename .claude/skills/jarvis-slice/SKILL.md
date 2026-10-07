---
name: jarvis-slice
description: The fixed part of every JARVIS slice - start checks, owned-files boundary, the test and verification tier steps, the commit message form and the fixed 15-line report. Use at the start and the end of any JARVIS implementation, docs or test slice, including each slice of a queue.
---

# JARVIS slice

`JARVIS_RUNBOOK.md` is the authority ("Parallel lanes", "Verification tiers
(risk)", "Testing discipline", "Standard gate order & exits", "Push"). This
skill only says how to apply it in every slice, and never restates or
overrides it. Where the two disagree, the runbook wins.

## 1. Start

- Bootstrap per the runbook ("Session bootstrap"): runbook,
  `JARVIS_ACTIVE_STATE.md`, the active roadmap section, then the freshness
  check. Contradiction: stop and report.
- Establish the facts live (`jarvis-lifecycle` section 1): toplevel, worktree
  list, branch, HEAD, `git status --short`. Never from the prompt. A code
  slice runs inside its native worktree (`EnterWorktree name=<slice-id>`);
  when it ends, commit, report, `ExitWorktree` keep.
- Read from the prompt: the tier (T0 / T1 / T2; unsure = higher), the
  MODEL / EFFORT lines and the **owned files**.

## 2. Boundary

- Edit only the owned files. A file another lane owns, a central single-owner
  surface, or code of a higher tier than named: stop and report (fail closed).
- Stage explicit paths only. Never `uv.lock`, never `dev/dashboard/*`.
- No push (the `ci/*` exception per the runbook). No reset / restore / delete
  / stash of work.

## 3. Verify

1. Implement with only the smallest related tests.
2. Freeze, then run the TEST BUDGET tier for the change ONCE (targeted, then
   the tier's required run; rule D for the listed load-sensitive tests).
3. T0 / T1: screenshots for visible changes (`scripts/ui_visual.py`), then ONE
   independent review subagent with fresh context. Give it the diff and the
   acceptance criteria only; it returns PASS or findings. Fix real findings
   (re-run only what they affect), then commit without waiting.
4. T2: the full flow of "Standard gate order & exits"; no self-verified
   shortcut.
5. `git diff --check`. GREEN MEANS STOP.

## 4. Commit

`<type>(<scope>): <what and why, one line>` - types `feat`, `fix`, `test`,
`docs`, `ci`; the body states the tier and the verification in a few lines.
End with the attribution line the session gives. No amend unless it completes
that same unpushed commit (say so on the first report line).

## 5. Report (at most 15 lines, plain text)

```
STATUS: PASS | BLOCKED
TIERS: risk T0|T1|T2; test tier run
COMMIT: <hash> <subject>   (BLOCKED: what is uncommitted)
FILES: <paths>
RUNS: <command: real counts, duration> per run
REVIEW: <subagent verdict PASS | findings and what was done> (T0/T1)
SHOTS: <screenshot paths> or none
GIT STATUS --SHORT: <output>
PUSHED: no
OPEN ISSUES: <none | list>
```

Real counts and durations only. A queue slice also writes the same text to
its `NN-<slug>.report.md` (`jarvis-queue`).
