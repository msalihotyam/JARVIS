import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

ContextPanel {
    id: settings
    // Above the interactive summary rail (420), below permanent top controls.
    z: 430
    surfaceEdge: Theme.materialBlue
    // A foreground surface like the model panel: the workspace behind it does
    // not read through its body. One edge, the panel's own shadow, no halo.
    surfaceFill: Theme.glassOverlay
    heading: qsTr("GENERAL SETTINGS")
    surfaceName: "settingsSurface"
    panelWidth: 460; panelHeight: 671
    // E2: the one edit-mode entry, in this panel's header immediately left of
    // its X (the content starts 20 px under the 34 px heading). Turning edit
    // mode on closes the panel; the "Düzenleme modu · Bitti" pill ends it.
    IconButton {
        objectName: "editLayoutTrigger"
        x: parent.width - 34 - 6 - width; y: -20 - 34
        icon: "sliders"; framed: true
        tip: bridge.editMode ? qsTr("Done editing") : qsTr("Edit workspace layout")
        active: bridge.editMode
        onClicked: { var on = !bridge.editMode; bridge.setEditMode(on); if (on) settings.open = false }
    }
    Flickable {
        anchors.fill: parent; clip: true; contentHeight: sections.height
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
            id: sections; width: parent.width; spacing: 17
            component Label: Text { color: Theme.textMuted; font: Theme.label }
            // Language / Dil: the option names are always shown in their own language.
            Row { width: parent.width; spacing: 18
                Label { text: qsTr("LANGUAGE"); width: 190; anchors.verticalCenter: parent.verticalCenter }
                Segmented { accent: Theme.materialBlue; objectName: "languageChoice"; value: bridge.language
                    options: i18n.languages.map(function(l) { return { value: l.id, label: l.name } })
                    onPicked: function(v) { bridge.setLanguage(v) } }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.border }
            Label { text: qsTr("PRESENTATION PACKAGE") }
            Column { width: parent.width; spacing: 8
                Repeater {
                    model: [
                        {key: "space", name: qsTr("Canonical Space"), note: qsTr("Orbital glass · primary JARVIS composition"), tone: "#1bbdff"},
                        {key: "dark", name: qsTr("Dark"), note: qsTr("Near-black glass · restrained light"), tone: "#476e82"},
                        {key: "command", name: qsTr("Command Center"), note: qsTr("Dedicated multi-column command layout"), tone: "#36aaff"}
                    ]
                    Rectangle {
                        id: choice
                        objectName: "package_" + modelData.key
                        width: parent.width; height: 64; radius: 8
                        readonly property bool selected: bridge.theme === modelData.key
                        // A nested choice: one small tonal step from the panel; the chosen one
                        // keeps its stable fill, its edge and its mark.
                        color: selected ? Theme.alpha(Theme.materialBlue, 0.12) : Theme.glassNested
                        border.width: 1; border.color: selected ? Theme.alpha(Theme.materialBlue, 0.6) : Theme.glassNestedEdge
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
                            Text { text: modelData.name; color: Theme.text; font: Theme.bodyStrong }
                            Text { text: modelData.note; color: Theme.textMuted; font: Theme.caption }
                        }
                        Icon { anchors.right: parent.right; anchors.rightMargin: 14; anchors.verticalCenter: parent.verticalCenter; visible: choice.selected; name: "check"; size: 14; tint: Theme.materialBlue }
                        Rectangle { anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1; color: "transparent"; border.width: 2; border.color: Theme.materialGlint; visible: choice.activeFocus }
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setTheme(modelData.key) }
                    }
                }
            }
            Row { width: parent.width; spacing: 18
                Text { text: qsTr("Ambient effects"); width: 190; anchors.verticalCenter: parent.verticalCenter; color: Theme.textLabel; font: Theme.body }
                Segmented { accent: Theme.materialBlue; value: bridge.ambientEffects ? "on" : "off"; options: [{value:"on",label:qsTr("On")},{value:"off",label:qsTr("Off")}]; onPicked: function(v) { bridge.setAmbientEffects(v === "on") } }
            }
            // Three skins (6G S1 adds the mechanical lamp): the choice sits under the label, like the freeze key.
            Column { width: parent.width; spacing: 8
                Text { text: qsTr("Status indicator"); color: Theme.textLabel; font: Theme.body }
                Segmented { accent: Theme.materialBlue; objectName: "statusIndicatorChoice"; segmentPrefix: "statusIndicator_"; value: bridge.statusIndicator
                    options: [{value:"mechanical",label:qsTr("Mechanical")},{value:"orb",label:qsTr("Canonical Orb")},{value:"hud",label:qsTr("HUD Orb")}]
                    onPicked: function(v) { bridge.setStatusIndicator(v) } }
            }
            // Four skins do not fit beside the 190 px label: the choice sits under it.
            Column { width: parent.width; spacing: 8
                Text { text: qsTr("Freeze key"); color: Theme.textLabel; font: Theme.body }
                Segmented { accent: Theme.materialBlue; objectName: "freezeKeyChoice"; segmentPrefix: "freezeKey_"; value: String(bridge.freezeKeySkin)
                    options: [{value:"1",label:qsTr("Classic")},{value:"2",label:qsTr("Key")},{value:"3",label:qsTr("Toggle")},{value:"4",label:qsTr("Knife switch")}]
                    onPicked: function(v) { bridge.setFreezeKeySkin(Number(v)) } }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.border }
            Label { text: qsTr("THIS PACKAGE’S LAYOUT") }
            Segmented { accent: Theme.materialBlue; value: bridge.profile; options: [{value:"normal",label:qsTr("Normal")},{value:"focus",label:qsTr("Focus")},{value:"monitoring",label:qsTr("Monitoring")}]; onPicked: function(v) { bridge.setProfile(v) } }
            Row { spacing: 10
                ActionButton { accent: Theme.materialBlue; label: qsTr("Reset this layout"); compact: true; onClicked: bridge.resetLayout() }
            }
            Text { width: parent.width; wrapMode: Text.Wrap; text: qsTr("Each package keeps its own layout. %1 temporarily focuses the workspace without replacing it.").arg("Ctrl+Shift+F"); color: Theme.textMuted; font: Theme.caption }
            Rectangle { width: parent.width; height: 1; color: Theme.border }
            Row { spacing: 12
                Label { text: qsTr("EXECUTION"); anchors.verticalCenter: parent.verticalCenter }
                Pill { text: bridge.configRefused ? qsTr("config refused") : bridge.executionEnabled ? qsTr("running") : qsTr("frozen"); tint: bridge.executionEnabled ? Theme.success : Theme.danger }
            }
            Text { width: parent.width; wrapMode: Text.Wrap; text: qsTr("Permission mode: %1. The red control freezes JARVIS at once; resuming asks for confirmation.").arg(bridge.config.permission_mode || qsTr("unavailable")); color: Theme.textMuted; font: Theme.caption }
        }
    }
}
