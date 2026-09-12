import QtQuick
import "../theme"
import "../components"

// The dominant workspace (1920x1080 contract: x 245-1340, y 205-1030):
// greeting at (325, 235), the conversation flow from y 300, the task
// pipeline module, four action cards (y 825-920), the composer (y 945-1015).
// Every block is truthful: empty and ready states keep the composition,
// nothing invents actions or results.
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
            font { family: Theme.font; pixelSize: Theme.fsHero; weight: Font.Medium; letterSpacing: -0.3 }
        }
        Text {
            text: bridge.configRefused ? "Configuration refused - execution is off until it is repaired."
                : !bridge.executionEnabled ? "Execution is disabled. JARVIS will not start work until it is re-enabled."
                : "Here to help you think, plan, and get things done."
            color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.textMuted
            font { family: Theme.font; pixelSize: Theme.fsLead }
        }
    }

    // The conversation flow. While nothing has been said, JARVIS's ready
    // state occupies the first slot so the flow never starts as a void.
    Item {
        id: flow
        anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: choices.top; leftMargin: page.inset; rightMargin: 60; topMargin: 20; bottomMargin: 14 }
        Item {
            id: ready
            visible: bridge.feed.length === 0
            width: parent.width; height: 56
            Item { width: 50; height: 50
                Rectangle { anchors.fill: parent; radius: 25; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.45) }
                Rectangle { anchors.centerIn: parent; width: 40; height: 40; radius: 20; color: "transparent"; border.width: 2; border.color: Theme.accent; opacity: 0.8 }
                Rectangle { anchors.centerIn: parent; width: 44; height: 44; radius: 22; color: "transparent"; border.width: 4; border.color: Theme.accent; opacity: 0.16 }
                Rectangle { anchors.centerIn: parent; width: 26; height: 26; radius: 13
                    gradient: Gradient { GradientStop { position: 0; color: Qt.lighter(Theme.accent, 1.2) } GradientStop { position: 0.55; color: Theme.bg1 } GradientStop { position: 1; color: Theme.bg0 } }
                    border.width: 1; border.color: Theme.accent }
                Rectangle { anchors.centerIn: parent; width: 64; height: 64; radius: 32; color: Theme.accent; opacity: 0.08; z: -1 }
            }
            Column { x: 68; y: 2; spacing: 6; width: parent.width - 68
                Row { spacing: 12
                    Text { text: "JARVIS"; color: Theme.accent; font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold; letterSpacing: 1.6 } }
                    Text { text: bridge.configRefused ? "unavailable" : (bridge.executionEnabled ? "ready" : "standing by"); color: Theme.textDim; font { family: Theme.font; pixelSize: Theme.fsMeta } }
                }
                Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font { family: Theme.font; pixelSize: Theme.fsBody }
                    text: bridge.configRefused ? "Configuration refused. Nothing will run until it is repaired."
                        : !bridge.executionEnabled ? "Execution is disabled. I will not start work until you re-enable it."
                        : "Waiting for your request. I will understand it, propose exactly one action, and ask before anything runs." }
            }
        }
        FeedView {
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: ready.visible ? ready.bottom : parent.top; topMargin: ready.visible ? 18 : 0 }
        }
    }

    // Backend-generated clarification choices. Each button submits EXACTLY
    // the label the backend offered; the bridge binds them to the current
    // request and drops them the moment the context changes.
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: pipeline.top; leftMargin: page.inset; rightMargin: 60; bottomMargin: visible ? 16 : 0 }
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

    // The task pipeline: the four real stages every JARVIS task passes,
    // read from the live task and core state. Empty = "No active task".
    Rectangle {
        id: pipeline
        anchors { left: parent.left; right: parent.right; bottom: quick.top; leftMargin: page.inset; rightMargin: 60; bottomMargin: visible ? 16 : 0 }
        // Full (with stage notes) while the flow is empty; compact once a
        // conversation needs the room. Reflows within the motion contract.
        readonly property bool compact: bridge.feed.length > 0
        // Short workspaces (windowed use) give the flow the room instead.
        visible: page.height >= 700
        height: !visible ? 0 : compact ? 158 : 222; radius: 12
        Behavior on height { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
        color: Theme.surfaceDeep; border.width: 1; border.color: Theme.border
        Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 11
            gradient: Gradient { GradientStop { position: 0; color: Theme.sheen } GradientStop { position: 1; color: "transparent" } } }
        Rectangle { x: 14; y: 0; height: 1; width: 160
            gradient: Gradient { orientation: Gradient.Horizontal; GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0.6) } GradientStop { position: 1; color: "transparent" } } }

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

        Item { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 16; topMargin: 13 } height: 18
            Row { spacing: 10; anchors.verticalCenter: parent.verticalCenter
                Rectangle { width: 6; height: 6; radius: 3; color: pipeline.hasTask ? Theme.stateColor(pipeline.cs) : Theme.textDim; anchors.verticalCenter: parent.verticalCenter }
                Text { text: "TASK PIPELINE"; color: Theme.textLabel; font { family: Theme.font; pixelSize: 10; letterSpacing: 2.4; weight: Font.DemiBold } } }
            Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                text: pipeline.hasTask ? ((bridge.task.status || "") + "  ·  " + bridge.task.task_id) : "No active task"
                color: pipeline.hasTask ? Theme.textLabel : Theme.textDim; font { family: Theme.mono; pixelSize: 10 } }
        }
        Column {
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 16; topMargin: 42 }
            spacing: 4
            Repeater {
                model: [
                    { name: "Understanding your request", note: "Your words become one structured proposal; the model never chooses a destination." },
                    { name: "Authorization", note: "Every external action stops here until you approve exactly what is shown." },
                    { name: "Execution", note: "One connector, one capability, one granted destination." },
                    { name: "Outcome", note: "Completed, failed, or explicitly unknown - an unknown outcome is never replayed." },
                ]
                Item {
                    width: parent.width; height: pipeline.compact ? 22 : 38
                    readonly property string s: pipeline.stage(index)
                    readonly property color c: pipeline.stageColor(s)
                    Rectangle { width: 14; height: 14; radius: 7; y: 4
                        color: s === "done" || s === "denied" ? c : "transparent"; border.width: 1.2; border.color: s === "pending" || s === "skipped" ? Theme.alpha(Theme.textDim, 0.8) : c
                        Text { visible: s === "done" || s === "denied"; anchors.centerIn: parent; text: s === "denied" ? "✕" : "✓"; color: "#04101c"; font { pixelSize: 9; weight: Font.Bold } }
                        Rectangle { visible: s === "running" || s === "waiting"; anchors.centerIn: parent; width: 6; height: 6; radius: 3; color: c
                            SequentialAnimation on opacity { running: (s === "running" || s === "waiting") && Theme.ambient; loops: Animation.Infinite
                                NumberAnimation { to: 0.3; duration: 700 } NumberAnimation { to: 1; duration: 700 } } }
                    }
                    Column { x: 26; y: 1; spacing: 1; width: parent.width - 150
                        Text { text: modelData.name; color: s === "pending" ? Theme.textLabel : Theme.text; font { family: Theme.font; pixelSize: 13 } }
                        Text { visible: !pipeline.compact; text: modelData.note; color: Theme.textDim; width: parent.width; elide: Text.ElideRight; font { family: Theme.font; pixelSize: 11 } }
                    }
                    Text { anchors.right: parent.right; y: 3; text: pipeline.stageText(s); color: s === "pending" ? Theme.textDim : c; font { family: Theme.font; pixelSize: 12 } }
                }
            }
        }
    }

    // Action cards: four real surfaces with real counts (y 825-920).
    Row {
        id: quick
        anchors { left: parent.left; right: parent.right; bottom: composer.top; leftMargin: page.inset; rightMargin: 5; bottomMargin: visible ? 28 : 0 }
        spacing: 13
        visible: width >= 900 && page.height >= 560
        height: visible ? 92 : 0
        component QuickCard: Rectangle {
            property string title: ""; property string line1: ""; property string line2: ""; property string glyph: "◈"; property color tint: Theme.accent
            signal clicked()
            readonly property bool hovered: hover.containsMouse
            width: (quick.width - 39) / 4; height: 92; radius: 14
            color: hovered ? Theme.surfaceRaised : Theme.surface; border.width: 1; border.color: hovered ? Theme.alpha(tint, 0.55) : Theme.border
            scale: hover.pressed ? 0.985 : 1
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
            Behavior on scale { NumberAnimation { duration: 90 } }
            Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 4; radius: 14; color: "#000000"; opacity: 0.28 }
            Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 13
                gradient: Gradient { GradientStop { position: 0; color: Theme.sheen } GradientStop { position: 1; color: "transparent" } } }
            Rectangle { x: 14; y: 0; height: 1; width: 90
                gradient: Gradient { orientation: Gradient.Horizontal; GradientStop { position: 0; color: Theme.alpha(tint, hovered ? 0.9 : 0.55) } GradientStop { position: 1; color: "transparent" } } }
            IconBox { x: 18; y: 24; width: 44; height: 44; glyph: parent.glyph; tint: parent.tint }
            Column { x: 76; y: 21; spacing: 2
                Text { text: title; color: Theme.text; font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold } }
                Text { text: line1; color: tint; font { family: Theme.font; pixelSize: 12 } }
                Text { text: line2; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
            }
            Text { anchors.right: parent.right; anchors.rightMargin: 18; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: hovered ? tint : Theme.textMuted; font.pixelSize: 22
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
            MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.clicked() }
        }
        QuickCard { title: "Tasks"; line1: bridge.taskLive ? "1 active" : "0 active"; line2: bridge.sessions.length + " session" + (bridge.sessions.length === 1 ? "" : "s"); glyph: "◇"; tint: Theme.accent; onClicked: bridge.setPage("tasks") }
        QuickCard { title: "Automations"; line1: bridge.jobs.length + " job" + (bridge.jobs.length === 1 ? "" : "s"); line2: "scheduled work"; glyph: "⟳"; tint: Theme.teal; onClicked: bridge.setPage("automations") }
        QuickCard { title: "Memory"; line1: bridge.projects.length + " project" + (bridge.projects.length === 1 ? "" : "s"); line2: "explicit reference data"; glyph: "▤"; tint: Theme.warning; onClicked: bridge.setPage("memory") }
        QuickCard { title: "History"; line1: bridge.sessions.length + " session" + (bridge.sessions.length === 1 ? "" : "s"); line2: "recent tasks and outcomes"; glyph: "◷"; tint: Theme.violet; onClicked: bridge.setPage("history") }
    }

    Composer {
        id: composer
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: 30; bottomMargin: -8 }
    }
}
