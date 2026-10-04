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
    // Observable from the outside (tests): the live motion contract.
    readonly property int textRenderToken: Theme.textRender
    readonly property int hoverToken: Theme.hoverMs
    readonly property int panelInToken: Theme.panelIn
    readonly property int panelTravelToken: Theme.panelTravel
    // Observable from the outside (tests): the type ramp and the ink steps.
    readonly property var typeTokens: ({ label: Theme.label, caption: Theme.caption, captionStrong: Theme.captionStrong,
                                         body: Theme.body, bodyStrong: Theme.bodyStrong, lead: Theme.lead, subtitle: Theme.subtitle,
                                         title: Theme.title, titleLarge: Theme.titleLarge, reading: Theme.conversationBody,
                                         composer: Theme.composerText })
    readonly property var inkTokens: ({ primary: Theme.text, secondary: Theme.textLabel, muted: Theme.textMuted, dim: Theme.textDim })
    readonly property bool inlineApproval: presentation === "command" && root.homePage && root.width >= 1500 && !bridge.focusMode
    property bool devApprovalVisible: false
    // The last non-minimized, non-hidden state (full screen / maximized /
    // windowed). Only a real change between those states updates it:
    // minimizing, hiding to the tray and Alt+Tab never do. Open JARVIS restores it.
    property int restoreVisibility: Window.FullScreen
    onVisibilityChanged: function(visibility) {
        if (visibility === Window.Windowed || visibility === Window.Maximized
                || visibility === Window.FullScreen) restoreVisibility = visibility
    }

    // Window X hides JARVIS to the system tray: the process, the session and
    // any work keep running (unless JARVIS is frozen separately). Only Quit
    // JARVIS in the tray ends the application; without a tray, closing quits.
    onClosing: function(close) {
        if (bridge.quitting || !bridge.trayAvailable) return
        close.accepted = false
        root.closeContexts()
        root.hide()
        bridge.noteHiddenToTray()
    }

    function closeContexts() { settings.open = false; modelPanel.open = false; nav.open = false; closePeek() }
    function closePeek() { peekSwitch.stop(); peek.open = false }
    // One summary peek at a time: the same card again closes it. Another card
    // is a real switch: the open surface closes with its own transition, then
    // the new one opens with its own - never content swapped in an open
    // surface. Picks during a switch only re-aim it; the last one wins.
    function togglePeek(key, cardY) {
        if (peekSwitch.running) {
            if (peekSwitch.key === key) peekSwitch.stop()
            else { peekSwitch.key = key; peekSwitch.cardY = cardY }
            return
        }
        if (peek.open && peek.key === key) { peek.open = false; return }
        if (palette.open) palette.hide()
        settings.open = false; modelPanel.open = false; nav.open = false
        if (peek.open) { peekSwitch.key = key; peekSwitch.cardY = cardY; peek.open = false; peekSwitch.start(); return }
        peek.key = key; peek.cardY = cardY; peek.open = true
    }
    Timer {
        id: peekSwitch
        property string key: ""
        property real cardY: 0
        interval: Theme.panelOut
        onTriggered: { peek.key = key; peek.cardY = cardY; peek.open = true }
    }
    function toggleSettings() { var next = !settings.open; closeContexts(); settings.open = next }
    function toggleModel() { var next = !modelPanel.open; closeContexts(); modelPanel.open = next }
    function toggleNav() { var next = !nav.open; closeContexts(); nav.open = next }
    onHomePageChanged: closeContexts()
    onPresentationChanged: { nav.open = false; modelPanel.open = false; closePeek() }

    Component.onCompleted: {
        Theme.name = bridge.theme
        Theme.ambient = bridge.ambientEffects
        bridge.start()
    }
    Connections {
        target: bridge
        function onPrefsChanged() { Theme.name = bridge.theme; Theme.ambient = bridge.ambientEffects; if (bridge.editMode) root.closePeek() }
        function onApprovalChanged() { if (bridge.approvalPending) { root.closeContexts(); bridge.setPage("home") } }
        // From the tray: the same confirmations the window uses.
        function onResumeConfirmRequested() { root.closeContexts(); resumeConfirm.open = true }
        function onQuitConfirmRequested() {
            confirm.ask(qsTr("Quit JARVIS?"), bridge.quitWarning, qsTr("Quit JARVIS"), false, function() { bridge.confirmQuit() }, qsTr("Cancel"))
        }
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
        sideRight: sidebar.visible ? sidebar.x + sidebar.width : 0
        viewportHeight: root.height
        fullscreen: root.fullscreen
        settingsOpen: settings.open
        modelOpen: modelPanel.open
        onMenuRequested: root.toggleNav()
        onLifecycleRequested: root.toggleLifecycle()
        onEditRequested: bridge.setEditMode(!bridge.editMode)
        onMinimizeRequested: bridge.minimizeWindow(root)
        onRestoreRequested: root.fullscreen ? root.showNormal() : root.showFullScreen()
        onCloseRequested: root.close()
        onOpenSettings: root.toggleSettings()
        onOpenModel: root.toggleModel()
    }

    // The red control: Freeze JARVIS is sent AT ONCE - no confirmation, no
    // delay, whatever is open. When the gate is closed (frozen, or a freeze
    // still waiting for its operation) it offers Resume, which asks first.
    function toggleLifecycle() {
        if (!bridge.executionEnabled && bridge.executionState !== "refused") { resumeConfirm.open = !resumeConfirm.open; return }
        resumeConfirm.open = false
        bridge.freezeJarvis()
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
        // Gate 6F: on home the work surface starts after the sidebar; the right
        // column and every other page keep the rail line (L5, Q11).
        readonly property real homeInset: command || !root.homePage ? 0 : sidebar.x + Theme.sidebarWidth(root.width) + columnGap - Theme.railWidth(root.width)
        readonly property int columnGap: command ? 16 : Theme.columnGap(root.width)
        // The conversation shell alone rises into the header environment;
        // the right stack and every other page keep the panel line.
        readonly property int lift: command || !root.homePage ? 0 : Theme.shellLift(root.height)

        // Workspace: the page, with a restrained crossfade + slide on change.
        Item {
            id: workspace
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom; topMargin: -content.lift; leftMargin: content.homeInset }
            width: content.width - content.homeInset - (content.showColumn ? content.columnWidth + content.columnGap : 0)
            Behavior on width { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
            Loader {
                id: pageLoader
                anchors.fill: parent
                property string current: bridge.page
                sourceComponent: current === "tasks" ? tasksPage : current === "automations" ? automationsPage : current === "memory" ? memoryPage : current === "history" ? historyPage : current === "projects" ? projectsPage : homePage
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
        Component { id: commandPackage; CommandPreset { fixtureApproval: bridge.devFixture && root.devApprovalVisible; onDismissFixture: root.devApprovalVisible = false
            onMenuRequested: function(card, anchor, canUp, canDown) { layoutMenus.openCard("stack", card, anchor, canUp, canDown) }
            onCardsRequested: function(anchor) { layoutMenus.toggle("cards", anchor) }
            onResetRequested: function(anchor) { layoutMenus.toggle("reset", anchor) } } }
        Component { id: tasksPage; TasksPage {} }
        Component { id: automationsPage; AutomationsPage { onConfirmArchive: function(job) { confirm.ask(qsTr("Archive job"), qsTr("Archive '%1'? There is no un-archive.").arg(job.label), qsTr("Archive"), true, function() { bridge.archiveJob(job.id, job.revision) }) } } }
        Component { id: memoryPage; MemoryPage {} }
        Component { id: historyPage; HistoryPage {} }
        Component { id: projectsPage; ProjectsPage {} }

        // Column width grip (Edit Mode): direct while dragging, snapped on release.
        Rectangle {
            visible: bridge.editMode && content.showColumn
            x: workspace.x + workspace.width + content.columnGap / 2 - 3; width: 6; height: 60
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
            onMenuRequested: function(card, anchor, canUp, canDown) { layoutMenus.openCard("stack", card, anchor, canUp, canDown) }
            onCardsRequested: function(anchor) { layoutMenus.toggle("cards", anchor) }
            onResetRequested: function(anchor) { layoutMenus.toggle("reset", anchor) }
            Behavior on width { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
            Behavior on opacity { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
        }
    }

    // -- HUD captions and fragments along the bottom edge -----------------------
    Item {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; bottomMargin: 24 }
        height: 30
        Row { anchors { left: parent.left; leftMargin: Theme.margin; verticalCenter: parent.verticalCenter } spacing: 12
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("INTELLIGENCE AMPLIFIES POTENTIAL"); color: Theme.textDim; font: Theme.hudLabel }
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 120; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.7 }
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 4; height: 4; radius: 2; color: Theme.hud; opacity: Theme.hudOpacity * 1.4 }
        }
        Row { anchors { right: parent.right; rightMargin: Theme.margin; verticalCenter: parent.verticalCenter } spacing: 12
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 4; height: 4; radius: 2; color: Theme.hud; opacity: Theme.hudOpacity * 1.4 }
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 120; height: 1; color: Theme.hud; opacity: Theme.hudOpacity * 0.7 }
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("SAME HIGHER FURTHER"); color: Theme.textDim; font: Theme.hudLabel }
        }
    }
    // Focus-mode badge: a tiny reminder that the arrangement is temporary.
    Pill {
        visible: bridge.focusMode
        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 8 }
        text: qsTr("Focus mode · %1 to leave").arg("Ctrl+Shift+F"); tint: Theme.accent
    }

    NavMenu { id: nav; anchorPoint: Qt.point(topBar.navAnchor.x - panelWidth, topBar.navAnchor.y) }
    // MAIN / DARK decide approvals in the conversation card; only the Command
    // preset (V2) keeps its own authorization surfaces.
    Loader { anchors.fill: parent; z: 450; active: root.presentation === "command" && !root.inlineApproval
        sourceComponent: Component { ApprovalOverlay { fixture: bridge.devFixture && root.devApprovalVisible; onDismissFixture: root.devApprovalVisible = false } }
    }

    // Only interaction controls sit over contextual scrims. The clock,
    // core and HUD stay behind floating glass, never across its text.
    Item { id: topControls; anchors.fill: parent; z: 500 }
    SettingsDrawer { id: settings; anchorPoint: topBar.settingsAnchor }

    // Resume JARVIS? - compact, anchored under the red control. Running
    // visuals follow only what the store confirms afterwards.
    Item {
        id: resumeConfirm
        objectName: "resumeConfirm"
        property bool open: false
        anchors.fill: parent; z: 520
        visible: open
        Connections { target: bridge; function onStateChanged() { if (bridge.executionEnabled) resumeConfirm.open = false } }
        MouseArea { anchors.fill: parent; onClicked: resumeConfirm.open = false }
        Rectangle {
            width: Math.min(330, root.width - 32); height: reBody.height + 36
            x: Math.max(16, Math.min(topBar.killAnchor.x - width + 40, root.width - width - 16))
            y: topBar.killAnchor.y
            radius: 12; color: Theme.glassMenu; border.width: 1; border.color: Theme.alpha(Theme.danger, 0.55)
            MouseArea { anchors.fill: parent }
            Column {
                id: reBody
                x: 18; y: 18; width: parent.width - 36; spacing: 10
                Text { text: qsTr("Resume JARVIS?"); color: Theme.text; font: Theme.uiValue; activeFocusOnTab: true
                       Accessible.role: Accessible.Heading; Accessible.name: text }
                Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.metadata
                       text: bridge.executionState === "freeze_requested"
                             ? qsTr("An operation is still on its way to a safe point. Resuming lets it and new work continue.")
                             : qsTr("New work can start again, and a task Freeze paused continues from where it stopped. Nothing failed, cancelled or with an unknown outcome is repeated. This is not an approval.") }
                Row {
                    width: parent.width; spacing: 8
                    ActionButton { objectName: "keepFrozen"; label: qsTr("Keep frozen"); height: 40; width: (parent.width - 8) / 2; onClicked: resumeConfirm.open = false }
                    ActionButton { objectName: "resumeJarvis"; label: qsTr("Resume JARVIS"); primary: true; height: 40; width: (parent.width - 8) / 2
                        onClicked: { resumeConfirm.open = false; bridge.resumeJarvis() } }
                }
            }
        }
    }
    ModelPanel { id: modelPanel; anchorPoint: topBar.modelAnchor }
    // The internal attachment preview (image lightbox / text view), above every panel.
    AttachmentPreview { id: attachmentPreview }
    Connections { target: bridge; function onPreviewRequested(file) { attachmentPreview.show(file) } }

    // -- the home sidebar (Gate 6F) and its tiles' quick-look peek ------------
    // One panel where the clock card and the summary rail were: it starts
    // where the clock card started (the TopBar keeps its height) and ends
    // above the lower HUD. It sits above the peek's outside-click scrim so its
    // tiles can toggle and switch the peek; approval and confirmation still
    // cover it. The peek opens beside the sidebar, level with its tile.
    SummaryPeek {
        id: peek
        rail: sidebar.rail
        anchorPoint: Qt.point(sidebar.x + sidebar.width + 12, sidebar.y + peek.cardY - 8)
    }
    Sidebar {
        id: sidebar
        z: 420
        visible: root.presentation !== "command" && root.homePage
        x: 10
        // While the scope group still takes its own row (until F4), below it.
        y: Math.max(84, Math.round(root.height * 0.1980) + 76 - 12 - 144, topBar.scopeBottom + 8)
        width: Theme.sidebarWidth(root.width)
        height: root.height - y - 64
        now: clock.now
        activeKey: peekSwitch.running ? peekSwitch.key : peek.open ? peek.key : ""
        onPicked: function(key, cardY, cardHeight) { root.togglePeek(key, cardY) }
        onMenuRequested: function(card, anchor, canUp, canDown) { layoutMenus.openCard("rail", card, anchor, canUp, canDown) }
    }
    // Edit Mode's popovers for both regions: over the rail, under approvals.
    LayoutMenus { id: layoutMenus; z: 445; railShown: sidebar.visible; commandMode: root.presentation === "command" }
    CommandPalette { id: palette; onLifecycleRequested: root.toggleLifecycle(); onSettingsRequested: root.toggleSettings(); onFocusComposer: {} }
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
            Text { id: devLabel; anchors.centerIn: parent; text: qsTr("DEV REFERENCE OVERLAY %1 %").arg(Math.round(devReference.opacity * 100)); color: "#ff8080"; font: Theme.label } }
    }
    Shortcut { sequence: "Ctrl+Shift+R"; enabled: bridge.devReference !== ""; onActivated: devReference.opacity = devReference.opacity === 0 ? 0.5 : (devReference.opacity === 0.5 ? 1 : 0) }
    Shortcut { sequence: "Ctrl+Shift+A"; enabled: bridge.devFixture && !bridge.approvalPending; onActivated: { root.closeContexts(); root.devApprovalVisible = !root.devApprovalVisible
        bridge.setDevSafetyScenario(root.devApprovalVisible ? "approval" : "") } }
    // DEV FIXTURE: cycle display-only safety scenarios (approval, missing details, unknown, failed, blocked, completed).
    Shortcut { sequence: "Ctrl+Shift+S"; enabled: bridge.devFixture; onActivated: bridge.nextDevSafetyScenario() }
    Pill { visible: bridge.devFixture; z: 8000; text: qsTr("Dev fixture · sample text, not real activity"); tint: Theme.danger
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
            text: "DEV · " + (root.auditionIndex === 0 ? qsTr("CURRENT BACKGROUND") : qsTr("BACKGROUND %1 / %2").arg(root.auditionIndex).arg(bridge.devBackgroundCandidates.length))
            font: Theme.label }
        SequentialAnimation { id: auditionFade; PauseAnimation { duration: 900 } NumberAnimation { target: auditionLabel; property: "opacity"; to: 0; duration: 250 } }
    }
    Shortcut { sequence: "Ctrl+5"; onActivated: bridge.setPage("history") }
    Shortcut { sequence: "Ctrl+6"; onActivated: bridge.setPage("projects") }
    Shortcut { sequence: "Escape"; onActivated: {
        if (attachmentPreview.open) attachmentPreview.close()
        else if (palette.open) palette.hide()
        else if (confirm.open) confirm.open = false
        else if (resumeConfirm.open) resumeConfirm.open = false
        else if (settings.open) settings.open = false
        else if (modelPanel.open) modelPanel.open = false
        else if (nav.open) nav.open = false
        else if (layoutMenus.open) layoutMenus.close()
        else if (peek.open || peekSwitch.running) root.closePeek()
        else if (!root.homePage && pageLoader.item) pageLoader.item.closePage()
        else if (bridge.editMode) bridge.setEditMode(false) } }
}
