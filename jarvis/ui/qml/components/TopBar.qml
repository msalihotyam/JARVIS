import QtQuick
import QtQuick.Shapes
import "../theme"

// The top of the canonical composition (1920x1080 contract, x positions
// proportional to width): brand with the orb as the Settings trigger,
// date / time, the local-context block, the core at (0.484 w, 85) with its
// orbit and HUD circuit lines, the state ticker to its right, compact
// model / execution controls, window controls, the layout-edit control.
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
    property string clock: ""
    property string meridiem: ""
    property string date: ""
    property string shortDate: ""
    property bool fullscreen: true
    property bool settingsOpen: false
    property Item controlLayer: null
    readonly property point settingsAnchor: Qt.point(orb.x - 4, orb.y + orb.height + 14)
    readonly property point modelAnchor: Qt.point(controls.x + modelChip.x, controls.y + modelChip.height + 18)
    readonly property point navAnchor: Qt.point(controls.x + navigationTrigger.x + navigationTrigger.width, controls.y + navigationTrigger.height + 18)
    // Below the design width the core shrinks, the HUD geometry goes, and the
    // model chip collapses to its icon, so nothing overlaps.
    readonly property bool narrow: width < 1500
    // The core keeps the reference's centre and a proportional diameter at
    // every width: it is the primary identity anchor, never repositioned
    // out of the composition.
    readonly property real coreCx: Theme.command ? width * 0.42 : width * Theme.macroCoreX
    readonly property real coreCy: Theme.command ? 60 : bar.viewportHeight * Theme.macroCoreY
    readonly property real coreDiameter: Theme.command ? 162 : Theme.coreSize(width)

    function tz() {
        var m = -new Date().getTimezoneOffset(); var sign = m < 0 ? "-" : "+"; m = Math.abs(m)
        return "UTC" + sign + ("0" + Math.floor(m / 60)).slice(-2) + ":" + ("0" + (m % 60)).slice(-2)
    }

    // -- HUD circuit line, broken around the core, with angled returns, end markers and ticks
    Shape {
        id: hudLines
        anchors.fill: parent
        visible: !bar.narrow
        opacity: Theme.hudOpacity
        preferredRendererType: Shape.CurveRenderer
        readonly property real yLine: Theme.command ? 100 : 150
        readonly property real yRight: Theme.command ? 90 : 126
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.width * 0.245; startY: hudLines.yLine + 22
            PathLine { x: bar.width * 0.265; y: hudLines.yLine }
            PathLine { x: bar.coreCx - 168; y: hudLines.yLine }
        }
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.coreCx + 168; startY: hudLines.yLine
            PathLine { x: bar.width * 0.655; y: hudLines.yLine }
            PathLine { x: bar.width * 0.685; y: hudLines.yRight }
            PathLine { x: bar.width * 0.93; y: hudLines.yRight }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.width * 0.31; startY: hudLines.yLine + 7
            PathLine { x: bar.coreCx - 186; y: hudLines.yLine + 7 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.coreCx + 250; startY: hudLines.yLine + 7
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

    // Orbit: a tilted ellipse to the left of the core, passing behind the
    // sphere (the sphere is opaque), with a dotted companion and a node.
    Shape {
        id: orbit
        visible: !bar.narrow
        width: 330; height: 84
        x: bar.coreCx - 150 - width / 2; y: bar.coreCy + 6 - height / 2
        rotation: -12
        opacity: Theme.hudOpacity
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"
            PathAngleArc { centerX: orbit.width / 2; centerY: orbit.height / 2; radiusX: orbit.width / 2 - 2; radiusY: orbit.height / 2 - 2; startAngle: 0; sweepAngle: 360 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.6); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 5]
            PathAngleArc { centerX: orbit.width / 2; centerY: orbit.height / 2; radiusX: orbit.width / 2 - 16; radiusY: orbit.height / 2 - 12; startAngle: 120; sweepAngle: 220 }
        }
        Rectangle { width: 5; height: 5; radius: 2.5; color: Theme.hud
            x: orbit.width / 2 + (orbit.width / 2 - 2) * Math.cos(205 * Math.PI / 180) - 2.5
            y: orbit.height / 2 + (orbit.height / 2 - 2) * Math.sin(205 * Math.PI / 180) - 2.5
            Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    }

    // -- brand: the orb IS the Settings trigger -------------------------------
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
        Rectangle { anchors.centerIn: parent; width: 84; height: 84; radius: 42; color: Theme.alpha(Theme.accent, orb.lit ? 0.18 : 0.07); Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        // Outer calibrated ring with nodes.
        Item {
            anchors.fill: parent
            RotationAnimator on rotation { running: Theme.ambient && bar.settingsOpen; loops: Animation.Infinite; from: 0; to: 360; duration: 12000 }
            Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: Theme.accent; opacity: orb.lit ? 0.95 : 0.5; Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
            Repeater { model: 12
                Rectangle { width: index % 3 === 0 ? 3 : 1; height: index % 3 === 0 ? 3 : 3; radius: index % 3 === 0 ? 1.5 : 0; color: Theme.accent; opacity: orb.lit ? 0.95 : 0.6; x: orb.width / 2 - width / 2; y: index % 3 === 0 ? -1 : 2
                    transform: Rotation { origin.x: width / 2; origin.y: orb.height / 2 - y; angle: index * 30 } } }
        }
        // The luminous ring: thick, uniform cyan with a soft outer glow and a dark centre.
        Rectangle { anchors.centerIn: parent; width: 56; height: 56; radius: 28; color: "transparent"; border.width: 10; border.color: Theme.accent; opacity: 0.18 }
        Rectangle { anchors.centerIn: parent; width: 50; height: 50; radius: 25; color: "transparent"; border.width: 7; border.color: Theme.accent; opacity: 0.3 }
        Rectangle { anchors.centerIn: parent; width: 46; height: 46; radius: 23; color: Theme.bg0 }
        Shape {
            id: orbRing
            anchors.centerIn: parent; width: 46; height: 46
            preferredRendererType: Shape.CurveRenderer
            opacity: orb.lit ? 1 : 0.9
            ShapePath {
                strokeWidth: -1; fillRule: ShapePath.OddEvenFill
                fillGradient: ConicalGradient {
                    centerX: 23; centerY: 23; angle: 135
                    GradientStop { position: 0.00; color: Qt.lighter(Theme.accent, 1.25) }
                    GradientStop { position: 0.35; color: Theme.accent }
                    GradientStop { position: 0.60; color: Theme.accentDeep }
                    GradientStop { position: 0.80; color: Theme.accent }
                    GradientStop { position: 1.00; color: Qt.lighter(Theme.accent, 1.25) }
                }
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 23; radiusY: 23; startAngle: 0; sweepAngle: 360 }
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 17; radiusY: 17; startAngle: 0; sweepAngle: 360 }
            }
        }
        Rectangle { anchors.centerIn: parent; width: 34; height: 34; radius: 17; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.35) }
        Rectangle { anchors.centerIn: parent; width: 28; height: 28; radius: 14; color: "transparent"; border.width: 2; border.color: Theme.accent; opacity: orb.lit ? 0.5 : 0.18; Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
        MouseArea { id: orbArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bar.openSettings() }
        Rectangle {
            visible: orb.hovered && !bar.settingsOpen
            anchors.top: parent.bottom; anchors.topMargin: 10; anchors.horizontalCenter: parent.horizontalCenter
            width: tipText.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
            Text { id: tipText; anchors.centerIn: parent; text: "Settings"; color: Theme.text; font { family: Theme.font; pixelSize: 11 } }
        }
    }
    Text { x: 120; y: 19; text: "JARVIS"; color: Theme.text; font { family: Theme.font; pixelSize: 27; letterSpacing: 12; weight: Font.Normal } }
    Text { x: 122; y: 58; text: "THINKS AHEAD FOR YOU"; color: Theme.textLabel; font { family: Theme.font; pixelSize: 10; letterSpacing: 3.8 } }

    Text {
        visible: !Theme.command; x: bar.railInset + 2; y: Math.round(bar.viewportHeight * 0.1040)
        width: bar.railWidth - bar.railInset - 10; elide: Text.ElideRight
        // The rail keeps the real date; a tight rail abbreviates the month
        // rather than eliding it.
        text: bar.railWidth >= 190 ? bar.date : bar.shortDate
        color: Theme.textLabel; font { family: Theme.font; pixelSize: 14; letterSpacing: 0.2 }
    }
    Row { visible: !Theme.command; x: bar.railInset; y: Math.round(bar.viewportHeight * 0.1185); spacing: 9
        Text { text: bar.clock; color: "#9fdcff"; font { family: Theme.font; pixelSize: 40; weight: Font.Medium; letterSpacing: 0.4 } }
        Text { text: bar.meridiem; color: "#9fdcff"; anchors.baseline: parent.children[0].baseline; font { family: Theme.font; pixelSize: 21; weight: Font.Medium; letterSpacing: 0.6 } }
    }

    // Local-context block (the reference's weather slot). No weather source
    // exists, so it carries the real local time zone and says so.
    Item {
        id: contextBlock
        x: bar.railInset; y: Math.round(bar.viewportHeight * 0.1980); width: bar.railWidth - bar.railInset - 8; height: 60
        visible: !bar.narrow && !Theme.command
        Item { id: contextMark; visible: bar.railWidth >= 220; width: 28; height: 28; y: 8
            Rectangle { anchors.fill: parent; radius: 14; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.5) }
            Rectangle { anchors.centerIn: parent; width: 14; height: 14; radius: 7; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.8) }
            Rectangle { anchors.centerIn: parent; width: 5; height: 5; radius: 2.5; color: Theme.accent }
            Repeater { model: 8; Rectangle { width: 1; height: 4; color: Theme.accent; opacity: 0.7; x: 13.5; y: -2; transform: Rotation { origin.x: 0.5; origin.y: 16; angle: index * 45 } } }
        }
        Column { x: contextMark.visible ? 44 : 0; y: 2; width: parent.width - x; spacing: 3
            Text { width: parent.width; elide: Text.ElideRight; text: bar.tz() + "  Local"; color: Theme.text; font { family: Theme.font; pixelSize: 14 } }
            Text { width: parent.width; elide: Text.ElideRight; text: "Weather not connected"; color: Theme.textLabel; font { family: Theme.font; pixelSize: 13 } }
        }
    }

    // -- core + state ticker --------------------------------------------------
    JarvisCore {
        id: core
        objectName: "jarvisCore"
        x: bar.coreCx - width / 2
        y: bar.coreCy - height / 2
        width: bar.coreDiameter; height: width
        state_: bridge.coreState
        active: Window.active
        Behavior on width { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
    }
    Column {
        visible: !bar.narrow
        x: bar.coreCx + 141; y: 45
        spacing: 10
        Repeater {
            model: ["LISTENING", "REASONING", "EXECUTING"]
            Text {
                text: modelData
                property bool lit: (modelData === "LISTENING" && (bridge.coreState === "idle" || bridge.coreState === "awaiting_input"))
                                   || (modelData === "REASONING" && bridge.coreState === "running")
                                   || (modelData === "EXECUTING" && bridge.coreState === "awaiting_approval")
                color: lit ? "#eaf6ff" : Theme.textLabel
                font { family: Theme.font; pixelSize: 10; letterSpacing: 2.2; weight: Font.DemiBold }
                Behavior on color { ColorAnimation { duration: 200 } }
            }
        }
    }
    Text {
        visible: !bar.narrow && bridge.coreState !== "idle"
        x: bar.coreCx + 150; y: 128
        text: Theme.stateLabel(bridge.coreState)
        color: Theme.stateColor(bridge.coreState)
        font { family: Theme.font; pixelSize: 9; letterSpacing: 2.6; weight: Font.DemiBold }
        Behavior on color { ColorAnimation { duration: 200 } }
    }

    // -- right controls -------------------------------------------------------
    Row {
        id: controls
        objectName: "commandStrip"
        parent: bar.controlLayer || bar
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight); topMargin: 25 }
        spacing: Theme.stripSpacing(bar.width)

        // Provider / model chip opens ONLY its own model panel.
        Rectangle {
            id: modelChip
            objectName: "modelTrigger"
            width: bar.narrow ? 50 : Theme.chipWidth(bar.width); height: 40; radius: Theme.radiusSmall
            activeFocusOnTab: true; Accessible.role: Accessible.Button; Accessible.name: "Model and provider"
            Keys.onReturnPressed: bar.openModel()
            Keys.onSpacePressed: bar.openModel()
            color: chipArea.containsMouse ? Theme.surfaceRaised : Theme.surface; border.width: 1; border.color: chipArea.containsMouse ? Theme.borderStrong : Theme.border
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Row {
                id: modelRow
                anchors.centerIn: parent; spacing: 10
                Rectangle { width: 22; height: 22; radius: 6; anchors.verticalCenter: parent.verticalCenter
                    gradient: Gradient { GradientStop { position: 0; color: "#f4bd66" } GradientStop { position: 1; color: "#d2842a" } }
                    Text { anchors.centerIn: parent; text: "AI"; color: "#2b1700"; font { family: Theme.font; pixelSize: 10; weight: Font.Bold } } }
                Text { visible: !bar.narrow; anchors.verticalCenter: parent.verticalCenter
                    width: Math.max(56, modelChip.width - 88); elide: Text.ElideRight
                    text: "Claude" + (bridge.config.claude_model ? " · " + bridge.config.claude_model : "")
                    color: Theme.text; font { family: Theme.font; pixelSize: 15 } }
                Text { visible: !bar.narrow; anchors.verticalCenter: parent.verticalCenter; text: "⌄"; color: Theme.textLabel; font.pixelSize: 15 }
            }
            Rectangle { anchors.fill: parent; anchors.margins: -3; radius: parent.radius; color: "transparent"; border.color: Theme.accent; visible: modelChip.activeFocus }
            MouseArea { id: chipArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bar.openModel() }
        }

        // Execution state: the glass lamp. Indicates only - the Kill Switch
        // beside it is the control. Sized so its ring reads at ~1.18x the
        // kill switch ring, as in the reference; the axis nodes overhang the row.
        Item {
            width: 62; height: 40
            StatusOrb {
                id: statusOrb
                objectName: "statusOrb"
                anchors.centerIn: parent
                lit: bridge.executionEnabled && !bridge.configRefused
                skin: bridge.statusIndicator
                Accessible.role: Accessible.Indicator
                Accessible.name: bridge.configRefused ? "Execution status: configuration refused" : (bridge.executionEnabled ? (bridge.busy ? "Execution status: running" : "Execution status: ready") : "Execution status: disabled")
                MouseArea { id: orbHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Rectangle {
                    visible: orbHover.containsMouse
                    anchors.top: parent.bottom; anchors.topMargin: 4; anchors.horizontalCenter: parent.horizontalCenter
                    width: orbTip.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
                    Text { id: orbTip; anchors.centerIn: parent; color: Theme.text; font { family: Theme.font; pixelSize: 11 }
                        text: bridge.configRefused ? "Configuration refused" : (bridge.executionEnabled ? (bridge.busy ? "Execution ready · running" : "Execution ready") : "Execution disabled") }
                }
            }
        }

        PowerControl { objectName: "killSwitchControl"; enabled: !bridge.configRefused; engaged: !bridge.executionEnabled; onClicked: bar.killSwitchRequested() }
        IconButton {
            id: navigationTrigger
            objectName: "navigationTrigger"
            tip: "Navigation"; width: 36; height: 40; onClicked: bar.menuRequested()
            Grid { anchors.centerIn: parent; columns: 2; spacing: 4
                Repeater { model: 4; Rectangle { width: 6; height: 6; radius: index === 3 ? 1 : 3; color: "transparent"; border.width: 1.4; border.color: Theme.accent } } }
        }
        IconButton { glyph: "–"; tip: "Minimize"; width: 36; height: 37; onClicked: bar.minimizeRequested() }
        IconButton { glyph: bar.fullscreen ? "❐" : "▢"; tip: bar.fullscreen ? "Windowed" : "Full screen"; width: 36; height: 37; onClicked: bar.restoreRequested() }
        IconButton { glyph: "✕"; tip: "Close"; width: 36; height: 37; onClicked: bar.closeRequested() }
    }

    // The tiny icon-only layout-edit control: a sliders glyph in a framed square.
    IconButton {
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight) + 8
                  topMargin: Math.round(bar.viewportHeight * 0.1130) }
        tip: bridge.editMode ? "Done editing" : "Edit layout"
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
