import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import "../theme"

// The natural-language composer. Text goes to bridge.submitText, which is
// JarvisApp.submit_text; no JSON, no capability names, no grammar here.
//
// A thin instrument: 52 px at rest, anchored by its bottom edge, growing
// UPWARD with the draft (about 140 ms per step) to `maxHeight` and scrolling
// inside beyond it; back to 52 px when an accepted submission clears the
// draft or the field is emptied. A refused submission keeps the draft.
// Enter sends, Shift+Enter breaks a line.
//
// Above the bar sit the draft's context items - quoted messages, quoted
// selections and the files the person attached with "+". They are separate
// from the text being written and stay separate all the way to the provider.
// Only supported controls appear; no voice, web or menu control is faked.
Item {
    id: composer
    property int minHeight: 52
    property int maxHeight: 180
    readonly property int vpad: 8
    readonly property int barHeight: Math.max(minHeight, Math.min(maxHeight, Math.ceil(input.implicitHeight) + 2 * vpad))
    implicitHeight: readOnlyBanner.height + (readOnlyBanner.visible ? 6 : 0) + strip.height + barHeight
    // An archived project's conversation is read-only (V2.0 Gate 6A). Presentation
    // only: Core refuses the work anyway (ARCHITECTURE 4.22).
    property bool readOnly: bridge.conversationReadOnly
    property bool blocked: bridge.busy || bridge.approvalPending || bridge.configRefused || readOnly
    // Attached items travel WITH a message; the request itself is still the words
    // the person writes, so an empty draft sends nothing.
    property bool canSend: !blocked && input.text.trim() !== ""
    property alias text: input.text
    // A press anywhere else releases the draft's focus (jarvis.ui.app); inside
    // the composer - the draft, its chips and actions - it stays.
    readonly property bool keepsTextFocus: true
    readonly property alias inputHeight: input.implicitHeight
    // Clear the draft only when the application really accepted it.
    function submit() { if (canSend && bridge.submitText(input.text)) input.text = "" }

    readonly property var permissionModes: [
        { id: "strict", short: qsTr("Ask"), name: qsTr("Ask every time"),
          detail: qsTr("Every action asks for your approval first, local file reads included.") },
        { id: "smart_auto", short: qsTr("Smart"), name: qsTr("Smart approval"),
          detail: qsTr("Low-risk, reversible actions (local file reads) run without asking. Everything else asks.") },
        { id: "session", short: qsTr("Session"), name: qsTr("Session permission"),
          detail: qsTr("As Smart approval; Core may also reuse an approval for this session where its rules allow. No current capability qualifies, so today it asks exactly like Smart approval.") }
    ]
    function modeInfo(id) { for (var i = 0; i < permissionModes.length; i++) if (permissionModes[i].id === id) return permissionModes[i]; return null }
    // Paste, for the keyboard and the menu alike: the bridge decides once whether
    // the clipboard holds attachments; only when it does not is it ordinary text.
    function paste() { if (bridge.attachClipboard() < 0) input.paste() }
    // Pick files for the message itself, or - with a group id - for that group only.
    function pick(kind, groupId) { picker.imagesOnly = kind === "image"; picker.group = groupId || ""; picker.open() }
    FileDialog {
        id: picker
        objectName: "attachDialog"
        property bool imagesOnly: false
        property string group: ""
        fileMode: FileDialog.OpenFiles
        nameFilters: imagesOnly ? [qsTr("Images (%1)").arg("*.png *.jpg *.jpeg *.webp")]
                                : [qsTr("Text and code (%1)").arg("*.txt *.md *.json *.yaml *.yml *.csv *.log *.py *.js *.ts *.qml *.html *.css *.xml *.sh *.sql"),
                                   qsTr("Images (%1)").arg("*.png *.jpg *.jpeg *.webp")]
        onAccepted: {
            for (var i = 0; i < selectedFiles.length; i++) {
                if (group) bridge.attachFileToGroup(group, selectedFiles[i])
                else bridge.attachFile(selectedFiles[i])
            }
        }
    }

    EditMenu {
        id: composerMenu
        objectName: "composerEditMenu"
        editor: input
        attachAware: true
        onPasteRequested: composer.paste()
    }

    // The draft's context items, above the bar and in their own order.
    Rectangle {
        id: readOnlyBanner
        objectName: "readOnlyBanner"
        anchors { left: parent.left; right: parent.right; top: parent.top }
        visible: composer.readOnly
        height: visible ? readOnlyText.height + 18 : 0
        radius: 8; color: Theme.alpha(Theme.warning, 0.10); border.width: 1; border.color: Theme.alpha(Theme.warning, 0.5)
        Text { id: readOnlyText; objectName: "readOnlyReason"; x: 12; y: 9; width: parent.width - 24; wrapMode: Text.Wrap
               color: Theme.warning; font: Theme.caption
               text: bridge.conversationArchived
                     ? qsTr("Read-only: this conversation is archived. Unarchive it to continue it.")
                     : qsTr("Read-only: this conversation belongs to the archived project %1. Unarchive the project to continue it.").arg((bridge.conversationProject || {}).name || "") }
    }

    ContextStrip {
        id: strip
        objectName: "composerContext"
        anchors { left: parent.left; right: parent.right; top: readOnlyBanner.bottom; topMargin: readOnlyBanner.visible ? 6 : 0 }
        onPickRequested: function(kind, groupId) { composer.pick(kind, groupId) }
    }

    Rectangle {
        id: bar
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: composer.barHeight
        Behavior on height { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
        radius: 10
        // The BODY only: very transparent at rest, a step darker under the
        // pointer, a step darker again while it has focus. The draft text, the
        // placeholder, the chips and the send key never change opacity.
        property color body: input.activeFocus ? Theme.glassControlActive : barHover.hovered ? Theme.glassControlHover : Theme.glassControlIdle
        property color bodyTop: input.activeFocus ? Theme.glassControlActiveTop : barHover.hovered ? Theme.glassControlHoverTop : Theme.glassControlIdleTop
        Behavior on body { ColorAnimation { duration: Theme.hoverMs } }
        Behavior on bodyTop { ColorAnimation { duration: Theme.hoverMs } }
        gradient: Gradient {
            GradientStop { position: 0; color: bar.bodyTop }
            GradientStop { position: 1; color: bar.body }
        }
        border.width: 1
        border.color: input.activeFocus ? Theme.glassEdgeStrong : Theme.glassEdge
        Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
        HoverHandler { id: barHover }

    // Material depth: a restrained upper-edge reflection; focus adds a fine
    // inner highlight and nothing larger.
    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: Math.min(parent.height * 0.5, 26); radius: 9
        gradient: Gradient { GradientStop { position: 0; color: input.activeFocus ? Theme.glassInnerHighlight : Theme.glassReflection } GradientStop { position: 1; color: "transparent" } } }

    // Permission mode: Core's own approval policy (``permission_mode``), shown
    // and changed here. The label is what the stored configuration holds; a
    // choice is written through the same config path the CLI uses and Core
    // applies it on the next action. It never overrides an always-approval
    // action, UNKNOWN_OUTCOME, the execution journal or Freeze.
    Item {
        id: permissionChip
        objectName: "permissionModeControl"
        anchors.left: parent.left; anchors.leftMargin: 8; anchors.bottom: parent.bottom; anchors.bottomMargin: 10
        width: chipRow.width + 18; height: 32
        readonly property var current: composer.modeInfo(bridge.permissionMode)
        enabled: !bridge.busy && !bridge.configRefused && !composer.readOnly
        opacity: enabled ? 1 : 0.5
        Accessible.role: Accessible.Button
        Accessible.name: qsTr("Permission mode: %1").arg(current ? current.name : qsTr("Unknown"))
        Rectangle { anchors.fill: parent; radius: Theme.radiusSmall
                    color: chipArea.containsMouse || modeMenu.visible ? Theme.alpha(Theme.accent, 0.14) : Theme.alpha(Theme.text, 0.04)
                    border.width: 1; border.color: modeMenu.visible ? Theme.glassEdgeStrong : Theme.borderStrong }
        Row { id: chipRow; anchors.centerIn: parent; spacing: 6
            Rectangle { width: 7; height: 7; radius: 3.5; anchors.verticalCenter: parent.verticalCenter
                        color: bridge.permissionMode === "strict" ? Theme.warning : Theme.accent }
            Text { objectName: "permissionModeLabel"; text: permissionChip.current ? permissionChip.current.short : qsTr("Unknown")
                   color: Theme.text; renderType: Theme.textRender; font: Theme.composerChip } }
        MouseArea { id: chipArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: modeMenu.visible ? modeMenu.close() : modeMenu.open() }
        Popup {
            id: modeMenu
            objectName: "permissionModeMenu"
            y: -height - 8; x: 0
            width: 360; padding: 10
            background: Rectangle { radius: 10; color: Theme.menuSurface; border.width: 1; border.color: Theme.glassOverlayEdge
                // An open menu OWNS the wheel: the conversation behind it stays put.
                WheelHandler { } }
            contentItem: Column {
                spacing: 4
                Text { text: qsTr("PERMISSION MODE"); color: Theme.textMuted
                       font: Theme.label }
                Repeater {
                    model: composer.permissionModes
                    delegate: Rectangle {
                        objectName: "permissionMode_" + modelData.id
                        width: 340; height: modeText.height + 16; radius: 8
                        readonly property bool selected: bridge.permissionMode === modelData.id
                        color: selected ? Theme.alpha(Theme.accent, 0.16) : optionArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
                        border.width: selected ? 1 : 0; border.color: Theme.alpha(Theme.accent, 0.5)
                        Column { id: modeText; x: 10; y: 8; width: parent.width - 20; spacing: 2
                            Text { text: modelData.name + (parent.parent.selected ? "  ✓" : ""); color: Theme.text
                                   font: Theme.captionStrong }
                            Text { width: parent.width; wrapMode: Text.Wrap; text: modelData.detail; color: Theme.textMuted
                                   font: Theme.caption } }
                        MouseArea { id: optionArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                    onClicked: { bridge.setPermissionMode(modelData.id); modeMenu.close() } }
                    }
                }
                Text { width: 340; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.caption
                       text: qsTr("Mail, GitHub, browser, desktop, file writes and Claude analysis always ask, in every mode. Freeze is separate and stops everything.") }
            }
        }
    }

    // "+": attach a file or an image the person picks themselves. It grants
    // JARVIS no filesystem access of any kind - only the exact item chosen in
    // this dialog is read, once, and copied into the draft.
    Item {
        id: attachChip
        objectName: "attachControl"
        anchors.left: permissionChip.right; anchors.leftMargin: 6
        anchors.bottom: parent.bottom; anchors.bottomMargin: 10
        width: 32; height: 32
        enabled: !bridge.busy && !bridge.configRefused && !composer.readOnly
        opacity: enabled ? 1 : 0.5
        Accessible.role: Accessible.Button
        Accessible.name: qsTr("Attach a file or an image")
        Rectangle { anchors.fill: parent; radius: Theme.radiusSmall
                    color: attachArea.containsMouse || attachMenu.visible ? Theme.alpha(Theme.accent, 0.14) : Theme.alpha(Theme.text, 0.04)
                    border.width: 1; border.color: attachMenu.visible ? Theme.glassEdgeStrong : Theme.borderStrong }
        Text { anchors.centerIn: parent; text: "+"; color: Theme.text; renderType: Theme.textRender
               font: Qt.font({ family: Theme.ui, pixelSize: 16 }) }
        MouseArea { id: attachArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: attachMenu.visible ? attachMenu.close() : attachMenu.open() }
        Popup {
            id: attachMenu
            objectName: "attachMenu"
            y: -height - 8; x: 0
            width: 240; padding: 8
            background: Rectangle { radius: 10; color: Theme.menuSurface; border.width: 1; border.color: Theme.glassOverlayEdge
                WheelHandler { } }
            contentItem: Column {
                spacing: 2
                Repeater {
                    model: [{ id: "file", label: qsTr("Add file") }, { id: "image", label: qsTr("Add image") }]
                    delegate: Rectangle {
                        objectName: "attach_" + modelData.id
                        width: 224; height: 34; radius: 6
                        color: pickArea.containsMouse ? Theme.alpha(Theme.text, 0.07) : "transparent"
                        Text { x: 10; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: Theme.text
                               font: Theme.body }
                        MouseArea { id: pickArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                    onClicked: { attachMenu.close(); composer.pick(modelData.id) } }
                    }
                }
                Text { width: 224; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.caption
                       text: qsTr("Only the item you choose is read. JARVIS gets no access to your files.") }
            }
        }
    }

    // The draft: a wrapping text area inside a flickable, so past the maximum
    // height the text scrolls and the caret stays in view.
    Flickable {
        id: flick
        anchors { left: attachChip.right; right: bridge.busy ? stopButton.left : sendButton.left; top: parent.top; bottom: parent.bottom
                  leftMargin: 10; rightMargin: 4; topMargin: composer.vpad; bottomMargin: composer.vpad }
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height + 1
        TextArea.flickable: TextArea {
            id: input
            objectName: "composerInput"
            padding: 0
            wrapMode: TextArea.Wrap
            verticalAlignment: TextEdit.AlignVCenter
            placeholderText: composer.readOnly ? qsTr("Read-only: the project is archived")
                            : bridge.approvalPending ? qsTr("Answer the pending approval first")
                            : bridge.configRefused ? qsTr("Configuration refused - repair it with: %1").arg("jarvis config reset --all")
                            : (bridge.task.status === "awaiting_clarification" ? qsTr("Answer JARVIS's question…") : qsTr("Message JARVIS…"))
            // The invitation has a face of its own (Inter Medium, Theme.composerPlaceholder):
            // the style's placeholder item - and nothing else in the composer - takes it.
            // The typed text keeps the interface face; the size and the muted ink are shared.
            placeholderTextColor: Theme.textMuted
            color: Theme.text
            renderType: Theme.textRender
            font: Theme.composerText
            property Item placeholderItem: null
            Component.onCompleted: {
                for (var i = 0; i < children.length; i++)
                    if (String(children[i]).indexOf("PlaceholderText") !== -1) placeholderItem = children[i]
            }
            Binding { target: input.placeholderItem; property: "font"; value: Theme.composerPlaceholder; when: input.placeholderItem !== null }
            background: null
            selectByMouse: true
            enabled: !bridge.configRefused && !composer.readOnly
            Keys.onReturnPressed: function(event) { if (event.modifiers & Qt.ShiftModifier) event.accepted = false; else composer.submit() }
            Keys.onEnterPressed: function(event) { if (event.modifiers & Qt.ShiftModifier) event.accepted = false; else composer.submit() }
            // Paste: the same single route as the menu's Paste (composer.paste).
            Keys.onPressed: function(event) {
                if (event.matches(StandardKey.Paste)) { composer.paste(); event.accepted = true }
                else if (event.key === Qt.Key_Menu) {
                    composerMenu.openAt(input, Qt.point(cursorRectangle.x, cursorRectangle.y + cursorRectangle.height)); event.accepted = true }
            }
            // The JARVIS edit menu replaces the platform one, at the pointer; its Paste is the same route.
            TapHandler { acceptedButtons: Qt.RightButton; onTapped: function(point) { composerMenu.openAt(input, point.position) } }
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
        Accessible.name: qsTr("Send message")
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
            Text { text: qsTr("Stop"); color: Theme.text; font: Theme.captionStrong } }
        MouseArea { id: stopArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { if (!bridge.cancelText()) bridge.cancelTask() } }
    }
    }
}
