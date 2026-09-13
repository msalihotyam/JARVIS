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
    readonly property bool modelOpen: modelPanel.open
    readonly property bool peekOpen: peek.open
    readonly property string peekKey: peek.open ? peek.key : ""
    readonly property string presentation: bridge.theme
    readonly property bool inlineApproval: presentation === "command" && root.homePage && root.width >= 1500 && !bridge.focusMode
    property bool devApprovalVisible: false

    function closeContexts() { settings.open = false; modelPanel.open = false; nav.open = false; peek.open = false }
    // One summary peek at a time: the same card again closes it, another
    // card switches the content in place without closing the surface first.
    function togglePeek(key, cardY) {
        if (peek.open && peek.key === key) { peek.open = false; return }
        if (palette.open) palette.hide()
        settings.open = false; modelPanel.open = false; nav.open = false
        peek.key = key; peek.cardY = cardY; peek.open = true
    }
    function toggleSettings() { var next = !settings.open; closeContexts(); settings.open = next }
    function toggleModel() { var next = !modelPanel.open; closeContexts(); modelPanel.open = next }
    function toggleNav() { var next = !nav.open; closeContexts(); nav.open = next }
    onHomePageChanged: closeContexts()
    onPresentationChanged: { nav.open = false; modelPanel.open = false; peek.open = false }

    Component.onCompleted: {
        Theme.name = bridge.theme
        Theme.ambient = bridge.ambientEffects
        bridge.start()
    }
    Connections {
        target: bridge
        function onPrefsChanged() { Theme.name = bridge.theme; Theme.ambient = bridge.ambientEffects }
        function onApprovalChanged() { if (bridge.approvalPending) { root.closeContexts(); bridge.setPage("home") } }
    }

    Timer { interval: 1000; running: true; repeat: true; onTriggered: { clock.now = new Date() } }
    QtObject { id: clock; objectName: "systemClock"; property date now: new Date() }

    // DEV background audition (JARVIS_UI_BACKGROUND_AUDITION=1 only): 0 is the
    // production background; 1..4 put a candidate in the background image slot.
    readonly property bool backgroundAudition: bridge.devBackgroundCandidates.length > 0
    property int auditionIndex: 0
    function audition(i) {
        if (!backgroundAudition || i > bridge.devBackgroundCandidates.length) return
        auditionIndex = i; auditionLabel.opacity = 1; auditionFade.restart()
    }
    Background { anchors.fill: parent; active: root.active
        screenFocus: Qt.point(topBar.coreCx / Math.max(1, root.width), topBar.coreCy / Math.max(1, root.height))
        overrideSource: root.backgroundAudition && root.auditionIndex > 0 ? bridge.devBackgroundCandidates[root.auditionIndex - 1] : "" }

    TopBar {
        id: topBar
        controlLayer: topControls
        anchors { left: parent.left; right: parent.right; top: parent.top }
        // The identity / core zone owns a proportional share of the height;
        // the Command preset keeps its own compact strip.
        height: root.presentation === "command" ? 110 : Theme.topZone(root.height)
        railWidth: Theme.railWidth(root.width)
        viewportHeight: root.height
        now: clock.now
        fullscreen: root.fullscreen
        settingsOpen: settings.open
        modelOpen: modelPanel.open
        onMenuRequested: root.toggleNav()
        onKillSwitchRequested: root.askKillSwitch()
        onEditRequested: bridge.setEditMode(!bridge.editMode)
        onMinimizeRequested: root.showMinimized()
        onRestoreRequested: root.fullscreen ? root.showNormal() : root.showFullScreen()
        onCloseRequested: root.close()
        onOpenSettings: root.toggleSettings()
        onOpenModel: root.toggleModel()
    }

    function askKillSwitch() {
        if (bridge.executionEnabled)
            confirm.ask("Kill switch", "Disable execution for this installation? No new work can start until it is re-enabled. Running work is stopped with Cancel.", "Disable execution", true, function() { bridge.setExecutionEnabled(false) })
        else
            confirm.ask("Re-enable execution", "Allow JARVIS to start work again? Every approval still applies.", "Re-enable", false, function() { bridge.setExecutionEnabled(true) })
    }

    // -- content: the macro regions. Canonical / Dark take the normalized
    //    Canonical Space map (ambient rail | work surface | intelligence
    //    stack); the Command preset keeps its own outer geometry. ----------
    Item {
        id: content
        readonly property bool command: root.presentation === "command"
        readonly property bool wide: root.width >= 1500
        anchors { left: parent.left; right: parent.right; top: parent.top; bottom: parent.bottom
                  leftMargin: command ? Theme.margin : Theme.railWidth(root.width)
                  rightMargin: command ? Theme.margin : Theme.outerRight(root.width)
                  topMargin: command ? topBar.height + 8 : Theme.topZone(root.height)
                  bottomMargin: command ? (wide ? Math.round(root.height * 0.046) : 40) : Theme.centralBottom(root.height) }
        Behavior on anchors.leftMargin { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
        readonly property bool showColumn: root.presentation !== "command" && root.homePage && !(bridge.focusMode && panelColumn.shown.length === 0)
        readonly property real columnWidth: showColumn ? Math.round(width * (bridge.focusMode ? Math.min(bridge.columnWidth, 0.26) : bridge.columnWidth)) : 0
        readonly property int columnGap: command ? 16 : Theme.columnGap(root.width)
        // The conversation shell alone rises into the header environment;
        // the right stack and every other page keep the panel line.
        readonly property int lift: command || !root.homePage ? 0 : Theme.shellLift(root.height)

        // Workspace: the page, with a restrained crossfade + slide on change.
        Item {
            id: workspace
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom; topMargin: -content.lift }
            width: content.width - (content.showColumn ? content.columnWidth + content.columnGap : 0)
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
        Component { id: homePage; Loader { sourceComponent: bridge.theme === "command" ? commandPackage : canonicalPackage } }
        Component { id: canonicalPackage; CommandCenter { lift: content.lift; fade: Theme.shellFade(root.height) } }
        Component { id: commandPackage; CommandPreset { fixtureApproval: bridge.devFixture && root.devApprovalVisible; onDismissFixture: root.devApprovalVisible = false } }
        Component { id: tasksPage; TasksPage {} }
        Component { id: automationsPage; AutomationsPage { onConfirmArchive: function(job) { confirm.ask("Archive job", "Archive '" + job.label + "'? There is no un-archive.", "Archive", true, function() { bridge.archiveJob(job.id, job.revision) }) } } }
        Component { id: memoryPage; MemoryPage {} }
        Component { id: historyPage; HistoryPage {} }

        // Column width grip (Edit Mode): direct while dragging, snapped on release.
        Rectangle {
            visible: bridge.editMode && content.showColumn
            x: workspace.width + content.columnGap / 2 - 3; width: 6; height: 60
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
            anchors { right: parent.right; top: parent.top; bottom: parent.bottom
                      bottomMargin: content.command ? 0 : Theme.stackInset(root.height) }
            width: content.columnWidth
            visible: content.showColumn
            opacity: content.showColumn ? 1 : 0
            Behavior on width { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
            Behavior on opacity { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
        }
    }

    // -- HUD captions and fragments along the bottom edge -----------------------
    Item {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; bottomMargin: 24 }
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

    NavMenu { id: nav; anchorPoint: Qt.point(topBar.navAnchor.x - panelWidth, topBar.navAnchor.y) }
    Loader { anchors.fill: parent; z: 450; active: !root.inlineApproval
        sourceComponent: Component { ApprovalOverlay { fixture: bridge.devFixture && root.devApprovalVisible; onDismissFixture: root.devApprovalVisible = false } }
    }

    // Only interaction controls sit over contextual scrims. The clock,
    // core and HUD stay behind floating glass, never across its text.
    Item { id: topControls; anchors.fill: parent; z: 500 }
    SettingsDrawer { id: settings; anchorPoint: topBar.settingsAnchor }
    ModelPanel { id: modelPanel; anchorPoint: topBar.modelAnchor }

    // -- the left ambient rail's summary stack and its quick-look peek ------
    // The rail sits under the date / time / context block, inside the Phase 1
    // rail width, and above the peek's outside-click scrim so its cards can
    // toggle and switch the peek; approval and confirmation still cover it.
    SummaryPeek {
        id: peek
        rail: summaryRail
        anchorPoint: Qt.point(summaryRail.x + summaryRail.width + 12, summaryRail.y + peek.cardY - 8)
    }
    SummaryRail {
        id: summaryRail
        z: 420
        visible: root.presentation !== "command" && root.homePage
        x: 10
        y: Math.round(root.height * 0.1980) + 76
        width: Theme.railWidth(root.width) - x - 10
        height: root.height - y - 64
        activeKey: peek.open ? peek.key : ""
        onPicked: function(key, cardY, cardHeight) { root.togglePeek(key, cardY) }
    }
    CommandPalette { id: palette; onKillSwitchRequested: root.askKillSwitch(); onSettingsRequested: root.toggleSettings(); onFocusComposer: {} }
    ConfirmDialog { id: confirm }

    // -- DEVELOPMENT ONLY: reconstruction aids. Both exist only when the
    //    environment provides them (JARVIS_UI_DEV_REFERENCE / _FIXTURE); a
    //    normal launch has neither. The reference overlay is non-interactive
    //    and cycles hidden -> 50 % blend -> reference only with Ctrl+Shift+R.
    Image {
        id: devReference
        anchors.fill: parent; z: 9000
        source: bridge.devReference
        visible: bridge.devReference !== "" && opacity > 0
        opacity: 0
        fillMode: Image.Stretch
        enabled: false
        Rectangle { anchors { right: parent.right; top: parent.top; margins: 8 } width: devLabel.width + 16; height: 22; radius: 6; color: "#c0000000"
            Text { id: devLabel; anchors.centerIn: parent; text: "DEV REFERENCE OVERLAY " + Math.round(devReference.opacity * 100) + " %"; color: "#ff8080"; font { family: Theme.font; pixelSize: 10; letterSpacing: 1.5; weight: Font.DemiBold } } }
    }
    Shortcut { sequence: "Ctrl+Shift+R"; enabled: bridge.devReference !== ""; onActivated: devReference.opacity = devReference.opacity === 0 ? 0.5 : (devReference.opacity === 0.5 ? 1 : 0) }
    Shortcut { sequence: "Ctrl+Shift+A"; enabled: bridge.devFixture && !bridge.approvalPending; onActivated: { root.closeContexts(); root.devApprovalVisible = !root.devApprovalVisible } }
    Pill { visible: bridge.devFixture; z: 8000; text: "Dev fixture · sample text, not real activity"; tint: Theme.danger
        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 26 } }

    // -- shortcuts -----------------------------------------------------------
    Shortcut { sequence: "Ctrl+K"; onActivated: palette.open ? palette.hide() : palette.show() }
    Shortcut { sequence: "F11"; onActivated: root.fullscreen ? root.showNormal() : root.showFullScreen() }
    Shortcut { sequence: "Ctrl+Shift+F"; onActivated: bridge.setFocusMode(!bridge.focusMode) }
    Shortcut { sequence: "Ctrl+E"; onActivated: bridge.setEditMode(!bridge.editMode) }
    Shortcut { sequence: "Ctrl+M"; onActivated: root.toggleNav() }
    // Ctrl+1..4 belong to the background audition while it is enabled.
    Shortcut { sequence: "Ctrl+1"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("home") }
    Shortcut { sequence: "Ctrl+2"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("tasks") }
    Shortcut { sequence: "Ctrl+3"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("automations") }
    Shortcut { sequence: "Ctrl+4"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("memory") }
    Shortcut { sequence: "Ctrl+0"; enabled: root.backgroundAudition; onActivated: root.audition(0) }
    Shortcut { sequence: "Ctrl+1"; enabled: root.backgroundAudition; onActivated: root.audition(1) }
    Shortcut { sequence: "Ctrl+2"; enabled: root.backgroundAudition; onActivated: root.audition(2) }
    Shortcut { sequence: "Ctrl+3"; enabled: root.backgroundAudition; onActivated: root.audition(3) }
    Shortcut { sequence: "Ctrl+4"; enabled: root.backgroundAudition; onActivated: root.audition(4) }
    Rectangle {
        id: auditionLabel
        visible: root.backgroundAudition && opacity > 0
        opacity: 0; z: 9500; enabled: false
        anchors { horizontalCenter: parent.horizontalCenter; top: parent.top; topMargin: 10 }
        width: auditionText.width + 24; height: 26; radius: 6; color: "#d0000812"; border.width: 1; border.color: "#ff8080"
        Text { id: auditionText; anchors.centerIn: parent; color: "#ff8080"
            text: "DEV · " + (root.auditionIndex === 0 ? "CURRENT BACKGROUND" : "BACKGROUND " + root.auditionIndex + " / " + bridge.devBackgroundCandidates.length)
            font { family: Theme.font; pixelSize: 11; letterSpacing: 1.5; weight: Font.DemiBold } }
        SequentialAnimation { id: auditionFade; PauseAnimation { duration: 900 } NumberAnimation { target: auditionLabel; property: "opacity"; to: 0; duration: 250 } }
    }
    Shortcut { sequence: "Ctrl+5"; onActivated: bridge.setPage("history") }
    Shortcut { sequence: "Ctrl+6"; onActivated: root.toggleSettings() }
    Shortcut { sequence: "Escape"; onActivated: {
        if (palette.open) palette.hide()
        else if (confirm.open) confirm.open = false
        else if (settings.open) settings.open = false
        else if (modelPanel.open) modelPanel.open = false
        else if (nav.open) nav.open = false
        else if (peek.open) peek.open = false
        else if (!root.homePage && pageLoader.item) pageLoader.item.closePage()
        else if (bridge.editMode) bridge.setEditMode(false) } }
}
