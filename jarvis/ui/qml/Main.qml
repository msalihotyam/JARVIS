import QtQuick
import QtQuick.Controls.Basic
import "theme"
import "components"
import "pages"
import "overlays"

// The one window. Full-screen by default; the top-right controls and F11
// keep minimize / windowed / maximize / close reachable.
Window {
    id: root
    width: 1600; height: 900
    minimumWidth: 1100; minimumHeight: 700
    visible: true
    visibility: Window.FullScreen
    title: "JARVIS"
    color: Theme.bg0

    readonly property bool fullscreen: visibility === Window.FullScreen
    readonly property bool homePage: bridge.page === "home"
    // Observable from the outside (tests): the live theme tokens and page.
    objectName: "mainWindow"
    readonly property string themeName: Theme.name
    readonly property color accentToken: Theme.accent
    readonly property color bgToken: Theme.bg0
    readonly property color textToken: Theme.text
    readonly property string loadedPage: pageLoader.current
    readonly property bool paletteOpen: palette.open
    readonly property bool navOpen: nav.open
    readonly property bool settingsOpen: settings.open

    function toggleSettings() { settings.open = !settings.open }

    Component.onCompleted: {
        Theme.name = bridge.theme
        Theme.ambient = bridge.ambientEffects
        bridge.start()
    }
    Connections {
        target: bridge
        function onPrefsChanged() { Theme.name = bridge.theme; Theme.ambient = bridge.ambientEffects }
        function onApprovalChanged() { if (bridge.approvalPending) bridge.setPage("home") }
    }

    Timer { interval: 1000; running: true; repeat: true; onTriggered: { clock.now = new Date() } }
    QtObject { id: clock; property date now: new Date() }

    Background { anchors.fill: parent; active: root.active }

    TopBar {
        id: topBar
        anchors { left: parent.left; right: parent.right; top: parent.top }
        clock: Qt.formatTime(clock.now, "HH:mm")
        date: Qt.formatDate(clock.now, "ddd, d MMMM yyyy")
        fullscreen: root.fullscreen
        settingsOpen: settings.open
        onMenuRequested: nav.open = !nav.open
        onKillSwitchRequested: root.askKillSwitch()
        onEditRequested: bridge.setEditMode(!bridge.editMode)
        onMinimizeRequested: root.showMinimized()
        onRestoreRequested: root.fullscreen ? root.showNormal() : root.showFullScreen()
        onCloseRequested: root.close()
        onOpenSettings: root.toggleSettings()
    }

    function askKillSwitch() {
        if (bridge.executionEnabled)
            confirm.ask("Kill switch", "Disable execution for this installation? No new work can start until it is re-enabled. Running work is stopped with Cancel.", "Disable execution", true, function() { bridge.setExecutionEnabled(false) })
        else
            confirm.ask("Re-enable execution", "Allow JARVIS to start work again? Every approval still applies.", "Re-enable", false, function() { bridge.setExecutionEnabled(true) })
    }

    // -- content ----------------------------------------------------------
    // The reference composition keeps a quiet left gutter under the clock;
    // the workspace and the column float to the right of it.
    Item {
        id: content
        anchors { left: parent.left; right: parent.right; top: topBar.bottom; bottom: parent.bottom
                  leftMargin: root.width < 1500 ? Theme.margin : Math.round(root.width * 0.122); rightMargin: Theme.margin; bottomMargin: 40; topMargin: 8 }
        Behavior on anchors.leftMargin { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
        readonly property bool showColumn: root.homePage && !(bridge.focusMode && panelColumn.shown.length === 0)
        readonly property real columnWidth: showColumn ? Math.round(width * (bridge.focusMode ? Math.min(bridge.columnWidth, 0.26) : bridge.columnWidth)) : 0

        // Workspace: the page, with a restrained crossfade + slide on change.
        Item {
            id: workspace
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
            width: content.width - (content.showColumn ? content.columnWidth + Theme.gap + 4 : 0)
            Behavior on width { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
            Loader {
                id: pageLoader
                anchors.fill: parent
                property string current: bridge.page
                sourceComponent: current === "tasks" ? tasksPage : current === "automations" ? automationsPage : current === "memory" ? memoryPage : current === "history" ? historyPage : homePage
                onCurrentChanged: pageIn.restart()
                opacity: 1
                transform: Translate { id: pageSlide; y: 0 }
                SequentialAnimation {
                    id: pageIn
                    PropertyAction { target: pageLoader; property: "opacity"; value: 0 }
                    PropertyAction { target: pageSlide; property: "y"; value: 8 }
                    ParallelAnimation {
                        NumberAnimation { target: pageLoader; property: "opacity"; to: 1; duration: Theme.pageMs; easing.type: Easing.OutCubic }
                        NumberAnimation { target: pageSlide; property: "y"; to: 0; duration: Theme.pageMs; easing.type: Easing.OutCubic }
                    }
                }
            }
        }
        Component { id: homePage; CommandCenter { id: cc } }
        Component { id: tasksPage; TasksPage {} }
        Component { id: automationsPage; AutomationsPage { onConfirmArchive: function(job) { confirm.ask("Archive job", "Archive '" + job.label + "'? There is no un-archive.", "Archive", true, function() { bridge.archiveJob(job.id, job.revision) }) } } }
        Component { id: memoryPage; MemoryPage {} }
        Component { id: historyPage; HistoryPage {} }

        // Column width grip (Edit Mode): direct while dragging, snapped on release.
        Rectangle {
            visible: bridge.editMode && content.showColumn
            x: workspace.width + (Theme.gap + 4) / 2 - 3; width: 6; height: 60
            anchors.verticalCenter: parent.verticalCenter
            radius: 3; color: Theme.accent; opacity: widthGrip.pressed ? 1 : 0.6
            MouseArea {
                id: widthGrip
                anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.SizeHorCursor
                property real startX: 0; property real startWidth: 0
                onPressed: function(mouse) { startX = mouse.x; startWidth = bridge.columnWidth }
                onPositionChanged: function(mouse) { if (pressed) bridge.setColumnWidth(startWidth - (mouse.x - startX) / content.width) }
            }
        }

        PanelColumn {
            id: panelColumn
            objectName: "panelColumn"
            anchors { right: parent.right; top: parent.top; bottom: parent.bottom }
            width: content.columnWidth
            visible: content.showColumn
            opacity: content.showColumn ? 1 : 0
            Behavior on width { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
            Behavior on opacity { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
        }
    }

    // -- HUD captions and fragments along the bottom edge -----------------------
    Item {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: 30
        Row { anchors { left: parent.left; leftMargin: Theme.margin; verticalCenter: parent.verticalCenter } spacing: 12
            Text { anchors.verticalCenter: parent.verticalCenter; text: "INTELLIGENCE AMPLIFIES POTENTIAL"; color: Theme.textDim; font { family: Theme.font; pixelSize: Theme.fsMicro; letterSpacing: 3.2 } }
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 120; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.7 }
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 4; height: 4; radius: 2; color: Theme.hud; opacity: Theme.hudOpacity * 1.4 }
        }
        Row { anchors { right: parent.right; rightMargin: Theme.margin; verticalCenter: parent.verticalCenter } spacing: 12
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 4; height: 4; radius: 2; color: Theme.hud; opacity: Theme.hudOpacity * 1.4 }
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 120; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.7 }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "SAME HIGHER FURTHER"; color: Theme.textDim; font { family: Theme.font; pixelSize: Theme.fsMicro; letterSpacing: 3.2 } }
        }
    }
    // Focus-mode badge: a tiny reminder that the arrangement is temporary.
    Pill {
        visible: bridge.focusMode
        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 8 }
        text: "Focus mode · Ctrl+Shift+F to leave"; tint: Theme.accent
    }

    NavMenu { id: nav; onSettingsRequested: settings.open = true }
    ApprovalOverlay {}
    SettingsDrawer { id: settings }
    CommandPalette { id: palette; onKillSwitchRequested: root.askKillSwitch(); onSettingsRequested: settings.open = true; onFocusComposer: {} }
    ConfirmDialog { id: confirm }

    // -- shortcuts -----------------------------------------------------------
    Shortcut { sequence: "Ctrl+K"; onActivated: palette.open ? palette.hide() : palette.show() }
    Shortcut { sequence: "F11"; onActivated: root.fullscreen ? root.showNormal() : root.showFullScreen() }
    Shortcut { sequence: "Ctrl+Shift+F"; onActivated: bridge.setFocusMode(!bridge.focusMode) }
    Shortcut { sequence: "Ctrl+E"; onActivated: bridge.setEditMode(!bridge.editMode) }
    Shortcut { sequence: "Ctrl+M"; onActivated: nav.open = !nav.open }
    Shortcut { sequence: "Ctrl+1"; onActivated: bridge.setPage("home") }
    Shortcut { sequence: "Ctrl+2"; onActivated: bridge.setPage("tasks") }
    Shortcut { sequence: "Ctrl+3"; onActivated: bridge.setPage("automations") }
    Shortcut { sequence: "Ctrl+4"; onActivated: bridge.setPage("memory") }
    Shortcut { sequence: "Ctrl+5"; onActivated: bridge.setPage("history") }
    Shortcut { sequence: "Ctrl+6"; onActivated: root.toggleSettings() }
    Shortcut { sequence: "Escape"; onActivated: {
        if (palette.open) palette.hide()
        else if (confirm.open) confirm.open = false
        else if (settings.open) settings.open = false
        else if (nav.open) nav.open = false
        else if (bridge.editMode) bridge.setEditMode(false) } }
}
