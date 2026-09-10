# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** dev-helper S2 process-level sharding — completed and
  integrated (`17589d2`): the helper suite runs as 4 isolated PowerShell shard
  processes, 736 passed / 0 failed, wall-clock 2:15.9 (previous S1/user
  baseline 8:07.4); no leftover shard processes, worktrees or temp bases;
  product code and product suite untouched. S1/S2 performance work is
  CLOSED — no further helper optimization is planned.
- **Dashboard:** 58% (GitHub capability 60%, Scheduler 92%).
- **Latest canonical main** includes the GitHub READ commit `5007c15`, the
  security correction `2301b53` → `83b701a` (atomic principal binding, 9 READ
  capabilities, `github_code_search` deferred post-v1, targeted GitHub tests
  155 OK), dev-helper S1 `b716905` and S2 `17589d2`; housekeeping commits
  after those do not change the product state recorded here.
- **Next implementation:** Gmail minimum usable-v1 (design not started).
  GitHub writes and `github_code_search` stay out of scope.
- **Contracts:** SQLite schema 10; output envelope 7 (GitHub READ moved it
  from 6 for the bounded `external_read` block). The security correction
  changed neither.
- **Active implementation worktree:** none. `dev.ps1 status` still reports the
  old `claude/scheduler-phase1b` recovery residue (PARTIAL REMOVAL — RECOVERY
  REQUIRED); it is left untouched on purpose and needs an explicit human
  recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
