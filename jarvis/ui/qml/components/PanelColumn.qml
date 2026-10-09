import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The right-side information column: an ordered stack of real panels.
// Heights come from the persisted shares, reflow is animated, and Edit Mode
// adds the workspace layout toolbar (state, Cards, Reset layout, Done), one
// layout menu per panel and the resize grips. The toolbar edits the whole
// workspace - this stack and the left rail - and its popovers live in Main.
// All geometry is derived, so nothing can leave the screen.
Item {
    id: column
    property bool commandMode: false
    property var panels: bridge.panels
    property var shown: panels.filter(function(p) { return p.shown })
    property real totalShare: shown.reduce(function(sum, p) { return sum + p.share }, 0)
    property real editChrome: bridge.editMode ? (bridge.layoutSaveState === "failed" ? 72 : 54) : 0
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)
    signal cardsRequested(Item anchor)
    signal resetRequested(Item anchor)
    property real usable: height - editChrome - Math.max(0, shown.length - 1) * gapPx
    readonly property int minPanelHeight: 120
    // One structural palette for the stack: deep navy surface, cool-blue
    // borders and default glyphs, blue-cyan local highlights. Green, red and
    // amber remain semantic only.
    readonly property color surface: Theme.glassSecondaryShell
    readonly property color structure: "#278dce"
    readonly property color highlight: "#49bdfa"
    // Gap between stacked panels: proportional to the stack, as in the reference.
    readonly property int gapPx: Theme.stackGap(height)
    property real nowSec: Date.now() / 1000
    // triggeredOnStart: a column shown again judges "live" by the clock of now, not of when it was hidden.
    Timer { interval: 30000; running: column.visible; repeat: true; triggeredOnStart: true; onTriggered: column.nowSec = Date.now() / 1000 }
    function ago(ts, fallback) {
        if (!ts) return fallback
        var s = Math.max(0, column.nowSec - ts)
        return s < 60 ? qsTr("just now") : s < 3600 ? qsTr("%1 min ago").arg(Math.floor(s / 60)) : s < 86400 ? qsTr("%1 h ago").arg(Math.floor(s / 3600))
             : i18n.when(ts, column.nowSec * 1000, "day")
    }
    // K3: the feed's "live" badge only while an approved execution runs or its
    // newest event is under five minutes old - never as decoration.
    readonly property int liveWindowSec: 300
    readonly property bool liveNow: (bridge.busy && bridge.executing)
                                    || (bridge.activity.length > 0 && column.nowSec - bridge.activity[0].ts < column.liveWindowSec)

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
        for (var i = 0; i < index; i++) y += heightFor(i) + gapPx
        return y
    }
    // The rendered geometry of every shown panel, for inspection.
    function slotBounds() {
        var out = []
        for (var i = 0; i < shown.length; i++) out.push({ id: shown[i].id, y: yFor(i), height: heightFor(i), width: column.width })
        return out
    }
    // Stages of the current task that are complete (the reference's "4/6").
    // A task (or run) status code as words; an unknown code is named as such and kept exact.
    function statusWord(s) {
        return ({ pending: qsTr("pending"), running: qsTr("running"), verifying: qsTr("verifying"), awaiting_approval: qsTr("awaiting approval"),
                  awaiting_clarification: qsTr("awaiting clarification"), awaiting_cost_override: qsTr("awaiting cost override"),
                  completed: qsTr("completed"), failed: qsTr("failed"), rejected: qsTr("rejected"),
                  cancel_requested: qsTr("cancellation requested"), cancelling: qsTr("cancellation requested"), cancelled: qsTr("cancelled") })[s]
               || (s ? qsTr("unrecognized (%1)").arg(s) : "")
    }
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
        objectName: "layoutToolbar"
        width: parent.width; height: 44
        opacity: bridge.editMode ? 1 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs; easing.type: Easing.OutCubic } }
        readonly property bool narrow: width < 300
        // Edit state: a blue dot, the label, and the truthful save state beneath.
        Rectangle {
            id: stateChip
            anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
            height: 36; width: stateCol.x + stateCol.width + 12; radius: Theme.radiusSmall
            color: Theme.alpha(Theme.accent, 0.08); border.width: 1; border.color: Theme.alpha(Theme.accent, 0.4)
            Rectangle { id: stateDot; x: 10; anchors.verticalCenter: parent.verticalCenter; width: 6; height: 6; radius: 3
                color: bridge.layoutSaveState === "failed" ? Theme.warning : Theme.accent }
            Column {
                id: stateCol
                x: 22; anchors.verticalCenter: parent.verticalCenter; spacing: 0
                Text { text: chrome.narrow ? qsTr("EDIT") : qsTr("EDIT LAYOUT"); color: Theme.accent; font: Theme.label }
                Text { objectName: "layoutSaveState"; visible: text !== ""
                       text: bridge.layoutSaveState === "saved" ? qsTr("Saved") : bridge.layoutSaveState === "failed" ? qsTr("Not saved") : ""
                       color: bridge.layoutSaveState === "failed" ? Theme.warning : Theme.textMuted; font: Theme.metadata }
            }
        }
        Row {
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            spacing: chrome.narrow ? 4 : 8
            ActionButton { id: cardsButton; objectName: "layoutCards"; label: qsTr("Cards"); trailingIcon: "chevron-down"; height: 36; width: implicitWidth - (chrome.narrow ? 8 : 0)
                onClicked: column.cardsRequested(cardsButton) }
            IconButton { id: resetButton; objectName: "layoutReset"; icon: "reset"; tip: qsTr("Reset layout"); framed: true; width: 36; height: 36
                onClicked: column.resetRequested(resetButton) }
            ActionButton { objectName: "layoutDone"; label: qsTr("Done"); primary: true; height: 36; width: implicitWidth - (chrome.narrow ? 8 : 0)
                onClicked: bridge.setEditMode(false) }
        }
    }
    Text {
        visible: bridge.editMode && bridge.layoutSaveState === "failed"
        y: chrome.height; width: parent.width; elide: Text.ElideRight
        text: qsTr("Change wasn’t saved. Previous layout restored."); color: Theme.warning; font: Theme.metadata
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
            transform: Translate { id: slide; x: Theme.travel }
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
                shadow: column.commandMode      // no black plates under secondary glass
                padding: column.width < 400 ? 12 : column.commandMode ? 16 : 20
                title: (column.commandMode ? {system: qsTr("Model & execution"), workflow: qsTr("Active task"), activity: qsTr("Recent actions"), automations: qsTr("Memory context")}
                                           : { system: qsTr("System status"), workflow: qsTr("Active workflow"), activity: qsTr("Live intelligence feed"), automations: qsTr("Automations") })[modelData.id]
                badge: modelData.id === "system" ? (bridge.executionState === "unconfirmed" ? qsTr("unconfirmed") : bridge.configRefused ? qsTr("config refused") : bridge.executionState === "freeze_requested" ? qsTr("freezing") : (bridge.executionEnabled ? qsTr("running") : qsTr("frozen")))
                     : modelData.id === "activity" ? (column.liveNow ? qsTr("live") : "")
                     : modelData.id === "automations" ? (column.commandMode ? qsTr("%1 projects").arg(bridge.projects.length) : (bridge.jobs.length === 1 ? qsTr("1 job") : qsTr("%1 jobs").arg(bridge.jobs.length)))
                     : ""
                badgeRight: !column.commandMode || modelData.id !== "system"
                titleWrap: !column.commandMode
                badgeColor: modelData.id === "system" ? (bridge.executionState === "unconfirmed" || bridge.executionState === "freeze_requested" ? Theme.warning : bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success)
                          : modelData.id === "activity" ? Theme.success : Theme.accent
                color: column.commandMode ? Theme.surface : column.surface
                accentColor: column.commandMode ? Theme.accent : column.highlight
                border.color: bridge.editMode ? Theme.alpha(Theme.accent, 0.7) : column.commandMode ? Theme.border : Theme.alpha(column.structure, 0.42)
                headerRight: [
                    Item {
                        height: 24; width: bridge.editMode ? handles.width : (modelData.id === "workflow" ? workflowHead.width : (pinned.visible ? pinned.width : 0))
                        Row {
                            id: handles
                            visible: bridge.editMode
                            IconButton { id: panelMenu; objectName: "stackMenu_" + modelData.id; icon: "more"; tip: qsTr("%1 layout options").arg(glass.title); width: 36; height: 30; y: -3
                                onClicked: column.menuRequested(modelData.id, panelMenu, slot.index > 0, slot.index < column.shown.length - 1) }
                        }
                        Row {
                            id: workflowHead
                            spacing: 8
                            visible: !bridge.editMode && modelData.id === "workflow"
                            ActionButton { label: qsTr("Cancel"); danger: true; compact: true; visible: bridge.taskLive; enabled: bridge.taskLive; onClicked: bridge.cancelTask() }
                            ActionButton { label: qsTr("New session"); compact: true; visible: !!bridge.sessionId && !bridge.taskLive; enabled: !bridge.busy; onClicked: bridge.newSession() }
                            Text { objectName: "workflowCounter"; visible: !!bridge.task.task_id; text: column.stagesDone() + "/4"; color: column.highlight; anchors.verticalCenter: parent.verticalCenter; font: Theme.counter }
                        }
                        Text { id: pinned; visible: !bridge.editMode && modelData.pinned && modelData.id !== "workflow"; text: qsTr("PINNED"); color: Theme.textDim; font: Theme.label }
                    }
                ]
                Flickable {
                    id: bodyViewport
                    anchors.fill: parent
                    contentHeight: Math.max(height, modelData.id === "workflow" ? (column.commandMode ? 166 : 0) : modelData.id === "system" ? (column.commandMode ? 138 : 0) : 80)
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
        property string k: ""; property string v: ""; property color tint: Theme.accentSoft; property color vColor: Theme.text
        property string label: k                              // shown; `k` stays the stable key
        property int size: 14
        property int labelX: 21
        property font valueFont: column.commandMode ? Theme.face(size, true) : Theme.uiValue
        width: parent.width; height: 30
        Rectangle { x: 0; width: 7; height: 7; radius: 3.5; color: tint; anchors.verticalCenter: parent.verticalCenter
            Rectangle { anchors.centerIn: parent; width: 15; height: 15; radius: 7.5; color: tint; opacity: 0.2 } }
        Text { x: labelX; anchors.verticalCenter: parent.verticalCenter; text: label; color: Theme.textLabel; renderType: Theme.textRender
               font: column.commandMode ? Theme.face(size, false) : Theme.uiLabel }
        Text { objectName: "statValue_" + k; renderType: Theme.textRender; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; width: parent.width - labelX - kw.width - 8; horizontalAlignment: Text.AlignRight
            // A longer translation steps down (to 11 px at most) before it elides; English fits at its role size.
            text: v; color: vColor; elide: Text.ElideRight; font: valueFont; fontSizeMode: Text.HorizontalFit; minimumPixelSize: 11 }
        TextMetrics { id: kw; text: label; font: column.commandMode ? Theme.face(size, false) : Theme.uiLabel }
    }
    // A gauge: the reference ring with the figure inside. Lit only when a real value exists.
    component Gauge: Item {
        property string label: ""; property string value: "—"; property real ratio: -1; property color tint: Theme.accent
        width: 60; height: 60
        readonly property real stroke: Math.max(3, Math.round(width * 0.085))
        // A quiet halo, then the track, then the dark glass disc and its edge
        // light: the decoration stays under the figure, which keeps its contrast.
        Rectangle { anchors.fill: parent; anchors.margins: -4; radius: width / 2; color: "transparent"; border.width: 1; border.color: ratio < 0 ? Theme.alpha(column.structure, 0.45) : Theme.alpha(tint, 0.28); opacity: 0.5 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "#081a2c"; border.width: stroke; border.color: ratio < 0 ? Theme.alpha(column.structure, 0.62) : Theme.alpha(tint, 0.2) }
        // The measured share of the ring, from 12 o'clock.
        Canvas {
            anchors.fill: parent; visible: ratio >= 0
            property real share: Math.max(0, Math.min(1, ratio)); property color line: tint; property real w: stroke
            onShareChanged: requestPaint(); onLineChanged: requestPaint(); onWidthChanged: requestPaint()
            onPaint: { var c = getContext("2d"); c.reset(); c.lineWidth = w; c.strokeStyle = line; c.lineCap = "butt"
                       c.beginPath(); c.arc(width / 2, height / 2, width / 2 - w / 2, -Math.PI / 2, -Math.PI / 2 + share * 2 * Math.PI); c.stroke() }
        }
        Rectangle { anchors.fill: parent; anchors.margins: stroke; radius: width / 2; color: "transparent"; border.width: 1; border.color: column.structure; opacity: 0.4 }
        Rectangle { anchors.fill: parent; anchors.margins: stroke + 1; radius: width / 2
            gradient: Gradient { GradientStop { position: 0; color: "#14102a44" } GradientStop { position: 1; color: "#00000000" } } }
        // A calibration tick at 12 o'clock: instrument, not value.
        Rectangle { anchors.horizontalCenter: parent.horizontalCenter; y: 1; width: 2; height: stroke - 2; color: column.highlight; opacity: 0.8 }
        Column { anchors.centerIn: parent; spacing: width < 48 ? -3 : -1
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: value; color: ratio < 0 ? Theme.textMuted : Theme.text
                   font { family: Theme.display; pixelSize: parent.parent.width < 48 ? 12 : 16; weight: Font.Medium; variableAxes: Theme.semibold600; features: Theme.tabular } }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: label; color: Theme.textLabel; font: Theme.label }
        }
    }
    // The stack's shared alignment grid, inside a well (px from the well's
    // inner left / right edges): A icon column at 12, B text after the icon
    // slot (+8), C the value column after the widest field label, D the
    // trailing slot at 12 from the right. The icon slot is 28 px on desktop
    // rhythm and 24 px compact; a smaller tile centres inside it so B holds.
    readonly property int gridA: 12
    readonly property int gridD: 12
    // One row of the workflow well: icon well, label, state dot, value, trailing indicator.
    component WRow: Item {
        id: wrow
        property string icon: "diamond"; property color tint: Theme.accent; property string k: ""; property string label: k; property string v: ""; property color vColor: "#9fb6d0"; property bool empty: false; property bool last: false
        property string tip: ""                              // the exact codes behind a worded value
        width: parent.width; height: 41
        HoverTip { objectName: "workflowTip_" + wrow.k; text: wrow.tip; active: wrow.tip !== "" }
        readonly property bool compact: height < 30
        readonly property int slot: height >= 38 ? 28 : 24
        readonly property int tile: Math.min(slot, height - 4)
        readonly property int fs: column.commandMode ? 12 : 14
        // Compact rows (1536 / 1280) give the meaningful value its room first: 13 px
        // for both texts, tighter gaps, and no decorative indicator - whole labels,
        // whole short values, no ellipsis where one fits.
        readonly property font labelFont: column.commandMode ? Theme.face(fs, false)
                                          : compact ? Theme.caption : Theme.uiLabel
        readonly property font valueFont: column.commandMode ? Theme.face(fs, false)
                                          : compact ? Theme.captionStrong : Theme.uiValue
        readonly property real textX: column.gridA + slot + 8
        // C: every label keeps its full width - the column is set by the widest field.
        readonly property real dotX: column.commandMode ? Math.round(parent.width * (parent.width < 340 ? 0.39 : 0.42))
                                                        : Math.ceil(textX + widest.advanceWidth + (compact ? 6 : 12))
        readonly property int trail: 10                      // the trailing indicator's own slot
        // The widest of the five workflow labels, in the interface language.
        TextMetrics { id: widest; font: wrow.labelFont
            text: [qsTr("Status"), qsTr("Connector"), qsTr("Capability"), qsTr("Destination"), qsTr("Outcome")].reduce(function(a, b) { return b.length > a.length ? b : a }) }
        Rectangle { x: column.gridA + (slot - tile) / 2; anchors.verticalCenter: parent.verticalCenter; width: tile; height: tile; radius: tile < 24 ? 5 : 7
            color: empty ? "#0b2036" : "#0e2f4e"; border.width: 1; border.color: Theme.alpha(column.structure, empty ? 0.4 : 0.75)
            Icon { anchors.centerIn: parent; name: wrow.icon; tint: empty ? Theme.alpha(column.structure, 0.8) : column.highlight; size: Math.round(tile * 0.5) } }
        Text { x: textX; width: dotX - x - (compact ? 4 : 8); elide: Text.ElideRight; renderType: Theme.textRender; anchors.verticalCenter: parent.verticalCenter; text: label
               color: empty ? Theme.textLabel : Theme.text; font: wrow.labelFont }
        Rectangle { x: dotX; width: compact ? 6 : 7; height: width; radius: width / 2; anchors.verticalCenter: parent.verticalCenter; color: empty ? "#4f6b8a" : tint
            Rectangle { visible: !empty; anchors.centerIn: parent; width: parent.width + 8; height: width; radius: width / 2; color: tint; opacity: 0.22 } }
        // The value stops before the trailing slot; it never reaches the indicator.
        // Compact rows give the decorative indicator's width to the value instead.
        Text { objectName: "workflowValue_" + k
            renderType: Theme.textRender
            anchors.left: parent.left; anchors.leftMargin: dotX + (compact ? 9 : 15); anchors.right: parent.right; anchors.rightMargin: compact ? 8 : column.gridD + trail + 8; anchors.verticalCenter: parent.verticalCenter
            // A compact value steps down one size (never below 12) before it elides, like the system rows.
            text: v; color: empty ? Theme.textMuted : vColor; elide: Text.ElideRight; font: wrow.valueFont
            fontSizeMode: compact ? Text.HorizontalFit : Text.FixedSize; minimumPixelSize: 12 }
        Icon { visible: !compact; anchors.right: parent.right; anchors.rightMargin: column.gridD; anchors.verticalCenter: parent.verticalCenter
               name: "grip"; size: trail; tint: "#4f6b8a" }
        Rectangle { visible: !last; anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: textX; rightMargin: column.gridD } height: 1; color: Theme.alpha(column.structure, 0.22) }
    }
    // The inset well that holds rows (the reference's rounded inner frame).
    component Well: Rectangle {
        default property alias rows: inner.data
        property bool fill: false
        width: parent.width; height: fill ? parent.height : inner.height + 2
        radius: 10; color: Theme.glassSecondaryReading; border.width: 1; border.color: Theme.alpha(column.structure, 0.18)
        Column { id: inner; x: 1; y: 1; width: parent.width - 2 }
    }

    Component {
        id: systemPanel
        Item {
            id: sys
            readonly property real bw: width
            readonly property real bh: height
            // Metrics beside the metadata while the body is wide enough (the
            // desktop arrangement, with local gaps reduced first); otherwise the
            // local reflow inside the same panel height: the rings across the
            // top, the note beneath, then the three metadata fields.
            readonly property bool side: bw >= 260
            // Reflowed and short: smaller rings, so the three fields beneath keep whole labels and values.
            readonly property int d: side ? (bw >= 460 ? 60 : bw >= 340 ? 48 : 34) : (bh < 90 ? 28 : 32)
            readonly property int g: side ? (bw >= 340 ? Math.round(d * 0.55) : 10) : 12
            readonly property int clusterW: 3 * d + 2 * g
            readonly property real listX: side && measured ? clusterW + 14 : 0
            readonly property int fs: 14
            readonly property int pitch: Math.max(20, Math.min(bh >= 110 ? 38 : 34, Math.floor((bh - 6) / 3)))
            // The caption is decoration: in the reflowed arrangement it yields its
            // room to the three fields (whole labels first) when the body is short.
            readonly property bool caption: side ? bh >= d + 34 : bh >= d + 74
            // The ring centre line; the list centres on it wherever the body allows.
            readonly property real cy: side ? (caption ? (bh - 16) / 2 : bh / 2) : d / 2
            readonly property real listY: Math.max(3, Math.min(Math.round(cy - 1.5 * pitch), bh - 3 - 3 * pitch))
            readonly property string uptime: (Math.floor(bridge.uptimeSeconds / 3600) > 0 ? Math.floor(bridge.uptimeSeconds / 3600) + "h " : "") + (Math.floor(bridge.uptimeSeconds / 60) % 60) + "m"
            readonly property string execution: bridge.executionState === "unconfirmed" ? qsTr("Unconfirmed") : bridge.configRefused ? qsTr("Refused") : bridge.executionState === "freeze_requested" ? qsTr("Freezing") : (bridge.executionEnabled ? qsTr("Running") : qsTr("Frozen"))
            readonly property color executionTint: bridge.executionState === "unconfirmed" || bridge.executionState === "freeze_requested" ? Theme.warning : bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success
            readonly property string model: (bridge.modelCard || {}).name
                                            || (bridge.chatProvider === "claude" ? qsTr("CLI default") : qsTr("No model chosen"))
            // The machine's own load, read from the kernel every few seconds.
            // No reading, no ring: the metadata simply takes the whole width.
            readonly property var load: bridge.telemetry || ({})
            readonly property bool measured: load.available === true
            Row {
                id: gauges
                objectName: "systemGauges"
                visible: sys.measured
                x: 2; y: Math.round(sys.cy - sys.d / 2)
                spacing: sys.g
                Gauge { objectName: "gaugeCpu"; label: "CPU"; width: sys.d; height: width
                        value: sys.measured ? sys.load.cpu + "%" : "—"; ratio: sys.measured ? sys.load.cpu / 100 : -1 }
                Gauge { objectName: "gaugeRam"; label: "RAM"; width: sys.d; height: width
                        value: sys.measured ? sys.load.ram + "%" : "—"; ratio: sys.measured ? sys.load.ram / 100 : -1 }
                Gauge { objectName: "gaugeDisk"; label: qsTr("Disk"); width: sys.d; height: width
                        value: sys.measured ? sys.load.disk + "%" : "—"; ratio: sys.measured ? sys.load.disk / 100 : -1 }
            }
            // Said once beneath the rings: what they are.
            Text { objectName: "systemNote"; visible: sys.caption && sys.measured
                   x: 2; y: gauges.y + sys.d + (sys.side ? 9 : 3)
                   width: !sys.side ? sys.bw - 4 : sys.bw >= 340 ? sys.clusterW : sys.listX - 10
                   horizontalAlignment: sys.side ? Text.AlignHCenter : Text.AlignLeft
                   text: qsTr("This machine, now"); color: Theme.textMuted; elide: Text.ElideRight
                   font: Theme.caption }
            // Beside the rings: three rows - one dot axis, labels left, values right.
            Rectangle { visible: sys.side; x: sys.listX - 8; width: sys.bw - x; radius: 10
                y: sys.listY - 4; height: 3 * sys.pitch + 8; color: Theme.glassSecondaryReading }
            Column {
                objectName: "systemMetadata"
                visible: sys.side
                x: sys.listX; width: sys.bw - x - 6
                y: sys.listY
                spacing: 0
                StatRow { height: sys.pitch; size: sys.fs; labelX: 15; k: "Uptime"; label: qsTr("Uptime"); v: sys.uptime; valueFont: Theme.uptimeValue }
                StatRow { height: sys.pitch; size: sys.fs; labelX: 15; k: "Execution"; label: qsTr("Execution"); v: sys.execution; tint: sys.executionTint; vColor: tint }
                StatRow { height: sys.pitch; size: sys.fs; labelX: 15; k: "Model"; label: qsTr("Model"); v: sys.model }
            }
            // Reflowed: the three fields side by side beneath the rings - each a
            // dot and label over its value - so all three stay inside the height.
            // A long label wraps onto a second line rather than losing its meaning.
            readonly property int fieldsY: sys.measured ? sys.d + (sys.caption ? 20 : 4) : 4
            Rectangle { visible: !sys.side; x: 0; width: sys.bw; y: sys.fieldsY - 1; height: sys.bh - y; radius: 7; color: Theme.glassSecondaryReading }
            Row {
                objectName: "systemMetadataCompact"
                visible: !sys.side
                x: 6; y: sys.fieldsY; width: sys.bw - 12
                Repeater {
                    model: [{ k: "Uptime", label: qsTr("Uptime"), v: sys.uptime, tint: Theme.accentSoft, vc: Theme.text },
                            { k: "Execution", label: qsTr("Execution"), v: sys.execution, tint: sys.executionTint, vc: sys.executionTint },
                            { k: "Model", label: qsTr("Model"), v: sys.model, tint: Theme.accentSoft, vc: Theme.text }]
                    Item {
                        width: (sys.bw - 12) / 3; height: sys.bh - sys.fieldsY
                        Rectangle { x: 0; y: 5; width: 6; height: 6; radius: 3; color: modelData.tint }
                        Text { id: compactLabel; x: 10; y: 0; width: parent.width - 12; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                               text: modelData.label; color: Theme.textLabel; font: Theme.caption
                               lineHeightMode: Text.FixedHeight; lineHeight: 13 }
                        Text { x: 10; y: compactLabel.height + 1; width: parent.width - 12; elide: Text.ElideRight; text: modelData.v; color: modelData.vc
                               font: Theme.face(13, true, 0, modelData.k === "Uptime") }
                    }
                }
            }
        }
    }
    Component {
        id: workflowPanel
        Well {
            id: workflowWell
            fill: true
            readonly property bool has: !!bridge.task.task_id
            readonly property real pitch: Math.floor((height - 2) / 5)
            // Projections only: the task's own status and outcome as Core
            // reported them. Global execution state never rewrites them.
            readonly property bool approvalShown: bridge.approvalPending && workflowWell.has
            readonly property var outcome: bridge.safety.fixture ? ({}) : (bridge.safety.outcome || ({}))
            WRow { height: workflowWell.pitch; icon: "diamond"; k: "Status"; label: qsTr("Status"); empty: !workflowWell.has
                tint: workflowWell.approvalShown ? Theme.warning : Theme.stateColor(bridge.coreState)
                vColor: workflowWell.approvalShown ? Theme.warning : bridge.coreState === "running" ? Theme.accent : "#cfdceb"
                v: workflowWell.approvalShown ? qsTr("Approval required") : workflowWell.has ? (column.statusWord(bridge.task.status) || "—") + (bridge.task.stage ? " / " + i18n.word("stage", bridge.task.stage) : "") : qsTr("No active task")
                tip: workflowWell.has ? [bridge.task.status, bridge.task.stage].filter(function(c) { return !!c }).join(" · ") : "" }
            WRow { height: workflowWell.pitch; icon: "plug"; tint: Theme.accent; k: "Connector"; label: qsTr("Connector"); empty: !workflowWell.has; vColor: "#cfdceb"; v: workflowWell.has ? (bridge.task.connector || "—") : qsTr("Waiting for your request…") }
            WRow { height: workflowWell.pitch; icon: "spark"; tint: Theme.accent; k: "Capability"; label: qsTr("Capability"); empty: !workflowWell.has; vColor: "#cfdceb"; v: workflowWell.has ? (bridge.task.capability || "—") : qsTr("Chosen by Core") }
            WRow { height: workflowWell.pitch; icon: "arrow-up-right"; tint: Theme.accent; k: "Destination"; label: qsTr("Destination"); empty: !workflowWell.has; vColor: "#cfdceb"; v: workflowWell.has ? (bridge.task.destination || "—") : qsTr("Granted destinations only") }
            WRow { height: workflowWell.pitch; icon: "clock"; k: "Outcome"; label: qsTr("Outcome"); empty: !workflowWell.has; last: true
                readonly property string kind: workflowWell.outcome.kind || ""
                tint: kind === "unknown" || kind === "in_progress" ? Theme.unknownEdge : kind === "failed" ? Theme.danger : kind === "blocked" ? Theme.warning
                    : kind === "completed" ? Theme.success : Theme.accentSoft
                vColor: kind === "unknown" || kind === "in_progress" ? Theme.unknownEdge : kind === "failed" ? Theme.danger : kind === "blocked" ? Theme.warning
                      : kind === "completed" ? Theme.success : "#cfdceb"
                v: !workflowWell.has ? qsTr("None yet")
                   : ({ unknown: qsTr("Outcome unknown"), in_progress: qsTr("Not established yet"), failed: qsTr("Failed"), blocked: qsTr("Blocked"), completed: qsTr("Completed"), cancelled: qsTr("Cancelled"), paused: qsTr("Paused by Freeze") })[kind]
                     || column.statusWord(bridge.task.run_status) || qsTr("pending") }
        }
    }
    Component {
        id: activityPanel
        // The intelligence stream: one shared surface, a compact source
        // glyph, the event, quieter metadata, the real relative time on the
        // right, hairline separators between rows. Measured off the canonical
        // reference: rows on a 48 px pitch at 1536 (60 at 1920), the text
        // block 85 px in, the time 15 px from the edge. The model is the
        // bridge's real activity list; when it is sparse the footer says so.
        // The Loader sizes its root to the body, so the well sits inside a
        // plain root and keeps its own height: whole rows only - it ends after
        // its content when the stream is sparse and shows as many complete
        // rows as fit when it is not.
        Item {
          id: feedRoot
          Well {
            id: feedWell
            readonly property bool compact: column.commandMode || width < 380
            readonly property int pitch: compact ? 46 : 56
            // The icon slot follows the shared grid (28 desktop, 24 compact);
            // the typography stays as it was.
            readonly property int glyphD: column.commandMode ? 24 : (width >= 340 ? 28 : 24)
            readonly property real textX: column.gridA + glyphD + 8
            // D: a stable, unwrapped timestamp column (~56 px) at the shared right inset.
            readonly property real timeW: Math.max(56, Math.ceil(stamp.advanceWidth))
            TextMetrics { id: stamp; text: qsTr("%1 min ago").arg("00"); font: column.commandMode ? Theme.caption : Theme.timestamp }
            readonly property int rowsShown: bridge.activity.length + (bridge.activity.length < 4 ? 1 : 0)
            readonly property int rowsFit: Math.max(1, Math.floor((feedRoot.height - 2) / pitch))
            // The well takes the whole body: as many real rows as fit, and a
            // quiet surface - never a hole - beneath a short list.
            objectName: "activityWell"
            height: feedRoot.height
            ListView {
                width: parent.width; height: feedWell.height - 2
                clip: true
                model: bridge.activity
                spacing: 0
                delegate: Item {
                    id: feedRow
                    width: ListView.view.width; height: feedWell.pitch
                    readonly property bool semantic: modelData.kind === "approval" || modelData.kind === "unknown" || modelData.kind === "error"
                    readonly property color tint: modelData.kind === "approval" || modelData.kind === "unmet" ? Theme.warning : modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : column.highlight
                    readonly property string icon: modelData.kind === "approval" ? "flag" : modelData.kind === "unmet" ? "minus" : modelData.kind === "unknown" ? "question" : modelData.kind === "error" ? "exclamation" : modelData.kind === "scheduled" ? "retry" : "diamond"
                    readonly property string source: modelData.kind === "approval" ? qsTr("Authorization") : modelData.kind === "unmet" ? qsTr("Objective not met") : modelData.kind === "unknown" ? qsTr("Outcome unknown") : modelData.kind === "error" ? qsTr("Error") : modelData.kind === "scheduled" ? qsTr("Scheduled run") : qsTr("Interface")
                    // Source glyph: a small technical disc, blue unless the event itself is semantic.
                    Rectangle { x: column.gridA; anchors.verticalCenter: parent.verticalCenter; width: feedWell.glyphD; height: width; radius: width / 2
                        color: semantic ? Theme.alpha(tint, 0.16) : "#0e2f4e"; border.width: 1; border.color: semantic ? Theme.alpha(tint, 0.55) : Theme.alpha(column.structure, 0.75)
                        Icon { anchors.centerIn: parent; name: feedRow.icon; tint: feedRow.tint; size: Math.round(feedWell.glyphD * 0.5) } }
                    // Compact: the event keeps its whole line; the time joins the source beneath it.
                    Column { anchors.left: parent.left; anchors.leftMargin: feedWell.textX; anchors.right: feedWell.compact ? parent.right : timeText.left
                             anchors.rightMargin: feedWell.compact ? column.gridD : 8; anchors.verticalCenter: parent.verticalCenter; spacing: 1
                        Text { objectName: "feedTitle"; text: modelData.text; color: Theme.text; width: parent.width; elide: Text.ElideRight
                               renderType: Theme.textRender
                               font: column.commandMode ? Theme.caption : Theme.eventTitle; lineHeightMode: Text.FixedHeight; lineHeight: column.commandMode ? 17 : 20 }
                        Text { objectName: "feedSource"; text: source + (feedWell.compact ? "  ·  " + column.ago(modelData.ts, modelData.time) : "")
                               color: column.commandMode ? Theme.textMuted : Theme.eventMetaInk; width: parent.width; elide: Text.ElideRight
                               renderType: Theme.textRender
                               font: column.commandMode ? Theme.caption : Theme.eventMeta; lineHeightMode: Text.FixedHeight; lineHeight: column.commandMode ? 15 : Theme.eventMetaLine }
                    }
                    Text { id: timeText; objectName: "feedTime"; visible: !feedWell.compact; renderType: Theme.textRender; anchors.right: parent.right; anchors.rightMargin: column.gridD; anchors.verticalCenter: parent.verticalCenter
                           width: feedWell.timeW; horizontalAlignment: Text.AlignRight; elide: Text.ElideLeft
                           text: column.ago(modelData.ts, modelData.time); color: Theme.textMuted
                           font: column.commandMode ? Theme.caption : Theme.timestamp }
                    Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: feedWell.textX; rightMargin: column.gridD } height: 1; color: Theme.alpha(column.structure, 0.22) }
                }
                // Sparse and empty states share the row grammar, one step quieter.
                footer: Item {
                    width: ListView.view.width; height: visible ? feedWell.pitch : 0
                    visible: bridge.activity.length < 4
                    Rectangle { x: column.gridA; anchors.verticalCenter: parent.verticalCenter; width: feedWell.glyphD; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: Theme.alpha(column.structure, 0.4)
                        Rectangle { anchors.centerIn: parent; width: 5; height: 5; radius: 2.5; color: "#4f6b8a" } }
                    Column { anchors.left: parent.left; anchors.leftMargin: feedWell.textX; anchors.right: parent.right; anchors.rightMargin: column.gridD; anchors.verticalCenter: parent.verticalCenter; spacing: 1
                        Text { text: bridge.activity.length === 0 ? qsTr("Nothing yet") : qsTr("Nothing else yet"); color: Theme.textLabel; width: parent.width; elide: Text.ElideRight
                               renderType: Theme.textRender
                               font: column.commandMode ? Theme.caption : Theme.eventTitle; lineHeightMode: Text.FixedHeight; lineHeight: column.commandMode ? 17 : 20 }
                        Text { objectName: "feedDetail"; text: qsTr("Approvals, outcomes and scheduled runs appear here")
                               color: column.commandMode ? Theme.textMuted : Theme.eventMetaInk; width: parent.width; elide: Text.ElideRight
                               renderType: Theme.textRender
                               font: column.commandMode ? Theme.caption : Theme.eventMeta; lineHeightMode: Text.FixedHeight; lineHeight: column.commandMode ? 15 : Theme.eventMetaLine }
                    }
                }
            }
          }
        }
    }
    Component {
        id: automationsPanel
        Well {
            WRow { visible: bridge.jobs.length === 0; icon: "retry"; k: "Jobs"; label: qsTr("Jobs"); empty: true; last: true; v: qsTr("No jobs defined") }
            Repeater {
                model: bridge.jobs.slice(0, 5)
                WRow { icon: "retry"; tint: modelData.enabled ? Theme.success : Theme.textDim; last: index === Math.min(bridge.jobs.length, 5) - 1
                    k: modelData.readable ? (modelData.label.length > 12 ? modelData.label.substring(0, 12) + "…" : modelData.label) : qsTr("(unreadable)")
                    v: modelData.next_due_at_utc ? qsTr("next %1").arg(i18n.when(modelData.next_due_at_utc, column.nowSec * 1000, "time")) : "—" }
            }
        }
    }
}
