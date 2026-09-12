import QtQuick
import QtQuick.Shapes
import "../theme"

// The JARVIS core: identity and live state in one object. Layers, innermost
// to outermost: sphere, label, inner luminous ring, segmented ring, thin
// ring family, calibration tick ring, orbital ring with tracking nodes,
// outer partial arcs, the faint outermost ring, and the halo beneath them
// all. Geometry is static Shapes; motion is rotation and opacity only -
// no particles, no shader effects.
Item {
    id: core
    property string state_: "idle"
    property bool active: true
    property color stateColor: Theme.stateColor(state_)
    readonly property bool running: state_ === "running"
    readonly property bool dormant: state_ === "disabled"
    readonly property bool ambientMotion: Theme.ambient && active && visible && !dormant
    readonly property bool turning: active && visible && (ambientMotion || running)
    readonly property real s: width
    implicitWidth: 300; implicitHeight: 300

    Behavior on stateColor { ColorAnimation { duration: 260; easing.type: Easing.InOutCubic } }

    QtObject {
        id: glow
        property real energy: 1.0
        SequentialAnimation on energy {
            running: core.ambientMotion
            loops: Animation.Infinite
            NumberAnimation { to: core.running ? 1.6 : (core.state_ === "awaiting_approval" ? 1.5 : 1.22)
                              duration: core.running ? 900 : 2600; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: core.running ? 900 : 2600; easing.type: Easing.InOutSine }
        }
    }

    // -- halo -----------------------------------------------------------------
    Shape {
        id: halo
        anchors.centerIn: parent
        width: core.s * 1.5; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: (core.dormant ? 0.2 : 0.7) * glow.energy * Theme.glowStrength
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: halo.width / 2; centerY: halo.height / 2; centerRadius: halo.width / 2
                focalX: centerX; focalY: centerY
                GradientStop { position: 0.00; color: Theme.alpha(core.stateColor, 0.46) }
                GradientStop { position: 0.22; color: Theme.alpha(core.stateColor, 0.17) }
                GradientStop { position: 0.50; color: Theme.alpha(core.stateColor, 0.04) }
                GradientStop { position: 1.00; color: "transparent" }
            }
            PathAngleArc { centerX: halo.width / 2; centerY: halo.height / 2; radiusX: halo.width / 2; radiusY: halo.height / 2; startAngle: 0; sweepAngle: 360 }
        }
    }

    // -- faint outermost ring with sparse marks -----------------------------------
    Item {
        id: outermost
        anchors.centerIn: parent
        width: core.s * 0.99; height: width
        opacity: core.dormant ? 0.2 : 0.9
        // Static: a 90 s turn on 24 marks is invisible and costs a layer per frame.
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.22 }
        Repeater {
            model: 24
            Rectangle { width: 1; height: index % 6 === 0 ? 7 : 3; color: core.stateColor; opacity: index % 6 === 0 ? 0.6 : 0.3; x: outermost.width / 2 - 0.5; y: 0
                transform: Rotation { origin.x: 0.5; origin.y: outermost.height / 2; angle: index * 15 } }
        }
    }

    // -- outer partial arcs ---------------------------------------------------------
    Shape {
        id: outerArcs
        anchors.centerIn: parent
        width: core.s * 0.93; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.2 : 0.7
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 20000 : 64000 }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: outerArcs.width / 2; centerY: outerArcs.height / 2; radiusX: outerArcs.width / 2; radiusY: outerArcs.height / 2; startAngle: 10; sweepAngle: 46 } }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: outerArcs.width / 2; centerY: outerArcs.height / 2; radiusX: outerArcs.width / 2; radiusY: outerArcs.height / 2; startAngle: 190; sweepAngle: 46 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.55); strokeWidth: 2.5; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: outerArcs.width / 2; centerY: outerArcs.height / 2; radiusX: outerArcs.width / 2 - 5; radiusY: outerArcs.height / 2 - 5; startAngle: 100; sweepAngle: 30 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.55); strokeWidth: 2.5; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: outerArcs.width / 2; centerY: outerArcs.height / 2; radiusX: outerArcs.width / 2 - 5; radiusY: outerArcs.height / 2 - 5; startAngle: 280; sweepAngle: 30 } }
    }

    // -- thin orbital ring with tracking nodes ---------------------------------------
    Item {
        id: orbital
        anchors.centerIn: parent
        width: core.s * 0.84; height: width
        opacity: core.dormant ? 0.2 : 1
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 12000 : 44000 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.38 }
        Repeater {
            model: 3
            Item { x: orbital.width / 2 - 3; y: -3; width: 6; height: 6
                transform: Rotation { origin.x: 3; origin.y: orbital.height / 2 + 3; angle: index * 137 }
                Rectangle { anchors.centerIn: parent; width: index === 0 ? 6 : 4; height: width; radius: width / 2; color: Qt.lighter(core.stateColor, 1.2); opacity: 0.95 }
                Rectangle { anchors.centerIn: parent; width: 14; height: 14; radius: 7; color: core.stateColor; opacity: 0.25 }
            }
        }
    }

    // -- calibration tick ring -----------------------------------------------------------
    Item {
        id: ticks
        anchors.centerIn: parent
        width: core.s * 0.72; height: width
        opacity: core.dormant ? 0.3 : 1
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 14000 : 52000 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.30 }
        Repeater {
            model: 72
            Rectangle {
                width: 1; height: index % 18 === 0 ? 9 : (index % 6 === 0 ? 5 : 3)
                color: core.stateColor
                opacity: index % 18 === 0 ? 0.9 : (index % 6 === 0 ? 0.6 : 0.32)
                x: ticks.width / 2 - 0.5; y: 0
                transform: Rotation { origin.x: 0.5; origin.y: ticks.height / 2; angle: index * 5 }
            }
        }
    }
    // -- the concentric thin-ring family of the reference ----------------------------------
    Rectangle { anchors.centerIn: parent; width: core.s * 0.90; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: core.dormant ? 0.08 : 0.18 }
    Rectangle { anchors.centerIn: parent; width: core.s * 0.77; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: core.dormant ? 0.08 : 0.22 }
    Rectangle { anchors.centerIn: parent; width: core.s * 0.64; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: core.dormant ? 0.1 : 0.34 }
    Rectangle { anchors.centerIn: parent; width: core.s * 0.60; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: core.dormant ? 0.1 : 0.26 }

    // -- segmented ring: three segments + marker, and a heavier counter layer with a dotted ring
    Shape {
        id: segments
        anchors.centerIn: parent
        width: core.s * 0.555; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.25 : 0.95
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 9000 : 36000 }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 0; sweepAngle: 70 } }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 120; sweepAngle: 70 } }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 240; sweepAngle: 70 } }
        Rectangle { width: 5; height: 5; radius: 2.5; color: Qt.lighter(core.stateColor, 1.2)
            x: segments.width / 2 + segments.width / 2 * Math.cos(70 * Math.PI / 180) - 2.5; y: segments.height / 2 + segments.height / 2 * Math.sin(70 * Math.PI / 180) - 2.5
            Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: core.stateColor; opacity: 0.3 } }
    }
    Shape {
        id: counter
        anchors.centerIn: parent
        width: core.s * 0.50; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.2 : 1
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 360; to: 0; duration: core.running ? 7000 : 28000 }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.35); strokeWidth: 3; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: counter.width / 2; centerY: counter.height / 2; radiusX: counter.width / 2 - 2; radiusY: counter.height / 2 - 2; startAngle: 20; sweepAngle: 130 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.35); strokeWidth: 3; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: counter.width / 2; centerY: counter.height / 2; radiusX: counter.width / 2 - 2; radiusY: counter.height / 2 - 2; startAngle: 200; sweepAngle: 130 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.55); strokeWidth: 1; fillColor: "transparent"; strokeStyle: ShapePath.DashLine; dashPattern: [1, 4]
            PathAngleArc { centerX: counter.width / 2; centerY: counter.height / 2; radiusX: counter.width / 2 - 9; radiusY: counter.height / 2 - 9; startAngle: 0; sweepAngle: 360 } }
    }

    // -- inner luminous ring: the bright rim right outside the sphere -------------------
    Rectangle { anchors.centerIn: parent; width: core.s * 0.47; height: width; radius: width / 2; color: "transparent"; border.width: 5; border.color: core.stateColor; opacity: (core.dormant ? 0.05 : 0.12) * glow.energy }
    Rectangle { anchors.centerIn: parent; width: core.s * 0.43; height: width; radius: width / 2; color: "transparent"; border.width: 1.5; border.color: core.stateColor; opacity: (core.dormant ? 0.08 : 0.5) * Math.min(glow.energy, 1.3) }
    Item {
        id: inner
        anchors.centerIn: parent
        width: core.s * 0.445; height: width
        opacity: core.dormant ? 0.25 : 0.8
        RotationAnimator on rotation { running: core.running && core.active && core.visible; loops: Animation.Infinite; from: 0; to: 360; duration: 40000 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.7 }
        Repeater {
            model: 8
            Rectangle { width: 1; height: 4; color: core.stateColor; opacity: 0.9; x: inner.width / 2 - 0.5; y: 1
                transform: Rotation { origin.x: 0.5; origin.y: inner.height / 2 - 1; angle: index * 45 } }
        }
    }
    // Running: one bright fast sweep, visible only while reasoning.
    Shape {
        id: sweep
        anchors.centerIn: parent
        width: core.s * 0.47; height: width
        preferredRendererType: Shape.CurveRenderer
        visible: core.running; opacity: core.running ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 200 } }
        RotationAnimator on rotation { running: core.running && core.active && core.visible; loops: Animation.Infinite; from: 0; to: 360; duration: 12000 }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: sweep.width / 2; centerY: sweep.height / 2; radiusX: sweep.width / 2; radiusY: sweep.height / 2; startAngle: 0; sweepAngle: 48 } }
    }

    // -- sphere and label ------------------------------------------------------------------
    Shape {
        id: sphere
        anchors.centerIn: parent
        width: core.s * 0.41; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.55 : 1
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: sphere.width / 2; centerY: sphere.height / 2; centerRadius: sphere.width / 2
                focalX: sphere.width * 0.43; focalY: sphere.height * 0.58; focalRadius: 0
                GradientStop { position: 0.00; color: "#020c14" }
                GradientStop { position: 0.45; color: "#041624" }
                GradientStop { position: 0.82; color: Qt.darker(core.stateColor, 7) }
                GradientStop { position: 0.975; color: Qt.darker(core.stateColor, 2.8) }
                GradientStop { position: 1.00; color: Qt.lighter(core.stateColor, 1.25) }
            }
            PathAngleArc { centerX: sphere.width / 2; centerY: sphere.height / 2; radiusX: sphere.width / 2; radiusY: sphere.height / 2; startAngle: 0; sweepAngle: 360 }
        }
        SequentialAnimation on scale {
            running: core.ambientMotion
            loops: Animation.Infinite
            NumberAnimation { to: 1.025; duration: 2600; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: 2600; easing.type: Easing.InOutSine }
        }
    }
    // Stable curved reflections give the sphere a dark glass volume.
    Shape {
        id: facets
        anchors.centerIn: parent; width: core.s * 0.39; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.15 : 0.52
        ShapePath {
            strokeWidth: -1
            fillGradient: LinearGradient {
                x1: 0; y1: 0; x2: facets.width * 0.8; y2: facets.height * 0.6
                GradientStop { position: 0; color: "#ccbfedff" }
                GradientStop { position: 0.45; color: Theme.alpha(core.stateColor, 0.26) }
                GradientStop { position: 1; color: "transparent" }
            }
            startX: facets.width * 0.08; startY: facets.height * 0.38
            PathCubic { x: facets.width * 0.76; y: facets.height * 0.13; control1X: facets.width * 0.15; control1Y: -facets.height * 0.02; control2X: facets.width * 0.55; control2Y: -facets.height * 0.07 }
            PathCubic { x: facets.width * 0.08; y: facets.height * 0.38; control1X: facets.width * 0.46; control1Y: facets.height * 0.10; control2X: facets.width * 0.26; control2Y: facets.height * 0.13 }
        }
        ShapePath { strokeWidth: 0.8; strokeColor: Theme.alpha(core.stateColor, 0.28); fillColor: "transparent"
            startX: facets.width * 0.07; startY: facets.height * 0.65
            PathCubic { x: facets.width * 0.9; y: facets.height * 0.23; control1X: facets.width * 0.4; control1Y: facets.height * 0.24; control2X: facets.width * 0.65; control2Y: facets.height * 0.5 }
        }
        ShapePath { strokeWidth: -1; fillColor: Theme.alpha(core.stateColor, 0.17)
            startX: facets.width * 0.2; startY: facets.height * 0.83
            PathCubic { x: facets.width * 0.91; y: facets.height * 0.62; control1X: facets.width * 0.35; control1Y: facets.height * 1.04; control2X: facets.width * 0.81; control2Y: facets.height * 0.97 }
            PathCubic { x: facets.width * 0.2; y: facets.height * 0.83; control1X: facets.width * 0.52; control1Y: facets.height * 0.96; control2X: facets.width * 0.52; control2Y: facets.height * 0.75 }
        }
    }
    Rectangle { anchors.centerIn: parent; width: core.s * 0.41; height: width; radius: width / 2; color: "transparent"; border.width: 1.5; border.color: Qt.lighter(core.stateColor, 1.15); opacity: core.dormant ? 0.3 : 0.9 }
    Text {
        anchors.centerIn: parent; anchors.verticalCenterOffset: 1; anchors.horizontalCenterOffset: 1
        text: "JARVIS"; color: Theme.bg0; opacity: 0.6
        font { family: Theme.font; pixelSize: core.s * 0.058; letterSpacing: 5; weight: Font.DemiBold }
    }
    Text {
        anchors.centerIn: parent
        text: "JARVIS"; color: core.dormant ? Theme.textMuted : "#f3fbff"
        font { family: Theme.font; pixelSize: core.s * 0.058; letterSpacing: 5; weight: Font.DemiBold }
    }

    // UNKNOWN_OUTCOME speaks in its own language: an unstable two-tone pulse
    // that resembles neither success nor an ordinary error.
    Rectangle {
        anchors.centerIn: parent
        width: core.s * 0.47; height: width; radius: width / 2
        color: "transparent"
        border.width: 3; border.color: Theme.unknown
        visible: core.state_ === "unknown"
        opacity: 0
        SequentialAnimation on opacity {
            running: core.state_ === "unknown" && core.active && core.visible
            loops: Animation.Infinite
            NumberAnimation { to: 0.9; duration: 380; easing.type: Easing.InOutCubic }
            NumberAnimation { to: 0.15; duration: 900; easing.type: Easing.InOutCubic }
            PauseAnimation { duration: 500 }
        }
    }

    // Completion flare: one restrained pulse, then back to rest.
    Rectangle {
        id: flare
        anchors.centerIn: parent
        width: core.s * 0.45; height: width; radius: width / 2
        color: "transparent"; border.width: 2; border.color: Theme.success
        opacity: 0; scale: 1
    }
    SequentialAnimation {
        id: flareAnim
        ParallelAnimation {
            NumberAnimation { target: flare; property: "opacity"; from: 0.9; to: 0; duration: 700; easing.type: Easing.OutCubic }
            NumberAnimation { target: flare; property: "scale"; from: 0.9; to: 1.7; duration: 700; easing.type: Easing.OutCubic }
        }
    }
    onState_Changed: if (state_ === "completed") flareAnim.restart()
}
