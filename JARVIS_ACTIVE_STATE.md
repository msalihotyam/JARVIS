# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" + "Salih kararları — V2.0 Gate 6".

Active gate: Projects Gate 7 (E2E + packaged dogfood + rollback). IN PROGRESS:
phase A done; phase B ran early (see Installed); rest in "Next action".

Status: Gates 1–5 and 6A–6D-b complete on main; nothing in flight. 6D-b
(§4.22/§4.25, Salih S1–S4 2026-09-30): reversible conversation archive →
permanent delete (tombstone + additive `conversation_deletions`, schema 10),
archived sections, open-conversation transition, GAP A drop, 6C re-open with
state. Base commit: `feat(projects): archive, unarchive and permanently delete
conversations`.

Installed: `1.0.0-8fbbaef` active since 2026-09-30T11:22Z (predates
6D-a/6D-b); previous `1.0.0-1c68161`; legacy pruned by that install. Live DB
opened by 8fbbaef at 14:48 with project "deneme". Codex CLI updated by Salih.

## Roles

- Orchestrator: Claude; reviewer: ChatGPT (one-pass); final decisions: Salih
  (2026-09-29). Protocol: RUNBOOK "Governance and prompt review".

## Open blockers

- Model-list gate (S5) BLOCKED at the full-suite step. Implementation and
  tests are complete but UNCOMMITTED in the tree: `jarvis/model_retirement.py`,
  `jarvis/ui/bridge.py`, `jarvis/ui/qml/overlays/ModelPanel.qml`,
  `jarvis/ui/i18n/jarvis_tr-TR.ts` + `.qm`, `scripts/ui_visual.py`,
  `ARCHITECTURE.md`, new `tests/test_model_list_newest.py`. Targeted: 52 OK
  (17 new). Full direct suite: 4640 run, 2 failures, neither the sourced
  RailSwitch exception: `test_ui.PrePolishTests.test_the_gauges_show_the_kernel_s_figures_or_nothing_at_all`
  and `test_ui.TypographyTests.test_normal_launch_path_is_clean` (QML
  "devFixture of null" at launch). Both PASS when run alone; run took 1630 s
  (load). Not shown to be caused by S5. Salih decides: rerun the full suite,
  or accept these two as load flakes, then commit.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner fixed
  (only exception 6C, Global → project, one-way, once); scope is context,
  never authority; no `SCHEMA_VERSION` bump while schema-10 releases are
  rollback targets (additive tables only, no ALTER).
- Gates 1–5 (§4.22–§4.24) incl. project archive, instructions, files.
- Gate 6 UX (Salih 2026-09-29/30): Projects on Ctrl+6, History keeps Global;
  mouse-first; selector lists active projects; purge by typing the row's name;
  6C entry only in the selector menu; 6D-a switcher + rename.
- 6D-b: S1 two-step archive → "Kalıcı sil", single-button honest dialog; S2
  delete only from archived / archived-project; S3 GAP B (`jarvis run/text
  --session-id`, no project, on a deleted conversation's session) accepted
  for V2.0, no Core change; S4 a deleted legacy import keeps
  `~/.jarvis/conversation.json`.

## Uncommitted / external decisions

- S5 model list (Salih 2026-09-30, next gate): per model NAME keep only the
  highest version (Sol, Luna, Terra, Astra, Opus, Sonnet, Haiku, Fable are
  separate names that never hide each other); unversioned/-latest, preview,
  context variants ([1m]) and unparseable ids stay; the selected model is
  never hidden (a move is offered); "Eski modelleri göster" toggle, default
  off; Codex's own declared upgrades still apply first.
- S6 (Salih 2026-09-30): Gate 6E visual polish of all 6A–6D surfaces before
  the re-install and dogfood (incl. "Yeni Sohbet" in History/project lists).
- Gate 7: D-1 and D-2 done; D-3 one approved live Claude call after Salih's
  own CLI re-login; D-4 packaged -Rollback to 1c68161 and back (approvals
  cleared, conversation.json backed up and restored).
- Purge residue (Salih 2026-09-30): §4.24 as is; scrubbing copies deferred.
  V2.1 owns `conversation_global_memory_toggles` (roadmap contract).
- Codex review queue (Salih, Windows, standard model, one commit per
  session): session1 remainder, Gate 5, 6A, 6B, 6C, 6D-a, 6D-b, fixes.
- Backlog: `build_release.ps1 -Install` exits 1 after a successful install
  (F1: no final `exit 0`, robocopy's 1 leaks); timing flake
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b`;
  project-filtered jobs query; catalog test skips on a system Python;
  Gate 4B Edge retirement deferred.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action (in order)

1. Model-list gate (S5). 2. Gate 6E visual polish (S6). 3. Back up
`releases\1.0.0-4f5a93d` (the next install prunes it), then re-install
(Salih's go). 4. Dogfood + D-3. 5. Rollback proof (D-4). 6. Close.

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
