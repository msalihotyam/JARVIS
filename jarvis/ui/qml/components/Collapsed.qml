import QtQuick
import "../theme"

// A collapsed selector: closed it shows ONLY what is selected; clicking opens
// the choices directly beneath it and choosing closes it again. One line at
// rest instead of every option always on screen.
//
// It owns the wheel while it is open, so turning the wheel over the list
// scrolls the list's own surface and never the conversation behind the panel.
Item {
    id: field
    property string title: ""
    property string meta: ""
    property bool open: false
    property int listHeight: 0            // 0 = as tall as the content
    default property alias choices: list.data

    width: parent ? parent.width : 0
    implicitHeight: head.height + (open ? listBox.height + 6 : 0)
    height: implicitHeight
    Behavior on height { NumberAnimation { duration: Theme.hoverMs; easing.type: Easing.OutCubic } }
    clip: true
    // The keyboard opens and closes it like the pointer does.
    activeFocusOnTab: enabled
    Accessible.role: Accessible.Button
    Accessible.name: title
    Keys.onReturnPressed: if (enabled) open = !open
    Keys.onSpacePressed: if (enabled) open = !open

    Rectangle {
        id: head
        width: parent.width; height: 52; radius: Theme.radiusSmall
        opacity: field.enabled ? 1 : 0.55
        // Idle: a nested control, one small tonal step from the panel. Hover
        // lifts the fill a step; open marks the edge as well.
        color: hit.containsMouse && field.enabled ? Theme.alpha(Theme.commandEdge, 0.08)
               : field.open ? Theme.alpha(Theme.commandEdge, 0.05) : Theme.glassNested
        border.width: 1
        border.color: field.open ? Theme.alpha(Theme.commandEdge, 0.42) : Theme.glassNestedEdge
        // Keyboard focus: a crisp 2 px outline inside the head (the field clips).
        Rectangle { anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1; color: "transparent"
                    border.width: 2; border.color: Theme.materialGlint; visible: field.activeFocus }
        Column {
            x: 12; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 42; spacing: 1
            Text { objectName: "collapsedTitle"; renderType: Theme.textRender; width: parent.width; elide: Text.ElideRight
                   text: field.title; color: "#e8f4ff"; font: Theme.subtitle }
            Text { objectName: "collapsedMeta"; renderType: Theme.textRender; visible: field.meta !== ""; width: parent.width; elide: Text.ElideRight
                   text: field.meta; color: Theme.textMuted; font: Theme.caption }
        }
        // The 10 px chevron, reversed when the choices are showing.
        Canvas {
            id: mark
            anchors.verticalCenter: parent.verticalCenter
            x: parent.width - 22; width: 10; height: 6
            rotation: field.open ? 180 : 0
            Behavior on rotation { NumberAnimation { duration: Theme.dur(160); easing.type: Easing.OutCubic } }
            onPaint: { var c = getContext("2d"); c.reset(); c.strokeStyle = Theme.textLabel; c.lineWidth = 1.4
                       c.beginPath(); c.moveTo(1, 1); c.lineTo(5, 5); c.lineTo(9, 1); c.stroke() }
        }
        MouseArea { id: hit; anchors.fill: parent; hoverEnabled: true; enabled: field.enabled
                    cursorShape: Qt.PointingHandCursor; onClicked: field.open = !field.open }
    }
    Item {
        id: listBox
        y: head.height + 6
        width: parent.width
        height: field.listHeight > 0 ? Math.min(field.listHeight, list.height) : list.height
        visible: field.open
        Flickable {
            anchors.fill: parent
            contentHeight: list.height
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentHeight > height + 1
            Column { id: list; width: parent.width; spacing: 4 }
        }
        // Whatever the list does with the wheel, it stops here.
        WheelHandler { }
    }
}
