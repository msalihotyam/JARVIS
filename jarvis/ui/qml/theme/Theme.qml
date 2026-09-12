pragma Singleton
import QtQuick

// Presentation tokens. Layouts are independent, versioned UI preferences.
// Every color token carries a staged Behavior, so switching the theme is one
// coordinated ~400 ms recalibration rather than a hard cut: glow first, then
// background, then the core accent, then surfaces and borders, then text.
QtObject {
    id: theme

    property string name: "space"
    property bool ambient: true
    readonly property bool space: name === "space"
    readonly property bool command: name === "command"

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
    readonly property int radius: 11
    readonly property int radiusSmall: 8
    readonly property int radiusTiny: 6
    readonly property int gap: 15
    readonly property int pad: 18
    readonly property int margin: 30

    // -- stage 1: glow (0-120 ms) --------------------------------------
    property color glow: space ? "#3d8bff" : "#2a5fb0"
    Behavior on glow { ColorAnimation { duration: 120; easing.type: Easing.InOutCubic } }
    property real glowStrength: space ? 0.8 : command ? 0.6 : 0.18
    Behavior on glowStrength { NumberAnimation { duration: 120; easing.type: Easing.InOutCubic } }

    // -- stage 2: background (60-360 ms) --------------------------------
    property color bg0: space ? "#020910" : command ? "#020c13" : "#030709"
    property color bg1: space ? "#031222" : command ? "#04131e" : "#060c0f"
    property color horizon: space ? "#08192e" : command ? "#061b29" : "#030809"
    Behavior on bg0 { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    Behavior on bg1 { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    Behavior on horizon { SequentialAnimation { PauseAnimation { duration: 60 } ColorAnimation { duration: 300; easing.type: Easing.InOutCubic } } }
    property real starOpacity: space ? 0.65 : 0.04
    Behavior on starOpacity { SequentialAnimation { PauseAnimation { duration: 60 } NumberAnimation { duration: 300; easing.type: Easing.InOutCubic } } }

    // -- stage 3: core energy / accent (120-300 ms) ---------------------
    property color accent: space ? "#20c7f6" : command ? "#00d4ff" : "#1bbaff"
    property color accentSoft: space ? "#398fd0" : "#2b86c7"
    property color accentDeep: space ? "#007bbd" : "#0076ae"
    Behavior on accent { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }
    Behavior on accentSoft { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }
    Behavior on accentDeep { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }

    // -- stage 4: surfaces and borders (180-400 ms) ---------------------
    // Material levels: 0 environment (bg*), 1 workspace glass, 2 panel glass,
    // 3 interactive / raised, wells (surfaceDeep) inside any of them.
    property color surfaceDeep: space ? "#60020a10" : "#6803090c"
    property color surfaceWorkspace: space ? "#94030d16" : command ? "#70021019" : "#ac03080a"
    property color surface: space ? "#b105141e" : command ? "#ba041722" : "#c7071014"
    property color surfaceRaised: space ? "#fd081926" : command ? "#fd061b29" : "#fd091318"
    property color sheen: space ? "#099acef4" : "#087fa8bd"
    property color border: space ? "#42377b9c" : command ? "#56419abb" : "#38384952"
    property color borderStrong: space ? "#8057b6e4" : "#705094ac"
    property color hud: space ? "#48b8f4" : "#318cab"
    property real hudOpacity: space ? 0.37 : command ? 0.5 : 0.22
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
    property color text: space ? "#daeaf5" : "#dfebf2"
    property color textLabel: space ? "#a9c5da" : "#a5c0d1"
    property color textMuted: space ? "#7899b4" : "#7792a5"
    property color textDim: space ? "#526f85" : "#506776"
    Behavior on text { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textLabel { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textMuted { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }
    Behavior on textDim { SequentialAnimation { PauseAnimation { duration: 260 } ColorAnimation { duration: 160; easing.type: Easing.InOutCubic } } }

    // -- semantic state colors (fixed across themes) --------------------
    readonly property color success: "#00d8ad"
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
