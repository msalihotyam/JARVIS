# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** GitHub usable-v1 READ (process seam, read connector,
  config-based repository authority) — completed, integrated and cleaned.
- **Dashboard:** 58% (GitHub capability 60%, Scheduler 92%).
- **Latest canonical main** is the GitHub READ commit `5007c15`.
- **Next implementation:** Gmail minimum. GitHub writes stay out of scope.
- **Contracts:** SQLite schema 10; output envelope 7 (GitHub READ moved it
  from 6 for the bounded `external_read` block).
- **Active implementation worktree:** none. `dev.ps1 status` still reports the
  old `claude/scheduler-phase1b` recovery residue (PARTIAL REMOVAL — RECOVERY
  REQUIRED); it is left untouched on purpose and needs an explicit human
  recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
