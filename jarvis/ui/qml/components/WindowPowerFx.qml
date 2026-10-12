import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
import "../theme"

// V2.1 (Salih 2026-10-11), v2 (DFWIN, Salih 2026-10-12: "richer and slower"): the
// window powers down INTO the core on OUR minimize and powers up FROM it on every
// return (taskbar, tray Open, Alt+Tab).
// Power down, 520 ms: a rim light runs once round the window edge while the content
// brightens a touch; a frozen snapshot of the window (never the live UI, which is
// never re-laid out) collapses like a CRT onto the core's centre line into a bright
// band with scanlines and a chromatic edge split, the room going dark; the band
// shrinks into the core, which flares (ring, ion sparks) and settles to an ember;
// then the minimize. Power up, 600 ms: the core ignites, a line runs out to both
// edges and unfolds over the live content (its edges are the scan sweep), the
// shimmer settling to clean. At rest nothing of it is left: the visuals are a Loader
// that is only active during a run; the snapshot is released in every run, once the
// band has gone into the core.
// A native minimize (Win+D, the taskbar button) cannot be pre-animated: only its
// return plays. Ambient effects off: no frame at all - minimize at once, content at
// once. Never interactive; a safety Timer (the effect + 60 ms) ends every run even
// when the timeline stalls (the window not drawn), so the minimize always happens
// and the content always ends at full opacity.
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
    property real t: 0                      // the timeline, 0..1 over the span
    property bool pending: false            // our minimize is on its way
    property bool away: false               // minimized or hidden
    property int plays: 0                   // observable from the outside (tests): runs started
    property bool snapshotHeld: false       // a power down's frozen window, released as the band vanishes
    property int swaps: -1                  // frames drawn since the snapshot was asked for; -1 not waiting
    property bool grabbed: false            // the snapshot is drawn: a power down's visuals may start
    property real grabT: 0                  // the timeline when it was: the visuals play from there, whole
    // The snapshot needs a GPU scene graph: the software renderer rebuilds the whole window's
    // render list when it is taken and released (~100 ms each), so there the band collapses
    // over the darkening live content instead.
    property bool snapshots: GraphicsInfo.api !== GraphicsInfo.Software
    readonly property int span: phase === "up" ? 600 : 520
    // The visuals' clock: a power down's holds at 0 until the snapshot is grabbed, so none of
    // the effect is ever in it (the timeline itself - and so the minimize - never waits); then
    // the whole sequence plays over the rest of the timeline, ending with it.
    readonly property real ms: !down ? t * span : !grabbed ? 0 : Math.max(0, t - grabT) / Math.max(1e-6, 1 - grabT) * span

    function clamp01(x) { return Math.max(0, Math.min(1, x)) }
    function seg(a, b) { return clamp01((ms - a) / (b - a)) }       // 0..1 over [a, b] ms of the run
    function outCubic(x) { return 1 - Math.pow(1 - x, 3) }
    function inOutCubic(x) { return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(2 - 2 * x, 3) / 2 }
    function inOutQuint(x) { return x < 0.5 ? 16 * Math.pow(x, 5) : 1 - Math.pow(2 - 2 * x, 5) / 2 }

    readonly property bool down: phase === "down"
    readonly property bool up: phase === "up"
    // Power down: the vertical collapse onto the core's line, then the band into the core.
    readonly property real squash: down ? inOutCubic(seg(80, 360)) : 0
    readonly property real shrink: down ? inOutQuint(seg(300, 470)) : 0
    // Power up: the line out to both edges, then the unfold over the live content.
    readonly property real extend: up ? inOutCubic(seg(90, 260)) : 0
    readonly property real unfold: up ? inOutCubic(seg(240, 500)) : 0
    // How much of the window shows (1 at rest): the room goes dark under the collapse.
    readonly property real contentOpacity: down ? 1 - outCubic(seg(80, 240)) : up ? unfold : 1

    // The collapsing frame (power down) and the opening slit (power up), in window pixels.
    readonly property real frameH: Math.max(2, height * (1 - squash))
    readonly property real frameY: coreY - frameH * coreY / Math.max(1, height)
    readonly property real frameW: width * (1 - shrink)
    readonly property real frameX: coreX - frameW * coreX / Math.max(1, width)
    readonly property real lineW: width * extend
    readonly property real lineX: coreX - lineW * coreX / Math.max(1, width)
    readonly property real slitTop: coreY * (1 - unfold)
    readonly property real slitBottom: coreY + (height - coreY) * unfold

    // The core: a flare that settles to an ember (held while minimized) and re-ignites.
    readonly property real flare: down ? outCubic(seg(330, 450)) * (1 - 0.75 * seg(470, 520))
                                : up ? (0.25 + 0.75 * outCubic(seg(0, 140))) * (1 - outCubic(seg(240, 500))) : 0
    readonly property real ringP: down ? outCubic(seg(390, 520)) : up ? outCubic(seg(30, 330)) : 0
    readonly property real rim: down ? 0.7 * seg(0, 110) * (1 - seg(160, 330))
                              : up ? 0.6 * seg(200, 300) * (1 - seg(300, 600)) : 0
    readonly property real glowK: Math.max(0.5, Theme.glowStrength)
    readonly property color night: Qt.darker(Theme.bg0, 1.6)
    readonly property color red: "#b0ff3b5c"
    // Faint scanlines every 3 px in window coordinates (they never travel with the snapshot); only during a run.
    readonly property var scanLines: {
        var out = []
        for (var y = 1; phase !== "" && y < height; y += 3) out.push([Qt.point(0, y), Qt.point(width, y)])
        return out
    }

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
        phase = next; t = 0; plays += 1; grabT = 0; grabbed = !snapshots
        if (next === "down" && snapshots) {
            // The snapshot is drawn with the next frame (two swaps: one may already be in flight).
            snapshotHeld = true; swaps = 0
            if (host) host.update()
        }
        run.start(); safety.start()
    }
    function stopRun() { run.stop(); safety.stop(); swaps = -1; snapshotHeld = false }
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
        function onFrameSwapped() {
            if (fx.swaps < 0) return
            fx.swaps += 1
            if (fx.swaps < 2) { fx.host.update(); return }      // nothing of ours changes until then
            fx.swaps = -1
            fx.grabT = fx.t
            fx.grabbed = true
        }
    }
    NumberAnimation { id: run; objectName: "windowPowerRun"; target: fx; property: "t"; from: 0; to: 1
                      duration: Theme.dur(fx.phase === "up" ? 600 : 520); onFinished: fx.settle() }
    Timer { id: safety; objectName: "windowPowerSafety"; interval: fx.span + 60; onTriggered: fx.settle() }

    Loader {
        id: stage
        objectName: "windowPowerStage"
        anchors.fill: parent
        active: fx.phase !== ""
        sourceComponent: Item {
            // The dark room: under the collapse, and the closed parts of the window while it unfolds.
            Rectangle { anchors.fill: parent; visible: fx.down; color: fx.night; opacity: 1 - fx.contentOpacity }
            Rectangle { visible: fx.up; width: parent.width; height: fx.slitTop; color: fx.night }
            Rectangle { visible: fx.up; y: fx.slitBottom; width: parent.width; height: parent.height - y; color: fx.night }

            // Power down: the frozen window, squashed into the frame (a texture, never the live items).
            Item {
                visible: fx.down
                x: fx.frameX; y: fx.frameY; width: fx.frameW; height: fx.frameH
                Item {
                    // Zero height until the collapse: the snapshot is grabbed, but never drawn into itself.
                    // A new one per run (its layer starts blank), destroyed once the band has vanished.
                    clip: true; width: parent.width; height: fx.ms >= 80 ? parent.height : 0
                    Loader {
                        active: fx.snapshotHeld && fx.ms < 480
                        sourceComponent: ShaderEffectSource {
                            objectName: "windowPowerSnapshot"
                            width: fx.frameW; height: fx.frameH
                            sourceItem: fx.parent
                            live: false; recursive: true; smooth: true
                        }
                    }
                }
                Rectangle { anchors.fill: parent; color: Theme.text; opacity: 0.08 * Math.sin(Math.PI * fx.seg(0, 170)) }
                Item {
                    anchors.fill: parent; clip: true
                    Shape {
                        x: -fx.frameX; y: -fx.frameY; width: fx.width; height: fx.height
                        opacity: 0.35 * fx.seg(80, 180)
                        ShapePath { strokeWidth: 1; strokeColor: Qt.rgba(0, 0, 0, 0.55); fillColor: "transparent"
                                    PathMultiline { paths: fx.down ? fx.scanLines : [] } }
                    }
                }
            }
            // Power up: the revealed live content shimmers, then settles to clean.
            Item {
                visible: fx.up
                y: fx.slitTop; width: parent.width; height: fx.slitBottom - fx.slitTop; clip: true
                Rectangle { anchors.fill: parent; color: Theme.text; opacity: 0.14 * (1 - fx.outCubic(fx.seg(260, 600))) }
                Shape {
                    y: -fx.slitTop; width: fx.width; height: fx.height
                    opacity: 0.35 * (1 - fx.seg(380, 590))
                    ShapePath { strokeWidth: 1; strokeColor: Qt.rgba(0, 0, 0, 0.55); fillColor: "transparent"
                                PathMultiline { paths: fx.up ? fx.scanLines : [] } }
                }
            }

            // The chromatic edge split, 1-2 px: red out of one edge, cyan out of the other.
            Item {
                anchors.fill: parent
                opacity: fx.down ? fx.seg(80, 160) * (1 - fx.seg(400, 470)) : fx.seg(240, 280) * (1 - fx.seg(420, 560))
                Rectangle { visible: fx.down; x: fx.frameX; y: fx.frameY - 2; width: fx.frameW; height: 1; color: fx.red }
                Rectangle { visible: fx.down; x: fx.frameX; y: fx.frameY + fx.frameH + 1; width: fx.frameW; height: 1; color: Theme.accent }
                Rectangle { visible: fx.down; x: fx.frameX - 2; y: fx.frameY; width: 1; height: fx.frameH; color: fx.red }
                Rectangle { visible: fx.down; x: fx.frameX + fx.frameW + 1; y: fx.frameY; width: 1; height: fx.frameH; color: Theme.accent }
                Rectangle { visible: fx.up; y: fx.slitTop - 2; width: fx.width; height: 1; color: fx.red }
                Rectangle { visible: fx.up; y: fx.slitBottom + 1; width: fx.width; height: 1; color: Theme.accent }
            }

            // The light: everything that glows, drawn crisp and blooming (GPU only: the software
            // backend cannot draw the bloom, so it never pays for its full-window layer).
            MultiEffect {
                objectName: "windowPowerBloom"
                source: light; anchors.fill: light
                visible: fx.ms > 0 && !fx.away && GraphicsInfo.api !== GraphicsInfo.Software
                blurEnabled: true; blur: 1.0; blurMax: 48; brightness: 0.15
                opacity: fx.glowK
            }
            Item {
                id: light
                anchors.fill: parent

                // The rim: the window edge lit, and (power down) a comet each way round from the core.
                Rectangle {
                    anchors.fill: parent; color: "transparent"
                    border.width: 2; border.color: Theme.accent; opacity: fx.rim
                }
                Shape {
                    id: rimRun
                    anchors.fill: parent
                    visible: fx.down && fx.ms > 0 && fx.ms < 200
                    // Each comet runs half the edge, from the core round to the point opposite it.
                    readonly property real half: fx.width + fx.height
                    readonly property real head: fx.inOutCubic(fx.seg(0, 180)) * (half + 700)
                    readonly property real i: 3                         // inset: the stroke stays inside the window
                    readonly property var cw: [Qt.point(fx.coreX, i), Qt.point(fx.width - i, i), Qt.point(fx.width - i, fx.height - i),
                                               Qt.point(fx.width - fx.coreX, fx.height - i)]
                    readonly property var ccw: [Qt.point(fx.coreX, i), Qt.point(i, i), Qt.point(i, fx.height - i),
                                                Qt.point(fx.width - fx.coreX, fx.height - i)]
                    // A dash of `len` px whose front is `head` px along the path (pattern units are the stroke width).
                    function offset(len, sw) { return (len + 4 * half + len - head) / sw }
                    // Per direction: a soft wide glow, the accent trail, the white-hot head.
                    ShapePath { strokeWidth: 14; strokeColor: Theme.alpha(Theme.glow, 0.3); fillColor: "transparent"; strokeStyle: ShapePath.DashLine
                                dashPattern: [700 / 14, 4 * rimRun.half / 14]; dashOffset: rimRun.offset(700, 14); capStyle: ShapePath.RoundCap
                                PathPolyline { path: rimRun.cw } }
                    ShapePath { strokeWidth: 14; strokeColor: Theme.alpha(Theme.glow, 0.3); fillColor: "transparent"; strokeStyle: ShapePath.DashLine
                                dashPattern: [700 / 14, 4 * rimRun.half / 14]; dashOffset: rimRun.offset(700, 14); capStyle: ShapePath.RoundCap
                                PathPolyline { path: rimRun.ccw } }
                    ShapePath { strokeWidth: 3; strokeColor: Theme.accent; fillColor: "transparent"; strokeStyle: ShapePath.DashLine
                                dashPattern: [700 / 3, 4 * rimRun.half / 3]; dashOffset: rimRun.offset(700, 3); capStyle: ShapePath.RoundCap
                                PathPolyline { path: rimRun.cw } }
                    ShapePath { strokeWidth: 3; strokeColor: Theme.accent; fillColor: "transparent"; strokeStyle: ShapePath.DashLine
                                dashPattern: [700 / 3, 4 * rimRun.half / 3]; dashOffset: rimRun.offset(700, 3); capStyle: ShapePath.RoundCap
                                PathPolyline { path: rimRun.ccw } }
                    ShapePath { strokeWidth: 5; strokeColor: "#ffffff"; fillColor: "transparent"; strokeStyle: ShapePath.DashLine
                                dashPattern: [160 / 5, 4 * rimRun.half / 5]; dashOffset: rimRun.offset(160, 5); capStyle: ShapePath.RoundCap
                                PathPolyline { path: rimRun.cw } }
                    ShapePath { strokeWidth: 5; strokeColor: "#ffffff"; fillColor: "transparent"; strokeStyle: ShapePath.DashLine
                                dashPattern: [160 / 5, 4 * rimRun.half / 5]; dashOffset: rimRun.offset(160, 5); capStyle: ShapePath.RoundCap
                                PathPolyline { path: rimRun.ccw } }
                }

                // Power down: the snapshot burns to a bright band, its glow round it.
                Rectangle {
                    visible: fx.down
                    x: fx.frameX; y: fx.frameY - 14; width: fx.frameW; height: fx.frameH + 28
                    opacity: Math.pow(fx.squash, 4) * (1 - fx.seg(450, 490))     // a halo round the band, never a wash
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: "transparent" }
                        GradientStop { position: 0.5; color: Theme.alpha(Theme.glow, 0.8) }
                        GradientStop { position: 1.0; color: "transparent" }
                    }
                }
                Rectangle {
                    visible: fx.down
                    x: fx.frameX; y: fx.frameY; width: fx.frameW; height: fx.frameH
                    color: Qt.lighter(Theme.accent, 1.55)
                    opacity: 0.92 * Math.pow(fx.squash, 3)               // white-hot only once it is thin
                }
                // Power up: the line out to both edges (its glow round it), then the unfolding edges:
                // each a bright line with a soft wake behind it, the lower one the scan sweep.
                Item {
                    visible: fx.up && fx.lineW > 0
                    x: fx.lineX; width: fx.lineW
                    opacity: 1 - fx.seg(240, 300)
                    Rectangle {
                        y: fx.coreY - 16; width: parent.width; height: 32
                        gradient: Gradient {
                            GradientStop { position: 0.0; color: "transparent" }
                            GradientStop { position: 0.5; color: Theme.alpha(Theme.glow, 0.7) }
                            GradientStop { position: 1.0; color: "transparent" }
                        }
                    }
                    Rectangle { y: fx.coreY - 1.5; width: parent.width; height: 3; color: Qt.lighter(Theme.accent, 1.6) }
                }
                Item {
                    visible: fx.up && fx.unfold > 0
                    anchors.fill: parent
                    opacity: 1 - fx.seg(440, 580)
                    Rectangle {
                        y: fx.slitTop; width: parent.width; height: Math.min(28, fx.coreY - fx.slitTop + 1)
                        gradient: Gradient {
                            GradientStop { position: 0.0; color: Theme.alpha(Theme.glow, 0.45) }
                            GradientStop { position: 1.0; color: "transparent" }
                        }
                    }
                    Rectangle {
                        readonly property real wake: Math.min(90, fx.slitBottom - fx.coreY + 1)
                        y: fx.slitBottom - wake; width: parent.width; height: wake
                        gradient: Gradient {
                            GradientStop { position: 0.0; color: "transparent" }
                            GradientStop { position: 1.0; color: Theme.alpha(Theme.glow, 0.5) }
                        }
                    }
                    Rectangle { y: fx.slitTop - 1; width: parent.width; height: 2; color: Qt.lighter(Theme.accent, 1.6) }
                    Rectangle { y: fx.slitBottom - 1.5; width: parent.width; height: 3; color: Qt.lighter(Theme.accent, 1.6) }
                }

                // The core: flare, ring, ion sparks.
                Shape {
                    id: flare
                    readonly property real r: fx.coreSize * 0.6
                    x: fx.coreX - r; y: fx.coreY - r; width: 2 * r; height: width
                    opacity: fx.flare
                    ShapePath {
                        strokeWidth: -1
                        fillGradient: RadialGradient {
                            centerX: flare.r; centerY: flare.r; centerRadius: flare.r; focalX: centerX; focalY: centerY
                            GradientStop { position: 0.00; color: "#ffffff" }
                            GradientStop { position: 0.08; color: Theme.text }
                            GradientStop { position: 0.20; color: Theme.accent }
                            GradientStop { position: 0.45; color: Theme.alpha(Theme.glow, 0.45) }
                            GradientStop { position: 1.00; color: "transparent" }
                        }
                        PathAngleArc { centerX: flare.r; centerY: flare.r; radiusX: flare.r; radiusY: flare.r; startAngle: 0; sweepAngle: 360 }
                    }
                }
                Repeater {
                    model: [10, 3]                                      // the ring: a glow band, a crisp edge
                    Rectangle {
                        required property int modelData
                        readonly property real r: fx.coreSize * (0.3 + (fx.up ? 2.2 : 1.2) * fx.ringP)
                        visible: fx.ringP > 0
                        x: fx.coreX - r; y: fx.coreY - r; width: 2 * r; height: width; radius: r
                        color: "transparent"; border.width: modelData
                        border.color: modelData > 3 ? Theme.alpha(Theme.glow, 0.35) : Qt.lighter(Theme.accent, 1.3)
                        opacity: 1 - fx.ringP
                    }
                }
                Repeater {
                    model: fx.down ? 18 : 0
                    Rectangle {
                        required property int index
                        readonly property real angle: index * 137.5 % 360
                        readonly property real reach: (70 + index * 53 % 120) * fx.outCubic(fx.seg(400, 520))
                        visible: fx.ms > 400
                        x: fx.coreX + Math.cos(angle * Math.PI / 180) * reach - width / 2
                        y: fx.coreY + Math.sin(angle * Math.PI / 180) * reach - height / 2
                        width: 2.5; height: 9 + index % 4 * 3; radius: 1.25
                        rotation: angle + 90
                        color: index % 3 === 0 ? Theme.text : Theme.accent
                        opacity: 1 - fx.seg(440, 520)
                    }
                }
            }
        }
    }
}
