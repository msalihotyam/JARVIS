# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** Browser basic usable-v1 — COMPLETED and integrated
  (`e3d5d14`); the accepted task line ended at task commit `5ceb270`. Nine
  capabilities in the closed vocabulary: `browser_navigate`,
  `browser_page_read`, `browser_click`, `browser_type`, `browser_wait`,
  `browser_tab_list`, `browser_tab_open`, `browser_tab_switch`,
  `browser_tab_close`. `browser_script_evaluate`, `browser_download`,
  `browser_upload`, `browser_screenshot` and `browser_cookie_read` are refused
  BY NAME, so no task can ask for them. The backend is the local Microsoft
  Edge driven over the DevTools protocol with the standard library only — the
  project keeps `dependencies = []`, and Playwright/Selenium were not added.
  A target is never a model-authored selector: it is a `ref` a previous
  reading printed plus that reading's digest, so a page that moved fails
  closed. Cross-origin redirect authority is ENFORCEMENT, not detection: every
  top-level Document request is paused before it is sent, the first hop
  leaving the approved origin is refused with `BlockedByClient`, the gate
  stays armed until `Page.frameNavigated` correlated on frameId AND loaderId,
  and `Fetch.disable` is never sent while a paused request lacks an
  acknowledged disposition — that tab is destroyed with `Target.closeTarget`
  instead. Field values are never rendered (the accessibility tree reports a
  password input as an ordinary textbox) and credential-shaped text is never
  typed. Targeted Browser suite: **79 tests, 0 failed, 0 errors**. Final
  independent gate: PASS.
- **Live validation:** the teardown claim was measured on real Microsoft Edge
  **152.0.4191.66**, once. Control run: `Fetch.disable` really did send the
  paused request to the destination server. Claim run: after
  `Target.closeTarget` the destination server received **zero** requests. No
  Edge process, tab, server or temporary file was left behind.
- **Dashboard:** 67% (Browser capability 75%, Gmail 75%, GitHub 60%,
  Scheduler 92%), as computed by the renderer from `dev/dashboard/status.json`.
- **Latest canonical main** is `e3d5d14` and includes the Gmail minimum series
  through `37404e0`, the dev-tooling `jarvis-lifecycle` skill `c41d898`, and
  the Browser basic series `f979fd7` → `e3908c0` → `f4685d5` → `bcfe67e` →
  `e3d5d14`. Housekeeping commits after those do not change the product state
  recorded here.
- **Next implementation (product):** Windows Desktop basic usable-v1 (design
  not started). GitHub writes, `github_code_search`, `gmail_attachment_read`
  and Browser end-to-end hardening stay out of scope.
- **Contracts:** SQLite schema 10; output envelope 7. Browser basic changed
  neither — it reuses the same bounded `external_read` block with the
  connector named `browser-cdp`.
- **Test coverage note:** the last FULL canonical product-suite run is still
  the Gmail one (3593 tests). Browser basic ran the full suite once on its own
  task tree (3648 tests, 0 failed, 1 skipped) because shared Core contracts
  changed there; its later corrections touched no shared contract and were
  verified by the targeted suite only, per the runbook.
- **Active implementation worktree:** none. The Browser worktree
  `JARVIS-Claude-browser-minimum` and its branch `claude/browser-minimum` were
  finished and cleaned up after integration; this persistent chat is rebound
  to canonical `C:\Users\realk\Masaüstü\JARVIS`. `dev.ps1 status` still
  reports the old `claude/scheduler-phase1b` recovery residue (PARTIAL
  REMOVAL — RECOVERY REQUIRED); it is left untouched on purpose and needs an
  explicit human recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih. Nothing has been
  pushed; canonical main is ahead of `origin/main`.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
