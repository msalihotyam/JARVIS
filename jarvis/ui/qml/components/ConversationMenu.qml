import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6F (F3, Q8): the conversation menu - rename (6D-a), archive and the
// permanent delete (6D-b) - with its rename editor and the delete dialog,
// moved unchanged out of the TopBar switcher. One menu, two openers: the open
// conversation (the switcher until F4, then the chat header), or a sidebar
// row, which it acts on WITHOUT opening it. Every item is disabled with its
// reason (archived project or conversation, not archived yet, a busy run);
// the store still refuses. Each acts on the conversation it was opened for.
// It holds the scope menus' shared parts (a row, a reason, a divider, the
// glass popup), which the TopBar's own menus use too.
Item {
    id: menu
    objectName: "conversationMenu"

    // One row of the scope menus: menuRowHeight; a hover lift; the current row
    // carries the 2 px accent mark; an action row reads in accent, an enabled
    // destructive row in danger, an untitled one in textLabel. An elided label
    // has its tip.
    component ScopeChoice: Rectangle {
        id: choice
        property string label: ""
        property bool current: false
        property bool available: true
        property bool action: false
        property bool danger: false
        property bool muted: false
        signal chosen()
        height: Theme.menuRowHeight; radius: 6
        enabled: !bridge.busy && available
        opacity: enabled ? 1 : 0.45
        color: current ? Theme.alpha(Theme.accent, 0.14) : choiceArea.containsMouse ? Theme.alpha(Theme.text, 0.07) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Rectangle { visible: choice.current; x: 0; y: 8; width: 2; height: parent.height - 16; radius: 1; color: Theme.accent }
        Text { id: choiceText; x: 12; width: parent.width - 24; anchors.verticalCenter: parent.verticalCenter; text: choice.label
               textFormat: Text.PlainText; elide: Text.ElideRight; font: Theme.body
               color: choice.danger && choice.enabled ? Theme.danger : choice.action ? Theme.accent
                      : choice.muted ? Theme.textLabel : Theme.text }
        MouseArea { id: choiceArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: choice.chosen() }
        HoverTip { text: choice.label; active: choiceText.truncated }
    }
    // A disabled item's reason, under it; and a menu's section divider.
    component Reason: Text {
        leftPadding: 12; rightPadding: 12; bottomPadding: 4
        wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.textDim; font: Theme.caption
    }
    component Divider: Item {
        height: 9
        Rectangle { y: 4; x: 8; width: parent.width - 16; height: 1; color: Theme.border }
    }
    // The scope menus: glass, and the popover motion - opacity and panelTravel
    // over panelIn in, panelOut out; no motion at all with ambient effects off.
    component ScopeMenu: Popup {
        id: scopeMenu
        property real restY: 0
        y: restY
        padding: 8
        background: Rectangle { radius: 10; color: Theme.glassOverlay; border.width: 1; border.color: Theme.glassOverlayEdge
                                WheelHandler { } }
        enter: Theme.ambient ? menuIn : null
        exit: Theme.ambient ? menuOut : null
        readonly property Transition menuIn: Transition {
            ParallelAnimation {
                NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.panelIn; easing.type: Theme.ease }
                NumberAnimation { property: "y"; from: scopeMenu.restY - Theme.panelTravel; to: scopeMenu.restY; duration: Theme.panelIn; easing.type: Theme.ease }
            }
        }
        readonly property Transition menuOut: Transition {
            NumberAnimation { property: "opacity"; to: 0; duration: Theme.panelOut; easing.type: Theme.ease }
        }
        onClosed: { opacity = 1; y = restY }
    }

    // What the menu acts on: "" = the open conversation; else a listed row of
    // the open one's scope (never archived, the same owner: the same reasons).
    property string rowId: ""
    property string rowTitle: ""
    readonly property string targetId: rowId || bridge.conversationId
    readonly property string shown: (rowId ? rowTitle : bridge.conversationTitle) || qsTr("New Chat")
    readonly property bool archived: !rowId && bridge.conversationArchived
    readonly property bool ownerArchived: !!(bridge.conversationProject || {}).archived
    readonly property bool open: context.visible
    readonly property bool renameAllowed: targetId !== "" && !archived && !ownerArchived && !bridge.busy
    readonly property string renameReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : archived ? qsTr("This conversation is archived: unarchive it to rename it.")
        : ownerArchived ? qsTr("The project is archived: unarchive it to rename this conversation.")
        : bridge.busy ? qsTr("Wait for the current run to finish.") : ""
    // Archive (reversible), then the permanent delete - offered only on an
    // archived conversation or under an archived project.
    readonly property bool archiveAllowed: renameAllowed
    readonly property string archiveReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : archived ? qsTr("This conversation is already archived.")
        : ownerArchived ? qsTr("The project is archived: its conversations are already read-only.")
        : bridge.busy ? qsTr("Wait for the current run to finish.") : ""
    readonly property bool deleteAllowed: targetId !== "" && !bridge.busy && (archived || ownerArchived)
    readonly property string deleteReason: targetId === "" ? qsTr("This conversation is not saved yet.")
        : bridge.busy ? qsTr("Wait for the current run to finish.")
        : !deleteAllowed ? qsTr("Archive it first") : ""
    // An edit is bound to the conversation it STARTED on, and is reverted
    // when the open conversation changes under it.
    property string editId: ""
    property string editTitle: ""
    property string editOn: ""

    // Open the menu for `id` ("" or the open one's id = the open conversation),
    // at `point` of `anchor`, kept inside the window (8 px clear of its edges).
    function openFor(id, title, anchor, point) {
        editor.close()
        rowId = id === bridge.conversationId ? "" : (id || "")
        rowTitle = title || ""
        var p = anchor.mapToItem(menu, point.x, point.y)
        place(context, p); place(editor, p)
        context.open()
    }
    function place(popup, p) {
        var w = menu.Window.width || 1920, h = menu.Window.height || 1080
        popup.x = Math.max(8, Math.min(p.x, w - popup.width - 8))
        popup.restY = Math.max(8, Math.min(p.y, h - popup.implicitHeight - 8))
    }
    function dismiss() { context.close(); editor.close() }

    ConversationDeleteDialog { id: deleteDialog }

    ScopeMenu {
        id: context
        objectName: "conversationSwitcherContext"
        width: 300
        onAboutToShow: bridge.previewDeleteConversation("")
        contentItem: Column {
            spacing: 2
            // Which conversation these act on.
            Text { objectName: "conversationContextHeader"; width: context.availableWidth; leftPadding: 12; rightPadding: 12
                   bottomPadding: 4; elide: Text.ElideRight; textFormat: Text.PlainText
                   text: menu.shown; color: Theme.textMuted; font: Theme.caption }
            ScopeChoice { objectName: "conversationRenameItem"; width: context.availableWidth
                          label: qsTr("Rename"); available: menu.renameAllowed
                          onChosen: { menu.editId = menu.targetId; menu.editTitle = menu.rowId ? menu.rowTitle : bridge.conversationTitle
                                      menu.editOn = bridge.conversationId; context.close()
                                      bridge.clearRenameNotice(); editor.open() } }
            Reason { width: context.availableWidth; objectName: "conversationRenameReason"; visible: !menu.renameAllowed; text: menu.renameReason }
            ScopeChoice { objectName: "conversationArchiveItem"; width: context.availableWidth
                          label: qsTr("Archive"); available: menu.archiveAllowed
                          onChosen: { var id = menu.targetId; if (bridge.archiveConversation(id)) context.close() } }
            Reason { width: context.availableWidth; objectName: "conversationArchiveReason"; visible: !menu.archiveAllowed; text: menu.archiveReason }
            Divider { width: context.availableWidth }
            ScopeChoice { objectName: "conversationDeleteItem"; width: context.availableWidth; danger: true
                          label: qsTr("Delete permanently…"); available: menu.deleteAllowed
                          onChosen: { var id = menu.targetId; context.close(); deleteDialog.openFor(id) } }
            Reason { width: context.availableWidth; objectName: "conversationDeleteReason"; visible: !menu.deleteAllowed; text: menu.deleteReason }
            Text { objectName: "conversationLifecycleNotice"; visible: bridge.conversationNotice !== ""
                   width: context.availableWidth; leftPadding: 12; rightPadding: 12; wrapMode: Text.Wrap
                   textFormat: Text.PlainText; text: bridge.conversationNotice; color: Theme.warning; font: Theme.caption }
        }
    }

    // The edit. Enter / Save commits the normalized name to the conversation
    // the edit STARTED on; Escape / Cancel / a change of conversation reverts.
    ScopeMenu {
        id: editor
        objectName: "conversationRenameEditor"
        width: 320; padding: 12
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onAboutToShow: { renameField.text = menu.editTitle }
        onOpened: { renameField.forceActiveFocus(); renameField.selectAll() }
        function commit() {
            if (!bridge.busy && bridge.renameConversation(menu.editId, renameField.text)) editor.close()
        }
        Connections {
            target: bridge
            function onConversationChanged() {
                if (editor.visible && bridge.conversationId !== menu.editOn) editor.close()
            }
        }
        contentItem: Column {
            spacing: 10
            Text { objectName: "conversationRenameTitle"; text: i18n.upper(qsTr("Rename")); color: Theme.textMuted; font: Theme.label }
            TextField {
                id: renameField
                objectName: "conversationRenameField"
                width: editor.availableWidth; height: 36; selectByMouse: true; color: Theme.text; font: Theme.body
                placeholderText: qsTr("Conversation name")
                background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                        border.color: renameField.activeFocus ? Theme.commandEdge : Theme.border }
                onAccepted: editor.commit()
            }
            Text { objectName: "conversationRenameNotice"; visible: bridge.conversationRenameNotice !== ""
                   width: editor.availableWidth; wrapMode: Text.Wrap; textFormat: Text.PlainText
                   text: bridge.conversationRenameNotice; color: Theme.warning; font: Theme.caption }
            Item {
                width: editor.availableWidth; height: 32
                Row {
                    anchors.right: parent.right
                    spacing: 8
                    ActionButton { objectName: "conversationRenameCancel"; label: qsTr("Cancel"); height: 32
                                   onClicked: editor.close() }
                    ActionButton { objectName: "conversationRenameSave"; label: qsTr("Save"); primary: true; height: 32
                                   enabled: !bridge.busy; onClicked: editor.commit() }
                }
            }
        }
    }
}
