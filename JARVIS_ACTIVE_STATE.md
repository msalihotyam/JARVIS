# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (first structural V2 layer; dogfood-capable,
not the official V2 daily-driver gate — that is V2.1 Projects + Advanced
Memory).

Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md` (canonical V2+); active
section "V2.0 — Projects".

Active gate: Projects Gate 4 — project instructions. NOT STARTED.

Status: Gates 1–3 are complete on main. Conversations live in the Core
SQLite store (additive schema-10 layer, `ARCHITECTURE.md` §4.22). Gate 3
added project create (opaque `p-` id) / rename / archive / unarchive (Core +
`jarvis project` CLI, no UI, no delete) and made the conversation owner the
only chat project scope (Task.project_id, session binding, project memory,
job project; Global = None, no single-project fallback for conversation
requests). Archive semantics are canonical in §4.22. Nothing is in flight.

Starting/base commit: the `feat(projects): add project lifecycle and scope
propagation` commit on main.

Current installed release: `1.0.0-1c68161` (Chrome runtime), active and
consistent; previous = `1.0.0-4f5a93d`.

## Open blockers

- None.

## Do not reopen

- V2 M1 Edge → Chrome migration: complete (trusted Chrome, chrome-profile,
  WebRTC preference + switch, per-tab boundary bound before exposure,
  MediaRouter disabled, packaged + rollback proven).
- Projects Gate 0 decisions: reuse the existing `projects` table; Global =
  `project_id` NULL (no synthetic Global project); opaque ids; conversation
  owner fixed at creation (no move in V2.0); project scope is context, never
  authority; no `SCHEMA_VERSION` bump while schema-10 releases are rollback
  targets (additive tables only, no ALTER of schema-10 tables).
- Gates 1–3: conversation store, test live-DB isolation (guard in
  `tests/__init__.py`), bridge cutover to the Core store, project lifecycle,
  conversation-owner scope propagation, archive semantics (Salih,
  2026-09-29; §4.22).
- Behaviour change accepted with Gate 3: a job cannot be created from a
  Global conversation even when exactly one project is active.

## Uncommitted / external decisions

- The remaining V2.0 gate plan exists only in the Gate 0 report: 4 project
  instructions → 5 project files foundation → 6 Projects UX → 7 E2E +
  packaged dogfood + rollback proof.
- Gate 3 test run used `uv run` once, which created `.venv` (ignored) and
  rewrote the untracked `uv.lock`; no prior copy existed to compare. Tests
  run with `python -m unittest` (as `scripts/test.ps1`).
- Gate 4B physical Edge retirement is deferred until a normal-use period
  and an explicit user decision.
- External: the installed app's Claude CLI OAuth session was expired at the
  last packaged smoke (2026-09-26); the user must re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 4 (Opus 5.5 High): project instructions / user decisions
  as explicit-user project context. Instructions stay context, never
  authority (§4.22 "Yetki değildir"); imported or external content is never
  a trusted instruction. No Projects UI yet.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active, `1.0.0-4f5a93d` pre-Chrome rollback
  retained, `legacy-1.0.0-5976769` retained.
- `data\browser-profile` (Edge rollback profile) kept unmodified.
- `data\jarvis.pre-v2.db`: verified pre-V2 copy of the live database.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target.
- `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
