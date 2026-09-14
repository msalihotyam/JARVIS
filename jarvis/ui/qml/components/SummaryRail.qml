import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import "../theme"

// The left ambient rail's summary stack: Tasks, Updates, Focus, Plan, Memory.
// It is contextual information, not navigation chrome. Each card is one glance
// at real state and never expands in the rail; a click opens a quick-look peek
// anchored to its right (SummaryPeek), and the full page stays one step
// further for deeper work. Memory is informational only: no peek, no route.
//
// Every figure comes from a bridge listing; nothing is invented to fill the
// approved layout:
//   Tasks   -> the live task and the durable sessions      (Tasks page)
//   Updates -> this interface session's activity notes     (History page,
//              whose "this interface session" list is exactly these notes)
//   Focus   -> the active memory projects                  (Memory page)
//   Plan    -> the scheduled automation jobs               (Automations page)
//   Memory  -> the backend's reported memory status        (no page)
//
// Order and visibility are the persisted layout (bridge.railCards). In Edit
// Mode the card bodies stop navigating and each card offers its layout menu.
//
// The shaping helpers live here so the peek reads the same derived state.
Item {
    id: rail
    objectName: "summaryRail"
    property string activeKey: ""
    signal picked(string key, real cardY, real cardHeight)
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)

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
        { id: "tasks", key: "Tasks", page: "tasks", icon: "tasks",
          line: (bridge.taskLive ? 1 : 0) + " active",
          note: rail.roomy ? rail.sessionsWord + " · " + rail.taskTotal + (rail.taskTotal === 1 ? " task" : " tasks") : rail.sessionsWord },
        { id: "updates", key: "Updates", page: "history", icon: "updates",
          line: bridge.activity.length + " recent",
          note: rail.attention > 0 ? rail.attention + (rail.roomy ? (rail.attention === 1 ? " needs attention" : " need attention") : " to review") : "All clear" },
        { id: "focus", key: "Focus", page: "memory", icon: "focus",
          line: bridge.projects.length + (bridge.projects.length === 1 ? " project" : " projects"),
          note: bridge.projects.length ? (rail.roomy ? "Active in memory" : "In memory")
                                       : (rail.roomy ? "No focus context yet" : rail.width < 180 ? "No context" : "No context yet") },
        { id: "plan", key: "Plan", page: "automations", icon: "plan",
          line: rail.enabledJobs.length + (rail.tight ? (rail.enabledJobs.length === 1 ? " job" : " jobs") : " scheduled"),
          note: rail.nextJob ? "Next " + rail.dueShort(rail.nextJob.next_due_at_utc)
                             : (rail.liveJobs.length ? (rail.roomy ? "Nothing enabled" : "None enabled") : (rail.roomy ? "Nothing scheduled" : "None yet")) },
        // A status, not a count: the whole line is the status word.
        { id: "memory", key: "Memory", page: "", icon: "memory", status: true,
          line: bridge.memoryStatus,
          note: "Persistent across sessions", shortNote: "Persistent" }
    ]
    readonly property var shownModules: {
        var byId = {}
        rail.modules.forEach(function(m) { byId[m.id] = m })
        return bridge.railCards.filter(function(c) { return c.visible }).map(function(c) { return byId[c.id] })
    }
    readonly property int count: Math.max(1, shownModules.length)

    // Share the available height, which already excludes the lower HUD, among
    // the cards shown. Extra space is breathing room for real content, never
    // invented rows; a fifth card takes blank space, not text size.
    readonly property real cardFull: Math.max(84, (height - 12 * (count - 1)) / count)
    readonly property real cardCompact: 50
    readonly property bool showNote: count * cardFull + 10 * (count - 1) <= height
    readonly property real cardHeight: showNote ? cardFull : cardCompact
    readonly property real gap: count * cardHeight + 12 * (count - 1) <= height ? 12 : 6

    Column {
        width: rail.width
        spacing: rail.gap
        Repeater {
            model: rail.shownModules
            Rectangle {
                id: card
                objectName: "rail" + modelData.key
                readonly property bool editing: bridge.editMode
                // Memory has no peek or page: outside editing it is read, not pressed.
                readonly property bool navigable: !editing && !modelData.status
                readonly property bool active: rail.activeKey === modelData.key
                readonly property bool hovered: hit.containsMouse
                width: rail.width
                height: rail.cardHeight
                radius: 14
                // Closed: controlled glass. Active: a restrained cyan
                // treatment - brighter edge and a slight lift, no glow bloom.
                color: active ? rail.activeFill : hovered ? "#b6051a30" : Theme.glassSecondaryShell
                border.width: 0
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                activeFocusOnTab: card.navigable
                Accessible.role: card.navigable ? Accessible.Button : Accessible.StaticText
                Accessible.name: modelData.key + ": " + modelData.line
                Keys.onReturnPressed: if (card.navigable) rail.picked(modelData.key, card.y, card.height)
                Keys.onSpacePressed: if (card.navigable) rail.picked(modelData.key, card.y, card.height)

                // The shell: a static nine-slice of light - soft outer glow, a
                // cyan rim brightest where light catches the upper left, and a
                // faint secondary inner rim. The active shell fades in over it.
                BorderImage {
                    anchors.fill: parent; anchors.margins: -12
                    source: "../assets/rail-card-shell.png"
                    layer.enabled: true
                    layer.effect: MultiEffect { colorization: 0.85; colorizationColor: "#55b7ff"; brightness: 0.12 }
                    border { left: 34; top: 34; right: 34; bottom: 34 }
                    opacity: card.active ? 0 : card.hovered ? 1 : 0.9
                    Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
                }
                BorderImage {
                    anchors.fill: parent; anchors.margins: -12
                    source: "../assets/rail-card-shell-active.png"
                    layer.enabled: true
                    layer.effect: MultiEffect { colorization: 0.85; colorizationColor: "#55b7ff"; brightness: 0.12 }
                    border { left: 34; top: 34; right: 34; bottom: 34 }
                    opacity: card.active ? 1 : card.hovered ? 0.35 : 0
                    visible: opacity > 0
                    Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
                }
                // Inner light from the upper left, fading down and across.
                Rectangle {
                    anchors.fill: parent; anchors.margins: 1; radius: 13
                    gradient: Gradient {
                        GradientStop { position: 0; color: card.active ? "#363692ff" : Theme.glassMicro }
                        GradientStop { position: 0.55; color: "transparent" }
                    }
                }
                Rectangle {
                    anchors.fill: parent; anchors.margins: -3; radius: 14
                    color: "transparent"; border.color: Theme.alpha(Theme.accent, 0.7)
                    visible: card.activeFocus
                }
                // The pointer into the open peek: a filled tab leaving the
                // active card's edge, stroked on its outer sides only.
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
                        strokeColor: Theme.materialGlint; strokeWidth: 1.2; fillColor: "transparent"
                        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.FlatCap
                        startX: 1.6; startY: 0.5
                        PathLine { x: 11.5; y: 11 } PathLine { x: 1.6; y: 21.5 }
                    }
                }

                // Icon socket: a compact recessed optical socket, nearly flush
                // with the card - deep navy, restrained cool-blue edge accents,
                // one blue-white line glyph. One family for every module; the
                // card, its labels and its whole hit area are unchanged.
                Rectangle {
                    id: tile
                    objectName: "railSocket" + modelData.key
                    x: rail.tight ? 9 : 13; anchors.verticalCenter: parent.verticalCenter
                    width: rail.tight ? 32 : 36; height: width; radius: rail.tight ? 8 : 9
                    color: "#0a1f36"
                    border.width: 1; border.color: Theme.alpha("#299ddf", card.active || card.hovered ? 0.7 : 0.42)
                    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
                    // The recess: a darker upper half, a faint light along the lower edge.
                    Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: parent.radius - 1
                        gradient: Gradient { GradientStop { position: 0; color: "#3a000a14" } GradientStop { position: 1; color: "transparent" } } }
                    Rectangle { x: 7; y: parent.height - 2; width: parent.width - 14; height: 1; color: Theme.alpha("#49bdfa", 0.3) }
                    SummaryIcon { anchors.centerIn: parent; icon: modelData.icon; tint: "#b8e7ff"; scale: (rail.tight ? 20 : 22) / 18 }
                }

                // Darker protection beneath the labels; the space stays visible
                // around them.
                Rectangle {
                    x: tile.x - 5; width: parent.width - x - 8; radius: 9
                    anchors.verticalCenter: parent.verticalCenter; height: Math.max(tile.height, labels.height) + 16
                    color: Theme.glassSecondaryReading
                }
                Column {
                    id: labels
                    x: tile.x + tile.width + (rail.tight ? 8 : 13)
                    anchors.verticalCenter: parent.verticalCenter
                    // The right reserve is the chevron's; Memory has none.
                    width: parent.width - x - (modelData.status ? 12 : rail.tight ? 17 : 24)
                    spacing: rail.showNote ? 2 : 0
                    readonly property int tier: Theme.tier(card.Window.width)
                    // Title, then the count numeral with its unit on one baseline, then the detail.
                    Text { objectName: "railTitle" + modelData.key; width: parent.width; elide: Text.ElideRight; text: modelData.key; color: Theme.text
                           font: Theme.railTitle(labels.tier); lineHeightMode: Text.FixedHeight; lineHeight: Theme.railTitleLine(labels.tier) }
                    Row {
                        width: parent.width; spacing: count === "" ? 0 : 5
                        readonly property string count: modelData.status ? "" : modelData.line.split(" ")[0]
                        // A status card keeps the count's line box (1 px wide: a Row skips zero-width items) so its rhythm matches.
                        Text { id: countText; objectName: "railCount" + modelData.key; text: parent.count === "" ? " " : parent.count; width: parent.count === "" ? 1 : implicitWidth; color: "#37c4f0"
                               font: Theme.railCount(labels.tier); lineHeightMode: Text.FixedHeight; lineHeight: Theme.railCountLine(labels.tier) }
                        Text { objectName: "railLine" + modelData.key; anchors.baseline: countText.baseline; width: parent.width - countText.width - parent.spacing; elide: Text.ElideRight
                               text: parent.count === "" ? modelData.line : modelData.line.substring(parent.count.length + 1); color: "#37c4f0"; font: Theme.uiLabel }
                    }
                    Text { objectName: "railNote" + modelData.key; visible: rail.showNote; width: parent.width; elide: Text.ElideRight; color: Theme.textLabel
                           text: modelData.shortNote && noteMetrics.advanceWidth > width ? modelData.shortNote : modelData.note
                           font: Theme.metadata; lineHeightMode: Text.FixedHeight; lineHeight: 18
                           TextMetrics { id: noteMetrics; text: modelData.note; font: Theme.metadata } }
                }
                Shape {
                    visible: !card.active && card.navigable
                    x: parent.width - (rail.tight ? 14 : 19); y: labels.y + (rail.showNote ? 4 : 2)
                    width: 7; height: 12; scale: rail.tight ? 1 : 1.15
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        strokeColor: card.hovered ? "#91ceff" : "#36b1ff"
                        strokeWidth: 1.6; fillColor: "transparent"
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
                    cursorShape: card.navigable ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: if (card.navigable) rail.picked(modelData.key, card.y, card.height)
                }

                // Edit Mode: a restrained blue outline and the card's layout menu.
                Rectangle {
                    anchors.fill: parent; radius: parent.radius
                    color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.55)
                    opacity: card.editing ? 1 : 0; visible: opacity > 0
                    Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
                }
                IconButton {
                    id: menuButton
                    objectName: "railMenu" + modelData.key
                    visible: card.editing
                    // Narrow rails keep the title clear with a slimmer target.
                    anchors { right: parent.right; top: parent.top; rightMargin: rail.width < 180 ? 2 : 4; topMargin: rail.showNote ? 4 : Math.max(0, (parent.height - height) / 2) }
                    width: rail.width < 180 ? 26 : 36; height: rail.showNote ? 32 : 36
                    glyph: "•••"; tip: modelData.key + " layout options"
                    onClicked: rail.menuRequested(modelData.id, menuButton, index > 0, index < rail.shownModules.length - 1)
                }
            }
        }
    }
}
