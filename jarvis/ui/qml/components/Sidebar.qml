import QtQuick
import "../theme"

// Gate 6F (L2): the one left panel of the home page. Top to bottom: the
// navigation (Genel, projects, the selected scope's conversations - F3), the
// summary tiles (SummaryRail) and the clock strip (LeftContextCard). The
// navigation takes whatever height the tiles and the strip leave.
Rectangle {
    id: sidebar
    objectName: "sidebar"
    property date now: new Date()
    property string activeKey: ""
    // The 1920-class sizes (the mockup's breakpoint), else the compact ones.
    readonly property bool big: (Window.width || 1920) >= 1600
    readonly property int pad: big ? 14 : 10
    readonly property alias rail: summaryRail
    readonly property alias nav: navigation
    signal picked(string key, real cardY, real cardHeight)
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)
    signal approvalRequested()

    radius: 12
    color: Theme.glassSecondaryShell
    border.width: 1; border.color: Theme.glassEdge
    // Inner light from the upper edge, as the other secondary glass has.
    Rectangle {
        anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1
        gradient: Gradient { GradientStop { position: 0; color: Theme.glassMicro } GradientStop { position: 0.35; color: "transparent" } }
    }

    // The navigation region (filled in F3): everything above the tiles.
    Item {
        id: navigation
        objectName: "sidebarNav"
        x: sidebar.pad; y: sidebar.pad
        width: sidebar.width - 2 * sidebar.pad
        height: Math.max(0, (summaryRail.height > 0 ? summaryRail.y - 8 : clock.y - 6) - y)
    }
    SummaryRail {
        id: summaryRail
        x: sidebar.pad; width: sidebar.width - 2 * sidebar.pad
        y: clock.y - 6 - height
        visible: count > 0
        big: sidebar.big
        activeKey: sidebar.activeKey
        onPicked: function(key, cardY, cardHeight) { sidebar.picked(key, summaryRail.y + cardY, cardHeight) }
        onMenuRequested: function(card, anchor, canUp, canDown) { sidebar.menuRequested(card, anchor, canUp, canDown) }
        onApprovalRequested: sidebar.approvalRequested()
    }
    LeftContextCard {
        id: clock
        x: sidebar.pad; width: sidebar.width - 2 * sidebar.pad
        height: 30
        y: sidebar.height - sidebar.pad - height + 4
        big: sidebar.big
        now: sidebar.now
    }
}
