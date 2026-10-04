import QtQuick
import "../theme"

// The sidebar's summary tiles (Gate 6F, L2 f): Tasks, Focus, Plan, Memory as
// a 2x2 grid at the foot of the sidebar. A hidden tile reflows the grid to
// 3 / 2 / 1 (Q3); Updates has no tile (Q4: the right column's live feed
// carries it). Each tile is one glance at real state: an icon well, a big
// number or status and ONE short line that never clips - it takes the
// longest of its variants that fits, and elides (with its tip) only as the
// last resort. A click opens the quick-look peek beside the sidebar (Q5);
// the full page stays one step further. Memory is read, not pressed.
//
// Every figure comes from a bridge listing; nothing is invented. Order and
// visibility are the persisted layout (bridge.railCards); in Edit Mode the
// tiles stop navigating and each offers its layout menu.
//
// The shaping helpers live here so the peek reads the same derived state.
Item {
    id: rail
    objectName: "summaryRail"
    property string activeKey: ""
    property bool big: true                  // the 1920-class tile (84 px), else the compact one (62 px)
    signal picked(string key, real cardY, real cardHeight)
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)

    // A slow clock so relative ages stay honest without per-second redraws.
    property real now: Date.now()
    Timer { interval: 30000; running: rail.visible; repeat: true; onTriggered: rail.now = Date.now() }

    function ago(ts) {
        if (!ts) return ""
        var s = Math.max(0, Math.round(rail.now / 1000 - ts))
        return s < 60 ? qsTr("%1s ago").arg(s) : s < 3600 ? qsTr("%1m ago").arg(Math.floor(s / 60))
             : s < 86400 ? qsTr("%1h ago").arg(Math.floor(s / 3600)) : qsTr("%1d ago").arg(Math.floor(s / 86400))
    }
    function dueShort(iso) {
        if (!iso) return ""
        var d = new Date(iso)
        if (isNaN(d.getTime())) return ""
        var loc = Qt.locale(i18n.qtLocale)
        return d.toDateString() === new Date(rail.now).toDateString() ? d.toLocaleTimeString(loc, "HH:mm") : d.toLocaleDateString(loc, "ddd d MMM")
    }
    function statusWord(s) {
        return ({ running: qsTr("Running"), awaiting_approval: qsTr("Awaiting you"), awaiting_clarification: qsTr("Needs a detail"),
                  completed: qsTr("Completed"), failed: qsTr("Failed"), rejected: qsTr("Denied"), cancelled: qsTr("Cancelled") })[s] || (s || "")
    }

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
    readonly property int taskTotal: bridge.sessions.reduce(function(n, s) { return n + (s.task_count || 0) }, 0)
    readonly property string sessionsWord: bridge.sessions.length === 1 ? qsTr("1 session") : qsTr("%1 sessions").arg(bridge.sessions.length)
    readonly property string tasksWord: rail.taskTotal === 1 ? qsTr("1 task") : qsTr("%1 tasks").arg(rail.taskTotal)

    // A tile: its title, then `value` - a big number, or with `word` a word
    // after its `dot` - then `lines`, its one short line, longest variant
    // first; `tip` is the whole sentence (its tip and accessible name).
    // `quiet`: read, not pressed. `key` is the stable id (object names, the
    // peek); `title` is what is shown.
    readonly property var modules: {
        var tasks = bridge.taskLive ? 1 : 0
        var memoryWord = ({ "Available": qsTr("Available"), "Unavailable": qsTr("Unavailable"),
                            "Status unavailable": qsTr("Status unavailable") })[bridge.memoryStatus] || bridge.memoryStatus
        var next = rail.nextJob ? qsTr("Next %1").arg(rail.dueShort(rail.nextJob.next_due_at_utc)) : ""
        return [
            { id: "tasks", key: "Tasks", title: qsTr("Tasks"), icon: "tasks", tone: "#4fd8ff", value: String(tasks),
              lines: [qsTr("%1 active").arg(tasks).substring(String(tasks).length + 1) + " · " + rail.sessionsWord,
                      qsTr("%1 active").arg(tasks).substring(String(tasks).length + 1)],
              tip: qsTr("%1 active").arg(tasks) + " · " + rail.sessionsWord + " · " + rail.tasksWord },
            { id: "focus", key: "Focus", title: qsTr("Focus"), icon: "focus", tone: Theme.warning, value: String(bridge.projects.length),
              lines: [(bridge.projects.length === 1 ? qsTr("1 project") : qsTr("%1 projects").arg(bridge.projects.length))
                          .substring(String(bridge.projects.length).length + 1) + " · " + (bridge.projects.length ? qsTr("In memory") : qsTr("No context yet")),
                      (bridge.projects.length === 1 ? qsTr("1 project") : qsTr("%1 projects").arg(bridge.projects.length))
                          .substring(String(bridge.projects.length).length + 1)],
              tip: bridge.projects.length ? qsTr("Active in memory") : qsTr("No focus context yet") },
            { id: "plan", key: "Plan", title: qsTr("Plan"), icon: "plan", tone: "#9b7bff", value: String(rail.enabledJobs.length),
              lines: [qsTr("%1 scheduled").arg(rail.enabledJobs.length).substring(String(rail.enabledJobs.length).length + 1)
                          + (next ? " · " + next : ""),
                      qsTr("%1 scheduled").arg(rail.enabledJobs.length).substring(String(rail.enabledJobs.length).length + 1)],
              tip: next || (rail.liveJobs.length ? qsTr("Nothing enabled") : qsTr("Nothing scheduled")) },
            { id: "memory", key: "Memory", title: qsTr("Memory"), icon: "memory", tone: Theme.success, quiet: true, word: true,
              dot: bridge.memoryStatus === "Available" ? Theme.success : Theme.warning, value: memoryWord,
              lines: [qsTr("Persistent across sessions"), qsTr("Persistent")], tip: qsTr("Persistent across sessions") }
        ]
    }
    readonly property var shownModules: {
        var byId = {}
        rail.modules.forEach(function(m) { byId[m.id] = m })
        return bridge.railCards.filter(function(c) { return c.visible && byId[c.id] !== undefined }).map(function(c) { return byId[c.id] })
    }
    readonly property int count: shownModules.length

    // The grid: two per row; an odd last tile takes the whole row (3 / 2 / 1).
    // A tile has three rows: the title, the value, the line.
    readonly property int gap: big ? 8 : 6
    readonly property real cardHeight: big ? 86 : 74
    readonly property int rows: Math.ceil(count / 2)
    readonly property real cell: (width - gap) / 2
    implicitHeight: rows > 0 ? rows * cardHeight + (rows - 1) * gap : 0
    height: implicitHeight
    readonly property color activeFill: Qt.tint(Theme.bg1, Theme.alpha(Theme.accentDeep, 0.34))

    Repeater {
        model: rail.shownModules
        Rectangle {
            id: card
            objectName: "rail" + modelData.key
            readonly property bool editing: bridge.editMode
            // Memory has no peek or page: outside editing it is read, not pressed.
            readonly property bool navigable: !editing && !modelData.quiet
            readonly property bool active: rail.activeKey === modelData.key
            readonly property bool hovered: hit.containsMouse
            readonly property bool alert: !!modelData.alert
            readonly property bool whole: index === rail.count - 1 && rail.count % 2 === 1
            x: whole ? 0 : (index % 2) * (rail.cell + rail.gap)
            y: Math.floor(index / 2) * (rail.cardHeight + rail.gap)
            width: whole ? rail.width : rail.cell
            height: rail.cardHeight
            radius: 10
            color: alert ? Theme.alpha(Theme.warning, hovered ? 0.20 : 0.13) : active ? rail.activeFill
                 : hovered && navigable ? Theme.glassControlHover : Theme.glassSecondaryShell
            border.width: 1
            border.color: alert ? Theme.alpha(Theme.warning, 0.75) : active ? Theme.alpha(Theme.accent, 0.7)
                        : hovered && navigable ? Theme.glassEdgeStrong : Theme.glassEdge
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
            activeFocusOnTab: card.navigable
            Accessible.role: card.navigable ? Accessible.Button : Accessible.StaticText
            Accessible.name: modelData.title + ": " + modelData.tip
            function activate() {
                if (card.navigable) rail.picked(modelData.key, card.y, card.height)
            }
            Keys.onReturnPressed: card.activate()
            Keys.onSpacePressed: card.activate()

            readonly property int inset: rail.big ? 12 : 10
            readonly property int pad: rail.big ? 9 : 7
            // Row 1: the icon well and the title.
            Rectangle {
                id: well
                objectName: "railSocket" + modelData.key
                x: card.inset; y: card.pad
                width: rail.big ? 20 : 18; height: width; radius: 5
                color: Theme.alpha(modelData.tone, card.alert ? 0.24 : 0.14)
                SummaryIcon { anchors.centerIn: parent; icon: modelData.icon; tint: "#d8f0ff"; scale: (well.width - 7) / 18 }
            }
            Text {
                objectName: "railTitle" + modelData.key
                x: well.x + well.width + 7; anchors.verticalCenter: well.verticalCenter
                width: card.width - x - card.inset
                elide: Text.ElideRight; textFormat: Text.PlainText
                text: modelData.title; color: Theme.textLabel; font: Theme.captionStrong; renderType: Theme.textRender
            }
            // Row 2: the big number, or a word after its dot. A word takes the
            // display face while it fits, else the caption's strong face, and
            // elides (its tip has it) only when even that is too wide.
            readonly property font wordFace: Qt.font({ family: Theme.display, pixelSize: rail.big ? 19 : 16, weight: Font.Medium })
            FontMetrics { id: wordMetrics; font: card.wordFace }
            readonly property real end: card.width - (menuButton.visible ? menuButton.width + 8 : card.inset)
            Rectangle {
                id: statusDot
                visible: !!modelData.dot && modelData.value !== ""
                x: card.inset; anchors.verticalCenter: count.verticalCenter
                width: 8; height: 8; radius: 4; color: modelData.dot || Theme.success
            }
            Text {
                id: count
                objectName: "railCount" + modelData.key
                x: statusDot.visible ? statusDot.x + statusDot.width + 7 : card.inset
                y: Math.round((well.y + well.height + line.y - height) / 2)
                width: Math.min(implicitWidth, Math.max(0, card.end - x)); elide: Text.ElideRight
                textFormat: Text.PlainText; text: modelData.value; color: card.alert ? Theme.warning : Theme.text
                font: !modelData.word ? Theme.figure(rail.big ? 26 : 20)
                      : wordMetrics.advanceWidth(modelData.value) <= card.end - x ? card.wordFace : Theme.captionStrong
                renderType: Theme.textRender
            }
            // Row 3: the one short line - the longest variant that fits; the
            // last one elides (its tip has it) only if even it does not.
            FontMetrics { id: lineMetrics; font: Theme.caption }
            Text {
                id: line
                objectName: "railLine" + modelData.key
                readonly property real room: card.width - 2 * card.inset
                readonly property string fitted: {
                    var v = modelData.lines
                    for (var i = 0; i < v.length; i++)
                        if (lineMetrics.advanceWidth(v[i]) <= room) return v[i]
                    return v[v.length - 1]
                }
                x: card.inset; y: card.height - card.pad - height
                width: Math.max(0, room)
                elide: Text.ElideRight; textFormat: Text.PlainText
                text: fitted; color: card.alert ? Theme.warning : Theme.textMuted; font: Theme.caption; renderType: Theme.textRender
            }
            MouseArea {
                id: hit
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: card.navigable ? Qt.PointingHandCursor : Qt.ArrowCursor
                onClicked: card.activate()
            }
            HoverTip { objectName: "railTip" + modelData.key; text: modelData.tip; active: !card.editing }
            Rectangle { objectName: "railFocusRing" + modelData.key; visible: card.activeFocus; anchors.fill: parent; anchors.margins: 1
                        radius: card.radius - 1; color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }

            // Edit Mode: a restrained blue outline and the tile's layout menu.
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
                // On the value's row, so the title and the line keep their room.
                anchors { right: parent.right; rightMargin: 4; verticalCenter: count.verticalCenter }
                width: 26; height: 26
                glyph: "•••"; tip: qsTr("%1 layout options").arg(modelData.title)
                onClicked: rail.menuRequested(modelData.id, menuButton, index > 0, index < rail.shownModules.length - 1)
            }
        }
    }
}
