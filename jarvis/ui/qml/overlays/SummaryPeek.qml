import QtQuick
import QtQuick.Shapes
import "../theme"
import "../components"

// The quick-look panel a summary card opens, anchored to its right. It
// overlays the workspace without reflowing it, closes on X / Escape / an
// outside click / the same card again, and switches in place when another
// card is picked.
//
// Content is real state only - the same derived state the rail shows, from
// the same bridge listings - with a truthful empty state where the product
// has nothing to list. The one deep action is the existing full page; no
// action is offered here that the product does not already have.
ContextPanel {
    id: peek
    property string key: ""
    property real cardY: 0
    property Item rail: null
    surfaceName: "summaryPeek"
    // The peek is the stronger member of the rail family: a crisp cyan edge.
    surfaceEdge: Theme.alpha(Theme.accent, 0.52)
    panelWidth: 392
    panelHeight: spec ? 50 + 16 + metricsHeight + 14 + listHeight + 14 + 58 + 44 : 300

    readonly property int metricsHeight: 68
    readonly property int rowHeight: 52
    readonly property int emptyHeight: 112
    // Optional detail is the first thing to give way on a short viewport.
    readonly property int maxRows: Math.max(2, Math.min(5, Math.floor((height - 108 - 50 - 16 - metricsHeight - 28 - 58 - 44) / rowHeight)))
    readonly property var rows: spec ? spec.rows.slice(0, maxRows) : []
    readonly property int listHeight: rows.length ? rows.length * rowHeight : emptyHeight

    function shortDate(v) {
        if (!v) return ""
        var d = new Date(v)
        return isNaN(d.getTime()) ? String(v).slice(0, 10) : Qt.formatDate(d, "d MMM")
    }

    readonly property var spec: {
        if (!rail) return null
        var r = rail
        switch (key) {
        case "Tasks": {
            var rows = []
            if (bridge.taskLive)
                rows.push({ title: bridge.task.capability || "Current request",
                            sub: bridge.task.stage ? "Stage: " + bridge.task.stage : (bridge.task.connector || "Live task"),
                            pill: r.statusWord(bridge.task.status) || "Running", meta: "Now",
                            dot: bridge.coreState === "awaiting_approval" ? "warn" : (bridge.coreState === "error" || bridge.coreState === "unknown") ? "alert" : "live" })
            bridge.sessions.forEach(function(s) {
                rows.push({ title: (s.task_count || 0) + ((s.task_count || 0) === 1 ? " task" : " tasks"),
                            sub: s.session_id, pill: s.active_task_id ? "Active task" : "",
                            meta: peek.shortDate(s.updated_at), dot: s.active_task_id ? "live" : "quiet" })
            })
            return { title: "Tasks", subtitle: "The live task and recorded sessions", icon: "tasks", tone: "#1b7fe0",
                     metrics: [{ value: bridge.taskLive ? 1 : 0, label: "Active task", tone: Theme.accent },
                               { value: r.taskTotal, label: "Tasks recorded", tone: Theme.textLabel }],
                     rows: rows, empty: "No tasks recorded yet.", emptyNote: "Ask JARVIS from the composer; tasks appear here.",
                     action: { label: "Open Tasks", note: "Go to the full tasks page", page: "tasks" } }
        }
        case "Updates":
            return { title: "Updates", subtitle: "This interface session", icon: "updates", tone: "#0f9e7e",
                     metrics: [{ value: bridge.activity.length, label: "Recent updates", tone: Theme.teal },
                               { value: r.attention, label: "Need attention", tone: r.attention > 0 ? Theme.danger : Theme.textLabel }],
                     rows: bridge.activity.map(function(a) {
                         return { title: a.text, sub: r.kindWord(a.kind) + " · " + a.time,
                                  pill: r.isAlert(a.kind) ? "Attention" : "", meta: r.ago(a.ts),
                                  dot: r.isAlert(a.kind) ? "alert" : a.kind === "scheduled" ? "live" : "quiet" } }),
                     empty: "No updates in this session yet.", emptyNote: "Notes from JARVIS and the scheduler appear here.",
                     action: { label: "Open History", note: "This session's updates and durable sessions", page: "history" } }
        case "Focus":
            return { title: "Focus", subtitle: "Active projects in memory", icon: "focus", tone: "#b7801e",
                     metrics: [{ value: bridge.projects.length, label: "Active projects", tone: Theme.warning },
                               { value: bridge.memoryItems.length,
                                 label: bridge.projects.length ? "Items in " + bridge.projects[0] : "Memory items", tone: Theme.textLabel }],
                     rows: bridge.projects.map(function(p, i) {
                         return { title: p, sub: i === 0 ? bridge.memoryItems.length + " memory items" : "Active project",
                                  pill: "", meta: "", dot: "live" } }),
                     empty: "No active focus context yet.", emptyNote: "Projects appear here once memory holds one.",
                     action: { label: "Open Memory", note: "Projects and reference data", page: "memory" } }
        case "Plan":
            return { title: "Plan", subtitle: "Scheduled work and next runs", icon: "plan", tone: "#7040c4",
                     metrics: [{ value: r.enabledJobs.length, label: "Scheduled", tone: Theme.violet },
                               { value: r.liveJobs.length - r.enabledJobs.length, label: "Paused", tone: Theme.textLabel }],
                     rows: r.liveJobs.map(function(j) {
                         return { title: j.label || j.id, sub: j.kind || "Job", pill: j.enabled ? "Scheduled" : "Paused",
                                  meta: j.enabled ? r.dueShort(j.next_due_at_utc) : "", dot: j.enabled ? "live" : "quiet" } }),
                     empty: "Nothing scheduled yet.", emptyNote: "Scheduled work appears here once a job exists.",
                     action: { label: "Open Automations", note: "Scheduled jobs and their next runs", page: "automations" } }
        }
        return null
    }

    header: Component {
        Row {
            spacing: 13
            Item {
                width: 40; height: 40
                anchors.verticalCenter: parent.verticalCenter
                SummaryIcon { anchors.centerIn: parent; icon: peek.spec ? peek.spec.icon : "tasks"; tint: "#e8f6ff"; scale: 1.7 }
            }
            Column {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 2
                Text { text: peek.spec ? peek.spec.title : ""; color: "#eef6fc"; font { family: Theme.font; pixelSize: 20; weight: Font.DemiBold } }
                Text { text: peek.spec ? peek.spec.subtitle : ""; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            }
        }
    }

    Column {
        width: parent.width
        spacing: 14
        visible: !!peek.spec

        // Compact summary metrics.
        Row {
            width: parent.width; height: peek.metricsHeight; spacing: 10
            Repeater {
                model: peek.spec ? peek.spec.metrics : []
                Rectangle {
                    width: (parent.width - 10) / 2; height: parent.height; radius: 10
                    gradient: Gradient {
                        GradientStop { position: 0; color: "#cc0f2639" }
                        GradientStop { position: 1; color: Theme.alpha(Theme.bg1, 0.6) }
                    }
                    border.width: 1; border.color: Theme.alpha(Theme.accent, 0.32)
                    Rectangle { x: 10; y: 1; width: parent.width - 20; height: 1; color: "#24bfeaff" }
                    Column {
                        x: 14; anchors.verticalCenter: parent.verticalCenter; spacing: -2
                        Text { text: modelData.value; color: modelData.tone; font { family: Theme.font; pixelSize: 30; weight: Font.DemiBold } }
                        Text { width: parent.parent.width - 28; elide: Text.ElideRight; text: modelData.label; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                    }
                }
            }
        }

        // Rows, or a truthful empty state.
        Item {
            width: parent.width; height: peek.listHeight
            Column {
                width: parent.width
                Repeater {
                    model: peek.rows
                    Item {
                        objectName: "summaryPeekRow"
                        width: parent.width; height: peek.rowHeight
                        readonly property color tone: modelData.dot === "alert" ? Theme.danger : modelData.dot === "warn" ? Theme.warning
                                                     : modelData.dot === "live" ? Theme.accent : Theme.textDim
                        Rectangle { visible: index > 0; width: parent.width; height: 1; color: Theme.alpha(Theme.accent, 0.14) }
                        Rectangle { x: 4; anchors.verticalCenter: parent.verticalCenter; width: 8; height: 8; radius: 4; color: parent.tone }
                        Column {
                            x: 22; anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - x - (pill.visible ? pill.width + 10 : 0) - (meta.visible ? meta.width + 12 : 0)
                            spacing: 2
                            Text { width: parent.width; elide: Text.ElideRight; text: modelData.title; color: "#eef6fc"; font { family: Theme.font; pixelSize: 15 } }
                            Text { width: parent.width; elide: Text.ElideRight; text: modelData.sub; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                        }
                        Rectangle {
                            id: pill
                            visible: modelData.pill !== ""
                            anchors { right: meta.visible ? meta.left : parent.right; rightMargin: meta.visible ? 12 : 2; verticalCenter: parent.verticalCenter }
                            width: pillText.width + 18; height: 24; radius: 7
                            color: Theme.alpha(parent.tone, modelData.dot === "quiet" ? 0.08 : 0.14)
                            border.width: 1; border.color: Theme.alpha(parent.tone, modelData.dot === "quiet" ? 0.25 : 0.42)
                            Text { id: pillText; anchors.centerIn: parent; text: modelData.pill
                                   color: modelData.dot === "quiet" ? Theme.textLabel : Qt.lighter(parent.parent.tone, 1.25)
                                   font { family: Theme.font; pixelSize: 11 } }
                        }
                        Text {
                            id: meta
                            visible: modelData.meta !== ""
                            anchors { right: parent.right; rightMargin: 2; verticalCenter: parent.verticalCenter }
                            text: modelData.meta; color: Theme.textLabel
                            font { family: Theme.font; pixelSize: 12 }
                        }
                    }
                }
            }
            Column {
                objectName: "summaryPeekEmpty"
                visible: peek.rows.length === 0
                anchors.centerIn: parent
                width: parent.width - 24; spacing: 8
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 38; height: 38; radius: 19; color: "transparent"
                    border.width: 1; border.color: Theme.alpha(Theme.accent, 0.35)
                    SummaryIcon { anchors.centerIn: parent; icon: peek.spec ? peek.spec.icon : "tasks"; tint: Theme.accent; scale: 0.85 }
                }
                Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; text: peek.spec ? peek.spec.empty : ""
                       color: Theme.text; font { family: Theme.font; pixelSize: 14 } }
                Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; text: peek.spec ? peek.spec.emptyNote : ""
                       color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            }
        }

        // The deep action: the existing full page, nothing else.
        Rectangle {
            objectName: "summaryPeekAction"
            width: parent.width; height: 58; radius: 10
            color: actionHit.containsMouse ? Theme.alpha(Theme.accent, 0.1) : Theme.alpha(Theme.bg1, 0.62)
            border.width: 1; border.color: actionHit.containsMouse ? Theme.alpha(Theme.accent, 0.6) : Theme.alpha(Theme.accent, 0.32)
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
            Accessible.role: Accessible.Button
            Accessible.name: peek.spec ? peek.spec.action.label : ""
            Shape {
                x: 16; anchors.verticalCenter: parent.verticalCenter
                width: 18; height: 18
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: Theme.accent; strokeWidth: 1.6; fillColor: "transparent"
                    capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                    startX: 8; startY: 2.5
                    PathLine { x: 2.5; y: 2.5 } PathLine { x: 2.5; y: 15.5 } PathLine { x: 15.5; y: 15.5 } PathLine { x: 15.5; y: 10 }
                    PathMove { x: 10.5; y: 2.5 } PathLine { x: 15.5; y: 2.5 } PathLine { x: 15.5; y: 7.5 }
                    PathMove { x: 15.5; y: 2.5 } PathLine { x: 8.5; y: 9.5 }
                }
            }
            Column {
                x: 48; anchors.verticalCenter: parent.verticalCenter; spacing: 1
                width: parent.width - 48 - 36
                Text { text: peek.spec ? peek.spec.action.label : ""; color: Theme.text; font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold } }
                Text { width: parent.width; elide: Text.ElideRight; text: peek.spec ? peek.spec.action.note : ""; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            }
            Text { anchors { right: parent.right; rightMargin: 16; verticalCenter: parent.verticalCenter }
                   text: "›"; color: Theme.accent; font.pixelSize: 22 }
            MouseArea {
                id: actionHit
                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                onClicked: { var page = peek.spec.action.page; peek.open = false; bridge.setPage(page) }
            }
        }
    }
}
