import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Usable-v1 settings: theme, ambient effects, the existing validated
// model / effort configuration, layout profile and reset. No raw editor.
GlassPanel {
    id: page
    title: "Settings"

    component SettingRow: Row {
        property string label: ""
        default property alias content: slot.data
        spacing: 16; width: parent.width
        Text { text: label.toUpperCase(); width: 200; color: Theme.textMuted; anchors.verticalCenter: parent.verticalCenter; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        Row { id: slot; spacing: 8 }
    }
    component Choice: ActionButton { property bool selected: false; primary: selected }

    Column {
        anchors.fill: parent
        spacing: 22
        SettingRow { label: "Theme"
            Choice { label: "Canonical Space"; selected: bridge.theme === "space"; onClicked: bridge.setTheme("space") }
            Choice { label: "Dark"; selected: bridge.theme === "dark"; onClicked: bridge.setTheme("dark") }
        }
        SettingRow { label: "Ambient effects"
            Choice { label: "On"; selected: bridge.ambientEffects; onClicked: bridge.setAmbientEffects(true) }
            Choice { label: "Off"; selected: !bridge.ambientEffects; onClicked: bridge.setAmbientEffects(false) }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "Off disables optional atmosphere only; state, approval and theme transitions stay."; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
        }
        SettingRow { label: "Layout profile"
            Choice { label: "Normal"; selected: bridge.profile === "normal"; onClicked: bridge.setProfile("normal") }
            Choice { label: "Focus"; selected: bridge.profile === "focus"; onClicked: bridge.setProfile("focus") }
            Choice { label: "Monitoring"; selected: bridge.profile === "monitoring"; onClicked: bridge.setProfile("monitoring") }
            ActionButton { label: "Reset this layout"; onClicked: bridge.resetLayout() }
        }
        Rectangle { width: parent.width; height: 1; color: Theme.border }
        SettingRow { label: "Provider"
            Text { anchors.verticalCenter: parent.verticalCenter; text: "claude — the Claude Code CLI on the subscription route (the only wired provider)"; color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
        }
        SettingRow { label: "Model"
            Rectangle { width: 260; height: 34; radius: Theme.radiusSmall; color: Theme.surfaceRaised; border.width: 1; border.color: modelField.activeFocus ? Theme.accent : Theme.border
                TextField { id: modelField; anchors.fill: parent; anchors.margins: 4; background: Item {}  color: Theme.text; font { family: Theme.font; pixelSize: 13 }
                    placeholderText: "CLI default"; placeholderTextColor: Theme.textDim; text: bridge.config.claude_model || "" } }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "letters, digits, dot, underscore, hyphen; empty = CLI default"; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
        }
        SettingRow { label: "Effort"
            Choice { label: "CLI default"; selected: !bridge.config.claude_effort && effortPick.value === ""; onClicked: effortPick.value = "" }
            Repeater { model: bridge.effortChoices
                Choice { label: modelData; selected: effortPick.value === modelData; onClicked: effortPick.value = modelData } }
        }
        QtObject { id: effortPick; property string value: bridge.config.claude_effort || "" }
        SettingRow { label: ""
            ActionButton { label: "Apply model / effort"; primary: true; onClicked: bridge.applyModel(modelField.text, effortPick.value) }
        }
        Rectangle { width: parent.width; height: 1; color: Theme.border }
        SettingRow { label: "Execution"
            Text { anchors.verticalCenter: parent.verticalCenter
                text: bridge.configRefused ? "Configuration refused: fail-closed. Repair with 'jarvis config reset --all'."
                    : (bridge.executionEnabled ? "Enabled. The kill switch at the top disables it persistently." : "DISABLED by the kill switch. Re-enable from the top bar (asks for confirmation).")
                color: bridge.executionEnabled && !bridge.configRefused ? Theme.text : Theme.danger; font { family: Theme.font; pixelSize: 13 } }
        }
        SettingRow { label: "Permission mode"
            Text { anchors.verticalCenter: parent.verticalCenter; text: (bridge.config.permission_mode || "—") + "   (change it with 'jarvis config set permission_mode ...')"; color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
        }
        Text { text: "Voice, Calendar, Drive, Slack and artifact generation are deferred and not configurable here."; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
    }
}
