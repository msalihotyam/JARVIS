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
// only rotated, faded or scaled; the semantic event arcs are three Shape
// strokes. No shaders, no per-frame drawing, no offscreen effect chain.
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
    Behavior on level { NumberAnimation { duration: core.rampMs; easing.type: Easing.InOutCubic } }
    Behavior on mainSpeed { NumberAnimation { duration: core.rampMs; easing.type: Easing.InOutCubic } }
    Behavior on secondSpeed { NumberAnimation { duration: core.rampMs; easing.type: Easing.InOutCubic } }
    Behavior on sweepSpeed { NumberAnimation { duration: core.rampMs; easing.type: Easing.InOutCubic } }
    Behavior on sweepGain { NumberAnimation { duration: core.rampMs; easing.type: Easing.InOutCubic } }
    Behavior on markerGain { NumberAnimation { duration: core.rampMs; easing.type: Easing.InOutCubic } }

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
    Component.onCompleted: { applyState(); if (alive) awaken.start(); else if (!Theme.ambient) settleBoot() }
    onAliveChanged: { if (alive && !booted) awaken.start(); if (!alive) stopEvent() }

    // Typing: +3 % at the centre, +10 % on the micro highlight, 180 ms in,
    // 450 ms back after the composer's own 900 ms quiet time.
    property real typeBoost: typing ? 1 : 0
    Behavior on typeBoost { NumberAnimation { duration: core.typing ? 180 : 450; easing.type: Easing.InOutCubic } }
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
            core.sweepAngle = (core.sweepAngle + core.sweepSpeed * core.sweepGain * (1 + 0.1 * core.typeBoost) * dt) % 360
            chamberLight.rotation = (chamberLight.rotation + 360 / 53 * dt) % 360
            markerA.rotation = (markerA.rotation + 360 / 29 * core.markerGain * dt) % 360
            markerB.rotation = (markerB.rotation - 360 / 47 * core.markerGain * dt + 360) % 360
            core.orbitPhase = (core.orbitPhase + 360 / 61 * core.markerGain * dt) % 360
            core.breath = 1 + 0.05 * Math.sin(core.clockT * 2 * Math.PI / 6.7)
            core.rimPulse = 0.7 + 0.3 * Math.sin(core.clockT * 2 * Math.PI / 11.3 + 2.1)
            core.pointA = 0.675 + 0.225 * Math.sin(core.clockT * 2 * Math.PI / 13 + 0.8)
        }
    }
    // A low-energy radial wave now and then, gone within 900 ms.
    Timer { interval: 23000; repeat: true; running: core.alive && !core.calm && core.booted; onTriggered: core.fire("ambient") }

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
        PauseAnimation { duration: 180 }
        ParallelAnimation {
            // 180-480: the small focus appears, light spreads into the chamber.
            NumberAnimation { target: core; property: "bootFocus"; to: 1; duration: 300; easing.type: Easing.OutCubic }
            NumberAnimation { target: core; property: "bootGlow"; to: 1; duration: 300; easing.type: Easing.OutCubic }
            SequentialAnimation {
                // 480-900: the rim appears layer by layer; sectors energise in turn.
                PauseAnimation { duration: 300 }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "bootRim"; to: 1; duration: 420; easing.type: Easing.InOutCubic }
                    SequentialAnimation {
                        PauseAnimation { duration: 40 }
                        NumberAnimation { target: core; property: "litA"; to: 0.7; duration: 140 }
                        NumberAnimation { target: core; property: "litA"; to: 0; duration: 220 }
                    }
                    SequentialAnimation {
                        PauseAnimation { duration: 220 }
                        NumberAnimation { target: core; property: "litB"; to: 0.7; duration: 140 }
                        NumberAnimation { target: core; property: "litB"; to: 0; duration: 220 }
                    }
                    // 700-1300: the two rings acquire motion at different moments.
                    SequentialAnimation {
                        PauseAnimation { duration: 220 }
                        NumberAnimation { target: core; property: "spin"; to: 1; duration: 400; easing.type: Easing.InOutCubic }
                    }
                    SequentialAnimation {
                        PauseAnimation { duration: 420 }
                        NumberAnimation { target: core; property: "spin2"; to: 1; duration: 400; easing.type: Easing.InOutCubic }
                    }
                    // 1200-1650: one brief alignment of the inner highlights.
                    SequentialAnimation {
                        PauseAnimation { duration: 720 }
                        NumberAnimation { target: core; property: "litA"; to: 0.5; duration: 150 }
                        NumberAnimation { target: core; property: "litA"; to: 0; duration: 300 }
                    }
                    SequentialAnimation {
                        PauseAnimation { duration: 720 }
                        NumberAnimation { target: core; property: "litB"; to: 0.5; duration: 150 }
                        NumberAnimation { target: core; property: "litB"; to: 0; duration: 300 }
                    }
                    // 1500-1950: a single light packet out to the near orbit.
                    SequentialAnimation {
                        PauseAnimation { duration: 1020 }
                        ScriptAction { script: if (core.environment) core.packetOut(0.45, 450) }
                        PauseAnimation { duration: 350 }
                        // 1850-2200: the transient light fades; ambient motion stays.
                        NumberAnimation { target: core; property: "bootGlow"; to: 0; duration: 350; easing.type: Easing.InOutCubic }
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
        NumberAnimation { target: core; property: "litA"; to: 0.75; duration: 700; easing.type: Easing.InOutSine }
        NumberAnimation { target: core; property: "litA"; to: 0; duration: 1200; easing.type: Easing.InOutSine }
        NumberAnimation { target: core; property: "litB"; to: 0.75; duration: 700; easing.type: Easing.InOutSine }
        NumberAnimation { target: core; property: "litB"; to: 0; duration: 1200; easing.type: Easing.InOutSine }
    }
    // Reasoning: the six sectors in two groups, one after the other.
    SequentialAnimation {
        running: core.alive && core.reasoning && core.booted; loops: Animation.Infinite
        onRunningChanged: if (!running) { core.litA = 0; core.litB = 0 }
        NumberAnimation { target: core; property: "litA"; to: 0.85; duration: 420; easing.type: Easing.InOutSine }
        ParallelAnimation {
            NumberAnimation { target: core; property: "litA"; to: 0.15; duration: 780; easing.type: Easing.InOutSine }
            SequentialAnimation { PauseAnimation { duration: 260 } NumberAnimation { target: core; property: "litB"; to: 0.85; duration: 420; easing.type: Easing.InOutSine } }
        }
        NumberAnimation { target: core; property: "litB"; to: 0.15; duration: 780; easing.type: Easing.InOutSine }
    }
    // Executing: the sector light shares one rhythm (the rings keep their own speeds).
    SequentialAnimation {
        running: core.alive && core.executing && core.booted; loops: Animation.Infinite
        onRunningChanged: if (!running) { core.litA = 0; core.litB = 0 }
        ParallelAnimation {
            NumberAnimation { target: core; property: "litA"; to: 0.8; duration: 520; easing.type: Easing.InOutSine }
            NumberAnimation { target: core; property: "litB"; to: 0.8; duration: 520; easing.type: Easing.InOutSine }
        }
        ParallelAnimation {
            NumberAnimation { target: core; property: "litA"; to: 0.3; duration: 900; easing.type: Easing.InOutSine }
            NumberAnimation { target: core; property: "litB"; to: 0.3; duration: 900; easing.type: Easing.InOutSine }
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
        if (!alive) return
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
        greenSeg.opacity = 0; redSeg.opacity = 0; amberSeg.opacity = 0
    }
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
        packetMove.toX = c + s * to - packet.width / 2; packetMove.toY = packet.y; packetMove.duration = ms
        packetMove.start()
    }
    function packetDown(ms) {      // centre -> towards the lower connection, short
        packetMove.stop()
        packet.x = c - packet.width / 2; packet.y = c + s * 0.17 - packet.height / 2; packet.opacity = 0.75
        packetMove.toX = packet.x; packetMove.toY = c + s * 0.44 - packet.height / 2; packetMove.duration = ms
        packetMove.start()
    }
    function packetIn(fromX, fromY, ms) {   // from the environment into the centre
        packetMove.stop()
        packet.x = c + s * fromX - packet.width / 2; packet.y = c + s * fromY - packet.height / 2; packet.opacity = 0.8
        packetMove.toX = c - packet.width / 2; packetMove.toY = c - packet.height / 2; packetMove.duration = ms
        packetMove.start()
    }
    SequentialAnimation {
        id: packetMove
        property real toX: 0; property real toY: 0; property int duration: 400
        ParallelAnimation {
            NumberAnimation { target: packet; property: "x"; to: packetMove.toX; duration: packetMove.duration; easing.type: Easing.InOutSine }
            NumberAnimation { target: packet; property: "y"; to: packetMove.toY; duration: packetMove.duration; easing.type: Easing.InOutSine }
            SequentialAnimation {
                PauseAnimation { duration: packetMove.duration * 0.55 }
                NumberAnimation { target: packet; property: "opacity"; to: 0; duration: packetMove.duration * 0.45 }
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
                    NumberAnimation { target: core; property: "litA"; to: 0.6; duration: 150 }
                    NumberAnimation { target: core; property: "litB"; to: 0.6; duration: 150 }
                }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "litA"; to: 0; duration: 400 }
                    NumberAnimation { target: core; property: "litB"; to: 0; duration: 400 }
                }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "eventLevel"; to: 0.65 - core.level; duration: 200; easing.type: Easing.OutCubic }
                NumberAnimation { target: core; property: "focusBoost"; to: 0.3; duration: 120; easing.type: Easing.OutCubic }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 450; easing.type: Easing.InOutCubic }
                    NumberAnimation { target: core; property: "eventLevel"; to: 0; duration: 450; easing.type: Easing.InOutCubic }
                }
            }
            SequentialAnimation {
                PauseAnimation { duration: 120 }
                PropertyAction { target: wave; property: "scale"; value: 0.22 }
                PropertyAction { target: wave; property: "opacity"; value: 0.85 }
                ParallelAnimation {
                    NumberAnimation { target: wave; property: "scale"; to: 0.82; duration: 520; easing.type: Easing.OutCubic }
                    NumberAnimation { target: wave; property: "opacity"; to: 0; duration: 520; easing.type: Easing.InCubic }
                }
            }
            SequentialAnimation {
                PauseAnimation { duration: 380 }
                NumberAnimation { target: greenSeg; property: "opacity"; to: 0.9; duration: 80 }
                NumberAnimation { target: greenSeg; property: "opacity"; to: 0; duration: 220; easing.type: Easing.InCubic }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // MAJOR COMPLETION: 200 ms gathering, a 550 ms wave to the outer
    // environment, <=600 ms faint echo on the near line, 650 ms return.
    SequentialAnimation {
        id: majorAnim
        ParallelAnimation {
            NumberAnimation { target: core; property: "eventLevel"; to: 0.78 - core.level; duration: 200; easing.type: Easing.InCubic }
            NumberAnimation { target: core; property: "focusBoost"; to: 0.4; duration: 200; easing.type: Easing.InCubic }
        }
        ParallelAnimation {
            SequentialAnimation {
                PropertyAction { target: wave; property: "scale"; value: 0.2 }
                PropertyAction { target: wave; property: "opacity"; value: 1 }
                ParallelAnimation {
                    NumberAnimation { target: wave; property: "scale"; to: 1.18; duration: 550; easing.type: Easing.OutCubic }
                    NumberAnimation { target: wave; property: "opacity"; to: 0; duration: 550; easing.type: Easing.InCubic }
                }
            }
            SequentialAnimation {
                PauseAnimation { duration: 120 }
                ScriptAction { script: if (core.environment) { core.packetOut(0.56, 600); packet.opacity = 0.35 } }
            }
            SequentialAnimation {
                PauseAnimation { duration: 200 }
                ParallelAnimation {
                    NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 650; easing.type: Easing.InOutCubic }
                    NumberAnimation { target: core; property: "eventLevel"; to: 0; duration: 650; easing.type: Easing.InOutCubic }
                }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // ERROR: one sector red for <=350 ms, 2-3 degrees of highlight
    // misalignment, the centre dips 100 ms and returns to blue. ~800 ms.
    SequentialAnimation {
        id: errorAnim
        ParallelAnimation {
            SequentialAnimation {
                NumberAnimation { target: redSeg; property: "opacity"; to: 0.85; duration: 90 }
                PauseAnimation { duration: 80 }
                NumberAnimation { target: redSeg; property: "opacity"; to: 0; duration: 180; easing.type: Easing.InCubic }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "jolt"; to: 2.6; duration: 110; easing.type: Easing.OutCubic }
                NumberAnimation { target: core; property: "jolt"; to: 0; duration: 560; easing.type: Easing.InOutCubic }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "focusBoost"; to: -0.18; duration: 100 }
                NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 420; easing.type: Easing.InOutCubic }
            }
        }
        PauseAnimation { duration: 130 }
        ScriptAction { script: core.strong = "" }
    }
    // BLOCKED: a short amber segment (<=450 ms) while outward motion stops;
    // then the calm blue wait the state itself provides.
    SequentialAnimation {
        id: blockedAnim
        NumberAnimation { target: amberSeg; property: "opacity"; to: 0.85; duration: 120 }
        PauseAnimation { duration: 110 }
        NumberAnimation { target: amberSeg; property: "opacity"; to: 0; duration: 220; easing.type: Easing.InCubic }
        ScriptAction { script: core.strong = "" }
    }
    // MODEL APPLIED: highlights separate in phase, the centre eases 6 %
    // down, the rings settle to a new highlight alignment, one blue retune
    // ring completes. ~1.1 s. Rotation angles are untouched.
    SequentialAnimation {
        id: modelAnim
        ParallelAnimation {
            SequentialAnimation {
                NumberAnimation { target: core; property: "jolt"; to: -3.2; duration: 180; easing.type: Easing.OutCubic }
                NumberAnimation { target: core; property: "jolt"; to: 0; duration: 620; easing.type: Easing.InOutCubic }
            }
            SequentialAnimation {
                NumberAnimation { target: core; property: "focusBoost"; to: -0.06; duration: 220; easing.type: Easing.InOutCubic }
                NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 700; easing.type: Easing.InOutCubic }
            }
            SequentialAnimation {
                ScriptAction { script: { core.sweepAngle = (core.sweepAngle + 14) % 360; if (core.environment) core.packetIn(1.05, 0, 520) } }
                PropertyAction { target: ringWave; property: "scale"; value: 0.3 }
                PropertyAction { target: ringWave; property: "opacity"; value: 0.7 }
                ParallelAnimation {
                    NumberAnimation { target: ringWave; property: "scale"; to: 0.98; duration: 1100; easing.type: Easing.InOutSine }
                    SequentialAnimation { PauseAnimation { duration: 500 } NumberAnimation { target: ringWave; property: "opacity"; to: 0; duration: 600 } }
                }
            }
        }
        ScriptAction { script: core.strong = "" }
    }
    // A model applied during execution: a small local acknowledgement only.
    SequentialAnimation {
        id: modelLocalAnim
        NumberAnimation { target: core; property: "focusBoost"; to: -0.06; duration: 160 }
        NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 400; easing.type: Easing.InOutCubic }
        ScriptAction { script: core.strong = "" }
    }
    // MESSAGE ACCEPTED: 90 ms of 1 % compression, a 360 ms blue wave, a
    // short transfer of light towards the lower connection. ~600 ms.
    SequentialAnimation {
        id: sentAnim
        NumberAnimation { target: core; property: "squeeze"; to: 0.99; duration: 90; easing.type: Easing.OutCubic }
        ParallelAnimation {
            NumberAnimation { target: core; property: "squeeze"; to: 1; duration: 260; easing.type: Easing.OutCubic }
            SequentialAnimation {
                PropertyAction { target: wave; property: "scale"; value: 0.2 }
                PropertyAction { target: wave; property: "opacity"; value: 0.7 }
                ParallelAnimation {
                    NumberAnimation { target: wave; property: "scale"; to: 0.62; duration: 360; easing.type: Easing.OutCubic }
                    NumberAnimation { target: wave; property: "opacity"; to: 0; duration: 360; easing.type: Easing.InCubic }
                }
            }
            ScriptAction { script: core.packetDown(320) }
        }
        PauseAnimation { duration: 150 }
        ScriptAction { script: core.strong = "" }
    }
    // NEW ACTIVITY: a packet on the near orbit comes in from the surroundings,
    // the focus answers for 120 ms. Low intensity, additive, at most one per 2 s.
    SequentialAnimation {
        id: activityAnim
        ScriptAction { script: core.packetIn(core.environment ? -0.53 : -0.42, -0.12, 380) }
        PauseAnimation { duration: 300 }
        NumberAnimation { target: core; property: "focusBoost"; to: 0.08; duration: 60 }
        NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 180; easing.type: Easing.InOutCubic }
    }
    // FOCUS RETURN: +4 % at the centre, a highlight pass along the rim, back in 400 ms.
    SequentialAnimation {
        id: focusAnim
        ParallelAnimation {
            NumberAnimation { target: core; property: "focusBoost"; to: 0.04; duration: 120; easing.type: Easing.OutCubic }
            NumberAnimation { target: rimPass; property: "opacity"; to: 0.9; duration: 120 }
        }
        ParallelAnimation {
            NumberAnimation { target: core; property: "focusBoost"; to: 0; duration: 400; easing.type: Easing.InOutCubic }
            NumberAnimation { target: rimPass; property: "rotation"; from: 0; to: 150; duration: 400; easing.type: Easing.InOutCubic }
            NumberAnimation { target: rimPass; property: "opacity"; to: 0; duration: 400; easing.type: Easing.InCubic }
        }
    }
    // The idle radial wave: low energy, gone within 900 ms.
    SequentialAnimation {
        id: ambientAnim
        PropertyAction { target: wave; property: "scale"; value: 0.25 }
        PropertyAction { target: wave; property: "opacity"; value: 0.28 }
        ParallelAnimation {
            NumberAnimation { target: wave; property: "scale"; to: 0.74; duration: 900; easing.type: Easing.OutCubic }
            NumberAnimation { target: wave; property: "opacity"; to: 0; duration: 900; easing.type: Easing.InCubic }
        }
    }
    // EXECUTION ENTRY: one light packet from the centre to the near header line.
    SequentialAnimation {
        id: launchAnim
        ScriptAction { script: if (core.environment) core.packetOut(0.56, 420) }
    }

    // ======================= the layer stack ==================================
    // Restrained halo: the level lifts it a little; it never becomes a fog.
    Image { source: "../assets/core-halo.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: (0.45 + 0.6 * (core.lvl - 0.22) + 0.3 * core.bootGlow) * (core.dormant ? 0.5 : 1) }
    // L1: the dark bed. Present from the first frame: the plate's globe never shows.
    Image { source: "../assets/core-bed.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true }
    // L8 (static part): fixed tick groups.
    Image { source: "../assets/core-ticks.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: (0.3 + 0.5 * core.bootRim) * (core.dormant ? 0.6 : 1) }
    // L3: the six-sector mechanical structure, and its light.
    Image { source: "../assets/core-sectors.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            opacity: 0.4 + 0.6 * core.bootRim }
    Repeater {
        model: 6
        Image {
            source: "../assets/core-sector-lit.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: index * 60
            readonly property real lit: index % 2 === 0 ? core.litA : core.litB
            // Listening lights only the opposite pair (sectors 0 and 3).
            visible: lit > 0.01 && (!core.listening || index === 0 || index === 3)
            opacity: lit
        }
    }
    // L5: the second ring - thinner, dimmer, counter-clockwise, its own segments.
    Image { id: secondRing; source: "../assets/core-ring-secondary.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: 37; opacity: (0.3 + 0.7 * core.bootRim) * (0.72 + 0.4 * (core.lvl - 0.22)) * (core.dormant ? 0.55 : 1) }
    // L4: the main ring - the most legible motion, clockwise.
    Image { id: mainRing; source: "../assets/core-ring-main.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: 0; opacity: (0.3 + 0.7 * core.bootRim) * Math.min(1, 0.86 + 0.5 * (core.lvl - 0.22)) * (core.dormant ? 0.55 : 1) }
    // L6: the energy sweep, a 28 degree highlight travelling over the rings.
    Image { id: sweep; source: "../assets/core-sweep.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true
            rotation: core.sweepAngle + core.jolt
            opacity: (0.5 + 1.0 * (core.lvl - 0.22)) * core.sweepGain * core.bootRim; visible: opacity > 0.01 }
    // L7: two energy points on the ring tracks (their carriers turn), and
    // one packet on the header's horizontal orbit.
    Item { id: markerA; anchors.centerIn: parent; width: core.s; height: width; rotation: 250
           Image { source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
                   x: core.c + core.s * 0.33 - width / 2; y: core.c - height / 2; opacity: core.pointA * core.bootRim } }
    Item { id: markerB; anchors.centerIn: parent; width: core.s; height: width; rotation: 95
           Image { source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true
                   x: core.c + core.s * 0.37 - width / 2; y: core.c - height / 2; opacity: 0.55 * core.bootRim } }
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
        opacity: 0.75 * core.bootRim * Math.max(0, Math.min(1, (core.orbitPhase - 78) / 14, (282 - core.orbitPhase) / 14))
    }
    // Semantic event arcs (Shapes): green on one small main-ring segment,
    // red or amber on one sector. Never the whole core.
    Shape { id: greenSeg; anchors.fill: parent; opacity: 0; visible: opacity > 0.01; preferredRendererType: Shape.CurveRenderer
            ShapePath { strokeColor: Theme.success; strokeWidth: Math.max(1.5, core.s * 0.007); fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.33; radiusY: core.s * 0.33; startAngle: mainRing.rotation + 22; sweepAngle: 16 } } }
    Shape { id: redSeg; anchors.fill: parent; opacity: 0; visible: opacity > 0.01; preferredRendererType: Shape.CurveRenderer
            ShapePath { strokeColor: Theme.alpha(Theme.danger, 0.7); strokeWidth: core.s * 0.05; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.27; radiusY: core.s * 0.27; startAngle: -23; sweepAngle: 46 } } }
    Shape { id: amberSeg; anchors.fill: parent; opacity: 0; visible: opacity > 0.01; preferredRendererType: Shape.CurveRenderer
            ShapePath { strokeColor: Theme.alpha(Theme.warning, 0.7); strokeWidth: core.s * 0.05; fillColor: "transparent"; capStyle: ShapePath.FlatCap
                PathAngleArc { centerX: core.c; centerY: core.c; radiusX: core.s * 0.27; radiusY: core.s * 0.27; startAngle: 97; sweepAngle: 46 } } }
    // Event light: the wave (scaled outward), the retune ring, the packet.
    Image { id: wave; source: "../assets/core-wave.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true; opacity: 0; visible: opacity > 0.01 }
    Image { id: ringWave; source: "../assets/core-wave.png"; anchors.centerIn: parent; width: core.s; height: width; mipmap: true; opacity: 0; visible: opacity > 0.01 }
    Image { id: packet; source: "../assets/core-dot.png"; width: core.s * 0.05; height: width; mipmap: true; opacity: 0; visible: opacity > 0.01 }

    // L2: the optical chamber and its nucleus, breathing in light, barely in size.
    Item {
        anchors.centerIn: parent; width: core.s * 0.5; height: width
        scale: (1 + 0.16 * (core.breath - 1)) * core.squeeze
        Image { source: "../assets/core-chamber.png"; anchors.fill: parent; mipmap: true }
        Image { id: chamberLight; source: "../assets/core-chamber-light.png"; anchors.fill: parent; mipmap: true; rotation: 40
                opacity: 0.85 * core.bootFocus * (core.dormant ? 0.4 : 1) }
        Image { source: "../assets/core-rim-hi.png"; anchors.fill: parent; mipmap: true
                opacity: (core.rimPulse * 0.8 + 0.2 * core.bootGlow) * core.bootRim * (core.dormant ? 0.4 : 1) }
        Image { id: rimPass; source: "../assets/core-rim-hi.png"; anchors.fill: parent; mipmap: true; opacity: 0; visible: opacity > 0.01 }
        Image { source: "../assets/core-focus-glow.png"; anchors.centerIn: parent; width: core.s * 0.4; height: width; mipmap: true
                opacity: Math.min(1, (0.34 + 1.1 * (core.lvl - 0.22) + core.focusBoost + 0.35 * core.bootGlow) * core.breath) * core.bootFocus }
        Image { source: "../assets/core-focus.png"; anchors.centerIn: parent; width: core.s * 0.2; height: width; mipmap: true
                opacity: Math.min(1, (0.9 + 0.9 * core.focusBoost + 0.3 * (core.lvl - 0.22)) * core.breath) * core.bootFocus * (core.dormant ? 0.55 : 1) }
    }
}
