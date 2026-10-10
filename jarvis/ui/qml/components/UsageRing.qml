import QtQuick
import QtQuick.Shapes
import "../theme"

// Gate 6G U2 (G6U1A): the usage button - a round ring whose arc and centre
// text show the 5-hour window's USED share as the provider reported it. One
// colour whatever the value (it is a figure, not a warning light). Nothing
// reported: an empty ring and "–", never a 0. A fine dashed halo turns slowly
// (a documented K2 rhythm, tests/test_motion_6g.py); with ambient effects off
// it stands still. A click opens and closes the usage popover (U3).
//
// Bridge-independent: `provider` and `rows` (UsagePopover's shape) are plain
// inputs; `usedPct` (0-100 or null) defaults to the rows' 5-hour window.
Item {
    id: ring
    objectName: "usageRing"
    property string provider: ""
    property var rows: []
    property var usedPct: pop.used("five_hour")

    readonly property bool reported: typeof usedPct === "number" && isFinite(usedPct)
    readonly property real sweep: reported ? 3.6 * Math.max(0, Math.min(100, usedPct)) : 0
    readonly property string centreText: reported ? qsTr("%1%").arg(Math.round(usedPct)) : "–"
    readonly property color tone: Theme.accent
    readonly property bool haloTurning: turn.running

    implicitWidth: 38; implicitHeight: 34
    width: implicitWidth; height: implicitHeight
    Accessible.role: Accessible.Button
    Accessible.name: tip.text
    Accessible.onPressAction: toggle()
    function toggle() { pop.visible ? pop.close() : pop.open() }

    Rectangle {
        anchors.fill: parent; radius: 9
        color: hit.containsMouse || pop.visible ? Theme.alpha(Theme.text, 0.08) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    }
    Shape {
        id: halo
        objectName: "usageHalo"
        width: 30; height: 30; anchors.centerIn: parent
        opacity: hit.containsMouse ? 0.8 : 0.45
        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: ring.tone; strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [3, 5]
            PathAngleArc { centerX: 15; centerY: 15; radiusX: 13.5; radiusY: 13.5; startAngle: 0; sweepAngle: 360 }
        }
        RotationAnimator on rotation {
            id: turn
            from: 0; to: 360; duration: Theme.dur(9000); loops: Animation.Infinite
            running: Theme.ambient && ring.visible
        }
    }
    Shape {
        objectName: "usageDial"
        width: 30; height: 30; anchors.centerIn: parent
        preferredRendererType: Shape.CurveRenderer
        ShapePath {                                   // the track
            strokeColor: Theme.alpha(ring.tone, 0.2); strokeWidth: 2.6; fillColor: "transparent"
            PathAngleArc { centerX: 15; centerY: 15; radiusX: 10; radiusY: 10; startAngle: 0; sweepAngle: 360 }
        }
        ShapePath {                                   // the used share, clockwise from the top
            objectName: "usageArc"
            strokeColor: ring.sweep > 0 ? ring.tone : "transparent"; strokeWidth: 2.6; fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 15; centerY: 15; radiusX: 10; radiusY: 10; startAngle: -90; sweepAngle: ring.sweep }
        }
    }
    Text {
        objectName: "usageRingText"
        anchors.centerIn: parent; width: 17
        horizontalAlignment: Text.AlignHCenter
        fontSizeMode: Text.HorizontalFit; minimumPixelSize: 6
        text: ring.centreText
        color: Theme.text; renderType: Theme.textRender; font: Theme.face(9, true, 0, true)
    }
    MouseArea {
        id: hit
        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
        onClicked: ring.toggle()
    }
    HoverTip {
        id: tip
        active: !pop.visible
        text: ring.reported ? qsTr("5-hour usage %1% · details").arg(Math.round(ring.usedPct)) : qsTr("Usage not reported · details")
    }
    UsagePopover {
        id: pop
        trigger: ring
        provider: ring.provider; rows: ring.rows
        restY: -height - 8; x: ring.width - width
    }
}
