# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" + "Salih kararları — V2.0 Gate 6". V2.0 scope is frozen
(Salih 2026-10-06; RUNBOOK "V2.0 scope freeze").

Gates 1–5, 6A–6F (6F = left sidebar, F1–F7) and S5 (model list) are complete
on main; `ci/gate6f` ran green (run 37425225354). Installed:
`1.0.0-a26d790` (predates 6F F6/F7); previous `1.0.0-8fbbaef`; `1.0.0-1c68161`
retained. Local `main` is ahead of `origin/main` `4f5a93d`, not pushed.

Now: **S0 speed-up** (Salih 2026-10-06; RUNBOOK "Parallel lanes",
"Verification tiers (risk)", "Slice queue and unattended runs"). S0a = the
rules/skills docs slice (this one, canonical main). S0b = test/CI speed, in
its own worktree; its numbers land in G0, not before.

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner
  fixed (only exception 6C, Global → project, one-way, once); scope is
  context, never authority; no `SCHEMA_VERSION` bump while schema-10 releases
  are rollback targets (additive tables only, no ALTER).
- Gates 1–5 (§4.22–§4.24) incl. project archive, instructions, files.
- Gate 6 UX (6A–6D-b, 6E, 6F) as built; 6D-b S1–S4 and S5 as recorded in the
  roadmap. V2.1 owns `conversation_global_memory_toggles`.
- Mechanical lamp and freeze-key skins 3–4: V2.1.

## Decisions / external / backlog

- Load-sensitive list (RUNBOOK rule D; only the orchestrator edits):
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b` and
  `test_ui.PrePolishTests.test_the_gauges_show_the_kernel_s_figures_or_nothing_at_all`.
- Cloud sessions: not used until the push-of-a-cloud-branch decision is
  recorded (RUNBOOK "Cloud sessions").
- Push: `main` not authorized; `ci/*` branches are standing-authorized
  (RUNBOOK "Push"); CI = `scripts\ci.ps1`.
- Gate 7 still to do: D-3 one approved live Claude call after Salih's own CLI
  re-login; D-4 packaged `-Rollback` and back (approvals cleared,
  `conversation.json` backed up and restored). Codex review queue (one commit
  per session): session1 remainder, Gate 5, 6A–6D-b, fixes.
- Backlog: project-filtered jobs query; catalog test skips on a system
  Python; Gate 4B Edge retirement deferred; roadmap item "Dağıtım: installer
  + onboarding" (after V2.1).

## Next action (in order)

1. S0b lands (test/CI speed). 2. G0 docs slice (roadmap "Next release inbox",
test-speed numbers, architecture/context touch-ups). 3. Gate 6G. 4. Full
suite once on the integrated release HEAD (CI on the same SHA allowed).
5. Re-install (Salih's go). 6. Dogfood (D-2/D-3). 7. Rollback (D-4).
8. V2.0 close.

## Rollback / safety anchors

- Releases: `1.0.0-a26d790` active; `1.0.0-8fbbaef` previous (rollback
  available); `1.0.0-1c68161` retained (Chrome).
- `%USERPROFILE%\JARVIS-backups`: `releases\legacy-1.0.0-5976769` (+
  `.manifest.sha256`, 2929 files verified) and `2026-09-30-gate7`
  (`data\jarvis.db` + `~\.jarvis\*.json`, SHA-256 manifest, taken before
  8fbbaef first opened the DB).
- `data\jarvis.pre-v2.db` verified; `SCHEMA_VERSION` stays 10 while any
  schema-10 release is a rollback target; V2 only reads
  `~/.jarvis/conversation.json`.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
