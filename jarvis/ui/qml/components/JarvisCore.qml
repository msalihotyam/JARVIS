import QtQuick
import QtQuick.Shapes
import "../theme"

// The JARVIS core: Reactor A. A machine with energy circulating inside it,
// not a glowing sphere - dark material, selective light, and layers that
// move at different speeds in different directions. The background plate
// (L0) supplies the wide orbits and HUD linework around it; this component
// covers the plate's own drawn globe with a dark bed and builds the living
// reactor on top of it. No text anywhere in the core.
//
// Layers, back to front (D = this item's width):
//   L1 bed 0.56 D  ·  L2 optical chamber 0.36 D  ·  L3 six-sector mechanical
//   structure 0.48-0.60 D  ·  L4 main ring 0.66 D (clockwise)  ·  L5 second
//   ring 0.74 D (counter)  ·  L6 a 28 degree energy sweep  ·  L7 three energy
//   points  ·  L8 fixed ticks and event light.
// Every layer is a pre-rendered raster (dev/assets/reactor_core.py) that is
// only rotated, faded or scaled. Semantic light (blue information, amber
// attention, red failure, the brief green of a success) is emitting faces
// INSET into the six sector housings - the same centre, radius, band,
// angles and transform as the housing, read from the generated
// ReactorGeometry. A response travels round the whole circumference,
// housing by housing with a short overlap, so the machine answers as one;
// the dark separators, the blue chassis and the blue nucleus always remain.
// Confirmed OFF is dormant: nothing turns, sweeps, breathes or glows - the
// dark hardware stays with faint static edges. No shaders, no per-frame
// drawing, no offscreen effect chain.
//
// Motion is one 16 ms clock advancing a handful of angles at constant
// angular speed. State changes retune speed and light through eased
// ramps; no angle is ever reset by a state or an event. Everything pauses
// while the window is inactive or ambient effects are off.
Item {
    id: core
    property string state_: "idle"
    property bool active: true
    // The header draws its near linework (orbit ellipse, connector traces)
    // around the core only at wider layouts; environment light travels on
    // those lines and is absent when they are.
    property bool environment: true
    implicitWidth: 300; implicitHeight: 300
    readonly property real s: width
    readonly property real c: width / 2

    // -- the real state ------------------------------------------------------
    readonly property bool running: state_ === "running"
    readonly property bool executing: running && bridge.executing
    readonly property bool reasoning: running && !bridge.executing
    readonly property bool listening: state_ === "awaiting_input"
    readonly property bool blocked: state_ === "awaiting_approval"
    readonly property bool dormant: state_ === "disabled"
    // Waiting on someone or on an unknown outcome: outward energy stops,
    // the centre and the slow counter-rotations keep living.
    readonly property bool calm: blocked || state_ === "unknown"
    readonly property bool alive: Theme.ambient && active && visible
    // Typing is the composer's real activity; it never changes the look of
    // an active job.
    readonly property bool typing: bridge.composing && !running

    // -- tuning targets (Astra section 13) -------------------------------------
    property real level: 0.22
    property real mainSpeed: 360 / 43           // degrees per second, clockwise
    property real secondSpeed: -360 / 71        // counter-clockwise
    property real sweepSpeed: 360 / 19
    property real sweepGain: 1                  // 0 while waiting: no scan
    property real markerGain: 1                 // 0 while waiting: no outward circulation
    property int rampMs: 550
    Behavior on level { NumberAnimation { duration: Theme.dur(core.rampMs); easing.type: Easing.InOutCubic } }
    Behavior on mainSpeed { NumberAnimation { duration: Theme.dur(core.rampMs); easing.type: Easing.InOutCubic } }
    Behavior on secondSpeed { NumberAnimation { duration: Theme.dur(core.rampMs); easing.type: Easing.InOutCubic } }
    Behavior on sweepSpeed { NumberAnimation { duration: Theme.dur(core.rampMs); easing.type: Easing.InOutCubic } }
    Behavior on sweepGain { NumberAnimation { duration: Theme.dur(core.rampMs); easing.type: Easing.InOutCubic } }
    Behavior on markerGain { NumberAnimation { duration: Theme.dur(core.rampMs); easing.type: Easing.InOutCubic } }

    property string previous: "idle"
    function applyState() {
        var st = state_, was = previous
        previous = st
        // Entry times per state; exits settle over the leaving state's time.
        rampMs = st === "running" ? (bridge.executing ? 300 : 350)
               : st === "awaiting_input" ? 280 : st === "awaiting_approval" ? 300
               : was === "running" ? 600 : was === "awaiting_input" ? 500 : 450
        var m = 1, s2 = 1, sw = 360 / 19, lv = 0.22
        if (st === "running" && bridge.executing) { m = 2.2; s2 = 1.4; sw = 360 / 6; lv = 0.60 }
        else if (st === "running") { m = 1.7; s2 = 1.35; sw = 360 / 8; lv = 0.48 }
        else if (st === "awaiting_input") { m = 1.15; lv = 0.32 }
        else if (st === "disabled") { lv = 0.10 }
        level = lv
        mainSpeed = 360 / 43 * m
        secondSpeed = -360 / 71 * s2
        sweepSpeed = sw
        sweepGain = calm ? 0 : 1
        markerGain = calm ? 0 : 1
        // State-earned events. A completed approved execution is the one
        // confirmed top-level completion; everything else is a normal success.
        if (st === "completed") fire(bridge.executing ? "major" : "success")
        else if (st === "error") fire("error")
        else if (st === "awaiting_approval") fire("blocked")
        else if (st === "running" && bridge.executing && was !== "running") fire("launch")
    }
    onState_Changed: applyState()
    Component.onCompleted: { applyState(); if (dormant) settleDormant(); else if (alive) awaken.start(); else if (!Theme.ambient) settleBoot() }
    onAliveChanged: { if (alive && !booted && !dormant) awaken.start(); if (!alive) stopEvent() }

    // -- power: confirmed OFF is dormant; the visual is confirmation only --------
    // The real disabled state is already in effect when these start; nothing
    // waits for them. A toggle cancels whatever transition was running.
    property real circulation: 1                // sweep, points, chamber light, breath
    property real ringEnergy: 1                 // ring / sector illumination
    property real nucleus: 1                    // the focus and the chamber's light
    property real residual: 1                   // halo, tick glow: the last emission
    readonly property real ringFloor: 0.16      // faint static edges of dormant hardware
    onDormantChanged: power(dormant)
    function power(off) {
        awaken.stop(); shutdown.stop(); shutdownQuick.stop(); wakeQuick.stop()
        stopEvent()
        if (off) { if (Theme.ambient) shutdown.start(); else shutdownQuick.start() }
        else if (Theme.ambient) wake()
        else wakeQuick.start()
    }
    // Re-enable reuses the startup: dormant -> nucleus ignition -> mechanical
    // energisation -> the accepted living motion. It grants nothing.
    function wake() {
        circulation = 1; ringEnergy = 1; nucleus = 1; residual = 1
        bootFocus = 0; bootRim = 0; bootGlow = 0; spin = 0; spin2 = 0; booted = false
        if (alive) awaken.start()
    }
    function settleDormant() { booted = true; bootFocus = 1; bootRim = 1; bootGlow = 0; spin = 0; spin2 = 0; circulation = 0; ringEnergy = 0; nucleus = 0; residual = 0 }
    SequentialAnimation {
        id: shutdown
        // 0-180 ms: circulation decelerates and stops; the sweep and the moving points go out.
        ParallelAnimation {
            NumberAnimation { target: core; property: "spin"; to: 0; duration: Theme.dur(180); easing.type: Easing.OutCubic }
            NumberAnimation { target: core; property: "spin2"; to: 0; duration: Theme.dur(180); easing.type: Easing.OutCubic }
            NumberAnimation { target: core; property: "circulation"; to: 0; duration: Theme.dur(180); easing.type: Easing.OutCubic }
        }
        // 180-360: the segmented ring illumination drains.
        NumberAnimation { target: core; property: "ringEnergy"; to: 0; duration: Theme.dur(180); easing.type: Easing.InOutCubic }
        // 360-540: the nucleus fades.
        NumberAnimation { target: core; property: "nucleus"; to: 0; duration: Theme.dur(180); easing.type: Easing.InOutCubic }
        // 540-720: the remaining emission disappears; the dark hardware stays.
        NumberAnimation { target: core; property: "residual"; to: 0; duration: Theme.dur(180); easing.type: Easing.InOutCubic }
        ScriptAction { script: core.booted = true }
    }
    // Reduced motion (ambient effects off): a brief energy fade, no choreography.
    SequentialAnimation {
        id: shutdownQuick
        PropertyAction { target: core; property: "spin"; value: 0 }
        PropertyAction { target: core; property: "spin2"; value: 0 }
        ParallelAnimation {
            NumberAnimation { target: core; property: "circulation"; to: 0; duration: Theme.dur(250) }
            NumberAnimation { target: core; property: "ringEnergy"; to: 0; duration: Theme.dur(250) }
            NumberAnimation { target: core; property: "nucleus"; to: 0; duration: Theme.dur(250) }
            NumberAnimation { target: core; property: "residual"; to: 0; duration: Theme.dur(250) }
        }
        ScriptAction { script: core.booted = true }
    }
    SequentialAnimation {
        id: wakeQuick
        ScriptAction { script: { core.bootFocus = 1; core.bootRim = 1; core.bootGlow = 0; core.spin = 1; core.spin2 = 1; core.booted = true } }
        ParallelAnimation {
            NumberAnimation { target: core; property: "circulation"; to: 1; duration: Theme.dur(250) }
            NumberAnimation { target: core; property: "ringEnergy"; to: 1; duration: Theme.dur(250) }
            NumberAnimation { target: core; property: "nucleus"; to: 1; duration: Theme.dur(250) }
            NumberAnimation { target: core; property: "residual"; to: 1; duration: Theme.dur(250) }
        }
    }

    // Typing: +3 % at the centre, +10 % on the micro highlight, 180 ms in,
    // 450 ms back after the composer's own 900 ms quiet time.
    property real typeBoost: typing ? 1 : 0
    Behavior on typeBoost { NumberAnimation { duration: Theme.dur(core.typing ? 180 : 450); easing.type: Easing.InOutCubic } }
    // Event-only light: added to the level (success peaks) and to the focus.
    property real eventLevel: 0
    property real focusBoost: 0
    property real jolt: 0                       // a few degrees of highlight misalignment
    readonly property real lvl: level + 0.03 * typeBoost + eventLevel

    // -- ambient rhythms: independent periods, small amplitudes ------------------
    // All continuous motion is advanced by ONE 16 ms clock (about 60 updates
    // a second) rather than per-vsync animations, so an idle core does not
    // drive a 240 Hz display at 240 frames a second. Angles advance at
    // constant speed from measured time; the rhythms are sinusoids of the
    // same clock, so a pause never resets a phase.
    property real breath: 1.0                   // 6.7 s, +-5 % light, <=1 % scale
    property real rimPulse: 0.7                 // 11.3 s, not in step with the breath
    property real pointA: 0.9                   // the energy points are never all at peak together
    property real sweepAngle: 113
    property real orbitPhase: 150
    property real clockT: 0
    property double lastTick: 0
    Timer {
        id: clock
        interval: 16; repeat: true; running: core.alive
        onRunningChanged: if (running) core.lastTick = Date.now()
        onTriggered: {
            var now = Date.now()
            var dt = Math.min(Math.max(now - core.lastTick, 0) / 1000, 0.05)
            core.lastTick = now
            core.clockT += dt
            mainRing.rotation = (mainRing.rotation + core.mainSpeed * core.spin * dt) % 360
            secondRing.rotation = (secondRing.rotation + core.secondSpeed * core.spin2 * dt + 360) % 360
            core.sweepAngle = (core.sweepAngle + core.sweepSpeed * core.sweepGain * core.circulation * (1 + 0.1 * core.typeBoost) * dt) % 360
            chamberLight.rotation = (chamberLight.rotation + 360 / 53 * core.circulation * dt) % 360
            markerA.rotation = (markerA.rotation + 360 / 29 * core.markerGain * core.circulation * dt) % 360
            markerB.rotation = (markerB.rotation - 360 / 47 * core.markerGain * core.circulation * dt + 360) % 360
            core.orbitPhase = (core.orbitPhase + core.idleOrbitSpeed * core.markerGain * core.circulation * dt) % 360
            core.breath = 1 + 0.05 * core.nucleus * Math.sin(core.clockT * 2 * Math.PI / 6.7)
            core.rimPulse = 0.7 + 0.3 * core.nucleus * Math.sin(core.clockT * 2 * Math.PI / 11.3 + 2.1)
            core.pointA = 0.675 + 0.225 * Math.sin(core.clockT * 2 * Math.PI / 13 + 0.8)
        }
    }
    // Idle life (Gate 6G K2): the wave every 23 s and the orbit packet stay,
    // but in their OWN calmer look - fainter and slower than any real event -
    // so idle life never reads as "sent" or "success". The event waves start
    // at 0.7-1.0 and run 360-1100 ms; event packets start at 0.35-0.9.
    readonly property real idleWavePeak: 0.16
    readonly property int idleWaveMs: 1400
    readonly property real idleOrbitPeak: 0.3
    readonly property real idleOrbitSpeed: 360 / 90     // degrees per second
    // A low-energy radial wave now and then.
    Timer { interval: 23000; repeat: true; running: core.alive && !core.calm && core.booted && !core.dormant; onTriggered: core.fire("ambient") }

    // -- startup: 2.2 s from dormant structure to stable life -------------------
    property bool booted: false
    property real bootFocus: 0                  // the focus and the chamber light
    property real bootRim: 0                    // rim, sectors, rings, ticks to full
    property real bootGlow: 0                   // the transient extra light
    property real spin: 0                       // main ring gains motion
    property real spin2: 0                      // the second ring, later
    function settleBoot() { booted = true; bootFocus = 1; bootRim = 1; bootGlow = 0; spin = 1; spin2 = 1 }
    SequentialAnimation {
        id: awaken
        // 0-180 ms: the bed and a dim mechanical silhouette are simply there.
        PauseAnimation { duration: Theme.dur(180) }
        ParallelAnimation {
            // 180-480: the small focus appears, light spreads into the chamber.
            NumberAnimation { target: core; property: "bootFocus"; to: 1; duration: Theme.dur(300); easing.type: Easing.OutCubic }
            NumberAnimation { target: core; property: "bootGlow"; to: 1; duration: Theme.dur(300); easing.type: Easing.OutCubic }
            SequentialAnimation {
                // 480-900: the rim appears layer by layer; sectors energise in turn.
                PauseAnimation { duration: Theme.dur(300) }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "bootRim"; to: 1; duration: Theme.dur(420); easing.type: Easing.InOutCubic }
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(40) }
                        NumberAnimation { target: core; property: "litA"; to: 0.7; duration: Theme.dur(140) }
                        NumberAnimation { target: core; property: "litA"; to: 0; duration: Theme.dur(220) }
                    }
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(220) }
                        NumberAnimation { target: core; property: "litB"; to: 0.7; duration: Theme.dur(140) }
                        NumberAnimation { target: core; property: "litB"; to: 0; duration: Theme.dur(220) }
                    }
                    // 700-1300: the two rings acquire motion at different moments.
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(220) }
                        NumberAnimation { target: core; property: "spin"; to: 1; duration: Theme.dur(400); easing.type: Easing.InOutCubic }
                    }
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(420) }
                        NumberAnimation { target: core; property: "spin2"; to: 1; duration: Theme.dur(400); easing.type: Easing.InOutCubic }
                    }
                    // 1200-1650: one brief alignment of the inner highlights.
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(720) }
                        NumberAnimation { target: core; property: "litA"; to: 0.5; duration: Theme.dur(150) }
                        NumberAnimation { target: core; property: "litA"; to: 0; duration: Theme.dur(300) }
                    }
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(720) }
                        NumberAnimation { target: core; property: "litB"; to: 0.5; duration: Theme.dur(150) }
                        NumberAnimation { target: core; property: "litB"; to: 0; duration: Theme.dur(300) }
                    }
                    // 1500-1950: a single light packet out to the near orbit.
                    SequentialAnimation {
                        PauseAnimation { duration: Theme.dur(1020) }
                        ScriptAction { script: if (core.environment) core.packetOut(0.45, 450) }
                        PauseAnimation { duration: Theme.dur(350) }
                        // 1850-2200: the transient light fades; ambient motion stays.
                        NumberAnimation { target: core; property: "bootGlow"; to: 0; duration: Theme.dur(350); easing.type: Easing.InOutCubic }
                    }
                }
            }
        }
        ScriptAction { script: core.booted = true }
    }

    // -- sector rhythms (L3 light) ---------------------------------------------
    property real litA: 0                       // sectors 0 / 2 / 4
    property real litB: 0                       // sectors 1 / 3 / 5
    // Listening: two opposite sectors, in turn, on a 3.8 s rhythm.
    SequentialAnimation {
        running: core.alive && core.listening && core.booted; loops: Animation.Infinite
        onRunningChanged: if (!running) { core.litA = 0; core.litB = 0 }
        NumberAnimation { target: core; property: "litA"; to: 0.75; duration: Theme.dur(700); easing.type: Easing.InOutSine }
        NumberAnimation { target: core; property: "litA"; to: 0; duration: Theme.dur(1200); easing.type: Easing.InOutSine }
        NumberAnimation { target: core; property: "litB"; to: 0.75; duration: Theme.dur(700); easing.type: Easing.InOutSine }
        NumberAnimation { target: core; property: "litB"; to: 0; duration: Theme.dur(1200); easing.type: Easing.InOutSine }
    }
    // Reasoning: the six sectors in two groups, one after the other.
    SequentialAnimation {
        running: core.alive && core.reasoning && core.booted; loops: Animation.Infinite
        onRunningChanged: if (!running) { core.litA = 0; core.litB = 0 }
        NumberAnimation { target: core; property: "litA"; to: 0.85; duration: Theme.dur(420); easing.type: Easing.InOutSine }
        ParallelAnimation {
            NumberAnimation { target: core; property: "litA"; to: 0.15; duration: Theme.dur(780); easing.type: Easing.InOutSine }
            SequentialAnimation { PauseAnimation { duration: Theme.dur(260) } NumberAnimation { target: core; property: "litB"; to: 0.85; duration: Theme.dur(420); easing.type: Easing.InOutSine } }
        }
        NumberAnimation { target: core; property: "litB"; to: 0.15; duration: Theme.dur(780); easing.type: Easing.InOutSine }
    }
    // Executing: the sector light shares one rhythm (the rings keep their own speeds).
    SequentialAnimation {
        running: core.alive && core.executing && core.booted; loops: Animation.Infinite
        onRunningChanged: if (!running) { core.litA = 0; core.litB = 0 }
        ParallelAnimation {
            NumberAnimation { target: core; property: "litA"; to: 0.8; duration: Theme.dur(520); easing.type: Easing.InOutSine }
            NumberAnimation { target: core; property: "litB"; to: 0.8; duration: Theme.dur(520); easing.type: Easing.InOutSine }
        }
        ParallelAnimation {
            NumberAnimation { target: core; property: "litA"; to: 0.3; duration: Theme.dur(900); easing.type: Easing.InOutSine }
            NumberAnimation { target: core; property: "litB"; to: 0.3; duration: Theme.dur(900); easing.type: Easing.InOutSine }
        }
    }

    // -- events: one strong effect at a time, weak ones merged or skipped ---------
    // Priority: error / blocked > confirmed completion > model / message >
    // typing / focus / new activity. Effects highlight the real state; when
    // they end the layers are already showing whatever is true now.
    readonly property var rank: ({ "error": 5, "blocked": 5, "major": 4, "success": 4, "model": 3, "sent": 3 })
    property string strong: ""
    property double lastActivity: 0
    function fire(kind) {
        if (!alive || dormant) return
        var now = Date.now()
        if (rank[kind]) {
            if (strong !== "" && rank[strong] > rank[kind]) return
            stopEvent()
            strong = kind
            if (kind === "model" && executing) kind = "modelLocal"
        } else {
            if (strong !== "") return
            if (kind === "activity") { if (now - lastActivity < 2000) return; lastActivity = now }
        }
        var anim = { "success": successAnim, "major": majorAnim, "error": errorAnim, "blocked": blockedAnim,
                     "model": modelAnim, "modelLocal": modelLocalAnim, "sent": sentAnim, "activity": activityAnim,
                     "focus": focusAnim, "ambient": ambientAnim, "launch": launchAnim }[kind]
        if (anim) anim.restart()
    }
    function stopEvent() {
        for (var i = 0; i < eventAnims.length; i++) eventAnims[i].stop()
        strong = ""; eventLevel = 0; focusBoost = 0; jolt = 0; squeeze = 1
        wave.opacity = 0; packet.opacity = 0; ringWave.opacity = 0; rimPass.opacity = 0
        sig0 = 0; sig1 = 0; sig2 = 0; sig3 = 0; sig4 = 0; sig5 = 0
        blue0 = 0; blue1 = 0; blue2 = 0; blue3 = 0; blue4 = 0; blue5 = 0; okLevel = 0
    }
    // Semantic levels per sector housing: the amber / red faces (sig*, tinted
    // by the real state) and the blue information faces (blue*), plus the one
    // small green success face on the main ring. While a blocked or failed
    // state is current every sector holds a restrained level after the
    // propagation, and crossfades away when the state resolves.
    property real sig0: 0; property real sig1: 0; property real sig2: 0; property real sig3: 0; property real sig4: 0; property real sig5: 0
    property real blue0: 0; property real blue1: 0; property real blue2: 0; property real blue3: 0; property real blue4: 0; property real blue5: 0
    readonly property color sigTint: state_ === "error" ? Theme.danger : Theme.warning
    // The persistent level arrives after the propagation has gone round
    // (so the travel reads first) and crossfades away when the state resolves.
    readonly property real holdTarget: state_ === "error" ? 0.5 : blocked ? 0.38 : 0
    property real hold: 0
    onHoldTargetChanged: holdAnim.restart()
    SequentialAnimation {
        id: holdAnim
        PauseAnimation { duration: Theme.dur(core.holdTarget > 0 ? 520 : 0) }
        NumberAnimation { target: core; property: "hold"; to: core.holdTarget; duration: Theme.dur(350); easing.type: Easing.InOutCubic }
    }
    property real okLevel: 0
    readonly property var eventAnims: [successAnim, majorAnim, errorAnim, blockedAnim, modelAnim, modelLocalAnim, sentAnim, activityAnim, focusAnim, ambientAnim, launchAnim]
    property real squeeze: 1                    // the nucleus's 1 % compression

    // Hooks into what the application really did.
    Connections {
        target: bridge
        function onInputAccepted() { core.fire("sent") }
        function onModelApplied() { core.fire("model") }
        function onDataChanged() {
            var top = bridge.activity.length ? bridge.activity[0].ts : 0
            if (core.seenActivity === 0) { core.seenActivity = top; return }
            if (top > core.seenActivity) { core.seenActivity = top; core.fire("activity") }
        }
    }
    property double seenActivity: 0
    property double inactiveSince: 0
    onActiveChanged: {
        if (!active) { inactiveSince = Date.now(); return }
        if (inactiveSince > 0 && Date.now() - inactiveSince >= 3000 && booted) fire("focus")
    }

    // Light packets on the existing near lines only (never new lines).
    function packetOut(to, ms) {   // centre -> the right connector trace
        packetMove.stop()
        packet.x = c - packet.width / 2; packet.y = c - packet.height / 2; packet.opacity = 0.9
        packetMove.toX = c + s * to - packet.width / 2; packetMove.toY = packet.y; packetMove.ms = ms
        packetMove.start()
    }
    function packetDown(ms) {      // centre -> towards the lower connection, short
        packetMove.stop()
        packet.x = c - packet.width / 2; packet.y = c + s * 0.17 - packet.height / 2; packet.opacity = 0.75
        packetMove.toX = packet.x; packetMove.toY = c + s * 0.44 - packet.height / 2; packetMove.ms = ms
        packetMove.start()
    }
    function packetIn(fromX, fromY, ms) {   // from the environment into the centre
        packetMove.stop()
        packet.x = c + s * fromX - packet.width / 2; packet.y = c + s * fromY - packet.height / 2; packet.opacity = 0.8
        packetMove.toX = c - packet.width / 2; packetMove.toY = c - packet.height / 2; packetMove.ms = ms
        packetMove.start()
    }
    SequentialAnimation {
        id: packetMove
        property real toX: 0; property real toY: 0; property int ms: 400
        ParallelAnimation {
            NumberAnimation { target: packet; property: "x"; to: packetMove.toX; duration: Theme.dur(packetMove.ms); easing.type: Easing.InOutSine }
            NumberAnimation { target: packet; property: "y"; to: packetMove.toY; duration: Theme.dur(packetMove.ms); easing.type: Easing.InOutSine }
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(packetMove.ms * 0.55) }
                NumberAnimation { target: packet; property: "opacity"; to: 0; duration: Theme.dur(packetMove.ms * 0.45) }
            }
        }
    }

    // NORMAL SUCCESS: 150 ms alignment, a blue release wave to the main ring,
    // <=300 ms green on one small segment, a short rise at the centre. 850 ms,
    // then a 450 ms settle into whatever is true now.
    SequentialAnimation {
        id: successAnim
        ParallelAnimation {
            SequentialAnimation {
                ParallelAnimation {
                    NumberAnimation { target: core; property: "litA"; to: 0.6; duration: Theme.dur(150) }
                    NumberAnimation { target: core; property: "litB"; to: 0.6; duration: Theme.dur(150) }
                }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "litA"; to: 0; duration: Theme.dur(400) }
                    NumberAnimation { target: core; property: "litB"; to: 0; duration: Theme.dur(400) }
                }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "eventLevel"; to: 0.65 - core.level; duration: Theme.dur(200); easing.type: Easing.OutCubic }
                NumberAnimation { target: core; property: "focusBoost"; to: 0.3; duration: Theme.dur(120); easing.type: Easing.OutCubic }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(450); easing.type: Easing.InOutCubic }
                    NumberAnimation { target: core; property: "eventLevel"; to: 0; duration: Theme.dur(450); easing.type: Easing.InOutCubic }
                }
            }
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(120) }
                PropertyAction { target: wave; property: "scale"; value: 0.22 }
                PropertyAction { target: wave; property: "opacity"; value: 0.85 }
                ParallelAnimation {
                    NumberAnimation { target: wave; property: "scale"; to: 0.82; duration: Theme.dur(520); easing.type: Easing.OutCubic }
                    NumberAnimation { target: wave; property: "opacity"; to: 0; duration: Theme.dur(520); easing.type: Easing.InCubic }
                }
            }
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(380) }
                NumberAnimation { target: core; property: "okLevel"; to: 0.9; duration: Theme.dur(80) }
                NumberAnimation { target: core; property: "okLevel"; to: 0; duration: Theme.dur(220); easing.type: Easing.InCubic }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // MAJOR COMPLETION: 200 ms gathering, a 550 ms wave to the outer
    // environment, <=600 ms faint echo on the near line, 650 ms return.
    SequentialAnimation {
        id: majorAnim
        ParallelAnimation {
            NumberAnimation { target: core; property: "eventLevel"; to: 0.78 - core.level; duration: Theme.dur(200); easing.type: Easing.InCubic }
            NumberAnimation { target: core; property: "focusBoost"; to: 0.4; duration: Theme.dur(200); easing.type: Easing.InCubic }
        }
        ParallelAnimation {
            SequentialAnimation {
                PropertyAction { target: wave; property: "scale"; value: 0.2 }
                PropertyAction { target: wave; property: "opacity"; value: 1 }
                ParallelAnimation {
                    NumberAnimation { target: wave; property: "scale"; to: 1.18; duration: Theme.dur(550); easing.type: Easing.OutCubic }
                    NumberAnimation { target: wave; property: "opacity"; to: 0; duration: Theme.dur(550); easing.type: Easing.InCubic }
                }
            }
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(120) }
                ScriptAction { script: if (core.environment) { core.packetOut(0.56, 600); packet.opacity = 0.35 } }
            }
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(200) }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(650); easing.type: Easing.InOutCubic }
                    NumberAnimation { target: core; property: "eventLevel"; to: 0; duration: Theme.dur(650); easing.type: Easing.InOutCubic }
                }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // One full-circumference propagation over the six sector housings:
    // each face rises as the previous one is still lit, so the response
    // reads as one connected machine, then falls back to whatever the state
    // holds. Used by the amber and red responses (the sig faces) and, on the
    // blue rasters, by ordinary information.
    component Travel: ParallelAnimation {
        property string prefix: "sig"
        property real peak: 1
        property int step: 70
        property int rise: 110
        property int fall: 260
        SequentialAnimation { NumberAnimation { target: core; property: prefix + "0"; to: peak; duration: Theme.dur(rise) } NumberAnimation { target: core; property: prefix + "0"; to: 0; duration: Theme.dur(fall); easing.type: Easing.InCubic } }
        SequentialAnimation { PauseAnimation { duration: Theme.dur(step) } NumberAnimation { target: core; property: prefix + "1"; to: peak; duration: Theme.dur(rise) } NumberAnimation { target: core; property: prefix + "1"; to: 0; duration: Theme.dur(fall); easing.type: Easing.InCubic } }
        SequentialAnimation { PauseAnimation { duration: Theme.dur(2 * step) } NumberAnimation { target: core; property: prefix + "2"; to: peak; duration: Theme.dur(rise) } NumberAnimation { target: core; property: prefix + "2"; to: 0; duration: Theme.dur(fall); easing.type: Easing.InCubic } }
        SequentialAnimation { PauseAnimation { duration: Theme.dur(3 * step) } NumberAnimation { target: core; property: prefix + "3"; to: peak; duration: Theme.dur(rise) } NumberAnimation { target: core; property: prefix + "3"; to: 0; duration: Theme.dur(fall); easing.type: Easing.InCubic } }
        SequentialAnimation { PauseAnimation { duration: Theme.dur(4 * step) } NumberAnimation { target: core; property: prefix + "4"; to: peak; duration: Theme.dur(rise) } NumberAnimation { target: core; property: prefix + "4"; to: 0; duration: Theme.dur(fall); easing.type: Easing.InCubic } }
        SequentialAnimation { PauseAnimation { duration: Theme.dur(5 * step) } NumberAnimation { target: core; property: prefix + "5"; to: peak; duration: Theme.dur(rise) } NumberAnimation { target: core; property: prefix + "5"; to: 0; duration: Theme.dur(fall); easing.type: Easing.InCubic } }
    }
    // ERROR: the strongest response - red travels the full circumference,
    // then the six faces hold a controlled red while the failure is current;
    // 2-3 degrees of highlight misalignment; the centre dips 100 ms and
    // returns to blue. The nucleus and the chassis stay blue.
    SequentialAnimation {
        id: errorAnim
        ParallelAnimation {
            Travel { prefix: "sig"; peak: 1; step: 70; rise: 110; fall: 300 }
            SequentialAnimation {
                NumberAnimation { target: core; property: "jolt"; to: 2.6; duration: Theme.dur(110); easing.type: Easing.OutCubic }
                NumberAnimation { target: core; property: "jolt"; to: 0; duration: Theme.dur(560); easing.type: Easing.InOutCubic }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "focusBoost"; to: -0.18; duration: Theme.dur(100) }
                NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(420); easing.type: Easing.InOutCubic }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // BLOCKED: muted technical amber travels the full circumference while
    // outward motion stops; then the six faces hold a restrained amber for
    // as long as attention is genuinely required. No neon ring, no amber
    // nucleus, no flashing; the state's own calm blue wait continues under it.
    SequentialAnimation {
        id: blockedAnim
        Travel { prefix: "sig"; peak: 0.85; step: 70; rise: 110; fall: 260 }
        ScriptAction { script: core.strong = "" }
    }
    // MODEL APPLIED: highlights separate in phase, the centre eases 6 %
    // down, the rings settle to a new highlight alignment, one blue retune
    // ring completes. ~1.1 s. Rotation angles are untouched.
    SequentialAnimation {
        id: modelAnim
        ParallelAnimation {
            SequentialAnimation {
                NumberAnimation { target: core; property: "jolt"; to: -3.2; duration: Theme.dur(180); easing.type: Easing.OutCubic }
                NumberAnimation { target: core; property: "jolt"; to: 0; duration: Theme.dur(620); easing.type: Easing.InOutCubic }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "focusBoost"; to: -0.06; duration: Theme.dur(220); easing.type: Easing.InOutCubic }
                NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(700); easing.type: Easing.InOutCubic }
            }
            SequentialAnimation {
                ScriptAction { script: { core.sweepAngle = (core.sweepAngle + 14) % 360; if (core.environment) core.packetIn(1.05, 0, 520) } }
                PropertyAction { target: ringWave; property: "scale"; value: 0.3 }
                PropertyAction { target: ringWave; property: "opacity"; value: 0.7 }
                ParallelAnimation {
                    NumberAnimation { target: ringWave; property: "scale"; to: 0.98; duration: Theme.dur(1100); easing.type: Easing.InOutSine }
                    SequentialAnimation { PauseAnimation { duration: Theme.dur(500) } NumberAnimation { target: ringWave; property: "opacity"; to: 0; duration: Theme.dur(600) } }
                }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // A model applied during execution: a small local acknowledgement only.
    SequentialAnimation {
        id: modelLocalAnim
        NumberAnimation { target: core; property: "focusBoost"; to: -0.06; duration: Theme.dur(160) }
        NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(400); easing.type: Easing.InOutCubic }
        ScriptAction { script: core.strong = "" }
    }
    // MESSAGE ACCEPTED: 90 ms of 1 % compression, a 360 ms blue wave, a
    // short transfer of light towards the lower connection. ~600 ms.
    SequentialAnimation {
        id: sentAnim
        NumberAnimation { target: core; property: "squeeze"; to: 0.99; duration: Theme.dur(90); easing.type: Easing.OutCubic }
        ParallelAnimation {
            NumberAnimation { target: core; property: "squeeze"; to: 1; duration: Theme.dur(260); easing.type: Easing.OutCubic }
            SequentialAnimation {
                PropertyAction { target: wave; property: "scale"; value: 0.2 }
                PropertyAction { target: wave; property: "opacity"; value: 0.7 }
                ParallelAnimation {
                    NumberAnimation { target: wave; property: "scale"; to: 0.62; duration: Theme.dur(360); easing.type: Easing.OutCubic }
                    NumberAnimation { target: wave; property: "opacity"; to: 0; duration: Theme.dur(360); easing.type: Easing.InCubic }
                }
            }
            ScriptAction { script: core.packetDown(320) }
        }
        PauseAnimation { duration: Theme.dur(150) }
        ScriptAction { script: core.strong = "" }
    }
    // NEW ACTIVITY: a packet on the near orbit comes in from the surroundings,
    // the focus answers for 120 ms. Low intensity, additive, at most one per 2 s.
    SequentialAnimation {
        id: activityAnim
        ScriptAction { script: core.packetIn(core.environment ? -0.53 : -0.42, -0.12, 380) }
        ParallelAnimation {
            // Blue information acknowledged by the whole machine: the six
            // sector faces light in turn round the circumference, overlapping
            // briefly, then the accepted living motion continues.
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(100) }
                Travel { prefix: "blue"; peak: 0.8; step: 60; rise: 90; fall: 260 }
            }
            SequentialAnimation {
                PauseAnimation { duration: Theme.dur(300) }
                NumberAnimation { target: core; property: "focusBoost"; to: 0.08; duration: Theme.dur(60) }
                NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(180); easing.type: Easing.InOutCubic }
            }
        }
    }
    // FOCUS RETURN: +4 % at the centre, a highlight pass along the rim, back in 400 ms.
    SequentialAnimation {
        id: focusAnim
        ParallelAnimation {
            NumberAnimation { target: core; property: "focusBoost"; to: 0.04; duration: Theme.dur(120); easing.type: Easing.OutCubic }
            NumberAnimation { target: rimPass; property: "opacity"; to: 0.9; duration: Theme.dur(120) }
        }
        ParallelAnimation {
            NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: Theme.dur(400); easing.type: Easing.InOutCubic }
            NumberAnimation { target: rimPass; property: "rotation"; from: 0; to: 150; duration: Theme.dur(400); easing.type: Easing.InOutCubic }
            NumberAnimation { target: rimPass; property: "opacity"; to: 0; duration: Theme.dur(400); easing.type: Easing.InCubic }
        }
    }
    // The idle radial wave: low energy, in its own calmer look (K2).
    SequentialAnimation {
        id: ambientAnim
        PropertyAction { target: wave; property: "scale"; value: 0.25 }
        PropertyAction { target: wave; property: "opacity"; value: core.idleWavePeak }
        ParallelAnimation {
            NumberAnimation { target: wave; property: "scale"; to: 0.74; duration: Theme.dur(core.idleWaveMs); easing.type: Easing.OutCubic }
            NumberAnimation { target: wave; property: "opacity"; to: 0; duration: Theme.dur(core.idleWaveMs); easing.type: Easing.InCubic }
        }
    }
    // EXECUTION ENTRY: one light packet from the centre to the near header line.
    SequentialAnimation {
        id: launchAnim
        ScriptAction { script: if (core.environment) core.packetOut(0.56, 420) }
    }

    // Housing geometry (generated with the rasters) and the one emitting face
    // every semantic signal is made of: 7 degrees of an existing housing,
    // inset by the bezel so dark material stays visible around it, with a
    // narrow edge highlight and a bloom too short to bridge a separator.
    // The face is a child of its housing, so centre, transform and rotation
    // are the housing's own. Thin housings (the rings) carry the face at
    // their full width and skip the bezel.
    ReactorGeometry { id: geo }
    component MicroFace: Shape {
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
    Image { source: "../assets/core-halo.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: 0.82 * (0.45 + 0.6 * (core.lvl - 0.22) + 0.3 * core.bootGlow) * core.residual }
    // L1: the dark bed. Present from the first frame: the plate's globe never shows.
    Image { source: "../assets/core-bed.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true }
    // L8 (static part): fixed tick groups.
    Image { source: "../assets/core-ticks.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: (0.3 + 0.5 * core.bootRim) * (0.25 + 0.75 * core.residual) }
    // L3: the six-sector mechanical structure (dark hardware, kept when
    // dormant) and its light: the state rhythms (litA / litB) and the blue
    // information faces share the same inset rasters, one per housing.
    Image { source: "../assets/core-sectors.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: (0.4 + 0.6 * core.bootRim) * (0.55 + 0.45 * core.ringEnergy) }
    Repeater {
        model: 6
        Image {
            source: "../assets/core-sector-lit.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: index * 60
            // Listening lights only the opposite pair (sectors 0 and 3).
            readonly property real lit: (core.listening && index !== 0 && index !== 3) ? 0 : (index % 2 === 0 ? core.litA : core.litB)
            opacity: lit * core.ringEnergy
            visible: opacity > 0.01
        }
    }
    // L5: the second ring - thinner, dimmer, counter-clockwise, its own segments.
    Image { id: secondRing; source: "../assets/core-ring-secondary.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: 37; opacity: (0.3 + 0.7 * core.bootRim) * (0.72 + 0.4 * (core.lvl - 0.22)) * (core.ringFloor + (1 - core.ringFloor) * core.ringEnergy) }
    // L4: the main ring - the most legible motion, clockwise. Its blue
    // information faces and the green success face are its own children:
    // same centre, same radius and band, angles in its frame, turning with it.
    Image { id: mainRing; source: "../assets/core-ring-main.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: 0; opacity: (0.3 + 0.7 * core.bootRim) * Math.min(1, 0.86 + 0.5 * (core.lvl - 0.22)) * (core.ringFloor + (1 - core.ringFloor) * core.ringEnergy)
        // The one small green success face rides on the ring's first arc.
        MicroFace { radius: geo.mainRadius; band: geo.mainBand; tint: Theme.success
                    angle: geo.mainArcs[0][0] + 12; level: core.okLevel }
    }
    // L6: the energy sweep, a 28 degree highlight travelling over the rings.
    Image { id: sweep; source: "../assets/core-sweep.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: core.sweepAngle + core.jolt
            opacity: (0.5 + 1.0 * (core.lvl - 0.22)) * core.sweepGain * core.bootRim * core.circulation; visible: opacity > 0.01 }
    // L7: two energy points on the ring tracks (their carriers turn), and
    // one packet on the header's horizontal orbit.
    Item { id: markerA; anchors.centerIn: parent; width: core.s; height: width; rotation: 250
           Image { source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
                   x: core.c + core.s * 0.33 - width / 2; y: core.c - height / 2; opacity: core.pointA * core.bootRim * core.circulation } }
    Item { id: markerB; anchors.centerIn: parent; width: core.s; height: width; rotation: 95
           Image { source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
                   x: core.c + core.s * 0.37 - width / 2; y: core.c - height / 2; opacity: 0.55 * core.bootRim * core.circulation } }
    Image {
        id: orbitPacket
        source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
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
                    angle: geo.sectorCentres[index]; span: geo.sectorSpan; tint: core.sigTint
                    level: Math.max([core.sig0, core.sig1, core.sig2, core.sig3, core.sig4, core.sig5][index], core.hold) * core.ringEnergy }
    }
    // Blue information: the same rich emitting faces, in the structural blue.
    Repeater {
        model: 6
        MicroFace { radius: (geo.sectorInner + geo.sectorOuter) / 2; band: geo.sectorOuter - geo.sectorInner
                    angle: geo.sectorCentres[index]; span: geo.sectorSpan; tint: "#4fb8ff"
                    level: [core.blue0, core.blue1, core.blue2, core.blue3, core.blue4, core.blue5][index] * core.ringEnergy }
    }
    // Event light: the wave (scaled outward), the retune ring, the packet.
    Image { id: wave; source: "../assets/core-wave.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true; opacity: 0; visible: opacity > 0.01 }
    Image { id: ringWave; source: "../assets/core-wave.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true; opacity: 0; visible: opacity > 0.01 }
    Image { id: packet; source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true; opacity: 0; visible: opacity > 0.01 }

    // L2: the optical chamber and its nucleus, breathing in light, barely in size.
    Item {
        anchors.centerIn: parent; width: core.s * 0.5; height: width
        scale: (1 + 0.16 * (core.breath - 1)) * core.squeeze
        Image { source: "../assets/core-chamber.png"; anchors.fill: parent; mipmap: true; opacity: 0.45 + 0.55 * core.nucleus }
        Image { id: chamberLight; source: "../assets/core-chamber-light.png"; anchors.fill: parent; mipmap: true; rotation: 40
                opacity: 0.85 * core.bootFocus * core.nucleus }
        Image { source: "../assets/core-rim-hi.png"; anchors.fill: parent; mipmap: true
                opacity: (core.rimPulse * 0.8 + 0.2 * core.bootGlow) * core.bootRim * core.nucleus }
        Image { id: rimPass; source: "../assets/core-rim-hi.png"; anchors.fill: parent; mipmap: true; opacity: 0; visible: opacity > 0.01 }
        Image { source: "../assets/core-focus-glow.png"; anchors.centerIn: parent; width: core.s * 0.4; height: width; mipmap: true
                opacity: Math.min(1, (0.34 + 1.1 * (core.lvl - 0.22) + core.focusBoost + 0.35 * core.bootGlow) * core.breath) * core.bootFocus * core.nucleus }
        Image { source: "../assets/core-focus.png"; anchors.centerIn: parent; width: core.s * 0.2; height: width; mipmap: true
                opacity: Math.min(1, (0.9 + 0.9 * core.focusBoost + 0.3 * (core.lvl - 0.22)) * core.breath) * core.bootFocus * core.nucleus }
    }
}
