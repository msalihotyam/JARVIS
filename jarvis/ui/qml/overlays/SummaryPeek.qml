import QtQuick
import QtQuick.Shapes
import "../theme"
import "../components"

// The quick-look panel a summary tile opens, beside the sidebar. It
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
    // K6 / G6CM1FIX: the peek never covers the composer's INPUT bar - it may come down to 4 px
    // above the bar's top (the C1 chip row lifts the bar; ContextPanel keeps 16 px above
    // bottomLimit, hence + 12). Where the rows do not fit, ContextPanel moves the peek up
    // rather than below its tile. Measured when it opens and when the window resizes; nothing
    // can be typed while it is open. Closing keeps the last value, so the surface does not
    // change size as it fades out.
    onHeightChanged: if (open) bottomLimit = composerTop() + 12
    function composerTop() {
        var stack = [peek.Window.contentItem]
        while (stack.length) {
            var node = stack.pop()
            if (!node || !node.visible) continue
            if (node.objectName === "canonicalComposer") {
                var top = node
                for (var c = 0; c < node.children.length; c++)
                    if (node.children[c].objectName === "composerBar") top = node.children[c]
                return Math.min(peek.height, top.mapToItem(peek, 0, 0).y)
            }
            for (var i = 0; i < node.children.length; i++) stack.push(node.children[i])
        }
        return peek.height
    }
    // Optional detail is the first thing to give way on a short viewport.
    readonly property int maxRows: Math.max(2, Math.min(5, Math.floor((bottomLimit - 108 - 50 - 16 - metricsHeight - 28 - 62 - 44) / rowHeight)))
    readonly property var rows: spec ? spec.rows.slice(0, maxRows) : []
    readonly property int listHeight: rows.length ? rows.length * rowHeight : emptyHeight

    // K6B (Salih 2026-10-10): Focus shows the Focus tile's SELECTED scope - its
    // label and its records IN USE. The listing holds whatever the Memory page
    // loaded last (another project, or replaced / retired records too), so the
    // peek reloads the scope's project when it shows Focus and keeps only what
    // belongs to it. The no-project scope has no records of its own; an archived
    // project's are not listed (refreshMemory loads active projects only), so it reads "—", never a false 0.
    readonly property string focusScope: rail && !rail.global && !rail.scope.archived ? rail.scope.id : ""
    readonly property var focusItems: peek.focusScope ? bridge.memoryItems.filter(function(m) { return m.project_id === peek.focusScope && !!m.active }) : []
    function loadFocus() { if (peek.open && peek.key === "Focus" && peek.focusScope) bridge.refreshMemory(peek.focusScope, false) }
    onKeyChanged: loadFocus()
    onFocusScopeChanged: loadFocus()
    onOpenChanged: {
        if (!open) return
        bottomLimit = composerTop() + 12
        loadFocus()
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
                            sub: bridge.task.stage ? qsTr("Stage: %1").arg(i18n.word("stage", bridge.task.stage)) : (bridge.task.connector || qsTr("Live task")),
                            pill: r.statusWord(bridge.task.status) || qsTr("Running"), meta: qsTr("Now"),
                            dot: bridge.coreState === "awaiting_approval" ? "warn" : (bridge.coreState === "error" || bridge.coreState === "unknown") ? "alert" : "live" })
            bridge.sessions.forEach(function(s) {
                // A session is shown by its name; its id stays underneath.
                var count = (s.task_count || 0) === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(s.task_count || 0)
                rows.push({ title: s.title || count, sub: s.title ? count : qsTr("Untitled session"),
                            id: s.session_id, named: s.title || "", pill: s.active_task_id ? qsTr("Active task") : "",
                            meta: i18n.when(s.updated_at, r.now, "day"), dot: s.active_task_id ? "live" : "quiet", page: "tasks" })
            })
            return { title: qsTr("Tasks"), subtitle: qsTr("The live task and recorded sessions"), icon: "tasks", tone: "#1b7fe0",
                     metrics: [{ value: bridge.taskLive ? 1 : 0, label: qsTr("Active task"), tone: Theme.accent },
                               { value: r.taskTotal, label: qsTr("Tasks recorded"), tone: Theme.textLabel }],
                     rows: rows, empty: qsTr("No tasks recorded yet."), emptyNote: qsTr("Ask JARVIS from the composer; tasks appear here."),
                     action: { label: qsTr("Open Tasks"), note: qsTr("Go to the full tasks page"), page: "tasks" } }
        }
        case "Focus":
            return { title: qsTr("Focus"), subtitle: r.global ? "" : r.scope.name || "", icon: "focus", tone: "#b7801e",
                     metrics: [{ value: bridge.projects.length, label: qsTr("Active projects"), tone: "#37c4f0" },
                               { value: peek.focusScope ? peek.focusItems.length : "—",
                                 label: r.global ? qsTr("Memory items") : qsTr("Items in %1").arg(r.scope.name), tone: Theme.textLabel }],
                     rows: peek.focusItems.map(function(m) {
                         return { title: m.text || "", sub: i18n.word("memory_kind", m.kind || ""),
                                  pill: "", meta: "", dot: "quiet", project: peek.focusScope } }),
                     empty: qsTr("No active focus context yet."), emptyNote: "",
                     action: { label: qsTr("Open Memory"), note: qsTr("Projects and reference data"), page: "memory" } }
        case "Plan":
            return { title: qsTr("Plan"), subtitle: qsTr("Scheduled work and next runs"), icon: "plan", tone: "#7040c4",
                     metrics: [{ value: r.enabledJobs.length, label: qsTr("Scheduled"), tone: "#37c4f0" },
                               { value: r.liveJobs.length - r.enabledJobs.length, label: qsTr("Paused"), tone: Theme.textLabel }],
                     rows: r.liveJobs.map(function(j) {
                         return { title: j.label || j.id, sub: j.kind ? i18n.word("job_kind", j.kind) : qsTr("Job"), pill: j.enabled ? qsTr("Scheduled") : qsTr("Paused"),
                                  meta: j.enabled ? r.dueShort(j.next_due_at_utc) : "", dot: j.enabled ? "live" : "quiet", page: "automations" } }),
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
                width: parent.width - 53
                spacing: 2
                Text { text: peek.spec ? peek.spec.title : ""; color: "#fafeff"; font: Theme.titleLarge }
                // K6B: the Focus subtitle is the scope's own name - any length, so it elides with its tip.
                Text { width: parent.width; elide: Text.ElideRight; text: peek.spec ? peek.spec.subtitle : ""; color: "#a9bfdc"; font: Theme.body
                       HoverTip { text: parent.text; active: parent.truncated } }
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
                        Text { objectName: "summaryPeekMetricValue"; text: modelData.value; color: modelData.tone; font: Theme.figure(34) }
                        Row {
                            spacing: 7
                            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 7; height: 7; radius: 3.5; color: modelData.tone; opacity: 0.9 }
                            Text { width: tile.width - 46; elide: Text.ElideRight; text: modelData.label; color: "#b3c7e0"; font: Theme.caption
                                   HoverTip { text: parent.text; active: parent.truncated } }
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
                        // K6: a row with a place to go opens it (hover, pointer, Return); one without one is plain text.
                        readonly property bool actionable: !!modelData.page || !!modelData.project
                        function activate() {
                            if (!actionable) return
                            var page = modelData.page, project = modelData.project
                            peek.open = false
                            if (project) bridge.showProject(project); else bridge.setPage(page)
                        }
                        activeFocusOnTab: actionable
                        Accessible.role: actionable ? Accessible.Button : Accessible.StaticText
                        Accessible.name: modelData.title
                        Keys.onReturnPressed: activate()
                        Keys.onSpacePressed: activate()
                        Rectangle { objectName: "summaryPeekRowHover"; anchors.fill: parent; anchors.leftMargin: -4; anchors.rightMargin: -4; radius: 8
                                    visible: peekRow.actionable && (rowHit.containsMouse || peekRow.activeFocus); color: Theme.alpha(Theme.accent, 0.10) }
                        MouseArea { id: rowHit; anchors.fill: parent; enabled: peekRow.actionable; hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor; onClicked: peekRow.activate() }
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
                            Text { width: parent.width; elide: Text.ElideRight; text: modelData.title; color: "#eef6fc"; font: Theme.lead
                                   HoverTip { text: parent.text; active: parent.truncated } }
                            Text { width: parent.width; elide: Text.ElideRight; text: modelData.sub; color: Theme.textMuted; font: Theme.caption
                                   HoverTip { text: parent.text; active: parent.truncated } }
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
                Text { width: parent.width; elide: Text.ElideRight; text: peek.spec ? peek.spec.action.note : ""; color: Theme.textMuted; font: Theme.caption
                       HoverTip { text: parent.text; active: parent.truncated } }
            }
            Icon { anchors { right: parent.right; rightMargin: 14; verticalCenter: parent.verticalCenter }
                   name: "chevron-right"; size: 18; tint: Theme.accent }
            MouseArea {
                id: actionHit
                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                onClicked: { var page = peek.spec.action.page; peek.open = false; bridge.setPage(page) }
            }
        }
    }
}
