# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" + "Salih kararları — V2.0 Gate 6".

Active gate: Projects Gate 7 (E2E + packaged dogfood + rollback). IN PROGRESS:
phase A (E2E test gaps, tests only) done; phase B (build + install) next.

Status: Gates 1–5 and 6A–6C complete on main; Gate 6 UX complete; nothing in
flight. 6C (§4.22/§4.25): one-way attach of a Global conversation to a project
(`attach_global_conversation_to_project`, sole caller
`conversation_attach_result`; additive `conversation_moves`; V2.1 reserved-table
guard), selector-menu dialog with preview + single confirm. Base commit:
`feat(projects): attach a Global conversation to a project`. Installed
release: `1.0.0-1c68161` (Chrome); previous `1.0.0-4f5a93d`.

## Roles

- Orchestrator: Claude; reviewer: ChatGPT (one-pass); final decisions: Salih
  (2026-09-29). Protocol: RUNBOOK "Governance and prompt review".

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner fixed
  (only exception: 6C "Projeye bağla", Global → project, one-way, once;
  never P→Q or P→Global; Salih 2026-09-29); scope is context, never
  authority; no `SCHEMA_VERSION` bump while schema-10 releases are rollback
  targets (additive tables only, no ALTER).
- Gates 1–3 (§4.22) incl. archive; no Global-conversation jobs.
- Gate 4 (§4.23): instructions are guidance, never authority; precedence
  safety/Core > current message > instruction > retrieved memory; R1.
- Gate 5 (§4.24): files are untrusted data; retire + explicit purge, purge
  allowed on archived projects.
- Gate 6 UX (Salih 2026-09-29/30): Projects on Ctrl+6, History keeps Global,
  no recents grouping; mouse-first, no shortcut-dependent feature; selector
  lists active projects only; purge confirmed by typing the row's name;
  6C: entry only in the selector menu, preview + single "Projeye bağla"
  button; a conversation that ever used "all projects memory" never moves.

## Uncommitted / external decisions

- Remaining V2.0: Gate 7 (E2E + packaged dogfood + rollback, incl.
  archive/unarchive E2E and a packaged rollback over a moved conversation).
  Purge residue (Salih 2026-09-30): V2.0 purge stays as in §4.24 (canonical
  file store purged; approval preview/audit payload, model responses, history,
  provider-sent content, WAL/backups remain, disclosed in the purge dialog).
  Scrubbing those copies is deferred to a later design (touches audit
  append-only integrity), not a V2.0 gate; Gate 7 is not waiting on it.
- Gate 7 phase A done (tests only; full suite 4576 OK, 2 skipped).
- Gate 7 decisions (Salih 2026-09-30): D-1 copy
  `releases\legacy-1.0.0-5976769` outside Apps\JARVIS, then let the install
  prune it (4f5a93d stays the Edge anchor; `data\browser-profile` kept);
  D-2 dogfood on the live DB after a hashed backup (JARVIS closed:
  `data\jarvis.db` + -wal/-shm, `~\.jarvis\*.json`); D-3 one approved live
  Claude call after Salih's own CLI re-login; D-4 real packaged -Rollback to
  1c68161 and back (pending approvals cleared, conversation.json backed up
  and restored).
- V2.1 (Salih 2026-09-29): "all projects memory" toggle (memory only,
  read-only, per-conversation, default off, provenance); PreCompact capture,
  full archive retrieval, context-usage indicator, "Hatırla" button. V2.1
  must own `conversation_global_memory_toggles` (roadmap contract).
- Codex review queue (Salih, Windows, standard model, one commit per session):
  session1 remainder (instruction channel, grant fix, schema-10), Gate 5, 6A,
  6B, 6C, fix commits. F1+F2 fixed in `74adbd0`, F3 by design (§4.22), F4
  fixed in 6A.
- Backlog: full-suite timing flake
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b`
  (passes alone); project-filtered jobs query (listing is the oldest 20);
  catalog test skips on a system Python (Linguist tools on PATH, not next to
  python.exe); Gate 4B Edge retirement deferred.
- External: installed app's Claude CLI OAuth expired (2026-09-26); re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Gate 7 phase B: build + install the release from the phase A commit (needs
  Salih's go), then D-1..D-4 below.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active; `1.0.0-4f5a93d`, `legacy-1.0.0-5976769`
  retained. `data\browser-profile` kept; `data\jarvis.pre-v2.db` verified.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target;
  `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
