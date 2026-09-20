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
    surfaceEdge: "#368ed1"
    surfaceFill: "#f5000e1a"
    surfaceBrackets: false
    surfaceShell: "../assets/peek-shell.png"
    surfaceShellMargin: 18
    surfaceShellBorder: 46
    surfaceRadius: 16
    panelWidth: 432
    panelHeight: spec ? 50 + 16 + metricsHeight + 14 + listHeight + 14 + 62 + 44 : 300

    readonly property int metricsHeight: 76
    readonly property int rowHeight: 52
    readonly property int emptyHeight: 112
    // Optional detail is the first thing to give way on a short viewport.
    readonly property int maxRows: Math.max(2, Math.min(5, Math.floor((height - 108 - 50 - 16 - metricsHeight - 28 - 62 - 44) / rowHeight)))
    readonly property var rows: spec ? spec.rows.slice(0, maxRows) : []
    readonly property int listHeight: rows.length ? rows.length * rowHeight : emptyHeight

    function shortDate(v) {
        if (!v) return ""
        var d = new Date(v)
        return isNaN(d.getTime()) ? String(v).slice(0, 10) : d.toLocaleDateString(Qt.locale(i18n.qtLocale), "d MMM")
    }

    readonly property var spec: {
        if (!rail) return null
        var r = rail
        switch (key) {
        case "Tasks": {
            var rows = []
            if (bridge.taskLive)
                rows.push({ title: bridge.task.title || bridge.task.capability || qsTr("Current request"),
                            id: bridge.task.task_id || "", named: bridge.task.title || "",
                            sub: bridge.task.stage ? qsTr("Stage: %1").arg(bridge.task.stage) : (bridge.task.connector || qsTr("Live task")),
                            pill: r.statusWord(bridge.task.status) || qsTr("Running"), meta: qsTr("Now"),
                            dot: bridge.coreState === "awaiting_approval" ? "warn" : (bridge.coreState === "error" || bridge.coreState === "unknown") ? "alert" : "live" })
            bridge.sessions.forEach(function(s) {
                // A session is shown by its name; its id stays underneath.
                var count = (s.task_count || 0) === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(s.task_count || 0)
                rows.push({ title: s.title || count, sub: s.title ? count : qsTr("Untitled session"),
                            id: s.session_id, named: s.title || "", pill: s.active_task_id ? qsTr("Active task") : "",
                            meta: peek.shortDate(s.updated_at), dot: s.active_task_id ? "live" : "quiet" })
            })
            return { title: qsTr("Tasks"), subtitle: qsTr("The live task and recorded sessions"), icon: "tasks", tone: "#1b7fe0",
                     metrics: [{ value: bridge.taskLive ? 1 : 0, label: qsTr("Active task"), tone: Theme.accent },
                               { value: r.taskTotal, label: qsTr("Tasks recorded"), tone: Theme.textLabel }],
                     rows: rows, empty: qsTr("No tasks recorded yet."), emptyNote: qsTr("Ask JARVIS from the composer; tasks appear here."),
                     action: { label: qsTr("Open Tasks"), note: qsTr("Go to the full tasks page"), page: "tasks" } }
        }
        case "Updates":
            return { title: qsTr("Updates"), subtitle: qsTr("This interface session"), icon: "updates", tone: "#0f9e7e",
                     metrics: [{ value: bridge.activity.length, label: qsTr("Recent updates"), tone: "#37c4f0" },
                               { value: r.attention, label: qsTr("Need attention"), tone: r.attention > 0 ? Theme.danger : Theme.textLabel }],
                     rows: bridge.activity.map(function(a) {
                         return { title: a.text, sub: r.kindWord(a.kind) + " · " + a.time,
                                  pill: r.isAlert(a.kind) ? qsTr("Attention") : "", meta: r.ago(a.ts),
                                  dot: r.isAlert(a.kind) ? "alert" : a.kind === "scheduled" ? "live" : "quiet" } }),
                     empty: qsTr("No updates in this session yet."), emptyNote: qsTr("Notes from JARVIS and the scheduler appear here."),
                     action: { label: qsTr("Open History"), note: qsTr("This session's updates and durable sessions"), page: "history" } }
        case "Focus":
            return { title: qsTr("Focus"), subtitle: qsTr("Active projects in memory"), icon: "focus", tone: "#b7801e",
                     metrics: [{ value: bridge.projects.length, label: qsTr("Active projects"), tone: "#37c4f0" },
                               { value: bridge.memoryItems.length,
                                 label: bridge.projects.length ? qsTr("Items in %1").arg(bridge.projects[0]) : qsTr("Memory items"), tone: Theme.textLabel }],
                     rows: bridge.projects.map(function(p, i) {
                         return { title: p, sub: i === 0 ? qsTr("%1 memory items").arg(bridge.memoryItems.length) : qsTr("Active project"),
                                  pill: "", meta: "", dot: "live" } }),
                     empty: qsTr("No active focus context yet."), emptyNote: qsTr("Projects appear here once memory holds one."),
                     action: { label: qsTr("Open Memory"), note: qsTr("Projects and reference data"), page: "memory" } }
        case "Plan":
            return { title: qsTr("Plan"), subtitle: qsTr("Scheduled work and next runs"), icon: "plan", tone: "#7040c4",
                     metrics: [{ value: r.enabledJobs.length, label: qsTr("Scheduled"), tone: "#37c4f0" },
                               { value: r.liveJobs.length - r.enabledJobs.length, label: qsTr("Paused"), tone: Theme.textLabel }],
                     rows: r.liveJobs.map(function(j) {
                         return { title: j.label || j.id, sub: j.kind || qsTr("Job"), pill: j.enabled ? qsTr("Scheduled") : qsTr("Paused"),
                                  meta: j.enabled ? r.dueShort(j.next_due_at_utc) : "", dot: j.enabled ? "live" : "quiet" } }),
                     empty: qsTr("Nothing scheduled yet."), emptyNote: qsTr("Scheduled work appears here once a job exists."),
                     action: { label: qsTr("Open Automations"), note: qsTr("Scheduled jobs and their next runs"), page: "automations" } }
        }
        return null
    }

    RenameMenu { id: renameMenu }

    header: Component {
        Row {
            spacing: 13
            Item {
                width: 40; height: 40
                anchors.verticalCenter: parent.verticalCenter
                SummaryIcon { anchors.centerIn: parent; icon: peek.spec ? peek.spec.icon : "tasks"; tint: "#eef8ff"; scale: 1.95 }
            }
            Column {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 2
                Text { text: peek.spec ? peek.spec.title : ""; color: "#fafeff"; font: Theme.titleLarge }
                Text { text: peek.spec ? peek.spec.subtitle : ""; color: "#a9bfdc"; font: Theme.body }
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
                    id: tile
                    width: (parent.width - 10) / 2; height: parent.height; radius: 12
                    gradient: Gradient {
                        GradientStop { position: 0; color: "#f2002238" }
                        GradientStop { position: 1; color: "#f0001524" }
                    }
                    border.width: 1; border.color: "#367fbf"
                    Rectangle { z: -1; anchors.fill: parent; anchors.margins: -2; radius: 14; color: "transparent"; border.width: 2; border.color: "#1a3fb4e6" }
                    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: 26; radius: 11
                        gradient: Gradient { GradientStop { position: 0; color: "#1c3fc4f0" } GradientStop { position: 1; color: "transparent" } } }
                    Rectangle { x: 12; y: 1; width: parent.width - 24; height: 1; color: Theme.alpha(Theme.materialGlint, 0.35) }
                    Column {
                        x: 16; anchors.verticalCenter: parent.verticalCenter; spacing: -1
                        Text { text: modelData.value; color: modelData.tone; font: Theme.figure(34) }
                        Row {
                            spacing: 7
                            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 7; height: 7; radius: 3.5; color: modelData.tone; opacity: 0.9 }
                            Text { width: tile.width - 46; elide: Text.ElideRight; text: modelData.label; color: "#b3c7e0"; font: Theme.caption }
                        }
                    }
                }
            }
        }

        // Rows, or a truthful empty state.
        Item {
            width: parent.width; height: peek.listHeight
            Rectangle {
                visible: peek.rows.length > 0
                anchors.fill: parent; anchors.leftMargin: -8; anchors.rightMargin: -8; radius: 12
                color: "#a6001321"; border.width: 1; border.color: "#1a4462"
                Rectangle { x: 14; y: 1; width: parent.width - 28; height: 1; color: "#306fd2f5" }
            }
            Column {
                width: parent.width
                Repeater {
                    model: peek.rows
                    Item {
                        id: peekRow
                        objectName: "summaryPeekRow"
                        width: parent.width; height: peek.rowHeight
                        // Right-click a task or a session: Rename.
                        TapHandler { acceptedButtons: Qt.RightButton; enabled: !!modelData.id
                                     onTapped: function(point) { renameMenu.openFor(peekRow, point.position, modelData.id, modelData.named) } }
                        readonly property color tone: modelData.dot === "alert" ? Theme.danger : modelData.dot === "warn" ? Theme.warning
                                                     : modelData.dot === "live" ? Theme.accent : Theme.textDim
                        Rectangle { visible: index > 0; width: parent.width; height: 1; color: Theme.alpha(Theme.accent, 0.14) }
                        Rectangle { x: 4; anchors.verticalCenter: parent.verticalCenter; width: 8; height: 8; radius: 4; color: parent.tone }
                        Column {
                            x: 22; anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - x - (pill.visible ? pill.width + 10 : 0) - (meta.visible ? meta.width + 12 : 0)
                            spacing: 2
                            Text { width: parent.width; elide: Text.ElideRight; text: modelData.title; color: "#eef6fc"; font: Theme.lead }
                            Text { width: parent.width; elide: Text.ElideRight; text: modelData.sub; color: Theme.textMuted; font: Theme.caption }
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
                                   font: Theme.caption }
                        }
                        Text {
                            id: meta
                            visible: modelData.meta !== ""
                            anchors { right: parent.right; rightMargin: 2; verticalCenter: parent.verticalCenter }
                            text: modelData.meta; color: Theme.textLabel
                            font: Theme.caption
                        }
                    }
                }
            }
            // A designed inset well for the truthful empty state.
            Rectangle {
                visible: peek.rows.length === 0
                anchors.fill: parent; radius: 10
                color: "#b3001523"; border.width: 1; border.color: "#173e58"
                Rectangle { x: 12; y: 1; width: parent.width - 24; height: 1; color: "#246fd2f5" }
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
                       color: Theme.text; font: Theme.body }
                Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; text: peek.spec ? peek.spec.emptyNote : ""
                       color: Theme.textMuted; font: Theme.caption }
            }
        }

        // The deep action: the existing full page, nothing else.
        Rectangle {
            objectName: "summaryPeekAction"
            width: parent.width; height: 62; radius: 12
            gradient: Gradient {
                GradientStop { position: 0; color: actionHit.containsMouse ? "#f4003050" : "#f2002034" }
                GradientStop { position: 1; color: actionHit.containsMouse ? "#f0002238" : "#f0001524" }
            }
            border.width: 1; border.color: actionHit.containsMouse ? "#6fd6f2" : "#3a92b8"
            Rectangle { z: -1; anchors.fill: parent; anchors.margins: -2; radius: 14; color: "transparent"; border.width: 2
                        border.color: actionHit.containsMouse ? "#3a3fc4f0" : "#1a3fb4e6" }
            Rectangle { x: 12; y: 1; width: parent.width - 24; height: 1; color: "#5a8fe0ff" }
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
                Text { text: peek.spec ? peek.spec.action.label : ""; color: "#fafeff"; font: Theme.subtitle }
                Text { width: parent.width; elide: Text.ElideRight; text: peek.spec ? peek.spec.action.note : ""; color: Theme.textMuted; font: Theme.caption }
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
