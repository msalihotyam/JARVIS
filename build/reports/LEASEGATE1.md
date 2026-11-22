STATUS: PASS
BRANCH: worktree-LEASEGATE1
PARENT: c450cc28d5affdc42baef30dd71f797801afa601
COMMIT: see git log -1 on the branch (message "test(conveyor): LEASEGATE1 two conveyors never dispatch at once")
VERDICT: startup already gated, per-launch re-check ADDED
  - Startup: CONVLOCK2 OS lock on conveyor.instance (single owner for the process life); conveyor.lock is info/heartbeat only, STALE_LOCK_S is for readers. jarvis/n6_lease.py only holds slice ids, not conveyor ownership. N6 lease semantics unchanged.
  - Gap: start/start_codex/start_jules did not re-check ownership right before launching.
  - Fix: Conveyor.not_owner(name) (own_lock() false = conveyor.lock names another pid) at the top of start, start_codex, start_jules; returns None, event dispatch_not_owner, lane not failed, prompt stays queued.
FILES: scripts/conveyor.py, tests/test_conveyor.py, build/reports/LEASEGATE1.md
TESTS (tests/test_conveyor.py TestLock):
  test_leasegate1_second_conveyor_with_a_live_owner_never_dispatches
  test_leasegate1_stale_heartbeat_is_not_a_takeover_only_a_free_os_lock_is
  test_leasegate1_ownership_is_rechecked_right_before_each_launch
  Negative check: with not_owner forced False all 3 fail; with the guard all pass.
RUNS: python scripts/test_parallel.py tests.test_conveyor tests.test_n6_lease: 922 tests, failures=0, errors=0, rc=0
git diff --check: clean (CRLF warning only)
NOT DONE: no instance_fd-is-held check (existing fixtures never acquire the lock); tests/test_n6_lease.py untouched.
