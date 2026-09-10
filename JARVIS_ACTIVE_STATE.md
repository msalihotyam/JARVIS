# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** Gmail minimum usable-v1 — COMPLETED and integrated
  (`37404e0`). Six capabilities in the closed vocabulary: `gmail_thread_list`,
  `gmail_search`, `gmail_message_read`, `gmail_thread_read`,
  `gmail_draft_create`, `gmail_send`. `gmail_attachment_read` is deferred
  post-v1; destructive, bulk, reply, forward and mailbox-monitoring features
  stay out of usable-v1 scope — those names do not exist in the vocabulary, so
  no task can ask for them. Send requires exact approval, bound to the exact
  RFC 2822 message bytes. A write whose outcome is uncertain after the child
  started settles as `UNKNOWN_OUTCOME` and is never replayed. Gmail uses the
  dedicated trusted-local `gws` credential boundary established during this
  milestone (controlled `gws auth login` → authorized-user attestation → that
  exact credential instance held for the whole operation → JARVIS's own steps
  serialized by one cooperative lock). Confidential message payload never
  enters process argv; a confidential transport cleanup failure is surfaced
  and blocks later writes until it is cleared. Final full product suite on the
  integrated tree: **3593 tests, 0 failed, 0 errors, 1 skipped**.
- **Dashboard:** 63% (Gmail capability 75%, GitHub 60%, Scheduler 92%), as
  computed by the renderer from `dev/dashboard/status.json`.
- **Latest canonical main** is `37404e0` and includes the GitHub READ commit
  `5007c15`, the GitHub security correction `2301b53` → `83b701a`, dev-helper
  S1 `b716905` and S2 `17589d2`, and the Gmail minimum series
  `d67c58a` → `ff8c94c` → `55b363f` → `e069369` → `37404e0`. Housekeeping
  commits after those do not change the product state recorded here.
- **Next checkpoint (not a product milestone):** Claude development tooling —
  Agent Skills + Ponytail, strict 10–15 minute scope. No product code, no
  product tests, no roadmap weight, so it does not move the dashboard
  percentage.
- **Next implementation (product):** Browser basic usable-v1 (design not
  started). GitHub writes, `github_code_search` and `gmail_attachment_read`
  stay out of scope.
- **Contracts:** SQLite schema 10; output envelope 7. Gmail minimum changed
  neither — it reuses the same bounded `external_read` block with the
  connector named `gmail-cli`.
- **Active implementation worktree:** `C:\Users\realk\Masaüstü\JARVIS-Claude-gmail-minimum`
  (`claude/gmail-minimum`, accepted and integrated) is still present and awaits
  the normal `finish-current` / `cleanup` sequence. This persistent chat is
  already rebound to canonical `C:\Users\realk\Masaüstü\JARVIS`.
  `dev.ps1 status` still reports the old `claude/scheduler-phase1b` recovery
  residue (PARTIAL REMOVAL — RECOVERY REQUIRED); it is left untouched on
  purpose and needs an explicit human recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih. Nothing has been
  pushed; canonical main is ahead of `origin/main`.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
