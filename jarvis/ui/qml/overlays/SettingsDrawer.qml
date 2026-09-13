import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

ContextPanel {
    id: settings
    // Above the interactive summary rail (420), below permanent top controls.
    z: 430
    surfaceEdge: Theme.materialBlue
    surfaceFill: "#fd06182d"
    heading: "GENERAL SETTINGS"
    surfaceName: "settingsSurface"
    panelWidth: 460; panelHeight: 615
    Flickable {
        anchors.fill: parent; clip: true; contentHeight: sections.height
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
            id: sections; width: parent.width; spacing: 17
            component Label: Text { color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            Label { text: "PRESENTATION PACKAGE" }
            Column { width: parent.width; spacing: 8
                Repeater {
                    model: [
                        {key: "space", name: "Canonical Space", note: "Orbital glass · primary JARVIS composition", tone: "#1bbdff"},
                        {key: "dark", name: "Dark", note: "Near-black glass · restrained light", tone: "#476e82"},
                        {key: "command", name: "Command Center", note: "Dedicated multi-column command layout", tone: "#36aaff"}
                    ]
                    Rectangle {
                        id: choice
                        objectName: "package_" + modelData.key
                        width: parent.width; height: 64; radius: 8
                        readonly property bool selected: bridge.theme === modelData.key
                        color: selected ? Theme.alpha(Theme.materialBlue, 0.12) : "#071522"
                        border.width: 1; border.color: Theme.alpha(Theme.materialBlue, selected ? 0.7 : 0.28)
                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button; Accessible.name: modelData.name
                        Keys.onSpacePressed: bridge.setTheme(modelData.key)
                        Keys.onReturnPressed: bridge.setTheme(modelData.key)
                        Rectangle { x: 12; y: 12; width: 42; height: 40; radius: 5; color: "#031019"; border.width: 1; border.color: modelData.tone
                            Rectangle { x: 5; y: 8; width: modelData.key === "command" ? 9 : 21; height: 24; color: Theme.alpha(modelData.tone, 0.25) }
                            Rectangle { x: modelData.key === "command" ? 17 : 29; y: 8; width: modelData.key === "command" ? 10 : 7; height: 24; color: Theme.alpha(modelData.tone, 0.45) }
                            Rectangle { visible: modelData.key === "command"; x: 30; y: 8; width: 7; height: 24; color: Theme.alpha(modelData.tone, 0.22) }
                        }
                        Column { x: 68; anchors.verticalCenter: parent.verticalCenter; spacing: 3
                            Text { text: modelData.name; color: Theme.text; font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold } }
                            Text { text: modelData.note; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                        }
                        Text { anchors.right: parent.right; anchors.rightMargin: 14; anchors.verticalCenter: parent.verticalCenter; text: choice.selected ? "✓" : ""; color: Theme.materialBlue }
                        Rectangle { anchors.fill: parent; radius: parent.radius; color: "transparent"; border.color: Theme.materialBlue; visible: choice.activeFocus }
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setTheme(modelData.key) }
                    }
                }
            }
            Row { width: parent.width; spacing: 18
                Text { text: "Ambient effects"; width: 190; anchors.verticalCenter: parent.verticalCenter; color: Theme.textLabel; font { family: Theme.font; pixelSize: 13 } }
                Segmented { accent: Theme.materialBlue; value: bridge.ambientEffects ? "on" : "off"; options: [{value:"on",label:"On"},{value:"off",label:"Off"}]; onPicked: function(v) { bridge.setAmbientEffects(v === "on") } }
            }
            Row { width: parent.width; spacing: 18
                Text { text: "Status indicator"; width: 190; anchors.verticalCenter: parent.verticalCenter; color: Theme.textLabel; font { family: Theme.font; pixelSize: 13 } }
                Segmented { accent: Theme.materialBlue; objectName: "statusIndicatorChoice"; value: bridge.statusIndicator; options: [{value:"orb",label:"Canonical Orb"},{value:"hud",label:"HUD Orb"}]; onPicked: function(v) { bridge.setStatusIndicator(v) } }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.border }
            Label { text: "THIS PACKAGE’S LAYOUT" }
            Segmented { accent: Theme.materialBlue; value: bridge.profile; options: [{value:"normal",label:"Normal"},{value:"focus",label:"Focus"},{value:"monitoring",label:"Monitoring"}]; onPicked: function(v) { bridge.setProfile(v) } }
            Row { spacing: 10
                ActionButton { accent: Theme.materialBlue; label: "Edit panels"; compact: true; onClicked: { bridge.setEditMode(true); settings.open = false } }
                ActionButton { accent: Theme.materialBlue; label: "Reset this layout"; compact: true; onClicked: bridge.resetLayout() }
            }
            Text { width: parent.width; wrapMode: Text.Wrap; text: "Each package keeps its own layout. Ctrl+Shift+F temporarily focuses the workspace without replacing it."; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            Rectangle { width: parent.width; height: 1; color: Theme.border }
            Row { spacing: 12
                Label { text: "EXECUTION"; anchors.verticalCenter: parent.verticalCenter }
                Pill { text: bridge.configRefused ? "config refused" : bridge.executionEnabled ? "enabled" : "disabled"; tint: bridge.executionEnabled ? Theme.success : Theme.danger }
            }
            Text { width: parent.width; wrapMode: Text.Wrap; text: "Permission mode: " + (bridge.config.permission_mode || "unavailable") + ". The red power control always asks for confirmation."; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
        }
    }
}
