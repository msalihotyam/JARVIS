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
// What each tile says (Salih, T1-T4), from existing bridge data only:
//   Tasks  - the running task: its running time and name; else a waiting
//            approval: the tile turns amber and its click goes to the
//            approval; else the last task: its result, name and time.
//   Focus  - what the model sees in the selected scope: the project with
//            its instruction, files and conversations; Genel has none.
//   Plan   - the next scheduled job: when, then its name and day.
//   Memory - the store's status, then this scope's records (a project) or
//            the last record written (Genel).
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
    signal approvalRequested()               // the Tasks tile while an approval waits (T1)

    // A slow clock so relative ages stay honest without per-second redraws.
    property real now: Date.now()
    Timer { interval: 30000; running: rail.visible; repeat: true; onTriggered: rail.now = Date.now() }

    function ago(ts) {
        if (!ts) return ""
        var s = Math.max(0, Math.round(rail.now / 1000 - ts))
        return s < 60 ? qsTr("%1s ago").arg(s) : s < 3600 ? qsTr("%1m ago").arg(Math.floor(s / 60))
             : s < 86400 ? qsTr("%1h ago").arg(Math.floor(s / 3600)) : qsTr("%1d ago").arg(Math.floor(s / 86400))
    }
    // K3: dates through the one formatter - a time today, else the day ("yesterday", "3 Oct").
    function dueShort(iso) { return i18n.when(iso, rail.now, "short") }
    // Every Core task status as a word; an unknown code is named as such and kept exact.
    function statusWord(s) {
        return ({ pending: qsTr("Pending"), running: qsTr("Running"), verifying: qsTr("Verifying"), awaiting_approval: qsTr("Awaiting you"),
                  awaiting_clarification: qsTr("Needs a detail"), awaiting_cost_override: qsTr("Cost check"),
                  completed: qsTr("Completed"), failed: qsTr("Failed"), rejected: qsTr("Denied"),
                  cancel_requested: qsTr("Cancelling"), cancelling: qsTr("Cancelling"), cancelled: qsTr("Cancelled") })[s]
               || (s ? qsTr("Unrecognized (%1)").arg(s) : "")
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

    // -- T1: the running task, a waiting approval, else the last task ------
    readonly property bool running: bridge.taskLive && !bridge.approvalPending
    // A 1 s clock only while a task runs: its running time.
    property real tick: Date.now()
    Timer { interval: 1000; running: rail.visible && rail.running && bridge.workStartedAt > 0; repeat: true; onTriggered: rail.tick = Date.now() }
    function clockOf(ms) {
        var s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60
        return (h > 0 ? h + ":" + (m < 10 ? "0" : "") : "") + m + ":" + (r < 10 ? "0" : "") + r
    }
    // This interface session's finished task (its time: its result's entry),
    // else the newest durable session's latest task as the store lists it.
    readonly property var lastTask: {
        var t = bridge.task || ({})
        if (t.task_id && ["completed", "failed", "rejected", "cancelled"].indexOf(t.status) >= 0) {
            var at = ""
            for (var i = bridge.feed.length - 1; i >= 0; i--)
                if (bridge.feed[i].task_id === t.task_id && bridge.feed[i].ts) { at = new Date(bridge.feed[i].ts * 1000).toISOString(); break }
            return { name: t.title || t.capability || qsTr("Current request"), status: t.status, at: at }
        }
        var s = bridge.sessions.filter(function(r) { return !!r.latest_status })[0]
        return s ? { name: s.latest_title || s.title || qsTr("Current request"), status: s.latest_status, at: s.latest_updated_at || "" } : null
    }
    readonly property var tasksTile: {
        var tile = { id: "tasks", key: "Tasks", title: qsTr("Tasks"), icon: "tasks", tone: "#4fd8ff" }
        if (rail.running) {
            var name = bridge.task.title || bridge.task.capability || qsTr("Current request")
            var time = bridge.workStartedAt > 0 ? rail.clockOf(rail.tick - bridge.workStartedAt) : ""
            return Object.assign(tile, { value: time || rail.statusWord(bridge.task.status), word: !time, dot: Theme.accent,
                                         lines: [name], tip: time ? qsTr("%1 · running for %2").arg(name).arg(time) : name })
        }
        if (bridge.approvalPending) {
            var waiting = qsTr("1 approval waiting")
            return Object.assign(tile, { tone: Theme.warning, alert: true, action: "approval", value: "1",
                                         lines: [waiting.substring(2)], tip: qsTr("Open the waiting approval") })
        }
        var last = rail.lastTask
        if (last) {
            var when = rail.dueShort(last.at), result = rail.statusWord(last.status)
            return Object.assign(tile, { value: result, word: true,
                                         dot: last.status === "completed" ? Theme.success : last.status === "failed" ? Theme.danger : Theme.textMuted,
                                         lines: when ? [last.name + " · " + when, last.name] : [last.name],
                                         tip: result + " · " + last.name + (when ? " · " + when : "") })
        }
        return Object.assign(tile, { value: "—", lines: [qsTr("No task yet"), qsTr("No task")], tip: qsTr("No task yet") })
    }

    // -- T2: what the model sees in the selected scope ----------------------
    readonly property var scope: bridge.conversationProject || ({})
    readonly property bool global: !!rail.scope.global || !rail.scope.id
    readonly property var scopeRecord: rail.global ? null : bridge.projectRecords.filter(function(p) { return p.id === rail.scope.id })[0] || null
    readonly property var focusTile: {
        var tile = { id: "focus", key: "Focus", title: qsTr("Focus"), icon: "focus", tone: Theme.warning, word: true, value: rail.scope.name || "" }
        if (rail.global)
            return Object.assign(tile, { lines: [qsTr("no project context"), qsTr("no context")],
                                         tip: (rail.scope.name || "") + " · " + qsTr("no project context") })
        var files = rail.scope.file_count || 0, talks = rail.scopeRecord ? rail.scopeRecord.conversation_count : 0
        var a = rail.scope.instruction ? qsTr("instruction ✓") : qsTr("no instruction")
        var b = files === 1 ? qsTr("1 file") : qsTr("%1 files").arg(files)
        var c = talks === 1 ? qsTr("1 conversation") : qsTr("%1 conversations").arg(talks)
        return Object.assign(tile, { lines: [a + " · " + b + " · " + c, b + " · " + c, c], tip: rail.scope.name + " · " + a + " · " + b + " · " + c })
    }

    // -- T3: the next scheduled job ----------------------------------------
    function dayOf(iso) { return i18n.when(iso, rail.now, "day") }
    readonly property var planTile: {
        var tile = { id: "plan", key: "Plan", title: qsTr("Plan"), icon: "plan", tone: "#9b7bff" }
        var job = rail.nextJob
        if (!job)
            return Object.assign(tile, { value: "—", lines: [qsTr("No scheduled job"), qsTr("No job")], tip: qsTr("No scheduled job") })
        var name = job.label || job.id, day = rail.dayOf(job.next_due_at_utc)
        var time = new Date(job.next_due_at_utc).toLocaleTimeString(Qt.locale(i18n.qtLocale), "HH:mm")
        return Object.assign(tile, { value: time, lines: [name + " · " + day, name], tip: name + " · " + day + " " + time })
    }

    // -- T4: the memory store and this scope's records ---------------------
    readonly property var memoryTile: {
        var word = ({ "Available": qsTr("Available"), "Unavailable": qsTr("Unavailable"),
                      "Status unavailable": qsTr("Status unavailable") })[bridge.memoryStatus] || bridge.memoryStatus
        var count = rail.scope.memory_count || 0, at = rail.scope.memory_at || ""
        var lines = rail.global ? (at ? [qsTr("last record %1").arg(rail.dueShort(at)), qsTr("last %1").arg(rail.dueShort(at))]
                                      : [qsTr("no records yet"), qsTr("no records")])
                                : count ? [count === 1 ? qsTr("1 record") : qsTr("%1 records").arg(count)] : [qsTr("no records yet"), qsTr("no records")]
        return { id: "memory", key: "Memory", title: qsTr("Memory"), icon: "memory", tone: Theme.success, quiet: true, word: true,
                 dot: bridge.memoryStatus === "Available" ? Theme.success : Theme.warning, value: word, lines: lines, tip: word + " · " + lines[0] }
    }

    // A tile: its title, then `value` - a big number, or with `word` a word
    // after its `dot` - then `lines`, its one short line, longest variant
    // first; `tip` is the whole sentence (its tip and accessible name).
    // `quiet`: read, not pressed; `alert` + `action`: the amber approval tile.
    // `key` is the stable id (object names, the peek); `title` is what is shown.
    readonly property var modules: [rail.tasksTile, rail.focusTile, rail.planTile, rail.memoryTile]
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
                if (!card.navigable) return
                if (modelData.action === "approval") rail.approvalRequested()
                else rail.picked(modelData.key, card.y, card.height)
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
                id: titleText
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
            // G3: a tip only for text that is elided, and never while its peek is open. It sits beside
            // the sidebar, level with its tile, so it never covers a neighbouring tile's title.
            HoverTip {
                objectName: "railTip" + modelData.key; text: modelData.tip
                active: !card.editing && !card.active && (titleText.truncated || count.truncated || line.truncated)
                offsetX: rail.width - card.x + (rail.parent && rail.parent.pad !== undefined ? rail.parent.pad : 10) + 6
                offsetY: 0
            }
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
                icon: "more"; tip: qsTr("%1 layout options").arg(modelData.title)
                onClicked: rail.menuRequested(modelData.id, menuButton, index > 0, index < rail.shownModules.length - 1)
            }
        }
    }
}
