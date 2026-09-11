import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Ctrl+K. Only real actions; each one is a bridge slot or a page.
Item {
    id: palette
    anchors.fill: parent
    property bool open: false
    property int cursor: 0
    signal killSwitchRequested()
    signal settingsRequested()
    visible: open || dim.opacity > 0
    z: 400

    readonly property var commands: [
        { label: "New task (focus composer)", run: function() { bridge.setPage("home"); palette.focusComposer() } },
        { label: "New session", run: function() { bridge.newSession(); bridge.setPage("home") } },
        { label: "Open Command Center", run: function() { bridge.setPage("home") } },
        { label: "Open Tasks", run: function() { bridge.setPage("tasks") } },
        { label: "Open Automations", run: function() { bridge.setPage("automations") } },
        { label: "Open Memory", run: function() { bridge.setPage("memory") } },
        { label: "Open History", run: function() { bridge.setPage("history") } },
        { label: "Open Settings (theme / model / effort / layout)", run: function() { palette.settingsRequested() } },
        { label: "Toggle Focus Mode", run: function() { bridge.setFocusMode(!bridge.focusMode) } },
        { label: "Toggle Edit Layout", run: function() { bridge.setEditMode(!bridge.editMode) } },
        { label: "Reset layout", run: function() { bridge.resetLayout() } },
        { label: "Layout profile: Normal", run: function() { bridge.setProfile("normal") } },
        { label: "Layout profile: Focus", run: function() { bridge.setProfile("focus") } },
        { label: "Layout profile: Monitoring", run: function() { bridge.setProfile("monitoring") } },
        { label: "Theme: Canonical Space", run: function() { bridge.setTheme("space") } },
        { label: "Theme: Dark", run: function() { bridge.setTheme("dark") } },
        { label: "Ambient effects: toggle", run: function() { bridge.setAmbientEffects(!bridge.ambientEffects) } },
        { label: "Cancel current task", run: function() { bridge.cancelTask() } },
        { label: "Kill switch", run: function() { palette.killSwitchRequested() } },
        { label: "Refresh history", run: function() { bridge.refreshHistory() } },
        { label: "Refresh automations", run: function() { bridge.refreshJobs(false) } },
    ]
    property var matches: commands.filter(function(c) { return c.label.toLowerCase().indexOf(search.text.toLowerCase()) >= 0 })
    signal focusComposer()

    function show() { open = true; search.text = ""; cursor = 0; search.forceActiveFocus() }
    function hide() { open = false }
    function runCurrent() { if (matches.length > 0) { var c = matches[Math.min(cursor, matches.length - 1)]; hide(); c.run() } }

    Rectangle {
        id: dim
        anchors.fill: parent; color: "#000000"
        opacity: palette.open ? 0.4 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.pageMs } }
        MouseArea { anchors.fill: parent; enabled: palette.open; onClicked: palette.hide() }
    }
    GlassPanel {
        raised: true
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height * 0.18
        width: Math.min(parent.width * 0.5, 640); height: 58 + list.height + 32
        opacity: palette.open ? 1 : 0
        scale: palette.open ? 1 : 0.98
        Behavior on opacity { NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        Behavior on scale { NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        padding: 16
        Column {
            width: parent.width; spacing: 10
            Rectangle { width: parent.width; height: 44; radius: Theme.radiusSmall; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.6)
                Rectangle { z: -1; anchors.fill: parent; anchors.margins: -3; radius: Theme.radiusSmall + 3; color: Theme.alpha(Theme.accent, 0.1) }
                Text { x: 14; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: Theme.accent; font.pixelSize: 18 }
                TextField { id: search; anchors.fill: parent; anchors.margins: 4; anchors.leftMargin: 30; background: Item {}  color: Theme.text; placeholderText: "Type a command…"; placeholderTextColor: Theme.textDim; font { family: Theme.font; pixelSize: 15 }
                    onTextChanged: palette.cursor = 0
                    Keys.onDownPressed: palette.cursor = Math.min(palette.cursor + 1, palette.matches.length - 1)
                    Keys.onUpPressed: palette.cursor = Math.max(palette.cursor - 1, 0)
                    Keys.onReturnPressed: palette.runCurrent()
                    Keys.onEnterPressed: palette.runCurrent()
                    Keys.onEscapePressed: palette.hide() } }
            ListView {
                id: list
                width: parent.width; height: Math.min(matches.length, 9) * 36; clip: true
                model: palette.matches
                delegate: Rectangle {
                    width: ListView.view.width; height: 36; radius: Theme.radiusSmall
                    color: index === palette.cursor ? Theme.alpha(Theme.accent, 0.14) : "transparent"
                    border.width: 1; border.color: index === palette.cursor ? Theme.alpha(Theme.accent, 0.4) : "transparent"
                    Rectangle { visible: index === palette.cursor; x: 0; y: 10; width: 2; height: 16; radius: 1; color: Theme.accent }
                    Text { anchors.left: parent.left; anchors.leftMargin: 14; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: index === palette.cursor ? Theme.accent : Theme.textLabel; font { family: Theme.font; pixelSize: 14 } }
                    MouseArea { anchors.fill: parent; hoverEnabled: true; onEntered: palette.cursor = index; onClicked: { palette.cursor = index; palette.runCurrent() } }
                }
            }
        }
    }
}
