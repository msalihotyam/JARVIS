import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Quoting v2 (Q9 / Q10): the detail of ONE quote of the draft, opened from its
// card or from its letter in the message. Titled by its kind - "Comment" when
// the person wrote one, "Quote" otherwise; the whole quoted text in a
// left-barred block, the comment under it and the files this quote owns
// (each opens the internal preview). Edit (Q6) changes the comment and the
// files in place; "Delete quote" removes the quote with everything it owns.
// On the K4 dialog shell. All of it is context for one turn - never authority.
JarvisDialog {
    id: detail
    objectName: "quoteDetail"
    property string groupId: ""
    property bool editing: false
    // A comment typed on the card in place announces nothing (bridge.setGroupNote),
    // so opening and editing read the group afresh.
    property int revision: 0
    readonly property var group: {
        revision
        var groups = bridge.contextGroups || []
        for (var i = 0; i < groups.length; i++) if (groups[i].id === groupId) return groups[i]
        return null
    }
    readonly property var ref: group ? group.reference : ({})
    readonly property bool commented: group !== null && group.note !== ""
    readonly property var files: { bridge.attachmentRevision; return group ? bridge.groupAttachments(groupId) : [] }
    preferredWidth: 560
    titleName: "quoteDetailTitle"
    label: group ? group.label + " · " + (ref.source_role === "user" ? qsTranslate("ContextStrip", "You") : "JARVIS")
                   + " · " + (ref.selection === "partial" ? qsTranslate("ContextStrip", "selection") : qsTranslate("ContextStrip", "whole message"))
                 : ""
    title: commented ? kindName(true) : kindName(false)
    initialFocus: closeButton

    // Q10: the kind a quote is shown under, the same on its card and here.
    function kindName(withComment) { return withComment ? qsTr("Comment") : qsTr("Quote") }
    function openFor(gid) { groupId = gid; revision++; editing = false; open() }
    function edit() { revision++; editor.text = group ? group.note : ""; editing = true; editor.field.forceActiveFocus() }
    function save() { editor.commit(); editing = false }
    function remove() { bridge.removeGroup(groupId); close() }
    // The quote left the draft (sent, removed elsewhere): nothing is left to show.
    onGroupChanged: if (opened && group === null) close()
    onClosed: { editing = false; groupId = "" }
    // While a comment is being edited a stray press outside does not throw it away;
    // Esc and Cancel still do.
    closePolicy: editing ? Popup.CloseOnEscape : Popup.CloseOnEscape | Popup.CloseOnPressOutside

    Item {
        objectName: "quoteDetailQuote"
        width: parent ? parent.width : 0; height: quoted.height
        Rectangle { width: 2; height: parent.height; radius: 1; color: Theme.alpha(Theme.textMuted, 0.6) }
        TextEdit { id: quoted; objectName: "quoteDetailText"; x: 12; width: parent.width - 12
                   text: String(detail.ref.text || ""); textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true
                   wrapMode: Text.Wrap; color: Theme.textLabel; selectionColor: Theme.accentSoft
                   renderType: Theme.textRender; font: Theme.body }
    }
    Text { objectName: "quoteDetailComment"; visible: detail.commented && !detail.editing
           width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText
           text: detail.group ? detail.group.note : ""; color: Theme.text; font: Theme.body }
    Flow {
        objectName: "quoteDetailFiles"
        visible: !detail.editing && detail.files.length > 0
        width: parent ? parent.width : 0; spacing: 6
        Repeater { model: detail.files
                   AttachmentCard { file: modelData; compact: true; nested: true; onOpened: bridge.previewAttachment(modelData) } }
    }
    QuoteEditor { id: editor; objectName: "quoteDetailEditor"; visible: detail.editing; showAdd: false
                  width: parent ? parent.width : 0; groupId: detail.editing ? detail.groupId : ""; onAccepted: detail.save() }

    buttons: [
        ActionButton { objectName: "quoteDetailDelete"; label: qsTr("Delete quote"); danger: true; height: 36; onClicked: detail.remove() },
        ActionButton { objectName: "quoteDetailEdit"; visible: !detail.editing; label: qsTr("Edit"); trailingIcon: "pencil"; height: 36; onClicked: detail.edit() },
        ActionButton { id: closeButton; objectName: "quoteDetailClose"; visible: !detail.editing; label: qsTranslate("TopBar", "Close"); height: 36; onClicked: detail.close() },
        ActionButton { objectName: "quoteDetailCancel"; visible: detail.editing; label: qsTranslate("RenameMenu", "Cancel"); height: 36; onClicked: detail.editing = false },
        ActionButton { objectName: "quoteDetailSave"; visible: detail.editing; label: qsTranslate("RenameMenu", "Save"); primary: true; height: 36; onClicked: detail.save() }
    ]
}
