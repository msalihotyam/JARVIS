import QtQuick

// One deterministic return destination. The active presentation package
// stays unchanged; neither this X nor Escape closes the application.
GlassPanel {
    closable: true
    function closePage() { bridge.setPage("home") }
    onCloseRequested: closePage()
}
