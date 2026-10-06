import QtQuick
import "../theme"

// A tinted rounded square (or disc) holding one drawn icon (Icon.qml, by
// name): the row / card icon of the reference. `solid` = the saturated filled
// variant with a white icon; otherwise a translucent tinted well.
Rectangle {
    id: box
    property string icon: "diamond"
    property color tint: Theme.accent
    property bool solid: false
    property bool round: false
    property int iconSize: Math.round(width * 0.42)
    implicitWidth: 36; implicitHeight: 36
    radius: round ? width / 2 : Math.round(width * 0.27)
    color: solid ? Theme.alpha(tint, 0.82) : Theme.alpha(tint, 0.13)
    border.width: 1
    border.color: solid ? Theme.alpha(Qt.lighter(tint, 1.3), 0.7) : Theme.alpha(tint, 0.35)
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
        height: parent.height * 0.5; radius: parent.radius - 1
        gradient: Gradient {
            GradientStop { position: 0.0; color: solid ? Qt.rgba(1, 1, 1, 0.18) : Theme.alpha(tint, 0.10) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    Rectangle { z: -1; visible: solid; anchors.fill: parent; anchors.margins: -4; radius: parent.radius + 4; color: Theme.alpha(tint, 0.18) }
    Icon { anchors.centerIn: parent; name: box.icon; size: box.iconSize; tint: solid ? "#f4fbff" : box.tint }
}
