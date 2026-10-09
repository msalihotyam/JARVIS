import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import "../theme"

// Quoting v2 (Q2' / Q9): the editor of ONE quote group - one comment line,
// the paperclip (a picked file or image, or a pasted one, owned by THIS group
// only) and, in the popup, the add key at its right; the group's thumbnails
// under it. Typing writes nothing: `commit()` stores the comment once, so Esc
// or Cancel leaves the group's note as it was. A comment is the group's note:
// context for that quote, never an instruction with any authority.
Item {
    id: editor
    property string groupId: ""
    property alias text: field.text
    property bool showAdd: true
    readonly property alias field: field
    signal accepted()
    readonly property var files: { bridge.attachmentRevision; return groupId !== "" ? bridge.groupAttachments(groupId) : [] }
    implicitHeight: line.height + (thumbs.visible ? thumbs.height + 8 : 0)

    // The comment, stored on the group; the cards re-read their groups.
    function commit() {
        if (groupId === "") return
        bridge.setGroupNote(groupId, field.text)
        bridge.groupsChanged()
    }
    // Paste: an image or files on the clipboard become THIS group's; otherwise text.
    function paste() { if (groupId === "" || bridge.attachClipboardToGroup(groupId) < 0) field.paste() }

    FileDialog {
        id: picker
        objectName: "quoteAttachDialog"
        fileMode: FileDialog.OpenFiles
        nameFilters: [qsTranslate("Composer", "Images (%1)").arg("*.png *.jpg *.jpeg *.webp"),
                      qsTranslate("Composer", "Text and code (%1)").arg("*.txt *.md *.json *.yaml *.yml *.csv *.log *.py *.js *.ts *.qml *.html *.css *.xml *.sh *.sql")]
        // Never the message itself: an empty owner would make these message-level files.
        onAccepted: { if (editor.groupId !== "") for (var i = 0; i < selectedFiles.length; i++) bridge.attachFileToGroup(editor.groupId, selectedFiles[i]) }
    }
    EditMenu { id: menu; objectName: "quoteCommentMenu"; editor: field; attachAware: true; onPasteRequested: editor.paste() }

    Item {
        id: line
        width: parent.width; height: 34
        TextField {
            id: field
            objectName: "quoteComment"
            anchors { left: parent.left; right: clip.left; rightMargin: 6; verticalCenter: parent.verticalCenter }
            height: 34; leftPadding: 10; rightPadding: 10
            placeholderText: qsTr("Add a comment (optional)…")
            placeholderTextColor: Theme.textDim; color: Theme.text
            selectByMouse: true; font: Theme.body
            Keys.onReturnPressed: editor.accepted()
            Keys.onEnterPressed: editor.accepted()
            Keys.onPressed: function(event) { if (event.matches(StandardKey.Paste)) { editor.paste(); event.accepted = true } }
            TapHandler { acceptedButtons: Qt.RightButton; onTapped: function(point) { menu.openAt(field, point.position) } }
            background: Rectangle { radius: Theme.radiusTiny; color: Theme.surfaceDeep; border.width: 1
                                    border.color: field.activeFocus ? Theme.commandEdge : Theme.border }
        }
        IconButton {
            id: clip
            objectName: "quoteAttach"
            anchors { right: add.visible ? add.left : parent.right; rightMargin: add.visible ? 4 : 0; verticalCenter: parent.verticalCenter }
            width: 30; height: 30
            icon: "paperclip"
            tip: qsTranslate("ContextStrip", "Attach a file or an image to this group")
            onClicked: picker.open()
        }
        IconButton {
            id: add
            objectName: "quoteAdd"
            visible: editor.showAdd
            anchors { right: parent.right; verticalCenter: parent.verticalCenter }
            width: 30; height: 30
            icon: "plus"; active: true
            tip: qsTranslate("FeedView", "Quote")
            onClicked: editor.accepted()
        }
    }
    // This group's files, under the line; each opens the internal preview.
    Flow {
        id: thumbs
        objectName: "quoteEditorFiles"
        visible: editor.files.length > 0
        y: line.height + 8
        width: parent.width
        spacing: 6
        Repeater { model: editor.files
                   AttachmentCard { file: modelData; removable: true; compact: true; nested: true; onOpened: bridge.previewAttachment(modelData) } }
    }
}
