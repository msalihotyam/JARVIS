# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" + "Salih kararları — V2.0 Gate 6".

Active gate: Projects Gate 7 (E2E + packaged dogfood + rollback); phase A
done, phase B ran early. Gates 1–5, 6A–6D-b and S5 (model list, §4.25) are
complete on main; nothing in flight. Installed: `1.0.0-8fbbaef` (predates
6D-a/6D-b/S5); previous `1.0.0-1c68161`; live DB opened by 8fbbaef.

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner
  fixed (only exception 6C, Global → project, one-way, once); scope is
  context, never authority; no `SCHEMA_VERSION` bump while schema-10 releases
  are rollback targets (additive tables only, no ALTER).
- Gates 1–5 (§4.22–§4.24) incl. project archive, instructions, files.
- Gate 6 UX: Projects on Ctrl+6, History keeps Global; mouse-first; selector
  lists active projects; purge by typing the row's name; 6C entry only in the
  selector menu; 6D-a switcher + rename.
- 6D-b: S1 two-step archive → "Kalıcı sil", single-button honest dialog; S2
  delete only from archived / archived-project; S3 GAP B (`jarvis run/text
  --session-id` on a deleted conversation's session, no project) accepted for
  V2.0, no Core change; S4 a deleted legacy import keeps
  `~/.jarvis/conversation.json`.
- S5: per model NAME the highest version only; selected never hidden; toggle
  window-only, default off. Purge residue: §4.24 as is, scrubbing deferred.
  V2.1 owns `conversation_global_memory_toggles`.

## Decisions / external / backlog

- S6: Gate 6E visual polish of all 6A–6D surfaces + the model panel before
  the re-install (incl. "Yeni Sohbet" in History/project lists).
- Gate 7: D-1, D-2 done. D-3 one approved live Claude call after Salih's own
  CLI re-login. D-4 packaged -Rollback to 1c68161 and back (approvals
  cleared, conversation.json backed up and restored). Codex review queue
  (one commit per session): session1 remainder, Gate 5, 6A–6D-b, fixes.
- Load-sensitive list (RUNBOOK rule D; only the orchestrator edits):
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b` and
  `test_ui.PrePolishTests.test_the_gauges_show_the_kernel_s_figures_or_nothing_at_all`
  (both hardened, stable under 14 CPU hogs) and
  `test_ui.TypographyTests.test_normal_launch_path_is_clean`.
- PRODUCT DEFECT (unfixed; launch-path test left as is): `Bridge._catalog_changed`
  emits `_catalogDone` from `jarvis-catalog-*` threads after the Bridge is
  deleted at shutdown -> "Signal source has been deleted" traceback (9/30
  launches under load, exit 0). Needs a shutdown guard/join in the bridge.
- Test speed: `-Parallel` 916 s vs serial 1577 s (same 4640 tests, 2
  skipped); slowest are test_ui window tests (max 26.6 s), non-UI max 11.6 s.
- Backlog: project-filtered jobs query; catalog test skips on a system
  Python; Gate 4B Edge retirement deferred; roadmap item "Dağıtım: installer
  + onboarding" (after V2.1). F1 (`build_release.ps1` exit code) fixed.
- Push: `main` not authorized (ahead of `origin/main` `4f5a93d`); `ci/*`
  branches are standing-authorized (RUNBOOK "Push"); CI = `scripts\ci.ps1`.

## Next action (in order)

1. Gate 6E visual polish (6A–6D + model panel). 2. Full suite once on the
release HEAD (`-Parallel` ok). 3. Back up `releases\1.0.0-4f5a93d` outside
Apps, re-install (Salih's go). 4. Dogfood (D-2/D-3). 5. Rollback (D-4). 6.
V2.0 close.

## Rollback / safety anchors

- Releases: `1.0.0-8fbbaef` active; `1.0.0-1c68161` previous (Chrome);
  `1.0.0-4f5a93d` retained (Edge; `data\browser-profile` kept).
- `%USERPROFILE%\JARVIS-backups`: `releases\legacy-1.0.0-5976769` (+
  `.manifest.sha256`, 2929 files verified) and `2026-09-30-gate7`
  (`data\jarvis.db` + `~\.jarvis\*.json`, SHA-256 manifest, taken before
  8fbbaef first opened the DB).
- `data\jarvis.pre-v2.db` verified; `SCHEMA_VERSION` stays 10 while any
  schema-10 release is a rollback target; V2 only reads
  `~/.jarvis/conversation.json`.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
