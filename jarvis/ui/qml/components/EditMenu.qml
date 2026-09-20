import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The JARVIS edit menu for a text editor: Undo, Cut, Copy, Paste, Select All,
// each enabled only when it can really do something. On a read-only text (a
// message in the conversation) it is Copy and Select All only, and Copy with
// nothing selected takes the whole message (`wholeText`).
//
// Paste is NOT decided here. A plain editor pastes text. The main composer sets
// `attachAware`, and Paste then asks its owner (`pasteRequested`), which routes
// it through the one attachment-aware decision Ctrl+V uses - so the keyboard
// and the menu can never behave differently.
//
// It opens AT the pointer (`openAt`): below and to the right of it, flipped
// above or to the left where the window ends. Labels only - the shortcuts are
// the platform's own and need no reminder here.
Menu {
    id: menu
    property Item editor: null
    property bool attachAware: false
    // What Copy takes from a read-only text when nothing is selected.
    property string wholeText: ""
    signal pasteRequested()

    readonly property bool readOnlyText: editor !== null && editor.readOnly
    readonly property bool writable: editor !== null && !editor.readOnly && editor.enabled
    // Read when the menu opens: the clipboard can change while it is closed.
    property bool attachable: false
    // The selection when the menu opened: opening the menu takes focus from the
    // text, and a text without a persistent selection drops it at that moment.
    property string selection: ""
    onAboutToShow: { attachable = attachAware && bridge.clipboardAttachable(); selection = editor ? editor.selectedText : "" }

    function openAt(item, position) {
        var at = item.mapToItem(null, position.x, position.y)
        var win = item.Window.window
        // The height from the rows that will show: before its first opening the
        // menu has not measured itself, and the visible rows depend on the editor.
        var w = implicitWidth, h = topPadding + bottomPadding
        for (var i = 0; i < count; i++) { var row = itemAt(i); if (row && row.shown !== false) h += row.implicitHeight }
        var x = win && at.x + w > win.width - 4 ? position.x - w : position.x
        var y = win && at.y + h > win.height - 4 ? position.y - h : position.y
        menu.feedback.anchorItem = item; menu.feedback.at = Qt.point(position.x, position.y)
        popup(item, Qt.point(x, y))
    }
    function copyOut(text) {
        if (!text) return
        if (!menu.feedback.anchorItem) { menu.feedback.anchorItem = editor; menu.feedback.at = Qt.point(0, 0) }
        bridge.copyText(text)
        menu.feedback.show()
    }

    padding: 6
    implicitWidth: 200
    // Foreground material, lighter than a panel: a translucent overlay body, one
    // fine edge, a quiet drop. The text keeps full contrast on it.
    background: Rectangle {
        implicitWidth: 200
        radius: 8
        color: Theme.glassMenuVeil
        border.width: 1
        border.color: Theme.glassOverlayEdge
        Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 3; anchors.leftMargin: 1; anchors.rightMargin: -1
                    anchors.bottomMargin: -3; radius: parent.radius + 1; color: "#000000"; opacity: 0.3 }
    }

    // One row: the action; a quiet fill under the pointer or the keyboard's
    // current item, with a fine edge so the keyboard position is explicit,
    // never colour alone. A hidden row takes no room.
    component EditItem: MenuItem {
        id: entry
        property bool shown: true
        visible: shown
        implicitHeight: shown ? 34 : 0
        height: implicitHeight
        leftPadding: 12; rightPadding: 12
        contentItem: Item {
            implicitHeight: 22
            Text { anchors.verticalCenter: parent.verticalCenter; text: entry.text
                   renderType: Theme.textRender
                   color: entry.enabled ? Theme.text : Theme.textDim
                   font: Theme.body }
        }
        background: Rectangle {
            radius: 6
            color: entry.highlighted && entry.enabled ? Theme.alpha("#36aaff", 0.12) : "transparent"
            border.width: entry.highlighted && entry.enabled ? 1 : 0
            border.color: Theme.alpha("#49bdfa", 0.4)
        }
    }

    EditItem { objectName: "editUndo"; text: qsTr("Undo"); shown: !menu.readOnlyText
               enabled: menu.writable && menu.editor.canUndo; onTriggered: menu.editor.undo() }
    EditItem { objectName: "editCut"; text: qsTr("Cut"); shown: !menu.readOnlyText
               enabled: menu.writable && menu.editor.selectedText.length > 0; onTriggered: menu.editor.cut() }
    EditItem { objectName: "editCopy"; text: qsTr("Copy")
               enabled: menu.selection.length > 0 || (menu.readOnlyText && menu.wholeText.length > 0)
               onTriggered: menu.copyOut(menu.selection.length > 0 ? menu.selection : menu.wholeText) }
    EditItem { objectName: "editPaste"; text: qsTr("Paste"); shown: !menu.readOnlyText
               enabled: menu.writable && (menu.editor.canPaste || menu.attachable)
               onTriggered: menu.attachAware ? menu.pasteRequested() : menu.editor.paste() }
    MenuSeparator {
        property bool shown: !menu.readOnlyText
        visible: shown
        height: shown ? implicitHeight : 0
        contentItem: Rectangle { implicitHeight: 1; color: Theme.glassSeparator }
    }
    EditItem { objectName: "editSelectAll"; text: qsTr("Select All")
               enabled: menu.editor !== null && menu.editor.length > 0; onTriggered: menu.editor.selectAll() }

    // "Copied": a short note where the menu was opened, gone by itself.
    property Popup feedback: Popup {
        objectName: "copyFeedback_" + menu.objectName
        property Item anchorItem: null
        property point at: Qt.point(0, 0)
        parent: anchorItem
        x: at.x; y: at.y - height - 6
        padding: 0
        closePolicy: Popup.NoAutoClose
        function show() { open(); hold.restart() }
        background: Rectangle { radius: 7; color: Theme.glassMenuVeil; border.width: 1; border.color: Theme.glassOverlayEdge }
        contentItem: Text { text: qsTr("Copied"); leftPadding: 10; rightPadding: 10; topPadding: 5; bottomPadding: 5
                            color: Theme.text; renderType: Theme.textRender; font: Theme.caption }
        Timer { id: hold; interval: 1200; onTriggered: menu.feedback.close() }
    }
}
