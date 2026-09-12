import QtQuick
import "../theme"

// The right-side information column: an ordered stack of real panels.
// Heights come from the persisted shares, reflow is animated, and Edit Mode
// adds handles (hide, pin, move, resize) plus the hidden-panel tray and
// Reset Layout. All geometry is derived, so nothing can leave the screen.
Item {
    id: column
    property var panels: bridge.panels
    property var shown: panels.filter(function(p) { return p.shown })
    property real totalShare: shown.reduce(function(sum, p) { return sum + p.share }, 0)
    property real editChrome: bridge.editMode ? 54 : 0
    property real usable: height - editChrome - Math.max(0, shown.length - 1) * Theme.gap
    readonly property int minPanelHeight: 120

    // Every shown panel gets its minimum first; the remainder is split by
    // share. The sum therefore never exceeds the column, whatever the shares.
    function heightFor(index) {
        var n = shown.length
        var free = usable - n * minPanelHeight
        if (free < 0) return Math.max(0, usable / Math.max(n, 1))
        return minPanelHeight + free * (shown[index].share / Math.max(totalShare, 0.0001))
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
                title: ({ system: "System status", workflow: "Active workflow", activity: "Live intelligence feed", automations: "Automations" })[modelData.id]
                badge: modelData.id === "system" ? (bridge.configRefused ? "config refused" : (bridge.executionEnabled ? "enabled" : "disabled"))
                     : modelData.id === "activity" ? "live"
                     : modelData.id === "automations" ? (bridge.jobs.length + " job" + (bridge.jobs.length === 1 ? "" : "s"))
                     : ""
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
                            spacing: 6
                            visible: !bridge.editMode && modelData.id === "workflow"
                            ActionButton { label: "Cancel"; danger: true; compact: true; visible: bridge.taskLive; enabled: bridge.taskLive; onClicked: bridge.cancelTask() }
                            ActionButton { label: "New session"; compact: true; visible: !!bridge.sessionId; enabled: !bridge.busy; onClicked: bridge.newSession() }
                            Pill { visible: !bridge.taskLive && !bridge.sessionId; text: bridge.coreState === "idle" ? "idle" : Theme.stateLabel(bridge.coreState); tint: Theme.stateColor(bridge.coreState); anchors.verticalCenter: parent.verticalCenter }
                        }
                        Text { id: pinned; visible: !bridge.editMode && modelData.pinned && modelData.id !== "workflow"; text: "PINNED"; color: Theme.textDim; font { family: Theme.font; pixelSize: 9; letterSpacing: 1.5 } }
                    }
                ]
                Loader {
                    anchors.fill: parent
                    sourceComponent: ({ system: systemPanel, workflow: workflowPanel, activity: activityPanel, automations: automationsPanel })[modelData.id]
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
    // A quiet stat line: dot, key, value right-aligned.
    component StatRow: Item {
        property string k: ""; property string v: ""; property color tint: Theme.accent; property color vColor: Theme.text
        width: parent.width; height: 27
        Rectangle { width: 6; height: 6; radius: 3; color: tint; anchors.verticalCenter: parent.verticalCenter
            Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: tint; opacity: 0.22 } }
        Text { x: 18; anchors.verticalCenter: parent.verticalCenter; text: k; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
        Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 100; horizontalAlignment: Text.AlignRight
            text: v; color: vColor; elide: Text.ElideMiddle; font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
    }
    // A gauge slot: the reference ring, lit only when a real value exists.
    component Gauge: Item {
        property string label: ""; property string value: "—"; property real ratio: -1; property color tint: Theme.accent
        width: 62; height: 78
        Item { width: 58; height: 58; anchors.horizontalCenter: parent.horizontalCenter
            Rectangle { anchors.fill: parent; radius: 29; color: "transparent"; border.width: 4; border.color: ratio < 0 ? Theme.alpha(Theme.textDim, 0.35) : Theme.alpha(tint, 0.9) }
            Rectangle { anchors.centerIn: parent; width: 46; height: 46; radius: 23; color: Theme.alpha(Theme.bg0, 0.5) }
            Column { anchors.centerIn: parent; spacing: 0
                Text { anchors.horizontalCenter: parent.horizontalCenter; text: value; color: ratio < 0 ? Theme.textDim : Theme.text; font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold } }
            }
        }
        Text { anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter; text: label; color: Theme.textMuted; font { family: Theme.font; pixelSize: 9; letterSpacing: 1.2 } }
    }
    // A strip: icon box, label, status dot, status text at the right.
    component Strip: Rectangle {
        property string glyph: "◇"; property color tint: Theme.accent; property string k: ""; property string v: ""; property color vColor: Theme.textLabel; property bool empty: false
        width: parent.width; height: 36; radius: 9
        color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.border, 0.7)
        IconBox { x: 6; anchors.verticalCenter: parent.verticalCenter; width: 26; height: 26; glyph: parent.glyph; tint: empty ? Theme.textDim : parent.tint; glyphSize: 12 }
        Text { x: 42; anchors.verticalCenter: parent.verticalCenter; text: k; color: Theme.text; font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
        Rectangle { x: 148; width: 5; height: 5; radius: 2.5; anchors.verticalCenter: parent.verticalCenter; color: empty ? Theme.textDim : tint; opacity: empty ? 0.5 : 0.9 }
        Text { anchors.left: parent.left; anchors.leftMargin: 161; anchors.right: parent.right; anchors.rightMargin: 28; anchors.verticalCenter: parent.verticalCenter
            text: v; color: empty ? Theme.textDim : vColor; elide: Text.ElideMiddle; font { family: Theme.font; pixelSize: 12 } }
        Text { anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; text: "⁝"; color: Theme.textDim; font.pixelSize: 12 }
    }

    Component {
        id: systemPanel
        Item {
            Row {
                id: gauges
                anchors.left: parent.left; anchors.top: parent.top; anchors.topMargin: 2
                spacing: 8
                // Not measured by this build: unlit slots, said plainly below.
                Gauge { label: "CPU" }
                Gauge { label: "RAM" }
                Gauge { label: "DISK" }
            }
            Text { anchors.left: parent.left; anchors.top: gauges.bottom; anchors.topMargin: 6; text: "not measured by this build"; color: Theme.textDim; font { family: Theme.font; pixelSize: 9; letterSpacing: 0.6 } }
            Column {
                anchors { left: gauges.right; right: parent.right; top: parent.top; leftMargin: 18; topMargin: 4 }
                visible: parent.width >= 430
                spacing: 1
                StatRow { k: "Uptime"; v: (Math.floor(bridge.uptimeSeconds / 3600) > 0 ? Math.floor(bridge.uptimeSeconds / 3600) + "h " : "") + (Math.floor(bridge.uptimeSeconds / 60) % 60) + "m"; tint: Theme.success }
                StatRow { k: "Execution"; v: bridge.configRefused ? "refused" : (bridge.executionEnabled ? "enabled" : "disabled"); tint: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success; vColor: tint }
                StatRow { k: "Model"; v: (bridge.config.claude_model || "CLI default") + (bridge.config.claude_effort ? " · " + bridge.config.claude_effort : ""); tint: Theme.success }
                StatRow { k: "Mode"; v: bridge.config.permission_mode || "—"; tint: Theme.accent }
            }
        }
    }
    Component {
        id: workflowPanel
        Column {
            spacing: 5
            readonly property bool has: !!bridge.task.task_id
            Strip { glyph: "◇"; tint: Theme.stateColor(bridge.coreState); k: "Status"; empty: !parent.has
                v: parent.has ? (bridge.task.status || "—") + (bridge.task.stage ? " / " + bridge.task.stage : "") : "No active task" }
            Strip { glyph: "⌘"; tint: Theme.teal; k: "Connector"; empty: !parent.has; v: parent.has ? (bridge.task.connector || "—") : "Waiting for your request" }
            Strip { glyph: "✦"; tint: Theme.accent; k: "Capability"; empty: !parent.has; v: parent.has ? (bridge.task.capability || "—") : "Chosen by the Core, never by the model" }
            Strip { glyph: "➶"; tint: Theme.violet; k: "Destination"; empty: !parent.has; v: parent.has ? (bridge.task.destination || "—") : "Granted destinations only" }
            Strip { glyph: "!"; k: "Outcome"; empty: !parent.has
                tint: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.success)
                vColor: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.textLabel)
                v: parent.has ? (bridge.task.error_code || (bridge.task.run_status || "pending")) : "None yet" }
        }
    }
    Component {
        id: activityPanel
        ListView {
            clip: true
            model: bridge.activity
            spacing: 0
            delegate: Item {
                width: ListView.view.width; height: 58
                readonly property color tint: modelData.kind === "approval" ? Theme.warning : modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "scheduled" ? Theme.success : Theme.accent
                readonly property string glyph: modelData.kind === "approval" ? "⚑" : modelData.kind === "unknown" ? "?" : modelData.kind === "error" ? "!" : modelData.kind === "scheduled" ? "⟳" : "◈"
                IconBox { x: 2; anchors.verticalCenter: parent.verticalCenter; width: 32; height: 32; glyph: parent.glyph; tint: parent.tint; glyphSize: 13 }
                Column { anchors.left: parent.left; anchors.leftMargin: 46; anchors.right: timeText.left; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                    Text { text: modelData.text; color: Theme.text; width: parent.width; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 13 } }
                    Text { text: modelData.kind === "approval" ? "authorization" : modelData.kind === "unknown" ? "outcome unknown" : modelData.kind === "error" ? "error" : modelData.kind === "scheduled" ? "scheduled" : "interface"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                }
                Text { id: timeText; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: modelData.time; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
                Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: Theme.border; opacity: 0.55 }
            }
            footer: Item {
                width: ListView.view.width; height: bridge.activity.length < 4 ? 58 : 0
                visible: bridge.activity.length < 4
                Row { anchors.verticalCenter: parent.verticalCenter; spacing: 12
                    Rectangle { width: 32; height: 32; radius: 9; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.border, 0.9)
                        Rectangle { anchors.centerIn: parent; width: 5; height: 5; radius: 2.5; color: Theme.textDim } }
                    Column { anchors.verticalCenter: parent.verticalCenter; spacing: 2
                        Text { text: "Nothing else yet"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
                        Text { text: "Approvals, outcomes and scheduled runs will appear here"; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
                    }
                }
            }
        }
    }
    Component {
        id: automationsPanel
        Column {
            spacing: 5
            Strip { visible: bridge.jobs.length === 0; glyph: "⟳"; k: "Jobs"; empty: true; v: "No jobs defined" }
            Repeater {
                model: bridge.jobs.slice(0, 5)
                Strip { glyph: "⟳"; tint: modelData.enabled ? Theme.success : Theme.textDim
                    k: modelData.readable ? (modelData.label.length > 12 ? modelData.label.substring(0, 12) + "…" : modelData.label) : "(unreadable)"
                    v: modelData.next_due_at_utc ? "next " + modelData.next_due_at_utc.substring(5, 16) + "Z" : "—" }
            }
        }
    }
}
