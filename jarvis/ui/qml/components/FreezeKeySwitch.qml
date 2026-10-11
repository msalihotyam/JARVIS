import QtQuick
import QtQuick.Shapes
import "../theme"

// Freeze key skins 3 (chrome toggle) and 4 (knife switch in a torn surface),
// Gate 6G S2. Three layers cut by dev/assets/freeze_keys.py: the plate, and
// the lever up (running) and down (frozen). The lever turns over toward the
// viewer about its hinge line, drawn as a vertical scale through zero.
// `position` (0 up, 1 down, between = on its way) comes from the established
// execution state only, never from the click. The toggle snaps (eased travel
// and a tiny settle) under a moving highlight, a contact shadow and a state
// glow; the knife swings slower and weighted, and arcs where its blades leave
// or seat in the jaws (DFSWITCH, Salih 2026-10-12). Every effect is decoration
// driven by the same `position` change; ambient off = no motion, no arcs.
Item {
    id: sw
    property string kind: "toggle"
    property real position: 0
    // The drawn lever; it follows `position`.
    property real shown: 0
    readonly property bool knife: kind === "knife"
    readonly property real pivot: knife ? 0.5418 : 0.4841      // printed by freeze_keys.py
    // +1 the lever stands up, 0 it points at the viewer, -1 it hangs down.
    readonly property real lift: 1 - 2 * shown
    readonly property real tilt: 1 - Math.min(1, Math.abs(lift))   // 0 at rest, 1 facing the viewer
    objectName: "freezeKeySwitch"
    implicitWidth: height * (knife ? 345 / 455 : 220 / 378)

    // The knife's arc burst (0 -> 1); `arcing` only while it runs.
    property real burst: 0
    readonly property bool arcing: arcBurst.running
    property var sparks: []
    // The blade tips sit in the jaws at this height; the jaws' centres (fractions of the art).
    readonly property real jawY: 0.31
    readonly property var jaws: [0.334, 0.515, 0.705]

    Component.onCompleted: shown = position
    onPositionChanged: {
        swing.stop()
        arcLater.stop()
        if (!Theme.ambient) { shown = position; return }
        // The blades leave the jaws at once; they seat near the end of the swing.
        if (knife && shown < 0.02 && position > 0)
            strike()
        else if (knife && position === 0 && shown > 0.02)
            arcLater.start()
        // A lever that lands at an end goes a hair past it and settles back.
        const end = position === 0 || position === 1
        flip.to = end ? position + (position - shown) * (knife ? 0.03 : 0.05) : position
        settle.to = position
        swing.start()
    }
    SequentialAnimation {
        id: swing
        NumberAnimation { id: flip; target: sw; property: "shown"; duration: Theme.dur(sw.knife ? 420 : 110); easing.type: Easing.InOutCubic }
        NumberAnimation { id: settle; target: sw; property: "shown"; duration: Theme.dur(sw.knife ? 140 : 70); easing.type: Easing.OutCubic }
    }
    SequentialAnimation {
        id: arcLater
        PauseAnimation { duration: Theme.dur(320) }
        ScriptAction { script: sw.strike() }
    }
    NumberAnimation { id: arcBurst; target: sw; property: "burst"; from: 0; to: 1; duration: Theme.dur(280) }

    function strike() {
        if (!Theme.ambient)
            return
        const made = []
        for (let i = 0; i < 6; ++i)
            // Slow enough to fade out before the box edge (nothing is cut off mid-flight).
            made.push({ x: jaws[i % 3] + (Math.random() - 0.5) * 0.06, vx: (Math.random() - 0.5) * 1.4,
                        vy: -0.2 - Math.random() * 0.7 })
        sparks = made
        arcBurst.restart()
    }

    // A soft round light (or shadow): `tint` in the middle, nothing at the rim.
    component Glow: Shape {
        id: glow
        property color tint: "black"
        property real core: 0           // where the tint starts (0 a spot, near 1 a ring)
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: "transparent"
            fillGradient: RadialGradient {
                centerX: glow.width / 2; centerY: glow.height / 2; centerRadius: glow.width / 2
                focalX: centerX; focalY: centerY
                GradientStop { position: 0; color: glow.core > 0 ? "transparent" : glow.tint }
                GradientStop { position: Math.max(0.01, glow.core); color: glow.core > 0 ? "transparent" : glow.tint }
                GradientStop { position: (1 + glow.core) / 2; color: glow.tint }
                GradientStop { position: 1; color: "transparent" }
            }
            PathAngleArc { centerX: glow.width / 2; centerY: glow.height / 2; radiusX: glow.width / 2; radiusY: glow.height / 2; sweepAngle: 360 }
        }
    }

    Image { anchors.fill: parent; source: "../assets/freeze-" + sw.kind + "-plate.png"; mipmap: true; smooth: true }
    // The toggle's state light around the bushing: cool while running, red when
    // frozen, cross-fading with the lever.
    Glow {
        id: runLight
        visible: !sw.knife
        x: sw.width * 0.47 - width / 2; y: sw.height * 0.485 - height / 2; width: sw.width * 0.66; height: width
        tint: Theme.alpha(Theme.accent, 0.24); core: 0.7
        opacity: 1 - Math.min(1, Math.max(0, sw.shown))
    }
    Glow {
        visible: !sw.knife
        x: runLight.x; y: runLight.y; width: runLight.width; height: width
        tint: Qt.rgba(1, 0.25, 0.35, 0.32); core: 0.7
        opacity: 1 - runLight.opacity
    }
    // The toggle's contact shadow: it follows the ball, and softens and widens
    // while the lever faces the viewer, away from the plate.
    Glow {
        visible: !sw.knife
        readonly property real ballY: sw.pivot - (sw.lift > 0 ? 0.135 : 0.16) * sw.lift
        width: sw.width * 0.4 * (1 + 0.4 * sw.tilt); height: width
        x: sw.width * 0.49 - width / 2; y: sw.height * (ballY + 0.03) - height / 2
        transform: Scale { origin.y: sw.width * 0.2 * (1 + 0.4 * sw.tilt); yScale: 0.55 }
        tint: Qt.rgba(0, 0, 0, 0.5 - 0.2 * sw.tilt)
    }
    // A lever layer, swung about the hinge; the toggle's ball carries a
    // highlight that slides and brightens as it turns toward the light.
    component Lever: Item {
        id: lever
        property string art
        property real yScale
        property real ballX
        property real ballY
        anchors.fill: parent
        visible: yScale > 0.01
        transform: Scale { origin.y: sw.height * sw.pivot; yScale: Math.max(0, lever.yScale) }
        Image { anchors.fill: parent; source: "../assets/freeze-" + sw.kind + "-" + lever.art + ".png"; mipmap: true; smooth: true; antialiasing: true }
        Glow {
            visible: !sw.knife
            width: sw.width * 0.16; height: width
            x: sw.width * (lever.ballX + 0.05 * sw.tilt) - width / 2; y: sw.height * (lever.ballY + 0.025 * sw.tilt) - height / 2
            tint: Qt.rgba(1, 1, 1, 0.25 + 0.45 * sw.tilt)
        }
    }
    Lever { objectName: "freezeLeverUp"; art: "up"; yScale: sw.lift; ballX: 0.41; ballY: 0.315 }
    Lever { objectName: "freezeLeverDown"; art: "down"; yScale: -sw.lift; ballX: 0.42; ballY: 0.6 }

    // The knife's electricity: jagged blue-white arcs between the blade tips and
    // the jaws, redrawn about every 33 ms while the burst runs, a few sparks
    // flying out, a flash on the contacts. Nothing is drawn at rest.
    Canvas {
        id: arcs
        objectName: "freezeKnifeArcs"
        anchors.fill: parent
        visible: sw.arcing
        readonly property int frame: Math.floor(sw.burst * 280 / 33)
        onFrameChanged: requestPaint()
        onVisibleChanged: requestPaint()
        function rgba(r, g, b, a) { return "rgba(" + r + "," + g + "," + b + "," + Math.max(0, a).toFixed(3) + ")" }
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            if (!sw.arcing)
                return
            const w = width, h = height, t = sw.burst, k = h / 112
            const jawY = sw.jawY * h
            const tipY = (sw.pivot - (sw.pivot - sw.jawY) * Math.max(0, Math.min(1, sw.lift))) * h
            const flash = (1 - t) * (1 - t)
            for (const jx of sw.jaws) {
                const g = ctx.createRadialGradient(jx * w, jawY, 0, jx * w, jawY, 0.1 * w)
                g.addColorStop(0, rgba(217, 240, 255, 0.9 * flash))
                g.addColorStop(0.4, rgba(89, 166, 255, 0.4 * flash))
                g.addColorStop(1, rgba(51, 128, 255, 0))
                ctx.fillStyle = g
                ctx.fillRect(jx * w - 0.1 * w, jawY - 0.1 * w, 0.2 * w, 0.2 * w)
            }
            // An arc holds until the blade is too far from the jaw, then breaks.
            if (t < 0.7 && tipY - jawY < 0.14 * h) {
                const count = 2 + Math.floor(Math.random() * 3)
                for (let n = 0; n < count; ++n) {
                    const jx = sw.jaws[Math.floor(Math.random() * 3)] * w
                    const x0 = jx + (Math.random() - 0.5) * 0.08 * w, y0 = jawY
                    const x1 = jx + (Math.random() - 0.5) * 0.04 * w, y1 = Math.max(tipY, jawY) + (0.02 + Math.random() * 0.06) * h
                    const pts = [[x0, y0]]
                    const steps = 6
                    for (let i = 1; i < steps; ++i)
                        pts.push([x0 + (x1 - x0) * i / steps + (Math.random() - 0.5) * 0.07 * w, y0 + (y1 - y0) * i / steps])
                    pts.push([x1, y1])
                    const alpha = 0.6 + Math.random() * 0.4
                    for (const [lw, color] of [[3.2 * k, rgba(102, 178, 255, 0.35 * alpha)], [1.1 * k, rgba(235, 247, 255, alpha)]]) {
                        ctx.beginPath()
                        ctx.moveTo(pts[0][0], pts[0][1])
                        for (let i = 1; i < pts.length; ++i)
                            ctx.lineTo(pts[i][0], pts[i][1])
                        ctx.lineWidth = lw
                        ctx.strokeStyle = color
                        ctx.lineJoin = "round"
                        ctx.lineCap = "round"
                        ctx.stroke()
                    }
                }
            }
            // Sparks: thrown from the contacts, falling, fading out.
            const s = t * 0.28
            ctx.lineCap = "round"
            for (const p of sw.sparks) {
                const x = p.x * w + p.vx * h * s, y = jawY + (p.vy * s + 1.2 * s * s) * h
                ctx.beginPath()
                ctx.moveTo(x, y)
                ctx.lineTo(x - p.vx * h * 0.03, y - (p.vy + 2.4 * s) * h * 0.03)
                ctx.lineWidth = 1.4 * k
                ctx.strokeStyle = rgba(255, 244, 214, 1 - t * t)
                ctx.stroke()
            }
        }
    }
}
