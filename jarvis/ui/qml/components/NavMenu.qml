import QtQuick
import "../theme"

// The compact expandable navigation. Closed it is nothing; open it is a
// slim floating list at the top-left, and it closes on any choice or on
// Escape / click outside.
Item {
    id: nav
    property bool open: false
    anchors.fill: parent
    visible: open || panel.opacity > 0
    z: 200

    function go(page) { bridge.setPage(page); nav.open = false }

    MouseArea { anchors.fill: parent; enabled: nav.open; onClicked: nav.open = false }

    GlassPanel {
        id: panel
        raised: true
        x: 28; y: 92
        width: 240; height: column.height + 28
        opacity: nav.open ? 1 : 0
        transform: Translate { y: nav.open ? 0 : -8; Behavior on y { NumberAnimation { duration: Theme.panelIn; easing.type: Easing.OutCubic } } }
        Behavior on opacity { NumberAnimation { duration: nav.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } }
        padding: 14
        Column {
            id: column
            width: parent.width
            spacing: 2
            Repeater {
                model: [
                    { page: "home", label: "Command Center", key: "1" },
                    { page: "tasks", label: "Tasks", key: "2" },
                    { page: "automations", label: "Automations", key: "3" },
                    { page: "memory", label: "Memory", key: "4" },
                    { page: "history", label: "History", key: "5" },
                    { page: "settings", label: "Settings", key: "6" },
                ]
                Rectangle {
                    width: column.width; height: 36; radius: Theme.radiusSmall
                    property bool current: bridge.page === modelData.page
                    color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.16) : (hover.containsMouse ? Qt.rgba(1, 1, 1, 0.05) : "transparent")
                    Text { anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter
                        text: modelData.label; color: current ? Theme.accent : Theme.text; font { family: Theme.font; pixelSize: 14 } }
                    Text { anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
                        text: "Ctrl+" + modelData.key; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                    MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: nav.go(modelData.page) }
                }
            }
        }
    }
}
