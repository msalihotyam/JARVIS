# JARVIS Active State

Short, current-state snapshot. Keep this file small and update it whenever
the state below actually changes — see `JARVIS_RUNBOOK.md` for the rules this
state is tracked under.

- **Current work:** Natural-Language Intent Understanding — COMPLETED and
  integrated (`d38c3b1`); the accepted task line ended at task commit
  `aeeeb56`, and the integrated tree is byte-identical to it.
  `JarvisApp.submit_text(...)` (CLI `jarvis text`) is the app-level
  natural-language entry seam. The intent model's output has ZERO authority:
  it is an untrusted proposal. Capability and destination are validated
  deterministically — a capability is offered only when a registered manifest
  serves it AND an exact `DestinationGrant` row exists, a destination only
  from that capability's grant rows — and the proposal becomes an ordinary
  task document judged by `decode_task_json`. With several authorized
  destinations neither the model nor a mention inside a sentence selects one:
  only a follow-up reply exactly equal to a backend-offered `intent.choices`
  label does. The existing Core / approval / execution path stays
  authoritative; understanding creates no grant, skips no approval, runs no
  connector and never produces UNKNOWN_OUTCOME. The understanding call reuses
  the hardened Claude Code CLI seam (subscription route only) and is never
  registered as a connector. Targeted intent suite: **59 tests, green**.
  Gemini gate: PASS. Final GPT-5.6 Sol narrow gate: PASS.
- **Previous closure:** Windows Desktop basic usable-v1 (`ca8f780`, task
  commit `2f8ed55`). Six capabilities in the closed
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
  review: PASS. Codex final independent gate: PASS. Before it: Browser basic
  usable-v1 (`e3d5d14`, task commit `5ceb270`).
- **Live validation (Intent):** on the task tree, once, through the real
  Claude Code CLI adapter against a temporary database: "Bu projeyi incele."
  became a `code_analysis` task parked at `awaiting_approval` (not approved,
  nothing executed); "Son maillerimi kontrol et." was reported
  `unsupported_capability` because no Gmail is configured. With
  `ANTHROPIC_BASE_URL` present the adapter correctly refused as
  `model_unavailable` before any process started.
- **Live validation (Desktop):** on real Windows, once per property: the UIA
  vtable slots were cross-checked against Win32's own answers (winver), and
  target-bound dispatch was shown with an unrelated app in front and covering
  the target — charmap's field received the text while winver covered it, and
  winver's OK button was invoked while charmap covered it and stayed in
  front. Nothing launched was left running.
- **Dashboard:** 71% (Desktop capability 75%, Browser 75%, Gmail 75%, GitHub
  60%, Scheduler 92%), as computed by the renderer from
  `dev/dashboard/status.json`.
- **Latest canonical main** is `d38c3b1` and includes the Windows Desktop
  basic series `62b669b` → `47af4e2` → `ca8f780` and the Natural-Language
  Intent Understanding series `12f7429` → `4fce6d8` → `d38c3b1`.
  Housekeeping commits after those do not change the product state recorded
  here.
- **Next implementation (product):** the high-end PySide6/QML Minimal UI,
  built on `JarvisApp.submit_text(...)` for natural-language input. Desktop
  `desktop_key`, packaged/UWP apps, checkbox/radio/selection patterns and
  Desktop/Browser end-to-end hardening stay out of scope.
- **Contracts:** SQLite schema 10; output envelope 7. Neither Windows Desktop
  basic nor Intent Understanding changed them — Desktop reuses the bounded
  `external_read` block (`desktop-uia`), and the new `text` command envelope
  carries the unchanged v7 run envelope under `run`.
- **Test coverage note:** the last FULL canonical product-suite run is still
  the Gmail one (3593 tests). Intent Understanding ran the full suite once on
  its own task tree (exit 0) because the shared application surface changed;
  its two later corrections were intent-local and verified by the targeted
  intent suite only (59 tests), per the runbook. Windows Desktop basic did the
  same before it (3713 tests OK, 1 skipped; later corrections verified by 48
  targeted tests).
- **Active implementation worktree:** the separate Minimal UI task worktree
  `JARVIS-Claude-minimal-ui` (`claude/minimal-ui`, `430c5fd`, tkinter
  functional prototype) still exists and is ACTIVE in `dev.ps1 status`; it
  was not touched. The intent worktree `JARVIS-Claude-intent-understanding`
  and its branch `claude/intent-understanding` were finished and cleaned up
  after integration; this persistent chat is rebound to canonical
  `C:\Users\realk\Masaüstü\JARVIS`. `dev.ps1 status` still
  reports the old `claude/scheduler-phase1b` recovery residue (PARTIAL
  REMOVAL — RECOVERY REQUIRED); it is left untouched on purpose and needs an
  explicit human recovery checkpoint.
- **Push:** forbidden unless explicitly authorized by Salih. Nothing has been
  pushed; canonical main is ahead of `origin/main`.
- **Before any worktree lifecycle operation:** the persistent Scheduler
  Development chat must be on canonical JARVIS
  (`C:\Users\realk\Masaüstü\JARVIS`).
