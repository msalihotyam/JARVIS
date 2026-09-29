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

## Orchestration — chief and subagents

The main Claude Code session is the **chief**: it plans, delegates,
integrates and verifies. It is not forbidden from writing code.

- **Delegate** a task when it has its own file/surface ownership, clear
  boundaries and an independent acceptance test.
- **The chief writes** central single-owner surfaces (core, contracts,
  schema, migrations), cross-module glue and integration fixes, and small
  work where delegation costs more than doing it.
- **Sequential by default.** Parallel or worktree fan-out is opt-in and only
  when ownership is genuinely disjoint. A central shared surface has exactly
  one owner at a time.
- **Brief:** objective, owned surface, forbidden scope, invariants,
  acceptance proof — nothing else.
- **Return:** concise, target <= 15 lines. Raw logs stay with the subagent;
  they are not promoted into the chief's context.

## Model policy

Sonnet is **no longer the default coder**: in real use it is too slow.

| Model / effort | Use for |
|---|---|
| Opus 5.5, High | Primary/default: architecture, implementation, debugging, refactors, security, gates |
| Fable 5.1, High | Exceptional secondary lane: an independent/review perspective or specific bounded work |
| Sonnet 5, High | Narrow, mechanical, lower-risk work |

`xhigh` / `max` stay exceptional — only when the concrete task genuinely
requires escalation, never a routine default.

Choose on **wall-clock time + correctness + turn count + Claude Max quota
impact**, not token count alone.

## Claude chat rotation

Persistent Claude chats are **disposable working context**. Canonical
continuity lives in this runbook, `JARVIS_ACTIVE_STATE.md` and Git — never in
a chat's scrollback.

- Let auto-compaction run normally; it is not a reason to rotate.
- Rotate at **milestone boundaries**, when Messages/context is roughly
  400–500k+ or the chat is noticeably slow.
- Do **not** rotate mid-implementation unless the session is unusable.
- Before retiring a chat: the accepted work must be canonical, and the chat
  must be rebound to `C:\Users\realk\Masaüstü\JARVIS`.
- Archive old chats; do not delete them.
- Start the fresh chat with exactly:

  > Read JARVIS_RUNBOOK.md and JARVIS_ACTIVE_STATE.md first. Continue from the
  > current canonical state.

## Session bootstrap

Every new session bootstraps from the repo, not from a handoff chain:

1. Read this runbook, `JARVIS_ACTIVE_STATE.md` and the section of the
   canonical roadmap (`JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`) it names as
   active.
2. Find the last commit that changed the state file:
   `git log -1 --format=%H -- JARVIS_ACTIVE_STATE.md`
3. Inspect what came after it: `git log --oneline <state_commit>..HEAD`
4. Inspect local work: `git status --short`
5. Later commits do not by themselves make the state stale. Read only those
   commits and decide whether they materially change the milestone, gate or
   status the state file describes.
6. Consistent: continue without asking for redundant confirmation.
7. Contradictory, or relevant uncommitted work the state file does not
   represent: stop and report the mismatch.

Read-only git inspection from a remote/linked session sets
`GIT_OPTIONAL_LOCKS=0`, so it never writes the index or leaves
`.git/index.lock`.

## Active state file

`JARVIS_ACTIVE_STATE.md` is a short, overwrite-style snapshot of NOW:
active milestone and gate, status, base commit, installed release, open
blockers, what must not be reopened, uncommitted/external decisions, next
action and rollback/safety anchors.

- Target <= 60–80 lines and <= ~5 KB. Rewrite it; never append.
- Closed gates and resolved blockers are removed. History lives in git and
  the roadmap.
- It carries no "current HEAD" field: its freshness is checked by the
  bootstrap above.
- Update it on canonical main when the milestone, gate or status changes.

## Evidence and handoffs

- Durable security, release or review evidence with long-term value may be
  committed, in the existing docs structure (architecture invariants in
  `ARCHITECTURE.md`, procedure here, the proof summary in the commit
  message). Raw terminal or test dumps are never committed.
- A chat/session handoff carries only what the repo does not yet hold:
  uncommitted decisions, unresolved questions, external state. Target
  <= half a page.

## Independent review (Work)

Reserve an independent Work gate for genuinely critical blockers only — not
for routine changes. Most work does not need one.

Codex is an independent reviewer: it is never the implementation worker of
the same change it reviews.

## Governance and prompt review

Current role holders live in `JARVIS_ACTIVE_STATE.md`, not here.

- **Operational orchestrator:** owns gate planning and orchestration /
  implementation decisions within the approved scope. Marks every design
  choice in a prompt as "orchestrator decision" with its source.
- **Architecture/authority reviewer:** may block a gate only for a concrete
  conflict with canonical architecture, authority or security invariants.
- **Salih decides:** unresolved orchestrator/reviewer disagreements and
  anything genuinely his (product-visible behaviour, security policy, cost,
  hard to reverse). Recorded as "Salih decision (date)".

**Prompt review protocol:**

- Only gate prompts that contain design go to review. Evidence-completion
  and small fix prompts go without review.
- The reviewer gets the prompt AND the orchestrator's evidence pack
  (file:line facts) together.
- One pass with ALL findings, each labelled BLOCKER (canonical conflict,
  security/correctness defect, unsourced assumption the worker would act
  on) or NIT (wording/clarity). Verdict: READY / READY-WITH-NITS / BLOCKED.
- Exactly one revision round, then the prompt ships. Re-review only if a
  fix introduces a new design decision.

**Orchestrator pre-send checklist:**

- Every assumption has a file:line or doc source.
- Every design choice is marked orchestrator or Salih decision.
- Empty / cleared / ineligible states are defined.
- Untrusted text has a tested serialization/escaping boundary.
- "Visible" says to whom (model payload vs local user).
- Verification wording matches what will actually run.

## Testing discipline — TEST BUDGET

Tests run with `python -m unittest discover -s tests` (as
`scripts/test.ps1`), never `uv run`.

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
- Run it ONCE, at the end, on the exact tree that is committed, with no
  edits after that run.

If a new concrete blocker or uncovered acceptance criterion appears after the
final green run, report it instead of silently expanding the task.

**Canonical stop rule: GREEN MEANS STOP.**

## Acceptance and commit

When acceptance criteria pass: commit and stop. Do not keep iterating past
acceptance, and do not bundle unrelated cleanup into the acceptance commit.

## Push

Never push without explicit authorization from Salih. This applies every
time — prior authorization does not carry forward to the next push.

## Release (Windows package)

The packaged app is managed locally by `scripts/JarvisRelease.psm1`; the
entry points are thin:

```
.\scripts\build_release.ps1            # build only (disposable output under %TEMP%)
.\scripts\build_release.ps1 -Install   # build + validate + install + activate
.\scripts\release.ps1 -Status          # what is installed and active (read-only)
.\scripts\release.ps1 -List            # every installed release (read-only)
.\scripts\release.ps1 -Rollback        # current -> the previously active release
.\scripts\release.ps1 -Activate <id>   # current -> that installed release
```

Layout under `%USERPROFILE%\Apps\JARVIS`:

- `releases\<version>-<git short sha>\` — one complete, immutable
  `--onedir` tree per release plus its `release.json` (no secrets, no
  operator data). The imported pre-manager flat install is
  `legacy-<version>-<exe sha7>`.
- `current\` — a directory junction to the active release. Activation,
  rollback and install only re-point it; nothing is copied.
- `release-state.json` — active / previous / history, written atomically.
- The Desktop shortcut `JARVIS.lnk` targets `current\JARVIS.exe` permanently,
  so a new release never changes it.

Rules: the release id comes from the committed tree — `-Install` refuses a
tree with uncommitted release-relevant changes (`uv.lock` and `dev/` are the
known non-release dirtiness and are ignored). JARVIS must be closed for any
change of the active release; the manager refuses otherwise and kills
nothing. Retention keeps the active release, the rollback target and the most
recently active others, 3 in total, and prunes only after a successful
activation. A state/junction disagreement is reported by `-Status` and
blocks install/rollback; it is never repaired by guesswork. Tests:
`powershell -File tests\release-manager-tests.ps1` (isolated temp roots).

Browser runtime and rollback: current releases drive trusted system Google
Chrome on `data\chrome-profile`. A release built before the Chrome migration
drives Microsoft Edge on `data\browser-profile`, so that profile is kept,
unmodified, as long as such a release is a rollback target. Deleting it, or
that release, is a separate explicit decision.

## Dashboard

Update `dev/dashboard/status.json` only after the accepted work is canonical
on main. See `dev/dashboard/UPDATE.md` for the full contract (percentages are
computed by the renderer, never written by hand). Also:

- Keep dashboard summaries short and human-facing.
- Do not change roadmap weights or progress rules unless explicitly told to.
- After a canonical milestone update, commit the resulting tracked
  `status.json` change in a small dev-tooling commit so main is clean again.

## Worktree lifecycle — HARD RULE

Before any `jfinish` / finish-current / worktree cleanup, ALL of the
following must be true, in order:

1. The implementation has been accepted.
2. The worktree is clean (`git status --porcelain` empty).
3. The persistent Claude development chat has been rebound to
   `C:\Users\realk\Masaüstü\JARVIS`.
4. The handoff is confirmed **to the helper**: in an interactive console
   `finish-current` / `cleanup` ask one explicit question and accept only the
   typed answer `REBOUND`; the helper then resolves the exact task-generation
   token itself. No `status` lookup, no token copying and no manual
   `$env:JARVIS_DEV_EXCLUSIVE_HANDOFF` in normal use. Automation (captured
   output, piped or closed stdin) never gets the question — it must set that
   variable to the exact token or it fails closed.

The normal interactive sequence is therefore:

```
# 1. work accepted, worktree clean
# 2. rebind the persistent Claude chat to C:\Users\realk\Masaüstü\JARVIS
.\dev.ps1 finish-current            # answer REBOUND when asked
# 3. from canonical main
.\dev.ps1 cleanup <worker> <task>   # answer REBOUND when asked
```

**No worktree removal may happen before step 3.** A worktree cleanup that
runs while the persistent chat is still pointed at the worktree being removed
is a boundary violation, regardless of how clean or accepted the work is —
and answering `REBOUND` before it is true is exactly that violation, typed.

## Before proposing the next action

Work out, in this order:

1. **NEREDEYİZ** — where we are right now.
2. **ŞİMDİ NE YAPIYORUZ** — what we are doing now.
3. **SONRA NE VAR** — what comes after.

State this before proposing what to do next, so the next action is always
anchored to the actual current state rather than to what the plan assumed it
would be.
