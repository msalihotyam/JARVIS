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
    // K6: a task reads as words (its status and stage); the exact capability and
    // connector names stay beside them, muted, as data.
    function joined(parts) { return parts.filter(function(v) { return !!v }).join("  ·  ") }
    // The session whose tasks are still on their way: no "no tasks" verdict before they arrive.
    // ponytail: any dataChanged ends the wait; a load-specific signal if a slow store shows the verdict early.
    property string loadingSession: ""
    Connections { target: bridge; function onDataChanged() { page.loadingSession = "" } }

    Row {
        anchors.fill: parent
        spacing: Theme.gap
        Column {
            width: parent.width * 0.32; height: parent.height; spacing: 8
            Text { text: qsTr("SESSIONS (most recent %1)").arg(20); color: Theme.textLabel; font: Theme.pageSection }
            PageEmpty { objectName: "tasksSessionsEmpty"; width: parent.width; visible: bridge.sessions.length === 0
                        title: qsTr("No task sessions yet.")
                        note: qsTr("A session starts the first time you ask JARVIS for something; its tasks are listed here.")
                        action: qsTr("Write to JARVIS"); onActed: bridge.setPage("home") }
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
                        Text { objectName: "tasksSessionMeta"; text: page.when(modelData.updated_at) + "  ·  " + (modelData.task_count === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(modelData.task_count || 0)) + (modelData.readable === false ? "  ·  " + qsTr("cannot be read") : ""); color: Theme.textMuted; font: Theme.pageMetaFigures
                               width: parent.parent.width - 20; elide: Text.ElideRight }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { page.selectedSession = modelData.session_id; page.selectedTask = ""; page.selectedTaskRow = null
                                                                 page.loadingSession = modelData.session_id; bridge.loadTasks(modelData.session_id) } }
                    TapHandler { acceptedButtons: Qt.RightButton
                                 onTapped: function(point) { renameMenu.openFor(parent, point.position, modelData.session_id, modelData.title) } }
                }
            }
        }
        Column {
            width: parent.width * 0.30; height: parent.height; spacing: 8
            Text { text: qsTr("TASKS"); color: Theme.textLabel; font: Theme.pageSection }
            PageEmpty { objectName: "tasksTasksEmpty"; width: parent.width; visible: page.selectedSession === "" || (bridge.tasks.length === 0 && page.loadingSession === "")
                        title: page.selectedSession === "" ? qsTr("Pick a session to see its tasks.") : qsTr("This session has no tasks.") }
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
                        Text { objectName: "tasksTaskTitle"; text: modelData.title || qsTr("Untitled task"); color: modelData.title ? Theme.text : Theme.textMuted
                               font: Theme.pageRowStrong; width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { objectName: "tasksTaskStatus"; visible: text !== ""; color: Theme.textLabel; font: Theme.pageMeta
                               text: page.joined([i18n.word("task_status", modelData.status || ""), i18n.word("stage", modelData.stage || "")])
                               width: parent.parent.width - 20; elide: Text.ElideRight }
                        Text { objectName: "tasksTaskSource"; visible: text !== ""; color: Theme.textMuted; font: Theme.pageMeta
                               text: page.joined([modelData.capability, modelData.selected_connector])
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
            PageEmpty { objectName: "tasksSnapshotEmpty"; width: parent.width; visible: page.selectedTask === ""
                        title: qsTr("Pick a task to see where it stands.")
                        note: qsTr("The snapshot shows the task's exact state, stage and record as JARVIS stored them.") }
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
