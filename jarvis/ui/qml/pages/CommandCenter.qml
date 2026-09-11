import QtQuick
import "../theme"
import "../components"

// The dominant experience: greeting, the conversation/task stream, the
// clarification choices when JARVIS needs one detail, and the composer.
GlassPanel {
    id: page
    padding: 38

    // A short HUD bracket outside the top-right corner, as in the reference chrome.
    Rectangle { x: parent.width - 120; y: -7; width: 120; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.8 }
    Rectangle { x: parent.width + 6; y: -7; width: 1; height: 90; color: Theme.hud; opacity: Theme.hudOpacity * 0.8 }
    Rectangle { x: parent.width + 4; y: 82; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.4 }

    Column {
        id: heading
        anchors { left: parent.left; right: parent.right; top: parent.top; topMargin: -8 }
        spacing: 4
        Text {
            text: (function() { var h = new Date().getHours(); return h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening." })()
            color: Theme.text
            font { family: Theme.font; pixelSize: Theme.fsHero; weight: Font.Medium; letterSpacing: -0.2 }
        }
        Text {
            text: bridge.configRefused ? "Configuration refused - execution is off until it is repaired."
                : !bridge.executionEnabled ? "Execution is disabled. JARVIS will not start work until it is re-enabled."
                : "Here to help you think, plan, and get things done."
            color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.textMuted
            font { family: Theme.font; pixelSize: Theme.fsLead }
        }
    }

    FeedView {
        anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: quick.top; topMargin: 26; bottomMargin: 16 }
    }

    // Quick actions: real entry points with real counts. The first only
    // prefills the composer - nothing is sent until the user sends it.
    Row {
        id: quick
        anchors { left: parent.left; right: parent.right; bottom: choices.top; bottomMargin: 18 }
        spacing: 14
        visible: bridge.feed.length === 0 && width >= 960
        height: visible ? 78 : 0
        component QuickCard: Rectangle {
            property string title: ""; property string line1: ""; property string line2: ""; property string glyph: "◈"; property color tint: Theme.accent
            signal clicked()
            readonly property bool hovered: hover.containsMouse
            width: (quick.width - 42) / 4; height: 78; radius: 14
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
            IconBox { x: 16; y: 19; width: 40; height: 40; glyph: parent.glyph; tint: parent.tint }
            Column { x: 68; y: 15; spacing: 2
                Text { text: title; color: Theme.text; font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold } }
                Text { text: line1; color: tint; font { family: Theme.font; pixelSize: 12 } }
                Text { text: line2; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
            }
            Text { anchors.right: parent.right; anchors.rightMargin: 16; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: hovered ? tint : Theme.textMuted; font.pixelSize: 20
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
            MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.clicked() }
        }
        QuickCard { title: "Analyse this project"; line1: "Ask Claude"; line2: "read-only, approval first"; glyph: "◎"; tint: Theme.accent; onClicked: composer.text = "Bu projeyi incele." }
        QuickCard { title: "Automations"; line1: bridge.jobs.length + " job" + (bridge.jobs.length === 1 ? "" : "s"); line2: "scheduled work"; glyph: "⟳"; tint: Theme.teal; onClicked: bridge.setPage("automations") }
        QuickCard { title: "Memory"; line1: bridge.projects.length + " project" + (bridge.projects.length === 1 ? "" : "s"); line2: "explicit reference data"; glyph: "▤"; tint: Theme.warning; onClicked: bridge.setPage("memory") }
        QuickCard { title: "History"; line1: bridge.sessions.length + " session" + (bridge.sessions.length === 1 ? "" : "s"); line2: "recent tasks and outcomes"; glyph: "◷"; tint: Theme.violet; onClicked: bridge.setPage("history") }
    }

    // Backend-generated clarification choices. Each button submits EXACTLY
    // the label the backend offered; the bridge binds them to the current
    // request and drops them the moment the context changes.
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: composer.top; bottomMargin: 16 }
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
                ActionButton {
                    label: modelData.label
                    primary: false
                    enabled: !bridge.busy
                    onClicked: bridge.chooseOption(modelData.label)
                }
            }
            ActionButton { label: "Never mind"; onClicked: bridge.dismissClarification() }
        }
    }

    Composer {
        id: composer
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
    }
}
