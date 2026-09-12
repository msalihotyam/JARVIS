import QtQuick
import QtQuick.Shapes
import "../theme"

// The JARVIS core: the dominant identity element of the header, and the
// place live state is read. Reconstructed from the approved Canonical Space
// reference, whose ring family measures (as fractions of the envelope):
// sphere 0.39, then a clean dark gap, then hairlines at 0.52, 0.63, 0.71,
// 0.75, 0.82, 0.90 and sparse marks at 0.98. Every stroke is a hairline;
// the reference has no thick neon rings.
//
// Motion is engineered, not decorative: exactly two layers turn, very
// slowly and in opposite directions, plus one sweep that exists only while
// reasoning. Geometry is static Shapes - no particles, no shader effects.
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
    readonly property real c: width / 2           // centre, in local coordinates
    readonly property real dim: dormant ? 0.22 : 1
    implicitWidth: 300; implicitHeight: 300

    Behavior on stateColor { ColorAnimation { duration: 260; easing.type: Easing.InOutCubic } }

    // A single luminance drift drives the halo and the inner rim: tiny,
    // opacity-only, and shared so there is one animation, not several.
    QtObject {
        id: glow
        property real energy: 1.0
        SequentialAnimation on energy {
            running: core.ambientMotion
            loops: Animation.Infinite
            NumberAnimation { to: core.running ? 1.5 : 1.16
                              duration: core.running ? 1100 : 3400; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: core.running ? 1100 : 3400; easing.type: Easing.InOutSine }
        }
    }

    // -- halo ---------------------------------------------------------------
    Shape {
        id: halo
        anchors.centerIn: parent
        width: core.s * 1.2; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: (core.dormant ? 0.12 : 0.34) * glow.energy * Theme.glowStrength
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: halo.width / 2; centerY: halo.height / 2; centerRadius: halo.width / 2
                focalX: centerX; focalY: centerY
                GradientStop { position: 0.00; color: Theme.alpha(core.stateColor, 0.30) }
                GradientStop { position: 0.30; color: Theme.alpha(core.stateColor, 0.09) }
                GradientStop { position: 0.62; color: Theme.alpha(core.stateColor, 0.02) }
                GradientStop { position: 1.00; color: "transparent" }
            }
            PathAngleArc { centerX: halo.width / 2; centerY: halo.height / 2; radiusX: halo.width / 2; radiusY: halo.height / 2; startAngle: 0; sweepAngle: 360 }
        }
    }

    // -- the static hairline ring family -----------------------------------
    // One Shape carries every fixed ring, so the whole family is a single
    // batched node instead of a stack of layers.
    Shape {
        id: rings
        anchors.centerIn: parent
        width: core.s; height: core.s
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dim
        ShapePath {      // 0.98: the outermost, barely there
            strokeColor: Theme.alpha(core.stateColor, 0.12); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.49; radiusY: core.s * 0.49; startAngle: 0; sweepAngle: 360 }
        }
        ShapePath {      // 0.90
            strokeColor: Theme.alpha(core.stateColor, 0.20); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.45; radiusY: core.s * 0.45; startAngle: 0; sweepAngle: 360 }
        }
        ShapePath {      // 0.75
            strokeColor: Theme.alpha(core.stateColor, 0.26); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.375; radiusY: core.s * 0.375; startAngle: 0; sweepAngle: 360 }
        }
        ShapePath {      // 0.71: the strongest of the family in the reference
            strokeColor: Theme.alpha(core.stateColor, 0.34); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.355; radiusY: core.s * 0.355; startAngle: 0; sweepAngle: 360 }
        }
        ShapePath {      // 0.52: the rim of the clean gap around the sphere
            strokeColor: Theme.alpha(core.stateColor, 0.28); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.26; radiusY: core.s * 0.26; startAngle: 0; sweepAngle: 360 }
        }
        // Two slightly brighter fragments on the 0.71 ring: the reference's
        // restrained highlights, not a second ring.
        ShapePath {
            strokeColor: Theme.alpha(core.stateColor, 0.62); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.355; radiusY: core.s * 0.355; startAngle: 202; sweepAngle: 26 }
        }
        ShapePath {
            strokeColor: Theme.alpha(core.stateColor, 0.45); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.355; radiusY: core.s * 0.355; startAngle: 38; sweepAngle: 18 }
        }
        // Radial detailing: four fine dashed spokes crossing the family.
        ShapePath {
            strokeColor: Theme.alpha(core.stateColor, 0.22); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 4]
            startX: core.c + core.s * 0.295 * Math.cos(0.49); startY: core.c + core.s * 0.295 * Math.sin(0.49)
            PathLine { x: core.c + core.s * 0.475 * Math.cos(0.49); y: core.c + core.s * 0.475 * Math.sin(0.49) }
        }
        ShapePath {
            strokeColor: Theme.alpha(core.stateColor, 0.30); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 4]
            startX: core.c + core.s * 0.295 * Math.cos(2.06); startY: core.c + core.s * 0.295 * Math.sin(2.06)
            PathLine { x: core.c + core.s * 0.475 * Math.cos(2.06); y: core.c + core.s * 0.475 * Math.sin(2.06) }
        }
        ShapePath {
            strokeColor: Theme.alpha(core.stateColor, 0.30); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 4]
            startX: core.c + core.s * 0.295 * Math.cos(3.63); startY: core.c + core.s * 0.295 * Math.sin(3.63)
            PathLine { x: core.c + core.s * 0.475 * Math.cos(3.63); y: core.c + core.s * 0.475 * Math.sin(3.63) }
        }
    }
    // Sparse marks on the outermost ring: static, 12 of them.
    Item {
        anchors.centerIn: parent
        width: core.s * 0.98; height: width
        opacity: core.dim
        Repeater {
            model: 12
            Rectangle { width: 1; height: index % 3 === 0 ? 6 : 3; color: core.stateColor
                opacity: index % 3 === 0 ? 0.55 : 0.3
                x: core.s * 0.49 - 0.5; y: 0
                transform: Rotation { origin.x: 0.5; origin.y: core.s * 0.49; angle: index * 30 } }
        }
    }
    // Three restrained luminous nodes, fixed on the family.
    Repeater {
        model: 3
        Item {
            readonly property real ang: [0.72, 2.62, 4.45][index]
            readonly property real rad: core.s * [0.355, 0.45, 0.26][index]
            x: core.c + rad * Math.cos(ang) - 2; y: core.c + rad * Math.sin(ang) - 2
            width: 4; height: 4
            opacity: core.dim
            Rectangle { anchors.fill: parent; radius: 2; color: Qt.lighter(core.stateColor, 1.3); opacity: 0.95 }
            Rectangle { anchors.centerIn: parent; width: 11; height: 11; radius: 5.5; color: core.stateColor; opacity: 0.22 }
        }
    }

    // -- rotating layer 1: the calibration ring, clockwise, very slow -------
    Item {
        id: calibration
        anchors.centerIn: parent
        width: core.s * 0.82; height: width
        opacity: core.dim
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 0; to: 360; duration: core.running ? 26000 : 96000 }
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: core.stateColor; opacity: 0.29 }
        Repeater {
            model: 60
            Rectangle {
                width: 1; height: index % 15 === 0 ? 8 : (index % 5 === 0 ? 5 : 2.5)
                color: core.stateColor
                opacity: index % 15 === 0 ? 0.6 : (index % 5 === 0 ? 0.38 : 0.2)
                x: calibration.width / 2 - 0.5; y: 0
                transform: Rotation { origin.x: 0.5; origin.y: calibration.height / 2; angle: index * 6 }
            }
        }
    }

    // -- rotating layer 2: the segmented ring, counter, slower still --------
    Shape {
        id: segments
        anchors.centerIn: parent
        width: core.s * 0.63; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: core.dim
        RotationAnimator on rotation { running: core.turning; loops: Animation.Infinite; from: 360; to: 0; duration: core.running ? 21000 : 74000 }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.30); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 0; sweepAngle: 360 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.9); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 6; sweepAngle: 58 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.55); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 126; sweepAngle: 42 } }
        ShapePath { strokeColor: Theme.alpha(core.stateColor, 0.55); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: segments.width / 2; centerY: segments.height / 2; radiusX: segments.width / 2; radiusY: segments.height / 2; startAngle: 244; sweepAngle: 42 } }
        Rectangle { width: 4; height: 4; radius: 2; color: Qt.lighter(core.stateColor, 1.3)
            x: segments.width / 2 + segments.width / 2 * Math.cos(64 * Math.PI / 180) - 2
            y: segments.height / 2 + segments.height / 2 * Math.sin(64 * Math.PI / 180) - 2
            Rectangle { anchors.centerIn: parent; width: 11; height: 11; radius: 5.5; color: core.stateColor; opacity: 0.26 } }
    }

    // Running: one bright fast sweep, alive only while reasoning.
    Shape {
        id: sweep
        anchors.centerIn: parent
        width: core.s * 0.52; height: width
        preferredRendererType: Shape.CurveRenderer
        visible: core.running
        RotationAnimator on rotation { running: core.running && core.active && core.visible; loops: Animation.Infinite; from: 0; to: 360; duration: 9000 }
        ShapePath { strokeColor: core.stateColor; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: sweep.width / 2; centerY: sweep.height / 2; radiusX: sweep.width / 2; radiusY: sweep.height / 2; startAngle: 0; sweepAngle: 44 } }
    }

    // -- the sphere: a dark glass globe, lit from the upper left ------------
    Shape {
        id: sphere
        anchors.centerIn: parent
        width: core.s * 0.39; height: width
        preferredRendererType: Shape.CurveRenderer
        readonly property real r: width / 2
        opacity: core.dormant ? 0.5 : 1
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: sphere.r; centerY: sphere.r; centerRadius: sphere.r
                focalX: sphere.r * 0.78; focalY: sphere.r * 1.16; focalRadius: 0
                GradientStop { position: 0.00; color: "#04101c" }
                GradientStop { position: 0.46; color: "#082138" }
                GradientStop { position: 0.78; color: Qt.darker(core.stateColor, 3.2) }
                GradientStop { position: 0.93; color: Qt.darker(core.stateColor, 1.7) }
                GradientStop { position: 1.00; color: Qt.lighter(core.stateColor, 1.15) }
            }
            PathAngleArc { centerX: sphere.r; centerY: sphere.r; radiusX: sphere.r; radiusY: sphere.r; startAngle: 0; sweepAngle: 360 }
        }
    }
    // The atmospheric limb: brightest along the upper-left edge, with two
    // thin specular arcs for glass volume. Arcs only, no cubic facets.
    Shape {
        id: limb
        anchors.centerIn: parent
        width: core.s * 0.39; height: width
        preferredRendererType: Shape.CurveRenderer
        readonly property real r: width / 2
        opacity: core.dormant ? 0.25 : 1
        ShapePath {
            strokeColor: Theme.alpha(Qt.lighter(core.stateColor, 1.45), 0.7); strokeWidth: core.s * 0.0075
            fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: limb.r; centerY: limb.r; radiusX: limb.r - core.s * 0.004; radiusY: limb.r - core.s * 0.004; startAngle: 172; sweepAngle: 116 }
        }
    }
    // The rim is not a uniform ring: it is brightest where the light falls
    // and fades to almost nothing on the far side.
    Shape {
        id: rim
        anchors.centerIn: parent; width: core.s * 0.39; height: width
        preferredRendererType: Shape.CurveRenderer
        readonly property real r: width / 2
        opacity: core.dormant ? 0.3 : 1
        ShapePath { strokeColor: Theme.alpha(Qt.lighter(core.stateColor, 1.1), 0.7); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: rim.r; centerY: rim.r; radiusX: rim.r - 0.5; radiusY: rim.r - 0.5; startAngle: 130; sweepAngle: 210 } }
        ShapePath { strokeColor: Theme.alpha(Qt.lighter(core.stateColor, 1.1), 0.42); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: rim.r; centerY: rim.r; radiusX: rim.r - 0.5; radiusY: rim.r - 0.5; startAngle: 340; sweepAngle: 150 } }
        ShapePath { strokeColor: Theme.alpha(Qt.lighter(core.stateColor, 1.2), 0.9); strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: rim.r; centerY: rim.r; radiusX: rim.r - 0.8; radiusY: rim.r - 0.8; startAngle: 178; sweepAngle: 92 } }
    }
    // JARVIS, centred in the sphere: a shadow pass, then the face.
    Text {
        anchors.centerIn: parent; anchors.verticalCenterOffset: 1; anchors.horizontalCenterOffset: 1
        text: "JARVIS"; color: "#00060c"; opacity: 0.65
        font { family: Theme.font; pixelSize: Math.round(core.s * 0.059); letterSpacing: core.s * 0.012; weight: Font.Normal }
    }
    Text {
        anchors.centerIn: parent
        text: "JARVIS"; color: core.dormant ? Theme.textMuted : "#f3fbff"
        font { family: Theme.font; pixelSize: Math.round(core.s * 0.059); letterSpacing: core.s * 0.012; weight: Font.Normal }
    }

    // UNKNOWN_OUTCOME speaks in its own language: an unstable two-tone pulse
    // that resembles neither success nor an ordinary error.
    Rectangle {
        anchors.centerIn: parent
        width: core.s * 0.52; height: width; radius: width / 2
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
        width: core.s * 0.5; height: width; radius: width / 2
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
