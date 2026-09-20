import QtQuick
import "../theme"

// One deterministic return destination. The active presentation package
// stays unchanged; neither this X nor Escape closes the application.
GlassPanel {
    closable: true
    titleFont: Theme.pageTitle
    headerHeight: 36
    function closePage() { bridge.setPage("home") }
    // A stored ISO timestamp as a list shows it ("18 Eyl 22:30"); a value that
    // is not a date is shown as it is, never invented.
    function when(v) {
        if (!v) return ""
        var d = new Date(v)
        return isNaN(d.getTime()) ? String(v) : d.toLocaleString(Qt.locale(i18n.qtLocale), "d MMM HH:mm")
    }
    onCloseRequested: closePage()
}
