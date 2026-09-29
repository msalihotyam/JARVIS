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

Status: Gates 1–5 and 6A complete on main; nothing in flight. §4.22
conversations/lifecycle/archive, §4.23 instructions (R1), §4.24 files (purge
residue), §4.25 window: Projects page (Ctrl+6, window-only), lifecycle via
app paths, detail + jobs ("first N"), owner badge, read-only archived view,
approval card shows instruction + excerpts complete as plain text. F1/F2
(Codex review): Core binds a conversation-linked session only to its
owner; the app refuses an archived conversation. Base commit: `fix(core):
bind conversation-linked sessions only to their conversation's owner`.
Installed release: `1.0.0-1c68161` (Chrome); previous `1.0.0-4f5a93d`.

## Roles

- Orchestrator: Claude (linked Cowork session, repo access); reviewer:
  ChatGPT (one-pass); final decisions: Salih. Salih decision (2026-09-29);
  trial concluded 2026-09-29. Protocol: RUNBOOK "Governance and prompt review".

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (trusted Chrome, chrome-profile,
  WebRTC switch, per-tab boundary, MediaRouter off, packaged + rollback).
- Projects Gate 0: reuse `projects`; Global = `project_id` NULL (no synthetic
  project); opaque ids; owner fixed (no move in V2.0); scope is context, never
  authority; no `SCHEMA_VERSION` bump while schema-10 releases are rollback
  targets (additive tables only, no ALTER).
- Gates 1–3 (§4.22): conversation store, live-DB test guard, bridge cutover,
  lifecycle, owner scope, archive (Salih 2026-09-29); no Global-conversation jobs.
- Gate 4 (orchestrator, 2026-09-29; §4.23): instructions are model guidance,
  never authority; precedence safety/Core > current message > instruction >
  retrieved memory; R1: attested outbound content, E = authority-only.
- Gate 5 (§4.24): files are untrusted data; remove = retire + explicit purge
  (Salih, 2026-09-29), allowed on archived projects.
- Gate 6 UX (Salih, 2026-09-29): Projects on Ctrl+6 (Settings stays on orb
  / palette), History keeps Global, badge never switches, no recents
  grouping; 6B: instruction/file UI, purge confirmed by typing the name.

## Uncommitted / external decisions

- Remaining V2.0 plan (Gate 0 report): 6B → 7 E2E + dogfood + rollback.
- Independent Codex review of Gates 3–5 is PARTIAL; the remaining reviews
  are queued for after the ChatGPT quota reset.
- Backlog: known full-suite timing flake
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b`
  (animation frame timing under load; passes alone) - fix later.
- Backlog: a project-filtered jobs query (today's listing is the oldest 20).
- Catalog test skips on a system Python (`linguist_tool` looks only next to
  python.exe; tools are on PATH). Gate 4B Edge retirement deferred.
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
