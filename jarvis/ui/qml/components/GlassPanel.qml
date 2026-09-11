import QtQuick
import "../theme"

// One layered glass surface. No live blur: depth comes from a soft drop
// shadow beneath, a translucent fill, a top sheen, a luminous top-left
// edge highlight and a thin border - all plain rectangles, all static.
Rectangle {
    id: panel
    property string title: ""
    property string badge: ""
    property color badgeColor: Theme.success
    property alias headerRight: headerRight.data
    default property alias content: body.data
    property int padding: Theme.pad
    property bool raised: false
    property bool shadow: true

    radius: Theme.radius
    color: raised ? Theme.surfaceRaised : Theme.surface
    border.width: 1
    border.color: Theme.border

    // Depth: two soft shadow plates behind the panel (drawn below it: z < 0).
    Rectangle { z: -2; visible: panel.shadow; anchors.fill: parent; anchors.margins: -2; anchors.topMargin: 6; radius: panel.radius + 2; color: "#000000"; opacity: 0.22 }
    Rectangle { z: -1; visible: panel.shadow; anchors.fill: parent; anchors.topMargin: 3; radius: panel.radius; color: "#000000"; opacity: 0.30 }

    // Material: a top sheen, and a faint left light, so the glass reads as lit
    // from the top-left rather than as a flat tint.
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
        height: Math.min(parent.height * 0.5, 220); radius: panel.radius - 1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.sheen }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    Rectangle {
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom; margins: 1 }
        width: Math.min(parent.width * 0.45, 260); radius: panel.radius - 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: Theme.alpha(Theme.accent, 0.035) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    // Edge highlights: the luminous top-left edge of the reference chrome.
    Rectangle {
        x: panel.radius; y: 0; height: 1; width: Math.min(parent.width * 0.55, 420)
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: Theme.alpha(Theme.accent, 0.75) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    Rectangle {
        x: 0; y: panel.radius; width: 1; height: Math.min(parent.height * 0.45, 260)
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.alpha(Theme.accent, 0.55) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }

    Item {
        id: header
        visible: panel.title !== ""
        anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: panel.padding; rightMargin: panel.padding; topMargin: panel.padding - 2 }
        height: visible ? 24 : 0
        Row {
            anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
            spacing: 12
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: panel.title.toUpperCase()
                color: Theme.textLabel
                font { family: Theme.font; pixelSize: Theme.fsLabel; letterSpacing: Theme.labelSpacing; weight: Font.DemiBold }
            }
            Pill { visible: panel.badge !== ""; text: panel.badge; tint: panel.badgeColor; anchors.verticalCenter: parent.verticalCenter }
        }
        Row { id: headerRight; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 8 }
    }
    Rectangle {
        visible: panel.title !== ""
        anchors { left: parent.left; right: parent.right; top: header.bottom; leftMargin: panel.padding; rightMargin: panel.padding; topMargin: 10 }
        height: 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: Theme.borderStrong }
            GradientStop { position: 0.6; color: Theme.border }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    Item {
        id: body
        clip: true
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: header.bottom
                  leftMargin: panel.padding; rightMargin: panel.padding; bottomMargin: panel.padding
                  topMargin: panel.title !== "" ? 20 : panel.padding }
    }
}
