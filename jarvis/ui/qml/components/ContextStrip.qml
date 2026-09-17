import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The draft's context, above the composer: one GROUP per quoted message or
// selection, each owning its own note and its own files, then the files that
// belong to the message as a whole.
//
// Ownership is shown, not implied: a group's files sit indented inside that
// group, under its letter, so it is plain which file belongs to which quote.
// Every change is addressed by a stable id. It is all context - nothing here is
// sent by itself, asks JARVIS to do anything or grants any authority.
//
// Rebuilds are kept to real membership changes: the group cards rebuild only
// when a group is added or removed; a file added to a group refreshes that
// group's file row; a note or the answer mode rebuilds nothing, so the editor
// someone is typing in is never destroyed under them.
Item {
    id: strip
    readonly property var groups: bridge.contextGroups || []
    readonly property var globals: bridge.globalAttachments || []
    signal pickRequested(string kind, string groupId)
    visible: groups.length > 0 || globals.length > 0
    height: visible ? body.height + 10 : 0

    // One attached file: its kind, its name, and a remove control.
    component FileChip: Rectangle {
        id: chip
        property var file: ({})
        objectName: "attachment_" + file.id
        width: chipRow.width + 18; height: 24; radius: 6
        color: Theme.alpha(Theme.text, 0.05)
        border.width: 1; border.color: Theme.alpha(Theme.text, 0.08)
        Row {
            id: chipRow
            x: 9; anchors.verticalCenter: parent.verticalCenter; spacing: 7
            Text { anchors.verticalCenter: parent.verticalCenter; color: Theme.materialBlue
                   text: chip.file.kind === "image" ? qsTr("Image") : qsTr("File")
                   font: Qt.font({ family: Theme.font, pixelSize: 10, weight: Font.DemiBold }) }
            Text { anchors.verticalCenter: parent.verticalCenter; color: Theme.textLabel
                   text: chip.file.name || ""; elide: Text.ElideMiddle; width: Math.min(implicitWidth, 180)
                   font: Qt.font({ family: Theme.font, pixelSize: 12 }) }
            Text {
                objectName: "attachmentRemove_" + chip.file.id
                anchors.verticalCenter: parent.verticalCenter
                text: "✕"; color: chipRemove.containsMouse ? Theme.danger : Theme.textMuted
                font: Qt.font({ family: Theme.font, pixelSize: 11 })
                Accessible.role: Accessible.Button
                Accessible.name: qsTr("Remove this attachment")
                MouseArea { id: chipRemove; anchors.fill: parent; anchors.margins: -5; hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor; onClicked: bridge.removeAttachment(chip.file.id) }
            }
        }
    }

    Column {
        id: body
        width: parent.width
        spacing: 6

        // How the groups are answered, offered once there is more than one group.
        Row {
            objectName: "responseModeRow"
            visible: strip.groups.length > 1
            height: visible ? 24 : 0
            spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Answer"); color: Theme.textMuted
                   font: Qt.font({ family: Theme.font, pixelSize: 11 }) }
            Repeater {
                model: [{ id: "together", label: qsTr("Evaluate together") },
                        { id: "separate", label: qsTr("Reply to each group separately") }]
                Rectangle {
                    objectName: "responseMode_" + modelData.id
                    readonly property bool on: bridge.responseMode === modelData.id
                    width: label.width + 18; height: 24; radius: Theme.radiusSmall
                    color: on ? Theme.alpha(Theme.accent, 0.16) : modeArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
                    border.width: 1; border.color: on ? Theme.alpha(Theme.accent, 0.5) : Theme.border
                    Text { id: label; anchors.centerIn: parent; text: modelData.label; color: Theme.text
                           font: Qt.font({ family: Theme.font, pixelSize: 11, weight: Font.DemiBold }) }
                    MouseArea { id: modeArea; anchors.fill: parent; hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor; onClicked: bridge.setResponseMode(modelData.id) }
                }
            }
        }

        Repeater {
            model: strip.groups
            delegate: Rectangle {
                id: card
                objectName: "contextGroup_" + modelData.id
                readonly property string groupId: modelData.id
                readonly property var ref: modelData.reference || ({})
                // The note this card edits: the delegate's OWN value, read once when
                // the card appears, so the editor is never rebound mid-typing.
                property string noteText: modelData.note || ""
                readonly property bool noted: noteText !== "" || noteBox.open
                // This group's files. Re-read when ANY attachment changes; the card
                // itself (and its note editor) stays.
                readonly property var files: { bridge.attachmentRevision; return bridge.groupAttachments(card.groupId) }
                width: strip.width
                height: head.height + (noteBox.visible ? noteBox.height + 6 : 0) + (fileRow.visible ? fileRow.height + 6 : 0) + 12
                radius: 8
                color: Theme.alpha(Theme.text, 0.035)
                border.width: 1; border.color: Theme.border

                Item {
                    id: head
                    x: 10; y: 6; width: parent.width - 20; height: 32
                    // The group's letter: the name it is answered under.
                    Rectangle {
                        id: badge
                        objectName: "contextGroupLabel_" + card.groupId
                        property string letter: modelData.label
                        width: 20; height: 20; radius: 10; y: 6
                        color: Theme.alpha("#36aaff", 0.14); border.width: 1; border.color: Theme.alpha("#49bdfa", 0.45)
                        Text { anchors.centerIn: parent; text: modelData.label; color: Theme.text
                               font: Qt.font({ family: Theme.font, pixelSize: 11, weight: Font.DemiBold }) }
                    }
                    Text {
                        objectName: "contextGroupTag_" + card.groupId
                        x: badge.width + 9; y: 0
                        text: (card.ref.source_role === "user" ? qsTr("You") : "JARVIS")
                              + " · " + (card.ref.selection === "partial" ? qsTr("selection") : qsTr("whole message"))
                        color: Theme.materialBlue; elide: Text.ElideRight
                        width: Math.min(implicitWidth, parent.width - 200)
                        font: Qt.font({ family: Theme.font, pixelSize: 10, weight: Font.DemiBold, letterSpacing: 1 })
                    }
                    Text {
                        objectName: "contextGroupText_" + card.groupId
                        x: badge.width + 9; y: 15; width: parent.width - x - 150; elide: Text.ElideRight
                        text: String(card.ref.text || "").replace(/\s+/g, " ")
                        color: Theme.textLabel
                        font: Qt.font({ family: Theme.font, pixelSize: 12 })
                    }
                    Row {
                        anchors.right: parent.right; y: 2
                        spacing: 14
                        // A note for THIS group: offered compactly, opened only when wanted.
                        Text {
                            objectName: "contextGroupNoteToggle_" + card.groupId
                            text: card.noted ? qsTr("Note") : qsTr("+ Note")
                            color: noteArea.containsMouse ? Theme.accent : Theme.textMuted
                            font: Qt.font({ family: Theme.font, pixelSize: 11 })
                            MouseArea { id: noteArea; anchors.fill: parent; anchors.margins: -5; hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: { noteBox.open = !noteBox.open; if (noteBox.open) noteInput.forceActiveFocus() } }
                        }
                        // Files for THIS group, and only this group.
                        Text {
                            id: attachToggle
                            objectName: "contextGroupAttach_" + card.groupId
                            text: qsTr("+ File")
                            color: attachArea.containsMouse || groupMenu.visible ? Theme.accent : Theme.textMuted
                            font: Qt.font({ family: Theme.font, pixelSize: 11 })
                            Accessible.role: Accessible.Button
                            Accessible.name: qsTr("Attach a file or an image to this group")
                            MouseArea { id: attachArea; anchors.fill: parent; anchors.margins: -5; hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor; onClicked: groupMenu.popup(attachToggle, 0, attachToggle.height + 4) }
                            Menu {
                                id: groupMenu
                                objectName: "contextGroupMenu_" + card.groupId
                                property bool attachable: false
                                onAboutToShow: attachable = bridge.clipboardAttachable()
                                padding: 5
                                background: Rectangle { implicitWidth: 210; radius: 8; color: Theme.alpha("#061424", 0.9)
                                                        border.width: 1; border.color: Theme.glassEdge }
                                component GroupItem: MenuItem {
                                    id: gi
                                    implicitHeight: 30; leftPadding: 12; rightPadding: 12
                                    contentItem: Text { text: gi.text; color: gi.enabled ? Theme.text : Theme.textDim
                                                        font: Qt.font({ family: Theme.font, pixelSize: 13 }); verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { radius: 5; color: gi.highlighted && gi.enabled ? Theme.alpha("#36aaff", 0.16) : "transparent" }
                                }
                                GroupItem { objectName: "groupAddFile_" + card.groupId; text: qsTr("Add file")
                                            onTriggered: strip.pickRequested("file", card.groupId) }
                                GroupItem { objectName: "groupAddImage_" + card.groupId; text: qsTr("Add image")
                                            onTriggered: strip.pickRequested("image", card.groupId) }
                                GroupItem { objectName: "groupPaste_" + card.groupId; text: qsTr("Paste from clipboard")
                                            enabled: groupMenu.attachable
                                            onTriggered: bridge.attachClipboardToGroup(card.groupId) }
                            }
                        }
                        Text {
                            objectName: "contextGroupRemove_" + card.groupId
                            text: "✕"; color: removeArea.containsMouse ? Theme.danger : Theme.textMuted
                            font: Qt.font({ family: Theme.font, pixelSize: 13 })
                            Accessible.role: Accessible.Button
                            Accessible.name: qsTr("Remove this group")
                            MouseArea { id: removeArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor; onClicked: bridge.removeGroup(card.groupId) }
                        }
                    }
                }
                Item {
                    id: noteBox
                    property bool open: false
                    visible: open || card.noteText !== ""
                    x: 39; y: head.y + head.height + 6
                    width: parent.width - 49; height: 30
                    TextField {
                        id: noteInput
                        objectName: "contextGroupNote_" + card.groupId
                        width: parent.width; height: 30; leftPadding: 10; rightPadding: 10
                        // Set once, never bound: a binding would rewrite the field (and
                        // move the caret) whenever the draft changed.
                        Component.onCompleted: text = card.noteText
                        placeholderText: qsTr("What should JARVIS do with this part?")
                        placeholderTextColor: Theme.textDim; color: Theme.text
                        selectByMouse: true; font { family: Theme.font; pixelSize: 12 }
                        // Each keystroke reaches the draft at once, addressed by the group's
                        // stable id, so sending mid-typing carries the latest text.
                        onTextChanged: { card.noteText = text; bridge.setGroupNote(card.groupId, text) }
                        // An ordinary text editor: its Paste is text only, never an attachment.
                        ContextMenu.menu: noteMenu
                        background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                                border.color: noteInput.activeFocus ? Theme.commandEdge : Theme.border }
                    }
                }
                EditMenu { id: noteMenu; objectName: "noteEditMenu_" + card.groupId; editor: noteInput }
                // This group's files, indented under its letter.
                Flow {
                    id: fileRow
                    objectName: "contextGroupFiles_" + card.groupId
                    visible: card.files.length > 0
                    x: 39; y: (noteBox.visible ? noteBox.y + noteBox.height : head.y + head.height) + 6
                    width: parent.width - 49
                    spacing: 6
                    Repeater { model: card.files; FileChip { file: modelData } }
                }
            }
        }

        // Files that belong to the message as a whole, not to any group.
        Column {
            objectName: "globalAttachments"
            visible: strip.globals.length > 0
            width: parent.width
            spacing: 4
            Text { visible: strip.groups.length > 0; text: qsTr("Attached to the message"); color: Theme.textMuted
                   font: Qt.font({ family: Theme.font, pixelSize: 11 }) }
            Flow {
                width: parent.width; spacing: 6
                Repeater { model: strip.globals; FileChip { file: modelData } }
            }
        }
    }
}
