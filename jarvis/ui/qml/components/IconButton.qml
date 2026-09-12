import QtQuick
import "../theme"

// A small icon-only control. The glyph is a text glyph or a short label;
// no icon pack. Frameless until hovered, like the reference window chrome.
Rectangle {
    id: button
    property string glyph: ""
    property string tip: ""
    property bool danger: false
    property bool active: false
    property bool framed: false
    signal clicked()
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: tip || glyph
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
    Rectangle { anchors.fill: parent; anchors.margins: -2; radius: parent.radius; color: "transparent"; border.color: Theme.accent; visible: button.activeFocus }

    Text {
        anchors.centerIn: parent
        text: button.glyph
        color: danger ? Theme.danger : (active ? tone : (hovered ? Theme.text : Theme.textLabel))
        font { family: Theme.font; pixelSize: 14 }
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
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
        Text { id: tipText; anchors.centerIn: parent; text: button.tip; color: Theme.text; font { family: Theme.font; pixelSize: 11 } }
    }
}
