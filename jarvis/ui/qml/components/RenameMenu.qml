import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Right-click a task or a session: "Rename". The name is the person's own label
// and always wins over the automatic one; the id underneath never changes. An
// empty name or a cancel leaves the current name as it is. K4: the one menu
// surface and row, at the pointer; Esc returns focus to the row.
MenuSurface {
    id: menu
    objectName: "renameMenu"
    property string targetId: ""
    property string current: ""
    function openFor(item, position, id, title) {
        if (!id) return
        targetId = id; current = title || ""; origin = item
        popupAt(item, position, { flip: true })
    }

    width: 200
    padding: 6
    MenuRow {
        objectName: "renameAction"
        width: menu.availableWidth
        icon: "pencil"
        label: qsTr("Rename")
        onTriggered: { menu.close(); menu.dialog.openFor(menu.targetId, menu.current) }
    }

    // Gate 6E: on the shared dialog shell (accent tone). Focus starts in the
    // field with the name selected; Enter there saves, Esc cancels, and focus
    // returns to the row that was right-clicked.
    property Item origin: null
    property JarvisDialog dialog: JarvisDialog {
        id: renameDialog
        objectName: "renameDialog"
        property string targetId: ""
        preferredWidth: 420
        label: qsTr("Rename")
        initialFocus: field
        confirmItem: saveButton
        enterConfirms: true
        function openFor(id, title) { targetId = id; field.text = title; openFrom(menu.origin); field.selectAll() }
        function save() { bridge.renameTask(targetId, field.text); close() }
        TextField {
            id: field
            objectName: "renameField"
            width: parent ? parent.width : 0; height: 36
            maximumLength: 120
            selectByMouse: true
            color: Theme.text
            font: Theme.body
            background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                    border.color: field.activeFocus ? Theme.commandEdge : Theme.border }
        }
        buttons: [
            ActionButton { objectName: "renameCancel"; label: qsTr("Cancel"); height: 36; width: 110; onClicked: renameDialog.close() },
            ActionButton { id: saveButton; objectName: "renameSave"; label: qsTr("Save"); primary: true; height: 36; width: 110
                           onClicked: renameDialog.save() }
        ]
    }
}
