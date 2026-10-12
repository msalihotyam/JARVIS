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

    // -- motion contract (Gate 6G K2) -------------------------------------
    // Motion only on a real state or transition: 120-220 ms, OutCubic, no
    // bounce, at most `travel` px. ONE gate: every duration in the QML is
    // dur(ms) or a token below built from it, so ambient effects off (reduced
    // motion) is zero motion everywhere - the same states and colours, shown
    // at once. The documented exceptions (the JarvisCore choreography, state
    // rhythms, the starfield) still pass through dur(); tests/test_motion_6g.py
    // holds the list. Known, not ours: the stock Basic ScrollBar's own hold +
    // fade (Qt style code) is outside this gate.
    function dur(ms) { return ambient ? ms : 0 }
    readonly property int travel: ambient ? 4 : 0
    readonly property int panelIn: dur(180)
    readonly property int panelOut: dur(150)
    readonly property int reflow: dur(220)
    readonly property int pageMs: dur(220)
    readonly property int approvalIn: dur(220)
    readonly property int focusMs: dur(220)
    readonly property int themeMs: dur(220)
    readonly property int hoverMs: dur(120)
    readonly property int pressMs: dur(120)
    readonly property int attachMs: dur(150)
    // NOT motion, so NOT gated by ambient: a destructive dialog's confirm (and
    // Enter) arm this long after it opens, so the second click of the gesture
    // that opened it can never land on confirm.
    readonly property int confirmGuardMs: 300
    readonly property int ease: Easing.OutCubic
    readonly property int easeInOut: Easing.InOutCubic

    // -- typography -----------------------------------------------------
    // ONE interface face and one small ramp; every text takes its type from a
    // role here, never from a size of its own (jarvis/ui/typography.py resolves
    // the faces; the names are the ones Qt reports).
    //   `ui` / `uiStrong` - the interface face and its REAL semibold: Segoe UI
    //       Variable on Windows 11, Segoe UI on Windows 10, the bundled IBM Plex
    //       Sans elsewhere. The conversation, the composer, labels, rows,
    //       captions and headings are all this face. A semibold is always
    //       `uiStrong` at `uiStrongWeight`: asking the regular family for 600
    //       lands on its Bold instance on Segoe UI Variable.
    //   `display` - Space Grotesk, the identity only: wordmark, greeting, clock
    //       and large instrument numerals. Never running text or small print.
    //   `mono` - a real fixed-pitch face for code and technical literals.
    // How text is rasterized: native glyphs from the font engine (Qt's FreeType
    // under the T default, grayscale; jarvis/ui/app.py). Nothing here is scaled or rotated.
    readonly property int textRender: Text.NativeRendering
    readonly property bool faces: typeof typography !== "undefined" && typography.ui !== ""
    readonly property string ui: faces ? typography.ui : "Segoe UI"
    readonly property string uiStrong: faces ? typography.uiStrong : "Segoe UI"
    readonly property int uiStrongWeight: faces ? typography.uiStrongWeight : Font.DemiBold
    // DFFONT: under FreeType the interface face is the registered Segoe UI
    // Variable file, its weight and optical size on axes. The regular is drawn
    // at 500 - the weight of the conversation text - because a 400 stem at
    // 12-15 px is about one pixel wide and grayscale spreads it into grey; the
    // semibold stays 600, the optical size is DirectWrite's Text cut (10.5).
    readonly property bool uiAxes: faces && typography.uiVariable === true
    readonly property int uiRegularWeight: uiAxes ? 500 : Font.Normal
    readonly property string sans: ui
    readonly property string reading: ui
    readonly property string font: ui
    readonly property string display: typeof typography !== "undefined" && typography.display !== "" ? typography.display : ui
    readonly property string mono: typeof typography !== "undefined" && typography.mono !== "" ? typography.mono : "Cascadia Mono"
    readonly property var tabular: ({ "tnum": 1 })
    // `snapped` (the 12 px tracked label only): full hinting under FreeType, so
    // the stems of each tracked capital land on whole pixels instead of the
    // fractional phases the tracking gives them.
    function face(px, strong, spacing, figures, snapped) {
        var weight = strong ? uiStrongWeight : uiRegularWeight
        var spec = { family: strong ? uiStrong : ui, pixelSize: px, weight: weight,
                     letterSpacing: spacing || 0, features: figures ? tabular : ({}) }
        if (uiAxes) spec.variableAxes = { "wght": weight, "opsz": 10.5 }
        if (uiAxes && snapped) spec.hintingPreference = Font.PreferFullHinting
        return Qt.font(spec)
    }

    // -- the ramp (1920x1080 baseline) ------------------------------------
    // Informational text is never below 13 px, and small text is never a
    // hairline: hierarchy comes from size steps, the semibold and the ink,
    // not from shrinking. 12 px exists only as the tracked uppercase label.
    readonly property font label: face(12, true, 0.7, false, true)   // UPPERCASE section / column labels
    readonly property font caption: face(13, false)            // metadata, timestamps, helper, row sub-lines, actions
    readonly property font captionStrong: face(13, true)       // a caption that names something (a chip, a counter's label)
    readonly property font captionFigures: face(13, false, 0, true)   // a caption count or time (tabular)
    readonly property font body: face(14, false)               // interface text, values, descriptions
    readonly property font bodyStrong: face(14, true)          // row titles, buttons, emphasised values
    readonly property font lead: face(15, false)               // an explanatory sentence on a card
    readonly property font subtitle: face(16, true)            // a panel / empty-state title
    readonly property font title: face(18, true)               // a card title
    readonly property font titleLarge: face(22, true)          // an open panel's heading
    readonly property font pageHeading: face(14, true, 1.1)    // a full page's UPPERCASE heading
    // A FULL PAGE (Tasks, History, Memory, Automations) is read at a desk, from
    // the whole screen: the same interface face, one clear step larger than the
    // dense side panels - hierarchy by size and ink, never by another family.
    readonly property font pageTitle: face(19, true, 1.3)      // the page's own UPPERCASE title
    readonly property font pageSection: face(14, true, 0.8)    // an UPPERCASE column / section label
    readonly property font pageRow: face(15, false)            // a list row's primary line, running content
    readonly property font pageRowStrong: face(15, true)       // a row's name
    readonly property font pageMeta: face(14, false)           // a row's second line, helper text
    readonly property font pageMetaFigures: face(14, false, 0, true)
    readonly property font pageCode: Qt.font({ family: mono, pixelSize: 14 })
    // Large instrument numerals keep the identity face.
    readonly property var semibold600: ({ "wght": 600 })
    function figure(px) { return Qt.font({ family: display, pixelSize: px, weight: Font.Medium, variableAxes: semibold600, features: tabular }) }

    // -- named roles on the ramp -----------------------------------------
    readonly property font brand: Qt.font({ family: display, pixelSize: 28, weight: Font.Medium, variableAxes: semibold600, letterSpacing: 3.92 })   // 28/34, +0.14 em
    // A panel's UPPERCASE heading in the right stack: a step under the page heading.
    readonly property font sectionHeading: face(13, true, 0.8)
    // The primary reading surface: 16/26 with a 12 px paragraph gap. Headings,
    // list rhythm and inline code inside a rendered reply are character
    // formats, so they live in jarvis/ui/typography.py.
    // THE CONVERSATION'S OWN VOICE (and nothing outside the conversation):
    // Manrope, read at Medium - a full, modern stroke that never goes thin on
    // the dark glass - with its real SemiBold for what names something. The
    // bundled file is what resolves; `ui` only stands in if it failed to load.
    readonly property string chat: typeof typography !== "undefined" && typography.chat !== "" ? typography.chat : ui
    function chatFace(px, strong, figures) {
        return Qt.font({ family: chat, pixelSize: px, weight: strong ? Font.DemiBold : Font.Medium,
                         features: figures ? tabular : ({}) })
    }
    readonly property font conversationBody: chatFace(16, false)
    readonly property int conversationLine: 26
    readonly property int conversationParagraph: 12
    // What the person wrote, inside its bounded block: the same face and size,
    // a tighter line - the block, not a heavier weight, makes it theirs.
    readonly property font conversationUser: chatFace(16, false)
    readonly property int conversationUserLine: 24
    // The compact layout's message text: the same voice, one step smaller.
    readonly property font conversationCompact: chatFace(15, false)
    // "JARVIS" above a reply: the identity face at a real 600 on its weight axis.
    readonly property font chatAuthor: Qt.font({ family: display, pixelSize: 14, weight: Font.Medium, variableAxes: semibold600, letterSpacing: 0.9 })
    // Beneath a reply: time, duration, tokens and the actions - subordinate by
    // size and ink, never by a weakened stroke.
    readonly property font chatMeta: chatFace(13, false, true)
    readonly property font chatSourcesHeading: chatFace(14, true)
    readonly property font chatSource: chatFace(13, false)
    // The live line of the turn being answered: what JARVIS is doing, and for how long.
    readonly property font chatStatus: chatFace(14, false, true)
    readonly property font metadataStrong: caption
    readonly property font eventTitle: bodyStrong
    readonly property font eventMeta: caption
    readonly property int eventMetaLine: 18
    readonly property font cardBody: lead
    readonly property font messageIdentity: label
    readonly property font uiLabel: body
    readonly property font uiValue: bodyStrong
    readonly property font metadata: caption
    readonly property font timestamp: face(13, false, 0, true)
    readonly property font counter: face(16, true, 0, true)
    readonly property font uptimeValue: face(14, true, 0, true)
    readonly property font modelName: title
    // What the person is writing, in the face their message will be read in:
    // the typed text and the placeholder are ONE font; the placeholder differs
    // only by ink (`textMuted`).
    readonly property font composerText: face(16, false)
    // The invitation in the empty composer - the placeholder ONLY - is Inter
    // Medium: clearly legible at rest, never thin, never heavy. Typed text and
    // every composer control keep the interface face.
    readonly property string placeholderFamily: typeof typography !== "undefined" && typography.placeholder !== "" ? typography.placeholder : ui
    readonly property font composerPlaceholder: Qt.font({ family: placeholderFamily, pixelSize: 16, weight: Font.Medium })
    readonly property font composerChip: captionStrong
    readonly property font hudLabel: label
    readonly property font dateText: caption
    readonly property font code: Qt.font({ family: mono, pixelSize: 13 })
    // A role with different tracking (e.g. a tight heading losing its tracking).
    function tracked(f, spacing) { return Qt.font({ family: f.family, pixelSize: f.pixelSize, weight: f.weight, letterSpacing: spacing, variableAxes: f.variableAxes, features: f.features }) }
    // Role-specific responsive sizes. Tier 0 = 1920x1080, 1 = 1536x864, 2 = 1280x760.
    function tier(w) { return w >= 1728 ? 0 : w >= 1408 ? 1 : 2 }
    function greeting(t) { var s = [44, 38, 34][t]; return Qt.font({ family: display, pixelSize: s, weight: Font.Medium, variableAxes: semibold600, letterSpacing: -0.02 * s }) }
    function greetingLine(t) { return [56, 48, 44][t] }
    function greetingSupport(t) { return face([20, 18, 18][t], false) }
    function greetingSupportLine(t) { return [28, 26, 26][t] }
    function clockSize(t) { return [44, 40, 36][t] }
    function clockLine(t) { return [48, 44, 42][t] }
    function clock(px) { return Qt.font({ family: display, pixelSize: px, weight: Font.Medium, features: tabular }) }
    function railTitle(t) { return face([18, 17, 16][t], true) }
    function railTitleLine(t) { return [24, 22, 22][t] }
    function railCount(t) { return figure([22, 20, 20][t]) }
    function railCountLine(t) { return [28, 26, 26][t] }

    // Legacy size tokens, on the ramp.
    readonly property int fsMicro: 12
    readonly property int fsLabel: 12
    readonly property int fsMeta: 13
    readonly property int fsBody: 14
    readonly property int fsLead: 16
    readonly property int fsTitle: 20
    readonly property int fsHero: 34
    readonly property real labelSpacing: 0.7

    // -- macro geometry ---------------------------------------------------
    // The Canonical Space composition, normalized to the viewport and
    // measured from the approved 1536x864 reference. Canonical and Dark
    // share this family; the Command preset keeps its own outer geometry,
    // so nothing here is consumed on that path.
    readonly property real macroRail: 0.1283           // ambient rail width = central surface left edge
    readonly property real macroStackRight: 0.9805     // right stack right edge
    readonly property real macroGap: 0.0104            // column gap (20 px at 1920)
    readonly property real macroTopZone: 0.1898        // identity / core zone height = panels begin
    readonly property real macroCentralBottom: 0.9514  // central surface bottom
    readonly property real macroStackBottom: 0.9213    // right stack bottom
    readonly property real macroStackGap: 0.030        // gap between stacked panels, of stack height
    readonly property real macroCoreX: 0.4890          // core centre
    readonly property real macroCoreY: 0.0799
    readonly property real macroCoreSize: 0.1580       // core diameter, of viewport width (reference 15.4)
    readonly property real macroStripRight: 0.0143     // command-strip right margin
    readonly property real macroHudLine: 0.1725         // header linework baseline, of viewport height
    // The rail shrinks before the work surface does, and never disappears.
    function px(v, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, v))) }
    function railWidth(w) { return px(w * macroRail, 132, 300) }
    // Gate 6F: the home sidebar (mockup: 340 at 1920, 256 below 1600).
    function sidebarWidth(w) { return px(w * 0.1771, 256, 340) }
    function outerRight(w) { return px(w * (1 - macroStackRight), 18, 48) }
    function topZone(h) { return px(h * macroTopZone, 150, 240) }
    function centralBottom(h) { return px(h * (1 - macroCentralBottom), 30, 70) }
    function stackInset(h) { return px(h * (macroCentralBottom - macroStackBottom), 16, 44) }
    function columnGap(w) { return px(w * macroGap, 12, 26) }
    function stackGap(stackHeight) { return px(stackHeight * macroStackGap, 10, 30) }
    function coreSize(w) { return px(w * macroCoreSize, 175, 330) }
    function stripSpacing(w) { return px(w * 0.0073, 8, 16) }
    function hudLine(h) { return px(h * macroHudLine, 132, 218) }

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
    Behavior on commandEdge { SequentialAnimation { PauseAnimation { duration: theme.dur(120) } ColorAnimation { duration: theme.dur(180); easing.type: Easing.InOutCubic } } }
    Behavior on commandEdgeDim { SequentialAnimation { PauseAnimation { duration: theme.dur(120) } ColorAnimation { duration: theme.dur(180); easing.type: Easing.InOutCubic } } }

    // -- geometry (1920x1080 contract, scaled by the layout) -------------
    readonly property int radius: 11
    readonly property int radiusSmall: 8
    readonly property int radiusTiny: 6
    readonly property int gap: 15
    readonly property int pad: 18
    readonly property int margin: 30
    // Gate 6E shared sizes: the TopBar scope pills (selector, switcher, new),
    // the menu rows (selector, switcher, context menu), the History / Projects
    // list rows; the dialog scrim (attach, delete, purge: the approval
    // overlay's strength) and the hover tip delay (as PowerControl's).
    readonly property int pillHeight: 30
    readonly property int rowHeight: 40
    readonly property int menuRowHeight: 36
    readonly property real scrimOpacity: 0.56
    readonly property int tipDelay: 500
    // Gate 6G K4: the one menu + dialog style. Every popup menu (MenuSurface)
    // and every dialog has this corner; a menu row's (MenuRow) states are these.
    readonly property int popupRadius: 10
    readonly property color menuRowHover: alpha(text, 0.07)
    readonly property color menuRowPressed: alpha(text, 0.12)
    readonly property color menuRowCurrent: alpha(accent, 0.14)
    readonly property real menuRowDisabled: 0.45

    // -- stage 1: glow (0-120 ms) --------------------------------------
    property color glow: space ? "#3d8bff" : "#2a5fb0"
    Behavior on glow { ColorAnimation { duration: theme.dur(120); easing.type: Easing.InOutCubic } }
    property real glowStrength: space ? 0.8 : command ? 0.6 : 0.18
    Behavior on glowStrength { NumberAnimation { duration: theme.dur(120); easing.type: Easing.InOutCubic } }

    // -- stage 2: background (60-280 ms) --------------------------------
    property color bg0: space ? "#020910" : command ? "#020c13" : "#030709"
    property color bg1: space ? "#031222" : command ? "#04131e" : "#060c0f"
    property color horizon: space ? "#08192e" : command ? "#061b29" : "#030809"
    Behavior on bg0 { SequentialAnimation { PauseAnimation { duration: theme.dur(60) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on bg1 { SequentialAnimation { PauseAnimation { duration: theme.dur(60) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on horizon { SequentialAnimation { PauseAnimation { duration: theme.dur(60) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    property real starOpacity: space ? 0.65 : 0.04
    Behavior on starOpacity { SequentialAnimation { PauseAnimation { duration: theme.dur(60) } NumberAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }

    // -- stage 3: core energy / accent (120-300 ms) ---------------------
    property color accent: space ? "#20c7f6" : command ? "#00d4ff" : "#1bbaff"
    property color accentSoft: space ? "#398fd0" : "#2b86c7"
    property color accentDeep: space ? "#007bbd" : "#0076ae"
    // Structural Phase 3 glass only: never a success/status/module identity.
    readonly property color materialBlue: "#36aaff"
    readonly property color materialGlint: "#9dd8ff"
    Behavior on accent { SequentialAnimation { PauseAnimation { duration: theme.dur(120) } ColorAnimation { duration: theme.dur(180); easing.type: Easing.InOutCubic } } }
    Behavior on accentSoft { SequentialAnimation { PauseAnimation { duration: theme.dur(120) } ColorAnimation { duration: theme.dur(180); easing.type: Easing.InOutCubic } } }
    Behavior on accentDeep { SequentialAnimation { PauseAnimation { duration: theme.dur(120) } ColorAnimation { duration: theme.dur(180); easing.type: Easing.InOutCubic } } }

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
    Behavior on surfaceDeep { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on surfaceWorkspace { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on surface { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on surfaceRaised { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on sheen { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on border { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on borderStrong { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on hud { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } ColorAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }
    Behavior on hudOpacity { SequentialAnimation { PauseAnimation { duration: theme.dur(180) } NumberAnimation { duration: theme.dur(220); easing.type: Easing.InOutCubic } } }

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
    // The edit menu: a lighter, translucent glass (about 88 %), text at full contrast.
    readonly property color glassMenuVeil: "#e0061424"
    readonly property color glassMicro: "#3d1a4f88"
    readonly property color glassEdge: "#80278dce"                  // the ~1 px structural perimeter
    readonly property color glassEdgeStrong: "#c049bdfa"
    readonly property color glassSeparator: "#38278dce"
    readonly property color glassReflection: ambient ? "#129dd8ff" : "#009dd8ff"       // ~5 % upper-edge highlight
    readonly property color glassInnerHighlight: ambient ? "#2449bdfa" : "#0049bdfa"

    // -- interactive glass: the three states of a control's BACKGROUND -----
    // Only the surface changes. Text, icons, borders and focus rings keep
    // their own colour at full strength, so nothing a person reads or aims
    // at ever fades with the state. The darkest state (active / focused) is
    // exactly the old fixed value, so no state is heavier than before.
    readonly property color glassControlIdle: ambient ? "#80061424" : "#d6061424"
    readonly property color glassControlIdleTop: ambient ? "#800a2038" : "#d60a2038"
    readonly property color glassControlHover: ambient ? "#b4061424" : "#dd061424"
    readonly property color glassControlHoverTop: ambient ? "#b40a2038" : "#dd0a2038"
    readonly property color glassControlActive: glassInteractive
    readonly property color glassControlActiveTop: glassInteractiveTop

    // -- the three visual distances ---------------------------------------
    // Environment (the shells above), workspace (reading islands) and the
    // FOREGROUND: an open panel, a preview, a menu. A foreground surface is
    // one material for all of them - deep navy at ~95 %, one fine edge, a
    // small dark shadow - so nothing behind it stays readable through it.
    readonly property color glassOverlay: ambient ? "#f2061424" : "#fa061424"
    readonly property color glassOverlayEdge: "#993d9be0"
    // A MENU over content (Gate 6F F5, P2): the same navy, fully opaque - a
    // 95 % body let the tile text behind it show through.
    readonly property color menuSurface: Qt.rgba(0x06 / 255, 0x14 / 255, 0x24 / 255, 1)
    // A nested control inside a card: a small tonal step from its parent,
    // never another framed sheet.
    readonly property color glassNested: "#0aeaf4ff"
    readonly property color glassNestedEdge: "#0eeaf4ff"
    // The reading veil behind an ordinary JARVIS reply: dark close to the
    // text and feathered outward (assets/reading-veil.png), so the planet
    // never competes with the answer and no card silhouette appears. This is
    // the veil's strength; the asset carries the falloff.
    readonly property real glassReadingVeil: ambient ? 0.6 : 0.82

    // -- stage 5: text and state accents (260-420 ms) -------------------
    // Resolved colours, never an opacity: a faded glyph rasterizes thinner on
    // Windows. Primary is clear without being white; each step is a distinct,
    // still-readable tone with the blue kept to a hint.
    property color text: space ? "#e1eaf3" : "#dde6ec"            // primary
    property color textLabel: space ? "#b8c6d4" : "#b2c1cb"       // secondary: helper text, row sub-lines
    property color textMuted: space ? "#98aabb" : "#93a4b0"       // metadata, placeholder, timestamps
    readonly property color eventMetaInk: textLabel                // a live event's secondary lines
    property color textDim: space ? "#6f8294" : command ? "#6b7d89" : "#71838f"   // tertiary / disabled; >= 4.5:1 on every glass (G6DARK)
    Behavior on text { SequentialAnimation { PauseAnimation { duration: theme.dur(260) } ColorAnimation { duration: theme.dur(160); easing.type: Easing.InOutCubic } } }
    Behavior on textLabel { SequentialAnimation { PauseAnimation { duration: theme.dur(260) } ColorAnimation { duration: theme.dur(160); easing.type: Easing.InOutCubic } } }
    Behavior on textMuted { SequentialAnimation { PauseAnimation { duration: theme.dur(260) } ColorAnimation { duration: theme.dur(160); easing.type: Easing.InOutCubic } } }
    Behavior on textDim { SequentialAnimation { PauseAnimation { duration: theme.dur(260) } ColorAnimation { duration: theme.dur(160); easing.type: Easing.InOutCubic } } }

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
    // One complete, sentence-case label per reported core state; the header
    // shows it whole. Idle says nothing (there is no readiness to claim), and
    // a running request is worded by what it is doing (TopBar).
    function stateLabel(state) {
        switch (state) {
        case "running": return qsTr("Preparing the answer")
        case "awaiting_approval": return qsTr("Awaiting your approval")
        case "awaiting_input": return qsTr("Awaiting your answer")
        case "completed": return qsTr("Completed")
        case "error": return qsTr("Error")
        case "unknown": return qsTr("Outcome unknown")
        case "disabled": return qsTr("JARVIS frozen")
        default: return ""
        }
    }
}
