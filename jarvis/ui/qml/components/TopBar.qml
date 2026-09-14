import QtQuick
import QtQuick.Shapes
import "../theme"

// The canonical command header. Left: the identity mark (which is also the
// Settings trigger), the wordmark, and the ambient rail (date / time / local
// context) whose geometry comes from the Phase 1 macro map. Centre: the
// JARVIS core, the dominant element, with its lateral connector lines and
// the core-side status group. Right: the Option 5 command strip -
// selector, status instrument, kill switch, navigation grid, window
// controls - anchored to the right edge and bound to real state only.
Item {
    id: bar
    // Height, rail width and the viewport height come from the macro
    // layout (Main.qml); the rail's own rhythm is proportional to it.
    property real railWidth: 246
    property real viewportHeight: 1080
    // The rail's own left inset, proportional like the rail itself.
    readonly property real railInset: Theme.px(bar.width * 0.0163, 20, 34)
    signal menuRequested()
    signal killSwitchRequested()
    signal editRequested()
    signal minimizeRequested()
    signal restoreRequested()
    signal closeRequested()
    signal openSettings()
    signal openModel()
    property date now: new Date()
    property bool fullscreen: true
    property bool settingsOpen: false
    property bool modelOpen: false
    property Item controlLayer: null
    readonly property point settingsAnchor: Qt.point(orb.x - 4, orb.y + orb.height + 14)
    readonly property point modelAnchor: Qt.point(controls.x + modelChip.x, controls.y + modelChip.height + 16)
    readonly property point navAnchor: Qt.point(controls.x + navigationTrigger.x + navigationTrigger.width, controls.y + navigationTrigger.height + 18)
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
    readonly property real selectorWidth: tickerFits ? Math.min(Theme.chipWidth(width), selectorMax)
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
        Accessible.role: Accessible.Button; Accessible.name: "General Settings"
        Keys.onReturnPressed: bar.openSettings()
        Keys.onSpacePressed: bar.openSettings()
        Rectangle { anchors.fill: parent; anchors.margins: -5; radius: width / 2; color: "transparent"; border.color: Theme.accent; visible: orb.activeFocus }
        scale: orbArea.pressed ? 0.95 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
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
        // Outer calibrated ring with nodes.
        Item {
            anchors.fill: parent
            RotationAnimator on rotation { running: Theme.ambient && bar.settingsOpen; loops: Animation.Infinite; from: 0; to: 360; duration: 12000 }
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
        Rectangle {
            visible: orb.hovered && !bar.settingsOpen
            anchors.top: parent.bottom; anchors.topMargin: 10; anchors.horizontalCenter: parent.horizontalCenter
            width: tipText.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
            Text { id: tipText; anchors.centerIn: parent; text: "Settings"; color: Theme.text; font { family: Theme.font; pixelSize: 11 } }
        }
    }
    Text { objectName: "brandWordmark"; x: 120; y: 21; text: "JARVIS"; color: Theme.text; font: Theme.brand
           lineHeightMode: Text.FixedHeight; lineHeight: 34 }
    Text { objectName: "brandTagline"; x: 122; y: 55; text: "THINKS AHEAD FOR YOU"; color: Theme.textLabel; font: Theme.hudLabel
           lineHeightMode: Text.FixedHeight; lineHeight: 16 }

    LeftContextCard {
        visible: !Theme.command
        x: 10
        width: bar.railWidth - 20
        // The accepted summary rail stays fixed; leave a deliberate 12 px gap.
        height: Math.min(144, Math.round(bar.viewportHeight * 0.1980) + 76 - 12 - 84)
        y: Math.round(bar.viewportHeight * 0.1980) + 76 - 12 - height
        now: bar.now
    }

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
    // The compact vertical status group, immediately right of the core and
    // clearly subordinate to it. Only states the installation genuinely
    // reports are lit: REASONING while a request is being understood,
    // EXECUTING only while an approved execution or a resume is running.
    Item {
        id: ticker
        objectName: "coreStatusGroup"
        visible: bar.tickerFits
        x: bar.ticker0
        y: bar.coreCy - height / 2 - Math.round(bar.coreDiameter * 0.021)
        width: bar.tickerNeed; height: stack.height
        Column {
            id: stack
            width: parent.width; spacing: Math.round(bar.coreDiameter * 0.052)
            Repeater {
                model: ["LISTENING", "REASONING", "EXECUTING"]
                Text {
                    readonly property bool lit: (modelData === "LISTENING" && (bridge.coreState === "idle" || bridge.coreState === "awaiting_input"))
                                                || (modelData === "REASONING" && bridge.coreState === "running" && !bridge.executing)
                                                || (modelData === "EXECUTING" && bridge.coreState === "running" && bridge.executing)
                    text: modelData
                    color: lit ? "#eaf6ff" : Theme.textLabel
                    opacity: lit ? 1 : 0.7
                    font: Theme.hudLabel
                    Behavior on color { ColorAnimation { duration: 200 } }
                    Behavior on opacity { NumberAnimation { duration: 200 } }
                }
            }
        }
    }
    Text {
        visible: ticker.visible && bridge.coreState !== "idle"
        x: ticker.x; y: ticker.y + ticker.height + 12
        // Never under the selector: the caption stops short of the strip.
        width: Math.max(0, controls.x - x - 12); elide: Text.ElideRight
        text: Theme.stateLabel(bridge.coreState)
        color: Theme.stateColor(bridge.coreState)
        font: Theme.hudLabel
        Behavior on color { ColorAnimation { duration: 200 } }
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
            // Real state: the configured model, or the CLI's own default.
            modelName: bridge.config.claude_model ? bridge.config.claude_model : "CLI default"
            metadata: bar.selectorWidth >= 240 ? "ANTHROPIC  |  CLI" : "ANTHROPIC"
                      + (bridge.config.claude_effort ? "  |  " + String(bridge.config.claude_effort).toUpperCase() : "")
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
                lit: bridge.executionEnabled && !bridge.configRefused
                skin: bridge.statusIndicator
                Accessible.role: Accessible.Indicator
                Accessible.name: bridge.configRefused ? "Execution status: configuration refused" : (bridge.executionEnabled ? (bridge.busy ? "Execution status: running" : "Execution status: ready") : "Execution status: disabled")
                MouseArea { id: orbHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Rectangle {
                    visible: orbHover.containsMouse
                    anchors.top: parent.bottom; anchors.topMargin: 2; anchors.horizontalCenter: parent.horizontalCenter
                    width: orbTip.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
                    Text { id: orbTip; anchors.centerIn: parent; color: Theme.text; font { family: Theme.font; pixelSize: 11 }
                        text: bridge.configRefused ? "Configuration refused" : (bridge.executionEnabled ? (bridge.busy ? "Execution ready · running" : "Execution ready") : "Execution disabled") }
                }
            }
        }

        // The kill switch: a separate control with its own hit target,
        // deliberately smaller than the status instrument and unmistakably
        // an action rather than a state.
        Item {
            width: Theme.stripKill; height: Theme.stripHeight
            y: controls.mid - height / 2
            PowerControl {
                objectName: "killSwitchControl"
                anchors.centerIn: parent
                diameter: Theme.stripKill
                enabled: !bridge.configRefused
                engaged: !bridge.executionEnabled
                onClicked: bar.killSwitchRequested()
            }
        }

        IconButton {
            id: navigationTrigger
            objectName: "navigationTrigger"
            y: controls.mid - height / 2
            tip: "Navigation"; width: 38; height: 38; onClicked: bar.menuRequested()
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
            IconButton { glyph: "–"; tip: "Minimize"; width: 34; height: 34; onClicked: bar.minimizeRequested() }
            IconButton { glyph: bar.fullscreen ? "❐" : "▢"; tip: bar.fullscreen ? "Windowed" : "Full screen"; width: 34; height: 34; onClicked: bar.restoreRequested() }
            IconButton { glyph: "✕"; tip: "Close"; width: 34; height: 34; onClicked: bar.closeRequested() }
        }
    }

    // The tiny icon-only layout-edit control: a sliders glyph in a framed square.
    IconButton {
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight) + 8
                  topMargin: Math.round(bar.viewportHeight * 0.1130) }
        objectName: "editLayoutTrigger"
        tip: bridge.editMode ? "Done editing" : "Edit workspace layout"
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
