@jarvis lane={ID} model=opus effort=high base=main
Read-only lane. Independent Claude review of {SRC} ({TIER}; written by another lane), written by the pre-approved chain (no new authority). LANE RULES: build/conveyor/notes/LANE_RULES.md (CORE; C8 output discipline: test output to a %TEMP% file, read the tail; C10).
Scope: {SRC} {SHA}. Review ONLY the diff {SHA}^..{SHA} (files: {FILES}). Spec: build/conveyor/prompts/{SRC}.md; report: build/reports/{SRC}.md.
CHECKS:
 1 It does what the spec asks and nothing more.
 2 Correctness: edge cases, failure paths, fail-closed behaviour; report real defects only.
 3 The tests assert the behaviour (they fail if it breaks).
 4 Run the focused tests of the touched modules on a clean detached checkout of {SHA}: python scripts/test_parallel.py <modules> (FOREGROUND, Bash timeout=600000, %TEMP% file, read its tail); uvx ruff@0.16.0 check <touched .py> --select E4,E7,E9,F.
OUTPUT: build/reports/{ID}.md. First line STATUS: PASS or STATUS: BLOCKED. Name the reviewed sha on exactly one line "COMMIT: {SHA}".
List findings as BLOCKER/MAJOR/MINOR. PASS = no REAL BLOCKER/MAJOR; else BLOCKED + one fix slice.
Hard rules: READ-ONLY on source; never commit or push; never touch uv.lock; no reset/restore/stash/clean/checkout of main; no live calls; never open data/jarvis.db. GREEN MEANS STOP.
