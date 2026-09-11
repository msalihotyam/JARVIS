import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Brand, clock, the core, compact model/execution controls, kill switch,
// navigation trigger, the tiny layout-edit control and window controls.
Item {
    id: bar
    height: 150
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
    // Below the design width the core shrinks, the state ticker and HUD
    // lines go, and the model chip collapses to its icon, so nothing overlaps.
    readonly property bool narrow: width < 1500

    // -- brand ------------------------------------------------------------
    Row {
        anchors { left: parent.left; top: parent.top; leftMargin: 28; topMargin: 22 }
        spacing: 16
        Item {
            width: 56; height: 56
            Rectangle { anchors.fill: parent; radius: 28; color: "transparent"; border.width: 1.5; border.color: Theme.accent; opacity: 0.8 }
            Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: "transparent"; border.width: 3; border.color: Theme.accent }
        }
        Column {
            anchors.verticalCenter: parent.verticalCenter
            spacing: 4
            Text { text: "J A R V I S"; color: Theme.text; font { family: Theme.font; pixelSize: 24; letterSpacing: 4; weight: Font.Light } }
            Text { text: "THINKS AHEAD FOR YOU"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 9; letterSpacing: 3 } }
        }
    }
    Column {
        anchors { left: parent.left; top: parent.top; leftMargin: 34; topMargin: 100 }
        spacing: 2
        Text { text: bar.date; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
        Text { text: bar.clock; color: Theme.text; font { family: Theme.font; pixelSize: 30; weight: Font.Light } }
    }

    // -- core + state ticker ------------------------------------------------
    JarvisCore {
        id: core
        x: (bar.narrow ? bar.width * 0.42 : bar.width / 2) - width / 2
        y: bar.narrow ? 4 : -8
        width: bar.narrow ? 110 : 150; height: width
        state_: bridge.coreState
        active: Window.active
        Behavior on width { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
    }
    Rectangle { visible: !bar.narrow; anchors.verticalCenter: core.verticalCenter; anchors.right: core.left; anchors.rightMargin: 24; width: 240; height: 1; color: Theme.hud; opacity: 0.7 }
    Rectangle { visible: !bar.narrow; anchors.verticalCenter: core.verticalCenter; anchors.left: core.right; anchors.leftMargin: 24; width: 240; height: 1; color: Theme.hud; opacity: 0.7 }
    Column {
        visible: !bar.narrow
        anchors { left: core.right; leftMargin: 34; verticalCenter: core.verticalCenter }
        spacing: 6
        Repeater {
            model: ["LISTENING", "REASONING", "EXECUTING"]
            Text {
                text: modelData
                property bool lit: (modelData === "LISTENING" && (bridge.coreState === "idle" || bridge.coreState === "awaiting_input"))
                                   || (modelData === "REASONING" && bridge.coreState === "running")
                                   || (modelData === "EXECUTING" && bridge.coreState === "awaiting_approval")
                color: lit ? Theme.accent : Theme.textDim
                font { family: Theme.font; pixelSize: 10; letterSpacing: 3 }
                Behavior on color { ColorAnimation { duration: 200 } }
            }
        }
    }
    Text {
        visible: !bar.narrow
        anchors { right: core.left; rightMargin: 34; verticalCenter: core.verticalCenter }
        text: Theme.stateLabel(bridge.coreState)
        color: Theme.stateColor(bridge.coreState)
        font { family: Theme.font; pixelSize: 10; letterSpacing: 3 }
        horizontalAlignment: Text.AlignRight
    }

    // -- right controls ---------------------------------------------------
    Row {
        anchors { right: parent.right; top: parent.top; rightMargin: 28; topMargin: 22 }
        spacing: 12

        // Provider / model chip -> Settings.
        Rectangle {
            width: modelRow.width + 28; height: 38; radius: Theme.radiusSmall
            color: Theme.surface; border.width: 1; border.color: Theme.border
            Row {
                id: modelRow
                anchors.centerIn: parent; spacing: 8
                Rectangle { width: 18; height: 18; radius: 4; color: Theme.accent; anchors.verticalCenter: parent.verticalCenter
                    Text { anchors.centerIn: parent; text: "AI"; color: Theme.bg0; font { pixelSize: 9; weight: Font.Bold } } }
                Text { visible: !bar.narrow; anchors.verticalCenter: parent.verticalCenter
                    text: "claude · " + (bridge.config.claude_model || "cli default") + (bridge.config.claude_effort ? " · " + bridge.config.claude_effort : "")
                    color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
                Text { visible: !bar.narrow; anchors.verticalCenter: parent.verticalCenter; text: "⌄"; color: Theme.textMuted; font.pixelSize: 14 }
            }
            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bar.openSettings() }
        }

        // Execution state chip.
        Rectangle {
            width: stateRow.width + 28; height: 38; radius: Theme.radiusSmall
            color: Theme.surface; border.width: 1
            border.color: bridge.executionEnabled ? Qt.rgba(Theme.success.r, Theme.success.g, Theme.success.b, 0.5) : Theme.danger
            Row {
                id: stateRow
                anchors.centerIn: parent; spacing: 8
                Rectangle { width: 8; height: 8; radius: 4; anchors.verticalCenter: parent.verticalCenter
                    color: bridge.executionEnabled ? Theme.success : Theme.danger }
                Text { anchors.verticalCenter: parent.verticalCenter
                    text: bridge.configRefused ? "CONFIG REFUSED" : (bridge.executionEnabled ? (bridge.busy ? "RUNNING" : "READY") : "DISABLED")
                    color: bridge.executionEnabled ? Theme.success : Theme.danger
                    font { family: Theme.font; pixelSize: 11; letterSpacing: 1.5; weight: Font.DemiBold } }
            }
        }

        // Kill switch.
        Rectangle {
            width: killRow.width + 28; height: 38; radius: Theme.radiusSmall
            color: bridge.executionEnabled ? Qt.rgba(Theme.danger.r, Theme.danger.g, Theme.danger.b, 0.08) : Theme.danger
            border.width: 1; border.color: Theme.danger
            opacity: bridge.configRefused ? 0.4 : 1
            Row {
                id: killRow
                anchors.centerIn: parent; spacing: 8
                Text { anchors.verticalCenter: parent.verticalCenter; text: "⏻"; color: bridge.executionEnabled ? Theme.danger : Theme.bg0; font.pixelSize: 14 }
                Text { anchors.verticalCenter: parent.verticalCenter
                    text: bridge.executionEnabled ? "Kill Switch" : "Re-enable"
                    color: bridge.executionEnabled ? Theme.danger : Theme.bg0
                    font { family: Theme.font; pixelSize: 13; weight: Font.DemiBold } }
            }
            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; enabled: !bridge.configRefused; onClicked: bar.killSwitchRequested() }
        }

        IconButton {
            tip: "Menu"; width: 38; height: 38; onClicked: bar.menuRequested()
            Grid { anchors.centerIn: parent; columns: 2; spacing: 4
                Repeater { model: 4; Rectangle { width: 5; height: 5; radius: 1; color: Theme.text } } }
        }
        IconButton { glyph: "–"; tip: "Minimize"; width: 38; height: 38; onClicked: bar.minimizeRequested() }
        IconButton { glyph: bar.fullscreen ? "❐" : "▢"; tip: bar.fullscreen ? "Windowed" : "Full screen"; width: 38; height: 38; onClicked: bar.restoreRequested() }
        IconButton { glyph: "✕"; tip: "Close"; width: 38; height: 38; onClicked: bar.closeRequested() }
    }

    // The tiny icon-only layout-edit control.
    IconButton {
        anchors { right: parent.right; top: parent.top; rightMargin: 28; topMargin: 76 }
        tip: bridge.editMode ? "Done editing" : "Edit layout"
        active: bridge.editMode
        width: 34; height: 34
        onClicked: bar.editRequested()
        // A tiny layout glyph: one tall pane beside two stacked panes.
        Item { anchors.centerIn: parent; width: 14; height: 12
            Rectangle { x: 0; y: 0; width: 6; height: 12; radius: 1; color: "transparent"; border.width: 1.2; border.color: bridge.editMode ? Theme.accent : Theme.text }
            Rectangle { x: 8; y: 0; width: 6; height: 5; radius: 1; color: "transparent"; border.width: 1.2; border.color: bridge.editMode ? Theme.accent : Theme.text }
            Rectangle { x: 8; y: 7; width: 6; height: 5; radius: 1; color: "transparent"; border.width: 1.2; border.color: bridge.editMode ? Theme.accent : Theme.text }
        }
    }
}
