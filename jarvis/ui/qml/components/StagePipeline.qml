import QtQuick
import "../theme"

// A presentation of the existing four-stage lifecycle, never a plan or
// invented activity. Fixture text does not create a task or stage result.
Rectangle {
    id: pipeline
    property bool notes: bridge.feed.length === 0
    readonly property bool hasTask: !!bridge.task.task_id
    readonly property string cs: bridge.coreState
    implicitHeight: (notes ? 49 : 32) * 4 + 36
    height: implicitHeight; radius: 9
    color: Theme.alpha(Theme.bg0, 0.3); border.width: 1; border.color: Theme.alpha(Theme.accent, 0.14)
    function stage(i) {
        var t = bridge.task, has = hasTask, cs = pipeline.cs
        if (has && t.status === "rejected") return i === 0 ? "done" : i === 1 ? "denied" : "skipped"
        if (has && t.status === "cancelled") return i === 0 ? "done" : "skipped"
        if (i === 0) return cs === "running" && !has ? "running" : has || cs === "awaiting_input" ? "done" : "pending"
        if (i === 1) return cs === "awaiting_approval" ? "waiting" : has && (t.run_status || cs === "completed" || cs === "error" || cs === "unknown") ? "done" : "pending"
        if (i === 2) return cs === "running" && has ? "running" : cs === "completed" ? "done" : cs === "error" ? "error" : cs === "unknown" ? "unknown" : "pending"
        return cs === "completed" ? "done" : cs === "error" ? "error" : cs === "unknown" ? "unknown" : "pending"
    }
    Text { anchors.right: parent.right; anchors.rightMargin: 16; y: 7; text: pipeline.hasTask ? (bridge.task.status || "") : "No active task"; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
    Column {
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 20 }
        Repeater {
            model: [
                {name:"Understanding your request", note:"Your words become a proposal, never authority."},
                {name:"Authorization", note:"Review the exact action before it can execute."},
                {name:"Execution", note:"One capability and its granted destination."},
                {name:"Outcome", note:"Unknown outcomes stay explicit and are never replayed."}
            ]
            Item {
                width: parent.width; height: pipeline.notes ? 49 : 32
                readonly property string s: pipeline.stage(index)
                readonly property color tone: s === "done" ? Theme.success : s === "running" ? Theme.accent : s === "waiting" ? Theme.warning : s === "unknown" ? Theme.unknownEdge : s === "error" || s === "denied" ? Theme.danger : Theme.textMuted
                Rectangle { y: 7; width: 19; height: 19; radius: 9.5; color: s === "done" ? tone : "transparent"; border.width: 1.5; border.color: tone
                    Text { visible: s === "done"; anchors.centerIn: parent; text: "✓"; color: Theme.bg0; font.pixelSize: 12 }
                }
                Column { x: 38; y: 4; width: parent.width - 160; spacing: 2
                    Text { text: modelData.name; width: parent.width; elide: Text.ElideRight; color: Theme.textLabel; font { family: Theme.font; pixelSize: 14 } }
                    Text { visible: pipeline.notes; text: modelData.note; width: parent.width; elide: Text.ElideRight; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                }
                Text { anchors.right: parent.right; y: 5; text: ({pending:"Pending",waiting:"Awaiting you",running:"Running",done:"Completed",error:"Failed",unknown:"Outcome unknown",denied:"Denied",skipped:"Not run"})[s]; color: tone; font { family: Theme.font; pixelSize: 13 } }
            }
        }
    }
}
