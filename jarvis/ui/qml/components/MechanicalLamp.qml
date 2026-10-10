import QtQuick
import "../theme"

// Mechanical status lamp, Gate 6G S1 (the third status skin): the JARVIS
// surface torn open on a running mechanism. The layers are cut from the
// approved hero image (master_v1.png, 1254 px) by scripts/lamp_assets/build.py,
// which writes their map to assets/lamp/parts.json; the lists below copy it
// (tests/test_g6s1_lamp.py holds the two equal). Motion follows the approved
// prototype "Mekanik Durum Lambası": one motor angle drives every part,
// meshing wheels turn opposite at a speed ~ 1/diameter, the escapement ticks,
// the cage swings, chains and coil scroll; the speed eases between states and
// ambient off leaves every part where it is. Metal only, no colour or light.
Item {
    id: lamp
    // The one size: the lamp's height in px incl. the curled flaps (approved 2.15x = 120).
    property real size: 120
    // ready | running | freezing | frozen, mapped from bridge.executionState by the caller.
    property string executionState: "ready"
    property bool ambient: Theme.ambient

    // The drive (read by the tests): the big wheel's angle, its speed (share of `base`),
    // the cage's swing and phase.
    property real motor: 0
    property real speed: 0
    property real swing: 0
    property real cagePhase: 0
    // The drive's frame clock (0..1 per loop) and its last reading.
    property real clock: 0
    property real lastClock: 0
    readonly property real base: 24                      // big wheel deg/s at full speed (LAMP APPROVED)
    readonly property var targets: ({
        ready: { s: 0.25, a: 12, tau: 0.7 },
        running: { s: 1, a: 30, tau: 0.6 },
        freezing: { s: 0, a: 0, tau: 0.9 },              // winds down in ~4 s
        frozen: { s: 0, a: 0, tau: 0.25 }
    })
    readonly property var target: targets[executionState] || targets.ready
    readonly property bool moving: ambient && Theme.ambient && (speed > 0 || swing > 0 || target.s > 0)
    // Texture detail: twice the drawn pixels (mipmapped), never above the master.
    readonly property real detail: Math.min(1, 2 * size * Screen.devicePixelRatio / 1254)

    readonly property var parts: [     // draw order of the prototype
        { n: "w9", x: 872, y: 457, w: 189, h: 185, cx: 972, cy: 556, r: 100 },
        { n: "w1", x: 305, y: 335, w: 392, h: 391, cx: 500, cy: 530, r: 195 },
        { n: "w6", x: 251, y: 682, w: 250, h: 249, cx: 375, cy: 807, r: 125 },
        { n: "sun", x: 336, y: 771, w: 69, h: 69, cx: 370, cy: 805, r: 33 },
        { n: "p1", x: 345, y: 715, w: 65, h: 65, cx: 377, cy: 747, r: 31 },
        { n: "p2", x: 283, y: 800, w: 65, h: 65, cx: 315, cy: 832, r: 31 },
        { n: "p3", x: 397, y: 805, w: 61, h: 61, cx: 427, cy: 835, r: 29 },
        { n: "w13", x: 485, y: 768, w: 45, h: 45, cx: 507, cy: 790, r: 21 },
        { n: "w4", x: 299, y: 184, w: 175, h: 175, cx: 386, cy: 271, r: 86 },
        { n: "w5", x: 540, y: 218, w: 137, h: 138, cx: 607, cy: 287, r: 69 },
        { n: "w2", x: 649, y: 307, w: 207, h: 207, cx: 752, cy: 410, r: 103 },
        { n: "w3", x: 744, y: 498, w: 136, h: 136, cx: 812, cy: 565, r: 67 },
        { n: "w8", x: 126, y: 401, w: 95, h: 95, cx: 173, cy: 448, r: 46 },
        { n: "w10", x: 608, y: 900, w: 85, h: 85, cx: 650, cy: 942, r: 41 },
        { n: "w11", x: 595, y: 1025, w: 85, h: 85, cx: 637, cy: 1067, r: 41 },
        { n: "w12", x: 930, y: 846, w: 55, h: 55, cx: 957, cy: 873, r: 26 },
        { n: "w7", x: 643, y: 649, w: 264, h: 267, cx: 775, cy: 782, r: 133 },
        { n: "l16", x: 1126, y: 504, w: 50, h: 170, cx: 1150, cy: 527 },
        { n: "l17", x: 947, y: 727, w: 72, h: 143, cx: 968, cy: 745 }
    ]
    readonly property var strips: [
        { n: "s_up", w: 228, h: 24, x0: 462, y0: 1006, ang: -29.453672726920946, len: 195.2331938989884, hw: 12, pitch: 16.5, sg: 1 },
        { n: "s_lo", w: 204, h: 24, x0: 498, y0: 1034, ang: -16.955313052025204, len: 171.45261736118232, hw: 12, pitch: 16.5, sg: -1 },
        { n: "s_coil", w: 97, h: 84, x0: 935, y0: 545, ang: 0, len: 83, hw: 42, pitch: 14, sg: 1 }
    ]
    readonly property var layers: ({
        shadow: { x: 0, y: 0, w: 1254, h: 1247 },
        plate: { x: 20, y: 20, w: 1221, h: 1185 },
        occ: { x: 267, y: 149, w: 809, h: 512 },
        rim: { x: 0, y: 0, w: 1254, h: 1247 }
    })
    // Turn sense and gearing of each wheel against the big wheel (w1).
    readonly property var turn: ({ w1: 1, w2: -1, w3: 1, w4: -1, w5: -1, w6: -1, w8: -1, w9: -1, w10: 1, w11: 1, w12: 1, w13: 1 })
    readonly property var gearing: ({ w8: 0.55, w10: 2.2, w11: 2.2, w12: 3.0 })
    readonly property point sunAxis: Qt.point(375, 807)    // the planet group turns with w6

    objectName: "mechanicalLamp"
    implicitWidth: size
    implicitHeight: size
    Accessible.role: Accessible.Indicator
    Accessible.name: ({
        ready: qsTr("JARVIS status: ready"),
        running: qsTr("JARVIS status: running an operation"),
        freezing: qsTr("JARVIS status: freeze requested"),
        frozen: qsTr("JARVIS status: frozen")
    })[targets[executionState] ? executionState : "ready"]

    function ratio(n) {
        const r = parts.find(p => p.n === n).r
        return gearing[n] !== undefined ? gearing[n] : 195 / r
    }
    function rad(d) { return d * Math.PI / 180 }
    // Spin of a part about its own pivot, in degrees.
    function spin(n) {
        const g = turn.w6 * motor * ratio("w6")
        if (n === "w4") {                                       // escapement: a 15 deg tick per tooth
            const a = motor * ratio("w4"), step = 15, f = (a / step) % 1, k = Math.floor(a / step)
            const e = f < 0.78 ? 0 : (1 - Math.cos(Math.PI * (f - 0.78) / 0.22)) / 2
            return turn.w4 * (k + e) * step
        }
        if (n === "w7") return swing * Math.sin(cagePhase)       // the balance cage
        if (n === "l16") return 2.2 * Math.sin(rad(motor * 5))
        if (n === "l17") return 3 * Math.sin(rad(motor * 4) + 1)
        if (n === "sun") return g * -2.2
        if (n === "p1" || n === "p2" || n === "p3") return g * 2.6
        return turn[n] * motor * ratio(n)
    }
    // A planet's shift as the group turns about the sun axis; 0 for every other part.
    function orbit(p, axis) {
        if (p.n[0] !== "p" && p.n !== "sun") return 0
        const g = rad(turn.w6 * motor * ratio("w6")), dx = p.cx - sunAxis.x, dy = p.cy - sunAxis.y
        return axis === "x" ? sunAxis.x + dx * Math.cos(g) - dy * Math.sin(g) - p.cx
                            : sunAxis.y + dx * Math.sin(g) + dy * Math.cos(g) - p.cy
    }
    // How far a strip's links have run, wrapped to one pitch.
    function stripOffset(s) {
        const run = s.n === "s_coil" ? motor * ratio("w9") / 360 * 60 * s.pitch : s.sg * rad(motor * ratio("w10")) * 30
        return ((run % s.pitch) + s.pitch) % s.pitch
    }
    // One drive step of dt seconds: the speed and the swing ease toward the state's target.
    function step(dt) {
        const t = target
        speed += (t.s - speed) * Math.min(1, dt / t.tau)
        swing += (t.a - swing) * Math.min(1, dt / (executionState === "freezing" ? 0.7 : t.tau * 1.4))
        if (t.s === 0 && speed < 0.01 && swing < 0.5) { speed = 0; swing = 0 }
        motor += base * speed * dt
        if (swing > 0.05) cagePhase += 2 * Math.PI * 1.5 * dt
    }

    Component.onCompleted: { speed = target.s; swing = target.a }

    // The one master drive: a frame clock that runs only while something moves.
    NumberAnimation on clock {
        id: drive
        objectName: "lampDrive"
        from: 0; to: 1
        duration: Theme.dur(200)
        loops: Animation.Infinite
        running: lamp.moving
        onStarted: lamp.lastClock = 0
    }
    onClockChanged: {
        const d = clock - lastClock
        lastClock = clock
        step(Math.min(0.05, (d < 0 ? d + 1 : d) * drive.duration / 1000))
    }

    Item {
        id: stage
        objectName: "lampStage"
        width: 1254; height: 1254                              // master pixels
        scale: lamp.size / 1254
        transformOrigin: Item.TopLeft

        Image {
            objectName: "lampLayer_shadow"
            x: lamp.layers.shadow.x; y: lamp.layers.shadow.y; width: lamp.layers.shadow.w; height: lamp.layers.shadow.h
            source: "../assets/lamp/shadow.png"; mipmap: true; smooth: true
            sourceSize: Qt.size(Math.ceil(width * lamp.detail), Math.ceil(height * lamp.detail))
        }
        Image {
            objectName: "lampLayer_plate"
            x: lamp.layers.plate.x; y: lamp.layers.plate.y; width: lamp.layers.plate.w; height: lamp.layers.plate.h
            source: "../assets/lamp/plate.png"; mipmap: true; smooth: true
            sourceSize: Qt.size(Math.ceil(width * lamp.detail), Math.ceil(height * lamp.detail))
        }
        Repeater {
            model: lamp.parts
            Image {
                id: part
                required property var modelData
                objectName: "lampPart_" + modelData.n
                x: modelData.x + lamp.orbit(modelData, "x"); y: modelData.y + lamp.orbit(modelData, "y")
                width: modelData.w; height: modelData.h
                source: "../assets/lamp/" + modelData.n + ".png"; mipmap: true; smooth: true
                sourceSize: Qt.size(Math.ceil(width * lamp.detail), Math.ceil(height * lamp.detail))
                transform: Rotation {
                    origin.x: part.modelData.cx - part.modelData.x; origin.y: part.modelData.cy - part.modelData.y
                    angle: lamp.spin(part.modelData.n)
                }
            }
        }
        Repeater {
            model: lamp.strips
            Item {
                id: strip
                required property var modelData
                readonly property real offset: lamp.stripOffset(modelData)
                objectName: "lampStrip_" + modelData.n
                x: modelData.x0; y: modelData.y0 - modelData.hw
                width: modelData.len; height: 2 * modelData.hw
                clip: true
                transform: Rotation { origin.y: strip.modelData.hw; angle: strip.modelData.ang }
                Repeater {
                    model: 2                                   // two copies cover the wrap
                    Image {
                        required property int index
                        x: strip.offset - strip.modelData.pitch * (2 - index)
                        width: strip.modelData.w; height: strip.modelData.h
                        source: "../assets/lamp/" + strip.modelData.n + ".png"; mipmap: true; smooth: true
                        sourceSize: Qt.size(Math.ceil(width * lamp.detail), Math.ceil(height * lamp.detail))
                    }
                }
            }
        }
        Image {
            objectName: "lampLayer_occ"
            x: lamp.layers.occ.x; y: lamp.layers.occ.y; width: lamp.layers.occ.w; height: lamp.layers.occ.h
            source: "../assets/lamp/occ.png"; mipmap: true; smooth: true
            sourceSize: Qt.size(Math.ceil(width * lamp.detail), Math.ceil(height * lamp.detail))
        }
        Image {
            objectName: "lampLayer_rim"
            x: lamp.layers.rim.x; y: lamp.layers.rim.y; width: lamp.layers.rim.w; height: lamp.layers.rim.h
            source: "../assets/lamp/rim.png"; mipmap: true; smooth: true
            sourceSize: Qt.size(Math.ceil(width * lamp.detail), Math.ceil(height * lamp.detail))
        }
    }
}
