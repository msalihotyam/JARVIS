import QtQuick
import QtQuick.Layouts

// FACEBAR1: the bottom bar of canvas frame 5 (window 640, full screen 660, ring box 460 = variant "mini").
// It draws face.composer and calls face slots; every decision (canSend, labels, corrections) is the model's.
Item {
    id: bar
    property string variant: "window"                               // window | full | mini
    signal attachRequested()                                        // the QML contract has no attach slot yet
    readonly property var t: face.tokens
    readonly property var c: face.composer
    readonly property string phase: typeof c.state === "string" ? c.state : ""
    readonly property bool listening: phase === "listening"
    readonly property bool transcribing: phase === "transcribing"
    readonly property bool still: t.motion_medium === 0
    readonly property real hit: t.type_control_line + 2 * t.space_12       // 44 px hit target
    readonly property int maxLines: 2 * 2 * 2
    readonly property bool canSend: c.canSend === true && face.link !== "unreachable"
    readonly property string given: typeof c.text === "string" ? c.text : ""
    readonly property string fixes: summary(c.corrections)
    property bool syncing: false                                    // true while the model's text is written in
    property bool ready: false                                      // TextEdit reports its load-time text unguarded
    implicitHeight: col.implicitHeight
    onGivenChanged: sync()
    Component.onCompleted: { ready = true; sync() }

    // Qt may normalise the model text (CRLF, CR), so only edits made outside load and sync() reach textEdited
    function sync() {
        if (edit.text === given)
            return
        syncing = true
        edit.text = given
        syncing = false
    }

    // "raw → fixed, ..." from [raw, fixed] pairs; anything else is skipped, never trusted
    function summary(list) {
        var out = []
        if (list && typeof list === "object" && list.length > 0)
            for (var i = 0; i < list.length; i++) {
                var p = list[i]
                if (p && typeof p === "object" && typeof p[0] === "string" && typeof p[1] === "string")
                    out.push(p[0] + " → " + p[1])
            }
        return out.join(", ")
    }

    component TextButton: Item {
        id: tb
        property string label
        property string accessibleName: label
        property real size: face.tokens.type_control_size
        signal activated()
        implicitWidth: Math.max(implicitHeight, glyph.implicitWidth + 2 * face.tokens.space_8)
        implicitHeight: face.tokens.type_control_line + 2 * face.tokens.space_12
        Accessible.role: Accessible.Button
        Accessible.name: accessibleName
        Text {
            id: glyph
            anchors.centerIn: parent
            text: tb.label
            textFormat: Text.PlainText
            color: face.tokens.color_text2
            font.pixelSize: tb.size
        }
        MouseArea {
            anchors.fill: parent
            onClicked: tb.activated()
        }
    }

    Column {
        id: col
        width: parent.width
        spacing: bar.t.space_8
        Text {
            objectName: "linkNotice"
            visible: face.link === "unreachable"
            x: bar.t.space_14
            text: "Predator'a ulaşılamıyor"
            textFormat: Text.PlainText
            color: bar.t.color_text2
            font.pixelSize: bar.t.type_small_size
        }
        Item {
            id: frame
            width: parent.width
            height: body.height
            Repeater {                                              // soft shadow: two faint layers under the body
                model: 2
                Rectangle {
                    required property int index
                    x: -index
                    y: index + 1
                    width: frame.width + 2 * index
                    height: frame.height + index
                    radius: bar.t.radius_composer + index
                    color: Qt.darker(bar.t.color_base, 2)
                    opacity: (2 - index) / (2 * 2)
                }
            }
            Rectangle {
                id: body
                objectName: "composerBody"
                width: parent.width
                height: inner.implicitHeight
                radius: bar.t.radius_composer
                color: bar.t.color_surface1
                border.width: 1
                border.color: bar.t.color_line2
                Rectangle {                                         // faint top highlight
                    objectName: "composerHighlight"
                    x: bar.t.radius_composer
                    y: 1
                    width: parent.width - 2 * x
                    height: 1
                    color: bar.t.color_text3
                    opacity: 1 / (2 * 2 * 2)
                }
                Item {                                              // transcribing: a 2 px line running along the top
                    id: track
                    objectName: "transcribingLine"
                    readonly property bool moving: run.running
                    visible: bar.transcribing
                    x: bar.t.radius_composer
                    y: 1
                    width: parent.width - 2 * x
                    height: 2
                    clip: true
                    Rectangle {
                        id: runner
                        objectName: "transcribingRunner"
                        width: bar.still ? track.width : track.width / 2
                        height: 2
                        radius: 1
                        color: bar.t.color_text3
                        NumberAnimation on x {
                            id: run
                            running: track.visible && !bar.still
                            onRunningChanged: if (!running) runner.x = 0      // a stopped run leaves x mid-way
                            loops: Animation.Infinite
                            from: -runner.width
                            to: track.width
                            duration: bar.t.motion_slow * 2 * 2
                            easing.type: Easing.BezierSpline
                            easing.bezierCurve: [bar.t.motion_curve_x1, bar.t.motion_curve_y1,
                                                 bar.t.motion_curve_x2, bar.t.motion_curve_y2, 1, 1]
                        }
                    }
                }
                Column {
                    id: inner
                    width: parent.width
                    Item {
                        objectName: "understoodStrip"
                        visible: bar.phase === "understood"
                        width: parent.width
                        height: bar.hit
                        RowLayout {
                            x: bar.t.space_14
                            width: parent.width - x - bar.t.space_8
                            height: parent.height
                            spacing: bar.t.space_8
                            Text {
                                text: "Anladığım"
                                textFormat: Text.PlainText
                                color: bar.t.color_text3
                                font.pixelSize: bar.t.type_label_size
                            }
                            Text {
                                objectName: "correctionSummary"
                                Layout.fillWidth: true
                                text: bar.fixes
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                color: bar.t.color_text2
                                font.pixelSize: bar.t.type_small_size
                            }
                            TextButton {
                                objectName: "undoCorrections"
                                label: "Düzeltmeleri geri al"
                                onActivated: face.undoCorrections()
                            }
                        }
                        Rectangle {
                            anchors.bottom: parent.bottom
                            x: bar.t.space_14
                            width: parent.width - 2 * x
                            height: 1
                            color: bar.t.color_line1
                        }
                    }
                    Item {
                        id: entry
                        width: parent.width
                        height: (bar.listening || bar.transcribing ? bar.hit : field.height) + bar.t.space_12
                        Flickable {                                 // grows to 8 lines, then scrolls inside
                            id: field
                            objectName: "composerScroll"
                            readonly property real lineH: edit.contentHeight / Math.max(1, edit.lineCount)
                            visible: !bar.listening && !bar.transcribing
                            enabled: visible                // an invisible item keeps key focus; disabled it gets no input
                            x: bar.t.space_14
                            y: bar.t.space_12
                            width: parent.width - 2 * x
                            height: Math.max(bar.hit, Math.min(Math.max(1, edit.lineCount), bar.maxLines) * lineH)
                            contentWidth: width
                            contentHeight: edit.height
                            clip: true
                            boundsBehavior: Flickable.StopAtBounds
                            function follow(r) {
                                if (contentY >= r.y)
                                    contentY = r.y
                                else if (contentY + height <= r.y + r.height)
                                    contentY = r.y + r.height - height
                            }
                            TextEdit {
                                id: edit
                                objectName: "composerText"
                                width: field.width
                                height: Math.max(field.height, contentHeight)
                                textFormat: TextEdit.PlainText
                                wrapMode: TextEdit.Wrap
                                verticalAlignment: TextEdit.AlignVCenter
                                color: bar.t.color_text1
                                selectionColor: bar.t.color_line2
                                selectedTextColor: bar.t.color_text1
                                selectByMouse: true
                                font.pixelSize: bar.t.type_turn_size
                                Accessible.role: Accessible.EditableText
                                Accessible.name: "Mesaj"
                                onCursorRectangleChanged: field.follow(cursorRectangle)
                                onTextChanged: if (bar.ready && !bar.syncing) face.textEdited(text)
                                Keys.onPressed: (event) => {
                                    if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                                            && (event.modifiers & ~Qt.KeypadModifier) === Qt.NoModifier) {
                                        event.accepted = true       // plain Enter never adds a line
                                        if (bar.canSend && !event.isAutoRepeat)   // a held Enter sends once
                                            face.send()
                                    }
                                }
                            }
                        }
                        Text {
                            objectName: "composerPlaceholder"
                            visible: field.visible && edit.length === 0
                            x: field.x
                            y: field.y
                            width: field.width
                            height: bar.hit
                            verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight
                            text: "JARVIS'e yaz ya da Konuş'a tıkla"
                            textFormat: Text.PlainText
                            color: bar.t.color_text3
                            font.pixelSize: bar.t.type_turn_size
                        }
                        Row {
                            objectName: "listeningRow"
                            visible: bar.listening
                            x: bar.t.space_14
                            y: bar.t.space_12
                            height: bar.hit
                            spacing: bar.t.space_12
                            Text {
                                id: hearing
                                anchors.verticalCenter: parent.verticalCenter
                                text: "Dinliyorum"
                                textFormat: Text.PlainText
                                color: bar.t.color_text1
                                font.pixelSize: bar.t.type_turn_size
                            }
                            Waveform {
                                objectName: "waveform"
                                anchors.verticalCenter: parent.verticalCenter
                                width: entry.width - 2 * bar.t.space_14 - hearing.width - bar.t.space_12
                            }
                        }
                        Text {
                            objectName: "transcribingLabel"
                            visible: bar.transcribing
                            x: bar.t.space_14
                            y: bar.t.space_12
                            height: bar.hit
                            verticalAlignment: Text.AlignVCenter
                            text: "Yazıya çevriliyor…"
                            textFormat: Text.PlainText
                            color: bar.t.color_text2
                            font.pixelSize: bar.t.type_turn_size
                        }
                    }
                    Item {                                          // tool row: padding 8 / 10 / 10, gap 6
                        width: parent.width
                        height: bar.hit + bar.t.space_8 + bar.t.space_10
                        RowLayout {
                            objectName: "toolRow"
                            x: bar.t.space_10
                            y: bar.t.space_8
                            width: parent.width - 2 * x
                            height: bar.hit
                            spacing: bar.t.space_6
                            TalkButton {
                                objectName: "talkButton"
                            }
                            TextButton {
                                objectName: "cancelTalk"
                                visible: bar.listening
                                label: "Vazgeç"
                                onActivated: face.talkCancel()
                            }
                            TextButton {
                                objectName: "attachButton"
                                visible: face.attachAvailable === true && bar.variant !== "mini"
                                label: "+"
                                accessibleName: "Ekle"
                                size: bar.t.type_h2_size
                                onActivated: bar.attachRequested()
                            }
                            Item {
                                Layout.fillWidth: true
                            }
                            ModeSwitch {
                                objectName: "modeSwitch"
                                visible: !(bar.variant === "mini" && bar.listening)   // 460 px: Bitir + Vazgeç + Send only
                            }
                            SendButton {
                                objectName: "sendButton"
                            }
                        }
                    }
                }
            }
        }
    }
}
