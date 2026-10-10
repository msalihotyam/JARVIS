import QtQuick
import QtQuick.Shapes
import "../theme"

// Gate 6G R3U: the top bar's bell. Its badge counts bridge.unreadBell - the
// conversations that wait for the person (needsYouIds, amber) or finished
// while another one was open (R3's green, or marked unread from a sidebar
// row's menu), each once, never the open one. Amber badge while anything
// waits for the person, else green; no badge when nothing is there. A click
// opens a K4 menu of them, newest first (title, project, the state in words,
// K3); an entry opens its conversation, which clears its green. Empty: the
// honest "Nothing waiting" line. No motion of its own (K2).
Item {
    id: bell
    readonly property var entries: bridge.unreadBell
    readonly property int count: entries.length
    readonly property bool waiting: entries.some(function (entry) { return entry.state === "needsYou" })
    readonly property bool open: menu.visible
    width: 38; height: 38
    Accessible.role: Accessible.Button
    Accessible.name: button.tip

    IconButton {
        id: button
        objectName: "unreadBellButton"
        anchors.fill: parent
        active: bell.open
        tip: bell.count > 0 ? qsTr("Waiting: %1").arg(bell.count) : qsTr("Nothing waiting")
        onClicked: if (bell.open) menu.close()
                   else menu.popupAt(button, Qt.point(0, button.height + 8), { trigger: button, flip: true })
        // K1: drawn on the icon set's 18-unit grid, 1.6 stroke, round caps (icons.js has no bell yet).
        Shape {
            objectName: "unreadBellIcon"
            anchors.centerIn: parent
            width: 18; height: 18
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: bell.open ? Theme.accent : button.hovered ? Theme.text : Theme.textLabel
                strokeWidth: 1.6; fillColor: "transparent"
                joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
                PathSvg { path: "M4.5 12.5 L4.5 8 A4.5 4.5 0 0 1 13.5 8 L13.5 12.5 L15 14 L3 14 Z M7.3 16.3 L10.7 16.3 M9 1.8 L9 3.5" }
            }
        }
    }
    Rectangle {
        objectName: "unreadBellBadge"
        visible: bell.count > 0
        x: parent.width - width + 2; y: -2
        width: Math.max(16, badgeText.implicitWidth + 8); height: 16; radius: 8
        color: bell.waiting ? Theme.warning : Theme.success
        Text { id: badgeText; anchors.centerIn: parent; text: bell.count > 99 ? "99+" : bell.count
               color: Theme.menuSurface; font: Theme.captionStrong }
    }

    MenuSurface {
        id: menu
        objectName: "unreadBellMenu"
        width: 300
        Column {
            width: menu.availableWidth
            spacing: 2
            Repeater {
                model: bell.entries
                MenuRow {
                    required property var modelData
                    objectName: "unreadBellEntry"
                    label: modelData.title || qsTr("New Chat"); muted: !modelData.title
                    detail: modelData.project || qsTr("Global")
                    shortcut: modelData.state === "needsYou" ? qsTr("Waiting for you") : qsTr("Finished")
                    available: !bridge.navigationLocked
                    onTriggered: { var b = bridge, id = modelData.id; menu.close()
                                   if (b.openConversation(id)) b.setPage("home") }
                }
            }
            Text { objectName: "unreadBellEmpty"; visible: bell.count === 0; width: parent.width
                   leftPadding: 12; rightPadding: 12; topPadding: 8; bottomPadding: 8
                   text: qsTr("Nothing waiting"); textFormat: Text.PlainText; color: Theme.textMuted; font: Theme.body }
        }
    }
}
