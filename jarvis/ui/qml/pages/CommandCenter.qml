import QtQuick
import "../theme"
import "../components"

// The dominant workspace (1920x1080 contract: x 245-1340, y 205-1030):
// greeting at (325, 235), the conversation flow from y 300 with the task
// module following the last message like the reference's step list, four
// action cards (y 825-920), the composer (y 945-1015). Every block is
// truthful: empty and ready states keep the composition, nothing invents
// actions or results.
GlassPanel {
    id: page
    level: 1
    padding: 30
    readonly property int inset: 50      // content column starts 80 px in

    // HUD bracket outside the top-right corner, and a foot accent bottom-left.
    Rectangle { x: parent.width - 120; y: -7; width: 120; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.8 }
    Rectangle { x: parent.width + 6; y: -7; width: 1; height: 90; color: Theme.hud; opacity: Theme.hudOpacity * 0.8 }
    Rectangle { x: parent.width + 4; y: 82; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.4 }
    Rectangle { x: -7; y: parent.height - 110; width: 1; height: 110; color: Theme.hud; opacity: Theme.hudOpacity * 0.6 }
    Rectangle { x: -7; y: parent.height + 6; width: 90; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.6 }

    Column {
        id: heading
        anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: page.inset; topMargin: -6 }
        spacing: 2
        Text {
            text: (function() { var h = new Date().getHours(); return (h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening") + ", " + bridge.operatorName + "." })()
            color: Theme.text
            font { family: Theme.font; pixelSize: 36; weight: Font.DemiBold; letterSpacing: -0.2 }
        }
        Text {
            text: bridge.configRefused ? "Configuration refused - execution is off until it is repaired."
                : !bridge.executionEnabled ? "Execution is disabled. JARVIS will not start work until it is re-enabled."
                : "Here to help you think, plan, and get things done."
            color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.textLabel
            font { family: Theme.font; pixelSize: Theme.fsLead }
        }
    }

    // The conversation flow. While nothing has been said, JARVIS's ready
    // state occupies the first slot so the flow never starts as a void; the
    // task module follows the last entry as the ListView footer.
    Item {
        id: flow
        anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: choices.top; leftMargin: page.inset; rightMargin: 26; topMargin: 22; bottomMargin: 14 }
        Item {
            id: ready
            visible: bridge.feed.length === 0
            width: parent.width; height: 56
            Item { width: 50; height: 50
                Rectangle { anchors.centerIn: parent; width: 60; height: 60; radius: 30; color: Theme.accent; opacity: 0.07 }
                Rectangle { anchors.fill: parent; radius: 25; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.35) }
                Repeater { model: 6; Rectangle { width: 2.5; height: 2.5; radius: 1.25; color: Theme.accent; opacity: 0.75; x: 23.75; y: -1; transform: Rotation { origin.x: 1.25; origin.y: 26; angle: index * 60 + 15 } } }
                Rectangle { anchors.centerIn: parent; width: 40; height: 40; radius: 20; color: "transparent"; border.width: 7; border.color: Theme.accent; opacity: 0.22 }
                Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: Theme.alpha(Theme.bg0, 0.6); border.width: 3; border.color: Qt.lighter(Theme.accent, 1.08) }
            }
            Column { x: 68; y: 2; spacing: 7; width: parent.width - 68
                Row { spacing: 12
                    Text { text: "JARVIS"; color: Theme.accent; font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold; letterSpacing: 1.6 } }
                    Text { anchors.baseline: parent.children[0].baseline; text: bridge.configRefused ? "unavailable" : (bridge.executionEnabled ? "ready" : "standing by"); color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
                }
                Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font { family: Theme.font; pixelSize: 16 }
                    text: bridge.configRefused ? "Configuration refused. Nothing will run until it is repaired."
                        : !bridge.executionEnabled ? "Execution is disabled. I will not start work until you re-enable it."
                        : "Waiting for your request. I will understand it, propose exactly one action, and ask before anything runs." }
            }
        }
        FeedView {
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: ready.visible ? ready.bottom : parent.top; topMargin: ready.visible ? 18 : 0 }
            footer: pipelineModule
        }
    }

    // The task pipeline: the four real stages every JARVIS task passes,
    // read from the live task and core state. Empty = "No active task".
    // Rendered as the flow's footer, indented to the text column.
    Component {
        id: pipelineModule
        Item {
            width: ListView.view ? ListView.view.width : flow.width
            height: pipeline.visible ? pipeline.height + 22 : 0
            Rectangle {
                id: pipeline
                x: 68; y: 22; width: parent.width - 68
                visible: page.height >= 640
                height: 20 + rows.height + 16; radius: 12
                color: Theme.alpha(Theme.bg0, 0.34); border.width: 1; border.color: Theme.alpha(Theme.accent, 0.14)
                Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 11
                    gradient: Gradient { GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0.035) } GradientStop { position: 1; color: "transparent" } } }

                readonly property bool hasTask: !!bridge.task.task_id
                readonly property string cs: bridge.coreState
                function stage(i) {
                    var t = bridge.task, has = hasTask, cs = pipeline.cs
                    if (has && t.status === "rejected") return i === 0 ? "done" : i === 1 ? "denied" : "skipped"
                    if (has && t.status === "cancelled") return i === 0 ? "done" : "skipped"
                    if (i === 0) return cs === "running" && !has ? "running" : (has || cs === "awaiting_input" ? "done" : "pending")
                    if (i === 1) return cs === "awaiting_approval" ? "waiting" : (has && (t.run_status || cs === "completed" || cs === "error" || cs === "unknown") ? "done" : "pending")
                    if (i === 2) return cs === "running" && has ? "running" : cs === "completed" ? "done" : cs === "error" ? "error" : cs === "unknown" ? "unknown" : "pending"
                    return cs === "completed" ? "done" : cs === "error" ? "error" : cs === "unknown" ? "unknown" : "pending"
                }
                function stageText(s) { return { pending: "Pending", waiting: "Awaiting you", running: "Running", done: "Completed", error: "Failed", unknown: "Outcome unknown", denied: "Denied", skipped: "Not run" }[s] }
                function stageColor(s) { return s === "done" ? Theme.success : s === "running" ? Theme.accent : s === "waiting" ? Theme.warning : s === "error" || s === "denied" ? Theme.danger : s === "unknown" ? Theme.unknownEdge : Theme.textDim }

                Text { anchors.right: parent.right; anchors.rightMargin: 16; y: 8
                    text: pipeline.hasTask ? ((bridge.task.status || "") + "  ·  " + bridge.task.task_id.substring(0, 8)) : "No active task"
                    color: Theme.textDim; font { family: Theme.mono; pixelSize: 10 } }
                Column {
                    id: rows
                    anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: 18; rightMargin: 18; topMargin: 20 }
                    spacing: 0
                    // With nothing said yet the stages carry their one-line notes,
                    // so the idle screen explains the gate instead of leaving a void.
                    readonly property bool notes: bridge.feed.length === 0
                    Repeater {
                        model: [
                            { name: "Understanding your request", note: "Your words become one structured proposal; the model never chooses a destination." },
                            { name: "Authorization", note: "Every external action stops here until you approve exactly what is shown." },
                            { name: "Execution", note: "One connector, one capability, one granted destination." },
                            { name: "Outcome", note: "Completed, failed, or explicitly unknown - an unknown outcome is never replayed." },
                        ]
                        Item {
                            width: parent.width; height: rows.notes ? 46 : 29
                            readonly property string s: pipeline.stage(index)
                            readonly property color c: pipeline.stageColor(s)
                            Rectangle { width: 16; height: 16; radius: 8; y: rows.notes ? 8 : (parent.height - 16) / 2
                                color: s === "done" || s === "denied" ? c : "transparent"; border.width: 1.5; border.color: s === "pending" || s === "skipped" ? Theme.alpha(Theme.textMuted, 0.8) : c
                                Text { visible: s === "done" || s === "denied"; anchors.centerIn: parent; text: s === "denied" ? "✕" : "✓"; color: "#04101c"; font { pixelSize: 10; weight: Font.Bold } }
                                Rectangle { visible: s === "running" || s === "waiting"; anchors.centerIn: parent; width: 6; height: 6; radius: 3; color: c
                                    SequentialAnimation on opacity { running: (s === "running" || s === "waiting") && Theme.ambient; loops: Animation.Infinite
                                        NumberAnimation { to: 0.3; duration: 700 } NumberAnimation { to: 1; duration: 700 } } }
                            }
                            Column { x: 30; y: rows.notes ? 4 : (parent.height - 21) / 2; spacing: 1; width: parent.width - 160
                                Text { text: modelData.name + (s === "pending" ? "" : "…"); color: s === "pending" || s === "skipped" ? Theme.textLabel : Theme.text; font { family: Theme.font; pixelSize: 15 } }
                                Text { visible: rows.notes; text: modelData.note; color: Theme.textMuted; width: parent.width; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 12 } }
                            }
                            Text { anchors.right: parent.right; y: rows.notes ? 6 : (parent.height - height) / 2; text: pipeline.stageText(s); color: s === "pending" || s === "skipped" ? Theme.textMuted : c; font { family: Theme.font; pixelSize: 15 } }
                        }
                    }
                }
            }
        }
    }

    // Backend-generated clarification choices. Each button submits EXACTLY
    // the label the backend offered; the bridge binds them to the current
    // request and drops them the moment the context changes.
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: quick.top; leftMargin: page.inset + 68; rightMargin: 26; bottomMargin: visible ? 16 : 0 }
        spacing: 10
        visible: bridge.clarification.choices !== undefined && bridge.clarification.choices.length > 0
        height: visible ? implicitHeight : 0
        Row { spacing: 10
            Rectangle { width: 6; height: 6; radius: 3; color: Theme.accentSoft; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "CHOOSE ONE"; color: Theme.accentSoft; font { family: Theme.font; pixelSize: 10; letterSpacing: 2.4; weight: Font.DemiBold } } }
        Flow {
            width: parent.width
            spacing: 10
            Repeater {
                model: bridge.clarification.choices || []
                ActionButton { label: modelData.label; primary: false; enabled: !bridge.busy; onClicked: bridge.chooseOption(modelData.label) }
            }
            ActionButton { label: "Never mind"; onClicked: bridge.dismissClarification() }
        }
    }

    // Action cards: four real surfaces with real counts (y 825-920).
    Row {
        id: quick
        anchors { left: parent.left; right: parent.right; bottom: composer.top; leftMargin: page.inset - 11; rightMargin: 53; bottomMargin: visible ? 26 : 0 }
        spacing: 18
        visible: width >= 900 && page.height >= 560
        height: visible ? 90 : 0
        component QuickCard: Rectangle {
            property string title: ""; property string line1: ""; property string line2: ""; property string glyph: "◈"; property color tint: Theme.accent
            signal clicked()
            readonly property bool hovered: hover.containsMouse
            width: (quick.width - 54) / 4; height: 90; radius: 14
            color: hovered ? Theme.surfaceRaised : Theme.surface; border.width: 1; border.color: hovered ? Theme.alpha(tint, 0.55) : Theme.border
            scale: hover.pressed ? 0.985 : 1
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
            Behavior on scale { NumberAnimation { duration: 90 } }
            Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 4; radius: 14; color: "#000000"; opacity: 0.28 }
            Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 13
                gradient: Gradient { GradientStop { position: 0; color: Theme.sheen } GradientStop { position: 1; color: "transparent" } } }
            Rectangle { x: 14; y: 0; height: 1; width: parent.width - 28
                gradient: Gradient { orientation: Gradient.Horizontal; GradientStop { position: 0; color: Theme.alpha(Theme.accent, hovered ? 0.9 : 0.5) } GradientStop { position: 1; color: Theme.alpha(Theme.accent, 0.08) } } }
            IconBox { x: 16; y: 24; width: 42; height: 42; glyph: parent.glyph; tint: parent.tint; solid: true; glyphSize: 19 }
            Column { x: 72; y: 15; spacing: 0
                Text { text: title; color: Theme.text; font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold } }
                Text { text: line1; color: Qt.lighter(tint, 1.35); font { family: Theme.font; pixelSize: 15 } }
                Text { text: line2; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
            }
            Text { anchors.right: parent.right; anchors.rightMargin: 18; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: hovered ? Qt.lighter(Theme.accent, 1.2) : Theme.accent; font.pixelSize: 26
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
            MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.clicked() }
        }
        QuickCard { title: "Tasks"; line1: bridge.taskLive ? "1 active" : "0 active"; line2: bridge.sessions.length + " session" + (bridge.sessions.length === 1 ? "" : "s"); glyph: "◇"; tint: "#2f7fe0"; onClicked: bridge.setPage("tasks") }
        QuickCard { title: "Automations"; line1: bridge.jobs.length + " job" + (bridge.jobs.length === 1 ? "" : "s"); line2: "scheduled work"; glyph: "⟳"; tint: "#22a877"; onClicked: bridge.setPage("automations") }
        QuickCard { title: "Memory"; line1: bridge.projects.length + " project" + (bridge.projects.length === 1 ? "" : "s"); line2: "explicit reference data"; glyph: "▤"; tint: "#c58a2a"; onClicked: bridge.setPage("memory") }
        QuickCard { title: "History"; line1: bridge.sessions.length + " session" + (bridge.sessions.length === 1 ? "" : "s"); line2: "recent tasks and outcomes"; glyph: "◷"; tint: "#7a4fd0"; onClicked: bridge.setPage("history") }
    }

    Composer {
        id: composer
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: 30; bottomMargin: -8 }
    }
}
