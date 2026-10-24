# JARVIS - rules for Codex lanes

You run as a conveyor lane (`codex exec`, `--sandbox workspace-write`) in a git worktree the
conveyor created for you. Read `build/conveyor/notes/LANE_RULES.md` (CORE) in the main checkout
(the folder that contains `.claude\worktrees`) and your prompt; your prompt wins over that file.
The hard rules below never relax. `CLAUDE.md` holds the same rules for Claude Code lanes.

Hard rules:

- Never push. Never run `git add` or `git commit`: the sandbox keeps you out of `.git`; after a
  `STATUS: PASS` report the conveyor commits the files your slice grants (and nothing else).
- Never touch `uv.lock` or `dev/dashboard/*`. Change only the files your prompt grants.
- No `git reset` / `restore` / `clean` / `stash`: never delete or hide work. Stay in your own worktree.
- No live connector, model, auth or network call; never start or reinstall the packaged app.
- Protected paths: `data/**`, `~/.jarvis/`, `.env*`, key/certificate/database files.
  Tests never open `data/jarvis.db`.
- `SCHEMA_VERSION` stays 10 (additive tables only; no ALTER) while schema-10 releases are a
  rollback target.
- Tests only with `python -m unittest`; never `uv run` (see "Running checks as a Codex lane").
- Never edit `CLAUDE.md`, `AGENTS.md` or anything under `.claude/`. Touch only the files your prompt owns.
- `git diff --check` before the report. GREEN MEANS STOP: no extra tests, audits or refactors.
- Fail closed: on uncertainty, an unowned file or a higher risk tier, stop and report BLOCKED.

Running checks as a Codex lane (the sandbox writes only your worktree; the conveyor points TEMP,
TMP, TMPDIR and the uv/ruff/pip caches at `<worktree>/.codex-tmp`, which is never committed):

- Tests: `python -m unittest <modules>`. NOT `scripts/test_parallel.py` (it locks in the main checkout).
- Lint: `python -m ruff check --select E4,E7,E9,F <files>` if ruff is installed, else
  `uvx ruff@0.16.0 check --select E4,E7,E9,F <files>`.
- Never write outside your worktree. Test data files go under `tests/fixtures/` (`tests/data/` is
  git-ignored by the `data/` rule).

Report: write `build/reports/<ID>.md` in your worktree ONCE, after every check. First line exactly
`STATUS: PASS` or `STATUS: BLOCKED`, then `FILES`, one line
`COMMIT MESSAGE: <type>(<scope>): <ID> <what+why>`, then the other fields LANE_RULES C7 lists (RUNS, ...)
but no `COMMIT:` line (the conveyor writes the real sha), and stop. A changed file outside the grant
leaves the lane BLOCKED. The conveyor trusts only that file, the diff and its own test gate.
