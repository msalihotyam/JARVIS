pragma Singleton
import QtQuick

// The design tokens. Exactly two themes: "space" (canonical) and "dark".
// Every color token carries a staged Behavior, so switching the theme is one
// coordinated ~400 ms recalibration rather than a hard cut: glow first, then
// background, then the core accent, then surfaces and borders, then text.
QtObject {
    id: theme

    property string name: "space"
    property bool ambient: true
    readonly property bool space: name === "space"

    // -- motion contract ------------------------------------------------
    readonly property int panelIn: 220
    readonly property int panelOut: 180
    readonly property int reflow: 220
    readonly property int pageMs: 220
    readonly property int approvalIn: 260
    readonly property int approvalOut: 200
    readonly property int editMs: 200
    readonly property int focusMs: 280
    readonly property int themeMs: 400
    readonly property int ease: Easing.OutCubic
    readonly property int easeInOut: Easing.InOutCubic

    // -- typography -----------------------------------------------------
    readonly property string font: "Segoe UI"
    readonly property string mono: "Cascadia Mono"
    readonly property int fsLabel: 11
    readonly property int fsBody: 14
    readonly property int fsTitle: 20
    readonly property int fsHero: 30
    readonly property real labelSpacing: 2.2

    // -- geometry -------------------------------------------------------
    readonly property int radius: 14
    readonly property int radiusSmall: 8
    readonly property int gap: 18
    readonly property int pad: 18

    // -- stage 1: glow (0-120 ms) --------------------------------------
    property color glow: space ? "#5a2cc7ff" : "#3a2aa7d8"
    Behavior on glow { ColorAnimation { duration: 120; easing.type: Easing.InOutCubic } }
    property real glowStrength: space ? 1.0 : 0.55
    Behavior on glowStrength { NumberAnimation { duration: 120; easing.type: Easing.InOutCubic } }

    // -- stage 2: background (60-360 ms) --------------------------------
    property color bg0: space ? "#040812" : "#050608"
    property color bg1: space ? "#0b1630" : "#0b0e14"
    property color horizon: space ? "#122a5c" : "#0d1420"
    Behavior on bg0 { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    Behavior on bg1 { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    Behavior on horizon { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    property real starOpacity: space ? 1.0 : 0.25
    Behavior on starOpacity { SequentialAnimation { PauseAnimation { duration: 60 } NumberAnimation { duration: 300; easing.type: Easing.InOutCubic } } }

    // -- stage 3: core energy / accent (120-300 ms) ---------------------
    property color accent: space ? "#4fd8ff" : "#3ab4e0"
    property color accentSoft: space ? "#5d9dff" : "#4d86c8"
    Behavior on accent { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }
    Behavior on accentSoft { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }

    // -- stage 4: surfaces and borders (180-400 ms) ---------------------
    property color surface: space ? "#8c0a1428" : "#d4090b10"
    property color surfaceRaised: space ? "#b40f1d3a" : "#ee10141b"
    property color border: space ? "#5533a0d8" : "#36284f66"
    property color borderStrong: space ? "#9040c8ff" : "#663a8fb4"
    property color hud: space ? "#2a3fc4ff" : "#12244f70"
    Behavior on surface { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on surfaceRaised { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on border { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on borderStrong { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on hud { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }

    // -- stage 5: text and state accents (260-420 ms) -------------------
    property color text: space ? "#e8f1ff" : "#dfe6ee"
    property color textMuted: space ? "#8ea3c7" : "#7f8b9a"
    property color textDim: space ? "#5a6f96" : "#556070"
    Behavior on text { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textMuted { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textDim { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }

    // -- semantic state colors (fixed across themes) --------------------
    readonly property color success: "#3ddc97"
    readonly property color warning: "#ffb347"
    readonly property color danger: "#ff5c6c"
    readonly property color unknown: "#ffd166"
    readonly property color unknownEdge: "#ff8a3d"
    readonly property color info: "#4fd8ff"

    // The color a core state speaks in.
    function stateColor(state) {
        switch (state) {
        case "running": return accent
        case "awaiting_approval": return warning
        case "awaiting_input": return accentSoft
        case "completed": return success
        case "error": return danger
        case "unknown": return unknownEdge
        case "disabled": return textDim
        default: return accent
        }
    }
    function stateLabel(state) {
        switch (state) {
        case "running": return "REASONING / EXECUTING"
        case "awaiting_approval": return "AWAITING APPROVAL"
        case "awaiting_input": return "AWAITING YOUR ANSWER"
        case "completed": return "COMPLETED"
        case "error": return "ERROR"
        case "unknown": return "OUTCOME UNKNOWN"
        case "disabled": return "EXECUTION DISABLED"
        default: return "IDLE"
        }
    }
}
