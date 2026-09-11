import QtQuick
import "../theme"

// A small status pill: dot + letter-spaced label on a tinted well.
Rectangle {
    id: pill
    property string text: ""
    property color tint: Theme.success
    property bool dot: true
    implicitHeight: 22
    implicitWidth: row.width + 22
    radius: 11
    color: Theme.alpha(tint, 0.10)
    border.width: 1
    border.color: Theme.alpha(tint, 0.32)
    Row {
        id: row
        anchors.centerIn: parent
        spacing: 7
        Rectangle { visible: pill.dot; width: 6; height: 6; radius: 3; color: pill.tint; anchors.verticalCenter: parent.verticalCenter
            Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: pill.tint; opacity: 0.25 } }
        Text { anchors.verticalCenter: parent.verticalCenter; text: pill.text.toUpperCase(); color: pill.tint
            font { family: Theme.font; pixelSize: 10; letterSpacing: 1.8; weight: Font.DemiBold } }
    }
}
