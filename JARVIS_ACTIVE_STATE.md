# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" (scope freeze, Next release inbox, slice estimates) and §3.7
"Salih kararları — V2.0 Gate 6G / 6H planı". V2.0 scope is frozen (Salih
2026-10-06): only bugs and blockers.

Gates 1–5, 6A–6F (6F = left sidebar, F1–F7) and S5 (model list) are complete
on main; `ci/gate6f` ran green (run 37425225354). Installed:
`1.0.0-a26d790` (predates 6F F6/F7); previous `1.0.0-8fbbaef`; `1.0.0-1c68161`
retained. Local `main` is ahead of `origin/main` `4f5a93d`, not pushed.

S0 speed-up is done: S0a rules (2eb095b), S0b test/CI speed (7bb272b; local
full suite 445 s, CI 4 shards 11.3 min). **Gate 6G** is under way: K1 icon
set and the F-ee reflow fix are on main; **K2** motion contract (Theme.dur
gate, 4 px travel, one HoverTip, calm idle core, destructive dialogs at once)
is committed on lane branch `worktree-6g-k2-motion` (full suite green, 4764),
waiting for Salih's "entegre et".

## Open blockers

- OB1 Part 1 (K2 fix-up, destructive confirm input guard) BLOCKED: its full
  suite had 3 failures, each green alone 3/3: `test_projects_ui_6f`
  `SidebarShellTests.test_i...` at 1100x700 and 1920x1080 (still frame taken
  mid-reflow, the F-ee2 defect) and `test_browser_lifecycle`
  `FailClosedTests.test_a...`; none is on the load-sensitive list. Uncommitted
  in `.claude/worktrees/6g-k2-motion`: `jarvis/ui/qml/theme/Theme.qml`,
  `jarvis/ui/qml/components/JarvisDialog.qml`,
  `jarvis/ui/qml/overlays/ConfirmDialog.qml`, `tests/test_projects_ui_6e.py`,
  `tests/test_motion_6g.py`, `tests/test_conversation_lifecycle_6d.py`.
  OB1 Parts 2-4 not started.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner
  fixed (only exception 6C, Global → project, one-way, once); scope is
  context, never authority; no `SCHEMA_VERSION` bump while schema-10 releases
  are rollback targets (additive tables only, no ALTER).
- Gates 1–5 (§4.22–§4.24) incl. project archive, instructions, files.
- Gate 6 UX (6A–6D-b, 6E, 6F) as built; 6F F7: a Global conversation deletes
  in one step, an active project's unarchived one must be archived first.
- The 6G/6H plan as recorded in the roadmap (mockup v16 approved 2026-10-05).
- Mechanical lamp, freeze-key skins 3–4 and the usage popover's
  context-window block: V2.1. V2.1 owns `conversation_global_memory_toggles`.

## Decisions / external / backlog

- Load-sensitive list (RUNBOOK rule D; only the orchestrator edits):
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b` and
  `test_ui.PrePolishTests.test_the_gauges_show_the_kernel_s_figures_or_nothing_at_all`.
- Cloud sessions: unused until the cloud-branch push decision (RUNBOOK).
- Push: `main` not authorized; `ci/*` branches are standing-authorized
  (RUNBOOK "Push"); CI = `scripts\ci.ps1`.
- Old dashboard retired (Salih 2026-10-06). The orchestrator's board is the
  artifact "JARVIS Pano" (https://claude.ai/artifact/LboCEVPnvVb9QapsNbD8GZ).
- Gate 7 still to do: D-3 one approved live Claude call after Salih's own CLI
  re-login; D-4 packaged `-Rollback` and back (approvals cleared,
  `conversation.json` backed up and restored). Codex review queue (one commit
  per session): session1 remainder, Gate 5, 6A–6D-b, fixes.
- Backlog: project-filtered jobs query; catalog test skips on a system
  Python; Gate 4B Edge retirement deferred; regenerate
  `scripts/test_durations.json` now and then (`--write-durations`).

## Next action (in order)

1. Integrate K2 (on "entegre et"). 2. Gate 6G (the remaining 6G slices;
F8/F9 folded in). 3. Gate 6H (authority review). 4. Full suite / CI on the
integrated release HEAD. 5. Re-install (Salih's go). 6. Dogfood (D-2/D-3).
7. Rollback (D-4). 8. V2.0 close.

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
- Dashboard WIP is parked in git stash "Salih dashboard WIP (2026-09-12),
  parked 2026-10-06"; never stage `dev/dashboard/*`.
- `uv.lock` is excluded locally via `.git/info/exclude`; never stage or
  commit it.
