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
                        height: 24; width: bridge.editMode ? handles.width : (modelData.id === "workflow" ? workflowButtons.width : pinned.width)
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
                            id: workflowButtons
                            spacing: 6
                            visible: !bridge.editMode && modelData.id === "workflow"
                            ActionButton { label: "Cancel"; danger: true; compact: true; enabled: bridge.taskLive; onClicked: bridge.cancelTask() }
                            ActionButton { label: "New session"; compact: true; enabled: !bridge.busy; onClicked: bridge.newSession() }
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
    // A quiet stat line: dot, key, value right-aligned (the reference's
    // Uptime / Memory / Network list).
    component StatRow: Item {
        property string k: ""; property string v: ""; property color tint: Theme.accent; property color vColor: Theme.text
        width: parent.width; height: 27
        Rectangle { width: 6; height: 6; radius: 3; color: tint; anchors.verticalCenter: parent.verticalCenter
            Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: tint; opacity: 0.22 } }
        Text { x: 18; anchors.verticalCenter: parent.verticalCenter; text: k; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
        Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 150; horizontalAlignment: Text.AlignRight
            text: v; color: vColor; elide: Text.ElideMiddle; font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
    }
    // A strip: icon box, label, status text at the right (the reference's
    // Active Workflow rows).
    component Strip: Rectangle {
        property string glyph: "◇"; property color tint: Theme.accent; property string k: ""; property string v: ""; property color vColor: Theme.textLabel
        width: parent.width; height: 38; radius: 9
        color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.border, 0.7)
        IconBox { x: 6; anchors.verticalCenter: parent.verticalCenter; width: 26; height: 26; glyph: parent.glyph; tint: parent.tint; glyphSize: 12 }
        Text { x: 42; anchors.verticalCenter: parent.verticalCenter; text: k; color: Theme.text; font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
        Rectangle { x: 130; width: 5; height: 5; radius: 2.5; anchors.verticalCenter: parent.verticalCenter; color: tint; opacity: 0.9 }
        Text { anchors.left: parent.left; anchors.leftMargin: 143; anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
            text: v; color: vColor; elide: Text.ElideMiddle; font { family: Theme.font; pixelSize: 12 } }
    }

    // A stat tile: one large honest figure with its label (the reference's
    // gauge row, without inventing gauges for values this build does not measure).
    component StatTile: Rectangle {
        property string k: ""; property string v: ""; property color tint: Theme.accent
        height: 66; radius: 12
        color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.border, 0.8)
        Rectangle { x: 12; y: 0; height: 1; width: parent.width * 0.5
            gradient: Gradient { orientation: Gradient.Horizontal; GradientStop { position: 0; color: Theme.alpha(tint, 0.6) } GradientStop { position: 1; color: "transparent" } } }
        Column { anchors.centerIn: parent; spacing: 3
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: v; color: Theme.text; font { family: Theme.font; pixelSize: 19; weight: Font.DemiBold; letterSpacing: -0.3 } }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: k.toUpperCase(); color: Theme.textMuted; font { family: Theme.font; pixelSize: 9; letterSpacing: 1.8 } }
        }
    }

    Component {
        id: systemPanel
        Item {
            Column {
                id: rows
                width: parent.width
                spacing: 0
                StatRow { k: "Execution"; v: bridge.configRefused ? "configuration refused" : (bridge.executionEnabled ? "enabled" : "disabled"); tint: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success; vColor: tint }
                StatRow { k: "Permission mode"; v: bridge.config.permission_mode || "—"; tint: Theme.success }
                StatRow { k: "Provider"; v: "claude · Claude Code CLI"; tint: Theme.success }
                StatRow { k: "Model"; v: bridge.config.claude_model || "CLI default"; tint: Theme.accent }
                StatRow { k: "Effort"; v: bridge.config.claude_effort || "CLI default"; tint: Theme.accent }
                StatRow { k: "Paid API"; v: bridge.config.paid_api_enabled ? "enabled" : "off · subscription route"; tint: bridge.config.paid_api_enabled ? Theme.warning : Theme.accent }
                StatRow { k: "Database"; v: bridge.config.memory_database || "—"; tint: Theme.accentSoft }
            }
            // Tiles fill the lower part when the panel has the room; otherwise the rows alone.
            Row {
                id: tiles
                anchors { left: parent.left; right: parent.right; top: rows.bottom; topMargin: 14 }
                visible: parent.height >= rows.height + 14 + 66
                spacing: 10
                readonly property real w: (width - 20) / 3
                StatTile { width: tiles.w; k: "Uptime"; v: Math.floor(bridge.uptimeSeconds / 60) + " min"; tint: Theme.accent }
                StatTile { width: tiles.w; k: "Schema"; v: bridge.statusInfo.schema_version !== undefined ? "v" + bridge.statusInfo.schema_version : "?"; tint: Theme.accentSoft }
                StatTile { width: tiles.w; k: "Session"; v: bridge.sessionId ? bridge.sessionId.substring(0, 8) : "new"; tint: Theme.teal }
            }
            Text { anchors { left: parent.left; bottom: parent.bottom } visible: parent.height >= rows.height + 14 + 66 + 34
                text: "CPU, RAM and network are not measured by this build."; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
        }
    }
    Component {
        id: workflowPanel
        Column {
            spacing: 6
            Item { width: parent.width; height: 26
                Row { spacing: 9; anchors.verticalCenter: parent.verticalCenter
                    Rectangle { width: 7; height: 7; radius: 3.5; anchors.verticalCenter: parent.verticalCenter; color: Theme.stateColor(bridge.coreState)
                        Rectangle { anchors.centerIn: parent; width: 15; height: 15; radius: 7.5; color: parent.color; opacity: 0.25 } }
                    Text { text: Theme.stateLabel(bridge.coreState); color: Theme.stateColor(bridge.coreState)
                        font { family: Theme.font; pixelSize: 11; letterSpacing: 1.8; weight: Font.DemiBold } }
                }
                Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: bridge.task.task_id || ""; color: Theme.textDim; font { family: Theme.mono; pixelSize: 10 } }
            }
            Strip { glyph: "◇"; tint: Theme.accent; k: "Status"; v: (bridge.task.status || "—") + (bridge.task.stage ? " / " + bridge.task.stage : "") }
            Strip { glyph: "▷"; tint: Theme.accent; k: "Run"; v: bridge.task.run_status || "—" }
            Strip { glyph: "⌘"; tint: Theme.teal; k: "Connector"; v: bridge.task.connector || "—" }
            Strip { glyph: "✦"; tint: Theme.teal; k: "Capability"; v: bridge.task.capability || "—" }
            Strip { glyph: "➶"; tint: Theme.violet; k: "Destination"; v: bridge.task.destination || "—" }
            Strip { glyph: "◈"; tint: Theme.violet; k: "Model"; v: (bridge.task.provider || "—") + (bridge.task.model ? " / " + bridge.task.model : "") }
            Strip { glyph: "!"; k: "Error"; v: bridge.task.error_code || "none"
                tint: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.success)
                vColor: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.textLabel) }
        }
    }
    Component {
        id: activityPanel
        ListView {
            clip: true
            model: bridge.activity
            spacing: 0
            delegate: Item {
                width: ListView.view.width; height: 54
                readonly property color tint: modelData.kind === "approval" ? Theme.warning : modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "scheduled" ? Theme.success : Theme.accent
                readonly property string glyph: modelData.kind === "approval" ? "⚑" : modelData.kind === "unknown" ? "?" : modelData.kind === "error" ? "!" : modelData.kind === "scheduled" ? "⟳" : "◈"
                IconBox { x: 0; anchors.verticalCenter: parent.verticalCenter; width: 34; height: 34; glyph: parent.glyph; tint: parent.tint; glyphSize: 14 }
                Column { anchors.left: parent.left; anchors.leftMargin: 46; anchors.right: timeText.left; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                    Text { text: modelData.text; color: Theme.text; width: parent.width; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 13 } }
                    Text { text: modelData.kind === "approval" ? "authorization" : modelData.kind === "unknown" ? "outcome unknown" : modelData.kind === "error" ? "error" : modelData.kind === "scheduled" ? "scheduled" : "interface"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                }
                Text { id: timeText; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: modelData.time; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
                Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: Theme.border; opacity: 0.55 }
            }
        }
    }
    Component {
        id: automationsPanel
        Column {
            spacing: 6
            Text { visible: bridge.jobs.length === 0; text: "No jobs defined."; color: Theme.textDim; font { family: Theme.font; pixelSize: 12 } }
            Repeater {
                model: bridge.jobs.slice(0, 5)
                Strip { glyph: "⟳"; tint: modelData.enabled ? Theme.success : Theme.textDim
                    k: modelData.readable ? (modelData.label.length > 12 ? modelData.label.substring(0, 12) + "…" : modelData.label) : "(unreadable)"
                    v: modelData.next_due_at_utc ? "next " + modelData.next_due_at_utc.substring(5, 16) + "Z" : "—" }
            }
        }
    }
}
