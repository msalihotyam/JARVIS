import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6G Sidebar v2 V4: the New chat screen's project picker, under the
// composer. Its chip ("Proje seç ⌄", or the chosen project's name) opens a K4
// menu: a search field, "Proje yok", the active projects the search matches,
// "Yeni proje…" (the existing new-project dialog). A choice is the owner of
// the open, still empty conversation (bridge.setNewChatProject); the first
// message fixes it, and the New chat screen - with this picker - is gone.
Item {
    id: picker
    objectName: "projectPicker"
    readonly property var owner: bridge.conversationProject || ({})
    readonly property bool none: !!owner.global
    property string query: ""
    // The active projects the search matches, in the sidebar's order (DRAG).
    readonly property var choices: {
        var order = bridge.sidebarProjectOrder, needle = query.trim().toLocaleLowerCase()
        return bridge.projectRecords.filter(function (p) { return p.active && p.name.toLocaleLowerCase().indexOf(needle) >= 0 })
                                    .sort(function (a, b) { return order.indexOf(a.id) - order.indexOf(b.id) })
    }
    implicitHeight: 32

    function choose(id) { menu.close(); bridge.setNewChatProject(id) }
    function toggle() {
        if (menu.visible) { menu.close(); return }
        search.text = ""
        menu.popupAt(chip, Qt.point(0, chip.height + 6), { trigger: chip, flip: true })
    }

    Rectangle {
        id: chip
        objectName: "projectPickerChip"
        width: chipRow.width + 20; height: 32; radius: Theme.radiusSmall
        color: chipArea.containsMouse || menu.visible ? Theme.alpha(Theme.text, 0.06) : "transparent"
        border.width: 1; border.color: menu.visible ? Theme.glassEdgeStrong : Theme.glassEdge
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.ComboBox
        Accessible.name: chipLabel.text
        Accessible.onPressAction: picker.toggle()
        Keys.onReturnPressed: picker.toggle()
        Keys.onSpacePressed: picker.toggle()
        Row {
            id: chipRow
            x: 10; anchors.verticalCenter: parent.verticalCenter; spacing: 6
            Icon { anchors.verticalCenter: parent.verticalCenter; name: "folder"; size: 14
                   tint: picker.none ? Theme.textLabel : Theme.accent }
            Text { id: chipLabel; objectName: "projectPickerLabel"; anchors.verticalCenter: parent.verticalCenter
                   width: Math.min(implicitWidth, 220); elide: Text.ElideRight; textFormat: Text.PlainText
                   text: picker.none ? qsTr("Choose project") : picker.owner.name || ""; color: Theme.text; font: Theme.caption }
            Icon { anchors.verticalCenter: parent.verticalCenter; name: "chevron-down"; size: 12; tint: Theme.textMuted }
        }
        Rectangle { visible: chip.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: parent.radius
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: chipArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: picker.toggle() }
    }
    Text {
        objectName: "projectPickerHint"
        x: chip.width + 10; anchors.verticalCenter: parent.verticalCenter
        width: Math.max(0, picker.width - x); elide: Text.ElideRight; textFormat: Text.PlainText
        text: picker.none ? qsTr("Without a project the conversation stays outside projects")
                          : qsTr("The conversation starts in this project")
        color: Theme.textDim; font: Theme.caption
    }

    MenuSurface {
        id: menu
        objectName: "projectPickerMenu"
        width: 320
        initialFocus: search
        Column {
            width: menu.availableWidth
            spacing: 2
            TextField {
                id: search
                objectName: "projectPickerSearch"
                width: parent.width; height: 34; selectByMouse: true; color: Theme.text; font: Theme.body
                placeholderText: qsTr("Search projects"); placeholderTextColor: Theme.textDim
                background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                        border.color: search.activeFocus ? Theme.commandEdge : Theme.border }
                onTextChanged: picker.query = text
                Keys.onDownPressed: menu.step(1)
            }
            MenuRow { objectName: "projectPickerNone"; icon: "chat"; label: qsTr("No project")
                      checked: picker.none; onTriggered: picker.choose("") }
            // The projects: at most eight rows tall, the rest scrolls.
            Flickable {
                width: parent.width; height: Math.min(contentHeight, 8 * (Theme.menuRowHeight + 2))
                contentHeight: choiceColumn.height; clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                Column {
                    id: choiceColumn
                    width: parent.width; spacing: 2
                    Repeater {
                        model: picker.choices
                        MenuRow { objectName: "projectPickerItem"; icon: "folder"; label: modelData.name
                                  checked: !picker.none && picker.owner.id === modelData.id
                                  onTriggered: picker.choose(modelData.id) }
                    }
                }
            }
            Text { objectName: "projectPickerNoMatch"; visible: picker.choices.length === 0 && picker.query.trim() !== ""
                   leftPadding: 12; text: qsTr("No project matches"); color: Theme.textMuted; font: Theme.caption }
            Rectangle { width: parent.width; height: 1; color: Theme.glassEdge }
            MenuRow { objectName: "projectPickerNew"; icon: "plus"; label: qsTr("New project…"); action: true
                      onTriggered: { menu.close(); bridge.requestNewProject() } }
        }
    }
}
