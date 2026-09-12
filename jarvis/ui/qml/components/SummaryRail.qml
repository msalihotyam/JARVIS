import QtQuick
import QtQuick.Shapes
import "../theme"

// The left ambient rail's summary stack: Tasks, Updates, Focus, Plan. It is
// contextual information, not navigation chrome. Each card is one glance at
// real state and never expands in the rail; a click opens a quick-look peek
// anchored to its right (SummaryPeek), and the full page stays one step
// further for deeper work.
//
// Every figure comes from a bridge listing; nothing is invented to fill the
// approved layout:
//   Tasks   -> the live task and the durable sessions      (Tasks page)
//   Updates -> this interface session's activity notes     (History page,
//              whose "this interface session" list is exactly these notes)
//   Focus   -> the active memory projects                  (Memory page)
//   Plan    -> the scheduled automation jobs               (Automations page)
//
// The shaping helpers live here so the peek reads the same derived state.
Item {
    id: rail
    objectName: "summaryRail"
    property string activeKey: ""
    signal picked(string key, real cardY, real cardHeight)

    // A slow clock so relative ages stay honest without per-second redraws.
    property real now: Date.now()
    Timer { interval: 30000; running: rail.visible; repeat: true; onTriggered: rail.now = Date.now() }

    function ago(ts) {
        if (!ts) return ""
        var s = Math.max(0, Math.round(rail.now / 1000 - ts))
        return s < 60 ? s + "s ago" : s < 3600 ? Math.floor(s / 60) + "m ago"
             : s < 86400 ? Math.floor(s / 3600) + "h ago" : Math.floor(s / 86400) + "d ago"
    }
    function dueShort(iso) {
        if (!iso) return ""
        var d = new Date(iso)
        if (isNaN(d.getTime())) return ""
        return d.toDateString() === new Date(rail.now).toDateString() ? Qt.formatTime(d, "HH:mm") : Qt.formatDate(d, "ddd d MMM")
    }
    function statusWord(s) {
        return ({ running: "Running", awaiting_approval: "Awaiting you", awaiting_clarification: "Needs a detail",
                  completed: "Completed", failed: "Failed", rejected: "Denied", cancelled: "Cancelled" })[s] || (s || "")
    }
    function kindWord(k) {
        return ({ system: "System", scheduled: "Scheduled", approval: "Approval", error: "Error",
                  unknown: "Outcome unknown", data: "Data" })[k] || "Update"
    }
    function isAlert(k) { return k === "error" || k === "unknown" || k === "approval" }

    // -- real state, shaped once ------------------------------------------
    readonly property var liveJobs: bridge.jobs.filter(function(j) { return !j.archived && j.readable !== false })
    readonly property var enabledJobs: liveJobs.filter(function(j) { return j.enabled === true })
    readonly property var nextJob: {
        var best = null
        for (var i = 0; i < enabledJobs.length; i++) {
            var j = enabledJobs[i]
            if (j.next_due_at_utc && (!best || j.next_due_at_utc < best.next_due_at_utc)) best = j
        }
        return best
    }
    readonly property int attention: (bridge.approvalPending ? 1 : 0)
                                     + bridge.activity.filter(function(a) { return rail.isAlert(a.kind) }).length
    readonly property int taskTotal: bridge.sessions.reduce(function(n, s) { return n + (s.task_count || 0) }, 0)

    readonly property bool roomy: width >= 200
    readonly property bool tight: width < 150
    readonly property color activeFill: Qt.tint(Theme.bg1, Theme.alpha(Theme.accentDeep, 0.34))
    readonly property string sessionsWord: bridge.sessions.length + (bridge.sessions.length === 1 ? " session" : " sessions")
    readonly property var modules: [
        { key: "Tasks", page: "tasks", tone: "#1b7fe0", icon: "tasks",
          line: (bridge.taskLive ? 1 : 0) + " active",
          note: rail.roomy ? rail.sessionsWord + " · " + rail.taskTotal + (rail.taskTotal === 1 ? " task" : " tasks") : rail.sessionsWord },
        { key: "Updates", page: "history", tone: "#0f9e7e", icon: "updates",
          line: bridge.activity.length + " recent",
          note: rail.attention > 0 ? rail.attention + (rail.roomy ? (rail.attention === 1 ? " needs attention" : " need attention") : " to review") : "All clear" },
        { key: "Focus", page: "memory", tone: "#b7801e", icon: "focus",
          line: bridge.projects.length + (bridge.projects.length === 1 ? " project" : " projects"),
          note: bridge.projects.length ? (rail.roomy ? "Active in memory" : "In memory")
                                       : (rail.roomy ? "No focus context yet" : rail.tight ? "No context" : "No context yet") },
        { key: "Plan", page: "automations", tone: "#7040c4", icon: "plan",
          line: rail.enabledJobs.length + (rail.tight ? (rail.enabledJobs.length === 1 ? " job" : " jobs") : " scheduled"),
          note: rail.nextJob ? "Next " + rail.dueShort(rail.nextJob.next_due_at_utc)
                             : (rail.liveJobs.length ? (rail.roomy ? "Nothing enabled" : "None enabled") : (rail.roomy ? "Nothing scheduled" : "None yet")) }
    ]

    // -- layout budget: the secondary line goes first, then micro spacing ---
    readonly property real cardFull: 68
    readonly property real cardCompact: 50
    readonly property bool showNote: 4 * cardFull + 30 <= height
    readonly property real cardHeight: showNote ? cardFull : cardCompact
    readonly property real gap: 4 * cardHeight + 30 <= height ? 10 : 6

    Column {
        width: rail.width
        spacing: rail.gap
        Repeater {
            model: rail.modules
            Rectangle {
                id: card
                objectName: "rail" + modelData.key
                readonly property bool active: rail.activeKey === modelData.key
                readonly property bool hovered: hit.containsMouse
                width: rail.width
                height: rail.cardHeight
                radius: 14
                // Closed: controlled glass. Active: a restrained cyan
                // treatment - brighter edge and a slight lift, no glow bloom.
                color: active ? rail.activeFill
                              : hovered ? Theme.alpha(Theme.bg1, 0.82) : Theme.alpha(Theme.bg1, 0.6)
                border.width: 1
                border.color: active ? Theme.alpha(Theme.accent, 0.78)
                                     : hovered ? Theme.alpha(Theme.accent, 0.42) : Theme.alpha(Theme.accent, 0.19)
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
                activeFocusOnTab: true
                Accessible.role: Accessible.Button
                Accessible.name: modelData.key + ": " + modelData.line
                Keys.onReturnPressed: rail.picked(modelData.key, card.y, card.height)
                Keys.onSpacePressed: rail.picked(modelData.key, card.y, card.height)

                Rectangle {
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
                    height: Math.min(parent.height, 36); radius: 13
                    gradient: Gradient {
                        GradientStop { position: 0; color: Theme.alpha(Theme.accent, card.active ? 0.12 : 0.08) }
                        GradientStop { position: 1; color: "transparent" }
                    }
                }
                Rectangle { x: 12; y: 1; width: parent.width - 24; height: 1; color: card.active ? "#4dbfeaff" : "#24bfeaff" }
                Rectangle {
                    anchors.fill: parent; anchors.margins: -3; radius: 14
                    color: "transparent"; border.color: Theme.alpha(Theme.accent, 0.7)
                    visible: card.activeFocus
                }
                // The pointer into the open peek: a small triangle leaving
                // the active card's edge, stroked on its outer sides only.
                Shape {
                    visible: card.active
                    x: parent.width - 2; y: parent.height / 2 - 11
                    width: 13; height: 22
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        strokeWidth: -1; fillColor: rail.activeFill
                        startX: 0; startY: 0
                        PathLine { x: 1.6; y: 0 } PathLine { x: 12; y: 11 } PathLine { x: 1.6; y: 22 } PathLine { x: 0; y: 22 } PathLine { x: 0; y: 0 }
                    }
                    ShapePath {
                        strokeColor: Theme.alpha(Theme.accent, 0.78); strokeWidth: 1; fillColor: "transparent"
                        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.FlatCap
                        startX: 1.6; startY: 0.5
                        PathLine { x: 11.5; y: 11 } PathLine { x: 1.6; y: 21.5 }
                    }
                }

                // Icon tile: a solid tinted instrument with a drawn line icon.
                Rectangle {
                    id: tile
                    x: rail.tight ? 9 : 11; anchors.verticalCenter: parent.verticalCenter
                    width: rail.tight ? 34 : rail.showNote ? 42 : 32; height: width; radius: rail.tight ? 9 : rail.showNote ? 10 : 8
                    gradient: Gradient {
                        GradientStop { position: 0; color: Qt.darker(modelData.tone, 1.2) }
                        GradientStop { position: 1; color: Qt.darker(modelData.tone, 2.25) }
                    }
                    border.width: 1; border.color: Theme.alpha(Qt.lighter(modelData.tone, 1.9), 0.42)
                    Rectangle { x: 6; y: 1; width: parent.width - 12; height: 1; color: Qt.rgba(1, 1, 1, 0.2) }
                    SummaryIcon { anchors.centerIn: parent; icon: modelData.icon; scale: tile.width / 42 * 1.22 }
                }

                Column {
                    x: tile.x + tile.width + (rail.tight ? 8 : 10)
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - x - (rail.tight ? 17 : 24)
                    spacing: 0
                    Text { width: parent.width; elide: Text.ElideRight; text: modelData.key; color: "#eef6fc"
                           font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold } }
                    Text { width: parent.width; elide: Text.ElideRight; text: modelData.line
                           color: modelData.key === "Tasks" ? Theme.accent : Qt.lighter(modelData.tone, 1.8)
                           font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold } }
                    Text { visible: rail.showNote; width: parent.width; elide: Text.ElideRight; text: modelData.note; color: Theme.textMuted
                           font { family: Theme.font; pixelSize: 12 } }
                }
                Shape {
                    visible: !card.active
                    x: parent.width - (rail.tight ? 14 : 18); anchors.verticalCenter: parent.verticalCenter
                    width: 7; height: 12
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        strokeColor: card.active || card.hovered ? Qt.lighter(Theme.accent, 1.25) : Theme.accent
                        strokeWidth: 1.7; fillColor: "transparent"
                        capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                        startX: 1; startY: 1
                        PathLine { x: 6; y: 6 }
                        PathLine { x: 1; y: 11 }
                    }
                }

                MouseArea {
                    id: hit
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: rail.picked(modelData.key, card.y, card.height)
                }
            }
        }
    }
}
