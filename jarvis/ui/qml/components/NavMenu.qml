import QtQuick
import "../theme"

// The compact expandable navigation. Closed it is nothing; open it is a
// slim floating list under the brand, and it closes on any choice or on
// Escape / click outside. Settings is a drawer, not a page.
Item {
    id: nav
    property bool open: false
    signal settingsRequested()
    anchors.fill: parent
    visible: open || panel.opacity > 0
    z: 200

    function go(page) { bridge.setPage(page); nav.open = false }

    MouseArea { anchors.fill: parent; enabled: nav.open; onClicked: nav.open = false }

    GlassPanel {
        id: panel
        raised: true
        x: 28; y: 94
        width: 268; height: column.height + 32
        opacity: nav.open ? 1 : 0
        transform: Translate { y: nav.open ? 0 : -8; Behavior on y { NumberAnimation { duration: Theme.panelIn; easing.type: Easing.OutCubic } } }
        Behavior on opacity { NumberAnimation { duration: nav.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } }
        padding: 16
        Column {
            id: column
            width: parent.width
            spacing: 2
            Text { text: "NAVIGATE"; color: Theme.textDim; font { family: Theme.font; pixelSize: 9; letterSpacing: 2.6; weight: Font.DemiBold } bottomPadding: 8; leftPadding: 10 }
            Repeater {
                model: [
                    { page: "home", label: "Command Center", key: "1", glyph: "◈" },
                    { page: "tasks", label: "Tasks", key: "2", glyph: "◇" },
                    { page: "automations", label: "Automations", key: "3", glyph: "⟳" },
                    { page: "memory", label: "Memory", key: "4", glyph: "▤" },
                    { page: "history", label: "History", key: "5", glyph: "◷" },
                    { page: "settings", label: "Settings", key: "6", glyph: "◎" },
                ]
                Rectangle {
                    width: column.width; height: 40; radius: Theme.radiusSmall
                    property bool current: bridge.page === modelData.page
                    color: current ? Theme.alpha(Theme.accent, 0.14) : (hover.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent")
                    border.width: 1; border.color: current ? Theme.alpha(Theme.accent, 0.45) : "transparent"
                    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                    Text { x: 12; anchors.verticalCenter: parent.verticalCenter; text: modelData.glyph; color: current ? Theme.accent : Theme.textMuted; font.pixelSize: 14 }
                    Text { x: 40; anchors.verticalCenter: parent.verticalCenter
                        text: modelData.label; color: current ? Theme.accent : (hover.containsMouse ? Theme.text : Theme.textLabel); font { family: Theme.font; pixelSize: 14 } }
                    Text { anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
                        text: "Ctrl+" + modelData.key; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                    MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: { if (modelData.page === "settings") { nav.open = false; nav.settingsRequested() } else nav.go(modelData.page) } }
                }
            }
        }
    }
}
