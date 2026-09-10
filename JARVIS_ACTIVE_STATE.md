# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** GitHub usable-v1 READ security correction (identity bound
  to the credential, then atomic principal binding: `viewer.login` and the
  requested data come from the same authenticated GraphQL request; 9 READ
  capabilities; `github_code_search` deferred post-v1) and dev-helper S1
  optimization (one canonical repo template, one copy per test) — completed,
  integrated and cleaned. Targeted GitHub tests: 155 OK.
- **Dashboard:** 58% (GitHub capability 60%, Scheduler 92%).
- **Latest canonical main** includes the GitHub READ commit `5007c15`, the
  security correction `2301b53` → `83b701a` (atomic principal binding) and
  dev-helper S1 `b716905`; housekeeping commits after those do not change the
  product state recorded here.
- **Next implementation:** dev-helper S2 process-level sharding (dev tooling,
  no product code). Gmail minimum remains the next product milestone. GitHub
  writes and `github_code_search` stay out of scope.
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
