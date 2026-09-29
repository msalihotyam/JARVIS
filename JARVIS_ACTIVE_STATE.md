# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" + "Salih kararları — V2.0 Gate 6".

Active gate: Projects Gate 6C "Projeye bağla". NOT STARTED.

Status: Gates 1–5, 6A, 6B complete on main; nothing in flight. 6B (§4.25):
top-bar project selector + "+ Yeni sohbet" (replaced the 6A badge), instruction
editor, file panel with counters and typed-name purge. Base commit:
`feat(projects): add instruction editor, file panel and project selector to
the window`. Installed release: `1.0.0-1c68161` (Chrome); previous
`1.0.0-4f5a93d`.

## Roles

- Orchestrator: Claude; reviewer: ChatGPT (one-pass); final decisions: Salih
  (2026-09-29). Protocol: RUNBOOK "Governance and prompt review".

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner fixed
  (no move in V2.0, except the narrow 6C "Projeye bağla": Global → project,
  one-way, preview + confirm; Salih 2026-09-29); scope is context, never
  authority; no `SCHEMA_VERSION` bump while schema-10 releases are rollback
  targets (additive tables only, no ALTER).
- Gates 1–3 (§4.22) incl. archive; no Global-conversation jobs.
- Gate 4 (§4.23): instructions are guidance, never authority; precedence
  safety/Core > current message > instruction > retrieved memory; R1.
- Gate 5 (§4.24): files are untrusted data; retire + explicit purge, purge
  allowed on archived projects.
- Gate 6 UX (Salih 2026-09-29/30): Projects on Ctrl+6, History keeps Global,
  no recents grouping; mouse-first, no shortcut-dependent feature; selector
  lists active projects only; purge confirmed by typing the row's name.

## Uncommitted / external decisions

- Remaining V2.0: 6C (Opus 5.5 xhigh; guard for conversations that used V2.1
  "all projects memory") → Gate 7 (E2E + packaged dogfood + rollback, incl.
  archive/unarchive E2E). Salih decides purge-residue privacy before Gate 7.
  No release before Gate 6 UX is complete.
- V2.1 (Salih 2026-09-29): "all projects memory" toggle (memory only,
  read-only, per-conversation, default off, provenance); PreCompact capture,
  full archive retrieval, context-usage indicator, "Hatırla" button.
- Codex review queue (Salih, Windows, standard model, one commit per session):
  session1 remainder (instruction channel, grant fix, schema-10), Gate 5, 6A,
  6B, fix commits. F1+F2 fixed in `74adbd0`, F3 by design (§4.22), F4 fixed in 6A.
- Backlog: full-suite timing flake
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b`
  (passes alone); project-filtered jobs query (listing is the oldest 20);
  catalog test skips on a system Python (Linguist tools on PATH, not next to
  python.exe); Gate 4B Edge retirement deferred.
- External: installed app's Claude CLI OAuth expired (2026-09-26); re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 6C design (Opus 5.5 xhigh): "Projeye bağla" over the existing
  store paths; no new authority.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active; `1.0.0-4f5a93d`, `legacy-1.0.0-5976769`
  retained. `data\browser-profile` kept; `data\jarvis.pre-v2.db` verified.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target;
  `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
