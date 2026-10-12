import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6F (F3, Q8): the conversation menu - rename (6D-a), archive and the
// permanent delete (6D-b) - with its rename editor and the delete dialog,
// moved unchanged out of the former TopBar switcher. One menu, two openers:
// the chat header's "…" (the open conversation), or a sidebar row, which it
// acts on WITHOUT opening it. Every item is disabled with its
// reason (archived project or conversation, not archived yet, a busy run);
// the store still refuses. Each acts on the conversation it was opened for.
// F6: Sabitle / Sabitlemeyi kaldır (at most bridge.pinLimit, disabled with
// its reason at the limit) and, on a pinned one, Yukarı / Aşağı taşı - window
// preferences only, the conversation itself never changes.
// F7: a Global conversation is deleted permanently in one step (the same
// confirm dialog); a project's still needs its archive (or the project's).
// It holds the scope menus' shared parts (a row, a reason, a divider), which
// the Command preset's scope menu uses too. K4: both are a MenuSurface with
// MenuRow rows, the window's one menu style; only the clicked opener shows open.
// Gate 6G F9 (H1, H3): "Projeye bağla…" lives here only - Global
// conversations only, the window's one attach dialog (6C: one-way, preview,
// explicit confirm, the store's guards). A row's item opens that row's
// conversation first: the dialog previews and attaches the open one. Order:
// pin, rename, attach | archive | delete permanently last, in danger.
Item {
    id: menu
    objectName: "conversationMenu"

    // One row of the scope menus (K4: a MenuRow), disabled while a run without a
    // record (a sweep) is busy; a recorded run leaves navigation open (R1).
    component ScopeChoice: MenuRow { enabled: !bridge.navigationLocked && available }
    // A disabled item's reason, under it; and a menu's section divider.
    component Reason: Text {
        leftPadding: 12; rightPadding: 12; bottomPadding: 4
        wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.textDim; font: Theme.caption
    }
    component Divider: Item {
        height: 9
        Rectangle { y: 4; x: 8; width: parent.width - 16; height: 1; color: Theme.border }
    }

    // What the menu acts on: "" = the open conversation; else a listed row of
    // the open one's scope (never archived, the same owner: the same reasons).
    property string rowId: ""
    property string rowTitle: ""
    // A pinned row of ANOTHER scope says whether its own project is archived;
    // undefined = the open conversation's owner (a row of the open scope).
    property var rowOwnerArchived: undefined
    // ...and whether it is Global (undefined = the open conversation's owner).
    property var rowOwnerGlobal: undefined
    // The item the menu was opened from (a row's "⋯" shows while its menu is open).
    property Item opener: null
    // The window's attach dialog (6C), handed over by the chat header (H1).
    property QtObject attachDialog: null           // a guarded object property: null once the dialog is gone
    readonly property string targetId: rowId || bridge.conversationId
    readonly property string shown: (rowId ? rowTitle : bridge.conversationTitle) || qsTr("New Chat")
    readonly property bool archived: !rowId && bridge.conversationArchived
    readonly property bool ownerArchived: rowId && rowOwnerArchived !== undefined ? !!rowOwnerArchived
                                                                              : !!(bridge.conversationProject || {}).archived
    readonly property bool ownerGlobal: rowId && rowOwnerGlobal !== undefined ? !!rowOwnerGlobal
                                                                          : !!(bridge.conversationProject || {}).global
    readonly property bool open: context.visible
    // R1: JARVIS is working for this conversation (or for none it could leave: a sweep).
    function runsIn(id) { return bridge.navigationLocked || (id !== "" && bridge.runningConversationId === id) }
    readonly property bool runBlocked: runsIn(targetId)
    readonly property bool renameAllowed: targetId !== "" && !archived && !ownerArchived && !runBlocked
    readonly property string renameReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : archived ? qsTr("This conversation is archived: unarchive it to rename it.")
        : ownerArchived ? qsTr("The project is archived: unarchive it to rename this conversation.")
        : runBlocked ? qsTr("Wait for the current run to finish.") : ""
    // Archive (reversible), then the permanent delete - offered only on an
    // archived conversation, under an archived project or (F7) on a Global one.
    readonly property bool archiveAllowed: renameAllowed
    readonly property string archiveReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : archived ? qsTr("This conversation is already archived.")
        : ownerArchived ? qsTr("The project is archived: its conversations are already read-only.")
        : runBlocked ? qsTr("Wait for the current run to finish.") : ""
    readonly property bool deleteAllowed: targetId !== "" && !runBlocked && (archived || ownerArchived || ownerGlobal)
    readonly property string deleteReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : runBlocked ? qsTr("Wait for the current run to finish.")
        : !deleteAllowed ? qsTr("Archive it first") : ""
    // Attaching (6C, F9 H1): a Global conversation only, never an archived one.
    readonly property bool attachShown: ownerGlobal && !archived
    readonly property bool attachAllowed: targetId !== "" && !runBlocked && !!attachDialog && (rowId !== "" || bridge.canAttach)
    readonly property string attachReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : runBlocked ? qsTr("Wait for the current run to finish.") : ""
    // Pinning (F6): the target's place in SABİTLENDİ (-1 = not pinned).
    readonly property int pinIndex: { var pins = bridge.pinnedConversations
                                      for (var i = 0; i < pins.length; ++i) if (pins[i].id === targetId) return i
                                      return -1 }
    readonly property bool pinned: pinIndex >= 0
    readonly property bool pinAllowed: targetId !== "" && !runBlocked && (pinned || (!archived && bridge.pinnedConversations.length < bridge.pinLimit))
    readonly property string pinReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : runBlocked ? qsTr("Wait for the current run to finish.")
        : archived ? qsTr("This conversation is archived: unarchive it to pin it.")
        : !pinAllowed ? qsTr("At most %1 conversations can be pinned: unpin one first.").arg(bridge.pinLimit) : ""
    // An edit is bound to the conversation it STARTED on, and is reverted
    // when the open conversation changes under it.
    property string editId: ""
    property string editTitle: ""
    property string editOn: ""

    // Open the menu for `id` ("" or the open one's id = the open conversation),
    // at `point` of `anchor`, kept inside the window (8 px clear of its edges);
    // its rename editor at `editPoint` of `editAnchor` when given, else there too.
    // `trigger` (the header's "…"): a press on it toggles, never closes and reopens.
    function openFor(id, title, anchor, point, editAnchor, editPoint, ownerArchived, ownerGlobal, trigger) {
        editor.close()
        rowId = id === bridge.conversationId ? "" : (id || "")
        rowTitle = title || ""
        rowOwnerArchived = ownerArchived
        rowOwnerGlobal = ownerGlobal
        opener = anchor
        editAt = editAnchor ? [editAnchor, editPoint] : [anchor, point]
        menuColumn.forceLayout()                                    // G6MENUFIX: placed by the rows just shown, not the last open's
        context.popupAt(anchor, point, { trigger: trigger })
    }
    property var editAt: []
    function dismiss() { context.close(); editor.close() }

    ConversationDeleteDialog { id: deleteDialog }

    MenuSurface {
        id: context
        objectName: "conversationSwitcherContext"
        width: 300
        onAboutToShow: bridge.previewDeleteConversation("")
        Column {
            id: menuColumn
            width: context.availableWidth
            spacing: 2
            // Which conversation these act on.
            Text { objectName: "conversationContextHeader"; width: parent.width; leftPadding: 12; rightPadding: 12
                   bottomPadding: 4; elide: Text.ElideRight; textFormat: Text.PlainText
                   text: menu.shown; color: Theme.textMuted; font: Theme.caption
                   HoverTip { objectName: "conversationContextHeaderTip"; text: parent.text; active: parent.truncated } }
            ScopeChoice { objectName: "conversationPinItem"; icon: "pin"
                          label: menu.pinned ? qsTr("Unpin") : qsTr("Pin"); available: menu.pinAllowed
                          onTriggered: { var id = menu.targetId; context.close()
                                         if (menu.pinned) bridge.unpinConversation(id); else bridge.pinConversation(id) } }
            Reason { width: parent.width; objectName: "conversationPinReason"; visible: !menu.pinAllowed; text: menu.pinReason }
            ScopeChoice { objectName: "conversationPinUpItem"; icon: "arrow-up"; visible: menu.pinIndex > 0; available: !menu.runBlocked
                          label: qsTr("Move up"); onTriggered: { var id = menu.targetId, to = menu.pinIndex - 1; context.close(); bridge.movePin(id, to) } }
            ScopeChoice { objectName: "conversationPinDownItem"; icon: "arrow-down"; available: !menu.runBlocked
                          visible: menu.pinned && menu.pinIndex < bridge.pinnedConversations.length - 1
                          label: qsTr("Move down"); onTriggered: { var id = menu.targetId, to = menu.pinIndex + 1; context.close(); bridge.movePin(id, to) } }
            // R3U: a sidebar row (never the open conversation) back to R3's green until opened again.
            ScopeChoice { objectName: "conversationMarkUnreadItem"; icon: "dot"; label: qsTr("Mark as unread")
                          visible: menu.rowId !== "" && bridge.finishedUnseenIds.indexOf(menu.rowId) < 0
                          onTriggered: { var id = menu.rowId; context.close(); bridge.markConversationUnread(id) } }
            ScopeChoice { objectName: "conversationRenameItem"; icon: "pencil"
                          label: qsTr("Rename"); available: menu.renameAllowed
                          onTriggered: { menu.editId = menu.targetId; menu.editTitle = menu.rowId ? menu.rowTitle : bridge.conversationTitle
                                         menu.editOn = bridge.conversationId; context.close()
                                         bridge.clearRenameNotice(); editor.popupAt(menu.editAt[0], menu.editAt[1], { opener: menu.opener }) } }
            Reason { width: parent.width; objectName: "conversationRenameReason"; visible: !menu.renameAllowed; text: menu.renameReason }
            ScopeChoice { objectName: "conversationAttachItem"; icon: "folder"; visible: menu.attachShown
                          label: qsTr("Attach to a project…"); available: menu.attachAllowed
                          onTriggered: { var id = menu.targetId, back = menu.opener; context.close()
                                         if (bridge.openConversation(id) && bridge.canAttach) {
                                             menu.attachDialog.opener = back; menu.attachDialog.openDialog() } } }
            Reason { width: parent.width; objectName: "conversationAttachReason"; visible: menu.attachShown && !menu.attachAllowed && text !== ""
                     text: menu.attachReason }
            Divider { width: parent.width; objectName: "conversationMenuDivider" }
            ScopeChoice { objectName: "conversationArchiveItem"; icon: "archive"
                          label: qsTr("Archive"); available: menu.archiveAllowed
                          onTriggered: { var id = menu.targetId; if (bridge.archiveConversation(id)) context.close() } }
            Reason { width: parent.width; objectName: "conversationArchiveReason"; visible: !menu.archiveAllowed; text: menu.archiveReason }
            Divider { width: parent.width; objectName: "conversationMenuDivider" }
            ScopeChoice { objectName: "conversationDeleteItem"; icon: "trash"; destructive: true
                          label: qsTr("Delete permanently…"); available: menu.deleteAllowed
                          onTriggered: { var id = menu.targetId; context.close(); deleteDialog.opener = menu.opener; deleteDialog.openFor(id) } }
            Reason { width: parent.width; objectName: "conversationDeleteReason"; visible: !menu.deleteAllowed; text: menu.deleteReason }
            Text { objectName: "conversationLifecycleNotice"; visible: bridge.conversationNotice !== ""
                   width: parent.width; leftPadding: 12; rightPadding: 12; wrapMode: Text.Wrap
                   textFormat: Text.PlainText; text: bridge.conversationNotice; color: Theme.warning; font: Theme.caption }
        }
    }

    // The edit, on the same surface. Enter / Save commits the normalized name to
    // the conversation the edit STARTED on; Escape / Cancel / a change of
    // conversation reverts. Focus starts in the name, all of it selected.
    MenuSurface {
        id: editor
        objectName: "conversationRenameEditor"
        width: 320; padding: 12
        initialFocus: renameField
        onAboutToShow: { renameField.text = menu.editTitle }
        onOpened: renameField.selectAll()
        function commit() {
            if (!menu.runsIn(menu.editId) && bridge.renameConversation(menu.editId, renameField.text)) editor.close()
        }
        Connections {
            target: bridge
            function onConversationChanged() {
                if (editor.visible && bridge.conversationId !== menu.editOn) editor.close()
            }
        }
        Column {
            width: editor.availableWidth
            spacing: 10
            Text { objectName: "conversationRenameTitle"; text: i18n.upper(qsTr("Rename")); color: Theme.textMuted; font: Theme.label }
            TextField {
                id: renameField
                objectName: "conversationRenameField"
                width: parent.width; height: 36; selectByMouse: true; color: Theme.text; font: Theme.body
                placeholderText: qsTr("Conversation name")
                background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                        border.color: renameField.activeFocus ? Theme.commandEdge : Theme.border }
                // Esc is the editor's: it reverts and returns focus to the opener.
                Keys.onShortcutOverride: function (event) { if (event.key === Qt.Key_Escape) event.accepted = true }
                onAccepted: editor.commit()
                ContextMenu.menu: null
                EditMenu { objectName: "conversationRenameFieldMenu"; editor: renameField }
            }
            Text { objectName: "conversationRenameNotice"; visible: bridge.conversationRenameNotice !== ""
                   width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                   text: bridge.conversationRenameNotice; color: Theme.warning; font: Theme.caption }
            Item {
                width: parent.width; height: 32
                Row {
                    anchors.right: parent.right
                    spacing: 8
                    ActionButton { objectName: "conversationRenameCancel"; label: qsTr("Cancel"); height: 32
                                   onClicked: editor.close() }
                    ActionButton { objectName: "conversationRenameSave"; label: qsTr("Save"); primary: true; height: 32
                                   enabled: !menu.runsIn(menu.editId); onClicked: editor.commit() }
                }
            }
        }
    }
}
