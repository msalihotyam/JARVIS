import QtQuick
import QtQuick.Shapes
import "../theme"

// The execution-status lamp: one physical glass instrument that is either
// lit (execution enabled) or switched off. Two bounded skins reproduce the
// approved references - "orb" (full calibration ring, thin axis, dotted
// arc) and "hud" (segmented ring, pill nodes, bright axis bar, side lines).
// Indicator only: it never changes state and has no click action.
// Geometry (at the 1920x1080 contract, kill switch = 42 px): ring 50 px,
// sphere 38 px, axis +-30 px, nodes on the ring. Gradients, opacity and
// transforms only; no shaders, no particles.
Item {
    id: orb
    property bool lit: true
    property string skin: "orb"          // "orb" | "hud"
    property bool ambient: true
    readonly property bool hud: skin === "hud"
    width: 60; height: 60

    // Staged power: the core ignites first, the glass follows, the halo last
    // (~420 ms). Powering down fades everything together (~300 ms).
    property real coreLevel: lit ? 1 : 0
    property real glassLevel: lit ? 1 : 0
    property real haloLevel: lit ? 1 : 0
    Behavior on coreLevel { NumberAnimation { duration: orb.lit ? 160 : 260; easing.type: Easing.OutCubic } }
    Behavior on glassLevel { SequentialAnimation { PauseAnimation { duration: orb.lit ? 80 : 0 } NumberAnimation { duration: orb.lit ? 240 : 300; easing.type: Easing.InOutCubic } } }
    Behavior on haloLevel { SequentialAnimation { PauseAnimation { duration: orb.lit ? 160 : 0 } NumberAnimation { duration: orb.lit ? 260 : 300; easing.type: Easing.InOutCubic } } }

    // Extremely slow luminance breathing while lit.
    property real breath: 1.0
    SequentialAnimation on breath {
        running: orb.lit && orb.ambient && Theme.ambient && orb.visible && orb.Window.active
        loops: Animation.Infinite
        NumberAnimation { to: 0.86; duration: 3400; easing.type: Easing.InOutSine }
        NumberAnimation { to: 1.0; duration: 3400; easing.type: Easing.InOutSine }
    }
    readonly property real glow: glassLevel * breath

    // Palette: emerald illumination vs. smoky graphite glass.
    readonly property color emerald: "#19e3a4"
    readonly property color emeraldDeep: "#0b8f68"
    readonly property color ice: "#d9fff3"
    readonly property color steel: "#8fb2cc"
    readonly property color steelDim: "#4e6a82"
    function mix(a, b, t) { a = Qt.color(a); b = Qt.color(b); return Qt.rgba(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t, a.a + (b.a - a.a) * t) }
    // Chrome (ring, nodes, axis): steel when off, emerald when lit.
    readonly property color chrome: mix(steel, emerald, glassLevel)
    readonly property real cx: width / 2
    readonly property real cy: height / 2
    readonly property real ringR: hud ? 24.5 : 25.5
    readonly property real sphereR: hud ? 18.5 : 19.5

    // -- halo (lit only) ---------------------------------------------------
    Shape {
        id: halo
        anchors.centerIn: parent
        width: 112; height: 112
        preferredRendererType: Shape.CurveRenderer
        opacity: orb.haloLevel * (0.55 + 0.45 * orb.breath) * (orb.hud ? 0.8 : 1)
        visible: opacity > 0.01
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: 56; centerY: 56; centerRadius: 56; focalX: 56; focalY: 56
                GradientStop { position: 0.00; color: Theme.alpha(orb.emerald, 0.42) }
                GradientStop { position: 0.28; color: Theme.alpha(orb.emerald, 0.18) }
                GradientStop { position: 0.55; color: Theme.alpha(orb.emerald, 0.05) }
                GradientStop { position: 1.00; color: "transparent" }
            }
            PathAngleArc { centerX: 56; centerY: 56; radiusX: 56; radiusY: 56; startAngle: 0; sweepAngle: 360 }
        }
    }

    // -- calibration ring ---------------------------------------------------
    // "orb": a full thin circle, a heavier segment on the right, a dotted arc
    // along the bottom, an inner faint circle. "hud": four arcs with gaps at
    // the cardinal points.
    Shape {
        anchors.centerIn: parent
        width: 60; height: 60
        preferredRendererType: Shape.CurveRenderer
        visible: !orb.hud
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.85); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 0; sweepAngle: 360 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.85); strokeWidth: 1.8; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR - 2.5; radiusY: orb.ringR - 2.5; startAngle: -30; sweepAngle: 62 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.85); strokeWidth: 1.8; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR - 2.5; radiusY: orb.ringR - 2.5; startAngle: 152; sweepAngle: 58 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.75); strokeWidth: 1; fillColor: "transparent"; strokeStyle: ShapePath.DashLine; dashPattern: [1.2, 2.2]
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR - 2.5; radiusY: orb.ringR - 2.5; startAngle: 104; sweepAngle: 44 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.45); strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR - 2.5; radiusY: orb.ringR - 2.5; startAngle: 190; sweepAngle: 40 } }
    }
    Shape {
        anchors.centerIn: parent
        width: 60; height: 60
        preferredRendererType: Shape.CurveRenderer
        visible: orb.hud
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.9); strokeWidth: 1.2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 8; sweepAngle: 74 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.9); strokeWidth: 1.2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 98; sweepAngle: 74 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.9); strokeWidth: 1.2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 188; sweepAngle: 74 } }
        ShapePath { strokeColor: Theme.alpha(orb.chrome, 0.9); strokeWidth: 1.2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 30; centerY: 30; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 278; sweepAngle: 74 } }
    }

    // -- vertical calibration axis, nodes and side marks --------------------
    Rectangle { x: orb.cx - 0.5; y: orb.cy - 30; width: 1; height: 60; color: orb.chrome; opacity: orb.hud ? 0.55 : 0.7; visible: !orb.hud }
    Rectangle { x: orb.cx - 0.5; y: orb.cy - 30; width: 1; height: 8; color: orb.chrome; opacity: 0.8; visible: orb.hud }
    Rectangle { x: orb.cx - 0.5; y: orb.cy + 22; width: 1; height: 8; color: orb.chrome; opacity: 0.8; visible: orb.hud }
    // Nodes: small ovals on the ring ("orb") / rounded pills ("hud").
    Repeater {
        model: 4
        Rectangle {
            readonly property bool vertical: index < 2
            readonly property real sign: index % 2 === 0 ? -1 : 1
            width: orb.hud ? (vertical ? 4 : 8) : (vertical ? 3.5 : 3)
            height: orb.hud ? (vertical ? 8 : 4) : (vertical ? 6 : 3)
            radius: orb.hud ? 1.5 : width / 2
            x: orb.cx + (vertical ? 0 : sign * orb.ringR) - width / 2
            y: orb.cy + (vertical ? sign * orb.ringR : 0) - height / 2
            color: mix(orb.steel, orb.ice, orb.coreLevel)
            opacity: 0.95
            Rectangle { anchors.centerIn: parent; width: parent.width + 2; height: parent.height + 2; radius: width / 2; color: orb.emerald; opacity: orb.coreLevel * 0.22; z: -1 }
        }
    }
    // Horizontal marks: short ticks outside the ring, longer thin lines for "hud".
    Rectangle { x: orb.cx - orb.ringR - (orb.hud ? 12 : 7); y: orb.cy - 0.5; width: orb.hud ? 11 : 6; height: 1; color: orb.chrome; opacity: 0.8 }
    Rectangle { x: orb.cx + orb.ringR + 1; y: orb.cy - 0.5; width: orb.hud ? 11 : 6; height: 1; color: orb.chrome; opacity: 0.8 }
    Rectangle { visible: !orb.hud; x: orb.cx - orb.ringR + 4; y: orb.cy - 1; width: 2; height: 2; radius: 1; color: orb.chrome; opacity: 0.6 }
    Rectangle { visible: !orb.hud; x: orb.cx + orb.ringR - 6; y: orb.cy - 1; width: 2; height: 2; radius: 1; color: orb.chrome; opacity: 0.6 }

    // -- the glass sphere -----------------------------------------------------
    Shape {
        id: sphere
        anchors.centerIn: parent
        width: orb.sphereR * 2; height: width
        preferredRendererType: Shape.CurveRenderer
        readonly property real r: orb.sphereR
        readonly property real g: orb.glow
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: sphere.r; centerY: sphere.r; centerRadius: sphere.r
                focalX: sphere.r * (1 - 0.16 * (1 - sphere.g)); focalY: sphere.r * (1 - 0.26 * (1 - sphere.g)); focalRadius: 0
                GradientStop { position: 0.00; color: orb.mix("#16202c", "#e8fff8", sphere.g) }
                GradientStop { position: 0.05; color: orb.mix("#131b26", "#9dffe0", sphere.g) }
                GradientStop { position: 0.16; color: orb.mix("#0f1620", "#2fdca4", sphere.g) }
                GradientStop { position: 0.42; color: orb.mix("#090e15", "#0c8f65", sphere.g) }
                GradientStop { position: 0.72; color: orb.mix("#04070c", "#095942", sphere.g) }
                GradientStop { position: 0.88; color: orb.mix("#0c1420", "#14a88a", sphere.g) }
                GradientStop { position: 0.96; color: orb.mix("#2b4159", "#3fd6b4", sphere.g) }
                GradientStop { position: 1.00; color: orb.mix("#8ea9c4", "#7fefd2", sphere.g) }
            }
            PathAngleArc { centerX: sphere.r; centerY: sphere.r; radiusX: sphere.r; radiusY: sphere.r; startAngle: 0; sweepAngle: 360 }
        }
    }
    // Glass rim: thin bright edge, brighter on the upper-left where the light falls.
    Rectangle { anchors.centerIn: parent; width: orb.sphereR * 2; height: width; radius: width / 2; color: "transparent"; border.width: 1
        border.color: orb.mix("#b4cbe0", "#a9ffe9", orb.glassLevel); opacity: 0.95 }
    Shape {
        anchors.centerIn: parent; width: orb.sphereR * 2; height: width
        preferredRendererType: Shape.CurveRenderer
        ShapePath { strokeColor: Theme.alpha(orb.mix("#e6f1fa", "#eafff9", orb.glassLevel), 0.75 - 0.25 * orb.glassLevel); strokeWidth: 1.6 - 0.5 * orb.glassLevel; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: orb.sphereR; centerY: orb.sphereR; radiusX: orb.sphereR - 2.2; radiusY: orb.sphereR - 2.2; startAngle: 198; sweepAngle: 66 } }
        ShapePath { strokeColor: Theme.alpha(orb.mix("#dbe9f5", "#f0fffb", orb.glassLevel), 0.85); strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: orb.sphereR; centerY: orb.sphereR; radiusX: orb.sphereR - 3.4; radiusY: orb.sphereR - 3.4; startAngle: 28; sweepAngle: 34 } }
    }
    // Soft specular pool upper-left.
    Rectangle {
        x: orb.cx - orb.sphereR * 0.72; y: orb.cy - orb.sphereR * 0.86
        width: orb.sphereR * 0.9; height: orb.sphereR * 0.55; radius: height / 2
        rotation: -30
        gradient: Gradient { GradientStop { position: 0; color: Qt.rgba(1, 1, 1, 0.08) } GradientStop { position: 1; color: "transparent" } }
    }

    // -- axis inside the sphere and the luminous core -------------------------
    // "hud": a bright bar from near the top of the sphere to the lower dot.
    Rectangle { visible: orb.hud; x: orb.cx - 1.25; y: orb.cy - orb.sphereR + 2.5; width: 2.5; height: orb.sphereR * 1.58; radius: 1.25
        color: orb.mix("#dfeaf5", "#ffffff", orb.coreLevel); opacity: 1
        Rectangle { anchors.centerIn: parent; width: 8; height: parent.height + 4; radius: 4; color: orb.emerald; opacity: orb.coreLevel * 0.55; z: -1 } }
    // "orb": the thin axis is visible through the glass.
    Rectangle { visible: !orb.hud; x: orb.cx - 0.5; y: orb.cy - orb.sphereR; width: 1; height: orb.sphereR * 2; color: orb.mix("#9fb8cc", "#c9ffee", orb.coreLevel); opacity: 0.55 }
    // Core: centre light with halo; secondary dots above / below (orb) or below only (hud).
    Rectangle { anchors.centerIn: parent; width: orb.hud ? 14 : 20; height: width; radius: width / 2; color: orb.emerald; opacity: orb.coreLevel * 0.5 * orb.breath }
    Rectangle { anchors.centerIn: parent; width: orb.hud ? 8 : 12; height: width; radius: width / 2; color: "#c8fff0"; opacity: orb.coreLevel * 0.35 * orb.breath }
    Rectangle { anchors.centerIn: parent; width: orb.hud ? 11 : 9; height: width; radius: width / 2; color: "transparent"; border.width: orb.hud ? 1.6 : 1
        border.color: orb.mix("#9fb4c8", "#c9ffee", orb.coreLevel); opacity: orb.hud ? 0.8 : 0.45 * orb.coreLevel }
    Rectangle { anchors.centerIn: parent; width: (orb.hud ? 4 : 3) + 3 * orb.coreLevel; height: width; radius: width / 2; color: orb.mix("#b3c6d8", "#ffffff", orb.coreLevel); opacity: 0.6 + 0.4 * orb.coreLevel }
    Rectangle { visible: !orb.hud; x: orb.cx - 1.75; y: orb.cy - orb.sphereR * 0.56 - 1.75; width: 3.5; height: 3.5; radius: 1.75; color: orb.mix("#8fa6ba", "#ffffff", orb.coreLevel); opacity: 0.25 + 0.75 * orb.coreLevel
        Rectangle { anchors.centerIn: parent; width: 10; height: 10; radius: 5; color: orb.emerald; opacity: orb.coreLevel * 0.5; z: -1 } }
    Rectangle { x: orb.cx - (orb.hud ? 2.5 : 1.5); y: orb.cy + orb.sphereR * 0.58 - (orb.hud ? 2.5 : 1.5); width: orb.hud ? 5 : 3; height: width; radius: width / 2
        color: orb.mix("#a3b8cb", "#f4fffb", orb.coreLevel); opacity: (orb.hud ? 0.7 : 0.25) + (orb.hud ? 0.3 : 0.75) * orb.coreLevel
        Rectangle { anchors.centerIn: parent; width: parent.width + 6; height: parent.height + 6; radius: width / 2; color: orb.emerald; opacity: orb.coreLevel * 0.45; z: -1 } }
    // Inner illumination bloom over the lower half (the lit reference glows most below centre).
    Rectangle { anchors.centerIn: parent; anchors.verticalCenterOffset: orb.sphereR * 0.18; width: orb.sphereR * 1.5; height: orb.sphereR * 1.2; radius: width / 2
        color: orb.emerald; opacity: orb.glow * (orb.hud ? 0.14 : 0.09) }
}
