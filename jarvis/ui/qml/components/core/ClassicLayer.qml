import QtQuick
import QtQuick.Shapes
import "../../theme"
import ".."

// Classic: Reactor A, the JARVIS core as it looked before Reactor II - and
// the default skin (DFCORESKIN, Salih 2026-10-14). A machine with energy
// circulating inside it, not a glowing sphere: dark material, selective
// light, and layers that move at different speeds in different directions.
// The same layer interface as ReactorLayer.qml: it only READS `core`.
//
// Layers, back to front (D = s): L1 bed 0.56 D · L2 optical chamber 0.36 D ·
// L3 six-sector mechanical structure 0.48-0.60 D · L4 main ring 0.66 D
// (clockwise) · L5 second ring 0.74 D (counter) · L6 a 28 degree energy
// sweep · L7 three energy points · L8 fixed ticks and event light.
// Every layer is a pre-rendered raster (dev/assets/reactor_core.py) that is
// only rotated, faded or scaled. Semantic light is emitting faces INSET into
// the six sector housings, read from the generated ReactorGeometry.
Item {
    id: classic
    required property Item core

    // Housing geometry (generated with the rasters) and the one emitting face
    // every semantic signal is made of: 7 degrees of an existing housing,
    // inset by the bezel so dark material stays visible around it, with a
    // narrow edge highlight and a bloom too short to bridge a separator.
    // The face is a child of its housing, so centre, transform and rotation
    // are the housing's own. Thin housings (the rings) carry the face at
    // their full width and skip the bezel.
    ReactorGeometry { id: geo }
    component MicroFace: Shape {
        readonly property Item core: classic.core
        property real radius: 0.27
        property real band: 0.06
        property real angle: 0
        property real span: 7
        property color tint: "#4fb8ff"
        property real level: 0
        anchors.fill: parent
        opacity: level; visible: level > 0.01
        preferredRendererType: Shape.CurveRenderer
        readonly property real r: radius * core.s
        readonly property real bezelPx: Math.max(1.5, geo.bezel * core.s)
        readonly property bool thin: band * core.s < 4 * bezelPx
        readonly property real face: thin ? Math.max(1.5, band * core.s) : band * core.s - 2 * bezelPx
        // The same bezel angularly, so the face sits inside the housing's end walls too.
        readonly property real inset: thin ? 0 : bezelPx / Math.max(1, r) * 180 / Math.PI
        readonly property real a0: angle - span / 2 + inset
        readonly property real sweep: span - 2 * inset
        // Restrained local bloom: one bezel past the face, never further.
        ShapePath { strokeColor: Theme.alpha(tint, 0.22); strokeWidth: face + 2 * bezelPx; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: r; radiusY: r; startAngle: a0 - 0.5; sweepAngle: sweep + 1 } }
        // The inset emissive body.
        ShapePath { strokeColor: Theme.alpha(tint, 0.62); strokeWidth: face; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: r; radiusY: r; startAngle: a0; sweepAngle: sweep } }
        // Brighter internal energy through the middle of the body.
        ShapePath { strokeColor: thin ? "transparent" : Theme.alpha(Qt.lighter(tint, 1.22), 0.72); strokeWidth: face * 0.46; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: r; radiusY: r; startAngle: a0 + inset * 0.6; sweepAngle: sweep - inset * 1.2 } }
        // Narrow edge highlights along the face's inner and outer contours.
        ShapePath { strokeColor: thin ? "transparent" : Theme.alpha(Qt.lighter(tint, 1.35), 0.95); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: r - face / 2 + 0.5; radiusY: r - face / 2 + 0.5; startAngle: a0; sweepAngle: sweep } }
        ShapePath { strokeColor: thin ? "transparent" : Theme.alpha(Qt.lighter(tint, 1.35), 0.85); strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            PathAngleArc { centerX: core.c; centerY: core.c; radiusX: r + face / 2 - 0.5; radiusY: r + face / 2 - 0.5; startAngle: a0; sweepAngle: sweep } }
    }

    // ======================= the layer stack ==================================
    // Restrained halo: the level lifts it a little; it never becomes a fog.
    // The broad outer bloom sits ~18 % under its first tuning, so the dark
    // structure and the lit faces read before the light around them.
    Image { source: "../../assets/core-halo.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: 0.82 * (0.45 + 0.6 * (core.lvl - 0.22) + 0.3 * core.bootGlow) * core.residual }
    // L1: the dark bed. Present from the first frame: the plate's globe never shows.
    Image { source: "../../assets/core-bed.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true }
    // L8 (static part): fixed tick groups.
    Image { source: "../../assets/core-ticks.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: (0.3 + 0.5 * core.bootRim) * (0.25 + 0.75 * core.residual) }
    // L3: the six-sector mechanical structure (dark hardware, kept when
    // dormant) and its light: the state rhythms (litA / litB) and the blue
    // information faces share the same inset rasters, one per housing.
    Image { source: "../../assets/core-sectors.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: (0.4 + 0.6 * core.bootRim) * (0.55 + 0.45 * core.ringEnergy) }
    Repeater {
        model: 6
        Image {
            source: "../../assets/core-sector-lit.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: index * 60
            // Listening lights only the opposite pair (sectors 0 and 3).
            readonly property real lit: (core.listening && index !== 0 && index !== 3) ? 0 : (index % 2 === 0 ? core.litA : core.litB)
            opacity: lit * core.ringEnergy
            visible: opacity > 0.01
        }
    }
    // L5: the second ring - thinner, dimmer, counter-clockwise, its own segments.
    Image { source: "../../assets/core-ring-secondary.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: core.secondAngle; opacity: (0.3 + 0.7 * core.bootRim) * (0.72 + 0.4 * (core.lvl - 0.22)) * (core.ringFloor + (1 - core.ringFloor) * core.ringEnergy) }
    // L4: the main ring - the most legible motion, clockwise. Its green
    // success face is its own child: same centre, same radius and band,
    // angle in its frame, turning with it.
    Image { source: "../../assets/core-ring-main.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: core.mainAngle; opacity: (0.3 + 0.7 * core.bootRim) * Math.min(1, 0.86 + 0.5 * (core.lvl - 0.22)) * (core.ringFloor + (1 - core.ringFloor) * core.ringEnergy)
        // The one small green success face rides on the ring's first arc.
        MicroFace { radius: geo.mainRadius; band: geo.mainBand; tint: Theme.success
                    angle: geo.mainArcs[0][0] + 12; level: classic.core.okLevel }
    }
    // L6: the energy sweep, a 28 degree highlight travelling over the rings.
    Image { source: "../../assets/core-sweep.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: core.sweepAngle + core.jolt
            opacity: (0.5 + 1.0 * (core.lvl - 0.22)) * core.sweepGain * core.bootRim * core.circulation; visible: opacity > 0.01 }
    // L7: two energy points on the ring tracks (their carriers turn), and
    // one packet on the header's horizontal orbit.
    Item { anchors.centerIn: parent; width: core.s; height: width; rotation: core.markerA.rotation
           Image { source: "../../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
                   x: core.c + core.s * 0.33 - width / 2; y: core.c - height / 2; opacity: core.pointA * core.bootRim * core.circulation } }
    Item { anchors.centerIn: parent; width: core.s; height: width; rotation: core.markerB.rotation
           Image { source: "../../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
                   x: core.c + core.s * 0.37 - width / 2; y: core.c - height / 2; opacity: 0.55 * core.bootRim * core.circulation } }
    Image {
        source: "../../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
        visible: core.environment && opacity > 0.01
        // The orbit ellipse the header draws: centre 0.292 D left of the core,
        // rx 0.699 D, ry 0.123 D; only its left half is drawn, so the packet
        // is seen from 78 to 282 degrees and passes behind the core otherwise.
        readonly property real rad: core.orbitPhase * Math.PI / 180
        x: core.c - core.s * 0.292 + core.s * 0.699 * Math.cos(rad) - width / 2
        y: core.c + core.s * 0.123 * Math.sin(rad) - height / 2
        opacity: core.idleOrbitPeak * core.bootRim * core.circulation * Math.max(0, Math.min(1, (core.orbitPhase - 78) / 14, (282 - core.orbitPhase) / 14))
    }
    // Attention / failure: one emitting face inset into each of the six
    // sector housings (the same band, centres and span the housings were
    // drawn with). Muted amber or controlled red per the real state; the
    // dark separators between them and the blue nucleus always remain.
    Repeater {
        model: 6
        MicroFace { radius: (geo.sectorInner + geo.sectorOuter) / 2; band: geo.sectorOuter - geo.sectorInner
                    angle: geo.sectorCentres[index]; span: geo.sectorSpan; tint: classic.core.sigTint
                    level: Math.max([classic.core.sig0, classic.core.sig1, classic.core.sig2, classic.core.sig3, classic.core.sig4, classic.core.sig5][index], classic.core.hold) * classic.core.ringEnergy }
    }
    // Blue information: the same rich emitting faces, in the structural blue.
    Repeater {
        model: 6
        MicroFace { radius: (geo.sectorInner + geo.sectorOuter) / 2; band: geo.sectorOuter - geo.sectorInner
                    angle: geo.sectorCentres[index]; span: geo.sectorSpan; tint: "#4fb8ff"
                    level: [classic.core.blue0, classic.core.blue1, classic.core.blue2, classic.core.blue3, classic.core.blue4, classic.core.blue5][index] * classic.core.ringEnergy }
    }
    // Event light: the wave (scaled outward), the retune ring, the packet
    // (the logic moves its CENTRE).
    Image { source: "../../assets/core-wave.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            scale: core.wave.scale; opacity: core.wave.opacity; visible: opacity > 0.01 }
    Image { source: "../../assets/core-wave.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            scale: core.ringWave.scale; opacity: core.ringWave.opacity; visible: opacity > 0.01 }
    Image { source: "../../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
            x: core.packet.x - width / 2; y: core.packet.y - height / 2; opacity: core.packet.opacity; visible: opacity > 0.01 }

    // L2: the optical chamber and its nucleus, breathing in light, barely in size.
    Item {
        anchors.centerIn: parent; width: core.s * 0.5; height: width
        scale: (1 + 0.16 * (core.breath - 1)) * core.squeeze
        Image { source: "../../assets/core-chamber.png"; anchors.fill: parent; mipmap: true; opacity: 0.45 + 0.55 * core.nucleus }
        // The chamber light turns 360 / 53 degrees a second on the core's clock.
        Image { source: "../../assets/core-chamber-light.png"; anchors.fill: parent; mipmap: true; rotation: (40 + 360 / 53 * core.clockT) % 360
                opacity: 0.85 * core.bootFocus * core.nucleus }
        Image { source: "../../assets/core-rim-hi.png"; anchors.fill: parent; mipmap: true
                opacity: (core.rimPulse * 0.8 + 0.2 * core.bootGlow) * core.bootRim * core.nucleus }
        Image { source: "../../assets/core-rim-hi.png"; anchors.fill: parent; mipmap: true
                rotation: core.rimPass.rotation; opacity: core.rimPass.opacity; visible: opacity > 0.01 }
        Image { source: "../../assets/core-focus-glow.png"; anchors.centerIn: parent; width: core.s * 0.4; height: width; mipmap: true
                opacity: Math.min(1, (0.34 + 1.1 * (core.lvl - 0.22) + core.focusBoost + 0.35 * core.bootGlow) * core.breath) * core.bootFocus * core.nucleus }
        Image { source: "../../assets/core-focus.png"; anchors.centerIn: parent; width: core.s * 0.2; height: width; mipmap: true
                opacity: Math.min(1, (0.9 + 0.9 * core.focusBoost + 0.3 * (core.lvl - 0.22)) * core.breath) * core.bootFocus * core.nucleus }
    }
}
