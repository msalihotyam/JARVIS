import QtQuick
import "../theme"
import "../components"

// Edit Mode's popovers, one at a time: a card's layout menu (both regions
// share its vocabulary), the Cards inventory of hidden cards, and the Reset
// layout confirmation. Everything here changes layout preferences only -
// never Memory, tasks, workflows, events or permissions. Each popover is
// placed against the control that opened it and kept inside the window.
Item {
    id: menus
    objectName: "layoutMenus"
    anchors.fill: parent
    property bool railShown: true
    property bool commandMode: false
    property string mode: ""                 // "", "card", "cards" or "reset"
    property string region: ""
    property string card: ""
    property bool canUp: false
    property bool canDown: false
    property rect anchorRect: Qt.rect(0, 0, 0, 0)
    readonly property bool open: mode !== ""
    visible: open

    // The sidebar's four tiles (Gate 6F); Updates has no tile (Q4), so it is not offered.
    readonly property var railNames: ({ tasks: qsTr("Tasks"), focus: qsTr("Focus"), plan: qsTr("Plan"), memory: qsTr("Memory") })
    readonly property var stackNames: commandMode
        ? ({ system: qsTr("Model & execution"), workflow: qsTr("Active task"), activity: qsTr("Recent actions"), automations: qsTr("Memory context") })
        : ({ system: qsTr("System status"), workflow: qsTr("Active workflow"), activity: qsTr("Live intelligence feed"), automations: qsTr("Automations") })
    // Layout cards only: these counts are never memories, tasks or events.
    readonly property var railTiles: bridge.railCards.filter(function(c) { return c.id in menus.railNames })
    readonly property var railHidden: railShown ? railTiles.filter(function(c) { return !c.visible }) : []
    readonly property int railVisible: railShown ? railTiles.length - railHidden.length : 0
    readonly property var stackHidden: bridge.panels.filter(function(p) { return !p.visible })
    readonly property int stackVisible: bridge.panels.length - stackHidden.length

    function place(anchor) {
        var p = anchor.mapToItem(menus, 0, 0)
        anchorRect = Qt.rect(p.x, p.y, anchor.width, anchor.height)
    }
    function openCard(region, card, anchor, canUp, canDown) {
        menus.region = region; menus.card = card; menus.canUp = canUp; menus.canDown = canDown
        place(anchor); mode = "card"
    }
    function toggle(which, anchor) {
        if (mode === which) { close(); return }
        place(anchor); mode = which
    }
    function close() { mode = "" }
    onModeChanged: if (open) fadeIn.restart()
    Connections { target: bridge; function onPrefsChanged() { if (!bridge.editMode) menus.close() } }

    // Outside click closes the popover; it does not reach what lies beneath.
    MouseArea { anchors.fill: parent; onClicked: menus.close(); onWheel: function(wheel) { wheel.accepted = true } }

    Rectangle {
        id: surface
        objectName: "layoutPopover"
        width: menus.mode === "card" ? 208 : 300
        height: body.height + 16
        // Left-rail anchors open rightwards, right-side anchors leftwards;
        // below the control when there is room, above it otherwise.
        readonly property bool leftward: menus.anchorRect.x + menus.anchorRect.width / 2 > menus.width / 2
        readonly property real wantX: leftward ? menus.anchorRect.x + menus.anchorRect.width - width : menus.anchorRect.x
        readonly property real below: menus.anchorRect.y + menus.anchorRect.height + 6
        x: Math.max(8, Math.min(wantX, menus.width - width - 8))
        y: Math.max(8, below + height + 8 <= menus.height ? below : menus.anchorRect.y - height - 6)
        radius: 12
        color: Theme.glassMenu
        border.width: 1; border.color: Theme.glassEdgeStrong
        MouseArea { anchors.fill: parent }        // clicks inside never close it
        NumberAnimation { id: fadeIn; target: surface; property: "opacity"; from: 0; to: 1; duration: 140; easing.type: Easing.OutCubic }

        Column {
            id: body
            x: 8; y: 8; width: parent.width - 16
            Loader { width: parent.width; sourceComponent: menus.mode === "card" ? cardMenu : menus.mode === "cards" ? inventory : menus.mode === "reset" ? resetConfirm : null }
        }
    }

    // -- one menu row: icon, label; disabled at a boundary --------------------
    component MenuRow: Rectangle {
        id: row
        property string icon: ""
        property string label: ""
        signal triggered()
        width: parent ? parent.width : 0; height: 40; radius: 8
        color: area.containsMouse && enabled ? Theme.alpha(Theme.accent, 0.12) : "transparent"
        opacity: enabled ? 1 : 0.4
        activeFocusOnTab: enabled
        Accessible.role: Accessible.MenuItem
        Accessible.name: label
        Keys.onReturnPressed: if (enabled) triggered()
        Keys.onSpacePressed: if (enabled) triggered()
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Rectangle { anchors.fill: parent; radius: parent.radius; color: "transparent"; border.color: Theme.accent; visible: row.activeFocus }
        Icon { x: 14; anchors.verticalCenter: parent.verticalCenter; name: row.icon; size: 16; tint: Theme.accent }
        Text { x: 42; anchors.verticalCenter: parent.verticalCenter; text: row.label; color: Theme.text; font: Theme.uiLabel }
        MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; cursorShape: row.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: if (row.enabled) row.triggered() }
    }

    Component {
        id: cardMenu
        Column {
            objectName: "layoutCardMenu"
            MenuRow { objectName: "menuMoveUp"; icon: "arrow-up"; label: qsTr("Move up"); enabled: menus.canUp
                onTriggered: { bridge.moveCard(menus.region, menus.card, -1); menus.close() } }
            MenuRow { objectName: "menuMoveDown"; icon: "arrow-down"; label: qsTr("Move down"); enabled: menus.canDown
                onTriggered: { bridge.moveCard(menus.region, menus.card, 1); menus.close() } }
            Item { width: parent.width; height: 9
                Rectangle { anchors.verticalCenter: parent.verticalCenter; x: 8; width: parent.width - 16; height: 1; color: Theme.glassSeparator } }
            MenuRow { objectName: "menuHide"; icon: "hide"; label: qsTr("Hide from layout")
                onTriggered: { bridge.setCardVisible(menus.region, menus.card, false); menus.close() } }
        }
    }

    Component {
        id: inventory
        Column {
            objectName: "layoutInventory"
            spacing: 0
            Item { width: parent.width; height: 52
                Text { x: 8; y: 8; text: qsTr("Cards"); color: Theme.text; font: Theme.uiValue }
                Text { objectName: "inventoryCount"; x: 8; y: 30
                       text: qsTr("%1 visible · %2 hidden").arg(menus.railVisible + menus.stackVisible).arg(menus.railHidden.length + menus.stackHidden.length)
                       color: Theme.textMuted; font: Theme.metadata }
            }
            Text { visible: menus.railHidden.length + menus.stackHidden.length === 0; x: 8; height: 36; verticalAlignment: Text.AlignVCenter
                   text: qsTr("All cards are visible"); color: Theme.textLabel; font: Theme.uiLabel }
            Repeater {
                model: menus.railHidden.length + menus.stackHidden.length === 0 ? [] : (menus.railShown ? ["rail", "stack"] : ["stack"])
                Column {
                    width: parent.width
                    readonly property var hidden: modelData === "rail" ? menus.railHidden : menus.stackHidden
                    Rectangle { width: parent.width; height: 1; color: Theme.glassSeparator }
                    Text { x: 8; height: 30; verticalAlignment: Text.AlignVCenter; text: modelData === "rail" ? qsTr("LEFT RAIL") : qsTr("RIGHT STACK")
                           color: Theme.textLabel; font: Theme.label }
                    Text { visible: parent.hidden.length === 0; x: 8; height: 30; verticalAlignment: Text.AlignTop
                           text: qsTr("No hidden cards"); color: Theme.textMuted; font: Theme.metadata }
                    Repeater {
                        model: parent.hidden
                        Item {
                            width: parent.width; height: 48
                            readonly property bool rail: modelData.id in menus.railNames
                            Rectangle { id: sock; visible: parent.rail; x: 8; anchors.verticalCenter: parent.verticalCenter; width: 30; height: 30; radius: 8
                                color: "#0a1f36"; border.width: 1; border.color: Theme.alpha("#299ddf", 0.42)
                                SummaryIcon { anchors.centerIn: parent; icon: modelData.id; tint: "#b8e7ff" } }
                            Column { x: parent.rail ? 48 : 8; anchors.verticalCenter: parent.verticalCenter; width: addButton.x - x - 8
                                Text { width: parent.width; elide: Text.ElideRight; color: Theme.text; font: Theme.uiLabel
                                       text: parent.parent.rail ? menus.railNames[modelData.id] : menus.stackNames[modelData.id] }
                                Text { text: qsTr("Hidden"); color: Theme.textMuted; font: Theme.metadata }
                            }
                            ActionButton { id: addButton; objectName: "addToLayout_" + modelData.id; compact: true; label: qsTr("Add to layout")
                                anchors.right: parent.right; anchors.rightMargin: 4; anchors.verticalCenter: parent.verticalCenter; height: 32
                                onClicked: bridge.setCardVisible(parent.rail ? "rail" : "stack", modelData.id, true) }
                        }
                    }
                }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.glassSeparator }
            Text { x: 8; width: parent.width - 16; height: 34; verticalAlignment: Text.AlignVCenter; wrapMode: Text.Wrap
                   text: qsTr("Hidden cards keep their data and capability state."); color: Theme.textMuted; font: Theme.metadata }
        }
    }

    Component {
        id: resetConfirm
        Column {
            objectName: "layoutResetConfirm"
            spacing: 10
            Item { width: parent.width; height: 2 }
            Text { x: 8; text: qsTr("Reset layout?"); color: Theme.text; font: Theme.uiValue }
            Text { x: 8; width: parent.width - 16; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.metadata
                   text: qsTr("Restore default card order, visibility and sizes. Data and capabilities stay unchanged.") }
            Row {
                x: 8; width: parent.width - 16; spacing: 8
                ActionButton { objectName: "resetCancel"; label: qsTr("Cancel"); height: 36; width: (parent.width - 8) / 2; onClicked: menus.close() }
                ActionButton { objectName: "resetConfirm"; label: qsTr("Reset layout"); primary: true; height: 36; width: (parent.width - 8) / 2
                    onClicked: { bridge.resetLayout(); menus.close() } }
            }
            Item { width: parent.width; height: 2 }
        }
    }
}
