import QtQuick
import "../theme"

// A small icon-only control: one drawn icon (Icon.qml, by name; Gate 6G K1)
// or drawn children of its own. Frameless until hovered, like the reference
// window chrome.
Rectangle {
    id: button
    property string icon: ""
    property real iconSize: Math.round(Math.min(width, height) * 0.47)
    property string tip: ""
    property bool danger: false
    property bool active: false
    property bool framed: false
    signal clicked()
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: tip || icon
    Accessible.onPressAction: if (enabled) clicked()
    Keys.onReturnPressed: if (enabled) clicked()
    Keys.onSpacePressed: if (enabled) clicked()

    readonly property bool hovered: area.containsMouse && enabled
    readonly property bool pressed: area.pressed && enabled
    readonly property color tone: danger ? Theme.danger : Theme.accent

    implicitWidth: 34; implicitHeight: 34
    radius: Theme.radiusSmall
    color: active ? Theme.alpha(tone, 0.16)
                  : pressed ? Theme.alpha(tone, 0.14)
                  : hovered ? Theme.alpha(Theme.text, 0.07)
                  : framed ? Theme.alpha(Theme.text, 0.035) : "transparent"
    border.width: 1
    border.color: active ? Theme.alpha(tone, 0.7) : hovered ? Theme.alpha(tone, 0.45) : framed ? Theme.border : "transparent"
    opacity: enabled ? 1 : 0.38
    scale: pressed ? 0.94 : 1
    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
    Behavior on scale { NumberAnimation { duration: 90 } }
    // Keyboard focus: a crisp 2 px outline just outside the control, distinct from hover and active.
    Rectangle { anchors.fill: parent; anchors.margins: -3; radius: parent.radius + 2; color: "transparent"; border.width: 2; border.color: Theme.materialGlint; visible: button.activeFocus }

    Icon {
        anchors.centerIn: parent
        visible: button.icon !== ""
        name: button.icon; size: button.iconSize
        tint: danger ? Theme.danger : (active ? tone : (hovered ? Theme.text : Theme.textLabel))
        Behavior on tint { ColorAnimation { duration: Theme.hoverMs } }
    }
    MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: button.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: if (button.enabled) button.clicked()
    }
    Rectangle {
        visible: hovered && button.tip !== ""
        anchors.top: parent.bottom; anchors.topMargin: 8; anchors.horizontalCenter: parent.horizontalCenter
        width: tipText.width + 16; height: tipText.height + 10
        radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.color: Theme.borderStrong; border.width: 1
        z: 50
        Text { id: tipText; anchors.centerIn: parent; text: button.tip; color: Theme.text; font: Theme.caption }
    }
}
