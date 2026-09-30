import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6D-b (Salih decision 2026-09-30): the permanent delete of ONE
// conversation - the second step after its archive (or its project's). The
// preview names the exact conversation and says honestly what is removed and
// what is not; one button confirms exactly what was previewed (id, next_seq,
// title). A refusal - the conversation changed, open work - is shown as
// returned and the preview refreshed; nothing is ever retried on its own.
Popup {
    id: dialog
    objectName: "conversationDeleteDialog"
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(600, parent ? parent.width - 40 : 600); padding: 18
    modal: true; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    // The conversation this dialog was opened for; the confirm carries it.
    property string targetId: ""
    readonly property var preview: bridge.deletePreview
    readonly property bool shown: !!preview.conversation_id && preview.conversation_id === targetId
    function openFor(conversationId) { targetId = conversationId || ""; bridge.previewDeleteConversation(targetId); open() }
    onClosed: bridge.previewDeleteConversation("")
    function when(v) {
        var d = new Date(v)
        return !v ? "" : isNaN(d.getTime()) ? String(v) : d.toLocaleString(Qt.locale(i18n.qtLocale), "d MMM yyyy HH:mm")
    }

    component Line: Text {
        width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
    }

    background: Rectangle { radius: 12; color: Theme.glassOverlay; border.width: 1; border.color: Theme.glassOverlayEdge }
    contentItem: Column {
        spacing: 10
        Text { objectName: "deleteTitle"; text: qsTr("Delete this conversation permanently?"); color: Theme.text; font: Theme.uiValue }
        Line { objectName: "deleteIdentity"; visible: dialog.shown; font: Theme.pageRowStrong
               text: qsTr("«%1» · %2 messages · created %3 · last changed %4 · %5")
                     .arg(dialog.preview.title || qsTr("New Chat")).arg(dialog.preview.turns || 0)
                     .arg(dialog.when(dialog.preview.created_at)).arg(dialog.when(dialog.preview.updated_at))
                     .arg(dialog.preview.owner ? dialog.preview.owner.name : qsTr("Global")) }
        Line { objectName: "deleteNotice"; visible: text !== ""; text: bridge.conversationNotice; color: Theme.warning }
        Line { objectName: "deleteBlocked"; visible: dialog.shown && !!dialog.preview.blocked; color: Theme.warning
               text: qsTr("Cannot be deleted: %1").arg(dialog.preview.blocked || "") }
        Column {
            visible: dialog.shown && !dialog.preview.blocked
            width: parent.width; spacing: 8
            Line { objectName: "deleteRemoved"
                   text: qsTr("REMOVED: this conversation's messages and title (including the task titles kept inside the conversation) from the JARVIS conversation store. The conversation no longer appears in any list and cannot be opened.") }
            Line { objectName: "deleteKept"; color: Theme.textLabel
                   text: qsTr("KEPT: the session, task, checkpoint, approval and audit records linked to this conversation (task results stay in the Tasks list); content already sent to providers; the database's WAL file, free pages and copies that may remain in the storage layer; backups.") }
            Line { objectName: "deleteLegacy"; visible: dialog.preview.origin === "legacy-import"; color: Theme.textLabel
                   text: qsTr("The previous version's conversation file (~/.jarvis/conversation.json) also stays where it is.") }
            Line { objectName: "deleteIrreversible"; color: Theme.warning; text: qsTr("This cannot be undone.") }
        }
        Row {
            anchors.right: parent.right
            spacing: 8
            ActionButton { objectName: "deleteCancel"; label: qsTr("Cancel"); height: 36; onClicked: dialog.close() }
            ActionButton { objectName: "deleteConfirm"; label: qsTr("Delete permanently"); danger: true; primary: true; height: 36
                           visible: dialog.shown && !dialog.preview.blocked; enabled: !bridge.busy
                           onClicked: { if (bridge.confirmDeleteConversation(dialog.targetId)) dialog.close() } }
        }
    }
}
