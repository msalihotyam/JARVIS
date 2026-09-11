import QtQuick
import QtQuick.Shapes
import "../theme"

// Brand (the orb is the Settings trigger), clock, the core with its HUD
// geometry, compact model / execution controls, kill switch, navigation
// trigger, window controls and the tiny layout-edit control.
Item {
    id: bar
    height: 160
    signal menuRequested()
    signal killSwitchRequested()
    signal editRequested()
    signal minimizeRequested()
    signal restoreRequested()
    signal closeRequested()
    signal openSettings()
    property string clock: ""
    property string date: ""
    property bool fullscreen: true
    property bool settingsOpen: false
    // Below the design width the core shrinks, the HUD geometry goes, and the
    // model chip collapses to its icon, so nothing overlaps.
    readonly property bool narrow: width < 1500

    // -- HUD geometry: the long horizontal circuit line, broken around the core,
    //    with two angled returns and end markers; and the orbit around the core.
    Shape {
        id: hudLines
        anchors.fill: parent
        visible: !bar.narrow
        opacity: Theme.hudOpacity
        preferredRendererType: Shape.CurveRenderer
        readonly property real yLine: 120
        readonly property real yRight: 98
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.width * 0.245; startY: hudLines.yLine + 22
            PathLine { x: bar.width * 0.265; y: hudLines.yLine }
            PathLine { x: bar.width / 2 - 150; y: hudLines.yLine }
        }
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.width / 2 + 150; startY: hudLines.yLine
            PathLine { x: bar.width * 0.655; y: hudLines.yLine }
            PathLine { x: bar.width * 0.685; y: hudLines.yRight }
            PathLine { x: bar.width * 0.93; y: hudLines.yRight }
        }
        // The fainter parallel companion line near the core.
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.width * 0.31; startY: hudLines.yLine + 7
            PathLine { x: bar.width / 2 - 168; y: hudLines.yLine + 7 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.width / 2 + 250; startY: hudLines.yLine + 7
            PathLine { x: bar.width * 0.62; y: hudLines.yLine + 7 }
        }
    }
    // Short calibration ticks along the right run.
    Repeater {
        model: 4
        Rectangle { visible: !bar.narrow; x: bar.width * (0.74 + index * 0.05); y: hudLines.yRight - 4; width: 1; height: 9; color: Theme.hud; opacity: Theme.hudOpacity * 1.2 }
    }
    Rectangle { visible: !bar.narrow; x: bar.width * 0.245 - 2.5; y: hudLines.yLine + 22 - 2.5; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    Rectangle { visible: !bar.narrow; x: bar.width * 0.93 - 2.5; y: hudLines.yRight - 2.5; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }

    // Orbit around the core: a tilted ellipse arc and its dotted companion.
    Shape {
        id: orbit
        visible: !bar.narrow
        width: 540; height: 130
        x: core.x + core.width / 2 - width / 2 - 70; y: core.y + core.height / 2 - height / 2 + 6
        rotation: -14
        opacity: Theme.hudOpacity
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: orbit.width / 2; centerY: orbit.height / 2; radiusX: orbit.width / 2 - 4; radiusY: orbit.height / 2 - 4; startAngle: 150; sweepAngle: 200 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.6); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 5]
            PathAngleArc { centerX: orbit.width / 2; centerY: orbit.height / 2; radiusX: orbit.width / 2 - 22; radiusY: orbit.height / 2 - 16; startAngle: 175; sweepAngle: 150 }
        }
        Rectangle { width: 5; height: 5; radius: 2.5; color: Theme.hud
            x: orbit.width / 2 + (orbit.width / 2 - 4) * Math.cos(200 * Math.PI / 180) - 2.5
            y: orbit.height / 2 + (orbit.height / 2 - 4) * Math.sin(200 * Math.PI / 180) - 2.5
            Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    }

    // -- brand: the orb IS the Settings trigger ----------------------------------
    Item {
        id: orb
        objectName: "settingsOrb"
        x: 28; y: 22; width: 58; height: 58
        readonly property bool hovered: orbArea.containsMouse
        readonly property bool lit: hovered || bar.settingsOpen
        scale: orbArea.pressed ? 0.95 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
        // Halo when hovered / open.
        Rectangle { anchors.centerIn: parent; width: 76; height: 76; radius: 38; color: Theme.alpha(Theme.accent, orb.lit ? 0.16 : 0); Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        // Outer calibrated ring.
        Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: Theme.accent; opacity: orb.lit ? 0.95 : 0.45; Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
        Repeater { model: 12
            Rectangle { width: 1; height: index % 3 === 0 ? 4 : 2; color: Theme.accent; opacity: orb.lit ? 0.9 : 0.5; x: orb.width / 2 - 0.5; y: 2
                transform: Rotation { origin.x: 0.5; origin.y: orb.height / 2 - 2; angle: index * 30 } } }
        // The luminous ring: a conical gradient annulus.
        Shape {
            id: orbRing
            anchors.centerIn: parent; width: 42; height: 42
            preferredRendererType: Shape.CurveRenderer
            RotationAnimator on rotation { running: Theme.ambient && bar.settingsOpen; loops: Animation.Infinite; from: 0; to: 360; duration: 6000 }
            ShapePath {
                strokeWidth: -1; fillRule: ShapePath.OddEvenFill
                fillGradient: ConicalGradient {
                    centerX: orbRing.width / 2; centerY: orbRing.height / 2; angle: 90
                    GradientStop { position: 0.00; color: Qt.lighter(Theme.accent, 1.3) }
                    GradientStop { position: 0.30; color: Theme.accent }
                    GradientStop { position: 0.58; color: Theme.alpha(Theme.accentDeep, 0.55) }
                    GradientStop { position: 0.80; color: Theme.alpha(Theme.accent, 0.25) }
                    GradientStop { position: 1.00; color: Qt.lighter(Theme.accent, 1.3) }
                }
                PathAngleArc { centerX: orbRing.width / 2; centerY: orbRing.height / 2; radiusX: 21; radiusY: 21; startAngle: 0; sweepAngle: 360 }
                PathAngleArc { centerX: orbRing.width / 2; centerY: orbRing.height / 2; radiusX: 15.5; radiusY: 15.5; startAngle: 0; sweepAngle: 360 }
            }
        }
        // Dark core with a small centre light.
        Rectangle { anchors.centerIn: parent; width: 30; height: 30; radius: 15; color: Theme.bg0; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.35) }
        Rectangle { anchors.centerIn: parent; width: 8; height: 8; radius: 4; color: Theme.accent; opacity: orb.lit ? 0.95 : 0.55; Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
            Rectangle { anchors.centerIn: parent; width: 16; height: 16; radius: 8; color: Theme.accent; opacity: 0.3 } }
        MouseArea { id: orbArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bar.openSettings() }
        Rectangle {
            visible: orb.hovered && !bar.settingsOpen
            anchors.top: parent.bottom; anchors.topMargin: 10; anchors.horizontalCenter: parent.horizontalCenter
            width: tipText.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
            Text { id: tipText; anchors.centerIn: parent; text: "Settings"; color: Theme.text; font { family: Theme.font; pixelSize: 11 } }
        }
    }
    Column {
        x: 104; y: 30
        spacing: 6
        Text { text: "J A R V I S"; color: Theme.text; font { family: Theme.font; pixelSize: 23; letterSpacing: 6; weight: Font.Normal } }
        Text { text: "THINKS AHEAD FOR YOU"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 9; letterSpacing: 3.4 } }
    }
    Column {
        x: 34; y: 100
        spacing: 0
        Text { text: bar.date; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13; letterSpacing: 0.3 } }
        Text { text: bar.clock; color: Theme.text; font { family: Theme.font; pixelSize: 31; weight: Font.Light; letterSpacing: 0.5 } }
    }

    // -- core + state ticker ------------------------------------------------
    JarvisCore {
        id: core
        x: (bar.narrow ? bar.width * 0.42 : bar.width / 2) - width / 2
        y: bar.narrow ? 8 : -16
        width: bar.narrow ? 116 : 204; height: width
        state_: bridge.coreState
        active: Window.active
        Behavior on width { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
    }
    Column {
        visible: !bar.narrow
        anchors { left: core.right; leftMargin: 40; verticalCenter: core.verticalCenter; verticalCenterOffset: -18 }
        spacing: 8
        // The current state, in its own color, heads the ticker.
        Text {
            text: Theme.stateLabel(bridge.coreState)
            color: Theme.stateColor(bridge.coreState)
            font { family: Theme.font; pixelSize: 10; letterSpacing: 3.2; weight: Font.DemiBold }
            bottomPadding: 4
            Behavior on color { ColorAnimation { duration: 200 } }
        }
        Repeater {
            model: ["LISTENING", "REASONING", "EXECUTING"]
            Row {
                spacing: 10
                property bool lit: (modelData === "LISTENING" && (bridge.coreState === "idle" || bridge.coreState === "awaiting_input"))
                                   || (modelData === "REASONING" && bridge.coreState === "running")
                                   || (modelData === "EXECUTING" && bridge.coreState === "awaiting_approval")
                Rectangle { width: 4; height: 4; radius: 2; anchors.verticalCenter: parent.verticalCenter; color: parent.lit ? Theme.accent : Theme.textDim; opacity: parent.lit ? 1 : 0.4
                    Behavior on color { ColorAnimation { duration: 200 } } }
                Text {
                    text: modelData
                    color: parent.lit ? Theme.accent : Theme.textDim
                    font { family: Theme.font; pixelSize: 10; letterSpacing: 3.2; weight: parent.lit ? Font.DemiBold : Font.Normal }
                    Behavior on color { ColorAnimation { duration: 200 } }
                }
            }
        }
    }
    // -- right controls ---------------------------------------------------
    Row {
        anchors { right: parent.right; top: parent.top; rightMargin: 24; topMargin: 24 }
        spacing: 10

        // Provider / model chip -> Settings (quiet; the orb is the primary trigger).
        Rectangle {
            width: modelRow.width + 26; height: 38; radius: Theme.radiusSmall
            color: chipArea.containsMouse ? Theme.surfaceRaised : Theme.surface; border.width: 1; border.color: chipArea.containsMouse ? Theme.borderStrong : Theme.border
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Row {
                id: modelRow
                anchors.centerIn: parent; spacing: 9
                Rectangle { width: 20; height: 20; radius: 5; anchors.verticalCenter: parent.verticalCenter
                    gradient: Gradient { orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: "#4f8dff" } GradientStop { position: 1; color: "#7a5cff" } }
                    Text { anchors.centerIn: parent; text: "AI"; color: "#ffffff"; font { family: Theme.font; pixelSize: 9; weight: Font.Bold } } }
                Text { visible: !bar.narrow; anchors.verticalCenter: parent.verticalCenter
                    text: "claude · " + (bridge.config.claude_model || "cli default") + (bridge.config.claude_effort ? " · " + bridge.config.claude_effort : "")
                    color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
                Text { visible: !bar.narrow; anchors.verticalCenter: parent.verticalCenter; text: "⌄"; color: Theme.textMuted; font.pixelSize: 14 }
            }
            MouseArea { id: chipArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bar.openSettings() }
        }

        // Execution state.
        Rectangle {
            readonly property color tone: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.success
            width: stateRow.width + 28; height: 38; radius: Theme.radiusSmall
            color: Theme.alpha(tone, 0.09); border.width: 1; border.color: Theme.alpha(tone, 0.38)
            Row {
                id: stateRow
                anchors.centerIn: parent; spacing: 9
                Rectangle { width: 7; height: 7; radius: 3.5; anchors.verticalCenter: parent.verticalCenter; color: parent.parent.tone
                    Rectangle { anchors.centerIn: parent; width: 15; height: 15; radius: 7.5; color: parent.color; opacity: 0.25 } }
                Text { anchors.verticalCenter: parent.verticalCenter
                    text: bridge.configRefused ? "CONFIG REFUSED" : (bridge.executionEnabled ? (bridge.busy ? "RUNNING" : "READY") : "DISABLED")
                    color: parent.parent.tone
                    font { family: Theme.font; pixelSize: 11; letterSpacing: 1.8; weight: Font.DemiBold } }
            }
        }

        // Kill switch.
        Rectangle {
            width: killRow.width + 28; height: 38; radius: Theme.radiusSmall
            color: bridge.executionEnabled ? Theme.alpha(Theme.danger, killArea.containsMouse ? 0.16 : 0.08) : Theme.danger
            border.width: 1; border.color: Theme.alpha(Theme.danger, 0.6)
            opacity: bridge.configRefused ? 0.4 : 1
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Row {
                id: killRow
                anchors.centerIn: parent; spacing: 9
                Text { anchors.verticalCenter: parent.verticalCenter; text: "⏻"; color: bridge.executionEnabled ? Theme.danger : "#12060a"; font.pixelSize: 14 }
                Text { anchors.verticalCenter: parent.verticalCenter
                    text: bridge.executionEnabled ? "Kill Switch" : "Re-enable"
                    color: bridge.executionEnabled ? Theme.danger : "#12060a"
                    font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold } }
            }
            MouseArea { id: killArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; enabled: !bridge.configRefused; onClicked: bar.killSwitchRequested() }
        }

        Item { width: 4; height: 1 }
        IconButton {
            tip: "Menu"; width: 36; height: 38; onClicked: bar.menuRequested()
            Grid { anchors.centerIn: parent; columns: 2; spacing: 4
                Repeater { model: 4; Rectangle { width: 5; height: 5; radius: 1.5; color: Theme.textLabel } } }
        }
        IconButton { glyph: "–"; tip: "Minimize"; width: 36; height: 38; onClicked: bar.minimizeRequested() }
        IconButton { glyph: bar.fullscreen ? "❐" : "▢"; tip: bar.fullscreen ? "Windowed" : "Full screen"; width: 36; height: 38; onClicked: bar.restoreRequested() }
        IconButton { glyph: "✕"; tip: "Close"; width: 36; height: 38; onClicked: bar.closeRequested() }
    }

    // The tiny icon-only layout-edit control: a sliders glyph in a framed square.
    IconButton {
        anchors { right: parent.right; top: parent.top; rightMargin: 24; topMargin: 84 }
        tip: bridge.editMode ? "Done editing" : "Edit layout"
        active: bridge.editMode
        framed: true
        width: 36; height: 36
        onClicked: bar.editRequested()
        Item { anchors.centerIn: parent; width: 16; height: 14
            property color c: bridge.editMode ? Theme.accent : Theme.textLabel
            Rectangle { x: 0; y: 1; width: 16; height: 1.2; color: parent.c }   Rectangle { x: 10; y: -1; width: 4; height: 5; radius: 1; color: parent.c }
            Rectangle { x: 0; y: 7; width: 16; height: 1.2; color: parent.c }   Rectangle { x: 3; y: 5; width: 4; height: 5; radius: 1; color: parent.c }
            Rectangle { x: 0; y: 13; width: 16; height: 1.2; color: parent.c }  Rectangle { x: 8; y: 11; width: 4; height: 5; radius: 1; color: parent.c }
        }
    }
}
