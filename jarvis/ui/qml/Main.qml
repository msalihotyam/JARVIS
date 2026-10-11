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
    readonly property bool peekOpen: peek.open
    readonly property string peekKey: peek.open ? peek.key : ""
    readonly property string presentation: bridge.theme
    // Observable from the outside (tests): the live motion contract.
    readonly property int textRenderToken: Theme.textRender
    readonly property int hoverToken: Theme.hoverMs
    readonly property int panelInToken: Theme.panelIn
    readonly property int panelTravelToken: Theme.travel
    // Observable from the outside (tests): the type ramp and the ink steps.
    readonly property var typeTokens: ({ label: Theme.label, caption: Theme.caption, captionStrong: Theme.captionStrong,
                                         body: Theme.body, bodyStrong: Theme.bodyStrong, lead: Theme.lead, subtitle: Theme.subtitle,
                                         title: Theme.title, titleLarge: Theme.titleLarge, reading: Theme.conversationBody,
                                         composer: Theme.composerText })
    readonly property var inkTokens: ({ primary: Theme.text, secondary: Theme.textLabel, muted: Theme.textMuted, dim: Theme.textDim })
    // Gate 6F (T1): the waiting approval is brought into view in the conversation.
    signal approvalRevealRequested()
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

    // -- Gate 6G Sidebar v2 slice 2 ---------------------------------------------
    // L1: on home the left column, the conversation and the right column share
    // ONE top line just under the brand and the controls (both fixed-size, so
    // the line is too); other pages keep their own geometry.
    readonly property bool homeCanonical: root.presentation !== "command" && root.homePage
    readonly property int topLine: 104
    // V5 / V6 / E1': remembered in the sidebar-layout preferences.
    readonly property var sideLayout: bridge.sidebarLayout
    readonly property string hideMode: sideLayout.hide_mode
    readonly property bool sideTiles: sideLayout.tiles
    // E1': the default width is the widest; a drag in edit mode only narrows
    // (240 / 340 of the default on the left, 300 / 386 on the right - the px
    // at 1920, scaled with the existing width rules at other sizes).
    function clampWidth(v, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, v))) }
    function storedWidth(px, max, lo, hi) { return clampWidth(px / Math.max(1, max) * hi, lo, hi) }
    readonly property int leftMax: Theme.sidebarWidth(root.width)
    readonly property int leftMin: Math.round(leftMax * 240 / 340)
    property real leftDrag: -1
    readonly property int leftChosen: leftDrag >= 0 ? leftDrag : clampWidth(sideLayout.left / 340 * leftMax, leftMin, leftMax)
    // V6: tiles mode is half of the DEFAULT left width, whatever was chosen.
    readonly property int leftWidth: sideTiles ? Math.round(leftMax / 2) : leftChosen
    function saveWidths() {
        bridge.setSidebarWidths(storedWidth(root.leftChosen, root.leftMax, 240, 340),
                                storedWidth(content.rightWidth, content.rightMax, 300, 386))
        root.leftDrag = -1; content.rightDrag = -1
    }
    // V5: while hidden, hovering the floating row shows the sidebar over the
    // content; it goes after a short grace once the pointer has left both.
    // A hide click on the floating row does not open it at once: the pointer
    // must leave the row first.
    property bool sideOverlay: false
    property bool overlayArmed: true
    function setHideMode(mode) {
        root.sideOverlay = false
        root.overlayArmed = mode === "" || !barHover.hovered
        bridge.setSidebarHideMode(mode)
    }
    Timer {
        id: overlayGrace
        interval: 350
        onTriggered: {
            if (sharedConversationMenu.open || layoutMenus.open) restart()
            else root.sideOverlay = sideHover.hovered || (barHover.hovered && root.overlayArmed)
        }
    }

    // L4: Geri / İleri over the views (a page; on home its conversation, on
    // Projects its project), newest last. A view that is gone is dropped.
    QtObject {
        id: history
        objectName: "navHistory"
        property var entries: []
        property int index: -1
        readonly property bool canBack: index > 0 && !bridge.busy
        readonly property bool canForward: index < entries.length - 1 && !bridge.busy
        function current() {
            var page = bridge.page
            return { page: page, conversation: page === "home" ? bridge.conversationId : "",
                     project: page === "projects" ? (bridge.openProject.id || "") : "" }
        }
        function same(a, b) { return !!a && a.page === b.page && a.conversation === b.conversation && a.project === b.project }
        function record() {
            var view = current()
            if (same(entries[index], view)) return
            var list = entries.slice(0, index + 1)
            list.push(view)
            if (list.length > 50) list.shift()
            entries = list; index = list.length - 1
        }
        function go(step) {
            var at = index + step
            if (at < 0 || at >= entries.length || bridge.busy) return
            var view = entries[at], ok = true
            if (view.page === "home") {
                if (view.conversation !== "" && view.conversation !== bridge.conversationId) ok = bridge.openConversation(view.conversation)
                if (ok) bridge.setPage("home")
            } else if (view.page === "projects") bridge.showProject(view.project)
            else bridge.setPage(view.page)
            if (ok) { index = at; return }
            var list = entries.slice(); list.splice(at, 1)
            entries = list; index = Math.min(at < index ? index - 1 : index, list.length - 1)
        }
    }
    Connections {
        target: bridge
        function onPrefsChanged() { Qt.callLater(history.record) }
        function onConversationChanged() { Qt.callLater(history.record) }
        function onProjectsChanged() { Qt.callLater(history.record) }
    }

    function closeContexts() { settings.open = false; nav.open = false; closePeek() }
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
        settings.open = false; nav.open = false
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
    function toggleNav() { var next = !nav.open; closeContexts(); nav.open = next }
    onHomePageChanged: closeContexts()
    onPresentationChanged: { nav.open = false; closePeek() }

    Component.onCompleted: {
        Theme.name = bridge.theme
        Theme.ambient = bridge.ambientEffects
        bridge.start()
        Qt.callLater(history.record)
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
        onMenuRequested: root.toggleNav()
        onLifecycleRequested: root.toggleLifecycle()
        panelsRaised: root.homeCanonical
        // Under the chat header, above the first message (L1).
        progressTop: root.homeCanonical && content.headerOnTop ? content.workTop + 8 + 52 + 6 : -1
        onMinimizeRequested: powerFx.powerDown()
        onRestoreRequested: root.fullscreen ? root.showNormal() : root.showFullScreen()
        onCloseRequested: root.close()
        onOpenSettings: root.toggleSettings()
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
        // E1': the stored fraction is the widest the column gets; the edit-mode drag narrows it.
        readonly property int rightMax: Math.round(width * (bridge.focusMode ? Math.min(bridge.columnWidth, 0.26) : bridge.columnWidth))
        readonly property int rightMin: Math.round(rightMax * 300 / 386)
        property real rightDrag: -1
        readonly property int rightWidth: rightDrag >= 0 ? rightDrag : root.clampWidth(root.sideLayout.right / 386 * rightMax, rightMin, rightMax)
        readonly property real columnWidth: showColumn ? rightWidth : 0
        // Gate 6F: on home the work surface starts after the sidebar; the right
        // column and every other page keep the rail line (L5, Q11). Measured
        // from this item's live x, so the rail's reflow never slides it under the sidebar.
        // V5: "grow" starts it at the window's left inset, under the floating row;
        // "keep" leaves the sidebar's space empty.
        readonly property real homeInset: command || !root.homePage ? 0
                                          : root.hideMode === "grow" ? sidebar.x - x : sidebar.x + root.leftWidth + columnGap - x
        readonly property int columnGap: command ? 16 : Theme.columnGap(root.width)
        // F-ee: the column and its gap, animated as ONE value that both the work
        // surface and the column read, so no frame of a reflow (a window resize,
        // focus mode, the grip) lets one overlap the other. While a large window
        // shrinks, the span is held to the room after the sidebar: never a
        // negative work surface, never the column under the sidebar.
        property real columnSpan: showColumn ? columnWidth + columnGap : 0
        Behavior on columnSpan { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
        readonly property real liveSpan: Math.min(columnSpan, Math.max(0, width - homeInset))
        // The conversation shell alone rises into the header environment;
        // the right stack and every other page keep the panel line.
        // L1: on home every column starts at the top line ("grow": under the floating row).
        readonly property int workTop: root.topLine + (root.hideMode === "grow" ? floatBar.height + 8 : 0)
        readonly property int lift: command || !root.homePage ? 0 : anchors.topMargin - workTop
        // The first message starts below the core art; the header sits at the panel
        // top, the progress band (TopBar) between them.
        // The header stops short of the core's machine ring (0.74 D, JarvisCore L5) when that
        // reaches its row; where that leaves it under 420 px (below the 1920 target) it goes
        // under the core instead, with the messages after it.
        readonly property real coreRing: topBar.coreDiameter * 0.37
        readonly property bool coreInRow: topBar.coreCy + coreRing > workTop + 8
        readonly property real headerRoom: topBar.coreCx - coreRing - 12 - x - workspace.x
        readonly property bool headerOnTop: !coreInRow || headerRoom - 24 >= 420
        readonly property int belowCore: Math.max(8, topBar.coreCy + topBar.coreDiameter / 2 - workTop + 8)
        readonly property int feedTop: headerOnTop ? Math.max(belowCore, 8 + 52 + 6 + 50 + 8) : belowCore

        // Workspace: the page, with a restrained crossfade + slide on change.
        Item {
            id: workspace
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom; topMargin: -content.lift; leftMargin: content.homeInset }
            width: content.width - content.homeInset - content.liveSpan
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
                    PropertyAction { target: pageSlide; property: "y"; value: Theme.travel }
                    ParallelAnimation {
                        NumberAnimation { target: pageLoader; property: "opacity"; to: 1; duration: Theme.pageMs; easing.type: Easing.OutCubic }
                        NumberAnimation { target: pageSlide; property: "y"; to: 0; duration: Theme.pageMs; easing.type: Easing.OutCubic }
                    }
                }
            }
        }
        Component { id: homePage; Loader { sourceComponent: bridge.theme === "command" ? commandPackage : canonicalPackage } }
        Component { id: canonicalPackage; CommandCenter { lift: content.feedTop - 8; fade: content.feedTop; headerTop: content.headerOnTop ? 8 : content.belowCore
                                                          headerRight: content.headerOnTop && content.coreInRow ? content.headerRoom : -1
                                                          conversationMenu: sharedConversationMenu; attachDialog: sharedAttachDialog } }
        Component { id: commandPackage; CommandPreset { fixtureApproval: bridge.devFixture && root.devApprovalVisible; onDismissFixture: root.devApprovalVisible = false
            conversationMenu: sharedConversationMenu; attachDialog: sharedAttachDialog
            onMenuRequested: function(card, anchor, canUp, canDown) { layoutMenus.openCard("stack", card, anchor, canUp, canDown) }
            onCardsRequested: function(anchor) { layoutMenus.toggle("cards", anchor) }
            onResetRequested: function(anchor) { layoutMenus.toggle("reset", anchor) } } }
        Component { id: tasksPage; TasksPage {} }
        Component { id: automationsPage; AutomationsPage { onConfirmArchive: function(job) { confirm.ask(qsTr("Archive job"), qsTr("Archive '%1'? There is no un-archive.").arg(job.label), qsTr("Archive"), true, function() { bridge.archiveJob(job.id, job.revision) }) } } }
        Component { id: memoryPage; MemoryPage {} }
        Component { id: historyPage; HistoryPage {} }
        Component { id: projectsPage; ProjectsPage {} }

        // E1': the right column's inner edge (Edit Mode): it only narrows, with a
        // live px label; the width is remembered on release.
        Rectangle {
            objectName: "rightWidthGrip"
            visible: bridge.editMode && content.showColumn
            x: workspace.x + workspace.width + content.columnGap / 2 - 3; width: 6; height: 60
            anchors.verticalCenter: parent.verticalCenter
            radius: 3; color: Theme.accent; opacity: rightGrip.pressed ? 1 : 0.6
            MouseArea {
                id: rightGrip
                anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.SizeHorCursor
                property real startX: 0; property real startWidth: 0
                onPressed: function(mouse) { startX = mapToItem(null, mouse.x, 0).x; startWidth = content.rightWidth }
                onPositionChanged: function(mouse) {
                    if (pressed) content.rightDrag = root.clampWidth(startWidth - (mapToItem(null, mouse.x, 0).x - startX), content.rightMin, content.rightMax)
                }
                onReleased: root.saveWidths()
                onCanceled: root.saveWidths()
            }
            Pill { objectName: "rightWidthLabel"; visible: rightGrip.pressed; dot: false; tint: Theme.accent
                   anchors { right: parent.left; rightMargin: 10; verticalCenter: parent.verticalCenter }
                   text: qsTr("%1 px").arg(content.rightWidth) }
        }

        PanelColumn {
            id: panelColumn
            objectName: "panelColumn"
            anchors { right: parent.right; top: parent.top; bottom: parent.bottom
                      topMargin: root.homeCanonical ? root.topLine - content.anchors.topMargin : 0
                      bottomMargin: content.command ? 0 : Theme.stackInset(root.height) }
            width: Math.max(0, content.liveSpan - content.columnGap)
            visible: content.showColumn
            opacity: content.showColumn ? 1 : 0
            onMenuRequested: function(card, anchor, canUp, canDown) { layoutMenus.openCard("stack", card, anchor, canUp, canDown) }
            onCardsRequested: function(anchor) { layoutMenus.toggle("cards", anchor) }
            onResetRequested: function(anchor) { layoutMenus.toggle("reset", anchor) }
            Behavior on opacity { NumberAnimation { duration: Theme.focusMs; easing.type: Easing.InOutCubic } }
        }
    }

    // -- HUD captions and fragments along the bottom edge -----------------------
    Item {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; bottomMargin: 24 }
        height: 30
        // K1: the tiles-mode toggle (V6) under the left column; the slogan moves right of it.
        IconButton {
            id: tilesToggle
            objectName: "sidebarTilesToggle"
            visible: root.homeCanonical
            x: sidebar.x; anchors.verticalCenter: parent.verticalCenter; width: 28; height: 28
            icon: "tiles"; active: root.sideTiles
            tip: root.sideTiles ? qsTr("Show the lists") : qsTr("Tiles view")
            onClicked: bridge.setSidebarTiles(!root.sideTiles)
        }
        Row { anchors { left: parent.left; leftMargin: tilesToggle.visible ? tilesToggle.x + tilesToggle.width + 12 : Theme.margin
                        verticalCenter: parent.verticalCenter } spacing: 12
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

    NavMenu { id: nav; anchorPoint: Qt.point(topBar.navAnchor.x - panelWidth, topBar.navAnchor.y); trigger: topBar.navTrigger }
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
        // K4 (the dialog style): focus starts on the safe choice and returns to the opener on close.
        property Item opener: null
        onVisibleChanged: if (visible) { opener = Window.activeFocusItem; keepFrozen.forceActiveFocus() }
                          else if (opener) { var back = opener; opener = null; back.forceActiveFocus() }
        Connections { target: bridge; function onStateChanged() { if (bridge.executionEnabled) resumeConfirm.open = false } }
        Rectangle { objectName: "resumeConfirmScrim"; anchors.fill: parent; color: Qt.rgba(0, 0, 0, Theme.scrimOpacity)
                    MouseArea { anchors.fill: parent; onClicked: resumeConfirm.open = false } }
        Rectangle {
            width: Math.min(330, root.width - 32); height: reBody.height + 36
            x: Math.max(16, Math.min(topBar.killAnchor.x - width + 40, root.width - width - 16))
            y: topBar.killAnchor.y
            radius: Theme.popupRadius; color: Theme.menuSurface; border.width: 1; border.color: Theme.alpha(Theme.danger, 0.55)
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
                    ActionButton { id: keepFrozen; objectName: "keepFrozen"; label: qsTr("Keep frozen"); height: 40; width: (parent.width - 8) / 2; onClicked: resumeConfirm.open = false }
                    ActionButton { objectName: "resumeJarvis"; label: qsTr("Resume JARVIS"); primary: true; height: 40; width: (parent.width - 8) / 2
                        onClicked: { resumeConfirm.open = false; bridge.resumeJarvis() } }
                }
            }
        }
    }
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
        visible: root.homeCanonical && (root.hideMode === "" || root.sideOverlay)
        x: 10
        y: root.topLine
        width: root.leftWidth
        height: root.height - y - 64
        tiles: root.sideTiles
        canBack: history.canBack; canForward: history.canForward; hideMode: root.hideMode
        onBackRequested: history.go(-1)
        onForwardRequested: history.go(1)
        onHideRequested: function (mode) { root.setHideMode(mode) }
        HoverHandler { id: sideHover; onHoveredChanged: if (!hovered) overlayGrace.restart() }
        now: clock.now
        activeKey: peekSwitch.running ? peekSwitch.key : peek.open ? peek.key : ""
        onPicked: function(key, cardY, cardHeight) { root.togglePeek(key, cardY) }
        onMenuRequested: function(card, anchor, canUp, canDown) { layoutMenus.openCard("rail", card, anchor, canUp, canDown) }
        onApprovalRequested: { root.closeContexts(); root.approvalRevealRequested() }
        conversationMenu: sharedConversationMenu
        // Q9: the Projects page, its create field focused.
        onNewProjectRequested: { bridge.showProject(""); if (pageLoader.item && pageLoader.item.focusCreate) pageLoader.item.focusCreate() }
    }
    // V5 / L4: while the sidebar is hidden its row floats at the same spot;
    // hovering it shows the sidebar over the content.
    Rectangle {
        id: floatBar
        objectName: "sidebarFloatBar"
        z: 419
        visible: root.homeCanonical && root.hideMode !== ""
        x: sidebar.x; y: root.topLine
        width: floatRow.implicitWidth + 2 * sidebar.pad; height: floatRow.height + 2 * sidebar.pad
        radius: 12; color: Theme.glassSecondaryShell; border.width: 1; border.color: Theme.glassEdge
        Sidebar.NavButtons {
            id: floatRow
            x: sidebar.pad; y: sidebar.pad; width: implicitWidth
            canBack: history.canBack; canForward: history.canForward; hideMode: root.hideMode
            onBackRequested: history.go(-1)
            onForwardRequested: history.go(1)
            onHideRequested: function (mode) { root.setHideMode(mode) }
        }
        HoverHandler {
            id: barHover
            onHoveredChanged: {
                if (hovered && root.overlayArmed) root.sideOverlay = true
                if (!hovered) { root.overlayArmed = true; overlayGrace.restart() }
            }
        }
    }
    // E1': the left column's inner edge (Edit Mode), like the right one.
    Rectangle {
        objectName: "leftWidthGrip"
        z: 425
        visible: bridge.editMode && sidebar.visible && root.hideMode === "" && !root.sideTiles
        x: sidebar.x + sidebar.width - 3; y: sidebar.y + sidebar.height / 2 - 30; width: 6; height: 60
        radius: 3; color: Theme.accent; opacity: leftGrip.pressed ? 1 : 0.6
        MouseArea {
            id: leftGrip
            anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.SizeHorCursor
            property real startX: 0; property real startWidth: 0
            onPressed: function(mouse) { startX = mapToItem(null, mouse.x, 0).x; startWidth = root.leftWidth }
            onPositionChanged: function(mouse) {
                if (pressed) root.leftDrag = root.clampWidth(startWidth + mapToItem(null, mouse.x, 0).x - startX, root.leftMin, root.leftMax)
            }
            onReleased: root.saveWidths()
            onCanceled: root.saveWidths()
        }
        Pill { objectName: "leftWidthLabel"; visible: leftGrip.pressed; dot: false; tint: Theme.accent
               anchors { left: parent.right; leftMargin: 10; verticalCenter: parent.verticalCenter }
               text: qsTr("%1 px").arg(root.leftWidth) }
    }
    // E2: while edit mode is on, "Düzenleme modu · Bitti" beside the brand ends it.
    Rectangle {
        objectName: "editModePill"
        z: 501
        visible: bridge.editMode
        x: topBar.brandRight + 24; y: 30
        width: pillRow.width + 24; height: 30; radius: 15
        color: Theme.alpha(Theme.accent, 0.12); border.width: 1; border.color: Theme.alpha(Theme.accent, 0.55)
        Row {
            id: pillRow
            x: 12; anchors.verticalCenter: parent.verticalCenter; spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Edit mode"); color: Theme.text; font: Theme.caption }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "·"; color: Theme.textMuted; font: Theme.caption }
            Text {
                objectName: "editModeDone"
                anchors.verticalCenter: parent.verticalCenter
                text: qsTr("Done"); font: Theme.captionStrong
                color: doneArea.containsMouse ? Theme.text : Theme.accent
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                Accessible.role: Accessible.Button; Accessible.name: qsTr("Done editing")
                Accessible.onPressAction: bridge.setEditMode(false)
                MouseArea { id: doneArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor; onClicked: bridge.setEditMode(false) }
            }
        }
    }
    // The one conversation menu (rename / archive / delete): the chat
    // header's "…" opens it on the open conversation, a sidebar row on that
    // row. And the one "Projeye bağla" dialog (the chat header opens it).
    ConversationMenu { id: sharedConversationMenu }
    AttachDialog { id: sharedAttachDialog }
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
    // The page shortcuts also work while a menu is open (MenuSurface holds them there).
    readonly property var menuShortcuts: [pageHome, pageTasks, pageAutomations, pageMemory, pageHistory, pageProjects]
    // Ctrl+1..4 belong to the background audition while it is enabled.
    Shortcut { id: pageHome; sequence: "Ctrl+1"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("home") }
    Shortcut { id: pageTasks; sequence: "Ctrl+2"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("tasks") }
    Shortcut { id: pageAutomations; sequence: "Ctrl+3"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("automations") }
    Shortcut { id: pageMemory; sequence: "Ctrl+4"; enabled: !root.backgroundAudition; onActivated: bridge.setPage("memory") }
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
        // Shown at once, gone at once after a hold (K2: no exit without an entry).
        Timer { id: auditionFade; interval: 1150; onTriggered: auditionLabel.opacity = 0 }
    }
    // V2.1: power down to the core on the minimize control, power up on every
    // return; over everything, never interactive (WindowPowerFx).
    WindowPowerFx {
        id: powerFx
        objectName: "windowPowerFx"
        anchors.fill: parent; z: 9900
        host: root
        coreX: topBar.coreCx; coreY: topBar.coreCy; coreSize: topBar.coreDiameter
        onMinimizeNow: bridge.minimizeWindow(root)
    }
    Shortcut { id: pageHistory; sequence: "Ctrl+5"; onActivated: bridge.setPage("history") }
    Shortcut { id: pageProjects; sequence: "Ctrl+6"; onActivated: bridge.setPage("projects") }
    Shortcut { sequence: "Escape"; onActivated: {
        if (attachmentPreview.open) attachmentPreview.close()
        else if (palette.open) palette.hide()
        else if (sharedConversationMenu.open) sharedConversationMenu.dismiss()
        else if (confirm.open) confirm.open = false
        else if (resumeConfirm.open) resumeConfirm.open = false
        else if (settings.open) settings.open = false
        else if (nav.open) nav.open = false
        else if (layoutMenus.open) layoutMenus.close()
        else if (peek.open || peekSwitch.running) root.closePeek()
        else if (!root.homePage && pageLoader.item) pageLoader.item.closePage()
        else if (bridge.editMode) bridge.setEditMode(false) } }
}
