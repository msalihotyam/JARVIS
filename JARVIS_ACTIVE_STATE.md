# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (first structural V2 layer; dogfood-capable,
not the official V2 daily-driver gate — that is V2.1 Projects + Advanced
Memory).

Active gate: Projects Gate 2 — conversation bridge cutover. NOT STARTED.

Status: Gate 1 is complete on main — Core SQLite conversation store as an
additive schema-10 compatibility layer (`ARCHITECTURE.md` §4.22), plus test
isolation so no test can open the live database. Nothing is in flight.

Starting/base commit: `f16b181` test: keep every test off the live database
(on `f81188c` feat(projects): add conversation persistence foundation).

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
- Gate 1 test live-DB isolation (guard in `tests/__init__.py`).

## Uncommitted / external decisions

- The V2 roadmap (V2.0–V2.6, including the V2.4B Orchestrated Tasks item)
  is not yet in the repo; its canonical placement is still to be decided.
- The remaining V2.0 gate plan exists only in the Gate 0 report: 2 bridge
  cutover → 3 project lifecycle + scope propagation → 4 project
  instructions → 5 project files foundation → 6 Projects UX → 7 E2E +
  packaged dogfood + rollback proof.
- Gate 4B physical Edge retirement is deferred until a normal-use period
  and an explicit user decision.
- External: the installed app's Claude CLI OAuth session was expired at the
  last packaged smoke (2026-09-26); the user must re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 2 (Opus 5.5 High): the UI bridge switches from
  `~/.jarvis/conversation.json` to the Core store — legacy import by digest
  at startup, conversation list, feed restored from stored turns, new
  conversation, session → conversation link, results routed to their own
  conversation, titles per conversation, research replies marked as
  web-derived. Global only; no Projects UI yet.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active, `1.0.0-4f5a93d` pre-Chrome rollback
  retained, `legacy-1.0.0-5976769` retained.
- `data\browser-profile` (Edge rollback profile) kept unmodified.
- `data\jarvis.pre-v2.db`: verified pre-V2 copy of the live database.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target.
- `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
