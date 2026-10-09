import QtQuick
import "../theme"

// One deterministic return destination. The active presentation package
// stays unchanged; neither this X nor Escape closes the application.
GlassPanel {
    id: secondaryPage
    closable: true
    titleFont: Theme.pageTitle
    headerHeight: 36
    function closePage() { bridge.setPage("home") }
    // A stored ISO timestamp as a list shows it ("dün 22:30", "18 Eyl 22:30"),
    // through the one formatter; a value that is not a date is shown as it is, never invented.
    // A slow clock while shown, so "today" turns into "yesterday" at midnight.
    property real now: Date.now()
    Timer { interval: 60000; running: secondaryPage.visible; repeat: true; triggeredOnStart: true; onTriggered: secondaryPage.now = Date.now() }
    function when(v) { return !v ? "" : i18n.when(v, now, "time") || String(v) }
    onCloseRequested: closePage()
}
