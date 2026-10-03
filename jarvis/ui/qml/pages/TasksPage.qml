import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Sessions -> tasks -> one task's operational snapshot. Read-only, bounded,
// with Resume for a task that is genuinely parked.
SecondaryPage {
    id: page
    title: qsTr("Tasks")
    property string selectedSession: ""
    property string selectedTask: ""
    property var selectedTaskRow: null
    headerRight: [
        ActionButton { label: qsTr("Refresh"); onClicked: bridge.refreshHistory() },
        ActionButton { label: qsTr("Use this session"); enabled: page.selectedSession !== "" && !bridge.busy; onClicked: bridge.useSession(page.selectedSession) },
        ActionButton { label: qsTr("Resume task"); primary: true
            enabled: !bridge.busy && page.selectedTaskRow !== null && ["awaiting_approval", "awaiting_clarification", "awaiting_cost_override"].indexOf(page.selectedTaskRow.status) >= 0
            onClicked: { bridge.resumeTask(page.selectedSession, page.selectedTask); bridge.setPage("home") } }
    ]

    RenameMenu { id: renameMenu }

    Row {
        anchors.fill: parent
        spacing: Theme.gap
        Column {
            width: parent.width * 0.32; height: parent.height; spacing: 8
            Text { text: qsTr("SESSIONS (most recent %1)").arg(20); color: Theme.textLabel; font: Theme.pageSection }
            ListView {
                width: parent.width; height: parent.height - 30; clip: true; spacing: 4
                model: bridge.sessions
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 62; radius: Theme.radiusSmall
                    property bool current: modelData.session_id === page.selectedSession
                    color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14) : "transparent"
                    border.width: 1; border.color: current ? Theme.accent : Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 4
                        Text { text: modelData.title || qsTr("Untitled session"); color: modelData.title ? Theme.text : Theme.textMuted
                               font: Theme.pageRowStrong; width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { objectName: "tasksSessionMeta"; text: page.when(modelData.updated_at) + "  ·  " + (modelData.task_count === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(modelData.task_count || 0)) + (modelData.readable === false ? "  ·  " + qsTr("UNREADABLE") : ""); color: Theme.textMuted; font: Theme.pageMetaFigures
                               width: parent.parent.width - 20; elide: Text.ElideRight }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { page.selectedSession = modelData.session_id; page.selectedTask = ""; page.selectedTaskRow = null; bridge.loadTasks(modelData.session_id) } }
                    TapHandler { acceptedButtons: Qt.RightButton
                                 onTapped: function(point) { renameMenu.openFor(parent, point.position, modelData.session_id, modelData.title) } }
                }
            }
        }
        Column {
            width: parent.width * 0.30; height: parent.height; spacing: 8
            Text { text: qsTr("TASKS"); color: Theme.textLabel; font: Theme.pageSection }
            ListView {
                width: parent.width; height: parent.height - 30; clip: true; spacing: 4
                model: page.selectedSession !== "" ? bridge.tasks : []
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 84; radius: Theme.radiusSmall
                    property bool current: modelData.task_id === page.selectedTask
                    color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14) : "transparent"
                    border.width: 1; border.color: current ? Theme.accent : Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 4
                        // The task's name first; its id stays in the snapshot.
                        Text { text: modelData.title || ((modelData.capability || "?") + "  ·  " + (modelData.status || "?")); color: Theme.text
                               font: Theme.pageRowStrong; width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { visible: !!modelData.title; text: (modelData.capability || "?") + "  ·  " + (modelData.status || "?"); color: Theme.textLabel; font: Theme.pageMeta
                               width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { text: (modelData.stage || "") + "  ·  " + (modelData.selected_connector || "—"); color: Theme.textMuted; font: Theme.pageMeta
                               width: parent.parent.width - 20; elide: Text.ElideRight }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { page.selectedTask = modelData.task_id; page.selectedTaskRow = modelData; bridge.showTask(page.selectedSession, modelData.task_id) } }
                    TapHandler { acceptedButtons: Qt.RightButton
                                 onTapped: function(point) { renameMenu.openFor(parent, point.position, modelData.task_id, modelData.title) } }
                }
            }
        }
        Column {
            width: parent.width - parent.width * 0.62 - Theme.gap * 2; height: parent.height; spacing: 8
            Text { text: qsTr("SNAPSHOT"); color: Theme.textLabel; font: Theme.pageSection }
            ListView {
                width: parent.width; height: parent.height - 30; clip: true; spacing: 10
                model: page.selectedTask !== "" ? bridge.taskDetail : []
                delegate: TextEdit {
                    width: ListView.view.width; readOnly: true; selectByMouse: true; wrapMode: Text.Wrap
                    text: modelData.text
                    color: modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "approval" ? Theme.warning : Theme.text
                    font: Theme.pageCode
                }
            }
        }
    }
}
