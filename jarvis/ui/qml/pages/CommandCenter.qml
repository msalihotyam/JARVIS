import QtQuick
import "../theme"
import "../components"

// Canonical/Dark home. The master's 76 px text inset, nested conversation
// rhythm and protected 70 px composer are independent of the side panels.
GlassPanel {
    id: page
    objectName: "canonicalWorkspace"
    level: 1; padding: 18; brackets: true
    readonly property int inset: width >= 900 ? 58 : 12
    Column {
        id: heading
        anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: page.inset; topMargin: 3 }
        spacing: 4
        Text { text: (function() { var h = new Date().getHours(); return (h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening") + ", " + bridge.operatorName + "." })(); color: "#b6def2"; font { family: Theme.font; pixelSize: page.width >= 900 ? 32 : 27; weight: Font.DemiBold } }
        Text { width: parent.width; wrapMode: Text.Wrap; text: bridge.configRefused ? "Configuration refused — execution is off." : !bridge.executionEnabled ? "Execution is disabled. Re-enable it explicitly when ready." : "Here to help you think, plan, and get things done."; color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.textLabel; font { family: Theme.font; pixelSize: 17 } }
    }
    Item {
        id: flow
        anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: choices.top; leftMargin: page.inset; rightMargin: 70; topMargin: 24; bottomMargin: 15 }
        Item { id: ready; visible: bridge.feed.length === 0; width: parent.width; height: 88
            IconBox { glyph: "◎"; width: 50; height: 50; tint: Theme.accent; round: true }
            Column { x: 68; y: 4; width: parent.width - 68; spacing: 8
                Text { text: "JARVIS"; color: Theme.accent; font { family: Theme.font; pixelSize: 14; letterSpacing: 1.4; weight: Font.DemiBold } }
                Text { width: parent.width; wrapMode: Text.Wrap; text: bridge.configRefused ? "Configuration needs attention before any work can start." : !bridge.executionEnabled ? "Standing by. No new work will start while execution is disabled." : "Waiting for your request. I’ll help you plan the next step and ask before an action needs your permission."; color: Theme.text; font { family: Theme.font; pixelSize: 16 } }
            }
        }
        FeedView {
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: ready.visible ? ready.bottom : parent.top; topMargin: ready.visible ? 18 : 0 }
            pipelineEnabled: true
            footer: Component { Item { width: ListView.view ? ListView.view.width : flow.width; height: bridge.feed.length === 0 ? emptyPipeline.height + 8 : 0
                StagePipeline { id: emptyPipeline; x: 68; width: parent.width - 68; visible: bridge.feed.length === 0 }
            } }
        }
    }
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: quick.top; leftMargin: page.inset + 68; rightMargin: 36; bottomMargin: visible ? 14 : 0 }
        visible: !!bridge.clarification.choices && bridge.clarification.choices.length > 0
        height: visible ? implicitHeight : 0; spacing: 10
        Text { text: "CHOOSE ONE"; color: Theme.accentSoft; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        Flow { width: parent.width; spacing: 10
            Repeater { model: bridge.clarification.choices || []; ActionButton { label: modelData.label; enabled: !bridge.busy; onClicked: bridge.chooseOption(modelData.label) } }
            ActionButton { label: "Never mind"; onClicked: bridge.dismissClarification() }
        }
    }
    Row {
        id: quick
        anchors { left: parent.left; right: parent.right; bottom: composer.top; leftMargin: 36; rightMargin: 14; bottomMargin: visible ? 26 : 0 }
        spacing: 16; visible: width >= 780 && page.height >= 620; height: visible ? 92 : 0
        Repeater {
            model: [
                {title:"Tasks",line:bridge.taskLive ? "1 active" : "0 active",note:bridge.sessions.length + " sessions",glyph:"▤",tone:"#058bf0",page:"tasks"},
                {title:"Automations",line:bridge.jobs.length + " jobs",note:"Scheduled work",glyph:"⟳",tone:"#00a67d",page:"automations"},
                {title:"Memory",line:bridge.projects.length + " projects",note:"Reference data",glyph:"◉",tone:"#b78624",page:"memory"},
                {title:"History",line:bridge.sessions.length + " sessions",note:"Tasks and outcomes",glyph:"◷",tone:"#7547ba",page:"history"}
            ]
            Rectangle {
                width: (quick.width - 48) / 4; height: 92; radius: 9
                color: hover.containsMouse ? Theme.surfaceRaised : Theme.surfaceDeep; border.width: 1; border.color: hover.containsMouse ? Theme.borderStrong : Theme.border
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                IconBox { x: 15; anchors.verticalCenter: parent.verticalCenter; width: 42; height: 44; glyph: modelData.glyph; tint: modelData.tone; solid: true; glyphSize: 21 }
                Column { x: 70; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 96; spacing: 3
                    Text { text: modelData.title; color: Theme.text; font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold } }
                    Text { text: modelData.line; color: Qt.lighter(modelData.tone, 1.5); font { family: Theme.font; pixelSize: 13 } }
                    Text { width: parent.width; elide: Text.ElideRight; text: modelData.note; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                }
                Text { anchors.right: parent.right; anchors.rightMargin: 13; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: Theme.accent; font.pixelSize: 25 }
                MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setPage(modelData.page) }
            }
        }
    }
    Composer { id: composer; objectName: "canonicalComposer"; height: 70
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: page.width >= 900 ? 36 : 12; rightMargin: 8 }
    }
}
