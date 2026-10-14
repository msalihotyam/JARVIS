import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import "../../theme"

// Reactor II: the arc-reactor look of the JARVIS core (DFCOREA prototype).
//
// THE LAYER INTERFACE. A design layer is pure drawing. JarvisCore owns the
// state, every animation, the clock and the timers; it hands itself to the
// layer as `core`, and the layer reads these values from it and nothing
// else. A layer defines no applyState / fire / stopEvent / Travel and no
// Timer or animation of its own; motion here is bindings on core values.
// A layer may ignore any value it does not draw. JarvisCore references no
// id inside a layer. A new design = one layer file + one registry line in
// JarvisCore (`designs`).
//
//   geometry       s (diameter px), c (centre px), environment (header packet allowed)
//   state          state_, running, executing, reasoning, listening, blocked,
//                  dormant, calm, alive, typing
//   palette        blue, glint, info, brass, warning, danger, success,
//                  coldMetal, sigTint (amber attention / red error)
//   light          level, lvl, eventLevel, focusBoost, jolt, typeBoost,
//                  squeeze, okLevel
//   power          circulation, ringEnergy, ringFloor, nucleus, residual,
//                  iris (1 = chamber sealed, 0 = open)
//   boot           booted, bootFocus, bootRim, bootGlow, spin, spin2
//   clock-driven   breath, rimPulse, pointA, sweepAngle, sweepGain, markerGain,
//                  mainAngle, secondAngle, fieldAngle, flow (0..1),
//                  orbitPhase, clockT, idleOrbitPeak
//   sector light   litA, litB, blue0..blue5, sig0..sig5, hold
//   event objects  wave {scale, opacity}, ringWave {scale, opacity} (scale of
//                  the core radius), packet {x, y, opacity} (its CENTRE in px),
//                  rimPass {rotation, opacity}, markerA / markerB {rotation}
//
// Geometry (of D = s): bed 0.56 · sector housings r 0.235 band 0.05 ·
// brass frame r 0.27 · main ring r 0.31 band 0.03 · tick ring r 0.355 ·
// field loops within r 0.21 · plasma r 0.17 · chamber rim r 0.13 · iris r 0.15.
Item {
    id: reactor
    required property Item core

    function mix(a, b, t) { return Qt.rgba(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t, 1) }
    readonly property real ringAlpha: core.ringFloor + (1 - core.ringFloor) * core.ringEnergy
    readonly property color ringCol: mix(core.coldMetal, core.blue, core.ringEnergy)
    readonly property real sectorR: 0.235 * core.s
    readonly property real sectorBand: 0.05 * core.s
    readonly property real mainR: 0.31 * core.s
    readonly property real mainBand: 0.03 * core.s

    // Halo: the level lifts it a little; never a fog.
    Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
            opacity: (0.35 + 0.6 * (core.lvl - 0.22) + 0.3 * core.bootGlow) * core.residual
        ShapePath { strokeWidth: -1
            fillGradient: RadialGradient { centerX: core.c; centerY: core.c; centerRadius: core.s * 0.5; focalX: core.c; focalY: core.c
                GradientStop { position: 0; color: Theme.alpha(core.blue, 0.40) }
                GradientStop { position: 0.45; color: Theme.alpha(core.blue, 0.12) }
                GradientStop { position: 1; color: Theme.alpha(core.blue, 0) } }
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.5; radiusY: core.s * 0.5; startAngle: 0; sweepAngle: 360 } }
    }
    // L1 bed: dark material with a lit upper edge.
    Rectangle { anchors.centerIn: parent; width: core.s * 0.56; height: width; radius: width / 2
                gradient: Gradient { GradientStop { position: 0; color: "#0b1a2c" } GradientStop { position: 1; color: "#040b14" } }
                border.width: 1; border.color: Theme.alpha(core.glint, 0.10) }
    // L3 the six sector housings: dark bodies with lit inner and outer contours.
    Repeater { model: 6
        Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer; opacity: 0.4 + 0.6 * core.bootRim
            readonly property real a0: index * 60 - 24
            ShapePath { strokeColor: "#0a1626"; strokeWidth: reactor.sectorBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR; radiusY: reactor.sectorR; startAngle: a0; sweepAngle: 48 } }
            ShapePath { strokeColor: Theme.alpha(reactor.ringCol, 0.55 * reactor.ringAlpha); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR - reactor.sectorBand / 2; radiusY: reactor.sectorR - reactor.sectorBand / 2; startAngle: a0; sweepAngle: 48 } }
            ShapePath { strokeColor: Theme.alpha(reactor.ringCol, 0.35 * reactor.ringAlpha); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR + reactor.sectorBand / 2; radiusY: reactor.sectorR + reactor.sectorBand / 2; startAngle: a0; sweepAngle: 48 } }
        } }
    // Brass frame: the fixed instrument ring and twelve bezel ticks (metal, kept when dormant).
    Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer; opacity: 0.5 + 0.5 * core.bootRim
        ShapePath { strokeColor: Theme.alpha(core.brass, 0.42); strokeWidth: 1.2; fillColor: "transparent"
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.27; radiusY: core.s * 0.27; startAngle: 0; sweepAngle: 360 } } }
    Repeater { model: 12
        Rectangle { readonly property bool major: index % 3 === 0
            readonly property real r1: core.s * (major ? 0.286 : 0.278)
            x: core.c - width / 2; y: core.c - r1; width: major ? 2 : 1; height: core.s * (major ? 0.024 : 0.016)
            color: Theme.alpha(core.brass, major ? 0.9 : 0.5); opacity: 0.5 + 0.5 * core.bootRim
            transform: Rotation { origin.x: (index % 3 === 0 ? 2 : 1) / 2; origin.y: core.s * (index % 3 === 0 ? 0.286 : 0.278); angle: index * 30 } } }
    // L5 tick ring: 36 fine ticks and a hairline, counter-clockwise.
    Item { anchors.fill: parent; rotation: core.secondAngle; opacity: (0.3 + 0.7 * core.bootRim) * (0.6 + 0.4 * (core.lvl - 0.22)) * reactor.ringAlpha
        Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
            ShapePath { strokeColor: Theme.alpha(reactor.ringCol, 0.22); strokeWidth: 1; fillColor: "transparent"
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.355; radiusY: core.s * 0.355; startAngle: 0; sweepAngle: 360 } } }
        Repeater { model: 36
            Rectangle { readonly property bool major: index % 3 === 0
                x: core.c - 0.5; y: core.c - core.s * (major ? 0.402 : 0.383); width: 1; height: core.s * (major ? 0.03 : 0.018)
                color: Theme.alpha(reactor.ringCol, major ? 0.8 : 0.45)
                transform: Rotation { origin.x: 0.5; origin.y: core.s * (index % 3 === 0 ? 0.402 : 0.383); angle: index * 10 } } }
    }

    Item {
        anchors.fill: parent
        // One bloom UNDER the emissive layer (absent under the software backend;
        // the structure reads without it).
        MultiEffect { source: emissive; anchors.fill: emissive; blurEnabled: true; blur: 0.8; blurMax: 40; brightness: 0.05
                      opacity: 0.6 * core.residual; visible: opacity > 0.02 }
        Item {
            id: emissive
            anchors.fill: parent
            // Field loops: six dipole petals round the nucleus, slowly counter-rotating,
            // with two energy points travelling each loop (the magnetic circulation).
            Item { anchors.fill: parent; rotation: core.fieldAngle + core.jolt * 0.5; opacity: core.ringEnergy * (0.3 + 0.7 * core.bootRim)
                Repeater { model: 6
                    Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                        readonly property real cx: Math.cos(index * Math.PI / 3); readonly property real sx: Math.sin(index * Math.PI / 3)
                        ShapePath { strokeColor: Theme.alpha(core.info, 0.16 + 0.5 * (core.lvl - 0.22)); strokeWidth: 1; fillColor: "transparent"
                            startX: core.c + core.s * 0.06 * sx; startY: core.c - core.s * 0.06 * cx
                            PathCubic { x: core.c - core.s * 0.06 * sx; y: core.c + core.s * 0.06 * cx
                                        control1X: core.c + core.s * (0.27 * cx + 0.07 * sx); control1Y: core.c + core.s * (0.27 * sx - 0.07 * cx)
                                        control2X: core.c + core.s * (0.27 * cx - 0.07 * sx); control2Y: core.c + core.s * (0.27 * sx + 0.07 * cx) } } } }
                Repeater { model: 12
                    Rectangle {
                        readonly property int loop: index % 6
                        readonly property real t: (core.flow + (index < 6 ? 0 : 0.5) + loop * 0.13) % 1
                        readonly property real u: 1 - t
                        readonly property real cx: Math.cos(loop * Math.PI / 3); readonly property real sx: Math.sin(loop * Math.PI / 3)
                        readonly property real p0x: core.c + core.s * 0.06 * sx; readonly property real p0y: core.c - core.s * 0.06 * cx
                        readonly property real p1x: core.c + core.s * (0.27 * cx + 0.07 * sx); readonly property real p1y: core.c + core.s * (0.27 * sx - 0.07 * cx)
                        readonly property real p2x: core.c + core.s * (0.27 * cx - 0.07 * sx); readonly property real p2y: core.c + core.s * (0.27 * sx + 0.07 * cx)
                        readonly property real p3x: core.c - core.s * 0.06 * sx; readonly property real p3y: core.c + core.s * 0.06 * cx
                        width: core.s * 0.016; height: width; radius: width / 2; color: core.glint; antialiasing: true
                        x: u * u * u * p0x + 3 * u * u * t * p1x + 3 * u * t * t * p2x + t * t * t * p3x - width / 2
                        y: u * u * u * p0y + 3 * u * u * t * p1y + 3 * u * t * t * p2y + t * t * t * p3y - height / 2
                        opacity: core.markerGain * core.circulation * (0.35 + 0.65 * Math.sin(t * Math.PI)) * (0.5 + 1.2 * (core.lvl - 0.22))
                    } }
            }
            // Sector light: blue information faces (state rhythms + the blue Travel) and
            // the amber / red faces (sig Travel + the hold), inset into the housings.
            Repeater { model: 6
                Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                    readonly property real lit: (core.listening && index !== 0 && index !== 3) ? 0 : (index % 2 === 0 ? core.litA : core.litB)
                    readonly property real lv: Math.max(lit * 0.9, [core.blue0, core.blue1, core.blue2, core.blue3, core.blue4, core.blue5][index]) * core.ringEnergy
                    opacity: lv; visible: lv > 0.01
                    ShapePath { strokeColor: Theme.alpha(core.info, 0.25); strokeWidth: reactor.sectorBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                        PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR; radiusY: reactor.sectorR; startAngle: index * 60 - 18; sweepAngle: 36 } }
                    ShapePath { strokeColor: Theme.alpha(core.glint, 0.85); strokeWidth: reactor.sectorBand * 0.45; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                        PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR; radiusY: reactor.sectorR; startAngle: index * 60 - 16; sweepAngle: 32 } }
                } }
            Repeater { model: 6
                Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                    readonly property real lv: Math.max([core.sig0, core.sig1, core.sig2, core.sig3, core.sig4, core.sig5][index], core.hold) * core.ringEnergy
                    opacity: lv; visible: lv > 0.01
                    ShapePath { strokeColor: Theme.alpha(core.sigTint, 0.3); strokeWidth: reactor.sectorBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                        PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR; radiusY: reactor.sectorR; startAngle: index * 60 - 18; sweepAngle: 36 } }
                    ShapePath { strokeColor: Theme.alpha(Qt.lighter(core.sigTint, 1.15), 0.9); strokeWidth: reactor.sectorBand * 0.45; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                        PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.sectorR; radiusY: reactor.sectorR; startAngle: index * 60 - 16; sweepAngle: 32 } }
                } }
            // L4 main ring: eight segments, clockwise; the green success face rides on it.
            Item { anchors.fill: parent; rotation: core.mainAngle
                   opacity: (0.3 + 0.7 * core.bootRim) * Math.min(1, 0.8 + 0.5 * (core.lvl - 0.22)) * reactor.ringAlpha
                Repeater { model: 8
                    Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                        ShapePath { strokeColor: Theme.alpha(reactor.ringCol, 0.62); strokeWidth: reactor.mainBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.mainR; radiusY: reactor.mainR; startAngle: index * 45 + 5; sweepAngle: 34 } }
                        ShapePath { strokeColor: Theme.alpha(core.glint, 0.5); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.mainR - reactor.mainBand / 2 + 0.5; radiusY: reactor.mainR - reactor.mainBand / 2 + 0.5; startAngle: index * 45 + 5; sweepAngle: 34 } } } }
                Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer; opacity: core.okLevel; visible: opacity > 0.01
                    ShapePath { strokeColor: Theme.alpha(core.success, 0.95); strokeWidth: reactor.mainBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                        PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.mainR; radiusY: reactor.mainR; startAngle: 17; sweepAngle: 10 } } }
            }
            // L6 the energy sweep: a 28 degree highlight with a tail, on the main ring.
            Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer; rotation: core.sweepAngle + core.jolt
                    opacity: (0.5 + 1.0 * (core.lvl - 0.22)) * core.sweepGain * core.bootRim * core.circulation; visible: opacity > 0.01
                ShapePath { strokeColor: Theme.alpha(core.glint, 0.95); strokeWidth: reactor.mainBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.mainR; radiusY: reactor.mainR; startAngle: -14; sweepAngle: 28 } }
                ShapePath { strokeColor: Theme.alpha(core.glint, 0.4); strokeWidth: reactor.mainBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.mainR; radiusY: reactor.mainR; startAngle: -26; sweepAngle: 12 } }
                ShapePath { strokeColor: Theme.alpha(core.glint, 0.14); strokeWidth: reactor.mainBand; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: reactor.mainR; radiusY: reactor.mainR; startAngle: -38; sweepAngle: 12 } }
            }
            // L7 two energy points on the ring tracks.
            Item { anchors.fill: parent; rotation: core.markerA.rotation
                Rectangle { width: core.s * 0.022; height: width; radius: width / 2; color: core.glint; antialiasing: true
                            x: core.c + core.s * 0.33 - width / 2; y: core.c - height / 2; opacity: core.pointA * core.bootRim * core.circulation } }
            Item { anchors.fill: parent; rotation: core.markerB.rotation
                Rectangle { width: core.s * 0.018; height: width; radius: width / 2; color: core.glint; antialiasing: true
                            x: core.c + core.s * 0.37 - width / 2; y: core.c - height / 2; opacity: 0.55 * core.bootRim * core.circulation } }
            Rectangle {
                width: core.s * 0.03; height: width; radius: width / 2; color: core.glint; antialiasing: true
                visible: core.environment && opacity > 0.01
                readonly property real rad: core.orbitPhase * Math.PI / 180
                x: core.c - core.s * 0.292 + core.s * 0.699 * Math.cos(rad) - width / 2
                y: core.c + core.s * 0.123 * Math.sin(rad) - height / 2
                opacity: core.idleOrbitPeak * core.bootRim * core.circulation * Math.max(0, Math.min(1, (core.orbitPhase - 78) / 14, (282 - core.orbitPhase) / 14))
            }
            // Event light: the wave (scaled outward), the retune ring, the packet.
            Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                    scale: core.wave.scale; opacity: core.wave.opacity; visible: opacity > 0.01
                ShapePath { strokeColor: Theme.alpha(core.info, 0.9); strokeWidth: core.s * 0.014; fillColor: "transparent"
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.5; radiusY: core.s * 0.5; startAngle: 0; sweepAngle: 360 } }
                ShapePath { strokeColor: Theme.alpha(core.info, 0.3); strokeWidth: core.s * 0.05; fillColor: "transparent"
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.48; radiusY: core.s * 0.48; startAngle: 0; sweepAngle: 360 } } }
            Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                    scale: core.ringWave.scale; opacity: core.ringWave.opacity; visible: opacity > 0.01
                ShapePath { strokeColor: Theme.alpha(core.info, 0.7); strokeWidth: core.s * 0.008; fillColor: "transparent"
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.5; radiusY: core.s * 0.5; startAngle: 0; sweepAngle: 360 } } }
            Rectangle { width: core.s * 0.03; height: width; radius: width / 2; color: core.glint; antialiasing: true
                        x: core.packet.x - width / 2; y: core.packet.y - height / 2
                        opacity: core.packet.opacity; visible: opacity > 0.01 }

            // L2 the plasma nucleus: a radial body breathing in light, barely in size.
            Item { anchors.centerIn: parent; width: core.s * 0.5; height: width
                   scale: (1 + 0.16 * (core.breath - 1)) * core.squeeze
                Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                        opacity: Math.min(1, (0.34 + 1.1 * (core.lvl - 0.22) + core.focusBoost + 0.35 * core.bootGlow) * core.breath) * core.bootFocus * core.nucleus
                    ShapePath { strokeWidth: -1
                        fillGradient: RadialGradient { centerX: core.s * 0.25; centerY: core.s * 0.25; centerRadius: core.s * 0.17; focalX: core.s * 0.25; focalY: core.s * 0.25
                            GradientStop { position: 0; color: "#ffffff" }
                            GradientStop { position: 0.18; color: Theme.alpha(core.glint, 0.95) }
                            GradientStop { position: 0.45; color: Theme.alpha(core.blue, 0.55) }
                            GradientStop { position: 1; color: Theme.alpha(core.blue, 0) } }
                        PathAngleArc { centerX: core.s * 0.25; centerY: core.s * 0.25; radiusX: core.s * 0.17; radiusY: core.s * 0.17; startAngle: 0; sweepAngle: 360 } }
                }
                // The chamber rim and the focus-return pass.
                Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                        opacity: (core.rimPulse * 0.8 + 0.2 * core.bootGlow) * core.bootRim * core.nucleus
                    ShapePath { strokeColor: Theme.alpha(core.glint, 0.7); strokeWidth: 1.5; fillColor: "transparent"
                        PathAngleArc { centerX: core.s * 0.25; centerY: core.s * 0.25; radiusX: core.s * 0.13; radiusY: core.s * 0.13; startAngle: 0; sweepAngle: 360 } } }
                Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                        rotation: core.rimPass.rotation; opacity: core.rimPass.opacity; visible: opacity > 0.01
                    ShapePath { strokeColor: Theme.alpha("#ffffff", 0.9); strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                        PathAngleArc { centerX: core.s * 0.25; centerY: core.s * 0.25; radiusX: core.s * 0.13; radiusY: core.s * 0.13; startAngle: -120; sweepAngle: 60 } } }
                Rectangle { anchors.centerIn: parent; width: core.s * 0.07; height: width; radius: width / 2; color: "#ffffff"; antialiasing: true
                            opacity: Math.min(1, (0.9 + 0.9 * core.focusBoost + 0.3 * (core.lvl - 0.22)) * core.breath) * core.bootFocus * core.nucleus }
            }
        }
    }
    // The brass iris: six blades close over the chamber when JARVIS is frozen.
    Item { anchors.fill: parent; visible: core.iris > 0.005; rotation: 60 * (1 - core.iris)
        Repeater { model: 6
            Shape { anchors.fill: parent; preferredRendererType: Shape.CurveRenderer
                readonly property real a0: index * Math.PI / 3
                ShapePath { fillColor: "#070d15"; strokeWidth: -1
                    startX: core.c; startY: core.c
                    PathLine { x: core.c + core.s * 0.15 * Math.cos(a0); y: core.c + core.s * 0.15 * Math.sin(a0) }
                    PathAngleArc { moveToStart: false; centerX: core.c; centerY: core.c; radiusX: core.s * 0.15; radiusY: core.s * 0.15; startAngle: index * 60; sweepAngle: 60.5 * core.iris }
                    PathLine { x: core.c; y: core.c } }
                ShapePath { strokeColor: Theme.alpha(core.brass, 0.75); strokeWidth: 1; fillColor: "transparent"
                    startX: core.c + core.s * 0.02 * Math.cos(a0); startY: core.c + core.s * 0.02 * Math.sin(a0)
                    PathLine { x: core.c + core.s * 0.15 * Math.cos(a0); y: core.c + core.s * 0.15 * Math.sin(a0) } }
                ShapePath { strokeColor: Theme.alpha(core.brass, 0.85); strokeWidth: 1.5; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                    PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.15; radiusY: core.s * 0.15; startAngle: index * 60; sweepAngle: 60 * core.iris } }
            } }
    }
}
