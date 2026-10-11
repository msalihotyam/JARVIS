import QtQuick
import QtQuick.Shapes
import "../theme"

// V2.1 (Salih 2026-10-11): the window powers down to the core on OUR minimize
// and powers up from it on every return (taskbar, tray Open, Alt+Tab). One
// 220 ms timeline drives every layer, each on its own OutCubic segment: the
// content fades (a veil in the window colour, so nothing moves - travel 0), a
// ring in the core's accent and glow contracts to / expands from the core, a
// scan line closes in / sweeps down, the core flashes at the end of a power
// down. A native minimize (Win+D, the taskbar button) cannot be pre-animated:
// only its return plays. Ambient effects off: no frame at all - minimize at
// once, content at once. Never interactive; a safety Timer ends every run
// even when the timeline stalls (the window not drawn), so the minimize
// always happens and the content always ends at full opacity.
Item {
    id: fx
    enabled: false
    visible: phase !== ""

    property Window host
    property real coreX: width / 2
    property real coreY: height / 2
    property real coreSize: 200
    signal minimizeNow()

    // "" at rest, "down" (power down; held while minimized), "up" (power up).
    property string phase: ""
    property real t: 0                      // the timeline, 0..1 over 220 ms
    property bool pending: false            // our minimize is on its way
    property bool away: false               // minimized or hidden
    property int plays: 0                   // observable from the outside (tests): runs started
    readonly property int span: 220
    readonly property real ms: t * span

    function ease(x) { var c = Math.max(0, Math.min(1, x)); return 1 - Math.pow(1 - c, 3) }   // OutCubic
    readonly property real sweep: ease(t)
    readonly property real contentOpacity: phase === "down" ? 1 - ease(ms / 180)
                                         : phase === "up" ? ease((ms - 40) / 180) : 1
    readonly property real reach: Math.max(coreX, width - coreX, coreY, height - coreY)
    readonly property real ringR: phase === "down" ? reach * (1 - sweep) : reach * sweep

    function powerDown() {
        if (pending) return                                     // a second click adds nothing
        if (!Theme.ambient) { minimizeNow(); return }
        pending = true
        begin("down")
    }
    function powerUp() {
        if (!Theme.ambient) { stopRun(); phase = ""; return }
        begin("up")
    }
    function begin(next) {
        stopRun()
        phase = next; t = 0; plays += 1
        run.start(); safety.start()
    }
    function stopRun() { run.stop(); safety.stop() }
    function settle() {
        stopRun()
        if (phase !== "down") { phase = ""; return }
        if (!pending) return
        pending = false
        minimizeNow()
        if (!away) phase = ""                                   // the window stayed up: never a dim window
    }

    Connections {
        target: fx.host
        function onVisibilityChanged() {
            var v = fx.host.visibility
            if (v === Window.Minimized || v === Window.Hidden) {
                fx.away = true
                // Gone natively (or hidden) mid-run: never minimize again later.
                if (fx.pending) { fx.pending = false; fx.stopRun() }
            } else if (fx.away) {
                fx.away = false
                fx.powerUp()
            }
        }
    }
    NumberAnimation { id: run; objectName: "windowPowerRun"; target: fx; property: "t"; from: 0; to: 1
                      duration: Theme.dur(220); onFinished: fx.settle() }
    Timer { id: safety; objectName: "windowPowerSafety"; interval: 250; onTriggered: fx.settle() }

    // The content: the window colour over everything, so the window reads as the content fading.
    Rectangle { anchors.fill: parent; color: Theme.bg0; opacity: 1 - fx.contentOpacity }

    // The ring, centred on the core: a glow band and a fine accent edge.
    Rectangle {
        readonly property real r: fx.ringR + 5
        x: fx.coreX - r; y: fx.coreY - r; width: 2 * r; height: width; radius: r
        color: "transparent"; border.width: 10; border.color: Theme.alpha(Theme.glow, 0.35 * Theme.glowStrength)
        opacity: fx.phase === "up" ? 1 - fx.sweep : 1
    }
    Rectangle {
        readonly property real r: fx.ringR
        x: fx.coreX - r; y: fx.coreY - r; width: 2 * r; height: width; radius: r
        color: "transparent"; border.width: 2; border.color: Theme.accent
        opacity: fx.phase === "up" ? 1 - fx.sweep : 1
    }

    // The scan line: closes from the bottom edge onto the core, or sweeps top to bottom.
    Item {
        readonly property real w: fx.phase === "down" ? fx.width * (1 - fx.sweep) : fx.width
        x: fx.phase === "down" ? fx.coreX - w / 2 : 0
        y: fx.phase === "down" ? fx.height + (fx.coreY - fx.height) * fx.sweep : fx.height * fx.sweep
        width: w
        opacity: fx.phase === "up" ? 1 - fx.sweep * fx.sweep : 1
        Rectangle { x: 0; y: -6; width: parent.width; height: 12; radius: 6; color: Theme.alpha(Theme.glow, 0.3 * Theme.glowStrength) }
        Rectangle { x: 0; y: -1; width: parent.width; height: 2; color: Theme.accent }
    }

    // The core flash: lights over the last 120 ms of a power down, fades as a power up starts.
    Shape {
        id: flash
        readonly property real r: fx.coreSize * 0.45
        x: fx.coreX - r; y: fx.coreY - r; width: 2 * r; height: width
        preferredRendererType: Shape.CurveRenderer
        opacity: fx.phase === "down" ? fx.ease((fx.ms - 100) / 120) : 1 - fx.ease(fx.ms / 120)
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: flash.r; centerY: flash.r; centerRadius: flash.r; focalX: centerX; focalY: centerY
                GradientStop { position: 0.00; color: Theme.text }
                GradientStop { position: 0.12; color: Theme.accent }
                GradientStop { position: 0.40; color: Theme.alpha(Theme.glow, 0.45) }
                GradientStop { position: 1.00; color: "transparent" }
            }
            PathAngleArc { centerX: flash.r; centerY: flash.r; radiusX: flash.r; radiusY: flash.r; startAngle: 0; sweepAngle: 360 }
        }
    }
}
