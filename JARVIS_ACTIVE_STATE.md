# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** Windows Desktop basic usable-v1 — COMPLETED and integrated
  (`ca8f780`); the accepted task line ended at task commit `2f8ed55`, and the
  integrated tree is byte-identical to it. Six capabilities in the closed
  vocabulary: `desktop_app_launch`, `desktop_window_list`,
  `desktop_window_inspect`, `desktop_window_focus`, `desktop_click`,
  `desktop_type`. `desktop_key`, `desktop_screenshot`,
  `desktop_clipboard_read`, `desktop_clipboard_write`, `desktop_window_close`,
  `desktop_window_move`, `desktop_drag`, `desktop_field_value_read` and
  `desktop_command_run` are refused BY NAME. The backend is standard-library
  `ctypes` over user32/kernel32 and Windows' own UI Automation client — the
  project keeps `dependencies = []`. Authority is configuration: each
  `desktop_apps` entry (`desktop/<canonical exe path>`) becomes exact
  `DestinationGrant` rows; shells, terminals, script hosts, explorer and the
  UWP frame host are refused by the grammar. A window is hwnd + pid + process
  creation time; a target is a `ref` an inspection printed plus that
  inspection's digest. Actions are TARGET-BOUND, never global input: a click
  is `InvokePattern.Invoke` and typing is `ValuePattern.SetValue`, called on
  the pattern object of the element just re-inspected and digest-verified;
  the global `SendInput` path was removed (Gemini finding). After the pattern
  is acquired and before dispatch, window ownership (hwnd, pid, creation
  time, image, class) is re-proven, so HWND/process reuse is a definite
  pre-dispatch refusal (Codex final-gate finding). Password fields are never
  written, field values are never read, credential-shaped text is never
  typed; an uncertain post-dispatch outcome is UNKNOWN_OUTCOME and is never
  replayed. Targeted Desktop suite: **48 tests, green**. Gemini independent
  review: PASS. Codex final independent gate: PASS.
- **Previous closure:** Browser basic usable-v1 (`e3d5d14`, task commit
  `5ceb270`) — nine capabilities, local Edge over DevTools, origin-gated
  navigation; targeted Browser suite 79 tests; teardown claim measured on real
  Edge 152.0.4191.66.
- **Live validation (Desktop):** on real Windows, once per property: the UIA
  vtable slots were cross-checked against Win32's own answers (winver), and
  target-bound dispatch was shown with an unrelated app in front and covering
  the target — charmap's field received the text while winver covered it, and
  winver's OK button was invoked while charmap covered it and stayed in
  front. Nothing launched was left running.
- **Dashboard:** 71% (Desktop capability 75%, Browser 75%, Gmail 75%, GitHub
  60%, Scheduler 92%), as computed by the renderer from
  `dev/dashboard/status.json`.
- **Latest canonical main** is `ca8f780` and includes the Browser basic series
  through `e3d5d14` and the Windows Desktop basic series `62b669b` →
  `47af4e2` → `ca8f780`. Housekeeping commits after those do not change the
  product state recorded here.
- **Next implementation (product):** Minimal UI (design not started). Desktop
  `desktop_key`, packaged/UWP apps, checkbox/radio/selection patterns and
  Desktop/Browser end-to-end hardening stay out of scope.
- **Contracts:** SQLite schema 10; output envelope 7. Windows Desktop basic
  changed neither — it reuses the same bounded `external_read` block with the
  connector named `desktop-uia`.
- **Test coverage note:** the last FULL canonical product-suite run is still
  the Gmail one (3593 tests). Windows Desktop basic ran the full suite once on
  its own task tree (3713 tests OK, 1 skipped) because shared Core contracts
  changed there; its two later corrections were Desktop-local and verified by
  the targeted suite only, per the runbook.
- **Active implementation worktree:** none. The Desktop worktree
  `JARVIS-Claude-desktop-minimum` and its branch `claude/desktop-minimum` were
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
