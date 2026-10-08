---
name: jarvis-lifecycle
description: JARVIS development lifecycle rules — how to identify the current repo/worktree/branch, tell canonical main from helper-managed and Claude-managed worktrees, and sequence integration, cleanup, dashboard/ACTIVE_STATE housekeeping and push. Use before starting, finishing, integrating or cleaning up any JARVIS development task, before any worktree removal, before updating the dashboard or JARVIS_ACTIVE_STATE.md, and whenever proposing the next action.
---

# JARVIS development lifecycle

`JARVIS_RUNBOOK.md` is the canonical authority. Read it for the rules
themselves — model policy, prompt shape, chat rotation, test budget, the full
worktree lifecycle HARD RULE. This skill only says how to apply them without
being told again, and never restates or overrides them. Where this file and
the runbook disagree, the runbook wins. Slices and queues: `jarvis-slice`,
`jarvis-queue`.

## 1. Identify where you actually are

Before any lifecycle action, establish the facts. Never infer them from the
prompt, the task name, or what a previous turn said.

```
git rev-parse --show-toplevel
git worktree list
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --porcelain
```

Three distinct kinds of location, and they are not interchangeable:

- **Canonical main** — `C:\Users\realk\Masaüstü\JARVIS`, branch `main`. Where
  integration lands and where dashboard/ACTIVE_STATE housekeeping happens.
- **Native worktree (the code-lane default)** — created by `EnterWorktree`
  under `.claude/worktrees/<slice-id>` on branch `worktree-<slice-id>`, from
  local `HEAD` (`baseRef head`). The chat stays bound to canonical.
- **Helper-managed task worktree (fallback)** — created by `dev.ps1`, a
  *sibling* of canonical main named `JARVIS-<Worker>-<task>`, on a branch like
  `claude/<task>`, with helper-owned state and an ownership marker.

**Never guess the type.** If the path, the worktree list and the branch do not
together make the type unambiguous, stop and ask. A wrong guess here is what
turns a cleanup into a boundary violation.

## 2. Route by worktree type

- A **native** worktree follows RUNBOOK "Parallel lanes": `EnterWorktree
  name=<slice-id>` → work and commit there → report → `ExitWorktree` keep.
  Integration only on Salih's "entegre et", from canonical on a clean main
  (`git merge --ff-only worktree-<slice-id>`, else `git cherry-pick
  main..worktree-<slice-id>`; a conflict is aborted and reported), then verify,
  run the tier's suite and `scripts\ci.ps1`, then `git worktree remove` (no
  `--force`) and the branch deletion the runbook names. It must **never** be
  sent through helper `finish` / `finish-current` / `cleanup`. While inside
  it, never run git against the canonical checkout.
- **Base check before any lane work:** the chat must have been launched in
  canonical (a chat launched from a `dev.ps1` helper folder does not load
  canonical's `.claude/settings.json`, so `EnterWorktree` would branch from
  `fresh` origin/main, or from the helper's own HEAD). Right after
  `EnterWorktree`, `git log -1` must equal the prompt's expected base SHA; on a
  mismatch STOP. Recovery without a reset: re-enter the kept worktree, then
  `git merge --ff-only <expected base>` (only if the wrong base is an
  ancestor).
- A **helper-managed** task (fallback) goes through the helper lifecycle end
  to end: `dev.ps1 new` → work → `dev.ps1 finish-current` → `dev.ps1
  cleanup`. Do not hand-roll `git worktree remove` or branch deletion for it.
- Canonical main is never "finished" or "cleaned up" at all.

## 3. Verify before integrating or cleaning up

Integration and cleanup are **separate steps**, in that order, each with its
own verification. Never treat one as implying the other, and never bundle
them into a single action.

Before either, re-verify live (not from memory): the worktree list, the
branch, HEAD, and `git status --porcelain` empty. Before cleanup also confirm
the work was accepted and already integrated.

For a **helper-managed** worktree, per the runbook's HARD RULE, before any
destructive cleanup the persistent development chat must already be **rebound
to canonical**
`C:\Users\realk\Masaüstü\JARVIS`. Answering the helper's `REBOUND` prompt
before that is true is itself the violation. Rebind first, verify, then
answer. (Native worktrees need no rebinding and no REBOUND answer.)

## 4. Stop conditions

- **GREEN MEANS STOP.** When the required suite passes, commit and stop. No
  extra audits, hardening passes, refactors or self-review after green. A new
  concrete blocker found after the final green run is *reported*, not
  silently fixed.
- **Never push without explicit authorization.** Authorization is per-push and
  never carries forward from a previous one.

## 5. Canonical housekeeping

`dev/dashboard/status.json` and `JARVIS_ACTIVE_STATE.md` are updated **only
after** the accepted work is integrated on canonical main — never from inside
a task worktree, never in anticipation. Percentages are computed by the
renderer, never written by hand (`dev/dashboard/UPDATE.md`). Ordinary bugfixes
and test reruns need no dashboard update.

## 6. Before proposing the next action

State, in this order, grounded in what you just verified:

1. **NEREDEYİZ** — where we are right now.
2. **ŞİMDİ NE YAPIYORUZ** — what we are doing now.
3. **SONRA NE VAR** — what comes after.
