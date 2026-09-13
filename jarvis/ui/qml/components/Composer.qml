import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// The natural-language composer. Text goes to bridge.submitText, which is
// JarvisApp.submit_text; no JSON, no capability names, no grammar here.
//
// Built as the approved helper's layered instrument rather than a bordered
// field: a soft outer bloom, a luminous outer ring, a deep glass shell with
// a bright cyan rim (brightest along the bottom, where the helper's light
// pools), a darker inset well for the prompt mark, and a vivid send key.
// Only supported controls appear - no attachment, voice or web action is
// faked; a real keyboard hint balances the space they would take.
Rectangle {
    id: composer
    height: 66
    radius: 12
    gradient: Gradient {
        GradientStop { position: 0; color: "#f400192d" }
        GradientStop { position: 1; color: "#f4001220" }
    }
    border.width: 1.5
    border.color: input.activeFocus ? "#6fd8f5" : "#2aa0c9"
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
    property bool blocked: bridge.busy || bridge.approvalPending || bridge.configRefused
    property bool canSend: !blocked && input.text.trim() !== ""
    property alias text: input.text
    readonly property real inner: height - 19          // well and key height
    function submit() { if (canSend) { bridge.submitText(input.text); input.text = "" } }

    // Second illumination layer around the shell, and a wide soft bloom.
    Rectangle { z: -1; anchors.fill: parent; anchors.margins: -8; radius: parent.radius + 8; color: "transparent"
        border.width: 6; border.color: input.activeFocus ? "#1a1fb0dc" : "#0f1fb0dc" }
    Rectangle { z: -1; anchors.fill: parent; anchors.margins: -3; radius: parent.radius + 3; color: "transparent"
        border.width: 2; border.color: input.activeFocus ? "#7a1fb0dc" : "#4a1fb0dc"
        Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } } }
    // Shell depth: an inner shadow under the top rim, a faint sheen, and the
    // bright pooled light along the bottom rim.
    Rectangle { x: parent.radius; y: 2; width: parent.width - 2 * parent.radius; height: 1; color: "#80000000" }
    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 2 } height: parent.height * 0.42; radius: 10
        gradient: Gradient { GradientStop { position: 0; color: "#0d6fd2f5" } GradientStop { position: 1; color: "transparent" } } }
    Rectangle { x: parent.radius; y: parent.height - 1.5; width: parent.width - 2 * parent.radius; height: 1.5
        gradient: Gradient { orientation: Gradient.Horizontal
            GradientStop { position: 0; color: "#002aa0c9" } GradientStop { position: 0.2; color: "#cc5cc6e6" }
            GradientStop { position: 0.8; color: "#cc5cc6e6" } GradientStop { position: 1; color: "#002aa0c9" } } }

    // The prompt mark, in its own darker inset well.
    Rectangle {
        id: well
        anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter
        width: Math.round(composer.inner * 1.15); height: composer.inner; radius: 10
        color: "#f4000b15"; border.width: 1; border.color: "#1f4260"
        Rectangle { x: 10; y: 1; width: parent.width - 20; height: 1; color: "#1f6fd2f5" }
        Rectangle {
            anchors.centerIn: parent
            width: 26; height: 26; radius: 13; color: "transparent"; border.width: 1.6; border.color: input.activeFocus ? "#6fd8f5" : "#3b9fc4"
            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
            Rectangle { anchors.centerIn: parent; width: 7; height: 7; radius: 3.5; color: "#2cc9ea"; opacity: input.activeFocus ? 1 : 0.8 }
        }
    }
    TextField {
        id: input
        anchors { left: well.right; right: hint.visible ? hint.left : (bridge.busy ? stopButton.left : sendButton.left); verticalCenter: parent.verticalCenter
                  leftMargin: composer.height >= 72 ? 22 : 14; rightMargin: 14 }
        placeholderText: bridge.approvalPending ? "Answer the pending approval first"
                        : bridge.configRefused ? "Configuration refused - repair it with: jarvis config reset --all"
                        : (bridge.task.status === "awaiting_clarification" ? "Answer JARVIS's question…" : "Message JARVIS…")
        placeholderTextColor: "#b1cdf1"
        color: "#f3f9ff"
        font { family: Theme.font; pixelSize: composer.height >= 72 ? 18 : 16 }
        background: Item {}
        selectByMouse: true
        enabled: !bridge.configRefused
        Keys.onReturnPressed: composer.submit()
        Keys.onEnterPressed: composer.submit()
        // Real typing activity for the core: on while text is being entered,
        // off 900 ms after the last edit or as soon as the field is empty.
        onTextChanged: { if (text.length > 0) { bridge.setComposing(true); composingQuiet.restart() } else { composingQuiet.stop(); bridge.setComposing(false) } }
    }
    Timer { id: composingQuiet; interval: 900; onTriggered: bridge.setComposing(false) }
    // A real affordance in the space the helper gives to tools this product
    // does not have: Enter sends.
    Text {
        id: hint
        visible: composer.width >= 720 && !bridge.busy && !composer.blocked && input.text === ""
        anchors { right: sendButton.left; rightMargin: 22; verticalCenter: parent.verticalCenter }
        text: "Enter  ↵  to send"
        color: "#6f88ad"
        font { family: Theme.font; pixelSize: 13; letterSpacing: 0.4 }
    }
    // Send: a vivid, flat cyan key.
    Rectangle {
        id: sendButton
        anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter
        width: Math.round(composer.inner * 1.12); height: composer.inner; radius: 10
        readonly property bool hovered: sendArea.containsMouse && composer.canSend
        gradient: Gradient {
            GradientStop { position: 0; color: composer.blocked ? Theme.alpha(Theme.accent, 0.25) : (sendButton.hovered ? "#52d3ff" : "#1ec0ff") }
            GradientStop { position: 1; color: composer.blocked ? Theme.alpha(Theme.accentDeep, 0.25) : (sendArea.pressed ? Qt.darker(Theme.accentDeep, 1.2) : "#07a4ec") }
        }
        border.width: 1; border.color: composer.blocked ? Theme.alpha(Theme.accent, 0.3) : "#8ce6f7ff"
        scale: sendArea.pressed && composer.canSend ? 0.96 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
        Rectangle { z: -1; anchors.fill: parent; anchors.margins: -4; radius: 14; color: Theme.alpha(Theme.accent, composer.blocked ? 0 : (parent.hovered ? 0.3 : 0.12)); Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        Rectangle { x: 8; y: 1; width: parent.width - 16; height: 1; color: "#66ffffff" }
        Shape {
            anchors.centerIn: parent; anchors.horizontalCenterOffset: 1
            width: 22; height: 20; scale: composer.inner / 60 * 1.42
            preferredRendererType: Shape.CurveRenderer
            opacity: composer.blocked ? 0.45 : 1
            ShapePath {
                strokeColor: "#ffffff"; strokeWidth: 1.7; fillColor: "transparent"
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
        anchors.right: sendButton.left; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter
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
