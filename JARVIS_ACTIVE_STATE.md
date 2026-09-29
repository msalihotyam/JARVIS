# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (first structural V2 layer; dogfood-capable,
not the official V2 daily-driver gate — that is V2.1 Projects + Advanced
Memory). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md` (canonical V2+),
section "V2.0 — Projects".

Active gate: Projects Gate 6 — Projects UX. NOT STARTED.

Status: Gates 1–5 complete on main; nothing in flight. Conversations in the
Core store (§4.22); Gate 3 lifecycle + owner scope + archive; Gate 4 project
instructions (§4.23, attested, R1). Gate 5 (§4.24): explicit-user text files
(`jarvis project file add|list|retire|restore|purge`, new `project_files` +
`project_file_chunks` tables, 20 active / 40 retained / 2M chars, 200k chars
per file, purge = tombstone + chunk delete in one transaction), selected per
Core task (<= 3 excerpts / 4,000 chars) and sent as one re-proved, attested,
previewed block; never on the intent path. Purge residue: §4.24.
Base commit: `feat(projects): add project files foundation` on main. Installed release: `1.0.0-1c68161` (Chrome runtime),
active and consistent; previous = `1.0.0-4f5a93d`.

## Roles

- Operational orchestrator: Claude (linked Cowork session with repo access).
  Architecture/authority reviewer: ChatGPT (one-pass protocol). Final
  decisions: Salih. Salih decision (2026-09-29); orchestrator trial concluded
  2026-09-29. Protocol: `JARVIS_RUNBOOK.md` → "Governance and prompt review".

## Open blockers: none.

## Do not reopen

- V2 M1 Edge → Chrome migration complete (trusted Chrome, chrome-profile,
  WebRTC preference + switch, per-tab boundary bound before exposure,
  MediaRouter disabled, packaged + rollback proven).
- Projects Gate 0: reuse the `projects` table; Global = `project_id` NULL (no
  synthetic Global project); opaque ids; owner fixed at creation (no move in
  V2.0); project scope is context, never authority; no `SCHEMA_VERSION` bump
  while schema-10 releases are rollback targets (additive tables only, no
  ALTER of schema-10 tables).
- Gates 1–3: conversation store, test live-DB isolation (`tests/__init__.py`
  guard), bridge cutover to the Core store, project lifecycle, owner scope
  propagation, archive semantics (Salih, 2026-09-29; §4.22). Accepted
  behaviour change: no job from a Global conversation, even with exactly one
  active project.
- Gate 4 (orchestrator, 2026-09-29; §4.23): instructions are model guidance,
  never authority; precedence safety/Core > current message > instruction >
  retrieved memory; R1: attested outbound content, E = authority-only.
- Gate 5 (§4.24): files are untrusted data; remove = retire + explicit purge
  (Salih, 2026-09-29); purge allowed on archived projects.

## Uncommitted / external decisions

- The remaining V2.0 gate plan exists only in the Gate 0 report: 6 Projects
  UX → 7 E2E + packaged dogfood + rollback proof.
- Gate 4B physical Edge retirement is deferred until a normal-use period and
  an explicit user decision.
- External: the installed app's Claude CLI OAuth session was expired at the
  last packaged smoke (2026-09-26); the user must re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 6 (Opus 5.5 High): Projects UX over the existing Core/app
  APIs (projects, conversations, instructions, files); no new authority.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active, `1.0.0-4f5a93d` pre-Chrome rollback and
  `legacy-1.0.0-5976769` retained. `data\browser-profile` (Edge rollback
  profile) kept unmodified; `data\jarvis.pre-v2.db` is the verified pre-V2
  copy of the live database.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target;
  `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
