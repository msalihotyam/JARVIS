# JARVIS - rules for Codex lanes

You run as a conveyor lane (`codex exec`, `--sandbox workspace-write`) in a git worktree the
conveyor created for you. Read `build/conveyor/notes/LANE_RULES.md` (CORE) in the main checkout
(the folder that contains `.claude\worktrees`) and your prompt; your prompt wins over that file.
The hard rules below never relax. `CLAUDE.md` holds the same rules for Claude Code lanes.

Hard rules:

- Never push. Commit ONCE, only on your worktree branch, never on `main`.
- Never touch, stage or commit `uv.lock`. Never stage `dev/dashboard/*`. Stage only explicit paths.
- No `git reset` / `restore` / `clean` / `stash`: never delete or hide work. Stay in your own worktree.
- No live connector, model, auth or network call; never start or reinstall the packaged app.
- Protected paths: `data/**`, `~/.jarvis/`, `.env*`, key/certificate/database files.
  Tests never open `data/jarvis.db`.
- `SCHEMA_VERSION` stays 10 (additive tables only; no ALTER) while schema-10 releases are a
  rollback target.
- Tests only with `python -m unittest` (via `python scripts/test_parallel.py <modules>`); never `uv run`.
- Never edit `CLAUDE.md`, `AGENTS.md` or anything under `.claude/`. Touch only the files your prompt owns.
- `git diff --check` before the commit. GREEN MEANS STOP: no extra tests, audits or refactors.
- Fail closed: on uncertainty, an unowned file or a higher risk tier, stop and report BLOCKED.

Report: write `build/reports/<ID>.md` in your worktree ONCE, after every check. First line exactly
`STATUS: PASS` or `STATUS: BLOCKED`, then the fields LANE_RULES C7 lists (BRANCH, COMMIT: <40 hex>,
FILES, RUNS, ...). The conveyor trusts only that file, the diff and its own test gate.
