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
    property string confirmLabel: qsTr("Confirm")
    property bool danger: true
    property var onConfirm: null
    visible: open || dim.opacity > 0
    z: 500

    property string cancelLabel: qsTr("Back")
    function ask(title, message, confirmLabel, danger, fn, cancelLabel) {
        dialog.armed = false; armTimer.restart()           // a re-ask while open is guarded again
        dialog.title = title; dialog.message = message; dialog.confirmLabel = confirmLabel; dialog.danger = danger; dialog.onConfirm = fn
        dialog.cancelLabel = cancelLabel || qsTr("Back"); dialog.open = true
    }

    // K2: a destructive confirmation (danger) has no motion - it appears and goes at once -
    // and an input guard instead: its confirm arms Theme.confirmGuardMs after it opens
    // (ambient effects on or off) and disarms the moment it closes.
    property bool armed: false
    Timer { id: armTimer; interval: Theme.confirmGuardMs; onTriggered: dialog.armed = true }
    // K4 (the one dialog style): Theme.scrimOpacity, Theme.popupRadius; focus starts on
    // the safe choice and returns to the opener when it closes (Esc included).
    property Item opener: null
    onOpenChanged: {
        armTimer.stop(); armed = false
        if (open) { armTimer.start(); if (!opener) opener = Window.activeFocusItem; focusSafe() }
        else if (opener) { var back = opener; opener = null; back.forceActiveFocus() }
    }
    onVisibleChanged: focusSafe()
    function focusSafe() { if (open && visible) cancelButton.forceActiveFocus() }
    Rectangle { id: dim; anchors.fill: parent; color: "#000000"; opacity: dialog.open ? Theme.scrimOpacity : 0
        Behavior on opacity { enabled: !dialog.danger; NumberAnimation { duration: Theme.pageMs } }
        MouseArea { anchors.fill: parent; enabled: dialog.open } }
    GlassPanel {
        raised: true
        anchors.centerIn: parent
        radius: Theme.popupRadius
        width: 460; height: column.height + 52
        border.color: dialog.danger ? Theme.danger : Theme.accent
        opacity: dialog.open ? 1 : 0; scale: dialog.open ? 1 : 1 - 2 * Theme.travel / Math.max(1, width, height)   // the edges travel at most Theme.travel
        Behavior on opacity { enabled: !dialog.danger; NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        Behavior on scale { enabled: !dialog.danger; NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        padding: 26
        Column {
            id: column
            width: parent.width; spacing: 14
            Row { spacing: 12
                Rectangle { width: 10; height: 10; radius: 5; color: dialog.danger ? Theme.danger : Theme.accent; anchors.verticalCenter: parent.verticalCenter
                    Rectangle { anchors.centerIn: parent; width: 20; height: 20; radius: 10; color: parent.color; opacity: 0.22 } }
                Text { anchors.verticalCenter: parent.verticalCenter; text: i18n.upper(dialog.title); color: dialog.danger ? Theme.danger : Theme.accent; font: Theme.label }
            }
            Text { text: dialog.message; color: Theme.text; width: parent.width; wrapMode: Text.Wrap; font: Theme.body }
            Row { spacing: 10; anchors.right: parent.right
                ActionButton { id: cancelButton; objectName: "confirmCancel"; label: dialog.cancelLabel; onClicked: dialog.open = false }
                ActionButton { objectName: "confirmAccept"; label: dialog.confirmLabel; primary: true; danger: dialog.danger
                               enabled: !dialog.danger || dialog.armed; onClicked: { dialog.open = false; if (dialog.onConfirm) dialog.onConfirm() } }
            }
        }
    }
    Keys.onEscapePressed: dialog.open = false
}
