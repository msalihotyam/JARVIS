import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// A distinct composition: navigation, conversation + authority bay,
// intelligence rail and a low instrument dock. No product authority here.
Item {
    id: cockpit
    objectName: "commandPreset"
    property bool fixtureApproval: false
    property Item conversationMenu: null
    property var attachDialog: null
    signal dismissFixture()
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)
    signal cardsRequested(Item anchor)
    signal resetRequested(Item anchor)
    readonly property bool wide: width >= 1400
    readonly property bool showBay: wide && !bridge.focusMode
    readonly property real navWidth: bridge.focusMode ? 0 : wide ? 204 : 60
    readonly property real railWidth: bridge.focusMode ? 0 : Math.min(440, Math.max(265, width * bridge.columnWidth))
    readonly property real dockHeight: bridge.focusMode ? 0 : height >= 720 ? 180 : 150
    // K6: the greeting follows the real time of day (a minute clock a test can set).
    property date now: new Date()
    Timer { interval: 60000; running: cockpit.visible; repeat: true; triggeredOnStart: true; onTriggered: cockpit.now = new Date() }
    GlassPanel {
        id: navigation; x: 0; y: 0; width: cockpit.navWidth; height: upper.height
        visible: width > 0; level: 1; padding: cockpit.wide ? 12 : 5; brackets: true
        Column { width: parent.width; spacing: 6
            Repeater { model: [{label:qsTr("Home"),page:"home",icon:"home"},{label:qsTr("Tasks"),page:"tasks",icon:"tasks"},{label:qsTr("Automations"),page:"automations",icon:"automations"},{label:qsTr("Memory"),page:"memory",icon:"memory"},{label:qsTr("History"),page:"history",icon:"history"},{label:qsTr("Projects"),page:"projects",icon:"project"}]
                Rectangle { width: parent.width; height: 43; radius: 5; color: bridge.page === modelData.page ? Theme.alpha(Theme.accent, 0.10) : "transparent"
                    Icon { x: cockpit.wide ? 10 : 12; anchors.verticalCenter: parent.verticalCenter; name: modelData.icon; size: 20; tint: Theme.accent }
                    Text { visible: cockpit.wide; x: 43; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: Theme.textLabel; font: Theme.caption }
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setPage(modelData.page) }
                }
            }
        }
    }
    Item {
        id: upper
        x: cockpit.navWidth ? cockpit.navWidth + 24 : 0; y: 0
        width: cockpit.width - x - (cockpit.railWidth ? cockpit.railWidth + 20 : 0)
        height: cockpit.height - composer.height - 20 - (cockpit.dockHeight ? cockpit.dockHeight + 18 : 0)
        Column {
            id: greeting; width: parent.width; spacing: 5
            Text { objectName: "presetGreeting"; color: Theme.text; font: Theme.face(29, true)
                   text: { var h = cockpit.now.getHours()
                           return (h >= 5 && h < 12 ? qsTr("Good morning, %1.") : h >= 12 && h < 18 ? qsTr("Good afternoon, %1.") : qsTr("Good evening, %1.")).arg(bridge.operatorName) } }
            Text { text: qsTr("Here to help you think, plan, and get things done."); color: Theme.textLabel; font: Theme.lead }
        }
        Item {
            id: conversation
            anchors { left: parent.left; top: greeting.bottom; bottom: parent.bottom; topMargin: 25 }
            width: cockpit.showBay ? parent.width * 0.53 - 20 : parent.width
            // Gate 6F (Q2): the chat header; here its chip opens the scope menu.
            ChatHeader { id: chatHeader; width: parent.width; scopeMenu: true
                         conversationMenu: cockpit.conversationMenu; attachDialog: cockpit.attachDialog }
            Text { y: chatHeader.height + 10; visible: bridge.feed.length === 0; width: parent.width; wrapMode: Text.Wrap; text: qsTr("Waiting for your request. The model proposes; you remain in control of each authorized action."); color: Theme.textLabel; font: Theme.lead }
            FeedView { anchors.fill: parent; anchors.topMargin: chatHeader.height + 10; visible: bridge.feed.length > 0 }
            Flow { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } spacing: 8
                Repeater { model: bridge.clarification.choices || []; ActionButton { label: modelData.label; enabled: !bridge.busy; onClicked: bridge.chooseOption(modelData.label) } }
            }
        }
        Item {
            id: bay
            objectName: "commandAuthorityBay"
            visible: cockpit.showBay
            anchors { right: parent.right; top: greeting.bottom; bottom: parent.bottom; topMargin: 25 }
            width: parent.width * 0.47
            ApprovalCard { anchors.fill: parent; visible: bridge.approvalPending || cockpit.fixtureApproval; fixture: cockpit.fixtureApproval && !bridge.approvalPending; onDismissFixture: cockpit.dismissFixture() }
            GlassPanel { anchors.fill: parent; visible: !bridge.approvalPending && !cockpit.fixtureApproval; title: qsTr("Authorization gate"); brackets: true; border.color: Theme.borderStrong
                Column { anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter } spacing: 22
                    IconBox { width: 58; height: 58; anchors.horizontalCenter: parent.horizontalCenter; icon: "diamond"; tint: Theme.accent }
                    Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; text: qsTr("No action awaiting approval"); color: Theme.text; font: Theme.title }
                    Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; text: qsTr("When an action needs permission, its exact scope, destination and outbound preview appear here. Nothing is pre-authorized by this panel."); color: Theme.textMuted; font: Theme.caption }
                    Pill { anchors.horizontalCenter: parent.horizontalCenter; text: qsTr("Standing by"); tint: Theme.accent }
                }
            }
        }
    }
    PanelColumn {
        id: rail; objectName: "commandPanelColumn"
        commandMode: true
        onMenuRequested: function(card, anchor, canUp, canDown) { cockpit.menuRequested(card, anchor, canUp, canDown) }
        onCardsRequested: function(anchor) { cockpit.cardsRequested(anchor) }
        onResetRequested: function(anchor) { cockpit.resetRequested(anchor) }
        anchors { top: parent.top; right: parent.right; bottom: composer.top; bottomMargin: 20 }
        width: cockpit.railWidth; visible: width > 0
    }
    Rectangle {
        visible: bridge.editMode && rail.visible
        x: rail.x - 12; width: 5; height: 60; radius: 2; anchors.verticalCenter: rail.verticalCenter; color: Theme.accent
        MouseArea { anchors.fill: parent; anchors.margins: -5; cursorShape: Qt.SizeHorCursor
            property real startX: 0; property real startWidth: 0
            onPressed: function(mouse) { startX = mouse.x; startWidth = bridge.columnWidth }
            onPositionChanged: function(mouse) { if (pressed) bridge.setColumnWidth(startWidth - (mouse.x - startX) / cockpit.width) }
        }
    }
    Flickable {
        id: dock
        objectName: "commandDock"
        x: 0; y: upper.height + 18; width: upper.x + upper.width; height: cockpit.dockHeight
        visible: height > 0; clip: true; boundsBehavior: Flickable.StopAtBounds
        contentWidth: Math.max(width, 1120); contentHeight: height
        ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }
        // K6: only real panels - no "agent capacity" or N/A placeholders for features that do not exist.
        Row { width: dock.contentWidth; height: dock.height; spacing: 12
        GlassPanel {
            id: statusPanel
            width: dock.contentWidth * 0.36; height: parent.height; title: qsTr("System status"); badge: bridge.executionEnabled ? qsTr("running") : qsTr("frozen"); padding: 14; brackets: true
            // Real machine load only: each ring is drawn to its measured share; an unread figure has no ring.
            readonly property var load: bridge.telemetry || ({})
            readonly property bool measured: load.available === true
            Row { id: loadRow; objectName: "presetGauges"; anchors { left: parent.left; right: parent.right; top: parent.top } spacing: 12
                visible: statusPanel.measured
                Repeater { model: [{ label: "CPU", key: "cpu" }, { label: "RAM", key: "ram" }, { label: qsTr("Disk"), key: "disk" }]
                    Item { id: gauge; width: (loadRow.width - 24) / 3; height: 77
                        readonly property var reading: statusPanel.load[modelData.key]
                        readonly property real share: typeof reading === "number" ? Math.max(0, Math.min(1, reading / 100)) : -1
                        Rectangle { objectName: "presetGaugeTrack_" + modelData.key; visible: gauge.share >= 0
                                    anchors.centerIn: parent; width: 65; height: 65; radius: 32.5; color: "transparent"; border.width: 6; border.color: Theme.alpha(Theme.accent, 0.12) }
                        Canvas {
                            objectName: "presetGauge_" + modelData.key
                            anchors.centerIn: parent; width: 65; height: 65; visible: share >= 0
                            property real share: gauge.share
                            onShareChanged: requestPaint()
                            onPaint: { var c = getContext("2d"); c.reset(); c.lineWidth = 6; c.strokeStyle = Theme.accent
                                       c.beginPath(); c.arc(width / 2, height / 2, width / 2 - 3, -Math.PI / 2, -Math.PI / 2 + share * 2 * Math.PI); c.stroke() }
                        }
                        Column { anchors.centerIn: parent; spacing: 1
                            Text { anchors.horizontalCenter: parent.horizontalCenter; text: gauge.share >= 0 ? Math.round(gauge.reading) + "%" : "—"; color: Theme.text; font.pixelSize: 18 }
                            Text { anchors.horizontalCenter: parent.horizontalCenter; text: modelData.label; color: Theme.textMuted; font: Theme.caption }
                        }
                    }
                }
            }
            Text { objectName: "presetGaugesEmpty"; visible: !statusPanel.measured; width: parent.width; wrapMode: Text.Wrap
                   text: qsTr("This computer's load cannot be read, so no gauge is shown."); color: Theme.textMuted; font: Theme.caption }
        }
        GlassPanel {
            width: dock.contentWidth * 0.36; height: parent.height; title: qsTr("Intelligence feed"); padding: 14
            ListView { anchors.fill: parent; clip: true; model: bridge.activity
                delegate: Item { width: ListView.view.width; height: 31
                    Rectangle { width: 5; height: 5; radius: 2.5; y: 10; color: modelData.kind === "approval" ? Theme.warning : Theme.accent }
                    Text { x: 14; width: parent.width - 16; y: 3; text: modelData.text; elide: Text.ElideRight; color: Theme.textLabel; font: Theme.caption }
                    Rectangle { y: 30; width: parent.width; height: 1; color: Theme.border }
                }
            }
        }
        GlassPanel {
            objectName: "presetQuickCommands"
            width: dock.contentWidth * 0.28 - 24; height: parent.height; title: qsTr("Quick commands"); padding: 14
            Grid { width: parent.width; columns: 2; spacing: 8
                Repeater { model: [{label:qsTr("Tasks"),page:"tasks"},{label:qsTr("History"),page:"history"},{label:qsTr("Automations"),page:"automations"},{label:qsTr("Memory"),page:"memory"}]
                    ActionButton { width: (parent.width - 8) / 2; label: modelData.label; compact: true; onClicked: bridge.setPage(modelData.page) }
                }
            }
        }
        }
    }
    Composer { id: composer; objectName: "commandComposer"; minHeight: 60; maxHeight: 60
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: cockpit.navWidth ? cockpit.navWidth + 24 : 0; rightMargin: cockpit.wide ? 180 : 0 }
    }
}
