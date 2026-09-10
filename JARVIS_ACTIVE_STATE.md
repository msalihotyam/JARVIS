# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** Scheduler Phase 2D minimum foreground runner — completed
  and integrated.
- **Dashboard:** 53% (Scheduler capability 85%; closing 2D was offset by the
  new operator-surface milestone entering the denominator).
- **Latest canonical main** includes the 2D runner commit `a94e67b` and the
  dev-helper finish-preflight fix `bce7f03`.
- **Next implementation:** Scheduler Operator Surface — minimum.
- **Active implementation worktree:** none. `dev.ps1 status` still reports the
  old `claude/scheduler-phase1b` recovery residue (PARTIAL REMOVAL — RECOVERY
  REQUIRED); it is left untouched on purpose and needs an explicit human
  recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
