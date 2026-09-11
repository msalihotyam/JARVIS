import QtQuick
import QtQuick.Shapes
import "../theme"

// The JARVIS core: identity and live state in one object. Concentric
// calibrated rings, segmented arcs on slow counter-rotating layers, a
// shaded sphere and a controlled halo. Geometry is static Shapes; motion is
// rotation and opacity only - no particles, no shader effects.
Item {
    id: core
    property string state_: "idle"
    property bool active: true
    property color stateColor: Theme.stateColor(state_)
    readonly property bool running: state_ === "running"
    readonly property bool dormant: state_ === "disabled"
    readonly property bool ambientMotion: Theme.ambient && active && !dormant
    readonly property real s: width
    implicitWidth: 170; implicitHeight: 170

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

    // -- halo: one smooth radial falloff, well beyond the rings ------------
    Shape {
        id: halo
        anchors.centerIn: parent
        width: core.s * 2.3; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: (core.dormant ? 0.2 : 0.62) * glow.energy * Theme.glowStrength
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: halo.width / 2; centerY: halo.height / 2; centerRadius: halo.width / 2
                focalX: centerX; focalY: centerY
                GradientStop { position: 0.00; color: Theme.alpha(core.stateColor, 0.34) }
                GradientStop { position: 0.28; color: Theme.alpha(core.stateColor, 0.11) }
                GradientStop { position: 0.55; color: Theme.alpha(core.stateColor, 0.03) }
                GradientStop { position: 1.00; color: "transparent" }
            }
            PathAngleArc { centerX: halo.width / 2; centerY: halo.height / 2; radiusX: halo.width / 2; radiusY: halo.height / 2; startAngle: 0; sweepAngle: 360 }
        }
    }

    // -- outer calibrated ring: 72 ticks, slow clockwise --------------------
    Item {
        id: outer
        anchors.centerIn: parent
        width: core.s * 0.985; height: width
        opacity: core.dormant ? 0.3 : 1
        RotationAnimator on rotation { running: core.ambientMotion || core.running; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 14000 : 52000 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.16 }
        Repeater {
            model: 72
            Rectangle {
                width: 1; height: index % 18 === 0 ? 9 : (index % 6 === 0 ? 5 : 3)
                color: core.stateColor
                opacity: index % 18 === 0 ? 0.85 : (index % 6 === 0 ? 0.55 : 0.28)
                x: outer.width / 2 - 0.5; y: 0
                transform: Rotation { origin.x: 0.5; origin.y: outer.height / 2; angle: index * 5 }
            }
        }
    }

    // -- arc layer A: three 70-degree segments + an orbiting marker ---------
    Shape {
        id: arcsA
        anchors.centerIn: parent
        width: core.s * 0.90; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.25 : 0.85
        RotationAnimator on rotation { running: core.ambientMotion || core.running; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 9000 : 36000 }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: arcsA.width / 2; centerY: arcsA.height / 2; radiusX: arcsA.width / 2; radiusY: arcsA.height / 2; startAngle: 0; sweepAngle: 70 } }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: arcsA.width / 2; centerY: arcsA.height / 2; radiusX: arcsA.width / 2; radiusY: arcsA.height / 2; startAngle: 120; sweepAngle: 70 } }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: arcsA.width / 2; centerY: arcsA.height / 2; radiusX: arcsA.width / 2; radiusY: arcsA.height / 2; startAngle: 240; sweepAngle: 70 } }
        Rectangle { width: 5; height: 5; radius: 2.5; color: core.stateColor; x: arcsA.width / 2 + arcsA.width / 2 * Math.cos(70 * Math.PI / 180) - 2.5; y: arcsA.height / 2 + arcsA.height / 2 * Math.sin(70 * Math.PI / 180) - 2.5
            Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: core.stateColor; opacity: 0.25 } }
    }

    // -- arc layer B: two heavy 130-degree segments + a dotted ring, counter-rotating
    Shape {
        id: arcsB
        anchors.centerIn: parent
        width: core.s * 0.80; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.2 : 1
        RotationAnimator on rotation { running: core.ambientMotion || core.running; loops: Animation.Infinite; from: 360; to: 0; duration: core.running ? 7000 : 28000 }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.28); strokeWidth: 3; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: arcsB.width / 2; centerY: arcsB.height / 2; radiusX: arcsB.width / 2 - 2; radiusY: arcsB.height / 2 - 2; startAngle: 20; sweepAngle: 130 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.28); strokeWidth: 3; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: arcsB.width / 2; centerY: arcsB.height / 2; radiusX: arcsB.width / 2 - 2; radiusY: arcsB.height / 2 - 2; startAngle: 200; sweepAngle: 130 } }
        ShapePath {
            strokeColor: Theme.alpha(core.stateColor, 0.5); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 4]
            PathAngleArc { centerX: arcsB.width / 2; centerY: arcsB.height / 2; radiusX: arcsB.width / 2 - 9; radiusY: arcsB.height / 2 - 9; startAngle: 0; sweepAngle: 360 }
        }
    }

    // -- inner calibrated ring ---------------------------------------------
    Item {
        id: inner
        anchors.centerIn: parent
        width: core.s * 0.66; height: width
        opacity: core.dormant ? 0.25 : 0.7
        RotationAnimator on rotation { running: core.running; loops: Animation.Infinite; from: 0; to: 360; duration: 20000 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.55 }
        Repeater {
            model: 8
            Rectangle { width: 1; height: 4; color: core.stateColor; opacity: 0.8; x: inner.width / 2 - 0.5; y: 1
                transform: Rotation { origin.x: 0.5; origin.y: inner.height / 2 - 1; angle: index * 45 } }
        }
    }

    // -- running: one bright fast sweep, visible only while reasoning --------
    Shape {
        id: sweep
        anchors.centerIn: parent
        width: core.s * 0.73; height: width
        preferredRendererType: Shape.CurveRenderer
        visible: core.running; opacity: core.running ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 200 } }
        RotationAnimator on rotation { running: core.running; loops: Animation.Infinite; from: 0; to: 360; duration: 2200 }
        ShapePath {
            strokeColor: core.stateColor; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: sweep.width / 2; centerY: sweep.height / 2; radiusX: sweep.width / 2; radiusY: sweep.height / 2; startAngle: 0; sweepAngle: 48 }
        }
    }

    // -- rim light and the sphere ------------------------------------------
    Rectangle { anchors.centerIn: parent; width: core.s * 0.58; height: width; radius: width / 2; color: "transparent"; border.width: 7; border.color: core.stateColor; opacity: (core.dormant ? 0.05 : 0.14) * glow.energy }
    Rectangle { anchors.centerIn: parent; width: core.s * 0.545; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: core.dormant ? 0.15 : 0.35 }
    Shape {
        id: sphere
        anchors.centerIn: parent
        width: core.s * 0.5; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dormant ? 0.55 : 1
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: sphere.width / 2; centerY: sphere.height / 2; centerRadius: sphere.width / 2
                focalX: sphere.width * 0.36; focalY: sphere.height * 0.32; focalRadius: 0
                GradientStop { position: 0.00; color: Qt.lighter(core.stateColor, 1.45) }
                GradientStop { position: 0.16; color: core.stateColor }
                GradientStop { position: 0.48; color: Qt.darker(core.stateColor, 2.6) }
                GradientStop { position: 0.80; color: Theme.bg1 }
                GradientStop { position: 1.00; color: Theme.bg0 }
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
    Rectangle { anchors.centerIn: parent; width: core.s * 0.5; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: core.dormant ? 0.3 : 0.85 }
    Text {
        anchors.centerIn: parent; anchors.verticalCenterOffset: 1; anchors.horizontalCenterOffset: 1
        text: "JARVIS"; color: Theme.bg0; opacity: 0.6
        font { family: Theme.font; pixelSize: core.s * 0.078; letterSpacing: 4; weight: Font.DemiBold }
    }
    Text {
        anchors.centerIn: parent
        text: "JARVIS"; color: core.dormant ? Theme.textMuted : "#f3fbff"
        font { family: Theme.font; pixelSize: core.s * 0.078; letterSpacing: 4; weight: Font.DemiBold }
    }

    // UNKNOWN_OUTCOME speaks in its own language: an unstable two-tone pulse
    // that resembles neither success nor an ordinary error.
    Rectangle {
        anchors.centerIn: parent
        width: core.s * 0.62; height: width; radius: width / 2
        color: "transparent"
        border.width: 3; border.color: Theme.unknown
        visible: core.state_ === "unknown"
        opacity: 0
        SequentialAnimation on opacity {
            running: core.state_ === "unknown"
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
        width: core.s * 0.6; height: width; radius: width / 2
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
