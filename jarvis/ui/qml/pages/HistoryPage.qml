import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Recent activity of this interface session plus the durable session
// listing. Bounded, read-only.
SecondaryPage {
    id: page
    title: qsTr("History")
    headerRight: [ ActionButton { label: qsTr("Refresh"); onClicked: bridge.refreshHistory() } ]

    RenameMenu { id: renameMenu }
    ConversationDeleteDialog { id: deleteDialog }

    Row {
        anchors.fill: parent
        spacing: Theme.gap
        Column {
            width: parent.width * 0.5 - Theme.gap / 2; height: parent.height; spacing: 8
            Text { text: qsTr("THIS INTERFACE SESSION"); color: Theme.textLabel; font: Theme.pageSection }
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
        Column {
            width: parent.width * 0.5 - Theme.gap / 2; height: parent.height; spacing: 8
            Item { width: parent.width; height: 36
                Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                ActionButton { objectName: "historyNewConversation"; anchors.right: parent.right; compact: true; label: qsTr("New conversation")
                               onClicked: { bridge.newConversation(); bridge.setPage("home") } } }
            ListView {
                id: conversationList
                width: parent.width; height: Math.min(contentHeight, parent.height * 0.4); clip: true; spacing: 4
                model: bridge.conversations
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    objectName: "historyConversationRow"
                    width: ListView.view.width; height: 40; radius: Theme.radiusSmall
                    color: modelData.active ? Theme.alpha(Theme.accent, 0.10) : "transparent"
                    border.width: 1; border.color: modelData.active ? Theme.alpha(Theme.accent, 0.6) : Theme.border
                    Text { anchors.left: parent.left; anchors.leftMargin: 10; anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter
                           text: modelData.title + "  ·  " + (modelData.active ? qsTr("open") : page.when(modelData.updated_at))
                           color: modelData.active ? Theme.accent : Theme.text; font: Theme.pageRowStrong; elide: Text.ElideRight }
                    // Locals: opening rebuilds the model, and this delegate's context with it.
                    MouseArea { anchors.fill: parent; onClicked: { var b = bridge, id = modelData.id; b.openConversation(id); b.setPage("home") } }
                }
            }
            // Gate 6D-b: archived Global conversations. A row opens read-only;
            // "Unarchive" undoes the archive, "Delete permanently…" is the second step.
            Text { objectName: "historyArchivedHeading"; visible: archivedList.count > 0; text: qsTr("ARCHIVED CONVERSATIONS")
                   color: Theme.textLabel; font: Theme.pageSection }
            Text { objectName: "historyArchivedNotice"; visible: text !== "" && archivedList.count > 0 && !deleteDialog.visible
                   width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText; text: bridge.conversationNotice
                   color: Theme.warning; font: Theme.pageMeta }
            ListView {
                id: archivedList
                objectName: "historyArchivedList"
                visible: count > 0
                width: parent.width; height: count > 0 ? Math.min(contentHeight, parent.height * 0.25) : 0; clip: true; spacing: 4
                model: bridge.archivedConversations
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    objectName: "historyArchivedRow"
                    property var row: modelData
                    width: ListView.view.width; height: 40; radius: Theme.radiusSmall
                    color: row.active ? Theme.alpha(Theme.accent, 0.10) : "transparent"
                    border.width: 1; border.color: row.active ? Theme.alpha(Theme.accent, 0.6) : Theme.border
                    Text { anchors { left: parent.left; leftMargin: 10; right: archivedButtons.left; rightMargin: 10; verticalCenter: parent.verticalCenter }
                           text: row.title + "  ·  " + qsTr("archived %1").arg(page.when(row.archived_at))
                           textFormat: Text.PlainText; color: Theme.textMuted; font: Theme.pageRowStrong; elide: Text.ElideRight }
                    MouseArea { anchors.fill: parent; onClicked: { var b = bridge, id = row.id; b.openConversation(id); b.setPage("home") } }
                    Row {
                        id: archivedButtons
                        anchors { right: parent.right; rightMargin: 6; verticalCenter: parent.verticalCenter }
                        spacing: 6
                        ActionButton { objectName: "historyUnarchive"; compact: true; label: qsTr("Unarchive"); enabled: !bridge.busy
                                       onClicked: { var b = bridge, id = row.id; b.unarchiveConversation(id) } }
                        ActionButton { objectName: "historyDelete"; compact: true; danger: true; label: qsTr("Delete permanently…")
                                       enabled: !bridge.busy; onClicked: deleteDialog.openFor(row.id) }
                    }
                }
            }
            Text { text: qsTr("DURABLE SESSIONS (most recent %1)").arg(20); color: Theme.textLabel; font: Theme.pageSection }
            ListView {
                width: parent.width; height: parent.height - 36 - conversationList.height - (archivedList.count > 0 ? 30 + archivedList.height + 2 * parent.spacing : 0)
                        - 30 - 3 * parent.spacing; clip: true; spacing: 4
                model: bridge.sessions
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 62; radius: Theme.radiusSmall
                    color: "transparent"; border.width: 1; border.color: Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 4
                        Text { text: modelData.title || qsTr("Untitled session"); color: modelData.title ? Theme.text : Theme.textMuted
                               font: Theme.pageRowStrong; width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { objectName: "historySessionMeta"; text: page.when(modelData.updated_at) + "  ·  " + (modelData.task_count === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(modelData.task_count || 0)) + (modelData.active_task_id ? "  ·  " + qsTr("a task is active") : "") + (modelData.readable === false ? "  ·  " + qsTr("UNREADABLE") : ""); color: Theme.textMuted; font: Theme.pageMetaFigures
                               width: parent.parent.width - 20; elide: Text.ElideRight }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { bridge.useSession(modelData.session_id); bridge.setPage("home") } }
                    TapHandler { acceptedButtons: Qt.RightButton
                                 onTapped: function(point) { renameMenu.openFor(parent, point.position, modelData.session_id, modelData.title) } }
                }
            }
        }
    }
}
