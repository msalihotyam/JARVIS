import QtQuick
import QtQuick.Shapes
import "../theme"

// The model / provider selector: a clean technical instrument - a deep navy
// body with one fine cool-blue perimeter and shallow corner cuts, one
// restrained upper-edge highlight, a 28 px provider socket, the selected
// model as primary text with the real metadata quieter beneath it, and a
// 12 px drawn chevron. Hover brightens the perimeter a step; keyboard focus
// adds a distinct fine outline; open keeps the silhouette and reverses the
// chevron. It is not a ComboBox and carries no Controls styling.
//
// Content is real state only: the configured model (or the CLI default)
// and the metadata the installation actually knows. Nothing is invented.
Item {
    id: sel
    property bool open: false
    property string modelName: ""
    property string metadata: ""
    signal activated()

    implicitWidth: 304
    implicitHeight: Theme.stripSelectorHeight
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: qsTr("Model and provider")
    Accessible.description: sel.modelName + (sel.metadata ? ", " + sel.metadata : "")
    Keys.onReturnPressed: sel.activated()
    Keys.onSpacePressed: sel.activated()

    readonly property bool hovered: hit.containsMouse
    readonly property bool active: hovered || open || activeFocus
    readonly property int cut: 6                         // shallow corner cuts
    readonly property color blue: "#278dce"              // structural cool blue
    readonly property color glint: "#49bdfa"             // local highlight
    // One progression drives the perimeter: restrained at rest, a step
    // brighter under the pointer or while open. Weight never changes.
    property real edge: (sel.open || sel.hovered) ? 1 : 0.5
    Behavior on edge { NumberAnimation { duration: Theme.hoverMs } }

    // -- body: deep navy, one fine perimeter with corner cuts -----------------
    Shape {
        id: frame
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        readonly property real w: sel.width
        readonly property real h: sel.height
        readonly property real c: sel.cut
        ShapePath {
            strokeColor: Theme.alpha(sel.blue, 0.42 + 0.5 * sel.edge); strokeWidth: 1
            joinStyle: ShapePath.MiterJoin
            fillGradient: LinearGradient {
                x1: 0; y1: 0; x2: 0; y2: frame.h
                GradientStop { position: 0; color: Theme.glassInteractiveTop }
                GradientStop { position: 1; color: Theme.glassInteractive }
            }
            startX: frame.c + 0.5; startY: 0.5
            PathLine { x: frame.w - frame.c - 0.5; y: 0.5 }
            PathLine { x: frame.w - 0.5; y: frame.c + 0.5 }
            PathLine { x: frame.w - 0.5; y: frame.h - frame.c - 0.5 }
            PathLine { x: frame.w - frame.c - 0.5; y: frame.h - 0.5 }
            PathLine { x: frame.c + 0.5; y: frame.h - 0.5 }
            PathLine { x: 0.5; y: frame.h - frame.c - 0.5 }
            PathLine { x: 0.5; y: frame.c + 0.5 }
            PathLine { x: frame.c + 0.5; y: 0.5 }
        }
        // One restrained upper-edge highlight, fading out before the middle.
        ShapePath {
            strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            strokeColor: Theme.alpha(sel.glint, 0.22 + 0.3 * sel.edge)
            startX: frame.c + 6; startY: 1.5
            PathLine { x: frame.w * 0.58; y: 1.5 }
        }
    }
    // Keyboard focus: a distinct fine outline outside the perimeter.
    Rectangle {
        anchors.fill: parent; anchors.margins: -3; radius: 4
        color: "transparent"; border.width: 1; border.color: Theme.alpha(sel.glint, 0.85)
        visible: sel.activeFocus
    }

    // -- provider socket ---------------------------------------------------------
    Rectangle {
        id: badge
        objectName: "selectorBadge"
        x: 12; anchors.verticalCenter: parent.verticalCenter
        width: 28; height: 28; radius: 7
        color: "#0a2238"
        border.width: 1; border.color: Theme.alpha(sel.blue, 0.55)
        Rectangle { x: 5; y: 1; width: parent.width - 10; height: 1; color: Theme.alpha(sel.glint, 0.22) }
        Text {
            objectName: "selectorLogo"
            anchors.centerIn: parent
            text: "AI"; color: "#dbeeff"
            font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold }
        }
    }

    // -- model identity ----------------------------------------------------------
    Column {
        id: identity
        anchors.verticalCenter: parent.verticalCenter
        x: badge.x + badge.width + 10
        width: Math.max(40, chevron.x - x - 8)
        spacing: 1
        Text {
            width: parent.width; elide: Text.ElideRight
            objectName: "selectorModelText"
            text: sel.modelName
            color: Theme.text
            font: Theme.modelName
            lineHeightMode: Text.FixedHeight; lineHeight: 24
        }
        Text {
            visible: sel.metadata !== ""
            width: parent.width; elide: Text.ElideRight
            id: metaText
            objectName: "selectorMetadataText"
            text: sel.metadata.replace(/\s*\|\s*/g, " | ")
            color: Theme.textMuted
            // 11 / 16 Medium, +0.04 em; where the real route text does not fit,
            // the tracking goes first, then the size steps down - never below 9.
            readonly property real fit: Math.min(1, width / Math.max(1, metaFull.advanceWidth))
            font.family: Theme.font; font.weight: Font.Medium
            font.letterSpacing: fit < 1 ? 0 : 0.44
            font.pixelSize: fit < 1 ? Math.max(9, Math.floor(11 * width / Math.max(1, metaTight.advanceWidth))) : 11
            lineHeightMode: Text.FixedHeight; lineHeight: 16
            TextMetrics { id: metaFull; text: metaText.text; font.family: Theme.font; font.pixelSize: 11; font.weight: Font.Medium; font.letterSpacing: 0.44 }
            TextMetrics { id: metaTight; text: metaText.text; font.family: Theme.font; font.pixelSize: 11; font.weight: Font.Medium }
        }
    }

    // -- chevron: 12 px, drawn, reversed when open --------------------------------
    Shape {
        id: chevron
        x: sel.width - 12 - width; anchors.verticalCenter: parent.verticalCenter
        width: 12; height: 7
        preferredRendererType: Shape.CurveRenderer
        rotation: sel.open ? 180 : 0
        Behavior on rotation { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
        ShapePath {
            strokeColor: sel.active ? "#dbeeff" : Theme.alpha(sel.glint, 0.85)
            strokeWidth: 1.6; fillColor: "transparent"; capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
            startX: 0.8; startY: 0.8
            PathLine { x: 6; y: 6.2 }
            PathLine { x: 11.2; y: 0.8 }
        }
    }

    MouseArea {
        id: hit
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: sel.activated()
    }
}
