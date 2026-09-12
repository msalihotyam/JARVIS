import QtQuick
import QtQuick.Shapes
import "../theme"

// A presentation of the existing four-stage lifecycle, never a plan or
// invented activity. Fixture text does not create a task or stage result.
// One embedded HUD glass surface in the approved conversation language:
// the current activity as a quiet caption, then the stages - large state
// markers, bright stage names, hairline separators, statuses aligned at the
// far right.
//
// `reference` (the Canonical Command Center) takes the helper's rhythm:
// 50 px rows, 29 px markers, 17 px names. The Command preset keeps the
// earlier compact rhythm.
Rectangle {
    id: pipeline
    property bool notes: bridge.feed.length === 0
    property bool reference: false
    readonly property bool hasTask: !!bridge.task.task_id
    readonly property string cs: bridge.coreState
    readonly property int rowHeight: reference ? (notes ? 58 : 47) : (notes ? 54 : 43)
    readonly property int headHeight: reference ? 8 : 36
    readonly property int marker: reference ? 29 : 24
    readonly property int padL: reference ? 28 : 20
    readonly property int padR: reference ? 30 : 20
    readonly property int nameSize: reference ? 18 : 15
    implicitHeight: headHeight + rowHeight * 4 + (reference ? 8 : 8)
    height: implicitHeight
    radius: 12
    gradient: Gradient {
        GradientStop { position: 0; color: "#e6001829" }
        GradientStop { position: 1; color: "#e0001221" }
    }
    border.width: 1; border.color: "#24557a"

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

    // Embedded depth: a soft top illumination and a crisp inner edge light.
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
        height: 46; radius: 11
        gradient: Gradient {
            GradientStop { position: 0; color: "#1422b4e6" }
            GradientStop { position: 1; color: "transparent" }
        }
    }
    Rectangle { x: 16; y: 1; width: parent.width - 32; height: 1; color: "#4a6fd2f5" }
    Rectangle { x: 1; y: 14; width: 1; height: parent.height - 28; color: "#1f6fd2f5" }

    // The current activity: a quiet caption with the real task, if any.
    Item {
        id: head
        visible: !pipeline.reference
        x: pipeline.padL; width: parent.width - pipeline.padL - pipeline.padR; height: pipeline.headHeight
        Text { anchors.verticalCenter: parent.verticalCenter; anchors.verticalCenterOffset: 2; text: "CURRENT ACTIVITY"; color: "#7f97b8"
               font { family: Theme.font; pixelSize: 10; letterSpacing: 2.2; weight: Font.DemiBold } }
        Row {
            anchors { right: parent.right; verticalCenter: parent.verticalCenter; verticalCenterOffset: 2 }
            spacing: 10
            Text { anchors.verticalCenter: parent.verticalCenter; text: pipeline.activity; color: pipeline.hasTask ? "#e8f3fb" : "#7f97b8"
                   font { family: Theme.font; pixelSize: 12 } }
            Text { visible: pipeline.activityState !== ""; anchors.verticalCenter: parent.verticalCenter; text: pipeline.activityState; color: "#2cc9ea"
                   font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
        }
    }

    Column {
        x: pipeline.padL; y: head.height; width: parent.width - pipeline.padL - pipeline.padR
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
                readonly property color tone: s === "done" ? "#2fd79b" : s === "running" ? "#2cc9ea" : s === "waiting" ? Theme.warning
                                              : s === "unknown" ? Theme.unknownEdge : s === "error" || s === "denied" ? Theme.danger : "#c9dcf8"
                // Hairline separator between rows, clear of the far edges.
                Rectangle { visible: index > 0; x: -pipeline.padL + 16; width: parent.width + pipeline.padL + pipeline.padR - 32; height: 1; color: "#1b3e5a" }
                // In the reference layout the real task rides on the stage it
                // is in, instead of a separate caption strip.
                readonly property bool carriesTask: pipeline.reference && pipeline.hasTask && !quiet && s !== "done"
                readonly property string taskLine: pipeline.activity + (pipeline.activityState ? "  ·  " + pipeline.activityState : "")

                // State marker: a filled check when done, a lit ring while
                // running, a calm lavender ring while pending.
                Item {
                    x: 0; anchors.verticalCenter: parent.verticalCenter
                    width: pipeline.marker; height: width
                    Rectangle { visible: stageRow.s === "running"; anchors.centerIn: parent; width: parent.width + 12; height: width; radius: width / 2; color: "#2e11bfdc" }
                    Rectangle {
                        anchors.fill: parent; radius: width / 2
                        color: stageRow.s === "done" ? stageRow.tone : "transparent"
                        border.width: stageRow.s === "done" ? 0 : (pipeline.reference ? 2.5 : 2)
                        border.color: stageRow.quiet ? "#8a9fc4" : stageRow.tone
                    }
                    Shape {
                        visible: stageRow.s === "done"
                        anchors.centerIn: parent; width: 12; height: 9; scale: pipeline.marker / 24
                        preferredRendererType: Shape.CurveRenderer
                        ShapePath { strokeColor: "#001a14"; strokeWidth: 2; fillColor: "transparent"
                            capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                            startX: 1; startY: 4.6
                            PathLine { x: 4.4; y: 8 }
                            PathLine { x: 11; y: 1 }
                        }
                    }
                }
                Column {
                    x: pipeline.reference ? 55 : 46; anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - x - 150; spacing: 2
                    Text { text: modelData.name; width: parent.width; elide: Text.ElideRight
                           color: stageRow.quiet ? "#e3edf8" : "#f3feff"
                           font { family: Theme.font; pixelSize: pipeline.nameSize } }
                    Text { visible: pipeline.notes || stageRow.carriesTask; text: stageRow.carriesTask ? stageRow.taskLine : modelData.note
                           width: parent.width; elide: Text.ElideRight
                           color: stageRow.carriesTask ? "#7fd6ee" : "#7f97b8"; font { family: Theme.font; pixelSize: 12 } }
                }
                Text {
                    anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                    text: ({pending:"Pending",waiting:"Awaiting you",running:"Running",done:"Completed",error:"Failed",unknown:"Outcome unknown",denied:"Denied",skipped:"Not run"})[stageRow.s]
                    color: stageRow.tone
                    font { family: Theme.font; pixelSize: pipeline.nameSize }
                }
            }
        }
    }
}
