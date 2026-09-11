import QtQuick
import "../theme"
import "../components"

// One modal confirmation. Used for the two hard-to-undo gestures: the kill
// switch (both directions) and archiving a job.
Item {
    id: dialog
    anchors.fill: parent
    property bool open: false
    property string title: ""
    property string message: ""
    property string confirmLabel: "Confirm"
    property bool danger: true
    property var onConfirm: null
    visible: open || dim.opacity > 0
    z: 500

    function ask(title, message, confirmLabel, danger, fn) {
        dialog.title = title; dialog.message = message; dialog.confirmLabel = confirmLabel; dialog.danger = danger; dialog.onConfirm = fn; dialog.open = true
    }

    Rectangle { id: dim; anchors.fill: parent; color: "#000000"; opacity: dialog.open ? 0.5 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.pageMs } }
        MouseArea { anchors.fill: parent; enabled: dialog.open } }
    GlassPanel {
        raised: true
        anchors.centerIn: parent
        width: 460; height: column.height + 52
        border.color: dialog.danger ? Theme.danger : Theme.accent
        opacity: dialog.open ? 1 : 0; scale: dialog.open ? 1 : 0.97
        Behavior on opacity { NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        Behavior on scale { NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        padding: 26
        Column {
            id: column
            width: parent.width; spacing: 14
            Row { spacing: 12
                Rectangle { width: 10; height: 10; radius: 5; color: dialog.danger ? Theme.danger : Theme.accent; anchors.verticalCenter: parent.verticalCenter
                    Rectangle { anchors.centerIn: parent; width: 20; height: 20; radius: 10; color: parent.color; opacity: 0.22 } }
                Text { anchors.verticalCenter: parent.verticalCenter; text: dialog.title.toUpperCase(); color: dialog.danger ? Theme.danger : Theme.accent; font { family: Theme.font; pixelSize: 12; letterSpacing: 3; weight: Font.DemiBold } }
            }
            Text { text: dialog.message; color: Theme.text; width: parent.width; wrapMode: Text.Wrap; font { family: Theme.font; pixelSize: 14 } }
            Row { spacing: 10; anchors.right: parent.right
                ActionButton { label: "Back"; onClicked: dialog.open = false }
                ActionButton { label: dialog.confirmLabel; primary: true; danger: dialog.danger; onClicked: { dialog.open = false; if (dialog.onConfirm) dialog.onConfirm() } }
            }
        }
    }
    Keys.onEscapePressed: dialog.open = false
}
