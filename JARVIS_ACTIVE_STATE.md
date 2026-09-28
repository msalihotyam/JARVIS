# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (first structural V2 layer; dogfood-capable,
not the official V2 daily-driver gate — that is V2.1 Projects + Advanced
Memory).

Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md` (canonical V2+); active
section "V2.0 — Projects".

Active gate: Projects Gate 3 — project lifecycle + scope propagation.
NOT STARTED.

Status: Gates 1–2 are complete on main. The window's conversations live in
the Core SQLite store (additive schema-10 layer, `ARCHITECTURE.md` §4.22):
several Global conversations, restored on open, results routed to the
conversation that owns their session; the legacy `conversation.json` is only
read. Nothing is in flight.

Starting/base commit: the `feat(projects): enable persisted conversations`
commit on main.

Current installed release: `1.0.0-1c68161` (Chrome runtime), active and
consistent; previous = `1.0.0-4f5a93d`.

## Open blockers

- None for Gate 2.

## Do not reopen

- V2 M1 Edge → Chrome migration: complete (trusted Chrome, chrome-profile,
  WebRTC preference + switch, per-tab boundary bound before exposure,
  MediaRouter disabled, packaged + rollback proven).
- Projects Gate 0 decisions: reuse the existing `projects` table; Global =
  `project_id` NULL (no synthetic Global project); opaque ids; conversation
  owner fixed at creation (no move in V2.0); project scope is context, never
  authority; no `SCHEMA_VERSION` bump while schema-10 releases are rollback
  targets (additive tables only, no ALTER of schema-10 tables).
- Gates 1–2: conversation store, test live-DB isolation (guard in
  `tests/__init__.py`), bridge cutover to the Core store.

## Uncommitted / external decisions

- The remaining V2.0 gate plan exists only in the Gate 0 report: 3 project
  lifecycle + scope propagation → 4 project instructions → 5 project files
  foundation → 6 Projects UX → 7 E2E + packaged dogfood + rollback proof.
- Gate 4B physical Edge retirement is deferred until a normal-use period
  and an explicit user decision.
- External: the installed app's Claude CLI OAuth session was expired at the
  last packaged smoke (2026-09-26); the user must re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 3 (Opus 5.5 High): create (opaque id) / rename / archive /
  unarchive projects in Core; a conversation's owner set at creation;
  chat-started tasks carry the typed `Task.project_id` of their
  conversation (session binding → project memory retrieval); the intent
  catalog's project comes from the conversation. Prove approval
  fingerprints, grants, destinations, roots and attestation are identical
  with and without a project. No Projects UI yet.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active, `1.0.0-4f5a93d` pre-Chrome rollback
  retained, `legacy-1.0.0-5976769` retained.
- `data\browser-profile` (Edge rollback profile) kept unmodified.
- `data\jarvis.pre-v2.db`: verified pre-V2 copy of the live database.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target.
- `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
