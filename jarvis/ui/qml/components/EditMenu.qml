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
// the platform's own and need no reminder here. K4: the one menu surface and
// row (opaque; Esc returns focus to the editor).
MenuSurface {
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
        menu.feedback.anchorItem = item; menu.feedback.at = Qt.point(position.x, position.y)
        popupAt(item, position, { opener: editor || item, flip: true })
    }
    // Open it under the editor's caret-less corner (a caller without a pointer).
    function popup() { if (editor) openAt(editor, Qt.point(0, editor.height)) }
    function copyOut(text) {
        if (!text) return
        if (!menu.feedback.anchorItem) { menu.feedback.anchorItem = editor; menu.feedback.at = Qt.point(0, 0) }
        bridge.copyText(text)
        menu.feedback.show()
    }
    function run(action) { close(); action() }

    width: 200
    padding: 6

    // A hidden row takes no room.
    component EditItem: MenuRow {
        property bool shown: true
        visible: shown
        height: shown ? implicitHeight : 0
    }

    Column {
        width: menu.availableWidth
        EditItem { objectName: "editUndo"; label: qsTr("Undo"); shown: !menu.readOnlyText
                   enabled: menu.writable && menu.editor.canUndo; onTriggered: menu.run(() => menu.editor.undo()) }
        EditItem { objectName: "editCut"; label: qsTr("Cut"); shown: !menu.readOnlyText
                   enabled: menu.writable && menu.editor.selectedText.length > 0; onTriggered: menu.run(() => menu.editor.cut()) }
        EditItem { objectName: "editCopy"; label: qsTr("Copy")
                   enabled: menu.selection.length > 0 || (menu.readOnlyText && menu.wholeText.length > 0)
                   onTriggered: menu.run(() => menu.copyOut(menu.selection.length > 0 ? menu.selection : menu.wholeText)) }
        EditItem { objectName: "editPaste"; label: qsTr("Paste"); shown: !menu.readOnlyText
                   enabled: menu.writable && (menu.editor.canPaste || menu.attachable)
                   onTriggered: menu.run(() => menu.attachAware ? menu.pasteRequested() : menu.editor.paste()) }
        Item {
            visible: !menu.readOnlyText
            width: parent.width; height: 9
            Rectangle { y: 4; x: 8; width: parent.width - 16; height: 1; color: Theme.glassSeparator }
        }
        EditItem { objectName: "editSelectAll"; label: qsTr("Select All")
                   enabled: menu.editor !== null && menu.editor.length > 0; onTriggered: menu.run(() => menu.editor.selectAll()) }
    }

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
