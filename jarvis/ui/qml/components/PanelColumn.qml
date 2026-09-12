import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The right-side information column: an ordered stack of real panels.
// Heights come from the persisted shares, reflow is animated, and Edit Mode
// adds handles (hide, pin, move, resize) plus the hidden-panel tray and
// Reset Layout. All geometry is derived, so nothing can leave the screen.
Item {
    id: column
    property bool commandMode: false
    property var panels: bridge.panels
    property var shown: panels.filter(function(p) { return p.shown })
    property real totalShare: shown.reduce(function(sum, p) { return sum + p.share }, 0)
    property real editChrome: bridge.editMode ? 54 : 0
    property real usable: height - editChrome - Math.max(0, shown.length - 1) * Theme.gap
    readonly property int minPanelHeight: 120
    property real nowSec: Date.now() / 1000
    Timer { interval: 30000; running: column.visible; repeat: true; onTriggered: column.nowSec = Date.now() / 1000 }
    function ago(ts, fallback) {
        if (!ts) return fallback
        var s = Math.max(0, column.nowSec - ts)
        return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : fallback
    }

    // Every shown panel gets its minimum first; the remainder is split by
    // share. The sum therefore never exceeds the column, whatever the shares.
    function heightFor(index) {
        var n = shown.length
        var minima = shown.map(function(p) { return column.commandMode ? ({workflow:230,system:195,activity:180,automations:125})[p.id] : column.minPanelHeight })
        var free = usable - minima.reduce(function(sum, value) { return sum + value }, 0)
        if (free < 0) return Math.max(0, usable / Math.max(n, 1))
        return minima[index] + free * (shown[index].share / Math.max(totalShare, 0.0001))
    }
    function yFor(index) {
        var y = editChrome
        for (var i = 0; i < index; i++) y += heightFor(i) + Theme.gap
        return y
    }
    // The rendered geometry of every shown panel, for inspection.
    function slotBounds() {
        var out = []
        for (var i = 0; i < shown.length; i++) out.push({ id: shown[i].id, y: yFor(i), height: heightFor(i), width: column.width })
        return out
    }
    // Stages of the current task that are complete (the reference's "4/6").
    function stagesDone() {
        var t = bridge.task, cs = bridge.coreState, has = !!t.task_id
        if (!has) return 0
        if (t.status === "rejected" || t.status === "cancelled") return 1
        var n = 1
        if (cs === "running" || cs === "completed" || cs === "error" || cs === "unknown" || t.run_status) n++
        if (cs === "completed" || cs === "error" || cs === "unknown") n += 2
        return Math.min(n, 4)
    }

    // -- edit chrome --------------------------------------------------------
    Item {
        id: chrome
        width: parent.width; height: 44
        opacity: bridge.editMode ? 1 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: Theme.editMs; easing.type: Easing.OutCubic } }
        Row {
            spacing: 8
            anchors.verticalCenter: parent.verticalCenter
            Pill { text: "Edit layout"; tint: Theme.accent; anchors.verticalCenter: parent.verticalCenter }
            Repeater {
                model: bridge.hiddenPanels
                Rectangle {
                    height: 26; width: chipText.width + 22; radius: 13
                    color: chipArea.containsMouse ? Theme.alpha(Theme.accent, 0.12) : Theme.surfaceRaised; border.width: 1; border.color: chipArea.containsMouse ? Theme.alpha(Theme.accent, 0.5) : Theme.border
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                    Text { id: chipText; anchors.centerIn: parent; text: "+ " + modelData; color: Theme.textLabel; font { family: Theme.font; pixelSize: 11 } }
                    MouseArea { id: chipArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setPanelVisible(modelData, true) }
                }
            }
        }
        ActionButton { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; label: "Reset layout"; compact: true; onClicked: bridge.resetLayout() }
    }

    // -- the stack ----------------------------------------------------------
    Repeater {
        model: shown
        delegate: Item {
            id: slot
            required property var modelData
            required property int index
            width: column.width
            y: column.yFor(index)
            height: column.heightFor(index)
            Behavior on y { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
            Behavior on height { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
            opacity: 0
            transform: Translate { id: slide; x: 10 }
            Component.onCompleted: appear.start()
            ParallelAnimation {
                id: appear
                NumberAnimation { target: slot; property: "opacity"; to: 1; duration: Theme.panelIn; easing.type: Easing.OutCubic }
                NumberAnimation { target: slide; property: "x"; to: 0; duration: Theme.panelIn; easing.type: Easing.OutCubic }
            }

            GlassPanel {
                id: glass
                anchors.fill: parent
                brackets: true
                padding: column.width < 400 ? 12 : column.commandMode ? 16 : 20
                title: (column.commandMode ? {system:"Model & execution",workflow:"Active task",activity:"Recent actions",automations:"Memory context"} : { system: "System status", workflow: "Active workflow", activity: "Live intelligence feed", automations: "Automations" })[modelData.id]
                badge: modelData.id === "system" ? (bridge.configRefused ? "config refused" : (bridge.executionEnabled ? "enabled" : "disabled"))
                     : modelData.id === "activity" ? "live"
                     : modelData.id === "automations" ? (column.commandMode ? bridge.projects.length + " projects" : bridge.jobs.length + " job" + (bridge.jobs.length === 1 ? "" : "s"))
                     : ""
                badgeRight: modelData.id !== "system"
                badgeColor: modelData.id === "system" ? (bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success)
                          : modelData.id === "activity" ? Theme.success : Theme.accent
                border.color: bridge.editMode ? Theme.alpha(Theme.accent, 0.7) : Theme.border
                headerRight: [
                    Item {
                        height: 24; width: bridge.editMode ? handles.width : (modelData.id === "workflow" ? workflowHead.width : pinned.width)
                        Row {
                            id: handles
                            spacing: 2
                            visible: bridge.editMode
                            IconButton { glyph: "▲"; tip: "Move up"; width: 24; height: 24; onClicked: bridge.movePanel(modelData.id, -1) }
                            IconButton { glyph: "▼"; tip: "Move down"; width: 24; height: 24; onClicked: bridge.movePanel(modelData.id, 1) }
                            IconButton { glyph: "⊙"; tip: modelData.pinned ? "Unpin" : "Pin"; active: modelData.pinned; width: 24; height: 24; onClicked: bridge.setPanelPinned(modelData.id, !modelData.pinned) }
                            IconButton { glyph: "✕"; tip: "Hide"; width: 24; height: 24; onClicked: bridge.setPanelVisible(modelData.id, false) }
                        }
                        Row {
                            id: workflowHead
                            spacing: 8
                            visible: !bridge.editMode && modelData.id === "workflow"
                            ActionButton { label: "Cancel"; danger: true; compact: true; visible: bridge.taskLive; enabled: bridge.taskLive; onClicked: bridge.cancelTask() }
                            ActionButton { label: "New session"; compact: true; visible: !!bridge.sessionId && !bridge.taskLive; enabled: !bridge.busy; onClicked: bridge.newSession() }
                            Text { text: column.stagesDone() + "/4"; color: Theme.accent; anchors.verticalCenter: parent.verticalCenter; font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold; letterSpacing: 0.5 } }
                        }
                        Text { id: pinned; visible: !bridge.editMode && modelData.pinned && modelData.id !== "workflow"; text: "PINNED"; color: Theme.textDim; font { family: Theme.font; pixelSize: 9; letterSpacing: 1.5 } }
                    }
                ]
                Flickable {
                    id: bodyViewport
                    anchors.fill: parent
                    contentHeight: Math.max(height, modelData.id === "workflow" ? 166 : modelData.id === "system" ? (column.commandMode ? 138 : column.width < 400 ? 62 : 108) : 80)
                    clip: true; boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    Loader {
                        width: bodyViewport.width; height: bodyViewport.contentHeight
                        sourceComponent: ({ system: column.commandMode ? modelPanel : systemPanel, workflow: workflowPanel, activity: activityPanel, automations: column.commandMode ? memoryPanel : automationsPanel })[modelData.id]
                    }
                }
            }
            // Resize grip: direct while dragging, snapped share on release.
            Rectangle {
                visible: bridge.editMode
                anchors.horizontalCenter: parent.horizontalCenter; anchors.bottom: parent.bottom; anchors.bottomMargin: 4
                width: 44; height: 5; radius: 2.5; color: Theme.accent; opacity: grip.pressed ? 1 : 0.6
                MouseArea {
                    id: grip
                    anchors.fill: parent; anchors.margins: -6
                    cursorShape: Qt.SizeVerCursor
                    property real startY: 0; property real startShare: 0
                    onPressed: function(mouse) { startY = mouse.y; startShare = modelData.share }
                    onPositionChanged: function(mouse) {
                        if (!pressed) return
                        var dy = mouse.y - startY
                        var share = startShare + dy / Math.max(column.usable, 1) * column.totalShare
                        bridge.setPanelShare(modelData.id, share)
                    }
                }
            }
        }
    }

    // -- panel bodies -------------------------------------------------------
    // A quiet stat line: dot, key, value right-aligned (the reference's Uptime / Memory / Network list).
    component StatRow: Item {
        property string k: ""; property string v: ""; property color tint: Theme.success; property color vColor: Theme.text
        width: parent.width; height: 30
        Rectangle { width: 9; height: 9; radius: 4.5; color: tint; anchors.verticalCenter: parent.verticalCenter
            Rectangle { anchors.centerIn: parent; width: 17; height: 17; radius: 8.5; color: tint; opacity: 0.22 } }
        Text { x: 24; anchors.verticalCenter: parent.verticalCenter; text: k; color: Theme.textLabel; font { family: Theme.font; pixelSize: 15 } }
        Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 96; horizontalAlignment: Text.AlignRight
            text: v; color: vColor; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold } }
    }
    // A gauge: the reference ring with the figure inside. Lit only when a real value exists.
    component Gauge: Item {
        property string label: ""; property string value: "—"; property real ratio: -1; property color tint: Theme.accent
        width: 74; height: 74
        Rectangle { anchors.fill: parent; radius: width / 2; color: Theme.alpha(Theme.bg0, 0.55); border.width: width < 60 ? 4 : 7; border.color: ratio < 0 ? Theme.alpha(Theme.textDim, 0.38) : Theme.alpha(tint, 0.9) }
        Rectangle { anchors.fill: parent; anchors.margins: -3; radius: width / 2; color: "transparent"; border.width: 1; border.color: Theme.alpha(tint, ratio < 0 ? 0.12 : 0.35) }
        Column { anchors.centerIn: parent; spacing: -2
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: value; color: ratio < 0 ? Theme.textMuted : Theme.text; font { family: Theme.font; pixelSize: column.width < 400 ? 13 : 20; weight: Font.DemiBold } }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: label; color: Theme.textLabel; font { family: Theme.font; pixelSize: column.width < 400 ? 9 : 11; letterSpacing: 0.6 } }
        }
    }
    // One row of the reference's workflow well: icon box, label, dot, status, handle.
    component WRow: Item {
        property string glyph: "◇"; property color tint: Theme.accent; property string k: ""; property string v: ""; property color vColor: Theme.textLabel; property bool empty: false; property bool last: false
        width: parent.width; height: 41
        Rectangle { x: 12; anchors.verticalCenter: parent.verticalCenter; width: 30; height: 30; radius: 8
            color: Theme.alpha(Theme.accentSoft, empty ? 0.16 : 0.32); border.width: 1; border.color: Theme.alpha(Theme.accentSoft, empty ? 0.3 : 0.55)
            Text { anchors.centerIn: parent; text: glyph; color: empty ? Theme.textMuted : Theme.accent; font { family: Theme.font; pixelSize: 14 } } }
        Text { x: 52; width: parent.width * 0.48 - 60; elide: Text.ElideRight; anchors.verticalCenter: parent.verticalCenter; text: k; color: Theme.text; font { family: Theme.font; pixelSize: column.commandMode ? 12 : 14 } }
        Rectangle { x: parent.width * 0.48 - 5; width: 6; height: 6; radius: 3; anchors.verticalCenter: parent.verticalCenter; color: empty ? Theme.textDim : tint; opacity: empty ? 0.6 : 1
            Rectangle { visible: !empty; anchors.centerIn: parent; width: 16; height: 16; radius: 8; color: tint; opacity: 0.25 } }
        Text { anchors.left: parent.left; anchors.leftMargin: parent.width * 0.48 + 9; anchors.right: parent.right; anchors.rightMargin: 25; anchors.verticalCenter: parent.verticalCenter
            text: v; color: empty ? Theme.textMuted : vColor; elide: Text.ElideRight; font { family: Theme.font; pixelSize: column.commandMode ? 12 : 14 } }
        Text { anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter; text: "⁘"; color: Theme.textMuted; font.pixelSize: 13 }
        Rectangle { visible: !last; anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: 10; rightMargin: 10 } height: 1; color: Theme.border; opacity: 0.55 }
    }
    // The inset well that holds rows (the reference's rounded inner frame).
    component Well: Rectangle {
        default property alias rows: inner.data
        property bool fill: false
        width: parent.width; height: fill ? parent.height : inner.height + 2
        radius: 10; color: Theme.alpha(Theme.bg0, 0.28); border.width: 1; border.color: Theme.alpha(Theme.border, 0.8)
        Column { id: inner; x: 1; y: 1; width: parent.width - 2 }
    }

    Component {
        id: systemPanel
        Item {
            Row {
                id: gauges
                anchors.left: parent.left; anchors.top: parent.top; anchors.leftMargin: 4; anchors.topMargin: column.width < 400 ? 3 : 6
                spacing: column.width < 400 ? Math.max(10, (parent.width - 150) / 2) : 23
                // Not measured by this build: unlit slots, said plainly below.
                Gauge { label: "CPU"; width: column.width < 400 ? 44 : 74; height: width }
                Gauge { label: "RAM"; width: column.width < 400 ? 44 : 74; height: width }
                Gauge { label: "Disk"; width: column.width < 400 ? 44 : 74; height: width }
            }
            Text { anchors.left: parent.left; anchors.leftMargin: 4; anchors.top: gauges.bottom; anchors.topMargin: column.width < 400 ? 3 : 7; text: column.width < 400 ? "Telemetry not measured" : "CPU · RAM · Disk are not measured by this build"; color: Theme.textDim; font { family: Theme.font; pixelSize: 10; letterSpacing: 0.4 } }
            Column {
                anchors { left: gauges.right; right: parent.right; top: parent.top; leftMargin: 26; topMargin: 4 }
                visible: parent.width >= 430
                spacing: 0
                StatRow { k: "Uptime"; v: (Math.floor(bridge.uptimeSeconds / 3600) > 0 ? Math.floor(bridge.uptimeSeconds / 3600) + "h " : "") + (Math.floor(bridge.uptimeSeconds / 60) % 60) + "m" }
                StatRow { k: "Execution"; v: bridge.configRefused ? "Refused" : (bridge.executionEnabled ? "Enabled" : "Disabled"); tint: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success; vColor: tint }
                StatRow { k: "Model"; v: bridge.config.claude_model || "CLI default" }
            }
        }
    }
    Component {
        id: workflowPanel
        Well {
            id: workflowWell
            fill: true
            readonly property bool has: !!bridge.task.task_id
            WRow { height: (workflowWell.height - 2) / 5; glyph: "◇"; tint: Theme.stateColor(bridge.coreState); k: "Status"; empty: !workflowWell.has
                v: workflowWell.has ? (bridge.task.status || "—") + (bridge.task.stage ? " / " + bridge.task.stage : "") : "No active task" }
            WRow { height: (workflowWell.height - 2) / 5; glyph: "⌘"; tint: Theme.success; k: "Connector"; empty: !workflowWell.has; v: workflowWell.has ? (bridge.task.connector || "—") : "Waiting for your request…" }
            WRow { height: (workflowWell.height - 2) / 5; glyph: "✦"; tint: Theme.accent; k: "Capability"; empty: !workflowWell.has; v: workflowWell.has ? (bridge.task.capability || "—") : "Chosen by Core" }
            WRow { height: (workflowWell.height - 2) / 5; glyph: "➶"; tint: Theme.accent; k: "Destination"; empty: !workflowWell.has; v: workflowWell.has ? (bridge.task.destination || "—") : "Granted destinations only" }
            WRow { height: (workflowWell.height - 2) / 5; glyph: "◷"; k: "Outcome"; empty: !workflowWell.has; last: true
                tint: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.success)
                vColor: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.textLabel)
                v: workflowWell.has ? (bridge.task.error_code || (bridge.task.run_status || "pending")) : "None yet" }
        }
    }
    Component {
        id: activityPanel
        Well {
            fill: true
            ListView {
                width: parent.width; height: parent.parent.height - 2
                clip: true
                model: bridge.activity
                spacing: 0
                delegate: Item {
                    width: ListView.view.width; height: column.commandMode ? 47 : 60
                    readonly property color tint: modelData.kind === "approval" ? "#c58a2a" : modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "scheduled" ? "#22a877" : "#2f7fe0"
                    readonly property string glyph: modelData.kind === "approval" ? "⚑" : modelData.kind === "unknown" ? "?" : modelData.kind === "error" ? "!" : modelData.kind === "scheduled" ? "⟳" : "◈"
                    IconBox { x: 12; anchors.verticalCenter: parent.verticalCenter; width: 38; height: 38; glyph: parent.glyph; tint: parent.tint; solid: true; round: true; glyphSize: 16 }
                    Column { anchors.left: parent.left; anchors.leftMargin: 62; anchors.right: timeText.left; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                        Text { text: modelData.text; color: Theme.text; width: parent.width; elide: Text.ElideRight; font { family: Theme.font; pixelSize: column.commandMode ? 12 : 14 } }
                        Text { text: modelData.kind === "approval" ? "Authorization" : modelData.kind === "unknown" ? "Outcome unknown" : modelData.kind === "error" ? "Error" : modelData.kind === "scheduled" ? "Scheduled run" : "Interface"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
                    }
                    Text { id: timeText; anchors.right: parent.right; anchors.rightMargin: 14; anchors.verticalCenter: parent.verticalCenter; text: column.ago(modelData.ts, modelData.time); color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
                    Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: 10; rightMargin: 10 } height: 1; color: Theme.border; opacity: 0.55 }
                }
                footer: Item {
                    width: ListView.view.width; height: bridge.activity.length < 4 ? 60 : 0
                    visible: bridge.activity.length < 4
                    Row { x: 12; anchors.verticalCenter: parent.verticalCenter; spacing: 12
                        Rectangle { width: 38; height: 38; radius: 19; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.border, 0.9)
                            Rectangle { anchors.centerIn: parent; width: 6; height: 6; radius: 3; color: Theme.textDim } }
                        Column { anchors.verticalCenter: parent.verticalCenter; spacing: 2
                            Text { text: "Nothing else yet"; color: Theme.textLabel; font { family: Theme.font; pixelSize: 15 } }
                            Text { text: "Approvals, outcomes and scheduled runs appear here"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
                        }
                    }
                }
            }
        }
    }
    Component {
        id: modelPanel
        Item {
            Row { id: identity; width: parent.width; spacing: 12
                IconBox { width: 38; height: 38; tint: "#e9b777"; glyph: "AI"; solid: true }
                Column { spacing: 3
                    Text { text: bridge.config.claude_model || "Claude · CLI default"; color: Theme.text; font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold } }
                    Text { text: "Claude Code · subscription route"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                }
            }
            Column { anchors { left: parent.left; right: parent.right; top: identity.bottom; topMargin: 12 } spacing: 8
                Repeater { model: [{k:"Execution",v:bridge.executionEnabled ? "Enabled" : "Disabled"},{k:"Effort",v:bridge.config.claude_effort || "CLI default"},{k:"Context window",v:"Not reported"}]
                    Item { width: parent.width; height: 20
                        Text { text: modelData.k; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                        Text { anchors.right: parent.right; text: modelData.v; color: modelData.k === "Execution" ? (bridge.executionEnabled ? Theme.success : Theme.danger) : Theme.textLabel; font { family: Theme.font; pixelSize: 12 } }
                        Rectangle { width: parent.width; height: 1; y: 20; color: Theme.border }
                    }
                }
            }
        }
    }
    Component {
        id: memoryPanel
        Item {
            Text { width: parent.width; wrapMode: Text.Wrap; visible: bridge.projects.length === 0; text: "No project memory loaded. Only explicit, stored reference data appears here."; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            Flow { width: parent.width; spacing: 8
                Repeater { model: bridge.projects.slice(0, 8)
                    Pill { text: modelData; tint: Theme.textMuted }
                }
            }
        }
    }
    Component {
        id: automationsPanel
        Well {
            WRow { visible: bridge.jobs.length === 0; glyph: "⟳"; k: "Jobs"; empty: true; last: true; v: "No jobs defined" }
            Repeater {
                model: bridge.jobs.slice(0, 5)
                WRow { glyph: "⟳"; tint: modelData.enabled ? Theme.success : Theme.textDim; last: index === Math.min(bridge.jobs.length, 5) - 1
                    k: modelData.readable ? (modelData.label.length > 12 ? modelData.label.substring(0, 12) + "…" : modelData.label) : "(unreadable)"
                    v: modelData.next_due_at_utc ? "next " + modelData.next_due_at_utc.substring(5, 16) + "Z" : "—" }
            }
        }
    }
}
