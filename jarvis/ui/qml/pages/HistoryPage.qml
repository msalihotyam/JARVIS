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
            Text { text: qsTr("DURABLE SESSIONS (most recent %1)").arg(20); color: Theme.textLabel; font: Theme.pageSection }
            ListView {
                width: parent.width; height: parent.height - 30; clip: true; spacing: 4
                model: bridge.sessions
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 62; radius: Theme.radiusSmall
                    color: "transparent"; border.width: 1; border.color: Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 4
                        Text { text: modelData.title || qsTr("Untitled session"); color: modelData.title ? Theme.text : Theme.textMuted
                               font: Theme.pageRowStrong; width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { text: page.when(modelData.updated_at) + "  ·  " + qsTr("%1 tasks").arg(modelData.task_count || 0) + (modelData.active_task_id ? "  ·  " + qsTr("a task is active") : "") + (modelData.readable === false ? "  ·  " + qsTr("UNREADABLE") : ""); color: Theme.textMuted; font: Theme.pageMetaFigures
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
