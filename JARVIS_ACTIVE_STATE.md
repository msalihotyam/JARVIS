# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** Scheduler minimum operator surface (`jarvis job create /
  list / enable / disable / archive`) — completed, integrated and cleaned.
- **Dashboard:** 54% (Scheduler capability 92%).
- **Latest canonical main** is the operator-surface commit `0b4ded8`.
- **Next implementation:** GitHub READ (Phase 2 process seam + auth policy,
  then live read). Write operations are out of scope.
- **Active implementation worktree:** none. `dev.ps1 status` still reports the
  old `claude/scheduler-phase1b` recovery residue (PARTIAL REMOVAL — RECOVERY
  REQUIRED); it is left untouched on purpose and needs an explicit human
  recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
