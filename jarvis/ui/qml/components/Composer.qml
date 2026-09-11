import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The natural-language composer. Text goes to bridge.submitText, which is
// JarvisApp.submit_text; no JSON, no capability names, no grammar here.
Rectangle {
    id: composer
    height: 64
    radius: Theme.radius
    color: Theme.surfaceRaised
    border.width: 1
    border.color: input.activeFocus ? Theme.accent : Theme.border
    Behavior on border.color { ColorAnimation { duration: 150 } }
    property bool canSend: !bridge.busy && !bridge.approvalPending && !bridge.configRefused && input.text.trim() !== ""
    property alias text: input.text
    function submit() { if (canSend) { bridge.submitText(input.text); input.text = "" } }

    Text {
        anchors.left: parent.left; anchors.leftMargin: 22; anchors.verticalCenter: parent.verticalCenter
        text: "◎"; color: Theme.textMuted; font.pixelSize: 18
    }
    TextField {
        id: input
        anchors { left: parent.left; right: sendButton.left; verticalCenter: parent.verticalCenter; leftMargin: 54; rightMargin: 14 }
        placeholderText: bridge.approvalPending ? "Answer the pending approval first"
                        : bridge.configRefused ? "Configuration refused - repair it with 'jarvis config reset --all'"
                        : (bridge.task.status === "awaiting_clarification" ? "Answer JARVIS's question…" : "Message JARVIS…")
        placeholderTextColor: Theme.textDim
        color: Theme.text
        font { family: Theme.font; pixelSize: 15 }
        background: Item {}
        selectByMouse: true
        enabled: !bridge.configRefused
        Keys.onReturnPressed: composer.submit()
        Keys.onEnterPressed: composer.submit()
    }
    Rectangle {
        id: sendButton
        anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter
        width: 46; height: 44; radius: Theme.radiusSmall
        color: composer.canSend ? Theme.accent : Theme.surface
        border.width: 1; border.color: composer.canSend ? Theme.accent : Theme.border
        Behavior on color { ColorAnimation { duration: 150 } }
        Text { anchors.centerIn: parent; text: bridge.busy ? "◌" : "➤"; color: composer.canSend ? Theme.bg0 : Theme.textDim; font.pixelSize: 18 }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: composer.submit() }
    }
    // Cancel the running understanding call from the same spot.
    Rectangle {
        anchors.right: sendButton.left; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter
        width: 70; height: 34; radius: Theme.radiusSmall
        visible: bridge.busy
        color: "transparent"; border.width: 1; border.color: Theme.borderStrong
        Text { anchors.centerIn: parent; text: "Stop"; color: Theme.text; font { family: Theme.font; pixelSize: 12 } }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: { if (!bridge.cancelText()) bridge.cancelTask() } }
    }
}
