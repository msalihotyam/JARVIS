import QtQuick
import "../theme"

// Gate 6G K4: THE menu row, inside a MenuSurface. Theme.menuRowHeight (a
// `detail` line under the label makes it taller); an icon slot (K1 Icon), the
// label, an optional shortcut / secondary text at the right and a drawn check.
// States from Theme: hover, pressed, current (the 2 px accent mark), keyboard
// focus (a fine edge, never colour alone), disabled (faded, with its reason
// beside it where the menu gives one) and destructive (danger while enabled).
// Enter / Space or a click choose it; Esc reaches its MenuSurface, which closes
// and returns focus to the opener; Up / Down move through the menu there.
Rectangle {
    id: row
    property string icon: ""
    property string label: ""
    readonly property string text: label      // a Menu item's name, for the readers that use it
    property string shortcut: ""
    property string detail: ""
    property bool current: false
    property bool checked: false
    property bool available: true
    property bool destructive: false
    property bool action: false               // an action rather than a choice: reads in accent
    property bool muted: false                // an untitled one: reads in textLabel
    readonly property bool menuRow: true
    signal triggered()

    width: parent ? parent.width : 0
    implicitHeight: detail !== "" ? Math.max(Theme.menuRowHeight, body.implicitHeight + 16) : Theme.menuRowHeight
    height: implicitHeight
    radius: Theme.radiusTiny
    enabled: available
    opacity: enabled ? 1 : Theme.menuRowDisabled
    color: area.pressed ? Theme.menuRowPressed : current ? Theme.menuRowCurrent
           : area.containsMouse || activeFocus ? Theme.menuRowHover : "transparent"
    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    border.width: activeFocus ? 1 : 0
    border.color: Theme.glassEdgeStrong
    activeFocusOnTab: enabled && visible
    Accessible.role: Accessible.MenuItem
    Accessible.name: label
    Accessible.checked: checked
    Accessible.onPressAction: if (enabled) triggered()
    // Esc is the menu's (MenuSurface), before the window's own Escape chain sees it.
    Keys.onShortcutOverride: function (event) { if (event.key === Qt.Key_Escape) event.accepted = true }
    Keys.onReturnPressed: if (enabled) triggered()
    Keys.onEnterPressed: if (enabled) triggered()
    Keys.onSpacePressed: if (enabled) triggered()

    readonly property color ink: destructive && enabled ? Theme.danger : action ? Theme.accent : muted ? Theme.textLabel : Theme.text
    Rectangle { visible: row.current; x: 0; y: 8; width: 2; height: parent.height - 16; radius: 1; color: Theme.accent }
    Icon { visible: row.icon !== ""; x: 12; anchors.verticalCenter: parent.verticalCenter; name: row.icon; size: 16
           tint: row.destructive && row.enabled ? Theme.danger : Theme.accent }
    Column {
        id: body
        x: row.icon !== "" ? 38 : 12
        width: row.width - x - 12 - (row.shortcut !== "" ? tail.implicitWidth + 10 : 0)
        anchors.verticalCenter: parent.verticalCenter
        spacing: 2
        Row {
            spacing: 8
            Text { id: labelText; width: Math.min(implicitWidth, body.width - (row.checked ? 20 : 0)); text: row.label
                   textFormat: Text.PlainText; elide: Text.ElideRight; font: Theme.body; color: row.ink }
            Icon { visible: row.checked; anchors.verticalCenter: parent.verticalCenter; name: "check"; size: 12; tint: Theme.text }
        }
        Text { visible: row.detail !== ""; width: body.width; text: row.detail; textFormat: Text.PlainText
               wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.caption }
    }
    Text { id: tail; visible: row.shortcut !== ""; anchors.right: parent.right; anchors.rightMargin: 12
           anchors.verticalCenter: parent.verticalCenter; text: row.shortcut; color: Theme.textDim; font: Theme.caption }
    MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                onClicked: row.triggered() }
    HoverTip { text: row.label; active: labelText.truncated }
}
