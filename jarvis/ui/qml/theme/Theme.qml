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
    // Two bundled families (jarvis/ui/typography.py registers them; the
    // names are the ones Qt reports): IBM Plex Sans for the interface and the
    // conversation, Space Grotesk for the wordmark, the greeting and the
    // instrument numerals. `font` is the interface family every text uses.
    readonly property string sans: typeof typography !== "undefined" && typography.sans !== "" ? typography.sans : "Segoe UI"
    readonly property string display: typeof typography !== "undefined" && typography.display !== "" ? typography.display : sans
    readonly property string font: sans
    readonly property string mono: "Cascadia Mono"
    readonly property int fsMicro: 9
    readonly property int fsLabel: 11
    readonly property int fsMeta: 12
    readonly property int fsBody: 15
    readonly property int fsLead: 17
    readonly property int fsTitle: 20
    readonly property int fsHero: 34
    readonly property real labelSpacing: 2.4

    // -- typographic roles (1920x1080 baseline; size / line height / weight) --
    // Space Grotesk's 600 is its variable weight axis on the Medium face -
    // asking Qt for weight 600 directly would synthesise bold. Numerals that
    // must not jitter use the font's tabular figures (tnum).
    readonly property var semibold600: ({ "wght": 600 })
    readonly property var tabular: ({ "tnum": 1 })
    readonly property font brand: Qt.font({ family: display, pixelSize: 28, weight: Font.Medium, variableAxes: semibold600, letterSpacing: 3.92 })   // 28/34, +0.14 em
    readonly property font sectionHeading: Qt.font({ family: sans, pixelSize: 13, weight: Font.DemiBold, letterSpacing: 0.78 })                    // 13/18, +0.06 em
    readonly property font conversationBody: Qt.font({ family: sans, pixelSize: 18, weight: Font.Normal })                                        // 18/28
    readonly property font messageIdentity: Qt.font({ family: sans, pixelSize: 12, weight: Font.DemiBold })                                       // 12/16
    readonly property font uiLabel: Qt.font({ family: sans, pixelSize: 14, weight: Font.Medium })                                                 // 14/20
    readonly property font uiValue: Qt.font({ family: sans, pixelSize: 14, weight: Font.DemiBold })                                               // 14/20
    readonly property font metadata: Qt.font({ family: sans, pixelSize: 12, weight: Font.Normal })                                                // 12/18
    readonly property font timestamp: Qt.font({ family: display, pixelSize: 12, weight: Font.Medium, features: tabular })                         // 12/16
    readonly property font counter: Qt.font({ family: display, pixelSize: 16, weight: Font.Medium, variableAxes: semibold600, features: tabular }) // 16/20
    readonly property font uptimeValue: Qt.font({ family: display, pixelSize: 14, weight: Font.Medium, variableAxes: semibold600, features: tabular }) // 14/20
    readonly property font modelName: Qt.font({ family: sans, pixelSize: 18, weight: Font.DemiBold })                                             // 18/24
    readonly property font composerText: Qt.font({ family: sans, pixelSize: 18, weight: Font.Normal })                                            // 18/24
    readonly property font hudLabel: Qt.font({ family: sans, pixelSize: 11, weight: Font.DemiBold, letterSpacing: 1.32 })                              // 11/16, +0.12 em
    readonly property font dateText: Qt.font({ family: sans, pixelSize: 13, weight: Font.Medium })                                                // 13/18
    // A role with different tracking (e.g. a tight heading losing its +0.06 em).
    function tracked(f, spacing) { return Qt.font({ family: f.family, pixelSize: f.pixelSize, weight: f.weight, letterSpacing: spacing, variableAxes: f.variableAxes, features: f.features }) }
    // Role-specific responsive sizes. Tier 0 = 1920x1080, 1 = 1536x864, 2 = 1280x760.
    function tier(w) { return w >= 1728 ? 0 : w >= 1408 ? 1 : 2 }
    function greeting(t) { var s = [44, 38, 34][t]; return Qt.font({ family: display, pixelSize: s, weight: Font.Medium, variableAxes: semibold600, letterSpacing: -0.02 * s }) }
    function greetingLine(t) { return [56, 48, 44][t] }
    function greetingSupport(t) { return Qt.font({ family: sans, pixelSize: [20, 18, 18][t], weight: Font.Normal }) }
    function greetingSupportLine(t) { return [28, 26, 26][t] }
    function clockSize(t) { return [44, 40, 36][t] }
    function clockLine(t) { return [48, 44, 42][t] }
    function clock(px) { return Qt.font({ family: display, pixelSize: px, weight: Font.Medium, features: tabular }) }
    function railTitle(t) { return Qt.font({ family: sans, pixelSize: [18, 17, 16][t], weight: Font.DemiBold }) }
    function railTitleLine(t) { return [24, 22, 22][t] }
    function railCount(t) { return Qt.font({ family: display, pixelSize: [22, 20, 20][t], weight: Font.Medium, variableAxes: semibold600, features: tabular }) }
    function railCountLine(t) { return [28, 26, 26][t] }

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
    property color text: space ? "#eaf4ff" : "#dfebf2"            // primary
    property color textLabel: space ? "#acc2d8" : "#a5c0d1"       // secondary
    property color textMuted: space ? "#8faac2" : "#7792a5"       // metadata
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
    // Outcome unknown: a pale ice-blue structure - never amber (blocked /
    // approval), red (failed) or green (completed).
    readonly property color unknownEdge: "#a8dcff"
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
        case "running": return qsTr("REASONING / EXECUTING")
        case "awaiting_approval": return qsTr("AWAITING APPROVAL")
        case "awaiting_input": return qsTr("AWAITING YOUR ANSWER")
        case "completed": return qsTr("COMPLETED")
        case "error": return qsTr("ERROR")
        case "unknown": return qsTr("OUTCOME UNKNOWN")
        case "disabled": return qsTr("JARVIS FROZEN")
        default: return qsTr("IDLE")
        }
    }
}
