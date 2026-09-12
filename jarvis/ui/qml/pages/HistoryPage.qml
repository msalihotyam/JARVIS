import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Recent activity of this interface session plus the durable session
// listing. Bounded, read-only.
SecondaryPage {
    id: page
    title: "History"
    headerRight: [ ActionButton { label: "Refresh"; onClicked: bridge.refreshHistory() } ]

    Row {
        anchors.fill: parent
        spacing: Theme.gap
        Column {
            width: parent.width * 0.5 - Theme.gap / 2; height: parent.height; spacing: 8
            Text { text: "THIS INTERFACE SESSION"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ListView {
                width: parent.width; height: parent.height - 24; clip: true; spacing: 6
                model: bridge.activity
                ScrollBar.vertical: ScrollBar {}
                delegate: Row { width: ListView.view.width; spacing: 10
                    Text { text: modelData.time; color: Theme.textDim; font { family: Theme.mono; pixelSize: 11 } width: 64 }
                    Text { text: modelData.text; width: parent.width - 80; wrapMode: Text.Wrap
                        color: modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "approval" ? Theme.warning : Theme.text
                        font { family: Theme.font; pixelSize: 12 } }
                }
            }
        }
        Column {
            width: parent.width * 0.5 - Theme.gap / 2; height: parent.height; spacing: 8
            Text { text: "DURABLE SESSIONS (most recent 20)"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ListView {
                width: parent.width; height: parent.height - 24; clip: true; spacing: 4
                model: bridge.sessions
                ScrollBar.vertical: ScrollBar {}
                delegate: Rectangle {
                    width: ListView.view.width; height: 50; radius: Theme.radiusSmall
                    color: "transparent"; border.width: 1; border.color: Theme.border
                    Column { anchors.left: parent.left; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                        Text { text: modelData.session_id; color: Theme.text; font { family: Theme.mono; pixelSize: 11 } }
                        Text { text: (modelData.updated_at || "") + "  ·  " + (modelData.task_count || 0) + " tasks  ·  active: " + (modelData.active_task_id || "none") + (modelData.readable === false ? "  ·  UNREADABLE" : ""); color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
                    }
                    MouseArea { anchors.fill: parent; onClicked: { bridge.useSession(modelData.session_id); bridge.setPage("home") } }
                }
            }
        }
    }
}
