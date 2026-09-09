# JARVIS Development Runbook

Canonical operating rules for driving JARVIS development with Claude Code
workers. This file is procedure, not architecture — see `ARCHITECTURE.md` and
`PROJECT_CONTEXT.md` for that, and `dev/dashboard/UPDATE.md` for the dashboard
contract.

**Precedence:** the newest explicit decision from Salih overrides any older
text in this runbook. When that happens, this file must be updated to reflect
it — the runbook must never be left stating a superseded rule.

## One line at a time

Only one main development line is active at a time: one accepted
implementation in flight, one worktree, one thread of review. Do not start a
second line of work before the current one reaches an accepted, committed,
clean state.

## Every worker instruction includes

1. **KİME VERECEKSİN** — which worker/session this goes to.
2. **MODEL / EFFORT** — the model and effort level (see policy below).
3. **SOHBET / WORKTREE** — which chat and which worktree the work happens in.

## Prompt shape

Keep prompts short: current state + delta + boundaries + expected output.
Do not repeat closed architecture or project history in the prompt — the
worker can read `ARCHITECTURE.md`, `PROJECT_CONTEXT.md`, and prior commits
itself. State only what changed and what is now being asked for.

## Model policy

| Model / effort | Use for |
|---|---|
| Sonnet 5, High | Bounded daily implementation |
| Fable 5.1, High | Broad or complex design and coding |
| Opus 5, High | Critical blocker, debug, security, concurrency, recovery |

Use a higher effort level only when justified by the task at hand — not by
default.

## Independent review (Work)

Reserve an independent Work gate for genuinely critical blockers only — not
for routine changes. Most work does not need one.

## Testing discipline — TEST BUDGET

**During implementation:**

- Run only the smallest tests directly related to the code being changed.
- Do not repeatedly rerun an entire targeted/module/helper suite after each
  fix.

**When code + required tests are frozen:**

- Run the relevant targeted suite ONCE.
- If it passes, do not add more tests, audits, hardening checks, refactors,
  mutation probes, or self-review work.
- Commit and STOP.

**Full product suite:**

- Run only when the task explicitly requires it.
- Run it ONCE, at the end.

If a new concrete blocker or uncovered acceptance criterion appears after the
final green run, report it instead of silently expanding the task.

**Canonical stop rule: GREEN MEANS STOP.**

## Acceptance and commit

When acceptance criteria pass: commit and stop. Do not keep iterating past
acceptance, and do not bundle unrelated cleanup into the acceptance commit.

## Push

Never push without explicit authorization from Salih. This applies every
time — prior authorization does not carry forward to the next push.

## Dashboard

Update `dev/dashboard/status.json` only after the accepted work is canonical
on main. See `dev/dashboard/UPDATE.md` for the full contract (percentages are
computed by the renderer, never written by hand).

## Worktree lifecycle — HARD RULE

Before any `jfinish` / finish-current / worktree cleanup, ALL of the
following must be true, in order:

1. The implementation has been accepted.
2. The worktree is clean (`git status --porcelain` empty).
3. The current handoff token has been verified.
4. The persistent Claude development chat has been rebound to
   `C:\Users\realk\Masaüstü\JARVIS`.

**No worktree removal may happen before step 4.** A worktree cleanup that
runs while the persistent chat is still pointed at the worktree being removed
is a boundary violation, regardless of how clean or accepted the work is.

## Before proposing the next action

Work out, in this order:

1. **NEREDEYİZ** — where we are right now.
2. **ŞİMDİ NE YAPIYORUZ** — what we are doing now.
3. **SONRA NE VAR** — what comes after.

State this before proposing what to do next, so the next action is always
anchored to the actual current state rather than to what the plan assumed it
would be.
