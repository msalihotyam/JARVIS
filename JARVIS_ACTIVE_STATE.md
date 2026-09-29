# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (first structural V2 layer; dogfood-capable,
not the official V2 daily-driver gate — that is V2.1 Projects + Advanced
Memory). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md` (canonical V2+),
section "V2.0 — Projects".

Active gate: Projects Gate 6B (instruction editor, file panel, purge UI).
NOT STARTED.

Status: Gates 1–5 and 6A complete on main; nothing in flight. Core: §4.22
conversations/lifecycle/archive, §4.23 instructions (R1), §4.24 files (purge
residue there). Gate 6A (§4.25): Projects page (Ctrl+6, window-only),
active + archived lists and lifecycle via the app paths, project detail
(conversations, jobs labelled "first N"), owner badge, read-only archived
view, Memory names, approval card shows instruction + excerpts complete as
inert plain text. Base commit: `feat(projects): add Projects page and
project conversations to the window` on main. Installed release:
`1.0.0-1c68161` (Chrome), active and consistent; previous `1.0.0-4f5a93d`.

## Roles

- Orchestrator: Claude (linked Cowork session, repo access); reviewer:
  ChatGPT (one-pass); final decisions: Salih. Salih decision (2026-09-29);
  trial concluded 2026-09-29. Protocol: RUNBOOK "Governance and prompt review".

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (trusted Chrome, chrome-profile,
  WebRTC switch, per-tab boundary, MediaRouter off, packaged + rollback).
- Projects Gate 0: reuse the `projects` table; Global = `project_id` NULL (no
  synthetic Global project); opaque ids; owner fixed at creation (no move in
  V2.0); project scope is context, never authority; no `SCHEMA_VERSION` bump
  while schema-10 releases are rollback targets (additive tables only, no
  ALTER of schema-10 tables).
- Gates 1–3: conversation store, live-DB test guard, bridge cutover, project
  lifecycle, owner scope, archive semantics (Salih, 2026-09-29; §4.22).
  Accepted: no job from a Global conversation, even with one active project.
- Gate 4 (orchestrator, 2026-09-29; §4.23): instructions are model guidance,
  never authority; precedence safety/Core > current message > instruction >
  retrieved memory; R1: attested outbound content, E = authority-only.
- Gate 5 (§4.24): files are untrusted data; remove = retire + explicit purge
  (Salih, 2026-09-29); purge allowed on archived projects.
- Gate 6 UX (Salih, 2026-09-29): Projects on Ctrl+6 (Settings stays on orb
  / palette), History keeps Global, badge never switches, no recents
  grouping; 6B: instruction/file UI, purge confirmed by typing the name.

## Uncommitted / external decisions

- Remaining V2.0 plan (Gate 0 report): 6B → 7 E2E + packaged dogfood +
  rollback proof.
- The catalog test skips on a system (non-venv) Python: `linguist_tool`
  looks only next to python.exe; the tools are on PATH (Scripts\).
- Gate 4B physical Edge retirement is deferred until a normal-use period and
  an explicit user decision.
- External: the installed app's Claude CLI OAuth session was expired at the
  last packaged smoke (2026-09-26); the user must re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 6B (Opus 5.5 High): instruction editor, file panel and
  purge UI over the existing app paths; no new authority.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active; `1.0.0-4f5a93d`, `legacy-1.0.0-5976769`
  retained. `data\browser-profile` kept; `data\jarvis.pre-v2.db` verified.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target;
  `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
