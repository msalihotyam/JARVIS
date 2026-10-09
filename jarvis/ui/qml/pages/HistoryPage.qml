import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Recent activity of this interface session plus the durable session
// listing. Bounded, read-only.
SecondaryPage {
    id: page
    objectName: "historyPage"
    title: qsTr("History")
    headerRight: [ ActionButton { label: qsTr("Refresh"); onClicked: bridge.refreshHistory() } ]

    RenameMenu { id: renameMenu }
    ConversationDeleteDialog { id: deleteDialog }

    // 40 / 60 below 1420 px (the conversations get the room), 50 / 50 wide.
    readonly property real split: page.Window.width > 0 && page.Window.width < 1420 ? 0.4 : 0.5

    Row {
        anchors.fill: parent
        spacing: Theme.gap
        Column {
            width: parent.width * page.split - Theme.gap / 2; height: parent.height; spacing: 8
            Text { text: qsTr("THIS INTERFACE SESSION"); color: Theme.textLabel; font: Theme.pageSection }
            PageEmpty { objectName: "historyActivityEmpty"; width: parent.width; visible: bridge.activity.length === 0
                        title: qsTr("Nothing has happened in this window yet.")
                        note: qsTr("What JARVIS does while this window is open is listed here, newest first.") }
            ListView {
                width: parent.width; height: parent.height - 30; clip: true; spacing: 10
                model: bridge.activity
                ScrollBar.vertical: ScrollBar {}
                delegate: Row { width: ListView.view.width; spacing: 10
                    Text { text: modelData.time; color: Theme.textMuted; font: Theme.pageMetaFigures; width: 78 }
                    Text { text: modelData.text; width: parent.width - 88; wrapMode: Text.Wrap
                        color: modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "approval" ? Theme.warning : Theme.text
                        font: Theme.pageRow }
                }
            }
        }
        // Gate 6E: one scroll for the whole column (G7) - conversations, the
        // archived ones (whole rows, at most 5 before their own scroll) and the
        // durable sessions (or the line saying there are none).
        Flickable {
            id: rightColumn
            objectName: "historyRightColumn"
            width: parent.width * (1 - page.split) - Theme.gap / 2; height: parent.height
            contentHeight: right.height; clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: rightColumn.contentHeight > rightColumn.height ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
            Column {
                id: right
                width: rightColumn.width - (rightColumn.contentHeight > rightColumn.height ? 12 : 0); spacing: 8
                Item { width: parent.width; height: 36
                    Text { objectName: "historyConversationsHeading"; anchors.verticalCenter: parent.verticalCenter
                           text: qsTr("CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                    ActionButton { objectName: "historyNewConversation"; anchors.right: parent.right; compact: true; label: qsTr("New conversation")
                                   onClicked: { bridge.newConversation(); bridge.setPage("home") } } }
                Text { objectName: "historyConversationsEmpty"; visible: bridge.conversations.length === 0; width: parent.width; wrapMode: Text.Wrap
                       text: qsTr("No conversations yet. Start one with New conversation."); color: Theme.textLabel; font: Theme.pageMeta }
                Column {
                    width: parent.width; spacing: 4
                    Repeater {
                        model: bridge.conversations
                        ListRow {
                            objectName: "historyConversationRow"
                            title: modelData.title; current: modelData.active
                            badge: modelData.active ? qsTr("open") : ""
                            meta: page.when(modelData.updated_at)
                            // Locals: opening rebuilds the model, and this delegate's context with it.
                            onActivated: { var b = bridge, id = modelData.id; b.openConversation(id); b.setPage("home") }
                        }
                    }
                }
                // Gate 6D-b: archived Global conversations. A row opens read-only;
                // "Unarchive" undoes the archive, "Delete permanently…" is the second step.
                Row {
                    visible: archivedList.count > 0; spacing: 8
                    Text { objectName: "historyArchivedHeading"; text: qsTr("ARCHIVED CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                    Text { objectName: "historyArchivedCount"; text: "(" + archivedList.count + ")"; color: Theme.textMuted; font: Theme.pageSection }
                }
                Text { objectName: "historyArchivedNotice"; visible: text !== "" && archivedList.count > 0 && !deleteDialog.visible
                       width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText; text: bridge.conversationNotice
                       color: Theme.warning; font: Theme.pageMeta }
                ListView {
                    id: archivedList
                    objectName: "historyArchivedList"
                    visible: count > 0
                    // Whole rows: every row has the same height, so five of them are 5/count of the content.
                    width: parent.width; height: count <= 5 ? contentHeight : 5 * (contentHeight + spacing) / count - spacing
                    clip: true; spacing: 4; interactive: count > 5; snapMode: ListView.SnapToItem; boundsBehavior: Flickable.StopAtBounds
                    model: bridge.archivedConversations
                    ScrollBar.vertical: ScrollBar { policy: archivedList.count > 5 ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
                    delegate: ListRow {
                        objectName: "historyArchivedRow"
                        property var row: modelData
                        width: ListView.view.width - (ListView.view.count > 5 ? 12 : 0)
                        title: row.title; current: row.active; muted: true
                        badge: qsTr("ARCHIVED"); badgeTint: Theme.warning
                        meta: qsTr("archived %1").arg(page.when(row.archived_at))
                        onActivated: { var b = bridge, id = row.id; b.openConversation(id); b.setPage("home") }
                        ActionButton { objectName: "historyUnarchive"; compact: true; label: qsTr("Unarchive"); enabled: !bridge.busy
                                       onClicked: { var b = bridge, id = row.id; b.unarchiveConversation(id) } }
                        ActionButton { objectName: "historyDelete"; compact: true; danger: true; label: qsTr("Delete permanently…")
                                       enabled: !bridge.busy; onClicked: deleteDialog.openFor(row.id) }
                    }
                }
                Text { text: qsTr("DURABLE SESSIONS (most recent %1)").arg(20); color: Theme.textLabel; font: Theme.pageSection }
                Text { objectName: "historySessionsEmpty"; visible: bridge.sessions.length === 0; text: qsTr("No durable session yet.")
                       color: Theme.textLabel; font: Theme.pageMeta }
                Column {
                    width: parent.width; spacing: 4
                    Repeater {
                        model: bridge.sessions
                        ListRow {
                            id: sessionRow
                            objectName: "historySessionRow"
                            title: modelData.title || qsTr("Untitled session"); muted: !modelData.title
                            detailName: "historySessionMeta"
                            detail: page.when(modelData.updated_at) + "  ·  "
                                    + (modelData.task_count === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(modelData.task_count || 0))
                                    + (modelData.active_task_id ? "  ·  " + qsTr("a task is active") : "")
                                    + (modelData.readable === false ? "  ·  " + qsTr("cannot be read") : "")
                            onActivated: { bridge.useSession(modelData.session_id); bridge.setPage("home") }
                            onContextRequested: function (position) { renameMenu.openFor(sessionRow, position, modelData.session_id, modelData.title) }
                        }
                    }
                }
            }
        }
    }
}
