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
    signal lifecycleRequested()
    signal settingsRequested()
    visible: open || dim.opacity > 0
    z: 400

    readonly property var commands: [
        { label: qsTr("New task (focus composer)"), run: function() { bridge.setPage("home"); palette.focusComposer() } },
        { label: qsTr("New session"), run: function() { bridge.newSession(); bridge.setPage("home") } },
        { label: qsTr("New conversation"), run: function() { bridge.newConversation(); bridge.setPage("home") } },
        { label: qsTr("Open Command Center"), run: function() { bridge.setPage("home") } },
        { label: qsTr("Open Tasks"), run: function() { bridge.setPage("tasks") } },
        { label: qsTr("Open Automations"), run: function() { bridge.setPage("automations") } },
        { label: qsTr("Open Memory"), run: function() { bridge.setPage("memory") } },
        { label: qsTr("Open History"), run: function() { bridge.setPage("history") } },
        { label: qsTr("Open Settings (theme / model / effort / layout)"), run: function() { palette.settingsRequested() } },
        { label: qsTr("Toggle Focus Mode"), run: function() { bridge.setFocusMode(!bridge.focusMode) } },
        { label: qsTr("Toggle Edit Layout"), run: function() { bridge.setEditMode(!bridge.editMode) } },
        { label: qsTr("Reset layout"), run: function() { bridge.resetLayout() } },
        { label: qsTr("Layout profile: Normal"), run: function() { bridge.setProfile("normal") } },
        { label: qsTr("Layout profile: Focus"), run: function() { bridge.setProfile("focus") } },
        { label: qsTr("Layout profile: Monitoring"), run: function() { bridge.setProfile("monitoring") } },
        { label: qsTr("Theme: Canonical Space"), run: function() { bridge.setTheme("space") } },
        { label: qsTr("Theme: Dark"), run: function() { bridge.setTheme("dark") } },
        { label: qsTr("Ambient effects: toggle"), run: function() { bridge.setAmbientEffects(!bridge.ambientEffects) } },
        { label: qsTr("Cancel current task"), run: function() { bridge.cancelTask() } },
        { label: bridge.executionEnabled ? qsTr("Freeze JARVIS") : qsTr("Resume JARVIS"), run: function() { palette.lifecycleRequested() } },
        { label: qsTr("Refresh history"), run: function() { bridge.refreshHistory() } },
        { label: qsTr("Refresh automations"), run: function() { bridge.refreshJobs(false) } },
    ]
    property var matches: commands.filter(function(c) { return c.label.toLocaleLowerCase(i18n.language).indexOf(search.text.toLocaleLowerCase(i18n.language)) >= 0 })
    signal focusComposer()

    // K6: closing gives the keyboard back to what had it before the palette
    // opened (when it is still shown); "New task" gives it to the composer.
    property Item returnFocus: null
    function show() { returnFocus = palette.Window.activeFocusItem; open = true; search.text = ""; cursor = 0; search.forceActiveFocus() }
    function hide() {
        if (!open) return
        open = false
        var item = returnFocus
        returnFocus = null
        if (item && item.visible && item !== search) item.forceActiveFocus()
    }
    function runCurrent() { if (matches.length > 0) { var c = matches[Math.max(0, Math.min(cursor, matches.length - 1))]; hide(); c.run() } }
    function move(step) { cursor = Math.max(0, Math.min(cursor + step, matches.length - 1)); list.positionViewAtIndex(cursor, ListView.Contain) }
    onFocusComposer: Qt.callLater(function() {
        var stack = [palette.Window.contentItem]
        while (stack.length) {
            var node = stack.pop()
            if (!node || !node.visible) continue
            if (node.objectName === "composerInput") { node.forceActiveFocus(); return }
            for (var i = 0; i < node.children.length; i++) stack.push(node.children[i])
        }
    })

    Rectangle {
        id: dim
        anchors.fill: parent; color: "#000000"
        opacity: palette.open ? 0.4 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.pageMs } }
        MouseArea { anchors.fill: parent; enabled: palette.open; onClicked: palette.hide() }
    }
    GlassPanel {
        id: paletteSurface
        objectName: "commandPaletteSurface"
        raised: true
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height * 0.18
        width: Math.min(parent.width * 0.5, 640); height: 58 + (palette.matches.length ? list.height : 30) + 32
        opacity: palette.open ? 1 : 0
        scale: palette.open ? 1 : 1 - 2 * Theme.travel / Math.max(1, width, height)   // the edges travel at most Theme.travel
        Behavior on opacity { NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        Behavior on scale { NumberAnimation { duration: Theme.pageMs; easing.type: Easing.OutCubic } }
        padding: 16
        // A click on the panel itself is not an outside click.
        MouseArea { parent: paletteSurface; anchors.fill: parent; z: -1; enabled: palette.open }
        Column {
            width: parent.width; spacing: 10
            Rectangle { width: parent.width; height: 44; radius: Theme.radiusSmall; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.6)
                Rectangle { z: -1; anchors.fill: parent; anchors.margins: -3; radius: Theme.radiusSmall + 3; color: Theme.alpha(Theme.accent, 0.1) }
                Icon { x: 11; anchors.verticalCenter: parent.verticalCenter; name: "chevron-right"; size: 16; tint: Theme.accent }
                TextField { id: search; objectName: "commandPaletteSearch"; anchors.fill: parent; anchors.margins: 4; anchors.leftMargin: 30; background: Item {}  color: Theme.text; placeholderText: qsTr("Type a command…"); placeholderTextColor: Theme.textDim; font: Theme.lead
                    onTextChanged: { palette.cursor = 0; list.positionViewAtBeginning() }
                    // The keyboard stays in the field: arrows and Tab move the choice, the list follows it.
                    Keys.onDownPressed: palette.move(1)
                    Keys.onUpPressed: palette.move(-1)
                    Keys.onTabPressed: palette.move(1)
                    Keys.onBacktabPressed: palette.move(-1)
                    Keys.onReturnPressed: palette.runCurrent()
                    Keys.onEnterPressed: palette.runCurrent()
                    Keys.onEscapePressed: palette.hide() } }
            Text { objectName: "commandPaletteEmpty"; visible: palette.matches.length === 0; width: parent.width; wrapMode: Text.Wrap
                   leftPadding: 14; text: qsTr("No command matches \"%1\".").arg(search.text); color: Theme.textMuted; font: Theme.body }
            ListView {
                id: list
                objectName: "commandPaletteList"
                width: parent.width; height: Math.min(matches.length, 9) * 36; clip: true
                model: palette.matches
                delegate: Rectangle {
                    width: ListView.view.width; height: 36; radius: Theme.radiusSmall
                    color: index === palette.cursor ? Theme.alpha(Theme.accent, 0.14) : "transparent"
                    border.width: 1; border.color: index === palette.cursor ? Theme.alpha(Theme.accent, 0.4) : "transparent"
                    Rectangle { visible: index === palette.cursor; x: 0; y: 10; width: 2; height: 16; radius: 1; color: Theme.accent }
                    Text { anchors.left: parent.left; anchors.leftMargin: 14; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: index === palette.cursor ? Theme.accent : Theme.textLabel; font: Theme.body }
                    MouseArea { anchors.fill: parent; hoverEnabled: true; onEntered: palette.cursor = index; onClicked: { palette.cursor = index; palette.runCurrent() } }
                }
            }
        }
    }
}
