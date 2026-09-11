import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Settings as a contextual drawer from the right. It never takes the page
// away: open from the orb / Ctrl+6 / menu / palette, close with the ✕, Esc,
// a click outside, or the orb again. Theme, ambient effects, the existing
// validated model / effort configuration, layout profile and reset.
Item {
    id: drawer
    anchors.fill: parent
    property bool open: false
    visible: open || dim.opacity > 0
    z: 350

    onOpenChanged: {
        if (open) {
            // Deterministic: the fields show the persisted configuration on every open.
            modelField.text = bridge.config.claude_model || ""
            effortPick.value = bridge.config.claude_effort || ""
            panel.forceActiveFocus()
        }
    }

    Rectangle {
        id: dim
        anchors.fill: parent; color: "#000000"
        opacity: drawer.open ? 0.38 : 0
        Behavior on opacity { NumberAnimation { duration: drawer.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } }
        MouseArea { anchors.fill: parent; enabled: drawer.open; onClicked: drawer.open = false }
    }

    GlassPanel {
        id: panel
        raised: true
        width: Math.min(560, parent.width * 0.38)
        anchors { right: parent.right; top: parent.top; bottom: parent.bottom; margins: 16 }
        padding: 26
        opacity: drawer.open ? 1 : 0
        transform: Translate { x: drawer.open ? 0 : 36; Behavior on x { NumberAnimation { duration: drawer.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } } }
        Behavior on opacity { NumberAnimation { duration: drawer.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } }
        Keys.onEscapePressed: drawer.open = false
        MouseArea { anchors.fill: parent; z: -1 }   // clicks inside stay inside

        // Header
        Item {
            id: head
            width: parent.width; height: 40
            Row { spacing: 12; anchors.verticalCenter: parent.verticalCenter
                Rectangle { width: 10; height: 10; radius: 5; color: Theme.accent; anchors.verticalCenter: parent.verticalCenter
                    Rectangle { anchors.centerIn: parent; width: 20; height: 20; radius: 10; color: Theme.accent; opacity: 0.22 } }
                Text { text: "SETTINGS"; color: Theme.text; anchors.verticalCenter: parent.verticalCenter; font { family: Theme.font; pixelSize: 13; letterSpacing: 3.4; weight: Font.DemiBold } }
            }
            Row { spacing: 8; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                Text { text: "ESC"; color: Theme.textDim; anchors.verticalCenter: parent.verticalCenter; font { family: Theme.font; pixelSize: 9; letterSpacing: 2 } }
                IconButton { objectName: "settingsClose"; glyph: "✕"; tip: ""; framed: true; width: 34; height: 34; onClicked: drawer.open = false }
            }
        }
        Rectangle { anchors { left: parent.left; right: parent.right; top: head.bottom; topMargin: 8 } height: 1
            gradient: Gradient { orientation: Gradient.Horizontal
                GradientStop { position: 0; color: Theme.borderStrong } GradientStop { position: 1; color: "transparent" } } }

        Flickable {
            anchors { left: parent.left; right: parent.right; top: head.bottom; bottom: parent.bottom; topMargin: 26 }
            contentHeight: sections.height; clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
                id: sections
                width: parent.width
                spacing: 22

                component Section: Column {
                    property string title: ""
                    default property alias rows: well.data
                    width: sections.width; spacing: 10
                    Text { text: title.toUpperCase(); color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2.6; weight: Font.DemiBold } }
                    Rectangle {
                        width: parent.width; height: well.height + 2
                        radius: 12; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.border
                        Column { id: well; x: 1; y: 1; width: parent.width - 2 }
                    }
                }
                component SettingRow: Item {
                    property string label: ""
                    property string hint: ""
                    property bool last: false
                    default property alias control: slot.data
                    width: parent.width; height: Math.max(54, slot.height + 22, hint !== "" ? 62 : 0)
                    Column { anchors.left: parent.left; anchors.leftMargin: 16; anchors.verticalCenter: parent.verticalCenter; spacing: 3; width: 150
                        Text { text: label; color: Theme.textLabel; font { family: Theme.font; pixelSize: 13 } }
                        Text { visible: hint !== ""; text: hint; color: Theme.textDim; width: 150; wrapMode: Text.Wrap; font { family: Theme.font; pixelSize: 10 } }
                    }
                    Row { id: slot; anchors.right: parent.right; anchors.rightMargin: 14; anchors.verticalCenter: parent.verticalCenter; spacing: 8 }
                    Rectangle { visible: !last; anchors { left: parent.left; right: parent.right; bottom: parent.bottom; leftMargin: 16; rightMargin: 14 } height: 1; color: Theme.border; opacity: 0.6 }
                }

                Section { title: "Appearance"
                    SettingRow { label: "Theme"
                        Segmented { value: bridge.theme; onPicked: function(v) { bridge.setTheme(v) }
                            options: [ { value: "space", label: "Canonical Space" }, { value: "dark", label: "Dark" } ] } }
                    SettingRow { label: "Ambient effects"; hint: "Off removes optional atmosphere only; state, approval and theme transitions stay."; last: true
                        Segmented { value: bridge.ambientEffects ? "on" : "off"; onPicked: function(v) { bridge.setAmbientEffects(v === "on") }
                            options: [ { value: "on", label: "On" }, { value: "off", label: "Off" } ] } }
                }
                Section { title: "Layout"
                    SettingRow { label: "Profile"
                        Segmented { value: bridge.profile; onPicked: function(v) { bridge.setProfile(v) }
                            options: [ { value: "normal", label: "Normal" }, { value: "focus", label: "Focus" }, { value: "monitoring", label: "Monitoring" } ] } }
                    SettingRow { label: "Panels"; hint: "Ctrl+E edits the right column."; last: true
                        ActionButton { label: "Reset this layout"; compact: true; onClicked: bridge.resetLayout() } }
                }
                Section { title: "Model"
                    SettingRow { label: "Provider"
                        Text { anchors.verticalCenter: parent.verticalCenter; text: "claude · Claude Code CLI (subscription route)"; color: Theme.text; font { family: Theme.font; pixelSize: 13 } } }
                    SettingRow { label: "Model"; hint: "letters, digits, dot, underscore, hyphen; empty = CLI default"
                        Rectangle { width: 250; height: 34; radius: Theme.radiusSmall; color: Theme.surfaceRaised; border.width: 1; border.color: modelField.activeFocus ? Theme.accent : Theme.border
                            Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
                            TextField { id: modelField; objectName: "modelField"; anchors.fill: parent; anchors.margins: 4; leftPadding: 8; background: Item {}  color: Theme.text; font { family: Theme.font; pixelSize: 13 }
                                placeholderText: "CLI default"; placeholderTextColor: Theme.textDim; selectByMouse: true } } }
                    SettingRow { label: "Effort"
                        Segmented { id: effortSeg; value: effortPick.value; onPicked: function(v) { effortPick.value = v }
                            options: [ { value: "", label: "CLI default" } ].concat(bridge.effortChoices.map(function(e) { return { value: e, label: e } })) } }
                    SettingRow { label: ""; last: true
                        ActionButton { label: "Apply model / effort"; primary: true; compact: true; onClicked: bridge.applyModel(modelField.text, effortPick.value) } }
                }
                QtObject { id: effortPick; property string value: "" }
                Section { title: "Execution"
                    SettingRow { label: "State"
                        Pill { anchors.verticalCenter: parent.verticalCenter
                            text: bridge.configRefused ? "config refused" : (bridge.executionEnabled ? "enabled" : "disabled")
                            tint: bridge.executionEnabled && !bridge.configRefused ? Theme.success : Theme.danger } }
                    SettingRow { label: "Kill switch"; hint: "Top bar. Persistent; asks for confirmation both ways."
                        Text { anchors.verticalCenter: parent.verticalCenter; width: 250; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignRight
                            text: bridge.configRefused ? "Fail-closed. Repair with: jarvis config reset --all" : (bridge.executionEnabled ? "No new work starts once it is thrown." : "Re-enable from the top bar.")
                            color: bridge.executionEnabled && !bridge.configRefused ? Theme.textLabel : Theme.danger; font { family: Theme.font; pixelSize: 12 } } }
                    SettingRow { label: "Permission mode"; last: true
                        Text { anchors.verticalCenter: parent.verticalCenter; text: bridge.config.permission_mode || "—"; color: Theme.text; font { family: Theme.mono; pixelSize: 12 } } }
                }
                Text { width: parent.width; wrapMode: Text.Wrap; text: "Permission mode changes with `jarvis config set permission_mode …`. Voice, Calendar, Drive, Slack and artifact generation are deferred and not configurable here."; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
                Item { width: 1; height: 6 }
            }
        }
    }
}
