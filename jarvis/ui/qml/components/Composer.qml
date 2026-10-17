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
// selections, the files the person attached with "+" and long pastes. They are separate
// from the text being written and stay separate all the way to the provider.
// Only supported controls appear; no voice, web or menu control is faked.
//
// Gate 6G C1/M2/M3 (G6CM1): the row UNDER the bar holds "+" and the
// permission mode on the left, the Model and Effort chips on the right. While
// JARVIS runs the send key is the stop key (the same cancel) and a quiet
// "JARVIS is working…" line stands in the row - no progress is invented.
// G6SB3F: this one composer - and so this row - serves every surface: the
// chat, the New chat screen (which adds the V4 project picker to the row's
// left group) and a project's page (P3), which starts a new conversation.
Item {
    id: composer
    property int minHeight: 52
    property int maxHeight: 180
    readonly property int vpad: 8
    readonly property int barHeight: Math.max(minHeight, Math.min(maxHeight, Math.ceil(input.implicitHeight) + 2 * vpad))
    readonly property int rowGap: 6
    implicitHeight: readOnlyBanner.height + (readOnlyBanner.visible ? 6 : 0) + strip.height + skillHint.height + barHeight + rowGap + underRow.height
    // An archived project's conversation is read-only (V2.0 Gate 6A). Presentation
    // only: Core refuses the work anyway (ARCHITECTURE 4.22).
    property bool readOnly: bridge.conversationReadOnly
    // DFQUEUE: while JARVIS works for another conversation a send joins the queue (bridge.queueText).
    readonly property bool queueable: conversationBound && bridge.queueing
    property bool blocked: (bridge.busy && !queueable) || bridge.approvalPending || bridge.configRefused || readOnly
    // Attached items travel WITH a message; the request itself is still the words
    // the person writes, so an empty draft sends nothing.
    property bool canSend: !blocked && input.text.trim() !== ""
    property alias text: input.text
    // G6SB3F - what a host surface sets. `conversationBound` false: the draft
    // is not the open conversation's (P3 starts a new one), so the open
    // conversation's run, stop key and read-only banner are not shown here.
    property bool conversationBound: true
    property bool projectPicker: false          // V4: the picker in the row's left group
    property string placeholder: ""             // "" = the conversation's own invitation
    property string inputName: "composerInput"
    property string sendName: "composerSend"
    property string sendTip: ""                 // the send key's hover tip ("" = none)
    // The send: the open conversation's submit by default; a host may route the
    // draft elsewhere (true = accepted: the draft clears, then `submitted`).
    property var sendHandler: function (text) { return composer.queueable ? bridge.queueText(text) : bridge.submitText(text) }
    signal submitted()
    // A press anywhere else releases the draft's focus (jarvis.ui.app); inside
    // the composer - the draft, its chips and actions - it stays.
    readonly property bool keepsTextFocus: true
    readonly property alias inputHeight: input.implicitHeight
    // Clear the draft only when the application really accepted it. The pencil texts
    // of the cards it carried go with it; another conversation's parked cards keep theirs.
    // Not the open conversation's (a project page's): no parked draft travels with it.
    function submit() {
        if (!canSend) return
        var sent = conversationBound ? bridge.globalAttachments.map(function (file) { return file.id }) : []
        var accepted = false
        if (!conversationBound) bridge.holdDraft(true)
        try { accepted = sendHandler(input.text) } finally { if (!conversationBound) bridge.holdDraft(false) }
        if (accepted) { input.text = ""; sent.forEach(function (id) { delete pastes[id] }); submitted() }
    }
    // DFDRAFT: a conversation-bound composer shows the OPEN conversation's own draft.
    // It reports every edit (bridge.setDraftText); a switch, a start or a send puts
    // that conversation's draft back here, the caret at its end. Local data only.
    function restoreDraft() {
        if (!conversationBound || input.text === bridge.draftText) return
        input.text = bridge.draftText
        input.cursorPosition = input.length
    }
    Component.onCompleted: restoreDraft()
    Connections { target: bridge; function onDraftRestored() { composer.restoreDraft() } }

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
    // the clipboard holds attachments; only when it does not is it text. A long
    // text (LP1) becomes a pasted-text card above the bar instead of filling the
    // draft; a shorter one - or one the bridge cannot attach - is ordinary text.
    // Only a paste does this: typing never turns into a card.
    function paste() {
        if (bridge.attachClipboard() >= 0) return
        var raw = bridge.clipboardText()
        var id = isLongPaste(raw) ? bridge.attachPastedText(raw) : ""
        if (id) pastes[id] = raw
        else input.paste()
    }
    // LP3: the one tunable threshold - either bound is enough. A final line break
    // does not start another line.
    readonly property var longPaste: ({ chars: 2000, lines: 30 })
    function isLongPaste(text) {
        return text.length >= longPaste.chars || text.replace(/\n$/, "").split("\n").length >= longPaste.lines
    }
    // LP2: each card's text exactly as it was pasted (the card itself holds the
    // bounded, redacted copy that is sent), by attachment id, for its pencil.
    property var pastes: ({})
    // The pencil: the card's text goes back into the draft at the caret; the card goes.
    function unpaste(file) {
        var raw = pastes[file.id] !== undefined ? pastes[file.id] : String(file.text || "")
        delete pastes[file.id]
        bridge.removeAttachment(file.id)
        input.insert(input.cursorPosition, raw)
        input.forceActiveFocus()
    }
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
        visible: composer.readOnly && composer.conversationBound
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
        onUnpasteRequested: function(file) { composer.unpaste(file) }
    }

    // b4: an explicit skill run is the whole message; one line says so while it is typed.
    Text {
        id: skillHint
        objectName: "composerSkillHint"
        anchors { left: parent.left; right: parent.right; top: strip.bottom }
        visible: input.text.startsWith("/beceri") || input.text.startsWith("/skill")
        height: visible ? implicitHeight + 4 : 0
        textFormat: Text.PlainText; elide: Text.ElideRight; color: Theme.textLabel; font: Theme.caption
        text: qsTr("To run a skill, the whole message must be \"/beceri <name>\".")
    }

    Rectangle {
        id: bar
        objectName: "composerBar"
        anchors { left: parent.left; right: parent.right; bottom: underRow.top; bottomMargin: composer.rowGap }
        height: composer.barHeight
        Behavior on height { NumberAnimation { duration: Theme.dur(140); easing.type: Easing.OutCubic } }
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


    // The draft: a wrapping text area inside a flickable, so past the maximum
    // height the text scrolls and the caret stays in view.
    Flickable {
        id: flick
        anchors { left: parent.left; right: sendButton.left; top: parent.top; bottom: parent.bottom
                  leftMargin: 14; rightMargin: 4; topMargin: composer.vpad; bottomMargin: composer.vpad }
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height + 1
        TextArea.flickable: TextArea {
            id: input
            objectName: composer.inputName
            padding: 0
            wrapMode: TextArea.Wrap
            verticalAlignment: TextEdit.AlignVCenter
            placeholderText: composer.placeholder !== "" ? composer.placeholder
                            : composer.readOnly ? qsTr("Read-only: the project is archived")
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
            // The JARVIS edit menu (composerMenu) replaces the platform one, at the pointer; its Paste is the same route.
            ContextMenu.menu: null
            // Real typing activity for the core: on while text is being entered,
            // off 900 ms after the last edit or as soon as the field is empty.
            onTextChanged: {
                var restored = composer.conversationBound && text === bridge.draftText    // a restored draft, not typing
                if (composer.conversationBound && !restored) bridge.setDraftText(text)
                if (text.length === 0) { composingQuiet.stop(); bridge.setComposing(false) }
                else if (!restored) { bridge.setComposing(true); composingQuiet.restart() }
            }
        }
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; width: 6 }
    }
    Timer { id: composingQuiet; interval: 900; onTriggered: bridge.setComposing(false) }

    // Send: a compact technical key - a 34 px chamfered square of deep navy
    // with a thin cool-blue perimeter and a pale-blue line glyph, inside a
    // 44 px target. Dimmed and inert while nothing can be sent. M2: the
    // pointer lifts it a step with a soft glow and a fine ring; a press sets
    // it down. While JARVIS runs it IS the stop key in the same spot - an
    // amber square inside a turning ring - and stops the running call (a task pauses).
    Item {
        id: sendButton
        objectName: composer.sendName
        anchors.right: parent.right; anchors.rightMargin: 4; anchors.verticalCenter: parent.verticalCenter
        width: 44; height: 44
        readonly property bool stopping: composer.conversationBound && bridge.openRunning
        // DFRESUME: after Stop cut a reply, an empty composer's key continues it; typed text wins.
        // DFPAUSE: on a running task Stop pauses it at its step boundary; a paused last task's
        // empty-composer key resumes it. Typed text is always a new request, never the task's.
        readonly property bool resumingTask: composer.conversationBound && !stopping && !composer.blocked
                                             && bridge.taskPaused && input.text.trim() === ""
        readonly property bool resuming: resumingTask || (composer.conversationBound && !stopping && !composer.blocked
                                         && bridge.canResume && input.text.trim() === "")
        enabled: stopping || resuming || composer.canSend
        opacity: enabled ? 1 : 0.42
        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        function act() {
            if (stopping) { if (!bridge.cancelText()) bridge.pauseTask() }
            else if (resumingTask) bridge.resumePausedTask()
            else if (resuming) bridge.resumeText()
            else composer.submit()
        }
        Accessible.role: Accessible.Button
        Accessible.name: stopping ? qsTr("Stop") : resuming ? qsTr("Resume") : qsTr("Send message")
        Accessible.onPressAction: act()
        Keys.onReturnPressed: act()
        Keys.onSpacePressed: act()
        readonly property bool hovered: sendArea.containsMouse
        readonly property bool pressed: sendArea.pressed
        readonly property color tone: stopping ? Theme.warning : "#3596cf"
        readonly property color rim: stopping ? Theme.warning : pressed ? "#2f7fb0" : hovered ? "#62c8f2" : "#3596cf"
        readonly property color face: pressed ? "#03101d" : hovered ? "#0c2a48" : "#071c33"
        // Hover: a soft glow behind the key.
        Rectangle { objectName: "composerSendGlow"; anchors.centerIn: parent; width: 42; height: 42; radius: 8
                    color: Theme.alpha(sendButton.tone, 0.16); opacity: sendButton.hovered && sendButton.enabled ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
        Item {
            objectName: "composerSendFace"
            anchors.fill: parent
            // Lift under the pointer, set down by a press.
            transform: Translate { y: sendButton.pressed ? 0 : sendButton.hovered && sendButton.enabled ? -1 : 0
                                   Behavior on y { NumberAnimation { duration: Theme.hoverMs } } }
            scale: sendButton.pressed ? 0.95 : 1
            Behavior on scale { NumberAnimation { duration: Theme.hoverMs } }
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
            }
            // Hover: a fine ring around the face.
            Rectangle { objectName: "composerSendRing"; visible: sendButton.hovered && sendButton.enabled && !sendButton.activeFocus
                        anchors.centerIn: parent; width: 40; height: 40; radius: 6; color: "transparent"
                        border.width: 1; border.color: Theme.alpha(sendButton.tone, 0.55) }
            // Pressed: a darker inset along the top-left inside the perimeter.
            Rectangle { visible: sendButton.pressed; anchors.centerIn: parent; width: 30; height: 30; color: "transparent"
                border.width: 1; border.color: "#66000a14" }
            // The glyph: an 18 px line send arrow.
            Shape {
                visible: !sendButton.stopping && !sendButton.resuming
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
            // Resume: an 18 px line play triangle (DFRESUME).
            Shape {
                objectName: "composerResumeGlyph"
                visible: sendButton.resuming
                anchors.centerIn: parent; anchors.horizontalCenterOffset: 1
                width: 18; height: 18
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: sendButton.hovered ? "#d6f1ff" : "#b8e7ff"; strokeWidth: 1.5; fillColor: "transparent"
                    joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
                    startX: 4; startY: 2.5
                    PathLine { x: 15.5; y: 9 } PathLine { x: 4; y: 15.5 } PathLine { x: 4; y: 2.5 }
                }
            }
            // Stop: the amber square inside a turning ring (M2). The turn is a documented
            // K2 rhythm: it loops only while a run is active and is static with ambient off.
            Rectangle { objectName: "composerStopSquare"; visible: sendButton.stopping; anchors.centerIn: parent
                        width: 10; height: 10; radius: 2; color: Theme.warning }
            Shape {
                objectName: "composerStopRing"
                visible: sendButton.stopping
                anchors.centerIn: parent; width: 24; height: 24
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: Theme.warning; strokeWidth: 1.6; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                    PathAngleArc { centerX: 12; centerY: 12; radiusX: 11; radiusY: 11; startAngle: -90; sweepAngle: 270 }
                }
                RotationAnimation on rotation { from: 0; to: 360; duration: Theme.dur(1200); loops: Animation.Infinite
                                                running: sendButton.stopping && Theme.ambient }
            }
        }
        // Keyboard focus: a fine explicit outline around the face.
        Rectangle { visible: sendButton.activeFocus; anchors.centerIn: parent; width: 40; height: 40; radius: 4; color: "transparent"
            border.width: 1; border.color: "#6fd8f5" }
        MouseArea { id: sendArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: sendButton.act() }
    }
    // A sibling, so it also tells why a disabled key is off.
    HoverTip { objectName: composer.sendName + "Tip"; target: sendButton; text: sendButton.resuming ? qsTr("Resume") : composer.sendTip }
    }

    // C1: the row under the bar - "+" and the permission mode on the left, the
    // working line beside them while JARVIS runs; Model and Effort on the right.
    Item {
        id: underRow
        objectName: "composerChipRow"
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: 32
        Row {
            id: leftChips
            anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
            spacing: 6
            // "+": attach a file or an image the person picks themselves. It grants
            // JARVIS no filesystem access of any kind - only the exact item chosen in
            // this dialog is read, once, and copied into the draft.
            Item {
                id: attachChip
                objectName: "attachControl"
                width: 32; height: 32
                enabled: !bridge.openRunning && !bridge.configRefused && !composer.readOnly
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
                MenuSurface {
                    id: attachMenu
                    objectName: "attachMenu"
                    trigger: attachChip; takesFocus: false
                    restY: -height - 8; x: 0
                    width: 240
                    Column {
                        width: attachMenu.availableWidth
                        spacing: 2
                        Repeater {
                            model: [{ id: "file", label: qsTr("Add file") }, { id: "image", label: qsTr("Add image") }]
                            delegate: MenuRow {
                                objectName: "attach_" + modelData.id
                                label: modelData.label
                                onTriggered: { attachMenu.close(); composer.pick(modelData.id) }
                            }
                        }
                        Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.caption
                               text: qsTr("Only the item you choose is read. JARVIS gets no access to your files.") }
                    }
                }
            }

            // Permission mode: Core's own approval policy (``permission_mode``), shown
            // and changed here. The label is what the stored configuration holds; a
            // choice is written through the same config path the CLI uses and Core
            // applies it on the next action. It never overrides an always-approval
            // action, UNKNOWN_OUTCOME, the execution journal or Freeze.
            Item {
                id: permissionChip
                objectName: "permissionModeControl"
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
                // K4: the one menu surface and row. A mouse-only chip: the draft keeps the focus.
                MenuSurface {
                    id: modeMenu
                    objectName: "permissionModeMenu"
                    trigger: permissionChip; takesFocus: false
                    restY: -height - 8; x: 0
                    width: 360; padding: 10
                    Column {
                        width: modeMenu.availableWidth
                        spacing: 4
                        Text { text: qsTr("PERMISSION MODE"); color: Theme.textMuted
                               font: Theme.label }
                        Repeater {
                            model: composer.permissionModes
                            delegate: MenuRow {
                                objectName: "permissionMode_" + modelData.id
                                // The drawn check is not text: the selected state is told to assistive tech here.
                                Accessible.role: Accessible.RadioButton
                                label: modelData.name; detail: modelData.detail
                                current: bridge.permissionMode === modelData.id; checked: current
                                onTriggered: { bridge.setPermissionMode(modelData.id); modeMenu.close() }
                            }
                        }
                        Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.caption
                               text: qsTr("Mail, GitHub, browser, desktop, file writes and Claude analysis always ask, in every mode. Freeze is separate and stops everything.") }
                    }
                }
            }
            ProjectPicker { visible: composer.projectPicker; anchors.verticalCenter: parent.verticalCenter }
        }
        Text {
            id: working
            objectName: "composerWorking"
            visible: sendButton.stopping
            anchors.left: leftChips.right; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, Math.min(implicitWidth, rightChips.x - x - 8)); elide: Text.ElideRight
            text: bridge.taskPausing ? qsTr("Stopping: when this step ends") : qsTr("JARVIS is working…")
            color: Theme.warning; renderType: Theme.textRender; font: Theme.caption
        }
        // R1: JARVIS works for ANOTHER conversation. One run at a time: a send here waits in
        // the queue (DFQUEUE), and a mouse-first link opens the conversation it works for.
        Row {
            id: elsewhere
            objectName: "composerElsewhere"
            visible: bridge.busy && !sendButton.stopping
            anchors.left: leftChips.right; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter
            spacing: 10
            Text { objectName: "composerElsewhereReason"; anchors.verticalCenter: parent.verticalCenter
                   width: Math.max(0, Math.min(implicitWidth, rightChips.x - elsewhere.x - goRunning.width - 18)); elide: Text.ElideRight
                   text: qsTr("JARVIS is working in another conversation"); color: Theme.warning; renderType: Theme.textRender; font: Theme.caption }
            Text {
                id: goRunning
                objectName: "composerElsewhereOpen"
                anchors.verticalCenter: parent.verticalCenter
                text: qsTr("Go to that conversation")
                color: goArea.containsMouse ? Theme.text : Theme.accent; renderType: Theme.textRender; font: Theme.caption
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                Accessible.role: Accessible.Link
                Accessible.name: text
                Accessible.onPressAction: bridge.openRunningConversation()
                MouseArea { id: goArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor; onClicked: bridge.openRunningConversation() }
            }
        }
        Row {
            id: rightChips
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            spacing: 2
            ModelMenu { id: modelMenu }
            EffortMenu {
                levels: modelMenu.levels; value: modelMenu.effort; recommended: modelMenu.recommended
                onPicked: function(level) { modelMenu.setEffort(level) }
            }
            // 6G U2/U3: the provider-reported usage, right of Model and Effort; its
            // popover opens above it. Only what was reported (bridge.usageLimits).
            UsageRing {
                anchors.verticalCenter: parent.verticalCenter
                limits: bridge.usageLimits || ({})
            }
        }
    }
}
