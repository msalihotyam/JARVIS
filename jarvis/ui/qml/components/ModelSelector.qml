import QtQuick
import QtQuick.Shapes
import "../theme"

// The approved "Option 5 - Command" model selector: a chamfered, layered
// dark-glass instrument with a fine cyan perimeter, an inset provider frame,
// the selected model as primary text, real metadata beneath it, and a drawn
// chevron. It belongs to the same machine as the JARVIS core - it is not a
// ComboBox and carries no Controls styling.
//
// Content is real state only: the configured model (or the CLI default) and
// the metadata the installation actually knows. Nothing is invented.
Item {
    id: sel
    property bool open: false
    property string modelName: ""
    property string metadata: ""
    signal activated()

    implicitWidth: 280
    implicitHeight: Theme.stripSelectorHeight
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: "Model and provider"
    Accessible.description: sel.modelName + (sel.metadata ? ", " + sel.metadata : "")
    Keys.onReturnPressed: sel.activated()
    Keys.onSpacePressed: sel.activated()

    readonly property bool hovered: hit.containsMouse
    readonly property bool active: hovered || open || activeFocus
    readonly property int chamfer: Theme.stripChamfer
    // The perimeter brightens on hover / open; it never changes weight, so
    // the silhouette stays crisp.
    property real edge: sel.open ? 1 : (sel.hovered ? 0.82 : 0.6)
    Behavior on edge { NumberAnimation { duration: Theme.hoverMs } }

    // -- layered glass body, chamfered ------------------------------------
    Shape {
        id: frame
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        readonly property real w: sel.width
        readonly property real h: sel.height
        readonly property real c: sel.chamfer
        // Body: the inner octagon, dark glass with a top-down gradient.
        ShapePath {
            strokeWidth: -1
            fillGradient: LinearGradient {
                x1: 0; y1: 4; x2: 0; y2: frame.h - 4
                GradientStop { position: 0; color: Theme.commandGlass0 }
                GradientStop { position: 1; color: Theme.commandGlass1 }
            }
            startX: 4 + frame.c; startY: 4
            PathLine { x: frame.w - 4 - frame.c; y: 4 }
            PathLine { x: frame.w - 4; y: 4 + frame.c }
            PathLine { x: frame.w - 4; y: frame.h - 4 - frame.c }
            PathLine { x: frame.w - 4 - frame.c; y: frame.h - 4 }
            PathLine { x: 4 + frame.c; y: frame.h - 4 }
            PathLine { x: 4; y: frame.h - 4 - frame.c }
            PathLine { x: 4; y: 4 + frame.c }
            PathLine { x: 4 + frame.c; y: 4 }
        }
        // Outer perimeter: a single fine hairline, one step dimmer.
        ShapePath {
            strokeColor: Theme.alpha(Theme.commandEdgeDim, 0.55 + 0.45 * sel.edge)
            strokeWidth: 1; fillColor: "transparent"; joinStyle: ShapePath.MiterJoin
            startX: frame.c; startY: 0.5
            PathLine { x: frame.w - frame.c; y: 0.5 }
            PathLine { x: frame.w - 0.5; y: frame.c }
            PathLine { x: frame.w - 0.5; y: frame.h - frame.c }
            PathLine { x: frame.w - frame.c; y: frame.h - 0.5 }
            PathLine { x: frame.c; y: frame.h - 0.5 }
            PathLine { x: 0.5; y: frame.h - frame.c }
            PathLine { x: 0.5; y: frame.c }
            PathLine { x: frame.c; y: 0.5 }
        }
        // Inner perimeter: the illuminated frame proper.
        ShapePath {
            strokeColor: Theme.alpha(Theme.commandEdge, 0.30 + 0.55 * sel.edge)
            strokeWidth: 1.4; fillColor: "transparent"; joinStyle: ShapePath.MiterJoin
            startX: 4 + frame.c; startY: 4
            PathLine { x: frame.w - 4 - frame.c; y: 4 }
            PathLine { x: frame.w - 4; y: 4 + frame.c }
            PathLine { x: frame.w - 4; y: frame.h - 4 - frame.c }
            PathLine { x: frame.w - 4 - frame.c; y: frame.h - 4 }
            PathLine { x: 4 + frame.c; y: frame.h - 4 }
            PathLine { x: 4; y: frame.h - 4 - frame.c }
            PathLine { x: 4; y: 4 + frame.c }
            PathLine { x: 4 + frame.c; y: 4 }
        }
        // Thin illuminated edge fragments: a bright run top-right, one
        // bottom-left, as in the reference.
        ShapePath {
            strokeColor: Theme.alpha(Theme.commandEdge, 0.95); strokeWidth: 1.6
            fillColor: "transparent"; capStyle: ShapePath.FlatCap
            startX: frame.w * 0.52; startY: 4.2
            PathLine { x: frame.w - 8 - frame.c; y: 4.2 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.commandEdge, 0.95); strokeWidth: 1.6
            fillColor: "transparent"; capStyle: ShapePath.FlatCap
            startX: 8 + frame.c; startY: frame.h - 4.2
            PathLine { x: frame.w * 0.48; y: frame.h - 4.2 }
        }
        // Small technical accents just inside the chamfered ends.
        ShapePath {
            strokeColor: Theme.alpha(Theme.commandEdge, 0.7); strokeWidth: 1; fillColor: "transparent"
            startX: 4.5; startY: 4 + frame.c + 4
            PathLine { x: 4.5; y: frame.h - 4 - frame.c - 4 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.commandEdge, 0.7); strokeWidth: 1; fillColor: "transparent"
            startX: frame.w - 4.5; startY: 4 + frame.c + 4
            PathLine { x: frame.w - 4.5; y: frame.h - 4 - frame.c - 4 }
        }
    }
    // Restrained inner highlight: one soft sheen across the upper body.
    Rectangle {
        x: 6; y: 5; width: sel.width - 12; height: Math.round((sel.height - 10) * 0.45)
        gradient: Gradient {
            GradientStop { position: 0; color: Theme.alpha(Theme.commandEdge, 0.07) }
            GradientStop { position: 1; color: "transparent" }
        }
    }
    Rectangle {
        anchors.fill: parent; anchors.margins: -3; radius: 4
        color: "transparent"; border.color: Theme.accent; visible: sel.activeFocus
    }

    // -- provider identity in its own inset instrument frame ---------------
    Rectangle {
        id: badge
        x: 12; anchors.verticalCenter: parent.verticalCenter
        width: 34; height: 34; radius: 9
        color: Theme.surfaceDeep
        border.width: 1; border.color: Theme.alpha(Theme.commandEdge, 0.34)
        Rectangle {
            anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1
            color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.text, 0.05)
        }
        Text {
            anchors.centerIn: parent; anchors.horizontalCenterOffset: -1
            text: "A\\"; color: Theme.text
            font { family: Theme.font; pixelSize: 17; weight: Font.DemiBold; letterSpacing: -1 }
        }
    }

    // -- model identity ----------------------------------------------------
    Column {
        id: identity
        anchors.verticalCenter: parent.verticalCenter
        x: badge.x + badge.width + 12
        width: Math.max(40, chevron.x - x - 10)
        spacing: 1
        Text {
            width: parent.width; elide: Text.ElideRight
            text: sel.modelName
            color: Theme.text
            font { family: Theme.font; pixelSize: Theme.fsLead; weight: Font.Medium }
        }
        Text {
            visible: sel.metadata !== ""
            width: parent.width; elide: Text.ElideRight
            text: sel.metadata
            color: Theme.textDim
            font { family: Theme.font; pixelSize: Theme.fsLabel; letterSpacing: 1.5 }
        }
    }

    // -- chevron: drawn, precise, rotates when open ------------------------
    Shape {
        id: chevron
        x: sel.width - 20 - width; anchors.verticalCenter: parent.verticalCenter
        width: 14; height: 9
        preferredRendererType: Shape.CurveRenderer
        rotation: sel.open ? 180 : 0
        Behavior on rotation { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
        ShapePath {
            strokeColor: sel.active ? Theme.text : Theme.textLabel
            strokeWidth: 1.8; fillColor: "transparent"; capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
            startX: 0.9; startY: 1
            PathLine { x: 7; y: 8 }
            PathLine { x: 13.1; y: 1 }
        }
    }

    MouseArea {
        id: hit
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: { sel.forceActiveFocus(); sel.activated() }
    }
}
