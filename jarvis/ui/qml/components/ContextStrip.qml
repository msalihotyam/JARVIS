import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The draft's context, above the composer (Quoting v2, Q3' / Q10): ONE CARD
// PER QUOTE - its letter, its kind ("Comment" when the person wrote one,
// "Quote" otherwise), the comment or else the quoted words themselves, faint
// behind a left bar, and the files that quote owns. The cards wrap: a full row
// opens a new one and, the composer being anchored at its bottom, the card area
// grows UPWARD; nothing scrolls sideways. With two or more quotes the answer
// mode sits at the end of the cards. Then the files that belong to the message
// as a whole - a long paste among them, as its pasted-text card (LP1).
//
// A click on a card opens its detail (QuoteDetail: the whole quote, edit,
// delete); on hover the paperclip adds a file, an image or the clipboard to
// that quote, the pencil writes the comment in place and the X removes it. Every change is addressed by the group's stable id. It is all
// context - nothing here is sent by itself, asks JARVIS to do anything or
// grants any authority.
//
// Rebuilds are kept to real membership changes: the cards rebuild only when a
// group is added or removed (or a comment is committed elsewhere); a file
// added to a group refreshes that card's files; typing a comment in place or
// the answer mode rebuilds nothing, so the editor being typed in is never
// destroyed under the person.
Item {
    id: strip
    readonly property var groups: bridge.contextGroups || []
    readonly property var globals: bridge.globalAttachments || []
    readonly property int cardWidth: 248
    signal pickRequested(string kind, string groupId)
    // LP2: a pasted-text card's pencil - the composer puts the text back into the draft.
    signal unpasteRequested(var file)
    visible: groups.length > 0 || globals.length > 0
    height: visible ? body.height + 10 : 0

    QuoteDetail { id: detail }

    Column {
        id: body
        width: parent.width
        spacing: 6

        Flow {
            id: cards
            objectName: "quoteCards"
            visible: strip.groups.length > 0
            width: parent.width
            spacing: 8

            Repeater {
                model: strip.groups
                delegate: Rectangle {
                    id: card
                    objectName: "contextGroup_" + modelData.id
                    readonly property string groupId: modelData.id
                    readonly property var ref: modelData.reference || ({})
                    // The comment this card edits in place: the delegate's OWN value,
                    // read once when the card appears, so the editor is never rebound
                    // mid-typing.
                    property string noteText: modelData.note || ""
                    readonly property bool commented: noteText !== ""
                    // This quote's files. Re-read when ANY attachment changes; the card
                    // itself (and its comment editor) stays.
                    readonly property var files: { bridge.attachmentRevision; return bridge.groupAttachments(card.groupId) }
                    width: strip.cardWidth
                    height: content.height + 20
                    radius: 10
                    color: cardHover.hovered ? Theme.alpha(Theme.text, 0.07) : Theme.alpha(Theme.text, 0.04)
                    border.width: 1; border.color: Theme.alpha("#278dce", cardHover.hovered ? 0.45 : 0.24)
                    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                    HoverHandler { id: cardHover }
                    // The card itself opens the detail; its own controls take their clicks first.
                    MouseArea { objectName: "contextGroupOpen_" + card.groupId; anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor; onClicked: detail.openFor(card.groupId) }

                    Column {
                        id: content
                        x: 10; y: 10; width: parent.width - 20
                        spacing: 6
                        Item {
                            width: parent.width; height: 22
                            // The quote's letter: the name it is answered under.
                            Rectangle {
                                id: badge
                                objectName: "contextGroupLabel_" + card.groupId
                                property string letter: modelData.label
                                width: 22; height: 22; radius: 11
                                color: Theme.alpha("#36aaff", 0.2); border.width: 1; border.color: Theme.alpha("#49bdfa", 0.6)
                                Text { anchors.centerIn: parent; text: modelData.label; color: Theme.text; font: Theme.captionStrong }
                            }
                            // Q10: its kind.
                            Rectangle {
                                objectName: "contextGroupKind_" + card.groupId
                                readonly property string kind: card.commented ? "comment" : "quote"
                                x: badge.width + 8; anchors.verticalCenter: parent.verticalCenter
                                width: kindText.width + 12; height: 18; radius: 4
                                color: card.commented ? Theme.alpha(Theme.accent, 0.16) : Theme.alpha(Theme.textMuted, 0.18)
                                Text { id: kindText; anchors.centerIn: parent; text: detail.kindName(card.commented)
                                       color: card.commented ? Theme.text : Theme.textLabel; font: Theme.caption }
                            }
                            Row {
                                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                                spacing: 4
                                // Shown on hover, and while one of them has the keyboard focus (never a hidden focus).
                                opacity: cardHover.hovered || noteInput.activeFocus || groupMenu.visible
                                         || attachToggle.activeFocus || pencil.activeFocus || remove.activeFocus ? 1 : 0
                                Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
                                // Files for THIS quote, and only this quote - the clipboard included.
                                IconButton {
                                    id: attachToggle
                                    objectName: "contextGroupAttach_" + card.groupId
                                    width: 24; height: 24; icon: "paperclip"; active: groupMenu.visible
                                    tip: qsTr("Attach a file or an image to this group")
                                    onClicked: groupMenu.visible ? groupMenu.close()
                                               : groupMenu.popupAt(attachToggle, Qt.point(0, attachToggle.height + 4), { trigger: attachToggle })
                                    // K4: the one menu surface and row.
                                    MenuSurface {
                                        id: groupMenu
                                        objectName: "contextGroupMenu_" + card.groupId
                                        property bool attachable: false
                                        onAboutToShow: attachable = bridge.clipboardAttachable()
                                        width: 220; padding: 6
                                        Column {
                                            width: groupMenu.availableWidth
                                            MenuRow { objectName: "groupAddFile_" + card.groupId; label: qsTr("Add file")
                                                      onTriggered: { groupMenu.close(); strip.pickRequested("file", card.groupId) } }
                                            MenuRow { objectName: "groupAddImage_" + card.groupId; label: qsTr("Add image")
                                                      onTriggered: { groupMenu.close(); strip.pickRequested("image", card.groupId) } }
                                            MenuRow { objectName: "groupPaste_" + card.groupId; label: qsTr("Paste from clipboard")
                                                      available: groupMenu.attachable
                                                      onTriggered: { groupMenu.close(); bridge.attachClipboardToGroup(card.groupId) } }
                                        }
                                    }
                                }
                                // Write the comment in place, on the card.
                                IconButton {
                                    id: pencil
                                    objectName: "contextGroupNoteToggle_" + card.groupId
                                    width: 24; height: 24; icon: "pencil"
                                    tip: qsTranslate("QuoteDetail", "Edit")
                                    onClicked: { noteBox.before = noteInput.text; noteBox.open = true; noteInput.forceActiveFocus() }
                                }
                                IconButton {
                                    id: remove
                                    objectName: "contextGroupRemove_" + card.groupId
                                    width: 24; height: 24; icon: "close"; danger: true
                                    tip: qsTr("Remove this group")
                                    onClicked: bridge.removeGroup(card.groupId)
                                }
                            }
                        }
                        // No comment: the quoted words themselves, faint behind a left bar.
                        Item {
                            visible: !card.commented
                            width: parent.width; height: quoteText.height
                            Rectangle { width: 2; height: parent.height; radius: 1; color: Theme.alpha(Theme.textMuted, 0.5) }
                            Text {
                                id: quoteText
                                objectName: "contextGroupText_" + card.groupId
                                renderType: Theme.textRender
                                x: 8; width: parent.width - 8
                                text: String(card.ref.text || "").replace(/\s+/g, " ")
                                textFormat: Text.PlainText; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                                color: Theme.text; opacity: 0.62
                                font: Theme.caption
                            }
                        }
                        // The comment, written straight to the group as it is typed; it
                        // reads as the card's text and is edited where it stands.
                        Item {
                            id: noteBox
                            property bool open: false
                            property string before: ""      // the comment as it was when the pencil opened it
                            visible: open || card.commented
                            // Enter keeps what was typed (it is already on the group); Esc puts
                            // back the comment from before. Either way the edit closes.
                            function finish(keep) {
                                if (!keep)
                                    noteInput.text = before
                                open = false
                                noteInput.focus = false
                            }
                            width: parent.width; height: 30
                            TextField {
                                id: noteInput
                                objectName: "contextGroupNote_" + card.groupId
                                width: parent.width; height: 30; leftPadding: 6; rightPadding: 6
                                // Set once, never bound: a binding would rewrite the field (and
                                // move the caret) whenever the draft changed.
                                Component.onCompleted: text = card.noteText
                                placeholderText: qsTr("What should JARVIS do with this part?")
                                placeholderTextColor: Theme.textDim; color: Theme.text
                                selectByMouse: true; font: Theme.caption
                                // Each keystroke reaches the draft at once, addressed by the group's
                                // stable id, so sending mid-typing carries the latest text.
                                onTextChanged: { card.noteText = text; bridge.setGroupNote(card.groupId, text) }
                                // An ordinary text editor: its Paste is text only, never an attachment.
                                ContextMenu.menu: null      // noteMenu instead
                                // Reached by Tab instead of the pencil: the edit starts the same way.
                                onActiveFocusChanged: if (activeFocus && !noteBox.open) { noteBox.before = text; noteBox.open = true }
                                Keys.onReturnPressed: noteBox.finish(true)
                                Keys.onEnterPressed: noteBox.finish(true)
                                // Esc is the field's, before the window's own Escape chain sees it.
                                Keys.onShortcutOverride: function (event) { if (event.key === Qt.Key_Escape) event.accepted = true }
                                Keys.onEscapePressed: noteBox.finish(false)
                                Keys.onMenuPressed: noteMenu.openAt(noteInput, Qt.point(cursorRectangle.x, cursorRectangle.y + cursorRectangle.height))
                                background: Rectangle { radius: 6; color: noteInput.activeFocus ? Theme.surfaceDeep : "transparent"; border.width: 1
                                                        border.color: noteInput.activeFocus ? Theme.commandEdge : "transparent" }
                            }
                            // At rest the comment is the card's text: a click opens the detail
                            // like the rest of the card; the pencil edits it in place.
                            MouseArea { anchors.fill: noteInput; visible: !noteInput.activeFocus
                                        cursorShape: Qt.PointingHandCursor; onClicked: detail.openFor(card.groupId) }
                        }
                        // This quote's files: the same cards as everywhere else, thumbnails included.
                        Flow {
                            id: fileRow
                            objectName: "contextGroupFiles_" + card.groupId
                            visible: card.files.length > 0
                            width: parent.width
                            spacing: 6
                            Repeater { model: card.files
                                       AttachmentCard { file: modelData; removable: true; compact: true; nested: true; onOpened: bridge.previewAttachment(modelData) } }
                        }
                    }
                    EditMenu { id: noteMenu; objectName: "noteEditMenu_" + card.groupId; editor: noteInput }
                }
            }

            // How the quotes are answered, at the end of the cards once there are two or more.
            Row {
                objectName: "responseModeRow"
                visible: strip.groups.length > 1
                height: visible ? 26 : 0
                spacing: 6
                Repeater {
                    model: [{ id: "together", label: qsTr("Evaluate together") },
                            { id: "separate", label: qsTr("Reply to each group separately") }]
                    Rectangle {
                        objectName: "responseMode_" + modelData.id
                        readonly property bool on: bridge.responseMode === modelData.id
                        width: label.width + 20; height: 26; radius: Theme.radiusSmall
                        // Selected: the stable fill and edge; hover: a small tonal lift, no glow.
                        color: on ? Theme.alpha(Theme.accent, 0.16) : modeArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
                        border.width: 1; border.color: on ? Theme.alpha(Theme.accent, 0.5) : Theme.border
                        Text { id: label; anchors.centerIn: parent; text: modelData.label; color: Theme.text
                               font: on ? Theme.captionStrong : Theme.caption }
                        MouseArea { id: modeArea; anchors.fill: parent; hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor; onClicked: bridge.setResponseMode(modelData.id) }
                    }
                }
            }
        }

        // Files that belong to the message as a whole, not to any quote.
        Column {
            objectName: "globalAttachments"
            visible: strip.globals.length > 0
            width: parent.width
            spacing: 4
            Text { visible: strip.groups.length > 0; text: qsTr("Attached to the message"); color: Theme.textMuted
                   font: Theme.caption }
            Flow {
                width: parent.width; spacing: 6
                Repeater { model: strip.globals
                           AttachmentCard { file: modelData; removable: true; editable: true
                                            onOpened: bridge.previewAttachment(modelData)
                                            onEdited: strip.unpasteRequested(modelData) } }
            }
        }
    }
}
