# Background candidates (temporary, development only)

Four full-screen background candidates under audition. Not production assets:
nothing here is loaded unless the interface runs with
`JARVIS_UI_BACKGROUND_AUDITION=1`, and then only by the audition shortcuts.

    JARVIS_UI_BACKGROUND_AUDITION=1 python -m jarvis.ui

- Ctrl+0 - the current production background
- Ctrl+1..4 - candidate-1.webp .. candidate-4.webp, in the order supplied

Delete this folder, the audition shortcuts in Main.qml and
`Bridge.devBackgroundCandidates` once a winner is chosen.
