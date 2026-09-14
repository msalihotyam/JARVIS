import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// The natural-language composer. Text goes to bridge.submitText, which is
// JarvisApp.submit_text; no JSON, no capability names, no grammar here.
//
// A thin instrument: 52 px at rest, anchored by its bottom edge, growing
// UPWARD with the draft (about 140 ms per step) to `maxHeight` and scrolling
// inside beyond it; back to 52 px when an accepted submission clears the
// draft or the field is emptied. A refused submission keeps the draft.
// Enter sends, Shift+Enter breaks a line. Only supported controls appear:
// the compact send key and, while a request is being understood, Stop. No
// attachment, voice, web or menu control is faked.
Rectangle {
    id: composer
    property int minHeight: 52
    property int maxHeight: 180
    readonly property int vpad: 8
    implicitHeight: Math.max(minHeight, Math.min(maxHeight, Math.ceil(input.implicitHeight) + 2 * vpad))
    Behavior on implicitHeight { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
    radius: 10
    gradient: Gradient {
        GradientStop { position: 0; color: Theme.glassInteractiveTop }
        GradientStop { position: 1; color: Theme.glassInteractive }
    }
    border.width: 1
    border.color: input.activeFocus ? Theme.glassEdgeStrong : Theme.glassEdge
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
    property bool blocked: bridge.busy || bridge.approvalPending || bridge.configRefused
    property bool canSend: !blocked && input.text.trim() !== ""
    property alias text: input.text
    readonly property alias inputHeight: input.implicitHeight
    // Clear the draft only when the application really accepted it.
    function submit() { if (canSend && bridge.submitText(input.text)) input.text = "" }

    // Material depth: a restrained upper-edge reflection; focus adds a fine
    // inner highlight and nothing larger.
    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: Math.min(parent.height * 0.5, 26); radius: 9
        gradient: Gradient { GradientStop { position: 0; color: input.activeFocus ? Theme.glassInnerHighlight : Theme.glassReflection } GradientStop { position: 1; color: "transparent" } } }

    // The draft: a wrapping text area inside a flickable, so past the maximum
    // height the text scrolls and the caret stays in view.
    Flickable {
        id: flick
        anchors { left: parent.left; right: bridge.busy ? stopButton.left : sendButton.left; top: parent.top; bottom: parent.bottom
                  leftMargin: 14; rightMargin: 4; topMargin: composer.vpad; bottomMargin: composer.vpad }
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height + 1
        TextArea.flickable: TextArea {
            id: input
            objectName: "composerInput"
            padding: 0
            wrapMode: TextArea.Wrap
            verticalAlignment: TextEdit.AlignVCenter
            placeholderText: bridge.approvalPending ? "Answer the pending approval first"
                            : bridge.configRefused ? "Configuration refused - repair it with: jarvis config reset --all"
                            : (bridge.task.status === "awaiting_clarification" ? "Answer JARVIS's question…" : "Message JARVIS…")
            placeholderTextColor: "#9db8dc"
            color: "#f3f9ff"
            font { family: Theme.font; pixelSize: 16 }
            background: null
            selectByMouse: true
            enabled: !bridge.configRefused
            Keys.onReturnPressed: function(event) { if (event.modifiers & Qt.ShiftModifier) event.accepted = false; else composer.submit() }
            Keys.onEnterPressed: function(event) { if (event.modifiers & Qt.ShiftModifier) event.accepted = false; else composer.submit() }
            // Real typing activity for the core: on while text is being entered,
            // off 900 ms after the last edit or as soon as the field is empty.
            onTextChanged: { if (text.length > 0) { bridge.setComposing(true); composingQuiet.restart() } else { composingQuiet.stop(); bridge.setComposing(false) } }
        }
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; width: 6 }
    }
    Timer { id: composingQuiet; interval: 900; onTriggered: bridge.setComposing(false) }

    // Send: a compact technical key - a 34 px chamfered square of deep navy
    // with a thin cool-blue perimeter and a pale-blue line glyph, inside a
    // 44 px target. Dimmed and inert while nothing can be sent.
    Item {
        id: sendButton
        objectName: "composerSend"
        anchors.right: parent.right; anchors.rightMargin: 4; anchors.verticalCenter: parent.verticalCenter
        width: 44; height: 44
        enabled: composer.canSend
        opacity: composer.canSend ? 1 : 0.42
        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: "Send message"
        Accessible.onPressAction: composer.submit()
        Keys.onReturnPressed: composer.submit()
        Keys.onSpacePressed: composer.submit()
        readonly property bool hovered: sendArea.containsMouse
        readonly property bool pressed: sendArea.pressed
        readonly property color rim: pressed ? "#2f7fb0" : hovered ? "#62c8f2" : "#3596cf"
        readonly property color face: pressed ? "#03101d" : hovered ? "#0c2a48" : "#071c33"
        Shape {
            anchors.centerIn: parent; width: 34; height: 34
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: sendButton.rim; strokeWidth: 1; fillColor: sendButton.face
                joinStyle: ShapePath.MiterJoin
                startX: 4.5; startY: 0.5
                PathLine { x: 29.5; y: 0.5 } PathLine { x: 33.5; y: 4.5 } PathLine { x: 33.5; y: 29.5 }
                PathLine { x: 29.5; y: 33.5 } PathLine { x: 4.5; y: 33.5 } PathLine { x: 0.5; y: 29.5 }
                PathLine { x: 0.5; y: 4.5 } PathLine { x: 4.5; y: 0.5 }
            }
            Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
        }
        // Pressed: a darker inset along the top-left inside the perimeter.
        Rectangle { visible: sendButton.pressed; anchors.centerIn: parent; anchors.horizontalCenterOffset: 0; width: 30; height: 30; color: "transparent"
            border.width: 1; border.color: "#66000a14" }
        // The glyph: an 18 px line send arrow.
        Shape {
            anchors.centerIn: parent; anchors.horizontalCenterOffset: 1
            width: 18; height: 18
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: sendButton.hovered ? "#d6f1ff" : "#b8e7ff"; strokeWidth: 1.5; fillColor: "transparent"
                joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
                startX: 1.5; startY: 1.5
                PathLine { x: 16.5; y: 9 } PathLine { x: 1.5; y: 16.5 } PathLine { x: 4; y: 9 } PathLine { x: 1.5; y: 1.5 }
                PathMove { x: 4; y: 9 } PathLine { x: 10; y: 9 }
            }
        }
        // Keyboard focus: a fine explicit outline around the face.
        Rectangle { visible: sendButton.activeFocus; anchors.centerIn: parent; width: 40; height: 40; radius: 4; color: "transparent"
            border.width: 1; border.color: "#6fd8f5" }
        MouseArea { id: sendArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: composer.submit() }
    }
    // Cancel the running understanding call from the same spot.
    Rectangle {
        id: stopButton
        anchors.right: sendButton.left; anchors.rightMargin: 6; anchors.verticalCenter: parent.verticalCenter
        width: stopRow.width + 24; height: 32; radius: Theme.radiusSmall
        visible: bridge.busy
        color: stopArea.containsMouse ? Theme.alpha(Theme.danger, 0.14) : Theme.alpha(Theme.text, 0.04); border.width: 1; border.color: stopArea.containsMouse ? Theme.alpha(Theme.danger, 0.6) : Theme.borderStrong
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Row { id: stopRow; anchors.centerIn: parent; spacing: 8
            Rectangle { width: 8; height: 8; radius: 2; color: Theme.danger; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "Stop"; color: Theme.text; font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } } }
        MouseArea { id: stopArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { if (!bridge.cancelText()) bridge.cancelTask() } }
    }
}
