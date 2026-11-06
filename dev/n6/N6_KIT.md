# N6 handover kit

Slice plan for "JARVIS takes over its own development" (process plan item P3-13b). It starts on Sunday 2026-11-08
at the latest (Salih). Base: 5ce9176f. This is a design document; it contains no code.

Sources:

- `dev/n6/N6_HANDOVER_MATRIX.md`: rows R01-R34, gaps G01-G29, rule mapping.
- `build/orch/ORCH_LIVE_STATE.md`: gates B1-B4 and M1-M9, and "N6 TASARIM SARTLARI" (Salih, 2026-11-05).
- `build/orch/ORCH_RULES.md`: texts of the nine decision rules.
- `build/reports/LANECTX.md`: cost facts.

Version name: the V1_KABUL_VE_SIRA K4 step 2 (N6) is now the V2 plan (V2 = the Jan-Feb main release).

Conventions:

- Every number marked "est." is an estimate.
- Numbers from LANECTX (last 7 days, 154 sessions) are measured and are marked "measured".
- "Slice" means one lane of at most 60 tool calls, as in the matrix gap list.

Fixed frame (V1 order, 31.10; nothing in this kit changes it):

- The stages are shadow -> limited -> accepted -> takeover.
- The conveyor (`scripts/conveyor.py`) stays the fallback.
- N6 cannot change Core policy.
- Main push and deployment stay two-key.
- Production JARVIS never changes its own running code unchecked.
- The previous-version rollback is kept.

Design conditions (Salih, 2026-11-05 01:21):

- Quality over time: 20-35 jobs, however long they take.
- N6 reasons as the orchestrator does: same model, broad READ-ONLY sight (files, reports, events, git, CI) and
  free-text notes.
- N6 acts only through a proposal, then a fixed code gate, then a second key.
- State-machine bugs are systematic, so these are mandatory:
  - shadow mode
  - named negative tests
  - review by two providers

Who builds the kit: the conveyor and the orchestrator build every slice below as normal lanes. N6 starts watching
those same lanes in shadow once N6-03 is in.

## 1. Phases and exit criteria

The E8 measures are counted per slice from the KPI rows (N6-02) and compared with the conveyor baseline on the same
slices (R33):

- (a) pass rate
- (b) post-accept failures
- (c) Salih interventions
- (d) quota per accepted slice
- (e) time to finish

A sixth condition applies in every phase: zero security incidents.

The conveyor baseline at the base commit (measured, LANECTX):

- Code PASS rate: opus 57% (n=83), sonnet 69% (n=13).
- Tokens per accepted code lane: opus 1.60M, sonnet 1.26M.
- BLOCKED plus DROPPED: 31% of all tokens.

Global rule for every phase: one security incident or one false PASS fails the phase at once. N6 drops back one
stage, and the conveyor resumes the affected behaviour unchanged. A worse E8 measure means the stage does not
advance (matrix R33).

### Phase 1: shadow

What N6 does in this phase:

- N6 computes dispatch, accept/block, pause and quota decisions for real conveyor lanes.
- N6 acts on nothing and writes nothing outside its own KPI and notes rows.

Exit criteria (all required):

- Slices N6-01 to N6-07 are accepted.
- N6 has run in shadow over at least 15 consecutive conveyor slices (est.).
- (a) Agreement with the conveyor's final accept/block verdict is at least 95% (est.). Every disagreement has a
  written note.
- (b) Zero cases where N6 would PASS a slice that the conveyor BLOCKED or that failed after acceptance. This is the
  false-PASS proxy.
- (c) Zero Salih interventions caused by N6.
- (d) N6 shadow overhead is at most 10% of the quota per slice (est.).
- (e) Shadow adds at most 5% to time to finish (est.).
- KPI rows are complete for 100% of the observed slices.
- Zero security incidents.

### Phase 2: limited real tasks

Entry condition: slices N6-08 to N6-15 are accepted, and so are the limited-stage backlog slices:

- G15 outage/stall
- G16 breaker
- G18 paging
- G19, only if the API is used

That backlog is 3-4 slices (est.), slices 16-19. Limited does not start at slice 15.

What N6 does in this phase:

- It runs T1 low-risk lanes end to end, on worktree branches only.
- INT, push, deploy, cleanup and T2 lanes stay with the conveyor.

Exit criteria (all required):

- At least 10 N6-run T1 slices (est.).
- (a) Pass rate is at least the conveyor baseline for the same lane class.
- (b) Post-accept failures are at most the baseline, with zero false PASS.
- (c) Salih interventions per slice are at most the baseline.
- (d) Quota per accepted slice is at most the baseline: 1.26M for Sonnet T1 (measured, as the reference).
- (e) Time to finish is at most the baseline plus 10% (est.).
- Zero security incidents.
- The two-manager negative test (N6-08) is green on the run tree.
- The fallback switch has been used once: N6 hands a running slice back to the conveyor cleanly.

### Phase 3: independent acceptance

Judges: the orchestrator plus a second provider (ChatGPT or Codex). N6 does not judge itself. The judges run the
N6-07 report over the M9 fixed acceptance set. That set is hash-pinned and outside N6's write scope (R28).

Exit criteria (all required):

- M9: false PASS 0, security incidents 0, and the rollback drill passes.
- M6: all three scenarios pass. The KPI is accepted slices per quota plus post-accept failures.
- E8: (a)-(e) are each equal or better than the conveyor over the whole limited window.
- Salih signs the verdict.

### Phase 4: gradual takeover

Order: N6 takes over one behaviour group at a time.

- R17/R18: CI watch and auto-fix.
- R21: cleanup.
- R31: integration sequencing.
- R26: advisory reader.
- R10: presence and power.
- R19/R20: two-key push and ff/deploy/rollback, last.

Before acting, each group first runs in shadow on at least 5 real events (est.).

Exit criteria for each group:

- E8 (a)-(e) stay equal or better over each rolling 10-slice window (est.).
- Zero security incidents.
- The R34 fallback drill passes for that group.

Takeover is complete when every group has passed. Main push stays two-key for good, as Section 3 says.

## 2. First 15 slices, in order

Tier key:

- T2 slices get a separate-session Claude review plus a Codex review (two providers). Every state-machine slice is
  T2. The G3 same-provider exception does not apply to them.
- T1 slices get one independent review. The approved G3 separate-session same-provider exception is allowed.

Model per tier (LANECTX P2/P6):

- T1: Sonnet (est.).
- T2: Opus high.
- No T2 or security slice on the API credit (2026-11-18 plan).

All file lists are est.

Tests:

- Tests run only with `python -m unittest`.
- They use their own TEMP repo and DB and never open `data/jarvis.db`.
- Each test named NEG is a negative test that must fail before the fix and pass after it.

### Shadow block (N6-01 to N6-07)

N6-01: composition root and read-only sight

- Goal: wire the PM26A modules through `build_ports` into one N6 entry point that the app does not start. Give N6
  read-only sight (files, reports, events, git, CI) plus a free-text notes sink. There is no app write path.
- Files (est.): `jarvis/n6_root.py` (new), `jarvis/work_ports.py`, `jarvis/work_supervisor.py`,
  `tests/test_n6_root.py` (new).
- Tier: T2, because it is the root every later slice builds on.
- Reviews: Claude in a separate session, plus Codex.
- Tests:
  - A fake worker runs one slice in a TEMP git repo end to end.
  - NEG: any write port called in sight mode raises.
  - NEG: importing `jarvis.core` does not import `n6_root`.
- Closes: G01. Feeds R07 and R11.

N6-02: KPI rows

- Goal: per-slice rows for E8 (a)-(e) and security incidents, written for conveyor and N6 decisions alike.
- Files (est.): `jarvis/work_ledger.py`, `jarvis/n6_kpi.py` (new), `tests/test_n6_kpi.py` (new).
- Tier: T1. Additive table only, no ALTER; `SCHEMA_VERSION` stays 10.
- Reviews: one independent review (G3 exception allowed).
- Tests:
  - Round trip of one row.
  - A schema-10 reader opens the DB unchanged.
  - NEG: a row with a missing measure is refused, not filled with zero.
- Closes: G07, R32.

N6-03: shadow comparator

- Goal: read a read-only conveyor snapshot (state, reports, ledger), compute N6's decision for each lane, and record
  agree or disagree with a reason. Uses the orchbot agreement pattern.
- Files (est.): `jarvis/n6_shadow.py` (new), `tests/test_n6_shadow.py` (new), fixtures under
  `tests/fixtures/n6/` (new).
- Tier: T2 (state machine).
- Reviews: Claude in a separate session, plus Codex.
- Tests:
  - Fixture snapshots of 5 past lanes (est.) give the known verdicts.
  - NEG: a torn snapshot read raises.
  - NEG: report text alone never yields PASS (B2+).
  - NEG: the comparator never writes to `build/conveyor/`.
- Closes: G02. Shadow stage of R07, R11 and R24.

N6-04: quota window

- Goal: a pure function for the E6 weekly window (reset Thursday 22:00, 5% reserve, line 95 until the full_burn
  decision). Unknown usage means pause.
- Files (est.): `jarvis/work_controls.py`, `tests/test_n6_quota.py` (new).
- Tier: T1.
- Reviews: one independent review.
- Tests:
  - Clock tests across the Thursday 22:00 boundary.
  - Usage at 94 continues and 95 pauses.
  - NEG: unknown usage pauses.
  - NEG: budget continues only on an explicit False.
- Closes: G03, R01.

N6-05: usage reader

- Goal: parse the usage source into the quota function's input. Parsing is tested from fixtures only, with no live
  call.
- Files (est.): `jarvis/n6_usage.py` (new), `tests/test_n6_usage.py` (new), fixtures (new).
- Tier: T1.
- Reviews: one independent review.
- Tests:
  - Three fixture formats parse.
  - NEG: a malformed or stale file gives "unknown", not zero.
- Closes: G04, R02.

N6-06: sensors and preflight

- Goal: RAM, commit and disk readings go into the Situation object. Add the O64 deterministic preflight port: base
  sha, file ownership and prerequisite state are checked before a lane starts. This is LANECTX P1, the early block.
- Files (est.): `jarvis/work_supervisor.py`, `jarvis/n6_preflight.py` (new), `tests/test_n6_preflight.py` (new).
- Tier: T1.
- Reviews: one independent review.
- Tests:
  - A fake sensor below the floor holds the dispatch.
  - NEG: a wrong base sha blocks before launch.
  - NEG: a file already owned by another lane blocks.
- Closes: G05, G06, R09, R24.

N6-07: E8 report and pinned acceptance set

- Goal: a side-by-side E8 report built from the KPI rows, plus the M9 fixed acceptance set with a pinned hash. The
  report runs without any N6 code deciding the verdict. The set lives outside N6's write scope.
- Files (est.): `scripts/n6_e8_report.py` (new), `dev/n6/acceptance/` (new, pinned), `tests/test_n6_e8.py` (new).
- Tier: T2, because it is the gate itself.
- Reviews: Claude in a separate session, plus Codex. The orchestrator signs the pinned hash.
- Tests:
  - One false PASS in the input fails the report regardless of the other measures.
  - A worse measure means "do not advance".
  - NEG: an edited acceptance set fails the hash check.
- Closes: G21, R33.

Shadow exit: see Phase 1. N6 shadows the lanes that build N6-08 onward.

### Limited-prerequisite block (N6-08 to N6-15)

All eight slices are T2 state-machine work. Each gets a separate-session Claude review plus a Codex review and the
named NEG tests below. No G3 exception applies.

N6-08: lease and fencing (M8)

- Goal: a single owner lease with a fencing epoch (the `condition_watch` fencing_epoch pattern), idempotent events,
  and no new DB. The conveyor honours the lease.
- Files (est.): `jarvis/n6_lease.py` (new), `jarvis/work_driver.py`, `scripts/conveyor.py` (a lease check only),
  `tests/test_n6_lease.py` (new).
- Tests:
  - NEG: of two managers, exactly one wins and the other is fenced.
  - NEG: a stale epoch write is refused.
  - NEG: an event replayed twice applies once.
  - The conveyor skips a slice under a live N6 lease.
- Closes: G08, R08, R11.

N6-09: durable pause

- Goal: pause survives a restart, and PAUSE also holds approvals and pushes (CONVHOUSE2 lesson).
- Files (est.): `jarvis/work_controls.py`, `jarvis/work_supervisor.py`, `tests/test_n6_pause.py` (new).
- Tests:
  - NEG: a restart while paused stays paused.
  - NEG: an approval or push during PAUSE is held.
  - NEG: a corrupt pause file means paused.
- Closes: G12, R03.

N6-10: invocation record, receipt and NOT_RUN

- Goal: the B2 trusted invocation record is bound to the receipt. A required check that did not run is NOT_RUN,
  never PASS. A partial commit is not PASS (M2, M4).
- Files (est.): `jarvis/work_evidence.py`, `jarvis/work_checks.py`, `tests/test_n6_receipt.py` (new).
- Tests:
  - NEG: report text claiming PASS with no record is not PASS.
  - NEG: a partial commit is not PASS.
  - NEG: a missing check is NOT_RUN.
  - NEG: no checks at all is refused.
- Closes: G09, G11, R06.

N6-11: review routing (G3)

- Goal: route by tier. T2 and state-machine slices get two providers. The G3 exception is recorded and is only
  allowed for T1 non-state-machine slices. Unknown review severity blocks; there is no review skip.
- Files (est.): `jarvis/work_review.py`, `tests/test_n6_review_route.py` (new).
- Tests:
  - NEG: a state-machine slice with a same-provider review only is blocked.
  - NEG: unknown severity blocks.
  - NEG: an exception without a record is refused.
- Closes: G10, R05.

N6-12: boot scan and crash recovery

- Goal: on start, scan for half-done work (lease, worktree, receipt, outbox) and resume or hand back. Apply runs
  exactly once.
- Files (est.): `jarvis/n6_boot.py` (new), `jarvis/work_driver.py`, `tests/test_n6_boot.py` (new).
- Tests:
  - NEG: a crash between commit and receipt does not apply twice.
  - NEG: a crash mid-event commits the event and the outbox row together or not at all.
  - NEG: an unknown leftover is handed to the conveyor, not guessed.
- Closes: G14, R12, R14.

N6-13: root-cause gate and retry cap

- Goal: a fix lane needs a written ROOT CAUSE before a retry. A non-retriable StageError ends the slice. The O61
  retry cap applies (PM26X).
- Files (est.): `jarvis/work_driver.py`, `jarvis/work_regress.py`, `tests/test_n6_rootcause.py` (new).
- Tests:
  - NEG: a retry without a root cause is refused.
  - NEG: a non-retriable StageError is not retried.
  - NEG: the attempt after the cap blocks.
- Closes: G17, R23.

N6-14: cross-run backlog and INT push wait (B4)

- Goal: dependents of an INT run only after the push and the main SHA are verified. The backlog survives across runs.
- Files (est.): `jarvis/work_planner.py`, `jarvis/n6_deps.py` (new), `tests/test_n6_deps.py` (new).
- Tests:
  - NEG: a dependent with an unverified main SHA does not start.
  - NEG: a push reported but not verified on the remote holds.
  - A restart keeps the backlog order.
- Closes: G13, R04, R25.

N6-15: integration ref verify (B1/M1)

- Goal: a run_id plus ref integrity check before any acceptance. A torn read raises. Cleanup evidence is INT PICKS
  plus patch-id (M3).
- Files (est.): `jarvis/n6_refcheck.py` (new), `tests/test_n6_refcheck.py` (new).
- Tests:
  - NEG: a torn read raises.
  - NEG: a mismatched run_id is refused.
  - NEG: a ref moved between read and accept is refused.
- Closes: G20, R30.

### Backlog after slice 15 (est.)

These are needed before limited starts (slices 16-19, est.):

- N6-16: G15, outage hold plus the stall action with the nudge cap (R13, R15). T2.
- N6-17: G16, the cross-run breaker (R16). T2.
- N6-18: G18, the importance filter plus the phone sink (R22). T1.
- N6-19: G19, the B3 broker (R27), only if Salih keeps API use (Section 4, extra question X2). T2, security.

Write scope R28 and Core policy R29 come from N6-01 (no write path) and N6-07 (pinned set). An explicit write-scope
slice is added if the review of N6-01 finds a gap (est.).

Takeover slices after acceptance (est. one each, about 8): G22 CI watch, G23 CI auto-fix, G24 two-key push in a TEMP
remote, G25 ff/deploy/rollback rehearsal, G26 cleanup dry-run, G27 integration sequencing, G28 presence/power and
G29 fallback drill.

Total (est.): 15 + 4 + 8 = 27 slices, inside Salih's 20-35 range.

## 3. Conveyor behaviours that stay as fallback

The conveyor (`scripts/conveyor.py`) stays able to run the whole loop alone in every phase. Switching N6 off at any
point returns the system to the base-commit behaviour.

The conveyor alone keeps these until each one's takeover group passes in Phase 4:

- R17/R18: CI watch and CI auto-fix.
- R19: the two-key main push. The orchestrator approval file plus the deterministic checks.
- R20: ff, deploy and rollback, including the previous-version rollback.
- R21: worktree and Temp cleanup.
- R26: the advisory reader.
- R31: integration sequencing.
- R10: presence and power.
- R34: the fallback switch itself.

These stay with the conveyor forever, even after takeover:

- PAUSE as the master stop, above N6.
- The single-instance OS lock (CONVLOCK2).
- The fallback switch.
- The pager path to Salih's phone.
- Two-key push.

N6 may propose a change to any of them. Salih or the orchestrator applies it.

During limited, the conveyor keeps all T2, INT, push, deploy and cleanup lanes. N6 holds a lease only on the T1
lanes it runs (N6-08). When N6 fails or hands a slice back, the conveyor picks that slice up.

## 4. The nine Salih decisions (matrix 3.2)

Each decision is a short question with a recommended answer.

O7: when N6 sees a BLOCKED for a "do not stop" reason (a rule miss), may it tighten the rule itself?

- Recommended: no. N6 writes the proposed rule change and a notebook entry. The orchestrator or Salih applies it in
  the same round. N6 cannot change Core policy (V1 frame).

O41: is N6 the first concrete step of the main goal ("work through sub-agents; the conveyor moves to JARVIS")?

- Recommended: yes. Count N6 as V2.4 under the V2 plan. Every slice answers "does this bring us closer?" in its
  goal line.

O43: in N6, against which rival do we refuse to fall behind?

- Recommended: Hermes Agent's unattended task loop. Add one comparison row (est. one evening) at the Phase 3 gate.
  It is evidence, not a gate.

O44: should N6 serve only code, or every job?

- Recommended: code first, because it is the slice we can measure (E8). Keep the ports generic (`work_ports`), so a
  non-code job type can be added after takeover without a redesign.

O46: where does N6 talk to Salih?

- Recommended: the always-on-top ring for status and push-to-talk, and Telegram on the phone for paging (N6-18). The
  big window only shows the plan, diff and approval detail. N6 adds no polish to the big window.

O49: which limit does N6 remove, and which does it keep?

- Recommended: it removes the "Salih relays every lane" limit. It keeps the authority limit (money, irreversible
  actions, outbound sending, main push) as a fixed code gate that N6 cannot edit (N6-07 pinned set, R28/R29). It
  makes no "flawless" claim; it catches errors before they reach Salih.

O51: is E8 a harness comparison or a model comparison?

- Recommended: a harness comparison. It uses the same model as the orchestrator and compares N6 with conveyor plus
  orchestrator on the same slices. HARNESS_KARNESI.md may be cited but is not a dependency.

O57: when is the third opinion (ChatGPT, Codex if needed) told?

- Recommended: one Turkish delta block at each phase gate (shadow exit, limited exit, acceptance), and after any
  security or false-PASS stop. The weekly one stays after the Thursday 22:00 reset.

O63: what replaces the v5.3 stop-loss (about 20 slices, 1.5 conveyor days, 15% quota) for N6?

- Recommended: keep the 15% weekly quota line per N6 week (est.). Replace the count and day caps with the quality
  stop rule in Section 5, because Salih set quality over time.

Extra open questions found while writing this kit:

- X1 (G03): full_burn, or keep the line at 95? Recommended: keep 95 until N6-04 and N6-05 have run a full week in
  shadow, then decide on data.
- X2 (G19): will N6 use the API credit ($100, expires 2026-11-18)? Recommended: no for N6. The credit expires before
  limited can start, and T2 and security work is not allowed on the API, so N6-19 (B3) is skipped.
- X3 (O60): T1 on Sonnet, given n=13 measured? Recommended: yes for N6 T1 slices, and record pass rate per lane class
  in the KPI rows (N6-02). Go back to Opus if Sonnet falls below the Opus rate over 10 slices (est.).

## 5. Risks and the stop rule

Risks, each with its control:

- State-machine bugs are systematic, not random. Control: shadow first, named NEG tests in every state-machine slice,
  and review by two providers (N6-03, N6-08 to N6-17).
- Split brain, where N6 and the conveyor both act on one slice. Control: the N6-08 lease with fencing, the conveyor
  honouring the lease, and the two-manager NEG test.
- Self-modification, where N6 changes its own running code or its own gate. Control: N6 only proposes. The pinned
  M9 set and Core policy sit outside N6's write scope. Main push stays two-key. The previous-version rollback is
  kept.
- Report text gaining authority. Control: B2+, so only the invocation record counts (N6-10). Chat is a channel, not
  authority (M7).
- Cost of failed lanes: BLOCKED plus DROPPED was 31% of tokens last week (measured). Control: the N6-06 preflight
  (early block) and a stop at the first BLOCKED cause.
- Quota exhaustion mid-slice. Control: the N6-04 window with the 5% reserve, and unknown usage pausing.
- Review load: two-provider review on 12 of 15 slices (est.) costs about 10M tokens per week (est., from LANECTX P6).
  Accepted as the price of state-machine safety.
- Small samples: Sonnet n=13 (measured). Control: KPI rows per lane class, and the X3 fallback to Opus.
- Drift in N6's free-text reasoning. Control: the notes are read-only evidence, and every action still passes the
  fixed code gates.

Stop rule (there is no elapsed-time trigger, because Salih set quality over time).

Stop N6 at once, hand all lanes to the conveyor and tell Salih when any of these happens:

- one security incident
- one false PASS (an N6 PASS that the conveyor or a post-accept check shows was wrong)
- a two-manager event, meaning both acted on one slice
- an acceptance-set hash mismatch

After a stop, N6 restarts only from shadow, and only after a root-cause note and Salih's go.

Pause N6, and let the conveyor continue, when any of these happens:

- weekly quota reaches the line (95 until X1 is decided) or the 5% reserve
- 3 consecutive N6 BLOCKED results (est.)
- the 15% weekly N6 quota cap (O63 recommendation, est.)

Do not advance a stage when any E8 measure is worse than the conveyor's. N6 stays in its current stage and the
conveyor is unchanged (matrix R33).
