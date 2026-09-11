import QtQuick
import "../theme"

// A small icon-only control. The glyph is a text glyph or a short label;
// no icon pack.
Rectangle {
    id: button
    property string glyph: ""
    property string tip: ""
    property bool danger: false
    property bool active: false
    signal clicked()

    implicitWidth: 30; implicitHeight: 30
    radius: Theme.radiusSmall
    color: active ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.18)
                  : (area.containsMouse ? Qt.rgba(1, 1, 1, 0.06) : "transparent")
    border.width: 1
    border.color: active ? Theme.accent : (area.containsMouse ? Theme.borderStrong : Theme.border)
    opacity: enabled ? 1 : 0.4
    Behavior on color { ColorAnimation { duration: 120 } }

    Text {
        anchors.centerIn: parent
        text: button.glyph
        color: danger ? Theme.danger : (active ? Theme.accent : Theme.text)
        font { family: Theme.font; pixelSize: 14 }
    }
    MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: button.clicked()
    }
    Rectangle {
        visible: area.containsMouse && button.tip !== ""
        anchors.top: parent.bottom; anchors.topMargin: 6; anchors.horizontalCenter: parent.horizontalCenter
        width: tipText.width + 14; height: tipText.height + 8
        radius: 6; color: Theme.surfaceRaised; border.color: Theme.border; border.width: 1
        z: 50
        Text { id: tipText; anchors.centerIn: parent; text: button.tip; color: Theme.text; font { family: Theme.font; pixelSize: 11 } }
    }
}
