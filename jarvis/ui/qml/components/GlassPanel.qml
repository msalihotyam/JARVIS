import QtQuick
import "../theme"

// One glass surface with the HUD border treatment. No live blur: a
// translucent fill over the static background reads as glass at zero cost.
Rectangle {
    id: panel
    property string title: ""
    property alias headerRight: headerRight.data
    default property alias content: body.data
    property int padding: Theme.pad
    property bool raised: false

    radius: Theme.radius
    color: raised ? Theme.surfaceRaised : Theme.surface
    border.width: 1
    border.color: Theme.border
    clip: true

    // Corner accents: the short bright strokes of the reference chrome.
    Rectangle { x: 0; y: 0; width: 26; height: 1; color: Theme.borderStrong }
    Rectangle { x: 0; y: 0; width: 1; height: 26; color: Theme.borderStrong }
    Rectangle { anchors.right: parent.right; anchors.bottom: parent.bottom; width: 26; height: 1; color: Theme.borderStrong }
    Rectangle { anchors.right: parent.right; anchors.bottom: parent.bottom; width: 1; height: 26; color: Theme.borderStrong }

    Item {
        id: header
        visible: panel.title !== ""
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: panel.padding }
        height: visible ? 22 : 0
        Text {
            anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
            text: panel.title.toUpperCase()
            color: Theme.textMuted
            font { family: Theme.font; pixelSize: Theme.fsLabel; letterSpacing: Theme.labelSpacing; weight: Font.DemiBold }
        }
        Row { id: headerRight; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 8 }
    }
    Rectangle {
        visible: panel.title !== ""
        anchors { left: parent.left; right: parent.right; top: header.bottom; leftMargin: panel.padding; rightMargin: panel.padding; topMargin: 8 }
        height: 1; color: Theme.border
    }
    Item {
        id: body
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: header.bottom
                  leftMargin: panel.padding; rightMargin: panel.padding; bottomMargin: panel.padding
                  topMargin: panel.title !== "" ? 18 : panel.padding }
    }
}
