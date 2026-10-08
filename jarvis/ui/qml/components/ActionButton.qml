import QtQuick
import "../theme"

// The one button. Idle / hover / pressed / focused / disabled are all
// designed; nothing falls back to a stock control.
Rectangle {
    id: button
    property string label: ""
    property bool primary: false
    property bool danger: false
    property bool compact: false
    property color accent: Theme.accent
    property string trailingIcon: ""          // a drawn icon after the label (Icon.qml name)
    signal clicked()
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: label
    Accessible.onPressAction: if (enabled) clicked()
    Keys.onReturnPressed: if (enabled) clicked()
    Keys.onSpacePressed: if (enabled) clicked()

    readonly property color tone: danger ? Theme.danger : accent
    readonly property bool hovered: area.containsMouse && enabled
    readonly property bool pressed: area.pressed && enabled

    implicitWidth: text.width + (trailingIcon !== "" ? 16 : 0) + (compact ? 22 : 30); implicitHeight: compact ? 30 : 36
    radius: Theme.radiusSmall
    color: primary ? (pressed ? Qt.darker(tone, 1.25) : hovered ? Qt.lighter(tone, 1.08) : tone)
                   : (pressed ? Theme.alpha(tone, 0.20) : hovered ? Theme.alpha(tone, 0.12) : Theme.alpha(Theme.text, 0.035))
    border.width: 1
    border.color: primary ? Theme.alpha(tone, 0.9) : (hovered || pressed ? Theme.alpha(tone, 0.6) : Theme.border)
    opacity: enabled ? 1 : 0.38
    scale: pressed ? 0.97 : 1
    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
    Behavior on scale { NumberAnimation { duration: Theme.pressMs; easing.type: Theme.ease } }

    // Top sheen on the primary fill, so it reads as a lit key rather than a flat block.
    Rectangle {
        visible: primary
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
        height: parent.height * 0.5; radius: parent.radius - 1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.rgba(1, 1, 1, 0.22) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    // Primary glow halo.
    Rectangle { z: -1; visible: primary; anchors.fill: parent; anchors.margins: -3; radius: parent.radius + 3; color: Theme.alpha(tone, hovered ? 0.30 : 0.16)
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
    // Focus ring (keyboard focus, when a parent forwards it): crisp, 2 px, outside the fill.
    Rectangle { visible: button.activeFocus; anchors.fill: parent; anchors.margins: -4; radius: parent.radius + 4; color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }

    Text {
        id: text
        anchors.centerIn: parent
        anchors.horizontalCenterOffset: button.trailingIcon !== "" ? -8 : 0
        text: button.label
        color: primary ? "#04101c" : (danger ? Theme.danger : (hovered ? Theme.text : Theme.textLabel))
        font: Theme.captionStrong
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    }
    Icon { visible: button.trailingIcon !== ""; name: button.trailingIcon; size: 12; tint: text.color
           anchors.left: text.right; anchors.leftMargin: 4; anchors.verticalCenter: text.verticalCenter }
    MouseArea {
        id: area
        anchors.fill: parent; hoverEnabled: true; cursorShape: button.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: if (button.enabled) button.clicked()
    }
}
