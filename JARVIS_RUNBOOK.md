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

Sonnet is **no longer the default coder**: in real use it is too slow.

| Model / effort | Use for |
|---|---|
| Fable 5.1, High | Primary choice: normal and complex coding, design + implementation |
| Opus 5, High | Critical blocker, debug, security, concurrency, recovery — and whenever wall-clock speed matters |
| Sonnet 5, High | Only with a clear reason: a small, mechanical task |

`xhigh` / `max` stay exceptional — justified by the task at hand, never a
default.

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

## Dashboard

Update `dev/dashboard/status.json` only after the accepted work is canonical
on main. See `dev/dashboard/UPDATE.md` for the full contract (percentages are
computed by the renderer, never written by hand).

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
