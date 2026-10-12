# JARVIS Active State

Overwrite-style snapshot of NOW (rules: `JARVIS_RUNBOOK.md` → "Active state
file"; freshness: "Session bootstrap"). History lives in git.

## PROJECT STATE

Active milestone: V2.0 Projects (dogfood-capable; the official V2
daily-driver gate is V2.1). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`,
"V2.0 — Projects" (scope freeze, Next release inbox, slice estimates) and §3.7
"Salih kararları — V2.0 Gate 6G / 6H planı". V2.0 scope is frozen (Salih
2026-10-06): only bugs and blockers.

Gates 1–5, 6A–6H and S5 (model list) are on main. V2.0 is in dogfood: the
dogfood fixes were integrated by INT37 / INT38 / INT39 and pushed; `origin/main`
= `3d6400b` (INT39 GREEN, CI run 38236839853; `build/reports/INT39.md`).

Installed: `1.0.0-3d6400b`, checked by Salih on 2026-10-12 (single instance,
window effects, drafts, jump-to-latest, own context menu: all pass);
`1.0.0-c3df707` previous (rollback target).

INT40 in progress: DFTONE, DFQUEUE, DFSPEED, DFMOTION, DFFONTTRY, DFFONT (font
rendering T by default), V20DOCS. Conveyor lanes follow
`build/conveyor/notes/LANE_RULES.md` (runbook "Konveyor" → "Lane process").

## Open blockers

**V21BUSY (V2.1 cleanup, T2) green, 2026-10-11:** committed on lane branch
`v21busy` (base v21k3 95b67d3, so V21K3's memory.py is under it); not
integrated, not pushed. Store write transactions retry `BEGIN IMMEDIATE` on
SQLITE_BUSY (whole transaction, nothing read or written yet): 3 attempts,
pauses 0.05/0.2 s, 15 s total cap, then the original error as before; every
retry and the give-up logged (`jarvis.memory` WARNING). Full suite as
foreground shards (N = 12 fallback; 6/6 shard 4 hit the 10-min cap)
aggregate 5446 OK (skipped=2), no rule D reruns. Report:
`build/reports/V21BUSY.md`.

**V21K3 (V2.1 cleanup, T2) green, 2026-10-11 (V21K3C):** committed on lane
branch `v21k3` (base ca3ffb5; pick db67868, then the fix commit); not
integrated, not pushed. (a) a K3 reject after a changed proposal ends
REJECTED (Core falls back to the stored request, every identity/fingerprint
check kept); (b) a malformed folder link row reads "unreadable", never
folderless. Targeted 119 OK; full suite as foreground shards
(`test_parallel.py --shard I/12`, the N = 12 fallback after 6/6 shard 4 hit
the 10-min cap) aggregate 5441 OK over 12 shards (skipped=2), no rule D
reruns. One independent review (no BLOCKER/MAJOR; NITs fixed). Left out of
scope: ARCHITECTURE.md:3600 wording, connectors/project_folder.py docstring.
Reports: `build/reports/V21K3.md`, `V21K3B.md`, `V21K3C.md`.

**G6H3POL (6H follow-up D-6/F-2/B-1, T2) green, 2026-10-11 (G6H3POLB):**
committed on lane branch `g6h3pol` (base 8ebe7e3; pick 81bccb8, then the
fix commit); not integrated, not pushed. Targeted 135 OK (skipped=1); full
suite `test_parallel.py --workers 4 --ui-workers 2` 5394 OK (skipped=2),
1301 s, no rule D reruns. One independent review (1 MAJOR + 2 MINOR + 1 NIT
fixed). F-2: pick-existing checks the folder read-only, rejects through
Core, then links, so the request ends REJECTED. B-1 docstring. D-6 for the
English refusals plus project_folders' Turkish one. Left for the V2.1 list
(via G6DOCS): the D-6 remainder (`tests/test_g6h1b_ui.py`,
`tests/test_g6h2_folder_ui.py`, `jarvis/ui/present.py`, the _UNVERIFIED
suffix) and the FAILED residual when the proposal changed before Core's
reject (needs core.py). Reports: `build/reports/G6H3POL.md`, `G6H3POLB.md`.

**G6H3 (6H H3 K3 chat approval card, T2) green, 2026-10-11 (G6H3C):**
committed on lane branch `worktree-G6H3` (base fea0ead); integrated on local main by INT26, not pushed.
K3: a folderless active project's chat proposes its folder through a Core
approval card; JARVIS computes the path, the card shows it ("Bu
bilgisayar"), every mode asks. Salih "6H3 A": archived = held by Core,
accepted. Composition pins re-pointed additively (`tests/test_app.py`
registry names + `project-folder-create`; `tests/test_github_phase1.py`
policy + the K3 DestinationGrant). Full suite on the frozen tree 5369 OK
(skipped=2), 662 s. Report: `build/reports/G6H3C.md`.
CLI follow-up (G6H3CLI, Salih 2026-10-11 "A"): `jarvis approve` refuses a K3
request (only the app card shows the path); reject allowed; app.py unchanged.
On lane branch `worktree-G6H3CLI` (base aa6cdb6), not integrated; full suite
5385 OK (skipped=2), 668 s. Report: `build/reports/G6H3CLI.md`.

**G6H1 (6H H1 folder link, T2) green, 2026-10-10 (G6H1B):** committed on lane
branch `worktree-G6H1` (base e3b2015), integrated on local main (INT16, not pushed); lane commit f417e57; additive
`project_folder_links` store, `jarvis/project_folders.py` path safety,
`project_folder_result` app boundary; context only, no Core change. Targeted
(9 modules) 573 OK; full suite on a quiet machine 5107/0/0 with the known
native `test_ui` crash group (0xC0000005, 32 ids) green under rule D (3 split
groups and the crash-point tests alone all pass; TUICRASH ff1b4fd on main
fixes the crash). Next: conveyor push gate.

**G6H1K (6H H1b folder creation, K1 popup, T2) green, 2026-10-10:** on lane
branch `worktree-G6H1K` (base 46a040f, G6H2 present), not integrated;
"Klasör oluştur" (+ "Masaüstünde") in the new-project popup, D1 path
recomputed and required equal, creation inside the store's one transaction
(`project_folders.create_and_link`, origin `created`), held no-follow
handles with handle-relative `NtCreateFile`, nothing removed, link state read
back. Independent review: 1 MAJOR + 2 MINOR, all fixed. Targeted (10 modules)
727 OK; full suite 5315/0/0 (601 s, skipped 2). Next: integrate on "entegre et".

**G6R1C (6G R1 background run, T2) green, 2026-10-10:** on lane branch
`worktree-6g-r1b` (base 0e2e746), not integrated; bridge run record (memory
only), navigation lifted for recorded runs, running-only refusals, preview
gate, delivery-time ownership, composer "başka bir sohbette" link. Targeted
154 OK; full suite 5191/0/0 (514.7 s). R2 (parked approvals, freeze/resume)
open. Next: integrate on "entegre et".

## Do not reopen

- V2 M1 Edge → Chrome migration complete (incl. packaged + rollback).
- Gate 0: reuse `projects`; Global = `project_id` NULL; opaque ids; owner
  fixed (only exception 6C, Global → project, one-way, once); scope is
  context, never authority; no `SCHEMA_VERSION` bump while schema-10 releases
  are rollback targets (additive tables only, no ALTER).
- Gates 1–5 (§4.22–§4.24) incl. project archive, instructions, files.
- Gate 6 UX (6A–6D-b, 6E, 6F) as built; 6F F7: a Global conversation deletes
  in one step, an active project's unarchived one must be archived first.
- The 6G/6H plan as recorded in the roadmap (mockup v16 approved 2026-10-05).
- Mechanical lamp and freeze-key skins 3–4 shipped in V2.0 (Salih 2026-10-11,
  A-2/A-3 accepted). Status lamp default stays as shipped: mechanical, glass
  orb selectable in settings (G-2, orchestrator decision). V2.1 keeps the usage
  popover's context-window block and owns `conversation_global_memory_toggles`.
- Gate 6G close (Salih 2026-10-11): A-4 W3 work panel floats beside the stream
  instead of the approved right-rail top: accepted, OPEN DOGFOOD item (Salih
  judges it). B-2 K1 "Oluştur" while frozen still creates the folder (click =
  approval, Q5), K3/E7 refuse: deferred, freeze feature revisited after V2.
  "Gate 6G review leftovers" list lives in the roadmap V2.1 section.

## Decisions / external / backlog

- Load-sensitive list (RUNBOOK rule D; only the orchestrator edits):
  `test_ui.RailSwitchTests.test_switching_cards_closes_a_then_opens_b`,
  `test_ui.PrePolishTests.test_the_gauges_show_the_kernel_s_figures_or_nothing_at_all`
  and
  `test_browser_lifecycle.FailClosedTests.test_a_refusal_the_browser_will_not_acknowledge_destroys_the_tab`
  (orchestrator 2026-10-08: CDP timing under load; passes alone 3/3; follow-up
  F-bl makes its wait event-driven),
  `test_ui.ConversationUxTests.test_a_new_reply_glides_once_to_the_end_and_idle_does_not_move`
  and
  `test_ui.ConversationSurfaceTests.test_conversation_scrolling_keeps_the_reading_position`
  (orchestrator 2026-10-08: scroll-animation timing under load; pass alone;
  follow-up F-sc makes their waits event-driven),
  `test_ui.AuthorityTests.test_background_work_does_not_block_the_gui_thread`
  (orchestrator 2026-10-10: GUI-thread SQLite commit + WAL checkpoint takes
  0.03 s alone, 0.57 s only under full-suite disk load; follow-up F-gt (T2,
  memory.py/bridge save order, V2.1): move the user-turn save and checkpoint
  off the GUI thread; Gate 6G C-7: it stays on this list only until F-gt
  lands in V2.1 and must not hide a real stall),
  `test_task_chain.DriverConcurrencyTests.test_two_runners_never_duplicate_a_step_task`
  (orchestrator G6TCFLAKE 2026-10-11: one runner died on CI shard 3/4, run
  38093652961, test 26.6 s there vs 1.3 s locally; a runner whose write waits
  past the store's 5 s busy timeout raises `database is locked` out of
  `run_chain`; the test now puts each runner's traceback in its failure
  message),
  `test_bg_run_6g` (whole module; orchestrator G6BGFLAKE 2026-10-11:
  `test_t4_the_running_conversation_refuses_its_mutations_with_zero_writes`
  timed out its 10 s wait for the approval card on CI shard 4/4, run
  38109324177, test 15.3 s and module 74 s there vs 7 s locally; every test
  pauses a real worker thread, so all of them depend on timing. Its waits are now
  60 s and a timeout names its condition and the bridge state). `scripts/test_parallel.py`
  reruns only `test_ui` ids itself; rule D for the others is applied by hand.
- Cloud sessions: unused until the cloud-branch push decision (RUNBOOK).
- Push: `main` not authorized; `ci/*` branches are standing-authorized
  (RUNBOOK "Push"); CI = `scripts\ci.ps1`.
- Old dashboard retired (Salih 2026-10-06). The orchestrator's board is the
  artifact "JARVIS Pano" (https://claude.ai/artifact/LboCEVPnvVb9QapsNbD8GZ).
- Gate 7 still to do: D-3 one approved live Claude call after Salih's own CLI
  re-login; D-4 packaged `-Rollback` and back (approvals cleared,
  `conversation.json` backed up and restored). Codex review queue (one commit
  per session): session1 remainder, Gate 5, 6A–6D-b, fixes.
- Backlog: project-filtered jobs query; catalog test skips on a system
  Python; Gate 4B Edge retirement deferred; regenerate
  `scripts/test_durations.json` now and then (`--write-durations`).

## Next action (in order)

1. INT40 (integrate, CI, push on Salih's go). 2. Install INT40 + Salih's
checks. 3. D-4: packaged `-Rollback` and back. 4. V2.0 close.

Core refresh (DFCOREA, parked in `build/conveyor/hold/`) postponed by Salih
(2026-10-12): a later week near the weekly quota renewal if quota is
plentiful, else the very last item.

## Rollback / safety anchors

- Releases (`%USERPROFILE%\Apps\JARVIS\release-state.json`): `1.0.0-3d6400b`
  active; `1.0.0-c3df707` previous (rollback target); `1.0.0-cf53097`
  retained. Pushed anchor: `origin/main` `3d6400b`.
- `%USERPROFILE%\JARVIS-backups`: `releases\legacy-1.0.0-5976769` (+
  `.manifest.sha256`, 2929 files verified) and `2026-09-30-gate7`
  (`data\jarvis.db` + `~\.jarvis\*.json`, SHA-256 manifest, taken before
  8fbbaef first opened the DB).
- `data\jarvis.pre-v2.db` verified; `SCHEMA_VERSION` stays 10 while any
  schema-10 release is a rollback target; V2 only reads
  `~/.jarvis/conversation.json`.
- Dashboard WIP is parked in git stash "Salih dashboard WIP (2026-09-12),
  parked 2026-10-06"; never stage `dev/dashboard/*`.
- `uv.lock` is excluded locally via `.git/info/exclude`; never stage or
  commit it.
