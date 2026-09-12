import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Sessions -> tasks -> one task's operational snapshot. Read-only, bounded,
// with Resume for a task that is genuinely parked.
SecondaryPage {
    id: page
    title: "Tasks"
    property string selectedSession: ""
    property string selectedTask: ""
    property var selectedTaskRow: null
    headerRight: [
        ActionButton { label: "Refresh"; onClicked: bridge.refreshHistory() },
        ActionButton { label: "Use this session"; enabled: page.selectedSession !== "" && !bridge.busy; onClicked: bridge.useSession(page.selectedSession) },
        ActionButton { label: "Resume task"; primary: true
            enabled: !bridge.busy && page.selectedTaskRow !== null && ["awaiting_approval", "awaiting_clarification", "awaiting_cost_override"].indexOf(page.selectedTaskRow.status) >= 0
            onClicked: { bridge.resumeTask(page.selectedSession, page.selectedTask); bridge.setPage("home") } }
    ]

    Row {
        anchors.fill: parent
        spacing: Theme.gap
        Column {
            width: parent.width * 0.32; height: parent.height; spacing: 8
            Text { text: "SESSIONS (most recent 20)"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ListView {
                width: parent.width; height: parent.height - 24; clip: true; spacing: 4
                model: bridge.sessions
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 48; radius: Theme.radiusSmall
                    property bool current: modelData.session_id === page.selectedSession
                    color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14) : "transparent"
                    border.width: 1; border.color: current ? Theme.accent : Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                        Text { text: modelData.session_id; color: Theme.text; font { family: Theme.mono; pixelSize: 11 } width: parent.parent.width - 20; elide: Text.ElideMiddle }
                        Text { text: (modelData.updated_at || "") + "  ·  " + (modelData.task_count || 0) + " tasks" + (modelData.readable === false ? "  ·  UNREADABLE" : ""); color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { page.selectedSession = modelData.session_id; page.selectedTask = ""; page.selectedTaskRow = null; bridge.loadTasks(modelData.session_id) } }
                }
            }
        }
        Column {
            width: parent.width * 0.30; height: parent.height; spacing: 8
            Text { text: "TASKS"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ListView {
                width: parent.width; height: parent.height - 24; clip: true; spacing: 4
                model: page.selectedSession !== "" ? bridge.tasks : []
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 54; radius: Theme.radiusSmall
                    property bool current: modelData.task_id === page.selectedTask
                    color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14) : "transparent"
                    border.width: 1; border.color: current ? Theme.accent : Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                        Text { text: (modelData.capability || "?") + "  ·  " + (modelData.status || "?"); color: Theme.text; font { family: Theme.font; pixelSize: 12 } }
                        Text { text: modelData.task_id; color: Theme.textDim; font { family: Theme.mono; pixelSize: 10 } width: parent.parent.width - 20; elide: Text.ElideMiddle }
                        Text { text: (modelData.stage || "") + "  ·  " + (modelData.selected_connector || "—"); color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { page.selectedTask = modelData.task_id; page.selectedTaskRow = modelData; bridge.showTask(page.selectedSession, modelData.task_id) } }
                }
            }
        }
        Column {
            width: parent.width - parent.width * 0.62 - Theme.gap * 2; height: parent.height; spacing: 8
            Text { text: "SNAPSHOT"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ListView {
                width: parent.width; height: parent.height - 24; clip: true; spacing: 10
                model: page.selectedTask !== "" ? bridge.taskDetail : []
                delegate: TextEdit {
                    width: ListView.view.width; readOnly: true; selectByMouse: true; wrapMode: Text.Wrap
                    text: modelData.text
                    color: modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "approval" ? Theme.warning : Theme.text
                    font { family: Theme.mono; pixelSize: 12 }
                }
            }
        }
    }
}
