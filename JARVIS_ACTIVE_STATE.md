# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (first structural V2 layer; dogfood-capable,
not the official V2 daily-driver gate — that is V2.1 Projects + Advanced
Memory).

Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md` (canonical V2+); active
section "V2.0 — Projects".

Active gate: Projects Gate 5 — project files foundation. NOT STARTED.

Status: Gates 1–4 are complete on main. Conversations live in the Core
SQLite store (§4.22). Gate 3: project create / rename / archive / unarchive,
conversation owner = the only chat project scope, archive semantics. Gate 4
(§4.23): one explicit-user instruction per project (INSERT-only + supersede
in the new `project_instructions` compatibility table, 2,000 chars,
PUBLIC/INTERNAL only, `jarvis project instruction set|show|history`),
delivered always-on under its own payload key and notice to the Claude and
Codex intent adapters and to claude_code task calls; fixed precedence
clause; attested as outbound content (decision R1). Nothing is in flight.

Starting/base commit: the `feat(projects): add project instructions` commit
on main.

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
- Gate 4 decisions (orchestrator, 2026-09-29; §4.23): instructions are
  model guidance, never authority; precedence safety/Core > current message
  > instruction > retrieved memory; R1 - the instruction is attested
  outbound content, E compares authority-bearing decisions only.

## Uncommitted / external decisions

- The remaining V2.0 gate plan exists only in the Gate 0 report: 5 project
  files foundation → 6 Projects UX → 7 E2E + packaged dogfood + rollback
  proof.
- Observed in Gate 4 tests, not investigated: `AgentCore.resume` with a
  `remember_for_session` grant on a non-rememberable claude_code request
  returns EXECUTION_IN_PROGRESS and leaves the task unexecutable.
- Gate 3 ran `uv run` once: created `.venv` (ignored), rewrote untracked
  `uv.lock` (no prior copy). Tests: `python -m unittest discover -s tests`.
- Gate 4B physical Edge retirement is deferred until a normal-use period
  and an explicit user decision.
- External: the installed app's Claude CLI OAuth session was expired at the
  last packaged smoke (2026-09-26); the user must re-login.
- Push: not authorized; main is ahead of `origin/main` (`4f5a93d`).

## Next action

- Projects Gate 5 (Opus 5.5 High): project files foundation. Roadmap V2.0:
  selective retrieval + provenance, never bulk prompt stuffing; a project
  file is data, never a trusted instruction, and cannot widen Core
  authority. No Projects UI yet.

## Rollback / safety anchors

- Releases: `1.0.0-1c68161` active, `1.0.0-4f5a93d` pre-Chrome rollback
  retained, `legacy-1.0.0-5976769` retained.
- `data\browser-profile` (Edge rollback profile) kept unmodified.
- `data\jarvis.pre-v2.db`: verified pre-V2 copy of the live database.
- `SCHEMA_VERSION` stays 10 while any schema-10 release is a rollback target.
- `~/.jarvis/conversation.json` is only ever read by V2.
- Never touch `uv.lock`; never stage the dashboard WIP
  (`dev/dashboard/UPDATE.md`, `render_dashboard.py`, `status.json`).
