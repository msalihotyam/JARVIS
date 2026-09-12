import QtQuick
import "../theme"

// One layered glass surface. No live blur: depth comes from a soft drop
// shadow beneath, a translucent fill, a top sheen, a luminous top edge, a
// darker bottom-right and a thin border - all plain rectangles, all
// static. `level`: 1 workspace glass, 2 panel glass, 3 raised / floating.
// `brackets` adds the small corner detail of the reference panels.
Rectangle {
    id: panel
    property string title: ""
    property string badge: ""
    property color badgeColor: Theme.success
    property bool badgeRight: false
    property alias headerRight: headerRight.data
    default property alias content: body.data
    property int padding: Theme.pad
    property int level: 2
    property bool raised: false
    property bool shadow: true
    property bool brackets: false

    radius: Theme.radius
    color: raised || level === 3 ? Theme.surfaceRaised : level === 1 ? Theme.surfaceWorkspace : Theme.surface
    border.width: 1
    border.color: Theme.border

    // Depth: two soft shadow plates behind the panel (drawn below it: z < 0).
    Rectangle { z: -2; visible: panel.shadow; anchors.fill: parent; anchors.margins: -2; anchors.topMargin: 6; radius: panel.radius + 2; color: "#000000"; opacity: 0.22 }
    Rectangle { z: -1; visible: panel.shadow; anchors.fill: parent; anchors.topMargin: 3; radius: panel.radius; color: "#000000"; opacity: 0.30 }
    // A faint outer cyan haze along the top edge (the reference's glowing rim).
    Rectangle { z: -1; visible: panel.raised; anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: panel.radius; rightMargin: panel.width * 0.4; topMargin: -2 } height: 4; radius: 2; color: Theme.alpha(Theme.accent, 0.06) }
    Rectangle { anchors.fill: parent; anchors.margins: 5; radius: Math.max(2, panel.radius - 4); color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, panel.level === 1 ? 0.09 : 0.045) }

    // Material: a top sheen, a faint left light, a darker foot.
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
    Rectangle {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: 1 }
        height: Math.min(parent.height * 0.35, 160); radius: panel.radius - 1
        gradient: Gradient {
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.18) }
        }
    }
    // Edge highlights: the luminous top edge, brightest at the left, and the left edge.
    Rectangle {
        x: panel.radius; y: 0; height: 1; width: parent.width - panel.radius * 2
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.00; color: Theme.alpha(Theme.accent, 0.38) }
            GradientStop { position: 0.20; color: Theme.alpha(Theme.accent, 0.08) }
            GradientStop { position: 0.75; color: Theme.alpha(Theme.accent, 0.04) }
            GradientStop { position: 1.00; color: Theme.alpha(Theme.accent, 0.26) }
        }
    }
    Rectangle {
        x: 0; y: panel.radius; width: 1; height: Math.min(parent.height * 0.45, 260)
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.alpha(Theme.accent, 0.28) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    // Corner brackets: short L fragments just inside the top-right and bottom-left corners.
    Item {
        visible: panel.brackets
        anchors.fill: parent
        Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 8; anchors.topMargin: 8; width: 14; height: 1; color: Theme.accent; opacity: 0.55 }
        Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 8; anchors.topMargin: 8; width: 1; height: 14; color: Theme.accent; opacity: 0.55 }
        Rectangle { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.leftMargin: 8; anchors.bottomMargin: 8; width: 14; height: 1; color: Theme.accent; opacity: 0.4 }
        Rectangle { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.leftMargin: 8; anchors.bottomMargin: 8; width: 1; height: 14; color: Theme.accent; opacity: 0.4 }
        Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 6; anchors.topMargin: 6; width: 3; height: 3; radius: 1.5; color: Theme.accent; opacity: 0.9 }
    }

    Item {
        id: header
        visible: panel.title !== ""
        anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: panel.padding; rightMargin: panel.padding; topMargin: panel.padding - 2 }
        height: visible ? 24 : 0
        Row {
            anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
            spacing: 14
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: panel.title.toUpperCase()
                width: Math.min(implicitWidth, Math.max(0, header.width - headerTail.width - (leftBadge.visible ? leftBadge.width + 14 : 0) - 8))
                elide: Text.ElideRight
                color: "#cfeaff"
                font { family: Theme.font; pixelSize: 12; letterSpacing: 1.6; weight: Font.DemiBold }
            }
            Pill { id: leftBadge; visible: panel.badge !== "" && !panel.badgeRight; text: panel.badge; tint: panel.badgeColor; anchors.verticalCenter: parent.verticalCenter }
        }
        Row { id: headerTail; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 8
            Row { id: headerRight; spacing: 8; anchors.verticalCenter: parent.verticalCenter }
            Pill { visible: panel.badge !== "" && panel.badgeRight; text: panel.badge; tint: panel.badgeColor; anchors.verticalCenter: parent.verticalCenter }
        }
    }
    Rectangle {
        visible: panel.title !== ""
        anchors { left: parent.left; right: parent.right; top: header.bottom; leftMargin: panel.padding; rightMargin: panel.padding; topMargin: 9 }
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
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: panel.title !== "" ? header.bottom : parent.top
                  leftMargin: panel.padding; rightMargin: panel.padding; bottomMargin: panel.padding
                  topMargin: panel.title !== "" ? 18 : panel.padding }
    }
}
