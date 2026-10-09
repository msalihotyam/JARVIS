import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// The canonical command header. Left: the identity mark (which is also the
// Settings trigger), the wordmark, and the ambient rail (date / time / local
// context) whose geometry comes from the Phase 1 macro map. Centre: the
// JARVIS core, the dominant element, with its lateral connector lines and
// the core-side status group. Right: the Option 5 command strip -
// selector, status instrument, Freeze control, navigation grid, window
// controls - anchored to the right edge and bound to real state only.
Item {
    id: bar
    // Height, rail width and the viewport height come from the macro
    // layout (Main.qml); the rail's own rhythm is proportional to it.
    property real railWidth: 246
    // Gate 6F: the home sidebar's right edge (0 when there is none); the
    // lifecycle indicator starts after it.
    property real sideRight: 0
    property real viewportHeight: 1080
    // The rail's own left inset, proportional like the rail itself.
    readonly property real railInset: Theme.px(bar.width * 0.0163, 20, 34)
    signal menuRequested()
    signal lifecycleRequested()      // the red control: Freeze / Resume JARVIS
    signal editRequested()
    signal minimizeRequested()
    signal restoreRequested()
    signal closeRequested()
    signal openSettings()
    signal openModel()
    property bool fullscreen: true
    property bool settingsOpen: false
    property bool modelOpen: false
    property Item controlLayer: null
    readonly property point settingsAnchor: Qt.point(orb.x - 4, orb.y + orb.height + 14)
    readonly property point modelAnchor: Qt.point(controls.x + modelChip.x, controls.y + modelChip.height + 16)
    readonly property point killAnchor: Qt.point(controls.x + killSlot.x + killSlot.width, controls.y + controls.height + 22)
    readonly property point navAnchor:Qt.point(controls.x + navigationTrigger.x + navigationTrigger.width, controls.y + navigationTrigger.height + 18)
    readonly property Item navTrigger: navigationTrigger        // K4: the nav menu's opener and toggle
    // Degradation, in the required order: decorative linework goes first,
    // then secondary metadata, then micro spacing - never hit targets and
    // never the authoritative controls.
    readonly property bool narrow: width < 1360         // decorative linework
    readonly property bool compact: width < 1420        // metadata / spacing
    // The core keeps the reference's centre and a proportional diameter at
    // every width: it is the primary identity anchor, never repositioned
    // out of the composition.
    readonly property real coreCx: Theme.command ? width * 0.42 : width * Theme.macroCoreX
    readonly property real coreCy: Theme.command ? 60 : bar.viewportHeight * Theme.macroCoreY
    readonly property real coreDiameter: Theme.command ? 162 : Theme.coreSize(width)
    readonly property real coreEdge: coreCx + coreDiameter / 2
    // The strip is anchored right and the selector takes what is left, but
    // the core-side status group is reserved space first: the selector is
    // only trimmed while it stays at a usable width, and below that the
    // group (secondary metadata) is what gives way - never the controls.
    readonly property real stripGap: compact ? 8 : 12
    readonly property real tickerNeed: 88
    readonly property real ticker0: coreEdge + 20
    readonly property real stripFixed: Theme.stripStatus + Theme.stripKill + 38 + 13 + 106 + 5 * stripGap
    readonly property real stripRoom: width - Math.round(width * Theme.macroStripRight)
                                      - (ticker0 + tickerNeed + 10)
    readonly property real selectorMax: stripRoom - stripFixed
    readonly property bool tickerFits: !Theme.command && selectorMax >= 188
    // The card carries the plan summary on its right when a provider reported
    // one, so it is allowed that much more width - never at the cost of the
    // controls, and never past the room the strip really has.
    readonly property real selectorExtra: ((bridge.modelCard || {}).quota || []).length > 0 ? 150 : 0
    readonly property real selectorWidth: tickerFits ? Math.min(Theme.chipWidth(width) + selectorExtra, selectorMax)
                                                     : Theme.chipWidth(width)

    // -- header linework ------------------------------------------------------
    // The reference runs one fine cyan line across the header, broken around
    // the core, with an angled return on the right and a second line closing
    // the identity zone at the boundary the panels start from.
    Shape {
        id: hudLines
        anchors.fill: parent
        opacity: Theme.hudOpacity * (bar.narrow ? 0.8 : 1)
        preferredRendererType: Shape.CurveRenderer
        readonly property real yLine: Theme.command ? 100 : Theme.hudLine(bar.viewportHeight)
        readonly property real yRight: Theme.command ? 90 : Math.round(hudLines.yLine * 0.84)
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.width * 0.245; startY: hudLines.yLine + 22
            PathLine { x: bar.width * 0.265; y: hudLines.yLine }
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.56; y: hudLines.yLine }
        }
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.coreCx + bar.coreDiameter * 0.56; startY: hudLines.yLine
            PathLine { x: bar.width * 0.655; y: hudLines.yLine }
            PathLine { x: bar.width * 0.685; y: hudLines.yRight }
            PathLine { x: bar.width * 0.93; y: hudLines.yRight }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.width * 0.31; startY: hudLines.yLine + 7
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.62; y: hudLines.yLine + 7 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.coreCx + bar.coreDiameter * 0.82; startY: hudLines.yLine + 7
            PathLine { x: bar.width * 0.62; y: hudLines.yLine + 7 }
        }
    }
    Repeater {
        model: 4
        Rectangle { visible: !bar.narrow; x: bar.width * (0.74 + index * 0.05); y: hudLines.yRight - 4; width: 1; height: 9; color: Theme.hud; opacity: Theme.hudOpacity * 1.2 }
    }
    Rectangle { visible: !bar.narrow; x: bar.width * 0.245 - 2.5; y: hudLines.yLine + 22 - 2.5; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    Rectangle { visible: !bar.narrow; x: bar.width * 0.93 - 2.5; y: hudLines.yRight - 2.5; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    // The boundary the panels start from: one restrained full-width line.
    Rectangle {
        visible: !Theme.command
        x: 0; y: bar.height - 1; width: bar.width; height: 1
        color: Theme.hud; opacity: Theme.hudOpacity * 0.7
    }

    // Lateral connector traces at the core's own height: the reference ties
    // the core into the header with fine lines, a dash and one node.
    Shape {
        id: coreTrace
        anchors.fill: parent
        visible: !bar.narrow && !Theme.command
        opacity: Theme.hudOpacity
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            startX: bar.coreCx - bar.coreDiameter * 0.52 - 116; startY: bar.coreCy
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.52; y: bar.coreCy }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.5); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [2, 5]
            startX: bar.coreCx - bar.coreDiameter * 0.52 - 182; startY: bar.coreCy
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.52 - 124; y: bar.coreCy }
        }
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            startX: bar.coreCx + bar.coreDiameter * 0.51; startY: bar.coreCy
            PathLine { x: bar.ticker0 - 7; y: bar.coreCy }
        }
    }
    Rectangle {
        visible: coreTrace.visible
        x: bar.coreCx - bar.coreDiameter * 0.52 - 118; y: bar.coreCy - 2.5
        width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 }
    }

    // Trajectory linework: a wide, shallow ellipse whose centre sits left
    // of the core, with a dashed companion inside it - the reference's
    // flattened trajectory, not a circle around the core. Measured from the
    // canonical reference: centre 0.292 core-diameters left of the core,
    // rx 0.699 d, ry 0.123 d (5.7:1). Its right end runs in behind the ring
    // family, which draws over it.
    Shape {
        id: orbit
        visible: !bar.narrow
        anchors.fill: parent
        opacity: Theme.hudOpacity * 0.8
        preferredRendererType: Shape.CurveRenderer
        readonly property real cx: bar.coreCx - bar.coreDiameter * 0.292
        readonly property real cy: bar.coreCy
        readonly property real rx: bar.coreDiameter * 0.699
        readonly property real ry: bar.coreDiameter * 0.123
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: orbit.cx; centerY: orbit.cy
                           radiusX: orbit.rx; radiusY: orbit.ry
                           startAngle: 78; sweepAngle: 204 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.6); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 5]
            PathAngleArc { centerX: orbit.cx; centerY: orbit.cy
                           radiusX: bar.coreDiameter * 0.627; radiusY: bar.coreDiameter * 0.088
                           startAngle: 92; sweepAngle: 176 }
        }
    }
    // One tracking node, on the ellipse at its leftmost vertex.
    Rectangle {
        visible: !bar.narrow
        width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        x: orbit.cx + orbit.rx * Math.cos(175 * Math.PI / 180) - 2.5
        y: orbit.cy + orbit.ry * Math.sin(175 * Math.PI / 180) - 2.5
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 }
    }

    // -- brand: the identity mark IS the Settings trigger ---------------------
    Item {
        id: orb
        parent: bar.controlLayer || bar
        objectName: "settingsOrb"
        x: 28; y: 16; width: 62; height: 62
        readonly property bool hovered: orbArea.containsMouse
        readonly property bool lit: hovered || bar.settingsOpen
        activeFocusOnTab: true
        Accessible.role: Accessible.Button; Accessible.name: qsTr("General Settings")
        Keys.onReturnPressed: bar.openSettings()
        Keys.onSpacePressed: bar.openSettings()
        Rectangle { anchors.fill: parent; anchors.margins: -5; radius: width / 2; color: "transparent"; border.color: Theme.accent; visible: orb.activeFocus }
        scale: orbArea.pressed ? 0.95 : 1
        Behavior on scale { NumberAnimation { duration: Theme.pressMs; easing.type: Theme.ease } }
        // Soft bloom, as in the reference mark: a gradient, so it has no edge.
        Shape {
            anchors.centerIn: parent; width: 106; height: 106
            preferredRendererType: Shape.CurveRenderer
            opacity: orb.lit ? 1 : 0.72
            Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
            ShapePath {
                strokeWidth: -1
                fillGradient: RadialGradient {
                    centerX: 53; centerY: 53; centerRadius: 53; focalX: 53; focalY: 53
                    GradientStop { position: 0.00; color: Theme.alpha(Theme.accent, 0.34) }
                    GradientStop { position: 0.42; color: Theme.alpha(Theme.accent, 0.15) }
                    GradientStop { position: 0.72; color: Theme.alpha(Theme.accent, 0.04) }
                    GradientStop { position: 1.00; color: "transparent" }
                }
                PathAngleArc { centerX: 53; centerY: 53; radiusX: 53; radiusY: 53; startAngle: 0; sweepAngle: 360 }
            }
        }
        // Outer calibrated ring with nodes: static (K2, it never spins).
        Item {
            anchors.fill: parent
            Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: Theme.accent; opacity: orb.lit ? 0.9 : 0.45; Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
            Repeater { model: 12
                Rectangle { width: index % 3 === 0 ? 3 : 1; height: 3; radius: index % 3 === 0 ? 1.5 : 0; color: Theme.accent; opacity: orb.lit ? 0.95 : 0.6; x: orb.width / 2 - width / 2; y: index % 3 === 0 ? -1 : 2
                    transform: Rotation { origin.x: width / 2; origin.y: orb.height / 2 - y; angle: index * 30 } } }
        }
        // The luminous mark: a thick broken cyan ring around a dark centre.
        Rectangle { anchors.centerIn: parent; width: 46; height: 46; radius: 23; color: Theme.bg0; opacity: 0.9 }
        Shape {
            anchors.centerIn: parent; width: 46; height: 46
            preferredRendererType: Shape.CurveRenderer
            opacity: orb.lit ? 1 : 0.92
            ShapePath { strokeColor: Theme.alpha(Theme.accent, 0.26); strokeWidth: 8; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 350; sweepAngle: 150 } }
            ShapePath { strokeColor: Theme.accent; strokeWidth: 3.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 350; sweepAngle: 150 } }
            ShapePath { strokeColor: Theme.accent; strokeWidth: 3.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 166; sweepAngle: 122 } }
            ShapePath { strokeColor: Qt.lighter(Theme.accent, 1.5); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 30; sweepAngle: 54 } }
        }
        Rectangle { anchors.centerIn: parent; width: 24; height: 24; radius: 12; color: Theme.bg0; opacity: 0.92 }
        Rectangle { anchors.centerIn: parent; width: 34; height: 34; radius: 17; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.3) }
        MouseArea { id: orbArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bar.openSettings() }
        HoverTip { text: qsTr("Settings"); active: !bar.settingsOpen; offsetX: (width - tipWidth) / 2; offsetY: height + 10 }
    }
    Text { id: wordmark; objectName: "brandWordmark"; x: 120; y: 21; text: "JARVIS"; color: Theme.text; font: Theme.brand
           lineHeightMode: Text.FixedHeight; lineHeight: 34 }
    Text { id: tagline; objectName: "brandTagline"; x: 122; y: 55; text: qsTr("THINKS AHEAD FOR YOU"); color: Theme.textLabel; font: Theme.hudLabel
           lineHeightMode: Text.FixedHeight; lineHeight: 16 }

    // -- core + core-side status ----------------------------------------------
    JarvisCore {
        id: core
        objectName: "jarvisCore"
        x: bar.coreCx - width / 2
        y: bar.coreCy - height / 2
        width: bar.coreDiameter; height: width
        state_: bridge.coreState
        active: Window.active
        environment: !bar.narrow && !Theme.command
        Behavior on width { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
    }
    // The core's companion: ONE complete label, immediately right of the core
    // and clearly subordinate to it, for where the installation STANDS - an
    // outcome (completed, error, outcome unknown) or the frozen safety state.
    // What is in progress - preparing, executing, waiting for an approval or an
    // answer - is said once, in the conversation, on the live turn it belongs
    // to (FeedView), never a second time here; the core's own light still shows
    // every one of those states. Idle says nothing: readiness is not claimed,
    // and no voice or listening is implied anywhere. It wraps whole words and
    // is never cut short.
    Item {
        id: ticker
        objectName: "coreStatusGroup"
        readonly property bool inProgress: bridge.coreState === "running" || bridge.coreState === "awaiting_approval"
                                          || bridge.coreState === "awaiting_input"
        readonly property string label: inProgress ? "" : Theme.stateLabel(bridge.coreState)
        visible: bar.tickerFits && label !== ""
        x: bar.ticker0
        y: bar.coreCy - height / 2
        // Never under the selector: the label stops short of the strip.
        width: Math.max(bar.tickerNeed, Math.min(240, controls.x - x - 12)); height: companion.height
        Text {
            id: companion
            objectName: "coreStatusLabel"
            width: parent.width
            text: ticker.label
            wrapMode: Text.WordWrap; maximumLineCount: 2
            color: bridge.coreState === "running" ? Theme.materialGlint : Theme.stateColor(bridge.coreState)
            font: Theme.caption
            lineHeightMode: Text.FixedHeight; lineHeight: 18
            Behavior on color { ColorAnimation { duration: Theme.dur(200) } }
        }
    }

    // -- lifecycle progress ------------------------------------------------------
    // The current request's four stages, quiet in the header between the
    // wordmark and the core: filled when done, lit with a faint halo for the
    // current stage, hollow and dim for what is still ahead. Projected by the
    // bridge (bridge.progress) from authoritative state and shown only while
    // there is real work - never a ghost at idle. No chrome, no motion.
    Item {
        id: progress
        objectName: "hudProgress"
        readonly property var stages: bridge.progress.stages || []
        // Ends just short of the core; reaches toward the rail, at most 360 px.
        readonly property real endX: bar.coreCx - bar.coreDiameter * 0.5 - bar.width * 0.02
        // It starts after the rail (on home: after the sidebar), or yields
        // when that leaves too little.
        readonly property real startX: Math.max(bar.railWidth, bar.sideRight) + 24
        visible: bridge.progress.active === true && stages.length === 4 && width >= 240
        width: Math.max(0, Math.min(360, endX - startX)); height: 50
        x: endX - width
        y: bar.coreCy + Math.round(bar.viewportHeight * 0.03) - 6
        readonly property real pitch: width / 4
        Repeater {
            model: 3
            Rectangle { x: progress.pitch * (index + 0.5) + 10; y: 5.5; width: progress.pitch - 20; height: 1
                        color: progress.stages[index] === "done" ? Theme.alpha(Theme.accent, 0.5) : Theme.alpha(Theme.hud, 0.3) }
        }
        Repeater {
            model: 4
            Item {
                id: step
                objectName: "hudStage_" + index
                x: progress.pitch * index; width: progress.pitch; height: parent.height
                readonly property string s: progress.stages[index] || "pending"
                readonly property bool current: ["running", "waiting", "error", "unknown", "denied"].indexOf(s) >= 0
                readonly property color tone: s === "waiting" ? Theme.warning : s === "unknown" ? Theme.unknownEdge
                                              : s === "error" || s === "denied" ? Theme.danger : Theme.accent
                Accessible.role: Accessible.StaticText
                Accessible.name: label.text + ": " + ({ pending: qsTr("Pending"), waiting: qsTr("Awaiting you"), running: qsTr("Running"), done: qsTr("Completed"),
                                                        error: qsTr("Failed"), unknown: qsTr("Outcome unknown"), denied: qsTr("Denied"), skipped: qsTr("Not run") })[s]
                Rectangle { visible: step.current; x: (parent.width - width) / 2; y: -4; width: 20; height: 20; radius: 10; color: Theme.alpha(step.tone, 0.18) }
                Rectangle {
                    objectName: "hudStageNode_" + index
                    x: (parent.width - width) / 2; y: 0; width: 12; height: 12; radius: 6
                    color: step.s === "done" ? Theme.alpha(Theme.accent, 0.8) : step.current ? step.tone : "transparent"
                    border.width: step.s === "done" || step.current ? 0 : 1.5
                    border.color: Theme.alpha(Theme.textLabel, 0.5)
                }
                Text {
                    id: label
                    x: -5; y: 19; width: parent.width + 10
                    horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                    text: [qsTr("Understanding your request"), qsTr("Authorization"), qsTr("Execution"), qsTr("Outcome")][index]
                    color: step.current ? step.tone : step.s === "done" ? Theme.text : Theme.textLabel
                    opacity: step.current || step.s === "done" ? 1 : 0.75
                    font: Theme.metadata; lineHeightMode: Text.FixedHeight; lineHeight: 15
                }
            }
        }
    }

    // -- the command strip ----------------------------------------------------
    // One horizontal command assembly: selector, status instrument, kill
    // switch, navigation grid, then the window chrome behind a divider. The
    // right edge is anchored; the selector takes the space it needs to the
    // left. Every element reads real state and nothing here duplicates it.
    Row {
        id: controls
        objectName: "commandStrip"
        parent: bar.controlLayer || bar
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight); topMargin: 22 }
        height: Theme.stripHeight
        spacing: bar.stripGap
        readonly property real mid: Theme.stripHeight / 2

        ModelSelector {
            id: modelChip
            objectName: "modelTrigger"
            y: controls.mid - height / 2
            width: bar.selectorWidth; height: Theme.stripSelectorHeight
            open: bar.modelOpen
            // One shape for every provider: the SHORT model name, then
            // BRAND | EFFORT, then the plan summary - all real reported state
            // (bridge.modelCard). The canonical model id is unchanged by this.
            readonly property var card: bridge.modelCard || ({})
            // Claude with nothing configured really is "whatever the CLI picks";
            // Codex has no default at all, so there the truth is "no model chosen".
            modelName: card.name ? card.name : card.provider === "claude" ? qsTr("CLI default") : qsTr("No model chosen")
            metadata: card.meta || ""
            quota: card.quota || []
            onActivated: bar.openModel()
        }

        // Execution state: the glass lamp, at instrument scale. It indicates
        // only - the Kill Switch beside it is the control.
        Item {
            width: Theme.stripStatus; height: Theme.stripHeight
            y: controls.mid - height / 2
            StatusOrb {
                id: statusOrb
                objectName: "statusOrb"
                anchors.centerIn: parent
                diameter: Theme.stripStatus
                // An unconfirmed change never shows the reassuring enabled glow.
                lit: bridge.executionState === "running"
                skin: bridge.statusIndicator
                Accessible.role: Accessible.Indicator
                Accessible.name: bridge.executionState === "unconfirmed" ? qsTr("JARVIS status: unconfirmed") : bridge.configRefused ? qsTr("JARVIS status: configuration refused") : bridge.executionState === "frozen" ? qsTr("JARVIS status: frozen") : bridge.executionState === "freeze_requested" ? qsTr("JARVIS status: freeze requested") : (bridge.busy ? qsTr("JARVIS status: running an operation") : qsTr("JARVIS status: ready"))
                MouseArea { id: orbHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Rectangle {
                    visible: orbHover.containsMouse
                    anchors.top: parent.bottom; anchors.topMargin: 2; anchors.horizontalCenter: parent.horizontalCenter
                    width: orbTip.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
                    Text { id: orbTip; anchors.centerIn: parent; color: Theme.text; font: Theme.caption
                        text: bridge.executionState === "unconfirmed" ? qsTr("JARVIS state unconfirmed") : bridge.configRefused ? qsTr("Configuration refused") : bridge.executionState === "frozen" ? qsTr("JARVIS frozen") : bridge.executionState === "freeze_requested" ? qsTr("Freeze requested") : (bridge.busy ? qsTr("Running · operation in progress") : qsTr("Running")) }
                }
            }
        }

        // The kill switch: a separate control with its own hit target,
        // deliberately smaller than the status instrument and unmistakably
        // an action rather than a state.
        Item {
            id: killSlot
            width: Theme.stripKill; height: Theme.stripHeight
            y: controls.mid - height / 2
            PowerControl {
                id: killControl
                objectName: "killSwitchControl"
                anchors.centerIn: parent
                diameter: Theme.stripKill
                enabled: !bridge.configRefused
                // Latched only when the store confirmed execution is off.
                engaged: bridge.executionState === "frozen"
                onClicked: bar.lifecycleRequested()
            }
            // Its meaning, in words: Freeze / Resume JARVIS - not close, not quit, not cancel.
            // A micro-label under the control; it does not move the strip.
            Text {
                objectName: "killSwitchLabel"
                anchors { horizontalCenter: killControl.horizontalCenter; top: killControl.bottom; topMargin: 5 }
                horizontalAlignment: Text.AlignHCenter
                text: ({ frozen: qsTr("FROZEN"), freeze_requested: qsTr("FREEZING"), resuming: qsTr("RESUMING"), unconfirmed: qsTr("STATE\nUNCONFIRMED"), refused: qsTr("FROZEN") })[bridge.executionState] || qsTr("FREEZE")
                color: bridge.executionState === "running" || bridge.executionState === "unknown" ? Theme.textLabel : Theme.danger
                font: Theme.label
                lineHeight: 0.95
            }
        }

        IconButton {
            id: navigationTrigger
            objectName: "navigationTrigger"
            y: controls.mid - height / 2
            tip: qsTr("Navigation"); width: 38; height: 38; onClicked: bar.menuRequested()
            Grid { anchors.centerIn: parent; columns: 2; spacing: 4
                Repeater { model: 4; Rectangle { width: 6; height: 6; radius: index === 3 ? 1 : 3; color: "transparent"; border.width: 1.4; border.color: Theme.accent } } }
        }

        // The window chrome sits behind a divider and stays restrained, so
        // it never competes with the JARVIS controls.
        Item {
            width: 13; height: Theme.stripHeight
            y: controls.mid - height / 2
            Rectangle { anchors.centerIn: parent; width: 1; height: 26; color: Theme.borderStrong; opacity: 0.8 }
        }
        Row {
            y: controls.mid - height / 2
            spacing: 2
            IconButton { icon: "minimize"; tip: qsTr("Minimize"); width: 34; height: 34; onClicked: bar.minimizeRequested() }
            IconButton { icon: bar.fullscreen ? "restore" : "maximize"; tip: bar.fullscreen ? qsTr("Windowed") : qsTr("Full screen"); width: 34; height: 34; onClicked: bar.restoreRequested() }
            IconButton { objectName: "windowCloseControl"; icon: "close"; tip: bridge.trayAvailable ? qsTr("Hide to tray - JARVIS keeps running") : qsTr("Close"); width: 34; height: 34; onClicked: bar.closeRequested() }
        }
    }

    // The tiny icon-only layout-edit control: a sliders glyph in a framed square.
    IconButton {
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight) + 8
                  topMargin: Math.round(bar.viewportHeight * 0.1130) }
        objectName: "editLayoutTrigger"
        tip: bridge.editMode ? qsTr("Done editing") : qsTr("Edit workspace layout")
        active: bridge.editMode
        framed: true
        width: 34; height: 34
        onClicked: bar.editRequested()
        Item { anchors.centerIn: parent; width: 16; height: 12
            property color c: bridge.editMode ? Theme.accent : Theme.accent
            Rectangle { x: 0; y: 2; width: 16; height: 1.2; color: parent.c }   Rectangle { x: 9; y: 0; width: 5; height: 5; radius: 2.5; color: Theme.bg0; border.width: 1.2; border.color: parent.c }
            Rectangle { x: 0; y: 10; width: 16; height: 1.2; color: parent.c }  Rectangle { x: 3; y: 8; width: 5; height: 5; radius: 2.5; color: Theme.bg0; border.width: 1.2; border.color: parent.c }
        }
    }
}
