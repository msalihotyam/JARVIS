# N6 handover matrix

Which conveyor and orchestrator BEHAVIOURS JARVIS must take over, with what safety rule and test.
Process plan v5.3 item 13a. Base: 6411bf48. Design document; no code changes.

Fixed frame (V1 order, 31.10): shadow -> limited real tasks (low-risk slices) -> independent acceptance -> gradual
takeover. The conveyor stays the fallback. N6 cannot change Core policy. Main push and deployment stay two-key.
Production JARVIS never changes its own running code unchecked. Previous-version rollback is kept.

Stages, as used in the "Stage" column (the earliest stage where the row must be real):

- shadow: JARVIS computes the decision, acts on nothing, and logs it for comparison with the conveyor.
- limited: JARVIS acts on low-risk slices only; everything outward stays with the conveyor.
- independent acceptance: the E8 gate and the M9 fixed acceptance set are judged by someone other than N6.
- takeover: JARVIS owns the behaviour; the conveyor is the fallback.

Evidence convention:

- Unmarked claims about code were read in source at the base commit.
- "absent" means a string-level grep of `jarvis/*.py` (power_switch, powercfg, GetLastInputInfo, `gh run`, `run rerun`,
  Thursday, 22:00, ntfy, `worktree remove`) had no hit. It is proof of missing strings, not of missing behaviour.
- "(unverified)" marks a claim whose code body was not read: conveyor `track`, `attention`, `usage_ledger`,
  `grant_check`, `off_main`, and the bodies of `work_plan.validate`, `notify_sink`, `StageManager`, the store fencing
  methods, `scheduler_runner`, `owned_process`, `trusted_exec`, `workers/route`. Rule texts come from
  `build/orch/ORCH_RULES.md` and gates from `build/orch/ORCH_LIVE_STATE.md`.

Wiring fact that frames every row: no production module in `jarvis/*.py` imports `work_driver`, `work_ports` or
`work_supervisor` (grep: only docstrings and the `work_plan` path list mention them). PM26A exists and is tested, but it
is not connected to the app. `work_controls` alone is imported (`core.py:266`, `work_planner.py:18`).

Where JARVIS is already stricter than the conveyor, the JARVIS rule wins. These rows are not ports: unknown quota
pauses (conveyor: no hold), unknown review severity blocks, there is no review skip, no checks means refused, apply runs
exactly once, budget state must be an explicit False to continue, event and outbox row commit in one transaction.

## 1 MATRIX

Paths: `conveyor` = `scripts/conveyor.py` (methods of `class Conveyor` unless noted).

| id | behaviour | conveyor/orchestrator today | JARVIS counterpart today | safety rule that must survive | acceptance test | stage | rollback |
|---|---|---|---|---|---|---|---|
| R01 | Quota manager (E6): weekly window, 5% reserve, plan line | `conveyor:govern`, `load_pace`; PACED_LIMIT 97, PACE_MARGIN 2, PACE_DEFAULTS; O18, O23. Window is `window_start` + `days`; reset is inferred. Thu 22:00 and a 5% reserve are absent (grep, no hit); 97 is a 3% margin | `work_supervisor.quota_pacing` (caller supplies used and line; QUOTA_LIMIT_PCT 100); `work_controls.budget_verdict(quota_available)`. Thu 22:00, reserve, line computation: NEW (absent) | Unknown quota PAUSES (JARVIS rule, not the conveyor's no-hold). Highest of several readings counts. No new Claude start at or above 95% used. A running slice is never killed and its model never changed. Limits are Core policy | Clock Thu 21:59 then 22:00: line goes near 100 to near 0. used 95.0 -> pause; 94.9 at line -> dispatch; used None -> pause `O23.unknown`; readings 40 and 96 -> pause; ahead of line by more than the margin -> non_claude_only; a slice running at 95 is still running | shadow | Conveyor `govern` stays owner; JARVIS decision is only logged |
| R02 | Quota manager (E6): usage reader and CONVUSAGE jump/reset guards | `conveyor:usage_pct`, `govern`; RESET_DROP 40, RESET_MIN_AGE_S 6 d, SPIKE_WINDOW_S 6 h, SPIKE_CONFIRM_S 1800, USAGE_FRESH_S 7 d; events `quota_reset`, `quota_reset_refused` | NEW. `quota_pacing` has no reader and no guard | A jump of RESET_DROP or more is held until a second source confirms or the same source repeats after the confirm wait. A drop is a reset only when the window is old enough. A stale reading is unknown, and unknown pauses | 30 then 75 from one source: 75 held; same source at +1800 s: accepted. 80 then 20 on day 3: `quota_reset_refused`, window unchanged; on day 6.5: reset. Reading 8 days old: unknown -> pause. No fresh source at all: pause | shadow | Reader output ignored; conveyor reader continues |
| R03 | Pause/resume | `conveyor:hold` (PAUSE file, disk, usage, memory, outage, cc_hold), orchbot `hold_dispatch`; O34, O38, O54. PAUSE fails OPEN and boot removes it (O38, O54) | `work_controls.pause`, `cancel`, `resume`; `work_driver.drive` (3 failed slices -> PAUSED); `work_supervisor` `operator_pause` first in PRECEDENCE, absent gate -> pause. Durable operator switch: NEW | Pause is a durable ledger fact and survives reboot. Unreadable pause = paused (fails closed, the opposite of O54). Resume is explicit and revalidates identity, capability, target, scope, approval in that order. Pause stops new starts and does not kill running work. Budget counters never reset on resume | Set pause, restart the manager: nothing starts. Corrupt the pause record: paused. `resume` with reordered revalidation or `explicit=False`: ValueError. New boot_id: still paused. Running worker alive after pause. Usage unchanged after resume | limited | Conveyor PAUSE stays the master stop for anything conveyor-owned |
| R04 | Dependencies, including INT dependents | `conveyor:pick_slice`, `slice_done`, `int_pushed`, `load_backlog`, `files_overlap`; O2, O2+, O10, O34; gate B4 (CONVINTDEP) | `work_ledger.runnable_steps`, `input_blocked_keys`; `work_driver.drive` ("Failed slices strand their dependents"). Step dependencies inside one run only. Backlog across runs and INT push wait: NEW | A BLOCKED dependency never unblocks dependents. A dependent of an INT starts only after push and main SHA are verified, and the SHA is passed along (B4). Anything unknown = not done. Hold = out of backlog; `approved:false` holds only T2. Overlapping files wait | Dep BLOCKED: dependents wait. INT GREEN but origin/main lacks the SHA: wait. Fetch error: wait. Two passes dispatch one dependent exactly once. Dependent's base equals the verified SHA. Overlap with a running slice: wait | shadow | Conveyor `pick_slice` stays owner; compare picks only |
| R05 | Independent review routing (provider independence G3) | `conveyor:codex_veto` (module), `route`, `start_codex`, `codex_commit`; C5 and C5 SKIP; O29. T2 double review is arranged by orchestrator prompts (unverified as a code rule) | `work_review.reviewer_for`, `review_slice`, `review_mode`; `work_ports` review port; `work_driver._accept`. Single cross-provider reviewer. `same_provider` is REFUSED, so neither T2 double review nor the recorded low-risk exception can be expressed: NEW | G3: T2 = separate Claude review + Codex, both must pass. Low-risk exception = separate-session same-provider review, recorded as such. Never the writer's own session. Reviewer read-only and can only narrow. No skip. Unknown severity blocks. Risk comes from the Core floor; a model may only raise it (O53) | T2 with Codex PASS alone: refused. T2 with Claude PASS and Codex unparsable: `review_failed`. Low-risk same session id: refused. Low-risk separate session: accepted and the receipt records the kind. T2 slice labelled low-risk by a model: floor keeps T2. Reviewer reports changed files: `review_failed`. Review of an older commit: refused | limited | Conveyor review lanes keep reviewing; JARVIS verdict is advisory |
| R06 | Receipts/evidence | `conveyor:find_report`, `report_status`, `collect`, `push_record`; GOOD set; report text is the input; `usage_ledger` rows (unverified) | `work_evidence.EvidenceReceipt` (`canonical_json`, `verify`); `work_ports` evidence port: plan hash, judged diff sha, check argv, max exit, output sha, base result, verdict. No reviewer provider, session, invocation or reviewed-SHA field; `GateDecision` has none either: NEW | B2+: a trusted invocation record (provider, real run, SHA, decision, immutable), not a worker-writable file. Report text is never authority. PASS, COMPLETE and BLOCKED do not convert. M4: a tool that did not run is NOT_RUN. M2: a partial commit is not PASS | B2 negatives: fake PASS, self-review, review of an old commit, missing second key, commit changed before push: all refused. Worker writes the receipt path: ignored. Check that never ran: NOT_RUN, blocks. One changed byte: `verify` fails | limited | Conveyor report flow continues; receipts stay additive |
| R07 | State ledger | `conveyor:event`, `load_lanes`, `save_lanes`, `write_atomic`; `events.jsonl`, `lanes.json`, `autopilot.json`, pager state. `lanes_bad`: nothing dispatched or removed, `{}` never saved over it | `work_ledger` (`advance_run`, `advance_step`, state enums), `memory.append_work_event`, `notify_outbox_rows` | One store, no new DB (M8). `SCHEMA_VERSION` stays 10; additive tables only, no ALTER. Invalid state: nothing dispatched or removed, never overwritten. Tests never open `data\jarvis.db` | Corrupt row: no dispatch, attention raised, row intact. Illegal transition: rejected by `advance_step`. Schema assert 10 at both revisions. Test run touches only its TEMP DB | shadow | Conveyor files stay authoritative; ledger rows are additive |
| R08 | Manager lease and fencing (gate M8) | `conveyor:acquire_lock`, `own_lock`, `beat`, `loop`: read-then-write JSON, STALE_LOCK_S 300, no generation | `memory.claim_work_lease` (atomic, per target, overlap refused; no generation in signature), `renew_work_lease`, `release_work_lease`. Pattern to reuse: `condition_watch` `fencing_epoch` (store body unverified). Manager lease over backlog/event intake: NEW | Exactly one manager, JARVIS or conveyor, never both. Atomic claim with a generation; a stale generation is refused at the store. No new DB. A timeout alone never reaps (`watchdog_verdict`). The conveyor honours the same lease before it is used as fallback | Two concurrent managers on one temp DB: exactly one claims. Old holder writes with a stale generation: refused. Lease expired with holder alive: no reaping by timeout alone. Conveyor starts while JARVIS holds: refuses, and the reverse | limited | Conveyor lock stays; JARVIS does not act until this passes |
| R09 | Resource management: RAM and disk | `conveyor:real_mem`, `hold`, `govern` memory band (min_free_mem_gb, max_commit_pct, MEM_RELEASE_S, MEM_GAP_S), `disk_free_gb`, `disk_hold` (MIN_FREE_GB 10) | `work_supervisor.memory_hold` (unknown waits; hysteresis; no commit-charge rule). Sensors, commit charge, disk: NEW | Unknown reading = wait (memory) and hold (disk). Release only after the margin held for the OK time. Running work is not touched | Free unknown: wait. Commit above limit: wait. Free just over the minimum: still held until margin + MEM_OK_S. Disk unreadable: hold. No process killed in any case | shadow | Conveyor hold stays |
| R10 | Resource management: presence and power | `conveyor:sense_presence`, `real_idle`, `power_switch`, `power_skip_reason`, `prioritize`, `own_tree` | NEW (absent: power_switch, powercfg, GetLastInputInfo) | Off by default. One switch per presence change; failure = event + attention, no retry. Unknown or battery-profile plan: no switch. Priority changes only own tree, PID-reuse safe. Unreadable idle counts as present | Unknown plan: no switch. Failed switch: no second attempt. Reused PID outside own tree: untouched. Idle unreadable: present and below-normal priority, one `presence_unknown`. Change inside PRESENCE_HOLD_S: no switch | takeover | Disable the setting; conveyor keeps the switch |
| R11 | Idempotency | `conveyor:autopilot` (`ln["ap"]`), `ci_rerun_claim` (claim before `gh`), `auto_approve` (open "x"), `fix_chain` (one per INT), `push_gate` (`done/` never overwritten) | `work_ledger.idempotency_key`; `memory.append_work_event` (same run+seq no-op; conflicting event or reused key fails closed); `NotificationCore` dedupe; `work_driver._accept` apply_intent, `_resume_apply` | Every external effect records a durable intent first. A repeat is a no-op. A conflict fails closed. A retry that adds severity does not lose the notification. `next_seq` is read-then-append, so it relies on the single-writer lease (R08) | Replay the event tail twice: same state, one key 1, one CI rerun, one page. Crash between apply_intent and apply: undo and a failed attempt, no second apply. Same key with a different event: `WorkLedgerError` | shadow | Ledger only; no outward effect exists at this stage |
| R12 | Recovery: crash | `conveyor:loop` (pass exception caught), `heal`, `early_death`, `redispatch`, MAX_REDISPATCH, `lanes_bad` | `work_driver._drive_slice` (RUNNING -> INTERRUPTED), `_resume_apply`; `work_ports` stage reattach (`gate:"stage"`). Restart scan of open runs: NEW (`scheduler_runner` sweep as pattern, unverified) | Restart resumes from the ledger only. Worker dead before its first commit: at most one redispatch in the same worktree. Heal never covers CI red or interim timeout and can never yield key 1 | Kill the manager after stage create, after apply_intent, and after apply before receipt: each resumes exactly once. Worker dies before commit: one redispatch, then dead. Healed lane: no key 1 | limited | Stop JARVIS; run stays INTERRUPTED; conveyor redoes the slice |
| R13 | Recovery: network and API outage | `conveyor:outage`, `pager_watch`, `hold` (pager outage), OUTAGE_GAP_S, OUTAGE_NUDGE_S | Gate errors fail closed as findings (`work_driver`). Outage detection, hold and staged nudge: NEW | An outage is not a slice failure: PAUSED, not stuck; no redispatch; no retry counted; one page; one lane probed at a time; no live calls of its own. Network unknown blocks a push and never passes it | Two lanes with API errors inside the gap: paused, no redispatch, one page. Progress on the probed lane: hold clears, the rest resume in place. Fetch failure in the push path: not pushed. Attempt count unchanged | limited | Conveyor outage handling continues |
| R14 | Recovery: power loss and reboot | STALE_LOCK_S, `early_death`, `heal`; O38 `panel --boot` removes PAUSE; `scripts/watchdog.ps1` (unverified) | Lease `boot_id` in `work_ports` and `memory.claim_work_lease`; `_resume_apply`; `work_ports.undo` refuses partial or mismatched records. Boot scan: NEW; `owned_process` orphan handling (unverified) | Boot never clears a pause and never auto-resumes without revalidation. A half-applied slice is undone or proven applied, never applied twice. No worker from the old boot is treated as alive | New boot_id with a RUNNING step: INTERRUPTED and reclaimed only under the new boot. Pause preserved. Ref transaction cut mid-way: `applied` raises or undo restores; operator decides. Old-boot process record: not trusted | limited | Conveyor boot flow continues; JARVIS stays down until resumed |
| R15 | Stall handling: nudge and respawn | `conveyor:nudge`, `watch_idle`, `context_full`, `resume_in_place`, `dead_reason`, `active_since`; MAX_NUDGES 2, NUDGE_GAP_S 900, BLOCKED_ATTENTION_S; O56 | `work_controls.watchdog_verdict` (verdict only, no kill); `work_driver` TIMEOUT step is revisable. Acting on the verdict: NEW | At most two nudges, counted even when one fails. A timeout is not proof of death and never reaps. A resume carries a fact note built from git and files, not from a model. Same model and settings; no silent escalation | Stalled worker: nudge at the gap, third refused, then attention. Timeout with live process: not reaped. Fact note content equals `git` output. Failed nudge: counted. Resume keeps model and effort | limited | Conveyor keeps nudging its own lanes |
| R16 | Breaker | `conveyor:autopilot`, `open_breaker`, BREAKER_BAD 3, BREAKER_WINDOW_S 7200; opens per root id; closes by reset file or a GOOD non-bad finish; unreadable = open; BLOCKED CDX/DRY excluded | `work_ledger.run_should_pause` (3 failed slices, per run). Across runs: NEW. `work_supervisor.filter_alarms` (alarm input) | Bad results count one per root. Unreadable state = open. An open breaker holds new non-INT work. Only a real GOOD or an explicit operator reset closes it | Three bad roots in 2 h: open. Same root three times: not open. Corrupt state: open. Good CDX finish: stays open. Reset older than the opening: ignored | limited | Conveyor breaker keeps governing conveyor lanes |
| R17 | CI watching | `conveyor:ci_wait`, `ci_step`, `ci_runs`, `ci_done`, `ci_slow`, `ci_rerun_claim`, `stacked`, `is_ancestor`; REQUIRED_WORKFLOWS; O27; I1-I6 | NEW (absent: `gh run`, `run rerun`). `work_ports.apply` stops at `refs/heads/jarvis/<run>/main` | CI runs on `ci/*` only. A verdict binds to one exact SHA. One automatic rerun per SHA, claimed durably before the call. Red stays red except named KNOWN-RED. A failed stacked base blocks. CI errors mean wait, not green | Success on another SHA: not green. Required workflow missing: not green. Restart after a rerun: second rerun refused. CI API error: slow, not green. Stacked base red: BLOCKED | takeover | Conveyor keeps the CI step; JARVIS stops at its own ref |
| R18 | CI auto-fix | `conveyor:auto_fix`, `fix_chain`, `split_chain`, `chain_failed`; `_autofix_template.md`, `_autofix_ci_template.md`; O5, O6, O15, O56 | `work_supervisor.fix_chain` (FIX_CAP 2), `plan_integration`. In-slice revise loop in `work_driver`. Chain across integration: NEW | One chain per INT name, never a second. Needs failing test ids or an own failed CI run. A fix lane never gets key 1 and never starts a chain. Second failed fix ends in a decision for Salih. Any doubt: no chain | INT red without failing ids: no chain. Second chain for one INT: refused. Fix ends non-GOOD: re-integration dead, owner attention. Fix slice asks for key 1: refused. Restart mid-chain: no duplicate | takeover | Conveyor keeps auto-fix |
| R19 | Two-key main push | `conveyor:auto_approve`, `approval_for`, `protected_change` (unverified body), `push_gate`, `push_checks`; O13, O26; B2 | NEW. No JARVIS code pushes main; `work_ports.apply` writes only `refs/heads/jarvis/<run>/main` | Two independent keys, never generated from a template or by a worker (B2). Key 2 re-checks exact SHA, fast-forward of origin/main, CI success, no `uv.lock` in the diff, SCHEMA_LINE at both revisions. Never `--force`. Guard, deny, hook and policy weakening needs Salih's manual key 1. Any exception = blocked | Key 1 written by a worker: refused. Self-review, old-commit review, missing key, commit changed before push: refused. Non-FF origin: blocked. `uv.lock` in diff: blocked. `.claude/` change without Salih's key: refused. Exception in the gate: nothing pushed | takeover | Conveyor gate remains the only main push path until this passes; revoke JARVIS key source |
| R20 | Local main fast-forward, deployment, previous-version rollback | `conveyor:fast_forward` (clean checkout on main, `merge --ff-only`, verified after). Deployment and rollback are operator steps (unverified in code read) | NEW. `work_ports.undo` covers only the run's own ref | Fast-forward only: never a merge commit or reset. Production JARVIS never changes its running code unchecked. Deployment stays two-key. The previous version stays runnable on schema 10 | Dirty checkout: no move. Not an ancestor: no move. Update failing its health check: previous version running, schema still 10. Deployment with one key: refused | takeover | Reinstall the previous version; conveyor continues |
| R21 | Cleanup/preservation | `conveyor:cleanup`, `stop_finished`, `archive_sessions`, `remove_worktree`, `unintegrated`, `merged_equivalent`, `off_main` (unverified), `preserve`, `release_preserved`, `retry_cleanup`; O61; M3, N2 | NEW (absent: `worktree remove`). `write_stage.StageManager` manages stages (unverified) | Only what this manager started. Any doubt keeps the work. Never `--force`. Evidence = INT PICKS (source -> picked SHA) and on main, patch-id helper (M3). Branch deleted only if still the proven tip; never `main` or `ci/*`. A refused cleanup is preserved, not retried by hand. Listing has a timeout (N2) | Untracked file: kept. skip-worktree bit: kept. Git error: kept. Tip moved during delete: `update-ref` fails, kept. Unproven: preserved; proven later: released once. Worktree listing hangs: timeout, kept. Foreign worktree: untouched | takeover | Disable cleanup; a preserved worktree loses nothing |
| R22 | Paging Salih (important-only) | `conveyor:page`, `flush_pages`, `pager_watch`, `attention` (unverified body), `backstop`, module `ntfy_send`; PAGE_GAP_S, NIGHT_HOURS; CONVPAGE2; O16, O19, O21, O39 | `notify.NotificationCore` (ledger-backed dedupe), `notify_sink.NotifySink` (unverified), `notify_outbox`, `work_driver` notify points. Importance filter and phone delivery: NEW (absent: ntfy) | Salih is paged only for their own or urgent items; orchestrator-only items never. One push per gap, joined and clipped. Status pages carry no command and no link. Night waits unless the breaker is open or the API is down. Every ask carries a recommendation. At most four ask batches a day | Orchestrator-only item: no page. Ten events: one clipped page. Send error: retried once per gap, no duplicate. Worker text with a link: not in the page. 03:00 routine item: waits; breaker opening: sent. Ask without recommendation: refused | limited | Conveyor pager keeps sending; JARVIS sink off |
| R23 | Retry cap (O61) | O61 (two retries without a ROOT CAUSE line; cleanup refused once -> preserved), `build/orch/preflight.py` retry-cap ERROR (file exists, body not read), MAX_REDISPATCH, MAX_NUDGES | `work_driver` `capped()`, MAX_REVISIONS 5, FAILED_STEPS_PAUSE 3, `work_controls.revision_verdict`, `work_review` REVISION_CAP. ROOT CAUSE gate: NEW. PM26X: driver retries a non-retriable StageError 6 times (ORCH_LIVE_STATE; unverified in code) | A third attempt needs a recorded root cause. A non-retriable error is not retried. Caps do not reset on resume. A retry changes something or it does not start | Two failures, third without root cause: refused. Same with root cause: starts. Non-retriable StageError: one attempt, then operator. Pause and resume: counter kept. Sixth revision: `slice_failed_after_cap` | limited | Conveyor preflight keeps enforcing O61 |
| R24 | Preflight (O64) | `build/orch/preflight.py` (O1, O64; body not read), `conveyor:lint_slice`, `slice_lint`, `backlog_lint`, `check_header`, `validate_slice`; O3, O9, O52; M5 | `work_plan.validate`, `classify`, `scope`, `work_planner.plan_project` (bodies unverified except signatures). O64 equivalent: NEW | Deterministic first; a model only advises (M5). Errors stop the dispatch. ERROR: retry cap, owned files not equal to the grant. WARN: protected paths, more than 3 non-test `.py` without SPLIT-OK, test module without a duration record | One fixture per check: third retry -> error; owned differs from grant -> error; `.claude/` owned -> warn; 4 source files -> warn, with SPLIT-OK none; module with no duration -> warn. Model says fine on an error fixture: still error | shadow | Conveyor preflight stays the gate |
| R25 | Dispatch and concurrency cap | `conveyor:dispatch`, `dispatch_backlog`, `start`, `record`, `cap_full`, `busy_names`, `int_blocked`, `reject`; MAX_LANES 3; O14 | `work_supervisor.supervise` (one Decision; malformed raises); `work_driver.drive` (one run). Dispatcher over several runs: NEW | Hold is checked before any start. The caller must not dispatch on a malformed decision. A failed start raises attention and blocks nothing else. Precedence order stays: operator pause, gate, memory wait, quota pause, then the rest | Hold active: no start. Malformed Situation: ValueError, no start. Failed start: other slices still start. Cap full: wait. Same name running: wait | limited | Conveyor dispatch stays owner |
| R26 | Advisory report reader | `conveyor:read_report`, `reader_call`; READER_MONTHLY_USD, `build/ledger/api.jsonl`; never changes status | None needed: `work_review.parse_review` and `WorkerResult` are typed data | Advisory output never changes a state, a key or a decision. It holds no user key. No parallel truth | Reader garbage: every decision unchanged. Code path check: no import from reader output to key 1 or state write | takeover | Conveyor reader stays frozen as Salih set it |
| R27 | API credential broker (gate B3), only if N6 uses API access | `conveyor:reader_key`, `anthropic_post`; B3, B3 M4 redo; HKCU reader key removed and FROZEN (Salih) | NEW. `trusted_exec.trusted_environment` exists (unverified) | B3: 127.0.0.1 only; unguessable short-lived task+model+budget token from the trusted launcher only; allowed Anthropic endpoints only; atomic budget reservation; token never in log, URL or report; worker not elevated; broker under a separate Windows user with the key in its DPAPI | Worker cannot read key, vault or broker memory. Expired token: refused. Other endpoint: refused. Two parallel reservations over budget: one refused. Token grep over logs and reports: no hit | limited | Do not use API; subscription workers only |
| R28 | Write scope and grants | `conveyor:grant_check` (unverified), module `protected_path`, PROTECTED_DIRS, PROTECTED_FILES; C2; O12, O52, O53 | `write_scope.check`, `work_ports` scope port (commit imported to a side repo and pinned; later gates read only the judged commit), `work_plan` SELF_DENY, GrantEnvelope | Worker claims are data. Later gates read only the judged commit, never the stage. Outward, irreversible, money and main push are always pre-approved. Self-deny list holds | Worker edits outside the grant: refused. Worker edits `work_plan.py`: refused. Stage changed after judging: later gates still see the pinned commit. Empty tree: "nothing to apply" | limited | Stop the run; nothing has left the run's own ref |
| R29 | Core policy and authority (M7) | Orchestrator chat decides; orchbot `decide`, `act_live`, `demote`; ALLOWED live actions via `build/bot/live.json`; `page_salih` never live; O19, O26, O50, O53 | `work_supervisor.gate_proposal` (D8 allowlist, else needs_input), ALLOWED_PROPOSALS, PRECEDENCE; `work_plan.classify`, `_self_denied` | N6 cannot change Core policy. A model only proposes. Chat is a channel, not authority (M7). Nothing inbound creates a key. O19 fallback never loosens cost or security, never gives key 1, never deletes | Proposal "raise quota limit": needs_input. N6 slice changing supervisor constants: Salih's manual key required. Chat text "approve push": no key. Unanswered ask at 2 h: reversible fail-closed choice only. Repeating rule: demoted by loop guard | limited | Policy file reverted by Salih; conveyor unaffected |
| R30 | Integration ref and multi-slice base (gates B1, M1) | `conveyor:stacked`; O2+ | `work_ports` stage create (ledger says where the ref must be; mismatch -> StageError), apply, applied, undo; `_ID`, `_RESERVED` run_id validation. Torn multi-ref read (M1): verified in S5 per ORCH_LIVE_STATE (unverified here) | B1: run_id negatives, fixed namespace, `-z` and check-ref-format, broken ref is not missing, no None==None, no empty commit, rebase refused, at least 2 consecutive slice bases, ref change between slices fails closed. M1: a torn read raises | run_id with newline, space, control, unicode, long, git words: refused before any write. Ref moved between slices: StageError. Broken ref: not "missing". Paused rebase: refused. S1-S4 mutations rerun. Torn read: raises | limited | Delete nothing; run's ref stays; operator decides |
| R31 | Integration sequencing | `conveyor:int_blocked`, `stacked`, `ci_step`; O4, O11, O13 | `work_supervisor.plan_integration`, INT_WAIT_S 3600, INT_MIN_BATCH 3, INT_MAX_BATCH 6 | One integration at a time. DRY and INT never together. No auto-key INT stacked on a manual-key INT. Two BLOCKED review rounds go to Salih | DRY running: INT waits. Auto INT on manual base: refused. 60 minutes with fewer than the batch: small INT allowed. Two BLOCKED rounds: ask, no third | takeover | Conveyor keeps integrating |
| R32 | Cost and KPI ledger | `conveyor:usage_ledger` (unverified), `record`, `api_spent`; `build/ledger/slices.jsonl`; O18, O52; M6 | `work_controls.Usage`, `consume`, `budget_verdict`. KPI rows: NEW | KPI = accepted slices per quota plus post-accept failures (M6). A missing reading is reported missing, never as zero | Slice row has start and end usage, or an explicit missing mark. Overrun recorded. Resume never resets. Report equals a hand count on a fixture | shadow | Conveyor ledger stays the reference |
| R33 | Shadow comparison and E8 gate | orchbot `decide`, `check_agreement`, `agree`, `verdict` (BOTAGREE), `build/bot/decisions.jsonl`; shadow v0 acts on nothing | NEW. `work_plan.shadow_candidate`, SHADOW_MODE=True (body unverified) | E8: equal or better on pass rate, post-accept failures, Salih interventions, quota per slice, time to finish; zero security incidents. M9: "flawless" = fixed acceptance set (false PASS 0, security 0, rollback works). M6: three scenarios. Judged by someone other than N6 | Same slices through both, metrics side by side from the ledgers. One security incident or one false PASS: fail regardless of the rest. Rollback drill passes. N6 cannot edit the acceptance set (hash pinned). A worse metric: stage does not advance | independent acceptance | Stay in the current stage; conveyor unchanged |
| R34 | Fallback switch between JARVIS and conveyor | Conveyor is the only owner; no switch exists | NEW | One owner at a time through R08. Switching back needs no JARVIS code to run. No slice runs twice. In-flight work is preserved | Flip to conveyor mid-run: lease released, run INTERRUPTED, conveyor picks up, no duplicate slice. JARVIS process absent: conveyor still starts. Flip back: revalidation required | takeover | The switch itself; conveyor is the rollback |

## 2 DO NOT CARRY

Chat-only authority:

- D01 Chat message as approval (M7): chat is a channel, and a message cannot prove who decided what.
- D02 One-off pre-approvals O20, O22, O42: single-use decisions that have been spent.
- D03 Report text and worker-writable approval files as evidence (B2+): a worker can write them.
- D04 Orchestrator judgement as the source of key 1 (O26 as practised): only its deterministic checks carry; see R19.

User credentials:

- D05 Reader key in HKCU, `conveyor:reader_key` (FROZEN by Salih): B3 requires a separate user and DPAPI.
- D06 The operator's ambient CLI logins as worker authority: JARVIS needs scoped ports, not whatever the shell can reach.

Fragile CLI quirks:

- D07 `claude --bg --resume <sid>`, the session-id regex and `prev_ids` copy tracking (`conveyor:nudge`): worker adapters hide this.
- D08 `cc_hold` pin to one Claude Code version: tied to one CLI; an adapter compatibility check replaces it.
- D09 Transcript scraping for liveness (`dead_reason`, `active_since`): owned processes and ledger events replace it.
- D10 Regex parsing of report lines (`approval_for`: one STATUS, one NEW MAIN SHA, one CI URL): typed fields replace it.
- D11 Name-encoded meaning: `INT_RE` (O30), `-AFX`, `-R`, digit-final continuation: typed step kinds replace it.
- D12 `gh run rerun` text flow as a contract: put CI behind one narrow port, so the CLI is replaceable.
- D13 File mailbox (`build/conveyor/inbox/*.md`, `os.replace`): the ledger is the intake (M8).
- D14 JSON lock file (`acquire_lock`): not atomic, no fencing; R08 replaces it.

Outdated bottleneck rules:

- D15 MAX_LANES 3 and MIN_LANES 2 as constants, O4 wave size: measured limits belong in policy. The DRY and INT ordering does carry (R31).
- D16 O31 release-first and O40 weekly priority: dated priorities that have ended.
- D17 Slice shape forced by one-chat attention: O52's size limit carries as a budget rule, chat attention does not.

Conveyor-only UI assumptions:

- D18 O58 Pano and Ajan Ofisi view, O59 Panel switch: JARVIS has its own surface.
- D19 `status.json`, `story.json`, the Attention list format (O39): the rule carries (R22), the file formats do not.
- D20 O38 `panel --boot` removing PAUSE: wrong under R03; pause must survive boot.
- D21 O54 PAUSE file, read-only attribute, `pause_watch.log`: patches a fail-open file; R03 replaces it.
- D22 orchbot state files, `build/bot/live.json`, `build/bot/OFF`: JARVIS uses Core policy and the ledger.
- D23 `full_burn` mode with a limit above 95: conflicts with the E6 5% reserve unless Salih rules otherwise (gap G03).

ORCH_RULES items that exist only because the orchestrator is a chat:

- D24 O17 wake chain and backstop: a service has no wake problem.
- D25 O24 chat rotation (already superseded): no chat, nothing to rotate.
- D26 O25 wake economy and `hb_watch.py`: saves chat tokens that a service does not spend.
- D27 O33 and O33a ack at every wake: acks prove a chat read something; ledger state replaces them.
- D28 O36 no 'JARVIS' in task or chat names: a chat-host naming workaround.
- D29 O37 cloud timers and chat-host quirk: its approval core survives through O53 (outward pre-approval).
- D30 O55 and N1 compaction-proofing: no context to lose.
- D31 O62 post-compaction check: same reason.

## 3 ORCHESTRATOR RULES -> CODE

Every rule O1-O64 appears exactly once (O2+ with O2, O33a with O33). Module names
in this section come from the rows in section 1; a function whose body was not read is
marked (unverified).

### 3.1 Become deterministic checks in JARVIS (37)

- O1 preflight after every backlog or prompt edit -> preflight gate before dispatch (R24).
- O2, O2+ base SHA from the dependency's report or stacked base -> base derived from the ledger, `work_ports` stage create (R04, R30).
- O3 grep files before queuing -> preflight file-existence check; the ask-upfront part goes through O47.
- O4 DRY before CDX/INT, DRY and INT never together -> dispatcher ordering, `plan_integration`; wave size becomes a policy value (D15).
- O5 two Codex rounds on the same component -> `work_supervisor.fix_chain` counter; the outcome is an ask to Salih, not a decision.
- O6 fix class rule -> preflight refuses a fix slice with no class statement; whether the fix is good remains review judgement.
- O8 model and effort table -> route policy table (`workers/route`, unverified).
- O9 no contradicting notes -> preflight warning.
- O10 a BLOCKED dependency never unblocks its dependents -> `work_driver.drive`, `runnable_steps`.
- O11 small INT after 60 min, two BLOCKED rounds go to Salih -> `plan_integration`, `INT_WAIT_S`.
- O12 no hand edits in main, `.claude/**` needs Salih -> `work_plan` SELF_DENY, `write_scope.check`.
- O13 no auto-key INT on top of a manual-key INT -> push gate (R19, R31).
- O14 act on an idle slot -> dispatcher loop under `supervise` precedence (R25).
- O15 fix chain cap -> `fix_chain`, `FIX_CAP` 2.
- O16 asks batched, at most 4 per day -> notify ask batching (R22).
- O18 quota and model allotment, KPI -> quota manager plus KPI ledger (R01, R32).
- O19 unanswered for 2 h -> a reversible fail-closed choice from a closed list; never cost, security loosening, key 1 or deletion (R29).
- O21 never leave Salih without a solution -> the ask record refuses an empty recommendation (R22).
- O23 quota stop thresholds -> `quota_pacing`, values held as policy (R01).
- O26 key 1 checks, exception only by Salih -> two-key gate (R19).
- O27 KNOWN-RED -> named list inside the CI verdict (R17).
- O28 no local full suite in INT prompts -> preflight check (R24).
- O29 CDX question list -> review prompt template, `work_review.review_prompt`.
- O32 stability gate S1-S5, watchdog thresholds, each new failure class gets a deterministic check -> watchdog plus a regression test requirement (R15).
- O34 hold means out of the backlog, `approved:false` only for T2 -> backlog pick (R04).
- O35 check before claiming -> state claims are read back from the ledger (R07).
- O39 Attention is orchestrator-only, Salih is reached by asks and phone -> importance filter (R22).
- O45 workers are Codex CLI and Jules only, no Antigravity automation, limits read live -> route allowlist (unverified).
- O47 project mode -> `work_planner.plan_project`, `MAX_REASKS`, `needs_input`.
- O48 Opus default effort medium -> route table.
- O50 PM34/PM35, the model only proposes -> `work_review`, `work_supervisor.gate_proposal`.
- O52 slice size, grants, budget hook -> preflight plus `work_controls.budget_verdict`.
- O53 scoped grant, risk floor, model may only raise, outward actions pre-approved -> `work_plan.classify`, GrantEnvelope (body unverified) (R28).
- O56 root-cause check after the first REAL MAJOR, GOOD statuses, drop after 2 nudges or 30 min -> root-cause gate, typed status, nudge cap (R15, R23).
- O60 T1 A/B, design docs on Opus high -> route table plus E8 comparison (R33).
- O61 retry cap -> R23.
- O64 preflight checks -> R24.

### 3.2 Stay human or Salih decisions (9)

JARVIS records and may propose these; it never decides them, because N6 cannot change
Core policy.

- O7 tighten a rule after a BLOCKED on a "do not stop" reason: JARVIS only proposes the rule change.
- O41 main goal.
- O43 rival rule.
- O44 product goal.
- O46 small window face.
- O49 authority limit.
- O51 harness comparison.
- O57 second-opinion rounds.
- O63 process plan and stop-loss.

### 3.3 Die (18)

Reasons are in section 2.

- O17, O20 (one-off; the min_lanes value moves to policy), O22, O24, O25, O30, O31, O33 and O33a, O36, O37 (the approval core survives through O53), O38, O40, O42, O54 (replaced by R03), O55, O58, O59, O62.

## 4 GAPS

Ordered by V1 handover stage, and by dependency within a stage. Each next step is one
slice of at most 60 tool calls. Row references point to section 1.

### 4.1 Shadow

- G01 PM26A is not wired: no production importer (R25, R07). Next step: a composition root that assembles `build_ports` with a fake worker in a temp repo, with no app write path.
- G02 No shadow comparator (R33, R25). Next step: run `supervise` over a read-only snapshot of conveyor state, log its decisions, compare them with conveyor events; reuse the orchbot agreement pattern.
- G03 E6 window, reserve and plan line (R01, D23). Thu 22:00 and the 5% reserve are absent in both codebases; `PACED_LIMIT` 97 is a 3% margin; the supervisor limit is 100; no source read states the Thu 22:00 time zone; a `full_burn` limit above 95 conflicts with the reserve, so Salih decides, and until then JARVIS fails closed at 95. Next step: a pure function plus clock tests.
- G04 Usage reader and CONVUSAGE jump/reset guards (R02). Next step: port the reader and guards as a pure parser with jump, reset and stale negatives, reading a fixture only.
- G05 RAM, commit and disk sensors do not reach Situation (R09). Next step: add read-only sensors to Situation; a low reading holds dispatch in a test.
- G06 No O64 preflight port (R24). Next step: the deterministic checks first, each with one failing fixture; model checks stay out.
- G07 No per-slice KPI rows (R32). Next step: append rows from existing ledger events, additive only, schema stays at 10.

### 4.2 Limited real tasks

- G08 M8 manager lease and generation fencing (R08), first in this stage. Next step: reuse the `condition_watch` fencing pattern, no new DB, schema stays at 10; add a two-manager negative test and make the conveyor honour the lease.
- G09 B2 trusted invocation record and SHA binding in the receipt (R06). Next step: bind the record to the receipt, with the B2 negatives.
- G10 G3 review routing (R05). Next step: T2 double review (separate Claude review plus Codex) and a recorded separate-session exception in `reviewer_for` and `review_slice`.
- G11 NOT_RUN (M4) and partial-commit-is-not-PASS (M2) (R06). No NOT_RUN outcome was seen in the driver and ports read (unverified beyond those files). Next step: add the outcome and both negatives.
- G12 Durable operator pause that survives boot and fails closed (R03, R14). Next step: a durable flag read before every dispatch; an unreadable flag counts as paused.
- G13 Cross-run backlog pick and waiting for the INT push, B4 (R04). Next step: dependents start only when the push is on main; negative test with a pushed-late dependency.
- G14 Restart and boot scan of open runs (R12, R14). Next step: classify open runs at start; ambiguous runs hold instead of resuming.
- G15 Outage hold and stall action from `watchdog_verdict`, with a nudge cap (R13, R15). Next step: map verdicts to hold or nudge; an outage never counts against a retry.
- G16 Cross-run breaker (R16). Next step: count failures across runs, trip to hold, reset only by an explicit action.
- G17 O61 ROOT CAUSE gate and non-retriable StageError (PM26X) (R23). Next step: a third attempt is refused without a root-cause record.
- G18 Importance filter and phone sink (R22). Next step: filter in `notify` with an orchestrator-only negative that must not page.
- G19 B3 broker, only if N6 uses the API (R27). Next step: decide by checking whether any N6 route uses the API; build nothing otherwise.
- G20 B1/M1 acceptance (R30). Verify slice: rerun the negatives on the integration ref; no new code unless one fails.

### 4.3 Independent acceptance

- G21 E8 metric report, M9 hash-pinned fixed acceptance set, M6 three scenarios, judged outside N6 (R33). Next step: a read-only report over the KPI rows plus the pinned set, runnable without N6 code deciding the verdict.

### 4.4 Gradual takeover

- G22 CI watch port (R17). Next step: read CI results into a typed verdict, with the KNOWN-RED list explicit.
- G23 CI auto-fix chain (R18). Next step: a fix slice from a red verdict under the `FIX_CAP` limit.
- G24 Two-key main push (R19). Next step: a gate that needs both the approval record and the deterministic checks; a missing or stale key refuses, in a temp remote only.
- G25 Fast-forward, deploy and previous-version rollback (R20). Next step: rehearse on a temp install; JARVIS never replaces its own running code unchecked.
- G26 Cleanup and preservation with M3 evidence and the N2 timeout (R21). Next step: a dry-run listing first; a target with no evidence is preserved.
- G27 Integration sequencing (R31). Next step: order merges from the ledger; a manual-key INT blocks an auto-key INT (O13).
- G28 Presence and power (R10). Next step: read-only presence signal first, power actions behind a separate approval.
- G29 Fallback switch drill (R34). Next step: stop N6 mid-run in a rehearsal and show the conveyor takes over with no lost or doubled slice.
