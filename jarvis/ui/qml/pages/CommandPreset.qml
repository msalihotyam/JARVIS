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
    signal dismissFixture()
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)
    signal cardsRequested(Item anchor)
    signal resetRequested(Item anchor)
    readonly property bool wide: width >= 1400
    readonly property bool showBay: wide && !bridge.focusMode
    readonly property real navWidth: bridge.focusMode ? 0 : wide ? 204 : 60
    readonly property real railWidth: bridge.focusMode ? 0 : Math.min(440, Math.max(265, width * bridge.columnWidth))
    readonly property real dockHeight: bridge.focusMode ? 0 : height >= 720 ? 180 : 150
    GlassPanel {
        id: navigation; x: 0; y: 0; width: cockpit.navWidth; height: upper.height
        visible: width > 0; level: 1; padding: cockpit.wide ? 12 : 5; brackets: true
        Column { width: parent.width; spacing: 6
            Repeater { model: [{label:qsTr("Home"),page:"home",glyph:"⌂"},{label:qsTr("Tasks"),page:"tasks",glyph:"◇"},{label:qsTr("Automations"),page:"automations",glyph:"⟳"},{label:qsTr("Memory"),page:"memory",glyph:"▤"},{label:qsTr("History"),page:"history",glyph:"◷"}]
                Rectangle { width: parent.width; height: 43; radius: 5; color: bridge.page === modelData.page ? Theme.alpha(Theme.accent, 0.10) : "transparent"
                    Text { x: cockpit.wide ? 10 : 12; anchors.verticalCenter: parent.verticalCenter; text: modelData.glyph; color: Theme.accent; font.pixelSize: 21 }
                    Text { visible: cockpit.wide; x: 43; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: Theme.textLabel; font { family: Theme.font; pixelSize: 13 } }
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
            Text { text: (new Date().getHours() < 12 ? qsTr("Good morning, %1.") : new Date().getHours() < 18 ? qsTr("Good afternoon, %1.") : qsTr("Good evening, %1.")).arg(bridge.operatorName); color: Theme.text; font { family: Theme.font; pixelSize: 29; weight: Font.DemiBold } }
            Text { text: qsTr("Here to help you think, plan, and get things done."); color: Theme.textLabel; font { family: Theme.font; pixelSize: 15 } }
        }
        Item {
            id: conversation
            anchors { left: parent.left; top: greeting.bottom; bottom: parent.bottom; topMargin: 25 }
            width: cockpit.showBay ? parent.width * 0.53 - 20 : parent.width
            Text { visible: bridge.feed.length === 0; width: parent.width; wrapMode: Text.Wrap; text: qsTr("Waiting for your request. The model proposes; you remain in control of each authorized action."); color: Theme.textLabel; font { family: Theme.font; pixelSize: 15 } }
            FeedView { anchors.fill: parent; visible: bridge.feed.length > 0 }
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
                    IconBox { width: 58; height: 58; anchors.horizontalCenter: parent.horizontalCenter; glyph: "◇"; tint: Theme.accent }
                    Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; text: qsTr("No action awaiting approval"); color: Theme.text; font { family: Theme.font; pixelSize: 18; weight: Font.DemiBold } }
                    Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; text: qsTr("When an action needs permission, its exact scope, destination and outbound preview appear here. Nothing is pre-authorized by this panel."); color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
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
        Row { width: dock.contentWidth; height: dock.height; spacing: 12
        GlassPanel {
            width: dock.contentWidth * 0.29; height: parent.height; title: qsTr("System status"); badge: bridge.executionEnabled ? qsTr("running") : qsTr("frozen"); padding: 14; brackets: true
            Row { anchors { left: parent.left; right: parent.right; top: parent.top } spacing: 12
                Repeater { model: ["CPU", "RAM", qsTr("Disk")]
                    Item { width: (parent.width - 24) / 3; height: 77
                        Rectangle { anchors.centerIn: parent; width: 65; height: 65; radius: 32.5; color: "transparent"; border.width: 6; border.color: Theme.alpha(Theme.textDim, 0.3) }
                        Column { anchors.centerIn: parent; spacing: 1
                            Text { anchors.horizontalCenter: parent.horizontalCenter; text: "—"; color: Theme.textLabel; font.pixelSize: 23 }
                            Text { anchors.horizontalCenter: parent.horizontalCenter; text: modelData; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
                        }
                    }
                }
            }
            Text { anchors.bottom: parent.bottom; text: qsTr("Telemetry not measured"); color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
        }
        GlassPanel {
            width: dock.contentWidth * 0.21; height: parent.height; title: qsTr("Agent capacity"); padding: 14
            Column { width: parent.width; spacing: 12
                Repeater { model: [qsTr("Coding agents"), qsTr("Research agents"), qsTr("Browser agents")]
                    Row { width: parent.width; spacing: 8
                        IconBox { width: 25; height: 25; glyph: "◇"; tint: Theme.textMuted }
                        Column { spacing: 1
                            Text { text: modelData; color: Theme.textLabel; font { family: Theme.font; pixelSize: 11 } }
                            Text { text: qsTr("Unavailable in this build"); color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                        }
                    }
                }
            }
        }
        GlassPanel {
            width: dock.contentWidth * 0.25; height: parent.height; title: qsTr("Intelligence feed"); padding: 14
            ListView { anchors.fill: parent; clip: true; model: bridge.activity
                delegate: Item { width: ListView.view.width; height: 31
                    Rectangle { width: 5; height: 5; radius: 2.5; y: 10; color: modelData.kind === "approval" ? Theme.warning : Theme.accent }
                    Text { x: 14; width: parent.width - 16; y: 3; text: modelData.text; elide: Text.ElideRight; color: Theme.textLabel; font { family: Theme.font; pixelSize: 11 } }
                    Rectangle { y: 30; width: parent.width; height: 1; color: Theme.border }
                }
            }
        }
        GlassPanel {
            width: dock.contentWidth * 0.25 - 36; height: parent.height; title: qsTr("Quick commands"); padding: 14
            Grid { width: parent.width; columns: 2; spacing: 8
                Repeater { model: [{label:qsTr("Tasks"),page:"tasks"},{label:qsTr("History"),page:"history"},{label:qsTr("Automations"),page:"automations"},{label:qsTr("Memory"),page:"memory"}]
                    ActionButton { width: (parent.width - 8) / 2; label: modelData.label; compact: true; onClicked: bridge.setPage(modelData.page) }
                }
                ActionButton { width: (parent.width - 8) / 2; label: qsTr("Voice — N/A"); compact: true; enabled: false }
                ActionButton { width: (parent.width - 8) / 2; label: qsTr("Calendar — N/A"); compact: true; enabled: false }
            }
        }
        }
    }
    Composer { id: composer; objectName: "commandComposer"; height: 60
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: cockpit.navWidth ? cockpit.navWidth + 24 : 0; rightMargin: cockpit.wide ? 180 : 0 }
    }
}
