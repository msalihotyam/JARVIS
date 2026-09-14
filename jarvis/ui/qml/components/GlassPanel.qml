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
    property bool closable: false
    // Opt-in: a title that does not fit on one line wraps deliberately onto
    // two (baselines ~20 / ~34 px) inside the same header allocation.
    property bool titleWrap: false
    // The colour of the panel's edge light and brackets (structural, not semantic).
    property color accentColor: Theme.accent
    signal closeRequested()

    radius: Theme.radius
    color: raised || level === 3 ? Theme.surfaceRaised : level === 1 ? Theme.surfaceWorkspace : Theme.surface
    border.width: 1
    border.color: Theme.border

    // Depth: two soft shadow plates behind the panel (drawn below it: z < 0).
    Rectangle { z: -2; visible: panel.shadow; anchors.fill: parent; anchors.margins: -2; anchors.topMargin: 6; radius: panel.radius + 2; color: "#000000"; opacity: 0.22 }
    Rectangle { z: -1; visible: panel.shadow; anchors.fill: parent; anchors.topMargin: 3; radius: panel.radius; color: "#000000"; opacity: 0.30 }
    // A faint outer cyan haze along the top edge (the reference's glowing rim).
    Rectangle { z: -1; visible: panel.raised; anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: panel.radius; rightMargin: panel.width * 0.4; topMargin: -2 } height: 4; radius: 2; color: Theme.alpha(panel.accentColor, 0.06) }
    Rectangle { anchors.fill: parent; anchors.margins: 5; radius: Math.max(2, panel.radius - 4); color: "transparent"; border.width: 1; border.color: Theme.alpha(panel.accentColor, panel.level === 1 ? 0.09 : 0.045) }

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
            GradientStop { position: 0.0; color: Theme.alpha(panel.accentColor, 0.035) }
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
            GradientStop { position: 0.00; color: Theme.alpha(panel.accentColor, 0.38) }
            GradientStop { position: 0.20; color: Theme.alpha(panel.accentColor, 0.08) }
            GradientStop { position: 0.75; color: Theme.alpha(panel.accentColor, 0.04) }
            GradientStop { position: 1.00; color: Theme.alpha(panel.accentColor, 0.26) }
        }
    }
    Rectangle {
        x: 0; y: panel.radius; width: 1; height: Math.min(parent.height * 0.45, 260)
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.alpha(panel.accentColor, 0.28) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    // Corner brackets: short L fragments just inside the top-right and bottom-left corners.
    Item {
        visible: panel.brackets
        anchors.fill: parent
        Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 8; anchors.topMargin: 8; width: 14; height: 1; color: panel.accentColor; opacity: 0.55 }
        Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 8; anchors.topMargin: 8; width: 1; height: 14; color: panel.accentColor; opacity: 0.55 }
        Rectangle { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.leftMargin: 8; anchors.bottomMargin: 8; width: 14; height: 1; color: panel.accentColor; opacity: 0.4 }
        Rectangle { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.leftMargin: 8; anchors.bottomMargin: 8; width: 1; height: 14; color: panel.accentColor; opacity: 0.4 }
        Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.rightMargin: 6; anchors.topMargin: 6; width: 3; height: 3; radius: 1.5; color: panel.accentColor; opacity: 0.9 }
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
                objectName: panel.title !== "" ? "panelTitle_" + panel.title : ""
                anchors.verticalCenter: parent.verticalCenter
                text: i18n.upper(panel.title)
                readonly property real room: Math.max(0, header.width - headerTail.width - (panel.closable ? 42 : 0) - (leftBadge.visible ? leftBadge.width + 14 : 0) - 8)
                // Section heading 13 / 18 Semibold, +0.06 em. A tight title
                // loses its tracking first; only then does it wrap (opt-in) or elide.
                readonly property bool tight: titleMetrics.advanceWidth > room
                readonly property bool wraps: panel.titleWrap && tightMetrics.advanceWidth > room
                width: !panel.titleWrap ? Math.min(implicitWidth, room) : wraps ? room : Math.min(Math.ceil((tight ? tightMetrics : titleMetrics).advanceWidth) + 2, room)
                wrapMode: wraps ? Text.WordWrap : Text.NoWrap
                maximumLineCount: wraps ? 2 : 1
                lineHeightMode: wraps ? Text.FixedHeight : Text.ProportionalHeight
                lineHeight: wraps ? 14 : 1
                elide: Text.ElideRight
                color: "#cfeaff"
                font: tight ? Theme.tracked(Theme.sectionHeading, 0) : Theme.sectionHeading
                TextMetrics { id: titleMetrics; text: i18n.upper(panel.title); font: Theme.sectionHeading }
                TextMetrics { id: tightMetrics; text: i18n.upper(panel.title); font.family: Theme.font; font.pixelSize: 13; font.weight: Font.DemiBold }
            }
            Pill { id: leftBadge; visible: panel.badge !== "" && !panel.badgeRight; text: panel.badge; tint: panel.badgeColor; anchors.verticalCenter: parent.verticalCenter }
        }
        Row { id: headerTail; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 8
            anchors.rightMargin: panel.closable ? 42 : 0
            Row { id: headerRight; spacing: 8; anchors.verticalCenter: parent.verticalCenter }
            Pill { visible: panel.badge !== "" && panel.badgeRight; text: panel.badge; tint: panel.badgeColor; anchors.verticalCenter: parent.verticalCenter }
        }
        IconButton {
            objectName: panel.closable ? "secondaryPageClose" : ""
            visible: panel.closable
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            width: 28; height: 28; glyph: "✕"; tip: qsTr("Back to Command Center")
            onClicked: panel.closeRequested()
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
