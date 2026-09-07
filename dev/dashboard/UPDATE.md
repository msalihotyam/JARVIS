# Updating the JARVIS Development Dashboard

Dev tooling only. No LLM, no network, no dependencies.

## When to update

Only when one of these changes:

- overall progress
- active milestone
- next step
- capability status
- blocker status
- a major completion
- a schema / system-health headline

Normal bugfixes and test reruns do **not** require a dashboard update.

## How

1. Edit only the relevant fields in `status.json`. Keep summaries to one short sentence.
2. Run:

   ```
   python dev/dashboard/render_dashboard.py
   ```

3. Open `dashboard.html` and check the fields you changed.
4. Do not rewrite historical sections (`recent`, completed capabilities) unless they became wrong.

`status.json` is the source of truth. `dashboard.html` is generated; never edit it by hand.

## Git-derived fields

Canonical HEAD, clean/dirty and ahead/behind origin/main are derived by the
renderer from local git at render time (no fetch, no network). Do not put them
in `status.json`. If git cannot answer, the dashboard shows `unknown`.

The generated `dashboard.html` is excluded from the dirty count, since the
renderer writes it on every run. It is a build artifact: re-run the renderer
after a commit to refresh HEAD and tree state.
