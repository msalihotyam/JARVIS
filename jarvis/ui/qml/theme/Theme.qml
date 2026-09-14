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

    // -- macro geometry ---------------------------------------------------
    // The Canonical Space composition, normalized to the viewport and
    // measured from the approved 1536x864 reference. Canonical and Dark
    // share this family; the Command preset keeps its own outer geometry,
    // so nothing here is consumed on that path.
    readonly property real macroRail: 0.1283           // ambient rail width = central surface left edge
    readonly property real macroStackRight: 0.9805     // right stack right edge
    readonly property real macroGap: 0.0104            // column gap (20 px at 1920)
    readonly property real macroTopZone: 0.1898        // identity / core zone height = panels begin
    // The conversation shell rises above that line into the header
    // environment: its material starts at 0.1556 of the height and is fully
    // present by 0.2000, where the text begins (168 / 216 px at 1080). The
    // right stack keeps the panel line; nothing else moves.
    readonly property real macroShellTop: 0.1556
    readonly property real macroShellText: 0.2000
    readonly property real macroCentralBottom: 0.9514  // central surface bottom
    readonly property real macroStackBottom: 0.9213    // right stack bottom
    readonly property real macroStackGap: 0.030        // gap between stacked panels, of stack height
    readonly property real macroCoreX: 0.4890          // core centre
    readonly property real macroCoreY: 0.0799
    readonly property real macroCoreSize: 0.1580       // core diameter, of viewport width (reference 15.4)
    readonly property real macroStripRight: 0.0143     // command-strip right margin
    readonly property real macroChip: 0.1583           // model selector envelope (304 px at 1920)
    readonly property real macroHudLine: 0.1725         // header linework baseline, of viewport height
    // The rail shrinks before the work surface does, and never disappears.
    function px(v, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, v))) }
    function railWidth(w) { return px(w * macroRail, 132, 300) }
    function outerRight(w) { return px(w * (1 - macroStackRight), 18, 48) }
    function topZone(h) { return px(h * macroTopZone, 150, 240) }
    function centralBottom(h) { return px(h * (1 - macroCentralBottom), 30, 70) }
    function stackInset(h) { return px(h * (macroCentralBottom - macroStackBottom), 16, 44) }
    function columnGap(w) { return px(w * macroGap, 12, 26) }
    function stackGap(stackHeight) { return px(stackHeight * macroStackGap, 10, 30) }
    function coreSize(w) { return px(w * macroCoreSize, 175, 330) }
    function chipWidth(w) { return px(w * macroChip, 188, 324) }
    function stripSpacing(w) { return px(w * 0.0073, 8, 16) }
    function hudLine(h) { return px(h * macroHudLine, 132, 218) }
    function shellLift(h) { return px(h * (macroTopZone - macroShellTop), 20, 44) }
    function shellFade(h) { return px(h * (macroShellText - macroShellTop), 26, 56) }

    // -- command strip (Option 5 "Command") -------------------------------
    // Element heights are part of the frozen control language, so they are
    // fixed at the 1920x1080 contract; only the envelope is proportional.
    readonly property int stripHeight: 56
    readonly property int stripSelectorHeight: 54
    readonly property int stripStatus: 64               // status instrument, clearly below the core
    readonly property int stripKill: 50                 // kill switch: separate, a step below the status
    readonly property int stripChamfer: 18              // Option 5 corner cut
    // Option 5 frame tones: layered dark glass with a fine cyan perimeter.
    property color commandEdge: space ? "#24d8ff" : command ? "#24d8ff" : "#1fbde0"
    property color commandEdgeDim: space ? "#1d4d6b" : "#193f57"
    property color commandGlass0: space ? "#f00d2230" : "#f00b1a22"
    property color commandGlass1: space ? "#f0060f1a" : "#f0050d12"
    Behavior on commandEdge { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }
    Behavior on commandEdgeDim { SequentialAnimation { PauseAnimation { duration: 120 } ColorAnimation { duration: 180; easing.type: Easing.InOutCubic } } }

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
    // Structural Phase 3 glass only: never a success/status/module identity.
    readonly property color materialBlue: "#36aaff"
    readonly property color materialGlint: "#9dd8ff"
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

    // -- Global Glass: the material tiers ---------------------------------
    // Values are approximate EFFECTIVE navy coverage over the background
    // once layers combine: primary shell ~54 % with reading islands ~88 %;
    // secondary shell ~66 % with data regions ~86 %; interactive ~89 %;
    // menus ~97 %; micro ~24 %. Text, icons and focus rings are never
    // faded with a parent: they stay opaque on their own. With ambient
    // effects off (the reduced-transparency path) the shells close up and
    // the reflections go; geometry never changes.
    readonly property color glassPrimaryShell: ambient ? "#8a040f1e" : "#e6040f1e"
    readonly property color glassPrimaryReading: "#e0031020"
    readonly property color glassUserReading: "#e0041b31"           // subtly bluer
    readonly property color glassSecondaryShell: ambient ? "#a8040f1e" : "#ea040f1e"
    readonly property color glassSecondaryReading: "#99051324"      // inside a secondary shell: ~86 % combined
    readonly property color glassInteractive: "#e3061424"
    readonly property color glassInteractiveTop: "#e30a2038"
    readonly property color glassMenu: "#f7061424"
    readonly property color glassMicro: "#3d1a4f88"
    readonly property color glassEdge: "#80278dce"                  // the ~1 px structural perimeter
    readonly property color glassEdgeStrong: "#c049bdfa"
    readonly property color glassSeparator: "#38278dce"
    readonly property color glassReflection: ambient ? "#129dd8ff" : "#009dd8ff"       // ~5 % upper-edge highlight
    readonly property color glassInnerHighlight: ambient ? "#2449bdfa" : "#0049bdfa"

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
