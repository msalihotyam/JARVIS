import QtQuick
import QtQuick.Shapes
import "../theme"

// A presentation of the existing four-stage lifecycle, never a plan or
// invented activity. Fixture text does not create a task or stage result.
// One bounded glass container in the approved conversation language: the
// current activity at the top, then the stages with restrained separators
// and clear completed / running / waiting / pending states.
Rectangle {
    id: pipeline
    property bool notes: bridge.feed.length === 0
    readonly property bool hasTask: !!bridge.task.task_id
    readonly property string cs: bridge.coreState
    readonly property int rowHeight: notes ? 54 : 43
    implicitHeight: 36 + rowHeight * 4 + 8
    height: implicitHeight
    radius: 12
    gradient: Gradient {
        GradientStop { position: 0; color: "#bd0e2438" }
        GradientStop { position: 1; color: Theme.alpha(Theme.bg1, 0.7) }
    }
    border.width: 1; border.color: Theme.alpha(Theme.accent, 0.33)
    // A crisp inner top highlight: depth without blur.
    Rectangle { x: 14; y: 1; width: parent.width - 28; height: 1; color: "#24bfeaff" }

    function stage(i) {
        var t = bridge.task, has = hasTask, cs = pipeline.cs
        if (has && t.status === "rejected") return i === 0 ? "done" : i === 1 ? "denied" : "skipped"
        if (has && t.status === "cancelled") return i === 0 ? "done" : "skipped"
        if (i === 0) return cs === "running" && !has ? "running" : has || cs === "awaiting_input" ? "done" : "pending"
        if (i === 1) return cs === "awaiting_approval" ? "waiting" : has && (t.run_status || cs === "completed" || cs === "error" || cs === "unknown") ? "done" : "pending"
        if (i === 2) return cs === "running" && has ? "running" : cs === "completed" ? "done" : cs === "error" ? "error" : cs === "unknown" ? "unknown" : "pending"
        return cs === "completed" ? "done" : cs === "error" ? "error" : cs === "unknown" ? "unknown" : "pending"
    }
    readonly property string activity: hasTask ? (bridge.task.capability || "Current request") : "No active task"
    readonly property string activityState: hasTask ? (({ running: "Running", awaiting_approval: "Awaiting you", awaiting_clarification: "Needs a detail",
                                                          completed: "Completed", failed: "Failed", rejected: "Denied", cancelled: "Cancelled" })[bridge.task.status] || bridge.task.status || "") : ""

    // Controlled glass: one soft sheen across the top.
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
        height: 40; radius: 11
        gradient: Gradient {
            GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0.05) }
            GradientStop { position: 1; color: "transparent" }
        }
    }

    // The current activity: the real task when there is one.
    Item {
        id: head
        x: 20; width: parent.width - 40; height: 36
        Text { anchors.verticalCenter: parent.verticalCenter; text: "CURRENT ACTIVITY"; color: Theme.textMuted; opacity: 0.85
               font { family: Theme.font; pixelSize: 10; letterSpacing: 2; weight: Font.DemiBold } }
        Row {
            anchors { right: parent.right; verticalCenter: parent.verticalCenter }
            spacing: 10
            Text { anchors.verticalCenter: parent.verticalCenter; text: pipeline.activity; color: pipeline.hasTask ? Theme.text : Theme.textDim
                   font { family: Theme.font; pixelSize: 12 } }
            Text { visible: pipeline.activityState !== ""; anchors.verticalCenter: parent.verticalCenter; text: pipeline.activityState; color: Theme.accent
                   font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
        }
    }

    Column {
        x: 20; y: head.height; width: parent.width - 40
        Repeater {
            model: [
                {name:"Understanding your request", note:"Your words become a proposal, never authority."},
                {name:"Authorization", note:"Review the exact action before it can execute."},
                {name:"Execution", note:"One capability and its granted destination."},
                {name:"Outcome", note:"Unknown outcomes stay explicit and are never replayed."}
            ]
            Item {
                id: stageRow
                width: parent.width; height: pipeline.rowHeight
                readonly property string s: pipeline.stage(index)
                readonly property bool quiet: s === "pending" || s === "skipped"
                readonly property color tone: s === "done" ? Theme.success : s === "running" ? Theme.accent : s === "waiting" ? Theme.warning
                                              : s === "unknown" ? Theme.unknownEdge : s === "error" || s === "denied" ? Theme.danger : Theme.textMuted
                Rectangle { width: parent.width; height: 1; color: Theme.alpha(Theme.accent, 0.15) }

                // State marker: a filled check when done, a lit ring while
                // running, a quiet ring otherwise.
                Item {
                    x: 2; anchors.verticalCenter: parent.verticalCenter
                    width: 24; height: 24
                    Rectangle { visible: stageRow.s === "running"; anchors.centerIn: parent; width: 34; height: 34; radius: 17; color: Theme.alpha(Theme.accent, 0.16) }
                    Rectangle {
                        anchors.fill: parent; radius: 12
                        color: stageRow.s === "done" ? stageRow.tone : "transparent"
                        border.width: stageRow.s === "done" ? 0 : 2
                        border.color: stageRow.quiet ? "#d9a9b9dc" : stageRow.tone
                    }
                    Shape {
                        visible: stageRow.s === "done"
                        anchors.centerIn: parent; width: 12; height: 9
                        preferredRendererType: Shape.CurveRenderer
                        ShapePath { strokeColor: Theme.bg0; strokeWidth: 2; fillColor: "transparent"
                            capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                            startX: 1; startY: 4.6
                            PathLine { x: 4.4; y: 8 }
                            PathLine { x: 11; y: 1 }
                        }
                    }
                }
                Column {
                    x: 46; anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - 190; spacing: 2
                    Text { text: modelData.name; width: parent.width; elide: Text.ElideRight
                           color: stageRow.quiet ? "#d3deec" : "#eef6fc"
                           font { family: Theme.font; pixelSize: 15 } }
                    Text { visible: pipeline.notes; text: modelData.note; width: parent.width; elide: Text.ElideRight
                           color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                }
                Text {
                    anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                    text: ({pending:"Pending",waiting:"Awaiting you",running:"Running",done:"Completed",error:"Failed",unknown:"Outcome unknown",denied:"Denied",skipped:"Not run"})[stageRow.s]
                    color: stageRow.quiet ? "#a9b9dc" : stageRow.tone
                    font { family: Theme.font; pixelSize: 15 }
                }
            }
        }
    }
}
