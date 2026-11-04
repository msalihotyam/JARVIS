@jarvis lane={ID} model=sonnet effort=medium base={MAIN}
LANE RULES: read build/conveyor/notes/LANE_RULES.md (CORE + INT; C8 output discipline). Integration of {SRC} ({TIER}), written by the pre-approved chain (no new authority).
CANDIDATE FLOW (CONVINT; do NOT move local main yourself):
0. PREFLIGHT (BLOCKED if any fails): local main == origin/main == {MAIN}; build/reports/{SRC}.md names COMMIT {SHA}.
1. In your worktree: git switch -c cand/{BRANCH} {MAIN}; git cherry-pick {SHA} (one commit, never merge).
   Expected files only: {FILES}. Anything else or a conflict: STATUS BLOCKED naming it.
2. Checks: git diff --check {MAIN}..HEAD; python scripts/test_parallel.py <the test modules of the touched modules> (FOREGROUND, Bash timeout=600000, output to a %TEMP% file, read its tail: exit 0, report planned units). NO local full suite. No live calls.
3. git push origin HEAD:refs/heads/ci/{BRANCH} (never --force, never main).
Report build/reports/{ID}.md: STATUS: CI_PENDING (or BLOCKED); exactly ONE "CANDIDATE SHA: <40 hex>" line and NO "NEW MAIN SHA" line;
CI BRANCH: ci/{BRANCH}; a PICKS block + FILES line + test counts. Name NO other CI branch. Do NOT restart the conveyor.
Hard rules: never push main; never touch uv.lock; explicit paths; no reset/restore/stash/clean; tests never open data/jarvis.db. GREEN MEANS STOP.
