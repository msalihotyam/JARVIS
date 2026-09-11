import QtQuick
import "../theme"
import "../components"

// The dominant experience: greeting, the conversation/task stream, the
// clarification choices when JARVIS needs one detail, and the composer.
GlassPanel {
    id: page
    padding: 30

    Column {
        id: heading
        anchors { left: parent.left; right: parent.right; top: parent.top }
        spacing: 4
        Text {
            text: (function() { var h = new Date().getHours(); return h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening." })()
            color: Theme.text
            font { family: Theme.font; pixelSize: Theme.fsHero; weight: Font.Light }
        }
        Text {
            text: bridge.configRefused ? "Configuration refused - execution is off until it is repaired."
                : !bridge.executionEnabled ? "Execution is disabled. JARVIS will not start work until it is re-enabled."
                : "Tell JARVIS what you need. Every action still passes the usual approval."
            color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.textMuted
            font { family: Theme.font; pixelSize: 15 }
        }
    }

    FeedView {
        anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: quick.top; topMargin: 22; bottomMargin: 12 }
    }

    // Quick actions: real entry points with real counts. The first only
    // prefills the composer - nothing is sent until the user sends it.
    Row {
        id: quick
        anchors { left: parent.left; right: parent.right; bottom: choices.top; bottomMargin: 14 }
        spacing: 12
        visible: bridge.feed.length === 0
        height: visible ? 74 : 0
        component QuickCard: Rectangle {
            property string title: ""; property string line1: ""; property string line2: ""; property string glyph: "◈"
            signal clicked()
            width: (quick.width - 36) / 4; height: 74; radius: Theme.radiusSmall
            color: hover.containsMouse ? Theme.surfaceRaised : Theme.surface; border.width: 1; border.color: hover.containsMouse ? Theme.borderStrong : Theme.border
            Behavior on color { ColorAnimation { duration: 120 } }
            Rectangle { x: 14; y: 17; width: 40; height: 40; radius: 10; color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14); border.width: 1; border.color: Theme.border
                Text { anchors.centerIn: parent; text: glyph; color: Theme.accent; font.pixelSize: 18 } }
            Column { x: 66; y: 14; spacing: 2
                Text { text: title; color: Theme.text; font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold } }
                Text { text: line1; color: Theme.accent; font { family: Theme.font; pixelSize: 12 } }
                Text { text: line2; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
            }
            Text { anchors.right: parent.right; anchors.rightMargin: 14; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: Theme.textMuted; font.pixelSize: 18 }
            MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.clicked() }
        }
        QuickCard { title: "Analyse this project"; line1: "Ask Claude"; line2: "read-only, approval first"; glyph: "◎"; onClicked: composer.text = "Bu projeyi incele." }
        QuickCard { title: "Automations"; line1: bridge.jobs.length + " job" + (bridge.jobs.length === 1 ? "" : "s"); line2: "scheduled work"; glyph: "⟳"; onClicked: bridge.setPage("automations") }
        QuickCard { title: "Memory"; line1: bridge.projects.length + " project" + (bridge.projects.length === 1 ? "" : "s"); line2: "explicit reference data"; glyph: "▤"; onClicked: bridge.setPage("memory") }
        QuickCard { title: "History"; line1: bridge.sessions.length + " session" + (bridge.sessions.length === 1 ? "" : "s"); line2: "recent tasks and outcomes"; glyph: "◷"; onClicked: bridge.setPage("history") }
    }

    // Backend-generated clarification choices. Each button submits EXACTLY
    // the label the backend offered; the bridge binds them to the current
    // request and drops them the moment the context changes.
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: composer.top; bottomMargin: 14 }
        spacing: 8
        visible: bridge.clarification.choices !== undefined && bridge.clarification.choices.length > 0
        height: visible ? implicitHeight : 0
        Text { text: "CHOOSE ONE"; color: Theme.accentSoft; font { family: Theme.font; pixelSize: 10; letterSpacing: 2; weight: Font.DemiBold } }
        Flow {
            width: parent.width
            spacing: 8
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
