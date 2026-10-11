# JARVIS Development Runbook

Canonical operating rules for driving JARVIS development with Claude Code
workers. This file is procedure, not architecture — see `ARCHITECTURE.md` and
`PROJECT_CONTEXT.md` for that, and `dev/dashboard/UPDATE.md` for the dashboard
contract.

**Precedence:** the newest explicit decision from Salih overrides any older
text in this runbook. When that happens, this file must be updated to reflect
it — the runbook must never be left stating a superseded rule.

## Parallel lanes

Salih decision (2026-10-06; replaces "One line at a time"). Lane procedure
changed 2026-10-07 (S0d): native worktrees, no chat rebinding.

- At most **3 lanes** run at once. Each code lane works in its own **native
  worktree** (below); at most one docs/state lane works on canonical main.
- Every prompt lists the lane's **owned files**. A file belongs to one lane at
  a time. Central single-owner surfaces (core, contracts, memory/store,
  schema/migrations, authority/approval/attestation) are owned by one lane at
  a time.
- **Chats stay bound to the canonical folder
  `C:\Users\realk\Masaüstü\JARVIS` and are never rebound.** Salih drives
  everything by messaging chats (works from a phone); no folder rebinding is
  ever required.
- **Opening a code lane:** the lane's chat starts with `EnterWorktree`
  `name=<slice-id>`. That creates `.claude/worktrees/<slice-id>` on branch
  `worktree-<slice-id>` from local `HEAD` (`.claude/settings.json`:
  `worktree.baseRef = "head"`; the default would branch from `origin/HEAD`,
  and local main is far ahead of it). All edits, tests and commits happen
  there.
- **Isolation:** while inside the worktree, never run git against the
  canonical checkout.
- **Ending a lane:** commit in the worktree, report, then `ExitWorktree`
  choosing **keep**. Never remove a worktree before it is integrated.
- **Integrating** happens only when Salih says "entegre et", one lane at a
  time, in the orchestrator's order, by a chat in canonical on a clean main:
  1. If `main` is still the branch's base: `git merge --ff-only
     worktree-<slice-id>`.
  2. Otherwise: `git cherry-pick main..worktree-<slice-id>`.
  3. On a conflict: abort and report. Never resolve silently.
  4. Then verify the tree, run the suite the tier requires, and run
     `scripts\ci.ps1`.
- **Cleanup after a successful integration:**
  - `git worktree remove .claude/worktrees/<slice-id>` (no `--force`).
  - ff-merged: `git branch -d worktree-<slice-id>`.
  - cherry-picked: `git cherry main worktree-<slice-id>` must show only `-`
    lines; then `git branch -D worktree-<slice-id>`.
  - If anything refuses, STOP and report.
- **Clean main first:** a docs/state lane on main commits its work before any
  lane is opened or integrated (and `dev.ps1 new` / `finish` / `cleanup`
  require a clean canonical main).
- **Fallback:** `dev.ps1 new` / `finish` / `cleanup` (sibling helper
  worktrees) stays available. The REBOUND attestation and the "Worktree
  lifecycle — HARD RULE" below apply only to those helper worktrees.
- **Base check (2026-10-07, F-ee lane):** code lanes start ONLY from a chat
  launched in canonical `C:\Users\realk\Masaüstü\JARVIS`. A chat launched
  from a `dev.ps1` helper folder (e.g. `JARVIS-Claude-6g-k1-icons`) does not
  load canonical's `.claude/settings.json`, so `EnterWorktree` falls back to
  `baseRef "fresh"` (origin/main, about 64 commits behind then); even with
  `"head"` it would resolve to that helper worktree's HEAD, not main.
- **First check of every lane prompt:** `git log -1` inside the new worktree
  must equal the expected base SHA. On a mismatch, STOP.
- **Recovery without a reset:** re-enter the kept worktree, then `git merge
  --ff-only <expected base>`. This works only when the wrong base is an
  ancestor of the expected one.
- **Gate close:** one full suite on the integrated main HEAD (CI on the same
  SHA allowed) covers the combined result.
- Chats: one chat per lane, and at most one chat on canonical main outside a
  worktree at a time (see "Claude chat rotation").

## Every worker instruction includes

1. **KİME VERECEKSİN** — which worker/session this goes to.
2. **MODEL / EFFORT** — the model and effort level (see policy below).
3. **SOHBET / WORKTREE** — which chat and which worktree the work happens in.

**Report files (Salih 2026-10-08):** every Claude Code lane writes its final
report, in full and exactly as reported, to `build/reports/<slice-id>.md`
(`build/` is gitignored; create the folder if missing). It still prints the
report in the chat. Salih may then just tell the orchestrator "<slice-id>
bitti"; the orchestrator reads the file from the repo instead of needing a
paste.

## Prompt shape

Keep prompts short: current state + delta + boundaries + expected output.
Do not repeat closed architecture or project history in the prompt — the
worker can read `ARCHITECTURE.md`, `PROJECT_CONTEXT.md`, and prior commits
itself. State only what changed and what is now being asked for.

## Prompt template (lanes)

Salih decision (2026-10-09): short prompts for 5.5-generation models. Stable
rules live in `CLAUDE.md` and are enforced by hooks / deny rules, so a prompt
does not repeat them (repo safety, protected paths, no push, `uv.lock`, tests
never open the live DB, `SCHEMA_VERSION`, fail-closed, rule D, GREEN MEANS
STOP, `python -m unittest` only). A prompt states only what is new.

- **Length and tone:** 15-30 lines, calm language, no capitals-for-emphasis.
  Give the reason for every rule that is specific to this prompt; a rule with
  a reason is followed more reliably than a bare command.
- **Line 1 is the header** (the mod parses it strictly, see "Mods"):
  `@jarvis lane=<slice-id> model=<opus|sonnet|haiku> effort=<medium|high|xhigh>
  base=<sha|main>`
- **Then four blocks, in this order:**
  - **Goal:** the one outcome wanted, in a sentence or two.
  - **Why:** the decision or need behind it, with its source (Salih decision
    date, file:line). This is what lets the worker judge edge cases.
  - **Scope:** the owned files, the risk tier (T0 / T1 / T2), what is out of
    bounds, and any first check (e.g. the base SHA).
  - **Done when:** the acceptance criteria, the required run (or "no tests"),
    the commit subject, and where the report goes (`build/reports/<slice-id>.md`).
- Do not repeat what `CLAUDE.md` or this runbook already say; link the section.

Example:

```
@jarvis lane=docs-x1 model=sonnet effort=medium base=main
Goal: add the missing "Cloud sessions" cross-link to the Push section.
Why: Salih approved cloud pushes of claude/* branches on 2026-10-08; the Push
  section does not mention them, so a reader of Push alone would think the
  rule is stricter than it is.
Scope: JARVIS_RUNBOOK.md only (T0, docs). First check: git log -1 equals base.
  Nothing under jarvis/ or tests/.
Done when: git diff --check is clean; no tests (docs only); commit
  docs(runbook): link Push to Cloud sessions; report in build/reports/docs-x1.md.
```

## Orchestration — chief and subagents

The main Claude Code session is the **chief**: it plans, delegates,
integrates and verifies. It is not forbidden from writing code.

- **Delegate** a task when it has its own file/surface ownership, clear
  boundaries and an independent acceptance test.
- **The chief writes** central single-owner surfaces (core, contracts,
  schema, migrations), cross-module glue and integration fixes, and small
  work where delegation costs more than doing it.
- **Parallel lanes** follow "Parallel lanes" (Salih 2026-10-06): at most 3,
  each with its owned files; a central shared surface has exactly one owner
  at a time.
- **Brief:** objective, owned surface, forbidden scope, invariants,
  acceptance proof — nothing else.
- **Return:** concise, target <= 15 lines. Raw logs stay with the subagent;
  they are not promoted into the chief's context.
- **Scout:** the chief may delegate mechanical lookups to `jarvis-scout` to
  save context; it re-checks any scout fact it acts on.

## Model policy

Salih decisions (2026-09-30, 2026-10-02):

| Model / effort | Use for |
|---|---|
| Sonnet 5.5, medium | Docs, test reruns |
| Sonnet 5.5, high | Well-scoped implementation |
| Opus 5.5, high | Design, preflight, diagnosis; DEFAULT for gates, including security-sensitive store/bridge/UI work backed by strong acceptance tests |
| Opus 5.5, xhigh | Changes to Core authority / approval / attestation code, or after a high run got stuck once on the same problem |
| Haiku 5.5 (jarvis-scout subagent only) | Mechanical read-only lookups: locate, list, count, extract from saved logs. Facts with path:line; the caller verifies anything it acts on |

- Effort floor stays medium (no low), never above xhigh.
- Opus stuck twice on the same problem: the orchestrator PROPOSES Fable 5.1.
  Fable 5.1 still runs only with Salih's explicit approval.
- Purely mechanical lookups go to `jarvis-scout` (Haiku 5.5); read-only work
  that needs judgment (diagnosis, "why", review) stays on Sonnet 5.5 or Opus;
  code-writing and decision work stays on Opus; Haiku never writes code, never
  reviews, and never decides.
- Every prompt carries `MODEL:` / `EFFORT:` lines.
- Orchestrator chat default effort is high. When a turn needs more (6C design,
  V2.1 memory design, verifying Codex findings, unclear root cause), the
  orchestrator ends the prior message with "Sonraki tur için effort: xhigh".
- ChatGPT "Astra" = ChatGPT's Fable-equivalent tier; quota-hungry, use
  sparingly.

Choose on **wall-clock time + correctness + turn count + Claude Max quota
impact**, not token count alone.

## Claude chat rotation

Persistent Claude chats are **disposable working context**. Canonical
continuity lives in this runbook, `JARVIS_ACTIVE_STATE.md` and Git — never in
a chat's scrollback.

- Salih decision (2026-09-30): the orchestrator proactively proposes rotation
  with a short handoff. Claude Code chats past ~400–500k are retired, not
  compacted (supersedes "let auto-compaction run normally" for them).
- Rotate at **milestone boundaries**, when Messages/context is roughly
  400–500k+ or the chat is noticeably slow.
- Do **not** rotate mid-implementation unless the session is unusable.
- Salih decision (2026-09-30, adapted 2026-10-06): each prompt goes to the
  chat matching its `MODEL:` line, so the model is never switched inside a
  chat (a switch forces a full re-read of the session). One chat per lane; at
  most one chat on canonical main at a time. Rotation rules above are
  unchanged, per chat.
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

**Tiers (Salih 2026-10-02) - what the frozen tree must run, once:**

| Change | Required run |
|---|---|
| Docs / state only | No tests |
| Tests only | The touched test modules |
| Window layer: QML, bridge, pure helper functions (no core / contracts / memory / app / schema / authority code) | Targeted suites + the whole `test_ui` module |
| Core / contracts / memory / store / app / schema / authority / security code | Full suite |
| Before any release build / install | One full suite on that exact HEAD (covers every window-layer commit since the last full run) |

**How to run (Salih 2026-10-02):**

- Serial, the REFERENCE when a result is disputed: `scripts\test.ps1`
  (`python -m unittest discover -s tests`).
- Parallel, same tests in several processes (S0b, 7bb272b): `scripts\test.ps1
  -Parallel [-Workers N] [-UiWorkers K]` (or `python scripts\test_parallel.py
  [modules]`). Discovery runs first in its own process. A module whose
  recorded duration is large is split at TestCase level into
  duration-balanced chunks. UI units (anything importing PySide6 /
  `jarvis.ui`) run over K processes (default `min(4, cpu_count // 3)`, so
  K=4 at 12 CPUs), the rest over N workers (default `min(cpu_count - 1, 6)`).
  Every process has its own TEMP and QApplication.
- Completeness check: exit 0 only when every unit passed AND the dispatched
  test ids equal the serial `discover -s tests` id set exactly AND every
  process ran exactly what it was given (its `Ran` count and every id in its
  `-v` output). One combined summary.
- Durations: `scripts/test_durations.json` (per test class) drives the splits
  and the CI shards; regenerate it now and then with `--write-durations
  scripts/test_durations.json`. Unrecorded units still run (least-loaded
  slot).
- Measured (S0b, 4746 tests, same set as the serial discover): local full
  suite 1398 s -> 445 s wall; `test_ui` ~530 s -> 176 s.
- Slowest tests: add `-Durations N` (either mode; serial prints them natively).

**CI (Salih 2026-10-02; S0b 2026-10-06):** a required FULL suite may be
satisfied by a green GitHub Actions run (`scripts\ci.ps1`) on the EXACT
commit (same SHA), reported with the run URL. The workflow runs 4
duration-balanced shards (`--shard I/4`) plus one aggregate job that checks
the count and the exact id set against a fresh discovery and gives the one
result. Measured: 56.7 min -> 11.3 min wall, 35.7 job-minutes (run
37452034169). The local serial runner stays the reference when results
disagree.

**Load-sensitive rerun rule D (Salih 2026-10-02; replaces the idle-machine
requirement):** the machine is not idle. If, in a required run, the ONLY
failures are tests on the load-sensitive list in `JARVIS_ACTIVE_STATE.md`
(only the orchestrator adds to it), each is rerun ALONE once; if all pass the
run counts as green and both results are recorded. Any other failure blocks.
The parallel runner applies this itself only to listed `test_ui` ids
(`scripts/test_parallel.py` `load_sensitive`); any other listed test that
fails is rerun alone by hand.

**Rule D addendum (Salih 2026-10-08):** if a required full suite fails only on
tests NOT on the load-sensitive list, and each of them passes when rerun alone,
rerun the FULL suite once on a quiet machine (the suite queue free, no own
background runs). Green: the run counts as green; record both runs. Any
failure again: STOP.

**Full product suite:**

- Run only when the tier above requires it.
- Run it ONCE, at the end, on the exact tree that is committed, with no
  edits after that run.

If a new concrete blocker or uncovered acceptance criterion appears after the
final green run, report it instead of silently expanding the task.

**Canonical stop rule: GREEN MEANS STOP.**

## Verification tiers (risk)

Salih decision (2026-10-06): a second axis next to the TEST BUDGET tiers
(which say WHAT to run, above). The prompt names the risk tier; when unsure,
take the higher one.

| Tier | Change |
|---|---|
| T0 | Text, layout, style; no logic |
| T1 | Window-layer behaviour (QML, bridge, pure helpers); no authority code |
| T2 | Core / contracts / memory / store / schema / authority / security / approval / attestation, connectors, network, release, `dev.ps1` |

- **T0 / T1: the worker self-verifies** - the test tier the TEST BUDGET
  requires, screenshots for visible changes (`scripts/ui_visual.py`), and one
  independent review subagent with fresh context that reads the diff and the
  acceptance criteria and returns PASS or findings - then commits without
  waiting. The orchestrator checks T0 in batch at gate end and T1 briefly
  before integration.
- **Docs / state-only slices** need no review subagent (G0, 2026-10-06).
- **T1 QML changes:** the review subagent also runs
  `qt-development-skills:qt-qml-review` (read-only) on the changed QML; its
  findings are filtered by this runbook and the Theme conventions.
- **T2: the full flow** ("Standard gate order & exits"), unchanged.
- A slice that discovers it must touch T2 code stops and reports (fail
  closed).

## Slice queue and unattended runs

Salih decision (2026-10-06):

- The queue lives in `build\queue\` (gitignored): `NN-<slug>.md`, each a
  complete slice prompt written by the orchestrator and approved by Salih. One
  queue runs in one lane, in order, via the `jarvis-queue` skill.
- **Unattended** means: no questions; no waiting for approval to test or
  commit; commits only on the lane's worktree branch (never on canonical
  main); never push `main` (the `ci/*` exception is unchanged); no `finish`,
  cleanup or worktree removal.
- After each slice the worker writes `NN-<slug>.report.md`. On BLOCKED or a
  red result it writes the report and stops the whole queue.

## Cloud sessions

Salih decision (2026-10-06): docs slices, and core/logic slices whose required
tier runs on Linux, may run in Claude Code cloud sessions while the PC is off.
Slices that need Windows UI evidence may not.

Pushing (Salih decision, 2026-10-08, "bulut: evet"):

- A Claude Code cloud session may push ONLY its own `claude/*` (or `ci/*`)
  branch to GitHub. Pushing `main`, or any branch that is not its own, is
  forbidden. `main` is pushed only with Salih's explicit per-time
  authorization, as before.
- Integration of a cloud branch into `main` follows "Parallel lanes": only on
  Salih's "entegre et"; done from a chat launched in canonical; fetch the
  branch, then ff-merge or cherry-pick; on a conflict, abort and report; then
  the tier's verification and `scripts\ci.ps1`.
- Cloud slices keep the existing scope: docs, and core/logic slices whose
  required tier runs on Linux. Never slices that need Windows UI evidence.
- A cloud prompt states its expected base SHA, and the session checks it
  first. On a mismatch: STOP.

## Mods (jarvis-flow)

Claude Code mod (function-hook plugin, Claude Code >= 2.1.287), wave 1, Salih
2026-10-08. It lives in the repo at `.claude/skills/jarvis-flow/` (a project
skills-dir plugin: Claude Code loads it for any session whose project folder
holds it) and is active only when `JARVIS_RUNBOOK.md` exists at the canonical
root; code lanes inside `.claude/worktrees/<x>` still read and write the
canonical `build/` folders. Wave 1 makes no network calls and approves no tool
permission (the pager and `jarvis-codex` below are the only network uses).
Runtime folders `build/queue/`, `build/lanes/` and `build/reports/` are
gitignored and never committed.

- **Prompt header.** The orchestrator writes this as the FIRST line of every
  prompt: `@jarvis lane=<slice-id> model=<opus|sonnet|haiku>
  effort=<medium|high|xhigh> base=<sha|main>`. It is parsed strictly. A prompt
  without the header is normal chat and passes untouched; a prompt that starts
  with `@jarvis` but is malformed is blocked with the reason.
- **Lane band** above the prompt: the chat identity (OPUS, SONNET, HAIKU, one
  colour each), the lane id of the last header, `taban ✓` or `taban ✗ <head>`
  (red; the header's base compared with this session's HEAD), the worktree and
  branch, context %, plan %, a battery mark on battery, `MOLA` while paused, and
  `Yeni istem hazır: <lane> · /basla` when a prompt is waiting. It shrinks by
  priority in a narrow window.
- **Status file** `build/lanes/<family>.json`, rewritten on turn start, turn
  completion and permission waits, and only when something changed: family,
  model, effort, lane, base, cwd, worktree, branch, head, state
  (`working|idle|waiting|low_battery`), busy, updated_at, context_pct, power
  `{ac,pct}`, paused, last_auto. The effort is the one the last request used, so
  it is known only after the first turn of a session.
- **Report capture.** When a turn ends and its final text has a line starting
  `STATUS:`, the text is written to `build/reports/<lane>.md`. An existing,
  different file is first kept as `<lane>.<yyyymmdd-hhmmss>.md`.
- **Prompt lint** (on submit, never for the mod's own submissions): a header
  model family that differs from this chat's blocks with "Bu istem <X> için, bu
  sohbet <Y>. Doğru sohbete yapıştır."; a differing effort blocks with "Önce
  /effort <x> yaz"; a sha base that is not canonical main's HEAD blocks with
  "İstem tabanı <x>, main <y>. Orkestratörden güncel istemi iste." (not checked
  for `base=main`). A bare `/compact` is blocked with "Boş /compact yok.
  /jcompact kullan". The lint is fail-open: if it throws, the prompt passes and
  a toast shows the error.
- **/jcompact** fills the prompt box with `/compact <template>` built from
  `.claude/skills/jarvis-flow/compact-template.md` (hard rules plus the lane
  state). It does not submit; Salih presses Enter.
- **Inbox** `build/queue/<opus|sonnet>/`, scanned every 5 s. Accepted files:
  `*.md`, real path directly inside the folder (no traversal or link out), under
  64 KB, starting with a valid `@jarvis` header whose model is this chat's
  family and which passes the same lint as a typed prompt. `/kuyruk` lists
  pending and refused files, `/basla` shows the oldest file's first 8 lines and a
  short sha256, submits its full text as the next prompt and moves it to
  `done/` with a timestamp (typing `/basla` IS the approval), `/atla <file>`
  moves one to `done/` unsent. The command is `/basla`, not `/başla`: Claude
  Code command names are ASCII only.
- **Auto queue** `build/queue/<family>/auto/` (delegated to the orchestrator,
  2026-10-08): the same acceptance rules, submitted automatically, oldest first,
  only when the session is idle, no manual file is waiting, `PAUSE` is absent
  and the power rules allow it. Each auto-start shows a toast and is recorded in
  the status file as `last_auto` (file, sha256, time).
- **Power and pause.** Every 2 minutes the battery is read; `PAUSE`
  (`build/queue/PAUSE`, made by `/mola`, removed by `/devam`, or by the
  orchestrator through the file) stops the inbox starting anything. On battery:
  at 40% or more at most one prompt runs at a time (the inbox waits while another
  lane's status says `working`, believed for 3 h); below 40% nothing new starts
  (one toast); below 20% the status file says `low_battery` so the orchestrator
  can tell the lanes to stop at a step boundary. A running turn is never killed
  or interrupted. Windows power settings are not touched by the mod.
- **Manual smoke test** (Salih, after integration): see the report of
  MB-flow1 in `build/reports/MB-flow1.md`.

**Wave 2 (Salih 2026-10-08; MA1).** Same plugin, same activation rule. Runtime
folders `build/locks/`, `build/flakes/`, `build/ledger/` and `build/reviews/`
are gitignored like the others.

- **Suite queue (A1).** At most one full suite (`python scripts\test_parallel.py`
  in any form, or `scripts\test.ps1`) at a time across all sessions on this PC.
  The lock is `build/locks/suite.json` (lane, session id, started_at, command,
  heartbeat); it is taken and freed with an exclusive compare-and-swap open, so
  two sessions cannot both win. The holder writes a heartbeat once a minute; a
  lock is stale after 45 min without one (a forged future time or a garbage file
  counts as stale too) and is then taken over. Another session's full-suite call
  is HELD up to 40 min, the hook waiting inside `$.process.run` calls, with the
  band showing "Test kuyruğu: <lane> koşuyor, <n> dk"; after 40 min the call is
  refused with a reason that says wait, then retry. While a full suite holds the
  lock, other sessions' `python -m unittest` runs that load UI tests (a
  `test_ui*`-style target, a whole-suite `discover`) are held the same way. The
  holder's own runs are never held. The lock is freed on the tool result, on an
  error and on session end; a background full suite keeps it until the runner's
  processes are gone. The hold hook fails closed: if the check itself breaks, the
  suite or UI command is refused ("retry"), other commands pass. This is a
  courtesy queue for model-driven commands, not a security boundary (a command
  built to hide `test_parallel` from the match is not caught).
- **Flake radar (A2).** After every test command (a full suite, or
  `python -m unittest ...`) the result is parsed (Ran N, FAIL/ERROR ids with
  subTest parameters, wall time) and one row is appended to
  `build/flakes/ledger.jsonl`: time, lane, kind (`full|targeted|alone`; `alone` is
  a single test-method id), targets, ran, failures, ok, wall_s, python_procs (at
  the start of the command), other_lane_held (another session held the suite lock
  when the run started or it had to wait). `/flaky` prints, per failing test id,
  the fails under load, the passes and fails of `alone` runs of it, and when it
  was last seen. No model call.
- **Pager (A3, ntfy).** Setup, once, with the topic value from Salih (never
  committed, never in the repo, logs, reports or ledgers):
  `setx JARVIS_NTFY_TOPIC <topic>`, then restart Claude Code. Pushes (Turkish,
  short, lane + what waits + since when) when this session has waited on a
  permission prompt for 5 min (then every 30 min while it still waits), when the
  battery is below 20% on battery, and when a header turn ends with `STATUS:
  STOP`, `BLOCKED` or `FAIL` (information only since CONV2's incident C, silent
  02:00-08:00; the conveyor's attention list is the alarm); at most one push
  per 10 min per session (a push inside the gap is dropped). It runs `curl.exe` through `$.process.run` with a
  fixed argument list; the topic and text go in on stdin as a curl config, so
  neither is on a command line. Topic names are `[A-Za-z0-9_-]{1,64}`. With the
  variable unset or invalid there is no network call and one toast.
- **Slice ledger (A4).** For every turn started by an `@jarvis` header prompt one
  row is appended to `build/ledger/slices.jsonl`: start, end, wall_s, lane, model,
  effort, the turn's token usage, the `five_hour` and `seven_day` plan percentages
  at start and at end, tool_calls, test_runs (kind, ran, failures, python_procs,
  wall_s: the python process count is taken at the start of every test run) and
  the final STATUS line. `/defter` prints today's rows.
- **Band fix.** A model change the user makes (`/model`, the model picker, the
  SDK) now moves the chat's family: band, `build/lanes/<family>.json` and the
  queue folder follow. The start pin guards only the automatic fallback model.

**jarvis-codex** (`.claude/skills/jarvis-codex/`, a separate plugin so it can be
disabled on its own; Salih approved its network use on 2026-10-08).
`/codex-inceleme <A..B> [name]` runs the Codex CLI non-interactively as an
independent reviewer of `git diff <A..B>` (tracked files only) and writes
`build/reviews/<name>-codex.md` (RANGE, VERDICT, run facts, then Codex's BLOCKER /
MAJOR / MINOR findings with file:line and a verdict of READY, READY-WITH-NITS or
NOT-READY). The band shows "Codex inceliyor…" and a toast says when it is done; no
Claude turn is used. The only network use is Codex's own call to its model with the
diff text. Codex is run as `codex exec -s read-only -c approval_policy=never
--ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check -C
build/reviews/_codex-cwd -o <file> -` with the prompt on stdin: the repository is
not its working folder and no diff text is on a command line. Before each review
the installed Codex must list the read-only sandbox flags in `codex exec --help`;
if not, nothing runs. The command refuses (nothing is sent) when the diff touches
`uv.lock`, `data/**`, `build/**`, `.git/`, `.env*`, key/certificate/database
files, `.jarvis/`, `.codex/` or an 8.3 short name, when the diff is over 300 KB or
empty, or when the range is not a plain `A..B`. The result file records whether
`git status` changed during the run.

## Konveyor

Salih approved the design on 2026-10-09. `scripts/conveyor.py` (stdlib only,
tests `tests/test_conveyor.py`) keeps development flowing while the PC is on.
It runs under a Windows Task Scheduler task, one pass every 60 s (`--once` for
one pass). Its only git/gh calls are the push gate and the autopilot below
(the autopilot runs only the gate's checks and `git diff --name-only`; it never
pushes), and it deletes no
file itself (lane sessions: see Cleanup below); its only model call is
the optional report reader below. One instance at a time
(`build/conveyor/conveyor.lock`, stale after 5 min). Runtime folder
`build/conveyor/` (gitignored): `inbox/`, `backlog.json`, `lanes.json`,
`status.json`, `events.jsonl`, `pager.json`, `ack/`.

- **Dispatch.** Each lane is a fresh `claude --bg --name <lane> --model <m>
  --effort <e> --settings build/conveyor/lanes.settings.json <prompt>`; at most
  3 lanes at a time. Inbox first: `inbox/*.md`, oldest first, line 1 a valid
  `@jarvis` header by exactly the jarvis-flow lint's rules (model
  `opus|sonnet|haiku|fable`, effort `medium|high|xhigh|max`, base `main` or 7-40 hex, any
  field order; a sha base must prefix-match canonical `main`'s HEAD, as in
  `lint.ts`; one table in `conveyor.py`, a test fails if it drifts from
  `header.ts`/`lint.ts`). A bad header is never dispatched: the prompt is moved
  (never deleted; an earlier same-name reject is kept) to `inbox/rejected/`
  with a reason file and is an attention item `reject-<file>` "header invalid:
  <reason>". A backlog slice whose generated header the lint would block (bad
  model/effort/id, stale base) is not dispatched: `held: <reason>` in the
  backlog view and one `backlog-invalid` attention item (also for invalid
  entries) per distinct error text. A re-dispatch whose base is no longer main
  is not sent; the lane is finished `DEAD` "re-dispatch not sent: ...". Started
  prompts go to `inbox/started/`. Only when the inbox is empty does the backlog
  dispatch.
- **Dead session.** 5 min (`DEAD_S`) after dispatch (or re-dispatch), a lane
  whose session is gone from `claude agents --all`, or whose session is not
  busy and whose transcript `~/.claude/projects/*/<sessionId>.jsonl` holds no
  `user`/`assistant` entry (the prompt was never submitted), is finished as
  `DEAD`: its slot is freed, the session is left alone (no stop/rm), no reader
  call, and it is an attention item "prompt not delivered / session gone".
  The listing has no archived flag or message count, so these are the only
  signals; a session with no transcript found keeps its slot. A `DEAD` lane
  whose session is rescued in place and later writes its report is finished by
  that report as usual.
- **Backlog** `build/conveyor/backlog.json`, written by the orchestrator:
  `{"slices": [{id, title, model, effort, tier, files, depends_on, prompt,
  approved}]}` in order. `tier` is T0/T1/T2; `files` are repo globs the slice
  may touch; `depends_on` slice ids; `prompt` a path from the repo root (its
  own `@jarvis` line, if any, is replaced by one built from the backlog, keeping
  its base; otherwise `base=main`). The first slice is dispatched whose
  dependencies have a PASS/COMPLETE/DONE/GREEN/OK report, whose files do not
  overlap a running lane's files (literal prefix before the first wildcard,
  conservative; inbox lanes have no files) and, for T2, only with
  `"approved": true` (Salih approves T2 at plan level). A slice that already
  has a report is not dispatched again. `status.json` `backlog` shows each
  slice's state (`ready`, `waiting_deps`, `overlap`, `needs_t2_approval`, a
  STATUS, ...) and invalid entries.
- **Finished by report.** A lane is finished when `<lane>.md` with a `STATUS:`
  line, written after its start, exists in `build/reports/`, in any
  `.claude/worktrees/*/build/reports/` or in the session's cwd, whatever the
  session shows (idle, blocked). Its slot is freed, it is never respawned, and
  a worktree report is copied to the canonical `build/reports/` (a different
  older file there is kept as `<lane>.<yyyymmdd-hhmmss>.md`).
- **Reboot.** A lane without a report whose session stopped is `claude
  respawn`ed (once per stop). A respawned lane with no activity for 10 min (no
  new file in its worktree, no `build/flakes/ledger.jsonl` row, no report) has
  its silent session stopped (`claude stop`, the conversation is kept) and is
  re-dispatched from its saved prompt with a short "continue from where you
  stopped" preface; after 2 re-dispatches it is finished as `STUCK` (slot freed,
  in `attention`). An idle or finished session never blocks a new lane of the same name.
- **Blocked lane (CONV9).** A live lane session whose turn ended on an API error
  (network loss; G6R1, INT18, G6R1B on 2026-10-10) shows state `blocked` and never
  continues by itself. After 10 min blocked (pace.json `blocked_nudge_min`, clamped
  5..30) and only when `api.anthropic.com:443` accepts a TCP connect, the conveyor
  nudges it: `claude stop <id>` (conversation kept), then `claude --bg --resume
  <sessionId> --name/--model/--effort/--settings ...` "Continue the task from where you
  stopped. ..." in the session's cwd (`--bg` help: with `--resume` it "continues that
  session in the background under the same ID"); at most 2 per lane, 15 min apart, a
  `nudge` event each; never for a lane with a report. 45 min blocked with no new file
  in its worktree: attention item `<lane>` "lane blocked, needs orchestrator" (one
  `blocked_attention` event; night quiet as in Autopilot) to drop or requeue it.
- **PAUSE.** `build/conveyor/PAUSE` present: nothing new starts (no dispatch,
  no re-dispatch); tracking, report collection and respawn go on.
- **Disk guard.** Free space on the repo drive under 10 GB (`MIN_FREE_GB`,
  `shutil.disk_usage`; unreadable counts as low) holds like PAUSE: no dispatch,
  no re-dispatch. `status.json` shows `disk_hold` and `disk_free_gb`; one
  `disk-low` attention item (and one `disk_hold` event) per episode; at 10 GB
  or more dispatch resumes and the episode ends.
- **Memory guard (CONV5).** Free physical memory under `min_free_mem_gb`
  (pace.json, default 3.0) or a commit charge above `max_commit_pct` (pace.json, default 90, clamped 80..97;
  `GlobalMemoryStatusEx` via ctypes, stdlib only) holds like PAUSE: no
  dispatch, no re-dispatch; running lanes are untouched. `status.json` shows
  `memory_hold` and `memory` (`free_gb`, `commit_pct`) and `pace.band` reads
  `memory hold` (a band change, so a `pace` event). Unreadable memory never
  holds: `memory` is null and one `memory_unknown` event is logged per
  conveyor process.
- **One integration lane at a time (CONV5).** A lane whose name starts with
  `INT` not followed by a letter (`INT7`, `int-x`; not `INTERACT1`) is not
  dispatched while another such lane is running, whatever `max_lanes` allows
  (they share the main checkout and run the full suite). An inbox prompt
  waits in the inbox; a backlog slice shows `int_busy`.
- **Auto-push of main (two keys).** Salih (2026-10-09 21:32) delegated the
  main push: "push it when you judge it right". Lanes still never push main;
  only the conveyor does, and no model is the authority. Key 1: the
  orchestrator, after reading the integration report, writes
  `build/approvals/push-<sha>` or `push-<sha>-<ci>` (content free text, only
  the name counts; `<sha>` 7-40 hex). Key 2: every pass, for each approval,
  all checks must hold: (1) `<sha>` matches `git rev-parse` of local `main`
  in the canonical checkout; (2) after `git fetch origin main`, origin/main is
  an ancestor of `<sha>` and not equal to it (fast-forward only); (3) on the
  CI branch the newest run of every required workflow (`tests`) with
  `headSha` exactly `<sha>` is completed with conclusion success (`gh run
  list --branch`); the CI branch is `ci/<ci>` from the approval name, else
  the single `origin/ci/*` that `git branch -r --contains <sha>` lists (none
  or several: blocked, name the approval with `-<ci>`); (4) `git diff
  --name-only origin/main..<sha>` has no `uv.lock`; (5) the line
  `SCHEMA_VERSION = 10` is in `jarvis/memory.py` at both origin/main and
  `<sha>`. Then exactly `git push origin <sha>:refs/heads/main` (never
  `--force`, no other ref), a `push` event with the sha and CI run url, the
  approval moved to `build/approvals/done/` (never deleted) and an ntfy "main
  <sha7> GitHub'a gönderildi" through the pager (10 min gap). A failed check
  or push: a `push_blocked` event with the reason and an attention item (id =
  the approval file name; ack `build/conveyor/ack/<name>`), nothing pushed,
  re-checked at most once per 10 min (`build/conveyor/push.json`). Removing
  the approval withdraws it. Since CONV7 key 1 is also written by the
  autopilot below; key 2 is unchanged.
- **Autopilot (CONV7, Salih 2026-10-10 03:56).** The conveyor takes the three
  routine orchestrator decisions itself; the orchestrator stays the watchman:
  it checks `status.json` periodically, acks attention items, closes the
  breaker and writes new slices. State: `build/conveyor/autopilot.json`
  (`since`, `chains`, `slices`, `bad`, `breaker`, `last_auto`); only lanes
  finished after `since` (the autopilot's first pass) are acted on. An
  unreadable or malformed `autopilot.json` stops every autopilot action (its
  fix slices are not dispatched either) and holds product lanes like an open
  breaker until it is fixed by hand.
  - Auto key 1. An integration lane (name matches `INT` as above; never a fix
    lane, i.e. a chain's `-AFX` or any name ending `-AFX`) whose report has exactly one line `STATUS: GREEN` (nothing
    else on it), exactly one `NEW MAIN SHA: <40 lowercase hex>` line, exactly
    one `ci/<name>` branch and exactly one CI run URL
    (`https://github.com/<o>/<r>/actions/runs/<id>`) gets
    `build/approvals/push-<sha40>-<name>` written by the conveyor (never over
    an existing file), only when: no other integration lane in `lanes.json` is
    unfinished, and every gate check above passes now (local main is that
    sha, fast-forward of origin/main, the newest `tests` run for that sha on
    that `ci/*` branch is completed/success, no `uv.lock` in
    `origin/main..sha`, `SCHEMA_VERSION = 10` at both ends), and the run the
    gate verified is the report's run. Then an `auto_approve` event and the
    band line `auto-approved <sha7>` (`status.json` `band`, 1 h); the push
    gate runs its own checks again before pushing. Any other case, an
    exception included: nothing is written, an `auto_approve_refused` event
    and the lane is an attention item "auto-approval refused: <reason>"
    (the orchestrator may still write key 1 by hand). One try per finished
    report; a report written before CI finished is refused, not retried.
  - Auto fix-forward, once per integration lane name. An integration lane
    that finishes `STATUS: BLOCKED` whose report names failing test ids
    (`tests.<module>.<Class>.test_*`) queues two slices ahead of
    `backlog.json` (they are not written into it): `<INT>-AFX` (Opus, effort
    high; tier T1; prompt from `build/conveyor/prompts/_autofix_template.md`,
    written from the built-in text when missing, editable, placeholders
    `{INT} {AFX} {R} {REPORT} {MAIN} {BRANCH} {TESTS} {FILES}`; branch from the
    current local main; owned files = `git diff --name-only` from local main
    at the lane's dispatch (`main_at_start` in `lanes.json`) to local main
    now) and `<INT>-R` (the same INT prompt, same model/effort, with a
    preface to pick only what is missing plus the fix commit; `depends_on
    [<INT>-AFX]`). Prompts go to `build/conveyor/prompts/auto/`. No test id,
    no picked file, unknown start main, an invalid slice or an `<INT>` name
    already used for a chain: no chain (`auto_fix_skipped`, the reason is
    added to the BLOCKED lane's attention item). Never a second automatic
    attempt: a BLOCKED `<INT>-R` (or `<INT>` again) is attention only.
  - Breaker. 3 lanes finishing BLOCKED/STOP/FAIL within 2 h, or a fix chain
    lane (`-AFX` or `-R`) finishing with anything but a good STATUS (incl.
    DEAD/STUCK), opens it: `status.json` `breaker: "open"`, band line
    `breaker: open`, a `breaker` event and attention item `breaker`. While
    open no new product lane starts (inbox prompts wait in the inbox,
    backlog slices show `breaker_open`); integration and fix lanes
    (`INT...` names) still start; running lanes are untouched. Acking the
    item does not close it: only the orchestrator (or Salih) touching
    `build/conveyor/breaker-reset` after it opened does; the BLOCKED count
    then starts from zero.
  - Night quiet. The 30 min escalation page (below) waits between 02:00 and
    08:00 local time and goes out after 08:00 if the item is still unacked,
    unless the breaker is open (then it pages at night too).
- **Usage guard.** `claude` has no plan-usage command, so the newest
  `build/ledger/slices.jsonl` row (jarvis-flow slice ledger, `seven_day`) is
  read (a row older than 7 days is ignored). The limit comes from the pace
  governor below. `status.json` shows `usage_pct`, `usage_hold` and `pace`.
- **Pace governor (Salih 2026-10-10).** Quality is fixed by the routing table
  below and never changed dynamically; pace is controlled only by how many
  lanes run in parallel. `build/conveyor/pace.json`, re-read every pass (the
  orchestrator may edit it by hand at any time; written with the defaults if
  missing): `{"mode": "full_burn" | "paced", "window_start": ISO|null,
  "days": 7, "reset_alert_pct": 95, "full_burn_limit_pct": 98,
  "min_free_mem_gb": 3.0, "max_commit_pct": 90, "lanes_present": 3, "lanes_away": 5,
  "away_after_min": 10, "power_switch": false, "power_switch_cmd": []}`.
  `max_lanes` below is the presence lane count (`lanes_present` /
  `lanes_away`, see "Presence modes"); a hand-set `"max_lanes"` key (Salih
  2026-10-10 03:26) overrides both. All three are numbers clamped to 1..6
  (`MAX_LANES_CAP`); a non-number makes pace.json invalid. `min_free_mem_gb`
  feeds the memory guard.
  - `full_burn` (until Salih uses his one manual quota reset): up to
    `max_lanes` lanes; no new lane at `usage_pct >= full_burn_limit_pct`. At
    `usage_pct >= reset_alert_pct` Salih is paged once ("Haftalık kota %<n>:
    sıfırlama hakkını şimdi kullanabilirsin") and attention item
    `quota-reset-alert` is added.
  - `paced`: `pace_line = 100 * elapsed_since(window_start) / days`.
    Effective max lanes: `max_lanes` when `usage_pct <= pace_line + 2`, 2 when
    `<= pace_line + 7`, 1 when `<= pace_line + 12` (never above `max_lanes`),
    else 0 (no new dispatch;
    running lanes are never touched). No new lane at `usage_pct >= 97`.
    Integration lanes count like any lane (and at most one runs, see above).
    Unknown usage: `min(3, max_lanes)` lanes.
  - Reset detection: `usage_pct` dropping 40 or more points between two
    readings (manual or weekly reset) switches pace.json to `paced` with
    `window_start` = now, logs a `quota_reset` event and adds attention item
    `quota-reset` ("kota sıfırlandı, tempo modu açık").
  - An invalid pace.json is read as `paced` from now (the cautious side), left
    unchanged on disk, and is attention item `pace-invalid` until fixed.
  - The decision (`mode`, `usage_pct`, `pace_line`, `max_lanes`, `band`,
    `limit_pct`, `error`, `presence`) is `status.json` `pace`; a change of
    band is a `pace` event. A slice's model and effort are never altered.
- **Presence modes (CONV6; LaptopControl v1.3 plan, Phase A).** The
  conveyor only reads the user's idle time and sets the priority of its own
  lanes; it changes no system setting (power plan, service, registry).
  - Presence: idle seconds from `GetLastInputInfo` (ctypes, stdlib only; the
    conveyor's own Windows session). `away` at idle >= `away_after_min`
    (pace.json, default 10); `present` at idle under 1 min; in between
    unchanged. A change counts only after the new state held 2 min
    (`PRESENCE_HOLD_S`), so away starts ~12 min after the last input and a
    single touch does not end it. Unreadable idle time is `present` (one
    `presence_unknown` event per process). A change is a `presence` event;
    `status.json` shows `presence` (`state`, `idle_s`) and `pace.presence`.
    A restart starts `present`.
  - Lanes: `max_lanes` = `lanes_present` (default 3) or `lanes_away`
    (default 5), still under the paced bands, the memory and disk guards and
    one integration lane at a time. Running lanes are never stopped.
  - Priority, own lanes only: for each running lane in `lanes.json`, its
    session pid from `claude agents` (same id and name; the process must be
    `claude.exe`) and that process's descendants (each created after its
    parent, so a pid-reuse orphan is never taken) get
    `BELOW_NORMAL_PRIORITY_CLASS` when present, `NORMAL_PRIORITY_CLASS` when
    away. Applied once per (session pid, presence): on a presence change, to a
    new dispatch once its pid is listed, and after a respawn; later children
    inherit it. No other process is touched. One `priority` event per lane
    (pid count, failures); a failure or an unreadable process list
    (`priority_error`) is logged, never fatal, not retried for that pid and
    presence.
  - Phase B hook, off by default: `"power_switch": true` runs
    `power_switch_cmd` (list of args, `{plan}` -> `JarvisWatch` when present,
    `JarvisFocus` when away; e.g. `["<path>\\LaptopControl.exe", "--activate",
    "{plan}"]`) once per presence change, 60 s timeout. No default command or
    path ships. A non-zero exit, a missing exe or a timeout is a
    `power_switch` event plus attention item `power-switch` (ack
    `build/conveyor/ack/power-switch`), no retry until the next presence
    change; a later success clears it. `power_switch: true` with an empty
    `power_switch_cmd` makes pace.json invalid.
- **Model routing table (Salih 2026-10-10).** Static; the header of every
  prompt and backlog slice follows it:
  - 6G product slices: Opus, effort medium.
  - T2 / security / R1 / R2 / 6H / architecture: Opus, effort high.
  - Integration lanes: Sonnet, effort medium.
  - Auto-fix lanes (`<INT>-AFX`, autopilot): Opus, effort high.
  - Mechanical lanes (test re-pointing, catalog, one-line fixes): Sonnet,
    effort medium.
  - Pace only via the number of parallel lanes (pace governor above).
- **Attention and pager (ntfy).** Salih's rule (2026-10-09): lane events go to
  the orchestrator, not to Salih. A lane report `STATUS: BLOCKED/STOP/FAIL`, a
  `STUCK` lane, a non-`pass` reading (below) and the backlog running dry (no
  lane running, no inbox file waiting and nothing to dispatch; id
  `backlog-dry`, one item per episode) are items in `status.json` `attention`
  (`{id, reason, since}`), not pushes. The orchestrator acks an item by
  creating or touching `build/conveyor/ack/<id>` (existence only, never
  deleted; an ack older than the item's `since` does not count, so a reused
  lane name needs a fresh touch). ntfy (topic from `JARVIS_NTFY_TOPIC`, same
  rules as the jarvis-flow pager; never logged) reaches Salih only for: an
  attention item left unacked for 30 min (`ATTENTION_PAGE_S`, "the
  orchestrator did not react"; once per item; held 02:00-08:00 unless the
  breaker is open, see Autopilot); and 3 failed conveyor passes in
  a row (plus the push notices below). At most one push per 10 min; pages inside the
  gap, or after a failed send, are held and sent together.
- **Report reader (advisory, optional).** Salih approved it on 2026-10-09
  (monthly API credit). When a lane finishes by report or is marked `STUCK`,
  its report (only the report text, at most 12,000 chars, head + tail; a
  `STUCK` lane without a report sends a fixed note) goes with a fixed
  instruction to Claude Haiku 5.5 (`claude-haiku-5-5`, effort `low` via
  `output_config`, Anthropic Messages API, stdlib, 20 s timeout; no assistant
  prefill, which Haiku 5.5 rejects with 400: JSON comes from the instruction and
  strict validation). The strict-JSON answer `{kind, summary,
  next}` (kind `pass`/`blocked`/`stalled`/`needs_orchestrator`/`needs_salih`)
  is stored as `reading` on the lane in `status.json` plus a `reader` event;
  a summary or next over 140 characters is clipped (`…`), not rejected;
  invalid output becomes `needs_orchestrator` "reader: invalid output" and its
  `reader` event carries `raw` (at most 300 chars of the model text,
  whitespace collapsed, the key and anything `sk-ant-` shaped removed) and
  `stop_reason`.
  A reading that is not `pass` puts the lane in `attention` (above); only
  `needs_salih` is listed with reason prefix "reader says needs_salih:" and
  pages nobody directly: the 30 min escalation above is its only page.
  The reader is never the authority: it cannot approve integration, push,
  patch apply or any git action, and never changes the `STATUS:` handling,
  dependencies or slots above. Key: `JARVIS_READER_API_KEY` (user env var; on
  Windows read from `HKCU\Environment` at call time, so no conveyor restart is
  needed): `setx JARVIS_READER_API_KEY <key>`. Never name it
  `ANTHROPIC_API_KEY` (that would switch Claude Code lanes to API billing); it
  is never logged or written anywhere. No key: the reader is off (one
  `reader_disabled` event per day). Every call is recorded in
  `build/ledger/api.jsonl` (tokens, model, lane, `est_usd` at $0.10 / $0.50
  per MTok input / output); at $20
  (`READER_MONTHLY_USD`) estimated for the current month, calls stop until the
  next month. Network error, timeout, non-200 or the cap: no reading, a
  `reader_error` event with the HTTP status only, the conveyor goes on.
- **Cleanup** (Salih 2026-10-09: lane sessions must not pile up). A lane in
  `lanes.json` finished by report (or `STUCK`) whose report was collected 2 h
  ago or more (`CLEANUP_S`; older lanes without `finished_ts`: the report's
  mtime), and that is not an unacked attention item, has its sessions (incl.
  re-dispatched or same-name earlier `prev_ids`), checked again on a fresh
  `claude agents` listing, stopped if alive (`claude stop`) and removed with
  `claude rm <id>` (the CLI has no archive; `rm` also removes the session's
  worktree only "when that is safe" - the conveyor never passes
  `--discard-unpushed` or `--force-remove-worktree`). First the usage ledger
  must have counted every session after the grace time (else cleanup runs it,
  at most once per 30 min), so token history survives a removed transcript. Any doubt
  (session not `background`, other name, state not done/blocked/failed, busy,
  no session id, ledger not counted) leaves the session (`cleanup_skip` event,
  once per reason); a stop/rm error leaves it for good (`cleanup` event with
  `result: error`, `last_error`). At most 5 lanes per pass; one `cleanup`
  event per lane. Sessions not in `lanes.json` (Salih's, the orchestrator's)
  are never touched.
  **Preserved lanes** (CONV11): before `rm`, a session whose cwd is a lane
  worktree is checked read-only in the canonical checkout (`git worktree list
  --porcelain` for its branch, then `git cherry main <branch>`). Only `-`
  lines (every commit has an equivalent patch on main, e.g. integrated by
  cherry-pick) lets `claude rm` run, once. Any `+` line, unknown branch, git
  error, or an `rm` that still refuses (`exists on no remote ...`) marks the
  lane `preserved` in `lanes.json` (reason, branch, first unintegrated sha),
  lists it under `preserved` in `status.json`, raises one attention item
  `preserved:<lane>` and the conveyor never touches it again (no `rm` retry,
  no deletion); the orchestrator removes the `preserved` key by hand. The CLI
  force options (`--discard-unpushed`, `--force-remove-worktree`) are never
  passed: the first also drops uncommitted changes.
- **Early stop** (CONV8, memory): such a lane's live sessions are `claude stop`ped (never `rm`) as soon as its report is collected and it is not in attention (same doubt checks on a fresh listing; once per lane, `early_stop` event; a failure is logged, never fatal); `rm` still waits for the 2 h + ledger rule above.
- **Switch-over.** The scheduled task runs `scripts/conveyor.py` from the
  canonical checkout (v0 was `build/conveyor/conveyor.py`); it reads the same
  `build/conveyor/` state. Every 30 min it runs the usage ledger
  `scripts/usage_ledger.py` (tests `tests/test_usage_ledger.py`; it merges into
  `build/ledger/usage.json` and never drops a known session; an unreadable file
  is left untouched), else the older runtime `build/conveyor/usage_ledger.py`.

### Incidents and lessons

- **A. Lint-dropped prompts held slots (2026-10-09 evening).** Prompts with
  `effort=low` passed the conveyor's own looser header check and were
  dispatched; the jarvis-flow lint blocked them inside the session, leaving
  empty sessions (AGY0, AGY0B) that held 2 of 3 slots for 40 min. Archiving
  them in the desktop app did not free the slots (`claude agents` still showed
  `blocked`). Fix (CONV2): the pre-dispatch check uses the lint's rules
  exactly, and the dead-session rule frees such a slot after 5 min.
- **B. "reader: invalid output" (G6K3, INT6, G6N2FIX, AGY0B, AGY1).** The
  accepted readings' fields were 96-130 chars, against a hard 140-char reject;
  AGY0 and AGY0B (same report, same 164 output tokens) were accepted and
  rejected respectively: Haiku miscounts Turkish characters. Fix (CONV2): over
  140 is clipped, the instruction asks for 100, a raw newline inside a string
  is accepted, and an invalid answer logs a redacted snippet and stop reason.
- **C. The plugin paged on STATUS BLOCKED.** jarvis-flow pushed a bad
  `STATUS:` straight to Salih's phone as an alarm, bypassing the orchestrator.
  Fix: the orchestrator's change to `register.ts`/`pager.ts` (committed by the
  integration lane): that push is information only, marked "(bilgi;
  orkestratör bakacak)", silent 02:00-08:00; the alarm path is the conveyor's
  attention list.
- **D. The reader paged Salih (AGY2, 23:15).** Haiku labelled AGY2
  `needs_salih` wrongly (the orchestrator resolved it alone) and the conveyor
  paged the phone at once. Fix (CONV3): a reader verdict never pages; it is an
  attention item ("reader says needs_salih: ...") and only the 30 min
  escalation, failed passes and push notices reach Salih.
- **Rules.** Efforts are `medium|high|xhigh|max` only (never `low`);
  models `opus|sonnet|haiku|fable` (CLI aliases, passed verbatim); a sha base
  must be main's HEAD. A bad status or a
  problem lane goes to `attention` for the orchestrator, never straight to the
  phone as an alarm; Salih is paged only by the backstop or repeated conveyor
  failures (and push notices).

## V2.0 scope freeze

Salih decision (2026-10-06): V2.0 scope is frozen. New ideas go to the
"Next release inbox" in the roadmap (V2.0 section), never into a V2.0
slice; V2.0 takes only bugs and blockers. The mechanical lamp (third status-indicator skin) and the
freeze-key skins 3-4 move to V2.1.

## Standard gate order & exits

Prompts reference this section instead of repeating it.

**Order:** implement (smallest related tests only) -> freeze -> targeted ONCE ->
the tier's required run ONCE on the frozen tree (load-sensitive rerun rule D,
see "Testing discipline": only listed tests failing -> each rerun alone once;
record both results) -> only if green update
`JARVIS_ACTIVE_STATE.md` -> `git diff --check` -> commit -> report (hash, files,
real counts, `git status --short`, pushed or not) -> STOP.

**BLOCKED exits:** before any implementation edit -> ACTIVE_STATE-only commit
`docs(state): <gate> blocked - <reason>`. Mid-implementation -> leave the work
uncommitted, record the blocker and the exact uncommitted files in
ACTIVE_STATE, stage only that file, same commit form. Then report and STOP.

**Standing hard rules:** never touch `uv.lock`; never stage `dev/dashboard/*`;
stage explicit paths only; no push without Salih's explicit authorization; no
reset / restore / delete / stash of work; no live connector / model / auth /
network calls unless authorized; tests never open the live DB;
`python -m unittest` only (UI tests on the system Python, as
`scripts/test.ps1`); do not start or reinstall the packaged app unless the
step says so.

**Amend:** an unpushed local commit may be amended only to complete that same
commit; say so on the first line of the report.

## Acceptance and commit

When acceptance criteria pass: commit and stop. Do not keep iterating past
acceptance, and do not bundle unrelated cleanup into the acceptance commit.

## Push

Never push without explicit authorization from Salih. This applies every
time — prior authorization does not carry forward to the next push.

**Standing exception (Salih 2026-10-02): `ci/*` branches.** Claude Code may
push and force-push branches named `ci/*` (disposable test branches that run
the suite on GitHub Actions, `.github/workflows/tests.yml`; helper
`scripts\ci.ps1 [-Name x]`). Pushing `main` or any other branch still needs
Salih's explicit authorization every time. The workflow runs only on `ci/**`
pushes and manual dispatch, with read-only permissions and no secrets.

**Delegated exception (Salih 2026-10-09): `main` via the conveyor.** Verified
`main` is pushed only by `scripts/conveyor.py` under two keys (an orchestrator
approval file plus deterministic checks), see "Konveyor" → "Auto-push of
main". No lane and no Claude session pushes `main` itself.

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

**Retired (Salih 2026-10-06):** the old dashboard is unused; its WIP is
parked in git stash "Salih dashboard WIP (2026-09-12), parked 2026-10-06".
Do not update or stage `dev/dashboard/*`. The rules below describe the
retired tool and stay only for reference.

Update `dev/dashboard/status.json` only after the accepted work is canonical
on main. See `dev/dashboard/UPDATE.md` for the full contract (percentages are
computed by the renderer, never written by hand). Also:

- Keep dashboard summaries short and human-facing.
- Do not change roadmap weights or progress rules unless explicitly told to.
- After a canonical milestone update, commit the resulting tracked
  `status.json` change in a small dev-tooling commit so main is clean again.

## Worktree lifecycle — HARD RULE

Applies to `dev.ps1` helper worktrees only (the fallback). Native worktrees
follow "Parallel lanes".

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
