# JARVIS Active State

Snapshot of NOW (runbook "Active state file"); STATEDOCS 2026-10-18.

## PROJECT STATE

Active: V2.1 close and V2.2 close in parallel; V2.4 Step 0 started (V24S0
PASS a400ca9: ~20 slices, est. 18-26, plan `build/reports/V24S0-plan.md`).
V2.0 closed 2026-10-13. V2.1+V2.2 ship as one release: the release lane opens
once INT56 is green and on origin, then 2-3 days dogfood with skills on
(Salih 2026-10-17). Roadmap: `JARVIS_V2PLUS_ROADMAP_AND_DECISIONS.md`.

INT chain: INT49G 5793ebd (CI green, Ran 908 OK) → INT55 93ef714 (FLOWFIX)
→ INT53 cd7aa82 (13 picks) → INT54: CDXINT54C BLOCKED A1 → SKSIMASK PASS
acdcd58 → SKSIMASK4B → SKDRAFTFIT/2/3 → CDXINT54I PASS → INT54R2 353cbe2
(Ran 5184 OK) = origin/main. Local main ea29fc7 = INTCONV2 (conveyor +
TESTINFRA): INTCONV2R BLOCKED on 5 UI tests red also on base (O27
known-red, root cause lane UIRED5); INTCONV2P CI_PENDING on `ci/intconv2`.
Queued: INT57 (docs + watchdog + FLOWUSAGE 121861e), INT58 (DFHOVER 119da76,
DFRESUME, DFPAUSE 20ae99a, DFPAUSEFIX/2, CDXDFPAUSEC), INT59.

## Open items

- V2.1 hardening wave: V21HARD1 PASS d07bc10 accepted; V21HARD2 PASS
  6cf3879 (items 5-7 → V21HARD2B) and V21HARD3 PASS 70a3580 (→ V21HARD3W,
  writer-side key block) both go into INT59; FGT (F-gt) not done.
- V2.2 INT54 landed (origin 353cbe2); left: joint release + dogfood.
- MEMEVAL3 PASS 4037591 (OR-merge ADOPT), not integrated; no embeddings call.

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
  60 s and a timeout names its condition and the bridge state),
  `test_dfreply.LongMarkdownTests.test_a_50000_character_markdown_reply_renders_whole`
  and
  `test_dfwin.WindowPowerV2Tests.test_the_power_up_ignites_the_core_then_unfolds_the_content`
  (orchestrator 2026-10-13: render/animation timing under parallel lanes; pass alone).
  `scripts/test_parallel.py` reruns every listed `module.Class.test` id itself; rule D
  for whole-module entries is applied by hand.
- Push: `main` only via the conveyor two-key gate; `ci/*` standing-authorized
  (RUNBOOK "Push"). Pace up to 8 lanes (Salih 2026-10-18 12:20); O23 quota
  stop at 90%; O28 no local full suite in INT prompts. Board: "JARVIS Pano"
  (https://claude.ai/artifact/LboCEVPnvVb9QapsNbD8GZ).
- Rollback/safety: `1.0.0-df463a5` active, `1.0.0-3d6400b` rollback; backups
  `%USERPROFILE%\JARVIS-backups`; `SCHEMA_VERSION` stays 10; dashboard WIP in
  stash (never stage `dev/dashboard/*`); never stage `uv.lock`.

## Next action (in order)

INTCONV2P CI → push ea29fc7; INT57; INT58; V21HARD2B + V21HARD3W → INT59;
FGT; CDXINT56 → INT56 (with GMAILMOVE) → release lane; V2.4 after Step 0.
DFCOREA (`build/conveyor/hold/`) postponed: near quota renewal, else last.
