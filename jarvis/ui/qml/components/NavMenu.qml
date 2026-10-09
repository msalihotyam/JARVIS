import QtQuick
import "../theme"

// The navigation menu (Ctrl+M or the top bar's grid). K4: the one menu surface
// and row - icon, page name, its Ctrl+N shortcut; the current page marked.
// `open` is the window's state (one context at a time); the surface follows
// it and reports its own closing (Esc, a press outside) back.
Item {
    id: nav
    property bool open: false
    property point anchorPoint: Qt.point(24, 88)
    property Item trigger: null
    readonly property real panelWidth: 300
    function go(page) { bridge.setPage(page); nav.open = false }
    onOpenChanged: if (!open) surface.close()
                   else surface.popupAt(nav, Qt.point(anchorPoint.x, Math.max(80, anchorPoint.y)), { trigger: trigger })

    MenuSurface {
        id: surface
        objectName: "navMenu"
        surfaceName: "navSurface"
        width: nav.panelWidth
        onAboutToHide: nav.open = false              // at once, not after the exit motion
        Column {
            width: surface.availableWidth
            spacing: 2
            Text { text: qsTr("NAVIGATE"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; topPadding: 4; bottomPadding: 6 }
            Repeater {
                model: [
                    { page: "home", label: qsTr("Command Center"), key: "1", icon: "home" },
                    { page: "tasks", label: qsTr("Tasks"), key: "2", icon: "tasks" },
                    { page: "automations", label: qsTr("Automations"), key: "3", icon: "automations" },
                    { page: "memory", label: qsTr("Memory"), key: "4", icon: "memory" },
                    { page: "history", label: qsTr("History"), key: "5", icon: "history" },
                    { page: "projects", label: qsTr("Projects"), key: "6", icon: "project" }
                ]
                MenuRow {
                    objectName: "navigate_" + modelData.page
                    icon: modelData.icon; label: modelData.label; shortcut: "Ctrl+" + modelData.key
                    current: bridge.page === modelData.page
                    onTriggered: nav.go(modelData.page)
                }
            }
        }
    }
}
