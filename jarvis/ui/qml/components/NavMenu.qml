import QtQuick
import "../theme"

ContextPanel {
    id: nav
    heading: qsTr("NAVIGATE")
    surfaceName: "navSurface"
    panelWidth: 300; panelHeight: 326
    function go(page) { bridge.setPage(page); nav.open = false }
    Column {
        width: parent.width; spacing: 4
        Repeater {
            model: [
                { page: "home", label: qsTr("Command Center"), key: "1", glyph: "⌂" },
                { page: "tasks", label: qsTr("Tasks"), key: "2", glyph: "◇" },
                { page: "automations", label: qsTr("Automations"), key: "3", glyph: "⟳" },
                { page: "memory", label: qsTr("Memory"), key: "4", glyph: "▤" },
                { page: "history", label: qsTr("History"), key: "5", glyph: "◷" }
            ]
            Rectangle {
                id: entry
                objectName: "navigate_" + modelData.page
                width: parent.width; height: 42; radius: 6
                readonly property bool current: bridge.page === modelData.page
                color: current ? Theme.alpha(Theme.accent, 0.10) : hover.containsMouse ? Theme.alpha(Theme.text, 0.04) : "transparent"
                border.width: 1; border.color: current || activeFocus ? Theme.borderStrong : "transparent"
                activeFocusOnTab: true; Accessible.role: Accessible.Button; Accessible.name: modelData.label
                Keys.onReturnPressed: nav.go(modelData.page)
                Keys.onSpacePressed: nav.go(modelData.page)
                Text { x: 10; anchors.verticalCenter: parent.verticalCenter; text: modelData.glyph; color: Theme.accent; font.pixelSize: 18 }
                Text { x: 42; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: entry.current ? Theme.text : Theme.textLabel; font { family: Theme.font; pixelSize: 14 } }
                Text { anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; text: "Ctrl+" + modelData.key; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: nav.go(modelData.page) }
            }
        }
    }
}
