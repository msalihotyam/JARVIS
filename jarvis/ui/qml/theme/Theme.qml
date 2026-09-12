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
    readonly property int hoverMs: 140
    readonly property int ease: Easing.OutCubic
    readonly property int easeInOut: Easing.InOutCubic

    // -- typography -----------------------------------------------------
    // System faces only: nothing here depends on a font that may be absent.
    // BRAND 25 tracked / HUD 10-11 tracked caps / TITLE 34 / BODY 15 / META 12 / STATUS 10-11.
    readonly property string font: "Segoe UI"
    readonly property string mono: "Cascadia Mono"
    readonly property int fsMicro: 9
    readonly property int fsLabel: 11
    readonly property int fsMeta: 12
    readonly property int fsBody: 15
    readonly property int fsLead: 17
    readonly property int fsTitle: 20
    readonly property int fsHero: 34
    readonly property real labelSpacing: 2.4

    // -- geometry (1920x1080 contract, scaled by the layout) -------------
    readonly property int radius: 16
    readonly property int radiusSmall: 10
    readonly property int radiusTiny: 6
    readonly property int gap: 15
    readonly property int pad: 18
    readonly property int margin: 30

    // -- stage 1: glow (0-120 ms) --------------------------------------
    property color glow: space ? "#3d8bff" : "#2a5fb0"
    Behavior on glow { ColorAnimation { duration: 120; easing.type: Easing.InOutCubic } }
    property real glowStrength: space ? 1.0 : 0.5
    Behavior on glowStrength { NumberAnimation { duration: 120; easing.type: Easing.InOutCubic } }

    // -- stage 2: background (60-360 ms) --------------------------------
    property color bg0: space ? "#03060f" : "#050608"
    property color bg1: space ? "#071228" : "#0a0d13"
    property color horizon: space ? "#0c2050" : "#0c121c"
    Behavior on bg0 { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    Behavior on bg1 { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    Behavior on horizon { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    property real starOpacity: space ? 0.9 : 0.22
    Behavior on starOpacity { SequentialAnimation { PauseAnimation { duration: 60 } NumberAnimation { duration: 300; easing.type: Easing.InOutCubic } } }

    // -- stage 3: core energy / accent (120-300 ms) ---------------------
    property color accent: space ? "#4fd8ff" : "#3ab4e0"
    property color accentSoft: space ? "#5d9dff" : "#4d86c8"
    property color accentDeep: space ? "#1e8fd0" : "#1d6f9a"
    Behavior on accent { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }
    Behavior on accentSoft { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }
    Behavior on accentDeep { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }

    // -- stage 4: surfaces and borders (180-400 ms) ---------------------
    // Material levels: 0 environment (bg*), 1 workspace glass, 2 panel glass,
    // 3 interactive / raised, wells (surfaceDeep) inside any of them.
    property color surfaceDeep: space ? "#66040a1a" : "#a0050709"
    property color surfaceWorkspace: space ? "#8a060d20" : "#c8080b11"
    property color surface: space ? "#b40a1530" : "#d80a0e15"
    property color surfaceRaised: space ? "#cc0e1c3c" : "#ee10161f"
    property color sheen: space ? "#1c9fd8ff" : "#10a0c8e0"
    property color border: space ? "#3c4fd8ff" : "#2e4a7f9a"
    property color borderStrong: space ? "#8a4fd8ff" : "#66408aa8"
    property color hud: space ? "#4fd8ff" : "#3a90b0"
    property real hudOpacity: space ? 0.5 : 0.3
    Behavior on surfaceDeep { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on surfaceWorkspace { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on surface { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on surfaceRaised { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on sheen { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on border { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on borderStrong { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on hud { SequentialAnimation { PauseAnimation { duration: 180 } ColorAnimation { duration: 220; easing.type: Easing.InOutCubic } } }
    Behavior on hudOpacity { SequentialAnimation { PauseAnimation { duration: 180 } NumberAnimation { duration: 220; easing.type: Easing.InOutCubic } } }

    // -- stage 5: text and state accents (260-420 ms) -------------------
    property color text: space ? "#eef4ff" : "#e3e9f0"
    property color textLabel: space ? "#b7c7e6" : "#a3aebd"
    property color textMuted: space ? "#8ea3c9" : "#7f8b9a"
    property color textDim: space ? "#5c7096" : "#4f5a68"
    Behavior on text { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textLabel { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textMuted { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textDim { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }

    // -- semantic state colors (fixed across themes) --------------------
    readonly property color success: "#3ddc97"
    readonly property color warning: "#ffb347"
    readonly property color danger: "#ff5c6c"
    readonly property color unknown: "#ffd166"
    readonly property color unknownEdge: "#ff8a3d"
    readonly property color info: "#4fd8ff"
    readonly property color violet: "#9b7bff"
    readonly property color teal: "#35d0b0"

    // A color at a given alpha; the one helper every surface uses.
    function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

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
