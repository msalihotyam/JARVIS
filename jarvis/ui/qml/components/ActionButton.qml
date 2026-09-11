import QtQuick
import "../theme"

Rectangle {
    id: button
    property string label: ""
    property bool primary: false
    property bool danger: false
    signal clicked()

    implicitWidth: text.width + 28; implicitHeight: 34
    radius: Theme.radiusSmall
    color: primary ? (danger ? Theme.danger : Theme.accent)
                   : (area.containsMouse ? Qt.rgba(1, 1, 1, 0.07) : "transparent")
    border.width: 1
    border.color: danger ? Theme.danger : (primary ? Theme.accent : Theme.borderStrong)
    opacity: enabled ? 1 : 0.4
    Behavior on color { ColorAnimation { duration: 120 } }
    Text {
        id: text
        anchors.centerIn: parent
        text: button.label
        color: primary ? Theme.bg0 : (danger ? Theme.danger : Theme.text)
        font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold }
    }
    MouseArea {
        id: area
        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
        onClicked: button.clicked()
    }
}
