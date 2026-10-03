import QtQuick
import "../theme"

// A segmented choice: one well, the selected segment lit. Used by Settings
// for theme / ambient / profile / effort. `options` is a list of
// { value, label }; `value` is the current value; `picked(value)` fires.
Rectangle {
    id: seg
    property var options: []
    property string value: ""
    property color accent: Theme.accent
    signal picked(string value)
    implicitHeight: 34
    implicitWidth: row.width + 6
    radius: Theme.radiusSmall
    color: Theme.surfaceDeep
    border.width: 1
    border.color: Theme.border
    Row {
        id: row
        anchors.verticalCenter: parent.verticalCenter
        x: 3
        spacing: 2
        Repeater {
            model: seg.options
            Rectangle {
                readonly property bool on: modelData.value === seg.value
                readonly property bool hovered: m.containsMouse && seg.enabled
                width: t.width + 24; height: 28; radius: Theme.radiusSmall - 3
                color: on ? Theme.alpha(seg.accent, 0.18) : hovered ? Theme.alpha(Theme.text, 0.05) : "transparent"
                border.width: 1; border.color: on ? Theme.alpha(seg.accent, 0.6) : "transparent"
                Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                Text { id: t; anchors.centerIn: parent; text: modelData.label; color: on ? seg.accent : (hovered ? Theme.text : Theme.textLabel)
                    font: on ? Theme.captionStrong : Theme.caption }
                MouseArea { id: m; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: seg.picked(modelData.value) }
            }
        }
    }
    opacity: enabled ? 1 : 0.38
    // Keyboard focus (Gate 6E): the ActionButton ring - crisp, 2 px, outside the well.
    activeFocusOnTab: true
    Rectangle { objectName: "segmentedFocusRing"; visible: seg.activeFocus; anchors.fill: parent; anchors.margins: -4
                radius: parent.radius + 4; color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
}
