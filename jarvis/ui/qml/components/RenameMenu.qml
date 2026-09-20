import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Right-click a task or a session: "Rename". The name is the person's own label
// and always wins over the automatic one; the id underneath never changes. An
// empty name or a cancel leaves the current name as it is.
Menu {
    id: menu
    objectName: "renameMenu"
    property string targetId: ""
    property string current: ""
    function openFor(item, position, id, title) {
        if (!id) return
        targetId = id; current = title || ""
        popup(item, position)
    }

    padding: 6
    implicitWidth: 200
    background: Rectangle {
        implicitWidth: 200
        radius: 8
        color: Theme.glassMenuVeil
        border.width: 1
        border.color: Theme.glassOverlayEdge
    }
    MenuItem {
        id: action
        objectName: "renameAction"
        text: qsTr("Rename")
        implicitHeight: 34
        leftPadding: 12; rightPadding: 12
        contentItem: Text { text: action.text; color: Theme.text; renderType: Theme.textRender
                            verticalAlignment: Text.AlignVCenter; font: Theme.body }
        background: Rectangle { radius: 6; color: action.highlighted ? Theme.alpha("#36aaff", 0.12) : "transparent"
                                border.width: action.highlighted ? 1 : 0; border.color: Theme.alpha("#49bdfa", 0.4) }
        onTriggered: menu.dialog.openFor(menu.targetId, menu.current)
    }

    property Popup dialog: Popup {
        objectName: "renameDialog"
        property string targetId: ""
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: 380; padding: 18
        modal: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        function openFor(id, title) { targetId = id; field.text = title; open(); field.selectAll(); field.forceActiveFocus() }
        function save() { bridge.renameTask(targetId, field.text); close() }
        background: Rectangle { radius: 12; color: Theme.glassOverlay; border.width: 1; border.color: Theme.glassOverlayEdge }
        contentItem: Column {
            spacing: 12
            Text { text: qsTr("Rename"); color: Theme.text; renderType: Theme.textRender; font: Theme.uiValue }
            TextField {
                id: field
                objectName: "renameField"
                width: parent.width
                maximumLength: 120
                selectByMouse: true
                color: Theme.text
                font: Theme.body
                background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                        border.color: field.activeFocus ? Theme.commandEdge : Theme.border }
                onAccepted: menu.dialog.save()
            }
            Row {
                anchors.right: parent.right
                spacing: 8
                ActionButton { objectName: "renameCancel"; label: qsTr("Cancel"); height: 36; width: 110; onClicked: menu.dialog.close() }
                ActionButton { objectName: "renameSave"; label: qsTr("Save"); primary: true; height: 36; width: 110
                               onClicked: menu.dialog.save() }
            }
        }
    }
}
