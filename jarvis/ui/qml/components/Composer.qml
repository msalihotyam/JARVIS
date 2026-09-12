import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// The natural-language composer. Text goes to bridge.submitText, which is
// JarvisApp.submit_text; no JSON, no capability names, no grammar here.
Rectangle {
    id: composer
    height: 66
    radius: 12
    color: Theme.alpha(Theme.bg1, 0.9)
    border.width: 1.5
    border.color: input.activeFocus ? Theme.alpha(Theme.accent, 0.95) : Theme.alpha(Theme.accent, 0.72)
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
    property bool blocked: bridge.busy || bridge.approvalPending || bridge.configRefused
    property bool canSend: !blocked && input.text.trim() !== ""
    property alias text: input.text
    function submit() { if (canSend) { bridge.submitText(input.text); input.text = "" } }

    // Depth and the cyan rim of the reference: a soft outer haze, stronger when focused.
    Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 4; radius: parent.radius; color: "#000000"; opacity: 0.3 }
    Rectangle { z: -1; anchors.fill: parent; anchors.margins: -5; radius: parent.radius + 5; color: Theme.alpha(Theme.accent, input.activeFocus ? 0.16 : 0.1)
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 2 } height: parent.height * 0.5; radius: 10
        gradient: Gradient { GradientStop { position: 0; color: Theme.sheen } GradientStop { position: 1; color: "transparent" } } }
    Rectangle { x: 14; y: 0; height: 1; width: parent.width - 28
        gradient: Gradient { orientation: Gradient.Horizontal
            GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0.2) } GradientStop { position: 0.15; color: Theme.alpha(Theme.accent, 0.85) }
            GradientStop { position: 0.85; color: Theme.alpha(Theme.accent, 0.6) } GradientStop { position: 1; color: Theme.alpha(Theme.accent, 0.15) } } }

    // The prompt mark, in its own inset well.
    Rectangle {
        id: well
        anchors.left: parent.left; anchors.leftMargin: 8; anchors.verticalCenter: parent.verticalCenter
        width: 50; height: parent.height - 16; radius: 9
        color: Theme.alpha(Theme.bg0, 0.55); border.width: 1; border.color: Theme.alpha(Theme.accent, 0.18)
        Rectangle {
            anchors.centerIn: parent
            width: 24; height: 24; radius: 12; color: "transparent"; border.width: 1.4; border.color: Theme.alpha(Theme.accent, input.activeFocus ? 0.9 : 0.55)
            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
            Rectangle { anchors.centerIn: parent; width: 6; height: 6; radius: 3; color: Theme.accent; opacity: input.activeFocus ? 1 : 0.7 }
        }
    }
    TextField {
        id: input
        anchors { left: parent.left; right: bridge.busy ? stopButton.left : sendButton.left; verticalCenter: parent.verticalCenter; leftMargin: 76; rightMargin: 12 }
        placeholderText: bridge.approvalPending ? "Answer the pending approval first"
                        : bridge.configRefused ? "Configuration refused - repair it with: jarvis config reset --all"
                        : (bridge.task.status === "awaiting_clarification" ? "Answer JARVIS's question…" : "Message JARVIS…")
        placeholderTextColor: Theme.textLabel
        color: Theme.text
        font { family: Theme.font; pixelSize: 16 }
        background: Item {}
        selectByMouse: true
        enabled: !bridge.configRefused
        Keys.onReturnPressed: composer.submit()
        Keys.onEnterPressed: composer.submit()
    }
    // Send: a lit key when there is something to send.
    Rectangle {
        id: sendButton
        anchors.right: parent.right; anchors.rightMargin: 7; anchors.verticalCenter: parent.verticalCenter
        width: 58; height: parent.height - 14; radius: 10
        readonly property bool hovered: sendArea.containsMouse && composer.canSend
        gradient: Gradient {
            GradientStop { position: 0; color: composer.blocked ? Theme.alpha(Theme.accent, 0.25) : (sendButton.hovered ? "#5fd6ff" : "#34c3ff") }
            GradientStop { position: 1; color: composer.blocked ? Theme.alpha(Theme.accentDeep, 0.25) : (sendArea.pressed ? Qt.darker(Theme.accentDeep, 1.2) : "#0aa2e8") }
        }
        border.width: 1; border.color: composer.blocked ? Theme.alpha(Theme.accent, 0.3) : Theme.alpha("#b8ecff", 0.7)
        scale: sendArea.pressed && composer.canSend ? 0.96 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
        Rectangle { z: -1; anchors.fill: parent; anchors.margins: -3; radius: 13; color: Theme.alpha(Theme.accent, composer.blocked ? 0 : (parent.hovered ? 0.3 : 0.16)); Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        Shape {
            anchors.centerIn: parent; anchors.horizontalCenterOffset: 1
            width: 22; height: 20
            preferredRendererType: Shape.CurveRenderer
            opacity: composer.blocked ? 0.45 : 1
            ShapePath {
                strokeColor: "#f4fbff"; strokeWidth: 1.8; fillColor: "transparent"
                joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
                startX: 1.5; startY: 1.5
                PathLine { x: 20.5; y: 10 } PathLine { x: 1.5; y: 18.5 } PathLine { x: 5; y: 10 } PathLine { x: 1.5; y: 1.5 }
                PathMove { x: 5; y: 10 } PathLine { x: 12; y: 10 }
            }
        }
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
