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
            Text { text: "EDIT LAYOUT"; color: Theme.accent; anchors.verticalCenter: parent.verticalCenter
                font { family: Theme.font; pixelSize: 10; letterSpacing: 2; weight: Font.DemiBold } }
            Repeater {
                model: bridge.hiddenPanels
                Rectangle {
                    height: 26; width: chipText.width + 22; radius: 13
                    color: Theme.surface; border.width: 1; border.color: Theme.border
                    anchors.verticalCenter: parent.verticalCenter
                    Text { id: chipText; anchors.centerIn: parent; text: "+ " + modelData; color: Theme.text; font { family: Theme.font; pixelSize: 11 } }
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setPanelVisible(modelData, true) }
                }
            }
        }
        ActionButton { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; label: "Reset layout"; onClicked: bridge.resetLayout() }
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
                title: ({ system: "System status", workflow: "Active workflow", activity: "Live activity", automations: "Automations" })[modelData.id]
                border.color: bridge.editMode ? Theme.accent : Theme.border
                headerRight: [
                    Item {
                        height: 22; width: handles.width
                        Row {
                            id: handles
                            spacing: 4
                            visible: bridge.editMode
                            IconButton { glyph: "▲"; tip: "Move up"; width: 22; height: 22; onClicked: bridge.movePanel(modelData.id, -1) }
                            IconButton { glyph: "▼"; tip: "Move down"; width: 22; height: 22; onClicked: bridge.movePanel(modelData.id, 1) }
                            IconButton { glyph: "⊙"; tip: modelData.pinned ? "Unpin" : "Pin"; active: modelData.pinned; width: 22; height: 22; onClicked: bridge.setPanelPinned(modelData.id, !modelData.pinned) }
                            IconButton { glyph: "✕"; tip: "Hide"; width: 22; height: 22; onClicked: bridge.setPanelVisible(modelData.id, false) }
                        }
                        Text { visible: !bridge.editMode && modelData.pinned; text: "PINNED"; color: Theme.textDim; font { family: Theme.font; pixelSize: 9; letterSpacing: 1.5 } }
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
    component KeyValue: Row {
        property string k: ""; property string v: ""; property color vColor: Theme.text
        spacing: 10; width: parent.width
        Text { text: k; color: Theme.textMuted; width: 110; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 12 } }
        Text { text: v; color: vColor; width: parent.width - 120; elide: Text.ElideMiddle; font { family: Theme.font; pixelSize: 12 } }
    }
    component Dot: Rectangle { property color c: Theme.success; width: 8; height: 8; radius: 4; color: c }

    Component {
        id: systemPanel
        Column {
            spacing: 6
            Row { spacing: 8
                Dot { c: bridge.configRefused ? Theme.danger : (bridge.executionEnabled ? Theme.success : Theme.danger); anchors.verticalCenter: parent.verticalCenter }
                Text { text: bridge.configRefused ? "CONFIGURATION REFUSED" : (bridge.executionEnabled ? "EXECUTION ENABLED" : "EXECUTION DISABLED")
                    color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success
                    font { family: Theme.font; pixelSize: 11; letterSpacing: 1.5; weight: Font.DemiBold } }
            }
            KeyValue { k: "Permission mode"; v: bridge.config.permission_mode || "—" }
            KeyValue { k: "Provider"; v: "claude (Claude Code CLI)" }
            KeyValue { k: "Model"; v: bridge.config.claude_model || "CLI default" }
            KeyValue { k: "Effort"; v: bridge.config.claude_effort || "CLI default" }
            KeyValue { k: "Paid API"; v: bridge.config.paid_api_enabled ? "enabled" : "off (subscription route only)" }
            KeyValue { k: "SQLite schema"; v: bridge.statusInfo.schema_version !== undefined ? String(bridge.statusInfo.schema_version) : "unknown" }
            KeyValue { k: "Database"; v: bridge.config.memory_database || "—" }
            KeyValue { k: "Session"; v: bridge.sessionId || "(new)" }
            KeyValue { k: "Interface uptime"; v: Math.floor(bridge.uptimeSeconds / 60) + " min" }
            Text { text: "CPU, RAM and network figures are not measured by this build and are not shown."; color: Theme.textDim; wrapMode: Text.Wrap; width: parent.width; font { family: Theme.font; pixelSize: 10 } }
        }
    }
    Component {
        id: workflowPanel
        Column {
            spacing: 6
            Item { width: parent.width; height: 34
                Row { spacing: 8; anchors.verticalCenter: parent.verticalCenter
                    Dot { c: Theme.stateColor(bridge.coreState); anchors.verticalCenter: parent.verticalCenter }
                    Text { text: Theme.stateLabel(bridge.coreState); color: Theme.stateColor(bridge.coreState)
                        font { family: Theme.font; pixelSize: 11; letterSpacing: 1.5; weight: Font.DemiBold } }
                }
                Row { spacing: 6; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                    ActionButton { label: "Cancel"; danger: true; enabled: bridge.taskLive; onClicked: bridge.cancelTask() }
                    ActionButton { label: "New session"; enabled: !bridge.busy; onClicked: bridge.newSession() }
                }
            }
            KeyValue { k: "Task"; v: bridge.task.task_id || "—" }
            KeyValue { k: "Status"; v: (bridge.task.status || "—") + (bridge.task.stage ? " / " + bridge.task.stage : "") }
            KeyValue { k: "Run"; v: bridge.task.run_status || "—" }
            KeyValue { k: "Connector"; v: bridge.task.connector || "—" }
            KeyValue { k: "Capability"; v: bridge.task.capability || "—" }
            KeyValue { k: "Destination"; v: bridge.task.destination || "—" }
            KeyValue { k: "Provider / model"; v: (bridge.task.provider || "—") + (bridge.task.model ? " / " + bridge.task.model : "") }
            KeyValue { k: "Error"; v: bridge.task.error_code || "none"; vColor: bridge.task.error_code === "execution_unknown_outcome" ? Theme.unknownEdge : (bridge.task.error_code ? Theme.danger : Theme.text) }
        }
    }
    Component {
        id: activityPanel
        ListView {
            clip: true
            model: bridge.activity
            spacing: 6
            delegate: Row {
                width: ListView.view.width; spacing: 10
                Dot { c: modelData.kind === "approval" ? Theme.warning : modelData.kind === "unknown" ? Theme.unknownEdge : modelData.kind === "error" ? Theme.danger : modelData.kind === "scheduled" ? Theme.success : Theme.accent; anchors.verticalCenter: parent.verticalCenter }
                Column { width: parent.width - 80
                    Text { text: modelData.text; color: Theme.text; width: parent.width; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 12 } } }
                Text { text: modelData.time; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } anchors.verticalCenter: parent.verticalCenter }
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
                Row { spacing: 10; width: parent.width
                    Dot { c: modelData.enabled ? Theme.success : Theme.textDim; anchors.verticalCenter: parent.verticalCenter }
                    Text { text: modelData.readable ? modelData.label : "(unreadable definition)"; color: Theme.text; width: parent.width - 150; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 12 } }
                    Text { text: modelData.next_due_at_utc ? modelData.next_due_at_utc.substring(5, 16) + "Z" : "—"; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                }
            }
        }
    }
}
