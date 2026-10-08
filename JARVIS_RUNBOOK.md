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
The parallel runner applies this itself.

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
canonical `build/` folders. It makes no network calls and approves no tool
permission. Runtime folders `build/queue/`, `build/lanes/` and `build/reports/`
are gitignored and never committed.

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
