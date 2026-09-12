import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The natural-language composer. Text goes to bridge.submitText, which is
// JarvisApp.submit_text; no JSON, no capability names, no grammar here.
Rectangle {
    id: composer
    height: 62
    radius: 14
    color: Theme.surfaceRaised
    border.width: 1
    border.color: input.activeFocus ? Theme.alpha(Theme.accent, 0.75) : Theme.border
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
    property bool canSend: !bridge.busy && !bridge.approvalPending && !bridge.configRefused && input.text.trim() !== ""
    property alias text: input.text
    function submit() { if (canSend) { bridge.submitText(input.text); input.text = "" } }

    // Depth and the focused glow.
    Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 4; radius: parent.radius; color: "#000000"; opacity: 0.3 }
    Rectangle { z: -1; anchors.fill: parent; anchors.margins: -4; radius: parent.radius + 4; color: Theme.alpha(Theme.accent, input.activeFocus ? 0.10 : 0)
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 13
        gradient: Gradient { GradientStop { position: 0; color: Theme.sheen } GradientStop { position: 1; color: "transparent" } } }
    Rectangle { x: 14; y: 0; height: 1; width: 200
        gradient: Gradient { orientation: Gradient.Horizontal; GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0.6) } GradientStop { position: 1; color: "transparent" } } }

    // The prompt mark.
    Rectangle {
        anchors.left: parent.left; anchors.leftMargin: 18; anchors.verticalCenter: parent.verticalCenter
        width: 26; height: 26; radius: 13; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, input.activeFocus ? 0.8 : 0.4)
        Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
        Rectangle { anchors.centerIn: parent; width: 6; height: 6; radius: 3; color: Theme.accent; opacity: input.activeFocus ? 1 : 0.6 }
    }
    TextField {
        id: input
        anchors { left: parent.left; right: bridge.busy ? stopButton.left : sendButton.left; verticalCenter: parent.verticalCenter; leftMargin: 58; rightMargin: 12 }
        placeholderText: bridge.approvalPending ? "Answer the pending approval first"
                        : bridge.configRefused ? "Configuration refused - repair it with: jarvis config reset --all"
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
    // Send: a lit key when there is something to send.
    Rectangle {
        id: sendButton
        anchors.right: parent.right; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter
        width: 46; height: 44; radius: 11
        readonly property bool hovered: sendArea.containsMouse && composer.canSend
        color: composer.canSend ? (sendArea.pressed ? Qt.darker(Theme.accent, 1.2) : hovered ? Qt.lighter(Theme.accent, 1.08) : Theme.accent) : Theme.alpha(Theme.text, 0.04)
        border.width: 1; border.color: composer.canSend ? Theme.alpha(Theme.accent, 0.9) : Theme.border
        scale: sendArea.pressed && composer.canSend ? 0.96 : 1
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Behavior on scale { NumberAnimation { duration: 90 } }
        Rectangle { z: -1; anchors.fill: parent; anchors.margins: -4; radius: 15; color: Theme.alpha(Theme.accent, composer.canSend ? (parent.hovered ? 0.34 : 0.2) : 0); Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        Rectangle { visible: composer.canSend; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 10
            gradient: Gradient { GradientStop { position: 0; color: Qt.rgba(1, 1, 1, 0.22) } GradientStop { position: 1; color: "transparent" } } }
        Text { anchors.centerIn: parent; anchors.horizontalCenterOffset: 1; text: "➤"; color: composer.canSend ? "#04101c" : Theme.textDim; font.pixelSize: 17
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        MouseArea { id: sendArea; anchors.fill: parent; hoverEnabled: true; cursorShape: composer.canSend ? Qt.PointingHandCursor : Qt.ArrowCursor; onClicked: composer.submit() }
    }
    // Cancel the running understanding call from the same spot.
    Rectangle {
        id: stopButton
        anchors.right: sendButton.left; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter
        width: stopRow.width + 24; height: 34; radius: Theme.radiusSmall
        visible: bridge.busy
        color: stopArea.containsMouse ? Theme.alpha(Theme.danger, 0.14) : Theme.alpha(Theme.text, 0.04); border.width: 1; border.color: stopArea.containsMouse ? Theme.alpha(Theme.danger, 0.6) : Theme.borderStrong
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Row { id: stopRow; anchors.centerIn: parent; spacing: 8
            Rectangle { width: 8; height: 8; radius: 2; color: Theme.danger; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "Stop"; color: Theme.text; font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } } }
        MouseArea { id: stopArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { if (!bridge.cancelText()) bridge.cancelTask() } }
    }
}
